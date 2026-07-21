import logging
import math
from typing import Tuple, Union

import numpy as np
import pandas as pd

from chanlun import fun
from chanlun.core.types import ICL, Kline
from chanlun.core.macd import MACD
from chanlun.exchange import exchange


def kcharts_frequency_h_l_map(
    market: str, frequency
) -> Tuple[Union[None, str], Union[None, str]]:
    """
    将原周期，转换为新的周期进行图表展示
    按照设置好的对应关系进行返回

    返回两个值，第一个是需要获取的低级别周期值，第二个是 kcharts 画图指定的 to_frequency 值
    """
    # 高级别对应的低级别关系
    market_frequencs_map = {
        "a": {
            "m": "w",
            "w": "d",
            "d": "30m",
            "120m": "15m",
            "60m": "15m",
            "30m": "5m",
            "15m": "5m",
            "5m": "1m",
        },
        "futures": {
            "w": "d",
            "d": "60m",
            "60m": "10m",
            "30m": "5m",
            "15m": "3m",
            "10m": "2m",
            "6m": "1m",
            "5m": "1m",
            "3m": "1m",
        },
        # TODO 港美股没有写周期转换的方法，先不支持呢
        # 'us': {
        #     'y': 'q', 'q': 'm', 'm': 'w', 'w': 'd', 'd': '60m', '120m': '15m',
        #     '60m': '15m', '30m': '5m', '15m': '5m', '5m': '1m',
        # },
        # 'hk': {
        #     'y': 'm', 'm': 'w', 'w': 'd', 'd': '60m', '120m': '15m', '60m': '15m',
        #     '30m': '5m', '15m': '5m', '10m': '1m', '5m': '1m',
        # },
        "currency": {
            "w": "d",
            "d": "4h",
            "4h": "30m",
            "60m": "15m",
            "30m": "5m",
            "15m": "5m",
            "10m": "2m",
            "5m": "1m",
            "3m": "1m",
        },
    }

    try:
        return market_frequencs_map[market][frequency], f"{market}:{frequency}"
    except KeyError:
        # 不支持的 market/frequency 组合：返回 (None, None) 让调用方走默认分支。
        return None, None


def cl_qstd(cd: ICL, line_type="xd", line_num: int = 5):
    """
    缠论线段的趋势通道
    基于已完成的最后 n 条线段，线段最高两个点，线段最低两个点连线，作为趋势通道线指导交易（不一定精确）
    """
    lines = cd.get_xds() if line_type == "xd" else cd.get_bis()
    qs_lines = []
    for i in range(1, len(lines)):
        xd = lines[-i]
        if xd.is_done():
            qs_lines.append(xd)
        if len(qs_lines) == line_num:
            break

    if len(qs_lines) != line_num:
        return None

    line_highs = [
        {"val": l.high, "index": l.end.k.k_index, "date": l.end.k.date}
        for l in qs_lines
        if l.type == "up"
    ]
    line_lows = [
        {"val": l.low, "index": l.end.k.k_index, "date": l.end.k.date}
        for l in qs_lines
        if l.type == "down"
    ]
    if len(line_highs) < 2 or len(line_lows) < 2:
        return None
    line_highs = sorted(line_highs, key=lambda v: v["val"], reverse=True)
    line_lows = sorted(line_lows, key=lambda v: v["val"], reverse=False)

    def xl(one, two):
        k = (one["val"] - two["val"]) / (one["index"] - two["index"])
        return k

    qstd = {
        "up": {
            "one": line_highs[0],
            "two": line_highs[1],
            "xl": xl(line_highs[0], line_highs[1]),
        },
        "down": {
            "one": line_lows[0],
            "two": line_lows[1],
            "xl": xl(line_lows[0], line_lows[1]),
        },
    }
    chart_up_start = {
        "val": line_highs[0]["val"]
        - qstd["up"]["xl"] * (line_highs[0]["index"] - qs_lines[-1].start.k.k_index),
        "index": qs_lines[-1].start.k.k_index,
        "date": qs_lines[-1].start.k.date,
    }
    chart_up_end = {
        "val": line_highs[0]["val"]
        - qstd["up"]["xl"] * (line_highs[0]["index"] - cd.get_klines()[-1].index),
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    chart_down_start = {
        "val": line_lows[0]["val"]
        - qstd["down"]["xl"] * (line_lows[0]["index"] - qs_lines[-1].start.k.k_index),
        "index": qs_lines[-1].start.k.k_index,
        "date": qs_lines[-1].start.k.date,
    }
    chart_down_end = {
        "val": line_lows[0]["val"]
        - qstd["down"]["xl"] * (line_lows[0]["index"] - cd.get_klines()[-1].index),
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    qstd["up"]["chart"] = {
        "x": [chart_up_start["date"], chart_up_end["date"]],
        "y": [chart_up_start["val"], chart_up_end["val"]],
        "index": [chart_up_start["index"], chart_up_end["index"]],
    }
    qstd["down"]["chart"] = {
        "x": [chart_down_start["date"], chart_down_end["date"]],
        "y": [chart_down_start["val"], chart_down_end["val"]],
        "index": [chart_down_start["index"], chart_down_start["index"]],
    }

    now_point = {
        "val": cd.get_klines()[-1].c,
        "index": cd.get_klines()[-1].index,
        "date": cd.get_klines()[-1].date,
    }
    qstd["up"]["now"] = (
        "up" if xl(chart_up_start, now_point) > qstd["up"]["xl"] else "down"
    )
    qstd["down"]["now"] = (
        "up" if xl(chart_down_start, now_point) > qstd["down"]["xl"] else "down"
    )

    return qstd


def prices_jiaodu(prices):
    """
    技术价格序列中，起始与终点的角度（正为上，负为下）

    弧度 = dy / dx
        dy = 终点与起点的差值
        dx = 固定位 100000
        dy 如果不足六位数，进行补位
    不同品种的标的价格有差异，这时计算的角度会有很大的不同，不利于量化，将 dy 固定，变相的将所有标的放在一个尺度进行对比
    """
    if prices[-1] == prices[0]:
        return 0
    dy = max(prices[-1], prices[0]) - min(prices[-1], prices[0])
    dx = 100000
    while True:
        dy_len = len(str(int(dy)))
        if dy_len < 6:
            dy = dy * (10 ** (6 - dy_len))
        elif dy_len > 6:
            dy = dy / (10 ** (dy_len - 6))
        else:
            break
    # 弧度
    k = math.atan2(dy, dx)
    # 弧度转角度
    j = math.degrees(k)
    return j if prices[-1] > prices[0] else -j


def _line_to_chart_metadata(line) -> dict | None:
    """Serialize one center entering/leaving line without exposing core objects."""
    if line is None:
        return None
    start = getattr(line, "start", None)
    end = getattr(line, "end", None)
    start_k = getattr(start, "k", None)
    end_k = getattr(end, "k", None)
    start_date = getattr(start_k, "date", None)
    end_date = getattr(end_k, "date", None)
    return {
        "direction": getattr(line, "type", None),
        "start_time": (
            None if start_date is None else fun.datetime_to_int(start_date)
        ),
        "start_price": getattr(start, "val", None),
        "end_time": None if end_date is None else fun.datetime_to_int(end_date),
        "end_price": getattr(end, "val", None),
    }


def _center_associated_points(zs) -> list[str]:
    """Return point types explicitly attached to the center's leaving line."""
    seen: set[str] = set()
    result: list[str] = []
    for line in (getattr(zs, "exit", None), getattr(zs, "end", None)):
        getter = getattr(line, "line_mmds", None)
        if not callable(getter):
            continue
        try:
            values = getter("|")
        except TypeError:
            values = getter()
        for value in values or ():
            point_type = str(value)
            if point_type and point_type not in seen:
                seen.add(point_type)
                result.append(point_type)
        if result:
            break
    return result


def zs_to_chart_dict(
    zs,
    use_envelope: bool = False,
    recursive_level: int | None = None,
) -> dict:
    """把 ZS 中枢序列化为前端图表 dict(core/web 共享,多周期叠加复用)。

    - points: 默认中枢核心区 [ZD,ZG]; use_envelope=True 用 [DD,GG] 包络
      (递归 L≥1 高级中枢 / 扩展中枢 / 多周期叠加需表达「瞬间波动」)。
    - linestyle: done→"0"(实线) / 未完成→"1"(虚线)。
    - type: 中枢方向(up/down/zd); is_expanded/sub_count: 扩展中枢标记。
    """
    hi = zs.gg if use_envelope else zs.zg
    lo = zs.dd if use_envelope else zs.zd
    entering = getattr(zs, "entry", None) or getattr(zs, "start", None)
    leaving = getattr(zs, "exit", None) or getattr(zs, "end", None)
    if recursive_level is None:
        recursive_level = getattr(zs, "recursive_level", None)
    return {
        "points": [
            {
                "time": fun.datetime_to_int(zs.start.end.k.date) if zs.start else fun.datetime_to_int(zs.lines[0].start.k.date),
                "price": hi,
            },
            {
                "time": fun.datetime_to_int(zs.end.start.k.date) if zs.end else fun.datetime_to_int(zs.lines[-1].end.k.date),
                "price": lo,
            },
        ],
        "linestyle": "0" if zs.done else "1",
        "type": zs.type,
        "is_expanded": bool(getattr(zs, "expanded_with", [])),
        "sub_count": len(getattr(zs, "expanded_with", []) or []),
        "tower": getattr(zs, "zs_type", None),
        "recursive_level": recursive_level,
        "zd": getattr(zs, "zd", None),
        "zg": getattr(zs, "zg", None),
        "done": bool(getattr(zs, "done", False)),
        "line_count": len(getattr(zs, "lines", []) or []),
        "entering_segment": _line_to_chart_metadata(entering),
        "leaving_segment": _line_to_chart_metadata(leaving),
        "associated_points": _center_associated_points(zs),
    }


_KUOZHAN_FREQ_CHAIN = {
    "1m": ["1m", "5m", "30m", "日线"], "5m": ["5m", "30m", "日线", "周线"],
    "15m": ["15m", "60m", "日线", "周线"], "30m": ["30m", "日线", "周线", "月线"],
    "60m": ["60m", "日线", "周线", "月线"], "d": ["日线", "周线", "月线", "年线"],
}


def _kuozhan_freq_label(base_freq: str, level: int) -> str:
    """递归 kuozhan 级别(1=高一级…)→ 频率标签,与前端 charts.js FREQ_CHAIN 对齐。"""
    chain = _KUOZHAN_FREQ_CHAIN.get(base_freq, [base_freq, "高一级", "高二级", "高三级"])
    return chain[level] if 0 <= level < len(chain) else f"L{level}"


def cl_data_to_tv_chart(
    cd: ICL,
    config: dict,
    to_frequency: str = None,
    *,
    strict_runtime=None,
) -> Union[dict, None]:
    """Convert base lines and one atomic strict evidence snapshot for TV charts."""

    from chanlun.cl_utils.strict_chart import build_strict_structure_snapshot

    klines = pd.DataFrame(
        [
            {
                "date": k.date,
                "high": k.h,
                "low": k.l,
                "open": k.o,
                "close": k.c,
                "volume": k.a,
            }
            for k in cd.get_klines()
        ]
    )
    if len(klines) == 0:
        return None
    klines.loc[:, "code"] = cd.get_code()

    display_frequency = cd.get_frequency()
    if to_frequency is not None:
        parts = to_frequency.split(":")
        if len(parts) != 2 or not all(parts):
            raise ValueError("to_frequency must use market:frequency")
        market, display_frequency = parts
        if market == "a":
            klines = exchange.convert_stock_kline_frequency(
                klines,
                display_frequency,
            )
        elif market == "futures":
            klines = exchange.convert_futures_kline_frequency(
                klines,
                display_frequency,
            )
        elif market == "currency":
            klines = exchange.convert_currency_kline_frequency(
                klines,
                display_frequency,
            )
        else:
            raise ValueError(f"unsupported chart conversion market: {market}")

    kline_ts = klines["date"].map(fun.datetime_to_int).tolist()
    kline_cs = klines["close"].tolist()
    kline_os = klines["open"].tolist()
    kline_hs = klines["high"].tolist()
    kline_ls = klines["low"].tolist()
    kline_vs = klines["volume"].tolist()

    def _enabled(name: str) -> bool:
        return config.get(name) in ("1", 1, True)

    fx_data = []
    if _enabled("chart_show_fx"):
        valid_fxs = {}
        for bi in cd.get_bis():
            for fx in (bi.start, bi.end):
                if fx is not None:
                    valid_fxs[fun.datetime_to_int(fx.k.date)] = fx
        fx_data = [
            {
                "points": [
                    {"time": timestamp, "price": fx.val},
                    {"time": timestamp, "price": fx.val},
                ],
                "text": fx.type,
            }
            for timestamp, fx in sorted(valid_fxs.items())
        ]

    bi_chart_data = []
    if _enabled("chart_show_bi"):
        bi_chart_data = [
            {
                "points": [
                    {
                        "time": fun.datetime_to_int(bi.start.k.date),
                        "price": bi.start.val,
                    },
                    {
                        "time": fun.datetime_to_int(bi.end.k.date),
                        "price": bi.end.val,
                    },
                ],
                "linestyle": "0" if bi.is_done() else "1",
            }
            for bi in cd.get_bis()
        ]
        bi_chart_data.sort(key=lambda value: value["points"][0]["time"])

    xd_chart_data = []
    if _enabled("chart_show_xd"):
        xd_chart_data = [
            {
                "points": [
                    {
                        "time": fun.datetime_to_int(xd.start.k.date),
                        "price": xd.start.val,
                    },
                    {
                        "time": fun.datetime_to_int(xd.end.k.date),
                        "price": xd.end.val,
                    },
                ],
                "linestyle": (
                    "1" if getattr(xd, "forming", False) else "0"
                ),
            }
            for xd in cd.get_xds()
        ]
        xd_chart_data.sort(key=lambda value: value["points"][0]["time"])

    if to_frequency is not None or len(klines) != len(cd.get_src_klines()):
        macd = MACD(
            fast_period=int(config.get("idx_macd_fast", 12)),
            slow_period=int(config.get("idx_macd_slow", 26)),
            signal_period=int(config.get("idx_macd_signal", 9)),
        )
        macd_klines = [
            Kline(
                index=index,
                date=row["date"],
                h=float(row["high"]),
                l=float(row["low"]),
                o=float(row["open"]),
                c=float(row["close"]),
                a=float(row.get("volume") or 0.0),
            )
            for index, row in enumerate(klines.to_dict("records"))
        ]
        macd.process_macd(macd_klines)
        macd_idx = macd.get_results()["macd"]
    else:
        macd_idx = cd.get_idx()["macd"]

    result = {
        "t": kline_ts,
        "c": kline_cs,
        "o": kline_os,
        "h": kline_hs,
        "l": kline_ls,
        "v": kline_vs,
        "macd_dif": np.round(macd_idx["dif"], 6).tolist(),
        "macd_dea": np.round(macd_idx["dea"], 6).tolist(),
        "macd_hist": np.round(macd_idx["hist"], 6).tolist(),
        "macd_area": np.round(macd_idx.get("hist_area", []), 6).tolist(),
        "fxs": fx_data,
        "bis": bi_chart_data,
        "xds": xd_chart_data,
    }

    if strict_runtime is not None and strict_runtime.error_code is not None:
        result["strict_structure_mode"] = "unavailable"
        result["strict_structure_error"] = {
            "code": strict_runtime.error_code
        }
        return result

    strict_cd = cd if strict_runtime is None else strict_runtime.cd
    error_code = "strict_evidence_invalid"
    try:
        if strict_cd is None:
            raise ValueError("strict chart runtime has no CL")
        evidence = strict_cd.get_strict_evidence()
        strict_structure = build_strict_structure_snapshot(
            evidence,
            interval=display_frequency,
        )
        if (
            strict_structure["symbol"] != cd.get_code()
            or strict_structure["source_frequency"] != display_frequency
            or strict_structure["display_frequency"] != display_frequency
            or strict_structure["source_closed_at"] != kline_ts[-1]
        ):
            error_code = "strict_context_mismatch"
            raise ValueError(
                "strict snapshot context does not match displayed bars"
            )
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "strict chart structure unavailable "
            "code=%s frequency=%s error_code=%s error=%s: %s",
            cd.get_code(),
            display_frequency,
            error_code,
            type(exc).__name__,
            exc,
        )
        result["strict_structure_mode"] = "unavailable"
        result["strict_structure_error"] = {"code": error_code}
    else:
        result["strict_structure_mode"] = "replace"
        result["strict_structure"] = strict_structure

    return result
