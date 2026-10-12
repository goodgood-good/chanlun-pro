"""Opportunistic merging of unclaimed ordinary point notices, without waiting."""
import json

from .notification_text import event_priority
from .notification_freshness import notification_deadline, notification_is_live


WINDOW_SECONDS = .250
MAX_CANDIDATES = 32
MAX_EVENTS = 5
MAX_CONTENT_BYTES = 3300
BATCH_PREFIX = 'signals:batch:'


def bucket(event):
    if (event.get('inline_chart') or event.get('signal_type') in ('holding_risk', 'consolidation_divergence')
            or event.get('point_type') not in ('1buy', '2buy', '3buy', '1sell', '2sell', '3sell')
            or event_priority([event]) != 0):
        return None
    return event.get('market'), event.get('frequency'), event.get('stage'), 0


def append_pending(db, event, stamp, render):
    """Caller owns a write transaction covering payload and event markers.

    The first receipt id, creation time and due time never move. Once claimed,
    retried, full, or older than 250ms, a receipt is permanently closed to joins.
    """
    identity = bucket(event)
    if identity is None:
        return None
    rows = db.execute(
        "SELECT o.id,o.expires,p.events FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id "
        "WHERE o.kind='signals' AND o.status='pending' AND o.attempts=0 AND o.send_started IS NULL "
        "AND o.id LIKE 'signals:batch:%' AND o.created>=? AND o.created<=? AND o.expires>? "
        "ORDER BY o.created DESC,o.rowid DESC LIMIT ?", (stamp - WINDOW_SECONDS, stamp, stamp, MAX_CANDIDATES)).fetchall()
    for row in rows:
        try:
            events = json.loads(row['events'])
            if (not isinstance(events, list) or not 0 < len(events) < MAX_EVENTS
                    or any(not isinstance(item, dict) or bucket(item) != identity
                           or not notification_is_live(item, stamp) for item in events)):
                continue
            combined = [*events, event]
            content = render(combined, stamp)
            if len(content.encode('utf-8')) > MAX_CONTENT_BYTES:
                continue
            payload = json.dumps(combined, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError, KeyError):
            continue
        changed = db.execute(
            "UPDATE outbox SET content=?,expires=? WHERE id=? AND status='pending' AND attempts=0 "
            "AND send_started IS NULL", (content, max(notification_deadline(item) for item in combined), row['id'])).rowcount
        if changed:
            db.execute('UPDATE signal_payloads SET events=? WHERE outbox_id=?', (payload, row['id']))
            return row['id']
    return None
