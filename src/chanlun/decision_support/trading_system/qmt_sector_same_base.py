"""QMT GICS3 板块合成柱的纯同源推导。"""

from __future__ import annotations

import hashlib
from numbers import Integral

import numpy as np
import pandas as pd

from chanlun.decision_support.fingerprints import sha256_json


QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT = (
    "SIX_CONTIGUOUS_COMPLETED_5M_COMPOSITE_BARS"
)
_REQUIRED = ("date", "open", "high", "low", "close", "volume")
_MINUTE_IN_NANOSECONDS = 60_000_000_000
_FIVE_MINUTE_OFFSETS = np.concatenate(
    (
        np.arange(9 * 60 + 35, 11 * 60 + 31, 5, dtype=np.int64),
        np.arange(13 * 60 + 5, 15 * 60 + 1, 5, dtype=np.int64),
    )
) * _MINUTE_IN_NANOSECONDS
_MEMBER_PATH_REVISION_SCHEMA = (
    "chanlun-qmt-sector-composite-member-path-v2-little-endian-vector"
)
_DERIVED_BASE_REVISION_SCHEMA = (
    "chanlun-qmt-sector-five-minute-derived-base-v2-little-endian-vector"
)


def _update_length_prefixed(digest: object, segment: bytes) -> None:
    digest.update(len(segment).to_bytes(8, byteorder="big", signed=False))
    digest.update(segment)


def qmt_sector_member_path_revision(frame: pd.DataFrame) -> str | None:
    """Return the shared exact date/member-mask provenance identity."""

    if frame.empty:
        return None
    if "member_mask" not in frame.columns or "date" not in frame.columns:
        raise ValueError("sector composite member path is unavailable")
    dates = pd.to_datetime(frame["date"], errors="raise")
    if dates.dt.tz is None:
        raise ValueError("sector composite member path dates must be timezone-aware")
    raw_masks = tuple(frame["member_mask"])
    if any(
        isinstance(mask, bool)
        or not isinstance(mask, Integral)
        or int(mask) < 0
        or int(mask) >= 1 << 64
        for mask in raw_masks
    ):
        raise ValueError("sector composite member masks must be uint64 integers")
    timestamps = np.asarray(
        pd.DatetimeIndex(dates).tz_convert("UTC").asi8,
        dtype="<i8",
    )
    masks = np.asarray(raw_masks, dtype="<u8")
    metadata = sha256_json(
        {
            "schema": _MEMBER_PATH_REVISION_SCHEMA,
            "row_count": len(frame),
            "timestamp_encoding": "utc-nanoseconds-int64-little-endian",
            "member_mask_encoding": "uint64-little-endian",
        }
    ).encode("ascii")
    digest = hashlib.sha256()
    for segment in (
        _MEMBER_PATH_REVISION_SCHEMA.encode("ascii"),
        metadata,
        timestamps.tobytes(order="C"),
        masks.tobytes(order="C"),
    ):
        _update_length_prefixed(digest, segment)
    return f"sha256:{digest.hexdigest()}"


def _derived_base_revision(
    frame: pd.DataFrame,
    *,
    source_attrs: dict[str, object],
) -> str:
    metadata = sha256_json(
        {
            "schema": _DERIVED_BASE_REVISION_SCHEMA,
            "sector_id": source_attrs.get("sector_id"),
            "sector_membership_revision": source_attrs.get(
                "sector_membership_revision"
            ),
            "sector_composite_members": source_attrs.get(
                "sector_composite_members"
            ),
            "sector_composite_member_path_revision": source_attrs.get(
                "sector_composite_member_path_revision"
            ),
            "row_count": len(frame),
            "value_columns": tuple(_REQUIRED[1:]),
            "has_member_mask": "member_mask" in frame.columns,
        }
    ).encode("ascii")
    timestamps = (
        np.empty(0, dtype="<i8")
        if frame.empty
        else np.asarray(
            pd.DatetimeIndex(frame["date"]).tz_convert("UTC").asi8,
            dtype="<i8",
        )
    )
    values = np.ascontiguousarray(
        frame.loc[:, list(_REQUIRED[1:])].to_numpy(
            dtype=np.float64,
            copy=True,
        ),
        dtype="<f8",
    )
    values[values == 0.0] = 0.0
    masks = (
        np.asarray(tuple(frame["member_mask"]), dtype="<u8")
        if "member_mask" in frame.columns
        else np.empty(0, dtype="<u8")
    )
    digest = hashlib.sha256()
    for segment in (
        _DERIVED_BASE_REVISION_SCHEMA.encode("ascii"),
        metadata,
        timestamps.tobytes(order="C"),
        values.tobytes(order="C"),
        masks.tobytes(order="C"),
    ):
        _update_length_prefixed(digest, segment)
    return f"sha256:{digest.hexdigest()}"


def _normalized_five_minute_frame(value: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame) or value.empty:
        return pd.DataFrame(columns=_REQUIRED)
    missing = set(_REQUIRED).difference(value.columns)
    if missing:
        raise ValueError("sector 5m base is missing required columns")
    optional = ("member_mask",) if "member_mask" in value.columns else ()
    result = value.loc[:, [*_REQUIRED, *optional]].copy()
    result["date"] = pd.to_datetime(result["date"], errors="raise")
    if result["date"].dt.tz is None:
        raise ValueError("sector 5m base must be timezone-aware")
    result["date"] = result["date"].dt.tz_convert("Asia/Shanghai")
    if result["date"].duplicated().any() or not result["date"].is_monotonic_increasing:
        raise ValueError("sector 5m base must be strictly chronological")
    for field in ("open", "high", "low", "close", "volume"):
        result[field] = pd.to_numeric(result[field], errors="raise")
    numeric = result.loc[:, ["open", "high", "low", "close", "volume"]].astype(
        float
    )
    prices = numeric.loc[:, ["open", "high", "low", "close"]]
    invalid = (
        ~np.isfinite(numeric.to_numpy()).all(axis=1)
        | (prices <= 0).any(axis=1)
        | (numeric["volume"] < 0)
        | (numeric["high"] < prices.max(axis=1))
        | (numeric["low"] > prices.min(axis=1))
    )
    if bool(invalid.any()):
        raise ValueError("sector 5m base contains invalid market facts")
    result.loc[:, ["open", "high", "low", "close", "volume"]] = numeric
    if "member_mask" in result:
        masks = tuple(result["member_mask"])
        if any(
            isinstance(mask, bool) or not isinstance(mask, Integral)
            for mask in masks
        ):
            raise ValueError("sector 5m member masks must be exact integers")
        result.loc[:, "member_mask"] = tuple(int(mask) for mask in masks)
    return result


def derive_qmt_sector_thirty_minute_frame(
    five_minute_frame: pd.DataFrame,
    *,
    request_bars: int | None = None,
) -> pd.DataFrame:
    """只聚合六根时钟对齐且已经完成的五分钟合成柱。

    按数量截断的数据源可能只返回最早交易日的后半段；该交易日必须整体
    丢弃，绝不能重新锚定成伪造的三十分钟桶。最新交易日可以是不完整前缀，
    此时只输出包含完整六根柱的桶。
    """

    if request_bars is not None and (
        type(request_bars) is not int or request_bars <= 0
    ):
        raise ValueError("request_bars must be a positive integer")
    source_attrs = dict(getattr(five_minute_frame, "attrs", {}))
    normalized = _normalized_five_minute_frame(five_minute_frame)
    accepted_rows = np.zeros(len(normalized), dtype=bool)
    if normalized.empty:
        session_ranges: tuple[tuple[int, int], ...] = ()
        minute_offsets = np.empty(0, dtype=np.int64)
    else:
        completion_ns = normalized["date"].array.asi8
        session_ns = normalized["date"].dt.normalize().array.asi8
        minute_offsets = completion_ns - session_ns
        changes = np.flatnonzero(session_ns[1:] != session_ns[:-1]) + 1
        starts = np.concatenate((np.asarray((0,)), changes))
        ends = np.concatenate((changes, np.asarray((len(normalized),))))
        session_ranges = tuple(zip(starts.tolist(), ends.tolist()))
    for index, (start, end) in enumerate(session_ranges):
        actual = minute_offsets[start:end]
        observed_count = end - start
        if np.array_equal(actual, _FIVE_MINUTE_OFFSETS):
            accepted_rows[start:end] = True
            continue
        if (
            index == 0
            and observed_count <= len(_FIVE_MINUTE_OFFSETS)
            and np.array_equal(
                actual,
                _FIVE_MINUTE_OFFSETS[-observed_count:],
            )
        ):
            # 最早的计数边界后缀没有交易日开盘锚点。
            continue
        if (
            index == len(session_ranges) - 1
            and observed_count <= len(_FIVE_MINUTE_OFFSETS)
            and np.array_equal(
                actual,
                _FIVE_MINUTE_OFFSETS[:observed_count],
            )
        ):
            accepted_rows[start:end] = True
            continue
        raise ValueError("sector 5m base is not a completed calendar-grid prefix")

    rows_for_revision = normalized.loc[accepted_rows].reset_index(drop=True)
    base_revision = _derived_base_revision(
        rows_for_revision,
        source_attrs=source_attrs,
    )
    if rows_for_revision.empty:
        result = pd.DataFrame(columns=_REQUIRED)
    else:
        sessions = rows_for_revision["date"].dt.normalize()
        grouped_sessions = rows_for_revision.groupby(sessions, sort=False)
        positions = grouped_sessions.cumcount()
        session_sizes = grouped_sessions["date"].transform("size")
        complete_mask = positions < (session_sizes // 6 * 6)
        complete = rows_for_revision.loc[
            complete_mask,
            list(_REQUIRED),
        ].copy()
        complete["_session"] = sessions.loc[complete_mask].array
        complete["_bucket"] = (positions.loc[complete_mask] // 6).array
        result = (
            complete.groupby(["_session", "_bucket"], sort=True)
            .agg(
                date=("date", "last"),
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                # Conservative minimum contributor coverage in the bucket.
                volume=("volume", "min"),
            )
            .reset_index(drop=True)
            .loc[:, list(_REQUIRED)]
        )
    if request_bars is not None:
        result = result.tail(request_bars).reset_index(drop=True)
    result.attrs = {
        **source_attrs,
        "source_base_stream_revision": base_revision,
        "source_base_frequency": "5m",
        "derived_frequency": "30m",
        "sector_thirty_minute_derivation_contract": (
            QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT
        ),
    }
    return result


__all__ = (
    "QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT",
    "derive_qmt_sector_thirty_minute_frame",
    "qmt_sector_member_path_revision",
)
