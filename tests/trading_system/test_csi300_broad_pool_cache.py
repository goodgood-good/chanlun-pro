from __future__ import annotations

import sqlite3

import pytest

from tools.merge_csi300_broad_pool_cache import (
    BAR_SCHEMA,
    QUERY_WINDOW_SCHEMA,
    SeriesSource,
    merge_pool_cache,
)
from tools.research_data import sha256_file


def _source(path, symbol: str, rows: tuple[tuple[object, ...], ...]) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(BAR_SCHEMA)
        connection.execute(QUERY_WINDOW_SCHEMA)
        connection.executemany("INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _bar(symbol: str, period: str, at: str, close: float) -> tuple[object, ...]:
    return (
        symbol,
        period,
        "S_Unsplit",
        at,
        at,
        close,
        close,
        close,
        close,
        close,
        1.0,
        close,
    )


def test_merge_pool_cache_preserves_each_source_series_and_primary_key(tmp_path) -> None:
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    _source(first, "510300.SH", (_bar("510300.SH", "P_Min1", "2020-01-02 09:30:00", 4.0),))
    _source(second, "159919.SZ", (_bar("159919.SZ", "P_Min1", "2020-01-02 09:30:00", 4.1),))
    target = tmp_path / "pool.sqlite3"

    reports = merge_pool_cache(
        target_path=target,
        series=(
            SeriesSource("159919.SZ", "P_Min1", "S_Unsplit", second),
            SeriesSource("510300.SH", "P_Min1", "S_Unsplit", first),
        ),
    )

    assert [item["symbol"] for item in reports] == ["159919.SZ", "510300.SH"]
    assert all(item["source_stats"] == item["target_stats"] for item in reports)
    with sqlite3.connect(target) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 2


def test_merge_pool_cache_refuses_to_overwrite_existing_target(tmp_path) -> None:
    target = tmp_path / "pool.sqlite3"
    target.write_bytes(b"owned")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        merge_pool_cache(target_path=target, series=())
    assert target.read_bytes() == b"owned"


def test_merge_pool_cache_is_byte_deterministic_for_same_sources(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    _source(
        source,
        "510300.SH",
        (
            _bar("510300.SH", "P_Min1", "2020-01-02 09:30:00", 4.0),
            _bar("510300.SH", "P_Min1", "2020-01-02 09:31:00", 4.1),
        ),
    )
    series = (SeriesSource("510300.SH", "P_Min1", "S_Unsplit", source),)
    first = tmp_path / "first-pool.sqlite3"
    second = tmp_path / "second-pool.sqlite3"

    merge_pool_cache(target_path=first, series=series)
    merge_pool_cache(target_path=second, series=series)

    assert sha256_file(first) == sha256_file(second)
