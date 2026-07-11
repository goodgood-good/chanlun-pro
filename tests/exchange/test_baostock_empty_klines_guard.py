# -*- coding: utf-8 -*-
"""R17: ExchangeBaostock.klines() 两个可达确定性缺陷(EXCHANGE_A="baostock" 六选一分支)。

BUG-A(更严重): 模块无 import datetime, 但 :166 datetime.datetime.now()(start_date=None 默认
路径, web/同步脚本默认调用)+ :216 append_time 均 NameError → klines 几乎全崩。修复=加 import datetime。

BUG-B: data_list=[] 时 kline 是列齐全的空 df; 分钟分支(:207)dates 为空 → new_kline=pd.DataFrame()
零列 → :240 sort_values("date") KeyError。日线分支不走 :207 故本就无此问题。修复=分钟重建
前置 len(kline)>0, 空结果落到 :242 既有空返回路径(与日线空结果同一路径)。镜像 tdx_hk R4-C。

可达: EXCHANGE_A="baostock"(exchange/__init__ 六选一正式分支, 同 tdx d0224790 防御纵深先例);
A股批量扫描/web 图表遇无效/退市/新股 IPO 前/该周期无覆盖代码返回 0 行即触发。neither(纯健壮性)。
"""
import sys
import types

import pytz

# baostock 属可选 cn-extra, 测试环境未装 → sys.modules 注入桩(须在 import exchange_baostock 前)
if "baostock" not in sys.modules:
    _bs_stub = types.ModuleType("baostock")
    _bs_stub.login = lambda *a, **k: None
    _bs_stub.query_history_k_data_plus = lambda *a, **k: None
    sys.modules["baostock"] = _bs_stub

import chanlun.exchange.exchange_baostock as bstock_mod  # noqa: E402
from chanlun.exchange.exchange_baostock import ExchangeBaostock  # noqa: E402


class _FakeRsEmpty:
    """模拟 baostock 空结果集: error_code=0, next() 恒 False(无行), 字段齐全。"""

    error_code = "0"
    error_msg = "success"
    fields = ["code", "date", "open", "low", "high", "close", "volume"]

    def next(self):
        return False

    def get_row_data(self):
        return []


def _make():
    Cls = getattr(ExchangeBaostock, "__wrapped__", ExchangeBaostock)  # 解 singleton
    obj = object.__new__(Cls)  # 绕 __init__(bs.login())
    obj.tz = pytz.timezone("Asia/Shanghai")
    return obj


def test_klines_empty_minute_result_no_keyerror(monkeypatch):
    """分钟频率空结果 → 干净返回空 df(列齐全), 不得 KeyError('date')。"""
    obj = _make()
    monkeypatch.setattr(
        bstock_mod.bs, "query_history_k_data_plus", lambda *a, **k: _FakeRsEmpty()
    )
    df = obj.klines("sh.600000", "5m")  # 修复前 KeyError('date'); 修复后 空 df
    assert df is not None
    assert len(df) == 0
    for col in ["code", "date", "open", "close", "high", "low", "volume"]:
        assert col in df.columns


def test_klines_empty_daily_result_regression(monkeypatch):
    """回归: 日线空结果本就走 :242 空路径干净返回, 证明分钟修复与日线同路径且不误伤。"""
    obj = _make()
    monkeypatch.setattr(
        bstock_mod.bs, "query_history_k_data_plus", lambda *a, **k: _FakeRsEmpty()
    )
    df = obj.klines("sh.600000", "d")
    assert df is not None
    assert len(df) == 0
