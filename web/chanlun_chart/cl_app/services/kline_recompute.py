# -*- coding: utf-8 -*-
"""K 线反构建 + 合并 + 缠论全量重算 服务。

设计目标:
  把 chart_data_cache 当作 L2(缠论结果层)使用,L1(原始 K 线集合)由
  chart_data 中的 t/o/h/l/c/v 字段反构建。每次范围请求(向左滚动)时:
    1. extract_klines_df_from_chart_data:从 L2 反构建出当前已缓存的 L1 K 线
    2. merge_klines_df:把新拉取的窄范围 K 线合并进去(按 date 去重 + 排序)
    3. recompute_chart_data_from_klines:用合并后的完整 K 线集走 cl.CL.process_klines
       全量重算,生成新的 chart_data dict
  调用方(tv_history)拿到新 chart_data 后,**整体替换** chart_data_cache,
  不再走 _merge_chart_data 的"shape 起点合并"路径。

无状态:所有函数纯函数,便于单测。
"""
import threading
import time
from typing import Optional

import pandas as pd

from chanlun.tools.log_util import LogUtil

# 限制并发"全量重算"数量：缠论重算 CPU 密集，多窗口(多标的)同时 prepend 全量重算
# 会争抢 CPU/GIL 导致耗时雪崩(实测 167ms→2839ms)。用信号量限并发，单次稍慢但不
# 雪崩，并发请求排队。2 兼顾少量并行与不过载；可按机器核数调整。
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
    return pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True),
        "open": o,
        "high": h,
        "low": low_arr,
        "close": c,
        "volume": v,
    })


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
        return _ensure_tz_aware(new).sort_values("date").reset_index(drop=True)
    if new is None or len(new) == 0:
        return _ensure_tz_aware(cached).sort_values("date").reset_index(drop=True)

    # 两边 tz 状态对齐(各自 naive 则补 UTC 标签),否则 sort_values 会抛
    # "Cannot compare tz-naive and tz-aware timestamps"。
    cached = _ensure_tz_aware(cached)
    new = _ensure_tz_aware(new)

    # new 后到,使其在 drop_duplicates(keep='last') 下覆盖 cached 的同 date 行
    combined = pd.concat([cached, new], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date"], keep="last")
    combined = combined.sort_values("date").reset_index(drop=True)
    return combined


def recompute_chart_data_from_klines(
    market: str,
    code: str,
    frequency: str,
    cl_config: dict,
    klines: pd.DataFrame,
    to_frequency: Optional[str] = None,
) -> Optional[dict]:
    """从一份**完整连续**的 K 线 DataFrame 出发,直接构造空 CL → process_klines →
    cl_data_to_tv_chart,返回新鲜的 chart_data dict。

    关键:
      - 不复用 ``fdb.get_web_cl_data`` 的 .pkl 缓存(那条路径有"末尾追加"假设,
        与"向左滚动"语义不兼容,会触发不必要的全量/增量分裂)。
      - 不调用 ``_merge_chart_data``;调用方拿到的就是基于完整 K 线的"权威"
        chart_data,直接整体替换 chart_data_cache。
      - to_frequency 用于"低周期合成高周期"场景,直接透传给 cl_data_to_tv_chart。
    返回 None 当 klines 为空(调用方用 _mark_negative_cache 兜底)。
    """
    if klines is None or len(klines) == 0:
        return None

    # 防御性 tz 归一化:``merge_klines_df`` 之后 dtype 通常已是 datetime64[ns, UTC],
    # 但极端 edge case(数据源混合返回 tz-aware/naive datetime 对象、长桥 SDK 边界
    # 行带 tzinfo 而其他行不带)会让 'date' 列退化成 object dtype。下游
    # ``KlineDataProcessor._preprocess`` 在 ``pd.api.types.is_datetime64_any_dtype``
    # 判断后会走 ``pd.to_datetime(klines['date'])`` —— 不传 ``utc=True``,pandas 2.x
    # 遇到混合 tz 会抛 ``ValueError: Tz-aware datetime.datetime cannot be converted
    # to datetime64 unless utc=True``,整条 prepend 路径崩。在这里提前用 ``utc=True``
    # 强制归一化为 ``datetime64[ns, UTC]``,既绕开 _preprocess 的脆弱分支,又不改
    # 其通用入口契约。
    if 'date' in klines.columns and not pd.api.types.is_datetime64_any_dtype(klines['date']):
        klines = klines.copy()
        klines['date'] = pd.to_datetime(klines['date'], utc=True)

    # 局部 import,避免顶层 import 循环(chanlun.cl_utils 反向依赖 chart_compute 几率小,
    # 但 cl.CL 的初始化栈较深,放到调用时 import 更稳)。
    from chanlun.core.cl import CL
    from chanlun.cl_utils import cl_data_to_tv_chart

    _wait_t0 = time.time()
    with _recompute_sem:  # 限并发，防多窗口同时全量重算把 CPU 打满导致雪崩
        _calc_t0 = time.time()
        cd = CL(code, frequency, dict(cl_config), market=market)
        cd.process_klines(klines)
        result = cl_data_to_tv_chart(cd, cl_config, to_frequency=to_frequency)
    _done = time.time()
    LogUtil.info(
        f"[recompute] {market}:{code} {frequency} klines={len(klines)} "
        f"wait={(_calc_t0 - _wait_t0) * 1000:.0f}ms calc={(_done - _calc_t0) * 1000:.0f}ms"
    )
    return result


def prepend_klines_and_replace_cache(
    market: str,
    code: str,
    frequency: str,
    cl_config: dict,
    new_klines: pd.DataFrame,
    cache_key: str,
    to_frequency: Optional[str] = None,
) -> Optional[dict]:
    """范围请求(向左滚动)主入口。

    流程:
      1. 从 chart_data_cache 取既有 entry,反构建已缓存 K 线 DataFrame
      2. 与 new_klines 合并(cached 优先 + 去重 + 升序)
      3. 用合并后的"完整连续 K 线集"重新构造空 CL,跑 process_klines,产出新 chart_data
      4. **整体替换** chart_data_cache(is_full_snapshot=True,不再走 _merge_chart_data)

    调用方(tv_history)在 chart_calc_locks.get(cache_key) 持锁期间调用本函数,
    保证"反构建 → 合并 → 重算 → 写回"原子性,前端不会读到中间态。

    返回新 chart_data;若 new_klines 为空且无缓存,返回 None。
    """
    # 局部 import 避免和 chart_compute 形成 import 链
    from . import chart_cache as _chart_cache

    cached_df = pd.DataFrame()
    cached_entry = _chart_cache._get_chart_cache_entry(cache_key)
    if cached_entry is not None:
        cached_df = extract_klines_df_from_chart_data(cached_entry.get("data") or {})

    merged = merge_klines_df(cached_df, new_klines)
    if merged is None or len(merged) == 0:
        return None

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
            ):
                return cached_entry.get("data")
        except (KeyError, IndexError):
            pass

    new_chart_data = recompute_chart_data_from_klines(
        market, code, frequency, cl_config, merged, to_frequency=to_frequency,
    )
    if new_chart_data is None:
        return None

    _chart_cache._set_chart_cache_entry(cache_key, new_chart_data, is_full_snapshot=True)
    return new_chart_data
