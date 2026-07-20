from datetime import datetime
from zoneinfo import ZoneInfo

from chanlun.decision_support.trading_system.models import (
    StructuralPoint,
    build_point_id,
)


CN = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 7, 20, 10, 31, tzinfo=CN)


def _point(*, tower: str, level: int) -> StructuralPoint:
    point_id = build_point_id(
        code="SZ.000001",
        point_type="1buy",
        source_frequency="1m",
        tower=tower,
        recursive_level=level,
        anchor_at=AT,
        center_id="center-a",
        parent_point_id=None,
    )
    return StructuralPoint(
        point_id=point_id,
        code="SZ.000001",
        point_type="1buy",
        side="buy",
        status="confirmed",
        variant="standard",
        source_frequency="1m",
        tower=tower,
        recursive_level=level,
        anchor_at=AT,
        confirmed_at=AT,
        anchor_price=10.20,
        invalidation_price=9.80,
        center_id="center-a",
        center_zd=9.40,
        center_zg=9.80,
        center_ordinal=1,
        divergence_kind="qs",
        parent_point_id=None,
        evidence_codes=("trend_divergence",),
    )


def test_point_identity_distinguishes_tower_and_recursive_level() -> None:
    bi_l0 = _point(tower="bi", level=0)
    xd_l0 = _point(tower="xd", level=0)
    xd_l1 = _point(tower="xd", level=1)

    assert len({bi_l0.point_id, xd_l0.point_id, xd_l1.point_id}) == 3
    assert bi_l0.structure_key == ("bi", 0, "center-a")
    assert bi_l0.confirmed is True
