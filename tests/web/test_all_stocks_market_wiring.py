"""R1-C6: 全市场选股/自选导入的 all_stocks 必须显式传 market。

ExchangeChangQiao 是 @fun.singleton, A/HK/US 三市场共享同一实例, default_market
被后初始化市场覆盖 → 无参 all_stocks() 恒拿最后初始化市场的股票列表:
- xuangu_tasks: hk/us 全市场选股拿错市场列表, 跑完清空目标自选组写入错市场结果
- blueprints/zixuan 导入: 用错市场代码全集校验导入代码
services/stock_list._safe_all_stocks 是历史同款 bug 的统一修复入口(注释自证
实测复现), 本测试钉死两个漏网调用点的接线。
"""
import pathlib
import types

from cl_app import xuangu_tasks


class _FakeEx:
    def __init__(self):
        self.received = "NOT-CALLED"

    def all_stocks(self, market=None):
        self.received = market
        return [{"code": "HK.00700", "name": "TX"}]


class _FakeZx:
    def __init__(self, market=None):
        pass

    def clear_zx_stocks(self, group):
        pass

    def add_stock(self, group, code, color):
        pass

    def zx_stocks(self, group):
        return []


def test_process_xuangu_task_passes_market_to_all_stocks(monkeypatch):
    fake_ex = _FakeEx()
    monkeypatch.setattr(xuangu_tasks, "get_exchange", lambda m: fake_ex)
    monkeypatch.setattr(xuangu_tasks, "zixuan", types.SimpleNamespace(ZiXuan=_FakeZx))
    monkeypatch.setattr(
        xuangu_tasks, "utils", types.SimpleNamespace(send_fs_msg=lambda *a, **k: None)
    )
    monkeypatch.setattr(xuangu_tasks, "process_xuangu_by_code", lambda args: None)
    xuangu_tasks.process_xuangu_task(
        "hk", "xg_single_bi_1mmd", ["5m"], ["long"], "all", "xg-target"
    )
    assert fake_ex.received == "hk"  # 旧实现无参调用 → None(落到 default_market)


def test_zixuan_import_endpoint_uses_safe_all_stocks_wiring():
    # Flask 上传上下文离线难构造, wiring 用源码扫描钉死
    # (同 tests/trader/test_open_dedup.py 先例)。
    src = pathlib.Path(
        "web/chanlun_chart/cl_app/blueprints/zixuan.py"
    ).read_text(encoding="utf-8")
    assert "_safe_all_stocks(ex, market)" in src
    assert "ex.all_stocks()" not in src