"""Causal all-member SW1 composites for the certified QMT replay."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import math
from typing import Mapping, Sequence

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataIndex,
    PITMetadataSnapshot,
    qmt_native_code,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    read_qmt_local_derived_30m,
    resolve_qmt_local_data_dir,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)


PIT_SW1_COMPOSITE_PROVIDER = "qmt-sw1-pit-composite"
PIT_SW1_COMPOSITE_ADJUSTMENT = "causal-all-member-median-v1"
_FIELDS = ("time", "open", "high", "low", "close", "volume")
_PRICES = ("open", "high", "low", "close")
_QUANTUM = Decimal("0.000001")


def _empty(sector_id: str) -> pd.DataFrame:
    frame = pd.DataFrame(
        columns=("code", "date", "open", "high", "low", "close", "volume")
    )
    metadata = build_provider_price_basis_metadata(
        provider=PIT_SW1_COMPOSITE_PROVIDER,
        market="a",
        code=sector_id,
        adjustment=PIT_SW1_COMPOSITE_ADJUSTMENT,
        structure_price_quantum=_QUANTUM,
    )
    return attach_price_basis_metadata(frame, metadata)


def _sector_at(
    index: PITMetadataIndex,
    code: str,
    observed_at: datetime,
) -> str | None:
    row = index.membership_at(code, observed_at)
    return None if row is None else row.sector_id


def _causal_member_ratios(
    *,
    index: PITMetadataIndex,
    code: str,
    frame: pd.DataFrame,
    sector_id: str,
    start_at: datetime,
    end_at: datetime,
) -> pd.DataFrame | None:
    if frame.empty:
        return None
    work = frame.copy()
    for field in _FIELDS:
        work[field] = pd.to_numeric(work[field], errors="coerce")
    work = work.dropna(subset=list(_FIELDS))
    work = work[
        (work["time"] > 0)
        & (work["open"] > 0)
        & (work["high"] > 0)
        & (work["low"] > 0)
        & (work["close"] > 0)
        & (work["volume"] >= 0)
    ].copy()
    if work.empty:
        return None
    work["date"] = pd.to_datetime(
        work.pop("time"), unit="ms", utc=True
    ).dt.tz_convert("Asia/Shanghai")
    work = work.sort_values("date").drop_duplicates("date", keep="last")
    factor = pd.Series(1.0, index=work.index, dtype="float64")
    sessions = work["date"].map(lambda value: value.date())
    for event in index.factors_for(code):
        factor.loc[sessions >= event.effective_on] *= float(
            event.raw_price_divisor
        )
    for field in _PRICES:
        work[field] *= factor
    work["previous_close"] = work["close"].shift(1)
    work["current_sector"] = work["date"].map(
        lambda value: _sector_at(index, code, value.to_pydatetime())
    )
    work["previous_sector"] = work["current_sector"].shift(1)
    cutoff_start = pd.Timestamp(normalize_datetime(start_at, "start_at"))
    cutoff_end = pd.Timestamp(normalize_datetime(end_at, "end_at"))
    work = work[
        (work["date"] >= cutoff_start)
        & (work["date"] <= cutoff_end)
        & (work["current_sector"] == sector_id)
        & (work["previous_sector"] == sector_id)
        & work["previous_close"].notna()
    ].copy()
    if work.empty:
        return None
    output = pd.DataFrame({"date": work["date"]})
    for field in _PRICES:
        output[f"{field}_ratio"] = work[field] / work["previous_close"]
    ratios = output[[f"{field}_ratio" for field in _PRICES]]
    finite = ratios.map(lambda value: math.isfinite(float(value))).all(axis=1)
    output = output[finite & (ratios > 0).all(axis=1)]
    if output.empty:
        return None
    output.insert(0, "member", code)
    return output


def composite_from_member_frames(
    *,
    snapshot: PITMetadataSnapshot,
    sector_id: str,
    member_frames: Mapping[str, pd.DataFrame],
    start_at: datetime,
    end_at: datetime,
    minimum_member_count: int = 8,
    minimum_bar_coverage: Decimal = Decimal("0.60"),
) -> pd.DataFrame:
    if minimum_member_count <= 0:
        raise ValueError("minimum_member_count must be positive")
    if not Decimal("0") < minimum_bar_coverage <= Decimal("1"):
        raise ValueError("minimum_bar_coverage must be in (0, 1]")
    facts: list[pd.DataFrame] = []
    index = PITMetadataIndex(snapshot)
    for code, frame in sorted(member_frames.items()):
        ratios = _causal_member_ratios(
            index=index,
            code=code,
            frame=frame,
            sector_id=sector_id,
            start_at=start_at,
            end_at=end_at,
        )
        if ratios is not None:
            facts.append(ratios)
    if not facts:
        return _empty(sector_id)
    joined = pd.concat(facts, ignore_index=True)
    candidate_codes = tuple(sorted(member_frames))
    dates = tuple(sorted(set(joined["date"])))
    eligible_count = {
        observed_at: sum(
            _sector_at(index, code, observed_at.to_pydatetime()) == sector_id
            for code in candidate_codes
        )
        for observed_at in dates
    }
    grouped = joined.groupby("date", sort=True).agg(
        member_count=("member", "nunique"),
        open_ratio=("open_ratio", "median"),
        high_ratio=("high_ratio", "median"),
        low_ratio=("low_ratio", "median"),
        close_ratio=("close_ratio", "median"),
    )
    grouped["eligible_count"] = [eligible_count[index] for index in grouped.index]
    grouped["required_count"] = grouped["eligible_count"].map(
        lambda count: max(
            minimum_member_count,
            math.ceil(count * float(minimum_bar_coverage)),
        )
    )
    grouped = grouped[grouped["member_count"] >= grouped["required_count"]]
    rows: list[dict[str, object]] = []
    previous_close = 1000.0
    for observed_at, item in grouped.iterrows():
        opened = previous_close * float(item["open_ratio"])
        closed = previous_close * float(item["close_ratio"])
        high = max(previous_close * float(item["high_ratio"]), opened, closed)
        low = min(previous_close * float(item["low_ratio"]), opened, closed)
        rows.append(
            {
                "code": sector_id,
                "date": observed_at,
                "open": opened,
                "high": high,
                "low": low,
                "close": closed,
                "volume": float(item["member_count"]),
            }
        )
        previous_close = closed
    result = pd.DataFrame(rows)
    if result.empty:
        return _empty(sector_id)
    metadata = build_provider_price_basis_metadata(
        provider=PIT_SW1_COMPOSITE_PROVIDER,
        market="a",
        code=sector_id,
        adjustment=PIT_SW1_COMPOSITE_ADJUSTMENT,
        structure_price_quantum=_QUANTUM,
    )
    return attach_price_basis_metadata(result.reset_index(drop=True), metadata)


def _candidate_codes(
    snapshot: PITMetadataSnapshot,
    sector_id: str,
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                row.code
                for row in snapshot.memberships
                if row.sector_id == sector_id
            }
        )
    )


def _load_qmt_member_frames(
    *,
    codes: Sequence[str],
    start_at: datetime,
    end_at: datetime,
    chunk_size: int = 256,
) -> dict[str, pd.DataFrame]:
    output: dict[str, pd.DataFrame] = {}
    read_start = normalize_datetime(start_at, "start_at") - timedelta(days=10)
    read_end = normalize_datetime(end_at, "end_at")
    local_data_dir = resolve_qmt_local_data_dir()
    if local_data_dir is not None:
        for code in codes:
            frame, _audit = read_qmt_local_derived_30m(
                data_dir=local_data_dir,
                code=code,
                start_at=read_start,
                end_at=read_end,
            )
            if not frame.empty:
                output[code] = frame.loc[:, list(_FIELDS)].copy()
        return output

    from xtquant import xtdata

    native_by_code = {code: qmt_native_code(code) for code in codes}
    for offset in range(0, len(codes), chunk_size):
        chunk = tuple(codes[offset : offset + chunk_size])
        native_codes = [native_by_code[code] for code in chunk]
        raw = xtdata.get_market_data(
            field_list=list(_FIELDS),
            stock_list=native_codes,
            period="30m",
            start_time=read_start.strftime("%Y%m%d%H%M%S"),
            end_time=read_end.strftime("%Y%m%d%H%M%S"),
            count=-1,
            dividend_type="none",
            fill_data=False,
        )
        if not isinstance(raw, Mapping):
            continue
        for code in chunk:
            native = native_by_code[code]
            values: dict[str, pd.Series] = {}
            for field in _FIELDS:
                matrix = raw.get(field)
                if not isinstance(matrix, pd.DataFrame) or native not in matrix.index:
                    values = {}
                    break
                values[field] = matrix.loc[native].reset_index(drop=True)
            if values:
                output[code] = pd.DataFrame(values)
    return output


def build_pit_sw1_composite(
    *,
    snapshot: PITMetadataSnapshot,
    sector_id: str,
    start_at: datetime,
    end_at: datetime,
) -> pd.DataFrame:
    codes = _candidate_codes(snapshot, sector_id)
    if not codes:
        return _empty(sector_id)
    frames = _load_qmt_member_frames(
        codes=codes,
        start_at=start_at,
        end_at=end_at,
    )
    return composite_from_member_frames(
        snapshot=snapshot,
        sector_id=sector_id,
        member_frames=frames,
        start_at=start_at,
        end_at=end_at,
    )


__all__ = (
    "PIT_SW1_COMPOSITE_ADJUSTMENT",
    "PIT_SW1_COMPOSITE_PROVIDER",
    "build_pit_sw1_composite",
    "composite_from_member_frames",
)
