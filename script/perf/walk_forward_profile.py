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
from chanlun.recursive_bt.engine.engine import CL_CFG       # noqa: E402

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


# 分函数标度诊断:这些核心组件的 cumtime 随 N 的标度指数(1=线性,2=O(n²)),
# 用于精确定位「假增量」全量重建的真凶,而非只看某一规模下的绝对占比。
KEY_FUNCS = [
    ("bi_calculator.py", "calculate"),
    ("bi_calculator.py", "_rebuild_from_fxs"),
    ("bi_calculator.py", "_build_endpoint_stack"),
    ("bi_calculator.py", "_collect_fxs"),
    ("bi_calculator.py", "_try_incremental_extend"),
    ("xd_calculator.py", "calculate"),
    ("xd_calculator.py", "_build_segments"),
    ("zs_calculator.py", "calculate"),
    ("zslx_calculator.py", "calculate"),
    ("bs_point_calculator.py", "calculate"),
    ("cl.py", "process_mmd"),
]


def _func_snapshot(df: pd.DataFrame) -> dict:
    """cProfile 一次,抽出 {(文件名, 函数名): (ncalls, tottime, cumtime)}。"""
    pr = cProfile.Profile()
    pr.enable()
    _feed(df)
    pr.disable()
    ps = pstats.Stats(pr)
    snap = {}
    for (fn, _lineno, name), (_cc, nc, tt, ct, _callers) in ps.stats.items():
        snap[(Path(fn).name, name)] = (nc, tt, ct)
    return snap


def _func_scale(n1: int, n2: int):
    """各核心组件 cumtime 在 N1 vs N2 的标度指数,精确定位 O(n²) 真凶。"""
    import math
    snaps = {}
    for n in (n1, n2):
        df = _load(n)
        print(f"[scale-fn] profiling {len(df)} bar ...")
        snaps[n] = _func_snapshot(df)
    rn = n2 / n1
    print(f"\n  bar 倍率 {rn:.2f}x  (cumtime 标度指数:1=线性, 2=O(n^2))\n")
    hdr = f"  {'file:func':<42}{'ncalls':>9}{'ct1(ms)':>10}{'ct2(ms)':>10}{'ratio':>8}{'exp':>6}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for key in KEY_FUNCS:
        s1 = snaps[n1].get(key)
        s2 = snaps[n2].get(key)
        label = f"{key[0]}:{key[1]}"
        if not s1 or not s2:
            print(f"  {label:<42}{'(missing)':>9}")
            continue
        ct1, ct2, nc2 = s1[2], s2[2], s2[0]
        if ct1 > 0 and rn > 1:
            r = ct2 / ct1
            exp = math.log(r) / math.log(rn) if r > 0 else float('nan')
        else:
            r = exp = float('nan')
        print(f"  {label:<42}{nc2:>9}{ct1*1000:>10.1f}{ct2*1000:>10.1f}{r:>8.2f}{exp:>6.2f}")


def _timed(df: pd.DataFrame) -> float:
    t0 = time.perf_counter()
    _feed(df)
    return time.perf_counter() - t0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000, help="喂入 bar 数(0=全量)")
    ap.add_argument("--top", type=int, default=30, help="cumtime top N")
    ap.add_argument("--scale", type=int, nargs=2, metavar=("N1", "N2"),
                    help="缩放模式:量 N1 vs N2 墙钟,推断整体标度指数")
    ap.add_argument("--scale-fn", type=int, nargs=2, metavar=("N1", "N2"),
                    dest="scale_fn",
                    help="分函数标度:量各核心组件 cumtime 在 N1 vs N2 的标度指数")
    a = ap.parse_args()
    if a.scale_fn:
        _func_scale(a.scale_fn[0], a.scale_fn[1])
    elif a.scale:
        scale(a.scale[0], a.scale[1])
    else:
        profile(a.n, a.top)
