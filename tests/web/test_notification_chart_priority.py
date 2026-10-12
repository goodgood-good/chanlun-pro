"""First attachments stay independent of bounded, concurrent history work."""
from copy import deepcopy
import threading
from unittest.mock import Mock

import pytest

from chanlun import config
from cl_app.services import notification_charts as charts, notification_periods, tradingview_snapshot as tv
from cl_app.services.dingtalk_notifications import DingTalkOutbox
from tests.web.test_notification_charts import ImageTransport, PNG, image_event
from tests.web.test_notification_multi_period import signal as signal
from tests.test_holding_consolidation_pipeline import holding_run as holding_run


def inline_event(identity, stamp=1000, frequency='5m'):
    return {**image_event(stamp=stamp, identity=identity), 'frequency': frequency,
            'inline_chart':True,  # Recovery of a legacy combined notification.
            'signal_type': 'consolidation_divergence', 'point_type': 'consolidation_up',
            'stage': 'warning', 'point_status': 'warning'}


def partial(event):
    return charts.RenderedChart(PNG + event['id'].encode(), periods=[event['frequency']],
        missing_periods=[f for f in tv.PERIODS if f != event['frequency']],
        primary_frequency=event['frequency'],
        history_coverage={f: {'state': 'pending', 'progress_token': '0'} for f in tv.PERIODS})


def make_box(tmp_path, clock, monkeypatch):
    monkeypatch.setattr(config, 'ALERT_CHART_PUBLIC_BASE_URL', 'https://example.test')
    box = DingTalkOutbox(tmp_path / 'outbox.sqlite', transport=ImageTransport(),
                        now=lambda: clock[0], charts=True, signal_validator=lambda rows: (rows, []))
    box.charts.check_public = False
    box.charts.archive_one = lambda: None
    return box


def test_first_image_never_waits_for_an_already_running_history_slice(tmp_path, monkeypatch):
    clock = [1000.]
    box = make_box(tmp_path, clock, monkeypatch)
    old = inline_event('old')
    box.charts.renderer = lambda event, _: partial(event)
    box.signal_events([old], since=0)
    box.charts.process_one('initial')
    box.deliver_one()
    assert len(box.transport.images) == 1
    started, release = threading.Event(), threading.Event()
    finished_first = threading.Event()
    calls = []

    def render(event, _):
        calls.append((event['id'], clock[0]))
        if event['id'] == 'old':
            started.set()
            assert release.wait(5)
            raise charts.ChartHistoryPending({f: {'state': 'pending', 'progress_token': '1'} for f in tv.PERIODS})
        finished_first.set()
        return partial(event)

    box.charts.renderer = render
    clock[0] = 1006
    try:
        box.charts.start(lambda: True)
        assert started.wait(3)
        clock[0] = 1007
        box.signal_events([inline_event('new', stamp=1007, frequency='30m')], since=0)
        assert finished_first.wait(3), 'history slice held the first-image worker'
        # Rendering precedes the SQLite/receipt commit. Serialize only on the
        # initial lane; the blocked history lane must still be running.
        from chanlun.persistence.file_lock import interprocess_file_lock
        with interprocess_file_lock(tmp_path / '.chart-initial.lock', timeout_seconds=3):
            box.deliver_one()
        assert len(box.transport.images) == 2 and not release.is_set()
        with box._connect() as db:
            sent = db.execute("SELECT sent FROM outbox WHERE status='sent' ORDER BY sent DESC LIMIT 1").fetchone()[0]
        assert sent == 1007
        assert calls == [('old', 1006), ('new', 1007)]
        assert box.charts.history_progress.snapshot()['state'] == 'running'
    finally:
        release.set()
        box.charts.stop()


@pytest.mark.parametrize('lane', ['initial', 'history'])
def test_execution_lock_prevents_reclaim_after_lease_even_from_another_instance(tmp_path, monkeypatch, lane):
    clock = [1000.]
    box = make_box(tmp_path, clock, monkeypatch)
    event = inline_event('locked')
    box.signal_events([event], since=0)
    if lane == 'history':
        box.charts.renderer = lambda event, _: partial(event)
        box.charts.process_one('initial')
        clock[0] = 1006
    started, release = threading.Event(), threading.Event()
    calls, failures = [], []

    def render(row, _):
        calls.append(row['id'])
        started.set()
        assert release.wait(5)
        return partial(row)

    box.charts.renderer = render
    def run():
        try:
            box.charts.process_one(lane)
        except Exception as exc:
            failures.append(exc)
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert started.wait(3)
        clock[0] += 61
        restored = make_box(tmp_path, clock, monkeypatch)
        restored.charts.renderer = lambda *_: pytest.fail('same live job was reclaimed')
        assert restored.charts.process_one() is False
        assert calls == ['locked']
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not failures


def test_queued_first_images_precede_history_and_prioritize_holdings_then_5m_30m(tmp_path, monkeypatch):
    clock = [1000.]
    box = make_box(tmp_path, clock, monkeypatch)
    old = inline_event('old')
    box.charts.renderer = lambda event, _: partial(event)
    box.charts.enqueue([old], 1000)
    box.charts.process_one('initial')
    clock[0] = 1006
    rows = [{**image_event(identity='one'), 'frequency': '1m'},
            {**image_event(identity='five'), 'frequency': '5m'},
            {**image_event(identity='thirty'), 'frequency': '30m'},
            {**image_event(identity='holding'), 'frequency': '1m', 'holding': True}]
    box.charts.enqueue(rows, 1006)
    calls = []
    box.charts.renderer = lambda event, _: calls.append(event['id']) or PNG
    assert box.charts.process_one('history') is False
    for _ in rows:
        assert box.charts.process_one('initial')
    assert calls == ['holding', 'five', 'thirty', 'one']


def test_first_layout_has_only_frozen_proof_and_never_reads_cold_auxiliary_evidence(signal, monkeypatch, tmp_path):
    event, frozen = signal
    before = deepcopy(frozen[1])
    context = Mock(side_effect=AssertionError('cold context blocked first chart'))
    background = Mock(side_effect=AssertionError('history blocked first chart'))
    monkeypatch.setattr(notification_periods, 'context_evidence', context)
    monkeypatch.setattr(notification_periods, 'background_evidence', background)
    layout = tv.prepare_history_layout(event, tmp_path, initial_only=True)
    proof = next(p for p in layout['panels'] if p['settings']['signal_panel'])
    assert proof['history']['macd_dif'] == before['chart']['macd_dif']
    assert proof['history']['higher_macd_dif'] == before['chart']['higher_macd_dif']
    assert [p['settings']['frequency'] for p in layout['panels']] == list(tv.PERIODS)
    assert all(p['error_type'] == 'BackgroundPending' for p in layout['panels'] if p is not proof)
    assert all(item['state'] == 'pending' for item in layout['history_coverage'].values())
    assert frozen[1] == before
    context.assert_not_called()
    background.assert_not_called()


def test_history_slices_do_not_repeat_cold_auxiliary_snapshot_work(signal, monkeypatch, tmp_path):
    event, _ = signal
    context = Mock(side_effect=AssertionError('repeated auxiliary context'))
    monkeypatch.setattr(notification_periods, 'context_evidence', context)
    calls = []
    def pending(e, root, frequency, attrs, *, anchor_frame, fetch):
        calls.append((frequency, fetch))
        raise notification_periods.HistoryPending({'state': 'pending', 'progress_token': frequency})
    monkeypatch.setattr(notification_periods, 'background_evidence', pending)
    cache = {'proof:5m': 'already-frozen-proof'}
    layout = tv.prepare_history_layout(event, tmp_path, reused_images=cache, history_period='30m', defer_context=True)
    assert calls == [('1m', False), ('5m', False), ('30m', True)]
    assert next(p for p in layout['panels'] if p['settings']['signal_panel'])['cached_image'] == cache['proof:5m']
    context.assert_not_called()


def test_parallel_first_publish_never_exposes_a_partial_signing_key(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    link = charts.os.link
    def simultaneous_link(source, target):
        if str(target).endswith('.signing-key'):
            barrier.wait(timeout=3)
        return link(source, target)
    monkeypatch.setattr(charts.os, 'link', simultaneous_link)
    values, failures = [], []
    def publish(tag):
        try:
            values.append(charts.ChartStore(tmp_path, now=lambda: 1000).publish(PNG + tag))
        except Exception as exc:
            failures.append(exc)
    workers = [threading.Thread(target=publish, args=(bytes([i]),)) for i in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(4)
    assert len(values) == 2 and not failures
    assert len((tmp_path / '.signing-key').read_bytes()) == 32
    assert not list(tmp_path.glob('*.tmp'))


def test_native_first_and_complete_images_reuse_identical_proof_pixels(holding_run, monkeypatch, tmp_path):
    import pandas as pd
    from tests.web.test_notification_history_rendering import background, history
    from cl_app.services.signal_monitor import _consolidation_signal
    _, directory, record, frozen_frame, *_ = holding_run
    row = record['rows'][0]
    candidate = next(c for c in row['consolidation_alerts'] if c['alert']['status'] == 'confirmed')
    event = {**_consolidation_signal({**row, **candidate}), 'evidence_run_id': directory.name}
    cold = Mock(side_effect=AssertionError('native first attachment requested another period'))
    monkeypatch.setattr(notification_periods, 'context_evidence', cold)
    monkeypatch.setattr(notification_periods, 'background_evidence', cold)
    runs = directory.parent
    initial = charts.render_signal_chart(event, runs)
    assert initial.coverage['periods'] == ['30m']
    assert initial.coverage['missing_periods'] == ['1m', '5m']
    assert initial.coverage['missing_history_periods'] == list(tv.PERIODS)
    cold.assert_not_called()
    cache = tv.PanelImageCache(event, runs.parent / 'chart_panel_cache')
    proof_before = cache.read()['proof:30m']
    saved = []
    original_save = tv.PanelImageCache.save
    def save(self, key, data):
        saved.append(key)
        original_save(self, key, data)
    monkeypatch.setattr(tv.PanelImageCache, 'save', save)
    def ready(e, root, frequency, attrs, **kwargs):
        frame, snapshot = background(frequency, e['source_closed_at'])
        # Respect the fixture's actual A-share sessions in the synthetic
        # auxiliary display data; overnight bars do not map to TV geometry.
        step = int(frequency[:-1])
        dates = [stamp - pd.Timedelta(minutes=offset) for stamp in frozen_frame.date
                 for offset in range(30 - step, -1, -step)]
        frame['date'] = pd.DatetimeIndex(dates[-len(frame):])
        snapshot['source_started_at'] = int(frame.date.iloc[0].timestamp())
        return frame, snapshot, history()[frequency]
    monkeypatch.setattr(notification_periods, 'background_evidence', ready)
    complete = charts.render_signal_chart(event, runs, completion=True, history_period='30m')
    assert complete.coverage['periods'] == list(tv.PERIODS)
    assert complete.coverage['missing_periods'] == []
    assert complete.coverage['missing_history_periods'] == []
    assert cache.read()['proof:30m'] == proof_before
    assert all(key.startswith('history:') for key in saved) and len(saved) >= 2
    assert initial != complete and len(initial) > 10000 and len(complete) > len(initial)
    (tmp_path / 'first.png').write_bytes(initial)
    (tmp_path / 'complete.png').write_bytes(complete)
