"""A single candidate request must not serialize unrelated candidate evidence."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from cl_app.services import screening_workbench as workbench


def candidate(code, point_id):
    return {'code': code, 'market': 'a', 'frequency': '5m', 'name': code,
            'point': {'point_id': point_id, 'point_type': '3buy', 'status': 'confirmed',
                      'available_at': 100, 'anchor_price': 10},
            'center': {'center_id': 'center-' + point_id},
            'exit_plan': {'stop_loss': {'price': 9}, 'take_profit': {'state': 'await_reverse'}},
            'proof': {'content': list(range(100))}}


@pytest.fixture
def projected(monkeypatch):
    source = 'a' * 32
    first = candidate('SH.600000', 'first')
    chosen = candidate('SZ.000001', 'chosen')
    duplicate = {**chosen, 'name': 'later duplicate'}
    payload = {'selected': [first, chosen], 'observations': [duplicate], 'entry_waiting': []}

    def results(*, project, available):
        assert available is True
        return {'run_id': source, **project(payload)}

    monkeypatch.setattr(workbench, 'manager', SimpleNamespace(results=results))
    monkeypatch.setattr(workbench, '_live_rows', lambda rows, *_: (rows, 0, 0, 0))
    encoded = []
    original = workbench.json.dumps

    def record(value, *args, **kwargs):
        if isinstance(value, dict) and 'point' in value:
            encoded.append(value['code'])
        return original(value, *args, **kwargs)

    expected = workbench._current_rows(payload, source)[1]
    monkeypatch.setattr(workbench.json, 'dumps', record)
    return source, payload, expected, encoded


def test_detail_builds_only_the_requested_candidate_and_keeps_duplicate_precedence(projected):
    source, payload, expected, encoded = projected
    original = deepcopy(payload)
    result = workbench.candidate_detail(source, expected['id'])
    assert result['candidate'] == {**expected, 'detail_loaded': True}
    assert encoded == ['SZ.000001']
    assert payload == original


def test_missing_candidate_does_not_build_unrelated_evidence(projected):
    source, _payload, _expected, encoded = projected
    with pytest.raises(ValueError):
        workbench.candidate_detail(source, 'unknown-candidate')
    assert encoded == []


def test_full_projection_still_retains_all_distinct_candidates(projected):
    source, payload, expected, encoded = projected
    rows = workbench._current_rows(payload, source)
    assert [row['code'] for row in rows] == ['SH.600000', 'SZ.000001']
    assert rows[1] == expected
    assert encoded == ['SH.600000', 'SZ.000001']
