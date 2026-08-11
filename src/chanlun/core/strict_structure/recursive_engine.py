from __future__ import annotations

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
    validate_unit_sequence,
)
from chanlun.core.strict_structure.models import (
    CenterState,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
    TrendAssemblyResult,
    TrendKind,
    TrendState,
    center_seed_size,
)
from chanlun.core.strict_structure.same_level_decomposition import (
    combine_same_level_trends,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit


def calculate_level_with_divergence_boundaries(
    units,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str] = frozenset(),
    *,
    strength=None,
):
    """Calculate one fixed same-level ledger to its causal boundary fixed point."""

    values = tuple(units)
    unit_index = {item.unit_id: index for index, item in enumerate(values)}
    if strength is None:
        center_result = calculate_centers(
            values,
            structural_level,
            source_kind,
            oscillatory_ids,
        )
        return center_result, assemble_trend_types(
            center_result.centers,
            values,
            structural_level,
            oscillatory_ids,
        )
    accepted: frozenset[str] = frozenset()
    closed_centers = {}
    frozen_current = {}
    frozen_completed = {}
    frozen_boundaries = {}
    last_boundary_index = -1

    def merge_unique(target, items, identifier, label):
        for item in items:
            item_id = getattr(item, identifier)
            previous = target.setdefault(item_id, item)
            if previous != item:
                raise ValueError(f"{label} id maps to conflicting evidence")

    locked_count = 0
    for item in values:
        if not item.locked:
            break
        locked_count += 1

    for _pass in range(len(values) + 1):
        discovered = None
        # A departure can later be folded back into its center body.  Replay
        # locked prefixes so the first confirmed divergence remains an
        # immutable boundary in batch, incremental, and restart calculations.
        for prefix_end in range(last_boundary_index + 1, locked_count):
            prefix = values[: prefix_end + 1]
            prefix_centers = calculate_centers(
                prefix,
                structural_level,
                source_kind,
                oscillatory_ids,
                hard_boundary_after_ids=accepted,
                boundary_closed_centers=tuple(closed_centers.values()),
            )
            centers_for_trends = tuple(
                center
                for index, center in enumerate(prefix_centers.centers)
                if unit_index[center.entry_unit.unit_id] > last_boundary_index
                and (
                    center.state is CenterState.COMPLETED
                    or index == len(prefix_centers.centers) - 1
                )
            )
            suffix = assemble_trend_types(
                centers_for_trends,
                prefix,
                structural_level,
                oscillatory_ids,
                strength=strength,
                group_start_unit_id=(
                    None
                    if last_boundary_index < 0
                    else values[last_boundary_index + 1].unit_id
                ),
            )
            candidates = tuple(
                boundary
                for boundary in suffix.decomposition_boundaries
                if unit_index[boundary.anchor_unit_id] > last_boundary_index
            )
            if candidates:
                discovered = (
                    min(
                        candidates,
                        key=lambda boundary: (
                            unit_index[boundary.anchor_unit_id],
                            boundary.available_at,
                            boundary.boundary_id,
                        ),
                    ),
                    suffix,
                )
                break

        if discovered is None:
            center_result = calculate_centers(
                values,
                structural_level,
                source_kind,
                oscillatory_ids,
                hard_boundary_after_ids=accepted,
                boundary_closed_centers=tuple(closed_centers.values()),
            )
            centers_for_trends = tuple(
                center
                for index, center in enumerate(center_result.centers)
                if unit_index[center.entry_unit.unit_id] > last_boundary_index
                and (
                    center.state is CenterState.COMPLETED
                    or index == len(center_result.centers) - 1
                )
            )
            suffix = assemble_trend_types(
                centers_for_trends,
                values,
                structural_level,
                oscillatory_ids,
                strength=strength,
                group_start_unit_id=(
                    None
                    if last_boundary_index < 0 or last_boundary_index + 1 >= len(values)
                    else values[last_boundary_index + 1].unit_id
                ),
            )
            merged_current = dict(frozen_current)
            merge_unique(
                merged_current,
                suffix.current_trends,
                "trend_id",
                "current trend",
            )
            merged_completed = dict(frozen_completed)
            merge_unique(
                merged_completed,
                suffix.completed_trends,
                "trend_id",
                "completed trend",
            )
            return center_result, TrendAssemblyResult(
                current_trends=tuple(merged_current.values()),
                completed_trends=tuple(
                    sorted(
                        merged_completed.values(),
                        key=lambda trend: (trend.available_at, trend.trend_id),
                    )
                ),
                decomposition_boundaries=tuple(
                    sorted(
                        frozen_boundaries.values(),
                        key=lambda boundary: (
                            boundary.available_at,
                            boundary.boundary_id,
                        ),
                    )
                ),
            )

        candidate, suffix = discovered
        current_ids = [trend.trend_id for trend in suffix.current_trends]
        try:
            boundary_trend_index = current_ids.index(candidate.left_trend_id)
        except ValueError as exc:
            raise ValueError("boundary trend is missing from suffix assembly") from exc
        merge_unique(
            frozen_current,
            suffix.current_trends[: boundary_trend_index + 1],
            "trend_id",
            "current trend",
        )
        merge_unique(
            frozen_completed,
            tuple(
                trend
                for trend in suffix.completed_trends
                if trend.market_end <= candidate.anchor_at
                and (
                    candidate.terminal_center_id
                    not in {center.center_id for center in trend.centers}
                    or trend.terminal_divergence == candidate.divergence
                )
            ),
            "trend_id",
            "completed trend",
        )
        merge_unique(
            frozen_boundaries,
            (candidate,),
            "boundary_id",
            "decomposition boundary",
        )
        accepted = accepted | {candidate.anchor_unit_id}
        boundary_trend = suffix.current_trends[boundary_trend_index]
        terminal_center = boundary_trend.centers[-1]
        if (
            terminal_center.center_id != candidate.terminal_center_id
            or terminal_center.state is not CenterState.DIVERGENCE_CLOSED
        ):
            raise ValueError("boundary terminal center is not causally closed")
        previous_center = closed_centers.setdefault(
            candidate.anchor_unit_id,
            terminal_center,
        )
        if previous_center != terminal_center:
            raise ValueError("boundary center identity collision")
        last_boundary_index = unit_index[candidate.anchor_unit_id]
    raise RuntimeError("center/divergence decomposition did not converge")


class StrictRecursiveEngine:
    def __init__(self, max_levels: int = 50) -> None:
        if type(max_levels) is not int or max_levels < 1:
            raise ValueError("max_levels must be >= 1")
        self.max_levels = max_levels

    def calculate(
        self,
        base_units,
        *,
        price_basis_revision: str | None = None,
        strength=None,
    ) -> StrictStructureResult:
        units = tuple(base_units)
        if units:
            inferred_basis = units[0].price_basis_revision
            if price_basis_revision is None:
                price_basis_revision = inferred_basis
            if price_basis_revision != inferred_basis or any(
                item.price_basis_revision != price_basis_revision for item in units
            ):
                raise ValueError("strict recursion cannot cross price basis")
        elif not price_basis_revision:
            raise ValueError("empty strict recursion requires price basis")

        levels = []
        # 线段层恒为空集：盘整只在走势类型层存在，因此 level 0 的行为与严格
        # 交替完全等价，本改动不触及线段与 level-0 线段中枢。
        oscillatory_ids: frozenset[str] = frozenset()
        for level in range(self.max_levels):
            source_kind = SourceKind.SEGMENT if level == 0 else SourceKind.TREND_TYPE
            validate_unit_sequence(units, level, source_kind, oscillatory_ids)
            # L0 consumes one immutable five-segment window: entry + middle
            # three + leave. Higher levels retain three completed lower-level
            # trends plus their auditable preceding trend entry.
            minimum_units = (
                center_seed_size(source_kind) + 1
                if source_kind is SourceKind.TREND_TYPE
                else 5
            )
            if len(units) < minimum_units:
                break

            # A confirmed divergence closes the current same-level movement.
            # Center geometry must then be replayed in partitions so no later
            # center can borrow units from both sides of that immutable edge.
            # Repeating to a fixed point also allows a newly exposed suffix to
            # reveal another confirmed boundary without future backfilling.
            center_result, assembly = calculate_level_with_divergence_boundaries(
                units,
                level,
                source_kind,
                oscillatory_ids,
                strength=strength,
            )
            levels.append(
                StrictLevelResult(
                    structural_level=level,
                    units=units,
                    center_result=center_result,
                    trend_types=assembly.current_trends,
                    completed_trends=assembly.completed_trends,
                    decomposition_boundaries=assembly.decomposition_boundaries,
                )
            )
            locked_trends = tuple(
                trend
                for trend in assembly.current_trends
                if trend.state is TrendState.LOCKED
            )
            next_units = tuple(trend_type_to_unit(trend) for trend in locked_trends)
            # 盘整走势类型没有方向。上一层把它按净位移记成 up/down，只是为了让
            # 单元有一个可比较的端点方向，不代表它在交替判定中算一条有向腿。
            next_oscillatory_ids = frozenset(
                unit.unit_id
                for unit, trend in zip(next_units, locked_trends)
                if trend.kind is TrendKind.CONSOLIDATION
            )
            # 同级别结合律先于高一级中枢计算：直接相邻的同向趋势
            # 合成一个可追溯复合单元，盘整则作为无方向连接件保留。这样
            # 「有了结合律，方向不是最重要的」不会被误读为取消方向约束。
            decomposition = combine_same_level_trends(
                next_units,
                next_oscillatory_ids,
                frozenset(
                    boundary.left_trend_id
                    for boundary in assembly.decomposition_boundaries
                ),
            )
            next_units = decomposition.units
            next_oscillatory_ids = decomposition.oscillatory_ids
            if len(next_units) < 3:
                break
            try:
                validate_unit_sequence(
                    next_units,
                    level + 1,
                    SourceKind.TREND_TYPE,
                    next_oscillatory_ids,
                )
            except ValueError:
                # 不交替、断链或跨 basis 的 LOCKED 走势不能被“挤”成
                # 高一级中枢；保留已完成的当前层并停止正式递归。
                break
            units = next_units
            oscillatory_ids = next_oscillatory_ids

        return StrictStructureResult(
            schema="chanlun-structure",
            price_basis_revision=price_basis_revision,
            levels=tuple(levels),
        )
