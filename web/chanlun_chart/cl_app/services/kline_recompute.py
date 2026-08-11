# -*- coding: utf-8 -*-
"""K 线反构建 + 合并 + 缠论全量重算 服务。

设计目标:
  把 chart_data_cache 当作 L2(缠论结果层)使用,L1(原始 K 线集合)由
  chart_data 中的 t/o/h/l/c/v 字段反构建。每次范围请求(向左滚动)时:
    1. extract_klines_df_from_chart_data:从 L2 反构建出当前已缓存的 L1 K 线
    2. merge_klines_df:把新拉取的窄范围 K 线合并进去(按 date 去重 + 排序)
    3. recompute_chart_data_from_klines:用合并后的完整 K 线集走 cl.CL.process_klines
       全量重算,生成新的 chart_data dict
  调用方(tv_history)拿到新 chart_data 后,**整体替换** chart_data_cache。

状态:extract/merge 为纯函数;另有进程级持久 CL 实例池(_cl_pool, LRU)承载增量
重算状态——recompute_chart_data_from_klines 传 cache_key 时复用持久 CL, 让盘中
末根刷新/新增根只算增量而非每次全量重建。复用判定见该函数与 _cl_pool 注释。
"""
import hashlib
import threading
import time
from collections import OrderedDict
from typing import Optional

import pandas as pd

from chanlun.exchange.price_basis import (
    PriceBasisMismatchError,
    copy_price_basis_metadata,
    merge_price_basis_metadata,
)
from chanlun.tools.log_util import LogUtil

# 限制并发"全量重算"数量：缠论重算 CPU 密集，多窗口同时全量重算争 CPU/GIL。曾试 1=
# 完全串行, 但日志证明适得其反——一个慢 full(拉取期 52s)会把后续重算堵在队列上
# (实测 wait=39969ms)。回 2: 配合 1m lookback 降到 20 天(full 根数减 2/3、单次更快),
# 2 并发不再互拖。真正治本是减 CPU 负载(lookback/SSE 降频)而非调这个值。
_RECOMPUTE_MAX_CONCURRENCY = 2
_recompute_sem = threading.Semaphore(_RECOMPUTE_MAX_CONCURRENCY)


def extract_klines_df_from_chart_data(chart_data: dict) -> pd.DataFrame:
    """从 chart_data 中的 t/o/h/l/c/v 列反构建 K 线 DataFrame。

    chart_data["t"] 是 unix 秒时间戳(int 列表),与 cl_data_to_tv_chart 输出对齐。
    返回的 DataFrame 列与 ex.klines() 一致:date / open / high / low / close / volume。
    若任一关键列缺失或长度不一致,返回空 DataFrame(调用方据此回退到全量拉取)。

    ``date`` 列必须带 UTC tz——ex.klines() 返回的 ``new_klines["date"]`` 是 tz-aware
    (alpaca 直接 UTC,cq 转 Asia/Shanghai),后续 ``merge_klines_df`` 的 sort_values
    会比较两边;一边 naive 一边 aware 会抛 ``TypeError: Cannot compare tz-naive
    and tz-aware timestamps``。这里直接用 ``utc=True`` 让反构建结果带 UTC tz,
    pandas 跨 tz 比较时会内部对齐到 UTC,不影响排序/去重正确性。
    """
    if not isinstance(chart_data, dict):
        return pd.DataFrame()
    ts = chart_data.get("t") or []
    o = chart_data.get("o") or []
    h = chart_data.get("h") or []
    low_arr = chart_data.get("l") or []
    c = chart_data.get("c") or []
    v = chart_data.get("v") or []
    n = len(ts)
    if n == 0:
        return pd.DataFrame()
    if not (len(o) == len(h) == len(low_arr) == len(c) == len(v) == n):
        # 列长度不一致说明 chart_data 内部不一致（上游 bug 或磁盘半态读取），
        # 静默返回空 DF 让调用方回退到全量，日志留痕便于排查。
        LogUtil.warning(
            f"[kline_recompute] chart_data 列长度不一致, 回退到全量拉取: "
            f"t={n} o={len(o)} h={len(h)} l={len(low_arr)} c={len(c)} v={len(v)}"
        )
        return pd.DataFrame()
    frame = pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True),
        "open": o,
        "high": h,
        "low": low_arr,
        "close": c,
        "volume": v,
    })
    strict_structure = chart_data.get("strict_structure")
    if (
        chart_data.get("strict_structure_mode") == "replace"
        and isinstance(strict_structure, dict)
    ):
        for name in ("structure_price_quantum", "price_basis_revision"):
            value = strict_structure.get(name)
            if value is not None:
                frame.attrs[name] = value
    return frame


def _ensure_tz_aware(df: pd.DataFrame) -> pd.DataFrame:
    """把 ``date`` 列统一规范成 ``datetime64[ns, UTC]``。

    pandas 不允许 naive 和 aware 的 Timestamp 互相比较 / sort,所以两个数据源
    的 ``date`` 列必须 tz 一致才能 concat → drop_duplicates → sort_values。

    更进一步:``pd.concat`` 两个 *不同 tz* 的 ``datetime64`` 列会降级到 ``object``
    dtype(每行变成带各自 tzinfo 的 Python datetime)。下游 ``KlineDataProcessor.
    _preprocess`` 用 ``is_datetime64_any_dtype`` 判断后会走 ``pd.to_datetime``
    fallback,而该调用没传 ``utc=True``,遇到混合 tz 会抛
    ``ValueError: Tz-aware datetime.datetime cannot be converted to datetime64
    unless utc=True``(2026-05-15 在用户 ``EXCHANGE_US='cq'`` 路由 QQQ.US 到长桥
    分支时复现:cached 是 UTC、new 是 Asia/Shanghai)。

    因此这里同时处理两种情形:
      - naive → ``tz_localize("UTC")`` 贴 UTC 标签(``extract_klines_df_from_chart_data``
        与 ``ex.klines()`` 的 epoch 语义一致,无时区偏移)。
      - 非 UTC 的 tz-aware → ``tz_convert("UTC")`` 转换到 UTC(不改变 epoch,
        只是统一时区标签,保证 ``concat`` 后 dtype 仍是 ``datetime64[ns, UTC]``)。
    """
    if df is None or len(df) == 0 or "date" not in df.columns:
        return df
    tz = getattr(df["date"].dt, "tz", None)
    if tz is None:
        df = df.copy()
        df["date"] = df["date"].dt.tz_localize("UTC")
        return df
    if str(tz) != "UTC":
        df = df.copy()
        df["date"] = df["date"].dt.tz_convert("UTC")
    return df


def merge_klines_df(cached: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并两段 K 线 DataFrame,按 date 去重 + 升序排序。

    重叠优先级:new > cached。
      - new 是刚从交易所(ex.klines)拉取的数据,反映该 bar 的最新/最完整状态;
      - cached 那根往往是"分钟刚开始时被算进缓存的进行中 bar"——此刻只有第一笔
        成交,o=h=l=c=开盘价、量极小。若让 cached 赢,这根 bar 会被永久冻结在
        开盘瞬间快照,web 上每根 K 线 OHLC 全塌缩、量只剩第一笔。
      - 对已收盘的 bar,new 与 cached 等值,new 覆盖是幂等的;对进行中的 bar,
        new 覆盖正是实时刷新所需。下游 recompute 本就全量 process_klines,
        不存在"避免末根更新路径"的收益。
    入参为空时直接返回另一边的副本(始终按 date 升序)。
    """
    if cached is None or len(cached) == 0:
        if new is None or len(new) == 0:
            return pd.DataFrame()
        target = _ensure_tz_aware(new).sort_values("date").reset_index(drop=True)
        return copy_price_basis_metadata(new, target)
    if new is None or len(new) == 0:
        target = _ensure_tz_aware(cached).sort_values("date").reset_index(drop=True)
        return copy_price_basis_metadata(cached, target)

    # 两边 tz 状态对齐(各自 naive 则补 UTC 标签),否则 sort_values 会抛
    # "Cannot compare tz-naive and tz-aware timestamps"。
    cached = _ensure_tz_aware(cached)
    new = _ensure_tz_aware(new)

    # new 后到,使其在 drop_duplicates(keep='last') 下覆盖 cached 的同 date 行
    combined = pd.concat([cached, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date"], keep="last")
    combined = combined.sort_values("date").reset_index(drop=True)
    return merge_price_basis_metadata(cached, new, combined)


# ── 持久 CL 实例池(增量重算) ──────────────────────────────────────────
# CL.process_klines 自带增量(对比 K 线数量+末根状态、各持久 calculator identity-LCP),
# 但每次"新建空 CL"会丢弃增量状态、退化成全量重算。按 cache_key 复用持久 CL, 让
# SSE/轮询的末尾刷新只算增量(盘中全量重算 ~1.8s → 末根增量 ~ms, 多标的不再抢爆 CPU)。
# 复用条件保守: config 一致 + 首根 date 不变 + 根数不减(= 末尾追加/末根更新, 前缀稳定);
# 向左滚动(首根变早)/config 变/symbol 切换 → 新建全量, 不踩"前缀变"风险。
# 线程安全: 同 cache_key 由调用方 chart_calc_locks 串行; _cl_pool_lock 仅护池字典本身。
_CL_POOL_MAX = 64
_cl_pool: "OrderedDict[str, dict]" = OrderedDict()
_cl_pool_lock = threading.Lock()




def reset_cl_pool() -> None:
    """清空 CL 池(测试隔离 / 手动失效)。"""
    with _cl_pool_lock:
        _cl_pool.clear()


def _klines_prefix_fp(klines, upto: int) -> str:
    """对 klines 前 ``upto`` 根的 OHLC 取指纹,用于检测"末根之前的历史根被回填/订正"。

    持久 CL 增量摄入假设"历史(末根之前)K 线永不变",一旦数据源订正中间某根,增量会
    丢弃该变更 → 增量 != 全量(审计 H1,已由 test_..._mid_bar_revision 实证)。复用前用
    本指纹比对"不可变前缀",变了就放弃复用、重建全量;只追加新根 / 末根更新则前缀不变、
    指纹一致, 照常增量(不伤性能)。upto<=0 表示无历史前缀需校验, 返回固定值。
    取指纹失败 → 返回 "" (不会等于任何真实 md5), 触发保守重建。
    """
    if upto <= 0:
        return "0"
    try:
        cols = [c for c in ("open", "high", "low", "close") if c in klines.columns]
        arr = klines.iloc[:upto][cols].to_numpy(dtype="float64")
        return hashlib.md5(arr.tobytes()).hexdigest()
    except Exception:
        return ""




def recompute_chart_data_from_klines(
    market: str,
    code: str,
    frequency: str,
    cl_config: dict,
    klines: pd.DataFrame,
    cache_key: Optional[str] = None,
) -> Optional[dict]:
    """Recompute chart data through the single strict incremental CL."""

    if klines is None or len(klines) == 0:
        return None
    if "date" not in klines.columns:
        raise ValueError("chart K lines require a date column")
    if not pd.api.types.is_datetime64_any_dtype(klines["date"]):
        klines = klines.copy()
        klines["date"] = pd.to_datetime(klines["date"], utc=True)

    from chanlun.cl_utils.strict_chart_runtime import StrictChartRuntimeResult
    from chanlun.exchange.kline_completion import drop_unclosed_last_bar
    from . import chart_compute as _chart_compute

    display_frequency = frequency
    display_klines = klines

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
        return None
    if len(completed_klines) != len(display_klines):
        completed_klines = completed_klines.copy(deep=True)
        completed_klines.attrs.clear()
        completed_klines.attrs.update(source_attrs)
    display_klines = completed_klines

    runtime_key = "|".join(
        (
            market,
            display_frequency,
            str(display_klines.attrs.get("price_basis_revision")),
            str(display_klines.attrs.get("structure_price_quantum")),
        )
    )
    first_date = int(display_klines.iloc[0]["date"].timestamp())
    n = len(display_klines)
    cd = None
    reused = False
    if cache_key is not None:
        with _cl_pool_lock:
            entry = _cl_pool.get(cache_key)
            if (
                entry is not None
                and entry["config_key"] == runtime_key
                and entry["first_date"] == first_date
                and n >= entry["n"]
                and _klines_prefix_fp(display_klines, entry["n"] - 1)
                == entry.get("prefix_fp")
            ):
                cd = entry["cl"]
                reused = True
                _cl_pool.move_to_end(cache_key)

    wait_started = time.time()
    with _recompute_sem:
        calc_started = time.time()
        if cd is None:
            strict_runtime = _chart_compute.build_strict_chart_cd(
                market=market,
                code=code,
                frequency=display_frequency,
                frame=display_klines,
            )
            cd = strict_runtime.cd
        else:
            cd.process_klines(display_klines)
            strict_runtime = StrictChartRuntimeResult.success(cd)

        result = _chart_compute.serialize_chart_data_with_strict_runtime(
            market=market,
            code=code,
            display_frequency=display_frequency,
            display_klines=display_klines,
            chart_config=cl_config,
            strict_runtime=strict_runtime,
        )
    done_at = time.time()

    if cache_key is not None and cd is not None:
        with _cl_pool_lock:
            _cl_pool[cache_key] = {
                "cl": cd,
                "config_key": runtime_key,
                "first_date": first_date,
                "n": n,
                "prefix_fp": _klines_prefix_fp(display_klines, n - 1),
            }
            _cl_pool.move_to_end(cache_key)
            while len(_cl_pool) > _CL_POOL_MAX:
                _cl_pool.popitem(last=False)

    LogUtil.info(
        f"[recompute] {market}:{code} {display_frequency} klines={n} "
        f"{'inc' if reused else 'full'} "
        f"wait={(calc_started - wait_started) * 1000:.0f}ms "
        f"calc={(done_at - calc_started) * 1000:.0f}ms"
    )
    return result


_VALUE_COLUMNS_FOR_ALIGN = (
    "c", "o", "h", "l", "v",
    "macd_dif", "macd_dea", "macd_hist", "macd_area",
    "higher_macd_dif", "higher_macd_dea", "higher_macd_hist",
)  # 保持与 tv._TV_VALUE_COLUMNS 同步


def _align_value_columns_to_t(chart_data: dict) -> None:
    """把所有按 bar index 的数值列原地对齐到 len(t):过长截断、过短右 pad None。

    仅列非空且长度 != len(t) 时才动(空/缺列保持"无数据"语义)。与 tv._align_value_columns_to_t
    同款, 搬到源头(prepend)对齐 → SSE 推送/缓存/tv_history tail_gap 全下游拿到等长值列,
    杜绝前端 feedRealtimeBar/建bars 越界取 undefined → NaN 蜡烛(前端 BUG3)。
    """
    _t = chart_data.get("t", []) or []
    _n = len(_t)
    for _k in _VALUE_COLUMNS_FOR_ALIGN:
        _col = chart_data.get(_k) or []
        if _col and len(_col) != _n:
            LogUtil.warning(f"[prepend] 值列 {_k} 长 {len(_col)} != t {_n}, 已对齐")
            chart_data[_k] = list(_col[:_n]) + [None] * max(0, _n - len(_col))


def prepend_klines_and_replace_cache(
    market: str,
    code: str,
    frequency: str,
    cl_config: dict,
    new_klines: pd.DataFrame,
    cache_key: str,
) -> Optional[dict]:
    """范围请求(向左滚动)主入口。

    流程:
      1. 从 chart_data_cache 取既有 entry,反构建已缓存 K 线 DataFrame
      2. 与 new_klines 合并(cached 优先 + 去重 + 升序)
      3. 用合并后的"完整连续 K 线集"重新构造空 CL,跑 process_klines,产出新 chart_data
      4. **整体替换** chart_data_cache(is_full_snapshot=True)

    调用方(tv_history)在 chart_calc_locks.get(cache_key) 持锁期间调用本函数,
    保证"反构建 → 合并 → 重算 → 写回"原子性,前端不会读到中间态。

    返回新 chart_data;若 new_klines 为空且无缓存,返回 None。
    """
    # 局部 import 避免和 chart_compute 形成 import 链
    from . import chart_cache as _chart_cache

    cached_df = pd.DataFrame()
    cached_entry = _chart_cache._get_chart_cache_entry(cache_key)
    if cached_entry is not None:
        _cached_data = cached_entry.get("data") or {}
        cached_df = extract_klines_df_from_chart_data(_cached_data)
        # web-B2: entry 有 bar(t 非空)但反构建得空(列长不一致的半坏 entry)-> 拿不到全量缓存。
        # 若继续: 窄 new_klines merge 出窄结果并以 is_full_snapshot=True 写入 -> 污染后续
        # firstDataRequest(命中"假全量"只返回几根 K 线)。extract 声明"回退全量", 故拒绝用窄
        # 数据覆盖: 返回 None(本次 no_data, 前端保持现状), 坏 entry 交下次 miss/freshness 全量重拉。
        if _cached_data.get("t") and len(cached_df) == 0:
            LogUtil.warning(
                f"[prepend] cached entry 反构建失败(疑列长不一致半坏), 拒绝窄数据覆盖 {cache_key}"
            )
            return None

    try:
        merged = merge_klines_df(cached_df, new_klines)
    except PriceBasisMismatchError as exc:
        LogUtil.warning(
            f"[prepend] price basis mismatch; invalidate cache "
            f"market={market} code={code} frequency={frequency} "
            f"cache_key={cache_key} error={type(exc).__name__}: {exc}"
        )
        _chart_cache._delete_chart_cache_entry(cache_key)
        with _cl_pool_lock:
            _cl_pool.pop(cache_key, None)
        return None
    if merged is None or len(merged) == 0:
        return None

    # H2 纵深防御闸门:merge_klines_df 是并集,正常追加/末根更新 len(merged) >= len(cached) 恒成立。
    # 若 merged 反而比 cached 缩水,只可能来自带洞数据覆盖(cq 漏网 / 未来其它源回归) → 拒绝替换,
    # 保留旧完整缓存,绝不让好缓存被带洞结果覆盖(主修已由 C1 源级完整性闸门覆盖,此为纵深防御)。
    if len(cached_df) > 0 and len(merged) < len(cached_df):
        LogUtil.warning(
            f"[prepend] 合并后根数缩水 {len(cached_df)}->{len(merged)}, 拒绝覆盖 {cache_key}"
        )
        return cached_entry.get("data") if cached_entry is not None else None

    # 数据没变(根数与末根 OHLC 都未变)→ 跳过全量重算, 直接复用缓存。
    # 收盘/非交易时段的标的仍会被前端轮询/SSE 每隔几秒触发, 若不跳过会反复全量
    # 重算几千根缠论(实测单次 calc 可达 18s)。多标的并发时严重抢 CPU, 把新标的
    # 首次加载拖到十几~几十秒。实时只会改末根(同 time 的 OHLC 更新)或追加新根
    # (根数变), 历史根不变; 故比较"根数 + 末根 OHLC"即可判定数据是否停滞。
    if cached_entry is not None and len(cached_df) > 0 and len(cached_df) == len(merged):
        _co = cached_df.iloc[-1]
        _mo = merged.iloc[-1]
        try:
            if (
                _co["date"] == _mo["date"]
                and _co["open"] == _mo["open"]
                and _co["high"] == _mo["high"]
                and _co["low"] == _mo["low"]
                and _co["close"] == _mo["close"]
                # 末根相同还不够:中间根可能被回填/订正(审计 M1,同 H1 根因)。比对不可变前缀
                # 指纹,前缀也一致才是真"数据没变"可跳过;否则继续重算以纳入中间根变更。
                and _klines_prefix_fp(cached_df, len(merged) - 1)
                == _klines_prefix_fp(merged, len(merged) - 1)
            ):
                _data = cached_entry.get("data")
                # web-B1: OHLC 未变但末根成交量在累积(涨跌停/平价 tick 段)-> 缠论结构只依赖
                # OHLC 故不重算(省 CPU), 但就地 O(1) 刷新缓存末根成交量, 避免成交量柱冻结。
                if _data and _data.get("v") and _co["volume"] != _mo["volume"]:
                    _data["v"][-1] = float(_mo["volume"])  # 原生 python(非 numpy 标量), 防 int64 源 json.dumps 崩
                return _data
        except (KeyError, IndexError):
            pass

    new_chart_data = recompute_chart_data_from_klines(
        market, code, frequency, cl_config, merged,
        cache_key=cache_key,
    )
    if new_chart_data is None:
        return None

    # 前端 BUG3: 值列长度 != len(t) 时前端越界取 undefined → NaN 蜡烛。源头对齐,
    # 缓存/SSE 推送/tv_history 全下游安全(tv._align 为读端二道防御)。
    _align_value_columns_to_t(new_chart_data)

    _chart_cache._set_chart_cache_entry(cache_key, new_chart_data, is_full_snapshot=True)
    return new_chart_data
