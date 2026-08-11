from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.forward_warmup_structure_lineage import (
    FORWARD_WARMUP_STRUCTURE_LINEAGE_ROLLUP_SCHEMA,
    ForwardWarmupLineageSessionSnapshot,
    build_forward_warmup_structure_lineage_rollup,
    validate_forward_warmup_structure_lineage_rollup,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID,
)
from tests.trading_system.test_warmup_structure_lineage import lineage_envelope


QUALIFICATION = "sha256:" + "9" * 64


def _source(
    *,
    session: date,
    suffix: str,
    signals: tuple[dict[str, object], ...],
) -> ForwardWarmupLineageSessionSnapshot:
    return ForwardWarmupLineageSessionSnapshot(
        session=session,
        live_object_file_sha256="sha256:" + suffix * 64,
        live_object_content_sha256="sha256:" + suffix * 64,
        snapshot_content_sha256="sha256:" + suffix * 64,
        signals=signals,
    )


def _recorded_signal(*, as_of: datetime) -> dict[str, object]:
    envelope = lineage_envelope(as_of=as_of)
    assert envelope.structure_lineage_diagnostic is not None
    return {
        "higher_timeframe_risk": {
            "warmup_structure_lineage_diagnostic_contract_id": (
                WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
            ),
            "market_warmup_structure_lineage_diagnostic_evidence": (
                envelope.structure_lineage_diagnostic.document()
            ),
            "sector_warmup_structure_lineage_diagnostic_evidence": None,
            "symbol_warmup_structure_lineage_diagnostic_evidence": None,
            "sector_strict_same_5m_warmup_structure_lineage_diagnostic_evidence": None,
        }
    }


def test_rollup_requires_current_evidence_and_deduplicates_repeated_risk() -> None:
    recorded_at = datetime.fromisoformat("2026-06-01T11:30:00+08:00")
    recorded_signal = _recorded_signal(as_of=recorded_at)
    empty = _source(
        session=date(2026, 5, 29),
        suffix="1",
        signals=(),
    )
    recorded = _source(
        session=date(2026, 6, 1),
        suffix="2",
        # The same market diagnostic is normally repeated on many symbol rows.
        signals=(recorded_signal, deepcopy(recorded_signal)),
    )

    result = build_forward_warmup_structure_lineage_rollup(
        (recorded, empty),
        through_session=date(2026, 6, 2),
        source_session_qualification_sha256=QUALIFICATION,
    )

    assert result["schema"] == FORWARD_WARMUP_STRUCTURE_LINEAGE_ROLLUP_SCHEMA
    assert result["status"] == "RECORDED"
    assert result["qualified_session_count"] == 2
    assert result["recorded_session_count"] == 2
    assert result["source_signal_count"] == 2
    assert result["lineage_extension_signal_count"] == 2
    assert result["unique_lineage_diagnostic_count"] == 1
    market = result["subjects"]["market"]
    assert market["diagnostic_status_counts"] == {"NON_MONOTONIC": 1}
    assert market["comparison_count"] == 1
    assert market["sell_trigger_absorbed_count"] == 1
    assert result["structure_event_count"] == 1
    event = result["structure_events"][0]
    assert event["point_type"] == "1sell"
    assert event["sell_trigger_absorbed"] is True
    assert event["first_observed_session"] == "2026-06-01"
    assert event["last_observed_session"] == "2026-06-01"
    assert event["observation_count"] == 1
    assert result["cross_session_convergence_adjudication"] == (
        "OBSERVATION_SERIES_ONLY_NO_ABSENCE_INFERENCE"
    )
    assert result["parameters_changed"] is False
    assert result["live_status"] == "LIVE_DISABLED"

    # Source ordering is irrelevant; session/date ordering is canonical.
    reordered = build_forward_warmup_structure_lineage_rollup(
        (empty, recorded),
        through_session=date(2026, 6, 2),
        source_session_qualification_sha256=QUALIFICATION,
    )
    assert reordered == result


def test_rollup_rejects_partial_extension_and_duplicate_sessions() -> None:
    partial = _source(
        session=date(2026, 6, 1),
        suffix="3",
        signals=(
            {
                "higher_timeframe_risk": {
                    "warmup_structure_lineage_diagnostic_contract_id": (
                        WARMUP_STRUCTURE_LINEAGE_DIAGNOSTIC_CONTRACT_ID
                    )
                }
            },
        ),
    )
    with pytest.raises(ValueError, match="incomplete or foreign"):
        build_forward_warmup_structure_lineage_rollup(
            (partial,),
            through_session=date(2026, 6, 2),
            source_session_qualification_sha256=QUALIFICATION,
        )

    empty = _source(
        session=date(2026, 6, 1),
        suffix="4",
        signals=(),
    )
    with pytest.raises(ValueError, match="sessions must be unique"):
        build_forward_warmup_structure_lineage_rollup(
            (empty, empty),
            through_session=date(2026, 6, 2),
            source_session_qualification_sha256=QUALIFICATION,
        )


def test_missing_risk_signal_is_rejected() -> None:
    observed_at = datetime.fromisoformat("2026-06-01T11:30:00+08:00")
    source = _source(
        session=observed_at.date(),
        suffix="6",
        signals=(_recorded_signal(as_of=observed_at), {"candidate_id": "missing"}),
    )
    with pytest.raises(ValueError, match="no higher-timeframe risk evidence"):
        build_forward_warmup_structure_lineage_rollup(
            (source,),
            through_session=date(2026, 6, 2),
            source_session_qualification_sha256=QUALIFICATION,
        )


def test_rehashed_derived_rollup_tamper_is_rejected() -> None:
    observed_at = datetime.fromisoformat("2026-06-01T11:30:00+08:00")
    source = _source(
        session=observed_at.date(),
        suffix="5",
        signals=(_recorded_signal(as_of=observed_at),),
    )
    result = build_forward_warmup_structure_lineage_rollup(
        (source,),
        through_session=date(2026, 6, 2),
        source_session_qualification_sha256=QUALIFICATION,
    )
    assert (
        validate_forward_warmup_structure_lineage_rollup(
            result,
            sources=(source,),
            through_session=date(2026, 6, 2),
            source_session_qualification_sha256=QUALIFICATION,
        )
        == result
    )

    tampered = deepcopy(result)
    tampered["subjects"]["market"]["sell_trigger_absorbed_count"] = 0
    stable = dict(tampered)
    stable.pop("content_sha256")
    tampered["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="(rollup changed|event totals changed)"):
        validate_forward_warmup_structure_lineage_rollup(
            tampered,
            sources=(source,),
            through_session=date(2026, 6, 2),
            source_session_qualification_sha256=QUALIFICATION,
        )
