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
from typing import Optional

import pandas as pd


def extract_klines_df_from_chart_data(chart_data: dict) -> pd.DataFrame:
    """从 chart_data 中的 t/o/h/l/c/v 列反构建 K 线 DataFrame。

    chart_data["t"] 是 unix 秒时间戳(int 列表),与 cl_data_to_tv_chart 输出对齐。
    返回的 DataFrame 列与 ex.klines() 一致:date / open / high / low / close / volume。
    若任一关键列缺失或长度不一致,返回空 DataFrame(调用方据此回退到全量拉取)。
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
    if n == 0 or not (len(o) == len(h) == len(low_arr) == len(c) == len(v) == n):
        return pd.DataFrame()
    return pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s"),
        "open": o,
        "high": h,
        "low": low_arr,
        "close": c,
        "volume": v,
    })


def merge_klines_df(cached: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并两段 K 线 DataFrame,按 date 去重 + 升序排序。

    重叠优先级:cached > new。
      - cached K 线通常已经是"已完成 bar",值更稳定;
      - new 可能在边界拉到的是仍在波动的当前 bar(尤其分钟级范围请求),
        若以 new 覆盖 cached,会把已经稳定的 bar 重新打波动状态,
        导致下游 process_klines 触发不必要的"末根更新"路径,且边界处的
        OHLC 可能与缓存里不一致 → 缠论笔/段重新画。
    入参为空时直接返回另一边的副本(始终按 date 升序)。
    """
    if cached is None or len(cached) == 0:
        if new is None or len(new) == 0:
            return pd.DataFrame()
        return new.sort_values("date").reset_index(drop=True)
    if new is None or len(new) == 0:
        return cached.sort_values("date").reset_index(drop=True)

    # cached 后到,使其在 drop_duplicates(keep='last') 下覆盖 new 的同 date 行
    combined = pd.concat([new, cached], ignore_index=True)
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

    # 局部 import,避免顶层 import 循环(chanlun.cl_utils 反向依赖 chart_compute 几率小,
    # 但 cl.CL 的初始化栈较深,放到调用时 import 更稳)。
    from chanlun.core.cl import CL
    from chanlun.cl_utils import cl_data_to_tv_chart

    cd = CL(code, frequency, dict(cl_config))
    cd.process_klines(klines)
    return cl_data_to_tv_chart(cd, cl_config, to_frequency=to_frequency)


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

    new_chart_data = recompute_chart_data_from_klines(
        market, code, frequency, cl_config, merged, to_frequency=to_frequency,
    )
    if new_chart_data is None:
        return None

    _chart_cache._set_chart_cache_entry(cache_key, new_chart_data, is_full_snapshot=True)
    return new_chart_data
