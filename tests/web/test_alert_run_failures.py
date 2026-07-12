import sys
from types import ModuleType, SimpleNamespace

import pytest

import chanlun
from cl_app import alert_tasks
from cl_app.alert_tasks import AlertTasks


class _RecordingLog:
    def __init__(self):
        self.infos = []
        self.errors = []
        self.warnings = []

    def info(self, message):
        self.infos.append(message)

    def error(self, message):
        self.errors.append(message)

    def warning(self, message):
        self.warnings.append(message)


def _config():
    return SimpleNamespace(
        market="a",
        task_name="test-alert",
        zx_group="group",
        frequency="1m",
        check_bi_type="",
        check_bi_beichi="",
        check_bi_mmd="",
        check_xd_type="",
        check_xd_beichi="",
        check_xd_mmd="",
        check_idx_ma_info="",
        check_idx_macd_info="",
        is_send_msg=0,
    )


def _task(monkeypatch, stocks, monitoring_code):
    monitor_module = ModuleType("chanlun.monitor")
    monitor_module.monitoring_code = monitoring_code
    monkeypatch.setitem(sys.modules, "chanlun.monitor", monitor_module)
    monkeypatch.setattr(chanlun, "monitor", monitor_module, raising=False)
    monkeypatch.setattr(alert_tasks, "get_exchange", lambda _market: object())
    monkeypatch.setattr(alert_tasks, "market_now_trading", lambda *_args: True)
    monkeypatch.setattr(
        alert_tasks,
        "ZiXuan",
        lambda _market: SimpleNamespace(zx_stocks=lambda _group: stocks),
    )
    monkeypatch.setattr(alert_tasks, "query_cl_chart_config", lambda *_args: {})
    monkeypatch.setattr(alert_tasks, "tqdm", lambda values: values)

    task = object.__new__(AlertTasks)
    task.log = _RecordingLog()
    task.alert_get = lambda _alert_id: _config()
    return task


def test_all_stock_failures_raise_summary_after_processing_every_stock(monkeypatch):
    calls = []

    def fail_monitor(_task, _market, code, *_args, **_kwargs):
        calls.append(code)
        raise RuntimeError(f"failed {code}")

    task = _task(
        monkeypatch,
        [{"code": "A", "name": "a"}, {"code": "B", "name": "b"}],
        fail_monitor,
    )

    with pytest.raises(RuntimeError, match="2/2"):
        task.alert_run(1)

    assert calls == ["A", "B"]
    assert len(task.log.errors) == 2


def test_partial_stock_failure_continues_and_logs_summary_warning(monkeypatch):
    calls = []

    def monitor(_task, _market, code, *_args, **_kwargs):
        calls.append(code)
        if code == "B":
            raise RuntimeError("failed B")

    task = _task(
        monkeypatch,
        [
            {"code": "A", "name": "a"},
            {"code": "B", "name": "b"},
            {"code": "C", "name": "c"},
        ],
        monitor,
    )

    assert task.alert_run(1) is True
    assert calls == ["A", "B", "C"]
    assert len(task.log.warnings) == 1
    assert "1/3" in task.log.warnings[0]


def test_missing_stock_code_is_logged_without_masking_remaining_stocks(monkeypatch):
    calls = []

    def monitor(_task, _market, code, *_args, **_kwargs):
        calls.append(code)

    task = _task(
        monkeypatch,
        [{"name": "missing-code"}, {"code": "OK", "name": "ok"}],
        monitor,
    )

    assert task.alert_run(1) is True
    assert calls == ["OK"]
    assert any("<missing-code>" in message for message in task.log.errors)
    assert len(task.log.warnings) == 1
    assert "1/2" in task.log.warnings[0]
