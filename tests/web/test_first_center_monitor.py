"""Queued native third buys must use current sequence proof at dispatch."""
from copy import deepcopy

import pytest

from cl_app.services.first_center_sequence import SCHEMA
from cl_app.services.notification_text import signal_status_lines
from cl_app.services.signal_monitor import _signal
from tests.web.test_notification_state_freshness import ready_event as ready_event, setup as setup


def first_proof(known):
    return {'schema': SCHEMA, 'state': 'first', 'ordinal': 1, 'minimum_ordinal': 1,
            'direction': 'up', 'center_id': 'center', 'origin_center_id': 'center',
            'known_at': known, 'preceding_center_ids': [],
            'origin_witness': {'kind': 'opposite_departure_and_separated_centers',
                               'center_ids': ['previous', 'center'],
                               'departure_point_id': 'departure', 'departure_available_at': known - 20}}


@pytest.mark.parametrize('proof_state', ['missing', 'malformed', 'old', 'later', 'unverified', 'first',
                                       'wrong_center', 'wrong_clock', 'wrong_direction'])
def test_old_queued_third_buy_uses_current_sequence_proof(ready_event, proof_state):
    service, _live, event = ready_event
    item = service._load()['signals'][event['id']]
    point_clock = item['available_at'] - 10
    item.update(evidence_scope='native_center_signal', center_id='center', point_available_at=point_clock)
    proof = first_proof(point_clock)
    if proof_state == 'missing':
        proof = None
    elif proof_state == 'malformed':
        proof = 'damaged persisted metadata'
    elif proof_state == 'old':
        proof['schema'] = 'old'
    elif proof_state == 'later':
        proof.update(state='later', ordinal=2)
    elif proof_state == 'unverified':
        proof.update(state='unverified', ordinal=None)
    elif proof_state == 'wrong_center':
        proof['center_id'] = 'other'
    elif proof_state == 'wrong_clock':
        proof['known_at'] -= 1
    elif proof_state == 'wrong_direction':
        proof['direction'] = 'down'
    item['center_sequence'] = proof
    # The enqueued event predates this proof. Dispatch must reload the current
    # item and distinguish the point clock from the later 1m trade clock.
    valid, pending = service._validate_notification_events([event])
    if proof_state == 'first':
        assert len(valid) == 1 and not pending
        assert valid[0]['center_sequence'] == proof
        assert '前序证据已核验' in '\n'.join(signal_status_lines(valid[0]))
    else:
        assert not valid
        assert pending == ([event] if proof_state in {'missing', 'malformed', 'old'} else [])
        assert '前序证据已核验' not in '\n'.join(signal_status_lines(item))


def test_signal_preserves_authoritative_sequence_and_both_clocks():
    proof = first_proof(100)
    row = {'code': 'fixture', 'frequency': '5m', 'point': {
        'point_type': '3buy', 'point_id': 'point', 'center_id': 'center',
        'status': 'confirmed', 'available_at': 100, 'center_ordinal': 99,
        'evidence_scope': 'native_center_signal'},
        'timeframe_policy': 'test', 'selection_available_at': 120,
        'center_sequence': proof}
    original = deepcopy(row)
    event = _signal(row)
    assert event['center_ordinal'] == 1
    assert event['point_available_at'] == 100 and event['available_at'] == 120
    assert event['center_sequence'] == proof and event['center_sequence'] is not proof
    assert row == original
