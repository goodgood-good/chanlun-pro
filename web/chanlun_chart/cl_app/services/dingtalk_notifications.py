"""Durable, deduplicated DingTalk delivery for application events only.

Credentials stay in private destination settings or environment variables.
This sender is independent of development hooks and never logs a webhook URL.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import json
import math
import re
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from chanlun.screening.rules import CN
from .notification_text import signal_content, signal_status_lines as formatted_status_lines, heading, illustrated_signal, event_priority, queued_priority
from .notification_freshness import notification_deadline, notification_is_live
from .notification_dedup import already_sent as semantic_already_sent, migrate_sent
from .notification_channels import DingTalkChannels, event_channel


POINTS = {"1buy": "一买", "2buy": "二买", "3buy": "三买", "1sell": "一卖", "2sell": "二卖", "3sell": "三卖"}
STAGES = {"warning": "提前预警（未确认）", "invalidated": "预警已失效", "confirmed": "已确认", "approaching": "形成等待（未确认）", "formed": "形成等待（未确认）",
          "observed": "形成等待（未确认）", "triggered":"持仓风险触发"}


def date(value):
    return datetime.fromtimestamp(value, CN).strftime("%m-%d %H:%M:%S") if value else "—"


def _clean(value, limit=100):
    return " ".join(str(value).split())[:limit]


def signal_status_key(row):
    """Track point confirmation independently from the strategy's extra gates."""
    if row.get("signal_type") in {"consolidation_divergence", "native_point_preview"} and (
            "hypothesis_id" in row or "lifecycle_cycle" in row):
        return json.dumps([row.get("point_status", row["stage"]), row.get("lifecycle_cycle", 0)], separators=(",", ":"))
    if "point_status" not in row:
        return row["stage"]
    return json.dumps([row["point_status"], row.get("selection_status", row["stage"]),
                       row.get("lower_confirmation_state")], separators=(",", ":"))


def signal_key(row):
    return f"signal:{row['id']}:{signal_status_key(row)}"


def signal_reference(row):
    """Match the occurrence as well as its lifecycle/confirmation identity."""
    return [signal_key(row), row['recorded_at']]


def eligible_signal_events(events, *, since, stamp):
    from .notification_policy import trade_notification_rejection
    return {signal_key(row):row for row in events
            if row.get('recorded_at',0)>=since and row.get('change') in {'discovered','changed'}
            and row.get('external_notifications',True)
            and trade_notification_rejection(row) is None
            and notification_is_live(row, stamp) and row.get('stage') in STAGES}


def live_signal_events(events, stamp):
    """Every signal keeps the deadline of its original market evidence."""
    return [row for row in events if notification_is_live(row, stamp)]


def _retain_recorded_times(updated, originals):
    """A refreshed quote/structure must not extend the notification lifetime."""
    sources={row['id']:row for row in originals}
    result=[]
    for row in updated:
        original=sources.get(row.get('id')) or sources.get(row.get('legacy_id'))
        if original is not None:
            result.append({**row,'recorded_at':original['recorded_at'],
                           'notification_expires_at':notification_deadline(original)})
    return result


def signal_status_lines(row):
    return formatted_status_lines(row)


class DeliveryError(Exception):
    """A diagnostic that contains no credentials or raw provider response."""
    def __init__(self, message='', *, request_attempted=True):
        self.request_attempted = request_attempted
        super().__init__(message)


class DingTalkTransport:
    def __init__(self, channels=None, *, channel='others'):
        self.channels = channels if channels is not None else DingTalkChannels()
        self.channel = channel

    def configuration(self):
        try:
            channels = self.channels.snapshot()
        except OSError:
            return {"configured": False, "configuration_error": "通知地址配置暂不可读取", "keyword_configured": False}
        configured = any(value['configured'] for value in channels.values())
        return {"configured": configured,
                "configuration_error": "" if configured else "请在通知设置中配置钉钉机器人地址",
                "keyword_configured": bool(channels[self.channel]['keyword']), "channels": channels}

    def for_channel(self, channel):
        return DingTalkTransport(self.channels, channel=channel)

    def send(self, content):
        self._send(content)

    def send_markdown(self, title, content):
        self._send(content, title=title)

    def _send(self, content, *, title=None):
        try:
            credentials = self.channels.credentials(self.channel)
        except (OSError, ValueError) as exc:
            raise DeliveryError(str(exc), request_attempted=False) from None
        url = urlsplit(credentials['webhook'])
        query = dict(parse_qsl(url.query))
        secret = credentials['secret']
        if secret:
            stamp = str(int(time.time() * 1000))
            signature = base64.b64encode(hmac.new(secret.encode(), f"{stamp}\n{secret}".encode(), hashlib.sha256).digest()).decode()
            query.update(timestamp=stamp, sign=signature)
        address = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
        keyword = _clean(credentials['keyword'], 80)
        text = content
        if keyword and keyword not in content:
            text = content + "\n\n" + keyword if title is not None else keyword + " · " + content
        if len(text.encode("utf-8")) > 3800:
            raise DeliveryError("通知内容超过长度上限", request_attempted=False)
        payload = ({"msgtype": "markdown", "markdown": {"title": _clean(title, 100), "text": text}, "at": {"isAtAll": False}}
                   if title is not None else {"msgtype": "text", "text": {"content": text}, "at": {"isAtAll": False}})
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(address, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                        headers={"Content-Type": "application/json; charset=utf-8"},
                                        timeout=(5, 10), allow_redirects=False)
            if response.status_code != 200:
                raise DeliveryError(f"钉钉请求失败（HTTP {response.status_code}）")
            result = response.json()
            code = result.get("errcode") if isinstance(result, dict) else None
            if type(code) is not int:
                raise DeliveryError("钉钉响应缺少有效的 errcode")
            if code != 0:
                raise DeliveryError(f"钉钉拒收（errcode={code}），请检查机器人关键词、签名与限流状态")
        except requests.RequestException as exc:
            raise DeliveryError(f"钉钉网络请求失败（{type(exc).__name__}）") from None
        except ValueError:
            raise DeliveryError("钉钉返回了无法解析的响应") from None


class DingTalkOutbox:
    def __init__(self, path, *, transport=None, now=None, signal_validator=None, charts=False, holding_reader=None):
        self.path = Path(path)
        self.channels = DingTalkChannels(self.path.parent / 'notification_channels.json')
        self.transport = transport if transport is not None else DingTalkTransport(self.channels)
        self.now = now or time.time
        self.signal_validator = signal_validator
        self.holding_reader = holding_reader
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._delivery_error = ''
        self._accepted_receipt = None
        self._receipt_pending = False
        from .worker_progress import WorkerProgress
        self.progress = WorkerProgress(60)
        self.charts = None
        if charts:
            from .notification_charts import NotificationCharts
            self.charts = NotificationCharts(self)

    @contextmanager
    def _connect(self, *, timeout=5, holding_symbols=None):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=timeout)
        try:
            db.row_factory = sqlite3.Row
            db.create_function('holding_priority', 1, lambda payload: queued_priority(payload, holding_symbols))
            db.executescript("""
            CREATE TABLE IF NOT EXISTS markers (id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                sent REAL, error TEXT NOT NULL DEFAULT '', send_started REAL
            );
            CREATE TABLE IF NOT EXISTS signal_payloads (
                outbox_id TEXT PRIMARY KEY, events TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS outbox_monitor_created ON outbox(created DESC,id DESC);
            CREATE INDEX IF NOT EXISTS outbox_monitor_status_created ON outbox(status,created DESC,id DESC);
            CREATE INDEX IF NOT EXISTS outbox_monitor_effective_time ON outbox(
                (CASE WHEN status='sent' THEN COALESCE(sent,created) ELSE created END) DESC,id DESC);
            CREATE INDEX IF NOT EXISTS outbox_monitor_status_effective_time ON outbox(status,
                (CASE WHEN status='sent' THEN COALESCE(sent,created) ELSE created END) DESC,id DESC);
            CREATE TABLE IF NOT EXISTS delivery_settings (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS inline_charts (signal_key TEXT PRIMARY KEY,url TEXT NOT NULL,created REAL NOT NULL,outbox_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS delivery_timings (
                outbox_id TEXT PRIMARY KEY, first_claimed REAL, last_claimed REAL,
                validation_started REAL, validation_finished REAL,
                validation_attempts INTEGER NOT NULL DEFAULT 0, validation_seconds REAL,
                request_started REAL, request_finished REAL
            );
            CREATE INDEX IF NOT EXISTS delivery_timings_claimed ON delivery_timings(last_claimed);
            CREATE TABLE IF NOT EXISTS delivery_decisions (
                outbox_id TEXT PRIMARY KEY, decided_at REAL NOT NULL,
                reason_code TEXT NOT NULL, details TEXT NOT NULL
            );
            """)
            if 'send_started' not in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}:
                try:
                    db.execute('ALTER TABLE outbox ADD COLUMN send_started REAL')
                except sqlite3.OperationalError:
                    if 'send_started' not in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}:
                        raise
            with db:
                migrate_sent(db)
                if db.execute("SELECT 1 FROM delivery_settings WHERE key='per_signal_deadlines'").fetchone() is None:
                    # Upgrade active batches from the old earliest-member
                    # deadline. Terminal records and switch cancellations stay
                    # terminal; only each original event can supply its deadline.
                    rows=db.execute("SELECT o.id,p.events FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id "
                                    "WHERE o.kind='signals' AND o.status IN ('pending','retry','sending')").fetchall()
                    for identity,payload in rows:
                        try:
                            events=json.loads(payload)
                            if not isinstance(events,list) or not events:
                                continue
                            if any(not isinstance(e,dict) or type(e.get('recorded_at')) not in (int,float)
                                   or not math.isfinite(e['recorded_at']) for e in events):
                                continue
                            db.execute('UPDATE outbox SET expires=? WHERE id=?',
                                       (max(notification_deadline(e) for e in events),identity))
                        except (ValueError,TypeError):
                            continue
                    db.execute("INSERT OR IGNORE INTO delivery_settings VALUES ('per_signal_deadlines',1)")
                if db.execute("SELECT 1 FROM delivery_settings WHERE key='market_event_deadlines_v1'").fetchone() is None:
                    rows = db.execute("SELECT o.id,p.events FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id "
                                      "WHERE o.kind IN ('signals','chart') AND o.status IN ('pending','retry','sending')").fetchall()
                    for identity, payload in rows:
                        try:
                            events = json.loads(payload)
                            deadline = max(notification_deadline(e) for e in events)
                        except (ValueError, TypeError, AttributeError):
                            deadline = 0
                        db.execute('UPDATE outbox SET expires=MIN(expires,?) WHERE id=?', (deadline, identity))
                    db.execute("INSERT OR IGNORE INTO delivery_settings VALUES ('market_event_deadlines_v1',1)")
                yield db
        finally:
            db.close()

    @contextmanager
    def _read_connect(self):
        def connect():
            db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.1)
            db.row_factory = sqlite3.Row
            return db

        db = connect()
        try:
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ready = {'markers', 'outbox', 'signal_payloads', 'delivery_settings', 'inline_charts', 'delivery_decisions'}.issubset(tables)
            ready = ready and 'send_started' in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}
            ready = ready and db.execute("SELECT 1 FROM delivery_settings WHERE key='per_signal_deadlines'").fetchone() is not None
            if not ready:
                # Release the read transaction before the one-time upgrade.
                # Routine status reads never request a write or a long wait.
                db.close()
                db = None
                with self._connect(timeout=0.1):
                    pass
                db = connect()
                db.execute('BEGIN')
            yield db
        finally:
            if db is not None:
                db.close()

    def has(self, identity):
        if not self.path.is_file():
            return False
        with self._read_connect() as db:
            return db.execute("SELECT 1 FROM markers WHERE id=?", (identity,)).fetchone() is not None

    def _holding_snapshot(self):
        """A bounded state-only scheduling hint; call outside every queue lock.

        None means unknown and preserves recorded hints. This never replaces
        the fresh membership/evidence validator which authorizes delivery.
        """
        if self.holding_reader is None:
            return None
        try:
            value = self.holding_reader()
            if isinstance(value, (set, frozenset)) and all(isinstance(key, str) for key in value):
                return frozenset(value)
        except Exception:
            pass
        return None

    @staticmethod
    def _holding_hints(events, holding_symbols):
        if holding_symbols is None:
            return events
        return [{**event, 'holding': f"{event.get('market', 'a')}:{event.get('code')}" in holding_symbols}
                for event in events]

    @staticmethod
    def _window_start(db):
        row = db.execute("SELECT value FROM delivery_settings WHERE key='window_start'").fetchone()
        return row[0] if row else 0

    @staticmethod
    def _point_policy_generation(db):
        row = db.execute("SELECT value FROM delivery_settings WHERE key='point_policy_generation'").fetchone()
        return int(row[0]) if row else 0

    def invalidate_point_notification_policy(self):
        """Fence old validation before the monitor commits a new point policy.

        Called with the monitor lock held. Dispatch reads that lock only outside
        the queue lock, and rechecks this generation before committing a send.
        Existing receipts keep their payloads/deadlines for the new validation.
        """
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO delivery_settings VALUES ('point_policy_generation',1) "
                       "ON CONFLICT(key) DO UPDATE SET value=value+1")
        self._wake.set()

    def _current_delivery(self, db, receipt):
        """Check ownership inside the transaction which will change this work."""
        current = db.execute("SELECT status,expires FROM outbox WHERE id=?", (receipt['id'],)).fetchone()
        if current is None or current['status'] != 'sending':
            return None
        if receipt['created'] < self._window_start(db):
            # Also retire a deferred batch left behind by an older dispatcher.
            db.execute("UPDATE outbox SET status='expired',error='通知开关已更新，旧队列不再补发' "
                       "WHERE id=? AND status='sending'", (receipt['id'],))
            return None
        if (receipt['kind'] in {'signals', 'chart'}
                and receipt.get('point_policy_generation', 0) != self._point_policy_generation(db)):
            db.execute("UPDATE outbox SET status='retry',attempts=?,next_try=?,send_started=NULL,"
                       "error='通知条件已更新，等待重新复核' WHERE id=? AND status='sending'",
                       (receipt['attempts'], self.now(), receipt['id']))
            return None
        return current

    def begin_window(self, since):
        """Retire old queued work on either edge of the notification switch."""
        with self._lock, self._connect() as db:
            db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('window_start',?)", (since,))
            db.execute("UPDATE outbox SET status='expired',error='通知开关已更新，旧队列不再补发' "
                       "WHERE status IN ('pending','retry','sending') AND created<=?", (since,))
        if self.charts is not None:
            try:
                self.charts.begin_window(since)
            except (OSError, sqlite3.Error):
                self.charts._runtime_error = '图表任务存储暂不可用；旧图表仍受发送起点限制'

    @staticmethod
    def _insert(db, identity, kind, content, stamp, lifetime):
        inserted = db.execute("INSERT OR IGNORE INTO markers VALUES (?,?)", (identity, stamp)).rowcount
        if inserted:
            db.execute("INSERT INTO outbox(id,kind,content,status,created,expires,next_try) VALUES (?,?,?,'pending',?,?,?)",
                       (identity, kind, content, stamp, stamp + lifetime, stamp))

    def screening_completed(self, result, baseline):
        identity = "screening:" + result["run_id"]
        failures = {(e.get("market", "a"), e["code"]) for e in result.get("errors", [])}
        # A task receipt is not a signal alert and has no fresh holding proof.
        # Point details are delivered solely through the validated signal path.
        lines = ["缠论 Pro · 选股完成", f"完成时间：{date(result.get('finished_at') or self.now())}",
                 f"检查标的：{result.get('completed_symbols',result.get('completed', 0))} / {result.get('total_symbols',result.get('total', 0))}",
                 f"数据异常：{len(failures)} 个标的", "详细结果请查看选股页面。"]
        if not result.get("source_current", True):
            lines.append("计算版本已变化，请重新选股后复核。")
        lines.append("任务：" + result["run_id"][:12])
        stamp = self.now()
        with self._lock, self._connect() as db:
            if (result.get('finished_at') or stamp) < self._window_start(db):
                return
            self._insert(db, identity, "screening", "\n".join(lines), stamp, 86400)
            # Existing pool signals are a baseline, not new live discoveries.
            db.executemany("INSERT OR IGNORE INTO markers VALUES (?,?)", [(signal_key(row), stamp) for row in baseline])
        self._wake.set()

    @staticmethod
    def _screening_receipt_text(content):
        """Also remove unvalidated point details queued by an older version."""
        safe = re.compile(r'^(?:完成时间：[0-9 /:—-]+|检查标的：\d+ / \d+|数据异常：\d+ 个标的|任务：[a-zA-Z0-9_-]{1,12})$')
        lines = ['缠论 Pro · 选股完成']
        for line in content.splitlines():
            if safe.fullmatch(line) or line == '计算版本已变化，请重新选股后复核。':
                lines.append(line)
        lines.append('详细结果请查看选股页面。')
        return '\n'.join(lines)

    def signal_events(self, events, *, since):
        from .notification_coalescing import append_pending, bucket as coalesce_bucket, BATCH_PREFIX
        stamp = self.now()
        eligible = eligible_signal_events(self._holding_hints(events, self._holding_snapshot()), since=since, stamp=stamp)
        if not eligible:
            return
        inline_events = []
        with self._lock, self._connect() as db:
            # Independent outbox instances must not both read a missing marker
            # or overwrite the same pending packet before either commits.
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            stamp = self.now()
            eligible = eligible_signal_events(eligible.values(), since=since, stamp=stamp)
            fresh = [(key, row) for key, row in eligible.items()
                     if row['recorded_at'] >= self._window_start(db)
                     and db.execute("SELECT 1 FROM markers WHERE id=?", (key,)).fetchone() is None]
            unique=[]
            for key,event in fresh:
                if semantic_already_sent(db,event):
                    identity='suppressed:'+hashlib.sha256(key.encode()).hexdigest()
                    self._insert(db,identity,'signals',self._signal_content([event],stamp),stamp,0)
                    db.execute("UPDATE outbox SET status='suppressed',error='同一结构、同一价格端点和同一状态已通知，合并重复提醒' WHERE id=?",(identity,))
                    db.execute('INSERT OR IGNORE INTO signal_payloads VALUES (?,?)',(identity,json.dumps([event],ensure_ascii=False)))
                    db.execute('INSERT OR IGNORE INTO markers VALUES (?,?)',(key,stamp))
                else:
                    unique.append((key,event))
            fresh=unique
            buckets = {}
            groups = []
            for pair in fresh:
                event = pair[1]
                # Preserve already queued legacy combined messages. New
                # warnings send their text immediately and attach charts next.
                if (self.charts is not None and event.get('inline_chart')
                        and event.get('signal_type') == 'consolidation_divergence'
                        and event.get('stage') in {'warning','confirmed','invalidated'}):
                    event = {**event,'inline_chart':True}
                    inline_events.append(event)
                    groups.append([(pair[0],event)])
                    continue
                buckets.setdefault((event.get("market"), event.get("frequency"), event.get("stage"), event_priority([event])), []).append(pair)
            for bucket in buckets.values():
                group = []
                for pair in bucket:
                    if group and (len(group) == 5 or len(self._signal_content([row for _, row in [*group, pair]], stamp).encode("utf-8")) > 3300):
                        groups.append(group)
                        group = []
                    group.append(pair)
                if group:
                    groups.append(group)
            for group in groups:
                if coalesce_bucket(group[0][1]) is not None:
                    for key, event in group:
                        merged = append_pending(db, event, stamp, self._signal_content)
                        if merged is None:
                            # A separate namespace prevents a later pending
                            # subset from colliding with this stable batch id.
                            identity = BATCH_PREFIX + hashlib.sha256(json.dumps([key]).encode()).hexdigest()
                            self._insert(db, identity, 'signals', self._signal_content([event], stamp),
                                         stamp, notification_deadline(event) - stamp)
                            db.execute('INSERT OR IGNORE INTO signal_payloads VALUES (?,?)',
                                       (identity, json.dumps([event], ensure_ascii=False, allow_nan=False)))
                        db.execute('INSERT OR IGNORE INTO markers VALUES (?,?)', (key, stamp))
                    continue
                identity = "signals:" + hashlib.sha256(json.dumps(sorted(key for key, _ in group)).encode()).hexdigest()
                payload = [row for _, row in group]
                # Individual members are pruned on every dispatch/retry. A
                # recent signal must not inherit an older member's deadline.
                lifetime = max(notification_deadline(row) for _, row in group) - stamp
                self._insert(db, identity, "signals", self._signal_content(payload, stamp), stamp, lifetime)
                db.execute("INSERT OR IGNORE INTO signal_payloads VALUES (?,?)",
                           (identity, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
                db.executemany("INSERT OR IGNORE INTO markers VALUES (?,?)", [(key, stamp) for key, _ in group])
        self._wake.set()
        if inline_events:
            try:
                self.charts.enqueue(inline_events,stamp)
            except (OSError,ValueError,sqlite3.Error):
                pass  # Recover pending illustrated events from the durable outbox.

    @staticmethod
    def _signal_content(events, stamp):
        return signal_content(events, stamp)

    def initial_chart_hash(self, event):
        """Recover the already committed first attachment after a worker crash."""
        if not self.path.is_file():
            return None
        key = signal_key(event)
        with self._read_connect() as db:
            if event.get('inline_chart'):
                row = db.execute('SELECT url FROM inline_charts WHERE signal_key=?', (key,)).fetchone()
            else:
                row = db.execute("SELECT content FROM outbox WHERE id=? AND kind='chart'", ('chart:' + key,)).fetchone()
        matched = re.search(r'/public/alert-chart/([a-f0-9]{64})\.png', str(row[0])) if row else None
        return matched.group(1) if matched else None

    def wake_validation(self, periods=None):
        """Wake only validation waits; never bypass checks or send pacing."""
        if periods and self.path.is_file():
            wanted=set(periods)
            with self._lock,self._connect() as db:
                rows=db.execute("SELECT o.id,p.events FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id "
                    "WHERE o.status='retry' AND o.error='等待信号有效性复核'").fetchall()
                for identity,raw in rows:
                    try:events=json.loads(raw)
                    except (ValueError,TypeError):continue
                    if any((f"{e.get('market','a')}:{e.get('code')}",e.get('frequency')) in wanted
                           for e in events if isinstance(e,dict)):
                        db.execute("UPDATE outbox SET next_try=? WHERE id=? AND status='retry'",(self.now(),identity))
        self._wake.set()

    def enqueue_chart(self, event, url, *, complete=False):
        if complete:
            return self._save_chart_completion(event, url)
        if not live_signal_events([event],self.now()):
            return False
        if event.get('inline_chart') and not complete:
            return self._attach_inline_chart(event,url)
        identity = ("chart-complete:" if complete else "chart:") + signal_key(event)
        stamp = self.now()
        content = {"title": ("原信号附图补全 · " if complete else "信号图表 · ") + heading(event),
                   "text": (("原通知附图补全 · 消息生成：" + date(event.get('recorded_at')) + "\n\n") if complete else "") + "### " + re.sub(r"([\\`*_{}\[\]()#+.!|>])", r"\\\1", heading(event)) + "\n\n行情截止：" + date(event.get("source_closed_at"))
                            + "（北京时间）\n\n![信号快照](" + url + ")"}
        with self._lock, self._connect() as db:
            if event['recorded_at'] < self._window_start(db):
                return False
            self._insert(db, identity, "chart", json.dumps(content, ensure_ascii=False), stamp,
                         max(0, notification_deadline(event) - stamp))
            if complete:
                db.execute("UPDATE outbox SET status='suppressed',error='附图补全仅更新页面，不重复推送' WHERE id=? AND status IN ('pending','retry')",(identity,))
            db.execute("INSERT OR IGNORE INTO signal_payloads VALUES (?,?)", (identity, json.dumps([event], ensure_ascii=False)))
        self._wake.set()
        return True

    def _completion_receipt(self, db, event):
        from .notification_evidence import same_notification_event, NotificationEvidence
        key = signal_key(event)
        if event.get('inline_chart'):
            row = db.execute("SELECT i.url,o.created,o.sent,p.events FROM inline_charts i "
                "JOIN outbox o ON o.id=i.outbox_id JOIN signal_payloads p ON p.outbox_id=o.id "
                "WHERE i.signal_key=? AND o.kind='signals' AND o.status='sent'", (key,)).fetchone()
        else:
            row = db.execute("SELECT o.content,o.created,o.sent,p.events FROM outbox o "
                "JOIN signal_payloads p ON p.outbox_id=o.id "
                "WHERE o.id=? AND o.kind='chart' AND o.status='sent'", ('chart:'+key,)).fetchone()
        if row is None or not row[2] or self.now() >= row[1] + NotificationEvidence.lifetime:
            return None
        try:
            events = json.loads(row[3])
            if not isinstance(events, list) or not any(same_notification_event(event, saved) for saved in events):
                return None
        except (ValueError, TypeError, AttributeError):
            return None
        match = re.search(r'/public/alert-chart/([a-f0-9]{64})\.png', row[0])
        return {'image_hash':match.group(1), 'deadline':row[1]+NotificationEvidence.lifetime} if match else None

    def chart_completion_receipt(self, event):
        """Only a committed first-image receipt authorizes later page updates."""
        if not self.path.is_file():
            return None
        with self._read_connect() as db:
            return self._completion_receipt(db, event)

    def _retained_signal_receipt(self,db,event):
        from .notification_evidence import same_notification_event,NotificationEvidence
        stamp=event.get('recorded_at')
        if type(stamp) not in (int,float) or not math.isfinite(stamp):return None
        rows=db.execute("SELECT o.id,o.created,o.sent,p.events FROM outbox o "
            "JOIN signal_payloads p ON p.outbox_id=o.id WHERE o.kind='signals' AND o.status='sent' "
            "AND o.sent>=? AND o.sent<=? AND o.created>=?",(stamp,stamp+1800,stamp-1800))
        for row in rows:
            if self.now()>=row['created']+NotificationEvidence.lifetime:continue
            try:events=json.loads(row['events'])
            except (ValueError,TypeError):continue
            if isinstance(events,list) and any(isinstance(e,dict) and same_notification_event(event,e) for e in events):
                return {'deadline':row['created']+NotificationEvidence.lifetime,'sent_at':row['sent'],'receipt_id':row['id']}
        return None

    def retained_signal_receipt(self,event):
        if not self.path.is_file():return None
        with self._read_connect() as db:return self._retained_signal_receipt(db,event)

    def save_retained_chart_view(self,event,url):
        return self._save_chart_completion(event,url,allow_text_receipt=True)

    def _save_chart_completion(self, event, url, *, allow_text_receipt=False):
        """A retained historical view is always silent, even after 30 minutes."""
        stamp = self.now()
        identity = 'chart-complete:' + signal_key(event)
        content = {'title':'原信号附图补全 · '+heading(event),
            'text':'原通知附图补全 · 消息生成：'+date(event.get('recorded_at'))
                +'\n\n行情截止：'+date(event.get('source_closed_at'))
                +'（北京时间）\n\n![信号快照]('+url+')'}
        with self._lock, self._connect() as db:
            receipt = (self._retained_signal_receipt(db,event) if allow_text_receipt
                       else self._completion_receipt(db, event))
            if receipt is None:
                return False
            # Insert in its terminal state in the same transaction. No live
            # dispatcher or retry can observe this as an outbound message.
            saved = db.execute("SELECT status FROM outbox WHERE id=?", (identity,)).fetchone()
            if saved is not None and saved[0] == 'sent':
                return False
            db.execute("INSERT INTO outbox "
                "(id,kind,content,status,created,expires,error) VALUES (?, 'chart', ?, 'suppressed', ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET content=excluded.content,status='suppressed',"
                "expires=excluded.expires,error=excluded.error WHERE outbox.status!='sent'",
                (identity,json.dumps(content,ensure_ascii=False),stamp,receipt['deadline'],
                 '附图补全仅更新页面，不重复推送'))
            db.execute('INSERT OR IGNORE INTO signal_payloads VALUES (?,?)',
                (identity,json.dumps([event],ensure_ascii=False,allow_nan=False)))
        return True

    def _attach_inline_chart(self,event,url):
        key=signal_key(event)
        identity='signals:'+hashlib.sha256(json.dumps([key]).encode()).hexdigest()
        with self._lock,self._connect() as db:
            row=db.execute('SELECT status,expires FROM outbox WHERE id=?',(identity,)).fetchone()
            if (event['recorded_at'] < self._window_start(db) or row is None
                    or row['status'] not in {'pending','retry','sending'} or row['expires']<=self.now()):
                return False
            db.execute('INSERT OR REPLACE INTO inline_charts VALUES (?,?,?,?)',(key,url,self.now(),identity))
            db.execute("UPDATE outbox SET next_try=?,error='' WHERE id=? AND status IN ('pending','retry')",(self.now(),identity))
        self._wake.set()
        return True

    def finish_inline_chart(self,event,status,error):
        if not event.get('inline_chart') or status not in {'failed','expired'}:
            return
        identity='signals:'+hashlib.sha256(json.dumps([signal_key(event)]).encode()).hexdigest()
        with self._lock,self._connect() as db:
            db.execute("UPDATE outbox SET status=?,error=? WHERE id=? AND status IN ('pending','retry','sending')",(status,error,identity))

    def referenced_runs(self):
        """Retain source evidence while a delivery/image can still need it."""
        result=self.charts.archive_referenced_runs() if self.charts is not None else set()
        if not self.path.exists():
            return result
        with self._read_connect() as db:
            rows = db.execute("SELECT p.events FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id "
                              "WHERE o.status IN ('pending','retry','sending') OR (o.status='sent' AND o.sent>=?)",
                              (self.now()-1800,)).fetchall()
        for row in rows:
            try:
                for event in json.loads(row[0]):
                    for proof in (event,event.get('invalidation_reference') or {}):
                        run = proof.get("evidence_run_id", "")
                        if isinstance(run, str) and re.fullmatch(r"[a-f0-9]{32}", run):
                            result.add(run)
            except (ValueError, TypeError, AttributeError):
                continue
        return result

    def signal_receipts(self, references):
        """Read exact committed receipts without joining the delivery writer lock."""
        result = {'available': True, 'expected': len(references), 'sent': 0}
        if not references:
            return result
        wanted = {tuple(ref) for ref in references}
        earliest = min(ref[1] for ref in wanted)
        matched = set()
        try:
            if not self.path.is_file():
                return result
            with self._read_connect() as db:
                rows = db.execute('''SELECT p.events FROM outbox o
                    JOIN signal_payloads p ON p.outbox_id=o.id
                    WHERE o.kind='signals' AND o.status='sent' AND o.sent>=? AND o.created>=?''',
                    (earliest, earliest))
                for (payload,) in rows:
                    try:
                        events = json.loads(payload)
                    except (ValueError, TypeError):
                        continue
                    if not isinstance(events, list):
                        continue
                    for event in events:
                        try:
                            ref = tuple(signal_reference(event))
                            if ref in wanted:
                                matched.add(ref)
                        except (KeyError, TypeError):
                            continue
                    if len(matched) == len(wanted):
                        break
            result['sent'] = len(matched)
        except (OSError, sqlite3.Error):
            result.update(available=False, sent=None)
        return result

    def snapshot(self):
        try:
            result=self._snapshot()
            result['storage_available']=True
            if self._delivery_error:
                result['last_error']=self._delivery_error
            return result
        except (OSError,sqlite3.Error):
            return {**self.transport.configuration(),'storage_available':False,
                    'pending':None,'failed':None,'sent':None,'last_sent_at':None,'history':[],
                    'chart_delivery':{'pending':None,'failed':None,'sent':None},
                    'chart_generation':self.charts.snapshot() if self.charts is not None else {},
                    'last_error':'通知队列存储暂不可用，正在等待恢复；行情复核继续运行'}

    def _snapshot(self):
        from .notification_decisions import decode
        from .notification_validation import REJECTION_LABELS
        result = {**self.transport.configuration(), "pending": 0, "failed": 0, "sent": 0,
                  "last_sent_at": None, "last_error": "", "history": []}
        if not self.path.is_file():
            return result
        with self._read_connect() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM outbox WHERE kind!='chart' GROUP BY status").fetchall())
            chart_counts = dict(db.execute("SELECT status,COUNT(*) FROM outbox WHERE kind='chart' OR id IN (SELECT outbox_id FROM inline_charts) GROUP BY status").fetchall())
            result.update(pending=sum(counts.get(s, 0) for s in ("pending", "retry", "sending")),
                          failed=counts.get("failed", 0), sent=counts.get("sent", 0))
            result["last_sent_at"] = db.execute("SELECT MAX(sent) FROM outbox WHERE kind!='chart'").fetchone()[0]
            result["chart_delivery"] = {"sent": chart_counts.get("sent", 0), "failed": chart_counts.get("failed", 0),
                                         "pending": sum(chart_counts.get(s, 0) for s in ("pending", "retry", "sending"))}
            rows = db.execute("SELECT o.id,o.kind,o.status,o.created,o.sent,o.attempts,o.error,d.reason_code,d.details "
                              "FROM outbox o LEFT JOIN delivery_decisions d ON d.outbox_id=o.id "
                              "ORDER BY o.created DESC,o.rowid DESC LIMIT 30").fetchall()
            result["history"] = [{**{key: row[key] for key in row.keys() if key != 'details'},
                                  'rejection_count': len(decode(row['details'])),
                                  'decision_message': REJECTION_LABELS.get(row['reason_code'], '')} for row in rows]
            # Successful later delivery resolves an old failure banner. Keep
            # historical failures in history/counts and show active retries.
            last_sent = result["last_sent_at"] or 0
            result["last_error"] = next((row["error"] for row in rows if row["kind"] != "chart"
                                         and (row["status"] == "retry" or (
                                             row["status"] == "failed" and row["created"] >= last_sent))), "")
        result["chart_generation"] = self.charts.snapshot() if self.charts is not None else {"pending": 0, "failed": 0}
        return result

    def deliver_one(self, *, recovery_only=False):
        """Serialize one queue dispatch across processes sharing this outbox.

        Producers never take this lock. Keep it through the transport receipt
        commit so an expired SQLite claim lease cannot overlap a live sender.
        """
        if not self.path.is_file():
            return
        from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
        try:
            canonical = self.path.resolve()
            with interprocess_file_lock(canonical.with_name(canonical.name + '.dispatch.lock'),
                                        timeout_seconds=.01, poll_seconds=.01):
                self._recover_accepted_receipt()
                if not recovery_only:
                    return self._deliver_one()
        except InterprocessLockTimeout:
            return self.now() + .1

    def _recover_accepted_receipt(self):
        from . import notification_receipts as journal
        self._receipt_pending = False
        if self._accepted_receipt is not None:
            memory = self._accepted_receipt
            with self._lock, self._read_connect() as db:
                committed = journal.committed(db, memory)
                if not committed:
                    journal.check_attempt(db, memory)
            if not committed:
                journal.save(self.path, memory)
            self._accepted_receipt = None
            if committed and memory['kind'] == 'signals' and not memory['inline']:
                self._enqueue_delivered_chart(memory['id'], memory['finished'])
        receipt = journal.load(self.path)
        if receipt is None:
            return
        # Once durable, read from disk instead of retaining a stale in-memory
        # copy after a peer recovers this attempt and starts the next one.
        self._receipt_pending = True
        with self._lock, self._connect() as db:
            if not journal.committed(db, receipt):
                journal.apply(db, receipt)
        journal.clear(self.path)
        self._receipt_pending = False
        if receipt['kind'] == 'signals' and not receipt['inline']:
            self._enqueue_delivered_chart(receipt['id'], receipt['finished'])

    def _enqueue_delivered_chart(self, identity, finished):
        if self.charts is None:
            return
        try:
            with self._read_connect() as db:
                payload = db.execute('SELECT events FROM signal_payloads WHERE outbox_id=?', (identity,)).fetchone()
            self.charts.enqueue(json.loads(payload[0]) if payload else [], finished)
        except (OSError, ValueError, sqlite3.Error):
            pass  # The chart worker also recovers committed text deliveries.

    def _deliver_one(self, *, skip_waiting=False):
        """Process one receipt, or return the next attempt time when not yet due."""
        from .notification_decisions import clock_rejections, detail, record as record_decision
        from .notification_retention import maintain
        if not self.path.is_file():
            return
        stamp = self.now()
        holding_symbols = self._holding_snapshot()
        ready_only = ("AND o.error NOT IN ('等待信号有效性复核','等待图文消息生成') " if skip_waiting else "")
        with self._lock, self._connect(holding_symbols=holding_symbols) as db:
            expired = db.execute("SELECT o.id,p.events FROM outbox o LEFT JOIN signal_payloads p ON p.outbox_id=o.id "
                                 "WHERE o.status IN ('pending','retry','sending') AND o.expires<=?", (stamp,)).fetchall()
            for item in expired:
                try:
                    events = json.loads(item['events'] or '[]')
                    events = [event for event in events if isinstance(event, dict)] if isinstance(events, list) else []
                except (ValueError, TypeError):
                    events = []
                record_decision(db, item['id'], stamp, 'MARKET_EVENT_EXPIRED', clock_rejections(events, stamp))
            db.execute("UPDATE outbox SET status='expired',error='通知已过期，未补发旧信号' WHERE status IN ('pending','retry','sending') AND expires<=?", (stamp,))
            maintain(db, stamp)
            # The dispatch lock is now ours, so a remaining request marker is
            # an interrupted transport/receipt commit. Its exact finish time
            # is unknown: establish a fresh four-second recovery fence once.
            next_request = db.execute("SELECT value FROM delivery_settings WHERE key='next_request_at'").fetchone()
            next_request = next_request[0] if next_request else 0
            inflight = db.execute("SELECT value FROM delivery_settings WHERE key='request_inflight'").fetchone()
            if inflight and inflight[0]:
                next_request = max(next_request, stamp + 4)
                db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('next_request_at',?)", (next_request,))
                db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('request_inflight',0)")
            # Preserve conservative recovery for old leases which predate
            # timing records. A known current validation-only lease is not an
            # HTTP attempt and must not create a four-second send cooldown.
            previous = db.execute("SELECT MAX(o.next_try-30) FROM outbox o LEFT JOIN delivery_timings t "
                                  "ON t.outbox_id=o.id WHERE o.status='sending' AND t.outbox_id IS NULL").fetchone()[0]
            last_sent = db.execute("SELECT MAX(sent) FROM outbox").fetchone()[0]
            allowed_at = max([stamp, next_request] + [value + 4 for value in (previous, last_sent) if value is not None])
            if allowed_at > stamp:
                return allowed_at
            # Retry deadlines let unknown work yield without losing its
            # holding tier when due again. Within a tier next_try keeps old
            # uncertain receipts from repeatedly overtaking ready siblings.
            row = db.execute("SELECT o.* FROM outbox o LEFT JOIN signal_payloads p ON p.outbox_id=o.id "
                             "WHERE o.status IN ('pending','retry','sending') AND o.next_try<=? " + ready_only +
                             "ORDER BY holding_priority(p.events) DESC, "
                             "CASE WHEN o.kind='chart' THEN 1 ELSE 0 END,o.next_try,o.created,o.rowid LIMIT 1", (stamp,)).fetchone()
            if row is None:
                next_due = db.execute("SELECT MIN(next_try) FROM outbox WHERE status IN ('pending','retry','sending')").fetchone()[0]
                if next_due is not None:
                    return next_due
                return
            row = dict(row)
            row['point_policy_generation'] = self._point_policy_generation(db)
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1,next_try=? WHERE id=?", (stamp+30, row["id"]))
            # Bound diagnostics even when every queued event expires during
            # validation and no transport call reaches normal receipt cleanup.
            db.execute('DELETE FROM delivery_timings WHERE last_claimed<?', (stamp - 30 * 86400,))
            db.execute('''INSERT INTO delivery_timings(outbox_id,first_claimed,last_claimed) VALUES (?,?,?)
                ON CONFLICT(outbox_id) DO UPDATE SET last_claimed=excluded.last_claimed,
                validation_started=NULL,validation_finished=NULL,request_started=NULL,request_finished=NULL''',
                (row['id'], stamp, stamp))
            saved = db.execute("SELECT events FROM signal_payloads WHERE outbox_id=?", (row["id"],)).fetchone()
        content, pending = row["content"], None
        destination = 'others'
        if row['kind'] == 'screening':
            content = self._screening_receipt_text(content)
        try:
            events = json.loads(saved[0]) if saved else []
            if not isinstance(events,list) or any(not isinstance(item,dict) for item in events):
                events=[]
        except (ValueError,TypeError):
            events=[]
        events = self._holding_hints(events, holding_symbols)
        priority_retry = (not skip_waiting and row['error'] in ('等待信号有效性复核', '等待图文消息生成')
                          and event_priority(events, holding_symbols) > 0)
        inline = row['kind']=='signals' and len(events)==1 and events[0].get('inline_chart') is True
        dispatch_events = events
        validation_started = validation_finished = None
        if row["kind"] in {"signals", "chart"}:
            self.progress.phase('signal_validation')
            # Never acquire the monitor lock while holding the outbox lock:
            # event ingestion takes those locks in the opposite order.
            queued_events=list(events)
            events = live_signal_events(events,stamp)
            validation_started = self.now()
            validation_retry_delay = 5
            rejected=clock_rejections(queued_events,stamp)
            try:
                valid, pending = self.signal_validator(events) if events and self.signal_validator else (events, [])
                rejected.extend(getattr(valid,'rejections',[]))
                from .notification_validation import retry_delay
                validation_retry_delay = retry_delay(pending)
                valid=_retain_recorded_times(valid,events)
                pending=_retain_recorded_times(pending,events)
            except Exception:
                valid, pending = [], events
                rejected=clock_rejections(queued_events,stamp)
            stamp=self.now()
            validation_finished = stamp
            evaluated=[*valid,*pending]
            rejected.extend(clock_rejections(evaluated,stamp))
            valid=live_signal_events(valid,stamp)
            pending=live_signal_events(pending,stamp)
            if not valid:
                with self._lock, self._connect() as db:
                    self._record_validation(db, row['id'], validation_started, validation_finished)
                    if self._current_delivery(db, row) is None:
                        return
                    from .notification_validation import REJECTION_LABELS
                    from .notification_freshness import notification_rejection
                    reasons={item['reason'] for item in rejected if item.get('reason')}
                    if not events or evaluated:
                        reasons.update(filter(None,(notification_rejection(event,stamp) for event in (evaluated or queued_events))))
                    reason=(next(iter(reasons)) if len(reasons)==1 else 'MULTIPLE_RECHECK_REJECTIONS'
                            if reasons else 'RECHECK_REJECTED')
                    if rejected or not pending:
                        record_decision(db, row['id'], stamp, 'PARTIAL_RECHECK_REJECTIONS' if pending else reason, rejected)
                    db.execute("UPDATE outbox SET status=?,attempts=?,next_try=?,error=? WHERE id=? AND status='sending'",
                               ("retry" if pending else "expired", row["attempts"], stamp + validation_retry_delay,
                                "等待信号有效性复核" if pending else REJECTION_LABELS.get(reason,REJECTION_LABELS['RECHECK_REJECTED']), row["id"]))
                    if pending:
                        db.execute('UPDATE outbox SET expires=? WHERE id=?',
                                   (max(notification_deadline(e) for e in pending),row['id']))
                    db.execute("UPDATE signal_payloads SET events=? WHERE outbox_id=?",
                               (json.dumps(pending if pending else queued_events, ensure_ascii=False, allow_nan=False), row["id"]))
                    # No provider request consumed the shared send budget.
                    # Continue ready work without adding an idle four seconds.
                    next_due = db.execute("SELECT MIN(next_try) FROM outbox WHERE status IN ('pending','retry','sending')").fetchone()[0]
                if priority_retry and pending and next_due is not None and next_due <= self.now():
                    # A due holding retry gets the first fresh check. If it
                    # still cannot send, allow one ready receipt to progress
                    # in this call; never recursively drain unknown work.
                    return self._deliver_one(skip_waiting=True)
                return next_due
            # Fresh validation supplies membership, even for a legacy batch
            # or a symbol added to/removed from holdings after it was queued.
            destination = event_channel(valid[0])
            routed_pending = []
            if row['kind'] == 'signals' and not inline:
                held = [event for event in valid if event_channel(event) == 'holdings']
                ordinary = [event for event in valid if event_channel(event) == 'others']
                if held:
                    valid, routed_pending, destination = held, ordinary, 'holdings'
            if inline:
                # Validity uses current state, but text and image both describe
                # the exact frozen event which initiated this notification.
                valid=events
            dispatch_events=events if row['kind']=='chart' or inline else valid
            content = self._signal_content(valid, stamp) if row["kind"] == "signals" else content
            with self._lock, self._connect() as db:
                self._record_validation(db, row['id'], validation_started, validation_finished)
                # Validation/formatting can outlast a notification switch.
                # Check before splitting or rewriting any canceled receipt.
                if self._current_delivery(db, row) is None:
                    return
                if rejected:
                    record_decision(db, row['id'], stamp, 'PARTIAL_RECHECK_REJECTIONS', rejected)
                if routed_pending:
                    # Commit the other destination before sending this one.
                    # A restart or partial failure cannot repeat sent members.
                    routed_id = 'signals:route:' + hashlib.sha256(json.dumps(
                        [row['id'], sorted(signal_key(event) for event in routed_pending)]).encode()).hexdigest()
                    self._insert(db, routed_id, 'signals', self._signal_content(routed_pending, stamp),
                                 row['created'], max(notification_deadline(e) for e in routed_pending) - row['created'])
                    db.execute('INSERT OR IGNORE INTO signal_payloads VALUES (?,?)',
                               (routed_id, json.dumps(routed_pending, ensure_ascii=False, allow_nan=False)))
                # Unknown members get their own durable batch. Ready signals
                # need not wait, and a successful partial send is never repeated.
                if pending:
                    pending_id = "signals:" + hashlib.sha256(json.dumps(sorted(signal_key(event) for event in pending)).encode()).hexdigest()
                    self._insert(db, pending_id, "signals", self._signal_content(pending, stamp),
                                 row["created"], max(notification_deadline(e) for e in pending) - row["created"])
                    db.execute("INSERT OR IGNORE INTO signal_payloads VALUES (?,?)",
                               (pending_id, json.dumps(pending, ensure_ascii=False, allow_nan=False)))
                db.execute("UPDATE signal_payloads SET events=? WHERE outbox_id=?",
                           (json.dumps(events if row["kind"] == "chart" else valid, ensure_ascii=False, allow_nan=False), row["id"]))
                db.execute("UPDATE outbox SET content=?,expires=? WHERE id=?",
                           (content,max(notification_deadline(e) for e in dispatch_events),row["id"]))
        if inline:
            image_waiting = False
            with self._lock,self._connect() as db:
                if self._current_delivery(db, row) is None:
                    return
                chart=db.execute('SELECT url FROM inline_charts WHERE signal_key=?',(signal_key(events[0]),)).fetchone()
                if chart is None:
                    db.execute("UPDATE outbox SET status='retry',attempts=?,next_try=?,error='等待图文消息生成' WHERE id=? AND status='sending'",
                               (row['attempts'],stamp+3,row['id']))
                    image_waiting = True
                    next_due = db.execute("SELECT MIN(next_try) FROM outbox WHERE status IN ('pending','retry','sending')").fetchone()[0]
                else:
                    content=json.dumps(illustrated_signal(events[0],chart['url'],stamp),ensure_ascii=False)
                    db.execute('UPDATE outbox SET content=? WHERE id=?',(content,row['id']))
            if image_waiting:
                if priority_retry and next_due is not None and next_due <= self.now():
                    return self._deliver_one(skip_waiting=True)
                return next_due
        error = ""
        # A switch change may have retired this lease while validity checking
        # or image generation was running. Do not resurrect that delivery.
        # Producers can publish a holding notice while ordinary validation is
        # running. Re-read only the bounded membership hint, outside SQLite
        # and the queue lock, then yield before reserving an HTTP attempt.
        holding_symbols = self._holding_snapshot()
        with self._lock, self._connect(holding_symbols=holding_symbols) as db:
            if not db.in_transaction:
                db.execute('BEGIN IMMEDIATE')
            current = self._current_delivery(db, row)
            if current is None:
                return
            dispatch_stamp=self.now()
            final_events=live_signal_events(dispatch_events,dispatch_stamp)
            expired_members=clock_rejections(dispatch_events,dispatch_stamp)
            if expired_members:
                record_decision(db,row['id'],dispatch_stamp,
                                'PARTIAL_RECHECK_REJECTIONS' if final_events else 'MARKET_EVENT_EXPIRED',expired_members)
            if row['kind']=='signals' and final_events:
                duplicates=[event for event in final_events if semantic_already_sent(db,event)]
                final_events=[event for event in final_events if event not in duplicates]
                if duplicates:
                    record_decision(db,row['id'],dispatch_stamp,
                                    'PARTIAL_RECHECK_REJECTIONS' if final_events else 'DUPLICATE_SIGNAL_SUPPRESSED',
                                    [detail(event,'DUPLICATE_SIGNAL_SUPPRESSED') for event in duplicates])
                if not final_events:
                    db.execute("UPDATE outbox SET status='suppressed',error='同一结构、同一价格端点和同一状态已通知，合并重复提醒' WHERE id=?",(row['id'],))
                    return
            if (current['expires']<=dispatch_stamp
                    or row['kind'] in {'signals','chart'} and not final_events):
                db.execute("UPDATE outbox SET status='expired',error='通知已过期，未补发旧信号' WHERE id=?",
                           (row['id'],))
                return
            priority = event_priority(final_events, holding_symbols)
            higher = db.execute(
                "SELECT 1 FROM outbox o LEFT JOIN signal_payloads p ON p.outbox_id=o.id "
                "WHERE o.id!=? AND o.status IN ('pending','retry','sending') "
                "AND o.next_try<=? AND o.expires>? AND o.created>=? " + ready_only +
                "AND holding_priority(p.events)>? LIMIT 1",
                (row['id'], dispatch_stamp, dispatch_stamp, self._window_start(db), priority)).fetchone()
            if higher is not None:
                db.execute("UPDATE outbox SET status='retry',attempts=?,next_try=?,send_started=NULL,error='' "
                           "WHERE id=? AND status='sending'", (row['attempts'], dispatch_stamp, row['id']))
                return dispatch_stamp
            if row['kind']=='signals' and not inline:
                content=self._signal_content(final_events,dispatch_stamp)
                db.execute('UPDATE outbox SET content=? WHERE id=?',(content,row['id']))
                db.execute('UPDATE signal_payloads SET events=? WHERE outbox_id=?',
                           (json.dumps(final_events,ensure_ascii=False,allow_nan=False),row['id']))
            from .notification_receipts import reserve as reserve_receipt
            dispatch_identity = reserve_receipt(db)
            db.execute('UPDATE outbox SET send_started=? WHERE id=?',(dispatch_stamp,row['id']))
            db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('request_inflight',1)")
        # A contended commit can itself cross a deadline. Recheck after all
        # formatting/storage work, immediately before handing off to transport.
        dispatch_now=self.now()
        surviving=live_signal_events(final_events,dispatch_now)
        if (current['expires']<=dispatch_now
                or row['kind'] in {'signals','chart'} and len(surviving)!=len(final_events)):
            retry=bool(surviving) and current['expires']>dispatch_now
            with self._lock,self._connect() as db:
                record_decision(db,row['id'],dispatch_now,
                                'PARTIAL_RECHECK_REJECTIONS' if retry else 'MARKET_EVENT_EXPIRED',
                                clock_rejections(final_events,dispatch_now))
                db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('request_inflight',0)")
                db.execute("UPDATE outbox SET status=?,attempts=?,next_try=?,send_started=NULL,error=? "
                           "WHERE id=? AND status='sending'",
                           ('retry' if retry else 'expired',row['attempts'],dispatch_now,
                            '部分信号已过期，等待复核其余信号' if retry else '通知已过期，未补发旧信号',row['id']))
            return
        request_started = None
        try:
            self.progress.phase('provider_request')
            transport = (self.transport.for_channel(destination)
                         if isinstance(self.transport, DingTalkTransport) else self.transport)
            if row["kind"] == "chart" or inline:
                markdown = json.loads(content)
                # Retire navigation links in messages queued by an older version
                # while preserving the inline PNG source needed by the client.
                markdown['text'] = re.sub(r'\n*\[查看当前图表\]\([^\n]*\)', '', markdown['text'])
                request_started = self.now()
                transport.send_markdown(markdown["title"], markdown["text"])
            else:
                text = '\n'.join(line for line in content.splitlines()
                                 if not line.startswith(('查看当前图表：','选股工作台：')))
                request_started = self.now()
                transport.send(text)
        except DeliveryError as exc:
            error = str(exc)
            if not getattr(exc, 'request_attempted', True):
                request_started = None
        except Exception as exc:
            error = f"通知发送失败（{type(exc).__name__}）"
        finished = self.now()
        if not error:
            from .notification_dedup import semantic_key
            from .notification_receipts import save as save_receipt
            self._accepted_receipt = {
                'id': row['id'], 'kind': row['kind'], 'created': row['created'],
                'send_started': dispatch_stamp, 'request_started': request_started, 'finished': finished,
                **dispatch_identity,
                'inline': inline, 'destination': destination,
                'semantic_keys': list(dict.fromkeys(key for event in final_events
                    if row['kind'] == 'signals' and (key := semantic_key(event)))),
            }
            # Do not depend on another database read after acceptance. Its
            # writer may be precisely the unavailable part being recovered.
            save_receipt(self.path, self._accepted_receipt)
            self._accepted_receipt = None
            self._recover_accepted_receipt()
            return
        with self._lock, self._connect() as db:
            # Provider rejection and transport exceptions use the same pacing
            # as accepted receipts. Commit before releasing the dispatch lock;
            # preparation failures do not consume the HTTP interval.
            if request_started is not None:
                db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('next_request_at',?)", (finished + 4,))
            db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('request_inflight',0)")
            db.execute('UPDATE delivery_timings SET request_started=?,request_finished=? WHERE outbox_id=?',
                       (request_started, finished if request_started is not None else None, row['id']))
            attempts = row["attempts"] + 1
            db.execute("UPDATE outbox SET status=?,next_try=?,error=? WHERE id=? AND status='sending'",
                       ("failed" if attempts >= 6 else "retry", finished + min(600, 15 * 2 ** (attempts-1)), error, row["id"]))

    @staticmethod
    def _record_validation(db, identity, started, finished):
        duration = finished - started if finished >= started else None
        db.execute('''UPDATE delivery_timings SET validation_started=?,validation_finished=?,
            validation_attempts=validation_attempts+1,
            validation_seconds=CASE WHEN ? IS NULL THEN validation_seconds
                                    ELSE COALESCE(validation_seconds,0)+? END WHERE outbox_id=?''',
                   (started, finished, duration, duration, identity))

    def latency_summary(self):
        from .notification_metrics import summarize
        if not self.path.is_file():
            return {'groups':[], 'days':7, 'error':''}
        try:
            with self._read_connect() as db:
                measured = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='delivery_timings'").fetchone()
                fields = ('first_claimed', 'last_claimed', 'validation_started', 'validation_finished',
                          'validation_attempts', 'validation_seconds', 'request_started', 'request_finished')
                columns = ','.join('t.' + field if measured else 'NULL AS ' + field for field in fields)
                joined = ' LEFT JOIN delivery_timings t ON t.outbox_id=o.id' if measured else ''
                rows = db.execute('''SELECT o.kind,o.created,o.sent,o.send_started,p.events,''' + columns + '''
                    FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id''' + joined + '''
                    WHERE o.status='sent' AND o.kind IN ('signals','chart') AND o.sent>=?''',
                    (self.now()-7*86400,)).fetchall()
            return {'groups':summarize(rows),'days':7,'error':''}
        except (OSError,sqlite3.Error):
            return {'groups':[],'days':7,'error':'通知延迟统计暂不可用'}

    def start(self, enabled):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                self._wake.clear()
                wait_seconds = 4.0
                next_attempt_at = None
                try:
                    with self.progress.cycle('delivery_queue'):
                        if enabled():
                            next_attempt_at = self.deliver_one()
                            self._delivery_error=''
                        else:
                            from .notification_receipts import paths as receipt_paths
                            if (self._accepted_receipt is not None or self._receipt_pending
                                    or any(path.exists() for path in receipt_paths(self.path))):
                                self.deliver_one(recovery_only=True)
                                self._delivery_error=''
                    if next_attempt_at is not None:
                        wait_seconds = max(0.01, min(4.0, next_attempt_at - self.now()))
                except (OSError, sqlite3.Error):
                    self._delivery_error=('平台已接收消息，本地回执待保存，恢复期间不会重复发送'
                                          if self._accepted_receipt is not None or self._receipt_pending else
                                          '通知发送记录暂不可用，正在等待恢复')
                self._wake.wait(wait_seconds)
        self._thread = threading.Thread(target=loop, name="DingTalkDelivery", daemon=True)
        self._thread.start()
        if self.charts is not None:
            self.charts.start(enabled)

    def stop(self):
        if self.charts is not None:
            self.charts.stop()
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
