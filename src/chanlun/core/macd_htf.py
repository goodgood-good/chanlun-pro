# -*- coding: utf-8 -*-
"""高周期 MACD —— 背驰力度度量的工程近似。

本模块是工程取舍：项目以线段为最低级别走势类型，线段跨多根 K 线、本质是
高一层结构，度量其背驰力度时若用原生 K 线 MACD 过于细碎，导致 1/2 类买卖点
与背驰难以稳定识别。故按 K 线时间戳把当前周期重采样到高一周期
（``1m→5m, 5m→30m, 30m→d, d→w, w→m, m→y``），在高周期 close 上算 MACD，
再线性插值回每根 K 线，供 ``query_macd_ld`` 取用。可经
``CL_CFG['macd_ld_use_htf']=False`` 关闭回退原生 MACD。

口径与 web 端 ``apply_higher_macd_to_chart_data`` 一致：真实重采样 + 桶末
锚点线性插值。区别仅在高周期 MACD 用本仓自带的 ``core.macd.MACD``
（与引擎其余处一致；早期不足 ``slow+signal`` 根处填 0 而非 NaN，故插值
结果天然无 NaN）。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from chanlun.core.types import Kline
from chanlun.core.macd import MACD

# 当前频率 → 高一级频率。无对照（如已是最高周期）→ 不做高周期 MACD。
HIGHER_FREQ_MAP = {
    "1m": "5m",
    "5m": "30m",
    "30m": "d",
    "d": "w",
    "w": "m",
    "m": "y",
}

# 跨 UTC 日界市场的日级分桶时区偏移（小时）；其余默认 +8。
# 仅影响 30m→d 及以上的分桶；1m→5m / 5m→30m 纯按时间戳整除，不需要它。
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
    """把每根 K 线的 unix 秒时间戳映射成「高一级周期」的分桶 key。

    key 只用于分组：同一高周期 K 线内 key 相同、随时间单调不减即可。
    """
    if higher == "5m":
        return t // 300
    if higher == "30m":
        return t // 1800
    offset = MARKET_DAY_OFFSET_H.get(market, 8) * 3600
    days = (t + offset) // 86400
    if higher == "d":
        return days
    if higher == "w":
        # 1970-01-01 是周四，+3 后整除 7 即周一对齐的周序号
        return (days + 3) // 7
    dt = t.astype("datetime64[s]")
    if higher == "m":
        return dt.astype("datetime64[M]").astype(np.int64)
    if higher == "y":
        return dt.astype("datetime64[Y]").astype(np.int64)
    return None


def compute_higher_macd(
    klines: List[Kline],
    frequency: str,
    market: Optional[str] = None,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    china_mode: bool = True,
) -> Optional[dict]:
    """计算高一周期 MACD，并线性插值回每根当前周期 K 线。

    Args:
        klines: 当前周期 K 线（需带 ``date``，用于按时间戳重采样）。
        frequency: 当前 K 线周期（``1m`` / ``5m`` / ``30m`` / ``d`` / ``w`` / ``m``）。
        market: 市场标识，仅 30m→d 及以上的日级分桶用于时区偏移；缺省按 +8。
        fast / slow / signal: MACD 参数。
        china_mode: 同 ``core.macd.MACD``，国内口径 hist = (DIF-DEA)*2。

    Returns:
        ``{'dif': [...], 'dea': [...], 'hist': [...]}``，三个数组均与 ``klines``
        等长（per-bar）。以下情形返回 ``None``，调用方据此回退原生 MACD：
        无高周期对照、K 线为空、时间戳不可用、高周期桶数不足 ``slow+signal``。
    """
    higher = HIGHER_FREQ_MAP.get(frequency)
    if higher is None:
        return None
    n = len(klines)
    if n == 0:
        return None

    try:
        # 用 datetime.timestamp() 取 POSIX 秒：tz-aware / naive 都适用，且
        # 不触发 numpy 对 tz-aware datetime64 解析的 DeprecationWarning。
        # 本地时区偏移恒为整小时（3600s 的整数倍，也是 300/1800 的整数倍），
        # 故 5m/30m 分桶不受影响；日级以上分桶由 market 偏移单独处理。
        t = np.array([k.date.timestamp() for k in klines], dtype=np.int64)
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        # date 缺失/非法 → 无法按时间戳重采样
        return None
    if t.size != n:
        return None

    keys = _bucket_keys(t, higher, market)
    if keys is None:
        return None

    # keys 单调不减 → 桶边界 = key 变化处；bucket_idx[i] = 第 i 根所属桶序号
    boundaries = np.concatenate(([True], keys[1:] != keys[:-1]))
    bucket_idx = np.cumsum(boundaries) - 1
    bucket_count = int(bucket_idx[-1]) + 1
    # MACD 至少需要 slow+signal 根高周期 K 线才有有意义的输出
    if bucket_count <= slow + signal:
        return None

    # 每桶取最后一根 bar 的下标（后写覆盖前写 → 落在该桶最后一根）
    last_pos = np.zeros(bucket_count, dtype=np.int64)
    last_pos[bucket_idx] = np.arange(n)

    # 高周期逐桶 MACD（复用引擎自带 MACD；其 dif/dea 早期填 0 不填 NaN）
    bucket_klines = [
        Kline(
            index=i,
            date=None,
            h=0.0,
            l=0.0,
            o=0.0,
            c=float(klines[pos].c),
            a=0.0,
        )
        for i, pos in enumerate(last_pos)
    ]
    macd = MACD(
        fast_period=fast,
        slow_period=slow,
        signal_period=signal,
        china_mode=china_mode,
    )
    macd.process_macd(bucket_klines)
    dif_b = np.asarray(macd.dif, dtype=float)
    dea_b = np.asarray(macd.dea, dtype=float)
    if dif_b.size != bucket_count or dea_b.size != bucket_count:
        return None

    # 桶内逐根：把高周期 MACD 视作「定位在每个桶末根」的锚点，相邻锚点间
    # 按 bar 位置线性插值。桶末根落在锚点上 → 严格等于高周期 MACD；首个
    # 锚点之前的 bar 由 np.interp 左端外推为锚点首值（早期高周期 MACD 本就
    # ≈0，外推无害）。hist 由插值后的 dif/dea 相减得到，三线自洽。
    anchor_x = last_pos.astype(float)
    bars = np.arange(n, dtype=float)
    dif = np.interp(bars, anchor_x, dif_b)
    dea = np.interp(bars, anchor_x, dea_b)
    hist = dif - dea
    if china_mode:
        hist = hist * 2.0

    return {"dif": dif.tolist(), "dea": dea.tolist(), "hist": hist.tolist()}


class HigherMACDCalculator:
    """Incremental equivalent of ``compute_higher_macd``.

    The full helper is intentionally kept as the reference implementation.  This
    stateful calculator is for live/walk-forward CL objects: it keeps higher
    timeframe buckets and only rewrites the interpolation suffix affected by the
    latest lower-timeframe bar.
    """

    def __init__(
        self,
        frequency: str,
        market: Optional[str] = None,
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
        china_mode: bool = True,
    ):
        self.frequency = frequency
        self.market = market
        self.fast = int(fast)
        self.slow = int(slow)
        self.signal = int(signal)
        self.china_mode = bool(china_mode)
        self.higher = HIGHER_FREQ_MAP.get(frequency)
        self.macd = MACD(
            fast_period=self.fast,
            slow_period=self.slow,
            signal_period=self.signal,
            china_mode=self.china_mode,
        )
        self.keys: list[int] = []
        self.bucket_last_pos: list[int] = []
        self.bucket_klines: list[Kline] = []
        self.dif: list[float] = []
        self.dea: list[float] = []
        self.hist: list[float] = []
        self._result = {"dif": self.dif, "dea": self.dea, "hist": self.hist}
        self._last_sig: Optional[tuple] = None

    def reset(self) -> None:
        self.macd = MACD(
            fast_period=self.fast,
            slow_period=self.slow,
            signal_period=self.signal,
            china_mode=self.china_mode,
        )
        self.keys = []
        self.bucket_last_pos = []
        self.bucket_klines = []
        self.dif = []
        self.dea = []
        self.hist = []
        self._result = {"dif": self.dif, "dea": self.dea, "hist": self.hist}
        self._last_sig = None

    def update(self, klines: List[Kline]) -> Optional[dict]:
        if self.higher is None:
            return None
        n = len(klines)
        if n == 0:
            self.reset()
            return None
        if not self.keys:
            return self._rebuild(klines)
        if n < len(self.keys):
            return self._rebuild(klines)
        if not self._can_increment(klines, n):
            return self._rebuild(klines)

        old_n = len(self.keys)
        try:
            if n == old_n:
                self._update_existing_last(klines[-1])
            else:
                self._update_existing_last(klines[old_n - 1])
                for pos in range(old_n, n):
                    self._append_bar(klines[pos])
        except ValueError:
            return self._rebuild(klines)
        self._last_sig = self._signature(klines[-1], n)
        return self._maybe_result()

    def _can_increment(self, klines: List[Kline], n: int) -> bool:
        if self._last_sig is None:
            return False
        if n == len(self.keys):
            return True
        if len(self.keys) == 0:
            return False
        try:
            prev = klines[len(self.keys) - 1]
            return self._key_for(prev) == self.keys[-1]
        except Exception:
            return False

    def _signature(self, kline: Kline, n: int) -> tuple:
        return (
            n,
            getattr(kline, "date", None),
            float(getattr(kline, "c", 0.0) or 0.0),
        )

    def _key_for(self, kline: Kline) -> int:
        try:
            ts = int(kline.date.timestamp())
        except (ValueError, TypeError, AttributeError, OverflowError, OSError) as exc:
            raise ValueError("invalid kline date for higher MACD") from exc
        keys = _bucket_keys(np.array([ts], dtype=np.int64), self.higher, self.market)
        if keys is None or keys.size != 1:
            raise ValueError("unsupported higher MACD bucket")
        return int(keys[0])

    def _bucket_kline(self, bucket_idx: int, close: float) -> Kline:
        return Kline(
            index=bucket_idx,
            date=None,
            h=0.0,
            l=0.0,
            o=0.0,
            c=float(close),
            a=0.0,
        )

    def _rebuild(self, klines: List[Kline]) -> Optional[dict]:
        self.reset()
        if self.higher is None or not klines:
            return None
        try:
            t = np.array([k.date.timestamp() for k in klines], dtype=np.int64)
        except (ValueError, TypeError, AttributeError, OverflowError, OSError):
            self.reset()
            return None
        keys = _bucket_keys(t, self.higher, self.market)
        if keys is None:
            self.reset()
            return None
        self.keys = [int(k) for k in keys.tolist()]
        boundaries = np.concatenate(([True], keys[1:] != keys[:-1]))
        bucket_idx = np.cumsum(boundaries) - 1
        bucket_count = int(bucket_idx[-1]) + 1
        last_pos = np.zeros(bucket_count, dtype=np.int64)
        last_pos[bucket_idx] = np.arange(len(klines))
        self.bucket_last_pos = [int(x) for x in last_pos.tolist()]
        self.bucket_klines = [
            self._bucket_kline(i, float(klines[pos].c))
            for i, pos in enumerate(self.bucket_last_pos)
        ]
        self.macd.process_macd(self.bucket_klines)
        self._last_sig = self._signature(klines[-1], len(klines))
        if bucket_count <= self.slow + self.signal:
            return None
        self._rebuild_interp_all(len(klines))
        return self._result

    def _update_existing_last(self, kline: Kline) -> None:
        pos = len(self.keys) - 1
        key = self._key_for(kline)
        if key != self.keys[pos] or not self.bucket_last_pos:
            raise ValueError("higher MACD tail bucket changed")
        bucket_idx = len(self.bucket_last_pos) - 1
        if self.bucket_last_pos[bucket_idx] != pos:
            raise ValueError("higher MACD tail bucket mismatch")
        self.bucket_klines[bucket_idx].c = float(kline.c)
        self.macd.process_macd(self.bucket_klines)
        self._rewrite_interp_suffix()

    def _append_bar(self, kline: Kline) -> None:
        pos = len(self.keys)
        key = self._key_for(kline)
        self.keys.append(key)
        if not self.bucket_last_pos or key != self.keys[pos - 1]:
            bucket_idx = len(self.bucket_last_pos)
            self.bucket_last_pos.append(pos)
            self.bucket_klines.append(self._bucket_kline(bucket_idx, float(kline.c)))
        else:
            bucket_idx = len(self.bucket_last_pos) - 1
            self.bucket_last_pos[bucket_idx] = pos
            self.bucket_klines[bucket_idx].c = float(kline.c)
        self.macd.process_macd(self.bucket_klines)
        self._rewrite_interp_suffix()

    def _maybe_result(self) -> Optional[dict]:
        if len(self.bucket_last_pos) <= self.slow + self.signal:
            return None
        if len(self.dif) != len(self.keys):
            self._rebuild_interp_all(len(self.keys))
        return self._result

    def _rebuild_interp_all(self, n: int) -> None:
        anchor_x = np.asarray(self.bucket_last_pos, dtype=float)
        bars = np.arange(n, dtype=float)
        dif_b = np.asarray(self.macd.dif, dtype=float)
        dea_b = np.asarray(self.macd.dea, dtype=float)
        dif = np.interp(bars, anchor_x, dif_b)
        dea = np.interp(bars, anchor_x, dea_b)
        hist = dif - dea
        if self.china_mode:
            hist = hist * 2.0
        self.dif[:] = dif.tolist()
        self.dea[:] = dea.tolist()
        self.hist[:] = hist.tolist()

    def _rewrite_interp_suffix(self) -> None:
        if len(self.bucket_last_pos) <= self.slow + self.signal:
            return
        n = len(self.keys)
        if len(self.dif) == 0 or len(self.dif) > n:
            self._rebuild_interp_all(n)
            return
        if len(self.dif) < n:
            missing = n - len(self.dif)
            last_dif = self.dif[-1]
            last_dea = self.dea[-1]
            last_hist = self.hist[-1]
            self.dif.extend([last_dif] * missing)
            self.dea.extend([last_dea] * missing)
            self.hist.extend([last_hist] * missing)
        cur_pos = self.bucket_last_pos[-1]
        cur_dif = float(self.macd.dif[-1])
        cur_dea = float(self.macd.dea[-1])
        if len(self.bucket_last_pos) == 1:
            start = 0
            dif_vals = np.full(cur_pos + 1, cur_dif, dtype=float)
            dea_vals = np.full(cur_pos + 1, cur_dea, dtype=float)
        else:
            prev_pos = self.bucket_last_pos[-2]
            start = prev_pos + 1
            x = np.array([prev_pos, cur_pos], dtype=float)
            bars = np.arange(start, cur_pos + 1, dtype=float)
            dif_vals = np.interp(
                bars,
                x,
                np.array([float(self.macd.dif[-2]), cur_dif], dtype=float),
            )
            dea_vals = np.interp(
                bars,
                x,
                np.array([float(self.macd.dea[-2]), cur_dea], dtype=float),
            )
        hist_vals = dif_vals - dea_vals
        if self.china_mode:
            hist_vals = hist_vals * 2.0
        end = cur_pos + 1
        self.dif[start:end] = dif_vals.tolist()
        self.dea[start:end] = dea_vals.tolist()
        self.hist[start:end] = hist_vals.tolist()
