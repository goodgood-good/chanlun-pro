"""web/chanlun_chart/cl_app/services/cl_object_cache.py — US-009 进程内 cl 对象 LRU 缓存。

设计目标:
- 让 web 路径 (tv_history polling, 多周期预热) 避免对同一份 K 线"反复全量算"。
- 同 ``(market, code, frequency, cl_config_hash)`` × 同 K 线 signature → cache hit,
  返回已经算好的 CL 对象, 节省"几百 ms - 几秒"全量计算。

为什么不做"真增量喂入" (这与 ``cl_utils.web_batch_get_cl_datas`` 当前注释里
描述的 master bug 直接相关):
- master 的 xd_calculator 增量路径在长序列下会累积 (US-003 xfail), 导致
  ``cd.xds`` 在多次 process_klines 后比一次性多 1。
- 在 US-009 修好这个 bug 之前, 缓存命中后只对"完全相同的 K 线 signature"复用 cd;
  K 线变化 (新增/末根 OHLC 改变) → 丢弃旧 cd, 新建并跑全量。
- 这样的"假增量"已经能让连续 polling 同一 cache_key 的 N 次请求节省 N-1 次全量,
  仍然是显著收益; 同时不引入新的 xds 累积风险。

API:
- ``get_or_compute_cl(market, code, frequency, cl_config, klines)`` → CL
- ``invalidate(market, code, frequency, cl_config=None)`` → 清单个 key 或前缀
- ``stats()`` → {"hits": int, "misses": int, "size": int}
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

import pandas as pd
from cachetools import LRUCache

if TYPE_CHECKING:
    from chanlun.core.cl import CL  # pragma: no cover


_CACHE_MAX_SIZE = 128


def _hash_cl_config(cl_config: Optional[Dict[str, Any]]) -> str:
    """对 cl_config dict 做稳定 hash, 同一 dict 不同插入顺序 → 同 hash。"""
    if not cl_config:
        return "empty"
    try:
        blob = json.dumps(cl_config, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        # 兜底: dict 含不可 JSON 序列化的 value, 退化为 str()
        blob = repr(sorted(cl_config.items()))
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


def _compute_kline_signature(klines: pd.DataFrame) -> Tuple[Any, ...]:
    """K 线序列的轻量 signature。

    9 元组结构 (设计兼顾"末段变化检测" + "中段复权检测"):
      (len, last_date, last_close,
       mid_date, mid_o, mid_h, mid_l, mid_c)

    选这些字段的理由:
    - **末段 3 元组** (len/last_date/last_close):
      polling 99% 场景是"追加几根 K 线" → len + last_date 必变;
      实时 tick "末根 close 更新" → close 变。三者覆盖增量主路径。
    - **中段 ref-bar 5 元组** (mid_date + OHLC):
      复权事件是"全部历史 bar 等比例缩放", 仅靠末根可能漏检 (复权后整段
      重缩放, 但同一根 K 线的相对位置不变, 末根 date/close 量级一致 →
      末段 signature 与旧值仍接近)。参考 file_db.py:540-578 的复权检测,
      取 ``-max(10, min(len // 4, 100))`` 处的稳定中段 bar 做 OHLC 指纹,
      任何 OHLC 字段变化都会让 signature 失配 → 强制重算。
    - 计算 O(1), 不像全表 md5 O(N); 中段 ref-bar 索引也是 O(1)。
    """
    if klines is None or klines.empty:
        return (0, "", 0.0, "", 0.0, 0.0, 0.0, 0.0)

    n = len(klines)
    last_row = klines.iloc[-1]
    last_date = last_row.get("date")
    last_close = float(last_row.get("close", 0.0))

    # 中段 ref-bar: 仅在 K 线足够长时才取 (太短的序列复权概率低且 ref 位置不稳定)
    if n >= 12:
        # 与 file_db.get_web_cl_data:550-553 同款"稳定中段"位置
        ref_idx = n - max(10, min(n // 4, 100))
        mid_row = klines.iloc[ref_idx]
        mid_date = mid_row.get("date")
        return (
            n,
            str(last_date) if last_date is not None else "",
            last_close,
            str(mid_date) if mid_date is not None else "",
            float(mid_row.get("open", 0.0)),
            float(mid_row.get("high", 0.0)),
            float(mid_row.get("low", 0.0)),
            float(mid_row.get("close", 0.0)),
        )
    # 短序列: 中段字段填 0 (与上面 empty 分支同形, 保证 tuple 长度一致便于比较)
    return (n, str(last_date) if last_date is not None else "", last_close, "", 0.0, 0.0, 0.0, 0.0)


@dataclass
class _CacheEntry:
    cd: "CL"
    signature: Tuple[Any, ...]


# LRUCache 本身非完全 thread-safe (read 是 atomic, set/pop 需要锁), 用 RLock 兜底
_cl_object_cache: "LRUCache[str, _CacheEntry]" = LRUCache(maxsize=_CACHE_MAX_SIZE)
_cache_lock = threading.RLock()
_stats = {"hits": 0, "misses": 0}


def _build_cache_key(
    market: str, code: str, frequency: str, cl_config: Optional[Dict[str, Any]]
) -> str:
    return f"{market}|{code}|{frequency}|{_hash_cl_config(cl_config)}"


def get_or_compute_cl(
    market: str,
    code: str,
    frequency: str,
    cl_config: Optional[Dict[str, Any]],
    klines: pd.DataFrame,
):
    """获取或计算 CL 对象。

    流程:
    1. key = (market, code, frequency, cl_config_hash)
    2. signature = (len, last_date, last_close)
    3. cache hit + signature 相同 → 直接返回 entry.cd (主要优化点)
    4. signature 不同 (或 cache miss) → 新建 CL + process_klines(full) + 存

    线程安全: 通过 _cache_lock 串行化 set/pop, 但 process_klines 在锁外执行
    避免长时间阻塞其它 key 的并发请求。

    ⚠️ 返回值使用约束 (architect review R1):
    cache hit 时返回的 CL 实例是**共享对象**, 多个并发请求会拿到同一引用。
    调用方**绝不能**:
    - 对返回的 cd 再调用 ``process_klines(...)`` 做增量喂入 (会污染其它请求看到的状态)
    - 修改 cd 内部 list/dict (例如 cd.xds.append(...), cd.bi_calculator.bis.clear())

    调用方**可以**:
    - 读 cd.get_klines() / get_fxs() / get_bis() / get_xds() / ... (这些方法返回 list 浅拷贝)
    - 把 cd 传给 cl_data_to_tv_chart 等纯计算函数 (纯读)

    如有"我要往这个 cd 里追加 K 线"的需求, 应当 invalidate(key) 后再调本方法重建。
    """
    from chanlun.core.cl import CL  # 局部 import 避免循环

    key = _build_cache_key(market, code, frequency, cl_config)
    sig = _compute_kline_signature(klines)

    # 1) read 路径: 在锁内检查 cache hit
    with _cache_lock:
        entry = _cl_object_cache.get(key)
        if entry is not None and entry.signature == sig:
            _stats["hits"] += 1
            return entry.cd
        _stats["misses"] += 1

    # 2) miss/stale 路径: 锁外算新 cd (允许其它 key 并发)
    cd = CL(code, frequency, dict(cl_config) if cl_config else {})
    cd.process_klines(klines)

    # 3) write 回 cache (锁内)
    with _cache_lock:
        _cl_object_cache[key] = _CacheEntry(cd=cd, signature=sig)

    return cd


def invalidate(
    market: str,
    code: str,
    frequency: str,
    cl_config: Optional[Dict[str, Any]] = None,
) -> int:
    """清除单个 key (cl_config 给定) 或前缀匹配 (cl_config=None 清所有 config)。

    Returns:
        被清掉的 entry 数量。
    """
    with _cache_lock:
        if cl_config is not None:
            key = _build_cache_key(market, code, frequency, cl_config)
            return 1 if _cl_object_cache.pop(key, None) is not None else 0
        # 前缀清除: 同 market/code/freq 不同 cl_config
        prefix = f"{market}|{code}|{frequency}|"
        keys_to_drop = [k for k in list(_cl_object_cache.keys()) if k.startswith(prefix)]
        for k in keys_to_drop:
            _cl_object_cache.pop(k, None)
        return len(keys_to_drop)


def clear_all() -> None:
    """清空整个缓存 (主要给测试用)。"""
    with _cache_lock:
        _cl_object_cache.clear()
        _stats["hits"] = 0
        _stats["misses"] = 0


def stats() -> Dict[str, int]:
    with _cache_lock:
        return {
            "hits": _stats["hits"],
            "misses": _stats["misses"],
            "size": len(_cl_object_cache),
        }
