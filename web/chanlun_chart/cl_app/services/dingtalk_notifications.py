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
import os
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests

from chanlun.screening.rules import CN


POINTS = {"1buy": "一买", "2buy": "二买", "3buy": "三买", "1sell": "一卖", "2sell": "二卖", "3sell": "三卖"}
STAGES = {"confirmed": "已确认", "approaching": "形成等待（未确认）", "formed": "形成等待（未确认）",
          "observed": "形成等待（未确认）"}


def date(value):
    return datetime.fromtimestamp(value, CN).strftime("%m-%d %H:%M:%S") if value else "—"


def _clean(value, limit=100):
    return " ".join(str(value).split())[:limit]


def signal_key(row):
    return f"signal:{row['id']}:{row['stage']}"


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
        text = (keyword + "\n" if keyword else "") + content
        if len(text.encode("utf-8")) > 3800:
            raise DeliveryError("通知内容超过长度上限")
        payload = {"msgtype": "text", "text": {"content": text}, "at": {"isAtAll": False}}
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
    def __init__(self, path, *, transport=None, now=None):
        self.path = Path(path)
        self.transport = transport if transport is not None else DingTalkTransport()
        self.now = now or time.time
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS markers (id TEXT PRIMARY KEY, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, content TEXT NOT NULL,
                status TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                sent REAL, error TEXT NOT NULL DEFAULT ''
            );
        """)
        try:
            with db:
                yield db
        finally:
            db.close()

    def has(self, identity):
        if not self.path.is_file():
            return False
        with self._connect() as db:
            return db.execute("SELECT 1 FROM markers WHERE id=?", (identity,)).fetchone() is not None

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
                 f"已确认：{len(confirmed)} 个信号 / {len({(r.get('market','a'),r['code']) for r in confirmed})} 个标的",
                 f"形成等待（未确认）：{len(waiting)} 个信号",
                 f"数据异常：{len(failures)} 个标的", "结果摘要："]
        for row in [*confirmed, *waiting][:8]:
            point = row["point"]
            stage = row.get("selection_status", point["status"])
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
            self._insert(db, identity, "screening", "\n".join(lines), stamp, 86400)
            # The completion summary represents the initial pool. Its existing
            # signals must not be advertised again as new intraday discoveries.
            db.executemany("INSERT OR IGNORE INTO markers VALUES (?,?)", [(signal_key(row), stamp) for row in baseline])
        self._wake.set()

    def signal_events(self, events, *, since):
        stamp = self.now()
        eligible = {signal_key(row): row for row in events
                    if row.get("recorded_at", 0) >= since and row.get("change") in {"discovered", "changed"}
                    and 0 <= stamp - row.get("recorded_at", 0) < 1800
                    and row.get("stage") in STAGES}
        if not eligible:
            return
        with self._lock, self._connect() as db:
            fresh = [(key, row) for key, row in eligible.items()
                     if db.execute("SELECT 1 FROM markers WHERE id=?", (key,)).fetchone() is None]
            for offset in range(0, len(fresh), 5):
                group = fresh[offset:offset + 5]
                identity = "signals:" + hashlib.sha256(json.dumps(sorted(key for key, _ in group)).encode()).hexdigest()
                lines = ["缠论 Pro · 监听信号变化", f"检查时间：{date(stamp)}"]
                for _, row in group:
                    lines += ["", f"{_clean(row.get('name',row['code']),28)} · {row.get('market','a')}:{_clean(row['code'],80)}",
                              f"{row['frequency']} {POINTS.get(row['point_type'],row['point_type'])} · {STAGES[row['stage']]}",
                              f"拐点价：{row.get('anchor_price') if row.get('anchor_price') is not None else '—'}",
                              f"信号可用：{date(row.get('available_at'))}；行情截止：{date(row.get('source_closed_at'))}"]
                lifetime = min(row["recorded_at"] + 1800 for _, row in group) - stamp
                self._insert(db, identity, "signals", "\n".join(lines), stamp, lifetime)
                db.executemany("INSERT OR IGNORE INTO markers VALUES (?,?)", [(key, stamp) for key, _ in group])
        self._wake.set()

    def snapshot(self):
        result = {**self.transport.configuration(), "pending": 0, "failed": 0, "sent": 0,
                  "last_sent_at": None, "last_error": "", "history": []}
        if not self.path.is_file():
            return result
        with self._connect() as db:
            counts = dict(db.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status").fetchall())
            result.update(pending=sum(counts.get(s, 0) for s in ("pending", "retry", "sending")),
                          failed=counts.get("failed", 0), sent=counts.get("sent", 0))
            result["last_sent_at"] = db.execute("SELECT MAX(sent) FROM outbox").fetchone()[0]
            rows = db.execute("SELECT id,kind,status,created,sent,attempts,error FROM outbox ORDER BY created DESC,rowid DESC LIMIT 30").fetchall()
            result["history"] = [dict(row) for row in rows]
            result["last_error"] = next((row["error"] for row in rows if row["status"] in {"retry", "failed"}), "")
        return result

    def deliver_one(self):
        if not self.path.is_file():
            return
        stamp = self.now()
        with self._lock, self._connect() as db:
            db.execute("UPDATE outbox SET status='expired',error='通知已过期，未补发旧信号' WHERE status IN ('pending','retry','sending') AND expires<=?", (stamp,))
            # A persisted lease lets a restarted worker recover an interrupted
            # send. Keep normal traffic below the robot's per-minute budget.
            previous = db.execute("SELECT MAX(next_try-30) FROM outbox WHERE status='sending'").fetchone()[0]
            last_sent = db.execute("SELECT MAX(sent) FROM outbox").fetchone()[0]
            if (previous is not None and stamp-previous < 4) or (last_sent is not None and stamp-last_sent < 4):
                return
            row = db.execute("SELECT * FROM outbox WHERE status IN ('pending','retry','sending') AND next_try<=? ORDER BY created,rowid LIMIT 1", (stamp,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE outbox SET status='sending',attempts=attempts+1,next_try=? WHERE id=?", (stamp+30, row["id"]))
        error = ""
        try:
            self.transport.send(row["content"])
        except DeliveryError as exc:
            error = str(exc)
        except Exception as exc:
            error = f"通知发送失败（{type(exc).__name__}）"
        finished = self.now()
        with self._lock, self._connect() as db:
            if error:
                attempts = row["attempts"] + 1
                db.execute("UPDATE outbox SET status=?,next_try=?,error=? WHERE id=?",
                           ("failed" if attempts >= 6 else "retry", finished + min(600, 15 * 2 ** (attempts-1)), error, row["id"]))
            else:
                db.execute("UPDATE outbox SET status='sent',sent=?,error='' WHERE id=?", (finished, row["id"]))
            db.execute("DELETE FROM outbox WHERE created<? AND status IN ('sent','failed','expired')", (finished-30*86400,))
            db.execute("DELETE FROM markers WHERE created<?", (finished-90*86400,))

    def start(self, enabled):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        def loop():
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    if enabled():
                        self.deliver_one()
                except (OSError, sqlite3.Error):
                    pass  # Retry local transient errors; never log credentials.
                self._wake.wait(4)
        self._thread = threading.Thread(target=loop, name="DingTalkDelivery", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
