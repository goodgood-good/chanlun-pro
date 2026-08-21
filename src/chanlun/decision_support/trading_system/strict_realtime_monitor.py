"""供只读实时监听使用的严格物理周期事实。

图表筛选入口、历史回放和本监听器共同使用 ``strict_cl_config`` 与
``build_screening_evidence`` 组成的同一决策核心。价格基准元数据缺失或结构
快照无效时必须形成可观察的刷新失败，绝不能回退为另一套买卖信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from chanlun import fun
from chanlun.core.cl import CL
from chanlun.core.strict_structure.formal_state import current_formal_direction
from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.trading_system.models import (
    CANONICAL_POINT_TYPE_SET,
    POINT_REVIEW_ORDER,
    StructuralPoint,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
    is_one_minute_segment_level,
)
from chanlun.decision_support.trading_system.lifecycle import (
    current_five_minute_setup_points,
    five_minute_setup_is_current,
    match_one_minute_segment_difference_for_point,
)
from chanlun.decision_support.trading_system.provisional import (
    extract_current_provisional_candidates,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    build_position_recommendation,
)
from chanlun.decision_support.trading_system.runtime_config import (
    StrictSnapshotPriceMetadata,
    strict_cl_config,
    strict_snapshot_price_metadata,
)
from chanlun.decision_support.trading_system.screening_structure import (
    build_screening_evidence,
)
from chanlun.decision_support.trading_system.screening_runtime import (
    validated_incremental_prefix_matches,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_WARMUP_REQUIRED_BARS,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
    extract_current_confirmed_points,
    extract_one_minute_segment_difference_points,
)
from chanlun.exchange.price_basis import copy_price_basis_metadata
from chanlun.exchange.kline_completion import (
    drop_unclosed_last_bar,
    frequency_to_minutes,
)


CN = ZoneInfo("Asia/Shanghai")


class _WarmupIncomplete(RuntimeError):
    """所需周期有效，但历史数据量尚未达到预热要求。"""


def _market_name(exchange: object, code: str) -> str:
    raw = getattr(exchange, "market", "")
    value = getattr(raw, "value", raw)
    market = str(value or "").strip().lower()
    if market:
        return market
    suffix = code.rsplit(".", 1)[-1].lower() if "." in code else ""
    if suffix == "us":
        return "us"
    if code.upper().startswith("HK."):
        return "hk"
    return "a"


def _aware_datetime(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp):
        raise ValueError("kline timestamp must be a datetime")
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(CN)
    return timestamp.to_pydatetime()


def _strict_direction(evidence: StrictEvidenceResult | None) -> str:
    return "neutral" if evidence is None else current_formal_direction(evidence)


@dataclass
class StrictRealtimeMonitorEvent:
    """带有严格证据身份的通知事实。"""

    code: str
    name: str
    side: str
    kind: str
    bs_type: str
    signal_time: str
    price: float
    big_dir: str
    reason: str
    op_level: str = "5m"
    big_level: str = "30m"
    mid_level: str = "1m"
    mid_dir: str = ""
    evidence_id: str = ""
    recursive_level: int = 0
    anchor_time: str = ""
    confirmed_time: str = ""
    detected_time: str = ""
    structure_anchor_price: float | None = None
    structure_invalidation_price: float | None = None
    price_source: str = "latest_completed_1m_close"
    price_observed_at: str = ""
    signal_role: str = "TRADE_SIGNAL_5M"
    position_recommendation: dict[str, object] | None = None
    setup_bs_type: str = ""
    setup_evidence_id: str = ""
    setup_recursive_level: int = 0
    setup_anchor_time: str = ""
    setup_confirmed_time: str = ""
    setup_available_time: str = ""
    segment_difference_point_type: str = ""
    segment_difference_evidence_id: str = ""
    segment_difference_recursive_level: int | None = None
    segment_difference_anchor_time: str = ""
    segment_difference_confirmed_time: str = ""
    segment_difference_available_time: str = ""
    segment_difference_divergence_kind: str | None = None

    def __post_init__(self) -> None:
        if self.side in {"buy", "sell"}:
            if self.bs_type not in CANONICAL_POINT_TYPE_SET:
                raise ValueError("实时监听事件只能使用统一六类买卖点")
            expected_side = "buy" if self.bs_type.endswith("buy") else "sell"
            if self.side != expected_side:
                raise ValueError("实时监听事件的买卖点类型与方向不一致")
            if self.signal_role == "TRADE_SIGNAL_5M":
                if self.kind != f"strict_{self.side}_point":
                    raise ValueError("实时监听买卖点事件必须来自统一严格通道")
            elif self.signal_role == "SEGMENT_DIFFERENCE_1M":
                if self.kind != "strict_segment_difference_update":
                    raise ValueError("1 分钟段差补充事件类型无效")
            else:
                raise ValueError("实时监听买卖点角色无效")
            if type(self.recursive_level) is not int or self.recursive_level < 0:
                raise ValueError("实时监听买卖点递归级别必须为非负整数")
            if not is_five_minute_trade_level(
                self.op_level,
                self.recursive_level,
            ):
                raise ValueError("实时监听买卖点必须来自物理 5m/L0 交易层")
            if not self.evidence_id or not self.anchor_time:
                raise ValueError("实时监听买卖点必须携带完整证据身份")
            if self.op_level != "5m" or self.mid_level != "1m":
                raise ValueError("实时监听买卖点必须以 5 分钟为买卖级别")
            segment_present = bool(self.segment_difference_point_type)
            if segment_present:
                expected_segment_side = (
                    "buy"
                    if self.segment_difference_point_type.endswith("buy")
                    else "sell"
                )
                if (
                    self.segment_difference_point_type
                    not in CANONICAL_POINT_TYPE_SET
                    or expected_segment_side != self.side
                    or not self.segment_difference_evidence_id
                    or type(self.segment_difference_recursive_level) is not int
                    or not is_one_minute_segment_level(
                        self.mid_level,
                        self.segment_difference_recursive_level,
                    )
                    or not self.segment_difference_anchor_time
                    or not self.segment_difference_confirmed_time
                    or not self.segment_difference_available_time
                    or self.segment_difference_divergence_kind
                    not in {None, "trend", "consolidation"}
                    or self.segment_difference_point_type.startswith("1")
                    and self.segment_difference_divergence_kind
                    not in {"trend", "consolidation"}
                    or self.segment_difference_point_type.startswith("2")
                    and self.segment_difference_divergence_kind
                    not in {None, "consolidation"}
                    or self.segment_difference_point_type.startswith("3")
                    and self.segment_difference_divergence_kind is not None
                ):
                    raise ValueError("实时监听的 1 分钟段差证据不完整")
            elif any(
                (
                    self.segment_difference_evidence_id,
                    self.segment_difference_anchor_time,
                    self.segment_difference_confirmed_time,
                    self.segment_difference_available_time,
                )
            ) or self.segment_difference_divergence_kind is not None or (
                self.segment_difference_recursive_level is not None
            ):
                raise ValueError("实时监听的 1 分钟段差字段互相矛盾")
            if self.signal_role == "TRADE_SIGNAL_5M":
                if self.kind != f"strict_{self.side}_point":
                    raise ValueError("实时监听买卖点事件必须来自统一严格通道")
                return
            if self.signal_role == "SEGMENT_DIFFERENCE_1M":
                if self.kind != "strict_segment_difference_update":
                    raise ValueError("1 分钟段差补充事件类型无效")
                if not segment_present:
                    raise ValueError("1 分钟段差补充事件必须携带段差证据")
                if (
                    self.setup_bs_type != self.bs_type
                    or self.setup_evidence_id != self.evidence_id
                    or self.setup_recursive_level != self.recursive_level
                    or self.setup_anchor_time != self.anchor_time
                    or self.setup_confirmed_time != self.confirmed_time
                    or not self.setup_available_time
                    or self.signal_time != self.segment_difference_available_time
                ):
                    raise ValueError("1 分钟段差补充事件的 5 分钟父结构不完整")
                return
            raise ValueError("实时监听买卖点角色无效")
        if (
            self.side == "risk"
            and not self.bs_type
            and self.kind == "strict_30m_context_warning"
            and self.big_level == "30m"
            and self.signal_role == "CONTEXT_WARNING_30M"
        ):
            return
        raise ValueError("实时监听事件方向无效")

    @property
    def identity(self) -> str:
        # 同一时刻、同一类别可以在不同递归级别同时成立，因此通知身份必须
        # 携带递归级别和完整点证据编号，不能只用类别与时间做模糊去重。
        base = self.delivery_identity
        if self.signal_role == "SEGMENT_DIFFERENCE_1M":
            return "|".join(
                (
                    base,
                    self.evidence_id or "-",
                    self.segment_difference_evidence_id or "-",
                )
            )
        if self.side not in {"buy", "sell"}:
            return "|".join((base, self.evidence_id or "-"))
        return "|".join((base, self.evidence_id or "-"))

    @property
    def delivery_identity(self) -> str:
        """返回跨证据重建仍稳定的同一次买卖点通知身份。

        完整 ``identity`` 继续绑定图表审计证据；通知去重只使用买卖点的
        物理发生事实。全量重建可能修订内部 ``evidence_id``，但不能因此
        把同一标的、递归级别、锚点和完成 K 线重复发送一次。
        """

        if self.signal_role == "SEGMENT_DIFFERENCE_1M":
            return "|".join(
                (
                    self.kind,
                    self.code,
                    self.op_level,
                    self.bs_type,
                    f"L{self.recursive_level}",
                    self.anchor_time or "-",
                    self.setup_available_time or "-",
                    self.mid_level,
                    self.segment_difference_point_type,
                    f"L{self.segment_difference_recursive_level}",
                    self.segment_difference_anchor_time or "-",
                    self.segment_difference_available_time or "-",
                )
            )
        fields = (
            self.kind,
            self.code,
            self.op_level,
            self.bs_type or "-",
            f"L{self.recursive_level}",
            self.anchor_time or "-",
            self.signal_time or "-",
        )
        if self.side not in {"buy", "sell"}:
            return "|".join(fields)
        return "|".join(fields)


@dataclass
class _FrequencyRuntime:
    cd: CL
    metadata: StrictSnapshotPriceMetadata
    strict_config_revision: str
    source_frame: pd.DataFrame
    evidence: StrictEvidenceResult | None = None
    confirmed_points: tuple[StructuralPoint, ...] | None = None
    update_count: int = 0
    incremental_update_count: int = 0
    rebuild_count: int = 0
    last_update_incremental: bool | None = None


class StrictPhysicalMonitorState:
    """使用统一筛选决策核心的 5m买卖/1m段差/30m环境增量监听状态。"""

    WARMUP_DAYS_BY_FREQ = {
        "1m": 30,
        "5m": 120,
        "30m": 365,
    }
    # 监听、实时选股和回放共用同一最低预热线。低于该数量时宁可保持
    # ``warming``，也不能用一条更短的监听专用历史产生另一套买卖点。
    MINIMUM_BARS_BY_FREQ = dict(SCREENING_WARMUP_REQUIRED_BARS)

    def __init__(
        self,
        code: str,
        ex: object,
        op_level: str = "5m",
        big_level: str = "30m",
        mid_level: str | None = "1m",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not code:
            raise ValueError("monitor code is required")
        if (op_level, mid_level, big_level) != ("5m", "1m", "30m"):
            raise ValueError(
                "realtime monitor requires 5m trade, 1m segment and 30m context levels"
            )
        levels = tuple(
            value for value in (op_level, mid_level or "", big_level) if value
        )
        if len(levels) != len(set(levels)):
            raise ValueError("monitor levels must be distinct")
        self.code = code
        self.ex = ex
        self.market = _market_name(ex, code)
        self.kline_time_label = getattr(ex, "kline_time_label", "start")
        if self.kline_time_label not in {"start", "end"}:
            raise ValueError("exchange kline_time_label must be start or end")
        self.op_level = op_level
        self.mid_level = mid_level or ""
        self.big_level = big_level
        self._clock = clock or (lambda: datetime.now(CN))
        self.last_op: pd.Timestamp | None = None
        self.last_mid: pd.Timestamp | None = None
        self.last_big: pd.Timestamp | None = None
        self.last_px = 0.0
        self.last_px_source = "unavailable"
        self.last_px_observed_at: datetime | None = None
        self.last_observed_at: datetime | None = None
        self.seen: set[tuple[str, ...]] = set()
        self.consecutive_refresh_failures = 0
        self.consecutive_warmup_incomplete = 0
        self.consecutive_segment_warmup_incomplete = 0
        self.warmup_ready = False
        self.segment_difference_ready = False
        self.segment_difference_reason = "NOT_REFRESHED"
        trade_minutes = 5
        # 实时监听只允许发布最近两根 5 分钟买卖周期 K 线内刚刚可用的信号。
        # 原来的至少 60 分钟窗口会把暖机重建出的旧结构误当作实时通知。
        self.signal_freshness = pd.Timedelta(minutes=trade_minutes * 2)
        self._runtime_by_frequency: dict[str, _FrequencyRuntime] = {}
        self._segment_difference_by_trade_point_id: dict[str, StructuralPoint] = {}
        self._segment_attached_parent_occurrences: set[tuple[str, ...]] = set()
        self._new_segment_difference_updates: tuple[
            tuple[StructuralPoint, StructuralPoint], ...
        ] = ()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("realtime monitor clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("realtime monitor clock must be timezone-aware")
        return value.astimezone(CN)

    def _is_fresh_point(
        self,
        point: StructuralPoint,
        observed_at: datetime,
    ) -> bool:
        lag = pd.Timestamp(observed_at) - pd.Timestamp(point.available_at)
        return bool(
            pd.Timedelta(0) <= lag <= self.signal_freshness
            and five_minute_setup_is_current(point, as_of=observed_at)
        )

    def _fetch_klines(
        self,
        frequency: str,
        last: pd.Timestamp | None,
        *,
        as_of: datetime,
    ):
        if last is None:
            warmup_days = self.WARMUP_DAYS_BY_FREQ.get(frequency)
            if warmup_days:
                start = (pd.Timestamp(as_of) - pd.Timedelta(days=warmup_days)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                try:
                    return self.ex.klines(
                        self.code,
                        frequency,
                        start_date=start,
                    )
                except TypeError:
                    pass
            return self.ex.klines(self.code, frequency)
        start = (last - pd.Timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        try:
            return self.ex.klines(self.code, frequency, start_date=start)
        except TypeError:
            return self.ex.klines(self.code, frequency)

    def _closed_frame(
        self,
        raw: object,
        frequency: str,
        *,
        as_of: datetime | None = None,
    ) -> pd.DataFrame:
        if not isinstance(raw, pd.DataFrame) or raw.empty:
            raise ValueError(f"{frequency} kline frame is unavailable")
        metadata = strict_snapshot_price_metadata(raw)
        required = ("date", "open", "high", "low", "close", "volume")
        missing = tuple(column for column in required if column not in raw.columns)
        if missing:
            raise ValueError(f"{frequency} kline fields missing: {','.join(missing)}")
        frame = drop_unclosed_last_bar(
            raw,
            frequency,
            time_label=self.kline_time_label,
            as_of=as_of,
        )
        if frame is None or frame.empty:
            raise ValueError(f"{frequency} has no completed kline")
        frame = frame.loc[:, list(required)].copy()
        copy_price_basis_metadata(raw, frame)
        strict_snapshot_price_metadata(frame)
        dates = pd.to_datetime(frame["date"])
        if dates.isna().any() or dates.duplicated().any():
            raise ValueError(f"{frequency} kline times must be unique datetimes")
        if not dates.is_monotonic_increasing:
            raise ValueError(f"{frequency} kline times must be increasing")
        numeric_columns = ["open", "high", "low", "close", "volume"]
        numeric = frame.loc[:, numeric_columns].apply(
            pd.to_numeric,
            errors="coerce",
        )
        if not np.isfinite(numeric.to_numpy(dtype=float)).all():
            raise ValueError(f"{frequency} kline values must be finite numbers")
        # 几何比较必须使用已经实际校验的数值。数据源允许返回数字字符串；若
        # 直接比较 object 类型，可能变成字典序比较或在中途失败。
        frame.loc[:, numeric_columns] = numeric
        if (
            (frame[["open", "high", "low", "close"]] <= 0).any(axis=None)
            or
            (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any()
            or (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any()
            or (frame["volume"] < 0).any()
        ):
            raise ValueError(f"{frequency} kline geometry is invalid")
        # 保留已校验元数据的显式引用，防止后续重构误把强制校验弱化为尽力检查。
        if not metadata.price_basis_revision:
            raise AssertionError("validated price basis unexpectedly empty")
        return frame

    def _new_runtime(
        self,
        frequency: str,
        metadata: StrictSnapshotPriceMetadata,
        source_frame: pd.DataFrame,
    ) -> _FrequencyRuntime:
        config = strict_cl_config(
            structure_price_quantum=metadata.structure_price_quantum,
            price_basis_revision=metadata.price_basis_revision,
        )
        revision = str(config["strict_config_revision"])
        return _FrequencyRuntime(
            cd=CL(self.code, frequency, config, market=self.market),
            metadata=metadata,
            strict_config_revision=revision,
            source_frame=source_frame,
        )

    @staticmethod
    def _merge_authoritative_tail(
        existing: pd.DataFrame,
        tail: pd.DataFrame,
    ) -> pd.DataFrame:
        """用新拉取数据替换重叠尾部，只保留未触及的历史前缀。"""

        first_tail_at = pd.Timestamp(tail["date"].iloc[0])
        prefix = existing.loc[pd.to_datetime(existing["date"]) < first_tail_at]
        merged = pd.concat((prefix, tail), ignore_index=True)
        merged = merged.sort_values("date", kind="stable").reset_index(drop=True)
        copy_price_basis_metadata(tail, merged)
        if pd.to_datetime(merged["date"]).duplicated().any():
            raise ValueError("authoritative kline merge produced duplicate times")
        strict_snapshot_price_metadata(merged)
        return merged

    @staticmethod
    def _point_occurrence_key(point: StructuralPoint) -> tuple[str, ...]:
        return (
            str(point.code),
            str(point.source_frequency),
            str(point.side),
            str(point.point_type),
            str(point.recursive_level),
            point.anchor_at.isoformat(timespec="seconds"),
            point.available_at.isoformat(timespec="seconds"),
        )

    def _refresh_visible_price(self) -> None:
        """Use the freshest completed close across the optional 1m and 5m feeds."""

        candidates: list[tuple[datetime, bool, float, str]] = []
        for frequency in (self.op_level, self.mid_level):
            if frequency == self.mid_level and not self.segment_difference_ready:
                continue
            runtime = self._runtime_by_frequency.get(frequency)
            if runtime is None:
                continue
            klines = runtime.cd.get_src_klines()
            if not klines:
                continue
            last_kline = klines[-1]
            raw_date = getattr(last_kline, "date", None)
            completed_at = (
                _aware_datetime(raw_date)
                if raw_date is not None
                else self.last_observed_at
            )
            if completed_at is None:
                continue
            if raw_date is not None and self.kline_time_label == "start":
                minutes = frequency_to_minutes(frequency)
                if minutes is None:
                    continue
                completed_at += timedelta(minutes=float(minutes))
            price = float(last_kline.c)
            if not np.isfinite(price) or price <= 0:
                continue
            candidates.append(
                (
                    completed_at,
                    frequency == self.mid_level,
                    price,
                    frequency,
                )
            )
        if not candidates:
            self.last_px = 0.0
            self.last_px_source = "unavailable"
            self.last_px_observed_at = None
            return
        completed_at, _prefer_one_minute, price, frequency = max(candidates)
        self.last_px = price
        self.last_px_source = f"latest_completed_{frequency}_close"
        self.last_px_observed_at = completed_at

    def _process_level(
        self,
        frequency: str,
        last_attr: str,
        observed_at: datetime,
    ) -> StrictEvidenceResult:
        last = getattr(self, last_attr)
        frame = self._closed_frame(
            self._fetch_klines(frequency, last, as_of=observed_at),
            frequency,
            as_of=observed_at,
        )
        metadata = strict_snapshot_price_metadata(frame)
        runtime = self._runtime_by_frequency.get(frequency)
        if runtime is not None and runtime.metadata != metadata:
            # 除权除息或数据源批次改变了结构价格基准。只回放五日增量尾部会混入
            # 不可比较的价格，因此必须重新取得完整预热窗口。
            frame = self._closed_frame(
                self._fetch_klines(frequency, None, as_of=observed_at),
                frequency,
                as_of=observed_at,
            )
            metadata = strict_snapshot_price_metadata(frame)
            runtime = None
            setattr(self, last_attr, None)
            last = None
        if runtime is None:
            minimum = self.MINIMUM_BARS_BY_FREQ.get(frequency, 1)
            if len(frame) < minimum:
                raise _WarmupIncomplete(
                    f"{frequency} warmup requires {minimum} completed bars, got {len(frame)}"
                )
            source_frame = frame.reset_index(drop=True)
            copy_price_basis_metadata(frame, source_frame)
        elif runtime.metadata != metadata:
            raise ValueError(f"{frequency} price basis changed during full warmup")
        else:
            source_frame = self._merge_authoritative_tail(
                runtime.source_frame,
                frame,
            )

        latest = pd.Timestamp(source_frame["date"].iloc[-1])

        source_closed_at = observed_at.astimezone(CN)
        latest_market_time = _aware_datetime(source_frame["date"].iloc[-1])
        if source_closed_at < latest_market_time.astimezone(CN):
            raise ValueError(f"{frequency} snapshot contains a future completed bar")
        unchanged = runtime is not None and runtime.source_frame.reset_index(
            drop=True
        ).equals(source_frame)
        if unchanged and runtime.evidence is not None:
            return runtime.evidence

        reusable = bool(
            runtime is not None
            and validated_incremental_prefix_matches(
                runtime.source_frame,
                source_frame,
            )
        )
        previous_update_count = 0 if runtime is None else runtime.update_count
        previous_incremental_count = (
            0 if runtime is None else runtime.incremental_update_count
        )
        previous_rebuild_count = 0 if runtime is None else runtime.rebuild_count
        candidate = (
            runtime
            if reusable
            else self._new_runtime(frequency, metadata, source_frame)
        )
        assert candidate is not None
        try:
            if reusable:
                candidate.cd.process_validated_incremental_klines(source_frame)
            else:
                candidate.cd.process_klines(source_frame)
            candidate.evidence = build_screening_evidence(
                candidate.cd,
                source_closed_at=source_closed_at,
                structure_price_quantum=metadata.structure_price_quantum,
                price_basis_revision=metadata.price_basis_revision,
                strict_config_revision=candidate.strict_config_revision,
            )
        except Exception:
            # 增量调用可能已改变内存态。证据投影失败后必须丢弃该对象，下一轮从
            # 完整预热窗口重建，绝不能把半更新状态继续当成权威快照。
            self._runtime_by_frequency.pop(frequency, None)
            setattr(self, last_attr, None)
            raise
        candidate.source_frame = source_frame
        candidate.confirmed_points = None
        candidate.update_count = previous_update_count + 1
        candidate.incremental_update_count = previous_incremental_count + int(
            reusable
        )
        candidate.rebuild_count = previous_rebuild_count + int(not reusable)
        candidate.last_update_incremental = reusable
        self._runtime_by_frequency[frequency] = candidate
        setattr(self, last_attr, latest)
        return candidate.evidence

    def _process_optional_segment_level(
        self,
        observed_at: datetime,
    ) -> StrictEvidenceResult | None:
        """刷新可选 1 分钟段差；失败不得抹掉 5 分钟正式信号。"""

        try:
            evidence = self._process_level(
                self.mid_level,
                "last_mid",
                observed_at,
            )
        except _WarmupIncomplete as exc:
            self.segment_difference_ready = False
            self.segment_difference_reason = str(exc)[:240]
            self.consecutive_segment_warmup_incomplete += 1
            return None
        except Exception as exc:
            self.segment_difference_ready = False
            self.segment_difference_reason = (
                f"{type(exc).__name__}: {str(exc)[:200]}"
            )
            return None
        self.segment_difference_ready = True
        self.segment_difference_reason = "READY"
        self.consecutive_segment_warmup_incomplete = 0
        return evidence

    def _confirmed_points(
        self,
        frequency: str,
        *,
        as_of: datetime,
    ) -> tuple[StructuralPoint, ...]:
        runtime = self._runtime_by_frequency.get(frequency)
        if runtime is None or getattr(runtime, "evidence", None) is None:
            return ()
        if getattr(runtime, "confirmed_points", None) is None:
            runtime.confirmed_points = extract_current_confirmed_points(
                runtime.evidence,
                code=self.code,
                source_frequency=frequency,
                as_of=as_of,
            )
        return runtime.confirmed_points

    def matching_five_minute_setup(
        self,
        trigger: StructuralPoint,
    ) -> StructuralPoint | None:
        """旧接口：5 分钟本身已是正式信号，不再由 1 分钟点反向匹配。"""

        return (
            trigger
            if is_five_minute_trade_level(
                trigger.source_frequency,
                trigger.recursive_level,
            )
            else None
        )

    def refresh(self) -> list[StructuralPoint]:
        self.warmup_ready = False
        self.segment_difference_ready = False
        self.segment_difference_reason = "REFRESH_PENDING"
        self._segment_difference_by_trade_point_id = {}
        self._new_segment_difference_updates = ()
        observed_at = self._now()
        self.last_observed_at = observed_at
        try:
            op = self._process_level(self.op_level, "last_op", observed_at)
            big = self._process_level(self.big_level, "last_big", observed_at)
        except _WarmupIncomplete:
            self.consecutive_warmup_incomplete += 1
            return []
        mid = self._process_optional_segment_level(observed_at)
        self.warmup_ready = True
        self.consecutive_warmup_incomplete = 0

        if self.op_level != "5m" or self.mid_level != "1m":
            raise ValueError("realtime monitor requires 5m trade and 1m segment levels")
        all_points = tuple(
            point
            for point in extract_current_confirmed_points(
                op,
                code=self.code,
                source_frequency="5m",
                as_of=observed_at,
            )
            if is_five_minute_trade_level(
                point.source_frequency,
                point.recursive_level,
            )
        )
        current_provisional = (
            extract_current_provisional_candidates(
                op,
                code=self.code,
                source_frequency="5m",
                as_of=observed_at,
            )
            if tuple(getattr(op, "approaching_points", ()) or ())
            else ()
        )
        # 页面、选股与通知共享同一个末端窗口裁剪入口。该入口会分别保留
        # 最新未完成线段和最新已完成线段，避免通知侧再次实现一套方向规则。
        current_setups = current_five_minute_setup_points(
            (*all_points, *current_provisional),
            as_of=observed_at,
        )
        points = tuple(
            point
            for point in current_setups
            if isinstance(point, StructuralPoint)
        )
        one_minute_points = (
            extract_one_minute_segment_difference_points(
                mid,
                code=self.code,
                source_frequency="1m",
                as_of=observed_at,
            )
            if mid is not None
            else ()
        )
        self._segment_difference_by_trade_point_id = {
            point.point_id: segment
            for point in points
            if (
                segment := match_one_minute_segment_difference_for_point(
                    point,
                    one_minute_points,
                    as_of=observed_at,
                )
            )
            is not None
        }
        new_segment_updates: list[tuple[StructuralPoint, StructuralPoint]] = []
        for point in points:
            segment = self._segment_difference_by_trade_point_id.get(point.point_id)
            if segment is None:
                continue
            parent_occurrence = self._point_occurrence_key(point)
            if parent_occurrence in self._segment_attached_parent_occurrences:
                continue
            # 与主选股通知保持相同语义：同一个 5 分钟正式点只在第一条 1 分钟
            # 新鲜段差证据附着时补充通知，后续证据重建或更新不重复冒充新事件。
            # 过期或尚未到达观察时刻的旧证据不能提前消耗这个机会，否则同一
            # 设置稍后真正出现的新鲜 1 分钟段差会被永久吞掉。
            confluence_at = max(point.available_at, segment.available_at)
            segment_lag = pd.Timestamp(observed_at) - pd.Timestamp(confluence_at)
            if pd.Timedelta(0) <= segment_lag <= self.signal_freshness:
                self._segment_attached_parent_occurrences.add(parent_occurrence)
                new_segment_updates.append((point, segment))
        self._new_segment_difference_updates = tuple(new_segment_updates)
        self._refresh_visible_price()

        output: list[StructuralPoint] = []
        selected_point_ids = {point.point_id for point in points}
        for point in all_points:
            occurrence_key = self._point_occurrence_key(point)
            if occurrence_key in self.seen:
                continue
            self.seen.add(occurrence_key)
            if (
                point.point_id in selected_point_ids
                and self._is_fresh_point(point, observed_at)
            ):
                output.append(point)
        # 为诊断调用方和方向门保留显式引用。1 分钟只提供段差背景。
        if mid is not None and _strict_direction(mid) not in {"up", "down", "neutral"}:
            raise AssertionError("invalid strict middle direction")
        if _strict_direction(big) not in {"up", "down", "neutral"}:
            raise AssertionError("invalid strict high direction")
        return output

    def refresh_chart_levels(self) -> bool:
        """刷新已完成的 5m/30m，并尽力刷新可选的 1m 段差图。

        通知图片属于复核材料，不是一次决策调用。图片必须展示同一物理周期核心，
        但不能要求或绕过独立的日线风险门。尤其是，未完成的 QMT 日线仍会被
        ``refresh()`` 拒绝，而图表渲染仍可使用三个已经完整收盘的盘中周期。
        """

        self.warmup_ready = False
        self.segment_difference_ready = False
        self.segment_difference_reason = "REFRESH_PENDING"
        observed_at = self._now()
        try:
            self._process_level(self.op_level, "last_op", observed_at)
            self._process_level(self.big_level, "last_big", observed_at)
        except _WarmupIncomplete:
            self.consecutive_warmup_incomplete += 1
            return False
        self._process_optional_segment_level(observed_at)
        self.warmup_ready = True
        self.consecutive_warmup_incomplete = 0
        return True

    def evidence(self, frequency: str) -> StrictEvidenceResult | None:
        if frequency == "1m" and not self.segment_difference_ready:
            return None
        runtime = self._runtime_by_frequency.get(frequency)
        return None if runtime is None else runtime.evidence

    def segment_difference_for_trade_point(
        self,
        point: StructuralPoint,
    ) -> StructuralPoint | None:
        """返回本轮与该 5 分钟正式点绑定的可选 1 分钟段差。"""

        return self._segment_difference_by_trade_point_id.get(point.point_id)

    def new_segment_difference_updates(
        self,
    ) -> tuple[tuple[StructuralPoint, StructuralPoint], ...]:
        """返回本轮首次附着且仍在通知新鲜窗口内的 1 分钟段差。"""

        return self._new_segment_difference_updates

    def chart_data(self, frequency: str) -> CL | None:
        """返回本监听器实际消费的对应周期计算对象。

        按约定仅向展示代码只读暴露该对象。通知渲染不能重建第二条信号路径，
        也不能修改此状态；它只负责展示已经刷新的结构。
        """

        normalized = str(frequency)
        if normalized == "1m" and not self.segment_difference_ready:
            return None
        runtime = self._runtime_by_frequency.get(normalized)
        return None if runtime is None else runtime.cd

    def confirmed_point_occurrence(
        self,
        point_type: str,
        signal_time: str,
        *,
        frequency: str = "5m",
        evidence_id: str,
        recursive_level: int,
        anchor_time: str,
    ) -> StructuralPoint | None:
        """从当前精确计算的图表快照中解析唯一的通知证据。"""

        evidence = self.evidence(frequency)
        if (
            evidence is None
            or not point_type
            or not signal_time
            or not evidence_id
            or type(recursive_level) is not int
            or recursive_level < 0
            or not anchor_time
        ):
            return None
        try:
            target = pd.Timestamp(signal_time)
            target_anchor = pd.Timestamp(anchor_time)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None or target_anchor.tzinfo is None:
            return None
        now = self._now()
        target_datetime = target.to_pydatetime()
        as_of = max(now, target_datetime.astimezone(CN))
        points = extract_confirmed_points(
            evidence,
            code=self.code,
            source_frequency=frequency,
            as_of=as_of,
        )
        target_utc = target.tz_convert("UTC")
        target_anchor_utc = target_anchor.tz_convert("UTC")
        semantic_matches = [
            point
            for point in points
            if point.point_type == point_type
            and point.recursive_level == recursive_level
            and pd.Timestamp(point.anchor_at).tz_convert("UTC")
            == target_anchor_utc
            and pd.Timestamp(point.available_at).tz_convert("UTC") == target_utc
        ]
        exact = [point for point in semantic_matches if point.point_id == evidence_id]
        if len(exact) == 1:
            return exact[0]
        # 全图重算或价格基准重建可能只修订内部证据编号。只有在物理发生
        # 身份仍唯一时才允许绑定新编号；零个或多个候选都继续安全关闭。
        return semantic_matches[0] if len(semantic_matches) == 1 else None

    def big_dir(self) -> str:
        return _strict_direction(self.evidence(self.big_level))

    def mid_dir(self) -> str:
        return (
            ""
            if not self.mid_level
            else _strict_direction(self.evidence(self.mid_level))
        )


def _monitor_position_facts(
    state: object,
    point: StructuralPoint,
    *,
    big_dir: str,
) -> tuple[float, dict[str, object]]:
    visible_price = float(getattr(state, "last_px", 0.0) or 0.0)
    sizing_price = (
        visible_price
        if visible_price > 0 and np.isfinite(visible_price)
        else point.structure_anchor_price
    )
    side = str(point.side)
    point_type = str(point.point_type)
    recommendation = build_position_recommendation(
        side=side,
        recommendation=(
            "CAUTION"
            if big_dir == "neutral"
            or side == "buy" and big_dir == "down"
            or side == "sell" and big_dir == "up"
            else "READY"
        ),
        risk_multiplier={
            "1buy": "0.50",
            "2buy": "1.00",
            "3buy": "0.75",
        }.get(point_type, "0"),
        context_risk_scale=(
            "1.00" if side == "buy" and big_dir == "up" else "0.50"
        ),
        # 人工操作发生在当前可见价格，风险必须从该价格量到防守位；结构锚点
        # 独立传入，用于追价保护和审计，不能冒充当前价。
        entry_price=sizing_price,
        structural_stop=point.structure_invalidation_price,
        exit_action="none",
        structure_anchor_price=point.structure_anchor_price,
    ).document()
    return visible_price, recommendation


def collect_strict_monitor_events(
    states: Mapping[str, object],
    *,
    names: Mapping[str, str] | None = None,
    holdings: set[str] | None = None,
) -> list[StrictRealtimeMonitorEvent]:
    """刷新各状态，并把刚确认的 5 分钟正式买卖点映射为通知。"""

    names = names or {}
    holdings = holdings or set()
    signals: dict[str, Iterable[StructuralPoint]] = {}
    refreshed_codes: set[str] = set()
    log = fun.get_logger()
    for code, state in states.items():
        try:
            signals[code] = tuple(state.refresh())
            state.consecutive_refresh_failures = 0
            refreshed_codes.add(code)
        except Exception as exc:
            signals[code] = ()
            failures = int(getattr(state, "consecutive_refresh_failures", 0) or 0) + 1
            state.consecutive_refresh_failures = failures
            state.warmup_ready = False
            log.warning(
                "[strict_monitor] refresh failed code=%s count=%s: %s",
                code,
                failures,
                exc,
            )

    point_events: list[StrictRealtimeMonitorEvent] = []
    context_events: list[StrictRealtimeMonitorEvent] = []
    for code, points in signals.items():
        state = states[code]
        big_dir = str(state.big_dir() or "neutral")
        mid_dir = str(state.mid_dir() or "")
        op_level = str(getattr(state, "op_level", "5m") or "5m")
        mid_level = str(getattr(state, "mid_level", "1m") or "1m")
        big_level = str(getattr(state, "big_level", "30m") or "30m")
        for point in points:
            side = str(point.side)
            point_type = str(point.point_type)
            if side not in {"buy", "sell"}:
                continue
            if (
                not point.confirmed
                or not is_five_minute_trade_level(
                    point.source_frequency,
                    point.recursive_level,
                )
            ):
                continue
            segment_resolver = getattr(
                state,
                "segment_difference_for_trade_point",
                None,
            )
            segment = (
                segment_resolver(point)
                if callable(segment_resolver)
                else None
            )
            visible_price, position_recommendation = _monitor_position_facts(
                state,
                point,
                big_dir=big_dir,
            )
            event = StrictRealtimeMonitorEvent(
                code=code,
                name=str(names.get(code, code)),
                side=side,
                kind=f"strict_{side}_point",
                bs_type=point_type,
                signal_time=point.available_at.isoformat(timespec="seconds"),
                # 使用本轮 1m/5m 数据中完成时刻最新的收盘价；同刻优先 1m，
                # 1m 滞后或段差通道不可用时明确使用 5m。结构锚点继续单独
                # 保留，不能冒充当前价。
                price=visible_price,
                price_source=str(
                    getattr(
                        state,
                        "last_px_source",
                        "latest_completed_1m_close",
                    )
                    or "latest_completed_1m_close"
                ),
                price_observed_at=(
                    ""
                    if getattr(state, "last_px_observed_at", None) is None
                    else _aware_datetime(
                        getattr(state, "last_px_observed_at")
                    ).isoformat(timespec="seconds")
                ),
                big_dir=big_dir,
                reason=(
                    f"strict_5m_{point_type}_trade_signal"
                ),
                op_level=op_level,
                mid_level=mid_level,
                big_level=big_level,
                mid_dir=mid_dir,
                evidence_id=point.point_id,
                recursive_level=point.recursive_level,
                anchor_time=point.anchor_at.isoformat(timespec="seconds"),
                confirmed_time=point.confirmed_at.isoformat(timespec="seconds"),
                structure_anchor_price=float(point.structure_anchor_price),
                structure_invalidation_price=float(
                    point.structure_invalidation_price
                ),
                position_recommendation=position_recommendation,
                segment_difference_point_type=(
                    "" if segment is None else segment.point_type
                ),
                segment_difference_evidence_id=(
                    "" if segment is None else segment.point_id
                ),
                segment_difference_recursive_level=(
                    None if segment is None else segment.recursive_level
                ),
                segment_difference_anchor_time=(
                    ""
                    if segment is None
                    else segment.anchor_at.isoformat(timespec="seconds")
                ),
                segment_difference_confirmed_time=(
                    ""
                    if segment is None or segment.confirmed_at is None
                    else segment.confirmed_at.isoformat(timespec="seconds")
                ),
                segment_difference_available_time=(
                    ""
                    if segment is None
                    else segment.available_at.isoformat(timespec="seconds")
                ),
                segment_difference_divergence_kind=(
                    None if segment is None else segment.divergence_kind
                ),
            )
            point_events.append(event)

        if code not in refreshed_codes:
            continue
        updates_provider = getattr(state, "new_segment_difference_updates", None)
        segment_updates = (
            tuple(updates_provider()) if callable(updates_provider) else ()
        )
        newly_notified_point_ids = {
            point.point_id
            for point in points
            if isinstance(point, StructuralPoint)
        }
        for raw_update in segment_updates:
            if (
                not isinstance(raw_update, tuple)
                or len(raw_update) != 2
                or not isinstance(raw_update[0], StructuralPoint)
                or not isinstance(raw_update[1], StructuralPoint)
            ):
                continue
            point, segment = raw_update
            # 同一轮新出现的 5 分钟信号已经携带段差，不再拆成第二条补充通知。
            if point.point_id in newly_notified_point_ids:
                continue
            side = str(point.side)
            point_type = str(point.point_type)
            if (
                side not in {"buy", "sell"}
                or not point.confirmed
                or not is_five_minute_trade_level(
                    point.source_frequency,
                    point.recursive_level,
                )
                or segment.side != side
                or not is_one_minute_segment_level(
                    segment.source_frequency,
                    segment.recursive_level,
                )
            ):
                continue
            visible_price, position_recommendation = _monitor_position_facts(
                state,
                point,
                big_dir=big_dir,
            )
            point_events.append(
                StrictRealtimeMonitorEvent(
                    code=code,
                    name=str(names.get(code, code)),
                    side=side,
                    kind="strict_segment_difference_update",
                    bs_type=point_type,
                    signal_time=segment.available_at.isoformat(timespec="seconds"),
                    price=visible_price,
                    price_source=str(
                        getattr(
                            state,
                            "last_px_source",
                            "latest_completed_1m_close",
                        )
                        or "latest_completed_1m_close"
                    ),
                    price_observed_at=(
                        ""
                        if getattr(state, "last_px_observed_at", None) is None
                        else _aware_datetime(
                            getattr(state, "last_px_observed_at")
                        ).isoformat(timespec="seconds")
                    ),
                    big_dir=big_dir,
                    reason="strict_1m_segment_difference_enrichment",
                    op_level=op_level,
                    mid_level=mid_level,
                    big_level=big_level,
                    mid_dir=mid_dir,
                    evidence_id=point.point_id,
                    recursive_level=point.recursive_level,
                    anchor_time=point.anchor_at.isoformat(timespec="seconds"),
                    confirmed_time=point.confirmed_at.isoformat(timespec="seconds"),
                    structure_anchor_price=float(point.structure_anchor_price),
                    structure_invalidation_price=float(
                        point.structure_invalidation_price
                    ),
                    signal_role="SEGMENT_DIFFERENCE_1M",
                    position_recommendation=position_recommendation,
                    setup_bs_type=point_type,
                    setup_evidence_id=point.point_id,
                    setup_recursive_level=point.recursive_level,
                    setup_anchor_time=point.anchor_at.isoformat(
                        timespec="seconds"
                    ),
                    setup_confirmed_time=point.confirmed_at.isoformat(
                        timespec="seconds"
                    ),
                    setup_available_time=point.available_at.isoformat(
                        timespec="seconds"
                    ),
                    segment_difference_point_type=segment.point_type,
                    segment_difference_evidence_id=segment.point_id,
                    segment_difference_recursive_level=segment.recursive_level,
                    segment_difference_anchor_time=segment.anchor_at.isoformat(
                        timespec="seconds"
                    ),
                    segment_difference_confirmed_time=(
                        ""
                        if segment.confirmed_at is None
                        else segment.confirmed_at.isoformat(timespec="seconds")
                    ),
                    segment_difference_available_time=(
                        segment.available_at.isoformat(timespec="seconds")
                    ),
                    segment_difference_divergence_kind=segment.divergence_kind,
                )
            )

    point_order = {
        point_type: index for index, point_type in enumerate(POINT_REVIEW_ORDER)
    }
    point_events.sort(
        key=lambda event: (
            event.signal_role == "SEGMENT_DIFFERENCE_1M",
            point_order[event.bs_type],
        )
    )

    for code in sorted(holdings):
        state = states.get(code)
        if state is None or str(state.big_dir() or "neutral") != "down":
            continue
        last_big = getattr(state, "last_big", None)
        signal_time = "" if last_big is None else _aware_datetime(last_big).isoformat()
        context_events.append(
            StrictRealtimeMonitorEvent(
                code=code,
                name=str(names.get(code, code)),
                side="risk",
                kind="strict_30m_context_warning",
                bs_type="",
                signal_time=signal_time,
                price=float(getattr(state, "last_px", 0.0) or 0.0),
                big_dir="down",
                reason=f"strict_{getattr(state, 'big_level', '30m')}_down",
                op_level=str(getattr(state, "op_level", "5m") or "5m"),
                mid_level=str(getattr(state, "mid_level", "1m") or "1m"),
                big_level=str(getattr(state, "big_level", "30m") or "30m"),
                mid_dir=str(state.mid_dir() or ""),
                evidence_id=f"big-down:{signal_time}",
                confirmed_time=signal_time,
                signal_role="CONTEXT_WARNING_30M",
            )
        )
    return point_events + context_events


__all__ = (
    "StrictPhysicalMonitorState",
    "StrictRealtimeMonitorEvent",
    "collect_strict_monitor_events",
)
