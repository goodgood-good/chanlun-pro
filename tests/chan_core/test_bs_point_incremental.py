# -*- coding: utf-8 -*-
"""bs_point 买卖点引擎增量化贴身 oracle:小窗口内「逐步」(非抽样)比对
「逐根增量喂」与「每前缀全量重算」的买卖点(mmd),守护 ① 增量化
(BsPointCalculator 持久化 + 检测器状态恢复)不在任意单步引入错信号。

现网 test_incremental_equivalence 在抽样检查点比对全快照(含 mmd);本网在密集
窗口逐步加严,专攻 first-touch / min_signal_interval / 定律一(kobo.54.1) 这类
「跨轮累积状态」最易在单步分叉的边界(此类 sticky-state 错通常持续而非自愈,
但逐步比对能第一时间定位到具体分叉步,便于调试)。

口径:batch(process_klines 一次喂前缀)= BsPointCalculator 全量重算一次(P=0,
fresh 实例);inc(process_kline_values 逐根)= 增量路径。两者每步 mmd 必须一致。
现行无状态实现下本网恒绿;① 增量化后它直守单步等价。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chanlun.core.cl import CL                     # noqa: E402
from chanlun.recursive_bt.engine import CL_CFG     # noqa: E402

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# (fixture, 起步 start, 窗口末 stop):逐步比对 [start, stop) 每个前缀长度。
# 窗口选结构已形成、买卖点密集的中段,控时长同时覆盖 1/2/3 类与定律一。
CASES = [
    ("SH.600519_30m", 60, 200),
    ("SH.600519_5m", 80, 220),
]


def _load(stem):
    return pd.read_parquet(FIX_DIR / f"{stem}.parquet")


def _feed(cd, row):
    cd.process_kline_values(
        row.date, float(row.open), float(row.high),
        float(row.low), float(row.close), float(getattr(row, "volume", 0.0)),
    )


def _mmd_snap(cd) -> tuple:
    """笔层 / 段层各自的 (方向, 末端K索引, 排序买卖点名)。末端K索引跨 inc/batch
    稳定(相邻线共享端点分型),作业务键避开 line.index 漂移。"""
    def snap(lines):
        out = []
        for ln in lines:
            names = sorted(m.name for m in ln.get_mmds())
            if not names:
                continue
            ek = ln.end.k.k_index if (ln.end is not None and ln.end.k is not None) else None
            out.append((ln.type, ek, tuple(names)))
        return tuple(out)
    return (snap(cd.get_bis()), snap(cd.get_xds()))


@pytest.mark.parametrize("stem,start,stop", CASES)
def test_bs_point_stepwise_incremental_matches_batch(stem, start, stop):
    df = _load(stem)
    stop = min(stop, len(df))
    code, freq = stem.rsplit("_", 1)
    inc = CL(code, freq, dict(CL_CFG))
    rows = list(df.itertuples(index=False))
    saw_signal = False
    for i in range(stop):
        _feed(inc, rows[i])
        L = i + 1
        if L < start:
            continue
        batch = CL(code, freq, dict(CL_CFG))
        batch.process_klines(df.iloc[:L])
        si, sb = _mmd_snap(inc), _mmd_snap(batch)
        assert si == sb, f"{stem} L={L}:买卖点 逐根增量 vs 全量重算 单步分叉\ninc={si}\nbatch={sb}"
        if si[0] or si[1]:
            saw_signal = True
    # 防空测:窗口须真的产生过买卖点,否则本网没有在考验 bs_point。
    assert saw_signal, f"{stem} [{start},{stop}) 窗口内无任何买卖点,需调整窗口"
