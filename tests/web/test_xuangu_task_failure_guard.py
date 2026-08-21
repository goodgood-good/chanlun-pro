"""选股任务只有在代码评估正常完成时才可替换目标自选组。"""

import types

import pytest

from cl_app import xuangu_tasks


class _FakeZiXuan:
    def __init__(self):
        self.zx_names = ["src", "dst"]
        self.replaced = []

    def zx_stocks(self, group):
        if group == "src":
            return [{"code": "SH.600000"}, {"code": "SZ.000001"}]
        return []

    def replace_zx_stocks(self, group, stocks):
        self.replaced.append((group, list(stocks)))
        return True


def _patch_task_dependencies(monkeypatch, fake_zx):
    monkeypatch.setattr(
        xuangu_tasks,
        "get_exchange",
        lambda _market: types.SimpleNamespace(
            support_frequencys=lambda: {"5m": "5分钟"}
        ),
    )
    monkeypatch.setattr(
        xuangu_tasks,
        "zixuan",
        types.SimpleNamespace(ZiXuan=lambda _market: fake_zx),
    )
    monkeypatch.setattr(
        xuangu_tasks,
        "utils",
        types.SimpleNamespace(send_fs_msg=lambda *_args, **_kwargs: None),
    )


def _run(src_group="src"):
    return xuangu_tasks.process_xuangu_task(
        "a", "strict_class1_point", ["5m"], ["long"], src_group, "dst"
    )


def test_all_code_errors_do_not_replace_target_group(monkeypatch):
    fake_zx = _FakeZiXuan()
    _patch_task_dependencies(monkeypatch, fake_zx)
    failed = {"code": "__failed__", "msg": "evaluation failed"}
    monkeypatch.setattr(
        xuangu_tasks, "_CODE_EVALUATION_FAILED", failed, raising=False
    )
    monkeypatch.setattr(
        xuangu_tasks, "process_xuangu_by_code", lambda _args: failed
    )

    assert _run() is False
    assert fake_zx.replaced == []


def test_partial_code_errors_do_not_replace_target_group(monkeypatch):
    fake_zx = _FakeZiXuan()
    _patch_task_dependencies(monkeypatch, fake_zx)
    failed = object()
    monkeypatch.setattr(
        xuangu_tasks, "_CODE_EVALUATION_FAILED", failed, raising=False
    )
    results = iter([{"code": "SH.600000", "msg": "matched"}, failed])
    monkeypatch.setattr(
        xuangu_tasks, "process_xuangu_by_code", lambda _args: next(results)
    )

    assert _run() is False
    assert fake_zx.replaced == []


def test_legitimate_zero_matches_publish_empty_snapshot(monkeypatch):
    fake_zx = _FakeZiXuan()
    _patch_task_dependencies(monkeypatch, fake_zx)
    monkeypatch.setattr(
        xuangu_tasks, "process_xuangu_by_code", lambda _args: None
    )

    assert _run() is True
    assert fake_zx.replaced == [("dst", [])]


def test_successful_evaluation_publishes_with_one_atomic_replace(monkeypatch):
    fake_zx = _FakeZiXuan()
    _patch_task_dependencies(monkeypatch, fake_zx)
    results = iter([{"code": "SH.600000", "msg": "matched"}, None])
    monkeypatch.setattr(
        xuangu_tasks, "process_xuangu_by_code", lambda _args: next(results)
    )

    assert _run() is True
    assert fake_zx.replaced == [
        ("dst", [{"code": "SH.600000", "msg": "matched"}])
    ]


def test_unknown_source_group_does_not_replace_target_group(monkeypatch):
    fake_zx = _FakeZiXuan()
    _patch_task_dependencies(monkeypatch, fake_zx)

    assert _run(src_group="missing") is False
    assert fake_zx.replaced == []


def test_task_level_exception_reports_failure(monkeypatch):
    fake_zx = _FakeZiXuan()
    _patch_task_dependencies(monkeypatch, fake_zx)
    monkeypatch.setattr(
        fake_zx,
        "zx_stocks",
        lambda _group: (_ for _ in ()).throw(RuntimeError("database offline")),
    )

    assert _run() is False
    assert fake_zx.replaced == []


def test_per_code_exception_has_distinct_failure_result(monkeypatch):
    failed = object()
    monkeypatch.setattr(
        xuangu_tasks, "_CODE_EVALUATION_FAILED", failed, raising=False
    )
    monkeypatch.setattr(
        xuangu_tasks,
        "get_exchange",
        lambda _market: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    result = xuangu_tasks.process_xuangu_by_code(
        ("SH.600000", "a", ["5m"], "strict_class1_point", ["long"])
    )
    assert result is failed


def test_scheduled_job_raises_when_task_reports_failure(monkeypatch):
    monkeypatch.setattr(xuangu_tasks, "process_xuangu_task", lambda *_args: False)

    with pytest.raises(RuntimeError, match="xuangu task failed"):
        xuangu_tasks.process_xuangu_job("a", "task", [], [], "src", "dst")


def test_scheduled_job_returns_true_when_task_succeeds(monkeypatch):
    monkeypatch.setattr(xuangu_tasks, "process_xuangu_task", lambda *_args: True)

    assert xuangu_tasks.process_xuangu_job("a", "task", [], [], "src", "dst") is True
