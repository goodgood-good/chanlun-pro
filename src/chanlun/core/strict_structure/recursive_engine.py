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
    TrendState,
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
        for level in range(self.max_levels):
            source_kind = (
                SourceKind.SEGMENT
                if level == 0
                else SourceKind.TREND_TYPE
            )
            validate_unit_sequence(units, level, source_kind)
            locked_count = 0
            for item in units:
                if not item.locked:
                    break
                locked_count += 1
            if locked_count < 5:
                break

            center_result = calculate_centers(units, level, source_kind)
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
            next_units = tuple(
                trend_type_to_unit(trend)
                for trend in assembly.current_trends
                if trend.state is TrendState.LOCKED
            )
            if len(next_units) < 5:
                break
            try:
                validate_unit_sequence(
                    next_units,
                    level + 1,
                    SourceKind.TREND_TYPE,
                )
            except ValueError:
                # 不交替、断链或跨 basis 的 LOCKED 走势不能被“挤”成
                # 高一级中枢；保留已完成的当前层并停止正式递归。
                break
            units = next_units

        return StrictStructureResult(
            schema_version="chanlun-structure/v3",
            price_basis_revision=price_basis_revision,
            levels=tuple(levels),
        )
