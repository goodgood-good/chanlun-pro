"""把唯一严格结构证据原子化序列化为图表快照。"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.formal_state import (
    FormalDirectionState,
    resolve_formal_direction,
    resolve_level_formal_direction,
    semantic_trend_direction,
)
from chanlun.core.strict_structure.current_events import (
    TerminalSegmentReference,
    terminal_segment_reference,
)
from chanlun.core.strict_structure.level_catalog import recursive_level_labels
from chanlun.core.strict_structure.models import (
    CenterPreview,
    CenterPreviewState,
    CenterState,
    ConstituentUnit,
    DivergenceEvidence,
    SourceKind,
    StrictEvidenceResult,
    StrictPointEvidence,
    StrictPointStatus,
    TrendCenter,
    TrendKind,
    TrendState,
    TrendType,
    center_seed_size,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    classify_five_minute_setup_state,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)


CHART_STRUCTURE_SCHEMA = "chanlun-chart-structure"
CHART_CENTER_SCHEMA = "chanlun-chart-center"
_ACTIVE_CENTER_STATES = frozenset({CenterState.ONGOING})


def _preview_lifecycle_role_count(preview: CenterPreview) -> int:
    leave_present = (
        preview.pending_leave_unit_id is not None
        or preview.completion_leave_unit_id is not None
    )
    return 1 + len(preview.unit_ids) + int(leave_present)


def _unique_units_in_time_order(
    units: Iterable[ConstituentUnit],
) -> tuple[ConstituentUnit, ...]:
    """按物理市场时间顺序返回不重复的证据单元。"""

    by_id: dict[str, ConstituentUnit] = {}
    for unit in units:
        by_id.setdefault(unit.unit_id, unit)
    return tuple(
        sorted(
            by_id.values(),
            key=lambda unit: (unit.market_start, unit.market_end, unit.unit_id),
        )
    )


def _center_overlap_units(
    center: TrendCenter,
    leaving_unit: ConstituentUnit | None,
) -> tuple[ConstituentUnit, ...]:
    """返回中枢生命周期中所有存在正宽重叠的物理构成单元。"""

    if center.source_kind is SourceKind.TREND_TYPE:
        return center.body_units
    return _unique_units_in_time_order(
        (
            center.entry_unit,
            *center.body_units,
            *(
                ()
                if center.establishment_leave_unit is None
                else (center.establishment_leave_unit,)
            ),
            *(() if leaving_unit is None else (leaving_unit,)),
        )
    )


def _renderable_center_preview(preview: CenterPreview) -> bool:
    """返回预览是否已经达到相应来源的图表展示门槛。

    物理预览在 ``S0`` 进入段与具有正宽重叠的中间三段 ``S1..S3`` 核心出现后
    即可展示。缺少 ``S4`` 成熟段时，矩形仍属于不可交易的临时预览；已完成预览
    与正式中枢仍必须满足完整五角色生命周期。递归走势类型预览继续使用原有的
    三走势门槛。
    """

    if preview.state not in (
        CenterPreviewState.FORMING,
        CenterPreviewState.COMPLETED,
    ):
        return False
    required_body_count = center_seed_size(preview.source_kind)
    if (
        len(preview.unit_ids) < required_body_count
        or preview.zd_tick is None
        or preview.zg_tick is None
    ):
        return False
    if preview.source_kind is not SourceKind.TREND_TYPE:
        lifecycle_role_count = _preview_lifecycle_role_count(preview)
        if preview.establishment_unit_id is None:
            if (
                preview.state is not CenterPreviewState.FORMING
                or len(preview.unit_ids) != 3
                or lifecycle_role_count != 4
            ):
                return False
        elif lifecycle_role_count < 5:
            return False
    return (
        preview.zd_tick <= preview.zg_tick
        if preview.source_kind is SourceKind.TREND_TYPE
        else preview.zd_tick < preview.zg_tick
    )


def aware_datetime_to_epoch_seconds(value: datetime) -> int:
    """把带时区时间精确转换为 UTC Unix 秒坐标。"""

    if not isinstance(value, datetime):
        raise TypeError("chart time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("chart time must be timezone-aware")
    if value.microsecond:
        raise ValueError("chart time must have whole-second precision")
    return int(value.astimezone(timezone.utc).timestamp())


def _optional_epoch(value: datetime | None) -> int | None:
    return None if value is None else aware_datetime_to_epoch_seconds(value)


def _unit_audit_payload(unit: ConstituentUnit) -> dict[str, object]:
    """序列化一个严格单元，供中枢进入、离开证据审计展示。"""

    if not isinstance(unit, ConstituentUnit):
        raise TypeError("center audit unit must be a ConstituentUnit")
    return {
        "unit_id": unit.unit_id,
        "direction": unit.direction,
        "start_time": aware_datetime_to_epoch_seconds(unit.market_start),
        "end_time": aware_datetime_to_epoch_seconds(unit.market_end),
        "start_tick": unit.start_tick,
        "end_tick": unit.end_tick,
        "low_tick": unit.low_tick,
        "high_tick": unit.high_tick,
        "locked": unit.locked,
        "forming": unit.forming,
    }


def _center_lifecycle_payload(
    *,
    pending_leave: ConstituentUnit | None,
    completion_leave: ConstituentUnit | None,
    completed: bool,
    provisional: bool = False,
    observation: bool = False,
) -> dict[str, object]:
    """序列化唯一的同级离开、回返生命周期契约。"""

    leave = completion_leave if completed else pending_leave
    expected_point_type = (
        None if leave is None else "3buy" if leave.direction == "up" else "3sell"
    )
    if observation:
        phase = "NON_TRADABLE_OBSERVATION"
        point_type = None
        point_status = None
    elif completed and provisional:
        phase = "GEOMETRIC_THIRD_CLASS_POINT"
        point_type = expected_point_type
        point_status = "provisional"
    elif completed:
        phase = "FORMAL_THIRD_CLASS_POINT"
        point_type = expected_point_type
        point_status = "confirmed"
    elif pending_leave is not None:
        phase = "AWAITING_SAME_LEVEL_RETURN"
        point_type = None
        point_status = None
    else:
        phase = "AWAITING_SAME_LEVEL_DEPARTURE"
        point_type = None
        point_status = None
    return {
        "completion_phase": phase,
        "completion_point_type": point_type,
        "expected_completion_point_type": expected_point_type,
        "completion_point_status": point_status,
    }


def _center_payload(
    center: TrendCenter,
    *,
    render_kind: str,
    tradable: bool,
) -> dict[str, object]:
    if not isinstance(center, TrendCenter):
        raise TypeError("center must be a TrendCenter")
    if not center.has_minimum_physical_roles:
        raise ValueError(
            "physical chart center requires exact five-segment establishment"
        )
    if (
        center.zd_tick > center.zg_tick
        if center.source_kind is SourceKind.TREND_TYPE
        else center.zd_tick >= center.zg_tick
    ):
        raise ValueError("formal chart center violates source overlap contract")
    leaving_unit = center.completion_leave_unit or center.pending_leave_unit
    establishment_units = center.establishment_units
    overlap_units = _center_overlap_units(center, leaving_unit)
    return {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": render_kind,
        "center_id": center.center_id,
        "render_id": (
            f"{center.center_id}@{center.body_revision}@{center.state.value}"
        ),
        "body_revision": center.body_revision,
        "structural_level": center.structural_level,
        "source_kind": center.source_kind.value,
        "state": center.state.value,
        "tradable": bool(tradable and center.physically_completed),
        **_center_lifecycle_payload(
            pending_leave=center.pending_leave_unit,
            completion_leave=center.completion_leave_unit,
            completed=center.physically_completed,
            observation=(center.source_kind is SourceKind.STROKE_OBSERVATION),
        ),
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(
                    center.display_range_start_market_time
                ),
                "price_tick": center.zg_tick,
            },
            {
                "time": aware_datetime_to_epoch_seconds(
                    center.display_range_end_market_time
                ),
                "price_tick": center.zd_tick,
            },
        ],
        "display_range": {
            "start_role": "middle_three_first_start",
            "end_role": (
                "body_tail_end" if center.extension_units else "middle_three_last_end"
            ),
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": "middle_three_intersection",
        },
        "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
        "envelope": {"dd_tick": center.dd_tick, "gg_tick": center.gg_tick},
        "entry_unit_id": center.entry_unit.unit_id,
        "entry_role": "external_entry",
        "lifecycle_role_count": center.lifecycle_role_count,
        "minimum_lifecycle_role_count": (
            3 if center.source_kind is SourceKind.TREND_TYPE else 5
        ),
        "core_component_count": 3,
        "overlap_component_count": len(overlap_units),
        "establishment_component_count": len(establishment_units),
        "establishment_segment_ids": [unit.unit_id for unit in establishment_units],
        "first_three_component_ids": [unit.unit_id for unit in center.core_units],
        "core_unit_ids": [unit.unit_id for unit in center.core_units],
        "establishment_unit_id": (
            None
            if center.establishment_unit is None
            else center.establishment_unit.unit_id
        ),
        "initial_exit_unit_id": (
            None
            if center.initial_exit_unit is None
            else center.initial_exit_unit.unit_id
        ),
        "initial_unit_ids": [unit.unit_id for unit in center.initial_units],
        "body_unit_ids": [unit.unit_id for unit in center.body_units],
        "extension_unit_ids": [unit.unit_id for unit in center.extension_units],
        "pending_leave_unit_id": (
            None
            if center.pending_leave_unit is None
            else center.pending_leave_unit.unit_id
        ),
        "completion_leave_unit_id": (
            None
            if center.completion_leave_unit is None
            else center.completion_leave_unit.unit_id
        ),
        "completion_return_unit_id": (
            None
            if center.completion_return_unit is None
            else center.completion_return_unit.unit_id
        ),
        "completion_direction": center.completion_direction,
        "boundary_divergence_id": center.boundary_divergence_id,
        "boundary_anchor_unit_id": center.boundary_anchor_unit_id,
        "entering_segment": _unit_audit_payload(center.entry_unit),
        "first_three_components": [
            _unit_audit_payload(unit) for unit in center.core_units
        ],
        "overlap_components": [_unit_audit_payload(unit) for unit in overlap_units],
        "establishment_segments": [
            _unit_audit_payload(unit) for unit in establishment_units
        ],
        "leaving_segment": (
            None if leaving_unit is None else _unit_audit_payload(leaving_unit)
        ),
        "completion_return_segment": (
            None
            if center.completion_return_unit is None
            else _unit_audit_payload(center.completion_return_unit)
        ),
        "established_market_time": aware_datetime_to_epoch_seconds(
            center.established_market_time
        ),
        "established_at": aware_datetime_to_epoch_seconds(center.established_at),
        "completed_at": _optional_epoch(center.completed_at),
        "available_at": aware_datetime_to_epoch_seconds(center.available_at),
    }


def strict_center_to_chart_dict(center: TrendCenter) -> dict[str, object]:
    """在不改变来源几何的前提下序列化正式中枢。"""

    if center.source_kind is SourceKind.STROKE_OBSERVATION:
        raise ValueError("formal serializer rejects stroke observation")
    if not center.tradable:
        raise ValueError("formal chart center must be tradable")
    return _center_payload(
        center,
        render_kind="formal_center",
        tradable=True,
    )


def center_observation_to_chart_dict(
    center: TrendCenter,
) -> dict[str, object]:
    """序列化明确不可交易的笔中枢观察。"""

    if center.source_kind is not SourceKind.STROKE_OBSERVATION:
        raise ValueError("center observation requires stroke_observation source")
    return _center_payload(
        center,
        render_kind="center_observation",
        tradable=False,
    )


def active_center_projection_to_chart_dict(
    center: TrendCenter,
    source_closed_at: datetime,
) -> dict[str, object]:
    """构建唯一活动框，但不把框体延伸到离开段。"""

    if center.source_kind is SourceKind.STROKE_OBSERVATION:
        raise ValueError("formal center projection rejects stroke observation")
    if not center.has_minimum_physical_roles:
        raise ValueError(
            "physical center projection requires exact five-segment establishment"
        )
    if center.state not in _ACTIVE_CENTER_STATES:
        raise ValueError("center projection requires an active center")
    closed_epoch = aware_datetime_to_epoch_seconds(source_closed_at)
    lifecycle_end_epoch = aware_datetime_to_epoch_seconds(
        center.display_range_end_market_time
    )
    if closed_epoch < lifecycle_end_epoch:
        raise ValueError("source close cannot precede center lifecycle end")
    display_start_epoch = aware_datetime_to_epoch_seconds(
        center.display_range_start_market_time
    )
    leaving_unit = center.pending_leave_unit
    overlap_units = _center_overlap_units(center, leaving_unit)
    return {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": "center_projection",
        "center_id": center.center_id,
        "render_id": (
            f"{center.center_id}@{center.body_revision}@{center.state.value}"
            f"@projection@{closed_epoch}"
        ),
        "body_revision": center.body_revision,
        "structural_level": center.structural_level,
        "source_kind": center.source_kind.value,
        "state": center.state.value,
        "tradable": False,
        **_center_lifecycle_payload(
            pending_leave=center.pending_leave_unit,
            completion_leave=None,
            completed=False,
        ),
        "points": [
            {"time": display_start_epoch, "price_tick": center.zg_tick},
            {"time": lifecycle_end_epoch, "price_tick": center.zd_tick},
        ],
        "display_range": {
            "start_role": "middle_three_first_start",
            "end_role": (
                "body_tail_end" if center.extension_units else "middle_three_last_end"
            ),
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": "middle_three_intersection",
        },
        "core": {"zd_tick": center.zd_tick, "zg_tick": center.zg_tick},
        "envelope": {"dd_tick": center.dd_tick, "gg_tick": center.gg_tick},
        "entry_unit_id": center.entry_unit.unit_id,
        "entry_role": "external_entry",
        "lifecycle_role_count": center.lifecycle_role_count,
        "minimum_lifecycle_role_count": (
            3 if center.source_kind is SourceKind.TREND_TYPE else 5
        ),
        "core_component_count": 3,
        "overlap_component_count": len(overlap_units),
        "establishment_component_count": len(center.establishment_units),
        "establishment_segment_ids": [
            unit.unit_id for unit in center.establishment_units
        ],
        "core_unit_ids": [unit.unit_id for unit in center.core_units],
        "establishment_unit_id": (
            None
            if center.establishment_unit is None
            else center.establishment_unit.unit_id
        ),
        "initial_exit_unit_id": (
            None
            if center.initial_exit_unit is None
            else center.initial_exit_unit.unit_id
        ),
        "initial_unit_ids": [unit.unit_id for unit in center.initial_units],
        "body_unit_ids": [unit.unit_id for unit in center.body_units],
        "extension_unit_ids": [unit.unit_id for unit in center.extension_units],
        "pending_leave_unit_id": (
            None if leaving_unit is None else leaving_unit.unit_id
        ),
        "completion_leave_unit_id": None,
        "completion_return_unit_id": None,
        "entering_segment": _unit_audit_payload(center.entry_unit),
        "first_three_component_ids": [unit.unit_id for unit in center.core_units],
        "first_three_components": [
            _unit_audit_payload(unit) for unit in center.core_units
        ],
        "overlap_components": [_unit_audit_payload(unit) for unit in overlap_units],
        "establishment_segments": [
            _unit_audit_payload(unit) for unit in center.establishment_units
        ],
        "leaving_segment": (
            None if leaving_unit is None else _unit_audit_payload(leaving_unit)
        ),
        "source_center_render_id": (
            f"{center.center_id}@{center.body_revision}@{center.state.value}"
        ),
        "available_at": aware_datetime_to_epoch_seconds(center.available_at),
    }


def strict_center_preview_to_chart_dict(
    preview: CenterPreview,
    units: tuple[ConstituentUnit, ...],
    source_closed_at: datetime,
) -> dict[str, object]:
    """序列化一个不可交易的临时中枢生命周期。"""

    if not isinstance(preview, CenterPreview):
        raise TypeError("preview must be a CenterPreview")
    if not _renderable_center_preview(preview):
        raise ValueError("chart center preview requires a source-valid center core")

    values = tuple(units)
    by_id = {item.unit_id: item for item in values}
    if len(by_id) != len(values):
        raise ValueError("chart center preview units require unique ids")
    try:
        entry = by_id[preview.entry_unit_id]
        body = tuple(by_id[item_id] for item_id in preview.unit_ids)
        pending_leave = (
            None
            if preview.pending_leave_unit_id is None
            else by_id[preview.pending_leave_unit_id]
        )
        completion_leave = (
            None
            if preview.completion_leave_unit_id is None
            else by_id[preview.completion_leave_unit_id]
        )
        completion_return = (
            None
            if preview.completion_return_unit_id is None
            else by_id[preview.completion_return_unit_id]
        )
        establishment_leave = (
            None
            if preview.establishment_leave_unit_id is None
            else by_id[preview.establishment_leave_unit_id]
        )
        establishment_unit = (
            None
            if preview.establishment_unit_id is None
            else by_id[preview.establishment_unit_id]
        )
    except KeyError as exc:
        raise ValueError("chart center preview unit is absent from level") from exc
    core_units = body[:3]
    seed_width = center_seed_size(preview.source_kind)
    initial_units = body[:seed_width]
    lifecycle_tail = completion_return or completion_leave or pending_leave or body[-1]
    lifecycle_units = (
        (entry,)
        + body
        + (() if pending_leave is None else (pending_leave,))
        + (() if completion_leave is None else (completion_leave,))
        + (() if completion_return is None else (completion_return,))
    )
    if any(
        item.structural_level != preview.structural_level
        or item.source_kind is not preview.source_kind
        or item.price_basis_revision != preview.price_basis_revision
        for item in lifecycle_units
    ):
        raise ValueError("chart center preview unit context mismatch")
    unlocked_seen = False
    for item in lifecycle_units:
        if not item.locked:
            unlocked_seen = True
        elif unlocked_seen:
            raise ValueError("chart center preview units must have a locked prefix")
    if not unlocked_seen and establishment_unit is not None:
        raise ValueError("chart center preview requires provisional units")
    for previous, current in zip(lifecycle_units, lifecycle_units[1:]):
        if (
            (
                preview.source_kind is SourceKind.SEGMENT
                and previous.direction == current.direction
            )
            or previous.end_tick != current.start_tick
            or current.market_start < previous.market_end
        ):
            raise ValueError("chart center preview lifecycle is disconnected")
    expected_zd = max(item.low_tick for item in core_units)
    expected_zg = min(item.high_tick for item in core_units)
    if (preview.zd_tick, preview.zg_tick) != (expected_zd, expected_zg):
        raise ValueError("chart center preview core does not match its seed")
        # 每个线段成分都必须与冻结核心形成正宽度重叠。递归走势类型继续沿用原有
        # 闭区间相等规则；完成回抽有意不计入 ``body``。
    if any(
        (
            max(item.low_tick, preview.zd_tick) > min(item.high_tick, preview.zg_tick)
            if preview.source_kind is SourceKind.TREND_TYPE
            else max(item.low_tick, preview.zd_tick)
            >= min(item.high_tick, preview.zg_tick)
        )
        for item in body
    ):
        raise ValueError("chart center preview body must positively overlap its core")
    if preview.source_kind is not SourceKind.TREND_TYPE:
        if max(entry.low_tick, preview.zd_tick) >= min(
            entry.high_tick, preview.zg_tick
        ):
            raise ValueError(
                "chart center preview entry must positively overlap its core"
            )
        if establishment_unit is not None and max(
            establishment_unit.low_tick, preview.zd_tick
        ) >= min(establishment_unit.high_tick, preview.zg_tick):
            raise ValueError(
                "chart center preview fifth unit must positively overlap its core"
            )
    if source_closed_at < lifecycle_tail.market_end:
        raise ValueError("source close cannot precede center preview tail")
    if preview.state is CenterPreviewState.COMPLETED:
        if completion_leave is None or completion_return is None:
            raise ValueError("completed chart preview requires leave and return units")
        if (
            completion_leave.end_tick <= preview.zg_tick
            if completion_leave.direction == "up"
            else completion_leave.end_tick >= preview.zd_tick
        ):
            raise ValueError("completed chart preview leave geometry is invalid")
        return_stays_outside = (
            completion_return.direction == "down"
            and completion_return.low_tick >= preview.zg_tick
            if completion_leave.direction == "up"
            else completion_return.direction == "up"
            and completion_return.high_tick <= preview.zd_tick
        )
        if not return_stays_outside:
            raise ValueError(
                "completed chart preview return must stay outside its core"
            )
    else:
        if completion_leave is not None or completion_return is not None:
            raise ValueError("forming chart preview cannot retain completion evidence")

    leaving_unit = (
        completion_leave
        if preview.state is CenterPreviewState.COMPLETED
        else pending_leave
    )
    if preview.source_kind is not SourceKind.TREND_TYPE and leaving_unit is not None:
        if max(leaving_unit.low_tick, preview.zd_tick) >= min(
            leaving_unit.high_tick, preview.zg_tick
        ):
            raise ValueError(
                "chart center preview leave must positively overlap its core"
            )
    initial_exit = establishment_leave
    establishment_units = (
        tuple(initial_units)
        if preview.source_kind is SourceKind.TREND_TYPE
        else (
            (entry, *core_units)
            if establishment_unit is None
            else (entry, *core_units, establishment_unit)
        )
    )
    overlap_units = (
        tuple(body)
        if preview.source_kind is SourceKind.TREND_TYPE
        else _unique_units_in_time_order(
            (
                entry,
                *body,
                *(() if leaving_unit is None else (leaving_unit,)),
            )
        )
    )
    preview_id = stable_structure_id(
        "chanlun-center-preview",
        preview.price_basis_revision,
        preview.structural_level,
        preview.source_kind.value,
        preview.entry_unit_id,
        preview.unit_ids[:seed_width],
        preview.establishment_unit_id,
        preview.zd_tick,
        preview.zg_tick,
    )
    formal_center_id = (
        preview.formal_center_id
        if preview.source_kind is SourceKind.TREND_TYPE
        or preview.establishment_unit_id is not None
        else None
    )
    closed_epoch = aware_datetime_to_epoch_seconds(source_closed_at)
    completed = preview.state is CenterPreviewState.COMPLETED
    end_epoch = (
        aware_datetime_to_epoch_seconds(leaving_unit.market_start)
        if leaving_unit is not None
        else aware_datetime_to_epoch_seconds(body[-1].market_end)
    )
    return {
        "schema": CHART_CENTER_SCHEMA,
        "render_kind": "center_preview",
        # ``center_id`` 始终表示可提升的正式中枢身份。物理预览在第五段成立
        # 证据出现之前只能拥有渲染身份，不能提前承诺一个正式中枢。
        "center_id": formal_center_id,
        "preview_id": preview_id,
        "render_id": (
            f"{preview_id}@{preview.state.value}@{len(body) - seed_width}"
            f"@{preview.completion_return_unit_id or closed_epoch}"
        ),
        "body_revision": len(body) - seed_width,
        "structural_level": preview.structural_level,
        "source_kind": preview.source_kind.value,
        "state": preview.state.value,
        "tradable": False,
        **_center_lifecycle_payload(
            pending_leave=pending_leave,
            completion_leave=completion_leave,
            completed=completed,
            provisional=completed,
        ),
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(core_units[0].market_start),
                "price_tick": preview.zg_tick,
            },
            {"time": end_epoch, "price_tick": preview.zd_tick},
        ],
        "display_range": {
            "start_role": "middle_three_first_start",
            "end_role": ("body_tail_end" if len(body) > 3 else "middle_three_last_end"),
            "includes_entry": False,
            "includes_leave": False,
            "price_core_source": "middle_three_intersection",
        },
        "core": {"zd_tick": preview.zd_tick, "zg_tick": preview.zg_tick},
        "envelope": {
            "dd_tick": min(item.low_tick for item in body),
            "gg_tick": max(item.high_tick for item in body),
        },
        "entry_unit_id": entry.unit_id,
        "entry_role": "external_entry",
        "lifecycle_role_count": _preview_lifecycle_role_count(preview),
        "minimum_lifecycle_role_count": (
            3 if preview.source_kind is SourceKind.TREND_TYPE else 5
        ),
        "core_component_count": 3,
        "overlap_component_count": len(overlap_units),
        "establishment_component_count": len(establishment_units),
        "establishment_segment_ids": [item.unit_id for item in establishment_units],
        "first_three_component_ids": [item.unit_id for item in core_units],
        "core_unit_ids": [item.unit_id for item in core_units],
        "establishment_unit_id": (
            None if establishment_unit is None else establishment_unit.unit_id
        ),
        "initial_exit_unit_id": (
            None if initial_exit is None else initial_exit.unit_id
        ),
        "initial_unit_ids": [item.unit_id for item in initial_units],
        "body_unit_ids": [item.unit_id for item in body],
        "extension_unit_ids": [item.unit_id for item in body[seed_width:]],
        "pending_leave_unit_id": (
            leaving_unit.unit_id
            if (not completed and leaving_unit is not None)
            else None
        ),
        "completion_leave_unit_id": (
            None if completion_leave is None else completion_leave.unit_id
        ),
        "completion_return_unit_id": (
            None if completion_return is None else completion_return.unit_id
        ),
        "completion_direction": (
            None if completion_leave is None else completion_leave.direction
        ),
        "entering_segment": _unit_audit_payload(entry),
        "first_three_components": [_unit_audit_payload(item) for item in core_units],
        "overlap_components": [_unit_audit_payload(item) for item in overlap_units],
        "establishment_segments": [
            _unit_audit_payload(item) for item in establishment_units
        ],
        "leaving_segment": (
            None if leaving_unit is None else _unit_audit_payload(leaving_unit)
        ),
        "completion_return_segment": (
            None
            if completion_return is None
            else _unit_audit_payload(completion_return)
        ),
        "established_market_time": (
            None
            if preview.source_kind is not SourceKind.TREND_TYPE
            and establishment_unit is None
            else aware_datetime_to_epoch_seconds(
                (
                    initial_units[-1]
                    if preview.source_kind is SourceKind.TREND_TYPE
                    else establishment_unit
                ).market_end
            )
        ),
        "established_at": _optional_epoch(
            None
            if preview.source_kind is not SourceKind.TREND_TYPE
            and establishment_unit is None
            else (
                initial_units[-1]
                if preview.source_kind is SourceKind.TREND_TYPE
                else establishment_unit
            ).confirmed_at
        ),
        # 几何完成但回返单元未锁定时没有正式确认时间。这里必须保持空值，
        # 防止消费者把盘中三类形态误当成已确认交易信号。
        "completed_at": None,
        "available_at": aware_datetime_to_epoch_seconds(preview.available_at),
    }


def _formal_direction_payload(
    state: FormalDirectionState,
) -> dict[str, object]:
    """把正式方向状态转为稳定、可审计的图表字段。"""

    return {
        "direction": state.direction,
        "structural_level": state.structural_level,
        "trend_id": state.trend_id,
        "support_point_id": state.support_point_id,
        "reason_codes": list(state.reason_codes),
    }


def _trend_direction_status(
    trend: TrendType,
    formal_direction: FormalDirectionState | None,
) -> str:
    """区分几何走势、正式方向、待确认反转和已经结束的走势。"""

    if trend.kind is TrendKind.CONSOLIDATION:
        return "consolidation"
    is_formal_target = (
        formal_direction is not None
        and formal_direction.structural_level == trend.structural_level
        and formal_direction.trend_id == trend.trend_id
    )
    if is_formal_target and formal_direction.direction in {"up", "down"}:
        return "formal"
    if is_formal_target and (
        "direction_change_lacks_first_or_second_point" in formal_direction.reason_codes
    ):
        return "awaiting_reversal_support"
    if trend.state is not TrendState.FORMING or trend.terminal_divergence is not None:
        return "ended"
    return "geometric_candidate"


def strict_trend_to_chart_dict(
    trend: TrendType,
    *,
    formal_direction: FormalDirectionState | None = None,
) -> dict[str, object]:
    """序列化一条严格走势，并显式标明它是否属于正式方向。"""

    if not isinstance(trend, TrendType):
        raise TypeError("走势必须是 TrendType")
    if formal_direction is not None and not isinstance(
        formal_direction,
        FormalDirectionState,
    ):
        raise TypeError("正式方向必须是 FormalDirectionState")
    terminal_id = trend.terminal_unit.unit_id
    direction_status = _trend_direction_status(trend, formal_direction)
    is_formal_target = (
        formal_direction is not None
        and formal_direction.structural_level == trend.structural_level
        and formal_direction.trend_id == trend.trend_id
    )
    semantic_direction = semantic_trend_direction(trend)
    return {
        "schema": "chanlun-chart-trend",
        "render_kind": "strict_trend",
        "trend_id": trend.trend_id,
        "render_id": (
            f"{trend.trend_id}@{trend.state.value}@{terminal_id}@{direction_status}"
        ),
        "structural_level": trend.structural_level,
        "source_kind": trend.terminal_unit.source_kind.value,
        "state": trend.state.value,
        "kind": trend.kind.value,
        # direction 保留递归结构所需的首尾几何方向；正式走势方向只能读取
        # semantic_direction 与 direction_status，二者不得互相替代。
        "direction": trend.direction,
        "geometric_direction": trend.direction,
        "semantic_direction": semantic_direction,
        "direction_status": direction_status,
        "formal_direction_confirmed": direction_status == "formal",
        "formal_support_point_id": (
            formal_direction.support_point_id if is_formal_target else None
        ),
        "direction_reason_codes": (
            list(formal_direction.reason_codes) if is_formal_target else []
        ),
        "tradable": trend.terminal_unit.source_kind
        is not SourceKind.STROKE_OBSERVATION,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(trend.market_start),
                "price_tick": trend.start_tick,
            },
            {
                "time": aware_datetime_to_epoch_seconds(trend.market_end),
                "price_tick": trend.end_tick,
            },
        ],
        "range": {"low_tick": trend.low_tick, "high_tick": trend.high_tick},
        "center_ids": [center.center_id for center in trend.centers],
        "constituent_unit_ids": [unit.unit_id for unit in trend.constituent_units],
        "confirmed_at": _optional_epoch(trend.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(trend.available_at),
    }


def strict_divergence_to_chart_dict(
    divergence: DivergenceEvidence,
) -> dict[str, object]:
    """序列化一条级别明确、彼此独立的正式背驰证据。"""

    if not isinstance(divergence, DivergenceEvidence):
        raise TypeError("divergence must be a DivergenceEvidence")
    if divergence.source_kind is SourceKind.STROKE_OBSERVATION:
        raise ValueError("formal divergence rejects stroke observation")
    metrics = {
        "price_extreme_confirmed": divergence.price_extreme_confirmed,
        "histogram_area_decayed": divergence.histogram_area_decayed,
        "histogram_peak_decayed": divergence.histogram_peak_decayed,
        "dif_extreme_decayed": divergence.dif_extreme_decayed,
        "strength_source": divergence.strength_source,
        "is_divergent": divergence.is_divergent,
        "strength_decay_count": divergence.strength_decay_count,
    }
    return {
        "schema": "chanlun-chart-divergence",
        "render_kind": "strict_divergence",
        "render_id": divergence.divergence_id,
        "divergence_id": divergence.divergence_id,
        "kind": divergence.kind,
        "direction": divergence.direction,
        "structural_level": divergence.structural_level,
        "source_kind": divergence.source_kind.value,
        "price_basis_revision": divergence.price_basis_revision,
        "compare_unit_id": divergence.compare_unit_id,
        "signal_unit_id": divergence.signal_unit_id,
        "comparison_width": divergence.comparison_width,
        "compare_leg_unit_ids": list(divergence.compare_leg_unit_ids),
        "signal_leg_unit_ids": list(divergence.signal_leg_unit_ids),
        "anchor_at": aware_datetime_to_epoch_seconds(divergence.anchor_at),
        "anchor_tick": divergence.anchor_tick,
        "confirmed_at": aware_datetime_to_epoch_seconds(divergence.confirmed_at),
        "available_at": aware_datetime_to_epoch_seconds(divergence.available_at),
        "metrics": metrics,
        "tradable": True,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(divergence.anchor_at),
                "price_tick": divergence.anchor_tick,
            }
        ],
    }


def _divergence_payload(point: StrictPointEvidence) -> dict[str, object] | None:
    divergence = point.divergence
    if divergence is None:
        return None
    payload = strict_divergence_to_chart_dict(divergence)
    return {
        key: value
        for key, value in payload.items()
        if key not in {"schema", "render_kind", "render_id", "tradable", "points"}
    }


def strict_point_to_chart_dict(
    point: StrictPointEvidence,
    *,
    terminal_segment: TerminalSegmentReference | None = None,
    operational_confirmed_at: datetime | None = None,
) -> dict[str, object]:
    """序列化严格点，并显式区分操作确认与防重绘审计锁。

    严格核心的 ``approaching`` 表示锚定线段尚未完成最终防重绘锁；交易层则在
    最新已完成线段首次保留完整几何时就确认操作点。图表必须展示与选股相同的
    操作状态，同时通过 ``strict_status`` 和 ``lock_state`` 保留底层审计事实。
    """

    if not isinstance(point, StrictPointEvidence):
        raise TypeError("point must be StrictPointEvidence")
    if terminal_segment is not None and (
        terminal_segment.structural_level != point.structural_level
        or terminal_segment.unit_id != point.anchor_unit_id
    ):
        raise ValueError("chart point terminal segment lineage mismatch")
    operational_confirmation = operational_confirmed_at is not None
    if operational_confirmation and (
        point.status is not StrictPointStatus.APPROACHING
        or terminal_segment is None
        or terminal_segment.role != "latest_completed"
        or terminal_segment.state not in {"formed", "locked"}
    ):
        raise ValueError(
            "operational chart confirmation requires the latest completed segment"
        )
    if operational_confirmation and (
        operational_confirmed_at < point.anchor_at
        or operational_confirmed_at > point.available_at
    ):
        raise ValueError("operational chart confirmation time is invalid")

    strict_status = point.status.value
    status = "confirmed" if operational_confirmation else strict_status
    evidence_revision = "sha256:" + stable_structure_id(
        "chanlun-chart-point-evidence",
        point.point_id,
        strict_status,
        status,
        operational_confirmed_at,
        (
            None
            if terminal_segment is None
            else (
                terminal_segment.role,
                terminal_segment.structural_level,
                terminal_segment.unit_id,
                terminal_segment.state,
                terminal_segment.available_at,
            )
        ),
        point.variant.value,
        point.available_at,
        point.evidence_codes,
        point.missing_conditions,
        point.related_point_ids,
        point.small_to_large_carrier_unit_ids,
    )
    render_kind = "point_confirmed" if status == "confirmed" else "point_approaching"
    render_id = (
        point.point_id
        if point.status is StrictPointStatus.CONFIRMED
        else f"{point.point_id}@{evidence_revision}"
    )
    setup_state = classify_five_minute_setup_state(
        point_type=point.point_type,
        status="confirmed" if status == "confirmed" else "provisional",
        evidence_codes=point.evidence_codes,
        missing_conditions=point.missing_conditions,
        terminal_segment_role=(
            None if terminal_segment is None else terminal_segment.role
        ),
        terminal_segment_state=(
            None if terminal_segment is None else terminal_segment.state
        ),
    )
    evidence_codes = tuple(point.evidence_codes)
    if (
        operational_confirmation
        and "geometry_confirmed_before_audit_lock" not in evidence_codes
    ):
        evidence_codes = (*evidence_codes, "geometry_confirmed_before_audit_lock")
    return {
        "schema": "chanlun-chart-point",
        "render_kind": render_kind,
        "render_id": render_id,
        "point_id": point.point_id,
        "point_type": point.point_type,
        "side": point.side,
        "status": status,
        "strict_status": strict_status,
        "operational_confirmation": operational_confirmation,
        "confirmation_basis": (
            "latest_completed_geometry"
            if operational_confirmation
            else "strict_audit_lock"
            if point.status is StrictPointStatus.CONFIRMED
            else None
        ),
        "formation_state": setup_state.formation_state,
        "lock_state": setup_state.lock_state,
        "actionable": setup_state.actionable,
        "contains_forming_segment": setup_state.contains_forming_segment,
        "contains_unlocked_segment": setup_state.contains_unlocked_segment,
        "terminal_segment_role": (
            None if terminal_segment is None else terminal_segment.role
        ),
        "terminal_segment_state": (
            None if terminal_segment is None else terminal_segment.state
        ),
        "variant": point.variant.value,
        "structural_level": point.structural_level,
        "source_kind": point.source_kind.value,
        "price_basis_revision": point.price_basis_revision,
        "anchor_unit_id": point.anchor_unit_id,
        "anchor_at": aware_datetime_to_epoch_seconds(point.anchor_at),
        "confirmed_at": _optional_epoch(
            operational_confirmed_at if operational_confirmation else point.confirmed_at
        ),
        "available_at": aware_datetime_to_epoch_seconds(point.available_at),
        "anchor_tick": point.anchor_tick,
        "invalidation_tick": point.invalidation_tick,
        "center_id": point.center_id,
        "center_zd_tick": point.center_zd_tick,
        "center_zg_tick": point.center_zg_tick,
        "center_ordinal": point.center_ordinal,
        "parent_point_id": point.parent_point_id,
        "divergence": _divergence_payload(point),
        "evidence_codes": list(evidence_codes),
        "missing_conditions": list(point.missing_conditions),
        "related_point_ids": list(point.related_point_ids),
        "small_to_large_carrier_unit_ids": list(point.small_to_large_carrier_unit_ids),
        "evidence_revision": evidence_revision,
        "tradable": setup_state.actionable,
        "points": [
            {
                "time": aware_datetime_to_epoch_seconds(point.anchor_at),
                "price_tick": point.anchor_tick,
            }
        ],
    }


def _chart_point_terminal_projection(
    evidence: StrictEvidenceResult,
    point: StrictPointEvidence,
) -> tuple[TerminalSegmentReference | None, datetime | None]:
    """返回图表点的末端血缘与操作确认时刻。

    这里只投影生产交易级别 ``5m/L0``。1 分钟段差、30 分钟环境以及递归高层
    仍展示严格核心原始状态，不能被误升格为可操作买卖点。
    """

    reference = terminal_segment_reference(
        evidence.structure,
        structural_level=point.structural_level,
        unit_id=point.anchor_unit_id,
    )
    if (
        point.status is not StrictPointStatus.APPROACHING
        or not is_five_minute_trade_level(
            evidence.source_frequency,
            point.structural_level,
        )
        or reference is None
        or reference.role != "latest_completed"
    ):
        return reference, None

    unit = next(
        (
            item
            for level in evidence.structure.levels
            if level.structural_level == point.structural_level
            for item in level.units
            if item.unit_id == point.anchor_unit_id
        ),
        None,
    )
    if unit is None or unit.formed_at is None:
        return reference, None
    # 与 structure_adapter.extract_current_confirmed_points 完全同口径：当前仍为
    # formed 时使用首次完整几何见证；重建到后来已锁定的历史截面时回溯 formed_at。
    confirmed_at = point.available_at if reference.state == "formed" else unit.formed_at
    return reference, confirmed_at


def _operational_third_class_center_payload(
    payload: dict[str, object],
    *,
    point: StrictPointEvidence,
    confirmed_at: datetime,
) -> dict[str, object]:
    """把同一三类点的中枢说明同步为操作确认，不改写审计锁事实。"""

    if point.point_type not in {"3buy", "3sell"}:
        raise ValueError("operational center projection requires a third-class point")
    if (
        payload.get("center_id") != point.center_id
        or payload.get("completion_point_type") != point.point_type
        or payload.get("completion_phase") != "GEOMETRIC_THIRD_CLASS_POINT"
    ):
        return payload
    return {
        **payload,
        "completion_phase": "OPERATIONAL_THIRD_CLASS_POINT",
        "completion_point_status": "confirmed",
        "tradable": True,
        "operational_confirmation": True,
        "confirmation_basis": "latest_completed_geometry",
        "audit_lock_state": "pending",
        "operational_point_id": point.point_id,
        "operational_confirmed_at": aware_datetime_to_epoch_seconds(confirmed_at),
    }


def _canonical_quantum(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError("structure_price_quantum must be a positive Decimal")
    return format(value.normalize(), "f")


def _tick_price(tick: int, quantum: Decimal) -> float:
    if type(tick) is not int:
        raise TypeError("price_tick must be an integer")
    value = Decimal(tick) * quantum
    if not value.is_finite():
        raise ValueError("chart price must be finite")
    return float(value)


def _with_prices(value: object, quantum: Decimal) -> object:
    if isinstance(value, list):
        return [_with_prices(item, quantum) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _with_prices(item, quantum) for key, item in value.items()}
    tick_price_names = {
        "price_tick": "price",
        "anchor_tick": "anchor_price",
        "invalidation_tick": "invalidation_price",
        "center_zd_tick": "center_zd_price",
        "center_zg_tick": "center_zg_price",
        "zd_tick": "zd_price",
        "zg_tick": "zg_price",
        "dd_tick": "dd_price",
        "gg_tick": "gg_price",
        "low_tick": "low_price",
        "high_tick": "high_price",
        "start_tick": "start_price",
        "end_tick": "end_price",
    }
    for tick_name, price_name in tick_price_names.items():
        tick = value.get(tick_name)
        if tick is not None:
            result[price_name] = _tick_price(tick, quantum)
    return result


def _visible(values: Iterable[object], source_closed_at: datetime) -> tuple:
    return tuple(
        item for item in values if getattr(item, "available_at") <= source_closed_at
    )


def _sorted_payloads(values: Iterable[dict[str, object]], id_name: str) -> list:
    return sorted(
        values,
        key=lambda item: (
            int(item.get("available_at", 0)),
            str(item.get(id_name, "")),
            str(item.get("render_id", "")),
        ),
    )


def _revision(namespace: str, *parts: object) -> str:
    return "sha256:" + stable_structure_id(namespace, *parts)


def build_strict_structure_snapshot(
    evidence: StrictEvidenceResult,
    *,
    interval: str,
) -> dict[str, object]:
    """构建唯一权威且不依赖可视窗口的严格图表快照。"""

    if not isinstance(evidence, StrictEvidenceResult):
        raise TypeError("evidence must be StrictEvidenceResult")
    if not isinstance(interval, str) or not interval.strip():
        raise ValueError("interval is required")
    if interval != evidence.source_frequency:
        raise ValueError("display interval must equal strict source frequency")
    if evidence.structure.price_basis_revision != evidence.price_basis_revision:
        raise ValueError("strict structure price basis mismatch")
    quantum = evidence.structure_price_quantum
    quantum_text = _canonical_quantum(quantum)
    source_closed_epoch = aware_datetime_to_epoch_seconds(evidence.source_closed_at)
    labels = recursive_level_labels(interval)
    formal_direction = resolve_formal_direction(evidence)
    formal_direction_payload = _formal_direction_payload(formal_direction)

    confirmed_by_level: dict[
        int,
        list[
            tuple[
                StrictPointEvidence,
                TerminalSegmentReference | None,
                datetime | None,
            ]
        ],
    ] = {}
    approaching_by_level: dict[
        int,
        list[
            tuple[
                StrictPointEvidence,
                TerminalSegmentReference | None,
                datetime | None,
            ]
        ],
    ] = {}
    divergences_by_level: dict[int, list[DivergenceEvidence]] = {}
    for point in _visible(evidence.confirmed_points, evidence.source_closed_at):
        reference, _operational_at = _chart_point_terminal_projection(
            evidence,
            point,
        )
        confirmed_by_level.setdefault(point.structural_level, []).append(
            (point, reference, None)
        )
    for point in _visible(evidence.approaching_points, evidence.source_closed_at):
        reference, operational_at = _chart_point_terminal_projection(
            evidence,
            point,
        )
        target = (
            confirmed_by_level if operational_at is not None else approaching_by_level
        )
        target.setdefault(point.structural_level, []).append(
            (point, reference, operational_at)
        )
    for divergence in _visible(evidence.divergences, evidence.source_closed_at):
        divergences_by_level.setdefault(
            divergence.structural_level,
            [],
        ).append(divergence)

    known_levels = {level.structural_level for level in evidence.structure.levels}
    point_levels = set(confirmed_by_level) | set(approaching_by_level)
    if not point_levels.issubset(known_levels):
        raise ValueError("strict point level is absent from structure snapshot")
    if not set(divergences_by_level).issubset(known_levels):
        raise ValueError("strict divergence level is absent from structure snapshot")
    if any(
        type(level.structural_level) is not int
        or level.structural_level < 0
        or level.structural_level >= len(labels)
        for level in evidence.structure.levels
    ):
        raise ValueError("strict structure level is outside display catalog")

    levels: list[dict[str, object]] = []
    for level in evidence.structure.levels:
        level_formal_direction = resolve_level_formal_direction(
            evidence,
            level.structural_level,
        )
        level_formal_direction_payload = _formal_direction_payload(
            level_formal_direction
        )
        centers = _visible(
            level.center_result.centers,
            evidence.source_closed_at,
        )
        center_previews = tuple(
            preview
            for preview in _visible(
                level.center_result.previews,
                evidence.source_closed_at,
            )
            if _renderable_center_preview(preview)
        )
        current_trends = _visible(
            level.trend_types,
            evidence.source_closed_at,
        )
        completed_trends = _visible(
            level.completed_trends,
            evidence.source_closed_at,
        )
        center_payloads = _sorted_payloads(
            (strict_center_to_chart_dict(center) for center in centers),
            "center_id",
        )
        preview_payloads = _sorted_payloads(
            (
                strict_center_preview_to_chart_dict(
                    preview,
                    level.units,
                    evidence.source_closed_at,
                )
                for preview in center_previews
            ),
            "preview_id",
        )
        operational_thirds = tuple(
            (point, operational_at)
            for point, _reference, operational_at in confirmed_by_level.get(
                level.structural_level,
                (),
            )
            if operational_at is not None and point.point_type in {"3buy", "3sell"}
        )
        for point, operational_at in operational_thirds:
            center_payloads = [
                _operational_third_class_center_payload(
                    payload,
                    point=point,
                    confirmed_at=operational_at,
                )
                for payload in center_payloads
            ]
            preview_payloads = [
                _operational_third_class_center_payload(
                    payload,
                    point=point,
                    confirmed_at=operational_at,
                )
                for payload in preview_payloads
            ]
        active_center = (
            centers[-1]
            if centers and centers[-1].state in _ACTIVE_CENTER_STATES
            else None
        )
        projections = _sorted_payloads(
            ()
            if active_center is None or preview_payloads
            else (
                active_center_projection_to_chart_dict(
                    active_center,
                    evidence.source_closed_at,
                ),
            ),
            "center_id",
        )
        current_payloads = _sorted_payloads(
            (
                strict_trend_to_chart_dict(
                    trend,
                    formal_direction=level_formal_direction,
                )
                for trend in current_trends
            ),
            "trend_id",
        )
        completed_payloads = _sorted_payloads(
            (
                strict_trend_to_chart_dict(
                    trend,
                    formal_direction=level_formal_direction,
                )
                for trend in completed_trends
            ),
            "trend_id",
        )
        confirmed_payloads = _sorted_payloads(
            (
                strict_point_to_chart_dict(
                    point,
                    terminal_segment=reference,
                    operational_confirmed_at=operational_at,
                )
                for point, reference, operational_at in confirmed_by_level.get(
                    level.structural_level,
                    (),
                )
            ),
            "point_id",
        )
        approaching_payloads = _sorted_payloads(
            (
                strict_point_to_chart_dict(
                    point,
                    terminal_segment=reference,
                    operational_confirmed_at=operational_at,
                )
                for point, reference, operational_at in approaching_by_level.get(
                    level.structural_level,
                    (),
                )
            ),
            "point_id",
        )
        divergence_payloads = _sorted_payloads(
            (
                strict_divergence_to_chart_dict(divergence)
                for divergence in divergences_by_level.get(
                    level.structural_level,
                    (),
                )
            ),
            "divergence_id",
        )
        levels.append(
            {
                "structural_level": level.structural_level,
                "label": labels[level.structural_level],
                "origin": "current_chart_recursive",
                "formal_direction": level_formal_direction_payload,
                "centers": center_payloads,
                "center_projections": projections,
                "center_previews": preview_payloads,
                "current_trends": current_payloads,
                "completed_trend_snapshots": completed_payloads,
                "confirmed_points": confirmed_payloads,
                "approaching_points": approaching_payloads,
                "divergences": divergence_payloads,
            }
        )

    observations = _sorted_payloads(
        (
            center_observation_to_chart_dict(center)
            for center in _visible(
                evidence.stroke_center_observations.centers,
                evidence.source_closed_at,
            )
        ),
        "center_id",
    )
    snapshot_revision = _revision(
        "chanlun-chart-snapshot",
        evidence.structure_revision,
        source_closed_epoch,
    )
    render_extras = {
        "formal_direction": formal_direction_payload,
        "stroke_center_observations": observations,
        "level_extras": [
            {
                "structural_level": level["structural_level"],
                "formal_direction": level["formal_direction"],
                "center_projections": level["center_projections"],
                "center_previews": level["center_previews"],
                "operational_confirmed_points": [
                    point
                    for point in level["confirmed_points"]
                    if point.get("operational_confirmation") is True
                ],
                "approaching_points": level["approaching_points"],
            }
            for level in levels
        ],
    }
    render_revision = _revision(
        "chanlun-chart-render",
        snapshot_revision,
        render_extras,
    )
    snapshot = {
        "schema": CHART_STRUCTURE_SCHEMA,
        "symbol": evidence.symbol,
        "source_frequency": evidence.source_frequency,
        "display_frequency": interval,
        "price_basis_revision": evidence.price_basis_revision,
        "structure_price_quantum": quantum_text,
        "strict_config_revision": evidence.strict_config_revision,
        "source_closed_at": source_closed_epoch,
        "structure_revision": evidence.structure_revision,
        "snapshot_revision": snapshot_revision,
        "render_revision": render_revision,
        "formal_direction": formal_direction_payload,
        "stroke_center_observations": observations,
        "levels": levels,
    }
    return _with_prices(snapshot, quantum)


__all__ = [
    "CHART_CENTER_SCHEMA",
    "CHART_STRUCTURE_SCHEMA",
    "active_center_projection_to_chart_dict",
    "aware_datetime_to_epoch_seconds",
    "build_strict_structure_snapshot",
    "center_observation_to_chart_dict",
    "strict_center_preview_to_chart_dict",
    "strict_center_to_chart_dict",
    "strict_divergence_to_chart_dict",
    "strict_point_to_chart_dict",
    "strict_trend_to_chart_dict",
]
