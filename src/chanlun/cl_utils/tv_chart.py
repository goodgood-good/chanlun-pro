import logging
import math
from typing import Tuple, Union

import numpy as np
import pandas as pd

from chanlun import fun
from chanlun.core.types import ICL, Kline
from chanlun.core.macd import MACD
from chanlun.exchange import exchange


def kcharts_frequency_h_l_map(
    market: str, frequency
) -> Tuple[Union[None, str], Union[None, str]]:
    """
    将原周期，转换为新的周期进行图表展示
    按照设置好的对应关系进行返回

    返回两个值，第一个是需要获取的低级别周期值，第二个是 kcharts 画图指定的 to_frequency 值
    """
    # 高级别对应的低级别关系
    market_frequencs_map = {
        "a": {
            "m": "w",
            "w": "d",
            "d": "30m",
            "120m": "15m",
            "60m": "15m",
            "30m": "5m",
            "15m": "5m",
            "5m": "1m",
        },
        "futures": {
            "w": "d",
            "d": "60m",
            "60m": "10m",
            "30m": "5m",
            "15m": "3m",
            "10m": "2m",
            "6m": "1m",
            "5m": "1m",
            "3m": "1m",
        },
        # TODO 港美股没有写周期转换的方法，先不支持呢
        # 'us': {
        #     'y': 'q', 'q': 'm', 'm': 'w', 'w': 'd', 'd': '60m', '120m': '15m',
        #     '60m': '15m', '30m': '5m', '15m': '5m', '5m': '1m',
        # },
        # 'hk': {
        #     'y': 'm', 'm': 'w', 'w': 'd', 'd': '60m', '120m': '15m', '60m': '15m',
        #     '30m': '5m', '15m': '5m', '10m': '1m', '5m': '1m',
        # },
        "currency": {
            "w": "d",
            "d": "4h",
            "4h": "30m",
            "60m": "15m",
            "30m": "5m",
            "15m": "5m",
            "10m": "2m",
            "5m": "1m",
            "3m": "1m",
        },
    }

    try:
        return market_frequencs_map[market][frequency], f"{market}:{frequency}"
    except KeyError:
        # 不支持的 market/frequency 组合：返回 (None, None) 让调用方走默认分支。
        return None, None


def cl_qstd(cd: ICL, line_type="xd", line_num: int = 5):
    """
    缠论线段的趋势通道
    基于已完成的最后 n 条线段，线段最高两个点，线段最低两个点连线，作为趋势通道线指导交易（不一定精确）
    """
    lines = cd.get_xds() if line_type == "xd" else cd.get_bis()
    qs_lines = []
    for i in range(1, len(lines)):
        xd = lines[-i]
        if xd.is_done():
            qs_lines.append(xd)
        if len(qs_lines) == line_num:
            break

    if len(qs_lines) != line_num:
        return None

    line_highs = [
        {"val": l.high, "index": l.end.k.k_index, "date": l.end.k.date}
        for l in qs_lines
        if l.type == "up"
    ]
    line_lows = [
        {"val": l.low, "index": l.end.k.k_index, "date": l.end.k.date}
        for l in qs_lines
        if l.type == "down"
    ]
    if len(line_highs) < 2 or len(line_lows) < 2:
        return None
    line_highs = sorted(line_highs, key=lambda v: v["val"], reverse=True)
    line_lows = sorted(line_lows, key=lambda v: v["val"], reverse=False)

    def xl(one, two):
        k = (one["val"] - two["val"]) / (one["index"] - two["index"])
        return k

    qstd = {
        "up": {
            "one": line_highs[0],
            "two": line_highs[1],
            "xl": xl(line_highs[0], line_highs[1]),
        },
        "down": {
            "one": line_lows[0],
            "two": line_lows[1],
            "xl": xl(line_lows[0], line_lows[1]),
        },
    }
    chart_up_start = {
        "val": line_highs[0]["val"]
        - qstd["up"]["xl"] * (line_highs[0]["index"] - qs_lines[-1].start.k.k_index),
        "index": qs_lines[-1].start.k.k_index,
        "date": qs_lines[-1].start.k.date,
    }
    chart_up_end = {
        "val": line_highs[0]["val"]
        - qstd["up"]["xl"] * (line_highs[0]["index"] - cd.get_klines()[-1].index),
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    chart_down_start = {
        "val": line_lows[0]["val"]
        - qstd["down"]["xl"] * (line_lows[0]["index"] - qs_lines[-1].start.k.k_index),
        "index": qs_lines[-1].start.k.k_index,
        "date": qs_lines[-1].start.k.date,
    }
    chart_down_end = {
        "val": line_lows[0]["val"]
        - qstd["down"]["xl"] * (line_lows[0]["index"] - cd.get_klines()[-1].index),
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    qstd["up"]["chart"] = {
        "x": [chart_up_start["date"], chart_up_end["date"]],
        "y": [chart_up_start["val"], chart_up_end["val"]],
        "index": [chart_up_start["index"], chart_up_end["index"]],
    }
    qstd["down"]["chart"] = {
        "x": [chart_down_start["date"], chart_down_end["date"]],
        "y": [chart_down_start["val"], chart_down_end["val"]],
        "index": [chart_down_start["index"], chart_down_start["index"]],
    }

    now_point = {
        "val": cd.get_klines()[-1].c,
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    qstd["up"]["now"] = (
        "up" if xl(chart_up_start, now_point) > qstd["up"]["xl"] else "down"
    )
    qstd["down"]["now"] = (
        "up" if xl(chart_down_start, now_point) > qstd["down"]["xl"] else "down"
    )

    return qstd


def prices_jiaodu(prices):
    """
    技术价格序列中，起始与终点的角度（正为上，负为下）

    弧度 = dy / dx
        dy = 终点与起点的差值
        dx = 固定位 100000
        dy 如果不足六位数，进行补位
    不同品种的标的价格有差异，这时计算的角度会有很大的不同，不利于量化，将 dy 固定，变相的将所有标的放在一个尺度进行对比
    """
    if prices[-1] == prices[0]:
        return 0
    dy = max(prices[-1], prices[0]) - min(prices[-1], prices[0])
    dx = 100000
    while True:
        dy_len = len(str(int(dy)))
        if dy_len < 6:
            dy = dy * (10 ** (6 - dy_len))
        elif dy_len > 6:
            dy = dy / (10 ** (dy_len - 6))
        else:
            break
    # 弧度
    k = math.atan2(dy, dx)
    # 弧度转角度
    j = math.degrees(k)
    return j if prices[-1] > prices[0] else -j


def _line_to_chart_metadata(line) -> dict | None:
    """Serialize one center entering/leaving line without exposing core objects."""
    if line is None:
        return None
    start = getattr(line, "start", None)
    end = getattr(line, "end", None)
    start_k = getattr(start, "k", None)
    end_k = getattr(end, "k", None)
    start_date = getattr(start_k, "date", None)
    end_date = getattr(end_k, "date", None)
    return {
        "direction": getattr(line, "type", None),
        "start_time": (
            None if start_date is None else fun.datetime_to_int(start_date)
        ),
        "start_price": getattr(start, "val", None),
        "end_time": None if end_date is None else fun.datetime_to_int(end_date),
        "end_price": getattr(end, "val", None),
    }


def _center_associated_points(zs) -> list[str]:
    """Return point types explicitly attached to the center's leaving line."""
    seen: set[str] = set()
    result: list[str] = []
    for line in (getattr(zs, "exit", None), getattr(zs, "end", None)):
        getter = getattr(line, "line_mmds", None)
        if not callable(getter):
            continue
        try:
            values = getter("|")
        except TypeError:
            values = getter()
        for value in values or ():
            point_type = str(value)
            if point_type and point_type not in seen:
                seen.add(point_type)
                result.append(point_type)
        if result:
            break
    return result


def _line_associated_points(line) -> list[str]:
    """Return point types attached to one explicit leaving segment."""
    getter = getattr(line, "line_mmds", None)
    if not callable(getter):
        return []
    try:
        values = getter("|")
    except TypeError:
        values = getter()
    return list(dict.fromkeys(str(value) for value in (values or ()) if str(value)))


def _display_segment_price_quantum(lines):
    """Return an exact decimal quantum for the supplied segment endpoints."""
    from decimal import Decimal, InvalidOperation

    exponents = [0]
    for line in lines:
        for endpoint in (getattr(line, "start", None), getattr(line, "end", None)):
            try:
                value = Decimal(str(endpoint.val))
            except (AttributeError, InvalidOperation, ValueError) as exc:
                raise ValueError("segment center endpoint price must be finite") from exc
            if not value.is_finite():
                raise ValueError("segment center endpoint price must be finite")
            exponents.append(value.normalize().as_tuple().exponent)
    return Decimal(1).scaleb(min(exponents))


def _xd_five_role_center_payload(
    *,
    entry_unit,
    core_units,
    body_units,
    leaving_unit,
    completion_return_unit,
    evidence_units,
    unit_lines,
    quantum,
    center_id: str,
    state: str,
    done: bool,
    provisional: bool,
) -> dict | None:
    """Serialize one validated five-role XD center for the chart.

    ``done`` means every segment needed by the same-level third-class point is
    locked and the center is formal.  A live ``CenterPreview`` can already be
    geometrically complete while its return segment is still unlocked.  Keep
    those states separate so the page never collapses "three-sell geometry is
    present" back into the misleading binary label "forming".
    """
    expected_core = (
        ("down", "up", "down")
        if entry_unit.direction == "up"
        else ("up", "down", "up")
    )
    if (
        tuple(unit.direction for unit in core_units) != expected_core
        or leaving_unit.direction != entry_unit.direction
    ):
        return None

    entry_line = unit_lines[entry_unit.unit_id]
    leaving_line = unit_lines[leaving_unit.unit_id]
    completion_return_line = (
        None
        if completion_return_unit is None
        else unit_lines[completion_return_unit.unit_id]
    )
    geometry_completed = state == "completed"
    leaves_core = (
        leaving_unit.end_tick > min(unit.high_tick for unit in core_units)
        if leaving_unit.direction == "up"
        else leaving_unit.end_tick < max(unit.low_tick for unit in core_units)
    )
    if geometry_completed:
        completion_phase = (
            "FORMAL_THIRD_CLASS_POINT"
            if done
            else "GEOMETRIC_THIRD_CLASS_POINT"
        )
        completion_point_type = (
            "3buy" if leaving_unit.direction == "up" else "3sell"
        )
        completion_point_status = "confirmed" if done else "provisional"
        associated_points = _line_associated_points(completion_return_line)
        completion_point_observed = completion_point_type in associated_points
        if completion_point_type not in associated_points:
            associated_points.insert(0, completion_point_type)
    else:
        completion_phase = (
            "AWAITING_SAME_LEVEL_RETURN"
            if leaves_core
            else "AWAITING_SAME_LEVEL_DEPARTURE"
        )
        completion_point_type = None
        completion_point_status = None
        associated_points = []
        completion_point_observed = False
    expected_completion_point_type = (
        "3buy" if leaving_unit.direction == "up" else "3sell"
    )
    return {
        "points": [
            {
                "time": fun.datetime_to_int(core_units[0].market_start),
                "price": float(quantum * min(unit.high_tick for unit in core_units)),
            },
            {
                "time": fun.datetime_to_int(leaving_unit.market_start),
                "price": float(quantum * max(unit.low_tick for unit in core_units)),
            },
        ],
        # Geometrically completed previews use a solid box as the strict
        # renderer already does.  ``done`` and ``tradable`` remain false until
        # the return segment locks, so visual completion cannot authorize a
        # trade.
        "linestyle": "0" if (done or geometry_completed) else "1",
        "type": entry_unit.direction,
        "tower": "xd",
        "zd": float(quantum * max(unit.low_tick for unit in core_units)),
        "zg": float(quantum * min(unit.high_tick for unit in core_units)),
        "done": done,
        "state": state,
        "line_count": len(body_units),
        "core_line_count": 3,
        "core_directions": list(expected_core),
        "entering_segment": _line_to_chart_metadata(entry_line),
        "leaving_segment": _line_to_chart_metadata(leaving_line),
        # A third-class point belongs to the first return, not to the leaving
        # segment.  Reading ``leaving_line.line_mmds`` mixed center ownership
        # and made a lower-level point look as if it completed this center.
        "associated_points": associated_points,
        "confirmation_scope": "xd",
        "completion_phase": completion_phase,
        "completion_point_type": completion_point_type,
        "expected_completion_point_type": expected_completion_point_type,
        "completion_point_status": completion_point_status,
        # Distinguish calculated live geometry from a point already attached
        # by the same-level signal calculator. This matters when resolving an
        # overlap with an older ongoing center: observed 3-buy/3-sell evidence
        # may advance the interpretation, geometry alone may not.
        "completion_point_observed": completion_point_observed,
        "completion_return_segment": _line_to_chart_metadata(
            completion_return_line
        ),
        "center_id": center_id,
        "center_state": state,
        "provisional": provisional,
        "contains_unfinished_segment": any(
            not unit.locked for unit in evidence_units
        ),
        "render_kind": "center_preview" if provisional else "formal_center",
        "tradable": not provisional,
        "suppressed_overlapping_candidate_count": 0,
        "algorithm_revision": "chanlun-display-xd-five-role/v5",
    }


def _collapse_overlapping_xd_center_candidates(payloads: list[dict]) -> list[dict]:
    """Fold temporally overlapping same-level candidates into one center.

    A displayed center body spans from the first core segment to the leaving
    segment. Two bodies with a positive time overlap necessarily reuse the
    same segment evidence, so only the most advanced coherent interpretation
    may be shown. Evidence progress wins before object provenance: a confirmed
    formal center, then completed third-class-point geometry, then an ongoing
    formal center, then an ordinary forming preview. This prevents an older
    ongoing object from hiding a later 3-buy/3-sell and from inheriting only
    that candidate's opposite-direction leaving leg. Merely sharing one
    boundary timestamp is valid adjacency and is deliberately not collapsed.
    """

    def interval(value: dict) -> tuple[int, int]:
        first = int(value["points"][0]["time"])
        second = int(value["points"][1]["time"])
        return min(first, second), max(first, second)

    def overlaps(left: dict, right: dict) -> bool:
        left_start, left_end = interval(left)
        right_start, right_end = interval(right)
        return max(left_start, right_start) < min(left_end, right_end)

    def winner_key(value: dict) -> tuple[int, int, int]:
        start, end = interval(value)
        if value.get("done") is True:
            evidence_rank = 0
        elif (
            value.get("center_state") == "completed"
            and value.get("completion_point_observed") is True
        ):
            evidence_rank = 1
        elif value.get("render_kind") == "formal_center":
            evidence_rank = 2
        elif value.get("center_state") == "completed":
            evidence_rank = 3
        else:
            evidence_rank = 4
        return (
            evidence_rank,
            start,
            -end,
        )

    retained: list[dict] = []
    for raw_candidate in sorted(
        payloads,
        key=lambda item: (*interval(item), winner_key(item)[0]),
    ):
        candidate = dict(raw_candidate)
        candidate.setdefault("suppressed_overlapping_candidate_count", 0)
        overlapping_indexes = [
            index
            for index, existing in enumerate(retained)
            if overlaps(existing, candidate)
        ]
        if not overlapping_indexes:
            retained.append(candidate)
            continue

        contenders = [retained[index] for index in overlapping_indexes]
        contenders.append(candidate)
        winner = min(contenders, key=winner_key)
        suppressed_count = sum(
            int(value.get("suppressed_overlapping_candidate_count", 0))
            for value in contenders
        ) + len(contenders) - 1
        winner = dict(winner)
        winner["suppressed_overlapping_candidate_count"] = suppressed_count
        # Never splice role metadata from a losing candidate into the winner.
        # A shifted preview may have the opposite entry/leave direction and a
        # different core.  Grafting only its leave previously produced payloads
        # such as ``type=down`` with an ``up`` leaving segment, and could even
        # turn a confirmed ``done`` center back into ``provisional``.  A real
        # extension is projected from the same formal seed by center_machine
        # and replaces that formal snapshot before this overlap reducer runs.

        retained = [
            value
            for index, value in enumerate(retained)
            if index not in overlapping_indexes
        ]
        retained.append(winner)

    retained.sort(key=lambda item: interval(item))
    return retained


def xd_segment_centers_to_chart_dicts(lines) -> list[dict]:
    """Build display centers from five explicit roles in the shown XD sequence.

    Each center consumes the exact segment stream used by the page.  U1 is the
    entering segment, U2-U4 are the fixed three-segment core, and only a
    same-direction fifth segment serves as U5/the leaving leg.  Whether that
    leg has completed a true departure remains explicit in the center state.
    The live final segment participates through a provisional center preview;
    it is rendered as forming evidence until that segment becomes locked.
    The rectangle therefore spans U2.start -> leave.start;
    neither the entering nor leaving segment is folded into its horizontal body.
    """
    from chanlun.core.strict_structure.center_machine import calculate_centers
    from chanlun.core.strict_structure.identity import stable_structure_id
    from chanlun.core.strict_structure.models import (
        CenterPreviewState,
        CenterState,
        SourceKind,
    )
    from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines

    line_values = tuple(lines or ())
    if len(line_values) < 5:
        return []

    evidence_times = []
    for line in line_values:
        for value in (
            getattr(getattr(getattr(line, "start", None), "k", None), "date", None),
            getattr(getattr(getattr(line, "end", None), "k", None), "date", None),
            getattr(line, "locked_at", None),
        ):
            if value is not None:
                evidence_times.append(value)
    if not evidence_times:
        raise ValueError("segment center requires dated line evidence")

    quantum = _display_segment_price_quantum(line_values)
    units = adapt_lines(
        line_values,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
        price_quantum=quantum,
        as_of=max(evidence_times),
        registry=UnitLockRegistry("chanlun-display-xd-five-role/v5"),
    )
    unit_lines = {
        unit.unit_id: line
        for unit, line in zip(units, line_values, strict=True)
    }
    center_result = calculate_centers(
        units,
        structural_level=0,
        source_kind=SourceKind.SEGMENT,
    )

    preview_payloads = []
    preview_seed_ids: set[tuple[str, ...]] = set()
    units_by_id = {unit.unit_id: unit for unit in units}
    for preview in center_result.previews:
        if (
            preview.state not in (
                CenterPreviewState.FORMING,
                CenterPreviewState.COMPLETED,
            )
            or len(preview.unit_ids) < 5
            or preview.zd_tick is None
            or preview.zg_tick is None
            or preview.zd_tick >= preview.zg_tick
        ):
            continue
        body_units = tuple(units_by_id[unit_id] for unit_id in preview.unit_ids)
        initial_units = body_units[:5]
        entry_unit = initial_units[0]
        core_units = initial_units[1:4]
        leaving_unit = next(
            unit
            for unit in reversed(body_units)
            if unit.direction == entry_unit.direction
        )
        completion_return = (
            None
            if preview.completion_return_unit_id is None
            else units_by_id[preview.completion_return_unit_id]
        )
        center_id = stable_structure_id(
            "chanlun-center/v3",
            preview.price_basis_revision,
            preview.structural_level,
            preview.source_kind.value,
            tuple(unit.unit_id for unit in initial_units),
            preview.zd_tick,
            preview.zg_tick,
        )
        payload = _xd_five_role_center_payload(
            entry_unit=entry_unit,
            core_units=core_units,
            body_units=body_units,
            leaving_unit=leaving_unit,
            completion_return_unit=completion_return,
            evidence_units=(
                body_units
                if completion_return is None
                else body_units + (completion_return,)
            ),
            unit_lines=unit_lines,
            quantum=quantum,
            center_id=center_id,
            state=preview.state.value,
            # 未完成线段参与几何识别，但在线段锁定前不能冒充正式完成中枢。
            done=False,
            provisional=True,
        )
        if payload is None:
            continue
        preview_seed_ids.add(tuple(unit.unit_id for unit in initial_units))
        preview_payloads.append(payload)

    payloads = []
    for center in center_result.centers:
        seed_ids = tuple(unit.unit_id for unit in center.initial_units)
        if seed_ids in preview_seed_ids:
            # 同一中枢的实时预览包含最后未完成线段，必须取代只覆盖锁定前缀的
            # 正式快照，否则页面会一直少画最后一段直到重新加载。
            continue
        leaving_unit = (
            center.completion_leave_unit
            or center.pending_leave_unit
            or next(
                unit
                for unit in reversed(center.body_units)
                if unit.direction == center.entry_unit.direction
            )
        )
        entry_unit = center.entry_unit
        core_units = tuple(center.core_units)
        done = center.state is CenterState.COMPLETED
        evidence_units = center.body_units + (
            ()
            if center.completion_return_unit is None
            else (center.completion_return_unit,)
        )
        payload = _xd_five_role_center_payload(
            entry_unit=entry_unit,
            core_units=core_units,
            body_units=center.body_units,
            leaving_unit=leaving_unit,
            completion_return_unit=center.completion_return_unit,
            evidence_units=evidence_units,
            unit_lines=unit_lines,
            quantum=quantum,
            center_id=center.center_id,
            state=center.state.value,
            done=done,
            provisional=False,
        )
        if payload is not None:
            payloads.append(payload)
    payloads.extend(preview_payloads)
    return _collapse_overlapping_xd_center_candidates(payloads)


def zs_to_chart_dict(
    zs,
    use_envelope: bool = False,
    recursive_level: int | None = None,
) -> dict:
    """把 ZS 中枢序列化为前端图表 dict(core/web 共享,多周期叠加复用)。

    - points: 默认中枢核心区 [ZD,ZG]; use_envelope=True 用 [DD,GG] 包络
      (递归 L≥1 高级中枢 / 扩展中枢 / 多周期叠加需表达「瞬间波动」)。
    - linestyle: done→"0"(实线) / 未完成→"1"(虚线)。
    - type: 中枢方向(up/down/zd); is_expanded/sub_count: 扩展中枢标记。
    """
    hi = zs.gg if use_envelope else zs.zg
    lo = zs.dd if use_envelope else zs.zd
    entering = getattr(zs, "entry", None) or getattr(zs, "start", None)
    leaving = getattr(zs, "exit", None) or getattr(zs, "end", None)
    if recursive_level is None:
        recursive_level = getattr(zs, "recursive_level", None)
    return {
        "points": [
            {
                "time": fun.datetime_to_int(zs.start.end.k.date) if zs.start else fun.datetime_to_int(zs.lines[0].start.k.date),
                "price": hi,
            },
            {
                "time": fun.datetime_to_int(zs.end.start.k.date) if zs.end else fun.datetime_to_int(zs.lines[-1].end.k.date),
                "price": lo,
            },
        ],
        "linestyle": "0" if zs.done else "1",
        "type": zs.type,
        "is_expanded": bool(getattr(zs, "expanded_with", [])),
        "sub_count": len(getattr(zs, "expanded_with", []) or []),
        "tower": getattr(zs, "zs_type", None),
        "recursive_level": recursive_level,
        "zd": getattr(zs, "zd", None),
        "zg": getattr(zs, "zg", None),
        "done": bool(getattr(zs, "done", False)),
        "line_count": len(getattr(zs, "lines", []) or []),
        "entering_segment": _line_to_chart_metadata(entering),
        "leaving_segment": _line_to_chart_metadata(leaving),
        "associated_points": _center_associated_points(zs),
    }


_KUOZHAN_FREQ_CHAIN = {
    "1m": ["1m", "5m", "30m", "日线"], "5m": ["5m", "30m", "日线", "周线"],
    "15m": ["15m", "60m", "日线", "周线"], "30m": ["30m", "日线", "周线", "月线"],
    "60m": ["60m", "日线", "周线", "月线"], "d": ["日线", "周线", "月线", "年线"],
}


def _kuozhan_freq_label(base_freq: str, level: int) -> str:
    """递归 kuozhan 级别(1=高一级…)→ 频率标签,与前端 charts.js FREQ_CHAIN 对齐。"""
    chain = _KUOZHAN_FREQ_CHAIN.get(base_freq, [base_freq, "高一级", "高二级", "高三级"])
    return chain[level] if 0 <= level < len(chain) else f"L{level}"


def cl_data_to_tv_chart(
    cd: ICL,
    config: dict,
    to_frequency: str = None,
    *,
    strict_runtime=None,
) -> Union[dict, None]:
    """Convert base lines and one atomic strict evidence snapshot for TV charts."""

    from chanlun.cl_utils.strict_chart import (
        build_strict_structure_snapshot,
    )

    klines = pd.DataFrame(
        [
            {
                "date": k.date,
                "high": k.h,
                "low": k.l,
                "open": k.o,
                "close": k.c,
                "volume": k.a,
            }
            for k in cd.get_klines()
        ]
    )
    if len(klines) == 0:
        return None
    klines.loc[:, "code"] = cd.get_code()

    display_frequency = cd.get_frequency()
    if to_frequency is not None:
        parts = to_frequency.split(":")
        if len(parts) != 2 or not all(parts):
            raise ValueError("to_frequency must use market:frequency")
        market, display_frequency = parts
        if market == "a":
            klines = exchange.convert_stock_kline_frequency(
                klines,
                display_frequency,
            )
        elif market == "futures":
            klines = exchange.convert_futures_kline_frequency(
                klines,
                display_frequency,
            )
        elif market == "currency":
            klines = exchange.convert_currency_kline_frequency(
                klines,
                display_frequency,
            )
        else:
            raise ValueError(f"unsupported chart conversion market: {market}")

    kline_ts = klines["date"].map(fun.datetime_to_int).tolist()
    kline_cs = klines["close"].tolist()
    kline_os = klines["open"].tolist()
    kline_hs = klines["high"].tolist()
    kline_ls = klines["low"].tolist()
    kline_vs = klines["volume"].tolist()

    def _enabled(name: str) -> bool:
        return config.get(name) in ("1", 1, True)

    fx_data = []
    if _enabled("chart_show_fx"):
        valid_fxs = {}
        for bi in cd.get_bis():
            for fx in (bi.start, bi.end):
                if fx is not None:
                    valid_fxs[fun.datetime_to_int(fx.k.date)] = fx
        fx_data = [
            {
                "points": [
                    {"time": timestamp, "price": fx.val},
                    {"time": timestamp, "price": fx.val},
                ],
                "text": fx.type,
            }
            for timestamp, fx in sorted(valid_fxs.items())
        ]

    bi_chart_data = []
    if _enabled("chart_show_bi"):
        bi_chart_data = [
            {
                "points": [
                    {
                        "time": fun.datetime_to_int(bi.start.k.date),
                        "price": bi.start.val,
                    },
                    {
                        "time": fun.datetime_to_int(bi.end.k.date),
                        "price": bi.end.val,
                    },
                ],
                "linestyle": "0" if bi.is_done() else "1",
            }
            for bi in cd.get_bis()
        ]
        bi_chart_data.sort(key=lambda value: value["points"][0]["time"])

    xd_chart_data = []
    if _enabled("chart_show_xd"):
        xd_chart_data = [
            {
                "points": [
                    {
                        "time": fun.datetime_to_int(xd.start.k.date),
                        "price": xd.start.val,
                    },
                    {
                        "time": fun.datetime_to_int(xd.end.k.date),
                        "price": xd.end.val,
                    },
                ],
                "linestyle": (
                    "1" if getattr(xd, "forming", False) else "0"
                ),
            }
            for xd in cd.get_xds()
        ]
        xd_chart_data.sort(key=lambda value: value["points"][0]["time"])

    if to_frequency is not None or len(klines) != len(cd.get_src_klines()):
        macd = MACD(
            fast_period=int(config.get("idx_macd_fast", 12)),
            slow_period=int(config.get("idx_macd_slow", 26)),
            signal_period=int(config.get("idx_macd_signal", 9)),
        )
        macd_klines = [
            Kline(
                index=index,
                date=row["date"],
                h=float(row["high"]),
                l=float(row["low"]),
                o=float(row["open"]),
                c=float(row["close"]),
                a=float(row.get("volume") or 0.0),
            )
            for index, row in enumerate(klines.to_dict("records"))
        ]
        macd.process_macd(macd_klines)
        macd_idx = macd.get_results()["macd"]
    else:
        macd_idx = cd.get_idx()["macd"]

    result = {
        "t": kline_ts,
        "c": kline_cs,
        "o": kline_os,
        "h": kline_hs,
        "l": kline_ls,
        "v": kline_vs,
        "macd_dif": np.round(macd_idx["dif"], 6).tolist(),
        "macd_dea": np.round(macd_idx["dea"], 6).tolist(),
        "macd_hist": np.round(macd_idx["hist"], 6).tolist(),
        "macd_area": np.round(macd_idx.get("hist_area", []), 6).tolist(),
        "fxs": fx_data,
        "bis": bi_chart_data,
        "xds": xd_chart_data,
        # 基础结构中的“笔中枢”必须与页面当前 CL 的笔同源；页面配置为
        # 老笔时，这里也只能消费老笔形成的中枢，不能换成严格运行时的新笔证据。
        # 中枢是否显示由前端独立控制。这里始终传输当前周期的笔中枢，
        # 避免历史 chart_show_bi_zs 配置把新菜单默认开启的“笔中枢”变成空数据。
        "bi_zss": [zs_to_chart_dict(zs) for zs in cd.get_bi_zss()],
        # 当前周期中枢控制直接消费页面正在显示的老笔线段，并按五段角色重建：
        # 进入段 + 下上下/上下上三段主体 + 真正离开段。它与递归结构完全解耦。
        "xd_zss": xd_segment_centers_to_chart_dicts(cd.get_xds()),
    }

    if strict_runtime is not None and strict_runtime.error_code is not None:
        result["strict_structure_mode"] = "unavailable"
        result["strict_structure_error"] = {
            "code": strict_runtime.error_code
        }
        return result

    strict_cd = cd if strict_runtime is None else strict_runtime.cd
    error_code = "strict_evidence_invalid"
    try:
        if strict_cd is None:
            raise ValueError("strict chart runtime has no CL")
        evidence = strict_cd.get_strict_evidence()
        strict_structure = build_strict_structure_snapshot(
            evidence,
            interval=display_frequency,
        )
        if (
            strict_structure["symbol"] != cd.get_code()
            or strict_structure["source_frequency"] != display_frequency
            or strict_structure["display_frequency"] != display_frequency
            or strict_structure["source_closed_at"] != kline_ts[-1]
        ):
            error_code = "strict_context_mismatch"
            raise ValueError(
                "strict snapshot context does not match displayed bars"
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "strict chart structure unavailable "
            "code=%s frequency=%s error_code=%s error=%s: %s",
            cd.get_code(),
            display_frequency,
            error_code,
            type(exc).__name__,
            exc,
        )
        result["strict_structure_mode"] = "unavailable"
        result["strict_structure_error"] = {"code": error_code}
    else:
        result["strict_structure_mode"] = "replace"
        result["strict_structure"] = strict_structure

    return result
