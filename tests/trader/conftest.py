"""tests/trader 离线单测脚手架。

真实柜台 SDK (openctp_ctp) 与天勤 (tqsdk) 未在本环境安装, 且这是真实下单代码,
单测一律 **离线**: 通过向 sys.modules 注入最小 stub, 使 trader_ctp / exchange_ctp
可被 import, 从而对纯逻辑分支 (M3 成交判定、H2 方向、H1 风控、H3-c order_ref 恢复)
做真实代码覆盖, 而不连任何柜台。

注: stub 仅提供被 import 的符号; 不模拟柜台行为。涉及真实下单/回报时序的部分
(CTP ReqOrderInsert 真单路径) 无法离线单测, 仅靠 py_compile + 逻辑分支单测覆盖。
"""

import pathlib
import sys
import types

import pytest

# 让 tests/trader 能 import src 下的 chanlun (src 布局, 与 tests/web/conftest 一致)
_root = pathlib.Path(__file__).resolve().parents[2]
_src = str(_root / "src")
if _src not in sys.path:
    sys.path.insert(0, _src)

# ---- CTP OrderStatus 标准常量值 (与 openctp_ctp.thosttraderapi 一致) ----
_OST = {
    "THOST_FTDC_OST_AllTraded": "0",
    "THOST_FTDC_OST_PartTradedQueueing": "1",
    "THOST_FTDC_OST_PartTradedNotQueueing": "2",
    "THOST_FTDC_OST_NoTradeQueueing": "3",
    "THOST_FTDC_OST_NoTradeNotQueueing": "4",
    "THOST_FTDC_OST_Canceled": "5",
}


def _make_field_class(name):
    """生成一个可任意赋属性的轻量 CThostFtdc* stub 类。"""

    def __init__(self, *a, **k):
        pass

    return type(name, (), {"__init__": __init__})


def _install_openctp_stub():
    """向 sys.modules 注入 openctp_ctp.thosttraderapi / thostmduserapi stub。"""
    if "openctp_ctp" in sys.modules:
        return

    pkg = types.ModuleType("openctp_ctp")
    trader_mod = types.ModuleType("openctp_ctp.thosttraderapi")
    md_mod = types.ModuleType("openctp_ctp.thostmduserapi")

    # OrderStatus 等常量
    for k, v in _OST.items():
        setattr(trader_mod, k, v)
    for cname in [
        "THOST_FTDC_TC_GFD",
        "THOST_FTDC_VC_AV",
        "THOST_FTDC_CC_Immediately",
        "THOST_FTDC_D_Buy",
        "THOST_FTDC_D_Sell",
        "THOST_FTDC_HF_Speculation",
        "THOST_FTDC_OF_Close",
        "THOST_FTDC_OF_CloseToday",
        "THOST_FTDC_OF_Open",
        "THOST_FTDC_OPT_LimitPrice",
    ]:
        setattr(trader_mod, cname, cname)  # 用名字字符串当占位常量值

    # Field / Api 类
    for cls_name in [
        "CThostFtdcInputOrderField",
        "CThostFtdcQryInstrumentField",
        "CThostFtdcQryInvestorPositionField",
        "CThostFtdcQryOrderField",
        "CThostFtdcQryTradeField",
        "CThostFtdcQryTradingAccountField",
        "CThostFtdcReqAuthenticateField",
        "CThostFtdcReqUserLoginField",
        "CThostFtdcSettlementInfoConfirmField",
    ]:
        setattr(trader_mod, cls_name, _make_field_class(cls_name))

    # 父类 CThostFtdcTraderApi: MyTraderCallback 继承它
    class _StubTraderApi:
        def __init__(self, *a, **k):
            pass

    trader_mod.CThostFtdcTraderApi = _StubTraderApi

    # md 模块
    md_mod.CThostFtdcInputOrderActionField = _make_field_class(
        "CThostFtdcInputOrderActionField"
    )
    md_mod.THOST_FTDC_AF_Delete = "THOST_FTDC_AF_Delete"

    pkg.thosttraderapi = trader_mod
    pkg.thostmduserapi = md_mod
    sys.modules["openctp_ctp"] = pkg
    sys.modules["openctp_ctp.thosttraderapi"] = trader_mod
    sys.modules["openctp_ctp.thostmduserapi"] = md_mod


def _install_exchange_ctp_stub():
    """stub chanlun.exchange.exchange_ctp.MarketCTP, 避免其 import openctp 失败。"""
    mod_name = "chanlun.exchange.exchange_ctp"
    if mod_name in sys.modules:
        return
    m = types.ModuleType(mod_name)

    class MarketCTP:
        def __init__(self, *a, **k):
            self.broker_id = "9999"
            self.user_id = "test"
            self.app_id = ""
            self.auth_code = ""
            self.password = ""
            self.td_front = ""

    m.MarketCTP = MarketCTP
    sys.modules[mod_name] = m


@pytest.fixture(scope="session", autouse=True)
def _stub_broker_sdks():
    """整个 tests/trader 会话注入 stub。"""
    _install_openctp_stub()
    _install_exchange_ctp_stub()
    yield


# ---- 通用轻量替身 ----
class FakeTick:
    def __init__(self, last=None, buy1=None, sell1=None):
        self.last = last
        self.buy1 = buy1
        self.sell1 = sell1


class FakeOrder:
    """模拟 CThostFtdcOrderField 的最小子集 (OrderStatus / VolumeTraded / 其它)。"""

    def __init__(self, OrderStatus=None, VolumeTraded=0, StatusMsg="", InstrumentID=""):
        self.OrderStatus = OrderStatus
        self.VolumeTraded = VolumeTraded
        self.StatusMsg = StatusMsg
        self.InstrumentID = InstrumentID


class FakePosInfo:
    """模拟 CThostFtdcInvestorPositionField 的最小子集。"""

    def __init__(self, InstrumentID, PosiDirection, Position, OpenPrice=0.0):
        self.InstrumentID = InstrumentID
        self.PosiDirection = PosiDirection
        self.Position = Position
        self.OpenPrice = OpenPrice
