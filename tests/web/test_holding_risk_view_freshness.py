"""Every visible holding-risk input must invalidate both server and UI caches."""

from datetime import datetime
from unittest.mock import Mock

import pytest

from chanlun.screening.rules import CN
from cl_app.services import holding_risk, signal_monitor


@pytest.fixture
def view(tmp_path, monkeypatch):
    jobs = Mock()
    jobs.root = tmp_path / "runs"
    jobs.status.return_value = {"status": "completed", "source_revision": "current", "current_source_revision": "current"}
    service = signal_monitor.SignalMonitor(tmp_path, screening=jobs, monitor=jobs,
                                          notifications=Mock(), now=lambda: datetime(2026, 9, 30, 10, 0, tzinfo=CN))
    state = service._load()
    state.update(enabled=True, holding_symbols=["a:SH.600000"], holding_source_current=True,
                 seed={"settings": {"frequencies": ["5m"]}})
    cutoff = [300]
    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", lambda *args: cutoff[0])
    calls = []

    def choices(directory, record):
        calls.append(record["evidence"])
        return [{"signal_id": record["evidence"]["proof"]}]

    monkeypatch.setattr(holding_risk, "choices", choices)
    return service, state, cutoff, calls


def revision(service):
    return service.business_health(revision="current")["risk_revision"]


def evidence(state):
    state["holding_evidence"]["a:SH.600000:5m"] = {
        "market": "a", "code": "SH.600000", "name": "fixture", "frequency": "5m",
        "source_closed_at": 300, "revision": "current", "evidence": {"proof": "first"},
    }


def test_missing_evidence_period_progress_does_not_stay_cached(view):
    service, state, cutoff, calls = view
    assert service.holding_risks()["holdings"][0]["periods"][0]["status"] == "pending"
    state["selection_checks"]["a:SH.600000:5m"] = {"revision": "current", "error": True, "failed_cutoff": 300}
    assert service.holding_risks()["holdings"][0]["periods"][0]["status"] == "limited"


def test_missing_evidence_check_changes_the_browser_refresh_revision(view):
    service, state, cutoff, calls = view
    before = revision(service)
    state["selection_checks"]["a:SH.600000:5m"] = {"revision": "current", "cutoff": 300, "checked_at": 310}
    assert revision(service) != before


def test_changed_monitored_periods_refresh_the_response_and_browser_revision(view):
    service, state, cutoff, calls = view
    service.holding_risks()
    before = revision(service)
    state["seed"]["settings"]["frequencies"] = ["30m"]
    assert [row["frequency"] for row in service.holding_risks()["holdings"][0]["periods"]] == ["30m"]
    assert revision(service) != before


def test_pausing_monitoring_changes_the_browser_refresh_revision(view):
    service, state, cutoff, calls = view
    before = revision(service)
    state["enabled"] = False
    assert revision(service) != before
    assert service.holding_risks()["monitor_enabled"] is False


def test_same_cutoff_evidence_replacement_changes_browser_revision(view):
    service, state, cutoff, calls = view
    evidence(state)
    before = revision(service)
    assert service.holding_risks()["holdings"][0]["choices"] == [{"signal_id": "first"}]
    state["holding_evidence"]["a:SH.600000:5m"]["evidence"] = {"proof": "corrected"}
    assert revision(service) != before
    assert service.holding_risks()["holdings"][0]["choices"] == [{"signal_id": "corrected"}]


def test_new_closed_bar_refreshes_the_view_before_recheck_finishes(view):
    service, state, cutoff, calls = view
    evidence(state)
    assert service.holding_risks()["holdings"][0]["choices"]
    before = revision(service)
    cutoff[0] = 600
    assert revision(service) != before
    assert service.holding_risks()["holdings"][0]["choices"] == []


def test_transient_choice_read_failure_can_recover_without_a_state_change(view, monkeypatch):
    service, state, cutoff, calls = view
    evidence(state)
    original = holding_risk.choices
    attempts = []

    def choices(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("controlled evidence read failure")
        return original(*args)

    monkeypatch.setattr(holding_risk, "choices", choices)
    assert service.holding_risks()["holdings"][0]["choices"] == []
    assert service.holding_risks()["holdings"][0]["choices"] == [{"signal_id": "first"}]


def test_unrelated_market_checks_are_not_copied_or_used_for_view_revision(view):
    service, state, cutoff, calls = view

    class NotAViewDependency:
        def __deepcopy__(self, memo):
            raise AssertionError("unrelated screening check was copied into holding view")

    before = revision(service)
    state["selection_checks"]["a:SH.600999:5m"] = {"large": NotAViewDependency()}
    service.holding_risks()
    assert revision(service) == before


def test_active_plan_period_outside_the_scan_settings_is_a_dependency(view):
    service, state, cutoff, calls = view
    state["holding_risk_plans"]["a:SH.600000:1m"] = {
        "market": "a", "code": "SH.600000", "frequency": "1m", "active": True,
        "binding_id": "test-only", "status": "active",
    }
    service.holding_risks()
    before = revision(service)
    state["selection_checks"]["a:SH.600000:1m"] = {"revision": "current", "cutoff": 300, "checked_at": 310}
    periods = {row["frequency"]: row for row in service.holding_risks()["holdings"][0]["periods"]}
    assert periods["1m"]["status"] == "ready"
    assert revision(service) != before


def test_cached_response_is_independent_of_caller_mutation(view):
    service, state, cutoff, calls = view
    evidence(state)
    first = service.holding_risks()
    first["holdings"][0]["choices"][0]["signal_id"] = "caller-change"
    assert service.holding_risks()["holdings"][0]["choices"] == [{"signal_id": "first"}]
    assert len(calls) == 1
