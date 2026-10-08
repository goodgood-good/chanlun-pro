"""Per-symbol error counts cannot erase successful period results."""

from copy import deepcopy

import pytest

from cl_app.services.screening import all_periods_failed, validate_settings
from chanlun.screening.rules import trading_context
from tests.web.test_signal_monitor import setup as setup, _seed, signal


def mixed_run(manual, cutoff):
    manual.start(
        validate_settings({"scope": "all_a_watchlist", "frequencies": ["1m", "5m"]})
    )
    manual.rows = [{**signal("SZ.000001"), "source_closed_at": cutoff}]
    manual.errors = [
        {"market": "a", "code": code, "frequency": "1m", "reasons": ["DATA_GAPS"]}
        for code in ("SZ.000001", "SH.600088")
    ]
    manual.finish(cutoff)
    manual.state.update(
        total=2, completed=2, error_count=2, selected_count=1, successful_period_count=2
    )


def test_partial_period_errors_do_not_block_verified_new_candidates(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service._consume_monitor()
    old_run = service._load()["seed"]["run_id"]
    mixed_run(manual, cutoff)
    service.tick()
    assert service._load()["seed"]["run_id"] == manual.state["run_id"] != old_run
    assert "a:SZ.000001" in service._load()["seed"]["codes"]


def test_successful_period_without_candidates_is_completed_instead_of_retried(setup):
    service, manual, _, clock, cutoff = _seed(setup, [])
    mixed_run(manual, cutoff)
    manual.rows = []
    manual.state["selected_count"] = 0
    service._load()["daily"] = {
        "session": clock[0].date().isoformat(),
        "afternoon": {
            "run_id": manual.state["run_id"],
            "status": "started",
            "attempts": 1,
        },
    }
    service._record_daily_run(manual.status())
    assert service._load()["daily"]["afternoon"]["status"] == "completed"


def test_a_fully_failed_run_still_keeps_the_previous_pool_and_allows_retry(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    service.tick()
    live.rows = [signal()]
    live.finish(cutoff)
    service._consume_monitor()
    previous = deepcopy(service._load()["seed"])
    mixed_run(manual, cutoff)
    manual.rows = []
    manual.state.update(selected_count=0, successful_period_count=0)
    service._load()["daily"] = {
        "session": clock[0].date().isoformat(),
        "afternoon": {
            "run_id": manual.state["run_id"],
            "status": "started",
            "attempts": 1,
        },
    }
    service.tick()
    assert service._load()["seed"]["run_id"] == previous["run_id"]
    assert service._load()["daily"]["afternoon"]["status"] == "failed"


@pytest.mark.parametrize("legacy", [False, True])
def test_daily_schedule_reuses_a_partial_success_at_the_requested_cutoff(setup, legacy):
    service, manual, _, clock = setup
    clock[0] = clock[0].replace(hour=15, minute=11)
    cutoff = trading_context(clock[0], "5m", 1, 1)["cutoff"]
    mixed_run(manual, cutoff)
    if legacy:
        manual.state.pop("successful_period_count")
    service._load()["settings"]["screening"] = deepcopy(manual.state["settings"])
    started = len(manual.started)
    service._schedule_daily(clock[0], cutoff, manual.status())
    assert len(manual.started) == started
    assert service._load()["daily"]["afternoon"] == {
        "run_id": manual.state["run_id"],
        "status": "completed",
    }


def test_verified_negative_result_is_usable_even_when_all_symbols_have_another_failed_period():
    assert not all_periods_failed(
        {
            "total": 2,
            "error_count": 2,
            "selected": [],
            "selected_count": 0,
            "verified_periods": [
                {"code": "SZ.000001", "frequency": "5m", "source_closed_at": 100}
            ],
        }
    )


def test_missing_verified_evidence_cannot_be_replaced_by_success_counters():
    assert all_periods_failed(
        {
            "total": 2,
            "error_count": 2,
            "selected": [],
            "selected_count": 1,
            "successful_period_count": 2,
            "verified_periods": [],
        }
    )


def test_loading_results_are_not_prematurely_marked_failed():
    assert not all_periods_failed(
        {
            "total": 2,
            "error_count": 2,
            "results_loading": True,
            "selected": [],
            "verified_periods": [],
        }
    )
