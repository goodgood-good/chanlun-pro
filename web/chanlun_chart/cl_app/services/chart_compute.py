"""图表计算服务（service）。

Tier 4 P3 重构：从 blueprints/tv.py 抽出 ``compute_and_cache_chart_data`` 主路径，
让 symbols.py 不再 import tv.py 任何符号；tv.py 真正回归"路由层"。

包含：
- ``_SafeLockRegistry``: weakref + 引用计数的 per-key 锁注册表
- ``chart_calc_locks``: chart_data_cache 的 per-key 计算锁
- ``HIGHER_FREQ_MAP``: 跨周期 MACD 的"当前→高一级"频率映射（真实重采样）
- ``HIGHER_MACD_RATIO`` / ``MARKET_30M_TO_D_RATIO`` / ``MARKET_D_TO_W_RATIO``: 跨周期倍率（索引分桶兜底 + lazy 判定）
- ``_shape_time`` / ``_merge_shape_lists`` / ``_merge_chart_data``: chart 数据合并的纯函数
- ``compute_and_cache_chart_data``: cache miss 后的完整计算路径

不包含（留在 tv.py 中）:
- ``prewarm_common_intervals``: OLD prewarm 路径，与 tv_history 流深度耦合，未迁
- ``_history_req_locks``: 仅 tv_history 内部使用的节流锁（实例化 _SafeLockRegistry）
"""
import bisect
import datetime
import threading
import time
import weakref
from threading import RLock

import numpy as np
import pytz
import talib

from chanlun import fun
from chanlun.exchange.lb_priority import lb_low_priority
from chanlun.cl_utils import (
    cl_data_to_tv_chart,
    kcharts_frequency_h_l_map,
    web_batch_get_cl_datas,
    zs_to_chart_dict,
)
from chanlun.market import Market
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil

from .chart_cache import (
    _TRANSIENT_NEGATIVE_TTL_SECONDS,
    _build_cache_key,
    _is_negatively_cached,
    _klines_fetch_incomplete,
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

# 当前频率 → 高一级频率。MACD_HTF 真实重采样路径用它把当前 K 线归并到高周期。
# 注意：与 _resolve_higher_macd_ratio 覆盖同一组 frequency（含高周期对照的）。
HIGHER_FREQ_MAP = {
    "1m": "5m",
    "5m": "30m",
    "30m": "d",
    "d": "w",
    "w": "m",
    "m": "y",
}

# ---------------- 多周期中枢叠加(P7) ----------------

# 叠加目标周期(MVP 各市场统一;自定义阶梯/目标留扩展)+ 周期→级别展示名
HIGHER_ZS_TARGET = "30m"
_PERIOD_LEVEL_NAME = {
    "5m": "5min级别", "30m": "30min级别", "d": "日线级别", "w": "周线级别",
}


def _zs_level_name(period: str) -> str:
    return _PERIOD_LEVEL_NAME.get(period, f"{period}级别")


def higher_zs_periods(frequency: str):
    """返回当前周期之上、沿阶梯到 HIGHER_ZS_TARGET(含)途经的 [(周期, 级别名)]。

    当前周期 >= 目标(从它沿 HIGHER_FREQ_MAP 走不到 target) → 返回 []。
    例: '1m'→[('5m','5min级别'),('30m','30min级别')]; '5m'→[('30m','30min级别')];
        '30m'/'d'→[]。
    """
    out = []
    cur = frequency
    seen = set()
    while cur in HIGHER_FREQ_MAP and cur not in seen:
        seen.add(cur)
        nxt = HIGHER_FREQ_MAP[cur]
        out.append((nxt, _zs_level_name(nxt)))
        if nxt == HIGHER_ZS_TARGET:
            return out
        cur = nxt
    return []

# 跨 UTC 日界的市场，日级分桶需按本地时区小时偏移修正（其余默认 +8）。
MARKET_DAY_OFFSET_H = {
    "us": -5,
    "ny_futures": -5,
    "currency": 0,
    "currency_spot": 0,
    "fx": 0,
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


# ---------------- 交易时段状态 (TTL 缓存) ----------------

# market → (is_trading, sampled_at)。tv_history 每请求判一次交易时段(方向2),
# 用短 TTL 缓存避免反复构造 exchange / 调 now_trading。
_trading_state_cache: dict = {}
_trading_state_lock = threading.Lock()
_TRADING_STATE_TTL = 30.0  # 秒


def market_now_trading(market: str, now: float = None) -> bool:
    """返回该 market 当前是否处于交易时段(带 30s TTL 缓存)。

    供 chart_cache serve-stale 阈值选择(方向2): 盘中短阈值更快后台刷新, 收盘
    长阈值少折腾静态数据。exchange 构造 / now_trading 异常时保守按"交易中"
    (True)返回 → 走短阈值、更倾向刷新到最新, 不让一次异常把图表钉死在旧快照。

    Args:
        market: 市场代码(a/us/hk/...)。
        now: 当前时间戳(秒); None 取 time.time()(单测可注入以验证 TTL)。
    """
    now = time.time() if now is None else now
    with _trading_state_lock:
        cached = _trading_state_cache.get(market)
        if cached is not None and (now - cached[1]) < _TRADING_STATE_TTL:
            return cached[0]
    try:
        ex = get_exchange(Market(market))
        try:
            trading = bool(ex.now_trading(market))
        except TypeError:
            # 仅 cq 单例(us/hk 共享)的 now_trading 接受 market 消歧; 其余单市场交易所无此形参, 回退无参
            trading = bool(ex.now_trading())
    except Exception:
        trading = True  # 保守: 不确定 → 当作交易中(短阈值, 更勤刷新)
    with _trading_state_lock:
        _trading_state_cache[market] = (trading, now)
    return trading


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
    # 2026-07: 原有的两个生产调用点(compute_and_cache_chart_data / tv.py MISS 分支)
    # 已改为整体替换,不再调用本函数——"起点身份并集"的 shape 合并语义在"陈旧快照 +
    # 全新全量重算"场景下会让已被新行情证伪的旧未完成形态残留(幽灵笔/线段/中枢),
    # 详见 tests/web/test_ghost_shape_no_merge.py。当前仅 tests/web/test_chart_compute_merge.py
    # 作为纯函数单测使用,生产代码零调用;保留供未来若有真正"只追加、起点不变"的合并
    # 场景时参考,新增调用前请先确认不会重现上述幽灵形态问题。
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
    ]
    # time->index 映射只建一次跨字段复用；重复时间戳 dict comprehension 后者覆盖前者。
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
                # 仅当新值有效时覆盖；None 不覆盖已有有效值(OHLCV:新窗口没覆盖的旧 bar 应保留)
                if new_val is not None or val is None:
                    val = new_val
            merged_col.append(val)
        merged[key] = merged_col

    # HTF(跨周期)MACD 三列单独合并:高周期插值产物,首个有效高周期桶之前的 bar 恒 None。
    # 不能套上面"None 不覆盖旧值"的逐 bar 规则(那是为 OHLC 保留历史设计)——否则新计算在某 bar
    # 算出 None、而旧 entry 同 bar 是旧非 None 值时会保留旧值,造成 HTF 边界"旧算法残留点"(审查
    # M-1)。规则:new 覆盖到的 bar 一律以 new 为准(含 None);new 未覆盖的 bar 才保留旧值。
    for key in ("higher_macd_dif", "higher_macd_dea", "higher_macd_hist"):
        existing_values = existing_data.get(key, [])
        new_values = new_data.get(key, [])
        if not existing_values and not new_values:
            merged[key] = []
            continue
        merged_col = []
        for bar_time in all_times:
            ni = new_idx.get(bar_time)
            if ni is not None and ni < len(new_values):
                merged_col.append(new_values[ni])
            else:
                ei = existing_idx.get(bar_time)
                merged_col.append(
                    existing_values[ei]
                    if (ei is not None and ei < len(existing_values))
                    else None
                )
        merged[key] = merged_col

    for key in [
        "fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds", "xd_zslx",
        "xd_zslx_lines",
        "bi_mmds", "xd_mmds", "bi_bcs", "xd_bcs",
    ]:
        merged[key] = _merge_shape_lists(existing_data.get(key, []), new_data.get(key, []))

    # 递归层级树 / 区间套 —— 嵌套结构,不走 shape 合并;新值有则覆盖,否则保留旧值。
    # 这两类是「全局视角」数据,通常在窗口移动时整体重算,直接 overwrite 安全。
    for key in ("recursive_levels", "interval_nest", "higher_zs"):
        if key in new_data:
            merged[key] = new_data[key]
        elif key in existing_data:
            merged[key] = existing_data[key]

    return merged


# ---------------- 主计算路径 ----------------

def compute_and_cache_chart_data(
    market: str, code: str, frequency: str, cl_config: dict, skip_download: bool = False
) -> bool:
    """完整复刻 ``tv_history`` 中 cache miss 后的计算路径，把结果写入 ``chart_data_cache``。

    返回 True 表示成功写入缓存（数据非空），False 表示中途无数据
    （空拉取只写负缓存、不再标 validated：R5-#4，避免重置陈旧快照 validated_at）。

    设计目的：让 ``symbols.py`` 的批量预热与用户实际打开图表时走完全相同的计算逻辑，
    避免预热结果"少算"了 higher_macd 等指标，导致用户切换时仍然 cache miss。

    关键步骤（与 ``tv_history`` 一致）：
    1. ``ex.klines`` 拉数据（支持 enable_kchart_low_to_high 的低周期合成高周期）
    2. ``web_batch_get_cl_datas`` 计算缠论
    3. ``cl_data_to_tv_chart`` 转 TV 图表格式
    4. 跨周期 MACD（higher_macd_dif/dea/hist）按时间戳重采样到高周期后用 talib.MACD 计算
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
    # 预热批量预下载后, 让 ex.klines 跳过逐只 download(数据已在本地库)。仅 A股/QMT 的
    # klines 识别 args["skip_download"]; 其他交易所不传此 args, 行为不变。
    if skip_download:
        kline_args["args"] = {"skip_download": True}

    if (
        cl_config.get("enable_kchart_low_to_high") == "1"
        and kchart_to_frequency is not None
        and frequency_low is not None
    ):
        with lb_low_priority():
            klines = ex.klines(code, frequency_low, **kline_args)
        if _klines_fetch_incomplete(klines):
            # 拉取带洞(数据源暂时不可用):短退避保留旧完整缓存、不标 validated,≤30s 自愈(C1+M3)。
            LogUtil.warning(f"[compute] {market}:{code}:{frequency} 拉取不完整,短退避保留旧缓存")
            _mark_negative_cache(cache_key, ttl=_TRANSIENT_NEGATIVE_TTL_SECONDS)
            return False
        if klines is None or len(klines) == 0:
            # R5-#4: 负缓存防重拉即可; 不标 validated(空拉取会重置既存陈旧快照 validated_at→当 fresh)。
            _mark_negative_cache(cache_key)
            return False
        cd = web_batch_get_cl_datas(market, code, {frequency_low: klines}, cl_config)[0]
    else:
        kchart_to_frequency = None
        with lb_low_priority():
            klines = ex.klines(code, frequency, **kline_args)
        if _klines_fetch_incomplete(klines):
            # 拉取带洞(数据源暂时不可用):短退避保留旧完整缓存、不标 validated,≤30s 自愈(C1+M3)。
            LogUtil.warning(f"[compute] {market}:{code}:{frequency} 拉取不完整,短退避保留旧缓存")
            _mark_negative_cache(cache_key, ttl=_TRANSIENT_NEGATIVE_TTL_SECONDS)
            return False
        if klines is None or len(klines) == 0:
            # R5-#4: 负缓存防重拉即可; 不标 validated(空拉取会重置既存陈旧快照 validated_at→当 fresh)。
            _mark_negative_cache(cache_key)
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
    # P8 取代 P7：停用真实多周期叠加，高级别中枢改由 recursive_levels 产出
    # apply_higher_zs_to_chart_data(cl_chart_data, market, code, frequency, cl_config)

    # 2026-07 修复(幽灵形态): 不再与 existing_entry 做 _merge_chart_data 合并。
    # cl_chart_data 本身就是基于完整回看窗口的全量权威计算结果(is_full_snapshot=True)；
    # existing_entry 可能是几分钟到几天前的陈旧快照。_merge_chart_data 对 bis/xds/
    # bi_zss/mmds 等形态字段用"起点身份并集"语义——若陈旧快照里某个未完成形态的起点
    # 在新计算里已不存在(正在形成的笔被新行情证伪、从别处重新起笔)，旧版本会被原样
    # 保留、与新版本一起返回，前端出现幽灵/重复形态。整体替换才是正确语义，与
    # kline_recompute.prepend_klines_and_replace_cache 的"整体替换"一致。
    with cache_lock:
        _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)
    return True


# 所有可能是"按 t 长度对齐的数组字段"列表 (用于 _slice_window / _trim_future_bars)。
_CHART_ARRAY_FIELDS = (
    "t", "o", "h", "l", "c", "v",
    "macd_dif", "macd_dea", "macd_hist", "macd_area",
    "higher_macd_dif", "higher_macd_dea", "higher_macd_hist",
)

# 形态字段 (笔/段/中枢/分型/背驰/买卖点/走势类型) - 不是按 t 长度对齐, 走 filter_shapes_in_window。
# 注:``recursive_levels`` 与 ``interval_nest`` 是嵌套结构(level/zss/zslxs 或
# links/turning_point),不在此列——它们走整体透传,不按窗口过滤。
# ``bi_mmds``/``xd_mmds``/``bi_bcs``/``xd_bcs`` 是按级别拆分的买卖点/背驰,
# 与合并版 ``mmds``/``bcs`` 并存(前端独立 reconcile)。
_CHART_SHAPE_FIELDS = (
    "fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds", "xd_zslx",
    "xd_zslx_lines", "bi_mmds", "xd_mmds", "bi_bcs", "xd_bcs",
)


def _decide_full_snapshot(first_data_request, to_ts: int, bar_times, source_is_full: bool) -> bool:
    """D4-F1: /tv/history 轮询响应是否置 full_snapshot=True(前端整体替换形态清幽灵)。

    仅当 (1)非首帧(update 路径, first_data_request=='false') (2)请求覆盖最近窗口(to_ts>=末根 bar,
    非向左滚动历史) (3)源为全量快照(source_is_full) 时返回 True。向左滚动/窄窗口 range-miss 结果
    返回 False——否则前端 full_snapshot 整体替换会丢弃窗口外的合法形态(比幽灵更糟)。
    """
    if first_data_request != "false" or not bar_times or not source_is_full:
        return False
    return to_ts == 0 or to_ts >= bar_times[-1]


def _miss_source_is_full(is_range_request, cache_miss_reason, cd_is_none) -> bool:
    """D4-F1/F2: MISS 分支产出的 cl_chart_data 是否全量快照(决定能否发 full_snapshot)。

    与 tv.py 对 entry 写入 is_full_snapshot 的口径一致: 非 range 请求 / cache_empty(均按全量
    回看拉取)/ prepend(cd is None: head/tail gap 整体重算全量)→ 全量; range-miss(窄 kline_args
    独立计算)→ 窄。F2-R1: 漏 cd_is_none 会使 tail_gap 轮询误判为窄 → 纯轮询下幽灵不清。
    """
    return (not is_range_request) or (cache_miss_reason == "cache_empty") or bool(cd_is_none)


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
    # 区间套 / 高级中枢是嵌套结构(不在 SHAPE_FIELDS),属「全局视角」跨窗口仍应可见,整体透传。
    for field in ("interval_nest", "higher_zs"):
        if field in chart_data:
            sliced[field] = chart_data[field]
    # recursive_levels:中枢框/走势类型线(zss/zslx_lines)全局可见整体透传,但每级嵌套的买卖点
    # mmds / 背驰 bcs 是单点形态,必须与顶层 mmds/bcs 同口径按窗口裁切——否则窄窗口滚动请求会把
    # 全量各级买卖点泄漏到该窄历史窗(响应体虚增 + 语义错位:滚到很早的窗却收到当下买卖点 + 前端
    # 双绘,审查 F-2)。
    if "recursive_levels" in chart_data:
        _rl = []
        for _lv in (chart_data.get("recursive_levels") or []):
            if not isinstance(_lv, dict):
                _rl.append(_lv)
                continue
            _lv2 = dict(_lv)
            for _k in ("mmds", "bcs"):
                if _k in _lv2:
                    _lv2[_k] = filter_shapes_in_window(_lv2[_k] or [], from_ts, to_ts)
            _rl.append(_lv2)
        sliced["recursive_levels"] = _rl
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
    # 递归层级树 / 区间套整体透传(同 _slice_window 决策)。
    for field in ("recursive_levels", "interval_nest", "higher_zs"):
        if field in chart_data:
            trimmed[field] = chart_data[field]
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


def _resolve_higher_freq(frequency: str):
    """返回 frequency 的高一级频率, 无对照返回 None。"""
    return HIGHER_FREQ_MAP.get(frequency)


def _higher_bucket_keys(times, frequency: str, market: str):
    """把每根 K 线的时间戳映射成"高一级周期"的分桶 key (numpy int64 数组)。

    key 仅用于分组: 同一高周期 K 线内 key 相同, 且随时间单调不减即可。
      - 1m → 5m / 5m → 30m: 时间戳按秒整除
      - 30m → d: 按(带市场时区偏移的) UTC 日整除
      - d  → w: 按周一对齐的周序号整除
      - w  → m / m → y: 用 datetime64 取月 / 年

    时间戳缺失或非法时返回 None, 调用方据此退回"按倍率索引分桶"。
    """
    higher = _resolve_higher_freq(frequency)
    if higher is None:
        return None
    t = np.asarray(times, dtype=np.int64)
    if t.size == 0:
        return None
    # chart_data["t"] 多为秒级; 偶有毫秒级 → 归一到秒
    if np.median(t) > 1e11:
        t = t // 1000
    if higher == "5m":
        return t // 300
    if higher == "30m":
        return t // 1800
    offset = MARKET_DAY_OFFSET_H.get(market, 8) * 3600
    days = (t + offset) // 86400
    if higher == "d":
        return days
    if higher == "w":
        # 1970-01-01 是周四, +3 后整除 7 即周一对齐的周序号
        return (days + 3) // 7
    dt = t.astype("datetime64[s]")
    if higher == "m":
        return dt.astype("datetime64[M]").astype(np.int64)
    if higher == "y":
        return dt.astype("datetime64[Y]").astype(np.int64)
    return None


def apply_higher_macd_to_chart_data(
    chart_data: dict,
    frequency: str,
    market: str,
    cl_config: dict,
) -> bool:
    """计算并 in-place 写入 chart_data 的 higher_macd_dif/dea/hist 字段。

    跨周期 MACD 思路 (真实重采样): 按时间戳把当前周期 K 线归并到高一级周期
    (1m→5m / 5m→30m / 30m→d / d→w / w→m / m→y), 取每桶最后一根 close 作为
    高周期收盘价, 在高周期 close 序列上用原始 fast/slow/signal 算 MACD。

    桶内逐根: 在相邻两个高周期桶的真值之间, 按 bar 位置做线性插值。桶末根严格
    等于真高周期 MACD, 桶内是平滑过渡的直线段 —— 与高周期图上 MACD 折线的形状
    一致; 既不会出现"连续 5 根一样"的阶梯, 也不会因把 1m 收盘价噪声画进线里而
    锯齿抖动。这样 1m 上显示的就是真实 5m 的 MACD、5m 显示真实 30m, 以此类推。

    (旧实现是"参数 × 倍率"的近似: 在当前周期 close 上拉长 EMA 周期, 与真高周期
    存在零轴附近翻转等偏差, 故改为真实重采样。)

    Args:
        chart_data: 已含 "c" 字段的 chart_data dict; 若含等长 "t" 字段则按时间戳
                   分桶, 否则退回按倍率索引分桶。本函数 in-place 加入 higher_macd_*。
        frequency: 当前 K 线周期 (1m / 5m / 30m / d / w / m)
        market: 市场标识 (用于日级分桶的时区偏移)
        cl_config: 缠论配置 (取 idx_macd_fast/slow/signal, 默认 12/26/9)

    Returns:
        bool: True 表示已写入 higher_macd_* 字段; False 表示未写入
              (无高周期对照 / 桶数不足 / 计算异常)。调用方据此决定是否回写缓存。
    """
    if _resolve_higher_freq(frequency) is None:
        return False  # 该 frequency 无高周期对照, 不做事

    try:
        closes = np.asarray(chart_data.get("c", []), dtype=float)
        n = closes.size
        if n == 0:
            return False

        # 分桶: 优先按时间戳; 时间戳不可用时退回按倍率索引分桶
        times = chart_data.get("t") or []
        keys = None
        if len(times) == n:
            keys = _higher_bucket_keys(times, frequency, market)
        if keys is None:
            ratio = _resolve_higher_macd_ratio(frequency, market)
            if ratio is None:
                return False
            keys = np.arange(n, dtype=np.int64) // ratio

        # keys 单调不减 → 桶边界 = key 变化处; bucket_idx[i] = 第 i 根所属桶序号
        boundaries = np.concatenate(([True], keys[1:] != keys[:-1]))
        bucket_idx = np.cumsum(boundaries) - 1
        bucket_count = int(bucket_idx[-1]) + 1

        fast = int(cl_config.get("idx_macd_fast", 12))
        slow = int(cl_config.get("idx_macd_slow", 26))
        signal = int(cl_config.get("idx_macd_signal", 9))
        # talib.MACD 至少需要 slow+signal 根高周期 K 线才有非 NaN 输出
        if bucket_count <= slow + signal:
            return False

        # 每桶取最后一根 bar 的 close 作为高周期 K 线收盘价
        # (后写覆盖前写 → last_pos[b] 落在桶 b 的最后一根)
        last_pos = np.zeros(bucket_count, dtype=np.int64)
        last_pos[bucket_idx] = np.arange(n)
        htf_closes = closes[last_pos]

        # 高周期逐桶 MACD (权威值; talib.MACD 三线统一从 slow+signal-2 起非 NaN)
        macd_b, dea_b, _ = talib.MACD(
            htf_closes, fastperiod=fast, slowperiod=slow, signalperiod=signal,
        )

        # 桶内逐根: 把真高周期 MACD 视作"定位在每个桶末根"的点, 相邻两点之间按
        # bar 位置线性插值。桶末根落在插值锚点上 → 严格等于真高周期 MACD; 桶内
        # 是平滑直线段, 不阶梯也不锯齿。hist 由插值后的 dif/dea 相减得到, 因线性
        # 插值对减法可交换, 等价于对真 hist 插值, 故三线自洽。
        valid = ~np.isnan(macd_b)
        if int(valid.sum()) < 2:
            return False  # 有效高周期桶不足以插值
        anchor_x = last_pos[valid].astype(float)  # 各有效桶的桶末根下标
        bars = np.arange(n, dtype=float)
        # 首个有效桶之前的 bar 置 NaN; 末桶锚点即 n-1, 不会发生右外推
        dif = np.interp(bars, anchor_x, macd_b[valid], left=np.nan)
        dea = np.interp(bars, anchor_x, dea_b[valid], left=np.nan)
        hist = dif - dea

        dif = np.round(dif, 6)
        dea = np.round(dea, 6)
        hist = np.round(hist, 6)
        chart_data["higher_macd_dif"] = np.where(np.isnan(dif), None, dif).tolist()
        chart_data["higher_macd_dea"] = np.where(np.isnan(dea), None, dea).tolist()
        chart_data["higher_macd_hist"] = np.where(np.isnan(hist), None, hist).tolist()
        return True
    except Exception as e:
        LogUtil.error(f"[apply_higher_macd] HTF resample MACD calc failed: {e}")
        return False


def _higher_zs_for_period(market: str, code: str, hf: str, cl_config: dict) -> list:
    """取 hf 周期 K 线→新核心→L1 线段中枢→zs_to_chart_dict 列表。

    异常/无数据/无 L1 → 返回 [](优雅降级,不阻断主图)。
    """
    try:
        ex = get_exchange(Market(market))
        klines = ex.klines(code, hf)
        if klines is None or len(klines) == 0:
            return []
        cd = web_batch_get_cl_datas(market, code, {hf: klines}, cl_config)[0]
        levels = cd.get_recursive_branch_levels() or []
        # 取 L0=线段中枢(新核心 L0=线段构建,是该周期最低正式级别中枢)。
        # "5min 级别"= 5m 数据的线段中枢、"30min 级别"= 30m 的线段中枢。
        l0 = next((lv for lv in levels if lv.level == 0), None)
        if l0 is None:
            return []
        # 中枢区间用核心区 [ZD,ZG](标准中枢);GG/DD 是瞬间波动范围、非中枢区间。
        return [zs_to_chart_dict(zs, use_envelope=False) for zs in l0.zss]
    except Exception as e:
        LogUtil.error(f"[apply_higher_zs] period={hf} code={code} failed: {e}")
        return []


def apply_higher_zs_to_chart_data(
    chart_data: dict, market: str, code: str, frequency: str, cl_config: dict
) -> bool:
    """低周期图叠加更高真实周期的 L1 线段中枢,in-place 写 chart_data['higher_zs']。

    返回是否写入(高周期图/配置关→False)。单级取数失败该级为空,不阻断其他级。

    P8 取代 P7：单周期递归扩展已产出高级别中枢(recursive_levels)，此真实多周期叠加停用；保留实现可逆。
    """
    return False  # P8 取代 P7：单周期递归扩展已产出高级别中枢(recursive_levels)，此真实多周期叠加停用；保留实现可逆
    if cl_config.get("chart_show_higher_zs", "1") != "1":
        return False
    periods = higher_zs_periods(frequency)
    if not periods:
        return False
    result = []
    for hf, level_name in periods:
        result.append({
            "period": hf,
            "level_name": level_name,
            "zss": _higher_zs_for_period(market, code, hf, cl_config),
        })
    chart_data["higher_zs"] = result
    return True


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
        if _klines_fetch_incomplete(klines):
            # D3-M2: 带洞(数据源瞬时失败)不等于确认无数据; 不标 validated, 保留旧 validated_at 让
            # stale 判定继续生效、约 30s 自愈(C1/M3), 否则陈旧/带洞缠论会被当 fresh 下发。
            return None
        if klines is None or len(klines) == 0:
            # R5-#4: 空拉取(非cq源瞬时劣化返[]无fetch_incomplete标记)不得标 validated——那会把
            # 既存 too_stale 全量快照的 validated_at 重置为 now→数小时前旧缠论(含悬空未完成笔)被
            # 当 fresh 下发。保留旧 validated_at 让 stale 判定继续生效并自愈(同上方 fetch_incomplete)。
            return None
        cd = web_batch_get_cl_datas(market, code, {frequency_low: klines}, cl_config)[0]
    else:
        kchart_to_frequency = None
        import time as _time
        _kl_t0 = _time.time()
        klines = ex.klines(code, frequency, **kline_args)
        LogUtil.info(
            f"[fetch_klines] {market}:{code} {frequency} ex.klines="
            f"{(_time.time() - _kl_t0) * 1000:.0f}ms rows="
            f"{0 if klines is None else len(klines)}"
        )
        if _klines_fetch_incomplete(klines):
            # D3-M2: 带洞(数据源瞬时失败)不等于确认无数据; 不标 validated, 保留旧 validated_at 让
            # stale 判定继续生效、约 30s 自愈(C1/M3), 否则陈旧/带洞缠论会被当 fresh 下发。
            return None
        if klines is None or len(klines) == 0:
            # R5-#4: 空拉取(非cq源瞬时劣化返[]无fetch_incomplete标记)不得标 validated——那会把
            # 既存 too_stale 全量快照的 validated_at 重置为 now→数小时前旧缠论(含悬空未完成笔)被
            # 当 fresh 下发。保留旧 validated_at 让 stale 判定继续生效并自愈(同上方 fetch_incomplete)。
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
            import time as _time
            _cl_t0 = _time.time()
            cd = web_batch_get_cl_datas(market, code, {frequency: klines}, cl_config)[0]
            LogUtil.info(
                f"[first_load] {market}:{code} {frequency} cl_calc="
                f"{(_time.time() - _cl_t0) * 1000:.0f}ms rows={0 if klines is None else len(klines)}"
            )
            # 首次加载(cache miss)的 CL 存入池, 让首次后第一次轮询/SSE 走增量而非 full,
            # 消除多标的 + 拉取争 CPU 时 full 重算飙到几十秒(实测 52s)的问题。
            from .kline_recompute import store_cl_to_pool
            store_cl_to_pool(cache_key, cd, klines, cl_config, market)

    # _to < first_kline_date: 用户请求的窗口完全在数据起点之前 → no_data
    if to_ts > 0 and len(klines) > 0 and to_ts < fun.datetime_to_int(klines.iloc[0]["date"]):
        with cache_lock:
            _mark_chart_cache_validated(cache_key)
        return None

    # 普通路径: cd 非空 → 跑 cl_data_to_tv_chart 转 chart_data
    cl_chart_data_local = None
    if cd is not None:
        import time as _time
        _ex_t0 = _time.time()
        cl_chart_data_local = cl_data_to_tv_chart(
            cd, cl_config, to_frequency=kchart_to_frequency
        )
        LogUtil.info(
            f"[first_load] {market}:{code} {frequency} extract="
            f"{(_time.time() - _ex_t0) * 1000:.0f}ms"
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
