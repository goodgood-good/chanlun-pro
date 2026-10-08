from types import SimpleNamespace
from unittest.mock import Mock
import threading
import time

import pytest

from cl_app.services import worker_progress
from cl_app.services.signal_monitor import SignalMonitor
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import ImageTransport, image_event, PNG


@pytest.mark.parametrize("worker,limit", [("charts", 180), ("delivery", 60)])
def test_alive_but_stuck_worker_is_unhealthy_and_recovers_after_completion(
    tmp_path, monkeypatch, worker, limit
):
    clock = [100.0]
    monkeypatch.setattr(
        worker_progress, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite", transport=ImageTransport(), charts=True
    )
    service = SignalMonitor(tmp_path, notifications=box)
    service._thread = box._thread = box.charts._thread = Mock()
    service._thread.is_alive.return_value = True
    service._last_cycle_at = time.monotonic()
    service._last_tick_at = time.time()
    target = box.charts if worker == "charts" else box
    with target.progress.cycle("read_queue"):
        clock[0] += limit - 1
        target.progress.phase(
            "chart_render" if worker == "charts" else "provider_request"
        )
        assert service.runtime_health()["ready"]
        clock[0] += 2
        with service._lock, box._lock:
            # Health must not acquire either potentially-held business lock.
            health = service.runtime_health()
        assert (
            health["status"] == "stalled"
            and health["recovery_recommended"]
            and not health["ready"]
        )
        assert health["workers"][worker]["elapsed_seconds"] == limit + 1
    clock[0] += 10000
    assert service.runtime_health()["ready"], (
        "idle time must never be treated as a stuck task"
    )


def test_renderer_loop_reports_the_actual_blocked_phase_without_blocking_probe(
    tmp_path, monkeypatch
):
    clock = [100.0]
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(
        worker_progress, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=ImageTransport(),
        now=lambda: 1000,
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )

    def render(*_):
        entered.set()
        release.wait(5)
        return PNG

    box.charts.renderer = render
    box.charts.check_public = False
    box.charts.enqueue([image_event()], 1000)
    try:
        box.charts.start(lambda: True)
        assert entered.wait(2)
        clock[0] += 181
        assert box.charts.progress.snapshot()["state"] == "stalled"
        assert box.charts.progress.snapshot()["phase"] == "chart_render"
    finally:
        release.set()
        box.charts.stop()


def test_crashed_chart_lease_recovers_with_a_new_worker_without_duplicate_dispatch(
    tmp_path,
):
    clock = [1000.0]
    path = tmp_path / "outbox.sqlite"
    transport = ImageTransport()
    box = DingTalkOutbox(
        path,
        transport=transport,
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    event = {
        **image_event(),
        "signal_type": "consolidation_divergence",
        "point_type": "consolidation_up",
        "stage": "warning",
        "point_status": "warning",
    }
    box.signal_events([event], since=0)
    with box.charts._db() as db:
        db.execute("UPDATE jobs SET status='running',attempts=1,next_try=1060")
    clock[0] += 181
    restored = DingTalkOutbox(
        path,
        transport=transport,
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    restored.charts.renderer = lambda *_: PNG
    restored.charts.check_public = False
    restored.charts.recover()
    restored.charts.process_one()
    restored.deliver_one()
    clock[0] += 5
    restored.charts.recover()
    restored.charts.process_one()
    restored.deliver_one()
    assert len(transport.images) == 1 and restored.snapshot()["pending"] == 0
