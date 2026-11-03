"""Reuse a completed native selection only after fresh full-input validation."""
from copy import deepcopy
import hashlib
import json


_TRANSIENT = frozenset({
    "code", "market", "name", "frequency", "bars", "source_closed_at",
    "data_errors", "data_quality", "processing_timeline", "timings_ms",
    "evidence", "calculation_reused", "structure_calculation", "replays_reused",
    "selection_reused", "consolidation_checked", "consolidation_error",
    'closing_bar_refresh',
    'history_refresh',
    'period_timings_ms',
})


def selection_key(settings, context, quality):
    # Unknown settings remain significant. Only execution/universe controls
    # are excluded; context already pins this symbol, period and calendar.
    policy = {k: v for k, v in settings.items()
              if k not in {"workers", "scope", "codes", "symbols", "frequencies", "watch_groups"}}
    try:
        value = json.dumps({"policy": policy, "context": {k:v for k,v in context.items() if k != '_screening_tail_prepared'}, "quality": quality},
                           sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(value.encode()).hexdigest()


def saved_selection(row, key):
    if key is None or row.get("data_errors") or row.get("error"):
        return None
    return {"schema": "screening-selection-v1", "key": key,
            "needs_evidence": bool(row.get("evidence")),
            "row": {k: v for k, v in row.items() if k not in _TRANSIENT}}


def read_selection(calculation, key):
    # The enclosing calculation has already authenticated both its entire
    # payload and original input, and matched the current full-input hash.
    value = calculation.get("selection") if calculation and key is not None else None
    if (not isinstance(value, dict) or value.get("schema") != "screening-selection-v1"
            or value.get("key") != key or type(value.get("needs_evidence")) is not bool
            or not isinstance(value.get("row"), dict)):
        return None
    row = value["row"]
    if (set(row) & _TRANSIENT or row.get("error")
            or any(not isinstance(row.get(k), list) for k in ("selected", "observations", "recent_rejections"))
            or not isinstance(row.get("reason_counts"), dict)):
        return None
    return deepcopy(value)
