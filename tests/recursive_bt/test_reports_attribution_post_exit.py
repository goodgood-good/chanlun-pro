# -*- coding: utf-8 -*-
"""R1-F2-2 + R1-F2-3: 买卖点归因报告两处。

F2-2: _summarize_trade_group 把 post_exit_ret_* 的 0.0 占位(写入端 post_exit_bars<
horizon 时保持 dataclass 默认,final 强平/窗口末退出必占位)当真实观测:样本数虚报为
全部交易数、均值被 0 稀释;下游 _sell_ratio_guidance 在虚数上做 review_scale_out 判定。
修复=按写入端专门记录的 post_exit_bars>=horizon 甄别。

F2-3: render_bs_point_attribution_markdown 买/卖两表 Trades 列填市场全量
(market_report['trade_count'])而非组内 group['trade_count'],与同行 guidance 的
sample-thin 结论自相矛盾;JSON 权威输出本就正确,仅 md 展示层错。
"""
from chanlun.recursive_bt.strategy_optimizer.reports_attribution import (
    _summarize_trade_group,
    render_bs_point_attribution_markdown,
)


def _rows():
    real = [{"ret": "0.02", "post_exit_ret_5": "0.01", "post_exit_ret_20": "0.05",
             "post_exit_ret_60": "0.08", "post_exit_mfe_20": "0.06",
             "post_exit_mae_20": "-0.01", "post_exit_bars": "25"} for _ in range(3)]
    placeholder = [{"ret": "0.01", "post_exit_ret_5": "0.0", "post_exit_ret_20": "0.0",
                    "post_exit_ret_60": "0.0", "post_exit_mfe_20": "0.0",
                    "post_exit_mae_20": "0.0", "post_exit_bars": "0"} for _ in range(5)]
    return real + placeholder


def test_post_exit_placeholder_rows_excluded():
    g = _summarize_trade_group("3", _rows(), min_trades=3)
    # 旧码: sample_count=8(全部行), avg=0.01875(被 5 个占位 0 稀释)
    assert g["post_exit_sample_count"] == 3, g["post_exit_sample_count"]
    assert abs(g["avg_post_exit_ret_20"] - 0.05) < 1e-12, g["avg_post_exit_ret_20"]
    assert abs(g["avg_post_exit_mfe_20"] - 0.06) < 1e-12
    # 60bar 后窗: 3 行 bars=25 <60 → 全占位排除, 样本 0 → 均值 0
    assert g["avg_post_exit_ret_60"] == 0.0
    # 交易统计口径不变: trade_count 仍是全部 8 笔
    assert g["trade_count"] == 8


def test_markdown_trades_column_uses_group_count():
    report = {
        "generated_at": "x",
        "markets": [{
            "market": "a",
            "trade_count": 3000,
            "groups": [{"bs_class": "1", "trade_count": 5, "win_rate": 0.6,
                        "avg_return": 0.01, "compound_return": 0.05,
                        "max_drawdown": 0.02, "avg_hold_hours": 4.0}],
            "ratio_guidance": [],
            "sell_groups": [{"bs_class": "1", "trade_count": 7, "win_rate": 0.5,
                             "avg_return": 0.0, "avg_post_exit_ret_20": 0.0,
                             "avg_post_exit_mfe_20": 0.0, "avg_post_exit_mae_20": 0.0,
                             "max_drawdown": 0.0}],
            "sell_ratio_guidance": [],
        }],
    }
    md = render_bs_point_attribution_markdown(report)
    assert "| a | 5 | 1 |" in md, md        # 买表: 组内 5 笔
    assert "| a | 7 | 1 |" in md, md        # 卖表: 组内 7 笔
    assert "| a | 3000 |" not in md, md     # 旧码: 两表全填市场总量 3000