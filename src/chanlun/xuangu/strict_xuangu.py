"""由唯一递归严格结构证据驱动的选股条件。"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.trading_system.screening_runtime import (
    screening_evidence_from_frame,
)


def _allowed_sides(opt_types: Iterable[str] | None) -> frozenset[str]:
    values = tuple(opt_types or ("long",))
    unknown = set(values) - {"long", "short"}
    if unknown:
        raise ValueError(f"unsupported stock-selection direction: {sorted(unknown)}")
    sides = set()
    if "long" in values:
        sides.add("buy")
    if "short" in values:
        sides.add("sell")
    return frozenset(sides)


def _closed_frame(code: str, mk_datas, frequency: str) -> pd.DataFrame:
    getter = getattr(mk_datas, "closed_klines", None)
    if not callable(getter):
        raise TypeError("strict stock selection requires closed_klines")
    frame = getter(code, frequency)
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("strict stock selection requires closed market bars")
    return frame


def _evidence(code: str, mk_datas, frequency: str) -> StrictEvidenceResult:
    frame = _closed_frame(code, mk_datas, frequency)
    market = getattr(mk_datas.market, "value", mk_datas.market)
    close_time = getattr(mk_datas, "closed_bar_as_of", None)
    if not callable(close_time):
        raise TypeError("strict stock selection requires closed_bar_as_of")
    return screening_evidence_from_frame(
        code=code,
        frequency=frequency,
        frame=frame,
        as_of=close_time(code, frequency),
        market=str(market),
    )


def _terminal_locked_unit_ids(
    evidence: StrictEvidenceResult,
) -> dict[int, str]:
    """分别返回每个递归级别当前最后一个已锁定单元。"""

    terminal_by_level: dict[int, str] = {}
    for level in evidence.structure.levels:
        locked = tuple(unit for unit in level.units if unit.locked)
        if locked:
            terminal_by_level[level.structural_level] = locked[-1].unit_id
    return terminal_by_level


def _current_points(
    evidence: StrictEvidenceResult,
    *,
    sides: frozenset[str],
    classes: frozenset[str] = frozenset({"1", "2", "3"}),
):
    terminal_by_level = _terminal_locked_unit_ids(evidence)
    if not terminal_by_level:
        return ()
    return tuple(
        sorted(
            (
                point
                for point in evidence.confirmed_points
                if terminal_by_level.get(point.structural_level)
                == point.anchor_unit_id
                and point.side in sides
                and point.point_type[0] in classes
            ),
            key=lambda point: (
                point.available_at,
                point.point_type,
                point.point_id,
            ),
        )
    )


def _current_divergences(
    evidence: StrictEvidenceResult,
    *,
    sides: frozenset[str],
):
    terminal_by_level = _terminal_locked_unit_ids(evidence)
    if not terminal_by_level:
        return ()
    return tuple(
        sorted(
            (
                divergence
                for divergence in evidence.divergences
                if terminal_by_level.get(divergence.structural_level)
                == divergence.signal_unit_id
                and ("buy" if divergence.direction == "down" else "sell") in sides
            ),
            key=lambda item: (item.available_at, item.kind, item.divergence_id),
        )
    )


def _event_sides(
    evidence: StrictEvidenceResult,
    *,
    allowed: frozenset[str],
) -> frozenset[str]:
    sides = {point.side for point in _current_points(evidence, sides=allowed)}
    sides.update(
        "buy" if item.direction == "down" else "sell"
        for item in _current_divergences(evidence, sides=allowed)
    )
    return frozenset(sides)


def _point_message(evidence: StrictEvidenceResult, points) -> str:
    labels = ",".join(
        f"L{point.structural_level}:{point.point_type}/{point.variant.value}"
        for point in points
    )
    return f"{evidence.source_frequency} 严格递归结构确认买卖点【{labels}】"


def _single_class_point(code, mk_datas, opt_types, point_class: str):
    sides = _allowed_sides(opt_types)
    evidence = _evidence(code, mk_datas, mk_datas.frequencys[0])
    points = _current_points(
        evidence,
        sides=sides,
        classes=frozenset({point_class}),
    )
    if not points:
        return None
    return {"code": code, "msg": _point_message(evidence, points)}


def select_strict_class1_point(
    code: str, mk_datas, opt_type: list | None = None
):
    return _single_class_point(code, mk_datas, opt_type, "1")


def select_strict_class2_point(
    code: str, mk_datas, opt_type: list | None = None
):
    return _single_class_point(code, mk_datas, opt_type, "2")


def select_strict_class3_point(
    code: str, mk_datas, opt_type: list | None = None
):
    return _single_class_point(code, mk_datas, opt_type, "3")


def select_strict_class3_after_class1(
    code: str,
    mk_datas,
    opt_type: list | None = None,
):
    sides = _allowed_sides(opt_type)
    evidence = _evidence(code, mk_datas, mk_datas.frequencys[0])
    thirds = _current_points(
        evidence,
        sides=sides,
        classes=frozenset({"3"}),
    )
    matches = []
    for third in thirds:
        expected = "1buy" if third.side == "buy" else "1sell"
        if any(
            point.point_type == expected
            and point.structural_level == third.structural_level
            and point.available_at <= third.available_at
            and point.anchor_at < third.anchor_at
            for point in evidence.confirmed_points
        ):
            matches.append(third)
    if not matches:
        return None
    return {
        "code": code,
        "msg": _point_message(evidence, matches) + "，且此前已有同级同向严格一类点",
    }


def select_strict_class3_after_trend_divergence(
    code: str,
    mk_datas,
    opt_type: list | None = None,
):
    sides = _allowed_sides(opt_type)
    evidence = _evidence(code, mk_datas, mk_datas.frequencys[0])
    thirds = _current_points(
        evidence,
        sides=sides,
        classes=frozenset({"3"}),
    )
    matches = []
    for third in thirds:
        expected = "1buy" if third.side == "buy" else "1sell"
        if any(
            point.point_type == expected
            and point.structural_level == third.structural_level
            and point.divergence is not None
            and point.divergence.kind == "trend"
            and point.available_at <= third.available_at
            and point.anchor_at < third.anchor_at
            for point in evidence.confirmed_points
        ):
            matches.append(third)
    if not matches:
        return None
    return {
        "code": code,
        "msg": _point_message(evidence, matches) + "，且此前同级严格趋势背驰已确认",
    }


def select_strict_point_divergence_confluence(
    code: str,
    mk_datas,
    opt_type: list | None = None,
):
    """要求当前严格买卖点与严格背驰方向一致。"""

    sides = _allowed_sides(opt_type)
    evidence = _evidence(code, mk_datas, mk_datas.frequencys[0])
    points = _current_points(evidence, sides=sides)
    divergence_sides = {
        "buy" if item.direction == "down" else "sell"
        for item in _current_divergences(evidence, sides=sides)
    }
    matches = tuple(point for point in points if point.side in divergence_sides)
    if not matches:
        return None
    return {
        "code": code,
        "msg": _point_message(evidence, matches) + "，且同时存在同向严格背驰",
    }


def select_strict_two_frequency_confluence(
    code: str,
    mk_datas,
    opt_type: list | None = None,
):
    sides = _allowed_sides(opt_type)
    high = _evidence(code, mk_datas, mk_datas.frequencys[0])
    low = _evidence(code, mk_datas, mk_datas.frequencys[1])
    matched = _event_sides(high, allowed=sides) & _event_sides(low, allowed=sides)
    if not matched:
        return None
    return {
        "code": code,
        "msg": (
            f"{high.source_frequency} 与 {low.source_frequency} "
            f"严格递归结构同向买卖点/背驰【{','.join(sorted(matched))}】"
        ),
    }


def select_strict_lower_class12_confluence(
    code: str,
    mk_datas,
    opt_type: list | None = None,
):
    sides = _allowed_sides(opt_type)
    high = _evidence(code, mk_datas, mk_datas.frequencys[0])
    high_sides = _event_sides(high, allowed=sides)
    if not high_sides:
        return None
    matched: set[str] = set()
    matched_frequencies = []
    for frequency in mk_datas.frequencys[1:]:
        low = _evidence(code, mk_datas, frequency)
        points = _current_points(
            low,
            sides=sides,
            classes=frozenset({"1", "2"}),
        )
        common = high_sides & {point.side for point in points}
        if common:
            matched.update(common)
            matched_frequencies.append(frequency)
    if not matched:
        return None
    return {
        "code": code,
        "msg": (
            f"{high.source_frequency} 严格递归结构事件，且低周期 "
            f"{','.join(matched_frequencies)} 出现同向严格一/二类点"
        ),
    }


def select_closed_ma250(code: str, mk_datas, opt_type: list | None = None):
    """非结构类对照任务，同样使用统一的收盘边界。"""

    sides = _allowed_sides(opt_type)
    frequency = mk_datas.frequencys[0]
    frame = _closed_frame(code, mk_datas, frequency)
    closes = pd.to_numeric(frame["close"], errors="raise")
    if len(closes) < 250:
        return None
    latest = float(closes.iloc[-1])
    average = float(closes.iloc[-250:].mean())
    if "buy" in sides and latest > average:
        return {"code": code, "msg": f"{frequency} 最新收盘价高于250周期均线"}
    if "sell" in sides and latest < average:
        return {"code": code, "msg": f"{frequency} 最新收盘价低于250周期均线"}
    return None


__all__ = (
    "select_closed_ma250",
    "select_strict_class1_point",
    "select_strict_class2_point",
    "select_strict_class3_after_class1",
    "select_strict_class3_after_trend_divergence",
    "select_strict_class3_point",
    "select_strict_lower_class12_confluence",
    "select_strict_point_divergence_confluence",
    "select_strict_two_frequency_confluence",
)
