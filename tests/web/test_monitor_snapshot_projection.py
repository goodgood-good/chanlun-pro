"""Scheduler and public snapshots should copy only data they actually consume."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from cl_app.services import signal_monitor
from tests.web.test_monitor_control_ownership import monitor as monitor


class NotCopied(dict):
    def __deepcopy__(self, memo):
        raise AssertionError("an unused internal collection was copied")


def idle_tick(service, monkeypatch):
    monkeypatch.setattr(service, "_sync_watchlist", lambda: None)
    monkeypatch.setattr(service, "_schedule_daily", lambda *args: None)
    monkeypatch.setattr(service, "_schedule_monitor", lambda *args: None)
    monkeypatch.setattr(signal_monitor, "closed_bar_cutoff", lambda *args: 300)


def test_idle_tick_ignores_signal_history_and_unrelated_seed_payloads(monitor, monkeypatch):
    service, manual, live, state = monitor
    idle_tick(service, monkeypatch)
    for key in ("signals", "known", "events", "selection_checks", "holding_evidence",
                "holding_risk_plans", "pending_notifications", "unverified_signals"):
        state[key] = NotCopied()
    state["seed"]["settings"] = NotCopied(frequencies=["5m"])
    state["seed"]["symbols"] = NotCopied()
    service.tick()


def test_paused_tick_does_not_copy_any_seed_members(monitor, monkeypatch):
    service, manual, live, state = monitor
    state["enabled"] = False
    state["seed"]["screening_symbols"] = NotCopied()
    published = Mock()
    monkeypatch.setattr(service, "_publish_notifications", published)
    service.tick()
    published.assert_called_once()
    live.start.assert_not_called()


def test_stopped_tick_never_loads_state(monitor, monkeypatch):
    service, manual, live, state = monitor
    service._stop.set()
    loading = Mock(side_effect=AssertionError("stopped runtime read its state"))
    monkeypatch.setattr(service, "_load", loading)
    service.tick()
    loading.assert_not_called()
    manual.status.assert_not_called()


def test_historical_handoff_keeps_its_detached_prior_members(monitor, monkeypatch):
    service, manual, live, state = monitor
    idle_tick(service, monkeypatch)
    state["seed"].update(selection_policy_version="policy", result_reader_revision="old-reader")
    settings = deepcopy(state["seed"]["settings"])
    result = {"status": "completed", "run_id": "previous", "source_current": False,
              "frozen_native_review": True, "selection_policy_version": "policy",
              "result_reader_revision": "new-reader", "settings": settings,
              "selected": [], "errors": [], "verified_periods": []}
    manual.status.return_value = deepcopy(result)

    def read(*args):
        state["seed"]["screening_symbols"][0]["name"] = "changed during lookup"
        return deepcopy(result)

    monkeypatch.setattr(service, "_screening_results_for_monitor", read)
    service.tick()
    assert state["seed"]["screening_symbols"][0]["name"] == "existing"
    assert state["seed"]["result_reader_revision"] == "new-reader"


@pytest.mark.parametrize("private_field", ["known", "unverified_signals"])
def test_public_snapshot_does_not_copy_fields_it_will_discard(monitor, private_field):
    service, manual, live, state = monitor
    service.notifications.snapshot.return_value = {}
    state[private_field] = NotCopied(large_internal_payload="not visible")
    result = service.snapshot()
    assert private_field not in result


def test_public_snapshot_still_returns_detached_visible_values(monitor):
    service, manual, live, state = monitor
    service.notifications.snapshot.return_value = {}
    first = service.snapshot()
    first["seed"]["symbols"][0]["name"] = "caller edit"
    first["settings"]["interval_seconds"] = 599
    assert state["seed"]["symbols"][0]["name"] == "existing"
    assert service.snapshot()["settings"]["interval_seconds"] != 599
