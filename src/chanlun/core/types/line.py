# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from abc import abstractmethod
from typing import List, Union

from chanlun.core.types.kline import FX


class LINE:
    """
    线的基本定义，笔和线段继承此对象
    """
    def __init__(self, start: FX = None, end: FX = None, _type: str = None, index: int = 0):
        self._start: FX = start  # 线的起始位置，以分型来记录
        self._end: FX = end  # 线的结束位置，以分型来记录
        self._type: str = _type  # 线的方向类型 （up 上涨  down 下跌）
        self.index: int = index  # 线的索引，后续查找方便
    # 因果结构锁定时刻。锁定线只能使用该时刻已经存在的证据；未完成线保持 None。
        self.locked_at = None

        # 根据缠论配置（笔/段区间），得来的高低点
        self.high: float = 0
        self.low: float = 0

        # 初始化时调用一次，设置初始的 high/low
        self.update_high_low()

        # 根据缠论配置（中枢区间），得来的高低点
        self.zs_high: float = 0
        self.zs_low: float = 0

    def update_high_low(self):
        """
        根据 start, end, 和 type 更新 high 和 low 属性.
        使用 _start, _end, _type 访问私有属性以避免 setter 递归.
        """
        start_val = self._start.val if self._start else None
        end_val = self._end.val if self._end else None

        if self._type == "up" and start_val is not None and end_val is not None:
            self.high = end_val
            self.low = start_val
        elif self._type == "down" and start_val is not None and end_val is not None:
            self.high = start_val
            self.low = end_val

    @property
    def start(self) -> FX:
        """获取线的起始位置"""
        return self._start

    @start.setter
    def start(self, value: FX):
        """设置线的起始位置并更新 high/low"""
        self._start = value
        self.update_high_low()

    @property
    def end(self) -> FX:
        """获取线的结束位置"""
        return self._end

    @end.setter
    def end(self, value: FX):
        """设置线的结束位置并更新 high/low"""
        self._end = value
        self.update_high_low()

    @property
    def type(self) -> str:
        """获取线的方向类型"""
        return self._type

    @type.setter
    def type(self, value: str):
        """设置线的方向类型并更新 high/low"""
        self._type = value
        self.update_high_low()

    @abstractmethod
    def is_done(self):
        """
        判断线是否结束
        """
        return False

    def __eq__(self, other):
        """判断两条线 (LINE 子类: BI / XD / ZSLX) 是否相同。

        用 type(self) is type(other) 严格类型匹配：BI 只与 BI 比、XD 只与
        XD 比（一段笔 != 一段线段，即使端点 k_index 相同），否则两个 XD
        间 == 永远 False 导致 in / set 去重失效。非 LINE 子类返回
        NotImplemented，让另一边的 __eq__ 兜底。
        """
        if not isinstance(other, LINE):
            return NotImplemented
        if type(self) is not type(other):
            return False
        return (self.start.k.k_index == other.start.k.k_index and
                self.end.k.k_index == other.end.k.k_index and
                self.type == other.type)

    def __hash__(self):
        """返回 LINE 对象的哈希值 (与 __eq__ 一致)。

        加入 type(self).__name__ 让 BI 与 XD 即使端点相同也 hash 不同，
        减少 set/dict 上的冲突。
        """
        return hash((type(self).__name__, self.start.k.k_index, self.end.k.k_index, self.type))


    def to_dict(self):
        """将LINE对象转换为字典"""
        return {
            'start': self.start.to_dict() if self.start else None,
            'end': self.end.to_dict() if self.end else None,
            'high': self.high,
            'low': self.low,
            'zs_high': self.zs_high,
            'zs_low': self.zs_low,
            'type': self.type,
            'index': self.index,
            'locked_at': self.locked_at.isoformat() if self.locked_at is not None else None,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class BI(LINE):
    """
    笔对象
    """

    def __init__(
        self,
        start: FX,
        end: FX = None,
        _type: str = None,
        index: int = 0,
    ):
        super().__init__(start, end, _type, index)
        # 笔(BI)的中枢重叠用高低点等于笔的端点价——
        # 与 XD 完成段的 ``zs_high/zs_low = max/min(sv, ev)`` 同口径,且
        # 等价于 LINE.update_high_low 设的 high/low。
        # LINE.__init__ 在 ``update_high_low()`` 之后把 ``zs_high/zs_low``
        # 强制重置为 0(为 XD pending 段口径留位),BI 在此显式同步回 high/low。
        # 缺失此步会让 ZsCalculator 在笔层重叠判定全部失败 → 笔中枢识别为 0、
        # 笔层买卖点全部无法识别(进而线段层买卖点亦失效)。
        self.zs_high = self.high if self.high is not None else 0
        self.zs_low = self.low if self.low is not None else 0

        # 记录是否是拆分笔
        self.is_split = ""

    def to_dict(self):
        """将BI对象转换为字典"""
        data = super().to_dict()
        data.update({
            'is_split': self.is_split,
        })
        return data
    def is_done(self) -> bool:
        """
        返回笔是否完成
        """
        return self.locked_at is not None

class TZXL:
    """
    特征序列
    """

    def __init__(
        self,
        bh_direction: str,
        line: Union[LINE, None],
        pre_line: LINE,
        line_bad: bool,
        done: bool,
    ):
        self.bh_direction: str = (
            bh_direction  # 特征序列包含的方向 up 向上包含，取高高，down 向下包含，取低低
        )
        self.line: Union[LINE, None] = line
        self.pre_line: LINE = pre_line
        self.line_bad: bool = line_bad
        self.is_up_line: bool = False
        self.lines: List[LINE] = [line]
        self.done: bool = done

        # 新增：记录原始笔，用于包含处理后的追溯
        self.original_lines: List[LINE] = self.lines.copy()
        self.is_merged: bool = False

        self.max: float = 0
        self.min: float = 0
        self.update_maxmin()

    def to_dict(self):
        """将TZXL对象转换为字典"""
        return {
            'bh_direction': self.bh_direction,
            'line': self.line.to_dict() if self.line else None,
            'pre_line': self.pre_line.to_dict() if self.pre_line else None,
            'line_bad': self.line_bad,
            'is_up_line': self.is_up_line,
            'lines': [_l.to_dict() for _l in self.lines],
            'done': self.done,
            'original_lines': [_l.to_dict() for _l in self.original_lines],
            'is_merged': self.is_merged,
            'max': self.max,
            'min': self.min,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def update_maxmin(self):
        if self.bh_direction == "up":
            self.max = max([_l.high for _l in self.lines])
            self.min = max([_l.low for _l in self.lines])
        else:
            self.max = min([_l.high for _l in self.lines])
            self.min = min([_l.low for _l in self.lines])



class XLFX:
    """
    序列分型
    """

    def __init__(self, _type: str, xl: TZXL, xls: List[TZXL], done: bool = True):
        self.type: str = _type
        self.xl: TZXL = xl
        self.xls: List[TZXL] = xls

        self.qk = False  # 分型是否有缺口
        self.is_line_bad = False  # 是否是一笔破坏分型
        self.fx_high = max(_xl.max for _xl in self.xls if _xl is not None)
        self.fx_low = min(_xl.min for _xl in self.xls if _xl is not None)

        self.done = done  # 序列分型是否完成

        self.bh_type = None

    @property
    def high(self):
        return self.xl.max

    @property
    def low(self):
        return self.xl.min

    def to_dict(self):
        """将XLFX对象转换为字典"""
        return {
            'type': self.type,
            'xl': self.xl.to_dict() if self.xl else None,
            'xls': [x.to_dict() for x in self.xls if x],
            'qk': self.qk,
            'is_line_bad': self.is_line_bad,
            'fx_high': self.fx_high,
            'fx_low': self.fx_low,
            'done': self.done,
            'bh_type': self.bh_type,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class XD(LINE):
    """
    线段对象
    """

    def __init__(
        self,
        start: FX = None,
        end: FX = None,
        start_line: LINE = None,
        end_line: LINE = None,
        _type: str = None,
        ding_fx: XLFX = None,
        di_fx: XLFX = None,
        index: int = 0,
    ):
        super().__init__(start, end, _type, index)

        self.start_line: Union[LINE, BI, XD] = start_line  # 线段起始笔
        self.end_line: Union[LINE, BI, XD] = end_line  # 线段结束笔
        self.ding_fx: XLFX = ding_fx
        self.di_fx: XLFX = di_fx
        self.tzxls: List[TZXL] = []  # 特征序列列表
        self.done: bool = False  # 标记线段是否完成（信号/当下性口径：端点是否已锁定不可回改）
        # 显示口径：是否为"正在形成的最后一段"。与 done 解耦——确认级联推迟 done 的末
        # 2 条已成形确认段 done=False 但 forming=False（图表画实线），只有真正在建的末段
        # forming=True（图表画虚线）。详见 xd_calculator._emit_pending。
        self.forming: bool = False

        # 是否是拆分后的线段，如果是，这里会写明原因
        self.is_split: str = ""

        self.not_del: bool = False  # 计算过程中，不允许删除重新计算
        self.not_yx: bool = False  # 计算过程中，不允许进行延续计算

    def is_done(self) -> bool:
        return self.locked_at is not None

    def to_dict(self):
        """将XD对象转换为字典"""
        data = super().to_dict()
        data.update({
            'start_line': self.start_line.to_dict() if self.start_line else None,
            'end_line': self.end_line.to_dict() if self.end_line else None,
            'ding_fx': self.ding_fx.to_dict() if self.ding_fx else None,
            'di_fx': self.di_fx.to_dict() if self.di_fx else None,
            'tzxls': [tzxl.to_dict() for tzxl in self.tzxls],
            'done': self.done,
            'is_split': self.is_split,
            'not_del': self.not_del,
            'not_yx': self.not_yx,
        })
        return data
