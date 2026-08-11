from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
    QmtNativeDailyCalendarCoverageEvidence,
    QmtNativeDailyReconciliationError,
    build_qmt_native_daily_bridge,
)
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    qmt_higher_timeframe_inputs,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    build_qmt_same_base_stream_frames,
)
from tests.trading_system.test_qmt_same_base_stream import _native_session


CN = ZoneInfo("Asia/Shanghai")


def _same_base(sessions: tuple[date, ...]):
    frames = tuple(
        _native_session(session, base=10.0 + index)
        for index, session in enumerate(sessions)
    )
    minute = pd.concat(frames, ignore_index=True)
    minute.attrs = dict(frames[0].attrs)
    return build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=minute,
        decision_time=datetime.combine(sessions[-1], datetime.max.time(), tzinfo=CN),
        expected_sessions=sessions,
    )


def _native_daily(same, *, older: int = 3) -> pd.DataFrame:
    first = same.daily["date"].iloc[0]
    older_rows = []
    older_sessions = pd.bdate_range(
        end=first - pd.Timedelta(days=1),
        periods=older,
        tz=first.tz,
    )
    for offset, close in enumerate(older_sessions, start=1):
        price = 8.0 + offset / 10
        older_rows.append(
            {
                "date": close.normalize(),
                "open": price,
                "high": price + 0.2,
                "low": price - 0.2,
                "close": price + 0.1,
                "volume": 1000.0,
            }
        )
    overlap = same.daily.copy()
    # Exercise the local-cache midnight convention.
    overlap["date"] = overlap["date"].dt.normalize()
    result = pd.concat((pd.DataFrame(older_rows), overlap), ignore_index=True)
    result.attrs = dict(same.daily.attrs)
    return result


def _calendar(native: pd.DataFrame) -> tuple[date, ...]:
    first = pd.Timestamp(native["date"].min()).date()
    last = pd.Timestamp(native["date"].max()).date()
    return tuple(value.date() for value in pd.bdate_range(first, last))


def test_bridge_uses_native_daily_only_before_the_reconciled_one_minute_tail() -> None:
    sessions = (date(2026, 7, 23), date(2026, 7, 24))
    same = _same_base(sessions)
    native = _native_daily(same)

    result = build_qmt_native_daily_bridge(
        symbol="SH.600000",
        native_daily_frame=native,
        same_base=same,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        trading_sessions=_calendar(native),
    )

    assert len(result.daily) == 5
    assert tuple(result.daily["date"].dt.time)[-2:] == (
        datetime.min.time().replace(hour=15),
        datetime.min.time().replace(hour=15),
    )
    assert tuple(result.daily["close"])[-2:] == tuple(same.daily["close"])
    assert result.daily.attrs["source_base_stream_revision"] == (
        result.thirty_minute.attrs["source_base_stream_revision"]
    )
    assert result.daily.attrs["source_base_frequency"] == (
        QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY
    )
    assert result.evidence.overlap_session_count == 2
    assert result.evidence.document()["all_overlap_ohlcv_equal"] is True
    coverage = result.calendar_coverage_evidence
    assert coverage.status == "EXACT"
    assert coverage.document()["entry_disposition"] == "NO_CALENDAR_BLOCKER"
    assert QmtNativeDailyCalendarCoverageEvidence.from_document(
        coverage.document()
    ) == coverage

    inputs = qmt_higher_timeframe_inputs(
        symbol="SH.600000",
        daily_frame=result.daily,
        thirty_minute_frame=result.thirty_minute,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        required_base_frequency=QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
        native_daily_reconciliation_evidence=result.evidence,
    )
    assert inputs.same_base_stream is True
    assert inputs.native_daily_reconciliation_evidence == result.evidence

    unresolved = qmt_higher_timeframe_inputs(
        symbol="SH.600000",
        daily_frame=result.daily,
        thirty_minute_frame=result.thirty_minute,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        required_base_frequency=QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
    )
    assert unresolved.same_base_stream is False
    assert {value.code for value in unresolved.blockers} == {
        "QMT_NATIVE_DAILY_RECONCILIATION_EVIDENCE_UNRESOLVED"
    }


def test_bridge_rejects_a_rehashed_or_relabelled_daily_price_basis() -> None:
    same = _same_base((date(2026, 7, 23), date(2026, 7, 24)))
    native = _native_daily(same)
    native.attrs["price_basis_revision"] = "sha256:" + "f" * 64

    with pytest.raises(
        QmtNativeDailyReconciliationError,
        match="QMT_NATIVE_DAILY_PRICE_BASIS_MISMATCH",
    ):
        build_qmt_native_daily_bridge(
            symbol="SH.600000",
            native_daily_frame=native,
            same_base=same,
            decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            trading_sessions=_calendar(native),
        )


def test_research_tolerance_accepts_exactly_one_price_quantum_but_not_two() -> None:
    same = _same_base((date(2026, 7, 23), date(2026, 7, 24)))
    native = _native_daily(same)
    native.loc[len(native) - 1, "close"] -= 0.01

    accepted = build_qmt_native_daily_bridge(
        symbol="SH.600000",
        native_daily_frame=native,
        same_base=same,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        trading_sessions=_calendar(native),
        max_price_difference_quanta=1,
    )
    document = accepted.evidence.document()
    assert document["all_overlap_ohlcv_equal"] is False
    assert document["all_overlap_ohlcv_within_declared_tolerance"] is True
    assert document["price_difference_count"] == 1
    assert document["max_observed_price_difference_quanta"] == 1

    native.loc[len(native) - 1, "close"] -= 0.01
    with pytest.raises(
        QmtNativeDailyReconciliationError,
        match="QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH",
    ):
        build_qmt_native_daily_bridge(
            symbol="SH.600000",
            native_daily_frame=native,
            same_base=same,
            decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            trading_sessions=_calendar(native),
            max_price_difference_quanta=1,
        )


@pytest.mark.parametrize("field", ("open", "high", "low", "close", "volume"))
def test_bridge_rejects_every_overlapping_ohlcv_mismatch(field: str) -> None:
    same = _same_base((date(2026, 7, 23), date(2026, 7, 24)))
    native = _native_daily(same)
    delta = {
        "open": 0.01,
        "high": 0.01,
        "low": -0.01,
        "close": -0.01,
        "volume": 1.0,
    }[field]
    native.loc[len(native) - 1, field] += delta

    with pytest.raises(
        QmtNativeDailyReconciliationError,
        match="QMT_NATIVE_DAILY_OHLCV_RECONCILIATION_MISMATCH",
    ):
        build_qmt_native_daily_bridge(
            symbol="SH.600000",
            native_daily_frame=native,
            same_base=same,
            decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            trading_sessions=_calendar(native),
        )


def test_bridge_rejects_missing_overlap_session_and_native_future_tail() -> None:
    sessions = (date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24))
    same = _same_base(sessions)
    native = _native_daily(same)
    missing = native[native["date"].dt.date != sessions[1]].copy()
    missing.attrs = dict(native.attrs)

    with pytest.raises(
        QmtNativeDailyReconciliationError,
        match="QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",
    ) as error:
        build_qmt_native_daily_bridge(
            symbol="SH.600000",
            native_daily_frame=missing,
            same_base=same,
            decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            trading_sessions=_calendar(native),
        )
    coverage = error.value.calendar_coverage_evidence
    assert coverage is not None
    assert coverage.status == "UNEXPLAINED_CALENDAR_SESSION_MISSING"
    assert coverage.unexplained_calendar_only_sessions == (sessions[1],)
    document = coverage.document()
    assert document["missing_session_interpretation"] == (
        "UNEXPLAINED_NEVER_INFERRED_AS_SUSPENSION"
    )
    assert document["point_in_time_status_evidence_present"] is False
    assert document["entry_disposition"] == "FAIL_CLOSED"
    assert QmtNativeDailyCalendarCoverageEvidence.from_document(document) == (
        coverage
    )
    bool_count = dict(document)
    bool_count["native_daily_bar_count"] = True
    with pytest.raises(
        ValueError,
        match="native-daily calendar coverage document is malformed",
    ):
        QmtNativeDailyCalendarCoverageEvidence.from_document(bool_count)

    future = native.copy()
    future.attrs = dict(native.attrs)
    row = dict(future.iloc[-1])
    row["date"] = pd.Timestamp(row["date"]) + pd.Timedelta(days=3)
    future = pd.concat((future, pd.DataFrame((row,))), ignore_index=True)
    future.attrs = dict(native.attrs)
    with pytest.raises(
        QmtNativeDailyReconciliationError,
        match="QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE",
    ):
        build_qmt_native_daily_bridge(
            symbol="SH.600000",
            native_daily_frame=future,
            same_base=same,
            decision_time=datetime(2026, 7, 27, 15, 1, tzinfo=CN),
            trading_sessions=_calendar(future),
        )


def test_unfinished_native_daily_bar_is_not_visible_before_its_close() -> None:
    same = _same_base((date(2026, 7, 23), date(2026, 7, 24)))
    native = _native_daily(same)
    tomorrow = dict(native.iloc[-1])
    tomorrow["date"] = pd.Timestamp(tomorrow["date"]) + pd.Timedelta(days=3)
    native = pd.concat((native, pd.DataFrame((tomorrow,))), ignore_index=True)
    native.attrs = dict(same.daily.attrs)

    result = build_qmt_native_daily_bridge(
        symbol="SH.600000",
        native_daily_frame=native,
        same_base=same,
        decision_time=datetime(2026, 7, 27, 14, 59, tzinfo=CN),
        trading_sessions=_calendar(native),
    )

    assert result.evidence.native_daily_bar_count == 5
    assert result.daily["date"].max().date() == date(2026, 7, 24)


def test_bridge_rejects_weekend_rows_and_missing_calendar_sessions() -> None:
    same = _same_base((date(2026, 7, 23), date(2026, 7, 24)))
    native = _native_daily(same)
    calendar = _calendar(native)

    weekend = native.copy()
    weekend.attrs = dict(native.attrs)
    row = dict(weekend.iloc[0])
    row["date"] = pd.Timestamp(row["date"]) - pd.Timedelta(days=1)
    weekend = pd.concat((pd.DataFrame((row,)), weekend), ignore_index=True)
    weekend.attrs = dict(native.attrs)
    with pytest.raises(
        QmtNativeDailyReconciliationError,
        match="QMT_NATIVE_DAILY_TRADING_CALENDAR_COVERAGE_INSUFFICIENT",
    ):
        build_qmt_native_daily_bridge(
            symbol="SH.600000",
            native_daily_frame=weekend,
            same_base=same,
            decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            trading_sessions=calendar,
        )

    missing = native[native["date"].dt.date != date(2026, 7, 21)].copy()
    missing.attrs = dict(native.attrs)
    with pytest.raises(
        QmtNativeDailyReconciliationError,
        match="QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",
    ):
        build_qmt_native_daily_bridge(
            symbol="SH.600000",
            native_daily_frame=missing,
            same_base=same,
            decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            trading_sessions=calendar,
        )


def test_future_calendar_append_does_not_change_a_historical_bridge_identity() -> None:
    same = _same_base((date(2026, 7, 23), date(2026, 7, 24)))
    native = _native_daily(same)
    visible = _calendar(native)
    future = visible + (date(2026, 7, 27), date(2026, 7, 28))
    decision = datetime(2026, 7, 24, 15, 1, tzinfo=CN)

    short = build_qmt_native_daily_bridge(
        symbol="SH.600000",
        native_daily_frame=native,
        same_base=same,
        decision_time=decision,
        trading_sessions=visible,
    )
    extended = build_qmt_native_daily_bridge(
        symbol="SH.600000",
        native_daily_frame=native,
        same_base=same,
        decision_time=decision,
        trading_sessions=future,
    )

    assert short.evidence == extended.evidence
    pd.testing.assert_frame_equal(short.daily, extended.daily)
