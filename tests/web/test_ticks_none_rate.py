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

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from cl_app import create_app  # noqa: E402
from cl_app.blueprints import other as other_mod  # noqa: E402
from chanlun.exchange.exchange import Tick  # noqa: E402


@pytest.fixture
def client():
    app = create_app(test_config={
        "TESTING": True,
        "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
        "WTF_CSRF_ENABLED": False,
    })
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

    def now_trading(self, _market=None):
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

def _assert_ticks_error(resp, status_code, error_code):
    assert resp.status_code == status_code
    payload = resp.get_json()
    assert payload["ok"] is False
    assert payload["market_state"] == "unknown"
    assert payload["now_trading"] is None
    assert payload["ticks"] == []
    assert payload["error"]["code"] == error_code
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["message"]
    assert set(payload) == {"ok", "market_state", "now_trading", "ticks", "error"}


def _raise_runtime_error(*_args, **_kwargs):
    raise RuntimeError("dependency unavailable")


@pytest.mark.parametrize(
    ("raw_state", "expected_now_trading", "expected_market_state"),
    [
        (np.bool_(True), True, "open"),
        (np.bool_(False), False, "closed"),
        (None, None, "unknown"),
    ],
    ids=["numpy-open", "numpy-closed", "unknown"],
)
def test_ticks_success_normalizes_market_state(
    client, monkeypatch, raw_state, expected_now_trading, expected_market_state
):
    ticks_map = {"SZ.000001": _tick("SZ.000001", 3.0, 2.5)}
    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _FakeEx(ticks_map))
    monkeypatch.setattr(other_mod, "market_now_trading", lambda _ex, _market: raw_state)

    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload == {
        "ok": True,
        "market_state": expected_market_state,
        "now_trading": expected_now_trading,
        "ticks": [{"code": "SZ.000001", "price": 3.0, "rate": 2.5}],
        "error": None,
    }


def test_ticks_serializes_numpy_float32_as_native_float(client, monkeypatch):
    ticks_map = {
        "SZ.000001": _tick(
            "SZ.000001", np.float32(3.25), np.float32(1.75)
        )
    }
    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _FakeEx(ticks_map))
    monkeypatch.setattr(other_mod, "market_now_trading", lambda _ex, _market: True)

    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )

    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["ticks"] == [
        {"code": "SZ.000001", "price": 3.25, "rate": 1.75}
    ]
    assert type(payload["ticks"][0]["price"]) is float
    assert type(payload["ticks"][0]["rate"]) is float


def test_ticks_rejects_invalid_market_with_400(client):
    resp = client.post(
        "/ticks", data={"market": "invalid", "codes": json.dumps([])}
    )
    _assert_ticks_error(resp, 400, "invalid_market")


def test_ticks_rejects_invalid_codes_json_with_400(client):
    resp = client.post("/ticks", data={"market": "a", "codes": "not-json"})
    _assert_ticks_error(resp, 400, "invalid_codes_json")


def test_ticks_rejects_non_list_codes_with_400(client):
    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps({"code": "SZ.000001"})}
    )
    _assert_ticks_error(resp, 400, "codes_not_list")


def test_ticks_rejects_too_many_codes_with_400(client):
    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["X"] * 501)}
    )
    _assert_ticks_error(resp, 400, "too_many_codes")


def test_ticks_rejects_non_string_code_with_400(client):
    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001", 1])}
    )
    _assert_ticks_error(resp, 400, "code_must_be_string")


def test_ticks_returns_503_when_exchange_fails(client, monkeypatch):
    monkeypatch.setattr(other_mod, "get_exchange", _raise_runtime_error)

    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )

    _assert_ticks_error(resp, 503, "service_unavailable")


def test_us_ticks_fail_closed_when_primary_times_out(client, monkeypatch):
    class _UnavailableUS:
        def ticks(self, _codes):
            raise TimeoutError("primary timeout")

        def now_trading(self):
            return False

    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _UnavailableUS())

    resp = client.post(
        "/ticks", data={"market": "us", "codes": json.dumps(["AAPL.US"])}
    )

    _assert_ticks_error(resp, 503, "service_unavailable")


def test_us_ticks_do_not_fill_missing_primary_rows_from_another_source(client, monkeypatch):
    primary = {"AAPL.US": _tick("AAPL.US", 201.25, 1.75)}
    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _FakeEx(primary))

    resp = client.post(
        "/ticks",
        data={"market": "us", "codes": json.dumps(["AAPL.US", "TSLA.US"])},
    )

    assert resp.status_code == 200
    assert resp.get_json()["ticks"] == [
        {"code": "AAPL.US", "price": 201.25, "rate": 1.75}
    ]


def test_ticks_keeps_prices_when_market_state_probe_fails(client, monkeypatch):
    ticks_map = {"SZ.000001": _tick("SZ.000001", 3.0, 2.5)}
    monkeypatch.setattr(other_mod, "get_exchange", lambda _market: _FakeEx(ticks_map))
    monkeypatch.setattr(other_mod, "market_now_trading", _raise_runtime_error)

    resp = client.post(
        "/ticks", data={"market": "a", "codes": json.dumps(["SZ.000001"])}
    )

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "market_state": "unknown",
        "now_trading": None,
        "ticks": [{"code": "SZ.000001", "price": 3.0, "rate": 2.5}],
        "error": None,
    }
