"""script/perf/process_mmd_bisect_bench.py — P7 valid_zss 切片 bisect vs O(M) 对比。

直接 micro-benchmark 两种实现 (旧 list comp / 新 bisect), 不依赖完整 CL 流水线,
单纯量化 valid_zss 切片这一步的加速比。

用法:
    python -m script.perf.process_mmd_bisect_bench [--n_lines 8000 --n_zss 500]
"""

from __future__ import annotations

import argparse
import bisect
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


@dataclass
class _MockK:
    k_index: int


@dataclass
class _MockStart:
    start: _MockK
    k: _MockK


@dataclass
class _MockZS:
    start: _MockStart
    end: _MockStart


@dataclass
class _MockLine:
    end_k_index: int


def _make_zss(n_zss: int) -> list:
    """合成 n_zss 个 zss, start.start.k.k_index 单调升序。"""
    zss = []
    for i in range(n_zss):
        s = _MockStart(start=_MockK(k_index=i * 10), k=_MockK(k_index=i * 10))
        e = _MockStart(start=_MockK(k_index=i * 10 + 5), k=_MockK(k_index=i * 10 + 5))
        zss.append(_MockZS(start=s, end=e))
    return zss


def _make_lines(n_lines: int, max_k: int) -> list:
    """合成 n_lines 个 line, end.k.k_index 随机分布在 [0, max_k)。"""
    rng = random.Random(42)
    return [_MockLine(end_k_index=rng.randrange(0, max_k)) for _ in range(n_lines)]


def filter_old(zss, now_end_k):
    """旧 list comp 实现 (O(M))。"""
    return [
        zs for zs in zss
        if zs.start is not None
        and zs.start.start is not None
        and zs.start.start.k_index < now_end_k
    ]


def filter_new(clean_zss, start_keys, now_end_k):
    """P7 bisect 实现 (O(log M))。"""
    idx = bisect.bisect_left(start_keys, now_end_k)
    return clean_zss[:idx]


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--n_lines", type=int, default=8000, help="N (典型 1m × 90 天 ~8000)")
    p.add_argument("--n_zss", type=int, default=500, help="M (典型长序列 ~数百中枢)")
    p.add_argument("--repeats", type=int, default=5)
    args = p.parse_args()

    zss = _make_zss(args.n_zss)
    max_k = args.n_zss * 10 + 10
    lines = _make_lines(args.n_lines, max_k)

    # 旧实现
    t0 = time.perf_counter()
    for _ in range(args.repeats):
        for line in lines:
            _ = filter_old(zss, line.end_k_index)
    old_ms = (time.perf_counter() - t0) * 1000 / args.repeats

    # 新实现 (预过滤一次)
    clean_zss = [zs for zs in zss if zs.start and zs.start.start]
    start_keys = [zs.start.start.k_index for zs in clean_zss]
    t0 = time.perf_counter()
    for _ in range(args.repeats):
        for line in lines:
            _ = filter_new(clean_zss, start_keys, line.end_k_index)
    new_ms = (time.perf_counter() - t0) * 1000 / args.repeats

    print(f"\n=== P7 valid_zss 切片 bench (N={args.n_lines} M={args.n_zss}, {args.repeats} reps) ===\n")
    print("| 实现        |  耗时 ms | 相对旧 |")
    print("|-------------|---------:|-------:|")
    print(f"| 旧 list comp | {old_ms:7.2f} | 1.00x  |")
    print(f"| 新 bisect    | {new_ms:7.2f} | {new_ms/old_ms:.3f}x |")
    print(f"\n加速比: {old_ms/new_ms:.1f}x faster")
    print("  (单次 calculate() 总收益还要乘以 3 — 1buy/2buy/3buy 都用同样 valid_zss 切片)")


if __name__ == "__main__":
    main()
