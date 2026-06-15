from typing import Tuple

import numpy as np
import pandas as pd

from chanlun.core.types import BI, ICL


def bi_td(bi: BI, cd: ICL):
    """
    判断是否笔停顿
    """
    if bi.is_done() is False:
        return False
    next_ks = cd.get_klines()[bi.end.klines[-1].k_index + 1 :]
    if len(next_ks) == 0:
        return False
    for _nk in next_ks:
        if bi.type == "up" and _nk.c < _nk.o and _nk.c < bi.end.klines[-1].l:
            return True
        elif bi.type == "down" and _nk.c > _nk.o and _nk.c > bi.end.klines[-1].h:
            return True

    return False


def up_cross(one_list: np.array, two_list: np.array):
    """
    获取上穿信号列表
    """
    assert len(one_list) == len(two_list), "信号输入维度不相等"
    if len(one_list) < 2:
        return []
    cross = []
    for i in range(1, len(two_list)):
        if one_list[i - 1] < two_list[i - 1] and one_list[i] > two_list[i]:
            cross.append(i)
    return cross


def down_cross(one_list: np.array, two_list: np.array):
    """
    获取下穿信号列表
    """
    assert len(one_list) == len(two_list), "信号输入维度不相等"
    if len(one_list) < 2:
        return []
    cross = []
    for i in range(1, len(two_list)):
        if one_list[i - 1] > two_list[i - 1] and one_list[i] < two_list[i]:
            cross.append(i)
    return cross


def last_done_bi(cd: ICL):
    """
    获取最后一个 完成笔
    """
    bis = cd.get_bis()
    if len(bis) == 0:
        return None
    for bi in bis[::-1]:
        if bi.is_done():
            return bi
    return None


def bi_qk_num(cd: ICL, bi: BI) -> Tuple[int, int]:
    """
    获取笔的缺口数量（分别是向上跳空，向下跳空数量）
    """
    up_qk_num = 0
    down_qk_num = 0
    _ks = cd.get_src_klines()[bi.start.k.k_index : bi.end.k.k_index + 1]
    for i in range(1, len(_ks)):
        pre_k = _ks[i - 1]
        now_k = _ks[i]
        if now_k.l > pre_k.h:
            up_qk_num += 1
        elif now_k.h < pre_k.l:
            down_qk_num += 1
    return up_qk_num, down_qk_num


def klines_to_heikin_ashi_klines(ks: pd.DataFrame) -> pd.DataFrame:
    """
    将缠论数据的普通K线，转换成平均K线数据，返回格式 pd.DataFrame
    """
    cd_klines = ks.to_dict(orient="records")

    # 平均K线公式：open=(前开+前收)/2，close=(开+高+低+收)/4，high/low 取与 open/close 的极值
    mean_klines: list = []
    for i in range(len(cd_klines)):
        if i == 0:
            mean_klines.append(cd_klines[i])
            continue
        mk = mean_klines[i - 1]
        nk = cd_klines[i]
        _open = (mk["open"] + mk["close"]) / 2
        _close = (nk["open"] + nk["high"] + nk["low"] + nk["close"]) / 4
        _high = max(nk["high"], _open, _close)
        _low = min(nk["low"], _open, _close)
        _volume = nk["volume"]
        mean_klines.append(
            {
                "code": nk["code"],
                "date": nk["date"],
                "high": _high,
                "open": _open,
                "low": _low,
                "close": _close,
                "volume": _volume,
            }
        )

    df = pd.DataFrame(mean_klines)
    return df
