from __future__ import annotations

from dataclasses import dataclass

from chanlun.core.strict_structure.center_machine import validate_unit_sequence
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind


@dataclass(frozen=True, slots=True)
class SameLevelCombination:
    """一次同向结合律合并的审计证据。"""

    combined_unit_id: str
    child_unit_ids: tuple[str, ...]
    direction: str
    protected_after_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SameLevelDecompositionResult:
    """可直接递归的确定性同级别分解结果。"""

    units: tuple[ConstituentUnit, ...]
    oscillatory_ids: frozenset[str]
    combinations: tuple[SameLevelCombination, ...]


def _validate_source_chain(
    values: tuple[ConstituentUnit, ...],
    oscillatory_ids: frozenset[str],
) -> None:
    """验证除方向交替外的所有不变量。

    相邻同向走势完成合并前不能检查方向；其余因果和身份约束仍采用封闭失败。
    """

    if not values:
        if oscillatory_ids:
            raise ValueError("empty decomposition cannot declare oscillatory units")
        return
    if len({item.unit_id for item in values}) != len(values):
        raise ValueError("same-level source unit ids must be unique")
    if not oscillatory_ids.issubset({item.unit_id for item in values}):
        raise ValueError("oscillatory ids must reference source units")

    level = values[0].structural_level
    basis = values[0].price_basis_revision
    for item in values:
        if (
            item.structural_level != level
            or item.source_kind is not SourceKind.TREND_TYPE
        ):
            raise ValueError("same-level decomposition requires one trend-type level")
        if item.price_basis_revision != basis:
            raise ValueError("same-level decomposition cannot cross price basis")
        if not item.locked:
            raise ValueError("same-level decomposition requires locked trend types")

    for previous, current in zip(values, values[1:]):
        if previous.end_tick != current.start_tick:
            raise ValueError("same-level source prices must connect")
        if current.market_start < previous.market_end:
            raise ValueError("same-level source intervals must not overlap")


def _combination_leaves(item: ConstituentUnit) -> tuple[str, ...]:
    return item.child_ids if item.same_level_combination else (item.unit_id,)


def _combined_unit(
    group: tuple[ConstituentUnit, ...],
    protected_after_ids: frozenset[str],
) -> ConstituentUnit:
    first = group[0]
    last = group[-1]
    direction = first.direction
    child_ids = tuple(
        leaf_id for item in group for leaf_id in _combination_leaves(item)
    )
    inherited_protected = {
        leaf_id for item in group for leaf_id in item.protected_after_ids
    }
    explicit_protected = set()
    for item in group:
        leaves = _combination_leaves(item)
        explicit_protected.update(
            leaf_id for leaf_id in leaves if leaf_id in protected_after_ids
        )
        if item.unit_id in protected_after_ids:
            explicit_protected.add(leaves[-1])
    protected_leaves = tuple(
        leaf_id
        for leaf_id in child_ids
        if leaf_id in inherited_protected or leaf_id in explicit_protected
    )
    return ConstituentUnit(
        unit_id=stable_structure_id(
            "chanlun-same-level-combination",
            first.price_basis_revision,
            first.structural_level,
            direction,
            child_ids,
        ),
        structural_level=first.structural_level,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=first.price_basis_revision,
        direction=direction,
        start_tick=first.start_tick,
        end_tick=last.end_tick,
        low_tick=min(item.low_tick for item in group),
        high_tick=max(item.high_tick for item in group),
        market_start=first.market_start,
        market_end=last.market_end,
        confirmed_at=max(item.confirmed_at for item in group),
        available_at=max(item.available_at for item in group),
        locked=True,
        child_ids=child_ids,
        same_level_combination=True,
        protected_after_ids=protected_leaves,
        formed_at=max(item.formed_at or item.confirmed_at for item in group),
    )


def combine_same_level_trends(
    units: tuple[ConstituentUnit, ...],
    oscillatory_ids: frozenset[str],
    protected_after_ids: frozenset[str] = frozenset(),
) -> SameLevelDecompositionResult:
    """在递归构建中枢前应用同级别结合律。

    按结合律，连续同向的走势类型属于同一走势。盘整同样由首尾端点确定方向，
    不能作为同向走势之间的无方向连接件。``oscillatory_ids`` 只为读取旧回放调用
    保留，不再改变方向判定。``SameLevelCombination`` 会保留受保护子边界来源。

    输出具有确定性和前缀因果性：只合并已锁定的来源走势类型，组合结果不会早于
    最晚子单元可用；其 ``child_ids`` 保留回放与审计所需的完整单层来源。
    """

    values = tuple(units)
    oscillatory = frozenset(oscillatory_ids)
    protected = frozenset(protected_after_ids)
    _validate_source_chain(values, oscillatory)
    unit_ids = {item.unit_id for item in values}
    leaf_ids = tuple(
        leaf_id for item in values for leaf_id in _combination_leaves(item)
    )
    if len(set(leaf_ids)) != len(leaf_ids):
        raise ValueError("same-level combination leaves must not overlap")
    if not protected.issubset(unit_ids | set(leaf_ids)):
        raise ValueError("protected boundary ids must reference source units")
    if not values:
        return SameLevelDecompositionResult((), frozenset(), ())

    output: list[ConstituentUnit] = []
    combinations: list[SameLevelCombination] = []
    index = 0
    while index < len(values):
        current = values[index]
        end = index + 1
        while end < len(values):
            candidate = values[end]
            if candidate.direction != current.direction:
                break
            end += 1

        group = values[index:end]
        if len(group) == 1:
            output.append(current)
        else:
            combined = _combined_unit(group, protected)
            output.append(combined)
            combinations.append(
                SameLevelCombination(
                    combined_unit_id=combined.unit_id,
                    child_unit_ids=combined.child_ids,
                    direction=combined.direction,
                    protected_after_ids=combined.protected_after_ids,
                )
            )
        index = end

    result_units = tuple(output)
    # Consolidation is a movement classification, not a directionless bridge.
    # All same-direction runs have already been combined above.
    result_oscillatory = frozenset()
    validate_unit_sequence(
        result_units,
        result_units[0].structural_level,
        SourceKind.TREND_TYPE,
        result_oscillatory,
    )
    return SameLevelDecompositionResult(
        units=result_units,
        oscillatory_ids=result_oscillatory,
        combinations=tuple(combinations),
    )
