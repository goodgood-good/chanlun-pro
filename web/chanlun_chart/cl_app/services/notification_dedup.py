"""Delivery identity for a structural hypothesis, price pivot and actual state.

Lifecycle counters describe calculation changes. They do not independently
authorize another message about the same pivot and state.
"""
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from .notification_channels import event_channel


def semantic_key(event):
    if event.get('signal_type') not in {'native_point_preview','consolidation_divergence'}:
        return None
    identity=event.get('hypothesis_id') or event.get('preview_key')
    if not isinstance(identity,str) or not identity or not event.get('code') or not event.get('frequency'):
        return None
    anchor=event.get('anchor_at')
    if type(anchor) not in (int,float) or not math.isfinite(anchor) or anchor<=0:
        return None
    try:
        price=Decimal(str(event.get('anchor_price')))
        if not price.is_finite() or price<=0:
            return None
    except (InvalidOperation,ValueError,TypeError):
        return None
    # Retouching the same price can move the engine's last-equal pivot time.
    # That does not create a new price hypothesis within this same leg.
    quantum=event.get('structure_price_quantum')
    if quantum is not None:
        try:
            step=Decimal(str(quantum))
            if step.is_finite() and step>0:
                price=(price/step).to_integral_value()*step
        except (InvalidOperation,ValueError,TypeError):
            pass
    facts=[event_channel(event),event.get('market','a'),event['code'],event['frequency'],event['signal_type'],identity,
           event.get('price_basis_revision'),str(price.normalize()),
           event.get('point_status',event.get('stage')),event.get('selection_status',event.get('stage'))]
    return 'semantic-signal-v1:'+hashlib.sha256(json.dumps(facts,separators=(',',':')).encode()).hexdigest()


def already_sent(db,event):
    key=semantic_key(event)
    return bool(key and db.execute('SELECT 1 FROM markers WHERE id=?',(key,)).fetchone())


def remember_sent(db,events,stamp):
    db.executemany('INSERT OR IGNORE INTO markers VALUES (?,?)',
                   [(key,stamp) for event in events if (key:=semantic_key(event))])


def migrate_sent(db):
    if db.execute("SELECT 1 FROM delivery_settings WHERE key='semantic_signal_dedup_v1'").fetchone():
        return
    for payload,stamp in db.execute("SELECT p.events,COALESCE(o.sent,o.created) FROM outbox o JOIN signal_payloads p ON p.outbox_id=o.id WHERE o.kind='signals' AND o.status='sent'").fetchall():
        try:
            events=json.loads(payload)
            if isinstance(events,list):
                remember_sent(db,[e for e in events if isinstance(e,dict)],stamp)
        except (ValueError,TypeError,KeyError):
            continue
    # Retain prepared images for the page, but retire queued completion copies.
    db.execute("UPDATE outbox SET status='suppressed',error='附图补全仅更新页面，不重复推送' WHERE id LIKE 'chart-complete:%' AND status IN ('pending','retry','sending')")
    db.execute("INSERT OR IGNORE INTO delivery_settings VALUES ('semantic_signal_dedup_v1',1)")
