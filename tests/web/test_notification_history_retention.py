"""Historical page completion requires an actual first-image receipt."""

import base64
import os

import pytest

from chanlun import config
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import PNG, ImageTransport, image_event
from tests.web.test_notification_partial_retry import box_at, rendered
from tests.web.test_notification_history_rendering import history, chart_with_history


def test_frozen_pixels_remain_available_for_the_entire_history_retention(tmp_path):
    from cl_app.services.tradingview_snapshot import PanelImageCache

    clock = [1000.0]
    box = DingTalkOutbox(tmp_path / "outbox.sqlite", now=lambda: clock[0], charts=True)
    cache = PanelImageCache(image_event(), tmp_path / "chart_panel_cache")
    cache.save("proof:5m", "data:image/png;base64," + base64.b64encode(PNG).decode())
    os.utime(cache.root, (1000, 1000))
    clock[0] += 3 * 86400
    box.charts.cleanup()
    assert cache.read()
    clock[0] += 6 * 86400
    box.charts.cleanup()
    assert not cache.read()


def test_unsent_or_expired_first_image_cannot_authorize_history_or_silent_receipt(
    tmp_path,
):
    clock = [1000.0]
    event = {**image_event(), "frequency": "1m"}
    box, transport, calls = box_at(
        tmp_path, clock, event, [rendered("initial", ["1m"])]
    )
    box.charts.process_one()
    assert box.chart_completion_receipt(event) is None
    clock[0] = 2801
    assert not box.enqueue_chart(event, "https://example.test/old.png", complete=True)
    box.charts.process_one()
    assert len(calls) == 1 and not transport.images
    with box.charts._db() as db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "expired"


def test_completion_requires_same_occurrence_and_expires_after_retention(tmp_path):
    clock = [1000.0]
    event = {**image_event(), "frequency": "1m"}
    box, transport, _ = box_at(tmp_path, clock, event, [rendered("initial", ["1m"])])
    box.charts.process_one()
    clock[0] += 5
    box.deliver_one()
    assert box.chart_completion_receipt(event)
    changed = {**event, "source_closed_at": 2000}
    assert box.chart_completion_receipt(changed) is None
    assert not box.enqueue_chart(
        changed, "https://example.test/foreign.png", complete=True
    )
    clock[0] = 1000 + 7 * 86400
    assert box.chart_completion_receipt(event) is None
    assert not box.enqueue_chart(
        event, "https://example.test/expired.png", complete=True
    )
    assert len(transport.images) == 1


def test_later_full_history_replaces_a_silent_partial_view_and_preserves_sent_original(
    tmp_path,
):
    clock = [1000.0]
    event = {**image_event(), "frequency": "1m"}
    box, transport, _ = box_at(tmp_path, clock, event, [rendered("initial", ["1m"])])
    box.charts.process_one()
    clock[0] += 5
    box.deliver_one()
    assert box.enqueue_chart(event, "https://example.test/short.png", complete=True)
    clock[0] = 5000
    assert box.enqueue_chart(event, "https://example.test/full.png", complete=True)
    with box._read_connect() as db:
        completed = db.execute(
            "SELECT content,status,attempts FROM outbox WHERE id LIKE 'chart-complete:%'"
        ).fetchall()
        assert len(completed) == 1 and "full.png" in completed[0][0]
        assert tuple(completed[0][1:]) == ("suppressed", 0)
        assert (
            db.execute(
                "SELECT COUNT(*) FROM outbox WHERE kind='chart' AND status='sent'"
            ).fetchone()[0]
            == 1
        )
    assert len(transport.images) == 1


def test_past_point_state_does_not_revalidate_or_resend_completed_image(tmp_path):
    clock = [1000.0]
    event = {**image_event(), "frequency": "1m"}
    box, transport, _ = box_at(
        tmp_path,
        clock,
        event,
        [rendered("initial", ["1m"]), rendered("complete", ["1m", "5m", "30m"])],
    )
    box.charts.process_one()
    clock[0] += 5
    box.deliver_one()
    clock[0] = 5000

    def forbidden(_):
        pytest.fail("historical evidence does not validate the current point")

    box.signal_validator = forbidden
    box.charts.process_one()
    with box._read_connect() as db:
        row = db.execute(
            "SELECT status,attempts FROM outbox WHERE id LIKE 'chart-complete:%'"
        ).fetchone()
        assert tuple(row) == ("suppressed", 0)
    box.deliver_one()
    box.invalidate_point_notification_policy()
    box.deliver_one()
    assert len(transport.images) == 1


def test_page_only_completion_does_not_depend_on_external_image_fetch(
    tmp_path, monkeypatch
):
    clock = [1000.0]
    event = {**image_event(), "frequency": "1m"}
    box, transport, _ = box_at(
        tmp_path,
        clock,
        event,
        [rendered("initial", ["1m"]), rendered("full", ["1m", "5m", "30m"])],
    )
    box.charts.process_one()
    clock[0] += 5
    box.deliver_one()
    box.charts.check_public = True
    monkeypatch.setattr(
        "requests.get",
        lambda *_a, **_k: pytest.fail("page-only views use the saved local image"),
    )
    clock[0] = 5000
    box.charts.process_one()
    with box.charts._db() as db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "ready"
    assert len(transport.images) == 1


def test_longbridge_upgrade_requeues_only_delivered_retained_history_once(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "usmart")
    clock = [1000.0]
    event = {**image_event(), "market": "us", "code": "QQQ.US", "frequency": "1m"}
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    box.charts.renderer = lambda *_: chart_with_history(
        "original", history("unsupported")
    )
    box.charts.check_public = False
    box.signal_events([event], since=0)
    box.deliver_one()
    box.charts.process_one()
    clock[0] += 5
    box.deliver_one()
    with box.charts._db() as db:
        original = dict(db.execute("SELECT * FROM jobs").fetchone())
        assert original["status"] == "limited"
    clock[0] = 5000
    monkeypatch.setattr(config, "US_HISTORY_KLINE_SOURCE", "longbridge")
    with box.charts._db() as db:
        upgraded = dict(db.execute("SELECT * FROM jobs").fetchone())
        assert upgraded["status"] == "pending"
        assert upgraded["initial_image_hash"] == original["initial_image_hash"]
        assert upgraded["history_coverage"] == original["history_coverage"]
        db.execute("UPDATE jobs SET status='limited'")
    with box.charts._db() as db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "limited"
    assert len(transport.images) == 1
