"""图表数据缓存层（service，cache 全链路）。

L1 Phase 2 + Tier 4 P1 重构：把 tv.py 里 chart cache 相关的状态 + 函数集中迁出，
形成完整的 cache 层（读 + 写 + 异步落盘 + 负缓存），让 tv.py 真正回归"路由层"。

状态：
- ``chart_data_cache``: RAM 热层（TTLCache）；与 fdb.get_chart_cache 的磁盘冷层
  组成两层缓存（RAM miss → disk → 回填 RAM）。
- ``cache_lock``: RLock；多线程读写 chart_data_cache 时的粗粒度互斥。
- ``_CACHE_REVALIDATION_INTERVAL``: 30s，缓存在此时间内被验证过则视为有效。
- ``_chart_cache_disk_executor``: 异步落盘线程池（4 worker）。
- ``_negative_cache``: 空数据 cache_key 短期负缓存（5 min TTL），防新上市标的反复拉空。

工具函数（纯）：
- ``_stable_hash`` / ``_build_cache_key``: 跨进程稳定的 cache_key 构造
- ``_build_chart_cache_entry`` / ``_normalize_cache_entry``: entry 字段规范化
- ``_cache_entry_recently_validated``: 验证时间戳判断

业务函数：
- ``_get_chart_cache_entry``: 两层读取（RAM → disk → warm RAM）
- ``_set_chart_cache_entry``: 两层写入（RAM 立即可见 + 异步落盘）
- ``_mark_chart_cache_validated``: 更新 entry.validated_at
- ``_persist_chart_cache_async``: 提交磁盘写入，异常 fallback 同步
- ``_is_negatively_cached`` / ``_mark_negative_cache``: 空数据负缓存
"""
import copy
import hashlib
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from threading import RLock
from typing import Dict, Optional

from cachetools import TTLCache

from chanlun.file_db import fdb
from chanlun.tools.log_util import LogUtil

# ---------------- 状态 ----------------

# 图表数据计算结果缓存（RAM 热层）。
# RAM 仅做热点加速，持久化由 fdb.set/get_chart_cache 兜底（RAM 淘汰后磁盘仍可命中）。
chart_data_cache: TTLCache = TTLCache(maxsize=512, ttl=3600)

cache_lock: RLock = RLock()

# 缓存数据最近验证时间戳（防止非交易时段 DataPulse 反复 cache miss）
# H4: 验证时间戳直接放在 chart_data_cache 的 entry["validated_at"] 中，
# 不再单独维护 chart_data_validated_at TTLCache。
_CACHE_REVALIDATION_INTERVAL = 30  # 秒，缓存在此时间内被验证过则视为有效

# firstDataRequest=true 路径下 is_full_snapshot 快照的过期阈值 (远大于 polling 30s,
# 远小于"停机数天"; 重启后磁盘冷层旧 entry 能识别为过期, 强制 cache miss 拉新数据)。
_SNAPSHOT_STALE_AFTER = 3600  # 秒


# ---------------- 工具函数 ----------------

def _stable_hash(obj) -> str:
    """
    生成稳定的 hash（不受 PYTHONHASHSEED 影响，跨进程/重启一致）。
    这样多 worker 部署、进程重启后 cache_key 仍然稳定，缓存命中率不会被打穿。
    """
    try:
        s = json.dumps(obj, sort_keys=True, default=str)
    except Exception:
        s = str(obj)
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# chart_data 序列化结构版本号。
#
# 后端 ``cl_data_to_tv_chart`` 输出的 chart_data dict 结构改动(新增/重命名字段、
# 字段语义变化)时,**必须 bump 这个版本**——否则启动 ``chart_warm`` 会把磁盘
# (fdb)里旧版本的 chart_data 回填 RAM,endpoint cache hit 永远拿到 stale 数据。
#
# 历史:
# - v5 (2026-05) ── 加入版本号机制本身。本次 bump 让 ``recursive_levels`` /
#   ``interval_nest`` / ``xd_zslx`` / ``bi_mmds`` / ``xd_mmds`` / ``bi_bcs`` /
#   ``xd_bcs`` 等原文化新字段在旧 entry 上全部失效,杜绝 endpoint 漏字段。
# - v6 (2026-05) ── 新增 ``xd_zslx_lines`` 以及 recursive_levels[*].zslx_lines,
#   让当前级别走势类型以线段形式参与下一层中枢显示。
# - v7 (2026-06) ── 级别纠正(递归 L0=线段中枢 / 笔中枢走 bi_zss 观察层 / 多周期
#   higher_zs)+ 中枢区间改用核心区 [ZD,ZG]。均改 chart_data 内容但不进 config,
#   bump 强制旧磁盘缓存失效重算,否则用户看不到这些改动。
_CHART_CACHE_SCHEMA_VERSION = "v7"


def _build_cache_key(market: str, code: str, frequency: str, cl_config: dict) -> str:
    """统一构造 chart_data_cache 的 key,确保所有调用方一致。

    key 含 ``_CHART_CACHE_SCHEMA_VERSION`` 前缀——bump 该版本号即可让所有旧
    磁盘 entry 失效,新版本路径无 stale 字段污染风险。
    """
    return f"{_CHART_CACHE_SCHEMA_VERSION}_{market}_{code}_{frequency}_{_stable_hash(cl_config)}"


def _build_chart_cache_entry(cl_chart_data: dict, is_full_snapshot: bool, validated_at: float = None):
    validated_at = time.time() if validated_at is None else validated_at
    bar_times = cl_chart_data.get("t", []) if isinstance(cl_chart_data, dict) else []
    return {
        "data": cl_chart_data,
        "min_time": bar_times[0] if len(bar_times) > 0 else None,
        "max_time": bar_times[-1] if len(bar_times) > 0 else None,
        "validated_at": validated_at,
        "is_full_snapshot": bool(is_full_snapshot),
    }


def _normalize_cache_entry(cached) -> Optional[dict]:
    """把任意来源（RAM / 磁盘）的 cache 对象规范化为带 validated_at 的 dict。

    None / 非 dict 一律视为 miss；老格式没有 validated_at 时补一个当前时间，
    保持下游 _cache_entry_recently_validated 等逻辑可用。
    """
    if cached is None:
        return None
    if isinstance(cached, dict) and "data" in cached and "validated_at" in cached:
        return cached
    if isinstance(cached, dict):
        return _build_chart_cache_entry(cached, is_full_snapshot=True, validated_at=time.time())
    return None


def _get_chart_cache_entry(cache_key: str):
    """两层缓存读取：先 RAM、miss 再走磁盘并回填 RAM。

    磁盘命中后立刻 warm 回 RAM（直接赋值不触发异步落盘——entry 来自磁盘已经持久化），
    后续相同 cache_key 的访问就走 RAM 热层。

    磁盘读失败（损坏/IO 异常）由 fdb.get_chart_cache 内部处理，这里看到的是 None。

    线程安全：``chart_data_cache`` 是非线程安全的 TTLCache，所有读写都必须在
    ``cache_lock`` 内。本函数自带 ``cache_lock``（可重入 RLock），调用方无需
    （也可重复）持锁——既保护直接调用方，也保护经 kline_recompute / symbols
    等不持锁路径进来的访问。
    """
    with cache_lock:
        entry = _normalize_cache_entry(chart_data_cache.get(cache_key))
        if entry is not None:
            return entry

        # RAM miss → 尝试磁盘冷层
        try:
            disk_entry = fdb.get_chart_cache(cache_key)
        except Exception as e:
            LogUtil.warning(f"[chart_cache] disk read failed key={cache_key} err={e}")
            disk_entry = None
        entry = _normalize_cache_entry(disk_entry)
        if entry is None:
            return None

        # 回填 RAM；不再异步写盘（来源就是磁盘）。
        chart_data_cache[cache_key] = entry

        # 机会型清理：极低概率触发，避免 chart_cache 目录膨胀。
        if random.randint(0, 2000) <= 1:
            try:
                fdb.maybe_cleanup_chart_cache()
            except Exception:
                pass

        return entry


def _entry_freshness(cache_entry: dict, mode: str) -> str:
    """统一的 cache entry 新鲜度判定。

    Args:
        cache_entry: chart_data_cache 条目，含 ``validated_at`` 浮点字段。
        mode: ``"polling"``（30s 阈值，TV polling 路径）或
              ``"first_request"``（3600s 阈值，firstDataRequest=true 路径，
              用于重启后识别停机期间过期的磁盘快照）。

    Returns:
        ``"fresh"`` / ``"stale"`` / ``"unknown"``（缺字段时按 stale 处理）。
    """
    if not isinstance(cache_entry, dict):
        return "unknown"
    validated_at = cache_entry.get("validated_at")
    if not isinstance(validated_at, (int, float)) or validated_at <= 0:
        return "unknown"

    threshold = (
        _CACHE_REVALIDATION_INTERVAL if mode == "polling" else _SNAPSHOT_STALE_AFTER
    )
    return "fresh" if (time.time() - validated_at) < threshold else "stale"


def _cache_entry_recently_validated(cache_entry: dict) -> bool:
    """polling 路径专用: 30s 内验证过即视为有效。委托给 _entry_freshness。"""
    return _entry_freshness(cache_entry, mode="polling") == "fresh"


def _full_snapshot_is_stale(cache_entry: dict) -> bool:
    """全量快照是否过期: validated_at 距今超过 _SNAPSHOT_STALE_AFTER。

    用于 tv_history 在 firstDataRequest=true 路径下校验从磁盘冷层加载的 entry
    时效: 程序停机期间没有 polling 推 validated_at, 重启后第一个请求若不做时效
    校验会直接命中老快照, 导致缺停机期间产生的 K 线。
    None / 非 dict / 缺字段一律视为过期 (保守降级, 触发 cache miss 重新拉取)。

    委托给 _entry_freshness("first_request"); ``unknown`` 也按过期处理。
    """
    return _entry_freshness(cache_entry, mode="first_request") != "fresh"


def evaluate_cache_for_tv_history(
    cache_entry: Optional[dict],
    from_ts: int,
    to_ts: int,
    is_range_request: bool,
) -> tuple:
    """评估 chart_data_cache entry 是否能满足 tv_history 当前请求。

    P5 (2026-05-15): 从 ``tv.py::tv_history`` 内嵌 ``_evaluate_cache`` 闭包提取
    成 module-level 纯函数。原内嵌实现依赖 ``_from``/``_to``/``is_range_request``
    三个 outer var; 提取后通过参数显式传递, 不再隐式依赖 closure 状态, 单测可独立。

    Args:
        cache_entry: chart_data_cache 中的 entry (None 表示 cache miss)
        from_ts: 请求 from 时间戳 (unix 秒, 0/负数表示未指定)
        to_ts: 请求 to 时间戳 (unix 秒)
        is_range_request: 是否窄范围请求 (firstDataRequest=false 且 from/to 都 >0)

    Returns:
        (is_hit, cached_data, miss_reason):
        - is_hit=True: cache 命中, cached_data 为 chart_data dict
        - is_hit=False: cache miss, miss_reason 是字符串原因 ("cache_empty" /
          "cache_partial_snapshot" / "cache_stale_snapshot" / "cache_no_coverage" /
          "cache_head_gap" / "cache_tail_gap")
    """
    if cache_entry is None:
        return False, None, "cache_empty"
    cached_data = cache_entry.get("data", {})
    cache_min_time = cache_entry.get("min_time")
    cache_max_time = cache_entry.get("max_time")
    if not is_range_request:
        if not cache_entry.get("is_full_snapshot", False):
            return False, None, "cache_partial_snapshot"
        # 即便 is_full_snapshot=True, 也要校验时效; 否则程序停机数天后第一个
        # firstDataRequest=true 请求会直接命中过期 snapshot (磁盘冷层),
        # 用户看到的图表缺停机期间产生的 K 线。
        if _full_snapshot_is_stale(cache_entry):
            return False, None, "cache_stale_snapshot"
        return True, cached_data, None
    if cache_min_time is None or cache_max_time is None:
        return False, None, "cache_no_coverage"
    if from_ts < cache_min_time:
        return False, None, "cache_head_gap"
    if to_ts > cache_max_time:
        if _cache_entry_recently_validated(cache_entry):
            return True, cached_data, None
        return False, None, "cache_tail_gap"
    return True, cached_data, None


# ---------------- 写入：RAM + 异步落盘 ----------------

# 磁盘异步写入器（chart_data_cache 落盘）。
#
# 为什么异步：单条 entry pickle 后 ~100-500KB，原子写盘 50-100ms，绝对不能让用户
# tv_history 请求等磁盘 fsync。失败仅记录 error 级日志，不影响 RAM 命中链路。
# 4 worker 足够撑住批量预热（symbols.py 全局 inflight 也才 2-4）+ 用户实时写入。
_CHART_CACHE_DISK_WORKERS = 4
_chart_cache_disk_executor = ThreadPoolExecutor(
    max_workers=_CHART_CACHE_DISK_WORKERS,
    thread_name_prefix="ChartCacheDisk",
)


def _persist_chart_cache_async(cache_key: str, entry: dict) -> None:
    """提交一次磁盘写入；调用方不阻塞。

    deepcopy entry 后再提交, 避免与主线程并发 in-place 修改 ``entry["data"]``
    (例如 tv_history cache hit 路径 lazy 补算 ``apply_higher_macd_to_chart_data``、
    或 ``_merge_chart_data`` 之后某些 prepend 后续操作) 产生
    ``RuntimeError: dictionary changed size during iteration`` 写盘失败。

    调用方(``_set_chart_cache_entry``)在 cache_lock 内调本函数, 此处 deepcopy
    在 cache_lock 保护下做, 期间没有其他线程能改 entry, 拿到的 snapshot 安全。
    成本: chart_data 通常 1KB~几十 KB, deepcopy < 1ms, 主线程同步代价可接受。
    """
    snapshot = copy.deepcopy(entry)
    try:
        _chart_cache_disk_executor.submit(fdb.set_chart_cache, cache_key, snapshot)
    except Exception as e:
        # executor 已关闭 / 队列满等极端场景：直接同步 fallback 写一次，
        # 写失败也只是丢这条，下次预热会重新算。
        LogUtil.warning(
            f"[chart_cache] async submit failed, fallback sync write key={cache_key} err={e}"
        )
        try:
            fdb.set_chart_cache(cache_key, snapshot)
        except Exception as e2:
            LogUtil.error(f"[chart_cache] fallback sync write failed key={cache_key} err={e2}")


def _set_chart_cache_entry(cache_key: str, cl_chart_data: dict, is_full_snapshot: bool):
    """两层缓存写入：RAM 立即可见，磁盘异步持久化。

    本函数自带 ``cache_lock``（可重入 RLock），调用方无需（也可重复）持锁——
    ``deepcopy`` 在锁内做，与 ``_persist_chart_cache_async`` 的 snapshot 不变量一致。
    """
    entry = _build_chart_cache_entry(cl_chart_data, is_full_snapshot=is_full_snapshot)
    with cache_lock:
        chart_data_cache[cache_key] = entry
        _persist_chart_cache_async(cache_key, entry)
    return entry


def _mark_chart_cache_validated(cache_key: str):
    # H4: validated_at 只更新到 entry 内部；entry 本身的 TTL 由 chart_data_cache 统一管理。
    # 若 cache 已被 TTL 淘汰，没有 entry 可标记，直接返回（下次请求自然重算）。
    # 自带 cache_lock（可重入）；_get_chart_cache_entry 同样自锁，嵌套获取安全。
    with cache_lock:
        entry = _get_chart_cache_entry(cache_key)
        if entry is None:
            return
        entry["validated_at"] = time.time()
        chart_data_cache[cache_key] = entry


# ---------------- 负缓存（空数据短期记忆）----------------

# 2026-04 修复：空数据周期的负缓存。
# 问题：ZK.US 这种新上市标的，长桥 1m 接口返回不了那么久的历史 → ex.klines() 返回 []
# → web_batch_get_cl_datas 抛 "输入的K线数据为空" warning → 缓存里永远没有 1m 的 entry
# → 用户每 3 秒 polling 一次都会重新尝试算 1m → 每次又拉空 → 无限重试，浪费 HTTP 配额。
#
# 修复：klines 为空或 cl_chart_data 为空时，把 cache_key 加入负缓存集合，
# 5 分钟内同 cache_key 再来直接 return，不再调 ex.klines()。
# 5 分钟是权衡：太短退化成无效，太长会让"上市新股第一次有 1m 数据"延迟感知。
_NEGATIVE_CACHE_TTL_SECONDS = 300.0
_negative_cache: Dict[str, float] = {}
_negative_cache_lock = threading.Lock()


def _is_negatively_cached(cache_key: str) -> bool:
    """检查 cache_key 是否在负缓存中（最近 5 分钟内被确认无数据）。"""
    now = time.time()
    with _negative_cache_lock:
        ts = _negative_cache.get(cache_key)
        if ts is None:
            return False
        if now - ts > _NEGATIVE_CACHE_TTL_SECONDS:
            _negative_cache.pop(cache_key, None)
            return False
        return True


def _mark_negative_cache(cache_key: str) -> None:
    """标记 cache_key 为"无数据"，5 分钟内不再尝试拉取。"""
    now = time.time()
    with _negative_cache_lock:
        _negative_cache[cache_key] = now
        # 顺便清理过期项（懒清理，避免长期运行时无限增长）
        if len(_negative_cache) > 500:
            cutoff = now - _NEGATIVE_CACHE_TTL_SECONDS
            stale = [k for k, t in _negative_cache.items() if t < cutoff]
            for k in stale:
                _negative_cache.pop(k, None)
