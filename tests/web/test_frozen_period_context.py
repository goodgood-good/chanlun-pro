"""Linkage must keep the exact screening input and expose real limitations."""

import json
import time

import pytest

from chanlun.screening import runner
from cl_app.services import higher_context as context
from cl_app.services import screening_workbench as workbench
from tests.web.test_native_period_link import (
    recorded_cache as recorded_cache,
    periods,
    row,
    PERIODS,
)


def frozen_case(recorded_cache):
    path, frame, snapshot, revision = recorded_cache
    root = path.parent / "screening"
    run_id = "c" * 32
    directory = root / run_id
    manifest = runner._save_evidence(directory, "SH.600519", "5m", frame, snapshot)
    (directory / "status.json").write_text(
        json.dumps({"source_revision": revision}), encoding="utf-8"
    )
    key = (
        revision,
        "a",
        "SH.600519",
        "5m",
        snapshot["source_closed_at"],
        "recorded-raw",
        None,
        json.dumps([run_id, manifest]),
    )
    return root, directory, key, frame, snapshot, manifest


def test_frozen_input_wins_over_a_replaced_live_cache(recorded_cache, monkeypatch):
    root, directory, key, frame, snapshot, _ = frozen_case(recorded_cache)
    monkeypatch.setattr(
        context,
        "snapshot_for",
        lambda *_a, **_k: pytest.fail("frozen context must not rebuild"),
    )
    service = context.PeriodContexts(
        [root / "analysis_cache"],
        evidence_root=root,
        loader=lambda *_: pytest.fail("frozen binding must not consult mutable caches"),
    )
    try:
        value = service._load(key, None)
        assert value["value"]["_evidence_source"] == "frozen_run"
        assert value["value"]["source_closed_at"] == snapshot["source_closed_at"]
        assert value["value"]["latest_close"] == float(frame.close.iloc[-1])
    finally:
        service.close()


def test_monitor_run_evidence_is_resolved_in_its_own_known_root(recorded_cache):
    monitor_root, _, key, _, snapshot, _ = frozen_case(recorded_cache)
    manual_root = monitor_root.parent / "manual"
    service = context.PeriodContexts(
        [], evidence_root=manual_root, additional_evidence_roots=(monitor_root,)
    )
    try:
        result = service._load(key, None)
        assert result["value"]["source_closed_at"] == snapshot["source_closed_at"]
        assert result["value"]["_evidence_source"] == "frozen_run"
    finally:
        service.close()


@pytest.mark.parametrize("damage", ["snapshot", "input", "revision", "cutoff"])
def test_damaged_frozen_proof_cannot_fall_back_to_an_unrelated_current_snapshot(
    recorded_cache, damage
):
    root, directory, key, _, _, _ = frozen_case(recorded_cache)
    service = context.PeriodContexts(
        [root / "analysis_cache"],
        evidence_root=root,
        loader=lambda *_: pytest.fail(
            "bad frozen evidence cannot silently switch source"
        ),
    )
    if damage == "snapshot":
        (directory / "evidence/SH.600519_5m.json.gz").write_bytes(b"broken")
    elif damage == "input":
        (directory / "evidence/SH.600519_5m.parquet").write_bytes(b"broken")
    elif damage == "revision":
        (directory / "status.json").write_text(
            json.dumps({"source_revision": "different"}), encoding="utf-8"
        )
    else:
        key = (*key[:4], key[4] - 300, *key[5:])
    try:
        result = service._load(key, None)
        assert result["value"] is None
        assert result["error"] == "frozen_evidence_unavailable"
    finally:
        service.close()


def test_completed_check_with_insufficient_structure_does_not_spin_forever(
    tmp_path, monkeypatch
):
    data = periods()
    data["30m"].update(direction="unknown", center=None, signals=[])
    service = context.PeriodContexts(
        [tmp_path],
        loader=lambda paths, market, code, period, *_: (
            data[period],
            tmp_path / period,
        ),
    )
    monkeypatch.setattr(
        context, "expected_clocks", lambda *_: {p: 100 for p in PERIODS}
    )
    monkeypatch.setattr(context, "_service", lambda *_: service)
    try:
        service.context(row(), "revision")
        deadline = time.monotonic() + 3
        while service.inflight and time.monotonic() < deadline:
            time.sleep(0.01)
        result = context.snapshot(None, "run", "revision", candidates=[row()])
        assert result["status"] == "completed"
        assert result["checked"] == 1 and result["completed"] == 0
        limited = result["rows"]["candidate"]
        assert limited["category"] == "unverified"
        assert limited["periods"][-1]["reason"] == "structure_insufficient"
        assert limited["periods"][-1]["check_state"] == "checked"
    finally:
        service.close()


def test_internal_manifest_references_do_not_enlarge_the_public_list_or_detail(
    recorded_cache,
):
    _, directory, _, _, snapshot, manifest = frozen_case(recorded_cache)
    point = snapshot["levels"][0]["points"][0]
    item = {
        "code": "SH.600519",
        "frequency": "5m",
        "point": point,
        "evidence": manifest,
    }
    internal = workbench._current_rows({"selected": [item]}, directory.name)
    assert internal[0]["_context_evidence"]["manifests"]["5m"] == manifest
    assert (
        "_context_evidence" in workbench._compact_rows(internal, True, context=True)[0]
    )
    for compact in (True, False):
        assert "_context_evidence" not in workbench._compact_rows(internal, compact)[0]
