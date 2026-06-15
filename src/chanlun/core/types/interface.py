# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
from abc import ABCMeta, abstractmethod
from typing import Any, List, Tuple, Union

import numpy as np
import pandas as pd

from chanlun.core.types.kline import CLKline, FX, Kline
from chanlun.core.types.line import BI, LINE, XD
from chanlun.core.types.zhongshu import ZS


class ICL(metaclass=ABCMeta):
    """
    缠论数据分析接口定义
    """

    @abstractmethod
    def __init__(
        self,
        code: str,
        frequency: str,
        config: Union[dict, None] = None,
        start_datetime: datetime.datetime = None,
    ):
        """
        缠论计算
        :param code: 代码
        :param frequency: 周期
        :param config: 计算缠论依赖的配置项
        :param start_datetime: 开始分析的时间，不设置则分析计算所有
        """

    @abstractmethod
    def process_klines(self, klines: pd.DataFrame):
        """
        计算k线缠论数据
        传递 pandas 数据，需要包括以下列：
            date  时间日期  datetime 格式，对于在 DataFrame 中 date 不是日期格式的，需要执行 pd.to_datetime 方法转换下
            high  最高价
            low   最低价
            open  开盘价
            close  收盘价
            volume  成交量

        可增量多次调用，重复已计算的会自动跳过，最后一个 bar 会进行更新
        """
        pass

    @abstractmethod
    def get_code(self) -> str:
        """
        返回计算的标的代码
        """
        pass

    @abstractmethod
    def get_frequency(self) -> str:
        """
        返回计算的周期参数
        """
        pass

    def get_config(self) -> dict:
        """
        返回计算时使用的缠论配置项
        """
        pass

    @abstractmethod
    def get_src_klines(self) -> List[Kline]:
        """
        返回原始K线列表
        """
        pass

    @abstractmethod
    def get_klines(self) -> List[Any]:
        """
        返回K线列表
        如果 kline_type == kline_default 则返回原始 K 线数据
        如果 kline_type == kline_chanlun 则返回缠论 K 线数据
        如需获取原始K线数据，使用 get_src_klines 方法
        """
        pass

    @abstractmethod
    def get_cl_klines(self) -> List[CLKline]:
        """
        返回合并后的缠论K线列表
        """
        pass

    @abstractmethod
    def get_idx(self) -> dict:
        """
        返回计算的指标数据
        """
        pass

    @abstractmethod
    def get_fxs(self) -> List[FX]:
        """
        返回缠论分型列表
        """
        pass

    @abstractmethod
    def get_bis(self) -> List[BI]:
        """
        返回计算缠论笔列表
        """
        pass

    @abstractmethod
    def get_xds(self) -> List[XD]:
        """
        返回计算缠论线段列表
        """
        pass

    @abstractmethod
    def get_bi_zss(self, zs_type: str = None) -> List[ZS]:
        """
        返回计算缠论笔中枢列表
        """
        pass

    @abstractmethod
    def get_xd_zss(self, zs_type: str = None) -> List[ZS]:
        """
        返回计算缠论线段中枢（走势中枢）
        """
        pass

    @abstractmethod
    def get_last_bi_zs(self) -> Union[ZS, None]:
        """
        返回最后的笔中枢，根据最后几笔倒推出的笔中枢，和 self.get_bi_zss()[-1] 方式获取的中枢不一定一致
        """
        pass

    @abstractmethod
    def get_last_xd_zs(self) -> Union[ZS, None]:
        """
        返回最后的线段中枢，需要 CL_CAL_LAST_ZS 设置为 True 才会有中枢
        """
        pass

    @abstractmethod
    def create_dn_zs(
        self,
        zs_type: str,
        lines: List[LINE],
        max_line_num: int = 999,
        zs_include_last_line=True,
    ) -> List[ZS]:
        """
        创建段内中枢
        @param zs_type: 中枢类型：bi 笔中枢 xd 线段中枢
        @param lines: 线的列表
        @param max_line_num: 中枢最大线段数量
        @param zs_include_last_line: 如果中枢最后一笔不是最高 or 最低，在中枢内部，中枢区间是否包含最后一笔，默认包含
        """
        pass

    @abstractmethod
    def beichi_pz(self, zs: ZS, now_line: LINE) -> Tuple[bool, Union[LINE, None]]:
        """
        判断中枢与指定线是否构成盘整背驰
        @param zs: 中枢
        @param now_line: 需要对比的线
        """

    @abstractmethod
    def beichi_qs(
        self, lines: List[LINE], zss: List[ZS], now_line: LINE
    ) -> Tuple[bool, List[LINE]]:
        """
        判断指定线与之前的中枢，是否形成了趋势背驰

        @parma lines: 线的列表，用来查找之前的线
        @param zss：中枢列表，用来获取最后两个中枢，判断配置关系
        @param now_line: 最后一个线
        """

    @abstractmethod
    def zss_is_qs(self, one_zs: ZS, two_zs: ZS) -> Tuple[str, None]:
        """
        判断两个中枢是否形成趋势（根据设置的位置关系配置，来判断两个中枢是否有重叠）
        返回  up 是向上趋势， down 是向下趋势 ，None 则没有趋势
        """


def _resolve_ld_macd(cd: ICL) -> dict:
    """选择背驰力度计算所用的 MACD 源。

    优先用高周期 MACD（``cd._htf_macd``）—— 本项目以线段为最低级别走势
    类型，度量其背驰力度应提高一个级别（1m→5m、5m→30m…）。高周期 MACD
    不可用（开关关 / 无高周期 / 桶不足 → CL 置 None）时回退原生 MACD。

    高周期数组与原生一样按原始 K 线逐根对齐（per-bar 插值），故调用方的
    ``k_index`` 切片逻辑对两者通用。
    """
    htf = getattr(cd, "_htf_macd", None)
    if isinstance(htf, dict) and htf.get("hist"):
        return htf
    return cd.get_idx()["macd"]


def query_macd_ld(cd: ICL, start_fx: FX, end_fx: FX):
    """
    计算分型区间 macd 力度
    实际比较力度是根据 hist 的 up_sum 和 down_sum 进行比较
    向上线段，比较 up_sum 红柱子总和
    向下线段，比较 down_sum 绿柱子总和
    """
    if start_fx.index > end_fx.index:
        raise Exception(
            "%s - %s - %s 计算力度，开始分型不可以大于结束分型"
            % (cd.get_code(), cd.get_frequency(), cd.get_klines()[-1].date)
        )

    # 背驰力度的 MACD 源：开关开且高周期 MACD 可用 → 用高一周期 MACD
    # （线段是最低级别走势类型，力度应提高一级度量）；否则回退原生 MACD。
    # 两者数组均与原始 K 线等长，下面统一按 k_index 切片。
    macd = _resolve_ld_macd(cd)
    dea = np.array(macd["dea"][start_fx.k.k_index : end_fx.k.k_index + 1])
    dif = np.array(macd["dif"][start_fx.k.k_index : end_fx.k.k_index + 1])
    hist = np.array(macd["hist"][start_fx.k.k_index : end_fx.k.k_index + 1])
    if len(hist) == 0:
        hist = np.array([0])
    if len(dea) == 0:
        dea = np.array([0])
    if len(dif) == 0:
        dif = np.array([0])

    hist_abs = abs(hist)
    hist_max = np.max(hist)
    hist_min = np.min(hist)
    hist_sum = hist_abs.sum()
    # 向量化正/负柱求和(原 Python 列表推导 [_i for _i in hist if ...] 是递归层背驰
    # 力度查询的主热点:每段 O(段长) Python 循环 ×1.2万次/2000bar)。numpy 布尔索引
    # 同序 ⇒ 求和逐位一致;hist_sum 仍由 abs().sum() 独立算(不从 up+down 推导)以保
    # 浮点求和顺序不变、背驰比较结果不漂移。
    hist_up_sum = hist[hist > 0].sum()
    hist_down_sum = abs(hist[hist < 0].sum())
    end_dea = dea[-1]
    end_dif = dif[-1]
    end_hist = hist[-1]
    return {
        "dea": {"end": end_dea, "max": np.max(dea), "min": np.min(dea)},
        "dif": {"end": end_dif, "max": np.max(dif), "min": np.min(dif)},
        "hist": {
            "sum": hist_sum,
            "up_sum": hist_up_sum,
            "down_sum": hist_down_sum,
            "max": hist_max,
            "min": hist_min,
            "end": end_hist,
        },
    }


def compare_ld_beichi(
    one_ld: dict, two_ld: dict, line_direction: str,
    dif_check: bool = False,
):
    """
    比较两个力度，后者小于前者，返回 True

    **口径差异(§3.10)**：本函数仅做柱子面积比较——是原文细则1的子集。原文完整
    口径(柱子面积 + 级别相关黄白线 + 0 轴回抽 + 创新高前提)见
    ``chanlun.core.beichi_calculator.is_beichi``,新代码建议直接用该函数。
    本函数为兼容遗留调用方(``cl_analyse`` / ``signal_monitor`` / 旧 user_custom_mmd)
    保留,可选 ``dif_check=True`` 启用黄白线高度衰减作渐进升级路径。

    :param one_ld: 前段力度(query_macd_ld 风格,含 ``macd`` 键)
    :param two_ld: 后段力度
    :param line_direction: ``'up'`` / ``'down'`` 决定看红柱/绿柱
    :param dif_check: 默认 False(向后兼容,仅看柱子面积)。True 时再叠加
        黄白线高度衰减(``up: dif.max 后 < 前;down: dif.min 后 > 前``),
        更贴近原文细则1。**未含 0 轴回抽**——需要的话用 ``is_beichi``。
    """
    hist_key = "sum"
    if line_direction == "up":
        hist_key = "up_sum"
    elif line_direction == "down":
        hist_key = "down_sum"
    if "macd" not in two_ld.keys() or "macd" not in one_ld.keys():
        return False
    if not (two_ld["macd"]["hist"][hist_key] < one_ld["macd"]["hist"][hist_key]):
        return False
    if dif_check:
        if line_direction == "up":
            if not (two_ld["macd"]["dif"]["max"] < one_ld["macd"]["dif"]["max"]):
                return False
        elif line_direction == "down":
            if not (two_ld["macd"]["dif"]["min"] > one_ld["macd"]["dif"]["min"]):
                return False
    return True


def user_custom_mmd(
    cd: ICL,
    line: Union[BI, XD],
    lines: List[Union[BI, XD]],
    zs_type: str,
    zss: List[ZS],
):
    """
    用户可自定义买卖点
    每次笔or线段更新，计算完系统默认买卖点后，会执行这个方法，用户可按照自己的规则，给当前线段增加买卖点

    这里示例增加类二类三类买卖点

    @param cd: 缠论数据对象
    @param line: 要计算买卖点的线
    @param lines：要计算线类型的列表
    @param zs_type: 中枢类型 ，参考中枢类型配置项 取值 zs_type_bz、zs_type_dn、zs_type_fx
    """
    # 清空买卖点与背驰情况，重新计算

    if len(lines) < 4 or len(zss) == 0:
        return False
    # line.index < 2 时 lines[line.index - 2] 会负索引环绕取到错误的线，提前返回。
    if line.index < 2:
        return False

    # 类二类买卖点，如果前一笔同向的线段出现二类买卖点，当前与二类买卖点笔有重叠（形成中枢），不创前一笔的高点或低点，增加类二类买卖点
    pre_same_line = lines[line.index - 2]
    for mmd in pre_same_line.get_mmds(zs_type):
        if line.type == "down" and mmd.name == "2buy" and line.low > pre_same_line.low:
            new_zss = cd.create_dn_zs("", lines[-4:])  # 自一类买卖点后形成的中枢
            if len(new_zss) != 1:
                continue
            line.add_mmd(
                "l2buy",
                new_zss[0],
                zs_type,
                msg="二买后，重叠中枢后，不创二买低点，形成类二买",
            )
        if line.type == "up" and mmd.name == "2sell" and line.high < pre_same_line.high:
            new_zss = cd.create_dn_zs("", lines[-4:])  # 自一类买卖点后形成的中枢
            if len(new_zss) != 1:
                continue
            line.add_mmd(
                "l2sell",
                new_zss[0],
                zs_type,
                msg="二卖后，重叠中枢后，不创二卖高点，形成类二卖",
            )

    # 类三类买卖点，如果前一笔同向的线段出现三类买卖点，当前与三类买卖点笔有重叠（形成中枢），不创前中枢的高低点，并且力度笔三类买卖点小，增加类三类买卖点
    for mmd in pre_same_line.get_mmds(zs_type):
        if (
            line.type == "down"
            and mmd.name == "3buy"
            and line.low > mmd.zs.zg
            and compare_ld_beichi(pre_same_line.get_ld(cd), line.get_ld(cd), "down")
        ):
            new_zss = cd.create_dn_zs(
                "", lines[-4:]
            )  # 从三买的前一段到现在一段，共4段形成的中枢
            if len(new_zss) != 1:
                continue
            line.add_mmd(
                "l3buy",
                new_zss[0],
                zs_type,
                msg="三买后，重叠中枢后，不创三买低点，形成类三买",
            )
        if (
            line.type == "up"
            and mmd.name == "3sell"
            and line.high < mmd.zs.zd
            and compare_ld_beichi(pre_same_line.get_ld(cd), line.get_ld(cd), "up")
        ):
            new_zss = cd.create_dn_zs(
                "", lines[-4:]
            )  # 从三卖的前一段到现在一段，共4段形成的中枢
            if len(new_zss) != 1:
                continue
            line.add_mmd(
                "l3sell",
                new_zss[0],
                zs_type,
                msg="三卖后，重叠中枢后，不创三卖高点，形成类三卖",
            )

    return True
