"""Durable, deduplicated DingTalk delivery for application events only.

Credentials stay in CHANLUN_DINGTALK_* environment variables. This sender is
independent of the Codex development hooks and never logs a webhook URL.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
import hashlib
import hmac
import json
import math
import os
import re
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from chanlun.screening.rules import CN
from .notification_text import signal_content, signal_status_lines as formatted_status_lines, heading, illustrated_signal


POINTS = {"1buy": "一买", "2buy": "二买", "3buy": "三买", "1sell": "一卖", "2sell": "二卖", "3sell": "三卖"}
STAGES = {"warning": "提前预警（未确认）", "invalidated": "预警已失效", "confirmed": "已确认", "approaching": "形成等待（未确认）", "formed": "形成等待（未确认）",
          "observed": "形成等待（未确认）", "triggered":"持仓风险触发"}


def date(value):
    return datetime.fromtimestamp(value, CN).strftime("%m-%d %H:%M:%S") if value else "—"


def _clean(value, limit=100):
    return " ".join(str(value).split())[:limit]


def signal_status_key(row):
    """Track point confirmation independently from the strategy's extra gates."""
    if row.get("signal_type") == "consolidation_divergence" and (
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
    return {signal_key(row):row for row in events
            if row.get('recorded_at',0)>=since and row.get('change') in {'discovered','changed'}
            and row.get('external_notifications',True)
            and 0<=stamp-row.get('recorded_at',0)<1800 and row.get('stage') in STAGES}


def live_signal_events(events, stamp):
    """Each queued signal keeps its own original thirty-minute deadline."""
    return [row for row in events if type(row.get('recorded_at')) in (int,float)
            and math.isfinite(row['recorded_at']) and 0<=stamp-row['recorded_at']<1800]


def _retain_recorded_times(updated, originals):
    """A refreshed quote/structure must not extend the notification lifetime."""
    sources={row['id']:row for row in originals}
    result=[]
    for row in updated:
        original=sources.get(row.get('id')) or sources.get(row.get('legacy_id'))
        if original is not None:
            result.append({**row,'recorded_at':original['recorded_at']})
    return result


def signal_status_lines(row):
    return formatted_status_lines(row)


class DeliveryError(Exception):
    """A diagnostic that contains no credentials or raw provider response."""


class DingTalkTransport:
    @staticmethod
    def configuration():
        raw = os.environ.get("CHANLUN_DINGTALK_WEBHOOK", "").strip()
        try:
            url = urlsplit(raw)
            token = dict(parse_qsl(url.query)).get("access_token")
            valid = (url.scheme == "https" and url.hostname == "oapi.dingtalk.com"
                     and url.path == "/robot/send" and url.port in (None, 443)
                     and not url.username and not url.password and not url.fragment and bool(token))
        except ValueError:
            valid = False
        return {"configured": valid, "configuration_error": "" if valid else "未配置有效的应用钉钉 Webhook",
                "keyword_configured": bool(os.environ.get("CHANLUN_DINGTALK_KEYWORD", "").strip())}

    def send(self, content):
        self._send(content)

    def send_markdown(self, title, content):
        self._send(content, title=title)

    def _send(self, content, *, title=None):
        if not self.configuration()["configured"]:
            raise DeliveryError("未配置有效的应用钉钉 Webhook")
        url = urlsplit(os.environ["CHANLUN_DINGTALK_WEBHOOK"].strip())
        query = dict(parse_qsl(url.query))
        secret = os.environ.get("CHANLUN_DINGTALK_SECRET", "").strip()
        if secret:
            stamp = str(int(time.time() * 1000))
            signature = base64.b64encode(hmac.new(secret.encode(), f"{stamp}\n{secret}".encode(), hashlib.sha256).digest()).decode()
            query.update(timestamp=stamp, sign=signature)
        address = urlunsplit((url.scheme, url.netloc, url.path, urlencode(query), ""))
        keyword = _clean(os.environ.get("CHANLUN_DINGTALK_KEYWORD", ""), 80)
        text = content
        if keyword and keyword not in content:
            text = content + "\n\n" + keyword if title is not None else keyword + " · " + content
        if len(text.encode("utf-8")) > 3800:
            raise DeliveryError("通知内容超过长度上限")
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
    def __init__(self, path, *, transport=None, now=None, signal_validator=None, charts=False):
        self.path = Path(path)
        self.transport = transport if transport is not None else DingTalkTransport()
        self.now = now or time.time
        self.signal_validator = signal_validator
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._delivery_error = ''
        from .worker_progress import WorkerProgress
        self.progress = WorkerProgress(60)
        self.charts = None
        if charts:
            from .notification_charts import NotificationCharts
            self.charts = NotificationCharts(self)

    @contextmanager
    def _connect(self, *, timeout=5):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=timeout)
        try:
            db.row_factory = sqlite3.Row
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
            CREATE TABLE IF NOT EXISTS delivery_settings (key TEXT PRIMARY KEY, value REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS inline_charts (signal_key TEXT PRIMARY KEY,url TEXT NOT NULL,created REAL NOT NULL,outbox_id TEXT NOT NULL);
            """)
            if 'send_started' not in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}:
                try:
                    db.execute('ALTER TABLE outbox ADD COLUMN send_started REAL')
                except sqlite3.OperationalError:
                    if 'send_started' not in {row[1] for row in db.execute('PRAGMA table_info(outbox)')}:
                        raise
            with db:
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
                                       (max(e['recorded_at']+1800 for e in events),identity))
                        except (ValueError,TypeError):
                            continue
                    db.execute("INSERT OR IGNORE INTO delivery_settings VALUES ('per_signal_deadlines',1)")
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
            ready = {'markers', 'outbox', 'signal_payloads', 'delivery_settings', 'inline_charts'}.issubset(tables)
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

    @staticmethod
    def _window_start(db):
        row = db.execute("SELECT value FROM delivery_settings WHERE key='window_start'").fetchone()
        return row[0] if row else 0

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
        confirmed, waiting = result.get("selected", []), result.get("observations", [])
        failures = {(e.get("market", "a"), e["code"]) for e in result.get("errors", [])}
        lines = ["缠论 Pro · 盘后选股完成", f"完成时间：{date(result.get('finished_at') or self.now())}",
                 f"检查标的：{result.get('completed', 0)} / {result.get('total', 0)}",
                 f"命中：{len({(r.get('market','a'),r['code']) for r in confirmed})} 个标的",
                 f"已确认买点：{sum(r['point']['point_type'].endswith('buy') for r in confirmed)} 个；卖点：{sum(r['point']['point_type'].endswith('sell') for r in confirmed)} 个",
                 f"数据异常：{len(failures)} 个标的", "结果摘要："]
        for row in [*confirmed, *waiting][:8]:
            point = row["point"]
            stage = point["status"]
            lines.append(f"{_clean(row.get('name', row['code']), 28)} {row.get('market','a')}:{row['code']} · "
                         f"{POINTS.get(point['point_type'], point['point_type'])} · {STAGES.get(stage, stage)}")
        if not confirmed and not waiting:
            lines.append("本轮没有可展示候选；数据异常不表示无信号。")
        if len(confirmed) + len(waiting) > 8:
            lines.append("更多候选请查看选股工作台。")
        if not result.get("source_current", True):
            lines.append("计算版本已变化，请重新选股后复核。")
        lines.append("任务：" + result["run_id"][:12])
        stamp = self.now()
        with self._lock, self._connect() as db:
            if (result.get('finished_at') or stamp) < self._window_start(db):
                return
            self._insert(db, identity, "screening", "\n".join(lines), stamp, 86400)
            # The completion summary represents the initial pool. Its existing
            # signals must not be advertised again as new intraday discoveries.
            db.executemany("INSERT OR IGNORE INTO markers VALUES (?,?)", [(signal_key(row), stamp) for row in baseline])
        self._wake.set()

    def signal_events(self, events, *, since):
        stamp = self.now()
        eligible = eligible_signal_events(events,since=since,stamp=stamp)
        if not eligible:
            return
        inline_events = []
        with self._lock, self._connect() as db:
            fresh = [(key, row) for key, row in eligible.items()
                     if row['recorded_at'] >= self._window_start(db)
                     and db.execute("SELECT 1 FROM markers WHERE id=?", (key,)).fetchone() is None]
            buckets = {}
            groups = []
            for pair in fresh:
                event = pair[1]
                if (self.charts is not None and event.get('signal_type') == 'consolidation_divergence'
                        and event.get('stage') in {'warning','confirmed','invalidated'}):
                    event = {**event,'inline_chart':True}
                    inline_events.append(event)
                    groups.append([(pair[0],event)])
                    continue
                buckets.setdefault((event.get("market"), event.get("frequency"), event.get("stage")), []).append(pair)
            for bucket in buckets.values():
                group = []
                for pair in bucket:
                    if group and (len(group) == 5 or len(self._signal_content([row for _, row in [*group, pair]], stamp).encode("utf-8")) > 3300):
                        groups.append(group); group = []
                    group.append(pair)
                if group:
                    groups.append(group)
            for group in groups:
                identity = "signals:" + hashlib.sha256(json.dumps(sorted(key for key, _ in group)).encode()).hexdigest()
                payload = [row for _, row in group]
                # Individual members are pruned on every dispatch/retry. A
                # recent signal must not inherit an older member's deadline.
                lifetime = max(row["recorded_at"] + 1800 for _, row in group) - stamp
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

    def enqueue_chart(self, event, url):
        if not live_signal_events([event],self.now()):
            return False
        if event.get('inline_chart'):
            return self._attach_inline_chart(event,url)
        identity = "chart:" + signal_key(event)
        stamp = self.now()
        content = {"title": "信号图表 · " + heading(event),
                   "text": "### " + re.sub(r"([\\`*_{}\[\]()#+.!|>])", r"\\\1", heading(event)) + "\n\n行情截止：" + date(event.get("source_closed_at"))
                            + "（北京时间）\n\n![信号快照](" + url + ")"}
        with self._lock, self._connect() as db:
            if event['recorded_at'] < self._window_start(db):
                return False
            self._insert(db, identity, "chart", json.dumps(content, ensure_ascii=False), stamp,
                         max(0, event["recorded_at"] + 1800 - stamp))
            db.execute("INSERT OR IGNORE INTO signal_payloads VALUES (?,?)", (identity, json.dumps([event], ensure_ascii=False)))
        self._wake.set()
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
            rows = db.execute("SELECT id,kind,status,created,sent,attempts,error FROM outbox ORDER BY created DESC,rowid DESC LIMIT 30").fetchall()
            result["history"] = [dict(row) for row in rows]
            # Successful later delivery resolves an old failure banner. Keep
            # historical failures in history/counts and show active retries.
            last_sent = result["last_sent_at"] or 0
            result["last_error"] = next((row["error"] for row in rows if row["kind"] != "chart"
                                         and (row["status"] == "retry" or (
                                             row["status"] == "failed" and row["created"] >= last_sent))), "")
        result["chart_generation"] = self.charts.snapshot() if self.charts is not None else {"pending": 0, "failed": 0}
        return result

    def deliver_one(self):
        """Process one receipt, or return the next attempt time when not yet due."""
        if not self.path.is_file():
            return
        stamp = self.now()
        with self._lock, self._connect() as db:
            db.execute("UPDATE outbox SET status='expired',error='通知已过期，未补发旧信号' WHERE status IN ('pending','retry','sending') AND expires<=?", (stamp,))
            # A persisted lease lets a restarted worker recover an interrupted
            # send. Keep normal traffic below the robot's per-minute budget.
            previous = db.execute("SELECT MAX(next_try-30) FROM outbox WHERE status='sending'").fetchone()[0]
            last_sent = db.execute("SELECT MAX(sent) FROM outbox").fetchone()[0]
            allowed_at = max([stamp] + [value + 4 for value in (previous, last_sent) if value is not None])
            if allowed_at > stamp:
                return allowed_at
            # A deferred validity check must yield to work already waiting.
            # Sorting only by creation time lets two old uncertain groups
            # alternate retries forever ahead of a later ready signal.
            row = db.execute("SELECT * FROM outbox WHERE status IN ('pending','retry','sending') AND next_try<=? ORDER BY CASE WHEN kind='chart' THEN 1 ELSE 0 END,next_try,created,rowid LIMIT 1", (stamp,)).fetchone()
            if row is None:
                next_due = db.execute("SELECT MIN(next_try) FROM outbox WHERE status IN ('pending','retry','sending')").fetchone()[0]
                if next_due is not None:
                    return next_due
                return
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1,next_try=? WHERE id=?", (stamp+30, row["id"]))
            saved = db.execute("SELECT events FROM signal_payloads WHERE outbox_id=?", (row["id"],)).fetchone()
        content, pending = row["content"], None
        try:
            events = json.loads(saved[0]) if saved else []
            if not isinstance(events,list) or any(not isinstance(item,dict) for item in events):
                events=[]
        except (ValueError,TypeError):
            events=[]
        inline = row['kind']=='signals' and len(events)==1 and events[0].get('inline_chart') is True
        dispatch_events = events
        if row["kind"] in {"signals", "chart"}:
            self.progress.phase('signal_validation')
            # Never acquire the monitor lock while holding the outbox lock:
            # event ingestion takes those locks in the opposite order.
            events = live_signal_events(events,stamp)
            try:
                valid, pending = self.signal_validator(events) if events and self.signal_validator else (events, [])
                valid=_retain_recorded_times(valid,events)
                pending=_retain_recorded_times(pending,events)
            except Exception:
                valid, pending = [], events
            stamp=self.now()
            valid=live_signal_events(valid,stamp)
            pending=live_signal_events(pending,stamp)
            if not valid:
                with self._lock, self._connect() as db:
                    if self._current_delivery(db, row) is None:
                        return
                    db.execute("UPDATE outbox SET status=?,attempts=?,next_try=?,error=? WHERE id=? AND status='sending'",
                               ("retry" if pending else "expired", row["attempts"], stamp + 5,
                                "等待信号有效性复核" if pending else "信号已过期、退出或缺少可核验身份，未补发", row["id"]))
                    if pending:
                        db.execute('UPDATE outbox SET expires=? WHERE id=?',
                                   (max(e['recorded_at']+1800 for e in pending),row['id']))
                    db.execute("UPDATE signal_payloads SET events=? WHERE outbox_id=?",
                               (json.dumps(pending, ensure_ascii=False, allow_nan=False), row["id"]))
                return
            if inline:
                # Validity uses current state, but text and image both describe
                # the exact frozen event which initiated this notification.
                valid=events
            dispatch_events=events if row['kind']=='chart' or inline else valid
            content = self._signal_content(valid, stamp) if row["kind"] == "signals" else content
            with self._lock, self._connect() as db:
                # Validation/formatting can outlast a notification switch.
                # Check before splitting or rewriting any canceled receipt.
                if self._current_delivery(db, row) is None:
                    return
                # Unknown members get their own durable batch. Ready signals
                # need not wait, and a successful partial send is never repeated.
                if pending:
                    pending_id = "signals:" + hashlib.sha256(json.dumps(sorted(signal_key(event) for event in pending)).encode()).hexdigest()
                    self._insert(db, pending_id, "signals", self._signal_content(pending, stamp),
                                 row["created"], max(e['recorded_at']+1800 for e in pending) - row["created"])
                    db.execute("INSERT OR IGNORE INTO signal_payloads VALUES (?,?)",
                               (pending_id, json.dumps(pending, ensure_ascii=False, allow_nan=False)))
                db.execute("UPDATE signal_payloads SET events=? WHERE outbox_id=?",
                           (json.dumps(events if row["kind"] == "chart" else valid, ensure_ascii=False, allow_nan=False), row["id"]))
                db.execute("UPDATE outbox SET content=?,expires=? WHERE id=?",
                           (content,max(e['recorded_at']+1800 for e in dispatch_events),row["id"]))
        if inline:
            with self._lock,self._connect() as db:
                if self._current_delivery(db, row) is None:
                    return
                chart=db.execute('SELECT url FROM inline_charts WHERE signal_key=?',(signal_key(events[0]),)).fetchone()
                if chart is None:
                    db.execute("UPDATE outbox SET status='retry',attempts=?,next_try=?,error='等待图文消息生成' WHERE id=? AND status='sending'",
                               (row['attempts'],stamp+3,row['id']))
                    return
                content=json.dumps(illustrated_signal(events[0],chart['url'],stamp),ensure_ascii=False)
                db.execute('UPDATE outbox SET content=? WHERE id=?',(content,row['id']))
        error = ""
        # A switch change may have retired this lease while validity checking
        # or image generation was running. Do not resurrect that delivery.
        with self._lock, self._connect() as db:
            current = self._current_delivery(db, row)
            if current is None:
                return
            dispatch_stamp=self.now()
            final_events=live_signal_events(dispatch_events,dispatch_stamp)
            if (current['expires']<=dispatch_stamp
                    or row['kind'] in {'signals','chart'} and not final_events):
                db.execute("UPDATE outbox SET status='expired',error='通知已过期，未补发旧信号' WHERE id=?",
                           (row['id'],))
                return
            if row['kind']=='signals' and not inline:
                content=self._signal_content(final_events,dispatch_stamp)
                db.execute('UPDATE outbox SET content=? WHERE id=?',(content,row['id']))
                db.execute('UPDATE signal_payloads SET events=? WHERE outbox_id=?',
                           (json.dumps(final_events,ensure_ascii=False,allow_nan=False),row['id']))
            db.execute('UPDATE outbox SET send_started=? WHERE id=?',(dispatch_stamp,row['id']))
        # A contended commit can itself cross a deadline. Recheck after all
        # formatting/storage work, immediately before handing off to transport.
        dispatch_now=self.now()
        surviving=live_signal_events(final_events,dispatch_now)
        if (current['expires']<=dispatch_now
                or row['kind'] in {'signals','chart'} and len(surviving)!=len(final_events)):
            retry=bool(surviving) and current['expires']>dispatch_now
            with self._lock,self._connect() as db:
                db.execute("UPDATE outbox SET status=?,attempts=?,next_try=?,send_started=NULL,error=? "
                           "WHERE id=? AND status='sending'",
                           ('retry' if retry else 'expired',row['attempts'],dispatch_now,
                            '部分信号已过期，等待复核其余信号' if retry else '通知已过期，未补发旧信号',row['id']))
            return
        try:
            self.progress.phase('provider_request')
            if row["kind"] == "chart" or inline:
                markdown = json.loads(content)
                # Retire navigation links in messages queued by an older version
                # while preserving the inline PNG source needed by the client.
                markdown['text'] = re.sub(r'\n*\[查看当前图表\]\([^\n]*\)', '', markdown['text'])
                self.transport.send_markdown(markdown["title"], markdown["text"])
            else:
                self.transport.send('\n'.join(line for line in content.splitlines()
                                              if not line.startswith(('查看当前图表：','选股工作台：'))))
        except DeliveryError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"通知发送失败（{type(exc).__name__}）"
        finished = self.now()
        with self._lock, self._connect() as db:
            if error:
                attempts = row["attempts"] + 1
                db.execute("UPDATE outbox SET status=?,next_try=?,error=? WHERE id=? AND status='sending'",
                           ("failed" if attempts >= 6 else "retry", finished + min(600, 15 * 2 ** (attempts-1)), error, row["id"]))
            else:
                # A switch cannot recall an HTTP request already sent. Preserve
                # its successful provider receipt without restoring queued work.
                db.execute("UPDATE outbox SET status='sent',sent=?,error=CASE WHEN status='expired' "
                           "THEN '关闭前请求已发出，平台已接收' ELSE '' END WHERE id=?", (finished, row["id"]))
            db.execute("DELETE FROM outbox WHERE created<? AND status IN ('sent','failed','expired')", (finished-30*86400,))
            db.execute("DELETE FROM signal_payloads WHERE outbox_id NOT IN (SELECT id FROM outbox)")
            db.execute("DELETE FROM markers WHERE created<?", (finished-90*86400,))
            db.execute('DELETE FROM inline_charts WHERE outbox_id NOT IN (SELECT id FROM outbox)')
        if not error and row["kind"] == "signals" and not inline and self.charts is not None:
            try:
                with self._read_connect() as db:
                    payload = db.execute("SELECT events FROM signal_payloads WHERE outbox_id=?", (row["id"],)).fetchone()
                self.charts.enqueue(json.loads(payload[0]) if payload else [], finished)
            except (OSError, ValueError, sqlite3.Error):
                pass  # The chart worker recovers committed text deliveries after a restart.

    def latency_summary(self):
        from .notification_metrics import summarize
        if not self.path.is_file():
            return {'groups':[], 'days':7, 'error':''}
        try:
            with self._read_connect() as db:
                rows = db.execute('''SELECT o.kind,o.created,o.sent,o.send_started,p.events
                    FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id
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
                    if next_attempt_at is not None:
                        wait_seconds = max(0.01, min(4.0, next_attempt_at - self.now()))
                except (OSError, sqlite3.Error):
                    self._delivery_error='通知发送记录暂不可用，正在等待恢复'
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
