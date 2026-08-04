from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FACT_SCHEMA,
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
    SparseEvaluationFact,
    SymbolResearchFacts,
    first_matching_trigger,
    run_sparse_portfolio,
    setup_active_ends,
    sparse_evaluation_times,
    load_qmt_daily_frame,
    load_qmt_frame,
)
from tests.trading_system.backtest.helpers import minute_bar
from tests.trading_system.helpers import CN, confirmed_point, eligible_sector


def test_newer_same_lane_supersedes_setup_before_four_day_expiry() -> None:
    first = confirmed_point("3buy", available_minutes_after=0)
    second = confirmed_point(
        "3buy",
        minutes_after=60,
        available_minutes_after=0,
        center_id="center-b",
    )

    ends = setup_active_ends((first, second))

    assert ends[first.point_id] == (second.available_at, True)
    assert ends[second.point_id] == (
        second.available_at + timedelta(days=4),
        False,
    )


def test_trigger_must_match_side_time_and_setup_price_band() -> None:
    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    wrong_price = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=10.2,
        minutes_after=1,
    )
    wrong_side = confirmed_point(
        "1sell",
        frequency="1m",
        anchor=9.9,
        minutes_after=2,
    )
    match = confirmed_point(
        "2buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=3,
    )

    trigger = first_matching_trigger(
        setup,
        (wrong_price, wrong_side, match),
        active_end=setup.available_at + timedelta(days=4),
        end_exclusive=False,
    )

    assert trigger == match


def test_sparse_times_start_at_trigger_then_use_thirty_minute_closes() -> None:
    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    trigger = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=5,
    )
    trigger_at = trigger.available_at
    one_closes = tuple(
        trigger_at + timedelta(minutes=offset) for offset in range(-2, 93)
    )
    thirty_closes = (
        trigger_at + timedelta(minutes=25),
        trigger_at + timedelta(minutes=55),
        trigger_at + timedelta(minutes=85),
    )

    observed = sparse_evaluation_times(
        five_points=(setup,),
        one_points=(trigger,),
        thirty_closes=thirty_closes,
        one_closes=one_closes,
        effective_start=datetime(2026, 7, 20, 9, 30, tzinfo=CN),
        requested_end=datetime(2026, 7, 20, 15, 0, tzinfo=CN),
    )

    assert observed == (trigger_at, *thirty_closes)


def test_superseding_timestamp_is_excluded_from_previous_setup() -> None:
    first = confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    second = confirmed_point(
        "3buy",
        anchor=10.1,
        stop=9.9,
        center_zg=10.0,
        minutes_after=60,
        center_id="center-b",
    )
    trigger = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=5,
    )

    observed = sparse_evaluation_times(
        five_points=(first, second),
        one_points=(trigger,),
        thirty_closes=(
            first.available_at + timedelta(minutes=30),
            second.available_at,
        ),
        one_closes=tuple(
            first.available_at + timedelta(minutes=value) for value in range(1, 90)
        ),
        effective_start=first.available_at,
        requested_end=second.available_at + timedelta(minutes=30),
    )

    assert second.available_at not in observed


def test_sparse_portfolio_fills_next_minute_and_marks_terminal_position(
    monkeypatch,
) -> None:
    from chanlun.decision_support.trading_system.backtest import fixed_year

    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    trigger = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=5,
    )
    observed_at = trigger.available_at
    event_bar = minute_bar(
        opened_at=observed_at - timedelta(minutes=1),
        raw_open="10.00",
        raw_high="10.05",
        raw_low="9.95",
        raw_close="10.00",
        analysis_open="10.00",
        analysis_high="10.05",
        analysis_low="9.95",
        analysis_close="10.00",
        previous_raw_close="10.00",
        volume="1000000",
    )
    evaluation = SparseEvaluationFact(observed_at, "neutral", event_bar)
    sector = eligible_sector()
    facts = SymbolResearchFacts(
        schema=FACT_SCHEMA,
        algorithm_revision="sha256:" + "a" * 64,
        source_revision="sha256:" + "b" * 64,
        code="SZ.000001",
        sector_id=sector.sector_id,
        requested_start=observed_at.date(),
        requested_end=observed_at.date(),
        effective_start=observed_at.date(),
        row_counts=(("30m", 1), ("5m", 1), ("1m", 1)),
        thirty_points=(),
        five_points=(setup,),
        one_points=(trigger,),
        evaluations=(evaluation,),
    )
    sector_facts = SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision="sha256:" + "a" * 64,
        source_revision="sha256:" + "c" * 64,
        sector_id=sector.sector_id,
        sector_name=sector.sector_name,
        member_count=8,
        row_count=1,
        thirty_points=(),
        assessments=((observed_at, sector),),
    )
    dates = (
        observed_at + timedelta(minutes=1),
        observed_at + timedelta(minutes=2),
    )
    frame = pd.DataFrame(
        {
            "code": ["SZ.000001", "SZ.000001"],
            "date": list(dates),
            "open": [10.0, 10.1],
            "high": [10.15, 10.2],
            "low": [9.95, 10.05],
            "close": [10.1, 10.15],
            "volume": [1_000_000.0, 1_000_000.0],
        }
    )

    sources = iter(
        (
            fixed_year._ActiveMinuteSource(
                frame=frame,
                dates=dates,
                previous_by_session={observed_at.date(): Decimal("10")},
                index=0,
            ),
            fixed_year._ActiveMinuteSource(
                frame=frame.iloc[0:0],
                dates=(),
                previous_by_session={},
                index=0,
            ),
        )
    )
    monkeypatch.setattr(
        fixed_year,
        "_active_minute_source",
        lambda *_args, **_kwargs: next(sources),
    )

    run = run_sparse_portfolio(
        (facts,),
        {sector.sector_id: sector_facts},
        initial_cash=Decimal("1000000"),
        minute_timeline=(observed_at, *dates),
        selection_sources_by_code={
            facts.code: ("QMT_SECTOR_TRIGGER",),
        },
    )

    assert run.trades == ()
    assert len(run.open_positions) == 1
    assert run.open_positions[0].opened_at == dates[0]
    assert run.open_positions[0].last_price == Decimal("10.15")
    assert run.equity_curve[0].closed_at == datetime.combine(
        observed_at.date(),
        time(9, 30),
        tzinfo=CN,
    )
    assert run.equity_curve[-1].closed_at == datetime.combine(
        observed_at.date(),
        time(15, 0),
        tzinfo=CN,
    )


def test_relevant_setup_cannot_silently_accept_missing_qmt_one_minute_data(
    monkeypatch,
) -> None:
    from chanlun.decision_support.trading_system.backtest import fixed_year

    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
        minutes_after=60 * 24 * 10,
    )
    context = pd.DataFrame(
        {
            "code": ["SZ.000001"],
            "date": [setup.available_at],
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.0],
            "volume": [1000.0],
        }
    )
    context.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw-v1",
    )
    monkeypatch.setattr(
        fixed_year,
        "load_qmt_frame",
        lambda _code, frequency, **_kwargs: (
            fixed_year._empty_frame("SZ.000001")
            if frequency == "1m"
            else context.copy()
        ),
    )
    monkeypatch.setattr(
        fixed_year,
        "_causal_confirmed_points",
        lambda _code, frequency, _frame, **_kwargs: (
            (setup,) if frequency == "5m" else ()
        ),
    )

    requested_start = setup.available_at.date()
    with pytest.raises(RuntimeError, match="QMT 1m history is unavailable"):
        fixed_year.build_symbol_facts(
            code="SZ.000001",
            sector_id="qmt-sw1:S48",
            warmup_start=requested_start - timedelta(days=30),
            requested_start=requested_start,
            effective_start=requested_start,
            requested_end=requested_start + timedelta(days=30),
            algorithm_revision="sha256:" + "a" * 64,
        )


def test_qmt_frame_retries_a_transient_empty_native_response(monkeypatch) -> None:
    native = "000001.SZ"
    observed_at = datetime(2026, 7, 24, 10, 0, tzinfo=CN)
    timestamp_ms = int(observed_at.timestamp() * 1000)

    class FakeXtdata:
        enable_hello = True
        calls = 0

        @classmethod
        def get_market_data(cls, **_kwargs):
            cls.calls += 1
            if cls.calls == 1:
                return {}
            values = {
                "time": timestamp_ms,
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1000.0,
            }
            return {
                field: pd.DataFrame([[value]], index=[native])
                for field, value in values.items()
            }

    monkeypatch.setitem(
        __import__("sys").modules,
        "xtquant",
        SimpleNamespace(xtdata=FakeXtdata),
    )
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system.backtest.fixed_year.wall_time.sleep",
        lambda _seconds: None,
    )

    frame = load_qmt_frame(
        "SZ.000001",
        "30m",
        start_at=observed_at - timedelta(hours=1),
        end_at=observed_at,
    )

    assert FakeXtdata.calls == 2
    assert len(frame) == 1
    assert frame.iloc[0]["date"] == observed_at


def test_qmt_native_daily_is_visible_only_at_close_on_the_causal_price_basis(
    monkeypatch,
) -> None:
    native = "000001.SZ"
    session = datetime(2026, 7, 24, 0, 0, tzinfo=CN)
    timestamp_ms = int(session.timestamp() * 1000)

    class FakeXtdata:
        enable_hello = True

        @staticmethod
        def get_market_data(**_kwargs):
            values = {
                "time": timestamp_ms,
                "open": 10.0,
                "high": 10.1,
                "low": 9.9,
                "close": 10.0,
                "volume": 1000.0,
            }
            return {
                field: pd.DataFrame([[value]], index=[native])
                for field, value in values.items()
            }

    monkeypatch.setitem(
        __import__("sys").modules,
        "xtquant",
        SimpleNamespace(xtdata=FakeXtdata),
    )
    frame = load_qmt_daily_frame(
        "SZ.000001",
        start_at=session,
        end_at=session.replace(hour=15),
    )

    assert frame.iloc[0]["date"] == session.replace(hour=15)
    assert frame.attrs["price_basis_provider"] == "qmt"
    assert frame.attrs["price_basis_adjustment"] == "causal-forward-ex-date-v1"
    assert str(frame.attrs["price_basis_revision"]).startswith("sha256:")
