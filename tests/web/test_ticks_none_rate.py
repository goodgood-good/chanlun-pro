"""R4-C6: /ticks 单个 None-rate 标的不得清空整批自选行情。

other.py 原用单个列表推导 + 外层 try/except: 任一 Tick.rate=None(EXCHANGE_US=ib 透传
redis / currency=binance 的 ccxt percentage 缺省)使 float(None) 抛 TypeError→整批返回
{now_trading:False, ticks:[]}, 前端收到 now_trading:false 会 stop_timer 停轮询。修复=
逐标的隔离 + `float(_t.rate or 0)`, 镜像 /tv/quotes(tv.py:637)。
"""
import json
import pathlib
import sys

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

import pytest  # noqa: E402

from cl_app import create_app  # noqa: E402
from cl_app.blueprints import other as other_mod  # noqa: E402
from chanlun.exchange.exchange import Tick  # noqa: E402


@pytest.fixture
def client():
    app = create_app()
    app.config["LOGIN_DISABLED"] = True
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = False  # POST 测试免 CSRF token
    return app.test_client()


def _tick(code, last, rate):
    return Tick(
        code=code, last=last, buy1=last, sell1=last,
        high=last, low=last, open=last, volume=100.0, rate=rate,
    )


class _FakeEx:
    def __init__(self, ticks_map):
        self._ticks = ticks_map

    def ticks(self, codes):
        return {c: self._ticks[c] for c in codes if c in self._ticks}

    def now_trading(self):
        return True


def test_ticks_none_rate_does_not_empty_batch(client, monkeypatch):
    ticks_map = {
        "SH.600000": _tick("SH.600000", 2.0, None),  # rate=None(ib/binance 场景)
        "SZ.000001": _tick("SZ.000001", 3.0, 1.5),   # 健康
    }
    monkeypatch.setattr(other_mod, "get_exchange", lambda m: _FakeEx(ticks_map))
    resp = client.post(
        "/ticks",
        data={"market": "a", "codes": json.dumps(["SH.600000", "SZ.000001"])},
    )
    j = resp.get_json()
    # 旧代码 float(None) 崩 → {now_trading:False, ticks:[], errmsg:internal_error}
    assert "errmsg" not in j
    assert j["now_trading"] is True
    out = {t["code"]: t for t in j["ticks"]}
    assert len(out) == 2  # 整批未被一个坏 tick 清空
    assert out["SZ.000001"]["rate"] == 1.5  # 健康标的正常
    assert out["SH.600000"]["rate"] == 0.0  # None rate 守零


def test_ticks_healthy_batch_unaffected(client, monkeypatch):
    ticks_map = {"SZ.000001": _tick("SZ.000001", 3.0, 2.5)}
    monkeypatch.setattr(other_mod, "get_exchange", lambda m: _FakeEx(ticks_map))
    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )
    j = resp.get_json()
    assert "errmsg" not in j
    assert j["ticks"][0]["rate"] == 2.5

# ============================================================================
# 终检R14-C1 (MED): /ticks 对 Tick.last/rate 缺 math.isfinite 校验, NaN 使整批响应
# 变非法 JSON(裸 NaN token 打断前端严格 JSON.parse → 含健康标的全批刷新失败)。
# 与已修的 /tv/quotes(tv.py:654)同款, /ticks 是漏补兄弟。R4-C6 只覆盖 rate=None。
# 注: Python json.loads 默认接受 NaN, 故 RED 断言查原始响应体是否含裸 NaN token。
# ============================================================================


def test_ticks_nan_rate_does_not_produce_invalid_json(client, monkeypatch):
    ticks_map = {
        "SH.513100": _tick("SH.513100", 2.0, float("nan")),  # rate=NaN
        "SZ.000001": _tick("SZ.000001", 2.0, 1.5),           # 健康
    }
    monkeypatch.setattr(other_mod, "get_exchange", lambda m: _FakeEx(ticks_map))
    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SH.513100", "SZ.000001"])}
    )
    body = resp.get_data(as_text=True)
    assert "NaN" not in body, f"响应体含非法 NaN token(打断前端 JSON.parse): {body}"
    j = json.loads(body)
    out = {t["code"]: t for t in j["ticks"]}
    assert "SZ.000001" in out and out["SZ.000001"]["rate"] == 1.5  # 健康标的不受坏 tick 污染


def test_ticks_nan_last_does_not_produce_invalid_json(client, monkeypatch):
    ticks_map = {
        "SH.513100": _tick("SH.513100", float("nan"), 1.0),  # last=NaN
        "SZ.000001": _tick("SZ.000001", 2.0, 1.5),
    }
    monkeypatch.setattr(other_mod, "get_exchange", lambda m: _FakeEx(ticks_map))
    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SH.513100", "SZ.000001"])}
    )
    body = resp.get_data(as_text=True)
    assert "NaN" not in body, f"响应体含非法 NaN token(打断前端 JSON.parse): {body}"
    j = json.loads(body)
    out = {t["code"]: t for t in j["ticks"]}
    assert "SZ.000001" in out and out["SZ.000001"]["price"] == 2.0