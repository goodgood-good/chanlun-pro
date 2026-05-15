"""图表计算服务（service）。

Tier 4 P3 重构：从 blueprints/tv.py 抽出 ``compute_and_cache_chart_data`` 主路径，
让 symbols.py 不再 import tv.py 任何符号；tv.py 真正回归"路由层"。

包含：
- ``_SafeLockRegistry``: weakref + 引用计数的 per-key 锁注册表
- ``chart_calc_locks``: chart_data_cache 的 per-key 计算锁
- ``HIGHER_FREQ_MAP`` / ``MARKET_TZ``: HTF MACD 频率映射与市场时区
- ``_bin_keys_for_higher`` / ``_resample_closes_to_higher``: HTF MACD 合成核心
- ``_shape_time`` / ``_merge_shape_lists`` / ``_merge_chart_data``: chart 数据合并的纯函数
- ``compute_and_cache_chart_data``: cache miss 后的完整计算路径

不包含（留在 tv.py 中）:
- ``prewarm_common_intervals``: OLD prewarm 路径，与 tv_history 流深度耦合，未迁
- ``_history_req_locks``: 仅 tv_history 内部使用的节流锁（实例化 _SafeLockRegistry）
"""
import bisect
import datetime
import threading
import weakref
from threading import RLock

import numpy as np
import pytz
import talib

from chanlun import fun
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

# ---------------- HTF MACD: 频率映射与市场时区 ----------------

# 当前周期 -> 目标高周期(去除"放大倍率"概念,改用"目标周期标识符")
HIGHER_FREQ_MAP = {
    "1m": "5m",
    "5m": "30m",
    "30m": "d",
    "d": "w",
    "w": "M",
}

# 市场时区,决定 d/w/M bin 切割时的"自然日界"
MARKET_TZ = {
    "a": "Asia/Shanghai",
    "hk": "Asia/Hong_Kong",
    "us": "America/New_York",
    "ny_futures": "America/New_York",
    "futures": "Asia/Shanghai",
    "currency": "UTC",
    "currency_spot": "UTC",
    "fx": "UTC",
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
    """末点 time（仅排序用）。"""
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


def _shape_id(shape):
    """身份键：起点 (time, price)。
    一根线段/中枢从某个起点出发任意时刻只该有一个版本——
    末点会随 K 线包含合并漂移、linestyle 会从 1 翻成 0，
    用末点做去重会让"同一身份的新旧两版"同时保留 → 视觉上线段重叠/断裂。
    单点形态（fxs/bcs/mmds）的 (time, price) 也唯一。
    """
    if not isinstance(shape, dict):
        return None
    points = shape.get("points")
    if isinstance(points, list) and len(points) > 0:
        first = points[0]
        if isinstance(first, dict):
            return (first.get("time"), first.get("price"))
    if isinstance(points, dict):
        return (points.get("time"), points.get("price"))
    return None


def _merge_shape_lists(existing_shapes, new_shapes):
    """按起点身份合并去重，新覆盖旧。
    旧实现按 new 数据的 end_time 区间切割旧数据：
    增量更新时新数据可能没覆盖到中间某段而中间段 end_time 却落在区间内
    → 中间段被永久删除 → 线段不连续。
    未完成段／中枢则起点稳定末点漂移，每次累积一份"双胞胎"。
    起点身份键能同时解决两类问题。
    """
    if not existing_shapes and not new_shapes:
        return []
    merged = {}
    for shape in (existing_shapes or []):
        sid = _shape_id(shape)
        if sid is not None:
            merged[sid] = shape
    for shape in (new_shapes or []):
        sid = _shape_id(shape)
        if sid is not None:
            merged[sid] = shape  # 新版本覆盖旧
    return sorted(merged.values(), key=lambda s: (_shape_time(s) or 0))


def _merge_chart_data(existing_data: dict, new_data: dict):
    # ★ 历史背景(2026-05):本函数把"两份独立计算的 chart_data"按 shape 起点合并,
    # 在向左滚动场景会让 XD 出现"局部计算 → 起点替换 → 视觉跳变"。
    # web/tv 范围请求路径已迁移到 kline_recompute.prepend_klines_and_replace_cache,
    # 该入口直接基于"完整 K 线集"全量重算,整体替换 chart_data_cache。
    # 这里仅保留以兼容首屏 cache_tail_gap / firstDataRequest 路径——它们的合并语义
    # 和"只补未来 K 线 + shape"的契约一致,不会触发 XD 起点跳变问题。
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

    for key in ["fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds"]:
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

    # P5 third step: 跨周期 MACD 抽到 apply_higher_macd_to_chart_data 共享 helper
    apply_higher_macd_to_chart_data(cl_chart_data, frequency, market, cl_config)

    with cache_lock:
        existing_entry = _get_chart_cache_entry(cache_key)
        if existing_entry is not None:
            cl_chart_data = _merge_chart_data(
                existing_entry.get("data", {}), cl_chart_data
            )
        _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)
    return True


# ===========================================================================
# P5 second step: chart_data 切片 / 裁未来 bar 抽到 module-level
# ===========================================================================

# 所有可能是"按 t 长度对齐的数组字段"列表 (用于 _slice_window / _trim_future_bars)。
_CHART_ARRAY_FIELDS = (
    "t", "o", "h", "l", "c", "v",
    "macd_dif", "macd_dea", "macd_hist", "macd_area",
    "higher_macd_dif", "higher_macd_dea", "higher_macd_hist",
)

# 形态字段 (笔/段/中枢/分型/背驰/买卖点) - 不是按 t 长度对齐, 走 filter_shapes_in_window
_CHART_SHAPE_FIELDS = ("fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds")


def filter_shapes_in_window(shapes, from_ts: int, to_ts: int) -> list:
    """按 [from_ts, to_ts) 窗口过滤形态 (笔/段/中枢/分型/背驰/买卖点)。

    多点形态 (笔/段/中枢): 与 [from_ts, to_ts) 有重叠即保留 (起点早于 to_ts 且
    终点晚于 from_ts), 避免"跨可视边界"形态丢失。
    单点形态 (分型/背驰/买卖点): 点位需落在窗口内。

    Args:
        shapes: list of shape dict (每个含 "points": list[dict] 或 dict)
        from_ts: 窗口起点 (unix 秒)
        to_ts: 窗口终点 (unix 秒, 0 表示无上界)

    Returns:
        过滤后的 list (不修改原输入)。
    """
    res = []
    for shape in shapes:
        if not (isinstance(shape, dict) and "points" in shape):
            continue
        pts = shape["points"]
        if isinstance(pts, list) and len(pts) > 0:
            t_start = pts[0].get("time", 0)
            t_end = pts[-1].get("time", 0)
            if t_end >= from_ts and (to_ts == 0 or t_start < to_ts):
                res.append(shape)
        elif isinstance(pts, dict):
            t = pts.get("time", 0)
            if t >= from_ts and (to_ts == 0 or t < to_ts):
                res.append(shape)
    return res


def slice_chart_data_to_window(chart_data: dict, from_ts: int, to_ts: int) -> dict:
    """把 chart_data 按 [from_ts, to_ts) 切窗口 (P5 second step, 抽自 tv_history)。

    bar_times 用 ``bisect_left`` 找 start_idx/end_idx (左闭右开, 与 TV UDF 一致);
    所有 t 长度对齐字段按 [start_idx:end_idx] 切片; 形态字段走 filter_shapes_in_window。

    Args:
        chart_data: 完整 chart_data dict (含 t/o/h/l/c/v/macd_*/fxs/bis/...)
        from_ts: 窗口起点
        to_ts: 窗口终点 (0 表示无上界, 取 bar_times 全长)

    Returns:
        切片后的新 chart_data dict (浅拷贝, 不改原 dict)。
    """
    bar_times = chart_data.get("t", []) or []
    if not bar_times:
        return dict(chart_data)

    start_idx = bisect.bisect_left(bar_times, from_ts)
    # bisect_left 让 to_ts 排他上界, 避免向左滚动返回相同 K 线
    end_idx = bisect.bisect_left(bar_times, to_ts) if to_ts > 0 else len(bar_times)

    sliced: dict = {}
    for field in _CHART_ARRAY_FIELDS:
        arr = chart_data.get(field) or []
        sliced[field] = arr[start_idx:end_idx] if arr else []
    for field in _CHART_SHAPE_FIELDS:
        sliced[field] = filter_shapes_in_window(
            chart_data.get(field, []) or [], from_ts, to_ts
        )
    return sliced


def trim_future_bars(chart_data: dict, to_ts: int) -> dict:
    """裁剪 chart_data 中时间戳 > to_ts 的"未来" bar (P5 second step)。

    交易所偶尔返回尚未完成的下一根 K 线 (timestamp > 当前时间), TradingView
    缓存后会在 DataPulse 更新时撞 time order violation。本函数对所有 t 长度
    对齐字段做 [:_resp_end] 切片, ``_resp_end`` = ``bisect_right(t, to_ts)``。

    Args:
        chart_data: 已切到可视窗口的 chart_data
        to_ts: 请求 to 时间戳 (0/负数 → 不裁)

    Returns:
        裁剪后的新 dict (浅拷贝)。形态字段不裁 (笔/段/中枢的 end.time 可能 > to_ts
        但起点在窗口内, 仍要展示, 由 ``filter_shapes_in_window`` 保留)。
    """
    if to_ts <= 0:
        return dict(chart_data)
    times = chart_data.get("t") or []
    if not times or times[-1] <= to_ts:
        return dict(chart_data)
    resp_end = bisect.bisect_right(times, to_ts)
    if resp_end >= len(times):
        return dict(chart_data)

    trimmed: dict = {}
    for field in _CHART_ARRAY_FIELDS:
        arr = chart_data.get(field) or []
        trimmed[field] = arr[:resp_end] if arr else []
    # 形态字段保持原样
    for field in _CHART_SHAPE_FIELDS:
        trimmed[field] = chart_data.get(field, []) or []
    return trimmed


# ===========================================================================
# P5 third step: 跨周期 MACD 计算抽到 module-level (消除 tv.py + chart_compute.py
# 的重复实现, 改动一处即可)
# ===========================================================================

def apply_higher_macd_to_chart_data(
    chart_data: dict,
    frequency: str,
    market: str,
    cl_config: dict,
) -> None:
    """计算并 in-place 写入 chart_data 的 higher_macd_dif/dea/hist 字段。

    实现:把低周期 closes 按市场时区合成目标高周期 closes(演化模式),
    在 higher_closes 上跑标准 talib.MACD(12,26,9),再把结果按低-高 idx
    映射投影回低周期长度。

    与旧"参数 × ratio"近似法相比:
    1. 数学上等价"用真实高周期 K 线跑标准 MACD"
    2. 跨夜断层、半日休市、午休由 bin 分组自然处理,不再污染 EMA

    Args:
        chart_data: 已含 "t" + "c" 字段的 chart_data dict, in-place 写入
                   higher_macd_*; target_freq is None 或数据不足时不修改。
        frequency: 当前 K 线周期 (1m / 5m / 30m / d / w / M)
        market: 市场标识 (用于 d/w/M 的市场时区)
        cl_config: 缠论配置 (取 idx_macd_fast/slow/signal, 默认 12/26/9)
    """
    target_freq = _resolve_higher_target_freq(frequency, market)
    if target_freq is None:
        return  # 月线或未知 freq, 无高周期对照

    times_list = chart_data.get("t", [])
    closes_list = chart_data.get("c", [])
    if not times_list or not closes_list:
        return
    if len(times_list) != len(closes_list):
        LogUtil.error(
            f"[apply_higher_macd] t/c length mismatch: "
            f"{len(times_list)} vs {len(closes_list)}"
        )
        return

    try:
        times = np.array(times_list, dtype=np.int64)
        closes = np.array(closes_list, dtype=float)
        bin_keys = _bin_keys_for_higher(times, target_freq, market)
        higher_closes, low2high = _resample_closes_to_higher(closes, bin_keys)

        fast = int(cl_config.get("idx_macd_fast", 12))
        slow = int(cl_config.get("idx_macd_slow", 26))
        signal = int(cl_config.get("idx_macd_signal", 9))
        if len(higher_closes) <= slow + signal:
            return

        h_dif, h_dea, h_hist = talib.MACD(
            higher_closes, fastperiod=fast, slowperiod=slow, signalperiod=signal,
        )
        low_dif = np.round(h_dif[low2high], 6)
        low_dea = np.round(h_dea[low2high], 6)
        low_hist = np.round(h_hist[low2high], 6)
        chart_data["higher_macd_dif"] = np.where(
            np.isnan(low_dif), None, low_dif
        ).tolist()
        chart_data["higher_macd_dea"] = np.where(
            np.isnan(low_dea), None, low_dea
        ).tolist()
        chart_data["higher_macd_hist"] = np.where(
            np.isnan(low_hist), None, low_hist
        ).tolist()
    except Exception as e:
        LogUtil.error(f"[apply_higher_macd] resample MACD calc failed: {e}")


# ===========================================================================
# P5 fourth step: cache miss 路径的 "拉 K 线 + 算 cl 数据" 抽到 helper
# ===========================================================================
# 这是 P5 中最复杂的一步, 涉及:
#   - ex.klines() IO 调用
#   - enable_kchart_low_to_high 分支
#   - prepend_klines_and_replace_cache 决策 (head_gap/partial_snapshot/tail_gap)
#   - cl_data_to_tv_chart 转换
#   - 多个 early return 路径 (no_data)
# 通过返回 Optional[dict] 让调用方处理 no_data, 副作用 _mark_chart_cache_validated
# 已在 helper 内部完成 (与原 tv_history 行为一致)。


def fetch_klines_and_compute_cl_data(
    market: str,
    code: str,
    frequency: str,
    cl_config: dict,
    kline_args: dict,
    is_range_request: bool,
    cache_miss_reason: str,
    cache_key: str,
    to_ts: int,
):
    """cache miss 路径: 拉 K 线 + 算 cl 数据。

    P5 fourth step (2026-05-15): 抽自 tv.py::tv_history 中 ~50 行 cache miss
    主路径, 含 enable_kchart_low_to_high / prepend / cl_data_to_tv_chart 三个
    decision point。

    Args:
        market / code / frequency / cl_config: 标的参数
        kline_args: 拉 K 线的参数 (start_date / end_date)
        is_range_request: 是否窄范围请求 (firstDataRequest=false + 有 from/to)
        cache_miss_reason: 来自 evaluate_cache_for_tv_history 的 miss 原因
                          ("cache_empty" / "cache_head_gap" / 等), 决定是否走
                          prepend_klines_and_replace_cache
        cache_key: chart_data_cache 的 key
        to_ts: 请求 to 时间戳 (用于"_to < first_kline_date 早返"判定)

    Returns:
        ``dict`` 含 ``cl_chart_data``/``cd``/``kchart_to_frequency`` 三键:
            - cl_chart_data: 计算结果
            - cd: 普通路径的 CL 对象 (供调用方后续写回 cache); prepend 路径下为 None
            - kchart_to_frequency: 高/低周期映射的目标 frequency (供 cl_data_to_tv_chart
              使用); 普通路径下为 None
        ``None`` 表示无数据 / prepend 失败 / cl 转换失败, 调用方应 return {"s": "no_data"}。
        helper 内部已 _mark_chart_cache_validated 标记 cache 时效, 调用方不需再做。
    """
    # 注意: 这里要懒 import 避免 chart_compute → kline_recompute → chart_cache
    # 循环依赖 (kline_recompute 在自己模块里 import chart_cache)。
    from .kline_recompute import prepend_klines_and_replace_cache

    ex = get_exchange(Market(market))
    frequency_low, kchart_to_frequency = kcharts_frequency_h_l_map(market, frequency)

    if (
        cl_config.get("enable_kchart_low_to_high") == "1"
        and kchart_to_frequency is not None
        and frequency_low is not None
    ):
        klines = ex.klines(code, frequency_low, **kline_args)
        if klines is None or len(klines) == 0:
            with cache_lock:
                _mark_chart_cache_validated(cache_key)
            return None
        cd = web_batch_get_cl_datas(market, code, {frequency_low: klines}, cl_config)[0]
    else:
        kchart_to_frequency = None
        klines = ex.klines(code, frequency, **kline_args)
        if klines is None or len(klines) == 0:
            with cache_lock:
                _mark_chart_cache_validated(cache_key)
            return None

        # 方案 A: 范围请求走分层缓存 — 把新 K 线合并进 L1, 基于完整 K 线集
        # 全量重算, 整体替换 L2。不再走 web_batch_get_cl_datas + _merge_chart_data,
        # 那条老路径会用窄范围 K 线独立计算 XD 再"按起点合并",
        # 导致用户向左滚动时看到 XD 跳变。
        # 2026-05 扩大覆盖: cache_tail_gap (polling 拉新末段 K 线) 也必须走 prepend
        # (整体替换), 否则 polling 末段触发 XD 重新划分时, 新旧 XD 起点身份不一致,
        # _merge_shape_lists 按起点合并 → 不去重 → "XD 双胞胎"。
        if (
            is_range_request
            and cache_miss_reason in (
                "cache_head_gap",
                "cache_partial_snapshot",
                "cache_tail_gap",
            )
        ):
            cl_chart_data = prepend_klines_and_replace_cache(
                market, code, frequency, cl_config,
                new_klines=klines, cache_key=cache_key,
                to_frequency=None,
            )
            if cl_chart_data is None:
                with cache_lock:
                    _mark_chart_cache_validated(cache_key)
                return None
            # prepend 内部已经写回 cache, 跳过下面的 cl_data_to_tv_chart 路径
            cd = None
        else:
            cd = web_batch_get_cl_datas(market, code, {frequency: klines}, cl_config)[0]

    # _to < first_kline_date: 用户请求的窗口完全在数据起点之前 → no_data
    if to_ts > 0 and len(klines) > 0 and to_ts < fun.datetime_to_int(klines.iloc[0]["date"]):
        with cache_lock:
            _mark_chart_cache_validated(cache_key)
        return None

    # 普通路径: cd 非空 → 跑 cl_data_to_tv_chart 转 chart_data
    cl_chart_data_local = None
    if cd is not None:
        cl_chart_data_local = cl_data_to_tv_chart(
            cd, cl_config, to_frequency=kchart_to_frequency
        )
        if cl_chart_data_local is None:
            with cache_lock:
                _mark_chart_cache_validated(cache_key)
            return None
    else:
        # prepend 路径: cl_chart_data 已由 prepend_klines_and_replace_cache 计算
        cl_chart_data_local = cl_chart_data  # noqa: F821 (上面 if 块定义)

    return {
        "cl_chart_data": cl_chart_data_local,
        "cd": cd,
        "kchart_to_frequency": kchart_to_frequency,
    }


# ===========================================================================
# HTF MACD: 真合成算法核心 (替代旧"参数 × ratio"近似法)
# ===========================================================================

def _resolve_higher_target_freq(frequency: str, market: str) -> "str | None":
    """frequency -> 目标高周期标识符;无对照(月线 M 或未知 freq)返回 None。

    后续由 _bin_keys_for_higher 决定具体怎么合成。market 参数当前未使用,
    保留以便未来按市场差异化映射(例如某些市场无 30m→d 对照时)。
    """
    return HIGHER_FREQ_MAP.get(frequency)


def _bin_keys_for_higher(
    times: "np.ndarray",
    target_freq: str,
    market: str,
) -> "np.ndarray":
    """计算每根低周期 K 线归属哪个高周期 bin。

    返回 int64 numpy 数组,长度 == len(times)。

    bin id 仅用作"相邻同 bin 分组键",不要求全局唯一/单调。但对每个
    target_freq,保证:bar_a 与 bar_b 同 bin 当且仅当二者应被合成进同一
    根高周期 K 线。

    target_freq:
      "5m":  epoch // 300
      "30m": epoch // 1800
      "d":   market 时区下 date.toordinal()
      "w":   market 时区下 ISO (year, week) 打包成 year*100 + week
      "M":   market 时区下 year * 100 + month
    """
    if target_freq == "5m":
        return (times // 300).astype(np.int64)
    if target_freq == "30m":
        return (times // 1800).astype(np.int64)
    # d / w / M 需要时区
    tz_name = MARKET_TZ.get(market, "UTC")
    tz = pytz.timezone(tz_name)
    out = np.empty(len(times), dtype=np.int64)
    for i, t in enumerate(times):
        dt = datetime.datetime.fromtimestamp(int(t), tz=tz)
        if target_freq == "d":
            out[i] = dt.date().toordinal()
        elif target_freq == "w":
            iso = dt.isocalendar()
            # isocalendar() 返回 IsoCalendarDate(year, week, weekday)
            iso_year, iso_week = iso[0], iso[1]
            out[i] = iso_year * 100 + iso_week
        elif target_freq == "M":
            out[i] = dt.year * 100 + dt.month
        else:
            raise ValueError(f"Unsupported target_freq: {target_freq}")
    return out


def _resample_closes_to_higher(
    closes: "np.ndarray",
    bin_keys: "np.ndarray",
) -> "tuple[np.ndarray, np.ndarray]":
    """按 bin_keys 把 closes 合成到高周期 closes (演化模式)。

    返回:
      higher_closes: 每个唯一 bin 的 close (bin 内最后一根低周期 close)
      low_to_higher_idx: 长度 == len(closes), 每个值是该低周期 K 线对应的
                        higher_closes 索引。

    "演化模式": 同 bin 内的多根低周期 K 线, higher_closes 用最新一根 close
    覆盖。每次重算时 higher_closes[-1] 反映当前 bin 内最新 close, 等价于
    "未收盘高周期 bar 实时演化"。

    假设 bin_keys 是按低周期 K 线时间顺序排列的(同一 bin 在数组里相邻),
    这由调用方保证 (chart_data["t"] 本身按时间升序)。
    """
    n = len(closes)
    if n == 0:
        return (
            np.empty(0, dtype=float),
            np.empty(0, dtype=np.int64),
        )
    higher_closes: "list[float]" = []
    low_to_higher_idx = np.empty(n, dtype=np.int64)
    prev_bin = None
    cur_higher_idx = -1
    for i in range(n):
        bk = bin_keys[i]
        if bk != prev_bin:
            cur_higher_idx += 1
            higher_closes.append(float(closes[i]))
            prev_bin = bk
        else:
            higher_closes[cur_higher_idx] = float(closes[i])
        low_to_higher_idx[i] = cur_higher_idx
    return np.array(higher_closes, dtype=float), low_to_higher_idx
