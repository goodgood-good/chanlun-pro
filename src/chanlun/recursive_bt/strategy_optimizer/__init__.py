"""chanlun.recursive_bt.strategy_optimizer —— 策略候选目录 + 评分/报告/决策工具集。

从单文件 strategy_optimizer.py(5585行/79公共符号)拆为包(facade re-export)。
首批已抽 models(6 数据类);剩余在 _impl,待按职责块继续拆
(candidates/scoring/reports/overrides/trade_analysis/cli)。
公共 API `from chanlun.recursive_bt.strategy_optimizer import <符号>` 经此 facade 不变。
"""
from chanlun.recursive_bt.strategy_optimizer.constants import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.models import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.candidates import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.scoring import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.reports_mtf3 import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.reports_strategy import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer._impl import *  # noqa: F401,F403
