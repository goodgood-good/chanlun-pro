"""Round12 final-gate MEDIUM-1: 买点比例乘数 0.0(=禁买该类)在实盘 apply_bs_point_ratio_multiplier
被 `float(... or 1.0)` 静默吞成 1.0(全额买), 而回测 _apply_buy_ratio_multiplier 正确保留 0.0(注释
明确警告"显式0.0必须保留,不能用 or 1.0")。→ 操盘手配 {"3":0.0} 禁买三类, 回测据此验证"不碰", 实盘却
满额买入被禁信号 = 真金静默部署在被禁买点类上。对齐: 仅缺失键/None 兜底 1.0, 显式 0.0 保留。"""

from chanlun.recursive_bt.monitor.live_monitor import apply_bs_point_ratio_multiplier
from chanlun.recursive_bt.sim.portfolio import _apply_buy_ratio_multiplier


def test_live_buy_multiplier_preserves_explicit_zero():
    # {"3":0.0} = 禁买三类买点; 实盘应返回 0.0(不买), 不得被 or 1.0 吞成满额 0.2
    adjusted, mult = apply_bs_point_ratio_multiplier(0.2, "3buy", {"3": 0.0})
    assert adjusted == 0.0, f"禁买三类被吞成买 {adjusted}"
    assert mult == 0.0


def test_live_buy_multiplier_matches_backtest():
    # 实盘与回测对同一乘数配置必须同口径(0.0禁买 / 非零一致 / 缺失键兜底1.0)
    cases = [
        ({"3": 0.0}, 0.2, "3buy"),
        ({"1": 0.5}, 0.2, "1buy"),
        ({}, 0.2, "3buy"),
        ({"2": 1.4}, 0.3, "2buy"),
    ]
    for m, ratio, bs in cases:
        live_adj, _ = apply_bs_point_ratio_multiplier(ratio, bs, m)
        bt_adj = _apply_buy_ratio_multiplier(ratio, bs, m)
        assert abs(live_adj - bt_adj) < 1e-9, f"live={live_adj} bt={bt_adj} m={m}"