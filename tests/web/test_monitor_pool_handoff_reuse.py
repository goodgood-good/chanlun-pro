"""A new screening run reuses only equivalent, successful live checks."""

from copy import deepcopy

import pytest

from tests.web.test_signal_monitor import setup as setup, _seed, signal


def checked_pool(setup):
    service, manual, live, clock, cutoff = _seed(setup, [signal()])
    manual.state.update(
        selection_policy_version="policy", result_reader_revision="reader"
    )
    service.tick()
    live.rows = [signal()]
    live.state.update(source_revision="code", current_source_revision="code")
    live.finish(cutoff)
    committed = live.committed_results

    def with_fingerprint(*args, **kwargs):
        result = committed(*args, **kwargs)
        result["verified_periods"] = [
            {**p, "input_fingerprint": "a" * 64} for p in result["verified_periods"]
        ]
        return result

    live.committed_results = with_fingerprint
    service._consume_monitor()
    return service, manual, live, clock, cutoff


def finish_replacement(manual, settings, cutoff, rows=None, **metadata):
    manual.start(settings)
    manual.rows = [signal()] if rows is None else rows
    for row in manual.rows:
        row.setdefault("input_fingerprint", "a" * 64)
    manual.finish(cutoff)
    manual.state.update(
        source_revision="code",
        current_source_revision="code",
        selection_policy_version="policy",
        result_reader_revision="reader",
    )
    manual.state.update(metadata)


def test_new_run_and_worker_count_do_not_discard_equivalent_successful_checks(setup):
    service, manual, live, _, cutoff = checked_pool(setup)
    before = deepcopy(service._load()["selection_checks"])
    settings = {**manual.state["settings"], "workers": 3}
    finish_replacement(manual, settings, cutoff)
    service.tick()
    assert service._load()["selection_checks"] == before
    assert len(live.started) == 1


def test_new_pool_checks_only_new_symbols(setup):
    service, manual, live, _, cutoff = checked_pool(setup)
    finish_replacement(
        manual, manual.state["settings"], cutoff, [signal(), signal("SZ.000001")]
    )
    service.tick()
    assert [r["code"] for r in live.started[-1]["symbols"]] == ["SZ.000001"]
    assert service._load()["selection_checks"]["a:SH.600088:5m"]["identities"]


@pytest.mark.parametrize(
    "change",
    [
        "rule",
        "reader",
        "policy",
        "code",
        "error",
        "newer_data",
        "inflight",
        "missing_old_fingerprint",
        "missing_new_fingerprint",
        "revised_input",
    ],
)
def test_changed_or_unfinished_evidence_still_requires_a_new_check(setup, change):
    service, manual, live, _, cutoff = checked_pool(setup)
    settings = deepcopy(manual.state["settings"])
    metadata = {}
    if change == "rule":
        settings["max_anchor_gain_pct"] = 5
    elif change == "reader":
        metadata["result_reader_revision"] = "new-reader"
    elif change == "policy":
        metadata["selection_policy_version"] = "new-policy"
    elif change == "code":
        metadata.update(source_revision="new-code", current_source_revision="new-code")
    elif change == "error":
        service._load()["selection_checks"]["a:SH.600088:5m"]["error"] = True
    elif change == "inflight":
        service._load()["attempted_cutoffs"]["a:SH.600088"] = cutoff + 300
    elif change == "missing_old_fingerprint":
        service._load()["selection_checks"]["a:SH.600088:5m"].pop("input_fingerprint")
    row = signal()
    if change == "missing_new_fingerprint":
        row["input_fingerprint"] = None
    elif change == "revised_input":
        row["input_fingerprint"] = "b" * 64
    row["source_closed_at"] = cutoff + 300 if change == "newer_data" else cutoff
    finish_replacement(manual, settings, cutoff, [row], **metadata)
    service.tick()
    assert len(live.started) == 2
    assert live.started[-1]["symbols"][0]["code"] == "SH.600088"


def test_reuse_survives_restart_and_keeps_a_watchlist_symbol_outside_the_new_screening_rows(
    setup, monkeypatch
):
    from cl_app.services.signal_monitor import SignalMonitor

    service, manual, live, clock, cutoff = checked_pool(setup)
    service._load()["selection_checks"]["a:SH.600088:5m"]["identities"] = []
    service._load()["signals"] = {}
    monkeypatch.setattr(
        "cl_app.services.signal_monitor.watchlist_symbols",
        lambda: [
            {
                "market": "a",
                "code": "SH.600088",
                "name": "watched",
                "origin": "watchlist",
            }
        ],
    )
    service._save()
    restored = SignalMonitor(
        service.root, screening=manual, monitor=live, now=lambda: clock[0]
    )
    monkeypatch.setattr(restored, "start_runtime", lambda: None)
    finish_replacement(manual, manual.state["settings"], cutoff, [signal("SZ.000001")])
    restored.tick()
    assert "a:SH.600088" in restored._load()["seed"]["codes"]
    assert "a:SH.600088:5m" in restored._load()["selection_checks"]
    assert [s["code"] for s in live.started[-1]["symbols"]] == ["SZ.000001"]


def test_a_new_screening_error_requires_rechecking_even_when_old_live_data_was_valid(
    setup,
):
    service, manual, live, _, cutoff = checked_pool(setup)
    finish_replacement(manual, manual.state["settings"], cutoff)
    manual.errors = [
        {
            "market": "a",
            "code": "SH.600088",
            "frequency": "5m",
            "reasons": ["DATA_GAPS"],
        }
    ]
    service.tick()
    assert len(live.started) == 2


def test_missing_one_required_period_cannot_reuse_the_symbol_schedule(setup):
    service, manual, live, _, cutoff = checked_pool(setup)
    service._load()["seed"]["settings"]["frequencies"] = ["5m", "30m"]
    settings = {**manual.state["settings"], "frequencies": ["5m", "30m"]}
    finish_replacement(manual, settings, cutoff)
    service.tick()
    assert len(live.started) == 2


def test_new_candidate_cannot_be_hidden_by_an_inconsistent_old_negative_check(setup):
    service, manual, live, _, cutoff = checked_pool(setup)
    service._load()["selection_checks"]["a:SH.600088:5m"]["identities"] = []
    finish_replacement(manual, manual.state["settings"], cutoff)
    service.tick()
    assert len(live.started) == 2
