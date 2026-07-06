"""combo_b140 采纳为生产默认(2026-07-06)的锁定测试。"""
import json
from chanlun.recursive_bt.backtest.live_backtest import make_arg_parser


def test_regime_defaults_are_combo_b140():
    args = make_arg_parser().parse_args([])
    assert args.regime_mode == "adaptive"
    mult = json.loads(args.regime_bs_ratio_multipliers_json)
    assert mult == {"bear": {"3": 1.4}, "bull": {"1": 0.5}, "range": {"1": 0.5}}
    assert args.regime_lookback_days == 20

def test_require_default_is_tech():
    """rs 门控(audit §十八)为强候选,证据仅同年 30 池(无全 A/跨年),默认维持 tech。"""
    args = make_arg_parser().parse_args([])
    assert args.require == "tech"
