"""Generate already queued charts without an idle pause between each image."""

import threading

import pytest

from chanlun import config
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import ImageTransport, PNG, image_event


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ALERT_CHART_PUBLIC_BASE_URL", "https://example.test")
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=ImageTransport(),
        now=lambda: 1000.0,
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    box.charts.check_public = False
    box.charts.archive_one = lambda: None
    return box


def next_wait(box, enabled=True):
    done = threading.Event()
    waits = []

    class Wake:
        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, timeout):
            waits.append(timeout)
            box.charts._stop.set()
            done.set()

    box.charts._wake = Wake()
    box.charts.start(lambda: enabled)
    try:
        assert done.wait(5)
    finally:
        box.charts.stop()
    assert not box.charts._thread.is_alive()
    return waits[0]


def test_a_ready_batch_is_rendered_in_order_without_two_second_gaps(box):
    rendered = []
    done = threading.Event()
    waits = []

    class Wake:
        def clear(self):
            pass

        def set(self):
            pass

        def wait(self, timeout):
            # Observe the scheduler's requested delay independently of SQLite
            # and image-file I/O. Completion is after the third image commits.
            waits.append(timeout)
            if len(rendered) == 3:
                box.charts._stop.set()
                done.set()

    def render(row, _root):
        rendered.append(row["id"])
        return PNG

    box.charts.renderer = render
    box.charts.enqueue([image_event(identity=str(i)) for i in range(3)], 1000)
    box.charts._wake = Wake()
    box.charts.start(lambda: True)
    try:
        assert done.wait(5), "chart worker did not finish the queued images"
    finally:
        box.charts.stop()
    assert len(waits) == 3
    assert all(0 <= delay <= 0.1 for delay in waits[:2]), "ready images were given an idle polling interval"
    assert rendered == ["0", "1", "2"]
    with box.charts._db() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM jobs WHERE status='ready'").fetchone()[0]
            == 3
        )
    assert not box.transport.messages and not box.transport.images


def test_a_future_retry_waits_until_its_due_time(box):
    box.charts.enqueue([image_event()], 1000)
    with box.charts._db() as db:
        db.execute("UPDATE jobs SET next_try=1000.75")
    box.charts.renderer = lambda *_: pytest.fail("future chart retried too early")
    assert next_wait(box) == pytest.approx(0.75)


def test_unknown_signal_evidence_keeps_retry_delay_and_attempt_budget(box):
    box.signal_validator = lambda rows: ([], rows)
    box.charts.enqueue([image_event()], 1000)
    box.charts.renderer = lambda *_: pytest.fail("unverified image rendered")
    assert next_wait(box) == 2
    with box.charts._db() as db:
        row = db.execute("SELECT attempts,next_try FROM jobs").fetchone()
        assert row["attempts"] == 0 and row["next_try"] == 1005


def test_failed_render_is_backed_off_instead_of_spinning(box):
    calls = []

    def fail(*_):
        calls.append(True)
        raise ValueError("local fixture failure")

    box.charts.enqueue([image_event()], 1000)
    box.charts.renderer = fail
    assert next_wait(box) == 2
    assert calls == [True]
    with box.charts._db() as db:
        assert db.execute("SELECT next_try FROM jobs").fetchone()[0] == 1030


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_idle_chart_worker_keeps_its_normal_wait(box, enabled):
    if not enabled:
        box.charts.enqueue([image_event()], 1000)
    box.charts.renderer = lambda *_: pytest.fail("disabled or empty queue rendered")
    assert next_wait(box, enabled) == 2
