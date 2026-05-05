"""图表计算服务（service）。

Tier 4 P3 重构：从 blueprints/tv.py 抽出 ``compute_and_cache_chart_data`` 主路径，
让 symbols.py 不再 import tv.py 任何符号；tv.py 真正回归"路由层"。

包含：
- ``_SafeLockRegistry``: weakref + 引用计数的 per-key 锁注册表
- ``chart_calc_locks``: chart_data_cache 的 per-key 计算锁
- ``HIGHER_MACD_RATIO`` / ``MARKET_30M_TO_D_RATIO`` / ``MARKET_D_TO_W_RATIO``: 跨周期 MACD 倍率
- ``_shape_time`` / ``_merge_shape_lists`` / ``_merge_chart_data``: chart 数据合并的纯函数
- ``compute_and_cache_chart_data``: cache miss 后的完整计算路径

不包含（留在 tv.py 中）:
- ``prewarm_common_intervals``: OLD prewarm 路径，与 tv_history 流深度耦合，未迁
- ``_history_req_locks``: 仅 tv_history 内部使用的节流锁（实例化 _SafeLockRegistry）
"""
import datetime
import threading
import weakref
from threading import RLock

import numpy as np
import pytz
import talib

from chanlun.cl_utils import (
    cl_data_to_tv_chart,
    kcharts_frequency_h_l_map,
    web_batch_get_cl_datas,
)
from chanlun.base import Market
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil

from .chart_cache import (
    _build_cache_key,
    _get_chart_cache_entry,
    _is_negatively_cached,
    _mark_chart_cache_validated,
    _mark_negative_cache,
    _set_chart_cache_entry,
    cache_lock,
)

# ---------------- 跨周期 MACD 倍率 ----------------

# key = 当前K线频率, value = 倍率（高级别周期包含多少根当前周期K线）
HIGHER_MACD_RATIO = {
    "1m": 5,     # 1分钟 → 5分钟 MACD，参数乘以5
    "5m": 6,     # 5分钟 → 30分钟 MACD，参数乘以6
}

# 30m → 日线的倍率因市场交易时长不同
MARKET_30M_TO_D_RATIO = {
    "a": 8,              # A股 4小时交易 = 8个30分钟
    "hk": 8,             # 港股
    "us": 13,            # 美股 6.5小时 = 13个30分钟
    "futures": 8,        # 国内期货
    "ny_futures": 13,    # 纽约期货
    "currency": 48,      # 数字货币 24小时 = 48个30分钟
    "currency_spot": 48,
    "fx": 48,            # 外汇
}

# 日线 → 周线的倍率
MARKET_D_TO_W_RATIO = {
    "a": 5,              # 5个交易日
    "hk": 5,
    "us": 5,
    "futures": 5,
    "ny_futures": 5,
    "currency": 7,       # 数字货币 7天
    "currency_spot": 7,
    "fx": 5,             # 外汇通常 5天
}


# ---------------- per-key 锁注册表 ----------------

class _SafeLockRegistry:
    """线程安全的 per-key 锁注册表，使用 weakref + 引用计数避免锁正确性问题。

    原 _LimitedLockDict 的问题：
    - 用 dict 的插入顺序做 FIFO 淘汰（不是 LRU），热点 key 会被错误淘汰；
    - 更严重的：被淘汰的 RLock 如果还被另一个线程持有，新请求会拿到一把全新的锁，
      导致同一 key 上"两把锁、各串行各的"，破坏 per-key 串行化语义。

    本实现改为：
    - 锁不主动淘汰，借助 WeakValueDictionary 让无引用的锁自然被 GC；
    - 调用方使用 with registry.get(key) as lock: 模式，进入 with 时锁的强引用挂在
      调用栈上，不会被 GC，确保两个并发请求拿到同一把锁。
    """

    def __init__(self):
        self._locks = weakref.WeakValueDictionary()
        self._registry_lock = threading.Lock()

    def get(self, key: str) -> RLock:
        """返回 key 对应的 RLock。调用方应立即用 with 包住，确保引用持续。"""
        with self._registry_lock:
            lock = self._locks.get(key)
            if lock is None:
                lock = RLock()
                self._locks[key] = lock
            return lock

    def __contains__(self, key):
        return key in self._locks

    def __len__(self):
        return len(self._locks)


# chart_data_cache 的 per-key 计算锁（同一标的同周期最多 1 个线程在算）
chart_calc_locks = _SafeLockRegistry()


# ---------------- chart data 合并（纯函数）----------------

def _shape_time(shape):
    if not isinstance(shape, dict):
        return None
    points = shape.get("points")
    if isinstance(points, list) and len(points) > 0:
        last_point = points[-1]
        if isinstance(last_point, dict):
            return last_point.get("time")
    if isinstance(points, dict):
        return points.get("time")
    return None


def _merge_shape_lists(existing_shapes, new_shapes):
    if not existing_shapes:
        return list(new_shapes or [])
    if not new_shapes:
        return list(existing_shapes or [])

    new_times = [t for t in (_shape_time(shape) for shape in new_shapes) if t is not None]
    if len(new_times) == 0:
        return list(existing_shapes or [])

    min_time = min(new_times)
    max_time = max(new_times)
    merged = []
    for shape in existing_shapes:
        shape_time = _shape_time(shape)
        if shape_time is None or shape_time < min_time or shape_time > max_time:
            merged.append(shape)
    merged.extend(new_shapes)
    return sorted(merged, key=lambda shape: (_shape_time(shape) or 0))


def _merge_chart_data(existing_data: dict, new_data: dict):
    if not existing_data:
        return new_data
    if not new_data:
        return existing_data

    merged = dict(existing_data)
    merged.update(new_data)

    existing_times = existing_data.get("t", [])
    new_times = new_data.get("t", [])
    all_times = sorted(set(existing_times) | set(new_times))
    merged["t"] = all_times

    aligned_keys = [
        "c", "o", "h", "l", "v",
        "macd_dif", "macd_dea", "macd_hist", "macd_area",
        "higher_macd_dif", "higher_macd_dea", "higher_macd_hist",
    ]
    for key in aligned_keys:
        existing_values = existing_data.get(key, [])
        new_values = new_data.get(key, [])
        if not existing_values and not new_values:
            merged[key] = []
            continue
        merged_values = {}
        for idx, bar_time in enumerate(existing_times):
            if idx < len(existing_values):
                merged_values[bar_time] = existing_values[idx]
        for idx, bar_time in enumerate(new_times):
            if idx < len(new_values):
                val = new_values[idx]
                # 仅当新值有效时才覆盖，避免 None 覆盖已有的有效值
                if val is not None:
                    merged_values[bar_time] = val
                elif bar_time not in merged_values:
                    merged_values[bar_time] = val
        merged[key] = [merged_values.get(bar_time) for bar_time in all_times]

    for key in ["fxs", "bis", "xds", "zsds", "bi_zss", "xd_zss", "zsd_zss", "bcs", "mmds"]:
        merged[key] = _merge_shape_lists(existing_data.get(key, []), new_data.get(key, []))

    return merged


# ---------------- 主计算路径 ----------------

def compute_and_cache_chart_data(market: str, code: str, frequency: str, cl_config: dict) -> bool:
    """完整复刻 ``tv_history`` 中 cache miss 后的计算路径，把结果写入 ``chart_data_cache``。

    返回 True 表示成功写入缓存（数据非空），False 表示中途无数据
    （已直接 _mark_chart_cache_validated）。

    设计目的：让 ``symbols.py`` 的批量预热与用户实际打开图表时走完全相同的计算逻辑，
    避免预热结果"少算"了 higher_macd 等指标，导致用户切换时仍然 cache miss。

    关键步骤（与 ``tv_history`` 一致）：
    1. ``ex.klines`` 拉数据（支持 enable_kchart_low_to_high 的低周期合成高周期）
    2. ``web_batch_get_cl_datas`` 计算缠论
    3. ``cl_data_to_tv_chart`` 转 TV 图表格式
    4. 跨周期 MACD（higher_macd_dif/dea/hist）按市场倍率放大后用 talib.MACD 计算
    5. ``_merge_chart_data`` 与既有缓存合并（如有）
    6. ``_set_chart_cache_entry`` 写入，``is_full_snapshot=True``
    """
    tz_sh = pytz.timezone("Asia/Shanghai")
    cache_key = _build_cache_key(market, code, frequency, cl_config)

    # 2026-04 修复：负缓存。最近 5 分钟内已经确认无数据的 cache_key 直接返回，
    # 不再调 ex.klines() 浪费 HTTP 配额。
    if _is_negatively_cached(cache_key):
        return False

    ex = get_exchange(Market(market))
    frequency_low, kchart_to_frequency = kcharts_frequency_h_l_map(market, frequency)

    kline_args = {
        "end_date": datetime.datetime.now(tz_sh).strftime("%Y-%m-%d %H:%M:%S")
    }

    if (
        cl_config.get("enable_kchart_low_to_high") == "1"
        and kchart_to_frequency is not None
        and frequency_low is not None
    ):
        klines = ex.klines(code, frequency_low, **kline_args)
        if klines is None or len(klines) == 0:
            _mark_negative_cache(cache_key)
            with cache_lock:
                _mark_chart_cache_validated(cache_key)
            return False
        cd = web_batch_get_cl_datas(market, code, {frequency_low: klines}, cl_config)[0]
    else:
        kchart_to_frequency = None
        klines = ex.klines(code, frequency, **kline_args)
        if klines is None or len(klines) == 0:
            _mark_negative_cache(cache_key)
            with cache_lock:
                _mark_chart_cache_validated(cache_key)
            return False
        cd = web_batch_get_cl_datas(market, code, {frequency: klines}, cl_config)[0]

    cl_chart_data = cl_data_to_tv_chart(cd, cl_config, to_frequency=kchart_to_frequency)
    if cl_chart_data is None:
        _mark_negative_cache(cache_key)
        with cache_lock:
            _mark_chart_cache_validated(cache_key)
        return False

    # 跨周期 MACD：与 tv_history 完全一致的倍率计算
    ratio = HIGHER_MACD_RATIO.get(frequency)
    if ratio is None and frequency == "30m":
        ratio = MARKET_30M_TO_D_RATIO.get(market, 8)
    elif ratio is None and frequency == "d":
        ratio = MARKET_D_TO_W_RATIO.get(market, 5)
    elif ratio is None and frequency == "w":
        ratio = 4
    elif ratio is None and frequency == "m":
        ratio = 12

    if ratio is not None:
        try:
            closes = np.array(cl_chart_data.get("c", []), dtype=float)
            fast = int(cl_config.get("idx_macd_fast", 12)) * ratio
            slow = int(cl_config.get("idx_macd_slow", 26)) * ratio
            signal = int(cl_config.get("idx_macd_signal", 9)) * ratio
            min_bars = slow + signal
            if len(closes) > min_bars:
                h_dif, h_dea, h_hist = talib.MACD(
                    closes,
                    fastperiod=fast,
                    slowperiod=slow,
                    signalperiod=signal,
                )
                h_dif_rounded = np.round(h_dif, 6)
                h_dea_rounded = np.round(h_dea, 6)
                h_hist_rounded = np.round(h_hist, 6)
                cl_chart_data["higher_macd_dif"] = np.where(
                    np.isnan(h_dif_rounded), None, h_dif_rounded
                ).tolist()
                cl_chart_data["higher_macd_dea"] = np.where(
                    np.isnan(h_dea_rounded), None, h_dea_rounded
                ).tolist()
                cl_chart_data["higher_macd_hist"] = np.where(
                    np.isnan(h_hist_rounded), None, h_hist_rounded
                ).tolist()
        except Exception as e:
            LogUtil.error(f"[compute_and_cache_chart_data] Scaled MACD calc failed: {e}")

    with cache_lock:
        existing_entry = _get_chart_cache_entry(cache_key)
        if existing_entry is not None:
            cl_chart_data = _merge_chart_data(
                existing_entry.get("data", {}), cl_chart_data
            )
        _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)
    return True
