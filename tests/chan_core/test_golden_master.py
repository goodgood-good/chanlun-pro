# -*- coding: utf-8 -*-
"""黄金主回归:钉住生产核心 chanlun.core.CL 当前结构化行为。

任何后续优化若改变 笔/段/中枢/买卖点/背驰 输出,这里立即报警。
golden 是平台相关的(当前 Windows + numpy/scipy 版本);依赖大版本变动后
用 `gen_fixtures.py --update-golden` 重生并人工复核。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from snapshot import cl_snapshot, canonical_json  # noqa: E402
from chanlun.core.cl import CL                     # noqa: E402
from chanlun.recursive_bt.engine.engine import CL_CFG     # noqa: E402

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures"
GOLD_DIR = Path(__file__).resolve().parents[1] / "golden"


def _keys():
    return sorted(p.stem for p in FIX_DIR.glob("*.parquet"))


def _build(key: str) -> CL:
    df = pd.read_parquet(FIX_DIR / f"{key}.parquet")
    code, freq = key.rsplit("_", 1)
    cd = CL(code, freq, dict(CL_CFG))
    cd.process_klines(df)
    return cd


@pytest.mark.parametrize("key", _keys())
def test_golden_master(key):
    got = canonical_json(cl_snapshot(_build(key)))
    golden = (GOLD_DIR / f"{key}.json").read_text(encoding="utf-8")
    assert got == golden, f"{key} 结构化输出相对 golden 漂移(疑似回归)"


def test_drift_is_detected():
    """变异检验:证明安全网真会响。整体缩放价格 → 快照价格字段必变。"""
    keys = _keys()
    assert keys, "无 fixture,先跑 gen_fixtures.py --pull"
    key = keys[0]
    code, freq = key.rsplit("_", 1)
    base = canonical_json(cl_snapshot(_build(key)))
    df = pd.read_parquet(FIX_DIR / f"{key}.parquet").copy()
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * 1.05  # 哨兵扰动:单调变换保结构,但所有价格字段位移
    cd = CL(code, freq, dict(CL_CFG))
    cd.process_klines(df)
    assert canonical_json(cl_snapshot(cd)) != base, "扰动价格后快照未变 → 安全网失效"
