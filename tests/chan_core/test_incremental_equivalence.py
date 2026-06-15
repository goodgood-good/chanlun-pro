# -*- coding: utf-8 -*-
"""增量路径 vs 全量 batch 的「逐前缀」等价对拍。

bi/xd/zs 等 calculator,走「逐根增量喂」(``process_kline_values``)与「一次性
全量」(新建 CL + ``process_klines(df[:L])``)是两条不同代码路径:前者命中
增量分支(如 bi_calculator 档2 ``_try_incremental_extend``),后者新实例首次
即走档3全量重建。

分两层钉死(口径见 ``snapshot.cl_snapshot``):

1. ``test_bi_xd_incremental_matches_batch``:**笔/线段结构**(index/type/端点/
   高低/done)逐前缀逐字一致。实测当前代码全 fixture 全检查点 0 分叉 —— 笔/段
   增量与全量**完全等价**。这是后续把 ``_rebuild_from_fxs`` /
   ``_build_endpoint_stack`` 改成「真增量」(只重放活跃尾部)的**贴身安全网**:
   任何笔/段分叉立即报警。

2. ``test_full_snapshot_incremental_matches_batch``:**全快照**(含笔层中枢
   bi_zss / 线段中枢 xd_zss / 买卖点 mmd / 背驰 bcs)逐前缀完全一致。曾有既有
   差异(bi_zss 笔层中枢方向/结构 + 部分 mmd),已由两处修复消除:① zs_calculator
   第一个中枢未完成时不增量、全量从 -1 重判(开头中枢进入段提升随段数变化不可逆);
   ② zslx_calculator 缓存命中时重放「中枢方向回填」副作用。5m 3000bar 加严 0 分叉。

现有 ``test_golden_master`` 只钉一次性全量的**终态**,测不到中间每步的增量
一致性,故另立此网。
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from snapshot import canonical_json, cl_snapshot  # noqa: E402
from chanlun.core.cl import CL                     # noqa: E402
from chanlun.recursive_bt.engine import CL_CFG     # noqa: E402

FIX_DIR = Path(__file__).resolve().parents[1] / "fixtures"

# (fixture stem, 截取上限 None=全量)。中小 fixture 全程逐根;5m 大盘截取一段控时长。
CASES = [
    ("SH.600519_d", None),
    ("QQQ.US_d", None),
    ("SH.600519_30m", None),
    ("QQQ.US_30m", None),
    ("SH.600519_5m", 1000),
]

_STRUCT_FIELDS = ("index", "type", "start", "end", "high", "low", "done")


def _checkpoints(n: int) -> set:
    """前 30 根每根查(笔/段刚形成的边界期最易分叉)+ 之后约每 n/25 一查 + 末尾。"""
    early = set(range(1, min(n, 30) + 1))
    step = max(1, n // 25)
    mid = set(range(step, n + 1, step))
    return early | mid | {n}


def _struct_only(snap: dict) -> dict:
    """只取笔/段的结构字段,排除 mmds/bcs 与中枢(那些差异见全快照 xfail)。"""
    def line(ln):
        return {k: ln[k] for k in _STRUCT_FIELDS}
    return {"bis": [line(b) for b in snap["bis"]], "xds": [line(x) for x in snap["xds"]]}


def _load(stem: str, limit) -> pd.DataFrame:
    df = pd.read_parquet(FIX_DIR / f"{stem}.parquet")
    if limit is not None:
        df = df.iloc[:limit].reset_index(drop=True)
    return df


def _new_cl(code: str, freq: str) -> CL:
    return CL(code, freq, dict(CL_CFG))


def _feed(cd: CL, row) -> None:
    cd.process_kline_values(
        row.date, float(row.open), float(row.high),
        float(row.low), float(row.close), float(getattr(row, "volume", 0.0)),
    )


def _iter_checkpoints(stem, limit):
    """逐根增量喂 inc,在每个检查点 yield (L, inc 快照, 全量 batch 快照)。"""
    df = _load(stem, limit)
    code, freq = stem.rsplit("_", 1)
    cps = _checkpoints(len(df))
    inc = _new_cl(code, freq)
    for i, row in enumerate(df.itertuples(index=False)):
        _feed(inc, row)
        L = i + 1
        if L not in cps:
            continue
        batch = _new_cl(code, freq)
        batch.process_klines(df.iloc[:L])
        yield L, cl_snapshot(inc), cl_snapshot(batch)


@pytest.mark.parametrize("stem,limit", CASES)
def test_bi_xd_incremental_matches_batch(stem, limit):
    """笔/线段结构:逐根增量 == 逐前缀全量 batch(攻 bi 真增量栈的贴身 oracle)。"""
    for L, si, sb in _iter_checkpoints(stem, limit):
        got = canonical_json(_struct_only(si))
        exp = canonical_json(_struct_only(sb))
        assert got == exp, f"{stem} L={L}:笔/段结构 增量 vs 全量 batch 分叉"


@pytest.mark.parametrize("stem,limit", CASES)
def test_full_snapshot_incremental_matches_batch(stem, limit):
    """全快照(含 bi_zss/xd_zss/mmd/bcs):增量 vs 全量 batch 逐前缀完全一致。

    曾有既有差异(bi_zss 笔层中枢方向/结构 + 部分 mmd),已由两处修复消除:
    ① zs_calculator:第一个中枢未完成(pending)时不增量、全量从 -1 重判——
       开头中枢进入段提升 _promote_opening_entry_if_needed 随段数变化且不可逆;
    ② zslx_calculator:缓存命中时重放「中枢方向回填」副作用(否则新中枢 type
       保持 _create_zs 占位)。SH.600519_5m 3000bar 加严全快照对拍亦 0 分叉。
    """
    for L, si, sb in _iter_checkpoints(stem, limit):
        assert canonical_json(si) == canonical_json(sb), \
            f"{stem} L={L}:全快照 增量 vs 全量 batch 分叉"


def _recursive_sig(cd) -> tuple:
    """递归层(回测真正消费的 L0/L1 中枢 + 多级买卖点)的稳定快照。

    cl_snapshot 只覆盖 legacy 层(bis/xds/bi_zss/xd_zss/mmd);回测走 recursive_branch
    (get_recursive_branch_levels / get_branch_bspoints),不在 cl_snapshot 内,故另取。
    """
    def r(v):
        return round(v, 6) if isinstance(v, (int, float)) else v

    def bsp(pts):
        out = []
        for p in pts:
            af = getattr(p, "anchor_fx", None)
            out.append((
                p.bs_type, p.level,
                getattr(getattr(af, "k", None), "date", None) if af else None,
                r(getattr(af, "val", None)),
                r(p.structural_stop_below), r(p.structural_stop_above),
            ))
        return out

    def lvl(levels):
        return [
            (lv.level, len(lv.zss), len(lv.zslxs),
             tuple((r(getattr(z, "zg", None)), r(getattr(z, "zd", None)), z.type)
                   for z in lv.zss))
            for lv in levels
        ]

    return (
        tuple(bsp(cd.get_branch_bspoints(use_xd=False))),
        tuple(bsp(cd.get_branch_bspoints(use_xd=True))),
        tuple(lvl(cd.get_recursive_branch_levels())),
    )


@pytest.mark.parametrize("stem,limit", CASES)
def test_recursive_branch_incremental_matches_batch(stem, limit):
    """递归层(回测真正消费的 L0/L1 中枢 + 多级买卖点)增量 vs 全量逐前缀一致。

    回测/实盘走 recursive_branch(get_recursive_branch_levels / get_branch_bspoints),
    每根 K 全量重算(_recursive_memo 每 K clear + RecursiveBranchCalculator new 实例),
    本应纯函数 inc==batch;此网钉死该不变量、防未来递归层增量化引入分叉(legacy 层
    的 bi_zss 增量化曾因 _promote 不可逆 / zslx 缓存漏回填而分叉,见上)。
    """
    df = _load(stem, limit)
    code, freq = stem.rsplit("_", 1)
    cps = _checkpoints(len(df))
    inc = _new_cl(code, freq)
    for i, row in enumerate(df.itertuples(index=False)):
        _feed(inc, row)
        L = i + 1
        if L not in cps:
            continue
        batch = _new_cl(code, freq)
        batch.process_klines(df.iloc[:L])
        assert _recursive_sig(inc) == _recursive_sig(batch), \
            f"{stem} L={L}:递归层 增量 vs 全量 batch 分叉"
