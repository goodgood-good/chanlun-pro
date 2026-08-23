#!/usr/bin/env python3
"""Compare causal profit-protection rules on an already certified small run.

This diagnostic never discovers new entries and never changes source facts.  It
replays only the positions that the certified portfolio actually opened, using
completed one-minute QMT bars.  A stop derived from one completed minute becomes
effective on the next minute, so the comparison cannot use an intrabar future.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_FLOOR
import json
from pathlib import Path
import pickle
import sys
from typing import Literal, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SOURCE_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from chanlun.decision_support.trading_system.backtest.execution import (
    ExecutionPolicy,
    OrderIntent,
    fees_for,
    liquidity_slippage,
    try_fill,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    SectorResearchFacts,
    SymbolResearchFacts,
    _active_minute_source,
    _status_for_bar,
    build_symbol_bundle,
)
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    _round_price,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
)
from chanlun.decision_support.trading_system.models import TradingPolicy


ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class Rule:
    rule_id: str
    kind: Literal["break_even", "continuous", "step"]
    activate_at_r: Decimal
    trail_distance_r: Decimal


RULES = (
    Rule("break_even_after_1r", "break_even", ONE, ONE),
    Rule("continuous_1r_after_1r", "continuous", ONE, ONE),
    Rule("continuous_1r_after_2r", "continuous", Decimal("2"), ONE),
    Rule("step_1r_after_1r", "step", ONE, ONE),
    Rule(
        "continuous_1_5r_after_1r",
        "continuous",
        ONE,
        Decimal("1.5"),
    ),
)


@dataclass(slots=True)
class PositionState:
    code: str
    opened_at: datetime
    baseline_end: datetime
    shares: int
    entry_price: Decimal
    entry_fees: Decimal
    reference_price: Decimal
    initial_risk: Decimal
    hard_stop: Decimal
    effective_stop: Decimal
    high_water: Decimal
    action_cash: Decimal = ZERO


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--input-dir", type=Path, required=True)
    result.add_argument("--initial-cash", type=Decimal, default=Decimal("1000000"))
    return result


def _fact_path(directory: Path, code: str) -> Path:
    return directory / "symbols" / f"{code.replace('.', '_')}.pkl"


def _load_pickle(path: Path, expected_type: type):
    value = pickle.loads(path.read_bytes())
    if not isinstance(value, expected_type):
        raise TypeError(f"unexpected artifact type in {path}: {type(value).__name__}")
    return value


def _entry_fee(run: BacktestRun, code: str, opened_at: datetime) -> Decimal:
    matches = tuple(
        fill
        for fill in run.fills
        if fill.filled
        and fill.filled_at == opened_at
        and fill.order_id.startswith("entry:")
    )
    if len(matches) != 1:
        raise ValueError(f"entry fill is not unique for {code} at {opened_at}")
    return matches[0].fees


def _entry_context(
    run: BacktestRun,
    directory: Path,
    facts: SymbolResearchFacts,
    position,
) -> dict[str, object]:
    opened_at = (
        position.entry_at if hasattr(position, "entry_at") else position.opened_at
    )
    matches = tuple(
        fill
        for fill in run.fills
        if fill.filled
        and fill.filled_at == opened_at
        and fill.order_id.startswith("entry:")
    )
    if len(matches) != 1:
        raise ValueError(f"entry fill is not unique for {facts.code} at {opened_at}")
    signal_id = matches[0].order_id.split(":", 2)[1] + ":" + matches[0].order_id.split(":", 2)[2][:64]
    created_text = matches[0].order_id[len(f"entry:{signal_id}:") :]
    created_at = datetime.fromisoformat(created_text)
    evaluations = tuple(
        row for row in facts.evaluations if row.observed_at == created_at
    )
    if len(evaluations) != 1:
        raise ValueError(f"entry evaluation is not unique for {facts.code}")
    evaluation = evaluations[0]
    sector_id = evaluation.sector_id or facts.sector_id
    sector_path = directory / "pit_sectors" / f"{sector_id.rsplit(':', 1)[-1]}.pkl"
    sector_facts = _load_pickle(sector_path, SectorResearchFacts)
    sector = dict(sector_facts.assessments).get(created_at)
    if sector is None:
        raise ValueError(f"entry sector assessment is unavailable for {facts.code}")
    selection_sources = (
        ("QMT_SECTOR_TRIGGER",)
        if sector.regime == "supportive"
        else ("QMT_SECTOR_ELIGIBLE_SCOPE",)
        if sector.eligible
        else ("INCREMENTAL_SCAN_SCOPE",)
    )
    bundle = build_symbol_bundle(
        facts,
        evaluation,
        sector,
        selection_sources=selection_sources,
    )
    evaluated = tuple(
        item
        for item in HumanAssistedDecisionCore(
            TradingPolicy(),
            formal_selection_required=False,
        ).evaluate_symbol(bundle)
        if item.entry is not None and item.entry.signal_id == signal_id
    )
    if len(evaluated) != 1:
        raise ValueError(f"entry decision is not unique for {facts.code}")
    item = evaluated[0]
    context = item.context_assessment
    gates = evaluation.higher_timeframe_gates
    setup = item.setup.point
    trigger = item.trigger
    return {
        "signal_id": signal_id,
        "setup_point_type": setup.point_type,
        "setup_available_at": setup.available_at.isoformat(),
        "setup_anchor_at": setup.anchor_at.isoformat(),
        "setup_anchor_price": str(setup.structure_anchor_price),
        "setup_invalidation_price": str(setup.structure_invalidation_price),
        "trigger_point_type": None if trigger is None else trigger.point_type,
        "trigger_available_at": (
            None if trigger is None else trigger.available_at.isoformat()
        ),
        "trigger_anchor_at": None if trigger is None else trigger.anchor_at.isoformat(),
        "trigger_anchor_price": (
            None if trigger is None else str(trigger.structure_anchor_price)
        ),
        "trigger_divergence_kind": (
            None if trigger is None else trigger.divergence_kind
        ),
        "grade": "UNRESOLVED" if context is None else context.grade,
        "daily_stance": "unresolved" if context is None else context.daily_stance,
        "thirty_minute_stance": (
            "unresolved" if context is None else context.thirty_minute_stance
        ),
        "sector_regime": sector.regime,
        "market_gate": None if gates is None else gates.market.gate,
        "sector_gate": None if gates is None else gates.sector.gate,
        "symbol_gate": None if gates is None else gates.symbol.gate,
        "higher_timeframe_reason_codes": list(
            item.higher_timeframe_reason_codes
        ),
        "higher_timeframe_data_integrity_reason_codes": list(
            item.higher_timeframe_data_integrity_reason_codes
        ),
    }


def _initial_state(
    run: BacktestRun,
    facts: SymbolResearchFacts,
    position,
) -> PositionState:
    opened_at = (
        position.entry_at if hasattr(position, "entry_at") else position.opened_at
    )
    baseline_end = getattr(position, "exit_at", None) or datetime.combine(
        facts.requested_end,
        datetime.max.time(),
        tzinfo=opened_at.tzinfo,
    )
    terminal_stop = (
        position.exit_trigger_price
        if hasattr(position, "exit_trigger_price")
        else position.structural_stop
    )
    later_actions = tuple(
        factor.corporate_action()
        for factor in facts.factors
        if opened_at < factor.effective_at <= baseline_end
    )
    initial_stop = terminal_stop
    initial_entry = position.entry_price
    initial_shares = Decimal(position.shares)
    for action in reversed(later_actions):
        initial_stop *= action.raw_price_divisor
        initial_entry *= action.share_multiplier
        initial_shares /= action.share_multiplier
    if initial_shares != initial_shares.to_integral_value():
        raise ValueError(f"non-integral pre-action shares for {facts.code}")
    risk = initial_entry - initial_stop
    if risk <= 0:
        raise ValueError(f"non-positive initial structural risk for {facts.code}")
    return PositionState(
        code=facts.code,
        opened_at=opened_at,
        baseline_end=baseline_end,
        shares=int(initial_shares),
        entry_price=initial_entry,
        entry_fees=_entry_fee(run, facts.code, opened_at),
        reference_price=initial_entry,
        initial_risk=risk,
        hard_stop=initial_stop,
        effective_stop=initial_stop,
        high_water=initial_entry,
    )


def _apply_action(state: PositionState, action) -> None:
    old_shares = state.shares
    state.action_cash += (
        action.cash_per_share - action.subscription_cost_per_share
    ) * Decimal(old_shares)
    state.shares = int(Decimal(old_shares) * action.share_multiplier)
    state.entry_price /= action.share_multiplier
    for name in (
        "reference_price",
        "initial_risk",
        "hard_stop",
        "effective_stop",
        "high_water",
    ):
        setattr(state, name, getattr(state, name) / action.raw_price_divisor)


def _ratcheted_stop(state: PositionState, rule: Rule) -> Decimal:
    gain_r = (state.high_water - state.reference_price) / state.initial_risk
    if gain_r < rule.activate_at_r:
        return state.effective_stop
    if rule.kind == "break_even":
        candidate = state.reference_price
    elif rule.kind == "continuous":
        candidate = state.high_water - rule.trail_distance_r * state.initial_risk
    else:
        completed_r = gain_r.to_integral_value(rounding=ROUND_FLOOR)
        candidate = state.reference_price + (completed_r - ONE) * state.initial_risk
    return max(state.effective_stop, state.hard_stop, candidate)


def _direct_stop_fill(state: PositionState, bar, status, policy: ExecutionPolicy):
    order = OrderIntent(
        order_id=f"protective:{state.code}:{bar.closed_at.isoformat()}",
        signal_id=f"protective:{state.code}:{state.opened_at.isoformat()}",
        code=state.code,
        side="sell",
        shares=state.shares,
        created_at=bar.closed_at,
        structural_stop=state.effective_stop,
    )
    limit_down = _round_price(
        bar.previous_raw_close * (ONE - status.limit_pct),
        policy.price_tick,
    )
    blocked = (
        status.suspended
        or bar.volume <= 0
        or bar.raw_high <= limit_down
        or (policy.require_observed_price_range and bar.raw_high == bar.raw_low)
        or Decimal(state.shares) > bar.volume * policy.max_volume_participation
    )
    if blocked:
        return order, None
    reference = min(bar.raw_open, state.effective_stop)
    slippage = liquidity_slippage(
        replace(order, created_at=bar.opened_at),
        bar,
        policy,
    )
    price = _round_price(
        max(reference * (ONE - slippage), bar.raw_low, limit_down),
        policy.price_tick,
    )
    fees = fees_for(order, price, status, bar.opened_at.date(), policy)
    return order, (bar.closed_at, price, state.shares, fees)


def _simulate(
    run: BacktestRun,
    facts: SymbolResearchFacts,
    position,
    rule: Rule,
    bars: Sequence[object],
) -> dict[str, object]:
    state = _initial_state(run, facts, position)
    actions = tuple(
        factor.corporate_action()
        for factor in facts.factors
        if state.opened_at < factor.effective_at <= state.baseline_end
    )
    action_index = 0
    policy = ExecutionPolicy(require_observed_price_range=True)
    pending: OrderIntent | None = None
    trigger_price: Decimal | None = None
    exit_at = None
    exit_price = None
    exit_fees = ZERO
    remaining = state.shares
    exit_value = ZERO
    stop_kind = None
    for bar in bars:
        while action_index < len(actions) and actions[action_index].effective_at <= bar.closed_at:
            _apply_action(state, actions[action_index])
            remaining = state.shares
            action_index += 1
        status = _status_for_bar(bar, facts.security_master)
        if pending is not None:
            if status.t_plus_days and status.session <= state.opened_at.date():
                continue
            capacity = int(bar.volume * policy.max_volume_participation)
            capacity = capacity // status.lot_size * status.lot_size
            shares = remaining if not 0 < capacity < remaining else capacity
            attempted = replace(pending, shares=shares)
            decision = try_fill(attempted, bar, status, policy)
            if decision.filled and decision.execution_price is not None:
                exit_at = decision.filled_at
                exit_value += decision.execution_price * Decimal(decision.shares)
                exit_fees += decision.fees
                remaining -= decision.shares
                if remaining == 0:
                    exit_price = exit_value / Decimal(state.shares)
                    break
                pending = replace(pending, shares=remaining)
            continue
        if bar.raw_low <= state.effective_stop:
            trigger_price = state.effective_stop
            stop_kind = (
                "profit_protection"
                if state.effective_stop > state.hard_stop
                else "structural_stop"
            )
            order, direct = _direct_stop_fill(state, bar, status, policy)
            if status.t_plus_days and status.session <= state.opened_at.date():
                pending = order
            elif direct is None:
                pending = order
            else:
                exit_at, exit_price, _shares, exit_fees = direct
                exit_value = exit_price * Decimal(state.shares)
                remaining = 0
                break
        state.high_water = max(state.high_water, bar.raw_high)
        state.effective_stop = _ratcheted_stop(state, rule)

    if exit_price is None:
        baseline_pnl = getattr(position, "net_pnl", None)
        if baseline_pnl is None:
            baseline_pnl = (
                (position.last_price - position.entry_price)
                * Decimal(position.shares)
                - state.entry_fees
            )
        net_pnl = baseline_pnl + state.action_cash
        outcome = "baseline_exit" if hasattr(position, "exit_at") else "open_mark"
    else:
        entry_value = state.entry_price * Decimal(state.shares)
        net_pnl = (
            exit_value
            - exit_fees
            - entry_value
            - state.entry_fees
            + state.action_cash
        )
        outcome = f"{stop_kind}_exit"
    return {
        "code": state.code,
        "outcome": outcome,
        "exit_at": None if exit_at is None else exit_at.isoformat(),
        "exit_price": None if exit_price is None else str(exit_price),
        "trigger_price": None if trigger_price is None else str(trigger_price),
        "net_pnl": str(net_pnl.quantize(Decimal("0.01"))),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    directory = args.input_dir.resolve()
    run = _load_pickle(directory / "certified_portfolio_run.pkl", BacktestRun)
    positions = tuple(run.trades) + tuple(run.open_positions)
    facts_by_code = {
        position.code: _load_pickle(
            _fact_path(directory, position.code),
            SymbolResearchFacts,
        )
        for position in positions
    }
    bars_by_code = {}
    contexts_by_code = {}
    for position in positions:
        facts = facts_by_code[position.code]
        opened_at = (
            position.entry_at
            if hasattr(position, "entry_at")
            else position.opened_at
        )
        baseline_end = getattr(position, "exit_at", None) or datetime.combine(
            facts.requested_end,
            datetime.max.time(),
            tzinfo=opened_at.tzinfo,
        )
        source = _active_minute_source(
            position.code,
            requested_start=facts.requested_start,
            requested_end=facts.requested_end,
            after=opened_at,
        )
        bars = []
        while source.next_at is not None and source.next_at <= baseline_end:
            bars.append(source.pop())
        bars_by_code[position.code] = tuple(bars)
        contexts_by_code[position.code] = _entry_context(
            run,
            directory,
            facts,
            position,
        )
    baseline_pnl = run.equity_curve[-1].equity - run.equity_curve[0].equity
    rows = []
    for rule in RULES:
        outcomes_list = []
        for position in positions:
            outcome = _simulate(
                run,
                facts_by_code[position.code],
                position,
                rule,
                bars_by_code[position.code],
            )
            outcome["entry_context"] = contexts_by_code[position.code]
            outcomes_list.append(outcome)
        outcomes = tuple(outcomes_list)
        net_pnl = sum(Decimal(row["net_pnl"]) for row in outcomes)
        rows.append(
            {
                "rule_id": rule.rule_id,
                "net_pnl": str(net_pnl),
                "net_return_on_initial_cash": str(net_pnl / args.initial_cash),
                "delta_vs_certified_baseline": str(net_pnl - baseline_pnl),
                "protective_exit_count": sum(
                    row["outcome"] == "profit_protection_exit"
                    for row in outcomes
                ),
                "positions": outcomes,
            }
        )
    print(
        json.dumps(
            {
                "schema": "chanlun-protective-stop-diagnostic-v1",
                "diagnostic_only": True,
                "causal_update_contract": (
                    "completed_1m_high_updates_stop_for_the_next_1m_bar"
                ),
                "certified_baseline_net_pnl": str(baseline_pnl),
                "position_count": len(positions),
                "rules": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
