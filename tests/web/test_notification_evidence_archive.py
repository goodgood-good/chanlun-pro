from copy import deepcopy
import hashlib
import json
from unittest.mock import Mock

import pytest

from cl_app.services.notification_evidence import NotificationEvidence
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from chanlun.screening.evidence import evidence_paths
from tests.web.test_notification_charts import ImageTransport, image_event, PNG
from tests.test_holding_consolidation_pipeline import holding_run as holding_run


def source(root, run="a" * 32, identity="point", stamp=1000):
    directory = root / run
    paths = evidence_paths(directory, "SH.600088", "5m")
    paths[0].parent.mkdir(parents=True, exist_ok=True)
    paths[0].write_bytes(b"frozen input " + run.encode())
    paths[1].write_bytes(b"frozen snapshot " + run.encode())
    (directory / "status.json").write_text(
        json.dumps({"run_id": run, "source_revision": "f" * 64}), encoding="utf-8"
    )
    manifest = {"status": "complete"}
    for path, kind in zip(paths, ("parquet", "snapshot")):
        manifest[kind + "_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest[kind + "_bytes"] = path.stat().st_size
    return {
        **image_event(stamp=stamp, identity=identity),
        "evidence": manifest,
        "evidence_run_id": run,
    }, paths


def test_archived_evidence_survives_source_pruning_and_keeps_its_original_algorithm(
    tmp_path,
):
    clock = [1000.0]
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: clock[0])
    event, paths = source(tmp_path / "runs")
    versions = [(p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths]
    identity = archive.capture(event, tmp_path / "runs", 1000)
    assert [(p.stat().st_mtime_ns, p.stat().st_ctime_ns) for p in paths] == versions
    for path in paths:
        path.unlink()
    record = archive.read(identity)
    assert (
        record["event"] == event
        and record["evidence"][0]["algorithm_revision"] == "f" * 64
    )
    assert archive.capture(event, tmp_path / "runs", 1000) == identity


def test_content_objects_are_shared_and_expire_only_after_their_last_event(tmp_path):
    clock = [1000.0]
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: clock[0])
    event, _ = source(tmp_path / "runs")
    first = archive.capture(event, tmp_path / "runs", 1000)
    clock[0] += 10
    second = archive.capture({**event, "id": "second"}, tmp_path / "runs", 1010)
    assert len(list((archive.root / "objects").iterdir())) == 2
    clock[0] = 1001 + archive.lifetime
    archive.cleanup()
    with pytest.raises((FileNotFoundError, ValueError)):
        archive.read(first)
    assert archive.read(second)["event"]["id"] == "second"
    clock[0] = 1011 + archive.lifetime
    archive.cleanup()
    assert not list((archive.root / "objects").iterdir())


def test_later_source_changes_cannot_modify_the_archived_bytes(tmp_path):
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: 1000)
    event, paths = source(tmp_path / "runs")
    identity = archive.capture(event, tmp_path / "runs", 1000)
    paths[0].write_bytes(b"changed source")
    assert archive.read(identity)["event"] == event
    obj = (
        archive.root
        / "objects"
        / archive.read(identity)["evidence"][0]["objects"]["parquet"]
    )
    obj.write_bytes(b"corrupt archive")
    with pytest.raises(ValueError, match="checksum"):
        archive.read(identity)


def test_invalidation_archives_both_frozen_sources_without_rewriting_the_reference(
    tmp_path,
):
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: 1000)
    old, _ = source(tmp_path / "runs", run="a" * 32, identity="warning")
    current, _ = source(tmp_path / "runs", run="b" * 32, identity="invalidated")
    current["invalidation_reference"] = old
    identity = archive.capture(current, tmp_path / "runs", 1000)
    record = archive.read(identity)
    assert [p["source"]["evidence_run_id"] for p in record["evidence"]] == [
        "b" * 32,
        "a" * 32,
    ]
    assert len(list((archive.root / "objects").iterdir())) == 4


def test_wrong_checksum_and_outside_source_are_rejected_before_publication(tmp_path):
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: 1000)
    event, _ = source(tmp_path / "runs")
    bad = deepcopy(event)
    bad["evidence"]["parquet_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        archive.capture(bad, tmp_path / "runs", 1000)
    assert not list((archive.root / "events").glob("*.json"))
    bad["evidence_run_id"] = "../outside"
    with pytest.raises(ValueError, match="run"):
        archive.capture(bad, tmp_path / "runs", 1000)


def test_delivery_continues_during_archive_failure_and_pending_sources_remain_protected(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    event, _ = source(tmp_path / "runs")
    box.charts.renderer = lambda *_: PNG
    box.charts.check_public = False
    box.signal_events([event], since=0)
    box.deliver_one()
    box.charts.process_one()
    original = box.charts.archive.capture
    with monkeypatch.context() as fault:
        fault.setattr(
            box.charts.archive,
            "capture",
            Mock(side_effect=OSError("isolated archive failure")),
        )
        box.charts.archive_one()
        clock[0] += 5
        box.deliver_one()
        assert len(transport.messages) == 1 and len(transport.images) == 1
        clock[0] += 1801
        assert event["evidence_run_id"] in box.referenced_runs()
        assert box.charts.archive_snapshot()["pending"] == 1
    box.charts.archive.capture = original
    box.charts.archive_one()
    assert box.charts.archive_snapshot()["saved"] == 1
    assert event["evidence_run_id"] not in box.referenced_runs()


def test_immutable_archive_corruption_stops_cleanup_without_deleting_objects(tmp_path):
    clock = [1000.0]
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: clock[0])
    event, _ = source(tmp_path / "runs")
    identity = archive.capture(event, tmp_path / "runs", 1000)
    (archive.root / "events" / (identity + ".json")).write_bytes(b"{broken")
    clock[0] += archive.lifetime + 1
    with pytest.raises(ValueError):
        archive.cleanup()
    assert len(list((archive.root / "objects").iterdir())) == 2


def test_restart_recovers_a_published_archive_before_its_queue_acknowledgement(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", now=lambda: clock[0], charts=True)
    event, paths = source(tmp_path / "runs")
    box.charts.enqueue([event], clock[0])
    capture = box.charts.archive.capture

    def interrupted(*args):
        capture(*args)
        raise OSError("interrupted after publishing evidence")

    monkeypatch.setattr(box.charts.archive, "capture", interrupted)
    box.charts.archive_one()
    for path in paths:
        path.unlink()
    clock[0] += 31
    restarted = DingTalkOutbox(
        tmp_path / "outbox.sqlite", now=lambda: clock[0], charts=True
    )
    restarted.charts.archive_one()
    assert restarted.charts.archive_snapshot()["saved"] == 1
    with restarted.charts._db() as db:
        assert (
            db.execute("SELECT attempts FROM evidence_archive_jobs").fetchone()[0] == 2
        )
    assert len(list((restarted.charts.archive.root / "events").glob("*.json"))) == 1


def test_archive_retry_limit_releases_original_runs_after_three_failures(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", now=lambda: clock[0], charts=True)
    event, _ = source(tmp_path / "runs")
    box.charts.enqueue([event], clock[0])
    failure = Mock(side_effect=OSError("archive storage unavailable"))
    monkeypatch.setattr(box.charts.archive, "capture", failure)
    for delay in (0, 31, 61):
        clock[0] += delay
        box.charts.archive_one()
    for _ in range(3):
        clock[0] += 100
        box.charts.archive_one()
    assert failure.call_count == 3
    snapshot = box.charts.archive_snapshot()
    assert snapshot["unavailable"] == 1 and snapshot["pending"] == 0
    assert not box.charts.archive_referenced_runs()


def test_rollout_excludes_old_jobs_but_restart_keeps_new_pending_jobs(tmp_path):
    clock = [1000.0]
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", now=lambda: clock[0], charts=True)
    old, _ = source(tmp_path / "runs", identity="old")
    box.charts.enqueue([old], clock[0])
    with box.charts._db() as db:
        db.execute("DROP TABLE evidence_archive_jobs")
        db.execute("DELETE FROM settings WHERE k='archive_enabled_at'")
    clock[0] = 2000
    upgraded = DingTalkOutbox(
        tmp_path / "outbox.sqlite", now=lambda: clock[0], charts=True
    )
    new, _ = source(tmp_path / "runs", identity="new", stamp=2000)
    upgraded.charts.enqueue([new], clock[0])
    upgraded.charts.begin_window(clock[0] + 10)
    clock[0] = 2020
    restarted = DingTalkOutbox(
        tmp_path / "outbox.sqlite", now=lambda: clock[0], charts=True
    )
    assert restarted.charts.archive_snapshot()["pending"] == 1
    restarted.charts.archive_one()
    assert restarted.charts.archive_snapshot()["saved"] == 1
    records = list((restarted.charts.archive.root / "events").glob("*.json"))
    assert (
        len(records) == 1
        and json.loads(records[0].read_text(encoding="utf-8"))["event"]["id"] == "new"
    )


def test_missing_object_reference_cannot_silently_delete_retained_evidence(tmp_path):
    clock = [1000.0]
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: clock[0])
    event, _ = source(tmp_path / "runs")
    identity = archive.capture(event, tmp_path / "runs", clock[0])
    descriptor = archive.root / "events" / (identity + ".json")
    data = json.loads(descriptor.read_text(encoding="utf-8"))
    data["evidence"] = []
    descriptor.write_text(json.dumps(data), encoding="utf-8")
    clock[0] += 3601
    with pytest.raises(ValueError, match="incomplete"):
        archive.cleanup()
    assert len(list((archive.root / "objects").iterdir())) == 2


def test_recorded_consolidation_proof_remains_replayable_after_source_files_are_removed(
    holding_run, tmp_path
):
    import shutil
    import pandas as pd
    from chanlun.screening.cache import input_fingerprint
    from cl_app.services.notification_charts import load_chart_evidence
    from cl_app.services.signal_monitor import _consolidation_signal

    _, directory, record, _, _, _, _ = holding_run
    row = record["rows"][0]
    candidate = next(
        c for c in row["consolidation_alerts"] if c["alert"]["status"] == "confirmed"
    )
    event = {
        **_consolidation_signal({**row, **candidate}),
        "evidence_run_id": directory.name,
    }
    before_frame, before_snapshot, before_signal = load_chart_evidence(
        event, directory.parent
    )
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: 1000)
    identity = archive.capture(event, directory.parent, 1000)
    for path in evidence_paths(directory, row["code"], row["frequency"]):
        path.unlink()
    saved = archive.read(identity)
    assert saved["evidence"][0]["algorithm_revision"] is None
    replay_root = tmp_path / "isolated_replay"
    paths = evidence_paths(replay_root / directory.name, row["code"], row["frequency"])
    paths[0].parent.mkdir(parents=True)
    for target, kind in zip(paths, ("parquet", "snapshot")):
        shutil.copyfile(
            archive.root / "objects" / saved["evidence"][0]["objects"][kind], target
        )
    frame, snapshot, signal = load_chart_evidence(saved["event"], replay_root)
    pd.testing.assert_frame_equal(before_frame, frame)
    assert snapshot == before_snapshot and signal == before_signal
    assert (
        input_fingerprint(frame, row["code"], row["frequency"])
        == row["evidence"]["input_fingerprint"]
    )


def test_crash_leftovers_expire_without_deleting_recent_writes_or_unrelated_files(
    tmp_path,
):
    import os

    archive = NotificationEvidence(tmp_path / "archive", now=lambda: 5000)
    objects = archive.root / "objects"
    objects.mkdir(parents=True)
    old = objects / ("." + "a" * 32 + ".tmp")
    recent = objects / ("." + "b" * 32 + ".tmp")
    unrelated = objects / "manual-note.tmp"
    for path, stamp in ((old, 1000), (recent, 4999), (unrelated, 1000)):
        path.write_bytes(b"partial")
        os.utime(path, (stamp, stamp))
    archive.cleanup()
    assert not old.exists() and recent.exists() and unrelated.exists()


def test_cleanup_and_archive_errors_clear_independently_after_idle_recovery(
    tmp_path, monkeypatch
):
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", now=lambda: 1000, charts=True)
    with monkeypatch.context() as fault:
        fault.setattr(
            box.charts.archive, "cleanup", Mock(side_effect=OSError("storage busy"))
        )
        box.charts.cleanup()
        box.charts._archive_error = "archive queue unavailable"
        box.charts.archive_one()
        assert not box.charts._archive_error
        assert box.charts.archive_snapshot()["error"] == "通知证据清理暂不可用"
    box.charts.cleanup()
    assert not box.charts.archive_snapshot()["error"]
