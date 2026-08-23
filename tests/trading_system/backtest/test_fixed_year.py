from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.core.strict_structure.current_events import TerminalSegmentReference
from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.trading_system.backtest import fixed_year
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    QMTLocalKlineAudit,
)
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    FACT_SCHEMA,
    FiveMinuteWarmupFact,
    PointVisibilityInterval,
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
    SparseEvaluationFact,
    SymbolResearchFacts,
    build_symbol_bundle,
    first_matching_segment_difference,
    run_sparse_portfolio,
    setup_active_ends,
    sparse_evaluation_times,
    load_qmt_daily_frame,
    load_qmt_frame,
)
from tests.trading_system.backtest.helpers import minute_bar
from tests.trading_system.helpers import (
    CN,
    confirmed_point,
    eligible_sector,
    valid_selection_research,
)


def test_symbol_bundle_keeps_recursive_points_as_context_not_trade_setups() -> None:
    observed_at = datetime(2026, 7, 20, 10, 1, tzinfo=CN)
    sector = eligible_sector()
    daily_l0 = confirmed_point("1sell", frequency="d", level=0)
    thirty_l0 = confirmed_point("1buy", frequency="30m", level=0)
    thirty_l1 = confirmed_point("1buy", frequency="30m", level=1)
    five_l0 = confirmed_point("3buy", frequency="5m", level=0)
    five_l1 = confirmed_point("2buy", frequency="5m", level=1)
    one_l0 = confirmed_point("1buy", frequency="1m", level=0)
    one_l1 = confirmed_point("1buy", frequency="1m", level=1)
    evaluation = SparseEvaluationFact(
        observed_at=observed_at,
        thirty_direction="neutral",
        bar=minute_bar(
            opened_at=observed_at - timedelta(minutes=1),
        ),
    )
    facts = SymbolResearchFacts(
        schema=FACT_SCHEMA,
        algorithm_revision="sha256:" + "a" * 64,
        source_revision="sha256:" + "b" * 64,
        code="SZ.000001",
        sector_id=sector.sector_id,
        requested_start=observed_at.date(),
        requested_end=observed_at.date(),
        effective_start=observed_at.date(),
        row_counts=(("d", 1), ("30m", 1), ("5m", 1), ("1m", 1)),
        daily_points=(daily_l0,),
        thirty_points=(thirty_l0, thirty_l1),
        five_points=(five_l0, five_l1),
        one_points=(one_l0, one_l1),
        evaluations=(evaluation,),
    )

    bundle = build_symbol_bundle(facts, evaluation, sector)

    assert bundle.physical_timeframe_recursive is True
    assert bundle.daily_points == (daily_l0,)
    assert bundle.thirty_points == (thirty_l0, thirty_l1)
    assert bundle.five_points == (five_l0,)
    assert bundle.one_points == (one_l0, one_l1)
    assert bundle.opposite_points == (
        daily_l0,
        thirty_l0,
        thirty_l1,
        five_l0,
        five_l1,
        one_l0,
        one_l1,
    )


def test_symbol_bundle_uses_current_context_but_keeps_one_minute_event_ledger(
) -> None:
    old = confirmed_point("3buy", center_id="old-center")
    current = confirmed_point(
        "3buy",
        center_id="current-center",
        minutes_after=5,
    )
    observed_at = old.available_at + timedelta(minutes=30)
    old_thirty = confirmed_point("1sell", frequency="30m")
    current_thirty = confirmed_point(
        "2sell",
        frequency="30m",
        minutes_after=10,
    )
    old_one = confirmed_point("1sell", frequency="1m")
    current_one = confirmed_point(
        "2sell",
        frequency="1m",
        minutes_after=10,
    )
    sector = eligible_sector()
    evaluation = SparseEvaluationFact(
        observed_at=observed_at,
        thirty_direction="neutral",
        bar=minute_bar(opened_at=observed_at - timedelta(minutes=1)),
    )
    facts = SymbolResearchFacts(
        schema=FACT_SCHEMA,
        algorithm_revision="sha256:" + "a" * 64,
        source_revision="sha256:" + "b" * 64,
        code=old.code,
        sector_id=sector.sector_id,
        requested_start=observed_at.date(),
        requested_end=observed_at.date(),
        effective_start=observed_at.date(),
        row_counts=(("d", 1), ("30m", 1), ("5m", 2), ("1m", 1)),
        daily_points=(),
        thirty_points=(old_thirty, current_thirty),
        five_points=(old, current),
        one_points=(old_one, current_one),
        evaluations=(evaluation,),
        thirty_point_visibility=(
            PointVisibilityInterval(
                old_thirty.point_id,
                old_thirty.available_at,
                observed_at,
            ),
            PointVisibilityInterval(
                current_thirty.point_id,
                observed_at,
            ),
        ),
        five_point_visibility=(
            PointVisibilityInterval(old.point_id, old.available_at, observed_at),
            PointVisibilityInterval(current.point_id, observed_at),
        ),
        one_point_visibility=(
            PointVisibilityInterval(
                old_one.point_id,
                old_one.available_at,
                observed_at,
            ),
            PointVisibilityInterval(current_one.point_id, observed_at),
        ),
    )

    bundle = build_symbol_bundle(facts, evaluation, sector)

    assert bundle.thirty_points == (current_thirty,)
    assert bundle.five_points == (current,)
    assert bundle.one_points == (old_one, current_one)
    assert bundle.opposite_points == (current_thirty, current, current_one)


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


def test_newer_opposite_direction_supersedes_same_point_family() -> None:
    old_sell = confirmed_point(
        "3sell",
        center_id="old-sell-center",
        stop=10.2,
        center_zd=10.1,
        center_zg=10.3,
    )
    new_buy = confirmed_point(
        "3buy",
        minutes_after=60,
        center_id="new-buy-center",
    )

    ends = setup_active_ends((old_sell, new_buy))

    assert ends[old_sell.point_id] == (new_buy.available_at, True)
    assert ends[new_buy.point_id] == (
        new_buy.anchor_at + timedelta(days=4),
        False,
    )


def test_backtest_does_not_reopen_expired_anchor_on_late_confirmation() -> None:
    delayed = confirmed_point(
        "3sell",
        anchor=18.89,
        stop=24.76,
        center_zd=24.76,
        center_zg=27.04,
        minutes_after=-(27 * 24 * 60),
        available_minutes_after=27 * 24 * 60,
    )
    delayed = replace(delayed, confirmed_at=delayed.available_at)

    active_end, superseded = setup_active_ends((delayed,))[delayed.point_id]

    assert active_end == delayed.anchor_at + timedelta(days=4)
    assert active_end < delayed.available_at
    assert superseded is False

    assert sparse_evaluation_times(
        five_points=(delayed,),
        one_points=(),
        thirty_closes=(),
        one_closes=(delayed.available_at,),
        effective_start=delayed.available_at,
        requested_end=delayed.available_at + timedelta(days=1),
    ) == ()


def test_terminal_lineage_visibility_keeps_late_confirmed_setup_current() -> None:
    delayed = confirmed_point(
        "3sell",
        anchor=18.89,
        stop=24.76,
        center_zd=24.76,
        center_zg=27.04,
        minutes_after=-(27 * 24 * 60),
        available_minutes_after=27 * 24 * 60,
    )
    delayed = replace(
        delayed,
        confirmed_at=delayed.available_at,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:late-confirmed-current",
            source_kind=SourceKind.SEGMENT,
            direction="up",
            state="locked",
            market_start=delayed.anchor_at - timedelta(minutes=30),
            market_end=delayed.anchor_at,
            available_at=delayed.available_at,
        ),
    )

    assert delayed.anchor_at + timedelta(days=4) < delayed.available_at
    assert sparse_evaluation_times(
        five_points=(delayed,),
        one_points=(),
        thirty_closes=(),
        one_closes=(delayed.available_at,),
        effective_start=delayed.available_at,
        requested_end=delayed.available_at + timedelta(days=1),
        five_point_visibility=(
            PointVisibilityInterval(
                delayed.point_id,
                delayed.available_at,
            ),
        ),
    ) == (delayed.available_at,)


def test_segment_difference_must_match_side_time_and_setup_price_band() -> None:
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

    segment = first_matching_segment_difference(
        setup,
        (wrong_price, wrong_side, match),
        active_end=setup.available_at + timedelta(days=4),
        end_exclusive=False,
    )

    assert segment == match


def test_historical_segment_difference_cannot_cross_symbol_boundary() -> None:
    setup = confirmed_point(
        "3buy",
        code="SZ.000001",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    other_symbol = confirmed_point(
        "1buy",
        code="SH.600000",
        frequency="1m",
        anchor=9.9,
        minutes_after=1,
    )

    assert (
        first_matching_segment_difference(
            setup,
            (other_symbol,),
            active_end=setup.available_at + timedelta(days=4),
            end_exclusive=False,
        )
        is None
    )


def test_historical_replay_accepts_valid_third_class_one_minute_segment() -> None:
    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    continuation = confirmed_point(
        "3buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=1,
    )

    assert first_matching_segment_difference(
        setup,
        (continuation,),
        active_end=setup.available_at + timedelta(days=4),
        end_exclusive=False,
    ) is continuation


def test_historical_segment_cannot_be_backfilled_from_five_minute_formation() -> None:
    setup = confirmed_point(
        "2buy",
        anchor=10.0,
        stop=9.8,
        available_minutes_after=5,
    )
    segment = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=2,
    )

    assert first_matching_segment_difference(
        setup,
        (segment,),
        active_end=setup.available_at + timedelta(days=4),
        end_exclusive=False,
    ) is None


def test_historical_segment_uses_terminal_segment_start_not_point_anchor() -> None:
    setup = confirmed_point(
        "3sell",
        anchor=10.0,
        stop=10.2,
        center_zd=10.1,
        center_zg=10.3,
    )
    setup = replace(
        setup,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:historical:sell",
            source_kind=SourceKind.SEGMENT,
            direction="up",
            state="locked",
            market_start=setup.anchor_at - timedelta(minutes=30),
            market_end=setup.anchor_at,
            available_at=setup.available_at,
        ),
    )
    segment = confirmed_point(
        "1sell",
        frequency="1m",
        anchor=10.1,
        stop=10.2,
        minutes_after=-5,
        available_minutes_after=6,
    )

    assert first_matching_segment_difference(
        setup,
        (segment,),
        active_end=setup.available_at + timedelta(days=4),
        end_exclusive=False,
    ) is segment


def test_historical_segment_anchored_before_terminal_segment_is_rejected_when_seen_late(
) -> None:
    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    terminal_start = setup.anchor_at - timedelta(minutes=30)
    setup = replace(
        setup,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:historical:late-seen-buy",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=terminal_start,
            market_end=setup.anchor_at,
            available_at=setup.available_at,
        ),
    )
    old_segment_seen_late = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=-31,
        available_minutes_after=32,
    )

    assert old_segment_seen_late.anchor_at < terminal_start
    assert old_segment_seen_late.available_at > terminal_start
    assert (
        first_matching_segment_difference(
            setup,
            (old_segment_seen_late,),
            active_end=setup.available_at + timedelta(days=4),
            end_exclusive=False,
        )
        is None
    )


def test_sparse_times_include_one_minute_locator_close() -> None:
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

    assert observed == (one_closes[0], trigger_at, *thirty_closes)


def test_one_minute_visibility_starts_when_five_minute_setup_is_available() -> None:
    from chanlun.core.strict_structure.models import SourceKind
    from chanlun.decision_support.trading_system.backtest import fixed_year
    from chanlun.decision_support.trading_system.models import (
        TerminalSegmentReference,
    )

    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
        available_minutes_after=60,
    )
    terminal_start = setup.anchor_at - timedelta(minutes=30)
    setup = replace(
        setup,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:formation-window",
            source_kind=SourceKind.SEGMENT,
            direction="down",
            state="locked",
            market_start=terminal_start,
            market_end=setup.anchor_at,
            available_at=setup.available_at,
        ),
    )
    active_end = setup.available_at + timedelta(days=1)

    assert fixed_year._one_minute_visibility_windows(
        (setup,),
        {setup.point_id: (active_end, False)},
        end_at=active_end,
    ) == ((setup.available_at, active_end),)


def test_one_minute_visibility_preserves_disjoint_five_minute_epochs() -> None:
    setup = confirmed_point(
        "2sell",
        anchor=10.0,
        stop=10.2,
        center_zd=9.9,
        center_zg=10.1,
    )
    first_end = setup.available_at + timedelta(days=3)
    second_start = setup.available_at + timedelta(days=21)
    second_end = setup.available_at + timedelta(days=72)
    visibility = (
        PointVisibilityInterval(
            point_id=setup.point_id,
            visible_from=setup.available_at,
            visible_until=first_end,
        ),
        PointVisibilityInterval(
            point_id=setup.point_id,
            visible_from=second_start,
            visible_until=second_end,
        ),
    )

    assert fixed_year._one_minute_visibility_windows(
        (setup,),
        end_at=second_end,
        point_visibility=visibility,
    ) == (
        (setup.available_at, first_end),
        (second_start, second_end),
    )


def test_sector_facts_use_only_points_current_at_each_decision(
    monkeypatch,
) -> None:
    point = confirmed_point(
        "2sell",
        frequency="30m",
        anchor=10.0,
        stop=10.2,
        center_zd=9.9,
        center_zg=10.1,
    )
    first = point.available_at
    second = first + timedelta(minutes=30)
    visibility = PointVisibilityInterval(
        point_id=point.point_id,
        visible_from=first,
        visible_until=second,
    )
    ledger = SimpleNamespace(
        points=(point,),
        point_visibility=(visibility,),
    )
    monkeypatch.setattr(
        fixed_year,
        "final_confirmed_structure_events",
        lambda *_args, **_kwargs: ledger,
    )
    monkeypatch.setattr(
        fixed_year,
        "causal_directions",
        lambda *_args, **_kwargs: (((first, "neutral"), (second, "neutral")), 0),
    )
    frame = pd.DataFrame({"date": [first, second]})

    facts = fixed_year.sector_facts_from_frame(
        sector_id="qmt-sw1:801010",
        sector_name="农林牧渔",
        member_count=8,
        frame=frame,
        observed_times=(first, second),
        algorithm_revision="sha256:" + "a" * 64,
        source_revision="sha256:" + "b" * 64,
        expected_closes=(first, second),
    )

    assessments = dict(facts.assessments)
    assert facts.thirty_points == (point,)
    assert facts.thirty_point_visibility == (visibility,)
    assert assessments[first].thirty_context is not None
    assert assessments[first].thirty_context.dominant_point_id == point.point_id
    assert assessments[second].thirty_context is not None
    assert assessments[second].thirty_context.dominant_point_id is None


def test_one_minute_replay_uses_bounded_cold_history_per_merged_epoch(
    monkeypatch,
) -> None:
    start = datetime(2026, 1, 1, 9, 31, tzinfo=CN)
    dates = tuple(start + timedelta(minutes=index) for index in range(13_020))
    frame = pd.DataFrame(
        {
            "code": ["SZ.000001"] * len(dates),
            "date": dates,
            "open": [10.0] * len(dates),
            "high": [10.1] * len(dates),
            "low": [9.9] * len(dates),
            "close": [10.0] * len(dates),
            "volume": [1000.0] * len(dates),
        }
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    calls: list[tuple[int, datetime, datetime, object, object]] = []

    def replay(_code, _frequency, chunk, **kwargs):
        calls.append(
            (
                len(chunk),
                pd.Timestamp(chunk.iloc[0]["date"]).to_pydatetime(),
                pd.Timestamp(chunk.iloc[-1]["date"]).to_pydatetime(),
                kwargs.get("visibility_windows"),
                kwargs.get("recursive_level_limit"),
            )
        )
        assert chunk.attrs["structure_price_quantum"] == "0.01"
        return SimpleNamespace(points=(), point_visibility=())

    monkeypatch.setattr(fixed_year, "_causal_confirmed_structure_events", replay)
    first_start = dates[12_050]
    first_end = dates[12_060]
    overlapping_end = dates[12_070]
    second_start = dates[13_000]
    second_end = dates[13_010]

    assert fixed_year._causal_one_minute_points_by_windows(
        "SZ.000001",
        frame,
        (
            (first_start, first_end),
            (dates[12_059], overlapping_end),
            (second_start, second_end),
        ),
    ) == ()
    assert calls == [
        (
            12_021,
            dates[50],
            overlapping_end,
            ((first_start, overlapping_end),),
            1,
        ),
        (
            12_011,
            dates[1_000],
            second_end,
            ((second_start, second_end),),
            1,
        ),
    ]


def test_earlier_geometry_time_requires_independent_production_visibility(
    monkeypatch,
) -> None:
    point = confirmed_point("1sell", frequency="1m")
    checkpoint = point.available_at + timedelta(minutes=17)
    dates = tuple(
        value.to_pydatetime()
        for value in pd.date_range(
            end=point.available_at,
            periods=960,
            freq="min",
        )
    )
    source = pd.DataFrame({"date": dates})
    source.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    rebuilt = confirmed_point(
        "1sell",
        frequency="1m",
        center_id="rebuilt-center",
    )
    returned: tuple = (rebuilt,)

    def production(*_args, **_kwargs):
        return returned

    monkeypatch.setattr(fixed_year, "_production_current_points", production)

    assert fixed_year._causally_verified_point_available_at(
        code=point.code,
        frequency="1m",
        frame=source,
        dates=dates,
        point=point,
        checkpoint=checkpoint,
        occurrence_cache={},
    ) == point.available_at

    returned = ()
    assert fixed_year._causally_verified_point_available_at(
        code=point.code,
        frequency="1m",
        frame=source,
        dates=dates,
        point=point,
        checkpoint=checkpoint,
        occurrence_cache={},
    ) == checkpoint


def test_operation_identity_allows_terminal_start_refinement() -> None:
    point = confirmed_point("1sell", frequency="1m")
    first = replace(
        point,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:first-tail",
            source_kind=SourceKind.SEGMENT,
            direction="up",
            state="formed",
            market_start=point.anchor_at - timedelta(minutes=60),
            market_end=point.anchor_at,
            available_at=point.available_at,
        ),
    )
    refined_at = point.available_at + timedelta(minutes=17)
    refined = replace(
        point,
        confirmed_at=refined_at,
        available_at=refined_at,
        terminal_segment=TerminalSegmentReference(
            role="latest_completed",
            structural_level=0,
            unit_id="segment:refined-tail",
            source_kind=SourceKind.SEGMENT,
            direction="up",
            state="formed",
            market_start=point.anchor_at - timedelta(minutes=30),
            market_end=point.anchor_at,
            available_at=refined_at,
        ),
    )

    assert fixed_year._operation_point_identity_signature(first) == (
        fixed_year._operation_point_identity_signature(refined)
    )


def test_one_minute_replay_does_not_resurrect_pre_setup_event(
    monkeypatch,
) -> None:
    point = confirmed_point("1buy", frequency="1m")
    dates = tuple(
        point.available_at + timedelta(minutes=index) for index in range(20)
    )
    frame = pd.DataFrame(
        {
            "code": [point.code] * len(dates),
            "date": dates,
            "open": [10.0] * len(dates),
            "high": [10.1] * len(dates),
            "low": [9.9] * len(dates),
            "close": [10.0] * len(dates),
            "volume": [1000.0] * len(dates),
        }
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    first_window = (dates[1], dates[4])
    second_window = (dates[10], dates[14])
    calls = 0

    def replay(_code, _frequency, _chunk, **kwargs):
        nonlocal calls
        calls += 1
        start, end = kwargs["visibility_windows"][0]
        refined = (
            point
            if calls == 1
            else replace(point, evidence_codes=("later_audit_lock",))
        )
        return SimpleNamespace(
            points=(refined,),
            point_visibility=(
                PointVisibilityInterval(refined.point_id, start, end),
            ),
        )

    monkeypatch.setattr(fixed_year, "_causal_confirmed_structure_events", replay)

    points, visibility = fixed_year._causal_one_minute_events_by_windows(
        point.code,
        frame,
        (first_window, second_window),
    )

    assert points == ()
    assert visibility == ()


def test_one_minute_replay_keeps_event_created_inside_active_setup_epoch(
    monkeypatch,
) -> None:
    point = confirmed_point("1buy", frequency="1m")
    dates = tuple(
        point.available_at + timedelta(minutes=index) for index in range(10)
    )
    source = pd.DataFrame({"date": dates})
    source.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    window = (point.available_at, dates[5])

    def replay(*_args, **_kwargs):
        return SimpleNamespace(
            points=(point,),
            point_visibility=(
                PointVisibilityInterval(point.point_id, *window),
            ),
        )

    monkeypatch.setattr(fixed_year, "_causal_confirmed_structure_events", replay)

    assert fixed_year._causal_one_minute_events_by_windows(
        point.code,
        source,
        (window,),
    ) == (
        (point,),
        (PointVisibilityInterval(point.point_id, *window),),
    )


def test_one_minute_replay_drops_point_visible_only_at_exclusive_window_end(
    monkeypatch,
) -> None:
    point = confirmed_point("1buy", frequency="1m")
    dates = tuple(
        point.available_at + timedelta(minutes=index) for index in range(10)
    )
    source = pd.DataFrame({"date": dates})
    source.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    window = (dates[1], dates[5])
    later_window = (dates[7], dates[9])

    def replay(*_args, **_kwargs):
        if _kwargs["visibility_windows"] == (later_window,):
            return SimpleNamespace(points=(), point_visibility=())
        return SimpleNamespace(
            points=(point,),
            point_visibility=(
                PointVisibilityInterval(point.point_id, window[1]),
            ),
        )

    monkeypatch.setattr(fixed_year, "_causal_confirmed_structure_events", replay)

    assert fixed_year._causal_one_minute_events_by_windows(
        point.code,
        source,
        (window, later_window),
    ) == ((), ())


def test_sparse_locator_event_executes_on_next_complete_minute(
    monkeypatch,
) -> None:
    from chanlun.decision_support.trading_system.backtest import fixed_year

    setup = confirmed_point(
        "3buy",
        anchor=10.0,
        stop=9.8,
        center_zg=9.9,
    )
    locator = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=5,
    )
    one_closes = tuple(
        setup.available_at + timedelta(minutes=value) for value in range(1, 8)
    )
    observed_times = sparse_evaluation_times(
        five_points=(setup,),
        one_points=(locator,),
        thirty_closes=(),
        one_closes=one_closes,
        effective_start=setup.available_at,
        requested_end=one_closes[-1],
    )
    assert observed_times == (one_closes[0], locator.available_at)

    def evaluation(observed_at: datetime) -> SparseEvaluationFact:
        return SparseEvaluationFact(
            observed_at,
            "neutral",
            minute_bar(
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
            ),
        )

    sector = replace(eligible_sector(), regime="supportive")
    facts = SymbolResearchFacts(
        schema=FACT_SCHEMA,
        algorithm_revision="sha256:" + "a" * 64,
        source_revision="sha256:" + "b" * 64,
        code=setup.code,
        sector_id=sector.sector_id,
        requested_start=setup.available_at.date(),
        requested_end=setup.available_at.date(),
        effective_start=setup.available_at.date(),
        row_counts=(("d", 1), ("30m", 1), ("5m", 1), ("1m", len(one_closes))),
        daily_points=(),
        thirty_points=(),
        five_points=(setup,),
        one_points=(locator,),
        evaluations=tuple(evaluation(value) for value in observed_times),
        five_minute_warmup=(
            FiveMinuteWarmupFact(
                observed_at=locator.available_at,
                source_closed_at=locator.available_at,
                converged=True,
                full_bar_count=960,
                suffix_bar_count=640,
                reason_code="WARMUP_TAIL_STABLE",
                production_five_points=(setup,),
                production_one_points=(locator,),
                one_minute_bar_count=960,
            ),
        ),
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
        assessments=tuple((value, sector) for value in observed_times),
    )
    fill_at = locator.available_at + timedelta(minutes=1)
    fill_frame = pd.DataFrame(
        {
            "code": [setup.code],
            "date": [fill_at],
            "open": [10.0],
            "high": [10.1],
            "low": [9.95],
            "close": [10.05],
            "volume": [1_000_000.0],
        }
    )
    sources = iter(
        (
            fixed_year._ActiveMinuteSource(
                frame=fill_frame,
                dates=(fill_at,),
                previous_by_session={fill_at.date(): Decimal("10")},
                index=0,
            ),
            fixed_year._ActiveMinuteSource(
                frame=fill_frame.iloc[0:0],
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
        minute_timeline=(*observed_times, fill_at),
    )

    assert len(run.fills) == 1
    assert run.fills[0].filled_at == fill_at
    assert len(run.open_positions) == 1


def test_superseding_timestamp_starts_the_new_five_minute_setup() -> None:
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

    assert observed == (
        first.available_at + timedelta(minutes=1),
        trigger.available_at,
        first.available_at + timedelta(minutes=30),
        second.available_at,
    )


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
    sector = replace(eligible_sector(), regime="supportive")
    facts = SymbolResearchFacts(
        schema=FACT_SCHEMA,
        algorithm_revision="sha256:" + "a" * 64,
        source_revision="sha256:" + "b" * 64,
        code="SZ.000001",
        sector_id=sector.sector_id,
        requested_start=observed_at.date(),
        requested_end=observed_at.date(),
        effective_start=observed_at.date(),
        row_counts=(("d", 1), ("30m", 1), ("5m", 1), ("1m", 1)),
        daily_points=(),
        thirty_points=(),
        five_points=(setup,),
        one_points=(trigger,),
        evaluations=(evaluation,),
        five_minute_warmup=(
            FiveMinuteWarmupFact(
                observed_at=observed_at,
                source_closed_at=observed_at,
                converged=True,
                full_bar_count=960,
                suffix_bar_count=640,
                reason_code="WARMUP_TAIL_STABLE",
                production_five_points=(setup,),
                production_one_points=(trigger,),
                one_minute_bar_count=960,
            ),
        ),
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


def test_sparse_portfolio_neutral_sector_cannot_be_promoted_by_research(
    monkeypatch,
) -> None:
    """正式研究不能把中性板块静态回填成历史触发。"""

    from chanlun.decision_support.trading_system.backtest import fixed_year

    setup = confirmed_point("3buy", anchor=10.0, stop=9.8, center_zg=9.9)
    trigger = confirmed_point(
        "1buy",
        frequency="1m",
        anchor=9.9,
        minutes_after=5,
    )
    observed_at = trigger.available_at
    evaluation = SparseEvaluationFact(
        observed_at,
        "neutral",
        minute_bar(
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
        ),
    )
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
        row_counts=(("d", 1), ("30m", 1), ("5m", 1), ("1m", 1)),
        daily_points=(),
        thirty_points=(),
        five_points=(setup,),
        one_points=(trigger,),
        evaluations=(evaluation,),
        five_minute_warmup=(
            FiveMinuteWarmupFact(
                observed_at=observed_at,
                source_closed_at=observed_at,
                converged=True,
                full_bar_count=960,
                suffix_bar_count=640,
                reason_code="WARMUP_TAIL_STABLE",
                production_five_points=(setup,),
                production_one_points=(trigger,),
                one_minute_bar_count=960,
            ),
        ),
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
    monkeypatch.setattr(
        fixed_year,
        "_active_minute_source",
        lambda *_args, **_kwargs: fixed_year._ActiveMinuteSource(
            frame=pd.DataFrame(),
            dates=(),
            previous_by_session={},
            index=0,
        ),
    )

    run = run_sparse_portfolio(
        (facts,),
        {sector.sector_id: sector_facts},
        initial_cash=Decimal("1000000"),
        minute_timeline=(observed_at,),
        selection_research_by_code={
            facts.code: (valid_selection_research(),),
        },
    )

    assert run.fills == ()
    assert run.open_positions == ()


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
        price_basis_revision="test-raw",
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
        "_causal_confirmed_structure_events",
        lambda _code, _frequency, _frame, **_kwargs: SimpleNamespace(
            points=(setup,),
            point_visibility=(
                PointVisibilityInterval(
                    setup.point_id,
                    setup.available_at,
                ),
            ),
        ),
    )
    monkeypatch.setattr(
        fixed_year,
        "_causal_confirmed_points",
        lambda *_args, **_kwargs: (),
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


def test_build_symbol_facts_reads_one_shared_local_five_minute_snapshot(
    monkeypatch,
) -> None:
    session = datetime(2026, 7, 24, tzinfo=CN)
    closes = (
        *(
            session.replace(hour=9, minute=35) + timedelta(minutes=5 * index)
            for index in range(24)
        ),
        *(
            session.replace(hour=13, minute=5) + timedelta(minutes=5 * index)
            for index in range(24)
        ),
    )
    raw = pd.DataFrame(
        {
            "time": [int(value.timestamp() * 1000) for value in closes],
            "open": [10.0] * len(closes),
            "high": [10.1] * len(closes),
            "low": [9.9] * len(closes),
            "close": [10.0] * len(closes),
            "volume": [1000.0] * len(closes),
            "amount": [10000.0] * len(closes),
        }
    )
    audit = QMTLocalKlineAudit(
        code="SZ.000001",
        frequency="5m",
        source_path="fixture.DAT",
        source_sha256="sha256:" + "b" * 64,
        source_record_count=len(raw),
        selected_record_count=len(raw),
        first_at=closes[0],
        last_at=closes[-1],
        source_first_at=closes[0],
        source_last_at=closes[-1],
    )
    calls = 0

    def read_once(**_kwargs):
        nonlocal calls
        calls += 1
        return raw.copy(), audit

    monkeypatch.setattr(fixed_year, "resolve_qmt_local_data_dir", lambda: object())
    monkeypatch.setattr(fixed_year, "read_qmt_local_kline", read_once)
    monkeypatch.setattr(
        fixed_year,
        "_causal_confirmed_points",
        lambda *_args, **_kwargs: (),
    )

    facts = fixed_year.build_symbol_facts(
        code="SZ.000001",
        sector_id="qmt-sw1:S48",
        warmup_start=session.date(),
        requested_start=session.date(),
        effective_start=session.date(),
        requested_end=session.date(),
        algorithm_revision="sha256:" + "a" * 64,
    )

    assert calls == 1
    assert dict(facts.row_counts) == {"d": 0, "30m": 8, "5m": 48, "1m": 0}


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
    monkeypatch.setattr(fixed_year, "resolve_qmt_local_data_dir", lambda: None)
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
    monkeypatch.setattr(fixed_year, "resolve_qmt_local_data_dir", lambda: None)
    frame = load_qmt_daily_frame(
        "SZ.000001",
        start_at=session,
        end_at=session.replace(hour=15),
    )

    assert frame.iloc[0]["date"] == session.replace(hour=15)
    assert frame.attrs["price_basis_provider"] == "qmt"
    assert frame.attrs["price_basis_adjustment"] == "causal-forward-ex-date"
    assert str(frame.attrs["price_basis_revision"]).startswith("sha256:")


def test_qmt_native_daily_rpc_keeps_only_requested_pre_start_history(
    monkeypatch,
) -> None:
    native = "000001.SZ"
    anchor = datetime(2026, 7, 24, 0, 0, tzinfo=CN)
    sessions = tuple(anchor - timedelta(days=offset) for offset in (4, 3, 2, 1, 0))

    class FakeXtdata:
        enable_hello = True
        starts: list[str] = []

        @classmethod
        def get_market_data(cls, **kwargs):
            cls.starts.append(kwargs["start_time"])
            values = {
                "time": [int(value.timestamp() * 1000) for value in sessions],
                "open": [10.0] * len(sessions),
                "high": [10.1] * len(sessions),
                "low": [9.9] * len(sessions),
                "close": [10.0] * len(sessions),
                "volume": [1000.0] * len(sessions),
            }
            return {
                field: pd.DataFrame([field_values], index=[native])
                for field, field_values in values.items()
            }

    monkeypatch.setitem(
        __import__("sys").modules,
        "xtquant",
        SimpleNamespace(xtdata=FakeXtdata),
    )
    monkeypatch.setattr(fixed_year, "resolve_qmt_local_data_dir", lambda: None)

    frame = load_qmt_daily_frame(
        "SZ.000001",
        start_at=anchor,
        end_at=anchor.replace(hour=15),
        history_bars_before_start=2,
    )

    assert FakeXtdata.starts == [""]
    assert frame["date"].tolist() == [
        sessions[-3].replace(hour=15),
        sessions[-2].replace(hour=15),
        sessions[-1].replace(hour=15),
    ]
