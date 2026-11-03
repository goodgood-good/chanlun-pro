"""Keep per-event rejection evidence when a receipt is split or retried."""
import json
import math

FIELDS = ('id', 'market', 'code', 'frequency', 'stage', 'point_type', 'signal_type',
          'point_status', 'selection_status', 'lower_confirmation_state', 'timeframe_policy',
          'recorded_at', 'source_closed_at', 'point_confirmed_at', 'selection_available_at',
          'notification_expires_at')


def detail(event, reason):
    def scalar(value):
        return value if value is None or type(value) in (str, int, bool) or (
            type(value) is float and math.isfinite(value)) else None
    return {**{key: scalar(event.get(key)) for key in FIELDS}, 'reason': reason}


def decode(raw):
    try:
        values = json.loads(raw or '[]')
    except (ValueError, TypeError):
        return []
    return [value for value in values if isinstance(value, dict) and isinstance(value.get('reason'), str)] if isinstance(values, list) else []


def clock_rejections(events, stamp):
    from .notification_freshness import notification_rejection
    return [detail(event, reason) for event in events if (reason := notification_rejection(event, stamp))]


def record(db, identity, stamp, reason, rejections):
    """Caller owns the same transaction as the receipt/payload update."""
    saved = db.execute('SELECT details FROM delivery_decisions WHERE outbox_id=?', (identity,)).fetchone()
    merged = {}
    for item in [*decode(saved[0] if saved else None), *rejections]:
        normalized = detail(item, item['reason'])
        key = json.dumps(normalized, sort_keys=True, ensure_ascii=False, allow_nan=False)
        merged.setdefault(key, normalized)
    db.execute('INSERT OR REPLACE INTO delivery_decisions VALUES (?,?,?,?)',
               (identity, stamp, reason, json.dumps(list(merged.values()), ensure_ascii=False, allow_nan=False)))
