"""普通选股与自选导入都不得隐式展开全市场。"""
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


def test_process_xuangu_task_uses_only_explicit_codes(monkeypatch):
    fake_ex = _FakeEx()
    processed = []
    monkeypatch.setattr(xuangu_tasks, "get_exchange", lambda m: fake_ex)
    monkeypatch.setattr(xuangu_tasks, "zixuan", types.SimpleNamespace(ZiXuan=_FakeZx))
    monkeypatch.setattr(
        xuangu_tasks, "utils", types.SimpleNamespace(send_fs_msg=lambda *a, **k: None)
    )
    monkeypatch.setattr(
        xuangu_tasks,
        "process_xuangu_by_code",
        lambda args: processed.append(args[0]),
    )
    xuangu_tasks.process_xuangu_task(
        "hk",
        "strict_class1_point",
        ["5m"],
        ["long"],
        ["HK.00700"],
        "xg-target",
    )
    assert fake_ex.called is False
    assert processed == ["HK.00700"]


def test_zixuan_import_endpoint_uses_bounded_identity_wiring():
    src = pathlib.Path(
        "web/chanlun_chart/cl_app/blueprints/zixuan.py"
    ).read_text(encoding="utf-8")
    assert "_safe_all_stocks" not in src
    assert "_MAX_BOUNDED_IMPORT_SYMBOLS = 20" in src
    assert "admit_explicit_validation_codes(" in src
    assert "resolve_bounded_stock_info(" in src
    assert "ex.stock_info(code)" not in src
