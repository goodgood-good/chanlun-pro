from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.v3_bar_execution import (
    BarProxyExecutionStatus,
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.v3_decision import (
    StrategicSignalFacts,
    SystemHealthFacts,
    TacticalSignalFacts,
    V3DecisionCore,
    V3DecisionInput,
)
from chanlun.decision_support.trading_system.v3_execution import (
    V3FeeModel,
    V3FeeRateAt,
)
from chanlun.decision_support.trading_system.v3_direct_recursive_structure import (
    DirectRecursiveEntryChain,
    direct_recursive_alignment_contract,
)
from chanlun.decision_support.trading_system.v3_multisymbol_replay import (
    ReplayBatch,
    ReplayDecisionEvent,
    ReplayFactBindings,
    ReplayMandatoryShareActionFact,
    ReplayPriceFact,
    StrictV3MultiSymbolReplayEngine,
    V3_ETF_REQUIRED_CANDIDATE_GATES,
    V3_INDIVIDUAL_REQUIRED_CANDIDATE_GATES,
    research_individual_direct_replay_contract,
    strict_v3_direct_replay_contract,
    strict_v3_replay_contract,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    etf_parameter_snapshot,
    individual_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v3_selection import (
    CandidateDecision,
    GateCheck,
)
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    V31_ALIGNMENT_CONTRACT_ID,
    V31AlignedEntryChain,
)
from tools.backtest_chanlun_v3_multisymbol_events import (
    INPUT_SCHEMA,
    _jsonable,
    run_payload,
)


CN = ZoneInfo("Asia/Shanghai")
D0 = date(2026, 7, 20)


def at(session: date, hour: int, minute: int) -> datetime:
    return datetime(
        session.year,
        session.month,
        session.day,
        hour,
        minute,
        tzinfo=CN,
    )


def healthy() -> SystemHealthFacts:
    return SystemHealthFacts(True, True, True, True, True)


def fee_model() -> V3FeeModel:
    return V3FeeModel(
        schedule_id="fees:v1",
        rates=(
            V3FeeRateAt(
                effective_from=date(2020, 1, 1),
                commission_rate=Decimal("0.0003"),
                minimum_commission=Decimal("5"),
                stock_sell_stamp_rate=Decimal("0.001"),
                transfer_rate=Decimal("0"),
            ),
        ),
    )


def status(
    session: date,
    decision: datetime,
    *,
    sellable: int = 0,
    corporate_complete: bool = True,
) -> BarProxyExecutionStatus:
    return BarProxyExecutionStatus(
        known_at=decision - timedelta(seconds=1),
        effective_session=session,
        listed=True,
        suspended=False,
        continuity_active=True,
        point_in_time_state_complete=True,
        corporate_action_state_complete=corporate_complete,
        sellable_quantity=sellable,
        limit_up=Decimal("15"),
        limit_down=Decimal("5"),
        buy_quantity_increment=100,
        sell_quantity_increment=100,
        fee_schedule_id="fees:v1",
    )


def bar(
    symbol: str,
    opened_at: datetime,
    sequence: int,
    *,
    low: str,
    high: str,
    volume: str,
    close: str | None = None,
) -> HistoricalMinuteExecutionBar:
    low_value = Decimal(low)
    high_value = Decimal(high)
    close_value = Decimal(close) if close is not None else (low_value + high_value) / 2
    return HistoricalMinuteExecutionBar(
        symbol=symbol,
        opened_at=opened_at,
        closed_at=opened_at + timedelta(minutes=1),
        sequence=sequence,
        raw_open=close_value,
        raw_high=high_value,
        raw_low=low_value,
        raw_close=close_value,
        raw_volume=Decimal(volume),
        source_id=f"bar:{symbol}:{opened_at.isoformat()}:{sequence}",
    )


def candidate(symbol: str, confirmation: datetime) -> CandidateDecision:
    return CandidateDecision(
        symbol=symbol,
        parameter_set_id=etf_parameter_snapshot().parameter_set_id,
        selection_path="ETF_PROXY",
        accepted=True,
        checks=tuple(
            GateCheck(gate, True, f"PASS_TEST_{gate.upper()}", "frozen test fact")
            for gate in sorted(V3_ETF_REQUIRED_CANDIDATE_GATES)
        ),
        fundamental_role="ETF_PROXY",
        relative_value_status="ETF_PROXY",
        sector_strength=Decimal("9"),
        confirmation_time=confirmation,
    )


def chain(symbol: str, decision: datetime, *, boundary: str = "10") -> V31AlignedEntryChain:
    return V31AlignedEntryChain(
        l0_point_id=f"{symbol}:l0-point",
        l0_center_id=f"{symbol}:l0-center",
        l1_departure_evidence_id=f"{symbol}:l1-departure",
        l1_return_evidence_id=f"{symbol}:l1-return",
        l1_evidence_kind="COMPLETED_CONSTITUENT_UNIT",
        l2_locator_point_id=f"{symbol}:l2-locator",
        decision_at=decision,
        return_low=Decimal("8.5"),
        l0_zg=Decimal("8"),
        l2_confirmation_bar_high=Decimal(boundary),
        structural_invalidation_price=Decimal("8"),
    )


def entry_facts(
    symbol: str,
    decision: datetime,
    *,
    quantity: int = 100,
    boundary: str = "10",
) -> V3DecisionInput:
    return V3DecisionInput(
        symbol=symbol,
        decision_time=decision,
        confirmation_time=decision,
        structure_snapshot_id=f"{symbol}:structure",
        selection_snapshot_id=f"{symbol}:selection",
        account_snapshot_id=f"{symbol}:account:{decision.isoformat()}",
        strategic_state="S_ENTRY_READY",
        health=healthy(),
        strategic=StrategicSignalFacts(),
        tactical=TacticalSignalFacts(),
        cycle_ledger=None,
        candidate=candidate(symbol, decision),
        q_plan=quantity,
        price_cap_or_floor=Decimal(boundary),
    )


def entry_bindings(
    symbol: str,
    decision: datetime,
    *,
    boundary: str = "10",
) -> ReplayFactBindings:
    contract = strict_v3_replay_contract()
    aligned = chain(symbol, decision, boundary=boundary)
    return ReplayFactBindings(
        timeframe_override_parameter_set_id=(
            contract.timeframe_override_parameter_set_id
        ),
        alignment_contract_id=contract.effective_alignment_contract_id,
        alignment_parameter_set_id=(
            contract.effective_alignment_parameter_set_id
        ),
        frozen_structure_fact_ids=(
            f"{symbol}:structure",
            aligned.l0_point_id,
            aligned.l0_center_id,
            aligned.l1_departure_evidence_id,
            aligned.l1_return_evidence_id,
            aligned.l2_locator_point_id,
        ),
        selection_fact_ids=(f"{symbol}:selection",),
        risk_fact_ids=(f"{symbol}:risk",),
        aligned_entry_chain=aligned,
    )


def held_facts(
    symbol: str,
    decision: datetime,
    *,
    strategic: StrategicSignalFacts | None = None,
    tactical: TacticalSignalFacts | None = None,
    boundary: str = "9",
) -> V3DecisionInput:
    return V3DecisionInput(
        symbol=symbol,
        decision_time=decision,
        confirmation_time=decision,
        structure_snapshot_id=f"{symbol}:structure:{decision.isoformat()}",
        selection_snapshot_id=None,
        account_snapshot_id=f"{symbol}:account:{decision.isoformat()}",
        strategic_state="S_ACTIVE_FULL",
        health=healthy(),
        strategic=strategic or StrategicSignalFacts(),
        tactical=tactical or TacticalSignalFacts(),
        cycle_ledger=None,
        candidate=None,
        q_plan=0,
        price_cap_or_floor=Decimal(boundary),
    )


def held_bindings(
    facts: V3DecisionInput,
    *,
    include_risk: bool = False,
) -> ReplayFactBindings:
    contract = strict_v3_replay_contract()
    return ReplayFactBindings(
        timeframe_override_parameter_set_id=(
            contract.timeframe_override_parameter_set_id
        ),
        alignment_contract_id=None,
        alignment_parameter_set_id=None,
        frozen_structure_fact_ids=(facts.structure_snapshot_id,),
        selection_fact_ids=(),
        risk_fact_ids=((f"{facts.symbol}:risk",) if include_risk else ()),
    )


def event(
    event_id: str,
    facts: V3DecisionInput,
    bindings: ReplayFactBindings,
    *,
    execution_status: BarProxyExecutionStatus,
    broker_position: int,
    bars: tuple[HistoricalMinuteExecutionBar, ...],
    optional: bool,
    persistent_intent_id: str | None = None,
) -> ReplayDecisionEvent:
    return ReplayDecisionEvent(
        event_id=event_id,
        facts=facts,
        bindings=bindings,
        created_at=facts.decision_time,
        broker_confirmed_at=facts.decision_time,
        expires_at=(
            facts.decision_time + timedelta(minutes=10) if optional else None
        ),
        execution_status=execution_status,
        broker_position_quantity=broker_position,
        bars=bars,
        persistent_intent_id=persistent_intent_id,
    )


def decision_mark(symbol: str, observed_at: datetime, price: str = "10") -> ReplayPriceFact:
    return ReplayPriceFact(
        symbol=symbol,
        available_at=observed_at,
        raw_close=Decimal(price),
        source_id=f"mark:{symbol}:{observed_at.isoformat()}",
    )


class CountingCore(V3DecisionCore):
    def __init__(self) -> None:
        self.calls = 0

    def decide(self, facts: V3DecisionInput):  # type: ignore[no-untyped-def]
        self.calls += 1
        return super().decide(facts)


def engine(started_at: datetime, core: V3DecisionCore | None = None):  # type: ignore[no-untyped-def]
    return StrictV3MultiSymbolReplayEngine(
        initial_cash=Decimal("1000000"),
        started_at=started_at,
        fee_model=fee_model(),
        decision_core=core,
    )


def test_contract_binds_parent_v3_etf_override_alignment_v2_and_tactical() -> None:
    contract = strict_v3_replay_contract()
    parent = etf_parameter_snapshot()
    assert contract.strategy_parameter_set_id == parent.parameter_set_id
    assert contract.selection_path == "ETF_PROXY"
    assert contract.effective_alignment_contract_id == V31_ALIGNMENT_CONTRACT_ID
    assert contract.effective_alignment_contract_id.endswith("_V2")
    assert contract.tactical_ratio == parent.tactical_ratio == Decimal("0.25")
    assert contract.slot_count == 5
    assert contract.live_status == "LIVE_DISABLED"


def test_direct_recursive_contract_uses_same_decision_and_execution_core() -> None:
    decision = at(D0, 10, 0)
    symbol = "SH.510300"
    contract = strict_v3_direct_replay_contract()
    alignment = direct_recursive_alignment_contract()
    chain = DirectRecursiveEntryChain(
        l0_point_id=f"{symbol}:raw-level-2-point",
        l0_center_id=f"{symbol}:raw-level-2-center",
        l1_departure_unit_id=f"{symbol}:raw-level-2-leave",
        l1_return_unit_id=f"{symbol}:raw-level-2-return",
        l2_locator_point_id=f"{symbol}:raw-level-0-locator",
        decision_at=decision,
        first_return_low=Decimal("8.5"),
        l0_zg=Decimal("8"),
        l2_confirmation_bar_high=Decimal("10"),
        structural_invalidation_price=Decimal("8"),
        provenance_unit_ids=(f"{symbol}:raw-level-0-leaf",),
    )
    facts = entry_facts(symbol, decision)
    bindings = ReplayFactBindings(
        timeframe_override_parameter_set_id=None,
        alignment_contract_id=contract.effective_alignment_contract_id,
        alignment_parameter_set_id=contract.effective_alignment_parameter_set_id,
        frozen_structure_fact_ids=(
            facts.structure_snapshot_id,
            chain.l0_point_id,
            chain.l0_center_id,
            chain.l1_departure_unit_id,
            chain.l1_return_unit_id,
            chain.l2_locator_point_id,
            *chain.provenance_unit_ids,
        ),
        selection_fact_ids=(f"{symbol}:selection",),
        risk_fact_ids=(f"{symbol}:risk",),
        aligned_entry_chain=chain,
    )
    core = CountingCore()
    result = StrictV3MultiSymbolReplayEngine(
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
        fee_model=fee_model(),
        decision_core=core,
        contract=contract,
    ).replay(
        (
            ReplayBatch(
                batch_id="direct-entry",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=(
                    event(
                        "entry:direct",
                        facts,
                        bindings,
                        execution_status=status(D0, decision),
                        broker_position=0,
                        bars=(
                            bar(
                                symbol,
                                decision,
                                1,
                                low="9.7",
                                high="9.9",
                                volume="2000",
                            ),
                        ),
                        optional=True,
                    ),
                ),
                decision_marks=(decision_mark(symbol, decision),),
            ),
        )
    )

    assert alignment.contract_id == contract.effective_alignment_contract_id
    assert contract.accepted_recursive_level == 2
    assert contract.timeframe_override_parameter_set_id is None
    assert core.calls == 1
    assert result.intents[0].fact_gate_reason_codes == ()
    assert result.metrics.fill_count == 1


def test_research_individual_contract_uses_stock_order_and_shared_core() -> None:
    decision = at(D0, 10, 0)
    symbol = "SH.600000"
    contract = research_individual_direct_replay_contract(
        "sha256:" + "a" * 64
    )
    alignment = direct_recursive_alignment_contract()
    chain = DirectRecursiveEntryChain(
        l0_point_id=f"{symbol}:l0-point",
        l0_center_id=f"{symbol}:l0-center",
        l1_departure_unit_id=f"{symbol}:l1-leave",
        l1_return_unit_id=f"{symbol}:l1-return",
        l2_locator_point_id=f"{symbol}:l2-locator",
        decision_at=decision,
        first_return_low=Decimal("8.5"),
        l0_zg=Decimal("8"),
        l2_confirmation_bar_high=Decimal("10"),
        structural_invalidation_price=Decimal("8"),
        provenance_unit_ids=(f"{symbol}:leaf",),
    )
    candidate_value = CandidateDecision(
        symbol=symbol,
        parameter_set_id=individual_parameter_snapshot().parameter_set_id,
        selection_path="INDIVIDUAL_THREE_PROGRAM",
        accepted=True,
        checks=tuple(
            GateCheck(gate, True, f"PASS_TEST_{gate.upper()}", "frozen proxy fact")
            for gate in sorted(V3_INDIVIDUAL_REQUIRED_CANDIDATE_GATES)
        ),
        fundamental_role="LEADER",
        relative_value_status="UNDERVALUED",
        sector_strength=Decimal("9"),
        confirmation_time=decision,
    )
    facts = replace(entry_facts(symbol, decision), candidate=candidate_value)
    bindings = ReplayFactBindings(
        timeframe_override_parameter_set_id=None,
        alignment_contract_id=alignment.contract_id,
        alignment_parameter_set_id=alignment.parameter_set_id,
        frozen_structure_fact_ids=(
            facts.structure_snapshot_id,
            chain.l0_point_id,
            chain.l0_center_id,
            chain.l1_departure_unit_id,
            chain.l1_return_unit_id,
            chain.l2_locator_point_id,
            *chain.provenance_unit_ids,
        ),
        selection_fact_ids=(f"{symbol}:selection",),
        risk_fact_ids=(f"{symbol}:risk",),
        aligned_entry_chain=chain,
    )
    core = CountingCore()
    result = StrictV3MultiSymbolReplayEngine(
        initial_cash=Decimal("1000000"),
        started_at=at(D0, 9, 30),
        fee_model=fee_model(),
        decision_core=core,
        contract=contract,
    ).replay(
        (
            ReplayBatch(
                batch_id="research-individual-entry",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=(
                    event(
                        "entry:research-individual",
                        facts,
                        bindings,
                        execution_status=status(D0, decision),
                        broker_position=0,
                        bars=(
                            bar(
                                symbol,
                                decision,
                                1,
                                low="9.7",
                                high="9.9",
                                volume="2000",
                            ),
                        ),
                        optional=True,
                    ),
                ),
                decision_marks=(decision_mark(symbol, decision),),
            ),
        )
    )

    assert core.calls == 1
    assert result.contract == contract
    assert result.intents[0].fact_gate_reason_codes == ()
    assert result.orders[0].order.instrument_kind == "A_SHARE_STOCK"
    assert result.orders[0].order.parameter_set_id == (
        individual_parameter_snapshot().parameter_set_id
    )
    assert result.metrics.fill_count == 1


def test_six_simultaneous_entries_call_one_core_and_only_fill_five_slots() -> None:
    decision = at(D0, 10, 0)
    symbols = tuple(f"SH.51000{index}" for index in range(1, 7))
    events = tuple(
        event(
            f"entry:{symbol}",
            entry_facts(symbol, decision),
            entry_bindings(symbol, decision),
            execution_status=status(D0, decision),
            broker_position=0,
            bars=(
                bar(
                    symbol,
                    decision,
                    1,
                    low="9.7",
                    high="9.9",
                    volume="2000",
                ),
            ),
            optional=True,
        )
        for symbol in symbols
    )
    core = CountingCore()
    result = engine(at(D0, 9, 30), core).replay(
        (
            ReplayBatch(
                batch_id="six-entries",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=events,
                decision_marks=tuple(
                    decision_mark(symbol, decision) for symbol in symbols
                ),
            ),
        )
    )
    assert core.calls == 6
    assert len(result.intents) == 6
    assert len(result.orders) == 5
    assert len(result.positions) == 5
    assert tuple(position.slot_number for position in result.positions) == (1, 2, 3, 4, 5)
    assert result.metrics.fill_count == 5
    assert any(
        "FIVE_SLOT_CAP_REACHED" in rejection.reason_codes
        for rejection in result.rejections
    )


def test_unfilled_ranked_entry_still_reserves_same_batch_slot() -> None:
    """A later bar outcome must not reallocate a slot decided at the same time."""

    decision = at(D0, 10, 0)
    symbols = tuple(f"SH.51000{index}" for index in range(1, 7))
    events = tuple(
        event(
            f"reserved-entry:{symbol}",
            entry_facts(symbol, decision),
            entry_bindings(symbol, decision),
            execution_status=status(D0, decision),
            broker_position=0,
            bars=(
                bar(
                    symbol,
                    decision,
                    1,
                    # The first, highest-ranked order never crosses its buy
                    # limit.  That future outcome must not expose its slot to
                    # the sixth candidate from the same decision snapshot.
                    low="10.1" if symbol == symbols[0] else "9.7",
                    high="10.2" if symbol == symbols[0] else "9.9",
                    volume="2000",
                ),
            ),
            optional=True,
        )
        for symbol in symbols
    )
    result = engine(at(D0, 9, 30), CountingCore()).replay(
        (
            ReplayBatch(
                batch_id="six-entries-one-unfilled",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=events,
                decision_marks=tuple(
                    decision_mark(symbol, decision) for symbol in symbols
                ),
            ),
        )
    )

    assert len(result.orders) == 5
    assert result.orders[0].match.filled_quantity == 0
    assert tuple(position.symbol for position in result.positions) == symbols[1:5]
    assert any(
        rejection.symbol == symbols[5]
        and "FIVE_SLOT_CAP_REACHED" in rejection.reason_codes
        for rejection in result.rejections
    )


def test_entry_scheduler_shrinks_to_decision_time_slot_cap() -> None:
    """Q_PLAN is shrunk, not rejected, when one full requested lot exceeds U_SLOT."""

    decision = at(D0, 10, 0)
    symbol = "SH.510300"
    replay = StrictV3MultiSymbolReplayEngine(
        initial_cash=Decimal("100000"),
        started_at=at(D0, 9, 30),
        fee_model=fee_model(),
    )
    result = replay.replay(
        (
            ReplayBatch(
                batch_id="entry-shrinks-to-slot-cap",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=(
                    event(
                        "entry:shrink-to-slot",
                        entry_facts(symbol, decision, quantity=2000),
                        entry_bindings(symbol, decision),
                        execution_status=status(D0, decision),
                        broker_position=0,
                        bars=(
                            bar(
                                symbol,
                                decision,
                                1,
                                low="9.7",
                                high="9.9",
                                volume="100000",
                            ),
                        ),
                        optional=True,
                    ),
                ),
                decision_marks=(decision_mark(symbol, decision),),
            ),
        )
    )

    record = result.intents[0]
    assert record.intent.quantity == 2000
    assert record.requested_quantity == 2000
    assert record.scheduled_quantity == 1800
    assert record.reserved_cash_at_decision == Decimal("18005.40")
    assert record.reserved_slot_number == 1
    assert "SCHEDULED_QUANTITY_REDUCED" in record.scheduler_reason_codes
    assert result.orders[0].order.quantity == 1800


def test_same_batch_exit_fill_does_not_fund_or_free_slot_for_entry() -> None:
    """Future sale proceeds and slot release cannot alter the same snapshot."""

    entry_at = at(D0, 10, 0)
    held_symbols = tuple(f"SH.51000{index}" for index in range(1, 6))
    initial_events = tuple(
        event(
            f"initial-entry:{symbol}",
            entry_facts(symbol, entry_at),
            entry_bindings(symbol, entry_at),
            execution_status=status(D0, entry_at),
            broker_position=0,
            bars=(
                bar(
                    symbol,
                    entry_at,
                    1,
                    low="9.7",
                    high="9.9",
                    volume="2000",
                ),
            ),
            optional=True,
        )
        for symbol in held_symbols
    )
    next_session = D0 + timedelta(days=1)
    rebalance_at = at(next_session, 10, 0)
    exit_facts = held_facts(
        held_symbols[0],
        rebalance_at,
        strategic=StrategicSignalFacts(l0_third_sell=True),
        boundary="9",
    )
    new_symbol = "SH.510006"
    result = engine(at(D0, 9, 30), CountingCore()).replay(
        (
            ReplayBatch(
                batch_id="fill-five-slots",
                decision_at=entry_at,
                valuation_at=entry_at + timedelta(minutes=1),
                events=initial_events,
                decision_marks=tuple(
                    decision_mark(symbol, entry_at) for symbol in held_symbols
                ),
            ),
            ReplayBatch(
                batch_id="exit-and-entry-same-decision",
                decision_at=rebalance_at,
                valuation_at=rebalance_at + timedelta(minutes=1),
                events=(
                    event(
                        "exit:first-slot",
                        exit_facts,
                        held_bindings(exit_facts),
                        execution_status=status(
                            next_session,
                            rebalance_at,
                            sellable=100,
                        ),
                        broker_position=100,
                        bars=(
                            bar(
                                held_symbols[0],
                                rebalance_at,
                                1,
                                low="9.1",
                                high="9.3",
                                volume="2000",
                            ),
                        ),
                        optional=False,
                        persistent_intent_id="persistent-exit:first-slot",
                    ),
                    event(
                        "entry:same-time-as-exit",
                        entry_facts(new_symbol, rebalance_at),
                        entry_bindings(new_symbol, rebalance_at),
                        execution_status=status(next_session, rebalance_at),
                        broker_position=0,
                        bars=(
                            bar(
                                new_symbol,
                                rebalance_at,
                                1,
                                low="9.7",
                                high="9.9",
                                volume="2000",
                            ),
                        ),
                        optional=True,
                    ),
                ),
                decision_marks=(
                    *(
                        decision_mark(symbol, rebalance_at)
                        for symbol in held_symbols
                    ),
                    decision_mark(new_symbol, rebalance_at),
                ),
            ),
        )
    )

    assert tuple(order.event_id for order in result.orders[-1:]) == (
        "exit:first-slot",
    )
    assert result.orders[-1].match.filled_quantity == 100
    assert new_symbol not in {position.symbol for position in result.positions}
    assert len(result.positions) == 4
    assert any(
        rejection.event_id == "entry:same-time-as-exit"
        and "FIVE_SLOT_CAP_REACHED" in rejection.reason_codes
        for rejection in result.rejections
    )


def test_simultaneous_entries_compete_by_shared_candidate_rank_before_symbol() -> None:
    decision = at(D0, 10, 0)
    symbols = tuple(f"SH.51000{index}" for index in range(1, 7))
    strengths = {
        symbol: Decimal(index)
        for index, symbol in enumerate(symbols, start=1)
    }
    events = []
    for symbol in symbols:
        facts = entry_facts(symbol, decision)
        assert facts.candidate is not None
        facts = replace(
            facts,
            candidate=replace(
                facts.candidate,
                sector_strength=strengths[symbol],
            ),
        )
        events.append(
            event(
                f"ranked-entry:{symbol}",
                facts,
                entry_bindings(symbol, decision),
                execution_status=status(D0, decision),
                broker_position=0,
                bars=(
                    bar(
                        symbol,
                        decision,
                        1,
                        low="9.7",
                        high="9.9",
                        volume="2000",
                    ),
                ),
                optional=True,
            )
        )
    result = engine(at(D0, 9, 30), CountingCore()).replay(
        (
            ReplayBatch(
                batch_id="six-ranked-entries",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=tuple(events),
                decision_marks=tuple(
                    decision_mark(symbol, decision) for symbol in symbols
                ),
            ),
        )
    )

    assert tuple(position.symbol for position in result.positions) == tuple(
        reversed(symbols[1:])
    )
    assert any(
        rejection.symbol == symbols[0]
        and "FIVE_SLOT_CAP_REACHED" in rejection.reason_codes
        for rejection in result.rejections
    )


def test_simultaneous_entries_allocate_green_risk_before_stronger_amber() -> None:
    """§3.3: GREEN 先占槽位，AMBER 不得凭板块强度越级。"""

    decision = at(D0, 10, 0)
    symbols = tuple(f"SH.51000{index}" for index in range(1, 7))
    events = []
    for index, symbol in enumerate(symbols, start=1):
        facts = entry_facts(symbol, decision)
        assert facts.candidate is not None
        facts = replace(
            facts,
            candidate=replace(
                facts.candidate,
                sector_strength=Decimal(index),
                higher_timeframe_risk_buyable=symbol == symbols[0],
            ),
        )
        events.append(
            event(
                f"risk-ranked-entry:{symbol}",
                facts,
                entry_bindings(symbol, decision),
                execution_status=status(D0, decision),
                broker_position=0,
                bars=(
                    bar(
                        symbol,
                        decision,
                        1,
                        low="9.7",
                        high="9.9",
                        volume="2000",
                    ),
                ),
                optional=True,
            )
        )
    result = engine(at(D0, 9, 30), CountingCore()).replay(
        (
            ReplayBatch(
                batch_id="six-risk-ranked-entries",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=tuple(events),
                decision_marks=tuple(
                    decision_mark(symbol, decision) for symbol in symbols
                ),
            ),
        )
    )

    assert tuple(position.symbol for position in result.positions) == (
        symbols[0],
        *reversed(symbols[2:]),
    )
    assert any(
        rejection.symbol == symbols[1]
        and "FIVE_SLOT_CAP_REACHED" in rejection.reason_codes
        for rejection in result.rejections
    )


def test_missing_alignment_fact_fails_closed_after_shared_core_decision() -> None:
    decision = at(D0, 10, 0)
    symbol = "SH.510300"
    core = CountingCore()
    bindings = replace(
        entry_bindings(symbol, decision),
        all_required_facts_resolved=False,
        unresolved_reason_codes=("ALIGNMENT_ARTIFACT_MISSING",),
    )
    result = engine(at(D0, 9, 30), core).replay(
        (
            ReplayBatch(
                batch_id="unresolved",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=1),
                events=(
                    event(
                        "entry:unresolved",
                        entry_facts(symbol, decision),
                        bindings,
                        execution_status=status(D0, decision),
                        broker_position=0,
                        bars=(
                            bar(
                                symbol,
                                decision,
                                1,
                                low="9.7",
                                high="9.9",
                                volume="2000",
                            ),
                        ),
                        optional=True,
                    ),
                ),
                decision_marks=(decision_mark(symbol, decision),),
            ),
        )
    )
    assert core.calls == 1
    assert result.intents[0].intent.action == "ENTRY_INTENT"
    assert result.orders == ()
    assert result.positions == ()
    assert "UNRESOLVED_ALIGNMENT_ARTIFACT_MISSING" in result.unresolved_reason_codes
    assert not result.metrics.valid


def test_completed_minute_matcher_ignores_signal_touch_and_mixed_bars() -> None:
    decision = at(D0, 10, 0)
    symbol = "SH.510500"
    bars = (
        bar(
            symbol,
            decision - timedelta(minutes=1),
            0,
            low="9.5",
            high="9.8",
            volume="100000",
        ),
        bar(symbol, decision, 1, low="10", high="10.1", volume="100000"),
        bar(
            symbol,
            decision + timedelta(minutes=1),
            2,
            low="9.8",
            high="10.1",
            volume="100000",
        ),
        bar(
            symbol,
            decision + timedelta(minutes=2),
            3,
            low="9.7",
            high="9.9",
            volume="2000",
        ),
        bar(
            symbol,
            decision + timedelta(minutes=3),
            4,
            low="9.6",
            high="9.8",
            volume="2000",
        ),
    )
    replay_event = event(
        "entry:partial",
        entry_facts(symbol, decision, quantity=300),
        entry_bindings(symbol, decision),
        execution_status=status(D0, decision),
        broker_position=0,
        bars=bars,
        optional=True,
    )
    replay_event = replace(
        replay_event,
        expires_at=decision + timedelta(minutes=4),
    )
    result = engine(at(D0, 9, 30)).replay(
        (
            ReplayBatch(
                batch_id="strict-bars",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=4),
                events=(replay_event,),
                decision_marks=(decision_mark(symbol, decision),),
            ),
        )
    )
    match = result.orders[0].match
    assert match.filled_quantity == 200
    assert match.remaining_quantity == 100
    assert tuple(fill.bar_sequence for fill in match.fills) == (3, 4)
    assert match.exact_limit_touch_bars == 1
    assert match.ambiguous_intrabar_cross_bars == 1
    assert match.total_fees == Decimal("5.00")
    position = result.positions[0]
    assert position.quantity == 200
    assert position.entry_cash > 0
    assert position.turnover_notional > 0
    assert position.tactical_cycles_completed == 0
    assert position.last_price == Decimal("9.7")
    assert position.market_value == Decimal("1940")
    assert position.marked_at == decision + timedelta(minutes=4)
    assert position.mark_complete is True


def test_strategic_exit_is_t1_blocked_then_persists_to_next_session() -> None:
    symbol = "SH.510880"
    entry_at = at(D0, 10, 0)
    exit_at = at(D0, 11, 0)
    next_session = D0 + timedelta(days=1)
    continue_at = at(next_session, 10, 0)
    persistent_intent_id = "v3-persistent-exit:SH.510880:point-1"
    entry_event = event(
        "entry:t1",
        entry_facts(symbol, entry_at, quantity=400),
        entry_bindings(symbol, entry_at),
        execution_status=status(D0, entry_at),
        broker_position=0,
        bars=(
            bar(
                symbol,
                entry_at,
                1,
                low="9.7",
                high="9.9",
                volume="8000",
            ),
        ),
        optional=True,
    )
    first_exit_facts = held_facts(
        symbol,
        exit_at,
        strategic=StrategicSignalFacts(l0_third_sell=True),
    )
    first_exit = event(
        "exit:t1-blocked",
        first_exit_facts,
        held_bindings(first_exit_facts),
        execution_status=status(D0, exit_at, sellable=0),
        broker_position=400,
        bars=(
            bar(
                symbol,
                exit_at,
                2,
                low="9.4",
                high="9.6",
                volume="8000",
            ),
        ),
        optional=False,
        persistent_intent_id=persistent_intent_id,
    )
    continuing_facts = held_facts(
        symbol,
        continue_at,
        strategic=StrategicSignalFacts(existing_persistent_exit=True),
    )
    continuing_exit = event(
        "exit:t1-next-session",
        continuing_facts,
        held_bindings(continuing_facts),
        execution_status=status(next_session, continue_at, sellable=400),
        broker_position=400,
        bars=(
            bar(
                symbol,
                continue_at,
                3,
                low="9.4",
                high="9.6",
                volume="8000",
            ),
        ),
        optional=False,
        persistent_intent_id=persistent_intent_id,
    )
    result = engine(at(D0, 9, 30)).replay(
        (
            ReplayBatch(
                "entry",
                entry_at,
                entry_at + timedelta(minutes=1),
                (entry_event,),
                (decision_mark(symbol, entry_at),),
            ),
            ReplayBatch(
                "blocked-exit",
                exit_at,
                exit_at + timedelta(minutes=1),
                (first_exit,),
                (decision_mark(symbol, exit_at, "9.5"),),
            ),
            ReplayBatch(
                "completed-exit",
                continue_at,
                continue_at + timedelta(minutes=1),
                (continuing_exit,),
                (decision_mark(symbol, continue_at, "9.5"),),
            ),
        )
    )
    assert result.orders[1].match.filled_quantity == 0
    assert "T_PLUS_ONE_OR_SELLABLE_QUANTITY_BLOCK" in (
        result.orders[1].match.rejection_and_unfilled_reasons
    )
    assert result.orders[2].match.filled_quantity == 400
    assert result.orders[1].order.intent_id == persistent_intent_id
    assert result.orders[2].order.intent_id == persistent_intent_id
    assert result.orders[1].order.client_order_id != result.orders[2].order.client_order_id
    assert result.positions == ()
    assert len(result.closed_cycles) == 1
    assert result.metrics.strategic_cycle_count == 1
    assert result.metrics.total_fees == Decimal("10.00")
    assert result.resolved_persistent_intent_ids == (persistent_intent_id,)
    assert result.suppressed_persistent_event_counts == ()
    assert result.live_status == "LIVE_DISABLED"


def test_verified_flat_persistent_exit_is_an_idempotent_noop() -> None:
    """A retried exit after a fill/unfilled entry must not poison the ledger."""

    symbol = "SH.510880"
    decision = at(D0, 10, 0)
    facts = held_facts(
        symbol,
        decision,
        strategic=StrategicSignalFacts(l0_third_sell=True),
    )
    flat_exit = event(
        "exit:already-flat",
        facts,
        held_bindings(facts),
        execution_status=status(D0, decision, sellable=0),
        broker_position=0,
        bars=(),
        optional=False,
        persistent_intent_id="v3-persistent-exit:already-flat",
    )
    retry_at = decision + timedelta(minutes=30)
    retry_facts = held_facts(
        symbol,
        retry_at,
        strategic=StrategicSignalFacts(l0_third_sell=True),
    )
    flat_retry = event(
        "exit:already-flat:retry",
        retry_facts,
        held_bindings(retry_facts),
        execution_status=status(D0, retry_at, sellable=0),
        broker_position=0,
        bars=(),
        optional=False,
        persistent_intent_id="v3-persistent-exit:already-flat",
    )

    result = engine(at(D0, 9, 30)).replay(
        (
            ReplayBatch(
                "already-flat",
                decision,
                decision + timedelta(minutes=1),
                (flat_exit,),
                (decision_mark(symbol, decision),),
            ),
            ReplayBatch(
                "already-flat-retry",
                retry_at,
                retry_at + timedelta(minutes=1),
                (flat_retry,),
                (decision_mark(symbol, retry_at),),
            ),
        )
    )

    assert len(result.intents) == 1
    assert result.intents[0].intent.action == "STRATEGIC_EXIT_INTENT"
    assert result.intents[0].intent.quantity == 0
    assert result.intents[0].fact_gate_reason_codes == ()
    assert result.orders == ()
    assert result.rejections == ()
    assert result.unresolved_reason_codes == ()
    assert result.resolved_persistent_intent_ids == (
        "v3-persistent-exit:already-flat",
    )
    assert result.suppressed_persistent_event_counts == (
        ("v3-persistent-exit:already-flat", 1),
    )
    assert result.metrics.ledger_valid is True
    assert result.metrics.empty_replay is True


def test_zero_lot_tactical_inventory_retires_persistent_sell_retries() -> None:
    """A 100-share position has no legal 25% tactical lot to keep retrying."""

    symbol = "SZ.300880"
    entry_at = at(D0, 10, 0)
    next_session = D0 + timedelta(days=1)
    sell_at = at(next_session, 10, 0)
    retry_at = sell_at + timedelta(minutes=30)
    persistent_id = "v3-persistent-tactical-sell:zero-lot"
    entry_event = event(
        "entry:zero-tactical-lot",
        entry_facts(symbol, entry_at, quantity=100),
        entry_bindings(symbol, entry_at),
        execution_status=status(D0, entry_at),
        broker_position=0,
        bars=(bar(symbol, entry_at, 1, low="9.7", high="9.9", volume="4000"),),
        optional=True,
    )

    def tactical_exit(at_: datetime, event_id: str) -> ReplayDecisionEvent:
        facts = held_facts(
            symbol,
            at_,
            tactical=TacticalSignalFacts(
                l1_phase="OSCILLATION",
                l1_third_sell=True,
                broker_sellable_tactical_qty=100,
                q_liquidity_cap=100,
            ),
            boundary="9",
        )
        return event(
            event_id,
            facts,
            held_bindings(facts, include_risk=True),
            execution_status=status(next_session, at_, sellable=100),
            broker_position=100,
            bars=(bar(symbol, at_, 2, low="9.4", high="9.6", volume="4000"),),
            optional=False,
            persistent_intent_id=persistent_id,
        )

    result = engine(at(D0, 9, 30)).replay(
        (
            ReplayBatch(
                "entry-zero-tactical-lot",
                entry_at,
                entry_at + timedelta(minutes=1),
                (entry_event,),
                (decision_mark(symbol, entry_at),),
            ),
            ReplayBatch(
                "zero-tactical-lot",
                sell_at,
                sell_at + timedelta(minutes=1),
                (tactical_exit(sell_at, "tactical:zero-lot"),),
                (decision_mark(symbol, sell_at, "9.5"),),
            ),
            ReplayBatch(
                "zero-tactical-lot-retry",
                retry_at,
                retry_at + timedelta(minutes=1),
                (tactical_exit(retry_at, "tactical:zero-lot:retry"),),
                (decision_mark(symbol, retry_at, "9.5"),),
            ),
        )
    )

    assert tuple(record.intent.action for record in result.intents) == (
        "ENTRY_INTENT",
        "WAIT",
    )
    assert result.intents[-1].intent.reason_codes == (
        "L1_THIRD_SELL_STOP_RESTORE",
        "NO_SELLABLE_TACTICAL_INVENTORY",
    )
    assert result.intents[-1].persistent_intent_id == persistent_id
    assert result.positions[0].quantity == 100
    assert result.positions[0].tactical_held_quantity == 0
    assert result.resolved_persistent_intent_ids == (persistent_id,)
    assert result.suppressed_persistent_event_counts == ((persistent_id, 1),)
    assert len(result.orders) == 1  # entry only


def test_order_identity_is_invariant_to_unrelated_noop_event() -> None:
    """Idempotent order ids may not depend on a transient batch ordinal."""

    decision = at(D0, 10, 0)
    entry_symbol = "SH.600000"
    flat_symbol = "SZ.000001"
    entry_event = event(
        "entry:stable-identity",
        entry_facts(entry_symbol, decision, quantity=100),
        entry_bindings(entry_symbol, decision),
        execution_status=status(D0, decision),
        broker_position=0,
        bars=(
            bar(
                entry_symbol,
                decision,
                1,
                low="9.7",
                high="9.9",
                volume="4000",
            ),
        ),
        optional=True,
    )
    flat_facts = held_facts(
        flat_symbol,
        decision,
        strategic=StrategicSignalFacts(l0_third_sell=True),
    )
    flat_noop = event(
        "exit:unrelated-flat-noop",
        flat_facts,
        held_bindings(flat_facts),
        execution_status=status(D0, decision, sellable=0),
        broker_position=0,
        bars=(),
        optional=False,
        persistent_intent_id="persistent:unrelated-flat-noop",
    )

    def run(events, marks):  # type: ignore[no-untyped-def]
        return engine(at(D0, 9, 30)).replay(
            (
                ReplayBatch(
                    "stable-order-identity",
                    decision,
                    decision + timedelta(minutes=1),
                    events,
                    marks,
                ),
            )
        )

    entry_only = run(
        (entry_event,),
        (decision_mark(entry_symbol, decision),),
    )
    with_noop = run(
        (flat_noop, entry_event),
        (
            decision_mark(flat_symbol, decision),
            decision_mark(entry_symbol, decision),
        ),
    )

    assert len(entry_only.orders) == len(with_noop.orders) == 1
    assert (
        entry_only.orders[0].order.client_order_id
        == with_noop.orders[0].order.client_order_id
    )
    assert entry_only.orders[0].match.fills == with_noop.orders[0].match.fills
    assert entry_only.equity_curve == with_noop.equity_curve


def test_partial_tactical_fills_create_cohorts_and_fifo_buyback_cycle() -> None:
    symbol = "SH.512000"
    entry_at = at(D0, 10, 0)
    next_session = D0 + timedelta(days=1)
    sell_at = at(next_session, 10, 0)
    buy_at = at(next_session, 11, 0)
    repeat_at = at(next_session, 12, 0)
    entry_event = event(
        "entry:tactical",
        entry_facts(symbol, entry_at, quantity=800),
        entry_bindings(symbol, entry_at),
        execution_status=status(D0, entry_at),
        broker_position=0,
        bars=(
            bar(
                symbol,
                entry_at,
                1,
                low="9.7",
                high="9.9",
                volume="16000",
            ),
        ),
        optional=True,
    )
    sell_tactical = TacticalSignalFacts(
        l1_phase="OSCILLATION",
        ordinary_sell_signal=True,
        l2_signal_confirmed=True,
        l2_reached_required_half=True,
        tactical_adaptation_passed=True,
        broker_sellable_tactical_qty=200,
        q_liquidity_cap=200,
    )
    sell_facts = held_facts(symbol, sell_at, tactical=sell_tactical, boundary="9")
    sell_event = event(
        "tactical:sell",
        sell_facts,
        held_bindings(sell_facts, include_risk=True),
        execution_status=status(next_session, sell_at, sellable=800),
        broker_position=800,
        bars=(
            bar(
                symbol,
                sell_at,
                2,
                low="9.5",
                high="9.7",
                volume="2000",
            ),
            bar(
                symbol,
                sell_at + timedelta(minutes=1),
                3,
                low="9.4",
                high="9.6",
                volume="2000",
            ),
        ),
        optional=True,
    )
    buy_tactical = TacticalSignalFacts(
        l1_phase="OSCILLATION",
        ordinary_buyback_signal=True,
        l2_signal_confirmed=True,
        l2_reached_required_half=True,
        zn_at_or_above_a=True,
        higher_timeframe_allows_ordinary_buyback=True,
        every_partial_prefix_edge_passed=True,
        q_liquidity_cap=200,
        cash_affordable_buyback_qty=200,
    )
    buy_facts = held_facts(symbol, buy_at, tactical=buy_tactical, boundary="9.2")
    buy_event = event(
        "tactical:buyback",
        buy_facts,
        held_bindings(buy_facts, include_risk=True),
        execution_status=status(next_session, buy_at, sellable=600),
        broker_position=600,
        bars=(
            bar(
                symbol,
                buy_at,
                4,
                low="8.8",
                high="9.0",
                volume="2000",
            ),
            bar(
                symbol,
                buy_at + timedelta(minutes=1),
                5,
                low="8.7",
                high="8.9",
                volume="2000",
            ),
        ),
        optional=True,
    )
    repeat_facts = held_facts(
        symbol,
        repeat_at,
        tactical=sell_tactical,
        boundary="9",
    )
    repeat_event = event(
        "tactical:repeat",
        repeat_facts,
        held_bindings(repeat_facts, include_risk=True),
        execution_status=status(next_session, repeat_at, sellable=600),
        broker_position=800,
        bars=(
            bar(
                symbol,
                repeat_at,
                6,
                low="9.5",
                high="9.7",
                volume="2000",
            ),
        ),
        optional=True,
    )
    result = engine(at(D0, 9, 30)).replay(
        (
            ReplayBatch(
                "entry",
                entry_at,
                entry_at + timedelta(minutes=1),
                (entry_event,),
                (decision_mark(symbol, entry_at),),
            ),
            ReplayBatch(
                "sell",
                sell_at,
                sell_at + timedelta(minutes=2),
                (sell_event,),
                (decision_mark(symbol, sell_at, "9.6"),),
            ),
            ReplayBatch(
                "buyback",
                buy_at,
                buy_at + timedelta(minutes=2),
                (buy_event,),
                (decision_mark(symbol, buy_at, "9"),),
            ),
            ReplayBatch(
                "repeat-blocked",
                repeat_at,
                repeat_at + timedelta(minutes=1),
                (repeat_event,),
                (decision_mark(symbol, repeat_at, "9.6"),),
            ),
        )
    )
    assert tuple(record.intent.action for record in result.intents) == (
        "ENTRY_INTENT",
        "TACTICAL_SELL_INTENT",
        "TACTICAL_BUYBACK_INTENT",
        "WAIT",
    )
    assert len(result.orders[1].match.fills) == 2
    assert len(result.orders[2].match.fills) == 2
    position = result.positions[0]
    assert position.quantity == 800
    assert position.pending_restore_quantity == 0
    assert len(position.restore_cohort_ids) == 2
    assert position.completed_tactical_cycle_sessions == (next_session,)
    assert result.metrics.tactical_cycle_count == 1
    assert result.metrics.total_fees == Decimal("15.00")
    assert result.metrics.fill_count == 5


def test_incomplete_corporate_action_state_is_unresolved_and_never_orders() -> None:
    decision = at(D0, 10, 0)
    symbol = "SH.513000"
    replay_event = event(
        "entry:corporate-unresolved",
        entry_facts(symbol, decision),
        entry_bindings(symbol, decision),
        execution_status=status(
            D0,
            decision,
            corporate_complete=False,
        ),
        broker_position=0,
        bars=(
            bar(
                symbol,
                decision,
                1,
                low="9.7",
                high="9.9",
                volume="2000",
            ),
        ),
        optional=True,
    )
    result = engine(at(D0, 9, 30)).replay(
        (
            ReplayBatch(
                "corporate-unresolved",
                decision,
                decision + timedelta(minutes=1),
                (replay_event,),
                (decision_mark(symbol, decision),),
            ),
        )
    )
    assert result.orders == ()
    assert "UNRESOLVED_CORPORATE_ACTION_STATE" in result.unresolved_reason_codes
    assert result.live_status == "LIVE_DISABLED"


def test_mandatory_share_action_scales_event_sourced_position() -> None:
    symbol = "SH.600000"
    entry_at = at(D0, 10, 0)
    effective_at = at(D0 + timedelta(days=1), 9, 30)
    entry_event = event(
        "entry:before-share-action",
        entry_facts(symbol, entry_at, quantity=400),
        entry_bindings(symbol, entry_at),
        execution_status=status(D0, entry_at),
        broker_position=0,
        bars=(bar(symbol, entry_at, 1, low="9.7", high="9.9", volume="8000"),),
        optional=True,
    )
    action = ReplayMandatoryShareActionFact(
        action_id="share-action:1",
        symbol=symbol,
        effective_at=effective_at,
        known_at=effective_at,
        share_multiplier=Decimal("1.5"),
        source_id="qmt-factor:1",
        source_ledger_sha256="sha256:" + "1" * 64,
    )
    result = engine(at(D0, 9, 30)).replay(
        (
            ReplayBatch(
                "entry",
                entry_at,
                entry_at + timedelta(minutes=1),
                (entry_event,),
                (decision_mark(symbol, entry_at),),
            ),
            ReplayBatch(
                "mandatory-share-action",
                effective_at,
                effective_at + timedelta(minutes=1),
                (),
                (decision_mark(symbol, effective_at, "6.6"),),
                (decision_mark(symbol, effective_at + timedelta(minutes=1), "6.6"),),
                (),
                (action,),
            ),
        )
    )
    assert result.positions[0].quantity == 600
    assert len(result.mandatory_share_actions) == 1
    record = result.mandatory_share_actions[0]
    assert (record.quantity_before, record.quantity_after) == (400, 600)
    assert record.applied is True


def test_json_artifact_entry_replays_the_same_strict_engine() -> None:
    decision = at(D0, 10, 0)
    symbol = "SH.513100"
    replay_event = event(
        "entry:json-artifact",
        entry_facts(symbol, decision),
        entry_bindings(symbol, decision),
        execution_status=status(D0, decision),
        broker_position=0,
        bars=(
            bar(
                symbol,
                decision,
                1,
                low="9.7",
                high="9.9",
                volume="2000",
            ),
        ),
        optional=True,
    )
    batch = ReplayBatch(
        "json-artifact",
        decision,
        decision + timedelta(minutes=1),
        (replay_event,),
        (decision_mark(symbol, decision),),
    )
    report = run_payload(
        {
            "schema": INPUT_SCHEMA,
            "initial_cash": "1000000",
            "started_at": at(D0, 9, 30).isoformat(),
            "fee_model": _jsonable(fee_model()),
            "builder_contract": frozen_builder_contract(),
            "batches": _jsonable((batch,)),
        },
        input_sha256="sha256:test-artifact",
    )
    result = report["result"]
    assert isinstance(result, dict)
    assert result["live_status"] == "LIVE_DISABLED"
    assert result["metrics"]["fill_count"] == 1
    assert result["positions"][0]["quantity"] == 100
    assert report["input_sha256"] == "sha256:test-artifact"
    assert report["builder_contract"] == frozen_builder_contract()
    # 有真实成交的非空回放才允许引用收益字段
    assert result["metrics"]["ledger_valid"] is True
    assert result["metrics"]["empty_replay"] is False


def frozen_builder_contract() -> dict[str, str]:
    from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
        V31AlignmentContract,
    )
    from chanlun.decision_support.trading_system.v3_timeframe_override import (
        independent_timeframe_override,
    )

    return {
        "alignment_contract_id": V31AlignmentContract().contract_id,
        "alignment_parameter_set_id": V31AlignmentContract().parameter_set_id,
        "timeframe_override_parameter_set_id": (
            independent_timeframe_override().parameter_set_id
        ),
        "strategy_parameter_set_id": "sha256:" + "0" * 64,
        "execution_parameter_set_id": "sha256:" + "1" * 64,
        "live_status": "LIVE_DISABLED",
    }


def _artifact_payload(batch: ReplayBatch, contract: object) -> dict[str, object]:
    return {
        "schema": INPUT_SCHEMA,
        "initial_cash": "1000000",
        "started_at": at(D0, 9, 30).isoformat(),
        "fee_model": _jsonable(fee_model()),
        "builder_contract": contract,
        "batches": _jsonable((batch,)),
    }


def test_json_artifact_entry_rejects_foreign_builder_contract() -> None:
    """R-06: 错版本 payload 必须在入口被拒，而不是靠深层事实门兜底。"""

    batch = ReplayBatch("empty", at(D0, 10, 0), at(D0, 10, 1), (), ())

    missing = _artifact_payload(batch, None)
    del missing["builder_contract"]
    with pytest.raises(KeyError):
        run_payload(missing)

    for key, bad in (
        ("alignment_contract_id", "V31_SOMETHING_ELSE"),
        ("alignment_parameter_set_id", "sha256:" + "a" * 64),
        ("timeframe_override_parameter_set_id", "sha256:" + "b" * 64),
        ("live_status", "LIVE_ENABLED"),
    ):
        contract = frozen_builder_contract()
        contract[key] = bad
        with pytest.raises(ValueError, match=f"builder_contract.{key}"):
            run_payload(_artifact_payload(batch, contract))

    for key in ("strategy_parameter_set_id", "execution_parameter_set_id"):
        contract = frozen_builder_contract()
        contract[key] = "not-a-hash"
        with pytest.raises(ValueError, match=f"builder_contract.{key}"):
            run_payload(_artifact_payload(batch, contract))


def test_empty_replay_is_not_reported_as_evaluable_performance() -> None:
    """R-05: 空回放的 0 收益/0 回撤不得被读成有效绩效。"""

    batch = ReplayBatch("empty", at(D0, 10, 0), at(D0, 10, 1), (), ())
    report = run_payload(_artifact_payload(batch, frozen_builder_contract()))
    metrics = report["result"]["metrics"]
    assert metrics["ledger_valid"] is True
    assert metrics["valid"] is True
    assert metrics["empty_replay"] is True
    assert metrics["performance_evaluable"] is False
    assert metrics["net_return"] == "0"
    assert metrics["max_drawdown"] == "0"
    assert "EMPTY_REPLAY_RETURNS_NOT_EVALUABLE" in metrics["warnings"]
