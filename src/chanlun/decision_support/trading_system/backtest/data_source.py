from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import re
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.backtest.models import (
    BacktestDataset,
    CorporateActionAt,
    MinuteBar,
    SectorMembershipAt,
    SecurityStatus,
)
from chanlun.decision_support.trading_system.context import classify_context
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    SectorAssessment,
    StructuralPoint,
)
from chanlun.decision_support.trading_system.provisional import (
    ProvisionalCandidate,
    extract_provisional_candidates,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.decision_support.trading_system.runtime_config import (
    strict_cl_config,
    strict_snapshot_price_metadata,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)


CN = ZoneInfo("Asia/Shanghai")
CODE_RE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
TABLE_RE = re.compile(r"^a_klines_(?:sh|sz|bj)_\d{4}$")
SECTOR_INDEX_RE = re.compile(r"^SH\.880\d{3}$")


@dataclass(frozen=True, slots=True)
class DailyMarketRow:
    code: str
    session: date
    opened: Decimal
    high: Decimal
    low: Decimal
    closed: Decimal
    volume: Decimal


@dataclass(frozen=True, slots=True)
class NativeSectorBar:
    sector_id: str
    index_code: str
    frequency: Literal["1m", "5m", "30m"]
    opened_at: datetime
    closed_at: datetime
    opened: Decimal
    high: Decimal
    low: Decimal
    closed: Decimal
    volume: Decimal
    source: Literal["tdx_native_880_index"] = "tdx_native_880_index"

    def __post_init__(self) -> None:
        if SECTOR_INDEX_RE.fullmatch(self.index_code) is None:
            raise ValueError("native sector bar requires a TDX 880 index code")
        opened_at = normalize_datetime(self.opened_at, "opened_at")
        closed_at = normalize_datetime(self.closed_at, "closed_at")
        if opened_at >= closed_at:
            raise ValueError("sector opened_at must precede closed_at")
        if any(
            value <= 0
            for value in (self.opened, self.high, self.low, self.closed)
        ):
            raise ValueError("sector prices must be positive")
        if (
            self.low > min(self.opened, self.closed)
            or self.high < max(self.opened, self.closed)
            or self.low > self.high
        ):
            raise ValueError("sector OHLC range is inconsistent")
        if self.volume < 0:
            raise ValueError("sector volume cannot be negative")
        object.__setattr__(self, "opened_at", opened_at)
        object.__setattr__(self, "closed_at", closed_at)


@dataclass(frozen=True, slots=True)
class MembershipLoad:
    records: tuple[SectorMembershipAt, ...]
    as_of_each_session: bool
    sector_names: tuple[tuple[str, str], ...]
    sector_index_codes: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        record_keys = tuple(
            (row.session, row.sector_id, row.code) for row in self.records
        )
        if len(record_keys) != len(set(record_keys)):
            raise ValueError("duplicate membership load record")
        for values, label in (
            (self.sector_names, "sector name"),
            (self.sector_index_codes, "sector index"),
        ):
            keys = tuple(key for key, _value in values)
            if len(keys) != len(set(keys)):
                raise ValueError(f"duplicate {label} key")


@dataclass(frozen=True, slots=True)
class BacktestDataConfig:
    start: date
    end: date
    codes: tuple[str, ...] = ()
    qmt_chunk_size: int = 20
    statuses: tuple[SecurityStatus, ...] = ()
    memberships: tuple[SectorMembershipAt, ...] = ()
    corporate_actions: tuple[CorporateActionAt, ...] = ()
    membership_as_of_each_session: bool = False
    security_status_as_of_each_session: bool = False
    point_in_time_adjustment: bool = False
    catalog: object | None = None

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError("start cannot follow end")
        if self.qmt_chunk_size <= 0 or self.qmt_chunk_size > 100:
            raise ValueError("qmt_chunk_size must be in [1, 100]")
        if len(self.codes) != len(set(self.codes)):
            raise ValueError("codes must be unique")
        if any(CODE_RE.fullmatch(code) is None for code in self.codes):
            raise ValueError("codes must be normalized A-share identifiers")


def _connect() -> object:
    from chanlun import config as project_config
    import pymysql

    if project_config.DB_TYPE != "mysql":
        raise RuntimeError("historical data source requires MySQL")
    return pymysql.connect(
        host=project_config.DB_HOST,
        port=project_config.DB_PORT,
        user=project_config.DB_USER,
        password=project_config.DB_PWD,
        database=project_config.DB_DATABASE,
        charset="utf8mb4",
        autocommit=False,
        cursorclass=pymysql.cursors.DictCursor,
    )


def open_read_only_connection() -> object:
    connection = _connect()
    with connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute("SET SESSION TRANSACTION READ ONLY")
    return connection


def _available_tables(connection: object) -> tuple[str, ...]:
    with connection.cursor() as cursor:  # type: ignore[attr-defined]
        cursor.execute("SHOW TABLES LIKE 'a_klines_%'")
        rows = cursor.fetchall()
    values: list[str] = []
    for row in rows:
        value = next(iter(row.values()))
        if isinstance(value, str) and TABLE_RE.fullmatch(value) is not None:
            values.append(value)
    return tuple(sorted(values))


def _decimal(value: object) -> Decimal:
    converted = Decimal(str(value))
    if not converted.is_finite():
        raise ValueError("market value must be finite")
    return converted


def _as_session(value: object) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def load_daily_rows(start: date, end: date) -> tuple[DailyMarketRow, ...]:
    if start > end:
        raise ValueError("start cannot follow end")
    connection = open_read_only_connection()
    output: list[DailyMarketRow] = []
    try:
        tables = _available_tables(connection)
        start_at = datetime.combine(start, time.min)
        end_at = datetime.combine(end + timedelta(days=1), time.min)
        sql = (
            "SELECT code, DATE(dt) AS session, "
            "SUBSTRING_INDEX(GROUP_CONCAT(o ORDER BY dt ASC), ',', 1) AS opened, "
            "MAX(h) AS high, MIN(l) AS low, "
            "SUBSTRING_INDEX(GROUP_CONCAT(c ORDER BY dt DESC), ',', 1) AS closed, "
            "SUM(v) AS volume FROM `{table}` "
            "WHERE f=%s AND dt >= %s AND dt < %s "
            "GROUP BY code, DATE(dt) ORDER BY code, DATE(dt)"
        )
        for table in tables:
            if TABLE_RE.fullmatch(table) is None:
                raise ValueError("unsafe historical table name")
            with connection.cursor() as cursor:  # type: ignore[attr-defined]
                cursor.execute(
                    sql.format(table=table),
                    ("5m", start_at, end_at),
                )
                rows = cursor.fetchall()
            for row in rows:
                code = str(row["code"]).upper()
                if CODE_RE.fullmatch(code) is None:
                    continue
                output.append(
                    DailyMarketRow(
                        code=code,
                        session=_as_session(row["session"]),
                        opened=_decimal(row["opened"]),
                        high=_decimal(row["high"]),
                        low=_decimal(row["low"]),
                        closed=_decimal(row["closed"]),
                        volume=_decimal(row["volume"]),
                    )
                )
    finally:
        rollback = getattr(connection, "rollback", None)
        if callable(rollback):
            rollback()
        close = getattr(connection, "close", None)
        if callable(close):
            close()
    return tuple(sorted(output, key=lambda row: (row.session, row.code)))


def _normalize_frame_date(value: object) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("market timestamp cannot be missing")
    if timestamp.tzinfo is None:
        return timestamp.tz_localize(CN)
    return timestamp.tz_convert(CN)


def _load_qmt_frames(
    *,
    codes: tuple[str, ...],
    start: date,
    end: date,
    chunk_size: int,
) -> dict[str, pd.DataFrame]:
    from chanlun.exchange.exchange_qmt import _XTDATA_NATIVE_LOCK, xtdata

    xtdata.enable_hello = False
    native_to_project = {
        f"{code.split('.', 1)[1]}.{code.split('.', 1)[0]}": code
        for code in codes
    }
    native_codes = tuple(native_to_project)
    start_text = datetime.combine(start, time.min).strftime("%Y%m%d%H%M%S")
    end_text = datetime.combine(end, time(15, 0)).strftime("%Y%m%d%H%M%S")
    fields = ("time", "open", "high", "low", "close", "volume")
    frames: dict[str, pd.DataFrame] = {}
    for offset in range(0, len(native_codes), chunk_size):
        chunk = native_codes[offset : offset + chunk_size]
        with _XTDATA_NATIVE_LOCK:
            xtdata.download_history_data2(
                list(chunk),
                "1m",
                start_time=start_text,
                end_time=end_text,
                incrementally=True,
            )
            raw = xtdata.get_market_data(
                field_list=list(fields),
                stock_list=list(chunk),
                period="1m",
                start_time=start_text,
                end_time=end_text,
                count=-1,
                dividend_type="none",
                fill_data=False,
            )
        if not isinstance(raw, Mapping):
            continue
        for native_code in chunk:
            columns: dict[str, object] = {}
            for field in fields:
                matrix = raw.get(field)
                if not isinstance(matrix, pd.DataFrame) or native_code not in matrix.index:
                    break
                columns[field] = matrix.loc[native_code]
            if len(columns) != len(fields):
                continue
            frame = pd.DataFrame(columns)
            for field in fields:
                frame[field] = pd.to_numeric(frame[field], errors="coerce")
            frame = frame.dropna(subset=list(fields))
            frame = frame[
                (frame["time"] > 0)
                & (frame["open"] > 0)
                & (frame["high"] > 0)
                & (frame["low"] > 0)
                & (frame["close"] > 0)
                & (frame["volume"] >= 0)
            ].copy()
            frame["date"] = pd.to_datetime(
                frame.pop("time"),
                unit="ms",
                utc=True,
            ).dt.tz_convert(CN)
            frames[native_to_project[native_code]] = frame[
                ["date", "open", "high", "low", "close", "volume"]
            ].sort_values("date", kind="stable").reset_index(drop=True)
    return frames


def load_qmt_minute_bars(
    codes: Sequence[str],
    *,
    start: date,
    end: date,
    chunk_size: int,
) -> tuple[MinuteBar, ...]:
    normalized = tuple(codes)
    if any(CODE_RE.fullmatch(code) is None for code in normalized):
        raise ValueError("QMT codes must be normalized A-share identifiers")
    if chunk_size <= 0 or chunk_size > 100:
        raise ValueError("chunk_size must be in [1, 100]")
    if not normalized:
        return ()
    frames = _load_qmt_frames(
        codes=normalized,
        start=start,
        end=end,
        chunk_size=chunk_size,
    )
    output: list[MinuteBar] = []
    for code in sorted(frames):
        frame = frames[code].copy()
        frame["date"] = frame["date"].map(_normalize_frame_date)
        frame = frame.sort_values("date", kind="stable").drop_duplicates(
            "date",
            keep="last",
        )
        previous_session_close: Decimal | None = None
        for session, rows in frame.groupby(frame["date"].map(lambda value: value.date())):
            del session
            ordered = rows.sort_values("date", kind="stable")
            first_open = _decimal(ordered.iloc[0]["open"])
            limit_reference = previous_session_close or first_open
            for row in ordered.itertuples(index=False):
                opened_at = _normalize_frame_date(row.date).to_pydatetime()
                closed_at = opened_at + timedelta(minutes=1)
                opened = _decimal(row.open)
                high = _decimal(row.high)
                low = _decimal(row.low)
                closed = _decimal(row.close)
                volume = _decimal(row.volume)
                output.append(
                    MinuteBar(
                        code=code,
                        opened_at=opened_at,
                        closed_at=closed_at,
                        raw_open=opened,
                        raw_high=high,
                        raw_low=low,
                        raw_close=closed,
                        analysis_open=opened,
                        analysis_high=high,
                        analysis_low=low,
                        analysis_close=closed,
                        previous_raw_close=limit_reference,
                        volume=volume,
                        turnover=closed * volume,
                        adjustment_known_at=closed_at,
                    )
                )
            previous_session_close = _decimal(ordered.iloc[-1]["close"])
    return tuple(sorted(output, key=lambda bar: (bar.closed_at, bar.code)))


def _load_current_catalog() -> object:
    from chanlun.decision_support.tdx_industry_sectors import (
        build_tdx_industry_sector_catalog,
    )
    from chanlun.exchange.stocks_bkgn import StocksBKGN

    return build_tdx_industry_sector_catalog(StocksBKGN().file_bkgns())


def load_sector_memberships(
    *,
    codes: tuple[str, ...],
    sessions: tuple[date, ...],
    catalog: object | None = None,
    historical_records: tuple[SectorMembershipAt, ...] | None = None,
) -> MembershipLoad:
    if historical_records is not None:
        records = tuple(
            sorted(
                (
                    row
                    for row in historical_records
                    if row.code in codes and row.session in sessions
                ),
                key=lambda row: (row.session, row.sector_id, row.code),
            )
        )
        sectors = sorted({row.sector_id for row in records})
        return MembershipLoad(
            records=records,
            as_of_each_session=True,
            sector_names=tuple((sector_id, sector_id) for sector_id in sectors),
            sector_index_codes=tuple(
                (sector_id, sector_id.rsplit(":", 1)[-1])
                for sector_id in sectors
            ),
        )
    raw_catalog = _load_current_catalog() if catalog is None else catalog
    if not isinstance(raw_catalog, Mapping):
        raise TypeError("sector catalog must be a mapping")
    raw_sectors = raw_catalog.get("sectors")
    if not isinstance(raw_sectors, list):
        raise ValueError("sector catalog must expose sectors")
    code_by_digits = {code.split(".", 1)[1]: code for code in codes}
    records: list[SectorMembershipAt] = []
    names: dict[str, str] = {}
    indices: dict[str, str] = {}
    for item in raw_sectors:
        if not isinstance(item, Mapping):
            continue
        sector_id = item.get("sector_id")
        name = item.get("name")
        index_code = item.get("kline_code")
        members = item.get("member_codes")
        if (
            not isinstance(sector_id, str)
            or not isinstance(name, str)
            or not isinstance(index_code, str)
            or SECTOR_INDEX_RE.fullmatch(index_code) is None
            or isinstance(members, (str, bytes))
            or not isinstance(members, Sequence)
        ):
            continue
        names[sector_id] = name
        indices[sector_id] = index_code
        project_codes = tuple(
            sorted(
                {
                    code_by_digits[digits]
                    for digits in members
                    if isinstance(digits, str) and digits in code_by_digits
                }
            )
        )
        for session in sessions:
            known_at = datetime.combine(session, time.min, tzinfo=CN)
            records.extend(
                SectorMembershipAt(
                    session=session,
                    sector_id=sector_id,
                    code=code,
                    known_at=known_at,
                )
                for code in project_codes
            )
    return MembershipLoad(
        records=tuple(
            sorted(records, key=lambda row: (row.session, row.sector_id, row.code))
        ),
        as_of_each_session=False,
        sector_names=tuple(sorted(names.items())),
        sector_index_codes=tuple(sorted(indices.items())),
    )


def _read_native_sector_frames(
    *,
    sector_indices: Mapping[str, str],
    start: date,
    end: date,
    max_pages: int,
) -> dict[str, dict[str, pd.DataFrame]]:
    from chanlun.exchange.exchange_tdx import ExchangeTDX
    from pytdx.hq import TdxHq_API

    exchange = ExchangeTDX()
    connect_info = getattr(exchange, "connect_info", None)
    if not isinstance(connect_info, Mapping):
        raise RuntimeError("TDX connection is unavailable")
    api = TdxHq_API(raise_exception=True, auto_retry=True)
    result: dict[str, dict[str, pd.DataFrame]] = {}
    categories = {"1m": 8, "5m": 0, "30m": 2}
    with api.connect(str(connect_info["ip"]), int(connect_info["port"])):
        for sector_id, code in sorted(sector_indices.items()):
            if SECTOR_INDEX_RE.fullmatch(code) is None:
                raise ValueError("sector index must be a native TDX 880 code")
            by_frequency: dict[str, pd.DataFrame] = {}
            digits = code.split(".", 1)[1]
            for frequency, category in categories.items():
                pages: list[pd.DataFrame] = []
                for page in range(max_pages):
                    raw = api.to_df(
                        api.get_index_bars(category, 1, digits, page * 700, 700)
                    )
                    if raw.empty:
                        break
                    pages.append(raw)
                    if pd.Timestamp(raw["datetime"].min()).date() <= start:
                        break
                if not pages:
                    continue
                frame = pd.concat(pages, ignore_index=True)
                frame["date"] = pd.to_datetime(frame["datetime"])
                frame["date"] = frame["date"].map(_normalize_frame_date)
                frame = frame[
                    (frame["date"].map(lambda value: value.date()) >= start)
                    & (frame["date"].map(lambda value: value.date()) <= end)
                ].copy()
                if frame.empty:
                    continue
                frame["volume"] = frame["vol"]
                by_frequency[frequency] = frame[
                    ["date", "open", "high", "low", "close", "volume"]
                ].drop_duplicates("date", keep="last").sort_values("date")
            if by_frequency:
                result[sector_id] = by_frequency
    return result


def load_tdx_native_sector_bars(
    *,
    sector_indices: Mapping[str, str],
    start: date,
    end: date,
    max_pages: int,
) -> tuple[NativeSectorBar, ...]:
    if max_pages <= 0:
        raise ValueError("max_pages must be positive")
    frames = _read_native_sector_frames(
        sector_indices=sector_indices,
        start=start,
        end=end,
        max_pages=max_pages,
    )
    durations = {"1m": 1, "5m": 5, "30m": 30}
    output: list[NativeSectorBar] = []
    for sector_id in sorted(frames):
        index_code = sector_indices[sector_id]
        for raw_frequency, frame in sorted(frames[sector_id].items()):
            if raw_frequency not in durations:
                continue
            frequency = cast(Literal["1m", "5m", "30m"], raw_frequency)
            for row in frame.itertuples(index=False):
                closed_at = _normalize_frame_date(row.date).to_pydatetime()
                if closed_at.hour == 13 and closed_at.minute == 0:
                    closed_at = closed_at.replace(hour=11, minute=30)
                output.append(
                    NativeSectorBar(
                        sector_id=sector_id,
                        index_code=index_code,
                        frequency=frequency,
                        opened_at=closed_at
                        - timedelta(minutes=durations[frequency]),
                        closed_at=closed_at,
                        opened=_decimal(row.open),
                        high=_decimal(row.high),
                        low=_decimal(row.low),
                        closed=_decimal(row.close),
                        volume=_decimal(row.volume),
                    )
                )
    return tuple(
        sorted(output, key=lambda row: (row.closed_at, row.sector_id, row.frequency))
    )


def _board_limit(code: str, session: date) -> Decimal:
    digits = code.split(".", 1)[1]
    if code.startswith("BJ."):
        return Decimal("0.30")
    if digits.startswith(("688", "689")):
        return Decimal("0.20")
    if digits.startswith(("300", "301")) and session >= date(2020, 8, 24):
        return Decimal("0.20")
    return Decimal("0.10")


def load_security_statuses(
    bars: tuple[MinuteBar, ...],
) -> tuple[SecurityStatus, ...]:
    keys = sorted({(bar.code, bar.opened_at.date()) for bar in bars})
    return tuple(
        SecurityStatus(
            session=session,
            code=code,
            listed=True,
            st=False,
            suspended=False,
            limit_pct=_board_limit(code, session),
            lot_size=100,
            t_plus_days=1,
        )
        for code, session in keys
    )


def _records_hash(records: object) -> str:
    payload = repr(records).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def load_point_in_time_dataset(config: BacktestDataConfig) -> BacktestDataset:
    if config.codes:
        codes = config.codes
    else:
        daily = load_daily_rows(config.start, config.end)
        codes = tuple(sorted({row.code for row in daily}))
    bars = load_qmt_minute_bars(
        codes,
        start=config.start,
        end=config.end,
        chunk_size=config.qmt_chunk_size,
    )
    statuses = config.statuses or load_security_statuses(bars)
    sessions = tuple(sorted({bar.opened_at.date() for bar in bars}))
    membership_load = load_sector_memberships(
        codes=codes,
        sessions=sessions,
        catalog=config.catalog,
        historical_records=(config.memberships if config.memberships else None),
    )
    return BacktestDataset(
        bars=bars,
        statuses=statuses,
        memberships=membership_load.records,
        corporate_actions=config.corporate_actions,
        membership_as_of_each_session=(
            config.membership_as_of_each_session
            and membership_load.as_of_each_session
        ),
        point_in_time_adjustment=config.point_in_time_adjustment,
        source_hashes=(
            ("bars", _records_hash(bars)),
            ("memberships", _records_hash(membership_load.records)),
            ("statuses", _records_hash(statuses)),
        ),
        security_status_as_of_each_session=(
            config.security_status_as_of_each_session
            and bool(config.statuses)
        ),
    )


class ReplayCL(Protocol):
    def process_klines(self, frame: pd.DataFrame) -> object: ...

    def get_strict_evidence(self) -> StrictEvidenceResult: ...


BundleFactory = Callable[..., object]


def _default_cl_factory(
    code: str,
    frequency: str,
    snapshot: pd.DataFrame,
) -> ReplayCL:
    from chanlun.core.cl import CL

    metadata = strict_snapshot_price_metadata(snapshot)
    return CL(
        code,
        frequency,
        strict_cl_config(
            structure_price_quantum=metadata.structure_price_quantum,
            price_basis_revision=metadata.price_basis_revision,
        ),
        market="a",
    )


def _strict_direction(evidence: StrictEvidenceResult) -> ContextDirection:
    structure = evidence.structure
    if not structure.levels:
        return "neutral"
    level = structure.levels[-1]
    if level.trend_types:
        return cast(ContextDirection, level.trend_types[-1].direction)
    locked = tuple(unit for unit in level.units if unit.locked)
    return "neutral" if not locked else cast(ContextDirection, locked[-1].direction)


@dataclass(frozen=True, slots=True)
class _ReplayFrameAnalysis:
    direction: ContextDirection
    confirmed_points: tuple[StructuralPoint, ...]
    provisional_points: tuple[ProvisionalCandidate, ...]


class CausalStructureReplay:
    def __init__(
        self,
        *,
        frames: Mapping[tuple[str, str], pd.DataFrame],
        cl_factory: Callable[[str, str, pd.DataFrame], ReplayCL] = _default_cl_factory,
        bundle_factory: BundleFactory | None = None,
        sector_names: Mapping[str, str] | None = None,
        sector_index_codes: Mapping[str, str] | None = None,
    ) -> None:
        self._frames = {
            key: self._validated_frame(frame) for key, frame in frames.items()
        }
        self._cl_factory = cl_factory
        self._bundle_factory = bundle_factory
        self._sector_names = dict(sector_names or {})
        self._sector_index_codes = dict(sector_index_codes or {})
        self._states: dict[tuple[str, str], ReplayCL] = {}
        self._row_cursors: dict[tuple[str, str], pd.Timestamp] = {}
        self._request_cursors: dict[str, datetime] = {}
        self._analysis_cache: dict[
            tuple[str, str], tuple[pd.Timestamp, _ReplayFrameAnalysis]
        ] = {}

    @staticmethod
    def _validated_frame(frame: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(frame, pd.DataFrame):
            raise TypeError("replay frame must be a DataFrame")
        required = {"date", "open", "high", "low", "close", "volume"}
        if not required.issubset(frame.columns):
            raise ValueError("replay frame is missing OHLCV columns")
        snapshot_attrs = dict(frame.attrs)
        result = frame[list(required)].copy()
        result["date"] = result["date"].map(_normalize_frame_date)
        result = result.sort_values("date", kind="stable").reset_index(drop=True)
        if result["date"].duplicated().any():
            raise ValueError("replay frame dates must be unique")
        result = result[["date", "open", "high", "low", "close", "volume"]]
        result.attrs = snapshot_attrs
        return result

    def advance_until(
        self,
        *,
        code: str,
        closed_at: datetime,
    ) -> Mapping[str, ReplayCL]:
        cutoff = normalize_datetime(closed_at, "closed_at")
        previous_request = self._request_cursors.get(code)
        if previous_request is not None and cutoff < previous_request:
            raise ValueError("causal replay cursor cannot move backwards")
        self._request_cursors[code] = cutoff
        output: dict[str, ReplayCL] = {}
        for frequency in ("1m", "5m", "30m"):
            key = (code, frequency)
            frame = self._frames.get(key)
            state = self._states.get(key)
            if state is None:
                if frame is None:
                    raise ValueError("replay frame is unavailable")
                state = self._cl_factory(code, frequency, frame)
                self._states[key] = state
            if frame is not None:
                eligible = frame.loc[frame["date"] <= pd.Timestamp(cutoff)]
                row_cursor = self._row_cursors.get(key)
                if row_cursor is not None:
                    eligible = eligible.loc[eligible["date"] > row_cursor]
                if not eligible.empty:
                    if eligible["date"].max() > pd.Timestamp(cutoff):
                        raise AssertionError("future row reached CL replay state")
                    state.process_klines(eligible.reset_index(drop=True))
                    self._row_cursors[key] = eligible["date"].iloc[-1]
            output[frequency] = state
        return output

    def _membership_at(
        self,
        dataset: BacktestDataset,
        code: str,
        closed_at: datetime,
    ) -> str | None:
        matches = sorted(
            {
                row.sector_id
                for row in dataset.memberships
                if row.code == code and row.session == closed_at.date()
            }
        )
        return matches[0] if matches else None

    def _analysis(
        self,
        state: ReplayCL,
        *,
        code: str,
        frequency: str,
        decision_at: datetime,
    ) -> _ReplayFrameAnalysis:
        key = (code, frequency)
        snapshot_cursor = self._row_cursors.get(key)
        if snapshot_cursor is None:
            return _ReplayFrameAnalysis("neutral", (), ())
        cached = self._analysis_cache.get(key)
        if cached is not None and cached[0] == snapshot_cursor:
            return cached[1]
        evidence = state.get_strict_evidence()
        snapshot_closed_at = normalize_datetime(
            snapshot_cursor.to_pydatetime(),
            "snapshot_closed_at",
        )
        if normalize_datetime(
            evidence.source_closed_at,
            "evidence.source_closed_at",
        ) != snapshot_closed_at:
            raise ValueError("strict evidence snapshot does not match replay frame")
        confirmed = extract_confirmed_points(
            evidence,
            code=code,
            source_frequency=frequency,
            as_of=decision_at,
        )
        provisional = extract_provisional_candidates(
            evidence,
            code=code,
            source_frequency=frequency,
            as_of=decision_at,
        )
        analysis = _ReplayFrameAnalysis(
            direction=_strict_direction(evidence),
            confirmed_points=confirmed,
            provisional_points=provisional,
        )
        self._analysis_cache[key] = (snapshot_cursor, analysis)
        return analysis

    def _default_bundle(
        self,
        *,
        dataset: BacktestDataset,
        code: str,
        closed_at: datetime,
        states: Mapping[str, ReplayCL],
    ) -> SymbolStructureBundle:
        thirty_analysis = self._analysis(
            states["30m"],
            code=code,
            frequency="30m",
            decision_at=closed_at,
        )
        five_analysis = self._analysis(
            states["5m"],
            code=code,
            frequency="5m",
            decision_at=closed_at,
        )
        one_analysis = self._analysis(
            states["1m"],
            code=code,
            frequency="1m",
            decision_at=closed_at,
        )
        thirty = thirty_analysis.confirmed_points
        five = five_analysis.confirmed_points
        one = one_analysis.confirmed_points
        sector_id = self._membership_at(dataset, code, closed_at)
        index_code = None if sector_id is None else self._sector_index_codes.get(sector_id)
        if sector_id is None or index_code is None:
            sector = SectorAssessment(
                sector_id=sector_id or "unclassified",
                sector_name=self._sector_names.get(sector_id or "", "未分类"),
                eligible=False,
                hard_block=True,
                regime="hostile",
                rank_components=(),
                reason_codes=("native_sector_data_missing",),
            )
        else:
            sector_states = self.advance_until(code=index_code, closed_at=closed_at)
            contexts = {}
            complete = True
            for frequency in ("1m", "5m", "30m"):
                sector_analysis = self._analysis(
                    sector_states[frequency],
                    code=index_code,
                    frequency=frequency,
                    decision_at=closed_at,
                )
                complete = complete and bool(self._frames.get((index_code, frequency)) is not None)
                contexts[frequency] = classify_context(
                    frequency=frequency,
                    current_direction=sector_analysis.direction,
                    points=sector_analysis.confirmed_points,
                    as_of=closed_at,
                )
            sector = assess_sector(
                sector_id=sector_id,
                sector_name=self._sector_names.get(sector_id, sector_id),
                market_data_source="tdx_native_880_index",
                thirty=contexts["30m"],
                five=contexts["5m"],
                one=contexts["1m"],
                data_complete=complete,
            )
        return SymbolStructureBundle(
            code=code,
            as_of=closed_at,
            sector=sector,
            thirty_direction=thirty_analysis.direction,
            thirty_points=thirty,
            five_points=(*five, *five_analysis.provisional_points),
            one_points=one,
            opposite_points=tuple(point for point in five if point.side == "sell"),
        )

    def bundle_at(
        self,
        *,
        dataset: BacktestDataset,
        closed_at: datetime,
        code: str,
    ) -> SymbolStructureBundle:
        cutoff = normalize_datetime(closed_at, "closed_at")
        states = self.advance_until(code=code, closed_at=cutoff)
        if self._bundle_factory is not None:
            return cast(
                SymbolStructureBundle,
                self._bundle_factory(
                    dataset=dataset,
                    code=code,
                    closed_at=cutoff,
                    states=states,
                    replay=self,
                ),
            )
        return self._default_bundle(
            dataset=dataset,
            code=code,
            closed_at=cutoff,
            states=states,
        )


__all__ = [
    "BacktestDataConfig",
    "CausalStructureReplay",
    "DailyMarketRow",
    "MembershipLoad",
    "NativeSectorBar",
    "load_daily_rows",
    "load_point_in_time_dataset",
    "load_qmt_minute_bars",
    "load_sector_memberships",
    "load_security_statuses",
    "load_tdx_native_sector_bars",
    "open_read_only_connection",
]
