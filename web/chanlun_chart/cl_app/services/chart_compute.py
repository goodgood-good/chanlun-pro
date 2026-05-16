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
    # 范围请求（向左滚动）已迁移到 kline_recompute.prepend_klines_and_replace_cache
    # 做全量重算整体替换。此函数仅用于首屏 cache_tail_gap / firstDataRequest 路径——
    # 这些路径只补未来 K 线 + shape，合并语义安全，不会触发 XD 起点跳变。
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
    # time->index 映射只建一次跨 12 个字段复用；重复时间戳 dict comprehension 后者覆盖前者。
    existing_idx = {bar_time: i for i, bar_time in enumerate(existing_times)}
    new_idx = {bar_time: i for i, bar_time in enumerate(new_times)}
    for key in aligned_keys:
        existing_values = existing_data.get(key, [])
        new_values = new_data.get(key, [])
        if not existing_values and not new_values:
            merged[key] = []
            continue
        merged_col = []
        for bar_time in all_times:
            val = None
            ei = existing_idx.get(bar_time)
            if ei is not None and ei < len(existing_values):
                val = existing_values[ei]
            ni = new_idx.get(bar_time)
            if ni is not None and ni < len(new_values):
                new_val = new_values[ni]
                # 仅当新值有效时覆盖；None 不覆盖已有有效值
                if new_val is not None or val is None:
                    val = new_val
            merged_col.append(val)
        merged[key] = merged_col

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


def _resolve_higher_macd_ratio(frequency: str, market: str):
    """返回 frequency 对应的"高周期 MACD 倍率", 找不到返回 None。

    优先查 HIGHER_MACD_RATIO 通用表; 特殊 frequency 走市场专属表
    (30m_TO_D / D_TO_W) 或硬编码 (w=4, m=12)。
    """
    ratio = HIGHER_MACD_RATIO.get(frequency)
    if ratio is None and frequency == "30m":
        ratio = MARKET_30M_TO_D_RATIO.get(market, 8)
    elif ratio is None and frequency == "d":
        ratio = MARKET_D_TO_W_RATIO.get(market, 5)
    elif ratio is None and frequency == "w":
        ratio = 4
    elif ratio is None and frequency == "m":
        ratio = 12
    return ratio


def apply_higher_macd_to_chart_data(
    chart_data: dict,
    frequency: str,
    market: str,
    cl_config: dict,
) -> bool:
    """计算并 in-place 写入 chart_data 的 higher_macd_dif/dea/hist 字段。

    跨周期 MACD 思路: 用 frequency × ratio 放大 fast/slow/signal 周期, 在原
    close 序列上跑 talib.MACD, 输出对齐到 chart_data["c"] 长度。

    Args:
        chart_data: 已含 "c" 字段的 chart_data dict, 本函数 in-place 修改
                   并加入 higher_macd_* 字段; ratio 为 None 时不修改 dict。
        frequency: 当前 K 线周期 (1m / 5m / ... / d / w / m)
        market: 市场标识 (用于 30m_TO_D / D_TO_W 倍率特殊处理)
        cl_config: 缠论配置 (取 idx_macd_fast/slow/signal, 默认 12/26/9)

    Returns:
        bool: True 表示已写入 higher_macd_* 字段; False 表示未写入
              (无倍率 / bar 数不足 / 计算异常)。调用方据此决定是否回写缓存。
    """
    ratio = _resolve_higher_macd_ratio(frequency, market)
    if ratio is None:
        return False  # 该 frequency 无高周期对照, 不做事

    try:
        closes = np.array(chart_data.get("c", []), dtype=float)
        fast = int(cl_config.get("idx_macd_fast", 12)) * ratio
        slow = int(cl_config.get("idx_macd_slow", 26)) * ratio
        signal = int(cl_config.get("idx_macd_signal", 9)) * ratio
        # 确保有足够的 bar 数量用于 MACD 计算 (至少 slow+signal 根 K 线)
        min_bars = slow + signal
        if len(closes) <= min_bars:
            return False  # bar 数不足以算高周期 MACD, 不写字段
        h_dif, h_dea, h_hist = talib.MACD(
            closes, fastperiod=fast, slowperiod=slow, signalperiod=signal,
        )
        h_dif_rounded = np.round(h_dif, 6)
        h_dea_rounded = np.round(h_dea, 6)
        h_hist_rounded = np.round(h_hist, 6)
        chart_data["higher_macd_dif"] = np.where(
            np.isnan(h_dif_rounded), None, h_dif_rounded
        ).tolist()
        chart_data["higher_macd_dea"] = np.where(
            np.isnan(h_dea_rounded), None, h_dea_rounded
        ).tolist()
        chart_data["higher_macd_hist"] = np.where(
            np.isnan(h_hist_rounded), None, h_hist_rounded
        ).tolist()
        return True
    except Exception as e:
        LogUtil.error(f"[apply_higher_macd] Scaled MACD calc failed: {e}")
        return False


def should_lazy_apply_higher_macd(
    chart_data: dict, frequency: str, market: str
) -> bool:
    """判断 cache hit 路径是否需要 lazy 补算 higher_macd (M1)。

    返回 True 需同时满足:
      1. 该 frequency 确有高周期倍率 (``_resolve_higher_macd_ratio`` 非 None);
      2. chart_data 有 bar;
      3. ``higher_macd_hist`` 长度与 bar 数不一致 (缺失或残缺)。

    对 15m / 60m / 2m 等无高周期倍率的 frequency, higher_macd 本就该缺失,
    返回 False —— 否则 tv_history 每次 polling (~3s) 都会因"长度不符"误判为
    需补算, 触发一次冗余的 _set_chart_cache_entry + 异步写盘。
    """
    if _resolve_higher_macd_ratio(frequency, market) is None:
        return False
    bar_count = len(chart_data.get("t", []) or [])
    if bar_count == 0:
        return False
    htf_hist = chart_data.get("higher_macd_hist") or []
    return len(htf_hist) != bar_count


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

    含 enable_kchart_low_to_high / prepend / cl_data_to_tv_chart 三个决策分支。
    返回 None 时调用方应 return {"s": "no_data"}（_mark_chart_cache_validated 已在内部完成）。

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
