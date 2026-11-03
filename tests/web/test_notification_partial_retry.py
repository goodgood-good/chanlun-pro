"""Partial chart delivery is prompt, bounded and independently completed."""
import base64
import hashlib
import json

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.notification_charts import RenderedChart
from cl_app.services import tradingview_snapshot as tv
from tests.web.test_notification_charts import PNG, ImageTransport, image_event


def rendered(tag, periods):
    return RenderedChart(PNG + tag.encode(), periods=periods,
                         missing_periods=[p for p in tv.PERIODS if p not in periods],
                         primary_frequency='1m')


def box_at(tmp_path, clock, event, images):
    if event.get('signal_type') == 'consolidation_divergence':
        event = {**event, 'inline_chart':True}  # Exercise legacy combined receipts.
    transport = ImageTransport()
    box = DingTalkOutbox(tmp_path / 'outbox.sqlite', transport=transport,
                        now=lambda: clock[0], charts=True, signal_validator=lambda rows: (rows, []))
    queue = iter(images)
    calls = []
    box.charts.renderer = lambda row, _: calls.append(row.copy()) or next(queue)
    box.charts.check_public = False
    box.signal_events([event], since=0)
    if not event.get('signal_type') == 'consolidation_divergence':
        box.deliver_one()
    return box, transport, calls


@pytest.mark.parametrize('inline', [False, True])
def test_partial_first_delivery_then_page_only_completion_without_another_message(tmp_path, inline):
    clock = [1000.]
    event = {**image_event(), 'frequency': '1m'}
    if inline:
        event.update(signal_type='consolidation_divergence', point_type='consolidation_up', stage='warning', point_status='warning')
    box, transport, calls = box_at(tmp_path, clock, event, [rendered('partial', ['1m']), rendered('full', list(tv.PERIODS))])
    box.charts.process_one()
    with box.charts._db() as db:
        job = dict(db.execute('SELECT * FROM jobs').fetchone())
    assert job['status'] == 'pending' and json.loads(job['missing_periods']) == ['5m', '30m']
    clock[0] += 5; box.deliver_one()
    with box._connect() as db:
        original = [tuple(row) for row in db.execute("SELECT id,content,sent FROM outbox WHERE status='sent'")]
    clock[0] += 26; box.charts.process_one()
    with box.charts._db() as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0] == 'ready'
    box.deliver_one()
    with box._connect() as db:
        complete = db.execute("SELECT id,status,content FROM outbox WHERE id LIKE 'chart-complete:%'").fetchall()
        assert len(complete) == 1 and complete[0][1] == 'suppressed'
        assert '原通知附图补全' in complete[0][2]
        for identity, content, sent in original:
            assert tuple(db.execute('SELECT content,sent FROM outbox WHERE id=?', (identity,)).fetchone()) == (content, sent)
    assert box.charts.store.coverage(job['initial_image_hash'])['missing_periods'] == ['5m', '30m']
    box.charts.recover(); box.charts.process_one(); box.deliver_one()
    assert len(calls) == 2 and len(transport.images) == 1


@pytest.mark.parametrize('first_complete', [False, True])
@pytest.mark.parametrize('inline', [False, True])
def test_committed_first_image_recovers_when_job_hash_was_not_written(tmp_path, first_complete, inline):
    clock = [1000.]
    event = {**image_event(), 'frequency': '1m'}
    if inline:
        event.update(signal_type='consolidation_divergence', point_type='consolidation_up', stage='warning', point_status='warning')
    full = rendered('full', list(tv.PERIODS))
    box, transport, calls = box_at(tmp_path, clock, event, [full if first_complete else rendered('partial', ['1m']), full])
    box.charts.process_one(); clock[0] += 5; box.deliver_one()
    with box.charts._db() as db:
        db.execute("UPDATE jobs SET status='running',initial_image_hash='',next_try=?", (clock[0] + 60,))
    clock[0] += 61; box.charts.process_one(); box.deliver_one()
    with box._connect() as db:
        count = db.execute("SELECT COUNT(*) FROM outbox WHERE id LIKE 'chart-complete:%'").fetchone()[0]
    assert count == (0 if first_complete else 1)
    with box.charts._db() as db:
        job = dict(db.execute('SELECT * FROM jobs').fetchone())
    assert job['status'] == 'ready' and job['initial_image_hash']


def test_missing_context_is_bounded_and_does_not_resend_partial_images(tmp_path):
    clock = [1000.]; event = {**image_event(), 'frequency': '1m'}
    partial = rendered('partial', ['1m'])
    box, transport, calls = box_at(tmp_path, clock, event, [partial] * 3)
    box.charts.process_one(); box.deliver_one()
    clock[0] += 31; box.charts.process_one(); box.deliver_one()
    clock[0] += 91; box.charts.process_one(); box.deliver_one()
    with box.charts._db() as db:
        job = dict(db.execute('SELECT * FROM jobs').fetchone())
    assert job['status'] == 'failed' and job['attempts'] == 3 and '5m' in job['error']
    box.charts.recover(); clock[0] += 100; box.charts.process_one()
    assert len(calls) == 3 and len(transport.images) == 1


def test_inline_partial_waiting_for_transport_survives_exhausted_auxiliary_retries(tmp_path):
    clock = [1000.]
    event = {**image_event(), 'frequency': '1m', 'signal_type': 'consolidation_divergence',
             'point_type': 'consolidation_up', 'stage': 'warning', 'point_status': 'warning'}
    box, transport, calls = box_at(tmp_path, clock, event, [rendered('partial', ['1m'])] * 3)
    box.charts.process_one()
    clock[0] += 31; box.charts.process_one()
    clock[0] += 91; box.charts.process_one()
    with box._connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == 'pending'
    box.deliver_one()
    with box._connect() as db:
        assert db.execute('SELECT status FROM outbox').fetchone()[0] == 'sent'
    assert len(calls) == 3


def test_committed_supplement_is_not_resent_after_worker_restart(tmp_path):
    clock = [1000.]; event = {**image_event(), 'frequency': '1m'}
    full = rendered('full', list(tv.PERIODS))
    box, transport, calls = box_at(tmp_path, clock, event, [rendered('partial', ['1m']), full, full])
    box.charts.process_one(); clock[0] += 5; box.deliver_one()
    clock[0] += 26; box.charts.process_one(); box.deliver_one()
    with box.charts._db() as db:
        db.execute("UPDATE jobs SET status='running',next_try=?", (clock[0] + 60,))
    clock[0] += 61; box.charts.process_one(); box.deliver_one()
    with box._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM outbox WHERE id LIKE 'chart-complete:%'").fetchone()[0] == 1
    assert len(transport.images) == 1


@pytest.mark.parametrize('expired', [True, False])
def test_delivered_original_can_complete_silently_after_signal_expires_or_changes(tmp_path, expired):
    clock = [1000.]; event = {**image_event(), 'frequency': '1m'}
    box, transport, calls = box_at(tmp_path, clock, event,
        [rendered('partial', ['1m']), rendered('complete', ['1m','5m','30m'])])
    box.charts.process_one(); clock[0] += 5; box.deliver_one()
    clock[0] += 1800 if expired else 31
    if not expired: box.signal_validator = lambda rows: ([], [])
    box.charts.process_one()
    with box.charts._db() as db:
        assert db.execute('SELECT status FROM jobs').fetchone()[0] == 'ready'
    assert len(calls) == 2 and len(transport.images) == 1
    with box._read_connect() as db:
        assert db.execute("SELECT status FROM outbox WHERE id LIKE 'chart-complete:%'").fetchone()[0] == 'suppressed'


def test_png_coverage_is_immutable_even_if_a_renderer_mislabels_identical_bytes(tmp_path):
    from cl_app.services.notification_charts import ChartStore
    store = ChartStore(tmp_path, now=lambda: 1000.)
    store.publish(RenderedChart(PNG, periods=['1m'], missing_periods=['5m', '30m'], primary_frequency='1m'))
    with pytest.raises(ValueError, match='coverage cannot be changed'):
        store.publish(RenderedChart(PNG, periods=list(tv.PERIODS), primary_frequency='1m'))
    assert store.coverage(hashlib.sha256(PNG).hexdigest())['missing_periods'] == ['5m', '30m']


def test_verified_panel_pixels_are_reused_only_for_the_same_frozen_event(tmp_path):
    event = {**image_event(), 'frequency': '1m'}
    cache = tv.PanelImageCache(event, tmp_path)
    data = 'data:image/png;base64,' + base64.b64encode(PNG).decode()
    cache.save('1m', data)
    assert tv.PanelImageCache(event, tmp_path).read() == {'1m': data}
    assert tv.PanelImageCache({**event, 'source_closed_at': event.get('source_closed_at', 0) + 60}, tmp_path).read() == {}
