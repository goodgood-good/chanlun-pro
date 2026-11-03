"""Tiny local fixtures only: no renderer, provider, delivery or worker process."""
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
from threading import Event, Thread
from urllib.parse import parse_qs, urlparse

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.notification_image_retention import protected_image_hashes
from tests.web.test_notification_charts import PNG, image_event


DAY = 86400


@pytest.fixture
def fixture(tmp_path):
    clock = [1000.0]
    box = DingTalkOutbox(tmp_path / 'outbox.sqlite', now=lambda: clock[0], charts=True)
    with box._connect():
        pass
    with box.charts._db():
        pass
    url = box.charts.store.publish(PNG)
    digest = hashlib.sha256(PNG).hexdigest()
    path = box.charts.store.root / (digest + '.png')
    clock[0] += 9 * DAY
    return box, clock, url, digest, path


def job(box, digest, *, updated, status='ready', extra=None):
    with box.charts._db() as db:
        db.execute('INSERT INTO jobs(id,event,status,created,updated,initial_image_hash,history_coverage) '
                   'VALUES (?,?,?,?,?,?,?)', ('job', json.dumps(extra or {}), status, 1000, updated, digest, '{}'))
        db.execute("INSERT INTO evidence_archive_jobs(id,status) VALUES ('job','saved')")


def receipt(box, url, *, status='sent'):
    content = json.dumps({'image': url})
    with box._connect() as db:
        db.execute('INSERT INTO outbox(id,kind,content,status,created,expires,sent) VALUES (?,?,?,?,?,?,?)',
                   ('receipt', 'chart', content, status, 1000, 2000, 1001 if status == 'sent' else None))
    return content


def test_historical_completion_retains_old_original_while_ready_job_is_retained(fixture):
    box, clock, url, digest, path = fixture
    job(box, digest, updated=clock[0] - 2 * DAY)
    before = receipt(box, url)
    box.charts.cleanup()
    assert path.read_bytes() == PNG
    with box.charts._read_db() as db:
        assert db.execute("SELECT status FROM jobs WHERE id='job'").fetchone()[0] == 'ready'
    with box._read_connect() as db:
        assert db.execute("SELECT content FROM outbox WHERE id='receipt'").fetchone()[0] == before


@pytest.mark.parametrize('inline', [False, True])
def test_crash_recovery_retains_original_before_the_job_hash_is_rebound(fixture, monkeypatch, inline):
    from cl_app.services.dingtalk_notifications import signal_key
    box, _, url, digest, path = fixture
    event = {**image_event(identity='crash-recovery'), 'inline_chart': inline}
    identity = signal_key(event)
    with box.charts._db() as db:
        db.execute('INSERT INTO jobs(id,event,status,created,initial_image_hash) VALUES (?,?,?,?,?)',
                   (identity, json.dumps(event), 'pending', 1000, ''))
    receipt_id = 'inline-message' if inline else 'chart:' + identity
    with box._connect() as db:
        db.execute('INSERT INTO outbox(id,kind,content,status,created,expires,sent) VALUES (?,?,?,?,?,?,?)',
                   (receipt_id, 'signals' if inline else 'chart', json.dumps({'image': url}), 'sent', 1000, 2000, 1001))
        if inline:
            db.execute('INSERT INTO inline_charts(signal_key,url,created,outbox_id) VALUES (?,?,?,?)',
                       (identity, url, 1000, receipt_id))
    monkeypatch.setattr(box.charts.store, 'publish', lambda *_: pytest.fail('Recovery must not recreate original pixels'))
    box.charts.check_public = False
    assert box.initial_chart_hash(event) == digest
    box.charts.cleanup()
    assert path.read_bytes() == PNG
    box.charts.process_one('initial')
    with box.charts._read_db() as db:
        row = db.execute('SELECT status,initial_image_hash FROM jobs WHERE id=?', (identity,)).fetchone()
    assert tuple(row) == ('pending', digest)
    assert path.read_bytes() == PNG


def test_expired_terminal_job_is_committed_before_unreferenced_old_pixels_are_removed(fixture):
    box, _, url, digest, path = fixture
    job(box, digest, updated=1000)
    original = receipt(box, url)
    box.charts.cleanup()
    assert not path.exists()
    with box.charts._read_db() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
        assert db.execute('SELECT COUNT(*) FROM evidence_archive_jobs').fetchone()[0] == 0
    with box._read_connect() as db:
        assert db.execute("SELECT content FROM outbox WHERE id='receipt'").fetchone()[0] == original
    assert int(parse_qs(urlparse(url).query)['expires'][0]) == 1000 + 7 * DAY


@pytest.mark.parametrize('status', ['pending', 'running', 'unknown_state'])
def test_nonterminal_or_unknown_job_always_protects_original(fixture, status):
    box, _, _, digest, path = fixture
    job(box, digest, updated=1000, status=status)
    box.charts.cleanup()
    assert path.is_file()


def test_live_authentic_sent_link_protects_old_file_even_after_job_retirement(fixture):
    box, clock, _, digest, path = fixture
    job(box, digest, updated=1000)
    fresh_url = box.charts.store.publish(PNG)
    os.utime(path, (1000, 1000))  # Restored file metadata may predate its valid URL.
    before = receipt(box, fresh_url)
    box.charts.cleanup()
    assert path.read_bytes() == PNG
    with box._read_connect() as db:
        assert db.execute("SELECT content FROM outbox WHERE id='receipt'").fetchone()[0] == before
    assert int(parse_qs(urlparse(fresh_url).query)['expires'][0]) == int(clock[0]) + 7 * DAY


@pytest.mark.parametrize('status', ['pending', 'retry', 'sending', 'unknown_state'])
def test_queued_or_unknown_delivery_reference_is_retained_even_with_expired_url(fixture, status):
    box, _, url, _, path = fixture
    receipt(box, url, status=status)
    box.charts.cleanup()
    assert path.is_file()


@pytest.mark.parametrize('problem', ['bad_signature', 'missing_key', 'bad_key'])
def test_unverifiable_expired_link_keeps_its_original(fixture, problem):
    box, _, url, _, path = fixture
    if problem == 'bad_signature':
        url = url.rsplit('signature=', 1)[0] + 'signature=' + '0' * 64
    elif problem == 'missing_key':
        (box.charts.store.root / '.signing-key').unlink()
    else:
        (box.charts.store.root / '.signing-key').write_bytes(b'broken')
    receipt(box, url)
    box.charts.cleanup()
    assert path.is_file()


def test_unidentifiable_local_reference_preserves_the_entire_image_store(fixture):
    box, _, _, _, path = fixture
    receipt(box, 'https://example.test/public/alert-chart/unknown.png?expires=1')
    box.charts.cleanup()
    assert path.is_file() and '保留原图' in box.charts._archive_cleanup_error


@pytest.mark.parametrize('reader', ['jobs', 'outbox'])
def test_database_read_failure_preserves_old_images(fixture, monkeypatch, reader):
    box, _, _, _, path = fixture
    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError('read unavailable')
        yield
    monkeypatch.setattr(box.charts, '_read_db' if reader == 'jobs' else '_read_image_outbox', unavailable)
    box.charts.cleanup()
    assert path.is_file() and '保留原图' in box.charts._archive_cleanup_error


def test_job_retirement_transaction_failure_never_runs_image_deletion(fixture, monkeypatch):
    box, _, _, digest, path = fixture
    job(box, digest, updated=1000)
    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError('write unavailable')
        yield
    monkeypatch.setattr(box.charts, '_db', unavailable)
    box.charts.cleanup()
    assert path.is_file()


def test_reference_read_after_retirement_failure_cannot_roll_jobs_back_to_deleted_png(fixture, monkeypatch):
    box, _, _, digest, path = fixture
    job(box, digest, updated=1000)
    @contextmanager
    def unavailable():
        raise sqlite3.OperationalError('outbox read unavailable')
        yield
    monkeypatch.setattr(box.charts, '_read_image_outbox', unavailable)
    box.charts.cleanup()
    assert path.is_file()
    with box.charts._read_db() as db:
        assert db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0


def test_standalone_cleanup_without_reference_proof_is_conservative(fixture):
    box, _, _, _, path = fixture
    box.charts.store.cleanup()
    assert path.is_file()


def test_image_cleanup_never_migrates_an_incomplete_outbox_schema(fixture):
    box, _, _, _, path = fixture
    with sqlite3.connect(box.path) as db:
        db.execute('DROP TABLE inline_charts')
    box.charts.cleanup()
    assert path.is_file()
    with sqlite3.connect(box.path) as db:
        assert db.execute("SELECT 1 FROM sqlite_master WHERE name='inline_charts'").fetchone() is None
    assert '保留原图' in box.charts._archive_cleanup_error


def test_republication_after_reference_snapshot_refreshes_age_before_deletion(fixture):
    box, _, _, _, path = fixture
    box.charts.store.publish(PNG)
    box.charts.store.cleanup(protected_hashes=set())
    assert path.is_file()


def test_publish_and_cleanup_cannot_race_on_the_same_old_content_hash(fixture, monkeypatch):
    box, _, _, _, path = fixture
    entered, release = Event(), Event()
    original = box.charts.store._publish
    def publishing(png):
        entered.set()
        assert release.wait(2)
        return original(png)
    monkeypatch.setattr(box.charts.store, '_publish', publishing)
    thread = Thread(target=box.charts.store.publish, args=(PNG,))
    thread.start()
    try:
        assert entered.wait(2)
        with pytest.raises(TimeoutError):
            box.charts.store.cleanup(protected_hashes=set())
        assert path.is_file()
    finally:
        release.set();thread.join(2)
    assert not thread.is_alive()


def test_job_embedded_image_reference_is_protected_even_if_not_initial_hash(fixture):
    box, clock, _, digest, path = fixture
    job(box, '', updated=clock[0], extra={'retained_image': digest})
    box.charts.cleanup()
    assert path.is_file()


def test_unknown_retained_job_image_identity_preserves_all_pixels(fixture):
    box, clock, _, _, path = fixture
    job(box, 'unrecognized-image-identity', updated=clock[0])
    box.charts.cleanup()
    assert path.is_file() and '保留原图' in box.charts._archive_cleanup_error


def test_retained_archive_reference_survives_terminal_job_expiry(fixture):
    box, _, _, digest, path = fixture
    job(box, digest, updated=1000)
    archive = box.charts.root / 'notification_history'
    archive.mkdir()
    (archive/'retained.json').write_text(json.dumps({'original_image_hash': digest}), encoding='utf-8')
    box.charts.cleanup()
    assert path.is_file()


@pytest.mark.parametrize('raw', [b'\xff\xfe', b'{', b'"unknown document"'])
def test_unreadable_archive_reference_preserves_all_pixels(fixture, raw):
    box, _, _, _, path = fixture
    archive = box.charts.root / 'notification_history'
    archive.mkdir()
    (archive/'unknown.json').write_bytes(raw)
    box.charts.cleanup()
    assert path.is_file() and '保留原图' in box.charts._archive_cleanup_error


def test_plain_numeric_hash_is_not_lost_when_interpreted_as_json_number(fixture):
    box, clock, _, _, _ = fixture
    with box._connect() as db:
        db.execute('INSERT INTO inline_charts(signal_key,url,created,outbox_id) VALUES (?,?,?,?)',
                   ('numeric', '1' * 64, clock[0], 'unknown'))
    with box.charts._read_db() as jobs, box._read_connect() as outbox:
        assert '1' * 64 in protected_image_hashes(jobs, outbox, now=clock[0], signing_key=None)
