import json
from types import SimpleNamespace

import pytest

from cl_app.alert_tasks import AlertTasks
from cl_app import create_app


class _Scheduler:
    def __init__(self):
        self.running = True
        self.added = []
        self.removed = []

    def add_job(self, **kwargs):
        self.added.append(kwargs)
        if kwargs["id"] == "2":
            raise RuntimeError("cannot add second job")
        return SimpleNamespace(id=kwargs["id"])

    def remove_job(self, job_id):
        self.removed.append(job_id)


def _task(task_id):
    return SimpleNamespace(
        id=task_id,
        is_run=1,
        interval_minutes=5,
        task_name=f"task-{task_id}",
    )


def test_reconcile_failure_does_not_delete_existing_jobs(monkeypatch):
    scheduler = _Scheduler()
    tasks = AlertTasks(scheduler)
    tasks.task_ids = ["1"]
    monkeypatch.setattr(tasks, "task_list", lambda: [_task(1), _task(2)])

    with pytest.raises(RuntimeError, match="cannot add second job"):
        tasks.run()

    assert scheduler.removed == []
    assert tasks.task_ids == ["1"]


def test_reconcile_uses_replace_existing_for_idempotent_updates(monkeypatch):
    scheduler = _Scheduler()
    tasks = AlertTasks(scheduler)
    monkeypatch.setattr(tasks, "task_list", lambda: [_task(1)])

    assert tasks.run() is True

    assert scheduler.added[0]["replace_existing"] is True
    assert tasks.task_ids == ["1"]


def test_alert_endpoint_reports_stopped_scheduler():
    class _StoppedTasks:
        def alert_del(self, _alert_id):
            raise RuntimeError("scheduler is not running")

    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    app.extensions["alert_tasks"] = _StoppedTasks()

    response = app.test_client().post("/alert_del/1")

    assert response.status_code == 503
    assert response.get_json() == {
        "ok": False,
        "msg": "任务调度器未运行，请使用正式启动入口。",
    }


def test_alert_save_rejects_invalid_integer_fields_with_json_400():
    saved = []

    class _Tasks:
        def alert_save(self, config):
            saved.append(config)

    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    app.extensions["alert_tasks"] = _Tasks()
    response = app.test_client().post(
        "/alert_save",
        data={
            "id": "",
            "market": "a",
            "task_name": "test",
            "interval_minutes": "5",
            "zx_group": "我的关注",
            "frequency": "5m",
            "check_bi_type": "up,down",
            "check_bi_beichi": "",
            "check_bi_mmd": "",
            "check_xd_type": "up,down",
            "check_xd_beichi": "",
            "check_xd_mmd": "",
            "check_idx_ma_info_enable": "bad",
            "check_idx_ma_info_slow": "5",
            "check_idx_ma_info_fast": "10",
            "check_idx_ma_info_cross_up": "1",
            "check_idx_ma_info_cross_down": "0",
            "check_idx_macd_info_enable": "1",
            "check_idx_macd_info_cross_up": "1",
            "check_idx_macd_info_cross_down": "0",
            "is_send_msg": "1",
            "is_run": "1",
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "code": "invalid_request",
        "msg": "check_idx_ma_info_enable must be 0 or 1",
    }
    assert saved == []


def test_alert_save_accepts_unchecked_optional_flags():
    saved = []

    class _Tasks:
        def alert_save(self, config):
            saved.append(config)

    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    app.extensions["alert_tasks"] = _Tasks()

    response = app.test_client().post(
        "/alert_save",
        data={
            "id": "",
            "market": "a",
            "task_name": "test",
            "interval_minutes": "5",
            "zx_group": "watchlist",
            "frequency": "5m",
            "check_idx_ma_info_slow": "10",
            "check_idx_ma_info_fast": "5",
            "is_send_msg": "0",
            "is_run": "1",
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert len(saved) == 1
    assert json.loads(saved[0]["check_idx_ma_info"]) == {
        "enable": 0,
        "slow": 10,
        "fast": 5,
        "cross_up": 0,
        "cross_down": 0,
    }
    assert json.loads(saved[0]["check_idx_macd_info"]) == {
        "enable": 0,
        "cross_up": 0,
        "cross_down": 0,
    }
