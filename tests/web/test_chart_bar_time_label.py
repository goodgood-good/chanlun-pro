"""US intraday source coordinates travel independently of recursive evidence."""

import asyncio
import json
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult
from cl_app import create_app
from cl_app.blueprints import tv
from cl_app.handlers.sse_stream import SseStreamHandler
from cl_app.services import chart_cache, chart_compute, chart_producer_identity
from cl_app.services import kline_recompute, sse_refresh
from cl_app.services.chart_bar_time import attach_chart_bar_time_label


def frame(label=None):
    value = pd.DataFrame({
        "date": pd.to_datetime(["2026-09-04 19:30Z", "2026-09-04 20:00Z"]),
        "open": [100., 101.], "high": [101., 102.], "low": [99., 100.],
        "close": [100.5, 101.5], "volume": [100., 200.],
    })
    value.attrs.update(structure_price_quantum="0.01", price_basis_revision="test-raw",
                       price_basis_provider="test", price_basis_adjustment="none")
    if label is not None:
        value.attrs["bar_time_label"] = label
    return value


def payload(label="end"):
    value = frame(label)
    return {
        "t": [int(t.timestamp()) for t in value["date"]],
        **{short: value[long].tolist() for short, long in (
            ("o", "open"), ("h", "high"), ("l", "low"), ("c", "close"), ("v", "volume"))},
        "bar_time_label": label,
        "price_basis": {key: val for key, val in value.attrs.items() if key != "bar_time_label"},
        "strict_structure_mode": "unavailable",
        "strict_structure_error": {"code": "test-unavailable"},
    }


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
@pytest.mark.parametrize("label", ["start", "end"])
def test_actual_exchange_label_keeps_source_times_and_does_not_mutate_frame(frequency, label):
    source = frame()
    result = attach_chart_bar_time_label(source, market="us", frequency=frequency,
                                         exchange=SimpleNamespace(kline_time_label=label))
    assert result.equals(source)
    assert "bar_time_label" not in source.attrs
    assert result.attrs["bar_time_label"] == label


def test_actual_adapters_declare_different_us_time_conventions():
    from chanlun.exchange.exchange_cq import ExchangeChangQiao
    from chanlun.exchange.exchange_usmart import ExchangeUSmart
    assert ExchangeChangQiao.kline_time_label == "start"
    assert ExchangeUSmart.kline_time_label == "end"


@pytest.mark.parametrize("market,frequency", [("a", "30m"), ("hk", "5m"), ("us", "d")])
def test_other_contracts_remain_unmodified(market, frequency):
    source = frame()
    assert attach_chart_bar_time_label(source, market=market, frequency=frequency,
                                      exchange=SimpleNamespace(kline_time_label="end")) is source
    assert "bar_time_label" not in source.attrs


def test_unknown_adapter_is_not_guessed_and_conflicting_declaration_is_rejected():
    source = frame()
    assert attach_chart_bar_time_label(source, market="us", frequency="30m",
                                      exchange=object()) is source
    with pytest.raises(ValueError, match="differ"):
        attach_chart_bar_time_label(frame("start"), market="us", frequency="30m",
                                    exchange=SimpleNamespace(kline_time_label="end"))


@pytest.mark.parametrize("frequency", ["1m", "5m", "30m"])
@pytest.mark.parametrize("label", ["start", "end"])
def test_real_serializer_retains_label_with_unavailable_structure(monkeypatch, frequency, label):
    unavailable = StrictChartRuntimeResult.unavailable("test_unavailable", "test")
    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", lambda **_: unavailable)
    source = frame(label)
    result = chart_compute.serialize_chart_data_with_strict_runtime(
        market="us", code="AAPL.US", display_frequency=frequency,
        display_klines=source, chart_config={}, strict_runtime=unavailable,
    )
    assert result["strict_structure_mode"] == "unavailable"
    assert result["bar_time_label"] == label
    assert result["t"] == payload()["t"]
    assert kline_recompute.extract_klines_df_from_chart_data(result).attrs == source.attrs


@pytest.mark.parametrize("label", ["start", "end"])
def test_unlabelled_direct_serializer_uses_actual_adapter_for_closed_bar_filter(monkeypatch, label):
    unavailable = StrictChartRuntimeResult.unavailable("test_unavailable", "test")
    monkeypatch.setattr(chart_compute, "get_exchange", lambda _: SimpleNamespace(kline_time_label=label))
    seen = []
    def drop(value, frequency, *, time_label):
        seen.append((frequency, time_label))
        return value
    monkeypatch.setattr(chart_compute, "drop_unclosed_last_bar", drop)
    result = chart_compute.serialize_chart_data_with_strict_runtime(
        market="us", code="AAPL.US", display_frequency="1m",
        display_klines=frame(), chart_config={}, strict_runtime=unavailable,
    )
    assert seen == [("1m", label)]
    assert result["bar_time_label"] == label
    assert result["t"] == payload()["t"]


def test_all_history_projections_preserve_label_and_raw_coordinates():
    data = payload()
    first, last = data["t"]
    for result, expected in (
        (chart_compute.slice_chart_data_to_window(data, first, last), [first]),
        (chart_compute.slice_chart_data_to_countback(data, 1), [last]),
        (chart_compute.trim_future_bars(data, first), [first]),
    ):
        assert result["bar_time_label"] == "end"
        assert result["t"] == expected
    assert data["t"] == [first, last]


def test_prepend_preserves_matching_label_even_when_nonbasis_attrs_differ():
    cached = kline_recompute.extract_klines_df_from_chart_data(payload())
    new = frame("end")
    new.attrs["diagnostic"] = "not part of price or time identity"
    result = kline_recompute.merge_klines_df(cached, new)
    assert result.attrs["bar_time_label"] == "end"
    assert result["date"].tolist() == new["date"].tolist()


@pytest.mark.parametrize("old,new", [("start", "end"), ("end", "start"), (None, "end"), ("end", None)])
def test_prepend_rejects_changed_or_unknown_time_convention(old, new):
    with pytest.raises(kline_recompute.PriceBasisMismatchError, match="bar time label"):
        kline_recompute.merge_klines_df(frame(old), frame(new))


def test_invalid_cached_label_is_fail_closed():
    data = payload()
    data["bar_time_label"] = ["end"]
    with pytest.raises(kline_recompute.PriceBasisMismatchError, match="bar time label"):
        kline_recompute.extract_klines_df_from_chart_data(data)




def test_incremental_runtime_never_reuses_identical_ohlcv_with_changed_label(monkeypatch):
    built = []
    def build(**_):
        result = StrictChartRuntimeResult.success(object())
        built.append(result)
        return result
    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", build)
    monkeypatch.setattr(chart_compute, "serialize_chart_data_with_strict_runtime",
                        lambda **kwargs: kwargs["strict_runtime"])
    kline_recompute.reset_cl_pool()
    try:
        results = [kline_recompute.recompute_chart_data_from_klines(
            "us", "AAPL.US", "1m", {}, frame(label), cache_key="bar-label-pool",
        ) for label in ("start", "end")]
        assert results[0] is not results[1]
        assert len(built) == 2
    finally:
        kline_recompute.reset_cl_pool()


def test_chart_identity_changes_for_web_producer_and_provider_without_global_cl_change(monkeypatch, tmp_path):
    from chanlun import config
    root = tmp_path / "cl_app"
    services = root / "services"
    services.mkdir(parents=True)
    identity = services / "chart_producer_identity.py"
    identity.write_text("first producer", encoding="utf-8")
    monkeypatch.setattr(chart_producer_identity, "__file__", str(identity))
    global_revision = chart_cache.source_fingerprint()
    chart_producer_identity.chart_producer_revision.cache_clear()
    try:
        first = chart_cache._build_cache_key("us", "AAPL.US", "30m", {})
        (services / "chart_compute.py").write_text("new source", encoding="utf-8")
        chart_producer_identity.chart_producer_revision.cache_clear()
        second = chart_cache._build_cache_key("us", "AAPL.US", "30m", {})
        monkeypatch.setattr(config, "EXCHANGE_US", "a-different-provider")
        third = chart_cache._build_cache_key("us", "AAPL.US", "30m", {})
        assert len({first, second, third}) == 3
        assert all(key.startswith(global_revision + "_chart") for key in (first, second, third))
        assert chart_cache.source_fingerprint() == global_revision
    finally:
        chart_producer_identity.chart_producer_revision.cache_clear()


@pytest.fixture(scope="module")
def client():
    app = create_app(test_config={
        "TESTING": True, "LOGIN_DISABLED": True, "VALIDATE_WEB_SECURITY": False,
        "WTF_CSRF_ENABLED": False,
    })
    yield app.test_client()
    app.extensions["shutdown_runtime_services"]()


@pytest.mark.parametrize("first", [True, False])
def test_history_label_is_independent_of_full_or_partial_strict_response(client, monkeypatch, first):
    data = payload()
    monkeypatch.setattr(tv, "query_cl_chart_config", lambda *_: {})
    monkeypatch.setattr(tv, "market_now_trading", lambda _: False)
    monkeypatch.setattr(tv, "_should_suppress_realtime_history_poll", lambda **_: False)
    monkeypatch.setattr(tv, "_mark_user_request", lambda *_: None)
    monkeypatch.setattr(tv, "_get_chart_cache_entry_ram_only", lambda _: None)
    monkeypatch.setattr(tv, "_get_chart_cache_entry", lambda _: {"data": data, "is_full_snapshot": True})
    monkeypatch.setattr(tv, "evaluate_cache_for_tv_history", lambda *a, **k: (True, data, "hit", False))
    monkeypatch.setattr(tv, "market_frequencys", SimpleNamespace(cached_snapshot=lambda _: {"us": ["30m"]}))
    response = client.get("/tv/history", query_string={
        "symbol": "us:AAPL.US", "resolution": "30", "from": data["t"][0],
        "to": data["t"][-1] + (1800 if first else -900),
        "firstDataRequest": "true" if first else "false",
    })
    assert response.status_code == 200
    result = response.get_json()
    assert result["s"] == "ok"
    assert result["bar_time_label"] == "end"
    assert result["t"] == (data["t"] if first else data["t"][:1])
    assert result["strict_structure_mode"] == ("unavailable" if first else "unchanged")


def test_sse_cached_snapshot_keeps_label_even_when_structure_unavailable(monkeypatch):
    data = payload()
    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry_ram_only", lambda _: {"data": data})
    sent = []
    async def send(value):
        sent.append(json.loads(value))
    handler = SimpleNamespace(_cache_key="label-test", _send=send)
    asyncio.run(SseStreamHandler._send_current_snapshot(handler))
    assert sent[0]["bar_time_label"] == "end"
    assert sent[0]["t"] == data["t"]
    assert sent[0]["strict_structure_mode"] == "unavailable"
    _, old = sse_refresh.decide_push(None, data)
    push, _ = sse_refresh.decide_push(old, dict(data, bar_time_label="start"))
    assert push


def test_sse_fetch_attaches_actual_adapter_label_before_merging(monkeypatch):
    monkeypatch.setattr(chart_cache, "_get_chart_cache_entry_ram_only", lambda _: {"data": payload()})
    monkeypatch.setattr(chart_cache, "_is_negatively_cached", lambda _: False)
    monkeypatch.setattr(sse_refresh, "get_exchange", lambda _: SimpleNamespace(
        kline_time_label="end", klines=lambda *a, **k: frame(),
    ))
    seen = []
    def prepend(*args):
        seen.append(args[4])
        return payload()
    monkeypatch.setattr(kline_recompute, "prepend_klines_and_replace_cache", prepend)
    result = sse_refresh.recompute_chart_data("us", "AAPL.US", "30m", {}, "label-test")
    assert result["bar_time_label"] == "end"
    assert seen[0].attrs["bar_time_label"] == "end"
    assert seen[0]["date"].tolist() == frame()["date"].tolist()
