# -*- coding: utf-8 -*-
"""缠论核心数据类型包。

原 ``chanlun.core.cl_interface`` 的全部类/函数已按依赖分层拆入本包的子模块：
``config`` / ``kline`` / ``line`` / ``zhongshu`` / ``signal`` / ``info`` / ``interface``。
本 ``__init__`` 从各子模块再导出全部公开名，使
``from chanlun.core.types import X`` 与历史的
``from chanlun.core.cl_interface import X``（经 facade 转发）取到同一对象。
"""
from chanlun.core.types.config import Config, Level
from chanlun.core.types.kline import _slot_setstate, Kline, CLKline, FX
from chanlun.core.types.line import LINE, BI, XD, ZSLX, TZXL, XLFX
from chanlun.core.types.zhongshu import ZS
from chanlun.core.types.signal import MMD, BC
from chanlun.core.types.info import (
    LOW_LEVEL_QS,
    MACD_INFOS,
    LINE_FORM_INFOS,
    BW_LINE_QS_INFOS,
)
from chanlun.core.types.interface import (
    ICL,
    _resolve_ld_macd,
    query_macd_ld,
    compare_ld_beichi,
    user_custom_mmd,
)

__all__ = [
    "Config",
    "Level",
    "Kline",
    "CLKline",
    "FX",
    "LINE",
    "BI",
    "XD",
    "ZSLX",
    "TZXL",
    "XLFX",
    "ZS",
    "MMD",
    "BC",
    "LOW_LEVEL_QS",
    "MACD_INFOS",
    "LINE_FORM_INFOS",
    "BW_LINE_QS_INFOS",
    "ICL",
    "query_macd_ld",
    "compare_ld_beichi",
    "user_custom_mmd",
]
