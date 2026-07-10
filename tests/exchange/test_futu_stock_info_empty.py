# -*- coding: utf-8 -*-
"""R12-#4: ExchangeFutu.stock_info() 对 RET_OK+空 DataFrame 未做长度校验,IndexError 冒泡阻断
港股开平仓(R11-#4 ExchangeQMT.stock_info 同模式未修兄弟)。

get_stock_basicinfo 底层是 repeated 字段 staticInfoList,对已退市/未识别代码惯常返回 RET_OK 但
data 为空 DataFrame → data.iloc[0][...] 抛 IndexError,而非按声明的 [Dict, None] 返回 None。
trader_hk_stock.py open_buy/open_sell/close_buy/close_sell 四处均 `if stock_info is None: return False`
契约,预期返回 None 而非抛异常。修复=`if ret == RET_OK:` 分支内先判 `if data is None or len(data)==0: return None`。

exchange_futu 依赖 futu SDK(本环境未装),注入最小 stub(仅供 import 期注解/名称求值),
再 object.__new__ 绕 __init__ + monkeypatch CTX 直测 stock_info 纯逻辑。★不连柜台。
"""
import sys
import types

if "futu" not in sys.modules:
    _fm = types.ModuleType("futu")
    _fm.OpenSecTradeContext = object
    _fm.OpenQuoteContext = object
    _fm.TrdMarket = type("TrdMarket", (), {"HK": "HK"})
    _fm.RET_OK = "RET_OK"
    _fm.SecurityType = type("SecurityType", (), {"STOCK": "STOCK"})
    sys.modules["futu"] = _fm

import pandas as pd  # noqa: E402
from chanlun.exchange import exchange_futu  # noqa: E402
from chanlun.exchange.exchange_futu import ExchangeFutu  # noqa: E402


class _FakeCtx:
    def __init__(self, ret, data):
        self._ret = ret
        self._data = data

    def get_stock_basicinfo(self, *a, **k):
        return self._ret, self._data


def _info(code):
    # stock_info 不访问 self, 直调未绑定方法绕开 __init__/__new__
    return ExchangeFutu.stock_info(None, code)


def test_stock_info_empty_df_returns_none(monkeypatch):
    """退市/未识别: RET_OK 但空 DataFrame → 返回 None 而非 IndexError。"""
    monkeypatch.setattr(exchange_futu, "CTX", lambda: _FakeCtx(exchange_futu.RET_OK, pd.DataFrame()))
    assert _info("HK.00700") is None


def test_stock_info_ret_error_returns_none(monkeypatch):
    """ret != RET_OK 仍返回 None(回归保护)。"""
    monkeypatch.setattr(exchange_futu, "CTX", lambda: _FakeCtx("RET_ERROR", None))
    assert _info("HK.00700") is None


def test_stock_info_valid_returns_dict(monkeypatch):
    """正常: 返回含 code/name/lot_size/stock_type 的 dict(回归保护)。"""
    df = pd.DataFrame([{"code": "HK.00700", "name": "腾讯控股", "lot_size": 100, "stock_type": "STOCK"}])
    monkeypatch.setattr(exchange_futu, "CTX", lambda: _FakeCtx(exchange_futu.RET_OK, df))
    info = _info("HK.00700")
    assert info is not None
    assert info["code"] == "HK.00700"
    assert info["name"] == "腾讯控股"
    assert info["lot_size"] == 100