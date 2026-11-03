"""Queued text and images must revalidate when point preferences change."""

import json
import threading

import pytest

from cl_app.services.dingtalk_notifications import DingTalkOutbox
from cl_app.services.notification_policy import default_point_notifications, point_notification_decision
from tests.web.test_dingtalk_notifications import Transport, event, result


class ImageTransport(Transport):
    def send_markdown(self, title, content):
        self.messages.append(title + '\n' + content)


@pytest.mark.parametrize('kind', ['signals', 'chart'])
@pytest.mark.parametrize('boundary', ['validation', 'formatting'])
def test_policy_change_fences_inflight_authorization_without_spending_send_budget(tmp_path, monkeypatch, kind, boundary):
    clock = [1000.]
    transport = ImageTransport()
    box = DingTalkOutbox(tmp_path / 'outbox.sqlite', transport=transport, now=lambda: clock[0])
    row = event('confirmed')
    if kind == 'signals':
        box.signal_events([row], since=0)
    else:
        box.enqueue_chart(row, 'https://example.test/chart.png', complete=kind == 'chart-complete')
    entered, resume = threading.Event(), threading.Event()
    failures = []

    def pause():
        entered.set()
        assert resume.wait(5)

    def validate(events):
        if boundary == 'validation':
            pause()
        return events, []

    box.signal_validator = validate
    original = box._signal_content
    if boundary == 'formatting':
        if kind == 'signals':
            def formatting(events, stamp):
                pause()
                return original(events, stamp)
            monkeypatch.setattr(box, '_signal_content', formatting)
        else:
            # Chart contents are frozen. Pause after validation, before the
            # final dispatch transaction, at the current membership hint read.
            reads = [0]
            def hint():
                reads[0] += 1
                if reads[0] == 2:
                    pause()
                return None
            monkeypatch.setattr(box, '_holding_snapshot', hint)

    def deliver():
        try:
            box.deliver_one()
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=deliver)
    worker.start()
    try:
        assert entered.wait(5)
        clock[0] += 1
        # A second object/process shares the durable generation, not a lock.
        DingTalkOutbox(box.path).invalidate_point_notification_policy()
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive() and not failures and not transport.messages
    with box._connect() as db:
        receipt = db.execute('SELECT status,attempts,expires,send_started FROM outbox').fetchone()
        assert tuple(receipt) == ('retry', 0, 2800, None)
        assert db.execute("SELECT value FROM delivery_settings WHERE key='next_request_at'").fetchone() is None
    # New policy denies this point; the saved queued image/text cannot revive.
    box.signal_validator = lambda events: ([], [])
    box.deliver_one()
    assert not transport.messages and box.snapshot()['pending'] == 0


def test_undelivered_notice_cannot_authorize_a_historical_completion(tmp_path):
    transport=ImageTransport()
    box=DingTalkOutbox(tmp_path/'outbox.sqlite',transport=transport,now=lambda:1000.)
    assert not box.enqueue_chart(event('confirmed'),'https://example.test/chart.png',complete=True)
    box.signal_validator=lambda _events:pytest.fail('page-only completion should never enter the delivery lane')
    box.deliver_one();box.invalidate_point_notification_policy();box.deliver_one()
    assert not transport.messages
    with box._connect() as db:
        assert db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0] == 0


def test_mixed_batch_prunes_nonholding_sell_and_disabled_buy_without_losing_enabled_buy(tmp_path):
    clock = [1000.]
    transport = Transport()
    policy = default_point_notifications()
    policy['others'] = ['2buy']
    state = {'point_notifications': policy}
    rows = [{**event('confirmed', identity=point), 'point_type': point, 'name': point}
            for point in ['1buy', '2buy', '3sell']]
    box = DingTalkOutbox(tmp_path / 'mixed.sqlite', transport=transport, now=lambda: clock[0],
                        signal_validator=lambda events: ([row for row in events
                            if point_notification_decision(state, row, set()) == 'allow'], []))
    box.signal_events(rows, since=0)
    for _ in range(3):
        box.deliver_one()
        clock[0] += 5
    assert len(transport.messages) == 1
    assert '2buy' in transport.messages[0] and '1buy' not in transport.messages[0] and '3sell' not in transport.messages[0]
    with box._connect() as db:
        sent = db.execute("SELECT p.events FROM signal_payloads p JOIN outbox o ON o.id=p.outbox_id WHERE o.status='sent'").fetchall()
    assert [row['point_type'] for payload, in sent for row in json.loads(payload)] == ['2buy']


def test_screening_completion_and_old_queued_summary_never_bypass_point_policy(tmp_path):
    transport = Transport()
    clock = [1000.]
    box = DingTalkOutbox(tmp_path / 'summary.sqlite', transport=transport, now=lambda: clock[0])
    sample = {**result(), 'selected': [{'market': 'a', 'code': 'SH.600088', 'name': 'FORBIDDEN_STOCK',
               'point': {'point_type': '3sell', 'status': 'confirmed'}}]}
    box.screening_completed(sample, [])
    box.deliver_one()
    with box._connect() as db:
        box._insert(db, 'legacy-summary', 'screening',
                    '缠论 Pro · 盘后选股完成\n检查标的：5 / 5\n数据异常：1 个标的\n已确认买点：1 个；卖点：2 个\nFORBIDDEN_STOCK · 三卖\n任务：legacy',
                    clock[0], 86400)
    clock[0] += 5
    box.deliver_one()
    assert len(transport.messages) == 2
    for content in transport.messages:
        assert '检查标的：5 / 5' in content and '数据异常：1 个标的' in content
        assert 'FORBIDDEN_STOCK' not in content and '卖点' not in content and '三卖' not in content
