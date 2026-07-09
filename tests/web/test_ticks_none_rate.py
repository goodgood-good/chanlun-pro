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