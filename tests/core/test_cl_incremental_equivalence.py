"""tests/core/test_cl_incremental_equivalence.py — US-003 增量 vs 全量等价性。

缠论核心算法理论上应满足:
- 对同一份 K 线, 无论"一次性喂入" / "逐根喂入" / "分批喂入",
  最终 ``cl_snapshot(cd)`` 必须完全相等。

实际探测 (commit c9c2553 附近的 master) 结果:
- 逐根 / 分批增量后, ``xds`` 数量会比全量多 1 (full=2, inc=3)。
  根因初判: ``xd_calculator`` 在中间步骤产生的 pending 段没在后续被回收;
  以及 ``bi_zss_calculator.calculate`` 调用时携带的尾段状态影响。
- 末根 OHLC 微变能正确更新 cd 内部 K 线, 结构数稳定。

本测试文件:
- 用例 1/2 (逐根 / 分批): 标 ``xfail(strict=True)`` —— 当前已知 bug, US-009
  (web 路径接入进程内 cl 对象 LRU 缓存) 要求把这个 bug 修掉,
  那时这两条会自动 xpass 并触发 CI 红灯, 提醒移除 xfail 标记。
- 用例 3 (末根微变): 不标 xfail, 当前 master 已正确, 作为回归网。
"""

from __future__ import annotations

import pytest

from tests.core.conftest import _generate_kline_df, DEFAULT_CL_CONFIG, cl_snapshot
from chanlun.core.cl import CL


def _make_full_snapshot(n: int = 200, seed: int = 42):
    df = _generate_kline_df(n, seed=seed, multi_freq=True)
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    return df, cl_snapshot(cd)


@pytest.mark.xfail(
    reason=(
        "已知 master bug: 逐根增量后 xds 比全量多 1。"
        "US-009 (cl 对象 LRU 缓存接入 web) 必须先修掉此 bug, "
        "届时本用例会自动 xpass 并强制移除 xfail 标记。"
    ),
    strict=True,
)
def test_incremental_per_kline_matches_full():
    """用例 1: 逐根 process_klines 200 次 vs 一次性 process_klines(full)。"""
    df, snap_full = _make_full_snapshot(200, seed=42)

    cd_inc = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    for i in range(1, len(df) + 1):
        cd_inc.process_klines(df.iloc[:i])
    snap_inc = cl_snapshot(cd_inc)

    assert snap_inc == snap_full, (
        f"逐根增量 vs 全量 snapshot 不等:\n"
        f"  full: fxs={len(snap_full['fxs'])} bis={len(snap_full['bis'])} xds={len(snap_full['xds'])}\n"
        f"  inc:  fxs={len(snap_inc['fxs'])} bis={len(snap_inc['bis'])} xds={len(snap_inc['xds'])}"
    )


@pytest.mark.xfail(
    reason=(
        "已知 master bug: 分批增量 (10/50/100/200) 后 xds 比全量多 1。"
        "与 test_incremental_per_kline_matches_full 同根, 由 US-009 一并修复。"
    ),
    strict=True,
)
def test_incremental_batch_matches_full():
    """用例 2: 分批喂入 [10, 50, 100, 200] vs 一次性 process_klines(full)。"""
    df, snap_full = _make_full_snapshot(200, seed=42)

    cd_batch = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    for upto in (10, 50, 100, 200):
        cd_batch.process_klines(df.iloc[:upto])
    snap_batch = cl_snapshot(cd_batch)

    assert snap_batch == snap_full, (
        f"分批增量 vs 全量 snapshot 不等:\n"
        f"  full:  fxs={len(snap_full['fxs'])} bis={len(snap_full['bis'])} xds={len(snap_full['xds'])}\n"
        f"  batch: fxs={len(snap_batch['fxs'])} bis={len(snap_batch['bis'])} xds={len(snap_batch['xds'])}"
    )


def test_last_kline_ohlc_update_propagates():
    """用例 3: 末根 K 线 OHLC 微变后, cd 内部 K 线被更新 (非静默忽略)。

    这是"实时盘"路径的关键正确性 —— 同一根 1m K 线的 high/close 会随每秒 tick
    上下波动, cd.process_klines(df_with_updated_last) 必须把内部 last_kline 改掉。
    """
    df = _generate_kline_df(200, seed=42, multi_freq=True)
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    h_before = cd.get_klines()[-1].h
    c_before = cd.get_klines()[-1].c

    df_v2 = df.copy()
    df_v2.loc[df_v2.index[-1], "high"] = 999.0
    df_v2.loc[df_v2.index[-1], "close"] = 998.0
    cd.process_klines(df_v2)

    assert cd.get_klines()[-1].h == 999.0, f"末根 high 未更新: before={h_before}, after={cd.get_klines()[-1].h}"
    assert cd.get_klines()[-1].c == 998.0, f"末根 close 未更新: before={c_before}, after={cd.get_klines()[-1].c}"
    # K 线总数应保持 200 (没有意外追加)
    assert len(cd.get_klines()) == 200


def test_full_recompute_idempotent():
    """补充用例: 同一份 df 跑两次 process_klines, cd 状态稳定 (幂等性)。

    这是逐根/分批 bug 之外的另一种增量场景: "已经处理过的全量再喂一次"。
    process_klines 内部 _preprocess 应识别为零增量, cd 不变。
    """
    df, snap_first = _make_full_snapshot(200, seed=42)

    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    cd.process_klines(df)  # 第二次喂同一份
    snap_second = cl_snapshot(cd)

    assert snap_second == snap_first
