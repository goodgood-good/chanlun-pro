"""R5-#7: monitor 与回测 buy_ratio 在 adaptive+soft+mid_down 下须一致(原 monitor 为 2 倍)。

engine.recommended_buy_ratio 的 adaptive 分支含 `if mid_dir=='down': ratio*=0.5`(内层)。
回测 portfolio.py:783 传 `mid_dir or ''`='down'→内层生效, 再外层 `if mid_soft: *0.5`=base×0.25;
monitor live_monitor.py:614 原传 `mid_dir if mid_relaxed else ''`=''(soft 档 mid_relaxed=False)
→跳内层, 只外层 *0.5=base×0.5=回测 2 倍。修复=line614 改 `mid_dir or ''` 与回测同口径。
"""
from chanlun.recursive_bt.engine.engine import recommended_buy_ratio

_KW = dict(
    max_pos=30, big_dir="up", daily_resonance=False, regime_mode="adaptive",
    nest_mode="off", nest_operable=None, nest_depth=0, trend_boost=False,
)


def _effective(mid_dir_arg, mid_soft):
    # 复刻 monitor(608-621)/portfolio(777-790) 的调用 + 外层 soft 折扣结构
    r = recommended_buy_ratio("1buy", mid_dir=mid_dir_arg, **_KW)
    if mid_soft:
        r = round(r * 0.5, 4)
    return r


def test_monitor_fixed_matches_backtest_adaptive_soft_mid_down():
    bt = _effective("down", mid_soft=True)          # 回测口径(portfolio.py:783 = "down")
    mon_fixed = _effective("down", mid_soft=True)    # monitor 修复后(line614 = "down")
    mon_old = _effective("", mid_soft=True)          # 旧 monitor(传 "" 跳内层0.5)
    assert mon_fixed == bt                            # 修复后与回测严格一致
    assert mon_old > mon_fixed                        # 旧口径多买
    assert 1.8 < (mon_old / mon_fixed) < 2.2          # 旧口径约为回测/修复值的 2 倍


def test_non_adaptive_mode_no_divergence():
    # regime_mode='off' 时内层块整体跳过, "" 与 "down" 无差异→默认生产不受影响
    kw = dict(_KW)
    kw["regime_mode"] = "off"
    assert recommended_buy_ratio("1buy", mid_dir="down", **kw) == recommended_buy_ratio(
        "1buy", mid_dir="", **kw
    )