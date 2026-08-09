from __future__ import annotations

from dataclasses import dataclass

from chanlun.core.strict_structure.center_machine import validate_unit_sequence
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind


@dataclass(frozen=True, slots=True)
class SameLevelCombination:
    """Audit evidence for one associative same-direction combination."""

    combined_unit_id: str
    child_unit_ids: tuple[str, ...]
    direction: str
    protected_after_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SameLevelDecompositionResult:
    """A deterministic same-level decomposition ready for recursion."""

    units: tuple[ConstituentUnit, ...]
    oscillatory_ids: frozenset[str]
    combinations: tuple[SameLevelCombination, ...]


def _validate_source_chain(
    values: tuple[ConstituentUnit, ...],
    oscillatory_ids: frozenset[str],
) -> None:
    """Validate every invariant except direction alternation.

    Direction cannot be checked until adjacent same-direction trends have been
    combined.  All other causal and identity constraints remain fail-closed.
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
        leaf_id
        for item in group
        for leaf_id in _combination_leaves(item)
    )
    inherited_protected = {
        leaf_id
        for item in group
        for leaf_id in item.protected_after_ids
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
            "chanlun-same-level-combination/v1",
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
    )


def combine_same_level_trends(
    units: tuple[ConstituentUnit, ...],
    oscillatory_ids: frozenset[str],
    protected_after_ids: frozenset[str] = frozenset(),
) -> SameLevelDecompositionResult:
    """Apply the same-level combination law before recursive center building.

    Consecutive directional trend types with the same direction are one trend
    under the associative combination law.  Consolidations are directionless
    connectors for this purpose and are deliberately never merged, so
    ``trend + consolidation + trend`` and ``consolidation + consolidation``
    remain observable decompositions.  Confirmed divergence edges belong to
    the fixed same-level ledger.  If the higher-level associative carrier must
    combine adjacent same-direction trends, ``SameLevelCombination`` retains
    every protected child edge instead of erasing its provenance.

    The output is deterministic and prefix-causal: a run is combined only from
    already locked source trend types, and the composite becomes available no
    earlier than its latest child.  Its ``child_ids`` retain the complete
    one-level provenance used by replay and audit code.
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
        if current.unit_id in oscillatory:
            output.append(current)
            index += 1
            continue

        end = index + 1
        while end < len(values):
            candidate = values[end]
            if candidate.unit_id in oscillatory:
                break
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
    result_oscillatory = frozenset(
        item.unit_id for item in result_units if item.unit_id in oscillatory
    )
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
