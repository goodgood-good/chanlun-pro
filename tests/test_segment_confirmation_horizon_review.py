import pytest

from script.review_segment_confirmation_horizon import inspect_row


def sample(points, mirror=False):
    if mirror:
        points = [-v for v in points]
    return [{'index': i, 'start_tick': a, 'end_tick': b, 'locked': True,
             'selection_pending': False, 'confirmed_at': i + 1, 'selected_at': i + 1}
            for i, (a, b) in enumerate(zip(points, points[1:]))]


@pytest.mark.parametrize('mirror', [False, True])
def test_second_fractal_beyond_drawn_endpoint_is_still_examined(mirror):
    pens = sample([0, 10, 6, 18, 13, 16, 11, 14, 12, 17, 8, 20], mirror)
    row = {'number': 1, 'start_pen': 0, 'end_pen': 4, 'start_tick': 0,
           'direction': 'down' if mirror else 'up', 'confirmed': True,
           'saved_confirmed_at': 9, 'proof': {'witness_index': 8}}
    result = inspect_row(row, {'decisions': []}, pens)
    item = next(c for c in result['candidates'] if c['family'] == 'standard' and c['end_pen'] == 2)
    assert item['first_witness'] == 5 and item['second_outcome']['witness'] == 8
    assert item['first_witness_after_native_endpoint'] and item['second_witness_after_native_endpoint']
    assert item['resolution'] == 'needs_individual_reference_and_priority_review'


@pytest.mark.parametrize('mirror', [False, True])
def test_later_second_fractal_cannot_retroactively_qualify(mirror):
    pens = sample([0, 10, 6, 18, 13, 16, 11, 14, 12, 17, 8, 20], mirror)
    row = {'number': 1, 'start_pen': 0, 'end_pen': 4, 'start_tick': 0,
           'direction': 'down' if mirror else 'up', 'confirmed': True,
           'saved_confirmed_at': 8, 'proof': {'witness_index': 7}}
    result = inspect_row(row, {'decisions': []}, pens)
    item = next(c for c in result['candidates'] if c['family'] == 'standard' and c['end_pen'] == 2)
    assert item['resolution'] == 'no_second_fractal_before_saved_endpoint'
    assert result['horizon'] == 7


@pytest.mark.parametrize('mirror', [False, True])
def test_original_extension_before_f2_invalidates_the_candidate(mirror):
    pens = sample([0, 10, 6, 18, 13, 16, 11, 14, 10, 20, 12, 19], mirror)
    row = {'number': 1, 'start_pen': 0, 'end_pen': 6, 'start_tick': 0,
           'direction': 'down' if mirror else 'up', 'confirmed': True,
           'saved_confirmed_at': 11, 'proof': {'witness_index': 10}}
    item = next(c for c in inspect_row(row, {'decisions': []}, pens)['candidates'] if c['family'] == 'standard' and c['end_pen'] == 2)
    assert item['resolution'] == 'invalidated_before_second_fractal'


@pytest.mark.parametrize('mirror', [False, True])
def test_claimed_parent_link_cannot_hide_an_unexplained_candidate(mirror):
    pens = sample([0, 10, 6, 18, 13, 16, 11, 14, 12, 17, 8, 20], mirror)
    row = {'number': 2, 'start_pen': 0, 'end_pen': 4, 'start_tick': 0,
           'direction': 'down' if mirror else 'up', 'confirmed': True,
           'saved_confirmed_at': 9,
           'proof': {'witness_index': 8, 'parent_key': [-3, -1, 'up'], 'first_sequence': ['claimed']}}
    previous = {'confirmed': True, 'proof': {'start_index': -3, 'end_index': -1,
                'direction': 'up', 'second_sequence': ['different_actual_source']}}
    item = next(c for c in inspect_row(row, {'decisions': []}, pens, previous)['candidates']
                if c['family'] == 'standard' and c['end_pen'] == 2)
    assert item['resolution'] == 'needs_individual_reference_and_priority_review'
    assert not item['context'][0]['parent_link_checked']


@pytest.mark.parametrize('mirror', [False, True])
def test_unverified_invalidation_trace_cannot_hide_a_valid_second_fractal(mirror):
    pens = sample([0, 10, 6, 18, 13, 16, 11, 14, 12, 17, 8, 20], mirror)
    row = {'number': 1, 'start_pen': 0, 'end_pen': 4, 'start_tick': 0,
           'direction': 'down' if mirror else 'up', 'confirmed': True,
           'saved_confirmed_at': 9, 'proof': {'witness_index': 8}}
    decisions = [{'start': 0, 'check': 3, 'candidate_index': 2,
                  'kind': '_GapConfirmationInvalidated', 'witness_index': 8}]
    item = next(c for c in inspect_row(row, {'decisions': decisions}, pens)['candidates']
                if c['family'] == 'standard' and c['end_pen'] == 2)
    assert item['resolution'] == 'needs_individual_reference_and_priority_review'
    assert not item['context']


@pytest.mark.parametrize('mirror', [False, True])
def test_selection_not_yet_available_cannot_be_used_at_claimed_close(mirror):
    pens = sample([0, 10, 6, 18, 13, 16, 11, 14, 12, 17, 8, 20], mirror)
    pens[8]['selected_at'] = 10
    row = {'number': 1, 'start_pen': 0, 'end_pen': 4, 'start_tick': 0,
           'direction': 'down' if mirror else 'up', 'confirmed': True,
           'saved_confirmed_at': 9, 'proof': {'witness_index': 8}}
    result = inspect_row(row, {'decisions': []}, pens)
    assert result['horizon'] == 7
    assert result['errors'] == ['native_confirmation_uses_unavailable_pen']
    item = next(c for c in result['candidates'] if c['family'] == 'standard' and c['end_pen'] == 2)
    assert item['resolution'] == 'no_second_fractal_before_saved_endpoint'
