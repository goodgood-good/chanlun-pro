import pytest
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED, JobEvent

from cl_app import create_app


TERMINAL_STATES = {"已完成", "执行异常", "未执行", "删除作业"}


def _create_test_app():
    return create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )


def test_jobs_snapshot_survives_listener_mutation_during_iteration():
    app = _create_test_app()
    scheduler = app.extensions["scheduler"]
    try:
        scheduler._dispatch_event(JobEvent(EVENT_JOB_EXECUTED, "existing", "default"))

        class ListenerMutatingValuesDict(dict):
            def values(self):
                iterator = iter(super().values())

                def mutate_during_iteration():
                    try:
                        first = next(iterator)
                    except StopIteration:
                        return
                    yield first
                    scheduler._dispatch_event(
                        JobEvent(EVENT_JOB_EXECUTED, "added-during-jobs-render", "default")
                    )
                    yield from iterator

                return mutate_during_iteration()

        scheduler.my_task_list = ListenerMutatingValuesDict(
            scheduler.my_task_list
        )
        response = app.test_client().get("/jobs")

        assert response.status_code == 200
        assert "后台调度状态" in response.get_data(as_text=True)
    except RuntimeError as exc:
        pytest.fail(f"/jobs iterated a mutating task registry: {exc}")
    finally:
        app.extensions["shutdown_scheduler"]()




def test_listener_bounds_terminal_history_without_dropping_active_tasks():
    app = _create_test_app()
    scheduler = app.extensions["scheduler"]
    try:
        active_ids = {f"active-{index}" for index in range(3)}
        for task_id in active_ids:
            scheduler._dispatch_event(JobEvent(EVENT_JOB_SUBMITTED, task_id, "default"))
        for index in range(600):
            scheduler._dispatch_event(
                JobEvent(EVENT_JOB_EXECUTED, f"terminal-{index}", "default")
            )

        registry = app.extensions.get("task_registry")
        jobs = (
            registry.snapshot()
            if registry is not None
            else [dict(task) for task in scheduler.my_task_list.values()]
        )
        terminal_jobs = [job for job in jobs if job["state"] in TERMINAL_STATES]
        job_ids = {job["id"] for job in jobs}

        assert len(terminal_jobs) == 500
        assert active_ids <= job_ids
        assert len(jobs) == 503
    finally:
        app.extensions["shutdown_scheduler"]()
