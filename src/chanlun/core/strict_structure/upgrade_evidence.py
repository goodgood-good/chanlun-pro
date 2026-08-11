from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import (
    CenterState,
    StrictStructureResult,
    TrendCenter,
)


class UpgradeEvidenceKind(str, Enum):
    NINE_SEGMENT_DERIVATION = "nine_segment_derivation"
    CENTER_EXPANSION = "center_expansion"


class UpgradeEvidenceStatus(str, Enum):
    CONFIRMED_DERIVED_CENTER = "confirmed_derived_center"
    EXPANSION_RECLASSIFYING = "expansion_reclassifying"


@dataclass(frozen=True, slots=True)
class RecursiveUpgradeEvidence:
    """Causal evidence that a source level is upgrading one level higher.

    This object is deliberately separate from :class:`TrendCenter`.  A
    nine-segment derivation is a higher-level center context, while an
    expansion pair is only a reclassification warning.  Neither may silently
    enter the ordinary strict-point signal channel.
    """

    evidence_id: str
    kind: UpgradeEvidenceKind
    status: UpgradeEvidenceStatus
    source_level: int
    target_level: int
    price_basis_revision: str
    source_center_ids: tuple[str, ...]
    source_unit_ids: tuple[str, ...]
    extension_unit_ids: tuple[str, ...]
    zd_tick: int
    zg_tick: int
    dd_tick: int
    gg_tick: int
    market_start: datetime
    market_end: datetime
    available_at: datetime
    resolved_by_standard_center_id: str | None = None
    signal_eligible: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", UpgradeEvidenceKind(self.kind))
        object.__setattr__(self, "status", UpgradeEvidenceStatus(self.status))
        object.__setattr__(self, "source_center_ids", tuple(self.source_center_ids))
        object.__setattr__(self, "source_unit_ids", tuple(self.source_unit_ids))
        object.__setattr__(self, "extension_unit_ids", tuple(self.extension_unit_ids))
        if not self.evidence_id:
            raise ValueError("upgrade evidence id is required")
        if self.source_level < 0 or self.target_level != self.source_level + 1:
            raise ValueError("upgrade evidence levels must be adjacent")
        if not self.price_basis_revision:
            raise ValueError("upgrade evidence price basis is required")
        if not self.source_center_ids or len(self.source_center_ids) != len(
            set(self.source_center_ids)
        ):
            raise ValueError("upgrade evidence requires unique source centers")
        if not self.source_unit_ids or len(self.source_unit_ids) != len(
            set(self.source_unit_ids)
        ):
            raise ValueError("upgrade evidence requires unique source units")
        if set(self.source_unit_ids) & set(self.extension_unit_ids):
            raise ValueError("establishing and extension evidence must be disjoint")
        if self.zd_tick > self.zg_tick:
            raise ValueError("upgrade evidence core must not be inverted")
        if self.dd_tick > self.zd_tick or self.gg_tick < self.zg_tick:
            raise ValueError("upgrade evidence envelope must contain its core")
        if self.market_end < self.market_start:
            raise ValueError("upgrade evidence market interval is inverted")
        if self.available_at < self.market_end:
            raise ValueError("upgrade evidence cannot be available before its market end")
        if self.signal_eligible:
            raise ValueError("upgrade evidence is context-only and never signal eligible")
        if (
            self.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
            and len(self.source_center_ids) != 1
        ):
            raise ValueError("nine-segment evidence requires one source center")
        if (
            self.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
            and len(self.source_unit_ids) != 9
        ):
            raise ValueError("nine-segment evidence requires exactly nine establishing units")
        if (
            self.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
            and self.status is not UpgradeEvidenceStatus.CONFIRMED_DERIVED_CENTER
        ):
            raise ValueError("nine-segment evidence must be confirmed")
        if (
            self.kind is UpgradeEvidenceKind.CENTER_EXPANSION
            and len(self.source_center_ids) != 2
        ):
            raise ValueError("expansion evidence requires an adjacent center pair")
        if (
            self.kind is UpgradeEvidenceKind.CENTER_EXPANSION
            and self.status is not UpgradeEvidenceStatus.EXPANSION_RECLASSIFYING
        ):
            raise ValueError("center expansion remains a reclassification state")


def _center_touch_units(center: TrendCenter):
    """Return only the lower-level movements that actually touch the core.

    All source kinds use three components as the frozen center core.  Failed
    departures and their re-entry legs are folded into ``body_units``;
    successful pending/completion leaves remain external and therefore need
    no positional exclusion here.
    """

    return center.body_units


def _resolved_standard_center(
    source: TrendCenter,
    target_centers: tuple[TrendCenter, ...],
) -> str | None:
    """Link, but never duplicate, a naturally-recursed higher center."""

    touch_units = _center_touch_units(source)
    if len(touch_units) < 9:
        return None
    market_start = touch_units[0].market_start
    market_end = touch_units[8].market_end
    for target in target_centers:
        if (
            target.body_start_market_time <= market_start
            and target.last_touch_market_time >= market_end
        ):
            return target.center_id
    return None


def _nine_segment_evidence(
    center: TrendCenter,
    *,
    target_centers: tuple[TrendCenter, ...],
) -> RecursiveUpgradeEvidence | None:
    if center.state is not CenterState.COMPLETED:
        return None
    # ``body_units`` already contains only center-touching movements.  Entry,
    # successful leave and first return are external lifecycle evidence.
    touch_units = _center_touch_units(center)
    if len(touch_units) < 9:
        return None
    establishing = touch_units[:9]
    extension = touch_units[9:]
    groups = tuple(establishing[offset : offset + 3] for offset in (0, 3, 6))
    group_lows = tuple(min(item.low_tick for item in group) for group in groups)
    group_highs = tuple(max(item.high_tick for item in group) for group in groups)
    zd_tick = max(group_lows)
    zg_tick = min(group_highs)
    if zd_tick > zg_tick:
        # Fail closed if the alleged three derived consolidations do not have
        # a common overlap.  A long body alone must never manufacture a center.
        return None
    source_ids = tuple(item.unit_id for item in establishing)
    extension_ids = tuple(item.unit_id for item in extension)
    available_at = max(
        center.available_at,
        *(item.available_at for item in establishing),
    )
    # A confirmed nine-segment derivation is append-only evidence.  A standard
    # higher-level center discovered later may describe the same geometry, but
    # must not retroactively mutate this historical evidence object.  Record a
    # resolution link only when that target was already observable when the
    # derivation itself first became available.
    resolved = _resolved_standard_center(
        center,
        tuple(
            target
            for target in target_centers
            if target.available_at <= available_at
        ),
    )
    evidence_id = stable_structure_id(
        "chanlun-nine-segment-upgrade",
        center.price_basis_revision,
        center.structural_level,
        center.center_id,
        source_ids,
        zd_tick,
        zg_tick,
    )
    return RecursiveUpgradeEvidence(
        evidence_id=evidence_id,
        kind=UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION,
        status=UpgradeEvidenceStatus.CONFIRMED_DERIVED_CENTER,
        source_level=center.structural_level,
        target_level=center.structural_level + 1,
        price_basis_revision=center.price_basis_revision,
        source_center_ids=(center.center_id,),
        source_unit_ids=source_ids,
        extension_unit_ids=extension_ids,
        zd_tick=zd_tick,
        zg_tick=zg_tick,
        dd_tick=min(item.low_tick for item in touch_units),
        gg_tick=max(item.high_tick for item in touch_units),
        market_start=establishing[0].market_start,
        market_end=establishing[-1].market_end,
        # A completed source center is the conservative immutable boundary for
        # a derived context.  This never back-dates the upgrade to its ninth leg.
        available_at=available_at,
        resolved_by_standard_center_id=resolved,
        signal_eligible=False,
    )


def _expansion_pair(
    previous: TrendCenter,
    current: TrendCenter,
) -> RecursiveUpgradeEvidence | None:
    if (
        previous.state is not CenterState.COMPLETED
        or current.state is not CenterState.COMPLETED
    ):
        return None
    core_separated = (
        current.zd_tick > previous.zg_tick
        or current.zg_tick < previous.zd_tick
    )
    if not core_separated:
        return None
    lo = max(previous.dd_tick, current.dd_tick)
    hi = min(previous.gg_tick, current.gg_tick)
    if lo > hi:
        return None
    source_unit_ids = tuple(
        dict.fromkeys(
            item.unit_id
            for center in (previous, current)
            for item in center.body_units
        )
    )
    evidence_id = stable_structure_id(
        "chanlun-center-expansion",
        previous.price_basis_revision,
        previous.structural_level,
        previous.center_id,
        current.center_id,
        lo,
        hi,
    )
    return RecursiveUpgradeEvidence(
        evidence_id=evidence_id,
        kind=UpgradeEvidenceKind.CENTER_EXPANSION,
        status=UpgradeEvidenceStatus.EXPANSION_RECLASSIFYING,
        source_level=previous.structural_level,
        target_level=previous.structural_level + 1,
        price_basis_revision=previous.price_basis_revision,
        source_center_ids=(previous.center_id, current.center_id),
        source_unit_ids=source_unit_ids,
        extension_unit_ids=(),
        zd_tick=lo,
        zg_tick=hi,
        dd_tick=min(previous.dd_tick, current.dd_tick),
        gg_tick=max(previous.gg_tick, current.gg_tick),
        market_start=previous.body_start_market_time,
        market_end=current.last_touch_market_time,
        available_at=max(previous.available_at, current.available_at),
        resolved_by_standard_center_id=None,
        signal_eligible=False,
    )


def collect_recursive_upgrade_evidence(
    structure: StrictStructureResult,
    *,
    as_of: datetime | None = None,
) -> tuple[RecursiveUpgradeEvidence, ...]:
    """Collect the upgrade context visible at one causal snapshot.

    ``as_of=None`` means the final structure snapshot.  Supplying ``as_of``
    filters immutable completed centers by their own availability before both
    nine-segment derivation and the tail-only expansion state are evaluated.
    No future center is allowed to resolve or create historical context.
    """

    if as_of is not None and (as_of.tzinfo is None or as_of.utcoffset() is None):
        raise ValueError("upgrade evidence as_of must be timezone-aware")

    output: list[RecursiveUpgradeEvidence] = []
    levels = structure.levels
    for index, level in enumerate(levels):
        target_centers = (
            tuple(
                center
                for center in levels[index + 1].center_result.centers
                if center.state is CenterState.COMPLETED
                and (as_of is None or center.available_at <= as_of)
            )
            if index + 1 < len(levels)
            else ()
        )
        completed = tuple(
            center
            for center in level.center_result.centers
            if center.state is CenterState.COMPLETED
            and (as_of is None or center.available_at <= as_of)
        )
        for center in completed:
            evidence = _nine_segment_evidence(
                center,
                target_centers=target_centers,
            )
            if evidence is not None:
                output.append(evidence)
        # Expansion is a present-tense reclassification state.  On a full
        # snapshot only the final adjacent pair can still be forming; older
        # pairs were states of historical prefixes and must be recovered by
        # causal replay, not left permanently active at the final timestamp.
        for previous, current in (
            ((completed[-2], completed[-1]),) if len(completed) >= 2 else ()
        ):
            evidence = _expansion_pair(previous, current)
            if evidence is not None:
                output.append(evidence)
    return tuple(
        sorted(
            output,
            key=lambda item: (
                item.available_at,
                item.source_level,
                item.kind.value,
                item.evidence_id,
            ),
        )
    )


__all__ = (
    "RecursiveUpgradeEvidence",
    "UpgradeEvidenceKind",
    "UpgradeEvidenceStatus",
    "collect_recursive_upgrade_evidence",
)
