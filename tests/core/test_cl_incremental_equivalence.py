"""tests/core/test_cl_incremental_equivalence.py — US-003 增量 vs 全量等价性。

缠论核心算法理论上应满足:
- 对同一份 K 线, 无论"一次性喂入" / "逐根喂入" / "分批喂入",
  最终 ``cl_snapshot(cd)`` 必须完全相等。

历史 bug (commit c9c2553~ 8d2ba0b 的 master):
- 逐根 / 分批增量后, ``xds`` 数量会比全量多 1 (full=2, inc=3)。
- 根因: ``XdCalculator._find_start`` 在 bis 列表早期 (<5 根, 无关键笔)
  走 overlap-only fallback 给出"权宜起点", 增量逻辑沿用此起点建立 xds[0];
  bis 增长后 _find_strict_start 找到真正关键笔起点 (位置不同), 但
  ``self.xds`` 已非空 → 不再走 _find_start → 永久多一段。
- 修复 (xd_calculator.py G7): calculate() 入口校验
  ``_find_strict_start(all_bis)`` 与旧 xds[0] 起点是否一致; 不一致即"关键笔
  起点漂移", 作废所有 xds 走全量重建。这是单向 fallback→strict 切换,
  不会抖动。

本测试文件:
- 用例 1 (逐根): 200 根 K 线逐根 process_klines vs 一次性, snapshot 必须相等
- 用例 2 (分批): 10/50/100/200 分批 vs 一次性, snapshot 必须相等
- 用例 3 (末根微变): 末根 OHLC 变化必须传播到 cd 内部
- 用例 4 (幂等): 同一份 df 跑两次 process_klines, cd 状态不变
"""

from __future__ import annotations

from tests.core.conftest import _generate_kline_df, DEFAULT_CL_CONFIG, cl_snapshot
from chanlun.core.cl import CL


def _make_full_snapshot(n: int = 200, seed: int = 42):
    df = _generate_kline_df(n, seed=seed, multi_freq=True)
    cd = CL("T", "1m", dict(DEFAULT_CL_CONFIG))
    cd.process_klines(df)
    return df, cl_snapshot(cd)


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
