from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from chanlun.screening import runner
from chanlun.screening.rules import CN
from cl_app.services.holding_risk import choices, slot, advance
from cl_app.services.signal_monitor import SignalMonitor
from tests.web.test_holding_risk import reference, frame, snapshot
from tests.web.test_signal_monitor import setup as setup
from tests.web.test_signal_monitor import signal, Jobs


def test_risk_facts_survive_selection_exit_but_stop_after_unbinding_or_holding_removal(
    tmp_path, monkeypatch
):
    plan, events = advance(reference(), frame(), snapshot(), {}, {}, set(), 310)
    jobs = Jobs(tmp_path / "runs")
    jobs.state = {"status": "completed", "source_current": True}
    monitor = SignalMonitor(
        tmp_path, screening=jobs, monitor=jobs, notifications=Mock()
    )
    state = monitor._load()
    state.update(holding_source_current=True, holding_symbols=["a:SH.600000"])
    state["holding_risk_plans"] = {"a:SH.600000:5m": plan}
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.holding_symbols",
        lambda: [{"market": "a", "code": "SH.600000"}],
    )
    assert monitor._validate_notification_events(events) == (events, [])
    plan["active"] = False
    assert monitor._validate_notification_events(events) == ([], [])
    plan["active"] = True
    monkeypatch.setattr("cl_app.services.signal_monitor.holding_symbols", lambda: [])
    assert monitor._validate_notification_events(events) == ([], [])


def test_bound_shorter_period_does_not_make_other_symbols_scan_that_period(
    setup, monkeypatch
):
    service, manual, live, clock = setup
    held = {"market": "a", "code": "SH.600088", "name": "held", "origin": "watchlist"}
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.holding_symbols", lambda: [held]
    )
    from cl_app.services.screening import validate_settings
    from chanlun.screening.rules import trading_context

    manual.start(
        validate_settings({"scope": "all_a_watchlist", "frequencies": ["30m"]})
    )
    manual.rows = [
        {**signal(), "frequency": "30m"},
        {**signal("SH.600089"), "frequency": "30m"},
    ]
    manual.finish(trading_context(clock[0], "30m", 1, 1)["cutoff"])
    service._load()["holding_risk_plans"] = {
        "a:SH.600088:5m": {**reference(), "code": "SH.600088"}
    }
    service.tick()
    assert live.runtime_periods == {"a:SH.600088": ["5m"]}
    assert live.started[0]["frequencies"] == ["30m"]
    assert (
        service._load()["active_cutoffs"]["a:SH.600088"]
        == trading_context(clock[0], "5m", 1, 1)["cutoff"]
    )
    assert (
        service._load()["active_cutoffs"]["a:SH.600089"]
        == trading_context(clock[0], "30m", 1, 1)["cutoff"]
    )


    cutoff_30=trading_context(clock[0],'30m',1,1)['cutoff']
    cutoff_5=trading_context(clock[0],'5m',1,1)['cutoff']
    original=live.committed_results
    def results(*args,**kwargs):
        value=original(*args,**kwargs)
        value['verified_periods'].append({'market':'a','code':'SH.600088','frequency':'5m',
                                         'source_closed_at':cutoff_5,'consolidation_checked':True})
        return value
    live.committed_results=results;live.finish(cutoff_30);service._consume_monitor()
    assert service._load()['last_cutoffs']['30m']==cutoff_30
    assert service._load()['last_cutoffs']['5m']==cutoff_5


def test_real_recorded_reference_is_replayed_before_binding_and_requires_fresh_data(
    tmp_path, monkeypatch
):
    full = pd.read_parquet(Path("tests/fixtures/native_30m/SH.600183_30m.parquet"))
    full.attrs.update(
        structure_price_quantum="0.01", price_basis_revision="recorded-raw"
    )
    sample = full.iloc[:1436].copy()
    snap = runner.snapshot_for(sample, "SH.600183", "30m")
    runs = tmp_path / "runs"
    directory = runs / ("a" * 32)
    manifest = runner._save_evidence(directory, "SH.600183", "30m", sample, snap)
    record = {
        "market": "a",
        "code": "SH.600183",
        "name": "fixture",
        "frequency": "30m",
        "source_closed_at": snap["source_closed_at"],
        "evidence": manifest,
        "evidence_run_id": directory.name,
        "revision": "test",
    }
    clock = [record["source_closed_at"]]
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.closed_bar_cutoff", lambda *a: clock[0]
    )
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.holding_symbols", lambda: [record]
    )
    jobs = Jobs(runs)
    jobs.state = {
        "status": "completed",
        "source_current": True,
        "source_revision": "test",
    }
    notifications = Mock()
    notifications.snapshot.return_value = {}
    service = SignalMonitor(
        tmp_path,
        screening=jobs,
        monitor=jobs,
        notifications=notifications,
        now=lambda: datetime.fromtimestamp(clock[0] + 5, CN),
    )
    key = slot("a", "SH.600183", "30m")
    state = service._load()
    state.update(
        enabled=True, holding_source_current=True, holding_symbols=["a:SH.600183"]
    )
    state["holding_evidence"] = {key: record}
    state["selection_checks"] = {
        key: {"revision": "test", "error": False, "cutoff": clock[0]}
    }
    choice = choices(runs, record)[0]
    body = {
        "action": "bind",
        "signal_id": choice["signal_id"],
        "source_closed_at": clock[0],
    }
    service.configure_holding_risk(body)
    bound = deepcopy(state["holding_risk_plans"][key])
    assert bound["active"] and bound["stop_price"] == choice["stop_price"]
    service.configure_holding_risk(body)
    assert state["holding_risk_plans"][key]["binding_id"] == bound["binding_id"]
    clock[0] += 1800
    assert not service.holding_risks()["holdings"][0]["choices"]
    with pytest.raises(ValueError):
        service.configure_holding_risk(body)
    service.configure_holding_risk(
        {"action": "unbind", "binding_id": bound["binding_id"]}
    )
    assert not state["holding_risk_plans"][key]["active"]
