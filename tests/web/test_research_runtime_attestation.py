from __future__ import annotations

import importlib
from types import MappingProxyType, SimpleNamespace

import pytest


class _Scheduler:
    def __init__(self, jobs, *, running: bool = True):
        self._jobs = tuple(jobs)
        self.running = running
        self.get_jobs_calls = 0

    def get_jobs(self):
        self.get_jobs_calls += 1
        return list(self._jobs)


def _job(job_id: str, executor: str = "default"):
    return SimpleNamespace(id=job_id, executor=executor)


def _attestation_module():
    return importlib.import_module(
        "cl_app.services.research_runtime_attestation"
    )


def _a_jobs(module):
    return tuple(
        _job(job_id, executor)
        for job_id, executor in module.A_REQUIRED_JOB_EXECUTORS.items()
    )


def test_a_required_jobs_have_exact_default_executor_mapping() -> None:
    module = _attestation_module()
    expected = {
        "decision_support_bar_cycle": "default",
        "decision_support_paper_admission": "default",
        "decision_support_review": "default",
    }
    scheduler = _Scheduler(_a_jobs(module))

    attestation = module.build_scheduler_attestation(
        scheduler,
        module.A_REQUIRED_JOB_EXECUTORS,
    )

    assert type(module.A_REQUIRED_JOB_EXECUTORS) is MappingProxyType
    assert dict(module.A_REQUIRED_JOB_EXECUTORS) == expected
    assert scheduler.get_jobs_calls == 1
    assert attestation.running is True
    assert attestation.job_ids == tuple(sorted(expected))
    assert attestation.job_executor_aliases == tuple(sorted(expected.items()))
    assert attestation.required_job_executor_aliases == tuple(
        sorted(expected.items())
    )
    assert attestation.missing_required_job_ids == ()
    assert attestation.wrong_executor_aliases == ()
    assert attestation.forbidden_matches == ()
    assert attestation.safe is True


def test_attestation_reports_one_missing_required_job() -> None:
    module = _attestation_module()
    missing = "decision_support_review"
    scheduler = _Scheduler(
        job
        for job in _a_jobs(module)
        if job.id != missing
    )

    attestation = module.build_scheduler_attestation(
        scheduler,
        module.A_REQUIRED_JOB_EXECUTORS,
    )

    assert attestation.missing_required_job_ids == (missing,)
    assert attestation.safe is False


def test_attestation_reports_wrong_required_executor() -> None:
    module = _attestation_module()
    wrong_id = "decision_support_review"
    scheduler = _Scheduler(
        _job(job.id, "unsafe" if job.id == wrong_id else job.executor)
        for job in _a_jobs(module)
    )

    attestation = module.build_scheduler_attestation(
        scheduler,
        module.A_REQUIRED_JOB_EXECUTORS,
    )

    assert attestation.wrong_executor_aliases == (
        (wrong_id, "default", "unsafe"),
    )
    assert attestation.safe is False


@pytest.mark.parametrize(
    "forbidden_id",
    (
        "recursive_live_monitor_a",
        "recursive_live_monitor_us",
        "decision_support_order",
        "us_watchlist_research_broker_retry",
    ),
)
def test_attestation_reports_each_forbidden_regex(forbidden_id) -> None:
    module = _attestation_module()
    scheduler = _Scheduler((*_a_jobs(module), _job(forbidden_id)))

    attestation = module.build_scheduler_attestation(
        scheduler,
        module.A_REQUIRED_JOB_EXECUTORS,
    )

    assert attestation.forbidden_matches == (forbidden_id,)
    assert forbidden_id in attestation.job_ids
    assert attestation.safe is False


def test_unrelated_unknown_job_remains_visible_and_safe() -> None:
    module = _attestation_module()
    scheduler = _Scheduler((*_a_jobs(module), _job("signal_scan")))

    attestation = module.build_scheduler_attestation(
        scheduler,
        module.A_REQUIRED_JOB_EXECUTORS,
    )

    assert attestation.job_ids == tuple(
        sorted((*module.A_REQUIRED_JOB_EXECUTORS, "signal_scan"))
    )
    assert ("signal_scan", "default") in attestation.job_executor_aliases
    assert attestation.forbidden_matches == ()
    assert attestation.safe is True


def test_duplicate_scheduler_job_ids_are_rejected() -> None:
    module = _attestation_module()
    scheduler = _Scheduler(
        (*_a_jobs(module), _job("decision_support_review"))
    )

    with pytest.raises(ValueError, match="duplicate scheduler job id"):
        module.build_scheduler_attestation(
            scheduler,
            module.A_REQUIRED_JOB_EXECUTORS,
        )

    assert scheduler.get_jobs_calls == 1


def test_stopped_scheduler_is_not_safe() -> None:
    module = _attestation_module()
    scheduler = _Scheduler(_a_jobs(module), running=False)

    attestation = module.build_scheduler_attestation(
        scheduler,
        module.A_REQUIRED_JOB_EXECUTORS,
    )

    assert attestation.running is False
    assert attestation.safe is False


def test_web_installs_one_stable_attestation_callable_with_atomic_mapping() -> None:
    from cl_app import create_app

    app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "SCHEDULER_ENABLED": False,
            "DECISION_SUPPORT_ENABLED": False,
        }
    )
    scheduler = app.extensions["scheduler"]
    baseline = app.extensions["research_required_job_executors"]
    provider = app.extensions["research_scheduler_attestation"]

    assert type(baseline) is MappingProxyType
    assert dict(baseline) == {}
    assert callable(provider)
    scheduler.add_job(
        lambda: None,
        trigger="interval",
        seconds=3600,
        id="signal_scan",
        executor="default",
    )
    replacement = MappingProxyType({"signal_scan": "default"})
    app.extensions["research_required_job_executors"] = replacement

    attestation = provider()

    assert app.extensions["research_required_job_executors"] is replacement
    assert app.extensions["research_scheduler_attestation"] is provider
    assert attestation.required_job_executor_aliases == (
        ("signal_scan", "default"),
    )
    assert attestation.job_ids == ("signal_scan",)
