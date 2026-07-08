"""script/perf/parquet_vs_csv_bench.py — V2: K 线 parquet vs CSV 体积/速度对比。

衡量 US-008 双写过渡的实际收益, 决定阶段 1 退出 (停 CSV 写) 的时机。

用法:
    python -m script.perf.parquet_vs_csv_bench [--sizes 100,1000,10000]

输出:
    | n_klines | parquet_kb | csv_kb | parquet/csv | parquet_write_ms | csv_write_ms | parquet_read_ms | csv_read_ms |
    并打印总结判定: 是否符合 docs/operations/kline-cache-parquet-migration.md 的退出条件。
"""

from __future__ import annotations

import argparse
import datetime
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandas as pd


# sys.path bootstrap
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


def make_klines(n: int) -> pd.DataFrame:
    """生成 n 根合成 K 线 (与生产 ex.klines 返回形态一致, tz-aware UTC)。"""
    rng = np.random.RandomState(42)
    start = datetime.datetime(2024, 1, 1, 9, 30, tzinfo=datetime.timezone.utc)
    dates = pd.date_range(start=start, periods=n, freq="1min")
    opens = 100.0 + rng.normal(0, 1, n).cumsum() * 0.1
    closes = opens + rng.normal(0, 0.3, n)
    highs = np.maximum.reduce([opens, closes]) + rng.uniform(0, 0.5, n)
    lows = np.minimum.reduce([opens, closes]) - rng.uniform(0, 0.5, n)
    volumes = rng.randint(1000, 5000, n).astype(float)
    return pd.DataFrame({
        "date": dates, "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": volumes,
    })


def bench_one_size(n: int) -> dict:
    """跑单个 size 的对比, 返回结果 dict。"""
    df = make_klines(n)
    tmp = Path(tempfile.mkdtemp(prefix=f"bench_{n}_"))
    parquet_path = tmp / "data.parquet"
    csv_path = tmp / "data.csv"

    # parquet write
    t0 = time.perf_counter()
    df.to_parquet(parquet_path, engine="pyarrow", compression="zstd", index=False)
    parquet_write_ms = (time.perf_counter() - t0) * 1000

    # csv write
    t0 = time.perf_counter()
    df.to_csv(csv_path, index=False)
    csv_write_ms = (time.perf_counter() - t0) * 1000

    parquet_kb = parquet_path.stat().st_size / 1024.0
    csv_kb = csv_path.stat().st_size / 1024.0

    # parquet read (10 次取平均, 避免冷启动噪音)
    parquet_reads = []
    for _ in range(10):
        t0 = time.perf_counter()
        _ = pd.read_parquet(parquet_path, engine="pyarrow")
        parquet_reads.append((time.perf_counter() - t0) * 1000)
    parquet_read_ms = sum(parquet_reads) / len(parquet_reads)

    # csv read (10 次取平均)
    csv_reads = []
    for _ in range(10):
        t0 = time.perf_counter()
        _ = pd.read_csv(csv_path, parse_dates=["date"])
        csv_reads.append((time.perf_counter() - t0) * 1000)
    csv_read_ms = sum(csv_reads) / len(csv_reads)

    shutil.rmtree(tmp, ignore_errors=True)

    return {
        "n": n,
        "parquet_kb": parquet_kb,
        "csv_kb": csv_kb,
        "size_ratio": parquet_kb / csv_kb if csv_kb > 0 else None,
        "parquet_write_ms": parquet_write_ms,
        "csv_write_ms": csv_write_ms,
        "write_speedup": csv_write_ms / parquet_write_ms if parquet_write_ms > 0 else None,
        "parquet_read_ms": parquet_read_ms,
        "csv_read_ms": csv_read_ms,
        "read_speedup": csv_read_ms / parquet_read_ms if parquet_read_ms > 0 else None,
    }


def fmt_row(r: dict) -> str:
    return (
        f"| {r['n']:>8} "
        f"| {r['parquet_kb']:>10.1f} "
        f"| {r['csv_kb']:>8.1f} "
        f"| {r['size_ratio']*100:>10.1f}% "
        f"| {r['parquet_write_ms']:>16.2f} "
        f"| {r['csv_write_ms']:>12.2f} "
        f"| {r['parquet_read_ms']:>15.2f} "
        f"| {r['csv_read_ms']:>11.2f} |"
    )


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default="100,1000,10000",
                   help="逗号分隔的 K 线数量列表 (默认 100,1000,10000)")
    args = p.parse_args()
    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]

    print("\n=== V2 parquet vs CSV 体积/速度基准 (synthetic seed=42) ===\n")
    print("| n_klines | parquet_kb |  csv_kb | parquet/csv | parquet_write_ms | csv_write_ms | parquet_read_ms | csv_read_ms |")
    print("|---------:|-----------:|--------:|------------:|-----------------:|-------------:|----------------:|------------:|")

    results = []
    for n in sizes:
        r = bench_one_size(n)
        results.append(r)
        print(fmt_row(r))

    # 总结判定
    avg_size_ratio = sum(r["size_ratio"] for r in results) / len(results)
    avg_read_speedup = sum(r["read_speedup"] for r in results) / len(results)
    avg_write_speedup = sum(r["write_speedup"] for r in results) / len(results)

    print("\n--- 总结 ---")
    print(f"平均 parquet/csv 体积比:  {avg_size_ratio*100:.1f}% (越小越好, 预期 ≤ 40%)")
    print(f"平均 parquet 读取加速:    {avg_read_speedup:.2f}× (预期 ≥ 3×)")
    print(f"平均 parquet 写入加速:    {avg_write_speedup:.2f}× (CSV 通常更快, 此项 < 1× 正常)")

    # docs/operations/kline-cache-parquet-migration.md 的退出条件 1: 体积减小 60-80%
    if avg_size_ratio < 0.4:
        print(f"\n[PASS] 体积压缩满足退出条件 (体积比 {avg_size_ratio*100:.1f}% < 40%)")
    else:
        print("\n[WARN] 体积压缩未达 60%+ 减小, 检查 zstd 压缩级别 / 数据特征")


if __name__ == "__main__":
    main()
