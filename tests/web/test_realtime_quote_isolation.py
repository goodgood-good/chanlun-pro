"""正式模式的 A 股实时行情必须全部经过认证子进程。"""

from __future__ import annotations

import json

import pytest

from chanlun.exchange.exchange import Tick
from cl_app import create_app
from cl_app.blueprints import other as other_mod
from cl_app.blueprints import tv as tv_mod
from cl_app.services.realtime_quotes import (
    AShareDisplayQuoteBatch,
    AShareRealtimeQuote,
    AShareRealtimeQuoteBatch,
)


@pytest.fixture
def isolated_app():
    app = create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": False,
            "TRADING_SCREENING_NATIVE_PROCESS_ISOLATION": True,
        },
    )
    yield app
    app.extensions["shutdown_scheduler"]()


def _batch(codes: tuple[str, ...]) -> AShareRealtimeQuoteBatch:
    return AShareRealtimeQuoteBatch(
        requested_codes=codes,
        market_open=True,
        quotes=tuple(
            AShareRealtimeQuote(
                code=code,
                last=2.0,
                buy1=1.99,
                sell1=2.01,
                high=2.1,
                low=1.9,
                open=1.95,
                volume=100.0,
                rate=1.5,
            )
            for code in codes
        ),
        tick_data_used=bool(codes),
    )


def _closed_display_batch(codes: tuple[str, ...]) -> AShareDisplayQuoteBatch:
    return AShareDisplayQuoteBatch(
        requested_codes=codes,
        market_open=False,
        quotes=tuple(
            AShareRealtimeQuote(
                code=code,
                last=1.672,
                buy1=1.671,
                sell1=1.672,
                high=1.684,
                low=1.655,
                open=1.66,
                volume=1000.0,
                rate=0.72,
            )
            for code in codes
        ),
        tick_data_used=bool(codes),
    )


def _forbid_web_exchange(*_args, **_kwargs):
    raise AssertionError("正式隔离模式不得在 Web 进程内创建 A 股交易所")


def test_a_share_quote_routes_use_only_isolated_provider(
    isolated_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def provider(codes: tuple[str, ...]) -> AShareRealtimeQuoteBatch:
        calls.append(codes)
        return _batch(codes)

    isolated_app.extensions["a_share_realtime_quotes"] = provider
    monkeypatch.setattr(other_mod, "get_exchange", _forbid_web_exchange)
    monkeypatch.setattr(tv_mod, "get_exchange", _forbid_web_exchange)
    client = isolated_app.test_client()

    ticks = client.post(
        "/ticks",
        data={
            "market": "a",
            "codes": json.dumps(["SZ.000001", "SH.600000"]),
        },
    )
    quotes = client.get("/tv/quotes?symbols=a:SZ.000001,a:SH.600000")

    assert ticks.status_code == 200
    assert {row["code"] for row in ticks.get_json()["ticks"]} == {
        "SH.600000",
        "SZ.000001",
    }
    assert quotes.status_code == 200
    assert all(row["s"] == "ok" for row in quotes.get_json()["d"])
    assert calls == [
        ("SH.600000", "SZ.000001"),
        ("SH.600000", "SZ.000001"),
    ]


def test_a_share_quote_routes_render_last_snapshot_while_market_is_closed(
    isolated_app,
) -> None:
    isolated_app.extensions["a_share_realtime_quotes"] = _closed_display_batch
    client = isolated_app.test_client()

    ticks = client.post(
        "/ticks",
        data={"market": "a", "codes": json.dumps(["SH.513100"])},
    )
    quotes = client.get("/tv/quotes?symbols=a:SH.513100")

    assert ticks.status_code == 200
    assert ticks.get_json() == {
        "ok": True,
        "market_state": "closed",
        "now_trading": False,
        "ticks": [{"code": "SH.513100", "price": 1.672, "rate": 0.72}],
        "error": None,
    }
    assert quotes.status_code == 200
    assert quotes.get_json()["d"][0]["v"]["lp"] == 1.672
    assert quotes.get_json()["d"][0]["v"]["chp"] == 0.72


def test_missing_isolated_provider_fails_closed_without_qmt_fallback(
    isolated_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_app.extensions["a_share_realtime_quotes"] = None
    monkeypatch.setattr(other_mod, "get_exchange", _forbid_web_exchange)
    monkeypatch.setattr(tv_mod, "get_exchange", _forbid_web_exchange)
    client = isolated_app.test_client()

    ticks = client.post(
        "/ticks",
        data={"market": "a", "codes": json.dumps(["SZ.000001"])},
    )
    quotes = client.get("/tv/quotes?symbols=a:SZ.000001")

    assert ticks.status_code == 503
    assert ticks.get_json()["error"]["code"] == "service_unavailable"
    assert quotes.status_code == 200
    assert quotes.get_json()["d"] == [{"s": "error", "n": "a:SZ.000001", "v": {}}]


def test_us_ticks_keep_the_external_market_provider_path(
    isolated_app,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_app.extensions["a_share_realtime_quotes"] = lambda _codes: (
        _ for _ in ()
    ).throw(AssertionError("美股不得调用 A 股隔离行情"))

    class USExchange:
        def ticks(self, codes):
            return {
                code: Tick(
                    code=code,
                    last=201.0,
                    buy1=200.9,
                    sell1=201.1,
                    high=202.0,
                    low=199.0,
                    open=200.0,
                    volume=100.0,
                    rate=1.0,
                )
                for code in codes
            }

    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: USExchange())
    monkeypatch.setattr(other_mod, "market_now_trading", lambda _ex, _market: True)

    response = isolated_app.test_client().post(
        "/ticks",
        data={"market": "us", "codes": json.dumps(["AAPL.US"])},
    )

    assert response.status_code == 200
    assert response.get_json()["ticks"] == [
        {"code": "AAPL.US", "price": 201.0, "rate": 1.0}
    ]
