# -*- coding: utf-8 -*-
"""高周期 MACD —— 背驰力度按原文应在「高一级别」上度量。

本项目以**线段**为最低级别走势类型。线段跨多根 K 线、本质是高一层结构，
度量其背驰力度时若用原生 K 线 MACD 过于细碎，导致 1/2 类买卖点与背驰难
以稳定识别。本模块按 K 线时间戳把当前周期重采样到高一周期
（``1m→5m, 5m→30m, 30m→d, d→w, w→m, m→y``），在高周期 close 上算 MACD，
再线性插值回每根 K 线，供 ``query_macd_ld`` 取用。

口径与 web 端 ``apply_higher_macd_to_chart_data`` 一致：真实重采样 + 桶末
锚点线性插值。区别仅在高周期 MACD 用本仓自带的 ``core.macd.MACD``
（与引擎其余处一致；早期不足 ``slow+signal`` 根处填 0 而非 NaN，故插值
结果天然无 NaN）。
"""
from __future__ import annotations

from typing import List, Optional

import numpy as np

from chanlun.core.cl_interface import Kline
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
