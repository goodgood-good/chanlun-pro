"""strategy_optimizer 报告/CLI 共享的回测 summary 路径常量 + regime 顺序。

从 _impl 抽出:被 regime/attribution 报告族 + CLI(make_arg_parser) + 外部
live_monitor 共享引用;纯数据(硬编码 D:/chanlun_pro/reports/*.json),零依赖。
"""

A_MTF3_SELL3_REBUY_MID3_DEFAULT_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_default_summary.json"
)
A_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_sell3_rebuy_mid3_summary.json"
)
A_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_sell3_rebuy3_summary.json"
)
A_MTF3_SELL3_REBUY3_UP_CANDIDATE_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_sell3_rebuy3_up_summary.json"
)
US_MTF3_DEFAULT_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_default_summary.json"
)
US_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_sell3_rebuy3_summary.json"
)
US_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_sell3_rebuy_mid3_summary.json"
)
US_2026Q1_MTF3_DEFAULT_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_2026q1_default_summary.json"
)
US_2026Q1_MTF3_SELL3_REBUY3_CANDIDATE_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_2026q1_sell3_rebuy3_summary.json"
)
US_2026Q1_MTF3_SELL3_REBUY_MID3_CANDIDATE_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_2026q1_sell3_rebuy_mid3_summary.json"
)
A_MTF3_REGIME_BEAR3BOOST_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_regime_bear3boost_summary.json"
)
A_MTF3_REGIME_WEAK1REDUCE_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_regime_weak1reduce_summary.json"
)
A_MTF3_REGIME_COMBO_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_regime_combo_summary.json"
)
A_ALL_5M30M_DEFAULT_SUMMARY = (
    "D:/chanlun_pro/reports/walk_forward_a_5m30m_all5145_max30_off_segments_summary.json"
)
A_ALL_5M30M_REGIME_BEAR3BOOST_SUMMARY = (
    "D:/chanlun_pro/reports/walk_forward_a_5m30m_all5145_max30_off_regime_bear3boost_summary.json"
)
A_ALL_5M30M_REGIME_COMBO_SUMMARY = (
    "D:/chanlun_pro/reports/walk_forward_a_5m30m_all5145_max30_off_regime_combo_summary.json"
)
A_MTF3_REGIME_COMBO_B140_SUMMARY = (
    "D:/chanlun_pro/reports/a_bt_mtf3_1m5m30m_regime_combo_b140_summary.json"
)
A_ALL_5M30M_REGIME_COMBO_B140_SUMMARY = (
    "D:/chanlun_pro/reports/walk_forward_a_5m30m_all5145_max30_off_regime_combo_b140_summary.json"
)
US_MTF3_REGIME_WEAK1REDUCE_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_regime_weak1reduce_summary.json"
)
US_2026Q1_MTF3_REGIME_WEAK1REDUCE_SUMMARY = (
    "D:/chanlun_pro/reports/us_core9_mtf3_2026q1_regime_weak1reduce_summary.json"
)


REGIME_ORDER = ("bull", "range", "bear")
