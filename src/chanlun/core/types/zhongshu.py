# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import List, Optional

import numpy as np

# scipy 延迟到 ZS.r2 等真正需要回归计算时才 import：避免只用 LINE/BI/XD
# 等纯数据类型的调用方被迫拖入 scipy (~50ms cold import + 一堆 BLAS 依赖)。

from chanlun.core.types.config import Level
from chanlun.core.types.kline import Kline
from chanlun.core.types.line import LINE


class ZS:
    """中枢对象"""

    def __init__(
            self,
            zs_type: str,
            start: LINE,
            end: Optional[LINE] = None,
            zg: Optional[float] = None,
            zd: Optional[float] = None,
            gg: Optional[float] = None,
            dd: Optional[float] = None,
            _type: Optional[str] = None,
            index: int = 0,
            line_num: int = 0,
            level: Level = Level.M1,
    ):
        self.zs_type = zs_type  # 'bi' 笔中枢, 'xd' 线段中枢
        self.start = start
        self.end = end
        self.lines: List[LINE] = []  # 构成中枢的线段
        # gg/dd 增量维护缓存：append 时增量更新，pop / 重新赋值时置脏，
        # 下次 update_boundaries 全量重算。
        self._gg_cache = None
        self._dd_cache = None
        self._bounds_dirty = True

        self.zg = zg  # 中枢高点
        self.zd = zd  # 中枢低点
        self.gg = gg  # 中枢最高点（包括延伸）
        self.dd = dd  # 中枢最低点（包括延伸）

        self.type: str = _type  # 中枢类型（up 上涨中枢  down 下跌中枢  zd 震荡中枢）
        self.index = index
        self.line_num = line_num  # 包含的线段数
        self.level:Level = level  # 中枢级别

        self.done = False  # 是否完成
        self.real = True  # 是否有效

        self.entry: Optional[LINE] = None  # 进入段
        self.exit: Optional[LINE] = None  # 离开段

        # 中枢扩展：两个或更多相邻同级别中枢的波动区间（GG/DD 包络）有重叠时，
        # 合并为高级中枢；本字段记录构成它的子中枢。
        # 非扩展中枢（独立、未合并）为空列表。
        self.expanded_with: List['ZS'] = []

    @property
    def type(self) -> Optional[str]:
        return self._type

    @type.setter
    def type(self, value: Optional[str]):
        self._type = value

    def update_boundaries(self):
        """根据核心线段更新中枢的边界值"""
        if self.lines:
            if self._bounds_dirty or self._gg_cache is None:
                # 全量重算（首次 / pop 后 / lines 被整体替换后）
                self._gg_cache = max(line.zs_high for line in self.lines)
                self._dd_cache = min(line.zs_low for line in self.lines)
                self._bounds_dirty = False
            else:
                # 增量：只把最后一段并入 running max/min
                last = self.lines[-1]
                self._gg_cache = max(self._gg_cache, last.zs_high)
                self._dd_cache = min(self._dd_cache, last.zs_low)
            self.gg = self._gg_cache
            self.dd = self._dd_cache
            self.line_num = len(self.lines)

    def __setstate__(self, state):
        # 旧 pickle 的 __dict__ 不含增量边界缓存字段，补默认值，
        # 避免反序列化后调用 update_boundaries 抛 AttributeError。
        state.setdefault('_gg_cache', None)
        state.setdefault('_dd_cache', None)
        state.setdefault('_bounds_dirty', True)
        # 中枢扩展字段:旧 pickle 缺失时补空列表
        state.setdefault('expanded_with', [])
        self.__dict__.update(state)

    def add_line(self, line: LINE) -> bool:
        """
        添加 笔 or 线段。
        注意：本方法只做 append，不调用 update_boundaries()；
        调用方在所有 add_line 完成后须显式调用 update_boundaries()，
        以保持 gg/dd 及增量缓存与 self.lines 同步。
        """
        self.lines.append(line)
        self.line_num = len(self.lines)
        return True

    def zf(self) -> float:
        """
        中枢振幅
        中枢重叠区间占整个中枢区间的百分比，越大说明中枢重叠区域外的波动越小
        """
        zgzd = self.zg - self.zd
        if zgzd == 0.0:
            return 0
        return (zgzd / (self.gg - self.dd)) * 100

    def r2(self, klines: List[Kline]) -> float:
        """
        计算中枢的R2值
        R2表示价格趋近于中枢中线的程度，取值在 0-1
        越接近1表示价格越平稳（越接近中线），越接近0表示波动大
        中线 = zg - (zg - zd) / 2
        """
        # 获取中枢范围内的所有K线
        zs_klines = [
            kline
            for kline in klines
            if kline.date >= self.start.k.date and kline.date <= self.end.k.date
        ]

        if len(zs_klines) == 0:
            return 0.0

        # 收集所有K线的高开低收价格
        prices = []
        for kline in zs_klines:
            prices.append((kline.h + kline.o + kline.l + kline.c) / 4)

        if len(prices) == 0:
            return 0.0

        # 根据中枢内的线，生成 zg/zd 之间的折线数据点
        # 每条线根据方向：向上从 zd 到 zg，向下从 zg 到 zd
        zs_zigzag = []
        zs_lines = [
            _l
            for _l in self.lines
            if _l.start.k.date >= self.start.k.date and _l.end.k.date <= self.end.k.date
        ]
        for line_idx, line in enumerate(zs_lines):
            # 计算这条线包含的K线数量
            k_count = line.end.k.k_index - line.start.k.k_index + 1

            # 根据线的方向确定起点和终点
            if line.type == "up":
                y_start, y_end = self.zd, self.zg
            else:  # down
                y_start, y_end = self.zg, self.zd

            # 使用两点式直线公式计算每个K线位置对应的值
            # y = y_start + (y_end - y_start) * i / (n - 1)
            # 注意：相邻线的连接点共享同一根K线，从第二条线开始跳过第一个点
            start_i = 1 if line_idx > 0 else 0
            if k_count == 1:
                if line_idx == 0:
                    zs_zigzag.append(y_start)
            else:
                for i in range(start_i, k_count):
                    y_value = y_start + (y_end - y_start) * i / (k_count - 1)
                    zs_zigzag.append(y_value)

        # 确保 prices 和 zs_zigzag 长度一致
        if len(prices) != len(zs_zigzag):
            return -1

        x = np.array(prices)
        y = np.array(zs_zigzag)

        # 线性回归算 R²（延迟 import，避免顶层拖入 scipy）
        from scipy.stats import linregress
        slope, intercept, r_value, p_value, std_err = linregress(x, y)
        return round(r_value**2, 6)

    def zs_mmds(self, zs_type="|") -> List[str]:
        """
        获取中枢内线的所有买点列表
        """
        mmds = []
        for _l in self.lines:
            mmds += _l.line_mmds(zs_type)
        return mmds

    def zs_up_bcs(self, zs_type="|") -> List[str]:
        """
        获取中枢内，向上线段的背驰列表
        """
        bcs = []
        for _l in self.lines:
            if _l.type == "up":
                bcs += _l.line_bcs(zs_type)
        return bcs

    def zs_down_bcs(self, zs_type="|") -> List[str]:
        """
        获取中枢内，向下线段的背驰列表
        """
        bcs = []
        for _l in self.lines:
            if _l.type == "down":
                bcs += _l.line_bcs(zs_type)
        return bcs

    def to_dict(self):
        """将ZS对象转换为字典"""
        return {
            'zs_type': self.zs_type,
            'start': self.start.to_dict() if self.start else None,
            'lines': [line.to_dict() for line in self.lines],
            'end': self.end.to_dict() if self.end else None,
            'zg': self.zg,
            'zd': self.zd,
            'gg': self.gg,
            'dd': self.dd,
            'type': self.type,
            'index': self.index,
            'line_num': self.line_num,
            # level 必须输出枚举 .value（与 ZSLX.to_dict 一致）；直接放 Level
            # 枚举会让 json.dumps(zs.to_dict()) / str(zs) 抛 TypeError。
            'level': self.level.value if self.level is not None else None,
            'done': self.done,
            'real': self.real,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
