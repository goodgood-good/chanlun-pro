# -*- coding: utf-8 -*-
"""C6/C7: signal_monitor 去重与推送顺序。

C6: 记录先落库、推送后执行 → 单次推送失败该信号因永久去重(grade恒定不升级)静默漏发。
    修复后改为「推送成功(或纯记录任务)才落库」, 推送失败下一轮可重试。
C7: identity 去重键不含 task_name → 同市场多任务重叠标的时后跑任务被先跑任务吞掉。
    修复后去重按 (market, identity, task_name) 隔离。
"""
from __future__ import annotations

from chanlun.signal_monitor import monitor as monitor_mod
from chanlun.signal_monitor.evaluator import EvaluatorConfig


class _Rec:
    _seq = 0

    def __init__(self, market, identity, task_name, grade):
        _Rec._seq += 1
        self.id = _Rec._seq
        self.market = market
        self.identity = identity
        self.task_name = task_name
        self.grade = grade


class _FakeRepo:
    """内存 repo, query 按 (market, identity, task_name?) 过滤——镜像真实修复口径。"""

    def __init__(self):
        self.records = []

    def signal_record_query_by_identity(self, market, identity, task_name=None):
        hits = [
            r for r in self.records
            if r.market == market and r.identity == identity
            and (task_name is None or r.task_name == task_name)
        ]
        return hits[-1] if hits else None

    def signal_record_save(self, market, task_name, stock_code, stock_name,
                           operation_level, signal_kind, direction, identity,
                           grade, score, alert_msg, signal_dt=None):
        self.records.append(_Rec(market, identity, task_name, grade))
        return True

    def signal_record_update(self, record_id, grade, score, alert_msg,
                             signal_dt=None):
        for r in self.records:
            if r.id == record_id:
                r.grade = grade
        return True


class _Sig:
    def __init__(self, identity, grade="C"):
        self.identity = identity
        self.grade = grade
        self.score = 40
        self.msg = "test"
        self.k_date = None
        self.operation_level = "5m"
        self.signal_kind = "bi_beichi"
        self.direction = "bullish"


def _patch_eval(monkeypatch, sigs):
    class _FakeEval:
        def __init__(self, market, code, name):
            pass

        def evaluate(self, cds_by_level, config):
            return list(sigs)

    monkeypatch.setattr(monitor_mod, "ClSignalEvaluator", _FakeEval)


_CFG = EvaluatorConfig(operation_level="5m", level_ladder=["5m"])
_CDS = {"5m": object()}


def test_push_failure_does_not_persist_then_retries(monkeypatch):
    """推送失败 → 不落库 → 下一轮同信号重试推送(C6:不永久静默漏发)。"""
    _patch_eval(monkeypatch, [_Sig("id-1")])
    repo = _FakeRepo()
    sends = []

    def _fail_send(market, title, msgs):
        sends.append("fail")
        return False

    out = monitor_mod.monitoring_signal_code(
        "taskA", "a", "SH.600519", "x", _CFG, cds_by_level=_CDS,
        is_send_msg=True, repo=repo, send_fn=_fail_send,
    )
    assert len(out) == 1                 # 本轮识别为新信号
    assert repo.records == []            # 推送失败 → 未落库
    assert sends == ["fail"]             # 确实尝试过推送

    # 下一轮推送成功 → 信号未丢, 仍能推送并落库
    def _ok_send(market, title, msgs):
        sends.append("ok")
        return True

    out2 = monitor_mod.monitoring_signal_code(
        "taskA", "a", "SH.600519", "x", _CFG, cds_by_level=_CDS,
        is_send_msg=True, repo=repo, send_fn=_ok_send,
    )
    assert len(out2) == 1
    assert len(repo.records) == 1        # 成功后落库
    assert sends == ["fail", "ok"]


def test_record_only_task_always_persists(monkeypatch):
    """纯记录任务(is_send_msg=False)无推送 → 始终落库。"""
    _patch_eval(monkeypatch, [_Sig("id-2")])
    repo = _FakeRepo()
    out = monitor_mod.monitoring_signal_code(
        "taskA", "a", "SH.600519", "x", _CFG, cds_by_level=_CDS,
        is_send_msg=False, repo=repo, send_fn=lambda *a: True,
    )
    assert len(out) == 1
    assert len(repo.records) == 1


def test_push_success_persists(monkeypatch):
    _patch_eval(monkeypatch, [_Sig("id-3")])
    repo = _FakeRepo()
    out = monitor_mod.monitoring_signal_code(
        "taskA", "a", "SH.600519", "x", _CFG, cds_by_level=_CDS,
        is_send_msg=True, repo=repo, send_fn=lambda *a: True,
    )
    assert len(out) == 1
    assert len(repo.records) == 1


def test_task_name_isolation(monkeypatch):
    """两任务同 identity, 后跑任务 B 不被先跑任务 A 的记录吞掉(C7)。"""
    repo = _FakeRepo()
    # 任务 A 先跑(纯记录), 落一条 identity=shared 的记录
    _patch_eval(monkeypatch, [_Sig("shared")])
    outA = monitor_mod.monitoring_signal_code(
        "taskA", "a", "SH.600519", "x", _CFG, cds_by_level=_CDS,
        is_send_msg=False, repo=repo, send_fn=lambda *a: True,
    )
    assert len(outA) == 1
    assert len(repo.records) == 1

    # 任务 B 后跑同标的同 identity, 要推送 → 不应被 A 的记录去重吞掉
    sends = []
    _patch_eval(monkeypatch, [_Sig("shared")])
    outB = monitor_mod.monitoring_signal_code(
        "taskB", "a", "SH.600519", "x", _CFG, cds_by_level=_CDS,
        is_send_msg=True, repo=repo,
        send_fn=lambda m, t, msgs: sends.append("B") or True,
    )
    assert len(outB) == 1                 # B 未被吞
    assert sends == ["B"]                 # B 确实推送
    task_names = sorted(r.task_name for r in repo.records)
    assert task_names == ["taskA", "taskB"]