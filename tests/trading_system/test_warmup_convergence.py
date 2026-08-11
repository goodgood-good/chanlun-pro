from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.warmup_convergence import (
    WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID,
    WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID,
    WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID,
    WarmupConvergenceDiagnosticEnvelope,
    WarmupConvergenceEnvelope,
    WarmupMappingSupplyDiagnosticEnvelope,
    WarmupMappingSupplySnapshot,
    WarmupPeriodSemanticFacts,
    WarmupPrefixObservation,
    WarmupSemanticSnapshot,
    bind_warmup_convergence_diagnostic,
    bind_warmup_mapping_supply_diagnostic,
    classify_warmup_convergence_envelope,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    RiskMappingPointEvidenceFacts,
    RiskMappingSupplyFacts,
)


AS_OF = datetime.fromisoformat("2026-07-29T14:30:00+08:00")
PARAMETERS = "sha256:" + "1" * 64


def observation(bar_count: int, signature: str) -> WarmupPrefixObservation:
    return WarmupPrefixObservation(
        bar_count=bar_count,
        starts_at=AS_OF - timedelta(minutes=bar_count),
        signature_sha256="sha256:" + signature * 64,
    )


def classify(*values: WarmupPrefixObservation):
    return classify_warmup_convergence_envelope(
        frequency="1m",
        as_of=AS_OF,
        parameter_set_id=PARAMETERS,
        observations=tuple(values),
    )


def semantic_snapshot(
    *,
    weekly_state: str = "NONE",
    daily_ma5: str = "10",
) -> WarmupSemanticSnapshot:
    return WarmupSemanticSnapshot(
        periods=tuple(
            WarmupPeriodSemanticFacts(
                period=period,
                state=weekly_state if period == "W" else "NONE",
                evidence_bar_end=AS_OF - timedelta(days=index + 1),
                active_top_interval=None,
                mapping_unique=False,
                mapped_center_id=None,
                mapping_candidate_ids=(),
                blocker_codes=(f"{period}_CENTER_MAPPING_UNRESOLVED",),
                warning_codes=(),
            )
            for index, period in enumerate(("M", "W", "D"))
        ),
        ma5=(
            ("M", Decimal("8")),
            ("W", Decimal("9")),
            ("D", Decimal(daily_ma5)),
        ),
    )


def semantic_envelope() -> WarmupConvergenceEnvelope:
    first = semantic_snapshot()
    middle = semantic_snapshot(weekly_state="FORMED", daily_ma5="11")
    longest = semantic_snapshot()
    prefixes = tuple(
        WarmupPrefixObservation(
            bar_count=count,
            starts_at=AS_OF - timedelta(days=count),
            signature_sha256=snapshot.signature_sha256,
        )
        for count, snapshot in zip((480, 640, 800), (first, middle, longest))
    )
    envelope = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=AS_OF,
        parameter_set_id=PARAMETERS,
        observations=prefixes,
    )
    return bind_warmup_convergence_diagnostic(
        envelope,
        snapshots=(first, middle, longest),
    )


def mapping_point(
    *,
    center_id: str,
    point_type: str,
    anchor_days: int,
    highest: bool,
) -> RiskMappingPointEvidenceFacts:
    anchor = AS_OF - timedelta(days=anchor_days)
    available = anchor + timedelta(days=1)
    return RiskMappingPointEvidenceFacts(
        point_id=RiskMappingPointEvidenceFacts.identity(
            source_symbol="SH.000001",
            source_frequency="d",
            center_id=center_id,
            center_level_rank=1,
            point_type=point_type,
            point_anchor_at=anchor,
            point_available_at=available,
        ),
        source_symbol="SH.000001",
        source_frequency="d",
        center_id=center_id,
        center_level_rank=1,
        center_completed=True,
        center_expanded=False,
        point_type=point_type,  # type: ignore[arg-type]
        point_anchor_at=anchor,
        point_available_at=available,
        inside_active_top_interval=True,
        highest_mapping_candidate=highest,
    )


def mapping_supply(*, unique: bool) -> RiskMappingSupplyFacts:
    point = mapping_point(
        center_id="sha256:" + ("a" if unique else "b") * 64,
        point_type="1sell" if unique else "3buy",
        anchor_days=8 if unique else 7,
        highest=unique,
    )
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


def mapping_semantic_snapshot(*, unique: bool) -> WarmupSemanticSnapshot:
    center_id = "sha256:" + "a" * 64
    periods = []
    for period in ("M", "W", "D"):
        active = period == "W"
        periods.append(
            WarmupPeriodSemanticFacts(
                period=period,
                state=(
                    "FORMED"
                    if active and unique
                    else "FORMED_UNRESOLVED"
                    if active
                    else "NONE"
                ),
                evidence_bar_end=AS_OF - timedelta(days=6),
                active_top_interval=(
                    (AS_OF - timedelta(days=12), AS_OF - timedelta(days=6))
                    if active
                    else None
                ),
                mapping_unique=(unique if active else True),
                mapped_center_id=(center_id if active and unique else None),
                mapping_candidate_ids=(
                    (center_id,) if active and unique else ()
                ),
                blocker_codes=(
                    ("NO_COMPLETED_LOWER_1SELL_2SELL_CENTER_IN_TOP_FRACTAL",)
                    if active and not unique
                    else ()
                ),
                warning_codes=(),
            )
        )
    return WarmupSemanticSnapshot(
        periods=tuple(periods),
        ma5=(("M", Decimal("8")), ("W", Decimal("9")), ("D", Decimal("10"))),
    )


def mapping_supply_envelope() -> WarmupConvergenceEnvelope:
    shortest = mapping_semantic_snapshot(unique=False)
    middle = mapping_semantic_snapshot(unique=True)
    longest = mapping_semantic_snapshot(unique=False)
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=count,
            starts_at=AS_OF - timedelta(days=count),
            signature_sha256=snapshot.signature_sha256,
        )
        for count, snapshot in zip((480, 620, 744), (shortest, middle, longest))
    )
    envelope = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=AS_OF,
        parameter_set_id=PARAMETERS,
        observations=observations,
    )
    envelope = bind_warmup_convergence_diagnostic(
        envelope,
        snapshots=(shortest, middle, longest),
    )
    return bind_warmup_mapping_supply_diagnostic(
        envelope,
        snapshots=(
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", mapping_supply(unique=False)), ("D", None))
            ),
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", mapping_supply(unique=True)), ("D", None))
            ),
            WarmupMappingSupplySnapshot(
                periods=(("M", None), ("W", mapping_supply(unique=False)), ("D", None))
            ),
        ),
    )


def test_all_prefixes_equal_is_diagnostic_stable() -> None:
    result = classify(
        observation(1200, "a"),
        observation(1600, "a"),
        observation(2000, "a"),
    )

    assert result.status == "STABLE_ALL_PREFIXES"
    assert result.stable_all_prefixes is True
    assert result.match_longest_pattern == (True, True, True)
    assert result.diagnostic_only is True
    assert result.active_gate_unchanged is True
    assert result.live_status == "LIVE_DISABLED"
    assert result.contract_id == WARMUP_CONVERGENCE_ENVELOPE_CONTRACT_ID


def test_prefix_sensitive_sequence_is_not_promoted_to_stable() -> None:
    result = classify(
        observation(1200, "a"),
        observation(1600, "b"),
        observation(2000, "c"),
    )

    assert result.status == "CONVERGED_ONLY_WITH_LONGER_HISTORY"
    assert result.stable_all_prefixes is False
    assert result.match_longest_pattern == (False, False, True)
    assert "LONGER_HISTORY_REQUIRED_FOR_STABILITY_EVIDENCE" in (
        result.reason_codes
    )


def test_a_b_a_signature_sequence_is_explicitly_non_monotonic() -> None:
    result = classify(
        observation(1200, "a"),
        observation(1600, "b"),
        observation(2000, "a"),
    )

    assert result.status == "NON_MONOTONIC"
    assert result.match_longest_pattern == (True, False, True)
    assert "ACTIVE_PAIRWISE_WARMUP_MAY_BE_FALSE_STABLE" in result.reason_codes


def test_missing_third_prefix_is_explicitly_insufficient() -> None:
    result = classify(observation(1200, "a"), observation(1600, "a"))

    assert result.status == "INSUFFICIENT_PREFIXES"
    assert result.reference_signature_sha256 is None
    assert result.match_longest_pattern == (False, False)


def test_duplicate_or_out_of_order_prefix_counts_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique increasing bar counts"):
        classify(
            observation(1600, "a"),
            observation(1200, "a"),
            observation(2000, "a"),
        )


def test_invalid_signature_identity_is_rejected() -> None:
    with pytest.raises(ValueError, match="signature_sha256"):
        WarmupPrefixObservation(
            bar_count=1200,
            starts_at=AS_OF,
            signature_sha256="not-a-hash",
        )


def test_canonical_document_round_trip_binds_every_classification_field() -> None:
    result = classify(
        observation(1200, "a"),
        observation(1600, "b"),
        observation(2000, "a"),
    )

    document = result.document()
    assert document["content_sha256"] == result.content_sha256
    assert WarmupConvergenceEnvelope.from_document(document) == result


def test_rehashed_semantic_verdict_tampering_is_rejected() -> None:
    document = deepcopy(
        classify(
            observation(1200, "a"),
            observation(1600, "b"),
            observation(2000, "a"),
        ).document()
    )
    # Rehashing a forged verdict is not enough: the parser recomputes the
    # classification from the immutable prefix signatures.
    document["status"] = "STABLE_ALL_PREFIXES"
    document["stable_all_prefixes"] = True
    document["match_longest_pattern"] = [True, True, True]
    document["reason_codes"] = ["WARMUP_ENVELOPE_STABLE_ALL_PREFIXES"]
    stable = dict(document)
    stable.pop("content_sha256")
    document["content_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError, match="malformed"):
        WarmupConvergenceEnvelope.from_document(document)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("observation_count", 4),
        ("stable_all_prefixes", 1),
        ("diagnostic_only", 1),
        ("active_gate_unchanged", False),
    ),
)
def test_noncanonical_counts_and_boolean_safety_fields_are_rejected(
    field: str,
    value: object,
) -> None:
    document = classify(
        observation(1200, "a"),
        observation(1600, "a"),
        observation(2000, "a"),
    ).document()
    document[field] = value
    stable = dict(document)
    stable.pop("content_sha256")
    document["content_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError):
        WarmupConvergenceEnvelope.from_document(document)


def test_semantic_diagnostic_explains_non_monotonic_period_and_ma5_changes() -> None:
    envelope = semantic_envelope()

    assert envelope.status == "NON_MONOTONIC"
    assert envelope.diagnostic is not None
    diagnostic = envelope.diagnostic
    assert diagnostic.contract_id == WARMUP_CONVERGENCE_DIAGNOSTIC_CONTRACT_ID
    assert diagnostic.envelope_content_sha256 == envelope.content_sha256
    document = diagnostic.document()
    assert document["observations"][0]["changed_paths_from_longest"] == []
    assert document["observations"][1]["changed_paths_from_longest"] == [
        "W.state",
        "D.ma5",
    ]
    assert document["observations"][2]["changed_paths_from_longest"] == []


def test_semantic_diagnostic_round_trip_recomputes_and_binds_every_fact() -> None:
    envelope = semantic_envelope()
    assert envelope.diagnostic is not None

    restored = WarmupConvergenceDiagnosticEnvelope.from_document(
        envelope.diagnostic.document()
    )
    restored.validate_against(envelope)
    assert restored == envelope.diagnostic


def test_rehashed_semantic_changed_path_tampering_is_rejected() -> None:
    envelope = semantic_envelope()
    assert envelope.diagnostic is not None
    document = deepcopy(envelope.diagnostic.document())
    document["observations"][1]["changed_paths_from_longest"] = ["M.state"]
    stable = dict(document)
    stable.pop("content_sha256")
    document["content_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError, match="non-canonical"):
        WarmupConvergenceDiagnosticEnvelope.from_document(document)


def test_semantic_diagnostic_cannot_bind_a_different_envelope() -> None:
    envelope = semantic_envelope()
    assert envelope.diagnostic is not None
    foreign = classify(
        observation(1200, "a"),
        observation(1600, "b"),
        observation(2000, "a"),
    )

    with pytest.raises(ValueError, match="does not bind"):
        envelope.diagnostic.validate_against(foreign)


def test_mapping_supply_diagnostic_explains_disappearing_sell_mapping() -> None:
    envelope = mapping_supply_envelope()

    assert envelope.status == "NON_MONOTONIC"
    assert envelope.mapping_supply_diagnostic is not None
    diagnostic = envelope.mapping_supply_diagnostic
    assert (
        diagnostic.contract_id
        == WARMUP_MAPPING_SUPPLY_DIAGNOSTIC_CONTRACT_ID
    )
    assert len(diagnostic.comparisons) == 1
    comparison = diagnostic.comparisons[0].document()
    assert (comparison["period"], comparison["prefix_bar_count"]) == ("W", 620)
    assert comparison["delta"]["transition_codes"] == [
        "SUPPLY_CLASSIFICATION_CHANGED",
        "SELL12_DISAPPEARED_WITH_LONGER_HISTORY",
        "COMPLETED_IN_INTERVAL_SELL12_DISAPPEARED_WITH_LONGER_HISTORY",
        "HIGHEST_CANDIDATE_DISAPPEARED_WITH_LONGER_HISTORY",
        "POINT_EVIDENCE_LOST_WITH_LONGER_HISTORY",
        "POINT_EVIDENCE_GAINED_WITH_LONGER_HISTORY",
        "POINT_IDENTITY_SET_RESEGMENTED",
    ]
    assert len(comparison["delta"]["lost_highest_candidate_point_ids"]) == 1
    assert comparison["delta"]["lost_points_from_longest"][0][
        "point_type"
    ] == "1sell"
    assert comparison["delta"]["gained_points_in_longest"][0][
        "point_type"
    ] == "3buy"


def test_mapping_supply_diagnostic_round_trip_and_identities_are_stable() -> None:
    envelope = mapping_supply_envelope()
    assert envelope.diagnostic is not None
    assert envelope.mapping_supply_diagnostic is not None

    restored = WarmupMappingSupplyDiagnosticEnvelope.from_document(
        envelope.mapping_supply_diagnostic.document()
    )
    restored.validate_against(envelope)
    assert restored == envelope.mapping_supply_diagnostic
    # Both existing documents remain unaware of the additive sibling.
    bare = classify_warmup_convergence_envelope(
        frequency=envelope.frequency,
        as_of=envelope.as_of,
        parameter_set_id=envelope.parameter_set_id,
        observations=envelope.observations,
    )
    assert envelope.content_sha256 == bare.content_sha256
    assert envelope.document() == bare.document()


def test_rehashed_mapping_supply_delta_tampering_is_rejected() -> None:
    envelope = mapping_supply_envelope()
    assert envelope.mapping_supply_diagnostic is not None
    document = deepcopy(envelope.mapping_supply_diagnostic.document())
    document["comparisons"][0]["delta"]["transition_codes"] = [
        "MAPPING_SUPPLY_UNCHANGED"
    ]
    stable = dict(document)
    stable.pop("content_sha256")
    document["content_sha256"] = sha256_json(stable)

    with pytest.raises(ValueError, match="malformed"):
        WarmupMappingSupplyDiagnosticEnvelope.from_document(document)


def test_mapping_supply_diagnostic_cannot_bind_foreign_semantics() -> None:
    envelope = mapping_supply_envelope()
    assert envelope.mapping_supply_diagnostic is not None
    foreign = semantic_envelope()

    with pytest.raises(ValueError, match="does not bind"):
        envelope.mapping_supply_diagnostic.validate_against(foreign)
