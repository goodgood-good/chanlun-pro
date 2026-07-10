"""进程内 CL 对象 LRU 缓存，避免对同一份 K 线反复全量计算。

三种路径（按耗时从低到高）：
1. cache hit + 同 signature → 直接返回 cached cd（0 cost）
2. cache hit + 末段追加 signature → 复用旧 cd，process_klines 走内部增量，省 80%+ 时间
3. cache miss / 复权 / 缩短 → 新建 cd 全量计算

增量路径安全前提：xd_calculator 增量路径与全量路径等价；同 key 并发用 _key_locks 串行化。

API：
- ``get_or_compute_cl(market, code, frequency, cl_config, klines)`` → CL
- ``invalidate(market, code, frequency, cl_config=None)`` → 清单个 key 或前缀
- ``stats()`` → {hits, misses, incremental_extends, full_rebuilds, size}
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Optional, Tuple

import pandas as pd
from cachetools import LRUCache

from chanlun.tools.cache_version import source_fingerprint

if TYPE_CHECKING:
    from chanlun.core.cl import CL  # pragma: no cover


_CACHE_MAX_SIZE = 128

# signature 中 ref-bar 的绝对位置上界。
# 用绝对位置而非末段相对偏移：追加 K 线时 ref_idx 指向同一根，OHLC 不变，
# 可被 _is_extending_signature 识别为"末段追加"走真增量路径。
_REF_BAR_MAX_IDX = 50


def _hash_cl_config(cl_config: Optional[Dict[str, Any]]) -> str:
    """对 cl_config dict 做稳定 hash, 同一 dict 不同插入顺序 → 同 hash。"""
    if not cl_config:
        return "empty"
    try:
        blob = json.dumps(cl_config, sort_keys=True, default=str, ensure_ascii=False)
    except Exception:
        blob = repr(sorted(cl_config.items()))
    return hashlib.md5(blob.encode("utf-8")).hexdigest()[:16]


def _ref_bar_index(n: int) -> int:
    """返回 ref-bar 的绝对索引 (>= 0, < n)。

    设计: ``ref_idx = min(_REF_BAR_MAX_IDX, max(0, n // 4))``。
    - n=12  → ref_idx=3   (n//4=3)
    - n=50  → ref_idx=12  (n//4=12)
    - n=200 → ref_idx=50  (clamp 到 _REF_BAR_MAX_IDX)
    - n=1000→ ref_idx=50  (同上)

    K 线追加时 ref_idx 落在同一根, 实现"末段追加"识别。
    """
    return min(_REF_BAR_MAX_IDX, max(0, n // 4))


def _hist_fp(klines, upto: int) -> str:
    """除末根外前 upto 根 OHLC 的 md5 指纹, 检测中途历史根被订正/回填。

    与 kline_recompute._klines_prefix_fp 同口径(同类 H1 防护): 采样单根 ref 会漏检
    非采样位置的局部订正, 靠本前缀指纹兜住。upto<=0 返回 "0"; 取指纹失败返回 ""
    (不等于任何真实 md5, 触发保守全量重建)。
    """
    if upto <= 0:
        return "0"
    try:
        cols = [c for c in ("open", "high", "low", "close") if c in klines.columns]
        arr = klines.iloc[:upto][cols].to_numpy(dtype="float64")
        return hashlib.md5(arr.tobytes()).hexdigest()
    except Exception:
        return ""


def _compute_kline_signature(klines: pd.DataFrame) -> Tuple[Any, ...]:
    """K 线序列的轻量 signature (F1 版: ref 用绝对位置)。

    12 元组结构:
      (len, last_date, last_close,
       ref_date, ref_o, ref_h, ref_l, ref_c, hist_fp,
       last_open, last_high, last_low)   # 末根O/H/L(终检MEDIUM-2补:close/date/n不变而末根高低变须失配)

    与 F1 前版 (mid 用末段相对偏移) 的语义差异:
    - F1 后 ref 是 ``_ref_bar_index(n)`` 的绝对位置 K 线
    - 追加 K 线时 ref 不变 → signature 的"末段 3 元组"变、ref 5 元组不变
      → 可被 ``_is_extending_signature`` 识别为真增量
    - 复权时 ref OHLC 改 → signature 完全失配 → 全量重建 (与之前一致)
    - 序列缩短时 ref_idx 变小可能指向不同根 K 线 → 多半失配 → 全量重建 (安全)
    """
    if klines is None or klines.empty:
        return (0, "", 0.0, "", 0.0, 0.0, 0.0, 0.0, "0", 0.0, 0.0, 0.0)

    n = len(klines)
    last_row = klines.iloc[-1]
    last_date = last_row.get("date")
    last_close = float(last_row.get("close", 0.0))
    last_open = float(last_row.get("open", 0.0))
    last_high = float(last_row.get("high", 0.0))
    last_low = float(last_row.get("low", 0.0))
    hist_fp = _hist_fp(klines, n - 1)

    if n >= 12:
        ref_idx = _ref_bar_index(n)
        ref_row = klines.iloc[ref_idx]
        ref_date = ref_row.get("date")
        return (
            n,
            str(last_date) if last_date is not None else "",
            last_close,
            str(ref_date) if ref_date is not None else "",
            float(ref_row.get("open", 0.0)),
            float(ref_row.get("high", 0.0)),
            float(ref_row.get("low", 0.0)),
            float(ref_row.get("close", 0.0)),
            hist_fp,
            last_open,
            last_high,
            last_low,
        )
    return (n, str(last_date) if last_date is not None else "", last_close, "", 0.0, 0.0, 0.0, 0.0, hist_fp, last_open, last_high, last_low)


def _is_extending_signature(old_sig: Tuple[Any, ...], new_sig: Tuple[Any, ...], new_klines) -> bool:
    """判断 new_sig 是否是 old_sig 的"末段追加"。

    条件 (全部满足才算 extending):
    - len 严格增大 (new_sig[0] > old_sig[0])
    - last_date 严格变大 (字符串字典序比较, ISO 格式日期天然单调)
    - hist_fp 一致(整段历史前缀 OHLC 未订正; ref-bar 冗余校验已移除, 见下文 R10-cl_obj_cache63)

    满足 extending → 真增量喂入安全 (历史段不变, 只追加新数据)。
    """
    if len(old_sig) < 9 or len(new_sig) < 9:
        return False
    old_n, old_last_date, _old_last_close = old_sig[0], old_sig[1], old_sig[2]
    new_n, new_last_date, _new_last_close = new_sig[0], new_sig[1], new_sig[2]
    if not isinstance(old_n, int) or not isinstance(new_n, int):
        return False
    if new_n <= old_n:
        return False
    if not old_last_date or not new_last_date or new_last_date <= old_last_date:
        return False
    # ref-bar 5 元组(位置 3-7)校验已移除: _ref_bar_index(n)=min(50,n//4) 随 n 漂移(R10-cl_obj_cache63),
    # 小根数标的追加 K 线跨 n//4 边界时 old/new 采样不同根→假失配退化全量重建; hist_fp(位置8)已完整
    # 覆盖 [0,old_n-1) 历史前缀订正检测(冗余且更健壮), 单靠 len↑+last_date↑+hist_fp 判 extending 即安全。
    # 全前缀指纹一致: old 除末根历史前缀 == new 在 old 长度处重算的前缀(检测中途订正)
    return old_sig[8] == _hist_fp(new_klines, old_sig[0] - 1)


@dataclass
class _CacheEntry:
    cd: "CL"
    signature: Tuple[Any, ...]


# 全局 LRUCache 配 _cache_lock 串行化 set/pop
_cl_object_cache: "LRUCache[str, _CacheEntry]" = LRUCache(maxsize=_CACHE_MAX_SIZE)
_cache_lock = threading.RLock()

# per-key 锁，串行化同 key 的 cd 增量喂入/重建。
# 不复用 _cache_lock：process_klines 可能耗时几百 ms，会阻塞所有其他 key 的读路径。
_key_locks: Dict[str, threading.RLock] = {}
_key_locks_meta_lock = threading.Lock()

_stats = {
    "hits": 0,             # 同 signature 命中
    "misses": 0,           # 未命中或 signature 不同
    "incremental_extends": 0,  # F1 真增量: cache hit + extending signature
    "full_rebuilds": 0,    # 完全新建 cd
}


def _get_key_lock(key: str) -> threading.RLock:
    """获取 per-key 锁 (惰性创建)。"""
    with _key_locks_meta_lock:
        lk = _key_locks.get(key)
        if lk is None:
            lk = threading.RLock()
            _key_locks[key] = lk
        return lk


# chart_data 序列化 schema 版本——每次后端 ``cl_data_to_tv_chart`` 输出结构
# 变化(新增/重命名字段)递增此值,让现有 chart_data_cache / cl_object_cache 旧
# 条目自动失效(key 前缀变了 → 全部 miss),无需手工清缓存。
# v2(2026-05-20):新增 xd_zslx / recursive_levels / interval_nest;
#                bi_zss/xd_zss 增 type / is_expanded / sub_count 字段。
# v3(2026-05-20):mmd/bc 按级别拆分,新增 bi_mmds/xd_mmds/bi_bcs/xd_bcs。
# v4(2026-05-20):3 类买卖点 first-touch 限定(原文 kobo.61.1「必须是第一次」),
#                同一中枢同一类型只挂最早一次 → 3B/3S 数量大幅减少。
# v5(2026-05-22):买卖点层签名纳入未完成笔/段端点,并新增走势类型线段输出。
# v7(2026-07-08):_compute_kline_signature 末根追加 open/high/low(终检MEDIUM-2:末根 high/low 变而 close/date/根数不变时原 signature 相同→path-1 下发陈旧缠论)。
# ⚠ source_fingerprint() 只覆盖 chanlun/core/* + types + tv_chart/chart_config + _lookback,
#   **不含本文件**(审查 F-4/F-9 已核实)。故改 _compute_kline_signature 的"签名计算逻辑"
#   或本 key 的"输出结构"时,必须手动 bump 此版本号——否则旧 CL 对象不失效、留陈旧结果。
_CHART_DATA_SCHEMA_VERSION = "v7"


def _build_cache_key(
    market: str, code: str, frequency: str, cl_config: Optional[Dict[str, Any]]
) -> str:
    # source_fingerprint():核心计算源码一改 → 指纹变 → 旧 CL 对象缓存自动失效(免手动 bump);
    # 原 _CHART_DATA_SCHEMA_VERSION 只在「输出字段结构」变化时手动 bump,管不住「计算逻辑」变化。
    return (f"{_CHART_DATA_SCHEMA_VERSION}|{source_fingerprint()}"
            f"|{market}|{code}|{frequency}|{_hash_cl_config(cl_config)}")


def get_or_compute_cl(
    market: str,
    code: str,
    frequency: str,
    cl_config: Optional[Dict[str, Any]],
    klines: pd.DataFrame,
):
    """获取或计算 CL 对象（含真增量路径）。

    三条路径:
    1. cache hit + 同 signature → 0 cost 返回 cached cd
    2. cache hit + extending signature → 复用 cd, process_klines 走内部增量（增量与全量等价）
    3. miss / 不连续 signature → 新建 cd 全量计算

    线程安全：路径 1 仅持 _cache_lock；路径 2/3 持 per-key 锁串行化。

    ⚠️ 返回的 CL 是 cache 内共享对象，调用方只能读；若需追加 K 线，传入 full klines
    由本函数代为处理增量喂入。
    """
    from chanlun.core.cl import CL  # 局部 import 避免循环

    key = _build_cache_key(market, code, frequency, cl_config)
    sig = _compute_kline_signature(klines)

    # ----- 路径 1: 同 signature, 0 cost cache hit (锁外可达) -----
    with _cache_lock:
        entry = _cl_object_cache.get(key)
        if entry is not None and entry.signature == sig:
            _stats["hits"] += 1
            return entry.cd

    # ----- 锁外: 抢 per-key 锁, 串行化 cd mutation -----
    key_lock = _get_key_lock(key)
    with key_lock:
        # double-check: 同 key 的其它线程可能在我们抢锁期间已建好 cd
        with _cache_lock:
            entry = _cl_object_cache.get(key)
            if entry is not None and entry.signature == sig:
                _stats["hits"] += 1
                return entry.cd

        # 走 miss 路径计数
        _stats["misses"] += 1

        # ----- 路径 2: extending signature → 复用旧 cd, 内部增量 -----
        if entry is not None and _is_extending_signature(entry.signature, sig, klines):
            cd = entry.cd
            try:
                cd.process_klines(klines)
            except Exception:
                # 增量喂入中途抛异常 → cd 处于半 applied 状态, 且它仍挂在
                # _cl_object_cache 里; 后续同 signature 的请求会走路径 1 命中,
                # 把这个坏对象继续返回。逐出该 key, 迫使下次请求走路径 3
                # 新建一个干净 cd (M6)。
                with _cache_lock:
                    _cl_object_cache.pop(key, None)
                raise
            _stats["incremental_extends"] += 1
        else:
            # ----- 路径 3: 全量重建 -----
            cd = CL(code, frequency, dict(cl_config) if cl_config else {}, market=market)
            cd.process_klines(klines)
            _stats["full_rebuilds"] += 1

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
        # web-B3: cache key = f"{VER}|{source_fingerprint()}|{market}|{code}|{freq}|{hash}",
        # prefix 必须带 VER + source_fingerprint 两段——否则 startswith 恒不匹配、退化为 no-op。
        prefix = f"{_CHART_DATA_SCHEMA_VERSION}|{source_fingerprint()}|{market}|{code}|{frequency}|"
        keys_to_drop = [k for k in list(_cl_object_cache.keys()) if k.startswith(prefix)]
        for k in keys_to_drop:
            _cl_object_cache.pop(k, None)
        return len(keys_to_drop)


def clear_all() -> None:
    """清空整个缓存 (主要给测试用)。"""
    with _cache_lock:
        _cl_object_cache.clear()
        for k in list(_stats.keys()):
            _stats[k] = 0
    # 也清掉 per-key 锁 (LRUCache pop 不会通知, 锁的累积不会无限增长但测试期希望干净)
    with _key_locks_meta_lock:
        _key_locks.clear()


def stats() -> Dict[str, int]:
    with _cache_lock:
        return {
            "hits": _stats["hits"],
            "misses": _stats["misses"],
            "incremental_extends": _stats["incremental_extends"],
            "full_rebuilds": _stats["full_rebuilds"],
            "size": len(_cl_object_cache),
        }
