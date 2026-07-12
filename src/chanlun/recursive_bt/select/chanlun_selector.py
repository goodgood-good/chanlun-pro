"""Chanlun-text grounded A-share selector for live monitoring.

This module deliberately selects from structural buy points produced by the
same ``get_branch_bspoints`` signal path used by the recursive backtests.  It
does not build an arbitrary first-N universe from cached files.
"""
from __future__ import annotations

import glob
import os
import pickle
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

import numpy as np
import pandas as pd

from chanlun.decision_support import strategies as decision_strategies
from chanlun.decision_support.models import DecisionEvent, StrategyTrack

# Importing engine also registers the legacy ``recursive_backtest`` pickle
# module alias used by older cached Signal objects.
from chanlun.recursive_bt.engine.engine import Signal  # noqa: F401


INDEX_CODE = "SH.000001"
DEFAULT_BUY_CLASSES = (3, 2, 1)
DEFAULT_FUND_DATA = "D:/chanlun_pro/bt_data_fund_all_a"
DEFAULT_ROE_ANN_MIN = 8.0
# F-MID-1:比价 median 门的最小样本数。有效 value_score 样本不足此值时
# 关闭比价门(中位数在小样本下不稳、池规模变动会致选股抖动)。
MIN_COMPARISON_SAMPLES = 20


def _clean_buy_classes(values: Iterable[int | str]) -> tuple[int, ...]:
    classes: list[int] = []
    for value in values:
        try:
            cls = int(str(value).strip()[0])
        except Exception:
            continue
        if cls in (1, 2, 3) and cls not in classes:
            classes.append(cls)
    return tuple(classes) or DEFAULT_BUY_CLASSES


@dataclass(frozen=True)
class ASelectionConfig:
    bt_data: str
    fund_data: str = DEFAULT_FUND_DATA
    scan_limit: int = 0
    max_codes: int = 50
    lookback_bars: int = 48  # Round11 B2: 曾为3(15min)致盘后启动永远选不出候选
    buy_classes: tuple[int, ...] = DEFAULT_BUY_CLASSES
    require_three_systems: bool = True
    fundamental_roe_ann_min: float = DEFAULT_ROE_ANN_MIN
    require_big_not_down: bool = True
    require_mid_not_down: bool = True
    min_bars: int = 100

    def __post_init__(self) -> None:
        object.__setattr__(self, "scan_limit", max(int(self.scan_limit), 0))
        object.__setattr__(self, "max_codes", max(int(self.max_codes), 0))
        object.__setattr__(self, "lookback_bars", max(int(self.lookback_bars), 1))
        object.__setattr__(self, "buy_classes", _clean_buy_classes(self.buy_classes))
        object.__setattr__(self, "min_bars", max(int(self.min_bars), 1))
        object.__setattr__(
            self,
            "fundamental_roe_ann_min",
            float(self.fundamental_roe_ann_min),
        )


@dataclass(frozen=True)
class SelectionCandidate:
    code: str
    bs_type: str
    signal_time: str
    price: float
    big_dir: str
    signal_index: int
    daily_resonance: bool = False
    fund_ok: bool = False
    comparison_ok: bool = False
    roe_ann: float = 0.0
    growth: float = 0.0
    value_score: float = 0.0
    value_threshold: float = 0.0
    name: Optional[str] = None

    @property
    def buy_class(self) -> int:
        return _buy_class(self.bs_type)


class OriginalChanlunASelector:
    """Select A shares using original Chanlun buy-point structure.

    The rule is the intersection of three independent systems:
    - fundamental: quality and growth are available point-in-time;
    - comparison: quality-adjusted valuation beats the current universe median;
    - technical: operation level has a recent first/second/third class buy point,
      with higher levels not down.  Daily third-buy resonance is a priority boost.
    """

    def __init__(
        self,
        config: ASelectionConfig,
        *,
        trend_event_adapter: Callable[
            [SelectionCandidate, dict, object], DecisionEvent
        ]
        | None = None,
    ):
        if trend_event_adapter is not None and not callable(trend_event_adapter):
            raise TypeError("trend_event_adapter must be callable")
        self.config = config
        self.trend_event_adapter = trend_event_adapter

    def select(self) -> list[SelectionCandidate]:
        rows: list[
            tuple[
                SelectionCandidate,
                FundamentalSnapshot,
                dict | None,
                object | None,
            ]
        ] = []
        value_scores: list[float] = []
        files = self._cache_files()
        for file in files:
            code = os.path.basename(file)[:-4]
            if code == INDEX_CODE:
                continue
            data = self._load(file)
            if not data:
                continue
            snapshot = self._fundamental_snapshot(code, data)
            if snapshot.value_score > 0:
                value_scores.append(snapshot.value_score)
            selection = self._candidate_with_signal_from_symbol(code, data)
            if selection is not None:
                candidate, signal = selection
                rows.append(
                    (
                        candidate,
                        snapshot,
                        data if self.trend_event_adapter is not None else None,
                        signal if self.trend_event_adapter is not None else None,
                    )
                )

        # F-MID-1(audit/_round6_select_data.md):比价门用本次扫描全 universe 的截面
        # median,池规模小/变动时中位数翻转会致同一只票今天入选明天落选(与该票自身
        # 数据无关)。加最小样本门稳定化:样本 < MIN_COMPARISON_SAMPLES 时不启用比价门
        # (该批所有候选 comparison_ok=True),样本足够才用 value_score > median 判定。
        enable_comparison = len(value_scores) >= MIN_COMPARISON_SAMPLES
        threshold = float(np.median(value_scores)) if value_scores else 0.0
        candidates: list[SelectionCandidate] = []
        for candidate, snapshot, data, signal in rows:
            if not enable_comparison:
                comparison_ok = True
            else:
                comparison_ok = threshold > 0 and snapshot.value_score > threshold
            if self.config.require_three_systems and not (snapshot.fund_ok and comparison_ok):
                continue
            if self.trend_event_adapter is not None:
                if data is None or signal is None:
                    raise AssertionError("delegated selector row lost source facts")
                if not self._delegated_trend_accepted(
                    candidate,
                    data,
                    signal,
                    fund_ok=(
                        snapshot.fund_ok
                        if self.config.require_three_systems
                        else True
                    ),
                    comparison_ok=(
                        comparison_ok if self.config.require_three_systems else True
                    ),
                ):
                    continue
            candidates.append(
                SelectionCandidate(
                    code=candidate.code,
                    name=candidate.name,
                    bs_type=candidate.bs_type,
                    signal_time=candidate.signal_time,
                    price=candidate.price,
                    big_dir=candidate.big_dir,
                    signal_index=candidate.signal_index,
                    daily_resonance=candidate.daily_resonance,
                    fund_ok=snapshot.fund_ok,
                    comparison_ok=comparison_ok,
                    roe_ann=snapshot.roe_ann,
                    growth=snapshot.growth,
                    value_score=snapshot.value_score,
                    value_threshold=threshold,
                )
            )
        candidates.sort(key=self._sort_key)
        if self.config.max_codes > 0:
            candidates = candidates[: self.config.max_codes]
        return candidates

    def _cache_files(self) -> list[str]:
        files = sorted(glob.glob(os.path.join(self.config.bt_data, "*.pkl")))
        if self.config.scan_limit > 0:
            files = files[: self.config.scan_limit]
        return files

    @staticmethod
    def _load(file: str) -> Optional[dict]:
        try:
            with open(file, "rb") as fp:
                data = pickle.load(fp)
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _candidate_from_symbol(
        self,
        code: str,
        data: dict,
    ) -> Optional[SelectionCandidate]:
        selection = self._candidate_with_signal_from_symbol(code, data)
        return None if selection is None else selection[0]

    def _candidate_with_signal_from_symbol(
        self,
        code: str,
        data: dict,
    ) -> Optional[tuple[SelectionCandidate, object]]:
        raw_dates = data.get("dates")
        if raw_dates is None:
            return None
        dates = list(raw_dates)
        if len(dates) < self.config.min_bars:
            return None

        small_by_bar = data.get("small_by_bar") or {}
        if not isinstance(small_by_bar, dict):
            return None

        last_idx = len(dates) - 1
        first_idx = max(0, len(dates) - self.config.lookback_bars)
        matches = []
        seq = 0
        for idx in range(last_idx, first_idx - 1, -1):
            if not self._direction_ok(data, idx, last_idx):
                continue
            for sig in small_by_bar.get(idx, []) or []:
                bs_type = str(getattr(sig, "bs_type", ""))
                cls = _buy_class(bs_type)
                if cls not in self.config.buy_classes or not _is_buy(sig, bs_type):
                    continue
                rank = self._buy_rank(cls)
                matches.append((rank, -idx, seq, sig, idx))
                seq += 1

        if not matches:
            return None

        matches.sort()
        _rank, _neg_idx, _seq, sig, idx = matches[0]
        bs_type = str(getattr(sig, "bs_type", ""))
        price = _signal_price(sig, data, idx)
        big_dir = _dir_at(data, "big_dir_at", idx) or "neutral"
        signal_time = str(getattr(sig, "date", None) or dates[idx])
        candidate = SelectionCandidate(
            code=code,
            name=str(data.get("name") or code),
            bs_type=bs_type,
            signal_time=signal_time,
            price=price,
            big_dir=big_dir,
            signal_index=idx,
            daily_resonance=_daily_resonance(data, idx),
        )
        return candidate, sig

    def _delegated_trend_accepted(
        self,
        candidate: SelectionCandidate,
        data: dict,
        signal: object,
        *,
        fund_ok: bool,
        comparison_ok: bool,
    ) -> bool:
        if self.trend_event_adapter is None:
            raise RuntimeError("trend event adapter is not configured")
        expected_code = candidate.code
        expected_bs_type = candidate.bs_type
        expected_price = candidate.price
        expected_signal_at = _as_shanghai(
            pd.Timestamp(candidate.signal_time)
        ).to_pydatetime()
        last_idx = len(data["dates"]) - 1
        expected_bar_closed_at = _cache_bar_closed_at(data)
        expected_big_dir = _required_dir_at(data, "big_dir_at", last_idx)
        expected_mid_dir = _required_dir_at(data, "mid_dir_at", last_idx)
        expected_level = int(getattr(signal, "level", 0) or 0)
        expected_stop_below = getattr(signal, "structural_stop_below", None)
        expected_stop_above = getattr(signal, "structural_stop_above", None)
        event = self.trend_event_adapter(candidate, data, signal)
        ordered_levels = (
            sorted(
                event.levels,
                key=lambda level: (level.level, level.frequency),
                reverse=True,
            )
            if isinstance(event, DecisionEvent)
            else ()
        )
        event_big_dir = ordered_levels[0].direction if ordered_levels else None
        event_mid_dir = (
            ordered_levels[1].direction
            if len(ordered_levels) > 1
            else event_big_dir
        )
        if (
            not isinstance(event, DecisionEvent)
            or event.strategy_track is not StrategyTrack.TREND_CONTINUATION
            or event.code != expected_code
            or event.observed_at != expected_bar_closed_at
            or event.bar_closed_at != expected_bar_closed_at
            or event.signal.bs_type != expected_bs_type
            or event.signal.signal_at != expected_signal_at
            or event.signal.level != expected_level
            or event.signal.price != expected_price
            or event.signal.structural_stop_below != expected_stop_below
            or event.signal.structural_stop_above != expected_stop_above
            or event_big_dir != expected_big_dir
            or event_mid_dir != expected_mid_dir
        ):
            raise TypeError("trend event adapter returned malformed event")
        decision = decision_strategies.evaluate_trend_continuation(
            event,
            fund_ok=fund_ok,
            comparison_ok=comparison_ok,
        )
        if (
            not isinstance(decision, decision_strategies.CandidateDecision)
            or decision.track is not StrategyTrack.TREND_CONTINUATION
            or decision.event != event
        ):
            raise TypeError("trend evaluator returned malformed decision")
        return decision.accepted

    def _direction_ok(self, data: dict, signal_idx: int, last_idx: int) -> bool:
        if self.config.require_big_not_down:
            if _dir_at(data, "big_dir_at", signal_idx) == "down":
                return False
            if _dir_at(data, "big_dir_at", last_idx) == "down":
                return False
        if self.config.require_mid_not_down and "mid_dir_at" in data:
            if _dir_at(data, "mid_dir_at", signal_idx) == "down":
                return False
            if _dir_at(data, "mid_dir_at", last_idx) == "down":
                return False
        return True

    def _buy_rank(self, cls: int) -> int:
        try:
            return self.config.buy_classes.index(cls)
        except ValueError:
            return len(self.config.buy_classes)

    def _sort_key(self, candidate: SelectionCandidate) -> tuple:
        return (
            0 if candidate.daily_resonance else 1,
            self._buy_rank(candidate.buy_class),
            -candidate.signal_index,
            candidate.code,
        )

    def _fundamental_snapshot(self, code: str, data: dict) -> "FundamentalSnapshot":
        if not self.config.require_three_systems:
            return FundamentalSnapshot(fund_ok=True, comparison_ok=True)
        dates = data.get("dates")
        close = data.get("close")
        if dates is None or close is None or len(dates) == 0 or len(close) == 0:
            return FundamentalSnapshot()
        try:
            asof = _as_shanghai(pd.Timestamp(list(dates)[-1]))
        except Exception:
            # dates[-1] 不可解析(半写/旧格式/腐坏缓存)时按"基本面不可用"降级(与上方
            # dates/close 为空的早退同语义),而非裸 DateParseError 冒泡崩掉整个 select()
            # →live_monitor 启动路径(无 try/except)进程崩溃且重启在同一坏文件上复崩。
            return FundamentalSnapshot()
        try:
            last_close = float(close[-1])
        except Exception:
            last_close = 0.0
        return _load_fundamental_snapshot(
            code=code,
            fund_data=self.config.fund_data,
            asof=asof,
            close=last_close,
            roe_ann_min=self.config.fundamental_roe_ann_min,
        )


@dataclass(frozen=True)
class FundamentalSnapshot:
    fund_ok: bool = False
    comparison_ok: bool = False
    roe_ann: float = 0.0
    growth: float = 0.0
    value_score: float = 0.0


def _buy_class(bs_type: str) -> int:
    try:
        return int(str(bs_type)[0])
    except Exception:
        return 0


def _is_buy(sig: object, bs_type: str) -> bool:
    is_buy = getattr(sig, "is_buy", None)
    if is_buy is not None:
        return bool(is_buy)
    return bs_type in {"1buy", "2buy", "3buy"}


def _dir_at(data: dict, key: str, idx: int) -> str:
    series = data.get(key)
    if series is None:
        return "neutral"
    if idx < 0 or idx >= len(series):
        return "neutral"
    return str(series[idx] or "neutral")


def _required_dir_at(data: dict, key: str, idx: int) -> str:
    series = data.get(key)
    if series is None or isinstance(series, (str, bytes)):
        raise TypeError("trend event adapter requires complete cache facts")
    try:
        value = series[idx]
    except (IndexError, KeyError, TypeError):
        raise TypeError(
            "trend event adapter requires complete cache facts"
        ) from None
    if value not in {"up", "down", "neutral"}:
        raise TypeError("trend event adapter requires complete cache facts")
    return str(value)


def _cache_bar_closed_at(data: dict):
    raw_dates = data.get("dates")
    if raw_dates is None or isinstance(raw_dates, (str, bytes)):
        raise TypeError("trend event adapter requires complete cache facts")
    try:
        dates = tuple(_as_shanghai(pd.Timestamp(value)) for value in raw_dates)
    except Exception:
        raise TypeError(
            "trend event adapter requires complete cache facts"
        ) from None
    if len(dates) < 2:
        raise TypeError("trend event adapter requires complete cache facts")
    deltas = []
    for previous, current in zip(dates, dates[1:]):
        delta = current - previous
        if delta <= pd.Timedelta(0):
            raise TypeError("trend event adapter requires complete cache facts")
        deltas.append(delta.value)
    counts = {value: deltas.count(value) for value in set(deltas)}
    step_nanos = min(counts, key=lambda value: (-counts[value], value))
    return (dates[-1] + pd.Timedelta(step_nanos, unit="ns")).to_pydatetime()


def _daily_resonance(data: dict, idx: int) -> bool:
    d3 = data.get("d3_ok")
    if d3 is not None and idx < len(d3):
        try:
            return bool(d3[idx])
        except Exception:
            return False
    return False


def _signal_price(sig: object, data: dict, idx: int) -> float:
    try:
        price = float(getattr(sig, "price", 0) or 0)
        if price > 0:
            return price
    except Exception:
        pass
    close = data.get("close")
    if close is None:
        return 0.0
    if idx < len(close):
        try:
            return float(close[idx])
        except Exception:
            pass
    return 0.0


def _load_fundamental_snapshot(
    *,
    code: str,
    fund_data: str,
    asof: pd.Timestamp,
    close: float,
    roe_ann_min: float,
) -> FundamentalSnapshot:
    path = os.path.join(fund_data, f"{code}.pkl")
    try:
        with open(path, "rb") as fp:
            data = pickle.load(fp)
    except Exception:
        return FundamentalSnapshot()

    reports = data.get("reports") or []
    latest = None
    for report in reports:
        ann = report.get("anntime") or ""
        if not ann:
            continue
        try:
            available = pd.Timestamp(ann, tz="Asia/Shanghai") + pd.Timedelta("1D")
        except Exception:
            continue
        if available <= asof:
            latest = report
        else:
            break
    if latest is None:
        return FundamentalSnapshot()

    roe = _float_or_zero(latest.get("roe"))
    roe_ann = roe * 4.0
    growth = max(
        _float_or_default(latest.get("rev_inc"), -999.0),
        _float_or_default(latest.get("np_inc"), -999.0),
    )
    bps = _float_or_zero(latest.get("bps"))
    value_score = roe_ann * bps / close if bps > 0 and close > 0 else 0.0
    return FundamentalSnapshot(
        fund_ok=roe_ann > roe_ann_min and growth > 0,
        roe_ann=roe_ann,
        growth=growth,
        value_score=value_score,
    )


def _as_shanghai(ts: pd.Timestamp) -> pd.Timestamp:
    if ts.tzinfo is None:
        return ts.tz_localize("Asia/Shanghai")
    return ts.tz_convert("Asia/Shanghai")


def _float_or_zero(value) -> float:
    return _float_or_default(value, 0.0)


def _float_or_default(value, default: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out else default
