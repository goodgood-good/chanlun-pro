"""tests/test_cl_object_cache_incremental.py — F1 真增量路径验证。

xd_calculator G7 修复后 (commit 77ab323), cache hit 时可以复用旧 cd 调
process_klines(full), 让 cd 内部 _preprocess 切片增量, 避免每次新建 cd
全量重算的几百 ms 开销。

验证三件事:
1. **触发**: extending signature (len↑ + last_date↑ + ref OHLC 不变) → 走真增量
2. **正确性**: 真增量出来的 cd 与"一次性全量"等价 (cl_snapshot md5 相等)
3. **降级**: 复权 / 缩短 / cache miss → 走全量重建, 不复用旧 cd
"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.core.conftest import _generate_kline_df, DEFAULT_CL_CONFIG, cl_snapshot
from chanlun.core.cl import CL


@pytest.fixture(autouse=True)
def _clear_cache():
    from cl_app.services.cl_object_cache import clear_all
    clear_all()
    yield
    clear_all()


def _snap_md5(cd: CL) -> str:
    blob = json.dumps(cl_snapshot(cd), sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.md5(blob.encode("utf-8")).hexdigest()


def test_extending_signature_uses_incremental_path():
    """K 线追加 5 根 → extending signature → 走真增量。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl, stats

    df_full = _generate_kline_df(205, seed=42, multi_freq=True)
    df_initial = df_full.iloc[:200].copy()
    df_extended = df_full.copy()  # 200 + 5 = 205

    # 1) cache miss + full rebuild
    cd1 = get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_initial)
    s1 = stats()
    assert s1["full_rebuilds"] == 1
    assert s1["incremental_extends"] == 0

    # 2) 追加 5 根 → extending signature → 真增量
    cd2 = get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_extended)
    s2 = stats()
    assert s2["full_rebuilds"] == 1, "追加场景不应触发新 full rebuild"
    assert s2["incremental_extends"] == 1, (
        f"F1 真增量未触发: stats={s2}"
    )
    # 真增量复用了同一个 cd 实例
    assert cd1 is cd2, "真增量应复用旧 cd 实例"
    # cd 内部 K 线已扩展到 205 根
    assert len(cd2.get_klines()) == 205


def test_incremental_equivalent_to_full_rebuild():
    """真增量出的 cd 与"一次性 full rebuild" cl_snapshot md5 完全相等。

    这是 F1 安全性的核心断言: xd_calculator G7 修好后, 增量与全量必须等价。
    若不等价说明 G7 修复有漏 → CI 红灯。
    """
    from cl_app.services.cl_object_cache import get_or_compute_cl, clear_all

    df_full = _generate_kline_df(250, seed=42, multi_freq=True)
    df_initial = df_full.iloc[:200].copy()

    # 路径 A: 真增量 (先 200 后 250)
    clear_all()
    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_initial)
    cd_incremental = get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_full)
    md5_incremental = _snap_md5(cd_incremental)

    # 路径 B: 一次性 full rebuild (清缓存后只跑一次)
    clear_all()
    cd_full = get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_full)
    md5_full = _snap_md5(cd_full)

    assert md5_incremental == md5_full, (
        "F1 真增量与全量重建 cl_snapshot 必须完全等价。"
        "若不等, xd_calculator G7 修复有漏, 增量正确性出问题。"
    )


def test_split_event_does_not_extend():
    """复权 (ref OHLC 改变) 时不走 extending, 走全量重建。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl, stats

    df_pre = _generate_kline_df(200, seed=42, multi_freq=True)
    df_post = df_pre.copy()
    # 模拟 2:1 拆股: 整段价格减半
    for col in ("open", "high", "low", "close"):
        df_post[col] = df_post[col] * 0.5

    cd_pre = get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_pre)
    cd_post = get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_post)

    assert cd_pre is not cd_post, "复权后必须新建 cd, 不能复用旧实例"
    s = stats()
    assert s["full_rebuilds"] == 2
    assert s["incremental_extends"] == 0


def test_shortened_series_does_not_extend():
    """K 线变短 (用户切换更窄范围) 时不走 extending, 走全量重建。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl, stats

    df_long = _generate_kline_df(200, seed=42, multi_freq=True)
    df_short = df_long.iloc[:100].copy()

    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_long)
    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_short)

    s = stats()
    assert s["incremental_extends"] == 0, "缩短时不应走 incremental"
    assert s["full_rebuilds"] >= 2  # 第一次 miss + 第二次也是 full (signature 不连续)


def test_three_path_stats_tracking():
    """验证三种路径的 stats counter 正确累积。"""
    from cl_app.services.cl_object_cache import get_or_compute_cl, stats

    df_full = _generate_kline_df(250, seed=42, multi_freq=True)
    df_a = df_full.iloc[:200].copy()
    df_b = df_full.iloc[:210].copy()  # extending
    df_c = df_full.copy()             # extending again

    # 1) miss + full rebuild
    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_a)
    # 2) hit (同 sig)
    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_a)
    # 3) extending
    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_b)
    # 4) extending again
    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_c)
    # 5) hit (同 sig as df_c)
    get_or_compute_cl("a", "T", "1m", DEFAULT_CL_CONFIG, df_c)

    s = stats()
    assert s["hits"] == 2, f"应有 2 个 cache hit, 实际: {s}"
    assert s["incremental_extends"] == 2, f"应有 2 个 incremental, 实际: {s}"
    assert s["full_rebuilds"] == 1, f"应有 1 个 full rebuild, 实际: {s}"


def test_ref_bar_index_stable_for_appending():
    """ref_bar_index 在 K 线追加时返回相同的绝对位置。

    这是 F1 真增量识别的关键: ref 必须稳定指向同一根 K 线。
    """
    from cl_app.services.cl_object_cache import _ref_bar_index

    # n=200..1000 时 ref_idx 都应 clamp 到 50
    for n in (200, 205, 300, 1000):
        assert _ref_bar_index(n) == 50, f"n={n} ref_idx 应该是 50, 实际 {_ref_bar_index(n)}"

    # 短序列时 ref_idx = n//4
    assert _ref_bar_index(40) == 10
    assert _ref_bar_index(12) == 3
