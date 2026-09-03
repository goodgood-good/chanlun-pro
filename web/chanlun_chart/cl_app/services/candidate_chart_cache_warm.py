"""Low-priority warming for the highest-ranked chart candidates.

Persisted snapshots are restored first. The complete top-20 candidate working
set covers all four display periods in the same critical order as the browser.
Missing A-share snapshots are built from QMT's local store. If the snapshot's
last completed bar proves that the local store is behind the live exchange
clock, only the recent QMT tail is downloaded before rebuilding. The worker
runs at reduced OS priority and yields while an interactive request is active.
"""
from __future__ import annotations

import ctypes
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone

from chanlun.cl_utils import query_cl_chart_config
from chanlun.tools.log_util import LogUtil

from .chart_cache import (
    _build_cache_key,
    _get_chart_cache_entry,
    _get_chart_cache_entry_ram_only,
    chart_cache_metrics,
)
from .chart_compute import compute_and_cache_chart_data, market_now_trading
from .user_activity import _get_last_user_request_time

_MAX_TARGETS = 20
_FREQUENCY_TARGET_LIMITS = tuple(
    (frequency, _MAX_TARGETS) for frequency in ("1m", "5m", "30m", "d")
)
_DISK_RESTORE_WORKERS = 8
_START_DELAY_SECONDS = 0.35
_USER_IDLE_SECONDS = 1.5
_USER_IDLE_POLL_SECONDS = 0.1
_LOCAL_CANDIDATE_TTL_SECONDS = 180.0
_REFRESH_CHECK_INTERVAL_SECONDS = 60.0
_REALTIME_CAPACITY_STABILITY_SECONDS = 60.0
_INCOMPLETE_RETRY_SECONDS = 60.0
_TRADING_REFRESH_AFTER_SECONDS = {
    "1m": 240.0,
    "5m": 240.0,
    "30m": 1200.0,
    "d": 1200.0,
}
_CLOSED_REFRESH_AFTER_SECONDS = 3600.0
_BAR_PUBLICATION_GRACE_SECONDS = 20.0
_INCREMENTAL_REFRESH_DAYS = 2
_DOWNLOAD_REFRESH_TARGET_LIMITS = {
    "1m": 5,
    "5m": _MAX_TARGETS,
}
_INTRADAY_FREQUENCY_MINUTES = {
    "1m": 1,
    "5m": 5,
    "30m": 30,
}
_GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "UNRESOLVED": 3}
_CURRENT_SELECTION_STAGES = frozenset(
    {"observed", "monitoring", "approaching", "triggered", "executable", "active"}
)
_STAGE_ORDER = {
    "executable": 0,
    "triggered": 1,
    "armed": 2,
    "formed": 3,
    "approaching": 4,
    "observed": 5,
    "monitoring": 6,
    "active": 6,
}
_POINT_ORDER = {
    "1buy": 0,
    "1sell": 1,
    "2buy": 2,
    "2sell": 3,
    "3buy": 4,
    "3sell": 5,
}
_REVIEW_PRIORITY_STAGES = frozenset(
    {"approaching", "formed", "armed", "observed", "triggered", "executable"}
)
_REVIEW_PRIORITY_RISK_GATES = frozenset({"GREEN", "AMBER", "RED", "UNRESOLVED"})
_REVIEW_PRIORITY_CONFIDENCE = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "UNRESOLVED": 0}
_REVIEW_PRIORITY_POSITION_BANDS = {
    "BLOCKED": (8, 0, 19),
    "NOT_ACTIONABLE": (30, 20, 39),
    "UNRESOLVED": (30, 20, 39),
    "CONDITIONAL": (55, 40, 69),
    "RECOMMENDED": (72, 70, 89),
    "STRUCTURAL_SELL_REVIEW": (82, 80, 89),
    "MANUAL_ATTENTION_SELL_REVIEW": (92, 90, 100),
}
_REVIEW_PRIORITY_STAGE_ADJUSTMENT = {
    "executable": 5,
    "triggered": 5,
    "observed": 3,
    "armed": 3,
    "formed": 2,
    "approaching": 1,
}
_NOT_ACTIONABLE_RECOMMENDATIONS = frozenset(
    {"WAITING_STRUCTURE", "GEOMETRY_AWAITING_CONFIRMATION", "WAITING_SEGMENT_DIFFERENCE"}
)
_MANUAL_ATTENTION_SOURCES = frozenset(
    {"MANUAL_ATTENTION_MONITOR", "HOLDING_MONITOR", "VIRTUAL_HOLDING_MONITOR"}
)
_SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))
_state_lock = threading.Lock()
_pending: tuple[tuple[str, str], ...] | None = None
_last_completed: tuple[tuple[str, str], ...] | None = None
_last_completed_at = 0.0
_worker_running = False
_background_work_allowed = True
_realtime_capacity_ready_since = 0.0
_next_retry_at = 0.0
_local_candidate_scopes: frozenset[tuple[str, str, str]] = frozenset()
_local_candidate_updated_at = 0.0
_metrics = {
    "runs": 0,
    "disk_hits": 0,
    "disk_misses": 0,
    "refreshes": 0,
    "local_attempts": 0,
    "local_hits": 0,
    "local_yields": 0,
    "local_misses": 0,
    "download_attempts": 0,
    "download_hits": 0,
    "errors": 0,
    "last_elapsed_ms": 0,
}


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _signal_market(signal: Mapping[str, object]) -> str:
    explicit = _text(signal.get("market")).lower()
    if explicit:
        return explicit
    code = _text(signal.get("code")).upper()
    return "us" if code.endswith(".US") else "a"


def _finite_review_priority(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        priority = float(value)
    except (TypeError, ValueError):
        return None
    return priority if math.isfinite(priority) and priority >= 0 else None


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = _text(value)
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_SHANGHAI_TIMEZONE)
    return parsed


def _strict_one_minute_segment(signal: Mapping[str, object]) -> Mapping[str, object] | None:
    segment = signal.get("segment_difference_1m")
    if not isinstance(segment, Mapping) or not segment:
        return None
    if _text(segment.get("source_frequency") or "1m") != "1m":
        return None
    raw_level = segment.get("recursive_level")
    try:
        recursive_level = 0.0 if raw_level in (None, "") else float(raw_level)
    except (TypeError, ValueError):
        return None
    if not recursive_level.is_integer() or int(recursive_level) != 0:
        return None
    return segment


def _segment_boundary_expired(
    signal: Mapping[str, object],
    observed_at: datetime,
) -> bool:
    segment = _strict_one_minute_segment(signal)
    if segment is None:
        return False
    if signal.get("synthetic_notification_projection") is True:
        persisted = _text(signal.get("notification_segment_difference_boundary_status"))
        if persisted == "expired":
            return True
        if persisted in {"absent", "unavailable", "unknown", "not_applicable"}:
            return False
    side = _text(signal.get("side") or segment.get("side"))
    if side != "buy":
        return False
    profile = signal.get("execution_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    reason_codes: set[str] = set()
    for values in (
        signal.get("decision_reasons"),
        profile.get("advisory_reason_codes"),
        profile.get("hard_block_reason_codes"),
    ):
        if isinstance(values, list):
            reason_codes.update(_text(value) for value in values if _text(value))
    if "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED" in reason_codes:
        return True
    boundary = signal.get("entry_execution_boundary")
    boundary = boundary if isinstance(boundary, Mapping) else {}
    valid_until = _parse_datetime(
        boundary.get("entry_valid_until")
        or signal.get("notification_segment_difference_valid_until")
    )
    if valid_until is None:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=_SHANGHAI_TIMEZONE)
    return valid_until <= observed_at


def _position_recommendation_status(
    signal: Mapping[str, object],
    profile: Mapping[str, object],
    observed_at: datetime,
) -> str:
    recommendation = signal.get("position_recommendation")
    if not isinstance(recommendation, Mapping):
        recommendation = profile.get("position_recommendation")
    recommendation = recommendation if isinstance(recommendation, Mapping) else {}
    side = _text(signal.get("side") or recommendation.get("side"))
    if side == "buy" and _segment_boundary_expired(signal, observed_at):
        return "BLOCKED"
    return _text(recommendation.get("status"))


def _immediate_five_minute_signal_fresh(
    signal: Mapping[str, object],
    observed_at: datetime,
) -> bool:
    setup = signal.get("setup_5m")
    setup = setup if isinstance(setup, Mapping) else {}
    started = _parse_datetime(setup.get("available_at"))
    ended = _parse_datetime(
        signal.get("monitor_observed_at") or signal.get("observed_at")
    ) or observed_at
    if started is None or ended < started:
        return False
    if _signal_market(signal) != "a":
        return (ended - started).total_seconds() <= 10 * 60
    started = started.astimezone(_SHANGHAI_TIMEZONE)
    ended = ended.astimezone(_SHANGHAI_TIMEZONE)
    if started.date() != ended.date():
        return False
    trading_minutes = 0
    for hour, minute in ((9, 31), (13, 1)):
        close_at = started.replace(hour=hour, minute=minute, second=0, microsecond=0)
        for offset in range(120):
            minute_close = close_at + timedelta(minutes=offset)
            if started < minute_close <= ended:
                trading_minutes += 1
    return trading_minutes <= 10


def _review_priority(
    signal: Mapping[str, object],
    observed_at: datetime | None = None,
) -> float | None:
    if signal.get("realtime_notification") is True:
        return 110.0 if signal.get("notification_delivery_status") == "failed" else 100.0
    supplied = _finite_review_priority(signal.get("review_priority"))
    if supplied is not None:
        return supplied

    stage = _text(signal.get("lifecycle_stage"))
    risk = signal.get("higher_timeframe_risk")
    warmup = signal.get("warmup")
    if (
        stage not in _REVIEW_PRIORITY_STAGES
        or not isinstance(risk, Mapping)
        or not isinstance(warmup, Mapping)
    ):
        return None
    gates = tuple(
        _text(risk.get(key) or "UNRESOLVED")
        for key in ("market_gate", "sector_gate", "symbol_gate")
    )
    if any(gate not in _REVIEW_PRIORITY_RISK_GATES for gate in gates):
        return None

    evaluated_at = observed_at or datetime.now(_SHANGHAI_TIMEZONE)
    profile = signal.get("execution_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    recommendation = _text(profile.get("recommendation"))
    position_status = _position_recommendation_status(signal, profile, evaluated_at)
    position_blocked = position_status == "BLOCKED"
    context_grade = _text(profile.get("context_grade") or "UNRESOLVED")
    exact_green = not position_blocked and (
        recommendation == "READY"
        or (
            not recommendation
            and bool(signal.get("entry_allowed") or signal.get("exit_allowed"))
        )
    )
    if position_blocked:
        confidence = "LOW"
    elif exact_green:
        confidence = "HIGH"
    elif context_grade in {"C", "UNRESOLVED"}:
        confidence = "LOW"
    elif recommendation == "CAUTION" or stage in {"observed", "triggered", "executable"}:
        confidence = "MEDIUM"
    elif stage in {"formed", "armed"}:
        confidence = "LOW"
    else:
        confidence = "UNRESOLVED"

    if position_status not in _REVIEW_PRIORITY_POSITION_BANDS:
        if recommendation == "BLOCKED":
            position_status = "BLOCKED"
        elif exact_green:
            position_status = "RECOMMENDED"
        elif recommendation == "CAUTION":
            position_status = "CONDITIONAL"
        elif recommendation in _NOT_ACTIONABLE_RECOMMENDATIONS:
            position_status = "NOT_ACTIONABLE"
        else:
            position_status = ""

    sources = signal.get("selection_sources")
    sources = sources if isinstance(sources, list) else []
    actionable_sell_review = (
        _text(signal.get("side")) == "sell"
        and stage in {"triggered", "executable", "active"}
        and (position_status in {"CONDITIONAL", "RECOMMENDED"} or exact_green)
    )
    if actionable_sell_review and any(
        _text(source) in _MANUAL_ATTENTION_SOURCES for source in sources
    ):
        position_status = "MANUAL_ATTENTION_SELL_REVIEW"
    elif actionable_sell_review and _immediate_five_minute_signal_fresh(
        signal, evaluated_at
    ):
        position_status = "STRUCTURAL_SELL_REVIEW"

    band = _REVIEW_PRIORITY_POSITION_BANDS.get(position_status)
    if band is None:
        return None
    base, minimum, maximum = band
    score = (
        base
        + _REVIEW_PRIORITY_CONFIDENCE[confidence]
        + (2 if exact_green else 0)
        + _REVIEW_PRIORITY_STAGE_ADJUSTMENT.get(stage, 0)
        + sum(gate == "GREEN" for gate in gates)
        - (2 if signal.get("monitor_only") is True else 0)
    )
    return float(min(maximum, max(minimum, score)))


def _review_stage_rank(signal: Mapping[str, object]) -> float:
    stage = _text(signal.get("lifecycle_stage"))
    setup = signal.get("setup_5m")
    if (
        stage == "observed"
        and isinstance(setup, Mapping)
        and setup.get("status") == "confirmed"
    ):
        return _STAGE_ORDER["armed"] + 0.5
    return float(_STAGE_ORDER.get(stage, 99))


def _signal_sort_key(
    signal: Mapping[str, object],
    sector_ranks: Mapping[str, int],
) -> tuple[object, ...]:
    profile = signal.get("execution_profile")
    profile = profile if isinstance(profile, Mapping) else {}
    grade = _text(profile.get("context_grade") or signal.get("context_grade"))
    grade = grade.upper() if grade else "UNRESOLVED"
    priority = _review_priority(signal)
    sector = signal.get("sector")
    sector = sector if isinstance(sector, Mapping) else {}
    try:
        sector_rank = int(sector_ranks.get(_text(sector.get("sector_id"))))
    except (TypeError, ValueError):
        sector_rank = 2**31 - 1
    return (
        _GRADE_ORDER.get(grade, _GRADE_ORDER["UNRESOLVED"]),
        0 if signal.get("realtime_notification") is True else 1,
        1 if priority is None else 0,
        0.0 if priority is None else -priority,
        _review_stage_rank(signal),
        sector_rank,
        _POINT_ORDER.get(_text(signal.get("point_type")), 99),
        _text(signal.get("code")).upper(),
        _text(signal.get("signal_id")),
    )


def candidate_chart_targets(
    snapshot: Mapping[str, object] | object,
    *,
    limit: int = _MAX_TARGETS,
    preferred_view: Mapping[str, object] | object | None = None,
) -> tuple[tuple[str, str], ...]:
    if not isinstance(snapshot, Mapping):
        return ()
    rows = snapshot.get("selection_signals")
    if not isinstance(rows, list):
        rows = snapshot.get("signals")
    if not isinstance(rows, list):
        return ()
    sectors = snapshot.get("sectors")
    sectors = sectors if isinstance(sectors, list) else []
    sector_ranks: dict[str, int] = {}
    for sector in sectors:
        if not isinstance(sector, Mapping):
            continue
        try:
            sector_ranks[_text(sector.get("sector_id"))] = int(sector.get("rank"))
        except (TypeError, ValueError):
            continue
    admitted: list[Mapping[str, object]] = [
        row
        for row in rows
        if (
            isinstance(row, Mapping)
            and _text(row.get("code"))
            and _text(row.get("lifecycle_stage")) in _CURRENT_SELECTION_STAGES
        )
    ]
    admitted.sort(key=lambda signal: _signal_sort_key(signal, sector_ranks))

    # The page is account-scoped, but warming used to be based only on the
    # unfiltered global queue.  A user looking at (for example) 三买 could see
    # its second card at global rank 185, while the only four-period snapshots
    # prepared in RAM belonged to global ranks 1-5.  Preserve the exact review
    # ordering within each set, but move rows admitted by the saved account
    # view to the front.  The target/entry limits do not grow, so this improves
    # the charts the user can actually click without increasing cache memory.
    view = preferred_view if isinstance(preferred_view, Mapping) else {}
    if view:
        preferred = [row for row in admitted if _matches_preferred_view(row, view)]
        if preferred and len(preferred) != len(admitted):
            preferred_ids = {id(row) for row in preferred}
            admitted = preferred + [row for row in admitted if id(row) not in preferred_ids]
    targets: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in admitted:
        target = (_signal_market(row), _text(row.get("code")).upper())
        if target in seen:
            continue
        seen.add(target)
        targets.append(target)
        if len(targets) >= max(1, min(int(limit), _MAX_TARGETS)):
            break
    return tuple(targets)


def _matches_preferred_view(
    signal: Mapping[str, object],
    view: Mapping[str, object],
) -> bool:
    """Mirror the stable account filters that determine the visible queue.

    Search text and the transient sector chip intentionally remain browser-only
    and therefore are not used here.  The account-backed filters below are
    normalized before persistence; unknown values fail closed to the unfiltered
    behavior rather than excluding every warm target.
    """

    point_type = _text(view.get("pointType"))
    signal_point = _text(signal.get("point_type"))
    signal_side = _text(signal.get("side"))
    if not signal_side:
        if signal_point.endswith("buy"):
            signal_side = "buy"
        elif signal_point.endswith("sell"):
            signal_side = "sell"
    if point_type == "buy" and signal_side != "buy":
        return False
    if point_type == "sell" and signal_side != "sell":
        return False
    if point_type not in {"", "all", "buy", "sell"} and signal_point != point_type:
        return False

    lifecycle = _text(view.get("lifecycle"))
    if lifecycle not in {"", "all"} and _text(signal.get("lifecycle_stage")) != lifecycle:
        return False

    market = _text(view.get("market")).lower()
    if market not in {"", "all"} and _signal_market(signal) != market:
        return False

    source = _text(view.get("signalSource"))
    sources = signal.get("selection_sources")
    sources = sources if isinstance(sources, list) else []
    normalized_sources = {_text(value) for value in sources}
    if source == "screening" and signal.get("synthetic_notification_projection") is True:
        return False
    if source == "notification" and signal.get("realtime_notification") is not True:
        return False
    if source in {"attention", "holding"} and not normalized_sources.intersection(
        _MANUAL_ATTENTION_SOURCES
    ):
        return False
    if source == "watchlist" and not normalized_sources.intersection(
        {"ACTIVE_WATCHLIST_MONITOR", "WATCHLIST_MONITOR", "US_AUXILIARY_MONITOR"}
    ):
        return False

    review_stage = _text(view.get("reviewStage"))
    stage = _text(signal.get("lifecycle_stage"))
    review_stages = {
        "forming": {"observed", "approaching"},
        "notified": {"triggered", "executable"},
        "tracking": {"monitoring", "active"},
    }
    if review_stage in review_stages and stage not in review_stages[review_stage]:
        return False
    return True


def candidate_local_history_ready(
    market: str,
    code: str,
    frequency: str = "5m",
) -> bool:
    """Whether one candidate period has a recently verified QMT local tail.

    This is deliberately short-lived. It is only a hint for a first chart
    request whose cache is empty or too old; forced recovery and arbitrary
    symbols still perform their normal download. Scheduling alone never grants
    this hint: the warmer must first prove the period's last completed bar.
    """

    scope = (
        _text(market).lower(),
        _text(code).upper(),
        _text(frequency).lower(),
    )
    with _state_lock:
        if time.time() - _local_candidate_updated_at > _LOCAL_CANDIDATE_TTL_SECONDS:
            return False
        return scope in _local_candidate_scopes


def _expected_latest_completed_bar_time(
    frequency: str,
    *,
    now: float,
) -> int | None:
    """Return the latest observable A-share intraday close in epoch seconds.

    QMT minute bars use completed-boundary labels.  The two auction sessions
    must be grouped independently, otherwise a 5m/30m boundary would drift
    across the lunch break.  A short publication grace avoids declaring a bar
    stale while QMT is still publishing the just-closed interval.
    """

    width = _INTRADAY_FREQUENCY_MINUTES.get(_text(frequency).lower())
    if width is None:
        return None
    observed = datetime.fromtimestamp(
        now - _BAR_PUBLICATION_GRACE_SECONDS,
        tz=_SHANGHAI_TIMEZONE,
    )
    if observed.weekday() >= 5:
        return None
    session_bounds = (
        ((9, 31), (11, 30)),
        ((13, 1), (15, 0)),
    )
    latest: datetime | None = None
    for (start_hour, start_minute), (end_hour, end_minute) in session_bounds:
        first_close = observed.replace(
            hour=start_hour,
            minute=start_minute,
            second=0,
            microsecond=0,
        )
        last_close = observed.replace(
            hour=end_hour,
            minute=end_minute,
            second=0,
            microsecond=0,
        )
        if observed < first_close:
            continue
        visible_close = min(observed.replace(second=0, microsecond=0), last_close)
        completed_one_minute = (
            int((visible_close - first_close).total_seconds() // 60) + 1
        )
        completed_groups = completed_one_minute // width
        if completed_groups <= 0:
            continue
        latest = first_close + timedelta(
            minutes=completed_groups * width - 1,
        )
    return None if latest is None else int(latest.timestamp())


def _entry_bar_lagging(
    entry: object,
    frequency: str,
    *,
    market_is_trading: bool,
    now: float,
) -> bool:
    """Whether a claimed full snapshot is behind the completed market grid."""

    if not market_is_trading:
        return False
    expected = _expected_latest_completed_bar_time(frequency, now=now)
    if expected is None:
        return False
    if not isinstance(entry, Mapping):
        return True
    latest = entry.get("max_time")
    if latest is None:
        data = entry.get("data")
        times = data.get("t") if isinstance(data, Mapping) else None
        latest = times[-1] if isinstance(times, list) and times else None
    if (
        not isinstance(latest, (int, float))
        or isinstance(latest, bool)
        or not math.isfinite(float(latest))
    ):
        return True
    return float(latest) < expected


def _download_refresh_allowed(
    market: str,
    code: str,
    frequency: str,
    target_ranks: Mapping[tuple[str, str], int],
) -> bool:
    if market != "a":
        return False
    limit = _DOWNLOAD_REFRESH_TARGET_LIMITS.get(frequency, 0)
    return target_ranks.get((market, code), _MAX_TARGETS) < limit


def _entry_refresh_due(
    entry: object,
    frequency: str,
    *,
    market_is_trading: bool,
    now: float,
) -> bool:
    """Return whether an existing full snapshot should be rebuilt locally.

    Presence alone is insufficient: the old worker marked a restored snapshot
    complete forever, so a dashboard left running through a trading morning
    eventually hit the synchronous ``too_stale`` path on the next symbol click.
    The refresh cadence stays below the route's hard stale boundary while using
    only QMT's already-prepared local store.
    """

    if not isinstance(entry, Mapping) or entry.get("is_full_snapshot") is not True:
        return True
    if _entry_bar_lagging(
        entry,
        frequency,
        market_is_trading=market_is_trading,
        now=now,
    ):
        return True
    validated_at = entry.get("validated_at")
    if (
        not isinstance(validated_at, (int, float))
        or isinstance(validated_at, bool)
        or not math.isfinite(float(validated_at))
        or float(validated_at) <= 0
    ):
        return True
    threshold = (
        _TRADING_REFRESH_AFTER_SECONDS.get(frequency, 1200.0)
        if market_is_trading
        else _CLOSED_REFRESH_AFTER_SECONDS
    )
    return now - float(validated_at) >= threshold


def _lower_current_thread_priority() -> None:
    if os.name != "nt":
        return
    try:
        kernel32 = ctypes.windll.kernel32
        # THREAD_PRIORITY_BELOW_NORMAL. Only this background thread changes;
        # request handlers and the native screening workers retain priority.
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), -1)
    except Exception:
        pass


def _background_warm_is_allowed() -> bool:
    with _state_lock:
        return _background_work_allowed


def _wait_for_interactive_idle() -> bool:
    while True:
        if not _background_warm_is_allowed():
            return False
        last_request = _get_last_user_request_time()
        remaining = _USER_IDLE_SECONDS - (time.time() - last_request)
        if remaining <= 0:
            return True
        time.sleep(min(_USER_IDLE_POLL_SECONDS, remaining))


def _realtime_capacity_observation(
    snapshot: Mapping[str, object] | object,
) -> bool | None:
    """Return the live-session capacity gate, or ``None`` outside that gate.

    Candidate chart warming is useful but optional.  While the A-share minute
    monitor is open it must not compete with the notification SLA.  Older test
    snapshots and after-hours snapshots intentionally remain unrestricted.
    """

    if not isinstance(snapshot, Mapping):
        return None
    runtime = snapshot.get("runtime_health")
    if not isinstance(runtime, Mapping):
        return None
    if runtime.get("priority_monitor_session_open") is not True:
        return None
    return runtime.get("realtime_alert_capacity_ready") is True


def _warm_targets(
    targets: tuple[tuple[str, str], ...],
    frequency_target_limits: tuple[tuple[str, int], ...] = _FREQUENCY_TARGET_LIMITS,
) -> None:
    global _last_completed, _last_completed_at
    global _local_candidate_scopes, _local_candidate_updated_at
    global _next_retry_at
    _lower_current_thread_priority()
    started = time.perf_counter()
    interrupted = not _background_warm_is_allowed()
    hits = misses = refreshes = errors = 0
    local_attempts = local_hits = local_yields = local_misses = 0
    download_attempts = download_hits = 0
    target_ranks = {target: index for index, target in enumerate(targets)}
    verified_scopes: set[tuple[str, str, str]] = set()
    jobs: list[tuple[str, str, str, dict, str]] = []
    for target_index, (market, code) in enumerate(targets):
        try:
            config = query_cl_chart_config(market, code)
            if not isinstance(config, dict):
                config = {}
            # Interleave periods by ranked symbol so each visible candidate
            # becomes fully multi-period-ready before the next candidate. 1m
            # starts first because it is the largest payload and the browser's
            # all-frame readiness gate cannot reveal before it completes.
            for frequency, target_limit in frequency_target_limits:
                if target_index >= target_limit:
                    continue
                cache_key = _build_cache_key(market, code, frequency, config)
                jobs.append((market, code, frequency, config, cache_key))
        except Exception as exc:
            errors += 1
            LogUtil.warning(
                f"[candidate_chart_warm] failed market={market} code={code}: {exc}"
            )
    restored: list[dict | None] = [None] * len(jobs)

    def restore(index: int, cache_key: str) -> tuple[int, dict | None]:
        _lower_current_thread_priority()
        return index, _get_chart_cache_entry(cache_key)

    # Windows may spend hundreds of milliseconds opening/unpickling each
    # multi-megabyte snapshot under real-time scanning. Bounded parallel reads
    # shorten startup warming without increasing native-market-data concurrency.
    if jobs and not interrupted:
        worker_count = min(_DISK_RESTORE_WORKERS, len(jobs))
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="CandidateChartDisk",
        ) as executor:
            future_jobs = {
                executor.submit(restore, index, job[4]): (index, job)
                for index, job in enumerate(jobs)
            }
            for future in as_completed(future_jobs):
                if not _background_warm_is_allowed():
                    interrupted = True
                    for queued in future_jobs:
                        queued.cancel()
                    break
                index, job = future_jobs[future]
                try:
                    _, restored[index] = future.result()
                except Exception as exc:
                    errors += 1
                    market, code, frequency, _, _ = job
                    LogUtil.warning(
                        f"[candidate_chart_warm] disk restore failed "
                        f"market={market} code={code} frequency={frequency}: {exc}"
                    )

    observed_at = time.time()
    trading_by_market: dict[str, bool] = {}
    for market, *_rest in jobs:
        if market == "a" and market not in trading_by_market:
            trading_by_market[market] = market_now_trading(market)

    work: list[tuple[tuple[str, str, str, dict, str], bool]] = []
    for job, entry in zip(jobs, restored):
        if entry is None or entry.get("is_full_snapshot") is not True:
            misses += 1
            work.append((job, False))
        elif job[0] == "a" and _entry_refresh_due(
            entry,
            job[2],
            market_is_trading=trading_by_market.get(job[0], True),
            now=observed_at,
        ):
            refreshes += 1
            needs_download = _entry_bar_lagging(
                entry,
                job[2],
                market_is_trading=trading_by_market.get(job[0], True),
                now=observed_at,
            ) and _download_refresh_allowed(
                job[0],
                job[1],
                job[2],
                target_ranks,
            )
            work.append((job, needs_download))
        else:
            hits += 1
            if job[0] == "a":
                verified_scopes.add((job[0], job[1], job[2]))
    # Prefer the shared local QMT store.  A recent validation timestamp alone is
    # not proof of freshness: if the last completed bar trails the exchange
    # grid, refresh only the recent native tail and then rebuild the full chart.
    # US/HK history remains quota-controlled and is never fetched here.
    for job, needs_download in work:
        if interrupted or not _background_warm_is_allowed():
            interrupted = True
            break
        market, code, frequency, config, cache_key = job
        if market != "a":
            continue
        if not _wait_for_interactive_idle():
            interrupted = True
            break
        existing = _get_chart_cache_entry_ram_only(cache_key)
        if (
            existing is not None
            and existing.get("is_full_snapshot") is True
            and not _entry_refresh_due(
                existing,
                frequency,
                market_is_trading=trading_by_market.get(market, True),
                now=time.time(),
            )
        ):
            local_hits += 1
            verified_scopes.add((market, code, frequency))
            continue
        local_attempts += 1
        try:
            downloaded_this_job = False
            if needs_download:
                download_attempts += 1
                downloaded_this_job = True
                computed = compute_and_cache_chart_data(
                    market,
                    code,
                    frequency,
                    config,
                    incremental_refresh_days=_INCREMENTAL_REFRESH_DAYS,
                )
            else:
                computed = compute_and_cache_chart_data(
                    market,
                    code,
                    frequency,
                    config,
                    skip_download=True,
                )
            warmed = _get_chart_cache_entry_ram_only(cache_key)
            is_ready = (
                warmed is not None
                and warmed.get("is_full_snapshot") is True
                and not _entry_refresh_due(
                    warmed,
                    frequency,
                    market_is_trading=trading_by_market.get(market, True),
                    now=time.time(),
                )
            )
            # A cache miss may have been built from an old local store.  Do not
            # stamp it ready: one bounded incremental download gets the tail,
            # while the existing full history and incremental CL runtime stay
            # reusable.
            if (
                not is_ready
                and not needs_download
                and _entry_bar_lagging(
                    warmed,
                    frequency,
                    market_is_trading=trading_by_market.get(market, True),
                    now=time.time(),
                )
                and _download_refresh_allowed(
                    market,
                    code,
                    frequency,
                    target_ranks,
                )
            ):
                if not _wait_for_interactive_idle():
                    interrupted = True
                    break
                download_attempts += 1
                downloaded_this_job = True
                computed = compute_and_cache_chart_data(
                    market,
                    code,
                    frequency,
                    config,
                    incremental_refresh_days=_INCREMENTAL_REFRESH_DAYS,
                )
                warmed = _get_chart_cache_entry_ram_only(cache_key)
                is_ready = (
                    warmed is not None
                    and warmed.get("is_full_snapshot") is True
                    and not _entry_refresh_due(
                        warmed,
                        frequency,
                        market_is_trading=trading_by_market.get(market, True),
                        now=time.time(),
                    )
                )
            if is_ready:
                local_hits += 1
                verified_scopes.add((market, code, frequency))
                if downloaded_this_job:
                    download_hits += 1
            elif computed:
                # The non-blocking per-key lock deliberately yields when the
                # user's request is already computing this same chart.
                local_yields += 1
            else:
                local_misses += 1
        except Exception as exc:
            errors += 1
            LogUtil.warning(
                f"[candidate_chart_warm] local build failed "
                f"market={market} code={code} frequency={frequency}: {exc}"
            )
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    completed = (
        not interrupted
        and errors == 0
        and local_yields == 0
        and local_misses == 0
    )
    with _state_lock:
        # A yielded/failed local build must remain eligible for the next
        # screening poll. Otherwise one transient native lock or empty local
        # read would permanently suppress warming until the ranked list changes.
        _last_completed = targets if completed else None
        _last_completed_at = time.time() if completed else 0.0
        if completed:
            _next_retry_at = 0.0
        elif not interrupted:
            _next_retry_at = time.time() + _INCOMPLETE_RETRY_SECONDS
        _local_candidate_scopes = frozenset(verified_scopes)
        _local_candidate_updated_at = time.time() if verified_scopes else 0.0
        _metrics["runs"] += 1
        _metrics["disk_hits"] += hits
        _metrics["disk_misses"] += misses
        _metrics["refreshes"] += refreshes
        _metrics["local_attempts"] += local_attempts
        _metrics["local_hits"] += local_hits
        _metrics["local_yields"] += local_yields
        _metrics["local_misses"] += local_misses
        _metrics["download_attempts"] += download_attempts
        _metrics["download_hits"] += download_hits
        _metrics["errors"] += errors
        _metrics["last_elapsed_ms"] = elapsed_ms
    LogUtil.info(
        "[candidate_chart_warm] "
        f"targets={len(targets)} entries={len(jobs)} "
        f"hits={hits} misses={misses} refreshes={refreshes} errors={errors} "
        f"local_attempts={local_attempts} local_hits={local_hits} "
        f"local_yields={local_yields} local_misses={local_misses} "
        f"download_attempts={download_attempts} download_hits={download_hits} "
        f"interrupted={interrupted} elapsed={elapsed_ms}ms "
        f"cache={chart_cache_metrics()}"
    )


def _worker_loop() -> None:
    global _pending, _worker_running
    while True:
        with _state_lock:
            targets = _pending
            _pending = None
            if not targets:
                _worker_running = False
                return
            if (
                not _background_work_allowed
                or time.time() < _next_retry_at
            ):
                _worker_running = False
                return
            # A request can enqueue the same ranked list while the current pass
            # is still running. Once that pass completed successfully, do not
            # spend another full background cycle reading/building it again.
            if (
                targets == _last_completed
                and time.time() - _last_completed_at < _REFRESH_CHECK_INTERVAL_SECONDS
            ):
                _worker_running = False
                return
        _warm_targets(targets)


def schedule_candidate_chart_cache_warm(
    snapshot: Mapping[str, object] | object,
    *,
    preferred_view: Mapping[str, object] | object | None = None,
) -> bool:
    global _pending, _worker_running
    global _local_candidate_scopes, _local_candidate_updated_at
    global _background_work_allowed, _realtime_capacity_ready_since
    targets = candidate_chart_targets(snapshot, preferred_view=preferred_view)
    if not targets:
        with _state_lock:
            _local_candidate_scopes = frozenset()
            _local_candidate_updated_at = 0.0
        return False
    capacity = _realtime_capacity_observation(snapshot)
    now = time.time()
    with _state_lock:
        if capacity is False:
            # Drop queued opportunistic work.  A running pass observes this
            # flag between disk batches and symbol builds, then exits without
            # turning one transient chart-lock yield into a tight retry loop.
            _background_work_allowed = False
            _realtime_capacity_ready_since = 0.0
            _pending = None
            return False
        if capacity is True:
            if _realtime_capacity_ready_since <= 0:
                _realtime_capacity_ready_since = now
            stable_for = now - _realtime_capacity_ready_since
            if stable_for < _REALTIME_CAPACITY_STABILITY_SECONDS:
                _background_work_allowed = False
                _pending = None
                return False
        else:
            _realtime_capacity_ready_since = 0.0
        _background_work_allowed = True
        if now < _next_retry_at:
            return False
        active_targets = frozenset(
            target for target in targets if target[0] == "a"
        )
        # Keep only previously proven scopes while the new pass is pending.
        # Merely appearing in a selection snapshot is not local-data evidence.
        _local_candidate_scopes = frozenset(
            scope
            for scope in _local_candidate_scopes
            if (scope[0], scope[1]) in active_targets
        )
        if targets == _pending:
            return False
        if (
            targets == _last_completed
            and time.time() - _last_completed_at < _REFRESH_CHECK_INTERVAL_SECONDS
        ):
            return False
        _pending = targets
        if _worker_running:
            return True
        _worker_running = True
    timer = threading.Timer(
        _START_DELAY_SECONDS,
        function=_worker_loop,
    )
    timer.name = "CandidateChartCacheWarm"
    timer.daemon = True
    timer.start()
    return True
