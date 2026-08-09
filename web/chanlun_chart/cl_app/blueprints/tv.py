"""
TradingView 相关接口蓝图。

提供 /tv/config、/tv/symbols、/tv/search、/tv/history 等标准 UDF 接口，
以及图表/模板/画线存取和自定义 Marks 支持。
"""
import pytz
import json
import math
import datetime
import time
import threading
from pathlib import Path
from threading import Semaphore
from typing import Dict
from flask import Blueprint, current_app, request
from flask_login import login_required

from chanlun import fun
from chanlun.market import Market
from chanlun.cl_utils import (
    kcharts_frequency_h_l_map,
    query_cl_chart_config,
    web_batch_get_cl_datas,
)
from chanlun.persistence.db import db
from chanlun.exchange import get_exchange
from chanlun.exchange.lb_priority import lb_low_priority
from chanlun.tools.log_util import LogUtil
from chanlun.tools.daemon_executor import DaemonExecutor

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
    _get_chart_cache_entry_ram_only,
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


# ---------------------------------------------------------------------------
# tv_history 响应列对齐（按 bar index 的数值列必须与 t 等长）
# ---------------------------------------------------------------------------
# 前端按 index 取 c/o/h/l/v[i] 与 macd_*[i]/higher_macd_*[i]（上界 = t.length），任一列短于 t →
# 越界处取到 undefined → 静默 NaN（K 线缺口 / MACD 面板空洞，无异常无日志，最难排查）。正常计算路径
# 恒等长，但跨版本 / 半态磁盘冷层 entry 经 slice / 合并后可能错位（审查 F-1/MED-3）。形态对象数组
# （fxs/bis/mmds/...）长度本就 != bar 数，不在此列。
_TV_VALUE_COLUMNS = (
    "c", "o", "h", "l", "v",
    "macd_dif", "macd_dea", "macd_hist", "macd_area",
    "higher_macd_dif", "higher_macd_dea", "higher_macd_hist",
)


def _align_value_columns_to_t(cl_chart_data, symbol="", resolution=""):
    """把所有按 bar index 的数值列原地对齐到 len(t)：过长截断、过短右 pad None。

    仅当列非空且长度 != len(t) 时才动（空 / 缺列保持"无数据"语义，与既有 OHLCV 守卫一致）。
    """
    _t_col = cl_chart_data.get("t", []) or []
    _n_bars = len(_t_col)
    for _col_k in _TV_VALUE_COLUMNS:
        _col = cl_chart_data.get(_col_k) or []
        if _col and len(_col) != _n_bars:
            LogUtil.warning(
                f"[tv_history] 列 {_col_k} 长 {len(_col)} != t {_n_bars}, 已对齐 {symbol} {resolution}"
            )
            cl_chart_data[_col_k] = list(_col[:_n_bars]) + [None] * max(0, _n_bars - len(_col))

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
    get_cached_processed_stock,
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
    _build_display_frequency_cl,
    _merge_shape_lists,
    _shape_time,
    apply_higher_macd_to_chart_data,
    apply_higher_zs_to_chart_data,
    chart_calc_locks,
    compute_and_cache_chart_data,
    fetch_klines_and_compute_cl_data,
    market_now_trading,
    should_lazy_apply_higher_macd,
    serialize_chart_data_with_strict_runtime,
    slice_chart_data_to_window,
    strict_structure_history_fields,
    _decide_full_snapshot,
    _miss_source_is_full,
    trim_future_bars,
)
from ..services.kline_recompute import (  # noqa: E402
    prepend_klines_and_replace_cache,
)
from ..services.chart_revalidate import submit_revalidation  # noqa: E402


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


def _validated_review_chart_lock():
    """Return a server-verified human-review or risk-point causal lock."""

    values = {
        "candidate_id": str(request.args.get("review_candidate_id") or ""),
        "source_sha256": str(request.args.get("review_source_sha256") or ""),
        "review_as_of": str(request.args.get("review_as_of") or ""),
    }
    if not any(values.values()):
        return None
    if not all(values.values()):
        raise ValueError("partial human-review chart lock")
    service = current_app.extensions.get("decision_support_human_review")
    validator = getattr(service, "validate_chart_lock", None)
    if callable(validator):
        try:
            return validator(
                candidate_id=values["candidate_id"],
                source_sha256=values["source_sha256"],
                review_as_of=int(values["review_as_of"]),
            )
        except (TypeError, ValueError, RuntimeError):
            pass
    try:
        from ..services.research_audit import validate_risk_point_chart_lock

        return validate_risk_point_chart_lock(
            current_app.config.get(
                "RESEARCH_AUDIT_ROOT", Path(__file__).resolve().parents[4]
            ),
            point_id=values["candidate_id"],
            source_sha256=values["source_sha256"],
            review_as_of=int(values["review_as_of"]),
        )
    except (TypeError, ValueError, RuntimeError) as risk_error:
        raise ValueError(
            "chart lock was rejected by both human-review and risk-point audits"
        ) from risk_error


def _sector_chart_archive_for_lock(lock):
    """Resolve an already server-validated synthetic-sector chart lock."""

    if (
        not isinstance(lock, dict)
        or lock.get("chart_source_kind") != "VERIFIED_QMT_SECTOR_ARCHIVE"
    ):
        return None
    from ..services.sector_chart_archive import load_sector_chart_archive

    archive = load_sector_chart_archive(
        current_app.config.get(
            "RESEARCH_AUDIT_ROOT", Path(__file__).resolve().parents[4]
        ),
        expected_manifest_content_sha256=str(
            lock.get("sector_chart_archive_manifest_content_sha256") or ""
        ),
    )
    entry_id = str(lock.get("sector_chart_archive_entry_id") or "")
    entry = archive.entries_by_id.get(entry_id)
    if (
        entry is None
        or entry.get("sector_id") != lock.get("symbol")
        or entry.get("review_as_of_unix") != lock.get("review_as_of")
    ):
        raise ValueError("sector chart archive lock changed")
    return archive, entry_id


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


_USER_DRAWING_STATE_SCHEMA = "chanlun-user-drawings/v2"


def _empty_user_drawing_state():
    """Return the only drawing-state shape accepted by the current UI.

    Automatic Chanlun entities are reconstructed from chart data and must never
    be restored from TradingView's line-tool persistence.  Older records mixed
    those entities with manual drawings, so a schema-less state is deliberately
    quarantined instead of guessed or migrated.
    """

    return {
        "schema": _USER_DRAWING_STATE_SCHEMA,
        "sources": {},
        "groups": {},
    }


def _normalize_user_drawing_state(value):
    """Normalize an explicit v2 manual-drawing state, or quarantine it."""

    if not isinstance(value, dict):
        return None
    if value.get("schema") != _USER_DRAWING_STATE_SCHEMA:
        return None
    sources = value.get("sources")
    groups = value.get("groups")
    if not isinstance(sources, dict) or not isinstance(groups, dict):
        return None
    return {
        "schema": _USER_DRAWING_STATE_SCHEMA,
        "sources": {
            str(source_id): source_state
            for source_id, source_state in sources.items()
            if isinstance(source_state, dict)
        },
        # The frontend intentionally persists no TradingView groups because a
        # legacy group can retain references to filtered automatic entities.
        "groups": {},
    }


# 单标的内 4 周期是否并行预热。
# - HTTP 数据源（cq/polygon/futu）：True，并行可省 3-4 倍时间
# - native 数据源（xtquant/tdx）：False，因为 native 客户端线程不安全
# 启动时根据 market 决定，但 prewarm_common_intervals 会针对每次调用动态选。
# fx(tdx 外汇)是 native 源(每次调用新建 TdxExHq_API 连接),并行只会徒增连接压力,
# 故移出本集合,与 symbols.py 的 _NATIVE_SERIAL_MARKETS({a,futures,ny_futures,fx})对齐。
_PREWARM_INTERVALS_PARALLEL_MARKETS = {"us", "hk", "currency", "currency_spot"}
_PREWARM_PARALLEL_TIMEOUT_SECONDS = 60.0


def _run_parallel_prewarm(intervals, compute, should_abort):
    """Run one bounded parallel batch without joining hung dependency calls."""
    interval_list = list(intervals)
    if not interval_list:
        return True
    executor = DaemonExecutor(
        max_workers=len(interval_list),
        thread_name_prefix="PrewarmInterval",
        max_pending=len(interval_list),
    )
    futures = []
    deadline = time.monotonic() + max(
        0.01, float(_PREWARM_PARALLEL_TIMEOUT_SECONDS)
    )
    try:
        futures = [executor.submit(compute, interval) for interval in interval_list]
        for future in futures:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except Exception as exc:
                LogUtil.error(f"[tv_history] prewarm future error: {exc}")
            if should_abort():
                return False
        return True
    finally:
        for future in futures:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

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
                        with lb_low_priority():
                            klines = ex.klines(code, frequency_low, **kline_args)
                        cd, display_klines = _build_display_frequency_cl(
                            market=market,
                            code=code,
                            fetched_klines=klines,
                            fetched_frequency=frequency_low,
                            display_frequency=freq,
                            cl_config=cl_config,
                        )
                    else:
                        with lb_low_priority():
                            klines = ex.klines(code, freq, **kline_args)
                        cd = web_batch_get_cl_datas(market, code, {freq: klines}, cl_config)[0]
                        display_klines = klines
                        to_frequency = None
                else:
                    with lb_low_priority():
                        klines = ex.klines(code, freq, **kline_args)
                    cd = web_batch_get_cl_datas(market, code, {freq: klines}, cl_config)[0]
                    display_klines = klines

                cl_chart_data = serialize_chart_data_with_strict_runtime(
                    market=market,
                    code=code,
                    display_frequency=freq,
                    display_klines=display_klines,
                    legacy_cd=cd,
                    legacy_config=cl_config,
                )
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
                completed = _run_parallel_prewarm(
                    COMMON_INTERVALS,
                    lambda interval: _compute_one_interval(interval, ex, tz_sh),
                    lambda: (
                        _user_acted_after_prewarm_start()
                        or not _is_still_latest()
                    ),
                )
                if not completed:
                    LogUtil.warning(
                        f"[tv_history] Prewarm aborted (user acted) for {market}:{code}"
                    )
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
    supportedResolutions = list(frequency_maps.values())
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
        review_lock = _validated_review_chart_lock()
        sector_archive = _sector_chart_archive_for_lock(review_lock)
    except (TypeError, ValueError, RuntimeError) as exc:
        LogUtil.warning(f"[tv_symbols] rejected review lock: {exc}")
        return {"s": "error", "errmsg": "invalid causal chart lock"}
    if sector_archive is not None:
        if market != "a" or code != review_lock.get("symbol"):
            return {"s": "error", "errmsg": "causal chart symbol mismatch"}
        from ..services.sector_chart_archive import sector_chart_symbol_info

        archive, entry_id = sector_archive
        return sector_chart_symbol_info(
            archive,
            entry_id=entry_id,
            interval=str(review_lock["chart_interval"]),
        )

    # 先读已恢复的 last-known-good symbol 缓存。冷启动时 QMT 的全市场刷新会长时间
    # 持有 xtdata native lock；若这里先调 stock_info，前端 Requester 会在 15 秒后超时并把
    # 一次临时阻塞永久记成 unknown_symbol，直到用户手动“重新加载数据”。缓存命中时不再
    # 调用 stock_info / xtdata native lock，保证 TradingView 首次 resolveSymbol 能立即完成。
    stocks = get_cached_processed_stock(market, code)
    ex = None
    if stocks is None:
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
    if market == "a" and ex is not None:
        try:
            gnbk = ex.stock_owner_plate(code)
            sector = " / ".join([_g["name"] for _g in gnbk["GN"]])
            industry = " / ".join([_h["name"] for _h in gnbk["HY"]])
        except Exception:
            pass

    # precision 缺失或非法时使用 K 线精度的同一规则；A 股磁盘 LKG 为控制体积不保存
    # precision，因此 ETF/基金需按代码恢复到 1000，普通股票恢复到 100。
    precision = stocks.get("precision")
    if precision is None:
        # 外汇(tdx_fx)stock_info 也不带 precision；与 K 线归一精度对齐，避免截断。
        if market in ("a", "fx"):
            from chanlun.exchange.kline_precision import resolve_decimals

            _dec = resolve_decimals(market, code)
            precision = 10 ** _dec if _dec is not None else 100
        else:
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


# /tv/quotes 单次请求标的数上限(自选组通常 < 100, 防超大列表打爆数据源)。
_MAX_QUOTE_SYMBOLS = 500


@tv_bp.route("/tv/quotes")
@login_required
def tv_quotes():
    """TradingView UDF 行情接口 —— 自选组(watchlist)实时报价来源。

    前端 datafeed 的 ``QuotesPulseProvider`` 按 Fast/General 定时器调 ``getQuotes``
    打 ``/tv/quotes?symbols=a:SH.513100,a:SZ.000001``, 据此周期性刷新自选列表的
    现价/涨跌幅。**缺此端点时前端每次请求 404 → 自选组行情不自动更新**(本次修复)。

    复用 ``ex.ticks()``(与 ``/ticks`` 同一取数口径), 按 market 分组批量取, 返回
    UDF 标准格式 ``{s:"ok", d:[{s:"ok", n:symbol, v:{lp,ch,chp,...}}]}``。Tick 不带
    昨收, 由现价 + 涨跌幅% 反推昨收与绝对涨跌额。单个 market 取数失败仅该组标记
    error, 不拖垮整批(自选常含多市场)。
    """
    symbols_raw = request.args.get("symbols", "")
    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
    if not symbols:
        return {"s": "ok", "d": []}
    symbols = symbols[:_MAX_QUOTE_SYMBOLS]

    # 按 market 分组: market -> {code: 原始 symbol}(返回的 n 字段须与请求字面一致,
    # 否则 TradingView 匹配不上不更新)。
    by_market: dict = {}
    data = []
    for sym in symbols:
        market, code = _parse_tv_symbol(sym)
        if market is None or code is None:
            data.append({"s": "error", "n": sym, "v": {}})
            continue
        by_market.setdefault(market, {})[code] = sym

    for market, code_map in by_market.items():
        try:
            ex = get_exchange(Market(market))
            stock_ticks = ex.ticks(list(code_map.keys()))
        except Exception:
            LogUtil.exception(
                f"[tv_quotes] ticks failed market={market} n={len(code_map)}"
            )
            for sym in code_map.values():
                data.append({"s": "error", "n": sym, "v": {}})
            continue
        if not isinstance(stock_ticks, dict):
            LogUtil.warning(
                f"[tv_quotes] ticks returned invalid payload market={market} "
                f"type={type(stock_ticks).__name__}"
            )
            stock_ticks = {}
        for code, sym in code_map.items():
            t = stock_ticks.get(code)
            if t is None or t.last is None:
                data.append({"s": "error", "n": sym, "v": {}})
                continue
            try:
                last = float(t.last)
                rate = float(t.rate or 0)
                # rate 为涨跌幅百分比(Tick 文档口径)→ 反推昨收: prev = last/(1+rate/100)。
                prev_close = last / (1 + rate / 100) if rate != -100 else last
                open_p = float(t.open) if t.open is not None else last
                high_p = float(t.high) if t.high is not None else last
                low_p = float(t.low) if t.low is not None else last
                volume = float(t.volume) if t.volume is not None else 0.0
            except Exception:
                # 单个标的字段转换异常仅标记该标的 error, 不拖垮同批其它标的。
                LogUtil.exception(
                    f"[tv_quotes] tick convert failed market={market} code={code}"
                )
                data.append({"s": "error", "n": sym, "v": {}})
                continue
            # NaN/Infinity 不是合法 JSON: Flask(allow_nan=True) 会原样输出裸 NaN token,
            # 打断前端 JSON.parse → 整批(含健康标的)报价更新失败。命中即降级该标的,
            # 决不让非有限值进入最终 JSON(NaN 与任何数比较均为 False, rate!=-100 拦不住)。
            if not all(
                math.isfinite(x)
                for x in (last, rate, prev_close, open_p, high_p, low_p, volume)
            ):
                data.append({"s": "error", "n": sym, "v": {}})
                continue
            data.append({
                "s": "ok",
                "n": sym,
                "v": {
                    "lp": last,
                    "ch": round(last - prev_close, 4),
                    "chp": round(rate, 2),
                    "open_price": open_p,
                    "high_price": high_p,
                    "low_price": low_p,
                    "prev_close_price": round(prev_close, 4),
                    "volume": volume,
                },
            })
    return {"s": "ok", "d": data}


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

def _lazy_writeback_htf(
    cache_key, cl_chart_data, frequency, market, cl_config, symbol="", resolution=""
):
    """Cache-hit 但 chart_data 缺 higher_macd_* 时懒补算 HTF MACD 并回写缓存, 返回补算后的
    chart_data(供本次响应用), 未补算返回原 cl_chart_data。

    并发安全(M2/T0-2): 补算+回写在 cache_lock 内; apply 只对浅拷副本(dict())加顶层
    higher_macd_* 键/整列替换, 不原地改共享 dict, 锁外 trim/SSE 迭代恒安全。
    """
    with cache_lock:
        if not should_lazy_apply_higher_macd(cl_chart_data, frequency, market):
            return cl_chart_data
        LogUtil.debug(
            f"[tv_history] cache hit but HTF missing/short, lazy-applying {symbol} {resolution}"
        )
        # R15-C1: 基于缓存 entry 当前 data 补算(而非请求入口 T0 读到的 cl_chart_data 陈旧快照),
        # 否则并发写者(SSE/revalidate 持 chart_calc_locks 完成的全量重算)在 T0 后写入的新缠论
        # 会被这里的回写整体覆盖(TOCTOU 数据丢失)。缓存缺 entry 时回退用传入快照。
        _existing = _get_chart_cache_entry_ram_only(cache_key)
        _base = (_existing or {}).get("data")
        if _base is None:
            _base = cl_chart_data
        _patched = dict(_base)
        if apply_higher_macd_to_chart_data(_patched, frequency, market, cl_config):
            _is_full = (_existing or {}).get("is_full_snapshot", False)
            _set_chart_cache_entry(cache_key, _patched, is_full_snapshot=_is_full)
            return _patched
        return cl_chart_data

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
        try:
            _review_lock = _validated_review_chart_lock()
        except (TypeError, ValueError, RuntimeError) as exc:
            LogUtil.warning(f"[tv_history] rejected human-review lock: {exc}")
            return {"s": "no_data"}
        _review_as_of = (
            None if _review_lock is None else int(_review_lock["review_as_of"])
        )
        if _review_as_of is not None:
            _to = _review_as_of if _to <= 0 else min(_to, _review_as_of)
            if _from > _review_as_of:
                return {"s": "no_data"}
        # H1(阶段E): 前端断档 gap-reset 主动带 force_refresh=1 → 绕过缓存强制重算,补齐断档。
        # 绕过而非删缓存:走既有 MISS→重算路径,重算失败旧 entry 仍在(符合 C1"绝不丢好缓存")。
        force_refresh = request.args.get("force_refresh") == "1"
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
        if _review_lock is not None and (
            market != "a" or code != _review_lock.get("symbol")
        ):
            LogUtil.warning("[tv_history] review lock symbol mismatch")
            return {"s": "no_data"}
        if (
            _review_lock is not None
            and _review_lock.get("lock_kind") == "RISK_POINT_AUDIT"
            and resolution != _review_lock.get("chart_interval")
        ):
            LogUtil.warning("[tv_history] risk-point review lock interval mismatch")
            return {"s": "no_data"}

        try:
            sector_archive = _sector_chart_archive_for_lock(_review_lock)
        except (TypeError, ValueError, RuntimeError) as exc:
            LogUtil.warning(f"[tv_history] sector archive lock rejected: {exc}")
            return {"s": "no_data"}
        if sector_archive is not None:
            from ..services.sector_chart_archive import (
                SectorChartArchiveUnavailable,
                sector_chart_history_payload,
            )

            archive, entry_id = sector_archive
            try:
                return sector_chart_history_payload(
                    archive,
                    entry_id=entry_id,
                    interval=resolution,
                    from_ts=_from,
                    to_ts=_to,
                )
            except SectorChartArchiveUnavailable as exc:
                LogUtil.warning(f"[tv_history] sector archive unavailable: {exc}")
                return {"s": "no_data"}

        frequency = resolution_maps.get(resolution)
        if frequency is None:
            LogUtil.warning(f"[tv_history] Unsupported resolution: {resolution}")
            return {"s": "no_data"}
        # 后端闸门:frequency 必须在该 market 实际支持的周期内(= 前端 supported_resolutions 的来源)。
        # 前端已按此过滤, 但手构请求(curl/改 URL)可绕过——传入 market 不支持的周期(如季线 q 对 cq
        # 美股/港股、qmt A股), 会落到各 exchange 不一致的处理(cq 返回空 / qmt·binance frequency_map
        # KeyError 抛 500 / tdx 系碰巧 frequency_map 有 q 而拉季线)。统一在此干净拒绝, 后端不依赖前端
        # 闸门。market_frequencys[market] 为空(exchange 初始化失败)时跳过本检查, 避免误拦正常请求。
        _supported_freqs = market_frequencys.cached_snapshot((market,)).get(
            market, []
        )
        if _supported_freqs and frequency not in _supported_freqs:
            LogUtil.warning(
                f"[tv_history] market={market} 不支持周期 {frequency}(resolution={resolution}), 拒绝"
            )
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
        if _review_lock is not None:
            # 严禁与实时快照共享缓存：先按复核时点截断 K 线，再计算结构；不能
            # 在包含未来 K 线的结构结果上做事后裁剪。
            cache_key += (
                f"_review_{_review_lock['candidate_id'][7:]}_{_review_as_of}"
            )

        cl_chart_data = None
        is_cache_hit = False
        cache_miss_reason = "cache_empty"
        is_range_request = (
            firstDataRequest == "false"
            and _from > 0
            and _to > 0
            and _to >= _from
        )
        if firstDataRequest == "false" and not is_range_request:
            # false 本意是 polling/向左滚动,但 from/to 缺失或非法(畸形请求或 resolution 切换瞬间)
            # 会退化进非 range 分支、可能回吐整条全量(审查 M-3,触发面窄)。记 debug 便于线上定位,
            # 不改行为(非 range 分支自带 stale 兜底)。
            LogUtil.debug(f"[tv_history] false 但非 range from={_from} to={_to} {code} {frequency}")

        # 注意：必须先 get 出 RLock 对象再 with，确保整个临界区内引用持续存在
        # （_SafeLockRegistry 用 WeakValueDictionary 存储锁，无强引用会被 GC）
        # 方向2: 交易时段决定 serve-stale 的过期阈值(盘中短/收盘长)。在锁外算
        # (带 30s TTL 缓存), 不占用 cache_lock 临界区。
        _market_trading = (
            False if _review_lock is not None else market_now_trading(market)
        )
        _needs_refresh = False
        _calc_lock = chart_calc_locks.get(cache_key)
        with _calc_lock:
            # RAM miss may synchronously read a pickle. Keep that I/O outside the
            # process-wide cache lock; the per-key calc lock still serializes writes.
            cache_entry = _get_chart_cache_entry(cache_key)
            if _review_lock is not None and cache_entry is not None:
                # 历史复核输入不可变，禁止 live stale-revalidate 用当前行情覆盖它。
                cache_entry = {**cache_entry, "validated_at": time.time()}
            with cache_lock:
                is_cache_hit, cl_chart_data, miss_reason, _needs_refresh = (
                    evaluate_cache_for_tv_history(
                        cache_entry, _from, _to, is_range_request,
                        market_is_trading=_market_trading,
                        force_refresh=force_refresh,
                    )
                )
                if not is_cache_hit:
                    cache_miss_reason = miss_reason

            # D4-F1/F2: 用与 cl_chart_data 同源的 is_full(cache-hit 取本次 entry), 避免 1050 行锁外
            # 重取 entry 产生 TOCTOU(窄 local + 并发全量写 entry → gate 误判 → 前端整体替换丢窗外形态)。
            _src_is_full = bool(cache_entry and cache_entry.get("is_full_snapshot", False))

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
                    # 此处不 mark validated:"请求窗口早于缓存最早时间"只证明这个窄窗口无数据,
                    # 不证明整条 entry 末端新鲜。进程重启后命中过期磁盘 entry 时若误标 fresh,
                    # 随后的 tail_gap polling 会命中缺停机期 K 线的旧数据、绕过 stale 兜底(审查 H-2)。
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
                    end_at = (
                        datetime.datetime.fromtimestamp(_review_as_of, tz_sh)
                        if _review_as_of is not None
                        else datetime.datetime.now(tz_sh)
                    )
                    kline_args["end_date"] = end_at.strftime("%Y-%m-%d %H:%M:%S")

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
                # D4-F1/F2: MISS 全量性与 entry 写入 is_full_snapshot 同口径(非range/cache_empty/prepend cd None→全量)。
                _src_is_full = _miss_source_is_full(is_range_request, cache_miss_reason, cd is None)

                # 跨周期 MACD (P5 third step)
                _htf_applied = apply_higher_macd_to_chart_data(cl_chart_data, frequency, market, cl_config)
                # P8 取代 P7：停用真实多周期叠加，高级别中枢改由 recursive_levels 产出
                # apply_higher_zs_to_chart_data(cl_chart_data, market, code, frequency, cl_config)

                if cd is not None:
                    # 2026-07 修复(幽灵形态): 不再与 existing_entry 做 _merge_chart_data 合并。
                    # 这里走到的都是 MISS 全量重算(cache_empty/cache_partial_snapshot/
                    # cache_stale_snapshot 等), cl_chart_data 本身就是基于完整回看窗口的
                    # 全量权威结果。existing_entry 可能是几分钟到几天前的陈旧快照——
                    # too_stale(cache_stale_snapshot)分支存在的目的就是防止把陈旧未完成
                    # 笔/线段/中枢泄漏给用户, 若仍用"起点身份并集"合并, 陈旧快照里起点
                    # 已被新行情证伪的形态会被原样保留、和新数据一起返回, 安全网形同虚设。
                    with cache_lock:
                        _set_chart_cache_entry(
                            cache_key,
                            cl_chart_data,
                            # cache_empty 已按全量回看拉取(见上方 kline_args 分支),
                            # 与非范围请求同样是完整快照,标 is_full_snapshot=True。
                            # ⚠ 不再继承 existing_entry 的 is_full_snapshot:range-miss 是窄窗口结果,
                            # 继承会把"窄范围 merge 进旧全量"误标成完整快照,令 firstDataRequest 命中
                            # 只有几根 K 线的假全量(审查 H-1,目前仅靠 tail_gap 改道 prepend 侥幸不触发)。
                            is_full_snapshot=(
                                (not is_range_request)
                                or cache_miss_reason == "cache_empty"
                            ),
                        )

                # prepend 路径在补完 higher_macd 后,需要把 cl_chart_data 重新写回 cache,
                # 否则下次相同范围请求 hit 时拿到的还是无 higher_macd 的版本。
                # M-2: 仅当 higher_macd 真被应用(_htf_applied=True)才回写——此回写纯为把新补的
                # higher_macd 落盘(prepend 自身已落基础数据:changed 走 _set、unchanged 不变)。
                # 无高周期倍率的周期(15m/60m/d/w/m 等)apply 返回 False → 跳过这次对未变数据的重复
                # deepcopy+异步写盘(纯 IO);有倍率周期 apply 每次重算返 True 仍照常回写,无害不丢数据。
                if cd is None and cl_chart_data is not None and _htf_applied:
                    with cache_lock:
                        _set_chart_cache_entry(cache_key, cl_chart_data, is_full_snapshot=True)

                # firstDataRequest 成功后，后台预热其他常用周期的缓存
                if firstDataRequest == "true" and _review_lock is None:
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
        if is_cache_hit and cl_chart_data is not None and not _needs_refresh:
            cl_chart_data = _lazy_writeback_htf(
                cache_key, cl_chart_data, frequency, market, cl_config, symbol, resolution
            )

        # 方向1 (stale-while-revalidate): firstDataRequest 命中"过期全量快照"已即时
        # 返回旧快照(秒显), 这里派去重的后台重验证拉全新数据写回缓存, 经现有
        # SSE 推送 / TV polling 自愈到前端。submit 非阻塞, 不影响本次响应延迟。
        if is_cache_hit and _needs_refresh and _review_lock is None:
            submit_revalidation(market, code, frequency, cl_config, cache_key)

        if cl_chart_data is None:
            return {"s": "no_data"}

        bar_times = cl_chart_data.get("t", [])
        # D4-F1: 纯轮询/SSE 降级下 /tv/history 轮询响应原按窄窗口 slice 形态且不带 full_snapshot,
        # 前端窗口外"只增不删" -> 起点早于窄窗口的被撤销形态幽灵残留(SSE 正常 ~8s 自愈, 纯轮询/
        # low2high 不自愈)。仅"最近窗口权威 + 源全量快照"时带全量形态 + full_snapshot=True, 前端
        # 已有整体替换分支清幽灵、bars/MACD 仍走增量不缩图; 向左滚动/窄窗口不置(防丢窗外合法形态)。
        # D4-F1/F2: _src_is_full 已在 _calc_lock 内与 cl_chart_data 同源捕获(消 TOCTOU), 此处不重取 entry。
        _strict_source_data = cl_chart_data
        _emit_full_snapshot = _decide_full_snapshot(
            firstDataRequest,
            _to,
            bar_times,
            _src_is_full,
            frequency=frequency,
        )
        _full_shape_snapshot = None
        if _emit_full_snapshot:
            _shape_keys = ("fxs", "bis", "xds", "bi_zss", "xd_zss", "bcs", "mmds",
                           "bi_mmds", "xd_mmds", "bi_bcs", "xd_bcs", "xd_zslx",
                           "xd_zslx_lines", "recursive_levels", "higher_zs", "interval_nest")
            _full_shape_snapshot = {_k: cl_chart_data.get(_k) for _k in _shape_keys}
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
                cl_chart_data = slice_chart_data_to_window(
                    cl_chart_data,
                    _from,
                    _to,
                    frequency=frequency,
                )
            except Exception as e:
                LogUtil.error(f"[tv_history] Slice data failed: {e}")

        # 切片后无数据,返回 no_data 阻止 TradingView 继续向前请求
        if len(cl_chart_data.get("t", [])) == 0:
            return {"s": "no_data"}

        _resp_times = cl_chart_data.get("t", []) or []
        cl_chart_data = trim_future_bars(
            cl_chart_data,
            _to,
            frequency=frequency,
        )
        if _full_shape_snapshot is not None:
            # D4-F1: slice 已把形态切窄, 换回全量供前端整体替换清幽灵(bars 保持窗口化不缩图)。
            for _k, _v in _full_shape_snapshot.items():
                if _v is not None:
                    cl_chart_data[_k] = _v
        _resp_t = cl_chart_data.get("t", []) or []
        if len(_resp_t) < len(_resp_times):
            LogUtil.warning(
                f"[tv_history] Trimmed {len(_resp_times) - len(_resp_t)} future bar(s) beyond to={_to}"
            )
        if not _resp_t:
            return {"s": "no_data"}

        # 严格快照按原始收盘时刻做身份校验；日/周/月仅在裁剪与图表坐标层
        # 使用周期锚点。把协议字段放到最终窗口确定之后，避免响应 t 已裁短而
        # strict_structure.source_closed_at 仍指向被裁掉的末根。
        _strict_history_fields = strict_structure_history_fields(
            _strict_source_data,
            authoritative=(
                firstDataRequest == "true" or _emit_full_snapshot
            ),
            expected_source_closed_at=_resp_t[-1],
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

        # 按 bar index 的数值列（OHLCV + macd_* + higher_macd_*）必须与 t 等长，否则前端越界取
        # undefined → 静默 NaN（无异常无日志，最难排查，审查 F-1/MED-3）。统一对齐见 _align_value_columns_to_t。
        _align_value_columns_to_t(cl_chart_data, symbol, resolution)

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
            "full_snapshot": _emit_full_snapshot,
            **_strict_history_fields,
        }
    except Exception as e:
        req_qs = request.query_string.decode("utf-8", errors="ignore")
        LogUtil.error(f"[tv_history] unhandled error query={req_qs} err={e}", exc_info=True)
        return {
            "s": "error",
            "errmsg": "History service is temporarily unavailable.",
        }, 503


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
        payload = request.get_json(silent=True)
        if request.is_json and not isinstance(payload, dict):
            return {
                "status": "error",
                "message": "JSON body must be an object.",
            }, 400
        payload = payload or {}
        content = payload.get("state")
        if not isinstance(content, dict):
            return {
                "status": "error",
                "message": "state must be a JSON object.",
            }, 400
        normalized = _normalize_user_drawing_state(content)
        if normalized is None:
            # Old tabs can keep executing the pre-v2 JavaScript after the app
            # has been upgraded.  A 2xx acknowledgement stops their retry/log
            # loop, while ignored=true makes the quarantine observable.  Most
            # importantly, the contaminated legacy payload never reaches DB.
            return {
                "status": "ok",
                "ignored": True,
                "reason_code": "LEGACY_DRAWING_STATE_QUARANTINED",
            }
        db.tv_chart_save(
            "drawing",
            client_id,
            user_id,
            drawing_name,
            json.dumps(normalized, ensure_ascii=False, sort_keys=True),
            symbol,
            resolution,
        )
        return {"status": "ok"}

    if request.method == "GET":
        drawing = db.tv_chart_get_by_name("drawing", drawing_name, client_id, user_id)
        if drawing is None:
            legacy_drawing = db.tv_chart_get_by_name(
                "drawing", legacy_drawing_name, client_id, user_id
            )
            legacy_state = None
            if legacy_drawing is not None:
                try:
                    legacy_state = _normalize_user_drawing_state(
                        json.loads(legacy_drawing.content)
                    )
                except Exception:
                    legacy_state = None
            # Only an already-versioned manual state may cross the old key
            # boundary.  Schema-less records are the source of the QQQ 1m
            # orange shapes leaking into the 30m canvas and must stay inert.
            if legacy_state is not None:
                db.tv_chart_save(
                    "drawing",
                    client_id,
                    user_id,
                    drawing_name,
                    json.dumps(legacy_state, ensure_ascii=False, sort_keys=True),
                    symbol,
                    resolution,
                )
                return {"status": "ok", "data": legacy_state}

        if drawing:
            try:
                data = _normalize_user_drawing_state(json.loads(drawing.content))
            except Exception:
                data = None
            return {
                "status": "ok",
                "data": data or _empty_user_drawing_state(),
            }
        return {
            "status": "ok",
            "data": _empty_user_drawing_state(),
        }
