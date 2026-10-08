"""Causal, current-signal eligibility. Historical chart markers stay immutable."""

from __future__ import annotations

from bisect import bisect_left
from datetime import datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache
import math
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from chanlun.exchange.trading_session import official_trading_session_evidence
from chanlun.core.strict_structure.unit_adapter import _tick
from chanlun.core.strict_structure.serialized_basis import first_class_comparison_basis_valid
from chanlun.core.strict_structure.native_center_signals import NATIVE_SIGNAL_RULE_BASIS
from chanlun.core.strict_structure.center_geometry import return_outside_core
from chanlun.core.strict_structure.native_consolidation import NATIVE_CONSOLIDATION_RULE_BASIS
from chanlun.screening.sessions import exempt_closes
from chanlun.screening.confirmation import confirmation_catalog, confirmation_reasons, is_confirmed, native_selection_lifetime_reasons
from chanlun.screening.nesting import is_nested_observation

CN = ZoneInfo("Asia/Shanghai")
FREQUENCIES = ("1m", "5m", "30m")
BUY_TYPES = ("1buy", "2buy", "3buy")
SELL_TYPES = ("1sell", "2sell", "3sell")
POINT_TYPES = BUY_TYPES + SELL_TYPES
FORMING_STATUSES = ("approaching", "formed", "observed")


def is_forming_observation(item):
    """Only an otherwise eligible, explicitly unfinished point is observable.

    Rejections caused by broken anchors, invalidation, age, or data failures
    must never reappear just because a point also lacks confirmation.
    """
    if "nested_confirmation" in item:
        return is_nested_observation(item)
    point = item.get("point", {})
    return (set(item.get("reasons", [])) == {"NOT_CONFIRMED"}
            and point.get("status") in FORMING_STATUSES
            and point.get("confirmed_at") is None
            and bool(point.get("missing_conditions"))
             and point.get("point_type") in POINT_TYPES
             and point.get("point_type", "").endswith(point.get("side", "?"))
            and point.get("structural_level") == 0 and point.get("source_kind") == "segment")


REASON_LABELS = {
    'STRUCTURAL_STOP_TRIGGERED':'已收盘行情触发结构止损参考',
    'ANCHOR_DISTANCE_EXCEEDED':'距拐点超过筛选上限',
    'SELECTION_WINDOW_ENDED':'退出当前筛选窗口',
    "FIRST_ADVANCED_TO_SECOND": "同一来源已推进到已确认二类点，一类点不再作为当前候选",
    "CONSOLIDATION_EVIDENCE_MISSING": "盘整背驰的来源或冻结证据校验失败",
    "NOT_CONFIRMED": "买卖点尚未确认",
    "CONFIRMING_SEGMENT_COMPLETED": "买卖点之后紧接的反向走势已完成，不再作为当前候选",
    "CONFIRMING_SEGMENT_MISSING": "缺少可核对的后继反向走势证据，需按当前规则重新计算",
    "CONFIRMING_SEGMENT_UNRESOLVED": "后继反向走势尚未判定，不能用尾部投影充当完成证据",
    "VARIANT_EXCLUDED": "该二买变体不在本次所选范围内",
    "FIRST_CLASS_NOT_TREND": "盘整背驰或无本级趋势背驰证据，不能作为一类点",
    "TREND_PROOF_MISSING": "缺少同级趋势或末中枢三类点证据，需按当前规则重算",
    "SECOND_PARENT_MISSING": "二类点缺少可核对的一类点来源，需重新计算",
    "SECOND_PARENT_INVALID": "二类点的一类点来源、级别或首次回试证据不成立",
    "THIRD_ENTRY_PRECISION_PENDING": "高一级三买结构已确认，次次级背驰或两次回拉的入场证仍待核验",
    "THIRD_ENTRY_PRECISION_EVIDENCE_MISMATCH": "高一级三买的筛选记录与冻结的入场证据不一致",
    "OLD_CONFIRMATION": "确认时间超出筛选窗口",
    "OLD_ANCHOR": "价格拐点距今过久",
    "PRICE_TOO_FAR": "最新价格距买卖点的顺向涨跌幅超过所设上限",
    "ANCHOR_BROKEN": "买点后创新低或卖点后创新高，原点不再作为当前候选",
    "INVALIDATED": "买卖点之后已越过失效边界（三类点仅触及 ZG/ZD 不算越界）",
    "LATER_SELL": "之后已出现同级别确认卖点",
    "LATER_BUY": "之后已出现同级别确认买点",
    "STRUCTURE_MISMATCH": "买点与中枢证据不一致",
    "ANCHOR_PRICE_MISMATCH": "买点价格不在标注时刻的 K 线内",
    "STALE_DATA": "行情未覆盖要求的最新已收盘 K 线",
    "INVALID_BARS": "行情存在重复、乱序或无效价格",
    "INSUFFICIENT_HISTORY": "历史 K 线不足 200 根",
    "DATA_GAPS": "行情存在未核实的缺口，需补齐数据或核实停牌",
    "LISTING_METADATA_CONFLICT": "上市日期与已有成交记录冲突，需核对行情元数据",
    "DEPENDENCY_MISSING": "缺少买点形成所需的完整来源区间",
    "CALENDAR_COVERAGE_UNKNOWN": "已验证交易日历未覆盖行情来源区间",
    "INVALID_SESSION": "K 线时间不在对应周期的交易时段内",
    "INVALID_SESSION_EVIDENCE": "停复牌依据无效或与实际成交冲突，无法判断",
    "NO_VOLUME": "最新交易日无成交数据",
    "CONFIRMATION_REPLAY_FAILED": "在标注的确认时刻无法重现买点",
    "CONFIRMATION_TIME_MISMATCH": "买点在所标确认时刻之前已经确认，时间证据不一致",
    "REBUILD_MISMATCH": "独立重算与筛选证据不一致",
    "ENGINE_ERROR": "结构计算失败",
    "BAR_TIME_LABEL_UNAVAILABLE": "行情源缺少已核实的分钟K线时间口径",
    "SESSION_CALENDAR_UNAVAILABLE": "缺少已核实的标的交易时段",
    "EVIDENCE_WRITE_FAILED": "复核证据保存失败，候选未发布",
    "EVIDENCE_MISSING": "复核证据文件不完整，候选不可用",
    "WORKER_TIMEOUT": "个股计算超时，未发布该标的结果",
    "LOWER_CONFIRMATION_PENDING": "5m 已有候选，等待对应末端区间内的 1m 确认",
    "LOWER_DATA_ERROR": "1m 行情未通过校验，不能判断区间内结构确认",
    "NESTED_INTERVAL_INVALID": "5m 与 1m 的区间、截止时间或价格基准无法对应",
    "LOWER_REPLAY_FAILED": "1m 确认信号未通过独立重算或确认时刻回放",
    "NESTED_CONFIRMATION_ERROR": "区间内结构确认计算或证据保存失败",
    "NESTED_EVIDENCE_MISSING": "缺少一致、完整的 5m 与 1m 组合确认依据",
}


def point_semantic_reasons(point, points, centers):
    """Check signal meaning independently of age/risk and old result labels.

    Consolidation divergence is not a first-class parent. It may still be the
    retest evidence of a weak second point with a valid trend-divergence parent.
    Cross-level parents require the engine's explicit small-to-large lineage.
    Confirmed third points retain the same center and complete leave/return
    proof when a saved screening result is read again.
    """
    kind = point.get("point_type")
    if kind in {"1buy", "1sell"}:
        divergence = point.get("divergence") or {}
        if divergence.get("kind") != "trend":
            return ["FIRST_CLASS_NOT_TREND"]
        center = centers.get(point.get("center_id"), {})
        if ("two_separated_centers" not in point.get("evidence_codes", ())
                or not first_class_comparison_basis_valid(point, center)
                or not divergence.get("metrics", {}).get("is_divergent")
                or divergence.get("structural_level") != point.get("structural_level")
                or divergence.get("price_basis_revision") != point.get("price_basis_revision")
                or divergence.get("direction") != ("down" if kind == "1buy" else "up")
                or not center.get("third_class_confirmed")
                or center.get("completion_direction") != divergence.get("direction")
                or not center.get("completion_return_unit_id")
                or center["completion_return_unit_id"] not in divergence.get("signal_leg_unit_ids", ())):
            return ["TREND_PROOF_MISSING"]
    elif kind in {"2buy", "2sell"}:
        parent = points.get(point.get("parent_point_id"))
        if parent is None:
            return ["SECOND_PARENT_MISSING"]
        expected = "1buy" if kind == "2buy" else "1sell"
        if parent.get("point_type") != expected:
            return ["SECOND_PARENT_INVALID"]
        parent_reasons = point_semantic_reasons(parent, points, centers)
        same_level = parent.get("structural_level") == point.get("structural_level")
        small_to_large = (type(parent.get("structural_level")) is int
                          and type(point.get("structural_level")) is int
                          and parent["structural_level"] < point["structural_level"]
                          and "small_to_large_reversal" in point.get("evidence_codes", ())
                          and len(set(point.get("small_to_large_carrier_unit_ids", ()))) == 3)
        parent_confirmed = parent.get("status") == "confirmed" and parent.get("confirmed_at") is not None
        parent_waiting = (point.get("status") in FORMING_STATUSES
                          and parent.get("status") in FORMING_STATUSES
                          and "parent_first_class_confirmed" in point.get("missing_conditions", ()))
        if (parent_reasons or not (same_level or small_to_large)
                or not (parent_confirmed or parent_waiting)
                or parent.get("price_basis_revision") != point.get("price_basis_revision")
                or not all(type(p.get(k)) is int for p in (parent, point)
                           for k in ("anchor_at", "available_at", "anchor_tick"))
                or parent["available_at"] > point["available_at"]
                or parent["anchor_at"] > point["anchor_at"]):
            return ["SECOND_PARENT_INVALID"]
        holds = (point["anchor_tick"] >= parent["anchor_tick"] if kind == "2buy"
                 else point["anchor_tick"] <= parent["anchor_tick"])
        divergence = point.get("divergence") or {}
        if ((point.get("variant") == "strict" and not holds)
                or (point.get("variant") == "weak_divergence" and
                    (holds or divergence.get("kind") != "consolidation"
                      or not divergence.get("metrics", {}).get("is_divergent")))
                or (point.get("variant") == "lower_retest" and
                    (holds or point.get("divergence") is not None
                     or point.get("invalidation_tick") != point.get("anchor_tick")))
                or point.get("variant") not in {"strict", "weak_divergence", "lower_retest"}):
            return ["SECOND_PARENT_INVALID"]
    elif kind in {"3buy", "3sell"} and is_confirmed(point):
        center = centers.get(point.get("center_id"))
        buy = kind == "3buy"
        grade = point.get("structural_level")
        if (type(grade) is not int or grade < 0
                or point.get("source_kind") != ("segment" if grade == 0 else "trend_type")
                or not isinstance(center, dict)
                or center.get("structural_level") != point.get("structural_level")
                or center.get("source_kind") != point.get("source_kind")
                or center.get("third_class_confirmed") is not True
                or point.get("side") != ("buy" if buy else "sell")
                or not isinstance(center.get("core"), dict)):
            return ["STRUCTURE_MISMATCH"]
        edge = center["core"].get("zg_tick" if buy else "zd_tick")
        anchor = point.get("anchor_tick")
        if (type(edge) is not int or type(anchor) is not int
                or (anchor < edge if buy else anchor > edge)
                or point.get("invalidation_tick") != edge
                or not _valid_center_roles(center, point)):
            return ["STRUCTURE_MISMATCH"]
    return []


def third_buy_entry_precision_reasons(point, precision_catalog, cutoff):
    """Keep L025:3157's entry timing separate from L020's formal 3-buy.

    The selected base level has no modeled lower-lower grade here, so this
    bounded entry check applies only where an exact recursive child source
    exists. It never renames or removes a formal chart point.
    """

    if point.get("point_type") != "3buy" or point.get("structural_level", 0) == 0:
        return []
    state = precision_catalog.get(point.get("point_id"))
    if state is None:
        return ["THIRD_ENTRY_PRECISION_PENDING"]
    if (
        state.get("point_id") != point.get("point_id")
        or state.get("point_type") != "3buy"
        or state.get("parent_center_id") != point.get("center_id")
        or state.get("formal_third_point_available_at") != point.get("available_at")
        or state.get("point_status") != point.get("status")
        or state.get("tradable") is not False
        or state.get("formal_third_point_independent") is not True
    ):
        return ["STRUCTURE_MISMATCH"]
    if state.get("lower_return_trend_id") is None:
        return ["THIRD_ENTRY_PRECISION_PENDING"]
    if state["lower_return_trend_id"] != point.get("anchor_unit_id"):
        return ["STRUCTURE_MISMATCH"]
    parent_center_known_at = state.get("parent_center_initial_known_at")
    precision_known_at = state.get("precision_known_at")
    if type(parent_center_known_at) is not int:
        return ["THIRD_ENTRY_PRECISION_PENDING"]
    point_available_at = point.get("available_at")
    if type(point_available_at) is not int:
        return ["STRUCTURE_MISMATCH"]
    if (parent_center_known_at > point_available_at
            or (type(precision_known_at) is int
                and parent_center_known_at > precision_known_at)):
        return ["STRUCTURE_MISMATCH"]
    if (
        state.get("status") != "bounded_precision_condition_observed"
        or state.get("author_entry_condition_supported") is not True
        or state.get("precision_branch") not in {
            "double_subsub_return", "lower_trend_divergence"
        }
        or type(precision_known_at) is not int
        or precision_known_at > cutoff
    ):
        return ["THIRD_ENTRY_PRECISION_PENDING"]
    return []


def _price_ticks(prices, quantum):
    """Match the chart's HALF_UP ticks, including fractional adjusted quotes."""
    values = np.asarray(prices, dtype=float)
    scaled = values / quantum
    ticks = np.floor(scaled + 0.5).astype(np.int64)
    # Float division can land on either side of a half tick. Only ambiguous
    # ties need Decimal; ordinary quotes stay in the vectorized fast path.
    ties = np.abs(scaled - np.floor(scaled) - 0.5) <= np.spacing(scaled) * 4
    basis = Decimal(str(quantum))
    for i in np.flatnonzero(ties):
        ticks[i] = _tick(values[i], basis)
    return ticks


def trading_context(observed_at: datetime, frequency: str, recent_sessions=5,
                    max_anchor_sessions=20) -> dict:
    """Use the verified exchange calendar; never treat weekend age as trading age."""
    if frequency not in FREQUENCIES:
        raise ValueError("unsupported screening frequency")
    if observed_at.tzinfo is None:
        raise ValueError("screening observation time must be timezone-aware")
    now = observed_at.astimezone(CN)
    evidence = official_trading_session_evidence(session=now.date(), observed_at=now)
    if evidence is None:
        raise ValueError("当前日期超出已验证交易日历范围")
    days = list(evidence["calendar_document"]["trading_days"])
    coverage_start = evidence["calendar_document"]["coverage_start"]
    sources = [evidence["source_document"]["source_url"]]
    year = now.year - 1
    while year >= 2000:
        previous = official_trading_session_evidence(session=datetime(year, 12, 31).date(), observed_at=now)
        if previous is None:
            break
        days = previous["calendar_document"]["trading_days"] + days
        coverage_start = previous["calendar_document"]["coverage_start"]
        sources.insert(0, previous["source_document"]["source_url"])
        year -= 1
    usable = [d for d in days if d <= now.date().isoformat()]
    step = int(frequency[:-1])
    closes = []
    for day in usable[-max(25, max_anchor_sessions + 2):]:
        for h, m in ((9, 30), (13, 0)):
            start = datetime.combine(datetime.fromisoformat(day).date(), time(h, m), CN)
            closes.extend(int(t.timestamp()) for offset in range(step, 121, step)
                          if (t := start + timedelta(minutes=offset)) <= now)
    if not closes:
        raise ValueError("交易日历内没有已收盘 K 线")
    cutoff = closes[-1]
    last_day = datetime.fromtimestamp(cutoff, CN).date().isoformat()
    usable = [d for d in usable if d <= last_day]
    if len(usable) < max(recent_sessions, max_anchor_sessions):
        raise ValueError("已验证日历不足以覆盖所选回看窗口")
    return {
        "frequency": frequency, "cutoff": cutoff,
        "recent_from": usable[-recent_sessions],
        "anchor_from": usable[-max_anchor_sessions],
        "history_from": usable[-min(len(usable), max(25, max_anchor_sessions + 2))],
        "trading_days": usable, "expected_closes": closes,
        "calendar_source": evidence["source_document"]["source_url"],
        "calendar_sources": sources, "calendar_coverage_start": coverage_start,
    }


@lru_cache(maxsize=16)
def _calendar_closes(days: tuple[str, ...], step: int, cutoff: int) -> tuple[int, ...]:
    offsets = tuple((base + offset) * 60 for base in (570, 780) for offset in range(step, 121, step))
    return tuple(stamp for day in days
                 for midnight in (int(datetime.fromisoformat(day).replace(tzinfo=CN).timestamp()),)
                 for offset in offsets if (stamp := midnight + offset) <= cutoff)


def expected_closes_between(context: dict, start: int) -> set[int]:
    """Generate verified closes for the actual dependency, even before the UI window."""
    if "session_periods" in context:
        if start < context["session_periods"][0][0]:
            raise ValueError("calendar does not cover dependency start")
        step = int(context["frequency"][:-1]) * 60
        return {stamp for opening, closing in context["session_periods"]
                for stamp in range(opening + step, closing + 1, step) if stamp >= start}
    first_day = datetime.fromtimestamp(start, CN).date().isoformat()
    if first_day < context.get("calendar_coverage_start", context["trading_days"][0]):
        raise ValueError("calendar does not cover dependency start")
    closes = _calendar_closes(tuple(context["trading_days"]), int(context["frequency"][:-1]), context["cutoff"])
    return set(closes[bisect_left(closes, start):])


def frame_gaps(frame: pd.DataFrame, context: dict, start: int, *, raw=False) -> list[int]:
    present = set() if frame is None or frame.empty else set(
        frame.date.astype(pd.DatetimeTZDtype(unit="ns", tz=CN)).astype("int64") // 1_000_000_000)
    expected = expected_closes_between(context, start)
    exempt = set() if raw or frame is None or frame.empty or "session_periods" in context else exempt_closes(frame, context, expected)
    return sorted(expected - present - exempt)


def frame_quality(frame: pd.DataFrame, context: dict) -> dict:
    """Quality is independent of whether an algorithm managed to find a point."""
    errors = validate_frame(frame, context)
    result = {"errors": errors, "status": "unusable" if errors else "complete", "missing_bars": 0}
    if errors:
        return result
    start = int(frame.date.iloc[0].timestamp())
    result["checked_from"] = start
    result["checked_through"] = context["cutoff"]
    try:
        missing = frame_gaps(frame, context, start)
    except ValueError as exc:
        reason = "CALENDAR_COVERAGE_UNKNOWN" if "calendar" in str(exc) else "INVALID_SESSION_EVIDENCE"
        result.update(status="unknown", errors=[reason], detail=str(exc))
        return result
    if missing:
        result.update(status="unknown", errors=["DATA_GAPS"], missing_bars=len(missing), first_missing_at=missing[0])
    if frame.attrs.get("screening_suspensions"):
        result["suspension_evidence"] = frame.attrs["screening_suspensions"]
    if frame.attrs.get("_screening_session_error"):
        result["suspension_check_error"] = frame.attrs["_screening_session_error"]
    return result


def validate_frame(frame: pd.DataFrame, context: dict) -> list[str]:
    if frame is None or frame.empty:
        return ["STALE_DATA"]
    if frame.attrs.get("qmt_listing_history_conflict") is True:
        return ["LISTING_METADATA_CONFLICT"]
    required = {"date", "open", "high", "low", "close", "volume"}
    if not required.issubset(frame.columns):
        return ["INVALID_BARS"]
    if "symbol" in context and "code" in frame and set(frame.code) != {context["symbol"]}:
        return ["INVALID_BARS"]
    dates = frame.date
    if (not isinstance(dates.dtype, pd.DatetimeTZDtype) or dates.isna().any()
            or dates.duplicated().any() or not dates.is_monotonic_increasing):
        return ["INVALID_BARS"]
    values = frame[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if (not np.isfinite(values).all() or (values[:, :4] <= 0).any()
            or (values[:, 4] < 0).any()
            or (frame.high < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame.low > frame[["open", "close", "high"]].min(axis=1)).any()):
        return ["INVALID_BARS"]
    reasons = []
    # Resolve the local clock once; component access on a timezone-aware
    # series otherwise repeats the UTC-to-local conversion for every field.
    local = dates.dt.tz_convert(context.get("session_timezone", CN)).dt.tz_localize(None)
    minute = local.dt.hour * 60 + local.dt.minute
    step = int(context["frequency"][:-1])
    if "session_periods" in context:
        from chanlun.screening.markets import _session_mask

        stamps = dates.astype("int64") // 1_000_000_000
        aligned = _session_mask(stamps, context["session_periods"], step * 60)
    else:
        aligned = (((minute > 570) & (minute <= 690) & ((minute - 570) % step == 0))
                   | ((minute > 780) & (minute <= 900) & ((minute - 780) % step == 0)))
    # Match the QMT adapter: these are real auction rows, not continuous
    # minute closes. Preserve their prices without requiring or inventing them.
    if context["frequency"] == "1m" and frame.attrs.get("price_basis_provider") == "qmt":
        aligned |= minute.isin((565, 570))
    # Calendar membership is a property of each date, not each intraday bar.
    known_days = (local.dt.normalize().drop_duplicates().dt.strftime("%Y-%m-%d")
                  if "session_periods" not in context else None)
    if (not aligned.all() or (local.dt.second != 0).any()
            or (local.dt.microsecond != 0).any() or (local.dt.nanosecond != 0).any()
            or ("session_periods" not in context and ((known_days >= context.get("calendar_coverage_start", context["trading_days"][0]))
                & ~known_days.isin(context["trading_days"])).any())):
        reasons.append("INVALID_SESSION")
    if int(dates.iloc[-1].timestamp()) != context["cutoff"]:
        reasons.append("STALE_DATA")
    if len(frame) < 200:
        reasons.append("INSUFFICIENT_HISTORY")
    cutoff_day = datetime.fromtimestamp(context["cutoff"], CN).date()
    if frame.loc[dates.dt.tz_convert(CN).dt.date == cutoff_day, "volume"].sum() <= 0:
        reasons.append("NO_VOLUME")
    return reasons


def audit_snapshot(snapshot: dict, frame: pd.DataFrame, context: dict,
                   point_types=BUY_TYPES, max_anchor_gain_pct=10, *,
                   include_weak_second=False, as_confirmation=False,
                   structural_levels=(0,)) -> dict:
    """Validate native signals, or historical evidence for a containing interval.

    A lower-period confirmation is evidence, not a second independent trade:
    its age, completed following segment and later opposite points do not end
    the main-period selection window. Its own price extreme must still hold.
    """
    quality = frame_quality(frame, context)
    errors = quality["errors"]
    result = {"selected": [], "observations": [], "rejected": [], "data_errors": errors,
              "data_quality": quality, "confirmed_buys": 0, "unconfirmed_buys": 0,
              "confirmed_sells": 0, "unconfirmed_sells": 0}
    if errors:
        return result
    grades = tuple(structural_levels)
    if (not grades or any(type(grade) is not int or grade < 0 for grade in grades)
            or len(set(grades)) != len(grades)):
        raise ValueError("structural_levels must be distinct nonnegative integers")
    selected_levels = [
        level for level in snapshot["levels"]
        if level["structural_level"] in grades
    ]
    centers = {
        center["center_id"]: center
        for level in selected_levels for center in level.get("centers", [])
    }
    all_points = [
        point for level in selected_levels for point in level.get("points", [])
    ]
    point_catalog = {p["point_id"]: p for level in snapshot["levels"] for p in level.get("points", [])}
    center_catalog = {c["center_id"]: c for level in snapshot["levels"] for c in level.get("centers", [])}
    precision_catalog = {
        state["point_id"]: state
        for level in snapshot["levels"]
        for state in level.get("third_point_entry_precision_rule_states", [])
    }
    confirmations = {
        point_id: record
        for grade in grades
        for point_id, record in confirmation_catalog(snapshot, grade).items()
    }
    cutoff = context["cutoff"]
    dates = np.array([int(d.timestamp()) for d in frame.date], dtype=np.int64)
    quantum = float(snapshot["structure_price_quantum"])
    if not math.isfinite(quantum) or quantum <= 0:
        raise ValueError("invalid price quantum")
    # Quantize with the same declared price basis, avoiding float boundary drift.
    lows = _price_ticks(frame.low.to_numpy(dtype=float), quantum)
    highs = _price_ticks(frame.high.to_numpy(dtype=float), quantum)
    coverage = {}
    for point in all_points:
        if point["point_type"] not in point_types or point["side"] not in {"buy", "sell"}:
            continue
        buy = point["side"] == "buy"
        third = point["point_type"] in {"3buy", "3sell"}
        reasons = point_semantic_reasons(point, point_catalog, center_catalog)
        if (point["point_type"] in {"2buy", "2sell"} and not include_weak_second
                and point.get("variant") in {"weak_divergence", "lower_retest"}):
            reasons.append("VARIANT_EXCLUDED")
        confirmed = is_confirmed(point)
        if confirmed:
            reasons.extend(third_buy_entry_precision_reasons(point, precision_catalog, cutoff))
        if not as_confirmation:
            reasons.extend(confirmation_reasons(point, confirmations))
        result[("confirmed_" if confirmed else "unconfirmed_") + ("buys" if buy else "sells")] += 1
        anchor = point["anchor_at"]
        available = point["available_at"]
        from zoneinfo import ZoneInfo
        session_tz = ZoneInfo(context["session_timezone"]) if "session_timezone" in context else CN
        anchor_day = datetime.fromtimestamp(anchor, session_tz).date().isoformat()
        confirm_day = datetime.fromtimestamp(available, session_tz).date().isoformat()
        if not confirmed:
            reasons.append("NOT_CONFIRMED")
        if not as_confirmation and confirm_day < context["recent_from"]:
            reasons.append("OLD_CONFIRMATION")
        if not as_confirmation and anchor_day < context["anchor_from"]:
            reasons.append("OLD_ANCHOR")
        gain = (float(frame.close.iloc[-1]) / (point["anchor_tick"] * quantum) - 1) * 100
        gain *= 1 if buy else -1
        if not as_confirmation and gain > max_anchor_gain_pct + 1e-9:
            reasons.append("PRICE_TOO_FAR")
        center = centers.get(point.get("center_id"))
        if (snapshot["source_closed_at"] != cutoff
                or snapshot["source_frequency"] != context["frequency"]
                or ("code" in frame and set(frame.code) != {snapshot["symbol"]})
                or point["source_kind"] != (
                    "segment" if point["structural_level"] == 0 else "trend_type"
                )
                or (center is not None and point["structural_level"] > 0 and (
                    center.get("structural_level") != point["structural_level"]
                    or center.get("source_kind") != point["source_kind"]
                ))
                or not point["point_type"].endswith(point["side"])
                or point["price_basis_revision"] != snapshot["price_basis_revision"]
                or anchor > available or available > cutoff
                or (confirmed and not anchor <= point["confirmed_at"] <= available)):
            reasons.append("STRUCTURE_MISMATCH")
        # Confirmed third-class geometry is checked in point_semantic_reasons
        # for both the live scan and frozen-result reader.
        if third and (point["anchor_tick"] < point["invalidation_tick"] if buy
                      else point["anchor_tick"] > point["invalidation_tick"]):
            # The source permits contact with the departure-side boundary;
            # crossing that boundary invalidates the third-class geometry.
            reasons.append("STRUCTURE_MISMATCH")
        # The anchor candle itself contains the low that defines a 1/2 buy. Start
        # strictly after it; include the entire subsequent history, not only now.
        after = (lows if buy else highs)[dates > anchor]
        boundary = point["invalidation_tick"]
        crossed = after < boundary if buy else after > boundary
        invalid = bool(crossed.any())
        if invalid:
            reasons.append("INVALIDATED")
        elif bool((after < point["anchor_tick"] if buy else after > point["anchor_tick"]).any()):
            # A historical third buy can remain outside its center while a later
            # pullback breaks the original buy low. That does not erase the
            # chart marker, but it is a different entry opportunity and must
            # acquire its own evidence; recovery cannot reactivate the old one.
            reasons.append("ANCHOR_BROKEN")
        if not as_confirmation and any(p["side"] == ("sell" if buy else "buy") and is_confirmed(p)
               and p["structural_level"] == point["structural_level"]
               and p["anchor_at"] > anchor and p["available_at"] <= cutoff
               and not point_semantic_reasons(p, point_catalog, center_catalog)
               for p in all_points):
            reasons.append("LATER_SELL" if buy else "LATER_BUY")
        # Include entry, core, departure, divergence legs and parent evidence.
        # A complete tail cannot validate a signal built over a historical hole.
        dependency_from = point.get("dependency_from")
        if dependency_from is None and third and center:
            roles = center.get("establishment_segments", [])
            if len(roles) in (3, 5):
                dependency_from = min(r["start_time"] for r in roles)
        if not reasons or set(reasons) == {"NOT_CONFIRMED"}:
            if type(dependency_from) is not int or dependency_from > anchor:
                reasons.append("DEPENDENCY_MISSING")
            else:
                if dependency_from not in coverage:
                    try:
                        missing = frame_gaps(frame, context, dependency_from)
                        coverage[dependency_from] = (missing, None)
                    except ValueError:
                        coverage[dependency_from] = ([], "CALENDAR_COVERAGE_UNKNOWN")
                missing, coverage_error = coverage[dependency_from]
                if coverage_error:
                    reasons.append(coverage_error)
                elif missing:
                    reasons.append("DATA_GAPS")
            anchor_index = int(np.searchsorted(dates, anchor))
            # Compare on chart ticks here too. A float half-tick tolerance can
            # reject valid adjusted quotes (5.145 + .005 is below float 5.15).
            if (anchor_index == len(dates) or dates[anchor_index] != anchor
                    or not lows[anchor_index] <= point["anchor_tick"] <= _tick(
                        frame.high.iloc[anchor_index], Decimal(str(quantum)))):
                reasons.append("ANCHOR_PRICE_MISMATCH")
        item = {
            "point": point, "center": center, "reasons": list(dict.fromkeys(reasons)),
            "anchor_age_sessions": len(context["trading_days"]) - 1
                - bisect_left(context["trading_days"], anchor_day),
            "confirmation_age_sessions": len(context["trading_days"]) - 1
                - bisect_left(context["trading_days"], confirm_day),
            "confirmation_delay_sessions": bisect_left(context["trading_days"], confirm_day)
                - bisect_left(context["trading_days"], anchor_day),
            "latest_price": float(frame.close.iloc[-1]),
            "distance_from_anchor_pct": round(gain, 2),
            "dependency_from": dependency_from,
        }
        if third and buy and point["structural_level"] > 0:
            item["entry_precision_evidence"] = precision_catalog.get(point["point_id"])
        if confirmed:
            item["confirmation_segment"] = confirmations.get(point["point_id"], {"state": "unavailable"})
        if "DATA_GAPS" in reasons:
            item["data_gap_count"] = len(missing)
            item["first_missing_at"] = missing[0]
        result["rejected" if reasons else "selected"].append(item)
        if is_forming_observation(item):
            result["observations"].append({**item, "observation_validation": "checked"})
    return result


def _native_center_body_source_valid(snapshot, center, unit_by_id, unit_position, known_at):
    """Check the whole frozen center, including its immutable three-unit core."""
    try:
        core_ids = center["core_unit_ids"]
        body_ids = center["body_unit_ids"]
        if (len(core_ids) != 3 or len(set(core_ids)) != 3 or body_ids[:3] != core_ids
                or len(set(body_ids)) != len(body_ids)):
            return False
        external_ids = {center.get(key) for key in (
            "entry_unit_id", "completion_leave_unit_id", "completion_return_unit_id"
        ) if center.get(key) is not None}
        if (set(body_ids) & external_ids
                or any(unit_position[left] >= unit_position[right]
                       for left, right in zip(body_ids, body_ids[1:]))):
            return False
        if center.get("formation_rule") == "three_core":
            role_ids = core_ids
        elif center.get("formation_rule") == "five_role":
            role_ids = [center["entry_unit_id"], *core_ids, center["establishment_leave_unit_id"]]
        else:
            return False
        roles = center["establishment_segments"]
        if [role["unit_id"] for role in roles] != role_ids:
            return False
        identities = set((*role_ids, *body_ids, *center.get("failed_departure_unit_ids", ()),
                          *center.get("supersession_bridge_unit_ids", ())))
        identities.update(center.get(field) for field in (
            "completion_leave_unit_id", "completion_return_unit_id", "pending_leave_unit_id"
        ) if center.get(field) is not None)
        positions = sorted(unit_position[identity] for identity in identities)
        chain = list(unit_by_id.values())[positions[0]:positions[-1] + 1]
        if any(
            not _causally_locked_role(unit, known_at)
            or unit.get("source_kind") != "segment" or unit.get("structural_level") != 0
            or unit.get("price_basis_revision") != snapshot.get("price_basis_revision")
            for unit in chain
        ):
            return False
        if any(left["end_time"] != right["start_time"]
               or left["end_tick"] != right["start_tick"]
               or left["direction"] == right["direction"]
               for left, right in zip(chain, chain[1:])):
            return False
        if any(unit_position[right] != unit_position[left] + 1
               for left, right in zip(role_ids, role_ids[1:])):
            return False
        audit_fields = ("direction", "start_time", "end_time", "start_tick", "end_tick",
                        "low_tick", "high_tick", "locked", "forming", "confirmed_at", "available_at")
        if any(any(role.get(key) != unit_by_id[role["unit_id"]].get(key) for key in audit_fields)
               for role in roles):
            return False
        core = [unit_by_id[identity] for identity in core_ids]
        body = [unit_by_id[identity] for identity in body_ids]
        envelope_units = [*body, *(unit_by_id[identity] for identity in center.get("failed_departure_unit_ids", ()))]
        return bool(
            (center["core"]["zd_tick"], center["core"]["zg_tick"])
            == (max(unit["low_tick"] for unit in core), min(unit["high_tick"] for unit in core))
            and center["core"]["zd_tick"] <= center["core"]["zg_tick"]
            and all(max(unit["low_tick"], center["core"]["zd_tick"])
                    <= min(unit["high_tick"], center["core"]["zg_tick"]) for unit in body)
            and (center["envelope"]["dd_tick"], center["envelope"]["gg_tick"])
            == (min(unit["low_tick"] for unit in envelope_units), max(unit["high_tick"] for unit in envelope_units))
            and center.get("established_at") == roles[-1]["confirmed_at"]
        )
    except (AttributeError, IndexError, KeyError, TypeError, ValueError):
        return False


def _native_center_signal_source_valid(snapshot, point, centers, units) -> bool:
    """Check the saved source graph before accepting a current signal."""

    if (point.get("structural_level") != 0
            or point.get("price_basis_revision") != snapshot.get("price_basis_revision")
            or point.get("point_type") not in POINT_TYPES
            or point.get("side") != ("buy" if point["point_type"].endswith("buy") else "sell")):
        return False
    clocks = tuple(point.get(key) for key in ("anchor_at", "confirmed_at", "available_at"))
    cutoff = snapshot.get("source_closed_at")
    if (not all(type(clock) is int for clock in (*clocks, cutoff))
            or not clocks[0] <= clocks[1] <= clocks[2] <= cutoff):
        return False
    center_list = list(centers.values())
    owner_index = next((index for index, center in enumerate(center_list)
                        if center["center_id"] == point.get("center_id")), None)
    if owner_index is None:
        return False
    owner = center_list[owner_index]
    if (owner.get("source_kind") != "segment" or owner.get("structural_level") != 0
            or owner.get("price_basis_revision") != snapshot.get("price_basis_revision")):
        return False
    unit_by_id = {item["unit_id"]: item for item in units}
    unit_position = {item["unit_id"]: index for index, item in enumerate(units)}
    if len(unit_by_id) != len(units):
        return False
    anchor_source = unit_by_id.get(point.get("price_anchor_unit_id"))
    if (anchor_source is None or type(point.get("anchor_at")) is not int
            or not anchor_source["start_time"] <= point["anchor_at"] <= anchor_source["end_time"]
            or not _native_center_body_source_valid(
                snapshot, owner, unit_by_id, unit_position, point.get("confirmed_at"))):
        return False

    def locked_chain(identifiers):
        positions = [unit_position.get(identity) for identity in identifiers]
        return bool(positions) and all(index is not None for index in positions) and all(
            right == left + 1 for left, right in zip(positions, positions[1:])
        ) and all(
            _causally_locked_role(unit_by_id[identity], point.get("confirmed_at"))
            and unit_by_id[identity].get("source_kind") == "segment"
            and unit_by_id[identity].get("structural_level") == 0
            and unit_by_id[identity].get("price_basis_revision") == snapshot.get("price_basis_revision")
            for identity in identifiers
        ) and all(
            unit_by_id[left]["end_time"] == unit_by_id[right]["start_time"]
            and unit_by_id[left]["end_tick"] == unit_by_id[right]["start_tick"]
            for left, right in zip(identifiers, identifiers[1:])
        )

    kind = point["point_type"]
    if kind.startswith("1"):
        if owner_index < 1 or point.get("parent_point_id") is not None:
            return False
        previous = center_list[owner_index - 1]
        if not _native_center_body_source_valid(
            snapshot, previous, unit_by_id, unit_position, point.get("confirmed_at")
        ):
            return False
        earlier_core, current_core = previous.get("core") or {}, owner.get("core") or {}
        earlier_env, current_env = previous.get("envelope") or {}, owner.get("envelope") or {}
        try:
            separated = (
                current_core["zg_tick"] < earlier_core["zd_tick"]
                and current_env["gg_tick"] < earlier_env["dd_tick"]
                if point["side"] == "buy" else
                current_core["zd_tick"] > earlier_core["zg_tick"]
                and current_env["dd_tick"] > earlier_env["gg_tick"]
            )
        except (KeyError, TypeError):
            return False
        divergence = point.get("divergence") or {}
        comparison = divergence.get("compare_leg_unit_ids") or []
        departure = divergence.get("signal_leg_unit_ids") or []
        anchor_source = unit_by_id.get(point.get("price_anchor_unit_id"))
        return bool(
            separated
            and owner.get("third_class_confirmed") is True
            and type(owner.get("center_end_available_at")) is int
            and owner["center_end_available_at"] <= point.get("confirmed_at", -1)
            and owner.get("completion_direction") == ("down" if point["side"] == "buy" else "up")
            and divergence.get("kind") == "native_two_center_divergence"
            and divergence.get("first_center_id") == previous["center_id"]
            and divergence.get("last_center_id") == owner["center_id"]
            and divergence.get("histogram_area_decayed") is True
            and len(comparison) in (1, 3) and len(departure) == 3
            and locked_chain(comparison) and locked_chain(departure)
            and point.get("related_unit_ids") == [*comparison, *departure]
            and divergence.get("compare_unit_id") == comparison[-1]
            and divergence.get("signal_unit_id") == departure[-1]
            and comparison[-1] == owner.get("entry_unit_id")
            and departure[0] == owner.get("completion_leave_unit_id")
            and departure[1] == owner.get("completion_return_unit_id")
            and departure[-1] == point.get("anchor_unit_id")
            and point.get("price_anchor_unit_id") in departure
            and anchor_source is not None
            and point.get("anchor_tick") == (
                min(unit_by_id[identity]["low_tick"] for identity in departure)
                if point["side"] == "buy" else
                max(unit_by_id[identity]["high_tick"] for identity in departure)
            )
            and point.get("invalidation_tick") == point.get("anchor_tick")
            and point.get("anchor_tick") == (
                anchor_source["low_tick"] if point["side"] == "buy"
                else anchor_source["high_tick"]
            )
        )
    if kind.startswith("2"):
        catalog = {item["point_id"]: item for item in snapshot["levels"][0].get("points", ())}
        parent = catalog.get(point.get("parent_point_id"))
        related = point.get("related_unit_ids") or []
        if (parent is None or parent.get("point_type") != "1" + point["side"]
                or len(related) != 3 or not locked_chain(related)
                or not _native_center_signal_source_valid(snapshot, parent, centers, units)):
            return False
        signal, rebound, retest = (unit_by_id[identity] for identity in related)
        return bool(
            parent.get("point_type") == "1" + point["side"]
            and parent.get("center_id") == point.get("center_id")
            and parent.get("available_at", 0) <= point.get("confirmed_at", -1)
            and parent.get("anchor_at", 0) < point.get("anchor_at", 0)
            and related[0] == parent.get("price_anchor_unit_id")
            and related[-1] == point.get("anchor_unit_id")
            and signal["direction"] == retest["direction"]
            and rebound["direction"] != signal["direction"]
            and signal["end_time"] == rebound["start_time"]
            and rebound["end_time"] == retest["start_time"]
            and point.get("anchor_tick") == (
                retest["low_tick"] if point["side"] == "buy" else retest["high_tick"]
            )
            and (point["anchor_tick"] >= parent["anchor_tick"] if point["side"] == "buy"
                 else point["anchor_tick"] <= parent["anchor_tick"])
            and point.get("invalidation_tick") == point.get("anchor_tick")
        )
    related = point.get("related_unit_ids") or []
    if len(related) != 2 or not locked_chain(related):
        return False
    leave, retest = (unit_by_id[identity] for identity in related)
    return bool(
        owner.get("third_class_confirmed") is True
        and type(owner.get("center_end_available_at")) is int
        and owner["center_end_available_at"] <= point.get("confirmed_at", -1)
        and owner.get("completion_leave_unit_id") == related[0]
        and owner.get("completion_return_unit_id") == related[1]
        and point.get("anchor_unit_id") == related[1]
        and point.get("price_anchor_unit_id") == related[1]
        and type(point.get("center_ordinal")) is int
        and point["center_ordinal"] >= 1
        and leave["end_time"] == retest["start_time"]
        and (leave["direction"], retest["direction"]) == (
            ("up", "down") if point["side"] == "buy" else ("down", "up")
        )
        and point.get("anchor_tick") == (
            retest["low_tick"] if point["side"] == "buy" else retest["high_tick"]
        )
        and point.get("invalidation_tick") == (
            owner["core"]["zg_tick"] if point["side"] == "buy" else owner["core"]["zd_tick"]
        )
        and return_outside_core(direction="up" if point["side"] == "buy" else "down",
                                low_tick=retest["low_tick"], high_tick=retest["high_tick"],
                                zd_tick=owner["core"]["zd_tick"], zg_tick=owner["core"]["zg_tick"])
    )


def audit_native_center_signals(snapshot: dict, frame: pd.DataFrame, context: dict,
                                point_types=POINT_TYPES, max_anchor_gain_pct=10) -> dict:
    """Select confirmed native-center-policy points without recursive types."""

    if snapshot.get("analysis_scope") != "native_centers":
        raise ValueError("native center signal audit requires a native snapshot")
    quality = frame_quality(frame, context)
    result = {
        "selected": [], "observations": [], "rejected": [],
        "data_errors": quality["errors"], "data_quality": quality,
        "confirmed_buys": 0, "unconfirmed_buys": 0,
        "confirmed_sells": 0, "unconfirmed_sells": 0,
    }
    if quality["errors"]:
        return result
    if (snapshot.get("source_frequency") != context.get("frequency")
            or snapshot.get("source_closed_at") != context.get("cutoff")):
        raise ValueError("native center signal context mismatch")
    centers = {
        item["center_id"]: item
        for item in snapshot["levels"][0].get("centers", ())
    }
    units = snapshot.get("screening_segments")
    if not isinstance(units, list):
        raise ValueError("native center signal source segments are missing")
    quantum = float(snapshot["structure_price_quantum"])
    quantum_decimal = Decimal(str(snapshot["structure_price_quantum"]))
    dates = np.array([int(value.timestamp()) for value in frame.date], dtype=np.int64)
    lows = _price_ticks(frame.low.to_numpy(dtype=float), quantum)
    highs = _price_ticks(frame.high.to_numpy(dtype=float), quantum)
    close = float(frame.close.iloc[-1])
    close_tick = _tick(close, quantum_decimal)
    session_tz = ZoneInfo(context.get("session_timezone", "Asia/Shanghai"))
    trading_days = context.get("trading_days", ())
    all_points = snapshot["levels"][0].get("points", ())
    source_checks = {}
    following = confirmation_catalog(snapshot)
    reverse_points = []
    for point in all_points:
        if (point.get("evidence_scope") != "native_center_signal"
                or point.get("rule_basis") != NATIVE_SIGNAL_RULE_BASIS
                or point.get("formal_point_issued") is not True
                or point.get("status") != "confirmed" or point.get("source_kind") != "segment"):
            continue
        source_checks[id(point)] = _native_center_signal_source_valid(snapshot, point, centers, units)
        anchor, confirmed, available = (point.get(key) for key in ("anchor_at", "confirmed_at", "available_at"))
        if (source_checks[id(point)] and all(type(clock) is int for clock in (anchor, confirmed, available))
                and anchor <= confirmed <= available <= snapshot["source_closed_at"]):
            position = int(np.searchsorted(dates, anchor))
            if (position < len(dates) and dates[position] == anchor
                    and lows[position] <= point["anchor_tick"] <= highs[position]):
                reverse_points.append(point)
    for point in all_points:
        if point.get("point_type") not in point_types:
            continue
        if (point.get("evidence_scope") != "native_center_signal"
                or point.get("rule_basis") != NATIVE_SIGNAL_RULE_BASIS
                or point.get("formal_point_issued") is not True
                or point.get("status") != "confirmed"
                or point.get("source_kind") != "segment"):
            raise ValueError("native center signal proof mismatch")
        if not source_checks.get(id(point), False):
            raise ValueError("native center signal source proof mismatch")
        available = point["available_at"]
        anchor = point["anchor_at"]
        confirmed = point.get("confirmed_at")
        if not (type(confirmed) is int and anchor <= confirmed <= available <= snapshot["source_closed_at"]):
            raise ValueError("native center signal time mismatch")
        anchor_day = datetime.fromtimestamp(anchor, session_tz).date().isoformat()
        confirm_day = datetime.fromtimestamp(available, session_tz).date().isoformat()
        if confirm_day < context["recent_from"]:
            continue
        if anchor_day < context["anchor_from"]:
            continue
        position = int(np.searchsorted(dates, anchor))
        if (position >= len(dates) or dates[position] != anchor
                or not lows[position] <= point["anchor_tick"] <= highs[position]):
            continue
        buy = point["side"] == "buy"
        subsequent = (lows if buy else highs)[dates > anchor]
        boundary = point["invalidation_tick"]
        if bool((subsequent < boundary if buy else subsequent > boundary).any()):
            result['rejected'].append({'point':point,'reasons':['STRUCTURAL_STOP_TRIGGERED']})
            continue
        distance_decimal = (Decimal(close_tick) / point["anchor_tick"] - 1) * (100 if buy else -100)
        if distance_decimal > Decimal(str(max_anchor_gain_pct)):
            result['rejected'].append({'point':point,'reasons':['ANCHOR_DISTANCE_EXCEEDED']})
            continue
        distance = float(distance_decimal)
        item = {
            "point": point,
            "center": centers.get(point.get("center_id")),
            "reasons": [],
            "latest_price": close,
            "distance_from_anchor_pct": round(distance, 2),
            "dependency_from": anchor,
            "anchor_age_sessions": len(trading_days) - 1 - bisect_left(trading_days, anchor_day)
                if trading_days else None,
            "confirmation_age_sessions": len(trading_days) - 1 - bisect_left(trading_days, confirm_day)
                if trading_days else None,
            "confirmation_delay_sessions": bisect_left(trading_days, confirm_day)
                - bisect_left(trading_days, anchor_day) if trading_days else None,
            "confirmation_delay_bars": int(np.searchsorted(dates, confirmed)) - position,
        }
        if item["center"] is None:
            raise ValueError("native center signal center is missing")
        lifetime = native_selection_lifetime_reasons(
            point, all_points, following,
            {candidate["point_id"]: source_checks.get(id(candidate), False) for candidate in all_points})
        item["reasons"] = lifetime
        item["confirmation_segment"] = following.get(point["point_id"], {"state": "unavailable"})
        if any(other["side"] != point["side"] and other["anchor_at"] > anchor
               and other["available_at"] > available for other in reverse_points):
            item["reasons"].append("LATER_SELL" if buy else "LATER_BUY")
        if item["reasons"]:
            result["rejected"].append(item)
            continue
        result["selected"].append(item)
        result["confirmed_buys" if buy else "confirmed_sells"] += 1
    return result


def native_consolidation_source_valid(snapshot, alert):
    """Validate one completed single-center comparison independently of flags."""
    if alert.get("status") == "warning":
        return _native_consolidation_warning_source_valid(snapshot, alert)
    try:
        if (alert.get("signal_type") != "consolidation_divergence"
                or alert.get("rule_basis") != NATIVE_CONSOLIDATION_RULE_BASIS
                or alert.get("status") != "confirmed" or alert.get("structural_level") != 0
                or alert.get("source_kind") != "segment" or alert.get("direction") not in {"up", "down"}
                or alert.get("price_basis_revision") != snapshot["price_basis_revision"]):
            return False
        units = snapshot["screening_segments"]
        by_id = {unit["unit_id"]: unit for unit in units}
        position = {unit["unit_id"]: index for index, unit in enumerate(units)}
        if len(by_id) != len(units):
            return False
        center = next(center for center in snapshot["levels"][0]["centers"]
                      if center["center_id"] == alert["center_id"])
        known = alert["available_at"]
        if (not all(type(alert.get(field)) is int for field in ("anchor_at", "anchor_tick", "confirmed_at", "available_at"))
                or not alert["anchor_at"] <= alert["confirmed_at"] == known <= snapshot["source_closed_at"]
                or not _native_center_body_source_valid(snapshot, center, by_id, position, snapshot["source_closed_at"])):
            return False
        a_ids, c_ids = alert["compare_leg_unit_ids"], alert["signal_leg_unit_ids"]
        if len(a_ids) not in {1, 3} or len(a_ids) != len(c_ids):
            return False
        chain = units[position[a_ids[0]]:position[c_ids[-1]] + 1]
        seed = [by_id[role["unit_id"]] for role in center["establishment_segments"]]
        if ([unit["unit_id"] for unit in chain] != alert["source_unit_ids"]
                or position[a_ids[-1]] >= position[c_ids[0]]
                or any(not _causally_locked_role(unit, known)
                       or unit["price_basis_revision"] != alert["price_basis_revision"]
                       or unit.get("source_kind") != "segment" or unit.get("structural_level") != 0
                       for unit in (*chain, *seed))
                or any(left["end_time"] != right["start_time"] or left["end_tick"] != right["start_tick"]
                       or left["direction"] == right["direction"] for left, right in zip(chain, chain[1:]))):
            return False
        for ids in (a_ids, c_ids):
            if (by_id[ids[0]]["direction"] != alert["direction"]
                    or by_id[ids[-1]]["direction"] != alert["direction"]
                    or any(position[right] != position[left] + 1 for left, right in zip(ids, ids[1:]))):
                return False
        zd, zg = center["core"]["zd_tick"], center["core"]["zg_tick"]
        if (alert["center_zd_tick"], alert["center_zg_tick"]) != (zd, zg):
            return False
        if alert["comparison_form"] == "entry_departure":
            departures = {center.get(field) for field in (
                "establishment_leave_unit_id", "pending_leave_unit_id", "completion_leave_unit_id")}
            if a_ids[-1] != center["entry_unit_id"] or c_ids[0] not in departures:
                return False
        elif alert["comparison_form"] == "center_oscillation":
            owned = set(center["body_unit_ids"]) | {role["unit_id"] for role in center["establishment_segments"]}
            owned.update(center.get("failed_departure_unit_ids", []))
            owned.add(center.get("pending_leave_unit_id"))
            if (len(chain) != 3 or len(a_ids) != 1 or not set(alert["source_unit_ids"]) <= owned
                    or not all(max(unit["low_tick"], zd) <= min(unit["high_tick"], zg) for unit in chain)):
                return False
        else:
            return False
        source = by_id[alert["price_anchor_unit_id"]]
        extreme = max(by_id[identity]["high_tick"] for identity in c_ids) if alert["direction"] == "up" else min(
            by_id[identity]["low_tick"] for identity in c_ids)
        area_a, area_c = alert["macd_area_a"], alert["macd_area_c"]
        from chanlun.core.strict_structure.identity import stable_structure_id
        expected_id = stable_structure_id(NATIVE_CONSOLIDATION_RULE_BASIS, alert["price_basis_revision"],
                                          center["center_id"], alert["comparison_form"], a_ids, c_ids)
        if "hypothesis_id" in alert:
            from chanlun.core.strict_structure.native_consolidation_preview import hypothesis_id
            start_at = by_id[c_ids[0]]["start_time"]
            if (alert.get("signal_start_at") != start_at or alert["hypothesis_id"] != hypothesis_id(
                    center["center_id"], alert["comparison_form"], a_ids, start_at,
                    alert["direction"], alert["price_basis_revision"])):
                return False
        return bool(
            alert["alert_id"] == expected_id
            and
            source["unit_id"] in c_ids and source["start_time"] <= alert["anchor_at"] <= source["end_time"]
            and alert["anchor_tick"] == extreme
            and all(type(area) in (int, float) and math.isfinite(area) for area in (area_a, area_c))
            and 0 < area_c < area_a
            and math.isclose(alert["macd_area_ratio"], area_c / area_a, rel_tol=1e-12)
            and known == max(unit["available_at"] for unit in (*chain, *seed))
        )
    except (KeyError, TypeError, ValueError, StopIteration, IndexError):
        return False


def _native_consolidation_warning_source_valid(snapshot, alert):
    from chanlun.core.strict_structure.native_consolidation_preview import PREVIEW_RULE, hypothesis_id
    try:
        units = snapshot["screening_segments"]
        by_id = {u["unit_id"]: u for u in units}
        position = {u["unit_id"]: i for i, u in enumerate(units)}
        center = next(c for c in snapshot["levels"][0]["centers"] if c["center_id"] == alert["center_id"])
        known = alert["available_at"]
        a_ids, c_ids = alert["compare_leg_unit_ids"], alert["signal_leg_unit_ids"]
        if (len(a_ids) not in (1, 3) or len(a_ids) != len(c_ids) or len(by_id) != len(units)
                or alert.get("rule_basis") != PREVIEW_RULE or alert.get("member_basis") != "native_unlocked_segment"
                or alert.get("signal_type") != "consolidation_divergence" or alert.get("status") != "warning"
                or alert.get("source_kind") != "segment" or alert.get("structural_level") != 0
                or alert.get("confirmed_at") is not None or type(known) is not int
                or known != snapshot["source_closed_at"] or alert["measurement_end_at"] != known
                or not _native_center_body_source_valid(snapshot, center, by_id, position, known)):
            return False
        tail = by_id[c_ids[-1]]
        chain = units[position[a_ids[0]]:position[c_ids[-1]] + 1]
        if (tail is not units[-1] or tail["locked"] or tail["confirmed_at"] is not None
                or tail["available_at"] > known or not tail["start_time"] < tail["end_time"] < known
                or position[a_ids[-1]] >= position[c_ids[0]]
                or [u["unit_id"] for u in chain] != alert["source_unit_ids"]
                or any(not _causally_locked_role(u, known) for u in chain[:-1])
                or any(u["price_basis_revision"] != snapshot["price_basis_revision"]
                       or u["source_kind"] != "segment" or u["structural_level"] != 0 for u in chain)
                or any(a["end_time"] != b["start_time"] or a["end_tick"] != b["start_tick"]
                       or a["direction"] == b["direction"] for a, b in zip(chain, chain[1:]))):
            return False
        for ids in (a_ids, c_ids):
            if (by_id[ids[0]]["direction"] != tail["direction"] or by_id[ids[-1]]["direction"] != tail["direction"]
                    or any(position[b] != position[a] + 1 for a, b in zip(ids, ids[1:]))):
                return False
        owned = set(center.get("frame_body_unit_ids", [])) | set(center["establishment_segment_ids"])
        form = alert["comparison_form"]
        if form == "center_oscillation":
            bridge = units[position[c_ids[0]] - 1]
            if (len(a_ids) != 1 or position[c_ids[0]] != position[a_ids[0]] + 2
                    or a_ids[0] not in owned or bridge["unit_id"] not in owned
                    or not all(max(u["low_tick"], center["core"]["zd_tick"])
                               <= min(u["high_tick"], center["core"]["zg_tick"])
                               for u in (by_id[a_ids[0]], bridge, tail))):
                return False
        elif form == "entry_departure":
            if a_ids[-1] != center["entry_unit_id"]:
                return False
            if len(c_ids) == 1:
                if units[position[c_ids[0]] - 1]["unit_id"] not in owned or not (
                        tail["end_tick"] > center["core"]["zg_tick"] if tail["direction"] == "up"
                        else tail["end_tick"] < center["core"]["zd_tick"]):
                    return False
            elif c_ids[:2] != [center["completion_leave_unit_id"], center["completion_return_unit_id"]]:
                return False
        else:
            return False
        a, c = alert["macd_area_a"], alert["macd_area_c"]
        times, momentum = alert["momentum_times"], alert["momentum_values"]
        turns, prices = alert["turn_times"], alert["turn_prices"]
        start = by_id[c_ids[0]]["start_time"]
        identity = hypothesis_id(center["center_id"], form, a_ids, start, tail["direction"], snapshot["price_basis_revision"])
        return (
            alert["alert_id"] == alert["hypothesis_id"] == identity
            and alert["price_basis_revision"] == snapshot["price_basis_revision"]
            and alert["direction"] == tail["direction"] and alert["signal_start_at"] == start
            and alert["anchor_at"] == tail["end_time"] and alert["anchor_tick"] == tail["end_tick"]
            and alert["price_anchor_unit_id"] == tail["unit_id"]
            and (alert["center_zd_tick"], alert["center_zg_tick"]) == (center["core"]["zd_tick"], center["core"]["zg_tick"])
            and all(type(v) in (int, float) and math.isfinite(v) for v in (a, c, *momentum, *prices))
            and 0 < 2 * c < a and alert["estimated_area_c"] == 2 * c and alert["estimate_factor"] == 2
            and alert["macd_area_ratio"] == c / a
            and len(times) == len(momentum) == len(turns) == len(prices) == 3
            and all(type(t) is int for t in (*times, *turns))
            and start <= times[0] < times[1] < times[2] <= known
            and momentum[0] > momentum[1] > momentum[2] >= 0
            and turns[0] < turns[1] == alert["anchor_at"] < turns[2] <= known
            and (prices[1] > max(prices[0], prices[2]) if tail["direction"] == "up"
                 else prices[1] < min(prices[0], prices[2]))
        )
    except (KeyError, TypeError, ValueError, IndexError, StopIteration):
        return False


def audit_native_consolidation_alerts(snapshot, frame, context):
    """Keep recent comparisons whose next segment has not completed."""
    from .confirmation import consolidation_lifetime_reasons
    if (snapshot.get("analysis_scope") != "native_centers"
            or snapshot.get("source_closed_at") != context["cutoff"]
            or snapshot.get("source_frequency") != context["frequency"]):
        raise ValueError("consolidation snapshot context mismatch")
    units = snapshot["screening_segments"]
    dates = {int(row.date.timestamp()): row for row in frame.itertuples()}
    timezone = ZoneInfo(context.get("session_timezone", "Asia/Shanghai"))
    selected = {}
    for alert in snapshot.get("native_consolidation_alerts", []):
        if not native_consolidation_source_valid(snapshot, alert):
            raise ValueError("consolidation source proof mismatch")
        if consolidation_lifetime_reasons(snapshot, alert):
            continue
        known_day = datetime.fromtimestamp(alert["available_at"], timezone).date().isoformat()
        anchor_day = datetime.fromtimestamp(alert["anchor_at"], timezone).date().isoformat()
        candle = dates.get(alert["anchor_at"])
        quantum = Decimal(str(snapshot["structure_price_quantum"]))
        if (known_day < context["recent_from"] or anchor_day < context["anchor_from"]
                or (alert.get("status") == "warning" and alert["signal_leg_unit_ids"][-1] != units[-1]["unit_id"])
                or candle is None or not _tick(candle.low, quantum) <= alert["anchor_tick"] <= _tick(candle.high, quantum)):
            continue
        # Preserve both verified comparison forms. The monitor prefers the
        # previously warned hypothesis before coalescing one displayed alert.
        key = alert["center_id"], alert["direction"], alert.get("status"), alert["comparison_form"]
        candidate = {"alert": alert, "reasons": []}
        previous = selected.get(key)
        rank = alert["available_at"], alert["comparison_form"] == "entry_departure", alert["alert_id"]
        if previous is None or rank > previous[0]:
            selected[key] = rank, candidate
    return [candidate for _, candidate in selected.values()]


def _valid_center_roles(center, point):
    try:
        return _valid_center_roles_checked(center, point)
    except (AttributeError, KeyError, TypeError, ValueError):
        # Saved or externally supplied snapshots may omit a role field. A
        # missing proof is a rejected candidate, never a screening crash.
        return False


def _causally_locked_role(unit, available_at):
    if not isinstance(unit, dict) or type(available_at) is not int:
        return False
    clocks = tuple(unit.get(key) for key in (
        "start_time", "end_time", "confirmed_at", "available_at"
    ))
    return (all(type(clock) is int for clock in clocks)
            and clocks[0] < clocks[1] <= clocks[2] <= clocks[3] <= available_at
            and unit.get("locked") is True and unit.get("forming") is False)


def _valid_center_roles_checked(center, point):
    if center.get("formation_rule") == "three_core":
        return _valid_three_core_roles(center, point)
    if center.get("formation_rule") == "recursive_three":
        # The same three-member geometry applies one grade up, but its members
        # must be completed lower trend types rather than physical segments.
        if (
            type(center.get("structural_level")) is not int
            or center["structural_level"] < 1
            or center.get("source_kind") != "trend_type"
            or point.get("structural_level") != center["structural_level"]
            or point.get("source_kind") != "trend_type"
        ):
            return False
        return _valid_three_core_roles(center, point)
    if center.get("formation_rule") not in (None, "five_role"):
        return False
    roles = center.get("establishment_segments", [])
    if len(roles) != 5 or len({r["unit_id"] for r in roles}) != 5:
        return False
    available_at = point.get("available_at")
    if (any(not _causally_locked_role(role, available_at) for role in roles)
            or [r["unit_id"] for r in roles[1:4]] != center["core_unit_ids"]
            or roles[0]["unit_id"] != center["entry_unit_id"]
            or roles[-1]["unit_id"] != center["establishment_leave_unit_id"]):
        return False
    if any(a["end_time"] != b["start_time"] or a["end_tick"] != b["start_tick"]
           or a["direction"] == b["direction"] for a, b in zip(roles, roles[1:])):
        return False
    zd = max(r["low_tick"] for r in roles[1:4])
    zg = min(r["high_tick"] for r in roles[1:4])
    if (zd > zg or zd != center["core"]["zd_tick"] or zg != center["core"]["zg_tick"]
            or any(max(r["low_tick"], zd) > min(r["high_tick"], zg) for r in (roles[0], roles[-1]))):
        return False
    ret = center.get("completion_return_segment")
    leave = center.get("lifecycle_leaving_segment")
    buy = point["side"] == "buy"
    return bool(_causally_locked_role(leave, available_at)
                and _causally_locked_role(ret, available_at)
                and type(point.get("confirmed_at")) is int
                and ret["confirmed_at"] <= point["confirmed_at"] <= available_at
                and leave["unit_id"] not in {r["unit_id"] for r in roles}
                and ret["unit_id"] not in {r["unit_id"] for r in roles}
                and ret["unit_id"] != leave["unit_id"]
                and leave["direction"] == ("up" if buy else "down")
                and ret["direction"] == ("down" if buy else "up")
                and roles[-1]["end_time"] == leave["start_time"]
                and leave["end_time"] == ret["start_time"]
                and leave["end_tick"] == ret["start_tick"]
                and ret["unit_id"] == point["anchor_unit_id"]
                and ret["start_time"] <= point.get("anchor_at", -1) <= ret["end_time"]
                and point.get("anchor_tick") == (ret["low_tick"] if buy else ret["high_tick"])
                and return_outside_core(direction="up" if buy else "down",
                                        low_tick=ret["low_tick"], high_tick=ret["high_tick"],
                                        zd_tick=zd, zg_tick=zg))


def _valid_three_core_roles(center, point):
    """Check source-grade formation and independent confirmed leave/return."""
    roles = center.get("establishment_segments", [])
    role_ids = [role.get("unit_id") for role in roles]
    available_at = point.get("available_at")

    def causally_locked(unit):
        return _causally_locked_role(unit, available_at)

    if (
        len(roles) != 3
        or len(set(role_ids)) != 3
        or role_ids != center.get("core_unit_ids")
        or center.get("entry_unit_id") is not None
        or center.get("establishment_leave_unit_id") is not None
        or any(not causally_locked(role) for role in roles)
    ):
        return False
    if any(
        left.get("end_time") != right.get("start_time")
        or left.get("end_tick") != right.get("start_tick")
        or left.get("direction") == right.get("direction")
        for left, right in zip(roles, roles[1:])
    ):
        return False
    zd = max(role["low_tick"] for role in roles)
    zg = min(role["high_tick"] for role in roles)
    if (
        zd > zg
        or (zd, zg) != (center["core"]["zd_tick"], center["core"]["zg_tick"])
    ):
        return False
    body_ids = center.get("body_unit_ids", [])
    leave = center.get("lifecycle_leaving_segment")
    ret = center.get("completion_return_segment")
    if (
        body_ids[:3] != role_ids
        or not isinstance(leave, dict)
        or not isinstance(ret, dict)
        or leave.get("unit_id") in body_ids
        or ret.get("unit_id") in body_ids
        or leave.get("unit_id") != center.get("completion_leave_unit_id")
        or ret.get("unit_id") != center.get("completion_return_unit_id")
        or not causally_locked(leave)
        or not causally_locked(ret)
        or type(point.get("confirmed_at")) is not int
        or ret["confirmed_at"] > point["confirmed_at"]
        or point["confirmed_at"] > available_at
        or leave.get("start_time") < roles[-1]["end_time"]
        or leave.get("end_time") != ret.get("start_time")
        or leave.get("end_tick") != ret.get("start_tick")
        or ret.get("unit_id") != point.get("anchor_unit_id")
    ):
        return False
    overlap = center.get("overlap_components", [])
    overlap_ids = [unit.get("unit_id") for unit in overlap]
    if (
        len(overlap) < 4
        or len(set(overlap_ids)) != len(overlap_ids)
        or overlap_ids[:3] != role_ids
        or overlap_ids[-1] != leave["unit_id"]
        or not set(body_ids) <= set(overlap_ids)
        or any(not causally_locked(unit) for unit in overlap)
        or any(
            left.get("end_time") != right.get("start_time")
            or left.get("end_tick") != right.get("start_tick")
            or left.get("direction") == right.get("direction")
            for left, right in zip(overlap, overlap[1:])
        )
    ):
        return False
    buy = point["side"] == "buy"
    if (
        leave.get("direction") != ("up" if buy else "down")
        or ret.get("direction") != ("down" if buy else "up")
        or (ret["low_tick"] < zg if buy else ret["high_tick"] > zd)
        or point.get("anchor_tick") != (
            ret["low_tick"] if buy else ret["high_tick"]
        )
        or not ret["start_time"] <= point.get("anchor_at", -1) <= ret["end_time"]
    ):
        return False
    return True
