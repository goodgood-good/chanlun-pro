"""Shared contract for the certified fixed-year causality gate."""

from __future__ import annotations

from collections.abc import Collection


CAUSALITY_GATE_SCHEMA = "chanlun-backtest-causality-gate"
CAUSALITY_GATE_STATUS_BLOCKED = "blocked"
CAUSALITY_GATE_STATUS_PASSED = "passed"
CAUSALITY_GATE_STATUSES = frozenset(
    {
        CAUSALITY_GATE_STATUS_BLOCKED,
        CAUSALITY_GATE_STATUS_PASSED,
    }
)

# Keep this ordered: the finalizer publishes the tuple verbatim, while readers
# treat it as a complete, duplicate-free set of independently proven controls.
CAUSALITY_GATE_PROVEN_CONTROLS = (
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
CAUSALITY_GATE_PROVEN_CONTROL_SET = frozenset(CAUSALITY_GATE_PROVEN_CONTROLS)


def causality_gate_state_is_consistent(
    *,
    status: object,
    pnl_generated: object,
    failures: Collection[object],
    report: object | None,
) -> bool:
    """Return whether gate outcome fields form one closed, valid state.

    Passing certifies causality, not profitability: a valid zero-fill replay is
    therefore allowed to carry ``pnl_generated=False``.  A blocked replay is
    deliberately stricter and must prove that no P&L/report was published.
    """

    if type(pnl_generated) is not bool:
        return False
    if status == CAUSALITY_GATE_STATUS_BLOCKED:
        return pnl_generated is False and bool(failures) and report is None
    if status == CAUSALITY_GATE_STATUS_PASSED:
        return not failures and report is not None
    return False


__all__ = (
    "CAUSALITY_GATE_PROVEN_CONTROLS",
    "CAUSALITY_GATE_PROVEN_CONTROL_SET",
    "CAUSALITY_GATE_SCHEMA",
    "CAUSALITY_GATE_STATUSES",
    "CAUSALITY_GATE_STATUS_BLOCKED",
    "CAUSALITY_GATE_STATUS_PASSED",
    "causality_gate_state_is_consistent",
)
