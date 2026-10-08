import json
import sqlite3
import pytest

from cl_app.services.notification_metrics import summarize
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport, event


def test_latency_uses_actual_receipts_and_preserves_missing_historical_stages():
    detailed = {
        **event("confirmed", stamp=1010),
        "source_closed_at": 1000,
        "notification_timeline": {
            "source_closed_at": 1000,
            "scheduled_at": 1002,
            "calculation_started_at": 1004,
            "fetched_at": 1005,
            "calculation_finished_at": 1008,
            "detected_at": 1010,
        },
    }
    older = {**event("confirmed", identity="old", stamp=1010), "source_closed_at": 1000}
    rows = [
        {
            "kind": "signals",
            "created": 1011,
            "send_started": 1013,
            "sent": 1014,
            "events": json.dumps([detailed, older]),
        },
        {
            "kind": "chart",
            "created": 1020,
            "send_started": 1023,
            "sent": 1024,
            "events": json.dumps([detailed]),
        },
    ]
    result = {row["kind"]: row for row in summarize(rows)}
    measures = result["signals"]["measures"]
    assert (
        result["signals"]["samples"] == 2 and measures["schedule_wait"]["samples"] == 1
    )
    assert measures["schedule_wait"]["average_seconds"] == 2
    assert measures["result_wait"]["average_seconds"] == 2
    assert (
        measures["queue"]["average_seconds"] == 2
        and measures["provider"]["average_seconds"] == 1
    )
    assert measures["cutoff_to_sent"]["p95_seconds"] == 14
    assert result["chart"]["measures"]["cutoff_to_sent"]["average_seconds"] == 24


def test_retry_timing_uses_successful_http_attempt_and_upgrades_old_database(tmp_path):
    path = tmp_path / "outbox.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("""CREATE TABLE outbox(id TEXT PRIMARY KEY,kind TEXT,content TEXT,status TEXT,
            created REAL,expires REAL,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,sent REAL,error TEXT DEFAULT '')""")
    clock = [1000.0]
    transport = Transport()
    box = DingTalkOutbox(path, transport=transport, now=lambda: clock[0])
    row = event("confirmed", stamp=1000)
    row["source_closed_at"] = 990
    box.signal_events([row], since=0)
    transport.error = True
    box.deliver_one()
    assert box.latency_summary()["groups"] == []
    clock[0] = 1020

    def send(text):
        clock[0] += 2
        transport.messages.append(text)

    transport.send = send
    box.deliver_one()
    measures = box.latency_summary()["groups"][0]["measures"]
    assert measures["provider"]["average_seconds"] == 2
    assert measures["queue"]["average_seconds"] == 20
    assert measures["cutoff_to_sent"]["average_seconds"] == 32


def test_clock_regressions_and_nonfinite_measurements_are_not_reported_as_zero():
    row = {
        "kind": "signals",
        "created": 1020,
        "send_started": 1010,
        "sent": 1012,
        "events": json.dumps(
            [{**event("confirmed", stamp=1011), "source_closed_at": float("nan")}]
        ),
    }
    measures = summarize([row])[0]["measures"]
    assert "queue" not in measures and "cutoff_to_sent" not in measures
    assert measures["provider"]["average_seconds"] == 2


def precise_receipt():
    item = {
        **event("confirmed", stamp=1010),
        "source_closed_at": 1000,
        "notification_timeline": {
            "source_closed_at": 1000, "scheduled_at": 1002,
            "calculation_started_at": 1004, "fetched_at": 1005.5,
            "calculation_finished_at": 1009.75, "detected_at": 1010.875,
        },
    }
    return item, {"kind": "signals", "created": 1011,
                  "send_started": 1011.125, "sent": 1011.375}


def test_precise_detection_time_keeps_latency_stages_consistent():
    item, row = precise_receipt()
    measures = summarize([{**row, "events": json.dumps([item])}])[0]["measures"]
    assert measures["detection_to_sent"]["average_seconds"] == 0.5
    assert sum(measures[key]["average_seconds"] for key in (
        "schedule_wait", "worker_wait", "fetch", "compute", "result_wait", "detection_to_sent"
    )) == measures["cutoff_to_sent"]["average_seconds"]


@pytest.mark.parametrize("missing", ["absent", "null"])
def test_legacy_recording_time_is_used_only_when_precise_detection_is_missing(missing):
    item, row = precise_receipt()
    if missing == "absent":
        item["notification_timeline"].pop("detected_at")
    else:
        item["notification_timeline"]["detected_at"] = None
    measures = summarize([{**row, "events": json.dumps([item])}])[0]["measures"]
    assert measures["detection_to_sent"]["average_seconds"] == 1.375
    assert "result_wait" not in measures


@pytest.mark.parametrize("invalid", [float("nan"), True, 1012])
def test_invalid_or_regressed_precise_detection_is_not_masked_by_rounded_time(invalid):
    item, row = precise_receipt()
    item["notification_timeline"]["detected_at"] = invalid
    measures = summarize([{**row, "events": json.dumps([item])}])[0]["measures"]
    assert "detection_to_sent" not in measures
    assert measures["provider"]["average_seconds"] == 0.25
