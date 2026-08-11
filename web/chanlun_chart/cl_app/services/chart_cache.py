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
import os
import random
import threading
import time
from threading import RLock
from typing import Dict, Optional

from cachetools import TTLCache

from chanlun.persistence.file_db import fdb
from chanlun.tools.daemon_executor import DaemonExecutor
from chanlun.tools.cache_identity import source_fingerprint
from chanlun.tools.log_util import LogUtil

# ---------------- 状态 ----------------

# 图表数据计算结果缓存（RAM 热层）。
# RAM 仅做热点加速，持久化由 fdb.set/get_chart_cache 兜底（RAM 淘汰后磁盘仍可命中）。
#
# 单条 1m 图表的 JSON 往往超过 1MB，而 Python 容器实际占用更高。只按 512 个
# key 限制会让热层轻易膨胀到数 GB，因此这里同时用“估算字节权重”和“最小条目
# 权重”约束总内存与最大条目数。淘汰只影响 RAM，磁盘冷层仍可恢复。


def _positive_env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


_CHART_CACHE_MAX_BYTES = max(
    16 * 1024 * 1024,
    _positive_env_int("CHANLUN_CHART_CACHE_MAX_BYTES", 256 * 1024 * 1024),
)
_CHART_CACHE_MAX_ENTRIES = _positive_env_int(
    "CHANLUN_CHART_CACHE_MAX_ENTRIES",
    512,
)
_CHART_CACHE_MEMORY_FACTOR = _positive_env_int(
    "CHANLUN_CHART_CACHE_MEMORY_FACTOR",
    3,
)
_CHART_CACHE_MIN_ENTRY_WEIGHT = max(
    1,
    _CHART_CACHE_MAX_BYTES // _CHART_CACHE_MAX_ENTRIES,
)


def _chart_cache_entry_weight(value: object) -> int:
    """Estimate resident weight from compact UTF-8 JSON, conservatively scaled."""

    try:
        serialized_bytes = len(
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        serialized_bytes = len(repr(value).encode("utf-8", errors="replace"))
    return max(
        _CHART_CACHE_MIN_ENTRY_WEIGHT,
        serialized_bytes * _CHART_CACHE_MEMORY_FACTOR,
    )


class _WeightedTTLCache(TTLCache):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.capacity_evictions = 0

    def popitem(self):
        item = super().popitem()
        self.capacity_evictions += 1
        return item


chart_data_cache: TTLCache = _WeightedTTLCache(
    maxsize=_CHART_CACHE_MAX_BYTES,
    ttl=3600,
    getsizeof=_chart_cache_entry_weight,
)

cache_lock: RLock = RLock()


def chart_cache_metrics() -> dict[str, int]:
    """Return bounded RAM-cache capacity metrics without exposing payloads."""

    with cache_lock:
        entry_count = len(chart_data_cache)
        return {
            "entries": entry_count,
            "estimated_bytes": int(chart_data_cache.currsize),
            "max_bytes": int(chart_data_cache.maxsize),
            "max_entries": _CHART_CACHE_MAX_ENTRIES,
            "capacity_evictions": int(
                getattr(chart_data_cache, "capacity_evictions", 0)
            ),
        }


def _put_chart_cache_ram(cache_key: str, entry: dict) -> bool:
    """Best-effort RAM insert; oversized entries remain available on disk."""

    try:
        chart_data_cache[cache_key] = entry
        return True
    except ValueError:
        LogUtil.warning(
            f"[chart_cache] RAM entry exceeds byte budget, skip key={cache_key}"
        )
        return False

# 缓存数据最近验证时间戳（防止非交易时段 DataPulse 反复 cache miss）
# H4: 验证时间戳直接放在 chart_data_cache 的 entry["validated_at"] 中，
# 不再单独维护 chart_data_validated_at TTLCache。
_CACHE_REVALIDATION_INTERVAL = 30  # 秒，缓存在此时间内被验证过则视为有效

# firstDataRequest=true 路径下 is_full_snapshot 快照的过期阈值 (远大于 polling 30s,
# 远小于"停机数天"; 重启后磁盘冷层旧 entry 能识别为过期, 强制 cache miss 拉新数据)。
_SNAPSHOT_STALE_AFTER = 3600  # 秒


def _cfg_int(name: str, default: int) -> int:
    """从 chanlun.config 读 int 配置, 缺失/异常回退 default(不让坏 config 炸 import)。"""
    try:
        from chanlun import config
        return int(getattr(config, name, default))
    except Exception:
        return default


# 方向2: firstDataRequest 全量快照的过期阈值, 按交易时段区分。
# - 盘中 (market_now_trading=True): 数据每根 K 线在变, 用短阈值 → 尽快后台刷新到最新
#   (serve-stale 已先秒显, 刷新在后台; 短阈值只是让缓存更快追平实时)。
# - 收盘/非交易时段: 数据静止, 用长阈值 → 避免对不变数据反复重算 (省 QMT/CPU)。
# 两个阈值都只决定"是否派后台刷新", 任一情况都先返回旧快照秒显, 绝不阻塞用户 (方向1)。
_SNAPSHOT_STALE_AFTER_TRADING = _cfg_int("CHART_SNAPSHOT_STALE_AFTER_TRADING", 300)
_SNAPSHOT_STALE_AFTER_CLOSED = _cfg_int("CHART_SNAPSHOT_STALE_AFTER_CLOSED", 3600)

# serve-stale 过期上限：超过上限后同步重算。
# 根因(2026-06-29): 上面两个阈值只决定"是否 serve-stale", serve-stale 本身**无上限**——
# 非活跃周期的缓存(1m 隔午休/隔夜、30m 隔日)会停更几小时~几天, firstDataRequest 仍把那份
# 旧快照原样返回, 其"未完成笔"是几小时/几天前的(用户报"未完成笔滞后/该实仍虚")。
# serve-stale 只在"只差几根 bar"时有意义(秒显近似正确、悄悄自愈); 差到几小时/几天时
# 必须回退阻塞重算保证新鲜。窗口语义:
#   age < STALE_AFTER          → fresh    (直接返回, 不刷新)
#   STALE_AFTER ≤ age < MAX    → serve_stale (秒显旧 + 后台刷新)
#   age ≥ MAX (或 validated_at 未知) → too_stale (MISS → 阻塞重算, 必新鲜)
# 盘中 MAX 取 30min: 既保留"差几~30min"的 serve-stale 秒显, 又拦住隔午休/隔夜的重度过期;
# 收盘 MAX 取 1 天: 收盘数据静止、serve-stale 无滞后, 仅拦"隔多日缺整段交易日"的缓存。
_SNAPSHOT_SERVE_STALE_MAX_TRADING = _cfg_int("CHART_SERVE_STALE_MAX_TRADING", 1800)
_SNAPSHOT_SERVE_STALE_MAX_CLOSED = _cfg_int("CHART_SERVE_STALE_MAX_CLOSED", 86400)


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


def _build_cache_key(market: str, code: str, frequency: str, cl_config: dict) -> str:
    """统一构造 chart_data_cache 的 key,确保所有调用方一致。

    源码指纹覆盖当前计算与序列化实现，字段或语义变化都会自然生成
    新 key；无需维护第二套手工版本号。
    """
    return (f"{source_fingerprint()}_{market}_{code}_{frequency}"
            f"_{_stable_hash(cl_config)}")


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
    """Validate the sole production chart-cache entry schema."""
    required = {"data", "min_time", "max_time", "validated_at", "is_full_snapshot"}
    if not isinstance(cached, dict) or set(cached) != required:
        return None
    if not isinstance(cached["data"], dict):
        return None
    if not isinstance(cached["validated_at"], (int, float)):
        return None
    if type(cached["is_full_snapshot"]) is not bool:
        return None
    return cached


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
    # RAM 命中(锁内快查, 不含磁盘 IO)
    with cache_lock:
        entry = _normalize_cache_entry(chart_data_cache.get(cache_key))
        if entry is not None:
            return entry

    # HIGH-2: RAM miss → 磁盘读移出 cache_lock。pickle-load(数百KB~MB)绝不在全局锁内做,
    # 否则一次冷读把 cache_lock 持有到 IO 结束 → 串行化所有 tv_history/写/SSE, 且 IOLoop
    # 线程会被磁盘 IO 卡住整个 Tornado。注意: 若调用方自己已持 cache_lock(如 tv_history
    # 主路径), RLock 重入下本段仍在其锁内(无 perf 收益但功能正确); 不持锁的调用方
    # (prepend 等)则真正锁外读盘。IOLoop 路径改用 _get_chart_cache_entry_ram_only(只读 RAM)。
    try:
        disk_entry = fdb.get_chart_cache(cache_key)
    except Exception as e:
        LogUtil.warning(f"[chart_cache] disk read failed key={cache_key} err={e}")
        disk_entry = None
    entry = _normalize_cache_entry(disk_entry)
    if entry is None:
        return None

    # 回填 RAM(CAS): 锁外读盘期间别的线程可能已写入更新值(SSE recompute / 用户重算),
    # 优先用已有的, 不用旧磁盘值覆盖更新的内存值。
    with cache_lock:
        existing = _normalize_cache_entry(chart_data_cache.get(cache_key))
        if existing is not None:
            return existing
        _put_chart_cache_ram(cache_key, entry)

    # 机会型清理(磁盘 IO)也移出锁。
    if random.randint(0, 2000) <= 1:
        try:
            fdb.maybe_cleanup_chart_cache()
        except Exception:
            pass

    return entry


def _get_chart_cache_entry_ram_only(cache_key: str):
    """只读 RAM 热层(绝不触磁盘)。供 IOLoop 线程(SSE _send_current_snapshot)调用——
    IOLoop 线程同步 pickle-load 磁盘会卡住整个 Tornado(所有 SSE 客户端, HIGH-2)。
    RAM miss 返回 None(调用方跳过补发, 由 firstDataRequest / 轮询兜底)。"""
    with cache_lock:
        return _normalize_cache_entry(chart_data_cache.get(cache_key))


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


def _first_request_freshness(
    cache_entry: dict, market_is_trading: bool, now: float = None
) -> str:
    """firstDataRequest 路径: 全量快照的新鲜度分档。

    返回三态之一(见上方常量块的窗口语义):
    - ``"fresh"``: 足够新鲜, 直接返回, 不必后台刷新;
    - ``"serve_stale"``: 小幅过期, 秒显旧快照 + 派后台重验证(方向1);
    - ``"too_stale"``: 重度过期(超 ``_SNAPSHOT_SERVE_STALE_MAX_*`` 上限, 或
      validated_at 缺失/<=0、时效无法验证), 不能 serve-stale(会把几小时/几天前
      的旧未完成笔发给前端), 交由调用方走 MISS-阻塞重算保证新鲜。

    阈值按交易时段区分(方向2): 盘中短/收盘长。重度过期上限同样按时段区分:
    盘中拦截隔午休/隔夜, 收盘拦截隔多日缺整段交易日。

    Args:
        cache_entry: chart_data_cache 条目(含 ``validated_at``)。
        market_is_trading: 当前该 market 是否处交易时段(由调用方算好传入,
            保持本函数纯净可测)。
        now: 当前时间戳(秒); None 时取 ``time.time()``(单测可注入固定时钟)。
    """
    validated_at = cache_entry.get("validated_at")
    if not isinstance(validated_at, (int, float)) or validated_at <= 0:
        # 时效无法验证：保守走同步重算。
        return "too_stale"
    now = time.time() if now is None else now
    age = now - validated_at
    if market_is_trading:
        soft, hard = _SNAPSHOT_STALE_AFTER_TRADING, _SNAPSHOT_SERVE_STALE_MAX_TRADING
    else:
        soft, hard = _SNAPSHOT_STALE_AFTER_CLOSED, _SNAPSHOT_SERVE_STALE_MAX_CLOSED
    if age < soft:
        return "fresh"
    if age < hard:
        return "serve_stale"
    return "too_stale"


def evaluate_cache_for_tv_history(
    cache_entry: Optional[dict],
    from_ts: int,
    to_ts: int,
    is_range_request: bool,
    *,
    market_is_trading: bool = True,
    now: float = None,
    force_refresh: bool = False,
) -> tuple:
    """评估 chart_data_cache entry 是否能满足 tv_history 当前请求。

    P5 (2026-05-15): 从 ``tv.py::tv_history`` 内嵌 ``_evaluate_cache`` 闭包提取
    成 module-level 纯函数。原内嵌实现依赖 ``_from``/``_to``/``is_range_request``
    三个 outer var; 提取后通过参数显式传递, 不再隐式依赖 closure 状态, 单测可独立。

    2026-06-27 (方向1+2): firstDataRequest 全量快照小幅过期不再 MISS-阻塞重算, 改为
    serve-stale(立即返回旧快照秒显)+ ``needs_refresh=True`` 让调用方派后台重验证。
    过期阈值按交易时段区分(``market_is_trading``)。range(polling)路径不变。

    2026-06-29 (过期上限): serve-stale 加幅度上限。重度过期(超
    ``_SNAPSHOT_SERVE_STALE_MAX_*``, 或 validated_at 未知)回退 MISS-阻塞重算 ——
    否则非活跃周期停更几小时/几天的旧快照会被原样返回, 其"未完成笔"是几小时/几天前的
    (用户报"未完成笔滞后/该实仍虚")。见 ``_first_request_freshness`` 三态语义。

    Args:
        cache_entry: chart_data_cache 中的 entry (None 表示 cache miss)
        from_ts: 请求 from 时间戳 (unix 秒, 0/负数表示未指定)
        to_ts: 请求 to 时间戳 (unix 秒)
        is_range_request: 是否窄范围请求 (firstDataRequest=false 且 from/to 都 >0)
        market_is_trading: 当前该 market 是否处交易时段(决定 serve-stale 的过期阈值)。
        now: 当前时间戳(秒); None 取 ``time.time()`` (单测可注入)。仅作用于
            firstDataRequest 过期判定; range 路径的 recently_validated 仍用真实时钟。

    Returns:
        (is_hit, cached_data, miss_reason, needs_refresh):
        - is_hit=True: 命中, cached_data 为 chart_data dict 可直接返回前端;
          needs_refresh=True 表示这是"过期快照即时返回", 调用方应派后台重验证。
        - is_hit=False: cache miss, miss_reason 是字符串原因 ("cache_empty" /
          "cache_partial_snapshot" / "cache_stale_snapshot"(重度过期回退阻塞重算)/
          "cache_no_coverage" / "cache_head_gap" / "cache_tail_gap"); needs_refresh 恒 False。
    """
    # H1(阶段E): 前端断档 gap-reset 主动要求绕过缓存重算 —— 无条件 MISS,让调用方重拉+重算。
    # 绕过而非删除缓存:重算失败时旧 entry 仍在(下次正常请求仍可 serve),符合 C1"绝不丢好缓存"。
    if cache_entry is None:
        return False, None, "cache_empty", False
    # H1(阶段E,F-2):force_refresh 无条件 MISS,放在 cache_entry is None 之后——空缓存仍报
    # cache_empty(语义更准),有缓存才报 cache_force_refresh。绕过而非删缓存(符合 C1"绝不丢好缓存")。
    if force_refresh:
        return False, None, "cache_force_refresh", False
    cached_data = cache_entry.get("data", {})
    cache_min_time = cache_entry.get("min_time")
    cache_max_time = cache_entry.get("max_time")
    if not is_range_request:
        if not cache_entry.get("is_full_snapshot", False):
            # 部分快照(可能只有几根 K 线)不能冒充全量返回, 仍走同步 MISS 重算。
            return False, None, "cache_partial_snapshot", False
        # 小幅过期可先展示并后台校验；重度过期或时效不明则同步重算。
        freshness = _first_request_freshness(cache_entry, market_is_trading, now)
        if freshness == "too_stale":
            return False, None, "cache_stale_snapshot", False
        if freshness == "serve_stale":
            return True, cached_data, None, True
        return True, cached_data, None, False
    if cache_min_time is None or cache_max_time is None:
        return False, None, "cache_no_coverage", False
    if from_ts < cache_min_time:
        return False, None, "cache_head_gap", False
    if to_ts > cache_max_time:
        if _cache_entry_recently_validated(cache_entry):
            return True, cached_data, None, False
        return False, None, "cache_tail_gap", False
    return True, cached_data, None, False


# ---------------- 写入：RAM + 异步落盘 ----------------

# 磁盘异步写入器（chart_data_cache 落盘）。
#
# 为什么异步：单条 entry pickle 后 ~100-500KB，原子写盘 50-100ms，绝对不能让用户
# tv_history 请求等磁盘 fsync。失败仅记录 error 级日志，不影响 RAM 命中链路。
# 4 worker 足够撑住批量预热（symbols.py 全局 inflight 也才 2-4）+ 用户实时写入。
_CHART_CACHE_DISK_WORKERS = 4
_CHART_CACHE_MAX_PENDING_WRITES = 64
_chart_cache_disk_lock = threading.Lock()
_chart_cache_disk_closed = True
_chart_cache_accepting_writes = True
_chart_cache_disk_futures = set()
_chart_cache_disk_future_keys = {}
_chart_cache_disk_slots = threading.BoundedSemaphore(_CHART_CACHE_MAX_PENDING_WRITES)
_chart_cache_disk_executor = None


def _persist_chart_cache_async(cache_key: str, entry: dict) -> None:
    """Submit a best-effort write with a strict pending-task capacity."""
    snapshot = copy.deepcopy(entry)
    with _chart_cache_disk_lock:
        global _chart_cache_disk_closed, _chart_cache_disk_executor
        if _chart_cache_disk_closed:
            if not _chart_cache_accepting_writes:
                return
            _chart_cache_disk_executor = DaemonExecutor(
                max_workers=_CHART_CACHE_DISK_WORKERS,
                thread_name_prefix="ChartCacheDisk",
            )
            _chart_cache_disk_closed = False
        if _chart_cache_disk_closed:
            return
        executor = _chart_cache_disk_executor
        slots = _chart_cache_disk_slots
        if not slots.acquire(blocking=False):
            LogUtil.warning(
                f"[chart_cache] pending write limit reached, skip key={cache_key}"
            )
            return
        try:
            future = executor.submit(fdb.set_chart_cache, cache_key, snapshot)
        except Exception as exc:
            slots.release()
            LogUtil.warning(
                f"[chart_cache] async submit failed key={cache_key} err={exc}"
            )
            return
        _chart_cache_disk_futures.add(future)
        _chart_cache_disk_future_keys[future] = cache_key

    def _completed(done_future):
        try:
            error = None if done_future.cancelled() else done_future.exception()
            if error is not None:
                LogUtil.error(
                    f"[chart_cache] async write failed key={cache_key} err={error}"
                )
        except Exception as exc:
            LogUtil.warning(
                f"[chart_cache] async write completion failed key={cache_key} err={exc}"
            )
        finally:
            with _chart_cache_disk_lock:
                _chart_cache_disk_futures.discard(done_future)
                _chart_cache_disk_future_keys.pop(done_future, None)
            slots.release()

    future.add_done_callback(_completed)


def start_chart_cache_runtime():
    global _chart_cache_disk_closed, _chart_cache_disk_executor
    global _chart_cache_accepting_writes, _chart_cache_disk_slots
    with _chart_cache_disk_lock:
        if not _chart_cache_disk_closed:
            return
        if _chart_cache_disk_futures:
            raise RuntimeError("cannot restart chart cache writer with active writes")
        _chart_cache_disk_executor = DaemonExecutor(
            max_workers=_CHART_CACHE_DISK_WORKERS,
            thread_name_prefix="ChartCacheDisk",
        )
        _chart_cache_disk_slots = threading.BoundedSemaphore(
            _CHART_CACHE_MAX_PENDING_WRITES
        )
        _chart_cache_accepting_writes = True
        _chart_cache_disk_closed = False


def allow_lazy_chart_cache_writes():
    """Allow a newly created application to open the writer on first use."""
    global _chart_cache_accepting_writes
    with _chart_cache_disk_lock:
        if _chart_cache_disk_closed and not _chart_cache_disk_futures:
            _chart_cache_accepting_writes = True


def shutdown_chart_cache_runtime(wait=False):
    """Reject new writes and cancel queued disk-cache work."""
    global _chart_cache_accepting_writes, _chart_cache_disk_closed
    global _chart_cache_disk_executor
    with _chart_cache_disk_lock:
        _chart_cache_accepting_writes = False
        if _chart_cache_disk_closed:
            return not _chart_cache_disk_futures
        _chart_cache_disk_closed = True
        executor = _chart_cache_disk_executor
        _chart_cache_disk_executor = None
    if executor is not None:
        executor.shutdown(wait=bool(wait), cancel_futures=True)
    with _chart_cache_disk_lock:
        return not _chart_cache_disk_futures

def _set_chart_cache_entry(cache_key: str, cl_chart_data: dict, is_full_snapshot: bool):
    """两层缓存写入：RAM 立即可见，磁盘异步持久化。

    本函数自带 ``cache_lock``（可重入 RLock），调用方无需（也可重复）持锁——
    ``deepcopy`` 在锁内做，与 ``_persist_chart_cache_async`` 的 snapshot 不变量一致。
    """
    entry = _build_chart_cache_entry(cl_chart_data, is_full_snapshot=is_full_snapshot)
    with cache_lock:
        _put_chart_cache_ram(cache_key, entry)
        _persist_chart_cache_async(cache_key, entry)
    return entry


def _delete_chart_cache_entry(cache_key: str) -> None:
    """Invalidate one RAM/disk entry after its price basis becomes unsafe."""

    with cache_lock:
        chart_data_cache.pop(cache_key, None)

    # A previous _set may still be writing this key asynchronously. Wait only
    # for those rare same-key writes before deleting, otherwise a late writer
    # could resurrect the unsafe snapshot after the unlink.
    with _chart_cache_disk_lock:
        pending = [
            future
            for future in _chart_cache_disk_futures
            if _chart_cache_disk_future_keys.get(future) == cache_key
        ]
    for future in pending:
        try:
            future.result()
        except Exception as exc:
            LogUtil.warning(
                f"[chart_cache] pending write failed before delete "
                f"key={cache_key} error={type(exc).__name__}: {exc}"
            )
    try:
        fdb.delete_chart_cache(cache_key)
    except Exception as exc:
        LogUtil.warning(
            f"[chart_cache] disk delete failed key={cache_key} "
            f"error={type(exc).__name__}: {exc}"
        )


def _mark_chart_cache_validated(cache_key: str):
    # H4: validated_at 只更新到 entry 内部；entry 本身的 TTL 由 chart_data_cache 统一管理。
    # 若 cache 已被 TTL 淘汰，没有 entry 可标记，直接返回（下次请求自然重算）。
    # 自带 cache_lock（可重入）；_get_chart_cache_entry 同样自锁，嵌套获取安全。
    with cache_lock:
        entry = _get_chart_cache_entry(cache_key)
        if entry is None:
            return
        entry["validated_at"] = time.time()
        _put_chart_cache_ram(cache_key, entry)


# ---------------- 负缓存（空数据短期记忆）----------------

# 2026-04 修复：空数据周期的负缓存。
# 问题：ZK.US 这种新上市标的，长桥 1m 接口返回不了那么久的历史 → ex.klines() 返回 []
# → 严格图表运行时无法构建快照，缓存里永远没有 1m 的 entry
# → 用户每 3 秒 polling 一次都会重新尝试算 1m → 每次又拉空 → 无限重试，浪费 HTTP 配额。
#
# 修复：klines 为空或 cl_chart_data 为空时，把 cache_key 加入负缓存集合，
# 5 分钟内同 cache_key 再来直接 return，不再调 ex.klines()。
# 5 分钟是权衡：太短退化成无效，太长会让"上市新股第一次有 1m 数据"延迟感知。
_NEGATIVE_CACHE_TTL_SECONDS = 300.0
# 异常空(数据源暂时不可用,如 cq 分段拉取失败返回 attrs['fetch_incomplete'])用短退避,与
# "真空(新股/退市,真没数据)"的 300s 区分:真空 5min 抑制防无限重拉;异常空 30s 快速自愈,不被
# 5min 抑制卡住恢复(C1+M3 协同,审查 M3)。
_TRANSIENT_NEGATIVE_TTL_SECONDS = 30.0
# value = (mark_ts, ttl)：每项按各自 ttl 独立失效,支持真空/异常空并存于同一表。
_NEGATIVE_CACHE_MAX_SIZE = 500
_negative_cache: Dict[str, tuple] = {}
_negative_cache_lock = threading.Lock()


def _is_negatively_cached(cache_key: str) -> bool:
    """检查 cache_key 是否在负缓存中（未超各自 ttl 内被确认无数据/暂不可用）。"""
    now = time.time()
    with _negative_cache_lock:
        item = _negative_cache.get(cache_key)
        if item is None:
            return False
        mark_ts, ttl = item
        if now - mark_ts > ttl:
            _negative_cache.pop(cache_key, None)
            return False
        return True


def _mark_negative_cache(cache_key: str, ttl: float = _NEGATIVE_CACHE_TTL_SECONDS) -> None:
    """Record a negative result while enforcing a strict oldest-first capacity."""
    now = time.time()
    with _negative_cache_lock:
        # Reinsert existing keys so dict insertion order reflects the latest mark.
        _negative_cache.pop(cache_key, None)
        _negative_cache[cache_key] = (now, ttl)
        stale = [
            key
            for key, (marked_at, item_ttl) in _negative_cache.items()
            if now - marked_at > item_ttl
        ]
        for key in stale:
            _negative_cache.pop(key, None)
        limit = max(1, int(_NEGATIVE_CACHE_MAX_SIZE))
        while len(_negative_cache) > limit:
            oldest = next(iter(_negative_cache))
            _negative_cache.pop(oldest, None)

def _klines_fetch_incomplete(klines) -> bool:
    """cq 源级完整性闸门信号：拉取带洞时返回空 DataFrame 且 attrs['fetch_incomplete']=True(C1)。

    web 层据此走短退避(_TRANSIENT_NEGATIVE_TTL_SECONDS)而非真空 5min 负缓存,避免数据源暂时失败
    被当真空抑制 5min 不自愈(M3)。回测/monitor 不查此标记,klines 契约不变。
    """
    attrs = getattr(klines, "attrs", None)
    return bool(attrs) and attrs.get("fetch_incomplete") is True
