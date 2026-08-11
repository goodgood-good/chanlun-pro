from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    apply_qmt_causal_factor_adjustment,
    build_causal_sector_price_basis_metadata,
    qmt_causal_factor_events_from_frame,
    qmt_causal_factor_revision,
)


CN = ZoneInfo("Asia/Shanghai")


def _factor_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "interest": 0,
                "stockBonus": 0,
                "stockGift": 1,
                "allotNum": 0,
                "allotPrice": 0,
                "gugai": 0,
                "dr": 2,
            },
            {
                "interest": 0,
                "stockBonus": 0,
                "stockGift": 2,
                "allotNum": 0,
                "allotPrice": 0,
                "gugai": 0,
                "dr": 3,
            },
        ],
        index=("2026-07-23", "2026-07-24"),
    )


def test_causal_factor_neutralizes_ex_date_jump_and_ignores_future_row() -> None:
    events = qmt_causal_factor_events_from_frame(
        code="SH.600000",
        frame=_factor_frame(),
        not_before=date(2026, 7, 1),
        not_after=date(2026, 7, 23),
    )
    assert tuple(value.effective_on for value in events) == (date(2026, 7, 23),)

    bars = pd.DataFrame(
        {
            "date": (
                datetime(2026, 7, 22, 15, 0, tzinfo=CN),
                datetime(2026, 7, 23, 9, 35, tzinfo=CN),
            ),
            "open": (10.0, 5.0),
            "high": (10.0, 5.0),
            "low": (10.0, 5.0),
            "close": (10.0, 5.0),
        }
    )
    adjusted = apply_qmt_causal_factor_adjustment(
        bars,
        code="SH.600000",
        events=events,
    )

    assert tuple(adjusted["close"]) == (10.0, 10.0)


def test_factor_revision_binds_cutoff_members_and_economics() -> None:
    events = qmt_causal_factor_events_from_frame(
        code="SH.600000",
        frame=_factor_frame(),
        not_before=date(2026, 7, 1),
        not_after=date(2026, 7, 23),
    )
    first = qmt_causal_factor_revision(
        members=("SH.600000",),
        events_by_code={"SH.600000": events},
        known_through=date(2026, 7, 23),
    )
    later_cutoff = qmt_causal_factor_revision(
        members=("SH.600000",),
        events_by_code={"SH.600000": events},
        known_through=date(2026, 7, 24),
    )
    more_members = qmt_causal_factor_revision(
        members=("SH.600000", "SZ.000001"),
        events_by_code={"SH.600000": events},
        known_through=date(2026, 7, 23),
    )

    assert len({first, later_cutoff, more_members}) == 3
    metadata = build_causal_sector_price_basis_metadata(
        provider="qmt-gics3-composite",
        market="a",
        code="qmt-gics3:test:membership",
        adjustment="causal-factor-stable-24-member-median",
        structure_price_quantum=Decimal("0.000001"),
        factor_revision=first,
    )
    assert metadata.price_basis_revision.startswith("sha256:")
    assert QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID in (
        "QMT_RAW_PRICE_DIVISOR_CAUSAL_EX_DATE",
    )


def test_nonempty_malformed_factor_response_is_rejected() -> None:
    malformed = pd.DataFrame([{"dr": 2}], index=("2026-07-23",))
    with pytest.raises(ValueError, match="missing required fields"):
        qmt_causal_factor_events_from_frame(
            code="SH.600000",
            frame=malformed,
            not_before=date(2026, 7, 1),
            not_after=date(2026, 7, 23),
        )

    no_effective_date = _factor_frame().reset_index(drop=True)
    with pytest.raises(ValueError, match="no effective date"):
        qmt_causal_factor_events_from_frame(
            code="SH.600000",
            frame=no_effective_date,
            not_before=date(2026, 7, 1),
            not_after=date(2026, 7, 23),
        )


def test_factor_revision_rejects_unordered_or_duplicate_direct_events() -> None:
    events = qmt_causal_factor_events_from_frame(
        code="SH.600000",
        frame=_factor_frame(),
        not_before=date(2026, 7, 1),
        not_after=date(2026, 7, 24),
    )
    for invalid in ((events[1], events[0]), (events[0], events[0])):
        with pytest.raises(ValueError, match="unique and increasing"):
            qmt_causal_factor_revision(
                members=("SH.600000",),
                events_by_code={"SH.600000": invalid},
                known_through=date(2026, 7, 24),
            )
