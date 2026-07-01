"""锁定 /tv/quotes(TradingView UDF 行情接口, 自选组实时报价来源)。

此前后端缺该端点 → 前端 getQuotes 每次 404 → 自选组行情不自动更新。
本测试用 test_client 打端点 + monkeypatch ex.ticks(不连真实数据源), 验证:
- 正常标的返回 UDF 格式 {s:ok, n, v:{lp,chp,ch,prev_close_price,...}};
- 非法 symbol / 取数失败 优雅降级为 {s:error};
- 多 market 分组、空 symbols 边界。
"""
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pytest  # noqa: E402

from cl_app import create_app  # noqa: E402
from cl_app.blueprints import tv as tv_mod  # noqa: E402
from chanlun.exchange.exchange import Tick  # noqa: E402


@pytest.fixture
def client():
    app = create_app()
    app.config["LOGIN_DISABLED"] = True
    app.config["TESTING"] = True
    return app.test_client()


def _tick(code, last, rate):
    return Tick(
        code=code, last=last, buy1=last, sell1=last,
        high=last, low=last, open=last, volume=100.0, rate=rate,
    )


class _FakeEx:
    def __init__(self, rate=1.5):
        self._rate = rate

    def ticks(self, codes):
        return {c: _tick(c, 2.0, self._rate) for c in codes}


def test_tv_quotes_returns_udf_format(client, monkeypatch):
    monkeypatch.setattr(tv_mod, "get_exchange", lambda m: _FakeEx(rate=1.5))
    j = client.get("/tv/quotes?symbols=a:SH.513100,a:SZ.000001").get_json()
    assert j["s"] == "ok"
    assert len(j["d"]) == 2
    q = {it["n"]: it for it in j["d"]}
    assert q["a:SH.513100"]["s"] == "ok"
    v = q["a:SH.513100"]["v"]
    assert v["lp"] == 2.0
    assert v["chp"] == 1.5
    # prev_close = 2.0 / 1.015；ch = last - prev_close
    assert abs(v["prev_close_price"] - 2.0 / 1.015) < 1e-3
    assert abs(v["ch"] - (2.0 - 2.0 / 1.015)) < 1e-3


def test_tv_quotes_invalid_symbol_is_error(client, monkeypatch):
    monkeypatch.setattr(tv_mod, "get_exchange", lambda m: _FakeEx())
    j = client.get("/tv/quotes?symbols=garbage_no_colon").get_json()
    assert j["s"] == "ok"
    assert j["d"] == [{"s": "error", "n": "garbage_no_colon", "v": {}}]


def test_tv_quotes_ticks_failure_degrades_per_market(client, monkeypatch):
    def _boom(_m):
        class _E:
            def ticks(self, codes):
                raise RuntimeError("data source down")
        return _E()
    monkeypatch.setattr(tv_mod, "get_exchange", _boom)
    j = client.get("/tv/quotes?symbols=a:SH.513100").get_json()
    assert j["s"] == "ok"
    assert j["d"][0]["s"] == "error"
    assert j["d"][0]["n"] == "a:SH.513100"


def test_tv_quotes_empty_symbols(client):
    j = client.get("/tv/quotes?symbols=").get_json()
    assert j == {"s": "ok", "d": []}
