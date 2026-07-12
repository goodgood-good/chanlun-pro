import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_SUBMITTED, JobEvent
from apscheduler.jobstores.base import ConflictingIdError

from cl_app import create_app
from cl_app.xuangu_tasks import XuanguTasks, xuangu_task_configs


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
    except RuntimeError as exc:
        pytest.fail(f"/jobs iterated a mutating task registry: {exc}")
    finally:
        app.extensions["shutdown_scheduler"]()


class _CollisionScheduler:
    def __init__(self):
        self.my_task_list = {}
        self.my_task_lock = threading.RLock()
        self.running = True
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self._job_ids = set()
        self.add_calls = 0

    def add_job(self, **kwargs):
        with self._lock:
            self.add_calls += 1
        try:
            self._barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass
        with self._lock:
            job_id = kwargs["id"]
            if job_id in self._job_ids:
                raise ConflictingIdError(job_id)
            self._job_ids.add(job_id)


def test_run_xuangu_atomically_rejects_duplicate_concurrent_submission():
    scheduler = _CollisionScheduler()
    tasks = XuanguTasks(scheduler)
    task_key = next(iter(xuangu_task_configs))
    args = ("a", task_key, ["d"], ["long"], "all", "target")

    results = []
    errors = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(tasks.run_xuangu, *args) for _ in range(2)]
        for future in futures:
            try:
                results.append(future.result(timeout=3))
            except Exception as exc:
                errors.append(exc)

    assert errors == []
    assert sorted(results) == [False, True]
    assert scheduler.add_calls == 1


def test_run_xuangu_rejects_submission_when_scheduler_is_stopped():
    scheduler = _CollisionScheduler()
    scheduler.running = False
    tasks = XuanguTasks(scheduler)
    task_key = next(iter(xuangu_task_configs))

    with pytest.raises(RuntimeError, match="scheduler is not running"):
        tasks.run_xuangu("a", task_key, ["d"], ["long"], "all", "target")

    assert scheduler.add_calls == 0


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
