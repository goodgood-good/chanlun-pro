"""Compact event history and aggregate timings, independent of notification delivery."""

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time


class SignalAnalytics:
    def __init__(self, root, *, now=None):
        self.path = Path(root) / "signal_analytics.sqlite3"
        self.now = now or time.time
        self.enabled_at = int(self.now())
        self.lock = threading.RLock()
        self.last_signature = None
        self.cached = None
        self.cached_day = None
        self.error = ""

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        try:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY,v REAL);
                CREATE TABLE IF NOT EXISTS events(
                    id TEXT PRIMARY KEY,signal_id TEXT,cycle INTEGER,market TEXT,frequency TEXT,point_type TEXT,
                    signal_type TEXT,stage TEXT,change_kind TEXT,recorded REAL,cutoff REAL,delay_bars INTEGER,
                    exit_reason TEXT,external INTEGER,revision TEXT);
                CREATE INDEX IF NOT EXISTS events_recorded ON events(recorded);
                CREATE TABLE IF NOT EXISTS timing_seen(id TEXT PRIMARY KEY,recorded REAL);
                CREATE TABLE IF NOT EXISTS timings(
                    day INTEGER,market TEXT,frequency TEXT,revision TEXT,n INTEGER,total_ms REAL,
                    fetch_ms REAL,structure_ms REAL,validation_ms REAL,persistence_ms REAL,max_ms REAL,
                    proofs_reused INTEGER,PRIMARY KEY(day,market,frequency,revision));
            """)
            db.execute(
                "INSERT OR IGNORE INTO settings VALUES ('since',?)", (self.enabled_at,)
            )
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _read_db(self):
        if not self.path.exists():
            with self._db():
                pass
        # Existing summaries must not acquire the writer reservation. Keep
        # every query on one committed snapshot, with bounded lock waiting.
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.1)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN")
            yield db
        finally:
            db.close()

    def record(self, events, periods=(), *, run_id="", revision=""):
        with self.lock:
            self._record_locked(events, periods, run_id=run_id, revision=revision)

    def _record_locked(self, events, periods, *, run_id, revision):
        try:
            events = tuple(events)
            # The fast path must use the same identity as persisted events.
            signature = tuple(
                (event.get("id"), event.get("lifecycle_cycle", 0), event.get("stage"),
                 event.get("change"), event.get("recorded_at", 0), event.get("source_closed_at"),
                 event.get("external_notifications", True))
                for event in events
            )
            if not periods and not self.error and self.last_signature == signature:
                return
            with self._db() as db:
                since = db.execute("SELECT v FROM settings WHERE k='since'").fetchone()[
                    0
                ]
                changed = False
                for event, event_key in zip(events, signature):
                    stamp = event.get("recorded_at", 0)
                    if stamp < since:
                        continue
                    identity = hashlib.sha256(
                        json.dumps(event_key, separators=(",", ":")).encode()
                    ).hexdigest()
                    changed |= (
                        db.execute(
                            "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                identity,
                                event["id"],
                                event.get("lifecycle_cycle", 0),
                                event.get("market", "a"),
                                event["frequency"],
                                event["point_type"],
                                event.get("signal_type", "buy_sell_point"),
                                event["stage"],
                                event.get("change"),
                                stamp,
                                event.get("source_closed_at"),
                                event.get("confirmation_delay_bars"),
                                json.dumps(event.get("exit_reasons", [])),
                                int(event.get("external_notifications", True)),
                                revision,
                            ),
                        ).rowcount
                        > 0
                    )
                for period in periods:
                    costs = period.get("timings_ms")
                    if not costs or not run_id:
                        continue
                    key = json.dumps(
                        [
                            run_id,
                            period.get("market", "a"),
                            period["code"],
                            period["frequency"],
                        ]
                    )
                    if not db.execute(
                        "INSERT OR IGNORE INTO timing_seen VALUES (?,?)",
                        (key, self.now()),
                    ).rowcount:
                        continue
                    values = [
                        max(0, float(costs.get(k, 0)))
                        for k in (
                            "total",
                            "fetch",
                            "structure",
                            "validation",
                            "persistence",
                        )
                    ]
                    db.execute(
                        """INSERT INTO timings VALUES (?,?,?,?,1,?,?,?,?,?,?,?)
                        ON CONFLICT(day,market,frequency,revision) DO UPDATE SET
                        n=n+1,total_ms=total_ms+excluded.total_ms,fetch_ms=fetch_ms+excluded.fetch_ms,
                        structure_ms=structure_ms+excluded.structure_ms,validation_ms=validation_ms+excluded.validation_ms,
                        persistence_ms=persistence_ms+excluded.persistence_ms,max_ms=MAX(max_ms,excluded.max_ms),
                        proofs_reused=proofs_reused+excluded.proofs_reused""",
                        (
                            int((self.now() + 8 * 3600) // 86400),
                            period.get("market", "a"),
                            period["frequency"],
                            revision,
                            *values,
                            values[0],
                            int(period.get("replays_reused", 0)),
                        ),
                    )
                    changed = True
                db.execute(
                    "DELETE FROM events WHERE recorded<?", (self.now() - 366 * 86400,)
                )
                db.execute(
                    "DELETE FROM timing_seen WHERE recorded<?",
                    (self.now() - 7 * 86400,),
                )
                db.execute(
                    "DELETE FROM timings WHERE day<?",
                    (int((self.now() + 8 * 3600) // 86400) - 366,),
                )
            # The context manager commits on exit. A failed commit must not
            # acknowledge this batch or invalidate the last successful view.
            if changed:
                self.cached = None
            self.error = ""
            self.last_signature = signature
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError):
            self.error = "统计记录暂不可用，信号监听继续运行"

    def summary(self):
        with self.lock:
            day = int((self.now() + 8 * 3600) // 86400)
            if self.cached is not None and self.cached_day == day:
                return {**deepcopy(self.cached), "error": self.error}
            try:
                with self._read_db() as db:
                    since = db.execute(
                        "SELECT v FROM settings WHERE k='since'"
                    ).fetchone()[0]
                    groups = db.execute(
                        """WITH samples AS (
                        SELECT signal_id,cycle,market,frequency,point_type,signal_type,
                        MAX(stage='warning') warned,MAX(stage='confirmed') confirmed,
                        MAX(stage='invalidated') invalidated,MAX(stage='left') exited,
                        MAX(exit_reason LIKE '%STRUCTURAL_STOP_TRIGGERED%') stopped,
                        MAX(delay_bars) delay_bars,MIN(recorded) began,MAX(recorded) last_seen
                        FROM events WHERE signal_type!='holding_risk' AND recorded>=?
                        GROUP BY signal_id,cycle,market,frequency,point_type,signal_type)
                        SELECT market,frequency,point_type,signal_type,COUNT(*) samples,SUM(warned) warnings,
                        SUM(confirmed) confirmations,SUM(warned*confirmed) warning_confirmed,
                        SUM(warned*invalidated) warning_invalidated,SUM(exited) window_exits,
                        SUM(stopped) window_stop_hits,AVG(delay_bars) confirmation_delay_bars
                        FROM samples GROUP BY market,frequency,point_type,signal_type
                        ORDER BY market,frequency,point_type""",
                        (self.now() - 366 * 86400,),
                    ).fetchall()
                    costs = db.execute(
                        """SELECT market,frequency,SUM(n) samples,
                        SUM(total_ms)/SUM(n) total_ms,SUM(fetch_ms)/SUM(n) fetch_ms,
                        SUM(structure_ms)/SUM(n) structure_ms,SUM(validation_ms)/SUM(n) validation_ms,
                        SUM(persistence_ms)/SUM(n) persistence_ms,MAX(max_ms) max_ms,SUM(proofs_reused) proofs_reused
                        FROM timings WHERE day>=? GROUP BY market,frequency""",
                        (day - 6,),
                    ).fetchall()
                    count = db.execute(
                        "SELECT COUNT(*) FROM events WHERE recorded>=?",
                        (self.now() - 366 * 86400,),
                    ).fetchone()[0]
                    risks = db.execute(
                        "SELECT COUNT(*) FROM events WHERE signal_type='holding_risk' AND recorded>=?",
                        (self.now() - 366 * 86400,),
                    ).fetchone()[0]
                self.cached = {
                    "since": since,
                    "events": count,
                    "groups": [dict(row) for row in groups],
                    "risk_events": risks,
                    "timings": [dict(row) for row in costs],
                    "scope": "提醒窗口内的程序事件统计；不代表实际交易收益",
                }
                self.cached_day = day
                return {**deepcopy(self.cached), "error": self.error}
            except (OSError, sqlite3.Error):
                return {
                    "since": None,
                    "events": 0,
                    "groups": [],
                    "timings": [],
                    "error": "统计暂不可用",
                }
