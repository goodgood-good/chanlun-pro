"""tests/test_file_db_parquet.py — US-008 验证 K 线 parquet 化 + 双写过渡。

AC:
- save_klines_parquet / load_klines_parquet round-trip 保持 schema 与 dtype
- save_tdx_klines 双写: parquet + CSV 都存在
- get_tdx_klines 优先 parquet, parquet 缺失/损坏时 fallback CSV
- 损坏 parquet 文件被自动 unlink, 不影响后续调用
"""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest

from chanlun.file_db import FileCacheDB


@pytest.fixture
def fdb(tmp_path, monkeypatch) -> FileCacheDB:
    import chanlun.config as _cfg
    monkeypatch.setattr(_cfg, "get_data_path", lambda: tmp_path)
    return FileCacheDB()


def _sample_klines_df() -> pd.DataFrame:
    return pd.DataFrame({
        "date": pd.date_range("2024-01-01 09:30", periods=10, freq="1min", tz="UTC"),
        "open": [100.0 + i * 0.1 for i in range(10)],
        "high": [101.0 + i * 0.1 for i in range(10)],
        "low": [99.0 + i * 0.1 for i in range(10)],
        "close": [100.5 + i * 0.1 for i in range(10)],
        "volume": [1000.0 + i * 50 for i in range(10)],
    })


def test_parquet_round_trip_preserves_schema(fdb: FileCacheDB):
    """write → read 后 dtype/values 完全一致。"""
    df_in = _sample_klines_df()
    assert fdb.save_klines_parquet("a", "TEST.001", "1m", df_in) is True
    df_out = fdb.load_klines_parquet("a", "TEST.001", "1m")

    assert df_out is not None
    pd.testing.assert_frame_equal(df_in, df_out, check_like=False)


def test_load_parquet_returns_none_when_missing(fdb: FileCacheDB):
    assert fdb.load_klines_parquet("a", "NONEXIST", "1m") is None


def test_load_parquet_handles_corrupt_file(fdb: FileCacheDB, tmp_path: pathlib.Path):
    """损坏的 parquet 文件不应让调用方崩, 应静默 unlink + 返回 None。"""
    p = fdb._kline_parquet_path("a", "BROKEN", "1m")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"this is not a valid parquet file")

    result = fdb.load_klines_parquet("a", "BROKEN", "1m")
    assert result is None
    assert not p.exists(), "损坏的 parquet 文件应被 unlink"


def test_save_tdx_klines_double_writes_parquet_and_csv(fdb: FileCacheDB):
    """save_tdx_klines 双写过渡: parquet 和 CSV 都应存在。"""
    df = _sample_klines_df()
    fdb.save_tdx_klines("a", "TEST.001", "1m", df)

    parquet_path = fdb._kline_parquet_path("a", "TEST.001", "1m")
    csv_path = fdb._kline_csv_path("a", "TEST.001", "1m")
    assert parquet_path.exists(), "parquet 主路径必须存在"
    assert csv_path.exists(), "CSV 兜底路径在灰度期内也要存在"


def test_get_tdx_klines_prefers_parquet(fdb: FileCacheDB):
    """parquet + CSV 都存在时, get_tdx_klines 走 parquet 路径。

    验证方式: 写一份 df, 然后手动改 CSV 让它内容不同;
    读回应该等于 parquet 内容 (不是 CSV)。
    """
    df_correct = _sample_klines_df()
    fdb.save_tdx_klines("a", "TEST.001", "1m", df_correct)

    # 篡改 CSV 让它不同 (close 全 999)
    csv_path = fdb._kline_csv_path("a", "TEST.001", "1m")
    df_wrong = df_correct.copy()
    df_wrong["close"] = 999.0
    df_wrong.to_csv(csv_path, index=False)

    result = fdb.get_tdx_klines("a", "TEST.001", "1m")
    assert result is not None
    # 注意 get_tdx_klines 会去掉最后一行 (业务行为, 见原代码)
    assert (result["close"] != 999.0).all(), "应读到 parquet 内容, 不是被篡改的 CSV"


def test_get_tdx_klines_fallback_to_csv_when_no_parquet(fdb: FileCacheDB):
    """parquet 不存在时, 走 CSV 兜底 (老数据兼容)。"""
    df = _sample_klines_df()
    # 只写 CSV (模拟"老数据没 parquet")
    csv_path = fdb._kline_csv_path("a", "TEST.001", "1m")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)

    result = fdb.get_tdx_klines("a", "TEST.001", "1m")
    assert result is not None
    assert len(result) == len(df) - 1  # get_tdx_klines 去掉最后一行
