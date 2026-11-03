"""First-center admission follows causal history, not a resettable scalar."""
from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest

from chanlun.screening import first_center_sequence as sequence
from cl_app.services.screening_policy import selection_policy_reasons
from cl_app.services.screening import ScreeningManager
from chanlun.screening.evidence import load_evidence, evidence_native_signal_catalog

FIXTURES = Path(__file__).parents[1] / 'fixtures/first_center_sequence'
CASES = json.loads((FIXTURES / 'cases.json').read_text(encoding='utf-8'))


def chain(values, monkeypatch):
    centers, points = [], []
    for index, (low, high, direction) in enumerate(values):
        identity = 'c' + str(index)
        centers.append({'center_id': identity, 'points': [{'time': 100 * (index + 1)}],
                        'envelope': {'dd_tick': low, 'gg_tick': high}})
        points.append({'point_id': 'p' + str(index), 'center_id': identity,
                       'point_type': '3buy' if direction == 'up' else '3sell',
                       'side': 'buy' if direction == 'up' else 'sell', 'status': 'confirmed',
                       'available_at': 100 * (index + 1) + 40, 'center_ordinal': 1,
                       'evidence_scope': 'native_center_signal'})
    snapshot = {'symbol': 'fixture', 'source_frequency': '5m', 'source_closed_at': 10000,
                'analysis_scope': 'native_centers', 'screening_segments': [],
                'levels': [{'centers': centers, 'points': points}]}
    monkeypatch.setattr(sequence, '_native_center_body_source_valid', lambda *_: True)
    return snapshot, {p['point_id']: True for p in points}


@pytest.mark.parametrize('mirrored', [False, True])
def test_overlap_or_recomposition_does_not_restart_the_directional_sequence(monkeypatch, mirrored):
    values = [(200, 220, 'down'), (160, 180, 'down'), (165, 195, 'up'), (190, 215, 'up'), (188, 230, 'up')]
    if mirrored:
        values = [(-high, -low, 'up' if direction == 'down' else 'down') for low, high, direction in values]
    snapshot, valid = chain(values, monkeypatch)
    catalog = sequence.CenterSequences(snapshot, valid)
    points = snapshot['levels'][0]['points']
    first = catalog.assess(points[2])
    assert sequence.first_verified(first)
    assert first['ordinal'] == 1
    for index in [3, 4]:
        result = catalog.assess(points[index])
        assert result['state'] == 'later' and result['ordinal'] == index - 1
        assert not sequence.first_verified(result)


def test_one_opposite_departure_is_not_enough_to_fabricate_a_new_origin(monkeypatch):
    snapshot, valid = chain([(200, 220, 'down'), (160, 180, 'down'), (165, 195, 'up'),
                             (150, 160, 'down'), (160, 180, 'up')], monkeypatch)
    result = sequence.CenterSequences(snapshot, valid).assess(snapshot['levels'][0]['points'][-1])
    assert result['state'] == 'later' and result['ordinal'] == 3


def test_a_new_verified_opposite_sequence_can_begin_a_new_first_center(monkeypatch):
    snapshot, valid = chain([(200, 220, 'down'), (160, 180, 'down'), (165, 195, 'up'),
                             (150, 160, 'down'), (120, 140, 'down'), (125, 150, 'up')], monkeypatch)
    point = snapshot['levels'][0]['points'][-1]
    result = sequence.CenterSequences(snapshot, valid).assess(point)
    assert sequence.first_verified(result)
    assert result['origin_witness']['center_ids'] == ['c3', 'c4']
    assert selection_policy_reasons(point, result) == []


def test_truncated_history_does_not_mean_first_even_if_scalar_says_one(monkeypatch):
    snapshot, valid = chain([(100, 120, 'up'), (130, 150, 'up')], monkeypatch)
    point = snapshot['levels'][0]['points'][0]
    result = sequence.CenterSequences(snapshot, valid).assess(point)
    assert result['state'] == 'unverified' and result['ordinal'] is None
    assert selection_policy_reasons(point, result) == ['THIRD_BUY_CENTER_SEQUENCE_UNKNOWN']


@pytest.mark.parametrize('damage', ['body', 'future_departure', 'missing_departure', 'bad_geometry'])
def test_gaps_or_future_evidence_cannot_reset_to_first(monkeypatch, damage):
    snapshot, valid = chain([(200, 220, 'down'), (160, 180, 'down'), (165, 195, 'up')], monkeypatch)
    points = snapshot['levels'][0]['points']
    if damage == 'body':
        monkeypatch.setattr(sequence, '_native_center_body_source_valid', lambda snapshot, center, *_: center['center_id'] != 'c1')
    elif damage == 'future_departure':
        points[0]['available_at'] = points[-1]['available_at'] + 1
    elif damage == 'missing_departure':
        valid[points[0]['point_id']] = False
    else:
        snapshot['levels'][0]['centers'][0]['points'] = []
    result = sequence.CenterSequences(snapshot, valid).assess(points[-1])
    assert result['state'] == 'unverified'
    assert not sequence.first_verified(result)


def test_centers_after_the_candidate_clock_cannot_rewrite_its_first_status(monkeypatch):
    snapshot, valid = chain([(200, 220, 'down'), (160, 180, 'down'), (165, 195, 'up')], monkeypatch)
    point = snapshot['levels'][0]['points'][-1]
    before = sequence.CenterSequences(snapshot, valid).assess(point)
    snapshot['levels'][0]['centers'].append({'center_id': 'future', 'points': [{'time': 9999}]})
    assert sequence.CenterSequences(snapshot, valid).assess(point) == before


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['code'])
def test_real_published_third_buys_are_checked_against_frozen_history(case, tmp_path, monkeypatch):
    # Isolate the archived first-center geometry under its recorded rule IDs.
    # These MACD-v6 fixtures must not be relabelled as new HTF evidence; current
    # indicator admission and old-rule membership have independent tests.
    from chanlun.core.strict_structure import native_point_preview, native_center_signals
    from chanlun.screening import rules
    from cl_app.services import screening
    monkeypatch.setattr(native_point_preview,'PREVIEW_RULE','native_point_unconfirmed_v1')
    for module in (native_center_signals,rules,screening):
        monkeypatch.setattr(module,'NATIVE_SIGNAL_RULE_BASIS','user_declared_native_center_macd_v6')
    directory = tmp_path / ('a' * 32)
    evidence = directory / 'evidence'
    evidence.mkdir(parents=True)
    for suffix in ('.parquet', '.json.gz'):
        name = case['code'] + '_5m' + suffix
        shutil.copy2(FIXTURES / name, evidence / name)
    _, snapshot, _ = load_evidence(directory, case['code'], '5m', case['manifest'])
    _, _, valid = evidence_native_signal_catalog(directory, case['code'], '5m', case['manifest'],
                                                source_closed_at=case['source_closed_at'])
    point = case['candidate']['point']
    result = sequence.CenterSequences(snapshot, valid).assess(point)
    if case['code'] == 'SH.688349':
        assert sequence.first_verified(result)
    else:
        assert result['state'] == 'later'
        assert result['minimum_ordinal'] == (3 if case['code'] == 'SZ.301119' else 2)
        assert selection_policy_reasons(point, result) == ['THIRD_BUY_NOT_FIRST_UP_CENTER']
    if case['code'] == 'SZ.001217':
        assert point['center_ordinal'] == 1 and result['ordinal'] == 2
    if case['code'] == 'SZ.301119':
        assert point['status'] == 'approaching'
        assert point['center_ordinal'] == 1 and result['ordinal'] == 3
        assert result['preceding_center_ids'] == [
            'ebc21748d50ccde97057cc2f084c8ff6ac32c747ff6a796cc54d8e89d3c88639',
            '0bd3cd92cec93eb6ce999180542ad90a39ba987ed16c5037711fb0e3f7038c15']
    candidate = deepcopy(case['candidate'])
    # This reader test isolates first-center admission. Nested 1m confirmation
    # has separate tests; the original frozen fixture retains that metadata.
    if case.get('group') == 'observations':
        candidate['reasons'] = ['NOT_CONFIRMED']
    row = {'code': case['code'], 'market': 'a', 'frequency': '5m', 'name': 'fixture',
           'source_closed_at': case['source_closed_at'], 'evidence': case['manifest'],
           'selected': [], 'observations': [], 'recent_rejections': [], 'reason_counts': {}}
    row[case.get('group', 'selected')] = [candidate]
    (directory / 'results.jsonl').write_text(json.dumps({'code': case['code'], 'rows': [row]}) + '\n', encoding='utf-8')
    read = ScreeningManager(tmp_path, warm_results=False)._read_results(directory,
        {'completed': 1, 'settings': {'strategy': 'direct_structural', 'alert_mode': 'native_center_signals'}})
    assert not read['errors']
    assert len(read['selected']) == int(case['code'] == 'SH.688349')
    assert not read['observations']
    assert read['selection_policy_exclusion_count'] == int(case['code'] != 'SH.688349')
    if read['selected']:
        assert read['selected'][0]['center_sequence'] == result
        from cl_app.services.screening_workbench import _compact_rows
        compact = _compact_rows([read['selected'][0]], True)[0]
        assert compact['center_sequence'] == result
        assert compact['point']['center_id'] == result['center_id']
        assert compact['point']['available_at'] == result['known_at']
    else:
        assert read['recent_rejections'][0]['reasons'] == ['THIRD_BUY_NOT_FIRST_UP_CENTER']
