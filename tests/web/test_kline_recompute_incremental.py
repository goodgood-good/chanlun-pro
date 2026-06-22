"""kline_recompute 持久 CL 池(增量重算)：复用判定 + 增量==全量对拍。

- 复用判定(mock CL，快)：首根稳定→复用同实例；向左滚动/config变/无cache_key→新建。
- 增量==全量(真实 CL + 合成 K 线)：逐前缀复用(增量)的 cl_data_to_tv_chart 输出与
  每次新建(全量)完全一致——钉死"SSE/轮询走增量复用不改变缠论结果"。
"""
import math

import pandas as pd
import pytest

from cl_app.services import kline_recompute
from cl_app.services.kline_recompute import recompute_chart_data_from_klines


def _klines_df(ts, prices):
    return pd.DataFrame(
        {
            "date": pd.to_datetime(list(ts), unit="s", utc=True),
            "open": list(prices),
            "high": [p + 0.5 for p in prices],
            "low": [p - 0.5 for p in prices],
            "close": list(prices),
            "volume": [1000] * len(ts),
        }
    )


# ── 层 1：复用判定(mock CL) ──────────────────────────────────────────
class _FakeCL:
    instances = []

    def __init__(self, *a, **k):
        _FakeCL.instances.append(self)
        self.n = 0

    def process_klines(self, klines):
        self.n = len(klines)


@pytest.fixture
def mock_cl(monkeypatch):
    _FakeCL.instances = []
    monkeypatch.setattr("chanlun.core.cl.CL", _FakeCL)
    monkeypatch.setattr(
        "chanlun.cl_utils.cl_data_to_tv_chart",
        lambda cd, cfg, to_frequency=None: {"n": cd.n, "id": id(cd)},
    )
    kline_recompute.reset_cl_pool()
    yield
    kline_recompute.reset_cl_pool()


def test_reuse_when_prefix_stable(mock_cl):
    """首根 date 不变 + 根数不减 → 复用同一 CL(增量)。"""
    r1 = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]), cache_key="a:SYN:1m"
    )
    r2 = recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060, 1120], [10, 11, 12]), cache_key="a:SYN:1m"
    )
    assert len(_FakeCL.instances) == 1  # 只构造一次
    assert r1["id"] == r2["id"]  # 同一 CL 实例


def test_new_cl_when_first_date_changes(mock_cl):
    """首根 date 变早(向左滚动)→ 新建 CL(全量)。"""
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]), cache_key="a:SYN:1m"
    )
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {}, _klines_df([940, 1000, 1060], [9, 10, 11]), cache_key="a:SYN:1m"
    )
    assert len(_FakeCL.instances) == 2  # 向左滚动新建


def test_new_cl_when_config_changes(mock_cl):
    """cl_config 变 → 不可复用, 新建 CL。"""
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {"fx_bh": "yes"}, _klines_df([1000, 1060], [10, 11]), cache_key="a:SYN:1m"
    )
    recompute_chart_data_from_klines(
        "a", "SYN", "1m", {"fx_bh": "no"}, _klines_df([1000, 1060, 1120], [10, 11, 12]), cache_key="a:SYN:1m"
    )
    assert len(_FakeCL.instances) == 2


def test_no_reuse_without_cache_key(mock_cl):
    """无 cache_key → 每次新建(不入池), 向后兼容全量行为。"""
    recompute_chart_data_from_klines("a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]))
    recompute_chart_data_from_klines("a", "SYN", "1m", {}, _klines_df([1000, 1060], [10, 11]))
    assert len(_FakeCL.instances) == 2


def test_store_cl_to_pool_then_reuse(mock_cl):
    """store_cl_to_pool 存入的实例, 后续 recompute(同 cache_key) 命中增量复用。

    模拟"首次加载 web_batch 算好 CL → 存池 → 第一次轮询走 inc 而非 full(消 52s)"。
    """
    ext_cl = _FakeCL()  # 模拟首次加载外部算好的 CL
    base = _klines_df([1000, 1060], [10, 11])
    kline_recompute.store_cl_to_pool("a:SYN:1m", ext_cl, base, {}, "a")
    # 第一次轮询: 末尾追加新根、同 cache_key → 应复用 ext_cl(不新建)
    nxt = _klines_df([1000, 1060, 1120], [10, 11, 12])
    r = recompute_chart_data_from_klines("a", "SYN", "1m", {}, nxt, cache_key="a:SYN:1m")
    assert len(_FakeCL.instances) == 1  # 只有 store 的那个, recompute 未再新建(=走了增量)
    assert r["id"] == id(ext_cl)


# ── 层 2：增量 == 全量(真实 CL + 合成 K 线) ──────────────────────────
def _synth_klines(n, start_ts=1_600_000_000):
    """确定性合成 K 线(多周期正弦叠加 → 趋势+转折, 产生分型/笔/线段/中枢)。"""
    rows = []
    for i in range(n):
        v = 100 + 18 * math.sin(i / 17.0) + 7 * math.sin(i / 5.0) + 2.5 * math.sin(i / 2.0)
        vn = 100 + 18 * math.sin((i + 0.5) / 17.0) + 7 * math.sin((i + 0.5) / 5.0) + 2.5 * math.sin((i + 0.5) / 2.0)
        o, c = v, vn
        rows.append(
            {
                "date": pd.Timestamp(start_ts + i * 60, unit="s", tz="UTC"),
                "open": o,
                "high": max(o, c) + 0.5,
                "low": min(o, c) - 0.5,
                "close": c,
                "volume": 1000 + i,
            }
        )
    return pd.DataFrame(rows)


def _cl_config():
    try:
        from chanlun.cl_utils import query_cl_chart_config
        return query_cl_chart_config("a", "SYNINC")
    except Exception:
        return {}


def test_incremental_equals_full_end_to_end():
    """真实 CL: 逐前缀复用(增量)的输出与每次新建(全量)完全一致。"""
    kline_recompute.reset_cl_pool()
    klines = _synth_klines(360)
    cfg = _cl_config()
    ck = "a:SYNINC:1m"
    try:
        for i in range(60, 361, 6):
            prefix = klines.iloc[:i].copy()
            full = recompute_chart_data_from_klines("a", "SYNINC", "1m", cfg, prefix.copy())
            inc = recompute_chart_data_from_klines("a", "SYNINC", "1m", cfg, prefix.copy(), cache_key=ck)
            assert inc == full, f"前缀 {i}: 增量 != 全量"
    finally:
        kline_recompute.reset_cl_pool()


def test_incremental_equals_full_on_last_bar_update():
    """末根 OHLC 更新(盘中同根刷新, 根数不变)→ 复用增量 == 新建全量。

    这是 SSE 最高频场景: 同一分钟内末根价格随 tick 变动。复用 CL 时根数不变、
    first_date 不变 → 命中复用, CL 须按末根变化做增量并与全量一致。
    """
    kline_recompute.reset_cl_pool()
    klines = _synth_klines(200)
    cfg = _cl_config()
    ck = "a:SYNUPD:1m"
    try:
        base = klines.iloc[:200].copy()
        recompute_chart_data_from_klines("a", "SYNUPD", "1m", cfg, base.copy(), cache_key=ck)  # 建基线
        # 末根 OHLC 上抬(模拟盘中拉升, 根数不变)
        upd = base.copy()
        ic, ih = upd.columns.get_loc("close"), upd.columns.get_loc("high")
        upd.iloc[-1, ic] = upd.iloc[-1, ic] + 3.0
        upd.iloc[-1, ih] = upd.iloc[-1, ih] + 3.0
        full = recompute_chart_data_from_klines("a", "SYNUPD", "1m", cfg, upd.copy())
        inc = recompute_chart_data_from_klines("a", "SYNUPD", "1m", cfg, upd.copy(), cache_key=ck)
        assert inc == full, "末根更新: 增量 != 全量"
    finally:
        kline_recompute.reset_cl_pool()
