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
    ex = exchange_qmt.ExchangeQMT()

    result = ex.all_stocks(full_market_authorized=True)

    assert ex.all_stocks_requires_explicit_authorization is True
    assert result == []
    assert native_calls == [("SH", "SZ", "BJ")]


def test_retired_qmt_full_market_entrypoints_stay_removed():
    assert not hasattr(exchange_qmt.ExchangeQMT, "all_ticks")
    assert not (ROOT / "script/crontab/reboot_sync_a_klines.py").exists()
