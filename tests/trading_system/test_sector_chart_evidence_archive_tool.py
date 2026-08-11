from __future__ import annotations

from types import SimpleNamespace

import pytest

from tools.build_sector_chart_evidence_archive import (
    _actual_sector_evidence,
    _assert_expected_projection,
)


class _Document:
    def __init__(self, value: object) -> None:
        self._value = value

    def document(self) -> object:
        return self._value


def test_actual_sector_evidence_preserves_both_convergence_envelopes() -> None:
    selected = {"status": "STABLE_ALL_PREFIXES", "content_sha256": "selected"}
    strict = {"status": "INSUFFICIENT_PREFIXES", "content_sha256": "strict"}
    selected_envelope = _Document(selected)
    selected_envelope.diagnostic = _Document({"status": "STABLE_ALL_PREFIXES"})
    selected_envelope.mapping_supply_diagnostic = _Document(
        {"status": "STABLE_ALL_PREFIXES", "comparison_count": 0}
    )
    strict_envelope = _Document(strict)
    strict_envelope.diagnostic = _Document({"status": "INSUFFICIENT_PREFIXES"})
    strict_envelope.mapping_supply_diagnostic = _Document(
        {"status": "INSUFFICIENT_PREFIXES", "comparison_count": 0}
    )
    evidence = SimpleNamespace(
        source_revision="sha256:source",
        monthly="RISING",
        weekly="RISING",
        daily="RISING",
        grade="RESEARCH_ONLY",
        period_diagnostics=(),
        session_evidence=_Document({"status": "COMPLETE"}),
        warmup_evidence=_Document({"reason": "TAIL_STABLE"}),
        warmup_convergence_evidence=selected_envelope,
        reason_codes=(),
    )
    resolution = SimpleNamespace(
        evidence=evidence,
        source_mode="QMT_SECTOR_5M_SAME_BASE",
        strict_warmup_evidence=_Document({"reason": "INSUFFICIENT"}),
        strict_warmup_convergence_evidence=strict_envelope,
        strict_source_coverage_evidence=_Document({"status": "COMPLETE"}),
        fallback_unavailable_reason_codes=(),
    )

    actual = _actual_sector_evidence(resolution)

    assert actual["warmup_convergence"] == selected
    assert actual["warmup_convergence_diagnostic"] == {
        "status": "STABLE_ALL_PREFIXES"
    }
    assert actual["warmup_mapping_supply_diagnostic"] == {
        "status": "STABLE_ALL_PREFIXES",
        "comparison_count": 0,
    }
    assert actual["strict_same_5m_warmup_convergence"] == strict
    assert actual["strict_same_5m_warmup_convergence_diagnostic"] == {
        "status": "INSUFFICIENT_PREFIXES"
    }
    assert actual["strict_same_5m_warmup_mapping_supply_diagnostic"] == {
        "status": "INSUFFICIENT_PREFIXES",
        "comparison_count": 0,
    }


def test_artifact_projection_permits_only_additive_diagnostics() -> None:
    expected = {
        "gate": "AMBER",
        "mapping_supply": {
            "completed_buy_point_count": 3,
            "reason_codes": ["M_CENTER_MAPPING_UNRESOLVED"],
        },
    }
    actual = {
        **expected,
        "mapping_supply": {
            **expected["mapping_supply"],
            "diagnostic_buy_point_type_counts": {"3buy": 3},
        },
        "diagnostic_only": True,
    }

    _assert_expected_projection(actual, expected)


@pytest.mark.parametrize(
    "actual, message",
    (
        (
            {"gate": "GREEN", "mapping_supply": {}},
            "sector_risk_warmup_evidence.gate changed",
        ),
        (
            {"mapping_supply": {}},
            "sector_risk_warmup_evidence.gate disappeared",
        ),
        (
            {"gate": "AMBER", "mapping_supply": {"reason_codes": []}},
            "changed sequence length",
        ),
    ),
)
def test_artifact_projection_rejects_any_recorded_fact_change(
    actual: object,
    message: str,
) -> None:
    expected = {
        "gate": "AMBER",
        "mapping_supply": {
            "reason_codes": ["M_CENTER_MAPPING_UNRESOLVED"],
        },
    }

    with pytest.raises(ValueError, match=message):
        _assert_expected_projection(actual, expected)
