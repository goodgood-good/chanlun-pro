"""Formal metadata search and US readiness stay independent of strategy scans."""

from flask import Flask

from cl_app import create_app
from cl_app.blueprints import tv
from cl_app.services import stock_list, constants


def test_full_catalog_searches_us_and_a_names_beyond_validation_cohort(monkeypatch):
    for name in ("stock_cache", "_symbol_states", "_disk_cache_metadata"):
        monkeypatch.setattr(stock_list, name, {})
    for name in (
        "_symbol_catalog_mode",
        "_symbol_catalog_codes_by_exchange",
        "_full_catalog_refresh_authorized",
    ):
        monkeypatch.setattr(stock_list, name, getattr(stock_list, name))
    stock_list.configure_symbol_catalog(full_catalog_authorized=True)
    rows = {
        "a": [{"code": "SH.600519", "name": "贵州茅台", "type": "stock_cn"}],
        "us": [
            {"code": "AAPL.US", "name": "苹果"},
            {"code": "MSFT.US", "name": "微软"},
        ],
    }
    calls = []

    class Provider:
        def __init__(self, market):
            self.market = market

        def all_stocks(self):
            calls.append(self.market)
            return rows[self.market]

    monkeypatch.setattr(
        stock_list, "get_exchange", lambda market: Provider(market.value)
    )
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *args: None)
    for market in rows:
        stock_list._preload_single_exchange(market)
    monkeypatch.setattr(
        tv, "market_frequencys", {"a": ["1m", "5m", "30m"], "us": ["1m", "5m", "30m"]}
    )
    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True)
    app.register_blueprint(tv.tv_bp)
    for market, query, expected in [
        ("a", "贵州茅台", "SH.600519"),
        ("us", "AAPL", "AAPL.US"),
        ("us", "微软", "MSFT.US"),
    ]:
        response = app.test_client().get(
            "/tv/search", query_string={"exchange": market, "query": query}
        )
        assert response.status_code == 200
        assert response.json[0]["symbol"] == expected
    assert calls == ["a", "us"]


def test_us_readiness_exposes_its_own_failure_without_provider_calls(monkeypatch):
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "READINESS_MARKETS": "a,us",
        }
    )
    for mapping in (constants.market_frequencys, constants.market_default_codes):
        monkeypatch.setattr(mapping, "status", lambda market: {"ready": True})
    ready = {"a": True, "us": False}
    monkeypatch.setattr(
        stock_list,
        "get_symbol_readiness",
        lambda market: {
            "ready": ready[market],
            "status": "ready" if ready[market] else "degraded",
            "count": 100 if ready[market] else 0,
        },
    )
    for market in ("a", "us"):
        app.extensions["readiness"].record_ticks_success(market)
    try:
        assert app.test_client().get("/readyz?market=a").status_code == 200
        failure = app.test_client().get("/readyz?market=us")
        assert (
            failure.status_code == 503
            and "symbols_not_ready" in failure.json["reasons"]
        )
        ready["us"] = True
        assert app.test_client().get("/readyz?market=us").status_code == 200
    finally:
        app.extensions["shutdown_runtime_services"]()


def test_required_us_catalog_cannot_be_ready_when_optional_preload_is_disabled(
    monkeypatch,
):
    monkeypatch.setattr(stock_list, "PRELOAD_EXCHANGES", ["a"])
    monkeypatch.setattr(stock_list, "stock_cache", {})
    monkeypatch.setattr(stock_list, "_symbol_states", {})
    monkeypatch.setattr(stock_list, "_disk_cache_metadata", {})
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "READINESS_MARKETS": "a,us",
            "SYMBOL_CATALOG_FULL_REFRESH_AUTHORIZED": True,
        }
    )
    for mapping in (constants.market_frequencys, constants.market_default_codes):
        monkeypatch.setattr(mapping, "status", lambda market: {"ready": True})
    app.extensions["readiness"].record_ticks_success("us")
    loads = []

    def cached_rows(market):
        loads.append(market)
        return [
            {"code": "AAPL.US" if market == "us" else "SH.600519", "name": "sample"}
        ]

    monkeypatch.setattr(stock_list, "_load_stocks_from_disk", cached_rows)
    monkeypatch.setattr(stock_list, "_save_stocks_to_disk", lambda *args: None)
    try:
        assert stock_list.get_symbol_readiness("us")["status"] == "disabled"
        assert app.test_client().get("/readyz?market=us").status_code == 503
        stock_list.require_preload_markets(app.config["READINESS_MARKETS"])
        stock_list._warm_cache_from_disk()
        assert loads == ["a", "us"]
        assert app.test_client().get("/readyz?market=us").status_code == 200
        assert stock_list.get_symbol_readiness("us")["count"] == 1
    finally:
        app.extensions["shutdown_runtime_services"]()
