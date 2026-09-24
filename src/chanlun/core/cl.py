# -*- coding: utf-8 -*-
"""本周期缠论运行时：K 线、分型、笔、线段、MACD 与线段中枢。"""

from __future__ import annotations
from collections import OrderedDict
from bisect import bisect_right

import datetime
import threading
from functools import wraps
from typing import Any, List, Union

import pandas as pd

from chanlun.core.bi_calculator import BiCalculator
from chanlun.core.cl_kline_process import CL_Kline_Process
from chanlun.core.kline_data_processor import KlineDataProcessor
from chanlun.core.macd import MACD
from chanlun.core.stroke_observations import StrokeComponentObservations
from chanlun.core.macd_htf import (
    CausalPartialHigherMACDCalculator,
    level_plus_one,
)
from chanlun.core.strict_structure.base_profile import (
    strict_base_config,
    strict_base_config_revision,
)
from chanlun.core.strict_structure.errors import StrictStructureContractError
from chanlun.core.types import BI, CLKline, FX, ICL, Kline, XD
from chanlun.core.xd_calculator import XdCalculator


def _strict_runtime_locked(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._strict_evidence_lock:
            return method(self, *args, **kwargs)

    return wrapper


def _strict_contract_boundary(method):
    """只在严格证据构建边界内转换通用不变量异常。"""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except StrictStructureContractError:
            raise
        except (ValueError, TypeError) as exc:
            raise StrictStructureContractError(str(exc)) from exc

    return wrapper


_RUNTIME_METADATA_KEYS = frozenset(
    {
        "structure_price_quantum",
        "price_basis_revision",
        "strict_base_profile_revision",
        "strict_config_revision",
    }
)


def _production_config(config: dict | None) -> dict[str, object]:
    """绑定唯一固定基础算法，只接受元数据和作用域字段。"""

    requested = {} if config is None else dict(config)
    base = strict_base_config()
    unknown = set(requested) - set(base) - _RUNTIME_METADATA_KEYS
    if unknown:
        raise ValueError(
            "unsupported CL configuration fields: " + ", ".join(sorted(unknown))
        )
    conflicts = {
        key: (base[key], requested[key])
        for key in set(requested) & set(base)
        if requested[key] != base[key]
    }
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise ValueError(f"production base structure configuration is fixed: {names}")
    configured_revision = requested.get("strict_base_profile_revision")
    if (
        configured_revision is not None
        and configured_revision != strict_base_config_revision()
    ):
        raise ValueError("strict base profile revision mismatch")
    result: dict[str, object] = dict(base)
    result.update(
        {key: requested[key] for key in _RUNTIME_METADATA_KEYS if key in requested}
    )
    return result


class CL(ICL):
    """以唯一严格证据为权威的生产缠论状态。"""

    _PICKLE_SCHEMA = "chanlun-analysis-cl-v15"
    _PICKLE_STATE_FIELDS = frozenset(
        {
            "code",
            "frequency",
            "config",
            "start_datetime",
            "market",
            "kline_processor",
            "cl_kline_processor",
            "macd_calculator",
            "bi_calculator",
            "xd_calculator",
            "stroke_observations",
            "_stroke_closed_through",
            "_stroke_close_times",
            "_strict_htf_macd_by_level",
            "_strict_htf_macd_calculators",
            "_strict_structure_memo",
            "_strict_unit_registry",
            "_strict_price_quantum_value",
        }
    )

    def __init__(
        self,
        code: str,
        frequency: str,
        config: Union[dict, None] = None,
        start_datetime: datetime.datetime = None,
        market: str = "",
    ):
        if not isinstance(code, str) or not code:
            raise ValueError("CL code is required")
        if not isinstance(frequency, str) or not frequency:
            raise ValueError("CL frequency is required")
        if not isinstance(market, str) or not market.strip():
            raise ValueError("CL market is required")
        self.code = code
        self.frequency = frequency
        self.config = _production_config(config)
        self.start_datetime = start_datetime
        self.market = market.strip().lower()

        self.kline_processor = KlineDataProcessor(start_datetime)
        self.cl_kline_processor = CL_Kline_Process()
        self.macd_calculator = MACD()
        self.bi_calculator = BiCalculator()
        self.xd_calculator = XdCalculator()
        self.stroke_observations = StrokeComponentObservations()
        self._stroke_closed_through = None
        self._stroke_close_times = {}

        self._strict_htf_macd_by_level: dict[int, dict] = {}
        self._strict_htf_macd_calculators: dict[
            int, CausalPartialHigherMACDCalculator
        ] = {}
        self._strict_structure_memo: dict[object, object] = {}
        self._strict_center_prefix_cache = OrderedDict()
        self._strict_unit_registry = None
        self._strict_price_quantum_value = None
        self._strict_evidence_lock = threading.RLock()

    def __getstate__(self):
        state = dict(self.__dict__)
        state.pop("_strict_evidence_lock", None)
        state.pop("_strict_center_prefix_cache", None)
        if set(state) != self._PICKLE_STATE_FIELDS:
            raise ValueError("strict CL pickle state is invalid")
        return {"_pickle_schema": self._PICKLE_SCHEMA, **state}

    def __setstate__(self, state: dict):
        if (
            not isinstance(state, dict)
            or state.get("_pickle_schema") != self._PICKLE_SCHEMA
            or set(state) != self._PICKLE_STATE_FIELDS | {"_pickle_schema"}
        ):
            raise ValueError("strict CL pickle schema is invalid")
        current = dict(state)
        current.pop("_pickle_schema")
        # 算法配置变更后不能继续复用旧笔、端点栈或其缓存字段。
        # __init__ 不参与反序列化，因此必须在恢复前重新校验固定配置。
        current["config"] = _production_config(current["config"])
        self.__dict__.update(current)
        self._strict_center_prefix_cache = OrderedDict()
        self._strict_evidence_lock = threading.RLock()

    @_strict_runtime_locked
    def process_klines(self, klines: pd.DataFrame, *, last_bar_closed: bool = False):
        """行情快照/增量入口；默认末根仍在形成，历史修订显式重建全部依赖。"""
        klines = self._without_auction(klines)
        replacement = self._history_replacement(klines)
        if replacement is not None:
            replacement.attrs.update(klines.attrs)
            last_bar_closed |= (self._stroke_closed_through is not None and
                                replacement.date.iloc[-1] <= self._stroke_closed_through)
            return self.process_klines_batch(replacement, last_bar_closed=last_bar_closed)
        src_klines = self.kline_processor.process_kline(klines)
        return self._process_src_klines(
            src_klines, last_bar_closed=last_bar_closed,
            bar_close_offset=self._bar_close_offset(klines),
        )

    @_strict_runtime_locked
    def process_klines_batch(self, klines: pd.DataFrame, *, last_bar_closed: bool = True):
        """一次性分析完整存量快照；成功后替换本实例的旧计算状态。

        不继承先前实时状态的端点选择、确认登记或中枢缓存。后续行情
        可继续使用 process_klines / process_kline_values 增量更新。
        """
        fresh = type(self)(self.code, self.frequency, self.config,
                           self.start_datetime, self.market)
        fresh.process_klines(klines, last_bar_closed=last_bar_closed)
        for name, value in fresh.__dict__.items():
            if name != "_strict_evidence_lock":
                setattr(self, name, value)
        return self

    @_strict_runtime_locked
    def process_validated_incremental_klines(self, klines: pd.DataFrame, *, last_bar_closed: bool = False):
        """处理已由图表缓存核对过历史前缀的完整行情帧。

        该入口省略历史纠错扫描和高周期 MACD 对同一旧前缀的重复比较；
        包含、笔、线段和严格结构使用相同计算器。调用方若发现
        滑窗或任意旧事实变化，必须丢弃本实例而不能使用此入口。
        """

        src_klines = self.kline_processor.process_kline(self._without_auction(klines))
        return self._process_src_klines(
            src_klines,
            validated_incremental_prefix=True,
            last_bar_closed=last_bar_closed,
            bar_close_offset=self._bar_close_offset(klines),
        )

    @_strict_runtime_locked
    def process_kline_values(self, date, open_, high, low, close, volume=0.0, *, bar_closed: bool = False):
        stamp = pd.Timestamp(date) if date is not None else None
        if stamp is not None and self.market == "a":
            local = stamp.tz_convert("Asia/Shanghai") if stamp.tzinfo is not None else stamp
            if local.hour == 9 and local.minute == 25:
                return self
        existing = self.kline_processor.klines
        if stamp is not None and existing and (
            stamp < existing[-1].date or
            self._stroke_closed_through is not None and stamp <= self._stroke_closed_through
        ):
            return self.process_klines(pd.DataFrame([dict(
                date=stamp, open=open_, high=high, low=low, close=close, volume=volume,
            )]), last_bar_closed=bar_closed)
        src_klines = self.kline_processor.process_kline_values(
            date, open_, high, low, close, volume
        )
        return self._process_src_klines(src_klines, last_bar_closed=bar_closed)

    def _without_auction(self, frame):
        if frame is None or frame.empty or self.market != "a":
            return frame
        dates = pd.to_datetime(frame.date)
        local = dates.dt.tz_convert("Asia/Shanghai") if dates.dt.tz is not None else dates
        mask = (local.dt.hour == 9) & (local.dt.minute == 25)
        return frame.loc[~mask] if mask.any() else frame

    def _history_replacement(self, frame):
        """不可信全量/重叠输入先核对历史；已认证的实时入口跳过此O(N)检查。

        修订是新历史版本，连同MACD、包含、笔和下游重新建立；缺少的旧行
        不代表删除，显式替换整个观察窗口使用process_klines_batch。
        """
        existing = self.kline_processor.klines
        if not existing or frame is None or frame.empty:
            return None
        dates = pd.to_datetime(frame.date)
        last = pd.Timestamp(existing[-1].date)
        if dates.dt.tz is not None and last.tzinfo is None:
            last = last.tz_localize(dates.dt.tz)
        elif dates.dt.tz is None and last.tzinfo is not None:
            last = last.tz_convert('UTC').tz_localize(None)
        mask = dates < last
        if self._stroke_closed_through is not None:
            mark = pd.Timestamp(self._stroke_closed_through)
            if dates.dt.tz is not None and mark.tzinfo is None:
                mark = mark.tz_localize(dates.dt.tz)
            elif dates.dt.tz is None and mark.tzinfo is not None:
                mark = mark.tz_convert('UTC').tz_localize(None)
            mask |= dates <= mark
        if not mask.any():
            return None
        # 使用同一输入规范化器比较，避免字符串数字、NaN填充等造成假修订。
        incoming = KlineDataProcessor(self.start_datetime)
        incoming.process_kline(frame.loc[mask])
        by_date = {pd.Timestamp(k.date): k for k in existing}
        changed = False
        for k in incoming.klines:
            old = by_date.get(pd.Timestamp(k.date))
            if old is None or (old.o, old.h, old.l, old.c, old.a) != (k.o, k.h, k.l, k.c, k.a):
                changed = True
                break
        if not changed:
            return None
        history = pd.DataFrame([dict(date=k.date, open=k.o, high=k.h, low=k.l, close=k.c, volume=k.a)
                                for k in existing])
        revised = frame.copy()
        revised['date'] = dates
        return pd.concat([history, revised], ignore_index=True).drop_duplicates(
            'date', keep='last',
        ).sort_values('date').reset_index(drop=True)

    def _bar_close_offset(self, frame):
        """Preserve source coordinates while recording actual minute closes."""
        from chanlun.exchange.kline_completion import frequency_to_minutes

        label = frame.attrs.get("bar_time_label", "end")
        if label not in {"start", "end"}:
            raise ValueError("bar_time_label must be start or end")
        minutes = frequency_to_minutes(self.frequency)
        return datetime.timedelta(minutes=minutes) if label == "start" and minutes is not None else datetime.timedelta()

    def _process_src_klines(
        self,
        src_klines: List[Kline],
        *,
        validated_incremental_prefix: bool = False,
        last_bar_closed: bool = False,
        bar_close_offset: datetime.timedelta = datetime.timedelta(),
    ):
        all_klines = self.kline_processor.klines
        previous_closed = self._stroke_closed_through
        if all_klines:
            closed = all_klines[-1].date if last_bar_closed else (
                all_klines[-2].date if len(all_klines) > 1 else None
            )
            if closed is not None and (previous_closed is None or closed > previous_closed):
                start = (bisect_right(all_klines, previous_closed, key=lambda k: k.date)
                         if previous_closed is not None else 0)
                stop = len(all_klines) if last_bar_closed else len(all_klines) - 1
                for k in all_klines[start:stop]:
                    # 明确收盘的输入在自身事件可用；默认盘中输入在下一根
                    # 到来时才获得前根收盘事实，不能把完成时间倒填一根。
                    self._stroke_close_times[k.date] = (
                        k.date + bar_close_offset if last_bar_closed else all_klines[k.index + 1].date
                    )
                self._stroke_closed_through = closed
        if not src_klines and previous_closed == self._stroke_closed_through:
            return self
        self.macd_calculator.process_macd(self.kline_processor.klines)
        self._compute_strict_htf_macd(
            validated_incremental_prefix=validated_incremental_prefix,
        )
        self.cl_kline_processor.process_cl_klines(self.kline_processor.klines)
        previous_confirmed = tuple(self.bi_calculator.confirmed_bis)
        self.bi_calculator.calculate(
            self.cl_kline_processor.cl_klines,
            source_revision=self.cl_kline_processor.structure_revision,
            validated_incremental_prefix=True,
            closed_through=self._stroke_closed_through,
            close_times=self._stroke_close_times,
        )
        self.xd_calculator.calculate(self.bi_calculator.contiguous_bis)
        self.stroke_observations.calculate(self.bi_calculator.stroke_components)
        current_confirmed = self.bi_calculator.confirmed_bis
        if len(current_confirmed) < len(previous_confirmed) or any(
            old is not new for old, new in zip(previous_confirmed, current_confirmed)
        ):
            # 端点或证据被重做时，依赖旧来源的中枢前缀缓存失效。
            # 正常追加承接笔保留既有对象，不触发这条历史修订路径。
            # 确认登记保留；重新选择的物理证据使用不同版本的单元 ID。
            self._strict_center_prefix_cache.clear()
        self._strict_structure_memo.clear()
        return self

    def _compute_strict_htf_macd(
        self,
        *,
        validated_incremental_prefix: bool = False,
    ) -> None:
        """更新仅供图表显示的首个高周期 MACD 覆盖层。

        此序列只用于指标展示，不参与笔、线段或本周期中枢的构造。
        """

        fast = int(self.config["idx_macd_fast"])
        slow = int(self.config["idx_macd_slow"])
        signal = int(self.config["idx_macd_signal"])
        target = level_plus_one(self.frequency)
        results: dict[int, dict] = {}
        if target is None:
            self._strict_htf_macd_by_level = results
            self._strict_htf_macd_calculators.clear()
            return
        calc = self._strict_htf_macd_calculators.get(0)
        if (
            calc is None
            or calc.frequency != self.frequency
            or calc.higher != target
            or calc.market != self.market
            or calc.fast != fast
            or calc.slow != slow
            or calc.signal != signal
        ):
            calc = CausalPartialHigherMACDCalculator(
                self.frequency,
                self.market,
                fast=fast,
                slow=slow,
                signal=signal,
                target_frequency=target,
            )
        self._strict_htf_macd_calculators = {0: calc}
        value = calc.update(
            self.kline_processor.klines,
            validated_incremental_prefix=validated_incremental_prefix,
        )
        if value is not None:
            results[0] = {**value}
        self._strict_htf_macd_by_level = results

    def get_code(self) -> str:
        return self.code

    def get_frequency(self) -> str:
        return self.frequency

    def get_config(self) -> dict:
        return dict(self.config)

    def get_src_klines(self) -> List[Kline]:
        return list(self.kline_processor.klines)

    def get_klines(self) -> List[Any]:
        return self.get_cl_klines()

    def get_cl_klines(self) -> List[CLKline]:
        return list(self.cl_kline_processor.cl_klines)

    def get_idx(self) -> dict:
        return self.macd_calculator.get_results()

    def get_fxs(self) -> List[FX]:
        return list(self.bi_calculator.fxs)

    def get_bis(self) -> List[BI]:
        """当前连续笔链，包含完成前缀与尚可重选的尾部。"""
        return list(self.bi_calculator.bis)

    def get_contiguous_bis(self) -> List[BI]:
        return self.bi_calculator.contiguous_bis

    def get_stroke_construction_state(self):
        result = self.bi_calculator.construction_state()
        result["conditional_segment_count"] = sum(len(x.segments) for x in self.stroke_observations.observations)
        return result

    def get_xds(self) -> List[XD]:
        return list(self.xd_calculator.xds)

    def get_chart_xds(self) -> List[XD]:
        """图表几何包含各待接续范围；信号仍使用 get_xds 的连续证据。"""
        return self.get_xds() + [
            xd for observation in self.stroke_observations.observations for xd in observation.segments
        ]

    @_strict_runtime_locked
    def get_segment_construction_state(self):
        """Expose the unassigned tail without inventing a segment endpoint."""
        lines = self.get_xds()
        pens = self.get_contiguous_bis()
        tail = self.xd_calculator.tail_state
        confirmed = [line for line in lines if line.is_done()]
        result = {
            "schema": "chanlun-segment-construction-v1",
            "status": "ready", "tail": None,
            "confirmed_segments": len(confirmed),
            "awaiting_confirmation_segments": sum(not line.is_done() and not line.forming for line in lines),
            "preview_segments": sum(bool(line.forming) for line in lines),
        }
        if tail.start_index is None or tail.start_index >= len(pens):
            result["status"] = "awaiting_pens" if not lines else "ready"
            return result
        remaining = pens[tail.start_index:]
        first = remaining[0]
        start_time = first.start.k.date
        source = [bar for bar in self.get_src_klines() if bar.date >= start_time]
        if not source:
            return result
        reason = tail.projection_reason or tail.reason
        unresolved = reason in {"tail-end-direction-conflict", "initial-pens-not-overlapping"}
        status = "unresolved" if unresolved else "forming" if tail.projection_index is not None else "awaiting_pens"
        result.update(status=status, tail={
            "status": status, "reason": reason, "boundary_search": tail.reason,
            "direction": tail.direction, "start_pen": tail.start_index,
            "start_time": int(start_time.timestamp()), "start_price": first.start.val,
            "observed_through": int(source[-1].date.timestamp()),
            "available_at": int(self._strict_as_of().timestamp()),
            "pen_count": len(remaining), "confirmed_pen_count": sum(p.is_done() for p in remaining),
            "bar_count": len(source), "high": max(bar.h for bar in source), "low": min(bar.l for bar in source),
            "candidate_end_pen": tail.candidate_index,
            "preview_end_pen": tail.projection_index,
            "end_confirmed": False,
        })
        return result

    @_strict_runtime_locked
    def get_conditional_centers(self):
        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("conditional_centers")
        if cached is None:
            cached = self.stroke_observations.centers(
                price_quantum=self._strict_price_quantum(), as_of=self._strict_as_of(),
                price_basis_revision=self._strict_price_basis_revision(),
                market_scope=(self.market, self.code, self.frequency),
            )
            self._strict_structure_memo["conditional_centers"] = cached
        return cached

    def _strict_as_of(self):
        values = self.get_src_klines()
        if not values:
            raise ValueError("strict structure requires source klines")
        last = values[-1].date
        return max(last, self._stroke_close_times.get(last, last))

    def _strict_price_quantum(self):
        from decimal import Decimal, DecimalException

        raw = self.config.get("structure_price_quantum")
        if raw is None:
            raise ValueError("strict structure requires price-basis quantum metadata")
        try:
            price_quantum = Decimal(str(raw))
        except (DecimalException, ValueError) as exc:
            raise ValueError(
                "strict structure requires positive finite price quantum"
            ) from exc
        if not price_quantum.is_finite() or price_quantum <= 0:
            raise ValueError("strict structure requires positive finite price quantum")
        if self._strict_price_quantum_value is None:
            self._strict_price_quantum_value = price_quantum
        elif self._strict_price_quantum_value != price_quantum:
            raise ValueError("price quantum changed within CL lifecycle")
        return price_quantum

    def _strict_price_basis_revision(self):
        value = self.config.get("price_basis_revision")
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError("strict structure requires price_basis_revision")
        return value

    def _strict_registry(self):
        from chanlun.core.strict_structure.unit_adapter import UnitLockRegistry

        price_basis_revision = self._strict_price_basis_revision()
        if self._strict_unit_registry is None:
            self._strict_unit_registry = UnitLockRegistry(
                price_basis_revision, scope=(self.market or "unknown", self.code, self.frequency)
            )
        elif self._strict_unit_registry.price_basis_revision != price_basis_revision:
            raise ValueError("price basis changed within CL lifecycle")
        return self._strict_unit_registry

    def _validate_strict_structure_metadata(self) -> None:
        """返回缓存事实前重新校验不可变运行时元数据。"""

        self._strict_registry()
        self._strict_price_quantum()

    def _strict_config_revision(self) -> str:
        value = self.config.get("strict_config_revision")
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
        ):
            raise ValueError("strict_config_revision must be a non-empty string")
        return value

    @_strict_runtime_locked
    @_strict_contract_boundary
    def get_segment_units(self):
        """Return causal segment units even before a structural level exists."""
        from chanlun.core.strict_structure.models import SourceKind
        from chanlun.core.strict_structure.unit_adapter import adapt_lines

        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("segment_units")
        if cached is not None:
            return cached
        units = adapt_lines(
            self.get_xds(), 0, SourceKind.SEGMENT,
            self._strict_price_quantum(), self._strict_as_of(),
            self._strict_registry(), constituent_lines=self.get_contiguous_bis(),
        )
        self._strict_structure_memo["segment_units"] = units
        return units

    @_strict_runtime_locked
    @_strict_contract_boundary
    def get_native_centers(self):
        """Calculate centers from the segments of this chart's own interval."""
        from chanlun.core.strict_structure.center_machine import calculate_centers
        from chanlun.core.strict_structure.models import SourceKind

        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("native_centers")
        if cached is not None:
            return cached
        units = self.get_segment_units()
        result = calculate_centers(units, 0, SourceKind.SEGMENT)
        self._strict_structure_memo["native_centers"] = result
        return result

    @_strict_runtime_locked
    def get_strict_structure_levels(self):
        from chanlun.core.strict_structure.level_catalog import recursive_level_labels
        from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
        from chanlun.core.strict_structure.strength import MacdStrengthProvider

        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("formal")
        if cached is not None:
            return cached
        price_basis_revision = self._strict_price_basis_revision()
        units = self.get_segment_units()
        labels = recursive_level_labels(self.get_frequency())
        engine = StrictRecursiveEngine(max_levels=len(labels))
        # 保持只覆写 ``max_levels`` 的研究/测试适配器兼容；缓存是运行时加速附件，
        # 不属于递归引擎的策略构造参数。
        engine.center_prefix_cache = self._strict_center_prefix_cache
        result = engine.calculate(
            units,
            price_basis_revision=price_basis_revision,
            strength=MacdStrengthProvider(self),
        )
        self._strict_structure_memo["formal"] = result
        return result

    @_strict_runtime_locked
    def get_stroke_observation_centers(self):
        from chanlun.core.strict_structure.center_machine import calculate_centers
        from chanlun.core.strict_structure.models import SourceKind
        from chanlun.core.strict_structure.unit_adapter import adapt_lines

        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("stroke_observation")
        if cached is not None:
            return cached
        self._strict_price_basis_revision()
        units = adapt_lines(
            self.get_contiguous_bis(),
            0,
            SourceKind.STROKE_OBSERVATION,
            self._strict_price_quantum(),
            self._strict_as_of(),
            self._strict_registry(),
        )
        result = calculate_centers(units, 0, SourceKind.STROKE_OBSERVATION)
        self._strict_structure_memo["stroke_observation"] = result
        return result

    def _strict_evidence_assembler(self):
        from chanlun.core.strict_structure.evidence_assembler import (
            StrictEvidenceAssembler,
        )
        from chanlun.core.strict_structure.strength import MacdStrengthProvider

        cached = self._strict_structure_memo.get("evidence_assembler")
        if cached is not None:
            return cached
        assembler = StrictEvidenceAssembler(
            symbol=self.get_code(),
            source_frequency=self.get_frequency(),
            source_closed_at=self._strict_as_of(),
            price_basis_revision=self._strict_price_basis_revision(),
            structure_price_quantum=self._strict_price_quantum(),
            strict_config_revision=self._strict_config_revision(),
            structure=self.get_strict_structure_levels(),
            strength=MacdStrengthProvider(self),
            projection_cache=self._strict_center_prefix_cache,
        )
        self._strict_structure_memo["evidence_assembler"] = assembler
        return assembler

    @_strict_runtime_locked
    def get_strict_points(self):
        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("confirmed_points")
        if cached is not None:
            return cached
        result = self._strict_evidence_assembler().confirmed_points()
        self._strict_structure_memo["confirmed_points"] = result
        return result

    @_strict_runtime_locked
    def get_strict_approaching_points(self):
        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("approaching_points")
        if cached is not None:
            return cached
        result = self._strict_evidence_assembler().approaching_points()
        self._strict_structure_memo["approaching_points"] = result
        return result

    @_strict_runtime_locked
    def get_strict_divergences(self):
        self._validate_strict_structure_metadata()
        cached = self._strict_structure_memo.get("divergences")
        if cached is not None:
            return cached
        result = self._strict_evidence_assembler().divergences()
        self._strict_structure_memo["divergences"] = result
        return result

    @_strict_runtime_locked
    @_strict_contract_boundary
    def get_strict_evidence(self):
        self._validate_strict_structure_metadata()
        strict_config_revision = self._strict_config_revision()
        cached = self._strict_structure_memo.get("evidence")
        if cached is not None:
            if cached.strict_config_revision != strict_config_revision:
                raise ValueError("strict config revision changed within CL lifecycle")
            return cached
        result = self._strict_evidence_assembler().evidence(
            stroke_center_observations=self.get_stroke_observation_centers(),
        )
        self._strict_structure_memo["evidence"] = result
        return result

    @_strict_runtime_locked
    def release_strict_evidence_cache(self) -> None:
        self._strict_structure_memo.clear()


__all__ = ("CL",)
