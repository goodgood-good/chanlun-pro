# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import List, Union

from chanlun.core.types.line import LINE, BI, XD
from chanlun.core.types.zhongshu import ZS


@dataclass
class LOW_LEVEL_QS:
    zss: List[ZS]  # 低级别线构成的中枢列表
    lines: List[Union[LINE, BI, XD]]  # 包含的低级别线
    zs_num: int = 0
    line_num: int = 0
    bc_line: Union[LINE, None] = None  # 背驰的线
    last_line: Union[LINE, BI, XD, None] = None  # 最后一个线
    qs: bool = False  # 是否形成趋势
    pz: bool = False  # 是否形成盘整
    line_bc: bool = False  # 是否形成（笔、线段）背驰
    qs_bc: bool = False  # 是否趋势背驰
    pz_bc: bool = False  # 是否盘整背驰

    def to_dict(self):
        """将LOW_LEVEL_QS对象转换为字典"""
        return {
            'zss': [zs.to_dict() for zs in self.zss],
            'lines': [line.to_dict() for line in self.lines],
            'zs_num': self.zs_num,
            'line_num': self.line_num,
            'bc_line': self.bc_line.to_dict() if self.bc_line else None,
            'last_line': self.last_line.to_dict() if self.last_line else None,
            'qs': self.qs,
            'pz': self.pz,
            'line_bc': self.line_bc,
            'qs_bc': self.qs_bc,
            'pz_bc': self.pz_bc,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass
class MACD_INFOS:
    # 记录中枢内，macd 的变化情况
    # R2-F1-2: 必须带类型注解才构成 dataclass field(无注解=普通类属性,
    # fields()==0/to_dict() 恒 {}/值不同的实例 __eq__ 恒 True)
    dif_up_cross_num: int = 0  # dif 线上穿零轴的次数
    dea_up_cross_num: int = 0  # dea 线上穿零轴的次数
    dif_down_cross_num: int = 0  # dif 线下穿零轴的次数
    dea_down_cross_num: int = 0  # dea 线下穿零轴的次数
    gold_cross_num: int = 0  # 金叉次数
    die_cross_num: int = 0  # 死叉次数
    last_dif: float = 0.0
    last_dea: float = 0.0

    def to_dict(self):
        """将MACD_INFOS对象转换为字典"""
        return asdict(self)

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)



@dataclass
class LINE_FORM_INFOS:
    # 组成形态的线列表
    lines: List[Union[LINE, BI, XD]]
    # 方向
    direction: str
    # 线的数量
    line_num: int
    # 线的形态描述
    form_type: str
    # 线组成的中枢信息
    zss: Union[None, List[ZS]] = None
    # 最后线是否背驰段
    is_bc_line: bool = False
    # 形态级别
    form_level: float = 0
    # 形态趋势
    form_qs: str = ""
    # 其他信息
    infos: dict = None

    def to_dict(self):
        """将LINE_FORM_INFOS对象转换为字典"""
        return {
            'lines': [line.to_dict() for line in self.lines],
            'direction': self.direction,
            'line_num': self.line_num,
            'form_type': self.form_type,
            'zss': [zs.to_dict() for zs in self.zss] if self.zss else None,
            'is_bc_line': self.is_bc_line,
            'form_level': self.form_level,
            'form_qs': self.form_qs,
            'infos': self.infos,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


@dataclass
class BW_LINE_QS_INFOS:
    """
    倒推线段的趋势信息
    """

    # 线段的组成
    lines: List[Union[LINE, BI, XD]]
    # 中枢列表
    zss: List[ZS]
    # 中枢类型拼接字符串
    zss_str = ""
    # 走势类型描述
    zoushi_type_str = ""

    def to_dict(self):
        """将BW_LINE_QS_INFOS对象转换为字典"""
        return {
            'lines': [line.to_dict() for line in self.lines],
            'zss': [zs.to_dict() for zs in self.zss],
            'zss_str': self.zss_str,
            'zoushi_type_str': self.zoushi_type_str,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
