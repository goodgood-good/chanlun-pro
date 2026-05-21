"""tests/signal_monitor/conftest.py — 复用 core 测试的合成 K 线 fixture。

`cl_with_synthetic_klines` 依赖 `cl_config`，两者都需 import 进本 conftest
命名空间，pytest 才能解析 fixture 依赖。合成 K 线在内存生成、确定性，
不涉及 csv 浮点噪声问题。
"""
from tests.core.conftest import (  # noqa: F401
    cl_config,
    cl_with_synthetic_klines,
)
