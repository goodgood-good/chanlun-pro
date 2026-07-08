"""R1-C2: --regime-lookback-days 的 argparse default 不得遮蔽 config/runtime override。

default=20(truthy) 会让 main 的 `args.x or market_config.get(x) or 20` 恒短路在 20,
config 与 RUNTIME_OVERRIDE_KEYS 宣称可热覆盖的同名键永久静默失效。
修复 = default=None + None-aware 解析(镜像同文件 mtf3_cache_bt_sample_size 惯例)。
"""
from chanlun.recursive_bt.monitor.live_monitor import (
    RUNTIME_OVERRIDE_KEYS,
    make_arg_parser,
)


def test_regime_lookback_days_parser_default_is_none():
    # default 必须是 None: 任何 truthy default 都会在 main 的 or 链里遮蔽 config/override
    args = make_arg_parser().parse_args([])
    assert args.regime_lookback_days is None


def test_regime_lookback_days_cli_explicit_wins():
    args = make_arg_parser().parse_args(["--regime-lookback-days", "60"])
    assert args.regime_lookback_days == 60


def test_regime_lookback_days_still_hot_overridable_key():
    # 该键在 RUNTIME_OVERRIDE_KEYS 中(宣称可热覆盖); parser default=None 是其生效前提
    assert "regime_lookback_days" in RUNTIME_OVERRIDE_KEYS