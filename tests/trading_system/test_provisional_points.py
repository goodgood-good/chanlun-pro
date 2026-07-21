from datetime import timedelta

import pytest

from chanlun.core.strict_structure.models import StrictPointStatus
from chanlun.decision_support.trading_system.provisional import (
    extract_provisional_candidates,
)
from tests.trading_system.strict_helpers import (
    DEFAULT_CLOSED_AT,
    strict_evidence_result,
    strict_point,
)


def test_provisional_adapter_reads_only_strict_approaching_points() -> None:
    raw = strict_point("2buy", status=StrictPointStatus.APPROACHING)
    candidates = extract_provisional_candidates(
        strict_evidence_result(approaching_points=(raw,)),
        code="SZ.000001",
        source_frequency="5m",
        as_of=DEFAULT_CLOSED_AT,
    )

    assert len(candidates) == 1
    assert candidates[0].candidate_id == raw.point_id
    assert candidates[0].tower == "formal"
    assert candidates[0].observed_at == raw.available_at
    assert candidates[0].missing_conditions == raw.missing_conditions
    assert candidates[0].evidence_codes == raw.evidence_codes
    assert candidates[0].actionable is False


def test_provisional_adapter_rejects_future_visibility() -> None:
    raw = strict_point("1buy", status=StrictPointStatus.APPROACHING)
    evidence = strict_evidence_result(approaching_points=(raw,))
    object.__setattr__(
        raw,
        "available_at",
        DEFAULT_CLOSED_AT + timedelta(minutes=1),
    )

    with pytest.raises(ValueError, match="available after as_of"):
        extract_provisional_candidates(
            evidence,
            code="SZ.000001",
            source_frequency="5m",
            as_of=DEFAULT_CLOSED_AT,
        )


def test_candidate_has_no_probability_score() -> None:
    raw = strict_point("3sell", status=StrictPointStatus.APPROACHING)
    candidate = extract_provisional_candidates(
        strict_evidence_result(approaching_points=(raw,)),
        code="SZ.000001",
        source_frequency="5m",
        as_of=DEFAULT_CLOSED_AT,
    )[0]

    assert not hasattr(candidate, "progress")
    assert not hasattr(candidate, "probability")
    assert not hasattr(candidate, "score")


def test_provisional_adapter_rejects_non_approaching_endpoint() -> None:
    raw = strict_point("2buy", status=StrictPointStatus.APPROACHING)
    evidence = strict_evidence_result(approaching_points=(raw,))
    object.__setattr__(raw, "status", StrictPointStatus.CONFIRMED)

    with pytest.raises(ValueError, match="non-approaching point"):
        extract_provisional_candidates(
            evidence,
            code="SZ.000001",
            source_frequency="5m",
            as_of=DEFAULT_CLOSED_AT,
        )
