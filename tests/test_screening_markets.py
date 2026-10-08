"""Cross-market identity, calendar gaps, closed sessions and watchlist scope."""

from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pandas as pd
import pytest

from chanlun.screening import markets, runner
from chanlun.screening.rules import CN, frame_quality


def test_watchlist_union_does_not_enumerate_non_a_markets(monkeypatch, tmp_path):
    source = Mock()
    source.all_stocks.return_value = [{"code": "SH.600088", "name": "A", "type": "stock_cn"}]
    monkeypatch.setattr(runner, "_exchange", lambda: source)
    monkeypatch.setattr(markets, "watchlist_symbols", lambda: [
        {"market": "a", "code": "SH.600088", "name": "A", "origin": "watchlist"},
        {"market": "us", "code": "TSLA.US", "name": "Tesla", "origin": "watchlist"},
        {"market": "currency_spot", "code": "BTC/USDT", "name": "BTC", "origin": "watchlist"},
    ])
    connection = Mock()
    runner._catalog_worker(connection, {"scope": "all_a_watchlist", "exclude_st": True}, tmp_path)
    stocks = connection.send.call_args.args[0]["stocks"]
    assert {(s["market"], s["code"]) for s in stocks} == {("a", "SH.600088"), ("us", "TSLA.US"), ("currency_spot", "BTC/USDT")}
    source.all_stocks.assert_called_once_with(full_market_authorized=True)


def test_market_and_symbol_are_part_of_safe_evidence_identity(tmp_path):
    from chanlun.screening.evidence import evidence_paths
    spot = evidence_paths(tmp_path, "BTC/USDT", "5m", "currency_spot")
    future = evidence_paths(tmp_path, "BTC/USDT", "5m", "currency")
    assert spot != future
    assert all(p.parent == tmp_path / "evidence" for p in (*spot, *future))
    with pytest.raises(ValueError):
        evidence_paths(tmp_path, "../../secret", "5m", "us")


def test_us_session_changes_with_dst_and_daily_selection_uses_completed_session():
    before = markets.market_context("us", "TSLA.US", datetime(2026, 3, 6, 23, 0, tzinfo=CN), "5m")
    after = markets.market_context("us", "TSLA.US", datetime(2026, 3, 9, 23, 0, tzinfo=CN), "5m")
    assert datetime.fromtimestamp(before["session_periods"][-1][0], timezone.utc).hour == 14
    assert datetime.fromtimestamp(after["session_periods"][-1][0], timezone.utc).hour == 13
    closed = markets.market_context("hk", "00700.HK", datetime(2026, 9, 17, 15, 10, tzinfo=CN), "5m", completed_session=True)
    assert datetime.fromtimestamp(closed["cutoff"], CN) == datetime(2026, 9, 16, 16, tzinfo=CN)


@pytest.mark.parametrize("market,code", [("us", "TSLA.US"), ("hk", "00700.HK")])
def test_non_a_calendar_accepts_real_session_breaks_but_rejects_missing_bars(market, code):
    context = markets.market_context(market, code, datetime(2026, 9, 17, 20, 0, tzinfo=CN), "5m")
    stamps = [t for opening, closing in context["session_periods"][-8:] for t in range(opening+300, closing+1, 300)]
    frame = pd.DataFrame({"date": pd.to_datetime(stamps, unit="s", utc=True), "code": code,
                          "open": 10., "high": 11., "low": 9., "close": 10., "volume": 100.})
    assert frame_quality(frame, context)["errors"] == []
    assert "DATA_GAPS" in frame_quality(frame.drop(index=200), context)["errors"]


def test_unknown_instrument_session_is_diagnostic_not_shanghai_calendar():
    with pytest.raises(ValueError, match="SESSION_CALENDAR_UNAVAILABLE"):
        markets.market_context("fx", "FE.USDCNY", datetime(2026, 9, 17, 20, tzinfo=CN), "5m")


def test_unverified_provider_contract_is_rejected_before_expensive_history_requests(monkeypatch):
    from types import SimpleNamespace
    from chanlun.screening.errors import MarketDataContractError
    history = Mock(side_effect=AssertionError("history must not be fetched"))
    ex = SimpleNamespace(screening_time_contract_verified=False, klines=history)
    monkeypatch.setattr("chanlun.exchange.get_exchange", lambda _: ex)
    with pytest.raises(MarketDataContractError) as error:
        markets.external_frame("NY.CL00W", "5m", {"market":"ny_futures","cutoff":1})
    assert error.value.reason == "BAR_TIME_LABEL_UNAVAILABLE"
    history.assert_not_called()


def test_source_contract_failures_keep_their_diagnostic_instead_of_engine_error(monkeypatch, tmp_path):
    from chanlun.screening.errors import MarketDataContractError
    def unavailable(*_):
        raise MarketDataContractError("BAR_TIME_LABEL_UNAVAILABLE", "unverified clock")
    monkeypatch.setattr(runner, "fetch_frame", unavailable)
    result = runner.scan_symbol({"code":"NY.CL00W","market":"ny_futures","name":"fixture"},
                                {"frequencies":["5m"]}, {"5m":{}}, tmp_path)
    assert result["rows"][0]["data_errors"] == ["BAR_TIME_LABEL_UNAVAILABLE"]
    assert result["rows"][0]["selected"] == []


def test_missing_calendar_is_retained_as_a_structured_worker_diagnostic(tmp_path):
    import json
    from cl_app.services.screening import validate_settings, ScreeningManager
    settings = validate_settings({"scope":"watchlist","frequencies":["5m"]})
    runner.write_json(tmp_path / "request.json", {"observed_at":"2026-09-17T20:00:00+08:00"})
    result = runner._scan_worker_symbol({"market":"fx","code":"FE.USDCNY","name":"fixture"},
                                         settings, {}, tmp_path, "revision",
                                         {"observed_at":"2026-09-17T20:00:00+08:00"})
    assert result['rows'][0]['data_errors'] == ["SESSION_CALENDAR_UNAVAILABLE"]
    (tmp_path / "results.jsonl").write_text(json.dumps(result)+"\n",encoding="utf-8")
    loaded = ScreeningManager(tmp_path.parent)._read_results(tmp_path, {"completed":1})
    assert loaded["errors"][0]["reasons"] == ["SESSION_CALENDAR_UNAVAILABLE"]
    assert not loaded["verified_periods"]


def test_external_start_labels_are_closed_once_and_future_tail_is_excluded(monkeypatch):
    from chanlun.exchange.exchange_binance_common import normalize_binance_kline_frame
    context = markets.market_context("currency_spot", "BTC/USDT", datetime(2026, 9, 17, 20, tzinfo=CN), "5m")
    dates = pd.date_range(end=pd.Timestamp(context["cutoff"]+300, unit="s", tz="UTC"), periods=242, freq="5min")
    raw = pd.DataFrame({"date": dates, "code": "BTC/USDT", "open": 10., "high": 11., "low": 9., "close": 10., "volume": 100.})
    frame = normalize_binance_kline_frame(raw, market="currency_spot", code="BTC/USDT")
    exchange = Mock(spec=["klines"])
    exchange.klines.return_value = frame
    monkeypatch.setattr("chanlun.exchange.get_exchange", lambda market: exchange)
    result = markets.external_frame("BTC/USDT", "5m", context)
    assert len(result) == 240
    assert int(result.date.iloc[-1].timestamp()) == context["cutoff"]
    assert result.attrs["bar_time_label"] == "end"
    assert frame_quality(result, context)["errors"] == []


def test_no_native_segments_is_an_empty_snapshot_not_a_worker_failure(monkeypatch):
    from types import SimpleNamespace
    evidence = SimpleNamespace(structure=SimpleNamespace(levels=[]), confirmed_points=[], approaching_points=[])
    runtime = SimpleNamespace(cd=SimpleNamespace(get_strict_evidence=lambda: evidence, get_segment_units=lambda: ()))
    monkeypatch.setattr("chanlun.cl_utils.strict_chart_runtime.build_strict_chart_cd", lambda **kwargs: runtime)
    snapshot = {"analysis_scope": "native_centers", "levels": [{
        "structural_level": 0, "centers": [], "points": [],
    }]}
    monkeypatch.setattr("chanlun.cl_utils.tv_chart.cl_data_to_tv_chart", lambda *args, **kwargs: {
        "strict_structure_mode": "replace", "strict_structure": snapshot})
    frame = pd.DataFrame({"date": [pd.Timestamp("2026-09-17T15:00:00+08:00")]})
    assert runner.snapshot_for(frame, "SH.600088", "5m")["screening_segments"] == []


@pytest.mark.parametrize("market,code", [("us", "TSLA.US"), ("hk", "00700.HK"),
                                         ("currency_spot", "BTC/USDT")])
def test_direct_worker_builds_each_selected_market_period_at_the_observed_clock(
    tmp_path, monkeypatch, market, code,
):
    from cl_app.services.screening import validate_settings
    settings = validate_settings({"scope": "watchlist", "frequencies": ["1m", "5m", "30m"]})
    observed = datetime(2026, 9, 17, 23, 14, tzinfo=CN)
    runner.write_json(tmp_path / "request.json", {"observed_at": observed.isoformat()})
    clock_calls = []

    def context(selected_market, selected_code, at, frequency, *_args, **kwargs):
        clock_calls.append((selected_market, selected_code, at, frequency, kwargs))
        return {"frequency": frequency, "cutoff": {"1m": 1014, "5m": 1010, "30m": 1000}[frequency]}

    monkeypatch.setattr(markets, "market_context", context)
    captured = {}

    def scan(stock, options, contexts, *_args, **_kwargs):
        captured.update(contexts)
        return {"code": stock["code"], "rows": []}

    monkeypatch.setattr(runner, "scan_symbol", scan)
    runner._scan_worker_symbol({"market": market, "code": code, "name": "fixture"},
                               settings, {}, tmp_path, "revision", {"observed_at": observed.isoformat()})
    assert set(captured) == {"1m", "5m", "30m"}
    assert captured["1m"]["cutoff"] == 1014
    assert captured["5m"]["cutoff"] == 1010
    assert captured["30m"]["cutoff"] == 1000
    assert all(call[2] == observed and not call[4].get("completed_session", False) for call in clock_calls)


def test_run_status_preserves_actual_cutoffs_for_every_market_and_period(tmp_path, monkeypatch):
    from cl_app.services.screening import validate_settings
    settings = validate_settings({"scope": "watchlist", "frequencies": ["1m", "5m", "30m"]})
    runner.write_json(tmp_path / "request.json", {"settings": settings,
                                                 "observed_at": "2026-09-17T23:14:00+08:00"})
    stock = {"market": "us", "code": "TSLA.US", "name": "Tesla"}
    monkeypatch.setattr(runner, "bounded_catalog", lambda *_: iter([{
        "stocks": [stock], "excluded_requested_codes": [],
    }]))
    rows = [{"market": "us", "frequency": frequency, "source_closed_at": cutoff,
             "selected": [], "data_errors": []}
            for frequency, cutoff in (("1m", 1014), ("5m", 1010), ("30m", 1000))]
    monkeypatch.setattr(runner, "bounded_scan", lambda *_: iter([({**stock, "rows": rows}, [])]))
    runner.run_screening(tmp_path)
    import json
    state = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    assert state["market_clock_symbols"] == {"us": "TSLA.US"}
    assert state["market_cutoffs"] == {"us": {"1m": 1014, "5m": 1010, "30m": 1000}}


def test_monitor_reuses_verified_catalog_and_preserves_holding_priority(tmp_path,monkeypatch):
    from cl_app.services.screening import validate_settings
    symbols=[{'market':'us','code':'TSLA.US','name':'held','origin':'watchlist'},
             {'market':'a','code':'SH.600000','name':'other'}]
    settings=validate_settings({'scope':'symbols','symbols':symbols,'frequencies':['5m']})
    runner.write_json(tmp_path/'request.json',{'settings':settings,'observed_at':'2026-09-17T23:14:00+08:00',
                      'holding_snapshot':{'schema':'monitor-holdings-v1','keys':['us:TSLA.US']}})
    monkeypatch.setattr(runner,'bounded_catalog',lambda *_:pytest.fail('verified monitor scope must not start another catalog process'))
    captured=[]
    def scan(stocks,*args):
        captured.extend(stocks)
        return iter([({**stock,'rows':[{'frequency':'5m','selected':[],'data_errors':[]}]},[]) for stock in stocks])
    monkeypatch.setattr(runner,'bounded_scan',scan)
    runner.run_screening(tmp_path)
    assert [s['code'] for s in captured]==['TSLA.US','SH.600000']
    assert [s['holding'] for s in captured]==[True,False]


def test_calendar_cache_reuses_symbols_but_never_shares_mutable_history():
    markets._market_context.cache_clear()
    observed = datetime(2026, 9, 17, 23, 14, 2, tzinfo=CN)
    first = markets.market_context("us", "TSLA.US", observed, "5m")
    expected_days = first["trading_days"][:]
    expected_periods = first["session_periods"][:]
    first["trading_days"].clear()
    first["session_periods"].clear()
    first["cutoff"] = 0
    second = markets.market_context("us", "AAPL.US", observed.replace(second=59), "5m")
    assert markets._market_context.cache_info().misses == 1
    assert second["symbol"] == "AAPL.US"
    assert second["trading_days"] == expected_days
    assert second["session_periods"] == expected_periods
    assert second["cutoff"] == expected_periods[-1][1]


def test_spot_and_contract_share_the_24_hour_clock_without_sharing_identity():
    markets._market_context.cache_clear()
    observed = datetime(2026, 9, 17, 23, 14, tzinfo=CN)
    spot = markets.market_context("currency_spot", "BTC/USDT", observed, "1m")
    contract = markets.market_context("currency", "ETH/USDT", observed.astimezone(timezone.utc), "1m")
    assert markets._market_context.cache_info().misses == 1
    assert spot["cutoff"] == contract["cutoff"]
    assert spot["market"] == "currency_spot" and contract["market"] == "currency"
    assert spot["symbol"] == "BTC/USDT" and contract["symbol"] == "ETH/USDT"


@pytest.mark.parametrize("market,code,frequency,observed,expected", [
    ("us", "TSLA.US", "1m", "2026-09-17T21:30:59+08:00", "2026-09-17T04:00:00+08:00"),
    ("us", "TSLA.US", "1m", "2026-09-17T21:31:00+08:00", "2026-09-17T21:31:00+08:00"),
    ("us", "TSLA.US", "5m", "2026-09-17T21:34:59+08:00", "2026-09-17T04:00:00+08:00"),
    ("us", "TSLA.US", "5m", "2026-09-17T21:35:00+08:00", "2026-09-17T21:35:00+08:00"),
    ("us", "TSLA.US", "30m", "2026-09-17T21:59:59+08:00", "2026-09-17T04:00:00+08:00"),
    ("us", "TSLA.US", "30m", "2026-09-17T22:00:00+08:00", "2026-09-17T22:00:00+08:00"),
    ("us", "TSLA.US", "5m", "2026-03-06T22:35:00+08:00", "2026-03-06T22:35:00+08:00"),
    ("us", "TSLA.US", "5m", "2026-03-09T21:35:00+08:00", "2026-03-09T21:35:00+08:00"),
    ("us", "TSLA.US", "30m", "2026-11-28T03:00:00+08:00", "2026-11-28T02:00:00+08:00"),
    ("hk", "00700.HK", "1m", "2026-09-17T13:00:59+08:00", "2026-09-17T12:00:00+08:00"),
    ("hk", "00700.HK", "1m", "2026-09-17T13:01:00+08:00", "2026-09-17T13:01:00+08:00"),
    ("hk", "00700.HK", "30m", "2026-09-17T12:45:00+08:00", "2026-09-17T12:00:00+08:00"),
    ("currency_spot", "BTC/USDT", "1m", "2026-09-17T08:00:59+08:00", "2026-09-17T08:00:00+08:00"),
    ("currency", "BTC/USDT", "30m", "2026-09-17T08:30:00+08:00", "2026-09-17T08:30:00+08:00"),
    ("a", "SH.600088", "1m", "2026-09-17T09:30:59+08:00", "2026-09-16T15:00:00+08:00"),
    ("a", "SH.600088", "1m", "2026-09-17T09:31:00+08:00", "2026-09-17T09:31:00+08:00"),
    ("a", "SH.600088", "5m", "2026-09-17T13:04:59+08:00", "2026-09-17T11:30:00+08:00"),
    ("a", "SH.600088", "5m", "2026-09-17T13:05:00+08:00", "2026-09-17T13:05:00+08:00"),
])
def test_cached_clock_obeys_session_and_exact_bar_boundaries(market, code, frequency, observed, expected):
    cutoff = markets.closed_bar_cutoff(market, code, datetime.fromisoformat(observed), frequency)
    assert cutoff == int(datetime.fromisoformat(expected).timestamp())
    assert cutoff <= datetime.fromisoformat(observed).timestamp()


def test_a_clock_cache_rechecks_on_the_next_minute(monkeypatch):
    markets._a_cutoff.cache_clear()
    provider = Mock(side_effect=lambda observed, *_args: {"cutoff": int(observed.timestamp())})
    monkeypatch.setattr(markets, "trading_context", provider)
    observed = datetime(2026, 9, 17, 10, 12, tzinfo=CN)
    try:
        first = markets.closed_bar_cutoff("a", "SH.600088", observed, "1m")
        same = markets.closed_bar_cutoff("a", "SZ.000001", observed.replace(second=59), "1m")
        next_minute = markets.closed_bar_cutoff("a", "SZ.000001", observed + timedelta(minutes=1), "1m")
        assert first == same and next_minute == first + 60
        assert provider.call_count == 2
    finally:
        markets._a_cutoff.cache_clear()


@pytest.mark.parametrize("frequency,observed", [
    ("0m", datetime(2026, 9, 17, tzinfo=CN)),
    ("1m", datetime(2026, 9, 17)),
])
def test_cached_clock_rejects_invalid_frequency_or_naive_time(frequency, observed):
    with pytest.raises(ValueError):
        markets.closed_bar_cutoff("us", "TSLA.US", observed, frequency)
    with pytest.raises(ValueError):
        markets.market_context("us", "TSLA.US", observed, frequency)


def test_holdings_source_reads_only_the_explicit_manual_holding_group(monkeypatch):
    watch = Mock()
    watch.zx_stocks.return_value = [{"market": "us", "code": "TSLA.US", "name": "Tesla"}]
    monkeypatch.setattr("chanlun.zixuan.ZiXuan", lambda _: watch)
    assert markets.holding_symbols() == [{"market": "us", "code": "TSLA.US", "name": "Tesla", "origin": "watchlist"}]
    watch.zx_stocks.assert_called_once_with("我的持仓")


@pytest.mark.parametrize("holding_market,expected", [("a", True), ("us", False)])
def test_catalog_marks_only_matching_market_and_code_as_held(tmp_path, monkeypatch, holding_market, expected):
    source = Mock()
    source.all_stocks.return_value = [{"code": "SH.600088", "name": "A", "type": "stock_cn"}]
    monkeypatch.setattr(runner, "_exchange", lambda: source)
    monkeypatch.setattr(markets, "holding_symbols", lambda: [{"market": holding_market, "code": "SH.600088"}])
    connection = Mock()
    runner._catalog_worker(connection, {"scope": "all_a", "exclude_st": True}, tmp_path)
    stock, = connection.send.call_args.args[0]["stocks"]
    assert bool(stock.get("holding")) is expected
