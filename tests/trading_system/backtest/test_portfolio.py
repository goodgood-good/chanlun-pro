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


@dataclass(frozen=True, slots=True)
class FakeSector:
    sector_id: str


@dataclass(frozen=True, slots=True)
class FakeSetup:
    point: FakePoint
    sector: FakeSector


@dataclass(frozen=True, slots=True)
class FakeEvaluation:
    setup: FakeSetup
    entry: EntryDecision | None
    exit: ExitDecision | None


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
) -> FakeEvaluation:
    typed_point = cast(PointType, point_type)
    return FakeEvaluation(
        setup=FakeSetup(FakePoint(typed_point), FakeSector(sector_id)),
        entry=EntryDecision(
            allowed=True,
            signal_id=signal_id,
            risk_multiplier=Decimal(multiplier),
            structural_stop=Decimal(stop),
            reason_codes=(),
        ),
        exit=None,
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
    entry_bar = market_bar(
        "SZ.000001", datetime(2026, 7, 20, 10, 31, tzinfo=CN)
    )
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
    entry_bar = market_bar(
        "SZ.000001", datetime(2026, 7, 20, 10, 31, tzinfo=CN)
    )
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
        (exit_signal.closed_at, exit_signal.code): (
            allowed_exit(signal_id="exit-a"),
        ),
    }

    run = run_fixture(
        (signal, entry_fill, exit_signal, exit_fill_a, exit_fill_b),
        schedule,
        t_plus_days=0,
    )

    sell_fills = tuple(
        fill
        for fill in run.fills
        if fill.filled and fill.order_id.startswith("exit:")
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
    assert position.shares == expected.shares == 900


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
    limits = replace(RiskLimits(), max_sector_fraction=Decimal("0.10"))

    run = run_fixture(signal_bars + fill_bars, schedule, risk_limits=limits)

    assert len(run.open_positions) == 1
    assert sum(
        position.last_price * position.shares for position in run.open_positions
    ) <= Decimal("10000")


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
            allowed_entry(signal_id="entry-a", stop="1.00"),
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

    run = run_fixture((first, final), schedule)

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

    marked = run_fixture((first, final), schedule)
    liquidated = run_fixture(
        (first, final),
        schedule,
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
    limits = replace(RiskLimits(), max_sector_fraction=Decimal("0.10"))

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
                market_start=point.anchor_at
                - timedelta(minutes=terminal_minutes),
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
