from dataclasses import replace

import pytest

from chanlun.core.strict_structure.models import CenterState, SourceKind
from chanlun.core.strict_structure.recursive_engine import StrictRecursiveEngine
from tests.core.strict_structure.helpers import valid_five_up_exit
from tests.core.strict_structure.helpers import unit


def test_five_locked_units_form_level_zero_with_v3_schema():
    result = StrictRecursiveEngine(max_levels=4).calculate(valid_five_up_exit())
    assert result.schema_version == "chanlun-structure/v3"
    assert len(result.levels) == 1
    assert result.levels[0].structural_level == 0
    assert result.levels[0].center_result.centers
    assert all(
        item.source_kind is SourceKind.SEGMENT for item in result.levels[0].units
    )


def test_recursion_requires_five_locked_inputs_at_every_level():
    result = StrictRecursiveEngine(max_levels=8).calculate(valid_five_up_exit()[:4])
    assert result.levels == ()

    one_level = StrictRecursiveEngine(max_levels=8).calculate(valid_five_up_exit())
    assert len(one_level.levels) == 1
    assert not any(trend.locked for trend in one_level.levels[0].trend_types)


def test_recursion_rejects_mixed_price_basis():
    values = valid_five_up_exit()
    mixed = values[:-1] + (
        replace(values[-1], price_basis_revision="another-basis"),
    )

    with pytest.raises(ValueError, match="cannot cross price basis"):
        StrictRecursiveEngine().calculate(mixed)


def test_empty_recursion_requires_explicit_basis_and_has_no_levels():
    with pytest.raises(ValueError, match="empty strict recursion requires price basis"):
        StrictRecursiveEngine().calculate(())

    result = StrictRecursiveEngine().calculate(
        (),
        price_basis_revision="test-raw-v1",
    )
    assert result.schema_version == "chanlun-structure/v3"
    assert result.levels == ()
    assert result.price_basis_revision == "test-raw-v1"


def test_recursive_engine_rejects_invalid_level_limit():
    for invalid in (0, -1, True, 1.5):
        with pytest.raises(ValueError, match="max_levels must be >= 1"):
            StrictRecursiveEngine(max_levels=invalid)


def test_max_levels_is_a_hard_structural_depth_cap():
    result = StrictRecursiveEngine(max_levels=1).calculate(valid_five_up_exit())
    assert len(result.levels) == 1


def test_direction_flip_completion_keeps_all_centers_in_trends():
    values = valid_five_up_exit() + (
        unit(5, "down", 130, 95),
        unit(6, "up", 95, 100),
        unit(7, "down", 100, 96),
        unit(8, "up", 96, 99),
        unit(9, "down", 99, 97),
        unit(10, "up", 97, 105),
        unit(11, "down", 105, 101),
    )
    result = StrictRecursiveEngine(max_levels=1).calculate(values)
    level = result.levels[0]
    assert len(level.center_result.centers) == 2
    assert all(
        center.state is CenterState.COMPLETED
        for center in level.center_result.centers
    )
    owned_centers = tuple(
        center for trend in level.trend_types for center in trend.centers
    )
    assert owned_centers == level.center_result.centers
