"""A small durable journal for known provider acceptance before SQLite commit.

The outbox dispatch lock owns both copies. This records responses actually
observed by the sender; it never infers acceptance from an in-flight request.
"""
import hashlib
import json
import math
from pathlib import Path
import re
from uuid import uuid4

from chanlun.screening.runner import write_json

SCHEMA = 'notification-accepted-receipt-v1'
FIELDS = {'id', 'kind', 'created', 'send_started', 'request_started', 'finished',
          'semantic_keys', 'inline', 'destination', 'generation', 'store_id'}
MAX_BYTES = 64 * 1024


class ReceiptJournalError(OSError):
    """An unverified journal must block another send, not silently disappear."""


def paths(database):
    database = Path(database).resolve()
    return (database.with_name(database.name + '.accepted.json'),
            database.with_name(database.name + '.accepted.backup.json'))


def validate(value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or not isinstance(value['id'], str) or not 0 < len(value['id']) <= 512
            or value['kind'] not in {'signals', 'chart', 'screening'}
            or type(value['inline']) is not bool or value['destination'] not in {'holdings', 'others'}
            or type(value['generation']) is not int or not 0 < value['generation'] < 2**53
            or not isinstance(value['store_id'], str) or not re.fullmatch(r'receipt-store-v1:[a-f0-9]{32}', value['store_id'])
            or not isinstance(value['semantic_keys'], list) or len(value['semantic_keys']) > 128
            or any(not isinstance(key, str) or not re.fullmatch(r'semantic-signal-v1:[a-f0-9]{64}', key)
                   for key in value['semantic_keys'])
            or any(type(value[key]) not in (int, float) or not math.isfinite(value[key]) or value[key] < 0
                   for key in ('created', 'send_started', 'request_started', 'finished'))):
        raise ReceiptJournalError('平台成功回执记录格式异常，等待恢复本地发送记录')
    return value


def save(database, value):
    validate(value)
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
    envelope = {'schema': SCHEMA, 'payload': raw, 'sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()}
    for path in paths(database):
        write_json(path, envelope, durable=True)


def load(database):
    damaged = False
    for path in paths(database):
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ReceiptJournalError('平台成功回执记录大小异常')
            envelope = json.loads(path.read_text(encoding='utf-8'))
            raw = envelope['payload']
            if (envelope.get('schema') != SCHEMA or not isinstance(raw, str)
                    or hashlib.sha256(raw.encode('utf-8')).hexdigest() != envelope['sha256']):
                raise ReceiptJournalError('平台成功回执摘要不匹配')
            return validate(json.loads(raw))
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            damaged = True
    if damaged:
        raise ReceiptJournalError('平台成功回执暂不可核验，等待恢复本地发送记录')
    return None


def clear(database):
    for path in paths(database):
        path.unlink(missing_ok=True)


def reserve(db):
    store = db.execute("SELECT value FROM delivery_settings WHERE key='receipt_store_id'").fetchone()
    store_id = store[0] if store else 'receipt-store-v1:' + uuid4().hex
    if not isinstance(store_id, str) or not re.fullmatch(r'receipt-store-v1:[a-f0-9]{32}', store_id):
        raise ReceiptJournalError('通知队列身份异常，等待恢复本地记录')
    if store is None:
        db.execute("INSERT INTO delivery_settings VALUES ('receipt_store_id',?)", (store_id,))
    previous = db.execute("SELECT value FROM delivery_settings WHERE key='request_generation'").fetchone()
    value = previous[0] if previous else 0
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not 0 <= value < 2**53 - 1 or value != int(value)):
        raise ReceiptJournalError('通知发送批次序号异常，等待恢复本地记录')
    generation = int(value) + 1
    db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('request_generation',?)", (generation,))
    return {'generation': generation, 'store_id': store_id}


def check_attempt(db, receipt):
    validate(receipt)
    store = db.execute("SELECT value FROM delivery_settings WHERE key='receipt_store_id'").fetchone()
    generation = db.execute("SELECT value FROM delivery_settings WHERE key='request_generation'").fetchone()
    row = db.execute('SELECT kind,created,send_started,status FROM outbox WHERE id=?', (receipt['id'],)).fetchone()
    if (not store or store[0] != receipt['store_id'] or not generation or generation[0] != receipt['generation']
            or row is None or row['kind'] != receipt['kind'] or row['created'] != receipt['created']
            or row['send_started'] != receipt['send_started']):
        raise ReceiptJournalError('平台成功回执与当前队列身份不符，未再次发送')
    return row


def committed(db, receipt):
    """A peer may have committed this receipt and moved to another attempt."""
    validate(receipt)
    store = db.execute("SELECT value FROM delivery_settings WHERE key='receipt_store_id'").fetchone()
    row = db.execute('SELECT kind,created,send_started,status,sent FROM outbox WHERE id=?', (receipt['id'],)).fetchone()
    return bool(store and store[0] == receipt['store_id'] and row and row['kind'] == receipt['kind']
                and row['created'] == receipt['created'] and row['send_started'] == receipt['send_started']
                and row['status'] == 'sent' and row['sent'] == receipt['finished'])


def apply(db, receipt):
    """Idempotent under the caller's database transaction and dispatch lock."""
    row = check_attempt(db, receipt)
    db.execute("INSERT INTO delivery_settings VALUES ('next_request_at',?) "
               "ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (receipt['finished'] + 4,))
    db.execute("INSERT OR REPLACE INTO delivery_settings VALUES ('request_inflight',0)")
    db.execute('UPDATE delivery_timings SET request_started=?,request_finished=? WHERE outbox_id=?',
               (receipt['request_started'], receipt['finished'], receipt['id']))
    if row['status'] != 'sent':
        db.execute("UPDATE outbox SET status='sent',sent=?,error=CASE WHEN status='expired' "
                   "THEN '关闭前请求已发出，平台已接收' ELSE '' END WHERE id=?", (receipt['finished'], receipt['id']))
    db.executemany('INSERT OR IGNORE INTO markers VALUES (?,?)',
                   [(key, receipt['finished']) for key in receipt['semantic_keys']])
