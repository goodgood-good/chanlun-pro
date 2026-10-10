"""Read notification receipts with SQL pagination, without dispatching anything."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time

from .monitor_view import LABELS

STAGES = {"confirmed": "确认", "warning": "预警", "invalidated": "失效", "triggered": "触发", "left": "退出"}
STATUSES = {"sent": "已发送", "pending": "待发送", "failed": "发送失败", "expired": "已过期", "suppressed": "静默", "unknown": "状态未知"}
KINDS = {"signal": "买卖点", "consolidation": "盘整背驰", "risk": "持仓风控", "screening": "选股摘要", "chart": "通知图表"}
EVENT_FIELDS = frozenset({
    "id", "market", "code", "name", "frequency", "point_type", "signal_type", "stage", "point_status",
    "recorded_at", "available_at", "point_confirmed_at", "source_closed_at", "anchor_at", "anchor_price",
    "selection_available_at", "first_detected_at", "first_warned_at", "state_changed_at", "holding",
    "macd_area_ratio", "core_position", "comparison_form", "invalidation_reason", "exit_reasons",
    "stop_price", "trigger_price", "reference_point_type", "reverse_point_type", "reverse_price",
    "notification_suppressed_reason", "external_notifications",
    "evidence_codes", "confirmation_delay_bars", "invalidation_price", "center_ordinal",
    "macd_area_a", "macd_area_c", "center_zd_price", "center_zg_price",
})
PNG_PATTERN = re.compile(r"/public/alert-chart/([a-f0-9]{64})\.png(?:\?|[)\s\"']|$)")
DELIVERY_TIME = "CASE WHEN o.status='sent' THEN COALESCE(o.sent,o.created) ELSE o.created END"
ORDER_BASIS = "sent_at_for_sent_else_created_at"
CHART_PERIODS = ('1m', '5m', '30m')


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _pack(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _unpack(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,3000}", value or ""):
        raise ValueError("通知标识不合法")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
    except (ValueError, UnicodeError):
        raise ValueError("通知标识不合法") from None


def parse_options(args, now):
    allowed = {"page", "size", "page_size", "q", "status", "kind", "scope", "frequency", "hours", "as_of", "cursor"}
    if set(args) - allowed or (hasattr(args, "getlist") and any(len(args.getlist(k)) != 1 for k in args)):
        raise ValueError("通知筛选参数不合法")
    if "size" in args and "page_size" in args:
        raise ValueError("请只提供一个分页大小")
    try:
        page, size = int(args.get("page", 1)), int(args.get("size", args.get("page_size", 25)))
    except (ValueError, TypeError):
        raise ValueError("通知分页参数不合法") from None
    if not 1 <= page <= 100000 or size not in (15, 25, 50, 100):
        raise ValueError("每页数量仅支持15、25、50或100")
    result = {"page": page, "size": size, "q": str(args.get("q", "")).strip()}
    if len(result["q"]) > 100:
        raise ValueError("搜索内容不能超过100个字符")
    choices = {"status": {"all", "sent", "pending", "failed", "expired", "suppressed"},
               "kind": {"all", *KINDS}, "scope": {"all", "holdings"},
               "frequency": {"all", "1m", "5m", "30m"}, "hours": {"24", "168", "720", "all"}}
    for field, values in choices.items():
        result[field] = str(args.get(field, "24" if field == "hours" else "all"))
        if result[field] not in values:
            raise ValueError("通知筛选范围不合法")
    try:
        result["as_of"] = float(args.get("as_of", now))
        cursor = json.loads(_unpack(args["cursor"])) if args.get("cursor") else None
        if cursor is not None:
            if set(cursor) != {"at", "id", "as_of", "filter"} or cursor["filter"] != _filter_key(result):
                raise ValueError()
            if _stamp(cursor["at"]) is None or not isinstance(cursor["id"], str):
                raise ValueError()
            result["as_of"] = float(cursor["as_of"])
        if _stamp(result["as_of"]) is None or result["as_of"] > now + 60:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise ValueError("通知分页游标不合法") from None
    result["cursor"] = cursor
    result["since"] = 0 if result["hours"] == "all" else max(0, result["as_of"] - int(result["hours"]) * 3600)
    return result


def _filter_key(options):
    return hashlib.sha256(json.dumps({"order": ORDER_BASIS, **{k: options[k] for k in ("q", "status", "kind", "scope", "frequency", "hours")}},
                                    sort_keys=True).encode()).hexdigest()[:20]


def _events(raw):
    try:
        values = json.loads(raw or "[]")
        return [v for v in values if isinstance(v, dict)] if isinstance(values, list) else []
    except (ValueError, TypeError):
        return []


def _body(content):
    try:
        value = json.loads(content)
        if isinstance(value, dict):
            return str(value.get("text", "")), str(value.get("title", ""))
    except (ValueError, TypeError):
        pass
    text = str(content or "")
    return text, text.splitlines()[0] if text else ""


def _delivery_status(status):
    return ('sent' if status == 'sent' else 'pending' if status in ('pending', 'retry', 'sending')
            else status if status in ('failed', 'expired') else 'unknown')


def _chart_periods(value):
    if not isinstance(value, list) or any(v not in CHART_PERIODS for v in value) or len(set(value)) != len(value):
        return None
    return [period for period in CHART_PERIODS if period in value]


def _saved_exit_plan(value):
    """Expose saved risk references, never file locations or proof graphs."""
    if not isinstance(value, dict):
        return None
    plan = {}
    for key in ('source_frequency','side','point_status'):
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r'[a-z0-9_]{1,32}', item):
            plan[key] = item
    for key in ('known_at','as_of'):
        if _stamp(value.get(key)) is not None:
            plan[key] = value[key]
    for key in ('stop_loss','take_profit'):
        reference = value.get(key)
        if not isinstance(reference, dict):
            continue
        saved = {}
        price = reference.get('price')
        if price is None or type(price) in (int,float) and math.isfinite(price):
            saved['price'] = price
        if type(reference.get('price_tick')) is int:
            saved['price_tick'] = reference['price_tick']
        for field in ('status','basis','trigger','point_type'):
            item = reference.get(field)
            if isinstance(item, str) and re.fullmatch(r'[a-z0-9_]{1,64}', item):
                saved[field] = item
        if saved:
            plan[key] = saved
    return plan or None


def _public_event(event):
    result = {k:v for k,v in event.items() if k in EVENT_FIELDS}
    plan = _saved_exit_plan(event.get('exit_plan'))
    if plan is not None:
        result['exit_plan'] = plan
    return result


def _evidence_lines(events):
    lines = []
    for event in events:
        name, code = event.get('name'), event.get('code')
        symbol = f'{name} {code}' if name and code and name != code else name or code or '该事件'
        label = ' · '.join(str(value) for value in (
            symbol, event.get('frequency'), LABELS.get(event.get('point_type'), event.get('point_type'))
        ) if value)
        if _stamp(event.get('anchor_price')) is not None:
            lines.append(f"{label} · 保存的定位价：{event['anchor_price']}")
        if type(event.get('macd_area_ratio')) in (int,float) and math.isfinite(event['macd_area_ratio']):
            lines.append(f"{label} · 保存的 MACD 面积比（比较段/参照段）：{event['macd_area_ratio']:.4f}")
        plan = event.get('exit_plan') or {}
        stop, target = plan.get('stop_loss') or {}, plan.get('take_profit') or {}
        if _stamp(stop.get('price')) is not None:
            trigger = {'lt':'跌破触发','gt':'升破触发'}.get(stop.get('trigger'))
            suffix = f"（{trigger}）" if trigger else ''
            lines.append(f"{label} · 保存的结构止损参考价：{stop['price']:g}{suffix}")
        if target.get('basis') == 'opposite_point' and target.get('status') == 'waiting':
            lines.append(f"{label} · 原止盈/退出计划：等待本周期已确认反向信号")
        elif _stamp(target.get('price')) is not None:
            lines.append(f"{label} · 保存的止盈/退出参考价：{target['price']:g}")
        for key, title in (('stop_price','结构止损价'),('trigger_price','触发价'),('reverse_price','反向信号价')):
            if key == 'stop_price' and _stamp(stop.get('price')) is not None:
                continue
            if _stamp(event.get(key)) is not None:
                lines.append(f"{label} · {title}：{event[key]}")
    return lines or ['该次记录未保存可展示的结构触发依据。']


class RecentNotifications:
    def __init__(self, root, *, now=None):
        self.root = Path(root)
        self.now = now or time.time
        self._lock = threading.Lock()
        self._metadata = ({}, {}, set())
        self._metadata_at = -math.inf

    def _state_metadata(self):
        # Only retained identity labels are read, at most once per15 seconds.
        # Sending history/pagination never enumerates all events in Python.
        with self._lock:
            if self.now() - self._metadata_at < 15:
                return self._metadata
            try:
                state = json.loads((self.root / "state.json").read_text(encoding="utf-8"))
                events, profiles = {}, {}
                values = [*state.get("signals", {}).values(), *state.get("unverified_signals", {}).values(),
                          *state.get("events", [])]
                for event in values:
                    if not isinstance(event, dict) or not isinstance(event.get("id"), str):
                        continue
                    profiles[event["id"]] = {k: event.get(k) for k in ("market", "code", "name", "frequency")}
                    if _stamp(event.get("recorded_at")) is not None:
                        key = event["id"], event.get("stage", ""), event["recorded_at"]
                        events[key] = _public_event(event)
                self._metadata = events, profiles, set(state.get("holding_symbols", []))
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            self._metadata_at = self.now()
            return self._metadata

    @contextmanager
    def _read(self, filename):
        path = self.root / filename
        if not path.is_file():
            yield None
            return
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def _event_filter(self, options, held, expression="j.value"):
        clauses, args = [], []
        value = lambda field: f"json_extract({expression}, '$.{field}')"
        if options["kind"] in {"signal", "consolidation", "risk"}:
            if options["kind"] == "signal":
                clauses.append(f"COALESCE({value('signal_type')},'buy_sell_point') NOT IN ('consolidation_divergence','holding_risk')")
            else:
                clauses.append(f"{value('signal_type')}=?")
                args.append("consolidation_divergence" if options["kind"] == "consolidation" else "holding_risk")
        if options["frequency"] != "all":
            clauses.append(f"{value('frequency')}=?"); args.append(options["frequency"])
        if options["scope"] == "holdings":
            if not held:
                clauses.append("0")
            else:
                clauses.append(f"COALESCE({value('market')},'a')||':'||{value('code')} IN ({','.join('?' for _ in held)})")
                args.extend(sorted(held))
        if options["q"]:
            label = "CASE " + value('point_type') + " " + " ".join(
                "WHEN '" + key + "' THEN '" + text + "'" for key, text in LABELS.items()) + " ELSE '' END"
            clauses.append(f"lower(COALESCE({value('code')},'')||' '||COALESCE({value('name')},'')||' '||COALESCE({value('point_type')},'')||' '||{label}) LIKE ? ESCAPE '\\'")
            text = options["q"].lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args.append("%" + text + "%")
        return " AND ".join(clauses) or "1", args

    def _delivery_sql(self, options, held):
        payload = "CASE WHEN json_valid(p.events) AND json_type(p.events)='array' THEN p.events ELSE '[]' END"
        where, args = [f"({DELIVERY_TIME})>=?", f"({DELIVERY_TIME})<=?"], [options["since"], options["as_of"]]
        kind = options["kind"]
        if kind in ("screening", "chart"):
            where.append("o.kind=?"); args.append(kind)
        elif kind != "all":
            where.append("o.kind='signals'")
        event_filter, event_args = self._event_filter(options, held)
        if event_filter != "1":
            expression = f"EXISTS (SELECT 1 FROM json_each({payload}) j WHERE {event_filter})"
            # Unstructured screening summaries have no per-symbol payload.
            # Do not let receipt-wide text satisfy a different signal's filter.
            if options['q'] and kind in ('all', 'screening') and options['scope'] == options['frequency'] == 'all':
                expression = f"({expression} OR (o.kind='screening' AND lower(o.content) LIKE ? ESCAPE '\\'))"
                event_args.append(event_args[-1])
            where.append(expression); args.extend(event_args)
        source = f"FROM outbox o LEFT JOIN signal_payloads p ON p.outbox_id=o.id WHERE {' AND '.join(where)}"
        status = "CASE WHEN o.status='sent' THEN 'sent' WHEN o.status IN ('pending','retry','sending') THEN 'pending' WHEN o.status IN ('failed','expired') THEN o.status ELSE 'unknown' END"
        return source, args, status, payload

    def _silent_rows(self, options, held, *, count_only=False, after=None):
        events, profiles, _ = self._state_metadata()
        with self._read('signal_analytics.sqlite3') as db:
            if db is None:
                return False if after is not None else [] if not count_only else 0
            # A connection-local label relation is bounded by retained state;
            # SQL still filters/orders the full persisted analytical history.
            db.execute("PRAGMA query_only=OFF")
            db.execute("CREATE TEMP TABLE labels(signal_id TEXT PRIMARY KEY,payload TEXT)")
            db.executemany("INSERT INTO labels VALUES (?,?)", ((key, json.dumps(value, ensure_ascii=False)) for key, value in profiles.items()))
            payload = "json_patch(COALESCE(l.payload,'{}'),json_object('id',e.signal_id,'market',e.market,'frequency',e.frequency,'point_type',e.point_type,'signal_type',e.signal_type,'stage',e.stage,'recorded_at',e.recorded))"
            matching, values = self._event_filter(options, held, payload)
            source = f"FROM events e LEFT JOIN labels l ON l.signal_id=e.signal_id WHERE e.external=0 AND e.recorded>=? AND e.recorded<=? AND ({matching})"
            args = [options['since'], options['as_of'], *values]
            if options['kind'] in {'screening', 'chart'}:
                source += " AND 0"
            if after is not None:
                return db.execute('SELECT 1 ' + source + ' AND e.recorded>? LIMIT 1',
                                  [*args, after]).fetchone() is not None
            if count_only:
                return db.execute("SELECT COUNT(*) " + source, args).fetchone()[0]
            cursor = options.get('cursor')
            if cursor:
                source += " AND (e.recorded<? OR (e.recorded=? AND e.id<?))"
                args += [cursor['at'], cursor['at'], cursor['id']]
            rows = db.execute("SELECT e.*," + payload + " AS payload " + source + " ORDER BY e.recorded DESC,e.id DESC LIMIT ? OFFSET ?",
                              [*args, options['size'] + 1, 0 if cursor else (options['page']-1)*options['size']]).fetchall()
            return [self._silent_item(row, events, held) for row in rows]

    def _silent_item(self, row, events, held):
        payload = json.loads(row['payload'])
        event = {**payload, **events.get((row['signal_id'], row['stage'], row['recorded']), {})}
        event['recorded_at'] = row['recorded']
        event['external_notifications'] = False
        item = self._item('s.' + row['id'], 'suppressed', 'signals', [event], row['recorded'], None, '')
        item.update(_source_id=row['id'], body='', message='', error='',
                    suppression_reason=event.get('notification_suppressed_reason') or '事件未授权外发',
                    metadata_complete=bool(event.get('code')))
        return item

    def _item(self, identity, status, kind, events, created, sent, error):
        held = self._state_metadata()[2]
        first = events[0] if events else {}
        category = kind if kind in ('screening', 'chart') else (
            'consolidation' if first.get('signal_type') == 'consolidation_divergence' else
            'risk' if first.get('signal_type') == 'holding_risk' else 'signal')
        title = KINDS[category] if not events else ' · '.join(filter(None, (
            first.get('name') or first.get('code') or '历史事件', first.get('frequency'),
            LABELS.get(first.get('point_type'), first.get('point_type')), STAGES.get(first.get('stage'), first.get('stage')))))
        if len(events) > 1:
            title += f" 等{len(events)}条信号"
        def shared(field):
            value = first.get(field)
            if len(events) <= 1:
                return value
            return value if value and all(event.get(field) == value for event in events) else None
        confirmations = {_stamp(e.get('point_confirmed_at') or (
            e.get('available_at') if e.get('stage') in ('confirmed','triggered') else None)) for e in events}
        event_times = [_stamp(e.get('recorded_at')) for e in events]
        event_times = [v for v in event_times if v is not None]
        return {
            'id': identity, 'status': status, 'delivery_status': status, 'status_label': STATUSES.get(status, status),
            'kind': category, 'title': title, 'summary': title,
            'code': first.get('code'), 'name': first.get('name'), 'market': first.get('market'),
            'frequency': shared('frequency'), 'point_type': shared('point_type'), 'signal_stage': shared('stage'),
            'holding': any(f"{e.get('market','a')}:{e.get('code')}" in held for e in events),
            'holding_at_event': any(e.get('holding') is True for e in events),
            'symbols': [{k: e.get(k) for k in ('market','code','name','frequency')} for e in events],
            'event_count': len(events), 'signals_count': len(events),
            'event_at': min(event_times) if event_times else None,
            'confirmed_at': next(iter(confirmations)) if len(confirmations) == 1 else None,
            'created_at': created, 'sent_at': sent, 'sort_at': sent if status == 'sent' and sent is not None else created,
            'error': error or '', 'image_count': int(kind == 'chart'),
            'signal': _public_event(first),
            'signals': [_public_event(e) for e in events],
        }

    def _receipt_item(self, row, options=None):
        events = _events(row['events'])
        if options is not None:
            held = self._state_metadata()[2]

            def matches(event):
                kind = options['kind']
                signal_type = event.get('signal_type') or 'buy_sell_point'
                if kind == 'signal' and signal_type in ('consolidation_divergence','holding_risk'):
                    return False
                if kind in ('consolidation','risk') and signal_type != (
                    'consolidation_divergence' if kind == 'consolidation' else 'holding_risk'):
                    return False
                if options['frequency'] != 'all' and event.get('frequency') != options['frequency']:
                    return False
                if options['scope'] == 'holdings' and f"{event.get('market','a')}:{event.get('code')}" not in held:
                    return False
                text = ' '.join(str(event.get(k) or '') for k in ('code','name','point_type'))
                text += ' ' + LABELS.get(event.get('point_type'), '')
                return not options['q'] or options['q'].lower() in text.lower()

            # Display the matching member first, without dropping other members
            # of the same actually sent message from its detail or counts.
            events = [e for e in events if matches(e)] + [e for e in events if not matches(e)]
        status = ('sent' if row['status'] == 'sent' else 'pending' if row['status'] in ('pending','retry','sending')
                  else row['status'] if row['status'] in ('failed','expired') else 'unknown')
        item = self._item('d.' + _pack(row['id']), status, row['kind'], events, row['created'], row['sent'], row['error'])
        body, title = _body(row['content'])
        if row['kind'] in ('screening','chart'):
            item['title'] = title or item['title']
        item.update(_source_id=row['id'], attempts=row['attempts'], next_attempt_at=row['next_try'],
                    expires_at=row['expires'], body=body, message=body)
        names = list(dict.fromkeys(PNG_PATTERN.findall(str(row['content']))))
        coverage = [self._image_coverage(name) for name in names]
        if coverage:
            item['chart_role'] = 'completion' if row['id'].startswith('chart-complete:') else 'initial'
            item['chart_coverage_status'] = ('partial' if any(v['coverage_status'] == 'partial' for v in coverage)
                                             else 'complete' if all(v['coverage_status'] == 'complete' for v in coverage)
                                             else 'unknown')
            item['chart_missing_periods'] = [p for p in CHART_PERIODS if any(p in v['missing_periods'] for v in coverage)]
        return item

    def _image_coverage(self, name):
        """Coverage belongs to an immutable PNG, never to a mutable job status."""
        unknown = {'coverage_status': 'unknown', 'periods': [], 'missing_periods': []}
        if not re.fullmatch(r'[a-f0-9]{64}', name or ''):
            return unknown
        directory = (self.root / 'chart_images').resolve()
        path = (directory / (name + '.json')).resolve()
        if path.parent != directory:
            return unknown
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
            periods, missing = _chart_periods(value.get('periods')), _chart_periods(value.get('missing_periods'))
            if (value.get('schema') != 'notification-chart-coverage-v1' or value.get('image_sha256') != name
                    or not periods or missing is None or set(periods) & set(missing)
                    or set(periods) | set(missing) != set(CHART_PERIODS)
                    or value.get('primary_frequency') not in periods):
                return unknown
            return {'coverage_status': 'partial' if missing else 'complete',
                    'periods': periods, 'missing_periods': missing}
        except (OSError, ValueError, TypeError, AttributeError):
            return unknown

    def _has_newer(self, options, held):
        now = self.now()
        if now <= options['as_of']:
            return False
        since = 0 if options['hours'] == 'all' else now - int(options['hours']) * 3600
        current = {**options, 'as_of': now, 'since': max(options['as_of'], since)}
        if options['status'] == 'suppressed':
            return self._silent_rows(current, held, after=options['as_of'])
        with self._read('dingtalk_outbox.sqlite3') as db:
            if db is None:
                return False
            source, params, _, _ = self._delivery_sql(current, held)
            if options['status'] != 'all':
                statuses = ('pending','retry','sending') if options['status'] == 'pending' else (options['status'],)
                source += f" AND o.status IN ({','.join('?' for _ in statuses)})"
                params.extend(statuses)
            return db.execute('SELECT 1 ' + source + f' AND ({DELIVERY_TIME})>? LIMIT 1',
                              [*params, options['as_of']]).fetchone() is not None

    def list(self, args):
        options = parse_options(args, self.now())
        held = self._state_metadata()[2]
        summary = {k: 0 for k in ('sent','pending','failed','expired','suppressed','holdings')}
        summary['last_sent_at'] = None
        rows, total = [], 0
        with self._read('dingtalk_outbox.sqlite3') as db:
            if db is not None:
                source, params, status, payload = self._delivery_sql(options, held)
                groups = db.execute(f"SELECT {status} AS s,COUNT(*) AS n,MAX(o.sent) AS last_sent {source} GROUP BY s", params).fetchall()
                for group in groups:
                    summary[group['s']] = group['n']
                    if group['s'] == 'sent': summary['last_sent_at'] = group['last_sent']
                holding_source, holding_params, _, _ = self._delivery_sql({**options, 'scope':'holdings'}, held)
                summary['holdings'] = db.execute('SELECT COUNT(*) ' + holding_source, holding_params).fetchone()[0]
                selected_status = options['status']
                if selected_status != 'suppressed':
                    if selected_status != 'all':
                        raw_statuses = ('pending','retry','sending') if selected_status == 'pending' else (selected_status,)
                        source += f" AND o.status IN ({','.join('?' for _ in raw_statuses)})"
                        params.extend(raw_statuses)
                    total = db.execute('SELECT COUNT(*) ' + source, params).fetchone()[0]
                    cursor = options['cursor']
                    if cursor:
                        source += f' AND (({DELIVERY_TIME})<? OR (({DELIVERY_TIME})=? AND o.id<?))'
                        params += [cursor['at'],cursor['at'],cursor['id']]
                    rows = [self._receipt_item(row, options) for row in db.execute(
                        f"SELECT o.*, {payload} AS events {source} ORDER BY ({DELIVERY_TIME}) DESC,o.id DESC LIMIT ? OFFSET ?",
                        [*params, options['size'] + 1, 0 if cursor else (options['page']-1)*options['size']]).fetchall()]
        summary['suppressed'] = self._silent_rows(options, held, count_only=True)
        if options['status'] == 'suppressed':
            total = summary['suppressed']; rows = self._silent_rows(options, held)
        has_more = len(rows) > options['size']
        rows = rows[:options['size']]
        cursor = None
        if rows and has_more:
            cursor = _pack(json.dumps({'at': rows[-1]['sort_at'], 'id': rows[-1]['_source_id'],
                                      'as_of': options['as_of'], 'filter': _filter_key(options)}, separators=(',',':')))
        for row in rows:
            for key in ('_source_id','body','message','signal','signals'):
                row.pop(key, None)
        return {'items': rows, 'pagination': {'page': options['page'], 'size': options['size'], 'page_size': options['size'],
                'total': total, 'pages': max(1, math.ceil(total/options['size'])),
                'has_more': has_more, 'as_of': options['as_of'], 'next_cursor': cursor, 'order': ORDER_BASIS,
                'has_newer': self._has_newer(options, held) if 'as_of' in args or args.get('cursor') else False},
                'summary': summary, 'window': {'hours': options['hours'], 'since': options['since'], 'until': options['as_of'],
                                              'time_basis': ORDER_BASIS},
                'summary_scope': 'same_filters_except_status; holdings_counts_messages_for_current_holdings',
                'loading': False, 'generated_at': self.now()}

    def _receipt(self, raw_id):
        with self._read('dingtalk_outbox.sqlite3') as db:
            if db is None: return None
            row = db.execute('SELECT o.*,p.events FROM outbox o LEFT JOIN signal_payloads p ON p.outbox_id=o.id WHERE o.id=?', (raw_id,)).fetchone()
            return None if row is None else dict(row)

    def _chart_sources(self, receipt):
        from .dingtalk_notifications import signal_key
        sources = [receipt]
        events = _events(receipt['events'])
        keys = []
        for event in events:
            try: key = signal_key(event)
            except (KeyError, TypeError, ValueError): continue
            if key not in keys: keys.append(key)
        inline, linked = [], {}
        with self._read('dingtalk_outbox.sqlite3') as db:
            if db is not None:
                inline = [dict(r) for r in db.execute(
                    'SELECT signal_key,url,outbox_id FROM inline_charts WHERE outbox_id=?' +
                    (f" OR signal_key IN ({','.join('?' for _ in keys)})" if keys else ''), [receipt['id'], *keys])]
                targets = list(dict.fromkeys([*(prefix + key for key in keys for prefix in ('chart:', 'chart-complete:')),
                                              *(row['outbox_id'] for row in inline)]))
                if targets:
                    linked = {r['id']: dict(r) for r in db.execute(
                        'SELECT o.*,p.events FROM outbox o LEFT JOIN signal_payloads p ON p.outbox_id=o.id '
                        f"WHERE o.id IN ({','.join('?' for _ in targets)})", targets)}
                    sources.extend(linked[key] for key in targets if key in linked and key != receipt['id'])
        jobs = {}
        try:
            with self._read('chart_jobs.sqlite3') as db:
                if db is not None and keys:
                    jobs = {r['id']: dict(r) for r in db.execute(
                        f"SELECT * FROM jobs WHERE id IN ({','.join('?' for _ in keys)})", keys)}
        except (sqlite3.Error, OSError):
            # Progress is a separate source. Its read failure cannot hide an
            # actual delivery receipt or an already saved PNG.
            jobs = {key: {'id': key, 'status': 'unavailable', 'error': '图表生成状态暂不可读取'} for key in keys}
        return sources, inline, jobs

    @staticmethod
    def _image_note(coverage, receipt, job):
        delivery = STATUSES.get(_delivery_status(receipt['status']), '状态未知')
        if coverage['coverage_status'] == 'partial':
            note = f"首张部分图{delivery}；缺少 {' / '.join(coverage['missing_periods'])}"
            state = job.get('status')
            retry = {'pending': '补图等待重试', 'running': '正在重试补图', 'failed': '补图重试失败',
                     'expired': '补图已过期，未补发', 'ready': '补图生成已完成，请查看关联图表回执',
                     'unavailable': '补图生成状态暂不可读取'}.get(state)
            return note + ('；' + retry if retry else '')
        if receipt['id'].startswith('chart-complete:'):
            return '补全图' + delivery + ('；三个周期齐全' if coverage['coverage_status'] == 'complete' else '；周期完整性未记录')
        return '原始图' + delivery + ('；三个周期齐全' if coverage['coverage_status'] == 'complete' else '；周期完整性未记录')

    def _image_records(self, identity, receipt, *, with_receipts=False):
        from .dingtalk_notifications import signal_key
        sources, inline, jobs = self._chart_sources(receipt)
        images, owners = {}, {}
        for source in sources:
            owners[source['id']] = source
            for name in PNG_PATTERN.findall(str(source['content'])):
                images.setdefault(name, []).append(source['id'])
        for row in inline:
            if row['outbox_id'] in owners:
                for name in PNG_PATTERN.findall(row['url']):
                    if row['outbox_id'] not in images.setdefault(name, []):
                        images[name].append(row['outbox_id'])
        result, seen_jobs = [], set()
        for index, (name, receipts) in enumerate(images.items()):
            source = owners[receipts[0]]
            source_events = _events(source['events'])
            try: key = signal_key(source_events[0]) if len(source_events) == 1 else None
            except (KeyError, TypeError, ValueError): key = None
            if key: seen_jobs.add(key)
            job = jobs.get(key, {})
            coverage = self._image_coverage(name)
            path = (self.root / 'chart_images' / (name + '.png')).resolve()
            valid = path.parent == (self.root / 'chart_images').resolve() and path.is_file()
            role = 'completion' if source['id'].startswith('chart-complete:') else 'initial'
            label = ('补全图' if role == 'completion' else '首张部分图' if coverage['coverage_status'] == 'partial' else '原始通知图')
            if len(source_events) == 1 and source_events[0].get('code'):
                label = str(source_events[0]['code']) + ' · ' + label
            if coverage['periods']: label += ' · ' + ' / '.join(coverage['periods'])
            result.append({'frequency': '原始通知图', **coverage, 'label': label, 'role': role,
                           'status': 'ready' if valid else 'expired',
                           'receipt_id': 'd.' + _pack(source['id']), 'delivery_status': _delivery_status(source['status']),
                           'created_at': source['created'], 'sent_at': source['sent'],
                           'generation_status': job.get('status'), 'next_attempt_at': job.get('next_try'),
                           'status_note': self._image_note(coverage, source, job),
                           'url': f'/monitor/notifications/{identity}/images/{index}.png?version={name}' if valid else None,
                           'error': '' if valid else '保存的图片已过期或不可用',
                           '_hash': name, '_path': path if valid else None})
        for key, job in jobs.items():
            if key in seen_jobs: continue
            missing = []
            try: missing = _chart_periods(json.loads(job.get('missing_periods') or '[]')) or []
            except (TypeError, ValueError): pass
            try: event = json.loads(job.get('event') or '{}')
            except (TypeError, ValueError): event = {}
            code = event.get('code') if isinstance(event, dict) else None
            label = f'{code} · 图表生成状态' if code else '图表生成状态'
            result.append({'frequency': '图表生成状态', 'label': label, 'periods': [],
                           'missing_periods': missing, 'coverage_status': 'unknown', 'is_placeholder': True,
                           'status': job['status'], 'generation_status': job['status'],
                           'next_attempt_at': job.get('next_try'), 'url': None,
                           'error': job.get('error') or ('图表记录存在，但保存图片不可用' if job['status'] == 'ready' else ''),
                           '_path': None})
        related = [{'id': 'd.' + _pack(source['id']), 'status': _delivery_status(source['status']),
                    'role': 'completion' if source['id'].startswith('chart-complete:') else 'initial',
                    'kind': source['kind'], 'created_at': source['created'], 'sent_at': source['sent']}
                   for source in sources if source['kind'] == 'chart' or any(source['id'] in values for values in images.values())]
        return (result, related) if with_receipts else result

    def detail(self, identity):
        if identity.startswith('d.'):
            raw_id = _unpack(identity[2:])
            receipt = self._receipt(raw_id)
            if receipt is None: return None
            result = self._receipt_item(receipt)
            images, related = self._image_records(identity, receipt, with_receipts=True)
            result['images'] = [{k:v for k,v in image.items() if not k.startswith('_')} for image in images]
            result['related_chart_receipts'] = related
            result['image_count'] = sum(not image.get('is_placeholder') for image in images)
            result['evidence_lines'] = _evidence_lines(result['signals'])
            result['evidence_lines_title'] = '保存的信号要点'
            result['structural_evidence_complete'] = False
            result['record_notes'] = ['本页展示该次通知的保存记录，不代表当前行情。',
                '本页未展示完整结构触发链，定位价和风控计划不等同于完整触发证明。',
                '已发送表示通知接口成功返回；不等同于接收人已经阅读。']
        elif re.fullmatch(r's\.[a-f0-9]{64}', identity):
            events, profiles, held = self._state_metadata()
            with self._read('signal_analytics.sqlite3') as db:
                row = db.execute('SELECT * FROM events WHERE id=? AND external=0',(identity[2:],)).fetchone() if db else None
            if row is None: return None
            row = dict(row); payload = {**profiles.get(row['signal_id'],{}), **{k:row[k] for k in ('market','frequency','point_type','signal_type','stage')}}
            payload['id'] = row['signal_id']; row['payload'] = json.dumps(payload)
            result = self._silent_item(row, events, held)
            result['images'] = []
            result['evidence_lines'] = _evidence_lines(result['signals'])
            result['evidence_lines_title'] = '保存的信号要点'
            result['structural_evidence_complete'] = False
            result['record_notes'] = ['该事件为静默记录，没有实际发送正文。']
            if not result['metadata_complete']: result['record_notes'].append('历史记录没有保留完整标的信息。')
        else:
            raise ValueError('通知标识不合法')
        result.pop('_source_id',None)
        return result

    def image_path(self, identity, index, version=None):
        if (not identity.startswith('d.') or type(index) is not int or index < 0 or index > 100
                or not re.fullmatch(r'[a-f0-9]{64}', version or '')):
            return None
        receipt = self._receipt(_unpack(identity[2:]))
        if receipt is None: return None
        images = self._image_records(identity, receipt)
        # A late image can move an earlier one to another display index. The
        # content hash remains the authority, but must still belong to this
        # receipt; a stale URL can never return the new image at its old index.
        return next((image['_path'] for image in images if image.get('_hash') == version), None)


@lru_cache(maxsize=4)
def reader_for(root):
    return RecentNotifications(root)
