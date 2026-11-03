from copy import deepcopy
from datetime import timedelta

import pandas as pd
import pytest

from chanlun.screening.runner import _save_evidence
from cl_app.services.warning_lifecycle import recheck_warning, invalidation_update
from cl_app.services.signal_monitor import _consolidation_signal
from cl_app.services.notification_charts import load_chart_evidence
from cl_app.services.notification_text import signal_status_lines
from cl_app.services.notification_evidence import NotificationEvidence
from tests.core.strict_structure.test_warning_lifecycle import consolidation_case


def frozen_warning(tmp_path):
    _, source, frame = consolidation_case()
    source['symbol'] = 'SH.600088'
    source['analysis_scope'] = 'native_centers'
    alert = next(a for a in source['native_consolidation_alerts'] if a['comparison_form'] == 'entry_departure')
    alert['anchor_price'] = float(alert['anchor_tick'])
    manifest = _save_evidence(tmp_path / ('a' * 32), 'SH.600088', '5m', frame, source)
    row = {'code':'SH.600088', 'market':'a', 'frequency':'5m', 'alert':alert,
           'source_closed_at':source['source_closed_at'], 'evidence':manifest, 'structure_price_quantum':'1'}
    event = {**_consolidation_signal(row), 'evidence_run_id':'a' * 32, 'lifecycle_cycle':0,
             'first_warned_at':source['source_closed_at'], 'recorded_at':source['source_closed_at']}
    return event, source, frame


def next_period(root, source, frame, *, high=126., momentum=.01, run='b' * 32):
    newer = deepcopy(source)
    extra = frame.iloc[[-1]].copy()
    extra['date'] += timedelta(minutes=5)
    extra['high'] = high
    frame = pd.concat([frame, extra], ignore_index=True)
    newer['source_closed_at'] = int(frame.date.iloc[-1].timestamp())
    newer['chart']['higher_macd_raw_hist'].append(momentum)
    manifest = _save_evidence(root / run, 'SH.600088', '5m', frame, newer)
    return {'code':'SH.600088', 'market':'a', 'frequency':'5m', 'source_closed_at':newer['source_closed_at'],
            'evidence':manifest, 'consolidation_checked':True}, newer, frame


def test_frozen_continuation_moves_candidate_without_repeating_a_warning_and_has_a_verifiable_chart(tmp_path):
    event, source, frame = frozen_warning(tmp_path)
    period, _, _ = next_period(tmp_path, source, frame)
    outcome, current = recheck_warning(event, period, 'b' * 32, tmp_path, {})
    assert outcome['state'] == 'candidate_updated'
    assert current['lifecycle_cycle'] == event['lifecycle_cycle']
    assert current['first_warned_at'] == event['first_warned_at']
    assert current['anchor_price'] == 126. and current['warning_reference']['anchor_price'] == 125.
    assert current['preview_trigger_active'] is False
    text = '\n'.join(signal_status_lines(current))
    assert '候选拐点更新' in text and '同向柱开始缩短' not in text
    _, _, marker = load_chart_evidence(current, tmp_path)
    assert marker['anchor_price'] == 126. and marker['confirmed_at'] is None
    forged = deepcopy(current)
    forged['warning_assessment']['anchor_price'] = 1234.
    with pytest.raises(ValueError, match='lifecycle disagrees'):
        load_chart_evidence(forged, tmp_path)
    archive = NotificationEvidence(tmp_path / 'archive', now=lambda:current['source_closed_at'])
    key = archive.capture(current, tmp_path, current['source_closed_at'])
    record = archive.read(key)
    assert len(record['evidence']) == 2


def test_maintained_warning_later_refutes_on_actual_area_and_chart_rechecks_both_proofs(tmp_path):
    event, source, frame = frozen_warning(tmp_path)
    period, source, frame = next_period(tmp_path, source, frame)
    _, current = recheck_warning(event, period, 'b' * 32, tmp_path, {})
    period, source, frame = next_period(tmp_path, source, frame, momentum=100., run='c' * 32)
    outcome, continued = recheck_warning(current, period, 'c' * 32, tmp_path, {})
    assert continued is None and outcome['reason_code'] == 'AREA_DECAY_REFUTED'
    invalid = {**current, **period, 'evidence_run_id':'c' * 32, 'stage':'invalidated', 'point_status':'invalidated',
               'warning_assessment':outcome, 'invalidation_reference':current,
               'invalidation_reason_code':outcome['reason_code'], 'invalidation_reason':outcome['reason']}
    _, _, marker = load_chart_evidence(invalid, tmp_path)
    assert marker['anchor_at'] == period['source_closed_at']
    archive = NotificationEvidence(tmp_path / 'archive', now=lambda:period['source_closed_at'])
    record = archive.read(archive.capture(invalid, tmp_path, period['source_closed_at']))
    assert len(record['evidence']) == 3
    # A delayed invalidation still needs a fresh, matching negative proof.
    period, source, frame = next_period(tmp_path, source, frame, momentum=100., run='d' * 32)
    outcome, _ = recheck_warning(invalid, period, 'd' * 32, tmp_path, {})
    refreshed = {**invalid, **invalidation_update(outcome, period, 'd' * 32)}
    assert refreshed['invalidation_checked_at'] == refreshed['source_closed_at']
    assert refreshed['source_closed_at'] > invalid['source_closed_at']
    _, _, marker = load_chart_evidence(refreshed, tmp_path)
    assert marker['anchor_at'] == refreshed['source_closed_at']


def test_successful_empty_scan_without_frozen_proof_cannot_cancel_warning(tmp_path):
    event, _, _ = frozen_warning(tmp_path)
    outcome, current = recheck_warning(event, {'source_closed_at':event['source_closed_at'] + 300,
        'point_preview_checked':True, 'evidence':None}, 'f' * 32, tmp_path, {})
    assert outcome['state'] == 'pending' and current is None
