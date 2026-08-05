from __future__ import annotations

from pathlib import Path

import pytest

import tools.backtest_chanlun_v3_strict as subject
from tools.backtest_chanlun_v3_strict import build_report


def inputs() -> dict[str, object]:
    core = {"core_contract_sha256": "sha256:core"}
    return {
        "baseline": {"core_contract": core},
        "current_core": core,
        "recursive": {
            "decision": "BLOCKED_BY_FROZEN_STRUCTURE",
            "reason": "missing recursive levels",
            "observed_recursive_levels": [0],
            "required_entry_point_counts": {
                "l0_level2_third_buy": 0,
                "l1_level1_points": 0,
                "l2_level0_first_or_second_buy": 2,
            },
            "source_start": "2018-01-01",
            "source_end": "2023-01-01",
            "source_sessions": 1000,
            "source_timestamp_contract": "completed bars",
        },
        "data_acceptance": {
            "basket_snapshot_failures": 0,
            "statistics": {"candidate_membership_snapshots": 16},
            "strict_candidate_membership_snapshots_available": True,
            "strict_full_v3_return_evaluation_allowed": False,
            "data_grade": "COMPONENT_ONLY",
            "blocking_reasons": ["BLOCKED_BY_FROZEN_STRUCTURE"],
        },
        "removed_paths_absent": True,
        "workspace_manifest": {"workspace_v3_sha256": "sha256:workspace"},
    }


def _bind_source_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    market = tmp_path / "market.sqlite3"
    pit = tmp_path / "pit.sqlite3"
    market.write_bytes(b"unit-test-market")
    pit.write_bytes(b"unit-test-pit")
    monkeypatch.setattr(subject, "DEFAULT_MARKET_DATABASE", market)
    monkeypatch.setattr(subject, "DEFAULT_PIT_DATABASE", pit)


def test_gate_failure_never_turns_no_trades_into_zero_return(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bind_source_files(monkeypatch, tmp_path)
    report = build_report(**inputs())

    assert report["evaluation_status"] == "NOT_EVALUATED_GATE_FAILED"
    assert report["return_evaluation_allowed"] is False
    assert report["performance"]["total_return"] == "NOT_EVALUATED"
    assert report["performance"]["maximum_drawdown"] == "NOT_EVALUATED"
    assert report["trade_counts"]["strategic_cycles"] == 0
    assert "not a zero-return run" in report["trade_counts"]["interpretation"]
    assert report["first_failed_gate"]["gate"] == "direct_recursive_l0_l1_l2"
    assert report["first_failed_gate"]["status"] == "BLOCKED_BY_FROZEN_STRUCTURE"
    assert report["live_status"] == "LIVE_DISABLED"


def test_frozen_core_mismatch_is_the_first_failed_gate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _bind_source_files(monkeypatch, tmp_path)
    values = inputs()
    values["current_core"] = {"core_contract_sha256": "sha256:changed"}

    report = build_report(**values)

    assert report["first_failed_gate"]["gate"] == "frozen_structure_zero_change"
    assert report["first_failed_gate"]["status"] == "FAIL"
