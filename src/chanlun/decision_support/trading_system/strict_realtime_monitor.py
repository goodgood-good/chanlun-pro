"""供只读实时监听使用的严格物理周期事实。

图表筛选入口、历史回放和本监听器共同使用 ``strict_cl_config`` 与
``build_screening_evidence`` 组成的同一决策核心。价格基准元数据缺失或结构
快照无效时必须形成可观察的刷新失败，绝不能回退为另一套买卖信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from chanlun import fun
from chanlun.core.cl import CL
from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.trading_system.models import StructuralPoint
from chanlun.decision_support.trading_system.runtime_config import (
    StrictSnapshotPriceMetadata,
    strict_cl_config,
    strict_snapshot_price_metadata,
)
from chanlun.decision_support.trading_system.screening_structure import (
    build_screening_evidence,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from chanlun.exchange.price_basis import copy_price_basis_metadata
from chanlun.exchange.kline_completion import drop_unclosed_last_bar


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
    if evidence is None or not evidence.structure.levels:
        return "neutral"
    level = evidence.structure.levels[-1]
    if level.trend_types:
        return str(level.trend_types[-1].direction)
    locked = tuple(unit for unit in level.units if unit.locked)
    return "neutral" if not locked else str(locked[-1].direction)


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
    op_level: str = "1m"
    big_level: str = "30m"
    mid_level: str = "5m"
    mid_dir: str = ""
    evidence_id: str = ""

    @property
    def identity(self) -> str:
        # 买卖点编号包含完整结构谱系，同一批已收盘源 K 线重建时可能合理变化。
        # 通知身份表示因果行情事件，而不是具体实现编号。
        return "|".join(
            (self.kind, self.code, self.bs_type or "-", self.signal_time or "-")
        )


@dataclass
class _FrequencyRuntime:
    cd: CL
    metadata: StrictSnapshotPriceMetadata
    strict_config_revision: str
    source_frame: pd.DataFrame
    evidence: StrictEvidenceResult | None = None


class StrictPhysicalMonitorState:
    """使用统一筛选决策核心的 1m/5m/30m 增量监听状态。"""

    WARMUP_DAYS_BY_FREQ = {
        "1m": 30,
        "5m": 120,
        "30m": 365,
    }
    MINIMUM_BARS_BY_FREQ = {
        "1m": 960,
        "5m": 480,
        "30m": 240,
        "d": 240,
    }

    def __init__(
        self,
        code: str,
        ex: object,
        op_level: str = "1m",
        big_level: str = "30m",
        mid_level: str | None = "5m",
    ) -> None:
        if not code:
            raise ValueError("monitor code is required")
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
        self.last_op: pd.Timestamp | None = None
        self.last_mid: pd.Timestamp | None = None
        self.last_big: pd.Timestamp | None = None
        self.last_px = 0.0
        self.seen: set[tuple[str, ...]] = set()
        self._op_baseline_initialized = False
        self.consecutive_refresh_failures = 0
        self.consecutive_warmup_incomplete = 0
        self.warmup_ready = False
        op_minutes = int(op_level[:-1]) if op_level.endswith("m") else 5
        self.signal_freshness = pd.Timedelta(minutes=max(op_minutes * 40, 60))
        self._runtime_by_frequency: dict[str, _FrequencyRuntime] = {}

    def _fetch_klines(self, frequency: str, last: pd.Timestamp | None):
        if last is None:
            warmup_days = self.WARMUP_DAYS_BY_FREQ.get(frequency)
            if warmup_days:
                start = (pd.Timestamp.now() - pd.Timedelta(days=warmup_days)).strftime(
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

    def _closed_frame(self, raw: object, frequency: str) -> pd.DataFrame:
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
            point.available_at.isoformat(timespec="seconds"),
        )

    def _process_level(
        self,
        frequency: str,
        last_attr: str,
        observed_at: datetime,
    ) -> StrictEvidenceResult:
        last = getattr(self, last_attr)
        frame = self._closed_frame(self._fetch_klines(frequency, last), frequency)
        metadata = strict_snapshot_price_metadata(frame)
        runtime = self._runtime_by_frequency.get(frequency)
        if runtime is not None and runtime.metadata != metadata:
            # 除权除息或数据源批次改变了结构价格基准。只回放五日增量尾部会混入
            # 不可比较的价格，因此必须重新取得完整预热窗口。
            frame = self._closed_frame(self._fetch_klines(frequency, None), frequency)
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

        # 从完整保留的源行情帧原子重建。待定尾部锁定时，严格结构可能修订历史
        # 编号与中枢；若只输入新增行，所得状态会不同于通知证据使用的全图重算。
        candidate = self._new_runtime(frequency, metadata, source_frame)
        candidate.cd.process_klines(source_frame)
        candidate.evidence = build_screening_evidence(
            candidate.cd,
            source_closed_at=source_closed_at,
            structure_price_quantum=metadata.structure_price_quantum,
            price_basis_revision=metadata.price_basis_revision,
            strict_config_revision=candidate.strict_config_revision,
        )
        self._runtime_by_frequency[frequency] = candidate
        setattr(self, last_attr, latest)
        return candidate.evidence

    def refresh(self) -> list[StructuralPoint]:
        self.warmup_ready = False
        observed_at = datetime.now(CN)
        baseline_only = not self._op_baseline_initialized
        try:
            op = self._process_level(self.op_level, "last_op", observed_at)
            big = self._process_level(self.big_level, "last_big", observed_at)
            mid = (
                self._process_level(self.mid_level, "last_mid", observed_at)
                if self.mid_level
                else None
            )
        except _WarmupIncomplete:
            self.consecutive_warmup_incomplete += 1
            return []
        self.warmup_ready = True
        self.consecutive_warmup_incomplete = 0

        points = extract_confirmed_points(
            op,
            code=self.code,
            source_frequency=self.op_level,
            as_of=observed_at,
        )
        op_runtime = self._runtime_by_frequency[self.op_level]
        klines = op_runtime.cd.get_src_klines()
        if klines:
            last_kline = klines[-1]
            self.last_px = float(last_kline.c)

        output: list[StructuralPoint] = []
        for point in points:
            occurrence_key = self._point_occurrence_key(point)
            if occurrence_key in self.seen:
                continue
            self.seen.add(occurrence_key)
            if baseline_only:
                continue
            lag = pd.Timestamp(self.last_op) - pd.Timestamp(point.available_at)
            if pd.Timedelta(0) <= lag <= self.signal_freshness:
                output.append(point)
        self._op_baseline_initialized = True
        # 为诊断调用方和方向门保留显式引用。
        if mid is not None and _strict_direction(mid) not in {"up", "down", "neutral"}:
            raise AssertionError("invalid strict middle direction")
        if _strict_direction(big) not in {"up", "down", "neutral"}:
            raise AssertionError("invalid strict high direction")
        return output

    def refresh_chart_levels(self) -> bool:
        """只刷新已完成的 1m/5m/30m 展示周期。

        通知图片属于复核材料，不是一次决策调用。图片必须展示同一物理周期核心，
        但不能要求或绕过独立的日线风险门。尤其是，未完成的 QMT 日线仍会被
        ``refresh()`` 拒绝，而图表渲染仍可使用三个已经完整收盘的盘中周期。
        """

        self.warmup_ready = False
        observed_at = datetime.now(CN)
        try:
            self._process_level(self.op_level, "last_op", observed_at)
            self._process_level(self.big_level, "last_big", observed_at)
            if self.mid_level:
                self._process_level(self.mid_level, "last_mid", observed_at)
        except _WarmupIncomplete:
            self.consecutive_warmup_incomplete += 1
            return False
        self.warmup_ready = True
        self.consecutive_warmup_incomplete = 0
        return True

    def evidence(self, frequency: str) -> StrictEvidenceResult | None:
        runtime = self._runtime_by_frequency.get(frequency)
        return None if runtime is None else runtime.evidence

    def chart_data(self, frequency: str) -> CL | None:
        """返回本监听器实际消费的对应周期计算对象。

        按约定仅向展示代码只读暴露该对象。通知渲染不能重建第二条信号路径，
        也不能修改此状态；它只负责展示已经刷新的结构。
        """

        runtime = self._runtime_by_frequency.get(str(frequency))
        return None if runtime is None else runtime.cd

    def confirmed_point_occurrence(
        self,
        point_type: str,
        signal_time: str,
        *,
        frequency: str = "1m",
    ) -> StructuralPoint | None:
        """从当前精确计算的图表快照中解析通知标记。"""

        evidence = self.evidence(frequency)
        if evidence is None or not point_type or not signal_time:
            return None
        try:
            target = pd.Timestamp(signal_time)
        except (TypeError, ValueError):
            return None
        if target.tzinfo is None:
            return None
        now = datetime.now(CN)
        target_datetime = target.to_pydatetime()
        as_of = max(now, target_datetime.astimezone(CN))
        points = extract_confirmed_points(
            evidence,
            code=self.code,
            source_frequency=frequency,
            as_of=as_of,
        )
        target_utc = target.tz_convert("UTC")
        return next(
            (
                point
                for point in points
                if point.point_type == point_type
                and pd.Timestamp(point.available_at).tz_convert("UTC") == target_utc
            ),
            None,
        )

    def big_dir(self) -> str:
        return _strict_direction(self.evidence(self.big_level))

    def mid_dir(self) -> str:
        return (
            ""
            if not self.mid_level
            else _strict_direction(self.evidence(self.mid_level))
        )

def collect_strict_monitor_events(
    states: Mapping[str, object],
    *,
    names: Mapping[str, str] | None = None,
    holdings: set[str] | None = None,
) -> list[StrictRealtimeMonitorEvent]:
    """刷新各状态，并且只把严格确认的买卖点映射为通知。"""

    names = names or {}
    holdings = holdings or set()
    signals: dict[str, Iterable[StructuralPoint]] = {}
    log = fun.get_logger()
    for code, state in states.items():
        try:
            signals[code] = state.refresh()
            state.consecutive_refresh_failures = 0
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

    buys: list[StrictRealtimeMonitorEvent] = []
    others: list[StrictRealtimeMonitorEvent] = []
    for code, points in signals.items():
        state = states[code]
        big_dir = str(state.big_dir() or "neutral")
        mid_dir = str(state.mid_dir() or "")
        op_level = str(getattr(state, "op_level", "1m") or "1m")
        mid_level = str(getattr(state, "mid_level", "") or "")
        big_level = str(getattr(state, "big_level", "30m") or "30m")
        for point in points:
            side = str(point.side)
            point_type = str(point.point_type)
            if side not in {"buy", "sell"}:
                continue
            if side == "buy" and big_dir == "down":
                continue
            event = StrictRealtimeMonitorEvent(
                code=code,
                name=str(names.get(code, code)),
                side=side,
                kind=f"strict_{side}_point",
                bs_type=point_type,
                signal_time=point.available_at.isoformat(timespec="seconds"),
                price=float(point.structure_anchor_price),
                big_dir=big_dir,
                reason=f"strict_confirmed_{point_type}",
                op_level=op_level,
                mid_level=mid_level,
                big_level=big_level,
                mid_dir=mid_dir,
                evidence_id=point.point_id,
            )
            (buys if side == "buy" else others).append(event)

    for code in sorted(holdings):
        state = states.get(code)
        if state is None or str(state.big_dir() or "neutral") != "down":
            continue
        last_big = getattr(state, "last_big", None)
        signal_time = "" if last_big is None else _aware_datetime(last_big).isoformat()
        others.append(
            StrictRealtimeMonitorEvent(
                code=code,
                name=str(names.get(code, code)),
                side="exit",
                kind="big_down_exit",
                bs_type="",
                signal_time=signal_time,
                price=float(getattr(state, "last_px", 0.0) or 0.0),
                big_dir="down",
                reason=f"strict_{getattr(state, 'big_level', '30m')}_down",
                op_level=str(getattr(state, "op_level", "1m") or "1m"),
                mid_level=str(getattr(state, "mid_level", "") or ""),
                big_level=str(getattr(state, "big_level", "30m") or "30m"),
                mid_dir=str(state.mid_dir() or ""),
                evidence_id=f"big-down:{signal_time}",
            )
        )
    return buys + others


__all__ = (
    "StrictPhysicalMonitorState",
    "StrictRealtimeMonitorEvent",
    "collect_strict_monitor_events",
)
