"""An idle delivery queue still retires terminal history without losing deduplication."""
import json

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox, signal_key
from tests.web.test_dingtalk_notifications import Transport, event


@pytest.mark.parametrize('terminal', ['sent', 'expired', 'suppressed'])
def test_terminal_history_is_pruned_without_another_provider_request(tmp_path, terminal):
    clock = [1000]
    transport = Transport()
    box = DingTalkOutbox(tmp_path / 'box.sqlite', transport=transport, now=lambda: clock[0])
    original = event()
    box.signal_events([original], since=0)
    if terminal == 'sent':
        box.deliver_one()
    elif terminal == 'suppressed':
        with box._connect() as db:
            db.execute("UPDATE outbox SET status='suppressed'")
    before = len(transport.messages)
    clock[0] += 31 * 86400
    box.deliver_one()
    with box._read_connect() as db:
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM signal_payloads').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM delivery_decisions').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM delivery_timings').fetchone()[0] == 0
        assert db.execute('SELECT 1 FROM markers WHERE id=?', (signal_key(original),)).fetchone()
    assert len(transport.messages) == before
    clock[0] += 60 * 86400
    box.deliver_one()
    with box._read_connect() as db:
        assert db.execute('SELECT COUNT(*) FROM markers').fetchone()[0] == 0


def test_retention_preserves_old_active_receipt_and_its_payload(tmp_path):
    clock = [1000 + 31 * 86400]
    box = DingTalkOutbox(tmp_path / 'box.sqlite', transport=Transport(), now=lambda: clock[0])
    with box._connect() as db:
        db.execute("INSERT INTO outbox(id,kind,content,status,created,expires,next_try) "
                   "VALUES ('active','signals','body','retry',1000,?,?)", (clock[0] + 500, clock[0] + 60))
        db.execute('INSERT INTO signal_payloads VALUES (?,?)', ('active', json.dumps([event(stamp=clock[0])])))
    assert box.deliver_one() == clock[0] + 60
    with box._read_connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == 'retry'
        assert db.execute('SELECT outbox_id FROM signal_payloads').fetchone()[0] == 'active'


def test_maintenance_is_hourly_and_recovers_a_reversed_wall_clock(tmp_path):
    clock = [10000]
    box = DingTalkOutbox(tmp_path / 'box.sqlite', transport=Transport(), now=lambda: clock[0])
    with box._connect():
        pass
    box.deliver_one()
    def last_maintenance():
        with box._read_connect() as db:
            return db.execute("SELECT value FROM delivery_settings WHERE key='history_maintenance_at'").fetchone()[0]
    assert last_maintenance() == 10000
    clock[0] += 3599
    box.deliver_one()
    assert last_maintenance() == 10000
    clock[0] += 1
    box.deliver_one()
    assert last_maintenance() == 13600
    clock[0] = 13000
    box.deliver_one()
    assert last_maintenance() == 13000
