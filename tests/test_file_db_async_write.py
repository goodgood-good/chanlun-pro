"""tests/test_file_db_async_write.py — US-007 验证 _atomic_write_pickle 异步化。

AC:
- 正常路径调用 _atomic_write_pickle 在 ≤50ms wall time 内返回 (不再 450ms 退避)
- 写入内容最终在 ≤1s 内可从磁盘读回
- worker 内失败仅记 LogUtil.warning, 不向调用栈抛
- 已 shutdown 时 fallback 同步, 不丢写

不依赖任何外部 SDK, 只测纯 file IO 行为。
"""

from __future__ import annotations

import pathlib
import pickle
import time
from concurrent.futures import Future
from typing import Any

import pytest

from chanlun.file_db import FileCacheDB


@pytest.fixture
def fdb(tmp_path, monkeypatch) -> FileCacheDB:
    """构造一个独立 FileCacheDB, 数据目录指向 tmp_path (与全局 .chanlun_pro 隔离)。"""
    # FileCacheDB 单例从 get_data_path 取目录, 用 monkeypatch 重定向
    import chanlun.config as _cfg

    monkeypatch.setattr(_cfg, "get_data_path", lambda: tmp_path)
    return FileCacheDB()


def test_atomic_write_pickle_returns_fast(fdb: FileCacheDB, tmp_path: pathlib.Path):
    """正常路径下 _atomic_write_pickle 在 ≤50ms 内返回 (即, 不再 sync 等 450ms)。"""
    obj: Any = {"hello": "world", "n": list(range(1000))}
    path = tmp_path / "fast.pkl"

    t0 = time.perf_counter()
    result = fdb._atomic_write_pickle(path, obj)
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    assert isinstance(result, Future), "应返回 Future (fire-and-forget 异步)"
    assert elapsed_ms < 50.0, (
        f"_atomic_write_pickle 阻塞太久: {elapsed_ms:.1f}ms (US-007 期望 < 50ms, "
        f"否则与之前同步 450ms 退避无差)"
    )


def test_atomic_write_pickle_eventually_lands_on_disk(
    fdb: FileCacheDB, tmp_path: pathlib.Path
):
    """异步写入最终一定要在 ≤1s 内可从磁盘读回。"""
    obj: Any = {"sentinel": "us-007", "nested": {"a": 1, "b": [2, 3]}}
    path = tmp_path / "eventually.pkl"

    fut = fdb._atomic_write_pickle(path, obj)
    # 等异步完成 (最多 1s, 但实际通常 < 100ms)
    fut.result(timeout=1.0)

    assert path.exists(), f"异步落盘后磁盘上仍无文件: {path}"
    with open(path, "rb") as fp:
        loaded = pickle.load(fp)
    assert loaded == obj


def test_atomic_write_pickle_logs_and_swallows_worker_errors(
    fdb: FileCacheDB, tmp_path: pathlib.Path, monkeypatch, caplog
):
    """worker 内底层 _atomic_write_pickle_blocking 抛错时:
    - 不向调用栈传播
    - LogUtil.warning 至少记一条 "async write failed"
    """
    import logging
    import chanlun.file_db as _filedb_mod

    # 让底层 blocking 写直接抛错
    def _boom(self, p, o):  # noqa: ARG001
        raise RuntimeError("simulated disk full")

    monkeypatch.setattr(FileCacheDB, "_atomic_write_pickle_blocking", _boom)

    # 同时捕获 LogUtil 的输出 (它包装 logging, 用 caplog 拦截即可)
    with caplog.at_level(logging.WARNING):
        fut = fdb._atomic_write_pickle(tmp_path / "boom.pkl", {"x": 1})
        # 等 worker 跑完 — fut.result() 不应再 raise (异常已被 worker 吞掉)
        fut.result(timeout=1.0)

    # 调用栈没炸, 到这一步就算通过了; 验证 warning 已记录
    assert any(
        "async write failed" in rec.getMessage() for rec in caplog.records
    ), f"应有 'async write failed' warning, 实际 caplog: {[r.getMessage() for r in caplog.records]}"


def test_atomic_write_pickle_concurrent_writes_dont_drop(
    fdb: FileCacheDB, tmp_path: pathlib.Path
):
    """并发对不同 path 写, 全部要落盘, 不能因 executor 队列丢任务。"""
    from concurrent.futures import wait

    objs = [{"i": i, "payload": list(range(50))} for i in range(20)]
    paths = [tmp_path / f"concurrent_{i}.pkl" for i in range(20)]

    futures = [fdb._atomic_write_pickle(p, o) for p, o in zip(paths, objs)]
    done, not_done = wait(futures, timeout=2.0)
    assert not not_done, f"{len(not_done)} 个 future 在 2s 内未完成 (executor 可能阻塞)"

    for p, o in zip(paths, objs):
        assert p.exists()
        with open(p, "rb") as fp:
            assert pickle.load(fp) == o
