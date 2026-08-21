from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataSnapshot,
    SecurityMasterRecord,
    membership_changes_from_cninfo,
    qmt_factors_from_rows,
    sha256_json,
    snapshot_from_payload,
    snapshot_payload,
)


CN = ZoneInfo("Asia/Shanghai")


def _membership_records() -> list[dict[str, object]]:
    return [
        {
            "VARYDATE": "2021-07-30",
            "F001V": "008003",
            "F003V": "S280501",
            "F004V": "汽车",
        },
        {
            "VARYDATE": "2025-08-11",
            "F001V": "008003",
            "F003V": "S420901",
            "F004V": "交通运输",
        },
        {
            "VARYDATE": "2025-09-01",
            "F001V": "008014",
            "F003V": "25102010",
            "F004V": "可选消费",
        },
    ]


def _snapshot() -> PITMetadataSnapshot:
    memberships = membership_changes_from_cninfo(
        code="SZ.000001",
        records=_membership_records(),
        not_after=date(2026, 7, 24),
    )
    factors = qmt_factors_from_rows(
        code="SZ.000001",
        rows=[
            {
                "effective_on": "2025-10-15",
                "interest": 0.236,
                "stockBonus": 0,
                "stockGift": 0,
                "allotNum": 0,
                "allotPrice": 0,
                "gugai": 0,
                "dr": 1.021505,
            }
        ],
        not_before=date(2025, 5, 1),
        not_after=date(2026, 7, 24),
    )
    return PITMetadataSnapshot(
        source_start=date(2025, 5, 1),
        source_end=date(2026, 7, 24),
        captured_at=datetime(2026, 7, 25, 12, tzinfo=CN),
        securities=(
            SecurityMasterRecord(
                code="SZ.000001",
                name="平安银行",
                listed_from=date(1991, 4, 3),
                listed_through=None,
            ),
        ),
        memberships=memberships,
        factors=factors,
        qmt_sw1_sector_names=(
            ("qmt-sw1:S28", "汽车"),
            ("qmt-sw1:S42", "交通运输"),
        ),
        source_hashes=(("fixture", "sha256:" + "1" * 64),),
    )


def test_membership_change_is_not_visible_on_its_source_day() -> None:
    snapshot = _snapshot()

    before = snapshot.membership_at(
        "SZ.000001", datetime(2025, 8, 11, 15, tzinfo=CN)
    )
    after = snapshot.membership_at(
        "SZ.000001", datetime(2025, 8, 12, 9, 30, tzinfo=CN)
    )

    assert before is not None and before.sector_id == "qmt-sw1:S28"
    assert after is not None and after.sector_id == "qmt-sw1:S42"


def test_future_membership_row_cannot_change_an_earlier_prefix() -> None:
    prefix = membership_changes_from_cninfo(
        code="SZ.000001",
        records=_membership_records()[:1],
        not_after=date(2025, 8, 1),
    )
    full = _snapshot()
    observed = datetime(2025, 8, 1, 15, tzinfo=CN)

    assert prefix[-1].sector_id == full.membership_at(
        "SZ.000001", observed
    ).sector_id


def test_security_master_enforces_listing_and_expiry_sessions() -> None:
    row = SecurityMasterRecord(
        code="SH.600001",
        name="fixture",
        listed_from=date(2025, 8, 1),
        listed_through=date(2026, 1, 2),
    )

    assert not row.listed_on(date(2025, 7, 31))
    assert row.listed_on(date(2025, 8, 1))
    assert row.listed_on(date(2026, 1, 2))
    assert not row.listed_on(date(2026, 1, 3))


def test_security_master_rejects_qmt_1970_open_date_sentinel() -> None:
    with pytest.raises(ValueError, match="pre-market sentinel"):
        SecurityMasterRecord(
            code="SZ.001232",
            name="future contract",
            listed_from=date(1970, 1, 1),
            listed_through=None,
        )


def test_qmt_factor_builds_ex_date_only_account_action() -> None:
    factor = qmt_factors_from_rows(
        code="SZ.301171",
        rows=[
            {
                "effective_on": "2026-04-28",
                "interest": "0.035",
                "stockBonus": "0",
                "stockGift": "0.3",
                "allotNum": "0",
                "allotPrice": "0",
                "gugai": "0",
                "dr": "1.300854",
            }
        ],
        not_before=date(2025, 5, 1),
        not_after=date(2026, 7, 24),
    )[0]
    action = factor.corporate_action()

    assert action.known_at == datetime(2026, 4, 28, 9, 30, tzinfo=CN)
    assert action.share_multiplier == Decimal("1.3")
    assert action.cash_per_share == Decimal("0.035")
    assert action.raw_price_divisor == Decimal("1.300854")


def test_snapshot_round_trip_verifies_content_hash() -> None:
    payload = snapshot_payload(_snapshot(), audit={"diagnostic": True})
    restored = snapshot_from_payload(payload)
    assert restored == _snapshot()

    tampered = json.loads(json.dumps(payload))
    tampered["securities"][0]["name"] = "tampered"
    with pytest.raises(ValueError, match="content hash"):
        snapshot_from_payload(tampered)


def test_snapshot_loader_accepts_hashed_v1_artifact_without_rewriting_it() -> None:
    payload = snapshot_payload(_snapshot(), audit={"diagnostic": True})
    payload["schema"] = "chanlun-qmt-pit-metadata/v1"
    canonical = {
        key: value
        for key, value in payload.items()
        if key not in {"content_sha256", "audit"}
    }
    payload["content_sha256"] = sha256_json(canonical)

    restored = snapshot_from_payload(payload)

    assert restored == _snapshot()
    assert payload["schema"] == "chanlun-qmt-pit-metadata/v1"


def test_conflicting_same_day_memberships_are_rejected() -> None:
    rows = _membership_records()[:1] + [
        {
            "VARYDATE": "2021-07-30",
            "F001V": "008003",
            "F003V": "S370501",
            "F004V": "医药生物",
        }
    ]
    with pytest.raises(ValueError, match="conflicting"):
        membership_changes_from_cninfo(
            code="SZ.000001",
            records=rows,
            not_after=date(2026, 7, 24),
        )
