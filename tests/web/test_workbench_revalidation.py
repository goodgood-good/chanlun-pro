"""Workbench candidates follow the monitor's pending-proof state."""

from copy import deepcopy

import pytest

from cl_app.services import screening, screening_workbench as workbench
from cl_app.services import signal_monitor
from tests.web.test_screening_workbench import fixture_run as fixture_run
from tests.web.test_signal_monitor import setup as setup, _seed


@pytest.fixture
def live_candidate(fixture_run, setup, monkeypatch):
    manager, source, _ = fixture_run
    row = deepcopy(manager.results()["selected"][0])
    revision = screening.source_revision()
    service, manual, live, clock, cutoff = _seed(setup, [row])
    manual.state.update(run_id=source, result_reader_revision="before-upgrade",
                        source_revision=revision, current_source_revision=revision)
    service.tick()
    live.rows = [{**row, "source_closed_at": cutoff}]
    live.state.update(source_revision=revision, current_source_revision=revision)
    live.finish(cutoff)
    service._consume_monitor()
    monkeypatch.setattr(signal_monitor, "service", service)
    candidate = workbench.dashboard()["candidates"][0]
    identity = signal_monitor._signal(candidate)["id"]
    original = deepcopy(service._load()["signals"][identity])
    return service, manual, live, cutoff, candidate, original, revision


def suspend(live_candidate):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    manual.state["result_reader_revision"] = "after-upgrade"
    service.tick()
    assert original["id"] not in service._load()["signals"]
    assert service._load()["unverified_signals"][original["id"]] == original
    assert service._validate_notification_events([original]) == ([], [original])


@pytest.mark.parametrize("compact", [False, True])
def test_reader_revalidation_hides_list_and_detail_until_live_recovery(live_candidate, compact):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    source = manual.state["run_id"]
    initial_version = workbench.live_recheck_version(source, revision)
    suspend(live_candidate)
    pending = workbench.dashboard(compact=compact)
    assert pending["candidates"] == [], "a signal suspended by the monitor remained selectable"
    assert pending["status"]["live_unverified_count"] == 1
    assert pending["status"]["live_exclusion_count"] == 0
    assert pending["status"]["live_recheck_version"] != initial_version
    with pytest.raises(ValueError, match="复核"):
        workbench.candidate_detail(source, candidate["id"])

    live.finish(cutoff)
    service._consume_monitor()
    restored = workbench.dashboard()
    assert restored["candidates"][0]["point"] == candidate["point"]
    assert restored["status"]["live_unverified_count"] == 0
    assert restored["status"]["live_recheck_version"] != pending["status"]["live_recheck_version"]
    assert service._load()["signals"][original["id"]]["first_detected_at"] == original["first_detected_at"]
    assert workbench.candidate_detail(source, candidate["id"])["candidate"]["id"] == candidate["id"]


@pytest.mark.parametrize("check_state", ["missing", "old_revision", "old_cutoff"])
def test_pending_identity_is_hidden_even_without_a_matching_successful_check(live_candidate, check_state):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    suspend(live_candidate)
    checks = service._load()["selection_checks"]
    key = f"{candidate['market']}:{candidate['code']}:{candidate['frequency']}"
    if check_state == "missing":
        checks.pop(key)
    elif check_state == "old_revision":
        checks[key]["revision"] = "previous-algorithm"
    else:
        checks[key]["cutoff"] = -1
    result = workbench.dashboard()
    assert result["candidates"] == []
    assert result["status"]["live_unverified_count"] == 1
    assert result["status"]["live_exclusion_count"] == 0


def test_pending_identity_does_not_hide_another_verified_point_in_the_same_period(live_candidate):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    suspend(live_candidate)
    other = deepcopy(candidate)
    other["point"]["point_id"] = "other-verified-point"
    other_id = signal_monitor._signal(other)["id"]
    key = f"{candidate['market']}:{candidate['code']}:{candidate['frequency']}"
    service._load()["selection_checks"][key]["identities"].append(other_id)
    rows, removed, version, uncertain = workbench._live_rows(
        [deepcopy(candidate), other], manual.state["run_id"], revision)
    assert [row["point"]["point_id"] for row in rows] == ["other-verified-point"]
    assert removed == 0 and uncertain == 1


def test_successful_exit_proof_keeps_precedence_over_an_old_pending_marker(live_candidate):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    suspend(live_candidate)
    key = f"{candidate['market']}:{candidate['code']}:{candidate['frequency']}"
    service._load()["selection_checks"][key]["identities"] = []
    result = workbench.dashboard()
    assert result["candidates"] == []
    assert result["status"]["live_exclusion_count"] == 1
    assert result["status"]["live_unverified_count"] == 0


def test_pending_overlay_does_not_expose_or_mutate_private_signal_proof(live_candidate):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    suspend(live_candidate)
    before = deepcopy(service._load())
    overlay = service.selection_overlay(manual.state["run_id"], revision)
    key = f"{candidate['market']}:{candidate['code']}:{candidate['frequency']}"
    assert overlay[key]["unverified_identities"] == [original["id"]]
    assert "evidence" not in overlay[key] and "first_detected_at" not in overlay[key]
    overlay[key]["unverified_identities"].clear()
    overlay[key]["identities"].clear()
    assert service._load() == before
    assert service.selection_overlay("another-run", revision) == {}
