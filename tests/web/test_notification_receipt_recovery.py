"""A known successful provider response must survive a local receipt failure."""
from contextlib import contextmanager
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
from unittest.mock import Mock

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_dingtalk_notifications import Transport, event
from tests.web.test_notification_charts import ImageTransport, image_event
from cl_app.services import notification_receipts as journal


def test_accepted_notification_is_not_resent_after_receipt_rollback_and_restart(tmp_path, monkeypatch):
    clock = [1000.]
    transport = Transport()
    path = tmp_path / 'queue.sqlite'
    sender = DingTalkOutbox(path, transport=transport, now=lambda: clock[0],
                           signal_validator=lambda rows: (rows, []))
    sender.signal_events([event('confirmed')], since=0)
    connect = sender._connect

    @contextmanager
    def fail_receipt(**kwargs):
        with connect(**kwargs) as db:
            yield db
            if transport.messages:
                raise OSError('receipt commit failed after successful response')

    with monkeypatch.context() as fault:
        fault.setattr(sender, '_connect', fail_receipt)
        with pytest.raises(OSError, match='receipt commit failed'):
            sender.deliver_one()
    assert len(transport.messages) == 1
    restored = DingTalkOutbox(path, transport=transport, now=lambda: clock[0],
                             signal_validator=lambda rows: (rows, []))
    restored.deliver_one()
    clock[0] += 35
    restored.deliver_one()
    assert len(transport.messages) == 1
    assert restored.snapshot()['sent'] == 1 and restored.snapshot()['pending'] == 0


def test_real_database_lock_after_acceptance_does_not_prevent_journal_persistence(tmp_path, monkeypatch):
    clock, transport, locks = [1000.], Transport(), []
    path = tmp_path / 'queue.sqlite'
    sender = DingTalkOutbox(path, transport=transport, now=lambda: clock[0])
    sender.signal_events([event('confirmed')], since=0)
    send = transport.send
    connect = sqlite3.connect
    def accept_and_lock(text):
        send(text)
        writer = connect(path, timeout=.1)
        writer.execute('BEGIN EXCLUSIVE')
        locks.append(writer)
    monkeypatch.setattr(transport, 'send', accept_and_lock)
    with monkeypatch.context() as fault:
        fault.setattr(sqlite3, 'connect', lambda *args, **kwargs: connect(*args, **{**kwargs, 'timeout': .02}))
        try:
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                sender.deliver_one()
            assert all(journal_path.exists() for journal_path in journal.paths(path))
            assert journal.load(path)['finished'] == 1000.
        finally:
            for writer in locks:
                writer.rollback()
                writer.close()
    monkeypatch.setattr(transport, 'send', send)
    clock[0] += 35
    restored = restored_sender(sender, clock, transport)
    restored.deliver_one()
    assert len(transport.messages) == 1 and restored.snapshot()['sent'] == 1


def accepted_before_rollback(tmp_path, monkeypatch, *, kind='signals', row=None):
    clock, transport = [1000.], ImageTransport()
    sender = DingTalkOutbox(tmp_path / 'queue.sqlite', transport=transport, now=lambda: clock[0],
                           signal_validator=lambda rows: (rows, []))
    row = row or image_event()
    if kind == 'chart':
        sender.enqueue_chart(row, 'https://fixture.invalid/saved.png')
    elif kind == 'screening':
        from tests.web.test_dingtalk_notifications import result
        sender.screening_completed(result(), [])
    else:
        if kind == 'inline':
            sender.charts = Mock()
            row = {**row, 'signal_type': 'consolidation_divergence', 'point_type': 'consolidation_down',
                   'stage': 'warning', 'point_status': 'warning', 'inline_chart': True}
        sender.signal_events([row], since=0)
        if kind == 'inline':
            sender.enqueue_chart(row, 'https://fixture.invalid/saved.png')
    connect = sender._connect
    @contextmanager
    def rollback(**kwargs):
        with connect(**kwargs) as db:
            yield db
            if transport.messages or transport.images:
                raise OSError('receipt rollback')
    with monkeypatch.context() as fault:
        fault.setattr(sender, '_connect', rollback)
        with pytest.raises(OSError, match='receipt rollback'):
            sender.deliver_one()
    assert len(transport.messages) + len(transport.images) == 1
    assert all(path.exists() for path in journal.paths(sender.path))
    return sender, clock, transport


def restored_sender(sender, clock, transport):
    return DingTalkOutbox(sender.path, transport=transport, now=lambda: clock[0],
                         signal_validator=lambda rows: (rows, []))


@pytest.mark.parametrize('kind', ['signals', 'chart', 'inline', 'screening'])
def test_each_delivery_kind_recovers_success_without_another_send(tmp_path, monkeypatch, kind):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch, kind=kind)
    restored = restored_sender(sender, clock, transport)
    clock[0] += 35
    restored.deliver_one()
    restored.deliver_one()
    with restored._read_connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == 'sent'
    assert len(transport.messages) + len(transport.images) == 1
    assert not any(path.exists() for path in journal.paths(sender.path))


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'oversized'])
def test_independent_backup_recovers_a_damaged_primary_receipt(tmp_path, monkeypatch, damage):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch)
    primary, _ = journal.paths(sender.path)
    if damage == 'missing':
        primary.unlink()
    elif damage == 'oversized':
        primary.write_bytes(b'x' * (journal.MAX_BYTES + 1))
    else:
        primary.write_text('{damaged', encoding='utf-8')
    restored = restored_sender(sender, clock, transport)
    clock[0] += 35
    restored.deliver_one()
    assert len(transport.messages) == 1 and restored.snapshot()['sent'] == 1


def test_corrupt_receipts_block_dispatch_until_saved_acceptance_is_recovered(tmp_path, monkeypatch):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch)
    primary, backup = journal.paths(sender.path)
    saved = primary.read_bytes()
    primary.write_text('{damaged', encoding='utf-8')
    backup.write_text('{damaged', encoding='utf-8')
    restored = restored_sender(sender, clock, transport)
    clock[0] += 35
    restored.signal_events([event('confirmed', identity='next', stamp=clock[0])], since=0)
    with pytest.raises(journal.ReceiptJournalError):
        restored.deliver_one()
    assert len(transport.messages) == 1
    backup.write_bytes(saved)
    restored.deliver_one()
    assert len(transport.messages) == 2 and restored.snapshot()['sent'] == 2


@pytest.mark.parametrize('mismatch', ['time', 'generation', 'store'])
def test_another_attempt_cannot_be_completed_with_a_mismatched_receipt(tmp_path, monkeypatch, mismatch):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch)
    with sender._connect() as db:
        if mismatch == 'time':
            db.execute('UPDATE outbox SET send_started=send_started+1')
        elif mismatch == 'generation':
            db.execute("UPDATE delivery_settings SET value=value+1 WHERE key='request_generation'")
        else:
            db.execute("UPDATE delivery_settings SET value=? WHERE key='receipt_store_id'", ('receipt-store-v1:' + 'f' * 32,))
    restored = restored_sender(sender, clock, transport)
    clock[0] += 35
    with pytest.raises(journal.ReceiptJournalError, match='身份'):
        restored.deliver_one()
    assert len(transport.messages) == 1


def test_journal_cleanup_failure_after_commit_is_idempotent(tmp_path, monkeypatch):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch)
    restored = restored_sender(sender, clock, transport)
    with monkeypatch.context() as fault:
        fault.setattr(journal, 'clear', Mock(side_effect=OSError('file busy')))
        with pytest.raises(OSError, match='file busy'):
            restored.deliver_one()
    assert restored.snapshot()['sent'] == 1
    final = restored_sender(sender, clock, transport)
    clock[0] += 35
    final.deliver_one()
    assert len(transport.messages) == 1 and not any(path.exists() for path in journal.paths(sender.path))


def test_original_sender_does_not_restore_an_old_receipt_after_a_peer_advances(tmp_path, monkeypatch):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch)
    peer = restored_sender(sender, clock, transport)
    peer.deliver_one()
    clock[0] += 35
    peer.signal_events([event('confirmed', identity='next', stamp=clock[0])], since=0)
    peer.deliver_one()
    sender.deliver_one()
    assert len(transport.messages) == 2 and sender.snapshot()['sent'] == 2
    assert not any(path.exists() for path in journal.paths(sender.path))


def test_already_committed_journal_replay_does_not_reset_a_newer_request_marker(tmp_path, monkeypatch):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch)
    primary, backup = journal.paths(sender.path)
    saved = primary.read_bytes()
    peer = restored_sender(sender, clock, transport)
    peer.deliver_one()
    with peer._connect() as db:
        db.execute("UPDATE delivery_settings SET value=value+1 WHERE key='request_generation'")
        db.execute("UPDATE delivery_settings SET value=1 WHERE key='request_inflight'")
    primary.write_bytes(saved)
    backup.write_bytes(saved)
    restored_sender(sender, clock, transport).deliver_one(recovery_only=True)
    with peer._read_connect() as db:
        assert db.execute("SELECT value FROM delivery_settings WHERE key='request_inflight'").fetchone()[0] == 1
    assert len(transport.messages) == 1 and not any(path.exists() for path in journal.paths(sender.path))


def test_memory_only_retry_observes_a_peer_commit_before_republishing_the_journal(tmp_path, monkeypatch):
    clock, transport = [1000.], Transport()
    sender = DingTalkOutbox(tmp_path / 'queue.sqlite', transport=transport, now=lambda: clock[0])
    sender.signal_events([event('confirmed')], since=0)
    save = journal.save
    def saved_then_failed(*args):
        save(*args)
        raise OSError('completed write reported a failure')
    with monkeypatch.context() as fault:
        fault.setattr(journal, 'save', saved_then_failed)
        with pytest.raises(OSError):
            sender.deliver_one()
    assert sender._accepted_receipt
    peer = restored_sender(sender, clock, transport)
    peer.deliver_one()
    clock[0] += 35
    peer.signal_events([event('confirmed', identity='next', stamp=clock[0])], since=0)
    peer.deliver_one()
    sender.deliver_one()
    assert len(transport.messages) == 2 and sender._accepted_receipt is None
    assert not any(path.exists() for path in journal.paths(sender.path))


def test_accepted_response_stays_in_memory_during_temporary_journal_write_failure(tmp_path, monkeypatch):
    clock, transport = [1000.], Transport()
    sender = DingTalkOutbox(tmp_path / 'queue.sqlite', transport=transport, now=lambda: clock[0])
    sender.signal_events([event('confirmed')], since=0)
    with monkeypatch.context() as fault:
        fault.setattr(journal, 'save', Mock(side_effect=OSError('journal busy')))
        with pytest.raises(OSError, match='journal busy'):
            sender.deliver_one()
    assert sender._accepted_receipt and len(transport.messages) == 1
    clock[0] += 35
    sender.deliver_one()
    assert sender.snapshot()['sent'] == 1 and len(transport.messages) == 1


@pytest.mark.parametrize('peer_recovered', [False, True])
def test_disabled_sender_restores_receipt_without_dispatching_queued_work(tmp_path, monkeypatch, peer_recovered):
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch)
    clock[0] += 1
    sender.begin_window(clock[0])
    clock[0] += 1
    sender.signal_events([event('confirmed', identity='next', stamp=clock[0])], since=0)
    if peer_recovered:
        restored_sender(sender, clock, transport).deliver_one(recovery_only=True)
        assert sender._receipt_pending
        restored = sender
    else:
        restored = restored_sender(sender, clock, transport)
    done = threading.Event()
    recover = restored._recover_accepted_receipt
    def recovered():
        recover()
        restored._stop.set()
        done.set()
    monkeypatch.setattr(restored, '_recover_accepted_receipt', recovered)
    try:
        restored.start(lambda: False)
        assert done.wait(5)
    finally:
        restored.stop()
    assert len(transport.messages) == 1
    status = restored.snapshot()
    assert status['sent'] == 1 and status['pending'] == 1
    assert any('平台已接收' in row['error'] for row in status['history'])


def test_semantic_dedup_and_chart_enqueue_recover_with_the_receipt(tmp_path, monkeypatch):
    from tests.web.test_notification_semantic_dedup import notice
    sender, clock, transport = accepted_before_rollback(tmp_path, monkeypatch, row=notice(1))
    restored = restored_sender(sender, clock, transport)
    restored.charts = Mock()
    clock[0] += 35
    restored.deliver_one()
    restored.charts.enqueue.assert_called_once()
    restored.signal_events([notice(2)], since=0)
    restored.deliver_one()
    assert len(transport.messages) == 1 and restored.snapshot()['sent'] == 1


class FileTransport(Transport):
    def __init__(self, log):
        super().__init__()
        self.log = Path(log)
    def send(self, _text):
        with self.log.open('a', encoding='utf-8') as stream:
            stream.write('accepted\n')
            stream.flush()
            os.fsync(stream.fileno())


def crash_after_acceptance(database, log):
    sender = DingTalkOutbox(Path(database), transport=FileTransport(log), now=lambda: 1000.)
    connect = sender._connect
    @contextmanager
    def interrupted(**kwargs):
        with connect(**kwargs) as db:
            yield db
            if Path(log).exists():
                os._exit(13)
    sender._connect = interrupted
    sender.deliver_one()


def test_real_process_exit_after_provider_acceptance_does_not_repeat_request(tmp_path):
    path, log = tmp_path / 'queue.sqlite', tmp_path / 'requests.log'
    sender = DingTalkOutbox(path, transport=Transport(), now=lambda: 1000.)
    sender.signal_events([event('confirmed')], since=0)
    child = multiprocessing.get_context('spawn').Process(target=crash_after_acceptance, args=(path, log))
    child.start()
    child.join(15)
    if child.is_alive():
        child.terminate()
        child.join(3)
        pytest.fail('isolated receipt child did not exit')
    assert child.exitcode == 13
    child.close()
    restored = DingTalkOutbox(path, transport=FileTransport(log), now=lambda: 1040.)
    restored.deliver_one()
    assert log.read_text(encoding='utf-8').splitlines() == ['accepted']
    assert restored.snapshot()['sent'] == 1
