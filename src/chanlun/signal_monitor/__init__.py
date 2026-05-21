# -*- coding: utf-8 -*-
"""
缠论买卖点监控 —— 独立子包。

中枢-free 信号引擎 + 自动监控 + 手动图上对比，全部逻辑收拢在本包内，
与项目原有代码（``core`` / ``db`` / ``monitor`` / ``alert_tasks``）解耦：
本包**单向依赖**它们，对原有代码零侵入（唯一接线在 web 的 ``create_app``）。

模块一览：
  - ``strength_compare`` —— 中枢-free 力度对比内核 + 手动对比
  - ``cl_signal``        —— 信号对象 ClSignal
  - ``evaluator``        —— 信号评估器（四支柱）
  - ``db_models``        —— 两张表的 ORM 模型
  - ``repository``       —— 任务/记录 CRUD（自管建表）
  - ``monitor``          —— monitoring_signal_code 单标的监控编排
  - ``scheduler``        —— APScheduler 调度注册
  - ``web``              —— Flask 蓝图（手动对比接口）

``__init__`` 只再导出「轻量核心」（仅依赖 core.cl_interface）；
``monitor`` / ``scheduler`` / ``web`` 牵涉 flask/apscheduler/exchange，
由各自的调用方按需直接 import，避免 ``import chanlun.signal_monitor`` 变重。
"""
from chanlun.signal_monitor.cl_signal import ClSignal
from chanlun.signal_monitor.evaluator import ClSignalEvaluator, EvaluatorConfig
from chanlun.signal_monitor.strength_compare import (
    StrengthCompareResult,
    compare_strength,
    compare_strength_raw,
    manual_strength_compare,
)

__all__ = [
    "StrengthCompareResult",
    "compare_strength",
    "compare_strength_raw",
    "manual_strength_compare",
    "ClSignal",
    "ClSignalEvaluator",
    "EvaluatorConfig",
]
