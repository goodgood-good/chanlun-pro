"""script/perf/db_bench.py — DB K 线存取吞吐量基准。

衡量单线程不同数据量下 insert/query/delete 耗时，以及多线程并发写入的总耗时与峰值内存。

用法:
    python -m script.perf.db_bench
"""
import sys
import time
import tracemalloc
import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd


# 确保从仓库根目录运行时 src 在 sys.path 中
REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_DIR = REPO_ROOT / "src"
if SRC_DIR.as_posix() not in [p.replace("\\", "/") for p in sys.path]:
    sys.path.insert(0, str(SRC_DIR))

from chanlun.persistence.db import DB  # noqa: E402
from chanlun.base import Market  # noqa: E402


def make_klines(n: int) -> pd.DataFrame:
    """生成 n 根合成 K 线数据帧，用于基准测试。"""
    start = datetime.datetime.now().replace(microsecond=0) - datetime.timedelta(
        minutes=n
    )
    dates = [start + datetime.timedelta(minutes=i) for i in range(n)]
    opens = np.random.uniform(100, 110, size=n)
    highs = opens + np.random.uniform(0, 5, size=n)
    lows = opens - np.random.uniform(0, 5, size=n)
    closes = opens + np.random.uniform(-2.5, 2.5, size=n)
    volumes = np.random.randint(1000, 5000, size=n)
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


def bench_insert_query_delete(db: DB, market: str, code: str, frequency: str, n: int):
    """单线程 insert/query/delete 三步耗时与峰值内存。"""
    print(f"[single] market={market} code={code} f={frequency} rows={n}")
    df = make_klines(n)
    tracemalloc.start()

    t0 = time.perf_counter()
    db.klines_insert(market, code, frequency, df)
    t1 = time.perf_counter()

    _ = db.klines_query(market, code, frequency)
    t2 = time.perf_counter()

    db.klines_delete(market, code, frequency)
    t3 = time.perf_counter()

    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    print(
        "  insert: {:.3f}s, query: {:.3f}s, delete: {:.3f}s, peak mem: {:.2f} MB".format(
            t1 - t0, t2 - t1, t3 - t2, peak / (1024 * 1024)
        )
    )


def _insert_task(db: DB, market: str, code: str, frequency: str, n: int):
    df = make_klines(n)
    t0 = time.perf_counter()
    db.klines_insert(market, code, frequency, df)
    t1 = time.perf_counter()
    return code, t1 - t0


def bench_concurrent_inserts(
    db: DB, market: str, base_code: str, frequency: str, workers: int, n: int
):
    """多线程并发写入基准：统计总耗时、各线程用时及峰值内存。"""
    print(
        f"[concurrent] market={market} base={base_code} f={frequency} workers={workers} rows={n}"
    )
    codes = [f"{base_code}_{i}" for i in range(workers)]
    tracemalloc.start()
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_insert_task, db, market, c, frequency, n) for c in codes]
        results = []
        for f in as_completed(futures):
            results.append(f.result())
    t1 = time.perf_counter()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total_time = t1 - t0
    per_worker = {c: round(sec, 3) for c, sec in results}
    print(
        f"  total: {total_time:.3f}s, peak mem: {peak/(1024*1024):.2f} MB, per worker: {per_worker}"
    )


def main():
    db = DB()
    market = Market.US.value
    frequency = "1m"

    # 单线程基准：测试不同数据量下的 insert/query/delete 耗时
    for n in [1000, 5000, 20000]:
        bench_insert_query_delete(db, market, "BENCH_CODE", frequency, n)

    # 多线程并发写入基准
    bench_concurrent_inserts(db, market, "BENCH_PAR", frequency, workers=4, n=5000)


if __name__ == "__main__":
    main()