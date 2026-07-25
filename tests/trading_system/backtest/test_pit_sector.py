from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataSnapshot,
    QmtFactorAt,
    SecurityMasterRecord,
    SectorMembershipChange,
)
from chanlun.decision_support.trading_system.backtest.pit_sector import (
    composite_from_member_frames,
)


CN = ZoneInfo("Asia/Shanghai")


def _membership(
    code: str,
    sector_id: str,
    name: str,
    changed_on: date,
) -> SectorMembershipChange:
    industry = sector_id.rsplit(":", 1)[-1]
    return SectorMembershipChange(
        code=code,
        sector_id=sector_id,
        sector_name=name,
        industry_code=industry,
        source_changed_on=changed_on,
        known_at=datetime.combine(
            changed_on + timedelta(days=1), datetime.min.time(), tzinfo=CN
        ),
    )


def test_sector_composite_uses_effective_membership_and_ex_date_factor() -> None:
    sector = "qmt-sw1:S27"
    other = "qmt-sw1:S28"
    codes = ("SZ.000001", "SZ.000002", "SZ.000003")
    securities = tuple(
        SecurityMasterRecord(
            code=code,
            name=code,
            listed_from=date(2020, 1, 1),
            listed_through=None,
        )
        for code in codes
    )
    memberships = [
        _membership(code, sector, "电子", date(2021, 7, 30)) for code in codes
    ]
    # This change is still unavailable on 2025-08-11 and becomes usable on
    # the next calendar day.
    memberships.append(
        _membership("SZ.000003", other, "汽车", date(2025, 8, 11))
    )
    factor = QmtFactorAt(
        code="SZ.000001",
        effective_on=date(2025, 8, 11),
        interest=Decimal("0"),
        stock_bonus=Decimal("0"),
        stock_gift=Decimal("1"),
        allot_num=Decimal("0"),
        allot_price=Decimal("0"),
        gugai=Decimal("0"),
        raw_price_divisor=Decimal("2"),
    )
    snapshot = PITMetadataSnapshot(
        source_start=date(2025, 8, 8),
        source_end=date(2025, 8, 12),
        captured_at=datetime(2025, 8, 13, tzinfo=CN),
        securities=securities,
        memberships=tuple(
            sorted(memberships, key=lambda row: (row.code, row.known_at, row.sector_id))
        ),
        factors=(factor,),
        qmt_sw1_sector_names=((sector, "电子"), (other, "汽车")),
        source_hashes=(("fixture", "sha256:" + "1" * 64),),
    )
    closes = (
        datetime(2025, 8, 8, 15, tzinfo=CN),
        datetime(2025, 8, 11, 10, tzinfo=CN),
        datetime(2025, 8, 12, 10, tzinfo=CN),
    )
    epoch = [int(value.timestamp() * 1000) for value in closes]
    frames = {}
    for code in codes:
        # The first member halves its raw price on the factor date.  Causal
        # adjustment restores a 1.0 ratio without modifying 2025-08-08.
        prices = [10.0, 5.0, 5.5] if code == "SZ.000001" else [10.0, 10.0, 11.0]
        frames[code] = pd.DataFrame(
            {
                "time": epoch,
                "open": prices,
                "high": prices,
                "low": prices,
                "close": prices,
                "volume": [1000, 1000, 1000],
            }
        )

    frame = composite_from_member_frames(
        snapshot=snapshot,
        sector_id=sector,
        member_frames=frames,
        start_at=closes[1],
        end_at=closes[2],
        minimum_member_count=2,
        minimum_bar_coverage=Decimal("0.60"),
    )

    assert tuple(frame["date"]) == (pd.Timestamp(closes[1]), pd.Timestamp(closes[2]))
    assert frame.iloc[0]["close"] == 1000.0
    # On 8/11 all three rows still belong to electronics.  On 8/12 the changed
    # member is excluded, leaving two valid all-member observations.
    assert tuple(frame["volume"]) == (3.0, 2.0)
    assert frame.iloc[1]["close"] == 1100.0
