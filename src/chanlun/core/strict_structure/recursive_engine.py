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
    TrendKind,
    TrendState,
)
from chanlun.core.strict_structure.same_level_decomposition import (
    combine_same_level_trends,
)
from chanlun.core.strict_structure.trend_assembler import assemble_trend_types
from chanlun.core.strict_structure.unit_adapter import trend_type_to_unit


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
    ) -> StrictStructureResult:
        units = tuple(base_units)
        if units:
            inferred_basis = units[0].price_basis_revision
            if price_basis_revision is None:
                price_basis_revision = inferred_basis
            if price_basis_revision != inferred_basis or any(
                item.price_basis_revision != price_basis_revision
                for item in units
            ):
                raise ValueError("strict recursion cannot cross price basis")
        elif not price_basis_revision:
            raise ValueError("empty strict recursion requires price basis")

        levels = []
        # 线段层恒为空集：盘整只在走势类型层存在，因此 level 0 的行为与严格
        # 交替完全等价，本改动不触及线段与 level-0 线段中枢。
        oscillatory_ids: frozenset[str] = frozenset()
        for level in range(self.max_levels):
            source_kind = (
                SourceKind.SEGMENT
                if level == 0
                else SourceKind.TREND_TYPE
            )
            validate_unit_sequence(units, level, source_kind, oscillatory_ids)
            # Segment-sourced L0 keeps the existing five-unit confirmation
            # envelope.  Recursive levels consume already-complete lower
            # trends, so the original center definition is available after
            # exactly three such trends; retaining the old five-unit gate here
            # would silently delay or drop a valid higher-level center.
            minimum_units = (
                5 if source_kind is SourceKind.SEGMENT else 3
            )
            if len(units) < minimum_units:
                break

            center_result = calculate_centers(
                units, level, source_kind, oscillatory_ids
            )
            centers_for_trends = tuple(
                center
                for index, center in enumerate(center_result.centers)
                if center.state is CenterState.COMPLETED
                or index == len(center_result.centers) - 1
            )
            assembly = assemble_trend_types(
                centers_for_trends,
                units,
                level,
                oscillatory_ids,
            )
            levels.append(
                StrictLevelResult(
                    structural_level=level,
                    units=units,
                    center_result=center_result,
                    trend_types=assembly.current_trends,
                    completed_trends=assembly.completed_trends,
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
            schema_version="chanlun-structure/v3",
            price_basis_revision=price_basis_revision,
            levels=tuple(levels),
        )
