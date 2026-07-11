# -*- coding: utf-8 -*-
"""R19-CRIT: CTP OnRtnDepthMarketData 用超集 dict 构造 Tick(**tick_data) 必抛 TypeError。

tick_data 含 time/amount/buy{i}/buy{i}_volume/sell{i}/sell{i}_volume 等非 Tick 字段
(Tick 只有 code/last/buy1/sell1/high/low/open/volume/rate 9 个), Tick(**tick_data) 运行时
抛 TypeError → :206 ticks_cache 写入永不执行 → ticks_cache 恒空 → MarketCTP.ticks() 恒返 {}
→ trader_ctp 全部 7 处 `if code not in tick: return False`(开多/开空/平多/平空/锁仓/强平/止损)
静默失效, CTP 实盘完全无法交易且风控强平失效, 无异常抛出极难运维发现。

修复=抽 MarketCTP._tick_from_data 静态方法只取 9 个已声明字段显式构造。可达=reboot_trader_ctp
→ MarketCTP + CTPTrader 常驻循环。realmoney: mock 单测, 真金灰度留用户。
"""
import sys
import types

import pytest

# openctp_ctp 属可选 futures extra, 测试环境未装 → sys.modules 注入桩(须在 import exchange_ctp 前)
if "openctp_ctp" not in sys.modules:
    _pkg = types.ModuleType("openctp_ctp")
    _mod = types.ModuleType("openctp_ctp.thostmduserapi")
    for _n in [
        "CThostFtdcDepthMarketDataField",
        "CThostFtdcMdApi",
        "CThostFtdcReqAuthenticateField",
        "CThostFtdcReqUserLoginField",
        "CThostFtdcRspInfoField",
        "CThostFtdcRspUserLoginField",
    ]:
        setattr(_mod, _n, type(_n, (), {}))
    _pkg.thostmduserapi = _mod
    sys.modules["openctp_ctp"] = _pkg
    sys.modules["openctp_ctp.thostmduserapi"] = _mod

from chanlun.exchange.exchange import Tick  # noqa: E402
from chanlun.exchange.exchange_ctp import MarketCTP  # noqa: E402


def _superset_tick_data(has_level2_depth=False):
    """模拟 OnRtnDepthMarketData 构造的 tick_data(Tick 的超集)。"""
    td = {
        "code": "rb2510",
        "time": "2024-01-02 14:30:00",  # 非 Tick 字段
        "last": 3500.0,
        "high": 3520.0,
        "low": 3480.0,
        "open": 3490.0,
        "volume": 100,
        "amount": 350000.0,  # 非 Tick 字段
        "rate": 0.5,
    }
    n = 20 if has_level2_depth else 5
    for i in range(1, n + 1):  # buy{i}/buy{i}_volume/sell{i}/sell{i}_volume 全非 Tick 字段
        td[f"buy{i}"] = 3500.0 - i
        td[f"buy{i}_volume"] = 10 * i
        td[f"sell{i}"] = 3500.0 + i
        td[f"sell{i}_volume"] = 8 * i
    return td


def test_tick_from_superset_dict_no_typeerror_5档():
    """5 档超集 dict → _tick_from_data 须返回合法 Tick(只取 9 字段), 不崩。"""
    td = _superset_tick_data(has_level2_depth=False)
    tick = MarketCTP._tick_from_data(td)
    assert isinstance(tick, Tick)
    assert tick.last == 3500.0
    assert tick.buy1 == 3499.0
    assert tick.sell1 == 3501.0
    assert tick.high == 3520.0
    assert tick.rate == 0.5


def test_tick_from_superset_dict_no_typeerror_20档():
    """20 档 Level2 超集 dict → 同样只取 9 字段, 不崩。"""
    td = _superset_tick_data(has_level2_depth=True)
    tick = MarketCTP._tick_from_data(td)
    assert isinstance(tick, Tick)
    assert tick.buy1 == 3499.0


def test_old_direct_construction_would_raise_typeerror():
    """钉死根因: Tick(**超集) 确实抛 TypeError(证明 _tick_from_data 必要性)。"""
    td = _superset_tick_data(has_level2_depth=False)
    with pytest.raises(TypeError):
        Tick(**td)
