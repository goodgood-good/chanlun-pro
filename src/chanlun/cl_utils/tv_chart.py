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


def zs_to_chart_dict(zs, use_envelope: bool = False) -> dict:
    """把 ZS 中枢序列化为前端图表 dict(core/web 共享,多周期叠加复用)。

    - points: 默认中枢核心区 [ZD,ZG]; use_envelope=True 用 [DD,GG] 包络
      (递归 L≥1 高级中枢 / 扩展中枢 / 多周期叠加需表达「瞬间波动」)。
    - linestyle: done→"0"(实线) / 未完成→"1"(虚线)。
    - type: 中枢方向(up/down/zd); is_expanded/sub_count: 扩展中枢标记。
    """
    hi = zs.gg if use_envelope else zs.zg
    lo = zs.dd if use_envelope else zs.zd
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
    cd: ICL, config: dict, to_frequency: str = None
) -> Union[dict, None]:
    """
    将缠论数据，转换成 tv 画图的坐标数据
    """
    klines = [
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
    klines = pd.DataFrame(klines)
    if len(klines) == 0:
        return None
    klines.loc[:, "code"] = cd.get_code()
    if to_frequency is not None:
        # to_frequency 格式为 "market:frequency"，如 "a:30m"
        market = to_frequency.split(":")[0]
        frequency = to_frequency.split(":")[1]
        if market == "a":
            klines = exchange.convert_stock_kline_frequency(klines, frequency)
        elif market == "futures":
            klines = exchange.convert_futures_kline_frequency(klines, frequency)
        elif market == "currency":
            klines = exchange.convert_currency_kline_frequency(klines, frequency)
        else:
            raise Exception(f"图表周期数据转换，不支持的市场 {market}")

    kline_ts = klines["date"].map(fun.datetime_to_int).tolist()
    kline_cs = klines["close"].tolist()
    kline_os = klines["open"].tolist()
    kline_hs = klines["high"].tolist()
    kline_ls = klines["low"].tolist()
    kline_vs = klines["volume"].tolist()

    fx_data = []
    if config["chart_show_fx"] == "1":
        # 只输出"被笔确认"的有效分型：每一笔的起止端点（bi.start / bi.end）
        # 必为分型，是缠论意义上"有效"的分型；同一分型可能同时是上一笔
        # 的 end 与下一笔的 start，按时间戳去重。
        valid_fxs = {}
        for bi in cd.get_bis():
            for fx in (bi.start, bi.end):
                if fx is None:
                    continue
                ts = fun.datetime_to_int(fx.k.date)
                valid_fxs[ts] = fx
        for ts, fx in valid_fxs.items():
            fx_data.append(
                {
                    "points": [
                        {"time": ts, "price": fx.val},
                        {"time": ts, "price": fx.val},
                    ],
                    "text": fx.type,
                }
            )

    bi_chart_data = []
    if config["chart_show_bi"] == "1":
        for bi in cd.get_bis():
            bi_chart_data.append(
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
            )

    xd_chart_data = []
    if config["chart_show_xd"] == "1":
        for xd in cd.get_xds():
            xd_chart_data.append(
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
                    "linestyle": "0" if xd.is_done() else "1",
                }
            )

    def _zs_to_chart(zs, use_envelope: bool = False) -> dict:
        return zs_to_chart_dict(zs, use_envelope)

    def _zslx_line_points(zslx) -> list:
        """走势类型的折线端点:用其真实起止分型,而不是中枢矩形边界。"""
        start = zslx.start or zslx.zss[0].lines[0].start
        end = zslx.end or zslx.zss[-1].lines[-1].end
        return [
            {"time": fun.datetime_to_int(start.k.date), "price": start.val},
            {"time": fun.datetime_to_int(end.k.date), "price": end.val},
        ]

    def _zslx_time_bounds(zslx) -> tuple:
        """走势类型矩形区间的时间边界:沿用中枢进入/离开段口径。"""
        first_zs, last_zs = zslx.zss[0], zslx.zss[-1]
        start_date = (
            first_zs.start.end.k.date
            if first_zs.start else first_zs.lines[0].start.k.date
        )
        end_date = (
            last_zs.end.start.k.date
            if last_zs.end else last_zs.lines[-1].end.k.date
        )
        return start_date, end_date

    def _zslx_meta(zslx, level: int) -> dict:
        return {
            "zslx_type": zslx.zslx_type,
            "direction": zslx.type,
            "done": bool(zslx.done),
            "linestyle": "0" if zslx.done else "1",
            "zss_count": len(zslx.zss),
            "level": level,
        }

    def _zslx_to_band_chart(zslx, level: int = 0) -> dict:
        """走势类型区间矩形:保留原有半透明背景表达。"""
        start_date, end_date = _zslx_time_bounds(zslx)
        return {
            "points": [
                {
                    "time": fun.datetime_to_int(start_date),
                    "price": max(z.gg for z in zslx.zss),
                },
                {
                    "time": fun.datetime_to_int(end_date),
                    "price": min(z.dd for z in zslx.zss),
                },
            ],
            "line_points": _zslx_line_points(zslx),
            **_zslx_meta(zslx, level),
        }

    def _zslx_to_line_chart(zslx, level: int = 0) -> dict:
        """走势类型线段:用于表达“本级走势类型作为下一级中枢构件”。"""
        return {
            "points": _zslx_line_points(zslx),
            **_zslx_meta(zslx, level),
        }

    # 笔中枢:新核心 zs_branch(bis) 观察层(get_bi_zhongshu)。前端「笔中枢」按钮(zs_bi)
    # 控制显示。不再走旧链路 get_bi_zss(清场已弃),也不门控 chart_show_bi_zs(改前端控制)。
    bi_zs_chart_data = []
    if config.get("chart_use_branch_core", "0") == "1":
        bi_zs_chart_data = [_zs_to_chart(zs) for zs in cd.get_bi_zhongshu()]
    elif config["chart_show_bi_zs"] == "1":
        for zs_type in config["zs_bi_type"]:
            for zs in cd.get_bi_zss(zs_type):
                bi_zs_chart_data.append(_zs_to_chart(zs))

    xd_zs_chart_data = []
    # branch core 开时 L0 段中枢由 recursive_levels[0] 唯一承载,不再叠加 legacy xd_zss
    # (否则同段区域 legacy + 新核心双框;对齐 mmds/bcs 在 branch core 下不双源的处理,见 line 628)。
    if (config["chart_show_xd_zs"] == "1"
            and config.get("chart_use_branch_core", "0") != "1"):
        for zs_type in config["zs_xd_type"]:
            for zs in cd.get_xd_zss(zs_type):
                xd_zs_chart_data.append(_zs_to_chart(zs))

    # 走势类型 (③ xd_zslx):矩形区间 + 独立线段。
    # 线段字段用于把“当前级别走势类型”作为“下一层中枢构件”直接画出来:
    # 线段(XD) → 当前周期中枢 → 当前周期走势类型线段 → 高一级中枢。
    xd_zslx_chart_data = []
    xd_zslx_line_chart_data = []
    if config.get("chart_show_xd_zslx", "1") == "1":
        for zslx in (cd.get_xd_zslx() or []):
            if not zslx.zss:
                continue
            xd_zslx_chart_data.append(_zslx_to_band_chart(zslx, level=0))
            xd_zslx_line_chart_data.append(_zslx_to_line_chart(zslx, level=0))

    # 递归层级树 (④ recursive_levels):L1+ 中枢、走势类型——多级联立可视化。
    recursive_levels_chart_data = []
    levels = []  # 供下方「中枢升级买卖点」复用(避免重复 get_recursive_levels)
    _kuozhan_levels = []  # 递归 kuozhan 各级(5m/30m…)中枢+背驰+买卖点,供中枢/买卖点/背驰三处复用
    if config.get("chart_show_recursive_levels", "1") == "1":
        try:
            if config.get("chart_use_branch_core", "0") == "1":
                levels = cd.get_recursive_branch_levels() or []   # 新核心 8 模块
            else:
                levels = cd.get_recursive_levels() or []          # 旧链路(默认)
        except Exception:
            levels = []
        for lv in levels:
            if lv.level == 0 and config.get("chart_use_branch_core", "0") != "1":
                continue   # 旧链路 L0 已在 xd_zss 展示;新核心 L0=线段中枢,需在此画出
            if lv.level >= 1:
                continue   # P9 止血: 中枢升级按 line4898 重做中,就绪前只画 L0,不画 L1+(旧走势递归产假宽框)
            # 中枢区间用核心区 [ZD,ZG](标准中枢=3段重叠区);GG/DD 是瞬间波动范围、非中枢区间。
            lv_zss = [_zs_to_chart(zs, use_envelope=False) for zs in lv.zss]
            # 右边缘正在形成的未完成中枢(live_zss,done=False)→虚线框(_zs_to_chart 按 done 出
            # linestyle=1)。让用户看到正在形成的 L0(5min)中枢;只展示、不入买卖点/走势类型
            # (那些只读 lv.zss=已完成中枢, 见 recursive_branch LevelResult.live_zss 注释)。
            lv_zss += [_zs_to_chart(zs, use_envelope=False) for zs in getattr(lv, "live_zss", [])]
            lv_zslxs = []
            lv_zslx_lines = []
            for zslx in lv.zslxs:
                if not zslx.zss:
                    continue
                lv_zslxs.append(_zslx_to_band_chart(zslx, level=lv.level))
                lv_zslx_lines.append(_zslx_to_line_chart(zslx, level=lv.level))
            recursive_levels_chart_data.append({
                "level": lv.level,
                "zss": lv_zss,
                "zslxs": lv_zslxs,
                "zslx_lines": lv_zslx_lines,
            })
        # 中枢升级·扩展(中心定理二 line10029 递归套用): L0 线段中枢 → L1(5m)/L2(30m)/L3(日线)。
        # cd.get_kuozhan_levels() 递归 kuozhan(上级中枢按运行交集分组,同 xds 定位)+ 各级背驰/买卖点;
        # 此处取中枢渲染,买卖点(xd_mmds)/背驰(xd_bcs)在下方接入(同源 _kuozhan_levels)。走势类型递归
        # L1+ 仍 gate 关(item2 证其退化成假宽框);递归 kuozhan 实测给紧致中枢(000001: L1=7/L2=2)。
        if config.get("chart_use_branch_core", "0") == "1":
            try:
                _kuozhan_levels = cd.get_kuozhan_levels()
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "get_kuozhan_levels 顶层失败 → 无 5m/30m 中枢/买卖点/背驰", exc_info=True)
                _kuozhan_levels = []
            for _kl in _kuozhan_levels:
                _flbl = _kuozhan_freq_label(cd.frequency, _kl["level"])
                recursive_levels_chart_data.append({
                    "level": _kl["level"],
                    "zss": [_zs_to_chart(z, use_envelope=False) for z in _kl["zss"]],
                    "zslxs": [],
                    "zslx_lines": [],
                    # 该级买卖点(一三类)/背驰,带 freq 级别标(5m/30m…);前端与该级中枢同 toggle(zs_L1/zs_L2)
                    "mmds": [{"points": {"time": fun.datetime_to_int(_p.anchor_fx.k.date),
                                         "price": _p.anchor_fx.val},
                              "text": _p.bs_type, "level": _flbl} for _p in _kl["bsp"]],
                    "bcs": [{"points": {"time": fun.datetime_to_int(_d), "price": _v},
                             "text": str(_k).upper(), "level": _flbl} for _d, _v, _k in _kl["bcs"]],
                })

    # 区间套 (interval_nest):从最高级别趋势背驰逐级下钻到 L0 的链 + 精确转折点。
    interval_nest_chart_data = None
    if config.get("chart_show_interval_nest", "1") == "1":
        try:
            inest = cd.get_interval_nest()
        except Exception:
            inest = None
        if inest is not None and inest.links:
            links_chart = []
            for link in inest.links:
                seg = link.beichi_seg
                try:
                    end_date = seg.end.k.date
                    end_val = seg.end.val
                except AttributeError:
                    continue
                links_chart.append({
                    "level": link.level,
                    "time": fun.datetime_to_int(end_date),
                    "price": end_val,
                    "is_beichi": bool(link.is_beichi),
                    "direction": seg.type,
                })
            if links_chart:
                tp = inest.turning_point
                interval_nest_chart_data = {
                    "links": links_chart,
                    "turning_point": {
                        "time": fun.datetime_to_int(tp.k.date),
                        "price": tp.val,
                    },
                    "direction": inest.direction,
                }

    bc_infos = {}
    mmd_infos = {}

    lines = {
        "bi": cd.get_bis(),
        "xd": cd.get_xds(),
    }
    line_type_map = {"bi": "笔", "xd": "段"}
    bc_type_map = {
        "bi": "BI",
        "xd": "XD",
        "pz": "PZ",
        "qs": "QS",
    }
    mmd_type_map = {
        "1buy": "1B",
        "2buy": "2B",
        "l2buy": "L2B",
        "3buy": "3B",
        "l3buy": "L3B",
        "1sell": "1S",
        "2sell": "2S",
        "l2sell": "L2S",
        "3sell": "3S",
        "l3sell": "L3S",
    }
    for line_type, ls in lines.items():
        for l in ls:
            bcs = l.line_bcs("|")
            if len(bcs) != 0 and l.end.k.date not in bc_infos.keys():
                bc_infos[l.end.k.date] = {
                    "price": l.end.val,
                    "bc_infos": {_type: [] for _type in line_type_map.keys()},
                }
            if config[f"chart_show_{line_type}_bc"] == "1":
                for bc in bcs:
                    bc_infos[l.end.k.date]["bc_infos"][line_type].append(
                        bc_type_map[bc]
                    )

            mmds = l.line_mmds("|")
            if len(mmds) != 0 and l.end.k.date not in mmd_infos.keys():
                mmd_infos[l.end.k.date] = {
                    "price": l.end.val,
                    "mmd_infos": {_type: [] for _type in line_type_map.keys()},
                }
            if config[f"chart_show_{line_type}_mmd"] == "1":
                for mmd in mmds:
                    mmd_infos[l.end.k.date]["mmd_infos"][line_type].append(
                        mmd_type_map[mmd]
                    )

    bc_chart_data = []
    bi_bc_chart_data = []
    xd_bc_chart_data = []
    if config.get("chart_use_branch_core", "0") == "1":
        # 新核心背驰信号:笔级→bi_bcs、段级(线段)→xd_bcs,与新核心买卖点**同源**(get_branch_bcs)。
        # 原图表背驰走 legacy line_bcs(极稀疏)、与新核心一类买卖点不一致(用户 2026-06 指出「背驰
        # 信号没有」);branch core 开时改接新核心背驰(done_divergence 里 is_beichi 的离开段)。
        for _use_xd, _bucket in ((False, bi_bc_chart_data), (True, xd_bc_chart_data)):
            try:
                for _date, _val, _kind in cd.get_branch_bcs(use_xd=_use_xd):
                    _bucket.append({
                        "points": {"time": fun.datetime_to_int(_date), "price": _val},
                        "text": bc_type_map.get(_kind, str(_kind).upper()),
                        "level": "xd" if _use_xd else "bi",
                    })
            except Exception:
                pass
    else:
        for dt, bc in bc_infos.items():
            ts = fun.datetime_to_int(dt)
            # 拆分版:笔/段独立产出,前端可分别 reconcile + 独立 toggle + 不同样式
            for _type, _bcs in bc["bc_infos"].items():
                if not _bcs:
                    continue
                target = bi_bc_chart_data if _type == "bi" else xd_bc_chart_data
                target.append({
                    "points": {"time": ts, "price": bc["price"]},
                    "text": ",".join(list(set(_bcs))),
                    "level": _type,
                })
            # 合并版(向后兼容):同时间点笔/段合并到一个 text 里
            bc_text = "/".join(
                [
                    f"{line_type_map[_type]}:{','.join(list(set(_bcs)))}"
                    for _type, _bcs in bc["bc_infos"].items()
                    if len(_bcs) > 0
                ]
            )
            if len(bc_text) > 0:
                bc_chart_data.append({
                    "points": {"time": ts, "price": bc["price"]},
                    "text": bc_text,
                })

    mmd_chart_data = []
    bi_mmd_chart_data = []
    xd_mmd_chart_data = []
    if config.get("chart_use_branch_core", "0") == "1":
        # 新核心买卖点:**笔级→bi_mmds、段级(线段)→xd_mmds**,各归其位。原先恒用笔级却全塞
        # xd_mmds(标 level=xd)= 笔买卖点冒充段买卖点(2026-06 用户指出);拆成两级各算。
        # branch core 开时不再叠加 legacy line_mmds(避免新核心+旧链路双源混在同一渠道)。
        for _use_xd, _bucket in ((False, bi_mmd_chart_data), (True, xd_mmd_chart_data)):
            try:
                for _p in cd.get_branch_bspoints(use_xd=_use_xd):
                    _bucket.append({
                        "points": {
                            "time": fun.datetime_to_int(_p.anchor_fx.k.date),
                            "price": _p.anchor_fx.val,
                        },
                        "text": _p.bs_type,
                        "level": "xd" if _use_xd else "bi",
                    })
            except Exception:
                pass
    else:
        # 旧链路:legacy 基础买卖点(line_mmds)笔→bi_mmds、段→xd_mmds + 向后兼容合并版。
        for dt, mmd in mmd_infos.items():
            ts = fun.datetime_to_int(dt)
            for _type, _mmds in mmd["mmd_infos"].items():
                if not _mmds:
                    continue
                target = bi_mmd_chart_data if _type == "bi" else xd_mmd_chart_data
                target.append({
                    "points": {"time": ts, "price": mmd["price"]},
                    "text": ",".join(list(set(_mmds))),
                    "level": _type,
                })
            mmd_text = "/".join(
                [
                    f"{line_type_map[_type]}:{','.join(list(set(_mmds)))}"
                    for _type, _mmds in mmd["mmd_infos"].items()
                    if len(_mmds) > 0
                ]
            )
            if len(mmd_text) > 0:
                mmd_chart_data.append({
                    "points": {"time": ts, "price": mmd["price"]},
                    "text": mmd_text,
                })
        # 中枢升级买卖点(旧链路):各升级级别走势单元上跑买卖点、合并进 xd_mmds、文本加「升」前缀。
        if config.get("chart_show_recursive_levels", "1") == "1" and levels:
            from chanlun.core.bs_point_calculator import BsPointCalculator
            for _lv in levels:
                _units = list(cd.get_xds()) if _lv.level == 0 else levels[_lv.level - 1].zslxs
                _zt = f"L{_lv.level}"
                try:
                    BsPointCalculator(cd, zs_type=_zt).calculate(_units, _lv.zss)
                except Exception:
                    continue
                for _u in _units:
                    for _m in _u.zs_type_mmds.get(_zt, []):
                        try:
                            xd_mmd_chart_data.append({
                                "points": {
                                    "time": fun.datetime_to_int(_u.end.k.date),
                                    "price": _u.end.val,
                                },
                                "text": "升" + mmd_type_map.get(_m.name, _m.name),
                                "level": "xd",
                            })
                        except Exception:
                            continue

    fx_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    bi_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    xd_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    bi_zs_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    xd_zs_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    bc_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    mmd_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    bi_bc_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    xd_bc_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    bi_mmd_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    xd_mmd_chart_data.sort(key=lambda v: v["points"]["time"], reverse=False)
    xd_zslx_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    xd_zslx_line_chart_data.sort(key=lambda v: v["points"][0]["time"], reverse=False)
    # 获取 MACD 数据
    if to_frequency is not None:
        macd = MACD(
            fast_period=int(config.get("idx_macd_fast", 12)),
            slow_period=int(config.get("idx_macd_slow", 26)),
            signal_period=int(config.get("idx_macd_signal", 9)),
        )
        macd_klines = [
            Kline(
                index=i,
                date=row["date"],
                h=float(row["high"]),
                l=float(row["low"]),
                o=float(row["open"]),
                c=float(row["close"]),
                a=float(row.get("volume") or 0.0),
            )
            for i, row in enumerate(klines.to_dict("records"))
        ]
        macd.process_macd(macd_klines)
        macd_idx = macd.get_results()["macd"]
    else:
        macd_idx = cd.get_idx()['macd']

    # 精度截断到 6 位小数, 与 higher_macd 保持一致 (tv.py:1558).
    # 默认 json 序列化会输出 17 位精度浮点数, 每个数 ~19B; round(6) 后 ~9B,
    # 4 个字段累计可省 ~200KB 响应体积.
    _dif = np.round(macd_idx['dif'], 6).tolist()
    _dea = np.round(macd_idx['dea'], 6).tolist()
    _hist = np.round(macd_idx['hist'], 6).tolist()
    _area = np.round(macd_idx.get('hist_area', []), 6).tolist()

    return {
        "t": kline_ts,
        "c": kline_cs,
        "o": kline_os,
        "h": kline_hs,
        "l": kline_ls,
        "v": kline_vs,
        "macd_dif": _dif,
        "macd_dea": _dea,
        "macd_hist": _hist,
        "macd_area": _area,
        "fxs": fx_data,
        "bis": bi_chart_data,
        "xds": xd_chart_data,
        "bi_zss": bi_zs_chart_data,
        "xd_zss": xd_zs_chart_data,
        "bcs": bc_chart_data,
        "mmds": mmd_chart_data,
        # 拆分版买卖点/背驰(笔层 vs 段层):前端可独立渲染 + 独立 toggle、
        # 用不同样式区分级别(笔=小灰、段=大显眼)。``mmds``/``bcs`` 合并版保留
        # 兼容,新前端应消费 ``bi_mmds``/``xd_mmds``/``bi_bcs``/``xd_bcs``。
        "bi_mmds": bi_mmd_chart_data,
        "xd_mmds": xd_mmd_chart_data,
        "bi_bcs": bi_bc_chart_data,
        "xd_bcs": xd_bc_chart_data,
        # 新增:③ 走势类型 / ④ 递归层级 / 区间套
        "xd_zslx": xd_zslx_chart_data,
        "xd_zslx_lines": xd_zslx_line_chart_data,
        "recursive_levels": recursive_levels_chart_data,
        "interval_nest": interval_nest_chart_data,
    }
