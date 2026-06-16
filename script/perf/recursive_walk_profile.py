"""script/perf/recursive_walk_profile.py — 含递归层的 walk-forward profiler。

walk_forward_profile.py 只量 legacy 流水线(process_kline_values);但真实回测每根 K 还
消费**递归层** get_branch_bspoints / get_recursive_branch_levels(按需算、memo 每 K clear),
那是 process_kline_values 之外的成本。本脚本每根 K 喂入后再调 get_branch_bspoints,
cProfile 揭示递归层的真实热点 + 它相对 legacy 的占比。

用法:
    python -m script.perf.recursive_walk_profile --n 2000 --top 35
    python -m script.perf.recursive_walk_profile --scale 1500 3000   # 递归层墙钟标度
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


def _feed_with_recursive(df: pd.DataFrame) -> CL:
    """逐根喂 + 每根调 get_branch_bspoints(模拟回测真实信号消费)。"""
    cd = CL("SH.600519", "5m", dict(CL_CFG))
    for row in df.itertuples(index=False):
        cd.process_kline_values(
            row.date, float(row.open), float(row.high),
            float(row.low), float(row.close), float(getattr(row, "volume", 0.0)),
        )
        # 回测每根 K 读信号:这会触发递归层全量重算(memo 每 K 已 clear)。
        cd.get_branch_bspoints(use_xd=False)
    return cd


def _timed(df: pd.DataFrame) -> float:
    t0 = time.perf_counter()
    _feed_with_recursive(df)
    return time.perf_counter() - t0


def profile(n: int, top: int):
    df = _load(n)
    print(f"[recursive-profile] 逐根喂 {len(df)} bar + 每根 get_branch_bspoints ...")
    pr = cProfile.Profile()
    pr.enable()
    _feed_with_recursive(df)
    pr.disable()
    s = io.StringIO()
    ps = pstats.Stats(pr, stream=s).sort_stats("cumulative")
    ps.print_stats(top)
    print(s.getvalue())


def scale(n1: int, n2: int):
    import math
    rows = []
    for n in (n1, n2):
        df = _load(n)
        best = min(_timed(df) for _ in range(2))
        rows.append((len(df), best))
        print(f"  N={len(df):5d}: {best*1000:9.1f} ms")
    (a_n, a_t), (b_n, b_t) = rows
    rn, rt = b_n / a_n, b_t / a_t
    exp = math.log(rt) / math.log(rn) if rt > 0 and rn > 1 else float("nan")
    print(f"\n  bar 倍率 {rn:.2f}x -> 时间倍率 {rt:.2f}x (标度指数 ~ {exp:.2f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--top", type=int, default=35)
    ap.add_argument("--scale", type=int, nargs=2, metavar=("N1", "N2"))
    a = ap.parse_args()
    if a.scale:
        scale(a.scale[0], a.scale[1])
    else:
        profile(a.n, a.top)
