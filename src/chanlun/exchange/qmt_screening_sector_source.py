"""为实时选股提供 QMT GICS3/GICS4 目录及由成分股合成的行业 K 线。"""

from __future__ import annotations

import ast
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import gc
import hashlib
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
import unicodedata
from uuid import uuid4
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from xtquant import xtdata

from chanlun.decision_support.fingerprints import (
    canonical_json,
    normalize_datetime,
    sha256_json,
)
from chanlun.decision_support.trading_system.sector_strength import (
    SectorStrengthBatch,
    SectorStrengthEvidence,
    build_horizontal_sector_strength_batch,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    QmtCausalFactorEvent,
    apply_qmt_causal_factor_adjustment,
    build_causal_sector_price_basis_metadata,
    qmt_causal_factor_events_from_frame,
    qmt_causal_factor_revision,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    qmt_sector_member_path_revision,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import DailyMarketBar
from chanlun.decision_support.trading_system.selection import (
    CompletedDailyClose,
    SectorMemberHistory,
)
from chanlun.decision_support.trading_system.trading_session import (
    build_trading_session_evidence,
)
from chanlun.exchange.exchange_qmt import _XTDATA_NATIVE_LOCK
from chanlun.exchange.price_basis import (
    QMT_STRUCTURE_DIVIDEND_TYPE,
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)
from chanlun.exchange.qmt_time_contract import qmt_exclusive_download_end


QMT_GICS3_CATALOG_SOURCE = "qmt_gics3_components"
QMT_GICS_HIERARCHY_CATALOG_SOURCE = "qmt_gics3_gics4_hierarchy"
QMT_GICS3_COMPOSITE_PROVIDER = "qmt-gics3-composite"
QMT_GICS3_COMPOSITE_ADJUSTMENT = (
    "causal-factor-stable-24-member-median"
)
QMT_GICS3_COMPOSITE_MEMBER_LIMIT = 24
QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT = 8
QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE = Decimal("0.60")
QMT_GICS3_COMPOSITE_CALENDAR_GRID_CONTRACT = (
    "QMT_SH_TRADING_CALENDAR_CONTIGUOUS_VISIBLE_SUFFIX"
)
QMT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT = (
    "BIT_I_IS_SECTOR_COMPOSITE_MEMBERS_I"
)
QMT_GICS3_COMPOSITE_METHOD = (
    "DETERMINISTIC_HASH_SAMPLE_CAUSAL_FACTOR_MEDIAN_RETURN_CHAIN"
)
QMT_CURRENT_A_SHARE_SECTOR = "沪深京A股"
QMT_SECTOR_STRENGTH_PRICE_BASIS_CONTRACT = (
    "QMT_FRONT_RATIO_TERMINAL_CLOSE_NORMALIZATION"
)


_DailyStrengthBar = DailyMarketBar | CompletedDailyClose
_DailyStrengthHistory = tuple[_DailyStrengthBar, ...]
QMT_SECTOR_STRENGTH_QMT_DIVIDEND_TYPE = QMT_STRUCTURE_DIVIDEND_TYPE
QMT_SECTOR_STRENGTH_ADJUSTMENT = (
    "front-ratio-terminal-close-normalized"
)

_GICS3_PREFIX = "GICS3"
_GICS4_PREFIX = "GICS4"
_QMT_A_SHARE_CODE = re.compile(r"^([0-9]{6})\.(SH|SZ|BJ)$")
_NORMALIZED_A_SHARE_CODE = re.compile(r"^(SH|SZ|BJ)\.([0-9]{6})$")
_FREQUENCY_SECONDS = {"5m": 5 * 60, "30m": 30 * 60, "1d": 24 * 60 * 60}
_FIELDS = ("time", "open", "high", "low", "close", "volume")
_PRICE_FIELDS = ("open", "high", "low", "close")
_COMPOSITE_QUANTUM = Decimal("0.000001")
_SHANGHAI = ZoneInfo("Asia/Shanghai")
# 月、周、日收敛门的短前缀需要 480 个已完成日线观测；完整前缀必须覆盖 720 个
# 交易日，删去最老三分之一后才能仍满足同一最低值。这里是物理证据回看范围，不是
# 信号参数；四个自然年为节假日和停牌保留余量，并在下方精确校验交易所日历。
_QMT_TRADING_CALENDAR_LOOKBACK_DAYS = 1460
# 每个高周期板块需要同时保留 5m 同源流和原生日线。GICS3/GICS4 全量轮转会
# GICS3+GICS4 全目录会产生数百个大型合成帧；真实生产复算证明 64 个帧可把
# 隔离进程推到约 2.9 GiB，远超工作进程声明的 1.5 GiB 水位。跨批次复用已经由
# 内容寻址的磁盘事实缓存承担；内存 LRU 只保留最近 8 个帧，足够覆盖调用方的
# 紧邻重读，同时给 24k 根 5m 基础帧、结构计算副本和 QMT RPC 留出明确余量。
_QMT_SECTOR_COMPOSITE_MEMORY_CACHE_CAPACITY = 8
_QMT_SECTOR_CALENDAR_GRID_CACHE_CAPACITY = 8
_QMT_SECTOR_COMPOSITE_INCREMENTAL_OVERLAP_BARS = 8
_QMT_SECTOR_COMPOSITE_INCREMENTAL_MAX_NEW_BARS = 48
_DAILY_FIELDS = ("time", "open", "high", "low", "close", "volume")
_FACT_CACHE_ENVELOPE_SCHEMA = "chanlun-qmt-sector-fact-cache-envelope"
_COMPOSITE_FACT_SCHEMA = "chanlun-qmt-sector-composite-facts"
_DAILY_FACT_SCHEMA = "chanlun-qmt-sector-daily-facts"
_MEMBER_STATUS_FACT_SCHEMA = "chanlun-qmt-sector-member-status-facts"
_MEMBER_LISTING_FACT_SCHEMA = "chanlun-qmt-sector-member-listing-facts"
_FACT_PRODUCER_SCHEMA = "chanlun-qmt-sector-fact-producer"
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_FACT_STREAM_BUFFER_CHARACTERS = 64 * 1024
_DAILY_STRENGTH_REQUEST_CHUNK_SIZE = 64
_DAILY_STRENGTH_PROGRESS_SYMBOL_INTERVAL = 8


def _producer_ast_manifest(
    source: str,
    *,
    roots: tuple[str, ...],
    excluded_names: frozenset[str] = frozenset(),
) -> tuple[tuple[str, str], ...]:
    """Return the transitive top-level implementation used by ``roots``.

    Hashing this entire large provider file made a daily-status-only change
    invalidate all 112 intraday composite fact files.  A hand-maintained list
    of helper names would be unsafe in the opposite direction: a newly called
    helper could be omitted accidentally.  The AST closure follows every
    referenced top-level definition and constant automatically, giving each
    fact family the narrowest complete code identity.
    """

    tree = ast.parse(source)
    definitions: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            definitions[node.target.id] = node
    if any(root not in definitions for root in roots):
        raise RuntimeError("QMT fact producer root is unavailable")
    pending = list(roots)
    selected: set[str] = set()
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        selected.add(name)
        node = definitions[name]
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Name)
                and child.id in definitions
                and child.id not in selected
                and child.id not in excluded_names
            ):
                pending.append(child.id)
    return tuple(
        (
            name,
            ast.dump(
                definitions[name],
                annotate_fields=True,
                include_attributes=False,
            ),
        )
        for name in sorted(selected)
    )


def _qmt_fact_family_revision(*, family: str, roots: tuple[str, ...]) -> str:
    package_root = Path(__file__).resolve().parents[1]
    provider = Path(__file__).resolve()
    manifest = _producer_ast_manifest(
        provider.read_text(encoding="utf-8"),
        roots=roots,
        # 内存/日历 LRU 只改变重复读取成本，不改变任何持久化行情事实。若把这些
        # 容量值纳入事实生产者身份，每次纯性能调优都会无谓淘汰全市场认证缓存。
        excluded_names=(
            frozenset(
                {
                    "_QMT_SECTOR_COMPOSITE_MEMORY_CACHE_CAPACITY",
                    "_QMT_SECTOR_CALENDAR_GRID_CACHE_CAPACITY",
                }
            )
            if family == "INTRADAY_SECTOR_COMPOSITE"
            else frozenset()
        ),
    )
    shared_paths = [
        package_root / "exchange" / "price_basis.py",
        package_root / "decision_support" / "fingerprints.py",
    ]
    if family == "DAILY_MEMBER_STRENGTH_AND_STATUS":
        # 日线强弱事实仍依赖 ``DailyMarketBar``。保持既有依赖身份，避免分钟下载契约
        # 的修复无故淘汰已经验证并持久化的全市场日线事实。
        shared_paths.append(
            package_root
            / "decision_support"
            / "trading_system"
            / "etf_proxy_facts.py"
        )
    shared_paths.append(
        package_root
        / "decision_support"
        / "trading_system"
        / "qmt_causal_factor_adjustment.py"
    )
    if family == "INTRADAY_SECTOR_COMPOSITE":
        shared_paths.extend(
            (
                package_root / "exchange" / "qmt_time_contract.py",
                package_root
                / "decision_support"
                / "trading_system"
                / "qmt_sector_same_base.py",
            )
        )
    shared = tuple(
        {
            "path": path.relative_to(package_root).as_posix(),
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in shared_paths
    )
    return sha256_json(
        {
            "schema": _FACT_PRODUCER_SCHEMA,
            "family": family,
            "provider_module": provider.relative_to(package_root).as_posix(),
            "provider_ast_manifest": manifest,
            "shared_files": shared,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
    )


def qmt_sector_composite_fact_producer_revision() -> str:
    """Identity of only the normalized 5m/30m composite fact producer."""

    return _qmt_fact_family_revision(
        family="INTRADAY_SECTOR_COMPOSITE",
        roots=("QmtSectorCompositeSource",),
    )


def qmt_sector_daily_fact_producer_revision() -> str:
    """Identity of the daily member bars and suspension-fact producer."""

    return _qmt_fact_family_revision(
        family="DAILY_MEMBER_STRENGTH_AND_STATUS",
        roots=("QmtSectorStrengthSource",),
    )


def _fact_cache_options(
    path: Path | str | None,
    revision: str | None,
    *,
    path_field: str,
) -> tuple[Path | None, str | None]:
    if (path is None) != (revision is None):
        raise ValueError(f"{path_field} and fact_cache_revision must be set together")
    if path is None:
        return None, None
    if not isinstance(revision, str) or _SHA256_ID.fullmatch(revision) is None:
        raise ValueError("fact_cache_revision must be a sha256 identity")
    return Path(path).resolve(), revision


def _fact_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(
        not isinstance(key, str) for key in value
    ):
        raise ValueError(f"{field_name} must be a string-keyed mapping")
    return value


def _read_fact_payload(path: Path) -> Mapping[str, object] | None:
    """Return one hash-authenticated payload, otherwise fail closed to a miss."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        outer = _fact_mapping(document, "fact cache envelope")
        payload = _fact_mapping(outer.get("payload"), "fact cache payload")
        if (
            outer.get("schema") != _FACT_CACHE_ENVELOPE_SCHEMA
            or outer.get("content_sha256") != sha256_json(payload)
        ):
            return None
        return payload
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _write_fact_payload(path: Path, payload: Mapping[str, object]) -> None:
    """Publish one content-authenticated JSON fact document atomically."""

    document = {
        "schema": _FACT_CACHE_ENVELOPE_SCHEMA,
        "content_sha256": sha256_json(payload),
        "payload": dict(payload),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                document,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _compact_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _daily_bar_fact_row(value: _DailyStrengthBar) -> tuple[object, ...]:
    if isinstance(value, CompletedDailyClose):
        return (
            value.session.isoformat(),
            str(value.close),
            value.known_at.isoformat(),
            value.completed,
        )
    return (
        value.session.isoformat(),
        str(value.open),
        str(value.high),
        str(value.low),
        str(value.close),
        str(value.volume),
        value.known_at.isoformat(),
        value.completed,
    )


def _iter_daily_bars_canonical_json(
    bars: Mapping[str, _DailyStrengthHistory],
) -> Iterator[str]:
    yield '{"$map":['
    symbol_separator = ""
    for symbol in sorted(bars):
        yield symbol_separator
        yield f"[{_compact_json(symbol)},"
        yield '{"$sequence":['
        row_separator = ""
        for value in bars[symbol]:
            row = _daily_bar_fact_row(value)
            encoded = ",".join(_compact_json(item) for item in row)
            yield f'{row_separator}{{"$sequence":[{encoded}]}}'
            row_separator = ","
        yield "]}]"
        symbol_separator = ","
    yield "]}"


def _iter_daily_fact_canonical_json(
    payload: Mapping[str, object],
    bars: Mapping[str, _DailyStrengthHistory],
) -> Iterator[str]:
    yield '{"$map":['
    separator = ""
    for key in sorted({*payload, "bars"}):
        yield separator
        yield f"[{_compact_json(key)},"
        if key == "bars":
            yield from _iter_daily_bars_canonical_json(bars)
        else:
            yield canonical_json(payload[key])
        yield "]"
        separator = ","
    yield "]}"


def _sha256_text_chunks(chunks: Iterable[str]) -> str:
    digest = hashlib.sha256()
    buffered: list[str] = []
    buffered_characters = 0
    for chunk in chunks:
        buffered.append(chunk)
        buffered_characters += len(chunk)
        if buffered_characters >= _FACT_STREAM_BUFFER_CHARACTERS:
            digest.update("".join(buffered).encode("utf-8"))
            buffered.clear()
            buffered_characters = 0
    if buffered:
        digest.update("".join(buffered).encode("utf-8"))
    return f"sha256:{digest.hexdigest()}"


def _iter_daily_bars_json(
    bars: Mapping[str, _DailyStrengthHistory],
) -> Iterator[str]:
    yield "{"
    symbol_separator = ""
    for symbol in sorted(bars):
        yield f"{symbol_separator}{_compact_json(symbol)}:["
        row_separator = ""
        for value in bars[symbol]:
            yield f"{row_separator}{_compact_json(_daily_bar_fact_row(value))}"
            row_separator = ","
        yield "]"
        symbol_separator = ","
    yield "}"


def _iter_daily_fact_json(
    payload: Mapping[str, object],
    bars: Mapping[str, _DailyStrengthHistory],
) -> Iterator[str]:
    yield "{"
    separator = ""
    for key in sorted({*payload, "bars"}):
        yield f"{separator}{_compact_json(key)}:"
        if key == "bars":
            yield from _iter_daily_bars_json(bars)
        else:
            yield _compact_json(payload[key])
        separator = ","
    yield "}"


def _write_text_chunks_atomically(path: Path, chunks: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            buffered: list[str] = []
            buffered_characters = 0
            for chunk in chunks:
                buffered.append(chunk)
                buffered_characters += len(chunk)
                if buffered_characters >= _FACT_STREAM_BUFFER_CHARACTERS:
                    handle.write("".join(buffered))
                    buffered.clear()
                    buffered_characters = 0
            if buffered:
                handle.write("".join(buffered))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _write_daily_fact_payload(
    path: Path,
    payload: Mapping[str, object],
    bars: Mapping[str, _DailyStrengthHistory],
) -> None:
    """Publish daily facts without materializing a second full JSON row tree."""

    if "bars" in payload:
        raise ValueError("daily fact payload metadata cannot contain bars")
    content_sha256 = _sha256_text_chunks(
        _iter_daily_fact_canonical_json(payload, bars)
    )

    def document_chunks() -> Iterator[str]:
        # This is the exact order produced by ``sort_keys=True`` for the
        # authenticated three-field envelope.
        yield '{"content_sha256":'
        yield _compact_json(content_sha256)
        yield ',"payload":'
        yield from _iter_daily_fact_json(payload, bars)
        yield ',"schema":'
        yield _compact_json(_FACT_CACHE_ENVELOPE_SCHEMA)
        yield "}"

    _write_text_chunks_atomically(path, document_chunks())


def _qmt_calendar_date(value: object) -> date:
    if isinstance(value, bool):
        raise TypeError("QMT trading calendar timestamp cannot be a boolean")
    if isinstance(value, str) and re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date()
    if not isinstance(value, (int, float)):
        raise TypeError("QMT trading calendar timestamp is invalid")
    return (
        pd.to_datetime(value, unit="ms", utc=True)
        .tz_convert("Asia/Shanghai")
        .date()
    )


def qmt_trading_session_evidence(
    *,
    session: date,
    observed_at: datetime,
) -> dict[str, object]:
    """Read only the QMT calendar APIs and return fail-closed evidence.

    The deployed QMT build publishes completed historical sessions only.  Its
    empty same-day response is therefore unresolved until the target itself,
    or a later trading session, has been published.  Native exceptions are
    converted to an explicit unresolved document so callers never fall back
    to a weekday guess.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    if isinstance(session, datetime) or not isinstance(session, date):
        raise TypeError("session must be a date")
    if session.weekday() >= 5 or session > observed.date():
        return build_trading_session_evidence(
            session=session,
            observed_at=observed,
            query_attempted=False,
            query_succeeded=False,
        )

    try:
        # 命令行状态命令输出机器可读 JSON；QMT 可选的连接问候属于运行噪声，
        # 不能作为证据。
        if hasattr(xtdata, "enable_hello"):
            xtdata.enable_hello = False
        with _XTDATA_NATIVE_LOCK:
            response = xtdata.get_trading_dates(
                "SH",
                session.strftime("%Y%m%d"),
                session.strftime("%Y%m%d"),
                -1,
            )
            published_raw = xtdata.get_market_last_trade_date("SH")
        if type(response) is not list:
            raise TypeError("QMT trading dates response must be a list")
        returned = tuple(sorted({_qmt_calendar_date(value) for value in response}))
        if any(value != session for value in returned):
            raise ValueError("QMT trading dates escaped the target interval")
        try:
            published_through = _qmt_calendar_date(published_raw)
        except (TypeError, ValueError, OverflowError):
            published_through = session if returned else None
        return build_trading_session_evidence(
            session=session,
            observed_at=observed,
            returned_sessions=returned,
            published_through=published_through,
            query_attempted=True,
            query_succeeded=True,
        )
    except Exception:
        return build_trading_session_evidence(
            session=session,
            observed_at=observed,
            query_attempted=True,
            query_succeeded=False,
        )


def qmt_trading_sessions(
    *,
    start: date,
    end: date,
    observed_at: datetime,
) -> tuple[date, ...]:
    """Return an exact published QMT trading-calendar interval.

    This bulk form is used by the higher-timeframe bridge.  It deliberately
    rejects an unpublished tail instead of filling weekdays or deriving the
    calendar from the price rows being certified.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    if (
        isinstance(start, datetime)
        or isinstance(end, datetime)
        or type(start) is not date
        or type(end) is not date
        or start > end
        or end > observed.date()
    ):
        raise ValueError("QMT trading-calendar interval is invalid")
    if hasattr(xtdata, "enable_hello"):
        xtdata.enable_hello = False
    with _XTDATA_NATIVE_LOCK:
        response = xtdata.get_trading_dates(
            "SH",
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
            -1,
        )
        published_raw = xtdata.get_market_last_trade_date("SH")
    if type(response) is not list:
        raise RuntimeError("QMT trading calendar is unavailable")
    sessions = tuple(sorted({_qmt_calendar_date(value) for value in response}))
    if any(value < start or value > end for value in sessions):
        raise RuntimeError("QMT trading calendar escaped the requested interval")
    published_through = _qmt_calendar_date(published_raw)
    if published_through < end:
        raise RuntimeError("QMT trading calendar is not published through interval end")
    if not sessions:
        raise RuntimeError("QMT trading calendar interval is empty")
    return sessions


def _latest_completed_qmt_daily_session(observed: datetime) -> date | None:
    """Return the latest session whose 15:00 daily bar must already exist.

    Before the close, today's daily bar is not yet a completed fact, so the
    search starts on the prior calendar day.  At and after 15:00 it starts on
    the decision date.  Weekends and exchange holidays are skipped only when
    QMT's official calendar proves they are non-trading sessions; an
    unavailable or unpublished calendar fails closed instead of guessing.
    """

    cutoff = observed.date()
    if observed.timetz().replace(tzinfo=None) < time(15, 0):
        cutoff -= timedelta(days=1)
    for offset in range(32):
        candidate = cutoff - timedelta(days=offset)
        evidence = qmt_trading_session_evidence(
            session=candidate,
            observed_at=observed,
        )
        classification = evidence.get("classification")
        if classification == "TRADING_SESSION":
            return candidate
        if classification != "NON_TRADING_SESSION":
            return None
    return None


def _canonical_sector_level_name(
    value: str,
    *,
    prefix: str,
) -> tuple[str, str] | None:
    text = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not text.startswith(prefix):
        return None
    name = text[len(prefix) :].strip()
    return (text, name) if name else None


def _canonical_sector_name(value: str) -> tuple[str, str] | None:
    """Return the legacy GICS3 identity used by the immutable ledger."""

    return _canonical_sector_level_name(value, prefix=_GICS3_PREFIX)


def _hierarchy_sector_id(*, source_key: str, taxonomy_level: str) -> str:
    if taxonomy_level == _GICS3_PREFIX:
        # 父级沿用旧目录身份，使同一个 GICS3 板块在历史账本和实时分层目录中可直接
        # 对照；只给新增的 GICS4 定义独立身份空间。
        schema = "chanlun-qmt-gics3-sector"
        namespace = "qmt-gics3:"
    elif taxonomy_level == _GICS4_PREFIX:
        schema = "chanlun-qmt-gics4-sector"
        namespace = "qmt-gics4:"
    else:
        raise ValueError("unsupported QMT GICS taxonomy level")
    return namespace + sha256_json(
        {
            "schema": schema,
            "source_key": source_key,
        }
    ).removeprefix("sha256:")


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


def _catalog_document(
    *,
    captures: list[tuple[str, str, list[object]]],
    captured_at: datetime,
    capture_transport: str,
    capture_evidence: Mapping[str, object] | None = None,
    eligible_member_codes: frozenset[str] | None = None,
) -> dict[str, object]:
    if not captures:
        raise RuntimeError("QMT GICS3 sector catalog is empty")
    sectors: list[dict[str, object]] = []
    all_normalized_members: set[str] = set()
    included_members: set[str] = set()
    for source_key, name, raw_members in captures:
        normalized_members = {
            code
            for code in (
                _normalized_a_share_code(value) for value in raw_members
            )
            if code is not None
        }
        all_normalized_members.update(normalized_members)
        if eligible_member_codes is not None:
            normalized_members.intersection_update(eligible_member_codes)
        included_members.update(normalized_members)
        members = sorted(normalized_members)
        sector_id = "qmt-gics3:" + sha256_json(
            {
                "schema": "chanlun-qmt-gics3-sector",
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
    sectors.sort(key=lambda row: str(row["source_key"]))
    revision = sha256_json(
        {
            "schema": "chanlun-qmt-gics3-catalog",
            "sectors": sectors,
        }
    )
    evidence = dict(capture_evidence or {})
    excluded_members = all_normalized_members - included_members
    evidence.update(
        {
            "membership_universe_filter_applied": (
                eligible_member_codes is not None
            ),
            "unfiltered_gics3_member_count": len(all_normalized_members),
            "included_gics3_member_count": len(included_members),
            "excluded_noncurrent_member_count": len(excluded_members),
            "excluded_noncurrent_members_sha256": sha256_json(
                tuple(sorted(excluded_members))
            ),
        }
    )
    return {
        "source": QMT_GICS3_CATALOG_SOURCE,
        "captured_at": captured_at.isoformat(),
        "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
        "catalog_revision": revision,
        "sectors": sectors,
        "capture_transport": capture_transport,
        "capture_evidence": evidence,
    }


def _canonical_hierarchy_rows(
    catalog: Mapping[str, object],
) -> tuple[dict[str, object], ...]:
    raw_rows = catalog.get("sectors")
    if (
        isinstance(raw_rows, (str, bytes))
        or not isinstance(raw_rows, Sequence)
        or not raw_rows
    ):
        raise ValueError("QMT GICS3/GICS4 hierarchy catalog is empty")

    rows: list[dict[str, object]] = []
    identities: set[str] = set()
    source_keys: set[str] = set()
    membership_owner: dict[tuple[str, str], str] = {}
    for raw in raw_rows:
        if not isinstance(raw, Mapping):
            raise ValueError("QMT hierarchy catalog row is invalid")
        sector_id = raw.get("sector_id")
        name = raw.get("name")
        source_key = raw.get("source_key")
        taxonomy_level = raw.get("taxonomy_level")
        parent_sector_id = raw.get("parent_sector_id")
        parent_sector_name = raw.get("parent_sector_name")
        raw_members = raw.get("member_codes")
        if taxonomy_level not in {_GICS3_PREFIX, _GICS4_PREFIX}:
            raise ValueError("QMT hierarchy taxonomy level is invalid")
        expected_source_prefix = str(taxonomy_level)
        expected_id_prefix = f"qmt-{expected_source_prefix.lower()}:"
        if (
            not isinstance(sector_id, str)
            or not sector_id.startswith(expected_id_prefix)
            or sector_id
            != _hierarchy_sector_id(
                source_key=str(source_key),
                taxonomy_level=expected_source_prefix,
            )
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(source_key, str)
            or not source_key.startswith(expected_source_prefix)
            or source_key == expected_source_prefix
            or sector_id in identities
            or source_key in source_keys
        ):
            raise ValueError("QMT hierarchy sector identity is invalid")
        if (
            isinstance(raw_members, (str, bytes))
            or not isinstance(raw_members, Sequence)
            or any(
                not isinstance(value, str)
                or _NORMALIZED_A_SHARE_CODE.fullmatch(value) is None
                for value in raw_members
            )
        ):
            raise ValueError("QMT hierarchy members must be normalized A-share codes")
        members = tuple(sorted(set(raw_members)))
        if len(members) != len(raw_members):
            raise ValueError("QMT hierarchy members must be unique")
        if taxonomy_level == _GICS3_PREFIX:
            if parent_sector_id is not None or parent_sector_name is not None:
                raise ValueError("QMT GICS3 parent fields must be empty")
        elif (
            parent_sector_id is not None
            and (
                not isinstance(parent_sector_id, str)
                or not parent_sector_id.startswith("qmt-gics3:")
                or not isinstance(parent_sector_name, str)
                or not parent_sector_name.strip()
            )
        ):
            raise ValueError("QMT GICS4 parent identity is invalid")
        elif parent_sector_id is None and (
            parent_sector_name is not None or members
        ):
            raise ValueError("non-empty QMT GICS4 sector must have one parent")

        for member in members:
            key = (expected_source_prefix, member)
            previous = membership_owner.get(key)
            if previous is not None and previous != sector_id:
                raise ValueError(
                    f"QMT {expected_source_prefix} member has multiple sectors"
                )
            membership_owner[key] = sector_id
        identities.add(sector_id)
        source_keys.add(source_key)
        rows.append(
            {
                "sector_id": sector_id,
                "name": name.strip(),
                "source_key": source_key,
                "taxonomy_level": expected_source_prefix,
                "parent_sector_id": parent_sector_id,
                "parent_sector_name": parent_sector_name,
                "member_codes": members,
            }
        )

    by_id = {str(row["sector_id"]): row for row in rows}
    for row in rows:
        if row["taxonomy_level"] != _GICS4_PREFIX:
            continue
        parent_id = row["parent_sector_id"]
        if parent_id is None:
            continue
        parent = by_id.get(str(parent_id))
        if (
            parent is None
            or parent["taxonomy_level"] != _GICS3_PREFIX
            or row["parent_sector_name"] != parent["name"]
            or not set(row["member_codes"]).issubset(parent["member_codes"])
        ):
            raise ValueError("QMT GICS4 parent relation is inconsistent")
    if not any(row["taxonomy_level"] == _GICS3_PREFIX for row in rows):
        raise ValueError("QMT hierarchy catalog has no GICS3 parents")
    if not any(row["taxonomy_level"] == _GICS4_PREFIX for row in rows):
        raise ValueError("QMT hierarchy catalog has no GICS4 children")
    rows.sort(
        key=lambda row: (
            0 if row["taxonomy_level"] == _GICS3_PREFIX else 1,
            str(row["source_key"]),
        )
    )
    return tuple(rows)


def qmt_gics_hierarchy_catalog_revision(catalog: Mapping[str, object]) -> str:
    """Validate and identify the live GICS3-parent/GICS4-child catalog."""

    if catalog.get("source") != QMT_GICS_HIERARCHY_CATALOG_SOURCE:
        raise ValueError("unsupported QMT hierarchy catalog source")
    return sha256_json(
        {
            "schema": "chanlun-qmt-gics3-gics4-hierarchy-catalog",
            "sectors": _canonical_hierarchy_rows(catalog),
        }
    )


def _hierarchy_catalog_document(
    *,
    captures: list[tuple[str, str, str, list[object]]],
    captured_at: datetime,
    capture_transport: str,
    capture_evidence: Mapping[str, object] | None = None,
    eligible_member_codes: frozenset[str] | None = None,
) -> dict[str, object]:
    if not captures:
        raise RuntimeError("QMT GICS3/GICS4 hierarchy catalog is empty")

    normalized: dict[tuple[str, str], set[str]] = {}
    labels: dict[tuple[str, str], str] = {}
    all_members_by_level = {
        _GICS3_PREFIX: set(),
        _GICS4_PREFIX: set(),
    }
    for taxonomy_level, source_key, name, raw_members in captures:
        key = (taxonomy_level, source_key)
        if key in normalized:
            raise RuntimeError(f"duplicate QMT hierarchy sector: {source_key}")
        members = {
            code
            for code in (_normalized_a_share_code(value) for value in raw_members)
            if code is not None
        }
        normalized[key] = members
        labels[key] = name
        all_members_by_level[taxonomy_level].update(members)

    included: dict[tuple[str, str], set[str]] = {}
    for key, members in normalized.items():
        included[key] = (
            set(members)
            if eligible_member_codes is None
            else set(members & eligible_member_codes)
        )

    rows: list[dict[str, object]] = []
    parent_by_member: dict[str, tuple[str, str]] = {}
    parent_members_by_id: dict[str, set[str]] = {}
    legacy_parent_rows: list[dict[str, object]] = []
    excluded_empty_gics3_source_keys: list[str] = []
    excluded_empty_gics4_source_keys: list[str] = []
    for taxonomy_level, source_key, _name, _raw_members in captures:
        if taxonomy_level != _GICS3_PREFIX:
            continue
        name = labels[(taxonomy_level, source_key)]
        sector_id = _hierarchy_sector_id(
            source_key=source_key,
            taxonomy_level=taxonomy_level,
        )
        for member in normalized[(taxonomy_level, source_key)]:
            previous = parent_by_member.get(member)
            if previous is not None and previous[0] != sector_id:
                raise RuntimeError(
                    f"QMT GICS3 member belongs to multiple parents: {member}"
                )
            parent_by_member[member] = (sector_id, name)
        members = sorted(included[(taxonomy_level, source_key)])
        parent_members_by_id[sector_id] = set(members)
        # QMT's global taxonomy includes categories that currently contain no
        # member of the authorized A-share universe. Such nodes cannot route
        # or rank a symbol. Keeping them only creates duplicate/empty filter
        # chips and unnecessary assessment work; their identities remain in
        # the authenticated capture evidence below.
        if not members:
            excluded_empty_gics3_source_keys.append(source_key)
            continue
        legacy_parent_rows.append(
            {
                "sector_id": sector_id,
                "name": name,
                "source_key": source_key,
                "member_codes": members,
            }
        )
        rows.append(
            {
                "sector_id": sector_id,
                "name": name,
                "source_key": source_key,
                "taxonomy_level": _GICS3_PREFIX,
                "parent_sector_id": None,
                "parent_sector_name": None,
                "member_codes": members,
            }
        )

    hierarchy_orphans: set[str] = set()
    collapsed_degenerate_gics4_source_keys: list[str] = []
    relation_count = 0
    for taxonomy_level, source_key, _name, _raw_members in captures:
        if taxonomy_level != _GICS4_PREFIX:
            continue
        short_name = labels[(taxonomy_level, source_key)]
        raw_members = normalized[(taxonomy_level, source_key)]
        parent_candidates = {
            parent_by_member[member]
            for member in raw_members
            if member in parent_by_member
        }
        if len(parent_candidates) > 1:
            raise RuntimeError(
                f"QMT GICS4 sector maps to multiple GICS3 parents: {source_key}"
            )
        parent_sector_id: str | None = None
        parent_sector_name: str | None = None
        if parent_candidates:
            parent_sector_id, parent_sector_name = next(iter(parent_candidates))
        candidate_members = included[(taxonomy_level, source_key)]
        if parent_sector_id is None:
            valid_members: set[str] = set()
        else:
            valid_members = candidate_members & parent_members_by_id[parent_sector_id]
        hierarchy_orphans.update(candidate_members - valid_members)
        if not valid_members:
            excluded_empty_gics4_source_keys.append(source_key)
            continue
        # QMT may publish a GICS4 node that is only an identity copy of its
        # GICS3 parent: both the normalized label and the eligible constituent
        # set are identical. Emitting that row creates two independent scans
        # and the meaningless presentation ``海上运输 → 海上运输``. Collapse
        # only this exact, auditable equivalence; a same-name child with a
        # genuinely narrower member set remains a valid fourth-level node.
        if (
            parent_sector_id is not None
            and parent_sector_name == short_name
            and valid_members == parent_members_by_id[parent_sector_id]
        ):
            collapsed_degenerate_gics4_source_keys.append(source_key)
            continue
        if parent_sector_id is not None:
            relation_count += 1
        display_name = (
            short_name
            if parent_sector_name is None
            else f"{parent_sector_name} → {short_name}"
        )
        rows.append(
            {
                "sector_id": _hierarchy_sector_id(
                    source_key=source_key,
                    taxonomy_level=taxonomy_level,
                ),
                "name": display_name,
                "source_key": source_key,
                "taxonomy_level": _GICS4_PREFIX,
                "parent_sector_id": parent_sector_id,
                "parent_sector_name": parent_sector_name,
                "member_codes": sorted(valid_members),
            }
        )

    rows.sort(
        key=lambda row: (
            0 if row["taxonomy_level"] == _GICS3_PREFIX else 1,
            str(row["source_key"]),
        )
    )
    legacy_parent_rows.sort(key=lambda row: str(row["source_key"]))
    evidence = dict(capture_evidence or {})
    evidence.update(
        {
            "membership_universe_filter_applied": (
                eligible_member_codes is not None
            ),
            "gics3_sector_count": sum(
                row["taxonomy_level"] == _GICS3_PREFIX for row in rows
            ),
            "gics4_sector_count": sum(
                row["taxonomy_level"] == _GICS4_PREFIX for row in rows
            ),
            "gics4_parent_relation_count": relation_count,
            "collapsed_degenerate_gics4_sector_count": len(
                collapsed_degenerate_gics4_source_keys
            ),
            "collapsed_degenerate_gics4_source_keys_sha256": sha256_json(
                tuple(sorted(collapsed_degenerate_gics4_source_keys))
            ),
            "excluded_empty_gics3_sector_count": len(
                excluded_empty_gics3_source_keys
            ),
            "excluded_empty_gics3_source_keys_sha256": sha256_json(
                tuple(sorted(excluded_empty_gics3_source_keys))
            ),
            "excluded_empty_gics4_sector_count": len(
                excluded_empty_gics4_source_keys
            ),
            "excluded_empty_gics4_source_keys_sha256": sha256_json(
                tuple(sorted(excluded_empty_gics4_source_keys))
            ),
            "unfiltered_gics3_member_count": len(
                all_members_by_level[_GICS3_PREFIX]
            ),
            "unfiltered_gics4_member_count": len(
                all_members_by_level[_GICS4_PREFIX]
            ),
            "included_gics3_member_count": len(
                set().union(
                    *(
                        set(row["member_codes"])
                        for row in rows
                        if row["taxonomy_level"] == _GICS3_PREFIX
                    )
                )
            ),
            "included_gics4_member_count": len(
                set().union(
                    *(
                        set(row["member_codes"])
                        for row in rows
                        if row["taxonomy_level"] == _GICS4_PREFIX
                    )
                )
            ),
            "hierarchy_orphan_member_count": len(hierarchy_orphans),
            "hierarchy_orphan_members_sha256": sha256_json(
                tuple(sorted(hierarchy_orphans))
            ),
        }
    )
    document: dict[str, object] = {
        "source": QMT_GICS_HIERARCHY_CATALOG_SOURCE,
        "captured_at": captured_at.isoformat(),
        "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
        "catalog_revision": None,
        "gics3_catalog_revision": sha256_json(
            {
                "schema": "chanlun-qmt-gics3-catalog",
                "sectors": legacy_parent_rows,
            }
        ),
        "sectors": rows,
        "capture_transport": capture_transport,
        "capture_evidence": evidence,
    }
    document["catalog_revision"] = qmt_gics_hierarchy_catalog_revision(document)
    return document


def build_qmt_gics3_sector_catalog(
    *,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Capture the current QMT GICS3 catalog without any TDX fallback."""

    captured = normalize_datetime(
        captured_at or datetime.now(_SHANGHAI),
        "captured_at",
    )

    with _XTDATA_NATIVE_LOCK:
        source_list = xtdata.get_sector_list()
        if (
            type(source_list) is not list
            or not source_list
            or any(type(item) is not str for item in source_list)
        ):
            raise RuntimeError("QMT sector list is unavailable")
        current_members_raw = xtdata.get_stock_list_in_sector(
            QMT_CURRENT_A_SHARE_SECTOR,
            real_timetag=-1,
        )
        if type(current_members_raw) is not list or not current_members_raw:
            raise RuntimeError("QMT current A-share universe is unavailable")
        current_members = tuple(
            _normalized_a_share_code(value) for value in current_members_raw
        )
        if any(value is None for value in current_members):
            raise RuntimeError("QMT current A-share universe contains invalid codes")
        current_a_share_members = frozenset(
            value for value in current_members if value is not None
        )
        if not current_a_share_members:
            raise RuntimeError("QMT current A-share universe is empty")
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

    return _catalog_document(
        captures=captures,
        captured_at=captured,
        capture_transport="QMT_RPC",
        capture_evidence={
            "membership_universe_source": (
                f"QMT_RPC:{QMT_CURRENT_A_SHARE_SECTOR}"
            ),
            "membership_universe_member_count": len(current_a_share_members),
        },
        eligible_member_codes=current_a_share_members,
    )


def build_qmt_gics_hierarchy_sector_catalog(
    *,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Capture the current QMT GICS3/GICS4 hierarchy without fallback data."""

    captured = normalize_datetime(
        captured_at or datetime.now(_SHANGHAI),
        "captured_at",
    )
    with _XTDATA_NATIVE_LOCK:
        source_list = xtdata.get_sector_list()
        if (
            type(source_list) is not list
            or not source_list
            or any(type(item) is not str for item in source_list)
        ):
            raise RuntimeError("QMT sector list is unavailable")
        current_members_raw = xtdata.get_stock_list_in_sector(
            QMT_CURRENT_A_SHARE_SECTOR,
            real_timetag=-1,
        )
        if type(current_members_raw) is not list or not current_members_raw:
            raise RuntimeError("QMT current A-share universe is unavailable")
        current_members = tuple(
            _normalized_a_share_code(value) for value in current_members_raw
        )
        if any(value is None for value in current_members):
            raise RuntimeError("QMT current A-share universe contains invalid codes")
        current_a_share_members = frozenset(
            value for value in current_members if value is not None
        )
        if not current_a_share_members:
            raise RuntimeError("QMT current A-share universe is empty")

        selected: list[tuple[str, str, str, str]] = []
        canonical_keys: set[str] = set()
        for raw_key in source_list:
            parsed_level: tuple[str, tuple[str, str]] | None = None
            for taxonomy_level in (_GICS3_PREFIX, _GICS4_PREFIX):
                parsed = _canonical_sector_level_name(
                    raw_key,
                    prefix=taxonomy_level,
                )
                if parsed is not None:
                    parsed_level = (taxonomy_level, parsed)
                    break
            if parsed_level is None:
                continue
            taxonomy_level, (canonical_key, name) = parsed_level
            if canonical_key in canonical_keys:
                raise RuntimeError(
                    f"duplicate QMT hierarchy sector: {canonical_key}"
                )
            canonical_keys.add(canonical_key)
            selected.append((taxonomy_level, canonical_key, raw_key, name))
        selected.sort(
            key=lambda item: (
                0 if item[0] == _GICS3_PREFIX else 1,
                item[1],
            )
        )
        if not any(item[0] == _GICS3_PREFIX for item in selected):
            raise RuntimeError("QMT GICS3 parent catalog is empty")
        if not any(item[0] == _GICS4_PREFIX for item in selected):
            raise RuntimeError("QMT GICS4 child catalog is empty")
        captures: list[tuple[str, str, str, list[object]]] = []
        for taxonomy_level, canonical_key, raw_key, name in selected:
            response = xtdata.get_stock_list_in_sector(
                raw_key,
                real_timetag=-1,
            )
            if type(response) is not list:
                raise RuntimeError(
                    f"QMT sector membership is unavailable: {canonical_key}"
                )
            captures.append(
                (taxonomy_level, canonical_key, name, list(response))
            )

    return _hierarchy_catalog_document(
        captures=captures,
        captured_at=captured,
        capture_transport="QMT_RPC",
        capture_evidence={
            "membership_universe_source": (
                f"QMT_RPC:{QMT_CURRENT_A_SHARE_SECTOR}"
            ),
            "membership_universe_member_count": len(current_a_share_members),
        },
        eligible_member_codes=current_a_share_members,
    )


def build_qmt_gics3_sector_catalog_from_local_files(
    *,
    qmt_data_dir: Path,
    captured_at: datetime | None = None,
) -> dict[str, object]:
    """Read QMT's own local GICS3 member files without an account/RPC session.

    The directory is only a current local cache.  Its file mtimes and a content
    manifest are returned as capture evidence so callers can flag staleness;
    the data is never treated as historical membership before ``captured_at``.
    """

    captured = normalize_datetime(
        captured_at or datetime.now(_SHANGHAI),
        "captured_at",
    )
    root = Path(qmt_data_dir).resolve()
    sector_dir = root / "Sector" / "Temple" / "GICS"
    if not sector_dir.is_dir():
        raise RuntimeError(f"QMT local GICS directory is unavailable: {sector_dir}")
    captures: list[tuple[str, str, list[object]]] = []
    manifest: list[tuple[str, str, int, str]] = []
    mtimes: list[datetime] = []
    for path in sorted(
        (value for value in sector_dir.iterdir() if value.is_file()),
        key=lambda value: value.name,
    ):
        parsed = _canonical_sector_name(path.name)
        if parsed is None:
            continue
        canonical_key, name = parsed
        payload = path.read_bytes()
        try:
            text = payload.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = payload.decode("gb18030")
        members = [value.strip() for value in text.split(",") if value.strip()]
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=_SHANGHAI)
        mtimes.append(modified)
        manifest.append(
            (
                path.name,
                "sha256:" + hashlib.sha256(payload).hexdigest(),
                len(payload),
                modified.isoformat(),
            )
        )
        captures.append((canonical_key, name, members))
    if not captures:
        raise RuntimeError("QMT local GICS3 sector catalog is empty")
    evidence = {
        "source_directory": str(sector_dir),
        "source_file_count": len(manifest),
        "source_manifest_sha256": sha256_json(
            {
                "schema": "chanlun-qmt-local-gics3-files",
                "files": tuple(manifest),
            }
        ),
        "oldest_source_mtime": min(mtimes).isoformat(),
        "latest_source_mtime": max(mtimes).isoformat(),
        "source_age_seconds": max(
            0,
            int((captured - max(mtimes)).total_seconds()),
        ),
        "membership_universe_source": "UNAVAILABLE_IN_LOCAL_GICS3_FILES",
    }
    return _catalog_document(
        captures=captures,
        captured_at=captured,
        capture_transport="QMT_LOCAL_SECTOR_FILES",
        capture_evidence=evidence,
    )


def _empty_composite_frame(
    sector_id: str,
    membership_revision: str,
    *,
    members: tuple[str, ...],
    composite_members: tuple[str, ...],
    minimum_member_count: int,
    minimum_bar_coverage: Decimal,
    maximum_composite_members: int,
    factor_revision: str | None = None,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        columns=(
            "code",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "member_mask",
        )
    )
    metadata = (
        build_provider_price_basis_metadata(
            provider=QMT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=f"{sector_id}:{membership_revision}",
            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=_COMPOSITE_QUANTUM,
        )
        if factor_revision is None
        else build_causal_sector_price_basis_metadata(
            provider=QMT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=f"{sector_id}:{membership_revision}",
            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=_COMPOSITE_QUANTUM,
            factor_revision=factor_revision,
        )
    )
    result = attach_price_basis_metadata(frame, metadata)
    return _attach_composite_provenance(
        result,
        sector_id=sector_id,
        membership_revision="sha256:" + membership_revision,
        members=members,
        composite_members=composite_members,
        minimum_member_count=minimum_member_count,
        minimum_bar_coverage=minimum_bar_coverage,
        maximum_composite_members=maximum_composite_members,
        factor_revision=factor_revision,
    )


def _composite_required_member_count(
    composite_member_count: int,
    *,
    minimum_member_count: int,
    minimum_bar_coverage: Decimal,
) -> int:
    return max(
        minimum_member_count,
        math.ceil(composite_member_count * float(minimum_bar_coverage)),
    )


def _attach_composite_provenance(
    frame: pd.DataFrame,
    *,
    sector_id: str,
    membership_revision: str,
    members: tuple[str, ...],
    composite_members: tuple[str, ...],
    minimum_member_count: int,
    minimum_bar_coverage: Decimal,
    maximum_composite_members: int,
    factor_revision: str | None,
) -> pd.DataFrame:
    frame.attrs["sector_id"] = sector_id
    frame.attrs["sector_membership_revision"] = membership_revision
    frame.attrs["sector_membership_scope"] = "CALLER_SUPPLIED"
    frame.attrs["sector_members"] = members
    frame.attrs["sector_composite_members"] = composite_members
    frame.attrs["sector_composite_member_limit"] = maximum_composite_members
    frame.attrs["sector_composite_minimum_member_count"] = minimum_member_count
    frame.attrs["sector_composite_minimum_bar_coverage"] = str(
        minimum_bar_coverage
    )
    frame.attrs["sector_composite_required_member_count"] = (
        _composite_required_member_count(
            len(composite_members),
            minimum_member_count=minimum_member_count,
            minimum_bar_coverage=minimum_bar_coverage,
        )
    )
    frame.attrs["sector_composite_member_mask_contract"] = (
        QMT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT
    )
    frame.attrs["sector_composite_member_path_revision"] = (
        _composite_member_path_revision(frame)
    )
    frame.attrs["sector_composite_method"] = QMT_GICS3_COMPOSITE_METHOD
    frame.attrs["sector_factor_adjustment_contract_id"] = (
        QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
    )
    frame.attrs["sector_factor_revision"] = factor_revision
    return frame


def _attach_native_daily_composite_provenance(
    frame: pd.DataFrame,
    *,
    sector_id: str,
    observed_at: datetime,
) -> pd.DataFrame:
    """Bind one native-daily advisory to its exact visible rows.

    This deliberately does not claim equivalence with the component-median 5m
    stream.  The higher-timeframe resolver retains the unreconciled blocker and
    caps every otherwise favourable result at AMBER.
    """

    observed = normalize_datetime(observed_at, "observed_at")
    revision = sha256_json(
        {
            "schema": "chanlun-qmt-current-sector-native-daily-base",
            "sector_id": sector_id,
            "observed_at": observed,
            "price_basis_revision": frame.attrs.get("price_basis_revision"),
            "sector_membership_revision": frame.attrs.get(
                "sector_membership_revision"
            ),
            "sector_factor_revision": frame.attrs.get("sector_factor_revision"),
            "sector_composite_member_path_revision": frame.attrs.get(
                "sector_composite_member_path_revision"
            ),
            "rows": tuple(
                {
                    "date": normalize_datetime(
                        pd.Timestamp(row.date).to_pydatetime(),
                        "sector native daily close",
                    ),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                    "member_mask": int(row.member_mask),
                }
                for row in frame.itertuples(index=False)
            ),
        }
    )
    frame.attrs.update(
        source_base_frequency="native-d",
        source_base_stream_revision=revision,
        derived_frequency="d",
        sector_native_daily_role=(
            "UNRECONCILED_RESEARCH_MWD_ADVISORY_ONLY"
        ),
        sector_native_daily_observed_at=observed.isoformat(),
    )
    return frame


def _composite_member_path_revision(frame: pd.DataFrame) -> str | None:
    return qmt_sector_member_path_revision(frame)


def _copy_frame(value: pd.DataFrame) -> pd.DataFrame:
    result = value.copy(deep=True)
    result.attrs = dict(value.attrs)
    return result


def _tail_composite_frame(
    value: pd.DataFrame,
    request_bars: int,
) -> pd.DataFrame:
    result = value.tail(request_bars).copy(deep=True).reset_index(drop=True)
    result.attrs = dict(value.attrs)
    result.attrs["sector_composite_member_path_revision"] = (
        _composite_member_path_revision(result)
    )
    if result.attrs.get("source_base_frequency") == "native-d":
        observed_at = result.attrs.get("sector_native_daily_observed_at")
        if not isinstance(observed_at, str):
            raise ValueError("sector native-daily observation time is unavailable")
        result = _attach_native_daily_composite_provenance(
            result,
            sector_id=str(result.attrs.get("sector_id") or ""),
            observed_at=datetime.fromisoformat(observed_at),
        )
    return result


def _latest_contiguous_calendar_suffix_length(
    actual_closes: tuple[datetime, ...],
    expected_closes: tuple[datetime, ...],
) -> int:
    """返回截至最新预期收盘时刻的连续交易日历后缀长度。"""

    if (
        not actual_closes
        or not expected_closes
        or len(actual_closes) > len(expected_closes)
        or actual_closes[-1] != expected_closes[-1]
    ):
        return 0
    actual_index = len(actual_closes) - 1
    expected_index = len(expected_closes) - 1
    while (
        actual_index >= 0
        and expected_index >= 0
        and actual_closes[actual_index] == expected_closes[expected_index]
    ):
        actual_index -= 1
        expected_index -= 1
    return len(actual_closes) - actual_index - 1


def _member_ratios(
    raw: Mapping[str, object],
    native_code: str,
    *,
    normalized_code: str,
    factor_events: tuple[QmtCausalFactorEvent, ...],
    frequency: str,
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
    frame["date"] = pd.to_datetime(
        frame["time"], unit="ms", utc=True
    ).dt.tz_convert("Asia/Shanghai")
    if frequency == "1d":
        # QMT 把原生日线标在交易日边界，但日线事实只有在 A 股收盘完成后才对决策可见。
        # 因果截点和下方交易所日历后缀校验都使用这一发布时间。
        frame["date"] = frame["date"].dt.normalize() + pd.Timedelta(hours=15)
    cutoff = pd.Timestamp(normalize_datetime(not_after, "not_after"))
    prices = frame.loc[:, list(_PRICE_FIELDS)]
    finite = np.isfinite(
        frame.loc[:, list(_FIELDS)].to_numpy(dtype=np.float64)
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
    frame = apply_qmt_causal_factor_adjustment(
        frame,
        code=normalized_code,
        events=factor_events,
    )
    previous_close = frame["close"].shift(1)
    result = pd.DataFrame({"date": frame["date"]})
    for field in _PRICE_FIELDS:
        result[f"{field}_ratio"] = frame[field] / previous_close
    result = result.iloc[1:].copy()
    ratios = result.loc[:, [f"{field}_ratio" for field in _PRICE_FIELDS]]
    valid_ratios = np.isfinite(
        ratios.to_numpy(dtype=np.float64)
    ).all(axis=1)
    valid_ratios &= (ratios > 0).all(axis=1)
    result = result.loc[valid_ratios]
    return None if result.empty else result


def _grouped_composite_ratios(
    raw: Mapping[str, object],
    *,
    composite_members: tuple[str, ...],
    factor_events_by_code: Mapping[
        str, tuple[QmtCausalFactorEvent, ...]
    ],
    frequency: str,
    observed_at: datetime,
    minimum_member_count: int,
    required_member_count: int,
) -> pd.DataFrame | None:
    """Build per-close median ratios without choosing a price anchor."""

    native_codes = tuple(_qmt_code(code) for code in composite_members)
    member_frames: list[pd.DataFrame] = []
    for member_index, (normalized_code, native_code) in enumerate(
        zip(composite_members, native_codes, strict=True)
    ):
        ratios = _member_ratios(
            raw,
            native_code,
            normalized_code=normalized_code,
            factor_events=factor_events_by_code[normalized_code],
            frequency=frequency,
            not_after=observed_at,
        )
        if ratios is None:
            continue
        ratios.insert(0, "member_bit", 1 << member_index)
        member_frames.append(ratios)
    if len(member_frames) < minimum_member_count:
        return None
    facts = pd.concat(member_frames, ignore_index=True)
    grouped = facts.groupby("date", sort=True).agg(
        member_count=("member_bit", "size"),
        member_mask=("member_bit", "sum"),
        open_ratio=("open_ratio", "median"),
        high_ratio=("high_ratio", "median"),
        low_ratio=("low_ratio", "median"),
        close_ratio=("close_ratio", "median"),
    )
    return grouped[grouped["member_count"] >= required_member_count]


def _composite_rows_from_grouped_ratios(
    grouped: pd.DataFrame,
    *,
    sector_id: str,
    starting_close: float,
) -> pd.DataFrame:
    """Materialize composite OHLC rows from an authenticated price anchor."""

    if grouped.empty:
        return pd.DataFrame()
    close_ratios = grouped["close_ratio"].to_numpy(
        dtype=np.float64,
        copy=False,
    )
    close_values = np.multiply.accumulate(
        np.concatenate((np.array([starting_close]), close_ratios))
    )[1:]
    previous_closes = np.concatenate(
        (np.array([starting_close]), close_values[:-1])
    )
    open_values = previous_closes * grouped["open_ratio"].to_numpy(
        dtype=np.float64,
        copy=False,
    )
    high_values = np.maximum.reduce(
        (
            previous_closes
            * grouped["high_ratio"].to_numpy(
                dtype=np.float64,
                copy=False,
            ),
            open_values,
            close_values,
        )
    )
    low_values = np.minimum.reduce(
        (
            previous_closes
            * grouped["low_ratio"].to_numpy(
                dtype=np.float64,
                copy=False,
            ),
            open_values,
            close_values,
        )
    )
    return pd.DataFrame(
        {
            "code": sector_id,
            "date": grouped.index.to_numpy(),
            "open": open_values,
            "high": high_values,
            "low": low_values,
            "close": close_values,
            "volume": grouped["member_count"].to_numpy(dtype=np.float64),
            "member_mask": grouped["member_mask"].to_numpy(dtype=np.int64),
        }
    ).reset_index(drop=True)


def _composite_rows_match(
    actual: pd.DataFrame,
    rebuilt: pd.DataFrame,
    *,
    expected_closes: tuple[datetime, ...],
) -> bool:
    if len(actual) != len(rebuilt) or len(actual) != len(expected_closes):
        return False
    rebuilt_closes = tuple(
        normalize_datetime(
            pd.Timestamp(value).to_pydatetime(),
            "incremental sector rebuilt close",
        )
        for value in rebuilt["date"]
    )
    price_columns = ["open", "high", "low", "close"]
    return bool(
        tuple(actual["code"]) == tuple(rebuilt["code"])
        and rebuilt_closes == expected_closes
        and np.allclose(
            actual.loc[:, price_columns].to_numpy(dtype=np.float64),
            rebuilt.loc[:, price_columns].to_numpy(dtype=np.float64),
            rtol=1e-12,
            atol=1e-12,
        )
        and np.array_equal(
            actual["volume"].to_numpy(dtype=np.int64),
            rebuilt["volume"].to_numpy(dtype=np.int64),
        )
        and np.array_equal(
            actual["member_mask"].to_numpy(dtype=np.int64),
            rebuilt["member_mask"].to_numpy(dtype=np.int64),
        )
    )


class QmtSectorCompositeSource:
    """Build deterministic equal-weight median sector bars from QMT members."""

    def __init__(
        self,
        *,
        minimum_member_count: int = QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT,
        minimum_bar_coverage: Decimal = QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE,
        maximum_composite_members: int = QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
        progress_callback: Callable[[], None] = lambda: None,
        fact_cache_directory: Path | str | None = None,
        fact_cache_revision: str | None = None,
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
        if not callable(progress_callback):
            raise TypeError("progress_callback must be callable")
        cache_directory, cache_revision = _fact_cache_options(
            fact_cache_directory,
            fact_cache_revision,
            path_field="fact_cache_directory",
        )
        self._minimum_member_count = minimum_member_count
        self._minimum_bar_coverage = minimum_bar_coverage
        self._maximum_composite_members = maximum_composite_members
        self._progress_callback = progress_callback
        self._fact_cache_directory = cache_directory
        self._fact_cache_revision = cache_revision
        self._lock = RLock()
        self._cache: OrderedDict[
            tuple[str, str, int], tuple[int, str, pd.DataFrame]
        ] = OrderedDict()
        self._prepared_buckets: dict[str, int] = {}
        self._attempted_members: dict[str, set[str]] = {}
        self._trading_dates_cache: tuple[date, tuple[date, ...]] | None = None
        self._calendar_grid_cache: OrderedDict[
            tuple[str, int], tuple[tuple[datetime, ...], str]
        ] = OrderedDict()
        self._factor_cache: dict[
            tuple[date, str], tuple[QmtCausalFactorEvent, ...]
        ] = {}
        self._fact_cache_counters = {
            "exact_hits": 0,
            "incremental_attempts": 0,
            "incremental_hits": 0,
            "incremental_fallbacks": 0,
            "full_rebuilds": 0,
        }

    def _remember_frame(
        self,
        key: tuple[str, str, int],
        value: tuple[int, str, pd.DataFrame],
    ) -> None:
        """在锁内保存一个有界的大型合成数据帧。"""

        self._cache.pop(key, None)
        self._cache[key] = value
        while len(self._cache) > _QMT_SECTOR_COMPOSITE_MEMORY_CACHE_CAPACITY:
            self._cache.popitem(last=False)

    def cache_health_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema": "chanlun-qmt-sector-composite-cache-health",
                "frame_entries": len(self._cache),
                "frame_capacity": _QMT_SECTOR_COMPOSITE_MEMORY_CACHE_CAPACITY,
                "factor_entries": len(self._factor_cache),
                "calendar_grid_entries": len(self._calendar_grid_cache),
                "calendar_grid_capacity": (
                    _QMT_SECTOR_CALENDAR_GRID_CACHE_CAPACITY
                ),
                "calendar_lookback_days": _QMT_TRADING_CALENDAR_LOOKBACK_DAYS,
                "fact_cache_counters": dict(self._fact_cache_counters),
            }

    def _record_fact_cache_event(self, name: str) -> None:
        with self._lock:
            self._fact_cache_counters[name] += 1

    def _report_progress(self) -> None:
        self._progress_callback()

    @staticmethod
    def _bucket(as_of: datetime, frequency: str) -> int:
        observed = normalize_datetime(as_of, "as_of")
        if frequency == "1d":
            # 同一交易日收盘前后的请求不能共用缓存分桶：15:00 日线对前者不可见，
            # 对后者可见。
            return observed.date().toordinal() * 2 + int(
                observed.timetz().replace(tzinfo=None) >= time(15)
            )
        seconds = _FREQUENCY_SECONDS[frequency]
        epoch = int(observed.timestamp())
        return epoch - epoch % seconds

    def _fact_path(
        self,
        *,
        sector_id: str,
        frequency: str,
        request_bars: int,
    ) -> Path | None:
        if self._fact_cache_directory is None:
            return None
        identity = sha256_json(
            {
                "schema": "chanlun-qmt-sector-composite-fact-path",
                "sector_id": sector_id,
                "frequency": frequency,
                "request_bars": request_bars,
            }
        ).removeprefix("sha256:")
        return self._fact_cache_directory / f"{identity}.json"

    def _fact_identity(
        self,
        *,
        sector_id: str,
        members: tuple[str, ...],
        composite_members: tuple[str, ...],
        membership_revision: str,
        frequency: str,
        request_bars: int,
        expected_closed_at: datetime,
        calendar_grid_started_at: datetime,
        calendar_grid_bar_count: int,
        calendar_grid_revision: str,
        factor_revision: str,
    ) -> dict[str, object]:
        if self._fact_cache_revision is None:
            raise RuntimeError("composite fact cache is disabled")
        return {
            "schema": _COMPOSITE_FACT_SCHEMA,
            "producer_revision": self._fact_cache_revision,
            "sector_id": sector_id,
            "frequency": frequency,
            "request_bars": request_bars,
            "expected_closed_at": expected_closed_at.isoformat(),
            "calendar_grid_started_at": calendar_grid_started_at.isoformat(),
            "calendar_grid_bar_count": calendar_grid_bar_count,
            "calendar_grid_contract": (
                QMT_GICS3_COMPOSITE_CALENDAR_GRID_CONTRACT
            ),
            "calendar_grid_revision": calendar_grid_revision,
            "members": list(members),
            "composite_members": list(composite_members),
            "membership_revision": "sha256:" + membership_revision,
            "minimum_member_count": self._minimum_member_count,
            "minimum_bar_coverage": str(self._minimum_bar_coverage),
            "maximum_composite_members": self._maximum_composite_members,
            "provider": QMT_GICS3_COMPOSITE_PROVIDER,
            "adjustment": QMT_GICS3_COMPOSITE_ADJUSTMENT,
            "factor_adjustment_contract_id": (
                QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
            ),
            "factor_revision": factor_revision,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }

    @staticmethod
    def _frame_from_fact_payload(
        payload: Mapping[str, object],
        *,
        identity: Mapping[str, object],
        observed_at: datetime,
        expected_closed_at: datetime,
        expected_closes: tuple[datetime, ...],
    ) -> pd.DataFrame | None:
        if any(payload.get(key) != value for key, value in identity.items()):
            return None
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows:
            return None
        if len(raw_rows) > int(identity["request_bars"]):
            return None
        rows: list[dict[str, object]] = []
        closes: list[datetime] = []
        sector_id = str(identity["sector_id"])
        composite_member_count = len(tuple(identity["composite_members"]))
        required_member_count = _composite_required_member_count(
            composite_member_count,
            minimum_member_count=int(identity["minimum_member_count"]),
            minimum_bar_coverage=Decimal(
                str(identity["minimum_bar_coverage"])
            ),
        )
        try:
            for index, value in enumerate(raw_rows):
                row = _fact_mapping(value, f"rows[{index}]")
                if row.get("code") != sector_id:
                    return None
                closed = normalize_datetime(
                    datetime.fromisoformat(str(row.get("date"))),
                    f"rows[{index}].date",
                )
                if closed > observed_at:
                    return None
                prices = {
                    name: float(row.get(name))
                    for name in ("open", "high", "low", "close")
                }
                volume = float(row.get("volume"))
                member_mask = row.get("member_mask")
                if (
                    any(not math.isfinite(value) or value <= 0 for value in prices.values())
                    or not math.isfinite(volume)
                    or not volume.is_integer()
                    or volume < required_member_count
                    or volume > composite_member_count
                    or type(member_mask) is not int
                    or member_mask <= 0
                    or member_mask >= 1 << composite_member_count
                    or member_mask.bit_count() != int(volume)
                    or prices["high"] < max(prices["open"], prices["close"])
                    or prices["low"] > min(prices["open"], prices["close"])
                ):
                    return None
                closes.append(closed)
                rows.append(
                    {
                        "code": sector_id,
                        "date": closed,
                        **prices,
                        "volume": volume,
                        "member_mask": member_mask,
                    }
                )
        except (ArithmeticError, TypeError, ValueError):
            return None
        if (
            closes != sorted(closes)
            or len(closes) != len(set(closes))
            or closes[-1] != expected_closed_at
            or len(closes) > len(expected_closes)
            or tuple(closes) != expected_closes[-len(closes) :]
        ):
            return None
        result = pd.DataFrame(rows)
        # 精确匹配数据提供方规范化后的数据类型。旧版 pandas 直接用 ``zoneinfo``
        # 时间构建数据帧时，会产生外观等价但内部不同的时区类型。
        result["date"] = pd.to_datetime(result["date"], utc=True).dt.tz_convert(
            "Asia/Shanghai"
        )
        metadata = build_causal_sector_price_basis_metadata(
            provider=QMT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=(
                f"{sector_id}:"
                + str(identity["membership_revision"]).removeprefix("sha256:")
            ),
            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=_COMPOSITE_QUANTUM,
            factor_revision=str(identity["factor_revision"]),
        )
        result = attach_price_basis_metadata(result, metadata)
        result = _attach_composite_provenance(
            result,
            sector_id=sector_id,
            membership_revision=str(identity["membership_revision"]),
            members=tuple(identity["members"]),
            composite_members=tuple(identity["composite_members"]),
            minimum_member_count=int(identity["minimum_member_count"]),
            minimum_bar_coverage=Decimal(
                str(identity["minimum_bar_coverage"])
            ),
            maximum_composite_members=int(
                identity["maximum_composite_members"]
            ),
            factor_revision=str(identity["factor_revision"]),
        )
        if identity.get("frequency") == "1d":
            result = _attach_native_daily_composite_provenance(
                result,
                sector_id=sector_id,
                observed_at=observed_at,
            )
        return result

    def _load_fact_frame(
        self,
        *,
        path: Path | None,
        identity: Mapping[str, object],
        observed_at: datetime,
        expected_closed_at: datetime,
        expected_closes: tuple[datetime, ...],
    ) -> pd.DataFrame | None:
        payload = self._load_fact_payload(path)
        if payload is None:
            return None
        return self._frame_from_fact_payload(
            payload,
            identity=identity,
            observed_at=observed_at,
            expected_closed_at=expected_closed_at,
            expected_closes=expected_closes,
        )

    def _load_fact_payload(
        self,
        path: Path | None,
    ) -> Mapping[str, object] | None:
        if path is None:
            return None
        self._report_progress()
        payload = _read_fact_payload(path)
        self._report_progress()
        return payload

    @staticmethod
    def _prior_fact_frame_from_payload(
        payload: Mapping[str, object],
        *,
        identity: Mapping[str, object],
        observed_at: datetime,
        expected_closes: tuple[datetime, ...],
    ) -> tuple[pd.DataFrame, int] | None:
        """Authenticate a complete same-day calendar prefix for extension."""

        dynamic_fields = frozenset(
            {
                "expected_closed_at",
                "calendar_grid_started_at",
                "calendar_grid_bar_count",
                "calendar_grid_revision",
            }
        )
        if any(
            payload.get(key) != value
            for key, value in identity.items()
            if key not in dynamic_fields
        ):
            return None
        try:
            prior_closed_at = normalize_datetime(
                datetime.fromisoformat(str(payload.get("expected_closed_at"))),
                "prior sector fact expected_closed_at",
            )
            current_closed_at = normalize_datetime(
                datetime.fromisoformat(str(identity["expected_closed_at"])),
                "current sector fact expected_closed_at",
            )
        except (TypeError, ValueError):
            return None
        if (
            identity.get("frequency") != "5m"
            or prior_closed_at >= current_closed_at
            or prior_closed_at.date() != current_closed_at.date()
            or current_closed_at != expected_closes[-1]
        ):
            return None
        try:
            prior_grid_index = expected_closes.index(prior_closed_at)
        except ValueError:
            return None
        prior_expected_closes = expected_closes[: prior_grid_index + 1]
        prior_grid_count = payload.get("calendar_grid_bar_count")
        if (
            type(prior_grid_count) is not int
            or prior_grid_count != len(prior_expected_closes)
            or payload.get("calendar_grid_started_at")
            != prior_expected_closes[0].isoformat()
            or payload.get("calendar_grid_revision")
            != sha256_json(
                {
                    "schema": "chanlun-qmt-sector-calendar-grid",
                    "frequency": identity["frequency"],
                    "expected_closes": prior_expected_closes,
                }
            )
        ):
            return None
        prior_identity = dict(identity)
        for field in dynamic_fields:
            prior_identity[field] = payload.get(field)
        frame = QmtSectorCompositeSource._frame_from_fact_payload(
            payload,
            identity=prior_identity,
            observed_at=observed_at,
            expected_closed_at=prior_closed_at,
            expected_closes=prior_expected_closes,
        )
        new_bar_count = len(expected_closes) - len(prior_expected_closes)
        suffix_start_index = (
            0
            if frame is None
            else len(prior_expected_closes) - len(frame)
        )
        if (
            frame is None
            or len(frame) < 2
            or suffix_start_index <= 0
            or not 0 < new_bar_count
            <= _QMT_SECTOR_COMPOSITE_INCREMENTAL_MAX_NEW_BARS
            or len(frame) + new_bar_count > int(identity["request_bars"])
        ):
            return None
        return frame, prior_grid_index

    def _persist_fact_frame(
        self,
        *,
        path: Path | None,
        identity: Mapping[str, object],
        frame: pd.DataFrame,
    ) -> None:
        if path is None or frame.empty:
            return
        rows = [
            {
                "code": str(item.code),
                "date": normalize_datetime(
                    pd.Timestamp(item.date).to_pydatetime(),
                    "sector fact close",
                ).isoformat(),
                "open": repr(float(item.open)),
                "high": repr(float(item.high)),
                "low": repr(float(item.low)),
                "close": repr(float(item.close)),
                "volume": repr(float(item.volume)),
                "member_mask": int(item.member_mask),
            }
            for item in frame.itertuples(index=False)
        ]
        self._report_progress()
        try:
            _write_fact_payload(path, {**identity, "rows": rows})
        except OSError:
            # 持久化只是优化；缓存卷暂时只读时，已经校验的实时 QMT 数据帧仍可使用。
            pass
        finally:
            self._report_progress()

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
                    "schema": "chanlun-qmt-gics3-sample",
                    "sector_id": sector_id,
                    "code": code,
                }
            ),
        )
        return tuple(sorted(ranked[: self._maximum_composite_members]))

    @staticmethod
    def _history_bounds_by_native_code(
        raw: object,
        native_codes: tuple[str, ...],
    ) -> dict[str, tuple[datetime, datetime]]:
        """Return validated earliest/latest local timestamps per member.

        A count-one freshness probe cannot distinguish a complete local cache
        from one containing only the newest few bars.  The latter stays
        permanently "fresh" while the M/W/D convergence gate can never obtain
        its 480-session left context.  Callers compare both returned boundaries
        with the exact visible span they require.  Interior absences may be
        legitimate suspensions; the downstream frozen-sample coverage and exact
        calendar-grid gates remain responsible for accepting or rejecting them.
        """

        if not isinstance(raw, Mapping):
            return {}
        source = raw.get("time")
        if not isinstance(source, pd.DataFrame):
            return {}
        bounds: dict[str, tuple[datetime, datetime]] = {}
        for native_code in native_codes:
            if native_code not in source.index:
                continue
            row = source.loc[native_code]
            if not isinstance(row, pd.Series):
                continue
            values = pd.to_numeric(row, errors="coerce").dropna()
            values = values[values > 0]
            if values.empty:
                continue
            try:
                returned = tuple(
                    normalize_datetime(
                        pd.Timestamp(value).to_pydatetime(),
                        "QMT member history bar",
                    )
                    for value in pd.to_datetime(
                        values,
                        unit="ms",
                        utc=True,
                        errors="coerce",
                    ).dropna()
                )
            except (OverflowError, TypeError, ValueError):
                continue
            if returned:
                bounds[native_code] = (min(returned), max(returned))
        return bounds

    def _prepare_history(
        self,
        *,
        members: tuple[str, ...],
        as_of: datetime,
        expected_closes: tuple[datetime, ...],
        required_bars: int,
        frequency: str = "5m",
    ) -> None:
        if frequency not in {"5m", "1d"}:
            raise ValueError("QMT sector history base must be 5m or 1d")
        bucket = self._bucket(as_of, frequency)
        with self._lock:
            if self._prepared_buckets.get(frequency) != bucket:
                self._prepared_buckets[frequency] = bucket
                self._attempted_members[frequency] = set()
            attempted = set(self._attempted_members.get(frequency, set()))
        native_by_member = {code: _qmt_code(code) for code in members}
        native_codes = tuple(native_by_member.values())
        self._report_progress()
        with _XTDATA_NATIVE_LOCK:
            latest = xtdata.get_market_data(
                field_list=["time"],
                stock_list=list(native_codes),
                period=frequency,
                start_time="",
                end_time=as_of.strftime("%Y%m%d%H%M%S"),
                count=required_bars + 32,
                dividend_type="none",
                fill_data=False,
            )
        self._report_progress()
        if type(required_bars) is not int or required_bars <= 0:
            raise ValueError("required_bars must be a positive integer")
        if not expected_closes:
            raise ValueError("expected_closes must not be empty")
        required = expected_closes[-required_bars:]
        bounds = self._history_bounds_by_native_code(
            latest,
            native_codes,
        )
        ready = {
            code
            for code, (earliest, newest) in bounds.items()
            if earliest <= required[0] and newest >= required[-1]
        }
        shallow = {
            code
            for code in native_codes
            if code not in bounds or bounds[code][0] > required[0]
        }
        pending = tuple(
            code
            for code in members
            if native_by_member[code] not in ready and code not in attempted
        )
        # QMT 下载的 ``end_time`` 是不包含端点；必须显式越过最后一根已完成 K 线，
        # 否则盘后增量下载会稳定缺少 15:00 收盘柱，而读取接口仍把 15:00 视为可见。
        download_end = qmt_exclusive_download_end(required[-1])
        completed_attempts: set[str] = set()
        for code in pending:
            try:
                self._report_progress()
                with _XTDATA_NATIVE_LOCK:
                    native_code = native_by_member[code]
                    repair_left_history = native_code in shallow
                    xtdata.download_history_data(
                        native_code,
                        frequency,
                        start_time=(
                            required[0].strftime("%Y%m%d%H%M%S")
                            if repair_left_history
                            else ""
                        ),
                        end_time=(
                            download_end
                        ),
                        incrementally=not repair_left_history,
                    )
                self._report_progress()
            except Exception:
                continue
            finally:
                completed_attempts.add(code)
        with self._lock:
            if self._prepared_buckets.get(frequency) == bucket:
                self._attempted_members.setdefault(frequency, set()).update(
                    completed_attempts
                )

    def _read_grouped_composite_ratios(
        self,
        *,
        composite_members: tuple[str, ...],
        factor_events_by_code: Mapping[
            str, tuple[QmtCausalFactorEvent, ...]
        ],
        frequency: str,
        observed_at: datetime,
        end_at: datetime,
        count: int,
    ) -> pd.DataFrame | None:
        native_codes = tuple(_qmt_code(code) for code in composite_members)
        self._report_progress()
        with _XTDATA_NATIVE_LOCK:
            raw = xtdata.get_market_data(
                field_list=list(_FIELDS),
                stock_list=list(native_codes),
                period=frequency,
                start_time="",
                end_time=end_at.strftime("%Y%m%d%H%M%S"),
                count=count,
                dividend_type="none",
                fill_data=False,
            )
        self._report_progress()
        if not isinstance(raw, Mapping):
            return None
        required_member_count = _composite_required_member_count(
            len(composite_members),
            minimum_member_count=self._minimum_member_count,
            minimum_bar_coverage=self._minimum_bar_coverage,
        )
        return _grouped_composite_ratios(
            raw,
            composite_members=composite_members,
            factor_events_by_code=factor_events_by_code,
            frequency=frequency,
            observed_at=observed_at,
            minimum_member_count=self._minimum_member_count,
            required_member_count=required_member_count,
        )

    def _incrementally_extend_fact_frame(
        self,
        *,
        previous: pd.DataFrame,
        prior_grid_index: int,
        sector_id: str,
        composite_members: tuple[str, ...],
        factor_events_by_code: Mapping[
            str, tuple[QmtCausalFactorEvent, ...]
        ],
        factor_revision: str,
        frequency: str,
        observed_at: datetime,
        expected_closes: tuple[datetime, ...],
        request_bars: int,
    ) -> pd.DataFrame | None:
        """Extend a proven complete calendar prefix after overlap validation."""

        new_closes = expected_closes[prior_grid_index + 1 :]
        overlap_count = min(
            _QMT_SECTOR_COMPOSITE_INCREMENTAL_OVERLAP_BARS,
            len(previous) - 1,
        )
        if (
            frequency != "5m"
            or not new_closes
            or overlap_count <= 0
            or len(previous) + len(new_closes) > request_bars
        ):
            return None
        anchor_count = min(
            _QMT_SECTOR_COMPOSITE_INCREMENTAL_OVERLAP_BARS,
            len(previous),
        )
        anchor_actual = previous.iloc[:anchor_count].reset_index(drop=True)
        anchor_closes = tuple(
            normalize_datetime(
                pd.Timestamp(value).to_pydatetime(),
                "incremental sector anchor close",
            )
            for value in anchor_actual["date"]
        )
        try:
            anchor_grid_index = expected_closes.index(anchor_closes[0])
        except ValueError:
            return None
        if (
            anchor_grid_index <= 0
            or anchor_closes
            != expected_closes[
                anchor_grid_index : anchor_grid_index + anchor_count
            ]
        ):
            return None
        anchor_grouped = self._read_grouped_composite_ratios(
            composite_members=composite_members,
            factor_events_by_code=factor_events_by_code,
            frequency=frequency,
            observed_at=observed_at,
            end_at=anchor_closes[-1],
            count=anchor_count + 32,
        )
        if anchor_grouped is None or anchor_grouped.empty:
            return None
        anchor_available_closes = {
            normalize_datetime(
                pd.Timestamp(value).to_pydatetime(),
                "incremental sector anchor grouped close",
            )
            for value in anchor_grouped.index
        }
        if (
            expected_closes[anchor_grid_index - 1]
            in anchor_available_closes
            or any(
                value not in anchor_available_closes
                for value in anchor_closes
            )
        ):
            return None
        rebuilt_anchor = _composite_rows_from_grouped_ratios(
            anchor_grouped.loc[list(anchor_closes)],
            sector_id=sector_id,
            starting_close=1000.0,
        )
        if not _composite_rows_match(
            anchor_actual,
            rebuilt_anchor,
            expected_closes=anchor_closes,
        ):
            return None
        tail_history_bars = len(new_closes) + overlap_count + 1
        self._prepare_history(
            members=composite_members,
            as_of=observed_at,
            expected_closes=expected_closes,
            required_bars=tail_history_bars,
            frequency=frequency,
        )
        grouped = self._read_grouped_composite_ratios(
            composite_members=composite_members,
            factor_events_by_code=factor_events_by_code,
            frequency=frequency,
            observed_at=observed_at,
            end_at=observed_at,
            count=tail_history_bars + 32,
        )
        if grouped is None or grouped.empty:
            return None
        available_closes = {
            normalize_datetime(
                pd.Timestamp(value).to_pydatetime(),
                "incremental sector grouped close",
            )
            for value in grouped.index
        }
        prior_tail = previous.iloc[-(overlap_count + 1) :].reset_index(
            drop=True
        )
        verification_closes = tuple(
            normalize_datetime(
                pd.Timestamp(value).to_pydatetime(),
                "incremental sector verification close",
            )
            for value in prior_tail["date"].iloc[1:]
        )
        required_closes = (*verification_closes, *new_closes)
        if any(value not in available_closes for value in required_closes):
            return None
        verification_grouped = grouped.loc[list(verification_closes)]
        rebuilt_tail = _composite_rows_from_grouped_ratios(
            verification_grouped,
            sector_id=sector_id,
            starting_close=float(prior_tail["close"].iloc[0]),
        )
        actual_tail = prior_tail.iloc[1:].reset_index(drop=True)
        if not _composite_rows_match(
            actual_tail,
            rebuilt_tail,
            expected_closes=verification_closes,
        ):
            return None
        appended = _composite_rows_from_grouped_ratios(
            grouped.loc[list(new_closes)],
            sector_id=sector_id,
            starting_close=float(previous["close"].iloc[-1]),
        )
        columns = [
            "code",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "member_mask",
        ]
        result = pd.concat(
            (previous.loc[:, columns], appended.loc[:, columns]),
            ignore_index=True,
        )
        result_dates = tuple(
            normalize_datetime(
                pd.Timestamp(value).to_pydatetime(),
                "incremental sector result close",
            )
            for value in result["date"]
        )
        if (
            len(result) > request_bars
            or result_dates != expected_closes[-len(result) :]
        ):
            return None
        metadata = build_causal_sector_price_basis_metadata(
            provider=QMT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=(
                f"{sector_id}:"
                + str(previous.attrs["sector_membership_revision"]).removeprefix(
                    "sha256:"
                )
            ),
            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=_COMPOSITE_QUANTUM,
            factor_revision=factor_revision,
        )
        result = attach_price_basis_metadata(result, metadata)
        return _attach_composite_provenance(
            result,
            sector_id=sector_id,
            membership_revision=str(
                previous.attrs["sector_membership_revision"]
            ),
            members=tuple(previous.attrs["sector_members"]),
            composite_members=tuple(
                previous.attrs["sector_composite_members"]
            ),
            minimum_member_count=self._minimum_member_count,
            minimum_bar_coverage=self._minimum_bar_coverage,
            maximum_composite_members=self._maximum_composite_members,
            factor_revision=factor_revision,
        )

    @staticmethod
    def _session_closes(
        trading_day: date,
        frequency: str,
    ) -> tuple[datetime, ...]:
        if frequency == "1d":
            slots = ((15, 0),)
        elif frequency == "30m":
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
            self._report_progress()
            with _XTDATA_NATIVE_LOCK:
                response = xtdata.get_trading_dates(
                    "SH",
                    (
                        as_of
                        - timedelta(days=_QMT_TRADING_CALENDAR_LOOKBACK_DAYS)
                    ).strftime("%Y%m%d"),
                    as_of.strftime("%Y%m%d"),
                    -1,
                )
            self._report_progress()
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

    def _calendar_grid(
        self,
        as_of: datetime,
        frequency: str,
    ) -> tuple[tuple[datetime, ...], str]:
        observed = normalize_datetime(as_of, "as_of")
        key = (frequency, self._bucket(observed, frequency))
        with self._lock:
            cached = self._calendar_grid_cache.pop(key, None)
            if cached is not None:
                self._calendar_grid_cache[key] = cached
                return cached
        candidates = tuple(
            close
            for trading_day in self._trading_dates(observed)
            for close in self._session_closes(trading_day, frequency)
            if close <= observed
        )
        if not candidates:
            raise RuntimeError("QMT trading calendar has no closed sector bar")
        result = (
            candidates,
            sha256_json(
                {
                    "schema": "chanlun-qmt-sector-calendar-grid",
                    "frequency": frequency,
                    "expected_closes": candidates,
                }
            ),
        )
        with self._lock:
            self._calendar_grid_cache[key] = result
            while (
                len(self._calendar_grid_cache)
                > _QMT_SECTOR_CALENDAR_GRID_CACHE_CAPACITY
            ):
                self._calendar_grid_cache.popitem(last=False)
        return result

    def _expected_closes(
        self,
        as_of: datetime,
        frequency: str,
    ) -> tuple[datetime, ...]:
        return self._calendar_grid(as_of, frequency)[0]

    def _causal_factor_snapshot(
        self,
        *,
        members: tuple[str, ...],
        as_of: datetime,
    ) -> tuple[
        dict[str, tuple[QmtCausalFactorEvent, ...]],
        str,
    ]:
        """Read each member's factor ledger once per decision day.

        Empty DataFrames are valid proof that a member has no event in the
        bounded history.  Missing, malformed, or failing factor responses are
        not treated as an empty ledger: the sector source fails closed.
        """

        observed_day = as_of.date()
        not_before = observed_day - timedelta(
            days=_QMT_TRADING_CALENDAR_LOOKBACK_DAYS
        )
        output: dict[str, tuple[QmtCausalFactorEvent, ...]] = {}
        for code in members:
            key = (observed_day, code)
            with self._lock:
                cached = self._factor_cache.get(key)
            if cached is None:
                self._report_progress()
                try:
                    with _XTDATA_NATIVE_LOCK:
                        raw = xtdata.get_divid_factors(
                            _qmt_code(code),
                            not_before.strftime("%Y%m%d"),
                            observed_day.strftime("%Y%m%d"),
                        )
                    cached = qmt_causal_factor_events_from_frame(
                        code=code,
                        frame=raw,
                        not_before=not_before,
                        not_after=observed_day,
                    )
                except Exception as exc:
                    raise RuntimeError(
                        f"QMT causal factor ledger unavailable for {code}"
                    ) from exc
                self._report_progress()
                with self._lock:
                    self._factor_cache[key] = cached
            output[code] = cached
        revision = qmt_causal_factor_revision(
            members=members,
            events_by_code=output,
            known_through=observed_day,
        )
        return output, revision

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
        if not isinstance(sector_id, str) or not sector_id.startswith(
            ("qmt-gics3:", "qmt-gics4:")
        ):
            raise ValueError("invalid QMT GICS3/GICS4 sector id")
        if not isinstance(sector_name, str) or not sector_name.strip():
            raise ValueError("sector_name is required")
        if frequency not in _FREQUENCY_SECONDS:
            raise ValueError("QMT sector frequency must be 5m, 30m or 1d")
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
                "schema": "chanlun-qmt-gics3-members",
                "sector_id": sector_id,
                "members": members,
                "composite_members": composite_members,
            }
        ).removeprefix("sha256:")
        cache_key = (sector_id, frequency, request_bars)
        bucket = self._bucket(observed_at, frequency)
        with self._lock:
            cached = self._cache.pop(cache_key, None)
            if (
                cached is not None
                and cached[0] == bucket
                and cached[1] == membership_revision
            ):
                self._cache[cache_key] = cached
                return _copy_frame(cached[2])
            supersets = tuple(
                (cached_request_bars, cached_value)
                for (
                    cached_sector_id,
                    cached_frequency,
                    cached_request_bars,
                ), cached_value in self._cache.items()
                if cached_sector_id == sector_id
                and cached_frequency == frequency
                and cached_request_bars > request_bars
                and cached_value[0] == bucket
                and cached_value[1] == membership_revision
                and len(cached_value[2]) >= request_bars
            )
            if supersets:
                _, reusable = min(supersets, key=lambda item: item[0])
                sliced = _tail_composite_frame(reusable[2], request_bars)
                self._remember_frame(
                    cache_key,
                    (
                        bucket,
                        membership_revision,
                        _copy_frame(sliced),
                    ),
                )
                return _copy_frame(sliced)

        empty = _empty_composite_frame(
            sector_id,
            membership_revision,
            members=members,
            composite_members=composite_members,
            minimum_member_count=self._minimum_member_count,
            minimum_bar_coverage=self._minimum_bar_coverage,
            maximum_composite_members=self._maximum_composite_members,
        )
        if len(members) < self._minimum_member_count:
            result = empty
        else:
            factor_events_by_code, factor_revision = (
                self._causal_factor_snapshot(
                    members=composite_members,
                    as_of=observed_at,
                )
            )
            empty = _empty_composite_frame(
                sector_id,
                membership_revision,
                members=members,
                composite_members=composite_members,
                minimum_member_count=self._minimum_member_count,
                minimum_bar_coverage=self._minimum_bar_coverage,
                maximum_composite_members=self._maximum_composite_members,
                factor_revision=factor_revision,
            )
            expected_closes, calendar_grid_revision = self._calendar_grid(
                observed_at,
                frequency,
            )
            expected_closed_at = expected_closes[-1]
            fact_path = self._fact_path(
                sector_id=sector_id,
                frequency=frequency,
                request_bars=request_bars,
            )
            fact_identity = (
                None
                if self._fact_cache_revision is None
                else self._fact_identity(
                    sector_id=sector_id,
                    members=members,
                    composite_members=composite_members,
                    membership_revision=membership_revision,
                    frequency=frequency,
                    request_bars=request_bars,
                    expected_closed_at=expected_closed_at,
                    calendar_grid_started_at=expected_closes[0],
                    calendar_grid_bar_count=len(expected_closes),
                    calendar_grid_revision=calendar_grid_revision,
                    factor_revision=factor_revision,
                )
            )
            fact_payload = (
                None
                if fact_identity is None
                else self._load_fact_payload(fact_path)
            )
            persisted = (
                None
                if fact_identity is None or fact_payload is None
                else self._frame_from_fact_payload(
                    fact_payload,
                    identity=fact_identity,
                    observed_at=observed_at,
                    expected_closed_at=expected_closed_at,
                    expected_closes=expected_closes,
                )
            )
            if persisted is not None:
                self._record_fact_cache_event("exact_hits")
                with self._lock:
                    self._remember_frame(
                        cache_key,
                        (
                            bucket,
                            membership_revision,
                            _copy_frame(persisted),
                        ),
                    )
                return _copy_frame(persisted)
            prior_fact = (
                None
                if fact_identity is None or fact_payload is None
                else self._prior_fact_frame_from_payload(
                    fact_payload,
                    identity=fact_identity,
                    observed_at=observed_at,
                    expected_closes=expected_closes,
                )
            )
            if prior_fact is not None:
                self._record_fact_cache_event("incremental_attempts")
            extended = (
                None
                if prior_fact is None
                else self._incrementally_extend_fact_frame(
                    previous=prior_fact[0],
                    prior_grid_index=prior_fact[1],
                    sector_id=sector_id,
                    composite_members=composite_members,
                    factor_events_by_code=factor_events_by_code,
                    factor_revision=factor_revision,
                    frequency=frequency,
                    observed_at=observed_at,
                    expected_closes=expected_closes,
                    request_bars=request_bars,
                )
            )
            if extended is not None:
                self._record_fact_cache_event("incremental_hits")
                if fact_identity is not None:
                    self._persist_fact_frame(
                        path=fact_path,
                        identity=fact_identity,
                        frame=extended,
                    )
                with self._lock:
                    self._remember_frame(
                        cache_key,
                        (
                            bucket,
                            membership_revision,
                            _copy_frame(extended),
                        ),
                    )
                return _copy_frame(extended)
            if prior_fact is not None:
                self._record_fact_cache_event("incremental_fallbacks")
            if fact_identity is not None:
                self._record_fact_cache_event("full_rebuilds")
            base_frequency = "1d" if frequency == "1d" else "5m"
            base_expected_closes = (
                expected_closes
                if frequency in {"5m", "1d"}
                else self._expected_closes(observed_at, "5m")
            )
            required_base_bars = min(
                len(base_expected_closes),
                request_bars + 1
                if frequency in {"5m", "1d"}
                else request_bars * 6 + 1,
            )
            self._prepare_history(
                members=composite_members,
                as_of=observed_at,
                expected_closes=base_expected_closes,
                required_bars=required_base_bars,
                frequency=base_frequency,
            )
            native_codes = tuple(_qmt_code(code) for code in composite_members)
            self._report_progress()
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
            self._report_progress()
            if not isinstance(raw, Mapping):
                result = empty
            else:
                member_frames: list[pd.DataFrame] = []
                for member_index, (normalized_code, native_code) in enumerate(
                    zip(composite_members, native_codes, strict=True)
                ):
                    ratios = _member_ratios(
                        raw,
                        native_code,
                        normalized_code=normalized_code,
                        factor_events=factor_events_by_code[normalized_code],
                        frequency=frequency,
                        not_after=observed_at,
                    )
                    if ratios is None:
                        continue
                    ratios.insert(0, "member_bit", 1 << member_index)
                    member_frames.append(ratios)
                if len(member_frames) < self._minimum_member_count:
                    result = empty
                else:
                    facts = pd.concat(member_frames, ignore_index=True)
                    required_count = _composite_required_member_count(
                        len(composite_members),
                        minimum_member_count=self._minimum_member_count,
                        minimum_bar_coverage=self._minimum_bar_coverage,
                    )
                    grouped = facts.groupby("date", sort=True).agg(
                        member_count=("member_bit", "size"),
                        member_mask=("member_bit", "sum"),
                        open_ratio=("open_ratio", "median"),
                        high_ratio=("high_ratio", "median"),
                        low_ratio=("low_ratio", "median"),
                        close_ratio=("close_ratio", "median"),
                    )
                    grouped = grouped[grouped["member_count"] >= required_count]
                    grouped_closes = tuple(
                        normalize_datetime(
                            pd.Timestamp(value).to_pydatetime(),
                            "sector grouped close",
                        )
                        for value in grouped.index
                    )
                    suffix_length = _latest_contiguous_calendar_suffix_length(
                        grouped_closes,
                        expected_closes,
                    )
                    minimum_suffix_length = min(request_bars, 2)
                    if suffix_length < minimum_suffix_length:
                        result = empty
                    else:
                        # 只保留以最新已完成柱结尾、与交易日历逐根一致的连续后缀。
                        # 早期覆盖不足不能使整个当前序列失效；近期缺口仍会因后缀过短
                        # 失败关闭。裁剪后从统一基准重新连乘，避免跨缺口收益污染后缀。
                        grouped = grouped.iloc[-suffix_length:]
                        close_ratios = grouped["close_ratio"].to_numpy(
                            dtype=np.float64,
                            copy=False,
                        )
                        close_values = np.multiply.accumulate(
                            np.concatenate(
                                (np.array([1000.0]), close_ratios)
                            )
                        )[1:]
                        previous_closes = np.concatenate(
                            (np.array([1000.0]), close_values[:-1])
                        )
                        open_values = previous_closes * grouped[
                            "open_ratio"
                        ].to_numpy(dtype=np.float64, copy=False)
                        high_values = np.maximum.reduce(
                            (
                                previous_closes
                                * grouped["high_ratio"].to_numpy(
                                    dtype=np.float64,
                                    copy=False,
                                ),
                                open_values,
                                close_values,
                            )
                        )
                        low_values = np.minimum.reduce(
                            (
                                previous_closes
                                * grouped["low_ratio"].to_numpy(
                                    dtype=np.float64,
                                    copy=False,
                                ),
                                open_values,
                                close_values,
                            )
                        )
                        result = (
                            pd.DataFrame(
                                {
                                    "code": sector_id,
                                    "date": grouped.index.to_numpy(),
                                    "open": open_values,
                                    "high": high_values,
                                    "low": low_values,
                                    "close": close_values,
                                    "volume": grouped[
                                        "member_count"
                                    ].to_numpy(dtype=np.float64),
                                    "member_mask": grouped[
                                        "member_mask"
                                    ].to_numpy(dtype=np.int64),
                                }
                            )
                            .tail(request_bars)
                            .reset_index(drop=True)
                        )
                    if not result.empty:
                        metadata = build_causal_sector_price_basis_metadata(
                            provider=QMT_GICS3_COMPOSITE_PROVIDER,
                            market="a",
                            code=f"{sector_id}:{membership_revision}",
                            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
                            structure_price_quantum=_COMPOSITE_QUANTUM,
                            factor_revision=factor_revision,
                        )
                        result = attach_price_basis_metadata(result, metadata)
                        result = _attach_composite_provenance(
                            result,
                            sector_id=sector_id,
                            membership_revision="sha256:" + membership_revision,
                            members=members,
                            composite_members=composite_members,
                            minimum_member_count=self._minimum_member_count,
                            minimum_bar_coverage=self._minimum_bar_coverage,
                            maximum_composite_members=(
                                self._maximum_composite_members
                            ),
                            factor_revision=factor_revision,
                        )
                        if frequency == "1d":
                            result = _attach_native_daily_composite_provenance(
                                result,
                                sector_id=sector_id,
                                observed_at=observed_at,
                            )

            if not result.empty and fact_identity is not None:
                self._persist_fact_frame(
                    path=fact_path,
                    identity=fact_identity,
                    frame=result,
                )

        if not result.empty:
            with self._lock:
                self._remember_frame(
                    cache_key,
                    (
                        bucket,
                        membership_revision,
                        _copy_frame(result),
                    ),
                )
        return _copy_frame(result)


def _daily_rows(
    raw: Mapping[str, object],
    native_code: str,
    *,
    not_after: datetime,
) -> tuple[DailyMarketBar, ...]:
    """Convert QMT's field-oriented daily response into completed bars."""

    values: dict[str, pd.Series] = {}
    shared_columns = None
    for field in _DAILY_FIELDS:
        source = raw.get(field)
        if not isinstance(source, pd.DataFrame) or source.index.has_duplicates:
            return ()
        if native_code not in source.index:
            return ()
        if shared_columns is None:
            shared_columns = source.columns
        elif not source.columns.equals(shared_columns):
            return ()
        row = source.loc[native_code]
        if not isinstance(row, pd.Series):
            return ()
        values[field] = row.reset_index(drop=True)
    frame = pd.DataFrame(values)
    for field in _DAILY_FIELDS:
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame = frame.dropna(subset=list(_DAILY_FIELDS))
    if frame.empty:
        return ()
    frame["date"] = pd.to_datetime(frame["time"], unit="ms", utc=True).dt.tz_convert(
        "Asia/Shanghai"
    )
    prices = frame.loc[:, list(_PRICE_FIELDS)]
    finite = np.isfinite(
        frame.loc[:, list(_DAILY_FIELDS)].to_numpy(dtype=np.float64)
    ).all(axis=1)
    valid = (
        finite
        & (prices > 0).all(axis=1)
        & (frame["volume"] >= 0)
        & (frame["high"] >= prices.max(axis=1))
        & (frame["low"] <= prices.min(axis=1))
    )
    frame = frame.loc[valid].sort_values("date")
    cutoff = normalize_datetime(not_after, "not_after")
    by_session: dict[date, DailyMarketBar] = {}
    for item in frame.itertuples(index=False):
        session = pd.Timestamp(item.date).date()
        known_at = datetime.combine(session, time(15, 0), tzinfo=_SHANGHAI)
        if known_at > cutoff:
            continue
        by_session[session] = DailyMarketBar(
            session=session,
            open=Decimal(str(item.open)),
            high=Decimal(str(item.high)),
            low=Decimal(str(item.low)),
            close=Decimal(str(item.close)),
            volume=Decimal(str(item.volume)),
            known_at=known_at,
        )
    return tuple(by_session[key] for key in sorted(by_session))


def _normalize_equal_ratio_daily_bars(
    rows: tuple[DailyMarketBar, ...],
) -> tuple[DailyMarketBar, ...]:
    """Remove the future-wide scale from QMT equal-ratio front adjustment.

    A corporate action after the decision cutoff multiplies every already
    visible ``front_ratio`` price by the same positive factor.  Dividing all
    OHLC values by the last visible close therefore makes both the broad-index
    fractal topology and every member's close-vs-SMA category invariant to
    that future event.  Actions already effective by the cutoff retain their
    piecewise adjustment and continue to remove the ex-date discontinuity.

    Twelve decimal places are deliberately retained: this is far below the
    input price tick while making the persisted fact identity insensitive to
    harmless binary-float representation noise in QMT's RPC response.
    """

    if not rows:
        return ()
    scale = rows[-1].close
    if not scale.is_finite() or scale <= 0:
        raise ValueError("daily strength normalization scale must be positive")
    quantum = Decimal("0.000000000001")

    def normalized(value: Decimal) -> Decimal:
        return (value / scale).quantize(quantum)

    return tuple(
        DailyMarketBar(
            session=value.session,
            open=normalized(value.open),
            high=normalized(value.high),
            low=normalized(value.low),
            close=normalized(value.close),
            volume=value.volume,
            known_at=value.known_at,
            completed=value.completed,
        )
        for value in rows
    )


def _normalize_equal_ratio_daily_closes(
    rows: tuple[DailyMarketBar, ...],
    *,
    sessions: dict[date, date],
    known_at_by_session: dict[date, datetime],
) -> tuple[CompletedDailyClose, ...]:
    """Keep only the member fields consumed by horizontal strength.

    Member OHLC and volume are validated by ``_daily_rows`` but never enter
    the equal-weight close-vs-SMA rule.  Retaining five Decimals per member bar
    made the full GICS hierarchy exceed the isolated worker's memory boundary.
    The broad benchmark still uses ``_normalize_equal_ratio_daily_bars`` and
    therefore keeps complete OHLCV for its bottom-fractal anchor.
    """

    if not rows:
        return ()
    scale = rows[-1].close
    if not scale.is_finite() or scale <= 0:
        raise ValueError("daily strength normalization scale must be positive")
    quantum = Decimal("0.000000000001")
    output: list[CompletedDailyClose] = []
    for value in rows:
        session = sessions.setdefault(value.session, value.session)
        known_at = known_at_by_session.setdefault(session, value.known_at)
        if known_at != value.known_at:
            raise ValueError("daily strength session publication time drifted")
        output.append(
            CompletedDailyClose(
                session=session,
                close=(value.close / scale).quantize(quantum),
                known_at=known_at,
                completed=value.completed,
            )
        )
    return tuple(output)


class QmtSectorStrengthSource:
    """Daily all-member horizontal strength source for the live sector scan.

    Unlike the intraday display composite, this path never samples the first
    24 members.  Every current QMT member is requested and contributes equal
    weight.  QMT equal-ratio front-adjusted prices are terminal-close
    normalized so later corporate actions cannot rewrite an earlier ranking.
    Results are cached by completed daily cutoff and membership hash.
    """

    def __init__(
        self,
        *,
        benchmark_symbol: str = "SH.000300",
        request_bars: int = 300,
        request_chunk_size: int = _DAILY_STRENGTH_REQUEST_CHUNK_SIZE,
        progress_callback: Callable[[], None] = lambda: None,
        fact_cache_path: Path | str | None = None,
        fact_cache_revision: str | None = None,
        status_fact_directory: Path | str | None = None,
        status_capture_clock: Callable[[], datetime] = (
            lambda: datetime.now(_SHANGHAI)
        ),
    ) -> None:
        if _NORMALIZED_A_SHARE_CODE.fullmatch(benchmark_symbol) is None:
            raise ValueError("benchmark_symbol must be a normalized A-share code")
        if request_bars < 233 or request_chunk_size <= 0:
            raise ValueError("daily sector strength history configuration is invalid")
        if not callable(progress_callback):
            raise TypeError("progress_callback must be callable")
        if not callable(status_capture_clock):
            raise TypeError("status_capture_clock must be callable")
        cache_path, cache_revision = _fact_cache_options(
            fact_cache_path,
            fact_cache_revision,
            path_field="fact_cache_path",
        )
        self._benchmark_symbol = benchmark_symbol
        self._request_bars = request_bars
        self._request_chunk_size = request_chunk_size
        self._progress_callback = progress_callback
        self._fact_cache_path = cache_path
        self._fact_cache_revision = cache_revision
        if status_fact_directory is not None and cache_revision is None:
            raise ValueError(
                "status_fact_directory requires authenticated fact caching"
            )
        self._status_fact_directory = (
            None
            if status_fact_directory is None
            else Path(status_fact_directory).resolve()
        )
        self._status_capture_clock = status_capture_clock
        self._lock = RLock()
        self._listing_session_cache: dict[
            tuple[date, date], tuple[date, ...]
        ] = {}
        self._cache: dict[
            tuple[
                date,
                bool,
                str,
                date | None,
                tuple[tuple[str, tuple[str, ...]], ...],
            ],
            SectorStrengthBatch,
        ] = {}

    @staticmethod
    def _after_daily_close(observed: datetime) -> bool:
        return observed.timetz().replace(tzinfo=None) >= time(15, 0)

    def _benchmark_cutoff_complete(
        self,
        bars: Mapping[str, _DailyStrengthHistory],
        *,
        required_session: date | None,
    ) -> bool:
        """Prove that the benchmark reaches the latest required daily close.

        QMT may publish the completed intraday 15:00 bars before its 1d table.
        It may also leave an index's local 1d cache stale while member stocks
        are current.  Persisting either response would freeze a stale
        sector ranking for the decision phase.  The QMT calendar proves the
        required session and the broad benchmark is its publication watermark;
        suspended members may legitimately end earlier.
        """

        if required_session is None:
            return False
        benchmark = bars.get(self._benchmark_symbol, ())
        return bool(benchmark) and benchmark[-1].session == required_session

    @staticmethod
    def _bundle_cutoff_complete(
        bars: Mapping[str, _DailyStrengthHistory],
        *,
        symbols: tuple[str, ...],
        required_session: date | None,
        explained_missing: frozenset[str] = frozenset(),
    ) -> bool:
        """Require every current member to reach the proven daily cutoff.

        A shorter history may be a real suspension or a member whose verified
        IPO date is after the required completed session.  Without either
        point-in-time fact it is indistinguishable from a stale QMT local
        cache.  The selection specification forbids guessing that missing
        fact, so the raw bundle remains retryable and affected sectors are
        unresolved.
        """

        return required_session is not None and not tuple(
            symbol
            for symbol in QmtSectorStrengthSource._incomplete_symbols(
                bars,
                symbols=symbols,
                required_session=required_session,
            )
            if symbol not in explained_missing
        )

    @staticmethod
    def _incomplete_symbols(
        bars: Mapping[str, _DailyStrengthHistory],
        *,
        symbols: tuple[str, ...],
        required_session: date | None,
    ) -> tuple[str, ...]:
        if required_session is None:
            return symbols
        return tuple(
            symbol
            for symbol in symbols
            if not bars.get(symbol)
            or bars[symbol][-1].session != required_session
        )

    def _status_fact_session_directory(self, session: date) -> Path | None:
        if self._status_fact_directory is None:
            return None
        return self._status_fact_directory / session.isoformat()

    def _listing_fact_directory(self) -> Path | None:
        if self._status_fact_directory is None:
            return None
        return self._status_fact_directory / "listing"

    def _listing_facts_from_payload(
        self,
        payload: Mapping[str, object],
        *,
        observed: datetime,
    ) -> dict[str, date] | None:
        if (
            payload.get("schema") != _MEMBER_LISTING_FACT_SCHEMA
            or payload.get("producer_revision") != self._fact_cache_revision
            or payload.get("source_method") != "QMT_GET_INSTRUMENT_DETAIL"
            or payload.get("point_in_time_scope") != "AFTER_CAPTURE_ONLY"
            or payload.get("minimum_market_data_frequency") != "1m"
            or payload.get("tick_data_used") is not False
            or payload.get("real_account_access") is not False
            or payload.get("real_order_transport") is not False
        ):
            return None
        try:
            captured_at = normalize_datetime(
                datetime.fromisoformat(str(payload["captured_at"])),
                "listing_fact.captured_at",
            )
        except (KeyError, TypeError, ValueError):
            return None
        if captured_at > observed:
            return None
        raw_facts = payload.get("facts")
        raw_symbols = payload.get("symbols")
        if (
            not isinstance(raw_facts, Mapping)
            or any(not isinstance(key, str) for key in raw_facts)
            or raw_symbols != sorted(raw_facts)
        ):
            return None
        expected_fields = {"native_code", "open_date"}
        result: dict[str, date] = {}
        for symbol, raw in raw_facts.items():
            if (
                _NORMALIZED_A_SHARE_CODE.fullmatch(symbol) is None
                or not isinstance(raw, Mapping)
                or set(raw) != expected_fields
                or raw.get("native_code") != _qmt_code(symbol)
            ):
                return None
            try:
                listed_on = date.fromisoformat(str(raw["open_date"]))
            except (KeyError, TypeError, ValueError):
                return None
            if listed_on < date(1990, 1, 1) or listed_on > captured_at.date():
                return None
            result[symbol] = listed_on
        return result

    def _load_listing_facts(
        self,
        symbols: tuple[str, ...],
        *,
        observed: datetime,
    ) -> dict[str, date]:
        directory = self._listing_fact_directory()
        if not symbols or directory is None or not directory.is_dir():
            return {}
        requested = frozenset(symbols)
        accepted: dict[str, date] = {}
        conflicts: set[str] = set()
        self._progress_callback()
        try:
            for path in sorted(directory.glob("*.json")):
                payload = _read_fact_payload(path)
                if payload is None:
                    continue
                facts = self._listing_facts_from_payload(
                    payload,
                    observed=observed,
                )
                if facts is None:
                    continue
                for symbol, listed_on in facts.items():
                    if symbol not in requested:
                        continue
                    prior = accepted.get(symbol)
                    if prior is not None and prior != listed_on:
                        conflicts.add(symbol)
                    else:
                        accepted[symbol] = listed_on
        except OSError:
            return {}
        finally:
            self._progress_callback()
        for symbol in conflicts:
            accepted.pop(symbol, None)
        return accepted

    def _persist_listing_facts(
        self,
        *,
        captured_at: datetime,
        facts: Mapping[str, date],
    ) -> None:
        directory = self._listing_fact_directory()
        if directory is None or not facts:
            return
        payload: dict[str, object] = {
            "schema": _MEMBER_LISTING_FACT_SCHEMA,
            "producer_revision": self._fact_cache_revision,
            "captured_at": captured_at.isoformat(),
            "source_method": "QMT_GET_INSTRUMENT_DETAIL",
            "point_in_time_scope": "AFTER_CAPTURE_ONLY",
            "symbols": sorted(facts),
            "facts": {
                symbol: {
                    "native_code": _qmt_code(symbol),
                    "open_date": facts[symbol].isoformat(),
                }
                for symbol in sorted(facts)
            },
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
        path = directory / f"{sha256_json(payload).removeprefix('sha256:')}.json"
        self._progress_callback()
        try:
            if not path.exists():
                _write_fact_payload(path, payload)
        except OSError:
            pass
        finally:
            self._progress_callback()

    def _capture_current_listing_dates(
        self,
        symbols: tuple[str, ...],
        *,
        observed: datetime,
    ) -> tuple[dict[str, date], dict[str, Mapping[str, object]]]:
        captured_at = normalize_datetime(
            self._status_capture_clock(),
            "status_capture_clock",
        )
        if not symbols or self._listing_fact_directory() is None:
            return {}, {}
        result: dict[str, date] = {}
        details: dict[str, Mapping[str, object]] = {}
        for symbol in symbols:
            try:
                self._progress_callback()
                with _XTDATA_NATIVE_LOCK:
                    detail = xtdata.get_instrument_detail(
                        _qmt_code(symbol),
                        iscomplete=False,
                    )
            except Exception:
                continue
            finally:
                self._progress_callback()
            if not isinstance(detail, Mapping):
                continue
            details[symbol] = dict(detail)
            raw_open_date = str(detail.get("OpenDate") or "").strip()
            try:
                listed_on = datetime.strptime(raw_open_date, "%Y%m%d").date()
            except ValueError:
                continue
            if listed_on < date(1990, 1, 1) or listed_on > captured_at.date():
                continue
            result[symbol] = listed_on
        self._persist_listing_facts(
            captured_at=captured_at,
            facts=result,
        )
        return (
            result if captured_at <= observed else {},
            details,
        )

    def _listing_sessions(
        self,
        listed_on: date,
        required_session: date,
    ) -> tuple[date, ...] | None:
        key = (listed_on, required_session)
        cached = self._listing_session_cache.get(key)
        if cached is not None:
            return cached
        try:
            self._progress_callback()
            try:
                with _XTDATA_NATIVE_LOCK:
                    raw_sessions = xtdata.get_trading_dates(
                        "SH",
                        listed_on.strftime("%Y%m%d"),
                        required_session.strftime("%Y%m%d"),
                        -1,
                    )
                    raw_published = xtdata.get_market_last_trade_date("SH")
            finally:
                self._progress_callback()
            if type(raw_sessions) is not list:
                return None
            sessions = tuple(
                sorted({_qmt_calendar_date(value) for value in raw_sessions})
            )
            published_through = _qmt_calendar_date(raw_published)
            if (
                published_through < required_session
                or any(
                    value < listed_on or value > required_session
                    for value in sessions
                )
            ):
                return None
        except Exception:
            return None
        self._listing_session_cache[key] = sessions
        return sessions

    def _new_listing_history_complete(
        self,
        bars: _DailyStrengthHistory,
        *,
        listed_on: date | None,
        required_session: date | None,
        observed: datetime,
    ) -> bool:
        if (
            listed_on is None
            or required_session is None
            or listed_on > observed.date()
        ):
            return False
        if listed_on > required_session:
            return not bars
        sessions = self._listing_sessions(listed_on, required_session)
        return bool(
            sessions
            and len(sessions) < 5
            and sessions[0] == listed_on
            and tuple(value.session for value in bars) == sessions
        )

    def _status_facts_from_payload(
        self,
        payload: Mapping[str, object],
        *,
        session: date,
        observed: datetime,
    ) -> dict[str, dict[str, object]] | None:
        """Validate one immutable same-session QMT suspension capture.

        The installed QMT client exposes only the current instrument detail;
        its historical ``is_suspended_stock`` service is unavailable.  A
        current response may therefore explain a missing daily bar only when
        its native ``TradingDay`` exactly equals the required session and the
        capture was already visible at the decision time.  Later captures are
        never backfilled into earlier decisions.
        """

        if (
            payload.get("schema") != _MEMBER_STATUS_FACT_SCHEMA
            or payload.get("producer_revision") != self._fact_cache_revision
            or payload.get("session") != session.isoformat()
            or payload.get("source_method") != "QMT_GET_INSTRUMENT_DETAIL"
            or payload.get("point_in_time_scope")
            != "CAPTURE_SESSION_AFTER_CAPTURE_ONLY"
            or payload.get("minimum_market_data_frequency") != "1m"
            or payload.get("tick_data_used") is not False
            or payload.get("real_account_access") is not False
            or payload.get("real_order_transport") is not False
        ):
            return None
        try:
            captured_at = normalize_datetime(
                datetime.fromisoformat(str(payload["captured_at"])),
                "status_fact.captured_at",
            )
        except (KeyError, TypeError, ValueError):
            return None
        if captured_at.date() != session or captured_at > observed:
            return None
        raw_facts = payload.get("facts")
        raw_symbols = payload.get("symbols")
        if not isinstance(raw_facts, Mapping) or any(
            not isinstance(key, str) for key in raw_facts
        ):
            return None
        if raw_symbols != sorted(raw_facts):
            return None
        expected_fields = {
            "native_code",
            "trading_day",
            "instrument_name",
            "instrument_status",
            "is_trading",
            "suspended",
        }
        result: dict[str, dict[str, object]] = {}
        for symbol, raw in raw_facts.items():
            if (
                _NORMALIZED_A_SHARE_CODE.fullmatch(symbol) is None
                or not isinstance(raw, Mapping)
                or set(raw) != expected_fields
                or raw.get("native_code") != _qmt_code(symbol)
                or raw.get("trading_day") != session.isoformat()
                or not isinstance(raw.get("instrument_name"), str)
                or not str(raw.get("instrument_name")).strip()
                or type(raw.get("instrument_status")) is not int
                or int(raw["instrument_status"]) < 1
                or type(raw.get("is_trading")) is not bool
                or raw.get("suspended") is not True
            ):
                return None
            result[symbol] = dict(raw)
        return result

    def _load_status_facts(
        self,
        *,
        session: date | None,
        observed: datetime,
    ) -> dict[str, dict[str, object]]:
        if session is None:
            return {}
        directory = self._status_fact_session_directory(session)
        if directory is None or not directory.is_dir():
            return {}
        self._progress_callback()
        accepted: dict[str, dict[str, object]] = {}
        conflicts: set[str] = set()
        try:
            paths = tuple(sorted(directory.glob("*.json")))
            for path in paths:
                payload = _read_fact_payload(path)
                if payload is None:
                    continue
                facts = self._status_facts_from_payload(
                    payload,
                    session=session,
                    observed=observed,
                )
                if facts is None:
                    continue
                for symbol, fact in facts.items():
                    prior = accepted.get(symbol)
                    if prior is not None and prior != fact:
                        conflicts.add(symbol)
                    else:
                        accepted[symbol] = fact
        except OSError:
            return {}
        finally:
            self._progress_callback()
        for symbol in conflicts:
            accepted.pop(symbol, None)
        return accepted

    def _persist_status_facts(
        self,
        *,
        session: date,
        captured_at: datetime,
        facts: Mapping[str, Mapping[str, object]],
    ) -> None:
        directory = self._status_fact_session_directory(session)
        if directory is None or not facts:
            return
        payload: dict[str, object] = {
            "schema": _MEMBER_STATUS_FACT_SCHEMA,
            "producer_revision": self._fact_cache_revision,
            "session": session.isoformat(),
            "captured_at": captured_at.isoformat(),
            "source_method": "QMT_GET_INSTRUMENT_DETAIL",
            "point_in_time_scope": "CAPTURE_SESSION_AFTER_CAPTURE_ONLY",
            "symbols": sorted(facts),
            "facts": {
                symbol: dict(facts[symbol]) for symbol in sorted(facts)
            },
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
        path = directory / f"{sha256_json(payload).removeprefix('sha256:')}.json"
        self._progress_callback()
        try:
            if not path.exists():
                _write_fact_payload(path, payload)
        except OSError:
            pass
        finally:
            self._progress_callback()

    def _capture_current_suspensions(
        self,
        symbols: tuple[str, ...],
        *,
        session: date | None,
        observed: datetime,
        instrument_details: Mapping[
            str, Mapping[str, object]
        ] | None = None,
    ) -> dict[str, dict[str, object]]:
        """Capture only positive same-session suspension facts, never infer.

        ``InstrumentStatus == 0`` is the observed normal state.  Negative or
        absent values are unresolved rather than silently treated as normal;
        only the SDK-documented positive suspension states can explain a
        missing daily bar.
        """

        captured_at = normalize_datetime(
            self._status_capture_clock(),
            "status_capture_clock",
        )
        if (
            not symbols
            or session is None
            or session != observed.date()
            or not self._after_daily_close(observed)
            or captured_at.date() != session
            or not self._after_daily_close(captured_at)
            or self._status_fact_directory is None
        ):
            return {}
        result: dict[str, dict[str, object]] = {}
        prefetched = instrument_details or {}
        for symbol in symbols:
            detail = prefetched.get(symbol)
            if detail is None:
                try:
                    self._progress_callback()
                    with _XTDATA_NATIVE_LOCK:
                        detail = xtdata.get_instrument_detail(
                            _qmt_code(symbol),
                            iscomplete=False,
                        )
                except Exception:
                    continue
                finally:
                    self._progress_callback()
            if not isinstance(detail, Mapping):
                continue
            raw_trading_day = str(detail.get("TradingDay") or "").strip()
            try:
                trading_day = datetime.strptime(
                    raw_trading_day,
                    "%Y%m%d",
                ).date()
            except ValueError:
                continue
            status = detail.get("InstrumentStatus")
            is_trading = detail.get("IsTrading")
            name = str(detail.get("InstrumentName") or "").strip()
            if (
                trading_day != session
                or type(status) is not int
                or int(status) < 1
                or is_trading not in {True, False, 0, 1}
                or not name
            ):
                continue
            result[symbol] = {
                "native_code": _qmt_code(symbol),
                "trading_day": trading_day.isoformat(),
                "instrument_name": name,
                "instrument_status": int(status),
                # 按当前时钟观测的 IsTrading 只保留为原始证据，不参与停牌解释。
                "is_trading": bool(is_trading),
                "suspended": True,
            }
        self._persist_status_facts(
            session=session,
            captured_at=captured_at,
            facts=result,
        )
        # 决策时刻之后读取的原生事实是有效前向证据，但不能注入更早决策。
        # 未解析批次保持可重试，后续请求可以重新加载。
        return result if captured_at <= observed else {}

    def _fact_identity(
        self,
        *,
        symbols: tuple[str, ...],
        observed: datetime,
        membership_revision: str,
        required_session: date | None,
    ) -> dict[str, object] | None:
        if self._fact_cache_revision is None or required_session is None:
            return None
        return {
            "schema": _DAILY_FACT_SCHEMA,
            "producer_revision": self._fact_cache_revision,
            "decision_date": observed.date().isoformat(),
            "after_daily_close": self._after_daily_close(observed),
            "required_daily_session": required_session.isoformat(),
            "benchmark_symbol": self._benchmark_symbol,
            "request_bars": self._request_bars,
            "symbols": list(symbols),
            "membership_revision": membership_revision,
            "benchmark_bar_fields": [
                "session",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "known_at",
                "completed",
            ],
            "member_bar_fields": [
                "session",
                "close",
                "known_at",
                "completed",
            ],
            "period": "1d",
            "qmt_dividend_type": QMT_SECTOR_STRENGTH_QMT_DIVIDEND_TYPE,
            "adjustment": QMT_SECTOR_STRENGTH_ADJUSTMENT,
            "price_basis_contract": (
                QMT_SECTOR_STRENGTH_PRICE_BASIS_CONTRACT
            ),
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }

    def _daily_bars_from_payload(
        self,
        payload: Mapping[str, object],
        *,
        identity: Mapping[str, object],
        observed: datetime,
    ) -> dict[str, _DailyStrengthHistory] | None:
        if any(payload.get(key) != value for key, value in identity.items()):
            return None
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, Mapping) or any(
            not isinstance(key, str) for key in raw_bars
        ):
            return None
        symbols = tuple(identity["symbols"])
        if set(raw_bars) != set(symbols):
            return None
        expected_incomplete = payload.get("incomplete_symbols")
        result: dict[str, _DailyStrengthHistory] = {}
        sessions_by_text: dict[str, date] = {}
        known_at_by_session: dict[date, datetime] = {}
        try:
            for ordinal, symbol in enumerate(symbols, start=1):
                # ``json.loads`` builds a large row tree.  Consume each symbol
                # as it is converted so raw strings/lists do not coexist with
                # the complete typed market-bar graph at the memory peak.
                raw_rows = (
                    raw_bars.pop(symbol)
                    if isinstance(raw_bars, dict)
                    else raw_bars[symbol]
                )
                if not isinstance(raw_rows, list) or not raw_rows:
                    return None
                if len(raw_rows) > int(identity["request_bars"]):
                    return None
                benchmark_rows = symbol == self._benchmark_symbol
                rows: list[_DailyStrengthBar] = []
                sessions: list[date] = []
                for index, value in enumerate(raw_rows):
                    expected_width = 8 if benchmark_rows else 4
                    if type(value) is not list or len(value) != expected_width:
                        return None
                    raw_session = value[0]
                    raw_close = value[4] if benchmark_rows else value[1]
                    raw_known_at = value[6] if benchmark_rows else value[2]
                    raw_completed = value[7] if benchmark_rows else value[3]
                    session_text = str(raw_session)
                    session = sessions_by_text.get(session_text)
                    if session is None:
                        session = date.fromisoformat(session_text)
                        sessions_by_text[session_text] = session
                    known_at = normalize_datetime(
                        datetime.fromisoformat(str(raw_known_at)),
                        f"bars.{symbol}[{index}].known_at",
                    )
                    expected_known_at = known_at_by_session.get(session)
                    if expected_known_at is None:
                        expected_known_at = datetime.combine(
                            session,
                            time(15, 0),
                            tzinfo=_SHANGHAI,
                        )
                        known_at_by_session[session] = expected_known_at
                    if (
                        raw_completed is not True
                        or known_at != expected_known_at
                        or known_at > observed
                    ):
                        return None
                    if benchmark_rows:
                        decimals = dict(
                            zip(
                                ("open", "high", "low", "close", "volume"),
                                (
                                    Decimal(str(value[1])),
                                    Decimal(str(value[2])),
                                    Decimal(str(value[3])),
                                    Decimal(str(raw_close)),
                                    Decimal(str(value[5])),
                                ),
                            )
                        )
                        if any(
                            not decimal.is_finite()
                            for decimal in decimals.values()
                        ):
                            return None
                        bar: _DailyStrengthBar = DailyMarketBar(
                            session=session,
                            known_at=expected_known_at,
                            completed=True,
                            **decimals,
                        )
                    else:
                        close = Decimal(str(raw_close))
                        if not close.is_finite():
                            return None
                        bar = CompletedDailyClose(
                            session=session,
                            close=close,
                            known_at=expected_known_at,
                            completed=True,
                        )
                    rows.append(bar)
                    sessions.append(session)
                if (
                    sessions != sorted(sessions)
                    or len(sessions) != len(set(sessions))
                ):
                    return None
                result[symbol] = tuple(rows)
                if ordinal % 32 == 0:
                    self._progress_callback()
        except (ArithmeticError, TypeError, ValueError):
            return None
        self._progress_callback()
        required_session = date.fromisoformat(
            str(identity["required_daily_session"])
        )
        incomplete = QmtSectorStrengthSource._incomplete_symbols(
            result,
            symbols=symbols,
            required_session=required_session,
        )
        if expected_incomplete != list(incomplete):
            return None
        return result

    def _load_daily_facts(
        self,
        *,
        identity: Mapping[str, object] | None,
        observed: datetime,
    ) -> dict[str, _DailyStrengthHistory] | None:
        if self._fact_cache_path is None or identity is None:
            return None
        self._progress_callback()
        payload = _read_fact_payload(self._fact_cache_path)
        self._progress_callback()
        if payload is None:
            return None
        return self._daily_bars_from_payload(
            payload,
            identity=identity,
            observed=observed,
        )

    def _persist_daily_facts(
        self,
        *,
        identity: Mapping[str, object] | None,
        bars: Mapping[str, _DailyStrengthHistory],
    ) -> None:
        if self._fact_cache_path is None or identity is None:
            return
        symbols = tuple(identity["symbols"])
        # 不要把 QMT 瞬时缺失响应持久化。完整标的数据包可以包含新上市标的合法的短历史，
        # 但每个请求标的至少必须有一条事实。
        if set(bars) != set(symbols) or any(not bars[symbol] for symbol in symbols):
            return
        required_session = date.fromisoformat(
            str(identity["required_daily_session"])
        )
        incomplete = self._incomplete_symbols(
            bars,
            symbols=symbols,
            required_session=required_session,
        )
        self._progress_callback()
        try:
            _write_daily_fact_payload(
                self._fact_cache_path,
                {
                    **identity,
                    "incomplete_symbols": list(incomplete),
                },
                bars,
            )
        except OSError:
            pass
        finally:
            self._progress_callback()

    def _read_daily(
        self,
        symbols: tuple[str, ...],
        *,
        as_of: datetime,
    ) -> dict[str, _DailyStrengthHistory]:
        output: dict[str, _DailyStrengthHistory] = {}
        shared_sessions: dict[date, date] = {}
        shared_known_at: dict[date, datetime] = {}
        for start in range(0, len(symbols), self._request_chunk_size):
            chunk = symbols[start : start + self._request_chunk_size]
            native = tuple(_qmt_code(value) for value in chunk)
            self._progress_callback()
            with _XTDATA_NATIVE_LOCK:
                raw = xtdata.get_market_data(
                    field_list=list(_DAILY_FIELDS),
                    stock_list=list(native),
                    period="1d",
                    start_time="",
                    end_time=as_of.strftime("%Y%m%d%H%M%S"),
                    count=self._request_bars,
                    dividend_type=QMT_SECTOR_STRENGTH_QMT_DIVIDEND_TYPE,
                    fill_data=False,
                )
            self._progress_callback()
            if not isinstance(raw, Mapping):
                del raw
                continue
            for ordinal, (symbol, native_code) in enumerate(
                zip(chunk, native, strict=True),
                start=1,
            ):
                raw_rows = _daily_rows(raw, native_code, not_after=as_of)
                rows: _DailyStrengthHistory = (
                    _normalize_equal_ratio_daily_bars(raw_rows)
                    if symbol == self._benchmark_symbol
                    else _normalize_equal_ratio_daily_closes(
                        raw_rows,
                        sessions=shared_sessions,
                        known_at_by_session=shared_known_at,
                    )
                )
                if rows:
                    output[symbol] = rows
                if ordinal % _DAILY_STRENGTH_PROGRESS_SYMBOL_INTERVAL == 0:
                    self._progress_callback()
            # QMT returns six wide pandas frames.  Release them before the next
            # chunk so the growing typed history and multiple native responses
            # never coexist at the worker's 1.5 GiB fail-closed boundary.
            del raw
            gc.collect()
            self._progress_callback()
        return output

    def _refresh_daily(self, symbols: tuple[str, ...]) -> None:
        for start in range(0, len(symbols), self._request_chunk_size):
            chunk = symbols[start : start + self._request_chunk_size]
            native = tuple(_qmt_code(value) for value in chunk)
            # Refresh only facts whose local cutoff proof failed. The native
            # call stays bounded so a wide industry cohort never falls back to
            # thousands of single-symbol downloads.
            try:
                self._progress_callback()
                with _XTDATA_NATIVE_LOCK:
                    xtdata.download_history_data2(
                        list(native),
                        "1d",
                        start_time="",
                        end_time="",
                        incrementally=True,
                    )
            except Exception:
                # The post-refresh read remains authoritative. A failed or
                # partial native refresh therefore fails closed at the cutoff
                # gate without discarding usable local history.
                pass
            finally:
                self._progress_callback()

    def _fetch(
        self,
        symbols: tuple[str, ...],
        *,
        as_of: datetime,
        required_session: date | None,
        force_refresh: bool = False,
    ) -> dict[str, _DailyStrengthHistory]:
        # Completed local QMT daily bars are already exact evidence for the
        # decision cutoff. Read and validate them before any network refresh;
        # only missing/stale symbols are downloaded and read again.
        current = self._read_daily(symbols, as_of=as_of)
        refresh_targets = (
            symbols
            if force_refresh
            else self._incomplete_symbols(
                current,
                symbols=symbols,
                required_session=required_session,
            )
        )
        if not refresh_targets:
            return current
        self._refresh_daily(refresh_targets)
        refreshed = self._read_daily(refresh_targets, as_of=as_of)
        return {
            symbol: refreshed.get(symbol, current.get(symbol, ()))
            for symbol in symbols
            if refreshed.get(symbol) or current.get(symbol)
        }

    def strengths(
        self,
        *,
        members_by_sector: Mapping[str, tuple[str, ...]],
        as_of: datetime,
        membership_revision: str,
    ) -> Mapping[str, SectorStrengthEvidence]:
        observed = normalize_datetime(as_of, "as_of")
        required_session = _latest_completed_qmt_daily_session(observed)
        member_scope_identity = tuple(
            (sector_id, tuple(sorted(set(members))))
            for sector_id, members in sorted(members_by_sector.items())
        )
        normalized_members_by_sector = dict(member_scope_identity)
        cache_key = (
            observed.date(),
            self._after_daily_close(observed),
            membership_revision,
            required_session,
            member_scope_identity,
        )
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached
        symbols = tuple(
            sorted(
                {self._benchmark_symbol}.union(
                    code
                    for members in normalized_members_by_sector.values()
                    for code in members
                )
            )
        )
        fact_identity = self._fact_identity(
            symbols=symbols,
            observed=observed,
            membership_revision=membership_revision,
            required_session=required_session,
        )
        bars = self._load_daily_facts(
            identity=fact_identity,
            observed=observed,
        )
        current_bars = {} if bars is None else bars
        member_symbols = frozenset(
            code
            for members in normalized_members_by_sector.values()
            for code in members
        )
        status_facts = self._load_status_facts(
            session=required_session,
            observed=observed,
        )
            # 状态采集只可复用于当前时点篮子中的成员，绝不能用来豁免过期的宽基基准。
        status_facts = {
            symbol: fact
            for symbol, fact in status_facts.items()
            if symbol in member_symbols
        }
        raw_incomplete = self._incomplete_symbols(
            current_bars,
            symbols=symbols,
            required_session=required_session,
        )
        refresh_targets = tuple(
            symbol
            for symbol in raw_incomplete
            if symbol not in status_facts or not current_bars.get(symbol)
        )
        if refresh_targets and required_session is not None:
            refreshed = self._fetch(
                refresh_targets,
                as_of=observed,
                required_session=required_session,
            )
            current_bars = {
                symbol: refreshed.get(symbol, current_bars.get(symbol, ()))
                for symbol in symbols
            }
            # 不完整原始事实可以安全保留，因为其精确缺失集合经过哈希认证，并在每次加载时
            # 重算。它们绝不会进入已解析排名；下次刷新只重试缺失标的，不重新下载全市场。
            self._persist_daily_facts(
                identity=fact_identity,
                bars=current_bars,
            )
        raw_incomplete = self._incomplete_symbols(
            current_bars,
            symbols=symbols,
            required_session=required_session,
        )
        listing_candidates = tuple(
            sorted(
                symbol
                for symbol in member_symbols
                if symbol not in status_facts
                and (
                    symbol in raw_incomplete
                    or len(current_bars.get(symbol, ())) < 5
                )
            )
        )
        listing_dates = self._load_listing_facts(
            listing_candidates,
            observed=observed,
        )
        uncaptured_listing = tuple(
            symbol
            for symbol in listing_candidates
            if symbol not in listing_dates
        )
        captured_listing, captured_instrument_details = (
            self._capture_current_listing_dates(
            uncaptured_listing,
            observed=observed,
            )
        )
        if captured_listing:
            listing_dates = {**listing_dates, **captured_listing}
        listing_gap_targets: list[str] = []
        if required_session is not None:
            for symbol, listed_on in listing_dates.items():
                daily = current_bars.get(symbol, ())
                if listed_on > required_session or len(daily) >= 5:
                    continue
                expected_sessions = self._listing_sessions(
                    listed_on,
                    required_session,
                )
                if (
                    expected_sessions is not None
                    and tuple(value.session for value in daily)
                    != expected_sessions
                ):
                    listing_gap_targets.append(symbol)
        if listing_gap_targets:
            refreshed = self._fetch(
                tuple(sorted(listing_gap_targets)),
                as_of=observed,
                required_session=required_session,
                force_refresh=True,
            )
            current_bars = {
                symbol: refreshed.get(symbol, current_bars.get(symbol, ()))
                for symbol in symbols
            }
            self._persist_daily_facts(
                identity=fact_identity,
                bars=current_bars,
            )
            raw_incomplete = self._incomplete_symbols(
                current_bars,
                symbols=symbols,
                required_session=required_session,
            )
        uncaptured_members = tuple(
            symbol
            for symbol in raw_incomplete
            if symbol in member_symbols and symbol not in status_facts
        )
        captured = self._capture_current_suspensions(
            uncaptured_members,
            session=required_session,
            observed=observed,
            instrument_details=captured_instrument_details,
        )
        if captured:
            status_facts = {**status_facts, **captured}
        bars = current_bars
        explained_suspended = frozenset(status_facts)
        explained_prelisting = frozenset(
            symbol
            for symbol, listed_on in listing_dates.items()
            if required_session is not None
            and listed_on > required_session
            and not bars.get(symbol)
        )
        benchmark_cutoff_complete = self._benchmark_cutoff_complete(
            bars,
            required_session=required_session,
        )
        bundle_cutoff_complete = self._bundle_cutoff_complete(
            bars,
            symbols=symbols,
            required_session=required_session,
            explained_missing=(
                explained_suspended | explained_prelisting
            ),
        )
        member_histories: dict[str, SectorMemberHistory] = {}
        for ordinal, symbol in enumerate(sorted(member_symbols), start=1):
            daily = bars.get(symbol, ())
            member_cutoff_complete = bool(daily) and (
                required_session is not None
                and daily[-1].session == required_session
            )
            member_suspended = (
                not member_cutoff_complete
                and symbol in explained_suspended
            )
            listed_on = listing_dates.get(symbol)
            new_listing_complete = self._new_listing_history_complete(
                daily,
                listed_on=listed_on,
                required_session=required_session,
                observed=observed,
            )
            member_histories[symbol] = SectorMemberHistory(
                symbol=symbol,
                listed_on=(
                    listed_on
                    or (daily[0].session if daily else observed.date())
                ),
                history_status=(
                    "COMPLETE"
                    if member_cutoff_complete and len(daily) >= 5
                    else "NEW_LISTING"
                    if new_listing_complete
                    else "SUSPENDED"
                    if member_suspended
                    else "UNEXPLAINED_GAP"
                ),
                closes=tuple(
                    (
                        value
                        if isinstance(value, CompletedDailyClose)
                        else CompletedDailyClose(
                            session=value.session,
                            close=value.close,
                            known_at=value.known_at,
                        )
                    )
                    for value in daily
                ),
            )
            if ordinal % 32 == 0:
                self._progress_callback()
        self._progress_callback()
        histories: dict[str, tuple[SectorMemberHistory, ...]] = {}
        for ordinal, (sector_id, members) in enumerate(
            normalized_members_by_sector.items(),
            start=1,
        ):
            histories[sector_id] = tuple(
                member_histories[symbol] for symbol in members
            )
            if ordinal % 16 == 0:
                self._progress_callback()
        self._progress_callback()
        result = build_horizontal_sector_strength_batch(
            decision_time=observed,
            benchmark_symbol=self._benchmark_symbol,
            benchmark_daily=(
                bars.get(self._benchmark_symbol, ())
                if benchmark_cutoff_complete
                else ()
            ),
            members_by_sector=histories,
            membership_revision=membership_revision,
            progress_callback=self._progress_callback,
        )
        self._progress_callback()
            # 发布滞后或未解析日历属于瞬时状态。若把该未解析批次留在全天内存缓存中，
            # 单例生产数据源不重启便永远无法恢复。
        member_history_complete = all(
            member.history_status != "UNEXPLAINED_GAP"
            for members in histories.values()
            for member in members
        )
        if bundle_cutoff_complete and member_history_complete:
            with self._lock:
                self._cache = {cache_key: result}
        return result


__all__ = (
    "QMT_CURRENT_A_SHARE_SECTOR",
    "QMT_GICS3_CATALOG_SOURCE",
    "QMT_GICS_HIERARCHY_CATALOG_SOURCE",
    "QMT_GICS3_COMPOSITE_ADJUSTMENT",
    "QMT_GICS3_COMPOSITE_CALENDAR_GRID_CONTRACT",
    "QMT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT",
    "QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE",
    "QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT",
    "QMT_GICS3_COMPOSITE_MEMBER_LIMIT",
    "QMT_GICS3_COMPOSITE_METHOD",
    "QMT_SECTOR_STRENGTH_ADJUSTMENT",
    "QMT_SECTOR_STRENGTH_PRICE_BASIS_CONTRACT",
    "QMT_SECTOR_STRENGTH_QMT_DIVIDEND_TYPE",
    "QmtSectorStrengthSource",
    "QMT_GICS3_COMPOSITE_PROVIDER",
    "QmtSectorCompositeSource",
    "build_qmt_gics_hierarchy_sector_catalog",
    "build_qmt_gics3_sector_catalog",
    "build_qmt_gics3_sector_catalog_from_local_files",
    "qmt_gics_hierarchy_catalog_revision",
    "qmt_sector_composite_fact_producer_revision",
    "qmt_sector_daily_fact_producer_revision",
    "qmt_trading_session_evidence",
    "qmt_trading_sessions",
)
