"""Actual dispatch receipts; no estimates for missing historical timestamps.

Receipt-local counters belong to one outbox id. A deferred subset gets a new
receipt and does not inherit the parent batch's earlier verification timings.
"""

from collections import defaultdict
import json
import math
from chanlun.screening.worker_transport import TIMING_SCOPE


def summarize(rows):
    groups = {}
    for row in rows:
        def measured(name):
            try:
                return row[name]
            except (KeyError, IndexError):
                return None
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
            if not isinstance(timeline, dict):
                timeline = {}
            cutoff = event.get("source_closed_at", timeline.get("source_closed_at"))
            task_cutoff = timeline.get("source_closed_at", cutoff)
            # recorded_at is rounded to seconds for lifecycle persistence.
            # The timeline keeps the actual detection instant when available.
            detected = timeline.get("detected_at")
            if detected is None:
                detected = event.get("recorded_at")
            primary_finished = (timeline.get('primary_calculation_finished_at')
                                if timeline.get('measurement_scope') == TIMING_SCOPE
                                else timeline.get('calculation_finished_at'))
            metrics = {
                "cutoff_to_sent": (cutoff, row["sent"]),
                "schedule_wait": (cutoff, timeline.get("scheduled_at")),
                "task_cutoff_to_sent": (task_cutoff, row["sent"]),
                "task_schedule_wait": (task_cutoff, timeline.get("scheduled_at")),
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
                    primary_finished,
                ),
                "result_wait": (
                    timeline.get("calculation_finished_at"),
                    timeline.get("detected_at"),
                ),
                "detection_to_sent": (detected, row["sent"]),
                "queue": (row["created"], row["send_started"]),
                "provider": (row["send_started"], row["sent"]),
                "queue_before_first_claim": (row['created'], measured('first_claimed')),
                "validation": (measured('validation_started'), measured('validation_finished')),
                "send_preparation": (measured('validation_finished'), measured('request_started')),
                "request": (measured('request_started'), measured('request_finished')),
            }
            if timeline.get('measurement_scope') == TIMING_SCOPE:
                metrics['coordination'] = (primary_finished, timeline.get('calculation_finished_at'))
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
            total = measured('validation_seconds')
            attempts = measured('validation_attempts')
            if (type(total) in (int, float) and math.isfinite(total) and total >= 0
                    and type(attempts) is int and attempts > 0):
                group['values']['validation_total'].append(total)
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
                "receipt_local_measures": [name for name in measures if name in (
                    'queue', 'provider', 'queue_before_first_claim', 'validation',
                    'validation_total', 'send_preparation', 'request')],
                "measures": measures,
            }
        )
    return result
