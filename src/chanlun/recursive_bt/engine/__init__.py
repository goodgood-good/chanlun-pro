"""chanlun.recursive_bt.engine — engine 子包:回测引擎核心 + 市场运行时 + 基本面（地基，被全员依赖）。

engine.py 迁入本子包后，限定路径由旧 ``chanlun.recursive_bt.engine`` 变为
``chanlun.recursive_bt.engine.engine``。为保持旧 ``bt_data`` 缓存（其中 Signal 以
旧限定路径序列化）的反序列化兼容，这里把 Signal 在包级重新导出，使
``chanlun.recursive_bt.engine.Signal`` 仍可解析到同一个类对象。
"""
from chanlun.recursive_bt.engine.engine import (  # noqa: F401
    MarketRules,
    Signal,
)
