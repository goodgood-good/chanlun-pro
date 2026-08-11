from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    qmt_higher_timeframe_inputs,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    build_qmt_same_base_stream_frames,
    normalize_qmt_opening_events_for_completed_minutes,
)


CN = ZoneInfo("Asia/Shanghai")
PRICE_BASIS = "sha256:" + "a" * 64


def _native_session(session: date, *, base: float = 10.0) -> pd.DataFrame:
    opening = datetime.combine(session, time(9, 30), tzinfo=CN)
    morning = datetime.combine(session, time(9, 31), tzinfo=CN)
    afternoon = datetime.combine(session, time(13, 1), tzinfo=CN)
    times = (
        opening,
        *(morning + timedelta(minutes=index) for index in range(120)),
        *(afternoon + timedelta(minutes=index) for index in range(120)),
    )
    frame = pd.DataFrame(
        {
            "date": times,
            "open": [base + index / 1000 for index in range(241)],
            "high": [base + 1 + index / 1000 for index in range(241)],
            "low": [base - 1 + index / 1000 for index in range(241)],
            "close": [base + 0.5 + index / 1000 for index in range(241)],
            "volume": [index + 1 for index in range(241)],
        }
    )
    frame.attrs.update(
        {
            "structure_price_quantum": "0.01",
            "price_basis_provider": "qmt",
            "price_basis_adjustment": "front",
            "price_basis_revision": PRICE_BASIS,
        }
    )
    return frame


def _two_sessions() -> pd.DataFrame:
    first = _native_session(date(2026, 7, 23))
    second = _native_session(date(2026, 7, 24), base=20.0)
    frame = pd.concat((first, second), ignore_index=True)
    frame.attrs = dict(first.attrs)
    return frame


def test_multi_session_opening_normalizer_preserves_attrs_and_volume() -> None:
    source = _two_sessions()

    normalized = normalize_qmt_opening_events_for_completed_minutes(source)

    assert len(normalized) == 480
    assert tuple(normalized.groupby(normalized["date"].dt.date).size()) == (240, 240)
    assert tuple(
        rows.iloc[0]["date"].time()
        for _, rows in normalized.groupby(normalized["date"].dt.date)
    ) == (time(9, 31), time(9, 31))
    assert normalized["volume"].sum() == source["volume"].sum()
    assert normalized.attrs == source.attrs


def test_opening_only_prefix_emits_no_fake_locator_bar_and_keeps_attrs() -> None:
    source = _native_session(date(2026, 7, 24)).iloc[:1].copy()
    source.attrs.update(_native_session(date(2026, 7, 24)).attrs)

    normalized = normalize_qmt_opening_events_for_completed_minutes(source)

    assert normalized.empty
    assert tuple(normalized.columns) == ("date", "open", "high", "low", "close", "volume")
    assert normalized.attrs == source.attrs


def test_zero_volume_opening_placeholder_does_not_override_first_trade_bar() -> None:
    source = _native_session(date(2026, 7, 24))
    source.loc[0, ["open", "high", "low", "close", "volume"]] = (
        50.0,
        60.0,
        1.0,
        50.0,
        0.0,
    )

    normalized = normalize_qmt_opening_events_for_completed_minutes(source)

    first = normalized.iloc[0]
    qmt_first_trade_bar = source.iloc[1]
    for field in ("open", "high", "low", "close", "volume"):
        assert first[field] == qmt_first_trade_bar[field]


def test_sparse_live_session_relabels_traded_opening_without_filling_gaps() -> None:
    source = _native_session(date(2026, 7, 24)).drop(index=[1, 2]).reset_index(
        drop=True
    )

    normalized = normalize_qmt_opening_events_for_completed_minutes(source)

    assert len(normalized) == len(source)
    assert tuple(normalized.iloc[:2]["date"].dt.time) == (
        time(9, 31),
        time(9, 33),
    )
    for field in ("open", "high", "low", "close", "volume"):
        assert normalized.iloc[0][field] == source.iloc[0][field]
    assert normalized["volume"].sum() == source["volume"].sum()
    assert normalized.attrs == source.attrs


def test_sparse_live_session_drops_zero_volume_opening_placeholder() -> None:
    source = _native_session(date(2026, 7, 24)).drop(index=[1, 2]).reset_index(
        drop=True
    )
    source.loc[0, "volume"] = 0.0

    normalized = normalize_qmt_opening_events_for_completed_minutes(source)

    assert len(normalized) == len(source) - 1
    assert normalized.iloc[0]["date"].time() == time(9, 33)
    pd.testing.assert_frame_equal(
        normalized.reset_index(drop=True),
        source.iloc[1:].loc[:, normalized.columns].reset_index(drop=True),
    )
    assert normalized.attrs == source.attrs


def test_multi_session_normalizer_rejects_bad_opening_volume_and_order() -> None:
    source = _two_sessions()
    negative_opening = source.copy()
    negative_opening.loc[0, "volume"] = -1

    with pytest.raises(ValueError, match="invalid OHLCV"):
        normalize_qmt_opening_events_for_completed_minutes(negative_opening)

    reversed_rows = source.iloc[::-1].reset_index(drop=True)
    reversed_rows.attrs = dict(source.attrs)
    with pytest.raises(ValueError, match="unique and chronological"):
        normalize_qmt_opening_events_for_completed_minutes(reversed_rows)

    duplicate = pd.concat((source.iloc[:2], source.iloc[1:]), ignore_index=True)
    duplicate.attrs = dict(source.attrs)
    with pytest.raises(ValueError, match="unique and chronological"):
        normalize_qmt_opening_events_for_completed_minutes(duplicate)


def test_intraday_aggregation_ignores_zero_volume_placeholder_prices() -> None:
    session = date(2026, 7, 24)
    source = _native_session(session)
    # 09:36 is the first row in the second completed 5m bucket.  QMT can emit
    # a carried, zero-volume minute there; it is not a traded OHLC fact.
    source.loc[6, ["open", "high", "low", "close", "volume"]] = (
        50.0,
        60.0,
        1.0,
        50.0,
        0.0,
    )

    result = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        expected_sessions=(session,),
    )

    second = result.five_minute.iloc[1]
    traded = source.iloc[7:11]
    assert second["date"].time() == time(9, 40)
    assert second["open"] == traded.iloc[0]["open"]
    assert second["high"] == traded["high"].max()
    assert second["low"] == traded["low"].min()
    assert second["close"] == traded.iloc[-1]["close"]
    assert second["volume"] == traded["volume"].sum()


def test_all_zero_volume_bucket_retains_a_deterministic_carried_bar() -> None:
    session = date(2026, 7, 24)
    source = _native_session(session)
    source.loc[6:10, "volume"] = 0.0

    result = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        expected_sessions=(session,),
    )

    second = result.five_minute.iloc[1]
    carried = source.iloc[6:11]
    assert second["open"] == carried.iloc[0]["open"]
    assert second["high"] == carried["high"].max()
    assert second["low"] == carried["low"].min()
    assert second["close"] == carried.iloc[-1]["close"]
    assert second["volume"] == 0.0


def test_complete_qmt_session_derives_all_periods_from_one_hash() -> None:
    session = date(2026, 7, 24)
    source = _native_session(session)
    result = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        expected_sessions=(session,),
    )

    assert result.grade == "FULL_SYSTEM_ELIGIBLE"
    assert result.blockers == ()
    assert len(result.one_minute) == 240
    assert len(result.five_minute) == 48
    assert len(result.thirty_minute) == 8
    assert len(result.daily) == 1
    assert tuple(result.thirty_minute["date"].dt.time) == (
        time(10, 0),
        time(10, 30),
        time(11, 0),
        time(11, 30),
        time(13, 30),
        time(14, 0),
        time(14, 30),
        time(15, 0),
    )
    # 09:30 auction/open event is part of the 09:31 completed bar.
    first = result.one_minute.iloc[0]
    assert first["date"].time() == time(9, 31)
    assert first["open"] == source.iloc[0]["open"]
    assert first["close"] == source.iloc[1]["close"]
    assert first["volume"] == source.iloc[0]["volume"] + source.iloc[1]["volume"]
    assert result.daily.iloc[0]["volume"] == source["volume"].sum()
    revisions = {
        frame.attrs["source_base_stream_revision"]
        for frame in (
            result.one_minute,
            result.five_minute,
            result.thirty_minute,
            result.daily,
        )
    }
    assert revisions == {result.source_base_stream_revision}

    inputs = qmt_higher_timeframe_inputs(
        symbol="SH.600000",
        daily_frame=result.daily,
        thirty_minute_frame=result.thirty_minute,
        decision_time=result.observed_at,
    )
    assert inputs.same_base_stream is True
    assert inputs.price_basis_revision == PRICE_BASIS


def test_current_partial_session_emits_only_completed_intraday_buckets() -> None:
    session = date(2026, 7, 24)
    source = _native_session(session)
    result = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 10, 14, tzinfo=CN),
        expected_sessions=(session,),
    )

    assert result.partial_session == session
    assert result.complete_sessions == ()
    assert tuple(result.thirty_minute["date"].dt.time) == (time(10, 0),)
    assert result.five_minute.iloc[-1]["date"].time() == time(10, 10)
    assert result.one_minute.iloc[-1]["date"].time() == time(10, 14)
    assert result.daily.empty


def test_incomplete_historical_session_fails_closed() -> None:
    session = date(2026, 7, 23)
    source = _native_session(session).iloc[20:].copy()
    source.attrs.update(
        {
            "price_basis_provider": "qmt",
            "price_basis_adjustment": "front",
            "price_basis_revision": PRICE_BASIS,
        }
    )
    result = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 10, 0, tzinfo=CN),
        expected_sessions=(session,),
    )

    assert result.grade == "UNRESOLVED"
    assert result.one_minute.empty
    assert "QMT_ONE_MINUTE_SESSION_GRID_INVALID" in {
        value.code for value in result.blockers
    }


def test_leading_finite_cache_slice_before_evaluation_is_excluded_and_bound() -> None:
    first_session = date(2026, 7, 23)
    evaluation_start = date(2026, 7, 24)
    partial = _native_session(first_session).iloc[-5:].copy()
    complete = _native_session(evaluation_start, base=20.0)
    source = pd.concat((partial, complete), ignore_index=True)
    source.attrs = dict(complete.attrs)

    result = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        expected_sessions=(first_session, evaluation_start),
        evaluation_not_before=evaluation_start,
    )
    clean = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=complete,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        expected_sessions=(evaluation_start,),
        evaluation_not_before=evaluation_start,
    )

    assert result.grade == "RESEARCH_ONLY"
    assert result.blockers == ()
    assert result.session_issues == ()
    assert result.complete_sessions == (evaluation_start,)
    assert len(result.one_minute) == 240
    assert len(result.source_boundary_exclusions) == 1
    evidence = result.source_boundary_exclusions[0].document()
    assert evidence["session"] == first_session.isoformat()
    assert evidence["observed_rows"] == 5
    assert evidence["used_as_completed_intraday_session"] is False
    assert evidence["entry_disposition"] == "EXCLUDED_BEFORE_EVALUATION"
    # The accepted rows match, but the omission remains part of provenance.
    pd.testing.assert_frame_equal(result.one_minute, clean.one_minute)
    assert result.source_base_stream_revision != clean.source_base_stream_revision


def test_historical_aggregates_are_prefix_invariant() -> None:
    source = _two_sessions()
    first = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 23, 15, 1, tzinfo=CN),
        expected_sessions=(date(2026, 7, 23),),
    )
    later = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        expected_sessions=(date(2026, 7, 23), date(2026, 7, 24)),
    )

    for frequency in ("five_minute", "thirty_minute", "daily"):
        expected = getattr(first, frequency).reset_index(drop=True)
        actual = getattr(later, frequency).iloc[: len(expected)].reset_index(drop=True)
        pd.testing.assert_frame_equal(actual, expected)


def test_missing_calendar_contract_is_explicitly_research_only() -> None:
    source = _native_session(date(2026, 7, 24))
    result = build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=source,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
    )

    assert result.grade == "RESEARCH_ONLY"
    assert "QMT_ONE_MINUTE_TRADING_CALENDAR_UNRESOLVED" in {
        value.code for value in result.blockers
    }
