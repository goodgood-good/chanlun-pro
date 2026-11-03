from copy import deepcopy
from types import SimpleNamespace
from threading import Event
import pandas as pd
import pytest

from chanlun.exchange.us_dual_history import DualUSHistory, USDualSourcePending
from chanlun.exchange.quote_capacity import (
    QuoteConnectionBusy,
    QuoteConnectionCancelled,
    quote_request_scope,
)
from chanlun.exchange.errors import USmartAPIError
from chanlun.exchange.errors import LongbridgeConnectionLimitError
from chanlun.exchange.longbridge_history_limit import LongbridgeHistoryBusy
from chanlun.exchange.usmart_rate_limit import (
    USmartRateLimitTimeout,
    USmartRateLimitCancelled,
)

END = "2026-10-27T20:00:00+00:00"


def frame(provider, *, label="end", volume=100):
    dates = pd.to_datetime(["2026-10-27 19:55Z", "2026-10-27 20:00Z"])
    if label == "start":
        dates -= pd.Timedelta(minutes=5)
    f = pd.DataFrame(
        {
            "date": dates,
            "code": "QQQ.US",
            "frequency": "5m",
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "volume": float(volume),
        }
    )
    f.attrs = {
        "price_basis_provider": provider,
        "price_basis_revision": provider + "-basis",
        "price_basis_adjustment": "forward",
        "structure_price_quantum": ".001",
        "bar_time_label": label,
    }
    return f


class Source:
    def __init__(self, provider, *, error=None, label="end", volume=100):
        self.provider, self.error, self.kline_time_label, self.volume = (
            provider,
            error,
            label,
            volume,
        )
        self.calls = []

    def support_frequencys(self):
        return {"1m": "1m", "5m": "5m", "30m": "30m"}

    def klines(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return frame(self.provider, label=self.kline_time_label, volume=self.volume)


def router(tmp_path, **kwargs):
    native = Source("usmart", volume=200)
    lb = Source("longbridge", label="start", **kwargs)
    return DualUSHistory(native, lambda: lb, root=tmp_path), native, lb


def test_primary_is_one_complete_normalized_source(tmp_path):
    route, native, lb = router(tmp_path)
    actual = route.klines("QQQ.US", "5m", end_date=END)
    assert not native.calls and len(lb.calls) == 1
    pd.testing.assert_series_equal(actual.date, frame("usmart").date)
    assert (
        actual.attrs["price_basis_provider"] == "longbridge"
        and actual.volume.eq(100).all()
    )


def test_busy_primary_uses_the_entire_other_window_and_pins_peer_source(tmp_path):
    route, native, lb = router(tmp_path, error=QuoteConnectionBusy("busy"))
    actual = route.klines("QQQ.US", "5m", end_date=END)
    assert (
        actual.attrs["price_basis_provider"] == "usmart" and actual.volume.eq(200).all()
    )
    assert route._selected("QQQ.US") == "usmart"
    with pytest.raises(USDualSourcePending):
        route.klines(
            "QQQ.US", "5m", end_date=END, args={"_history_provider": "longbridge"}
        )
    assert len(native.calls) == 1, "a pinned dependency may not silently change source"


@pytest.mark.parametrize("damage", ["clock", "geometry", "identity", "incomplete"])
def test_bad_primary_is_rejected_before_any_rows_are_merged(tmp_path, damage):
    route, native, lb = router(tmp_path)
    bad = frame("longbridge", label="start")
    if damage == "clock":
        bad = bad.iloc[:1].copy()
    elif damage == "geometry":
        bad.loc[0, "low"] = 13.0
    elif damage == "identity":
        bad["code"] = "OTHER.US"
    else:
        bad.attrs["fetch_incomplete"] = True
    lb.klines = lambda *_a, **_kw: deepcopy(bad)
    actual = route.klines("QQQ.US", "5m", end_date=END)
    assert len(actual) == 2 and actual.volume.eq(200).all()
    assert actual.attrs["price_basis_provider"] == "usmart"


def test_cancelled_request_never_opens_a_second_source(tmp_path):
    route, native, lb = router(tmp_path)
    cancel = Event()
    cancel.set()
    with quote_request_scope(cancel), pytest.raises(QuoteConnectionCancelled):
        route.klines("QQQ.US", "5m", end_date=END)
    assert not native.calls and not lb.calls


def test_both_permanently_bad_sources_remain_an_explicit_error(tmp_path):
    route, native, lb = router(tmp_path, error=ValueError("invalid source"))
    native.error = ValueError("invalid source")
    with pytest.raises(USmartAPIError) as caught:
        route.klines("QQQ.US", "5m", end_date=END)
    assert caught.value.code == "us_dual_sources_unavailable"


@pytest.mark.parametrize(
    "lb_error", [LongbridgeHistoryBusy(), LongbridgeConnectionLimitError()]
)
@pytest.mark.parametrize(
    "native_error", [USmartRateLimitTimeout(), USmartRateLimitCancelled()]
)
def test_two_admission_waits_never_exhaust_the_provider_failure_budget(
    tmp_path, lb_error, native_error
):
    route, native, lb = router(tmp_path, error=lb_error)
    native.error = native_error
    with pytest.raises(USDualSourcePending) as caught:
        route.klines("QQQ.US", "5m", end_date=END)
    assert caught.value.preparation["retry_after"] == 3
    assert set(caught.value.reasons) == {"longbridge", "usmart"}


def test_explicit_older_peer_can_not_flip_the_shared_selected_source(tmp_path):
    route, native, lb = router(tmp_path, error=QuoteConnectionBusy())
    route.klines("QQQ.US", "5m", end_date=END)
    lb.error = None
    route.klines("QQQ.US", "5m", end_date=END, args={"_history_provider": "longbridge"})
    assert route._selected("QQQ.US") == "usmart"


def test_gateway_does_not_shift_closed_labels_twice(tmp_path, monkeypatch):
    from chanlun import config
    from chanlun.exchange.exchange_usmart import ExchangeUSmart

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    source = Source("longbridge", label="start")
    gateway = ExchangeUSmart(
        "us", client=SimpleNamespace(), history_exchange=source, history_source="dual"
    )
    actual = gateway.klines("QQQ.US", "5m", end_date=END)
    assert list(actual.date) == list(frame("usmart").date)


def test_tick_fallback_keeps_each_provider_record_whole_and_only_fetches_missing(
    tmp_path, monkeypatch
):
    from chanlun import config
    from chanlun.exchange.exchange_usmart import ExchangeUSmart

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    gateway = ExchangeUSmart("us", client=SimpleNamespace(), history_source="dual")
    native = object()
    alternate = object()
    calls = []
    monkeypatch.setattr(gateway, "_native_ticks", lambda codes: {"QQQ.US": native})
    lb = SimpleNamespace(
        ticks=lambda codes: calls.append(codes) or {"SPY.US": alternate}
    )
    route = SimpleNamespace(_provider=lambda name: lb)
    monkeypatch.setattr(gateway, "_configured_us_history_exchange", lambda *_: route)
    actual = gateway.ticks(["QQQ.US", "SPY.US"])
    assert actual == {"QQQ.US": native, "SPY.US": alternate} and calls == [["SPY.US"]]


def test_tick_fallback_also_handles_native_connection_failure(tmp_path, monkeypatch):
    from chanlun import config
    from chanlun.exchange.exchange_usmart import ExchangeUSmart

    monkeypatch.setattr(config, "DATA_PATH", str(tmp_path))
    gateway = ExchangeUSmart("us", client=SimpleNamespace(), history_source="dual")

    def failed(_):
        raise TimeoutError("native unavailable")

    monkeypatch.setattr(gateway, "_native_ticks", failed)
    result = {"QQQ.US": object()}
    monkeypatch.setattr(
        gateway,
        "_configured_us_history_exchange",
        lambda *_: SimpleNamespace(
            _provider=lambda _: SimpleNamespace(ticks=lambda codes: result)
        ),
    )
    assert gateway.ticks(["QQQ.US"]) == result
