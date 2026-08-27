from __future__ import annotations

import pytest

from chanlun.decision_support.trading_system.backtest.causality_gate_contract import (
    CAUSALITY_GATE_PROVEN_CONTROLS,
    CAUSALITY_GATE_SCHEMA,
    causality_gate_state_is_consistent,
)
from tools import finalize_qmt_pit_fixed_year as pit_finalizer


def test_causality_gate_schema_and_controls_are_frozen() -> None:
    assert CAUSALITY_GATE_SCHEMA == "chanlun-backtest-causality-gate"
    assert CAUSALITY_GATE_PROVEN_CONTROLS == (
        "survivorship_free_effective_dated_security_master",
        "decision_time_sw1_membership",
        "ex_date_only_causal_price_basis",
        "cash_and_share_corporate_action_accounting",
        "closed_bar_strict_structure_witnesses",
        "causal_daily_current_state_intervals",
        "causal_thirty_minute_current_state_intervals",
        "causal_five_minute_current_state_intervals",
        "causal_one_minute_current_state_intervals",
        "causal_sector_thirty_minute_current_state_intervals",
        "canonical_production_five_minute_snapshot_at_every_nesting_pair",
        "canonical_full_one_minute_nesting_witness_ledger",
        "production_five_minute_warmup_gate_at_buy_nesting_pair",
        "production_higher_timeframe_integrity_gate_at_buy_nesting_pair",
        "exact_one_minute_nesting_pair_close_evaluation",
        "next_complete_minute_execution",
        "observed_range_and_volume_fill_guard",
        "delisted_security_zero_recovery",
        "content_addressed_algorithm_data_and_checkpoints",
    )
    assert len(CAUSALITY_GATE_PROVEN_CONTROLS) == 19
    assert len(set(CAUSALITY_GATE_PROVEN_CONTROLS)) == 19


@pytest.mark.parametrize(
    ("status", "pnl_generated", "failures", "report", "expected"),
    (
        ("passed", False, (), "certified_report.json", True),
        ("passed", True, (), "certified_report.json", True),
        ("passed", False, ("failure",), "certified_report.json", False),
        ("passed", False, (), None, False),
        ("blocked", False, ("failure",), None, True),
        ("blocked", True, ("failure",), None, False),
        ("blocked", 0, ("failure",), None, False),
        ("blocked", False, (), None, False),
        ("blocked", False, ("failure",), "report.json", False),
        ("unknown", False, ("failure",), None, False),
    ),
)
def test_causality_gate_state_contract(
    status: object,
    pnl_generated: object,
    failures: tuple[str, ...],
    report: object | None,
    expected: bool,
) -> None:
    assert (
        causality_gate_state_is_consistent(
            status=status,
            pnl_generated=pnl_generated,
            failures=failures,
            report=report,
        )
        is expected
    )


def test_finalizer_rejects_blocked_gate_that_claims_pnl(tmp_path) -> None:
    path = tmp_path / "causality_gate.json"

    with pytest.raises(ValueError, match="inconsistent causality gate state"):
        pit_finalizer._write_gate(
            path=path,
            status="blocked",
            pnl_generated=True,
            algorithm_revision="sha256:" + "1" * 64,
            snapshot_hash="sha256:" + "2" * 64,
            symbols=1,
            evaluations=0,
            failures=("causal_failure",),
        )

    assert not path.exists()


@pytest.mark.parametrize(
    (
        "expected_pair_keys",
        "snapshot_pair_keys",
        "snapshot_converged",
        "expected",
    ),
    (
        ({("setup-a", "witness-a")}, {("setup-a", "witness-a")}, True, False),
        ({("setup-a", "witness-a")}, set(), True, True),
        ({("setup-a", "witness-a")}, set(), False, False),
        (
            {("setup-a", "witness-a")},
            {("setup-b", "witness-a")},
            False,
            False,
        ),
    ),
)
def test_production_snapshot_pair_mismatch_only_blocks_certification_when_unsafe(
    expected_pair_keys: set[tuple[str, str]],
    snapshot_pair_keys: set[tuple[str, str]],
    snapshot_converged: bool,
    expected: bool,
) -> None:
    assert (
        pit_finalizer._production_snapshot_pair_mismatch_is_unsafe(
            expected_pair_keys=expected_pair_keys,
            snapshot_pair_keys=snapshot_pair_keys,
            snapshot_converged=snapshot_converged,
        )
        is expected
    )
