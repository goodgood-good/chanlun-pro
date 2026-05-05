"""图表数据缓存层（service）。

L1 Phase 2 重构：把 tv.py 里 chart cache 相关的状态 + 纯函数集中迁出，
让 symbols.py 不再越界 import tv 的下划线"私有"符号；tv.py 内部仍保留
``_set_chart_cache_entry`` / ``_mark_chart_cache_validated`` / 异步落盘 executor
等业务函数，它们也通过本模块的公开符号访问数据。

状态：
- ``chart_data_cache``: RAM 热层缓存（TTLCache）；与 fdb.get_chart_cache 的磁盘冷层
  组成两层缓存（RAM miss → disk → 回填 RAM）。
- ``cache_lock``: RLock；多线程读写 chart_data_cache 时的粗粒度互斥。
- ``_CACHE_REVALIDATION_INTERVAL``: 30s，缓存在此时间内被验证过则视为有效。

工具函数：
- ``_stable_hash`` / ``_build_cache_key``: 跨进程稳定的 cache_key 构造
- ``_build_chart_cache_entry`` / ``_normalize_cache_entry``: entry 字段规范化
- ``_get_chart_cache_entry``: 两层读取（RAM → disk → warm RAM）
- ``_cache_entry_recently_validated``: 验证时间戳判断
"""
import hashlib
import json
import random
import time
from threading import RLock
from typing import Optional

from cachetools import TTLCache

from chanlun.file_db import fdb
from chanlun.tools.log_util import LogUtil

# ---------------- 状态 ----------------

# 图表数据计算结果缓存（RAM 热层）。
#
# 2026-04 重构：把 chart_data_cache 从「单层 RAM」改成「RAM 热层 + 磁盘冷层」。
# - 旧设计 maxsize=100 / ttl=600 仅适合"短时切周期防抖"场景；
# - 全市场预热（11755 标的 × 4 周期 ≈ 47k entry）时，RAM 灌不下，TTL 也撑不住，
#   预热刚算完就被淘汰，用户切标的依然要等待重新计算。
# 现在 RAM 仅做热点加速，maxsize/ttl 适当上调；持久化由 fdb.set/get_chart_cache 兜底。
chart_data_cache: TTLCache = TTLCache(maxsize=512, ttl=3600)

cache_lock: RLock = RLock()

# 缓存数据最近验证时间戳（防止非交易时段 DataPulse 反复 cache miss）
# H4: 验证时间戳直接放在 chart_data_cache 的 entry["validated_at"] 中，
# 不再单独维护 chart_data_validated_at TTLCache。
_CACHE_REVALIDATION_INTERVAL = 30  # 秒，缓存在此时间内被验证过则视为有效


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
    """统一构造 chart_data_cache 的 key，确保所有调用方一致。"""
    return f"{market}_{code}_{frequency}_{_stable_hash(cl_config)}"


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
    """
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


def _cache_entry_recently_validated(cache_entry: dict) -> bool:
    validated_at = cache_entry.get("validated_at", 0) if isinstance(cache_entry, dict) else 0
    return (time.time() - validated_at) < _CACHE_REVALIDATION_INTERVAL
