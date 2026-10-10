"""Unverified product sessions cannot mint cutoffs or imply market closure."""
from datetime import datetime
from unittest.mock import Mock

import pytest

from chanlun.screening import markets, runner
from chanlun.screening.errors import MarketDataContractError
from chanlun.screening.rules import CN


@pytest.mark.parametrize("market,code", [
    ("ny_futures", "CO.GC00W"), ("ny_futures", "NY.CL00W"),
    ("ny_futures", "CO.UNKNOWN"), ("fx", "FE.CNYUSD"), ("fx", "FE.USDCNY"),
])
@pytest.mark.parametrize("observed", [
    datetime(2026, 10, 9, 20, tzinfo=CN),
    datetime(2026, 10, 10, 11, tzinfo=CN),
    datetime(2026, 11, 2, 8, tzinfo=CN),
])
def test_missing_product_contract_never_falls_back_to_generic_calendar(monkeypatch, market, code, observed):
    calendar = Mock(side_effect=AssertionError("unverified product cannot select a calendar"))
    monkeypatch.setattr(markets, "_calendar", calendar)
    for frequency in ("1m", "5m", "30m"):
        with pytest.raises(MarketDataContractError) as error:
            markets.market_context(market, code, observed, frequency)
        assert error.value.reason == "SESSION_CALENDAR_UNAVAILABLE"
        with pytest.raises(MarketDataContractError):
            markets.closed_bar_cutoff(market, code, observed, frequency)
    calendar.assert_not_called()


@pytest.mark.parametrize("code", ["CO.GC00W", "NY.CL00W"])
def test_worker_keeps_symbol_and_period_diagnostics_without_fetching_history(tmp_path, monkeypatch, code):
    fetch = Mock(side_effect=AssertionError("unverified clocks must not fetch structural input"))
    monkeypatch.setattr(runner, "fetch_frame", fetch)
    stock = {"market": "ny_futures", "code": code, "name": "recorded watchlist symbol"}
    result = runner._scan_worker_symbol(stock, {
        "scope": "symbols", "frequencies": ["1m", "5m"],
        "recent_sessions": 5, "max_anchor_sessions": 20,
    }, {}, tmp_path, "test", {"observed_at": "2026-10-10T11:00:00+08:00"})
    assert result["code"] == code and result["market"] == "ny_futures"
    assert [row["frequency"] for row in result["rows"]] == ["1m", "5m"]
    assert all(row["data_errors"] == ["SESSION_CALENDAR_UNAVAILABLE"] for row in result["rows"])
    assert all(row["selected"] == [] for row in result["rows"])
    fetch.assert_not_called()


def test_unknown_ny_market_state_preserves_chart_refresh_without_claiming_open():
    from chanlun.exchange.exchange_tdx_ny_futures import ExchangeTDXNYFutures
    from chanlun.exchange.exchange_tdx_fx import ExchangeTDXFX
    from cl_app.services.chart_market_state import ChartMarketStateCache
    ny = ExchangeTDXNYFutures.__wrapped__
    fx = ExchangeTDXFX.__wrapped__
    # Unbound methods do not construct a provider or connect to a vendor.
    assert ny.now_trading(None, "ny_futures") is None
    assert fx.now_trading(None, "fx") is None
    assert ny.screening_time_contract_verified is False
    cache = ChartMarketStateCache()
    try:
        assert cache.read("ny_futures", lambda: ny.now_trading(None, "ny_futures")) is True
    finally:
        cache.shutdown()
