"""Evidence-based linkage of the user's 1m/5m/30m operating framework.

Local author sources in D:/缠论/chanlun_lesson_corpus:
L014_*:115-124,169-175 (lower-level timing and risk); L043_*:82-94,142-148
(a smaller reversal does not confirm a larger one); L061_*:97-121
(compare the actual legs and refine a revisable hypothesis).
The physical-period mapping and ranking below are engineering policy, not a
claim that three candle intervals constitute a proven recursive interval nest.
"""

from __future__ import annotations

import math
import numpy as np

from chanlun.screening.confirmation import (
    confirmation_catalog,
    native_selection_lifetime_reasons,
)
from chanlun.screening.rules import (
    _native_center_body_source_valid,
    _native_center_signal_source_valid,
    _price_ticks,
)
from chanlun.screening.timeframe_policy import background

PERIODS = ("1m", "5m", "30m")
SCHEMA = "native-period-link-v1"
POINT_NAMES = {
    "1buy": "一买",
    "2buy": "二买",
    "3buy": "三买",
    "1sell": "一卖",
    "2sell": "二卖",
    "3sell": "三卖",
}
LABELS = {
    "aligned": "联动支持",
    "turning": "转折候选",
    "countertrend": "逆向风险",
    "mixed": "信号分歧",
    "independent": "本周期信号",
    "unverified": "联动待核验",
}
PRIORITY = {
    "aligned": 0,
    "turning": 1,
    "independent": 2,
    "mixed": 3,
    "countertrend": 4,
    "unverified": 5,
}


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def summarize(snapshot, latest_close, *, input_fingerprint, frame):
    """Publish only small, causal facts from an already calculated native chart."""
    cutoff = snapshot["source_closed_at"]
    if (
        snapshot.get("analysis_scope") != "native_centers"
        or not number(latest_close)
        or snapshot.get("segment_construction", {}).get("status") == "unresolved"
    ):
        raise ValueError("native period context is not available")
    units = snapshot.get("screening_segments", [])
    by_id = {unit["unit_id"]: unit for unit in units}
    positions = {u["unit_id"]: i for i, u in enumerate(units)}
    level = snapshot["levels"][0]
    centers = {c["center_id"]: c for c in level.get("centers", [])}
    valid_centers = [
        c
        for c in centers.values()
        if _native_center_body_source_valid(snapshot, c, by_id, positions, cutoff)
    ]
    valid_centers.sort(
        key=lambda c: (c.get("frame_available_at", 0), c["points"][0]["time"])
    )
    center = valid_centers[-1] if valid_centers else None
    quantum = float(snapshot["structure_price_quantum"])
    dates = np.asarray([int(value.timestamp()) for value in frame.date], dtype=np.int64)
    lows = _price_ticks(frame.low.to_numpy(dtype=float), quantum)
    highs = _price_ticks(frame.high.to_numpy(dtype=float), quantum)
    if not len(dates) or dates[-1] != cutoff or np.any(dates > cutoff):
        raise ValueError("period context and source bars disagree")
    suffix_low = np.minimum.accumulate(lows[::-1])[::-1]
    suffix_high = np.maximum.accumulate(highs[::-1])[::-1]
    direction = "range" if center else "unknown"
    direction_basis = "center_oscillation" if center else "structure_missing"
    if len(valid_centers) >= 2:
        previous = valid_centers[-2]
        if center["envelope"]["dd_tick"] > previous["envelope"]["gg_tick"]:
            direction = "up"
            direction_basis = "separated_centers"
        elif center["envelope"]["gg_tick"] < previous["envelope"]["dd_tick"]:
            direction = "down"
            direction_basis = "separated_centers"
    position = "unknown"
    if center:
        zd, zg = (
            center["core"]["zd_tick"] * quantum,
            center["core"]["zg_tick"] * quantum,
        )
        position = (
            "above" if latest_close > zg else "below" if latest_close < zd else "inside"
        )
        if direction == "range" and center.get("center_ended"):
            if center.get("completion_point_type") == "3buy" and position == "above":
                direction = "up"
                direction_basis = "completed_departure"
            if center.get("completion_point_type") == "3sell" and position == "below":
                direction = "down"
                direction_basis = "completed_departure"
    points = {
        p["point_id"]: p
        for p in level.get("points", [])
        if p.get("point_type") in POINT_NAMES
    }
    valid = {
        key: _native_center_signal_source_valid(snapshot, p, centers, units)
        for key, p in points.items()
    }
    following = confirmation_catalog(snapshot)
    reverse = [
        p
        for key, p in points.items()
        if valid[key]
        and p.get("status") == "confirmed"
        and p.get("available_at", cutoff + 1) <= cutoff
    ]
    signals = []
    for key, p in points.items():
        if not valid[key] or p.get("available_at", cutoff + 1) > cutoff:
            continue
        if p.get("status") == "confirmed" and native_selection_lifetime_reasons(
            p, points.values(), following, valid
        ):
            continue
        if p.get("status") not in {"confirmed", "approaching"}:
            continue
        anchor_position = int(np.searchsorted(dates, p["anchor_at"]))
        if (
            anchor_position >= len(dates)
            or dates[anchor_position] != p["anchor_at"]
            or not lows[anchor_position] <= p["anchor_tick"] <= highs[anchor_position]
        ):
            continue
        side = "buy" if p["point_type"].endswith("buy") else "sell"
        stop = p.get("invalidation_price", p.get("invalidation_tick", 0) * quantum)
        subsequent = anchor_position + 1
        if subsequent < len(dates) and (
            suffix_low[subsequent] < p["invalidation_tick"]
            if side == "buy"
            else suffix_high[subsequent] > p["invalidation_tick"]
        ):
            continue
        if any(
            other["side"] != side
            and other["anchor_at"] > p["anchor_at"]
            and other["available_at"] > p["available_at"]
            for other in reverse
        ):
            continue
        if (
            number(stop)
            and stop > 0
            and (latest_close < stop if side == "buy" else latest_close > stop)
        ):
            continue
        unit = by_id.get(p.get("anchor_unit_id"), {})
        signals.append(
            {
                "point_id": key,
                "point_type": p["point_type"],
                "side": side,
                "status": p["status"],
                "available_at": p["available_at"],
                "anchor_at": p["anchor_at"],
                "anchor_price": p.get(
                    "anchor_price", p.get("anchor_tick", 0) * quantum
                ),
                "invalidation_price": stop,
                "leg_start": p.get("terminal_start_at", unit.get("start_time")),
            }
        )
    signals.sort(
        key=lambda p: (p["available_at"], p["anchor_at"], p["point_id"]), reverse=True
    )
    tail = units[-1] if units else {}
    return {
        "schema": SCHEMA,
        "frequency": snapshot["source_frequency"],
        "source_closed_at": cutoff,
        "input_fingerprint": input_fingerprint,
        "price_basis_revision": snapshot["price_basis_revision"],
        "latest_close": latest_close,
        "price_quantum": quantum,
        "direction": direction,
        "position": position,
        "direction_basis": direction_basis,
        "center": None
        if center is None
        else {
            "id": center["center_id"],
            "zd": zd,
            "zg": zg,
            "ended": bool(center.get("center_ended")),
        },
        "leg_start": tail.get("start_time"),
        "leg_direction": tail.get("direction"),
        "signals": signals,
        # Preserve the same center-based background used by the scanner. A
        # completed departure in the richer linkage summary is a different fact.
        "background_30m": background(
            snapshot, side="buy", expected_cutoff=cutoff,
            as_of=cutoff, price_basis=snapshot["price_basis_revision"],
        ) if snapshot["source_frequency"] == "30m" else None,
    }


def relate(row, summaries, expected):
    """Use background and timing separately; never promote a small reversal."""
    base = row.get("point", row)
    frequency = row["frequency"]
    as_of = row["source_closed_at"]
    side = "buy" if base["point_type"].endswith("buy") else "sell"
    intent = "up" if side == "buy" else "down"
    basis = base.get("price_basis_revision") or row.get("price_basis_revision")
    main = summaries.get(frequency)
    main_point = next(
        (
            p
            for p in (main or {}).get("signals", [])
            if p["point_id"] == base.get("point_id")
        ),
        None,
    )
    anchor = base.get("anchor_at")
    start = base.get("terminal_start_at") or (main_point or {}).get("leg_start")
    current_price = (main or {}).get("latest_close")
    missing = []
    periods = []
    higher = []
    lower = []
    background_30m = {"frequency": "30m", "state": "unavailable",
                      "direction": "unknown", "alignment": "unverified"}
    for peer in PERIODS:
        summary = summaries.get(peer)
        role = (
            "main"
            if peer == frequency
            else "background"
            if PERIODS.index(peer) > PERIODS.index(frequency)
            else "timing"
        )
        reason = (
            "period_evidence_missing"
            if not summary
            else "period_cutoff_mismatch"
            if summary.get("source_closed_at") != expected.get(peer)
            or summary.get("source_closed_at", as_of + 1) > as_of
            else "price_basis_mismatch"
            if basis and summary.get("price_basis_revision") != basis
            else "period_context_mismatch"
        )
        valid = (
            summary
            and summary.get("schema") == SCHEMA
            and summary.get("source_closed_at") == expected.get(peer)
            and summary["source_closed_at"] <= as_of
            and (not basis or summary.get("price_basis_revision") == basis)
        )
        if peer == frequency and valid:
            fingerprint = row.get("input_fingerprint") or (
                row.get("evidence") or {}
            ).get("input_fingerprint")
            status = base.get("status") or row.get("point_status")
            valid = (
                bool(main_point)
                and main_point["point_type"] == base["point_type"]
                and (not status or main_point["status"] == status)
                and (not number(anchor) or main_point["anchor_at"] == anchor)
                and (not fingerprint or fingerprint == summary.get("input_fingerprint"))
            )
            if not valid:
                reason = "main_point_or_input_mismatch"
        if (
            valid
            and main
            and summary["source_closed_at"] == main.get("source_closed_at")
        ):
            tolerance = (
                max(summary.get("price_quantum", 0), main.get("price_quantum", 0)) / 2
            )
            valid = (
                number(current_price)
                and number(summary.get("latest_close"))
                and abs(current_price - summary["latest_close"]) <= tolerance + 1e-10
            )
            if not valid:
                reason = "period_close_mismatch"
        if not valid:
            labels = {
                "period_evidence_missing": "周期证据待读取",
                "period_cutoff_mismatch": "周期截止不一致",
                "price_basis_mismatch": "价格基准不一致",
                "main_point_or_input_mismatch": "主结构与计算输入不一致",
                "period_close_mismatch": "同一截止的收盘价格不一致",
            }
            missing.append(peer)
            periods.append(
                {
                    "frequency": peer,
                    "role": role,
                    "state": "pending",
                    "reason": reason,
                    "label": labels.get(reason, "周期证据不一致"),
                }
            )
            continue
        if peer == "30m" and summary.get("background_30m"):
            background_30m = dict(summary["background_30m"])
            direction = background_30m.get("direction", "unknown")
            background_30m["alignment"] = (
                "unverified" if direction == "unknown" else "range" if direction == "range"
                else "aligned" if direction == intent else "countertrend"
            )
        candidates = summary.get("signals", [])
        broken = False
        if role == "timing":
            candidates = [
                p
                for p in candidates
                if number(start) and start <= p["anchor_at"] <= as_of
            ]
        elif role == "background":
            # An unfinished higher-period leg extends beyond its last closed
            # candle; this is an operating context, not a proven nested type.
            candidates = [
                p
                for p in candidates
                if number(anchor)
                and number(p.get("leg_start"))
                and p["leg_start"] <= anchor
            ]
            if number(current_price):
                filtered = [
                    p
                    for p in candidates
                    if not number(p.get("invalidation_price"))
                    or (
                        current_price >= p["invalidation_price"]
                        if p["side"] == "buy"
                        else current_price <= p["invalidation_price"]
                    )
                ]
                broken = len(filtered) < len(candidates)
                candidates = filtered
        recent = candidates[0] if candidates else None
        insufficient = summary["direction"] == "unknown" and not recent
        if insufficient:
            missing.append(peer)
        direction_label = {
            "up": "中枢上移",
            "down": "中枢下移",
            "range": "中枢震荡",
            "unknown": "结构未齐",
        }[summary["direction"]]
        if summary.get("direction_basis") == "completed_departure":
            direction_label = (
                "向上离开中枢" if summary["direction"] == "up" else "向下离开中枢"
            )
        note = (
            (
                POINT_NAMES[recent["point_type"]]
                + ("预警" if recent["status"] == "approaching" else "确认")
            )
            if recent
            else direction_label
        )
        position = summary["position"]
        if summary.get("center") and number(current_price):
            position = (
                "above"
                if current_price > summary["center"]["zg"]
                else "below"
                if current_price < summary["center"]["zd"]
                else "inside"
            )
        if broken:
            note += " · 参考边界已被当前价格突破"
        anchor_aligned = False
        if role == "timing" and recent and recent["point_type"] == "1" + side:
            small_price = recent.get("anchor_price")
            price = base.get("anchor_price")
            tolerance = max(
                summary.get("price_quantum", 0), (main or {}).get("price_quantum", 0)
            )
            anchor_aligned = bool(
                number(anchor)
                and number(start)
                and number(price)
                and number(small_price)
                and max(start, anchor - int(frequency[:-1]) * 60)
                <= recent["anchor_at"]
                <= anchor
                and abs(price - small_price) <= tolerance + 1e-10
            )
        item = {
            "frequency": peer,
            "role": role,
            "state": "ready",
            "source_closed_at": summary["source_closed_at"],
            "direction": summary["direction"],
            "position": position,
            "label": note,
            "boundary_broken": broken,
            "position_checked_at": as_of,
            "anchor_aligned": anchor_aligned,
            "signal": dict(recent) if recent else None,
            "center": dict(summary["center"]) if summary.get("center") else None,
        }
        if insufficient:
            item.update(
                reason="structure_insufficient", label="已核验，尚无充分中枢或信号依据"
            )
        periods.append(item)
        if role == "background":
            higher.append(item)
        elif role == "timing":
            lower.append(item)
    category = "independent"
    top = higher[-1] if higher else None
    higher_risk = bool(top and top["direction"] not in {intent, "range", "unknown"})
    top_turn = bool(top and top.get("signal") and top["signal"]["side"] == side)
    turning = any(
        p.get("signal") and p["signal"]["side"] == side and p["direction"] != intent
        for p in higher
    )
    turning = turning and (not higher_risk or top_turn)
    crossing = bool(
        top
        and higher_risk
        and top["position"] == ("above" if side == "buy" else "below")
    )
    turning = turning or crossing
    broken_support = bool(
        top
        and (
            top.get("boundary_broken")
            or top["direction"] == intent
            and top["position"] == ("below" if side == "buy" else "above")
        )
    )
    opposite_signal = any(
        p.get("signal") and p["signal"]["side"] != side for p in [*higher, *lower]
    )
    opposite = opposite_signal or any(
        p["direction"] not in {intent, "range", "unknown"} for p in higher
    )
    supported = any(
        p["direction"] == intent or p.get("signal") and p["signal"]["side"] == side
        for p in higher
    )
    timed = any(
        p.get("signal")
        and p["signal"]["side"] == side
        and (not base["point_type"].startswith("2") or p["anchor_aligned"])
        for p in lower
    )
    if missing:
        category = "unverified"
    elif broken_support:
        category = "mixed"
    elif turning:
        category = "turning"
    elif higher_risk:
        category = "countertrend"
    elif opposite:
        category = "mixed"
    elif supported or timed:
        category = "aligned"
    notes = []
    if broken_support:
        notes.append("当前价格已突破较大周期的参考边界，原支持条件需重新核验。")
    elif crossing:
        notes.append("当前价格已越过较大周期中枢参考边界，转折仍待该周期结构确认。")
    elif turning:
        notes.append("较大周期出现同向转折信号，仍需跟踪后续结构。")
    if higher_risk:
        notes.append("较大周期方向仍相反，当前信号可能只对应较小范围的反弹或回撤。")
    if opposite_signal:
        notes.append("存在反向信号，应区分操作周期与短期波动。")
    if missing:
        notes.append("待核验周期：" + "、".join(missing))
    if (
        lower
        and base["point_type"].startswith("2")
        and not any(p["anchor_aligned"] for p in lower)
    ):
        notes.append("二类点的低周期一类拐点尚未对齐，当前仅作背景辅助。")
    return {
        "schema": SCHEMA,
        "primary_frequency": frequency,
        "as_of": as_of,
        "category": category,
        "label": LABELS[category],
        "priority": PRIORITY[category],
        "quality": "pending" if missing else "complete",
        "periods": periods,
        "notes": notes,
        "timing_anchor_verified": any(p["anchor_aligned"] for p in lower),
        "higher_reversal_confirmed": False,
        "background_30m": background_30m,
    }
