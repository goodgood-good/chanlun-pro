"""script/perf/cl_object_cache_bench.py — V3: cl_object_cache 三路径耗时基准。

衡量 US-009 / F1 的实际加速比, 把"cache hit / incremental extend / full rebuild"
三种路径的耗时拉到同一个量表上, 验证 P4 优化的真实收益。

用法:
    python -m script.perf.cl_object_cache_bench [--n 500]

输出:
    路径                 | 平均 ms | 相对全量重建
    full_rebuild         | xxx ms  | 1.00× (baseline)
    incremental_extend   | yyy ms  | 0.zz×
    cache_hit            | zzz ms  | 0.00z×
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd


# sys.path bootstrap
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT / "web" / "chanlun_chart"))


def make_klines(n: int, seed: int = 42) -> pd.DataFrame:
    """multi_freq 合成 K 线 (与 tests/core/conftest.py 同款公式)。"""
    rng = np.random.RandomState(seed)
    t = np.arange(n, dtype=float)
    closes = (100.0 + 0.005 * t + 12.0 * np.sin(t / 30.0)
              + 4.0 * np.sin(t / 8.0) + 2.0 * np.sin(t / 3.0))
    closes += rng.normal(0, 0.15, size=n)
    highs = closes + 0.6 + rng.uniform(0, 0.15, size=n)
    lows = closes - 0.6 - rng.uniform(0, 0.15, size=n)
    opens = closes - 0.05 * np.sin(t / 3.0) + rng.normal(0, 0.02, size=n)
    volumes = (1000 + (t.astype(int) % 7) * 50 + rng.randint(0, 100, size=n)).astype(float)
    highs = np.maximum.reduce([highs, opens, closes])
    lows = np.minimum.reduce([lows, opens, closes])
    dates = pd.date_range(start="2024-01-01 09:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    })


def _measure(fn, repeats: int = 10) -> tuple[float, float]:
    """跑 fn N 次, 返回 (平均 ms, 最小 ms)。"""
    times = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t0) * 1000)
    return sum(times) / len(times), min(times)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n", type=int, default=500, help="基线 K 线数 (默认 500)")
    p.add_argument("--repeats", type=int, default=10, help="每路径重复次数")
    args = p.parse_args()

    from cl_app.services.cl_object_cache import (
        get_or_compute_cl, clear_all, stats
    )

    # 关键: df_extended 必须由 df_full 切片得到, 保证前 args.n 根完全相同。
    # 若独立两次 make_klines, 即使 seed 相同, 长度不同的 RandomState 序列也会
    # 生成不同的前缀 → ref-bar OHLC 不一致 → signature 不连续 → 走 full_rebuild
    # 而非 incremental_extend, V3 结果失真。
    df_full = make_klines(args.n + 5)
    df_initial = df_full.iloc[:args.n].copy()
    df_extended = df_full.copy()

    cfg = {"chart_show_fx": "1"}  # 任意 cl_config

    # === full_rebuild 路径 (每次都清缓存) ===
    def _full_rebuild():
        clear_all()
        get_or_compute_cl("a", "T", "1m", cfg, df_initial)
    avg_full, min_full = _measure(_full_rebuild, args.repeats)

    # === incremental_extend 路径 (setup 不计时, 只测第二次调用) ===
    inc_timings = []
    for _ in range(args.repeats):
        clear_all()
        get_or_compute_cl("a", "T", "1m", cfg, df_initial)  # setup, 不计时
        t0 = time.perf_counter()
        get_or_compute_cl("a", "T", "1m", cfg, df_extended)  # 真增量, 计时
        inc_timings.append((time.perf_counter() - t0) * 1000)
    avg_inc = sum(inc_timings) / len(inc_timings)
    min_inc = min(inc_timings)

    # === cache_hit 路径 ===
    clear_all()
    get_or_compute_cl("a", "T", "1m", cfg, df_initial)  # setup
    def _hit():
        get_or_compute_cl("a", "T", "1m", cfg, df_initial)
    avg_hit, min_hit = _measure(_hit, args.repeats * 100)  # hit 极快, 多跑几次

    # === 输出 ===
    print(f"\n=== V3 cl_object_cache 三路径耗时 (n={args.n}, repeats={args.repeats}) ===\n")
    print(f"| Path               | Avg ms   | Min ms   | vs full_rebuild |")
    print(f"|--------------------|---------:|---------:|----------------:|")
    print(f"| full_rebuild       | {avg_full:8.2f} | {min_full:8.2f} | 1.00x baseline  |")
    print(f"| incremental_extend | {avg_inc:8.2f} | {min_inc:8.2f} | {avg_inc/avg_full:6.2f}x         |")
    print(f"| cache_hit          | {avg_hit:8.4f} | {min_hit:8.4f} | {avg_hit/avg_full:6.4f}x       |")

    print(f"\n--- Summary ---")
    if avg_inc < avg_full * 0.5:
        print(f"[PASS] incremental_extend < 50% full_rebuild ({avg_inc/avg_full*100:.0f}%)")
    else:
        print(f"[WARN] incremental not significantly faster ({avg_inc/avg_full*100:.0f}%), check process_klines._preprocess")
    if avg_hit < 1.0:
        print(f"[PASS] cache_hit < 1ms ({avg_hit:.3f}ms)")
    print(f"\nStats: {stats()}")


if __name__ == "__main__":
    main()
