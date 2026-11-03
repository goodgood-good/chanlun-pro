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
    row['point'].update(available_at=cutoff, confirmed_at=cutoff)
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
    queued = {**original, 'recorded_at':original['first_detected_at']}
    assert service._validate_notification_events([queued]) == ([], [queued])


@pytest.mark.parametrize("compact", [False, True])
def test_reader_revalidation_keeps_readonly_reference_and_blocks_detail_until_live_recovery(live_candidate, compact):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    source = manual.state["run_id"]
    initial_version = workbench.live_recheck_version(source, revision)
    suspend(live_candidate)
    pending = workbench.dashboard(compact=compact)
    assert len(pending['candidates']) == 1
    assert pending['candidates'][0]['refresh_state'] == 'limited'
    assert pending['candidates'][0]['evidence_chart_available'] is False
    assert pending['status']['current_result_count'] == 0
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
def test_pending_identity_is_readonly_even_without_a_matching_successful_check(live_candidate, check_state):
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
    assert len(result['candidates']) == 1
    assert result['candidates'][0]['refresh_state'] == 'limited'
    assert result['status']['current_result_count'] == 0
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


def test_previous_display_survives_temporary_revalidation_but_not_a_proven_exit(live_candidate):
    service, manual, live, cutoff, candidate, original, revision = live_candidate
    suspend(live_candidate)
    previous={**deepcopy(candidate),'refresh_state':'pending','result_source':manual.state['run_id']}
    rows,removed,_,uncertain=workbench._live_rows([previous],manual.state['run_id'],revision,preserve_unverified=True)
    assert len(rows)==1 and rows[0]['refresh_state']=='limited' and removed==0 and uncertain==1
    live.state.update(source_revision=revision,current_source_revision=revision)
    live.finish(cutoff);service._consume_monitor()
    rows,removed,_,uncertain=workbench._live_rows([previous],manual.state['run_id'],revision,preserve_unverified=True)
    assert len(rows)==1 and removed==uncertain==0
    key=f"{candidate['market']}:{candidate['code']}:{candidate['frequency']}"
    service._load()['selection_checks'][key]['identities']=[]
    assert workbench._live_rows([previous],manual.state['run_id'],revision,preserve_unverified=True)[0]==[]


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


def test_incomplete_preview_check_preserves_readonly_row_until_recovery(setup, monkeypatch):
    from tests.web.test_native_point_previews import _monitor_preview
    service, live, row, finish = _monitor_preview(setup, monkeypatch)
    monkeypatch.setattr(signal_monitor, 'service', service)
    source = service._load()['seed']['run_id']
    revision = live.state.get('source_revision')
    finish([], checked=False)
    rows, removed, _, uncertain = workbench._live_rows(
        [deepcopy(row)], source, revision, preserve_unverified=True)
    assert len(rows) == 1 and rows[0]['refresh_state'] == 'limited'
    assert removed == 0 and uncertain == 1
    assert workbench._live_rows([deepcopy(row)], source, revision)[0] == []
    other = deepcopy(row)
    other['point']['point_id'] = 'a-different-exited-point'
    rows, removed, _, uncertain = workbench._live_rows(
        [deepcopy(row), other], source, revision, preserve_unverified=True)
    assert len(rows) == 1 and removed == uncertain == 1
    service._save()
    restored = signal_monitor.SignalMonitor(service.root, screening=service.screening,
                                           monitor=live, now=service.now)
    monkeypatch.setattr(signal_monitor, 'service', restored)
    assert workbench._live_rows([deepcopy(row)], source, revision, preserve_unverified=True)[0][0]['refresh_state'] == 'limited'
    monkeypatch.setattr(signal_monitor, 'service', service)
    finish([row])
    rows, removed, _, uncertain = workbench._live_rows(
        [deepcopy(row)], source, revision, preserve_unverified=True)
    assert len(rows) == 1 and not rows[0].get('refresh_state')
    assert removed == uncertain == 0


def test_verified_preview_invalidation_removes_active_candidate_but_keeps_event(setup, monkeypatch):
    from tests.web.test_native_point_previews import _monitor_preview
    from tests.web.warning_helpers import refute_warning
    service, live, row, finish = _monitor_preview(setup, monkeypatch)
    monkeypatch.setattr(signal_monitor, 'service', service)
    source = service._load()['seed']['run_id']
    refute_warning(monkeypatch)
    finish([], checked=True)
    invalidated = next(event for event in service._load()['events'] if event.get('stage') == 'invalidated')
    assert service._validate_notification_events([invalidated])[0]
    rows, removed, _, uncertain = workbench._live_rows(
        [deepcopy(row)], source, live.state.get('source_revision'), preserve_unverified=True)
    assert rows == [] and removed == 1 and uncertain == 0
    finish([row])
    rows, removed, _, uncertain = workbench._live_rows(
        [deepcopy(row)], source, live.state.get('source_revision'), preserve_unverified=True)
    assert len(rows) == 1 and not rows[0].get('refresh_state') and removed == uncertain == 0
