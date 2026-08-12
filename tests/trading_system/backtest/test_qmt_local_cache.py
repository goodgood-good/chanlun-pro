from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from pathlib import Path
import struct

import pytest

from chanlun.decision_support.trading_system.backtest import qmt_local_cache as subject
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    QMTLocalCacheFormatError,
    read_qmt_local_derived_30m,
    read_qmt_local_kline,
    read_qmt_local_pershare,
)
from tests.trading_system.helpers import CN


SENTINEL = bytes.fromhex("feffffffffffff7f")


def _write_kline(
    path: Path, rows: tuple[tuple[int, int, int, int, int, int], ...]
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = bytearray(SENTINEL)
    for timestamp, open_tick, high_tick, low_tick, close_tick, volume_lots in rows:
        record = bytearray(64)
        struct.pack_into("<i", record, 0, timestamp)
        struct.pack_into("<iiii", record, 4, open_tick, high_tick, low_tick, close_tick)
        struct.pack_into("<i", record, 24, volume_lots)
        struct.pack_into("<q", record, 32, 123_456)
        payload.extend(record)
    path.write_bytes(payload)


def test_local_kline_reads_only_requested_rows_and_converts_lots(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 7, 24, 9, 30, tzinfo=CN)
    rows = tuple(
        (
            int(start.timestamp()) + offset * 60,
            10_000 + offset,
            10_100 + offset,
            9_900 + offset,
            10_050 + offset,
            10 + offset,
        )
        for offset in range(3)
    )
    _write_kline(tmp_path / "SZ" / "60" / "000001.DAT", rows)

    frame, audit = read_qmt_local_kline(
        data_dir=tmp_path,
        code="SZ.000001",
        frequency="1m",
        start_at=start.replace(minute=31),
        end_at=start.replace(minute=32),
    )

    assert frame["time"].tolist() == [rows[1][0] * 1000, rows[2][0] * 1000]
    assert frame["close"].tolist() == [10.051, 10.052]
    assert frame["volume"].tolist() == [1100.0, 1200.0]
    assert audit.source_record_count == 3
    assert audit.selected_record_count == 2
    assert audit.source_first_at == start
    assert audit.source_last_at == start + timedelta(minutes=2)
    assert audit.first_at == start + timedelta(minutes=1)
    assert audit.last_at == start + timedelta(minutes=2)
    assert audit.source_sha256.startswith("sha256:")
    assert frame.attrs["qmt_transport"] == "LOCAL_FIXED_RECORD_READ_ONLY"


def test_local_kline_hash_binds_the_exact_parsed_byte_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live QMT file replacement cannot split bars from their source hash."""

    start = datetime(2026, 7, 24, 9, 30, tzinfo=CN)
    path = tmp_path / "SZ" / "60" / "000001.DAT"
    original = (
        (int(start.timestamp()), 10_000, 10_100, 9_900, 10_050, 10),
    )
    replacement = (
        (int(start.timestamp()), 20_000, 20_100, 19_900, 20_050, 20),
    )
    _write_kline(path, original)
    original_payload = path.read_bytes()
    real_read_bytes = Path.read_bytes
    mutation_count = 0

    def read_then_replace(value: Path) -> bytes:
        nonlocal mutation_count
        payload = real_read_bytes(value)
        if value == path and mutation_count == 0:
            mutation_count += 1
            _write_kline(path, replacement)
        return payload

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)
    frame, audit = subject.read_qmt_local_kline(
        data_dir=tmp_path,
        code="SZ.000001",
        frequency="1m",
        start_at=start,
        end_at=start,
    )

    assert mutation_count == 1
    assert frame["close"].tolist() == [10.05]
    assert audit.source_sha256 == (
        "sha256:" + hashlib.sha256(original_payload).hexdigest()
    )
    with path.open("rb") as handle:
        assert hashlib.sha256(handle.read()).hexdigest() != (
            audit.source_sha256.removeprefix("sha256:")
        )


def test_local_kline_rejects_changed_sentinel_or_invalid_ohlc(tmp_path: Path) -> None:
    start = datetime(2026, 7, 24, 9, 30, tzinfo=CN)
    path = tmp_path / "SH" / "60" / "600000.DAT"
    _write_kline(
        path,
        ((int(start.timestamp()), 10_000, 9_900, 9_800, 10_050, 10),),
    )
    with pytest.raises(QMTLocalCacheFormatError, match="invalid OHLCV"):
        read_qmt_local_kline(
            data_dir=tmp_path,
            code="SH.600000",
            frequency="1m",
            start_at=start,
            end_at=start,
        )

    payload = bytearray(path.read_bytes())
    payload[:8] = b"changed!"
    path.write_bytes(payload)
    with pytest.raises(QMTLocalCacheFormatError, match="sentinel"):
        read_qmt_local_kline(
            data_dir=tmp_path,
            code="SH.600000",
            frequency="1m",
            start_at=start,
            end_at=start,
        )


def _pershare_record(
    report: datetime,
    announced: datetime,
    metrics: tuple[float, ...],
) -> bytes:
    values = metrics + (0.0,) * (41 - len(metrics))
    return struct.pack(
        "<2q41d",
        int(report.timestamp() * 1000),
        int(announced.timestamp() * 1000),
        *values,
    )


def test_local_pershare_is_visible_only_after_announcement_day(tmp_path: Path) -> None:
    path = tmp_path / "Finance" / "SZ" / "86400" / "000001_7008.DAT"
    path.parent.mkdir(parents=True)
    missing = float.fromhex("0x1.fffffffffffffp+1023")
    metrics = (1.2, 5.0, 0.8, missing, 1.0, 2.0, 0.7, 12.0, 30.0, 8.0)
    path.write_bytes(
        _pershare_record(
            datetime(2025, 12, 31, tzinfo=CN),
            datetime(2026, 3, 20, tzinfo=CN),
            metrics,
        )
    )

    records, audit = read_qmt_local_pershare(
        data_dir=tmp_path,
        code="SZ.000001",
    )

    assert len(records) == 1
    [record] = records
    assert record.report_period.isoformat() == "2025-12-31"
    assert record.announced_on.isoformat() == "2026-03-20"
    assert record.known_at == datetime(2026, 3, 21, tzinfo=CN)
    assert record.get("book_value_per_share") == 5.0
    assert record.get("diluted_eps") is None
    assert audit.record_count == 1
    assert audit.data_grade == "RESEARCH_ONLY"


def test_local_pershare_hash_binds_the_exact_parsed_byte_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "Finance" / "SZ" / "86400" / "000001_7008.DAT"
    path.parent.mkdir(parents=True)
    original = _pershare_record(
        datetime(2025, 12, 31, tzinfo=CN),
        datetime(2026, 3, 20, tzinfo=CN),
        (1.2, 5.0),
    )
    replacement = _pershare_record(
        datetime(2025, 12, 31, tzinfo=CN),
        datetime(2026, 3, 20, tzinfo=CN),
        (9.9, 8.8),
    )
    path.write_bytes(original)
    real_read_bytes = Path.read_bytes
    mutation_count = 0

    def read_then_replace(value: Path) -> bytes:
        nonlocal mutation_count
        payload = real_read_bytes(value)
        if value == path and mutation_count == 0:
            mutation_count += 1
            path.write_bytes(replacement)
        return payload

    monkeypatch.setattr(Path, "read_bytes", read_then_replace)
    records, audit = subject.read_qmt_local_pershare(
        data_dir=tmp_path,
        code="SZ.000001",
    )

    assert mutation_count == 1
    assert records[0].get("book_value_per_share") == 5.0
    assert audit.source_sha256 == "sha256:" + hashlib.sha256(original).hexdigest()


def test_missing_local_files_are_explicit_empty_facts(tmp_path: Path) -> None:
    start = datetime(2026, 7, 24, 9, 30, tzinfo=CN)
    frame, kline_audit = read_qmt_local_kline(
        data_dir=tmp_path,
        code="SZ.000001",
        frequency="1m",
        start_at=start,
        end_at=start,
    )
    records, financial_audit = read_qmt_local_pershare(
        data_dir=tmp_path,
        code="SZ.000001",
    )

    assert frame.empty and kline_audit.source_sha256 == "MISSING"
    assert records == () and financial_audit.source_sha256 == "MISSING"


def test_local_30m_is_derived_only_from_complete_same_session_5m_buckets(
    tmp_path: Path,
) -> None:
    session = datetime(2026, 7, 24, tzinfo=CN)
    ends = (
        *(session.replace(hour=9, minute=35) + timedelta(minutes=5 * index) for index in range(24)),
        *(session.replace(hour=13, minute=5) + timedelta(minutes=5 * index) for index in range(24)),
    )
    rows = tuple(
        (
            int(value.timestamp()),
            10_000 + index,
            10_100 + index,
            9_900 + index,
            10_050 + index,
            10 + index,
        )
        for index, value in enumerate(ends)
        if value.time() != datetime(2026, 7, 24, 10, 10).time()
    )
    _write_kline(tmp_path / "SZ" / "300" / "000001.DAT", rows)

    frame, audit = read_qmt_local_derived_30m(
        data_dir=tmp_path,
        code="SZ.000001",
        start_at=session.replace(hour=9, minute=30),
        end_at=session.replace(hour=15, minute=0),
    )

    # One missing 10:10 bar invalidates only its six-bar 10:30 bucket.  No
    # bucket may bridge the 11:30--13:00 exchange recess.
    assert len(frame) == 7
    closes = tuple(
        datetime.fromtimestamp(int(value) / 1000, tz=CN).strftime("%H:%M")
        for value in frame["time"]
    )
    assert closes == ("10:00", "11:00", "11:30", "13:30", "14:00", "14:30", "15:00")
    assert audit.frequency == "30m_from_5m"
    assert audit.selected_record_count == 7
    assert frame.attrs["qmt_transport"] == "LOCAL_5M_DERIVED_30M_READ_ONLY"
    assert frame.attrs["data_grade"] == "RESEARCH_ONLY"
