"""IPO placeholders must not become bars or apparent holes in listed history."""
from datetime import datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.exchange import exchange_qmt
from chanlun.screening.rules import CN, expected_closes_between, frame_quality, trading_context


@pytest.fixture
def listing_feed(monkeypatch):
    context = {**trading_context(datetime(2026, 9, 30, 15, tzinfo=CN), "5m"), "symbol": "BJ.920025"}
    closes = sorted(expected_closes_between(context, int(datetime(2026, 9, 23, tzinfo=CN).timestamp())))
    dates = [int(datetime(2026, 9, 14, 14, minute, tzinfo=CN).timestamp()) for minute in (50, 55)]
    dates += [int(datetime(2026, 9, 14, 15, tzinfo=CN).timestamp()), *closes]
    state = SimpleNamespace(open_date="20260923", stock=True, pre_volume=0, metadata_error=False, calls=0)

    def detail(*_):
        state.calls += 1
        if state.metadata_error:
            raise RuntimeError("metadata offline")
        return {"ExchangeID": "BJ", "InstrumentID": "920025", "OpenDate": state.open_date}

    def market_data(**_):
        values = {"time": [at * 1000 for at in dates], "open": [4.26] * 3 + [30.] * len(closes),
                  "high": [4.26] * 3 + [31.] * len(closes), "low": [4.26] * 3 + [29.] * len(closes),
                  "close": [4.26] * 3 + [30.5] * len(closes), "volume": [state.pre_volume] * 3 + [100.] * len(closes)}
        return {key: pd.DataFrame([value]) for key, value in values.items()}

    monkeypatch.setattr(exchange_qmt, "xtdata", SimpleNamespace(
        enable_hello=False, get_market_data=market_data, get_divid_factors=lambda *_: pd.DataFrame(),
        get_instrument_detail=detail, get_instrument_type=lambda *_: {"stock": True} if state.stock else {"index": True},
    ))
    state.ex = exchange_qmt.ExchangeQMT()
    state.context = context
    state.read = lambda: state.ex.klines("BJ.920025", "5m", start_date="20250930", end_date="2026-09-30 15:00:00",
                                        args={"skip_download": True, "exact_end": True})
    return state


def test_prelisting_zero_volume_rows_do_not_create_false_data_gaps(listing_feed):
    frame = listing_feed.read()
    assert len(frame) == 240
    assert frame.date.iloc[0] == pd.Timestamp("2026-09-23 09:35", tz=CN)
    assert frame.attrs["qmt_prelisting_rows_removed"] == 3
    assert frame.attrs["qmt_listing_date"] == "2026-09-23"
    assert frame_quality(frame, listing_feed.context)["errors"] == []
    listing_feed.read()
    assert listing_feed.calls == 1  # Repeated periods reuse bounded metadata.


@pytest.mark.parametrize("date", [None, "0", "19700101", "19700106", "20260230", "unknown"])
def test_unavailable_or_special_listing_dates_do_not_trim_history(listing_feed, date):
    listing_feed.open_date = date
    assert len(listing_feed.read()) == 243


def test_metadata_failure_keeps_unknown_history_instead_of_guessing_a_listing_date(listing_feed):
    listing_feed.metadata_error = True
    assert len(listing_feed.read()) == 243


def test_complete_post_listing_local_history_does_not_trigger_pointless_download(listing_feed, monkeypatch):
    import datetime as dt
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            current = dt.datetime(2026, 9, 30, 15, 1, tzinfo=CN)
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)
    module = SimpleNamespace(**vars(dt))
    module.datetime = Clock
    monkeypatch.setattr(exchange_qmt, "datetime", module)
    # The fake provider has no download method: using it would fail this call.
    frame = listing_feed.ex.klines("BJ.920025", "5m", start_date="20250930", end_date="2026-09-30 15:00:00",
                                  args={"prefer_local": True, "exact_end": True})
    assert len(frame) == 240
    assert frame.attrs["qmt_history_read_mode"] == "local_complete"
    assert listing_feed.calls == 1


def test_index_history_is_not_cut_at_its_publication_date(listing_feed):
    listing_feed.stock = False
    assert len(listing_feed.read()) == 243


def test_real_trades_before_the_reported_listing_date_are_not_silently_deleted(listing_feed):
    from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
    listing_feed.pre_volume = 100
    frame = listing_feed.read()
    assert len(frame) == 243
    assert frame.attrs["qmt_listing_history_conflict"] is True
    assert "LISTING_METADATA_CONFLICT" in frame_quality(frame, listing_feed.context)["errors"]
    runtime = build_strict_chart_cd(market="a", code="BJ.920025", frequency="5m", frame=frame)
    assert runtime.cd is None and runtime.error_code == "listing_metadata_conflict"
