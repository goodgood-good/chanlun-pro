# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime
import json
from typing import List



def _slot_setstate(obj, state):
    """只恢复当前插槽类型生成的规范状态。"""
    if (
        type(state) is not tuple
        or len(state) != 2
        or state[0] is not None
        or type(state[1]) is not dict
        or set(state[1]) != set(type(obj).__slots__)
    ):
        raise ValueError("kline pickle state does not match the current schema")
    for key, value in state[1].items():
        setattr(obj, key, value)


class Kline:
    """
    原始K线对象。

    ``index`` 表示原始K线在源数据序列中的坐标，可直接用于原始指标数组下标。
    """

    __slots__ = ("index", "date", "h", "l", "o", "c", "a")

    def __init__(
        self,
        index: int,
        date: datetime.datetime,
        h: float,
        l: float,
        o: float,
        c: float,
        a: float,
    ):
        self.index: int = index
        self.date: datetime.datetime = date
        self.h: float = h
        self.l: float = l
        self.o: float = o
        self.c: float = c
        self.a: float = a

    def __setstate__(self, state):
        _slot_setstate(self, state)

    def to_dict(self):
        """将Kline对象转换为字典"""
        date_str = self.date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(self.date, 'strftime') else str(self.date)
        return {
            'index': self.index,
            'date': date_str,
            'h': self.h,
            'l': self.l,
            'o': self.o,
            'c': self.c,
            'a': self.a
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

class CLKline:
    """
    缠论K线对象。

    这里同时维护两套坐标：
    - ``index``: 缠论K线序号，位于合并后的 ``cl_klines`` 坐标系中
    - ``k_index``: 原始K线坐标，位于源 ``Kline`` 序列坐标系中

    两者不可混用：
    - 结构距离、增量回退等缠论对象关系，使用 ``index``
    - MACD 切片、原始K线跨度、原始序列定位，使用 ``k_index``
    """

    __slots__ = (
        "k_index", "date", "h", "l", "o", "c", "a",
        "klines", "index", "n", "q", "up_qs",
    )

    def __init__(
        self,
        k_index: int,
        date: datetime,
        h: float,
        l: float,
        o: float,
        c: float,
        a: float,
        klines: List[Kline] = None,
        index: int = 0,
        _n: int = 0,
        _q: bool = False,
    ):
        if klines is None:
            klines = []
        self.k_index: int = k_index
        self.date: datetime = date
        self.h: float = h
        self.l: float = l
        self.o: float = o
        self.c: float = c
        self.a: float = a
        self.klines: List[Kline] = klines  # 其中包含K线对象
        self.index: int = index
        self.n: int = _n  # 记录包含的K线数量
        self.q: bool = _q  # 是否有缺口
        self.up_qs = None  # 合并时之前的趋势

    def __setstate__(self, state):
        _slot_setstate(self, state)

    def to_dict(self):
        """将CLKline对象转换为字典"""
        date_str = self.date.strftime("%Y-%m-%d %H:%M:%S") if hasattr(self.date, 'strftime') else str(self.date)
        return {
            'k_index': self.k_index,
            'date': date_str,
            'h': self.h,
            'l': self.l,
            'o': self.o,
            'c': self.c,
            'a': self.a,
            'index': self.index,
            'n': self.n,
            'q': self.q,
            'up_qs': self.up_qs,
            'klines': [kline.to_dict() for kline in self.klines] if self.klines else []
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class FX:
    """
    分型对象
    """

    __slots__ = ("type", "k", "klines", "val", "index", "done")

    def __init__(
        self,
        _type: str,
        k: CLKline,
        klines: List[CLKline],
        val: float,
        index: int = 0,
        done: bool = True,
    ):
        self.type: str = _type  # 分型类型 （ding 顶分型 di 底分型）
        self.k: CLKline = k
        self.klines: List[CLKline] = klines
        self.val: float = val
        self.index: int = index
        self.done: bool = done  # 分型是否完成

    def __setstate__(self, state):
        _slot_setstate(self, state)

    def ld(self) -> int:
        """
        分型力度值，数值越大表示分型力度越大
        根据第三根K线与前两根K线的位置关系决定
        """
        ld = 0
        one_k = self.klines[0]
        two_k = self.klines[1]
        three_k = self.klines[2]
        if three_k is None:
            return ld
        if self.klines[0].k_index == -1 or self.klines[-1].k_index == -1:
            return ld
        if self.type == "ding":
            # 第三个缠论K线要一根单阴线
            if len(three_k.klines) > 1:
                return ld
            if three_k.klines[0].c > three_k.klines[0].o:
                return ld
            # 第三个K线的高点，低于第二根的 50% 以下
            if three_k.h < (two_k.h - ((two_k.h - two_k.l) * 0.5)):
                ld += 1
            # 第三个最低点是三根中最低的
            if three_k.l < one_k.l and three_k.l < two_k.l:
                ld += 1
            # 第三根的K线的收盘价要低于前两个K线
            if three_k.klines[0].c < one_k.l and three_k.klines[0].c < two_k.l:
                ld += 1
            # 第三个缠论K线的实体，要大于第二根缠论K线
            if (three_k.h - three_k.l) > (two_k.h - two_k.l):
                ld += 1
            # 第三个K线不能有太大的下影线
            if (three_k.klines[0].h - three_k.klines[0].l) != 0 and (
                three_k.klines[0].c - three_k.klines[0].l
            ) / (three_k.klines[0].h - three_k.klines[0].l) < 0.3:
                ld += 1
        elif self.type == "di":
            # 第三个缠论K线要一根单阳线
            if len(three_k.klines) > 1:
                return ld
            if three_k.klines[0].c < three_k.klines[0].o:
                return ld
            # 第三个K线的低点，高于第二根的 50% 之上
            if three_k.l > (two_k.l + ((two_k.h - two_k.l) * 0.5)):
                ld += 1
            # 第三个最高点是三根中最高的
            if three_k.h > one_k.h and three_k.h > two_k.h:
                ld += 1
            # 第三根的K线的收盘价要高于前两个K线
            if three_k.klines[0].c > one_k.h and three_k.klines[0].c > two_k.h:
                ld += 1
            # 第三个缠论K线的实体，要大于第二根缠论K线
            if (three_k.h - three_k.l) > (two_k.h - two_k.l):
                ld += 1
            # 第三个K线不能有太大的上影线
            if (three_k.klines[0].h - three_k.klines[0].l) != 0 and (
                three_k.klines[0].h - three_k.klines[0].c
            ) / (three_k.klines[0].h - three_k.klines[0].l) < 0.3:
                ld += 1
        return ld

    def to_dict(self):
        """将FX对象转换为字典"""
        return {
            'type': self.type,
            'val': self.val,
            'index': self.index,
            'done': self.done,
            'k': self.k.to_dict() if self.k else None,
            'klines': [kline.to_dict() for kline in self.klines] if self.klines else []
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
