import itertools
from typing import List, Union

import numpy as np
import pandas as pd
import talib

from chanlun.backtesting.base import MarketDatas, Strategy
from chanlun.core.cl_interface import BI, Config
from chanlun.cl_utils import (
    bi_qk_num,
    cal_klines_macd_infos,
    cal_zs_macd_infos,
    last_done_bi,
)

"""
选股函数集合：根据缠论数据（笔/线段/中枢/背驰/买卖点）筛选目标标的。
每个函数接受 code、mk_datas、opt_type 参数，返回 {"code": ..., "msg": ...} 或 None。
"""

direction_types = {"long": ["down"], "short": ["up"]}
mmd_types = {
    "long": ["1buy", "2buy", "3buy", "l2buy", "l3buy"],
    "short": ["1sell", "2sell", "3sell", "l2sell", "l3sell"],
}


def get_opt_types(opt_type: list = []):
    """将 opt_type（如 ['long', 'short']）展开为对应的笔方向列表和买卖点类型列表。"""
    if len(opt_type) == 0:
        opt_type = ["long"]
    opt_direction = list(
        itertools.chain.from_iterable([direction_types[x] for x in opt_type])
    )
    opt_mmd = list(itertools.chain.from_iterable([mmd_types[x] for x in opt_type]))
    return opt_direction, opt_mmd


def xg_single_xd_and_bi_mmd(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    线段和笔都有出现买点
    周期：单周期
    适用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)

    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_xds()) == 0 or len(cd.get_bis()) == 0:
        return None
    xd = cd.get_xds()[-1]
    bi = cd.get_bis()[-1]

    if xd.mmd_exists(opt_mmd, "|") and bi.mmd_exists(opt_mmd, "|"):
        return {
            "code": code,
            "msg": f"线段买点 【{xd.line_mmds('|')}】 笔买点【{bi.line_mmds('|')}】",
        }
    return None


def xg_multiple_xd_bi_mmd(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    选择 高级别线段，低级别笔 都出现买点，或者 高级别线段和高级别笔 都出现 背驰 的条件
    高级别线段买点或背驰，并且次级别笔买点或背驰
    周期：两个周期
    适用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)

    # 先判断高级别的
    high_data = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(high_data.get_xds()) == 0 or len(high_data.get_bis()) == 0:
        return None
    high_xd = high_data.get_xds()[-1]
    if high_xd.type not in opt_direction:
        return None

    # 再判断低级别的
    low_data = mk_datas.get_cl_data(code, mk_datas.frequencys[1])
    if len(low_data.get_xds()) == 0 or len(low_data.get_bis()) == 0:
        return None
    low_bi = low_data.get_bis()[-1]
    if low_bi.type not in opt_direction:
        return None

    # 判断是否买点或背驰
    if (high_xd.mmd_exists(opt_mmd, "|") or high_xd.bc_exists(["pz", "qs"], "|")) and (
        low_bi.mmd_exists(opt_mmd, "|") or low_bi.bc_exists(["pz", "qs"], "|")
    ):
        return {
            "code": code,
            "msg": f"{high_data.get_frequency()} 线段买点【{high_xd.line_mmds('|')}】背驰【{high_xd.line_bcs('|')}】 {low_data.get_frequency()} 笔买点【{low_bi.line_mmds('|')}】背驰【{low_bi.line_bcs('|')}】",
        }

    return None


def xg_single_xd_bi_zs_zf_5(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    上涨线段的 第一个 笔中枢， 突破 笔中枢， 大涨 5% 以上的股票
    周期：单周期
    适用市场：沪深A股
    作者：Jiang Haoquan
    """
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])

    if len(cd.get_xds()) == 0 or len(cd.get_bi_zss()) == 0:
        return None
    xd = cd.get_xds()[-1]
    bi_zs = cd.get_bi_zss()[-1]
    kline = cd.get_klines()[-1]

    if (
        xd.type == "up"
        and xd.start.index == bi_zs.lines[0].start.index
        and kline.h > bi_zs.zg >= kline.l
        and (kline.c - kline.o) / kline.o > 0.05
    ):
        return {
            "code": cd.get_code(),
            "msg": "线段向上，当前K线突破中枢高点，并且涨幅大于 5% 涨幅",
        }

    return None


def xg_single_xd_bi_23_overlapped(
    code: str, mk_datas: MarketDatas, opt_type: list = []
):
    """
    上涨线段的 第一个 笔中枢， 突破 笔中枢后 23买重叠的股票
    周期：单周期
    适用市场：沪深A股
    作者：Jiang Haoquan
    """
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_xds()) == 0 or len(cd.get_bi_zss()) == 0:
        return None
    xd = cd.get_xds()[-1]
    bi_zs = cd.get_bi_zss()[-1]
    bi = cd.get_bis()[-1]
    bi_2 = cd.get_bis()[-2]
    bi_3 = cd.get_bis()[-3]

    overlapped_23_bi = bi.mmd_exists(["2buy"]) and bi.mmd_exists(["3buy"])
    overlapped_23_bi_2 = (
        bi_2.mmd_exists(["2buy"])
        and bi_2.mmd_exists(["3buy"])
        and Strategy.bi_td(bi, cd) is True
    )
    overlapped_23_bi_3 = (
        bi_3.mmd_exists(["2buy"])
        and bi_3.mmd_exists(["3buy"])
        and bi.mmd_exists(["l3buy"])
    )

    if (
        xd.type == "up"
        and xd.start.index == bi_zs.lines[0].start.index
        and overlapped_23_bi
        or overlapped_23_bi_2
        or overlapped_23_bi_3
    ):
        return {
            "code": cd.get_code(),
            "msg": "线段向上，当前笔突破中枢高点后 2，3 买重叠",
        }

    return None


def xg_single_day_bc_and_up_jincha(
    code: str, mk_datas: MarketDatas, opt_type: list = []
):
    """
    日线级别，倒数第二个向下笔背驰（笔背驰、盘整背驰、趋势背驰），后续macd在水上金叉
    """
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) <= 5 or len(cd.get_xds()) == 0 or len(cd.get_bi_zss()) == 0:
        return None
    xd = cd.get_xds()[-1]
    bis = cd.get_bis()
    bi_zs = cd.get_bi_zss()[-1]
    # 获取所有下跌笔
    down_bis = [bi for bi in bis if bi.type == "down"]
    if len(down_bis) < 2:
        return None
    if xd.type != "down":
        return None

    # 下跌笔不能再创新低
    if down_bis[-1].low < down_bis[-2].low:
        return None

    # 当前黄白线要在零轴上方
    macd_dif = cd.get_idx()["macd"]["dif"][-1]
    macd_dea = cd.get_idx()["macd"]["dea"][-1]
    if macd_dif < 0 or macd_dea < 0:
        return None

    # 倒数第二下跌笔要出背驰
    if down_bis[-2].bc_exists(["pz", "qs"]) is False:
        return None

    # 最后一个中枢 黄白线要上穿零轴
    zs_macd_info = cal_zs_macd_infos(bi_zs, cd)
    if zs_macd_info.dif_up_cross_num == 0 and zs_macd_info.dea_up_cross_num == 0:
        return None

    macd_infos = cal_klines_macd_infos(
        down_bis[-1].start.k.klines[0], cd.get_klines()[-1], cd
    )
    if macd_infos.gold_cross_num > 0:
        return {
            "code": cd.get_code(),
            "msg": f"前down笔背驰 {down_bis[-2].line_bcs()} macd 在零轴之上，后续又出现金叉，可关注",
        }
    return None


def xg_multiple_low_level_12mmd(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    选择 高级别出现背驰or买卖点，并且低级别出现一二类买卖点
    周期：三个周期
    适用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)

    high_data = mk_datas.get_cl_data(code, mk_datas.frequencys[0])

    if len(high_data.get_bis()) == 0:
        return None
    # 高级别向下，并且有背驰or买卖点
    high_bi = high_data.get_bis()[-1]
    if high_bi.type not in opt_direction:
        return None
    if len(high_bi.line_bcs("|")) == 0 and len(high_bi.line_mmds("|")) == 0:
        return None

    low_data_1 = mk_datas.get_cl_data(code, mk_datas.frequencys[1])
    low_data_2 = mk_datas.get_cl_data(code, mk_datas.frequencys[2])
    if len(low_data_1.get_bis()) == 0 or len(low_data_2.get_bis()) == 0:
        return None

    # 获取高级别底分型后的低级别笔
    start_datetime = high_bi.end.klines[0].date
    low_bis: List[BI] = []
    for _bi in low_data_1.get_bis():
        if _bi.end.k.date > start_datetime:
            low_bis.append(_bi)
    for _bi in low_data_2.get_bis():
        if _bi.end.k.date > start_datetime:
            low_bis.append(_bi)

    # 遍历低级别的笔，找是否有一二类买点
    exists_12_mmd = False
    for _bi in low_bis:
        if _bi.mmd_exists(
            ["1buy", "2buy"] if high_bi.type == "down" else ["1sell", "2sell"], "|"
        ):
            exists_12_mmd = True
            break

    if exists_12_mmd:
        return {
            "code": high_data.get_code(),
            "msg": f"{high_data.get_frequency()} 背驰 {high_bi.line_bcs('|')} 买点 {high_bi.line_mmds('|')} 并且低级别出现12类买卖点",
        }

    return None


def xg_single_bi_1mmd(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    获取笔的一类买卖点
    周期：单周期
    适用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)

    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) == 0:
        return None
    bi = cd.get_bis()[-1]
    if bi.type not in opt_direction:
        return None
    for _zs_type, _mmds in bi.zs_type_mmds.items():
        for _m in _mmds:
            if _m.name == "1buy" and _m.zs.line_num < 9:
                return {
                    "code": cd.get_code(),
                    "msg": f"{cd.get_frequency()} 出现本级别笔一买",
                }

    return None


def xg_single_bi_2mmd(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    获取笔的二类买卖点
    周期：单周期
    适用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) == 0:
        return None
    bi = cd.get_bis()[-1]
    if bi.type not in opt_direction:
        return None
    for _zs_type, _mmds in bi.zs_type_mmds.items():
        for _m in _mmds:
            if _m.name == "2buy" and _m.zs.line_num < 9:
                return {
                    "code": cd.get_code(),
                    "msg": f"{cd.get_frequency()} 出现本级别笔二买",
                }

    return None


def xg_single_bi_3mmd(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    获取笔的三类买卖点
    周期：单周期
    适用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) == 0:
        return None
    bi = cd.get_bis()[-1]
    if bi.type not in opt_direction:
        return None
    for _zs_type, _mmds in bi.zs_type_mmds.items():
        for _m in _mmds:
            if _m.name == "3buy" and _m.zs.line_num < 9:
                return {
                    "code": cd.get_code(),
                    "msg": f"{cd.get_frequency()} 出现本级别笔三买",
                }

    return None


def xg_single_bcmmd_next_di_fx_verif(
    code: str, mk_datas: MarketDatas, opt_type: list = []
):
    """
    笔出现买点或下跌背驰，并且后续出现底分型验证，则提示
    周期：单周期
    适用市场：沪深A股
    作者：WX
    """
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) == 0:
        return None
    bi = last_done_bi(cd)
    if bi.type != "down":
        return None

    for bc in bi.bcs:
        if bc.type in ["pz", "qs"] and bc.zs.line_num <= 9:
            zs_macd_info = cal_zs_macd_infos(bc.zs, cd)
            if zs_macd_info.dif_up_cross_num > 0 or zs_macd_info.dea_up_cross_num > 0:
                end_di_fx = [
                    _fx
                    for _fx in cd.get_fxs()
                    if (_fx.type == "di" and _fx.index > bi.end.index and _fx.done)
                ]
                if len(end_di_fx) == 0:
                    return None
                end_fx = end_di_fx[0]
                if (
                    cd.get_cl_klines()[-1].index - end_fx.k.index <= 3
                    and end_fx.val > bi.end.val
                ):
                    return {
                        "code": cd.get_code(),
                        "msg": f"{cd.get_frequency()} 出现背驰 {bi.line_bcs()}，并且后续出现验证底分型，可关注",
                    }

    return None


def xg_multiple_zs_tupo_low_3buy(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    高级别中枢突破，在低级别有三买
    所谓横有多长竖有多长
    找一个高级别（比如日线）大级别中枢窄幅震荡（大于9笔的中枢），在 macd 零轴上方，低一级别出现三类买点的股票
    周期：双周期
    适用市场：沪深A股
    作者：WX
    """
    high_cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(high_cd.get_bi_zss()) == 0:
        return None
    high_last_bi_zs = high_cd.get_bi_zss()[-1]
    if (
        high_last_bi_zs.done is True
        or high_last_bi_zs.line_num < 9
        or high_last_bi_zs.zf() <= 50
    ):
        return None
    # macd 黄白线要在上方
    high_dif = high_cd.get_idx()["macd"]["dif"][-1]
    high_dea = high_cd.get_idx()["macd"]["dea"][-1]
    if high_dif < 0 or high_dea < 0:
        return None

    # 低级别笔三买
    low_cd = mk_datas.get_cl_data(code, frequency=mk_datas.frequencys[1])
    if len(low_cd.get_bis()) == 0:
        return None

    low_last_bi = low_cd.get_bis()[-1]
    if low_last_bi.mmd_exists(["3buy"]):
        return {
            "code": high_cd.get_code(),
            "msg": f"{high_cd.get_frequency()} 中枢有可能突破，低级别出现三买，进行关注",
        }

    return None


def xg_single_pre_bi_tk_and_3buy(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    在三买点前一笔，有跳空缺口
    说明突破中枢的力度比较大，可以重点关注
    周期：单周期
    使用市场：沪深A股
    作者：WX
    """
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) < 5:
        return None
    pre_bi = cd.get_bis()[-2]
    now_bi = cd.get_bis()[-1]
    # 之前一笔要出现向上跳空缺口
    up_qk_num, _ = bi_qk_num(cd, pre_bi)
    if up_qk_num <= 0:
        return None
    # 出现三类买点，并且前笔的高点大于等于中枢的 gg 点
    for mmd in now_bi.mmds:
        if mmd.name == "3buy" and pre_bi.high >= mmd.zs.gg:
            return {
                "code": cd.get_code(),
                "msg": f"三买前一笔出现 {up_qk_num} 缺口，可重点关注",
            }
    return None


def xg_single_find_3buy_by_1buy(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    找三买点，前提是前面中枢内有一类买卖点
    （不同的中枢配置，筛选的条件会有差异）
    周期：单周期
    使用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)

    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) <= 5:
        return None

    if len(cd.get_bi_zss()) < 2:
        return None

    # 前面有一买有以下几种情况
    # 三买出现在一个大的中枢上方，在中枢内部有一买 （标准中枢情况会出现）
    # 在三买中枢与前一个中枢之间，有个一买（段内中枢有可能出现）
    # ......

    bi = cd.get_bis()[-1]
    if bi.type not in opt_direction:
        return None
    for _zs_type, _mmds in bi.zs_type_mmds.items():
        for _m in _mmds:
            if _m.name not in ["3buy", "3sell"]:
                continue
            for _l in _m.zs.lines:
                if _l.mmd_exists(
                    ["1buy"] if bi.type == "down" else ["1sell"], _zs_type
                ):
                    return {
                        "code": cd.get_code(),
                        "msg": "出现三买，并且之前有出现一买",
                    }
    return None


def xg_single_find_3buy_by_zhuanzhe(
    code: str, mk_datas: MarketDatas, opt_type: list = []
):
    """
    找三买点，之前段内要有是一个下跌趋势，后续下跌趋势结束，出现转折中枢的三买
    （缠论的笔中枢配置要是段内中枢）
    周期：单周期
    使用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if (
        len(cd.get_bis()) <= 5
        or len(cd.get_xds()) < 2
        or len(cd.get_bi_zss(Config.ZS_TYPE_DN.value)) < 3
    ):
        return None
    # 在三买中枢之前的两个中枢，要是趋势下跌
    bi = cd.get_bis()[-1]
    if bi.type not in opt_direction:
        return None
    if bi.mmd_exists(["3buy"] if bi.type == "down" else ["3sell"], "|") is False:
        return None
    dn_zss = cd.get_bi_zss(Config.ZS_TYPE_DN.value)
    if (
        cd.zss_is_qs(dn_zss[-2], dn_zss[-1]) == "down"
        or cd.zss_is_qs(dn_zss[-3], dn_zss[-2]) == "down"
    ):
        return {"code": cd.get_code(), "msg": "出现三买，并且之前有下跌趋势"}
    return None


def xg_single_ma_250(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    找最新价格在 ma 250 线的上下
    周期：单周期
    使用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    closes = np.array([_k.c for _k in cd.get_src_klines()])
    ma250 = talib.MA(closes, timeperiod=250)
    close = cd.get_src_klines()[-1].c
    if "up" in opt_direction and close > ma250[-1]:
        return {"code": cd.get_code(), "msg": "最新价格高于250日均线"}
    if "down" in opt_direction and close < ma250[-1]:
        return {"code": cd.get_code(), "msg": "最新价格低于250日均线"}
    return None


def xg_single_bi_1buy_next_l3buy_mmd(
    code: str, mk_datas: MarketDatas, opt_type: list = []
):
    """
    笔1buy后的中枢[类三买/类二买]
    周期：单周期
    适用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)

    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) == 0:
        return None
    bi = cd.get_bis()[-1]

    if bi.type == "up":
        return None

    # 从最新笔向前找最近一根有一买信号的笔
    bi_by_1buy = None
    for _bi in cd.get_bis()[::-1]:
        if _bi.mmd_exists(["1buy"], "|"):
            bi_by_1buy = _bi
            break

    if bi_by_1buy is None:
        return None

    # 一买后续笔构成恰好一个中枢才符合条件（无中枢或多中枢均排除）
    zs_lines = cd.get_bis()[bi_by_1buy.index + 1 :]
    # 一买后超过 9 笔说明结构已发展充分，不再关注
    if len(zs_lines) > 9:
        return None
    zs = cd.create_dn_zs("bi", zs_lines)
    if len(zs) != 1:
        return None

    zs = zs[0]
    if bi.index not in [_l.index for _l in zs.lines]:
        return None

    # 中枢起始笔必须紧接一买后的第一笔，否则中间有缺口
    if zs.lines[0].index != zs_lines[0].index:
        return None

    # 排除中枢进入段（lines[0]），取后续线段的最低价
    zs_min_price = min([_l.low for _l in zs.lines[1:]])

    # 一买可能属于多种中枢配置，收集所有对应中枢
    zss_by_1buy = []
    for _zs_type, _mmds in bi_by_1buy.zs_type_mmds.items():
        for _m in _mmds:
            if _m.name == "1buy":
                zss_by_1buy.append(_m.zs)
    for _zs_1buy in zss_by_1buy:
        # 一买后中枢最低点须高于一买中枢的中心价，确保后续结构不破坏一买有效性
        _zs_1buy_mid_price = _zs_1buy.zg - (_zs_1buy.zg - _zs_1buy.zd) / 2
        if zs_min_price > _zs_1buy_mid_price:
            return {
                "code": cd.get_code(),
                "msg": f"一买后形成中枢，且中枢低点 {zs_min_price} 高于 一买中枢的中心 {_zs_1buy_mid_price}",
            }

    return None


def xg_single_find_qs_by_zhuanzhe_zs(
    code: str, mk_datas: MarketDatas, opt_type: list = []
):
    """
    找趋势转折后的第一个中枢
    周期：单周期
    使用市场：沪深A股
    作者：WX
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_bis()) <= 5 or len(cd.get_xds()) < 2 or len(cd.get_bi_zss()) < 3:
        return None
    zss = cd.get_bi_zss()
    # 判断倒数第二、三个中枢是否构成趋势（up/down）
    zs_qs = cd.zss_is_qs(zss[-3], zss[-2])
    if zs_qs not in opt_direction:
        return None

    if zss[-2].lines[0].index - zss[-3].lines[-1].index >= 3:
        return None
    zs = zss[-1]
    # 最新中枢的进入段方向须与趋势相反，才是真正的转折
    if zs.lines[0].type == zs_qs:
        return None
    # 最新中枢仍在进行中（最后一笔即当前笔）
    bi = cd.get_bis()[-1]
    if zs.lines[-1].index != bi.index:
        return None
    # 两个中枢间距过大说明已充分发展，不再是"第一个转折中枢"
    if zs.lines[0].index - zss[-2].lines[-1].index >= 5:
        return None
    return {"code": cd.get_code(), "msg": "出现趋势转折的第一个中枢了"}


def xg_single_xdzs_bimmdbc(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """向上线段中枢内，笔回调至中枢 zd 附近（±8%）且有买卖点或背驰信号。
    周期：单周期
    适用市场：沪深A股
    """
    deviation_rate = 0.08
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_xd_zss()) == 0:
        return None
    xd_zs = cd.get_xd_zss()[-1]
    xd = cd.get_xds()[-1]
    # 线段中枢进入段必须向上（排除下跌中枢）
    if xd_zs.lines[0].type != "up":
        return None
    if xd_zs.lines[-1].type == "up":
        return None
    if xd_zs.lines[-1].index != xd.index:
        return None
    if xd_zs.line_num >= 9:
        return None
    # 进入段起点必须是中枢最低点，否则不是标准上涨中枢形态
    if xd_zs.lines[0].low != min([_l.low for _l in xd_zs.lines]):
        return None
    if "1sell" in xd_zs.zs_mmds() or "2sell" in xd_zs.zs_mmds():
        return None

    bi = cd.get_bis()[-1]
    if bi.type != "down":
        bi = cd.get_bis()[-2]
    if len(bi.line_mmds()) == 0 and len(bi.line_bcs()) == 0:
        return None

    # 笔结束价格落在 zd ±deviation_rate 区间内，视为回调到位
    if xd_zs.zd * (1 - deviation_rate) <= bi.end.val <= xd_zs.zd * (1 + deviation_rate):
        return {
            "code": code,
            "msg": "向上线段中枢，笔回调到线段zd附近，有买卖点背驰信号",
        }


def xg_single_week_k_overlap(code: str, mk_datas: MarketDatas, opt_type: list = []):
    """
    周线级别，k线重叠；理论与操作，在周线长时间盘整，积聚理论，一旦突破重叠区间，往往有可观的上涨区间
    （注意：大级别可用，小级别不可用）
    周期：单周期
    适用市场：沪深A股
    作者：WX
    """
    klines_week: pd.DataFrame = mk_datas.klines(code, mk_datas.frequencys[0])
    if len(klines_week) < 100:
        return None
    # 先验证最新三根 K 线之间有重叠，作为"重叠区间"基准
    cross_low = klines_week.iloc[-3:]["low"].max()
    cross_high = klines_week.iloc[-3:]["high"].min()
    if cross_low > cross_high:
        return None
    cross_low = klines_week.iloc[-3:]["low"].min()
    cross_high = klines_week.iloc[-3:]["high"].max()
    overlap_nums = 0
    for _i in range(1, len(klines_week) - 1):
        _lh = max(klines_week.iloc[-_i]["low"], cross_low)
        _hl = min(klines_week.iloc[-_i]["high"], cross_high)
        # 连续三根也须互相重叠，避免孤立 K 线误入重叠段计数
        if _i > 3:
            _three_overlap = (
                klines_week.iloc[-_i : -(_i - 3)]["low"].max()
                < klines_week.iloc[-_i : -(_i - 3)]["high"].min()
            )
        else:
            _three_overlap = True
        if _lh < _hl and _three_overlap:
            overlap_nums += 1
        else:
            break

    if overlap_nums < 13:
        return None

    # 重叠区间的成交量须不低于前期，说明筹码仍然活跃而非死水
    overlap_avg_volume = klines_week.iloc[-overlap_nums:]["volume"].mean()
    pre_avg_volume = klines_week.iloc[-(overlap_nums * 2) : -overlap_nums][
        "volume"
    ].mean()
    if overlap_avg_volume < pre_avg_volume:
        return None

    return {
        "code": code,
        "msg": f"周线重叠，重叠数量：{overlap_nums}，有可观的上涨区间",
    }


def xg_single_xd_next_zz(
    code: str, mk_datas: MarketDatas, opt_type: list = []
) -> Union[None, dict]:
    """
    单周期，查找线段结束转折要转折的的标的
    要求：
    1. 线段内部必须有两个或以上的中枢
    2. 当前的笔方向与线段方向要一致，并且（下跌笔的低点要高于线段的低点，上涨笔的高点要低于线段的高点）
    3. 笔是线段结束笔的下一笔
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_xds()) == 0:
        return None
    xd = cd.get_xds()[-1]
    bi = cd.get_bis()[-1]
    if xd.type not in opt_direction:
        return None
    if xd.type != bi.type:
        return None
    # 当前笔须是线段结束笔的下下一笔（+2），即转折后的第一笔
    if bi.index != xd.end_line.index + 2:
        return None
    # 转折笔须未突破线段极值，否则不是"未转折"形态
    if bi.type == "down" and bi.low < xd.end_line.low:
        return None
    if bi.type == "up" and bi.high > xd.end_line.high:
        return None
    # 线段内至少有两个中枢，才符合"多中枢震荡后转折"逻辑
    xd_zss = [
        _zs
        for _zs in cd.get_bi_zss()
        if _zs.lines[1].start.k.date > xd.start.k.date
        and _zs.lines[1].start.k.date > xd.end.k.date
    ]
    if len(xd_zss) < 2:
        return None
    return {
        "code": code,
        "msg": f"线段内有{len(xd_zss)}个中枢，笔是线段结束笔的下下一笔，笔方向与线段方向一致，且笔价格在线段价格附近",
    }


def xg_single_xd_zs_nei_3mmds(
    code: str, mk_datas: MarketDatas, opt_type: list = []
) -> Union[None, dict]:
    """
    单周期，查找线段中枢内的三类买卖点
    要求：
    1. 必须有线段中枢，并且线段中枢包括进入段数量要大于等于7
    2. 最新的线段要在线段中枢内部，自线段其实的位置，只有一个三类买卖点
    """
    opt_direction, opt_mmd = get_opt_types(opt_type)
    cd = mk_datas.get_cl_data(code, mk_datas.frequencys[0])
    if len(cd.get_xd_zss()) == 0:
        return None
    xd = cd.get_xds()[-1]
    xd_zs = cd.get_xd_zss()[-1]
    bi = cd.get_bis()[-1]
    if len(xd_zs.lines) < 7:
        return None
    if xd.index not in [_l.index for _l in xd_zs.lines]:
        return None

    if bi.type not in opt_direction:
        return None

    check_mmds = []
    if "down" in opt_direction:
        check_mmds.extend(["3buy", "l3buy"])
    if "up" in opt_direction:
        check_mmds.extend(["3sell", "l3sell"])

    if bi.mmd_exists(check_mmds, "|") is False:
        return None

    # 进入段起点须是中枢内最高/最低点，确保中枢结构标准
    xd_zs_high = max([_l.high for _l in xd_zs.lines])
    xd_zs_low = min([_l.low for _l in xd_zs.lines])
    if xd_zs.lines[0].type == "down" and xd_zs_high != xd_zs.lines[0].high:
        return None
    if xd_zs.lines[0].type == "up" and xd_zs_low != xd_zs.lines[0].low:
        return None

    # 仅在线段内第一次出现三买/三卖时提醒，避免重复信号
    mmd_count = 0
    for _bi in cd.get_bis():
        if _bi.index < xd.start_line.index:
            continue
        if _bi.type == bi.type and _bi.mmd_exists(["3buy", "3sell"], "|"):
            mmd_count += 1

    if mmd_count >= 2:
        return None

    return {
        "code": code,
        "msg": f"线段中枢盘整后出现笔的三类买卖点",
    }


if __name__ == "__main__":
    from chanlun.cl_utils import query_cl_chart_config
    from chanlun.exchange.exchange_tdx import ExchangeTDX
    from chanlun.trader.online_market_datas import OnlineMarketDatas

    market = "a"
    code = "SZ.000017"
    freqs = ["d"]

    ex = ExchangeTDX()
    cl_config = query_cl_chart_config(market, code)
    mkd = OnlineMarketDatas(market, freqs, ex, cl_config)

    res = xg_single_xd_zs_nei_3mmds(code, mkd, opt_type=["long", "short"])
    print(res)
