from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingPointEvidenceFacts,
    RiskMappingSupplyFacts,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupConvergenceEnvelope,
    WarmupMappingSupplySnapshot,
    WarmupPeriodSemanticFacts,
    WarmupPrefixObservation,
    WarmupSemanticSnapshot,
    bind_warmup_convergence_diagnostic,
    bind_warmup_mapping_supply_diagnostic,
    classify_warmup_convergence_envelope,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
    WarmupStructureCenterFacts,
    WarmupStructureLineFacts,
    WarmupStructureLineageDiagnosticEnvelope,
    WarmupStructureLineageSnapshot,
    WarmupStructureLineageSnapshotSet,
    WarmupStructurePointLineageFacts,
    bind_warmup_structure_lineage_diagnostic,
    capture_warmup_structure_lineage_snapshot,
)
from tests.trading_system.strict_helpers import strict_evidence_result, strict_point


AS_OF = datetime.fromisoformat("2026-06-01T11:30:00+08:00")
PARAMETERS = "sha256:" + "1" * 64
SYMBOL = "SH.000001"
FREQUENCY = "d"


def _line(
    *,
    ordinal: int,
    offset: int,
    as_of: datetime = AS_OF,
) -> WarmupStructureLineFacts:
    start = as_of - timedelta(days=180 - offset * 4)
    end = start + timedelta(days=3)
    direction = "up" if offset % 2 == 0 else "down"
    start_value = Decimal(100 + offset)
    end_value = start_value + (Decimal("5") if direction == "up" else Decimal("-5"))
    locked = end + timedelta(days=1)
    identity = WarmupStructureLineFacts.identity(
        source_symbol=SYMBOL,
        source_frequency=FREQUENCY,
        source_kind="SEGMENT",
        direction=direction,
        start_at=start,
        end_at=end,
        start_value=start_value,
        end_value=end_value,
        locked_at=locked,
        completed=True,
    )
    return WarmupStructureLineFacts(
        line_id=identity,
        source_kind="SEGMENT",
        ordinal=ordinal,
        direction=direction,
        start_at=start,
        end_at=end,
        start_value=start_value,
        end_value=end_value,
        locked_at=locked,
        completed=True,
    )


def _center(
    *,
    index: int,
    direction: str,
    entry: WarmupStructureLineFacts,
    constituents: tuple[WarmupStructureLineFacts, ...],
) -> WarmupStructureCenterFacts:
    center_id = stable_structure_id(
        "test-strict-center",
        FREQUENCY,
        index,
        entry.line_id,
        tuple(value.line_id for value in constituents),
        direction,
    )
    return WarmupStructureCenterFacts(
        center_id=center_id,
        source_kind="SEGMENT",
        level_rank=0,
        center_index=index,
        direction=direction,
        start_at=entry.start_at,
        end_at=constituents[-1].end_at,
        core_low=Decimal("90"),
        core_high=Decimal("110"),
        range_low=Decimal("80"),
        range_high=Decimal("120"),
        completed=True,
        real=True,
        expanded=False,
        entry_line_id=entry.line_id,
        constituent_line_ids=tuple(value.line_id for value in constituents),
    )


def _point(
    *,
    center: WarmupStructureCenterFacts,
    trigger: WarmupStructureLineFacts,
    point_type: str,
    highest: bool,
) -> RiskMappingPointEvidenceFacts:
    assert trigger.locked_at is not None
    point_id = RiskMappingPointEvidenceFacts.identity(
        source_symbol=SYMBOL,
        source_frequency=FREQUENCY,
        center_id=center.center_id,
        center_level_rank=0,
        point_type=point_type,
        point_anchor_at=trigger.end_at,
        point_available_at=trigger.locked_at,
    )
    return RiskMappingPointEvidenceFacts(
        point_id=point_id,
        source_symbol=SYMBOL,
        source_frequency=FREQUENCY,
        center_id=center.center_id,
        center_level_rank=0,
        center_completed=True,
        center_expanded=False,
        point_type=point_type,  # type: ignore[arg-type]
        point_anchor_at=trigger.end_at,
        point_available_at=trigger.locked_at,
        inside_active_top_interval=True,
        highest_mapping_candidate=highest,
    )


def _supply(point: RiskMappingPointEvidenceFacts) -> RiskMappingSupplyFacts:
    unique = point.point_type == "1sell"
    return RiskMappingSupplyFacts(
        classification="UNIQUE_MAPPING" if unique else "ONLY_THIRD_CLASS_POINTS",
        lower_structure_available=True,
        point_evidence_count=1,
        point_type_counts=(
            ("1sell", int(unique)),
            ("2sell", 0),
            ("3sell", 0),
            ("3buy", int(not unique)),
        ),
        completed_sell12_count=int(unique),
        in_top_interval_sell12_count=int(unique),
        completed_in_top_interval_sell12_count=int(unique),
        incomplete_in_top_interval_sell12_count=0,
        outside_top_interval_sell12_count=0,
        highest_candidate_center_count=int(unique),
        point_evidence=(point,),
        diagnostic_buy_point_type_counts=(("1buy", 0), ("2buy", 0)),
        diagnostic_buy_point_evidence=(),
    )


def _semantic(
    *,
    center: WarmupStructureCenterFacts | None,
    as_of: datetime = AS_OF,
) -> WarmupSemanticSnapshot:
    return WarmupSemanticSnapshot(
        periods=tuple(
            WarmupPeriodSemanticFacts(
                period=period,
                state=(
                    "FORMED"
                    if period == "W" and center is not None
                    else "FORMED_UNRESOLVED"
                    if period == "W"
                    else "NONE"
                ),
                evidence_bar_end=as_of - timedelta(days=3),
                active_top_interval=(
                    (as_of - timedelta(days=60), as_of - timedelta(days=2))
                    if period == "W"
                    else None
                ),
                mapping_unique=(period != "W" or center is not None),
                mapped_center_id=(
                    center.center_id if period == "W" and center is not None else None
                ),
                mapping_candidate_ids=(
                    (center.center_id,)
                    if period == "W" and center is not None
                    else ()
                ),
                blocker_codes=(
                    ("NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",)
                    if period == "W" and center is None
                    else ()
                ),
                warning_codes=(),
            )
            for period in ("M", "W", "D")
        ),
        ma5=(("M", Decimal("8")), ("W", Decimal("9")), ("D", Decimal("10"))),
    )


def _snapshot(
    *,
    bar_count: int,
    lines: tuple[WarmupStructureLineFacts, ...],
    center: WarmupStructureCenterFacts,
    point: RiskMappingPointEvidenceFacts,
    trigger: WarmupStructureLineFacts,
) -> WarmupStructureLineageSnapshot:
    return WarmupStructureLineageSnapshot(
        period="W",
        source_symbol=SYMBOL,
        source_frequency=FREQUENCY,
        source_bar_count=bar_count,
        source_start_at=lines[0].start_at,
        source_end_at=lines[-1].end_at,
        source_content_sha256=sha256_json(
            {"bar_count": bar_count, "first": lines[0].line_id}
        ),
        lines=lines,
        centers=(center,),
        points=(
            WarmupStructurePointLineageFacts(
                point=point,
                trigger_line_id=trigger.line_id,
            ),
        ),
    )


def lineage_envelope(
    *,
    as_of: datetime = AS_OF,
    parameter_set_id: str = PARAMETERS,
) -> WarmupConvergenceEnvelope:
    leading = tuple(
        _line(ordinal=index, offset=index, as_of=as_of) for index in range(3)
    )
    common_prefix = tuple(
        _line(ordinal=index, offset=index + 3, as_of=as_of)
        for index in range(9)
    )
    # Stable line identities deliberately exclude prefix-local ordinals.
    common_reference = tuple(
        WarmupStructureLineFacts(
            line_id=value.line_id,
            source_kind=value.source_kind,
            ordinal=index + len(leading),
            direction=value.direction,
            start_at=value.start_at,
            end_at=value.end_at,
            start_value=value.start_value,
            end_value=value.end_value,
            locked_at=value.locked_at,
            completed=value.completed,
        )
        for index, value in enumerate(common_prefix)
    )
    reference_lines = leading + common_reference
    prefix_center = _center(
        index=3,
        direction="down",
        entry=common_prefix[0],
        constituents=common_prefix[:8],
    )
    reference_center = _center(
        index=5,
        direction="up",
        entry=common_reference[0],
        constituents=common_reference[1:9],
    )
    prefix_point = _point(
        center=prefix_center,
        trigger=common_prefix[8],
        point_type="1sell",
        highest=True,
    )
    reference_point = _point(
        center=reference_center,
        trigger=common_reference[8],
        point_type="3buy",
        highest=False,
    )
    prefix_supply = _supply(prefix_point)
    reference_supply = _supply(reference_point)
    reference_semantic = _semantic(center=None, as_of=as_of)
    prefix_semantic = _semantic(center=prefix_center, as_of=as_of)
    semantic_values = (reference_semantic, prefix_semantic, reference_semantic)
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=count,
            starts_at=as_of - timedelta(days=count),
            signature_sha256=snapshot.signature_sha256,
        )
        for count, snapshot in zip((480, 620, 744), semantic_values)
    )
    envelope = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=as_of,
        parameter_set_id=parameter_set_id,
        observations=observations,
    )
    envelope = bind_warmup_convergence_diagnostic(
        envelope, snapshots=semantic_values
    )
    envelope = bind_warmup_mapping_supply_diagnostic(
        envelope,
        snapshots=(
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", reference_supply), ("D", None))
            ),
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", prefix_supply), ("D", None))
            ),
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", reference_supply), ("D", None))
            ),
        ),
    )
    prefix_snapshot = _snapshot(
        bar_count=620,
        lines=common_prefix,
        center=prefix_center,
        point=prefix_point,
        trigger=common_prefix[8],
    )
    reference_snapshot = _snapshot(
        bar_count=744,
        lines=reference_lines,
        center=reference_center,
        point=reference_point,
        trigger=common_reference[8],
    )
    empty = WarmupStructureLineageSnapshotSet(
        periods=(("M", None), ("W", reference_snapshot), ("D", None))
    )
    return bind_warmup_structure_lineage_diagnostic(
        envelope,
        snapshots=(
            empty,
            WarmupStructureLineageSnapshotSet(
                periods=(("M", None), ("W", prefix_snapshot), ("D", None))
            ),
            empty,
        ),
    )


def test_structure_lineage_explains_sell_trigger_absorption() -> None:
    envelope = lineage_envelope()
    diagnostic = envelope.structure_lineage_diagnostic
    assert diagnostic is not None
    assert diagnostic.contract_id == WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
    comparison = diagnostic.comparisons[0].document()
    assert comparison["delta"]["transition_codes"] == [
        "LOWER_LINE_COMMON_SUFFIX_IDENTICAL",
        "SHORTER_LINE_SEQUENCE_IS_REFERENCE_SUFFIX",
        "CENTER_PARTITION_CHANGED_WITH_IDENTICAL_COMMON_LINES",
        "CENTER_CORE_RETAINED_WITH_ONE_LINE_PHASE_SHIFT",
        "LOST_SELL_TRIGGER_LINE_ABSORBED_INTO_REFERENCE_CENTER",
        "POINT_TRIGGER_ROLE_CHANGED_WITH_LONGER_HISTORY",
    ]
    role = comparison["delta"]["point_trigger_role_changes"][0]
    assert role["same_core_interval"] is True
    assert role["one_line_phase_shift"] is True
    assert role["shared_constituent_line_count"] == 7
    assert role["union_constituent_line_count"] == 9
    assert role["prefix_trigger_role"] == "AFTER_CENTER"
    assert role["reference_trigger_role"] == "CENTER_CONSTITUENT"


def test_structure_lineage_round_trip_preserves_prior_hashes() -> None:
    envelope = lineage_envelope()
    diagnostic = envelope.structure_lineage_diagnostic
    assert diagnostic is not None
    prior = deepcopy(diagnostic.document())
    restored = WarmupStructureLineageDiagnosticEnvelope.from_document(prior)
    restored.validate_against(envelope)
    assert restored == diagnostic

    without_lineage = envelope.__class__(
        frequency=envelope.frequency,
        as_of=envelope.as_of,
        parameter_set_id=envelope.parameter_set_id,
        observations=envelope.observations,
        status=envelope.status,
        stable_all_prefixes=envelope.stable_all_prefixes,
        reference_signature_sha256=envelope.reference_signature_sha256,
        match_longest_pattern=envelope.match_longest_pattern,
        reason_codes=envelope.reason_codes,
        diagnostic=envelope.diagnostic,
        mapping_supply_diagnostic=envelope.mapping_supply_diagnostic,
    )
    assert envelope.document() == without_lineage.document()
    assert envelope.content_sha256 == without_lineage.content_sha256
    assert envelope.diagnostic.content_sha256 == without_lineage.diagnostic.content_sha256
    assert (
        envelope.mapping_supply_diagnostic.content_sha256
        == without_lineage.mapping_supply_diagnostic.content_sha256
    )


def test_rehashed_structure_lineage_derived_tamper_is_rejected() -> None:
    envelope = lineage_envelope()
    diagnostic = envelope.structure_lineage_diagnostic
    assert diagnostic is not None
    document = deepcopy(diagnostic.document())
    document["comparisons"][0]["delta"]["transition_codes"] = [
        "STRUCTURE_LINEAGE_NOT_RECORDED"
    ]
    stable = dict(document)
    stable.pop("content_sha256")
    document["content_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError, match="malformed"):
        WarmupStructureLineageDiagnosticEnvelope.from_document(document)


def test_capture_keeps_first_center_entry_absent_without_fabricating_a_line() -> None:
    evidence = strict_evidence_result(
        code=SYMBOL,
        source_frequency=FREQUENCY,
        confirmed_points=(strict_point("3buy"),),
    )
    center = evidence.structure.levels[0].center_result.centers[0]
    assert center.entry_unit is None
    point = evidence.confirmed_points[0]
    anchor_at = normalize_datetime(point.anchor_at, "point_anchor_at")
    available_at = normalize_datetime(point.available_at, "point_available_at")
    mapping_point = RiskMappingPointEvidenceFacts(
        point_id=RiskMappingPointEvidenceFacts.identity(
            source_symbol=SYMBOL,
            source_frequency=FREQUENCY,
            center_id=center.center_id,
            center_level_rank=center.structural_level,
            point_type=point.point_type,
            point_anchor_at=anchor_at,
            point_available_at=available_at,
        ),
        source_symbol=SYMBOL,
        source_frequency=FREQUENCY,
        center_id=center.center_id,
        center_level_rank=center.structural_level,
        center_completed=True,
        center_expanded=False,
        point_type=point.point_type,
        point_anchor_at=anchor_at,
        point_available_at=available_at,
        inside_active_top_interval=True,
        highest_mapping_candidate=False,
    )
    bar = SimpleNamespace(
        completed=True,
        end_at=evidence.source_closed_at,
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100"),
        volume=Decimal("1"),
    )

    snapshot = capture_warmup_structure_lineage_snapshot(
        period="W",
        source_symbol=SYMBOL,
        source_frequency=FREQUENCY,
        source_bars=(bar,),
        state=SimpleNamespace(get_strict_evidence=lambda: evidence),
        mapping_supply=_supply(mapping_point),
    )

    captured = snapshot.centers[0]
    assert captured.entry_line_id is None
    assert captured.start_at == center.body_start_market_time
    assert captured.constituent_line_ids == tuple(
        next(line.line_id for line in snapshot.lines if line.ordinal == ordinal)
        for ordinal in range(len(center.body_units))
    )
