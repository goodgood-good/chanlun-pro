"""Causal, non-trading U293 facts reconstructed from completed native 5m bars.

The author described two dated 5m/15m MACD cases, but did not specify which
15m line must cross zero or a numerical near-zero threshold. This module only
exposes source facts. It never certifies the author's condition or a point.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
import math
from zoneinfo import ZoneInfo


CN = ZoneInfo("Asia/Shanghai")
FIVE = timedelta(minutes=5)


def _aware(value) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def _session_closes(day) -> tuple[datetime, ...]:
    return tuple(
        datetime.combine(day, time(hour, minute), CN) + timedelta(minutes=offset)
        for hour, minute in ((9, 30), (13, 0))
        for offset in range(5, 121, 5)
    )


def _ema_macd_last(closes: tuple[float, ...]) -> tuple[float, float]:
    """Use the runtime MACD's 12/26/9, adjust=False initialization."""

    fast = slow = closes[0]
    dea = 0.0
    for close in closes:
        fast += (2.0 / 13.0) * (close - fast)
        slow += (2.0 / 27.0) * (close - slow)
        dif = fast - slow
        dea += (2.0 / 10.0) * (dif - dea)
    return dif, dea


def observe_u293_macd_cross_frequency(
    bar_labels,
    bar_known_times,
    closes,
    five_histogram,
    *,
    physical_frequency: str,
    market: str,
    bar_time_label: str,
    as_of: datetime,
    closed_through: datetime | None,
    price_basis_revision: str,
) -> dict:
    """Return provenance and bounded facts, never a formal U293 trigger.

    A closed 15m price bar is reconstructed only when its three source 5m
    closes are present and known. An incomplete current 15m bucket is not used
    in the MACD EMA. The provenance remains ``derived_from_5m`` because no
    independent 15m feed has been checked. Red histogram groups count only
    after a nonpositive source bar has closed their positive run.
    """

    def result(status: str, **facts) -> dict:
        return {
            "rule_id": "U293",
            "rule_source_refs": ["L103:496-502", "L104:214-220"],
            "physical_frequency": physical_frequency,
            "higher_frequency": "15m",
            "market": market,
            "price_basis_revision": price_basis_revision,
            "source_policy": "complete_15m_buckets_derived_from_native_5m",
            "source_continuity_scope": "within_each_observed_a_share_session",
            "macd_initialization_scope": "first_complete_fifteen_bucket_in_visible_window",
            "status": status,
            "author_zero_axis_line_policy": "unspecified",
            "author_condition_certified": False,
            "formal_divergence_independent": True,
            "formal_point_independent": True,
            "tradable": False,
            **facts,
        }

    if physical_frequency != "5m" or market != "a":
        return result("not_applicable_source_period_or_market")
    if (bar_time_label not in {"start", "end"} or not _aware(as_of)
            or not _aware(closed_through) or closed_through > as_of
            or not price_basis_revision):
        return result("pending_causal_source_clock")
    labels = tuple(bar_labels)
    known = tuple(bar_known_times)
    close_prices = tuple(closes)
    hist = tuple(five_histogram)
    if (not labels or len({len(labels), len(known), len(close_prices), len(hist)}) != 1
            or any(not _aware(value) for value in labels)
            or any(left >= right for left, right in zip(labels, labels[1:]))):
        return result("pending_aligned_five_minute_source")
    accepted = []
    unfinished_tail = False
    for label, learned, close, histogram in zip(labels, known, close_prices, hist):
        if learned is not None and not _aware(learned):
            return result("pending_aligned_five_minute_source")
        if label > closed_through or learned is None or learned > as_of:
            unfinished_tail = True
            continue
        if unfinished_tail:
            return result("pending_aligned_five_minute_source")
        market_close = label + FIVE if bar_time_label == "start" else label
        try:
            close, histogram = float(close), float(histogram)
        except (TypeError, ValueError):
            return result("pending_finite_five_minute_values")
        if (not math.isfinite(close) or not math.isfinite(histogram)
                or learned < market_close or market_close > as_of):
            return result("pending_finite_five_minute_values")
        accepted.append((market_close.astimezone(CN), learned, close, histogram))
    if not accepted:
        return result("pending_closed_five_minute_bars")
    by_day: dict[object, list[datetime]] = {}
    for market_close, _, _, _ in accepted:
        by_day.setdefault(market_close.date(), []).append(market_close)
    for day, actual in by_day.items():
        schedule = _session_closes(day)
        indices = [schedule.index(value) if value in schedule else -1 for value in actual]
        if (-1 in indices or indices != list(range(indices[0], indices[-1] + 1))):
            return result("pending_five_minute_session_gap")

    red_groups = []
    current = None
    for market_close, learned, _, value in accepted:
        if value > 0:
            if current is None:
                current = {"area": 0.0, "peak": value, "start_at": market_close}
            current["area"] += value
            current["peak"] = max(current["peak"], value)
            current["last_positive_at"] = market_close
        elif current is not None:
            current["closed_known_at"] = learned
            red_groups.append(current)
            current = None

    buckets: dict[int, list[tuple[datetime, datetime, float]]] = {}
    for market_close, learned, close, _ in accepted:
        stamp = int(market_close.timestamp())
        bucket_end = ((stamp - 1) // 900 + 1) * 900
        buckets.setdefault(bucket_end, []).append((market_close, learned, close))
    ordered = sorted(buckets.items())
    full = []
    for position, (bucket_end, members) in enumerate(ordered):
        expected = [bucket_end - 600, bucket_end - 300, bucket_end]
        observed = [int(item[0].timestamp()) for item in members]
        if observed == expected:
            full.append((bucket_end, max(item[1] for item in members), members[-1][2]))
        elif position not in (0, len(ordered) - 1):
            return result("pending_incomplete_interior_fifteen_minute_bucket")
    if not full:
        return result("pending_complete_fifteen_minute_bucket",
                      five_minute_bar_count=len(accepted))
    dif, dea = _ema_macd_last(tuple(item[2] for item in full))
    last_fifteen = full[-1]
    axis = (
        "both_below_zero" if dif < 0 and dea < 0 else
        "both_above_zero" if dif > 0 and dea > 0 else
        "mixed_or_on_zero"
    )
    latest = red_groups[-1] if len(red_groups) >= 1 else None
    previous = red_groups[-2] if len(red_groups) >= 2 else None
    comparable = current is None and latest is not None and previous is not None
    status = (
        "current_red_group_unfinished" if current is not None else
        "pending_two_completed_red_groups" if not comparable else
        "cross_frequency_facts_observed_author_condition_unresolved"
    )
    return result(
        status,
        five_minute_bar_count=len(accepted),
        fifteen_minute_complete_bar_count=len(full),
        last_five_minute_known_at=int(accepted[-1][1].timestamp()),
        last_fifteen_minute_market_close_at=last_fifteen[0],
        last_fifteen_minute_known_at=int(last_fifteen[1].timestamp()),
        fifteen_minute_dif=dif, fifteen_minute_dea=dea,
        fifteen_minute_axis_relation=axis,
        five_minute_completed_red_group_count=len(red_groups),
        five_minute_last_red_group_area=None if latest is None else latest["area"],
        five_minute_previous_red_group_area=None if previous is None else previous["area"],
        five_minute_last_red_group_peak=None if latest is None else latest["peak"],
        five_minute_last_red_group_closed_known_at=(
            None if latest is None else int(latest["closed_known_at"].timestamp())
        ),
        five_minute_red_group_area_decayed=(
            latest["area"] < previous["area"] if comparable else None
        ),
        five_minute_red_group_peak_decayed=(
            latest["peak"] < previous["peak"] if comparable else None
        ),
        joined_facts_known_at=(
            max(int(latest["closed_known_at"].timestamp()),
                int(last_fifteen[1].timestamp())) if comparable else None
        ),
    )
