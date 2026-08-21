"""Read-only adapters for QMT's local A-share research cache.

The running QMT RPC service is preferable because it owns the vendor format.
For research replay, however, a quote process may be alive while the Python
RPC endpoint is unavailable.  QMT's local minute/daily files and the
``PershareIndex`` cache are fixed-size records, so they can be read without
restarting or logging in to the trading terminal.

This is deliberately a fail-closed adapter:

* callers must provide the data directory explicitly (or through the dedicated
  environment variable);
* unknown sentinels, record sizes, timestamps, or price geometry raise a
  format error;
* no file is downloaded, repaired, inferred, or written;
* the format is research-only and can never promote a result above
  ``RESEARCH_ONLY / LIVE_DISABLED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import hashlib
import math
import os
from pathlib import Path
from typing import Final

import numpy as np
import pandas as pd

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.backtest.pit_metadata import CN


QMT_LOCAL_DATA_ENV: Final = "CHANLUN_QMT_LOCAL_DATA_DIR"
QMT_LOCAL_CACHE_SCHEMA: Final = "chanlun-qmt-local-fixed-record"
_KLINE_SENTINEL: Final = bytes.fromhex("feffffffffffff7f")
_KLINE_RECORD_BYTES: Final = 64
_PERSHARE_RECORD_BYTES: Final = 344
_QMT_MISSING_FLOAT: Final = float.fromhex("0x1.fffffffffffffp+1023")
_DERIVED_30M_GRID_REVISION: Final = "QMT_LOCAL_COMPLETED_5M_TO_30M"

_PERIOD_DIRECTORIES: Final = {
    "1m": "60",
    "5m": "300",
    "15m": "900",
    "30m": "1800",
    "60m": "3600",
    "1h": "3600",
    "1d": "86400",
}

_KLINE_DTYPE = np.dtype(
    {
        "names": ("time", "open", "high", "low", "close", "volume", "amount"),
        "formats": ("<i4", "<i4", "<i4", "<i4", "<i4", "<i4", "<i8"),
        "offsets": (0, 4, 8, 12, 16, 24, 32),
        "itemsize": _KLINE_RECORD_BYTES,
    }
)

_PERSHARE_FIELDS: Final = (
    "operating_cash_flow_per_share",
    "book_value_per_share",
    "basic_eps",
    "diluted_eps",
    "undistributed_profit_per_share",
    "capital_reserve_per_share",
    "adjusted_eps",
    "roe",
    "sales_gross_margin",
    "revenue_yoy",
    "profit_yoy",
    "parent_profit_yoy",
    "adjusted_profit_yoy",
    "total_revenue_sequential",
    "parent_profit_sequential",
    "adjusted_profit_sequential",
    "weighted_roe",
    "diluted_roe",
    "total_asset_return",
    "gross_margin",
    "net_margin",
    "effective_tax_rate",
    "advance_receipts_to_revenue",
    "sales_cash_flow_to_revenue",
    "debt_ratio",
    "inventory_turnover",
    "reserved_metric",
)


class QMTLocalCacheFormatError(ValueError):
    """The local vendor file does not match the frozen read-only contract."""


def _sha256_bytes(payload: bytes) -> str:
    """Bind provenance to the exact immutable byte snapshot being parsed."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def resolve_qmt_local_data_dir(
    value: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve an explicit local QMT ``datadir`` without probing processes."""

    raw = value if value is not None else os.environ.get(QMT_LOCAL_DATA_ENV)
    if raw is None or not str(raw).strip():
        return None
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"QMT local data directory does not exist: {path}")
    return path


def _native_parts(code: str) -> tuple[str, str]:
    values = str(code).strip().upper().split(".")
    if len(values) != 2:
        raise ValueError("project code must be MARKET.NUMBER")
    market, number = values
    if market not in {"SH", "SZ", "BJ"} or not number.isdigit():
        raise ValueError("QMT local cache supports SH/SZ/BJ A-share codes only")
    return market, number


@dataclass(frozen=True, slots=True)
class QMTLocalKlineAudit:
    code: str
    frequency: str
    source_path: str
    source_sha256: str
    source_record_count: int
    selected_record_count: int
    first_at: datetime | None
    last_at: datetime | None
        # 物理文件边界与 ``first_at`` / ``last_at`` 相互独立；后两者只描述调用方
        # 选取的窗口，而这里的字段会把完整 QMT 文件覆盖范围绑定到审计标识中。
    source_first_at: datetime | None = None
    source_last_at: datetime | None = None
    schema: str = QMT_LOCAL_CACHE_SCHEMA
    data_grade: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"

    @property
    def audit_id(self) -> str:
        return sha256_json(
            {
                "schema": self.schema,
                "code": self.code,
                "frequency": self.frequency,
                "source_sha256": self.source_sha256,
                "source_record_count": self.source_record_count,
                "selected_record_count": self.selected_record_count,
                "first_at": self.first_at,
                "last_at": self.last_at,
                "source_first_at": self.source_first_at,
                "source_last_at": self.source_last_at,
            }
        )


def qmt_local_kline_path(data_dir: Path, code: str, frequency: str) -> Path:
    market, number = _native_parts(code)
    try:
        period = _PERIOD_DIRECTORIES[frequency]
    except KeyError as exc:
        raise ValueError(f"unsupported QMT local frequency: {frequency}") from exc
    return data_dir / market / period / f"{number}.DAT"


def read_qmt_local_kline(
    *,
    data_dir: str | os.PathLike[str] | Path,
    code: str,
    frequency: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[pd.DataFrame, QMTLocalKlineAudit]:
    """Read raw completed QMT bars from one immutable local cache file."""

    directory = resolve_qmt_local_data_dir(data_dir)
    assert directory is not None
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise ValueError("QMT local cache boundaries must be timezone-aware")
    if start_at > end_at:
        raise ValueError("QMT local cache start cannot follow end")
    path = qmt_local_kline_path(directory, code, frequency)
    if not path.is_file():
        empty = pd.DataFrame(columns=("time", "open", "high", "low", "close", "volume"))
        return empty, QMTLocalKlineAudit(
            code=code,
            frequency=frequency,
            source_path=str(path),
            source_sha256="MISSING",
            source_record_count=0,
            selected_record_count=0,
            first_at=None,
            last_at=None,
            source_first_at=None,
            source_last_at=None,
        )

    # QMT 终端运行期间可能追加或替换缓存文件。因此，先用内存映射解析、再按路径
    # 计算哈希，可能会把返回的 K 线绑定到另一代文件。这里一次性读取字节，并从
    # 同一份字节推导结构、物理边界和 SHA256。
    payload = path.read_bytes()
    size = len(payload)
    if (
        size < len(_KLINE_SENTINEL)
        or (size - len(_KLINE_SENTINEL)) % _KLINE_RECORD_BYTES
    ):
        raise QMTLocalCacheFormatError(
            f"QMT kline file has an unsupported length: {path} ({size})"
        )
    sentinel = payload[: len(_KLINE_SENTINEL)]
    if sentinel != _KLINE_SENTINEL:
        raise QMTLocalCacheFormatError(f"QMT kline sentinel changed: {path}")

    count = (size - len(_KLINE_SENTINEL)) // _KLINE_RECORD_BYTES
    values = np.frombuffer(
        payload,
        dtype=_KLINE_DTYPE,
        offset=len(_KLINE_SENTINEL),
        count=count,
    )
    source_first_at = (
        None
        if count == 0
        else datetime.fromtimestamp(int(values["time"][0]), tz=CN)
    )
    source_last_at = (
        None
        if count == 0
        else datetime.fromtimestamp(int(values["time"][-1]), tz=CN)
    )
    if (
        source_first_at is not None
        and source_last_at is not None
        and source_first_at > source_last_at
    ):
        raise QMTLocalCacheFormatError(
            f"QMT kline physical boundaries are inverted: {path}"
        )
    start_seconds = math.floor(start_at.timestamp())
    end_seconds = math.floor(end_at.timestamp())
    mask = (values["time"] >= start_seconds) & (values["time"] <= end_seconds)
    selected = values[mask]
    if selected.size:
        timestamps = selected["time"].astype("int64", copy=True)
        if np.any(np.diff(timestamps) <= 0):
            raise QMTLocalCacheFormatError(
                f"QMT kline timestamps are not strictly increasing: {path}"
            )
        prices = np.column_stack(
            tuple(
                selected[field].astype("float64", copy=True) / 1000.0
                for field in ("open", "high", "low", "close")
            )
        )
        valid = (
            np.all(np.isfinite(prices), axis=1)
            & np.all(prices > 0, axis=1)
            & (prices[:, 1] >= np.max(prices[:, (0, 3)], axis=1))
            & (prices[:, 2] <= np.min(prices[:, (0, 3)], axis=1))
            & (prices[:, 1] >= prices[:, 2])
            & (selected["volume"] >= 0)
        )
        if not np.all(valid):
            raise QMTLocalCacheFormatError(
                f"QMT kline contains invalid OHLCV records in requested range: {path}"
            )
    # QMT 本地定长记录以“手”保存 A 股成交量，而 xtdata 对外暴露“股”；
    # 这里保持公共接口原始成交量的单位。
        frame = pd.DataFrame(
            {
                "time": timestamps * 1000,
                "open": prices[:, 0],
                "high": prices[:, 1],
                "low": prices[:, 2],
                "close": prices[:, 3],
                "volume": selected["volume"].astype("float64", copy=True) * 100.0,
                "amount": selected["amount"].astype("float64", copy=True),
            }
        )
        first_at = datetime.fromtimestamp(int(timestamps[0]), tz=CN)
        last_at = datetime.fromtimestamp(int(timestamps[-1]), tz=CN)
    else:
        frame = pd.DataFrame(columns=("time", "open", "high", "low", "close", "volume"))
        first_at = None
        last_at = None
    del values
    audit = QMTLocalKlineAudit(
        code=code,
        frequency=frequency,
        source_path=str(path),
        source_sha256=_sha256_bytes(payload),
        source_record_count=count,
        selected_record_count=len(frame),
        first_at=first_at,
        last_at=last_at,
        source_first_at=source_first_at,
        source_last_at=source_last_at,
    )
    frame.attrs.update(
        qmt_transport="LOCAL_FIXED_RECORD_READ_ONLY",
        qmt_local_cache_audit_id=audit.audit_id,
        qmt_local_cache_source_sha256=audit.source_sha256,
    )
    return frame, audit


def _derive_completed_30m_reference(frame: pd.DataFrame) -> pd.DataFrame:
    """仅聚合同一交易时段内连续、完整的六根 QMT 五分钟线。

    本适配器只为板块研究数据补齐三十分钟行情；本地 QMT 缓存包含一分钟和
    五分钟目录，但没有独立的三十分钟目录。聚合不会跨越午休，也不会填补
    缺失的五分钟线。个股信号仍只由一分钟原始数据构建的直接递归结构决定。
    """

    required = {"time", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"QMT 5m frame is missing columns: {sorted(missing)!r}")
    if frame.empty:
        result = pd.DataFrame(columns=tuple(frame.columns))
        result.attrs = dict(frame.attrs)
        result.attrs.update(
            qmt_transport="LOCAL_5M_DERIVED_30M_READ_ONLY",
            qmt_derived_grid_revision=_DERIVED_30M_GRID_REVISION,
            data_grade="RESEARCH_ONLY",
            live_status="LIVE_DISABLED",
        )
        return result

    work = frame.copy()
    work["_date"] = pd.to_datetime(work["time"], unit="ms", utc=True).dt.tz_convert(CN)
    if work["_date"].duplicated().any() or not work["_date"].is_monotonic_increasing:
        raise QMTLocalCacheFormatError("QMT 5m timestamps must be unique and ordered")

    rows: list[dict[str, float | int]] = []
    for _session, session_rows in work.groupby(work["_date"].dt.date, sort=True):
        ordered = session_rows.sort_values("_date", kind="stable")
        for side, opening_hour, opening_minute in (
            ("morning", 9, 35),
            ("afternoon", 13, 5),
        ):
            del side  # 仅用于说明两个交易时段，不写入结果字段
            anchor = ordered["_date"].iloc[0].replace(
                hour=opening_hour,
                minute=opening_minute,
                second=0,
                microsecond=0,
            )
            expected = tuple(anchor + timedelta(minutes=5 * index) for index in range(24))
            lookup = {value: position for position, value in enumerate(ordered["_date"])}
            for bucket in range(4):
                times = expected[bucket * 6 : (bucket + 1) * 6]
                if any(value not in lookup for value in times):
                    continue
                positions = [lookup[value] for value in times]
                values = ordered.iloc[positions]
                row: dict[str, float | int] = {
                    "time": int(values.iloc[-1]["time"]),
                    "open": float(values.iloc[0]["open"]),
                    "high": float(values["high"].max()),
                    "low": float(values["low"].min()),
                    "close": float(values.iloc[-1]["close"]),
                    "volume": float(values["volume"].sum()),
                }
                if "amount" in values.columns:
                    row["amount"] = float(values["amount"].sum())
                rows.append(row)
    columns = tuple(value for value in frame.columns if value != "_date")
    result = pd.DataFrame(rows)
    if result.empty:
        result = pd.DataFrame(columns=columns)
    else:
        result = result.loc[:, [value for value in columns if value in result.columns]]
        result = result.sort_values("time", kind="stable").reset_index(drop=True)
    result.attrs = dict(frame.attrs)
    result.attrs.update(
        qmt_transport="LOCAL_5M_DERIVED_30M_READ_ONLY",
        qmt_derived_grid_revision=_DERIVED_30M_GRID_REVISION,
        qmt_derived_from_frequency="5m",
        data_grade="RESEARCH_ONLY",
        live_status="LIVE_DISABLED",
    )
    return result


def derive_completed_30m_from_qmt_5m(frame: pd.DataFrame) -> pd.DataFrame:
    """Vectorized, fail-closed 5m-to-30m exchange-session aggregation."""

    required = {"time", "open", "high", "low", "close", "volume"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"QMT 5m frame is missing columns: {sorted(missing)!r}")
    if frame.empty:
        result = pd.DataFrame(columns=tuple(frame.columns))
        result.attrs = dict(frame.attrs)
        result.attrs.update(
            qmt_transport="LOCAL_5M_DERIVED_30M_READ_ONLY",
            qmt_derived_grid_revision=_DERIVED_30M_GRID_REVISION,
            data_grade="RESEARCH_ONLY",
            live_status="LIVE_DISABLED",
        )
        return result

    work = frame.copy()
    work["_date"] = pd.to_datetime(work["time"], unit="ms", utc=True).dt.tz_convert(CN)
    if work["_date"].duplicated().any() or not work["_date"].is_monotonic_increasing:
        raise QMTLocalCacheFormatError("QMT 5m timestamps must be unique and ordered")

    minute_of_day = work["_date"].dt.hour * 60 + work["_date"].dt.minute
    morning_offset = minute_of_day - (9 * 60 + 35)
    afternoon_offset = minute_of_day - (13 * 60 + 5)
    morning = morning_offset.between(0, 23 * 5) & morning_offset.mod(5).eq(0)
    afternoon = afternoon_offset.between(0, 23 * 5) & afternoon_offset.mod(5).eq(0)
    valid = morning | afternoon
    work = work.loc[valid].copy()

    if work.empty:
        result = pd.DataFrame()
    else:
        morning_valid = morning.loc[valid]
        work["_session"] = work["_date"].dt.normalize()
        work["_side"] = np.where(morning_valid.to_numpy(), 0, 1)
        work["_slot"] = np.where(
            morning_valid.to_numpy(),
            (morning_offset.loc[valid] // 5).to_numpy(),
            (afternoon_offset.loc[valid] // 5).to_numpy(),
        ).astype("int16")
        work["_bucket"] = (work["_slot"] // 6).astype("int8")

        group_columns = ["_session", "_side", "_bucket"]
        grouped = work.groupby(group_columns, sort=True, observed=True)
        completeness = grouped["_slot"].agg(["count", "nunique", "min", "max"])
        expected_first = (
            completeness.index.get_level_values("_bucket").to_numpy(dtype="int64")
            * 6
        )
        complete = (
            (completeness["count"].to_numpy() == 6)
            & (completeness["nunique"].to_numpy() == 6)
            & (completeness["min"].to_numpy() == expected_first)
            & (completeness["max"].to_numpy() == expected_first + 5)
        )
        aggregations: dict[str, str] = {
            "time": "last",
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
        if "amount" in work.columns:
            aggregations["amount"] = "sum"
        grouped_rows = grouped.agg(aggregations)
        result = grouped_rows.iloc[np.flatnonzero(complete)].reset_index(drop=True)

    columns = tuple(value for value in frame.columns if value != "_date")
    if result.empty:
        result = pd.DataFrame(columns=columns)
    else:
        result = result.loc[:, [value for value in columns if value in result.columns]]
        result = result.sort_values("time", kind="stable").reset_index(drop=True)
    result.attrs = dict(frame.attrs)
    result.attrs.update(
        qmt_transport="LOCAL_5M_DERIVED_30M_READ_ONLY",
        qmt_derived_grid_revision=_DERIVED_30M_GRID_REVISION,
        qmt_derived_from_frequency="5m",
        data_grade="RESEARCH_ONLY",
        live_status="LIVE_DISABLED",
    )
    return result


def derive_completed_30m_with_audit(
    frame: pd.DataFrame,
    source_audit: QMTLocalKlineAudit,
) -> tuple[pd.DataFrame, QMTLocalKlineAudit]:
    """Derive 30m bars from one already-frozen 5m byte snapshot."""

    if source_audit.frequency != "5m":
        raise ValueError("derived 30m audit requires a 5m source audit")
    derived = derive_completed_30m_from_qmt_5m(frame)
    audit = QMTLocalKlineAudit(
        code=source_audit.code,
        frequency="30m_from_5m",
        source_path=source_audit.source_path,
        source_sha256=source_audit.source_sha256,
        source_record_count=source_audit.source_record_count,
        selected_record_count=len(derived),
        first_at=(
            None
            if derived.empty
            else datetime.fromtimestamp(int(derived.iloc[0]["time"]) / 1000, tz=CN)
        ),
        last_at=(
            None
            if derived.empty
            else datetime.fromtimestamp(int(derived.iloc[-1]["time"]) / 1000, tz=CN)
        ),
        source_first_at=source_audit.source_first_at,
        source_last_at=source_audit.source_last_at,
    )
    derived.attrs.update(
        qmt_local_cache_audit_id=audit.audit_id,
        qmt_local_cache_source_sha256=audit.source_sha256,
    )
    return derived, audit


def read_qmt_local_derived_30m(
    *,
    data_dir: str | os.PathLike[str] | Path,
    code: str,
    start_at: datetime,
    end_at: datetime,
) -> tuple[pd.DataFrame, QMTLocalKlineAudit]:
    """读取本地五分钟记录，并按因果时序生成已完成的三十分钟线。"""

    five, source_audit = read_qmt_local_kline(
        data_dir=data_dir,
        code=code,
        frequency="5m",
        start_at=start_at,
        end_at=end_at,
    )
    return derive_completed_30m_with_audit(five, source_audit)


@dataclass(frozen=True, slots=True)
class QMTPershareRecord:
    code: str
    report_period: date
    announced_on: date
    known_at: datetime
    values: tuple[tuple[str, float | None], ...]
    source_record_ordinal: int

    def __post_init__(self) -> None:
        if self.known_at.tzinfo is None:
            raise ValueError("QMT financial known_at must be timezone-aware")
        if self.known_at.date() <= self.announced_on:
            raise ValueError(
                "QMT financial record must become visible after announcement day"
            )
        if tuple(name for name, _value in self.values) != _PERSHARE_FIELDS:
            raise ValueError("QMT financial field order changed")

    def get(self, field: str) -> float | None:
        return dict(self.values)[field]


@dataclass(frozen=True, slots=True)
class QMTPershareAudit:
    code: str
    source_path: str
    source_sha256: str
    record_count: int
    first_report_period: date | None
    last_report_period: date | None
    schema: str = "chanlun-qmt-local-pershare-index"
    data_grade: str = "RESEARCH_ONLY"
    live_status: str = "LIVE_DISABLED"


def qmt_local_pershare_path(data_dir: Path, code: str) -> Path:
    market, number = _native_parts(code)
    return data_dir / "Finance" / market / "86400" / f"{number}_7008.DAT"


def _clean_metric(value: float) -> float | None:
    if not math.isfinite(value) or value == _QMT_MISSING_FLOAT:
        return None
    return float(value)


def read_qmt_local_pershare(
    *,
    data_dir: str | os.PathLike[str] | Path,
    code: str,
) -> tuple[tuple[QMTPershareRecord, ...], QMTPershareAudit]:
    """Read disclosure-dated QMT ``PershareIndex`` records."""

    directory = resolve_qmt_local_data_dir(data_dir)
    assert directory is not None
    path = qmt_local_pershare_path(directory, code)
    if not path.is_file():
        return (), QMTPershareAudit(code, str(path), "MISSING", 0, None, None)
    payload = path.read_bytes()
    if len(payload) % _PERSHARE_RECORD_BYTES:
        raise QMTLocalCacheFormatError(
            f"QMT PershareIndex file has an unsupported length: {path} ({len(payload)})"
        )
    dtype = np.dtype(
        {
            "names": ("report_ms", "announce_ms", "metrics"),
            "formats": ("<i8", "<i8", ("<f8", 41)),
            "offsets": (0, 8, 16),
            "itemsize": _PERSHARE_RECORD_BYTES,
        }
    )
    raw = np.frombuffer(payload, dtype=dtype)
    records: list[QMTPershareRecord] = []
    for ordinal, row in enumerate(raw):
        report = datetime.fromtimestamp(int(row["report_ms"]) / 1000, tz=CN).date()
        announced = datetime.fromtimestamp(int(row["announce_ms"]) / 1000, tz=CN).date()
        if not date(1990, 1, 1) <= report <= date(2100, 12, 31):
            raise QMTLocalCacheFormatError(f"QMT report timestamp is invalid: {path}")
        if not report <= announced <= date(2100, 12, 31):
            raise QMTLocalCacheFormatError(
                f"QMT announcement timestamp is invalid: {path}"
            )
        metrics = tuple(
            (name, _clean_metric(float(value)))
            for name, value in zip(
                _PERSHARE_FIELDS, row["metrics"][: len(_PERSHARE_FIELDS)]
            )
        )
    # 缓存只有披露日期，没有日内发布时间。统一在下一自然日零点后可见，
    # 对所有 A 股交易决策都是保守处理。
        known_at = datetime.combine(
            announced + timedelta(days=1),
            datetime.min.time(),
            tzinfo=CN,
        )
        records.append(
            QMTPershareRecord(
                code=code,
                report_period=report,
                announced_on=announced,
                known_at=known_at,
                values=metrics,
                source_record_ordinal=ordinal,
            )
        )
    ordered = tuple(
        sorted(
            records,
            key=lambda value: (
                value.known_at,
                value.report_period,
                value.source_record_ordinal,
            ),
        )
    )
    audit = QMTPershareAudit(
        code=code,
        source_path=str(path),
        source_sha256=_sha256_bytes(payload),
        record_count=len(ordered),
        first_report_period=min((row.report_period for row in ordered), default=None),
        last_report_period=max((row.report_period for row in ordered), default=None),
    )
    return ordered, audit


__all__ = (
    "QMT_LOCAL_CACHE_SCHEMA",
    "QMT_LOCAL_DATA_ENV",
    "QMTLocalCacheFormatError",
    "QMTLocalKlineAudit",
    "QMTPershareAudit",
    "QMTPershareRecord",
    "qmt_local_kline_path",
    "qmt_local_pershare_path",
    "derive_completed_30m_from_qmt_5m",
    "derive_completed_30m_with_audit",
    "read_qmt_local_kline",
    "read_qmt_local_derived_30m",
    "read_qmt_local_pershare",
    "resolve_qmt_local_data_dir",
)
