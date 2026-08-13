"""原生 QMT 选股读取的崩溃隔离进程边界。

xtquant 客户端含有可能直接终止解释器、且不抛出 Python 异常的原生代码。因此实时
选股工作器把全部 QMT 和结构读取放进持久子进程。本模块管理带认证的本机回环 IPC
传输，并公开与 :mod:`trading_screening` 相同的只读网关协议。

此边界不接受账户对象、交易会话或订单传输。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from multiprocessing.connection import Connection, Listener
import os
from pathlib import Path
from queue import Empty, Queue
import secrets
import subprocess
import sys
from threading import Lock, RLock, Thread
import time
from uuid import uuid4
from zoneinfo import ZoneInfo

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.incremental_scan import BarKey
from chanlun.decision_support.trading_system.screening_structure import (
    SCREENING_STRUCTURE_FREQUENCIES,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (
    calculate_forward_application_source_revision,
    content_addressed_source_revision_from_build,
    is_content_addressed_application_source_revision,
)
from chanlun.decision_support.trading_system.sector_strength import (
    sector_strength_batch_from_evidence_document,
)
from chanlun.decision_support.trading_system.models import (
    SectorAssessment,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.trading_session import (
    build_trading_session_evidence,
    validate_trading_session_evidence,
)
from cl_app.services.trading_screening_gateway import (
    SectorAnalysisExclusion,
    SectorAnalysisFailure,
    SectorAssessmentBatch,
    _KNOWN_SCREENING_INSTRUMENT_TYPES,
    _TRADABLE_SCREENING_INSTRUMENT_TYPES,
    _stock_codes,
)
from cl_app.services.realtime_quotes import (
    AShareRealtimeQuoteBatch,
    normalized_a_share_codes,
    validated_quote_batch,
)


IPC_SCHEMA = "chanlun-trading-screening-native-ipc"
IPC_AUTHKEY_ENV = "CHANLUN_SCREENING_WORKER_AUTHKEY"
_CN = ZoneInfo("Asia/Shanghai")
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_WORKER = Path(__file__).with_name("trading_screening_native_worker.py")
_SECTOR_CACHE_SCHEMA = "chanlun-native-sector-snapshot-cache"
_SECTOR_CACHE_PAYLOAD_SCHEMA = "chanlun-native-sector-snapshot-cache-payload"
_SECTOR_SNAPSHOT_PRODUCER_SCHEMA = "chanlun-native-sector-snapshot-producer"
_SECTOR_SNAPSHOT_WEB_PRODUCERS = (
    "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
    "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py",
    "web/chanlun_chart/cl_app/services/trading_screening_process.py",
)
def _sector_cache_decision_epoch(value: datetime) -> tuple[date, str, int]:
    """把墙上时钟请求映射到因果 A 股 5m 数据周期。

    板块快照计算昂贵，但已完成行情前缀不会每个自然分钟都变化。若要求精确时间戳
    匹配，20:58 写入的缓存到 20:59 就失效，会使后台选股陷入永久板块重建循环。只有
    在不可能出现新已完成 5m 行情的窗口内才能安全复用。工作日假期会有意按交易日
    处理；这可能多算，但不会让快照跨越一根可能新增的行情。
    """

    local = normalize_datetime(value, "sector cache decision time").astimezone(_CN)
    minute = local.hour * 60 + local.minute
    if local.weekday() >= 5:
        return local.date(), "NON_TRADING_WEEKEND", 0
    if minute < 9 * 60 + 30:
        return local.date(), "PREOPEN", 0
    if minute < 11 * 60 + 30:
        return local.date(), "MORNING", (minute - (9 * 60 + 30)) // 5
    if minute < 13 * 60:
        return local.date(), "LUNCH", 0
    if minute < 15 * 60:
        return local.date(), "AFTERNOON", (minute - 13 * 60) // 5
    return local.date(), "POSTCLOSE", 0


def native_sector_snapshot_producer_revision(
    *,
    project_root: Path | str | None = None,
) -> str:
    """返回完整且独立于界面的原生板块生产者身份。

    持久原生快照包含 QMT 目录、合成结构、成分强度和缓存编解码输出，因此其身份覆盖
    ``src`` 下全部运行文件，以及负责组装、传输和认证输出的三个 Web 服务模块。模板、
    JavaScript、CSS、无关 Web 路由和部署脚本无法改变这些事实，因而不会让高成本板块
    回放失效。

    覆盖范围有意大于人工维护的 Python 导入列表：捆绑的 QMT 二进制和配置、新增决策
    助手都会自动纳入；运行字节码和缓存目录不属于源码，予以排除。
    """

    root = _PROJECT_ROOT if project_root is None else Path(project_root).resolve()
    source_root = root / "src"
    required = tuple(root / value for value in _SECTOR_SNAPSHOT_WEB_PRODUCERS)
    if not source_root.is_dir() or any(not value.is_file() for value in required):
        raise RuntimeError("native sector snapshot producer source is incomplete")
    ignored_directories = frozenset(
        {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    )
    paths = {
        value.resolve()
        for value in source_root.rglob("*")
        if value.is_file()
        and not any(part in ignored_directories for part in value.parts)
        and value.suffix.lower() not in {".pyc", ".pyo"}
    }
    paths.update(value.resolve() for value in required)
    manifest = tuple(
        {
            "path": value.relative_to(root).as_posix(),
            "sha256": "sha256:" + hashlib.sha256(value.read_bytes()).hexdigest(),
        }
        for value in sorted(paths, key=lambda item: item.relative_to(root).as_posix())
    )
    return sha256_json(
        {
            "schema": _SECTOR_SNAPSHOT_PRODUCER_SCHEMA,
            "files": manifest,
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
    )


def native_sector_snapshot_cache_revision(
    build_revision: str,
    *,
    project_root: Path | str | None = None,
) -> str | None:
    """只有精确内容寻址的源码树才能启用缓存。"""

    if not isinstance(build_revision, str):
        raise TypeError("build_revision must be a string")
    runtime_revision = build_revision.strip()
    if content_addressed_source_revision_from_build(runtime_revision) is None:
        return None
    return native_sector_snapshot_producer_revision(project_root=project_root)


class NativeScreeningWorkerError(RuntimeError):
    """隔离原生选股边界的异常基类。"""


class NativeScreeningWorkerUnavailable(NativeScreeningWorkerError):
    """子进程已退出，或仍处于重启退避期。"""


class NativeScreeningWorkerTimeout(NativeScreeningWorkerError):
    """原生调用空闲期限前没有收到进度。"""


class NativeScreeningWorkerProtocolError(NativeScreeningWorkerError):
    """已认证子进程返回无效协议消息。"""


class NativeScreeningWorkerRemoteError(NativeScreeningWorkerError):
    """健康子进程内部抛出普通 Python 异常。"""

    def __init__(
        self,
        *,
        method: str,
        remote_error_type: str,
        remote_message: str,
    ) -> None:
        self.method = method
        self.remote_error_type = remote_error_type
        self.remote_message = remote_message
        super().__init__(
            f"native worker {method} failed: {remote_error_type}: {remote_message}"
        )


@dataclass(frozen=True, slots=True)
class NativeWorkerProcessConfig:
    startup_timeout_seconds: float = 45.0
    native_idle_timeout_seconds: float = 210.0
    restart_backoff_seconds: float = 30.0

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0
            for value in (
                self.startup_timeout_seconds,
                self.native_idle_timeout_seconds,
                self.restart_backoff_seconds,
            )
        ):
            raise ValueError("native worker timeouts must be positive numbers")


def _now() -> datetime:
    return datetime.now(_CN)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


@dataclass(frozen=True, slots=True)
class _SectorSnapshotComponents:
    batch: SectorAssessmentBatch
    members: dict[str, tuple[str, ...]]
    changed_bars: tuple[BarKey, ...]
    symbol_names: dict[str, str]


class _SectorSnapshotCacheError(ValueError):
    def __init__(self, reason_code: str, detail: str) -> None:
        self.reason_code = reason_code
        super().__init__(detail)


def _cache_mapping(value: object, field_name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must be a string-keyed mapping")
    return value


def _cache_sequence(value: object, field_name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{field_name} must be a sequence")
    return value


def _cache_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _cache_optional_string(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    return _cache_string(value, field_name)


def _cache_bool(value: object, field_name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _cache_int(value: object, field_name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field_name} must be an integer >= {minimum}")
    return value


def _cache_datetime(value: object, field_name: str) -> datetime:
    raw = _cache_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO datetime") from exc
    return normalize_datetime(parsed, field_name)


def _cache_date(value: object, field_name: str) -> date:
    raw = _cache_string(value, field_name)
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO date") from exc


def _cache_strings(value: object, field_name: str) -> tuple[str, ...]:
    values = _cache_sequence(value, field_name)
    result = tuple(_cache_string(item, f"{field_name}[]") for item in values)
    if len(result) != len(set(result)):
        raise ValueError(f"{field_name} values must be unique")
    return result


def _context_cache_document(value: TimeframeContext | None) -> object:
    if value is None:
        return None
    return {
        "frequency": value.frequency,
        "direction": value.direction,
        "disposition": value.disposition,
        "hard_block": value.hard_block,
        "dominant_point_id": value.dominant_point_id,
        "dominant_point_type": value.dominant_point_type,
        "reason_codes": list(value.reason_codes),
        "observed_at": value.observed_at.isoformat(),
    }


def _context_from_cache(value: object, field_name: str) -> TimeframeContext | None:
    if value is None:
        return None
    row = _cache_mapping(value, field_name)
    direction = _cache_string(row.get("direction"), f"{field_name}.direction")
    disposition = _cache_string(row.get("disposition"), f"{field_name}.disposition")
    point_type = _cache_optional_string(
        row.get("dominant_point_type"), f"{field_name}.dominant_point_type"
    )
    if direction not in {"up", "down", "neutral"}:
        raise ValueError(f"{field_name}.direction is unsupported")
    if disposition not in {"supportive", "neutral", "hostile"}:
        raise ValueError(f"{field_name}.disposition is unsupported")
    if point_type not in {None, "1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}:
        raise ValueError(f"{field_name}.dominant_point_type is unsupported")
    return TimeframeContext(
        frequency=_cache_string(row.get("frequency"), f"{field_name}.frequency"),
        direction=direction,  # type: ignore[arg-type]
        disposition=disposition,  # type: ignore[arg-type]
        hard_block=_cache_bool(row.get("hard_block"), f"{field_name}.hard_block"),
        dominant_point_id=_cache_optional_string(
            row.get("dominant_point_id"), f"{field_name}.dominant_point_id"
        ),
        dominant_point_type=point_type,  # type: ignore[arg-type]
        reason_codes=_cache_strings(
            row.get("reason_codes"), f"{field_name}.reason_codes"
        ),
        observed_at=_cache_datetime(
            row.get("observed_at"), f"{field_name}.observed_at"
        ),
    )


def _assessment_cache_document(value: SectorAssessment) -> dict[str, object]:
    return {
        "sector_id": value.sector_id,
        "sector_name": value.sector_name,
        "eligible": value.eligible,
        "hard_block": value.hard_block,
        "regime": value.regime,
        "rank_components": [list(item) for item in value.rank_components],
        "reason_codes": list(value.reason_codes),
        "thirty_context": _context_cache_document(value.thirty_context),
        "five_context": _context_cache_document(value.five_context),
        "one_context": _context_cache_document(value.one_context),
        "horizontal_strength": (
            None
            if value.horizontal_strength is None
            else str(value.horizontal_strength)
        ),
        "horizontal_rank": value.horizontal_rank,
        "strength_anchor_session": (
            None
            if value.strength_anchor_session is None
            else value.strength_anchor_session.isoformat()
        ),
        "strength_member_count": value.strength_member_count,
        "strength_source_revision": value.strength_source_revision,
        "strength_reason_codes": list(value.strength_reason_codes),
    }


def _rank_components_from_cache(
    value: object,
    field_name: str,
) -> tuple[tuple[str, int], ...]:
    result: list[tuple[str, int]] = []
    for index, item in enumerate(_cache_sequence(value, field_name)):
        pair = _cache_sequence(item, f"{field_name}[{index}]")
        if len(pair) != 2:
            raise ValueError(f"{field_name}[{index}] must have two values")
        result.append(
            (
                _cache_string(pair[0], f"{field_name}[{index}].name"),
                _cache_int(pair[1], f"{field_name}[{index}].value", minimum=-1000000),
            )
        )
    return tuple(result)


def _assessment_from_cache(value: object, field_name: str) -> SectorAssessment:
    row = _cache_mapping(value, field_name)
    regime = _cache_string(row.get("regime"), f"{field_name}.regime")
    if regime not in {"supportive", "neutral", "hostile"}:
        raise ValueError(f"{field_name}.regime is unsupported")
    raw_strength = row.get("horizontal_strength")
    try:
        strength = (
            None
            if raw_strength is None
            else Decimal(
                _cache_string(raw_strength, f"{field_name}.horizontal_strength")
            )
        )
    except ArithmeticError as exc:
        raise ValueError(f"{field_name}.horizontal_strength is invalid") from exc
    raw_rank = row.get("horizontal_rank")
    rank = (
        None
        if raw_rank is None
        else _cache_int(raw_rank, f"{field_name}.horizontal_rank", minimum=1)
    )
    raw_session = row.get("strength_anchor_session")
    anchor_session = (
        None
        if raw_session is None
        else _cache_date(raw_session, f"{field_name}.strength_anchor_session")
    )
    return SectorAssessment(
        sector_id=_cache_string(row.get("sector_id"), f"{field_name}.sector_id"),
        sector_name=_cache_string(row.get("sector_name"), f"{field_name}.sector_name"),
        eligible=_cache_bool(row.get("eligible"), f"{field_name}.eligible"),
        hard_block=_cache_bool(row.get("hard_block"), f"{field_name}.hard_block"),
        regime=regime,  # type: ignore[arg-type]
        rank_components=_rank_components_from_cache(
            row.get("rank_components"), f"{field_name}.rank_components"
        ),
        reason_codes=_cache_strings(
            row.get("reason_codes"), f"{field_name}.reason_codes"
        ),
        thirty_context=_context_from_cache(
            row.get("thirty_context"), f"{field_name}.thirty_context"
        ),
        five_context=_context_from_cache(
            row.get("five_context"), f"{field_name}.five_context"
        ),
        one_context=_context_from_cache(
            row.get("one_context"), f"{field_name}.one_context"
        ),
        horizontal_strength=strength,
        horizontal_rank=rank,
        strength_anchor_session=anchor_session,
        strength_member_count=_cache_int(
            row.get("strength_member_count"),
            f"{field_name}.strength_member_count",
        ),
        strength_source_revision=_cache_optional_string(
            row.get("strength_source_revision"),
            f"{field_name}.strength_source_revision",
        ),
        strength_reason_codes=_cache_strings(
            row.get("strength_reason_codes"),
            f"{field_name}.strength_reason_codes",
        ),
    )


def _failure_cache_document(value: SectorAnalysisFailure) -> dict[str, object]:
    return {
        "sector_id": value.sector_id,
        "code": value.code,
        "error_type": value.error_type,
        "reason": value.reason,
        "detail_code": value.detail_code,
        "catalog_member_count": value.catalog_member_count,
        "universe_member_count": value.universe_member_count,
    }


def _failure_from_cache(value: object, field_name: str) -> SectorAnalysisFailure:
    row = _cache_mapping(value, field_name)

    def optional_count(name: str) -> int | None:
        raw = row.get(name)
        return None if raw is None else _cache_int(raw, f"{field_name}.{name}")

    return SectorAnalysisFailure(
        sector_id=_cache_string(row.get("sector_id"), f"{field_name}.sector_id"),
        code=_cache_string(row.get("code"), f"{field_name}.code"),
        error_type=_cache_string(row.get("error_type"), f"{field_name}.error_type"),
        reason=_cache_string(row.get("reason"), f"{field_name}.reason"),
        detail_code=_cache_optional_string(
            row.get("detail_code"), f"{field_name}.detail_code"
        ),
        catalog_member_count=optional_count("catalog_member_count"),
        universe_member_count=optional_count("universe_member_count"),
    )


def _exclusion_cache_document(
    value: SectorAnalysisExclusion,
) -> dict[str, object]:
    return {
        "sector_id": value.sector_id,
        "code": value.code,
        "reason_code": value.reason_code,
        "reason": value.reason,
        "detail_code": value.detail_code,
        "catalog_member_count": value.catalog_member_count,
        "universe_member_count": value.universe_member_count,
        "required_member_count": value.required_member_count,
    }


def _exclusion_from_cache(
    value: object,
    field_name: str,
) -> SectorAnalysisExclusion:
    row = _cache_mapping(value, field_name)
    return SectorAnalysisExclusion(
        sector_id=_cache_string(row.get("sector_id"), f"{field_name}.sector_id"),
        code=_cache_string(row.get("code"), f"{field_name}.code"),
        reason_code=_cache_string(row.get("reason_code"), f"{field_name}.reason_code"),
        reason=_cache_string(row.get("reason"), f"{field_name}.reason"),
        detail_code=_cache_string(row.get("detail_code"), f"{field_name}.detail_code"),
        catalog_member_count=_cache_int(
            row.get("catalog_member_count"),
            f"{field_name}.catalog_member_count",
        ),
        universe_member_count=_cache_int(
            row.get("universe_member_count"),
            f"{field_name}.universe_member_count",
        ),
        required_member_count=_cache_int(
            row.get("required_member_count"),
            f"{field_name}.required_member_count",
            minimum=1,
        ),
    )


def _batch_cache_document(value: SectorAssessmentBatch) -> dict[str, object]:
    return {
        "assessments": [_assessment_cache_document(item) for item in value.assessments],
        "discovered_count": value.discovered_count,
        "completed_count": value.completed_count,
        "failure_counts": [list(item) for item in value.failure_counts],
        "errors": [_failure_cache_document(item) for item in value.errors],
        "exclusion_counts": [list(item) for item in value.exclusion_counts],
        "exclusions": [_exclusion_cache_document(item) for item in value.exclusions],
        # 这是原生网关携带、独立重算的 QMT 标的目录标识。若缓存往返时丢弃它，
        # 同一构建版本的普通 Web 重启便会悄然用服务端较弱的成员回退哈希替换它，
        # 从而改变覆盖周期。
        "catalog_revision": value.catalog_revision,
        "strength_evidence": (
            None
            if value.strength_evidence is None
            else value.strength_evidence.evidence_document()
        ),
        "strength_evidence_revision": (
            None
            if value.strength_evidence is None
            else value.strength_evidence.evidence_revision
        ),
    }


def _batch_from_cache(value: object) -> SectorAssessmentBatch:
    row = _cache_mapping(value, "payload.snapshot.assessments")
    raw_strength_evidence = row.get("strength_evidence")
    if raw_strength_evidence is None:
        strength_evidence = None
        if row.get("strength_evidence_revision") is not None:
            raise ValueError("sector strength evidence revision has no document")
    else:
        strength_evidence = sector_strength_batch_from_evidence_document(
            raw_strength_evidence
        )
        if row.get("strength_evidence_revision") != strength_evidence.evidence_revision:
            raise ValueError("sector strength evidence revision is inconsistent")
    raw_counts = _cache_sequence(
        row.get("failure_counts"), "payload.snapshot.assessments.failure_counts"
    )
    failure_counts: list[tuple[str, int]] = []
    for index, item in enumerate(raw_counts):
        pair = _cache_sequence(
            item, f"payload.snapshot.assessments.failure_counts[{index}]"
        )
        if len(pair) != 2:
            raise ValueError("sector failure-count rows must have two values")
        failure_counts.append(
            (
                _cache_string(pair[0], "sector failure-count code"),
                _cache_int(pair[1], "sector failure-count value", minimum=1),
            )
        )
    raw_exclusion_counts = _cache_sequence(
        row.get("exclusion_counts"),
        "payload.snapshot.assessments.exclusion_counts",
    )
    exclusion_counts: list[tuple[str, int]] = []
    for index, item in enumerate(raw_exclusion_counts):
        pair = _cache_sequence(
            item,
            f"payload.snapshot.assessments.exclusion_counts[{index}]",
        )
        if len(pair) != 2:
            raise ValueError("sector exclusion-count rows must have two values")
        exclusion_counts.append(
            (
                _cache_string(pair[0], "sector exclusion-count code"),
                _cache_int(pair[1], "sector exclusion-count value", minimum=1),
            )
        )
    return SectorAssessmentBatch(
        assessments=tuple(
            _assessment_from_cache(item, f"assessment[{index}]")
            for index, item in enumerate(
                _cache_sequence(row.get("assessments"), "sector assessments")
            )
        ),
        discovered_count=_cache_int(
            row.get("discovered_count"), "sector discovered_count"
        ),
        completed_count=_cache_int(
            row.get("completed_count"), "sector completed_count"
        ),
        failure_counts=tuple(failure_counts),
        errors=tuple(
            _failure_from_cache(item, f"sector error[{index}]")
            for index, item in enumerate(
                _cache_sequence(row.get("errors"), "sector errors")
            )
        ),
        exclusion_counts=tuple(exclusion_counts),
        exclusions=tuple(
            _exclusion_from_cache(item, f"sector exclusion[{index}]")
            for index, item in enumerate(
                _cache_sequence(row.get("exclusions"), "sector exclusions")
            )
        ),
        catalog_revision=_cache_optional_string(
            row.get("catalog_revision"),
            "payload.snapshot.assessments.catalog_revision",
        ),
        strength_evidence=strength_evidence,
    )


def _bar_cache_document(value: BarKey) -> dict[str, object]:
    return {
        "code": value.code,
        "frequency": value.frequency,
        "closed_at": value.closed_at.isoformat(),
    }


def _bar_from_cache(value: object, field_name: str) -> BarKey:
    row = _cache_mapping(value, field_name)
    return BarKey(
        code=_cache_string(row.get("code"), f"{field_name}.code"),
        frequency=_cache_string(  # type: ignore[arg-type]
            row.get("frequency"), f"{field_name}.frequency"
        ),
        closed_at=_cache_datetime(row.get("closed_at"), f"{field_name}.closed_at"),
    )


class NativeWorkerProcessTransport:
    """带空闲超时和崩溃恢复的持久认证 IPC 客户端。"""

    def __init__(
        self,
        *,
        log_path: Path,
        config: NativeWorkerProcessConfig = NativeWorkerProcessConfig(),
        worker_command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        progress_callback: Callable[[], None] = lambda: None,
        expected_application_source_revision: str | None = None,
    ) -> None:
        if worker_command is not None and (
            isinstance(worker_command, (str, bytes)) or not worker_command
        ):
            raise ValueError("worker_command must be a non-empty argument sequence")
        if not callable(progress_callback):
            raise TypeError("progress_callback must be callable")
        if expected_application_source_revision is None and worker_command is None:
            expected_application_source_revision = (
                calculate_forward_application_source_revision(_PROJECT_ROOT)
            )
        if (
            expected_application_source_revision is not None
            and not is_content_addressed_application_source_revision(
                expected_application_source_revision
            )
        ):
            raise ValueError(
                "expected_application_source_revision must be content-addressed"
            )
        self._log_path = log_path.resolve()
        self._config = config
        self._worker_command = (
            None
            if worker_command is None
            else tuple(str(value) for value in worker_command)
        )
        self._environment = None if environment is None else dict(environment)
        self._progress_callback = progress_callback
        self._expected_application_source_revision = (
            expected_application_source_revision
        )
        self._request_lock = Lock()
        self._state_lock = RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._connection: Connection | None = None
        self._worker_pid: int | None = None
        self._worker_application_source_revision: str | None = None
        self._worker_market_data_probe: dict[str, object] | None = None
        self._started_at: datetime | None = None
        self._request_started_at: datetime | None = None
        self._last_progress_at: datetime | None = None
        self._last_response_at: datetime | None = None
        self._last_method: str | None = None
        self._last_error: str | None = None
        self._last_remote_error: str | None = None
        self._last_failure_monotonic: float | None = None
        self._restart_count = 0
        self._failure_count = 0
        self._in_flight_request_id: str | None = None

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("progress callback must be callable")
        with self._state_lock:
            self._progress_callback = callback

    def _base_command(self) -> tuple[str, ...]:
        if self._worker_command is not None:
            return self._worker_command
        return (sys.executable, str(_DEFAULT_WORKER))

    def _accept_connection(
        self,
        listener: Listener,
        output: Queue[tuple[Connection | None, BaseException | None]],
    ) -> None:
        try:
            output.put((listener.accept(), None))
        except BaseException as exc:  # pragma: no cover - platform shutdown race
            output.put((None, exc))

    def _spawn(self) -> None:
        with self._state_lock:
            process = self._process
            connection = self._connection
            if (
                process is not None
                and process.poll() is None
                and connection is not None
                and not connection.closed
            ):
                return
            last_failure = self._last_failure_monotonic
            if last_failure is not None:
                remaining = self._config.restart_backoff_seconds - (
                    time.monotonic() - last_failure
                )
                if remaining > 0:
                    raise NativeScreeningWorkerUnavailable(
                        f"native worker restart backoff active ({remaining:.1f}s)"
                    )

        authkey = secrets.token_bytes(32)
        listener = Listener(("127.0.0.1", 0), authkey=authkey)
        host, port = listener.address
        accepted: Queue[tuple[Connection | None, BaseException | None]] = Queue(
            maxsize=1
        )
        Thread(
            target=self._accept_connection,
            args=(listener, accepted),
            name="TradingScreeningNativeAccept",
            daemon=True,
        ).start()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        command = (
            *self._base_command(),
            "--host",
            str(host),
            "--port",
            str(port),
        )
        environment = os.environ.copy()
        if self._environment is not None:
            environment.update(self._environment)
        environment[IPC_AUTHKEY_ENV] = authkey.hex()
        log_handle = self._log_path.open("ab", buffering=0)
        try:
            process = subprocess.Popen(
                command,
                cwd=_PROJECT_ROOT,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except BaseException:
            listener.close()
            log_handle.close()
            raise
        finally:
            # Windows 下由 Popen 为子进程复制句柄。
            log_handle.close()

        deadline = time.monotonic() + self._config.startup_timeout_seconds
        connection: Connection | None = None
        failure: BaseException | None = None
        try:
            while time.monotonic() < deadline:
                try:
                    connection, failure = accepted.get(timeout=0.1)
                    break
                except Empty:
                    if process.poll() is not None:
                        failure = RuntimeError(
                            f"worker exited during startup with code {process.returncode}"
                        )
                        break
            if connection is None:
                raise NativeScreeningWorkerUnavailable(
                    f"native worker failed to connect: {failure or 'startup timeout'}"
                )
            while time.monotonic() < deadline and not connection.poll(0.1):
                if process.poll() is not None:
                    raise NativeScreeningWorkerUnavailable(
                        f"native worker exited before handshake ({process.returncode})"
                    )
            if not connection.poll(0):
                raise NativeScreeningWorkerUnavailable(
                    "native worker handshake timeout"
                )
            handshake = connection.recv()
            worker_source_revision = (
                handshake.get("application_source_revision")
                if isinstance(handshake, Mapping)
                else None
            )
            worker_market_data_probe = (
                handshake.get("market_data_probe")
                if isinstance(handshake, Mapping)
                else None
            )
            if not isinstance(handshake, Mapping) or (
                handshake.get("schema") != IPC_SCHEMA
                or handshake.get("type") != "ready"
                or type(handshake.get("pid")) is not int
                or handshake.get("real_account_access") is not False
                or handshake.get("real_order_transport") is not False
                or not isinstance(worker_market_data_probe, Mapping)
                or worker_market_data_probe.get("schema")
                != "chanlun-qmt-market-data-readiness"
                or worker_market_data_probe.get("ready") is not True
                or worker_market_data_probe.get("provider") != "QMT_XTDATA"
                or worker_market_data_probe.get("real_account_access") is not False
                or worker_market_data_probe.get("real_order_transport") is not False
            ):
                raise NativeScreeningWorkerProtocolError(
                    "native worker returned an invalid safety handshake"
                )
            if self._expected_application_source_revision is not None and (
                not is_content_addressed_application_source_revision(
                    worker_source_revision
                )
                or worker_source_revision != self._expected_application_source_revision
            ):
                raise NativeScreeningWorkerProtocolError(
                    "native worker application source revision mismatch"
                )
        except BaseException as exc:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self._stop_process(process)
                self._record_failure(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            listener.close()

        started = _now()
        with self._state_lock:
            self._process = process
            self._connection = connection
            self._worker_pid = int(handshake["pid"])
            self._worker_application_source_revision = (
                None if worker_source_revision is None else str(worker_source_revision)
            )
            self._worker_market_data_probe = dict(worker_market_data_probe)
            self._started_at = started
            self._last_progress_at = started
            self._last_response_at = started
            self._last_error = None
            self._last_remote_error = None
            self._last_failure_monotonic = None
            self._restart_count += 1

    @staticmethod
    def _stop_process(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:  # pragma: no cover - OS failure
                pass

    def _record_failure(self, message: str) -> None:
        with self._state_lock:
            self._last_error = message[:500]
            self._last_failure_monotonic = time.monotonic()
            self._failure_count += 1

    def _discard_worker(self, message: str, *, record_failure: bool = True) -> None:
        with self._state_lock:
            connection = self._connection
            process = self._process
            self._connection = None
            self._process = None
            self._worker_pid = None
            self._worker_application_source_revision = None
            self._worker_market_data_probe = None
            self._in_flight_request_id = None
            self._request_started_at = None
        try:
            if connection is not None:
                connection.close()
        finally:
            self._stop_process(process)
        if record_failure:
            self._record_failure(message)

    def _notify_progress(self) -> None:
        callback = self._progress_callback
        try:
            callback()
        except Exception:
            # 运行遥测失败不得破坏有效的行情响应。
            pass

    def request(self, method: str, **kwargs: object) -> object:
        if not isinstance(method, str) or not method:
            raise ValueError("native worker method is required")
        with self._request_lock:
            return self._request_locked(method, kwargs)

    def request_nowait(self, method: str, **kwargs: object) -> object:
        """仅在当前没有请求时发送；繁忙时立即失败，不进入等待队列。"""

        if not isinstance(method, str) or not method:
            raise ValueError("native worker method is required")
        acquired = self._request_lock.acquire(blocking=False)
        if not acquired:
            raise NativeScreeningWorkerUnavailable(
                "native worker is busy; request was not queued"
            )
        try:
            return self._request_locked(method, kwargs)
        finally:
            self._request_lock.release()

    def _request_locked(
        self,
        method: str,
        kwargs: Mapping[str, object],
    ) -> object:
        """在调用方已经取得单飞锁后执行一次认证请求。"""

        self._spawn()
        request_id = "sha256:" + uuid4().hex + uuid4().hex
        started = _now()
        with self._state_lock:
            process = self._process
            connection = self._connection
            self._in_flight_request_id = request_id
            self._request_started_at = started
            self._last_progress_at = started
            self._last_method = method
        if process is None or connection is None:
            raise NativeScreeningWorkerUnavailable("native worker is unavailable")
        try:
            connection.send(
                {
                    "schema": IPC_SCHEMA,
                    "type": "request",
                    "request_id": request_id,
                    "method": method,
                    "kwargs": dict(kwargs),
                }
            )
            idle_deadline = time.monotonic() + self._config.native_idle_timeout_seconds
            while True:
                if process.poll() is not None:
                    raise NativeScreeningWorkerUnavailable(
                        "native worker exited during request "
                        f"{method} with code {process.returncode}"
                    )
                remaining = idle_deadline - time.monotonic()
                if remaining <= 0:
                    raise NativeScreeningWorkerTimeout(
                        f"native worker made no progress for "
                        f"{self._config.native_idle_timeout_seconds:g}s in {method}"
                    )
                if not connection.poll(min(0.2, remaining)):
                    continue
                response = connection.recv()
                if not isinstance(response, Mapping) or (
                    response.get("schema") != IPC_SCHEMA
                    or response.get("request_id") != request_id
                ):
                    raise NativeScreeningWorkerProtocolError(
                        "native worker response identity is invalid"
                    )
                response_type = response.get("type")
                if response_type == "progress":
                    progressed = _now()
                    with self._state_lock:
                        self._last_progress_at = progressed
                    idle_deadline = (
                        time.monotonic() + self._config.native_idle_timeout_seconds
                    )
                    self._notify_progress()
                    continue
                if response_type == "error":
                    name = str(response.get("error_type") or "RemoteError")
                    message = str(response.get("message") or "")[:400]
                    with self._state_lock:
                        self._last_response_at = _now()
                        self._last_remote_error = f"{name}: {message}"
                    raise NativeScreeningWorkerRemoteError(
                        method=method,
                        remote_error_type=name,
                        remote_message=message,
                    )
                if response_type != "result" or "value" not in response:
                    raise NativeScreeningWorkerProtocolError(
                        "native worker returned an unsupported response"
                    )
                with self._state_lock:
                    self._last_response_at = _now()
                    self._last_remote_error = None
                return response["value"]
        except NativeScreeningWorkerRemoteError:
            raise
        except (EOFError, BrokenPipeError, OSError) as exc:
            message = f"native worker transport failed in {method}: {exc}"
            self._discard_worker(message)
            raise NativeScreeningWorkerUnavailable(message) from exc
        except NativeScreeningWorkerError as exc:
            self._discard_worker(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            with self._state_lock:
                if self._in_flight_request_id == request_id:
                    self._in_flight_request_id = None
                    self._request_started_at = None

    def startup(self) -> None:
        """建立已认证工作进程，但不发出数据请求。

        请求仍保持惰性和崩溃可恢复；不过应用运行时必须先证明隔离原生依赖确实能启动，
        ``/readyz`` 才能报告成功。把握手与行情方法分离，可避免仅为证明进程就绪就推进
        游标或重建板块快照。
        """

        with self._request_lock:
            self._spawn()

    def shutdown(self) -> None:
        acquired = self._request_lock.acquire(timeout=1.0)
        try:
            with self._state_lock:
                connection = self._connection
                request_id = self._in_flight_request_id
            if acquired and connection is not None and request_id is None:
                try:
                    connection.send(
                        {
                            "schema": IPC_SCHEMA,
                            "type": "shutdown",
                            "request_id": "shutdown:" + uuid4().hex,
                        }
                    )
                except (EOFError, BrokenPipeError, OSError):
                    pass
            self._discard_worker("native worker stopped", record_failure=False)
            with self._state_lock:
                self._last_failure_monotonic = None
                self._last_error = None
        finally:
            if acquired:
                self._request_lock.release()

    close = shutdown

    def health_snapshot(self) -> dict[str, object]:
        with self._state_lock:
            process = self._process
            alive = process is not None and process.poll() is None
            backoff_remaining = 0.0
            if self._last_failure_monotonic is not None:
                backoff_remaining = max(
                    0.0,
                    self._config.restart_backoff_seconds
                    - (time.monotonic() - self._last_failure_monotonic),
                )
            revision_match = (
                None
                if self._expected_application_source_revision is None
                or self._worker_application_source_revision is None
                else self._worker_application_source_revision
                == self._expected_application_source_revision
            )
            ready = (
                alive
                and self._connection is not None
                and self._last_error is None
                and revision_match is not False
            )
            reasons: list[str] = []
            if not alive:
                reasons.append("native_screening_worker_not_running")
            if self._last_error is not None:
                reasons.append("native_screening_worker_failed")
            if revision_match is False or (
                self._last_error is not None
                and "source revision mismatch" in self._last_error
            ):
                reasons.append("native_screening_worker_source_revision_mismatch")
            return {
                "schema": "chanlun-trading-screening-native-health",
                "required": True,
                "ready": ready,
                "status": "ready" if ready else "not_ready",
                "isolated_process": True,
                "loopback_authenticated": True,
                "worker_pid": self._worker_pid,
                "worker_alive": alive,
                "expected_application_source_revision": (
                    self._expected_application_source_revision
                ),
                "worker_application_source_revision": (
                    self._worker_application_source_revision
                ),
                "market_data_probe": (
                    None
                    if self._worker_market_data_probe is None
                    else dict(self._worker_market_data_probe)
                ),
                "application_source_revision_match": revision_match,
                "started_at": _iso(self._started_at),
                "in_flight": self._in_flight_request_id is not None,
                "request_started_at": _iso(self._request_started_at),
                "last_method": self._last_method,
                "last_progress_at": _iso(self._last_progress_at),
                "last_response_at": _iso(self._last_response_at),
                "restart_count": self._restart_count,
                "failure_count": self._failure_count,
                "restart_backoff_remaining_seconds": round(backoff_remaining, 3),
                "last_error": self._last_error,
                "last_remote_error": self._last_remote_error,
                "minimum_market_data_frequency": "1m",
                "tick_data_used": False,
                "real_account_access": False,
                "real_order_transport": False,
                "reasons": reasons,
            }


class NativeTradingDataGatewayProcessProxy:
    """由 :class:`NativeWorkerProcessTransport` 支撑的类型化只读网关。"""

    def __init__(
        self,
        *,
        watchlist_provider: Callable[[], object] = lambda: (),
        holdings_provider: Callable[[], object] = lambda: (),
        instrument_type_provider: Callable[[tuple[str, ...]], Mapping[str, str]]
        | None = None,
        transport: NativeWorkerProcessTransport | None = None,
        log_path: Path | None = None,
        process_config: NativeWorkerProcessConfig = NativeWorkerProcessConfig(),
        sector_cache_path: Path | None = None,
        sector_cache_revision: str | None = None,
        worker_environment: Mapping[str, str] | None = None,
        structure_worker_count: int = 1,
        expected_application_source_revision: str | None = None,
    ) -> None:
        if not callable(watchlist_provider) or not callable(holdings_provider):
            raise TypeError("watchlist and holdings providers must be callable")
        if instrument_type_provider is not None and not callable(
            instrument_type_provider
        ):
            raise TypeError("instrument_type_provider must be callable")
        if transport is None and log_path is None:
            raise ValueError("log_path is required when no transport is supplied")
        if (sector_cache_path is None) != (sector_cache_revision is None):
            raise ValueError(
                "sector_cache_path and sector_cache_revision must be supplied together"
            )
        if sector_cache_revision is not None and not sector_cache_revision.strip():
            raise ValueError("sector_cache_revision must be a non-empty string")
        if type(structure_worker_count) is not int or structure_worker_count <= 0:
            raise ValueError("structure_worker_count must be a positive integer")
        if transport is not None and structure_worker_count != 1:
            raise ValueError("custom transport supports exactly one structure worker")
        if transport is None and expected_application_source_revision is None:
            expected_application_source_revision = (
                calculate_forward_application_source_revision(_PROJECT_ROOT)
            )
        if (
            expected_application_source_revision is not None
            and not is_content_addressed_application_source_revision(
                expected_application_source_revision
            )
        ):
            raise ValueError(
                "expected_application_source_revision must be content-addressed"
            )
        self._watchlist_provider = watchlist_provider
        self._holdings_provider = holdings_provider
        self._instrument_type_provider = instrument_type_provider
        self._transport = transport or NativeWorkerProcessTransport(
            log_path=log_path,  # type: ignore[arg-type]
            config=process_config,
            environment=worker_environment,
            expected_application_source_revision=(expected_application_source_revision),
        )
        structure_transports: list[NativeWorkerProcessTransport] = [self._transport]
        if transport is None:
            assert log_path is not None
            structure_transports = []
            for index in range(structure_worker_count):
                worker_log = log_path.with_name(
                    f"{log_path.stem}.structure-{index + 1}{log_path.suffix}"
                )
                structure_transports.append(
                    NativeWorkerProcessTransport(
                        log_path=worker_log,
                        config=process_config,
                        environment=worker_environment,
                        expected_application_source_revision=(
                            expected_application_source_revision
                        ),
                    )
                )
        self._structure_transports = tuple(structure_transports)
        self._cache_lock = RLock()
        self._sector_cache_path = sector_cache_path
        self._sector_cache_revision = sector_cache_revision
        self._sector_cache_state = (
            "disabled" if sector_cache_path is None else "not_checked"
        )
        self._sector_cache_reason: str | None = None
        self._sector_cache_requested_as_of: datetime | None = None
        self._sector_cache_content_sha256: str | None = None
        self._sector_members: dict[str, tuple[str, ...]] | None = None
        self._changed_bars: tuple[BarKey, ...] = ()
        self._emitted_bar_ids: set[tuple[str, str, datetime]] = set()
        self._symbol_names: dict[str, str] = {}
        self._instrument_types: dict[str, str] = {}
        self._trading_session_cache: dict[date, dict[str, object]] = {}
        self._prepared_history_lock = RLock()
        self._prepared_history_as_of: datetime | None = None
        self._prepared_history_by_code: dict[str, tuple[str, ...]] = {}

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        for transport in (self._transport, *self._structure_transports):
            transport.set_progress_callback(callback)

    def startup(self) -> None:
        """预启动主只读工作进程，结构分片保持惰性。

        主传输负责网关健康及轻量目录和日历调用。若在这里启动全部结构分片，即使闭市
        也会消耗全量覆盖级别的内存。分片仍按确定性方式分配，并在首次使用标的时启动。
        """

        self._transport.startup()

    def _structure_transport(self, code: str) -> NativeWorkerProcessTransport:
        """让同一标的固定到同一工作进程，以保留内存分析缓存。"""

        digest = hashlib.sha256(code.encode("ascii", errors="strict")).digest()
        index = int.from_bytes(digest[:8], "big") % len(self._structure_transports)
        return self._structure_transports[index]

    def native_sector_assessments(self, *, as_of: datetime) -> SectorAssessmentBatch:
        observed_at = normalize_datetime(as_of, "as_of")
        cached = self._load_sector_snapshot_cache(observed_at)
        if cached is not None:
            self._install_sector_snapshot(cached)
            return cached.batch

        # 行业快照可能持续数分钟，必须进入结构进程，不能占用为逐笔、日历和轻量
        # 分类保留的控制进程。
        value = self._structure_transports[0].request("sector_snapshot", as_of=as_of)
        components = self._validated_atomic_snapshot(value, observed_at)
        self._install_sector_snapshot(components)
        self._persist_sector_snapshot_cache(components, observed_at)
        return components.batch

    def _validated_atomic_snapshot(
        self,
        value: object,
        as_of: datetime,
    ) -> _SectorSnapshotComponents:
        if not isinstance(value, Mapping) or (
            value.get("schema") != "chanlun-native-sector-snapshot"
        ):
            raise NativeScreeningWorkerProtocolError("invalid atomic sector snapshot")
        if (
            value.get("real_account_access") is not False
            or value.get("real_order_transport") is not False
            or value.get("tick_data_used") is not False
            or value.get("minimum_market_data_frequency") != "1m"
        ):
            raise NativeScreeningWorkerProtocolError(
                "atomic sector snapshot crossed the read-only safety boundary"
            )
        batch = value.get("assessments")
        members = self._validated_members(value.get("members"))
        bars = value.get("changed_bars")
        names = value.get("symbol_names")
        if not isinstance(batch, SectorAssessmentBatch):
            raise NativeScreeningWorkerProtocolError("invalid sector assessment batch")
        if not isinstance(bars, tuple) or any(
            not isinstance(item, BarKey) for item in bars
        ):
            raise NativeScreeningWorkerProtocolError("invalid sector changed bars")
        if not isinstance(names, Mapping) or any(
            not isinstance(code, str) or not isinstance(name, str)
            for code, name in names.items()
        ):
            raise NativeScreeningWorkerProtocolError("invalid sector symbol names")
        components = _SectorSnapshotComponents(
            batch=batch,
            members=members,
            changed_bars=bars,
            symbol_names=dict(names),
        )
        try:
            self._validate_sector_snapshot_causality(components, as_of)
        except ValueError as exc:
            raise NativeScreeningWorkerProtocolError(
                f"atomic sector snapshot violates causality: {exc}"
            ) from exc
        return components

    def _install_sector_snapshot(self, value: _SectorSnapshotComponents) -> None:
        with self._cache_lock:
            self._sector_members = dict(value.members)
            self._changed_bars = tuple(value.changed_bars)
            self._symbol_names = dict(value.symbol_names)

    @staticmethod
    def _validate_sector_snapshot_causality(
        value: _SectorSnapshotComponents,
        as_of: datetime,
    ) -> None:
        batch = value.batch
        assessment_ids = tuple(item.sector_id for item in batch.assessments)
        if assessment_ids != tuple(sorted(assessment_ids)):
            raise ValueError("sector assessments must be sorted")
        if len(assessment_ids) != len(set(assessment_ids)):
            raise ValueError("sector assessments must be unique")
        if len(assessment_ids) != batch.discovered_count:
            raise ValueError("sector assessment count must match discovered_count")
        if (
            len(batch.errors) + len(batch.exclusions)
            != batch.discovered_count - batch.completed_count
        ):
            raise ValueError(
                "sector errors and exclusions must explain every incomplete assessment"
            )
        error_ids = tuple(item.sector_id for item in batch.errors)
        if error_ids != tuple(sorted(error_ids)):
            raise ValueError("sector errors must be sorted")
        exclusion_ids = tuple(item.sector_id for item in batch.exclusions)
        if exclusion_ids != tuple(sorted(exclusion_ids)):
            raise ValueError("sector exclusions must be sorted")
        if (
            set(error_ids) & set(exclusion_ids)
            or not set(error_ids).issubset(assessment_ids)
            or not set(exclusion_ids).issubset(assessment_ids)
        ):
            raise ValueError("sector dispositions do not match assessments")
        if set(assessment_ids) - set(value.members):
            raise ValueError("every sector assessment must retain its member snapshot")

        for assessment in batch.assessments:
            contexts = (
                assessment.thirty_context,
                assessment.five_context,
                assessment.one_context,
            )
            if any(
                context is not None and context.observed_at > as_of
                for context in contexts
            ):
                raise ValueError("sector context is later than the decision time")
            if (
                assessment.strength_anchor_session is not None
                and assessment.strength_anchor_session > as_of.date()
            ):
                raise ValueError(
                    "sector strength anchor is later than the decision date"
                )

        bar_ids = tuple(
            (item.code, item.frequency, item.closed_at) for item in value.changed_bars
        )
        if bar_ids != tuple(
            sorted(bar_ids, key=lambda item: (item[2], item[0], item[1]))
        ):
            raise ValueError("sector changed bars must be sorted")
        if len(bar_ids) != len(set(bar_ids)):
            raise ValueError("sector changed bars must be unique")
        if any(item.closed_at > as_of for item in value.changed_bars):
            raise ValueError("sector changed bar is later than the decision time")
        if any(item.code not in value.members for item in value.changed_bars):
            raise ValueError("sector changed bar has no membership snapshot")

    def _sector_snapshot_cache_document(
        self,
        value: _SectorSnapshotComponents,
        as_of: datetime,
    ) -> dict[str, object]:
        if self._sector_cache_revision is None:
            raise ValueError("sector snapshot cache is disabled")
        snapshot = {
            "schema": "chanlun-native-sector-snapshot",
            "assessments": _batch_cache_document(value.batch),
            "members": {
                key: list(items) for key, items in sorted(value.members.items())
            },
            "changed_bars": [_bar_cache_document(item) for item in value.changed_bars],
            "symbol_names": dict(sorted(value.symbol_names.items())),
            "minimum_market_data_frequency": "1m",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
        payload: dict[str, object] = {
            "schema": _SECTOR_CACHE_PAYLOAD_SCHEMA,
            "source_revision": self._sector_cache_revision,
            "requested_as_of": as_of.isoformat(),
            "captured_at": _now().isoformat(),
            "snapshot": snapshot,
        }
        return {
            "schema": _SECTOR_CACHE_SCHEMA,
            "content_sha256": sha256_json(payload),
            "payload": payload,
        }

    def _components_from_cache_document(
        self,
        document: object,
        as_of: datetime,
    ) -> tuple[_SectorSnapshotComponents, str]:
        outer = _cache_mapping(document, "sector cache document")
        if outer.get("schema") != _SECTOR_CACHE_SCHEMA:
            raise _SectorSnapshotCacheError(
                "CACHE_SCHEMA_MISMATCH", "sector cache schema is unsupported"
            )
        payload = _cache_mapping(outer.get("payload"), "sector cache payload")
        expected_hash = _cache_string(
            outer.get("content_sha256"), "sector cache content_sha256"
        )
        if sha256_json(payload) != expected_hash:
            raise _SectorSnapshotCacheError(
                "CACHE_CONTENT_HASH_MISMATCH",
                "sector cache content hash does not match its payload",
            )
        if payload.get("schema") != _SECTOR_CACHE_PAYLOAD_SCHEMA:
            raise _SectorSnapshotCacheError(
                "CACHE_PAYLOAD_SCHEMA_MISMATCH",
                "sector cache payload schema is unsupported",
            )
        if payload.get("source_revision") != self._sector_cache_revision:
            raise _SectorSnapshotCacheError(
                "CACHE_SOURCE_REVISION_MISMATCH",
                "sector cache was produced by a different source revision",
            )
        cached_as_of = _cache_datetime(
            payload.get("requested_as_of"), "sector cache requested_as_of"
        )
        if _sector_cache_decision_epoch(cached_as_of) != (
            _sector_cache_decision_epoch(as_of)
        ):
            raise _SectorSnapshotCacheError(
                "CACHE_DECISION_TIME_MISMATCH",
                "sector cache belongs to a different causal market-data epoch",
            )
        _cache_datetime(payload.get("captured_at"), "sector cache captured_at")

        snapshot = _cache_mapping(payload.get("snapshot"), "sector cache snapshot")
        if snapshot.get("schema") != "chanlun-native-sector-snapshot":
            raise _SectorSnapshotCacheError(
                "CACHE_ATOMIC_SCHEMA_MISMATCH",
                "cached atomic sector snapshot schema is unsupported",
            )
        if (
            snapshot.get("real_account_access") is not False
            or snapshot.get("real_order_transport") is not False
            or snapshot.get("tick_data_used") is not False
            or snapshot.get("minimum_market_data_frequency") != "1m"
        ):
            raise _SectorSnapshotCacheError(
                "CACHE_SAFETY_BOUNDARY_VIOLATION",
                "cached sector snapshot crossed the read-only safety boundary",
            )
        try:
            members = self._validated_members(snapshot.get("members"))
            raw_names = _cache_mapping(
                snapshot.get("symbol_names"), "cached sector symbol names"
            )
            names = {
                _cache_string(code, "cached sector symbol code"): _cache_string(
                    name, f"cached sector symbol name[{code}]"
                )
                for code, name in raw_names.items()
            }
            components = _SectorSnapshotComponents(
                batch=_batch_from_cache(snapshot.get("assessments")),
                members=members,
                changed_bars=tuple(
                    _bar_from_cache(item, f"cached changed bar[{index}]")
                    for index, item in enumerate(
                        _cache_sequence(
                            snapshot.get("changed_bars"),
                            "cached sector changed bars",
                        )
                    )
                ),
                symbol_names=names,
            )
            self._validate_sector_snapshot_causality(components, as_of)
        except _SectorSnapshotCacheError:
            raise
        except (NativeScreeningWorkerProtocolError, TypeError, ValueError) as exc:
            raise _SectorSnapshotCacheError("CACHE_DOCUMENT_INVALID", str(exc)) from exc
        return components, expected_hash

    def _set_sector_cache_status(
        self,
        *,
        state: str,
        reason: str | None,
        as_of: datetime | None,
        content_sha256: str | None,
    ) -> None:
        with self._cache_lock:
            self._sector_cache_state = state
            self._sector_cache_reason = reason
            self._sector_cache_requested_as_of = as_of
            self._sector_cache_content_sha256 = content_sha256

    def _load_sector_snapshot_cache(
        self,
        as_of: datetime,
    ) -> _SectorSnapshotComponents | None:
        path = self._sector_cache_path
        if path is None:
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            components, content_sha256 = self._components_from_cache_document(
                document, as_of
            )
        except FileNotFoundError:
            self._set_sector_cache_status(
                state="miss",
                reason="CACHE_FILE_MISSING",
                as_of=as_of,
                content_sha256=None,
            )
            return None
        except _SectorSnapshotCacheError as exc:
            self._set_sector_cache_status(
                state="rejected",
                reason=exc.reason_code,
                as_of=as_of,
                content_sha256=None,
            )
            return None
        except (OSError, TypeError, ValueError) as exc:
            self._set_sector_cache_status(
                state="rejected",
                reason=f"CACHE_READ_INVALID:{type(exc).__name__}",
                as_of=as_of,
                content_sha256=None,
            )
            return None
        self._set_sector_cache_status(
            state="hit",
            reason=None,
            as_of=as_of,
            content_sha256=content_sha256,
        )
        return components

    def _persist_sector_snapshot_cache(
        self,
        value: _SectorSnapshotComponents,
        as_of: datetime,
    ) -> None:
        path = self._sector_cache_path
        if path is None:
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            document = self._sector_snapshot_cache_document(value, as_of)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(
                    document,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self._set_sector_cache_status(
                state="write_failed",
                reason=f"CACHE_WRITE_FAILED:{type(exc).__name__}",
                as_of=as_of,
                content_sha256=None,
            )
            return
        self._set_sector_cache_status(
            state="refreshed",
            reason=None,
            as_of=as_of,
            content_sha256=_cache_string(
                document.get("content_sha256"), "sector cache content_sha256"
            ),
        )

    @staticmethod
    def _validated_members(value: object) -> dict[str, tuple[str, ...]]:
        if not isinstance(value, Mapping) or any(
            not isinstance(key, str)
            or isinstance(items, (str, bytes))
            or not isinstance(items, Sequence)
            or any(not isinstance(item, str) for item in items)
            for key, items in value.items()
        ):
            raise NativeScreeningWorkerProtocolError("invalid sector membership result")
        return {str(key): tuple(items) for key, items in value.items()}

    def members(self) -> Mapping[str, tuple[str, ...]]:
        with self._cache_lock:
            if self._sector_members is None:
                raise NativeScreeningWorkerUnavailable(
                    "atomic sector snapshot has not been captured"
                )
            return dict(self._sector_members)

    def restore_authenticated_sector_members(
        self,
        *,
        members: Mapping[str, tuple[str, ...]],
        as_of: datetime,
        catalog_revision: str,
    ) -> None:
        """根据已认证应用快照预置进程本地路由。

        完整类型化板块批次仍由选股服务持有并校验；这里只恢复 ``structure_bundle``
        所需成员路由，不重算市场事实、不推进变化行情游标，也不覆盖磁盘缓存。
        """

        observed_at = normalize_datetime(as_of, "restored sector members as_of")
        if not isinstance(catalog_revision, str) or not catalog_revision:
            raise ValueError("catalog_revision must be a non-empty string")
        validated = self._validated_members(members)
        if any(
            not sector_id or values != tuple(sorted(set(values)))
            for sector_id, values in validated.items()
        ):
            raise NativeScreeningWorkerProtocolError(
                "restored sector membership must be canonical"
            )
        attestation = sha256_json(
            {
                "schema": "chanlun-restored-sector-member-routing",
                "as_of": observed_at.isoformat(),
                "catalog_revision": catalog_revision,
                "members": {
                    key: list(values) for key, values in sorted(validated.items())
                },
            }
        )
        with self._cache_lock:
            self._sector_members = dict(validated)
            self._sector_cache_state = "restored_from_screening_snapshot"
            self._sector_cache_reason = None
            self._sector_cache_requested_as_of = observed_at
            self._sector_cache_content_sha256 = attestation

    def changed_bars(self, since: datetime | None) -> tuple[BarKey, ...]:
        cutoff = (
            None if since is None else normalize_datetime(since, "changed bars cutoff")
        )
        with self._cache_lock:
            changed = tuple(
                item
                for item in self._changed_bars
                if (item.code, item.frequency, item.closed_at)
                not in self._emitted_bar_ids
                and (cutoff is None or item.closed_at > cutoff)
            )
            self._emitted_bar_ids.update(
                (item.code, item.frequency, item.closed_at) for item in changed
            )
        return tuple(
            sorted(
                changed,
                key=lambda item: (item.closed_at, item.code, item.frequency),
            )
        )

    def active_watchlist(self) -> tuple[str, ...]:
        return self.active_watchlist_scope()[0]

    def active_watchlist_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = _stock_codes(self._watchlist_provider())
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(code for code in requested if code not in eligible)

    def holdings(self) -> tuple[str, ...]:
        return self.holdings_scope()[0]

    def holdings_scope(
        self,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        requested = _stock_codes(self._holdings_provider())
        eligible = self.tradable_instrument_codes(requested)
        return eligible, tuple(code for code in requested if code not in eligible)

    def tradable_instrument_codes(
        self,
        codes: tuple[str, ...],
    ) -> tuple[str, ...]:
        """以唯一的精确类型契约筛选可监听股票和 ETF。"""

        normalized = _stock_codes(codes)
        if not normalized:
            return ()
        dispositions = self.screening_instrument_types(normalized)
        return tuple(
            code
            for code in normalized
            if dispositions[code] in _TRADABLE_SCREENING_INSTRUMENT_TYPES
        )

    def screening_instrument_types(
        self,
        codes: tuple[str, ...],
    ) -> Mapping[str, str]:
        """从 Web 进程已恢复的统一证券目录读取精确类型。"""

        normalized = _stock_codes(codes)
        if not normalized:
            return {}
        with self._cache_lock:
            result = {
                code: self._instrument_types[code]
                for code in normalized
                if code in self._instrument_types
            }
        missing = tuple(code for code in normalized if code not in result)
        if not missing:
            return {code: result[code] for code in normalized}
        provider = self._instrument_type_provider
        if provider is None:
            raise RuntimeError("instrument type catalog is unavailable")
        value = provider(missing)
        if (
            not isinstance(value, Mapping)
            or set(value) != set(missing)
            or any(
                type(code) is not str
                or type(kind) is not str
                or kind not in _KNOWN_SCREENING_INSTRUMENT_TYPES
                for code, kind in value.items()
            )
        ):
            raise RuntimeError("instrument type catalog result is invalid")
        validated = {code: str(value[code]) for code in missing}
        result.update(validated)
        stable = {
            code: kind
            for code, kind in validated.items()
            if kind != "unresolved_cn"
        }
        if stable:
            with self._cache_lock:
                self._instrument_types.update(stable)
        return {code: result[code] for code in normalized}

    def tick_probe(self, code: str) -> Mapping[str, object]:
        """用统一实时行情结果生成就绪探测，不保留第二套 IPC 协议。"""

        normalized = _stock_codes((code,))
        if len(normalized) != 1 or normalized[0] != code:
            raise ValueError("tick probe requires an exact normalized A-share code")
        batch = self.realtime_ticks((code,))
        usable = code in batch.ticks()
        return {
            "schema": "chanlun-native-tick-probe",
            "code": code,
            "status": (
                "market_closed"
                if not batch.market_open
                else "ready"
                if usable
                else "empty"
            ),
            "market_open": batch.market_open,
            "usable": usable,
            "tick_data_used": batch.tick_data_used,
            "real_account_access": False,
            "real_order_transport": False,
        }

    def realtime_ticks(
        self,
        codes: tuple[str, ...],
    ) -> AShareRealtimeQuoteBatch:
        """单飞读取认证控制进程；繁忙时立即失败，禁止 Web 请求堆积。"""

        normalized = normalized_a_share_codes(codes)
        value = self._transport.request_nowait("realtime_ticks", codes=normalized)
        try:
            return validated_quote_batch(value, requested_codes=normalized)
        except (TypeError, ValueError) as exc:
            raise NativeScreeningWorkerProtocolError(
                "invalid native realtime quote result"
            ) from exc

    def prepare_local_history(
        self,
        *,
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
        as_of: datetime,
    ) -> Mapping[str, object]:
        """在一次分钟轮次前合并 QMT 补数，并认证可供各结构分片本地读取的范围。"""

        observed_at = normalize_datetime(as_of, "as_of")
        if type(frequency_requests) is not tuple:
            raise TypeError("frequency_requests must be an exact tuple")
        raw_requests = frequency_requests
        if any(
            type(item) is not tuple
            or len(item) != 2
            or type(item[0]) is not str
            or _stock_codes((item[0],)) != (item[0],)
            or type(item[1]) is not tuple
            or not item[1]
            or len(item[1]) != len(set(item[1]))
            or item[1]
            != tuple(
                frequency
                for frequency in SCREENING_STRUCTURE_FREQUENCIES
                if frequency in item[1]
            )
            for item in raw_requests
        ):
            raise ValueError("frequency_requests contains an invalid row")
        if raw_requests != tuple(sorted(raw_requests, key=lambda item: item[0])) or len(
            {item[0] for item in raw_requests}
        ) != len(raw_requests):
            raise ValueError("frequency_requests must be canonical and unique")
        canonical = raw_requests
        if not canonical:
            with self._prepared_history_lock:
                self._prepared_history_as_of = observed_at
                self._prepared_history_by_code = {}
            return {
                "schema": "chanlun-screening-local-history-preparation",
                "as_of": observed_at.isoformat(),
                "prepared_frequencies_by_code": {},
                "batch_download_available": False,
            }
        # 只让一个结构工作进程执行批量补数；QMT 本地库由全部分片共享，控制进程
        # 不参与，实时逐笔仍可服务。
        value = self._structure_transports[0].request(
            "prepare_local_history",
            frequency_requests=canonical,
            as_of=observed_at,
        )
        if not isinstance(value, Mapping) or (
            value.get("schema") != "chanlun-screening-local-history-preparation"
            or value.get("as_of") != observed_at.isoformat()
            or not isinstance(value.get("prepared_frequencies_by_code"), Mapping)
        ):
            raise NativeScreeningWorkerProtocolError(
                "invalid local history preparation result"
            )
        raw = value["prepared_frequencies_by_code"]
        prepared: dict[str, tuple[str, ...]] = {}
        for code, requested_frequencies in canonical:
            frequencies = raw.get(code)
            if (
                type(frequencies) is not tuple
                or len(frequencies) != len(set(frequencies))
                or not set(frequencies).issubset(set(requested_frequencies))
            ):
                raise NativeScreeningWorkerProtocolError(
                    "local history preparation scope is invalid"
                )
            prepared[code] = frequencies
        if set(raw) != set(prepared):
            raise NativeScreeningWorkerProtocolError(
                "local history preparation codes are invalid"
            )
        with self._prepared_history_lock:
            self._prepared_history_as_of = observed_at
            self._prepared_history_by_code = dict(prepared)
        return {
            "schema": "chanlun-screening-local-history-preparation",
            "as_of": observed_at.isoformat(),
            "prepared_frequencies_by_code": dict(prepared),
            "batch_download_available": value.get("batch_download_available"),
        }

    def symbol_name(self, code: str) -> str | None:
        with self._cache_lock:
            cached = self._symbol_names.get(code)
        if cached is not None:
            return cached
        value = self._transport.request("symbol_name", code=code)
        if value is not None and not isinstance(value, str):
            raise NativeScreeningWorkerProtocolError("invalid symbol name result")
        return value

    def trading_session_evidence(
        self,
        *,
        session: date,
        observed_at: datetime,
    ) -> Mapping[str, object]:
        """在原生工作进程内读取并校验 QMT 日历证据。"""

        if isinstance(session, datetime) or not isinstance(session, date):
            raise TypeError("session must be a date")
        observed = normalize_datetime(observed_at, "observed_at")
        with self._cache_lock:
            cached = self._trading_session_cache.get(session)
        if cached is not None:
            return validate_trading_session_evidence(
                cached,
                session=session,
                observed_at=observed,
            )
        # 控制进程可能正在执行逐笔或轻量目录调用。就绪探针不能继续排队，否则 Web
        # 部署健康门可能超时。“繁忙”只表示交易日历来源暂时不可用，绝不表示目标
        # 日期一定是工作日或交易日。
        worker_health = self._transport.health_snapshot()
        if worker_health.get("in_flight") is True:
            return build_trading_session_evidence(
                session=session,
                observed_at=observed,
                query_attempted=False,
                query_succeeded=False,
            )
        value = self._transport.request(
            "trading_session_evidence",
            session=session,
            observed_at=observed,
        )
        if not isinstance(value, Mapping):
            raise NativeScreeningWorkerProtocolError("invalid trading session evidence")
        try:
            validated = validate_trading_session_evidence(
                value,
                session=session,
                observed_at=observed,
            )
        except (TypeError, ValueError) as exc:
            raise NativeScreeningWorkerProtocolError(
                "invalid trading session evidence"
            ) from exc
        if validated["classification"] != "UNRESOLVED":
            with self._cache_lock:
                self._trading_session_cache[session] = validated
        return validated

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
    ) -> SymbolStructureBundle:
        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=None,
        )

    def structure_bundle_with_risk_cutoff(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
    ) -> SymbolStructureBundle:
        """保留当前 1m 精度，同时把月周日证据冻结在更早时点。"""

        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=normalize_datetime(
                risk_evidence_cutoff,
                "risk_evidence_cutoff",
            ),
        )

    def _structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        higher_timeframe_as_of: datetime | None,
    ) -> SymbolStructureBundle:
        with self._cache_lock:
            members = self._sector_members
            sector_members = (
                None if members is None else tuple(members.get(sector.sector_id, ()))
            )
        if sector_members is None:
            raise NativeScreeningWorkerUnavailable(
                "atomic sector snapshot has not been captured"
            )
        value = self._structure_transport(code).request(
            "structure_bundle",
            code=code,
            as_of=as_of,
            sector=sector,
            sector_members=sector_members,
            frequencies=frequencies,
            higher_timeframe_as_of=higher_timeframe_as_of,
            local_history_frequencies=self._prepared_local_frequencies(
                code,
                as_of,
            ),
        )
        if not isinstance(value, SymbolStructureBundle):
            raise NativeScreeningWorkerProtocolError("invalid structure bundle result")
        return value

    def _prepared_local_frequencies(
        self,
        code: str,
        as_of: datetime,
    ) -> tuple[str, ...]:
        observed_at = normalize_datetime(as_of, "as_of")
        with self._prepared_history_lock:
            if self._prepared_history_as_of != observed_at:
                return ()
            return self._prepared_history_by_code.get(code, ())

    def health_snapshot(self) -> dict[str, object]:
        result = self._transport.health_snapshot()
        worker_health = tuple(
            transport.health_snapshot() for transport in self._structure_transports
        )
        running_revisions = {
            value.get("worker_application_source_revision")
            for value in worker_health
            if value.get("worker_alive") is True
            and isinstance(value.get("worker_application_source_revision"), str)
        }
        result["structure_worker_pool"] = {
            "configured_worker_count": len(worker_health),
            "running_worker_count": sum(
                value.get("worker_alive") is True for value in worker_health
            ),
            "ready_worker_count": sum(
                value.get("ready") is True for value in worker_health
            ),
            "in_flight_worker_count": sum(
                value.get("in_flight") is True for value in worker_health
            ),
            "worker_pids": [
                value.get("worker_pid")
                for value in worker_health
                if type(value.get("worker_pid")) is int
            ],
            "application_source_revision_consistent": (
                len(running_revisions) <= 1
                and all(
                    value.get("application_source_revision_match") is not False
                    for value in worker_health
                    if value.get("worker_alive") is True
                )
            ),
            "running_application_source_revisions": sorted(running_revisions),
            "workers": list(worker_health),
        }
        with self._cache_lock:
            result["sector_snapshot_cache"] = {
                "schema": _SECTOR_CACHE_SCHEMA,
                "enabled": self._sector_cache_path is not None,
                "state": self._sector_cache_state,
                "reason": self._sector_cache_reason,
                "source_revision": self._sector_cache_revision,
                "requested_as_of": _iso(self._sector_cache_requested_as_of),
                "content_sha256": self._sector_cache_content_sha256,
            }
        return result

    def close(self) -> None:
        seen: set[int] = set()
        for transport in (self._transport, *self._structure_transports):
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            transport.shutdown()


__all__ = (
    "IPC_SCHEMA",
    "IPC_AUTHKEY_ENV",
    "NativeScreeningWorkerError",
    "NativeScreeningWorkerProtocolError",
    "NativeScreeningWorkerRemoteError",
    "NativeScreeningWorkerTimeout",
    "NativeScreeningWorkerUnavailable",
    "NativeTradingDataGatewayProcessProxy",
    "NativeWorkerProcessConfig",
    "NativeWorkerProcessTransport",
)
