# -*- coding: utf-8 -*-
"""严格结构使用的因果高周期 MACD。

当前高周期桶只按当下已知收盘计算临时值；未来 K 线只能追加新样本，不能
改写历史证据。生产代码只保留这一种高周期力度口径。
"""

from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from chanlun.core.types import Kline


HIGHER_FREQ_MAP = {
    "1m": "5m",
    "5m": "30m",
    "30m": "d",
    "d": "w",
    "w": "m",
    "m": "y",
}

CAUSAL_PARTIAL_HTF_ALGORITHM = "causal-partial-htf"


def level_plus_one(frequency: str) -> Optional[str]:
    """Return the next frequency used by the HTF MACD chain."""

    return HIGHER_FREQ_MAP.get(frequency)


def level_plus_one_chain(frequency: str) -> tuple[str, ...]:
    """Return all higher frequencies reachable from ``frequency``."""

    out: list[str] = []
    current = frequency
    while True:
        current = level_plus_one(current)
        if current is None:
            return tuple(out)
        out.append(current)


MARKET_DAY_OFFSET_H = {
    "us": -5,
    "ny_futures": -5,
    "currency": 0,
    "currency_spot": 0,
    "fx": 0,
}


def _bucket_keys(
    t: np.ndarray, higher: str, market: Optional[str]
) -> Optional[np.ndarray]:
    """Map source-bar close timestamps to higher-timeframe buckets."""

    if higher == "5m":
        return t // 300
    if higher == "30m":
        return t // 1800
    offset = MARKET_DAY_OFFSET_H.get(market, 8) * 3600
    days = (t + offset) // 86400
    if higher == "d":
        return days
    if higher == "w":
        return (days + 3) // 7
    dt = t.astype("datetime64[s]")
    if higher == "m":
        return dt.astype("datetime64[M]").astype(np.int64)
    if higher == "y":
        return dt.astype("datetime64[Y]").astype(np.int64)
    return None


def _causal_bucket_keys(
    t: np.ndarray,
    higher: str,
    market: Optional[str],
) -> Optional[np.ndarray]:
    """Bucket close-time source bars for formal causal HTF evidence."""

    if higher == "5m":
        return (t - 1) // 300
    if higher == "30m":
        return (t - 1) // 1800
    return _bucket_keys(t, higher, market)


class CausalPartialHigherMACDCalculator:
    """Incremental causal partial-bucket HTF MACD calculator.

    The state before the current bucket contains closed HTF bars only. Each
    lower-timeframe close is applied once to that frozen state as the current
    bucket's provisional close. When a new bucket starts, the preceding
    bucket's last provisional state is promoted to the closed baseline.
    """

    algorithm = CAUSAL_PARTIAL_HTF_ALGORITHM

    def __init__(
        self,
        frequency: str,
        market: Optional[str] = None,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        china_mode: bool = True,
        target_frequency: Optional[str] = None,
    ) -> None:
        self.frequency = frequency
        self.market = market
        self.fast = int(fast)
        self.slow = int(slow)
        self.signal = int(signal)
        self.china_mode = bool(china_mode)
        chain = level_plus_one_chain(frequency)
        if target_frequency is not None and target_frequency not in chain:
            raise ValueError("target frequency must be above source frequency")
        self.higher = target_frequency or level_plus_one(frequency)
        self.fast_alpha = 2.0 / (self.fast + 1.0)
        self.slow_alpha = 2.0 / (self.slow + 1.0)
        self.signal_alpha = 2.0 / (self.signal + 1.0)
        self.reset()

    def reset(self) -> None:
        self.keys: list[int] = []
        self.dates: list[object] = []
        self.closes: list[float] = []
        self.dif: list[float] = []
        self.dea: list[float] = []
        self.hist: list[float] = []
        self._bucket_base: Optional[tuple[float, float, float]] = None
        self._last_partial: Optional[tuple[float, float, float]] = None

    def update(self, klines: List[Kline]) -> Optional[dict]:
        if self.higher is None:
            return None
        if not klines:
            self.reset()
            return None

        if not self._can_extend(klines):
            return self._rebuild(klines)

        old_n = len(self.keys)
        if len(klines) == old_n:
            if not self._replace_last(klines[-1]):
                return self._rebuild(klines)
        else:
            for kline in klines[old_n:]:
                if not self._append(kline):
                    return self._rebuild(klines)
        return self._result()

    def _can_extend(self, klines: List[Kline]) -> bool:
        old_n = len(self.keys)
        if old_n == 0 or len(klines) < old_n:
            return False
        preserved = old_n - 1 if len(klines) == old_n else old_n
        try:
            return all(
                klines[pos].date == self.dates[pos]
                and self._close_for(klines[pos]) == self.closes[pos]
                and self._key_for(klines[pos]) == self.keys[pos]
                for pos in range(preserved)
            )
        except (ValueError, AttributeError, IndexError, TypeError):
            return False

    def _key_for(self, kline: Kline) -> int:
        try:
            timestamp = int(kline.date.timestamp())
        except (ValueError, TypeError, AttributeError, OverflowError, OSError) as exc:
            raise ValueError("invalid kline date for causal HTF MACD") from exc
        keys = _causal_bucket_keys(
            np.array([timestamp], dtype=np.int64), self.higher, self.market
        )
        if keys is None or keys.size != 1:
            raise ValueError("unsupported causal HTF MACD bucket")
        return int(keys[0])

    @staticmethod
    def _close_for(kline: Kline) -> float:
        close = float(kline.c)
        if not math.isfinite(close):
            raise ValueError("causal HTF MACD close must be finite")
        return close

    def _rebuild(self, klines: List[Kline]) -> Optional[dict]:
        self.reset()
        previous_date = None
        for kline in klines:
            try:
                if previous_date is not None and kline.date <= previous_date:
                    self.reset()
                    return None
                if not self._append(kline):
                    self.reset()
                    return None
            except (TypeError, AttributeError):
                self.reset()
                return None
            previous_date = kline.date
        return self._result()

    def _append(self, kline: Kline) -> bool:
        try:
            key = self._key_for(kline)
            close = self._close_for(kline)
        except ValueError:
            return False
        if self.dates and kline.date <= self.dates[-1]:
            return False
        if self.keys and key < self.keys[-1]:
            return False

        if not self.keys:
            self._bucket_base = None
        elif key != self.keys[-1]:
            self._bucket_base = self._last_partial

        partial = self._partial(close, self._bucket_base)
        self.keys.append(key)
        self.dates.append(kline.date)
        self.closes.append(close)
        self._append_partial(partial)
        return True

    def _replace_last(self, kline: Kline) -> bool:
        try:
            key = self._key_for(kline)
            close = self._close_for(kline)
        except ValueError:
            return False
        if not self.keys or key != self.keys[-1] or kline.date != self.dates[-1]:
            return False
        partial = self._partial(close, self._bucket_base)
        self.closes[-1] = close
        self.dif[-1], self.dea[-1], self.hist[-1] = self._values(partial)
        self._last_partial = partial
        return True

    def _partial(
        self,
        close: float,
        base: Optional[tuple[float, float, float]],
    ) -> tuple[float, float, float]:
        if base is None:
            return (close, close, 0.0)
        base_fast, base_slow, base_dea = base
        ema_fast = self.fast_alpha * close + (1.0 - self.fast_alpha) * base_fast
        ema_slow = self.slow_alpha * close + (1.0 - self.slow_alpha) * base_slow
        dif = ema_fast - ema_slow
        dea = self.signal_alpha * dif + (1.0 - self.signal_alpha) * base_dea
        return (ema_fast, ema_slow, dea)

    def _values(
        self, partial: tuple[float, float, float]
    ) -> tuple[float, float, float]:
        ema_fast, ema_slow, dea = partial
        dif = ema_fast - ema_slow
        hist = dif - dea
        if self.china_mode:
            hist *= 2.0
        return dif, dea, hist

    def _append_partial(self, partial: tuple[float, float, float]) -> None:
        dif, dea, hist = self._values(partial)
        self.dif.append(dif)
        self.dea.append(dea)
        self.hist.append(hist)
        self._last_partial = partial

    def _result(self) -> dict:
        return {
            "dif": self.dif,
            "dea": self.dea,
            "hist": self.hist,
            "dates": tuple(self.dates),
            "known_at": tuple(self.dates),
            "bucket_keys": tuple(self.keys),
            "algorithm": self.algorithm,
            "source_frequency": self.frequency,
            "target_frequency": self.higher,
        }
