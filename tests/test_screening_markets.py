"""Cross-market identity, calendar gaps, closed sessions and watchlist scope."""

from datetime import datetime, timezone
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
    snapshot = {"levels": []}
    monkeypatch.setattr("chanlun.cl_utils.tv_chart.cl_data_to_tv_chart", lambda *args, **kwargs: {
        "strict_structure_mode": "replace", "strict_structure": snapshot})
    frame = pd.DataFrame({"date": [pd.Timestamp("2026-09-17T15:00:00+08:00")]})
    assert runner.snapshot_for(frame, "SH.600088", "5m")["screening_segments"] == []
