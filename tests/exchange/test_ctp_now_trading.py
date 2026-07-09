# -*- coding: utf-8 -*-
"""C17: MarketCTP.now_trading() 曾用 datetime.datetime.now() 但文件是
`from datetime import datetime` 形态, 一调必抛 AttributeError, CTP 实盘主循环
(reboot_trader_ctp.py 第一步 if not market.now_trading()) 永久瘫痪。

本测试以独立模块名加载真实 exchange_ctp(仅 stub openctp_ctp), 用 object.__new__
绕过需要连接配置的 __init__, 直接调 now_trading——修复前抛 AttributeError(红),
修复后返回 bool(绿)。与 tests/trader 的 exchange_ctp stub 隔离(独立模块名)。
"""
import importlib.util
import pathlib
import sys
import types


def _load_real_ctp():
    if "openctp_ctp" not in sys.modules:
        pkg = types.ModuleType("openctp_ctp")
        md = types.ModuleType("openctp_ctp.thostmduserapi")
        for n in [
            "CThostFtdcDepthMarketDataField", "CThostFtdcMdApi",
            "CThostFtdcReqAuthenticateField", "CThostFtdcReqUserLoginField",
            "CThostFtdcRspInfoField", "CThostFtdcRspUserLoginField",
        ]:
            setattr(md, n, type(n, (), {"__init__": lambda self, *a, **k: None}))
        pkg.thostmduserapi = md
        sys.modules["openctp_ctp"] = pkg
        sys.modules["openctp_ctp.thostmduserapi"] = md
    src = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src" / "chanlun" / "exchange" / "exchange_ctp.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_real_exchange_ctp_for_test", src
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeNow:
    def __init__(self, y, mo, d, h, mi):
        import datetime as _dt
        self._d = _dt.datetime(y, mo, d, h, mi)

    def weekday(self):
        return self._d.weekday()

    @property
    def hour(self):
        return self._d.hour

    @property
    def minute(self):
        return self._d.minute


def _fake_datetime_class(y, mo, d, h, mi):
    class _Fake:
        @staticmethod
        def now(tz=None):
            return _FakeNow(y, mo, d, h, mi)
    return _Fake


def test_now_trading_no_attributeerror():
    # now_trading 不访问 self, 用 dummy self 直调未绑定方法, 绕过抽象类实例化
    mod = _load_real_ctp()
    r = mod.MarketCTP.now_trading(object())
    assert isinstance(r, bool)


def test_now_trading_weekend_false(monkeypatch):
    """周六(weekday=5)一律不交易。"""
    mod = _load_real_ctp()
    monkeypatch.setattr(mod, "datetime", _fake_datetime_class(2026, 7, 11, 10, 0))
    assert mod.MarketCTP.now_trading(object()) is False


def test_now_trading_day_session_true(monkeypatch):
    """周五 10:00 日盘交易时段应为 True。"""
    mod = _load_real_ctp()
    monkeypatch.setattr(mod, "datetime", _fake_datetime_class(2026, 7, 10, 10, 0))
    assert mod.MarketCTP.now_trading(object()) is True