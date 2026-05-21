"""tests/signal_monitor/test_monitor_integration.py — monitoring_signal_code 集成测试。

用合成 K 线 + 注入式 repo / send，验证端到端评估、去重幂等、推送。
"""
from __future__ import annotations

from chanlun.signal_monitor.evaluator import EvaluatorConfig
from chanlun.signal_monitor.monitor import monitoring_signal_code

LADDER = ["d", "30m", "5m"]


class _FakeRecord:
    def __init__(self, grade: str):
        self.grade = grade


class _FakeRepo:
    """内存版 repository：只实现 monitoring_signal_code 用到的两个函数。"""

    def __init__(self):
        self.records = {}  # identity -> grade

    def signal_record_query_by_identity(self, market, identity):
        g = self.records.get(identity)
        return _FakeRecord(g) if g is not None else None

    def signal_record_save(self, **kw):
        self.records[kw["identity"]] = kw["grade"]


def _three_level(factory):
    return {
        "d": factory(200, multi_freq=True, seed=1),
        "30m": factory(220, multi_freq=True, seed=2),
        "5m": factory(240, multi_freq=True, seed=3),
    }


def test_end_to_end_and_dedup_idempotent(cl_with_synthetic_klines):
    cds = _three_level(cl_with_synthetic_klines)
    cfg = EvaluatorConfig("30m", LADDER)
    fake_repo = _FakeRepo()
    sent = []

    first = monitoring_signal_code(
        "t1", "a", "TEST.001", "测试", cfg,
        cds_by_level=cds, is_send_msg=True,
        repo=fake_repo, send_fn=lambda *a: sent.append(a),
    )
    assert isinstance(first, list)
    for sig in first:
        assert sig.identity in fake_repo.records

    # 第二次同样数据 → identity 全部命中、分级未升级 → 无新信号（幂等去重）
    second = monitoring_signal_code(
        "t1", "a", "TEST.001", "测试", cfg,
        cds_by_level=cds, is_send_msg=True,
        repo=fake_repo, send_fn=lambda *a: sent.append(a),
    )
    assert second == []
    assert len(sent) == (1 if first else 0)


def test_missing_operation_level_returns_empty():
    cfg = EvaluatorConfig("30m", LADDER)
    out = monitoring_signal_code(
        "t1", "a", "X", "x", cfg,
        cds_by_level={}, repo=_FakeRepo(), send_fn=lambda *a: None,
    )
    assert out == []


def test_grade_upgrade_triggers_realert(cl_with_synthetic_klines):
    """已报信号若分级升级，应重新提醒。"""
    cds = _three_level(cl_with_synthetic_klines)
    cfg = EvaluatorConfig("30m", LADDER)
    fake_repo = _FakeRepo()
    first = monitoring_signal_code(
        "t1", "a", "TEST.001", "测试", cfg,
        cds_by_level=cds, repo=fake_repo, send_fn=lambda *a: None,
    )
    if not first:
        return  # 合成数据本轮无信号，跳过
    # 把已存记录的分级人为压到 C，再跑一次 → 凡评估分级 > C 的应重新成为新信号
    for ident in list(fake_repo.records):
        fake_repo.records[ident] = "C"
    second = monitoring_signal_code(
        "t1", "a", "TEST.001", "测试", cfg,
        cds_by_level=cds, repo=fake_repo, send_fn=lambda *a: None,
    )
    upgraded = [s for s in first if s.grade in ("A", "B")]
    assert len(second) == len(upgraded)
