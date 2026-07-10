"""R5-H2-1: cl_alert_task 的 UniqueConstraint(market,task_name) 在 db_models/alert_task.py:36
被二次 __table_args__(mysql_collate dict)覆盖成死代码(Python 类命名空间同名后赋值胜出),
运行时表无任何唯一约束→/alert_save 无去重可静默插入重复 (market,task_name)→AlertTasks.run
对每行 add_job 生成不同 job→对同一 zx_group 重复监控+重复告警推送+重复 alert_records。
修复=alert_save 新建分支 query-first 去重(同名则更新已存在任务而非插入重复)。"""

from cl_app import alert_tasks as at_mod
from cl_app.alert_tasks import AlertTasks


class _T:
    def __init__(self, id, task_name):
        self.id = id
        self.task_name = task_name


def _mk_at(monkeypatch, existing, saved, updated):
    at = object.__new__(AlertTasks)  # 绕 __init__(需 scheduler)
    monkeypatch.setattr(at, "run", lambda: None)
    monkeypatch.setattr(at_mod.db, "task_query", lambda market=None, id=None: list(existing))
    monkeypatch.setattr(at_mod.db, "task_save", lambda **kw: saved.append(kw))
    monkeypatch.setattr(at_mod.db, "task_update", lambda **kw: updated.append(kw))
    return at


def test_new_task_same_name_updates_not_duplicates(monkeypatch):
    saved, updated = [], []
    at = _mk_at(monkeypatch, [_T(7, "任务X")], saved, updated)
    at.alert_save({"id": "", "market": "a", "task_name": "任务X"})
    # 同名已存在→更新(id=7), 不插入重复
    assert saved == []
    assert len(updated) == 1 and updated[0]["id"] == 7


def test_new_task_new_name_inserts(monkeypatch):
    saved, updated = [], []
    at = _mk_at(monkeypatch, [_T(7, "别的任务")], saved, updated)
    at.alert_save({"id": "", "market": "a", "task_name": "全新任务"})
    # 无同名→正常插入
    assert len(saved) == 1
    assert updated == []


def test_edit_existing_id_still_updates(monkeypatch):
    saved, updated = [], []
    at = _mk_at(monkeypatch, [], saved, updated)
    at.alert_save({"id": "3", "market": "a", "task_name": "任务X"})
    # 编辑分支(id 非空)不变: 走 update(id=3)
    assert saved == []
    assert len(updated) == 1 and updated[0]["id"] == 3