from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from chanlun.decision_support.trading_system.backtest.data_source import (
    NativeSectorBar,
)
from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
    SectorMembershipAt,
)
from chanlun.decision_support.trading_system.backtest.portfolio import (
    BacktestRun,
    EquityPoint,
)
from chanlun.decision_support.trading_system.backtest.report import (
    REQUIRED_ABLATION_IDS,
)
from chanlun.decision_support.trading_system.backtest.runner import (
    build_causal_period_runner,
    build_replay_frames,
    empty_evaluation,
    run_required_ablations,
    run_walk_forward_evaluation,
)
from chanlun.decision_support.trading_system.backtest.walk_forward import (
    FrozenParameters,
)
from tests.trading_system.backtest.helpers import (
    CN,
    dataset,
    minute_bar,
    normal_status,
)


def _minute(index: int):
    opened_at = datetime(2026, 7, 20, 9, 30, tzinfo=CN) + timedelta(minutes=index)
    opened = Decimal("5.00") + Decimal(index) / Decimal("10")
    closed = opened + Decimal("0.05")
    return minute_bar(
        opened_at=opened_at,
        analysis_open=opened,
        analysis_high=closed + Decimal("0.05"),
        analysis_low=opened - Decimal("0.05"),
        analysis_close=closed,
        volume=Decimal("100") + index,
    )


def test_replay_frames_use_analysis_prices_and_only_complete_aggregates() -> None:
    bars = tuple(_minute(index) for index in range(6))

    frames = build_replay_frames(dataset(bars=bars), ())

    one = frames[("SZ.000001", "1m")]
    five = frames[("SZ.000001", "5m")]
    thirty = frames[("SZ.000001", "30m")]
    assert len(one) == 6
    assert one.iloc[0]["open"] == 5.0
    assert one.iloc[0]["close"] == 5.05
    assert len(five) == 1
    assert five.iloc[0]["date"].to_pydatetime() == bars[4].closed_at
    assert five.iloc[0]["open"] == 5.0
    assert five.iloc[0]["close"] == 5.45
    assert five.iloc[0]["volume"] == sum(float(bar.volume) for bar in bars[:5])
    assert thirty.empty


def test_qmt_close_labels_include_auction_in_first_native_five_minutes() -> None:
    auction_close = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
    bars = tuple(
        minute_bar(
            opened_at=auction_close + timedelta(minutes=index - 1),
            analysis_open=Decimal("5.00") + Decimal(index) / Decimal("10"),
            analysis_high=Decimal("5.15") + Decimal(index) / Decimal("10"),
            analysis_low=Decimal("4.95") + Decimal(index) / Decimal("10"),
            analysis_close=Decimal("5.10") + Decimal(index) / Decimal("10"),
        )
        for index in range(6)
    )

    frames = build_replay_frames(dataset(bars=bars), ())

    five = frames[("SZ.000001", "5m")]
    assert len(five) == 1
    assert five.iloc[0]["date"].to_pydatetime() == datetime(
        2026,
        7,
        20,
        9,
        35,
        tzinfo=CN,
    )
    assert five.iloc[0]["open"] == 5.0
    assert five.iloc[0]["close"] == 5.6


def test_aggregated_frames_keep_morning_before_afternoon() -> None:
    morning = datetime(2026, 7, 20, 9, 30, tzinfo=CN)
    afternoon = datetime(2026, 7, 20, 13, 0, tzinfo=CN)
    bars = tuple(
        minute_bar(opened_at=start + timedelta(minutes=index))
        for start in (morning, afternoon)
        for index in range(5)
    )

    five = build_replay_frames(dataset(bars=bars), ())[("SZ.000001", "5m")]

    assert [value.to_pydatetime() for value in five["date"]] == [
        datetime(2026, 7, 20, 9, 35, tzinfo=CN),
        datetime(2026, 7, 20, 13, 5, tzinfo=CN),
    ]


def test_replay_frames_include_native_tdx_sector_frequency_without_synthesis() -> None:
    sector_bar = NativeSectorBar(
        sector_id="tdx-industry:SH.880301",
        index_code="SH.880301",
        frequency="5m",
        opened_at=datetime(2026, 7, 20, 9, 30, tzinfo=CN),
        closed_at=datetime(2026, 7, 20, 9, 35, tzinfo=CN),
        opened=Decimal("1000"),
        high=Decimal("1010"),
        low=Decimal("995"),
        closed=Decimal("1005"),
        volume=Decimal("12345"),
    )

    frames = build_replay_frames(dataset(), (sector_bar,))

    frame = frames[("SH.880301", "5m")]
    assert len(frame) == 1
    assert frame.iloc[0]["open"] == 1000.0
    assert ("SH.880301", "1m") not in frames


def _sparse_walk_forward_dataset() -> BacktestDataset:
    validation_at = datetime(2023, 2, 1, 10, 0, tzinfo=CN)
    test_at = datetime(2023, 8, 1, 10, 0, tzinfo=CN)
    bars = (
        minute_bar(opened_at=validation_at),
        minute_bar(opened_at=test_at),
    )
    statuses = tuple(normal_status(session=bar.opened_at.date()) for bar in bars)
    memberships = tuple(
        SectorMembershipAt(
            session=bar.opened_at.date(),
            sector_id="tdx-industry:SH.880301",
            code=bar.code,
            known_at=bar.opened_at - timedelta(hours=1),
        )
        for bar in bars
    )
    return BacktestDataset(
        bars=bars,
        statuses=statuses,
        memberships=memberships,
        corporate_actions=(),
        membership_as_of_each_session=True,
        point_in_time_adjustment=True,
        source_hashes=(("fixture", "sha256:fixture"),),
    )


def _flat_run(
    observed_at: datetime,
    initial_cash: Decimal,
    net_return: Decimal,
) -> BacktestRun:
    return BacktestRun(
        fills=(),
        trades=(),
        equity_curve=(
            EquityPoint(
                observed_at,
                initial_cash,
                Decimal("0"),
                initial_cash,
                Decimal("0"),
            ),
            EquityPoint(
                observed_at + timedelta(minutes=1),
                initial_cash * (Decimal("1") + net_return),
                Decimal("0"),
                initial_cash * (Decimal("1") + net_return),
                Decimal("0"),
            ),
        ),
        open_positions=(),
        pending_exits=(),
    )


def test_walk_forward_selects_on_validation_and_locks_test_parameters() -> None:
    calls: list[tuple[date, Decimal, bool, Decimal, Decimal]] = []

    def period_runner(period, parameters, initial_cash):
        observed = period.bars[0].closed_at
        calls.append(
            (
                observed.date(),
                parameters.base_trade_risk,
                parameters.first_center_three_buy_only,
                parameters.max_portfolio_heat,
                parameters.first_buy_risk_multiplier,
            )
        )
        validation = observed.month == 2
        net_return = parameters.base_trade_risk if validation else Decimal("0.01")
        return _flat_run(observed, initial_cash, net_return)

    research = run_walk_forward_evaluation(
        _sparse_walk_forward_dataset(),
        start=date(2020, 1, 1),
        end=date(2024, 1, 10),
        initial_cash=Decimal("100000"),
        bootstrap_repetitions=20,
        period_runner=period_runner,
    )

    validation_calls = [row for row in calls if row[0].month == 2]
    test_calls = [row for row in calls if row[0].month == 8]
    assert len(validation_calls) == 16
    assert test_calls == [
        (
            date(2023, 8, 1),
            Decimal("0.005"),
            False,
            Decimal("0.015"),
            Decimal("0.25"),
        )
    ]
    assert len(research.evaluation.walk_forward_windows) == 1
    assert research.evaluation.aggregate_run.equity_curve[-1].equity == Decimal(
        "101000"
    )


def test_short_span_returns_explicit_empty_evaluation() -> None:
    research = run_walk_forward_evaluation(
        dataset(),
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        initial_cash=Decimal("100000"),
        bootstrap_repetitions=20,
    )
    direct = empty_evaluation(
        initial_cash=Decimal("100000"),
        observed_at=datetime(2026, 1, 1, tzinfo=CN),
        bootstrap_repetitions=20,
    )

    assert research.evaluation == direct
    assert research.limitations == ("insufficient_calendar_span_for_walk_forward",)


def test_causal_period_runner_applies_selected_policy_and_risk(
    monkeypatch,
) -> None:
    from chanlun.decision_support.trading_system.backtest import runner

    captured: dict[str, object] = {}

    class RecordingReplay:
        def __init__(self, **kwargs) -> None:
            captured["replay"] = kwargs

    def fake_backtest(period, **kwargs):
        captured["period"] = period
        captured.update(kwargs)
        return _flat_run(period.bars[0].closed_at, kwargs["initial_cash"], Decimal("0"))

    monkeypatch.setattr(runner, "CausalStructureReplay", RecordingReplay)
    monkeypatch.setattr(runner, "run_event_backtest", fake_backtest)
    parameters = FrozenParameters(
        base_trade_risk=Decimal("0.0035"),
        first_center_three_buy_only=False,
        max_portfolio_heat=Decimal("0.015"),
        first_buy_risk_multiplier=Decimal("0.25"),
    )

    period_runner = build_causal_period_runner(dataset(), ())
    period_runner(dataset(), parameters, Decimal("100000"))

    engine = captured["engine"]
    risk_limits = captured["risk_limits"]
    assert engine._policy.first_center_three_buy_only is False
    assert engine._policy.first_buy_risk_multiplier == Decimal("0.25")
    assert risk_limits.base_trade_risk == Decimal("0.0035")
    assert risk_limits.max_portfolio_heat == Decimal("0.015")
    assert captured["terminal_liquidation"] is True


def test_required_ablations_run_each_locked_variant_on_test_only() -> None:
    parameters = FrozenParameters(
        base_trade_risk=Decimal("0.005"),
        first_center_three_buy_only=True,
        max_portfolio_heat=Decimal("0.02"),
        first_buy_risk_multiplier=Decimal("0.50"),
    )
    calls: list[tuple[str, date]] = []

    def factory(ablation_id: str):
        ordinal = REQUIRED_ABLATION_IDS.index(ablation_id)

        def run_period(period, locked_parameters, initial_cash):
            assert locked_parameters == parameters
            observed = period.bars[0].closed_at
            calls.append((ablation_id, observed.date()))
            return _flat_run(
                observed,
                initial_cash,
                Decimal(ordinal) / Decimal("100"),
            )

        return run_period

    rows = run_required_ablations(
        _sparse_walk_forward_dataset(),
        start=date(2020, 1, 1),
        end=date(2024, 1, 10),
        initial_cash=Decimal("100000"),
        selected_parameters=(("wf-001", parameters),),
        period_runner_factory=factory,
        data_grade="research_only",
    )

    assert tuple(row.ablation_id for row in rows) == REQUIRED_ABLATION_IDS
    assert all(row.completed for row in rows)
    assert all(row.data_grade == "research_only" for row in rows)
    assert len(calls) == 6
    assert {session for _ablation, session in calls} == {date(2023, 8, 1)}
    assert rows[-1].net_return == Decimal("0.05")


def test_causal_ablation_layers_are_cumulative(monkeypatch) -> None:
    from chanlun.decision_support.trading_system.backtest import runner

    captured: list[tuple[object, object]] = []

    class RecordingReplay:
        def __init__(self, **_kwargs) -> None:
            pass

    def fake_backtest(period, **kwargs):
        captured.append((kwargs["engine"]._policy, kwargs["risk_limits"]))
        return _flat_run(period.bars[0].closed_at, kwargs["initial_cash"], Decimal("0"))

    monkeypatch.setattr(runner, "CausalStructureReplay", RecordingReplay)
    monkeypatch.setattr(runner, "run_event_backtest", fake_backtest)
    parameters = FrozenParameters(
        base_trade_risk=Decimal("0.005"),
        first_center_three_buy_only=True,
        max_portfolio_heat=Decimal("0.015"),
        first_buy_risk_multiplier=Decimal("0.50"),
    )

    for ablation_id in REQUIRED_ABLATION_IDS:
        build_causal_period_runner(
            dataset(),
            (),
            ablation_id=ablation_id,
        )(dataset(), parameters, Decimal("100000"))

    original_policy, original_risk = captured[0]
    trigger_policy, _trigger_risk = captured[3]
    first_center_policy, _first_center_risk = captured[4]
    final_policy, final_risk = captured[5]
    assert original_policy.require_sector_eligibility is False
    assert original_policy.require_thirty_minute_context is False
    assert original_policy.require_confirmed_one_minute is False
    assert trigger_policy.require_confirmed_one_minute is True
    assert first_center_policy.first_center_three_buy_only is True
    assert final_policy.require_sector_eligibility is True
    assert original_risk.max_portfolio_heat == Decimal("1")
    assert final_risk.max_portfolio_heat == Decimal("0.015")
