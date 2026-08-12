"""Pure same-base derivation for QMT GICS3 sector composite bars."""

from __future__ import annotations

from datetime import date, datetime, time
import math
from numbers import Integral
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json


QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT = (
    "SIX_CONTIGUOUS_COMPLETED_5M_COMPOSITE_BARS"
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_REQUIRED = ("date", "open", "high", "low", "close", "volume")


def _session_five_minute_closes(session: date) -> tuple[datetime, ...]:
    return tuple(
        datetime.combine(
            session,
            time(hour=minute // 60, minute=minute % 60),
            tzinfo=_SHANGHAI,
        )
        for start, end in (
            (9 * 60 + 35, 11 * 60 + 30),
            (13 * 60 + 5, 15 * 60),
        )
        for minute in range(start, end + 1, 5)
    )


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
        ~numeric.map(math.isfinite).all(axis=1)
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
    """Aggregate only six clock-aligned, completed 5m composite bars.

    A count-bounded provider may return a suffix of the oldest session.  That
    session is discarded in full; it is never re-anchored into counterfeit
    30-minute buckets.  The newest session may be a prefix, in which case only
    complete six-bar buckets are emitted.
    """

    if request_bars is not None and (
        type(request_bars) is not int or request_bars <= 0
    ):
        raise ValueError("request_bars must be a positive integer")
    source_attrs = dict(getattr(five_minute_frame, "attrs", {}))
    normalized = _normalized_five_minute_frame(five_minute_frame)
    accepted: list[pd.DataFrame] = []
    grouped = tuple(
        normalized.groupby(normalized["date"].dt.date, sort=True)
    ) if not normalized.empty else ()
    for index, (session, rows) in enumerate(grouped):
        ordered = rows.sort_values("date", kind="stable").reset_index(drop=True)
        expected = _session_five_minute_closes(session)
        actual = tuple(
            normalize_datetime(
                pd.Timestamp(value).to_pydatetime(),
                "sector 5m close",
            )
            for value in ordered["date"]
        )
        if actual == expected:
            accepted.append(ordered)
            continue
        if index == 0 and actual == expected[-len(actual) :]:
            # 最早的计数边界后缀没有交易日开盘锚点。
            continue
        if index == len(grouped) - 1 and actual == expected[: len(actual)]:
            accepted.append(ordered)
            continue
        raise ValueError("sector 5m base is not a completed calendar-grid prefix")

    rows_for_revision = (
        pd.concat(accepted, ignore_index=True)
        if accepted
        else pd.DataFrame(columns=normalized.columns)
    )
    has_member_mask = "member_mask" in rows_for_revision.columns
    base_revision = sha256_json(
        {
            "schema": "chanlun-qmt-sector-five-minute-derived-base",
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
            "rows": tuple(
                {
                    "date": normalize_datetime(
                        pd.Timestamp(row.date).to_pydatetime(),
                        "sector 5m close",
                    ),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                    **(
                        {"member_mask": int(row.member_mask)}
                        if has_member_mask
                        else {}
                    ),
                }
                for row in rows_for_revision.itertuples(index=False)
            ),
        }
    )

    output: list[pd.DataFrame] = []
    for rows in accepted:
        complete_count = len(rows) // 6 * 6
        if complete_count == 0:
            continue
        complete = rows.iloc[:complete_count].copy()
        complete["bucket"] = complete.index // 6
        output.append(
            complete.groupby("bucket", sort=True)
            .agg(
                date=("date", "last"),
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
        # 这是分桶内保守的五分钟最小覆盖量，不是虚构的三十分钟全成员成交量。
                volume=("volume", "min"),
            )
            .reset_index(drop=True)
        )
    result = (
        pd.concat(output, ignore_index=True)
        if output
        else pd.DataFrame(columns=_REQUIRED)
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
)
