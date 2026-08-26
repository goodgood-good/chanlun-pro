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
import ctypes
import hashlib
import hmac
import json
from multiprocessing.connection import Connection, Listener
import os
from pathlib import Path
from queue import Empty, Queue
import re
import secrets
import shutil
import subprocess
import sys
from threading import Event, Lock, RLock, Thread
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
    CANONICAL_POINT_TYPE_SET,
    SectorAssessment,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.trading_session import (
    build_trading_session_evidence,
    validate_trading_session_evidence,
)
from cl_app.services.trading_screening_gateway import (
    CachedSectorSnapshot,
    SectorAnalysisExclusion,
    SectorAnalysisFailure,
    SectorAssessmentBatch,
    _KNOWN_SCREENING_INSTRUMENT_TYPES,
    _TRADABLE_SCREENING_INSTRUMENT_TYPES,
    _stock_codes,
)
from cl_app.services.realtime_quotes import (
    AShareDisplayQuoteBatch,
    AShareInstrumentSessionStatusBatch,
    AShareRealtimeQuoteBatch,
    normalized_a_share_codes,
    validated_display_quote_batch,
    validated_instrument_session_status_batch,
    validated_quote_batch,
)
from cl_app.services.trading_screening_source_migrations import (
    sector_snapshot_source_migration_allowed,
)


IPC_SCHEMA = "chanlun-trading-screening-native-ipc"
IPC_AUTHKEY_ENV = "CHANLUN_SCREENING_WORKER_AUTHKEY"
_CN = ZoneInfo("Asia/Shanghai")
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_DEFAULT_WORKER = Path(__file__).with_name("trading_screening_native_worker.py")
_SECTOR_CACHE_SCHEMA = "chanlun-native-sector-snapshot-cache"
_SECTOR_CACHE_PAYLOAD_SCHEMA = "chanlun-native-sector-snapshot-cache-payload"
_SECTOR_CACHE_SCOPE_SIDECAR_SCHEMA = (
    "chanlun-native-sector-cache-scope-v2-exact-request"
)
_SECTOR_CACHE_SCOPE_SIDECAR_MAX_BYTES = 256 * 1024
_SECTOR_SNAPSHOT_PRODUCER_SCHEMA = "chanlun-native-sector-snapshot-producer"
_STRUCTURE_WORKER_AFFINITY_CONTRACT_ID = (
    "coverage-sector-minimal-move-balanced-v4"
)
_COVERAGE_AFFINITY_TARGET_NUMERATOR = 11
_COVERAGE_AFFINITY_TARGET_DENOMINATOR = 10
_DISPLAY_QUOTE_LOCK_WAIT_SECONDS = 2.0
# The candidate phase deadline is an admission boundary, not a safe deadline
# for destroying a process that is already rebuilding one symbol.  A newly
# started QMT/Chanlun worker may legitimately need more than the remaining lane
# budget for its first request.  Keep a separate hard ceiling so a hung request
# is still reclaimed, without turning every near-boundary request into another
# cold start on the following minute.
_CANDIDATE_IN_FLIGHT_MINIMUM_SECONDS = 75.0
_SECTOR_SNAPSHOT_WEB_PRODUCERS = (
    "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
    "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py",
    "web/chanlun_chart/cl_app/services/trading_screening_process.py",
)
_RUNTIME_STATE_CACHE_PRODUCER_SCHEMA = (
    "chanlun-screening-runtime-state-producer-v1"
)
_RUNTIME_STATE_CACHE_PRODUCER_FILES = (
    "src/chanlun/decision_support/fingerprints.py",
    "src/chanlun/decision_support/trading_system/runtime_config.py",
    "src/chanlun/decision_support/trading_system/screening_runtime.py",
    "src/chanlun/decision_support/trading_system/screening_structure.py",
    "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
)


def runtime_state_cache_producer_revision(
    *,
    project_root: Path | str | None = None,
) -> str:
    """返回可序列化严格结构运行态的最小内容寻址身份。

    缓存里只保存 ``CL``、严格结构运行态和对应行情前缀，不保存环境分级、通知、页面
    或选股决策。因此这些外围实现变化不应让数千只标的的 5 分钟状态失效。核心结构
    目录、运行态编解码入口或网关容器变化仍会生成全新身份；载入端还会独立校验
    Python、pandas 与 numpy ABI，并在行情前缀不一致时自动完整重建。
    """

    root = _PROJECT_ROOT if project_root is None else Path(project_root).resolve()
    core_root = root / "src" / "chanlun" / "core"
    required = tuple(root / value for value in _RUNTIME_STATE_CACHE_PRODUCER_FILES)
    if not core_root.is_dir() or any(not value.is_file() for value in required):
        raise RuntimeError("runtime state cache producer source is incomplete")
    ignored_directories = frozenset(
        {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    )
    paths = {
        value.resolve()
        for value in core_root.rglob("*.py")
        if value.is_file()
        and not any(part in ignored_directories for part in value.parts)
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
            "schema": _RUNTIME_STATE_CACHE_PRODUCER_SCHEMA,
            "files": manifest,
            "frequency": "5m",
            "serialized_state": "full-and-warmup-suffix",
            "tick_data_used": False,
            "real_account_access": False,
            "real_order_transport": False,
        }
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


class NativeScreeningWorkerDeadlineExceeded(NativeScreeningWorkerTimeout):
    """低优先级请求超过本轮绝对预算，工作分片已被回收。"""

    reason_code = "CANDIDATE_MONITOR_TIME_BUDGET_EXHAUSTED"


class NativePriorityScreeningWorkerDeadlineExceeded(
    NativeScreeningWorkerDeadlineExceeded
):
    """1m 优先读取超过分钟预算；该错误属于实时通道而非候选延期。"""

    reason_code = "PRIORITY_MONITOR_TIME_BUDGET_EXHAUSTED"


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
    # A fresh Windows worker independently imports the strict runtime, hashes
    # the complete decision source tree and proves a read-only QMT RPC before
    # its authenticated handshake.  Production observations under host load
    # exceeded the old 45-second limit, which killed healthy workers and made
    # application startup loop forever.  This remains bounded and applies only
    # to startup; request idle/deadline controls are unchanged.
    startup_timeout_seconds: float = 180.0
    native_idle_timeout_seconds: float = 210.0
    restart_backoff_seconds: float = 30.0
    max_completed_requests_per_process: int = 256
    max_worker_rss_bytes: int = 1536 * 1024 * 1024

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
        if (
            type(self.max_completed_requests_per_process) is not int
            or self.max_completed_requests_per_process <= 0
        ):
            raise ValueError(
                "max_completed_requests_per_process must be a positive integer"
            )
        if type(self.max_worker_rss_bytes) is not int or self.max_worker_rss_bytes <= 0:
            raise ValueError("max_worker_rss_bytes must be a positive integer")


def _now() -> datetime:
    return datetime.now(_CN)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _process_memory_bytes(pid: int | None) -> tuple[int | None, int | None]:
    """读取子进程工作集/RSS 与私有内存，不引入可选运行依赖。"""

    if type(pid) is not int or pid <= 0:
        return None, None
    if os.name == "nt":
        try:
            from ctypes import wintypes

            class _ProcessMemoryCountersEx(ctypes.Structure):
                _fields_ = (
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                    ("PrivateUsage", ctypes.c_size_t),
                )

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            psapi.GetProcessMemoryInfo.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(_ProcessMemoryCountersEx),
                wintypes.DWORD,
            )
            handle = kernel32.OpenProcess(0x1000 | 0x0010, False, pid)
            if not handle:
                return None, None
            try:
                counters = _ProcessMemoryCountersEx()
                counters.cb = ctypes.sizeof(counters)
                if not psapi.GetProcessMemoryInfo(
                    handle,
                    ctypes.byref(counters),
                    counters.cb,
                ):
                    return None, None
                return int(counters.WorkingSetSize), int(counters.PrivateUsage)
            finally:
                kernel32.CloseHandle(handle)
        except (AttributeError, OSError, TypeError, ValueError):
            return None, None
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
    except (OSError, UnicodeError):
        return None, None
    values: dict[str, int] = {}
    for line in status.splitlines():
        name, separator, raw = line.partition(":")
        if not separator or name not in {"VmRSS", "VmSize"}:
            continue
        parts = raw.strip().split()
        if parts and parts[0].isdigit():
            values[name] = int(parts[0]) * 1024
    return values.get("VmRSS"), values.get("VmSize")


_RUNTIME_STATE_CACHE_ROOT_PATTERN = re.compile(r"^web-(\d+)-[0-9a-f]{16}$")
_PERSISTENT_RUNTIME_STATE_CACHE_ROOT_PATTERN = re.compile(
    r"^runtime-[0-9a-f]{24}$"
)
_PERSISTENT_RUNTIME_STATE_CACHE_OWNER_PATTERN = re.compile(
    r"^\.(runtime-[0-9a-f]{24})\.owner-(\d+)-[0-9a-f]{16}$"
)
_RUNTIME_STATE_CACHE_KEY_CONTEXT = (
    b"chanlun-screening-runtime-state-cache/structure-producer-v1"
)


@dataclass(frozen=True, slots=True)
class _RuntimeStateCacheSettings:
    root: Path
    key_hex: str
    identity: str
    scope: str
    delete_on_close: bool


def _runtime_state_cache_settings(
    *,
    parent: Path,
    expected_runtime_state_producer_revision: str,
    persistent_secret: bytes | None,
) -> _RuntimeStateCacheSettings:
    """Build either a Web-lifecycle cache or a structure-producer cache.

    Production supplies a secret derived from the persistent Flask secret.  The
    resulting HMAC key is stable only for the exact runtime-state producer, so
    Web, notification and decision-policy changes can reuse the expensive 5m
    state while any structure/runtime change is isolated into a new directory
    and key.
    """

    resolved_parent = parent.resolve()
    if re.fullmatch(
        r"sha256:[0-9a-f]{64}", expected_runtime_state_producer_revision
    ) is None:
        raise ValueError(
            "runtime state cache requires a content-addressed producer revision"
        )
    if persistent_secret is None:
        root = (
            resolved_parent / f"web-{os.getpid()}-{secrets.token_hex(8)}"
        ).resolve()
        return _RuntimeStateCacheSettings(
            root=root,
            key_hex=secrets.token_hex(32),
            identity=root.name,
            scope="web_lifecycle",
            delete_on_close=True,
        )
    if not isinstance(persistent_secret, bytes) or len(persistent_secret) < 32:
        raise ValueError("runtime state cache persistent secret is too short")
    revision_bytes = expected_runtime_state_producer_revision.encode("ascii")
    revision_digest = hashlib.sha256(revision_bytes).hexdigest()
    root = (resolved_parent / f"runtime-{revision_digest[:24]}").resolve()
    if (
        root.parent != resolved_parent
        or _PERSISTENT_RUNTIME_STATE_CACHE_ROOT_PATTERN.fullmatch(root.name) is None
    ):
        raise ValueError("runtime state cache source root is invalid")
    derived_key = hmac.new(
        persistent_secret,
        _RUNTIME_STATE_CACHE_KEY_CONTEXT + b"\0" + revision_bytes,
        hashlib.sha256,
    ).hexdigest()
    return _RuntimeStateCacheSettings(
        root=root,
        key_hex=derived_key,
        identity=expected_runtime_state_producer_revision,
        scope="runtime_state_producer_revision",
        delete_on_close=False,
    )


def _process_exists(pid: int) -> bool:
    if type(pid) is not int or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        try:
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            )
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER is the documented signal for a PID
                # that no longer exists.  Access denied (or any other error)
                # is inconclusive, so preserve the cache rather than risk
                # deleting another live Web instance's state.
                return ctypes.get_last_error() != 87
            kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            # 无法证明进程已经退出时保留缓存，避免误删另一个活动实例。
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, PermissionError):
        return True
    return True


def _claim_persistent_runtime_state_cache_root(root: Path) -> Path:
    """为持久缓存登记当前进程租约，供并行或滚动启动安全避让。"""

    resolved = root.resolve()
    resolved_parent = resolved.parent
    if _PERSISTENT_RUNTIME_STATE_CACHE_ROOT_PATTERN.fullmatch(resolved.name) is None:
        raise ValueError("persistent runtime state cache root is invalid")
    resolved_parent.mkdir(parents=True, exist_ok=True)
    marker = (
        resolved_parent
        / f".{resolved.name}.owner-{os.getpid()}-{secrets.token_hex(8)}"
    )
    if (
        marker.parent.resolve() != resolved_parent
        or _PERSISTENT_RUNTIME_STATE_CACHE_OWNER_PATTERN.fullmatch(marker.name) is None
    ):
        raise ValueError("persistent runtime state cache owner marker is invalid")
    marker.touch(exist_ok=False)
    return marker


def _release_persistent_runtime_state_cache_root(marker: Path | None) -> None:
    if marker is None:
        return
    match = _PERSISTENT_RUNTIME_STATE_CACHE_OWNER_PATTERN.fullmatch(marker.name)
    if match is None or int(match.group(2)) != os.getpid():
        return
    try:
        resolved_parent = marker.parent.resolve()
        resolved = marker.resolve()
    except OSError:
        return
    if resolved.parent != resolved_parent:
        return
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass


def _cleanup_stale_runtime_state_cache_roots(
    parent: Path,
    *,
    current_root: Path | None = None,
) -> None:
    """清理退出的 Web 缓存和无活动租约的旧持久版本缓存。"""

    resolved_parent = parent.resolve()
    if not resolved_parent.exists():
        return
    current_root_name: str | None = None
    if current_root is not None:
        try:
            resolved_current = current_root.resolve()
        except OSError:
            resolved_current = None
        if (
            resolved_current is not None
            and resolved_current.parent == resolved_parent
            and _PERSISTENT_RUNTIME_STATE_CACHE_ROOT_PATTERN.fullmatch(
                resolved_current.name
            )
            is not None
        ):
            current_root_name = resolved_current.name
    try:
        candidates = tuple(resolved_parent.iterdir())
    except OSError:
        return

    active_persistent_roots: set[str] = set()
    for candidate in candidates:
        match = _PERSISTENT_RUNTIME_STATE_CACHE_OWNER_PATTERN.fullmatch(
            candidate.name
        )
        if match is None:
            continue
        try:
            if not candidate.is_file():
                continue
            owner_pid = int(match.group(2))
            resolved = candidate.resolve()
        except (OSError, ValueError):
            continue
        if resolved.parent != resolved_parent:
            continue
        if _process_exists(owner_pid):
            active_persistent_roots.add(match.group(1))
            continue
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass

    for candidate in candidates:
        match = _RUNTIME_STATE_CACHE_ROOT_PATTERN.fullmatch(candidate.name)
        if match is not None:
            try:
                if not candidate.is_dir():
                    continue
                owner_pid = int(match.group(1))
                resolved = candidate.resolve()
            except (OSError, ValueError):
                continue
            if resolved.parent != resolved_parent or _process_exists(owner_pid):
                continue
            shutil.rmtree(resolved, ignore_errors=True)
            continue

        if (
            _PERSISTENT_RUNTIME_STATE_CACHE_ROOT_PATTERN.fullmatch(candidate.name)
            is None
            or candidate.name == current_root_name
            or candidate.name in active_persistent_roots
        ):
            continue
        try:
            if not candidate.is_dir():
                continue
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.parent != resolved_parent:
            continue

        # Re-read matching leases immediately before deletion to narrow the
        # startup race with a second Web process claiming this revision.
        has_live_owner = False
        try:
            owner_markers = tuple(
                resolved_parent.glob(f".{candidate.name}.owner-*")
            )
        except OSError:
            owner_markers = ()
        for owner_marker in owner_markers:
            owner_match = _PERSISTENT_RUNTIME_STATE_CACHE_OWNER_PATTERN.fullmatch(
                owner_marker.name
            )
            if owner_match is None:
                continue
            try:
                owner_resolved = owner_marker.resolve()
                owner_pid = int(owner_match.group(2))
            except (OSError, ValueError):
                continue
            if (
                owner_resolved.parent == resolved_parent
                and _process_exists(owner_pid)
            ):
                has_live_owner = True
                break
        if has_live_owner:
            continue
        shutil.rmtree(resolved, ignore_errors=True)


@dataclass(frozen=True, slots=True)
class _CoverageSectorAffinityPlan:
    worker_by_sector: dict[str, int]
    default_worker_loads: tuple[int, ...]
    balanced_worker_loads: tuple[int, ...]
    target_max_load: int
    moved_sector_count: int
    moved_symbol_count: int
    sector_count: int
    symbol_count: int

    def audit_document(self) -> dict[str, object]:
        return {
            "schema": "chanlun-coverage-sector-affinity-plan-v1",
            "contract_id": _STRUCTURE_WORKER_AFFINITY_CONTRACT_ID,
            "configured": bool(self.worker_by_sector),
            "worker_count": len(self.balanced_worker_loads),
            "sector_count": self.sector_count,
            "symbol_count": self.symbol_count,
            "target_max_load": self.target_max_load,
            "default_worker_loads": list(self.default_worker_loads),
            "balanced_worker_loads": list(self.balanced_worker_loads),
            "moved_sector_count": self.moved_sector_count,
            "moved_symbol_count": self.moved_symbol_count,
        }


def _structure_worker_affinity_slot(affinity_key: str, worker_count: int) -> int:
    if not isinstance(affinity_key, str) or not affinity_key:
        raise ValueError("structure worker affinity key is required")
    if type(worker_count) is not int or worker_count <= 0:
        raise ValueError("structure worker count must be positive")
    digest = hashlib.sha256(affinity_key.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % worker_count


def _balanced_coverage_sector_affinity(
    members_by_sector: Mapping[str, Sequence[str]],
    *,
    worker_count: int,
) -> _CoverageSectorAffinityPlan:
    """Rebalance only the few sectors responsible for a coverage tail.

    The normal hash assignment remains the preferred location so persistent
    symbol runtime states stay on the worker that already owns them. A sector
    moves only while doing so lowers the maximum shard load, and rebalancing
    stops once every shard is within ten percent of the ideal average. Entire
    sectors move together, preserving the shared sector-frame locality.
    """

    if not isinstance(members_by_sector, Mapping):
        raise TypeError("coverage sector members must be a mapping")
    if type(worker_count) is not int or worker_count <= 0:
        raise ValueError("coverage worker_count must be positive")
    weights: dict[str, int] = {}
    observed_codes: set[str] = set()
    for sector_id, raw_members in sorted(members_by_sector.items()):
        if not isinstance(sector_id, str) or not sector_id:
            raise ValueError("coverage sector id is required")
        if isinstance(raw_members, (str, bytes)) or not isinstance(
            raw_members, Sequence
        ):
            raise TypeError("coverage sector members must be a sequence")
        members = tuple(raw_members)
        if (
            len(members) != len(set(members))
            or any(
                not isinstance(code, str)
                or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                for code in members
            )
        ):
            raise ValueError("coverage sector members are invalid")
        if observed_codes.intersection(members):
            raise ValueError("coverage symbols must belong to one effective sector")
        observed_codes.update(members)
        if members:
            weights[sector_id] = len(members)

    worker_by_sector = {
        sector_id: _structure_worker_affinity_slot(
            f"sector:{sector_id}",
            worker_count,
        )
        for sector_id in weights
    }
    loads = [0] * worker_count
    for sector_id, weight in weights.items():
        loads[worker_by_sector[sector_id]] += weight
    default_loads = tuple(loads)
    total_symbols = sum(loads)
    target_max_load = (
        0
        if total_symbols == 0
        else max(
            max(weights.values(), default=0),
            (
                total_symbols * _COVERAGE_AFFINITY_TARGET_NUMERATOR
                + worker_count * _COVERAGE_AFFINITY_TARGET_DENOMINATOR
                - 1
            )
            // (worker_count * _COVERAGE_AFFINITY_TARGET_DENOMINATOR),
        )
    )
    moved_sectors: set[str] = set()
    while loads and max(loads) > target_max_load:
        source = max(range(worker_count), key=lambda index: (loads[index], -index))
        excess = loads[source] - target_max_load
        current_max = max(loads)
        best_key: tuple[object, ...] | None = None
        best_move: tuple[str, int, list[int]] | None = None
        for sector_id in sorted(weights):
            if sector_id in moved_sectors or worker_by_sector[sector_id] != source:
                continue
            weight = weights[sector_id]
            for destination in sorted(
                (index for index in range(worker_count) if index != source),
                key=lambda index: (loads[index], index),
            ):
                candidate_loads = list(loads)
                candidate_loads[source] -= weight
                candidate_loads[destination] += weight
                candidate_max = max(candidate_loads)
                if candidate_max >= current_max:
                    continue
                key: tuple[object, ...] = (
                    0 if candidate_loads[destination] <= target_max_load else 1,
                    abs(weight - excess),
                    weight,
                    candidate_max,
                    candidate_max - min(candidate_loads),
                    sector_id,
                    destination,
                )
                if best_key is None or key < best_key:
                    best_key = key
                    best_move = (sector_id, destination, candidate_loads)
        if best_move is None:
            break
        sector_id, destination, loads = best_move
        worker_by_sector[sector_id] = destination
        moved_sectors.add(sector_id)

    return _CoverageSectorAffinityPlan(
        worker_by_sector=worker_by_sector,
        default_worker_loads=default_loads,
        balanced_worker_loads=tuple(loads),
        target_max_load=target_max_load,
        moved_sector_count=len(moved_sectors),
        moved_symbol_count=sum(weights[value] for value in moved_sectors),
        sector_count=len(weights),
        symbol_count=total_symbols,
    )


@dataclass(frozen=True, slots=True)
class _SectorSnapshotComponents:
    admitted_codes: tuple[str, ...] | None
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
    if point_type is not None and point_type not in CANONICAL_POINT_TYPE_SET:
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
        "parent_relations": [list(item) for item in value.parent_relations],
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
    parent_relations: list[tuple[str, str]] = []
    for index, item in enumerate(
        _cache_sequence(
            row.get("parent_relations", ()),
            "payload.snapshot.assessments.parent_relations",
        )
    ):
        pair = _cache_sequence(
            item,
            f"payload.snapshot.assessments.parent_relations[{index}]",
        )
        if len(pair) != 2:
            raise ValueError("sector parent-relation rows must have two values")
        parent_relations.append(
            (
                _cache_string(pair[0], "sector child id"),
                _cache_string(pair[1], "sector parent id"),
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
        parent_relations=tuple(parent_relations),
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
        self._worker_runtime_health: dict[str, object] | None = None
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
        self._completed_request_count = 0
        self._total_completed_request_count = 0
        self._recycle_count = 0
        self._last_recycled_at: datetime | None = None
        self._last_recycle_reason: str | None = None
        self._last_worker_rss_bytes: int | None = None
        self._last_worker_private_bytes: int | None = None
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

    def _spawn(self, *, deadline_monotonic: float | None = None) -> None:
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
        # Windows 服务进程可能继承本地代码页；原生工作进程日志统一固定为 UTF-8，
        # 防止中文诊断写成不可检索的乱码。
        environment["PYTHONUTF8"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
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
        if deadline_monotonic is not None:
            deadline = min(deadline, deadline_monotonic)
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
                if (
                    deadline_monotonic is not None
                    and time.monotonic() >= deadline_monotonic
                ):
                    raise NativeScreeningWorkerDeadlineExceeded(
                        "native worker startup exceeded request deadline"
                    )
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
                if not isinstance(exc, NativeScreeningWorkerDeadlineExceeded):
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
            self._worker_runtime_health = None
            self._started_at = started
            self._last_progress_at = started
            self._last_response_at = started
            self._last_error = None
            self._last_remote_error = None
            self._last_failure_monotonic = None
            self._restart_count += 1
            self._completed_request_count = 0
            self._last_worker_rss_bytes = None
            self._last_worker_private_bytes = None

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

    def _record_completed_request_and_recycle_if_needed(self) -> None:
        """在响应交付前记录资源水位，并在安全请求边界释放进程。"""

        with self._state_lock:
            self._completed_request_count += 1
            self._total_completed_request_count += 1
            completed = self._completed_request_count
            pid = self._worker_pid
        rss_bytes, private_bytes = _process_memory_bytes(pid)
        with self._state_lock:
            self._last_worker_rss_bytes = rss_bytes
            self._last_worker_private_bytes = private_bytes
        recycle_reason: str | None = None
        if (
            rss_bytes is not None
            and rss_bytes >= self._config.max_worker_rss_bytes
        ):
            recycle_reason = (
                "worker_rss_limit_reached:"
                f"{rss_bytes}>={self._config.max_worker_rss_bytes}"
            )
        elif completed >= self._config.max_completed_requests_per_process:
            recycle_reason = (
                "worker_request_limit_reached:"
                f"{completed}>={self._config.max_completed_requests_per_process}"
            )
        if recycle_reason is None:
            return
        recycled_at = _now()
        self._discard_worker(recycle_reason, record_failure=False)
        with self._state_lock:
            self._recycle_count += 1
            self._last_recycled_at = recycled_at
            self._last_recycle_reason = recycle_reason

    def request(self, method: str, **kwargs: object) -> object:
        if not isinstance(method, str) or not method:
            raise ValueError("native worker method is required")
        with self._request_lock:
            return self._request_locked(method, kwargs)

    def request_until(
        self,
        method: str,
        *,
        deadline_monotonic: float,
        **kwargs: object,
    ) -> object:
        """在绝对单调时钟截止点前完成请求，超时即回收对应隔离进程。"""

        if not isinstance(method, str) or not method:
            raise ValueError("native worker method is required")
        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
        ):
            raise TypeError("native worker deadline must be numeric")
        remaining = float(deadline_monotonic) - time.monotonic()
        if remaining <= 0 or not self._request_lock.acquire(timeout=remaining):
            raise NativeScreeningWorkerDeadlineExceeded(
                f"native worker request lock exceeded deadline in {method}"
            )
        try:
            return self._request_locked(
                method,
                kwargs,
                deadline_monotonic=float(deadline_monotonic),
            )
        finally:
            self._request_lock.release()

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

    def request_when_available(
        self,
        method: str,
        *,
        max_wait_seconds: float,
        **kwargs: object,
    ) -> object:
        """只在短预算内等待单飞锁；取得锁后沿用正常原生空闲超时。"""

        if not isinstance(method, str) or not method:
            raise ValueError("native worker method is required")
        if (
            isinstance(max_wait_seconds, bool)
            or not isinstance(max_wait_seconds, (int, float))
            or max_wait_seconds <= 0
        ):
            raise ValueError("native worker max wait must be positive")
        if not self._request_lock.acquire(timeout=float(max_wait_seconds)):
            raise NativeScreeningWorkerUnavailable(
                "native worker stayed busy beyond the bounded queue wait"
            )
        try:
            return self._request_locked(method, kwargs)
        finally:
            self._request_lock.release()

    def _request_locked(
        self,
        method: str,
        kwargs: Mapping[str, object],
        *,
        deadline_monotonic: float | None = None,
    ) -> object:
        """在调用方已经取得单飞锁后执行一次认证请求。"""

        if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
            raise NativeScreeningWorkerDeadlineExceeded(
                f"native worker request deadline already elapsed in {method}"
            )
        self._spawn(deadline_monotonic=deadline_monotonic)
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
                if deadline_monotonic is not None:
                    remaining = min(
                        remaining,
                        deadline_monotonic - time.monotonic(),
                    )
                if remaining <= 0:
                    if (
                        deadline_monotonic is not None
                        and time.monotonic() >= deadline_monotonic
                    ):
                        raise NativeScreeningWorkerDeadlineExceeded(
                            f"native worker exceeded request deadline in {method}"
                        )
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
                raw_runtime_health = response.get("runtime_health")
                if raw_runtime_health is not None and (
                    not isinstance(raw_runtime_health, Mapping)
                    or raw_runtime_health.get("schema")
                    != "chanlun-native-screening-runtime-health"
                ):
                    raise NativeScreeningWorkerProtocolError(
                        "native worker returned invalid runtime health"
                    )
                with self._state_lock:
                    self._last_response_at = _now()
                    self._last_remote_error = None
                    if isinstance(raw_runtime_health, Mapping):
                        self._worker_runtime_health = dict(raw_runtime_health)
                value = response["value"]
                self._record_completed_request_and_recycle_if_needed()
                return value
        except NativeScreeningWorkerRemoteError:
            raise
        except NativeScreeningWorkerDeadlineExceeded as exc:
            # 这是调度器主动执行的低频预算边界，不应触发重启退避；下一分钟的 1m
            # 标的必须能够立即拉起一个干净分片。
            self._discard_worker(
                f"{type(exc).__name__}: {exc}",
                record_failure=False,
            )
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
            worker_pid = self._worker_pid
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
            completed_request_count = self._completed_request_count
            total_completed_request_count = self._total_completed_request_count
            recycle_count = self._recycle_count
            last_recycled_at = self._last_recycled_at
            last_recycle_reason = self._last_recycle_reason
            observed_rss = self._last_worker_rss_bytes
            observed_private = self._last_worker_private_bytes
            live_rss, live_private = _process_memory_bytes(worker_pid if alive else None)
            if live_rss is not None:
                observed_rss = live_rss
            if live_private is not None:
                observed_private = live_private
            return {
                "schema": "chanlun-trading-screening-native-health",
                "required": True,
                "ready": ready,
                "status": "ready" if ready else "not_ready",
                "isolated_process": True,
                "loopback_authenticated": True,
                "worker_pid": worker_pid,
                "worker_alive": alive,
                "worker_rss_bytes": observed_rss,
                "worker_private_bytes": observed_private,
                "max_worker_rss_bytes": self._config.max_worker_rss_bytes,
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
                "runtime_health": (
                    None
                    if self._worker_runtime_health is None
                    else dict(self._worker_runtime_health)
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
                "completed_request_count": completed_request_count,
                "total_completed_request_count": total_completed_request_count,
                "max_completed_requests_per_process": (
                    self._config.max_completed_requests_per_process
                ),
                "recycle_count": recycle_count,
                "last_recycled_at": _iso(last_recycled_at),
                "last_recycle_reason": last_recycle_reason,
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
        symbol_name_provider: Callable[
            [tuple[str, ...]], Mapping[str, str | None]
        ]
        | None = None,
        transport: NativeWorkerProcessTransport | None = None,
        log_path: Path | None = None,
        process_config: NativeWorkerProcessConfig = NativeWorkerProcessConfig(),
        sector_cache_path: Path | None = None,
        sector_cache_revision: str | None = None,
        sector_cache_scope_mode: str = "VALIDATION_COHORT",
        sector_cache_scope_limit: int | None = 12,
        sector_cache_admitted_codes: tuple[str, ...] = (),
        worker_environment: Mapping[str, str] | None = None,
        structure_worker_count: int = 1,
        expected_application_source_revision: str | None = None,
        runtime_state_cache_secret: bytes | None = None,
    ) -> None:
        if not callable(watchlist_provider) or not callable(holdings_provider):
            raise TypeError("watchlist and holdings providers must be callable")
        if instrument_type_provider is not None and not callable(
            instrument_type_provider
        ):
            raise TypeError("instrument_type_provider must be callable")
        if symbol_name_provider is not None and not callable(symbol_name_provider):
            raise TypeError("symbol_name_provider must be callable")
        if transport is None and log_path is None:
            raise ValueError("log_path is required when no transport is supplied")
        if (sector_cache_path is None) != (sector_cache_revision is None):
            raise ValueError(
                "sector_cache_path and sector_cache_revision must be supplied together"
            )
        if sector_cache_revision is not None and not sector_cache_revision.strip():
            raise ValueError("sector_cache_revision must be a non-empty string")
        if sector_cache_scope_mode not in {
            "FULL_MARKET",
            "LARGE_SCOPE",
            "VALIDATION_COHORT",
        }:
            raise ValueError("sector_cache_scope_mode is invalid")
        if sector_cache_scope_mode != "FULL_MARKET" and (
            type(sector_cache_scope_limit) is not int
            or sector_cache_scope_limit <= 0
        ):
            raise ValueError("bounded sector cache scope requires a positive limit")
        if (
            not isinstance(sector_cache_admitted_codes, tuple)
            or len(sector_cache_admitted_codes)
            != len(set(sector_cache_admitted_codes))
            or any(
                not isinstance(code, str)
                or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                for code in sector_cache_admitted_codes
            )
        ):
            raise ValueError("sector_cache_admitted_codes must be a unique tuple")
        if (
            sector_cache_scope_mode != "FULL_MARKET"
            and sector_cache_scope_limit is not None
            and len(sector_cache_admitted_codes) > sector_cache_scope_limit
        ):
            raise ValueError("sector_cache_admitted_codes exceed the bounded limit")
        if (
            sector_cache_path is not None
            and sector_cache_scope_mode != "FULL_MARKET"
            and not sector_cache_admitted_codes
        ):
            raise ValueError(
                "bounded sector cache requires exact admitted codes"
            )
        if type(structure_worker_count) is not int or structure_worker_count <= 0:
            raise ValueError("structure_worker_count must be a positive integer")
        if transport is not None and structure_worker_count != 1:
            raise ValueError("custom transport supports exactly one structure worker")
        if runtime_state_cache_secret is not None and (
            not isinstance(runtime_state_cache_secret, bytes)
            or len(runtime_state_cache_secret) < 32
        ):
            raise ValueError("runtime_state_cache_secret must contain at least 32 bytes")
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
        self._symbol_name_provider = symbol_name_provider
        self._transport = transport or NativeWorkerProcessTransport(
            log_path=log_path,  # type: ignore[arg-type]
            config=process_config,
            environment=worker_environment,
            expected_application_source_revision=(expected_application_source_revision),
        )
        self._runtime_state_cache_root: Path | None = None
        self._runtime_state_cache_delete_on_close = False
        self._runtime_state_cache_owner_marker: Path | None = None
        runtime_state_cache_key: str | None = None
        runtime_state_cache_identity: str | None = None
        runtime_state_cache_scope: str | None = None
        if transport is None and structure_worker_count > 1:
            assert log_path is not None
            assert expected_application_source_revision is not None
            cache_parent = (
                log_path.parent / "trading_screening_runtime_state_cache"
            ).resolve()
            runtime_cache = _runtime_state_cache_settings(
                parent=cache_parent,
                expected_runtime_state_producer_revision=(
                    runtime_state_cache_producer_revision()
                ),
                persistent_secret=runtime_state_cache_secret,
            )
            if not runtime_cache.delete_on_close:
                self._runtime_state_cache_owner_marker = (
                    _claim_persistent_runtime_state_cache_root(runtime_cache.root)
                )
            _cleanup_stale_runtime_state_cache_roots(
                cache_parent,
                current_root=runtime_cache.root,
            )
            self._runtime_state_cache_root = runtime_cache.root
            self._runtime_state_cache_delete_on_close = (
                runtime_cache.delete_on_close
            )
            runtime_state_cache_key = runtime_cache.key_hex
            runtime_state_cache_identity = runtime_cache.identity
            runtime_state_cache_scope = runtime_cache.scope
        structure_transports: list[NativeWorkerProcessTransport] = [self._transport]
        if transport is None:
            assert log_path is not None
            structure_transports = []
            for index in range(structure_worker_count):
                worker_log = log_path.with_name(
                    f"{log_path.stem}.structure-{index + 1}{log_path.suffix}"
                )
                cache_role = (
                    "shared"
                    if structure_worker_count == 1
                    else "priority"
                    if index == 0
                    else "candidate"
                )
                structure_environment = {
                    **dict(worker_environment or {}),
                    "CHANLUN_SCREENING_WORKER_CACHE_ROLE": cache_role,
                }
                if (
                    cache_role == "candidate"
                    and self._runtime_state_cache_root is not None
                    and runtime_state_cache_key is not None
                    and runtime_state_cache_identity is not None
                    and runtime_state_cache_scope is not None
                ):
                    structure_environment.update(
                        {
                            "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_DIR": str(
                                self._runtime_state_cache_root
                                / f"structure-{index + 1}"
                            ),
                            "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_KEY": (
                                runtime_state_cache_key
                            ),
                            "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_IDENTITY": (
                                runtime_state_cache_identity
                            ),
                            "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_SCOPE": (
                                runtime_state_cache_scope
                            ),
                        }
                    )
                structure_transports.append(
                    NativeWorkerProcessTransport(
                        log_path=worker_log,
                        config=process_config,
                        environment=structure_environment,
                        expected_application_source_revision=(
                            expected_application_source_revision
                        ),
                    )
                )
        self._structure_transports = tuple(structure_transports)
        self._cache_lock = RLock()
        self._sector_scope_lock = RLock()
        self._sector_snapshot_build_lock = Lock()
        self._sector_scope_epoch = 0
        self._sector_cache_path = sector_cache_path
        self._sector_cache_revision = sector_cache_revision
        self._sector_cache_scope_mode = sector_cache_scope_mode
        self._sector_cache_scope_limit = (
            None
            if sector_cache_scope_mode == "FULL_MARKET"
            else sector_cache_scope_limit
        )
        self._sector_cache_admitted_codes = (
            ()
            if sector_cache_scope_mode == "FULL_MARKET"
            else sector_cache_admitted_codes
        )
        self._sector_cache_state = (
            "disabled" if sector_cache_path is None else "not_checked"
        )
        self._sector_cache_reason: str | None = None
        self._sector_cache_requested_as_of: datetime | None = None
        self._sector_cache_content_sha256: str | None = None
        self._sector_members: dict[str, tuple[str, ...]] | None = None
        self._coverage_sector_affinity_plan = _balanced_coverage_sector_affinity(
            {},
            worker_count=len(self._structure_transports),
        )
        self._changed_bars: tuple[BarKey, ...] = ()
        self._emitted_bar_ids: set[tuple[str, str, datetime]] = set()
        self._symbol_names: dict[str, str] = {}
        self._instrument_types: dict[str, str] = {}
        self._trading_session_cache: dict[date, dict[str, object]] = {}
        self._prepared_history_lock = RLock()
        self._prepared_history_as_of: datetime | None = None
        self._prepared_history_by_code: dict[str, tuple[str, ...]] = {}
        self._sector_snapshot_in_flight = Event()

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        for transport in (self._transport, *self._structure_transports):
            transport.set_progress_callback(callback)

    def configure_sector_cache_restore_scope(
        self,
        *,
        scope_mode: str,
        max_symbols: int,
        admitted_codes: Sequence[str] = (),
    ) -> None:
        """Install Web admission before any sector payload restore is attempted."""

        if scope_mode not in {
            "FULL_MARKET",
            "LARGE_SCOPE",
            "VALIDATION_COHORT",
        }:
            raise ValueError("sector cache scope_mode is invalid")
        if type(max_symbols) is not int or max_symbols <= 0:
            raise ValueError("sector cache max_symbols must be positive")
        admitted = tuple(admitted_codes)
        if (
            len(admitted) != len(set(admitted))
            or any(
                not isinstance(code, str)
                or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                for code in admitted
            )
            or (scope_mode != "FULL_MARKET" and len(admitted) > max_symbols)
        ):
            raise ValueError("sector cache admitted_codes are invalid")
        new_scope_limit = None if scope_mode == "FULL_MARKET" else max_symbols
        new_admitted = () if scope_mode == "FULL_MARKET" else admitted
        with self._sector_scope_lock, self._cache_lock:
            scope_changed = (
                self._sector_cache_scope_mode != scope_mode
                or self._sector_cache_scope_limit != new_scope_limit
                or self._sector_cache_admitted_codes != new_admitted
            )
            self._sector_cache_scope_mode = scope_mode
            self._sector_cache_scope_limit = new_scope_limit
            self._sector_cache_admitted_codes = new_admitted
            if scope_changed:
                self._sector_scope_epoch += 1
                # Routing and presentation facts belong to the old admission
                # identity. They must not survive even a bounded-to-bounded
                # cohort change while a restore call is racing this update.
                self._sector_members = None
                self._coverage_sector_affinity_plan = (
                    _balanced_coverage_sector_affinity(
                        {},
                        worker_count=len(self._structure_transports),
                    )
                )
                self._changed_bars = ()
                self._emitted_bar_ids.clear()
                self._symbol_names.clear()

    def startup(self) -> None:
        """预启动主只读工作进程，结构分片保持惰性。

        主传输负责网关健康及轻量目录和日历调用。若在这里启动全部结构分片，即使闭市
        也会消耗全量覆盖级别的内存。分片仍按确定性方式分配，并在首次使用标的时启动。
        """

        self._transport.startup()

    def configure_coverage_sector_affinity(
        self,
        *,
        members_by_sector: Mapping[str, Sequence[str]],
    ) -> dict[str, object]:
        """Install an exact, minimally moved whole-sector coverage plan."""

        plan = _balanced_coverage_sector_affinity(
            members_by_sector,
            worker_count=len(self._structure_transports),
        )
        with self._cache_lock:
            self._coverage_sector_affinity_plan = plan
        return plan.audit_document()

    def _structure_transports_for_lane(
        self,
        lane: str,
    ) -> tuple[NativeWorkerProcessTransport, ...]:
        """返回盘中控制、精确定位突发和普通候选工作进程集合。"""

        if lane == "priority":
            # 轻量逐笔和停牌探针始终使用保留分片；这些调用发生在结构波次前，
            # 不会与后续精确定位结构计算争抢同一传输锁。
            return (
                self._structure_transports[:1]
                if len(self._structure_transports) > 1
                else self._structure_transports
            )
        if lane == "priority_burst":
            # 精确 1m 区间套拥有本轮最高优先级。服务层保证该波次结束后才会
            # 接纳普通候选，因此可临时借用全部结构分片而不发生跨通道争抢。
            return self._structure_transports
        if lane in {"candidate", "candidate_overflow"}:
            # 只有一个分片时保持兼容；多分片配置把第一个永久从普通候选中保留，
            # 其余分片并行服务正式 5m 候选。精确定位阶段可在候选开始前借用全部分片。
            candidates = (
                self._structure_transports[1:]
                if len(self._structure_transports) > 1
                else self._structure_transports
            )
            # 原子板块快照固定占用第一个候选分片，可能持续数分钟。生产至少有两个
            # 候选分片时，期间把普通 5m/30m 轮换收敛到其余分片，避免亲和哈希落到
            # 长请求的标的连续延期；板块请求结束后立即恢复完整分片和原缓存亲和。
            if self._sector_snapshot_in_flight.is_set() and len(candidates) > 1:
                return candidates[1:]
            return candidates
        if lane == "coverage":
            return self._structure_transports
        raise ValueError("structure worker lane is invalid")

    def _structure_transport(
        self,
        affinity_key: str,
        *,
        lane: str = "coverage",
    ) -> NativeWorkerProcessTransport:
        """让同一通道内的同一亲和组固定到同一工作进程。"""

        transports = self._structure_transports_for_lane(lane)
        # 所有通道都做确定性分片。同一亲和组若在相邻分钟被轮询到不同进程，进程内的
        # 行情、严格 CL 状态和高周期事实缓存都会失效，等价于反复冷启动。
        # 通道本身仍决定可使用的进程集合，因此普通候选不会占用 1m 优先保留分片。
        if lane == "coverage" and affinity_key.startswith("sector:"):
            sector_id = affinity_key.removeprefix("sector:")
            with self._cache_lock:
                configured_index = (
                    self._coverage_sector_affinity_plan.worker_by_sector.get(
                        sector_id
                    )
                )
            if configured_index is not None:
                return transports[configured_index]
        index = _structure_worker_affinity_slot(affinity_key, len(transports))
        return transports[index]

    @staticmethod
    def _structure_affinity_key(
        code: str,
        sector: SectorAssessment,
        *,
        has_sector_members: bool,
    ) -> str:
        """Co-locate shared sector facts without losing symbol stickiness.

        A symbol's classified sector is stable for one captured membership
        epoch, so sector affinity keeps both its CL state and the shared sector
        composite on one worker. Unclassified and proxy instruments retain
        symbol affinity to avoid concentrating unrelated instruments.
        """

        if has_sector_members and sector.sector_id != "unclassified":
            return f"sector:{sector.sector_id}"
        return f"symbol:{code}"

    @staticmethod
    def _lane_structure_affinity_key(
        code: str,
        affinity_key: str,
        *,
        work_lane: str,
    ) -> str:
        """Keep coverage locality while balancing live burst/candidate work.

        Structure workers can each retain the same bounded sector composite, so
        stable symbol entropy prevents one large supportive sector from pinning
        a whole urgent or cadence batch to one process. The suffix is
        deterministic; each code therefore keeps its worker-local strict state
        across later rounds instead of bouncing between shards.
        """

        if work_lane in {
            "priority_burst",
            "candidate",
            "candidate_overflow",
        } and affinity_key.startswith("sector:"):
            return f"{affinity_key}|symbol:{code}"
        return affinity_key

    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
        admitted_codes: tuple[str, ...] | None = None,
    ) -> SectorAssessmentBatch:
        observed_at = normalize_datetime(as_of, "as_of")
        # Serialize native sector builds without holding the admission lock across
        # the potentially multi-minute worker IPC.  Priority cache reads retain a
        # short scope lock and can therefore continue serving 1m reviews.
        with self._sector_snapshot_build_lock:
            with self._sector_scope_lock:
                if self._sector_cache_scope_mode == "FULL_MARKET":
                    if admitted_codes is not None:
                        raise NativeScreeningWorkerProtocolError(
                            "full-market sector snapshot cannot carry admitted codes"
                        )
                    expected_admitted_codes: tuple[str, ...] | None = None
                else:
                    expected_admitted_codes = self._sector_cache_admitted_codes
                    if not expected_admitted_codes:
                        raise NativeScreeningWorkerProtocolError(
                            "bounded sector snapshot requires exact admitted codes"
                        )
                    if admitted_codes != expected_admitted_codes:
                        raise NativeScreeningWorkerProtocolError(
                            "bounded sector snapshot admission differs from configured scope"
                        )
                scope_identity = (
                    self._sector_scope_epoch,
                    self._sector_cache_scope_mode,
                    self._sector_cache_scope_limit,
                    self._sector_cache_admitted_codes,
                )
                cached = self._load_sector_snapshot_cache(observed_at)
                if cached is not None:
                    self._install_sector_snapshot(cached)
                    return cached.batch

                # 行业快照可能持续数分钟。生产多分片配置必须把它放到普通候选分片，第一
                # 个结构进程永久留给实时优先标的；否则盘中显式重建会让 1m/5m 监听整段过期。
                # 单分片测试和嵌入式配置仍自然回退到唯一进程。
                sector_transport = self._structure_transports_for_lane("candidate")[0]
                request_kwargs: dict[str, object] = {"as_of": as_of}
                if expected_admitted_codes is not None:
                    request_kwargs["admitted_codes"] = expected_admitted_codes
                self._sector_snapshot_in_flight.set()
            try:
                value = sector_transport.request("sector_snapshot", **request_kwargs)
            finally:
                self._sector_snapshot_in_flight.clear()

            with self._sector_scope_lock:
                current_scope_identity = (
                    self._sector_scope_epoch,
                    self._sector_cache_scope_mode,
                    self._sector_cache_scope_limit,
                    self._sector_cache_admitted_codes,
                )
                if current_scope_identity != scope_identity:
                    raise NativeScreeningWorkerProtocolError(
                        "sector snapshot scope changed during native rebuild"
                    )
                components = self._validated_atomic_snapshot(
                    value,
                    observed_at,
                    expected_admitted_codes=expected_admitted_codes,
                )
                self._install_sector_snapshot(components)
                self._persist_sector_snapshot_cache(components, observed_at)
                return components.batch

    def _validated_atomic_snapshot(
        self,
        value: object,
        as_of: datetime,
        *,
        expected_admitted_codes: tuple[str, ...] | None,
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
        raw_admitted_codes = value.get("admitted_codes")
        if raw_admitted_codes != expected_admitted_codes:
            raise NativeScreeningWorkerProtocolError(
                "atomic sector snapshot admission identity mismatch"
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
            admitted_codes=expected_admitted_codes,
            batch=batch,
            members=members,
            changed_bars=bars,
            symbol_names=dict(names),
        )
        self._validate_sector_snapshot_scope(
            components,
            expected_admitted_codes=expected_admitted_codes,
        )
        try:
            self._validate_sector_snapshot_causality(components, as_of)
        except ValueError as exc:
            raise NativeScreeningWorkerProtocolError(
                f"atomic sector snapshot violates causality: {exc}"
            ) from exc
        return components

    def _validate_sector_snapshot_scope(
        self,
        value: _SectorSnapshotComponents,
        *,
        expected_admitted_codes: tuple[str, ...] | None,
    ) -> None:
        if value.admitted_codes != expected_admitted_codes:
            raise NativeScreeningWorkerProtocolError(
                "atomic sector snapshot admission identity mismatch"
            )
        if expected_admitted_codes is None:
            return
        subject_codes = set(self._sector_cache_strategy_subject_codes(value))
        limit = self._sector_cache_scope_limit
        if (
            limit is None
            or len(subject_codes) > limit
            or not subject_codes.issubset(expected_admitted_codes)
        ):
            raise NativeScreeningWorkerProtocolError(
                "bounded atomic sector snapshot escaped its exact admitted scope"
            )

    def _install_sector_snapshot(self, value: _SectorSnapshotComponents) -> None:
        with self._cache_lock:
            members_changed = self._sector_members != value.members
            self._sector_members = dict(value.members)
            if members_changed:
                self._coverage_sector_affinity_plan = (
                    _balanced_coverage_sector_affinity(
                        {},
                        worker_count=len(self._structure_transports),
                    )
                )
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
            "admitted_codes": (
                None
                if value.admitted_codes is None
                else list(value.admitted_codes)
            ),
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

    @staticmethod
    def _sector_cache_scope_sidecar_path(path: Path) -> Path:
        return path.with_name(f"{path.name}.scope")

    @staticmethod
    def _sector_cache_strategy_subject_codes(
        value: _SectorSnapshotComponents,
    ) -> tuple[str, ...]:
        candidates = [
            *(code for members in value.members.values() for code in members),
            *value.symbol_names,
            *(bar.code for bar in value.changed_bars),
        ]
        # The cache payload is serialized with sorted mapping keys.  Canonicalize
        # the subject identity independently of the worker's sector insertion
        # order so a cache can validate after the JSON round trip that wrote it.
        return tuple(
            sorted(
                {
                    code
                    for code in candidates
                    if isinstance(code, str)
                    and re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is not None
                }
            )
        )

    def _sector_cache_scope_sidecar_document(
        self,
        value: _SectorSnapshotComponents,
        document: Mapping[str, object],
    ) -> dict[str, object]:
        codes = self._sector_cache_strategy_subject_codes(value)
        configured_limit = self._sector_cache_scope_limit
        configured_codes = set(self._sector_cache_admitted_codes)
        admitted = bool(
            (
                self._sector_cache_scope_mode == "FULL_MARKET"
                and value.admitted_codes is None
            )
            or (
                self._sector_cache_scope_mode != "FULL_MARKET"
                and configured_limit is not None
                and len(codes) <= configured_limit
                and bool(configured_codes)
                and set(codes).issubset(configured_codes)
                and value.admitted_codes == self._sector_cache_admitted_codes
            )
        )
        try:
            payload_stat = self._sector_cache_path.stat()  # type: ignore[union-attr]
        except OSError:
            payload_stat = None
        return {
            "schema": _SECTOR_CACHE_SCOPE_SIDECAR_SCHEMA,
            "scope_mode": (
                self._sector_cache_scope_mode if admitted else "OVERSCOPE"
            ),
            "max_symbols": configured_limit,
            "strategy_subject_codes": list(codes) if admitted else [],
            "strategy_subject_count": len(codes),
            "configured_admitted_codes": (
                list(self._sector_cache_admitted_codes)
                if self._sector_cache_scope_mode != "FULL_MARKET"
                else []
            ),
            "requested_admitted_codes": (
                None
                if value.admitted_codes is None
                else list(value.admitted_codes)
            ),
            "source_revision": self._sector_cache_revision,
            "payload_content_sha256": document.get("content_sha256"),
            "payload_name": (
                None if self._sector_cache_path is None else self._sector_cache_path.name
            ),
            "payload_size_bytes": (
                None if payload_stat is None else payload_stat.st_size
            ),
            "payload_mtime_ns": (
                None if payload_stat is None else payload_stat.st_mtime_ns
            ),
        }

    def _sector_cache_source_revision_allowed(self, value: object) -> bool:
        current = self._sector_cache_revision
        return bool(
            isinstance(current, str)
            and (
                value == current
                or sector_snapshot_source_migration_allowed(
                    cached_source_revision=value,
                    current_source_revision=current,
                )
            )
        )

    def _sector_cache_scope_allows_payload(self, path: Path) -> bool:
        """Reject broad/legacy cache files before reading their payload bytes."""

        sidecar = self._sector_cache_scope_sidecar_path(path)
        try:
            if sidecar.stat().st_size > _SECTOR_CACHE_SCOPE_SIDECAR_MAX_BYTES:
                return False
            document = json.loads(sidecar.read_text(encoding="utf-8"))
            payload_stat = path.stat()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        codes = (
            document.get("strategy_subject_codes")
            if isinstance(document, Mapping)
            else None
        )
        configured_codes = (
            document.get("configured_admitted_codes")
            if isinstance(document, Mapping)
            else None
        )
        limit = self._sector_cache_scope_limit
        common_valid = bool(
            isinstance(document, Mapping)
            and document.get("schema") == _SECTOR_CACHE_SCOPE_SIDECAR_SCHEMA
            and document.get("scope_mode") == self._sector_cache_scope_mode
            and document.get("max_symbols") == limit
            and self._sector_cache_source_revision_allowed(
                document.get("source_revision")
            )
            and document.get("payload_name") == path.name
            and document.get("payload_size_bytes") == payload_stat.st_size
            and document.get("payload_mtime_ns") == payload_stat.st_mtime_ns
            and isinstance(codes, list)
            and all(
                isinstance(code, str)
                and re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is not None
                for code in codes
            )
            and len(codes) == len(set(codes))
            and document.get("strategy_subject_count") == len(codes)
            and configured_codes == list(self._sector_cache_admitted_codes)
        )
        if not common_valid:
            return False
        if self._sector_cache_scope_mode == "FULL_MARKET":
            return bool(
                limit is None
                and configured_codes == []
                and document.get("requested_admitted_codes") is None
            )
        return bool(
            limit is not None
            and len(codes) <= limit
            and bool(self._sector_cache_admitted_codes)
            and set(codes).issubset(self._sector_cache_admitted_codes)
            and document.get("requested_admitted_codes")
            == list(self._sector_cache_admitted_codes)
        )

    def _sector_cache_scope_matches_loaded_payload(
        self,
        path: Path,
        content_sha256: str,
        value: _SectorSnapshotComponents,
    ) -> bool:
        try:
            document = json.loads(
                self._sector_cache_scope_sidecar_path(path).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        expected_admitted_codes = (
            None
            if self._sector_cache_scope_mode == "FULL_MARKET"
            else self._sector_cache_admitted_codes
        )
        sidecar_codes = (
            document.get("strategy_subject_codes")
            if isinstance(document, Mapping)
            else None
        )
        return bool(
            isinstance(document, Mapping)
            and document.get("payload_content_sha256") == content_sha256
            and document.get("scope_mode") == self._sector_cache_scope_mode
            and document.get("configured_admitted_codes")
            == list(self._sector_cache_admitted_codes)
            and document.get("requested_admitted_codes")
            == (
                None
                if expected_admitted_codes is None
                else list(expected_admitted_codes)
            )
            and value.admitted_codes == expected_admitted_codes
            and isinstance(sidecar_codes, list)
            and all(isinstance(code, str) for code in sidecar_codes)
            and len(sidecar_codes) == len(set(sidecar_codes))
            and tuple(sorted(sidecar_codes))
            == self._sector_cache_strategy_subject_codes(value)
        )

    def _persist_sector_cache_scope_sidecar(
        self,
        path: Path,
        value: _SectorSnapshotComponents,
        document: Mapping[str, object],
    ) -> None:
        sidecar = self._sector_cache_scope_sidecar_path(path)
        temporary = sidecar.with_name(f".{sidecar.name}.{uuid4().hex}.tmp")
        proof = self._sector_cache_scope_sidecar_document(value, document)
        try:
            temporary.write_text(
                json.dumps(
                    proof,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, sidecar)
        finally:
            temporary.unlink(missing_ok=True)

    def _components_from_cache_document(
        self,
        document: object,
        as_of: datetime,
        *,
        require_current_epoch: bool = True,
    ) -> tuple[_SectorSnapshotComponents, str, datetime]:
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
        if not self._sector_cache_source_revision_allowed(
            payload.get("source_revision")
        ):
            raise _SectorSnapshotCacheError(
                "CACHE_SOURCE_REVISION_MISMATCH",
                "sector cache was produced by a different source revision",
            )
        cached_as_of = _cache_datetime(
            payload.get("requested_as_of"), "sector cache requested_as_of"
        )
        if cached_as_of > as_of:
            raise _SectorSnapshotCacheError(
                "CACHE_DECISION_TIME_IN_FUTURE",
                "sector cache was captured for a later decision time",
            )
        if require_current_epoch and _sector_cache_decision_epoch(cached_as_of) != (
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
            raw_admitted_codes = snapshot.get("admitted_codes")
            cached_admitted_codes = (
                None
                if raw_admitted_codes is None
                else _cache_strings(
                    raw_admitted_codes,
                    "cached sector admitted_codes",
                )
            )
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
                admitted_codes=cached_admitted_codes,
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
            # 快照内的每项事实都必须在它自己声明的请求时刻已经可见。使用当前较晚
            # 的墙上时钟校验会让被重写为未来行情的旧缓存蒙混过关。
            self._validate_sector_snapshot_causality(components, cached_as_of)
            self._validate_sector_snapshot_scope(
                components,
                expected_admitted_codes=(
                    None
                    if self._sector_cache_scope_mode == "FULL_MARKET"
                    else self._sector_cache_admitted_codes
                ),
            )
        except _SectorSnapshotCacheError:
            raise
        except (NativeScreeningWorkerProtocolError, TypeError, ValueError) as exc:
            raise _SectorSnapshotCacheError("CACHE_DOCUMENT_INVALID", str(exc)) from exc
        return components, expected_hash, cached_as_of

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
        if not self._sector_cache_scope_allows_payload(path):
            self._set_sector_cache_status(
                state="rejected",
                reason="CACHE_SCOPE_PROOF_MISSING_OR_INVALID",
                as_of=as_of,
                content_sha256=None,
            )
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            components, content_sha256, cached_as_of = (
                self._components_from_cache_document(document, as_of)
            )
            payload = document.get("payload")
            cached_source_revision = (
                payload.get("source_revision")
                if isinstance(payload, Mapping)
                else None
            )
            if not self._sector_cache_scope_matches_loaded_payload(
                path,
                content_sha256,
                components,
            ):
                raise _SectorSnapshotCacheError(
                    "CACHE_SCOPE_PAYLOAD_IDENTITY_MISMATCH",
                    "sector cache scope proof belongs to another payload",
                )
            if cached_source_revision != self._sector_cache_revision:
                # The old payload and its scope proof have both passed their
                # original content identities plus one byte-exact reviewed
                # producer transition. Retag that same validated fact tree so
                # future starts no longer need the migration exception.
                self._persist_sector_snapshot_cache(components, cached_as_of)
                return components
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

    def cached_sector_snapshot_for_priority(
        self,
        *,
        as_of: datetime,
    ) -> CachedSectorSnapshot | None:
        with self._sector_scope_lock:
            return self._cached_sector_snapshot_for_priority_locked(as_of=as_of)

    def _cached_sector_snapshot_for_priority_locked(
        self,
        *,
        as_of: datetime,
    ) -> CachedSectorSnapshot | None:
        """只读取最近一次已完成板块快照，绝不调用原生板块计算。

        盘中优先通道需要先服务 1m 买卖点。即便磁盘快照属于较早行情周期，也可用来
        恢复个股到板块的路由；调用方会把这种上下文强制改为买入关闭失败。内容身份、
        来源身份、安全边界和因果时点仍执行与正式缓存完全相同的校验。
        """

        observed_at = normalize_datetime(as_of, "as_of")
        path = self._sector_cache_path
        if path is None:
            return None
        if not self._sector_cache_scope_allows_payload(path):
            self._set_sector_cache_status(
                state="priority_rejected",
                reason="CACHE_SCOPE_PROOF_MISSING_OR_INVALID",
                as_of=observed_at,
                content_sha256=None,
            )
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            components, content_sha256, cached_as_of = (
                self._components_from_cache_document(
                    document,
                    observed_at,
                    require_current_epoch=False,
                )
            )
            if not self._sector_cache_scope_matches_loaded_payload(
                path,
                content_sha256,
                components,
            ):
                raise _SectorSnapshotCacheError(
                    "CACHE_SCOPE_PAYLOAD_IDENTITY_MISMATCH",
                    "sector cache scope proof belongs to another payload",
                )
        except FileNotFoundError:
            self._set_sector_cache_status(
                state="priority_miss",
                reason="CACHE_FILE_MISSING",
                as_of=observed_at,
                content_sha256=None,
            )
            return None
        except _SectorSnapshotCacheError as exc:
            self._set_sector_cache_status(
                state="priority_rejected",
                reason=exc.reason_code,
                as_of=observed_at,
                content_sha256=None,
            )
            return None
        except (OSError, TypeError, ValueError) as exc:
            self._set_sector_cache_status(
                state="priority_rejected",
                reason=f"CACHE_READ_INVALID:{type(exc).__name__}",
                as_of=observed_at,
                content_sha256=None,
            )
            return None

        current_epoch = _sector_cache_decision_epoch(cached_as_of) == (
            _sector_cache_decision_epoch(observed_at)
        )
        self._install_sector_snapshot(components)
        self._set_sector_cache_status(
            state="priority_hit" if current_epoch else "priority_stale_hit",
            reason=None if current_epoch else "CACHE_DECISION_TIME_STALE",
            as_of=cached_as_of,
            content_sha256=content_sha256,
        )
        return CachedSectorSnapshot(
            batch=components.batch,
            members=components.members,
            requested_as_of=cached_as_of,
            current_decision_epoch=current_epoch,
            content_sha256=content_sha256,
        )

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
            self._sector_cache_scope_sidecar_path(path).unlink(missing_ok=True)
            os.replace(temporary, path)
            self._persist_sector_cache_scope_sidecar(path, value, document)
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
        with self._sector_scope_lock, self._cache_lock:
            if self._sector_cache_scope_mode != "FULL_MARKET":
                admitted = frozenset(self._sector_cache_admitted_codes)
                restored_codes = {
                    code for values in validated.values() for code in values
                }
                limit = self._sector_cache_scope_limit
                if (
                    not admitted
                    or limit is None
                    or len(restored_codes) > limit
                    or not restored_codes.issubset(admitted)
                ):
                    raise NativeScreeningWorkerProtocolError(
                        "restored sector membership escaped configured admission"
                    )
            attestation = sha256_json(
                {
                    "schema": "chanlun-restored-sector-member-routing",
                    "as_of": observed_at.isoformat(),
                    "catalog_revision": catalog_revision,
                    "members": {
                        key: list(values)
                        for key, values in sorted(validated.items())
                    },
                }
            )
            members_changed = self._sector_members != validated
            self._sector_members = dict(validated)
            if members_changed:
                self._coverage_sector_affinity_plan = (
                    _balanced_coverage_sector_affinity(
                        {},
                        worker_count=len(self._structure_transports),
                    )
                )
            # A restored routing attestation starts a fresh local fact epoch,
            # even when the configured cohort itself did not change.
            self._changed_bars = ()
            self._emitted_bar_ids.clear()
            self._symbol_names.clear()
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
        batch = self.priority_realtime_ticks((code,))
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

    def current_session_instrument_statuses(
        self,
        codes: tuple[str, ...],
        *,
        session: date,
    ) -> AShareInstrumentSessionStatusBatch:
        """读取认证控制进程中的当日停牌状态，不进入结构计算队列。"""

        normalized = normalized_a_share_codes(codes)
        if type(session) is not date:
            raise TypeError("instrument-status session must be an exact date")
        value = self._transport.request_nowait(
            "current_session_instrument_statuses",
            codes=normalized,
            session=session,
        )
        try:
            return validated_instrument_session_status_batch(
                value,
                requested_codes=normalized,
                session=session,
            )
        except (TypeError, ValueError) as exc:
            raise NativeScreeningWorkerProtocolError(
                "invalid native instrument-status result"
            ) from exc

    def priority_realtime_ticks(
        self,
        codes: tuple[str, ...],
    ) -> AShareRealtimeQuoteBatch:
        """Read the mandatory-lane quote without contending for the UI worker.

        The shared control process serves display quotes and lightweight
        catalog requests with non-queuing semantics.  A mandatory monitor must
        not lose zero-trade evidence merely because that process is busy.  The
        reserved priority structure workers are otherwise idle before the
        minute batch is submitted, so probe them without queuing and fall over
        only when a shard is already occupied.
        """

        normalized = normalized_a_share_codes(codes)
        last_busy: NativeScreeningWorkerUnavailable | None = None
        for transport in self._structure_transports_for_lane("priority"):
            try:
                value = transport.request_nowait(
                    "realtime_ticks",
                    codes=normalized,
                )
            except NativeScreeningWorkerUnavailable as exc:
                last_busy = exc
                continue
            try:
                return validated_quote_batch(value, requested_codes=normalized)
            except (TypeError, ValueError) as exc:
                raise NativeScreeningWorkerProtocolError(
                    "invalid native priority realtime quote result"
                ) from exc
        if last_busy is not None:
            raise last_busy
        raise NativeScreeningWorkerUnavailable(
            "priority quote worker is unavailable"
        )

    def priority_current_session_instrument_statuses(
        self,
        codes: tuple[str, ...],
        *,
        session: date,
    ) -> AShareInstrumentSessionStatusBatch:
        """Read same-session suspension evidence on a reserved priority shard."""

        normalized = normalized_a_share_codes(codes)
        if type(session) is not date:
            raise TypeError("instrument-status session must be an exact date")
        last_busy: NativeScreeningWorkerUnavailable | None = None
        for transport in self._structure_transports_for_lane("priority"):
            try:
                value = transport.request_nowait(
                    "current_session_instrument_statuses",
                    codes=normalized,
                    session=session,
                )
            except NativeScreeningWorkerUnavailable as exc:
                last_busy = exc
                continue
            try:
                return validated_instrument_session_status_batch(
                    value,
                    requested_codes=normalized,
                    session=session,
                )
            except (TypeError, ValueError) as exc:
                raise NativeScreeningWorkerProtocolError(
                    "invalid native priority instrument-status result"
                ) from exc
        if last_busy is not None:
            raise last_busy
        raise NativeScreeningWorkerUnavailable(
            "priority instrument-status worker is unavailable"
        )

    def display_quote_snapshot(
        self,
        codes: tuple[str, ...],
    ) -> AShareDisplayQuoteBatch:
        """读取认证控制进程中的页面展示行情，休市快照不进入交易探针。"""

        normalized = normalized_a_share_codes(codes)
        # 页面可能同时有自选列表和复核列表读取同一条轻量行情通道。给前一个请求一个
        # 很短的完成窗口，避免一次毫秒级重叠就让整批价格变空，同时仍禁止无界排队。
        value = self._transport.request_when_available(
            "display_quote_snapshot",
            max_wait_seconds=_DISPLAY_QUOTE_LOCK_WAIT_SECONDS,
            codes=normalized,
        )
        try:
            return validated_display_quote_batch(
                value,
                requested_codes=normalized,
            )
        except (TypeError, ValueError) as exc:
            raise NativeScreeningWorkerProtocolError(
                "invalid native display quote result"
            ) from exc

    @staticmethod
    def _validated_history_requests(
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
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
        return raw_requests

    def _install_prepared_history_scope(
        self,
        *,
        canonical: tuple[tuple[str, tuple[str, ...]], ...],
        observed_at: datetime,
        prepared: Mapping[str, tuple[str, ...]],
        batch_download_available: object,
        merge: bool = False,
    ) -> Mapping[str, object]:
        with self._prepared_history_lock:
            if merge and self._prepared_history_as_of == observed_at:
                combined = dict(self._prepared_history_by_code)
                for code, frequencies in prepared.items():
                    available = set(combined.get(code, ())).union(frequencies)
                    combined[code] = tuple(
                        frequency
                        for frequency in SCREENING_STRUCTURE_FREQUENCIES
                        if frequency in available
                    )
                self._prepared_history_by_code = combined
            else:
                self._prepared_history_as_of = observed_at
                self._prepared_history_by_code = dict(prepared)
        return {
            "schema": "chanlun-screening-local-history-preparation",
            "as_of": observed_at.isoformat(),
            "prepared_frequencies_by_code": {
                code: tuple(prepared.get(code, ())) for code, _ in canonical
            },
            "batch_download_available": batch_download_available,
        }

    def prepare_local_history(
        self,
        *,
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
        as_of: datetime,
        deadline_monotonic: float | None = None,
        _lane: str = "coverage",
    ) -> Mapping[str, object]:
        """在一次分钟轮次前合并 QMT 补数，并认证可供各结构分片本地读取的范围。"""

        observed_at = normalize_datetime(as_of, "as_of")
        canonical = self._validated_history_requests(frequency_requests)
        if not canonical:
            return self._install_prepared_history_scope(
                canonical=canonical,
                observed_at=observed_at,
                prepared={},
                batch_download_available=False,
            )
        # 只让一个结构工作进程执行批量补数；QMT 本地库由全部分片共享，控制进程
        # 不参与，实时逐笔仍可服务。
        history_transport = self._structure_transports_for_lane(_lane)[0]
        request = history_transport.request
        request_kwargs: dict[str, object] = {
            "frequency_requests": canonical,
            "as_of": observed_at,
        }
        if deadline_monotonic is not None:
            request = history_transport.request_until
            request_kwargs["deadline_monotonic"] = deadline_monotonic
        value = request("prepare_local_history", **request_kwargs)
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
        return self._install_prepared_history_scope(
            canonical=canonical,
            observed_at=observed_at,
            prepared=prepared,
            batch_download_available=value.get("batch_download_available"),
        )

    def prepare_local_history_until(
        self,
        *,
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
        as_of: datetime,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        """按盘中低频预算准备历史；正式方法仍保持同一事实语义。"""

        return self.prepare_local_history(
            frequency_requests=frequency_requests,
            as_of=as_of,
            deadline_monotonic=deadline_monotonic,
        )

    def prepare_priority_local_history(
        self,
        *,
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
        as_of: datetime,
    ) -> Mapping[str, object]:
        """登记可复用的低频本地库，禁止盘中优先通道执行无界批量下载。"""

        observed_at = normalize_datetime(as_of, "as_of")
        canonical = self._validated_history_requests(frequency_requests)
        # 日线在盘中不会形成新完成 K 线。结构入口固定先刷新 5m；QMT 的 30m 也以
        # 5m 为下载基础，因此随后只读本地 30m 就已经包含本轮刚刷新的基础事实。
        # 其余周期仍由有界逐只请求刷新，避免 download_history_data2 卡住数分钟。
        reusable = {
            code: tuple(
                frequency
                for frequency in SCREENING_STRUCTURE_FREQUENCIES
                if frequency in {"d", "30m"}
            )
            for code, _frequencies in canonical
        }
        return self._install_prepared_history_scope(
            canonical=canonical,
            observed_at=observed_at,
            prepared=reusable,
            batch_download_available=False,
            merge=True,
        )

    def prepare_candidate_local_history_until(
        self,
        *,
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
        as_of: datetime,
        deadline_monotonic: float,
    ) -> Mapping[str, object]:
        """普通候选只刷新会变化的基础周期，禁止重复下载占满分钟预算。"""

        if (
            isinstance(deadline_monotonic, bool)
            or not isinstance(deadline_monotonic, (int, float))
            or deadline_monotonic <= time.monotonic()
        ):
            raise NativeScreeningWorkerDeadlineExceeded(
                "candidate history preparation deadline already elapsed"
            )
        observed_at = normalize_datetime(as_of, "as_of")
        canonical = self._validated_history_requests(frequency_requests)
        # 所有结构包都先读取 5m；30m 的 QMT 下载基础同样是 5m，所以在 5m 本轮刷新后
        # 直接读取本地 30m 可避免同一基础周期下载两次。日线盘中不变，也无需逐只刷新。
        reusable = {
            code: tuple(
                frequency
                for frequency in SCREENING_STRUCTURE_FREQUENCIES
                if frequency in {"d", "30m"}
            )
            for code, _frequencies in canonical
        }
        return self._install_prepared_history_scope(
            canonical=canonical,
            observed_at=observed_at,
            prepared=reusable,
            batch_download_available=False,
            merge=True,
        )

    def symbol_name(self, code: str) -> str | None:
        with self._cache_lock:
            cached = self._symbol_names.get(code)
        if cached is not None:
            return cached
        provider = self._symbol_name_provider
        if provider is not None:
            value = provider((code,))
            if (
                not isinstance(value, Mapping)
                or set(value) != {code}
                or value[code] is not None
                and (not isinstance(value[code], str) or not value[code].strip())
            ):
                raise NativeScreeningWorkerProtocolError(
                    "invalid in-process symbol name result"
                )
            normalized = None if value[code] is None else value[code].strip()
            if normalized is not None:
                with self._cache_lock:
                    self._symbol_names[code] = normalized
            return normalized
        value = self._transport.request("symbol_name", code=code)
        if value is not None and not isinstance(value, str):
            raise NativeScreeningWorkerProtocolError("invalid symbol name result")
        if isinstance(value, str) and value:
            with self._cache_lock:
                self._symbol_names[code] = value
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
        deadline_monotonic: float | None = None,
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
            deadline_monotonic=deadline_monotonic,
        )

    def priority_structure_bundle_with_risk_cutoff(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
    ) -> SymbolStructureBundle:
        """在预留分片读取当前 1m 优先结构，不受候选超时回收影响。"""

        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=normalize_datetime(
                risk_evidence_cutoff,
                "risk_evidence_cutoff",
            ),
            work_lane="priority_burst",
        )

    def candidate_structure_bundle_with_risk_cutoff(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
    ) -> SymbolStructureBundle:
        """在非优先分片执行无硬超时的盘中全量结构任务。"""

        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=normalize_datetime(
                risk_evidence_cutoff,
                "risk_evidence_cutoff",
            ),
            work_lane="candidate",
        )

    def priority_structure_bundle_with_risk_cutoff_until(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
        deadline_monotonic: float,
    ) -> SymbolStructureBundle:
        """在绝对分钟预算内读取 1m 优先结构，超时后立即释放下一轮。"""

        try:
            return self._structure_bundle(
                code,
                as_of=as_of,
                sector=sector,
                frequencies=frequencies,
                higher_timeframe_as_of=normalize_datetime(
                    risk_evidence_cutoff,
                    "risk_evidence_cutoff",
                ),
                deadline_monotonic=deadline_monotonic,
                work_lane="priority_burst",
            )
        except NativeScreeningWorkerDeadlineExceeded as exc:
            raise NativePriorityScreeningWorkerDeadlineExceeded(str(exc)) from exc

    def candidate_structure_bundle_with_risk_cutoff_until(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
        deadline_monotonic: float,
    ) -> SymbolStructureBundle:
        """在普通候选分片读取结构；超时只回收候选分片。"""

        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=normalize_datetime(
                risk_evidence_cutoff,
                "risk_evidence_cutoff",
            ),
            deadline_monotonic=deadline_monotonic,
            work_lane="candidate",
        )

    def candidate_overflow_structure_bundle_with_risk_cutoff_until(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
        deadline_monotonic: float,
    ) -> SymbolStructureBundle:
        """兼容旧调用名，但仍把候选固定在独立候选分片。"""

        return self._structure_bundle(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            higher_timeframe_as_of=normalize_datetime(
                risk_evidence_cutoff,
                "risk_evidence_cutoff",
            ),
            deadline_monotonic=deadline_monotonic,
            work_lane="candidate_overflow",
        )

    def structure_bundle_with_risk_cutoff_until(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        risk_evidence_cutoff: datetime,
        deadline_monotonic: float,
    ) -> SymbolStructureBundle:
        """在绝对预算内读取带冻结高周期风险的结构包。"""

        return self.structure_bundle_with_risk_cutoff(
            code,
            as_of=as_of,
            sector=sector,
            frequencies=frequencies,
            risk_evidence_cutoff=risk_evidence_cutoff,
            deadline_monotonic=deadline_monotonic,
        )

    def _structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        frequencies: tuple[str, ...],
        higher_timeframe_as_of: datetime | None,
        deadline_monotonic: float | None = None,
        work_lane: str = "coverage",
    ) -> SymbolStructureBundle:
        with self._cache_lock:
            members = self._sector_members
            sector_members = (
                None if members is None else tuple(members.get(sector.sector_id, ()))
            )
        if sector_members is None:
            if sector.sector_id == "unclassified" and sector.hard_block:
                # 暂无可信板块快照时仍须识别持仓、自选和规则复查标的的结构与卖点。
                # 空成员会让高周期板块证据保持关闭失败，不能放行任何新买入。
                sector_members = ()
            else:
                raise NativeScreeningWorkerUnavailable(
                    "atomic sector snapshot has not been captured"
                )
        affinity_key = self._structure_affinity_key(
            code,
            sector,
            has_sector_members=bool(sector_members),
        )
        transport = self._structure_transport(
            self._lane_structure_affinity_key(
                code,
                affinity_key,
                work_lane=work_lane,
            ),
            lane=work_lane,
        )
        request_deadline_monotonic = deadline_monotonic
        if deadline_monotonic is not None and work_lane in {
            "candidate",
            "candidate_overflow",
        }:
            request_deadline_monotonic = max(
                float(deadline_monotonic),
                time.monotonic() + _CANDIDATE_IN_FLIGHT_MINIMUM_SECONDS,
            )
        request = (
            transport.request
            if request_deadline_monotonic is None
            else transport.request_until
        )
        local_history_frequencies = self._prepared_local_frequencies(code, as_of)
        incremental_refresh_frequencies = tuple(
            frequency
            for frequency in ("5m", "1m")
            if work_lane != "coverage"
            and frequency in frequencies
            and frequency not in local_history_frequencies
        )
        instrument_type = (
            "stock_cn"
            if self._instrument_type_provider is None
            else self.screening_instrument_types((code,))[code]
        )
        if instrument_type not in _TRADABLE_SCREENING_INSTRUMENT_TYPES:
            raise ValueError("instrument is outside the trading screening scope")
        request_kwargs: dict[str, object] = {
            "code": code,
            "as_of": as_of,
            "sector": sector,
            "sector_members": sector_members,
            "instrument_type": instrument_type,
            "frequencies": frequencies,
            "higher_timeframe_as_of": higher_timeframe_as_of,
            "local_history_frequencies": local_history_frequencies,
            "incremental_refresh_frequencies": incremental_refresh_frequencies,
        }
        if request_deadline_monotonic is not None:
            request_kwargs["deadline_monotonic"] = request_deadline_monotonic
        value = request(
            "structure_bundle",
            **request_kwargs,
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
        with self._cache_lock:
            coverage_affinity = (
                self._coverage_sector_affinity_plan.audit_document()
            )
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
            "affinity_contract_id": _STRUCTURE_WORKER_AFFINITY_CONTRACT_ID,
            "coverage_sector_affinity": coverage_affinity,
            "configured_worker_count": len(worker_health),
            "priority_reserved_worker_count": 1 if worker_health else 0,
            "priority_burst_worker_count": len(worker_health),
            "priority_phase_exclusive": True,
            "candidate_worker_count": (
                max(1, len(worker_health) - 1) if worker_health else 0
            ),
            # Kept for health-schema compatibility. Candidate work is never
            # released onto the reserved shard during a live monitor round.
            "candidate_released_worker_count": (
                max(1, len(worker_health) - 1) if worker_health else 0
            ),
            "candidate_disk_runtime_cache_enabled": (
                self._runtime_state_cache_root is not None
            ),
            "lane_isolation_active": len(worker_health) > 1,
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
                "restore_scope_mode": self._sector_cache_scope_mode,
                "restore_scope_limit": self._sector_cache_scope_limit,
                "restore_admitted_code_count": len(
                    self._sector_cache_admitted_codes
                ),
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
        _release_persistent_runtime_state_cache_root(
            self._runtime_state_cache_owner_marker
        )
        self._runtime_state_cache_owner_marker = None
        cache_root = self._runtime_state_cache_root
        if cache_root is None or not self._runtime_state_cache_delete_on_close:
            return
        resolved = cache_root.resolve()
        expected_parent = (
            cache_root.parent.resolve()
        )
        if (
            resolved.parent == expected_parent
            and resolved.name.startswith(f"web-{os.getpid()}-")
        ):
            shutil.rmtree(resolved, ignore_errors=True)


__all__ = (
    "IPC_SCHEMA",
    "IPC_AUTHKEY_ENV",
    "NativeScreeningWorkerError",
    "NativeScreeningWorkerDeadlineExceeded",
    "NativePriorityScreeningWorkerDeadlineExceeded",
    "NativeScreeningWorkerProtocolError",
    "NativeScreeningWorkerRemoteError",
    "NativeScreeningWorkerTimeout",
    "NativeScreeningWorkerUnavailable",
    "NativeTradingDataGatewayProcessProxy",
    "NativeWorkerProcessConfig",
    "NativeWorkerProcessTransport",
)
