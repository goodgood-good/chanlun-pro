"""After-close screening and monitoring of candidates plus the live watchlist.

The scheduler only coordinates the existing screening engine. Calculations run
in its detached, bounded worker processes, with a separate results directory.
"""

from __future__ import annotations

from copy import deepcopy
import math
from collections import Counter
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import threading
import time
from uuid import uuid4

from chanlun import config
from chanlun.screening.rules import CN, POINT_TYPES
from chanlun.screening.compute_capacity import (cpu_capacity, MAX_WORKERS,
                                               budget_request, publish_monitor_demand, role_limits)
from chanlun.screening.runner import write_json
from chanlun.screening.markets import closed_bar_cutoff, holding_symbols, watchlist_symbols
from .screening import ScreeningManager, MonitorResultsReadError, manager as screening_manager, read_state_json, validate_settings, all_periods_failed
from .dingtalk_notifications import signal_status_key, signal_reference
from .notification_handoff import retain as retain_notifications, pending_events, evidence_references
from .notification_freshness import notification_deadline, notification_is_live, stamp_notification, NOTIFICATION_LIFETIME
from .notification_text import REASONS, price as notification_price
from .monitor_view import validate_page_options
from .notification_policy import (
    default_point_notifications, normalize_point_notifications,
    next_point_notification_since, point_notification_decision,
    trade_notification_rejection,
)
from .screening_pool_recovery import independent_members, pending_members, suspend_pending, resolve_pending
from .monitor_scope import build_minute_scope_index, minute_cadence, record_minute_checkpoint, independent_monitor_periods
from . import monitor_requests
from .monitor_exclusions import quote_only_symbols
from .monitor_fairness import plan_batch, task_lane, SERVICE_PATTERN, remember_pending, record_costs, service_pressure, live_batch_waves
from .monitor_maintenance import prune_diagnostics, recover_warning_rechecks, basis_change_exit, notification_priority_periods, warning_recheck_symbols


ACTIVE = {"starting", "running"}
MONITOR_BATCH_SIZE = 64
MONITOR_WORKER_WAVES = 4
MONITOR_BACKGROUND_WAVES = 2
MONITOR_MAX_FAILURES = 3
MONITOR_READ_ATTEMPTS = 3
MONITOR_LIVE_SLICE_SECONDS = 30
MONITOR_HOLDING_WAIT_SECONDS = 5
MORNING_AFTER_CLOSE = "11:35"
LOG = logging.getLogger(__name__)
_PRIVATE_SNAPSHOT_FIELDS = frozenset({
    'point_notification_since',
    'pending_notifications', 'known', 'last_attempt_key', 'force',
    'unverified_signals', 'holding_evidence', 'holding_risk_plans', 'startup_errors', 'last_check_event_refs',
    'minute_cadence_checkpoints', 'minute_force_pending', 'active_minute_override_tokens',
    'minute_cadence_policy_version',
    'monitor_pending_deadlines',
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


def _bound_risk_periods(state, key):
    return {plan['frequency'] for plan in state.get('holding_risk_plans',{}).values()
            if plan.get('active') and _symbol_key(plan)==key}


def _monitor_periods(state, key):
    return independent_monitor_periods(state, key)-set(state.get('suspended_periods',{}).get(key,{}))


def _monitor_frequency(state, symbol, default='5m'):
    frequencies = _monitor_periods(state, _symbol_key(symbol))
    return next((f for f in ('1m','5m','30m') if f in frequencies),default)


def _available_monitor_periods(state, key, revision):
    """Only a known, unchanged source contract can suspend an automatic retry."""
    blocked = state.get('contract_blocks', {})
    return {frequency for frequency in _monitor_periods(state, key)
            if not revision or blocked.get(f'{key}:{frequency}', {}).get('revision') != revision}


def monitor_settings(state, source_settings):
    """Apply explicit monitoring periods without rewriting saved scan results."""
    settings = deepcopy(source_settings)
    from chanlun.screening.timeframe_policy import enabled, LIVE_PERIODS, POLICY
    if enabled(settings) or enabled(state.get('settings', {}).get('screening', {})):
        settings['timeframe_policy'] = POLICY
        settings['frequencies'] = list(LIVE_PERIODS)
        settings.pop('minimum_operating_frequency', None)
        return settings
    if state.get('monitor_frequencies'):
        settings['frequencies'] = list(state['monitor_frequencies'])
        if settings.get('minimum_operating_frequency') and settings['frequencies'] != [settings['minimum_operating_frequency']]:
            settings.pop('minimum_operating_frequency')
    return settings


def configure_periods(state, frequencies, stamp):
    """Persist a period change and establish notification cutoffs for additions."""
    base = deepcopy(state['settings']['screening'])
    from chanlun.screening.timeframe_policy import enabled, LIVE_PERIODS
    if enabled(base):
        requested = {**base, 'frequencies': frequencies}
        requested.pop('minimum_operating_frequency', None)
        validate_settings(requested)
        frequencies = list(LIVE_PERIODS)
    base['frequencies'] = frequencies
    if base.get('minimum_operating_frequency') and frequencies != [base['minimum_operating_frequency']]:
        base.pop('minimum_operating_frequency')
    validated = validate_settings(base)
    seed = state.get('seed') or {}
    previous = set(seed.get('settings', state['settings']['screening']).get('frequencies', []))
    added = set(validated['frequencies']) - previous
    removed = previous - set(validated['frequencies'])
    state['monitor_frequencies'] = list(validated['frequencies'])
    state['settings']['screening'] = validated
    if seed:
        seed.setdefault('screening_frequencies', list(seed['settings']['frequencies']))
        seed['settings'] = monitor_settings(state, seed['settings'])
    for frequency in added:
        state.setdefault('notification_frequency_since', {})[frequency] = stamp
    if removed:
        # Controls are authoritative immediately, including while an old worker
        # is still returning. A separately bound holding risk period stays live.
        for name in ('signals', 'unverified_signals', 'pending_notifications'):
            state[name] = {key: item for key, item in state.get(name, {}).items()
                           if item.get('frequency') not in removed
                           or item.get('signal_type') == 'holding_risk'}
        for name in ('selection_checks', 'holding_evidence', 'contract_blocks'):
            state[name] = {key: item for key, item in state.get(name, {}).items()
                           if key.rsplit(':', 1)[-1] not in removed
                           or state.get('holding_risk_plans', {}).get(key, {}).get('active')}
        state['monitor_pending_deadlines'] = {key: value for key, value in state.get('monitor_pending_deadlines', {}).items()
            if key.rsplit(':', 1)[-1] not in removed
            or key.rsplit(':', 1)[-1] in _bound_risk_periods(state, key.rsplit(':', 1)[0])}
        state['errors'] = [item for item in state.get('errors', [])
                           if item.get('frequency') not in removed
                           or state.get('holding_risk_plans', {}).get(
                               f"{_symbol_key(item)}:{item.get('frequency')}", {}).get('active')]
    if added or removed:
        # Existing successful checks and notification identities stay intact;
        # only scheduling clocks are cleared so the new periods get a first pass.
        for symbol in seed.get('codes', []):
            state.setdefault('attempted_cutoffs', {}).pop(symbol, None)
        state['last_attempt_key'] = None
        state['next_check_at'] = 0
        state['pending_symbols_count'] = len(seed.get('codes', []))
        state['deferred_monitor_periods'] = {}
        state['attempted_monitor_periods'] = {}


def _recent_clock(now, closed, frequency):
    return 0<=now.timestamp()-closed<=int(frequency[:-1])*60


def _brief(status):
    keys = ("run_id", "status", "completed", "total", "error", "error_count",
            "progress_unit", "total_symbols", "completed_symbols",
            "selected_count", "phase", "cutoffs", "source_current", "freshness_state",
            "cancel_requested", "elapsed_seconds", "current_codes")
    return {key: status[key] for key in keys if key in status}


def _period_retries(retry):
    """Keep legacy symbol retries readable; new failures own a period clock."""
    if not retry:
        return {}
    if retry.get('by_period'):
        return dict(retry['by_period'])
    periods = retry.get('periods') or [None]
    return {period: retry for period in periods}


def _store_period_retries(state, key, records):
    if not records:
        state['retry_symbols'].pop(key, None)
    elif len(records) == 1:
        state['retry_symbols'][key] = next(iter(records.values()))
    else:
        # Retain the public symbol summary while each failed period owns its
        # deadline and attempt budget. Only enabled physical periods enter here.
        representative = min(records.values(), key=lambda item: item.get('retry_at') or float('inf'))
        state['retry_symbols'][key] = {**representative, 'periods': sorted(records), 'by_period': records}


def _clear_period_retries(state, key, periods):
    records = {period: retry for period, retry in _period_retries(state['retry_symbols'].get(key, {})).items()
               if period is not None and period not in periods}
    _store_period_retries(state, key, records)


def _signal_identity(row):
    identity = json.dumps([row.get('market', 'a'), row['code'], row['frequency'], row['point']['point_id']])
    return hashlib.sha256(identity.encode()).hexdigest()


def _signal(row):
    point = row["point"]
    from chanlun.core.strict_structure.native_point_preview import is_point_preview
    nested = row.get("nested_confirmation") or {}
    following = row.get("confirmation_segment") or {}
    witness = point.get('preview_witness') or {}
    return {
        "id": _signal_identity(row),
        "code": row["code"], "market": row.get("market", "a"), "name": row.get("name", row["code"]),
        "frequency": row["frequency"], "point_type": point["point_type"],
        "timeframe_policy": row.get('timeframe_policy'),
        "operating_role": row.get('operating_role'),
        "background_30m": deepcopy(row.get('background_30m')),
        "nested_confirmation": deepcopy(row.get('nested_confirmation')),
        "confirmation_evidence": deepcopy(row.get('confirmation_evidence')),
        "point_id": point["point_id"],
        **({'preview_key':point['preview_key']} if point.get('preview_key') else {}),
        **({'signal_type':'native_point_preview','formal_point_issued':False} if is_point_preview(point) else {}),
        **({key:witness.get(key) for key in ('preview_trigger','estimate_factor','estimated_area_c',
                                            'macd_area_a','macd_area_c')} if is_point_preview(point) else {}),
        'price_basis_revision': point.get('price_basis_revision'),
        'strength_source': point.get('strength_source'),
        'strength_frequency': (point.get('divergence') or witness).get('strength_frequency'),
        'confirmation_delay_bars': row.get('confirmation_delay_bars'),
        "evidence_scope": point.get("evidence_scope"),
        "center_ordinal": ((row.get('center_sequence') or {}).get('ordinal')
                           if point.get('evidence_scope') == 'native_center_signal' else point.get('center_ordinal')),
        'center_sequence': deepcopy(row.get('center_sequence')),
        "stage": row.get("selection_status", point["status"]),
        "point_status": point["status"],
        "point_available_at": point.get("available_at"),
        "point_confirmed_at": point.get("confirmed_at"),
        "selection_status": row.get("selection_status", point["status"]),
        "lower_confirmation_state": nested.get("state"),
        'lower_check_state': nested.get('check_state'),
        'lower_assessment': deepcopy(nested.get('assessment')),
        'lower_evidence_type': nested.get('evidence_type'),
        "lower_frequency": nested.get("frequency"),
        "following_segment_state": following.get("state"),
        "following_segment_direction": (following.get("segment") or {}).get("direction"),
        "anchor_price": point.get("anchor_price"),
        "anchor_at": point.get("anchor_at"), "center_id": point.get("center_id"),
        "latest_close": row.get("latest_close"), "structure_price_quantum": row.get("structure_price_quantum"),
        "exit_plan": deepcopy(row.get("exit_plan")), "evidence": deepcopy(row.get("evidence")),
        "available_at": (row.get('selection_available_at', point.get("available_at"))
                         if row.get('timeframe_policy') else point.get('available_at')),
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
        "strength_source": alert.get('strength_source'), "strength_frequency": alert.get('strength_frequency'),
        "macd_area_ratio": alert["macd_area_ratio"], "core_position": alert["core_position"],
        "comparison_form": alert["comparison_form"], "holding": True,
        "estimated_area_c": alert.get("estimated_area_c"),
        "estimate_factor": alert.get('estimate_factor'), "preview_trigger":alert.get('preview_trigger'),
        "preview_conditions": ("当前同向柱开始缩短；累计面积×2仍小于参考段；结构尚未完成"
            if alert.get('preview_trigger') == 'macd_first_contraction' else
            "累计面积的两倍仍小于参考段；已收盘MACD连续两次收缩；局部价格转折") if status == "warning" else None,
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
    def __init__(self, root=None, *, screening=None, monitor=None, now=None, notifications=None, compute_budget=None):
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
        self._source_prefetch_control = None
        self._compute_budget = compute_budget
        self._publish_compute_budget = root is None or compute_budget is not None
        self._compute_budget_owner = uuid4().hex
        self._busy_poll = False
        self._last_compute_limits = {}
        # Versions distinguish new requests from work already being launched.
        self._check_requests = {}
        self._notification_rechecks = {}
        self._notification_retry_after = {}
        from .notification_validation import DependencyReads
        self._notification_dependency_reads = DependencyReads(wake=self._wake_notification_validation)
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
        from .monitor_incremental import CommittedReader
        self._incremental_reader = CommittedReader(self._wake.set)
        self._monitor_result_reader = CommittedReader(self._wake.set)
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
                                                         signal_validator=self._validate_notification_events, charts=True,
                                                         holding_reader=self._notification_holding_keys)
        return self._notifications

    def _notification_holding_keys(self):
        """A bounded membership snapshot for queue order, never send approval."""
        if not self._lock.acquire(timeout=.02):
            return None
        try:
            state = self._state
            if not state or state.get('holding_source_current') is not True:
                return None
            return set(state.get('holding_symbols', []))
        except (TypeError, ValueError):
            return None
        finally:
            self._lock.release()

    def _validate_notification_events(self, events):
        """Yield slow dependencies; only current state may authorize sending."""
        from .notification_validation import validation_deadline, remaining, holding_lane, DeferredEvents, state_epoch, ValidatedEvents
        quote_only = quote_only_symbols()
        if events and all(f"{event.get('market', 'a')}:{event.get('code', '')}" in quote_only for event in events):
            rejected = ValidatedEvents()
            for event in events:
                rejected.reject(event, 'OUTSIDE_MONITOR_SCOPE')
            return rejected, []
        deadline = validation_deadline()
        # Loading a cold state can perform recovery I/O. Runtime startup owns
        # that work; a queued notification waits instead of doing it here.
        if not self._lock.acquire(timeout=remaining(deadline)):
            return [], DeferredEvents(events)
        try:
            if self._state is None:
                return [], events
            events = [event for event in events
                      if notification_is_live(event, self.now().timestamp())
                      and point_notification_decision(self._state, event, None) != 'deny']
            if not events:
                return [], []
            scan_types = set((self._state.get('seed') or {}).get('settings', {}).get('point_types', []))
            epoch = state_epoch(self._state, self._control_version, events)
        finally:
            self._lock.release()
        frozen_events = deepcopy(events)
        observed_minute = int(self.now().timestamp()) // 60
        dependency_key = hashlib.sha256(json.dumps(
            [frozen_events, epoch, observed_minute],
            sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        dependencies = self._notification_dependency_reads.read(
            'holding' if holding_lane(events) else 'ordinary',
            lambda: self._read_notification_dependencies(frozen_events, scan_types), deadline,
            key=dependency_key, context_key=(epoch, observed_minute))
        if dependencies is None:
            return [], DeferredEvents(events)
        source, fresh_holdings, expected_cutoffs, observed_minute = dependencies
        revision = source.get('current_source_revision') or (
            source.get('source_revision') if source.get('source_current') is True else None)
        version_known = bool(revision) or source.get('source_current') is True
        if not self._lock.acquire(timeout=remaining(deadline)):
            return [], DeferredEvents(events)
        try:
            if (self._state is None or not remaining(deadline) or expected_cutoffs
                    and int(self.now().timestamp()) // 60 != observed_minute):
                return [], DeferredEvents(events)
            if epoch != state_epoch(self._state, self._control_version, events):
                return [], DeferredEvents(events)
            # Dependency workers never mutate signals or request rechecks.
            # Re-read the live state here after all dependency I/O completes.
            validated = self._validate_notification_state(events, revision, version_known, fresh_holdings, expected_cutoffs)
        finally:
            self._lock.release()
        # Context is auxiliary and asynchronous. It cannot hold the monitor
        # lock or delay a current primary warning while peer periods load.
        from .higher_context import event_context
        valid, pending = validated
        for event in valid:
            if (event.get('stage') in {'approaching','confirmed'} and event.get('point_type') in POINT_TYPES
                    and event.get('evidence_scope')=='native_center_signal'):
                try:
                    context=event_context(self.screening,event,revision)
                    if context:event['higher_context']=context
                except (OSError,ValueError,TypeError,KeyError):
                    pass
        return valid, pending

    def _read_notification_dependencies(self, events, scan_types):
        """Read-only phase; only its original request can resume a recent result."""
        observed = self.now()
        monitor = self.monitor
        reader = getattr(monitor, 'notification_source_status', None)
        source = reader() if callable(reader) else monitor.status()
        fresh_holdings = None
        if any(event.get("signal_type") in {"consolidation_divergence","holding_risk"}
               or event.get('point_type') in POINT_TYPES
               or event.get('holding') or event.get('point_type') not in scan_types for event in events):
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
                    event.get('market', 'a'), event['code'], observed, event.get('frequency'))
            except (ValueError, OSError, TypeError):
                expected_cutoffs[key] = None
        return source, fresh_holdings, expected_cutoffs, int(observed.timestamp()) // 60

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
        from .notification_validation import ValidatedEvents
        from .notification_freshness import notification_rejection
        valid, pending = ValidatedEvents(), []
        for event in events:
            if not notification_is_live(event, self.now().timestamp()):
                valid.reject(event,notification_rejection(event,self.now().timestamp()))
                continue
            key = _symbol_key(event)
            if key in quote_only_symbols():
                valid.reject(event, 'OUTSIDE_MONITOR_SCOPE')
                continue
            policy_decision = point_notification_decision(state, event, fresh_holdings)
            if policy_decision == 'deny':
                valid.reject(event,'NOTIFICATION_POLICY_DISABLED')
                continue
            if policy_decision == 'pending':
                pending.append(event)
                continue
            frequency = event.get("frequency")
            from chanlun.screening.timeframe_policy import enabled
            rejection = trade_notification_rejection(event, policy_enabled=enabled(seed.get('settings', {})))
            if rejection:
                valid.reject(event, rejection)
                continue
            awaiting_commit = bool(recovering_run and event.get('run_id') == recovering_run)
            holding_only = (event.get('signal_type') in {'holding_risk', 'consolidation_divergence'}
                            or event.get('point_type') not in seed.get('settings', {}).get('point_types', []))
            if holding_only and holding_current and key not in state['holding_symbols']:
                valid.reject(event,'HOLDING_REMOVED')
                continue
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
                valid.reject(event,'OUTSIDE_MONITOR_SCOPE')
                continue
            admission_at = event.get('recorded_at') if event.get('signal_type') == 'native_point_preview' else event.get('available_at')
            if (admission_at or 0) < max(
                    state.get('notification_frequency_since', {}).get(frequency, 0),
                    state.get('notification_symbol_since', {}).get(key, 0)):
                valid.reject(event,'BEFORE_NOTIFICATION_ENABLED')
                continue
            consolidation = event.get("signal_type") == "consolidation_divergence"
            if consolidation:
                if fresh_holdings is not None and key not in fresh_holdings:
                    valid.reject(event,'HOLDING_REMOVED')
                    continue
                if fresh_holdings is None or not holding_current:
                    pending.append(event)
                    continue
                since = max(state.get('consolidation_enabled_at', 0),
                            state.get('holding_notification_since', {}).get(key, 0))
                if event.get('stage') == 'warning':
                    since = max(since, state.get('consolidation_warning_enabled_at', 0))
                if (event.get('available_at') or 0) < since:
                    valid.reject(event,'BEFORE_NOTIFICATION_ENABLED')
                    continue
            elif event.get("point_type") not in seed.get("settings", {}).get("point_types", []):
                if event.get('point_type') not in POINT_TYPES:
                    continue
                if not holding_current or fresh_holdings is None:
                    if key in state['holding_symbols'] or event.get('holding'):
                        pending.append(event)
                    continue
                if key not in fresh_holdings:
                    continue
            if not version_known or event.get("id") in uncertain:
                expected=expected_cutoffs.get((key,frequency))
                if expected is not None:
                    self._request_notification_recheck(event,expected,revision)
                pending.append(event)
                continue
            item = current.get(event.get("id"))
            if item is None and consolidation:
                item = next((row for row in current.values() if row.get("legacy_id") == event.get("id")), None)
            if item is None and not consolidation:
                # A queued ordinary point can outlive its in-memory view while
                # a cold period awaits revalidation. Missing state is not a
                # current negative proof, and must never become a new signal.
                from .dingtalk_notifications import eligible_signal_events, live_signal_events
                stamp = self.now().timestamp()
                stages = {'approaching','invalidated'} if event.get('signal_type') == 'native_point_preview' else {'confirmed'}
                if (event.get('stage') not in stages
                        or event.get('point_status', event.get('stage')) not in stages
                        or not live_signal_events([event], stamp)
                        or not eligible_signal_events([event], since=state.get('notification_since', 0), stamp=stamp)):
                    continue
                check = checks.get(f'{key}:{frequency}', {})
                expected = expected_cutoffs.get((key, frequency))
                negative = (expected is not None and check.get('error') is False
                            and check.get('revision') == revision
                            and type(check.get('cutoff')) is int and check['cutoff'] >= expected
                            and isinstance(check.get('identities'), list) and event['id'] not in check['identities'])
                known = state['known'].get(event['id'], {})
                newer = (known.get('updated_at') or 0) >= event['recorded_at']
                superseded = newer and (known.get('stage') not in (None, event.get('stage'))
                    or known.get('notification_authorized') is False
                    or known.get('notification_state') not in (None, signal_status_key(event)))
                if negative or superseded:
                    valid.reject(event,'SIGNAL_EXIT_CONFIRMED' if negative else 'SIGNAL_SUPERSEDED')
                    continue
                if expected is not None:
                    self._request_notification_recheck(event, expected, revision)
                pending.append(event)
                continue
            allowed = ({'approaching','invalidated'} if event.get('signal_type') == 'native_point_preview' else
                       {"warning", "confirmed", "invalidated"} if consolidation else {"confirmed"})
            legacy_confirmation = (consolidation and "hypothesis_id" not in event and "lifecycle_cycle" not in event
                                   and event.get("stage") == "confirmed" and item is not None
                                   and item.get("stage") == "confirmed")
            if (item is None or item.get("stage") not in allowed
                    or item.get("point_status", item.get("stage")) not in allowed
                    or not legacy_confirmation and signal_status_key(item) != signal_status_key(event)):
                if awaiting_commit:
                    pending.append(event)
                else:
                    valid.reject(event,'SIGNAL_STATE_CHANGED')
                continue
            check = checks.get(f"{key}:{frequency}")
            expected = expected_cutoffs.get((key, frequency))
            if expected is None:
                pending.append(event)
                continue
            if (not check or check.get("error") or check.get("revision") != revision
                    or (check.get("cutoff") or 0) < expected
                    or item.get("source_closed_at") != check.get("cutoff")):
                self._request_notification_recheck(event, expected, revision)
                pending.append(event)
                continue
            if not consolidation and item["id"] not in check.get("identities", []):
                valid.reject(event,'SIGNAL_IDENTITY_REJECTED')
                continue
            if (item.get('point_type') == '3buy' and item.get('evidence_scope') == 'native_center_signal'
                    and item.get('stage') != 'invalidated'):
                from .first_center_sequence import first_verified, SCHEMA
                sequence = item.get('center_sequence')
                if (not first_verified(sequence) or sequence.get('direction') != 'up'
                        or sequence.get('center_id') != item.get('center_id')
                        or sequence.get('known_at') != item.get('point_available_at', item.get('available_at'))):
                    if not isinstance(sequence, dict) or sequence.get('schema') != SCHEMA:
                        self._request_notification_recheck(event, expected, revision)
                        pending.append(event)
                    else:
                        valid.reject(event,'FIRST_CENTER_POLICY_REJECTED')
                    continue
            delivered = deepcopy({**event, **item})
            if fresh_holdings is not None:
                delivered['holding'] = key in fresh_holdings
            valid.append(delivered)
        return valid, pending

    def _request_notification_recheck(self, event, cutoff, revision):
        """A pending valid notification requests a bounded, fresh recheck."""
        if not notification_is_live(event, self.now().timestamp()):
            return
        key=(_symbol_key(event),event['frequency'])
        value={'cutoff':cutoff,'revision':revision,'expires':notification_deadline(event)}
        previous=self._notification_rechecks.get(key)
        if previous and previous['revision']==revision:
            value['cutoff']=max(cutoff,previous['cutoff'])
            value['expires']=max(value['expires'],previous['expires'])
        if self._notification_rechecks.get(key)!=value:
            self._notification_rechecks[key]=value
            self._wake.set()

    def _pending_notification_symbols(self, state, now):
        pool=set((state.get('seed') or {}).get('codes',[]))
        for key, request in list(self._notification_rechecks.items()):
            symbol, frequency=key
            check=state['selection_checks'].get(f'{symbol}:{frequency}',{})
            complete=(not check.get('error') and check.get('revision')==request['revision']
                      and (check.get('cutoff') or 0)>=request['cutoff'])
            identities = check.get('identities')
            if complete and isinstance(identities, list) and identities:
                # A positive period proof with a missing/stale materialized
                # signal still needs repair; it cannot acknowledge that repair.
                complete = all(identity in state['signals']
                               and state['signals'][identity].get('source_closed_at') == check.get('cutoff')
                               for identity in identities)
            if symbol not in pool or frequency not in _monitor_periods(state, symbol) or request['expires']<=now or complete:
                self._notification_rechecks.pop(key,None)
        urgent={symbol for symbol,_ in self._notification_rechecks}
        self._notification_retry_after={key:value for key,value in self._notification_retry_after.items() if key in urgent}
        return urgent

    def _load(self):
        if self._state is None:
            path = self.root / "state.json"
            recovered = False
            if path.is_file():
                try:
                    self._state = read_state_json(path)
                except (ValueError, UnicodeError):
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
            self._state.setdefault('contract_blocks', {})
            self._state.setdefault("unverified_signals", {})
            self._state.setdefault("holding_symbols", [])
            self._state.setdefault('holding_risk_plans',{})
            self._state.setdefault('holding_evidence',{})
            self._state.setdefault("selection_checks", {})
            self._state.setdefault("holding_notification_since", {})
            self._state.setdefault('point_notifications', default_point_notifications())
            self._state.setdefault('point_notification_since', {})
            self._state.setdefault('point_notification_version', 0)
            policy_migrated = False
            try:
                saved_policy = self._state['point_notifications']
                policy = normalize_point_notifications(saved_policy, legacy_ordinary_sells=True)
                self._state['point_notifications'] = policy
                if (not isinstance(self._state['point_notification_since'], dict)
                        or any(not isinstance(key, str) or type(value) not in (int, float)
                               or not math.isfinite(value) or value < 0
                               for key, value in self._state['point_notification_since'].items())
                        or type(self._state['point_notification_version']) is not int
                        or self._state['point_notification_version'] < 0):
                    raise ValueError('通知策略版本或起点无效')
                from .notification_policy import BUY_POINTS
                # Remove only ordinary sell delivery; preserve selected buys,
                # holding choices and activation windows across the upgrade.
                upgraded = {**policy, 'others': [point for point in policy['others'] if point in BUY_POINTS]}
                if 'other_frequencies' not in saved_policy or upgraded != policy:
                    self._state.update(point_notifications=upgraded,
                        point_notification_since=next_point_notification_since(policy, upgraded,
                            self._state['point_notification_since'], self.now().timestamp()),
                        point_notification_version=self._state['point_notification_version'] + 1)
                    policy_migrated = True
            except ValueError:
                self._state['point_notifications'] = {key: [] for key in default_point_notifications()}
                self._state['point_notification_since'] = {}
                self._state['point_notification_version'] = 0
                self._state['point_notification_error'] = '已暂停买卖点通知：保存的通知条件无效，请重新设置。'
            self._state.setdefault("consolidation_enabled_at", int(self.now().timestamp()))
            self._state.setdefault("consolidation_warning_enabled_at", int(self.now().timestamp()))
            self._state["settings"].setdefault("morning_after_close", MORNING_AFTER_CLOSE)
            self._state['settings'].setdefault('monitor_workers',MAX_WORKERS)
            from chanlun.screening.timeframe_policy import POLICY, LIVE_PERIODS, SCAN_PERIODS
            if self._state['settings']['screening'].get('timeframe_policy') != POLICY:
                base = {**self._state['settings']['screening'], 'timeframe_policy': POLICY,
                        'frequencies': list(SCAN_PERIODS)}
                base.pop('minimum_operating_frequency', None)
                self._state['settings']['screening'] = validate_settings(base)
                seed = self._state.get('seed') or {}
                if seed:
                    seed['settings'] = monitor_settings(self._state, {**seed['settings'], 'timeframe_policy': POLICY})
                # Former independent 5m/30m points await the new joint proof.
                # Existing watch/holding membership and notification switches survive.
                for name in ('signals', 'unverified_signals', 'pending_notifications'):
                    values = self._state.get(name, {})
                    self._state[name] = {key: value for key, value in values.items()
                        if value.get('point_type') not in POINT_TYPES or value.get('frequency') == '1m'}
                self._state['force'] = True
                self._state['next_check_at'] = 0
                self._state['timeframe_policy_updated_at'] = self.now().timestamp()
                self._state['monitor_frequencies'] = list(LIVE_PERIODS)
                now = self.now()
                clock = now.strftime('%H:%M')
                phase = ('afternoon' if clock >= self._state['settings']['after_close'] else
                         'morning' if self._state['settings']['morning_after_close'] <= clock < '15:00' else None)
                daily = self._state.get('daily') or {}
                if phase and daily.get('session') == now.date().isoformat() and daily.get(phase):
                    daily[phase].update(status='updating', attempts=0, retry_at=0,
                                        refresh_reason='timeframe_policy_updated')
            if self._state.get('compute_budget_version') != 1:
                # Upgrade the old default once; smaller explicit budgets survive.
                if self._state['settings'].get('screening',{}).get('workers') == 12:
                    self._state['settings']['screening']['workers'] = MAX_WORKERS
                self._state['compute_budget_version'] = 1
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
            maintenance = recover_warning_rechecks(self._state, _clear_period_retries)
            maintenance |= prune_diagnostics(self._state)
            if policy_migrated or maintenance:
                self._save()
        return self._state

    def _save(self):
        try:
            prune_diagnostics(self._state)
            from chanlun.screening.source_prefetch import control_value, publish_control
            control = control_value(self._state, self._control_version,
                                    os.environ.get('CHANLUN_BUILD_REVISION'))
            # Revoke stale source hints before publishing a changed scope.
            # Ordinary signal commits leave this small control file untouched.
            self._source_prefetch_control = publish_control(
                self.root / 'runs', control, getattr(self, '_source_prefetch_control', None))
            from chanlun.persistence.state_snapshot import compact_state
            snapshot=compact_state(self._state)
            write_json(self.root / "state.json", snapshot, durable=True)
            write_json(self.root / "state.backup.json", snapshot, durable=True)
        except Exception:
            # Subsequent readers must reload what actually reached disk, never
            # publish events left only in memory after a failed commit. This
            # also covers encoding/compression failure before either write.
            self._state = None
            raise

    def snapshot(self, *, page_options=None):
        # Read-only: opening a page never enables or starts a scan.
        manual_status, monitor_status = self.screening.status(), self.monitor.status()
        revision = monitor_status.get("current_source_revision") or manual_status.get("current_source_revision")
        if not revision and monitor_status.get("source_current") is not False:
            revision = monitor_status.get("source_revision")
        expected_cutoffs={}
        if page_options is not None:
            with self._lock:
                seed=self._load().get('seed') or {}
                identities=[(s.get('market','a'),s['code']) for s in seed.get('symbols',[])]
                periods={f'{market}:{code}': _monitor_periods(self._load(), f'{market}:{code}') for market,code in identities}
            observed=self.now()
            # Calendar work must not hold the monitor's shared state lock.
            for market, code in identities:
                for period in periods[f'{market}:{code}']:
                    try:value=closed_bar_cutoff(market,code,observed,period)
                    except (ValueError,OSError,TypeError):value=None
                    expected_cutoffs[f'{market}:{code}:{period}']=value
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
            from .monitor_scope import minute_scope_summary
            minute_scope = minute_scope_summary(raw, revision, {
                key.rsplit(':', 1)[0]: value for key, value in expected_cutoffs.items() if key.endswith(':1m')})
            if page_options is None:
                state = deepcopy({k:v for k,v in self._load().items() if k not in _PRIVATE_SNAPSHOT_FIELDS})
                if state.get('seed'):
                    for symbol in state['seed'].get('symbols', []):
                        symbol['minute_monitoring'] = minute_scope.get('per_symbol', {}).get(
                            _symbol_key(symbol), {}).get('classification')
            else:
                from .monitor_view import paged_state
                state = paged_state(self._load(), page_options, revision, monitor_status.get('source_current'),
                                    expected_cutoffs=expected_cutoffs, minute_scope=minute_scope)
            state['minute_scope'] = {key: value for key, value in minute_scope.items() if key != 'per_symbol'}
            state['scheduling'] = self.scheduling_activity()
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
                from .monitor_view import period_check_state
                try:target=closed_bar_cutoff(market,code,self.now(),frequency)
                except (ValueError,OSError,TypeError):target=None
                progress.append({'frequency':frequency,'cutoff':check.get('cutoff'),'checked_at':check.get('checked_at'),
                                 'status':period_check_state(check,revision,target)})
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
                unchanged=(existing and existing.get('active') and existing['signal_id']==selected['signal_id']
                           and existing.get('status')!='requires_review'
                           and (not record.get('price_basis_revision')
                                or existing.get('price_basis_revision')==record['price_basis_revision']))
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

    def _wake_notification_validation(self):
        box = self._notifications
        wake = getattr(box,'wake_validation',None)
        if callable(wake):
            wake()

    def selection_overlay(self, source, revision):
        with self._lock:
            state = self._load()
            seed = state.get('seed') or {}
            admitted = None
            if seed.get('run_id') != source:
                cursor = state.get('screening_monitor_cursor') or {}
                version = cursor.get('version') or []
                members = seed.get('incremental_screening_symbols', {})
                if len(version) < 2 or version[:2] != [source, revision] or not members:
                    return {}
                admitted = {f'{key}:{frequency}' for key, periods in cursor.get('periods', {}).items()
                            if key in members for frequency in periods}
            overlay = deepcopy({key: value for key, value in state["selection_checks"].items()
                                if value.get("revision") == revision and (admitted is None or key in admitted)})
            # A reader upgrade can suspend a point before a new period check
            # exists. Publish only its identity, never the private old proof.
            for identity, item in state["unverified_signals"].items():
                symbol = _symbol_key(item)
                key = f"{symbol}:{item['frequency']}"
                if admitted is not None and key not in admitted:
                    continue
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
            return {'revision':previous['revision'],'cutoff':previous['cutoff'],
                    **({'price_basis_revision':previous['price_basis_revision']}
                       if previous.get('price_basis_revision') else {})}
        return None

    @staticmethod
    def _reusable_pool_checks(state, result):
        """Carry successful checks across equivalent pool replacements only."""
        previous = state.get('seed') or {}
        revision = result.get('current_source_revision') or (result.get('source_revision')
                    if result.get('source_current') is True else None)
        ignored = {'scope','codes','symbols','workers','exclude_st','watch_groups'}
        from chanlun.screening.timeframe_policy import POLICY, SCAN_PERIODS, LIVE_PERIODS, enabled as roles_enabled
        scan_profiles = (set(SCAN_PERIODS), set(SCAN_PERIODS) | {'1m'}, {'5m', '30m'}, set(LIVE_PERIODS))
        fixed_roles = all(roles_enabled(settings)
                          and set(settings.get('frequencies', [])) in scan_profiles
                          for settings in (previous.get('settings', {}), result.get('settings', {})))
        if fixed_roles:
            # Prewarmed and candidate-only background plans share the same
            # fixed rules. Every reused physical period is still checked below.
            ignored.add('frequencies')
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
            incoming_identities.setdefault(key,set()).add(_signal_identity(row))
        excluded=set(state.get('force_pending_symbols',[])) | set(state.get('retry_symbols',{}))
        errors={( _symbol_key(error), error.get('frequency')) for error in result.get('errors',[]) if error.get('code')}
        retained={}
        for key, check in state['selection_checks'].items():
            symbol=check.get('symbol'); frequency=key.rsplit(':',1)[-1]
            fingerprint=check.get('input_fingerprint')
            if (symbol not in excluded and (symbol,None) not in errors and (symbol,frequency) not in errors
                    and check.get('revision')==revision and check.get('error') is False
                    and not check.get('pending_identities')
                    and type(check.get('cutoff')) is int and isinstance(check.get('identities'),list)
                    and isinstance(fingerprint,str) and len(fingerprint)==64
                    and all(char in '0123456789abcdef' for char in fingerprint)
                    and (key not in incoming or incoming_fingerprints.get(key)=={fingerprint})
                    and (key not in incoming or set(check['identities'])==incoming_identities.get(key,set()))
                    and check['cutoff']>=incoming.get(key,check['cutoff'])):
                retained[key]=deepcopy(check)
        checked={}
        for symbol, cutoff in state.get('checked_cutoffs',{}).items():
            periods=set(LIVE_PERIODS if fixed_roles else result.get('settings',{}).get('frequencies',[]))
            periods.update(plan['frequency'] for plan in state.get('holding_risk_plans',{}).values()
                           if plan.get('active') and _symbol_key(plan)==symbol)
            if (type(cutoff) is int and state.get('attempted_cutoffs',{}).get(symbol)==cutoff
                    and periods and all(f'{symbol}:{frequency}' in retained for frequency in periods)):
                checked[symbol]=cutoff
        return retained, dict(checked), checked

    @classmethod
    def _historical_reclassification(cls, check, revision, available_at, price_basis_revision=None):
        previous=cls._notification_baseline(check)
        changed=bool(previous and ((revision and previous['revision']!=revision)
                     or (previous.get('price_basis_revision') and price_basis_revision
                         and previous['price_basis_revision']!=price_basis_revision)))
        return bool(changed
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
            raise ValueError("请先在通知设置中配置钉钉机器人地址")
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

    def configure_point_notifications(self, body, *, page_options=None):
        validate_page_options(page_options)
        policy = normalize_point_notifications(body)
        with self._control_lock:
            with self._lock:
                state = self._load()
                previous = state['point_notifications']
                if policy != previous or state.get('point_notification_error'):
                    since = next_point_notification_since(previous, policy,
                        state['point_notification_since'], self.now().timestamp())
                    # The sender's generation fence precedes the new state,
                    # so a receipt authorized by the old policy cannot commit.
                    self.notifications.invalidate_point_notification_policy()
                    previous_fields = {key: deepcopy(state[key]) for key in (
                        'point_notifications', 'point_notification_since', 'point_notification_version')}
                    previous_error = state.get('point_notification_error')
                    state.update(point_notifications=policy, point_notification_since=since,
                                 point_notification_version=state['point_notification_version'] + 1)
                    state.pop('point_notification_error', None)
                    try:
                        self._save()
                    except (OSError, ValueError):
                        state.update(previous_fields)
                        if previous_error:
                            state['point_notification_error'] = previous_error
                        raise RuntimeError('通知设置未能保存，请稍后重试') from None
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
        # Lightweight polling omits the full-directory token. Actual result
        # consumption still needs it so repaired evidence resets a failed
        # read's bounded retry budget, even when the run/cutoff did not change.
        if ('evidence_version' not in status
                and callable(getattr(type(self.screening), 'scheduler_status', None))):
            refreshed = self.screening.status()
            if refreshed.get('run_id') != status.get('run_id'):
                return {'status': 'unavailable'}
            status = refreshed
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
        if not isinstance(body, dict) or set(body) - {"morning_after_close", "after_close", "interval_seconds", "daily_scope", "screening_workers", "monitor_workers", "frequencies"}:
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
        base["workers"] = body.get("screening_workers", base.get("workers", MAX_WORKERS))
        monitor_workers=body.get('monitor_workers',self._load()['settings'].get('monitor_workers',MAX_WORKERS))
        if type(monitor_workers) is not int or not 1<=monitor_workers<=MAX_WORKERS:
            raise ValueError('监听计算进程必须是 1–16 的整数')
        if scope != "latest":
            base.update(scope=scope, codes=[])
            base.pop("symbols", None)
        settings = {"morning_after_close": morning, "after_close": after, "interval_seconds": interval,
                    "daily_scope": scope, "screening": validate_settings(base), 'monitor_workers':monitor_workers}
        # Validate before taking ownership or changing any live state.
        if 'frequencies' in body:
            trial = {'settings': deepcopy(settings)}
            configure_periods(trial, body['frequencies'], self.now().timestamp())
            settings = trial['settings']
        with self._control_lock:
            with self._lock:
                state = self._load()
                if (state["settings"] != settings or not state["enabled"]
                        or 'frequencies' in body and state.get('monitor_frequencies') != body['frequencies']):
                    self._control_version += 1
                    state["daily"] = {}
                    self._startup_retries.clear()
                    state.pop('startup_errors',None)
                if 'frequencies' in body:
                    configure_periods(state, body['frequencies'], self.now().timestamp())
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
            self._monitor_capacity(0, False)
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
            manual_generation = monitor_requests.next_manual_generation(state)
            for key in state['seed']['codes']:
                if '1m' in _monitor_periods(state, key):
                    monitor_requests.add(state, key, 'manual', manual_generation)
            state['contract_blocks'] = {}
            state['contract_check_generation'] = state.get('contract_check_generation', 0) + 1
            state["force"] = True
            self._save()
        self._wake.set()
        return self._control_snapshot(page_options)

    def start_runtime(self):
        self._input_retention.stop.clear()
        self._incremental_reader.resume()
        self._monitor_result_reader.resume()
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
        self._incremental_reader.close()
        self._monitor_result_reader.close()
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=2)
        if self._notifications is not None:
            self._notifications.stop()
        self._monitor_capacity(0, False)
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
            self._wake.wait(self.poll_interval())

    def poll_interval(self):
        return .5 if self._busy_poll else 3

    def _monitor_capacity(self, demand, screening_active, *, overdue_pressure=False, service_estimate=None, holding_demand=0):
        """Publish bounded live demand; isolated explicit roots use pure limits."""
        demand = 0 if self._stop.is_set() else max(0, int(demand))
        if demand:
            with self._lock:
                configured = self._load()['settings'].get('monitor_workers', MAX_WORKERS)
            demand = min(demand, configured, cpu_capacity())
            estimate=service_estimate or {}
            needed=estimate.get('required_workers')
            if (screening_active and estimate.get('boost_ready') is True
                    and type(needed) is int and needed>0):
                # Queue length is not simultaneous compute demand. Use the
                # measured deadline/recurring workload plus one spare worker;
                # cold/unknown estimates retain the conservative full demand.
                demand=min(demand,needed+1)
        holding_demand = min(demand, max(0, int(holding_demand)))
        limits = role_limits(cpu_capacity(), demand, screening_active, overdue_pressure=overdue_pressure, holding_demand=holding_demand)
        self._busy_poll = demand > 0
        if self._publish_compute_budget:
            if self._compute_budget is None:
                self._compute_budget = budget_request()
            try:
                pressure = publish_monitor_demand(self._compute_budget, demand=demand,
                    expires_in=12, owner=self._compute_budget_owner, overdue_pressure=overdue_pressure, holding_demand=holding_demand)
                actual_limit = pressure.get('limit', cpu_capacity())
                needed = min(demand, (service_estimate or {}).get('required_workers', demand))
                normal = role_limits(actual_limit, needed, True, holding_demand=min(holding_demand, needed))['monitor_capacity']
                if (demand and actual_limit > 1 and screening_active and not overdue_pressure
                        and (service_estimate or {}).get('boost_ready') and needed > normal):
                    # Memory can shrink the shared budget after planning. The
                    # same measured demand must be compared to that real split.
                    overdue_pressure, demand = True, needed
                    pressure = publish_monitor_demand(self._compute_budget, demand=demand,
                        expires_in=12, owner=self._compute_budget_owner, overdue_pressure=True, holding_demand=min(holding_demand, demand))
                    limits = role_limits(cpu_capacity(), demand, screening_active, overdue_pressure=True, holding_demand=min(holding_demand, demand))
                limits['monitor_capacity'] = min(limits['monitor_capacity'], pressure['monitor_capacity'])
                for key in ('screening_capacity', 'monitor_reserved', 'limit'):
                    if key in pressure:
                        limits[key] = pressure[key]
            except (OSError, ValueError, RuntimeError):
                # The worker's shared slot locks remain authoritative even if
                # this advisory demand update could not be published.
                LOG.warning('monitor compute demand update unavailable')
        self._last_compute_limits = {**limits, 'monitor_demand': demand, 'holding_demand': holding_demand, 'overdue_pressure': overdue_pressure,
                                     'observed_at': self.now().timestamp()}
        return limits['monitor_capacity']

    def scheduling_activity(self):
        """Queue/allocation counts only; no candle-age latency approximation."""
        with self._lock:
            state = self._load()
            enabled_pool = state['enabled'] and bool((state.get('seed') or {}).get('codes'))
            active = (set(state.get('active_symbols', [])) - set(state.get('active_consumed_symbols', []))
                      if enabled_pool and state.get('active_monitor_run') != state.get('processed_run') else set())
            lanes = state.get('active_service_lanes', {})
            running = Counter(lanes.get(key, 'critical') for key in active)
            return {'waiting': {lane: state.get('monitor_queue_counts', {}).get(lane, 0) if enabled_pool else 0
                                for lane in ('hot', 'critical', 'cold')},
                    'in_flight': {lane: running[lane] for lane in ('hot', 'critical', 'cold')},
                    'planned_at': state.get('monitor_queue_at') if enabled_pool else None,
                    'allocation': deepcopy(self._last_compute_limits),
                    'weights': {'hot': 8, 'critical': 3, 'cold': 1},
                    'pressure': deepcopy(state.get('monitor_service_pressure', {})) if enabled_pool else {},
                    'period_costs': deepcopy(state.get('monitor_period_costs', {})),
                    'poll_seconds': self.poll_interval()}

    def _notification_worker_health(self):
        from .worker_progress import WorkerProgress
        delivery=self._notifications
        charts=getattr(delivery,'charts',None)
        progress = {'delivery':getattr(delivery,'progress',None),
                    'charts':getattr(charts,'progress',None),
                    'chart_history':getattr(charts,'history_progress',None)}
        return {name:worker.snapshot() for name,worker in progress.items()
                if isinstance(worker,WorkerProgress)}

    def runtime_health(self):
        """Nonblocking liveness probe, independent of disk and the state lock."""
        alive = lambda thread: bool(thread and thread.is_alive())
        threads = {'scheduler':alive(self._thread)}
        workers = self._notification_worker_health()
        if self._notifications is not None:
            threads['delivery']=alive(self._notifications._thread)
            if self._notifications.charts is not None:
                threads['charts']=alive(self._notifications.charts._thread)
                if 'chart_history' in workers:
                    threads['chart_history']=alive(self._notifications.charts._history_thread)
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
            manual = self._scheduler_status(self.screening)
            self._record_daily_run(manual)
            if not self._control_is_current(control_version):
                return
            if not state["enabled"]:
                self._publish_notifications(manual)
                return
            now = self.now().astimezone(CN)
            self._consume_screening_increment(manual, control_version, now)
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
                                           "cutoffs": result.get("cutoffs", {}),
                                           "screening_frequencies": list(result["settings"].get('frequencies', [])),
                                           "settings": monitor_settings(current, result["settings"]),
                                           "error_count": len(result.get("errors", []))}
                        # Prune after merging the watchlist below, so replacing
                        # the daily pool does not erase unexamined US signals.
                        current["last_attempt_key"] = None
                        current['next_check_at'] = 0
                        if not same_run:
                            current["attempted_cutoffs"] = attempted
                            current['attempted_monitor_periods'] = {}
                            current['deferred_monitor_periods'] = {}
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

    def _consume_screening_increment(self, manual, control_version, now):
        """Import only membership, never signals, from audited committed records."""
        from .monitor_incremental import audited_members
        identity = manual.get('run_id')
        version = (identity, manual.get('source_revision'), manual.get('result_reader_revision'),
                   manual.get('selection_policy_version'))
        with self._lock:
            state = self._load()
            if not state['enabled'] or not self._control_is_current(control_version):
                return
            cursor = state.get('screening_monitor_cursor') or {}
            seed = state.get('seed') or {}
            budget_stopped = manual.get('status') == 'cancelled' and manual.get('stop_reason') == 'time_budget'
            eligible = (identity and (manual.get('status') in ACTIVE or budget_stopped)
                        and manual.get('source_current') is True
                        and identity != seed.get('run_id'))
            if not eligible:
                self._incremental_reader.invalidate()
                terminal_failure = manual.get('status') in {'cancelled', 'failed', 'interrupted'}
                obsolete = cursor.get('version', [None])[0] != identity or manual.get('source_current') is False
                if terminal_failure or obsolete:
                    changed = bool(seed.pop('incremental_screening_symbols', None))
                    changed |= self._discard_screening_minute_overrides(state, (cursor.get('version') or [None])[0])
                    if changed:
                        self._save()
                return
            after = cursor.get('completed', 0) if tuple(cursor.get('version', [])) == version else 0
            if tuple(cursor.get('version', [])) != version:
                changed = bool(seed.pop('incremental_screening_symbols', None))
                changed |= self._discard_screening_minute_overrides(state, (cursor.get('version') or [None])[0])
                if changed:
                    self._save()
            if manual.get('completed', 0) <= after:
                return
        try:
            read_key = (version, after, control_version, seed.get('run_id'), manual.get('completed', 0))
            result = self._incremental_reader.read(read_key,
                lambda: self.screening.committed_results(after, limit=16))
        except MonitorResultsReadError:
            LOG.warning('incremental screening proof unavailable for %s', identity)
            self._incremental_reader.invalidate()
            return
        if result is None:
            return
        if (result.get('run_id') != identity or not result.get('source_current')
                or (result.get('status') not in ACTIVE | {'completed'} and not (
                    result.get('status') == 'cancelled' and result.get('stop_reason') == 'time_budget'))
                or result.get('frozen_native_review') or result.get('results_loading')):
            return
        if result.get('results_through', after) <= after:
            return
        if any(manual.get(field) is not None and result.get(field) != manual[field]
               for field in ('source_revision', 'result_reader_revision', 'selection_policy_version')):
            return
        members, admitted_periods = audited_members(result)
        with self._lock:
            state = self._load()
            if not self._control_is_current(control_version) or not state['enabled']:
                return
            current_cursor = state.get('screening_monitor_cursor') or {}
            current_after = (current_cursor.get('completed', 0)
                             if tuple(current_cursor.get('version', [])) == version else 0)
            if current_after != after or (state.get('seed') or {}).get('run_id') != read_key[-2]:
                return
            seed = state.get('seed')
            if seed is None:
                seed = {'run_id': 'watchlist', 'screening_symbols': [], 'symbols': [], 'codes': [],
                        'cutoffs': {}, 'settings': monitor_settings(state, result['settings']), 'error_count': 0}
                state['seed'] = seed
            previous = state.get('screening_monitor_cursor') or {}
            if tuple(previous.get('version', [])) != version:
                seed['incremental_screening_symbols'] = {}
            periods = deepcopy(previous.get('periods', {})) if tuple(previous.get('version', [])) == version else {}
            for key, frequencies in admitted_periods.items():
                periods[key] = sorted(set(periods.get(key, [])) | set(frequencies))
                if '1m' in frequencies:
                    monitor_requests.add(state, key, f'screening:{identity}', {
                        'source': 'screening', 'run_id': identity,
                        'revision': result.get('source_revision'), 'through': result.get('results_through')})
            incremental = seed.setdefault('incremental_screening_symbols', {})
            for key, member in members.items():
                if key not in seed['codes'] and key not in incremental:
                    # Admission is a notification boundary. Historical points
                    # remain visible after live verification without a backfill.
                    state.setdefault('notification_symbol_since', {})[key] = int(now.timestamp())
                incremental[key] = member
            state['screening_monitor_cursor'] = {'version': list(version),
                                                 'completed': result.get('results_through', after),
                                                 'periods': periods}
            if members:
                state['next_check_at'] = 0
            self._save()
            if result.get('results_through', after) < result.get('completed', 0):
                self._wake.set()

    @staticmethod
    def _discard_screening_minute_overrides(state, run_id):
        return monitor_requests.discard_screening(state, run_id)

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
            quote_only = quote_only_symbols()
            quote_members = sorted({_symbol_key(symbol) for symbol in [*watch, *holdings]
                                    if _symbol_key(symbol) in quote_only})
            watch = [symbol for symbol in watch if _symbol_key(symbol) not in quote_only]
            holdings = [symbol for symbol in holdings if _symbol_key(symbol) not in quote_only]
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
            quote_changed = state.get('quote_only_symbols', []) != quote_members
            state['quote_only_symbols'] = quote_members
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
                    if quote_changed:
                        self._save()
                    return
                seed = {"run_id": "watchlist", "screening_symbols": [], "symbols": [], "codes": [],
                        "cutoffs": {}, "settings": monitor_settings(state, state["settings"]["screening"]), "error_count": 0}
                state["seed"] = seed
            base = seed.get("screening_symbols", seed["symbols"])
            pool = {_symbol_key(s): s for s in base}
            pool.update(seed.get('incremental_screening_symbols', {}))
            pool.update({_symbol_key(s): s for s in watch})
            pool.update({_symbol_key(s): s for s in holdings})
            pool = {key:value for key,value in pool.items() if key not in quote_only}
            symbols = [pool[key] for key in sorted(pool)]
            frequencies = set(seed.get("settings", {}).get("frequencies", []))
            point_types = set(seed.get("settings", {}).get("point_types", []))
            scope_pruned = quote_changed
            scope_state = {**state, 'holding_symbols': held_keys}
            for name in ("signals", "unverified_signals"):
                kept = {key: item for key, item in state[name].items()
                        if _symbol_key(item) in pool and item.get("frequency") in _monitor_periods(scope_state, _symbol_key(item))
                        and (_symbol_key(item) in held_keys if item.get("signal_type") == "consolidation_divergence"
                             else item.get("point_type") in (set(POINT_TYPES) if _symbol_key(item) in held_keys else point_types))}
                scope_pruned |= len(kept) != len(state[name])
                for item in kept.values():
                    holding = _symbol_key(item) in held_keys
                    scope_pruned |= item.get('holding') != holding
                    item['holding'] = holding
                state[name] = kept
            previous_errors = state.get('errors', [])
            kept_errors = [item for item in previous_errors if not item.get('frequency')
                           or item['frequency'] in _monitor_periods(scope_state, _symbol_key(item))]
            scope_pruned |= len(kept_errors) != len(previous_errors)
            if 'errors' in state:
                state['errors'] = kept_errors
            for name in ('minute_force_pending', 'active_minute_override_tokens'):
                previous = state.get(name, {})
                kept = {key: value for key, value in previous.items()
                        if key in pool and '1m' in _monitor_periods(scope_state, key)}
                scope_pruned |= len(kept) != len(previous)
                if name in state:
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
            state['deferred_monitor_periods'] = {key: periods for key, periods in
                state.get('deferred_monitor_periods', {}).items() if key in pool}
            state['attempted_monitor_periods'] = {key: value for key, value in
                state.get('attempted_monitor_periods', {}).items() if key.rsplit(':', 1)[0] in pool}
            for name in ('minute_cadence_checkpoints', 'minute_force_pending'):
                state[name] = {key: value for key, value in state.get(name, {}).items() if key in pool}
            state['monitor_pending_deadlines'] = {key: value for key, value in state.get('monitor_pending_deadlines', {}).items()
                                                   if key.rsplit(':', 1)[0] in pool}
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
                updating = manual.get('status') == 'cancelled' and manual.get('stop_reason') == 'policy_update'
                resumed = bool(manual.get('resume_count')) and record.get('status') in {'partial', 'timed_out'}
                if record.get("run_id") != manual.get("run_id") or (record.get("status") != "started"
                        and not resumed and not (updating and record.get('status') == 'failed')):
                    continue
                healthy = manual["status"] == "completed" and not all_periods_failed(manual)
                budget_stopped = manual.get('stop_reason') == 'time_budget'
                record["status"] = ('partial' if manual.get('completed', 0) else 'timed_out') if budget_stopped else (
                    'updating' if updating else 'completed' if healthy else 'failed')
                record["finished_at"] = manual.get("finished_at")
                if resumed:
                    record.pop('deadline_at', None)
                    record.update(completed=manual.get('completed', 0), total=manual.get('total', 0))
                if manual.get('stop_reason') == 'time_budget':
                    record.update(completed=manual.get('completed', 0), total=manual.get('total', 0))
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
            phase_settings = deepcopy(settings['screening'])
            if phase == 'morning' and phase_settings.get('strategy') == 'direct_structural':
                phase_settings['frequencies'] = ['5m', '30m']
                phase_settings.pop('minimum_operating_frequency', None)
            from chanlun.screening.timeframe_policy import screening_settings
            phase_settings = screening_settings(phase_settings)
            compatible_completed = (
                manual.get('status') == 'completed' and not all_periods_failed(manual)
                and manual.get('cutoffs', {}).get('5m') == cutoff
                and {**manual.get('settings', {}), **({'timeframe_policy': phase_settings['timeframe_policy']}
                    if phase_settings.get('timeframe_policy') else {})} == phase_settings)
            if compatible_completed and manual.get('source_current'):
                # A manually refreshed run may have replaced an already
                # completed scheduled run. Remember its actual identity.
                updated = {**phase_state, 'run_id':manual['run_id'], 'status':'completed'}
                cleared = self._clear_start_failure('daily',state)
                if updated != phase_state or cleared:
                    daily[phase] = updated
                    self._save()
                self._startup_retries.pop('daily',None)
                return
            revision = manual.get('current_source_revision')
            if (phase_state.get('status') == 'completed' and compatible_completed
                    and manual.get('source_current') is False and manual.get('frozen_native_review')
                    and revision and manual.get('source_revision') and manual['source_revision'] != revision
                    and phase_state.get('source_recovery_revision') != revision):
                # Frozen completed results intentionally omit old-rule previews.
                # Rebuild once under the new rules instead of leaving that view
                # empty indefinitely. Scheduled scans run until completion.
                phase_state = {**phase_state, 'run_id':manual['run_id'], 'status':'updating',
                    'attempts':0, 'retry_at':0, 'source_recovery_revision':revision,
                    'refresh_reason':'rules_updated'}
                daily[phase] = phase_state
                self._save()
            if phase_state.get("status") in {"completed", "partial", "timed_out"}:
                if self._clear_start_failure('daily',state):
                    self._save()
                self._startup_retries.pop('daily',None)
                return
            if manual["status"] in ACTIVE:
                return
            recovery = getattr(self.screening, '_automatic_recovery', None)
            if recovery is not None and recovery.pending(manual):
                # Let the same-run recovery owner authenticate and resume the
                # committed prefix before a daily retry replaces latest.json.
                return
            revision=manual.get('current_source_revision')
            if (manual.get('status') in {'failed','interrupted','cancelled'}
                    and phase_state.get('run_id')==manual.get('run_id')
                    and revision and manual.get('source_revision') and manual['source_revision']!=revision
                    and (str(manual.get('error','')).startswith('RuntimeError: Screening source changed')
                         or manual.get('status') == 'cancelled' and manual.get('stop_reason') == 'policy_update')
                    and phase_state.get('source_recovery_revision')!=revision):
                # A deployment interruption is not a failed data attempt. Give
                # the new code one normal retry budget, once per revision.
                phase_state.update(attempts=0,retry_at=0,source_recovery_revision=revision)
                daily[phase]=phase_state
                self._save()
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
                settings=phase_settings
                context={'control':control_version,'session':session,'phase':phase}
                if not self._start_allowed('daily',context):
                    return
            try:
                options = {}
                if phase_state.get('refresh_reason') in {'rules_updated', 'timeframe_policy_updated'}:
                    options['external_notifications'] = False
                launched = self.screening.start(settings, **options)
            except Exception as error:
                self._record_start_failure('daily',context,error)
                raise TaskStartError('daily',error) from error
            with self._lock:
                state=self._load()
                state['daily'][phase] = {**({'source_recovery_revision':phase_state['source_recovery_revision']}
                                           if phase_state.get('source_recovery_revision') else {}),
                                        **({'refresh_reason':phase_state['refresh_reason']}
                                           if phase_state.get('refresh_reason') else {}),
                                        "run_id": launched["run_id"], "status": "started",
                                                "attempts": attempts + 1, "retry_at": now.timestamp() + (60 if phase == 'morning' else 900)}
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
                unfinished = set(state.get('active_symbols', [])) - set(state.get('active_consumed_symbols', []))
                if unfinished & set(state.get('holding_symbols', [])):
                    # A holding already admitted to this run gets a complete
                    # attempt before another market's ordinary work can yield it.
                    return
                # A durable intent survives a restart before the cancel flag
                # is written. Never overlap two private runs.
                requested=state.get('yielding_monitor_run')==identity
                if not requested:
                    if state.get('background_empty_yields',0)>=1 and not status.get('completed',0):
                        # Allow one committed result after an empty preemption.
                        return
                    scope_index = build_minute_scope_index(state)
                    for symbol in seed.get('symbols',[]):
                        revision = status.get('current_source_revision') or status.get('source_revision')
                        available = self._schedulable_periods(state, symbol, revision, now, scope_index)
                        if not available:
                            continue
                        frequency = next(f for f in ('1m', '5m', '30m') if f in available)
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

    def _yield_live_for_priority(self, now, status, control_version, expected_seed, expected_run):
        """Let key periods interrupt minute work and rotate a slow batch tail."""
        with self._control_lock:
            with self._lock:
                state = self._load()
                seed = state.get('seed') or {}
                if (not state['enabled'] or not self._control_is_current(control_version)
                        or seed.get('run_id') != expected_seed
                        or state.get('active_monitor_run') != expected_run
                        or status.get('run_id') != expected_run
                        or state.get('active_monitor_mode') != 'live'
                        or status.get('cancel_requested')):
                    return
                active_periods = state.get('active_periods', {})
                if not active_periods:
                    return
                held = set(state.get('holding_symbols', []))
                unfinished = set(state.get('active_symbols', [])) - set(state.get('active_consumed_symbols', []))
                if unfinished & held:
                    return
                active_lanes = state.get('active_service_lanes', {})
                cold_batch = bool(active_lanes) and all(lane == 'cold' for lane in active_lanes.values())
                elapsed = now.timestamp() - state.get('active_scheduled_at', now.timestamp())
                urgent = {key for key in self._pending_notification_symbols(state, now.timestamp())
                          if now.timestamp() >= self._notification_retry_after.get(key, 0)}
                short_wait = bool(held - unfinished) or cold_batch or bool(urgent)
                if elapsed < (MONITOR_HOLDING_WAIT_SECONDS if short_wait else MONITOR_LIVE_SLICE_SECONDS):
                    return
                revision = status.get('current_source_revision') or status.get('source_revision')
                waiting = False
                priority_waiting = []
                scope_index = build_minute_scope_index(state)
                decisions = {}
                for symbol in seed.get('symbols', []):
                    key = _symbol_key(symbol)
                    for frequency in self._schedulable_periods(state, symbol, revision, now, scope_index, decisions):
                        try:
                            target = closed_bar_cutoff(symbol['market'], symbol['code'], now, frequency)
                        except (ValueError, OSError):
                            continue
                        if not _recent_clock(now, target, frequency):
                            continue
                        period_key = f'{key}:{frequency}'
                        check = state['selection_checks'].get(period_key, {})
                        in_flight = (frequency in active_periods.get(key, [])
                                     and state.get('active_period_targets', {}).get(period_key) == target)
                        retry = _period_retries(state.get('retry_symbols', {}).get(key, {}))
                        retry = retry.get(frequency, retry.get(None, {}))
                        if (not state['force'] and retry.get('period_cutoff', retry.get('cutoff')) == target
                                and (retry.get('failures', 0) >= MONITOR_MAX_FAILURES
                                     or (retry.get('retry_at') or 0) > now.timestamp())):
                            continue
                        needed = (check.get('revision') != revision or check.get('cutoff') != target
                                  or key in urgent or frequency == '1m' and key in state.get('minute_force_pending', {}))
                        lane = task_lane(frequency, decisions.get(key, {}))
                        slice_due = (elapsed >= MONITOR_LIVE_SLICE_SECONDS or cold_batch and lane == 'hot'
                                     or (key in held or key in urgent) and elapsed >= MONITOR_HOLDING_WAIT_SECONDS)
                        if not in_flight and needed and slice_due:
                            waiting = True
                            if key in held or key in urgent and (key,frequency) in self._notification_rechecks:
                                priority_waiting.append((0 if key in held and frequency=='1m' else 1 if key in held else 2,
                                                         period_key,[revision,target]))
                if not waiting:
                    return
                unfinished = set(state.get('active_symbols', [])) - set(state.get('active_consumed_symbols', []))
                counts = state.setdefault('live_slice_yields', {})
                fingerprints = {key: json.dumps([(period, state.get('active_period_targets', {}).get(f'{key}:{period}'))
                                                 for period in sorted(active_periods.get(key, []))])
                                for key in unfinished}
                # A cold period gets one rotation, then an uninterrupted
                # attempt. Its own cutoff, not the faster symbol clock, guards
                # against frequent minute ticks killing the same work forever.
                priority_yields = state.setdefault('priority_slice_yields', {})
                fresh_priorities = {scope: token for _, scope, token in priority_waiting
                                    if priority_yields.get(scope) != token}
                protected = {key: token for key, token in fingerprints.items() if counts.get(key) == token}
                rescues = state.setdefault('live_priority_rescues', {})
                if protected:
                    if not fresh_priorities or any(rescues.get(key) == [revision, token]
                                                   for key, token in protected.items()):
                        return
                    # Permit one urgent rescue, then let the interrupted target
                    # finish even if faster holding clocks keep advancing.
                    rescues.update({key: [revision, token] for key, token in fingerprints.items()})
                # A protected slow attempt cannot suppress a new holding clock
                # or an explicit notification proof indefinitely. Each such
                # target gets one priority handoff, subject to the slow target's
                # bounded rescue above; new minute clocks cannot starve it.
                # One handoff admits all currently waiting priority periods.
                # Recording only one would allow the remaining periods to
                # cancel the same slow attempt repeatedly at the same clock.
                priority_yields.update(fresh_priorities)
                counts.update(fingerprints)
                state.setdefault('live_yielded_periods', {}).update(
                    {key: list(active_periods.get(key, [])) for key in unfinished})
                counts = {key: token for key, token in counts.items() if key in seed.get('codes', [])}
                state.update(live_slice_yields=counts, yielding_monitor_run=expected_run)
                self._save()
                monitor = self.monitor
                priority_control = deepcopy(getattr(self, '_source_prefetch_control', None))
                soft_handoff = bool(priority_waiting) and callable(getattr(monitor, 'handoff_for_priority', None))
            if soft_handoff:
                monitor.handoff_for_priority(expected_run, priority_control)
            else:
                monitor.cancel()
            self._wake.set()

    def _schedulable_periods(self, state, symbol, revision, now, scope_index, decisions=None):
        """Select independent jobs; linked evidence remains inside the 5m task."""
        key = _symbol_key(symbol)
        available = _available_monitor_periods(state, key, revision)
        if '1m' not in available:
            return available
        try:
            closed = closed_bar_cutoff(symbol['market'], symbol['code'], now, '1m')
        except (ValueError, OSError):
            # The ordinary calendar-error path owns diagnostics and retries.
            return available
        request = self._notification_rechecks.get((key, '1m'), {})
        decision = minute_cadence(state, key, revision, closed,
            manual_override=state['force'] or key in state.get('minute_force_pending', {}),
            notification_recheck=bool(request and request.get('expires', 0) > now.timestamp()),
            scope_index=scope_index)
        if decisions is not None:
            decisions[key] = decision
        return available if decision['eligible'] else available - {'1m'}

    def _refresh_suspension_status(self,state,now):
        from chanlun.screening.suspension_status import fund_halt
        paused={}
        for symbol in (state.get('seed') or {}).get('symbols',[]):
            if symbol.get('market','a')!='a' or not symbol['code'].startswith('SH.5'):
                continue
            key=_symbol_key(symbol)
            for period in independent_monitor_periods(state,key):
                try:target=closed_bar_cutoff('a',symbol['code'],now,period)
                except (ValueError,OSError):continue
                fact=fund_halt(symbol['code'],now,cutoff=target)
                if fact:paused.setdefault(key,{})[period]={**fact,'name':symbol.get('name',symbol['code'])}
        changed=state.get('suspended_periods',{})!=paused
        state['suspended_periods']=paused
        old_errors=state.get('errors',[])
        state['errors']=[row for row in old_errors if not (
            row.get('frequency') in paused.get(_symbol_key(row),{})
            and set(row.get('reasons',[])) and set(row['reasons'])<={'STALE_DATA','NO_VOLUME'})]
        for row in old_errors:
            if row not in state['errors']:
                _clear_period_retries(state,_symbol_key(row),{row['frequency']})
        changed |= old_errors!=state['errors']
        if not state['errors'] and state.get('last_error')=='部分标的行情或计算异常，请查看问题列表':
            state['last_error']='';changed=True
        if changed:self._save()

    def _initialize_minute_cadence(self, state, revision):
        """Adopt existing verified checks once; later seeds still get a first check."""
        if state.get('minute_cadence_policy_version') == 1 or not revision:
            return
        for key in (state.get('seed') or {}).get('codes', []):
            check = state['selection_checks'].get(f'{key}:1m', {})
            record_minute_checkpoint(state, key, revision, check.get('cutoff'))
        state['minute_cadence_policy_version'] = 1
        self._save()

    @staticmethod
    def _acknowledge_minute_override(state, symbols, periods_for):
        active = state.get('active_minute_override_tokens', {})
        for key in symbols:
            if '1m' in periods_for(key) and key in active:
                monitor_requests.acknowledge(state, key, active[key])

    def _schedule_monitor(self, now, cutoff, control_version=None):
        self._busy_poll = False
        try:
            return self._plan_monitor(now, cutoff, control_version)
        finally:
            with self._lock:
                state = self._load()
                active = state['enabled'] and bool((state.get('seed') or {}).get('codes')) and bool(state.get('active_monitor_run')) and (
                    state.get('active_monitor_run') != state.get('processed_run'))
            if active:
                self._busy_poll = True
            elif not self._busy_poll:
                self._monitor_capacity(0, False)
                with self._lock:
                    state = self._load()
                    if any(state.get('monitor_queue_counts', {}).values()):
                        state['monitor_queue_counts'] = {'hot': 0, 'critical': 0, 'cold': 0}
                        self._save()

    def _plan_monitor(self, now, cutoff, control_version=None):
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
        manual = self._scheduler_status(self.screening)
        monitor_status = self._scheduler_status(self.monitor)
        if monitor_status['status'] in ACTIVE:
            with self._control_lock:
                with self._lock:
                    state = self._load()
                    active = set(state.get('active_symbols', [])) - set(state.get('active_consumed_symbols', []))
                    live_active = active & set(state.get('active_live_symbols', []))
                    demand = (len(live_active) + state.get('pending_live_tasks', 0)
                              + min(2, len(active - live_active) + state.get('pending_background_tasks', 0))) if state['enabled'] else 0
                    if monitor_status.get('phase')=='source_preparation':
                        # Native history preparation does not use CL workers.
                        # Keep two slots for a prompt live handoff instead of
                        # reserving a full calculation wave during provider I/O.
                        demand=min(2,demand)
                    pressure = state.get('monitor_service_pressure', {})
                    boosted = bool(demand and pressure.get('overdue_pressure'))
                    if boosted:
                        demand = min(demand, pressure.get('required_workers', demand))
                    holding_demand = len(active & set(state.get('holding_symbols', [])))
                self._monitor_capacity(max(1, demand) if demand else 0, manual['status'] in ACTIVE,
                                       overdue_pressure=boosted, service_estimate=pressure, holding_demand=holding_demand)
            self._yield_live_for_priority(now, monitor_status, control_version,
                                          expected_seed, expected_run)
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
            self._refresh_suspension_status(state,now)
            trigger_period = next(
                (period for period in ("1m", "5m", "30m")
                 if period in seed.get("settings", {}).get("frequencies", ())),
                "5m",
            )
            # Keep the completed pool and holdings live while audited members
            # arrive from the full scan. Share capacity through smaller batches
            # and fewer workers, retaining the prior seed as the baseline.
            screening_active = manual["status"] in ACTIVE
            if (monitor_status.get("run_id") == state.get("active_monitor_run")
                        and monitor_status.get("run_id") != state.get("processed_run")):
                return
            reference = monitor_status if monitor_status.get("run_id") else manual
            revision = reference.get('current_source_revision') or reference.get('source_revision')
            if reference.get('source_current') is True:
                self._initialize_minute_cadence(state, revision)
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
            urgent=self._pending_notification_symbols(state,now.timestamp())
            cutoffs, errors, pending, retries, recent = {}, [], [], set(), set()
            closing_delivery = set()
            due_retry_periods, due_notification_periods = {}, {}
            revision_only_periods = {}
            scope_index = build_minute_scope_index(state)
            schedulable_periods = {}
            minute_decisions = {}
            previous_calendar_errors = {_symbol_key(error) for error in state.get("schedule_errors", [])}
            for symbol in seed["symbols"]:
                key = _symbol_key(symbol)
                available = self._schedulable_periods(state, symbol, revision, now, scope_index, minute_decisions)
                if not available:
                    continue
                schedulable_periods[key] = available
                symbol_trigger = next(f for f in ('1m', '5m', '30m') if f in available)
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
                due_periods, blocked_periods = set(), set()
                for period, retry in _period_retries(state['retry_symbols'].get(key, {})).items():
                    if period is not None and period not in available:
                        continue
                    if period is not None and type(retry.get('period_cutoff')) is int:
                        try:
                            current = closed_bar_cutoff(symbol['market'], symbol['code'], now, period)
                        except (ValueError, OSError):
                            continue
                        same_clock = retry['period_cutoff'] == current
                    else:
                        same_clock = retry.get('cutoff') == closed
                    if not same_clock:
                        continue
                    affected = {period} if period is not None else available
                    if retry.get('failures', 0) >= MONITOR_MAX_FAILURES or (retry.get('retry_at') or 0) > now.timestamp():
                        blocked_periods.update(affected)
                    elif retry.get('retry_at') is not None:
                        due_periods.update(affected)
                due_retry_periods[key] = due_periods
                due = bool(due_periods)
                urgent_periods = {period for symbol_key, period in self._notification_rechecks if symbol_key == key}
                due_notification_periods[key] = (urgent_periods - blocked_periods
                    if now.timestamp() >= self._notification_retry_after.get(key, 0) else set())
                urgent_due = bool(due_notification_periods[key])
                # Inspect the cached session clock even during the idle wait.
                # New candles and already-due retries take priority over idle
                # polling. Another batch cannot extend a symbol's backoff.
                deferred = set(state.get('deferred_monitor_periods', {}).get(key, [])) & available
                minute_override = '1m' in available and key in state.get('minute_force_pending', {})
                minute_first_check = minute_decisions.get(key, {}).get('reason') == 'new_candidate'
                # Another market's latest run may already use the new source,
                # so a pool-wide source-current flag cannot own every period's
                # first recheck. Admit only periods never checked/attempted at
                # this revision; current attempts retain their normal backoff.
                revision_periods = {frequency for frequency in available if revision and
                    state['selection_checks'].get(f'{key}:{frequency}', {}).get('revision') != revision
                    and state.get('attempted_monitor_periods', {}).get(f'{key}:{frequency}', {}).get('revision') != revision}
                revision_due = bool(revision_periods)
                ordinary_wakeup = (deferred or minute_override or minute_first_check or due or urgent_due
                                   or is_recent and (attempted is None or closed > attempted))
                ordinary_fresh = (state['force'] or key in previous_calendar_errors
                                  or key in state.get('force_pending_symbols', []) or deferred
                                  or minute_override or minute_first_check or attempted != closed)
                if revision_due and not ((ordinary_fresh or due or urgent_due)
                                         and (not waiting_interval or ordinary_wakeup)):
                    revision_only_periods[key] = revision_periods
                if waiting_interval and not (deferred or minute_override or minute_first_check or revision_due or due or urgent_due or is_recent and (attempted is None or closed > attempted)):
                    continue
                fresh = (state["force"] or key in previous_calendar_errors or key in state.get("force_pending_symbols", [])
                         or bool(deferred) or minute_override or minute_first_check or revision_due or attempted != closed)
                if fresh or due or urgent_due:
                    pending.append(symbol)
                    cutoffs[key] = closed
                    if not is_recent and 0 <= now.timestamp()-closed < NOTIFICATION_LIFETIME:
                        closing_delivery.add(key)
                    if due or not fresh:
                        retries.add(key)
            calendar_revision = reference.get("current_source_revision") or reference.get("source_revision")
            recurring_recent = set(recent)
            recent.update(closing_delivery)
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
            planned_periods, planned_targets = {}, {}
            for symbol in pending:
                key = _symbol_key(symbol)
                available = schedulable_periods[key]
                needed = []
                for frequency in ('30m', '5m', '1m'):
                    if frequency not in available:
                        continue
                    if key in revision_only_periods and frequency not in revision_only_periods[key]:
                        continue
                    check = state['selection_checks'].get(f'{key}:{frequency}', {})
                    try:
                        expected = closed_bar_cutoff(symbol['market'], symbol['code'], now, frequency)
                    except (ValueError, OSError):
                        expected = None
                    planned_targets[f'{key}:{frequency}'] = expected
                    attempted_period = state.get('attempted_monitor_periods', {}).get(f'{key}:{frequency}', {})
                    deferred = state.get('deferred_monitor_periods', {}).get(key, [])
                    urgent_period = frequency in due_notification_periods.get(key, ())
                    explicitly_due = (state['force'] or key in state.get('force_pending_symbols', [])
                            or key in previous_calendar_errors or frequency in due_retry_periods.get(key, ()) or urgent_period
                            or frequency in deferred)
                    explicitly_due |= frequency == '1m' and key in state.get('minute_force_pending', {})
                    explicitly_due |= frequency == '1m' and minute_decisions.get(key, {}).get('reason') == 'new_candidate'
                    unchecked = (not revision or expected is None or check.get('error')
                                 or check.get('revision') != revision or check.get('cutoff') != expected)
                    already_attempted = (attempted_period.get('cutoff') == expected
                                         and attempted_period.get('revision') == revision)
                    if explicitly_due or unchecked and not already_attempted:
                        needed.append(frequency)
                yielded_periods = state.get('live_yielded_periods', {}).get(key, [])
                needed.sort(key=lambda frequency: (frequency == '1m', frequency in yielded_periods))
                if not needed and key not in state['attempted_cutoffs']:
                    # A new pool can retain proof while rejecting an in-flight
                    # scheduling baseline. Recheck one eligible period; cold
                    # minute work has already been excluded from this set.
                    needed = [next(f for f in ('1m', '5m', '30m') if f in available)]
                planned_periods[key] = needed
            # A quicker symbol clock or an old blanket deferral can make the
            # symbol look fresh while every eligible period is already checked.
            # Do not turn an empty plan back into an unsolicited minute task.
            pending = [symbol for symbol in pending if planned_periods[_symbol_key(symbol)]]
            from .monitor_scan_priority import defer_to_screening
            deferred_closed=[symbol for symbol in pending if defer_to_screening(
                state,manual,symbol,cutoffs.get(_symbol_key(symbol)),now.timestamp(),revision,scope_index,
                urgent=bool(due_notification_periods.get(_symbol_key(symbol))))]
            deferred_keys={_symbol_key(symbol) for symbol in deferred_closed}
            if state.get('closed_market_deferred_symbols',0)!=len(deferred_keys):
                state['closed_market_deferred_symbols']=len(deferred_keys)
                state['closed_market_deferred_run']=manual.get('run_id') if deferred_keys else None
                self._save()
            pending=[symbol for symbol in pending if _symbol_key(symbol) not in deferred_keys]
            planned_periods={key:periods for key,periods in planned_periods.items() if key not in deferred_keys}
            if not pending:
                if state['force'] or state.get('pending_symbols_count'):
                    state.update(force=False, pending_symbols_count=0)
                    self._save()
                return
            if remember_pending(state, planned_periods, planned_targets, minute_decisions, revision, now.timestamp()):
                self._save()
            # Eligible holdings precede both live and historical ordinary work;
            # the remaining budget retains bounded retries and fair rotation.
            backlog = len(pending)
            pending_keys={_symbol_key(symbol) for symbol in pending}
            forced = ({_symbol_key(s) for s in pending} if state["force"]
                      else set(state.get("force_pending_symbols", [])))
            forced |= {_symbol_key(symbol) for symbol in pending} & previous_calendar_errors
            live_pending=[symbol for symbol in pending if _symbol_key(symbol) in recent]
            live_work = sum(len(planned_periods[_symbol_key(symbol)]) for symbol in live_pending)
            historical_work = sum(len(value) for value in planned_periods.values()) - live_work
            demand = live_work + min(2, historical_work)
            available_capacity = min(cpu_capacity(), self._last_compute_limits.get('limit', cpu_capacity()))
            pressure = service_pressure(state, planned_periods, planned_targets, minute_decisions,
                                        recent, now.timestamp(), available_capacity, recurring_keys=recurring_recent)
            if pressure['overdue_pressure']:
                demand = min(demand, pressure['required_workers'])
            holding_demand = min(demand, sum(key in state.get('holding_symbols', []) for key in planned_periods))
            workers = min(role_limits(cpu_capacity(), demand, screening_active,
                                      overdue_pressure=pressure['overdue_pressure'], holding_demand=holding_demand)['monitor_capacity'],
                          state['settings'].get('monitor_workers', MAX_WORKERS))
            mode='live' if live_pending else 'background'
            fair_plan = deepcopy({'symbols':pending, 'periods':planned_periods, 'targets':planned_targets,
                'decisions':minute_decisions, 'urgent':urgent | warning_recheck_symbols(state), 'recent':recent, 'cutoffs':cutoffs,
                'period_costs':state.get('monitor_period_costs', {}),
                'now':now.timestamp(), 'capacity':workers, 'cursor':state.get('monitor_fair_cursor', 0),
                'state':{name:state.get(name, {}) for name in ('selection_checks', 'attempted_cutoffs',
                    'monitor_pending_deadlines', 'attempted_monitor_periods', 'holding_symbols', 'monitor_critical_cursor')},
                'minute_requests':{key:monitor_requests.snapshot(state,key) for key in planned_periods},
                'risk_periods':{key:sorted(_bound_risk_periods(state,key)) for key in planned_periods}})
            fair_plan['state']['notification_priority_periods'] = notification_priority_periods(state)
            tasks, fair_cursor, critical_cursor = self._fair_batch(fair_plan, workers)
            pending = [task['symbol'] for task in tasks]
            queue_counts = Counter(task_lane(frequency, minute_decisions.get(key, {}))
                                   for key, frequencies in planned_periods.items() for frequency in frequencies)
            queue_counts.subtract(task['lane'] for task in tasks)
            cutoffs = {_symbol_key(symbol): cutoffs[_symbol_key(symbol)] for symbol in pending}
            # Keep capacity for the waiting backlog when an urgent batch is
            # small. The pool starts children only when it has an actual task;
            # a smaller total queue or full-screening budget still trims it.
            workers = min(workers, backlog)
            settings = {**seed["settings"], "scope": "symbols", "codes": [], "symbols": pending,
                        "workers": workers}
            settings.pop('watch_groups', None)
            runtime_periods={_symbol_key(symbol):sorted({p['frequency'] for p in state['holding_risk_plans'].values()
                if p.get('active') and f"{p['market']}:{p['code']}"==_symbol_key(symbol)}) for symbol in pending}
            runtime_periods={k:v for k,v in runtime_periods.items() if set(v)-set(seed['settings']['frequencies'])}
            due_periods={task['key']: [task['frequency']] for task in tasks}
            subset=any(set(due_periods[_symbol_key(s)]) != (set(seed['settings']['frequencies'])|set(runtime_periods.get(_symbol_key(s),[])))
                       for s in pending)
            # A cached membership snapshot still bounds computation while the
            # database recovers; publication separately requires current membership.
            catalog_args={'holding_snapshot':list(state['holding_symbols'])}
            force_generation = {'seed': seed['run_id'], 'revision': revision,
                                'control': control_version, 'request': self._check_requests.get(None, 0)}
            minute_overrides = {key: force_generation for key, periods in planned_periods.items()
                                if state['force'] and '1m' in periods}
            minute_snapshots = {}
            for key, frequencies in due_periods.items():
                if '1m' not in frequencies:
                    continue
                minute_snapshots[key] = monitor_requests.snapshot(state, key)
                if key in minute_overrides:
                    minute_snapshots[key]['sources']['recheck'] = deepcopy(force_generation)
            launch={
                'control_version':control_version,'seed':seed['run_id'],
                'expected_run':expected_run,'expected_processed':expected_processed,
                'settings':deepcopy(settings),
                'options':deepcopy({**catalog_args,**({'monitor_periods':runtime_periods} if runtime_periods else {}),
                                   **({'due_periods':due_periods} if subset else {})}),
                'cutoffs':cutoffs,'retries':retries,'forced':forced,
                'deferred_periods':{key: [f for f in planned_periods[key] if f not in due_periods[key]] for key in cutoffs},
                'period_targets':{f'{key}:{frequency}': planned_targets[f'{key}:{frequency}']
                                  for key, periods in due_periods.items() for frequency in periods},
                'pending_before':set(state.get('force_pending_symbols',[])),
                'waiting':pending_keys-set(cutoffs),'mode':mode,'scheduled_at':now.timestamp(),
                'notification_rechecks':set(cutoffs)&urgent,
                'contract_generation':state.get('contract_check_generation', 0),
                'source_revision': revision,
                'minute_overrides': minute_overrides,
                'minute_request_snapshots': minute_snapshots,
                'fair_cursor': fair_cursor,
                'critical_cursor': critical_cursor, 'fair_plan': fair_plan,
                'service_lanes': {task['key']: task['lane'] for task in tasks},
                'service_deadlines': {task['key']: task['deadline'] for task in tasks},
                'compute_demand': demand, 'holding_demand': holding_demand, 'screening_active': screening_active,
                'pressure': pressure,
                'active_live_symbols': sorted(set(cutoffs) & recent),
                'pending_live_tasks': max(0, live_work - len(set(cutoffs) & recent)),
                'pending_background_tasks': max(0, historical_work - len(set(cutoffs) - recent)),
                'queue_counts': dict(queue_counts),
                'interval':state['settings']['interval_seconds'],'check_requests':dict(self._check_requests),
            }
        self._dispatch_monitor(launch)

    @staticmethod
    def _batch_limit(plan, capacity):
        live_pending = any(_symbol_key(s) in plan['recent'] for s in plan['symbols'])
        waves = live_batch_waves(plan, MONITOR_WORKER_WAVES) if live_pending else MONITOR_BACKGROUND_WAVES
        return min(MONITOR_BATCH_SIZE, capacity * waves)

    @staticmethod
    def _fair_batch(plan, capacity):
        """Admit holdings first, then divide the remaining effective budget."""
        recent = set(plan['recent'])
        held = set(plan['state'].get('holding_symbols', []))
        holdings = [s for s in plan['symbols'] if _symbol_key(s) in held]
        live = [s for s in plan['symbols'] if _symbol_key(s) not in held and _symbol_key(s) in recent]
        history = [s for s in plan['symbols'] if _symbol_key(s) not in held and _symbol_key(s) not in recent]
        cursor = plan['cursor']
        critical = plan['state'].get('monitor_critical_cursor') or 0
        options = dict(state=plan['state'], targets=plan['targets'], minute_decisions=plan['decisions'],
                       urgent=plan['urgent'], now=plan['now'])
        limit = SignalMonitor._batch_limit(plan, capacity)
        tasks, cursor = plan_batch(holdings, plan['periods'], limit=limit, cursor=cursor,
                                   critical_cursor=critical, **options)
        remaining = max(0, limit-len(tasks))
        ordinary_critical = critical+sum(t['lane']=='critical' for t in tasks)
        if not live:
            old, cursor = plan_batch(history, plan['periods'], limit=remaining, cursor=cursor,
                                     critical_cursor=ordinary_critical, **options)
            tasks.extend(old)
        else:
            protected = sum(_symbol_key(s) in plan['urgent'] for s in live)
            reserved = min(capacity, max(1,limit//4), len(history), max(0,remaining-max(1,protected)))
            current, cursor = plan_batch(live, plan['periods'], limit=remaining-reserved, cursor=cursor,
                                         critical_cursor=ordinary_critical, **options)
            tasks.extend(current)
            old, cursor = plan_batch(history, plan['periods'], limit=reserved, cursor=cursor,
                critical_cursor=critical+sum(t['lane']=='critical' for t in tasks), **options)
            tasks.extend(old)
        return tasks, cursor, (critical+sum(t['lane']=='critical' for t in tasks)) % 2

    @staticmethod
    def _trim_launch(launch, capacity):
        """A lower live memory budget must not leave an oversized queued wave."""
        if launch.get('fair_plan'):
            plan = launch['fair_plan']
            if capacity == plan['capacity']:
                return
            tasks, cursor, critical = SignalMonitor._fair_batch(plan, capacity)
            chosen = {task['key'] for task in tasks}
            due = {task['key']:[task['frequency']] for task in tasks}
            launch['settings']['symbols'] = [task['symbol'] for task in tasks]
            runtime = {key:value for key,value in plan['risk_periods'].items()
                       if key in chosen and set(value)-set(launch['settings']['frequencies'])}
            subset = any(set(frequencies) != (set(launch['settings']['frequencies']) | set(runtime.get(key, [])))
                         for key, frequencies in due.items())
            if subset:
                launch['options']['due_periods'] = due
            else:
                launch['options'].pop('due_periods', None)
            if runtime:
                launch['options']['monitor_periods'] = runtime
            else:
                launch['options'].pop('monitor_periods', None)
            launch['cutoffs'] = {key:plan['cutoffs'][key] for key in chosen}
            launch['waiting'] = set(plan['periods'])-chosen
            launch['deferred_periods'] = {key:[f for f in plan['periods'][key] if f not in due[key]] for key in chosen}
            launch['period_targets'] = {f"{task['key']}:{task['frequency']}":plan['targets'][f"{task['key']}:{task['frequency']}"] for task in tasks}
            launch['minute_request_snapshots'] = {key:deepcopy(plan['minute_requests'][key]) for key in chosen if '1m' in due[key]}
            for key, generation in launch['minute_overrides'].items():
                if key in launch['minute_request_snapshots']:
                    launch['minute_request_snapshots'][key]['sources']['recheck'] = deepcopy(generation)
            launch['notification_rechecks'] = chosen & set(plan['urgent'])
            launch['fair_cursor'], launch['critical_cursor'] = cursor, critical
            launch['service_lanes'] = {task['key']:task['lane'] for task in tasks}
            launch['service_deadlines'] = {task['key']:task['deadline'] for task in tasks}
            live = chosen & set(plan['recent'])
            launch['active_live_symbols'] = sorted(live)
            live_work = sum(len(fs) for key,fs in plan['periods'].items() if key in plan['recent'])
            launch['pending_live_tasks'] = live_work-len(live)
            launch['pending_background_tasks'] = sum(map(len,plan['periods'].values()))-live_work-len(chosen-live)
            counts = Counter(task_lane(f,plan['decisions'].get(key,{})) for key,fs in plan['periods'].items() for f in fs)
            counts.subtract(task['lane'] for task in tasks)
            launch['queue_counts'] = dict(counts)
            return
        waves = MONITOR_WORKER_WAVES if launch['mode'] == 'live' else MONITOR_BACKGROUND_WAVES
        limit = min(MONITOR_BATCH_SIZE, max(1, capacity) * waves)
        symbols = launch['settings']['symbols']
        if len(symbols) <= limit:
            return
        removed = {_symbol_key(symbol) for symbol in symbols[limit:]}
        live_removed = removed & set(launch.get('active_live_symbols', []))
        for key in removed:
            lane = launch.get('service_lanes', {}).get(key, 'critical')
            launch['queue_counts'][lane] = launch['queue_counts'].get(lane, 0) + 1
        launch['settings']['symbols'] = symbols[:limit]
        launch['waiting'].update(removed)
        launch['notification_rechecks'].difference_update(removed)
        launch['fair_cursor'] = (launch['fair_cursor'] - len(removed)) % len(SERVICE_PATTERN)
        launch['pending_live_tasks'] += len(live_removed)
        launch['pending_background_tasks'] += len(removed - live_removed)
        launch['active_live_symbols'] = sorted(set(launch.get('active_live_symbols', [])) - removed)
        for field in ('cutoffs', 'deferred_periods', 'minute_request_snapshots', 'service_lanes', 'service_deadlines'):
            launch[field] = {key: value for key, value in launch.get(field, {}).items() if key not in removed}
        launch['period_targets'] = {key: value for key, value in launch['period_targets'].items()
                                    if key.rsplit(':', 1)[0] not in removed}
        for field in ('due_periods', 'monitor_periods'):
            if field in launch['options']:
                launch['options'][field] = {key: value for key, value in launch['options'][field].items() if key not in removed}

    @staticmethod
    def _bind_warning_evidence(launch, state, stamp):
        settings, options = launch['settings'], launch['options']
        settings.pop('_warning_evidence_periods', None)
        selected = {
            _symbol_key(symbol): set(options.get('due_periods', {}).get(
                _symbol_key(symbol), settings['frequencies']
                + options.get('monitor_periods', {}).get(_symbol_key(symbol), [])))
            for symbol in settings['symbols']
        }
        periods = {}
        for warning in (*state.get('signals', {}).values(), *state.get('unverified_signals', {}).values()):
            needs_proof = warning.get('stage') in {'warning', 'approaching'} or (
                warning.get('stage') == 'invalidated' and stamp - (warning.get('invalidated_at') or 0) < 1800)
            key, frequency = _symbol_key(warning), warning.get('frequency')
            if (needs_proof and warning.get('signal_type') in {'native_point_preview', 'consolidation_divergence'}
                    and frequency in selected.get(key, ())):
                periods.setdefault(key, set()).add(frequency)
        if periods:
            settings['_warning_evidence_periods'] = {key: sorted(value) for key, value in periods.items()}

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
                capacity = self._monitor_capacity(launch.get('compute_demand', len(launch['cutoffs'])),
                                                  launch.get('screening_active', False),
                                                  overdue_pressure=launch.get('pressure', {}).get('overdue_pressure', False),
                                                  service_estimate=launch.get('pressure'), holding_demand=launch.get('holding_demand', 0))
                if capacity < 1:
                    return
                if launch.get('pressure'):
                    launch['pressure']['overdue_pressure'] = self._last_compute_limits.get('overdue_pressure', False)
                launch['settings']['workers'] = min(launch['settings']['workers'], capacity)
                self._trim_launch(launch, launch['settings']['workers'])
                # Memory admission may reselect symbols and periods. Bind
                # warning evidence to the final batch, not its earlier plan.
                with self._lock:
                    self._bind_warning_evidence(launch, self._load(), self.now().timestamp())
                    from chanlun.screening.source_prefetch import control_value, make_hint
                    hint = make_hint(launch, self._load(),
                        control_value(self._load(), self._control_version, os.environ.get('CHANLUN_BUILD_REVISION')),
                        self._fair_batch, batch_limit=self._batch_limit)
                launched=monitor.start(launch['settings'],**launch['options'],
                                       **({'source_prefetch': hint} if hint is not None else {}))
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
                    for key, generation in launch.get('minute_overrides', {}).items():
                        monitor_requests.add(state, key, 'recheck', generation)
                    for key in cutoffs:
                        requested = set(launch['options'].get('due_periods', {}).get(key,
                                        launch['settings']['frequencies']))
                        if key in launch['forced'] or key not in launch['retries']:
                            _clear_period_retries(state, key, requested)
                    state['attempted_cutoffs'].update(cutoffs)
                    for key, target in launch.get('period_targets', {}).items():
                        state.setdefault('attempted_monitor_periods', {})[key] = {
                            'cutoff': target, 'revision': launch.get('source_revision'),
                            'scheduled_at': launch['scheduled_at']}
                    for key, periods in launch['options'].get('due_periods', {}).items():
                        yielded_periods = state.get('live_yielded_periods', {}).get(key, [])
                        if set(yielded_periods) <= set(periods):
                            state.get('live_yielded_periods', {}).pop(key, None)
                    deferred = state.setdefault('deferred_monitor_periods', {})
                    for key, periods in launch.get('deferred_periods', {}).items():
                        if periods:
                            deferred[key] = periods
                        else:
                            deferred.pop(key, None)
                    for key in launch.get('notification_rechecks',[]):
                        self._notification_retry_after[key]=self.now().timestamp()+10
                    pending=set(state.get('force_pending_symbols',[]))
                    changed={key for key in pending
                             if key not in launch['pending_before']
                             or self._check_requests.get(key,0)!=launch['check_requests'].get(key,0)}
                    acknowledged=set(cutoffs)-changed
                    pool=set(seed['codes'])
                    newer_full_check=self._check_requests.get(None,0)!=launch['check_requests'].get(None,0)
                    state.update(force=state['force'] if newer_full_check else False,last_error='',
                                 monitor_fair_cursor=launch.get('fair_cursor', state.get('monitor_fair_cursor', 0)),
                                 monitor_critical_cursor=launch.get('critical_cursor', 0),
                                 monitor_service_pressure=deepcopy(launch.get('pressure', {})),
                                 pending_live_tasks=launch.get('pending_live_tasks', 0),
                                 pending_background_tasks=launch.get('pending_background_tasks', 0),
                                 monitor_queue_counts=launch.get('queue_counts', {}),
                                 monitor_queue_at=launch['scheduled_at'],
                                 force_pending_symbols=sorted(((launch['forced']|changed)-acknowledged)&pool),
                                 pending_symbols_count=len((launch['waiting']|changed)&pool),
                                 next_check_at=launch['scheduled_at']+launch['interval'])
                # A launched run always needs durable ownership, including a
                # shutdown while startup was in progress. Its seed identifies
                # whether later results still belong to the current pool.
                state.update(active_seed=launch['seed'],active_monitor_run=launched['run_id'],
                             active_contract_generation=launch.get('contract_generation', 0),
                             active_monitor_mode=launch['mode'],yielding_monitor_run=None,
                             active_periods=deepcopy(launch['options'].get('due_periods') or {
                                 _symbol_key(symbol): launch['settings']['frequencies']
                                 for symbol in launch['settings']['symbols']}),
                             active_period_targets=launch.get('period_targets', {}),
                             active_service_lanes=deepcopy(launch.get('service_lanes', {})),
                             active_service_deadlines=deepcopy(launch.get('service_deadlines', {})),
                             active_live_symbols=launch.get('active_live_symbols', []),
                             active_minute_override_tokens=deepcopy(launch.get('minute_request_snapshots', {})),
                             active_scheduled_at=launch['scheduled_at'],active_symbols=list(cutoffs),
                             active_cutoffs=cutoffs,active_consumed_symbols=[],
                             active_partial_periods={},
                             monitor_cursor={'run_id':launched['run_id'],'completed':0})
                self._save()
                self._startup_retries.pop('live',None)

    def _record_failures(self, keys, stamp, run_id, *, periods_by_symbol=None):
        """Bound recovery at one clock; a new clock or forced check resets it."""
        state = self._load()
        pool = set((state.get("seed") or {}).get("codes", []))
        for key in keys & pool:
            cutoff = state.get("active_cutoffs", {}).get(key)
            if type(cutoff) is not int:
                continue
            records = _period_retries(state['retry_symbols'].get(key, {}))
            periods = ((state.get('active_periods', {}).get(key, []) or [None]) if periods_by_symbol is None
                       else periods_by_symbol.get(key, ()))
            for frequency in periods:
                previous = records.get(frequency, records.get(None, {}))
                target = state.get('active_period_targets', {}).get(f'{key}:{frequency}')
                same_clock = (previous.get('period_cutoff') == target if type(target) is int
                              else previous.get('cutoff') == cutoff)
                if same_clock and previous.get('run_id') == run_id:
                    continue
                failures = previous.get('failures', 0) + 1 if same_clock else 1
                delay = max(60, state['settings']['interval_seconds']) * 2 ** (failures - 1)
                records[frequency] = {'cutoff': cutoff, 'period_cutoff': target,
                    'failures': failures, 'run_id': run_id, 'periods': [frequency] if frequency else [],
                    'retry_at': stamp + delay if failures < MONITOR_MAX_FAILURES else None}
                if frequency is not None:
                    records.pop(None, None)
            _store_period_retries(state, key, records)

    def _record_source_preparation(self, keys, errors, stamp, run_id):
        """Bound each warm-up slice without exhausting the transient failure budget."""
        state = self._load()
        for key in keys & set((state.get('seed') or {}).get('codes', [])):
            cutoff = state.get('active_cutoffs', {}).get(key)
            if type(cutoff) is not int:
                continue
            records = _period_retries(state['retry_symbols'].get(key, {}))
            for frequency in state.get('active_periods', {}).get(key, []) or [None]:
                previous = records.get(frequency, records.get(None, {}))
                if previous.get('run_id') == run_id and previous.get('cutoff') == cutoff:
                    continue
                tokens = sorted((str(error.get('frequency', '')),
                                 str(error.get('source_preparation', {}).get('progress_token', ''))[:256])
                                for error in errors if _symbol_key(error) == key
                                and (frequency is None or error.get('frequency') in (None, frequency)))
                token = hashlib.sha256(json.dumps(tokens).encode()).hexdigest()
                advanced = any(value for _, value in tokens) and token != previous.get('preparation_token')
                stalls = 0 if advanced else min(4, previous.get('preparation_stalls', 0) + 1)
                records[frequency] = {'cutoff':cutoff, 'failures':0, 'run_id':run_id,
                    'periods': [frequency] if frequency else [],
                    'period_cutoff': state.get('active_period_targets', {}).get(f'{key}:{frequency}'),
                    'retry_at':stamp + (3 if advanced else min(300, 30 * 2 ** (stalls - 1))),
                    'preparation_token':token, 'preparation_stalls':stalls}
                if frequency is not None:
                    records.pop(None, None)
            _store_period_retries(state, key, records)

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

    @staticmethod
    def _scheduler_status(manager):
        if callable(getattr(type(manager),'scheduler_status',None)):
            return manager.scheduler_status()
        return manager.status()

    def _consume_monitor(self):
        status = self._scheduler_status(self.monitor)
        prepared_checks = None
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
                if (getattr(self.monitor,'background_committed_reads',False) is True
                        and (yielded or status['status'] in ACTIVE | {'completed'})):
                    key=(status.get('run_id'), status.get('current_source_revision') or status.get('source_revision'),
                         after,self._control_version,state.get('active_seed'),status.get('completed',0))
                    def read_monitor_proofs():
                        value=self.monitor.committed_results(after)
                        if value is None:
                            return None,{},{}
                        return value,self._prepare_risk_checks(value),self._prepare_warning_checks(value)
                    bundle=self._monitor_result_reader.read(key,read_monitor_proofs)
                    if bundle is None:
                        return
                    result,risk_results,warning_results=bundle
                    prepared_checks=(risk_results,warning_results)
                else:
                    result = self.monitor.committed_results(after) if yielded or status["status"] in ACTIVE | {"completed"} else status
            except MonitorResultsReadError as exc:
                result=self._monitor_read_failure(status,after,exc)
        if result is None:
            return
        if yielded:
            result={**result,'background_yielded':True}
        # Decompression and cold structural replays must not hold the global
        # state lock used by HTTP reads and notification validity checks.
        risk_results,warning_results = (prepared_checks if prepared_checks is not None else
            (self._prepare_risk_checks(result),self._prepare_warning_checks(result)))
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
            current_frequencies = set((state.get('seed') or {}).get('settings',{}).get('frequencies',[]))
            launched_frequencies = set(result.get('settings',{}).get('frequencies',[]) or current_frequencies)
            frequencies = launched_frequencies & current_frequencies
            requested={_symbol_key(symbol):set(symbol.get('_monitor_periods',
                           launched_frequencies | _bound_risk_periods(state, _symbol_key(symbol))))
                       for symbol in result.get('settings',{}).get('symbols',[])}
            if not result.get('source_current'):
                requested={}
            def periods_for(symbol):
                return requested.get(symbol, launched_frequencies | _bound_risk_periods(state, symbol)) & _monitor_periods(state, symbol)
            def touched(item, symbols):
                return _symbol_key(item) in symbols and item.get('frequency') in periods_for(_symbol_key(item))
            if (result["status"] not in ACTIVE | {"completed"} and not yielded) or not result.get("source_current"):
                active = set(state.get('active_symbols', []))
                unchecked = {key for key in active if periods_for(key)
                             and key in (state.get('seed') or {}).get('codes', [])}
                if not unchecked:
                    state['processed_run'] = result.get('run_id')
                    state['active_consumed_symbols'] = sorted(set(state.get('active_consumed_symbols', [])) | active)
                    self._save()
                    return
                if result.get("source_current"):
                    unchecked -= set(state.get("active_consumed_symbols", []))
                for symbol in unchecked:
                    for frequency in periods_for(symbol):
                        self._mark_selection_failures({symbol}, stamp,
                            result.get('current_source_revision') or result.get('source_revision'),frequency)
                if result["status"] not in ACTIVE | {"completed"}:
                    self._record_failures(unchecked, stamp, result.get("run_id"))
                    self._acknowledge_minute_override(state, unchecked, periods_for)
                state["unverified_signals"].update({k: v for k, v in state["signals"].items()
                                                    if touched(v,unchecked)})
                state["signals"] = {k: v for k, v in state["signals"].items() if not touched(v,unchecked)}
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
            consumed = {_symbol_key(symbol) for symbol in result.get("processed_symbols", [])}
            if (result.get('run_id') == state.get('yielding_monitor_run')
                    and (yielded or result['status'] in ACTIVE and result.get('cancel_requested'))):
                # A worker can observe the cancel flag before its first period
                # and commit rows=[]. Advance the JSONL cursor, but keep this
                # symbol unfinished for requeue instead of inventing a failure.
                # Ordinary completed empty records still fail the period check.
                consumed -= {_symbol_key(symbol) for symbol in result.get('empty_result_symbols', [])}
                # Legacy multi-period tasks may commit their first period just
                # after cancellation. Only those finished periods are checked;
                # an untouched sibling is queued without a synthetic failure.
                for key in consumed:
                    planned = periods_for(key)
                    completed_periods = {p.get('frequency') for p in result.get('verified_periods', [])
                                         if _symbol_key(p) == key}
                    completed_periods |= {e.get('frequency') for e in result.get('errors', [])
                                          if _symbol_key(e) == key and e.get('frequency')}
                    if any(_symbol_key(e) == key and not e.get('frequency') for e in result.get('errors', [])):
                        completed_periods = planned
                    remaining = planned - completed_periods
                    if remaining:
                        state.setdefault('active_partial_periods', {})[key] = sorted(remaining)
                        deferred = state.setdefault('deferred_monitor_periods', {})
                        deferred[key] = sorted(set(deferred.get(key, [])) | remaining)
                        requested[key] = planned & completed_periods
            consumed &= set(state.get("active_symbols", pool))
            checked = {key for key in consumed & pool if periods_for(key)}
            self._acknowledge_minute_override(state, checked, periods_for)
            risk_results = {key: value for key, value in risk_results.items()
                            if value['original']['frequency'] in periods_for(_symbol_key(value['original']))}
            risk_retries = {_symbol_key(value['original']) for value in risk_results.values() if value['retry']}
            # A completed old period is acknowledged without publishing its
            # proof, errors or notifications into the current control scope.
            result = {**result,
                      'verified_periods': [period for period in result.get('verified_periods', [])
                          if _symbol_key(period) in checked and period.get('frequency') in periods_for(_symbol_key(period))],
                      'errors': [error for error in result.get('errors', [])
                          if _symbol_key(error) in checked and (not error.get('frequency')
                              or error['frequency'] in periods_for(_symbol_key(error)))]}
            verified = {(_symbol_key(period), period.get("frequency"))
                        for period in result.get("verified_periods", [])
                        if type(period.get("source_closed_at")) is int}
            errors = result.get("errors", [])
            missing = []
            for symbol in sorted(checked):
                market, code = symbol.split(":", 1)
                for frequency in sorted(periods_for(symbol) & frequencies):
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
            scan_types=set((state.get('seed') or {}).get('settings',{}).get('point_types',POINT_TYPES))
            signals = {}
            for row in [*result.get('selected', []), *result.get('observations', [])]:
                symbol = _symbol_key(row)
                allowed_types = POINT_TYPES if symbol in state['holding_symbols'] else scan_types
                if (symbol in pool and symbol in checked and row.get('frequency') in frequencies
                        and row.get('point', {}).get('point_type') in allowed_types
                        and (symbol, row.get('frequency')) in verified):
                    item = _signal(row)
                    signals[item['id']] = item
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
            # This NY adapter explicitly rejects its static, unverified bar-time
            # contract before fetching. Another candle cannot change that
            # contract. Keep the failed proof/diagnostic, and retry only after a
            # source revision change or an explicit user check.
            revision = result.get('source_revision')
            blocks = state.setdefault('contract_blocks', {})
            for error in result.get('errors', []):
                key = _symbol_key(error)
                if (revision and state.get('active_contract_generation', 0) == state.get('contract_check_generation', 0)
                        and error.get('market') == 'ny_futures'
                        and error.get('signal_type') != 'consolidation_divergence'
                        and 'BAR_TIME_LABEL_UNAVAILABLE' in error.get('reasons', [])
                        and error.get('frequency') in periods_for(key)):
                    blocks[f"{key}:{error['frequency']}"] = {
                        'revision': revision, 'at': stamp, 'reason': 'BAR_TIME_LABEL_UNAVAILABLE',
                        'error': error.get('error', '')}
            retry_keys = ({_symbol_key(error) for error in result.get('errors', [])
                           if not error.get('frequency') or error['frequency'] in
                               _available_monitor_periods(state, _symbol_key(error), revision)} | risk_retries) & checked
            retry_keys = {key for key in retry_keys if _available_monitor_periods(state, key, revision)}
            preparation_keys = {key for key in retry_keys - risk_retries
                                if all(error.get('reasons') == ['DATA_SOURCE_PREPARING']
                                       for error in result.get('errors', []) if _symbol_key(error) == key)}
            self._record_source_preparation(preparation_keys, result.get('errors', []), stamp, result['run_id'])
            self._record_failures(retry_keys - preparation_keys,
                                   stamp, result["run_id"])
            suspended = {key for key in checked if not _available_monitor_periods(state, key, revision)}
            if suspended:
                state['force_pending_symbols'] = [key for key in state.get('force_pending_symbols', [])
                                                  if key not in suspended]
            previous_signals = {**state["unverified_signals"], **state["signals"]}
            def warning_check(previous):
                checked_warning = warning_results.get(previous['id'])
                if checked_warning and checked_warning['original'] == previous:
                    return checked_warning['assessment'], checked_warning['continued']
                from chanlun.core.strict_structure.warning_lifecycle import SCHEMA, REASONS
                return {'schema':SCHEMA, 'state':'pending', 'reason_code':'EVIDENCE_UNAVAILABLE',
                        'reason':REASONS['EVIDENCE_UNAVAILABLE']}, None
            for identity, item in signals.items():
                legacy = item.get("legacy_id")
                if legacy and legacy != identity and legacy in previous_signals:
                    previous_signals[identity] = {**previous_signals.pop(legacy), "id": identity}
                    if identity not in known and legacy in known:
                        known[identity] = {**known[legacy], "notification_state": signal_status_key(item)}
                previous = known.get(identity)
                reclassified=self._historical_reclassification(
                    state['selection_checks'].get(f"{_symbol_key(item)}:{item['frequency']}",{}),
                    result.get('source_revision'),item.get('available_at'),item.get('price_basis_revision'))
                period_since = state.get('notification_frequency_since', {}).get(item['frequency'], 0)
                historical_period = bool(period_since and (item.get('available_at') or 0) < period_since)
                symbol_since = state.get('notification_symbol_since', {}).get(_symbol_key(item), 0)
                historical_member = bool(symbol_since and (item.get('available_at') or 0) < symbol_since)
                if item.get('signal_type') == 'native_point_preview' and _recent_clock(
                        self.now(), item['source_closed_at'], item['frequency']):
                    # A newly watched, still-unconfirmed structure can warn on
                    # the current closed bar. Do not wait another 30m candle
                    # merely because admission occurred after that bar closed.
                    historical_period = historical_member = False
                    if previous is None:
                        reclassified = False
                notification_authorized = not reclassified and not historical_period and not historical_member
                if item.get('signal_type') == 'native_point_preview':
                    item['lifecycle_cycle'] = (previous or {}).get('lifecycle_cycle', 0)
                    if previous and previous.get('stage') in {'invalidated','left'}:
                        item['lifecycle_cycle'] += 1
                    prior_preview = previous_signals.get(identity, {})
                    if (prior_preview.get('stage') == 'approaching'
                            and item.get('price_basis_revision') == prior_preview.get('price_basis_revision')
                            and item.get('anchor_price') != prior_preview.get('anchor_price')):
                        item.update(candidate_updated=True, warning_state='candidate_updated',
                                    candidate_previous_price=prior_preview.get('anchor_price'), candidate_updated_at=stamp)
                    for field in ('warning_reference', 'warning_hypothesis'):
                        if prior_preview.get('stage') == 'approaching' and prior_preview.get(field):
                            item[field] = deepcopy(prior_preview[field])
                linked_preview = next((old for old in previous_signals.values()
                    if old.get('signal_type') == 'native_point_preview' and old.get('preview_key')
                    and old['preview_key'] == item.get('preview_key') and _symbol_key(old) == _symbol_key(item)
                    and old['frequency'] == item['frequency']), None) if item['stage'] == 'confirmed' else None
                if linked_preview is None and item['stage'] == 'confirmed' and item.get('preview_key'):
                    from chanlun.core.strict_structure.identity import stable_structure_id
                    from chanlun.core.strict_structure.native_point_preview import PREVIEW_RULE
                    preview_identity = _signal_identity({**item, 'point':{
                        'point_id':stable_structure_id(PREVIEW_RULE, item['preview_key'])}})
                    linked_preview = known.get(preview_identity, {}).get('resolved_preview')
                if item.get("signal_type") == "consolidation_divergence":
                    prior_warning = previous_signals.get(identity, {})
                    if prior_warning.get('stage') == 'warning':
                        for field in ('warning_reference', 'warning_hypothesis'):
                            if prior_warning.get(field):
                                item[field] = deepcopy(prior_warning[field])
                        if item.get('anchor_price') != prior_warning.get('anchor_price'):
                            item.update(candidate_updated=True, warning_state='candidate_updated',
                                        candidate_previous_price=prior_warning.get('anchor_price'), candidate_updated_at=stamp)
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
                        if previous and previous.get('stage') in {'invalidated','left'}:
                            # The first newer, fully checked candle may restore
                            # a warning. Do not wait one extra 5m/30m candle.
                            recovery_cutoff = (previous_signals.get(identity, {}).get('source_closed_at')
                                               or previous.get('last_source_closed_at') or item['source_closed_at'])
                        item['warning_recovery_cutoff'] = recovery_cutoff
                        item['warning_recovery_pending'] = (recovery_cutoff is not None
                                                            and item['source_closed_at'] <= recovery_cutoff)
                        item['warning_reactivated'] = recovery_cutoff is not None
                        notification_authorized &= not item['warning_recovery_pending']
                notification_state = signal_status_key(item)
                from chanlun.screening.timeframe_policy import enabled as timeframe_enabled
                trade_rejection = trade_notification_rejection(item,
                    policy_enabled=timeframe_enabled((state.get('seed') or {}).get('settings', {})))
                notification_authorized &= trade_rejection is None
                timely = notification_is_live({**item, 'recorded_at':stamp}, stamp)
                # An unchanged, previously authorized occurrence stays known
                # during a market break. Age alone must not manufacture a new
                # warning when the next session opens.
                same_authorized_notice = (previous and previous.get('notification_authorized')
                                          and previous.get('notification_state') == notification_state)
                notification_authorized &= timely or bool(same_authorized_notice)
                previous_item = previous_signals.get(identity, {})
                changed = previous is None or previous.get("notification_state") != notification_state
                item["first_detected_at"] = (previous or {}).get("first_detected_at") or previous_item.get("first_detected_at")
                if previous is None:
                    item["first_detected_at"] = stamp
                item["state_changed_at"] = stamp if changed else ((previous or {}).get("state_changed_at")
                                                                  or previous_item.get("state_changed_at"))
                item["first_warned_at"] = (previous or {}).get("first_warned_at") or previous_item.get("first_warned_at")
                if linked_preview:
                    item['first_warned_at'] = linked_preview.get('first_warned_at')
                    item['confirmed_preview_id'] = linked_preview['id']
                if item["stage"] in {"warning", "approaching"} and (previous is None or previous.get("stage") in {"invalidated", "left"}
                        or previous.get('notification_authorized') is False and notification_authorized):
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
                ) or (item["stage"] in {"warning", "approaching"} and previous.get("notification_authorized") is False
                      and notification_authorized):
                    event = {**item, "recorded_at": stamp, "run_id": result["run_id"],
                             "change": "discovered" if previous is None else "changed"}
                    if item.get("signal_type") == "consolidation_divergence" or not notification_authorized:
                        event["external_notifications"] = notification_authorized
                    if reclassified:
                        event['notification_suppressed_reason']='ALGORITHM_RECLASSIFICATION'
                    elif historical_period:
                        event['notification_suppressed_reason']='CONFIRMED_BEFORE_PERIOD_ENABLED'
                    elif historical_member:
                        event['notification_suppressed_reason']='CONFIRMED_BEFORE_POOL_ADMISSION'
                    elif trade_rejection:
                        event['notification_suppressed_reason'] = trade_rejection
                    stamp_notification(event, stamp)
                    events.insert(0, event)
                    if event.get("external_notifications", True):
                        changed_events.append(event)
                known[identity] = {"stage": item["stage"], "notification_state": notification_state, "updated_at": stamp,
                                   "lifecycle_cycle": item.get("lifecycle_cycle", 0),
                                   "notification_authorized": notification_authorized,
                                   "last_source_closed_at":item.get('source_closed_at'),
                                   "warning_recovery_cutoff": item.get('warning_recovery_cutoff'),
                                   **{k: item.get(k) for k in ("first_detected_at", "first_warned_at", "state_changed_at")}}
            pending_point_previews = set()
            for identity, previous in previous_signals.items():
                if (touched(previous,checked) and identity not in signals
                        and (previous.get("market", "a"), previous["code"]) not in failed_all_periods
                        and (previous.get("market", "a"), previous["code"], previous["frequency"]) not in failed_periods
                        and not (previous.get("signal_type") == "consolidation_divergence" and (
                            (previous.get("market", "a"), previous["code"], previous["frequency"]) in failed_consolidation
                            or (previous.get("market", "a"), previous["code"], None) in failed_consolidation))):
                    if previous.get('signal_type') in {'native_point_preview', 'consolidation_divergence'}:
                        assessment, _ = warning_check(previous)
                        retired = basis_change_exit(previous, assessment, result['run_id'], stamp)
                        if retired is not None:
                            events.insert(0, retired)
                            known[identity] = {**known.get(identity, {}), 'stage': 'left',
                                'updated_at': stamp, 'state_changed_at': stamp,
                                'exit_reasons': retired['exit_reasons'], 'notification_authorized': False}
                            continue
                    if previous.get('signal_type') == 'native_point_preview' and previous.get('stage') in {'approaching','invalidated'}:
                        period = next((p for p in result.get('verified_periods', [])
                            if _symbol_key(p) == _symbol_key(previous) and p['frequency'] == previous['frequency']), None)
                        completed_preview = any(item.get('confirmed_preview_id') == identity for item in signals.values())
                        if completed_preview:
                            known[identity] = {**known.get(identity,{}), 'stage':'confirmed','updated_at':stamp}
                            continue
                        if not period or period.get('point_preview_checked') is not True:
                            pending_point_previews.add(identity)
                            continue
                        cutoff = period['source_closed_at']
                        if previous['stage'] == 'approaching':
                            assessment, continued = warning_check(previous)
                            if assessment['state'] == 'pending':
                                pending_point_previews.add(identity)
                                continue
                            if assessment['state'] == 'confirmed':
                                known[identity] = {**known.get(identity, {}), 'stage': 'confirmed', 'updated_at': stamp,
                                    'resolved_preview':{k:previous.get(k) for k in
                                        ('id','preview_key','code','market','frequency','first_warned_at')}}
                                continue
                            if continued is not None and assessment['state'] in {'observing', 'candidate_updated'}:
                                signals[identity] = continued
                                continue
                            item = {**previous,'stage':'invalidated','point_status':'invalidated','selection_status':'invalidated',
                                'source_closed_at':cutoff,'available_at':cutoff,'invalidated_at':stamp,'state_changed_at':stamp,
                                'latest_close':period.get('latest_close'),'invalidation_reason':assessment['reason'],
                                'invalidation_reason_code':assessment['reason_code'],'invalidation_checked_at':cutoff,
                                'warning_assessment':assessment,
                                'invalidation_reference':deepcopy(previous),'evidence_run_id':result['run_id'],
                                'evidence':deepcopy(period.get('evidence'))}
                            authorized = known.get(identity,{}).get('notification_authorized',False)
                            authorized &= not self._historical_reclassification(state['selection_checks'].get(
                                f"{_symbol_key(previous)}:{previous['frequency']}",{}),result.get('source_revision'),cutoff,
                                period.get('price_basis_revision'))
                            event = {**item,'change':'changed','recorded_at':stamp,'run_id':result['run_id'],
                                     'external_notifications':authorized}
                            stamp_notification(event, stamp)
                            events.insert(0,event)
                            if event['external_notifications']:
                                changed_events.append(event)
                            known[identity] = {**known.get(identity,{}),'stage':'invalidated','notification_state':signal_status_key(item),
                                'updated_at':stamp,'lifecycle_cycle':item.get('lifecycle_cycle',0)}
                            signals[identity] = item
                        elif stamp-previous.get('invalidated_at',0)<1800:
                            assessment, _ = warning_check(previous)
                            if assessment['state'] == 'invalidated':
                                from .warning_lifecycle import invalidation_update
                                signals[identity] = {**previous, **invalidation_update(assessment, period, result['run_id'])}
                            elif assessment['state'] != 'confirmed':
                                pending_point_previews.add(identity)
                        continue
                    if previous.get("signal_type") == "consolidation_divergence" and previous.get("stage") in {"warning", "invalidated"}:
                        period = next((period for period in result.get("verified_periods", [])
                                       if _symbol_key(period) == _symbol_key(previous)
                                       and period["frequency"] == previous["frequency"]), None)
                        if period is None:
                            continue
                        cutoff = period["source_closed_at"]
                        if previous["stage"] == "warning":
                            assessment, continued = warning_check(previous)
                            if assessment['state'] == 'pending':
                                pending_point_previews.add(identity)
                                continue
                            if assessment['state'] == 'confirmed':
                                known[identity] = {**known.get(identity, {}), 'stage': 'confirmed', 'updated_at': stamp}
                                continue
                            if continued is not None and assessment['state'] in {'observing', 'candidate_updated'}:
                                signals[identity] = continued
                                continue
                            item = {**previous, "stage": "invalidated", "point_status": "invalidated",
                                    "selection_status": "invalidated", "source_closed_at": cutoff,
                                    "available_at": cutoff, "invalidated_at": stamp,
                                    'evidence':deepcopy(period.get('evidence')),'evidence_run_id':result['run_id'],
                                    'invalidation_reference':deepcopy(previous),
                                    'current_price_quantum':period.get('structure_price_quantum'),
                                    'current_price_basis_revision':period.get('price_basis_revision'),
                                    "state_changed_at": stamp, 'warning_assessment':assessment,
                                    'invalidation_reason':assessment['reason'], 'invalidation_reason_code':assessment['reason_code'],
                                    'invalidation_checked_at':cutoff, 'latest_close':period.get('latest_close'),
                                    'metrics_at':cutoff, **{k:assessment.get(k) for k in ('macd_area_a','macd_area_c','macd_area_ratio')}}
                            event = {**item, "change": "changed", "recorded_at": stamp, "run_id": result["run_id"],
                                     "external_notifications": known.get(identity, {}).get("notification_authorized", False)}
                            if self._historical_reclassification(
                                    state['selection_checks'].get(f"{_symbol_key(previous)}:{previous['frequency']}",{}),
                                    result.get('source_revision'),cutoff,period.get('price_basis_revision')):
                                event.update(external_notifications=False,
                                             notification_suppressed_reason='ALGORITHM_RECLASSIFICATION')
                            stamp_notification(event, stamp)
                            events.insert(0, event)
                            if event["external_notifications"]:
                                changed_events.append(event)
                            known[identity] = {"stage": "invalidated", "notification_state": signal_status_key(item),
                                               "updated_at": stamp, "lifecycle_cycle": item.get("lifecycle_cycle", 0),
                                               "last_source_closed_at":cutoff,
                                               **{k: item.get(k) for k in ("first_detected_at", "first_warned_at", "state_changed_at")}}
                            signals[identity] = item
                        elif stamp - previous.get("invalidated_at", 0) < 1800:
                            assessment, _ = warning_check(previous)
                            if assessment['state'] == 'invalidated':
                                from .warning_lifecycle import invalidation_update
                                signals[identity] = {**previous, **invalidation_update(assessment, period, result['run_id'])}
                            elif assessment['state'] != 'confirmed':
                                pending_point_previews.add(identity)
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
            pending_preview_periods = {}
            for identity in pending_point_previews:
                item = previous_signals[identity]
                pending_preview_periods.setdefault(_symbol_key(item), set()).add(item['frequency'])
            for key in checked:
                if key not in retry_keys:
                    _clear_period_retries(state, key, periods_for(key) - pending_preview_periods.get(key, set()))
            self._record_failures(set(pending_preview_periods) - retry_keys, stamp, result['run_id'],
                                  periods_by_symbol=pending_preview_periods)
            # Failed evidence is hidden until a successful check can resolve
            # it. Keep its identity privately so recovery can record a real
            # exit, or restore an unchanged point without another discovery.
            state["unverified_signals"] = {
                identity: item for identity, item in previous_signals.items()
                if _symbol_key(item) in pool and identity not in signals
                and (identity in pending_point_previews or not touched(item,checked) and identity in state["unverified_signals"]
                     or touched(item,checked) and (
                         (item.get("market", "a"), item["code"]) in failed_all_periods
                         or (item.get("market", "a"), item["code"], item["frequency"]) in failed_periods
                         or item.get("signal_type") == "consolidation_divergence" and (
                             (item.get("market", "a"), item["code"], item["frequency"]) in failed_consolidation
                             or (item.get("market", "a"), item["code"], None) in failed_consolidation)))
            }
            # Unchanged/closed markets were not in this batch. Preserve their
            # signals and errors instead of publishing false exits for them.
            signals = {**{k: v for k, v in state["signals"].items()
                          if not touched(v,checked) and _symbol_key(v) in pool}, **signals}
            errors = [e for e in state.get("errors", []) if _symbol_key(e) not in checked
                      or e.get('frequency') and not touched(e,checked)]
            errors.extend(result.get("errors", []))
            errors.extend({'market': key.split(':', 1)[0], 'code': key.split(':', 1)[1],
                           'frequency': frequency, 'reasons': ['POINT_PREVIEW_CHECK_PENDING'],
                           'error': '未确认预警尚未完成复核，保留原记录；按周期重试，连续失败后等待新时点或手动复核'}
                          for key, periods in pending_preview_periods.items() for frequency in sorted(periods))
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
                state['contract_blocks'].pop(period_key, None)
                identities = [item["id"] for item in signals.values()
                              if _symbol_key(item) == symbol and item["frequency"] == period["frequency"]
                              and item.get("signal_type") != "consolidation_divergence"]
                inactive = sorted(identity for identity in identities
                                  if signals[identity].get('stage') in {'invalidated', 'left'})
                pending = sorted(identity for identity in pending_point_previews
                                 if _symbol_key(previous_signals[identity]) == symbol
                                 and previous_signals[identity]['frequency'] == period['frequency'])
                state["selection_checks"][period_key] = {
                    "symbol": symbol, "cutoff": period["source_closed_at"], "checked_at": stamp,
                    "revision": result.get("source_revision"), "identities": identities, "error": False,
                    **({'price_basis_revision':period['price_basis_revision']} if period.get('price_basis_revision') else {}),
                    "input_fingerprint": period.get('input_fingerprint') or (period.get('evidence') or {}).get('input_fingerprint'),
                    # Inactive events remain available to notification/history
                    # consumers; incomplete preview checks prove no exit.
                    **({'inactive_identities': inactive} if inactive else {}),
                    **({'pending_identities': pending} if pending else {}),
                }
                record_costs(state, [period])
                pending_age = state.get('monitor_pending_deadlines', {}).get(period_key, {})
                if (pending_age.get('revision') == result.get('source_revision')
                        and (pending_age.get('target') is None or period['source_closed_at'] >= pending_age['target'])):
                    state['monitor_pending_deadlines'].pop(period_key, None)
                if period['frequency'] == '1m':
                    record_minute_checkpoint(state, symbol, result.get('source_revision'), period['source_closed_at'])
                if symbol in state['holding_symbols'] and period.get('evidence'):
                    state['holding_evidence'][period_key]={**{k:period[k] for k in (
                        'market','code','name','frequency','source_closed_at','evidence','price_basis_revision') if k in period},
                        'evidence_run_id':result['run_id'],'revision':result.get('source_revision')}
            risk_changes=[]
            for key, value in risk_results.items():
                state['holding_risk_plans'][key] = value['updated']
                for change in value['events']:
                    change.update(run_id=result['run_id'], recorded_at=stamp)
                    stamp_notification(change, stamp)
                    events.insert(0,change)
                    risk_changes.append(change)
            for error in result.get("errors", []):
                if error.get("signal_type") == "consolidation_divergence":
                    continue
                symbol = _symbol_key(error)
                if symbol in checked:
                    for frequency in ({error['frequency']} if error.get('frequency') else periods_for(symbol)):
                        self._mark_selection_failures({symbol}, stamp, result.get("source_revision"), frequency)
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
            state["active_consumed_symbols"] = sorted(set(state.get("active_consumed_symbols", [])) |
                                                     (consumed - set(state.get('active_partial_periods', {}))))
            if (result["status"] == "completed" or yielded) and through >= result.get("completed", 0):
                state["processed_run"] = result["run_id"]
                state["last_batch_completed_at"] = stamp
                if yielded:
                    unfinished=set(state.get('active_symbols',[]))-set(state['active_consumed_symbols'])
                    live_yield = state.get('active_monitor_mode') == 'live'
                    if live_yield:
                        deferred = state.setdefault('deferred_monitor_periods', {})
                        for key in unfinished:
                            remaining = set(state.get('active_partial_periods', {}).get(key, periods_for(key)))
                            deferred[key] = sorted(set(deferred.get(key, [])) | remaining)
                    waiting=(set(state.get('force_pending_symbols',[]))|(set() if live_yield else unfinished))&pool
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
            waiting=state.setdefault('source_waiting_periods',{})
            for period in result.get('verified_periods',[]):
                waiting.pop(f"{_symbol_key(period)}:{period['frequency']}",None)
            real_errors=[]
            for error in errors:
                key=f"{_symbol_key(error)}:{error.get('frequency','')}"
                if error.get('reasons')==['DATA_SOURCE_PREPARING']:
                    waiting[key]={k:error.get(k) for k in ('market','code','name','frequency','source_preparation','error')}
                else:
                    if not error.get('frequency'):
                        for scope in list(waiting):
                            if scope.rsplit(':',1)[0]==_symbol_key(error):
                                waiting.pop(scope,None)
                    else:
                        waiting.pop(key,None)
                    real_errors.append(error)
            state.update(signals=signals, events=events[:500],
                         known=dict(sorted(known.items(), key=lambda item: item[1]["updated_at"], reverse=True)[:10000]),
                         monitor_cursor={"run_id": result["run_id"], "completed": through},
                         last_cutoffs=latest_cutoffs, errors=real_errors,
                         last_error="部分标的行情或计算异常，请查看问题列表" if real_errors else "")
            retain_notifications(state,stamp,changed_events+risk_changes)
            pool_changed = resolve_pending(state, checked, result.get('source_revision'))
            self._save()
            # The durable state is the source of truth for dispatch. A crash
            # before enqueue is recovered by _publish_notifications; enqueue
            # must never expose a fact which exists only in process memory.
            notifications_enabled = state.get('dingtalk_enabled')
            analytics_events = self._snapshot_analytics_events(events)
            committed = result.get("completed", 0)
            # An asynchronous proof may describe an earlier committed prefix.
            # The current status can reveal more work for that same run and
            # source, without widening the proof just consumed.
            if (status.get("run_id") == result.get("run_id")
                    and result.get("source_revision")
                    and status.get("source_revision") == result.get("source_revision")
                    and status.get("current_source_revision", status.get("source_revision"))
                        == result.get("current_source_revision", result.get("source_revision"))
                    and type(status.get("completed")) is int):
                committed = max(committed, status["completed"])
            if after < through < committed:
                # Preserve bounded reads but drain an already committed
                # backlog without another fixed three-second polling sleep.
                self._wake.set()
        if pool_changed:
            self._sync_watchlist()
        wake = getattr(self._notifications,'wake_validation',None)
        if callable(wake):
            wake({(_symbol_key(p),p['frequency']) for p in result.get('verified_periods',[])})
        try:
            from .higher_context import prefetch
            prefetch(self.screening,result)
        except (OSError,ValueError,TypeError,KeyError):
            pass  # Secondary context never invalidates the primary result.
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

    def _snapshot_analytics_events(self, events):
        from .signal_analytics import SignalAnalytics, snapshot_events as snapshot_analytics_events
        analytics = self.analytics
        if (type(analytics) is SignalAnalytics
                and getattr(analytics.record, '__func__', None) is SignalAnalytics.record):
            return snapshot_analytics_events(events)
        return deepcopy(events)  # Preserve the payload contract for custom consumers.

    def _prepare_warning_checks(self, result):
        """Read large frozen sources outside the HTTP/notification state lock."""
        if not result.get('source_current'):
            return {}
        periods = {(_symbol_key(p), p['frequency']):p for p in result.get('verified_periods', [])}
        present = {_signal_identity(row) for row in [*result.get('selected', []), *result.get('observations', [])]}
        present.update(_consolidation_signal(row)['id'] for row in result.get('consolidation_alerts', []))
        with self._lock:
            state = self._load()
            if not state['enabled'] or result.get('run_id') != state.get('active_monitor_run'):
                return {}
            previous = {**state['unverified_signals'], **state['signals']}
            pending = [deepcopy(item) for identity, item in previous.items()
                       if identity not in present and (_symbol_key(item), item['frequency']) in periods
                       and item.get('signal_type') in {'native_point_preview', 'consolidation_divergence'}
                       and item.get('stage') in {'approaching', 'warning', 'invalidated'}]
        from .warning_lifecycle import recheck_warning
        cache, output = {}, {}
        for item in pending:
            period = periods[_symbol_key(item), item['frequency']]
            assessment, continued = recheck_warning(item, period, result['run_id'], self.monitor.root, cache)
            output[item['id']] = {'original':item, 'assessment':assessment, 'continued':continued}
        return output

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
                     if plan.get('active') and _symbol_key(plan) in checked & set(state['holding_symbols'])
                     and plan['frequency'] in next((s.get('_monitor_periods',result.get('settings',{}).get('frequencies',[]))
                         for s in result.get('settings',{}).get('symbols',[]) if _symbol_key(s)==_symbol_key(plan)),
                         result.get('settings',{}).get('frequencies',state.get('monitor_frequencies',[])))}
            previous = deepcopy(state['selection_checks'])
        periods = {f"{_symbol_key(p)}:{p['frequency']}": p for p in result.get('verified_periods',[])}
        active_ids = {_signal_identity(row) for row in result.get('selected',[])+result.get('observations',[])}
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
                    values.append((item.get('warning_reference') or {}).get('evidence_run_id'))
                    values.append(((item.get('invalidation_reference') or {}).get('warning_reference') or {}).get('evidence_run_id'))
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
            if time.monotonic()-getattr(self,'_warning_cleanup_at',0)>3600:
                from .warning_evidence import cleanup
                with self._lock:
                    saved=self._load()
                    origins=deepcopy({k:saved.get(k,{ } if k!='events' else []) for k in
                                      ('signals','unverified_signals','pending_notifications','events')})
                cleanup(root,origins,now=self.now().timestamp())
                self._warning_cleanup_at=time.monotonic()
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
