import pytest

from script.review_segment_direct_breaks import questions
from script.check_segment_candidates import local_first_fractal
from script.check_segment_model import pen


def fixture(points, mirror=False):
    points = [-v for v in points] if mirror else points
    pens = [{'index': i, 'start_tick': a, 'end_tick': b, 'start_time': i,
             'locked': True, 'selection_pending': False, 'confirmed_at': i + 1, 'selected_at': i + 1}
            for i, (a, b) in enumerate(zip(points, points[1:]))]
    row = {'start_pen': 0, 'end_pen': 2, 'start_tick': points[0], 'direction': 'up' if mirror else 'down',
           'confirmation_horizon': len(pens) - 1, 'confirmed': False, 'saved_confirmed_at': None}
    previous = {'number': 1, 'confirmed': True, 'saved_confirmed_at': 1}
    return row, pens, previous


@pytest.mark.parametrize('mirror', [False, True])
def test_direct_break_is_reviewed_even_without_a_new_local_extreme(mirror):
    row, pens, previous = fixture([12, 7, 10, 8, 13, 9, 14], mirror)
    values = [pen(i, p['start_tick'], p['end_tick']) for i, p in enumerate(pens)]
    assert local_first_fractal(values, 0, 3, 5) is None
    found = questions(row, pens, previous)
    assert len(found) == 1
    assert (found[0]['end_pen'], found[0]['witness'], found[0]['dependencies_available_at']) == (2, 5, 6)


@pytest.mark.parametrize('points', [[12, 7, 10, 8, 13, 7, 14], [12, 7, 10, 8, 13, 9, 12]])
@pytest.mark.parametrize('mirror', [False, True])
def test_three_pens_must_extend_without_first_breaking_the_origin(points, mirror):
    row, pens, previous = fixture(points, mirror)
    assert not questions(row, pens, previous)


def test_unconfirmed_predecessor_and_unclosed_evidence_do_not_qualify():
    row, pens, previous = fixture([12, 7, 10, 8, 13, 9, 14])
    assert not questions(row, pens, {**previous, 'confirmed': False})
    assert not questions(row, pens, {**previous, 'saved_confirmed_at': 5})
    pens[-1]['confirmed_at'] = None
    assert not questions(row, pens, previous)


def test_matching_native_confirmation_is_not_reported_as_a_question():
    row, pens, previous = fixture([12, 7, 10, 8, 13, 9, 14])
    row.update(confirmed=True, saved_confirmed_at=6)
    assert not questions(row, pens, previous)
