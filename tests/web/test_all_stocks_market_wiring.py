"""全市场选股和自选导入统一使用市场绑定的无参股票列表契约。"""
import pathlib
import types

from cl_app import xuangu_tasks


class _FakeEx:
    def __init__(self):
        self.called = False

    def all_stocks(self):
        self.called = True
        return [{"code": "HK.00700", "name": "TX"}]

    @staticmethod
    def support_frequencys():
        return {"5m": "5分钟"}


class _FakeZx:
    def __init__(self, market=None):
        pass

    def zx_stocks(self, group):
        return []

    def replace_zx_stocks(self, group, stocks):
        return True


def test_process_xuangu_task_uses_bound_market_stock_list(monkeypatch):
    fake_ex = _FakeEx()
    monkeypatch.setattr(xuangu_tasks, "get_exchange", lambda m: fake_ex)
    monkeypatch.setattr(xuangu_tasks, "zixuan", types.SimpleNamespace(ZiXuan=_FakeZx))
    monkeypatch.setattr(
        xuangu_tasks, "utils", types.SimpleNamespace(send_fs_msg=lambda *a, **k: None)
    )
    monkeypatch.setattr(xuangu_tasks, "process_xuangu_by_code", lambda args: None)
    xuangu_tasks.process_xuangu_task(
        "hk", "strict_class1_point", ["5m"], ["long"], "all", "xg-target"
    )
    assert fake_ex.called is True


def test_zixuan_import_endpoint_uses_safe_all_stocks_wiring():
    # Flask 上传上下文离线难构造, wiring 用源码扫描钉死
    # (同 tests/trader/test_open_dedup.py 先例)。
    src = pathlib.Path(
        "web/chanlun_chart/cl_app/blueprints/zixuan.py"
    ).read_text(encoding="utf-8")
    assert "_safe_all_stocks(ex)" in src
