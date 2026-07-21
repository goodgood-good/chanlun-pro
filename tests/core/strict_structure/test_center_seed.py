from dataclasses import replace

import pytest

from chanlun.core.strict_structure.center_machine import (
    establish_center,
    forming_preview,
)
from chanlun.core.strict_structure.models import (
    CenterPreviewState,
    CenterState,
    SourceKind,
)
from tests.core.strict_structure.helpers import (
    TEST_PRICE_BASIS,
    unit,
    valid_five_up_exit,
)


def invalid_initial_five(mutation):
    values = {
        "middle_has_no_positive_core": (
            unit(0, "up", 90, 120),
            unit(1, "down", 120, 100),
            unit(2, "up", 100, 130),
            unit(3, "down", 130, 121),
            unit(4, "up", 121, 140),
        ),
        "entry_has_no_positive_overlap": (
            unit(0, "up", 80, 90),
            unit(1, "down", 90, 70),
            unit(2, "up", 70, 78),
            unit(3, "down", 78, 72),
            unit(4, "up", 72, 95),
        ),
        "exit_has_no_positive_overlap": (
            unit(0, "down", 130, 100),
            unit(1, "up", 100, 120),
            unit(2, "down", 120, 90),
            replace(unit(3, "up", 90, 95), high_tick=110),
            unit(4, "down", 95, 80),
        ),
        "exit_endpoint_not_outside": (
            unit(0, "up", 90, 120),
            unit(1, "down", 120, 100),
            unit(2, "up", 100, 115),
            unit(3, "down", 115, 105),
            unit(4, "up", 105, 110),
        ),
    }
    return values[mutation]


def test_five_locked_units_establish_ongoing_center_with_middle_core():
    initial = valid_five_up_exit()
    value = establish_center(initial, 0, SourceKind.SEGMENT)
    assert value is not None
    assert value.state is CenterState.ONGOING
    assert value.initial_units == initial
    assert value.entry_unit is initial[0]
    assert value.core_units == initial[1:4]
    assert value.initial_exit_unit is initial[4]
    assert value.pending_leave_unit is initial[4]
    assert (value.zd_tick, value.zg_tick) == (105, 115)
    assert value.established_at == initial[4].confirmed_at
    assert value.body_units == initial
    assert value.price_basis_revision == TEST_PRICE_BASIS


def test_three_or_four_locked_units_never_establish_formal_center():
    initial = valid_five_up_exit()
    assert establish_center(initial[:3], 0, SourceKind.SEGMENT) is None
    assert establish_center(initial[:4], 0, SourceKind.SEGMENT) is None


@pytest.mark.parametrize(
    "mutation",
    (
        "middle_has_no_positive_core",
        "entry_has_no_positive_overlap",
        "exit_has_no_positive_overlap",
        "exit_endpoint_not_outside",
    ),
)
def test_initial_five_reject_each_geometric_violation(mutation):
    assert (
        establish_center(
            invalid_initial_five(mutation),
            0,
            SourceKind.SEGMENT,
        )
        is None
    )


def test_entry_touching_core_boundary_has_no_positive_overlap():
    values = (
        unit(0, "up", 90, 105),
        replace(unit(1, "down", 105, 100), high_tick=120),
        unit(2, "up", 100, 115),
        unit(3, "down", 115, 105),
        unit(4, "up", 105, 130),
    )
    assert establish_center(values, 0, SourceKind.SEGMENT) is None


def test_initial_exit_touching_core_boundary_has_no_positive_overlap():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 115),
        replace(unit(3, "down", 115, 115), low_tick=105),
        unit(4, "up", 115, 130),
    )
    assert establish_center(values, 0, SourceKind.SEGMENT) is None


def test_unlocked_initial_exit_is_preview_only():
    initial = valid_five_up_exit()
    active = initial[:-1] + (
        replace(initial[-1], locked=False, confirmed_at=None),
    )
    assert establish_center(active, 0, SourceKind.SEGMENT) is None
    preview = forming_preview(active, 0, SourceKind.SEGMENT)
    assert preview is not None
    assert preview.state is CenterPreviewState.FORMING
    assert preview.price_basis_revision == TEST_PRICE_BASIS
    assert not hasattr(preview, "center_id")


def test_zero_width_middle_intersection_is_touch_only_observation():
    values = (
        unit(0, "up", 90, 120),
        unit(1, "down", 120, 100),
        unit(2, "up", 100, 130),
        unit(3, "down", 130, 120),
        unit(4, "up", 120, 140),
    )
    assert establish_center(values, 0, SourceKind.SEGMENT) is None
    preview = forming_preview(values, 0, SourceKind.SEGMENT)
    assert preview is not None
    assert preview.state is CenterPreviewState.TOUCH_ONLY
    assert preview.zd_tick == preview.zg_tick == 120


def test_center_identity_namespace_includes_price_basis_revision():
    initial = valid_five_up_exit()
    original = establish_center(initial, 0, SourceKind.SEGMENT)
    rebased_initial = tuple(
        replace(item, price_basis_revision="post-action-v2") for item in initial
    )
    rebased = establish_center(rebased_initial, 0, SourceKind.SEGMENT)
    assert original is not None and rebased is not None
    assert original.center_id != rebased.center_id


def test_initial_units_reject_mixed_basis_instead_of_squeezing_it_into_center():
    initial = valid_five_up_exit()
    mixed = initial[:-1] + (
        replace(initial[-1], price_basis_revision="post-action-v2"),
    )
    with pytest.raises(ValueError, match="seed price basis mismatch"):
        establish_center(mixed, 0, SourceKind.SEGMENT)
    with pytest.raises(ValueError, match="seed price basis mismatch"):
        forming_preview(mixed, 0, SourceKind.SEGMENT)
