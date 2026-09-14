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
from chanlun.screening.sessions import exempt_closes
from chanlun.screening.confirmation import confirmation_catalog, confirmation_reasons, is_confirmed
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
    "NOT_CONFIRMED": "买卖点尚未确认",
    "CONFIRMING_SEGMENT_COMPLETED": "买卖点已确认且确认该点的反向线段已完成，不再作为当前候选",
    "CONFIRMING_SEGMENT_MISSING": "缺少可核对的确认线段证据，需按当前规则重新计算",
    "VARIANT_EXCLUDED": "该二买变体不在本次所选范围内",
    "FIRST_CLASS_NOT_TREND": "盘整背驰或无本级趋势背驰证据，不能作为一类点",
    "TREND_PROOF_MISSING": "缺少同级趋势或末中枢三类点证据，需按当前规则重算",
    "SECOND_PARENT_MISSING": "二类点缺少可核对的一类点来源，需重新计算",
    "SECOND_PARENT_INVALID": "二类点的一类点来源、级别或首次回试证据不成立",
    "OLD_CONFIRMATION": "确认时间超出筛选窗口",
    "OLD_ANCHOR": "价格拐点距今过久",
    "PRICE_TOO_FAR": "最新价格距买卖点的顺向涨跌幅超过所设上限",
    "ANCHOR_BROKEN": "买点后创新低或卖点后创新高，原点不再作为当前候选",
    "INVALIDATED": "买卖点之后已越过失效边界（三类点触边也失效）",
    "LATER_SELL": "之后已出现同级别确认卖点",
    "LATER_BUY": "之后已出现同级别确认买点",
    "STRUCTURE_MISMATCH": "买点与中枢证据不一致",
    "ANCHOR_PRICE_MISMATCH": "买点价格不在标注时刻的 K 线内",
    "STALE_DATA": "行情未覆盖要求的最新已收盘 K 线",
    "INVALID_BARS": "行情存在重复、乱序或无效价格",
    "INSUFFICIENT_HISTORY": "历史 K 线不足 200 根",
    "DATA_GAPS": "行情存在未核实的缺口，需补齐数据或核实停牌",
    "DEPENDENCY_MISSING": "缺少买点形成所需的完整来源区间",
    "CALENDAR_COVERAGE_UNKNOWN": "已验证交易日历未覆盖行情来源区间",
    "INVALID_SESSION": "K 线时间不在对应周期的交易时段内",
    "INVALID_SESSION_EVIDENCE": "停复牌依据无效或与实际成交冲突，无法判断",
    "NO_VOLUME": "最新交易日无成交数据",
    "CONFIRMATION_REPLAY_FAILED": "在标注的确认时刻无法重现买点",
    "CONFIRMATION_TIME_MISMATCH": "买点在所标确认时刻之前已经确认，时间证据不一致",
    "REBUILD_MISMATCH": "独立重算与筛选证据不一致",
    "ENGINE_ERROR": "结构计算失败",
    "EVIDENCE_WRITE_FAILED": "复核证据保存失败，候选未发布",
    "EVIDENCE_MISSING": "复核证据文件不完整，候选不可用",
    "WORKER_TIMEOUT": "个股计算超时，未发布该标的结果",
    "LOWER_CONFIRMATION_PENDING": "5m 已有候选，等待对应末端区间内的 1m 确认",
    "LOWER_DATA_ERROR": "1m 行情未通过校验，不能判断区间套确认",
    "NESTED_INTERVAL_INVALID": "5m 与 1m 的区间、截止时间或价格基准无法对应",
    "LOWER_REPLAY_FAILED": "1m 确认信号未通过独立重算或确认时刻回放",
    "NESTED_CONFIRMATION_ERROR": "区间套确认计算或证据保存失败",
    "NESTED_EVIDENCE_MISSING": "缺少一致、完整的 5m 与 1m 区间套证据",
}


def point_semantic_reasons(point, points, centers):
    """Check signal meaning independently of age/risk and old result labels.

    Consolidation divergence is not a first-class parent. It may still be the
    retest evidence of a weak second point with a valid trend-divergence parent.
    Cross-level parents require the engine's explicit small-to-large lineage.
    """
    kind = point.get("point_type")
    if kind in {"1buy", "1sell"}:
        divergence = point.get("divergence") or {}
        if divergence.get("kind") != "trend":
            return ["FIRST_CLASS_NOT_TREND"]
        center = centers.get(point.get("center_id"), {})
        if ("two_separated_centers" not in point.get("evidence_codes", ())
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
                    (divergence.get("kind") != "consolidation"
                     or not divergence.get("metrics", {}).get("is_divergent")))
                or point.get("variant") not in {"strict", "weak_divergence"}):
            return ["SECOND_PARENT_INVALID"]
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
    first_day = datetime.fromtimestamp(start, CN).date().isoformat()
    if first_day < context.get("calendar_coverage_start", context["trading_days"][0]):
        raise ValueError("calendar does not cover dependency start")
    closes = _calendar_closes(tuple(context["trading_days"]), int(context["frequency"][:-1]), context["cutoff"])
    return set(closes[bisect_left(closes, start):])


def frame_gaps(frame: pd.DataFrame, context: dict, start: int, *, raw=False) -> list[int]:
    present = set() if frame is None or frame.empty else set(
        frame.date.astype(pd.DatetimeTZDtype(unit="ns", tz=CN)).astype("int64") // 1_000_000_000)
    expected = expected_closes_between(context, start)
    exempt = set() if raw or frame is None or frame.empty else exempt_closes(frame, context, expected)
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
    local = dates.dt.tz_convert(CN)
    minute = local.dt.hour * 60 + local.dt.minute
    step = int(context["frequency"][:-1])
    aligned = (((minute > 570) & (minute <= 690) & ((minute - 570) % step == 0))
               | ((minute > 780) & (minute <= 900) & ((minute - 780) % step == 0)))
    # Match the QMT adapter: these are real auction rows, not continuous
    # minute closes. Preserve their prices without requiring or inventing them.
    if context["frequency"] == "1m" and frame.attrs.get("price_basis_provider") == "qmt":
        aligned |= minute.isin((565, 570))
    known_days = local.dt.strftime("%Y-%m-%d")
    if (not aligned.all() or (local.dt.second != 0).any()
            or (local.dt.microsecond != 0).any() or (local.dt.nanosecond != 0).any()
            or ((known_days >= context.get("calendar_coverage_start", context["trading_days"][0]))
                & ~known_days.isin(context["trading_days"])).any()):
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
                   include_weak_second=False, as_confirmation=False) -> dict:
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
    native = next((x for x in snapshot["levels"] if x["structural_level"] == 0), {})
    centers = {x["center_id"]: x for x in native.get("centers", [])}
    all_points = native.get("points", [])
    point_catalog = {p["point_id"]: p for level in snapshot["levels"] for p in level.get("points", [])}
    center_catalog = {c["center_id"]: c for level in snapshot["levels"] for c in level.get("centers", [])}
    confirmations = confirmation_catalog(snapshot)
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
                and point.get("variant") == "weak_divergence"):
            reasons.append("VARIANT_EXCLUDED")
        confirmed = is_confirmed(point)
        if not as_confirmation:
            reasons.extend(confirmation_reasons(point, confirmations))
        result[("confirmed_" if confirmed else "unconfirmed_") + ("buys" if buy else "sells")] += 1
        anchor = point["anchor_at"]
        available = point["available_at"]
        anchor_day = datetime.fromtimestamp(anchor, CN).date().isoformat()
        confirm_day = datetime.fromtimestamp(available, CN).date().isoformat()
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
                or point["source_kind"] != "segment"
                or not point["point_type"].endswith(point["side"])
                or point["price_basis_revision"] != snapshot["price_basis_revision"]
                or anchor > available or available > cutoff
                or (confirmed and not anchor <= point["confirmed_at"] <= available)):
            reasons.append("STRUCTURE_MISMATCH")
        if third and confirmed:
            edge = None if center is None else center["core"]["zg_tick" if buy else "zd_tick"]
            if (center is None or not center.get("third_class_confirmed")
                    or (point["anchor_tick"] <= edge if buy else point["anchor_tick"] >= edge)
                    or point["invalidation_tick"] != edge):
                reasons.append("STRUCTURE_MISMATCH")
            elif not _valid_center_roles(center, point):
                reasons.append("STRUCTURE_MISMATCH")
        if third and (point["anchor_tick"] <= point["invalidation_tick"] if buy
                      else point["anchor_tick"] >= point["invalidation_tick"]):
            # Touching the center is not a third buy, including while waiting.
            reasons.append("STRUCTURE_MISMATCH")
        # The anchor candle itself contains the low that defines a 1/2 buy. Start
        # strictly after it; include the entire subsequent history, not only now.
        after = (lows if buy else highs)[dates > anchor]
        boundary = point["invalidation_tick"]
        crossed = (after <= boundary if third else after < boundary) if buy else (
            after >= boundary if third else after > boundary)
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
               and p["anchor_at"] > anchor and p["available_at"] <= cutoff
               and not point_semantic_reasons(p, point_catalog, center_catalog)
               for p in all_points):
            reasons.append("LATER_SELL" if buy else "LATER_BUY")
        # Include entry, core, departure, divergence legs and parent evidence.
        # A complete tail cannot validate a signal built over a historical hole.
        dependency_from = point.get("dependency_from")
        if dependency_from is None and third and center:
            roles = center.get("establishment_segments", [])
            if len(roles) == 5:
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
        if confirmed:
            item["confirmation_segment"] = confirmations.get(point["point_id"], {"state": "unavailable"})
        if "DATA_GAPS" in reasons:
            item["data_gap_count"] = len(missing)
            item["first_missing_at"] = missing[0]
        result["rejected" if reasons else "selected"].append(item)
        if is_forming_observation(item):
            result["observations"].append({**item, "observation_validation": "checked"})
    return result


def _valid_center_roles(center, point):
    roles = center.get("establishment_segments", [])
    if len(roles) != 5 or len({r["unit_id"] for r in roles}) != 5:
        return False
    if (any(not r["locked"] or r["forming"] for r in roles)
            or [r["unit_id"] for r in roles[1:4]] != center["core_unit_ids"]
            or roles[0]["unit_id"] != center["entry_unit_id"]
            or roles[-1]["unit_id"] != center["establishment_leave_unit_id"]):
        return False
    if any(a["end_time"] != b["start_time"] or a["end_tick"] != b["start_tick"]
           or a["direction"] == b["direction"] for a, b in zip(roles, roles[1:])):
        return False
    zd = max(r["low_tick"] for r in roles[1:4])
    zg = min(r["high_tick"] for r in roles[1:4])
    if (zd >= zg or zd != center["core"]["zd_tick"] or zg != center["core"]["zg_tick"]
            or any(max(r["low_tick"], zd) > min(r["high_tick"], zg) for r in (roles[0], roles[-1]))):
        return False
    ret = center.get("completion_return_segment")
    leave = center.get("lifecycle_leaving_segment")
    buy = point["side"] == "buy"
    return bool(ret and leave and ret["locked"] and leave["locked"]
                and not ret["forming"] and not leave["forming"]
                and leave["direction"] == ("up" if buy else "down")
                and ret["direction"] == ("down" if buy else "up")
                and leave["end_time"] == ret["start_time"]
                and ret["unit_id"] == point["anchor_unit_id"]
                and (ret["low_tick"] > zg if buy else ret["high_tick"] < zd))
