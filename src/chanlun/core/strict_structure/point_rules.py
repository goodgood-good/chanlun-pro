"""正式与盘中买卖点共用的纯结构规则。"""

from __future__ import annotations

from decimal import Decimal

from chanlun.core.strict_structure.center_relation import classify_center_relation
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterPreview,
    CenterPreviewState,
    CenterRelation,
    CenterState,
    ConstituentUnit,
    SourceKind,
    StrictPointEvidence,
    StrictPointStatus,
    StrictPointVariant,
    StrictStructureResult,
    TrendCenter,
)


def build_approaching_point_id(
    *,
    price_basis_revision: str,
    point_type: str,
    structural_level: int,
    anchor_unit_id: str,
    center_id: str | None,
    parent_point_id: str | None,
) -> str:
    """构建盘中预判点的稳定身份。"""

    return stable_structure_id(
        "chanlun-strict-approaching",
        price_basis_revision,
        point_type,
        structural_level,
        anchor_unit_id,
        center_id,
        parent_point_id,
    )


def classify_third_class_geometry(
    *,
    zd_tick: int,
    zg_tick: int,
    leave: ConstituentUnit,
    return_unit: ConstituentUnit,
):
    """用同一套离开、首次回返几何判定正式与盘中三类点。"""

    if (
        leave.direction == "up"
        and return_unit.direction == "down"
        and return_unit.low_tick >= zg_tick
    ):
        direction = "up"
        point_type = "3buy"
        side = "buy"
        anchor_tick = return_unit.low_tick
        invalidation_tick = zg_tick
        boundary_tick = zg_tick
    elif (
        leave.direction == "down"
        and return_unit.direction == "up"
        and return_unit.high_tick <= zd_tick
    ):
        direction = "down"
        point_type = "3sell"
        side = "sell"
        anchor_tick = return_unit.high_tick
        invalidation_tick = zd_tick
        boundary_tick = zd_tick
    else:
        return None
    variant = (
        StrictPointVariant.BOUNDARY_TOUCH
        if anchor_tick == boundary_tick
        else StrictPointVariant.STANDARD
    )
    return (
        direction,
        point_type,
        side,
        anchor_tick,
        invalidation_tick,
        variant,
    )


def center_ordinals(
    centers: tuple[TrendCenter, ...],
    decomposition_boundaries=(),
) -> dict[tuple[str, str], int]:
    """在背驰边界切分的严格走势序列内，按中枢核心位置编号。"""

    values = tuple(centers)
    if len({center.center_id for center in values}) != len(values):
        raise ValueError("同一结构级别的中枢身份必须唯一")
    boundary_center_ids = tuple(
        boundary.terminal_center_id for boundary in decomposition_boundaries
    )
    if len(set(boundary_center_ids)) != len(boundary_center_ids):
        raise ValueError("划分边界的末端中枢必须唯一")
    unknown_boundary_centers = set(boundary_center_ids) - {
        center.center_id for center in values
    }
    if unknown_boundary_centers:
        raise ValueError("划分边界引用了不存在的中枢")
    boundary_center_ids = set(boundary_center_ids)
    output: dict[tuple[str, str], int] = {}
    up_run = 1
    down_run = 1
    previous = None
    for center in values:
        if previous is not None:
            if previous.center_id in boundary_center_ids:
                up_run = down_run = 1
            else:
                # 先复用正式关系校验，确保两个中枢同级、同源且时序合法。
                # 序号表达的是不可变 ZD/ZG 核心的连续抬高/降低；正式走势
                # 关系仍由 center_relation 按更严格的 DD/GG 外围完全分离判定。
                classify_center_relation(previous, center)
                relation = (
                    CenterRelation.UP_TREND
                    if center.zd_tick > previous.zg_tick
                    else CenterRelation.DOWN_TREND
                    if center.zg_tick < previous.zd_tick
                    else CenterRelation.UPGRADE
                )
                if relation is CenterRelation.UP_TREND:
                    up_run += 1
                    down_run = 1
                elif relation is CenterRelation.DOWN_TREND:
                    down_run += 1
                    up_run = 1
                else:
                    up_run = down_run = 1
        output[(center.center_id, "up")] = up_run
        output[(center.center_id, "down")] = down_run
        previous = center
    return output


def preview_center_ordinal(
    level,
    preview: CenterPreview,
    *,
    direction: str,
) -> int:
    """按预览锁定后所在的同级中枢序列计算方向编号。"""

    center_id = preview.formal_center_id
    if center_id is None:
        raise ValueError("中枢预览没有可提升的正式身份")
    centers = tuple(level.center_result.centers)
    ordinals = center_ordinals(centers, level.decomposition_boundaries)
    existing = tuple(center for center in centers if center.center_id == center_id)
    if existing:
        if len(existing) != 1:
            raise ValueError("中枢预览映射到多个正式中枢")
        return ordinals[(center_id, direction)]

    units_by_id = {unit.unit_id: unit for unit in level.units}
    try:
        body = tuple(units_by_id[unit_id] for unit_id in preview.unit_ids)
    except KeyError as exc:
        raise ValueError("中枢预览本体不在同级单元账本中") from exc
    if not body:
        raise ValueError("中枢预览缺少本体单元")
    body_start = body[0].market_start
    previous = None
    for center in centers:
        shares_completion_boundary = (
            center.completion_return_unit is not None
            and preview.entry_unit_id == center.completion_return_unit.unit_id
        )
        if center.body_start_market_time >= body_start:
            continue
        if (
            body_start < center.last_touch_market_time
            and not shares_completion_boundary
        ):
            continue
        previous = center
    if previous is None:
        return 1
    if previous.center_id in {
        boundary.terminal_center_id
        for boundary in level.decomposition_boundaries
    }:
        return 1

    if preview.zd_tick is None or preview.zg_tick is None:
        raise ValueError("中枢预览缺少来源有效的核心区间")
    relation = (
        "up"
        if preview.zd_tick > previous.zg_tick
        else "down"
        if preview.zg_tick < previous.zd_tick
        else "upgrade"
    )
    if relation != direction:
        return 1
    return ordinals[(previous.center_id, direction)] + 1


def approaching_third_class_points(
    level,
    *,
    price_quantum: Decimal,
) -> tuple[StrictPointEvidence, ...]:
    """从唯一严格结构账本重放该级别全部盘中三类点。"""

    active = level.units[level.center_result.locked_unit_count :]
    if not active:
        return ()
    tail = active[-1]
    if tail.locked:
        raise ValueError("活动结构尾单元必须保持未锁定")

    output: dict[str, StrictPointEvidence] = {}
    centers = tuple(level.center_result.centers)
    center_ids = {center.center_id for center in centers}
    units_by_id = {unit.unit_id: unit for unit in level.units}

    for preview in level.center_result.previews:
        if (
            preview.state is not CenterPreviewState.COMPLETED
            or preview.source_kind is SourceKind.STROKE_OBSERVATION
            or preview.zd_tick is None
            or preview.zg_tick is None
            or preview.completion_leave_unit_id is None
            or preview.completion_return_unit_id is None
        ):
            continue
        center_id = preview.formal_center_id
        if center_id is None:
            raise ValueError("已完成中枢预览缺少正式身份")
        if center_id in center_ids:
            # 正式进行中中枢的投影由下面的正式中枢分支负责。
            continue
        try:
            leave = units_by_id[preview.completion_leave_unit_id]
            return_unit = units_by_id[preview.completion_return_unit_id]
        except KeyError as exc:
            raise ValueError("中枢预览完成证据不在同级单元账本中") from exc
        classified = classify_third_class_geometry(
            zd_tick=preview.zd_tick,
            zg_tick=preview.zg_tick,
            leave=leave,
            return_unit=return_unit,
        )
        if classified is None:
            continue
        (
            direction,
            point_type,
            side,
            anchor_tick,
            invalidation_tick,
            variant,
        ) = classified
        point = StrictPointEvidence(
            point_id=build_approaching_point_id(
                price_basis_revision=preview.price_basis_revision,
                point_type=point_type,
                structural_level=preview.structural_level,
                anchor_unit_id=return_unit.unit_id,
                center_id=center_id,
                parent_point_id=None,
            ),
            point_type=point_type,
            side=side,
            status=StrictPointStatus.APPROACHING,
            variant=variant,
            structural_level=preview.structural_level,
            source_kind=preview.source_kind,
            price_basis_revision=preview.price_basis_revision,
            anchor_unit_id=return_unit.unit_id,
            anchor_at=return_unit.market_end,
            confirmed_at=None,
            available_at=max(preview.available_at, return_unit.available_at),
            price_quantum=price_quantum,
            anchor_tick=anchor_tick,
            invalidation_tick=invalidation_tick,
            center_id=center_id,
            center_zd_tick=preview.zd_tick,
            center_zg_tick=preview.zg_tick,
            center_ordinal=preview_center_ordinal(
                level,
                preview,
                direction=direction,
            ),
            divergence=None,
            parent_point_id=None,
            evidence_codes=(
                "unified_strict_signal_engine",
                "unfinished_segment_participates",
                "provisional_center_completion",
                "core_boundary_held",
            ),
            missing_conditions=(
                "unfinished_segment_lock",
                "formal_center_confirmation",
            ),
        )
        previous = output.setdefault(point.point_id, point)
        if previous != point:
            raise ValueError("盘中三类点身份映射到冲突预览证据")

    ordinals = center_ordinals(centers, level.decomposition_boundaries)
    for center in reversed(centers):
        if center.source_kind is SourceKind.STROKE_OBSERVATION:
            continue
        leave = center.pending_leave_unit
        if (
            leave is None
            or not leave.locked
            or leave.end_tick != tail.start_tick
            or tail.market_start < leave.market_end
            or center.state is not CenterState.ONGOING
        ):
            continue
        classified = classify_third_class_geometry(
            zd_tick=center.zd_tick,
            zg_tick=center.zg_tick,
            leave=leave,
            return_unit=tail,
        )
        if classified is None:
            continue
        (
            direction,
            point_type,
            side,
            anchor_tick,
            invalidation_tick,
            variant,
        ) = classified
        point = StrictPointEvidence(
            point_id=build_approaching_point_id(
                price_basis_revision=center.price_basis_revision,
                point_type=point_type,
                structural_level=center.structural_level,
                anchor_unit_id=tail.unit_id,
                center_id=center.center_id,
                parent_point_id=None,
            ),
            point_type=point_type,
            side=side,
            status=StrictPointStatus.APPROACHING,
            variant=variant,
            structural_level=center.structural_level,
            source_kind=center.source_kind,
            price_basis_revision=center.price_basis_revision,
            anchor_unit_id=tail.unit_id,
            anchor_at=tail.market_end,
            confirmed_at=None,
            available_at=max(center.available_at, tail.available_at),
            price_quantum=price_quantum,
            anchor_tick=anchor_tick,
            invalidation_tick=invalidation_tick,
            center_id=center.center_id,
            center_zd_tick=center.zd_tick,
            center_zg_tick=center.zg_tick,
            center_ordinal=ordinals[(center.center_id, direction)],
            divergence=None,
            parent_point_id=None,
            evidence_codes=(
                "formal_center",
                "complete_leave",
                "live_first_return",
                "core_boundary_currently_held",
            ),
            missing_conditions=("terminal_unit_locked",),
        )
        previous = output.setdefault(point.point_id, point)
        if previous != point:
            raise ValueError("盘中三类点身份映射到冲突正式证据")
        break

    return tuple(
        sorted(
            output.values(),
            key=lambda point: (
                point.available_at,
                point.structural_level,
                point.point_type,
                point.point_id,
            ),
        )
    )


def approaching_third_class_point_ledger(
    structure: StrictStructureResult,
    *,
    price_quantum: Decimal,
) -> tuple[StrictPointEvidence, ...]:
    """从整棵严格结构生成唯一的盘中三类点账本。"""

    if not isinstance(structure, StrictStructureResult):
        raise TypeError("盘中三类点账本需要严格结构")
    output: dict[str, StrictPointEvidence] = {}
    for level in structure.levels:
        for point in approaching_third_class_points(
            level,
            price_quantum=price_quantum,
        ):
            previous = output.setdefault(point.point_id, point)
            if previous != point:
                raise ValueError("盘中三类点身份映射到冲突结构证据")
    return tuple(
        sorted(
            output.values(),
            key=lambda point: (
                point.available_at,
                point.structural_level,
                point.point_type,
                point.point_id,
            ),
        )
    )


__all__ = (
    "approaching_third_class_point_ledger",
    "approaching_third_class_points",
    "build_approaching_point_id",
    "center_ordinals",
    "classify_third_class_geometry",
    "preview_center_ordinal",
)
