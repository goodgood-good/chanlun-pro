# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from typing import TYPE_CHECKING, List, Union

# MMD/BC 只在类型注解里引用 ZS/LINE（已被 ``from __future__ import annotations``
# 字符串化）。放 TYPE_CHECKING 块：满足 ruff/类型工具,运行期不执行 → 不成环。
if TYPE_CHECKING:
    from chanlun.core.types.line import LINE
    from chanlun.core.types.zhongshu import ZS


class MMD:
    """
    买卖点对象
    """

    __slots__ = ("name", "zs", "msg")

    def __init__(self, name: str, zs: ZS):
        self.name: str = name  # 买卖点名称
        self.zs: ZS = zs  # 买卖点对应的中枢对象
        self.msg: str = ""  # 买卖点信息

    def __setstate__(self, state):
        from chanlun.core.types.kline import _slot_setstate
        _slot_setstate(self, state)

    def to_dict(self):
        """将MMD对象转换为字典"""
        return {
            'name': self.name,
            'zs': self.zs.to_dict() if self.zs else None,
            'msg': self.msg,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


class BC:
    """
    背驰对象
    """

    __slots__ = ("type", "zs", "compare_line", "compare_lines", "bc")

    def __init__(
        self,
        _type: str,
        zs: Union[ZS, None],
        compare_line: LINE,
        compare_lines: List[LINE],
        bc: bool,
    ):
        self.type: str = (
            _type  # 背驰类型 （bi 笔背驰 xd 线段背驰 pz 盘整背驰 qs 趋势背驰）
        )
        self.zs: Union[ZS, None] = zs  # 背驰对应的中枢
        self.compare_line: LINE = (
            compare_line  # 比较的笔 or 线段， 在 笔背驰、线段背驰、盘整背驰有用
        )
        self.compare_lines: List[LINE] = compare_lines  # 在趋势背驰的时候使用
        self.bc = bc  # 是否背驰

    def __setstate__(self, state):
        from chanlun.core.types.kline import _slot_setstate
        _slot_setstate(self, state)

    def to_dict(self):
        """将BC对象转换为字典"""
        return {
            'type': self.type,
            'zs': self.zs.to_dict() if self.zs else None,
            'compare_line': self.compare_line.to_dict() if self.compare_line else None,
            'compare_lines': [line.to_dict() for line in self.compare_lines],
            'bc': self.bc,
        }

    def __str__(self):
        """以字典形式显示所有属性"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
