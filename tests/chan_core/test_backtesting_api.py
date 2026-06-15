"""backtesting 交易地基公共 API 契约网。

backtesting/ 是命名错位的**活跃交易地基**(base 5 抽象类 + BackTestTrader 被所有实盘
交易器继承,见 memory project_chanlun_dir_structure_audit)。此网钉死其公共 API 契约,
作为未来命名重构(把地基移 trading/ + backtesting facade re-export)的安全网——
重构前后此网必须逐项全绿,保证 ~15 调用方(含实盘 trader_*/券商 cl_myquant·vnpy·wtpy)
的 `from chanlun.backtesting.base import ...` 不破。
"""
from abc import ABC

import inspect


def test_base_public_symbols():
    """base 模块对外暴露的 7 个公共符号齐全。"""
    from chanlun.backtesting import base

    for name in ("POSITION", "Operation", "MarketDatas", "Trader", "Strategy",
                 "fee_a", "fee_us"):
        assert hasattr(base, name), f"backtesting.base 缺公共符号 {name}"


def test_base_direct_imports_work():
    """~15 调用方都走这条 import 路径,逐字钉死(facade 化后仍须可解析)。"""
    from chanlun.backtesting.base import (  # noqa: F401
        POSITION, Operation, MarketDatas, Trader, Strategy, fee_a, fee_us,
    )

    assert callable(fee_a) and callable(fee_us)


def test_abc_contracts():
    """三个抽象基类身份不变(地基契约)。"""
    from chanlun.backtesting.base import MarketDatas, Strategy, Trader

    for cls in (MarketDatas, Trader, Strategy):
        assert issubclass(cls, ABC), f"{cls.__name__} 应为 ABC"


def test_backtest_trader_is_concrete_trader():
    """BackTestTrader 是 Trader 的具体子类(实盘交易器基类,可实例化)。"""
    from chanlun.backtesting.backtest_trader import BackTestTrader
    from chanlun.backtesting.base import Trader

    assert issubclass(BackTestTrader, Trader)
    assert not inspect.isabstract(BackTestTrader), "BackTestTrader 应是具体类"
