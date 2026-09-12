"""Real CQ parsing plus uSMART delegation, using captured-shape candles only."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun import config
from chanlun.exchange.exchange_cq import ExchangeChangQiao
from chanlun.exchange.exchange_usmart import ExchangeUSmart


def _source_rows(frequency, *, close="2026-09-04T20:00:00Z"):
    size = {"1m": 1, "5m": 5, "30m": 30}[frequency]
    last = pd.Timestamp(close)
    dates = pd.date_range(last - pd.Timedelta(hours=1), last, freq=f"{size}min")
    return pd.DataFrame({
        "date": dates,
        "open": [100.0 + index for index in range(len(dates))],
        "high": [101.0 + index for index in range(len(dates))],
        "low": [99.0 + index for index in range(len(dates))],
        "close": [100.5 + index for index in range(len(dates))],
        "volume": [1000.0 + index for index in range(len(dates))],
    })


@pytest.fixture
def delegated(monkeypatch):
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "longbridge")
    cls = ExchangeChangQiao.__wrapped__
    cq = cls.__new__(cls)
    cq.executor = ThreadPoolExecutor(2)
    frames = {frequency: _source_rows(frequency) for frequency in ("1m", "5m", "30m")}
    requests = []

    def segment(code, period, _adjust, end, start, priority="interactive"):
        frequency = {"Min_1": "1m", "Min_5": "5m", "Min_30": "30m"}[str(period).split(".")[-1]]
        requests.append((frequency, start, end))
        # Deliberately include a candle opening exactly at the cursor.  The
        # adapter must also remain causal for an inclusive provider endpoint.
        frame = frames[frequency]
        frame = frame.loc[(frame.date >= start) & (frame.date <= end)]
        return [
            SimpleNamespace(timestamp=row.date.to_pydatetime(), open=row.open,
                            high=row.high, low=row.low, close=row.close,
                            volume=row.volume)
            for row in frame.itertuples(index=False)
        ], "complete"

    monkeypatch.setattr(cq, "_fetch_segment_data", segment)
    client = SimpleNamespace(quote=lambda *_a, **_k: pytest.fail("must not mix native uSMART quotes"))
    ex = ExchangeUSmart("us", client=client, history_exchange=cq)
    yield SimpleNamespace(ex=ex, cq=cq, frames=frames, requests=requests)
    cq.executor.shutdown(wait=True)


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_real_delegate_maps_existing_final_candle_to_common_2000_close(delegated, frequency):
    result = delegated.ex.klines(
        "AAPL.US", frequency, start_date="2026-09-04T19:00:00Z",
        end_date="2026-09-04T20:00:00Z",
    )

    assert delegated.cq.kline_time_label == "start"
    assert result.iloc[-1].date == pd.Timestamp("2026-09-04T20:00:00Z")
    source = delegated.frames[frequency].iloc[:-1]
    columns = ["open", "high", "low", "close", "volume"]
    pd.testing.assert_frame_equal(result[columns], source[columns].reset_index(drop=True))
    assert result.attrs["price_basis_provider"] == "longbridge"
    assert result.attrs["price_basis_adjustment"] == "forward"
    assert result.iloc[-1].date.isoformat() == "2026-09-05T04:00:00+08:00"
    assert len(result) == len(source)


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_cutoff_does_not_admit_candle_that_only_opens_at_cutoff(delegated, frequency):
    cutoff = pd.Timestamp("2026-09-04T19:30:00Z")
    result = delegated.ex.klines("AAPL.US", frequency, end_date=cutoff.isoformat())

    assert not result.empty
    assert result.iloc[-1].date == cutoff
    size = {"1m": 1, "5m": 5, "30m": 30}[frequency]
    source = delegated.frames[frequency]
    terminal = source.loc[source.date == cutoff - pd.Timedelta(minutes=size)].iloc[0]
    assert result.iloc[-1].close == terminal.close
    assert (result.date <= cutoff).all()


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_explicit_closed_lower_boundary_retains_its_real_preceding_candle(delegated, frequency):
    result = delegated.ex.klines(
        "AAPL.US", frequency, start_date="2026-09-04T19:30:00Z",
        end_date="2026-09-04T20:00:00Z",
    )

    assert result.iloc[0].date == pd.Timestamp("2026-09-04T19:30:00Z")
    assert result.iloc[-1].date == pd.Timestamp("2026-09-04T20:00:00Z")
    size = {"1m": 1, "5m": 5, "30m": 30}[frequency]
    assert len(result) == 30 // size + 1


@pytest.mark.parametrize("close", ["2026-03-06T21:00:00Z", "2026-03-09T20:00:00Z"])
@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_actual_dst_offset_is_preserved_across_local_midnight(delegated, close, frequency):
    delegated.frames[frequency] = _source_rows(frequency, close=close)
    result = delegated.ex.klines("AAPL.US", frequency, end_date=close)
    assert result.iloc[-1].date == pd.Timestamp(close)
    assert result.iloc[-1].date.tz_convert("US/Eastern").hour == 16
    assert (result.date <= pd.Timestamp(close)).all()


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
def test_half_day_close_keeps_real_last_bar_without_extending_the_session(delegated, frequency):
    close = "2026-07-02T17:00:00Z"
    delegated.frames[frequency] = _source_rows(frequency, close=close)
    result = delegated.ex.klines("AAPL.US", frequency, end_date=close)
    assert result.iloc[-1].date == pd.Timestamp(close)
    assert result.iloc[-1].date.tz_convert("US/Eastern").hour == 13


def test_date_only_end_means_us_market_day_not_shanghai_midnight(delegated):
    result = delegated.ex.klines("AAPL.US", "1m", end_date="2026-09-04")
    assert result.iloc[-1].date >= pd.Timestamp("2026-09-04T20:00:00Z")
    assert max(end for _freq, _start, end in delegated.requests) == pd.Timestamp("2026-09-04T23:59:59.999999-04:00")


def test_end_labelled_delegate_is_not_shifted_twice(delegated, monkeypatch):
    frame = delegated.frames["1m"].copy()
    frame.attrs["price_basis_revision"] = "source-revision"
    monkeypatch.setattr(delegated.cq, "kline_time_label", "end")
    monkeypatch.setattr(delegated.cq, "klines", lambda *_a, **_k: frame)
    result = delegated.ex.klines("AAPL.US", "1m", end_date="2026-09-04T20:00:00Z")
    pd.testing.assert_frame_equal(result, frame)
    assert result.attrs == frame.attrs


def test_download_crossing_minute_close_cannot_admit_a_partial_tail(delegated, monkeypatch):
    observed = [pd.Timestamp("2026-09-04T19:59:59Z")]
    monkeypatch.setattr(pd.Timestamp, "now", lambda **_kwargs: observed[0])

    def download(*_args, **_kwargs):
        # The 19:59 opening candle was read before closing, but other history
        # pages completed after 20:00. It must wait for a later provider read.
        observed[0] = pd.Timestamp("2026-09-04T20:00:02Z")
        return delegated.frames["1m"].copy()

    monkeypatch.setattr(delegated.cq, "klines", download)
    result = delegated.ex.klines("AAPL.US", "1m")
    assert result.iloc[-1].date == pd.Timestamp("2026-09-04T19:59:00Z")


def test_daily_delegate_and_incomplete_empty_marker_are_not_normalized(delegated, monkeypatch):
    frame = delegated.frames["1m"].iloc[:1].copy()
    frame.attrs["price_basis_provider"] = "longbridge"
    monkeypatch.setattr(delegated.cq, "klines", lambda *_a, **_k: frame)
    assert delegated.ex.klines("AAPL.US", "d", end_date="2026-09-04") is frame

    empty = frame.iloc[:0].copy()
    empty.attrs["fetch_incomplete"] = True
    monkeypatch.setattr(delegated.cq, "klines", lambda *_a, **_k: empty)
    assert delegated.ex.klines("AAPL.US", "1m", end_date="2026-09-04") is empty
    assert empty.attrs["fetch_incomplete"] is True


def test_native_usmart_end_label_is_unchanged(monkeypatch):
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "usmart")
    responses = iter([
        [{"latestTime": 20260904160000000, "open": 100.0, "high": 101.0,
          "low": 99.0, "close": 100.5, "volume": 1000}],
        [],
    ])
    client = SimpleNamespace(quote=lambda *_a, **_k: {"list": next(responses)})
    ex = ExchangeUSmart("us", client=client)
    result = ex.klines("AAPL.US", "1m", start_date="2026-09-04", end_date="2026-09-04")
    assert len(result) == 1
    assert result.iloc[0].date == pd.Timestamp("2026-09-04T20:00:00Z")
    assert result.attrs["price_basis_provider"] == "usmart"


def _canonical_head_rows(delegated):
    from chanlun.exchange._lookback import get_lookback_timedelta

    # The Sunday wall-clock window misses the Friday closed-cutoff window's
    # beginning. Include prices there so blindly reusing the first fetch fails.
    cutoff = pd.Timestamp("2026-09-04T20:00:00Z")
    observed = pd.Timestamp("2026-09-06T12:00:00Z")
    start = cutoff - get_lookback_timedelta("1m")
    dates = [start - pd.Timedelta(minutes=1), start, start + pd.Timedelta(minutes=1),
             observed - get_lookback_timedelta("1m"), cutoff - pd.Timedelta(minutes=1)]
    frame = delegated.frames["1m"].iloc[:len(dates)].copy()
    frame["date"] = dates
    delegated.frames["1m"] = frame
    return start, cutoff, observed


def test_canonical_query_supplements_only_missing_head_and_matches_closed_query(delegated):
    start, cutoff, observed = _canonical_head_rows(delegated)
    result = delegated.ex.canonical_closed_minute_history("AAPL.US", end_date=observed.isoformat())
    requests = list(delegated.requests)
    assert requests[-1][1] == start
    assert requests[-1][2] == observed - pd.Timedelta(days=30)
    assert len(requests) == 4  # Three original 10-day slices and one head supplement.
    assert result.iloc[0].date == start + pd.Timedelta(minutes=1)
    assert result.iloc[-1].date == cutoff
    assert len(result) == 4
    assert delegated.ex.can_reuse_closed_minute_history("AAPL.US", result)

    expected = delegated.ex.klines("AAPL.US", "1m", end_date=cutoff.isoformat())
    pd.testing.assert_frame_equal(result, expected)
    assert result.attrs["price_basis_revision"] == expected.attrs["price_basis_revision"]


def test_canonical_query_reuses_full_provider_page_when_it_already_covers_head(delegated, monkeypatch):
    start, cutoff, observed = _canonical_head_rows(delegated)
    original = delegated.cq._fetch_segment_data

    def full_page(code, period, adjust, end, lower, priority="interactive"):
        if lower == observed - pd.Timedelta(days=30):
            # A complete SDK page can extend before the requested lower bound.
            lower = start - pd.Timedelta(minutes=1)
        return original(code, period, adjust, end, lower, priority)

    monkeypatch.setattr(delegated.cq, "_fetch_segment_data", full_page)
    result = delegated.ex.canonical_closed_minute_history("AAPL.US", end_date=observed.isoformat())
    assert len(delegated.requests) == 3
    assert len(result) == 4
    assert result.iloc[0].date == start + pd.Timedelta(minutes=1)
    assert result.iloc[-1].date == cutoff
    assert delegated.ex.can_reuse_closed_minute_history("AAPL.US", result)


@pytest.mark.parametrize("failure", ["failed", "exception"])
def test_canonical_query_refuses_a_failed_head_supplement(delegated, monkeypatch, failure):
    start, _cutoff, observed = _canonical_head_rows(delegated)
    original = delegated.cq._fetch_segment_data

    def segment(code, period, adjust, end, lower, priority="interactive"):
        if lower == start:
            if failure == "exception":
                raise TimeoutError("head unavailable")
            return [], "failed"
        return original(code, period, adjust, end, lower, priority)

    monkeypatch.setattr(delegated.cq, "_fetch_segment_data", segment)
    result = delegated.ex.canonical_closed_minute_history("AAPL.US", end_date=observed.isoformat())
    assert result.empty
    assert result.attrs["fetch_incomplete"] is True
    assert not delegated.ex.can_reuse_closed_minute_history("AAPL.US", result)


def test_canonical_observation_is_frozen_before_download_crosses_close(delegated, monkeypatch):
    observed = [pd.Timestamp("2026-09-04T19:59:59Z")]
    monkeypatch.setattr(pd.Timestamp, "now", lambda **_kwargs: observed[0])
    original = delegated.cq._fetch_segment_data

    def download(*args, **kwargs):
        observed[0] = pd.Timestamp("2026-09-04T20:00:02Z")
        return original(*args, **kwargs)

    monkeypatch.setattr(delegated.cq, "_fetch_segment_data", download)
    result = delegated.ex.canonical_closed_minute_history("AAPL.US")
    assert result.iloc[-1].date == pd.Timestamp("2026-09-04T19:59:00Z")
    assert delegated.ex.can_reuse_closed_minute_history("AAPL.US", result)


def test_explicit_range_and_unaware_provider_keep_the_original_query_contract(delegated, monkeypatch):
    calls = []
    monkeypatch.setattr(delegated.ex, "klines", lambda *args, **kwargs: calls.append((args, kwargs)))
    delegated.ex.canonical_closed_minute_history("AAPL.US", start_date="2026-09-04", args={"right": 1})
    assert calls[-1] == (("AAPL.US", "1m"), {
        "start_date": "2026-09-04", "end_date": None, "args": {"right": 1},
    })
    monkeypatch.setattr(delegated.cq, "supports_canonical_minute_history", False)
    delegated.ex.canonical_closed_minute_history("AAPL.US", end_date="2026-09-04")
    assert calls[-1] == (("AAPL.US", "1m"), {
        "start_date": None, "end_date": "2026-09-04", "args": {},
    })
