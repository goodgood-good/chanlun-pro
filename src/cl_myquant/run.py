# coding=utf-8
from __future__ import absolute_import, print_function

from gm.api import ADJUST_PREV, MODE_BACKTEST, run, set_serv_addr, set_token

from chanlun import config

# 远程执行时需要指定掘金终端地址，本地运行可留空；参见 https://www.myquant.cn/docs/gm3_faq/154#b244aeed0032526e
set_serv_addr(config.GM_SERVER_ADDR)
set_token(config.GM_TOKEN)

strategy_filename = "strategy/strategy_test.py"

# TODO 这里的 strategy_id 替换为自己的（掘金平台生成）
run(
    strategy_id="2cd63317-8c4a-11f0-987e-5847ca718ef4",
    filename=strategy_filename,
    mode=MODE_BACKTEST,
    # TODO 这里的 token 替换为自己的（用户-密钥管理中获取）
    token=config.GM_TOKEN,
    backtest_start_time="2025-06-01 09:30:00",
    backtest_end_time="2025-09-01 15:00:00",
    backtest_adjust=ADJUST_PREV,
    backtest_initial_cash=10000000,
    backtest_commission_ratio=0.0001,
    backtest_slippage_ratio=0.0001,
    backtest_match_mode=1,
)
