"""A partial daily screening failure must not erase a prior monitor member."""

from copy import deepcopy

import pytest

from cl_app.services.signal_monitor import SignalMonitor
from tests.web.test_signal_monitor import setup as setup, signal
from tests.web.test_monitor_pool_handoff_reuse import checked_pool, finish_replacement


def finish_live(live, cutoff):
    live.finish(cutoff)
    live.state.update(source_revision="code", current_source_revision="code")


def partial_replacement(setup, *, error=None, frequencies=None):
    service, manual, live, clock, cutoff = checked_pool(setup)
    settings = deepcopy(manual.state["settings"])
    if frequencies:
        settings["frequencies"] = frequencies
    finish_replacement(manual, settings, cutoff, [signal("SZ.000001")])
    manual.errors = [
        error
        or {
            "market": "a",
            "code": "SH.600088",
            "frequency": "5m",
            "reasons": ["DATA_GAPS"],
        }
    ]
    manual.state.update(total=2, completed=2, error_count=1, successful_period_count=1)
    return service, manual, live, clock, cutoff


def test_failed_prior_candidate_stays_in_pool_without_publishing_stale_signal(setup):
    service, manual, live, _, _ = partial_replacement(setup)
    old = deepcopy(next(iter(service._load()["signals"].values())))
    service.tick()
    state = service._load()
    assert state["seed"]["run_id"] == manual.state["run_id"]
    assert set(state["seed"]["codes"]) == {"a:SH.600088", "a:SZ.000001"}
    assert {row["code"] for row in live.started[-1]["symbols"]} == {
        "SH.600088",
        "SZ.000001",
    }
    assert old["id"] not in state["signals"]
    assert state["unverified_signals"][old["id"]] == old
    view = service.snapshot(page_options={})
    assert view["seed"]["screening_count"] == 1
    assert view["seed"]["screening_recheck_count"] == 1
    assert service._validate_notification_events([old]) == ([], [old])


def test_recovered_positive_rejoins_candidates_without_duplicate_discovery(setup):
    service, _, live, _, cutoff = partial_replacement(setup)
    old = deepcopy(next(iter(service._load()["signals"].values())))
    service.tick()
    live.rows = [signal(), signal("SZ.000001")]
    finish_live(live, cutoff)
    service._consume_monitor()
    state = service._load()
    assert state["seed"].get("screening_rechecks") == {}
    assert set(state["seed"]["codes"]) == {"a:SH.600088", "a:SZ.000001"}
    assert state["signals"][old["id"]]["first_detected_at"] == old["first_detected_at"]
    assert (
        len([event for event in state["events"] if event["code"] == "SH.600088"]) == 1
    )


@pytest.mark.parametrize("membership", ["screening", "watchlist", "holding"])
def test_verified_negative_removes_only_the_screening_membership(
    setup, monkeypatch, membership
):
    service, _, live, _, cutoff = partial_replacement(setup)
    if membership != "screening":
        source = "watchlist_symbols" if membership == "watchlist" else "holding_symbols"
        monkeypatch.setattr(
            "cl_app.services.signal_monitor." + source,
            lambda: [
                {
                    "market": "a",
                    "code": "SH.600088",
                    "name": "watched",
                    "origin": "watchlist",
                }
            ],
        )
    service.tick()
    live.rows = [signal("SZ.000001")]
    finish_live(live, cutoff)
    service._consume_monitor()
    state = service._load()
    assert state["seed"].get("screening_rechecks") == {}
    assert {row["code"] for row in state["seed"]["screening_symbols"]} == {"SZ.000001"}
    assert ("a:SH.600088" in state["seed"]["codes"]) is (membership != "screening")


def test_missing_one_period_keeps_pending_membership_across_restart(setup, monkeypatch):
    service, manual, live, clock, cutoff = partial_replacement(
        setup, frequencies=["5m", "30m"]
    )
    service.tick()
    live.rows = [signal("SZ.000001")]
    committed = live.committed_results

    def incomplete(*args, **kwargs):
        result = committed(*args, **kwargs)
        result["verified_periods"] = [
            row for row in result["verified_periods"] if row["frequency"] == "5m"
        ]
        return result

    live.committed_results = incomplete
    finish_live(live, cutoff)
    service._consume_monitor()
    assert "a:SH.600088" in service._load()["seed"]["screening_rechecks"]
    restored = SignalMonitor(
        service.root, screening=manual, monitor=live, now=lambda: clock[0]
    )
    monkeypatch.setattr(restored, "start_runtime", lambda: None)
    assert "a:SH.600088" in restored._load()["seed"]["screening_rechecks"]
    restored.check_now()
    restored.tick()
    live.committed_results = committed
    finish_live(live, cutoff)
    restored._consume_monitor()
    assert "a:SH.600088" not in restored._load()["seed"]["codes"]


@pytest.mark.parametrize(
    "error",
    [
        {"code": "SH.600088", "frequency": "1m", "reasons": ["DATA_GAPS"]},
        {
            "code": "SH.600088",
            "frequency": "5m",
            "signal_type": "consolidation_divergence",
        },
        {"code": "SH.600089", "frequency": "5m", "reasons": ["DATA_GAPS"]},
    ],
)
def test_unrelated_error_does_not_keep_an_unselected_old_candidate(setup, error):
    service, _, _, _, _ = partial_replacement(setup, error=error)
    service.tick()
    assert service._load()["seed"]["codes"] == ["a:SZ.000001"]


def test_regressed_recovery_data_cannot_remove_the_pending_member(setup):
    service, _, live, _, cutoff = partial_replacement(setup)
    service.tick()
    live.rows = [signal("SZ.000001")]
    finish_live(live, cutoff - 300)
    service._consume_monitor()
    assert "a:SH.600088" in service._load()["seed"]["screening_rechecks"]
    assert any(
        "REGRESSED_SOURCE_CUTOFF" in row.get("reasons", [])
        for row in service._load()["errors"]
    )


def test_another_partial_replacement_preserves_the_pending_member_and_its_history(
    setup,
):
    service, manual, live, _, cutoff = partial_replacement(setup)
    old = deepcopy(next(iter(service._load()["signals"].values())))
    service.tick()
    finish_replacement(manual, manual.state["settings"], cutoff, [signal("SZ.000001")])
    service.tick()
    state = service._load()
    assert "a:SH.600088" in state["seed"]["screening_rechecks"]
    assert (
        state["unverified_signals"][old["id"]]["first_detected_at"]
        == old["first_detected_at"]
    )
    assert state["selection_checks"]["a:SH.600088:5m"]["cutoff"] == cutoff


def test_a_valid_candidate_on_one_period_can_rejoin_while_another_period_retries(setup):
    service, _, live, _, cutoff = partial_replacement(setup, frequencies=["5m", "30m"])
    service.tick()
    live.rows = [signal(), signal("SZ.000001")]
    live.errors = [
        {
            "market": "a",
            "code": "SH.600088",
            "frequency": "30m",
            "reasons": ["DATA_GAPS"],
        }
    ]
    finish_live(live, cutoff)
    service._consume_monitor()
    state = service._load()
    assert not state["seed"]["screening_rechecks"]
    assert any(row["code"] == "SH.600088" for row in state["signals"].values())
    assert "a:SH.600088" in state["retry_symbols"]
