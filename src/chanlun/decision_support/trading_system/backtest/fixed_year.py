"""固定策略年度 QMT 回放使用的稀疏因果事实。

回放只在进出场结论实际可能改变的时刻评估固定生产策略：

* 已确认的五分钟正式点在下一根可见一分钟柱开始接受成交观察；
* 一分钟买卖点只记录段差证据，不创建或阻断五分钟正式信号；
* 设置仍有效时，后续三十分钟收盘改变了环境闸门。

已确认点写入只追加事件账本。全历史严格快照并不等同于该账本：后续线段递归可以合法
替换实时尾部，并移除早期前缀曾经可见的点。因此，下方回放在线段首次可由锁定笔观察
时将其冻结，并记录每个点首次可见的时刻。非永久冻结的三十分钟方向仍在各稀疏评估
时点按前缀重新回放。
"""

from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
import math
import time as wall_time
from typing import Iterable, Mapping, Sequence, cast
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.current_events import (
    current_strict_point_evidence,
    terminal_segment_reference,
)
from chanlun.core.strict_structure.evidence_assembler import (
    StrictEvidenceAssembler,
)
from chanlun.core.strict_structure.formal_state import (
    current_formal_direction_from_components,
)
from chanlun.core.strict_structure.level_catalog import recursive_level_labels
from chanlun.core.strict_structure.models import (
    ConstituentUnit,
    SourceKind,
    TrendType,
)
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from chanlun.core.strict_structure.strength import MacdStrengthProvider
from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry, adapt_lines
from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.core.xd_calculator import XdCalculator
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_optional_entry_valid_until,
)
from chanlun.decision_support.trading_system.backtest.models import MinuteBar
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    QmtFactorAt,
    SecurityMasterRecord,
    SectorMembershipChange,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    QMTLocalKlineAudit,
    derive_completed_30m_with_audit,
    read_qmt_local_derived_30m,
    read_qmt_local_kline,
    resolve_qmt_local_data_dir,
)
from chanlun.decision_support.trading_system.context import classify_context
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.lifecycle import (
    five_minute_segment_difference_window_start,
    five_minute_setup_expires_at,
    five_minute_setup_family_lane,
    five_minute_setup_is_in_policy_scope,
    is_one_minute_segment_difference,
)
from chanlun.decision_support.trading_system.models import (
    ContextDirection,
    EntryExecutionBoundary,
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    SectorAssessment,
    StructuralPoint,
    StructureTower,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.runtime_config import (
    strict_cl_config,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    convert_confirmed_point_evidence,
    structural_point_id_map,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.decision_support.trading_system.selection import (
    SelectionResearchSnapshot,
    visible_selection_research,
)
from chanlun.exchange.kline_precision import (
    normalize_kline_precision,
    resolve_structure_price_quantum,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
    copy_price_basis_metadata,
)


CN = ZoneInfo("Asia/Shanghai")
FREQUENCIES = ("30m", "5m", "1m")
FRAME_COLUMNS = (
    "code",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "raw_open",
    "raw_high",
    "raw_low",
    "raw_close",
)
BASE_FRAME_COLUMNS = FRAME_COLUMNS[:7]
FACT_SCHEMA = "chanlun-fixed-year-symbol-facts"
SECTOR_FACT_SCHEMA = "chanlun-fixed-year-sector-facts"


@dataclass(frozen=True, slots=True)
class SparseEvaluationFact:
    observed_at: datetime
    thirty_direction: ContextDirection
    bar: MinuteBar
    sector_id: str | None = None

    def __post_init__(self) -> None:
        observed = normalize_datetime(self.observed_at, "observed_at")
        if self.bar.closed_at != observed:
            raise ValueError("evaluation bar must close at observed_at")
        object.__setattr__(self, "observed_at", observed)


@dataclass(frozen=True, slots=True)
class SymbolResearchFacts:
    schema: str
    algorithm_revision: str
    source_revision: str
    code: str
    sector_id: str
    requested_start: date
    requested_end: date
    effective_start: date
    row_counts: tuple[tuple[str, int], ...]
    thirty_points: tuple[StructuralPoint, ...]
    five_points: tuple[StructuralPoint, ...]
    one_points: tuple[StructuralPoint, ...]
    evaluations: tuple[SparseEvaluationFact, ...]
    direction_unavailable_count: int = 0
    security_master: SecurityMasterRecord | None = None
    memberships: tuple[SectorMembershipChange, ...] = ()
    factors: tuple[QmtFactorAt, ...] = ()

    def __post_init__(self) -> None:
        if self.schema != FACT_SCHEMA:
            raise ValueError("unsupported fixed-year fact schema")
        for value, label in (
            (self.algorithm_revision, "algorithm_revision"),
            (self.source_revision, "source_revision"),
        ):
            if not (
                isinstance(value, str)
                and value.startswith("sha256:")
                and len(value) == 71
                and all(char in "0123456789abcdef" for char in value[7:])
            ):
                raise ValueError(f"{label} must be a sha256 fingerprint")
        if not self.code or not self.sector_id:
            raise ValueError("symbol and sector identity are required")
        if not self.requested_start <= self.effective_start <= self.requested_end:
            raise ValueError("invalid fixed-year fact range")
        names = tuple(name for name, _count in self.row_counts)
        if names != FREQUENCIES or any(count < 0 for _name, count in self.row_counts):
            raise ValueError("row counts must cover 30m, 5m and 1m")
        times = tuple(row.observed_at for row in self.evaluations)
        if times != tuple(sorted(set(times))):
            raise ValueError("evaluation facts must be unique and chronological")
        if self.direction_unavailable_count < 0:
            raise ValueError("direction unavailable count cannot be negative")
        if self.security_master is not None and self.security_master.code != self.code:
            raise ValueError("security master does not match symbol facts")
        if any(row.code != self.code for row in self.memberships):
            raise ValueError("sector membership does not match symbol facts")
        if any(row.code != self.code for row in self.factors):
            raise ValueError("QMT factor does not match symbol facts")
        membership_order = tuple(
            (row.known_at, row.sector_id) for row in self.memberships
        )
        if membership_order != tuple(sorted(set(membership_order))):
            raise ValueError("symbol memberships must be unique and chronological")
        factor_dates = tuple(row.effective_on for row in self.factors)
        if factor_dates != tuple(sorted(set(factor_dates))):
            raise ValueError("symbol factors must be unique and chronological")


@dataclass(frozen=True, slots=True)
class SectorResearchFacts:
    schema: str
    algorithm_revision: str
    source_revision: str
    sector_id: str
    sector_name: str
    member_count: int
    row_count: int
    thirty_points: tuple[StructuralPoint, ...]
    assessments: tuple[tuple[datetime, SectorAssessment], ...]
    direction_unavailable_count: int = 0
    error: str | None = None

    def __post_init__(self) -> None:
        if self.schema != SECTOR_FACT_SCHEMA:
            raise ValueError("unsupported fixed-year sector fact schema")
        for value, label in (
            (self.algorithm_revision, "algorithm_revision"),
            (self.source_revision, "source_revision"),
        ):
            if not (
                isinstance(value, str)
                and value.startswith("sha256:")
                and len(value) == 71
                and all(char in "0123456789abcdef" for char in value[7:])
            ):
                raise ValueError(f"{label} must be a sha256 fingerprint")
        if not self.sector_id or not self.sector_name or self.member_count < 0:
            raise ValueError("invalid sector identity")
        if self.row_count < 0 or self.direction_unavailable_count < 0:
            raise ValueError("invalid sector coverage counts")
        times = tuple(observed_at for observed_at, _row in self.assessments)
        if times != tuple(sorted(set(times))):
            raise ValueError("sector assessments must be unique and chronological")


def qmt_native_code(code: str) -> str:
    market, digits = code.split(".", 1)
    if market not in {"SH", "SZ", "BJ"} or len(digits) != 6 or not digits.isdigit():
        raise ValueError(f"invalid normalized A-share code: {code!r}")
    return f"{digits}.{market}"


def _empty_frame(code: str) -> pd.DataFrame:
    return pd.DataFrame(columns=FRAME_COLUMNS).assign(code=code)


def qmt_factor_frame(factors: Sequence[QmtFactorAt]) -> pd.DataFrame:
    return pd.DataFrame(
        (
            {
                "effective_on": row.effective_on,
                "interest": str(row.interest),
                "stockBonus": str(row.stock_bonus),
                "stockGift": str(row.stock_gift),
                "allotNum": str(row.allot_num),
                "allotPrice": str(row.allot_price),
                "gugai": str(row.gugai),
                "dr": str(row.raw_price_divisor),
            }
            for row in factors
        ),
        columns=(
            "effective_on",
            "interest",
            "stockBonus",
            "stockGift",
            "allotNum",
            "allotPrice",
            "gugai",
            "dr",
        ),
    )


def load_qmt_frame(
    code: str,
    frequency: str,
    *,
    start_at: datetime,
    end_at: datetime,
    factors: pd.DataFrame | None = None,
    _allow_native_daily: bool = False,
    _local_five_snapshot: tuple[pd.DataFrame, QMTLocalKlineAudit] | None = None,
) -> pd.DataFrame:
    """读取原始 QMT 行，并只用除权日当时已知信息构建因果分析价格基准。"""

    if frequency not in FREQUENCIES and not (_allow_native_daily and frequency == "1d"):
        raise ValueError("frequency must be 30m, 5m or 1m")
    start = normalize_datetime(start_at, "start_at")
    end = normalize_datetime(end_at, "end_at")
    if start > end:
        raise ValueError("start_at cannot follow end_at")
    native = qmt_native_code(code)
    fields = ("time", "open", "high", "low", "close", "volume")
    frame = pd.DataFrame()
    provider_error: Exception | None = None
    local_directory = resolve_qmt_local_data_dir()
    if _local_five_snapshot is not None:
        if frequency not in {"30m", "5m"}:
            raise ValueError("a shared 5m snapshot only supports 5m or derived 30m")
        source_frame, source_audit = _local_five_snapshot
        if source_audit.code != code or source_audit.frequency != "5m":
            raise ValueError("shared QMT 5m snapshot identity does not match request")
        if frequency == "30m":
            frame, local_audit = derive_completed_30m_with_audit(
                source_frame,
                source_audit,
            )
        else:
            frame = source_frame.copy()
            frame.attrs = dict(source_frame.attrs)
            local_audit = source_audit
        if not frame.empty:
            frame.attrs.update(
                qmt_transport=(
                    "LOCAL_5M_DERIVED_30M_READ_ONLY"
                    if frequency == "30m"
                    else "LOCAL_FIXED_RECORD_READ_ONLY"
                ),
                qmt_local_cache_audit_id=local_audit.audit_id,
                qmt_local_cache_source_sha256=local_audit.source_sha256,
            )
    elif local_directory is not None:
        reader = (
            read_qmt_local_derived_30m if frequency == "30m" else read_qmt_local_kline
        )
        reader_arguments: dict[str, object] = {
            "data_dir": local_directory,
            "code": code,
            "start_at": start,
            "end_at": end,
        }
        if frequency != "30m":
            reader_arguments["frequency"] = frequency
        frame, local_audit = reader(**reader_arguments)  # type: ignore[arg-type]
        if not frame.empty:
            frame.attrs.update(
                qmt_transport=(
                    "LOCAL_5M_DERIVED_30M_READ_ONLY"
                    if frequency == "30m"
                    else "LOCAL_FIXED_RECORD_READ_ONLY"
                ),
                qmt_local_cache_audit_id=local_audit.audit_id,
                qmt_local_cache_source_sha256=local_audit.source_sha256,
            )
    else:
        try:
            from xtquant import xtdata

            xtdata.enable_hello = False
            for attempt in range(3):
                try:
                    raw = xtdata.get_market_data(
                        field_list=list(fields),
                        stock_list=[native],
                        period=frequency,
                        start_time=start.strftime("%Y%m%d%H%M%S"),
                        end_time=end.strftime("%Y%m%d%H%M%S"),
                        count=-1,
                        dividend_type="none",
                        fill_data=False,
                    )
                except Exception as exc:  # 数据提供方可能在遍历标的范围期间中断
                    provider_error = exc
                    raw = {}
                columns: dict[str, pd.Series] = {}
                if isinstance(raw, Mapping):
                    for field in fields:
                        matrix = raw.get(field)
                        if (
                            not isinstance(matrix, pd.DataFrame)
                            or native not in matrix.index
                        ):
                            columns = {}
                            break
                        columns[field] = matrix.loc[native]
                if columns:
                    candidate = pd.DataFrame(columns)
                    for field in fields:
                        candidate[field] = pd.to_numeric(
                            candidate[field], errors="coerce"
                        )
                    candidate = candidate.dropna(subset=list(fields))
                    candidate = candidate[
                        (candidate["time"] > 0)
                        & (candidate["open"] > 0)
                        & (candidate["high"] > 0)
                        & (candidate["low"] > 0)
                        & (candidate["close"] > 0)
                        & (candidate["volume"] >= 0)
                    ].copy()
                    if not candidate.empty:
                        frame = candidate
                        frame.attrs["qmt_transport"] = "RPC"
                        break
                if attempt < 2:
                    wall_time.sleep(0.05 * (attempt + 1))
        except (ImportError, ModuleNotFoundError) as exc:
            provider_error = exc
    transport_metadata = dict(frame.attrs)
    if frame.empty:
        if provider_error is not None and local_directory is None:
            raise RuntimeError(
                "QMT RPC is unavailable and CHANLUN_QMT_LOCAL_DATA_DIR is not set"
            ) from provider_error
        return _empty_frame(code)
    frame["date"] = pd.to_datetime(
        frame.pop("time"), unit="ms", utc=True
    ).dt.tz_convert(CN)
    if frequency == "1d":
        # 行情终端 QMT 的固定日线记录标记在午夜；日级事实直到 A 股收盘才可知，
        # 在 15:00 暴露，而不是在该交易日开始时暴露。
        frame["date"] = frame["date"].dt.normalize() + pd.Timedelta(hours=15)
    frame.insert(0, "code", code)
    frame = frame.loc[:, list(BASE_FRAME_COLUMNS)]
    frame = frame.sort_values("date", kind="stable").drop_duplicates(
        "date", keep="last"
    )
    frame = frame.reset_index(drop=True)
    normalized = normalize_kline_precision(frame, "a", code)
    if normalized is None:
        raise ValueError("QMT precision normalization returned no frame")
    for field in ("open", "high", "low", "close"):
        normalized[f"raw_{field}"] = normalized[field]
    multiplier = pd.Series(1.0, index=normalized.index, dtype="float64")
    if factors is not None and not factors.empty:
        required = {"effective_on", "dr"}
        if not required.issubset(factors.columns):
            raise ValueError("causal QMT factor ledger is malformed")
        for row in factors.sort_values("effective_on").itertuples(index=False):
            effective_on = row.effective_on
            if not isinstance(effective_on, date):
                effective_on = date.fromisoformat(str(effective_on))
            divisor = float(row.dr)
            if not math.isfinite(divisor) or divisor <= 0:
                raise ValueError("causal QMT factor divisor must be positive")
            sessions = normalized["date"].map(lambda value: value.date())
            multiplier.loc[sessions >= effective_on] *= divisor
    for field in ("open", "high", "low", "close"):
        normalized[field] = normalized[f"raw_{field}"] * multiplier
    quantum = resolve_structure_price_quantum("a", code)
    if quantum is None:
        raise ValueError("A-share structure price quantum is unavailable")
    metadata = build_provider_price_basis_metadata(
        provider="qmt",
        market="a",
        code=code,
        adjustment="causal-forward-ex-date",
        structure_price_quantum=quantum,
    )
    normalized = normalized.loc[:, list(FRAME_COLUMNS)]
    result = attach_price_basis_metadata(normalized, metadata)
    result.attrs.update(transport_metadata)
    return result


def load_qmt_daily_frame(
    code: str,
    *,
    start_at: datetime,
    end_at: datetime,
    factors: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """读取 QMT 原生日线，并保持与 1 分钟线相同的因果价格基准。"""

    return load_qmt_frame(
        code,
        "1d",
        start_at=start_at,
        end_at=end_at,
        factors=factors,
        _allow_native_daily=True,
    )


def strict_state(code: str, frequency: str, frame: pd.DataFrame) -> CL:
    if frame.empty:
        raise ValueError(f"{frequency} frame is empty")
    quantum = Decimal(str(frame.attrs["structure_price_quantum"]))
    revision = cast(str, frame.attrs["price_basis_revision"])
    config = strict_cl_config(
        structure_price_quantum=quantum,
        price_basis_revision=revision,
    )
    # 严格背驰使用因果局部高周期 MACD；每个源 K 线样本冻结于自身收盘，使认证回放与
    # 实时计算共享同一前缀稳定力度证据。
    return CL(code, frequency, config, market="a")


def _symbol_source_revision(
    frames: Mapping[str, pd.DataFrame],
    factors: pd.DataFrame,
    *,
    security_master: SecurityMasterRecord | None = None,
    memberships: Sequence[SectorMembershipChange] = (),
) -> str:
    """为事实实际使用的 QMT 行和复权因子账本生成精确指纹。

    检查点属于派生结果，只记录行数无法证明由哪些历史值生成。因此需要纳入三个周期
    的内容哈希、各自价格基准元数据以及复权因子账本。
    """

    digest = hashlib.sha256()
    for name in (*FREQUENCIES, "factors"):
        frame = factors if name == "factors" else frames[name]
        digest.update(name.encode("utf-8"))
        digest.update(len(frame).to_bytes(8, "big"))
        digest.update(
            pd.util.hash_pandas_object(
                frame.reset_index(drop=True),
                index=False,
                categorize=False,
            )
            .to_numpy(dtype="uint64", copy=False)
            .tobytes()
        )
        if name != "factors":
            metadata = {
                key: frame.attrs.get(key)
                for key in sorted(frame.attrs)
                if key
                in {
                    "adjustment",
                    "price_basis_revision",
                    "qmt_local_cache_audit_id",
                    "qmt_local_cache_source_sha256",
                    "qmt_transport",
                    "structure_price_quantum",
                }
            }
            digest.update(
                json.dumps(
                    metadata,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
    digest.update(repr(security_master).encode("utf-8"))
    digest.update(repr(tuple(memberships)).encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def final_confirmed_points(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
) -> tuple[StructuralPoint, ...]:
    return _causal_confirmed_points(code, frequency, frame)


@dataclass(frozen=True, slots=True)
class CausalCenterCompletionFact:
    """在因果检查点首次可见的只读完成几何事实。

    严格中枢实现仍是唯一权威。本事实只复制不可变的完成离开段和返回段，避免下游策略
    适配器再从买卖点锚定时间推断历史窗口。
    """

    center_id: str
    source_frequency: str
    structural_level: int
    price_basis_revision: str
    body_revision: int
    available_at: datetime
    completed_at: datetime
    zd_tick: int
    zg_tick: int
    leave_unit_id: str
    leave_direction: str
    leave_market_start: datetime
    leave_market_end: datetime
    leave_available_at: datetime
    leave_start_tick: int
    leave_end_tick: int
    leave_low_tick: int
    leave_high_tick: int
    return_unit_id: str
    return_direction: str
    return_market_start: datetime
    return_market_end: datetime
    return_available_at: datetime
    return_start_tick: int
    return_end_tick: int
    return_low_tick: int
    return_high_tick: int

    def __post_init__(self) -> None:
        for field in (
            "available_at",
            "completed_at",
            "leave_market_start",
            "leave_market_end",
            "leave_available_at",
            "return_market_start",
            "return_market_end",
            "return_available_at",
        ):
            object.__setattr__(
                self, field, normalize_datetime(getattr(self, field), field)
            )
        if not self.center_id or not self.price_basis_revision:
            raise ValueError("causal center identity is required")
        if self.structural_level < 0 or self.body_revision < 0:
            raise ValueError("causal center revisions and levels cannot be negative")
        # 唯一策略契约用闭区间交集 ``ZD <= ZG`` 定义中枢，因此一跳或相等中枢属于有效
        # 因果证据，不能只在历史账本消失而仍出现在页面和实时链路。
        if self.zd_tick > self.zg_tick:
            raise ValueError("causal center core must be a non-empty interval")
        if self.leave_direction != "up" or self.return_direction != "down":
            # 严格策略只消费向上三买完成几何；通用账本保持关闭失败，可防止卖侧中枢
            # 被误认成入场证据。
            raise ValueError("causal entry center requires up-leave/down-return")
        if not (
            self.leave_market_start
            <= self.leave_market_end
            <= self.return_market_start
            <= self.return_market_end
            <= self.completed_at
            <= self.available_at
        ):
            raise ValueError("causal center completion times are inconsistent")
        if (
            self.leave_available_at > self.available_at
            or self.return_available_at > self.available_at
        ):
            raise ValueError("center units cannot become visible after the center fact")


@dataclass(frozen=True, slots=True)
class CausalStructureEventLedger:
    """只追加记录从冻结线段前缀中首次观察到的事实。"""

    points: tuple[StructuralPoint, ...]
    completed_trends: tuple[TrendType, ...]
    completed_units: tuple[ConstituentUnit, ...] = ()
    center_completions: tuple[CausalCenterCompletionFact, ...] = ()
    point_anchor_unit_ids: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "points", tuple(self.points))
        object.__setattr__(self, "completed_trends", tuple(self.completed_trends))
        object.__setattr__(self, "completed_units", tuple(self.completed_units))
        object.__setattr__(self, "center_completions", tuple(self.center_completions))
        object.__setattr__(
            self,
            "point_anchor_unit_ids",
            tuple(self.point_anchor_unit_ids),
        )
        if (
            tuple(
                sorted(self.points, key=lambda item: (item.available_at, item.point_id))
            )
            != self.points
        ):
            raise ValueError("causal points must be deterministically ordered")
        if (
            tuple(
                sorted(
                    self.completed_trends,
                    key=lambda item: (item.available_at, item.trend_id),
                )
            )
            != self.completed_trends
        ):
            raise ValueError("causal trends must be deterministically ordered")
        if (
            tuple(
                sorted(
                    self.completed_units,
                    key=lambda item: (item.available_at, item.unit_id),
                )
            )
            != self.completed_units
        ):
            raise ValueError("causal completed units must be deterministically ordered")
        unit_keys = tuple(
            (item.unit_id, item.structural_level) for item in self.completed_units
        )
        if len(unit_keys) != len(set(unit_keys)):
            raise ValueError("causal completed units must be unique")
        if (
            tuple(
                sorted(
                    self.center_completions,
                    key=lambda item: (item.available_at, item.center_id),
                )
            )
            != self.center_completions
        ):
            raise ValueError(
                "causal center completions must be deterministically ordered"
            )
        center_keys = tuple(
            (item.center_id, item.structural_level) for item in self.center_completions
        )
        if len(center_keys) != len(set(center_keys)):
            raise ValueError("causal center completions must be unique")
        anchors = tuple(self.point_anchor_unit_ids)
        if anchors != tuple(sorted(anchors)):
            raise ValueError("causal point anchors must be sorted")
        if len({point_id for point_id, _unit_id in anchors}) != len(anchors):
            raise ValueError("causal point anchors must be unique by point")
        point_ids = {item.point_id for item in self.points}
        if {point_id for point_id, _unit_id in anchors} != point_ids:
            raise ValueError("every causal point requires one anchor unit")
        unit_ids = {item.unit_id for item in self.completed_units}
        if any(unit_id not in unit_ids for _point_id, unit_id in anchors):
            raise ValueError("causal point anchor references an unknown unit")


def final_confirmed_structure_events(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    *,
    visibility_windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> CausalStructureEventLedger:
    """返回实时与回放路径共同使用的因果买卖点/趋势账本。"""

    return _causal_confirmed_structure_events(
        code,
        frequency,
        frame,
        visibility_windows=visibility_windows,
    )


def _causal_confirmed_points(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    *,
    visibility_windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> tuple[StructuralPoint, ...]:
    return _causal_confirmed_structure_events(
        code,
        frequency,
        frame,
        visibility_windows=visibility_windows,
    ).points


def _causal_confirmed_structure_events(
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    *,
    visibility_windows: Sequence[tuple[datetime, datetime]] | None = None,
) -> CausalStructureEventLedger:
    """只从已锁定笔前缀构建并返回只追加事实。

    普通的最终严格快照在这里有意不作为充分证据。线段和递归尾部计算只是当前状态投影：
    后续前缀可能替换尾部，让此前确认的事实消失。读取最终投影再按买卖点历史
    ``available_at`` 过滤，会产生幸存者偏差。

    已锁定笔是前缀稳定输入。本过程依次推进因果检查点，冻结首个完成的线段且不再重开
    其边界，并在买卖点及完成趋势证据首次可观察时记录。检查点本身限定
    ``available_at`` 的最早值，因此即使内部几何辅助函数返回更早见证时间，也不能把
    可交易事件倒填到过去。
    """

    if frame.empty or visibility_windows == ():
        return CausalStructureEventLedger(points=(), completed_trends=())
    windows = None
    if visibility_windows is not None:
        normalized = sorted(
            (
                normalize_datetime(start, "visibility_start"),
                normalize_datetime(end, "visibility_end"),
            )
            for start, end in visibility_windows
        )
        if any(start > end for start, end in normalized):
            raise ValueError("visibility window start cannot follow end")
        merged: list[tuple[datetime, datetime]] = []
        for start, end in normalized:
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        windows = tuple(merged)

    def checkpoint_is_relevant(value: datetime) -> bool:
        return windows is None or any(start <= value <= end for start, end in windows)

    state = strict_state(code, frequency, frame)
    state.process_klines(frame)
    locked_bis = tuple(item for item in state.get_bis() if item.locked_at is not None)
    if len(locked_bis) < 3:
        return CausalStructureEventLedger(points=(), completed_trends=())

    price_quantum = Decimal(str(frame.attrs["structure_price_quantum"]))
    price_basis_revision = cast(str, frame.attrs["price_basis_revision"])
    strength = MacdStrengthProvider(state)
    max_levels = len(recursive_level_labels(frequency))
    recursive_engine = StrictRecursiveEngine(
        max_levels=max_levels,
        center_prefix_cache=OrderedDict(),
    )
    unit_lock_registry = UnitLockRegistry(price_basis_revision)

    frozen_segments = []
    point_ledger: dict[str, StructuralPoint] = {}
    point_anchor_ledger: dict[str, str] = {}
    trend_ledger: dict[str, TrendType] = {}
    unit_ledger: dict[tuple[str, int], ConstituentUnit] = {}
    center_ledger: dict[tuple[str, int], CausalCenterCompletionFact] = {}
    next_segment_start: int | None = None
    prefix_size = 3

    while prefix_size <= len(locked_bis):
        calculator = XdCalculator()
        prefix = list(locked_bis[:prefix_size])
        if next_segment_start is None:
            calculator.calculate(prefix)
        else:
            # 冻结线段是不可逆因果边界，只重建仍活动的后缀；未来笔不允许穿过已可观测点
            # 向前合并。
            calculator._build_segments(prefix, next_segment_start)
        completed = tuple(item for item in calculator.xds if item.is_done())
        if not completed:
            prefix_size += 1
            continue

        segment = completed[0]
        checkpoint = locked_bis[prefix_size - 1].locked_at
        if checkpoint is None:
            raise ValueError("causal segment checkpoint must be locked")
        # 首次产生线段的前缀是最强可见性边界；辅助对象更早的内部时间戳绝不能优先于
        # 此处实际消费的完整输入前缀。
        segment.locked_at = checkpoint
        segment.done = True
        frozen_segments.append(segment)

        following_start = int(segment.end_line.index) + 1
        if next_segment_start is not None and following_start <= next_segment_start:
            raise RuntimeError("causal segment replay did not advance")
        next_segment_start = following_start

        if not checkpoint_is_relevant(checkpoint):
            continue

        units = adapt_lines(
            frozen_segments,
            0,
            SourceKind.SEGMENT,
            price_quantum,
            checkpoint,
            unit_lock_registry,
        )
        if len(units) < 5:
            continue
        structure = recursive_engine.calculate(
            units,
            price_basis_revision=price_basis_revision,
            strength=strength,
        )
        for level in structure.levels:
            for unit in level.units:
                if not unit.locked or unit.confirmed_at is None:
                    continue
                first_seen_unit = replace(
                    unit,
                    available_at=max(unit.available_at, checkpoint),
                )
                unit_ledger.setdefault(
                    (first_seen_unit.unit_id, first_seen_unit.structural_level),
                    first_seen_unit,
                )
            for center in level.center_result.centers:
                leave = center.completion_leave_unit
                ret = center.completion_return_unit
                if (
                    center.completed_at is None
                    or leave is None
                    or ret is None
                    or not leave.locked
                    or not ret.locked
                    or leave.direction != "up"
                    or ret.direction != "down"
                ):
                    continue
                fact = CausalCenterCompletionFact(
                    center_id=center.center_id,
                    source_frequency=frequency,
                    structural_level=center.structural_level,
                    price_basis_revision=center.price_basis_revision,
                    body_revision=center.body_revision,
                    available_at=max(center.available_at, checkpoint),
                    completed_at=center.completed_at,
                    zd_tick=center.zd_tick,
                    zg_tick=center.zg_tick,
                    leave_unit_id=leave.unit_id,
                    leave_direction=leave.direction,
                    leave_market_start=leave.market_start,
                    leave_market_end=leave.market_end,
                    leave_available_at=leave.available_at,
                    leave_start_tick=leave.start_tick,
                    leave_end_tick=leave.end_tick,
                    leave_low_tick=leave.low_tick,
                    leave_high_tick=leave.high_tick,
                    return_unit_id=ret.unit_id,
                    return_direction=ret.direction,
                    return_market_start=ret.market_start,
                    return_market_end=ret.market_end,
                    return_available_at=ret.available_at,
                    return_start_tick=ret.start_tick,
                    return_end_tick=ret.end_tick,
                    return_low_tick=ret.low_tick,
                    return_high_tick=ret.high_tick,
                )
                center_ledger.setdefault((fact.center_id, fact.structural_level), fact)
        for level in structure.levels:
            for trend in level.completed_trends:
                first_seen_trend = replace(
                    trend,
                    available_at=max(trend.available_at, checkpoint),
                )
                trend_ledger.setdefault(
                    first_seen_trend.trend_id,
                    first_seen_trend,
                )
        strict_config_revision = cast(
            str,
            state.get_config()["strict_config_revision"],
        )
        assembler = StrictEvidenceAssembler(
            symbol=code,
            source_frequency=frequency,
            source_closed_at=checkpoint,
            price_basis_revision=price_basis_revision,
            structure_price_quantum=price_quantum,
            strict_config_revision=strict_config_revision,
            structure=structure,
            strength=strength,
        )
        raw_points = assembler.confirmed_points()
        raw_current = current_strict_point_evidence(structure, raw_points)
        converted_point_ids = structural_point_id_map(
            raw_points,
            code=code,
            source_frequency=frequency,
        )
        raw_anchor_by_point_id = {
            converted_point_ids[raw.point_id]: raw.anchor_unit_id for raw in raw_points
        }
        converted = convert_confirmed_point_evidence(
            raw_points,
            code=code,
            source_frequency=frequency,
            as_of=checkpoint,
        )
        current_ids = {
            converted_point_ids[point.point_id] for point in raw_current
        }
        current_terminal_references = {
            converted_point_ids[point.point_id]: terminal_segment_reference(
                structure,
                structural_level=point.structural_level,
                unit_id=point.anchor_unit_id,
            )
            for point in raw_current
        }
        if any(
            reference is None
            for reference in current_terminal_references.values()
        ):
            raise ValueError("current backtest point lost terminal segment lineage")
        for point in (
            item for item in converted if item.point_id in current_ids
        ):
            if windows is not None and not any(
                start <= point.available_at <= end for start, end in windows
            ):
                # 在活动设置窗口之前已可用的点属于历史背景，不是窗口内首个检查点新确认的触发。
                continue
            first_seen = replace(
                point,
                available_at=max(point.available_at, checkpoint),
                terminal_segment=current_terminal_references[point.point_id],
            )
            point_ledger.setdefault(first_seen.point_id, first_seen)
            anchor_unit_id = raw_anchor_by_point_id[first_seen.point_id]
            previous_anchor = point_anchor_ledger.setdefault(
                first_seen.point_id,
                anchor_unit_id,
            )
            if previous_anchor != anchor_unit_id:
                raise ValueError("causal point anchor unit changed across prefixes")

    return CausalStructureEventLedger(
        points=tuple(
            sorted(
                point_ledger.values(),
                key=lambda point: (point.available_at, point.point_id),
            )
        ),
        completed_trends=tuple(
            sorted(
                trend_ledger.values(),
                key=lambda trend: (trend.available_at, trend.trend_id),
            )
        ),
        completed_units=tuple(
            sorted(
                unit_ledger.values(),
                key=lambda item: (item.available_at, item.unit_id),
            )
        ),
        center_completions=tuple(
            sorted(
                center_ledger.values(),
                key=lambda item: (item.available_at, item.center_id),
            )
        ),
        point_anchor_unit_ids=tuple(sorted(point_anchor_ledger.items())),
    )


def _point_lane(point: StructuralPoint) -> tuple[str, str, int]:
    return five_minute_setup_family_lane(point)


def setup_active_ends(
    points: Sequence[StructuralPoint],
) -> dict[str, tuple[datetime, bool]]:
    """返回每个形态的包含式结束时间，以及它是否已被更新通道取代。"""

    ordered = tuple(
        sorted(points, key=lambda point: (point.available_at, point.point_id))
    )
    next_by_lane: dict[tuple[str, str, int], datetime] = {}
    output: dict[str, tuple[datetime, bool]] = {}
    for point in reversed(ordered):
        lane = _point_lane(point)
        expiry = five_minute_setup_expires_at(
            point,
            max_setup_age_seconds=MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
        )
        following = next_by_lane.get(lane)
        if following is not None and following <= expiry:
            output[point.point_id] = (following, True)
        else:
            output[point.point_id] = (expiry, False)
        next_by_lane[lane] = point.available_at
    return output


def first_matching_segment_difference(
    setup: StructuralPoint,
    one_points: Sequence[StructuralPoint],
    *,
    active_end: datetime,
    end_exclusive: bool,
) -> StructuralPoint | None:
    if setup.source_frequency != "5m" or not setup.confirmed:
        raise ValueError("setup must be a confirmed 5m point")
    prices = [
        setup.structure_invalidation_price,
        setup.structure_anchor_price,
    ]
    boundary = setup.center_zg if setup.side == "buy" else setup.center_zd
    if boundary is not None:
        prices.append(boundary)
    low, high = min(prices), max(prices)
    window_start = five_minute_segment_difference_window_start(setup)
    matches = (
        point
        for point in one_points
        if is_one_minute_segment_difference(point)
        and point.code == setup.code
        and point.side == setup.side
        and point.point_id != setup.point_id
        and point.price_basis_revision == setup.price_basis_revision
        and window_start <= point.anchor_at
        and window_start <= point.available_at
        and (
            point.available_at < active_end
            if end_exclusive
            else point.available_at <= active_end
        )
        and low <= point.structure_anchor_price <= high
    )
    return min(
        matches,
        key=lambda point: (
            point.available_at,
            point.recursive_level,
            point.tower,
            point.point_id,
        ),
        default=None,
    )


def first_matching_trigger(
    setup: StructuralPoint,
    one_points: Sequence[StructuralPoint],
    *,
    active_end: datetime,
    end_exclusive: bool,
) -> StructuralPoint | None:
    """旧接口别名；返回的是可选1分钟段差，不是正式信号触发器。"""

    return first_matching_segment_difference(
        setup,
        one_points,
        active_end=active_end,
        end_exclusive=end_exclusive,
    )


def sparse_evaluation_times(
    *,
    five_points: Sequence[StructuralPoint],
    one_points: Sequence[StructuralPoint],
    thirty_closes: Sequence[datetime],
    one_closes: Sequence[datetime],
    effective_start: datetime,
    requested_end: datetime,
) -> tuple[datetime, ...]:
    """构建 5 分钟正式点可用后可能发生变化的唯一时间点集合。

    1 分钟结构点只作为段差证据，不能决定一条 5 分钟信号是否进入回放。
    ``one_points`` 参数为旧事实档案兼容而保留；执行仍落在下一根可见 1 分钟柱。
    """

    start = normalize_datetime(effective_start, "effective_start")
    end = normalize_datetime(requested_end, "requested_end")
    if start > end:
        raise ValueError("effective_start cannot follow requested_end")
    one_dates = tuple(
        sorted(normalize_datetime(value, "one_close") for value in one_closes)
    )
    thirty_dates = tuple(
        sorted(normalize_datetime(value, "thirty_close") for value in thirty_closes)
    )
    active_ends = setup_active_ends(five_points)
    _ = one_points
    output: set[datetime] = set()
    for setup in five_points:
        active_end, superseded = active_ends[setup.point_id]
        if active_end < start or setup.available_at > end:
            continue
        first_at = max(setup.available_at, start)
        position = bisect_right(one_dates, first_at - timedelta(microseconds=1))
        if position >= len(one_dates):
            continue
        first_bar = one_dates[position]
        if first_bar > end or (
            first_bar >= active_end if superseded else first_bar > active_end
        ):
            continue
        output.add(first_bar)
        for observed_at in thirty_dates:
            if observed_at <= first_bar or observed_at > end:
                continue
            if observed_at > active_end or (superseded and observed_at >= active_end):
                break
            output.add(observed_at)
    return tuple(sorted(output))


def causal_directions(
    code: str,
    frame: pd.DataFrame,
    observed_times: Sequence[datetime],
) -> tuple[tuple[datetime, ContextDirection], int]:
    if not observed_times:
        return (), 0
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    state = strict_state(code, "30m", frame)
    cursor = 0
    output: list[tuple[datetime, ContextDirection]] = []
    unavailable = 0
    for raw_time in sorted(set(observed_times)):
        observed_at = normalize_datetime(raw_time, "observed_at")
        end = bisect_right(dates, observed_at)
        if end > cursor:
            chunk = frame.iloc[cursor:end].copy().reset_index(drop=True)
            copy_price_basis_metadata(frame, chunk)
            # ``chunk`` is a monotonic slice of the same immutable frame and
            # ``cursor`` proves that every prior row is unchanged.  Let the
            # HTF calculators consume only the revised tail/new rows instead
            # of revalidating the complete prefix at every evaluation point.
            state.process_validated_incremental_klines(chunk)
            cursor = end
        if cursor == 0:
            output.append((observed_at, "neutral"))
            unavailable += 1
            continue
        try:
            direction = cast(
                ContextDirection,
                current_formal_direction_from_components(
                    structure=state.get_strict_structure_levels(),
                    confirmed_points=state.get_strict_points(),
                    source_closed_at=dates[cursor - 1],
                ),
            )
            output.append((observed_at, direction))
        except (StrictStructureContractError, TypeError, ValueError):
            output.append((observed_at, "neutral"))
            unavailable += 1
    return tuple(output), unavailable


def _neutral_context(frequency: str, observed_at: datetime) -> TimeframeContext:
    return TimeframeContext(
        frequency=frequency,
        direction="neutral",
        disposition="neutral",
        hard_block=False,
        dominant_point_id=None,
        dominant_point_type=None,
        reason_codes=("sector_frequency_not_a_hard_gate",),
        observed_at=observed_at,
    )


def sector_facts_from_frame(
    *,
    sector_id: str,
    sector_name: str,
    member_count: int,
    frame: pd.DataFrame,
    observed_times: Sequence[datetime],
    algorithm_revision: str,
    source_revision: str,
    market_data_source: str = "qmt_gics3_component_composite",
    expected_closes: Sequence[datetime] = (),
) -> SectorResearchFacts:
    """在标的评估时点回放生产环境的板块硬闸门。"""

    times = tuple(sorted(set(observed_times)))
    if not times:
        return SectorResearchFacts(
            schema=SECTOR_FACT_SCHEMA,
            algorithm_revision=algorithm_revision,
            source_revision=source_revision,
            sector_id=sector_id,
            sector_name=sector_name,
            member_count=member_count,
            row_count=len(frame),
            thirty_points=(),
            assessments=(),
        )
    if frame.empty:
        return unavailable_sector_facts(
            sector_id=sector_id,
            sector_name=sector_name,
            member_count=member_count,
            observed_times=times,
            reason="sector_composite_frame_empty",
            algorithm_revision=algorithm_revision,
            source_revision=source_revision,
        )
    points = final_confirmed_points(sector_id, "30m", frame)
    directions, unavailable = causal_directions(sector_id, frame, times)
    frame_closes = tuple(
        sorted(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    )
    market_closes = tuple(
        sorted(normalize_datetime(value, "expected_close") for value in expected_closes)
    )
    assessments: list[tuple[datetime, SectorAssessment]] = []
    for observed_at, direction in directions:
        available = tuple(
            point for point in points if point.available_at <= observed_at
        )
        thirty = classify_context(
            frequency="30m",
            current_direction=direction,
            points=available,
            as_of=observed_at,
        )
        expected_position = bisect_right(market_closes, observed_at)
        actual_position = bisect_right(frame_closes, observed_at)
        data_complete = (
            True
            if not market_closes
            else expected_position > 0
            and actual_position > 0
            and market_closes[expected_position - 1]
            == frame_closes[actual_position - 1]
        )
        assessments.append(
            (
                observed_at,
                assess_sector(
                    sector_id=sector_id,
                    sector_name=sector_name,
                    market_data_source=market_data_source,
                    thirty=thirty,
                    five=_neutral_context("5m", observed_at),
                    one=_neutral_context("1m", observed_at),
                    data_complete=data_complete,
                ),
            )
        )
    return SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        sector_id=sector_id,
        sector_name=sector_name,
        member_count=member_count,
        row_count=len(frame),
        thirty_points=points,
        assessments=tuple(assessments),
        direction_unavailable_count=unavailable,
    )


def unavailable_sector_facts(
    *,
    sector_id: str,
    sector_name: str,
    member_count: int,
    observed_times: Sequence[datetime],
    reason: str,
    algorithm_revision: str,
    source_revision: str,
) -> SectorResearchFacts:
    if not reason:
        raise ValueError("unavailable sector reason is required")
    assessments = tuple(
        (
            observed_at,
            SectorAssessment(
                sector_id=sector_id,
                sector_name=sector_name,
                eligible=False,
                hard_block=True,
                regime="hostile",
                rank_components=(),
                reason_codes=("sector_data_incomplete",),
            ),
        )
        for observed_at in sorted(set(observed_times))
    )
    return SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        sector_id=sector_id,
        sector_name=sector_name,
        member_count=member_count,
        row_count=0,
        thirty_points=(),
        assessments=assessments,
        error=reason,
    )


def build_symbol_bundle(
    facts: SymbolResearchFacts,
    evaluation: SparseEvaluationFact,
    sector: SectorAssessment,
    *,
    held_tower: StructureTower | None = None,
    held_level: int | None = None,
    selection_sources: tuple[str, ...] = (),
    selection_research: SelectionResearchSnapshot | None = None,
) -> SymbolStructureBundle:
    observed_at = evaluation.observed_at

    def tradable(points, frequency):
        return tuple(
            point
            for point in points
            if point.available_at <= observed_at
            and point.source_frequency == frequency
        )

    # 完整递归图保留在事实档案中；订单级别只消费物理 5m/L0。5m/L1 的有效
    # 周期是 30m，只能进入高周期上下文，不能成为第二条 5m 交易通道。
    thirty = tradable(facts.thirty_points, "30m")
    five_context = tradable(facts.five_points, "5m")
    five = tuple(
        point
        for point in five_context
        if is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        )
    )
    one = tradable(facts.one_points, "1m")
    entry_boundaries = tuple(
        EntryExecutionBoundary(
            symbol=facts.code,
            point_id=point.point_id,
            source_frequency="1m",
            confirmation_bar_closed_at=evaluation.bar.closed_at,
            raw_open=evaluation.bar.raw_open,
            raw_high=evaluation.bar.raw_high,
            raw_low=evaluation.bar.raw_low,
            raw_close=evaluation.bar.raw_close,
            raw_volume=evaluation.bar.volume,
            entry_valid_until=a_share_optional_entry_valid_until(
                evaluation.bar.closed_at
            ),
            raw_price_basis_revision=facts.source_revision,
        )
        for point in one
        if point.available_at == evaluation.bar.closed_at
        and is_one_minute_segment_difference(point)
    )
    return SymbolStructureBundle(
        code=facts.code,
        as_of=observed_at,
        sector=sector,
        thirty_direction=evaluation.thirty_direction,
        thirty_points=thirty,
        five_points=five,
        one_points=one,
        # 与实时网关一致：冲突证据覆盖全部已分析个股周期，并在 ``resolve_conflict``
        # 内按方向和级别过滤。
        opposite_points=(*thirty, *five_context, *one),
        held_tower=held_tower,
        held_level=held_level,
        physical_timeframe_recursive=True,
        entry_execution_boundaries=entry_boundaries,
        selection_sources=selection_sources,
        selection_research=selection_research,
    )


def _event_bars(
    frame: pd.DataFrame,
    observed_times: Iterable[datetime],
) -> dict[datetime, MinuteBar]:
    if frame.empty:
        return {}
    rows = frame.set_index("date", drop=False)
    sessions = tuple(sorted({pd.Timestamp(value).date() for value in frame["date"]}))
    session_close = {
        session: Decimal(
            str(
                group.iloc[-1]["raw_close" if "raw_close" in group.columns else "close"]
            )
        )
        for session, group in frame.groupby(
            frame["date"].map(lambda value: value.date())
        )
    }
    previous: dict[date, Decimal] = {}
    for index, session in enumerate(sessions):
        if index > 0:
            previous[session] = session_close[sessions[index - 1]]
    output: dict[datetime, MinuteBar] = {}
    for raw_time in observed_times:
        observed_at = normalize_datetime(raw_time, "observed_at")
        key = pd.Timestamp(observed_at)
        if key not in rows.index:
            continue
        row = rows.loc[key]
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        opened = Decimal(str(row["open"]))
        high = Decimal(str(row["high"]))
        low = Decimal(str(row["low"]))
        closed = Decimal(str(row["close"]))
        raw_opened = Decimal(str(row.get("raw_open", row["open"])))
        raw_high = Decimal(str(row.get("raw_high", row["high"])))
        raw_low = Decimal(str(row.get("raw_low", row["low"])))
        raw_closed = Decimal(str(row.get("raw_close", row["close"])))
        volume = Decimal(str(row["volume"]))
        reference = previous.get(observed_at.date(), opened)
        output[observed_at] = MinuteBar(
            code=str(row["code"]),
            opened_at=observed_at - timedelta(minutes=1),
            closed_at=observed_at,
            raw_open=raw_opened,
            raw_high=raw_high,
            raw_low=raw_low,
            raw_close=raw_closed,
            analysis_open=opened,
            analysis_high=high,
            analysis_low=low,
            analysis_close=closed,
            previous_raw_close=reference,
            volume=volume,
            turnover=raw_closed * volume,
            adjustment_known_at=observed_at,
        )
    return output


def _board_limit(code: str, session: date) -> Decimal:
    digits = code.split(".", 1)[1]
    if code.startswith("BJ."):
        return Decimal("0.30")
    if digits.startswith(("688", "689")):
        return Decimal("0.20")
    if digits.startswith(("300", "301")) and session >= date(2020, 8, 24):
        return Decimal("0.20")
    return Decimal("0.10")


def _status_for_bar(
    bar: MinuteBar,
    master: SecurityMasterRecord | None = None,
):
    from chanlun.decision_support.trading_system.backtest.models import SecurityStatus

    session = bar.opened_at.date()
    return SecurityStatus(
        session=session,
        code=bar.code,
        listed=True if master is None else master.listed_on(session),
        st=False,
        suspended=bar.volume <= 0,
        # 认证执行直接观察下一根已完成分钟 K 线价格区间；有意使用宽限制可避免虚构历史
        # 特别处理股票涨跌幅，同时原始 OHLC 仍约束所有可能成交价。
        limit_pct=(_board_limit(bar.code, session) if master is None else Decimal("1")),
        lot_size=100,
        t_plus_days=1,
    )


@dataclass(slots=True)
class _ActiveMinuteSource:
    frame: pd.DataFrame
    dates: tuple[datetime, ...]
    previous_by_session: dict[date, Decimal]
    index: int

    @property
    def next_at(self) -> datetime | None:
        return None if self.index >= len(self.dates) else self.dates[self.index]

    def pop(self) -> MinuteBar:
        if self.index >= len(self.frame):
            raise IndexError("minute source is exhausted")
        row = self.frame.iloc[self.index]
        observed_at = self.dates[self.index]
        self.index += 1
        opened = Decimal(str(row["open"]))
        high = Decimal(str(row["high"]))
        low = Decimal(str(row["low"]))
        closed = Decimal(str(row["close"]))
        raw_opened = Decimal(str(row.get("raw_open", row["open"])))
        raw_high = Decimal(str(row.get("raw_high", row["high"])))
        raw_low = Decimal(str(row.get("raw_low", row["low"])))
        raw_closed = Decimal(str(row.get("raw_close", row["close"])))
        volume = Decimal(str(row["volume"]))
        return MinuteBar(
            code=str(row["code"]),
            opened_at=observed_at - timedelta(minutes=1),
            closed_at=observed_at,
            raw_open=raw_opened,
            raw_high=raw_high,
            raw_low=raw_low,
            raw_close=raw_closed,
            analysis_open=opened,
            analysis_high=high,
            analysis_low=low,
            analysis_close=closed,
            previous_raw_close=self.previous_by_session.get(
                observed_at.date(),
                opened,
            ),
            volume=volume,
            turnover=raw_closed * volume,
            adjustment_known_at=observed_at,
        )


def _active_minute_source(
    code: str,
    *,
    requested_start: date,
    requested_end: date,
    after: datetime,
) -> _ActiveMinuteSource:
    frame = load_qmt_frame(
        code,
        "1m",
        start_at=datetime.combine(
            requested_start - timedelta(days=1),
            time(9, 30),
            tzinfo=CN,
        ),
        end_at=datetime.combine(requested_end, time(15, 0), tzinfo=CN),
    )
    dates = tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])
    sessions = tuple(sorted({value.date() for value in dates}))
    session_closes = {
        session: Decimal(
            str(
                group.iloc[-1]["raw_close" if "raw_close" in group.columns else "close"]
            )
        )
        for session, group in frame.groupby(
            frame["date"].map(lambda value: value.date())
        )
    }
    previous = {
        session: session_closes[sessions[index - 1]]
        for index, session in enumerate(sessions)
        if index > 0
    }
    return _ActiveMinuteSource(
        frame=frame,
        dates=dates,
        previous_by_session=previous,
        index=bisect_right(dates, normalize_datetime(after, "after")),
    )


def _market_minute_timeline(
    *,
    effective_start: date,
    requested_end: date,
) -> tuple[datetime, ...]:
    frame = load_qmt_frame(
        "SH.000001",
        "1m",
        start_at=datetime.combine(effective_start, time(9, 30), tzinfo=CN),
        end_at=datetime.combine(requested_end, time(15, 0), tzinfo=CN),
    )
    if frame.empty:
        raise RuntimeError("QMT market-minute timeline is unavailable")
    return tuple(pd.Timestamp(value).to_pydatetime() for value in frame["date"])


def _densify_equity_curve(
    run,
    timeline: Sequence[datetime],
    *,
    effective_start: date,
    requested_end: date,
):
    from chanlun.decision_support.trading_system.backtest.portfolio import (
        BacktestRun,
        EquityPoint,
    )

    points = tuple(sorted(run.equity_curve, key=lambda row: row.closed_at))
    if not points:
        raise ValueError("sparse run has no equity points")
    baseline_at = datetime.combine(effective_start, time(9, 30), tzinfo=CN)
    terminal_at = datetime.combine(requested_end, time(15, 0), tzinfo=CN)
    times = {
        normalize_datetime(value, "market_minute")
        for value in timeline
        if baseline_at <= normalize_datetime(value, "market_minute") <= terminal_at
    }
    times.update((baseline_at, terminal_at))
    times.update(point.closed_at for point in points)
    ordered_times = tuple(sorted(times))
    dense: list[EquityPoint] = []
    cursor = 0
    current = points[0]
    if current.closed_at > ordered_times[0]:
        raise ValueError("equity curve starts after the market timeline")
    for observed_at in ordered_times:
        while cursor + 1 < len(points) and points[cursor + 1].closed_at <= observed_at:
            cursor += 1
            current = points[cursor]
        dense.append(
            EquityPoint(
                closed_at=observed_at,
                cash=current.cash,
                market_value=current.market_value,
                equity=current.equity,
                open_risk_cash=current.open_risk_cash,
            )
        )
    return BacktestRun(
        fills=run.fills,
        trades=run.trades,
        equity_curve=tuple(dense),
        open_positions=run.open_positions,
        pending_exits=run.pending_exits,
    )


def run_sparse_portfolio(
    symbol_facts: Sequence[SymbolResearchFacts],
    sector_facts: Mapping[str, SectorResearchFacts],
    *,
    initial_cash: Decimal,
    minute_timeline: Sequence[datetime] | None = None,
    selection_research_by_code: Mapping[
        str,
        tuple[SelectionResearchSnapshot, ...],
    ]
    | None = None,
    formal_selection_required: bool = True,
):
    """执行稀疏决策，并逐分钟回放所有持仓与待处理订单。

    新买入始终要求当时的板块评估确属支持状态。只有调用方明确启用
    ``formal_selection_required`` 时，才额外要求当时可见的正式个股研究快照；
    当前生产选股不读取该旧账本，因此正式固定年度回放会显式关闭这一门。
    板块来源由同一次因果回放中的 ``SectorResearchFacts`` 唯一推导，调用方不能再
    用静态标的映射把未来触发回填到整段历史。
    """

    from chanlun.decision_support.trading_system.backtest.execution import (
        ExecutionPolicy,
    )
    from chanlun.decision_support.trading_system.backtest.portfolio import (
        _PortfolioState,
        risk_candidate_from,
    )
    from chanlun.decision_support.trading_system.human_assisted_decision import (
        HumanAssistedDecisionCore,
    )
    from chanlun.decision_support.trading_system.models import TradingPolicy
    from chanlun.decision_support.trading_system.portfolio_risk import (
        RiskLimits,
        size_entry,
    )

    if initial_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if type(formal_selection_required) is not bool:
        raise ValueError("formal_selection_required must be a boolean")
    if not symbol_facts:
        raise ValueError("symbol facts are required")
    selection_research = {
        code: tuple(snapshots)
        for code, snapshots in (selection_research_by_code or {}).items()
    }
    facts_by_code = {row.code: row for row in symbol_facts}
    if len(facts_by_code) != len(symbol_facts):
        raise ValueError("symbol facts contain duplicate codes")
    unknown_research_symbols = set(selection_research).difference(facts_by_code)
    if unknown_research_symbols:
        raise ValueError("selection research contains symbols outside replay facts")
    for code, snapshots in selection_research.items():
        if any(snapshot.symbol != code for snapshot in snapshots):
            raise ValueError("selection research symbol does not match mapping key")
        identities = tuple(snapshot.snapshot_id for snapshot in snapshots)
        if len(identities) != len(set(identities)):
            raise ValueError("selection research snapshots must be unique")
        if snapshots != tuple(
            sorted(
                snapshots,
                key=lambda snapshot: (
                    snapshot.effective_at,
                    snapshot.known_at,
                    snapshot.snapshot_id,
                ),
            )
        ):
            raise ValueError("selection research snapshots must be chronological")
    requested_starts = {row.requested_start for row in symbol_facts}
    requested_ends = {row.requested_end for row in symbol_facts}
    effective_starts = {row.effective_start for row in symbol_facts}
    if any(
        len(values) != 1
        for values in (requested_starts, requested_ends, effective_starts)
    ):
        raise ValueError("symbol fact ranges disagree")
    requested_start = next(iter(requested_starts))
    requested_end = next(iter(requested_ends))
    effective_start = next(iter(effective_starts))
    events_by_time: dict[
        datetime,
        list[tuple[SymbolResearchFacts, SparseEvaluationFact]],
    ] = {}
    for facts in symbol_facts:
        for evaluation in facts.evaluations:
            events_by_time.setdefault(evaluation.observed_at, []).append(
                (facts, evaluation)
            )
    event_times = tuple(sorted(events_by_time))
    sector_by_time = {
        sector_id: dict(facts.assessments) for sector_id, facts in sector_facts.items()
    }
    state = _PortfolioState.initial(initial_cash)
    baseline_at = datetime.combine(effective_start, time(9, 30), tzinfo=CN)
    state.record_equity(baseline_at)
    engine = HumanAssistedDecisionCore(
        TradingPolicy(),
        formal_selection_required=formal_selection_required,
    )
    risk_limits = RiskLimits()
    execution_policy = ExecutionPolicy(require_observed_price_range=True)
    actions = tuple(
        sorted(
            (
                factor.corporate_action()
                for facts in symbol_facts
                for factor in facts.factors
            ),
            key=lambda row: (row.effective_at, row.code),
        )
    )
    action_keys = tuple((row.code, row.effective_at) for row in actions)
    if len(action_keys) != len(set(action_keys)):
        raise ValueError("corporate actions contain duplicate symbol dates")
    action_index = 0
    active: dict[str, _ActiveMinuteSource] = {}
    last_bars: dict[str, MinuteBar] = {}
    event_index = 0
    while event_index < len(event_times) or active:
        next_event = (
            event_times[event_index] if event_index < len(event_times) else None
        )
        active_times = tuple(
            source.next_at for source in active.values() if source.next_at is not None
        )
        next_active = min(active_times, default=None)
        candidates = tuple(
            value for value in (next_event, next_active) if value is not None
        )
        if not candidates:
            break
        observed_at = min(candidates)
        due_actions = []
        while (
            action_index < len(actions)
            and actions[action_index].effective_at <= observed_at
        ):
            due_actions.append(actions[action_index])
            action_index += 1
        if due_actions:
            state.apply_corporate_actions(tuple(due_actions))
        bars: dict[str, MinuteBar] = {}
        for code, source in tuple(active.items()):
            if source.next_at == observed_at:
                bars[code] = source.pop()
        current_events = (
            events_by_time[observed_at] if next_event == observed_at else []
        )
        if next_event == observed_at:
            event_index += 1
        for facts, evaluation in current_events:
            bars.setdefault(facts.code, evaluation.bar)
        for code in sorted(bars):
            bar = bars[code]
            status = _status_for_bar(
                bar,
                facts_by_code[code].security_master,
            )
            state.try_pending_orders(
                bar,
                status,
                execution_policy,
                risk_limits,
            )
            state.mark_to_market(bar)
            state.check_intrabar_structural_stops(
                bar,
                status,
                execution_policy,
            )
            last_bars[code] = bar
        for facts, evaluation in sorted(current_events, key=lambda row: row[0].code):
            bar = bars[facts.code]
            evaluation_sector_id = (
                facts.sector_id
                if facts.security_master is None
                else evaluation.sector_id
            )
            resolved_sector_id = evaluation_sector_id or "qmt-sw1:unclassified"
            sector = sector_by_time.get(resolved_sector_id, {}).get(observed_at)
            if sector is None:
                sector = SectorAssessment(
                    sector_id=resolved_sector_id,
                    sector_name=resolved_sector_id,
                    eligible=False,
                    hard_block=True,
                    regime="hostile",
                    rank_components=(),
                    reason_codes=("sector_data_incomplete",),
                )
            held = state.held_structures().get(facts.code)
            current_research = visible_selection_research(
                selection_research.get(facts.code, ()),
                symbol=facts.code,
                selection_path="INDIVIDUAL_THREE_PROGRAM",
                decision_time=observed_at,
            )
            bundle = build_symbol_bundle(
                facts,
                evaluation,
                sector,
                held_tower=None if held is None else held[0],
                held_level=None if held is None else held[1],
                selection_sources=(
                    ("QMT_SECTOR_TRIGGER",)
                    if sector.regime == "supportive"
                    else ("QMT_SECTOR_ELIGIBLE_SCOPE",)
                    if sector.eligible
                    else ("INCREMENTAL_SCAN_SCOPE",)
                ),
                selection_research=current_research,
            )
            status = _status_for_bar(bar, facts.security_master)
            for evaluated in engine.evaluate_symbol(bundle):
                if (
                    evaluated.entry is not None
                    and evaluated.entry.allowed
                    and evaluated.entry.signal_id not in state.consumed_signal_ids
                ):
                    candidate = risk_candidate_from(evaluated, bar)
                    sized = size_entry(
                        portfolio=state.snapshot(),
                        candidate=candidate,
                        limits=risk_limits,
                    )
                    state.enqueue_entry(
                        sized,
                        evaluated=evaluated,
                        bar=bar,
                        created_at=observed_at,
                    )
                if (
                    evaluated.exit is not None
                    and evaluated.exit.allowed
                    and evaluated.exit.signal_id not in state.consumed_signal_ids
                ):
                    state.enqueue_structural_exit(
                        evaluated,
                        code=facts.code,
                        created_at=observed_at,
                        lot_size=status.lot_size,
                    )
        state.record_equity(observed_at)
        needed = set(state.positions_by_code)
        needed.update(row.intent.code for row in state.pending_orders)
        for code in tuple(active):
            if code not in needed or active[code].next_at is None:
                del active[code]
        for code in sorted(needed - set(active)):
            active[code] = _active_minute_source(
                code,
                requested_start=requested_start,
                requested_end=requested_end,
                after=observed_at,
            )
    if state.positions_by_code:
        terminal_at = datetime.combine(requested_end, time(15, 0), tzinfo=CN)
        for code in sorted(tuple(state.positions_by_code)):
            master = facts_by_code[code].security_master
            if master is not None and (
                (
                    master.listed_through is not None
                    and master.listed_through < requested_end
                )
                or "\u9000\u5e02" in master.name
                or master.name.endswith("\u9000")
            ):
                state.write_off_position(
                    code=code,
                    closed_at=terminal_at,
                    reason="expired_security_zero_recovery",
                )
        # 样本边界仍开放的持仓继续保持开放；若用已完成末分钟 OHLC 卖出，会制造常规执行链
        # 明确禁止的同 K 线成交。改用最后一个因果可见的原始收盘价盯市，报告将开放持仓
        # 与已实现交易分开展示。
        state.record_equity(terminal_at)
    run = state.finish()
    timeline = (
        _market_minute_timeline(
            effective_start=effective_start,
            requested_end=requested_end,
        )
        if minute_timeline is None
        else tuple(minute_timeline)
    )
    return _densify_equity_curve(
        run,
        timeline,
        effective_start=effective_start,
        requested_end=requested_end,
    )


def build_symbol_facts(
    *,
    code: str,
    sector_id: str,
    warmup_start: date,
    requested_start: date,
    requested_end: date,
    effective_start: date,
    algorithm_revision: str,
    security_master: SecurityMasterRecord | None = None,
    memberships: Sequence[SectorMembershipChange] = (),
    qmt_factors: Sequence[QmtFactorAt] = (),
) -> SymbolResearchFacts:
    if not warmup_start <= requested_start <= effective_start <= requested_end:
        raise ValueError("invalid fixed-year date boundaries")
    if security_master is not None and security_master.code != code:
        raise ValueError("security master does not match requested code")
    membership_rows = tuple(
        sorted(memberships, key=lambda row: (row.known_at, row.sector_id))
    )
    factor_rows = tuple(sorted(qmt_factors, key=lambda row: row.effective_on))
    if any(row.code != code for row in (*membership_rows, *factor_rows)):
        raise ValueError("PIT metadata does not match requested code")
    factors = qmt_factor_frame(factor_rows)
    end_at = datetime.combine(requested_end, time(15, 0), tzinfo=CN)
    context_start = datetime.combine(warmup_start, time(9, 30), tzinfo=CN)
    # 行情终端 QMT 的滚动一年 1m 边界会在当前墙钟分钟截断首个请求交易日。
    # 日历日，再由编排器在 1m 预热后统一强制有效起点。
    minute_start = datetime.combine(
        requested_start - timedelta(days=1),
        time(9, 30),
        tzinfo=CN,
    )
    effective_at = datetime.combine(effective_start, time(9, 30), tzinfo=CN)
    local_directory = resolve_qmt_local_data_dir()
    local_five_snapshot = (
        read_qmt_local_kline(
            data_dir=local_directory,
            code=code,
            frequency="5m",
            start_at=context_start,
            end_at=end_at,
        )
        if local_directory is not None
        else None
    )
    frames = {
        frequency: load_qmt_frame(
            code,
            frequency,
            start_at=(minute_start if frequency == "1m" else context_start),
            end_at=end_at,
            factors=factors,
            _local_five_snapshot=local_five_snapshot,
        )
        for frequency in ("30m", "5m")
    }
    missing_context = tuple(
        frequency for frequency in ("30m", "5m") if frames[frequency].empty
    )
    if missing_context:
        raise RuntimeError(
            "QMT context history is unavailable for classified security "
            f"{code}: {','.join(missing_context)}"
        )
    five_points = _causal_confirmed_points(
        code,
        "5m",
        frames["5m"],
        visibility_windows=(
            (
                effective_at - timedelta(seconds=MAX_FIVE_MINUTE_SETUP_AGE_SECONDS),
                end_at,
            ),
        ),
    )
    operation_five_points = tuple(
        point
        for point in five_points
        if is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        )
        and five_minute_setup_is_in_policy_scope(point)
    )
    active_ends = setup_active_ends(operation_five_points)
    relevant_setups = tuple(
        setup
        for setup in operation_five_points
        if setup.available_at <= end_at
        and active_ends[setup.point_id][0] >= effective_at
        and setup.available_at <= active_ends[setup.point_id][0]
    )
    has_relevant_setup = bool(relevant_setups)
    one_frame = (
        load_qmt_frame(
            code,
            "1m",
            start_at=minute_start,
            end_at=end_at,
            factors=factors,
        )
        if has_relevant_setup
        else _empty_frame(code)
    )
    if has_relevant_setup and one_frame.empty:
        raise RuntimeError(
            f"QMT 1m history is unavailable for a causally relevant setup: {code}"
        )
    frames["1m"] = one_frame
    source_revision = _symbol_source_revision(
        frames,
        factors,
        security_master=security_master,
        memberships=membership_rows,
    )
    one_points = _causal_confirmed_points(
        code,
        "1m",
        one_frame,
        visibility_windows=tuple(
            (
                setup.available_at,
                min(active_ends[setup.point_id][0], end_at),
            )
            for setup in relevant_setups
        ),
    )
    evaluation_times = sparse_evaluation_times(
        five_points=operation_five_points,
        one_points=one_points,
        thirty_closes=tuple(
            pd.Timestamp(value).to_pydatetime() for value in frames["30m"]["date"]
        ),
        one_closes=tuple(
            pd.Timestamp(value).to_pydatetime() for value in one_frame["date"]
        ),
        effective_start=effective_at,
        requested_end=end_at,
    )
    thirty_points = _causal_confirmed_points(
        code,
        "30m",
        frames["30m"],
        visibility_windows=((context_start, max(evaluation_times)),)
        if evaluation_times
        else (),
    )
    directions, unavailable = causal_directions(
        code,
        frames["30m"],
        evaluation_times,
    )
    direction_by_time = dict(directions)
    bars = _event_bars(one_frame, evaluation_times)

    def sector_at(observed_at: datetime) -> str | None:
        if security_master is not None and not security_master.listed_on(
            observed_at.date()
        ):
            return None
        available = tuple(row for row in membership_rows if row.known_at <= observed_at)
        return None if not available else available[-1].sector_id

    evaluations = tuple(
        SparseEvaluationFact(
            observed_at=observed_at,
            thirty_direction=direction_by_time[observed_at],
            bar=bars[observed_at],
            sector_id=sector_at(observed_at),
        )
        for observed_at in evaluation_times
        if observed_at in bars
    )
    return SymbolResearchFacts(
        schema=FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        code=code,
        sector_id=sector_id,
        requested_start=requested_start,
        requested_end=requested_end,
        effective_start=effective_start,
        row_counts=tuple(
            (frequency, len(frames[frequency])) for frequency in FREQUENCIES
        ),
        thirty_points=thirty_points,
        five_points=five_points,
        one_points=one_points,
        evaluations=evaluations,
        direction_unavailable_count=unavailable,
        security_master=security_master,
        memberships=membership_rows,
        factors=factor_rows,
    )


__all__ = (
    "FACT_SCHEMA",
    "SECTOR_FACT_SCHEMA",
    "SectorResearchFacts",
    "SparseEvaluationFact",
    "SymbolResearchFacts",
    "build_symbol_bundle",
    "build_symbol_facts",
    "causal_directions",
    "final_confirmed_points",
    "first_matching_segment_difference",
    "first_matching_trigger",
    "load_qmt_frame",
    "load_qmt_daily_frame",
    "qmt_factor_frame",
    "qmt_native_code",
    "run_sparse_portfolio",
    "sector_facts_from_frame",
    "setup_active_ends",
    "sparse_evaluation_times",
    "strict_state",
    "unavailable_sector_facts",
)
