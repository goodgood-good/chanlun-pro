"""Bound terminal delivery history even when no provider request is made."""
import math

INTERVAL_SECONDS = 3600


def maintain(db, stamp):
    """Caller owns the outbox transaction and dispatch lock."""
    previous = db.execute("SELECT value FROM delivery_settings WHERE key='history_maintenance_at'").fetchone()
    if previous and type(previous[0]) in (int, float) and math.isfinite(previous[0]):
        if 0 <= stamp - previous[0] < INTERVAL_SECONDS:
            return
    db.execute("DELETE FROM outbox WHERE created<? AND status IN ('sent','failed','expired','suppressed')",
               (stamp - 30 * 86400,))
    for table in ('delivery_decisions', 'signal_payloads', 'inline_charts', 'delivery_timings'):
        db.execute(f'DELETE FROM {table} WHERE outbox_id NOT IN (SELECT id FROM outbox)')
    db.execute('DELETE FROM markers WHERE created<?', (stamp - 90 * 86400,))
    db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('history_maintenance_at',?)", (stamp,))
