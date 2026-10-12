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

from .notification_text import public_base, queued_priority


class _ChartExpired(Exception):
    pass


class ChartHistoryPending(Exception):
    """A bounded history slice finished without producing another sendable PNG."""
    def __init__(self, coverage):
        self.coverage = coverage or {period: {'state': 'pending'} for period in ('1m','5m','30m')}
        super().__init__('notification background history is not complete')


def missing_history_periods(coverage):
    return [period for period in ('1m', '5m', '30m') if coverage
            and coverage.get(period, {}).get('state') not in ('target_reached', 'source_exhausted')]


def preparing_history_periods(coverage):
    return [period for period in missing_history_periods(coverage)
            if coverage.get(period, {}).get('state') not in ('invalid', 'unsupported')]


def _frequency_priority(payload):
    try:
        return int(json.loads(payload).get('frequency') in ('5m', '30m'))
    except (ValueError, TypeError, AttributeError, RecursionError):
        return 0


class RenderedChart(bytes):
    """A real PNG plus the periods which were actually exported into it."""

    def __new__(cls, png, *, periods, missing_periods=(), errors=None, primary_frequency=None,
                history_coverage=None, panels=None):
        result = super().__new__(cls, png)
        result.coverage = {"schema": "notification-chart-coverage-v1",
                           "image_sha256": hashlib.sha256(png).hexdigest(),
                           "periods": list(periods), "missing_periods": list(missing_periods),
                           "errors": dict(errors or {}), "primary_frequency": primary_frequency}
        if history_coverage is not None:
            history_coverage = history_coverage or {period: {'state': 'pending'} for period in ('1m','5m','30m')}
            result.coverage.update(schema='notification-chart-coverage-v2', history_coverage=history_coverage,
                                   missing_history_periods=missing_history_periods(history_coverage), panels=panels or [])
        return result


class ChartStore:
    lifetime = 7 * 86400

    def __init__(self, root, *, now=None):
        self.root = Path(root)
        self.now = now or time.time

    def _secret(self, create=False):
        path = self.root / ".signing-key"
        if create:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.root / ('.signing-key.' + secrets.token_hex(6) + '.tmp')
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secrets.token_bytes(32))
                # Both workers may publish their first PNG simultaneously.
                # Link only a completely written key, never expose an empty
                # O_EXCL destination to the other publisher.
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                temporary.unlink(missing_ok=True)
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
        coverage = getattr(png, 'coverage', None)
        if coverage is not None:
            metadata = self.root / (name + '.json')
            if metadata.exists():
                saved = self.coverage(name)
                if saved is None or any(saved.get(key) != coverage.get(key) for key in ('periods', 'missing_periods', 'primary_frequency')):
                    raise ValueError('existing image coverage cannot be changed')
            # A sent image's completeness cannot change when a later render
            # succeeds. Each content hash owns its original coverage record.
            if not metadata.exists():
                temporary = metadata.with_suffix('.' + secrets.token_hex(6) + '.tmp')
                try:
                    temporary.write_text(json.dumps(coverage, ensure_ascii=False, allow_nan=False), encoding='utf-8')
                    try:
                        os.link(temporary, metadata)
                    except FileExistsError:
                        pass
                finally:
                    temporary.unlink(missing_ok=True)
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
                    path.with_suffix('.json').unlink(missing_ok=True)

    def coverage(self, name):
        if not isinstance(name, str) or not re.fullmatch(r'[a-f0-9]{64}', name):
            return None
        try:
            value = json.loads((self.root / (name + '.json')).read_text(encoding='utf-8'))
            return value if value.get('schema') in ('notification-chart-coverage-v1', 'notification-chart-coverage-v2') and value.get('image_sha256') == name else None
        except (OSError, ValueError, AttributeError):
            return None


def render_signal_chart(event, runs_root, *, completion=False, history_period=None):
    """Export the real TradingView renderer using verified, frozen evidence."""
    from .tradingview_snapshot import capture, prepare_history_layout, PanelImageCache
    cache = PanelImageCache(event, Path(runs_root).parent / 'chart_panel_cache')
    reused = cache.read()
    payload = prepare_history_layout(event, runs_root, reused_images=reused, history_period=history_period,
                                     initial_only=not completion, defer_context=completion)
    if completion and preparing_history_periods(payload['history_coverage']):
        raise ChartHistoryPending(payload['history_coverage'])
    return capture(payload, panel_sink=cache.save,
                   completion_event=event if completion else None)


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
        from chanlun.core.strict_structure.native_point_preview import is_point_preview
        preview = event.get('signal_type') == 'native_point_preview' and event.get('stage') == 'approaching'
        matches = [p for level in snapshot["levels"] for p in level.get("points", [])
                   if p.get("point_id") == event.get("point_id") and
                   (is_point_preview(p) if preview else p.get("status") == "confirmed")]
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
        self._history_wake = threading.Event()
        self._thread = None
        self._history_thread = None
        self.created_at = outbox.now()
        self._runtime_error = ''
        self._history_runtime_error = ''
        from .notification_evidence import NotificationEvidence
        self.archive=NotificationEvidence(self.root/'notification_evidence',now=outbox.now)
        self._archive_error=''
        self._archive_cleanup_error=''
        from .worker_progress import WorkerProgress
        self.progress = WorkerProgress(180)
        self.history_progress = WorkerProgress(180)

    @contextmanager
    def _db(self, *, timeout=5, holding_symbols=None):
        self.root.mkdir(parents=True,exist_ok=True)
        db = sqlite3.connect(self.root / "chart_jobs.sqlite3",timeout=timeout)
        try:
            db.row_factory=sqlite3.Row
            db.create_function('holding_priority', 1, lambda payload: queued_priority(payload, holding_symbols))
            db.create_function('frequency_priority', 1, _frequency_priority)
            db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,event TEXT,status TEXT,created REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,error TEXT DEFAULT '',updated REAL)")
            if 'updated' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                try:
                    db.execute('ALTER TABLE jobs ADD COLUMN updated REAL')
                except sqlite3.OperationalError:
                    if 'updated' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
                        raise
            columns = {row[1] for row in db.execute('PRAGMA table_info(jobs)')}
            for name, default in (('missing_periods', '[]'), ('present_periods', '[]'), ('initial_image_hash', ''),
                                  ('history_coverage', '{}'), ('missing_history_periods', '[]'),
                                  ('history_progress', ''), ('history_turn', '0'), ('history_stalls', '0')):
                if name not in columns:
                    try:
                        db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
                    except sqlite3.OperationalError:
                        if name not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
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
                          or event.get('stage') == 'approaching' and event.get('signal_type') == 'native_point_preview'
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
            self._history_wake.set()

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

    def process_one(self, lane=None):
        """One first image or one history slice; at most two across processes.

        Hold the lane lock beyond the SQLite claim lease. Slow provider/browser
        calls therefore cannot be reclaimed while their owner is still alive.
        Process exit releases the lock, preserving the existing crash retry.
        """
        from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout
        if lane not in (None, 'initial', 'history'):
            raise ValueError('unknown chart worker lane')
        for selected_lane in (('initial', 'history') if lane is None else (lane,)):
            try:
                with interprocess_file_lock(self.root / ('.chart-' + selected_lane + '.lock'),
                                            timeout_seconds=.01, poll_seconds=.01):
                    if self._process_one(selected_lane):
                        return True
            except InterprocessLockTimeout:
                continue
        return False

    def _process_one(self, lane, *, skip_waiting=False):
        now=self.outbox.now()
        worker_progress = self.progress if lane == 'initial' else self.history_progress
        holding_symbols = self.outbox._holding_snapshot()
        with self._db(holding_symbols=holding_symbols) as db:
            # Schema migration precedes an explicit serialized claim. The lane
            # file lock bounds execution across separate outbox instances too.
            db.commit()
            db.execute('BEGIN IMMEDIATE')
            if lane == 'history' and db.execute(
                    "SELECT 1 FROM jobs WHERE status IN ('pending','running') AND initial_image_hash='' "
                    "AND next_try<=? LIMIT 1", (now,)).fetchone():
                return False
            condition = "initial_image_hash=''" if lane == 'initial' else "initial_image_hash!=''"
            if skip_waiting:
                condition += " AND error!='等待信号有效性复核'"
            row=db.execute("SELECT * FROM jobs WHERE status IN ('pending','running') AND next_try<=? "
                           "AND " + condition + " "
                           "ORDER BY holding_priority(event) DESC, "
                           "frequency_priority(event) DESC, "
                           "next_try,created,rowid LIMIT 1",(now,)).fetchone()
            if row is None:
                return False
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1,next_try=? WHERE id=?",(now+60,row['id']))
        try:
            event=json.loads(row['event'])
            self._validate_job_event(event,row['id'])
        except (ValueError,TypeError,KeyError):
            with self._db() as db:
                db.execute("UPDATE jobs SET status='failed',error='图表任务数据损坏',updated=? WHERE id=?",(self.outbox.now(),row['id']))
            return True

        def check_deadline():
            if not 0<=self.outbox.now()-event['recorded_at']<1800:
                raise _ChartExpired()

        missing = json.loads(row['missing_periods'] or '[]')
        present = json.loads(row['present_periods'] or '[]')
        history = json.loads(row['history_coverage'] or '{}')
        turn, stalls = int(row['history_turn'] or 0), int(row['history_stalls'] or 0)
        progress = row['history_progress'] or ''
        attempts = row['attempts'] + 1
        initial_hash = row['initial_image_hash'] or self.outbox.initial_chart_hash(event)
        if lane == 'initial' and initial_hash:
            # The first receipt can survive a crash before the chart job write.
            # Move recovered completion work out of the latency-sensitive lane.
            with self._db() as db:
                db.execute("UPDATE jobs SET status='pending',initial_image_hash=?,attempts=?,next_try=0 "
                           "WHERE id=? AND status='running'", (initial_hash,row['attempts'],row['id']))
            self._history_wake.set()
            return False
        retry_delay = 30
        selected = None

        def history_outcome(coverage):
            nonlocal history, progress, stalls, retry_delay, attempts
            history = coverage
            outstanding = missing_history_periods(history)
            states = {history.get(period, {}).get('state') for period in outstanding}
            if states & {'invalid', 'unsupported'} and not preparing_history_periods(history):
                return 'failed', '历史范围受限：' + '、'.join(
                    period + ('来源暂不支持' if history[period].get('state') == 'unsupported' else '历史校验未通过')
                    for period in outstanding if history[period].get('state') in {'invalid', 'unsupported'})
            if 'render_error' in states:
                return ('failed' if row['attempts'] >= 2 else 'pending'), '长期背景导出失败，等待有界重试'
            if selected and history.get(selected, {}).get('state') == 'source_error':
                delay = history[selected].get('next_retry', 30)
                if type(delay) in (int, float) and math.isfinite(delay):
                    retry_delay = min(max(30, delay), max(0, event['recorded_at'] + 1800 - self.outbox.now()))
                return ('failed' if row['attempts'] >= 2 else 'pending'), '历史来源请求失败，等待有界重试'
            signature = hashlib.sha256(json.dumps({p: history.get(p, {}).get('progress_token')
                for p in ('1m', '5m', '30m')}, sort_keys=True).encode()).hexdigest()
            advanced = signature != progress
            stalls = 0 if advanced else min(stalls + 1, 6)
            progress = signature
            # A successful page slice/deadline is scheduling, not a rendering
            # failure. Persist its cursor proof and back off if no progress.
            attempts = row['attempts']
            retry_delay = 5 if advanced else min(300, 15 * 2 ** stalls)
            return 'pending', '历史范围正在补齐：' + '、'.join(outstanding)

        if now-event.get('recorded_at',0)>=1800:
            status,error='expired','图表任务已过期'
        else:
            phase='信号复核'
            try:
                worker_progress.phase('signal_validation')
                if self.outbox.signal_validator:
                    valid,pending=self.outbox.signal_validator(self.outbox._holding_hints([event], holding_symbols))
                    check_deadline()
                    if pending:
                        from .notification_validation import retry_delay
                        with self._db() as db:
                            db.execute("UPDATE jobs SET status='pending',attempts=?,next_try=?,error='等待信号有效性复核' WHERE id=? AND status='running'",
                                       (row['attempts'],now+retry_delay(pending),row['id']))
                        if (not skip_waiting and row['error'] == '等待信号有效性复核'
                                and queued_priority(row['event'], holding_symbols) > 0):
                            # Preserve due holding priority, then let one
                            # ready same-lane image proceed if still unknown.
                            self._process_one(lane, skip_waiting=True)
                        return True
                    if not valid:
                        with self._db() as db:
                            db.execute("UPDATE jobs SET status='expired',error='信号已更新',updated=? WHERE id=?",(self.outbox.now(),row['id']))
                        self.outbox.finish_inline_chart(event,'expired','信号已更新，图文消息已撤销')
                        return True
                check_deadline()
                phase='冻结证据绘图'
                worker_progress.phase('chart_render')
                outstanding = preparing_history_periods(history)
                ordered = ('5m', '30m', '1m')
                selected = next((ordered[(turn + offset) % 3] for offset in range(3)
                    if ordered[(turn + offset) % 3] in outstanding), None) if initial_hash else None
                if selected:
                    turn = (ordered.index(selected) + 1) % 3
                png=(self.renderer(event,self.root/'runs',completion=bool(initial_hash),history_period=selected)
                     if self.renderer is render_signal_chart else self.renderer(event,self.root/'runs'))
                check_deadline()
                coverage = getattr(png, 'coverage', None)
                if coverage is not None:
                    missing, present = coverage['missing_periods'], coverage['periods']
                    history = coverage.get('history_coverage', {})
                incomplete_history = missing_history_periods(history)
                phase='图片发布'
                worker_progress.phase('image_publish')
                url=self.store.publish(png)
                if self.check_public:
                    phase='公网访问校验'
                    worker_progress.phase('public_image_check')
                    import requests
                    response=requests.get(url,timeout=(2,4),allow_redirects=False)
                    if response.status_code!=200 or hashlib.sha256(response.content).digest()!=hashlib.sha256(png).digest():
                        raise ValueError('public-chart-unavailable')
                check_deadline()
                phase='图片入队'
                worker_progress.phase('image_enqueue')
                image_hash = hashlib.sha256(png).hexdigest()
                previous = self.store.coverage(initial_hash) if initial_hash else None
                if initial_hash and (missing or preparing_history_periods(history)):
                    accepted = True  # Do not send a second partial copy.
                elif initial_hash and (image_hash == initial_hash or previous
                                      and previous.get('schema') == 'notification-chart-coverage-v2'
                                      and set(previous.get('periods', [])) == {'1m','5m','30m'}
                                      and not previous.get('missing_periods')
                                      and all(previous.get('history_coverage', {}).get(p, {}).get('state')
                                              in ('target_reached','source_exhausted') for p in ('1m','5m','30m'))):
                    accepted = True  # A committed complete image survived a crash.
                else:
                    accepted = (self.outbox.enqueue_chart(event, url, complete=True) if initial_hash
                                else self.outbox.enqueue_chart(event, url))
                    if accepted and not initial_hash:
                        initial_hash = image_hash
                if not accepted:
                    status,error='expired','通知已过期或开关已更新'
                elif missing and not preparing_history_periods(history):
                    status = 'failed' if row['attempts'] >= 2 else 'pending'
                    error = '辅助周期未补齐：' + '、'.join(missing) + ('；已达到本次重试上限' if status == 'failed' else '；将重试补齐')
                    retry_delay = 30 if row['attempts'] == 0 else 90
                elif incomplete_history:
                    status, error = history_outcome(history)
                else:
                    status,error='ready',''
            except ChartHistoryPending as exc:
                if 0 <= self.outbox.now() - event['recorded_at'] < 1800:
                    status, error = history_outcome(exc.coverage)
                else:
                    status, error = 'expired', '图表任务已过期'
            except _ChartExpired:
                status,error='expired','图表任务已过期'
            except Exception as exc:
                status='failed' if row['attempts']>=2 else 'pending'
                error=phase+'失败（'+type(exc).__name__+'）'
        with self._db() as db:
            finished=self.outbox.now()
            next_try = min(finished + retry_delay, event['recorded_at'] + 1800) if status == 'pending' else finished + retry_delay
            db.execute("UPDATE jobs SET status=?,error=?,next_try=?,updated=?,missing_periods=?,present_periods=?,initial_image_hash=?,"
                       "history_coverage=?,missing_history_periods=?,history_progress=?,history_turn=?,history_stalls=?,attempts=? "
                       "WHERE id=? AND status='running'",
                       (status,error,next_try,finished,json.dumps(missing),json.dumps(present),initial_hash or '',
                        json.dumps(history),json.dumps(missing_history_periods(history)),progress,turn,stalls,attempts,row['id']))
        if not initial_hash or status == 'expired':
            self.outbox.finish_inline_chart(event,status,error)
        elif status == 'pending':
            self._history_wake.set()
        return True

    def cleanup(self):
        """Expire historical diagnostics even when no new charts are generated."""
        self.store.cleanup()
        from .notification_history import HistoryArchive
        HistoryArchive.cleanup(self.root/'notification_history', now=self.outbox.now())
        cache_root = self.root / 'chart_panel_cache'
        if cache_root.is_dir():
            for folder in cache_root.iterdir():
                if (folder.is_dir() and not folder.is_symlink() and re.fullmatch(r'[a-f0-9]{64}', folder.name)
                        and folder.resolve().parent == cache_root.resolve()
                        and self.outbox.now() - folder.stat().st_mtime > 2 * 86400):
                    for saved in folder.iterdir():
                        if saved.is_file() and not saved.is_symlink() and saved.resolve().parent == folder.resolve():
                            saved.unlink(missing_ok=True)
                    try:
                        folder.rmdir()
                    except OSError:
                        pass
        if (self.root/'chart_jobs.sqlite3').exists():
            with self._db() as db:
                db.execute("DELETE FROM jobs WHERE status IN ('ready','failed','expired') AND COALESCE(updated,created)<?",
                           (self.outbox.now()-8*86400,))
                db.execute('DELETE FROM evidence_archive_jobs WHERE id NOT IN (SELECT id FROM jobs)')
        try:
            self.archive.cleanup()
        except (OSError,ValueError,TypeError,KeyError):
            self._archive_cleanup_error='通知证据清理暂不可用'
        else:
            self._archive_cleanup_error=''

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
                event=json.loads(row['event'])
                self._validate_job_event(event,row['id'])
                self.history_progress.phase('evidence_archive')
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
        if not (self.root/'chart_jobs.sqlite3').exists():
            return set()
        with self._read_db() as db:
            rows=db.execute("SELECT j.event FROM jobs j LEFT JOIN evidence_archive_jobs a ON a.id=j.id "
                "WHERE j.id NOT LIKE 'recovery:%' AND COALESCE(a.status,'retry')='retry' "
                "AND j.created>=(SELECT v FROM settings WHERE k='archive_enabled_at') AND j.created>?",
                (self.outbox.now()-self.archive.lifetime,)).fetchall()
        refs=set()
        for row in rows:
            try:
                event=json.loads(row[0])
                self._validate_job_event(event)
                refs.update(evidence_references([event]))
            except (ValueError,TypeError,KeyError,AttributeError):
                continue
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
                'history_worker': self.history_progress.snapshot(),
                'last_error':self._runtime_error or self._history_runtime_error or (active[0] if active else failed[0] if failed else '')}

    def start(self, enabled):
        if self._thread and self._thread.is_alive():
            return
        try:
            with self._db():
                pass
        except (OSError,sqlite3.Error):
            self._runtime_error='图表任务存储暂不可用，文字通知继续发送'
        self._stop.clear()
        def loop(lane):
            cleaned=0
            wake = self._wake if lane == 'initial' else self._history_wake
            progress = self.progress if lane == 'initial' else self.history_progress
            condition = "initial_image_hash=''" if lane == 'initial' else "initial_image_hash!=''"
            while not self._stop.is_set():
                wake.clear()
                wait_seconds = 2.0
                next_attempt_at = None
                try:
                    with progress.cycle('chart_recovery' if lane == 'initial' else 'history_queue'):
                        if lane == 'history' and self.outbox.now()-cleaned>3600:
                            self.cleanup()
                            cleaned=self.outbox.now()
                        if enabled():
                            if lane == 'initial':
                                self.recover()
                            processed = self.process_one(lane)
                            if lane == 'initial':
                                self._runtime_error=''
                            else:
                                self._history_runtime_error=''
                            with self._db() as db:
                                next_attempt_at = db.execute(
                                    "SELECT MIN(next_try) FROM jobs WHERE status IN ('pending','running') AND " + condition
                                ).fetchone()[0]
                            if lane == 'history' and not processed and next_attempt_at is not None and next_attempt_at <= self.outbox.now():
                                # Initial jobs or another process own the lane.
                                # Yield without spinning on a due history task.
                                next_attempt_at = self.outbox.now() + .25
                        if lane == 'history':
                            try:
                                self.archive_one()
                            except (OSError,sqlite3.Error):
                                self._archive_error='原始证据归档暂不可用，正在等待恢复'
                    if next_attempt_at is not None:
                        # Neither lane idles between ready work of its own.
                        # Recompute after archiving so its work is not waited twice.
                        wait_seconds = max(0.01, min(2.0, next_attempt_at - self.outbox.now()))
                except (OSError,ValueError,sqlite3.Error):
                    if lane == 'initial':
                        self._runtime_error='图表任务暂不可用，等待恢复；文字通知继续发送'
                    else:
                        self._history_runtime_error='历史图表任务暂不可用，等待恢复'
                wake.wait(wait_seconds)
        self._thread=threading.Thread(target=loop,args=('initial',),name='NotificationChartRenderer',daemon=True)
        self._history_thread=threading.Thread(target=loop,args=('history',),name='NotificationChartHistory',daemon=True)
        self._thread.start()
        self._history_thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        self._history_wake.set()
        for thread in (self._thread, self._history_thread):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)
