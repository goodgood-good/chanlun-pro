from dataclasses import replace
from datetime import timedelta

import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import StrictPointStatus
from chanlun.decision_support.trading_system.provisional import (
    ProvisionalCandidate,
    extract_provisional_candidates,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    point_decision_document,
)
from tests.trading_system.helpers import provisional_point
from tests.trading_system.strict_helpers import (
    DEFAULT_CLOSED_AT,
    strict_evidence_result,
    strict_point,
)


def test_provisional_adapter_reads_only_strict_approaching_points() -> None:
    parent = strict_point("1buy")
    raw = strict_point("2buy", status=StrictPointStatus.APPROACHING)
    raw = replace(raw, parent_point_id=parent.point_id)
    candidates = extract_provisional_candidates(
        strict_evidence_result(
            confirmed_points=(parent,),
            approaching_points=(raw,),
        ),
        code="SZ.000001",
        source_frequency="5m",
        as_of=DEFAULT_CLOSED_AT,
    )

    assert len(candidates) == 1
    assert candidates[0].candidate_id == raw.point_id
    assert candidates[0].tower == "formal"
    assert candidates[0].observed_at == raw.available_at
    assert candidates[0].anchor_at == raw.anchor_at
    assert candidates[0].available_at == raw.available_at
    assert candidates[0].anchor_at < candidates[0].available_at
    assert candidates[0].missing_conditions == raw.missing_conditions
    assert candidates[0].evidence_codes == raw.evidence_codes
    assert candidates[0].parent_point_id is not None
    assert candidates[0].parent_point_id != parent.point_id
    assert candidates[0].related_point_ids == ()
    assert candidates[0].small_to_large_carrier_unit_ids == ()
    assert candidates[0].actionable is False
    document = point_decision_document(candidates[0])
    assert document["anchor_at"] == raw.anchor_at.isoformat()
    assert document["available_at"] == raw.available_at.isoformat()


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
    raw = strict_point("1sell", status=StrictPointStatus.APPROACHING)
    candidate = extract_provisional_candidates(
        strict_evidence_result(approaching_points=(raw,)),
        code="SZ.000001",
        source_frequency="5m",
        as_of=DEFAULT_CLOSED_AT,
    )[0]

    assert not hasattr(candidate, "progress")
    assert not hasattr(candidate, "probability")
    assert not hasattr(candidate, "score")


def test_provisional_candidate_rejects_noncanonical_point_type() -> None:
    valid = provisional_point("1buy")
    values = {
        field: getattr(valid, field)
        for field in valid.__dataclass_fields__
    }
    values.update(
        candidate_id="candidate:old-l2buy",
        point_type="l2buy",
    )

    with pytest.raises(ValueError, match="买卖点类型无效"):
        ProvisionalCandidate(**values)


def test_provisional_candidate_rejects_opposite_terminal_segment_direction() -> None:
    valid = provisional_point("3buy")
    reference = TerminalSegmentReference(
        role="latest_unfinished",
        structural_level=0,
        unit_id="segment:wrong-direction",
        source_kind="segment",
        direction="up",
        state="forming",
        market_start=valid.anchor_at - timedelta(minutes=30),
        market_end=valid.anchor_at,
        available_at=valid.available_at,
    )

    with pytest.raises(ValueError, match="terminal lineage mismatch"):
        replace(valid, terminal_segment=reference)


def test_provisional_adapter_rejects_non_approaching_endpoint() -> None:
    parent = strict_point("1buy")
    raw = strict_point("2buy", status=StrictPointStatus.APPROACHING)
    raw = replace(raw, parent_point_id=parent.point_id)
    evidence = strict_evidence_result(
        confirmed_points=(parent,),
        approaching_points=(raw,),
    )
    object.__setattr__(raw, "status", StrictPointStatus.CONFIRMED)

    with pytest.raises(ValueError, match="non-approaching point"):
        extract_provisional_candidates(
            evidence,
            code="SZ.000001",
            source_frequency="5m",
            as_of=DEFAULT_CLOSED_AT,
        )
