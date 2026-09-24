"""After-close screening and monitoring of candidates plus the live watchlist.

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
from chanlun.screening.markets import market_context, watchlist_symbols
from .screening import ScreeningManager, manager as screening_manager, read_state_json, validate_settings
from .dingtalk_notifications import signal_status_key
from .screening_policy import selection_policy_reasons


ACTIVE = {"starting", "running"}
MONITOR_BATCH_SIZE = 64
MORNING_AFTER_CLOSE = "11:35"
LOG = logging.getLogger(__name__)


def _symbol_key(symbol):
    return f"{symbol.get('market', 'a')}:{symbol['code']}"


def _brief(status):
    keys = ("run_id", "status", "completed", "total", "error", "error_count",
            "selected_count", "phase", "cutoffs", "source_current", "freshness_state",
            "cancel_requested", "elapsed_seconds", "current_codes")
    return {key: status[key] for key in keys if key in status}


def _signal(row):
    point = row["point"]
    nested = row.get("nested_confirmation") or {}
    following = row.get("confirmation_segment") or {}
    identity = json.dumps([row.get("market", "a"), row["code"], row["frequency"], point["point_id"]])
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest(),
        "code": row["code"], "market": row.get("market", "a"), "name": row.get("name", row["code"]),
        "frequency": row["frequency"], "point_type": point["point_type"],
        "center_ordinal": point.get("center_ordinal"),
        "stage": row.get("selection_status", point["status"]),
        "point_status": point["status"],
        "point_confirmed_at": point.get("confirmed_at"),
        "selection_status": row.get("selection_status", point["status"]),
        "lower_confirmation_state": nested.get("state"),
        "lower_frequency": nested.get("frequency"),
        "following_segment_state": following.get("state"),
        "following_segment_direction": (following.get("segment") or {}).get("direction"),
        "anchor_price": point.get("anchor_price"),
        "available_at": point.get("available_at"),
        "selection_available_at": row.get("selection_available_at", point.get("available_at")),
        "source_closed_at": row.get("source_closed_at"),
        "missing_conditions": point.get("missing_conditions", []),
        "selection_missing_conditions": row.get("selection_missing_conditions", point.get("missing_conditions", [])),
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
            recovered = False
            if path.is_file():
                try:
                    self._state = read_state_json(path)
                except (json.JSONDecodeError, UnicodeError):
                    backup = self.root / "state.backup.json"
                    self._state = read_state_json(backup)
                    if not isinstance(self._state, dict) or not {"enabled", "settings", "seed", "events"}.issubset(self._state):
                        raise ValueError("监听备份状态缺少必需字段")
                    damaged = self.root / f"state.corrupt.{int(self.now().timestamp())}.json"
                    try:
                        shutil.copy2(path, damaged)
                    except OSError:
                        LOG.warning("could not preserve corrupt monitor state %s", path)
                    recovered = True
            else:
                self._state = {
                    "enabled": False,
                    "settings": {"morning_after_close": MORNING_AFTER_CLOSE, "after_close": "15:10",
                                 "interval_seconds": 60, "daily_scope": "all_a_watchlist",
                                 "screening": validate_settings({"scope": "all_a_watchlist"})},
                    "seed": None, "signals": {}, "known": {}, "events": [],
                    "daily": {}, "last_attempt_key": None, "processed_run": None,
                    "last_check_at": None, "last_error": "", "force": False,
                }
            if recovered:
                self._save()
                LOG.warning("monitor state restored from durable backup after invalid JSON")
            # Old releases stored one market clock for an entire candidate run.
            # Migrate only symbols actually in that old pool, not newly added
            # watchlist members that still need their first check.
            if "attempted_cutoffs" not in self._state:
                seed = self._state.get("seed") or {}
                prefix = seed.get("run_id", "")
                previous = self._state.get("last_attempt_key") or ""
                try:
                    clocks = json.loads(previous[len(prefix):]) if prefix and previous.startswith(prefix) else {}
                except (TypeError, ValueError):
                    clocks = {}
                if not isinstance(clocks, dict):
                    clocks = {}
                self._state["attempted_cutoffs"] = {
                    _symbol_key(s): clocks[s["market"]] for s in seed.get("symbols", [])
                    if type(clocks.get(s["market"])) is int
                }
            self._state.setdefault("checked_cutoffs", {})
            self._state["settings"].setdefault("morning_after_close", MORNING_AFTER_CLOSE)
            daily = self._state.setdefault("daily", {})
            if "afternoon" not in daily and (daily.get("run_id") or daily.get("completed_session")):
                daily["afternoon"] = {k: daily[k] for k in ("run_id", "status", "attempts", "retry_at") if k in daily}
                if daily.get("completed_session") == daily.get("session"):
                    daily["afternoon"]["status"] = "completed"
            # A quiet historical screening suppresses that run's summary only.
            # It must never override the user's live notification switch.
            (self._state.get("seed") or {}).pop("external_notifications", None)
        return self._state

    def _save(self):
        write_json(self.root / "state.json", self._state, durable=True)
        write_json(self.root / "state.backup.json", self._state, durable=True)

    def snapshot(self):
        # Read-only: opening a page never enables or starts a scan.
        with self._lock:
            state = deepcopy(self._load())
        state.pop("known", None)
        state.pop("last_attempt_key", None)
        state.pop("force", None)
        state["today"] = self.now().astimezone(CN).date().isoformat()
        state["signals"] = list(state["signals"].values())
        symbols = (state.get("seed") or {}).get("symbols", [])
        state["pool_markets"] = {market: sum(s["market"] == market for s in symbols)
                                 for market in sorted({s["market"] for s in symbols})}
        state["errors"] = [*state.get("errors", []), *state.get("schedule_errors", [])]
        if not state.get("last_error"):
            state["last_error"] = state.get("watchlist_error", "") or (
                "部分关注标的的交易时段无法核对，请查看问题列表" if state.get("schedule_errors") else "")
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
            events = [e for e in state["events"] if e.get("external_notifications", True)]
        if not enabled:
            return
        if (manual["status"] == "completed" and manual.get("external_notifications", True) and manual.get("run_id")
                and (manual.get("finished_at", 0) >= since or manual["run_id"] == following)
                and not self.notifications.has("screening:" + manual["run_id"])):
            result = self.screening.results()
            if (result["status"] == "completed" and result.get("run_id") == manual["run_id"]
                    and result.get("external_notifications", True)):
                baseline = [_signal(row) for row in [*result.get("selected", []), *result.get("observations", [])]]
                self.notifications.screening_completed(result, baseline)
        # Recovery after a restart also covers a state write completed just
        # before the process exited, without replaying historical notifications.
        self.notifications.signal_events(events, since=since)

    def enable(self, body):
        if not isinstance(body, dict) or set(body) - {"morning_after_close", "after_close", "interval_seconds", "daily_scope", "screening_workers"}:
            raise ValueError("自动任务设置不合法")
        morning = body.get("morning_after_close", self._load()["settings"].get("morning_after_close", MORNING_AFTER_CLOSE))
        if not isinstance(morning, str) or not re.fullmatch(r"(?:11:(?:3[5-9]|[4-5][0-9])|12:[0-5][0-9])", morning):
            raise ValueError("午间选股时间应在 11:35–12:59 之间")
        after = body.get("after_close", "15:10")
        if not isinstance(after, str) or not re.fullmatch(r"(?:1[5-9]|2[0-3]):[0-5][0-9]", after) or after == "15:00":
            raise ValueError("盘后选股时间应在 15:01–23:59 之间")
        interval = body.get("interval_seconds", 60)
        if type(interval) is not int or not 30 <= interval <= 600:
            raise ValueError("检查间隔应为 30–600 秒")
        scope = body.get("daily_scope", "all_a_watchlist")
        if scope not in {"all_a_watchlist", "watchlist", "latest"}:
            raise ValueError("请选择盘后选股范围")
        base = dict(self._load()["settings"].get("screening") or self.screening.status().get("settings") or {"scope": "all_a"})
        base["workers"] = body.get("screening_workers", base.get("workers", 12))
        if scope != "latest":
            base.update(scope=scope, codes=[])
            base.pop("symbols", None)
        settings = {"morning_after_close": morning, "after_close": after, "interval_seconds": interval,
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
            self._record_daily_run(manual)
            if not state["enabled"]:
                return
            now = self.now().astimezone(CN)
            if (manual["status"] == "completed" and manual.get("source_current")
                    and (manual.get("run_id") != (state.get("seed") or {}).get("run_id")
                         or manual.get("selection_policy_version") != (state.get("seed") or {}).get("selection_policy_version"))):
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
                        same_run = (current.get("seed") or {}).get("run_id") == result["run_id"]
                        if same_run:
                            refreshed = {_signal(row)["id"]: _signal(row) for row in rows}
                            current["signals"] = {
                                identity: refreshed.get(identity, item)
                                for identity, item in current["signals"].items()
                                if not selection_policy_reasons(refreshed.get(identity, item))
                            }
                        current["seed"] = {"run_id": result["run_id"], "codes": sorted(pool), "symbols": list(pool.values()),
                                           "screening_symbols": list(pool.values()),
                                           "selection_policy_version": result.get("selection_policy_version"),
                                           "cutoffs": result.get("cutoffs", {}), "settings": result["settings"],
                                           "error_count": len(result.get("errors", []))}
                        # Prune after merging the watchlist below, so replacing
                        # the daily pool does not erase unexamined US signals.
                        current["last_attempt_key"] = None
                        if not same_run:
                            current["attempted_cutoffs"] = {}
                            current["checked_cutoffs"] = {}
                        self._save()
            self._sync_watchlist()
            self._consume_monitor()
            context = trading_context(now, "5m", 1, 1)
            cutoff = context["cutoff"]
            self._schedule_daily(now, cutoff, manual)
            self._schedule_monitor(now, cutoff, manual)
            if manual.get("status") == "completed" and manual.get("source_current"):
                try:
                    monitor_status = self.monitor.status()
                    if monitor_status.get("status") not in ACTIVE:
                        from .higher_context import ensure as ensure_higher_context
                        ensure_higher_context(self.screening, manual, monitor_runs=self.monitor.root)
                except Exception as exc:
                    # Optional context must never interrupt point monitoring.
                    LOG.warning("30m screening context launch failed: %s: %s", type(exc).__name__, exc)
        finally:
            self._tick_lock.release()

    def _sync_watchlist(self):
        """Keep user-selected symbols under observation before a signal exists."""
        with self._lock:
            state = self._load()
            scope = state["settings"]["daily_scope"]
            include_watchlist = scope in {"all_a_watchlist", "watchlist"} or (
                scope == "latest" and state["settings"]["screening"].get("scope") in {"all_a_watchlist", "watchlist"})
        try:
            watch = watchlist_symbols() if include_watchlist else []
        except Exception as exc:
            with self._lock:
                self._load()["watchlist_error"] = "关注组暂时无法读取，继续使用已保存的监听名单"
            LOG.warning("watchlist refresh failed: %s", type(exc).__name__)
            return
        with self._lock:
            state = self._load()
            if not state["enabled"]:
                return
            state["watchlist_error"] = ""
            seed = state.get("seed")
            if seed is None:
                if not watch:
                    return
                seed = {"run_id": "watchlist", "screening_symbols": [], "symbols": [], "codes": [],
                        "cutoffs": {}, "settings": state["settings"]["screening"], "error_count": 0}
                state["seed"] = seed
            base = seed.get("screening_symbols", seed["symbols"])
            pool = {_symbol_key(s): s for s in base}
            pool.update({_symbol_key(s): s for s in watch})
            symbols = [pool[key] for key in sorted(pool)]
            if (symbols == seed["symbols"] and "screening_symbols" in seed
                    and seed.get("watchlist_count") == len(watch)):
                return
            seed.update(screening_symbols=base, symbols=symbols, codes=sorted(pool), watchlist_count=len(watch))
            state["signals"] = {k: v for k, v in state["signals"].items() if _symbol_key(v) in pool}
            for name in ("attempted_cutoffs", "checked_cutoffs"):
                state[name] = {k: v for k, v in state.get(name, {}).items() if k in pool}
            state["next_check_at"] = 0
            self._save()

    def _record_daily_run(self, manual):
        if manual.get("status") not in {"completed", "failed", "cancelled", "interrupted"}:
            return
        with self._lock:
            daily = self._load().get("daily") or {}
            for phase in ("morning", "afternoon"):
                record = daily.get(phase) or {}
                if record.get("run_id") != manual.get("run_id") or record.get("status") != "started":
                    continue
                healthy = manual["status"] == "completed" and not (
                    manual.get("total") and manual.get("error_count", 0) >= manual["total"])
                record["status"] = "completed" if healthy else "failed"
                record["finished_at"] = manual.get("finished_at")
                self._save()
                break

    def _schedule_daily(self, now, cutoff, manual):
        session = now.date().isoformat()
        closed = datetime.fromtimestamp(cutoff, CN)
        with self._lock:
            state = self._load()
            if not state["enabled"] or self._stop.is_set():
                return
            settings = state["settings"]
            if closed.date() != now.date():
                return
            clock = now.strftime("%H:%M")
            if clock >= settings["after_close"] and cutoff == int(now.replace(hour=15, minute=0, second=0, microsecond=0).timestamp()):
                phase = "afternoon"
            elif (settings["morning_after_close"] <= clock < "15:00"
                  and cutoff == int(now.replace(hour=11, minute=30, second=0, microsecond=0).timestamp())):
                phase = "morning"
            else:
                return
            daily = state["daily"]
            if daily.get("session") != session:
                daily = state["daily"] = {"session": session}
            phase_state = daily.get(phase, {})
            if phase_state.get("status") == "completed":
                return
            if (manual["status"] == "completed" and manual.get("source_current")
                    and not (manual.get("total") and manual.get("error_count", 0) >= manual["total"])
                    and manual.get("cutoffs", {}).get("5m") == cutoff
                    and manual.get("settings") == settings["screening"]):
                daily[phase] = {**phase_state, "run_id": manual["run_id"], "status": "completed"}
                self._save()
                return
            if manual["status"] in ACTIVE:
                return
            attempts = phase_state.get("attempts", 0)
            if attempts >= 3 or now.timestamp() < phase_state.get("retry_at", 0):
                return
            monitor_status = self.monitor.status()
            if (monitor_status["status"] in ACTIVE or (monitor_status.get("run_id") == state.get("active_monitor_run")
                    and monitor_status.get("run_id") != state.get("processed_run"))):
                # Finish the last intraday check before the full-market scan.
                return
            launched = self.screening.start(settings["screening"])
            daily[phase] = {"run_id": launched["run_id"], "status": "started",
                            "attempts": attempts + 1, "retry_at": now.timestamp() + 900}
            self._save()

    def _schedule_monitor(self, now, cutoff, manual=None):
        if manual is None:
            manual = self.screening.status()
        with self._lock:
            state = self._load()
            if not state["enabled"] or self._stop.is_set():
                return
            seed = state.get("seed")
            if not seed or not seed["codes"]:
                return
            # A full-market scan gets the resources first; live monitoring then
            # consumes its completed pool, never its partially appended rows.
            monitor_status = self.monitor.status()
            if (manual["status"] in ACTIVE or monitor_status["status"] in ACTIVE
                    or (monitor_status.get("run_id") == state.get("active_monitor_run")
                        and monitor_status.get("run_id") != state.get("processed_run"))):
                return
            if not state["force"] and now.timestamp() < state.get("next_check_at", 0):
                return
            cutoffs, errors, pending = {}, [], []
            for symbol in seed["symbols"]:
                key = _symbol_key(symbol)
                try:
                    closed = cutoff if symbol["market"] == "a" else market_context(
                        symbol["market"], symbol["code"], now, "5m", 1, 1)["cutoff"]
                except (ValueError, OSError) as exc:
                    errors.append({"market": symbol["market"], "code": symbol["code"],
                                   "error": str(exc), "reasons": ["SESSION_CALENDAR_UNAVAILABLE"]})
                    continue
                if state["force"] or key in state.get("force_pending_symbols", []) or state["attempted_cutoffs"].get(key) != closed:
                    pending.append(symbol)
                    cutoffs[key] = closed
            if state.get("schedule_errors", []) != errors:
                state["schedule_errors"] = errors
                self._save()
            if not pending:
                if state["force"]:
                    state["force"] = False
                    self._save()
                return
            # Refresh a bounded batch at the current clock. Watchlist members
            # go first, then the least recently attempted symbols; failures do
            # not permanently monopolize the front of the queue.
            pending.sort(key=lambda symbol: (symbol.get("origin") != "watchlist",
                                             state["attempted_cutoffs"].get(_symbol_key(symbol), 0)))
            backlog = len(pending)
            forced = ({_symbol_key(s) for s in pending} if state["force"]
                      else set(state.get("force_pending_symbols", [])))
            pending = pending[:MONITOR_BATCH_SIZE]
            cutoffs = {_symbol_key(symbol): cutoffs[_symbol_key(symbol)] for symbol in pending}
            settings = {**seed["settings"], "scope": "symbols", "codes": [], "symbols": pending,
                        "workers": min(6, max(2, seed["settings"].get("workers", 4)))}
            launched = self.monitor.start(settings)
            state["attempted_cutoffs"].update(cutoffs)
            state.update(force=False, last_error="",
                         active_seed=seed["run_id"], active_monitor_run=launched["run_id"],
                         active_symbols=list(cutoffs), active_cutoffs=cutoffs,
                         monitor_cursor={"run_id": launched["run_id"], "completed": 0},
                         force_pending_symbols=sorted((forced - set(cutoffs)) & set(seed["codes"])),
                         pending_symbols_count=backlog - len(pending),
                         next_check_at=now.timestamp() + state["settings"]["interval_seconds"])
            self._save()

    def _consume_monitor(self):
        status = self.monitor.status()
        with self._lock:
            state = self._load()
            if status.get("run_id") == state.get("processed_run") or status["status"] == "idle":
                return
            cursor = state.get("monitor_cursor") or {}
            after = cursor.get("completed", 0) if cursor.get("run_id") == status.get("run_id") else 0
            if status["status"] in ACTIVE and status.get("completed", 0) <= after:
                return
        result = self.monitor.committed_results(after) if status["status"] in ACTIVE | {"completed"} else status
        stamp = int(self.now().timestamp())
        with self._lock:
            state = self._load()
            # A newer selection pool can replace a run while the worker finishes.
            if (not state["enabled"] or result.get("run_id") != state.get("active_monitor_run")
                    or state.get("active_seed") != (state.get("seed") or {}).get("run_id")):
                state["processed_run"] = result.get("run_id")
                self._save()
                return
            if result["status"] not in ACTIVE | {"completed"} or not result.get("source_current"):
                state.update(processed_run=result.get("run_id"),
                             last_error=result.get("error") or "监听任务未完成或计算版本已变化，请重新检查")
                self._save()
                return
            pool = set((state.get("seed") or {}).get("codes", []))
            checked = {_symbol_key(symbol) for symbol in result.get("processed_symbols", [])}
            checked &= set(state.get("active_symbols", pool))
            signals = {_signal(r)["id"]: _signal(r) for r in [*result.get("selected", []), *result.get("observations", [])]
                       if _symbol_key(r) in pool and _symbol_key(r) in checked}
            known, events = state["known"], state["events"]
            changed_events = []
            failed = {(error.get("market", "a"), error["code"]) for error in result.get("errors", [])}
            for identity, item in signals.items():
                previous = known.get(identity)
                notification_state = signal_status_key(item)
                if previous is None or (
                    previous["notification_state"] != notification_state if "notification_state" in previous
                    else previous["stage"] != item["stage"] or (
                        item["point_status"] == "confirmed" and item["stage"] != "confirmed")
                ):
                    event = {**item, "recorded_at": stamp, "run_id": result["run_id"],
                             "change": "discovered" if previous is None else "changed"}
                    events.insert(0, event)
                    changed_events.append(event)
                known[identity] = {"stage": item["stage"], "notification_state": notification_state, "updated_at": stamp}
            for identity, previous in state["signals"].items():
                if (_symbol_key(previous) in checked and identity not in signals
                        and (previous.get("market", "a"), previous["code"]) not in failed):
                    events.insert(0, {**previous, "stage": "left", "change": "left",
                                      "recorded_at": stamp, "run_id": result["run_id"]})
                    known[identity] = {"stage": "left", "updated_at": stamp}
            if state.get("dingtalk_enabled"):
                self.notifications.signal_events(changed_events, since=state.get("notification_since", float("inf")))
            # Unchanged/closed markets were not in this batch. Preserve their
            # signals and errors instead of publishing false exits for them.
            signals = {**{k: v for k, v in state["signals"].items()
                          if _symbol_key(v) not in checked and _symbol_key(v) in pool}, **signals}
            errors = [e for e in state.get("errors", []) if _symbol_key(e) not in checked]
            errors.extend(result.get("errors", []))
            valid_cutoffs = {key: value for key, value in state.get("active_cutoffs", {}).items()
                             if key in checked and tuple(key.split(":", 1)) not in failed}
            state["checked_cutoffs"].update(valid_cutoffs)
            market_cutoffs = state.setdefault("last_cutoffs_by_market", {})
            for key, value in valid_cutoffs.items():
                market = key.split(":", 1)[0]
                market_cutoffs[market] = max(value, market_cutoffs.get(market, 0))
            latest_cutoffs = ({"5m": max(valid_cutoffs.values()), "1m": max(valid_cutoffs.values())}
                              if valid_cutoffs else state.get("last_cutoffs", {}))
            through = result.get("results_through", after)
            if result["status"] == "completed" and through >= result.get("completed", 0):
                state["processed_run"] = result["run_id"]
                state["last_batch_completed_at"] = stamp
            if checked:
                state["last_check_at"] = stamp
            state.update(signals=signals, events=events[:500],
                         known=dict(sorted(known.items(), key=lambda item: item[1]["updated_at"], reverse=True)[:10000]),
                         monitor_cursor={"run_id": result["run_id"], "completed": through},
                         last_cutoffs=latest_cutoffs, errors=errors,
                         last_error="部分标的行情或计算异常，请查看问题列表" if errors else "")
            self._save()
        if state.get("processed_run") == result["run_id"]:
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
            saved = read_state_json(path / "status.json")
            if saved["status"] in ACTIVE:
                continue
            try:
                shutil.rmtree(path)
            except OSError:
                LOG.warning("could not prune monitor run %s", path.name)


service = SignalMonitor()
