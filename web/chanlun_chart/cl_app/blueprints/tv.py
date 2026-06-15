"""
TradingView 相关接口蓝图。

提供 /tv/config、/tv/symbols、/tv/search、/tv/history 等标准 UDF 接口，
以及图表/模板/画线存取和自定义 Marks 支持。
"""
import pytz
import json
import datetime
import time
import threading
from threading import Semaphore
from typing import Dict
from flask import Blueprint, request
from flask_login import login_required

from chanlun import fun
from chanlun.base import Market
from chanlun.cl_utils import (
    cl_data_to_tv_chart,
    kcharts_frequency_h_l_map,
    query_cl_chart_config,
    web_batch_get_cl_datas,
)
from chanlun.persistence.db import db
from chanlun.exchange import get_exchange
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
from ..services.last_chart_state import record_user_request

tv_bp = Blueprint("tv", __name__)

# 图表缓存、symbols 预加载、跨周期 MACD 等基础设施均已迁至 services 子包，
# 以下 re-export 保持本模块内部引用不变。
from ..services.chart_cache import (  # noqa: E402
    _CACHE_REVALIDATION_INTERVAL,
    _build_cache_key,
    _build_chart_cache_entry,
    _cache_entry_recently_validated,
    _chart_cache_disk_executor,
    _full_snapshot_is_stale,
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
    evaluate_cache_for_tv_history,
)


# ---------------------------------------------------------------------------
# 批量预热活动注册表（service 层，与 symbols.py PrewarmManager 协作）
# ---------------------------------------------------------------------------
from ..services.prewarm_status import (  # noqa: E402
    is_batch_prewarm_active,
    mark_batch_prewarm_active,
)

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


# ---------------------------------------------------------------------------
# 用户活跃度跟踪（service 层，供 symbols.py 的批量预热让位用）
# ---------------------------------------------------------------------------
from ..services.user_activity import (  # noqa: E402
    _get_last_user_request_time,
    _get_user_recent_codes,
    _mark_user_request,
)
# stock_list 服务：symbols 预加载、缓存、读取
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
# chart_compute 服务：MACD 倍率 + 锁注册表 + chart 合并 + 主计算路径
from ..services.chart_compute import (  # noqa: E402
    HIGHER_MACD_RATIO,
    MARKET_30M_TO_D_RATIO,
    MARKET_D_TO_W_RATIO,
    _SafeLockRegistry,
    _merge_chart_data,
    _merge_shape_lists,
    _shape_time,
    apply_higher_macd_to_chart_data,
    apply_higher_zs_to_chart_data,
    chart_calc_locks,
    compute_and_cache_chart_data,
    fetch_klines_and_compute_cl_data,
    should_lazy_apply_higher_macd,
    slice_chart_data_to_window,
    trim_future_bars,
)
from ..services.kline_recompute import (  # noqa: E402
    prepend_klines_and_replace_cache,
)


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


def _drawing_storage_name(chart_id: str, layout_id: str, symbol: str, resolution: str):
    return f"drawings_{layout_id}_{chart_id}_{symbol}_{resolution}"


def _legacy_drawing_storage_name(symbol: str, resolution: str):
    return f"drawings_{symbol}_{resolution}"


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

            # 已在缓存或正在计算中则跳过
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
                # HTTP 市场：4 周期并行，总耗时 ≈ max(各周期)；局部 import 避免顶层依赖扩散
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

    try:
        stocks = ex.stock_info(code)
    except Exception as e:
        # 数据源故障(如 QMT/xtquant 不可用)时优雅降级为 error,不抛到 flask 变 500。
        LogUtil.error(f"[tv_symbols] stock_info failed symbol={raw_symbol} err={e}")
        return {"s": "error", "errmsg": f"unknown symbol: {raw_symbol}"}
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

    # precision 缺失或非法时默认 100（即 2 位小数），避免 TradingView minmov 校验失败
    precision = stocks.get("precision")
    if precision is None:
        precision = 100
    else:
        try:
            precision = int(precision)
            if precision <= 0:
                precision = 100
        except (TypeError, ValueError):
            precision = 100

    info = {
        "name": stocks["code"],
        "ticker": f"{market}:{stocks['code']}",
        "full_name": f"{market}:{stocks['code']}",
        "description": stocks["name"],
        "exchange": market,
        "type": market_types.get(market, "stock"),
        "session": market_session.get(market, "24x7"),
        "timezone": market_timezone.get(market, "Asia/Shanghai"),
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
        tz_sh = pytz.timezone("Asia/Shanghai")

        def _fmt_ts(ts: int) -> str:
            """把 unix 时间戳格式化为上海时区可读时间;非正值(缺省 0)原样返回。"""
            if ts <= 0:
                return str(ts)
            return datetime.datetime.fromtimestamp(ts, tz_sh).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        LogUtil.info(
            f"[tv_history] >>> {symbol} {resolution} first={firstDataRequest} "
            f"from={_fmt_ts(_from)} to={_fmt_ts(_to)}"
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
            # 记录最后访问状态，供下次启动预热 RAM chart_data_cache；失败吞异常不影响主流程。
            try:
                record_user_request(market, code, frequency)
            except Exception:
                pass

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

        # 注意：必须先 get 出 RLock 对象再 with，确保整个临界区内引用持续存在
        # （_SafeLockRegistry 用 WeakValueDictionary 存储锁，无强引用会被 GC）
        _calc_lock = chart_calc_locks.get(cache_key)
        with _calc_lock:
            with cache_lock:
                cache_entry = _get_chart_cache_entry(cache_key)
                is_cache_hit, cl_chart_data, miss_reason = evaluate_cache_for_tv_history(
                    cache_entry, _from, _to, is_range_request
                )
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
                kline_args = {}
                # cache_empty(冷缓存,缓存里完全没有该 cache_key)即便是窄范围轮询
                # 请求,也必须按默认回看窗口全量拉取。否则空缓存会被窄窗口请求"种小"
                # 成只有几根 K 线的 entry,后续 prepend 又把它标成 is_full_snapshot=True,
                # 导致 firstDataRequest=true 命中这个"假全量"快照只返回几根 K 线。
                if is_range_request and cache_miss_reason != "cache_empty":
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

                _fetch_result = fetch_klines_and_compute_cl_data(
                    market, code, frequency, cl_config,
                    kline_args=kline_args,
                    is_range_request=is_range_request,
                    cache_miss_reason=cache_miss_reason,
                    cache_key=cache_key,
                    to_ts=_to,
                )
                if _fetch_result is None:
                    return {"s": "no_data"}
                cl_chart_data = _fetch_result["cl_chart_data"]
                cd = _fetch_result["cd"]

                # 跨周期 MACD (P5 third step)
                apply_higher_macd_to_chart_data(cl_chart_data, frequency, market, cl_config)
                # P8 取代 P7：停用真实多周期叠加，高级别中枢改由 recursive_levels 产出
                # apply_higher_zs_to_chart_data(cl_chart_data, market, code, frequency, cl_config)

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
                            # cache_empty 已按全量回看拉取(见上方 kline_args 分支),
                            # 与非范围请求同样是完整快照,标 is_full_snapshot=True。
                            is_full_snapshot=(
                                (not is_range_request)
                                or cache_miss_reason == "cache_empty"
                                or (existing_entry or {}).get("is_full_snapshot", False)
                            ),
                        )

                # prepend 路径在补完 higher_macd 后,需要把 cl_chart_data 重新写回 cache,
                # 否则下次相同范围请求 hit 时拿到的还是无 higher_macd 的版本。
                if cd is None and cl_chart_data is not None:
                    with cache_lock:
                        _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)

                # firstDataRequest 成功后，后台预热其他常用周期的缓存
                if firstDataRequest == "true":
                    prewarm_common_intervals(market, code, cl_config)

        # Cache hit 路径 lazy 补算 HTF MACD:
        # apply_higher_macd_to_chart_data 只在上面的 ``if not is_cache_hit`` 块里调,
        # cache hit 时若 cache 内 chart_data 缺 ``higher_macd_*`` (旧版 prewarm 路径
        # 漏调 apply、或 prepend 半态写入残留), 每次 hit 都会让 server 返回
        # ``higher_macd_hist: []`` 给前端 — 前端 study 拿不到 HTF 数据。
        # 这里 lazy 检测并即时补算 + 回写 cache, 一次修好后续 hit 都走快路径。
        # M1: 仅对"确有高周期倍率"的 frequency 补算（should_lazy_apply_higher_macd
        #     内已判定）。15m/60m 等无倍率周期 HTF 本就该缺失，旧逻辑只看长度不符
        #     会让它们每次 polling 都误判为需补算 → 冗余重写缓存 + 异步写盘。
        # M2: 补算 + 回写整体放进 cache_lock。apply_higher_macd 是对 cache 内共享
        #     chart_data 的 in-place 修改，必须与 _persist_chart_cache_async 的
        #     deepcopy 互斥；should_lazy 在锁内复查，并发请求只有一个真正补算。
        if is_cache_hit and cl_chart_data is not None:
            with cache_lock:
                if should_lazy_apply_higher_macd(cl_chart_data, frequency, market):
                    LogUtil.debug(
                        f"[tv_history] cache hit but HTF missing/short, "
                        f"lazy-applying {symbol} {resolution}"
                    )
                    # 仅当 apply 真正写入了 higher_macd_*（有倍率且 bar 数足够）
                    # 才回写缓存——否则 bar 数不足的周期每次 polling 都冗余写盘。
                    if apply_higher_macd_to_chart_data(
                        cl_chart_data, frequency, market, cl_config
                    ):
                        _existing = _get_chart_cache_entry(cache_key)
                        _is_full = (_existing or {}).get("is_full_snapshot", False)
                        _set_chart_cache_entry(
                            cache_key, cl_chart_data, is_full_snapshot=_is_full,
                        )

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
                cl_chart_data = slice_chart_data_to_window(cl_chart_data, _from, _to)
            except Exception as e:
                LogUtil.error(f"[tv_history] Slice data failed: {e}")

        # 切片后无数据,返回 no_data 阻止 TradingView 继续向前请求
        if len(cl_chart_data.get("t", [])) == 0:
            return {"s": "no_data"}

        _resp_times = cl_chart_data.get("t", []) or []
        cl_chart_data = trim_future_bars(cl_chart_data, _to)
        _resp_t = cl_chart_data.get("t", []) or []
        if len(_resp_t) < len(_resp_times):
            LogUtil.warning(
                f"[tv_history] Trimmed {len(_resp_times) - len(_resp_t)} future bar(s) beyond to={_to}"
            )

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
            "t": cl_chart_data.get("t", []),
            "c": cl_chart_data.get("c", []),
            "o": cl_chart_data.get("o", []),
            "h": cl_chart_data.get("h", []),
            "l": cl_chart_data.get("l", []),
            "v": cl_chart_data.get("v", []),
            "macd_dif": cl_chart_data.get("macd_dif", []),
            "macd_dea": cl_chart_data.get("macd_dea", []),
            "macd_hist": cl_chart_data.get("macd_hist", []),
            "macd_area": cl_chart_data.get("macd_area", []),
            "higher_macd_dif": cl_chart_data.get("higher_macd_dif", []),
            "higher_macd_dea": cl_chart_data.get("higher_macd_dea", []),
            "higher_macd_hist": cl_chart_data.get("higher_macd_hist", []),
            "fxs": cl_chart_data.get("fxs", []),
            "bis": cl_chart_data.get("bis", []),
            "xds": cl_chart_data.get("xds", []),
            "bi_zss": cl_chart_data.get("bi_zss", []),
            "xd_zss": cl_chart_data.get("xd_zss", []),
            "bcs": cl_chart_data.get("bcs", []),
            "mmds": cl_chart_data.get("mmds", []),
            # 拆分版买卖点/背驰(笔/段独立),前端按级别独立渲染 + 独立 toggle
            "bi_mmds": cl_chart_data.get("bi_mmds", []),
            "xd_mmds": cl_chart_data.get("xd_mmds", []),
            "bi_bcs": cl_chart_data.get("bi_bcs", []),
            "xd_bcs": cl_chart_data.get("xd_bcs", []),
            # 原文化新增(③④/区间套):走势类型区间 + 递归层级 + 区间套链
            "xd_zslx": cl_chart_data.get("xd_zslx", []),
            "xd_zslx_lines": cl_chart_data.get("xd_zslx_lines", []),
            "recursive_levels": cl_chart_data.get("recursive_levels", []),
            "higher_zs": cl_chart_data.get("higher_zs", []),
            "interval_nest": cl_chart_data.get("interval_nest"),
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
            if chart is None:
                # chart_id 不存在（已删除 / 脏 id）→ 返回 error，前端
                # getChartContent 对 status!='ok' 取 null，优雅降级，不 500。
                return {"status": "error"}
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
            if template is None:
                # template 不存在 → 返回 error，避免 None.name 抛 AttributeError。
                return {"status": "error"}
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

# ---------------------------------------------------------------------------
# 多周期叠加 (overlay):在当前周期图上叠加高级别周期的中枢/走势类型
# ---------------------------------------------------------------------------

# 当前 TV interval → 自动叠加哪些高级别 frequency(内部名)
# 设计原则:每次默认叠加 2-3 个高一档,既能看清"多级别联立"又控制性能。
_AUTO_OVERLAY_FREQS = {
    "1":   ["5m", "30m", "d"],
    "3":   ["15m", "60m", "d"],
    "5":   ["30m", "d", "w"],
    "10":  ["60m", "d", "w"],
    "15":  ["60m", "d", "w"],
    "30":  ["d", "w"],
    "60":  ["d", "w"],
    "1D":  ["w", "m"],
    "1W":  ["m"],
}
# overlay 合法 freq 白名单——用户从 query 传入 ``overlays=`` 参数时,
# 必须落在这个集合里才会被喂给 ``ex.klines``。防御不可信输入,即便
# exchange 实现对未知 freq 已 return [],也避免对内部接口的隐性依赖。
_OVERLAY_ALLOWED_FREQS = frozenset(
    f for freqs in _AUTO_OVERLAY_FREQS.values() for f in freqs
)


def _serialize_overlay_zss(cd) -> list:
    """从一个 overlay CL 对象提取 xd_zss 序列化为 chart 用 dict。"""
    out = []
    try:
        zss = cd.get_xd_zss()
    except Exception:
        return out
    for zs in zss:
        try:
            start_date = zs.start.end.k.date if zs.start else zs.lines[0].start.k.date
            end_date = zs.end.start.k.date if zs.end else zs.lines[-1].end.k.date
            out.append({
                "points": [
                    {"time": fun.datetime_to_int(start_date), "price": zs.zg},
                    {"time": fun.datetime_to_int(end_date), "price": zs.zd},
                ],
                "linestyle": "0" if zs.done else "1",
                "type": zs.type,
                "is_expanded": bool(getattr(zs, "expanded_with", [])),
                "sub_count": len(getattr(zs, "expanded_with", []) or []),
            })
        except Exception:
            continue
    return out


def _serialize_overlay_zslx(cd) -> list:
    out = []
    try:
        zslxs = cd.get_xd_zslx() or []
    except Exception:
        return out
    for zslx in zslxs:
        try:
            if not zslx.zss:
                continue
            first_zs, last_zs = zslx.zss[0], zslx.zss[-1]
            start_date = first_zs.start.end.k.date if first_zs.start else first_zs.lines[0].start.k.date
            end_date = last_zs.end.start.k.date if last_zs.end else last_zs.lines[-1].end.k.date
            out.append({
                "points": [
                    {"time": fun.datetime_to_int(start_date),
                     "price": max(z.gg for z in zslx.zss)},
                    {"time": fun.datetime_to_int(end_date),
                     "price": min(z.dd for z in zslx.zss)},
                ],
                "zslx_type": zslx.zslx_type,
                "direction": zslx.type,
                "done": bool(zslx.done),
                "zss_count": len(zslx.zss),
            })
        except Exception:
            continue
    return out


def _serialize_overlay_zslx_lines(cd) -> list:
    out = []
    try:
        zslxs = cd.get_xd_zslx() or []
    except Exception:
        return out
    for zslx in zslxs:
        try:
            if not zslx.zss:
                continue
            start = zslx.start or zslx.zss[0].lines[0].start
            end = zslx.end or zslx.zss[-1].lines[-1].end
            out.append({
                "points": [
                    {"time": fun.datetime_to_int(start.k.date), "price": start.val},
                    {"time": fun.datetime_to_int(end.k.date), "price": end.val},
                ],
                "linestyle": "0" if zslx.done else "1",
                "zslx_type": zslx.zslx_type,
                "direction": zslx.type,
                "done": bool(zslx.done),
                "zss_count": len(zslx.zss),
            })
        except Exception:
            continue
    return out


@tv_bp.route("/tv/overlays")
@login_required
def tv_overlays():
    """多周期叠加 endpoint:在当前 interval 图表上,返回若干高级别周期的
    中枢与走势类型(序列化为 TV chart shape 数据)。

    Query params:
        symbol:   TV symbol(如 ``a:SH.000001``)
        interval: 当前 TV interval(``1``/``5``/``30``/``1D``...)
        overlays: 可选,逗号分隔的目标 frequency(内部名,如 ``5m,30m,d``);
                  缺省走 ``_AUTO_OVERLAY_FREQS`` 映射。

    Response:
        ``{"overlays": {"5m": {"xd_zss": [...], "xd_zslx": [...]},
                        "30m": {...}, ...}, "current_interval": "1"}``
        失败/无数据时对应 freq 缺失或为空数组。
    """
    symbol = request.args.get("symbol", "")
    interval = request.args.get("interval", "")
    overlays_param = (request.args.get("overlays", "") or "").strip()

    market, code = _parse_tv_symbol(symbol)
    if market is None or code is None:
        return {"overlays": {}, "current_interval": interval, "error": "invalid_symbol"}

    # 解析目标 overlay frequencies——用户传 ``overlays=`` 时必须落在白名单内
    if overlays_param:
        overlay_freqs = [
            f.strip() for f in overlays_param.split(",")
            if f.strip() and f.strip() in _OVERLAY_ALLOWED_FREQS
        ]
    else:
        overlay_freqs = list(_AUTO_OVERLAY_FREQS.get(interval, []))

    if not overlay_freqs:
        return {"overlays": {}, "current_interval": interval}

    cl_config = query_cl_chart_config(market, code)

    try:
        ex = get_exchange(Market(market))
    except Exception as e:
        return {"overlays": {}, "current_interval": interval, "error": f"exchange_init: {e}"}

    out = {}
    for freq in overlay_freqs:
        try:
            klines = ex.klines(code, freq)
            if klines is None or len(klines) == 0:
                continue
            cds = web_batch_get_cl_datas(market, code, {freq: klines}, cl_config)
            if not cds:
                continue
            cd = cds[0]
            out[freq] = {
                "xd_zss": _serialize_overlay_zss(cd),
                "xd_zslx": _serialize_overlay_zslx(cd),
                "xd_zslx_lines": _serialize_overlay_zslx_lines(cd),
            }
        except Exception as e:
            LogUtil.warning(f"[tv_overlays] symbol={symbol} freq={freq} err={e}")
            continue

    return {"overlays": out, "current_interval": interval}
