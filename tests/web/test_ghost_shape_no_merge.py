"""全量重算必须整体替换快照，不得保留已被行情证伪的形态。"""
import pathlib
import sys

import pandas as pd
import pytest

_root = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_root / "src"))
sys.path.insert(0, str(_root / "web" / "chanlun_chart"))

from cl_app.services import chart_cache, chart_compute  # noqa: E402
from chanlun.cl_utils import query_cl_chart_config  # noqa: E402

MARKET, CODE, FREQ = "a", "SH.GHOSTTEST", "5m"

_GHOST_START = (100, 1.0)
_FRESH_START = (200, 1.5)


def _ghost_entry():
    """existing_entry 里有一笔起点 (100, 1.0) 的未完成笔,新计算里将不再出现。"""
    data = {
        "t": [100, 200, 300],
        "o": [1.0] * 3, "h": [1.0] * 3, "l": [1.0] * 3, "c": [1.0] * 3, "v": [1.0] * 3,
        "bis": [{
            "points": [
                {"time": _GHOST_START[0], "price": _GHOST_START[1]},
                {"time": 300, "price": 2.0},
            ],
            "linestyle": 1,
        }],
    }
    return chart_cache._build_chart_cache_entry(data, is_full_snapshot=True)


def _fresh_chart_data():
    """全新全量计算结果:完全没有起点 (100, 1.0),只有从新起点 (200, 1.5) 开始的笔。"""
    return {
        "t": [100, 200, 300, 400],
        "o": [1.0] * 4, "h": [1.0] * 4, "l": [1.0] * 4, "c": [1.0] * 4, "v": [1.0] * 4,
        "bis": [{
            "points": [
                {"time": _FRESH_START[0], "price": _FRESH_START[1]},
                {"time": 400, "price": 2.5},
            ],
            "linestyle": 1,
        }],
    }


@pytest.fixture
def seeded_cache_key():
    cfg = query_cl_chart_config(MARKET, CODE)
    cache_key = chart_cache._build_cache_key(MARKET, CODE, FREQ, cfg)
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache[cache_key] = _ghost_entry()
    yield cache_key, cfg
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache.pop(cache_key, None)


def _fake_klines():
    return pd.DataFrame({
        "date": pd.to_datetime([100, 200, 300, 400], unit="s", utc=True),
        "open": [1.0] * 4, "high": [1.0] * 4, "low": [1.0] * 4,
        "close": [1.0] * 4, "volume": [1.0] * 4,
    })


class _FakeEx:
    def klines(self, code, freq, **kwargs):
        return _fake_klines()


def test_compute_and_cache_chart_data_does_not_resurrect_ghost_shape(
    monkeypatch, seeded_cache_key
):
    cache_key, cfg = seeded_cache_key
    monkeypatch.setattr(chart_compute, "_build_cache_key", lambda *a, **k: cache_key)
    monkeypatch.setattr(chart_compute, "get_exchange", lambda m: _FakeEx())
    monkeypatch.setattr(
        chart_compute,
        "build_strict_chart_cd",
        lambda **_kwargs: object(),
    )
    fresh = _fresh_chart_data()
    monkeypatch.setattr(
        chart_compute, "cl_data_to_tv_chart",
        lambda _frame, _config, **_kwargs: dict(fresh),
    )
    ok = chart_compute.compute_and_cache_chart_data(MARKET, CODE, FREQ, cfg)
    assert ok is True

    entry = chart_cache._get_chart_cache_entry(cache_key)
    starts = {
        (b["points"][0]["time"], b["points"][0]["price"])
        for b in entry["data"]["bis"]
    }
    assert _GHOST_START not in starts, (
        "陈旧快照里已被新行情证伪的未完成笔不应被保留(幽灵形态)"
    )
    assert starts == {_FRESH_START}, "应整体替换为全新计算结果,而非与旧数据并集"


# ---------------- Fix 2: tv_history 自己的 MISS(含 too_stale)兜底分支 ----------------
# 走真实 /tv/history 端点(过 evaluate_cache_for_tv_history 的 too_stale 判定),
# 而不是直接调内部函数,确保修复覆盖用户实际会打到的路径。

import time as _time  # noqa: E402

import pytest as _pytest  # noqa: E402

from cl_app import create_app  # noqa: E402
from cl_app.blueprints import tv as tv_mod  # noqa: E402

TOO_STALE_MARKET, TOO_STALE_CODE, TOO_STALE_FREQ = "a", "SH.GHOSTSTALE", "5m"


@_pytest.fixture
def client():
    app = create_app(test_config={
        "TESTING": True,
        "LOGIN_DISABLED": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
    })
    return app.test_client()


def _too_stale_ghost_entry():
    """validated_at 3 天前(远超收盘 hard cap 86400s)→ too_stale, 且带一笔陈旧未完成形态。"""
    data = {
        "t": [100, 200, 300],
        "o": [1.0] * 3, "h": [1.0] * 3, "l": [1.0] * 3, "c": [1.0] * 3, "v": [1.0] * 3,
        "bis": [{
            "points": [
                {"time": _GHOST_START[0], "price": _GHOST_START[1]},
                {"time": 300, "price": 2.0},
            ],
            "linestyle": 1,
        }],
    }
    return chart_cache._build_chart_cache_entry(
        data, is_full_snapshot=True, validated_at=_time.time() - 3 * 86400,
    )


def test_tv_history_too_stale_path_does_not_resurrect_ghost_shape(monkeypatch, client):
    cfg = query_cl_chart_config(TOO_STALE_MARKET, TOO_STALE_CODE)
    cache_key = chart_cache._build_cache_key(TOO_STALE_MARKET, TOO_STALE_CODE, TOO_STALE_FREQ, cfg)
    with chart_cache.cache_lock:
        chart_cache.chart_data_cache[cache_key] = _too_stale_ghost_entry()

    monkeypatch.setattr(tv_mod, "market_now_trading", lambda m: False)

    fresh = _fresh_chart_data()
    monkeypatch.setattr(chart_compute, "get_exchange", lambda m: _FakeEx())
    monkeypatch.setattr(
        chart_compute,
        "build_strict_chart_cd",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        chart_compute, "cl_data_to_tv_chart",
        lambda _frame, _config, **_kwargs: dict(fresh),
    )
    try:
        to = int(_time.time()) + 86400
        url = (
            f"/tv/history?symbol={TOO_STALE_MARKET}:{TOO_STALE_CODE}"
            f"&resolution=5&firstDataRequest=true&from=0&to={to}"
        )
        resp = client.get(url)
        body = resp.get_json()
        assert body["s"] == "ok"
        starts = {
            (b["points"][0]["time"], b["points"][0]["price"])
            for b in body.get("bis", [])
        }
        assert _GHOST_START not in starts, (
            "too_stale 兜底本应防止陈旧未完成笔泄漏给用户"
        )
        assert starts == {_FRESH_START}
    finally:
        with chart_cache.cache_lock:
            chart_cache.chart_data_cache.pop(cache_key, None)
