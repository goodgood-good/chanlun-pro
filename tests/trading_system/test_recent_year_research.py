from dataclasses import replace

import pytest

from chanlun.decision_support.trading_system.recent_year_research import (
    RECENT_YEAR_SELECTION_PATH,
    recent_year_research_parameters,
)
from chanlun.decision_support.trading_system.multisymbol_replay import (
    SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES,
    research_sector_technical_direct_replay_contract,
)


def test_recent_year_research_snapshot_is_frozen_and_explicitly_biased() -> None:
    value = recent_year_research_parameters()
    document = value.document()

    assert value.selection_path == RECENT_YEAR_SELECTION_PATH
    assert value.warmup_start.isoformat() == "2023-05-01"
    assert value.requested_start.isoformat() == "2025-07-25"
    assert value.requested_end.isoformat() == "2026-07-24"
    assert value.three_program_mode == "DISABLED_USER_AUTHORIZED"
    assert value.execution_observation == "COMPLETED_1M_BAR"
    assert value.tick_data_used is False
    assert value.live_status == "LIVE_DISABLED"
    assert document["parameter_set_id"].startswith("sha256:")
    assert "CURRENT_QMT_GICS3_MEMBERSHIP_BACKFILLED_OVER_TEST_YEAR" in document[
        "known_biases"
    ]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("three_program_mode", "ENABLED"),
        ("tick_data_used", True),
        ("signal_bar_fill_allowed", True),
        ("strategic_frequency", "5m"),
        ("live_status", "LIVE_ENABLED"),
    ),
)
def test_recent_year_research_snapshot_rejects_semantic_drift(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        replace(recent_year_research_parameters(), **{field: value})


def test_recent_year_replay_contract_uses_stock_rules_without_three_program() -> None:
    contract = research_sector_technical_direct_replay_contract()

    assert contract.selection_path == RECENT_YEAR_SELECTION_PATH
    assert contract.accepted_recursive_level == 2
    assert contract.research_variant_parameter_set_id == (
        recent_year_research_parameters().parameter_set_id
    )
    assert "sector_trigger" in SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES
    assert "industry_opportunity" not in SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES
    assert "fundamental_role" not in SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES
    assert "relative_value" not in SECTOR_TECHNICAL_REQUIRED_CANDIDATE_GATES
    assert contract.live_status == "LIVE_DISABLED"
