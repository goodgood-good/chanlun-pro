from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import cast

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.trading_system.backtest.execution import (
    ExecutionPolicy,
)
from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
    MinuteBar,
    SecurityStatus,
)
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    risk_candidate_from,
    run_event_backtest,
)
from chanlun.decision_support.trading_system.engine import (
    SymbolStructureBundle,
)
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
)
from chanlun.decision_support.trading_system.models import (
    EntryDecision,
    ExitDecision,
    PointType,
    StructuralPoint,
    StructureTower,
)
from chanlun.decision_support.trading_system.portfolio_risk import (
    PortfolioSnapshot,
    RiskCandidate,
    RiskLimits,
    size_entry,
)
from tests.trading_system.backtest.helpers import CN
from tests.trading_system.helpers import valid_selection_research
from tests.trading_system.helpers import confirmed_point, eligible_sector


@dataclass(frozen=True, slots=True)
class FakeBundle:
    code: str
    as_of: datetime


@dataclass(frozen=True, slots=True)
class FakePoint:
    point_type: PointType
    tower: StructureTower = "formal"
    recursive_level: int = 0
    structure_anchor_price: float = 10.0
    available_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FakeTrigger:
    available_at: datetime


@dataclass(frozen=True, slots=True)
class FakeSector:
    sector_id: str
    regime: str = "neutral"
    rank_score: int = 0
    horizontal_rank: int | None = None


@dataclass(frozen=True, slots=True)
class FakeSetup:
    point: FakePoint
    sector: FakeSector


@dataclass(frozen=True, slots=True)
class FakeContext:
    grade: str = "A"


@dataclass(frozen=True, slots=True)
class FakeBoundary:
    raw_high: Decimal


@dataclass(frozen=True, slots=True)
class FakeEvaluation:
    setup: FakeSetup
    entry: EntryDecision | None
    exit: ExitDecision | None
    context_assessment: FakeContext | None = FakeContext()
    market_risk_gate: str = "UNRESOLVED"
    symbol_risk_gate: str = "UNRESOLVED"
    entry_execution_boundary: FakeBoundary | None = None
    trigger: FakeTrigger | None = None


class ScheduledEngine:
    def __init__(
        self,
        schedule: dict[tuple[datetime, str], tuple[FakeEvaluation, ...]],
    ) -> None:
        self.schedule = schedule

    def evaluate_symbol(self, bundle: FakeBundle) -> tuple[FakeEvaluation, ...]:
        return self.schedule.get((bundle.as_of, bundle.code), ())


class FakeReplay:
    def bundle_at(
        self,
        *,
        dataset: BacktestDataset,
        closed_at: datetime,
        code: str,
    ) -> FakeBundle:
        del dataset
        return FakeBundle(code=code, as_of=closed_at)


class ScheduledBundleReplay:
    def __init__(self, schedule: dict[datetime, SymbolStructureBundle]) -> None:
        self.schedule = schedule

    def bundle_at(
        self,
        *,
        dataset: BacktestDataset,
        closed_at: datetime,
        code: str,
    ) -> SymbolStructureBundle:
        del dataset, code
        return self.schedule[closed_at]


def market_bar(
    code: str,
    opened_at: datetime,
    *,
    opened: str = "10.00",
    high: str = "10.02",
    low: str = "9.99",
    closed: str = "10.00",
    previous: str = "10.00",
    volume: str = "100000",
) -> MinuteBar:
    closed_at = opened_at + timedelta(seconds=59)
    return MinuteBar(
        code=code,
        opened_at=opened_at,
        closed_at=closed_at,
        raw_open=Decimal(opened),
        raw_high=Decimal(high),
        raw_low=Decimal(low),
        raw_close=Decimal(closed),
        analysis_open=Decimal(opened),
        analysis_high=Decimal(high),
        analysis_low=Decimal(low),
        analysis_close=Decimal(closed),
        previous_raw_close=Decimal(previous),
        volume=Decimal(volume),
        turnover=Decimal(volume) * Decimal(closed),
        adjustment_known_at=closed_at,
    )


def security_status(
    code: str,
    session: date,
    *,
    t_plus_days: int = 1,
    limit_pct: str = "0.10",
) -> SecurityStatus:
    return SecurityStatus(
        session=session,
        code=code,
        listed=True,
        st=False,
        suspended=False,
        limit_pct=Decimal(limit_pct),
        lot_size=100,
        t_plus_days=t_plus_days,
    )


def backtest_dataset(
    bars: tuple[MinuteBar, ...],
    *,
    t_plus_days: int = 1,
    limit_pct_by_code: dict[str, str] | None = None,
) -> BacktestDataset:
    keys = sorted({(bar.code, bar.opened_at.date()) for bar in bars})
    limit_pct_by_code = limit_pct_by_code or {}
    statuses = tuple(
        security_status(
            code,
            session,
            t_plus_days=t_plus_days,
            limit_pct=limit_pct_by_code.get(code, "0.10"),
        )
        for code, session in keys
    )
    return BacktestDataset(
        bars=bars,
        statuses=statuses,
        memberships=(),
        corporate_actions=(),
        membership_as_of_each_session=True,
        point_in_time_adjustment=True,
        source_hashes=(("fixture", "sha256:fixture"),),
    )


def allowed_entry(
    *,
    signal_id: str,
    stop: str,
    point_type: str = "2buy",
    sector_id: str = "TDX.880301",
    multiplier: str = "1.00",
    context_grade: str | None = "A",
    anchor: str = "10.00",
    price_cap: str | None = None,
    five_minute_available_at: datetime | None = None,
    one_minute_available_at: datetime | None = None,
) -> FakeEvaluation:
    typed_point = cast(PointType, point_type)
    return FakeEvaluation(
        setup=FakeSetup(
            FakePoint(
                typed_point,
                structure_anchor_price=float(anchor),
                available_at=five_minute_available_at,
            ),
            FakeSector(sector_id),
        ),
        entry=EntryDecision(
            allowed=True,
            signal_id=signal_id,
            risk_multiplier=Decimal(multiplier),
            structural_stop=Decimal(stop),
            reason_codes=(),
        ),
        exit=None,
        context_assessment=(
            None if context_grade is None else FakeContext(context_grade)
        ),
        entry_execution_boundary=(
            None if price_cap is None else FakeBoundary(Decimal(price_cap))
        ),
        trigger=(
            None
            if one_minute_available_at is None
            else FakeTrigger(one_minute_available_at)
        ),
    )


def allowed_exit(
    *,
    signal_id: str,
    point_type: str = "2sell",
    sector_id: str = "TDX.880301",
) -> FakeEvaluation:
    typed_point = cast(PointType, point_type)
    return FakeEvaluation(
        setup=FakeSetup(FakePoint(typed_point), FakeSector(sector_id)),
        entry=None,
        exit=ExitDecision(
            allowed=True,
            signal_id=signal_id,
            action="exit_full",
            reason_codes=(),
        ),
    )


def run_fixture(
    bars: tuple[MinuteBar, ...],
    schedule: dict[tuple[datetime, str], tuple[FakeEvaluation, ...]],
    *,
    t_plus_days: int = 1,
    risk_limits: RiskLimits = RiskLimits(),
    initial_cash: str = "100000",
    terminal_liquidation: bool = False,
    limit_pct_by_code: dict[str, str] | None = None,
) -> BacktestRun:
    return run_event_backtest(
        backtest_dataset(
            bars,
            t_plus_days=t_plus_days,
            limit_pct_by_code=limit_pct_by_code,
        ),
        engine=ScheduledEngine(schedule),  # type: ignore[arg-type]
        structure_replay=FakeReplay(),  # type: ignore[arg-type]
        risk_limits=risk_limits,
        execution_policy=ExecutionPolicy(),
        initial_cash=Decimal(initial_cash),
        terminal_liquidation=terminal_liquidation,
    )


def test_intraday_low_crossing_structural_stop_creates_exit() -> None:
    first = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 30, tzinfo=CN))
    entry_bar = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 31, tzinfo=CN))
    stop_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 32, tzinfo=CN),
        high="10.05",
        low="9.70",
        closed="9.95",
    )
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((first, entry_bar, stop_bar), schedule, t_plus_days=0)

    assert run.trades[0].exit_reason == "structural_stop"
    assert run.trades[0].exit_trigger_price == Decimal("9.80")
    assert run.trades[0].exit_price < Decimal("9.80")


def test_same_day_stop_respects_t_plus_one() -> None:
    first = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 30, tzinfo=CN))
    entry_bar = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 31, tzinfo=CN))
    stop_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 32, tzinfo=CN),
        low="9.70",
        closed="9.95",
    )
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((first, entry_bar, stop_bar), schedule, t_plus_days=1)

    assert run.pending_exits[0].reason == "t_plus_one_locked"
    assert run.trades == ()


def test_limit_down_keeps_structural_exit_pending() -> None:
    day_one = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    first = market_bar("SZ.000001", day_one)
    entry_bar = market_bar("SZ.000001", day_one + timedelta(minutes=1))
    limit_down = market_bar(
        "SZ.000001",
        datetime(2026, 7, 21, 9, 30, tzinfo=CN),
        opened="9.00",
        high="9.00",
        low="9.00",
        closed="9.00",
        previous="10.00",
    )
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((first, entry_bar, limit_down), schedule)

    assert run.trades == ()
    assert run.pending_exits[0].reason == "limit_down_locked"


def test_entry_fills_on_next_bar_not_signal_bar() -> None:
    first = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 30, tzinfo=CN))
    second = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 31, tzinfo=CN))
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((first, second), schedule)

    accepted = tuple(fill for fill in run.fills if fill.filled)
    assert len(accepted) == 1
    assert accepted[0].filled_at == second.closed_at


def test_live_no_chase_anchor_guard_is_applied_to_backtest_admission() -> None:
    signal = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 30, tzinfo=CN),
        opened="10.58",
        high="10.62",
        low="10.55",
        closed="10.60",
    )
    next_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 31, tzinfo=CN),
        opened="10.60",
        high="10.65",
        low="10.55",
        closed="10.62",
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(
                signal_id="entry-a",
                stop="9.80",
                anchor="10.00",
            ),
        )
    }

    run = run_fixture((signal, next_bar), schedule)

    assert run.open_positions == ()
    assert len(run.fills) == 1
    assert run.fills[0].order_id.startswith("admission:")
    assert run.fills[0].reason == "BUY_PRICE_TOO_FAR_ABOVE_STRUCTURE_ANCHOR"


def test_stale_one_minute_precision_is_rejected_at_backtest_admission() -> None:
    signal = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 30, tzinfo=CN),
    )
    next_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 31, tzinfo=CN),
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(
                signal_id="entry-a",
                stop="9.80",
                five_minute_available_at=signal.closed_at,
                one_minute_available_at=signal.closed_at - timedelta(minutes=25),
            ),
        )
    }

    run = run_fixture((signal, next_bar), schedule)

    assert run.open_positions == ()
    assert len(run.fills) == 1
    assert run.fills[0].reason == ("ONE_MINUTE_PRECISION_PRECEDES_FIVE_MINUTE_SETUP")


def test_wide_initial_structural_risk_is_rejected_at_backtest_admission() -> None:
    signal = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 30, tzinfo=CN),
    )
    next_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 31, tzinfo=CN),
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(signal_id="entry-a", stop="9.40"),
        )
    }

    run = run_fixture((signal, next_bar), schedule)

    assert run.open_positions == ()
    assert len(run.fills) == 1
    assert run.fills[0].reason == "INITIAL_STRUCTURAL_RISK_TOO_WIDE"


def test_one_minute_confirmation_high_is_a_terminal_no_chase_cap() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal = market_bar("SZ.000001", opened)
    crossed = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=1),
        opened="10.08",
        high="10.10",
        low="10.06",
        closed="10.08",
    )
    later = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=2),
        high="10.04",
        low="10.00",
        closed="10.02",
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(
                signal_id="entry-a",
                stop="9.80",
                price_cap="10.05",
            ),
        )
    }

    run = run_fixture((signal, crossed, later), schedule)

    assert run.open_positions == ()
    assert tuple(fill.reason for fill in run.fills) == ("entry_price_cap_crossed",)


def test_mixed_price_cap_bar_defers_until_a_whole_bar_is_within_cap() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal = market_bar("SZ.000001", opened)
    mixed = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=1),
        opened="10.03",
        high="10.08",
        low="10.00",
        closed="10.04",
    )
    within = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=2),
        opened="10.02",
        high="10.04",
        low="10.00",
        closed="10.02",
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(
                signal_id="entry-a",
                stop="9.80",
                price_cap="10.05",
            ),
        )
    }

    run = run_fixture((signal, mixed, within), schedule)

    assert run.fills[0].reason == "entry_price_cap_unresolved"
    accepted = tuple(fill for fill in run.fills if fill.filled)
    assert len(accepted) == 1
    assert accepted[0].filled_at == within.closed_at
    assert accepted[0].execution_price <= Decimal("10.05")


def test_entry_fill_cannot_use_an_earlier_low_from_the_same_bar() -> None:
    signal = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 30, tzinfo=CN),
    )
    fill = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 31, tzinfo=CN),
        high="10.05",
        low="9.70",
        closed="10.00",
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((signal, fill), schedule, t_plus_days=0)

    assert run.trades == ()
    assert len(run.open_positions) == 1
    assert run.pending_exits == ()


def test_breakeven_stop_arms_after_one_r_and_activates_next_bar() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal = market_bar("SZ.000001", opened)
    fill = market_bar("SZ.000001", opened + timedelta(minutes=1))
    trigger = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=2),
        high="10.30",
        low="9.90",
        closed="10.10",
    )
    next_bar = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=3),
        high="10.05",
        low="9.95",
        closed="10.00",
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    armed = run_fixture((signal, fill, trigger), schedule, t_plus_days=0)
    completed = run_fixture(
        (signal, fill, trigger, next_bar),
        schedule,
        t_plus_days=0,
    )

    assert armed.trades == ()
    assert armed.open_positions[0].breakeven_armed_at == trigger.closed_at
    assert armed.open_positions[0].structural_stop == (
        armed.open_positions[0].entry_price
    )
    assert completed.trades[0].exit_at == next_bar.closed_at
    assert completed.trades[0].exit_reason == "breakeven_stop"
    assert completed.trades[0].exit_trigger_price == (completed.trades[0].entry_price)


def test_entry_is_resized_to_next_bar_volume_capacity() -> None:
    first = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 30, tzinfo=CN))
    second = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 31, tzinfo=CN),
        volume="1000",
    )
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((first, second), schedule)

    accepted = tuple(fill for fill in run.fills if fill.filled)
    assert len(accepted) == 1
    assert accepted[0].shares == 100
    assert run.open_positions[0].shares == 100


def test_entry_expires_with_the_production_risk_snapshot() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    first = market_bar("SZ.000001", opened)
    blocked = tuple(
        market_bar(
            "SZ.000001",
            opened + timedelta(minutes=offset),
            opened="11.00",
            high="11.00",
            low="11.00",
            closed="11.00",
            previous="10.00",
        )
        for offset in range(1, 6)
    )
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((first, *blocked), schedule)

    assert run.open_positions == ()
    assert run.fills[-1].reason == "risk_snapshot_expired"
    assert sum(fill.reason == "limit_up_locked" for fill in run.fills) == 4


def test_exit_partials_are_aggregated_into_one_trade() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal = market_bar("SZ.000001", opened)
    entry_fill = market_bar("SZ.000001", opened + timedelta(minutes=1))
    exit_signal = market_bar("SZ.000001", opened + timedelta(minutes=2))
    exit_fill_a = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=3),
        volume="5000",
    )
    exit_fill_b = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=4),
        volume="5000",
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        ),
        (exit_signal.closed_at, exit_signal.code): (allowed_exit(signal_id="exit-a"),),
    }

    run = run_fixture(
        (signal, entry_fill, exit_signal, exit_fill_a, exit_fill_b),
        schedule,
        t_plus_days=0,
        risk_limits=RiskLimits(
            base_trade_risk=Decimal("0.005"),
            max_symbol_fraction=Decimal("0.10"),
        ),
    )

    sell_fills = tuple(
        fill for fill in run.fills if fill.filled and fill.order_id.startswith("exit:")
    )
    assert tuple(fill.shares for fill in sell_fills) == (500, 400)
    assert len(run.trades) == 1
    assert run.trades[0].shares == 900
    assert run.trades[0].exit_reason == "signal_exit_full"
    assert run.open_positions == ()


def test_entry_uses_shared_risk_sizing() -> None:
    first = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 30, tzinfo=CN))
    second = market_bar("SZ.000001", datetime(2026, 7, 20, 10, 31, tzinfo=CN))
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture((first, second), schedule)

    position = run.open_positions[0]
    expected = size_entry(
        portfolio=PortfolioSnapshot(
            equity=Decimal("100000"),
            available_cash=Decimal("100000"),
            drawdown=Decimal("0"),
            open_risk_cash=Decimal("0"),
        ),
        candidate=RiskCandidate(
            signal_id="entry-a",
            sector_id="TDX.880301",
            symbol_id=position.code,
            entry_price=position.entry_price,
            stop_price=Decimal("9.80"),
            risk_multiplier=Decimal("1.00"),
        ),
        limits=RiskLimits(),
    )
    assert position.shares == expected.shares == 400


def test_context_grade_scales_the_portfolio_risk_candidate() -> None:
    bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 30, tzinfo=CN),
    )
    grade_a = allowed_entry(
        signal_id="entry-a",
        stop="9.80",
        context_grade="A",
    )
    grade_b = allowed_entry(
        signal_id="entry-b",
        stop="9.80",
        context_grade="B",
    )
    unresolved = allowed_entry(
        signal_id="entry-u",
        stop="9.80",
        context_grade=None,
    )

    assert risk_candidate_from(grade_a, bar).risk_multiplier == Decimal("1.00")
    assert risk_candidate_from(grade_b, bar).risk_multiplier == Decimal("0.75")
    assert risk_candidate_from(unresolved, bar).risk_multiplier == Decimal("0.50")


def test_context_scale_is_preserved_during_fill_price_revalidation() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal = market_bar("SZ.000001", opened)
    gap_fill = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=1),
        opened="10.50",
        high="10.52",
        low="10.49",
        closed="10.50",
    )
    schedule = {
        (signal.closed_at, signal.code): (
            allowed_entry(
                signal_id="entry-b",
                stop="9.80",
                context_grade="B",
            ),
        )
    }
    limits = RiskLimits(
        base_trade_risk=Decimal("0.005"),
        max_symbol_fraction=Decimal("1"),
        max_sector_fraction=Decimal("1"),
        max_portfolio_heat=Decimal("1"),
    )

    run = run_fixture((signal, gap_fill), schedule, risk_limits=limits)

    assert run.open_positions[0].shares == 500


def test_sector_exposure_reserves_same_timestamp_orders() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal_bars = (
        market_bar("SH.600000", opened),
        market_bar("SZ.000001", opened),
    )
    fill_bars = tuple(
        market_bar(bar.code, opened + timedelta(minutes=1)) for bar in signal_bars
    )
    schedule = {
        (bar.closed_at, bar.code): (
            allowed_entry(signal_id=f"entry-{bar.code}", stop="9.99"),
        )
        for bar in signal_bars
    }
    limits = replace(RiskLimits(), max_sector_fraction=Decimal("0.05"))

    run = run_fixture(signal_bars + fill_bars, schedule, risk_limits=limits)

    assert len(run.open_positions) == 1
    assert sum(
        position.last_price * position.shares for position in run.open_positions
    ) <= Decimal("10000")


def test_causal_context_priority_wins_limited_same_timestamp_capacity() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal_c = market_bar("SH.600000", opened)
    signal_a = market_bar("SZ.000001", opened)
    fill_c = market_bar("SH.600000", opened + timedelta(minutes=1))
    fill_a = market_bar("SZ.000001", opened + timedelta(minutes=1))
    schedule = {
        (signal_c.closed_at, signal_c.code): (
            allowed_entry(
                signal_id="entry-c",
                stop="9.99",
                context_grade="C",
            ),
        ),
        (signal_a.closed_at, signal_a.code): (
            allowed_entry(
                signal_id="entry-a",
                stop="9.99",
                context_grade="A",
            ),
        ),
    }
    limits = replace(RiskLimits(), max_sector_fraction=Decimal("0.05"))

    run = run_fixture(
        (signal_c, signal_a, fill_c, fill_a),
        schedule,
        risk_limits=limits,
    )

    assert tuple(position.code for position in run.open_positions) == ("SZ.000001",)


def test_portfolio_heat_reserves_same_timestamp_orders() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal_bars = (
        market_bar("SH.600000", opened),
        market_bar("SZ.000001", opened),
    )
    fill_bars = tuple(
        market_bar(bar.code, opened + timedelta(minutes=1)) for bar in signal_bars
    )
    schedule = {
        (bar.closed_at, bar.code): (
            allowed_entry(signal_id=f"entry-{bar.code}", stop="9.00"),
        )
        for bar in signal_bars
    }
    limits = replace(RiskLimits(), max_portfolio_heat=Decimal("0.001"))

    run = run_fixture(signal_bars + fill_bars, schedule, risk_limits=limits)

    assert run.equity_curve[-1].open_risk_cash <= Decimal("100")


def test_drawdown_stop_gate_blocks_new_entry() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    first = market_bar("SH.600000", opened)
    fill = market_bar("SH.600000", opened + timedelta(minutes=1))
    drawdown_a = market_bar(
        "SH.600000",
        opened + timedelta(minutes=2),
        opened="7.80",
        high="7.90",
        low="7.70",
        closed="7.80",
        previous="10.00",
    )
    drawdown_b = market_bar("SZ.000001", opened + timedelta(minutes=2))
    next_b = market_bar("SZ.000001", opened + timedelta(minutes=3))
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.50"),
        ),
        (drawdown_b.closed_at, drawdown_b.code): (
            allowed_entry(signal_id="entry-b", stop="9.80"),
        ),
    }
    limits = replace(
        RiskLimits(),
        base_trade_risk=Decimal("0.50"),
        max_symbol_fraction=Decimal("0.50"),
        max_sector_fraction=Decimal("1.00"),
        max_portfolio_heat=Decimal("1.00"),
    )

    run = run_fixture(
        (first, fill, drawdown_a, drawdown_b, next_b),
        schedule,
        risk_limits=limits,
        limit_pct_by_code={"SH.600000": "0.30"},
    )

    assert {position.code for position in run.open_positions} == {"SH.600000"}


def test_terminal_open_position_is_marked_not_counted_as_trade() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    first = market_bar("SZ.000001", opened)
    final = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=1),
        opened="10.00",
        high="11.00",
        low="9.99",
        closed="11.00",
    )
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    run = run_fixture(
        (first, final),
        schedule,
        risk_limits=replace(
            RiskLimits(),
            base_trade_risk=Decimal("0.005"),
        ),
    )

    assert run.trades == ()
    assert run.open_positions
    assert run.equity_curve[-1].market_value == (
        Decimal("11.00") * run.open_positions[0].shares
    )
    assert run.equity_curve[-1].equity == (
        run.equity_curve[-1].cash + run.equity_curve[-1].market_value
    )


def test_forced_liquidation_is_explicit_sensitivity() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    first = market_bar("SZ.000001", opened)
    final = market_bar(
        "SZ.000001",
        opened + timedelta(minutes=1),
        opened="10.00",
        high="11.00",
        low="9.99",
        closed="11.00",
    )
    schedule = {
        (first.closed_at, first.code): (
            allowed_entry(signal_id="entry-a", stop="9.80"),
        )
    }

    mechanics_limits = replace(
        RiskLimits(),
        base_trade_risk=Decimal("0.005"),
    )
    marked = run_fixture(
        (first, final),
        schedule,
        risk_limits=mechanics_limits,
    )
    liquidated = run_fixture(
        (first, final),
        schedule,
        risk_limits=mechanics_limits,
        terminal_liquidation=True,
    )

    assert marked.open_positions
    assert liquidated.open_positions == ()
    assert liquidated.trades[0].exit_reason == "forced_liquidation_sensitivity"


def test_symbol_input_order_does_not_change_portfolio_result() -> None:
    opened = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    signal_a = market_bar("SH.600000", opened)
    signal_b = market_bar("SZ.000001", opened)
    fill_a = market_bar("SH.600000", opened + timedelta(minutes=1))
    fill_b = market_bar("SZ.000001", opened + timedelta(minutes=1))
    schedule = {
        (signal_a.closed_at, signal_a.code): (
            allowed_entry(signal_id="entry-a", stop="9.99"),
        ),
        (signal_b.closed_at, signal_b.code): (
            allowed_entry(signal_id="entry-b", stop="9.99"),
        ),
    }
    limits = replace(RiskLimits(), max_sector_fraction=Decimal("0.05"))

    forward = run_fixture(
        (signal_a, signal_b, fill_a, fill_b),
        schedule,
        risk_limits=limits,
    )
    reverse = run_fixture(
        (signal_b, signal_a, fill_b, fill_a),
        schedule,
        risk_limits=limits,
    )

    assert forward == reverse


def test_position_structure_is_injected_into_same_level_sell_evaluation() -> None:
    def with_locked_terminal_segment(
        point: StructuralPoint,
        *,
        terminal_minutes: int,
    ) -> StructuralPoint:
        return replace(
            point,
            terminal_segment=TerminalSegmentReference(
                role="latest_completed",
                structural_level=point.recursive_level,
                unit_id=f"segment:{point.source_frequency}:{point.point_id}",
                source_kind=SourceKind.SEGMENT,
                direction="down" if point.side == "buy" else "up",
                state="locked",
                market_start=point.anchor_at - timedelta(minutes=terminal_minutes),
                market_end=point.anchor_at,
                available_at=point.available_at,
            ),
        )

    signal_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 4, tzinfo=CN),
    )
    sell_signal_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 5, tzinfo=CN),
    )
    exit_fill_bar = market_bar(
        "SZ.000001",
        datetime(2026, 7, 20, 10, 6, tzinfo=CN),
    )
    buy = with_locked_terminal_segment(
        confirmed_point("2buy", tower="formal", level=0),
        terminal_minutes=30,
    )
    buy_trigger = with_locked_terminal_segment(
        confirmed_point(
            "2buy",
            frequency="1m",
            tower="formal",
            level=0,
            minutes_after=-1,
            available_minutes_after=2,
        ),
        terminal_minutes=1,
    )
    sell = with_locked_terminal_segment(
        confirmed_point(
            "2sell",
            tower="formal",
            level=0,
            minutes_after=2,
        ),
        terminal_minutes=30,
    )
    sell_trigger = with_locked_terminal_segment(
        confirmed_point(
            "2sell",
            frequency="1m",
            tower="formal",
            level=0,
            minutes_after=1,
            available_minutes_after=2,
        ),
        terminal_minutes=1,
    )
    sector = eligible_sector()
    replay = ScheduledBundleReplay(
        {
            signal_bar.closed_at: SymbolStructureBundle(
                code=signal_bar.code,
                as_of=signal_bar.closed_at,
                sector=sector,
                thirty_direction="neutral",
                thirty_points=(),
                five_points=(buy,),
                one_points=(buy_trigger,),
                opposite_points=(),
                selection_sources=("QMT_SECTOR_TRIGGER",),
                selection_research=valid_selection_research(),
            ),
            sell_signal_bar.closed_at: SymbolStructureBundle(
                code=sell_signal_bar.code,
                as_of=sell_signal_bar.closed_at,
                sector=sector,
                thirty_direction="neutral",
                thirty_points=(),
                five_points=(sell,),
                one_points=(sell_trigger,),
                opposite_points=(sell,),
            ),
            exit_fill_bar.closed_at: SymbolStructureBundle(
                code=exit_fill_bar.code,
                as_of=exit_fill_bar.closed_at,
                sector=sector,
                thirty_direction="neutral",
                thirty_points=(),
                five_points=(),
                one_points=(),
                opposite_points=(),
            ),
        }
    )

    run = run_event_backtest(
        backtest_dataset(
            (signal_bar, sell_signal_bar, exit_fill_bar),
            t_plus_days=0,
        ),
        engine=HumanAssistedDecisionCore(),
        structure_replay=replay,
        risk_limits=RiskLimits(),
        execution_policy=ExecutionPolicy(),
        initial_cash=Decimal("100000"),
    )

    assert len(run.trades) == 1
    assert run.trades[0].exit_reason == "signal_exit_full"
    assert run.open_positions == ()
