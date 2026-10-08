"""Consolidation messages are one frozen card, including withdrawal evidence."""

from copy import deepcopy
import json

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import ImageTransport, PNG
from tests.web.test_notification_content import warning


def alert(stage="warning", cycle=0, stamp=1000):
    return {
        **warning(),
        "stage": stage,
        "point_status": stage,
        "selection_status": stage,
        "recorded_at": stamp,
        "source_closed_at": stamp,
        "change": "changed",
        "available_at": stamp,
        "lifecycle_cycle": cycle,
        "warning_reactivated": cycle > 0,
        "evidence": {"status": "complete"},
        "evidence_run_id": "a" * 32,
        "invalidation_reason": "同向MACD柱在收缩后重新扩大",
    }


@pytest.fixture
def combined(tmp_path):
    clock = [1000.0]
    transport = ImageTransport()
    box = DingTalkOutbox(
        tmp_path / "outbox.sqlite",
        transport=transport,
        now=lambda: clock[0],
        signal_validator=lambda rows: (rows, []),
        charts=True,
    )
    rendered = []
    box.charts.renderer = lambda event, _: rendered.append(deepcopy(event)) or PNG
    box.charts.check_public = False
    return box, transport, clock, rendered


@pytest.mark.parametrize(
    "stage,cycle,label",
    [
        ("warning", 0, "提前预警"),
        ("warning", 1, "预警恢复"),
        ("confirmed", 0, "已确认"),
        ("invalidated", 0, "预警失效"),
    ],
)
def test_each_consolidation_stage_delivers_one_card_with_text_and_png(
    combined, stage, cycle, label
):
    box, transport, clock, rendered = combined
    row = alert(stage, cycle)
    box.signal_events([row], since=0)
    box.deliver_one()
    assert not transport.messages and not transport.images
    box.charts.process_one()
    box.deliver_one()
    assert len(transport.images) == 1 and not transport.messages and len(rendered) == 1
    title, body = transport.images[0]
    assert label in title and label in body and "![信号快照]" in body
    assert "行情截止" in body and "[查看" not in body
    if stage == "invalidated":
        assert "同向MACD柱在收缩后重新扩大" in body and "MACD 同向柱面积比" not in body
    else:
        assert "MACD 同向柱面积比" in body
    assert box.snapshot()["sent"] == 1 and box.snapshot()["chart_delivery"]["sent"] == 1
    box.charts.recover()
    box.charts.process_one()
    clock[0] += 5
    box.signal_events([row], since=0)
    box.deliver_one()
    assert len(transport.images) == 1 and len(rendered) == 1
    assert box.latency_summary()["groups"][0]["kind"] == "combined"


def test_snapshot_is_not_replaced_when_current_signal_has_newer_candles(combined):
    box, transport, clock, rendered = combined
    box.signal_validator = lambda rows: (
        [{**e, "source_closed_at": 2000, "evidence_run_id": "b" * 32} for e in rows],
        [],
    )
    box.signal_events([alert()], since=0)
    box.charts.process_one()
    box.deliver_one()
    with box._connect() as db:
        event = json.loads(
            db.execute("SELECT events FROM signal_payloads").fetchone()[0]
        )[0]
    assert event["source_closed_at"] == 1000 and event["evidence_run_id"] == "a" * 32
    assert rendered[0]["source_closed_at"] == 1000 and len(transport.images) == 1


def test_network_retry_reuses_the_same_image_and_never_sends_a_text_copy(combined):
    from cl_app.services.dingtalk_notifications import DeliveryError

    box, transport, clock, rendered = combined
    original = transport.send_markdown
    transport.send_markdown = lambda *_: (_ for _ in ()).throw(
        DeliveryError("temporary network error")
    )
    box.signal_events([alert()], since=0)
    box.charts.process_one()
    box.deliver_one()
    assert box.snapshot()["pending"] == 1 and not transport.messages
    clock[0] += 16
    transport.send_markdown = original
    box.charts.recover()
    box.charts.process_one()
    box.deliver_one()
    assert len(transport.images) == 1 and len(rendered) == 1 and not transport.messages


def test_pending_card_recovers_image_generation_before_any_text_was_sent(
    combined, monkeypatch
):
    box, transport, clock, rendered = combined
    with monkeypatch.context() as patch:
        patch.setattr(
            box.charts,
            "enqueue",
            lambda *_: (_ for _ in ()).throw(OSError("interrupted enqueue")),
        )
        box.signal_events([alert()], since=0)
    clock[0] += 10
    restarted = DingTalkOutbox(
        box.path,
        transport=transport,
        now=lambda: clock[0],
        charts=True,
        signal_validator=lambda rows: (rows, []),
    )
    restarted.charts.renderer = lambda *_: PNG
    restarted.charts.check_public = False
    restarted.charts.recover()
    restarted.charts.process_one()
    restarted.deliver_one()
    assert len(transport.images) == 1 and not transport.messages


def test_unavailable_image_configuration_is_reported_without_an_endless_wait(
    combined, monkeypatch
):
    box, transport, _, _ = combined
    monkeypatch.setattr("cl_app.services.notification_charts.public_base", lambda: "")
    box.signal_events([alert()], since=0)
    assert box.snapshot()["failed"] == 1 and box.snapshot()["pending"] == 0
    assert "图片地址" in box.snapshot()["last_error"]
    assert not transport.images and not transport.messages


def test_unknown_evidence_does_not_exhaust_attempts_and_withdrawn_cards_never_send(
    combined,
):
    box, transport, clock, rendered = combined
    box.signal_events([alert()], since=0)
    box.signal_validator = lambda rows: ([], rows)
    for _ in range(4):
        box.deliver_one()
        box.charts.process_one()
        clock[0] += 6
    assert not transport.messages and not transport.images and not rendered
    assert box.snapshot()["history"][0]["attempts"] == 0
    box.signal_validator = lambda rows: ([], [])
    box.charts.process_one()
    box.deliver_one()
    assert box.snapshot()["pending"] == 0 and not transport.images


def test_failed_render_is_explicit_and_does_not_send_an_unillustrated_card(combined):
    box, transport, clock, _ = combined
    box.charts.renderer = lambda *_: (_ for _ in ()).throw(
        ValueError("invalid evidence")
    )
    box.signal_events([alert()], since=0)
    for _ in range(3):
        box.charts.process_one()
        clock[0] += 31
    box.deliver_one()
    assert box.snapshot()["failed"] == 1 and box.snapshot()["pending"] == 0
    assert not transport.images and not transport.messages
    assert "冻结证据绘图失败" in box.snapshot()["last_error"]


def test_switch_change_during_render_cannot_reactivate_a_card(combined):
    box, transport, clock, _ = combined

    def render(*_):
        clock[0] += 1
        box.begin_window(clock[0])
        return PNG

    box.charts.renderer = render
    box.signal_events([alert()], since=0)
    box.charts.process_one()
    box.deliver_one()
    assert (
        box.snapshot()["pending"] == 0
        and not transport.images
        and not transport.messages
    )


def test_both_invalidation_and_previous_warning_evidence_remain_referenced(combined):
    box, *_ = combined
    row = alert("invalidated")
    row["invalidation_reference"] = {**alert(), "evidence_run_id": "b" * 32}
    box.signal_events([row], since=0)
    assert box.referenced_runs() == {"a" * 32, "b" * 32}


def test_invalidation_renderer_verifies_both_snapshots_and_uses_the_check_candle(
    monkeypatch,
):
    from cl_app.services.notification_charts import load_chart_evidence
    from cl_app.services.signal_monitor import (
        _consolidation_signal,
        _invalidation_evidence,
    )
    from tests.core.strict_structure.test_native_consolidation_preview import (
        sample,
        build,
        snapshot,
    )

    parts = sample()
    frame = parts[3]
    previous_snapshot = snapshot(parts, build(parts))
    previous_snapshot["native_consolidation_alerts"] = build(parts)
    source_alert = previous_snapshot["native_consolidation_alerts"][0]
    source_alert["anchor_price"] = source_alert["anchor_tick"] * float(
        previous_snapshot["structure_price_quantum"]
    )
    previous = {
        **_consolidation_signal(
            {
                "market": "a",
                "code": "SH.600000",
                "frequency": "5m",
                "source_closed_at": previous_snapshot["source_closed_at"],
                "alert": source_alert,
            }
        ),
        "evidence": {"phase": "before"},
        "evidence_run_id": "a" * 32,
    }
    cutoff = previous_snapshot["source_closed_at"]
    current = deepcopy(previous_snapshot)
    current["native_consolidation_alerts"] = []
    current["native_consolidation_checks"] = {
        "schema": "native-preview-checks-v1",
        "source_closed_at": cutoff,
        "price_basis_revision": current["price_basis_revision"],
        "tail_start_at": previous["terminal_start_at"],
        "reason_code": "MOMENTUM_RECOVERED",
        "evaluations": {},
    }
    event = {
        **previous,
        **_invalidation_evidence(
            previous,
            {
                "source_closed_at": cutoff,
                "consolidation_checks": current["native_consolidation_checks"],
            },
        ),
        "stage": "invalidated",
        "point_status": "invalidated",
        "invalidation_reference": previous,
        "evidence": {"phase": "after"},
        "evidence_run_id": "b" * 32,
    }
    reads = []

    def load(directory, code, frequency, manifest):
        reads.append(manifest["phase"])
        return (
            frame,
            (previous_snapshot if manifest["phase"] == "before" else current),
            {"verified": True},
        )

    monkeypatch.setattr("chanlun.screening.evidence.load_evidence", load)
    _, _, marker = load_chart_evidence(event, ".")
    assert set(reads) == {"before", "after"} and marker["anchor_at"] == cutoff
    assert marker["anchor_price"] == float(frame.close.iloc[-1])
    with pytest.raises(ValueError, match="reason disagrees"):
        load_chart_evidence(
            {**event, "invalidation_reason_code": "PRICE_EXTREME_BROKEN"}, "."
        )
    with pytest.raises(ValueError, match="reference"):
        load_chart_evidence({**event, "invalidation_reference": None}, ".")
    with pytest.raises(ValueError, match="another cutoff"):
        load_chart_evidence({**event, "invalidation_checked_at": cutoff + 1}, ".")
