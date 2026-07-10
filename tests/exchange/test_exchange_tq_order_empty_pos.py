# -*- coding: utf-8 -*-
"""R13-#3: ExchangeTq.order() 平仓分支 pos = self.positions(code)[code] 未防空。

positions(code) 在标的无持仓时返回 {}(pos_long==pos_short==0 被过滤),此时
{}[code] → KeyError 使 order() 崩溃而非优雅 return False。可达=天勤实盘期货
平仓路径遇"账本以为有仓但柜台已平/重复平仓/竞态"时整条下单调用抛未捕获异常。

★天勤实盘路径:本修复是纯防御性 crash-guard(无持仓即 return False 不发单,
严格比崩溃安全,不改任何成功下单语义),可用 mock positions 单测验证;真金灰度
留用户拍板(与 CTP N1/N2、Binance N5 同口径)。
"""
import sys
import types
from types import SimpleNamespace

# ---- stub tqsdk(未装 futures extras 时) ----
if "tqsdk" not in sys.modules:
    _tq = types.ModuleType("tqsdk")
    _objs = types.ModuleType("tqsdk.objs")

    class _Account:
        pass

    class _Position:
        pass

    class _Quote:
        pass

    _objs.Account = _Account
    _objs.Position = _Position
    _objs.Quote = _Quote
    _tq.objs = _objs
    # 类体注解 g_api: tqsdk.TqApi / g_account: tqsdk.TqAccount 定义时求值,须存在
    for _name in ("TqApi", "TqAccount", "TqAuth", "TqKq"):
        setattr(_tq, _name, type(_name, (), {}))
    sys.modules["tqsdk"] = _tq
    sys.modules["tqsdk.objs"] = _objs

import chanlun.exchange.exchange_tq as tq_mod  # noqa: E402


def _cls():
    # @fun.singleton 把类包成 wrapper 函数,原始类在 __wrapped__
    return getattr(tq_mod.ExchangeTq, "__wrapped__", tq_mod.ExchangeTq)


def test_order_close_empty_positions_returns_false():
    cls = _cls()
    fake = SimpleNamespace(
        get_api=lambda use_account=True: object(),
        g_account_enable=True,
        positions=lambda code: {},  # 无持仓
    )
    # 修复前: {}[code] → KeyError;修复后: 优雅 return False
    result = cls.order(fake, "SHFE.rb2401", "close_long", 1)
    assert result is False


def test_order_close_short_empty_positions_returns_false():
    cls = _cls()
    fake = SimpleNamespace(
        get_api=lambda use_account=True: object(),
        g_account_enable=True,
        positions=lambda code: {},
    )
    result = cls.order(fake, "SHFE.rb2401", "close_short", 1)
    assert result is False