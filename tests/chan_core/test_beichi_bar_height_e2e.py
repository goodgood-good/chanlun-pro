# -*- coding: utf-8 -*-
"""B1 端到端差分守护:柱子高度口径(面积 OR 高度) vs 旧「仅面积」,在真实 1m 数据上
多检出背驰类买卖点(原文 L025:11 乖离极限漏判已修)。

为何差分而非 golden 快照:① golden fixture(SH.600519/QQQ_30m)不含「面积大但柱高
度不破」形态(改动对其零影响,故 golden 网漏覆盖此真实信号变化);② 差分(同数据相
对比较)平台无关,比绝对快照更稳健。fixture 置 synthetic/ 避 golden glob 自动捕获。

旧口径用 monkeypatch 让 `_ld_height` 恒 0 → `_bar_decays` 柱高度永不衰竭 → 退化为
仅面积(即 B1 改前行为)。新口径检出的背驰类买卖点应严格多于旧口径。
"""
from pathlib import Path

import pandas as pd

import chanlun.core.beichi_calculator as bc
from chanlun.core.cl import CL
from chanlun.recursive_bt.engine.engine import CL_CFG

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "synthetic" / "QQQ.US_1m.parquet"
_BEICHI_CLASSES = ("1buy", "1sell", "2buy", "2sell")   # 背驰衍生(一类=趋势背驰、二类=其后回抽)


def _count_beichi_class(df: pd.DataFrame) -> int:
    cd = CL("QQQ.US", "1m", dict(CL_CFG))
    cd.process_klines(df)
    return sum(1 for p in cd.get_branch_bspoints(use_xd=True) if p.bs_type in _BEICHI_CLASSES)


def test_bar_height_adds_beichi_class_signals_e2e(monkeypatch):
    df = pd.read_parquet(FIX)
    n_new = _count_beichi_class(df)                            # 新:面积 OR 柱高度
    monkeypatch.setattr(bc, "_ld_height", lambda ld, d: 0.0)   # 退化:柱高度恒0 → 仅面积(B1改前)
    n_old = _count_beichi_class(df)
    assert n_new > n_old, (
        f"B1 柱高度口径应多检出背驰类买卖点(乖离极限 L025:11 漏判已修): "
        f"new={n_new} old={n_old}"
    )
