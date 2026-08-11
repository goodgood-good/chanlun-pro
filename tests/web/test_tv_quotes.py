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
from chanlun.market import Market  # noqa: E402


@pytest.fixture
def client():
    app = create_app(test_config={
        "TESTING": True,
        "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
    })
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


def test_tv_quotes_none_ticks_degrades_without_500(client, monkeypatch):
    class _NoneExchange:
        def ticks(self, _codes):
            return None

    monkeypatch.setattr(tv_mod, "get_exchange", lambda _market: _NoneExchange())

    response = client.get("/tv/quotes?symbols=a:SH.513100")

    assert response.status_code == 200
    assert response.get_json() == {
        "s": "ok",
        "d": [{"s": "error", "n": "a:SH.513100", "v": {}}],
    }


def test_tv_quotes_empty_symbols(client):
    j = client.get("/tv/quotes?symbols=").get_json()
    assert j == {"s": "ok", "d": []}


class _NanEx:
    """指定 code 的 tick 带 NaN 字段(last 或 rate), 同 market 其余 code 正常。"""

    def __init__(self, bad_code, nan_field):
        self._bad = bad_code
        self._nan_field = nan_field

    def ticks(self, codes):
        out = {}
        for c in codes:
            if c == self._bad and self._nan_field == "last":
                out[c] = _tick(c, float("nan"), 1.5)
            elif c == self._bad:  # nan_field == "rate"
                out[c] = _tick(c, 2.0, float("nan"))
            else:
                out[c] = _tick(c, 2.0, 1.5)
        return out


@pytest.mark.parametrize("nan_field", ["rate", "last"])
def test_tv_quotes_nan_tick_does_not_poison_whole_response(client, monkeypatch, nan_field):
    # 同一 market 内: 一个标的 tick 字段为 NaN, 另一个正常。NaN 不得污染整批响应。
    monkeypatch.setattr(
        tv_mod, "get_exchange",
        lambda m: _NanEx(bad_code="SH.513100", nan_field=nan_field),
    )
    resp = client.get("/tv/quotes?symbols=a:SH.513100,a:SZ.000001")

    # 关键断言: 裸 NaN token 不是合法 JSON, 前端 JSON.parse 会整体抛错 → 全批更新失败。
    # 直接对原始响应体做字符串检查, 比 get_json()(宽松解析能吞 NaN)更能可靠暴露缺陷。
    body = resp.get_data(as_text=True)
    assert "NaN" not in body

    j = resp.get_json()
    assert j["s"] == "ok"
    q = {it["n"]: it for it in j["d"]}
    # 健康标的不受 NaN 标的牵连, 正常返回数值。
    assert q["a:SZ.000001"]["s"] == "ok"
    assert q["a:SZ.000001"]["v"]["lp"] == 2.0
    assert q["a:SZ.000001"]["v"]["chp"] == 1.5
    # 产生非有限值的标的降级为 error, 而非把 NaN 塞进 JSON。
    assert q["a:SH.513100"]["s"] == "error"
    assert q["a:SH.513100"]["v"] == {}


def test_tv_quotes_conversion_error_is_isolated_per_symbol(client, monkeypatch):
    # 一个标的 tick 的 last 无法 float()(转换抛异常), 同 market 另一标的正常。
    def _ex(_m):
        class _E:
            def ticks(self, codes):
                out = {}
                for c in codes:
                    if c == "SH.513100":
                        out[c] = Tick(
                            code=c, last="boom", buy1=0.0, sell1=0.0,
                            high=2.0, low=2.0, open=2.0, volume=100.0, rate=1.5,
                        )
                    else:
                        out[c] = _tick(c, 2.0, 1.5)
                return out
        return _E()

    monkeypatch.setattr(tv_mod, "get_exchange", _ex)
    resp = client.get("/tv/quotes?symbols=a:SH.513100,a:SZ.000001")
    body = resp.get_data(as_text=True)
    assert "NaN" not in body
    j = resp.get_json()
    assert j["s"] == "ok"
    q = {it["n"]: it for it in j["d"]}
    # 健康标的正常返回。
    assert q["a:SZ.000001"]["s"] == "ok"
    assert q["a:SZ.000001"]["v"]["lp"] == 2.0
    # 转换抛异常的标的被隔离为 error, 不拖垮同批其它标的。
    assert q["a:SH.513100"]["s"] == "error"
    assert q["a:SH.513100"]["v"] == {}


def test_tv_quotes_one_market_failure_does_not_affect_healthy_market(client, monkeypatch):
    # a 市场取数抛异常(整组降级), us 市场健康 —— 验证「一个 market 失败不影响
    # 另一个健康 market」的市场级隔离契约(此前测试名承诺但从未真正验证)。
    def _ex(market):
        if market == Market.A:
            class _Boom:
                def ticks(self, codes):
                    raise RuntimeError("a source down")
            return _Boom()
        return _FakeEx(rate=2.0)

    monkeypatch.setattr(tv_mod, "get_exchange", _ex)
    resp = client.get("/tv/quotes?symbols=a:SH.513100,us:AAPL")
    j = resp.get_json()
    assert j["s"] == "ok"
    q = {it["n"]: it for it in j["d"]}
    # a 市场取数失败 → 该组 error。
    assert q["a:SH.513100"]["s"] == "error"
    assert q["a:SH.513100"]["v"] == {}
    # us 市场健康 → 不受 a 市场故障牵连, 正常返回数值。
    assert q["us:AAPL"]["s"] == "ok"
    assert q["us:AAPL"]["v"]["lp"] == 2.0
    assert q["us:AAPL"]["v"]["chp"] == 2.0
