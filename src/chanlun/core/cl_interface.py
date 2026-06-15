# -*- coding: utf-8 -*-
"""
CL_*** 配置项，可以在调用缠论计算时，通过传递 config 变量进行变更，如 config['CL_BI_FX_STRICT'] = True

本模块已拆分到 chanlun.core.types/。此处保留为 re-export facade:
① 兼容旧 import 路径;② 作为老 pickle 的 module-alias(旧对象 __module__=本模块,
加载时 from chanlun.core.cl_interface import X 须命中,故必须永久保留)。
"""
from chanlun.core.types import *          # noqa: F401,F403
from chanlun.core.types import __all__    # noqa: F401

# 旧 cl_interface 顶层 `from typing import ...` 把这些名泄漏成可从本模块导入;
# 个别调用方依赖了(如 strategy_a_d_mmd_test 的 `from ...cl_interface import Dict, List`),
# facade 显式补回,保持 import 兼容。
from typing import Any, Dict, List, Optional, Tuple, Union  # noqa: F401
