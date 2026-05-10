"""
TradingView相关接口蓝图。
(终极修复版：修复 minmov 校验错误 + 全量数据返回 + 内存缓存)
"""
import pytz
import json
import hashlib
import datetime
import random
import time
import threading
import weakref
import numpy as np
import talib
from cachetools import TTLCache
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import RLock, Semaphore
from typing import Dict, List, Optional
import bisect
from flask import Blueprint, request
from flask_login import login_required
import pinyin

from chanlun import config, fun
from chanlun.base import Market
from chanlun.cl_utils import (
    cl_data_to_tv_chart,
    kcharts_frequency_h_l_map,
    query_cl_chart_config,
    web_batch_get_cl_datas,
)
from chanlun.db import db
from chanlun.exchange import get_exchange
from chanlun.file_db import fdb
from chanlun.tools.log_util import LogUtil

from ..csrf import csrf
from ..services.constants import (
    frequency_maps,
    resolution_maps,
    market_frequencys,
    market_session,
    market_timezone,
    market_types,
)
from ..services.state import history_req_counter as __history_req_counter


tv_bp = Blueprint("tv", __name__)

# 跨周期 MACD 倍率常量已迁到 services.chart_compute（Tier 4 P3），
# 见模块顶部 re-export。

# stock_cache + 全套 symbols 预加载逻辑已迁到 services.stock_list (Tier 4 P2)，
# 见模块顶部 re-export。

# 图表缓存基础设施（chart_data_cache / cache_lock / 工具函数）已迁移到
# ..services.chart_cache（L1 Phase 2）。这里 re-export 让 tv.py 内部既有引用
# （_set_chart_cache_entry / _mark_chart_cache_validated / _persist_chart_cache_async
# 等业务函数）以及 line 600 处的 _stable_hash 调用都不需要修改即可继续工作。
from ..services.chart_cache import (  # noqa: E402
    _CACHE_REVALIDATION_INTERVAL,
    _build_cache_key,
    _build_chart_cache_entry,
    _cache_entry_recently_validated,
    _chart_cache_disk_executor,
    _get_chart_cache_entry,
    _is_negatively_cached,
    _mark_chart_cache_validated,
    _mark_negative_cache,
    _normalize_cache_entry,
    _persist_chart_cache_async,
    _set_chart_cache_entry,
    _stable_hash,
    cache_lock,
    chart_data_cache,
)

# 磁盘异步写入器 / 落盘函数已迁到 services.chart_cache（Tier 4 P1），见模块顶部 re-export。


# ---------------------------------------------------------------------------
# 批量预热活动注册表（service 层，与 symbols.py PrewarmManager 协作）
# ---------------------------------------------------------------------------
# L1 重构后，状态与函数迁移到 ..services.prewarm_status；这里 re-export 让
# 本模块内部既有的 ``is_batch_prewarm_active(market)`` 调用（line 660 等）
# 不需要修改即可继续工作；symbols.py 直接 import 自 service 模块。
from ..services.prewarm_status import (  # noqa: E402
    is_batch_prewarm_active,
    mark_batch_prewarm_active,
)

# Pre-warm cache for common interval switches to reduce recomputation
_MAX_PREWARMED_SIZE = 50  # prewarmed 集合上限，防止线程异常导致无限增长
# 已"在飞行中"的 prewarm cache_key 集合，避免同一个 key 被多个 prewarm 任务重复计算。
# 设计上始终通过 discard 移除，size 不会超过同时 in-flight 的预热任务数（极小）。
chart_data_cache_stats = {"prewarmed": set()}
# 用户实际同时打开的 4 个周期（界面默认布局）。砍掉 15/60/1W 后单标的预热从 ~20s 降到 ~5s。
# 如果将来用户启用别的周期，TV chart 自己的 first=true 请求会触发计算，不会丢功能。
COMMON_INTERVALS = ["1", "5", "30", "1D"]

# Prewarm 全局并发限制：xtquant native 不是线程安全的，且每次启动 prewarm 会拉 7 个周期，
# 多个 prewarm 线程并发会直接撞 BSON 断言。这里用信号量限制全局只有 1 个 prewarm 在飞行。
_PREWARM_MAX_CONCURRENT = 1
_prewarm_semaphore = Semaphore(_PREWARM_MAX_CONCURRENT)
# 记录最近一次 prewarm 的目标 symbol，用户快速切换时旧任务可主动放弃。
_prewarm_latest_target = {"key": None}
_prewarm_target_lock = threading.Lock()

# 2026-04 修复：prewarm 入口去重（dedupe）。
# 问题：用户界面上 4 个面板看同一标的不同周期时，4 个 first=true 几乎同时进来，
# 每个 first=true 都会触发一次 prewarm_common_intervals → 启动 4 个预热线程；
# 全局信号量只放过去 1 个跑、其余 3 个 skip 掉，但每次还是会读锁、调用 _stable_hash、
# 写 _prewarm_latest_target，浪费 CPU；更糟的是，4 个 prewarm 各自 hold 一段
# semaphore 等待时间，"哪个先抢到 semaphore"是随机的，会反复打断 _is_still_latest 检查。
#
# 修复：记录每个 (market, code, cl_config_hash) 最近一次 prewarm 的启动时间戳，
# 30 秒内同 target 再来直接 return，连线程都不启动。
# 30s 是经验值：用户切回同标的的频率通常 > 30s（除非主动反复切换），
# 而 4 面板齐发的 first=true 时间窗口在 ~100ms 内，30s 远超这个窗口，足够去重。
_PREWARM_DEDUPE_TTL_SECONDS = 30.0
_prewarm_recent_targets: Dict[str, float] = {}
_prewarm_dedupe_lock = threading.Lock()

# 负缓存（_is_negatively_cached / _mark_negative_cache）已迁到 services.chart_cache，
# 见模块顶部 re-export。

# 注：原 _CACHE_REVALIDATION_INTERVAL / cache_lock 定义已迁到 services.chart_cache，
# 见模块顶部 re-export。原 req_lock 全局 RLock 已被 H7 拆为 per-key 的
# _history_req_locks（见下方），全文已无任何引用，故移除避免误用。

# ---------------------------------------------------------------------------
# 用户活跃度跟踪（service 层，供 symbols.py 的批量预热让位用）
# ---------------------------------------------------------------------------
# L1 重构后，状态与函数迁移到 ..services.user_activity；本模块内部仅在
# tv_history 入口处调用 ``_mark_user_request`` 写入；symbols.py 直接 import 自 service。
from ..services.user_activity import (  # noqa: E402
    _get_last_user_request_time,
    _get_user_recent_codes,
    _mark_user_request,
)
# stock_list 服务（Tier 4 P2）：symbols 预加载、缓存、读取
from ..services.stock_list import (  # noqa: E402
    _preload_single_exchange,
    _process_stock_list,
    _safe_all_stocks,
    _trigger_async_refresh,
    PRELOAD_EXCHANGES,
    PRELOAD_INTERVAL_SECONDS,
    PRELOAD_PARALLEL_WORKERS,
    PRELOAD_STARTUP_DELAY_SECONDS,
    get_cached_processed_stocks,
    preload_symbols,
    start_symbol_preload_thread,
    stock_cache,
)
# chart_compute 服务（Tier 4 P3）：MACD 倍率 + 锁注册表 + chart 合并 + 主计算路径
from ..services.chart_compute import (  # noqa: E402
    HIGHER_MACD_RATIO,
    MARKET_30M_TO_D_RATIO,
    MARKET_D_TO_W_RATIO,
    _SafeLockRegistry,
    _merge_chart_data,
    _merge_shape_lists,
    _shape_time,
    chart_calc_locks,
    compute_and_cache_chart_data,
)
from ..services.kline_recompute import (  # noqa: E402
    prepend_klines_and_replace_cache,
)


# _stable_hash / _build_cache_key 已迁到 services.chart_cache，见模块顶部 re-export

def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        return int(float(value))
    except Exception:
        return default


def _normalize_unix_ts(value, default=0):
    ts = _safe_int(value, default)
    if ts > 10**12:
        ts = ts // 1000
    return ts


def _normalize_resolution(resolution: str):
    if resolution is None:
        return None
    return {"D": "1D", "W": "1W", "M": "1M"}.get(resolution, resolution)


def _parse_tv_symbol(symbol: str):
    if not symbol:
        return None, None
    symbol = symbol.strip()
    if ":" in symbol:
        market, code = symbol.split(":", 1)
        market = market.lower().strip()
        code = code.strip()
        if market in market_types and code != "":
            return market, code
    upper_symbol = symbol.upper()
    if upper_symbol.endswith(".US"):
        return "us", upper_symbol
    return None, None


# _build_chart_cache_entry / _normalize_cache_entry / _get_chart_cache_entry 已迁到
# services.chart_cache，见模块顶部 re-export


# _set_chart_cache_entry / _mark_chart_cache_validated / _cache_entry_recently_validated
# 已迁到 services.chart_cache，见模块顶部 re-export


# _shape_time / _merge_shape_lists / _merge_chart_data 已迁到 services.chart_compute
# （Tier 4 P3），见模块顶部 re-export。


def _drawing_storage_name(chart_id: str, layout_id: str, symbol: str, resolution: str):
    return f"drawings_{layout_id}_{chart_id}_{symbol}_{resolution}"


def _legacy_drawing_storage_name(symbol: str, resolution: str):
    return f"drawings_{symbol}_{resolution}"


# compute_and_cache_chart_data 已迁到 services.chart_compute（Tier 4 P3），
# 见模块顶部 re-export。

# 单标的内 4 周期是否并行预热。
# - HTTP 数据源（cq/polygon/futu）：True，并行可省 3-4 倍时间
# - native 数据源（xtquant/tdx）：False，因为 native 客户端线程不安全
# 启动时根据 market 决定，但 prewarm_common_intervals 会针对每次调用动态选。
_PREWARM_INTERVALS_PARALLEL_MARKETS = {"us", "hk", "fx", "currency", "currency_spot"}

def prewarm_common_intervals(market, code, cl_config):
    """
    用户首次切到一个标的时，后台预热其他常用周期，让用户切周期时能秒命中缓存。

    核心设计（重要！这个函数曾经是切标的卡顿的主犯）：
    1. **入口去重**（2026-04 新增）：30 秒内同 (market, code, cl_config) 只触发 1 次预热。
       4 面板齐发 first=true 时，只有第一个会真正启动预热线程，其余 3 个直接 return。
    2. **HTTP 市场 4 周期并行**：us/hk 用 ThreadPoolExecutor 并发跑，总时长 = max(各周期) ≈ 5s，
       而不是 sum(各周期) ≈ 20s。native 市场（a股 xtquant）保持串行避免 BSON 崩溃。
    3. **激进让位**：只要用户在最近 N 秒内有过 firstDataRequest=true（即切了标的或周期），
       本预热立即放弃。比 _is_still_latest 更敏感——后者只比 symbol，前者切周期也会让位。
    4. **全局信号量**：只允许 1 个 prewarm 任务在跑，新的 prewarm 来直接 skip
       （旧 prewarm 会通过 _is_still_latest 自然退出，新 prewarm 替补上来）。
    """
    # 2026-04 新增：如果当前 market 有 symbols.py 的批量预热在跑，旧版逐标的预热
    # 让位，避免双方争抢同一份 chart_calc_locks 与上游 HTTP 配额。批量预热已经覆盖
    # 该 cache_key（且写盘持久化），用户切回该标的时会从磁盘命中。
    if is_batch_prewarm_active(market):
        return

    target_key = f"{market}:{code}:{_stable_hash(cl_config)}"

    # 入口去重：30s 内同 target 已经触发过预热则直接 return。
    # 注意这里只去重"启动调用"，真正预热任务跑完后会留在 chart_data_cache 里供命中，
    # 与下面的 in-flight semaphore + _is_still_latest 是 3 道独立防线，互相不冲突。
    now = time.time()
    with _prewarm_dedupe_lock:
        last_ts = _prewarm_recent_targets.get(target_key, 0.0)
        if now - last_ts < _PREWARM_DEDUPE_TTL_SECONDS:
            # 4 面板齐发的场景下，第 2~4 个 first=true 走到这里直接退出，避免日志刷屏。
            # 只在调试时打 debug 日志，info 级别不打，否则正常使用每分钟会刷几十行。
            LogUtil.debug(
                f"[tv_history] Prewarm dedup-skip for {market}:{code} "
                f"(last triggered {now - last_ts:.1f}s ago)"
            )
            return
        _prewarm_recent_targets[target_key] = now
        # 懒清理过期项，避免长期运行时无限增长
        if len(_prewarm_recent_targets) > 500:
            cutoff = now - _PREWARM_DEDUPE_TTL_SECONDS
            stale = [k for k, t in _prewarm_recent_targets.items() if t < cutoff]
            for k in stale:
                _prewarm_recent_targets.pop(k, None)

    # 记录本次 prewarm 启动时的"用户活跃时间快照"，后续判断"用户是否又有了新动作"
    prewarm_start_user_ts = _get_last_user_request_time()

    with _prewarm_target_lock:
        _prewarm_latest_target["key"] = target_key

    def _is_still_latest():
        with _prewarm_target_lock:
            return _prewarm_latest_target["key"] == target_key

    def _user_acted_after_prewarm_start():
        """用户在 prewarm 启动之后，又触发过新的 firstDataRequest=true（切标的或切周期）。"""
        # 注意：必须 > 而不是 >=，因为本次触发 prewarm 的请求自己也会更新 _last_user_request_ts
        return _get_last_user_request_time() > prewarm_start_user_ts + 0.1

    def _compute_one_interval(interval: str, ex, tz_sh) -> bool:
        """计算单个周期的缓存。返回 True 表示已写入或已存在。"""
        if not _is_still_latest() or _user_acted_after_prewarm_start():
            return False

        cache_key = None
        try:
            freq = resolution_maps.get(interval, interval)
            cache_key = _build_cache_key(market, code, freq, cl_config)

            # 2026-04 修复：跳过已经确认无数据的周期，避免反复重算空 K 线。
            # 典型场景：ZK.US 1m 周期长桥拉不到数据，之前每次 prewarm 都会再尝试一遍，
            # 4 周期里有 1 个空 = 整个 prewarm 会卡住其它 3 个周期的并行槽位。
            if _is_negatively_cached(cache_key):
                LogUtil.debug(f"[prewarm] skip negatively-cached {market}:{code} interval={interval}")
                return False

            LogUtil.info(f"[prewarm] >>> {market}:{code} interval={interval}")

            # Skip if already in cache or being computed
            with cache_lock:
                if cache_key in chart_data_cache or cache_key in chart_data_cache_stats["prewarmed"]:
                    return True
                if len(chart_data_cache_stats["prewarmed"]) >= _MAX_PREWARMED_SIZE:
                    chart_data_cache_stats["prewarmed"].clear()
                    LogUtil.warning(f"[tv_history] prewarmed 集合已达上限 {_MAX_PREWARMED_SIZE}，已清空")
                chart_data_cache_stats["prewarmed"].add(cache_key)

            # 用 chart_calc_locks 上的 per-key 锁，避免和用户的 tv_history 计算撞车（同一标的同周期不会双重计算）
            with chart_calc_locks.get(cache_key):
                # 二次检查：拿锁过程中可能用户的 first=true 请求已经填了缓存
                with cache_lock:
                    if cache_key in chart_data_cache:
                        chart_data_cache_stats["prewarmed"].discard(cache_key)
                        return True

                if not _is_still_latest() or _user_acted_after_prewarm_start():
                    with cache_lock:
                        chart_data_cache_stats["prewarmed"].discard(cache_key)
                    return False

                kline_args = {
                    'end_date': datetime.datetime.now(tz_sh).strftime("%Y-%m-%d %H:%M:%S")
                }

                to_frequency = None
                if cl_config.get("enable_kchart_low_to_high") == "1":
                    frequency_low, to_frequency = kcharts_frequency_h_l_map(market, freq)
                    if frequency_low is not None and to_frequency is not None:
                        klines = ex.klines(code, frequency_low, **kline_args)
                        cd = web_batch_get_cl_datas(market, code, {frequency_low: klines}, cl_config)[0]
                    else:
                        klines = ex.klines(code, freq, **kline_args)
                        cd = web_batch_get_cl_datas(market, code, {freq: klines}, cl_config)[0]
                        to_frequency = None
                else:
                    klines = ex.klines(code, freq, **kline_args)
                    cd = web_batch_get_cl_datas(market, code, {freq: klines}, cl_config)[0]

                cl_chart_data = cl_data_to_tv_chart(cd, cl_config, to_frequency=to_frequency)
                if cl_chart_data is None:
                    # 2026-04 修复：cl_chart_data 为空说明这个 cache_key 没数据，打负缓存防止反复重算
                    _mark_negative_cache(cache_key)
                    with cache_lock:
                        chart_data_cache_stats["prewarmed"].discard(cache_key)
                    return False

                with cache_lock:
                    _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)
                    chart_data_cache_stats["prewarmed"].discard(cache_key)
                    LogUtil.debug(f"[tv_history] Pre-warmed cache for {market}:{code} interval {interval}")
                return True
        except Exception as e:
            if cache_key is not None:
                with cache_lock:
                    chart_data_cache_stats["prewarmed"].discard(cache_key)
            LogUtil.error(f"[tv_history] Pre-warm failed for {interval}: {e}")
            return False

    def _prewarm():
        # 非阻塞获取信号量；获取不到说明已有 prewarm 在跑，本次直接放弃
        if not _prewarm_semaphore.acquire(blocking=False):
            LogUtil.info(f"[tv_history] Prewarm skipped (another in flight) for {market}:{code}")
            return
        try:
            if not _is_still_latest():
                return
            ex = get_exchange(Market(market))
            tz_sh = pytz.timezone("Asia/Shanghai")

            use_parallel = market in _PREWARM_INTERVALS_PARALLEL_MARKETS
            if use_parallel:
                # HTTP 市场：4 周期并行，总耗时 ≈ max(各周期) ≈ 5s
                # 注意：worker 数 = len(COMMON_INTERVALS)，每个周期一个线程，互不阻塞
                # 局部 import 与 preload_symbols 风格一致，避免顶层依赖扩散
                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(
                    max_workers=len(COMMON_INTERVALS),
                    thread_name_prefix="PrewarmInterval",
                ) as executor:
                    futures = [
                        executor.submit(_compute_one_interval, interval, ex, tz_sh)
                        for interval in COMMON_INTERVALS
                    ]
                    for fut in futures:
                        try:
                            fut.result(timeout=60)
                        except Exception as e:
                            LogUtil.error(f"[tv_history] prewarm future error: {e}")
                        # 每个周期完成后检查一次让位
                        if _user_acted_after_prewarm_start() or not _is_still_latest():
                            # 取消剩余 futures（已开始的会跑完，未开始的会被跳过 _compute_one_interval 内部检查）
                            for f in futures:
                                f.cancel()
                            LogUtil.warning(
                                f"[tv_history] Prewarm aborted (user acted) for {market}:{code}"
                            )
                            break
            else:
                # native 市场（a 股等）：保持串行避免线程安全问题
                for interval in COMMON_INTERVALS:
                    if not _is_still_latest() or _user_acted_after_prewarm_start():
                        LogUtil.warning(
                            f"[tv_history] Prewarm aborted (user acted) for {market}:{code}"
                        )
                        return
                    _compute_one_interval(interval, ex, tz_sh)
                    time.sleep(0.1)  # 串行场景下小憩，避免对 native 接口持续压力
        except Exception as e:
            LogUtil.error(f"[tv_history] Pre-warm thread error: {e}")
        finally:
            _prewarm_semaphore.release()

    t = threading.Thread(target=_prewarm, daemon=True, name="IntervalPrewarmThread")
    t.start()
    LogUtil.info(f"[tv_history] Started pre-warm thread for {market}:{code}")


# _SafeLockRegistry / chart_calc_locks 已迁到 services.chart_compute（Tier 4 P3），
# 见模块顶部 re-export。
# H7（保守版）：__history_req_counter 的 per-key 节流锁。
# 仅 tv_history 内部使用，本地实例化 _SafeLockRegistry（从 service 导入的 class）。
# 注意：必须用 with _history_req_locks.get(key) 模式，详见 _SafeLockRegistry 注释。
_history_req_locks = _SafeLockRegistry()

# symbols 预加载逻辑（_resolve_preload_exchanges / _safe_all_stocks /
# _preload_single_exchange / preload_symbols / start_symbol_preload_thread 等）
# 已迁到 services.stock_list（Tier 4 P2），见模块顶部 re-export。

@tv_bp.route("/tv/config")
@login_required
def tv_config():
    frequencys = list(
        set(market_frequencys["a"])
        | set(market_frequencys["hk"])
        | set(market_frequencys["fx"])
        | set(market_frequencys["us"])
        | set(market_frequencys["futures"])
        | set(market_frequencys["currency"])
        | set(market_frequencys["currency_spot"])
    )
    supportedResolutions = [v for k, v in frequency_maps.items() if k in frequencys]
    return {
        "supports_search": True,
        "supports_group_request": False,
        "supported_resolutions": supportedResolutions,
        "supports_marks": True,
        "supports_timescale_marks": True,
        "supports_time": False,
        "exchanges": [
            {"value": "a", "name": "沪深", "desc": "沪深A股"},
            {"value": "hk", "name": "港股", "desc": "港股"},
            {"value": "fx", "name": "外汇", "desc": "外汇"},
            {"value": "us", "name": "美股", "desc": "美股"},
            {"value": "futures", "name": "国内期货", "desc": "国内期货"},
            {"value": "ny_futures", "name": "纽约期货", "desc": "纽约期货"},
            {
                "value": "currency",
                "name": "数字货币(Futures)",
                "desc": "数字货币（合约）",
            },
            {
                "value": "currency_spot",
                "name": "数字货币(Spot)",
                "desc": "数字货币（现货）",
            },
        ],
    }


@tv_bp.route("/tv/symbol_info")
@login_required
def tv_symbol_info():
    group = request.args.get("group")
    try:
        all_symbols = get_cached_processed_stocks(group)
    except Exception:
        ex = get_exchange(Market(group))
        all_symbols = _safe_all_stocks(ex, group)

    info = {
        "symbol": [s["code"] for s in all_symbols],
        "description": [s["name"] for s in all_symbols],
        "exchange-listed": group,
        "exchange-traded": group,
    }
    return info


@tv_bp.route("/tv/symbols")
@login_required
def tv_symbols():
    raw_symbol: str = request.args.get("symbol", "")
    market, code = _parse_tv_symbol(raw_symbol)
    if market is None or code is None:
        return {"s": "error", "errmsg": f"invalid symbol: {raw_symbol}"}

    try:
        ex = get_exchange(Market(market))
    except Exception as e:
        LogUtil.error(f"[tv_symbols] get_exchange failed symbol={raw_symbol} err={e}")
        return {"s": "error", "errmsg": "invalid market"}

    stocks = ex.stock_info(code)
    if stocks is None:
        return {"s": "error", "errmsg": f"unknown symbol: {raw_symbol}"}

    if "code" not in stocks:
        stocks["code"] = code
    if "name" not in stocks:
        stocks["name"] = code

    sector = ""
    industry = ""
    if market == "a":
        try:
            gnbk = ex.stock_owner_plate(code)
            sector = " / ".join([_g["name"] for _g in gnbk["GN"]])
            industry = " / ".join([_h["name"] for _h in gnbk["HY"]])
        except Exception:
            pass

    # --- 修复 minmov 错误的关键部分 ---
    # 获取 precision，默认值设为 100 (2位小数)
    precision = stocks.get("precision")
    if precision is None:
        precision = 100
    else:
        try:
            precision = int(precision)
            if precision <= 0:
                precision = 100
        except:
            precision = 100
    # --------------------------------

    info = {
        "name": stocks["code"],
        "ticker": f"{market}:{stocks['code']}",
        "full_name": f"{market}:{stocks['code']}",
        "description": stocks["name"],
        "exchange": market,
        "type": market_types.get(market, "stock"),
        "session": market_session.get(market, "24x7"),
        "timezone": market_timezone.get(market, "Asia/Shanghai"),

        # 显式设置 minmov 和 pricescale
        "minmov": 1,
        "pricescale": precision,

        "visible_plots_set": "ohlcv",
        "supported_resolutions": [
            v for k, v in frequency_maps.items() if k in market_frequencys.get(market, [])
        ],
        "intraday_multipliers": ["1", "5", "15", "30", "60"],
        "has_intraday": True,
        "has_seconds": True if market in ["futures", "ny_futures"] else False,
        "has_daily": True,
        "has_weekly_and_monthly": True,
        "sector": sector,
        "industry": industry,
    }
    return info


# _process_stock_list / _trigger_async_refresh / get_cached_processed_stocks
# 已迁到 services.stock_list（Tier 4 P2），见模块顶部 re-export。


@tv_bp.route("/tv/search")
@login_required
def tv_search():
    # 关键修复：搜索结果必须严格按"当前页面市场"过滤，避免 A 股搜出美股之类的串市场问题。
    # 触发原因：TradingView 的 Symbol Search 组件默认会传 exchange="" / "All" / 上次选中的
    # 交易所，并不一定等于当前 chart 的 market；若后端不校验直接命中错误缓存或 KeyError 退化，
    # 会让前端 datafeed 回退到内置 symbol 列表（含历史浏览过的其它市场标的）。
    query = (request.args.get("query") or "").strip()
    type_ = request.args.get("type")
    exchange = (request.args.get("exchange") or "").strip().lower()
    try:
        limit = int(request.args.get("limit", "10"))
    except (TypeError, ValueError):
        limit = 10
    if limit <= 0:
        limit = 10

    # exchange 必须是已知市场之一，否则直接拒绝；不要静默回退到任何"看似合理"的市场。
    if exchange not in market_types or exchange not in market_frequencys:
        LogUtil.warning(
            f"[tv_search] reject invalid exchange={exchange!r} query={query!r}"
        )
        return {"error": f"invalid exchange: {exchange!r}"}, 400

    # 空 query 直接返回空列表，避免对几万条 symbol 全量扫描后被 limit 截断成"看起来随机"的结果。
    if not query:
        return []

    # 用 allow_sync_fallback=True: 启动后 60s 预加载空窗期或某个市场首次访问时, 同步加载一次,
    # 避免直接 500。最差情况是返回 [], 搜索框显示"无结果"——优于"接口异常"的体感。
    try:
        processed_stocks = get_cached_processed_stocks(exchange, allow_sync_fallback=True)
    except Exception as e:
        LogUtil.error(f"[tv_search] get stocks failed exchange={exchange}: {e}")
        # 兜底也失败时仍降级为空列表而不是 500, 避免前端 datafeed 抛异常显示"加载错误"。
        processed_stocks = []

    if not processed_stocks:
        # 没有可搜的 symbol 直接返回空, 后续逻辑还有 market_session/market_timezone 取值,
        # 提前返回也能省一次循环。
        return []

    query_lower = query.lower()
    is_currency = exchange in ["currency", "currency_spot"]

    # 优先级：完全相等 > code/拼音前缀 > 任意子串包含。
    # 这样搜 "600" 不会被一堆名字含 600 的票淹没；搜 "中国" 也不会被代码含相同字符的票打乱顺序。
    exact_hits = []
    prefix_hits = []
    contains_hits = []
    for stock in processed_stocks:
        code_l = stock['code_lower']
        name_l = stock['name_lower']
        pinyin_l = stock['pinyin_initials']

        if is_currency:
            if query_lower == code_l:
                exact_hits.append(stock)
            elif code_l.startswith(query_lower):
                prefix_hits.append(stock)
            elif query_lower in code_l:
                contains_hits.append(stock)
        else:
            if query_lower == code_l or query_lower == name_l:
                exact_hits.append(stock)
            elif (code_l.startswith(query_lower)
                  or pinyin_l.startswith(query_lower)
                  or name_l.startswith(query_lower)):
                prefix_hits.append(stock)
            elif (query_lower in code_l
                  or query_lower in name_l
                  or query_lower in pinyin_l):
                contains_hits.append(stock)

        # 早停：精确+前缀已经够用就不再扫剩下的，节省 CPU。
        if len(exact_hits) + len(prefix_hits) >= limit:
            break

    res_stocks = (exact_hits + prefix_hits + contains_hits)[:limit]

    # 用 .get 防御 market_frequencys 中 exchange 因懒加载失败缺键的情况（前面已校验在表内，
    # 但懒加载 build 失败时值会是 []，这里再兜一层就不会抛）。
    supported_resolutions = [
        v for k, v in frequency_maps.items() if k in market_frequencys.get(exchange, [])
    ]
    session_value = market_session.get(exchange, "24x7")
    timezone_value = market_timezone.get(exchange, "Asia/Shanghai")

    infos = []
    for stock in res_stocks:
        infos.append(
            {
                "symbol": stock["code"],
                "name": stock["code"],
                "full_name": f"{exchange}:{stock['code']}",
                "description": stock["name"],
                "exchange": exchange,
                "ticker": f"{exchange}:{stock['code']}",
                "type": type_,
                "session": session_value,
                "timezone": timezone_value,
                "supported_resolutions": supported_resolutions,
            }
        )
    return infos

@tv_bp.route("/tv/history")
@login_required
def tv_history():
    _req_start_ts = time.time()
    try:
        args = request.args.to_dict()
        symbol = request.args.get("symbol", "")
        resolution = _normalize_resolution(request.args.get("resolution"))
        firstDataRequest = request.args.get("firstDataRequest", "false")
        _from = _normalize_unix_ts(request.args.get("from", "0"))
        _to = _normalize_unix_ts(request.args.get("to", "0"))
        LogUtil.info(
            f"[tv_history] >>> {symbol} {resolution} first={firstDataRequest} "
            f"from={_from} to={_to}"
        )

        if not symbol or not resolution:
            return {"s": "no_data"}
        if _from < 0 and _to < 0:
            return {"s": "no_data"}

        market, code = _parse_tv_symbol(symbol)
        if market is None or code is None:
            LogUtil.warning(f"[tv_history] invalid symbol: {symbol}")
            return {"s": "no_data"}

        frequency = resolution_maps.get(resolution)
        if frequency is None:
            LogUtil.warning(f"[tv_history] Unsupported resolution: {resolution}")
            return {"s": "no_data"}

        # 标记用户活跃度，供批量预热（symbols.py）让位 / 优先插队使用。
        # 关键：仅 firstDataRequest=true（用户主动切标的/切周期）才标记活跃；
        # firstDataRequest=false 是 TradingView 后台 polling（每 ~3 秒 1 次），
        # 如果也算"用户活跃"，会把批量预热永久卡死。
        if firstDataRequest == "true":
            _mark_user_request(market, code)
            # B5: 记录最后访问状态，供下次启动预热 RAM chart_data_cache。
            # 失败吞异常——这是观测/优化用，不能影响 history 主流程。
            try:
                from cl_app.services.last_chart_state import record_user_request
                record_user_request(market, code, frequency)
            except Exception:
                pass

        tz_sh = pytz.timezone("Asia/Shanghai")
        log_args = dict(args)
        for key in ("from", "to"):
            if key in log_args:
                ts = _normalize_unix_ts(log_args.get(key))
                if ts > 0:
                    try:
                        log_args[key] = datetime.datetime.fromtimestamp(ts, tz_sh).strftime(
                            "%Y-%m-%d %H:%M:%S"
                        )
                    except Exception:
                        pass
        LogUtil.debug(f"tv_history request args: {log_args}")

        req_tag = f"{symbol}|{resolution}|{firstDataRequest}|{_from}->{_to}"
        _symbol_res_old_k_time_key = f"{symbol}_{resolution}"
        now_time = time.time()
        if firstDataRequest == "false":
            # H7（保守版）：per-key 锁替代全局 req_lock，不同 symbol/resolution 并发不互斥。
            # 必须先 get 出 RLock 再 with，确保整个临界区内强引用持续（WeakValueDictionary 语义）。
            _req_lock = _history_req_locks.get(_symbol_res_old_k_time_key)
            with _req_lock:
                if _symbol_res_old_k_time_key not in __history_req_counter.keys():
                    __history_req_counter[_symbol_res_old_k_time_key] = {
                        "counter": 0,
                        "tm": now_time,
                    }
                else:
                    if __history_req_counter[_symbol_res_old_k_time_key]["counter"] >= 30:
                        __history_req_counter[_symbol_res_old_k_time_key] = {
                            "counter": 0,
                            "tm": now_time,
                        }
                        LogUtil.warning(
                            f"[tv_history] Too many requests in short time: {_symbol_res_old_k_time_key}, counter reset"
                        )
                    elif (
                        now_time - __history_req_counter[_symbol_res_old_k_time_key]["tm"]
                        <= 5
                    ):
                        __history_req_counter[_symbol_res_old_k_time_key]["counter"] += 1
                        __history_req_counter[_symbol_res_old_k_time_key]["tm"] = now_time
                    else:
                        __history_req_counter[_symbol_res_old_k_time_key] = {
                            "counter": 0,
                            "tm": now_time,
                        }

        cl_config = query_cl_chart_config(market, code)
        if not isinstance(cl_config, dict):
            cl_config = {}
        # 使用稳定 hash 构造 cache_key（不受 PYTHONHASHSEED 影响，进程重启后仍一致）
        cache_key = _build_cache_key(market, code, frequency, cl_config)

        cl_chart_data = None
        is_cache_hit = False
        cache_miss_reason = "cache_empty"
        is_range_request = (
            firstDataRequest == "false"
            and _from > 0
            and _to > 0
            and _to >= _from
        )

        # 内联函数：根据当前缓存项判断是否命中。提取出来供 double-check locking 复用。
        def _evaluate_cache(_cache_entry):
            if _cache_entry is None:
                return False, None, "cache_empty"
            cached_data = _cache_entry.get("data", {})
            cache_min_time = _cache_entry.get("min_time")
            cache_max_time = _cache_entry.get("max_time")
            if not is_range_request:
                if _cache_entry.get("is_full_snapshot", False):
                    return True, cached_data, None
                return False, None, "cache_partial_snapshot"
            if cache_min_time is None or cache_max_time is None:
                return False, None, "cache_no_coverage"
            if _from < cache_min_time:
                return False, None, "cache_head_gap"
            if _to > cache_max_time:
                if _cache_entry_recently_validated(_cache_entry):
                    return True, cached_data, None
                return False, None, "cache_tail_gap"
            return True, cached_data, None

        # 注意：必须先 get 出 RLock 对象再 with，确保整个临界区内引用持续存在
        # （_SafeLockRegistry 用 WeakValueDictionary 存储锁，无强引用会被 GC）
        _calc_lock = chart_calc_locks.get(cache_key)
        with _calc_lock:
            with cache_lock:
                cache_entry = _get_chart_cache_entry(cache_key)
                is_cache_hit, cl_chart_data, miss_reason = _evaluate_cache(cache_entry)
                if not is_cache_hit:
                    cache_miss_reason = miss_reason

            if not is_cache_hit:
                # 早返: 请求范围完全早于(或刚好接到)缓存最早时间 -> 必无数据.
                # TradingView UDF 翻页时下一次请求的 _to 正好等于上次的 cache_min_time,
                # 用 <= 才能覆盖这种边界. 切片逻辑 bisect_left 是左闭右开, _to == min_time
                # 时切片 [0:0] 仍为空, 语义一致.
                # 不早返会触发 ex.klines + web_batch_get_cl_datas 共耗 300-500ms.
                if (
                    is_range_request
                    and _to > 0
                    and cache_entry is not None
                    and cache_entry.get("min_time") is not None
                    and _to <= cache_entry["min_time"]
                ):
                    with cache_lock:
                        _mark_chart_cache_validated(cache_key)
                    return {"s": "no_data"}

                LogUtil.debug(f"[tv_history] Cache miss ({cache_miss_reason}) req={req_tag}")
                ex = get_exchange(Market(market))
                frequency_low, kchart_to_frequency = kcharts_frequency_h_l_map(market, frequency)
                kline_args = {}
                if is_range_request:
                    kline_args["start_date"] = datetime.datetime.fromtimestamp(
                        _from, tz=tz_sh
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    kline_args["end_date"] = datetime.datetime.fromtimestamp(
                        _to, tz=tz_sh
                    ).strftime("%Y-%m-%d %H:%M:%S")
                    LogUtil.debug(
                        f"[tv_history] incremental request {code} range: {kline_args['start_date']} -> {kline_args['end_date']}"
                    )
                else:
                    kline_args["end_date"] = datetime.datetime.now(tz_sh).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )

                if (
                    cl_config.get("enable_kchart_low_to_high") == "1"
                    and kchart_to_frequency is not None
                    and frequency_low is not None
                ):
                    klines = ex.klines(code, frequency_low, **kline_args)
                    if klines is None or len(klines) == 0:
                        with cache_lock:
                            _mark_chart_cache_validated(cache_key)
                        return {"s": "no_data"}
                    cd = web_batch_get_cl_datas(
                        market, code, {frequency_low: klines}, cl_config
                    )[0]
                else:
                    kchart_to_frequency = None
                    klines = ex.klines(code, frequency, **kline_args)
                    if klines is None or len(klines) == 0:
                        with cache_lock:
                            _mark_chart_cache_validated(cache_key)
                        return {"s": "no_data"}

                    # ★ 方案 A:范围请求(向左滚动)走分层缓存——
                    # 把新 K 线合并进 L1,基于完整 K 线集全量重算,整体替换 L2。
                    # 不再走 web_batch_get_cl_datas + _merge_chart_data,
                    # 那条老路径会用窄范围 K 线独立计算 XD 再"按起点合并",
                    # 导致用户向左滚动时看到 XD 跳变(详见 docs/superpowers/plans/
                    # 2026-05-10-kline-cl-layered-cache.md)。
                    if (
                        is_range_request
                        and cache_miss_reason in ("cache_head_gap", "cache_partial_snapshot")
                    ):
                        cl_chart_data = prepend_klines_and_replace_cache(
                            market, code, frequency, cl_config,
                            new_klines=klines, cache_key=cache_key,
                            to_frequency=None,
                        )
                        if cl_chart_data is None:
                            with cache_lock:
                                _mark_chart_cache_validated(cache_key)
                            return {"s": "no_data"}
                        # prepend 内部已经写回 cache,跳过下面的 _merge_chart_data 路径
                        cd = None
                    else:
                        cd = web_batch_get_cl_datas(
                            market, code, {frequency: klines}, cl_config
                        )[0]

                if _to > 0 and len(klines) > 0 and _to < fun.datetime_to_int(klines.iloc[0]["date"]):
                    with cache_lock:
                        _mark_chart_cache_validated(cache_key)
                    return {"s": "no_data"}

                if cd is not None:
                    cl_chart_data = cl_data_to_tv_chart(
                        cd, cl_config, to_frequency=kchart_to_frequency
                    )
                    if cl_chart_data is None:
                        with cache_lock:
                            _mark_chart_cache_validated(cache_key)
                        return {"s": "no_data"}

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
                        # 确保有足够的 bar 数量用于 MACD 计算（至少需要 slow+signal 根K线）
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
                        LogUtil.error(f"[tv_history] Scaled MACD calc failed: {e}")

                if cd is not None:
                    with cache_lock:
                        existing_entry = _get_chart_cache_entry(cache_key)
                        if existing_entry is not None:
                            cl_chart_data = _merge_chart_data(
                                existing_entry.get("data", {}), cl_chart_data
                            )
                        _set_chart_cache_entry(
                            cache_key,
                            cl_chart_data,
                            is_full_snapshot=(not is_range_request) or (existing_entry or {}).get("is_full_snapshot", False),
                        )

                # prepend 路径在补完 higher_macd 后,需要把 cl_chart_data 重新写回 cache,
                # 否则下次相同范围请求 hit 时拿到的还是无 higher_macd 的版本。
                if cd is None and cl_chart_data is not None:
                    with cache_lock:
                        _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)

                # firstDataRequest 成功后，后台预热其他常用周期的缓存
                if firstDataRequest == "true":
                    prewarm_common_intervals(market, code, cl_config)

        if cl_chart_data is None:
            return {"s": "no_data"}

        bar_times = cl_chart_data.get("t", [])
        if not is_cache_hit:
            _fxs_cnt = len(cl_chart_data.get("fxs", []))
            _bis_cnt = len(cl_chart_data.get("bis", []))
            _xds_cnt = len(cl_chart_data.get("xds", []))
            LogUtil.debug(
                f"[tv_history] Calc Finish & Cached req={req_tag}, bars={len(bar_times)}, fxs={_fxs_cnt}, bis={_bis_cnt}, xds={_xds_cnt}"
            )
        else:
            LogUtil.debug(f"[tv_history] Cache Hit req={req_tag}, bars={len(bar_times)}")

        if firstDataRequest == "false" and len(bar_times) > 0:
            try:
                from_ts = _from
                to_ts = _to
                start_idx = bisect.bisect_left(bar_times, from_ts)
                # bisect_left 使 to_ts 为排他上界（与 TradingView UDF 协议一致），
                # 避免 backward request 返回与请求 to 时间戳完全相同的 K 线（会导致重复 bar + 多余请求）
                end_idx = (
                    bisect.bisect_left(bar_times, to_ts)
                    if to_ts > 0
                    else len(bar_times)
                )

                def filter_shapes(shapes):
                    """
                    多点形态（笔/线段/中枢）：与 [from_ts, to_ts) 有重叠即保留——
                    线段起点早于 to_ts 且终点晚于 from_ts。避免「跨可视边界」
                    的线段被丢弃后，前端合并时永久丢失。
                    单点形态（分型/背驰/买卖点）：点位需落在窗口内。
                    """
                    res = []
                    for shape in shapes:
                        if isinstance(shape, dict) and "points" in shape:
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

                cl_chart_data = {
                    "t": cl_chart_data.get("t", [])[start_idx:end_idx],
                    "c": cl_chart_data.get("c", [])[start_idx:end_idx],
                    "o": cl_chart_data.get("o", [])[start_idx:end_idx],
                    "h": cl_chart_data.get("h", [])[start_idx:end_idx],
                    "l": cl_chart_data.get("l", [])[start_idx:end_idx],
                    "v": cl_chart_data.get("v", [])[start_idx:end_idx],
                    "macd_dif": cl_chart_data.get("macd_dif", [])[start_idx:end_idx]
                    if cl_chart_data.get("macd_dif")
                    else [],
                    "macd_dea": cl_chart_data.get("macd_dea", [])[start_idx:end_idx]
                    if cl_chart_data.get("macd_dea")
                    else [],
                    "macd_hist": cl_chart_data.get("macd_hist", [])[start_idx:end_idx]
                    if cl_chart_data.get("macd_hist")
                    else [],
                    "macd_area": cl_chart_data.get("macd_area", [])[start_idx:end_idx]
                    if cl_chart_data.get("macd_area")
                    else [],
                    "higher_macd_dif": cl_chart_data.get("higher_macd_dif", [])[start_idx:end_idx]
                    if cl_chart_data.get("higher_macd_dif")
                    else [],
                    "higher_macd_dea": cl_chart_data.get("higher_macd_dea", [])[start_idx:end_idx]
                    if cl_chart_data.get("higher_macd_dea")
                    else [],
                    "higher_macd_hist": cl_chart_data.get("higher_macd_hist", [])[start_idx:end_idx]
                    if cl_chart_data.get("higher_macd_hist")
                    else [],
                    "fxs": filter_shapes(cl_chart_data.get("fxs", [])),
                    "bis": filter_shapes(cl_chart_data.get("bis", [])),
                    "xds": filter_shapes(cl_chart_data.get("xds", [])),
                    "bi_zss": filter_shapes(cl_chart_data.get("bi_zss", [])),
                    "xd_zss": filter_shapes(cl_chart_data.get("xd_zss", [])),
                    "bcs": filter_shapes(cl_chart_data.get("bcs", [])),
                    "mmds": filter_shapes(cl_chart_data.get("mmds", [])),
                }
            except Exception as e:
                LogUtil.error(f"[tv_history] Slice data failed: {e}")

        # 切片后无数据，返回 no_data 阻止 TradingView 继续向前请求
        if len(cl_chart_data.get("t", [])) == 0:
            return {"s": "no_data"}

        # 裁剪超出请求 _to 的「未来」K 线
        # 交易所可能返回尚未完成的下一根 K 线（时间戳 > 当前时间），
        # TradingView 缓存后会导致 DataPulse 更新时 time order violation
        _resp_times = cl_chart_data.get("t", [])
        _resp_end = len(_resp_times)
        if _to > 0 and _resp_times and _resp_times[-1] > _to:
            _resp_end = bisect.bisect_right(_resp_times, _to)
            if _resp_end < len(_resp_times):
                LogUtil.warning(
                    f"[tv_history] Trimmed {len(_resp_times) - _resp_end} future bar(s) beyond to={_to}"
                )

        def _trim(arr):
            """对列表做 [:_resp_end] 切片，不修改缓存原对象"""
            if not arr or _resp_end >= len(arr):
                return arr
            return arr[:_resp_end]

        # [DataVerify] Log response shape counts for frontend correlation
        _resp_t = _trim(cl_chart_data.get("t", []))
        LogUtil.debug(
            f"[DataVerify][Backend] symbol={symbol} resolution={resolution} "
            f"update={firstDataRequest != 'true'} bars={len(_resp_t)} "
            f"fxs={len(cl_chart_data.get('fxs', []))} "
            f"bis={len(cl_chart_data.get('bis', []))} "
            f"xds={len(cl_chart_data.get('xds', []))} "
            f"bi_zss={len(cl_chart_data.get('bi_zss', []))} "
            f"bcs={len(cl_chart_data.get('bcs', []))} "
            f"mmds={len(cl_chart_data.get('mmds', []))}"
        )

        _elapsed_ms = (time.time() - _req_start_ts) * 1000
        LogUtil.info(
            f"[tv_history] {symbol} {resolution} bars={len(_resp_t)} "
            f"first={firstDataRequest} elapsed={_elapsed_ms:.0f}ms"
        )

        return {
            "s": "ok",
            "t": _trim(cl_chart_data.get("t", [])),
            "c": _trim(cl_chart_data.get("c", [])),
            "o": _trim(cl_chart_data.get("o", [])),
            "h": _trim(cl_chart_data.get("h", [])),
            "l": _trim(cl_chart_data.get("l", [])),
            "v": _trim(cl_chart_data.get("v", [])),
            "macd_dif": _trim(cl_chart_data.get("macd_dif", [])),
            "macd_dea": _trim(cl_chart_data.get("macd_dea", [])),
            "macd_hist": _trim(cl_chart_data.get("macd_hist", [])),
            "macd_area": _trim(cl_chart_data.get("macd_area", [])),
            "higher_macd_dif": _trim(cl_chart_data.get("higher_macd_dif", [])),
            "higher_macd_dea": _trim(cl_chart_data.get("higher_macd_dea", [])),
            "higher_macd_hist": _trim(cl_chart_data.get("higher_macd_hist", [])),
            "fxs": cl_chart_data.get("fxs", []),
            "bis": cl_chart_data.get("bis", []),
            "xds": cl_chart_data.get("xds", []),
            "bi_zss": cl_chart_data.get("bi_zss", []),
            "xd_zss": cl_chart_data.get("xd_zss", []),
            "bcs": cl_chart_data.get("bcs", []),
            "mmds": cl_chart_data.get("mmds", []),
            "update": False if firstDataRequest == "true" else True,
        }
    except Exception as e:
        req_qs = request.query_string.decode("utf-8", errors="ignore")
        LogUtil.error(f"[tv_history] unhandled error query={req_qs} err={e}", exc_info=True)
        return {"s": "no_data"}


@tv_bp.route("/tv/timescale_marks")
@login_required
def tv_timescale_marks():
    symbol = request.args.get("symbol", "")
    _from = _normalize_unix_ts(request.args.get("from"))
    _to = _normalize_unix_ts(request.args.get("to"))
    resolution = _normalize_resolution(request.args.get("resolution"))
    market, code = _parse_tv_symbol(symbol)
    if market is None or code is None:
        return []
    freq = resolution_maps.get(resolution)
    if freq is None:
        return []

    order_type_maps = {
        "buy": "买入",
        "sell": "卖出",
        "open_long": "买入开多",
        "open_short": "买入开空",
        "close_long": "卖出平多",
        "close_short": "买入平空",
    }
    marks = []

    orders = db.order_query_by_code(market, code)
    for i in range(len(orders)):
        o = orders[i]
        _dt_int = fun.datetime_to_int(o["datetime"])
        if _from <= _dt_int <= _to:
            m = {
                "id": i,
                "time": _dt_int,
                "color": (
                    "red"
                    if o["type"] in ["buy", "open_long", "close_short"]
                    else "green"
                ),
                "label": (
                    "B" if o["type"] in ["buy", "open_long", "close_short"] else "S"
                ),
                "tooltip": [
                    f"{order_type_maps[o['type']]}[{o['price']}/{o['amount']}]",
                    f"{'' if 'info' not in o else o['info']}",
                ],
                "shape": (
                    "earningUp"
                    if o["type"] in ["buy", "open_long", "close_short"]
                    else "earningDown"
                ),
            }
            marks.append(m)

    other_marks = db.marks_query(market, code)
    for i in range(len(other_marks)):
        _m = other_marks[i]
        if _m.frequency == "" or _m.frequency == freq:
            if _from <= _m.mark_time <= _to:
                marks.append(
                    {
                        "id": f"m-{i}",
                        "time": int(_m.mark_time),
                        "color": _m.mark_color,
                        "label": _m.mark_label,
                        "tooltip": _m.mark_tooltip,
                        "shape": _m.mark_shape,
                    }
                )

    return marks


@tv_bp.route("/tv/marks")
@login_required
def tv_marks():
    symbol = request.args.get("symbol", "")
    _from = _normalize_unix_ts(request.args.get("from"))
    _to = _normalize_unix_ts(request.args.get("to"))
    resolution = _normalize_resolution(request.args.get("resolution"))
    market, code = _parse_tv_symbol(symbol)
    if market is None or code is None:
        return []
    freq = resolution_maps.get(resolution)
    if freq is None:
        return []

    marks = []
    price_marks = db.marks_query_by_price(market, code, start_date=_from)
    for i in range(len(price_marks)):
        _m = price_marks[i]
        if _m.frequency == "" or _m.frequency == freq:
            if _from <= _m.mark_time <= _to:
                marks.append(
                    {
                        "id": f"m-{i}",
                        "time": int(_m.mark_time),
                        "color": _m.mark_color,
                        "text": _m.mark_text,
                        "label": _m.mark_label,
                        "labelFontColor": _m.mark_label_font_color,
                        "minSize": _m.mark_min_size,
                    }
                )

    return marks


# TradingView charting_library 是第三方 JS 库，无法注入 CSRF token，
# 这些路由通过 @login_required 保证只有登录用户可访问，CSRF 风险可控。
@tv_bp.route("/tv/del_marks", methods=["POST"])
@csrf.exempt
@login_required
def tv_del_marks():
    symbol = request.form["symbol"]
    market, code = _parse_tv_symbol(symbol)
    if market is None or code is None:
        return {"status": "ok"}

    db.marks_del_all_by_code(market, code)

    return {"status": "ok"}


@tv_bp.route("/tv/time")
@login_required
def tv_time():
    return fun.datetime_to_int(datetime.datetime.now())


@tv_bp.route("/tv/<version>/charts", methods=["GET", "POST", "DELETE"])
@csrf.exempt
@login_required
def tv_charts(version):
    client_id = str(request.args.get("client"))
    user_id = str(request.args.get("user"))

    if request.method == "GET":
        chart_id = request.args.get("chart")
        if chart_id is None:
            chart_list = db.tv_chart_list("chart", client_id, user_id)
            return {
                "status": "ok",
                "data": [
                    {
                        "timestamp": c.timestamp,
                        "symbol": c.symbol,
                        "resolution": c.resolution,
                        "id": c.id,
                        "name": c.name,
                    }
                    for c in chart_list
                ],
            }
        else:
            chart = db.tv_chart_get("chart", chart_id, client_id, user_id)
            return {
                "status": "ok",
                "data": {
                    "content": chart.content,
                    "timestamp": chart.timestamp,
                    "name": chart.name,
                    "id": chart.id,
                },
            }
    elif request.method == "DELETE":
        chart_id = request.args.get("chart")
        db.tv_chart_del("chart", chart_id, client_id, user_id)
        return {
            "status": "ok",
        }
    else:
        name = request.form["name"]
        content = request.form["content"]
        symbol = request.form["symbol"]
        resolution = request.form["resolution"]
        chart_id = request.args.get("chart")

        if chart_id is None:
            id = db.tv_chart_save(
                "chart", client_id, user_id, name, content, symbol, resolution
            )
            return {
                "status": "ok",
                "id": id,
            }
        else:
            db.tv_chart_update(
                "chart",
                chart_id,
                client_id,
                user_id,
                name,
                content,
                symbol,
                resolution,
            )
            return {"status": "ok"}


@tv_bp.route("/tv/<version>/study_templates", methods=["GET", "POST", "DELETE"])
@csrf.exempt
@login_required
def tv_study_templates(version):
    client_id = str(request.args.get("client"))
    user_id = str(request.args.get("user"))

    if request.method == "GET":
        template = request.args.get("template")
        if template is None:
            template_list = db.tv_chart_list("template", client_id, user_id)
            return {
                "status": "ok",
                "data": [{"name": t.name} for t in template_list],
            }
        else:
            template = db.tv_chart_get_by_name(
                "template", template, client_id, user_id
            )
            return {
                "status": "ok",
                "data": {"name": template.name, "content": template.content},
            }
    elif request.method == "DELETE":
        name = request.args.get("template")
        db.tv_chart_del_by_name("template", name, client_id, user_id)
        return {
            "status": "ok",
        }
    else:
        name = request.form["name"]
        content = request.form["content"]
        db.tv_chart_save("template", client_id, user_id, name, content, "", "")
        return {"status": "ok"}


@tv_bp.route("/tv/<version>/drawings", methods=["GET", "POST"])
@csrf.exempt
@login_required
def tv_drawings(version):
    client_id = str(request.args.get("client"))
    user_id = str(request.args.get("user"))
    chart_id = request.args.get("chart", "default")
    layout_id = request.args.get("layout", "default")
    symbol = request.args.get("symbol", "")
    resolution = request.args.get("resolution", "")

    drawing_name = _drawing_storage_name(chart_id, layout_id, symbol, resolution)
    legacy_drawing_name = _legacy_drawing_storage_name(symbol, resolution)

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        content = payload.get("state", {})
        db.tv_chart_save(
            "drawing",
            client_id,
            user_id,
            drawing_name,
            json.dumps(content),
            symbol,
            resolution,
        )
        return {"status": "ok"}

    if request.method == "GET":
        drawing = db.tv_chart_get_by_name("drawing", drawing_name, client_id, user_id)
        if drawing is None:
            drawing = db.tv_chart_get_by_name(
                "drawing", legacy_drawing_name, client_id, user_id
            )
            if drawing is not None:
                db.tv_chart_save(
                    "drawing",
                    client_id,
                    user_id,
                    drawing_name,
                    drawing.content,
                    symbol,
                    resolution,
                )

        if drawing:
            try:
                data = json.loads(drawing.content)
            except Exception:
                data = {}
            return {
                "status": "ok",
                "data": data,
            }
        return {
            "status": "ok",
            "data": {},
        }