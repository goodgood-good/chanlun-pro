"""An audited pending reference is a comparison source, not a body mutation."""
from copy import deepcopy
from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import advance_center, calculate_centers
from chanlun.core.strict_structure.models import SourceKind
from chanlun.screening.rules import native_consolidation_source_valid
from tests.core.strict_structure.helpers import ongoing_center, unit
from tests.core.strict_structure.test_native_consolidation import Areas
from tests.core.strict_structure.test_native_consolidation_preview import sample, build, snapshot


def pending_sample(down=False):
    center = ongoing_center()
    bridge = unit(5, 'down', 130, 100)
    center, _ = advance_center(center, bridge)
    values = (*center.establishment_units, bridge, unit(6, 'up', 100, 125, locked=False))
    _, _, _, frame, histogram = sample()
    if down:
        values = tuple(replace(value, direction='down' if value.direction == 'up' else 'up',
            start_tick=300-value.start_tick, end_tick=300-value.end_tick,
            low_tick=300-value.high_tick, high_tick=300-value.low_tick) for value in values)
        center = calculate_centers(values[:-1], 0, SourceKind.SEGMENT).centers[0]
        frame['open'], frame['close'] = 300-frame.open, 300-frame.close
        frame['high'], frame['low'] = 300-frame.low.copy(), 300-frame.high.copy()
        histogram = [-value for value in histogram]
    return values, center, Areas({value.unit_id:200. for value in values}), frame, histogram


@pytest.mark.parametrize('down', [False, True])
def test_generated_pending_bridge_previews_survive_the_reader_without_reclassifying_body(down):
    parts = pending_sample(down)
    alerts = build(parts)
    assert {alert['comparison_form'] for alert in alerts} == {'entry_departure', 'center_oscillation'}
    source = snapshot(parts, alerts)
    center = source['levels'][0]['centers'][0]
    pending = center['pending_leave_unit_id']
    assert pending == parts[0][-2].unit_id
    assert pending not in center['frame_body_unit_ids']
    assert pending not in center['body_unit_ids']
    assert pending not in center['extension_unit_ids']
    before = deepcopy(source)
    assert all(native_consolidation_source_valid(source, alert) for alert in alerts)
    assert source == before
    assert all(alert['status'] == 'warning' and alert['confirmed_at'] is None
               and 'point_type' not in alert for alert in alerts)


@pytest.mark.parametrize('fault', ['missing_reference', 'foreign_reference', 'wrong_existing_reference', 'future', 'unlocked', 'disconnected', 'basis'])
@pytest.mark.parametrize('form', ['entry_departure', 'center_oscillation'])
def test_pending_reference_cannot_bypass_the_existing_source_chain_proof(fault, form):
    parts = pending_sample()
    alerts = build(parts)
    source = snapshot(parts, alerts)
    alert = next(value for value in alerts if value['comparison_form'] == form)
    center = source['levels'][0]['centers'][0]
    bridge = next(value for value in source['screening_segments'] if value['unit_id'] == center['pending_leave_unit_id'])
    if fault == 'missing_reference':center['pending_leave_unit_id'] = None
    elif fault == 'foreign_reference':center['pending_leave_unit_id'] = 'not-in-the-source-chain'
    elif fault == 'wrong_existing_reference':center['pending_leave_unit_id'] = center['entry_unit_id']
    elif fault == 'future':bridge['available_at'] = source['source_closed_at']+1
    elif fault == 'unlocked':bridge['locked'] = False
    elif fault == 'disconnected':bridge['start_tick'] += 1
    elif fault == 'basis':bridge['price_basis_revision'] = 'different'
    assert not native_consolidation_source_valid(source, alert)
