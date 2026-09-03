"""Operational scheduling policy for the live screening service.

This module may change how bounded realtime work is scheduled, but it never
changes the decision produced for a fixed authenticated structure bundle.  It
remains visible in the full decision-source manifest so a policy revision is
auditable; an already complete snapshot may be retagged only when the verified
manifest diff is confined to this module.
"""

from __future__ import annotations

import math


def candidate_monitor_deadline_perf(
    *,
    priority_deadline_perf: float,
    candidate_budget_deadline_perf: float,
    minute_codes_present: bool,
    force_startup_bootstrap: bool,
    compute_window_open: bool,
    priority_finalization_reserve_seconds: float = 0.0,
) -> float:
    """Return the absolute candidate admission deadline.

    During a trading/pre-open compute window, 1m and candidate work share one
    minute boundary.  A forced bootstrap outside every compute window has no
    next completed minute to protect, so its bounded candidate catch-up may use
    the independent candidate budget after the locator has been verified.
    """

    deadlines = (priority_deadline_perf, candidate_budget_deadline_perf)
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        for value in deadlines
    ):
        raise ValueError("monitor deadlines must be finite numbers")
    if any(
        type(value) is not bool
        for value in (
            minute_codes_present,
            force_startup_bootstrap,
            compute_window_open,
        )
    ):
        raise TypeError("monitor deadline policy flags must be exact bools")
    if (
        isinstance(priority_finalization_reserve_seconds, bool)
        or not isinstance(priority_finalization_reserve_seconds, (int, float))
        or not math.isfinite(priority_finalization_reserve_seconds)
        or priority_finalization_reserve_seconds < 0
    ):
        raise ValueError("priority finalization reserve must be non-negative")
    closed_startup_candidate_catchup = bool(
        force_startup_bootstrap and not compute_window_open
    )
    live_shared_boundary = bool(
        not closed_startup_candidate_catchup
        and (minute_codes_present or compute_window_open)
    )
    return (
        min(
            priority_deadline_perf - priority_finalization_reserve_seconds,
            candidate_budget_deadline_perf,
        )
        if live_shared_boundary
        else candidate_budget_deadline_perf
    )


__all__ = ("candidate_monitor_deadline_perf",)
