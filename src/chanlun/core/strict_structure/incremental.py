from __future__ import annotations

from chanlun.core.strict_structure.center_machine import calculate_centers
from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    ConstituentUnit,
    SourceKind,
)


class PrefixStabilityViolation(StrictStructureContractError):
    """Raised when evidence that was already locked is later rewritten."""


def _locked_signature(units: tuple[ConstituentUnit, ...]) -> tuple[tuple, ...]:
    output = []
    for item in units:
        if not item.locked:
            break
        output.append(
            (
                item.unit_id,
                item.structural_level,
                item.source_kind,
                item.price_basis_revision,
                item.direction,
                item.start_tick,
                item.end_tick,
                item.low_tick,
                item.high_tick,
                item.market_start,
                item.market_end,
                item.confirmed_at,
                item.available_at,
                item.child_ids,
            )
        )
    return tuple(output)


class IncrementalCenterEngine:
    """Causal facade whose reference implementation recomputes pure results."""

    def __init__(self, structural_level: int, source_kind: SourceKind) -> None:
        if type(structural_level) is not int or structural_level < 0:
            raise ValueError("structural_level must be >= 0")
        self.structural_level = structural_level
        self.source_kind = SourceKind(source_kind)
        self._locked_signature: tuple[tuple, ...] = ()

    def update(self, units) -> CenterLevelResult:
        values = tuple(units)
        signature = _locked_signature(values)
        old = self._locked_signature
        if len(signature) < len(old) or signature[: len(old)] != old:
            raise PrefixStabilityViolation("locked prefix changed")

        result = calculate_centers(
            values,
            self.structural_level,
            self.source_kind,
        )
        self._locked_signature = signature
        return result
