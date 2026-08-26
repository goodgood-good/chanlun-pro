"""唯一生产选股引擎使用的原生行情适配器。"""

from __future__ import annotations

from collections import Counter, OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
import hashlib
import hmac
import json
from numbers import Integral
import os
from pathlib import Path
import pickle
import re
import sys
import tempfile
from threading import RLock
from time import perf_counter
from typing import Protocol, cast
import zlib

import pandas as pd
import numpy as np

from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.core.strict_structure.formal_state import current_formal_direction
from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.context import (
    classify_context,
    context_point_max_age,
)
from chanlun.decision_support.trading_system.context_evidence import (
    SamePeriodTechnicalContext,
    build_same_period_technical_context,
)
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeDataUnavailable,
    HigherTimeframeGateBundle,
    HigherTimeframeSessionEvidence,
    unresolved_higher_timeframe_gates,
)
from chanlun.decision_support.trading_system.incremental_scan import BarKey
from chanlun.decision_support.trading_system.lifecycle import (
    current_five_minute_setup_points,
    five_minute_setup_is_executable,
    five_minute_setup_is_in_policy_scope,
    match_one_minute_nesting_witness_for_point,
    structural_point_occurrence_id,
)
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    EntryExecutionBoundary,
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    SectorAssessment,
    StructuralPoint,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.provisional import (
    ProvisionalCandidate,
    extract_current_provisional_candidates,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT,
    derive_qmt_sector_thirty_minute_frame,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
)
from chanlun.decision_support.trading_system.runtime_config import (
    strict_snapshot_price_metadata,
)
from chanlun.decision_support.trading_system.screening_runtime import (
    ScreeningRuntimeState,
    screening_evidence_from_frame,
)
from chanlun.decision_support.trading_system.screening_structure import (
    SCREENING_STRUCTURE_FREQUENCIES,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_CANONICAL_REQUEST_BARS,
    SCREENING_MINIMUM_BARS_BY_FREQUENCY,
    SCREENING_QMT_30M_FALLBACK_REASON_CODE,
    SCREENING_WARMUP_DIFFERENCE_CODES,
    SCREENING_WARMUP_REQUIRED_BARS,
    screening_warmup_tail_signature,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.decision_support.trading_system.sector_strength import (
    SectorStrengthBatch,
    SectorStrengthEvidence,
)
from chanlun.decision_support.trading_system.qmt_sector_ledger import (
    qmt_sector_catalog_revision,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    normalize_qmt_opening_events_for_completed_minutes,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_current_confirmed_points,
    extract_one_minute_segment_difference_points,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupConvergenceEnvelope,
    WarmupPrefixObservation,
    classify_warmup_convergence_envelope,
)
from chanlun.exchange.exchange import convert_stock_kline_frequency
from chanlun.exchange.price_basis import QMT_STRUCTURE_DIVIDEND_TYPE
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_CATALOG_SOURCE,
    QMT_GICS3_COMPOSITE_ADJUSTMENT,
    QMT_GICS3_COMPOSITE_PROVIDER,
    QMT_GICS_HIERARCHY_CATALOG_SOURCE,
    qmt_gics_hierarchy_catalog_revision,
)
from chanlun.tools.log_util import LogUtil
from cl_app.services.realtime_quotes import (
    AShareDisplayQuoteBatch,
    AShareInstrumentSessionStatus,
    AShareInstrumentSessionStatusBatch,
    AShareRealtimeQuoteBatch,
    normalized_a_share_codes,
    quote_from_exchange_tick,
)


_FREQUENCIES = SCREENING_STRUCTURE_FREQUENCIES
CANONICAL_REQUEST_BARS_BY_FREQUENCY = tuple(
    SCREENING_CANONICAL_REQUEST_BARS.items()
)
_SECTOR_FREQUENCIES = ("30m", "5m")
_A_STOCK_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
_FRAME_UNSET = object()
_WARMUP_ENVELOPE_PREFIX_RATIOS = ((1, 2), (2, 3), (5, 6), (1, 1))
_TRADABLE_SCREENING_INSTRUMENT_TYPES = frozenset({"stock_cn", "etf_cn"})
_QMT_DOWNLOAD_BASE_BY_FREQUENCY = {
    "d": "1d",
    "30m": "5m",
    "5m": "5m",
    "1m": "1m",
}
_REALTIME_INCREMENTAL_REFRESH_DAYS = {"5m": 14, "1m": 7}


def _default_qmt_instrument_detail(native_code: str) -> object:
    from xtquant import xtdata

    return xtdata.get_instrument_detail(native_code, iscomplete=False)
_RUNTIME_CACHE_CAPACITY_BY_FREQUENCY = {
    # 一个严格运行状态会保留完整递归结构；旧上限在单进程中可占用数 GiB。
    # LRU 只用于分钟级热点复用，覆盖通道被逐出后会从冻结物理帧确定性重建。
    "1m": 8,
    "5m": 8,
}
# 严格运行态远大于最终分析摘要，不能把数百只待定位标的全部常驻为 Python 对象；
# 但直接丢弃 LRU 尾部又会让轮转监听每次重放约 12,000 根 K 线。1m 内存二级缓存只
# 保存本进程刚生成的压缩字节；候选 5m 的完整轮换状态另由 Web 生命周期密钥认证的
# 本地磁盘层承接工作进程回收。416 个 1m 槽覆盖两个优先分片的热点段差标的；候选
# 进程的 512 MiB 内存层只负责最快的一组热点，容量不足时按稳定代码哈希保留固定
# 子集；生产环境的多个候选分片各自承接稳定亲和子集，避免顺序轮询造成零命中抖动。
_RUNTIME_CACHE_ROLE = os.environ.get(
    "CHANLUN_SCREENING_WORKER_CACHE_ROLE",
    "shared",
).strip().lower()
if _RUNTIME_CACHE_ROLE not in {"shared", "priority", "candidate"}:
    _RUNTIME_CACHE_ROLE = "shared"
_CANDIDATE_CACHE_ROLE = _RUNTIME_CACHE_ROLE == "candidate"
_SERIALIZED_RUNTIME_CACHE_CAPACITY_BY_FREQUENCY = {
    # 候选分片不计算实时 1m 段差；1m 状态留给优先监听分片，避免覆盖扫描占用内存。
    "1m": 0 if _CANDIDATE_CACHE_ROLE else 416,
    # 候选分片可把有界内存层用于轮换 5m 池；优先/共享分片保持较小，避免挤出 1m
    # 热点或触发 RSS 回收。完整候选轮回由下方认证磁盘层承接。
    "5m": 256 if _CANDIDATE_CACHE_ROLE else 32,
}
_SERIALIZED_RUNTIME_CACHE_MAX_BYTES_BY_FREQUENCY = {
    "1m": 0 if _CANDIDATE_CACHE_ROLE else 896 * 1024 * 1024,
    "5m": (512 if _CANDIDATE_CACHE_ROLE else 96) * 1024 * 1024,
}
_DISK_RUNTIME_CACHE_SCHEMA = "chanlun-screening-runtime-state-disk-cache-v2"
_DISK_RUNTIME_CACHE_MAGIC = b"CHANLUN_SCREENING_RUNTIME_STATE_V2"
_DISK_RUNTIME_CACHE_DIR_ENV = "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_DIR"
_DISK_RUNTIME_CACHE_KEY_ENV = "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_KEY"
_DISK_RUNTIME_CACHE_IDENTITY_ENV = (
    "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_IDENTITY"
)
_DISK_RUNTIME_CACHE_SCOPE_ENV = "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_SCOPE"
_DISK_RUNTIME_CACHE_CAPACITY_ENV = (
    "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_CAPACITY"
)
_DISK_RUNTIME_CACHE_MAX_BYTES_ENV = (
    "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_MAX_BYTES"
)
_DISK_RUNTIME_CACHE_SCOPES = frozenset(
    {
        "web_lifecycle",
        "application_source_revision",
        "runtime_state_producer_revision",
    }
)
_DISK_RUNTIME_CACHE_ABI = (
    f"python-{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    f"|pandas-{pd.__version__}|numpy-{np.__version__}"
)
# 热点标的固定左边界后允许一代状态增长的最大完成 K 线数。达到边界即重新取
# 规范的最新窗口并完整校验，从而兼顾绝大多数逐 K 线增量与有界内存。
_RUNTIME_STABLE_WINDOW_EXTRA_BARS = {
    "1m": 480,
    "5m": 96,
}
_ANALYSIS_CACHE_CAPACITY_BY_FREQUENCY = {
    # 结果对象只保留很小的结构摘要（真实样本通常约 1--5 KiB），不持有原始行情帧。
    # 普通候选在优先通道运行期间会集中到一个隔离分片；当前约 2,029 只的工作集若沿
    # 用 512 槽，会在顺序轮询中产生完整 LRU 抖动，日线、30m 与 5m 摘要每轮都重算。
    # 4,096 可覆盖当前候选与合理波动，仍由频率分区和固定上限约束内存。
    "d": 4096,
    "30m": 4096,
    "5m": 4096,
    # 1m 摘要同样很小；覆盖当前约 705 只已武装标的后，同一分钟内的重试或重复
    # 请求可以直接命中，而逐 K 线运行态仍由单独的压缩 L2 字节预算约束。
    "1m": 1024,
}
_ANALYSIS_CACHE_CAPACITY = sum(_ANALYSIS_CACHE_CAPACITY_BY_FREQUENCY.values())
_HIGHER_TIMEFRAME_CACHE_CAPACITY = 4096
_KNOWN_SCREENING_INSTRUMENT_TYPES = frozenset(
    {
        "stock_cn",
        "etf_cn",
        "index_cn",
        "fund_cn",
        "unsupported_cn",
        "unresolved_cn",
    }
)


def _etf_proxy_sector_assessment(code: str) -> SectorAssessment:
    """Return the explicit non-industry context used by the ETF path."""

    return SectorAssessment(
        sector_id=f"etf-proxy:{code}",
        sector_name="ETF代理路径（不要求个股行业）",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(),
        reason_codes=("ETF_PROXY_SECTOR_NOT_REQUIRED",),
    )


@dataclass(frozen=True, slots=True)
class FrameStructureAnalysis:
    closed_at: datetime
    direction: ContextDirection
    confirmed_points: tuple[StructuralPoint, ...]
    provisional_points: tuple[ProvisionalCandidate, ...]
    setup_confirmed_points: tuple[StructuralPoint, ...] = ()
    segment_difference_points: tuple[StructuralPoint, ...] | None = None
    latest_price: float | None = None
    warmup_converged: bool = True
    warmup_full_bar_count: int = 0
    warmup_suffix_bar_count: int = 0
    warmup_reason_codes: tuple[str, ...] = ()
    warmup_difference_codes: tuple[str, ...] = ()
    trade_level_warmup_converged: bool | None = None
    trade_level_warmup_reason_codes: tuple[str, ...] = ()
    trade_level_warmup_difference_codes: tuple[str, ...] = ()
    same_period_technical_context: SamePeriodTechnicalContext | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "closed_at",
            normalize_datetime(self.closed_at, "closed_at"),
        )
        if self.direction not in {"up", "down", "neutral"}:
            raise ValueError("invalid structure direction")
        if not self.setup_confirmed_points and self.confirmed_points:
            # 测试夹具和非 5m 调用默认复用已经按末端线段血缘过滤的当前点；
            # 真实 5m 分析也必须显式传入同一集合。
            object.__setattr__(
                self,
                "setup_confirmed_points",
                self.confirmed_points,
            )
        if any(point.status != "confirmed" for point in self.setup_confirmed_points):
            raise ValueError("setup confirmed points must be confirmed")
        if any(
            point.status != "confirmed"
            for point in (self.segment_difference_points or ())
        ):
            raise ValueError("segment difference points must be confirmed")
        segment_point_ids = tuple(
            point.point_id for point in (self.segment_difference_points or ())
        )
        if len(segment_point_ids) != len(set(segment_point_ids)):
            raise ValueError("segment difference point ids must be unique")
        if self.latest_price is not None and (
            not np.isfinite(self.latest_price) or self.latest_price <= 0
        ):
            raise ValueError("latest_price must be a positive finite number")
        if min(self.warmup_full_bar_count, self.warmup_suffix_bar_count) < 0:
            raise ValueError("warmup bar counts cannot be negative")
        if len(self.warmup_reason_codes) != len(set(self.warmup_reason_codes)):
            raise ValueError("warmup reason codes must be unique")
        if len(self.warmup_difference_codes) != len(
            set(self.warmup_difference_codes)
        ) or not set(self.warmup_difference_codes).issubset(
            SCREENING_WARMUP_DIFFERENCE_CODES
        ):
            raise ValueError("warmup difference codes are invalid")
        if self.trade_level_warmup_converged not in {None, True, False}:
            raise ValueError("trade-level warmup convergence is invalid")
        if len(self.trade_level_warmup_reason_codes) != len(
            set(self.trade_level_warmup_reason_codes)
        ):
            raise ValueError("trade-level warmup reason codes must be unique")
        if len(self.trade_level_warmup_difference_codes) != len(
            set(self.trade_level_warmup_difference_codes)
        ) or not set(self.trade_level_warmup_difference_codes).issubset(
            SCREENING_WARMUP_DIFFERENCE_CODES
        ):
            raise ValueError("trade-level warmup difference codes are invalid")
        if self.same_period_technical_context is not None and (
            self.same_period_technical_context.observed_at != self.closed_at
        ):
            raise ValueError("same-period technical context time mismatch")

    @property
    def effective_segment_difference_points(self) -> tuple[StructuralPoint, ...]:
        """Return the explicit causal ledger or the legacy live-tail fallback."""

        return (
            self.confirmed_points
            if self.segment_difference_points is None
            else self.segment_difference_points
        )


@dataclass(slots=True)
class _WarmupRuntimeStates:
    full: ScreeningRuntimeState
    suffix: ScreeningRuntimeState


@dataclass(frozen=True, slots=True)
class _SerializedWarmupRuntimeStates:
    payload: bytes
    raw_size: int
    retained_frame_start: datetime | None
    retained_frame_count: int
    admission_rank: int

    @property
    def byte_size(self) -> int:
        return len(self.payload)


class _AuthenticatedRuntimeStateDiskCache:
    """保存候选 5m 压缩运行态，并在反序列化前验证身份和签名。

    正式运行的目录和 HMAC 密钥绑定精确的结构运行态生产者版本，可以跨普通 Web
    重启及外围决策代码变化复用；结构生产者或 Python/数据框 ABI 变化时拒绝旧内容。
    磁盘内容必须先通过 HMAC、身份和长度校验，才会交给 pickle，从而不把持久文件
    扩大成新的反序列化信任边界。
    """

    _DEFAULT_CAPACITY = 3072
    _DEFAULT_MAX_BYTES = 8 * 1024 * 1024 * 1024

    def __init__(
        self,
        *,
        root: Path,
        key: bytes,
        capacity: int,
        max_bytes: int,
        identity: str,
        scope: str,
    ) -> None:
        if len(key) < 32:
            raise ValueError("runtime disk cache key is too short")
        if capacity <= 0 or max_bytes <= 0:
            raise ValueError("runtime disk cache limits must be positive")
        if (
            not isinstance(identity, str)
            or not identity
            or len(identity) > 512
            or any(ord(character) < 32 for character in identity)
        ):
            raise ValueError("runtime disk cache identity is invalid")
        if scope not in _DISK_RUNTIME_CACHE_SCOPES:
            raise ValueError("runtime disk cache scope is invalid")
        self._root = root.resolve()
        self._key = key
        self._capacity = capacity
        self._max_bytes = max_bytes
        self._identity = identity
        self._scope = scope
        self._lock = RLock()
        self._entries: dict[Path, tuple[int, int]] = {}
        self._total_bytes = 0
        self._counters: Counter[str] = Counter()
        self._root.mkdir(parents=True, exist_ok=True)
        self._load_index()

    @classmethod
    def from_environment(cls) -> "_AuthenticatedRuntimeStateDiskCache | None":
        root_text = os.environ.get(_DISK_RUNTIME_CACHE_DIR_ENV, "").strip()
        key_text = os.environ.get(_DISK_RUNTIME_CACHE_KEY_ENV, "").strip()
        identity = os.environ.get(_DISK_RUNTIME_CACHE_IDENTITY_ENV, "").strip()
        scope = os.environ.get(
            _DISK_RUNTIME_CACHE_SCOPE_ENV,
            "web_lifecycle",
        ).strip()
        if not root_text and not key_text:
            return None
        if not root_text or not key_text:
            raise ValueError("runtime disk cache environment is incomplete")
        try:
            key = bytes.fromhex(key_text)
            capacity = int(
                os.environ.get(
                    _DISK_RUNTIME_CACHE_CAPACITY_ENV,
                    str(cls._DEFAULT_CAPACITY),
                )
            )
            max_bytes = int(
                os.environ.get(
                    _DISK_RUNTIME_CACHE_MAX_BYTES_ENV,
                    str(cls._DEFAULT_MAX_BYTES),
                )
            )
        except ValueError as exc:
            raise ValueError("runtime disk cache environment is invalid") from exc
        if not identity:
            if scope != "web_lifecycle":
                raise ValueError("persistent runtime disk cache identity is missing")
            identity = "legacy-web-lifecycle"
        return cls(
            root=Path(root_text),
            key=key,
            capacity=capacity,
            max_bytes=max_bytes,
            identity=identity,
            scope=scope,
        )

    @staticmethod
    def _admission_rank(code: str) -> int:
        digest = hashlib.sha256(code.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")

    def _path(self, *, code: str, frequency: str) -> Path:
        if frequency != "5m":
            raise ValueError("disk runtime cache only supports candidate 5m state")
        rank = self._admission_rank(code)
        identity = hashlib.sha256(f"{frequency}\0{code}".encode("utf-8")).hexdigest()
        return self._root / frequency / f"{rank:016x}-{identity}.clrt"

    def _load_index(self) -> None:
        entries: dict[Path, tuple[int, int]] = {}
        total_bytes = 0
        for path in self._root.glob("5m/*.clrt"):
            try:
                rank_text = path.name.split("-", 1)[0]
                rank = int(rank_text, 16)
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            if size <= 0:
                continue
            entries[path] = (rank, size)
            total_bytes += size
        self._entries = entries
        self._total_bytes = total_bytes
        self._evict_to_limits()

    def _evict_to_limits(self) -> None:
        while (
            len(self._entries) > self._capacity
            or self._total_bytes > self._max_bytes
        ):
            path = max(
                self._entries,
                key=lambda value: (self._entries[value][0], value.name),
            )
            _rank, size = self._entries.pop(path)
            self._total_bytes = max(0, self._total_bytes - size)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                self._counters["eviction_failure"] += 1
            else:
                self._counters["eviction"] += 1

    def store(
        self,
        *,
        code: str,
        frequency: str,
        cached: _SerializedWarmupRuntimeStates,
    ) -> None:
        if frequency != "5m":
            return
        started = perf_counter()
        path = self._path(code=code, frequency=frequency)
        retained_start = (
            None
            if cached.retained_frame_start is None
            else cached.retained_frame_start.isoformat()
        )
        header = {
            "schema": _DISK_RUNTIME_CACHE_SCHEMA,
            "cache_identity": self._identity,
            "runtime_abi": _DISK_RUNTIME_CACHE_ABI,
            "code": code,
            "frequency": frequency,
            "raw_size": cached.raw_size,
            "retained_frame_start": retained_start,
            "retained_frame_count": cached.retained_frame_count,
            "admission_rank": cached.admission_rank,
            "payload_size": len(cached.payload),
        }
        header_bytes = json.dumps(
            header,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        signed = header_bytes + b"\n" + cached.payload
        signature = hmac.new(self._key, signed, hashlib.sha256).hexdigest().encode(
            "ascii"
        )
        content = (
            _DISK_RUNTIME_CACHE_MAGIC
            + b"\n"
            + signature
            + b"\n"
            + signed
        )
        temporary_path: Path | None = None
        try:
            with self._lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    prefix=f".{path.stem}.",
                    suffix=".tmp",
                    dir=path.parent,
                    delete=False,
                ) as handle:
                    temporary_path = Path(handle.name)
                    handle.write(content)
                os.replace(temporary_path, path)
                temporary_path = None
                previous = self._entries.pop(path, None)
                if previous is not None:
                    self._total_bytes = max(0, self._total_bytes - previous[1])
                size = len(content)
                self._entries[path] = (cached.admission_rank, size)
                self._total_bytes += size
                self._counters["store"] += 1
                self._evict_to_limits()
        except OSError:
            self._counters["store_failure"] += 1
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            self._counters["store_elapsed_milliseconds"] += max(
                0,
                int((perf_counter() - started) * 1000),
            )

    def contains(self, *, code: str, frequency: str) -> bool:
        if frequency != "5m":
            return False
        path = self._path(code=code, frequency=frequency)
        with self._lock:
            return path in self._entries

    def load(
        self,
        *,
        code: str,
        frequency: str,
    ) -> _SerializedWarmupRuntimeStates | None:
        if frequency != "5m":
            return None
        started = perf_counter()
        path = self._path(code=code, frequency=frequency)
        try:
            content = path.read_bytes()
        except FileNotFoundError:
            self._counters["miss"] += 1
            return None
        except OSError:
            self._counters["read_failure"] += 1
            return None
        try:
            magic, signature, header_bytes, payload = content.split(b"\n", 3)
            if magic != _DISK_RUNTIME_CACHE_MAGIC:
                raise ValueError("runtime disk cache magic changed")
            expected_signature = hmac.new(
                self._key,
                header_bytes + b"\n" + payload,
                hashlib.sha256,
            ).hexdigest().encode("ascii")
            if not hmac.compare_digest(signature, expected_signature):
                raise ValueError("runtime disk cache signature changed")
            header = json.loads(header_bytes.decode("ascii"))
            expected_fields = {
                "schema",
                "cache_identity",
                "runtime_abi",
                "code",
                "frequency",
                "raw_size",
                "retained_frame_start",
                "retained_frame_count",
                "admission_rank",
                "payload_size",
            }
            if not isinstance(header, dict) or set(header) != expected_fields:
                raise ValueError("runtime disk cache header changed")
            if (
                header["schema"] != _DISK_RUNTIME_CACHE_SCHEMA
                or header["cache_identity"] != self._identity
                or header["runtime_abi"] != _DISK_RUNTIME_CACHE_ABI
                or header["code"] != code
                or header["frequency"] != frequency
                or type(header["raw_size"]) is not int
                or header["raw_size"] <= 0
                or type(header["retained_frame_count"]) is not int
                or header["retained_frame_count"] <= 0
                or header["admission_rank"] != self._admission_rank(code)
                or header["payload_size"] != len(payload)
            ):
                raise ValueError("runtime disk cache identity changed")
            retained_text = header["retained_frame_start"]
            retained_start = (
                None
                if retained_text is None
                else datetime.fromisoformat(retained_text)
            )
            if retained_text is not None and retained_start.tzinfo is None:
                raise ValueError("runtime disk cache time is naive")
            cached = _SerializedWarmupRuntimeStates(
                payload=payload,
                raw_size=header["raw_size"],
                retained_frame_start=retained_start,
                retained_frame_count=header["retained_frame_count"],
                admission_rank=header["admission_rank"],
            )
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._counters["validation_failure"] += 1
            with self._lock:
                previous = self._entries.pop(path, None)
                if previous is not None:
                    self._total_bytes = max(0, self._total_bytes - previous[1])
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    self._counters["validation_cleanup_failure"] += 1
            return None
        finally:
            self._counters["load_elapsed_milliseconds"] += max(
                0,
                int((perf_counter() - started) * 1000),
            )
        self._counters["hit"] += 1
        return cached

    def health_snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "schema": _DISK_RUNTIME_CACHE_SCHEMA,
                "enabled": True,
                "frequency": "5m",
                "entry_count": len(self._entries),
                "capacity": self._capacity,
                "bytes": self._total_bytes,
                "max_bytes": self._max_bytes,
                "counters": dict(sorted(self._counters.items())),
                "authenticated_before_deserialization": True,
                "web_lifecycle_scoped": self._scope == "web_lifecycle",
                "application_source_revision_scoped": (
                    self._scope == "application_source_revision"
                ),
                "runtime_state_producer_revision_scoped": (
                    self._scope == "runtime_state_producer_revision"
                ),
                "cache_identity_sha256": (
                    "sha256:"
                    + hashlib.sha256(self._identity.encode("utf-8")).hexdigest()
                ),
                "runtime_abi": _DISK_RUNTIME_CACHE_ABI,
            }


class SectorAnalysisUnavailable(RuntimeError):
    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        if not code:
            raise ValueError("sector analysis error code is required")
        self.code = code


class StrictStructureAnalysisError(RuntimeError):
    """表示已校验行情帧不满足严格结构契约。"""


@dataclass(frozen=True, slots=True)
class SectorAnalysisFailure:
    sector_id: str
    code: str
    error_type: str
    reason: str
    detail_code: str | None = None
    catalog_member_count: int | None = None
    universe_member_count: int | None = None

    def __post_init__(self) -> None:
        for field_name in ("sector_id", "code", "error_type", "reason"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} is required")
        if self.detail_code is not None and (
            not isinstance(self.detail_code, str) or not self.detail_code
        ):
            raise ValueError("detail_code must be a non-empty string")
        for field_name in ("catalog_member_count", "universe_member_count"):
            value = getattr(self, field_name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{field_name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class SectorAnalysisExclusion:
    """表示确定性的目录资格结论，而不是分析失败。"""

    sector_id: str
    code: str
    reason_code: str
    reason: str
    detail_code: str
    catalog_member_count: int
    universe_member_count: int
    required_member_count: int

    def __post_init__(self) -> None:
        for field_name in (
            "sector_id",
            "code",
            "reason_code",
            "reason",
            "detail_code",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} is required")
        if self.reason_code != "sector_member_coverage_insufficient":
            raise ValueError("unsupported sector exclusion reason")
        for field_name in (
            "catalog_member_count",
            "universe_member_count",
            "required_member_count",
        ):
            value = getattr(self, field_name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.required_member_count <= 0:
            raise ValueError("required_member_count must be positive")
        if self.universe_member_count >= self.required_member_count:
            raise ValueError("sector exclusion must be below its member threshold")


@dataclass(frozen=True, slots=True)
class SectorAssessmentBatch:
    assessments: tuple[SectorAssessment, ...]
    discovered_count: int
    completed_count: int
    failure_counts: tuple[tuple[str, int], ...]
    errors: tuple[SectorAnalysisFailure, ...]
    exclusion_counts: tuple[tuple[str, int], ...] = ()
    exclusions: tuple[SectorAnalysisExclusion, ...] = ()
    # 精确的 QMT 目录身份。缺失时批次保持关闭失败，不能进入前向发布。
    catalog_revision: str | None = None
    # 紧凑的成员分类证据，用于重算全部横向强度、跨板块排序和各板块来源身份。
    # 缺失时只能展示，不能进入前向发布。
    strength_evidence: SectorStrengthBatch | None = None
    # (GICS4 子板块, GICS3 父板块)。旧 GICS3 批次为空，保持历史兼容。
    parent_relations: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "assessments", tuple(self.assessments))
        object.__setattr__(self, "failure_counts", tuple(self.failure_counts))
        object.__setattr__(self, "errors", tuple(self.errors))
        object.__setattr__(self, "exclusion_counts", tuple(self.exclusion_counts))
        object.__setattr__(self, "exclusions", tuple(self.exclusions))
        object.__setattr__(
            self,
            "parent_relations",
            tuple(tuple(value) for value in self.parent_relations),
        )
        if self.catalog_revision is not None and (
            not isinstance(self.catalog_revision, str)
            or not self.catalog_revision.startswith("sha256:")
        ):
            raise ValueError("sector catalog revision must be a sha256 identity")
        if self.strength_evidence is not None:
            evidence_ids = tuple(self.strength_evidence)
            assessment_ids = tuple(sorted(item.sector_id for item in self.assessments))
            if evidence_ids != assessment_ids:
                raise ValueError("sector strength evidence must cover every assessment")
            assessment_by_id = {item.sector_id: item for item in self.assessments}
            if any(
                assessment_by_id[sector_id].horizontal_strength
                != self.strength_evidence[sector_id].strength
                or assessment_by_id[sector_id].horizontal_rank
                != self.strength_evidence[sector_id].rank
                or assessment_by_id[sector_id].strength_anchor_session
                != self.strength_evidence[sector_id].anchor_session
                or assessment_by_id[sector_id].strength_member_count
                != self.strength_evidence[sector_id].member_count
                or assessment_by_id[sector_id].strength_source_revision
                != self.strength_evidence[sector_id].source_revision
                or assessment_by_id[sector_id].strength_reason_codes
                != self.strength_evidence[sector_id].reason_codes
                for sector_id in evidence_ids
            ):
                raise ValueError("sector strength evidence does not match assessments")
            evidence_document = self.strength_evidence.evidence_document()
            if (
                self.catalog_revision is not None
                and evidence_document.get("membership_revision")
                != self.catalog_revision
            ):
                raise ValueError(
                    "sector strength evidence membership revision is inconsistent"
                )
        assessment_ids = {item.sector_id for item in self.assessments}
        if self.parent_relations != tuple(sorted(self.parent_relations)):
            raise ValueError("sector parent relations must be sorted")
        child_ids: set[str] = set()
        for relation in self.parent_relations:
            if (
                len(relation) != 2
                or not isinstance(relation[0], str)
                or not relation[0].startswith("qmt-gics4:")
                or not isinstance(relation[1], str)
                or not relation[1].startswith("qmt-gics3:")
                or relation[0] in child_ids
                or relation[0] not in assessment_ids
                or relation[1] not in assessment_ids
            ):
                raise ValueError("sector parent relation is invalid")
            child_ids.add(relation[0])
        if (
            type(self.discovered_count) is not int
            or type(self.completed_count) is not int
            or not 0 <= self.completed_count <= self.discovered_count
        ):
            raise ValueError("sector completion counts are invalid")
        if len({item.sector_id for item in self.errors}) != len(self.errors):
            raise ValueError("sector analysis errors must have unique sector ids")
        if len({item.sector_id for item in self.exclusions}) != len(self.exclusions):
            raise ValueError("sector analysis exclusions must have unique sector ids")
        if {item.sector_id for item in self.errors} & {
            item.sector_id for item in self.exclusions
        }:
            raise ValueError("sector errors and exclusions must be disjoint")
        normalized_counts = tuple(sorted(self.failure_counts))
        if normalized_counts != self.failure_counts:
            raise ValueError("sector failure counts must be sorted by code")
        if any(
            not code or type(count) is not int or count <= 0
            for code, count in self.failure_counts
        ):
            raise ValueError("sector failure counts are invalid")
        if sum(count for _code, count in self.failure_counts) != len(self.errors):
            raise ValueError("sector failure counts do not match errors")
        actual_counts = tuple(
            sorted(Counter(item.error_type for item in self.errors).items())
        )
        if actual_counts != self.failure_counts:
            raise ValueError("sector failure codes do not match errors")
        normalized_exclusion_counts = tuple(sorted(self.exclusion_counts))
        if normalized_exclusion_counts != self.exclusion_counts:
            raise ValueError("sector exclusion counts must be sorted by code")
        if any(
            not code or type(count) is not int or count <= 0
            for code, count in self.exclusion_counts
        ):
            raise ValueError("sector exclusion counts are invalid")
        if sum(count for _code, count in self.exclusion_counts) != len(self.exclusions):
            raise ValueError("sector exclusion counts do not match exclusions")
        actual_exclusion_counts = tuple(
            sorted(Counter(item.reason_code for item in self.exclusions).items())
        )
        if actual_exclusion_counts != self.exclusion_counts:
            raise ValueError("sector exclusion codes do not match exclusions")

    @property
    def completion_ratio(self) -> Decimal:
        if self.discovered_count == 0:
            return Decimal("0")
        return Decimal(self.completed_count) / Decimal(self.discovered_count)

    @property
    def resolution_ratio(self) -> Decimal:
        if self.discovered_count == 0:
            return Decimal("0")
        return Decimal(self.completed_count + len(self.exclusions)) / Decimal(
            self.discovered_count
        )


@dataclass(frozen=True, slots=True)
class CachedSectorSnapshot:
    """供盘中监听只读复用的已校验板块快照。

    该对象只表达磁盘中已经完成的事实，读取它不得触发板块计算。是否仍属于当前
    行情周期由提供器明确给出；过期快照只能帮助恢复成员路由和展示上下文，不能放行
    新买入。
    """

    batch: SectorAssessmentBatch
    members: Mapping[str, tuple[str, ...]]
    requested_as_of: datetime
    current_decision_epoch: bool
    content_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.batch, SectorAssessmentBatch):
            raise TypeError("cached sector batch is invalid")
        normalized_members: dict[str, tuple[str, ...]] = {}
        for sector_id, values in self.members.items():
            if (
                not isinstance(sector_id, str)
                or not sector_id
                or isinstance(values, (str, bytes))
                or not isinstance(values, Sequence)
                or any(not isinstance(value, str) or not value for value in values)
            ):
                raise ValueError("cached sector members are invalid")
            normalized_members[sector_id] = tuple(values)
        object.__setattr__(self, "members", normalized_members)
        object.__setattr__(
            self,
            "requested_as_of",
            normalize_datetime(self.requested_as_of, "cached sector requested_as_of"),
        )
        if type(self.current_decision_epoch) is not bool:
            raise TypeError("cached sector current_decision_epoch must be a boolean")
        if (
            not isinstance(self.content_sha256, str)
            or not self.content_sha256.startswith("sha256:")
        ):
            raise ValueError("cached sector content_sha256 is invalid")


def _sector_failure_document(item: SectorAnalysisFailure) -> dict[str, object]:
    result: dict[str, object] = {
        "sector_id": item.sector_id,
        "code": item.code,
        "error_type": item.error_type,
        "reason": item.reason[:160],
    }
    if item.detail_code is not None:
        result["detail_code"] = item.detail_code
    if item.catalog_member_count is not None:
        result["catalog_member_count"] = item.catalog_member_count
    if item.universe_member_count is not None:
        result["universe_member_count"] = item.universe_member_count
    return result


def _sector_exclusion_document(
    item: SectorAnalysisExclusion,
) -> dict[str, object]:
    return {
        "sector_id": item.sector_id,
        "code": item.code,
        "exclusion_type": "sector_analysis_exclusion",
        "eligibility": "MINIMUM_SECTOR_MEMBERS_NOT_MET",
        "reason_code": item.reason_code,
        "reason": item.reason[:160],
        "detail_code": item.detail_code,
        "catalog_member_count": item.catalog_member_count,
        "universe_member_count": item.universe_member_count,
        "required_member_count": item.required_member_count,
        "deterministic_for_catalog_revision": True,
        "retry_policy": "NEXT_SECTOR_CATALOG_REVISION",
    }


class StructureAnalyzer(Protocol):
    def __call__(
        self,
        *,
        code: str,
        frequency: str,
        frame: pd.DataFrame,
        as_of: datetime,
    ) -> FrameStructureAnalysis: ...


@dataclass(frozen=True, slots=True)
class NativeTradingGatewayConfig:
    # 冷启动使用各物理周期可获得的完整 QMT 回看区间。旧的 3200/4800 根尾部数据
    # 足以形成线段，却不足以稳定一、二类点：同一标的在旧中枢恢复前可能只显示三类点。
    # 标的采用确定性分片，未变化数据帧会缓存，实时调度只分析有界变更代码批次；
    # 较长耗时仅发生在冷态契约重建。
    request_bars_by_frequency: tuple[tuple[str, int], ...] = (
        CANONICAL_REQUEST_BARS_BY_FREQUENCY
    )
    minimum_bars_by_frequency: tuple[tuple[str, int], ...] = (
        tuple(SCREENING_MINIMUM_BARS_BY_FREQUENCY.items())
    )
    minimum_sector_members: int = 8
    current_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS

    def __post_init__(self) -> None:
        for field_name in (
            "request_bars_by_frequency",
            "minimum_bars_by_frequency",
        ):
            values = dict(getattr(self, field_name))
            if set(values) != set(_FREQUENCIES):
                raise ValueError(f"{field_name} must define d, 30m, 5m and 1m")
            if any(type(value) is not int or value <= 0 for value in values.values()):
                raise ValueError(f"{field_name} values must be positive integers")
        requests = dict(self.request_bars_by_frequency)
        minimums = dict(self.minimum_bars_by_frequency)
        if any(minimums[key] > requests[key] for key in _FREQUENCIES):
            raise ValueError("minimum bars cannot exceed requested bars")
        if (
            type(self.minimum_sector_members) is not int
            or self.minimum_sector_members <= 0
        ):
            raise ValueError("minimum_sector_members must be a positive integer")
        if (
            type(self.current_setup_age_seconds) is not int
            or self.current_setup_age_seconds <= 0
        ):
            raise ValueError("current_setup_age_seconds must be a positive integer")

    def request_bars(self, frequency: str) -> int:
        return dict(self.request_bars_by_frequency)[frequency]

    def minimum_bars(self, frequency: str) -> int:
        return dict(self.minimum_bars_by_frequency)[frequency]


def _market_datetime(value: object, field_name: str) -> datetime:
    try:
        timestamp = pd.Timestamp(value)
    except Exception as exc:
        raise ValueError(f"{field_name} must be a datetime") from exc
    if pd.isna(timestamp):
        raise ValueError(f"{field_name} must be a datetime")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("Asia/Shanghai")
    else:
        timestamp = timestamp.tz_convert("Asia/Shanghai")
    return normalize_datetime(timestamp.to_pydatetime(), field_name)


def _closed_frame(
    value: object,
    *,
    not_after: datetime,
    minimum_bars: int,
) -> pd.DataFrame:
    if not isinstance(value, pd.DataFrame) or value.empty:
        raise ValueError("kline frame is unavailable")
    required = ("date", "open", "high", "low", "close", "volume")
    if any(column not in value.columns for column in required):
        raise ValueError("kline frame is missing required columns")
    snapshot_attrs = dict(value.attrs)
    optional = tuple(column for column in ("member_mask",) if column in value.columns)
    result = value.loc[:, [*required, *optional]].copy()
    raw_dates = result["date"]
    try:
        parsed_dates = pd.to_datetime(raw_dates, errors="raise")
        if bool(parsed_dates.isna().any()):
            raise ValueError("kline.date must be a datetime")
        if isinstance(parsed_dates.dtype, pd.DatetimeTZDtype):
            dates = parsed_dates.dt.tz_convert("Asia/Shanghai")
        elif pd.api.types.is_datetime64_any_dtype(parsed_dates.dtype):
            dates = parsed_dates.dt.tz_localize("Asia/Shanghai")
        else:
            raise TypeError("mixed datetime representation")
    except (TypeError, ValueError, OverflowError):
        # 混合时区或少见对象类型退回逐值规范化；正常 QMT DatetimeIndex 走上面的
        # 向量路径，避免每只股票对上万根 K 线创建 Python datetime 对象。
        dates = pd.Series(
            tuple(pd.Timestamp(_market_datetime(item, "kline.date")) for item in raw_dates),
            index=result.index,
            dtype="datetime64[ns, Asia/Shanghai]",
        )
    if not bool(dates.is_monotonic_increasing) or bool(dates.duplicated().any()):
        raise ValueError("kline dates must be strictly chronological")
    cutoff = pd.Timestamp(normalize_datetime(not_after, "not_after"))
    closed_mask = dates <= cutoff
    if not bool(closed_mask.any()):
        raise ValueError("kline frame has no closed bars")
    result = result.loc[closed_mask].copy().reset_index(drop=True)
    result["date"] = dates.loc[closed_mask].reset_index(drop=True).array
    numeric_columns = ("open", "high", "low", "close", "volume")
    for column in numeric_columns:
        result.loc[:, column] = pd.to_numeric(result[column], errors="coerce")
    numeric = result.loc[:, list(numeric_columns)].astype(float)
    numeric_values = numeric.to_numpy(dtype=np.float64, copy=False)
    prices = numeric_values[:, :4]
    invalid = (
        ~np.isfinite(numeric_values).all(axis=1)
        | (prices <= 0).any(axis=1)
        | (numeric_values[:, 4] < 0)
        | (numeric_values[:, 1] < prices.max(axis=1))
        | (numeric_values[:, 2] > prices.min(axis=1))
    )
    if bool(np.any(invalid)):
        raise ValueError("kline frame contains invalid market facts")
    if "member_mask" in result:
        masks = tuple(result["member_mask"])
        if any(
            isinstance(mask, bool) or not isinstance(mask, Integral) for mask in masks
        ):
            raise ValueError("kline member masks must be exact integers")
        result.loc[:, "member_mask"] = tuple(int(mask) for mask in masks)
    if len(result) < minimum_bars:
        raise ValueError("kline frame does not meet minimum history")
    result.loc[:, list(numeric_columns)] = numeric
    result.attrs = snapshot_attrs
    return result


def _frame_content_revision(frame: pd.DataFrame) -> str:
    """把分析缓存命中绑定到精确的已收盘输入前缀。

    价格基准身份会有意跨普通 QMT 刷新保持稳定，不能单独证明缓存结构有效：QMT 可能
    补齐缺失成分，或在末根收盘时间不变时修订开高低收。哈希需覆盖全部已使用行情行，
    板块合成还需覆盖精确贡献者位图路径，使这类修订必然触发确定性重算。
    """

    identity_attrs = {
        name: frame.attrs.get(name)
        for name in (
            "structure_price_quantum",
            "price_basis_revision",
            "price_basis_provider",
            "price_basis_adjustment",
            "source_base_stream_revision",
            "source_base_frequency",
            "sector_id",
            "sector_membership_revision",
            "sector_members",
            "sector_composite_members",
            "sector_composite_required_member_count",
            "sector_composite_member_mask_contract",
            "sector_composite_member_path_revision",
            "sector_composite_method",
            "sector_factor_adjustment_contract_id",
            "sector_factor_revision",
            "sector_thirty_minute_derivation_contract",
            "derived_frequency",
        )
        if name in frame.attrs
    }
    has_member_mask = "member_mask" in frame.columns
    if not has_member_mask:
        # 股票帧已经过 ``_closed_frame`` 的时区、有限数值和列顺序校验。直接对规范的
        # UTC 纳秒与 little-endian float64 字节做 SHA-256，保留逐位内容敏感性，同时
        # 避免每分钟为每根 K 线创建字典、datetime 和 JSON 对象。该身份只用于进程内
        # 分析缓存，不是跨版本的持久协议。
        dates = pd.DatetimeIndex(frame["date"])
        if dates.tz is None:
            dates = dates.tz_localize("Asia/Shanghai")
        else:
            dates = dates.tz_convert("Asia/Shanghai")
        date_values = np.asarray(dates.asi8, dtype="<i8")
        numeric_values = np.ascontiguousarray(
            frame.loc[:, ["open", "high", "low", "close", "volume"]].to_numpy(
                dtype=np.float64,
                copy=True,
            ),
            dtype="<f8",
        )
        digest = hashlib.sha256()
        digest.update(b"chanlun-screening-closed-stock-frame\0")
        digest.update(
            sha256_json(
                {
                    "schema": "chanlun-screening-closed-stock-frame",
                    "attrs": identity_attrs,
                    "row_count": len(frame),
                }
            ).encode("ascii")
        )
        digest.update(date_values.tobytes(order="C"))
        digest.update(numeric_values.tobytes(order="C"))
        return "sha256:" + digest.hexdigest()
    return sha256_json(
        {
            "schema": "chanlun-screening-closed-frame",
            "attrs": identity_attrs,
            "rows": tuple(
                {
                    "date": _market_datetime(row.date, "kline.date"),
                    "open": float(row.open),
                    "high": float(row.high),
                    "low": float(row.low),
                    "close": float(row.close),
                    "volume": float(row.volume),
                    **(
                        {"member_mask": int(row.member_mask)} if has_member_mask else {}
                    ),
                }
                for row in frame.itertuples(index=False)
            ),
        }
    )


def _entry_valid_until(confirmation_closed_at: datetime) -> datetime:
    """返回按结束时间标记的 A 股 1m 行情对应的冻结可选入场有效期。"""

    return a_share_optional_entry_valid_until(confirmation_closed_at)


def _entry_execution_boundaries(
    *,
    code: str,
    pairs: tuple[tuple[StructuralPoint, StructuralPoint], ...],
    decision_at: datetime,
    raw_frame: pd.DataFrame,
) -> tuple[EntryExecutionBoundary, ...]:
    """把 5m setup/1m 区间套见证对绑定到共同可知时刻的不复权行情。"""

    decision_closed_at = normalize_datetime(decision_at, "decision_at")
    metadata = strict_snapshot_price_metadata(raw_frame)
    if (
        raw_frame.attrs.get("price_basis_provider") != "qmt"
        or raw_frame.attrs.get("price_basis_adjustment") != "none"
    ):
        raise ValueError("entry confirmation evidence must be unadjusted QMT data")
    rows_by_time: dict[datetime, object] = {}
    for row in raw_frame.itertuples(index=False):
        bar_closed_at = _market_datetime(row.date, "raw confirmation bar close")
        if bar_closed_at in rows_by_time:
            raise ValueError("raw confirmation bar times must be unique")
        rows_by_time[bar_closed_at] = row
    row = rows_by_time.get(decision_closed_at)
    if row is None:
        return ()
    output: list[EntryExecutionBoundary] = []
    for setup, point in pairs:
        if max(setup.available_at, point.available_at) != decision_closed_at:
            raise ValueError("entry boundary pair is not new at decision time")
        output.append(
            EntryExecutionBoundary(
                symbol=code,
                setup_occurrence_id=structural_point_occurrence_id(setup),
                point_id=point.point_id,
                source_frequency="1m",
                confirmation_bar_closed_at=decision_closed_at,
                raw_open=Decimal(str(row.open)),
                raw_high=Decimal(str(row.high)),
                raw_low=Decimal(str(row.low)),
                raw_close=Decimal(str(row.close)),
                raw_volume=Decimal(str(row.volume)),
                entry_valid_until=_entry_valid_until(decision_closed_at),
                raw_price_basis_revision=metadata.price_basis_revision,
            )
        )
    return tuple(
        sorted(
            output,
            key=lambda value: (value.setup_occurrence_id, value.point_id),
        )
    )


def _new_entry_boundary_pairs(
    *,
    setup_points: tuple[StructuralPoint, ...],
    witness_points: tuple[StructuralPoint, ...],
    decision_at: datetime,
) -> tuple[tuple[StructuralPoint, StructuralPoint], ...]:
    """Return each executable 5m buy setup's first exact 1m nesting pair.

    A pair is new only at its first jointly-known timestamp. Re-evaluating a
    later bar, including after another nested 1m point appears, therefore can
    never recreate or move the execution window.
    """

    closed_at = normalize_datetime(decision_at, "decision_at")
    pairs = {
        (structural_point_occurrence_id(setup), witness.point_id): (
            setup,
            witness,
        )
        for setup in setup_points
        if setup.side == "buy"
        and five_minute_setup_is_in_policy_scope(setup)
        and five_minute_setup_is_executable(setup, as_of=closed_at)
        for witness in (
            match_one_minute_nesting_witness_for_point(
                setup,
                witness_points,
                as_of=closed_at,
            ),
        )
        if witness is not None
        and max(setup.available_at, witness.available_at) == closed_at
    }
    return tuple(pairs[key] for key in sorted(pairs))


def analyze_native_frame(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
    runtime_state: ScreeningRuntimeState | None = None,
) -> FrameStructureAnalysis:
    if frequency not in _FREQUENCIES:
        raise ValueError("unsupported trading frequency")
    closed_at = normalize_datetime(as_of, "as_of")
    # 元数据失败描述输入快照，而非结构引擎契约违规，因此保留公开的 ValueError 分类。
    strict_snapshot_price_metadata(frame)
    context_runtime_state: ScreeningRuntimeState | None = runtime_state
    try:
        if runtime_state is None and frequency not in {"d", "30m"}:
            evidence = screening_evidence_from_frame(
                code=code,
                frequency=frequency,
                frame=frame,
                as_of=closed_at,
                market="a",
            )
        else:
            if context_runtime_state is None:
                context_runtime_state = ScreeningRuntimeState(
                    code,
                    frequency,
                    market="a",
                )
            update = context_runtime_state.update_from_frame(
                frame=frame,
                as_of=closed_at,
            )
            evidence = update.evidence()
    except (StrictStructureContractError, ValueError) as exc:
        raise StrictStructureAnalysisError(str(exc)) from exc
    try:
        provisional = extract_current_provisional_candidates(
            evidence,
            code=code,
            source_frequency=frequency,
            as_of=closed_at,
        )
        current_confirmed = extract_current_confirmed_points(
            evidence,
            code=code,
            source_frequency=frequency,
            as_of=closed_at,
        )
        segment_difference_points = (
            extract_one_minute_segment_difference_points(
                evidence,
                code=code,
                source_frequency=frequency,
                as_of=closed_at,
            )
            if frequency == "1m"
            else current_confirmed
        )
        analysis = FrameStructureAnalysis(
            closed_at=closed_at,
            direction=_strict_direction(evidence),
            confirmed_points=current_confirmed,
            # 交易设置与页面“当前状态”共享同一条末端线段血缘。完整正式点
            # 账本仍由严格证据保存，只供图表和审计使用，不能再依靠四天时窗
            # 把已经离开末端两条线段的旧点重新带回实时选股。
            setup_confirmed_points=current_confirmed,
            segment_difference_points=segment_difference_points,
            provisional_points=provisional,
            latest_price=float(
                pd.to_numeric(frame["close"], errors="raise").iloc[-1]
            ),
            same_period_technical_context=(
                None
                if frequency not in {"d", "30m"}
                or context_runtime_state is None
                or context_runtime_state.cl_state is None
                else build_same_period_technical_context(
                    frequency=frequency,
                    frame=frame,
                    cl_state=context_runtime_state.cl_state,
                    as_of=closed_at,
                )
            ),
        )
        return analysis
    finally:
        if context_runtime_state is not None:
            context_runtime_state.release_evidence_cache()


def _warmup_tail_signature(
    analysis: FrameStructureAnalysis,
    *,
    not_before: datetime,
    trade_level_only: bool = False,
) -> tuple[object, ...]:
    return screening_warmup_tail_signature(
        direction=analysis.direction,
        points=analysis.setup_confirmed_points,
        not_before=not_before,
        trade_level_only=trade_level_only,
    )


def _warmup_latest_points(
    analysis: FrameStructureAnalysis,
    *,
    not_before: datetime,
    trade_level_only: bool = False,
) -> dict[tuple[str, str, int, str], tuple[datetime, StructuralPoint]]:
    """返回活动签名使用的各条正式语义通道中的最新点。"""

    latest: dict[
        tuple[str, str, int, str], tuple[datetime, StructuralPoint]
    ] = {}
    for point in analysis.setup_confirmed_points:
        if trade_level_only and not is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        ):
            continue
        observed_at = point.available_at
        if point.terminal_segment is None and (
            observed_at < not_before or point.anchor_at < not_before
        ):
            continue
        lane = (
            point.point_type,
            point.tower,
            point.recursive_level,
            (
                "legacy"
                if point.terminal_segment is None
                else point.terminal_segment.role
            ),
        )
        previous = latest.get(lane)
        if previous is None or observed_at > previous[0]:
            latest[lane] = (observed_at, point)
    return latest


def _warmup_tail_difference_codes(
    full: FrameStructureAnalysis,
    short: FrameStructureAnalysis,
    *,
    not_before: datetime,
    trade_level_only: bool = False,
) -> tuple[str, ...]:
    """解释两段历史的正式证据差异，不放宽活动门禁。"""

    codes: list[str] = []
    if not trade_level_only and full.direction != short.direction:
        codes.append("WARMUP_DIRECTION_CHANGED")
    full_points = _warmup_latest_points(
        full,
        not_before=not_before,
        trade_level_only=trade_level_only,
    )
    short_points = _warmup_latest_points(
        short,
        not_before=not_before,
        trade_level_only=trade_level_only,
    )
    if set(full_points) != set(short_points):
        codes.append("WARMUP_ACTIVE_POINT_LANES_CHANGED")

    def values(point: object, names: tuple[str, ...]) -> tuple[object, ...]:
        return tuple(getattr(point, name, None) for name in names)

    for lane in sorted(set(full_points).intersection(short_points)):
        left = full_points[lane][1]
        right = short_points[lane][1]
        if type(left) is not type(right) or values(
            left,
            ("side", "status", "actionable"),
        ) != values(right, ("side", "status", "actionable")):
            codes.append("WARMUP_POINT_STATUS_CHANGED")
        if values(
            left,
            ("anchor_at", "confirmed_at", "available_at", "observed_at"),
        ) != values(
            right,
            ("anchor_at", "confirmed_at", "available_at", "observed_at"),
        ):
            codes.append("WARMUP_POINT_TIMING_CHANGED")
        if values(
            left,
            (
                "price_basis_revision",
                "structure_anchor_price",
                "structure_invalidation_price",
                "center_zd",
                "center_zg",
                "anchor_price",
            ),
        ) != values(
            right,
            (
                "price_basis_revision",
                "structure_anchor_price",
                "structure_invalidation_price",
                "center_zd",
                "center_zg",
                "anchor_price",
            ),
        ):
            codes.append("WARMUP_PRICE_OR_BOUNDARY_CHANGED")
        if values(
            left,
            (
                "source_frequency",
                "center_ordinal",
                "variant",
                "divergence_kind",
            ),
        ) != values(
            right,
            (
                "source_frequency",
                "center_ordinal",
                "variant",
                "divergence_kind",
            ),
        ):
            codes.append("WARMUP_STRUCTURE_IDENTITY_CHANGED")
        if values(
            left,
            ("evidence_codes", "missing_conditions"),
        ) != values(right, ("evidence_codes", "missing_conditions")):
            codes.append("WARMUP_POINT_EVIDENCE_CHANGED")
    unique = tuple(dict.fromkeys(codes))
    if not unique and _warmup_tail_signature(
        full,
        not_before=not_before,
        trade_level_only=trade_level_only,
    ) != _warmup_tail_signature(
        short,
        not_before=not_before,
        trade_level_only=trade_level_only,
    ):
        return ("WARMUP_OTHER_SEMANTIC_CHANGED",)
    return unique


def analyze_native_frame_with_warmup(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
    runtime_states: _WarmupRuntimeStates | None = None,
) -> FrameStructureAnalysis:
    """要求活动尾部的正式证据在两种左侧历史长度下保持一致。"""

    full = analyze_native_frame(
        code=code,
        frequency=frequency,
        frame=frame,
        as_of=as_of,
        runtime_state=(None if runtime_states is None else runtime_states.full),
    )
    required = SCREENING_WARMUP_REQUIRED_BARS[frequency]
    if len(frame) < required:
        return replace(
            full,
            warmup_converged=False,
            warmup_full_bar_count=len(frame),
            warmup_suffix_bar_count=0,
            warmup_reason_codes=("WARMUP_HISTORY_INSUFFICIENT",),
            trade_level_warmup_converged=(False if frequency == "5m" else None),
            trade_level_warmup_reason_codes=(
                ("WARMUP_HISTORY_INSUFFICIENT",) if frequency == "5m" else ()
            ),
        )
    trim = len(frame) // 3
    suffix = frame.iloc[trim:].copy().reset_index(drop=True)
    suffix.attrs = dict(frame.attrs)
    suffix_start = _market_datetime(suffix["date"].iloc[0], "warmup suffix start")
    active_tail_start = max(
        suffix_start,
        normalize_datetime(as_of, "as_of") - context_point_max_age(frequency),
    )
    short = analyze_native_frame(
        code=code,
        frequency=frequency,
        frame=suffix,
        as_of=as_of,
        runtime_state=(None if runtime_states is None else runtime_states.suffix),
    )
    converged = _warmup_tail_signature(
        full,
        not_before=active_tail_start,
    ) == _warmup_tail_signature(
        short,
        not_before=active_tail_start,
    )
    difference_codes = (
        ()
        if converged
        else _warmup_tail_difference_codes(
            full,
            short,
            not_before=active_tail_start,
        )
    )
    trade_level_converged: bool | None = None
    trade_level_difference_codes: tuple[str, ...] = ()
    if frequency == "5m":
        trade_level_converged = _warmup_tail_signature(
            full,
            not_before=active_tail_start,
            trade_level_only=True,
        ) == _warmup_tail_signature(
            short,
            not_before=active_tail_start,
            trade_level_only=True,
        )
        if not trade_level_converged:
            trade_level_difference_codes = _warmup_tail_difference_codes(
                full,
                short,
                not_before=active_tail_start,
                trade_level_only=True,
            )
    return replace(
        full,
        warmup_converged=converged,
        warmup_full_bar_count=len(frame),
        warmup_suffix_bar_count=len(suffix),
        warmup_reason_codes=(
            "WARMUP_TAIL_STABLE" if converged else "WARMUP_TAIL_DIVERGED",
        ),
        warmup_difference_codes=difference_codes,
        trade_level_warmup_converged=trade_level_converged,
        trade_level_warmup_reason_codes=(
            ()
            if trade_level_converged is None
            else (
                "WARMUP_TAIL_STABLE"
                if trade_level_converged
                else "WARMUP_TAIL_DIVERGED",
            )
        ),
        trade_level_warmup_difference_codes=trade_level_difference_codes,
    )


def audit_native_frame_warmup_envelope(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
    prefix_ratios: tuple[tuple[int, int], ...] = (_WARMUP_ENVELOPE_PREFIX_RATIOS),
) -> WarmupConvergenceEnvelope:
    """在四种左侧历史长度下审计同一活动尾部。

    此处特意调用 :func:`analyze_native_frame`，而不是生效中的成对预热包装器。结果只
    用于诊断，并携带不可变的 ``active_gate_unchanged`` 标记；调用方不得把结果反馈到
    排名、候选选择或订单资格判断。
    """

    if frequency not in SCREENING_WARMUP_REQUIRED_BARS:
        raise ValueError(f"unsupported warmup frequency: {frequency}")
    if not isinstance(frame, pd.DataFrame) or frame.empty or "date" not in frame:
        raise ValueError("warmup envelope requires a non-empty dated frame")
    ratios = tuple(prefix_ratios)
    if len(ratios) < 3 or any(
        type(numerator) is not int
        or type(denominator) is not int
        or numerator <= 0
        or denominator <= 0
        or numerator > denominator
        for numerator, denominator in ratios
    ):
        raise ValueError("prefix_ratios require at least three valid fractions")
    parameter_set_id = sha256_json(
        {
            "contract": "warmup-common-tail-multi-prefix",
            "frequency": frequency,
            "prefix_ratios": ratios,
            "minimum_prefix_bars": SCREENING_WARMUP_REQUIRED_BARS[frequency],
            "active_gate_unchanged": True,
        }
    )
    bar_counts = tuple(
        sorted(
            {
                len(frame) * numerator // denominator
                for numerator, denominator in ratios
                if len(frame) * numerator // denominator
                >= SCREENING_WARMUP_REQUIRED_BARS[frequency]
            }
        )
    )
    prepared: list[tuple[int, datetime, FrameStructureAnalysis]] = []
    for bar_count in bar_counts:
        suffix = frame.iloc[-bar_count:].copy().reset_index(drop=True)
        suffix.attrs = dict(frame.attrs)
        starts_at = _market_datetime(
            suffix["date"].iloc[0],
            "warmup envelope prefix start",
        )
        prepared.append(
            (
                bar_count,
                starts_at,
                analyze_native_frame(
                    code=code,
                    frequency=frequency,
                    frame=suffix,
                    as_of=as_of,
                ),
            )
        )
    common_tail_start = (
        None
        if not prepared
        else max(
            max(row[1] for row in prepared),
            normalize_datetime(as_of, "as_of") - context_point_max_age(frequency),
        )
    )
    observations = tuple(
        WarmupPrefixObservation(
            bar_count=bar_count,
            starts_at=starts_at,
            signature_sha256=sha256_json(
                _warmup_tail_signature(
                    analysis,
                    not_before=cast(datetime, common_tail_start),
                )
            ),
        )
        for bar_count, starts_at, analysis in prepared
    )
    return classify_warmup_convergence_envelope(
        frequency=frequency,
        as_of=as_of,
        parameter_set_id=parameter_set_id,
        observations=observations,
    )


def _strict_direction(evidence: StrictEvidenceResult) -> ContextDirection:
    return cast(ContextDirection, current_formal_direction(evidence))


def _stock_codes(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TypeError("stock scope provider must return a sequence")
    values: set[str] = set()
    for item in raw:
        code = item.get("code") if isinstance(item, Mapping) else item
        if isinstance(code, str) and _A_STOCK_CODE.fullmatch(code):
            values.add(code)
    return tuple(sorted(values))


def _qmt_catalog_universe(
    rows: Sequence[object],
) -> dict[str, str]:
    """使用已捕获的 QMT GICS 成员作为板块优先选股范围。

    目录构建器已经把原生响应规范化并筛成 A 股身份；再与
    ``ExchangeQMT.all_stocks`` 求交既重复，也会让 ``get_full_tick`` 意外影响扫描范围。
    """

    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        raw_members = row.get("member_codes")
        if isinstance(raw_members, (str, bytes)) or not isinstance(
            raw_members, Sequence
        ):
            continue
        for value in raw_members:
            if isinstance(value, str) and _A_STOCK_CODE.fullmatch(value):
                result.setdefault(value.split(".", 1)[1], value)
    return result


def _catalog_member_count(raw: Sequence[object]) -> int:
    """统计 QMT 目录中唯一且规范的 A 股身份。"""

    identities: set[str] = set()
    for value in raw:
        if isinstance(value, str) and _A_STOCK_CODE.fullmatch(value):
            identities.add(value)
    return len(identities)


class NativeTradingDataGateway:
    """先读取板块事实，再构建个股日线、30m、5m、1m 物理结构。"""

    def __init__(
        self,
        *,
        exchange_provider: Callable[[], object],
        sector_provider: Callable[[], object],
        sector_frame_provider: Callable[..., object] | None = None,
        sector_strength_provider: Callable[..., Mapping[str, SectorStrengthEvidence]]
        | None = None,
        higher_timeframe_provider: Callable[..., HigherTimeframeGateBundle]
        | None = None,
        trading_session_provider: Callable[..., Mapping[str, object]] | None = None,
        instrument_type_provider: Callable[[tuple[str, ...]], Mapping[str, str]]
        | None = None,
        instrument_detail_provider: Callable[[str], object] = (
            _default_qmt_instrument_detail
        ),
        watchlist_provider: Callable[[], object] = lambda: (),
        holdings_provider: Callable[[], object] = lambda: (),
        analyzer: StructureAnalyzer = analyze_native_frame_with_warmup,
        progress_callback: Callable[[], None] = lambda: None,
        config: NativeTradingGatewayConfig = NativeTradingGatewayConfig(),
    ) -> None:
        providers = (
            exchange_provider,
            sector_provider,
            watchlist_provider,
            holdings_provider,
            analyzer,
            progress_callback,
            instrument_detail_provider,
        )
        if any(not callable(provider) for provider in providers):
            raise TypeError("trading gateway providers must be callable")
        if sector_frame_provider is not None and not callable(sector_frame_provider):
            raise TypeError("sector_frame_provider must be callable")
        if sector_strength_provider is not None and not callable(
            sector_strength_provider
        ):
            raise TypeError("sector_strength_provider must be callable")
        if higher_timeframe_provider is not None and not callable(
            higher_timeframe_provider
        ):
            raise TypeError("higher_timeframe_provider must be callable")
        if trading_session_provider is not None and not callable(
            trading_session_provider
        ):
            raise TypeError("trading_session_provider must be callable")
        if instrument_type_provider is not None and not callable(
            instrument_type_provider
        ):
            raise TypeError("instrument_type_provider must be callable")
        self._exchange_provider = exchange_provider
        self._sector_provider = sector_provider
        self._sector_frame_provider = sector_frame_provider
        self._sector_strength_provider = sector_strength_provider
        self._higher_timeframe_provider = higher_timeframe_provider
        self._trading_session_provider = trading_session_provider
        self._instrument_type_provider = instrument_type_provider
        self._instrument_detail_provider = instrument_detail_provider
        self._watchlist_provider = watchlist_provider
        self._holdings_provider = holdings_provider
        self._analyzer = analyzer
        self._progress_callback = progress_callback
        self._config = config
        self._lock = RLock()
        self._members: dict[str, tuple[str, ...]] = {}
        self._symbol_names: dict[str, str] = {}
        # 原生证券类型在同一工作进程和源码版本内稳定；只缓存已明确解析的类型。
        # ``unresolved_cn`` 可能来自瞬时 QMT 故障，必须保留后续重试机会。
        self._instrument_types: dict[str, str] = {}
        self._latest_sector_bars: dict[tuple[str, str], datetime] = {}
        self._emitted_sector_bars: dict[tuple[str, str], datetime] = {}
        self._analysis_cache: OrderedDict[
            tuple[str, str], tuple[str, FrameStructureAnalysis]
        ] = OrderedDict()
        self._analysis_cache_entries_by_frequency: Counter[str] = Counter()
        self._runtime_states_by_frequency: dict[
            str, OrderedDict[str, _WarmupRuntimeStates]
        ] = {
            frequency: OrderedDict()
            for frequency in _RUNTIME_CACHE_CAPACITY_BY_FREQUENCY
        }
        self._disk_runtime_state_cache = (
            _AuthenticatedRuntimeStateDiskCache.from_environment()
        )
        self._serialized_runtime_cache_capacities = dict(
            _SERIALIZED_RUNTIME_CACHE_CAPACITY_BY_FREQUENCY
        )
        self._serialized_runtime_cache_max_bytes = dict(
            _SERIALIZED_RUNTIME_CACHE_MAX_BYTES_BY_FREQUENCY
        )
        # 候选 5m 运行态已经由认证磁盘层完整承接。继续在同一进程里保留相同的
        # 压缩 payload 只会重复占用数百 MiB，并不会增加可恢复覆盖面。磁盘恢复会在
        # 请求行情前预热回 L1，因此仍能使用稳定左边界做增量读取。
        if self._disk_runtime_state_cache is not None:
            self._serialized_runtime_cache_capacities["5m"] = 0
            self._serialized_runtime_cache_max_bytes["5m"] = 0
        self._serialized_runtime_states_by_frequency: dict[
            str, dict[str, _SerializedWarmupRuntimeStates]
        ] = {
            frequency: {}
            for frequency in self._serialized_runtime_cache_capacities
        }
        self._serialized_runtime_state_bytes_by_frequency: Counter[str] = Counter()
        # 批量补数只证明某个精确观察时刻的本地 QMT 基础流可读；结构判断仍走
        # ``_load_analysis`` 的唯一严格入口。本表仅用于跳过重复下载，读取或校验
        # 失败时会立即退回逐只下载，不能把补数成功误当成结构成功。
        self._prepared_local_frequencies: dict[
            tuple[str, str], tuple[str, ...]
        ] = {}
        # 月/周/日事实冻结在盘中监听所用的显式因果截止点。只缓存完全解析的证据包，
        # 避免重复 1m/5m 观测反复采样数百个日级交易日；瞬时 UNRESOLVED 仍可重试。
        self._higher_timeframe_cache: OrderedDict[
            tuple[str, str, str, str, str, str], HigherTimeframeGateBundle
        ] = OrderedDict()
        self._performance_counters: Counter[str] = Counter()
        # name -> (count, total seconds, maximum seconds, last seconds)
        self._performance_timings: dict[str, tuple[int, float, float, float]] = {}

    def _report_progress(self) -> None:
        """在原生调用或高计算量边界前后立即证明进度。

        回调会有意传播失败。隔离工作进程与父进程断连后必须停止后续 QMT 工作，不能
        留下继续消耗原生资源的孤儿进程。
        """

        self._progress_callback()

    def _record_performance_event(self, name: str, count: int = 1) -> None:
        """记录不影响交易语义的进程内累计计数。"""

        with self._lock:
            self._performance_counters[name] += count

    def _record_performance_timing(self, name: str, elapsed: float) -> None:
        """记录阶段耗时；健康接口只读取快照，不触发行情或结构计算。"""

        seconds = max(0.0, float(elapsed))
        with self._lock:
            count, total, maximum, _last = self._performance_timings.get(
                name,
                (0, 0.0, 0.0, 0.0),
            )
            self._performance_timings[name] = (
                count + 1,
                total + seconds,
                max(maximum, seconds),
                seconds,
            )

    @staticmethod
    def _serialized_runtime_admission_rank(code: str) -> int:
        digest = hashlib.sha256(code.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big")

    def _restore_serialized_runtime_states(
        self,
        *,
        code: str,
        frequency: str,
        cached: _SerializedWarmupRuntimeStates,
    ) -> _WarmupRuntimeStates | None:
        """恢复仅由当前进程产生的压缩运行态；任何异常都退回确定性重建。"""

        started = perf_counter()
        try:
            raw = zlib.decompress(cached.payload)
            if len(raw) != cached.raw_size:
                raise ValueError("serialized runtime size changed")
            # 内存载荷由本进程生成；磁盘载荷已在进入这里前通过会话 HMAC 校验。
            states = pickle.loads(raw)  # noqa: S301
            if (
                not isinstance(states, _WarmupRuntimeStates)
                or not isinstance(states.full, ScreeningRuntimeState)
                or not isinstance(states.suffix, ScreeningRuntimeState)
                or any(
                    state.code != code
                    or state.frequency != frequency
                    or state.market != "a"
                    for state in (states.full, states.suffix)
                )
                or states.full.retained_frame_start
                != cached.retained_frame_start
                or states.full.retained_frame_count
                != cached.retained_frame_count
            ):
                raise ValueError("serialized runtime identity changed")
        except Exception:
            self._record_performance_event(
                f"serialized_runtime_restore_failure.{frequency}"
            )
            return None
        finally:
            self._record_performance_timing(
                f"serialized_runtime_restore.{frequency}",
                perf_counter() - started,
            )
        self._record_performance_event(f"serialized_runtime_cache_hit.{frequency}")
        return states

    def _store_serialized_runtime_states(
        self,
        *,
        code: str,
        frequency: str,
        states: _WarmupRuntimeStates,
    ) -> None:
        """把 L1 淘汰态压缩到磁盘层，并按需保留有界的进程内副本。"""

        capacity = self._serialized_runtime_cache_capacities.get(frequency, 0)
        max_bytes = self._serialized_runtime_cache_max_bytes.get(frequency, 0)
        disk_backed = (
            self._disk_runtime_state_cache is not None and frequency == "5m"
        )
        if not disk_backed and (capacity <= 0 or max_bytes <= 0):
            return
        started = perf_counter()
        try:
            states.full.release_evidence_cache()
            states.suffix.release_evidence_cache()
            raw = pickle.dumps(states, protocol=pickle.HIGHEST_PROTOCOL)
            payload = zlib.compress(raw, level=1)
            cached = _SerializedWarmupRuntimeStates(
                payload=payload,
                raw_size=len(raw),
                retained_frame_start=states.full.retained_frame_start,
                retained_frame_count=states.full.retained_frame_count,
                admission_rank=self._serialized_runtime_admission_rank(code),
            )
            if disk_backed:
                assert self._disk_runtime_state_cache is not None
                self._disk_runtime_state_cache.store(
                    code=code,
                    frequency=frequency,
                    cached=cached,
                )
            if capacity <= 0 or max_bytes <= 0:
                self._record_performance_event(
                    f"serialized_runtime_memory_copy_skipped.{frequency}"
                )
                return
            if cached.byte_size > max_bytes:
                self._record_performance_event(
                    f"serialized_runtime_oversized.{frequency}"
                )
                return
            with self._lock:
                cache = self._serialized_runtime_states_by_frequency[frequency]
                previous = cache.pop(code, None)
                if previous is not None:
                    self._serialized_runtime_state_bytes_by_frequency[frequency] -= (
                        previous.byte_size
                    )
                cache[code] = cached
                self._serialized_runtime_state_bytes_by_frequency[frequency] += (
                    cached.byte_size
                )
                while (
                    len(cache) > capacity
                    or self._serialized_runtime_state_bytes_by_frequency[frequency]
                    > max_bytes
                ):
                    rejected_code = max(
                        cache,
                        key=lambda value: (
                            cache[value].admission_rank,
                            value,
                        ),
                    )
                    rejected = cache.pop(rejected_code)
                    self._serialized_runtime_state_bytes_by_frequency[frequency] -= (
                        rejected.byte_size
                    )
                    self._performance_counters[
                        f"serialized_runtime_cache_eviction.{frequency}"
                    ] += 1
            self._record_performance_event(f"serialized_runtime_cache_store.{frequency}")
        except Exception:
            self._record_performance_event(
                f"serialized_runtime_store_failure.{frequency}"
            )
        finally:
            self._record_performance_timing(
                f"serialized_runtime_store.{frequency}",
                perf_counter() - started,
            )

    def _restore_disk_runtime_states(
        self,
        *,
        code: str,
        frequency: str,
    ) -> _WarmupRuntimeStates | None:
        if self._disk_runtime_state_cache is None or frequency != "5m":
            return None
        serialized = self._disk_runtime_state_cache.load(
            code=code,
            frequency=frequency,
        )
        self._record_performance_event(
            f"disk_runtime_cache_{'hit' if serialized is not None else 'miss'}.{frequency}"
        )
        if serialized is None:
            return None
        return self._restore_serialized_runtime_states(
            code=code,
            frequency=frequency,
            cached=serialized,
        )

    def _analyze_frame(
        self,
        *,
        code: str,
        frequency: str,
        frame: pd.DataFrame,
        as_of: datetime,
    ) -> FrameStructureAnalysis:
        """调用唯一分析器，并为生产 1m/5m 通道分别保留有界严格状态。"""

        started = perf_counter()
        if (
            self._analyzer is not analyze_native_frame_with_warmup
            or frequency not in _RUNTIME_CACHE_CAPACITY_BY_FREQUENCY
        ):
            try:
                result = self._analyzer(
                    code=code,
                    frequency=frequency,
                    frame=frame,
                    as_of=as_of,
                )
            except Exception:
                self._record_performance_event(
                    f"structure_analysis_failure.{frequency}"
                )
                raise
            finally:
                self._record_performance_timing(
                    f"structure_analysis.{frequency}",
                    perf_counter() - started,
                )
            self._record_performance_event(
                f"structure_analysis_success.{frequency}"
            )
            return result
        serialized: _SerializedWarmupRuntimeStates | None = None
        with self._lock:
            cache = self._runtime_states_by_frequency[frequency]
            states = cache.pop(code, None)
            runtime_state_l1_hit = states is not None
            if states is None:
                serialized_cache = self._serialized_runtime_states_by_frequency.get(
                    frequency
                )
                serialized = (
                    None if serialized_cache is None else serialized_cache.pop(code, None)
                )
                if serialized is not None:
                    self._serialized_runtime_state_bytes_by_frequency[frequency] -= (
                        serialized.byte_size
                    )
        if states is None and serialized is not None:
            states = self._restore_serialized_runtime_states(
                code=code,
                frequency=frequency,
                cached=serialized,
            )
        if states is None and serialized is None:
            states = self._restore_disk_runtime_states(
                code=code,
                frequency=frequency,
            )
        runtime_state_hit = states is not None
        with self._lock:
            cache = self._runtime_states_by_frequency[frequency]
            if states is None:
                states = _WarmupRuntimeStates(
                    full=ScreeningRuntimeState(code, frequency, market="a"),
                    suffix=ScreeningRuntimeState(code, frequency, market="a"),
                )
            cache[code] = states
            evicted: list[tuple[str, _WarmupRuntimeStates]] = []
            while len(cache) > _RUNTIME_CACHE_CAPACITY_BY_FREQUENCY[frequency]:
                evicted.append(cache.popitem(last=False))
        self._record_performance_event(
            f"runtime_state_l1_cache_{'hit' if runtime_state_l1_hit else 'miss'}.{frequency}"
        )
        self._record_performance_event(
            f"runtime_state_cache_{'hit' if runtime_state_hit else 'miss'}.{frequency}"
        )
        try:
            result = analyze_native_frame_with_warmup(
                code=code,
                frequency=frequency,
                frame=frame,
                as_of=as_of,
                runtime_states=states,
            )
        except Exception:
            self._record_performance_event(
                f"structure_analysis_failure.{frequency}"
            )
            raise
        finally:
            self._record_performance_timing(
                f"structure_analysis.{frequency}",
                perf_counter() - started,
            )
            for evicted_code, evicted_states in evicted:
                self._store_serialized_runtime_states(
                    code=evicted_code,
                    frequency=frequency,
                    states=evicted_states,
                )
        self._record_performance_event(
            f"structure_analysis_success.{frequency}"
        )
        for label, state in (("full", states.full), ("suffix", states.suffix)):
            if state.last_update_incremental is None:
                continue
            mode = "incremental" if state.last_update_incremental else "rebuild"
            self._record_performance_event(
                f"structure_{label}_{mode}.{frequency}"
            )
        return result

    def _stable_incremental_start(
        self,
        *,
        exchange: object,
        code: str,
        frequency: str,
    ) -> datetime | None:
        """Return a bounded generation anchor for a hot QMT runtime state."""

        if getattr(exchange, "supports_stable_incremental_window", False) is not True:
            return None
        extra_bars = _RUNTIME_STABLE_WINDOW_EXTRA_BARS.get(frequency)
        if extra_bars is None:
            return None
        with self._lock:
            cache = self._runtime_states_by_frequency.get(frequency)
            states = None if cache is None else cache.get(code)
            serialized_cache = self._serialized_runtime_states_by_frequency.get(
                frequency
            )
            serialized = (
                None
                if serialized_cache is None
                else serialized_cache.get(code)
            )
        disk_state_available = (
            self._disk_runtime_state_cache is not None
            and self._disk_runtime_state_cache.contains(
                code=code,
                frequency=frequency,
            )
        )
        if states is None and serialized is None and disk_state_available:
            restored = self._restore_disk_runtime_states(
                code=code,
                frequency=frequency,
            )
            if restored is not None:
                evicted: list[tuple[str, _WarmupRuntimeStates]] = []
                with self._lock:
                    cache = self._runtime_states_by_frequency[frequency]
                    existing = cache.get(code)
                    if existing is None:
                        cache[code] = restored
                        states = restored
                        while (
                            len(cache)
                            > _RUNTIME_CACHE_CAPACITY_BY_FREQUENCY[frequency]
                        ):
                            evicted.append(cache.popitem(last=False))
                    else:
                        states = existing
                for evicted_code, evicted_states in evicted:
                    self._store_serialized_runtime_states(
                        code=evicted_code,
                        frequency=frequency,
                        states=evicted_states,
                    )
        retained_count = (
            states.full.retained_frame_count
            if states is not None
            else 0
            if serialized is None
            else serialized.retained_frame_count
        )
        retained_start = (
            states.full.retained_frame_start
            if states is not None
            else None
            if serialized is None
            else serialized.retained_frame_start
        )
        if (
            retained_start is None
            or retained_count <= 0
            or retained_count
            >= self._config.request_bars(frequency) + extra_bars
        ):
            return None
        return normalize_datetime(retained_start, "runtime retained frame start")

    def set_progress_callback(self, callback: Callable[[], None]) -> None:
        if not callable(callback):
            raise TypeError("progress callback must be callable")
        with self._lock:
            self._progress_callback = callback

    def runtime_health_snapshot(self) -> dict[str, object]:
        """返回不触发行情读取的有界缓存遥测。"""

        with self._lock:
            performance_timings = {
                name: {
                    "count": count,
                    "total_seconds": round(total, 6),
                    "average_seconds": round(total / count, 6),
                    "maximum_seconds": round(maximum, 6),
                    "last_seconds": round(last, 6),
                }
                for name, (count, total, maximum, last) in sorted(
                    self._performance_timings.items()
                )
                if count > 0
            }
            result: dict[str, object] = {
                "schema": "chanlun-native-screening-runtime-health",
                "runtime_cache_role": _RUNTIME_CACHE_ROLE,
                "analysis_cache_entries": len(self._analysis_cache),
                "analysis_cache_capacity": _ANALYSIS_CACHE_CAPACITY,
                "analysis_cache_capacities": dict(
                    _ANALYSIS_CACHE_CAPACITY_BY_FREQUENCY
                ),
                "analysis_cache_entries_by_frequency": {
                    frequency: int(
                        self._analysis_cache_entries_by_frequency[frequency]
                    )
                    for frequency in _ANALYSIS_CACHE_CAPACITY_BY_FREQUENCY
                },
                "runtime_state_entries": {
                    frequency: len(cache)
                    for frequency, cache in self._runtime_states_by_frequency.items()
                },
                "runtime_state_capacities": dict(
                    _RUNTIME_CACHE_CAPACITY_BY_FREQUENCY
                ),
                "serialized_runtime_state_entries": {
                    frequency: len(cache)
                    for frequency, cache in (
                        self._serialized_runtime_states_by_frequency.items()
                    )
                },
                "serialized_runtime_state_capacities": dict(
                    self._serialized_runtime_cache_capacities
                ),
                "serialized_runtime_state_bytes": {
                    frequency: int(
                        self._serialized_runtime_state_bytes_by_frequency[frequency]
                    )
                    for frequency in self._serialized_runtime_cache_capacities
                },
                "serialized_runtime_state_max_bytes": dict(
                    self._serialized_runtime_cache_max_bytes
                ),
                "disk_runtime_state_cache": (
                    {
                        "schema": _DISK_RUNTIME_CACHE_SCHEMA,
                        "enabled": False,
                        "authenticated_before_deserialization": True,
                        "web_lifecycle_scoped": True,
                        "application_source_revision_scoped": False,
                        "runtime_state_producer_revision_scoped": False,
                    }
                    if self._disk_runtime_state_cache is None
                    else self._disk_runtime_state_cache.health_snapshot()
                ),
                "higher_timeframe_cache_entries": len(
                    self._higher_timeframe_cache
                ),
                "higher_timeframe_cache_capacity": (
                    _HIGHER_TIMEFRAME_CACHE_CAPACITY
                ),
                "performance": {
                    "counters": dict(sorted(self._performance_counters.items())),
                    "timings": performance_timings,
                },
            }
        provider_owner = getattr(self._higher_timeframe_provider, "__self__", None)
        provider_health = getattr(provider_owner, "cache_health_snapshot", None)
        if callable(provider_health):
            result["higher_timeframe_provider"] = provider_health()
        sector_owner = getattr(self._sector_frame_provider, "__self__", None)
        sector_health = getattr(sector_owner, "cache_health_snapshot", None)
        if callable(sector_health):
            result["sector_frame_provider"] = sector_health()
        return result

    def _load_analysis(
        self,
        *,
        exchange: object | None,
        code: str,
        analysis_code: str,
        frequency: str,
        as_of: datetime,
        sector_source: str | None = None,
        frame_override: object = _FRAME_UNSET,
        skip_download: bool = False,
        fast_incremental_refresh: bool = False,
    ) -> FrameStructureAnalysis:
        request_started = perf_counter()
        if sector_source not in {
            None,
            QMT_GICS3_CATALOG_SOURCE,
            QMT_GICS_HIERARCHY_CATALOG_SOURCE,
        }:
            raise ValueError("unsupported sector source")
        is_sector = sector_source is not None
        loader = getattr(exchange, "klines", None)
        if frame_override is _FRAME_UNSET and not callable(loader):
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_adapter_error",
                    "sector frame source is unavailable",
                )
            raise TypeError("exchange must expose klines")
        if type(skip_download) is not bool:
            raise TypeError("skip_download must be an exact bool")
        if type(fast_incremental_refresh) is not bool:
            raise TypeError("fast_incremental_refresh must be an exact bool")
        if fast_incremental_refresh and (
            frame_override is not _FRAME_UNSET
            or is_sector
            or frequency not in _REALTIME_INCREMENTAL_REFRESH_DAYS
        ):
            raise ValueError("fast incremental refresh is invalid for this source")
        local_only = bool(
            skip_download and frame_override is _FRAME_UNSET and not is_sector
        )
        args: dict[str, object] = {
            "req_counts": self._config.request_bars(frequency),
            "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
        }
        if local_only:
            args["skip_download"] = True
        if fast_incremental_refresh:
            args["incremental_refresh_days"] = _REALTIME_INCREMENTAL_REFRESH_DAYS[
                frequency
            ]
        stable_incremental_start = (
            self._stable_incremental_start(
                exchange=exchange,
                code=analysis_code,
                frequency=frequency,
            )
            if (local_only or fast_incremental_refresh) and exchange is not None
            else None
        )
        if stable_incremental_start is not None:
            # ``req_counts`` would truncate the anchored response back to the
            # latest N rows and recreate the moving-left-boundary problem.
            args.pop("req_counts", None)
            self._record_performance_event(
                f"stable_incremental_window_request.{frequency}"
            )
        frame_acquisition_started = perf_counter()
        if frame_override is _FRAME_UNSET:
            try:
                self._report_progress()
                raw_frame = (
                    loader(
                        code,
                        frequency,
                        start_date=stable_incremental_start.strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                        args=args,
                    )
                    if stable_incremental_start is not None
                    else loader(code, frequency, args=args)
                )
                self._report_progress()
            except SectorAnalysisUnavailable:
                raise
            except Exception as exc:
                if is_sector:
                    raise SectorAnalysisUnavailable(
                        "sector_adapter_error",
                        str(exc),
                    ) from exc
                if not local_only and not fast_incremental_refresh:
                    raise
                retry_args = dict(args)
                retry_args.pop("skip_download", None)
                retry_args.pop("incremental_refresh_days", None)
                retry_args["req_counts"] = self._config.request_bars(frequency)
                if stable_incremental_start is not None:
                    self._record_performance_event(
                        f"stable_incremental_window_fallback.{frequency}"
                    )
                self._report_progress()
                raw_frame = loader(code, frequency, args=retry_args)
                self._report_progress()
        else:
            raw_frame = frame_override
        self._record_performance_timing(
            f"frame_acquisition.{frequency}",
            perf_counter() - frame_acquisition_started,
        )
        validation_started = perf_counter()
        if is_sector:
            if not isinstance(raw_frame, pd.DataFrame):
                raise SectorAnalysisUnavailable(
                    "sector_kline_unavailable",
                    "kline frame is unavailable",
                )
            try:
                expected_attrs = {
                    "price_basis_provider": QMT_GICS3_COMPOSITE_PROVIDER,
                    "price_basis_adjustment": QMT_GICS3_COMPOSITE_ADJUSTMENT,
                    "sector_factor_adjustment_contract_id": (
                        QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
                    ),
                }
                factor_revision = raw_frame.attrs.get("sector_factor_revision")
                if (
                    not isinstance(factor_revision, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", factor_revision) is None
                ):
                    raise ValueError("sector causal factor revision is unavailable")
                if frequency == "30m":
                    expected_attrs.update(
                        {
                            "source_base_frequency": "5m",
                            "derived_frequency": "30m",
                            "sector_thirty_minute_derivation_contract": (
                                QMT_SECTOR_THIRTY_MINUTE_DERIVATION_CONTRACT
                            ),
                        }
                    )
                if any(
                    raw_frame.attrs.get(name) != value
                    for name, value in expected_attrs.items()
                ):
                    raise ValueError("sector price basis attrs are incomplete")
                strict_snapshot_price_metadata(raw_frame)
            except Exception as exc:
                raise SectorAnalysisUnavailable(
                    "sector_price_basis_unavailable",
                    str(exc),
                ) from exc
        fallback_reason_codes: tuple[str, ...] = ()

        def close_stock_frame(value: object) -> pd.DataFrame:
            frame = _closed_frame(
                value,
                not_after=as_of,
                minimum_bars=self._config.minimum_bars(frequency),
            )
            if frequency == "1m" and not is_sector:
                frame = normalize_qmt_opening_events_for_completed_minutes(frame)
                if len(frame) < self._config.minimum_bars("1m"):
                    raise ValueError("kline frame does not meet minimum history")
            return frame

        validation_error: Exception | None = None
        try:
            frame = close_stock_frame(raw_frame)
            if fast_incremental_refresh and len(frame) < (
                SCREENING_WARMUP_REQUIRED_BARS[frequency]
            ):
                raise ValueError(
                    "incremental local history does not meet warmup history"
                )
        except Exception as exc:
            validation_error = exc
        if validation_error is not None and (
            local_only or fast_incremental_refresh
        ):
            # 批量资格或短窗增量都不能代替逐只完整性校验。本地库为空、历史不足
            # 或行情事实无效时，精确回退完整下载路径，再执行同一校验。
            retry_args = dict(args)
            retry_args.pop("skip_download", None)
            retry_args.pop("incremental_refresh_days", None)
            retry_args["req_counts"] = self._config.request_bars(frequency)
            try:
                self._report_progress()
                raw_frame = loader(code, frequency, args=retry_args)
                self._report_progress()
                frame = close_stock_frame(raw_frame)
                validation_error = None
            except Exception as exc:
                validation_error = exc
        if validation_error is not None:
            exc = validation_error
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_kline_unavailable",
                    str(exc),
                ) from exc
            if (
                frequency == "30m"
                and frame_override is _FRAME_UNSET
                and str(exc) == "kline frame contains invalid market facts"
            ):
                try:
                    frame = self._validated_thirty_minute_fallback(
                        exchange=exchange,
                        code=code,
                        as_of=as_of,
                        skip_download=local_only,
                    )
                except Exception as fallback_exc:
                    raise ValueError(
                        "native 30m frame contains invalid market facts; "
                        "validated completed-5m fallback unavailable: "
                        f"{type(fallback_exc).__name__}: {fallback_exc}"
                    ) from fallback_exc
                fallback_reason_codes = (SCREENING_QMT_30M_FALLBACK_REASON_CODE,)
                LogUtil.warning(
                    "[trading_screening.market_data_fallback] "
                    "code="
                    f"{code} frequency=30m "
                    f"reason={SCREENING_QMT_30M_FALLBACK_REASON_CODE}"
                )
            else:
                # ``validation_error`` was captured above and we are no longer
                # executing inside its ``except`` block.  A bare raise here
                # produced ``RuntimeError: No active exception to reraise`` and
                # mislabeled deterministic bad history as an unclassified worker
                # failure.
                raise exc
        try:
            strict_snapshot_price_metadata(frame)
            if sector_source in {
                QMT_GICS3_CATALOG_SOURCE,
                QMT_GICS_HIERARCHY_CATALOG_SOURCE,
            }:
                expected_provider = QMT_GICS3_COMPOSITE_PROVIDER
                expected_adjustment = QMT_GICS3_COMPOSITE_ADJUSTMENT
            else:
                expected_provider = expected_adjustment = None
            if is_sector and (
                frame.attrs.get("price_basis_provider") != expected_provider
                or frame.attrs.get("price_basis_adjustment") != expected_adjustment
                or (
                    sector_source
                    in {
                        QMT_GICS3_CATALOG_SOURCE,
                        QMT_GICS_HIERARCHY_CATALOG_SOURCE,
                    }
                    and (
                        frame.attrs.get("sector_factor_adjustment_contract_id")
                        != QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
                        or re.fullmatch(
                            r"sha256:[0-9a-f]{64}",
                            str(frame.attrs.get("sector_factor_revision")),
                        )
                        is None
                    )
                )
            ):
                raise ValueError("closed sector frame lost price basis attrs")
        except Exception as exc:
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_price_basis_unavailable",
                    str(exc),
                ) from exc
            raise
        closed_at = _market_datetime(frame["date"].iloc[-1], "bar close")
        revision_started = perf_counter()
        frame_content_revision = _frame_content_revision(frame)
        self._record_performance_timing(
            f"frame_revision.{frequency}",
            perf_counter() - revision_started,
        )
        self._record_performance_timing(
            f"frame_validation_and_revision.{frequency}",
            perf_counter() - validation_started,
        )
        cache_key = (analysis_code, frequency)
        with self._lock:
            cached = self._analysis_cache.pop(cache_key, None)
            if cached is not None:
                self._analysis_cache[cache_key] = cached
        if (
            cached is not None
            and cached[0] == frame_content_revision
            and cached[1].closed_at == closed_at
        ):
            self._record_performance_event(f"analysis_cache_hit.{frequency}")
            self._record_performance_timing(
                f"load_analysis_total.{frequency}",
                perf_counter() - request_started,
            )
            return cached[1]
        self._record_performance_event(f"analysis_cache_miss.{frequency}")
        try:
            self._report_progress()
            analysis = self._analyze_frame(
                code=analysis_code,
                frequency=frequency,
                frame=frame,
                as_of=closed_at,
            )
            self._report_progress()
        except StrictStructureAnalysisError as exc:
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_structure_invalid",
                    str(exc),
                ) from exc
            raise
        except Exception as exc:
            if is_sector:
                raise SectorAnalysisUnavailable(
                    "sector_adapter_error",
                    str(exc),
                ) from exc
            raise
        if fallback_reason_codes:
            analysis = replace(
                analysis,
                warmup_reason_codes=tuple(
                    dict.fromkeys(
                        (*analysis.warmup_reason_codes, *fallback_reason_codes)
                    )
                ),
            )
        with self._lock:
            replaced = self._analysis_cache.pop(cache_key, None)
            if replaced is None:
                self._analysis_cache_entries_by_frequency[frequency] += 1
            self._analysis_cache[cache_key] = (
                frame_content_revision,
                analysis,
            )
            capacity = _ANALYSIS_CACHE_CAPACITY_BY_FREQUENCY[frequency]
            while self._analysis_cache_entries_by_frequency[frequency] > capacity:
                stale_key = next(
                    key
                    for key in self._analysis_cache
                    if key[1] == frequency
                )
                self._analysis_cache.pop(stale_key, None)
                self._analysis_cache_entries_by_frequency[frequency] -= 1
        self._record_performance_timing(
            f"load_analysis_total.{frequency}",
            perf_counter() - request_started,
        )
        return analysis

    def _validated_thirty_minute_fallback(
        self,
        *,
        exchange: object | None,
        code: str,
        as_of: datetime,
        skip_download: bool = False,
    ) -> pd.DataFrame:
        """用已完成的 QMT 5m 行情重建一条无效的原生 30m 数据流。

        这不是价格修补：无效原生行情会被完整丢弃。替代开高低收量全部由同源的六根
        已完成 5m 行情确定性聚合，再通过常规因果行情门槛校验；底层行情缺失或无效
        仍属于硬失败。
        """

        loader = getattr(exchange, "klines", None)
        if not callable(loader):
            raise TypeError("exchange must expose klines")
        requested_thirty = self._config.request_bars("30m")
        minimum_thirty = self._config.minimum_bars("30m")
        self._report_progress()
        fallback_args: dict[str, object] = {
            "req_counts": requested_thirty * 6,
            "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
        }
        if skip_download:
            fallback_args["skip_download"] = True
        raw_five = loader(
            code,
            "5m",
            args=fallback_args,
        )
        self._report_progress()
        five = _closed_frame(
            raw_five,
            not_after=as_of,
            minimum_bars=minimum_thirty * 6,
        )
        five.insert(0, "code", code)
        source_attrs = dict(five.attrs)
        rebuilt = convert_stock_kline_frequency(five, "30m")
        rebuilt.attrs = source_attrs
        return _closed_frame(
            rebuilt,
            not_after=as_of,
            minimum_bars=minimum_thirty,
        )

    def prepare_local_history(
        self,
        *,
        frequency_requests: tuple[tuple[str, tuple[str, ...]], ...],
        as_of: datetime,
    ) -> dict[str, object]:
        """按频率组合批量补齐 QMT 本地库，随后仍由严格结构入口逐只校验。

        该方法只优化下载传输，不生成、不缓存任何买卖点结论。批量块失败的标的不会
        获得本地只读资格；逐只结构请求会继续使用原有下载路径。
        """

        observed_at = normalize_datetime(as_of, "as_of")
        if type(frequency_requests) is not tuple:
            raise TypeError("frequency_requests must be an exact tuple")
        normalized: list[tuple[str, tuple[str, ...]]] = []
        for item in frequency_requests:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or _A_STOCK_CODE.fullmatch(item[0]) is None
                or type(item[1]) is not tuple
                or not item[1]
                or len(item[1]) != len(set(item[1]))
                or not set(item[1]).issubset(_FREQUENCIES)
                or item[1]
                != tuple(
                    frequency
                    for frequency in _FREQUENCIES
                    if frequency in item[1]
                )
            ):
                raise ValueError("frequency_requests contains an invalid row")
            normalized.append((item[0], item[1]))
        if tuple(normalized) != tuple(
            sorted(normalized, key=lambda value: value[0])
        ) or len({code for code, _frequencies in normalized}) != len(normalized):
            raise ValueError("frequency_requests must be canonical and unique")
        if not normalized:
            return {
                "schema": "chanlun-screening-local-history-preparation",
                "as_of": observed_at.isoformat(),
                "prepared_frequencies_by_code": {},
                "batch_download_available": False,
            }

        exchange = self._exchange_provider()
        downloader = getattr(exchange, "prewarm_batch_download", None)
        prepared: dict[str, set[str]] = {code: set() for code, _ in normalized}
        if callable(downloader):
            grouped: dict[tuple[str, ...], list[str]] = {}
            for code, frequencies in normalized:
                grouped.setdefault(frequencies, []).append(code)
            for frequencies, codes in sorted(grouped.items()):
                self._report_progress()
                result = downloader(
                    tuple(codes),
                    frequencies,
                    progress_callback=(
                        lambda _base, _done, _total: self._report_progress()
                    ),
                    req_counts_by_frequency={
                        frequency: self._config.request_bars(frequency)
                        for frequency in frequencies
                    },
                )
                self._report_progress()
                if not isinstance(result, Mapping) or (
                    result.get("schema") != "chanlun-qmt-batch-download-result"
                    or result.get("cancelled") is not False
                    or not isinstance(result.get("successful_by_base"), Mapping)
                    or not isinstance(result.get("failed_by_base"), Mapping)
                ):
                    continue
                successful_by_base = result["successful_by_base"]
                failed_by_base = result["failed_by_base"]
                for code in codes:
                    for frequency in frequencies:
                        base = _QMT_DOWNLOAD_BASE_BY_FREQUENCY[frequency]
                        successes = successful_by_base.get(base, ())
                        failures = failed_by_base.get(base, ())
                        if code in successes and code not in failures:
                            prepared[code].add(frequency)

        prepared_document = {
            code: tuple(
                frequency
                for frequency in _FREQUENCIES
                if frequency in prepared[code]
            )
            for code, _frequencies in normalized
        }
        with self._lock:
            self._prepared_local_frequencies = {
                (code, observed_at.isoformat()): frequencies
                for code, frequencies in prepared_document.items()
                if frequencies
            }
        return {
            "schema": "chanlun-screening-local-history-preparation",
            "as_of": observed_at.isoformat(),
            "prepared_frequencies_by_code": prepared_document,
            "batch_download_available": callable(downloader),
        }

    def _has_current_five_minute_setup(
        self,
        analysis: FrameStructureAnalysis,
    ) -> bool:
        return bool(
            current_five_minute_setup_points(
                (
                *analysis.setup_confirmed_points,
                *analysis.provisional_points,
                ),
                as_of=analysis.closed_at,
                max_setup_age_seconds=self._config.current_setup_age_seconds,
            )
        )

    def _has_current_five_minute_buy_setup(
        self,
        analysis: FrameStructureAnalysis,
    ) -> bool:
        """Return whether a buy setup needs higher-period integrity evidence."""

        return any(
            point.side == "buy"
            for point in current_five_minute_setup_points(
                (
                    *analysis.setup_confirmed_points,
                    *analysis.provisional_points,
                ),
                as_of=analysis.closed_at,
                max_setup_age_seconds=self._config.current_setup_age_seconds,
            )
        )

    def _cached_analysis(
        self,
        code: str,
        frequency: str,
    ) -> FrameStructureAnalysis | None:
        with self._lock:
            cache_key = (code, frequency)
            cached = self._analysis_cache.pop(cache_key, None)
            if cached is not None:
                self._analysis_cache[cache_key] = cached
        return None if cached is None else cached[1]

    def native_sector_assessments(
        self,
        *,
        as_of: datetime,
        admitted_codes: tuple[str, ...] | None = None,
    ) -> SectorAssessmentBatch:
        observed_at = normalize_datetime(as_of, "as_of")
        if admitted_codes is not None and (
            type(admitted_codes) is not tuple
            or not admitted_codes
            or len(admitted_codes) != len(set(admitted_codes))
            or any(
                type(code) is not str
                or re.fullmatch(r"^(?:SH|SZ|BJ)\.\d{6}$", code) is None
                for code in admitted_codes
            )
        ):
            raise ValueError("admitted_codes must be a unique non-empty A-share tuple")
        admitted_code_set = (
            None if admitted_codes is None else frozenset(admitted_codes)
        )
        self._report_progress()
        raw = self._sector_provider()
        self._report_progress()
        if not isinstance(raw, Mapping):
            raise TypeError("sector catalog must be a mapping")
        catalog_source = raw.get("source")
        if catalog_source not in {
            QMT_GICS3_CATALOG_SOURCE,
            QMT_GICS_HIERARCHY_CATALOG_SOURCE,
        }:
            raise ValueError("sector catalog must expose QMT GICS3/GICS4 components")
        rows = raw.get("sectors")
        if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
            raise TypeError("sector catalog must expose a sectors sequence")
        hierarchy_catalog = catalog_source == QMT_GICS_HIERARCHY_CATALOG_SOURCE
        catalog_revision = (
            qmt_gics_hierarchy_catalog_revision(raw)
            if hierarchy_catalog
            else qmt_sector_catalog_revision(raw)
        )
        provided_revision = raw.get("catalog_revision")
        if provided_revision is not None and provided_revision != catalog_revision:
            raise ValueError("QMT sector catalog revision does not match its members")
        # 当前 QMT 成分构成时点化选股标的池。
        digits = _qmt_catalog_universe(rows)
        symbol_names: dict[str, str] = {}
        universe_codes = set(digits.values())
        assessments: list[SectorAssessment] = []
        errors: list[SectorAnalysisFailure] = []
        exclusions: list[SectorAnalysisExclusion] = []
        discovered_count = 0
        completed_count = 0
        members_by_sector: dict[str, tuple[str, ...]] = {}
        analysis_members_by_sector: dict[str, tuple[str, ...]] = {}
        latest_bars: dict[tuple[str, str], datetime] = {}
        parent_relations: list[tuple[str, str]] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            sector_id = row.get("sector_id")
            sector_name = row.get("name")
            source_key = row.get("source_key")
            taxonomy_level = row.get("taxonomy_level")
            parent_sector_id = row.get("parent_sector_id")
            raw_members = row.get("member_codes")
            valid_identity = bool(
                (
                    hierarchy_catalog
                    and taxonomy_level in {"GICS3", "GICS4"}
                    and isinstance(sector_id, str)
                    and sector_id.startswith(
                        "qmt-gics3:"
                        if taxonomy_level == "GICS3"
                        else "qmt-gics4:"
                    )
                    and isinstance(source_key, str)
                    and source_key.startswith(cast(str, taxonomy_level))
                )
                or (
                    not hierarchy_catalog
                    and isinstance(sector_id, str)
                    and sector_id.startswith("qmt-gics3:")
                    and isinstance(source_key, str)
                    and source_key.startswith("GICS3")
                )
            )
            if (
                not valid_identity
                or sector_id in seen
                or not isinstance(sector_name, str)
                or not sector_name.strip()
                or isinstance(raw_members, (str, bytes))
                or not isinstance(raw_members, Sequence)
            ):
                continue
            analysis_members = tuple(
                sorted(
                    {
                        (value if value in universe_codes else digits[value])
                        for value in raw_members
                        if isinstance(value, str)
                        and (value in universe_codes or value in digits)
                    }
                )
            )
            if admitted_code_set is not None:
                members = tuple(
                    code for code in analysis_members if code in admitted_code_set
                )
                if not members:
                    continue
            else:
                members = analysis_members
            catalog_member_count = _catalog_member_count(raw_members)
            seen.add(sector_id)
            discovered_count += 1
            members_by_sector[sector_id] = members
            analysis_members_by_sector[sector_id] = analysis_members
            if (
                hierarchy_catalog
                and taxonomy_level == "GICS4"
                and isinstance(parent_sector_id, str)
            ):
                parent_relations.append((sector_id, parent_sector_id))
            if len(analysis_members) < self._config.minimum_sector_members:
                if catalog_member_count == 0:
                    detail_code = "sector_catalog_members_missing"
                elif catalog_member_count < self._config.minimum_sector_members:
                    detail_code = "sector_constituent_count_below_minimum"
                else:
                    detail_code = "sector_universe_member_coverage_insufficient"
                exclusion = SectorAnalysisExclusion(
                    sector_id=sector_id,
                    code=cast(str, source_key),
                    reason_code="sector_member_coverage_insufficient",
                    reason=(
                        f"catalog_members={catalog_member_count}; "
                        f"universe_members={len(analysis_members)}; "
                        f"required={self._config.minimum_sector_members}"
                    ),
                    detail_code=detail_code,
                    catalog_member_count=catalog_member_count,
                    universe_member_count=len(analysis_members),
                    required_member_count=self._config.minimum_sector_members,
                )
                exclusions.append(exclusion)
                assessments.append(
                    SectorAssessment(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        rank_components=(),
                        reason_codes=(exclusion.reason_code, detail_code),
                        strength_member_count=len(analysis_members),
                        strength_reason_codes=(
                            "SECTOR_STRENGTH_MEMBER_COVERAGE_INSUFFICIENT",
                            detail_code.upper(),
                        ),
                    )
                )
                continue
            current_frequency = "unknown"
            try:
                analyses: dict[str, FrameStructureAnalysis] = {}
                for frequency in _SECTOR_FREQUENCIES:
                    current_frequency = frequency
                    if self._sector_frame_provider is None:
                        raise SectorAnalysisUnavailable(
                            "sector_adapter_error",
                            "QMT sector frame provider is unavailable",
                        )
                    provider_frequency = frequency
                    provider_request_bars = self._config.request_bars(frequency)
                    if frequency == "30m":
                        # 原生 30m 成员收益中位数不等同于六段 5m 成员收益中位数的连乘结果。
                        provider_frequency = "5m"
                        provider_request_bars = (
                            self._config.request_bars("30m") * 6 + 47
                        )
                    self._report_progress()
                    provider_started = perf_counter()
                    try:
                        raw_sector_frame = self._sector_frame_provider(
                            sector_id=sector_id,
                            sector_name=sector_name.strip(),
                            members=analysis_members,
                            frequency=provider_frequency,
                            as_of=observed_at,
                            request_bars=provider_request_bars,
                        )
                    finally:
                        self._record_performance_timing(
                            f"sector_frame_provider.{frequency}",
                            perf_counter() - provider_started,
                        )
                    self._report_progress()
                    if frequency == "30m":
                        if not isinstance(raw_sector_frame, pd.DataFrame):
                            raise SectorAnalysisUnavailable(
                                "sector_kline_unavailable",
                                "QMT sector 5m base is unavailable",
                            )
                        raw_sector_frame = derive_qmt_sector_thirty_minute_frame(
                            raw_sector_frame,
                            request_bars=self._config.request_bars("30m"),
                        )
                    analyses[frequency] = self._load_analysis(
                        exchange=None,
                        code=sector_id,
                        analysis_code=sector_id,
                        frequency=frequency,
                        as_of=observed_at,
                        sector_source=cast(str, catalog_source),
                        frame_override=raw_sector_frame,
                    )
                contexts = {
                    frequency: classify_context(
                        frequency=frequency,
                        current_direction=analyses[frequency].direction,
                        points=analyses[frequency].confirmed_points,
                        as_of=analyses[frequency].closed_at,
                    )
                    for frequency in _SECTOR_FREQUENCIES
                }
                context_time = max(analysis.closed_at for analysis in analyses.values())
                one = TimeframeContext(
                    frequency="1m",
                    direction="neutral",
                    disposition="neutral",
                    hard_block=False,
                    dominant_point_id=None,
                    dominant_point_type=None,
                    reason_codes=("stock_one_minute_segment_difference_only",),
                    observed_at=context_time,
                )
                assessments.append(
                    assess_sector(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        market_data_source=(
                            "qmt_gics_hierarchy_component_composite"
                            if hierarchy_catalog
                            else "qmt_gics3_component_composite"
                        ),
                        thirty=contexts["30m"],
                        five=contexts["5m"],
                        one=one,
                        data_complete=True,
                    )
                )
                for frequency, analysis in analyses.items():
                    latest_bars[(sector_id, frequency)] = analysis.closed_at
                completed_count += 1
            except SectorAnalysisUnavailable as exc:
                failure = SectorAnalysisFailure(
                    sector_id=sector_id,
                    code=cast(str, source_key),
                    error_type=exc.code,
                    reason=str(exc),
                )
                errors.append(failure)
                LogUtil.error(
                    "[trading_screening.sector] "
                    f"sector={sector_id} frequency={current_frequency} "
                    "provider=qmt-gics-composite "
                    f"error_type={failure.error_type} reason={failure.reason}"
                )
                assessments.append(
                    SectorAssessment(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        rank_components=(),
                        reason_codes=(failure.error_type,),
                    )
                )
            except Exception as exc:
                failure = SectorAnalysisFailure(
                    sector_id=sector_id,
                    code=cast(str, source_key),
                    error_type="sector_adapter_error",
                    reason=str(exc),
                )
                errors.append(failure)
                LogUtil.error(
                    "[trading_screening.sector] "
                    f"sector={sector_id} frequency={current_frequency} "
                    "provider=qmt-gics-composite "
                    f"error_type={failure.error_type} reason={failure.reason}"
                )
                assessments.append(
                    SectorAssessment(
                        sector_id=sector_id,
                        sector_name=sector_name.strip(),
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        rank_components=(),
                        reason_codes=(failure.error_type,),
                    )
                )
        strength_evidence: SectorStrengthBatch | None = None
        if self._sector_strength_provider is not None:
            try:
                self._report_progress()
                # ``observed_at`` 是工作进程墙钟。午夜后回放可能在周二观察目录，但全部
                # 已完成 30m/5m K 线仍属于周一收盘；这种跨日回放必须继续使用周一截止点，
                # 否则会被误标为周二并被不可变复核边界拒绝。
                #
                # 同一交易日盘后则不同：QMT 的正向停牌事实只能在 15:00 后采集。如果把
                # 15:41 的真实观察时点无条件截断成最后一根 15:00 K 线，停牌事实会永久被
                # 判成“来自未来”，当日成员历史也就永远无法收敛。价格仍严格截止于已完成
                # K 线；这里只保留同日已经实际可见的非价格证据时点。
                strength_market_cutoff = max(
                    latest_bars.values(),
                    default=observed_at,
                )
                strength_decision_time = (
                    observed_at
                    if (
                        strength_market_cutoff.date() == observed_at.date()
                        and strength_market_cutoff.timetz().replace(tzinfo=None)
                        >= time(15, 0)
                    )
                    else strength_market_cutoff
                )
                strength_started = perf_counter()
                try:
                    strengths = self._sector_strength_provider(
                        members_by_sector=analysis_members_by_sector,
                        as_of=strength_decision_time,
                        membership_revision=catalog_revision,
                    )
                finally:
                    self._record_performance_timing(
                        "sector_strength_provider",
                        perf_counter() - strength_started,
                    )
                self._report_progress()
                if not isinstance(strengths, Mapping):
                    raise TypeError("sector strength provider must return a mapping")
                if isinstance(strengths, SectorStrengthBatch):
                    strength_evidence = strengths
                assessments = [
                    replace(
                        assessment,
                        horizontal_strength=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].strength
                        ),
                        horizontal_rank=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].rank
                        ),
                        strength_anchor_session=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].anchor_session
                        ),
                        strength_member_count=(
                            0
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].member_count
                        ),
                        strength_source_revision=(
                            None
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].source_revision
                        ),
                        strength_reason_codes=(
                            ("SECTOR_STRENGTH_RESULT_MISSING",)
                            if strengths.get(assessment.sector_id) is None
                            else strengths[assessment.sector_id].reason_codes
                        ),
                    )
                    for assessment in assessments
                ]
            except Exception as exc:
                LogUtil.error(
                    "[trading_screening.sector_strength] "
                    f"error_type=sector_strength_unavailable reason={str(exc)[:160]}"
                )
                assessments = [
                    replace(
                        assessment,
                        strength_reason_codes=("SECTOR_STRENGTH_PROVIDER_UNAVAILABLE",),
                    )
                    for assessment in assessments
                ]
        if parent_relations:
            unavailable_ids = {
                item.sector_id for item in (*errors, *exclusions)
            }
            assessment_by_id = {
                item.sector_id: item for item in assessments
            }
            child_to_parent = dict(parent_relations)
            gated: list[SectorAssessment] = []
            for assessment in assessments:
                parent_id = child_to_parent.get(assessment.sector_id)
                if parent_id is None or assessment.sector_id in unavailable_ids:
                    gated.append(assessment)
                    continue
                parent = assessment_by_id.get(parent_id)
                if (
                    parent is not None
                    and parent_id not in unavailable_ids
                    and parent.eligible
                    and not parent.hard_block
                ):
                    gated.append(assessment)
                    continue
                reason_code = (
                    "gics3_parent_gate_unavailable"
                    if parent is None or parent_id in unavailable_ids
                    else "gics3_parent_gate_blocked"
                )
                gated.append(
                    replace(
                        assessment,
                        eligible=False,
                        hard_block=True,
                        regime="hostile",
                        reason_codes=tuple(
                            dict.fromkeys((*assessment.reason_codes, reason_code))
                        ),
                    )
                )
            assessments = gated
        with self._lock:
            self._members = members_by_sector
            self._symbol_names = symbol_names
            self._latest_sector_bars = latest_bars
        ordered_errors = tuple(sorted(errors, key=lambda item: item.sector_id))
        ordered_exclusions = tuple(sorted(exclusions, key=lambda item: item.sector_id))
        failure_counts = tuple(
            sorted(Counter(item.error_type for item in ordered_errors).items())
        )
        exclusion_counts = tuple(
            sorted(Counter(item.reason_code for item in ordered_exclusions).items())
        )
        return SectorAssessmentBatch(
            assessments=tuple(sorted(assessments, key=lambda item: item.sector_id)),
            discovered_count=discovered_count,
            completed_count=completed_count,
            failure_counts=failure_counts,
            errors=ordered_errors,
            exclusion_counts=exclusion_counts,
            exclusions=ordered_exclusions,
            catalog_revision=catalog_revision,
            strength_evidence=strength_evidence,
            parent_relations=tuple(sorted(parent_relations)),
        )

    def members(self) -> Mapping[str, tuple[str, ...]]:
        with self._lock:
            return dict(self._members)

    def changed_bars(self, since: datetime | None) -> tuple[BarKey, ...]:
        cutoff = (
            None if since is None else normalize_datetime(since, "changed bars cutoff")
        )
        with self._lock:
            changed = tuple(
                BarKey(code=code, frequency=frequency, closed_at=closed_at)
                for (code, frequency), closed_at in self._latest_sector_bars.items()
                if self._emitted_sector_bars.get((code, frequency)) != closed_at
                and (cutoff is None or closed_at > cutoff)
            )
            for item in changed:
                self._emitted_sector_bars[(item.code, item.frequency)] = item.closed_at
        return tuple(
            sorted(
                changed, key=lambda item: (item.closed_at, item.code, item.frequency)
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
        """只保留 QMT 原生 A 股股票和交易所交易基金。"""

        dispositions = self.screening_instrument_types(codes)
        return tuple(
            code
            for code in dispositions
            if dispositions[code] in _TRADABLE_SCREENING_INSTRUMENT_TYPES
        )

    def screening_instrument_types(
        self,
        codes: tuple[str, ...],
    ) -> Mapping[str, str]:
        """从统一证券目录读取并缓存精确原生类型。"""

        normalized = _stock_codes(codes)
        if not normalized:
            return {}
        with self._lock:
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
        raw = provider(missing)
        if not isinstance(raw, Mapping) or set(raw) != set(missing):
            raise RuntimeError("instrument type catalog result is incomplete")
        if any(
            type(code) is not str
            or type(kind) is not str
            or kind not in _KNOWN_SCREENING_INSTRUMENT_TYPES
            for code, kind in raw.items()
        ):
            raise RuntimeError("instrument type catalog result is invalid")
        resolved = {code: str(raw[code]) for code in missing}

        stable = {
            code: kind for code, kind in resolved.items() if kind != "unresolved_cn"
        }
        if stable:
            with self._lock:
                self._instrument_types.update(stable)
        result.update(resolved)
        return {code: result[code] for code in normalized}

    def tick_probe(self, code: str) -> Mapping[str, object]:
        """用统一实时行情契约探测一个 A 股报价。"""

        if not isinstance(code, str) or _A_STOCK_CODE.fullmatch(code) is None:
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
        """在原生进程内批量读取 A 股 Tick，并转换为纯 Python 值对象。"""

        normalized = normalized_a_share_codes(codes)
        exchange = self._exchange_provider()
        market_open_probe = getattr(exchange, "now_trading", None)
        if not callable(market_open_probe):
            raise TypeError("exchange must expose now_trading")
        market_open = bool(market_open_probe("a"))
        if not market_open:
            return AShareRealtimeQuoteBatch(
                requested_codes=normalized,
                market_open=False,
                quotes=(),
                tick_data_used=False,
            )
        if not normalized:
            return AShareRealtimeQuoteBatch(
                requested_codes=(),
                market_open=True,
                quotes=(),
                tick_data_used=False,
            )
        loader = getattr(exchange, "ticks", None)
        if not callable(loader):
            raise TypeError("exchange must expose ticks")
        self._report_progress()
        values = loader(list(normalized)) or {}
        self._report_progress()
        if not isinstance(values, Mapping):
            raise TypeError("exchange ticks must return a mapping")
        quotes = tuple(
            quote
            for code in normalized
            if (quote := quote_from_exchange_tick(code, values.get(code))) is not None
        )
        return AShareRealtimeQuoteBatch(
            requested_codes=normalized,
            market_open=True,
            quotes=quotes,
            tick_data_used=True,
        )

    def current_session_instrument_statuses(
        self,
        codes: tuple[str, ...],
        *,
        session: date,
    ) -> AShareInstrumentSessionStatusBatch:
        """Read exact QMT same-session suspension facts without account access."""

        normalized = normalized_a_share_codes(codes)
        if type(session) is not date:
            raise TypeError("instrument-status session must be an exact date")
        facts: list[AShareInstrumentSessionStatus] = []
        for code in normalized:
            self._report_progress()
            try:
                detail = self._instrument_detail_provider(
                    f"{code[3:]}.{code[:2]}"
                )
            except Exception:
                continue
            finally:
                self._report_progress()
            if not isinstance(detail, Mapping):
                continue
            try:
                trading_day = datetime.strptime(
                    str(detail.get("TradingDay") or ""),
                    "%Y%m%d",
                ).date()
            except ValueError:
                continue
            status = detail.get("InstrumentStatus")
            raw_is_trading = detail.get("IsTrading")
            name = str(detail.get("InstrumentName") or "").strip()
            if (
                trading_day != session
                or type(status) is not int
                or status < 0
                or (
                    type(raw_is_trading) is not bool
                    and not (
                        type(raw_is_trading) is int
                        and raw_is_trading in {0, 1}
                    )
                )
                or not name
            ):
                continue
            facts.append(
                AShareInstrumentSessionStatus(
                    code=code,
                    trading_day=trading_day,
                    instrument_name=name,
                    instrument_status=status,
                    is_trading=bool(raw_is_trading),
                )
            )
        return AShareInstrumentSessionStatusBatch(
            requested_codes=normalized,
            session=session,
            facts=tuple(facts),
        )

    def display_quote_snapshot(
        self,
        codes: tuple[str, ...],
    ) -> AShareDisplayQuoteBatch:
        """读取页面展示报价；休市也返回 QMT 保存的最近有效快照。"""

        normalized = normalized_a_share_codes(codes)
        exchange = self._exchange_provider()
        market_open_probe = getattr(exchange, "now_trading", None)
        if not callable(market_open_probe):
            raise TypeError("exchange must expose now_trading")
        market_open = bool(market_open_probe("a"))
        if not normalized:
            return AShareDisplayQuoteBatch(
                requested_codes=(),
                market_open=market_open,
                quotes=(),
                tick_data_used=False,
            )
        loader = getattr(exchange, "ticks", None)
        if not callable(loader):
            raise TypeError("exchange must expose ticks")
        self._report_progress()
        values = loader(list(normalized)) or {}
        self._report_progress()
        if not isinstance(values, Mapping):
            raise TypeError("exchange ticks must return a mapping")
        quotes = tuple(
            quote
            for code in normalized
            if (quote := quote_from_exchange_tick(code, values.get(code))) is not None
        )
        return AShareDisplayQuoteBatch(
            requested_codes=normalized,
            market_open=market_open,
            quotes=quotes,
            tick_data_used=True,
        )

    def symbol_name(self, code: str) -> str | None:
        with self._lock:
            cached = self._symbol_names.get(code)
        if cached is not None:
            return cached
        # 三级行业成员关系不含展示名称；仅为实际产生复核记录的代码读取静态标的信息，
        # 绝不使用全量逐笔数据决定名称或标的池成员。
        try:
            provider = getattr(self._exchange_provider(), "stock_info", None)
            if not callable(provider):
                return None
            self._report_progress()
            raw = provider(code)
            self._report_progress()
            name = raw.get("name") if isinstance(raw, Mapping) else None
            if not isinstance(name, str) or not name.strip():
                return None
            normalized = name.strip()
            with self._lock:
                self._symbol_names.setdefault(code, normalized)
            return normalized
        except Exception as exc:
            LogUtil.warning(
                "[trading_screening.symbol_name] "
                f"code={code} error={type(exc).__name__}: {str(exc)[:160]}"
            )
            return None

    def trading_session_evidence(
        self,
        *,
        session: date,
        observed_at: datetime,
    ) -> Mapping[str, object]:
        """通过同一个只读 QMT 边界返回交易日历证据。"""

        provider = self._trading_session_provider
        if provider is None:
            raise RuntimeError("QMT trading session provider is unavailable")
        observed = normalize_datetime(observed_at, "observed_at")
        self._report_progress()
        result = provider(session=session, observed_at=observed)
        self._report_progress()
        if not isinstance(result, Mapping):
            raise TypeError("trading session provider returned an invalid document")
        return dict(result)

    def structure_bundle(
        self,
        code: str,
        *,
        as_of: datetime,
        sector: SectorAssessment,
        sector_members: tuple[str, ...] | None = None,
        frequencies: tuple[str, ...] | None = None,
        higher_timeframe_as_of: datetime | None = None,
        local_history_frequencies: tuple[str, ...] | None = None,
        incremental_refresh_frequencies: tuple[str, ...] | None = None,
        instrument_type: str | None = None,
    ) -> SymbolStructureBundle:
        if _A_STOCK_CODE.fullmatch(code) is None:
            raise ValueError("invalid A-share code")
        resolved_instrument_type = instrument_type
        if resolved_instrument_type is None:
            resolved_instrument_type = "stock_cn"
            if self._instrument_type_provider is not None:
                resolved_instrument_type = self.screening_instrument_types((code,))[code]
        if resolved_instrument_type not in _TRADABLE_SCREENING_INSTRUMENT_TYPES:
            raise ValueError("instrument is outside the trading screening scope")
        selection_path = (
            "ETF_PROXY"
            if resolved_instrument_type == "etf_cn"
            else "INDIVIDUAL_THREE_PROGRAM"
        )
        effective_sector = (
            _etf_proxy_sector_assessment(code)
            if selection_path == "ETF_PROXY"
            else sector
        )
        observed_at = normalize_datetime(as_of, "as_of")
        exchange = self._exchange_provider()
        requested = set(_FREQUENCIES if frequencies is None else frequencies)
        if not requested or not requested.issubset(_FREQUENCIES):
            raise ValueError("frequencies must contain only d, 30m, 5m and 1m")
        if local_history_frequencies is None:
            with self._lock:
                prepared_frequencies = self._prepared_local_frequencies.get(
                    (code, observed_at.isoformat()),
                    (),
                )
        else:
            if (
                type(local_history_frequencies) is not tuple
                or len(local_history_frequencies)
                != len(set(local_history_frequencies))
                or not set(local_history_frequencies).issubset(_FREQUENCIES)
            ):
                raise ValueError("local_history_frequencies are invalid")
            prepared_frequencies = local_history_frequencies
        prepared_frequency_set = set(prepared_frequencies)
        if incremental_refresh_frequencies is None:
            incremental_frequencies: tuple[str, ...] = ()
        elif (
            type(incremental_refresh_frequencies) is not tuple
            or len(incremental_refresh_frequencies)
            != len(set(incremental_refresh_frequencies))
            or not set(incremental_refresh_frequencies).issubset(
                _REALTIME_INCREMENTAL_REFRESH_DAYS
            )
        ):
            raise ValueError("incremental_refresh_frequencies are invalid")
        else:
            incremental_frequencies = incremental_refresh_frequencies
        incremental_frequency_set = set(incremental_frequencies)
        if prepared_frequency_set.intersection(incremental_frequency_set):
            raise ValueError(
                "prepared and incremental refresh frequencies must be disjoint"
            )
        analyses: dict[str, FrameStructureAnalysis] = {}
        # 共享决策核心从“当前 5m 设置”开始，必须先检查这一必要条件。缺失时，30m/日级
        # 背景、1m 段差和月/周/日风险事实都无法生成决策记录，无需读取或计算。
        # 这只是执行剪枝：``_has_current_five_minute_setup`` 与统一决策核心内部的
        # 5m 时效契约一致；返回证据包仍携带精确 5m 证据，由共享核心独立证明结果为空。
        cached_five = self._cached_analysis(code, "5m")
        analyses["5m"] = (
            self._load_analysis(
                exchange=exchange,
                code=code,
                analysis_code=code,
                frequency="5m",
                as_of=observed_at,
                skip_download="5m" in prepared_frequency_set,
                fast_incremental_refresh="5m" in incremental_frequency_set,
            )
            if "5m" in requested or cached_five is None
            else cached_five
        )
        has_current_five_minute_setup = self._has_current_five_minute_setup(
            analyses["5m"]
        )
        has_current_five_minute_buy_setup = (
            self._has_current_five_minute_buy_setup(analyses["5m"])
        )
        if has_current_five_minute_setup:
            for frequency in ("d", "30m"):
                cached = self._cached_analysis(code, frequency)
                analyses[frequency] = (
                    self._load_analysis(
                        exchange=exchange,
                        code=code,
                        analysis_code=code,
                        frequency=frequency,
                        as_of=observed_at,
                        skip_download=frequency in prepared_frequency_set,
                        fast_incremental_refresh=(
                            frequency in incremental_frequency_set
                        ),
                    )
                    if frequency in requested or cached is None
                    else cached
                )
            if "1m" in requested:
                analyses["1m"] = self._load_analysis(
                    exchange=exchange,
                    code=code,
                    analysis_code=code,
                    frequency="1m",
                    as_of=observed_at,
                    skip_download="1m" in prepared_frequency_set,
                    fast_incremental_refresh="1m" in incremental_frequency_set,
                )
        bundle_as_of = max(item.closed_at for item in analyses.values())
        entry_execution_boundaries: tuple[EntryExecutionBoundary, ...] = ()
        if "1m" in analyses:
            boundary_pairs = _new_entry_boundary_pairs(
                setup_points=analyses["5m"].setup_confirmed_points,
                witness_points=(
                    analyses["1m"].effective_segment_difference_points
                ),
                decision_at=bundle_as_of,
            )
            if boundary_pairs:
                # Structure remains on its frozen adjustment basis.  Only a
                # newly formed exact setup/witness pair authorizes one local,
                # unadjusted 1m read for the jointly-known decision bar.
                try:
                    raw_loader = getattr(exchange, "klines", None)
                    if not callable(raw_loader):
                        raise TypeError("exchange must expose klines")
                    self._report_progress()
                    raw_confirmation_frame = raw_loader(
                        code,
                        "1m",
                        args={
                            "req_counts": self._config.request_bars("1m"),
                            "dividend_type": "none",
                            "skip_download": True,
                        },
                    )
                    self._report_progress()
                    raw_confirmation_frame = _closed_frame(
                        raw_confirmation_frame,
                        not_after=bundle_as_of,
                        minimum_bars=self._config.minimum_bars("1m"),
                    )
                    raw_confirmation_frame = (
                        normalize_qmt_opening_events_for_completed_minutes(
                            raw_confirmation_frame
                        )
                    )
                    if len(raw_confirmation_frame) < self._config.minimum_bars(
                        "1m"
                    ):
                        raise ValueError("kline frame does not meet minimum history")
                    entry_execution_boundaries = _entry_execution_boundaries(
                        code=code,
                        pairs=boundary_pairs,
                        decision_at=bundle_as_of,
                        raw_frame=raw_confirmation_frame,
                    )
                except Exception as exc:
                    # Missing raw execution evidence is fail-closed; the
                    # structural signal remains available for human review.
                    LogUtil.warning(
                        "[trading_screening.entry_execution_boundary] "
                        f"code={code} reason={type(exc).__name__}: {str(exc)[:160]}"
                    )
        # 低级别 1m 精细通道可合理晚于最新已完成板块 5m K 线（如 09:47 对 09:45）。
        # 信号保留在该已完成 1m 前缀上，而全部月/周/日风险事实冻结到页面统一行情截止点。
        # 若复用信号墙钟，会让收敛、对账和来源覆盖证据描述 09:47，尽管原子板块快照
        # 冻结于 09:45，导致文档写入后立即无法通过自身因果校验。
        risk_as_of = bundle_as_of
        if higher_timeframe_as_of is not None:
            requested_risk_as_of = normalize_datetime(
                higher_timeframe_as_of,
                "higher_timeframe_as_of",
            )
            decision_as_of = normalize_datetime(as_of, "as_of")
            if requested_risk_as_of > decision_as_of:
                raise ValueError(
                    "higher_timeframe_as_of cannot be after decision as_of"
                )
            risk_as_of = min(bundle_as_of, requested_risk_as_of)
        higher_timeframe_gates = None
        # 决策核心为每个当前 5m 设置输出一个结果。已完成 5m 前缀没有当前设置时，
        # 没有设置时输出可证明为空元组，因此避免为该空分支读取并重采样
        # 约 300 个交易日的 QMT 1m 历史。这仅是执行剪枝；所有可能产生候选的标的仍使用
        # 完全相同的高周期提供器，且下方入场闸门继续关闭失败。
        if (
            self._higher_timeframe_provider is not None
            and has_current_five_minute_buy_setup
        ):
            resolved_sector_members = (
                None
                if selection_path == "ETF_PROXY"
                else (
                    self._members.get(effective_sector.sector_id)
                    if sector_members is None
                    else sector_members
                )
            )
            higher_timeframe_cache_key = (
                code,
                risk_as_of.isoformat(),
                effective_sector.sector_id,
                effective_sector.sector_name,
                selection_path,
                sha256_json(
                    {
                        "sector_members": list(resolved_sector_members or ()),
                    }
                ),
            )
            with self._lock:
                higher_timeframe_gates = self._higher_timeframe_cache.pop(
                    higher_timeframe_cache_key,
                    None,
                )
                if higher_timeframe_gates is not None:
                    self._higher_timeframe_cache[higher_timeframe_cache_key] = (
                        higher_timeframe_gates
                    )
            self._record_performance_event(
                "higher_timeframe_cache_hit"
                if higher_timeframe_gates is not None
                else "higher_timeframe_cache_miss"
            )
            try:
                if higher_timeframe_gates is None:
                    higher_timeframe_started = perf_counter()
                    try:
                        self._report_progress()
                        higher_timeframe_gates = self._higher_timeframe_provider(
                            symbol=code,
                            as_of=risk_as_of,
                            sector_id=effective_sector.sector_id,
                            sector_name=(
                                None
                                if selection_path == "ETF_PROXY"
                                else effective_sector.sector_name
                            ),
                            sector_members=resolved_sector_members,
                        )
                        self._report_progress()
                    finally:
                        self._record_performance_timing(
                            "higher_timeframe_provider",
                            perf_counter() - higher_timeframe_started,
                        )
                if not isinstance(
                    higher_timeframe_gates,
                    HigherTimeframeGateBundle,
                ):
                    raise TypeError(
                        "higher timeframe provider returned an invalid bundle"
                    )
                if all(
                    evidence.gate != "UNRESOLVED"
                    for evidence in (
                        higher_timeframe_gates.market,
                        *(
                            (higher_timeframe_gates.sector,)
                            if selection_path
                            == "INDIVIDUAL_THREE_PROGRAM"
                            else ()
                        ),
                        higher_timeframe_gates.symbol,
                    )
                ):
                    with self._lock:
                        self._higher_timeframe_cache.pop(
                            higher_timeframe_cache_key,
                            None,
                        )
                        self._higher_timeframe_cache[higher_timeframe_cache_key] = (
                            higher_timeframe_gates
                        )
                        while (
                            len(self._higher_timeframe_cache)
                            > _HIGHER_TIMEFRAME_CACHE_CAPACITY
                        ):
                            self._higher_timeframe_cache.popitem(last=False)
            except HigherTimeframeDataUnavailable as exc:
                LogUtil.error(
                    "[trading_screening.higher_timeframe.data] "
                    f"code={code} reason_codes={','.join(exc.reason_codes)}"
                )
                higher_timeframe_gates = unresolved_higher_timeframe_gates(
                    symbol=code,
                    observed_at=risk_as_of,
                    reason_codes=exc.reason_codes,
                    session_evidence=HigherTimeframeSessionEvidence.exact(
                        exc.session_issues
                    ),
                    sector_subject=effective_sector.sector_id,
                )
            except Exception as exc:
                LogUtil.error(
                    "[trading_screening.higher_timeframe] "
                    f"code={code} reason={str(exc)[:160]}"
                )
                higher_timeframe_gates = unresolved_higher_timeframe_gates(
                    symbol=code,
                    observed_at=risk_as_of,
                    reason_code="QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE",
                    sector_subject=effective_sector.sector_id,
                )
        elif (
            has_current_five_minute_setup
            and not has_current_five_minute_buy_setup
        ):
            # 高周期方向已不再授权买入；这里只为新买入核验同源、日历和预热完整性。
            # 纯卖出结构仍保留日线/30m环境与全部退出证据，无需重建该买入专用审计包。
            # M/W/D is an entry-only gate, so a sell-only structure must not
            # invoke the expensive provider.  It still needs an explicit,
            # schema-complete unresolved bundle so downstream review never
            # mistakes an intentional skip for missing evidence.
            higher_timeframe_gates = unresolved_higher_timeframe_gates(
                symbol=code,
                observed_at=risk_as_of,
                reason_code=(
                    "HIGHER_TIMEFRAME_ENTRY_GATE_NOT_APPLICABLE_TO_SELL_ONLY"
                ),
                sector_subject=effective_sector.sector_id,
            )
            self._record_performance_event("higher_timeframe_skipped.sell_only")
        confirmed = tuple(
            point
            for analysis in analyses.values()
            for point in analysis.confirmed_points
        )

        def decision_warmup_converged(frequency: str) -> bool:
            analysis = analyses[frequency]
            if (
                frequency == "5m"
                and analysis.trade_level_warmup_converged is not None
            ):
                return analysis.trade_level_warmup_converged
            return analysis.warmup_converged

        def decision_warmup_reasons(frequency: str) -> tuple[str, ...]:
            analysis = analyses[frequency]
            if (
                frequency == "5m"
                and analysis.trade_level_warmup_converged is not None
            ):
                return analysis.trade_level_warmup_reason_codes
            return analysis.warmup_reason_codes

        def decision_warmup_difference_codes(
            frequency: str,
        ) -> tuple[str, ...]:
            analysis = analyses[frequency]
            if (
                frequency == "5m"
                and analysis.trade_level_warmup_converged is not None
            ):
                return analysis.trade_level_warmup_difference_codes
            return analysis.warmup_difference_codes

        warmup_by_frequency = tuple(
            (
                frequency,
                decision_warmup_converged(frequency),
                analyses[frequency].warmup_full_bar_count,
                analyses[frequency].warmup_suffix_bar_count,
            )
            for frequency in ("d", "30m", "5m", "1m")
            if frequency in analyses
        )
        warmup_reasons = tuple(
            dict.fromkeys(
                f"{frequency.upper()}:{reason}"
                for frequency in ("d", "30m", "5m", "1m")
                if frequency in analyses
                for reason in decision_warmup_reasons(frequency)
            )
        )
        warmup_difference_codes_by_frequency = tuple(
            (
                frequency,
                decision_warmup_difference_codes(frequency),
            )
            for frequency in ("d", "30m", "5m", "1m")
            if frequency in analyses
        )
        return SymbolStructureBundle(
            code=code,
            as_of=bundle_as_of,
            sector=effective_sector,
            daily_direction=(
                "neutral" if "d" not in analyses else analyses["d"].direction
            ),
            daily_points=(
                () if "d" not in analyses else analyses["d"].confirmed_points
            ),
            thirty_direction=(
                "neutral" if "30m" not in analyses else analyses["30m"].direction
            ),
            thirty_points=(
                () if "30m" not in analyses else analyses["30m"].confirmed_points
            ),
            five_points=(
                *(
                    point
                    for point in analyses["5m"].setup_confirmed_points
                    if is_five_minute_trade_level(
                        point.source_frequency,
                        point.recursive_level,
                    )
                ),
                *(
                    point
                    for point in analyses["5m"].provisional_points
                    if is_five_minute_trade_level(
                        point.source_frequency,
                        point.recursive_level,
                    )
                ),
            ),
            one_points=(
                ()
                if "1m" not in analyses
                else analyses["1m"].effective_segment_difference_points
            ),
            opposite_points=confirmed,
            higher_timeframe_gates=higher_timeframe_gates,
            enforce_higher_timeframe_entry_gate=(
                self._higher_timeframe_provider is not None
            ),
            warmup_converged=all(
                decision_warmup_converged(frequency)
                for frequency in analyses
            ),
            warmup_reason_codes=warmup_reasons,
            warmup_by_frequency=warmup_by_frequency,
            analysis_closed_at_by_frequency=tuple(
                (frequency, analyses[frequency].closed_at)
                for frequency in ("d", "30m", "5m", "1m")
                if frequency in analyses
            ),
            warmup_difference_codes_by_frequency=(
                warmup_difference_codes_by_frequency
            ),
            enforce_warmup_entry_gate=True,
            physical_timeframe_recursive=True,
            entry_execution_boundaries=entry_execution_boundaries,
            selection_path=selection_path,
            latest_price=max(
                analyses.values(),
                key=lambda analysis: analysis.closed_at,
            ).latest_price,
            daily_technical_context=(
                None
                if "d" not in analyses
                else analyses["d"].same_period_technical_context
            ),
            thirty_technical_context=(
                None
                if "30m" not in analyses
                else analyses["30m"].same_period_technical_context
            ),
        )


__all__ = (
    "CANONICAL_REQUEST_BARS_BY_FREQUENCY",
    "CachedSectorSnapshot",
    "FrameStructureAnalysis",
    "NativeTradingDataGateway",
    "NativeTradingGatewayConfig",
    "SectorAnalysisExclusion",
    "SectorAnalysisFailure",
    "SectorAnalysisUnavailable",
    "SectorAssessmentBatch",
    "StrictStructureAnalysisError",
    "_sector_failure_document",
    "_sector_exclusion_document",
    "analyze_native_frame",
    "analyze_native_frame_with_warmup",
    "audit_native_frame_warmup_envelope",
)
