"""审计 D1-HIGH-2: CTP 平仓 SHFE/INE 平今平昨 offset 规划(_plan_close_offsets 纯逻辑)。

延迟 import: trader_ctp / openctp_ctp 在 conftest 的 session autouse stub 装好后才导入
(模块顶层导入会在 collection 期早于 stub 而 ModuleNotFoundError)。
"""


def _plan(*args):
    from chanlun.trader.trader_ctp import _plan_close_offsets

    return _plan_close_offsets(*args)


def _c():
    from openctp_ctp.thosttraderapi import (
        THOST_FTDC_OF_Close,
        THOST_FTDC_OF_CloseToday,
    )

    return THOST_FTDC_OF_CloseToday, THOST_FTDC_OF_Close


def test_non_shfe_single_close():
    CT, CL = _c()
    assert _plan("CFFEX", 3, 3, 0) == [(CL, 3)]  # 中金所不分今昨
    assert _plan("DCE", 2, 2, 1) == [(CL, 2)]  # 大商所不分今昨


def test_shfe_all_today():
    CT, CL = _c()
    assert _plan("SHFE", 3, 3, 0) == [(CT, 3)]  # 全当日仓 → 平今


def test_shfe_all_yesterday():
    CT, CL = _c()
    assert _plan("SHFE", 3, 3, 3) == [(CL, 3)]  # 全昨仓 → Close


def test_shfe_mixed_split():
    CT, CL = _c()
    assert _plan("SHFE", 5, 5, 2) == [(CT, 3), (CL, 2)]  # today=3 平今 + 昨2 Close


def test_shfe_partial_close_today_first():
    CT, CL = _c()
    assert _plan("SHFE", 4, 5, 2) == [(CT, 3), (CL, 1)]  # 只平4: 优先平今3 + 平昨1


def test_ine_treated_like_shfe():
    CT, CL = _c()
    assert _plan("INE", 2, 2, 0) == [(CT, 2)]


def test_missing_position_fallback_close():
    CT, CL = _c()
    assert _plan("SHFE", 3, 0, 0) == [(CL, 3)]  # 快照缺 position → 保守单 Close


def test_amount_exceeds_position_remainder_close():
    CT, CL = _c()
    assert _plan("SHFE", 5, 3, 0) == [(CT, 3), (CL, 2)]  # amount>position: 平今today + 兜底Close


def test_zero_amount_empty():
    CT, CL = _c()
    assert _plan("SHFE", 0, 3, 0) == []
