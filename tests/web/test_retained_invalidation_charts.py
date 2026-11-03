from copy import deepcopy
import json
import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.notification_charts import RenderedChart
from cl_app.services.notification_evidence import NotificationEvidence
from tests.web.test_notification_charts import ImageTransport, image_event, PNG
from tests.web.test_notification_evidence_archive import source


def invalidated():
    return {
        **image_event(),
        "stage": "invalidated",
        "point_status": "invalidated",
        "signal_type": "native_point_preview",
        "change": "changed",
        "lifecycle_cycle": 0,
    }


def test_new_native_invalidation_is_admitted_to_the_chart_queue(tmp_path):
    clock = [1000.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        charts=True,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
    )
    box.signal_events([invalidated()], since=0)
    box.deliver_one()
    with box.charts._db() as db:
        event = json.loads(db.execute("SELECT event FROM jobs").fetchone()[0])
    assert (
        event["stage"] == "invalidated"
        and event["signal_type"] == "native_point_preview"
    )


def test_historical_backfill_requires_actual_sent_text_and_never_pushes_an_old_image(
    tmp_path,
):
    clock = [1000.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        charts=True,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
    )
    event = invalidated()
    box.signal_events([event], since=0)
    box.deliver_one()
    with box.charts._db() as db:
        db.execute("DELETE FROM jobs")  # Simulate the old queue omitting this event.
    clock[0] = 5000
    box.charts.recover_retained_invalidation_views()
    before = deepcopy(transport.messages)
    box.signal_validator = lambda rows: pytest.fail(
        "a historical view must use its frozen evidence"
    )
    coverage = {p: {"state": "target_reached"} for p in ("1m", "5m", "30m")}
    box.charts.renderer = lambda *_: RenderedChart(
        PNG, periods=["1m", "5m", "30m"], history_coverage=coverage
    )
    box.charts.check_public = False
    assert not box.charts.process_one("initial")
    assert box.charts.process_one("history")
    with box.charts._db() as db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "ready"
    with box._read_connect() as db:
        completed = db.execute(
            "SELECT status,attempts FROM outbox WHERE id LIKE 'chart-complete:%'"
        ).fetchone()
        assert tuple(completed) == ("suppressed", 0)
    box.deliver_one()
    assert transport.messages == before and not transport.images


def test_explicit_partial_archive_preserves_available_bytes_and_marks_the_missing_reference(
    tmp_path,
):
    event, paths = source(tmp_path / "runs")
    original, old_paths = source(tmp_path / "runs", run="b" * 32, identity="original")
    event["warning_reference"] = original
    for path in old_paths:
        path.unlink()
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: 1000)
    with pytest.raises(FileNotFoundError):
        archive.capture(event, tmp_path / "runs", 1000)
    identity = archive.capture(event, tmp_path / "runs", 1000, allow_partial=True)
    saved = archive.read(identity)
    assert saved["schema"] == "notification-evidence-v2" and saved["partial_sources"]
    assert saved["evidence"][1]["unavailable"] and saved["evidence"][1]["objects"] == {}
    with archive.materialized_runs(event) as (mounted, _):
        assert any(mounted.glob("*/evidence/*.parquet"))
    assert all(path.exists() for path in paths)


def test_partial_archive_cannot_hide_missing_primary_evidence(tmp_path):
    event, paths = source(tmp_path / "runs")
    for path in paths:
        path.unlink()
    archive = NotificationEvidence(tmp_path / "archive", now=lambda: 1000)
    with pytest.raises(FileNotFoundError):
        archive.capture(event, tmp_path / "runs", 1000, allow_partial=True)
