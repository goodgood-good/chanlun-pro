"""Production history uses canonical minutes and cannot extend legacy clocks."""
from types import SimpleNamespace
from unittest.mock import Mock

import pandas as pd
import pytest

from chanlun import config
from cl_app.services.notification_history import prepare_notification_history
from tests.web.test_notification_history_preparation import inputs


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'EXCHANGE_US','usmart')
    monkeypatch.setattr(config,'US_HISTORY_KLINE_SOURCE','usmart')
    event, _, anchor, frame = inputs(tmp_path)
    native = Mock(return_value=(frame, {'complete':True,'source_origin':True, 'target_reached':False,
                                       'can_resume':False, 'progress_token':'verified-source'}))
    monkeypatch.setattr('chanlun.exchange.get_exchange', lambda *_:SimpleNamespace(notification_history_window=native))
    def run(**kwargs):
        return prepare_notification_history(event, '30m', anchor.attrs, anchor_frame=anchor,
                                            aligned_cutoff=event['source_closed_at'], **kwargs)
    return native, anchor, frame, run


def test_canonical_source_uses_same_cutoff_adjustment_and_bounded_budget(source):
    native, anchor, _, run = source
    saved = anchor.copy(deep=True)
    result = run(max_pages=3, timeout_seconds=9, fetch=False)
    options = native.call_args.kwargs
    assert options['cutoff'] == anchor.date.max() and options['all_origin']
    assert options['args'] == {'right':1} and options['max_pages']==3
    assert options['timeout_seconds']==9 and options['fetch'] is False
    assert result.coverage['source_origin'] and result.coverage['anchor_complete']
    pd.testing.assert_frame_equal(anchor, saved)


@pytest.mark.parametrize('fault',['legacy_basis','revised_price','incomplete_anchor'])
def test_changed_or_incomplete_evidence_never_becomes_usable_history(source,fault):
    native, anchor, frame, run = source
    candidate=frame.copy(deep=True)
    if fault=='legacy_basis':candidate.attrs['price_basis_revision']='new-clock'
    elif fault=='revised_price':candidate.loc[candidate.index[-1],'close']+=.01
    else:candidate=candidate.tail(1)
    native.return_value=(candidate, {'complete':True,'source_origin':True, 'can_resume':False})
    result=run()
    assert result.coverage['reason']=='contract_error' and not result.coverage['compatible_with_anchor']
    assert not result.coverage['can_resume']


def test_incomplete_warmup_is_resumable_without_claiming_source_origin(source):
    native, anchor, frame, run=source
    native.return_value=(anchor, {'source_origin':False,'target_reached':False,
                                 'can_resume':True,'progress_token':'partial'})
    first=run()
    assert first.coverage['reason']=='page_budget' and first.coverage['can_resume']
    native.return_value=(frame, {'complete':True,'source_origin':True,'can_resume':False,'progress_token':'complete'})
    second=run(previous=first)
    assert second.coverage['source_origin'] and second.coverage['anchor_complete']


def test_missing_minutes_cannot_become_an_endless_preparation_loop(source):
    native, anchor, _, run=source
    native.return_value=(anchor, {'reason':'minute_contract_error', 'can_resume':False,
                                 'source_origin':False, 'target_reached':False})
    result=run()
    assert result.coverage['reason']=='contract_error'
    assert not result.coverage['compatible_with_anchor'] and not result.coverage['can_resume']


def test_known_old_origin_is_not_completion_when_current_tail_validation_times_out(source):
    native, _, frame, run=source
    native.return_value=(frame, {'complete':False,'reason':'deadline','source_origin':True,
                                'can_resume':True,'progress_token':'same-old-prefix'})
    result=run()
    assert result.coverage['reason']=='deadline' and result.coverage['can_resume']


def test_history_backfill_keeps_background_quote_priority(source):
    from chanlun.exchange.lb_priority import quote_request_priority
    native, _, _, run=source
    result=native.return_value
    def observe(*args,**kwargs):
        assert quote_request_priority()=='background'
        return result
    native.side_effect=observe
    before=quote_request_priority()
    run()
    assert quote_request_priority()==before
