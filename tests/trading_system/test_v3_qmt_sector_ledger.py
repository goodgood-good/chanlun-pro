from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.v3_qmt_sector_ledger import (
    HistoricalSectorProbe,
    append_sector_catalog,
    audit_historical_sector_probes,
    captured_catalog_at,
    load_sector_ledger,
)
from chanlun.decision_support.trading_system import v3_qmt_sector_ledger as subject


CN = ZoneInfo("Asia/Shanghai")
CAPTURED = datetime(2026, 7, 27, 8, 30, tzinfo=CN)


def catalog(captured_at: datetime, members: tuple[str, ...]) -> dict[str, object]:
    sectors = [
        {
            "sector_id": "qmt-gics3:bank",
            "name": "商业银行",
            "source_key": "GICS3商业银行",
            "member_codes": list(members),
        }
    ]
    return {
        "source": "qmt_gics3_components",
        "captured_at": captured_at.isoformat(),
        "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
        "catalog_revision": sha256_json(
            {"schema": "chanlun-qmt-gics3-catalog/v1", "sectors": sectors}
        ),
        "sectors": sectors,
    }


def test_hash_chained_capture_is_visible_only_after_capture_on_same_session(
    tmp_path: Path,
) -> None:
    path = tmp_path / "sector-ledger.json"
    first = append_sector_catalog(
        path,
        catalog(CAPTURED, ("SH.600000", "SH.600036")),
    )
    second = append_sector_catalog(
        path,
        catalog(CAPTURED + timedelta(hours=1), ("SH.600000", "SH.600036")),
    )

    assert len(second["entries"]) == 2
    assert second["entries"][1]["previous_entry_sha256"] == first["entries"][0][
        "entry_sha256"
    ]
    assert captured_catalog_at(
        second,
        decision_time=CAPTURED - timedelta(seconds=1),
    ) is None
    visible = captured_catalog_at(
        second,
        decision_time=CAPTURED + timedelta(hours=2),
    )
    assert visible is not None
    assert visible["ledger_entry_sha256"] == second["entries"][1]["entry_sha256"]
    assert captured_catalog_at(
        second,
        decision_time=CAPTURED + timedelta(days=1),
    ) is None
    assert load_sector_ledger(path) == second


def test_ledger_tampering_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "sector-ledger.json"
    append_sector_catalog(path, catalog(CAPTURED, ("SH.600000", "SH.600036")))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["entries"][0]["sectors"][0]["member_codes"].append("SH.601398")
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="content hash changed"):
        load_sector_ledger(path)


def test_append_holds_interprocess_lock_for_read_modify_write(
    monkeypatch,
    tmp_path: Path,
) -> None:
    events: list[tuple[str, Path]] = []

    @contextmanager
    def observed_lock(path: Path):
        events.append(("enter", Path(path)))
        yield
        events.append(("exit", Path(path)))

    monkeypatch.setattr(subject, "interprocess_file_lock", observed_lock)
    path = tmp_path / "sector-ledger.json"

    append_sector_catalog(path, catalog(CAPTURED, ("SH.600000",)))

    assert events == [
        ("enter", path.with_suffix(".json.lock")),
        ("exit", path.with_suffix(".json.lock")),
    ]


def test_future_listed_member_proves_historical_current_backfill() -> None:
    probes = (
        HistoricalSectorProbe(
            "GICS3半导体产品与设备",
            date(2019, 1, 2),
            ("SH.688809", "SH.688981"),
        ),
        HistoricalSectorProbe(
            "GICS3半导体产品与设备",
            date(2026, 7, 24),
            ("SH.688809", "SH.688981"),
        ),
    )
    result = audit_historical_sector_probes(
        probes,
        listed_from={
            "SH.688981": date(2020, 7, 16),
            "SH.688809": date(2025, 12, 30),
        },
    )

    assert result["status"] == "CURRENT_BACKFILL_PROVEN"
    assert result["historical_point_in_time_eligible"] is False
    assert len(result["future_listed_members"]) == 2
    assert "FUTURE_LISTED_MEMBER_IN_HISTORICAL_RESPONSE" in result["reason_codes"]
    assert result["identical_member_sets_across_dates"] == (
        "GICS3半导体产品与设备",
    )


def test_identical_sets_without_listing_proof_remain_unverified_not_certified() -> None:
    probes = (
        HistoricalSectorProbe("GICS3商业银行", date(2020, 1, 2), ("SH.600000",)),
        HistoricalSectorProbe("GICS3商业银行", date(2021, 1, 4), ("SH.600000",)),
    )
    result = audit_historical_sector_probes(
        probes,
        listed_from={"SH.600000": date(1999, 11, 10)},
    )

    assert result["status"] == "HISTORICAL_POINT_IN_TIME_UNVERIFIED"
    assert result["historical_point_in_time_eligible"] is False
