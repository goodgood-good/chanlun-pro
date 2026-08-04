from __future__ import annotations

import pytest

from chanlun.exchange import exchange_qmt
from chanlun.exchange.exchange_qmt import ExchangeQMT


def test_qmt_screening_instrument_types_distinguish_stock_etf_and_index(
    monkeypatch,
) -> None:
    native = {
        "600000.SH": {"stock": True},
        "510300.SH": {"etf": True, "fund": True},
        "000001.SH": {"index": True},
        "160000.SZ": {"fund": True},
    }
    calls: list[str] = []

    def instrument_type(code: str):
        calls.append(code)
        return native[code]

    monkeypatch.setattr(exchange_qmt.xtdata, "get_instrument_type", instrument_type)

    result = ExchangeQMT().screening_instrument_types(
        ("SH.600000", "SH.510300", "SH.000001", "SZ.160000")
    )

    assert result == {
        "SH.600000": "stock_cn",
        "SH.510300": "etf_cn",
        "SH.000001": "index_cn",
        "SZ.160000": "fund_cn",
    }
    assert calls == ["600000.SH", "510300.SH", "000001.SH", "160000.SZ"]


def test_qmt_screening_instrument_types_fail_closed_on_unresolved_native_type(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        exchange_qmt.xtdata,
        "get_instrument_type",
        lambda _code: {},
    )

    assert ExchangeQMT().screening_instrument_types(("SH.600000",)) == {
        "SH.600000": "unresolved_cn"
    }


@pytest.mark.parametrize(
    "codes",
    (
        ["SH.600000"],
        ("SH.600000", "SH.600000"),
        ("000001.SH",),
    ),
)
def test_qmt_screening_instrument_types_reuses_exact_research_code_contract(
    codes,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ExchangeQMT().screening_instrument_types(codes)
