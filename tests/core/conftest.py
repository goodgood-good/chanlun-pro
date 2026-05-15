"""tests/core/conftest.py — 缠论核心算法层的测试脚手架 (US-001)。

提供:
- ``cl_config``: 完整默认 cl 配置 dict (与 web 生产路径一致)。
- ``make_klines_df``: 确定性合成 K 线 DataFrame 工厂 (seed + trend 控制)。
- ``cl_with_synthetic_klines``: 一键得到"已喂完 process_klines 的 CL 对象"。
- ``cl_snapshot``: 把 CL 内部状态序列化成可哈希/可比较的纯 Python dict。
  (用于 US-002 计数断言、US-003 增量等价性、US-004 真实标的 baseline。)

设计原则:
- 完全确定性: 同 seed 输入 → 同输出，无全局状态污染 (用 RandomState 局部实例)。
- OHLC 不变量: high >= max(open, close), low <= min(open, close), high >= low。
- 与生产 K 线一致的 tz: 默认 UTC tz-aware，覆盖 alpaca/cq 真实路径。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from chanlun.core.cl import CL


# ---------------------------------------------------------------------------
# 默认 cl 配置 (与 tests/test_tv_history_backward_scroll.py:28 对齐)
# ---------------------------------------------------------------------------
DEFAULT_CL_CONFIG: Dict[str, Any] = {
    "chart_show_fx": "1",
    "chart_show_bi": "1",
    "chart_show_xd": "1",
    "chart_show_bi_zs": "1",
    "chart_show_xd_zs": "1",
    "chart_show_bi_mmd": "1",
    "chart_show_xd_mmd": "1",
    "chart_show_bi_bc": "1",
    "chart_show_xd_bc": "1",
    "zs_bi_type": ["zs_type_bz"],
    "zs_xd_type": ["zs_type_bz"],
    "idx_macd_fast": 12,
    "idx_macd_slow": 26,
    "idx_macd_signal": 9,
}


@pytest.fixture
def cl_config() -> Dict[str, Any]:
    """返回默认 cl 配置的深拷贝，测试可自由修改不会影响其它测试。"""
    return dict(DEFAULT_CL_CONFIG)


# ---------------------------------------------------------------------------
# 合成 K 线生成
# ---------------------------------------------------------------------------
def _generate_kline_df(
    n_klines: int,
    *,
    seed: int = 42,
    trend: str = "oscillate",
    base_price: float = 100.0,
    freq: str = "1min",
    start: str = "2024-01-01 09:30",
    tz: Optional[str] = "UTC",
    with_gap: bool = False,
    multi_freq: bool = False,
) -> pd.DataFrame:
    """确定性合成 K 线 DataFrame.

    简单模式 (multi_freq=False): ``close = base + 5*sin(t/6) + drift*t + noise``，
    适用于 smoke / fixture 一致性检查。

    多频模式 (multi_freq=True): ``close = base + 大周期 + 中周期 + 小周期 + drift + noise``，
    产生更复杂的笔/段/中枢结构，适用于 golden / 增量等价性测试。

    Args:
        n_klines: 行数 (>= 1)。
        seed: 随机种子 (局部 RandomState, 不污染全局 np.random)。
        trend: ``"up" | "down" | "oscillate"``，决定 close 漂移系数。
        with_gap: True 时在 n//2 处插入 1 天 gap，模拟跨日不连续。
        multi_freq: True 时使用 3 频叠加, 产生更密集的分型/笔/段。

    Returns:
        DataFrame with columns: date, open, high, low, close, volume.
    """
    if n_klines < 1:
        raise ValueError(f"n_klines must be >= 1, got {n_klines}")

    rng = np.random.RandomState(seed)
    t = np.arange(n_klines, dtype=float)

    if multi_freq:
        # 多频叠加: 大波 (周期 30) + 中波 (周期 8) + 小波 (周期 3)
        drift = {"up": 0.06, "down": -0.06, "oscillate": 0.005}.get(trend, 0.005)
        closes = (
            base_price
            + drift * t
            + 12.0 * np.sin(t / 30.0)
            + 4.0 * np.sin(t / 8.0)
            + 2.0 * np.sin(t / 3.0)
        )
        noise_std = 0.15
    else:
        drift = {"up": 0.10, "down": -0.10, "oscillate": 0.02}.get(trend, 0.02)
        closes = base_price + 5.0 * np.sin(t / 6.0) + t * drift
        noise_std = 0.05

    closes = closes + rng.normal(0, noise_std, size=n_klines)

    highs = closes + 0.6 + rng.uniform(0, 0.15, size=n_klines)
    lows = closes - 0.6 - rng.uniform(0, 0.15, size=n_klines)
    opens = closes - 0.05 * np.sin(t / 3.0) + rng.normal(0, 0.02, size=n_klines)
    volumes = (1000 + (t.astype(int) % 7) * 50 + rng.randint(0, 100, size=n_klines)).astype(float)

    # OHLC 不变量: 强制 high >= max(open, close, high), low <= min(open, close, low)
    highs = np.maximum.reduce([highs, opens, closes])
    lows = np.minimum.reduce([lows, opens, closes])

    if with_gap:
        half = n_klines // 2
        before = pd.date_range(start=start, periods=half, freq=freq, tz=tz)
        gap_start = before[-1] + pd.Timedelta(days=1)
        after = pd.date_range(start=gap_start, periods=n_klines - half, freq=freq, tz=tz)
        dates = before.append(after)
    else:
        dates = pd.date_range(start=start, periods=n_klines, freq=freq, tz=tz)

    return pd.DataFrame(
        {
            "date": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


@pytest.fixture
def make_klines_df():
    """K 线 DataFrame 工厂 (透传 ``_generate_kline_df`` 参数)。"""
    return _generate_kline_df


# ---------------------------------------------------------------------------
# CL 对象工厂
# ---------------------------------------------------------------------------
@pytest.fixture
def cl_with_synthetic_klines(cl_config):
    """工厂 fixture: ``cl_with_synthetic_klines(n_klines, seed=42, with_gap=False, trend="oscillate")``

    返回值: 已完成 ``process_klines`` 的 CL 对象，调用方可直接 ``cd.get_xds()`` 等。
    """

    def _factory(
        n_klines: int,
        *,
        seed: int = 42,
        with_gap: bool = False,
        trend: str = "oscillate",
        code: str = "TEST.001",
        frequency: str = "1m",
        config: Optional[Dict[str, Any]] = None,
        multi_freq: bool = False,
    ) -> CL:
        df = _generate_kline_df(
            n_klines,
            seed=seed,
            trend=trend,
            with_gap=with_gap,
            multi_freq=multi_freq,
        )
        cd = CL(code, frequency, dict(config or cl_config))
        cd.process_klines(df)
        return cd

    return _factory


# ---------------------------------------------------------------------------
# CL 状态 snapshot (用于等价性比较)
# ---------------------------------------------------------------------------
def _fmt_date(dt: Any) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if hasattr(dt, "strftime") else str(dt)


def _line_snap(line: Any) -> Dict[str, Any]:
    """笔/线段的端点签名: (start, end, type, done)."""
    start_date = getattr(getattr(line, "start", None), "k", None)
    end_date = getattr(getattr(line, "end", None), "k", None)
    return {
        "start_date": _fmt_date(start_date.date) if start_date else None,
        "start_val": getattr(getattr(line, "start", None), "val", None),
        "end_date": _fmt_date(end_date.date) if end_date else None,
        "end_val": getattr(getattr(line, "end", None), "val", None),
        "type": getattr(line, "type", None),
        "done": bool(getattr(line, "done", False)),
    }


def _fx_snap(fx: Any) -> Dict[str, Any]:
    return {
        "type": getattr(fx, "type", None),
        "date": _fmt_date(fx.k.date) if getattr(fx, "k", None) else None,
        "val": getattr(fx, "val", None),
        "done": bool(getattr(fx, "done", True)),
    }


def _zs_snap(zs: Any) -> Dict[str, Any]:
    start = getattr(zs, "start", None)
    end = getattr(zs, "end", None)
    return {
        "zs_type": getattr(zs, "zs_type", None),
        "start_date": _fmt_date(start.k.date) if (start and getattr(start, "k", None)) else None,
        "end_date": _fmt_date(end.k.date) if (end and getattr(end, "k", None)) else None,
        "zg": getattr(zs, "zg", None),
        "zd": getattr(zs, "zd", None),
        "gg": getattr(zs, "gg", None),
        "dd": getattr(zs, "dd", None),
        "level": getattr(zs, "level", None),
        "done": bool(getattr(zs, "done", False)),
    }


def cl_snapshot(cd: CL) -> Dict[str, Any]:
    """把 CL 对象的关键算法状态序列化为可比较 dict。

    用于 US-003 增量 vs 全量等价性、US-004 真实标的 baseline。
    覆盖: kline 数量 + fx + bi + xd + bi_zs + xd_zs 全部端点签名。
    不覆盖: MACD 内部数组 (浮点不稳定)、bs_points (单独路径)。
    """
    return {
        "klines_count": len(cd.get_klines()),
        "fxs": [_fx_snap(f) for f in cd.get_fxs()],
        "bis": [_line_snap(b) for b in cd.get_bis()],
        "xds": [_line_snap(x) for x in cd.get_xds()],
        "bi_zss": [_zs_snap(z) for z in cd.get_bi_zss()],
        "xd_zss": [_zs_snap(z) for z in cd.get_xd_zss()],
    }


@pytest.fixture
def snapshot():
    """fixture 形态暴露 cl_snapshot，便于 ``snapshot(cd)`` 用法。"""
    return cl_snapshot
