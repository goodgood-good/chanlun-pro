from copy import deepcopy

import pytest

from chanlun.screening import runner, rules
from chanlun.screening.cache import REPLAY_CHECKS
from tests.test_screening_native_center_signals import _sample


@pytest.fixture
def selection(monkeypatch, tmp_path):
    frame, snapshot, context = _sample(monkeypatch)
    frame["open"] = frame.close
    frame["volume"] = 100.0
    frame.attrs.update(
        price_basis_revision=snapshot["price_basis_revision"],
        structure_price_quantum="1",
    )
    snapshot.update(
        symbol="SH.600000", snapshot_revision="fixture", point_preview_checked=True
    )
    monkeypatch.setattr(runner, "fetch_frame", lambda *_: frame)
    monkeypatch.setattr(
        runner, "frame_quality", lambda *_: {"errors": [], "status": "complete"}
    )
    monkeypatch.setattr(runner, "snapshot_for", lambda *_: deepcopy(snapshot))
    counts = {"audits": 0}

    def audit(*args, **kwargs):
        counts["audits"] += 1
        return rules.audit_native_center_signals(*args, **kwargs)

    monkeypatch.setattr(runner, "audit_native_center_signals", audit)

    def review(_snapshot, _frame, candidates, *_):
        for candidate in candidates:
            candidate["audit"] = dict.fromkeys(REPLAY_CHECKS, True)
        return candidates, []

    monkeypatch.setattr(runner, "_review_native_center_signals", review)
    settings = {
        "frequencies": ["1m"],
        "alert_mode": "native_center_signals",
        "strategy": "direct_structural",
        "point_types": list(rules.POINT_TYPES),
    }
    runs = []

    def scan(**kwargs):
        directory = tmp_path / f"run-{len(runs)}"
        runs.append(directory)
        result = runner.scan_symbol(
            {"code": "SH.600000", "name": "fixture"},
            settings,
            {"1m": context},
            directory,
            cache_root=tmp_path / "cache",
            revision=kwargs.pop("revision", "test"),
            **kwargs,
        )["rows"][0]
        assert not result.get("data_errors"), result
        return result

    return scan, frame, context, settings, counts, runs


def semantic(row):
    return {
        k: v
        for k, v in row.items()
        if k
        not in {
            "processing_timeline",
            "timings_ms",
            "selection_reused",
            "calculation_reused",
            "replays_reused",
            "structure_calculation",
        }
    }


def test_verified_selection_reused_and_evidence_published_in_new_run(selection):
    scan, _, _, settings, counts, runs = selection
    first = scan()
    assert first["selected"] and not first["selection_reused"]
    settings["workers"] = 16
    second = scan()
    assert second["selection_reused"] and counts["audits"] == 1
    assert semantic(second) == semantic(first)
    assert (runs[1] / "evidence/SH.600000_1m.parquet").is_file()
    assert (runs[1] / "evidence/SH.600000_1m.json.gz").is_file()
    second["selected"].clear()
    assert scan()["selected"]


@pytest.mark.parametrize(
    "change",
    [
        "history",
        "volume",
        "basis",
        "window",
        "points",
        "gain",
        "unknown_policy",
        "force",
        "revision",
    ],
)
def test_changed_inputs_policy_and_forced_rebuild_are_rechecked(selection, change):
    scan, frame, context, settings, counts, _ = selection
    scan()
    kwargs = {}
    if change == "history":
        frame.loc[0, "high"] += 1.0
    elif change == "volume":
        frame.loc[0, "volume"] += 1.0
    elif change == "basis":
        frame.attrs["price_basis_revision"] += "-changed"
    elif change == "window":
        context["recent_from"] = "2027-01-01"
    elif change == "points":
        settings["point_types"] = ["1sell"]
    elif change == "gain":
        settings["max_anchor_gain_pct"] = 1.0
    elif change == "unknown_policy":
        settings["future_policy"] = {"changed": True}
    elif change == "force":
        kwargs["force_rebuild"] = True
    elif change == "revision":
        kwargs["revision"] = "next"
    row = scan(**kwargs)
    assert not row["selection_reused"] and counts["audits"] == 2
    if change in {"window", "points"}:
        assert not row["selected"]


def test_missing_original_input_invalidates_selection_cache(selection):
    scan, _, _, _, counts, runs = selection
    scan()
    for original in (runs[0].parent / "cache/inputs").glob("*.parquet"):
        original.unlink()
    assert not scan()["selection_reused"] and counts["audits"] == 2


def test_warning_recheck_publishes_frozen_evidence_even_when_no_candidate_is_selected(selection, monkeypatch):
    scan, _, _, settings, _, runs = selection
    original = runner.snapshot_for
    def empty(*args):
        source = original(*args)
        for level in source['levels']:
            level['points'] = []
        return source
    monkeypatch.setattr(runner, 'snapshot_for', empty)
    settings['_warning_evidence_periods'] = ['1m']
    first = scan()
    assert not first['selected'] and not first['observations'] and first['evidence']
    second = scan()
    assert second['selection_reused'] and second['evidence']
    assert (runs[-1] / 'evidence/SH.600000_1m.parquet').is_file()
