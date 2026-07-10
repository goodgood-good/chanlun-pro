"""R10-cl_object_cache63: _ref_bar_index=min(50,n//4) 随 n 漂移致增量签名假失配退化全量。

12<=n<200 时 _ref_bar_index(n)=n//4 随 n 增长: old_n=20→ref_idx=5, 追加到 new_n=24→
ref_idx=6, _is_extending_signature 的 old_sig[3:8]!=new_sig[3:8](比较了两根不同日期的 K线)
→ 恒判非 extending → get_or_compute_cl 放弃 Path2 增量复用退回 Path3 全量重建。纯性能
(全量与增量产出等价), 影响月K/新股等小根数标的默认 UI 路径。hist_fp(签名位8)已完整覆盖
[0,n-2) 历史订正检测, ref-bar 5元组冗余 → 移除该冗余校验, 靠 hist_fp 判 extending。
"""
import pandas as pd

from cl_app.services.cl_object_cache import (
    _compute_kline_signature,
    _is_extending_signature,
    _ref_bar_index,
)


def _mk_df(n):
    dates = pd.date_range("2024-01-01", periods=n, freq="D")
    return pd.DataFrame([
        {"date": dates[i], "open": 10 + i * 0.1, "high": 10.5 + i * 0.1,
         "low": 9.5 + i * 0.1, "close": 10.2 + i * 0.1, "volume": 1000.0}
        for i in range(n)
    ])


def test_small_root_extending_across_quarter_boundary_not_false_mismatch():
    # n=20→24 跨 n//4 边界(ref_idx 5→6), 前缀不变 → 必判 extending(修复前 ref drift 假失配)
    assert _ref_bar_index(20) != _ref_bar_index(24), "前提: 该窗口 ref_idx 确实漂移"
    df = _mk_df(24)
    old = df.iloc[:20].reset_index(drop=True)
    new = df.iloc[:24].reset_index(drop=True)
    old_sig = _compute_kline_signature(old)
    new_sig = _compute_kline_signature(new)
    assert _is_extending_signature(old_sig, new_sig, new) is True, (
        "小根数跨 n//4 边界的纯追加被 ref drift 假失配退化全量(应判 extending)"
    )


def test_history_revision_still_blocks_extending():
    # 安全回归: 前缀中一根被订正 → hist_fp 失配 → 不 extending(全量重建), ref-bar 移除后仍守住
    df = _mk_df(24)
    old = df.iloc[:20].reset_index(drop=True)
    new = df.iloc[:24].reset_index(drop=True)
    old_sig = _compute_kline_signature(old)
    revised = new.copy()
    revised.loc[3, "close"] = 999.0  # 订正历史第 3 根
    revised.loc[3, "high"] = 999.0
    new_sig = _compute_kline_signature(revised)
    assert _is_extending_signature(old_sig, new_sig, revised) is False, (
        "历史订正必须被 hist_fp 拦截(不可当 extending 增量, 否则下发陈旧缠论)"
    )


def test_pure_append_no_history_change_extends():
    # 大根数(n>=200, ref_idx 恒 50)纯追加仍 extending(回归: 修复不改大根数已正确行为)
    df = _mk_df(260)
    old = df.iloc[:250].reset_index(drop=True)
    new = df.iloc[:260].reset_index(drop=True)
    old_sig = _compute_kline_signature(old)
    new_sig = _compute_kline_signature(new)
    assert _is_extending_signature(old_sig, new_sig, new) is True