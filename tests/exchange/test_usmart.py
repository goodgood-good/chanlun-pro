from __future__ import annotations

import base64
import json
from typing import Any, Mapping

import pandas as pd
import pytest
from Crypto.Cipher import PKCS1_v1_5
from Crypto.Hash import MD5
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

from chanlun import config
from chanlun.exchange.exchange_usmart import ExchangeUSmart, USmartClient
from chanlun.market import Market


class _Response:
    def __init__(self, payload: Mapping[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _Session:
    def __init__(self, *responses: _Response):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, data, headers, timeout):
        self.calls.append(
            {"url": url, "data": data, "headers": headers, "timeout": timeout}
        )
        return self.responses.pop(0)


class _QuoteClient:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def quote(self, endpoint, payload):
        self.calls.append((endpoint, payload))
        return self.handler(endpoint, payload)


@pytest.fixture(scope="module")
def rsa_keys():
    key = RSA.generate(1024)
    public_der = key.public_key().export_key(format="DER")
    private_der = key.export_key(format="DER", pkcs=8)
    return {
        "key": key,
        "public": base64.b64encode(public_der).decode("ascii"),
        "private": base64.b64encode(private_der).decode("ascii"),
    }


def _verify_signature(key, content: str, encoded: str, *, urlsafe: bool):
    decoder = base64.urlsafe_b64decode if urlsafe else base64.b64decode
    signature = decoder(encoded.encode("ascii"))
    pkcs1_15.new(key.public_key()).verify(MD5.new(content.encode("utf-8")), signature)


def test_usmart_kline_timestamp_contract_is_explicitly_end_labelled():
    assert ExchangeUSmart.kline_time_label == "end"


def test_quote_request_uses_exact_signed_body_and_required_headers(rsa_keys):
    session = _Session(_Response({"code": 0, "msg": "success", "data": {"status": 7}}))
    client = USmartClient(
        channel="test-channel",
        private_key=rsa_keys["private"],
        public_key=rsa_keys["public"],
        token="test-token",
        quote_host="https://quote.example",
        session=session,
    )

    assert client.quote("marketstate", {"market": "hk"}) == {"status": 7}

    call = session.calls[0]
    body = call["data"].decode("utf-8")
    headers = call["headers"]
    assert call["url"] == (
        "https://quote.example/quotes-openservice/api/v1/marketstate"
    )
    assert json.loads(body) == {"market": "hk"}
    assert headers["Authorization"] == "test-token"
    assert headers["X-Channel"] == "test-channel"
    assert headers["X-Request-Id"].isdigit()
    assert len(headers["X-Request-Id"]) == 19
    row_content = (
        headers["Authorization"]
        + headers["X-Channel"]
        + headers["X-Lang"]
        + headers["X-Request-Id"]
        + headers["X-Time"]
        + body
    )
    _verify_signature(
        rsa_keys["key"], row_content, headers["X-Sign"], urlsafe=True
    )


def test_automatic_login_encrypts_credentials_and_caches_token(rsa_keys):
    session = _Session(
        _Response({"code": 0, "msg": "success", "data": {"token": "fresh-token"}})
    )
    client = USmartClient(
        channel="test-channel",
        private_key=rsa_keys["private"],
        public_key=rsa_keys["public"],
        phone="13000000000",
        login_password="not-a-real-password",
        area_code="86",
        trade_host="https://trade.example",
        session=session,
    )

    assert client.login() == "fresh-token"
    assert client.login() == "fresh-token"
    assert len(session.calls) == 1

    call = session.calls[0]
    body = call["data"].decode("utf-8")
    payload = json.loads(body)
    decryptor = PKCS1_v1_5.new(rsa_keys["key"])
    phone = decryptor.decrypt(
        base64.urlsafe_b64decode(payload["phoneNumber"]), b"decrypt-error"
    ).decode("utf-8")
    password = decryptor.decrypt(
        base64.urlsafe_b64decode(payload["password"]), b"decrypt-error"
    ).decode("utf-8")
    assert phone == "13000000000"
    assert password == "not-a-real-password"
    assert phone not in body
    assert password not in body
    assert call["url"] == "https://trade.example/user-server-sg/open-api/login"
    assert "Authorization" not in call["headers"]
    assert len(call["headers"]["X-Request-Id"]) == 30
    _verify_signature(
        rsa_keys["key"], body, call["headers"]["X-Sign"], urlsafe=False
    )


def test_a_share_stock_list_merges_sh_sz_and_is_cached():
    def handler(endpoint, payload):
        assert endpoint == "basicinfo"
        if payload["market"] == "sh":
            return {
                "list": [
                    {
                        "market": "sh",
                        "symbol": "600000",
                        "nameChs": "浦发银行",
                        "type1": 1,
                        "lotSize": 100,
                    },
                    {
                        "market": "sh",
                        "symbol": "510300",
                        "nameChs": "沪深300ETF",
                        "type1": 2,
                        "lotSize": 100,
                    },
                ]
            }
        return {
            "list": [
                {
                    "market": "sz",
                    "symbol": "000001",
                    "nameChs": "平安银行",
                    "type1": 1,
                    "lotSize": 100,
                }
            ]
        }

    client = _QuoteClient(handler)
    exchange = ExchangeUSmart("a", client=client)

    stocks = exchange.all_stocks()
    assert [item["code"] for item in stocks] == [
        "SH.510300",
        "SH.600000",
        "SZ.000001",
    ]
    assert exchange.stock_info("SZ.000001")["name"] == "平安银行"
    assert exchange.stock_info("SH.510300")["type"] == "etf_cn"
    assert exchange.all_stocks() is stocks
    assert [payload for _, payload in client.calls] == [
        {"market": "sh"},
        {"market": "sz"},
    ]


def test_kline_paginates_backwards_filters_range_and_attaches_price_basis():
    def bar(timestamp, price):
        return {
            "latestTime": timestamp,
            "open": price + 0.001,
            "high": price + 1.009,
            "low": price - 1.001,
            "close": price + 0.006,
            "volume": 100,
        }

    def handler(endpoint, payload):
        assert endpoint == "kline"
        if payload["start"] == 0:
            return {
                "list": [
                    bar(20240104000000000, 10),
                    bar(20240103000000000, 9),
                ]
            }
        return {
            "list": [
                bar(20240103000000000, 9),
                bar(20240102000000000, 8),
                bar(20240101000000000, 7),
            ]
        }

    client = _QuoteClient(handler)
    exchange = ExchangeUSmart("a", client=client)
    frame = exchange.klines(
        "SH.600000",
        "d",
        start_date="2024-01-02",
        args={"count": 2, "pages": 5, "right": "none"},
    )

    assert list(frame["date"].dt.strftime("%Y-%m-%d %H:%M")) == [
        "2024-01-02 15:00",
        "2024-01-03 15:00",
        "2024-01-04 15:00",
    ]
    assert list(frame["close"]) == [8.01, 9.01, 10.01]
    assert frame["date"].dt.tz.zone == "Asia/Shanghai"
    assert frame.attrs["price_basis_provider"] == "usmart"
    assert frame.attrs["price_basis_adjustment"] == "none"
    assert client.calls[0][1] == {
        "secuId": "sh600000",
        "type": 7,
        "start": 0,
        "right": 0,
        "count": 2,
    }
    assert client.calls[1][1]["start"] == 20240102235959999


def test_us_kline_uses_configured_long_history_backend_without_mixing_sources(
    monkeypatch,
):
    expected = pd.DataFrame(
        {
            "date": [pd.Timestamp("2026-08-06 16:00", tz="US/Eastern")],
            "frequency": ["1m"],
            "code": ["TSLA.US"],
            "open": [320.0],
            "high": [321.0],
            "low": [319.0],
            "close": [320.5],
            "volume": [100],
        }
    )
    expected.attrs["price_basis_provider"] = "longbridge"

    class _HistoryExchange:
        def __init__(self):
            self.calls = []

        def support_frequencys(self):
            return {"1m": "1 Min"}

        def klines(self, code, frequency, start_date=None, end_date=None, args=None):
            self.calls.append(
                (code, frequency, start_date, end_date, dict(args or {}))
            )
            return expected

    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "longbridge")
    quote_client = _QuoteClient(
        lambda *_: pytest.fail("uSMART K-line endpoint must not be mixed in")
    )
    history = _HistoryExchange()
    exchange = ExchangeUSmart(
        "us",
        client=quote_client,
        history_exchange=history,
    )

    actual = exchange.klines(
        "TSLA.US",
        "1m",
        start_date="2026-07-07 00:00:00",
        end_date="2026-08-06 23:59:59",
        args={"right": "qfq", "count": 1000},
    )

    assert actual is expected
    assert actual.attrs["price_basis_provider"] == "longbridge"
    assert history.calls == [
        (
            "TSLA.US",
            "1m",
            "2026-07-07 00:00:00",
            "2026-08-06 23:59:59",
            {"right": "qfq", "count": 1000},
        )
    ]
    assert quote_client.calls == []


def test_realtime_ticks_preserve_callers_codes_and_calculate_rate():
    def handler(endpoint, payload):
        assert endpoint == "realtime"
        assert payload == {"secuIds": ["hk00700", "hk00005"]}
        return {
            "list": [
                {
                    "market": "hk",
                    "symbol": "00700",
                    "latestPrice": 110,
                    "preClose": 100,
                    "bidPrice": 109.8,
                    "askPrice": 110.2,
                    "open": 101,
                    "high": 111,
                    "low": 99,
                    "volume": 1234,
                },
                {
                    "market": "hk",
                    "symbol": "00005",
                    "latestPrice": 50,
                    "preClose": 0,
                },
            ]
        }

    exchange = ExchangeUSmart("hk", client=_QuoteClient(handler))
    ticks = exchange.ticks(["KH.00700", "00005.HK"])

    assert set(ticks) == {"KH.00700", "00005.HK"}
    assert ticks["KH.00700"].rate == 10.0
    assert ticks["KH.00700"].buy1 == 109.8
    assert ticks["00005.HK"].rate == 0


@pytest.mark.parametrize(
    ("market", "code", "secu_id"),
    [
        ("a", "SH.600000", "sh600000"),
        ("a", "000001.SZ", "sz000001"),
        ("hk", "KH.00700", "hk00700"),
        ("hk", "00700.HK", "hk00700"),
        ("us", "BRK.B.US", "usBRK.B"),
        ("us", "AAPL", "usAAPL"),
    ],
)
def test_symbol_mapping_accepts_project_and_provider_variants(market, code, secu_id):
    exchange = ExchangeUSmart(market, client=_QuoteClient(lambda *_: {}))
    assert exchange._to_secu_id(code) == secu_id


def test_market_state_maps_open_closed_and_failure_to_three_states():
    open_exchange = ExchangeUSmart(
        "hk", client=_QuoteClient(lambda *_: {"status": 4})
    )
    closed_exchange = ExchangeUSmart(
        "hk", client=_QuoteClient(lambda *_: {"status": 7})
    )

    assert open_exchange.now_trading("us") is True
    assert closed_exchange.now_trading("us") is False


def test_factory_builds_independent_usmart_adapter_for_each_market(monkeypatch):
    import chanlun.exchange as exchange_module

    monkeypatch.setattr(exchange_module, "g_exchange_obj", {})
    monkeypatch.setattr(exchange_module.config, "EXCHANGE_A", "usmart")
    monkeypatch.setattr(exchange_module.config, "EXCHANGE_HK", "usmart")
    monkeypatch.setattr(exchange_module.config, "EXCHANGE_US", "usmart")

    a_exchange = exchange_module.get_exchange(Market.A)
    hk_exchange = exchange_module.get_exchange(Market.HK)
    us_exchange = exchange_module.get_exchange(Market.US)

    assert isinstance(a_exchange, ExchangeUSmart)
    assert isinstance(hk_exchange, ExchangeUSmart)
    assert isinstance(us_exchange, ExchangeUSmart)
    assert (a_exchange.market, hk_exchange.market, us_exchange.market) == (
        "a",
        "hk",
        "us",
    )
    assert len({id(a_exchange), id(hk_exchange), id(us_exchange)}) == 3


def test_missing_auth_configuration_fails_without_making_a_request():
    session = _Session()
    client = USmartClient(
        channel="",
        private_key="",
        public_key="",
        token="",
        phone="",
        login_password="",
        session=session,
    )

    with pytest.raises(RuntimeError, match="USMART_CHANNEL"):
        client.quote("marketstate", {"market": "hk"})
    assert session.calls == []


def test_end_date_cursor_and_filter_include_the_whole_calendar_day(monkeypatch):
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "usmart")
    def handler(endpoint, payload):
        assert endpoint == "kline"
        return {
            "list": [
                {
                    "latestTime": 20240102160000000,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 1,
                }
            ]
        }

    client = _QuoteClient(handler)
    exchange = ExchangeUSmart("us", client=client)
    frame = exchange.klines(
        "AAPL.US",
        "d",
        start_date="2024-01-02",
        end_date="2024-01-02",
        args={"pages": 1},
    )

    assert len(frame) == 1
    assert frame.iloc[0]["date"] == pd.Timestamp(
        "2024-01-02 16:00:00", tz="US/Eastern"
    )
    assert client.calls[0][1]["start"] == 20240102235959999


def test_us_intraday_bounds_follow_project_shanghai_time_contract(monkeypatch):
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "usmart")
    def handler(endpoint, payload):
        assert endpoint == "kline"
        return {
            "list": [
                {
                    "latestTime": 20240102160000000,
                    "open": 10,
                    "high": 11,
                    "low": 9,
                    "close": 10.5,
                    "volume": 1,
                }
            ]
        }

    client = _QuoteClient(handler)
    exchange = ExchangeUSmart("us", client=client)
    frame = exchange.klines(
        "AAPL.US",
        "1m",
        # 冬令时美东比上海慢 13 小时：上海 05:00 对应前一日美东 16:00。
        start_date="2024-01-03 05:00:00",
        end_date="2024-01-03 05:00:00",
        args={"pages": 1},
    )

    assert len(frame) == 1
    assert frame.iloc[0]["date"] == pd.Timestamp(
        "2024-01-02 16:00:00", tz="US/Eastern"
    )
    assert client.calls[0][1]["start"] == 20240102160000000
