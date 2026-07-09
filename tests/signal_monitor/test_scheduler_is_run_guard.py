# -*- coding: utf-8 -*-
"""C8: signal_alert_run 每轮重读任务行却不检查 is_run → 手工 SQL 停用任务后 job
仍持续执行与推送直到 web 重启。修复:取到 task 后 is_run != 1 立即 return。
"""
from chanlun.signal_monitor import scheduler as sched_mod


class _Task:
    def __init__(self, is_run):
        self.id = 1
        self.is_run = is_run
        self.market = "a"
        self.task_name = "t"
        self.zx_group = "g"
        self.operation_level = "5m"
        self.level_ladder = "5m"
        self.signal_kinds = "bi_beichi"
        self.min_grade = "C"
        self.is_send_msg = 0


class _FakeEx:
    def now_trading(self):
        return True


class _FakeZiXuan:
    def __init__(self, market):
        pass

    def zx_stocks(self, group):
        return [{"code": "SH.600519", "name": "x"}]


def _wire(monkeypatch, task, spy):
    monkeypatch.setattr(sched_mod.repository, "signal_task_query",
                        lambda id=None: [task])
    monkeypatch.setattr(sched_mod, "get_exchange", lambda _m: _FakeEx())
    monkeypatch.setattr(sched_mod, "Market", lambda m: m)
    monkeypatch.setattr(sched_mod, "ZiXuan", _FakeZiXuan)
    monkeypatch.setattr(sched_mod, "query_cl_chart_config", lambda *a, **k: {})
    monkeypatch.setattr(sched_mod.sm_monitor, "monitoring_signal_code",
                        lambda *a, **k: spy.append(1))


def test_disabled_task_not_executed(monkeypatch):
    """is_run=0 → 不遍历自选组、不评估、不推送。"""
    spy = []
    _wire(monkeypatch, _Task(is_run=0), spy)
    assert sched_mod.signal_alert_run(1) is True
    assert spy == []          # monitoring_signal_code 从未被调用


def test_enabled_task_executed(monkeypatch):
    """is_run=1 → 正常执行(守卫不误伤)。"""
    spy = []
    _wire(monkeypatch, _Task(is_run=1), spy)
    assert sched_mod.signal_alert_run(1) is True
    assert spy == [1]         # 正常调用了一次