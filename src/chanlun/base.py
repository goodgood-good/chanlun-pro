# -*- coding: utf-8 -*-
"""Facade shim:``Market`` 枚举已迁至 :mod:`chanlun.market`。

原模块名 ``base`` 名不副实(实为市场枚举,非任何基类),已正名为 ``market``。
本 shim 保留旧的 ``chanlun.base`` 导入路径一个迁移周期;全调用方已迁
``chanlun.market``,新代码请直接用 ``chanlun.market``,后续可清理本文件。
"""
from chanlun.market import Market  # noqa: F401
