"""QMT GICS3 catalog and component-derived sector bars for live screening."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import math
import re
from threading import RLock
import unicodedata
from zoneinfo import ZoneInfo

import pandas as pd
from xtquant import xtdata

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.exchange.exchange_qmt import _XTDATA_NATIVE_LOCK
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)


QMT_GICS3_CATALOG_SOURCE = "qmt_gics3_components"
QMT_GICS3_COMPOSITE_PROVIDER = "qmt-gics3-composite"
QMT_GICS3_COMPOSITE_ADJUSTMENT = "none-stable-24-member-median-v2"
QMT_GICS3_COMPOSITE_MEMBER_LIMIT = 24

_GICS3_PREFIX = "GICS3"
_QMT_A_SHARE_CODE = re.compile(r"^([0-9]{6})\.(SH|SZ|BJ)$")
_NORMALIZED_A_SHARE_CODE = re.compile(r"^(SH|SZ|BJ)\.([0-9]{6})$")
_FREQUENCY_SECONDS = {"5m": 5 * 60, "30m": 30 * 60}
_FIELDS = ("time", "open", "high", "low", "close", "volume")
_PRICE_FIELDS = ("open", "high", "low", "close")
_COMPOSITE_QUANTUM = Decimal("0.000001")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


def _canonical_sector_name(value: str) -> tuple[str, str] | None:
    text = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not text.startswith(_GICS3_PREFIX):
        return None
    name = text[len(_GICS3_PREFIX) :].strip()
    return (text, name) if name else None


def _normalized_a_share_code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    match = _QMT_A_SHARE_CODE.fullmatch(value.strip().upper())
    if match is None:
        return None
    digits, market = match.groups()
    return f"{market}.{digits}"


def _qmt_code(value: str) -> str:
    match = _NORMALIZED_A_SHARE_CODE.fullmatch(value)
    if match is None:
        raise ValueError(f"invalid normalized A-share code: {value!r}")
    market, digits = match.groups()
    return f"{digits}.{market}"


def build_qmt_gics3_sector_catalog() -> dict[str, object]:
    """Capture the current QMT GICS3 catalog without any TDX fallback."""

    with _XTDATA_NATIVE_LOCK:
        source_list = xtdata.get_sector_list()
        if (
            type(source_list) is not list
            or not source_list
            or any(type(item) is not str for item in source_list)
        ):
            raise RuntimeError("QMT sector list is unavailable")
        selected: list[tuple[str, str, str]] = []
        canonical_keys: set[str] = set()
        for raw_key in source_list:
            parsed = _canonical_sector_name(raw_key)
            if parsed is None:
                continue
            canonical_key, name = parsed
            if canonical_key in canonical_keys:
                raise RuntimeError(f"duplicate QMT GICS3 sector: {canonical_key}")
            canonical_keys.add(canonical_key)
            selected.append((canonical_key, raw_key, name))
        selected.sort(key=lambda item: item[0])
        captures: list[tuple[str, str, list[object]]] = []
        for canonical_key, raw_key, name in selected:
            response = xtdata.get_stock_list_in_sector(
                raw_key,
                real_timetag=-1,
            )
            if type(response) is not list:
                raise RuntimeError(
                    f"QMT sector membership is unavailable: {canonical_key}"
                )
            captures.append((canonical_key, name, list(response)))

    if not captures:
        raise RuntimeError("QMT GICS3 sector catalog is empty")
    sectors: list[dict[str, object]] = []
    for source_key, name, raw_members in captures:
        members = sorted(
            {
                code
                for code in (
                    _normalized_a_share_code(value) for value in raw_members
                )
                if code is not None
            }
        )
        sector_id = "qmt-gics3:" + sha256_json(
            {
                "schema": "chanlun-qmt-gics3-sector/v1",
                "source_key": source_key,
            }
        ).removeprefix("sha256:")
        sectors.append(
            {
                "sector_id": sector_id,
                "name": name,
                "source_key": source_key,
                "member_codes": members,
            }
        )
    revision = sha256_json(
        {
            "schema": "chanlun-qmt-gics3-catalog/v1",
            "sectors": sectors,
        }
    )
    return {
        "source": QMT_GICS3_CATALOG_SOURCE,
        "catalog_revision": revision,
        "sectors": sectors,
    }


def _empty_composite_frame(
    sector_id: str,
    membership_revision: str,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        columns=("code", "date", "open", "high", "low", "close", "volume")
    )
    metadata = build_provider_price_basis_metadata(
        provider=QMT_GICS3_COMPOSITE_PROVIDER,
        market="a",
        code=f"{sector_id}:{membership_revision}",
        adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
        structure_price_quantum=_COMPOSITE_QUANTUM,
    )
    return attach_price_basis_metadata(frame, metadata)


def _copy_frame(value: pd.DataFrame) -> pd.DataFrame:
    result = value.copy(deep=True)
    result.attrs = dict(value.attrs)
    return result


def _member_ratios(
    raw: Mapping[str, object],
    native_code: str,
    *,
    not_after: datetime,
) -> pd.DataFrame | None:
    values: dict[str, pd.Series] = {}
    shared_columns = None
    for field in _FIELDS:
        source = raw.get(field)
        if not isinstance(source, pd.DataFrame) or source.index.has_duplicates:
            return None
        if native_code not in source.index:
            return None
        if shared_columns is None:
            shared_columns = source.columns
        elif not source.columns.equals(shared_columns):
            return None
        row = source.loc[native_code]
        if not isinstance(row, pd.Series):
            return None
        values[field] = row.reset_index(drop=True)
    frame = pd.DataFrame(values)
    for field in _FIELDS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame = frame.dropna(subset=list(_FIELDS))
    if frame.empty:
        return None
    frame["date"] = pd.to_datetime(frame["time"], unit="ms", utc=True).dt.tz_convert(
        "Asia/Shanghai"
    )
    cutoff = pd.Timestamp(normalize_datetime(not_after, "not_after"))
    prices = frame.loc[:, list(_PRICE_FIELDS)]
    finite = frame.loc[:, list(_FIELDS)].map(
        lambda value: math.isfinite(float(value))
    ).all(axis=1)
    valid = (
        finite
        & (frame["date"] <= cutoff)
        & (prices > 0).all(axis=1)
        & (frame["volume"] >= 0)
        & (frame["high"] >= prices.max(axis=1))
        & (frame["low"] <= prices.min(axis=1))
    )
    frame = frame.loc[valid].sort_values("date")
    frame = frame.drop_duplicates(subset="date", keep="last")
    if len(frame) < 2:
        return None
    previous_close = frame["close"].shift(1)
    result = pd.DataFrame({"date": frame["date"]})
    for field in _PRICE_FIELDS:
        result[f"{field}_ratio"] = frame[field] / previous_close
    result = result.iloc[1:].copy()
    ratios = result.loc[:, [f"{field}_ratio" for field in _PRICE_FIELDS]]
    valid_ratios = ratios.map(lambda value: math.isfinite(float(value))).all(axis=1)
    valid_ratios &= (ratios > 0).all(axis=1)
    result = result.loc[valid_ratios]
    return None if result.empty else result


class QmtSectorCompositeSource:
    """Build deterministic equal-weight median sector bars from QMT members."""

    def __init__(
        self,
        *,
        minimum_member_count: int = 8,
        minimum_bar_coverage: Decimal = Decimal("0.60"),
        maximum_composite_members: int = QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
    ) -> None:
        if type(minimum_member_count) is not int or minimum_member_count <= 0:
            raise ValueError("minimum_member_count must be a positive integer")
        if (
            not isinstance(minimum_bar_coverage, Decimal)
            or not minimum_bar_coverage.is_finite()
            or not Decimal("0") < minimum_bar_coverage <= Decimal("1")
        ):
            raise ValueError("minimum_bar_coverage must be in (0, 1]")
        if (
            type(maximum_composite_members) is not int
            or maximum_composite_members < minimum_member_count
        ):
            raise ValueError(
                "maximum_composite_members must cover minimum_member_count"
            )
        self._minimum_member_count = minimum_member_count
        self._minimum_bar_coverage = minimum_bar_coverage
        self._maximum_composite_members = maximum_composite_members
        self._lock = RLock()
        self._cache: dict[
            tuple[str, str, int], tuple[int, str, pd.DataFrame]
        ] = {}
        self._prepared_bucket: int | None = None
        self._attempted_members: set[str] = set()
        self._trading_dates_cache: tuple[date, tuple[date, ...]] | None = None

    @staticmethod
    def _bucket(as_of: datetime, frequency: str) -> int:
        seconds = _FREQUENCY_SECONDS[frequency]
        epoch = int(normalize_datetime(as_of, "as_of").timestamp())
        return epoch - epoch % seconds

    def _composite_members(
        self,
        sector_id: str,
        members: tuple[str, ...],
    ) -> tuple[str, ...]:
        if len(members) <= self._maximum_composite_members:
            return members
        ranked = sorted(
            members,
            key=lambda code: sha256_json(
                {
                    "schema": "chanlun-qmt-gics3-sample/v1",
                    "sector_id": sector_id,
                    "code": code,
                }
            ),
        )
        return tuple(sorted(ranked[: self._maximum_composite_members]))

    @staticmethod
    def _fresh_native_codes(
        raw: object,
        native_codes: tuple[str, ...],
        expected_closed_at: datetime,
    ) -> set[str]:
        if not isinstance(raw, Mapping):
            return set()
        source = raw.get("time")
        if not isinstance(source, pd.DataFrame):
            return set()
        fresh: set[str] = set()
        for native_code in native_codes:
            if native_code not in source.index:
                continue
            row = source.loc[native_code]
            if not isinstance(row, pd.Series):
                continue
            values = pd.to_numeric(row, errors="coerce").dropna()
            if values.empty:
                continue
            try:
                latest = normalize_datetime(
                    pd.to_datetime(
                        values.iloc[-1], unit="ms", utc=True
                    ).to_pydatetime(),
                    "latest QMT member bar",
                )
            except (OverflowError, TypeError, ValueError):
                continue
            if latest >= expected_closed_at:
                fresh.add(native_code)
        return fresh

    def _prepare_history(
        self,
        *,
        members: tuple[str, ...],
        as_of: datetime,
    ) -> None:
        bucket = self._bucket(as_of, "5m")
        with self._lock:
            if self._prepared_bucket != bucket:
                self._prepared_bucket = bucket
                self._attempted_members = set()
            attempted = set(self._attempted_members)
        native_by_member = {code: _qmt_code(code) for code in members}
        native_codes = tuple(native_by_member.values())
        with _XTDATA_NATIVE_LOCK:
            latest = xtdata.get_market_data(
                field_list=["time"],
                stock_list=list(native_codes),
                period="5m",
                start_time="",
                end_time=as_of.strftime("%Y%m%d%H%M%S"),
                count=1,
                dividend_type="none",
                fill_data=False,
            )
        fresh = self._fresh_native_codes(
            latest,
            native_codes,
            self._expected_closed_at(as_of, "5m"),
        )
        pending = tuple(
            code
            for code in members
            if native_by_member[code] not in fresh and code not in attempted
        )
        completed_attempts: set[str] = set()
        for code in pending:
            try:
                with _XTDATA_NATIVE_LOCK:
                    xtdata.download_history_data(
                        native_by_member[code],
                        "5m",
                        start_time="",
                        end_time="",
                        incrementally=True,
                    )
            except Exception:
                continue
            finally:
                completed_attempts.add(code)
        with self._lock:
            if self._prepared_bucket == bucket:
                self._attempted_members.update(completed_attempts)

    @staticmethod
    def _session_closes(
        trading_day: date,
        frequency: str,
    ) -> tuple[datetime, ...]:
        if frequency == "30m":
            slots = (
                (10, 0),
                (10, 30),
                (11, 0),
                (11, 30),
                (13, 30),
                (14, 0),
                (14, 30),
                (15, 0),
            )
        else:
            slots = tuple(
                (minute // 60, minute % 60)
                for start, end in (
                    (9 * 60 + 35, 11 * 60 + 30),
                    (13 * 60 + 5, 15 * 60),
                )
                for minute in range(start, end + 1, 5)
            )
        return tuple(
            datetime.combine(
                trading_day,
                time(hour=hour, minute=minute),
                tzinfo=_SHANGHAI,
            )
            for hour, minute in slots
        )

    def _trading_dates(self, as_of: datetime) -> tuple[date, ...]:
        observed_day = as_of.date()
        with self._lock:
            cached = self._trading_dates_cache
            if cached is not None and cached[0] == observed_day:
                return cached[1]
            with _XTDATA_NATIVE_LOCK:
                response = xtdata.get_trading_dates(
                    "SH",
                    (as_of - timedelta(days=45)).strftime("%Y%m%d"),
                    as_of.strftime("%Y%m%d"),
                    -1,
                )
            if type(response) is not list or not response:
                raise RuntimeError("QMT trading calendar is unavailable")
            try:
                days = tuple(
                    sorted(
                        {
                            pd.to_datetime(value, unit="ms", utc=True)
                            .tz_convert("Asia/Shanghai")
                            .date()
                            for value in response
                            if not isinstance(value, bool)
                        }
                    )
                )
            except Exception as exc:
                raise RuntimeError("QMT trading calendar is invalid") from exc
            days = tuple(day for day in days if day <= observed_day)
            if not days:
                raise RuntimeError("QMT trading calendar has no prior session")
            self._trading_dates_cache = (observed_day, days)
            return days

    def _expected_closed_at(
        self,
        as_of: datetime,
        frequency: str,
    ) -> datetime:
        candidates = tuple(
            close
            for trading_day in self._trading_dates(as_of)
            for close in self._session_closes(trading_day, frequency)
            if close <= as_of
        )
        if not candidates:
            raise RuntimeError("QMT trading calendar has no closed sector bar")
        return max(candidates)

    def frame(
        self,
        *,
        sector_id: str,
        sector_name: str,
        members: tuple[str, ...],
        frequency: str,
        as_of: datetime,
        request_bars: int,
    ) -> pd.DataFrame:
        if not isinstance(sector_id, str) or not sector_id.startswith("qmt-gics3:"):
            raise ValueError("invalid QMT GICS3 sector id")
        if not isinstance(sector_name, str) or not sector_name.strip():
            raise ValueError("sector_name is required")
        if frequency not in _FREQUENCY_SECONDS:
            raise ValueError("QMT sector frequency must be 5m or 30m")
        if type(request_bars) is not int or request_bars <= 0:
            raise ValueError("request_bars must be a positive integer")
        if (
            type(members) is not tuple
            or len(members) != len(set(members))
            or any(_NORMALIZED_A_SHARE_CODE.fullmatch(code) is None for code in members)
        ):
            raise ValueError("members must be unique normalized A-share codes")
        observed_at = normalize_datetime(as_of, "as_of")
        composite_members = self._composite_members(sector_id, members)
        membership_revision = sha256_json(
            {
                "schema": "chanlun-qmt-gics3-members/v2",
                "sector_id": sector_id,
                "members": members,
                "composite_members": composite_members,
            }
        ).removeprefix("sha256:")
        cache_key = (sector_id, frequency, request_bars)
        bucket = self._bucket(observed_at, frequency)
        with self._lock:
            cached = self._cache.get(cache_key)
            if (
                cached is not None
                and cached[0] == bucket
                and cached[1] == membership_revision
            ):
                return _copy_frame(cached[2])

        empty = _empty_composite_frame(sector_id, membership_revision)
        if len(members) < self._minimum_member_count:
            result = empty
        else:
            self._prepare_history(
                members=composite_members,
                as_of=observed_at,
            )
            native_codes = tuple(_qmt_code(code) for code in composite_members)
            with _XTDATA_NATIVE_LOCK:
                raw = xtdata.get_market_data(
                    field_list=list(_FIELDS),
                    stock_list=list(native_codes),
                    period=frequency,
                    start_time="",
                    end_time=observed_at.strftime("%Y%m%d%H%M%S"),
                    count=request_bars + 32,
                    dividend_type="none",
                    fill_data=False,
                )
            if not isinstance(raw, Mapping):
                result = empty
            else:
                member_frames: list[pd.DataFrame] = []
                for native_code in native_codes:
                    ratios = _member_ratios(
                        raw,
                        native_code,
                        not_after=observed_at,
                    )
                    if ratios is None:
                        continue
                    ratios.insert(0, "member", native_code)
                    member_frames.append(ratios)
                if len(member_frames) < self._minimum_member_count:
                    result = empty
                else:
                    facts = pd.concat(member_frames, ignore_index=True)
                    required_count = max(
                        self._minimum_member_count,
                        math.ceil(
                            len(member_frames)
                            * float(self._minimum_bar_coverage)
                        ),
                    )
                    grouped = facts.groupby("date", sort=True).agg(
                        member_count=("member", "nunique"),
                        open_ratio=("open_ratio", "median"),
                        high_ratio=("high_ratio", "median"),
                        low_ratio=("low_ratio", "median"),
                        close_ratio=("close_ratio", "median"),
                    )
                    grouped = grouped[grouped["member_count"] >= required_count]
                    rows: list[dict[str, object]] = []
                    previous_close = 1000.0
                    for date, item in grouped.iterrows():
                        open_value = previous_close * float(item["open_ratio"])
                        close_value = previous_close * float(item["close_ratio"])
                        high_value = max(
                            previous_close * float(item["high_ratio"]),
                            open_value,
                            close_value,
                        )
                        low_value = min(
                            previous_close * float(item["low_ratio"]),
                            open_value,
                            close_value,
                        )
                        rows.append(
                            {
                                "code": sector_id,
                                "date": date,
                                "open": open_value,
                                "high": high_value,
                                "low": low_value,
                                "close": close_value,
                                "volume": float(item["member_count"]),
                            }
                        )
                        previous_close = close_value
                    result = pd.DataFrame(rows).tail(request_bars).reset_index(drop=True)
                    expected_closed_at = self._expected_closed_at(
                        observed_at,
                        frequency,
                    )
                    actual_closed_at = (
                        None
                        if result.empty
                        else normalize_datetime(
                            pd.Timestamp(result["date"].iloc[-1]).to_pydatetime(),
                            "sector bar close",
                        )
                    )
                    if actual_closed_at != expected_closed_at:
                        result = empty
                    else:
                        metadata = build_provider_price_basis_metadata(
                            provider=QMT_GICS3_COMPOSITE_PROVIDER,
                            market="a",
                            code=f"{sector_id}:{membership_revision}",
                            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
                            structure_price_quantum=_COMPOSITE_QUANTUM,
                        )
                        result = attach_price_basis_metadata(result, metadata)

        if not result.empty:
            with self._lock:
                self._cache[cache_key] = (
                    bucket,
                    membership_revision,
                    _copy_frame(result),
                )
        return _copy_frame(result)


__all__ = (
    "QMT_GICS3_CATALOG_SOURCE",
    "QMT_GICS3_COMPOSITE_ADJUSTMENT",
    "QMT_GICS3_COMPOSITE_MEMBER_LIMIT",
    "QMT_GICS3_COMPOSITE_PROVIDER",
    "QmtSectorCompositeSource",
    "build_qmt_gics3_sector_catalog",
)
