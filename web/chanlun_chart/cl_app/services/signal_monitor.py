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
import sqlite3
import threading
import time

from chanlun import config
from chanlun.screening.rules import CN
from chanlun.screening.runner import write_json
from chanlun.screening.markets import closed_bar_cutoff, holding_symbols, watchlist_symbols
from .screening import ScreeningManager, MonitorResultsReadError, manager as screening_manager, read_state_json, validate_settings, all_periods_failed
from .dingtalk_notifications import signal_status_key, signal_reference
from .notification_handoff import retain as retain_notifications, pending_events, evidence_references
from .notification_text import REASONS, price as notification_price
from .monitor_view import validate_page_options
from .screening_pool_recovery import independent_members, pending_members, suspend_pending, resolve_pending


ACTIVE = {"starting", "running"}
MONITOR_BATCH_SIZE = 64
MONITOR_DURING_SCREENING_BATCH_SIZE = 16
MONITOR_DURING_SCREENING_WORKERS = 2
MONITOR_BACKGROUND_WAVES = 2
MONITOR_MAX_FAILURES = 3
MONITOR_READ_ATTEMPTS = 3
MORNING_AFTER_CLOSE = "11:35"
LOG = logging.getLogger(__name__)
_PRIVATE_SNAPSHOT_FIELDS = frozenset({
    'pending_notifications', 'known', 'last_attempt_key', 'force',
    'unverified_signals', 'holding_evidence', 'holding_risk_plans', 'startup_errors', 'last_check_event_refs',
})


class TaskStartError(RuntimeError):
    """A recorded startup failure which must not stop independent scheduling."""

    def __init__(self, kind, cause):
        self.kind = kind
        super().__init__(str(cause))


def _tick_inputs(raw):
    """Freeze only the seed fields the initial scheduling decision consumes."""
    enabled = raw['enabled']
    seed = raw.get('seed') or {}
    return {'enabled': enabled, 'seed': deepcopy({
        key: seed[key] for key in ('run_id', 'selection_policy_version',
                                  'result_reader_revision', 'screening_symbols') if key in seed
    }) if enabled else None}


def _symbol_key(symbol):
    return f"{symbol.get('market', 'a')}:{symbol['code']}"


def _holding_risk_view_inputs(raw):
    """Copy the dependencies of the holding view while the state lock is held."""
    records = raw.get('holding_evidence', {})
    plans = raw.get('holding_risk_plans', {})
    held = sorted(set(raw.get('holding_symbols', [])))
    periods = sorted(set((raw.get('seed') or {}).get('settings', {}).get('frequencies', [])))
    keys = set(records)
    keys.update(f'{symbol}:{frequency}' for symbol in held for frequency in periods)
    keys.update(f"{_symbol_key(plan)}:{plan['frequency']}" for plan in plans.values()
                if plan.get('active') and _symbol_key(plan) in held)
    checks = raw.get('selection_checks', {})
    return deepcopy({'records': records, 'plans': plans, 'held': held,
                     'current': raw.get('holding_source_current'), 'enabled': raw['enabled'],
                     'periods': periods, 'checks': {key: checks[key] for key in sorted(keys) if key in checks}})


def _holding_risk_expected(records, observed):
    expected = {}
    for identity, record in records.items():
        try:
            expected[identity] = closed_bar_cutoff(record['market'], record['code'], observed, record['frequency'])
        except (ValueError, OSError):
            expected[identity] = None
    return expected


def _holding_risk_view_key(inputs, revision, expected):
    return hashlib.sha256(json.dumps([inputs, revision, expected], sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _monitor_frequency(state, symbol, default='5m'):
    frequencies=set((state.get('seed') or {}).get('settings',{}).get('frequencies',[]))
    frequencies.update(plan['frequency'] for plan in state.get('holding_risk_plans',{}).values()
                       if plan.get('active') and _symbol_key(plan)==_symbol_key(symbol))
    return next((f for f in ('1m','5m','30m') if f in frequencies),default)


def _recent_clock(now, closed, frequency):
    return 0<=now.timestamp()-closed<=int(frequency[:-1])*60


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
        "point_id": point["point_id"],
        'price_basis_revision': point.get('price_basis_revision'),
        'confirmation_delay_bars': row.get('confirmation_delay_bars'),
        "evidence_scope": point.get("evidence_scope"),
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
        "anchor_at": point.get("anchor_at"), "center_id": point.get("center_id"),
        "latest_close": row.get("latest_close"), "structure_price_quantum": row.get("structure_price_quantum"),
        "exit_plan": deepcopy(row.get("exit_plan")), "evidence": deepcopy(row.get("evidence")),
        "available_at": point.get("available_at"),
        "selection_available_at": row.get("selection_available_at", point.get("available_at")),
        "source_closed_at": row.get("source_closed_at"),
        "missing_conditions": point.get("missing_conditions", []),
        "selection_missing_conditions": row.get("selection_missing_conditions", point.get("missing_conditions", [])),
    }


def _consolidation_signal(row):
    alert = row["alert"]
    legacy = json.dumps([row.get("market", "a"), row["code"], row["frequency"], alert["alert_id"], "consolidation"])
    identity = json.dumps([row.get("market", "a"), row["code"], row["frequency"],
                           alert.get("hypothesis_id", alert["alert_id"]), "consolidation"])
    status = alert.get("status", "confirmed")
    return {
        "id": hashlib.sha256(identity.encode()).hexdigest(), "signal_type": "consolidation_divergence",
        "legacy_id": hashlib.sha256(legacy.encode()).hexdigest(),
        "hypothesis_id": alert.get("hypothesis_id"),
        "code": row["code"], "market": row.get("market", "a"), "name": row.get("name", row["code"]),
        "frequency": row["frequency"], "point_type": "consolidation_" + alert["direction"],
        "stage": status, "point_status": status, "selection_status": status,
        "evidence_scope": "native_consolidation_divergence", "anchor_price": alert.get("anchor_price"),
        "anchor_at": alert.get("anchor_at"), "price_basis_revision": alert.get("price_basis_revision"),
        "signal_start_at": alert.get("signal_start_at"),
        "terminal_start_at": alert.get("terminal_start_at", alert.get("signal_start_at") if len(alert.get("signal_leg_unit_ids", [])) == 1 else None),
        "compare_leg_unit_ids": alert.get("compare_leg_unit_ids", []),
        "signal_leg_unit_ids": alert.get("signal_leg_unit_ids", []),
        "latest_close": row.get("latest_close"), "structure_price_quantum": row.get("structure_price_quantum"),
        "evidence": deepcopy(row.get("evidence")),
        "available_at": alert["available_at"], "point_confirmed_at": alert["confirmed_at"],
        "source_closed_at": row.get("source_closed_at"), "center_id": alert["center_id"],
        "macd_area_a": alert["macd_area_a"], "macd_area_c": alert["macd_area_c"],
        "macd_area_ratio": alert["macd_area_ratio"], "core_position": alert["core_position"],
        "comparison_form": alert["comparison_form"], "holding": True,
        "estimated_area_c": alert.get("estimated_area_c"),
        "preview_conditions": "累计面积的两倍仍小于参考段；已收盘MACD连续两次收缩；局部价格转折" if status == "warning" else None,
    }


def _invalidation_evidence(previous, period):
    """Explain a successful negative check without relabeling old numbers as new."""
    checks = period.get("consolidation_checks") or {}
    cutoff = period["source_closed_at"]
    code, measured = "DIAGNOSTIC_UNAVAILABLE", {}
    if checks.get("schema") == "native-preview-checks-v1" and checks.get("source_closed_at") == cutoff:
        if previous.get("price_basis_revision") and checks.get("price_basis_revision") != previous["price_basis_revision"]:
            code = "PRICE_BASIS_CHANGED"
        else:
            measured = checks.get("evaluations", {}).get(previous.get("hypothesis_id"), {})
            code = measured.get("reason_code") or checks.get("reason_code", "COMPARISON_CHANGED")
            if not measured and not (previous.get("price_basis_revision") and previous.get("terminal_start_at")):
                code = "DIAGNOSTIC_UNAVAILABLE"
            if previous.get("terminal_start_at") and checks.get("tail_start_at") != previous["terminal_start_at"]:
                code, measured = "COMPARISON_CHANGED", {}
        if code == "ACTIVE":
            code = "COMPARISON_CHANGED"
    reason = REASONS.get(code, REASONS["DIAGNOSTIC_UNAVAILABLE"])
    details = checks.get("details") or {}
    if code == "PRICE_EXTREME_BROKEN" and all(isinstance(details.get(k),(int,float)) for k in ("anchor","latest_extreme")):
        quantum=previous.get("structure_price_quantum")
        reason = ("候选极值被突破（" + notification_price(details["anchor"],quantum) + " → "
                  + notification_price(details["latest_extreme"],quantum) + "），原局部转折不再成立")
    ratio = measured.get("macd_area_ratio")
    if code == "AREA_MARGIN_LOST" and isinstance(ratio, (int, float)) and ratio >= .5:
        reason = f"C/A面积比升至{ratio:.1%}，不再满足低于50%的预警门槛"
    return {"invalidation_reason_code": code, "invalidation_reason": reason,
            "invalidation_checked_at": cutoff, "metrics_at": cutoff if measured else None,
            "macd_area_a": measured.get("macd_area_a"), "macd_area_c": measured.get("macd_area_c"),
            "macd_area_ratio": measured.get("macd_area_ratio"), "latest_close": period.get("latest_close"),
            "last_valid_at": previous.get("source_closed_at"),
            "last_valid_macd_area_ratio": previous.get("macd_area_ratio")}


class SignalMonitor:
    def __init__(self, root=None, *, screening=None, monitor=None, now=None, notifications=None):
        self._root = root
        self.screening = screening if screening is not None else screening_manager
        self._monitor = monitor
        self.now = now or (lambda: datetime.now(CN))
        self._lock = threading.RLock()
        # Serialize enable/pause effects without holding the shared state lock
        # through worker cancellation and dispatch-lock I/O.
        self._control_lock = threading.RLock()
        self._tick_lock = threading.Lock()
        # Each lazy service owns its initialization lock so unrelated first
        # accesses do not wait on one another or acquire the state lock.
        self._analytics_init_lock = threading.Lock()
        self._monitor_init_lock = threading.Lock()
        self._notifications_init_lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._state = None
        self._control_version = 0
        # Versions distinguish new requests from work already being launched.
        self._check_requests = {}
        self._startup_retries = {}
        self._notifications = notifications
        self._analytics = None
        self._last_tick_at = None
        self._last_tick_error = None
        self._last_cycle_at = None
        self._runtime_started_at = None
        self._last_error_log_at = None
        self._notification_queue_error = ''
        self._notification_retry_at = 0
        self._screening_read_key = None
        self._screening_read_retry_at = 0
        self._screening_read_attempts = 0
        self._risk_view_cache = None
        from .screening_maintenance import InputRetention
        self._input_retention = InputRetention()

    @property
    def root(self):
        return Path(self._root) if self._root else config.get_data_path() / "signal_monitor"

    @property
    def analytics(self):
        if self._analytics is None:
            with self._analytics_init_lock:
                if self._analytics is None:
                    from .signal_analytics import SignalAnalytics
                    self._analytics = SignalAnalytics(self.root,now=lambda:self.now().timestamp())
        return self._analytics

    @property
    def monitor(self):
        if self._monitor is None:
            with self._monitor_init_lock:
                if self._monitor is None:
                    # Committed prefixes are consumed as they arrive. These private
                    # batches have no page requiring a second full-result reader.
                    self._monitor = ScreeningManager(self.root / "runs", max_codes=10000, warm_results=False, reuse_workers=True)
        return self._monitor

    @property
    def notifications(self):
        if self._notifications is None:
            with self._notifications_init_lock:
                if self._notifications is None:
                    from .dingtalk_notifications import DingTalkOutbox
                    self._notifications = DingTalkOutbox(self.root / "dingtalk_outbox.sqlite3",
                                                         signal_validator=self._validate_notification_events, charts=True)
        return self._notifications

    def _validate_notification_events(self, events):
        """Dispatch only current confirmed signals; uncertainty waits in queue."""
        # Complete dependency I/O first. A concurrent result may retire a
        # signal during these reads; validation must use the state after them.
        source = self.monitor.status()
        revision = source.get('current_source_revision') or (
            source.get('source_revision') if source.get('source_current') is True else None)
        version_known = bool(revision) or source.get('source_current') is True
        fresh_holdings = None
        if any(event.get("signal_type") in {"consolidation_divergence","holding_risk"} for event in events):
            try:
                fresh_holdings = {_symbol_key(symbol) for symbol in holding_symbols()}
            except Exception:
                pass
        expected_cutoffs = {}
        for event in events:
            if event.get('signal_type') == 'holding_risk':
                continue
            key = (_symbol_key(event), event.get('frequency'))
            if key in expected_cutoffs:
                continue
            try:
                expected_cutoffs[key] = closed_bar_cutoff(
                    event.get('market', 'a'), event['code'], self.now(), event.get('frequency'))
            except (ValueError, OSError, TypeError):
                expected_cutoffs[key] = None
        with self._lock:
            return self._validate_notification_state(events, revision, version_known, fresh_holdings, expected_cutoffs)

    def _validate_notification_state(self, events, revision, version_known, fresh_holdings, expected_cutoffs):
        """Validate in-memory facts while the caller holds the state lock."""
        state = self._load()
        current = state['signals']
        uncertain = state['unverified_signals']
        seed = state.get('seed') or {}
        checks = state['selection_checks']
        holding_current = state.get('holding_source_current')
        risk_plans = state.get('holding_risk_plans', {})
        recovering_run = (state.get('active_monitor_run')
                          if state.get('active_monitor_run') != state.get('processed_run') else None)
        valid, pending = [], []
        for event in events:
            key = _symbol_key(event)
            frequency = event.get("frequency")
            awaiting_commit = bool(recovering_run and event.get('run_id') == recovering_run)
            if event.get('signal_type') == 'holding_risk':
                if fresh_holdings is None or not holding_current:
                    pending.append(event);continue
                plan = risk_plans.get(f'{key}:{frequency}',{})
                field = {'risk_stop':'stop_triggered_at','risk_reverse':'reverse_at'}.get(event.get('point_type'))
                if (key in fresh_holdings and plan.get('active') and plan.get('binding_id') == event.get('binding_id')
                        and field and plan.get(field) == event.get('available_at')):
                    valid.append(event)
                elif (awaiting_commit and key in fresh_holdings and plan.get('active')
                      and plan.get('binding_id') == event.get('binding_id')):
                    pending.append(event)
                continue
            if key not in seed.get("codes", []) or frequency not in seed.get("settings", {}).get("frequencies", []):
                continue
            consolidation = event.get("signal_type") == "consolidation_divergence"
            if consolidation:
                if fresh_holdings is not None and key not in fresh_holdings:
                    continue
                if fresh_holdings is None or not holding_current:
                    pending.append(event)
                    continue
            elif event.get("point_type") not in seed.get("settings", {}).get("point_types", []):
                continue
            if not version_known or event.get("id") in uncertain:
                pending.append(event)
                continue
            item = current.get(event.get("id"))
            if item is None and consolidation:
                item = next((row for row in current.values() if row.get("legacy_id") == event.get("id")), None)
            allowed = {"warning", "confirmed", "invalidated"} if consolidation else {"confirmed"}
            legacy_confirmation = (consolidation and "hypothesis_id" not in event and "lifecycle_cycle" not in event
                                   and event.get("stage") == "confirmed" and item is not None
                                   and item.get("stage") == "confirmed")
            if (item is None or item.get("stage") not in allowed
                    or item.get("point_status", item.get("stage")) not in allowed
                    or not legacy_confirmation and signal_status_key(item) != signal_status_key(event)):
                if awaiting_commit:
                    pending.append(event)
                continue
            check = checks.get(f"{key}:{frequency}")
            expected = expected_cutoffs.get((key, frequency))
            if expected is None:
                pending.append(event)
                continue
            if (not check or check.get("error") or check.get("revision") != revision
                    or (check.get("cutoff") or 0) < expected
                    or item.get("source_closed_at") != check.get("cutoff")):
                pending.append(event)
                continue
            if not consolidation and item["id"] not in check.get("identities", []):
                continue
            valid.append(deepcopy({**event, **item}))
        return valid, pending

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
            self._state.setdefault("retry_symbols", {})
            self._state.setdefault("unverified_signals", {})
            self._state.setdefault("holding_symbols", [])
            self._state.setdefault('holding_risk_plans',{})
            self._state.setdefault('holding_evidence',{})
            self._state.setdefault("selection_checks", {})
            self._state.setdefault("holding_notification_since", {})
            self._state.setdefault("consolidation_enabled_at", int(self.now().timestamp()))
            self._state.setdefault("consolidation_warning_enabled_at", int(self.now().timestamp()))
            self._state["settings"].setdefault("morning_after_close", MORNING_AFTER_CLOSE)
            daily = self._state.setdefault("daily", {})
            if "afternoon" not in daily and (daily.get("run_id") or daily.get("completed_session")):
                daily["afternoon"] = {k: daily[k] for k in ("run_id", "status", "attempts", "retry_at") if k in daily}
                if daily.get("completed_session") == daily.get("session"):
                    daily["afternoon"]["status"] = "completed"
            # A quiet historical screening suppresses that run's summary only.
            # It must never override the user's live notification switch.
            (self._state.get("seed") or {}).pop("external_notifications", None)
            if 'pending_notifications' not in self._state:
                retain_notifications(self._state,self.now().timestamp())
        return self._state

    def _save(self):
        try:
            write_json(self.root / "state.json", self._state, durable=True)
            write_json(self.root / "state.backup.json", self._state, durable=True)
        except OSError:
            # Subsequent readers must reload what actually reached disk, never
            # publish events left only in memory after a failed commit.
            self._state = None
            raise

    def snapshot(self, *, page_options=None):
        # Read-only: opening a page never enables or starts a scan.
        manual_status, monitor_status = self.screening.status(), self.monitor.status()
        revision = monitor_status.get("current_source_revision") or manual_status.get("current_source_revision")
        if not revision and monitor_status.get("source_current") is not False:
            revision = monitor_status.get("source_revision")
        with self._lock:
            raw=self._load()
            activity={key:raw.get(key) for key in ('enabled','dingtalk_enabled','last_check_at','last_check_notifications')}
            activity['pool_size']=len((raw.get('seed') or {}).get('codes',[]))
            activity['last_event_at']=raw.get('last_signal_event_at') or max(
                (e.get('recorded_at',0) for e in raw['events'] if e.get('external_notifications',True)
                 and e.get('change') in {'discovered','changed'}),default=None)
            event_refs=[]
            if activity['last_check_notifications']:
                if 'last_check_event_refs' in raw:
                    event_refs=deepcopy(raw['last_check_event_refs'])
                else:
                    # Old states may have trimmed history. Require a complete
                    # batch before treating those recovered references as proof.
                    event_refs=[signal_reference(e) for e in raw['events']
                                if e.get('recorded_at')==activity['last_check_at']
                                and e.get('external_notifications',True)
                                and e.get('change') in {'discovered','changed'}]
            screening_read_error=deepcopy(self._load().get('screening_result_error'))
            startup_errors=deepcopy(self._load().get('startup_errors',{}))
            startup_has_pool=bool((self._load().get('seed') or {}).get('codes'))
            if page_options is None:
                state = deepcopy({k:v for k,v in self._load().items() if k not in _PRIVATE_SNAPSHOT_FIELDS})
            else:
                from .monitor_view import paged_state
                state = paged_state(self._load(), page_options, revision, monitor_status.get('source_current'))
        if revision and page_options is None:
            # A deployment invalidates every old-version signal, including
            # symbols outside the batch currently being recomputed. Project
            # only individually rechecked periods; keep durable identities so
            # recovery neither invents exits nor rediscoveries.
            state["signals"] = {
                identity: item for identity, item in state["signals"].items()
                if state["selection_checks"].get(
                    f"{_symbol_key(item)}:{item['frequency']}", {}
                ).get("revision") == revision
            }
        elif monitor_status.get("source_current") is False and page_options is None:
            state["signals"] = {}
        state.pop("known", None)
        state.pop("last_attempt_key", None)
        state.pop("force", None)
        state.pop("unverified_signals", None)
        state.pop('holding_evidence',None)
        state.pop('holding_risk_plans',None)
        observed_at = self.now().astimezone(CN)
        state["today"] = observed_at.date().isoformat()
        from .monitor_schedule import describe as describe_daily_schedule
        state['daily_schedule'] = describe_daily_schedule(state, manual_status, observed_at)
        state["signals"] = [item for item in state["signals"].values() if item.get("stage") != "invalidated"]
        symbols = (state.get("seed") or {}).get("symbols", [])
        if page_options is None:
            if state.get('seed'):
                state['seed']['screening_recheck_count'] = len(state['seed'].get('screening_rechecks', {}))
                state['seed']['screening_count'] = len(state['seed'].get('screening_symbols', symbols)) - state['seed']['screening_recheck_count']
            state["pool_markets"] = {market: sum(s["market"] == market for s in symbols)
                                     for market in sorted({s["market"] for s in symbols})}
        state["errors"] = [*state.get("errors", []), *state.get("schedule_errors", [])]
        if not state.get("last_error"):
            state["last_error"] = state.get("watchlist_error", "") or (
                "部分关注标的的交易时段无法核对，请查看问题列表" if state.get("schedule_errors")
                else state.get("daily_schedule_error", ""))
        state["runtime_running"] = bool(self._thread and self._thread.is_alive())
        state["screening_job"] = _brief(manual_status)
        if screening_read_error and screening_read_error.get('run_id')==manual_status.get('run_id'):
            state['last_error']=screening_read_error['message']
        startup_messages=[]
        live_startup_error=''
        if state['enabled']:
            for kind,error in startup_errors.items():
                if kind=='daily':
                    current=error.get('session')==state['today'] and manual_status['status'] not in ACTIVE
                else:
                    current=(kind=='live' and startup_has_pool and error.get('seed')==(state.get('seed') or {}).get('run_id')
                             and monitor_status['status'] not in ACTIVE)
                if current:
                    startup_messages.append(error['message'])
                    if kind=='live':
                        live_startup_error=error['message']
        if startup_messages:
            state['last_error']='；'.join(startup_messages)
        state["monitor_job"] = _brief(monitor_status)
        state["dingtalk"] = {**self.notifications.snapshot(), "enabled": bool(state.get("dingtalk_enabled"))}
        with self._lock:
            state['dingtalk']['awaiting_enqueue']=len(pending_events(self._load(),self.now().timestamp()))
        if self._notification_queue_error:
            state['dingtalk']['last_error']=self._notification_queue_error
        state['business_health'] = self.business_health(revision=revision)
        worker_error=''
        if state['runtime_running']:
            threads=self.runtime_health()['threads']
            if threads.get('delivery') is False:
                worker_error='通知发送线程已停止，正在等待恢复'
            elif threads.get('charts') is False:
                worker_error='图表生成线程已停止，正在等待恢复'
        from .monitor_activity import describe
        activity['worker_error']=worker_error
        activity['check_error']=live_startup_error
        if event_refs:
            activity['receipts']=self.notifications.signal_receipts(event_refs)
        state['notification_activity']=describe(activity,state['dingtalk'],state['business_health']['scheduler'])
        return state

    def business_health(self, *, revision=None):
        with self._lock:
            raw=self._load()
            plans=list(raw.get('holding_risk_plans',{}).values())
            state={'enabled':raw['enabled'],'last_check_at':raw.get('last_check_at'),
                   'pending':raw.get('pending_symbols_count',0),'holdings':len(raw['holding_symbols']),
                   'risk_bound':sum(p.get('active',False) for p in plans),
                    'risk_attention':sum(p.get('active') and (p.get('status')!='active' or p.get('reverse_check_pending',False)) for p in plans)}
            inputs = _holding_risk_view_inputs(raw)
        expected = _holding_risk_expected(inputs['records'], self.now())
        state['risk_revision'] = _holding_risk_view_key(inputs, revision, expected)[:20]
        alive=bool(self._thread and self._thread.is_alive())
        elapsed=None if self._last_tick_at is None else self.now().timestamp()-self._last_tick_at
        state['scheduler']=('paused' if not state['enabled'] else 'stopped' if not alive else
                            'starting' if elapsed is None else 'delayed' if elapsed>30 or self._last_tick_error else 'running')
        if state['enabled'] and any(w['state']=='stalled' for w in self._notification_worker_health().values()):
            state['scheduler']='delayed'
        state['last_tick_error']=self._last_tick_error
        return state

    def holding_risks(self):
        from .holding_risk import choices
        status = self.monitor.status()
        revision = status.get('current_source_revision') or status.get('source_revision')
        with self._lock:
            inputs = _holding_risk_view_inputs(self._load())
        records, plans, held = inputs['records'], inputs['plans'], inputs['held']
        current, enabled = inputs['current'], inputs['enabled']
        checks, periods = inputs['checks'], inputs['periods']
        expected = _holding_risk_expected(records, self.now())
        key = _holding_risk_view_key(inputs, revision, expected)
        if self._risk_view_cache and self._risk_view_cache[0]==key:
            return deepcopy(self._risk_view_cache[1])
        output=[]
        retry_needed = False
        for symbol in sorted(held):
            market,code=symbol.split(':',1);candidates=[];errors=[]
            for identity,record in records.items():
                if _symbol_key(record)!=symbol:continue
                check=checks.get(identity,{})
                if (not current or check.get('error') or revision and record.get('revision')!=revision
                        or record.get('source_closed_at')!=expected[identity]):
                    errors.append('正在等待该周期的有效复核');continue
                try:candidates.extend(choices(self.monitor.root,record))
                except (OSError,ValueError,KeyError,TypeError):
                    errors.append('该周期参考点暂不可核验')
                    retry_needed = True
            owned=[{k:v for k,v in plan.items() if k not in {'evidence'}} for plan in plans.values()
                   if f"{plan['market']}:{plan['code']}"==symbol and plan.get('active')]
            progress=[]
            for frequency in sorted(set(periods)|{p['frequency'] for p in owned}):
                check=checks.get(f'{symbol}:{frequency}',{})
                progress.append({'frequency':frequency,'cutoff':check.get('cutoff'),'checked_at':check.get('checked_at'),
                                 'status':'limited' if check.get('error') else 'ready' if check and (not revision or check.get('revision')==revision) else 'pending'})
            output.append({'market':market,'code':code,'name':next((r.get('name') for r in records.values() if _symbol_key(r)==symbol),code),
                           'choices':candidates,'plans':owned,'periods':progress,'message':'；'.join(sorted(set(errors)))})
        result={'holdings':output,'holding_source_current':bool(current),'monitor_enabled':enabled}
        if retry_needed:
            result['retry_needed'] = True
            if self._risk_view_cache and self._risk_view_cache[0] == key:
                self._risk_view_cache = None
        else:
            self._risk_view_cache=(key,deepcopy(result))
        return result

    def configure_holding_risk(self,body):
        from .holding_risk import bind,slot
        if not isinstance(body,dict):raise ValueError('参考点参数不合法')
        if body.get('action')=='unbind':
            with self._lock:
                state=self._load()
                plan=next((p for p in state['holding_risk_plans'].values() if p['binding_id']==body.get('binding_id')),None)
                if plan is None:raise ValueError('参考点已变更，请刷新')
                plan.update(active=False,status='detached');self._save()
        elif body.get('action')=='bind':
            selected=next((choice for row in self.holding_risks()['holdings'] for choice in row['choices']
                           if choice['signal_id']==body.get('signal_id')),None)
            if selected is None:raise ValueError('请选择已核验的参考买点')
            key=slot(selected['market'],selected['code'],selected['frequency'])
            try:held={_symbol_key(row) for row in holding_symbols()}
            except Exception:raise ValueError('持仓来源暂不可核验，请稍后重试') from None
            if _symbol_key(selected) not in held:raise ValueError('该标的已不在我的持仓')
            with self._lock:
                state=self._load();record=deepcopy(state['holding_evidence'][key])
                if body.get('source_closed_at')!=record['source_closed_at']:
                    raise ValueError('行情已更新，请刷新参考点后再绑定')
                existing=deepcopy(state['holding_risk_plans'].get(key))
                unchanged=existing and existing.get('active') and existing['signal_id']==selected['signal_id']
            if unchanged:
                return self.holding_risks()
            plan=bind(self.monitor.root,record,selected['signal_id'],self.now().timestamp())
            if record['source_closed_at']!=closed_bar_cutoff(record['market'],record['code'],self.now(),record['frequency']):
                raise ValueError('已有新的收盘K线，请等待复核后重新绑定')
            with self._lock:
                state=self._load()
                if (state['holding_evidence'].get(key)!=record or _symbol_key(selected) not in state['holding_symbols']
                        or not state.get('holding_source_current')):
                    raise ValueError('持仓或行情已更新，请刷新后重试')
                if state['holding_risk_plans'].get(key) != existing:
                    raise ValueError('参考点已被其他操作修改，请刷新后重试')
                state['holding_risk_plans'][key]=plan
                symbol=_symbol_key(selected)
                self._check_requests[symbol]=self._check_requests.get(symbol,0)+1
                state['force_pending_symbols']=sorted(set(state.get('force_pending_symbols',[]))|{symbol})
                state['next_check_at']=0;self._save()
            self._wake.set()
        else:raise ValueError('未知的持仓参考点操作')
        self._risk_view_cache=None
        return self.holding_risks()

    def notifications_enabled(self):
        with self._lock:
            return bool(self._load().get("dingtalk_enabled"))

    def selection_overlay(self, source, revision):
        with self._lock:
            state = self._load()
            if (state.get("seed") or {}).get("run_id") != source:
                return {}
            overlay = deepcopy({key: value for key, value in state["selection_checks"].items()
                                if value.get("revision") == revision})
            # A reader upgrade can suspend a point before a new period check
            # exists. Publish only its identity, never the private old proof.
            for identity, item in state["unverified_signals"].items():
                symbol = _symbol_key(item)
                key = f"{symbol}:{item['frequency']}"
                period = overlay.setdefault(key, {"symbol": symbol, "revision": revision})
                period.setdefault("unverified_identities", []).append(identity)
            for period in overlay.values():
                if "unverified_identities" in period:
                    period["unverified_identities"].sort()
            return overlay

    @staticmethod
    def _notification_baseline(check):
        previous=check.get('notification_baseline') or check
        if previous.get('revision') and type(previous.get('cutoff')) is int:
            return {'revision':previous['revision'],'cutoff':previous['cutoff']}
        return None

    @staticmethod
    def _reusable_pool_checks(state, result):
        """Carry successful checks across equivalent pool replacements only."""
        previous = state.get('seed') or {}
        revision = result.get('current_source_revision') or (result.get('source_revision')
                    if result.get('source_current') is True else None)
        ignored = {'scope','codes','symbols','workers','exclude_st'}
        def rules(settings):
            return {key:(sorted(value) if key in {'frequencies','point_types','structural_levels'} else value)
                    for key,value in settings.items() if key not in ignored}
        if (not revision or state.get('force')
                or not previous.get('selection_policy_version') or not previous.get('result_reader_revision')
                or any(previous.get(key)!=result.get(key) for key in ('selection_policy_version','result_reader_revision'))
                or rules(previous.get('settings',{}))!=rules(result.get('settings',{}))):
            return {}, {}, {}
        incoming = {}
        incoming_fingerprints = {}
        incoming_identities = {}
        for row in [*result.get('verified_periods',[]), *result.get('selected',[]), *result.get('observations',[])]:
            cutoff = row.get('source_closed_at')
            if type(cutoff) is int and row.get('frequency'):
                key=f"{_symbol_key(row)}:{row['frequency']}"
                incoming[key]=max(incoming.get(key,cutoff),cutoff)
                fingerprint=row.get('input_fingerprint') or (row.get('evidence') or {}).get('input_fingerprint')
                incoming_fingerprints.setdefault(key,set()).add(fingerprint if isinstance(fingerprint,str) else None)
        for row in [*result.get('selected',[]), *result.get('observations',[])]:
            key=f"{_symbol_key(row)}:{row['frequency']}"
            incoming_identities.setdefault(key,set()).add(_signal(row)['id'])
        excluded=set(state.get('force_pending_symbols',[])) | set(state.get('retry_symbols',{}))
        errors={( _symbol_key(error), error.get('frequency')) for error in result.get('errors',[]) if error.get('code')}
        retained={}
        for key, check in state['selection_checks'].items():
            symbol=check.get('symbol'); frequency=key.rsplit(':',1)[-1]
            fingerprint=check.get('input_fingerprint')
            if (symbol not in excluded and (symbol,None) not in errors and (symbol,frequency) not in errors
                    and check.get('revision')==revision and check.get('error') is False
                    and type(check.get('cutoff')) is int and isinstance(check.get('identities'),list)
                    and isinstance(fingerprint,str) and len(fingerprint)==64
                    and all(char in '0123456789abcdef' for char in fingerprint)
                    and (key not in incoming or incoming_fingerprints.get(key)=={fingerprint})
                    and (key not in incoming or set(check['identities'])==incoming_identities.get(key,set()))
                    and check['cutoff']>=incoming.get(key,check['cutoff'])):
                retained[key]=deepcopy(check)
        checked={}
        for symbol, cutoff in state.get('checked_cutoffs',{}).items():
            periods=set(result.get('settings',{}).get('frequencies',[]))
            periods.update(plan['frequency'] for plan in state.get('holding_risk_plans',{}).values()
                           if plan.get('active') and _symbol_key(plan)==symbol)
            if (type(cutoff) is int and state.get('attempted_cutoffs',{}).get(symbol)==cutoff
                    and periods and all(f'{symbol}:{frequency}' in retained for frequency in periods)):
                checked[symbol]=cutoff
        return retained, dict(checked), checked

    @classmethod
    def _historical_reclassification(cls, check, revision, available_at):
        previous=cls._notification_baseline(check)
        return bool(previous and revision and previous['revision']!=revision
                    and type(available_at) in (int,float) and available_at<=previous['cutoff'])

    def _mark_selection_failures(self, symbols, stamp, revision, frequency=None, *, cutoffs=None, reason=None):
        """Keep successful exit evidence while suspending unverified candidates."""
        state = self._load()
        for symbol in symbols:
            periods = [frequency] if frequency else list((state.get("seed") or {}).get("settings", {}).get("frequencies", []))
            if frequency is None:
                periods=list(set(periods)|{p['frequency'] for p in state.get('holding_risk_plans',{}).values()
                                           if p.get('active') and _symbol_key(p)==symbol})
            cutoff = (state.get("active_cutoffs", {}) if cutoffs is None else cutoffs).get(symbol)
            if type(cutoff) is not int:
                continue
            for period in periods:
                key = f"{symbol}:{period}"
                previous = state["selection_checks"].get(key, {})
                baseline=self._notification_baseline(previous)
                target = revision or previous.get("revision")
                if previous.get("revision") != target:
                    previous = {}
                if (previous.get("cutoff") or 0) > cutoff:
                    continue
                state["selection_checks"][key] = {
                    **previous, "symbol": symbol, "revision": target,
                    "error": True, "failed_at": stamp, "failed_cutoff": cutoff,
                    **({'notification_baseline':baseline} if baseline else {}),
                    **({'reason':reason} if reason else {}),
                }

    def _control_snapshot(self, page_options):
        return self.snapshot() if page_options is None else self.snapshot(page_options=page_options)

    def configure_notifications(self, body, *, page_options=None):
        validate_page_options(page_options)
        if not isinstance(body, dict) or set(body) != {"enabled"} or type(body["enabled"]) is not bool:
            raise ValueError("请指定是否启用钉钉通知")
        if body["enabled"] and not self.notifications.transport.configuration()["configured"]:
            raise ValueError("请先配置应用的 CHANLUN_DINGTALK_WEBHOOK")
        manual = self.screening.status()
        with self._lock:
            state = self._load()
            if body['enabled'] != bool(state.get('dingtalk_enabled')):
                self.notifications.begin_window(self.now().timestamp())
                state['pending_notifications']={}
            if body["enabled"] and not state.get("dingtalk_enabled"):
                state["notification_since"] = self.now().timestamp()
                state["notification_follow_run"] = manual.get("run_id") if manual["status"] in ACTIVE else None
            state["dingtalk_enabled"] = body["enabled"]
            self._save()
        self.start_runtime()
        self._wake.set()
        return self._control_snapshot(page_options)

    def _publish_notifications(self, manual):
        if time.monotonic() < self._notification_retry_at:
            return
        try:
            self._publish_pending_notifications(manual)
            self._notification_queue_error=''
        except (OSError,sqlite3.Error):
            self._defer_notification_queue()

    def _defer_notification_queue(self):
        self._notification_queue_error='通知队列暂不可用，行情复核继续运行；恢复后重新核验有效事件'
        self._notification_retry_at=time.monotonic()+5

    def _screening_results_for_monitor(self, status):
        key=json.dumps({k:status.get(k) for k in ('run_id','status','completed','evidence_version',
            'source_revision','source_current','frozen_native_review','current_source_revision',
            'result_reader_revision','selection_policy_version','settings','cutoffs','market_cutoffs')},sort_keys=True)
        if key!=self._screening_read_key:
            self._screening_read_key=key
            self._screening_read_retry_at=0
            self._screening_read_attempts=0
        unavailable={'status':'unavailable'}
        if time.monotonic()<self._screening_read_retry_at:
            return unavailable
        try:
            result=self.screening.results(background=True)
        except (OSError,ValueError,TypeError,KeyError,AttributeError,EOFError) as exc:
            self._screening_read_attempts+=1
            if isinstance(exc,OSError):
                # Access failures can recover without changing the result's
                # version. Keep retrying with bounded backoff in that case.
                delay=(15,30,60)[min(self._screening_read_attempts-1,2)]
                self._screening_read_retry_at=time.monotonic()+delay
            else:
                self._screening_read_retry_at=(float('inf') if self._screening_read_attempts>=3
                                               else time.monotonic()+15*self._screening_read_attempts)
            with self._lock:
                self._load()['screening_result_error']={
                    'run_id':status.get('run_id'),'attempts':self._screening_read_attempts,
                    'message':'新选股结果暂不可读取，已有监听名单继续运行',
                    'detail':type(exc).__name__,'at':self.now().timestamp()}
                self._save()
            return unavailable
        if not result.get('results_loading'):
            self._screening_read_attempts=0
            with self._lock:
                if self._load().pop('screening_result_error',None) is not None:
                    self._save()
        return result

    def _publish_pending_notifications(self, manual):
        with self._lock:
            state = self._load()
            enabled = bool(state.get("dingtalk_enabled"))
            since = state.get("notification_since", float("inf"))
            following = state.get("notification_follow_run")
            analytics_events = list(state['events'])
            if retain_notifications(state,self.now().timestamp()):
                self._save()
        self.analytics.record(analytics_events)
        if not enabled:
            return
        if (manual["status"] == "completed" and manual.get("source_current") is not False
                and manual.get("external_notifications", True) and manual.get("run_id")
                and (manual.get("finished_at", 0) >= since or manual["run_id"] == following)
                and not self.notifications.has("screening:" + manual["run_id"])):
            result = self._screening_results_for_monitor(manual)
            if (result["status"] == "completed" and not result.get('results_loading') and result.get("run_id") == manual["run_id"]
                    and result.get("source_current") is not False
                    and result.get("external_notifications", True)):
                baseline = [_signal(row) for row in [*result.get("selected", []), *result.get("observations", [])]]
                self.notifications.screening_completed(result, baseline)
        # Recovery after a restart also covers a state write completed just
        # before the process exited, without replaying historical notifications.
        self._enqueue_pending_notifications()

    def _enqueue_pending_notifications(self):
        with self._lock:
            state=self._load()
            if retain_notifications(state,self.now().timestamp()):
                self._save()
            if not state.get('dingtalk_enabled'):
                return
            since=state.get('notification_since',float('inf'))
            pending=deepcopy({key:event for key,event in state['pending_notifications'].items()
                if event.get('signal_type')!='consolidation_divergence'
                or state.get('holding_source_current') and _symbol_key(event) in state['holding_symbols']})
        # No monitor lock is held across SQLite I/O. On failure the committed
        # journal remains intact. Outbox markers make a crash before this ack
        # safe to retry after restart without duplicating a queued message.
        self.notifications.signal_events(list(pending.values()),since=since)
        if not pending:
            return
        with self._lock:
            state=self._load()
            journal=state['pending_notifications']
            acknowledged=[key for key,event in pending.items() if journal.get(key)==event]
            if acknowledged:
                for key in acknowledged:
                    del journal[key]
                self._save()

    def enable(self, body, *, page_options=None):
        validate_page_options(page_options)
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
        with self._control_lock:
            with self._lock:
                state = self._load()
                if state["settings"] != settings or not state["enabled"]:
                    self._control_version += 1
                    state["daily"] = {}
                    self._startup_retries.clear()
                    state.pop('startup_errors',None)
                state.update(enabled=True, settings=settings, last_error="")
                self._save()
            self.start_runtime()
            self._wake.set()
        return self._control_snapshot(page_options)

    def pause(self, *, page_options=None):
        validate_page_options(page_options)
        with self._control_lock:
            with self._lock:
                self._control_version += 1
                self._load().update(enabled=False, force=False)
                self._save()
                monitor = self.monitor
            monitor.cancel()
            release = getattr(monitor, 'release_workers', None)
            if callable(release):
                release()
        self._wake.set()
        return self._control_snapshot(page_options)

    def check_now(self, *, page_options=None):
        validate_page_options(page_options)
        with self._lock:
            state = self._load()
            if not state["enabled"]:
                raise ValueError("请先启动自动任务")
            if not (state.get("seed") or {}).get("codes"):
                raise ValueError("尚无可监听的已完成选股候选")
            self._check_requests[None]=self._check_requests.get(None,0)+1
            state["force"] = True
            self._save()
        self._wake.set()
        return self._control_snapshot(page_options)

    def start_runtime(self):
        self._input_retention.stop.clear()
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._runtime_started_at = time.monotonic()
            self._last_cycle_at = None
            self.notifications.start(self.notifications_enabled)
            self._thread = threading.Thread(target=self._loop, name="SignalMonitor", daemon=True)
            self._thread.start()

    def stop_runtime(self):
        self._input_retention.stop.set()
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
            self._last_cycle_at = time.monotonic()
            try:
                self.tick()
                with self._lock:
                    raw=self._load()
                    recovered=raw.pop('scheduler_error',None)
                    if recovered:
                        if raw.get('last_error')==recovered.get('message'):
                            raw['last_error']=''
                        self._save()
                self._last_tick_at = self.now().timestamp()
                self._last_tick_error = None
                self._last_error_log_at = None
                self._input_retention.schedule((self.screening.root/'analysis_cache',self.monitor.root/'analysis_cache'),
                                               runs_root=self.screening.root,protected_runs=self._protected_screening_runs)
            except Exception as exc:
                self._last_tick_error = type(exc).__name__
                report = self._last_error_log_at is None or time.monotonic()-self._last_error_log_at >= 60
                if report:
                    self._last_error_log_at = time.monotonic()
                    try:LOG.exception("signal monitor scheduler failed")
                    except Exception:pass
                try:
                    with self._lock:
                        raw=self._load()
                        raw['scheduler_error']={'message':str(exc)[:500],'at':self.now().timestamp()}
                        raw["last_error"] = str(exc)[:500]
                        self._save()
                except Exception as storage_error:
                    # A failed error record must not terminate recovery itself.
                    # Reload durable state on the next tick after storage recovers.
                    if report:
                        try:LOG.warning('monitor error state could not be saved: %s',type(storage_error).__name__)
                        except Exception:pass
            self._wake.wait(3)

    def _notification_worker_health(self):
        from .worker_progress import WorkerProgress
        delivery=self._notifications
        charts=getattr(delivery,'charts',None)
        return {name:worker.progress.snapshot() for name,worker in (('delivery',delivery),('charts',charts))
                if isinstance(getattr(worker,'progress',None),WorkerProgress)}

    def runtime_health(self):
        """Nonblocking liveness probe, independent of disk and the state lock."""
        alive = lambda thread: bool(thread and thread.is_alive())
        threads = {'scheduler':alive(self._thread)}
        workers = self._notification_worker_health()
        if self._notifications is not None:
            threads['delivery']=alive(self._notifications._thread)
            if self._notifications.charts is not None:
                threads['charts']=alive(self._notifications.charts._thread)
        heartbeat = self._last_cycle_at if self._last_cycle_at is not None else self._runtime_started_at
        stalled = heartbeat is not None and time.monotonic()-heartbeat > 180
        stalled = stalled or any(worker['state']=='stalled' for worker in workers.values())
        recover = not all(threads.values()) or stalled
        ready = not recover and not self._last_tick_error and self._last_tick_at is not None
        return {'ready':ready,'status':'stopped' if not all(threads.values()) else 'stalled' if stalled
                else 'degraded' if self._last_tick_error else 'running' if ready else 'starting',
                'threads':threads,'workers':workers,'last_success_at':self._last_tick_at,'error':self._last_tick_error,
                'recovery_recommended':recover}

    def _protected_screening_runs(self):
        with self._lock:
            seed=(self._load().get('seed') or {}).get('run_id')
        references=self.notifications.referenced_runs()
        if not isinstance(references,(set,list,tuple)):
            raise ValueError('notification evidence references unavailable')
        readers=getattr(self.screening,'referenced_runs',None)
        return {seed,*references,*(readers() if callable(readers) else ())}

    def _control_is_current(self, version):
        with self._lock:
            return self._control_version == version and not self._stop.is_set()

    def tick(self):
        if not self._tick_lock.acquire(blocking=False):
            return
        try:
            if self._stop.is_set():
                return
            with self._lock:
                state = _tick_inputs(self._load())
                control_version = self._control_version
            if self._stop.is_set():
                return
            manual = self.screening.status()
            self._record_daily_run(manual)
            if not self._control_is_current(control_version):
                return
            if not state["enabled"]:
                self._publish_notifications(manual)
                return
            now = self.now().astimezone(CN)
            if (manual["status"] == "completed" and (manual.get("source_current") or manual.get("frozen_native_review"))
                    and (manual.get("run_id") != (state.get("seed") or {}).get("run_id")
                         or manual.get("selection_policy_version") != (state.get("seed") or {}).get("selection_policy_version")
                         or manual.get("result_reader_revision") != (state.get("seed") or {}).get("result_reader_revision"))):
                result = self._screening_results_for_monitor(manual)
                historical = result.get("frozen_native_review") is True
                previous_pool = ((state.get('seed') or {}).get('screening_symbols', [])
                                 if historical and result.get('run_id') == (state.get('seed') or {}).get('run_id') else [])
                recheck_periods = result.get('historical_recheck_periods', []) if historical else []
                all_failed = all_periods_failed(result)
                if all_failed and not (previous_pool or recheck_periods):
                    with self._lock:
                        if not self._control_is_current(control_version) or not self._load()['enabled']:
                            return
                        self._load()["last_error"] = "本轮选股全部标的数据异常，保留上次监听名单；请查看选股诊断"
                        self._save()
                if (result["status"] == "completed" and not result.get('results_loading')
                        and (result.get("source_current") or result.get("frozen_native_review"))
                        and (not all_failed or previous_pool or recheck_periods)):
                    rows = [*result.get("selected", []), *result.get("observations", [])]
                    pool = {f"{r.get('market', 'a')}:{r['code']}": {
                        "market": r.get("market", "a"), "code": r["code"], "name": r.get("name", r["code"])} for r in rows}
                    if historical:
                        pool.update({_symbol_key(r): {k:r[k] for k in ('market','code','name') if k in r}
                                     for r in [*previous_pool, *recheck_periods]})
                    with self._lock:
                        current = self._load()
                        if not self._control_is_current(control_version) or not current['enabled']:
                            return
                        pending = pending_members(current.get('seed'), result, {_symbol_key(row) for row in rows})
                        pool.update(pending)
                        previous_seed = current.get('seed') or {}
                        combined = {**pool, **independent_members(current)}
                        previous_checks = current['selection_checks']
                        same_run = (current.get("seed") or {}).get("run_id") == result["run_id"]
                        retained, attempted, checked = self._reusable_pool_checks(current,result) if not same_run else ({},{},{})
                        if same_run:
                            # Saved screening rows belong to a different proof
                            # directory and omit live detection/lifecycle fields.
                            # A changed reader/policy requires a live recheck;
                            # retain the complete prior proof privately until then.
                            current['unverified_signals'].update(current['signals'])
                            current['signals'] = {}
                            current["force"] = True
                        current["seed"] = {"run_id": result["run_id"], "codes": sorted(combined), "symbols": list(combined.values()),
                                           "screening_symbols": list(pool.values()),
                                           "screening_rechecks": pending,
                                           "watchlist_count": previous_seed.get('watchlist_count', 0),
                                           "holding_count": previous_seed.get('holding_count', len(current['holding_symbols'])),
                                           "selection_policy_version": result.get("selection_policy_version"),
                                           "result_reader_revision": result.get("result_reader_revision"),
                                           "historical_pool_requires_recheck": bool(result.get("frozen_native_review")),
                                           "cutoffs": result.get("cutoffs", {}), "settings": result["settings"],
                                           "error_count": len(result.get("errors", []))}
                        # Prune after merging the watchlist below, so replacing
                        # the daily pool does not erase unexamined US signals.
                        current["last_attempt_key"] = None
                        current['next_check_at'] = 0
                        if not same_run:
                            current["attempted_cutoffs"] = attempted
                            current["checked_cutoffs"] = checked
                            current["retry_symbols"] = {}
                            current["selection_checks"] = retained
                        suspend_pending(current, pending, previous_checks, int(now.timestamp()))
                        for period in recheck_periods:
                            key=f"{_symbol_key(period)}:{period['frequency']}"
                            if key not in current['selection_checks']:
                                current['selection_checks'][key]={
                                    'symbol':_symbol_key(period),'revision':period['revision'],
                                    'cutoff':period['cutoff'],'identities':[],'error':True,
                                    'notification_baseline':{'revision':period['revision'],'cutoff':period['cutoff']}}
                        self._save()
            if not self._control_is_current(control_version):
                return
            self._sync_watchlist()
            if not self._control_is_current(control_version):
                return
            self._publish_notifications(manual)
            if not self._control_is_current(control_version):
                return
            self._consume_monitor()
            if not self._control_is_current(control_version):
                return
            # An unavailable A-share calendar must not stop live US/HK/crypto
            # checks. Daily scans still require their own verified A clock.
            try:
                cutoff = closed_bar_cutoff("a", "", now, "5m")
                daily_error = ""
            except (ValueError, OSError) as exc:
                cutoff = None
                daily_error = f"无法核对 A 股定时选股时刻：{exc}"
            with self._lock:
                current = self._load()
                if not self._control_is_current(control_version) or not current['enabled']:
                    return
                if current.get("daily_schedule_error", "") != daily_error:
                    current["daily_schedule_error"] = daily_error
                    self._save()
            if cutoff is not None:
                try:
                    self._schedule_daily(now, cutoff, manual, control_version)
                except TaskStartError:
                    pass
            with self._lock:
                periods = (self._load().get("seed") or {}).get("settings", {}).get(
                    "frequencies", ["5m"],
                )
            trigger_period = next(
                (period for period in ("1m", "5m", "30m") if period in periods),
                "5m",
            )
            trigger_cutoff = cutoff if trigger_period == "5m" else None
            try:
                self._schedule_monitor(now, trigger_cutoff, control_version)
            except TaskStartError:
                pass
        finally:
            self._tick_lock.release()

    def _sync_watchlist(self):
        """Keep user-selected symbols under observation before a signal exists."""
        with self._lock:
            state = self._load()
            if not state['enabled'] or self._stop.is_set():
                return
            control_version = self._control_version
            scope = state["settings"]["daily_scope"]
            include_watchlist = scope in {"all_a_watchlist", "watchlist"} or (
                scope == "latest" and state["settings"]["screening"].get("scope") in {"all_a_watchlist", "watchlist"})
        try:
            watch = watchlist_symbols() if include_watchlist else []
            holdings = holding_symbols()
        except Exception as exc:
            with self._lock:
                current = self._load()
                if not self._control_is_current(control_version) or not current['enabled']:
                    return
                message = "关注组暂时无法读取，继续使用已保存的监听名单"
                uncertain = {key: item for key, item in current["signals"].items()
                             if item.get("signal_type") == "consolidation_divergence"}
                changed = (current.get("watchlist_error") != message
                           or current.get("holding_source_current") is not False or bool(uncertain))
                current.update(watchlist_error=message, holding_source_current=False)
                current["unverified_signals"].update(uncertain)
                current["signals"] = {key: item for key, item in current["signals"].items() if key not in uncertain}
                if changed:
                    self._save()
            LOG.warning("watchlist refresh failed: %s", type(exc).__name__)
            return
        with self._lock:
            state = self._load()
            if not state["enabled"] or not self._control_is_current(control_version):
                return
            source_recovered = state.get("holding_source_current") is False
            state["watchlist_error"] = ""
            state["holding_source_current"] = True
            held_keys = sorted(_symbol_key(symbol) for symbol in holdings)
            for plan in state['holding_risk_plans'].values():
                if f"{plan['market']}:{plan['code']}" not in held_keys:
                    plan.update(active=False,status='detached')
            state['holding_evidence']={k:v for k,v in state['holding_evidence'].items() if _symbol_key(v) in held_keys}
            seed = state.get("seed")
            if seed is None:
                if not watch and not holdings:
                    return
                seed = {"run_id": "watchlist", "screening_symbols": [], "symbols": [], "codes": [],
                        "cutoffs": {}, "settings": state["settings"]["screening"], "error_count": 0}
                state["seed"] = seed
            base = seed.get("screening_symbols", seed["symbols"])
            pool = {_symbol_key(s): s for s in base}
            pool.update({_symbol_key(s): s for s in watch})
            pool.update({_symbol_key(s): s for s in holdings})
            symbols = [pool[key] for key in sorted(pool)]
            frequencies = set(seed.get("settings", {}).get("frequencies", []))
            point_types = set(seed.get("settings", {}).get("point_types", []))
            scope_pruned = False
            for name in ("signals", "unverified_signals"):
                kept = {key: item for key, item in state[name].items()
                        if _symbol_key(item) in pool and item.get("frequency") in frequencies
                        and (_symbol_key(item) in held_keys if item.get("signal_type") == "consolidation_divergence"
                             else item.get("point_type") in point_types)}
                scope_pruned |= len(kept) != len(state[name])
                state[name] = kept
            checks = {key: item for key, item in state["selection_checks"].items()
                      if item.get("symbol") in pool and (key.rsplit(":", 1)[-1] in frequencies
                          or state['holding_risk_plans'].get(key,{}).get('active'))}
            scope_pruned |= len(checks) != len(state["selection_checks"])
            state["selection_checks"] = checks
            if (symbols == seed["symbols"] and "screening_symbols" in seed
                    and seed.get("watchlist_count") == len(watch)
                    and state["holding_symbols"] == held_keys and seed.get("holding_count") == len(holdings)
                    and not scope_pruned and not source_recovered):
                return
            new_holdings = set(held_keys) - set(state["holding_symbols"])
            state["holding_notification_since"] = {key: value for key, value in state["holding_notification_since"].items()
                                                   if key in held_keys}
            for key in new_holdings:
                state["holding_notification_since"][key] = int(self.now().timestamp())
            seed.update(screening_symbols=base, symbols=symbols, codes=sorted(pool),
                        watchlist_count=len(watch), holding_count=len(holdings))
            state["holding_symbols"] = held_keys
            state["force_pending_symbols"] = sorted((set(state.get("force_pending_symbols", [])) | new_holdings
                                                      | (set(held_keys) if source_recovered else set())) & set(pool))
            state["signals"] = {k: v for k, v in state["signals"].items() if _symbol_key(v) in pool
                                and (v.get("signal_type") != "consolidation_divergence" or _symbol_key(v) in held_keys)}
            state["unverified_signals"] = {k: v for k, v in state["unverified_signals"].items()
                                           if _symbol_key(v) in pool}
            for name in ("attempted_cutoffs", "checked_cutoffs", "retry_symbols"):
                state[name] = {k: v for k, v in state.get(name, {}).items() if k in pool}
            state["next_check_at"] = 0
            state["selection_checks"] = {key: value for key, value in state["selection_checks"].items()
                                         if value.get("symbol") in pool}
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
                healthy = manual["status"] == "completed" and not all_periods_failed(manual)
                record["status"] = "completed" if healthy else "failed"
                record["finished_at"] = manual.get("finished_at")
                self._save()
                break

    def _start_allowed(self, kind, context):
        previous=self._startup_retries.get(kind)
        return (not previous or previous['context']!=context
                or time.monotonic()>=previous['retry_at'])

    def _record_start_failure(self, kind, context, error):
        with self._lock:
            previous=self._startup_retries.get(kind) or {}
            attempts=previous.get('attempts',0)+1 if previous.get('context')==context else 1
            delay=(15,30,60)[min(attempts-1,2)]
            self._startup_retries[kind]={'context':deepcopy(context),'attempts':attempts,
                                         'retry_at':time.monotonic()+delay}
            state=self._load()
            message=('定时选股暂时无法启动，将自动重试' if kind=='daily'
                     else '实时检查暂时无法启动，将自动重试')
            stamp=self.now().timestamp()
            state.setdefault('startup_errors',{})[kind]={
                'message':message,'detail':type(error).__name__,'attempts':attempts,
                'failed_at':stamp,'retry_at':stamp+delay,
                **{key:context[key] for key in ('session','phase','seed') if key in context},
            }
            self._save()
        try:
            LOG.warning('%s task startup failed; retrying after %s seconds',kind,delay,exc_info=True)
        except Exception:
            pass

    @staticmethod
    def _clear_start_failure(kind, state):
        errors=state.get('startup_errors',{})
        changed=errors.pop(kind,None) is not None
        if not errors:
            state.pop('startup_errors',None)
        return changed

    def _schedule_daily(self, now, cutoff, manual, control_version=None):
        session = now.date().isoformat()
        closed = datetime.fromtimestamp(cutoff, CN)
        with self._lock:
            state = self._load()
            if not state["enabled"] or self._stop.is_set():
                return
            if control_version is not None and self._control_version != control_version:
                return
            control_version = self._control_version
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
                if self._clear_start_failure('daily',state):
                    self._save()
                self._startup_retries.pop('daily',None)
                return
            if (manual["status"] == "completed" and manual.get("source_current")
                    and not all_periods_failed(manual)
                    and manual.get("cutoffs", {}).get("5m") == cutoff
                    and manual.get("settings") == settings["screening"]):
                daily[phase] = {**phase_state, "run_id": manual["run_id"], "status": "completed"}
                self._clear_start_failure('daily',state)
                self._save()
                self._startup_retries.pop('daily',None)
                return
            if manual["status"] in ACTIVE:
                return
            attempts = phase_state.get("attempts", 0)
            if attempts >= 3 or now.timestamp() < phase_state.get("retry_at", 0):
                return
            expected_daily = deepcopy(daily)
            expected_run = state.get('active_monitor_run')
            expected_processed = state.get('processed_run')
        # Status can inspect files, process ownership and calendars. It must
        # not keep page reads and controls behind the monitor state lock.
        monitor_status = self.monitor.status()
        with self._control_lock:
            with self._lock:
                state = self._load()
                if (not state['enabled'] or not self._control_is_current(control_version)
                        or state['daily'] != expected_daily
                        or state.get('active_monitor_run') != expected_run
                        or state.get('processed_run') != expected_processed):
                    return
                if (monitor_status["status"] in ACTIVE or (monitor_status.get("run_id") == state.get("active_monitor_run")
                        and monitor_status.get("run_id") != state.get("processed_run"))):
                    # Finish the last intraday check before the full-market scan.
                    return
                settings=deepcopy(state['settings']['screening'])
                context={'control':control_version,'session':session,'phase':phase}
                if not self._start_allowed('daily',context):
                    return
            try:
                launched = self.screening.start(settings)
            except Exception as error:
                self._record_start_failure('daily',context,error)
                raise TaskStartError('daily',error) from error
            with self._lock:
                state=self._load()
                state['daily'][phase] = {"run_id": launched["run_id"], "status": "started",
                                                "attempts": attempts + 1, "retry_at": now.timestamp() + 900}
                self._clear_start_failure('daily',state)
                self._save()
                self._startup_retries.pop('daily',None)

    def _yield_background_for_live(self, now, status, control_version,
                                   expected_seed, expected_run, expected_processed):
        identity=status.get('run_id')

        def current(state):
            seed=state.get('seed') or {}
            return (state['enabled'] and bool(seed.get('codes')) and not self._stop.is_set()
                    and self._control_version==control_version
                    and seed.get('run_id')==expected_seed
                    and state.get('active_monitor_run')==expected_run
                    and state.get('processed_run')==expected_processed
                    and identity and state.get('active_monitor_mode')=='background'
                    and identity==state.get('active_monitor_run')
                    and state.get('active_seed')==seed.get('run_id')
                    and not status.get('cancel_requested'))

        # Paused or unrelated work must not wait for another control's cleanup.
        with self._lock:
            if not current(self._load()):
                return
        # Serialize cancellation with enable/pause, while keeping state reads
        # and check-now requests independent of worker cancellation I/O.
        with self._control_lock:
            with self._lock:
                state=self._load()
                if not current(state):
                    return
                seed=state.get('seed') or {}
                # A durable intent survives a restart before the cancel flag
                # is written. Never overlap two private runs.
                requested=state.get('yielding_monitor_run')==identity
                if not requested:
                    if state.get('background_empty_yields',0)>=1 and not status.get('completed',0):
                        # Allow one committed result after an empty preemption.
                        return
                    for symbol in seed.get('symbols',[]):
                        frequency=_monitor_frequency(state,symbol)
                        try:
                            closed=closed_bar_cutoff(symbol['market'],symbol['code'],now,frequency)
                        except (ValueError,OSError):
                            continue
                        attempted=state['attempted_cutoffs'].get(_symbol_key(symbol))
                        if _recent_clock(now,closed,frequency) and (attempted is None or closed>attempted):
                            requested=True
                            break
                if not requested:
                    return
                state['yielding_monitor_run']=identity
                self._save()
                monitor=self.monitor
            monitor.cancel()
            self._wake.set()

    def _schedule_monitor(self, now, cutoff, control_version=None):
        # The daily scan may have started earlier in this same scheduler tick.
        # Read its current state before choosing the live worker budget.
        with self._lock:
            state = self._load()
            if not state['enabled'] or self._stop.is_set():
                return
            if control_version is None:
                control_version = self._control_version
            elif self._control_version != control_version:
                return
            seed = state.get('seed')
            if not seed or not seed['codes']:
                return
            expected_seed = seed.get('run_id')
            expected_run = state.get('active_monitor_run')
            expected_processed = state.get('processed_run')
        manual = self.screening.status()
        monitor_status = self.monitor.status()
        if monitor_status['status'] in ACTIVE:
            self._yield_background_for_live(now,monitor_status,control_version,
                                            expected_seed,expected_run,expected_processed)
            return
        with self._lock:
            state = self._load()
            if not state["enabled"] or self._stop.is_set():
                return
            if self._control_version != control_version:
                return
            if (state.get('active_monitor_run') != expected_run
                    or state.get('processed_run') != expected_processed
                    or (state.get('seed') or {}).get('run_id') != expected_seed):
                return
            seed = state.get("seed")
            if not seed or not seed["codes"]:
                return
            trigger_period = next(
                (period for period in ("1m", "5m", "30m")
                 if period in seed.get("settings", {}).get("frequencies", ())),
                "5m",
            )
            # Keep the last completed pool and current holdings live while a
            # full scan runs. Its partial rows are never used as the new seed.
            # Share resources through smaller batches and fewer workers.
            screening_active = manual["status"] in ACTIVE
            if (monitor_status.get("run_id") == state.get("active_monitor_run")
                        and monitor_status.get("run_id") != state.get("processed_run")):
                return
            reference = monitor_status if monitor_status.get("run_id") else manual
            if reference.get("source_current") is False:
                # Latch the target version, not a failed run ID. A run that
                # dies before recording its source hash must not force an
                # unlimited succession of whole-pool rescans.
                target_revision = reference.get("current_source_revision") or reference.get("source_revision") or "unknown"
                if state.get("source_recheck_revision") != target_revision:
                    state["source_recheck_revision"] = target_revision
                    state["force"] = True
            waiting_interval = (not state["force"] and not state.get("pending_symbols_count")
                                and now.timestamp() < state.get("next_check_at", 0))
            cutoffs, errors, pending, retries, recent = {}, [], [], set(), set()
            previous_calendar_errors = {_symbol_key(error) for error in state.get("schedule_errors", [])}
            for symbol in seed["symbols"]:
                key = _symbol_key(symbol)
                symbol_trigger=_monitor_frequency(state,symbol,trigger_period)
                try:
                    closed = (cutoff if symbol["market"] == "a" and cutoff is not None and symbol_trigger==trigger_period
                              else closed_bar_cutoff(symbol["market"], symbol["code"], now, symbol_trigger))
                except (ValueError, OSError) as exc:
                    errors.append({"market": symbol["market"], "code": symbol["code"],
                                   "error": str(exc), "reasons": ["SESSION_CALENDAR_UNAVAILABLE"]})
                    continue
                is_recent = _recent_clock(now,closed,symbol_trigger)
                if is_recent:
                    recent.add(key)
                attempted = state["attempted_cutoffs"].get(key)
                retry = state["retry_symbols"].get(key, {})
                due = (retry.get("cutoff") == closed and retry.get("retry_at") is not None
                       and retry.get("failures", 0) < MONITOR_MAX_FAILURES
                       and now.timestamp() >= retry["retry_at"])
                # Inspect the cached session clock even during the idle wait.
                # New candles and already-due retries take priority over idle
                # polling. Another batch cannot extend a symbol's backoff.
                if waiting_interval and not (due or is_recent and (attempted is None or closed > attempted)):
                    continue
                fresh = (state["force"] or key in previous_calendar_errors or key in state.get("force_pending_symbols", [])
                         or attempted != closed)
                if fresh or due:
                    pending.append(symbol)
                    cutoffs[key] = closed
                    if not fresh:
                        retries.add(key)
            calendar_revision = reference.get("current_source_revision") or reference.get("source_revision")
            failed_symbols = {_symbol_key(error) for error in errors}
            changed_calendar = state.get("schedule_errors", []) != errors
            if failed_symbols and (changed_calendar or any(
                    not state["selection_checks"].get(f"{symbol}:{period}", {}).get("error")
                    or state["selection_checks"].get(f"{symbol}:{period}", {}).get("revision") != calendar_revision
                    for symbol in failed_symbols for period in seed.get("settings", {}).get("frequencies", []))):
                # This is the time of a failed calendar check, not a claimed
                # closed bar. It suspends candidates without proving an exit.
                stamp = int(now.timestamp())
                self._mark_selection_failures(failed_symbols, stamp, calendar_revision,
                                              cutoffs={symbol: stamp for symbol in failed_symbols})
                state["unverified_signals"].update({key: item for key, item in state["signals"].items()
                                                    if _symbol_key(item) in failed_symbols})
                state["signals"] = {key: item for key, item in state["signals"].items()
                                    if _symbol_key(item) not in failed_symbols}
                changed_calendar = True
            if changed_calendar:
                state["schedule_errors"] = errors
                self._save()
            if not pending:
                if state["force"] or state.get("pending_symbols_count"):
                    state.update(force=False, pending_symbols_count=0)
                    self._save()
                return
            # Live clocks precede historical rebuilds. Within each class keep
            # holdings/watchlist priority, bounded retries and queue rotation.
            pending.sort(key=lambda symbol: (_symbol_key(symbol) not in recent,
                                             _symbol_key(symbol) in retries,
                                             _symbol_key(symbol) not in state["holding_symbols"],
                                             symbol.get("origin") != "watchlist",
                                             state["attempted_cutoffs"].get(_symbol_key(symbol), 0)))
            backlog = len(pending)
            pending_keys={_symbol_key(symbol) for symbol in pending}
            forced = ({_symbol_key(s) for s in pending} if state["force"]
                      else set(state.get("force_pending_symbols", [])))
            forced |= {_symbol_key(symbol) for symbol in pending} & previous_calendar_errors
            batch_size = (min(MONITOR_BATCH_SIZE, MONITOR_DURING_SCREENING_BATCH_SIZE)
                          if screening_active else MONITOR_BATCH_SIZE)
            workers = min(6, max(2, seed["settings"].get("workers", 4)))
            if screening_active:
                workers = min(workers, MONITOR_DURING_SCREENING_WORKERS)
            live_pending=[symbol for symbol in pending if _symbol_key(symbol) in recent]
            mode='live' if live_pending else 'background'
            if live_pending:
                pending=live_pending
            elif recent:
                batch_size=min(batch_size,workers*MONITOR_BACKGROUND_WAVES)
            pending = pending[:batch_size]
            cutoffs = {_symbol_key(symbol): cutoffs[_symbol_key(symbol)] for symbol in pending}
            # Keep capacity for the waiting backlog when an urgent batch is
            # small. The pool starts children only when it has an actual task;
            # a smaller total queue or full-screening budget still trims it.
            workers = min(workers, backlog)
            settings = {**seed["settings"], "scope": "symbols", "codes": [], "symbols": pending,
                        "workers": workers}
            runtime_periods={_symbol_key(symbol):sorted({p['frequency'] for p in state['holding_risk_plans'].values()
                if p.get('active') and f"{p['market']}:{p['code']}"==_symbol_key(symbol)}) for symbol in pending}
            runtime_periods={k:v for k,v in runtime_periods.items() if set(v)-set(seed['settings']['frequencies'])}
            catalog_args={'holding_snapshot':list(state['holding_symbols'])} if state.get('holding_source_current') else {}
            launch={
                'control_version':control_version,'seed':seed['run_id'],
                'expected_run':expected_run,'expected_processed':expected_processed,
                'settings':deepcopy(settings),
                'options':deepcopy({**catalog_args,**({'monitor_periods':runtime_periods} if runtime_periods else {})}),
                'cutoffs':cutoffs,'retries':retries,'forced':forced,
                'pending_before':set(state.get('force_pending_symbols',[])),
                'waiting':pending_keys-set(cutoffs),'mode':mode,'scheduled_at':now.timestamp(),
                'interval':state['settings']['interval_seconds'],'check_requests':dict(self._check_requests),
            }
        self._dispatch_monitor(launch)

    def _dispatch_monitor(self, launch):
        # Launches are serialized with enable/pause, but their process/file I/O
        # must leave the shared state available to readers and new requests.
        with self._control_lock:
            with self._lock:
                state=self._load()
                seed=state.get('seed') or {}
                if (not state['enabled'] or self._stop.is_set()
                        or self._control_version!=launch['control_version']
                        or seed.get('run_id')!=launch['seed'] or not seed.get('codes')
                        or state.get('active_monitor_run')!=launch['expected_run']
                        or state.get('processed_run')!=launch['expected_processed']):
                    return
                monitor=self.monitor
                context={'control':launch['control_version'],'seed':launch['seed'],
                         'requests':launch['check_requests']}
                if not self._start_allowed('live',context):
                    return
            try:
                launched=monitor.start(launch['settings'],**launch['options'])
            except Exception as error:
                self._record_start_failure('live',context,error)
                raise TaskStartError('live',error) from error
            with self._lock:
                state=self._load()
                seed=state.get('seed') or {}
                cutoffs=launch['cutoffs']
                if seed.get('run_id')==launch['seed']:
                    self._clear_start_failure('live',state)
                    state.pop('monitor_read_error',None)
                    state.pop('abandoned_monitor_run',None)
                    for key in cutoffs.keys()-launch['retries']:
                        state['retry_symbols'].pop(key,None)
                    state['attempted_cutoffs'].update(cutoffs)
                    pending=set(state.get('force_pending_symbols',[]))
                    changed={key for key in pending
                             if key not in launch['pending_before']
                             or self._check_requests.get(key,0)!=launch['check_requests'].get(key,0)}
                    acknowledged=set(cutoffs)-changed
                    pool=set(seed['codes'])
                    newer_full_check=self._check_requests.get(None,0)!=launch['check_requests'].get(None,0)
                    state.update(force=state['force'] if newer_full_check else False,last_error='',
                                 force_pending_symbols=sorted(((launch['forced']|changed)-acknowledged)&pool),
                                 pending_symbols_count=len((launch['waiting']|changed)&pool),
                                 next_check_at=launch['scheduled_at']+launch['interval'])
                # A launched run always needs durable ownership, including a
                # shutdown while startup was in progress. Its seed identifies
                # whether later results still belong to the current pool.
                state.update(active_seed=launch['seed'],active_monitor_run=launched['run_id'],
                             active_monitor_mode=launch['mode'],yielding_monitor_run=None,
                             active_scheduled_at=launch['scheduled_at'],active_symbols=list(cutoffs),
                             active_cutoffs=cutoffs,active_consumed_symbols=[],
                             monitor_cursor={'run_id':launched['run_id'],'completed':0})
                self._save()
                self._startup_retries.pop('live',None)

    def _record_failures(self, keys, stamp, run_id):
        """Bound recovery at one clock; a new clock or forced check resets it."""
        state = self._load()
        pool = set((state.get("seed") or {}).get("codes", []))
        for key in keys & pool:
            cutoff = state.get("active_cutoffs", {}).get(key)
            if type(cutoff) is not int:
                continue
            previous = state["retry_symbols"].get(key, {})
            same_clock = previous.get("cutoff") == cutoff
            if same_clock and previous.get("run_id") == run_id:
                continue
            failures = previous.get("failures", 0) + 1 if same_clock else 1
            delay = max(60, state["settings"]["interval_seconds"]) * 2 ** (failures - 1)
            state["retry_symbols"][key] = {
                "cutoff": cutoff, "failures": failures, "run_id": run_id,
                "retry_at": stamp + delay if failures < MONITOR_MAX_FAILURES else None,
            }

    def _monitor_read_failure(self, status, after, error):
        with self._lock:
            state=self._load()
            if (not state['enabled'] or error.run_id!=status.get('run_id')
                    or error.run_id!=state.get('active_monitor_run')):
                return None
            if state.get('active_seed')!=(state.get('seed') or {}).get('run_id'):
                issue=state.pop('monitor_read_error',None)
                if issue and state.get('last_error')==issue.get('message'):
                    state['last_error']=''
                pool=set((state.get('seed') or {}).get('codes',[]))
                state['errors']=[e for e in state.get('errors',[])
                                 if 'RESULT_READ_FAILED' not in e.get('reasons',[]) or _symbol_key(e) in pool]
                state['processed_run']=error.run_id
                self._save()
                return None
            previous=state.get('monitor_read_error') or {}
            attempts=(previous.get('attempts',0)+1 if previous.get('run_id')==error.run_id
                      and previous.get('after')==after else 1)
            message='监听结果读取异常，正在重试' if attempts<MONITOR_READ_ATTEMPTS else '本轮监听结果无法核验，未完成标的将重新检查'
            state['monitor_read_error']={'run_id':error.run_id,'after':after,'attempts':attempts,
                                         'message':message,'detail':str(error),'at':self.now().timestamp()}
            unchecked=set(state.get('active_symbols',[]))-set(state.get('active_consumed_symbols',[]))
            self._mark_selection_failures(unchecked,int(self.now().timestamp()),
                status.get('current_source_revision') or status.get('source_revision'),reason='RESULT_READ_FAILED')
            state['unverified_signals'].update({k:v for k,v in state['signals'].items() if _symbol_key(v) in unchecked})
            state['signals']={k:v for k,v in state['signals'].items() if _symbol_key(v) not in unchecked}
            state['errors']=[e for e in state.get('errors',[]) if _symbol_key(e) not in unchecked]+[
                {'market':key.split(':',1)[0],'code':key.split(':',1)[1],
                 'reasons':['RESULT_READ_FAILED'],'error':message} for key in sorted(unchecked)]
            state['last_error']=message
            if attempts>=MONITOR_READ_ATTEMPTS:
                state['abandoned_monitor_run']=error.run_id
                state['next_check_at']=0
            self._save()
        if attempts<MONITOR_READ_ATTEMPTS:
            return None
        return self._retire_unreadable_monitor(status)

    def _retire_unreadable_monitor(self, status):
        if status['status'] in ACTIVE:
            if not status.get('cancel_requested'):
                self.monitor.cancel()
            return None
        return {**status,'status':'failed','error':'本轮监听结果无法核验，未完成标的将重新检查',
                'result_read_failed':True}

    def _consume_monitor(self):
        status = self.monitor.status()
        with self._lock:
            state = self._load()
            if status.get("run_id") == state.get("processed_run") or status["status"] == "idle":
                return
            cursor = state.get("monitor_cursor") or {}
            after = cursor.get("completed", 0) if cursor.get("run_id") == status.get("run_id") else 0
            yielded=(status['status']=='cancelled' and bool(state.get('yielding_monitor_run'))
                     and status.get('run_id')==state.get('yielding_monitor_run'))
            abandoned=bool(state.get('abandoned_monitor_run')) and state['abandoned_monitor_run']==status.get('run_id')
            if not abandoned and status["status"] in ACTIVE and status.get("completed", 0) <= after:
                return
        if abandoned:
            result=self._retire_unreadable_monitor(status)
        else:
            try:
                result = self.monitor.committed_results(after) if yielded or status["status"] in ACTIVE | {"completed"} else status
            except MonitorResultsReadError as exc:
                result=self._monitor_read_failure(status,after,exc)
        if result is None:
            return
        if yielded:
            result={**result,'background_yielded':True}
        # Decompression and cold structural replays must not hold the global
        # state lock used by HTTP reads and notification validity checks.
        risk_results = self._prepare_risk_checks(result)
        stamp = int(self.now().timestamp())
        with self._lock:
            state = self._load()
            # A newer selection pool can replace a run while the worker finishes.
            if (not state["enabled"] or result.get("run_id") != state.get("active_monitor_run")
                    or state.get("active_seed") != (state.get("seed") or {}).get("run_id")):
                state["processed_run"] = result.get("run_id")
                self._save()
                return
            if not result.get('result_read_failed') and state.get('monitor_read_error',{}).get('run_id')==result.get('run_id'):
                issue=state.pop('monitor_read_error')
                if state.get('last_error')==issue.get('message'):
                    state['last_error']=''
            yielded=(result.get('background_yielded') and result.get('run_id')==state.get('yielding_monitor_run')
                     and result['status']=='cancelled')
            if (result["status"] not in ACTIVE | {"completed"} and not yielded) or not result.get("source_current"):
                unchecked = set(state.get("active_symbols", []))
                if result.get("source_current"):
                    unchecked -= set(state.get("active_consumed_symbols", []))
                self._mark_selection_failures(unchecked, stamp,
                                              result.get("current_source_revision") or result.get("source_revision"))
                if result["status"] not in ACTIVE | {"completed"}:
                    self._record_failures(unchecked, stamp, result.get("run_id"))
                state["unverified_signals"].update({k: v for k, v in state["signals"].items()
                                                    if _symbol_key(v) in unchecked})
                state["signals"] = {k: v for k, v in state["signals"].items() if _symbol_key(v) not in unchecked}
                state.update(processed_run=result.get("run_id"),
                             last_error=result.get("error") or "监听任务未完成或计算版本已变化，请重新检查")
                self._save()
                return
            risk_results = {key: value for key, value in risk_results.items()
                            if state.get('holding_source_current')
                            and state['holding_risk_plans'].get(key) == value['original']
                            and _symbol_key(value['original']) in state['holding_symbols']}
            risk_retries = {_symbol_key(value['original']) for value in risk_results.values() if value['retry']}
            pool = set((state.get("seed") or {}).get("codes", []))
            checked = {_symbol_key(symbol) for symbol in result.get("processed_symbols", [])}
            checked &= set(state.get("active_symbols", pool))
            frequencies = set(result.get("settings", {}).get("frequencies", [])
                              or (state.get("seed") or {}).get("settings", {}).get("frequencies", []))
            verified = {(_symbol_key(period), period.get("frequency"))
                        for period in result.get("verified_periods", [])
                        if type(period.get("source_closed_at")) is int}
            errors = result.get("errors", [])
            missing = []
            for symbol in sorted(checked):
                market, code = symbol.split(":", 1)
                for frequency in sorted(frequencies):
                    if ((symbol, frequency) not in verified and not any(
                            _symbol_key(error) == symbol
                            and error.get("signal_type") != "consolidation_divergence"
                            and error.get("frequency") in (None, frequency) for error in errors)):
                        missing.append({"market": market, "code": code, "frequency": frequency,
                                        "reasons": ["PERIOD_CHECK_MISSING"],
                                        "error": "缺少该周期的成功检查证据，暂不展示信号并等待补查"})
            for period in result.get("verified_periods", []):
                symbol = _symbol_key(period)
                if (symbol in checked and symbol in state["holding_symbols"]
                        and period.get("consolidation_checked") is not True
                        and not any(_symbol_key(error) == symbol
                                    and error.get("frequency") in (None, period["frequency"])
                                    and error.get("signal_type") == "consolidation_divergence" for error in errors)):
                    missing.append({"market": period.get("market", "a"), "code": period["code"],
                                    "frequency": period["frequency"], "signal_type": "consolidation_divergence",
                                    "reasons": ["CONSOLIDATION_CHECK_MISSING"],
                                    "error": "盘整背驰检查尚未完成，保留原状态等待复核"})
            if missing:
                result = {**result, "errors": [*errors, *missing]}
            regressed = []
            for period in result.get("verified_periods", []):
                symbol = _symbol_key(period)
                previous = state["selection_checks"].get(f"{symbol}:{period['frequency']}", {})
                if (symbol in checked and previous.get("revision") == result.get("source_revision")
                        and (previous.get("cutoff") or 0) > period["source_closed_at"]):
                    regressed.append({"market": period.get("market", "a"), "code": period["code"],
                                      "frequency": period["frequency"], "reasons": ["REGRESSED_SOURCE_CUTOFF"],
                                      "error": "复核行情截止早于上次成功检查，等待新行情恢复"})
            if regressed:
                result = {**result, "errors": [*result.get("errors", []), *regressed]}
            signals = {_signal(r)["id"]: _signal(r) for r in [*result.get("selected", []), *result.get("observations", [])]
                       if _symbol_key(r) in pool and _symbol_key(r) in checked
                       and r.get("frequency") in frequencies
                       and (_symbol_key(r), r.get("frequency")) in verified}
            consolidation_choices = {}
            prior = {**state["unverified_signals"], **state["signals"]}
            for row in result.get("consolidation_alerts", []):
                if (state.get("holding_source_current") and _symbol_key(row) in state["holding_symbols"]
                        and _symbol_key(row) in pool and _symbol_key(row) in checked
                        and row.get("frequency") in frequencies
                        and (_symbol_key(row), row.get("frequency")) in verified):
                    item = _consolidation_signal(row)
                    previous = prior.get(item["id"]) or prior.get(item.get("legacy_id"))
                    rank = (2 if previous and previous.get("stage") == "warning"
                            else 1 if previous and previous.get("stage") == "confirmed" else 0,
                            item["comparison_form"] == "entry_departure", item["available_at"], item["id"])
                    key = (_symbol_key(item), item["frequency"], item["center_id"], item["point_type"], item["stage"])
                    if key not in consolidation_choices or rank > consolidation_choices[key][0]:
                        consolidation_choices[key] = rank, item
            for _, item in consolidation_choices.values():
                signals[item["id"]] = item
            for item in signals.values():
                item["holding"] = _symbol_key(item) in state["holding_symbols"]
                item["evidence_run_id"] = result["run_id"]
            known, events = state["known"], state["events"]
            changed_events = []
            failed = {(error.get("market", "a"), error["code"]) for error in result.get("errors", [])}
            failed_all_periods = {(error.get("market", "a"), error["code"])
                                  for error in result.get("errors", []) if not error.get("frequency")
                                  and error.get("signal_type") != "consolidation_divergence"}
            failed_periods = {(error.get("market", "a"), error["code"], error["frequency"])
                              for error in result.get("errors", []) if error.get("frequency")
                              and error.get("signal_type") != "consolidation_divergence"}
            failed_consolidation = {(error.get("market", "a"), error["code"], error.get("frequency"))
                                    for error in result.get("errors", []) if error.get("signal_type") == "consolidation_divergence"}
            if not state.get("holding_source_current"):
                failed_consolidation.update((*symbol.split(":", 1), None)
                                            for symbol in checked if symbol in state["holding_symbols"])
            signals = {identity: item for identity, item in signals.items()
                       if (item.get("market", "a"), item["code"]) not in failed_all_periods
                       and (item.get("market", "a"), item["code"], item["frequency"]) not in failed_periods
                       and not (item.get("signal_type") == "consolidation_divergence" and (
                           (item.get("market", "a"), item["code"], item["frequency"]) in failed_consolidation
                           or (item.get("market", "a"), item["code"], None) in failed_consolidation))}
            self._record_failures(({_symbol_key(error) for error in result.get("errors", [])} | risk_retries) & checked,
                                   stamp, result["run_id"])
            for key in checked:
                if tuple(key.split(":", 1)) not in failed and key not in risk_retries:
                    state["retry_symbols"].pop(key, None)
            previous_signals = {**state["unverified_signals"], **state["signals"]}
            for identity, item in signals.items():
                legacy = item.get("legacy_id")
                if legacy and legacy != identity and legacy in previous_signals:
                    previous_signals[identity] = {**previous_signals.pop(legacy), "id": identity}
                    if identity not in known and legacy in known:
                        known[identity] = {**known[legacy], "notification_state": signal_status_key(item)}
                previous = known.get(identity)
                reclassified=self._historical_reclassification(
                    state['selection_checks'].get(f"{_symbol_key(item)}:{item['frequency']}",{}),
                    result.get('source_revision'),item.get('available_at'))
                notification_authorized = not reclassified
                if item.get("signal_type") == "consolidation_divergence":
                    cycle = (previous or {}).get("lifecycle_cycle", 0)
                    if item["stage"] == "warning" and previous and previous.get("stage") in {"invalidated", "left"}:
                        cycle += 1
                    item["lifecycle_cycle"] = cycle
                    # Upgrade old confirmation-only records without replaying their messages.
                    if previous and previous.get("stage") == "confirmed" and item["stage"] == "confirmed" and "lifecycle_cycle" not in previous:
                        previous["notification_state"] = signal_status_key(item)
                    notification_since = max(state["consolidation_enabled_at"],
                                             state["holding_notification_since"].get(_symbol_key(item), 0))
                    if item["stage"] == "warning" or previous is None:
                        notification_since = max(notification_since, state["consolidation_warning_enabled_at"])
                    notification_authorized &= item["available_at"] >= notification_since
                    recovery_cutoff = (previous or {}).get('warning_recovery_cutoff')
                    if item['stage'] == 'warning':
                        if (previous and previous.get('stage') in {'invalidated','left'}
                                or recovery_cutoff is not None and identity in state['unverified_signals']):
                            recovery_cutoff = item['source_closed_at']
                        item['warning_recovery_cutoff'] = recovery_cutoff
                        item['warning_recovery_pending'] = (recovery_cutoff is not None
                                                            and item['source_closed_at'] <= recovery_cutoff)
                        item['warning_reactivated'] = recovery_cutoff is not None
                        notification_authorized &= not item['warning_recovery_pending']
                notification_state = signal_status_key(item)
                previous_item = previous_signals.get(identity, {})
                changed = previous is None or previous.get("notification_state") != notification_state
                item["first_detected_at"] = (previous or {}).get("first_detected_at") or previous_item.get("first_detected_at")
                if previous is None:
                    item["first_detected_at"] = stamp
                item["state_changed_at"] = stamp if changed else ((previous or {}).get("state_changed_at")
                                                                  or previous_item.get("state_changed_at"))
                item["first_warned_at"] = (previous or {}).get("first_warned_at") or previous_item.get("first_warned_at")
                if item["stage"] == "warning" and (previous is None or previous.get("stage") in {"invalidated", "left"}):
                    item["first_warned_at"] = stamp
                if not item["first_warned_at"] and item.get("signal_type") == "consolidation_divergence":
                    prior_warnings = [e for e in events if e.get("id") in {identity, legacy}
                                      and e.get("stage") == "warning"
                                      and e.get("lifecycle_cycle", 0) == item.get("lifecycle_cycle", 0)]
                    item["first_warned_at"] = min((e["recorded_at"] for e in prior_warnings), default=None)
                if previous is None or (
                    previous["notification_state"] != notification_state if "notification_state" in previous
                    else previous["stage"] != item["stage"] or (
                        item["point_status"] == "confirmed" and item["stage"] != "confirmed")
                ) or (item["stage"] == "warning" and previous.get("notification_authorized") is False
                      and notification_authorized):
                    event = {**item, "recorded_at": stamp, "run_id": result["run_id"],
                             "change": "discovered" if previous is None else "changed"}
                    if item.get("signal_type") == "consolidation_divergence" or reclassified:
                        event["external_notifications"] = notification_authorized
                    if reclassified:
                        event['notification_suppressed_reason']='ALGORITHM_RECLASSIFICATION'
                    events.insert(0, event)
                    if event.get("external_notifications", True):
                        changed_events.append(event)
                known[identity] = {"stage": item["stage"], "notification_state": notification_state, "updated_at": stamp,
                                   "lifecycle_cycle": item.get("lifecycle_cycle", 0),
                                   "notification_authorized": notification_authorized,
                                   "warning_recovery_cutoff": item.get('warning_recovery_cutoff'),
                                   **{k: item.get(k) for k in ("first_detected_at", "first_warned_at", "state_changed_at")}}
            for identity, previous in previous_signals.items():
                if (_symbol_key(previous) in checked and identity not in signals
                        and (previous.get("market", "a"), previous["code"]) not in failed_all_periods
                        and (previous.get("market", "a"), previous["code"], previous["frequency"]) not in failed_periods
                        and not (previous.get("signal_type") == "consolidation_divergence" and (
                            (previous.get("market", "a"), previous["code"], previous["frequency"]) in failed_consolidation
                            or (previous.get("market", "a"), previous["code"], None) in failed_consolidation))):
                    if previous.get("signal_type") == "consolidation_divergence" and previous.get("stage") in {"warning", "invalidated"}:
                        period = next((period for period in result.get("verified_periods", [])
                                       if _symbol_key(period) == _symbol_key(previous)
                                       and period["frequency"] == previous["frequency"]), None)
                        if period is None:
                            continue
                        cutoff = period["source_closed_at"]
                        if previous["stage"] == "warning":
                            item = {**previous, "stage": "invalidated", "point_status": "invalidated",
                                    "selection_status": "invalidated", "source_closed_at": cutoff,
                                    "available_at": cutoff, "invalidated_at": stamp,
                                    'evidence':deepcopy(period.get('evidence')),'evidence_run_id':result['run_id'],
                                    'invalidation_reference':deepcopy(previous),
                                    'current_price_quantum':period.get('structure_price_quantum'),
                                    'current_price_basis_revision':period.get('price_basis_revision'),
                                    "state_changed_at": stamp, **_invalidation_evidence(previous, period)}
                            event = {**item, "change": "changed", "recorded_at": stamp, "run_id": result["run_id"],
                                     "external_notifications": known.get(identity, {}).get("notification_authorized", False)}
                            if self._historical_reclassification(
                                    state['selection_checks'].get(f"{_symbol_key(previous)}:{previous['frequency']}",{}),
                                    result.get('source_revision'),cutoff):
                                event.update(external_notifications=False,
                                             notification_suppressed_reason='ALGORITHM_RECLASSIFICATION')
                            events.insert(0, event)
                            if event["external_notifications"]:
                                changed_events.append(event)
                            known[identity] = {"stage": "invalidated", "notification_state": signal_status_key(item),
                                               "updated_at": stamp, "lifecycle_cycle": item.get("lifecycle_cycle", 0),
                                               **{k: item.get(k) for k in ("first_detected_at", "first_warned_at", "state_changed_at")}}
                            signals[identity] = item
                        elif stamp - previous.get("invalidated_at", 0) < 1800:
                            signals[identity] = {**previous, "source_closed_at": cutoff, "latest_close": period.get("latest_close")}
                        continue
                    rejection = next((r for r in result.get('recent_rejections',[]) if r.get('market','a') == previous.get('market','a')
                        and r.get('code') == previous['code'] and r.get('frequency') == previous['frequency']
                        and r.get('point',{}).get('point_id') == previous.get('point_id')), {})
                    exit_period=next((p for p in result.get('verified_periods',[]) if _symbol_key(p)==_symbol_key(previous)
                                      and p['frequency']==previous['frequency']),{})
                    events.insert(0, {**previous, "stage": "left", "change": "left",
                                      'source_closed_at':exit_period.get('source_closed_at',previous.get('source_closed_at')),
                                      'exit_reasons':rejection.get('reasons') or ['SELECTION_WINDOW_ENDED'],
                                      "recorded_at": stamp, "state_changed_at": stamp, "run_id": result["run_id"]})
                    known[identity] = {"stage": "left", "updated_at": stamp, "state_changed_at": stamp,
                                       "lifecycle_cycle": previous.get('lifecycle_cycle', known.get(identity,{}).get('lifecycle_cycle',0)),
                                       **{key: previous.get(key) or known.get(identity,{}).get(key)
                                          for key in ('first_detected_at','first_warned_at')}}
            # Failed evidence is hidden until a successful check can resolve
            # it. Keep its identity privately so recovery can record a real
            # exit, or restore an unchanged point without another discovery.
            state["unverified_signals"] = {
                identity: item for identity, item in previous_signals.items()
                if _symbol_key(item) in pool and identity not in signals
                and (_symbol_key(item) not in checked and identity in state["unverified_signals"]
                     or _symbol_key(item) in checked and (
                         (item.get("market", "a"), item["code"]) in failed_all_periods
                         or (item.get("market", "a"), item["code"], item["frequency"]) in failed_periods
                         or item.get("signal_type") == "consolidation_divergence" and (
                             (item.get("market", "a"), item["code"], item["frequency"]) in failed_consolidation
                             or (item.get("market", "a"), item["code"], None) in failed_consolidation)))
            }
            # Unchanged/closed markets were not in this batch. Preserve their
            # signals and errors instead of publishing false exits for them.
            signals = {**{k: v for k, v in state["signals"].items()
                          if _symbol_key(v) not in checked and _symbol_key(v) in pool}, **signals}
            errors = [e for e in state.get("errors", []) if _symbol_key(e) not in checked]
            errors.extend(result.get("errors", []))
            valid_cutoffs = {key: value for key, value in state.get("active_cutoffs", {}).items()
                             if key in checked and tuple(key.split(":", 1)) not in failed}
            state["checked_cutoffs"].update(valid_cutoffs)
            for period in result.get("verified_periods", []):
                symbol = _symbol_key(period)
                if (symbol not in checked
                        or (period.get("market", "a"), period["code"]) in failed_all_periods
                        or (period.get("market", "a"), period["code"], period["frequency"]) in failed_periods):
                    continue
                period_key = f"{symbol}:{period['frequency']}"
                identities = [item["id"] for item in signals.values()
                              if _symbol_key(item) == symbol and item["frequency"] == period["frequency"]
                              and item.get("signal_type") != "consolidation_divergence"]
                state["selection_checks"][period_key] = {
                    "symbol": symbol, "cutoff": period["source_closed_at"], "checked_at": stamp,
                    "revision": result.get("source_revision"), "identities": identities, "error": False,
                    "input_fingerprint": period.get('input_fingerprint') or (period.get('evidence') or {}).get('input_fingerprint'),
                }
                if symbol in state['holding_symbols'] and period.get('evidence'):
                    state['holding_evidence'][period_key]={**{k:period[k] for k in (
                        'market','code','name','frequency','source_closed_at','evidence','price_basis_revision') if k in period},
                        'evidence_run_id':result['run_id'],'revision':result.get('source_revision')}
            risk_changes=[]
            for key, value in risk_results.items():
                state['holding_risk_plans'][key] = value['updated']
                for change in value['events']:
                    change.update(run_id=result['run_id'], recorded_at=stamp)
                    events.insert(0,change)
                    risk_changes.append(change)
            for error in result.get("errors", []):
                if error.get("signal_type") == "consolidation_divergence":
                    continue
                symbol = _symbol_key(error)
                if symbol in checked:
                    self._mark_selection_failures({symbol}, stamp, result.get("source_revision"), error.get("frequency"))
            market_cutoffs = state.setdefault("last_cutoffs_by_market", {})
            for key, value in valid_cutoffs.items():
                market = key.split(":", 1)[0]
                market_cutoffs[market] = max(value, market_cutoffs.get(market, 0))
            latest_cutoffs = dict(state.get('last_cutoffs',{}))
            for period in result.get('verified_periods',[]):
                if (_symbol_key(period) in checked and (period.get('market','a'),period['code']) not in failed_all_periods
                        and (period.get('market','a'),period['code'],period['frequency']) not in failed_periods):
                    frequency=period['frequency']
                    latest_cutoffs[frequency]=max(period['source_closed_at'],latest_cutoffs.get(frequency,0))
            through = result.get("results_through", after)
            state["active_consumed_symbols"] = sorted(set(state.get("active_consumed_symbols", [])) | checked)
            if (result["status"] == "completed" or yielded) and through >= result.get("completed", 0):
                state["processed_run"] = result["run_id"]
                state["last_batch_completed_at"] = stamp
                if yielded:
                    unfinished=set(state.get('active_symbols',[]))-set(state['active_consumed_symbols'])
                    waiting=(set(state.get('force_pending_symbols',[]))|unfinished)&pool
                    state.update(force_pending_symbols=sorted(waiting),
                                 pending_symbols_count=max(state.get('pending_symbols_count',0),len(waiting)),
                                 background_empty_yields=0 if state['active_consumed_symbols'] else state.get('background_empty_yields',0)+1,
                                 yielding_monitor_run=None,next_check_at=0)
                    self._wake.set()
                elif state.get('yielding_monitor_run')==result['run_id']:
                    state['yielding_monitor_run']=None
                if not yielded:
                    missing_symbols=(set(state.get('active_symbols',[]))-set(state['active_consumed_symbols']))&pool
                    if missing_symbols:
                        self._mark_selection_failures(missing_symbols,stamp,result.get('source_revision'),reason='RESULT_SYMBOL_MISSING')
                        self._record_failures(missing_symbols,stamp,result['run_id'])
                        for identity,item in list(signals.items()):
                            if _symbol_key(item) in missing_symbols:
                                state['unverified_signals'][identity]=signals.pop(identity)
                        errors.extend({'market':key.split(':',1)[0],'code':key.split(':',1)[1],
                                       'reasons':['RESULT_SYMBOL_MISSING'],'error':'本轮未取得该标的结果，已安排补查'}
                                      for key in sorted(missing_symbols))
                        state['next_check_at']=0
                if state.get('active_monitor_mode')=='background' and state['active_consumed_symbols']:
                    state['background_empty_yields']=0
            if checked:
                state["last_check_at"] = stamp
                state['last_check_notifications']=len(changed_events)+len(risk_changes)
                state['last_check_event_refs']=[signal_reference(e) for e in changed_events+risk_changes]
                if changed_events or risk_changes:
                    state['last_signal_event_at']=stamp
            timelines = {f"{_symbol_key(p)}:{p['frequency']}": p.get('processing_timeline',{})
                         for p in result.get('verified_periods',[])}
            for event in changed_events+risk_changes:
                event['notification_timeline'] = {
                    **timelines.get(f"{_symbol_key(event)}:{event['frequency']}",{}),
                    'source_closed_at':event.get('source_closed_at'),
                    'scheduled_at':state.get('active_scheduled_at',result.get('started_at')),
                    'detected_at':self.now().timestamp(),
                }
            state.update(signals=signals, events=events[:500],
                         known=dict(sorted(known.items(), key=lambda item: item[1]["updated_at"], reverse=True)[:10000]),
                         monitor_cursor={"run_id": result["run_id"], "completed": through},
                         last_cutoffs=latest_cutoffs, errors=errors,
                         last_error="部分标的行情或计算异常，请查看问题列表" if errors else "")
            retain_notifications(state,stamp,changed_events+risk_changes)
            pool_changed = resolve_pending(state, checked, result.get('source_revision'))
            self._save()
            # The durable state is the source of truth for dispatch. A crash
            # before enqueue is recovered by _publish_notifications; enqueue
            # must never expose a fact which exists only in process memory.
            notifications_enabled = state.get('dingtalk_enabled')
            analytics_events = deepcopy(events)
            if after < through < result.get("completed", 0):
                # Preserve bounded reads but drain an already committed
                # backlog without another fixed three-second polling sleep.
                self._wake.set()
        if pool_changed:
            self._sync_watchlist()
        if notifications_enabled and time.monotonic() >= self._notification_retry_at:
            try:
                self._enqueue_pending_notifications()
            except (OSError,sqlite3.Error):
                # The events are already durable in monitor state. A local
                # queue outage must not stop scheduling the next market check.
                self._defer_notification_queue()
        self.analytics.record(analytics_events,result.get('verified_periods',()),run_id=result['run_id'],revision=result.get('source_revision',''))
        if state.get("processed_run") == result["run_id"]:
            self._prune_runs()

    def _prepare_risk_checks(self, result):
        from .holding_risk import check
        if (result.get('status') not in ACTIVE | {'completed'}
                and not (result.get('background_yielded') and result.get('status')=='cancelled')) or not result.get('source_current'):
            return {}
        with self._lock:
            state = self._load()
            if (not state['enabled'] or not state.get('holding_source_current')
                    or result.get('run_id') != state.get('active_monitor_run')
                    or state.get('active_seed') != (state.get('seed') or {}).get('run_id')):
                return {}
            checked = {_symbol_key(row) for row in result.get('processed_symbols', [])}
            plans = {key: deepcopy(plan) for key,plan in state['holding_risk_plans'].items()
                     if plan.get('active') and _symbol_key(plan) in checked & set(state['holding_symbols'])}
            previous = deepcopy(state['selection_checks'])
        periods = {f"{_symbol_key(p)}:{p['frequency']}": p for p in result.get('verified_periods',[])}
        active_ids = {_signal(row)['id'] for row in result.get('selected',[])+result.get('observations',[])}
        output = {}
        for key,plan in plans.items():
            period = periods.get(key)
            failed = any(_symbol_key(error) == _symbol_key(plan)
                         and error.get('frequency') in (None,plan['frequency'])
                         and error.get('signal_type') != 'consolidation_divergence'
                         for error in result.get('errors',[]))
            old = previous.get(key,{})
            regressed = (period and old.get('revision') == result.get('source_revision')
                         and (old.get('cutoff') or 0) > period['source_closed_at'])
            if not period or not period.get('evidence') or failed or regressed:
                updated,events,retry = {**plan,'status':'waiting','detail':'正在等待本周期完整复核'},[],True
            else:
                record = {**period,'evidence_run_id':result['run_id']}
                updated,events,retry = check(plan,self.monitor.root,record,active_ids,self.now().timestamp())
            output[key] = {'original':plan,'updated':updated,'events':events,'retry':retry}
        return output

    def _monitor_state_run_references(self):
        """Collect only durable evidence identities, without copying payloads."""
        with self._lock:
            state = self._load()
            values = [state.get('active_monitor_run')]
            for collection in (state['signals'], state['unverified_signals']):
                for item in collection.values():
                    if item.get('evidence'):
                        if not item.get('evidence_run_id'):
                            raise ValueError('monitor evidence owner is missing')
                        values.append(item.get('evidence_run_id'))
                    values.append((item.get('invalidation_reference') or {}).get('evidence_run_id'))
            values.extend(plan.get('evidence_run_id') for plan in state['holding_risk_plans'].values() if plan.get('active'))
            for record in state['holding_evidence'].values():
                if record.get('evidence') and not record.get('evidence_run_id'):
                    raise ValueError('holding evidence owner is missing')
                values.append(record.get('evidence_run_id'))
            values.extend(evidence_references(pending_events(state, self.now().timestamp()).values()))
            if any(value and (not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value)) for value in values):
                raise ValueError('monitor evidence reference is unknown')
            return {value for value in values if value}

    def _monitor_run_references(self):
        referenced = self.notifications.referenced_runs()
        if not isinstance(referenced, (set, list, tuple)) or any(
                not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value) for value in referenced):
            raise ValueError('notification evidence references are unknown')
        latest = self.monitor.status().get('run_id')
        if latest is not None and (not isinstance(latest, str) or not re.fullmatch(r'[a-f0-9]{32}', latest)):
            raise ValueError('latest monitor identity is unknown')
        # Read state after the external reference lookup: a notification may
        # have moved between the durable handoff journal and outbox meanwhile.
        return {latest, *referenced, *self._monitor_state_run_references()}

    def _prune_runs(self):
        from .screening_retention import TERMINAL
        try:
            root = self.monitor.root.resolve()
            if not root.is_dir():
                return
            runs = []
            for path in root.iterdir():
                try:
                    if (re.fullmatch(r'[a-f0-9]{32}', path.name) and path.is_dir()
                            and not path.is_symlink() and path.resolve().parent == root):
                        runs.append((path.stat().st_mtime_ns, path))
                except OSError:
                    continue
            runs.sort(key=lambda row: row[0], reverse=True)
            if len(runs) <= 3:
                return
            protected = self._monitor_run_references()
        except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
            LOG.warning('monitor run cleanup deferred: %s', type(exc).__name__)
            return
        for modified, path in runs[3:]:
            if path.name in protected:
                continue
            try:
                saved = read_state_json(path / 'status.json')
                if (not isinstance(saved, dict) or saved.get('run_id') != path.name
                        or not isinstance(saved.get('status'), str) or saved['status'] not in TERMINAL):
                    continue
            except (OSError, ValueError, TypeError) as exc:
                LOG.warning('monitor run metadata retained %s: %s', path.name, type(exc).__name__)
                continue
            try:
                protected = self._monitor_run_references()
            except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error) as exc:
                LOG.warning('monitor run cleanup deferred: %s', type(exc).__name__)
                return
            if path.name in protected:
                continue
            try:
                if (path.is_symlink() or path.resolve().parent != root
                        or path.stat().st_mtime_ns != modified
                        or read_state_json(path / 'status.json') != saved
                        or path.name in self._monitor_state_run_references()):
                    continue
                # Resolve and restrict every recursive delete to this service's
                # own root; uncertain metadata or references never authorize it.
                shutil.rmtree(path)
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                LOG.warning('could not prune monitor run %s: %s', path.name, type(exc).__name__)


service = SignalMonitor()
