from typing import List, Tuple

import numpy as np

from chanlun.core.types import BI, FX, ICL, LINE, MACD_INFOS, ZS, Kline

from chanlun.cl_utils.indicators import up_cross, down_cross


def cal_klines_macd_infos(start_k: Kline, end_k: Kline, cd: ICL) -> MACD_INFOS:
    """
    计算线中macd信息
    """
    infos = MACD_INFOS()

    idx = cd.get_idx()
    dea = np.array(idx["macd"]["dea"][start_k.index : end_k.index + 1])
    dif = np.array(idx["macd"]["dif"][start_k.index : end_k.index + 1])
    if len(dea) < 2 or len(dif) < 2:
        return infos
    zero = np.zeros(len(dea))

    infos.dif_up_cross_num = len(up_cross(dif, zero))
    infos.dif_down_cross_num = len(down_cross(dif, zero))
    infos.dea_up_cross_num = len(up_cross(dea, zero))
    infos.dea_down_cross_num = len(down_cross(dea, zero))
    infos.gold_cross_num = len(up_cross(dif, dea))
    infos.die_cross_num = len(down_cross(dif, dea))
    infos.last_dif = dif[-1]
    infos.last_dea = dea[-1]
    return infos


def cal_line_macd_infos(line: LINE, cd: ICL) -> MACD_INFOS:
    """
    计算线中macd信息
    """
    infos = MACD_INFOS()

    idx = cd.get_idx()
    dea = np.array(idx["macd"]["dea"][line.start.k.k_index : line.end.k.k_index + 1])
    dif = np.array(idx["macd"]["dif"][line.start.k.k_index : line.end.k.k_index + 1])
    if len(dea) < 2 or len(dif) < 2:
        return infos
    zero = np.zeros(len(dea))

    infos.dif_up_cross_num = len(up_cross(dif, zero))
    infos.dif_down_cross_num = len(down_cross(dif, zero))
    infos.dea_up_cross_num = len(up_cross(dea, zero))
    infos.dea_down_cross_num = len(down_cross(dea, zero))
    infos.gold_cross_num = len(up_cross(dif, dea))
    infos.die_cross_num = len(down_cross(dif, dea))
    infos.last_dif = dif[-1]
    infos.last_dea = dea[-1]
    return infos


def cal_macd_bis_is_bc(bis: List[BI], cd: ICL) -> Tuple[bool, bool]:
    """
    给定一组笔列表，判断其 macd 是否出现背驰，柱子高度变小，黄白线也缩小

    条件：
        1. 结束笔是向下，则需要结束笔的低点是给定笔的最低点
        2. 结束笔是向上，则需要结束笔的高点是给定笔的最高点

    只获取最后两块 红绿柱子的情况进行对比

    返回：
        bool 第一个值是 红绿柱子是否背驰
        bool 第二个值是 黄白线是否背驰
    """
    if len(bis) < 3:
        return False, False

    # 最后一笔不是最高或最低
    direction = bis[-1].type
    if direction == "up":
        bi_max_val = max([_bi.high for _bi in bis])
        if bi_max_val != bis[-1].high:
            return False, False
    else:
        bi_min_val = min([_bi.low for _bi in bis])
        if bi_min_val != bis[-1].low:
            return False, False

    macd_idx = cd.get_idx()["macd"]
    # 如果最后一笔内部没有找到 红绿柱子，则直接返回 True
    if direction == "up":
        macd_up_hist_max_val = max(
            macd_idx["hist"][bis[-1].start.k.k_index : bis[-1].end.k.k_index + 1]
        )
        if macd_up_hist_max_val <= 0:
            return True, True
    elif direction == "down":
        macd_down_hist_min_val = min(
            macd_idx["hist"][bis[-1].start.k.k_index : bis[-1].end.k.k_index + 1]
        )
        if macd_down_hist_min_val >= 0:
            return True, True

    # 黄白线在给定的笔区间内部，至少有一次穿越零轴
    start_k = cd.get_klines()[bis[0].start.k.k_index]
    end_k = cd.get_klines()[bis[-1].end.k.k_index]
    bis_macd_infos = cal_klines_macd_infos(start_k, end_k, cd)
    if direction == "up" and (
        bis_macd_infos.dea_up_cross_num == 0 or bis_macd_infos.dif_up_cross_num == 0
    ):
        return False, False
    if direction == "down" and (
        bis_macd_infos.dif_down_cross_num == 0 or bis_macd_infos.dif_down_cross_num == 0
    ):
        return False, False

    def get_macd_dump_info(start_fx: FX, end_fx: FX):
        # 获取给定区间内，hist dif dea 最大值，hist 出现的驼峰值列表
        start_k_index = start_fx.klines[0].k_index
        end_k_index = (
            end_fx.klines[-1].k_index
            if end_fx.klines[-1] is not None
            else end_fx.klines[1].k_index
        )
        # 根据红绿柱子的边界获取
        while True:
            if direction == "up":
                if start_k_index > 0 and macd_idx["hist"][start_k_index] > 0:
                    start_k_index -= 1
                else:
                    break
            if direction == "down":
                if start_k_index > 0 and macd_idx["hist"][start_k_index] < 0:
                    start_k_index -= 1
                else:
                    break
        while True:
            if direction == "up":
                if (
                    end_k_index < len(macd_idx["hist"])
                    and macd_idx["hist"][end_k_index] > 0
                ):
                    end_k_index += 1
                else:
                    break
            if direction == "down":
                if (
                    end_k_index < len(macd_idx["hist"])
                    and macd_idx["hist"][end_k_index] < 0
                ):
                    end_k_index += 1
                else:
                    break

        macd_hists = macd_idx["hist"][start_k_index : end_k_index + 1]
        macd_difs = macd_idx["dif"][start_k_index : end_k_index + 1]
        macd_deas = macd_idx["dea"][start_k_index : end_k_index + 1]

        max_hist = max_dif = max_dea = 0
        hist_dumps = []
        _temp_dumps = []
        for i in range(len(macd_hists)):
            _hist = macd_hists[i]
            _dif = macd_difs[i]
            _dea = macd_deas[i]
            if direction == "up" and _hist > 0:
                _temp_dumps.append(_hist)
                max_hist = max(max_hist, _hist)
            if direction == "up" and _dif > 0:
                max_dif = max(max_dif, _dif)
            if direction == "up" and _dea > 0:
                max_dea = max(max_dea, _dea)
            if direction == "down" and _hist < 0:
                _temp_dumps.append(abs(_hist))
                max_hist = max(max_hist, abs(_hist))
            if direction == "down" and _dif < 0:
                max_dif = max(max_dif, abs(_dif))
            if direction == "down" and _dea < 0:
                max_dea = max(max_dea, abs(_dea))
            if (direction == "up" and _hist < 0) or (direction == "down" and _hist > 0):
                if len(_temp_dumps) > 0:
                    hist_dumps.append(_temp_dumps)
                    _temp_dumps = []
        if len(_temp_dumps) > 0:
            hist_dumps.append(_temp_dumps)

        return max_hist, max_dif, max_dea, hist_dumps

    # 计算最后一笔的 macd 信息
    (
        last_bi_max_hist,
        last_bi_max_dif,
        last_bi_max_dea,
        last_bi_hist_dumps,
    ) = get_macd_dump_info(bis[-1].start, bis[-1].end)
    last_bi_sum_hist = sum([sum(_hists) for _hists in last_bi_hist_dumps])
    # 根据中枢数量确定比较基准段
    zss = cd.create_dn_zs("bi", bis)
    if len(zss) == 0:
        # 没有中枢，就用上一笔
        compare_start_fx = bis[-3].start
        compare_end_fx = bis[-3].end
    else:
        # 有中枢，根据最后一个中枢获取，这里也分两种情况，中枢最后一笔是否为给定笔的最后一笔
        if zss[-1].lines[-1].index == bis[-1].index:
            # 是中枢最后一笔
            compare_start_fx = zss[-1].lines[0].start
            compare_end_fx = zss[-1].lines[-2].end
        else:
            # 不是中枢最后一笔
            compare_start_fx = zss[-1].lines[0].start
            compare_end_fx = zss[-1].lines[-1].end

    (
        compare_max_hist,
        compare_max_dif,
        compare_max_dea,
        compare_hist_dumps,
    ) = get_macd_dump_info(compare_start_fx, compare_end_fx)
    compare_max_sum_hist = max([sum(_hists) for _hists in compare_hist_dumps])

    hist_bc = False
    deadif_bc = False
    if last_bi_max_hist < compare_max_hist and last_bi_sum_hist < compare_max_sum_hist:
        hist_bc = True
    if last_bi_max_dif < compare_max_dif and last_bi_max_dea < compare_max_dea:
        deadif_bc = True

    return hist_bc, deadif_bc


def cal_zs_macd_infos(zs: ZS, cd: ICL) -> MACD_INFOS:
    """
    计算中枢的macd信息
    """
    infos = MACD_INFOS()
    dea = np.array(
        cd.get_idx()["macd"]["dea"][zs.start.k.k_index : zs.end.k.k_index + 1]
    )
    dif = np.array(
        cd.get_idx()["macd"]["dif"][zs.start.k.k_index : zs.end.k.k_index + 1]
    )
    if len(dea) < 2 or len(dif) < 2:
        return infos
    zero = np.zeros(len(dea))

    infos.dif_up_cross_num = len(up_cross(dif, zero))
    infos.dif_down_cross_num = len(down_cross(dif, zero))
    infos.dea_up_cross_num = len(up_cross(dea, zero))
    infos.dea_down_cross_num = len(down_cross(dea, zero))
    infos.gold_cross_num = len(up_cross(dif, dea))
    infos.die_cross_num = len(down_cross(dif, dea))
    infos.last_dif = dif[-1]
    infos.last_dea = dea[-1]
    return infos
