from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
)
from tests.trading_system.helpers import line, raw_point, zone


CN = ZoneInfo("Asia/Shanghai")
AS_OF = datetime(2026, 7, 20, 15, 0, tzinfo=CN)


def test_adapter_keeps_bi_and_xd_points_separate() -> None:
    bi_point = raw_point("1buy", line("down", 1, 11.0, 5, 9.0), zone_id=1)
    xd_point = raw_point("1buy", line("down", 1, 11.0, 5, 9.0), zone_id=1)
    cd = SimpleNamespace(
        get_branch_bspoints=lambda use_xd=False: [xd_point] if use_xd else [bi_point],
        get_recursive_branch_levels_for_tower=lambda use_xd: [],
    )

    points = extract_confirmed_points(
        cd,
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert {(point.tower, point.recursive_level) for point in points} == {
        ("bi", 0),
        ("xd", 0),
    }
    assert len({point.point_id for point in points}) == 2


def test_three_buy_ordinal_uses_its_exact_recursive_level() -> None:
    low = zone(line("up", 0, 6.0, 5, 7.0), zg=7.0, zd=6.0, zone_id=1)
    middle = zone(line("up", 10, 7.5, 15, 8.0), zg=8.0, zd=7.5, zone_id=2)
    target = zone(line("up", 20, 9.0, 25, 11.0), zg=10.0, zd=9.0, zone_id=3)
    retest = line("down", 25, 11.0, 30, 10.5)
    point = raw_point("3buy", retest, level=1, core=target)
    levels = (
        SimpleNamespace(level=0, zss=[low, middle, target]),
        SimpleNamespace(level=1, zss=[middle, target]),
    )
    cd = SimpleNamespace(
        get_branch_bspoints=lambda use_xd=False: [] if use_xd else [point],
        get_recursive_branch_levels_for_tower=lambda use_xd: [] if use_xd else levels,
    )

    points = extract_confirmed_points(
        cd,
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert points[0].center_ordinal == 2


def test_adapter_rejects_future_anchor() -> None:
    future = raw_point(
        "1buy",
        line("down", 400, 11.0, 405, 9.0),
        zone_id=1,
    )
    cd = SimpleNamespace(
        get_branch_bspoints=lambda use_xd=False: [] if use_xd else [future],
        get_recursive_branch_levels_for_tower=lambda use_xd: [],
    )

    with pytest.raises(ValueError, match="after as_of"):
        extract_confirmed_points(
            cd,
            code="SZ.000001",
            source_frequency="1m",
            as_of=AS_OF,
        )


def test_adapter_accepts_opening_center_without_entry_segment() -> None:
    opening = zone(line("down", 20, 11.0, 25, 9.0), zone_id=1)
    opening.start = None
    point = raw_point(
        "1buy",
        line("down", 20, 11.0, 25, 9.0),
        core=opening,
    )
    cd = SimpleNamespace(
        get_branch_bspoints=lambda use_xd=False: [] if use_xd else [point],
        get_recursive_branch_levels_for_tower=lambda use_xd: [],
    )

    points = extract_confirmed_points(
        cd,
        code="SZ.000001",
        source_frequency="1m",
        as_of=AS_OF,
    )

    assert len(points) == 1
    assert points[0].center_id.startswith("sha256:")
