"""Native Longbridge pages preserve the frozen proof and bound every slice."""

from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun import config
from chanlun.exchange import exchange_cq as cq
from cl_app.services import notification_history as history
from cl_app.services.notification_longbridge_history import (
    LongbridgePageSource,
    decode_page,
)


@pytest.fixture(autouse=True)
def backend(monkeypatch):
    monkeypatch.setattr(config, "EXCHANGE_US", "usmart")
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "longbridge")


def frame(stamps):
    raw = pd.DataFrame(
        {
            "date": pd.to_datetime(stamps, utc=True),
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "volume": 100.0,
        }
    )
    result = cq.normalize_history_frame(raw, "QQQ.US", "30m", "us")
    result.attrs["bar_time_label"] = "end"
    return result


def payload(candles, **extra):
    return {
        "provider": "longbridge",
        "code": "QQQ.US",
        "frequency": "30m",
        "right": 1,
        "basis": dict(candles.attrs),
        "rows": [
            {
                "time": int(row.date.timestamp()),
                **{k: getattr(row, k) for k in history.VALUES},
            }
            for row in candles.itertuples()
        ],
        **extra,
    }


class Pages:
    identity = "native-offline"

    def __init__(self, *values):
        self.values, self.calls = list(values), []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


def setup():
    anchor = frame(pd.date_range("2026-10-26 14:00Z", periods=4, freq="30min"))
    event = {
        "id": "qqq-old",
        "market": "us",
        "code": "QQQ.US",
        "source_closed_at": int(anchor.date.max().timestamp()),
    }
    return anchor, event


def prepare(anchor, event, pages, **kwargs):
    return history.prepare_notification_history(
        event,
        "30m",
        anchor.attrs,
        anchor_frame=anchor,
        aligned_cutoff=event["source_closed_at"],
        page_source=pages,
        **kwargs,
    )


def test_longbridge_keeps_overlap_between_pages_and_never_calls_account_floor_market_origin():
    anchor, event = setup()
    older = frame(pd.date_range("2026-10-26 13:00Z", periods=3, freq="30min"))
    pages = Pages(
        payload(anchor),
        payload(older),
        payload(older.iloc[:0], source_boundary="provider_retention_limit"),
    )
    first = prepare(anchor, event, pages, max_pages=1)
    assert pd.Timestamp(first.state["cursor"]) == anchor.date.min()
    result = prepare(anchor, event, pages, previous=first)
    assert (
        result.coverage["provider_exhausted"] and not result.coverage["source_origin"]
    )
    assert (
        result.coverage["compatible_with_anchor"] and result.coverage["anchor_complete"]
    )
    assert result.frame.date.is_unique and len(result.frame) == 6


def test_revised_vendor_price_is_rejected_by_default_and_separate_view_never_patches_frozen_anchor():
    anchor, event = setup()
    frozen = anchor.copy(deep=True)
    current = anchor.copy(deep=True)
    current.loc[0, "open"] = 10.5
    strict = prepare(anchor, event, Pages(payload(current)))
    assert strict.coverage["error_code"] == "anchor_values_changed"
    assert not strict.coverage["compatible_with_anchor"]
    pages = Pages(
        payload(current),
        payload(current.iloc[:0], source_boundary="provider_empty_before_cursor"),
    )
    revised = prepare(anchor, event, pages, allow_source_revisions=True)
    assert revised.coverage["source_view"] == "current_source_history"
    assert revised.coverage["source_revision_rows"] == 1
    assert (
        revised.coverage["independent_source_verified"]
        and revised.coverage["original_frozen_preserved"]
    )
    assert (
        not revised.coverage["price_basis_overlap_verified"]
        and not revised.coverage["compatible_with_anchor"]
    )
    assert revised.frame.open.iloc[0] == 10.5
    pd.testing.assert_frame_equal(anchor, frozen)


@pytest.mark.parametrize("change", ["date", "price", "basis"])
def test_independent_view_still_rejects_broken_chain_or_basis(change):
    anchor, event = setup()
    page = frame(pd.date_range("2026-10-26 13:00Z", periods=3, freq="30min"))
    if change == "date":
        page = page.iloc[:-1]
    elif change == "price":
        page.loc[page.index[-1], "volume"] = 101
    else:
        page.attrs["price_basis_revision"] = "changed"
    result = prepare(
        anchor,
        event,
        Pages(payload(anchor), payload(page)),
        allow_source_revisions=True,
    )
    assert result.coverage["reason"] == "contract_error"
    assert (
        not result.coverage["compatible_with_anchor"]
        and not result.coverage["independent_source_verified"]
    )


def test_missing_frozen_dates_are_not_accepted_as_price_revisions():
    anchor, event = setup()
    result = prepare(
        anchor, event, Pages(payload(anchor.drop(index=1))), allow_source_revisions=True
    )
    assert result.coverage["error_code"] == "anchor_rows_missing"


def test_opening_overlap_page_advances_without_claiming_source_exhaustion():
    anchor, event = setup()
    before = anchor.date.min() - pd.Timedelta(minutes=30, milliseconds=1)
    pages = Pages(
        payload(anchor),
        payload(
            anchor.iloc[:0],
            source_boundary="opening_overlap_only",
            next_cursor=before.isoformat(),
        ),
    )
    result = prepare(anchor, event, pages, max_pages=2)
    assert result.coverage["can_resume"] and not result.coverage["provider_exhausted"]
    assert pd.Timestamp(result.state["cursor"]) == before


@pytest.mark.parametrize("boundary", [None, "market_origin", "arbitrary_limit"])
def test_an_unverified_empty_page_is_not_an_origin(boundary):
    anchor, event = setup()
    result = prepare(
        anchor, event, Pages(payload(anchor.iloc[:0], source_boundary=boundary))
    )
    assert (
        result.coverage["reason"] == "contract_error"
        and not result.coverage["provider_exhausted"]
    )


def sdk_candle(stamp):
    return SimpleNamespace(
        timestamp=int(pd.Timestamp(stamp).timestamp()),
        open=10.0,
        high=12.0,
        low=9.0,
        close=11.0,
        volume=100.0,
    )


def test_native_adapter_reuses_normal_price_contract_and_filters_forming_opening_label():
    calls = []
    candles = [
        sdk_candle(t)
        for t in ("2026-10-26 13:30Z", "2026-10-26 14:00Z", "2026-10-26 14:30Z")
    ]
    exchange = SimpleNamespace(
        _fetch_candlesticks_api=lambda *a, **kw: calls.append((a, kw)) or candles
    )
    source = LongbridgePageSource(exchange_factory=lambda: exchange)
    cutoff = pd.Timestamp("2026-10-26 14:30Z")
    data = source(code="QQQ.US", frequency="30m", cursor=cutoff, right=1, remaining=4)
    result = decode_page(data, code="QQQ.US", frequency="30m", cursor=cutoff, right=1)
    assert list(result.date) == list(
        pd.to_datetime(["2026-10-26 14:00Z", "2026-10-26 14:30Z"])
    )
    assert (
        result.attrs["bar_time_label"] == "end"
        and result.attrs["price_basis_provider"] == "longbridge"
    )
    assert (
        0 < calls[0][1]["timeout_seconds"] <= 4 and calls[0][1]["priority"] == "prewarm"
    )
    assert calls[0][0][3] == 1000


@pytest.mark.parametrize(
    "message,allowed",
    [("out of minute kline begin date", True), ("invalid request", False)],
)
def test_only_specific_retention_error_is_a_source_boundary(message, allowed):
    exc = cq.OpenApiException.__new__(cq.OpenApiException)
    exc.code, exc.message = 301600, message

    def fail(*a, **kw):
        raise exc

    source = LongbridgePageSource(
        exchange_factory=lambda: SimpleNamespace(_fetch_candlesticks_api=fail)
    )
    args = dict(
        code="QQQ.US",
        frequency="30m",
        cursor=pd.Timestamp("2026-10-26 14:30Z"),
        right=1,
        remaining=1,
    )
    if allowed:
        assert source(**args)["source_boundary"] == "provider_retention_limit"
    else:
        with pytest.raises(cq.OpenApiException):
            source(**args)


def test_revised_view_is_labelled_in_chart_and_public_receipt():
    from cl_app.services.tradingview_snapshot import history_description
    from cl_app.services.recent_notifications import _public_history

    coverage = {
        "state": "source_exhausted",
        "source_view": "current_source_history",
        "source_revision_rows": 1,
        "provider_exhausted": True,
        "source_origin": False,
    }
    assert "事后修订" in history_description("30m", coverage)
    public = _public_history({"30m": coverage})["30m"]
    assert public["source_revision_rows"] == 1 and public["provider_exhausted"]
    assert (
        public["source_view"] == "current_source_history"
        and not public["source_origin"]
    )
