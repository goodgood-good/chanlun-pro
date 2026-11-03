from datetime import datetime
from unittest.mock import Mock

import pandas as pd
import pytest

from chanlun.screening import runner, closing_bar
from chanlun.screening.rules import CN, trading_context, validate_frame


@pytest.fixture(autouse=True)
def fixed_source_clock(monkeypatch):
    monkeypatch.setattr(
        closing_bar, "_now", lambda: datetime(2026, 10, 14, tzinfo=CN).timestamp()
    )


def inputs(frequency="1m"):
    context = trading_context(datetime(2026, 10, 13, 15, tzinfo=CN), frequency, 1, 1)
    timestamps = context["expected_closes"][-max(200, 1200 // int(frequency[:-1])) :]
    frame = pd.DataFrame(
        {
            "code": "SH.688139",
            "date": pd.to_datetime(timestamps, unit="s", utc=True).tz_convert(CN),
            "open": 34.01,
            "high": 34.01,
            "low": 34.01,
            "close": 34.01,
            "volume": 10.0,
        }
    )
    frame.attrs.update(
        price_basis_provider="qmt",
        price_basis_revision="fixed",
        structure_price_quantum="0.01",
    )
    tail = frame.loc[frame.date.dt.date == datetime(2026, 10, 13).date()].copy()
    return frame, tail, context


@closing_bar.completed_input_scope
def verify(exchange, frame, context, *, cache_root=None):
    return closing_bar.verify_completed_input(
        exchange,
        frame,
        "SH.688139",
        context["frequency"],
        context,
        start="20260814",
        end=datetime.fromtimestamp(context["cutoff"], CN).strftime("%Y-%m-%d %H:%M:%S"),
    )


@pytest.mark.parametrize(
    "frequency,old_volume", [("1m", 0.0), ("5m", 40.0), ("30m", 154.0)]
)
def test_zero_and_positive_volume_tails_need_native_completion(
    frequency, old_volume, tmp_path
):
    frame, tail, context = inputs(frequency)
    frame.loc[frame.index[-1], "volume"] = old_volume
    assert validate_frame(frame, context) == []
    tail.loc[tail.index[-1], ["high", "close", "volume"]] = [34.45, 34.45, 1202.0]
    exchange = Mock()
    exchange.klines.return_value = tail
    result, checked = verify(exchange, frame, context, cache_root=tmp_path)
    assert checked and result.close.iloc[-1] == 34.45 and result.volume.iloc[-1] == 1202
    assert len(result) == len(frame)
    pd.testing.assert_frame_equal(result.iloc[: -len(tail)], frame.iloc[: -len(tail)])
    exchange.klines.assert_called_once_with(
        "SH.688139",
        frequency,
        start_date="20261013",
        end_date="2026-10-13 15:00:00",
        args={"exact_end": True, "local_only": True, "download_start_date": "20261013"},
    )
    again, checked = verify(exchange, result.copy(), context, cache_root=tmp_path)
    assert not checked and exchange.klines.call_count == 1
    assert again.equals(result)


def test_intraday_positive_volume_cannot_authorize_a_preexisting_partial_bucket(
    tmp_path,
):
    frame, tail, context = inputs("5m")
    frame = frame.iloc[:-2].copy()
    tail = tail.iloc[:-2].copy()
    context = {**context, "cutoff": int(frame.date.iloc[-1].timestamp())}
    exchange = Mock()
    exchange.klines.return_value = tail
    _, checked = verify(exchange, frame, context, cache_root=tmp_path)
    assert checked
    assert exchange.klines.call_args.kwargs["end_date"] == "2026-10-13 14:50:00"


@pytest.mark.parametrize("change", ["price", "basis", "receipt"])
def test_changed_input_or_corrupt_receipt_cannot_reuse_completion(change, tmp_path):
    frame, tail, context = inputs("5m")
    exchange = Mock()
    exchange.klines.return_value = tail
    result, _ = verify(exchange, frame, context, cache_root=tmp_path)
    if change == "price":
        result.loc[result.index[-1], ["high", "close"]] = 35.0
    elif change == "basis":
        result.attrs["price_basis_revision"] = "different"
    else:
        next((tmp_path / "completed_inputs").glob("*.json")).write_text(
            "{}", encoding="utf-8"
        )
    _, checked = verify(exchange, result, context, cache_root=tmp_path)
    assert checked and exchange.klines.call_count >= 2


@pytest.mark.parametrize(
    "failure", ["unavailable", "exception", "truncated", "other_symbol"]
)
def test_failed_refresh_cannot_republish_unverified_cached_input(failure, tmp_path):
    frame, tail, context = inputs()
    exchange = Mock()
    if failure == "exception":
        exchange.klines.side_effect = TimeoutError("bounded provider request")
    else:
        if failure == "other_symbol":
            tail["code"] = "SH.600000"
        exchange.klines.return_value = (
            None
            if failure == "unavailable"
            else tail.iloc[:-1]
            if failure == "truncated"
            else tail
        )
    result, checked = verify(exchange, frame, context, cache_root=tmp_path)
    assert checked and validate_frame(result, context) == ["CLOSING_BAR_UNVERIFIED"]
    assert not list((tmp_path / "completed_inputs").glob("*.json"))
    assert not frame.attrs.get("_screening_closing_refresh_error")


def test_basis_change_reads_the_full_prefix_again_before_combining(tmp_path):
    frame, tail, context = inputs("5m")
    revised = frame.copy()
    tail.attrs["price_basis_revision"] = revised.attrs["price_basis_revision"] = (
        "new-basis"
    )
    exchange = Mock()
    exchange.klines.side_effect = [tail, revised]
    result, checked = verify(exchange, frame, context, cache_root=tmp_path)
    assert checked and result.attrs["price_basis_revision"] == "new-basis"
    assert exchange.klines.call_args.kwargs["start_date"] == "20260814"
    assert exchange.klines.call_args.kwargs["args"]["skip_download"] is True


def test_offline_replay_is_explicit_and_cannot_leave_verification_disabled():
    frame, tail, context = inputs()
    exchange = Mock()
    exchange.klines.return_value = tail
    with closing_bar.input_verification(False):
        assert verify(exchange, frame, context)[1] is False
    exchange.klines.assert_not_called()
    assert verify(exchange, frame, context)[1] is True


def test_fetch_preserves_the_requested_history_and_refreshes_only_the_tail(monkeypatch):
    frame, tail, context = inputs("5m")
    tail.loc[tail.index[-1], ["high", "close", "volume"]] = [34.45, 34.45, 1202.0]
    exchange = Mock()
    exchange.get_start_date_by_frequency.return_value = "20260814"
    exchange.klines.side_effect = [frame, tail]
    monkeypatch.setattr(runner, "_exchange", lambda: exchange)
    monkeypatch.setattr(runner, "_resolve_suspensions", lambda data, *_: data)
    result = runner.fetch_frame("SH.688139", "5m", context)
    assert result.close.iloc[-1] == 34.45 and len(result) == len(frame)
    assert exchange.klines.call_count == 2
    assert exchange.klines.call_args.kwargs["start_date"] == "20261013"
