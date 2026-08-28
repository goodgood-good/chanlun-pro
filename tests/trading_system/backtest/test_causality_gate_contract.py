from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from chanlun.decision_support.trading_system.backtest.causality_gate_contract import (
    CAUSALITY_GATE_PROVEN_CONTROLS,
    CAUSALITY_GATE_SCHEMA,
    causality_gate_state_is_consistent,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FiveMinuteWarmupFact,
)
from tools import finalize_qmt_pit_fixed_year as pit_finalizer
from tests.trading_system.helpers import confirmed_point, eligible_sector


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


def test_pair_consistency_overlay_downgrades_mismatched_converged_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_setup = confirmed_point("3buy", center_id="expected-center")
    production_setup = confirmed_point(
        "3buy",
        anchor=10.1,
        center_id="production-center",
    )
    witness = confirmed_point("1buy", frequency="1m", center_id="witness-center")
    observed_at = expected_setup.available_at
    snapshot = FiveMinuteWarmupFact(
        observed_at=observed_at,
        source_closed_at=observed_at,
        converged=True,
        full_bar_count=12000,
        suffix_bar_count=8000,
        reason_code="WARMUP_TAIL_STABLE",
        production_five_points=(production_setup,),
        one_minute_bar_count=12000,
    )

    @dataclass(frozen=True)
    class FakeFacts:
        code: str
        evaluations: tuple[SimpleNamespace, ...]
        five_minute_warmup: tuple[FiveMinuteWarmupFact, ...]

    facts = FakeFacts(
        code=expected_setup.code,
        evaluations=(SimpleNamespace(observed_at=observed_at),),
        five_minute_warmup=(snapshot,),
    )

    def exact_pairs(
        _facts: object,
        _evaluation: object,
        *,
        setup_points: object | None = None,
    ) -> tuple[tuple[object, object], ...]:
        setup = expected_setup if setup_points is None else production_setup
        return ((setup, witness),)

    monkeypatch.setattr(pit_finalizer, "_new_exact_buy_nesting_pairs", exact_pairs)

    adjusted, document = (
        pit_finalizer._apply_production_snapshot_pair_consistency_overlay(
            (facts,),
            algorithm_revision="sha256:" + "1" * 64,
            fact_algorithm_revision="sha256:" + "2" * 64,
        )
    )

    [adjusted_facts] = adjusted
    [adjusted_snapshot] = adjusted_facts.five_minute_warmup
    assert snapshot.converged is True
    assert adjusted_snapshot.converged is False
    assert adjusted_snapshot.reason_code == "WARMUP_TAIL_DIVERGED"
    assert adjusted_snapshot.difference_codes == (
        "WARMUP_OTHER_SEMANTIC_CHANGED",
    )
    assert document["production_snapshot_count"] == 1
    assert document["downgraded_snapshot_count"] == 1
    assert document["downgraded_codes"] == [expected_setup.code]
    assert pit_finalizer._production_snapshot_pair_mismatch_is_unsafe(
        expected_pair_keys={
            (
                pit_finalizer.structural_point_occurrence_id(expected_setup),
                witness.point_id,
            )
        },
        snapshot_pair_keys={
            (
                pit_finalizer.structural_point_occurrence_id(production_setup),
                witness.point_id,
            )
        },
        snapshot_converged=adjusted_snapshot.converged,
    ) is False


def test_decision_funnel_discloses_point_types_and_exact_one_minute_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    point = confirmed_point("3buy")
    sector = eligible_sector()
    gates = SimpleNamespace(
        market=SimpleNamespace(gate="GREEN"),
        sector=SimpleNamespace(gate="AMBER"),
        symbol=SimpleNamespace(gate="UNRESOLVED"),
    )
    evaluation = SimpleNamespace(
        observed_at=point.available_at,
        sector_id=sector.sector_id,
        higher_timeframe_gates=gates,
    )
    visibility = SimpleNamespace(
        point_id=point.point_id,
        contains=lambda _observed_at: True,
    )
    symbol = SimpleNamespace(
        code=point.code,
        sector_id=sector.sector_id,
        five_points=(point,),
        five_point_visibility=(visibility,),
        evaluations=(evaluation,),
    )
    sector_facts = SimpleNamespace(
        assessments=((evaluation.observed_at, sector),),
    )
    monkeypatch.setattr(
        pit_finalizer,
        "_new_exact_buy_nesting_pairs",
        lambda _facts, _evaluation: ((point, object()),),
    )

    document = pit_finalizer._decision_funnel_diagnostics(
        symbols=(symbol,),
        sectors={sector.sector_id: sector_facts},
    )

    assert document["schema"] == "chanlun-fixed-year-decision-funnel-v1"
    assert document["five_minute_signal_event_count"] == 1
    assert document["unique_five_minute_setups_by_point_type"]["3buy"] == 1
    assert document["exact_one_minute_nesting_boundary_event_count"] == 1
    assert document[
        "exact_one_minute_nesting_boundary_events_by_five_minute_point_type"
    ]["3buy"] == 1
    assert document["higher_timeframe_market_gates_at_boundary"] == {"GREEN": 1}
    assert document["higher_timeframe_sector_gates_at_boundary"] == {"AMBER": 1}
    assert document["higher_timeframe_symbol_gates_at_boundary"] == {
        "UNRESOLVED": 1
    }
    assert document["sector_regimes_at_causal_evaluation"] == {"neutral": 1}
