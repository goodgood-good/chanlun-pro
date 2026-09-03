"""图表计算服务（service）。

Tier 4 P3 重构：从 blueprints/tv.py 抽出 ``compute_and_cache_chart_data`` 主路径，
让 symbols.py 不再 import tv.py 任何符号；tv.py 真正回归"路由层"。

包含：
- ``_SafeLockRegistry``: weakref + 引用计数的 per-key 锁注册表
- ``chart_calc_locks``: chart_data_cache 的 per-key 计算锁
- ``compute_and_cache_chart_data``: cache miss 后的完整计算路径

``_history_req_locks`` 仍由路由层实例化，因为它只服务单次 UDF 请求节流。
"""
import bisect
import datetime
import threading
import time
import weakref
from threading import RLock

import pytz

from chanlun import fun
from chanlun.exchange.lb_priority import lb_low_priority
from chanlun.cl_utils import (
    build_strict_chart_cd,
    cl_data_to_tv_chart,
)
from chanlun.market import Market
from chanlun.exchange import (
    get_exchange,
    market_now_trading as exchange_market_now_trading,
)
from chanlun.exchange.kline_completion import drop_unclosed_last_bar
from chanlun.tools.log_util import LogUtil

from .chart_cache import (
    _TRANSIENT_NEGATIVE_TTL_SECONDS,
    _build_cache_key,
    _is_negatively_cached,
    _klines_fetch_incomplete,
    _mark_chart_cache_validated,
    _mark_negative_cache,
    _set_chart_cache_entry,
)

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
        trading = bool(exchange_market_now_trading(ex, market))
    except Exception:
        trading = True  # 保守: 不确定 → 当作交易中(短阈值, 更勤刷新)
    with _trading_state_lock:
        _trading_state_cache[market] = (trading, now)
    return trading


# ---------------- chart data 合并（纯函数）----------------

def serialize_chart_data_with_strict_runtime(
    *,
    market,
    code,
    display_frequency,
    display_klines,
    chart_config,
    strict_runtime=None,
):
    """Serialize one chart from one strict runtime and one closed-bar prefix."""

    source_attrs = dict(getattr(display_klines, "attrs", {}))
    completed_klines = display_klines
    while completed_klines is not None and len(completed_klines) > 0:
        closed_prefix = drop_unclosed_last_bar(
            completed_klines,
            display_frequency,
            time_label="end",
        )
        if len(closed_prefix) == len(completed_klines):
            break
        completed_klines = closed_prefix

    if completed_klines is None or len(completed_klines) == 0:
        LogUtil.warning(
            "[chart_compute] no completed bars "
            f"market={market} code={code} frequency={display_frequency}"
        )
        return None

    if len(completed_klines) != len(display_klines):
        dropped = len(display_klines) - len(completed_klines)
        completed_klines = completed_klines.copy(deep=True)
        completed_klines.attrs.clear()
        completed_klines.attrs.update(source_attrs)
        LogUtil.info(
            f"[chart_compute] removed {dropped} unclosed/future bars "
            f"market={market} code={code} frequency={display_frequency}"
        )

    if strict_runtime is None:
        strict_runtime = build_strict_chart_cd(
            market=market,
            code=code,
            frequency=display_frequency,
            frame=completed_klines,
        )
    return cl_data_to_tv_chart(
        completed_klines,
        chart_config,
        market=market,
        code=code,
        frequency=display_frequency,
        strict_runtime=strict_runtime,
    )

def compute_and_cache_chart_data(
    market: str,
    code: str,
    frequency: str,
    cl_config: dict,
    skip_download: bool = False,
    incremental_refresh_days: int | None = None,
) -> bool:
    """全量计算前先取 per-key chart_calc_locks(与 tv_history/_do_revalidate 同锁域), 消除
    预热的 cl_data_to_tv_chart 读取共享 CL 时，可能与用户增量路径的 process_klines 改写
    产生并发撕裂；原缓存锁只保护字典写入，遗漏了共享 CL 的并发读写。RLock 可重入：
    _do_revalidate 已持锁的嵌套调用即成功、行为不变; 裸 prewarm(symbols.py)取新锁→与用户互斥。
    非阻塞：他方正持锁计算同一键时让位（其结果会入缓存，保持“预热让位用户”语义、不增加用户
    延迟), 跳过视为已覆盖返回 True。"""
    cache_key = _build_cache_key(market, code, frequency, cl_config)
    _calc_lock = chart_calc_locks.get(cache_key)
    if not _calc_lock.acquire(blocking=False):
        return True
    try:
        return _compute_and_cache_chart_data_impl(
            market,
            code,
            frequency,
            cl_config,
            skip_download,
            incremental_refresh_days,
        )
    finally:
        _calc_lock.release()


def _compute_and_cache_chart_data_impl(
    market: str,
    code: str,
    frequency: str,
    cl_config: dict,
    skip_download: bool = False,
    incremental_refresh_days: int | None = None,
) -> bool:
    """完整复刻 ``tv_history`` 中 cache miss 后的计算路径，把结果写入 ``chart_data_cache``。

    返回 True 表示成功写入缓存（数据非空），False 表示中途无数据
    （空拉取只写负缓存、不再标记已验证，避免重置陈旧快照的 validated_at）。

    设计目的：让 ``symbols.py`` 的批量预热与用户实际打开图表时走完全相同的计算逻辑，
    避免预热结果"少算"了 higher_macd 等指标，导致用户切换时仍然 cache miss。

    拉取并聚合行情后构建严格结构快照，再补充图表用高周期 MACD，最终以
    ``is_full_snapshot=True`` 整体写入缓存。
    """
    tz_sh = pytz.timezone("Asia/Shanghai")
    cache_key = _build_cache_key(market, code, frequency, cl_config)

    # 2026-04 修复：负缓存。最近 5 分钟内已经确认无数据的 cache_key 直接返回，
    # 不再调 ex.klines() 浪费 HTTP 配额。
    if _is_negatively_cached(cache_key):
        return False

    ex = get_exchange(Market(market))

    kline_args = {
        "end_date": datetime.datetime.now(tz_sh).strftime("%Y-%m-%d %H:%M:%S")
    }
    # 预热批量预下载后, 让 ex.klines 跳过逐只 download(数据已在本地库)。仅 A股/QMT 的
    # klines 识别 args["skip_download"]; 其他交易所不传此 args, 行为不变。
    if skip_download and incremental_refresh_days is not None:
        raise ValueError("skip_download and incremental_refresh_days are mutually exclusive")
    if skip_download:
        kline_args["args"] = {"skip_download": True}
    elif incremental_refresh_days is not None:
        kline_args["args"] = {
            "incremental_refresh_days": incremental_refresh_days,
        }

    with lb_low_priority():
        klines = ex.klines(code, frequency, **kline_args)
    if _klines_fetch_incomplete(klines):
        LogUtil.warning(
            f"[compute] {market}:{code}:{frequency} 拉取不完整,短退避保留旧缓存"
        )
        _mark_negative_cache(cache_key, ttl=_TRANSIENT_NEGATIVE_TTL_SECONDS)
        return False
    if klines is None or len(klines) == 0:
        _mark_negative_cache(cache_key)
        return False
    display_klines = klines

    cl_chart_data = serialize_chart_data_with_strict_runtime(
        market=market,
        code=code,
        display_frequency=frequency,
        display_klines=display_klines,
        chart_config=cl_config,
    )
    if cl_chart_data is None:
        _mark_negative_cache(cache_key)
        _mark_chart_cache_validated(cache_key)
        return False

    # 完整回看窗口的严格结果是本次唯一权威快照；直接整体替换，避免保留已撤销形态。
    _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)
    return True


# 所有可能是"按 t 长度对齐的数组字段"列表 (用于 _slice_window / _trim_future_bars)。
_CHART_ARRAY_FIELDS = (
    "t", "o", "h", "l", "c", "v",
    "macd_dif", "macd_dea", "macd_hist", "macd_area",
    "higher_macd_dif", "higher_macd_dea", "higher_macd_hist",
)

# 分型、笔、段是严格快照之外需要按窗口裁切的基础图元。中枢、走势、背驰和
# 三类买卖点只存在于原子化 ``strict_structure``，不再保留第二套顶层传输字段。
_CHART_SHAPE_FIELDS = ("fxs", "bis", "xds")


_CALENDAR_FREQUENCY_ALIASES = {
    "D": "d",
    "1D": "d",
    "2D": "2d",
    "3D": "3d",
    "W": "w",
    "1W": "w",
    "M": "m",
    "1M": "m",
    "3M": "q",
    "12M": "y",
}


def _calendar_frequency(frequency) -> str:
    value = str(frequency or "")
    if value in _CALENDAR_FREQUENCY_ALIASES:
        return _CALENDAR_FREQUENCY_ALIASES[value]
    if value in {"d", "2d", "3d", "w", "m", "q", "y"}:
        return value
    return ""


def chart_bar_time_coordinate(timestamp: int, frequency: str) -> int:
    """Map a raw market-close timestamp to TradingView's calendar coordinate.

    Intraday timestamps retain their exact source instant.  Calendar bars use
    UTC period anchors for chart geometry, while the raw close remains the
    authoritative identity timestamp carried by ``bars_result.times``.
    """

    source_ts = int(timestamp)
    calendar_frequency = _calendar_frequency(frequency)
    if not calendar_frequency:
        return source_ts

    source_dt = datetime.datetime.fromtimestamp(
        source_ts,
        tz=datetime.timezone.utc,
    )
    year = source_dt.year
    month = source_dt.month
    day = source_dt.day
    if calendar_frequency == "w":
        source_dt -= datetime.timedelta(days=source_dt.weekday())
        year, month, day = source_dt.year, source_dt.month, source_dt.day
    elif calendar_frequency == "m":
        day = 1
    elif calendar_frequency == "q":
        month = ((month - 1) // 3) * 3 + 1
        day = 1
    elif calendar_frequency == "y":
        month = 1
        day = 1

    return int(datetime.datetime(
        year,
        month,
        day,
        tzinfo=datetime.timezone.utc,
    ).timestamp())


def chart_bar_time_coordinates(bar_times, frequency: str) -> list[int]:
    return [chart_bar_time_coordinate(ts, frequency) for ts in (bar_times or [])]


def _decide_full_snapshot(
    first_data_request,
    to_ts: int,
    bar_times,
    source_is_full: bool,
    frequency: str | None = None,
) -> bool:
    """D4-F1: /tv/history 轮询响应是否置 full_snapshot=True(前端整体替换形态清幽灵)。

    仅当 (1)非首帧(update 路径, first_data_request=='false') (2)请求覆盖最近窗口(to_ts>=末根 bar,
    非向左滚动历史) (3)源为全量快照(source_is_full) 时返回 True。向左滚动/窄窗口 range-miss 结果
    返回 False——否则前端 full_snapshot 整体替换会丢弃窗口外的合法形态(比幽灵更糟)。
    """
    if first_data_request != "false" or not bar_times or not source_is_full:
        return False
    latest_coordinate = chart_bar_time_coordinate(bar_times[-1], frequency or "")
    return to_ts == 0 or to_ts >= latest_coordinate


def strict_structure_history_fields(
    chart_data: dict,
    *,
    authoritative: bool,
    expected_source_closed_at: int | None = None,
) -> dict:
    """Return the three-state atomic strict-structure history contract."""

    if not authoritative:
        return {"strict_structure_mode": "unchanged"}
    mode = chart_data.get("strict_structure_mode")
    if mode == "replace":
        strict = chart_data.get("strict_structure")
        if (
            not isinstance(strict, dict)
            or strict.get("schema") != "chanlun-chart-structure"
        ):
            raise ValueError("replace 模式要求当前严格图表结构协议")
        if (
            expected_source_closed_at is not None
            and strict.get("source_closed_at") != expected_source_closed_at
        ):
            return {
                "strict_structure_mode": "unavailable",
                "strict_structure_error": {"code": "strict_context_mismatch"},
            }
        return {
            "strict_structure_mode": "replace",
            "strict_structure": strict,
        }
    if mode == "unavailable":
        error = chart_data.get("strict_structure_error")
        if not isinstance(error, dict) or not error.get("code"):
            error = {"code": "strict_evidence_invalid"}
        return {
            "strict_structure_mode": "unavailable",
            "strict_structure_error": error,
        }
    return {
        "strict_structure_mode": "unavailable",
        "strict_structure_error": {"code": "strict_evidence_missing"},
    }


def _miss_source_is_full(is_range_request, cache_miss_reason, cd_is_none) -> bool:
    """D4-F1/F2: MISS 分支产出的 cl_chart_data 是否全量快照(决定能否发 full_snapshot)。

    与 tv.py 对 entry 写入 is_full_snapshot 的口径一致: 非 range 请求 / cache_empty(均按全量
    回看拉取)/ prepend(cd is None: head/tail gap 整体重算全量)→ 全量; range-miss(窄 kline_args
    独立计算）为窄范围。遗漏 cd_is_none 会使 tail_gap 轮询误判为窄范围，
    导致纯轮询下残留数据无法清除。
    """
    return (not is_range_request) or (cache_miss_reason == "cache_empty") or bool(cd_is_none)


def filter_shapes_in_window(
    shapes,
    from_ts: int,
    to_ts: int,
    frequency: str | None = None,
) -> list:
    """按 [from_ts, to_ts) 窗口过滤基础结构图元。

    多点形态 (笔/段): 与 [from_ts, to_ts) 有重叠即保留 (起点早于 to_ts 且
    终点晚于 from_ts), 避免"跨可视边界"形态丢失。
    单点形态 (分型): 点位需落在窗口内。

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
            t_start = chart_bar_time_coordinate(
                pts[0].get("time", 0), frequency or ""
            )
            t_end = chart_bar_time_coordinate(
                pts[-1].get("time", 0), frequency or ""
            )
            if t_end >= from_ts and (to_ts == 0 or t_start < to_ts):
                res.append(shape)
        elif isinstance(pts, dict):
            t = chart_bar_time_coordinate(
                pts.get("time", 0), frequency or ""
            )
            if t >= from_ts and (to_ts == 0 or t < to_ts):
                res.append(shape)
    return res


def slice_chart_data_to_window(
    chart_data: dict,
    from_ts: int,
    to_ts: int,
    frequency: str | None = None,
) -> dict:
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

    comparison_times = chart_bar_time_coordinates(bar_times, frequency or "")
    start_idx = bisect.bisect_left(comparison_times, from_ts)
    # bisect_left 让 to_ts 排他上界, 避免向左滚动返回相同 K 线
    end_idx = (
        bisect.bisect_left(comparison_times, to_ts)
        if to_ts > 0
        else len(bar_times)
    )

    sliced: dict = {}
    for field in _CHART_ARRAY_FIELDS:
        arr = chart_data.get(field) or []
        sliced[field] = arr[start_idx:end_idx] if arr else []
    for field in _CHART_SHAPE_FIELDS:
        sliced[field] = filter_shapes_in_window(
            chart_data.get(field, []) or [], from_ts, to_ts, frequency
        )
    return sliced


def slice_chart_data_to_countback(
    chart_data: dict,
    countback: int,
    frequency: str | None = None,
) -> dict:
    """Return only the newest ``countback`` bars and matching basic shapes.

    TradingView already expands ``countBack`` for indicators and requests older
    ranges when the user scrolls left.  Sending the complete 10k-20k bar cache
    on every first load wastes JSON encoding, transfer and browser parse time.
    The authoritative strict snapshot remains outside this projection; this
    helper only bounds the transport window and never mutates the cached graph.
    """

    if type(countback) is not int or countback <= 0:
        return dict(chart_data)
    bar_times = chart_data.get("t", []) or []
    if len(bar_times) <= countback:
        return dict(chart_data)
    comparison_times = chart_bar_time_coordinates(bar_times, frequency or "")
    return slice_chart_data_to_window(
        chart_data,
        comparison_times[-countback],
        0,
        frequency=frequency,
    )


def trim_future_bars(
    chart_data: dict,
    to_ts: int,
    frequency: str | None = None,
) -> dict:
    """裁剪 chart_data 中时间戳 > to_ts 的"未来" bar (P5 second step)。

    交易所偶尔返回尚未完成的下一根 K 线 (timestamp > 当前时间), TradingView
    缓存后会在 DataPulse 更新时撞 time order violation。本函数对所有 t 长度
    对齐字段做 [:_resp_end] 切片, ``_resp_end`` = ``bisect_right(t, to_ts)``。

    Args:
        chart_data: 已切到可视窗口的 chart_data
        to_ts: 请求 to 时间戳 (0/负数 → 不裁)

    Returns:
        裁剪后的新 dict (浅拷贝)。形态字段不裁 (笔/段的 end.time 可能 > to_ts
        但起点在窗口内, 仍要展示, 由 ``filter_shapes_in_window`` 保留)。
    """
    if to_ts <= 0:
        return dict(chart_data)
    times = chart_data.get("t") or []
    comparison_times = chart_bar_time_coordinates(times, frequency or "")
    if not times or comparison_times[-1] <= to_ts:
        return dict(chart_data)
    resp_end = bisect.bisect_right(comparison_times, to_ts)
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
    """Fetch bars and compute one strict chart snapshot for a cache miss."""

    from .kline_recompute import prepend_klines_and_replace_cache

    ex = get_exchange(Market(market))
    fetched_frequency = frequency

    import time as _time

    fetch_started = _time.time()
    klines = ex.klines(code, fetched_frequency, **kline_args)
    LogUtil.info(
        f"[fetch_klines] {market}:{code} {fetched_frequency} ex.klines="
        f"{(_time.time() - fetch_started) * 1000:.0f}ms rows="
        f"{0 if klines is None else len(klines)}"
    )
    if _klines_fetch_incomplete(klines):
        return None
    if klines is None or len(klines) == 0:
        return None

    used_prepend = False
    cl_chart_data = None
    if (
        is_range_request
        and cache_miss_reason
        in {
            "cache_head_gap",
            "cache_partial_snapshot",
            "cache_tail_gap",
        }
    ):
        cl_chart_data = prepend_klines_and_replace_cache(
            market,
            code,
            frequency,
            cl_config,
            new_klines=klines,
            cache_key=cache_key,
        )
        if cl_chart_data is None:
            _mark_chart_cache_validated(cache_key)
            return None
        display_klines = klines
        used_prepend = True
    else:
        display_klines = klines

    if (
        to_ts > 0
        and len(klines) > 0
        and to_ts < fun.datetime_to_int(klines.iloc[0]["date"])
    ):
        _mark_chart_cache_validated(cache_key)
        return None

    if cl_chart_data is None:
        serialize_started = _time.time()
        cl_chart_data = serialize_chart_data_with_strict_runtime(
            market=market,
            code=code,
            display_frequency=frequency,
            display_klines=display_klines,
            chart_config=cl_config,
        )
        LogUtil.info(
            f"[first_load] {market}:{code} {frequency} strict_extract="
            f"{(_time.time() - serialize_started) * 1000:.0f}ms"
        )
        if cl_chart_data is None:
            _mark_chart_cache_validated(cache_key)
            return None

    return {
        "cl_chart_data": cl_chart_data,
        "cache_already_written": used_prepend,
        "is_full_snapshot": _miss_source_is_full(
            is_range_request,
            cache_miss_reason,
            used_prepend,
        ),
    }
