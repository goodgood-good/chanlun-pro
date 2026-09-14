from pathlib import Path

import pytest

from chanlun.exchange import exchange_qmt


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "kwargs",
    (
        {},
        {"full_market_authorized": False},
        {"full_market_authorized": 1},
        {"full_market_authorized": "true"},
    ),
)
def test_all_stocks_rejects_without_exact_authorization_before_native_io(
    monkeypatch,
    kwargs,
):
    native_calls = []

    def unexpected_full_tick(codes):
        native_calls.append(tuple(codes))
        raise AssertionError("unauthorized all_stocks reached xtdata")

    monkeypatch.setattr(exchange_qmt.xtdata, "get_full_tick", unexpected_full_tick)
    ex = exchange_qmt.ExchangeQMT()

    with pytest.raises(PermissionError, match="explicit authorization"):
        ex.all_stocks(**kwargs)

    assert native_calls == []


def test_all_stocks_cached_catalog_does_not_bypass_authorization(monkeypatch):
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "get_full_tick",
        lambda _codes: pytest.fail("cached unauthorized read reached xtdata"),
    )
    ex = exchange_qmt.ExchangeQMT()
    ex.g_all_stocks = [{"code": "SH.600000", "name": "Pufa"}]

    with pytest.raises(PermissionError, match="explicit authorization"):
        ex.all_stocks()


def test_all_stocks_exact_authorization_uses_full_market_primitive_once(monkeypatch):
    native_calls = []

    def empty_full_tick(codes):
        native_calls.append(tuple(codes))
        return {}

    monkeypatch.setattr(exchange_qmt.xtdata, "get_full_tick", empty_full_tick)
    monkeypatch.setattr(exchange_qmt.xtdata, "get_stock_list_in_sector", lambda _: [])
    ex = exchange_qmt.ExchangeQMT()

    result = ex.all_stocks(full_market_authorized=True)

    assert ex.all_stocks_requires_explicit_authorization is True
    assert result == []
    assert native_calls == [("SH", "SZ", "BJ")]


def test_retired_qmt_full_market_entrypoints_stay_removed():
    assert not hasattr(exchange_qmt.ExchangeQMT, "all_ticks")
    assert not (ROOT / "script/crontab/reboot_sync_a_klines.py").exists()


def test_full_catalog_includes_listed_stocks_without_quotes_and_excludes_bonds(monkeypatch):
    ex = exchange_qmt.ExchangeQMT()
    ex.g_all_stocks = []
    quote_calls, directory_calls = [], []
    def quotes(markets):
        quote_calls.append(markets)
        return {"600000.SH": {}, "510300.SH": {}}
    def directory(sector):
        directory_calls.append(sector)
        return ["600000.SH", "920238.BJ", "000001.SZ", "821023.BJ"]
    kinds = {"600000.SH": {"stock": True}, "510300.SH": {"etf": True},
             "920238.BJ": {"stock": True}, "000001.SZ": {"stock": True}, "821023.BJ": {"bond": True}}
    monkeypatch.setattr(exchange_qmt.xtdata, "get_full_tick", quotes)
    monkeypatch.setattr(exchange_qmt.xtdata, "get_stock_list_in_sector", directory)
    monkeypatch.setattr(exchange_qmt.xtdata, "get_instrument_type", lambda code: kinds[code])
    monkeypatch.setattr(exchange_qmt.xtdata, "get_instrument_detail", lambda code, _: {"InstrumentName": code, "PriceTick": .01})
    with pytest.raises(PermissionError):
        ex.all_stocks()
    assert not quote_calls and not directory_calls
    stocks = ex.all_stocks(full_market_authorized=True)
    assert {s["code"] for s in stocks} == {"SH.600000", "SH.510300", "BJ.920238", "SZ.000001"}
    assert len(stocks) == 4
    assert next(s for s in stocks if s["code"] == "BJ.920238")["type"] == "stock_cn"
    assert ex.all_stocks(full_market_authorized=True) is stocks
    assert quote_calls == [["SH", "SZ", "BJ"]] and directory_calls == ["沪深京A股"]
