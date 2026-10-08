"""Actual dispatch receipts; no estimates for missing historical timestamps."""

from collections import defaultdict
import json
import math


def summarize(rows):
    groups = {}
    for row in rows:
        try:
            events = json.loads(row["events"])
        except (ValueError, TypeError):
            continue
        if not isinstance(events, list):
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = 'combined' if event.get('inline_chart') and row['kind']=='signals' else row['kind']
            key = (event.get("market", "a"), event.get("frequency", ""), kind)
            group = groups.setdefault(key, {"samples": 0, "values": defaultdict(list)})
            group["samples"] += 1
            timeline = event.get("notification_timeline") or {}
            cutoff = timeline.get("source_closed_at", event.get("source_closed_at"))
            # recorded_at is rounded to seconds for lifecycle persistence.
            # The timeline keeps the actual detection instant when available.
            detected = timeline.get("detected_at")
            if detected is None:
                detected = event.get("recorded_at")
            metrics = {
                "cutoff_to_sent": (cutoff, row["sent"]),
                "schedule_wait": (cutoff, timeline.get("scheduled_at")),
                "worker_wait": (
                    timeline.get("scheduled_at"),
                    timeline.get("calculation_started_at"),
                ),
                "fetch": (
                    timeline.get("calculation_started_at"),
                    timeline.get("fetched_at"),
                ),
                "compute": (
                    timeline.get("fetched_at"),
                    timeline.get("calculation_finished_at"),
                ),
                "result_wait": (
                    timeline.get("calculation_finished_at"),
                    timeline.get("detected_at"),
                ),
                "detection_to_sent": (detected, row["sent"]),
                "queue": (row["created"], row["send_started"]),
                "provider": (row["send_started"], row["sent"]),
            }
            for name, (start, end) in metrics.items():
                if (
                    all(
                        isinstance(v, (float, int))
                        and not isinstance(v, bool)
                        and math.isfinite(v)
                        for v in (start, end)
                    )
                    and 0 < start <= end
                ):
                    group["values"][name].append(end - start)
    result = []
    for (market, frequency, kind), group in sorted(groups.items()):
        measures = {}
        for name, values in group["values"].items():
            ordered = sorted(values)
            measures[name] = {
                "samples": len(values),
                "average_seconds": round(sum(values) / len(values), 3),
                "p95_seconds": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 3),
                "max_seconds": round(ordered[-1], 3),
            }
        result.append(
            {
                "market": market,
                "frequency": frequency,
                "kind": kind,
                "samples": group["samples"],
                "measures": measures,
            }
        )
    return result
