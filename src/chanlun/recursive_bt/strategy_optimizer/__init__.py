"""chanlun.recursive_bt.strategy_optimizer —— 策略候选目录 + 评分/报告/决策工具集。

职责模块包(facade re-export):
constants(路径常量)/models(数据类)/candidates(候选目录)/utils(数值helper)/
scoring(评分)/reports_mtf3·reports_strategy·reports_attribution·reports_regime(各类报告)/
decision(优化报告+决策覆盖)/cli(命令行)。
公共 API `from chanlun.recursive_bt.strategy_optimizer import <符号>` 经此 facade 不变。
"""
from chanlun.recursive_bt.strategy_optimizer.constants import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.models import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.candidates import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.scoring import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.reports_mtf3 import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.reports_strategy import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.reports_attribution import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.reports_regime import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.decision import *  # noqa: F401,F403
from chanlun.recursive_bt.strategy_optimizer.cli import *  # noqa: F401,F403
