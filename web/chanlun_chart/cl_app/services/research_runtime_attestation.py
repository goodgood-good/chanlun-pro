from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from apscheduler.schedulers.base import BaseScheduler


A_REQUIRED_JOB_EXECUTORS = MappingProxyType(
    {
        "decision_support_bar_cycle": "default",
        "decision_support_paper_admission": "default",
        "decision_support_review": "default",
    }
)
FORBIDDEN_RESEARCH_JOB_PATTERNS = (
    re.compile(r"^recursive_live_monitor_(?:a|us)$"),
    re.compile(
        r"^(?:decision_support|us_watchlist_research)_"
        r"(?:.*_)?(?:order|orders|broker|execution|trade)(?:_.*)?$"
    ),
)


@dataclass(frozen=True, slots=True)
class SchedulerAttestation:
    running: bool
    job_ids: tuple[str, ...]
    job_executor_aliases: tuple[tuple[str, str], ...]
    required_job_executor_aliases: tuple[tuple[str, str], ...]
    missing_required_job_ids: tuple[str, ...]
    wrong_executor_aliases: tuple[tuple[str, str, str], ...]
    forbidden_matches: tuple[str, ...]
    safe: bool


def build_scheduler_attestation(
    scheduler: BaseScheduler,
    required_job_executors: Mapping[str, str],
) -> SchedulerAttestation:
    """Snapshot all scheduler jobs once; never filter away unknown jobs."""

    jobs = tuple(scheduler.get_jobs())
    job_executor_aliases = tuple(
        sorted((job.id, job.executor) for job in jobs)
    )
    job_ids = tuple(job_id for job_id, _executor in job_executor_aliases)
    duplicate_job_ids = tuple(
        sorted(
            job_id
            for index, job_id in enumerate(job_ids[1:], start=1)
            if job_id == job_ids[index - 1]
        )
    )
    if duplicate_job_ids:
        raise ValueError(
            "duplicate scheduler job id: " + ",".join(duplicate_job_ids)
        )

    required_job_executor_aliases = tuple(
        sorted(required_job_executors.items())
    )
    installed_executors = dict(job_executor_aliases)
    missing_required_job_ids = tuple(
        job_id
        for job_id, _expected_executor in required_job_executor_aliases
        if job_id not in installed_executors
    )
    wrong_executor_aliases = tuple(
        (
            job_id,
            expected_executor,
            installed_executors[job_id],
        )
        for job_id, expected_executor in required_job_executor_aliases
        if job_id in installed_executors
        and installed_executors[job_id] != expected_executor
    )
    forbidden_matches = tuple(
        job_id
        for job_id in job_ids
        if any(
            pattern.match(job_id) is not None
            for pattern in FORBIDDEN_RESEARCH_JOB_PATTERNS
        )
    )
    running = scheduler.running is True
    safe = (
        running
        and missing_required_job_ids == ()
        and wrong_executor_aliases == ()
        and forbidden_matches == ()
    )
    return SchedulerAttestation(
        running=running,
        job_ids=job_ids,
        job_executor_aliases=job_executor_aliases,
        required_job_executor_aliases=required_job_executor_aliases,
        missing_required_job_ids=missing_required_job_ids,
        wrong_executor_aliases=wrong_executor_aliases,
        forbidden_matches=forbidden_matches,
        safe=safe,
    )


__all__ = [
    "A_REQUIRED_JOB_EXECUTORS",
    "FORBIDDEN_RESEARCH_JOB_PATTERNS",
    "SchedulerAttestation",
    "build_scheduler_attestation",
]
