"""Five physical roles share a price interval and retain unresolved suffixes.

These are the user's explicit operational requirements. The SH.513100 fixture
provides market evidence; it is not an author-labelled Chan illustration.
"""
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from chanlun.cl_utils.strict_chart_runtime import build_strict_chart_cd
from chanlun.core.strict_structure.center_machine import (
    advance_center,
    calculate_centers,
    establish_center,
    establish_center_preview,
    forming_preview,
)
from chanlun.core.strict_structure.models import CenterState, SourceKind
from tests.core.strict_structure.helpers import unit, valid_five_up_exit


def separate_contacts(*, mirror=False, source_kind=SourceKind.SEGMENT):
    # Every role overlaps core [2,5]. Entry reaches only [2,3] and leave
    # starts at4; their five-way intersection is empty despite both contacts.
    points = (0, 2, 1, 6, 4, 7)
    values = [unit(i, "up" if b > a else "down", a, b, source_kind=source_kind)
              for i, (a, b) in enumerate(zip(points, points[1:]))]
    values[0] = replace(values[0], high_tick=3)
    values[1] = replace(values[1], high_tick=5)
    values[3] = replace(values[3], low_tick=2)
    if mirror:
        values = [replace(u, direction="down" if u.direction == "up" else "up",
                          start_tick=10-u.start_tick, end_tick=10-u.end_tick,
                          low_tick=10-u.high_tick, high_tick=10-u.low_tick)
                  for u in values]
    return tuple(values)


@pytest.mark.parametrize("mirror", [False, True])
@pytest.mark.parametrize("source_kind", [SourceKind.SEGMENT, SourceKind.STROKE_OBSERVATION])
def test_separate_core_contacts_are_not_five_way_overlap(mirror, source_kind):
    values = separate_contacts(mirror=mirror, source_kind=source_kind)
    assert max(u.low_tick for u in values) > min(u.high_tick for u in values)
    assert establish_center(values, 0, source_kind) is None
    assert forming_preview(values, 0, source_kind) is None
    live = (*values[:-1], replace(values[-1], locked=False, confirmed_at=None))
    assert establish_center_preview(live, 0, source_kind) is None
    assert forming_preview(live, 0, source_kind) is None


def test_model_cannot_bypass_five_way_overlap_with_separate_contacts():
    invalid = separate_contacts()
    valid = (replace(invalid[0], high_tick=5), *invalid[1:])
    center = establish_center(valid, 0, SourceKind.SEGMENT)
    assert center is not None
    # The common five-role interval [4,5] must not narrow the middle-three
    # center core [2,5]. The two calculations have different meanings.
    assert (center.zd_tick, center.zg_tick) == (2, 5)
    with pytest.raises(ValueError, match="five physical establishment roles must share"):
        replace(center, entry_unit=invalid[0])


def test_completed_five_role_center_does_not_revive_on_later_core_contact():
    seed = valid_five_up_exit()
    ret = unit(5, "down", 130, 120)
    ended = calculate_centers((*seed, ret), 0, SourceKind.SEGMENT).centers[0]
    assert ended.state is CenterState.COMPLETED
    growth = (unit(6, "up", 120, 150), unit(7, "down", 150, 110))
    later = calculate_centers((*seed, ret, *growth), 0, SourceKind.SEGMENT)
    assert later.centers[0] == ended
    with pytest.raises(ValueError, match="completed center cannot transition"):
        advance_center(ended, growth[0])


@pytest.fixture(scope="module")
def recorded_units():
    frame = pd.read_parquet(
        Path(__file__).parents[2] / "fixtures/SH.513100_5m_center_20261009.parquet"
    )
    result = build_strict_chart_cd(market="a", code="SH.513100", frequency="5m",
                                   frame=frame, last_bar_closed=True)
    assert result.cd is not None
    return tuple(result.cd.get_segment_units())


def test_recorded_target_requires_all_five_roles_and_keeps_middle_core(recorded_units):
    values = recorded_units[32:37]
    for count in (3, 4):
        assert establish_center(values[:count], 0, SourceKind.SEGMENT) is None
    assert establish_center((*values[:-1], replace(values[-1], locked=False, confirmed_at=None)),
                            0, SourceKind.SEGMENT) is None
    center = establish_center(values, 0, SourceKind.SEGMENT)
    assert center is not None
    assert center.entry_unit == recorded_units[32]
    assert center.initial_units == recorded_units[33:36]
    assert center.establishment_leave_unit == recorded_units[36]
    assert (center.zd_tick, center.zg_tick) == (2154, 2232)
    assert center.established_at == pd.Timestamp("2026-07-07 14:10", tz="Asia/Shanghai")
    for ret in recorded_units[37:42]:
        center, _ = advance_center(center, ret)
    assert center.state is CenterState.COMPLETED
    assert center.completion_return_unit == recorded_units[41]
    assert center.completion_available_at == pd.Timestamp("2026-07-28 13:55", tz="Asia/Shanghai")


def test_recorded_five_role_scan_exposes_stop_and_suppresses_suffix_preview(recorded_units):
    result = calculate_centers(recorded_units, 0, SourceKind.SEGMENT)
    stalled = result.centers[-1]
    assert (stalled.zd_tick, stalled.zg_tick) == (2150, 2270)
    assert stalled.state is CenterState.ONGOING
    assert result.unprocessed_unit_ids == tuple(u.unit_id for u in recorded_units[42:49])
    assert stalled not in result.signal_centers
    assert result.signal_centers == result.centers[:-1]
    assert result.previews == ()
