from chanlun.core.bs_branch import BsBranchCalculator
from tests.trading_system.helpers import (
    divergence,
    line,
    weak_second_buy_case,
    zone,
    zone_result,
)


def test_first_point_requires_confirmed_trend_divergence() -> None:
    down = line("down", 0, 10.0, 5, 8.0)
    pz = divergence(down, kind="pz", provisional=False)
    provisional_qs = divergence(down, kind="qs", provisional=True)

    assert BsBranchCalculator().calculate(zone_result(zone(down), pz), [down]) == []
    assert (
        BsBranchCalculator().calculate(
            zone_result(zone(down), provisional_qs),
            [down],
        )
        == []
    )


def test_third_buy_keeps_boundary_touch_as_non_standard_variant() -> None:
    core = zone(line("up", 0, 8.0, 5, 10.0), zg=10.0, zd=9.0)
    leave = core.end
    retest = line("down", 10, 12.0, 15, 10.0)
    points = BsBranchCalculator().calculate(
        zone_result(core, None),
        [*core.lines, leave, retest],
    )

    point = next(item for item in points if item.bs_type == "3buy")
    assert point.definition_variant == "boundary_touch"


def test_second_buy_marks_breaking_pullback_as_weak_divergence() -> None:
    result, lines, provider = weak_second_buy_case()
    points = BsBranchCalculator().second_class(result, lines, provider, "1m")

    assert points[0].bs_type == "2buy"
    assert points[0].definition_variant == "weak_divergence"


def test_third_point_requires_completed_retest() -> None:
    core = zone(line("up", 0, 8.0, 5, 11.0), zg=10.0, zd=9.0)
    retest = line("down", 10, 12.0, 15, 10.5, done=False)

    points = BsBranchCalculator().calculate(
        zone_result(core, None),
        [*core.lines, core.end, retest],
    )

    assert all(point.bs_type != "3buy" for point in points)


def test_second_point_requires_completed_pullback() -> None:
    result, lines, provider = weak_second_buy_case()
    lines[-1].end.done = False

    points = BsBranchCalculator().second_class(result, lines, provider, "1m")

    assert all(point.bs_type != "2buy" for point in points)
