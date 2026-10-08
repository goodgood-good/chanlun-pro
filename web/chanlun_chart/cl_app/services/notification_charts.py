"""Asynchronous, frozen-evidence charts with expiring capability URLs."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from decimal import Decimal

from .notification_text import public_base


class _ChartExpired(Exception):
    pass


class ChartStore:
    lifetime = 7 * 86400

    def __init__(self, root, *, now=None):
        self.root = Path(root)
        self.now = now or time.time

    def _secret(self, create=False):
        path = self.root / ".signing-key"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
        value = path.read_bytes()
        if len(value) != 32:
            raise ValueError("chart signing key unavailable")
        return value

    def publish(self, png):
        if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > 8 * 1024 * 1024:
            raise ValueError("invalid chart image")
        key = self._secret(create=True)
        name = hashlib.sha256(png).hexdigest()
        target = self.root / (name + ".png")
        if not target.exists():
            temporary = self.root / (name + "." + secrets.token_hex(6) + ".tmp")
            try:
                temporary.write_bytes(png)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        os.utime(target, (self.now(), self.now()))
        expires = int(self.now()) + self.lifetime
        signature = hmac.new(key, f"{name}:{expires}".encode(), hashlib.sha256).hexdigest()
        return f"{public_base()}/public/alert-chart/{name}.png?expires={expires}&signature={signature}"

    def resolve(self, name, expires, signature):
        try:
            if not re.fullmatch(r"[a-f0-9]{64}", name) or not re.fullmatch(r"[a-f0-9]{64}", signature or ""):
                return None
            stamp = int(expires)
            if not self.now() < stamp <= self.now() + self.lifetime + 60:
                return None
            expected = hmac.new(self._secret(), f"{name}:{stamp}".encode(), hashlib.sha256).hexdigest()
            path = (self.root / (name + ".png")).resolve()
            if not hmac.compare_digest(expected, signature) or path.parent != self.root.resolve() or not path.is_file():
                return None
            return path
        except (OSError, ValueError, TypeError):
            return None

    def cleanup(self):
        if self.root.is_dir():
            for path in self.root.glob("*.png"):
                if path.resolve().parent == self.root.resolve() and self.now() - path.stat().st_mtime > self.lifetime + 86400:
                    path.unlink(missing_ok=True)


def render_signal_chart(event, runs_root):
    """Export the real TradingView renderer using verified, frozen evidence."""
    from .tradingview_snapshot import capture, prepare
    return capture(prepare(event, runs_root))


def load_chart_evidence(event, runs_root):
    """Reject a stale or mismatched event before opening a browser."""
    run_id = event.get("evidence_run_id", "")
    if not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("chart evidence run is missing")
    from chanlun.screening.evidence import load_evidence
    frame, snapshot, health = load_evidence(Path(runs_root) / run_id, event["code"], event["frequency"], event["evidence"])
    if not health["verified"] or snapshot.get("analysis_scope") != "native_centers":
        raise ValueError("chart requires verified native evidence")
    if snapshot["source_closed_at"] != event["source_closed_at"]:
        raise ValueError("chart and delivered text have different cutoffs")
    if event.get('signal_type') == 'holding_risk':
        if snapshot['price_basis_revision'] != event.get('price_basis_revision'):
            raise ValueError('risk chart price basis changed')
        if not event['bound_cutoff'] < event['available_at'] <= event['source_closed_at']:
            raise ValueError('risk chart trigger is outside its bound window')
        if event['point_type'] == 'risk_stop':
            candle = frame.loc[frame.date.map(lambda t:int(t.timestamp())) == event['available_at']]
            stop = Decimal(event['stop_loss']['price_tick']) * Decimal(event['structure_price_quantum'])
            if (len(candle) != 1 or float(candle.low.iloc[0]) != event['trigger_price']
                    or Decimal(str(event['trigger_price'])) >= stop or float(stop) != event['stop_price']):
                raise ValueError('risk chart stop does not match frozen evidence')
            return frame,snapshot,{'anchor_at':event['available_at'],'anchor_price':event['trigger_price']}
        points = [point for level in snapshot['levels'] for point in level.get('points',[])
                  if point['point_id'] == event.get('reverse_point_id') and point.get('side') == 'sell'
                  and point.get('status') == 'confirmed' and point['available_at'] == event['available_at']
                  and point['point_type'] == event.get('reverse_point_type')
                  and point['anchor_price'] == event.get('reverse_price')]
        if len(points) != 1:
            raise ValueError('risk chart reversal does not match frozen evidence')
        return frame,snapshot,points[0]
    consolidation = event.get("signal_type") == "consolidation_divergence"
    if consolidation and event.get('stage') == 'invalidated':
        from .signal_monitor import _invalidation_evidence
        reference=event.get('invalidation_reference')
        if (not isinstance(reference,dict) or reference.get('stage') != 'warning'
                or reference.get('signal_type') != 'consolidation_divergence'
                or any(reference.get(k)!=event.get(k) for k in ('id','hypothesis_id','code','market','frequency','anchor_at','anchor_price'))
                or reference.get('source_closed_at',0)>snapshot['source_closed_at']):
            raise ValueError('invalidation chart reference is missing or mismatched')
        load_chart_evidence(reference,runs_root)
        cutoff=snapshot['source_closed_at']
        checks=snapshot.get('native_consolidation_checks',{})
        if checks.get('source_closed_at')!=cutoff or event.get('invalidation_checked_at')!=cutoff:
            raise ValueError('invalidation chart check has another cutoff')
        verified=_invalidation_evidence(reference,{'source_closed_at':cutoff,'latest_close':float(frame.close.iloc[-1]),
                                                 'consolidation_checks':checks})
        if any(event.get(k)!=verified[k] for k in ('invalidation_reason_code','invalidation_reason')):
            raise ValueError('invalidation chart reason disagrees with evidence')
        # The marker denotes the invalidation check, not a still-active preview.
        return frame,snapshot,{'anchor_at':cutoff,'anchor_price':float(frame.close.iloc[-1]),
                              'reference_anchor_at':reference['anchor_at'],'reference_anchor_price':reference['anchor_price'],
                              'reference_basis_current':reference.get('price_basis_revision')==snapshot['price_basis_revision']}
    if consolidation:
        matches = [a for a in snapshot.get("native_consolidation_alerts", [])
                   if a.get("hypothesis_id") == event.get("hypothesis_id") and a.get("status") == event.get("stage")]
    else:
        matches = [p for level in snapshot["levels"] for p in level.get("points", [])
                   if p.get("point_id") == event.get("point_id") and p.get("status") == "confirmed"]
    if len(matches) != 1:
        raise ValueError("chart signal does not match frozen evidence")
    signal = matches[0]
    expected_type = "consolidation_" + signal["direction"] if consolidation else signal["point_type"]
    anchor_price = float(signal["anchor_tick"]) * float(snapshot["structure_price_quantum"])
    if (event["point_type"] != expected_type
            or event.get("anchor_at") not in (None, signal["anchor_at"])
            or abs(float(event["anchor_price"]) - anchor_price) > float(snapshot["structure_price_quantum"]) * 1e-6):
        raise ValueError("chart marker disagrees with notification")
    return frame, snapshot, signal


class NotificationCharts:
    def __init__(self, outbox, *, renderer=None, check_public=True):
        self.outbox = outbox
        self.root = outbox.path.parent
        self.store = ChartStore(self.root / "chart_images", now=outbox.now)
        self.renderer = renderer or render_signal_chart
        self.check_public = check_public
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self.created_at = outbox.now()
        self._runtime_error = ''
        from .notification_evidence import NotificationEvidence
        self.archive=NotificationEvidence(self.root/'notification_evidence',now=outbox.now)
        self._archive_error=''
        self._archive_cleanup_error=''
        from .worker_progress import WorkerProgress
        self.progress = WorkerProgress(180)

    @contextmanager
    def _db(self, *, timeout=5):
        self.root.mkdir(parents=True,exist_ok=True)
        db = sqlite3.connect(self.root / "chart_jobs.sqlite3",timeout=timeout)
        try:
            db.row_factory=sqlite3.Row
            db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,event TEXT,status TEXT,created REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,error TEXT DEFAULT '',updated REAL)")
            if 'updated' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                try:
                    db.execute('ALTER TABLE jobs ADD COLUMN updated REAL')
                except sqlite3.OperationalError:
                    if 'updated' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                        raise
            db.execute("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v REAL)")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('enabled_at',?)",(self.created_at,))
            db.execute("CREATE TABLE IF NOT EXISTS evidence_archive_jobs(id TEXT PRIMARY KEY,status TEXT,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,error TEXT DEFAULT '')")
            db.execute("INSERT OR IGNORE INTO settings VALUES ('archive_enabled_at',?)",(self.created_at,))
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _read_db(self):
        def connect():
            path=(self.root/'chart_jobs.sqlite3').resolve()
            db=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=0.1)
            db.row_factory=sqlite3.Row
            return db

        db=connect()
        try:
            db.execute('BEGIN')
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ready={'jobs','settings','evidence_archive_jobs'}.issubset(tables)
            ready=ready and 'updated' in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            ready=ready and db.execute("SELECT COUNT(*) FROM settings WHERE k IN ('enabled_at','archive_enabled_at')").fetchone()[0]==2
            if not ready:
                # Preserve one-time legacy upgrades without giving routine
                # status reads write access or a long lock wait.
                db.close()
                db=None
                with self._db(timeout=0.1):
                    pass
                db=connect()
                db.execute('BEGIN')
            yield db
        finally:
            if db is not None:
                db.close()

    def enqueue(self, events, sent_at):
        from .dingtalk_notifications import signal_key
        if not public_base():
            for event in events:
                self.outbox.finish_inline_chart(event,'failed','未配置可访问的图表图片地址')
            return
        inserted = False
        with self._db() as db:
            enabled_at=db.execute("SELECT v FROM settings WHERE k='enabled_at'").fetchone()[0]
            if sent_at < enabled_at and not any(e.get('inline_chart') for e in events):
                return
            for event in events:
                if event.get('inline_chart') and (not event.get('evidence') or not event.get('evidence_run_id')):
                    self.outbox.finish_inline_chart(event,'failed','缺少可核验的图表证据')
                    continue
                if (not (event.get("stage") in {"warning","confirmed"}
                         or event.get('stage') == 'invalidated' and event.get('signal_type') == 'consolidation_divergence' and event.get('inline_chart')
                         or event.get('stage') == 'triggered' and event.get('signal_type') == 'holding_risk')
                        or event.get('recorded_at', 0) < enabled_at and not event.get('inline_chart')
                        or not event.get("evidence") or not event.get("evidence_run_id")):
                    continue
                inserted |= db.execute("INSERT OR IGNORE INTO jobs(id,event,status,created) VALUES (?,?,'pending',?)",
                                       (signal_key(event),json.dumps(event,ensure_ascii=False),sent_at)).rowcount > 0
                job=db.execute('SELECT status,error FROM jobs WHERE id=?',(signal_key(event),)).fetchone()
                if job and job['status'] in {'failed','expired'}:
                    # A crash or temporary outbox failure after a terminal job
                    # write must not leave its combined message waiting forever.
                    self.outbox.finish_inline_chart(event,job['status'],job['error'])
        if inserted:
            self._wake.set()

    def begin_window(self, since):
        with self._db() as db:
            db.execute("UPDATE settings SET v=? WHERE k='enabled_at'", (since,))
            db.execute("UPDATE jobs SET status='expired',error='通知开关已更新，旧图表不再补发' "
                       "WHERE status IN ('pending','running') AND created<?", (since,))

    def recover(self):
        if not self.outbox.path.exists():
            return
        with self.outbox._read_connect() as db:
            rows=db.execute("SELECT o.id,p.events,o.sent,o.created,o.status FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id "
                            "WHERE o.kind='signals' AND o.status IN ('sent','pending','retry','sending') AND o.created>=?",
                            (self.outbox.now()-1800,)).fetchall()
        for identity,payload,sent,created,status in rows:
            damaged=False
            try:
                records=json.loads(payload)
                if not isinstance(records,list):
                    raise ValueError('invalid notification payload')
                stamp=sent if status=='sent' else created
                if type(stamp) not in (int,float) or not math.isfinite(stamp):
                    raise ValueError('invalid notification timestamp')
            except (ValueError,TypeError,KeyError):
                self._record_recovery_failure(identity,payload,created)
                continue
            events=[]
            for event in records:
                try:
                    if not isinstance(event,dict):
                        raise ValueError('invalid event record')
                    if bool(event.get('inline_chart')) != (status!='sent'):
                        continue
                    self._validate_job_event(event)
                    events.append(event)
                except (ValueError,TypeError,KeyError):
                    damaged=True
            if damaged:
                self._record_recovery_failure(identity,payload,created)
                if status!='sent':
                    continue
            self.enqueue(events,stamp)

    @staticmethod
    def _validate_job_event(event, identity=None):
        from .dingtalk_notifications import signal_key
        if (not isinstance(event,dict) or not isinstance(event.get('id'),str) or not event['id']
                or not isinstance(event.get('stage'),str)
                or type(event.get('recorded_at')) not in (int,float)
                or not math.isfinite(event['recorded_at'])):
            raise ValueError('invalid chart event')
        key=signal_key(event)
        if identity is not None and key!=identity:
            raise ValueError('chart event identity mismatch')

    def _record_recovery_failure(self,identity,payload,created):
        error='图表恢复记录损坏，该记录已隔离'
        failure_id='recovery:'+hashlib.sha256((identity+str(payload)).encode()).hexdigest()
        with self._db() as db:
            db.execute("INSERT OR IGNORE INTO jobs(id,event,status,created,error,updated) VALUES (?,?,'failed',?,?,?)",
                       (failure_id,json.dumps({'outbox_id':identity}),created,error,self.outbox.now()))
        with self.outbox._lock,self.outbox._connect() as db:
            # Preserve actual text receipts; cancel only unsent malformed work.
            # A concurrent validator may have replaced the payload meanwhile.
            db.execute("UPDATE outbox SET status=CASE WHEN status='sent' THEN status ELSE 'failed' END,error=? "
                       "WHERE id=? AND status IN ('sent','pending','retry','sending') AND EXISTS "
                       "(SELECT 1 FROM signal_payloads p WHERE p.outbox_id=outbox.id AND p.events=?)",
                       (error,identity,payload))

    def process_one(self):
        now=self.outbox.now()
        with self._db() as db:
            row=db.execute("SELECT * FROM jobs WHERE status IN ('pending','running') AND next_try<=? ORDER BY next_try,created,rowid LIMIT 1",(now,)).fetchone()
            if row is None:
                return
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,next_try=? WHERE id=?",(now+60,row['id']))
        try:
            event=json.loads(row['event'])
            self._validate_job_event(event,row['id'])
        except (ValueError,TypeError,KeyError):
            with self._db() as db:
                db.execute("UPDATE jobs SET status='failed',error='图表任务数据损坏',updated=? WHERE id=?",(self.outbox.now(),row['id']))
            return

        def check_deadline():
            if not 0<=self.outbox.now()-event['recorded_at']<1800:
                raise _ChartExpired()

        if now-event.get('recorded_at',0)>=1800:
            status,error='expired','图表任务已过期'
        else:
            phase='信号复核'
            try:
                self.progress.phase('signal_validation')
                if self.outbox.signal_validator:
                    valid,pending=self.outbox.signal_validator([event])
                    check_deadline()
                    if pending:
                        with self._db() as db:
                            db.execute("UPDATE jobs SET status='pending',attempts=?,next_try=?,error='等待信号有效性复核' WHERE id=? AND status='running'",
                                       (row['attempts'],now+5,row['id']))
                        return
                    if not valid:
                        with self._db() as db:
                            db.execute("UPDATE jobs SET status='expired',error='信号已更新',updated=? WHERE id=?",(self.outbox.now(),row['id']))
                        self.outbox.finish_inline_chart(event,'expired','信号已更新，图文消息已撤销')
                        return
                check_deadline()
                phase='冻结证据绘图'
                self.progress.phase('chart_render')
                png=self.renderer(event,self.root/'runs')
                check_deadline()
                phase='图片发布'
                self.progress.phase('image_publish')
                url=self.store.publish(png)
                if self.check_public:
                    phase='公网访问校验'
                    self.progress.phase('public_image_check')
                    import requests
                    response=requests.get(url,timeout=(2,4),allow_redirects=False)
                    if response.status_code!=200 or hashlib.sha256(response.content).digest()!=hashlib.sha256(png).digest():
                        raise ValueError('public-chart-unavailable')
                check_deadline()
                phase='图片入队'
                self.progress.phase('image_enqueue')
                accepted = self.outbox.enqueue_chart(event,url)
                status,error=('ready','') if accepted else ('expired','通知已过期或开关已更新')
            except _ChartExpired:
                status,error='expired','图表任务已过期'
            except Exception as exc:
                status='failed' if row['attempts']>=2 else 'pending'
                error=phase+'失败（'+type(exc).__name__+'）'
        with self._db() as db:
            finished=self.outbox.now()
            db.execute("UPDATE jobs SET status=?,error=?,next_try=?,updated=? WHERE id=? AND status='running'",(status,error,finished+30,finished,row['id']))
        self.outbox.finish_inline_chart(event,status,error)

    def cleanup(self):
        """Expire historical diagnostics even when no new charts are generated."""
        self.store.cleanup()
        if (self.root/'chart_jobs.sqlite3').exists():
            with self._db() as db:
                db.execute("DELETE FROM jobs WHERE status IN ('ready','failed','expired') AND COALESCE(updated,created)<?",
                           (self.outbox.now()-8*86400,))
                db.execute('DELETE FROM evidence_archive_jobs WHERE id NOT IN (SELECT id FROM jobs)')
        try:self.archive.cleanup()
        except (OSError,ValueError,TypeError,KeyError):
            self._archive_cleanup_error='通知证据清理暂不可用'
        else:self._archive_cleanup_error=''

    def archive_one(self):
        now=self.outbox.now()
        with self._db() as db:
            row=db.execute("SELECT j.*,COALESCE(a.attempts,0) archive_attempts FROM jobs j "
                "LEFT JOIN evidence_archive_jobs a ON a.id=j.id WHERE j.id NOT LIKE 'recovery:%' "
                "AND j.created>=(SELECT v FROM settings WHERE k='archive_enabled_at') "
                "AND COALESCE(a.status,'retry')='retry' AND COALESCE(a.next_try,0)<=? "
                "ORDER BY COALESCE(a.next_try,0),j.created,j.rowid LIMIT 1",(now,)).fetchone()
            if row is None:
                self._archive_error=''
                return
            attempts=row['archive_attempts']+1
            db.execute("INSERT OR REPLACE INTO evidence_archive_jobs VALUES (?,'retry',?,?,'')",
                       (row['id'],attempts,now+60))
        try:
            if now>=row['created']+self.archive.lifetime:
                status,error='expired','证据留存期限已过'
            else:
                event=json.loads(row['event']);self._validate_job_event(event,row['id'])
                self.progress.phase('evidence_archive')
                self.archive.capture(event,self.root/'runs',row['created'])
                status,error='saved',''
        except (OSError,sqlite3.Error) as exc:
            status='unavailable' if attempts>=3 else 'retry'
            error='原始证据暂不可保存（'+type(exc).__name__+'）'
        except (ValueError,TypeError,KeyError,AttributeError):
            status,error='unavailable','原始证据缺失或校验失败，未归档'
        with self._db() as db:
            db.execute('UPDATE evidence_archive_jobs SET status=?,next_try=?,error=? WHERE id=?',
                       (status,self.outbox.now()+30*attempts,error,row['id']))
        self._archive_error=''

    def archive_referenced_runs(self):
        from .notification_handoff import evidence_references
        if not (self.root/'chart_jobs.sqlite3').exists():return set()
        with self._read_db() as db:
            rows=db.execute("SELECT j.event FROM jobs j LEFT JOIN evidence_archive_jobs a ON a.id=j.id "
                "WHERE j.id NOT LIKE 'recovery:%' AND COALESCE(a.status,'retry')='retry' "
                "AND j.created>=(SELECT v FROM settings WHERE k='archive_enabled_at') AND j.created>?",
                (self.outbox.now()-self.archive.lifetime,)).fetchall()
        refs=set()
        for row in rows:
            try:
                event=json.loads(row[0]);self._validate_job_event(event)
                refs.update(evidence_references([event]))
            except (ValueError,TypeError,KeyError,AttributeError):continue
        return refs

    def archive_snapshot(self):
        error=self._archive_error or self._archive_cleanup_error
        if not (self.root/'chart_jobs.sqlite3').exists():
            return {'saved':0,'pending':0,'unavailable':0,'keep_days':7,'error':error}
        try:
            with self._read_db() as db:
                counts=dict(db.execute("SELECT COALESCE(a.status,'retry'),COUNT(*) FROM jobs j "
                    "LEFT JOIN evidence_archive_jobs a ON a.id=j.id WHERE j.id NOT LIKE 'recovery:%' "
                    "AND j.created>=(SELECT v FROM settings WHERE k='archive_enabled_at') AND j.created>? "
                    "GROUP BY COALESCE(a.status,'retry')",(self.outbox.now()-self.archive.lifetime,)).fetchall())
            return {'saved':counts.get('saved',0),'pending':counts.get('retry',0),
                    'unavailable':counts.get('unavailable',0),'keep_days':7,'error':error}
        except (OSError,sqlite3.Error):
            return {'saved':None,'pending':None,'unavailable':None,'keep_days':7,'error':'通知证据留存状态暂不可用'}

    def snapshot(self):
        if not (self.root/'chart_jobs.sqlite3').exists():
            return {'pending':0,'failed':0,'last_error':self._runtime_error}
        try:
            with self._read_db() as db:
                counts=dict(db.execute("SELECT status,COUNT(*) FROM jobs GROUP BY status").fetchall())
                last_success=db.execute("SELECT MAX(COALESCE(updated,created)) FROM jobs WHERE status='ready'").fetchone()[0]
                active=db.execute("SELECT error FROM jobs WHERE status IN ('pending','running') AND error!='' "
                                  "ORDER BY COALESCE(updated,created) DESC,rowid DESC LIMIT 1").fetchone()
                failed=db.execute("SELECT error FROM jobs WHERE status='failed' AND COALESCE(updated,created)>? "
                                  "ORDER BY COALESCE(updated,created) DESC,rowid DESC LIMIT 1",
                                  (max(last_success or 0,self.outbox.now()-8*86400),)).fetchone()
        except (OSError,sqlite3.Error):
            return {'pending':0,'failed':0,'last_error':'图表任务存储暂不可用'}
        return {'pending':counts.get('pending',0)+counts.get('running',0),'failed':counts.get('failed',0),
                'last_success_at':last_success,
                'evidence_archive':self.archive_snapshot(),
                'last_error':self._runtime_error or (active[0] if active else failed[0] if failed else '')}

    def start(self, enabled):
        if self._thread and self._thread.is_alive():
            return
        try:
            with self._db():
                pass
        except (OSError,sqlite3.Error):
            self._runtime_error='图表任务存储暂不可用，文字通知继续发送'
        self._stop.clear()
        def loop():
            cleaned=0
            while not self._stop.is_set():
                self._wake.clear()
                wait_seconds = 2.0
                next_attempt_at = None
                try:
                    with self.progress.cycle('chart_recovery'):
                        if self.outbox.now()-cleaned>3600:
                            self.cleanup();cleaned=self.outbox.now()
                        if enabled():
                            self.recover();self.process_one()
                            self._runtime_error=''
                            with self._db() as db:
                                next_attempt_at = db.execute(
                                    "SELECT MIN(next_try) FROM jobs WHERE status IN ('pending','running')"
                                ).fetchone()[0]
                        try:self.archive_one()
                        except (OSError,sqlite3.Error):
                            self._archive_error='原始证据归档暂不可用，正在等待恢复'
                    if next_attempt_at is not None:
                        # Keep one renderer, but do not idle between ready jobs.
                        # Recompute after archiving so its work is not waited twice.
                        wait_seconds = max(0.01, min(2.0, next_attempt_at - self.outbox.now()))
                except (OSError,ValueError,sqlite3.Error):
                    self._runtime_error='图表任务暂不可用，等待恢复；文字通知继续发送'
                self._wake.wait(wait_seconds)
        self._thread=threading.Thread(target=loop,name='NotificationChartRenderer',daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set();self._wake.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
