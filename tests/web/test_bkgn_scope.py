from flask import Flask

from cl_app.blueprints import bkgn as bkgn_module


class _Catalog:
    def __init__(self, codes):
        self._codes = tuple(codes)

    def get_codes_by_hy(self, _code):
        return self._codes

    def get_codes_by_gn(self, _code):
        return self._codes

    @staticmethod
    def ths_to_tdx_codes(codes):
        return list(codes)


def _request(monkeypatch, codes):
    app = Flask(__name__)
    monkeypatch.setattr(bkgn_module, "StocksBKGN", lambda: _Catalog(codes))
    with app.test_request_context(
        "/a/bkgn_codes",
        method="POST",
        data={"bkgn_type": "hy", "bkgn_code": "test"},
    ):
        return bkgn_module.a_bkgn_codes.__wrapped__()


def test_two_hundred_members_are_rejected_before_exchange_access(monkeypatch):
    exchange_calls = []
    monkeypatch.setattr(
        bkgn_module,
        "get_exchange",
        lambda market: exchange_calls.append(market),
    )
    codes = tuple(f"SH.{600000 + index:06d}" for index in range(200))

    response, status = _request(monkeypatch, codes)

    assert status == 400
    assert response == {
        "code": 1,
        "msg": "板块成员超过普通接口 20 只上限，请提供显式小范围代码",
        "data": {},
        "count": 0,
    }
    assert exchange_calls == []


def test_twenty_members_never_use_catalog_expanding_stock_info(monkeypatch):
    calls = {"stock_info": 0, "all_stocks": 0, "basicinfo": 0}

    class _CatalogExpandingExchange:
        def all_stocks(self):
            calls["all_stocks"] += 1
            return []

        def stock_info(self, _code):
            calls["stock_info"] += 1
            calls["basicinfo"] += 1
            self.all_stocks()
            return None

    monkeypatch.setattr(
        bkgn_module,
        "get_exchange",
        lambda _market: _CatalogExpandingExchange(),
    )
    codes = tuple(f"SH.{600000 + index:06d}" for index in range(20))

    response = _request(monkeypatch, codes)

    assert response["count"] == 20
    assert tuple(response["data"]) == codes
    assert all(response["data"][code]["name"] == code for code in codes)
    assert calls == {"stock_info": 0, "all_stocks": 0, "basicinfo": 0}
