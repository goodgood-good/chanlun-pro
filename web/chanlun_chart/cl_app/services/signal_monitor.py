"""After-close screening and serial monitoring of its completed candidate pool.

The scheduler only coordinates the existing screening engine. Calculations run
in its detached, bounded worker processes, with a separate results directory.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import threading

from chanlun import config
from chanlun.screening.rules import CN, trading_context
from chanlun.screening.runner import write_json
from .screening import ScreeningManager, manager as screening_manager, validate_settings


ACTIVE = {"starting", "running"}
LOG = logging.getLogger(__name__)


def _brief(status):
    keys = ("run_id", "status", "completed", "total", "error", "error_count",
            "selected_count", "phase", "cutoffs", "source_current", "freshness_state",
            "cancel_requested", "elapsed_seconds", "current_codes")
    return {key: status[key] for key in keys if key in status}


def _signal(row):
    point = row["point"]
    identity = json.dumps([row.get("market", "a"), row["code"], row["frequency"], point["point_id"]])
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest(),
        "code": row["code"], "market": row.get("market", "a"), "name": row.get("name", row["code"]),
        "frequency": row["frequency"], "point_type": point["point_type"],
        "stage": row.get("selection_status", point["status"]),
        "anchor_price": point.get("anchor_price"),
        "available_at": row.get("selection_available_at", point.get("available_at")),
        "source_closed_at": row.get("source_closed_at"),
        "missing_conditions": row.get("selection_missing_conditions", point.get("missing_conditions", [])),
    }


class SignalMonitor:
    def __init__(self, root=None, *, screening=None, monitor=None, now=None, notifications=None):
        self._root = root
        self.screening = screening if screening is not None else screening_manager
        self._monitor = monitor
        self.now = now or (lambda: datetime.now(CN))
        self._lock = threading.RLock()
        self._tick_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._state = None
        self._notifications = notifications

    @property
    def root(self):
        return Path(self._root) if self._root else config.get_data_path() / "signal_monitor"

    @property
    def monitor(self):
        if self._monitor is None:
            self._monitor = ScreeningManager(self.root / "runs", max_codes=10000)
        return self._monitor

    @property
    def notifications(self):
        if self._notifications is None:
            from .dingtalk_notifications import DingTalkOutbox
            self._notifications = DingTalkOutbox(self.root / "dingtalk_outbox.sqlite3")
        return self._notifications

    def _load(self):
        if self._state is None:
            path = self.root / "state.json"
            self._state = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
                "enabled": False,
                "settings": {"after_close": "15:10", "interval_seconds": 60, "daily_scope": "all_a_watchlist",
                             "screening": validate_settings({"scope": "all_a_watchlist"})},
                "seed": None, "signals": {}, "known": {}, "events": [],
                "daily": {}, "last_attempt_key": None, "processed_run": None,
                "last_check_at": None, "last_error": "", "force": False,
            }
        return self._state

    def _save(self):
        write_json(self.root / "state.json", self._state)

    def snapshot(self):
        # Read-only: opening a page never enables or starts a scan.
        with self._lock:
            state = deepcopy(self._load())
        state.pop("known", None)
        state.pop("last_attempt_key", None)
        state.pop("force", None)
        state["signals"] = list(state["signals"].values())
        state["runtime_running"] = bool(self._thread and self._thread.is_alive())
        state["screening_job"] = _brief(self.screening.status())
        state["monitor_job"] = _brief(self.monitor.status())
        state["dingtalk"] = {**self.notifications.snapshot(), "enabled": bool(state.get("dingtalk_enabled"))}
        return state

    def notifications_enabled(self):
        with self._lock:
            return bool(self._load().get("dingtalk_enabled"))

    def configure_notifications(self, body):
        if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            raise ValueError("请指定是否启用钉钉通知")
        if body["enabled"] and not self.notifications.transport.configuration()["configured"]:
            raise ValueError("请先配置应用的 CHANLUN_DINGTALK_WEBHOOK")
        manual = self.screening.status()
        with self._lock:
            state = self._load()
            if body["enabled"] and not state.get("dingtalk_enabled"):
                state["notification_since"] = self.now().timestamp()
                state["notification_follow_run"] = manual.get("run_id") if manual["status"] in ACTIVE else None
            state["dingtalk_enabled"] = body["enabled"]
            self._save()
        self.start_runtime()
        self._wake.set()
        return self.snapshot()

    def _publish_notifications(self, manual):
        with self._lock:
            state = self._load()
            enabled = bool(state.get("dingtalk_enabled"))
            since = state.get("notification_since", float("inf"))
            following = state.get("notification_follow_run")
            events = list(state["events"])
        if not enabled:
            return
        if (manual["status"] == "completed" and manual.get("run_id")
                and (manual.get("finished_at", 0) >= since or manual["run_id"] == following)
                and not self.notifications.has("screening:" + manual["run_id"])):
            result = self.screening.results()
            if result["status"] == "completed" and result.get("run_id") == manual["run_id"]:
                baseline = [_signal(row) for row in [*result.get("selected", []), *result.get("observations", [])]]
                self.notifications.screening_completed(result, baseline)
        # Recovery after a restart also covers a state write completed just
        # before the process exited, without replaying historical notifications.
        self.notifications.signal_events(events, since=since)

    def enable(self, body):
        if not isinstance(body, dict) or set(body) - {"after_close", "interval_seconds", "daily_scope"}:
            raise ValueError("自动任务设置不合法")
        after = body.get("after_close", "15:10")
        if not isinstance(after, str) or not re.fullmatch(r"(?:1[5-9]|2[0-3]):[0-5][0-9]", after) or after == "15:00":
            raise ValueError("盘后选股时间应在 15:01–23:59 之间")
        interval = body.get("interval_seconds", 60)
        if type(interval) is not int or not 30 <= interval <= 600:
            raise ValueError("检查间隔应为 30–600 秒")
        scope = body.get("daily_scope", "all_a_watchlist")
        if scope not in {"all_a_watchlist", "watchlist", "latest"}:
            raise ValueError("请选择盘后选股范围")
        base = dict(self.screening.status().get("settings") or {"scope": "all_a"})
        if scope != "latest":
            base.update(scope=scope, codes=[])
            base.pop("symbols", None)
        settings = {"after_close": after, "interval_seconds": interval,
                    "daily_scope": scope, "screening": validate_settings(base)}
        with self._lock:
            state = self._load()
            if state["settings"] != settings or not state["enabled"]:
                state["daily"] = {}
            state.update(enabled=True, settings=settings, last_error="")
            self._save()
        self.start_runtime()
        self._wake.set()
        return self.snapshot()

    def pause(self):
        with self._lock:
            self._load().update(enabled=False, force=False)
            self._save()
            self.monitor.cancel()
        self._wake.set()
        return self.snapshot()

    def check_now(self):
        with self._lock:
            state = self._load()
            if not state["enabled"]:
                raise ValueError("请先启动自动任务")
            if not (state.get("seed") or {}).get("codes"):
                raise ValueError("尚无可监听的已完成选股候选")
            state["force"] = True
            self._save()
        self._wake.set()
        return self.snapshot()

    def start_runtime(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self.notifications.start(self.notifications_enabled)
            self._thread = threading.Thread(target=self._loop, name="SignalMonitor", daemon=True)
            self._thread.start()

    def stop_runtime(self):
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        if self._notifications is not None:
            self._notifications.stop()
        # Persisted enablement and detached workers survive a web restart.

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.tick()
            except Exception as exc:
                LOG.exception("signal monitor scheduler failed")
                with self._lock:
                    self._load()["last_error"] = str(exc)[:500]
                    self._save()
            self._wake.wait(3)

    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            with self._lock:
                state = deepcopy(self._load())
            if self._stop.is_set():
                return
            manual = self.screening.status()
            self._publish_notifications(manual)
            if not state["enabled"]:
                return
            now = self.now().astimezone(CN)
            if (manual["status"] == "completed" and manual.get("source_current")
                    and manual.get("run_id") != (state.get("seed") or {}).get("run_id")):
                result = self.screening.results()
                all_failed = bool(result.get("total") and result.get("error_count", 0) >= result["total"])
                if all_failed:
                    with self._lock:
                        self._load()["last_error"] = "本轮选股全部标的数据异常，保留上次监听名单；请查看选股诊断"
                        self._save()
                if result["status"] == "completed" and result.get("source_current") and not all_failed:
                    rows = [*result.get("selected", []), *result.get("observations", [])]
                    pool = {f"{r.get('market', 'a')}:{r['code']}": {
                        "market": r.get("market", "a"), "code": r["code"], "name": r.get("name", r["code"])} for r in rows}
                    with self._lock:
                        current = self._load()
                        current["seed"] = {"run_id": result["run_id"], "codes": sorted(pool), "symbols": list(pool.values()),
                                           "cutoffs": result.get("cutoffs", {}), "settings": result["settings"],
                                           "error_count": len(result.get("errors", []))}
                        current["signals"] = {k: v for k, v in current["signals"].items() if f"{v.get('market', 'a')}:{v['code']}" in pool}
                        current["last_attempt_key"] = None
                        self._save()
            self._consume_monitor()
            context = trading_context(now, "5m", 1, 1)
            cutoff = context["cutoff"]
            self._schedule_daily(now, cutoff, manual)
            self._schedule_monitor(now, cutoff)
        finally:
            self._tick_lock.release()

    def _schedule_daily(self, now, cutoff, manual):
        session = now.date().isoformat()
        closed = datetime.fromtimestamp(cutoff, CN)
        with self._lock:
            state = self._load()
            if not state["enabled"] or self._stop.is_set():
                return
            settings = state["settings"]
            if closed.date() != now.date() or now.strftime("%H:%M") < settings["after_close"]:
                return
            daily = state["daily"]
            if daily.get("completed_session") == session:
                return
            if (manual["status"] == "completed" and manual.get("source_current")
                    and not (manual.get("total") and manual.get("error_count", 0) >= manual["total"])
                    and manual.get("cutoffs", {}).get("5m") == cutoff
                    and manual.get("settings") == settings["screening"]):
                daily.update(completed_session=session, run_id=manual["run_id"], status="completed")
                self._save()
                return
            if manual["status"] in ACTIVE:
                return
            attempts = daily.get("attempts", 0) if daily.get("session") == session else 0
            if attempts >= 3 or now.timestamp() < daily.get("retry_at", 0):
                return
            if self.monitor.status()["status"] in ACTIVE:
                # Finish the last intraday check before the full-market scan.
                return
            launched = self.screening.start(settings["screening"])
            state["daily"] = {"session": session, "run_id": launched["run_id"], "status": "started",
                              "attempts": attempts + 1, "retry_at": now.timestamp() + 900}
            self._save()

    def _schedule_monitor(self, now, cutoff):
        with self._lock:
            state = self._load()
            if not state["enabled"] or self._stop.is_set():
                return
            seed = state.get("seed")
            if not seed or not seed["codes"]:
                return
            # A full-market scan gets the resources first; live monitoring then
            # consumes its completed pool, never its partially appended rows.
            if self.screening.status()["status"] in ACTIVE or self.monitor.status()["status"] in ACTIVE:
                return
            from chanlun.screening.markets import market_context
            cutoffs = {"a": cutoff}
            for symbol in seed["symbols"]:
                market = symbol["market"]
                if market not in cutoffs:
                    cutoffs[market] = market_context(market, symbol["code"], now, "5m", 1, 1)["cutoff"]
            key = seed["run_id"] + json.dumps(cutoffs, sort_keys=True)
            if not state["force"] and (key == state.get("last_attempt_key")
                    or now.timestamp() < state.get("next_check_at", 0)):
                return
            settings = {**seed["settings"], "scope": "symbols", "codes": [], "symbols": seed["symbols"], "workers": 2}
            launched = self.monitor.start(settings)
            state.update(last_attempt_key=key, force=False, last_error="",
                         active_seed=seed["run_id"], active_monitor_run=launched["run_id"],
                         next_check_at=now.timestamp() + state["settings"]["interval_seconds"])
            self._save()

    def _consume_monitor(self):
        status = self.monitor.status()
        with self._lock:
            state = self._load()
            if status.get("run_id") == state.get("processed_run") or status["status"] in ACTIVE | {"idle"}:
                return
        result = self.monitor.results() if status["status"] == "completed" else status
        stamp = int(self.now().timestamp())
        with self._lock:
            state = self._load()
            # A newer selection pool can replace a run while the worker finishes.
            if (not state["enabled"] or result.get("run_id") != state.get("active_monitor_run")
                    or state.get("active_seed") != (state.get("seed") or {}).get("run_id")):
                state["processed_run"] = result.get("run_id")
                self._save()
                return
            if result["status"] != "completed" or not result.get("source_current"):
                state.update(processed_run=result.get("run_id"),
                             last_error=result.get("error") or "监听任务未完成或计算版本已变化，请重新检查")
                self._save()
                return
            signals = {_signal(r)["id"]: _signal(r) for r in [*result.get("selected", []), *result.get("observations", [])]}
            known, events = state["known"], state["events"]
            changed_events = []
            failed = {(error.get("market", "a"), error["code"]) for error in result.get("errors", [])}
            for identity, item in signals.items():
                previous = known.get(identity)
                if previous is None or previous["stage"] != item["stage"]:
                    event = {**item, "recorded_at": stamp, "run_id": result["run_id"],
                             "change": "discovered" if previous is None else "changed"}
                    events.insert(0, event)
                    changed_events.append(event)
                known[identity] = {"stage": item["stage"], "updated_at": stamp}
            for identity, previous in state["signals"].items():
                if identity not in signals and (previous.get("market", "a"), previous["code"]) not in failed:
                    events.insert(0, {**previous, "stage": "left", "change": "left",
                                      "recorded_at": stamp, "run_id": result["run_id"]})
                    known[identity] = {"stage": "left", "updated_at": stamp}
            if state.get("dingtalk_enabled"):
                self.notifications.signal_events(changed_events, since=state.get("notification_since", float("inf")))
            state.update(signals=signals, events=events[:500],
                         known=dict(sorted(known.items(), key=lambda item: item[1]["updated_at"], reverse=True)[:10000]),
                         processed_run=result["run_id"], last_check_at=stamp,
                         last_cutoffs=result.get("cutoffs", {}), errors=result.get("errors", []),
                         last_error="部分标的行情或计算异常，请查看问题列表" if failed else "")
            self._save()
        self._prune_runs()

    def _prune_runs(self):
        root = self.monitor.root.resolve()
        if not root.is_dir():
            return
        runs = sorted((p for p in root.iterdir() if re.fullmatch(r"[0-9a-f]{32}", p.name)
                       and p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime, reverse=True)
        latest = self.monitor.status().get("run_id")
        for path in runs[3:]:
            # Restrict recursive deletion to this service's own resolved runs.
            if path.resolve().parent != root or path.name == latest:
                continue
            saved = json.loads((path / "status.json").read_text(encoding="utf-8"))
            if saved["status"] in ACTIVE:
                continue
            try:
                shutil.rmtree(path)
            except OSError:
                LOG.warning("could not prune monitor run %s", path.name)


service = SignalMonitor()
