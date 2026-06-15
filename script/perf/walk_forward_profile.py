"""script/perf/walk_forward_profile.py — P3 walk-forward 真实负载 profiler。

逐根喂 K(`process_kline_values`,实盘 live_monitor 的负载),用 cProfile 量
**per-trigger 全重扫**这条 O(n²) 主路径的真实热点。batch 一次性 process 测不出
(那只触发一次 calculate);walk-forward 才让 process_mmd 在每次尾部变化时重扫全史。

用法:
    python -m script.perf.walk_forward_profile --n 2000            # 单次 profile,top cumtime
    python -m script.perf.walk_forward_profile --scale 1500 3000   # N vs 2N 墙钟缩放比(验 O(n²))
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
import time
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from chanlun.core.cl import CL                       # noqa: E402
from chanlun.recursive_bt.engine import CL_CFG       # noqa: E402

FIX = REPO_ROOT / "tests" / "fixtures" / "SH.600519_5m.parquet"


def _load(n: int) -> pd.DataFrame:
    df = pd.read_parquet(FIX)
    if n > 0:
        df = df.iloc[:n].reset_index(drop=True)
    return df


def _feed(df: pd.DataFrame) -> CL:
    """逐根喂入,模拟实盘 live 增量。返回算完的 CL。"""
    cd = CL("SH.600519", "5m", dict(CL_CFG))
    for row in df.itertuples(index=False):
        cd.process_kline_values(
            row.date, float(row.open), float(row.high),
            float(row.low), float(row.close), float(getattr(row, "volume", 0.0)),
        )
    return cd


def profile(n: int, top: int):
    df = _load(n)
    print(f"[profile] 逐根喂 {len(df)} bar (walk-forward) ...")
    pr = cProfile.Profile()
    pr.enable()
    cd = _feed(df)
    pr.disable()
    bis = len(cd.get_bis())
    xds = len(cd.get_xds())
    print(f"[profile] done: bis={bis} xds={xds}")
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(top)
    print(s.getvalue())


def scale(n1: int, n2: int):
    """N vs ~2N 墙钟缩放:线性≈2.0x,O(n²)≈4.0x。各跑 2 次取小值去抖。"""
    rows = []
    for n in (n1, n2):
        df = _load(n)
        best = min(_timed(df) for _ in range(2))
        rows.append((len(df), best))
        print(f"  N={len(df):5d}: {best*1000:8.1f} ms")
    (a_n, a_t), (b_n, b_t) = rows
    ratio_n = b_n / a_n
    ratio_t = b_t / a_t
    # 时间倍率 = ratio_n ** exponent  →  exponent = log(ratio_t)/log(ratio_n)
    import math
    exp = math.log(ratio_t) / math.log(ratio_n) if ratio_t > 0 and ratio_n > 1 else float("nan")
    print(f"\n  bar 倍率 {ratio_n:.2f}x -> 时间倍率 {ratio_t:.2f}x  (标度指数 ~ {exp:.2f}; 1=线性, 2=O(n^2))")


def _timed(df: pd.DataFrame) -> float:
    t0 = time.perf_counter()
    _feed(df)
    return time.perf_counter() - t0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="喂入 bar 数(0=全量)")
    ap.add_argument("--top", type=int, default=30, help="cumtime top N")
    ap.add_argument("--scale", type=int, nargs=2, metavar=("N1", "N2"),
                    help="缩放模式:量 N1 vs N2 墙钟,推断标度指数")
    a = ap.parse_args()
    if a.scale:
        scale(a.scale[0], a.scale[1])
    else:
        profile(a.n, a.top)
