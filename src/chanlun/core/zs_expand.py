"""zs_expand.py — P8 中枢扩展实体化（中心定理二）。

走势类型递归主链之外的「中枢升级」路径：相邻中枢按定理二判扩展(本体包络重叠+
核心区分离)，借跨越的次级别走势类型实体化为高级别中枢。孤立、不改走势类型边界
(原文 line16429 扩展⊥转折)。设计见 docs/chanlun_core_redesign_8_中枢扩展_design.md。
"""
from __future__ import annotations

from typing import List, Optional  # noqa: F401

from chanlun.core.cl_interface import ZS, ZSLX  # noqa: F401


def is_zs_expand(prev: Optional[ZS], cur: Optional[ZS]) -> bool:
    """中心定理二·中枢扩展判定（委托 ZS.can_expand_with，统一几何口径）。"""
    if prev is None or cur is None:
        return False
    return prev.can_expand_with(cur)
