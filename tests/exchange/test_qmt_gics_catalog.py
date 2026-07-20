from __future__ import annotations

import inspect
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.exchange import exchange_qmt
from chanlun.exchange import qmt_sector_catalog as subject
from chanlun.exchange.qmt_sector_catalog import QmtGicsCatalog


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _at(hour: int = 9, minute: int = 31) -> datetime:
    return datetime(2026, 7, 17, hour, minute, tzinfo=SHANGHAI)


def _info(
    names: list[object],
    *,
    category: str = "行业",
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "sector": names,
            "category": [category] * len(names),
        }
    )


class _TrackingLock:
    def __init__(self) -> None:
        self.depth = 0
        self.enter_count = 0

    def __enter__(self) -> _TrackingLock:
        self.enter_count += 1
        self.depth += 1
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.depth -= 1


class _FakeXtdata:
    def __init__(
        self,
        *,
        lock: _TrackingLock,
        sector_list: object,
        sector_info: object,
        members: dict[str, object],
    ) -> None:
        self._lock = lock
        self._sector_list = sector_list
        self._sector_info = sector_info
        self._members = members
        self.calls: list[tuple[str, str | None, int]] = []

    def _record(self, operation: str, sector: str | None = None) -> None:
        assert self._lock.depth == 1, "xtdata call escaped the native lock"
        self.calls.append((operation, sector, id(self._lock)))

    @staticmethod
    def _result(value: object) -> object:
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, pd.DataFrame):
            return value.copy(deep=True)
        if isinstance(value, list):
            return list(value)
        return value

    def get_sector_list(self):
        self._record("get_sector_list")
        return self._result(self._sector_list)

    def get_sector_info(self, sector_name: str = ""):
        self._record("get_sector_info", sector_name)
        assert sector_name == ""
        return self._result(self._sector_info)

    def get_stock_list_in_sector(
        self,
        sector_name: str,
        real_timetag: int = -1,
    ):
        self._record("get_stock_list_in_sector", sector_name)
        assert real_timetag == -1
        return self._result(self._members[sector_name])

    def download_sector_data(self):
        raise AssertionError("catalog must not download sector data")

    def get_full_tick(self, codes):
        raise AssertionError("catalog must not capture ticks")

    def get_instrument_detail(self, code, complete=False):
        raise AssertionError("catalog must not query per-security detail")


def _install_native(
    monkeypatch: pytest.MonkeyPatch,
    *,
    sector_list: object,
    sector_info: object,
    members: dict[str, object],
) -> tuple[_TrackingLock, _FakeXtdata]:
    lock = _TrackingLock()
    fake = _FakeXtdata(
        lock=lock,
        sector_list=sector_list,
        sector_info=sector_info,
        members=members,
    )
    monkeypatch.setattr(subject, "_XTDATA_NATIVE_LOCK", lock)
    monkeypatch.setattr(exchange_qmt, "_XTDATA_NATIVE_LOCK", lock)
    monkeypatch.setattr(subject, "xtdata", fake)
    return lock, fake


def _snapshot(as_of: datetime | None = None):
    return QmtGicsCatalog().snapshot(as_of or _at())


def _valid_native(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reverse: bool = False,
    info_category: str = "行业",
) -> tuple[_TrackingLock, _FakeXtdata]:
    names = [
        "普通板块",
        "GICS1消费",
        "GICS1金融",
        "GICS3白酒",
        "GICS3银行",
    ]
    members: dict[str, object] = {
        "GICS1消费": ["600519.SH", "000858.SZ", "430047.BJ"],
        "GICS1金融": ["000001.SZ"],
        "GICS3白酒": ["600519.SH", "000858.SZ", "430047.BJ"],
        "GICS3银行": ["000001.SZ"],
    }
    if reverse:
        names = list(reversed(names))
        members = {
            name: list(reversed(values))
            for name, values in reversed(tuple(members.items()))
        }
    return _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names, category=info_category),
        members=members,
    )


@pytest.mark.parametrize(
    ("as_of", "message"),
    (
        (None, "as_of must be a datetime"),
        ("2026-07-17T09:31:00+08:00", "as_of must be a datetime"),
        (datetime(2026, 7, 17, 9, 31), "as_of must be timezone-aware"),
    ),
)
def test_qmt_gics_catalog_rejects_invalid_as_of_before_native_reads(
    monkeypatch: pytest.MonkeyPatch,
    as_of: object,
    message: str,
) -> None:
    lock, fake = _valid_native(monkeypatch)

    with pytest.raises(ValueError, match=message):
        QmtGicsCatalog().snapshot(as_of)  # type: ignore[arg-type]

    assert lock.enter_count == 0
    assert fake.calls == []


def test_qmt_gics_catalog_normalizes_utc_as_of_to_shanghai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _valid_native(monkeypatch)
    utc_as_of = datetime(2026, 7, 17, 1, 31, tzinfo=timezone.utc)

    snapshot = QmtGicsCatalog().snapshot(utc_as_of)

    assert snapshot.captured_at == _at()
    assert snapshot.captured_at.tzinfo == SHANGHAI


@pytest.mark.parametrize(
    "sector_list",
    (
        RuntimeError("sector list unavailable"),
        None,
        [],
        ("GICS1父级", "GICS3子级"),
        ["GICS1父级", 7, "GICS3子级"],
    ),
)
def test_qmt_gics_catalog_rejects_invalid_sector_list_shapes(
    monkeypatch: pytest.MonkeyPatch,
    sector_list: object,
) -> None:
    names = ["GICS1父级", "GICS3子级"]
    lock, fake = _install_native(
        monkeypatch,
        sector_list=sector_list,
        sector_info=_info(names),
        members={"GICS1父级": ["600000.SH"], "GICS3子级": ["600000.SH"]},
    )

    snapshot = _snapshot()

    assert lock.enter_count == 1
    assert snapshot.sectors == ()
    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == ("sector_source_unavailable",)
    assert snapshot.ambiguous_gics3_memberships == ()
    assert snapshot.invalid_codes == ()
    assert snapshot.empty_sector_names == ()
    assert snapshot.parent_mapping_conflicts == ()
    assert not any(call[0] == "get_stock_list_in_sector" for call in fake.calls)


def test_qmt_gics_catalog_normalizes_unique_gics3_membership_under_one_native_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert subject._XTDATA_NATIVE_LOCK is exchange_qmt._XTDATA_NATIVE_LOCK
    lock, fake = _valid_native(monkeypatch)

    snapshot = _snapshot()

    sectors = {item.name: item for item in snapshot.sectors}
    liquor = sectors["白酒"]
    bank = sectors["银行"]
    info_rows = tuple(
        sorted(
            (
                ("普通板块", "行业"),
                ("GICS1消费", "行业"),
                ("GICS1金融", "行业"),
                ("GICS3白酒", "行业"),
                ("GICS3银行", "行业"),
            )
        )
    )
    assert snapshot.source == "qmt_gics"
    assert snapshot.source_service_id == "xtquant-sector-info:" + sha256_json(
        info_rows
    ).removeprefix("sha256:")
    assert snapshot.captured_at == _at()
    assert snapshot.eligible_for_entry is True
    assert snapshot.reason_codes == ()
    assert liquor.normalized_name == "白酒"
    assert liquor.members == ("BJ.430047", "SH.600519", "SZ.000858")
    assert bank.members == ("SZ.000001",)
    assert liquor.parent_gics1_id == "GICS1消费"
    assert liquor.parent_gics1_name == "消费"
    assert bank.parent_gics1_id == "GICS1金融"
    assert bank.parent_gics1_name == "金融"
    assert liquor.sector_id == "sector:" + sha256_json(
        {
            "source": "qmt_gics",
            "level": "GICS3",
            "normalized_name": "白酒",
        }
    ).removeprefix("sha256:")
    assert snapshot.membership_fingerprint.startswith("sha256:")
    assert lock.enter_count == 1
    assert lock.depth == 0
    assert {call[2] for call in fake.calls} == {id(lock)}
    assert [call[:2] for call in fake.calls] == [
        ("get_sector_list", None),
        ("get_sector_info", ""),
        ("get_stock_list_in_sector", "GICS1消费"),
        ("get_stock_list_in_sector", "GICS1金融"),
        ("get_stock_list_in_sector", "GICS3白酒"),
        ("get_stock_list_in_sector", "GICS3银行"),
    ]


@pytest.mark.parametrize("parent_count", (0, 2))
def test_qmt_gics_catalog_fails_closed_on_zero_or_multiple_gics1_parents(
    monkeypatch: pytest.MonkeyPatch,
    parent_count: int,
) -> None:
    names = ["GICS1父级A", "GICS1父级B", "GICS3孤儿"]
    if parent_count == 0:
        parent_a = ["000001.SZ"]
        parent_b = ["000002.SZ"]
        child = ["000001.SZ", "000002.SZ"]
    else:
        parent_a = ["600000.SH"]
        parent_b = ["600000.SH"]
        child = ["600000.SH"]
    _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1父级A": parent_a,
            "GICS1父级B": parent_b,
            "GICS3孤儿": child,
        },
    )

    snapshot = _snapshot()

    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == ("parent_mapping_conflict",)
    assert snapshot.parent_mapping_conflicts == ("GICS3孤儿",)
    assert all(item.name != "孤儿" for item in snapshot.sectors)


def test_qmt_gics_catalog_detects_clean_union_coverage_mismatch_without_local_contamination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["GICS1父级", "GICS3子级"]
    _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1父级": ["600000.SH", "000001.SZ"],
            "GICS3子级": ["600000.SH"],
        },
    )

    snapshot = _snapshot()

    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == ("catalog_coverage_mismatch",)
    assert snapshot.invalid_codes == ()
    assert snapshot.empty_sector_names == ()
    assert snapshot.parent_mapping_conflicts == ()
    assert len(snapshot.sectors) == 1
    sector = snapshot.sectors[0]
    assert sector.name == "子级"
    assert sector.members == ("SH.600000",)
    assert sector.eligible_for_entry is True
    assert sector.reason_codes == ()


def test_cross_gics3_member_is_quarantined_without_arbitrary_sector_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["GICS1消费", "GICS3白酒", "GICS3食品"]
    members = {
        "GICS1消费": ["600519.SH", "000858.SZ", "000568.SZ"],
        "GICS3白酒": ["600519.SH", "000858.SZ"],
        "GICS3食品": ["600519.SH", "000568.SZ"],
    }
    _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members=members,
    )
    first = _snapshot(_at(9, 31))
    _install_native(
        monkeypatch,
        sector_list=list(reversed(names)),
        sector_info=_info(list(reversed(names))),
        members={
            name: list(reversed(values))
            for name, values in reversed(tuple(members.items()))
        },
    )
    second = _snapshot(_at(14, 59))

    sectors = {item.name: item for item in first.sectors}
    ambiguous = first.ambiguous_gics3_memberships
    assert first.eligible_for_entry is False
    assert first.reason_codes == ("ambiguous_gics3_membership",)
    assert sectors["白酒"].members == ("SZ.000858",)
    assert sectors["食品"].members == ("SZ.000568",)
    assert sectors["白酒"].eligible_for_entry is False
    assert sectors["食品"].eligible_for_entry is False
    assert sectors["白酒"].reason_codes == ("ambiguous_gics3_membership",)
    assert sectors["食品"].reason_codes == ("ambiguous_gics3_membership",)
    assert all("SH.600519" not in item.members for item in first.sectors)
    assert len(ambiguous) == 1
    assert ambiguous[0].code == "SH.600519"
    assert ambiguous[0].source_sector_ids == tuple(
        sorted((sectors["白酒"].sector_id, sectors["食品"].sector_id))
    )
    assert first.ambiguous_gics3_memberships == second.ambiguous_gics3_memberships
    assert first.membership_fingerprint == second.membership_fingerprint


@pytest.mark.parametrize(
    "sector_info",
    (
        pd.DataFrame({"sector": ["GICS1父级", "GICS3子级"]}),
        {"sector": ["GICS1父级", "GICS3子级"], "category": ["行业"] * 2},
        None,
        pd.DataFrame({"sector": [], "category": []}),
        RuntimeError("sector info unavailable"),
        pd.DataFrame({"sector": ["GICS1父级", ""], "category": ["行业"] * 2}),
        pd.DataFrame({"sector": ["GICS1父级", 7], "category": ["行业"] * 2}),
        pd.DataFrame({"sector": ["GICS1父级", "GICS3子级"], "category": ["行业", " "]}),
        pd.DataFrame({"sector": ["GICS1父级", "GICS3子级"], "category": ["行业", 7]}),
        pd.DataFrame(
            [["GICS1父级", "重复", "行业"], ["GICS3子级", "重复", "行业"]],
            columns=["sector", "sector", "category"],
        ),
        pd.DataFrame(
            [["GICS1父级", "行业", "重复"], ["GICS3子级", "行业", "重复"]],
            columns=["sector", "category", "category"],
        ),
    ),
)
def test_qmt_gics_catalog_uses_bundled_sector_info_dataframe_shape(
    monkeypatch: pytest.MonkeyPatch,
    sector_info: object,
) -> None:
    names = ["GICS1父级", "GICS3子级"]
    lock, fake = _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=sector_info,
        members={"GICS1父级": ["600000.SH"], "GICS3子级": ["600000.SH"]},
    )

    snapshot = _snapshot()

    assert snapshot.source_service_id == "xtquant-sector-info:unavailable"
    assert lock.enter_count == 1
    assert snapshot.sectors == ()
    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == ("sector_source_unavailable",)
    assert snapshot.ambiguous_gics3_memberships == ()
    assert snapshot.invalid_codes == ()
    assert snapshot.empty_sector_names == ()
    assert snapshot.parent_mapping_conflicts == ()
    assert not any(call[0] == "get_stock_list_in_sector" for call in fake.calls)


def test_qmt_gics_catalog_source_service_id_preserves_duplicate_rows_and_ignores_extra_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["GICS3子级", "GICS1父级", "GICS3子级", "GICS1父级"]
    members = {"GICS1父级": ["600000.SH"], "GICS3子级": ["600000.SH"]}
    duplicate_rows = pd.DataFrame(
        {
            "sector": ["GICS3子级", "GICS1父级", "GICS3子级"],
            "category": ["行业", "行业", "行业"],
            "provider_version": ["ignored-a", "ignored-b", "ignored-c"],
        }
    )

    def capture(sector_info: pd.DataFrame):
        _, fake = _install_native(
            monkeypatch,
            sector_list=names,
            sector_info=sector_info,
            members=members,
        )
        return _snapshot(), fake

    duplicate, duplicate_fake = capture(duplicate_rows)
    changed_extra, _ = capture(
        duplicate_rows.assign(provider_version=["x", "y", "z"])
    )
    deduplicated, _ = capture(duplicate_rows.drop_duplicates(["sector", "category"]))
    expected_rows = tuple(
        sorted(
            (
                ("GICS3子级", "行业"),
                ("GICS1父级", "行业"),
                ("GICS3子级", "行业"),
            )
        )
    )

    assert duplicate.eligible_for_entry is True
    assert duplicate.reason_codes == ()
    assert duplicate.source_service_id == "xtquant-sector-info:" + sha256_json(
        expected_rows
    ).removeprefix("sha256:")
    assert duplicate.source_service_id == changed_extra.source_service_id
    assert duplicate.source_service_id != deduplicated.source_service_id
    assert duplicate.membership_fingerprint == changed_extra.membership_fingerprint
    assert duplicate.membership_fingerprint == deduplicated.membership_fingerprint
    assert [call[:2] for call in duplicate_fake.calls] == [
        ("get_sector_list", None),
        ("get_sector_info", ""),
        ("get_stock_list_in_sector", "GICS1父级"),
        ("get_stock_list_in_sector", "GICS3子级"),
    ]


def test_qmt_gics_catalog_rejects_invalid_security_and_normalized_name_collisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_names = ["GICS1父级", "GICS3有效"]
    _install_native(
        monkeypatch,
        sector_list=invalid_names,
        sector_info=_info(invalid_names),
        members={
            "GICS1父级": ["600000.SH"],
            "GICS3有效": [
                "600000.SH",
                "12345.SH",
                "600000.HK",
                "600001.SH",
            ],
        },
    )
    invalid = _snapshot()
    assert invalid.invalid_codes == ("12345.SH", "600000.HK", "SH.600001")
    assert invalid.reason_codes == (
        "catalog_coverage_mismatch",
        "invalid_security_code",
    )
    invalid_sector = next(item for item in invalid.sectors if item.name == "有效")
    assert invalid_sector.members == ("SH.600000",)
    assert invalid_sector.eligible_for_entry is False
    assert invalid_sector.reason_codes == ("invalid_security_code",)

    collision_names = ["GICS1父级", "GICS3白酒", "GICS3 白酒"]
    _install_native(
        monkeypatch,
        sector_list=collision_names,
        sector_info=_info(collision_names),
        members={
            "GICS1父级": ["600000.SH", "000001.SZ"],
            "GICS3白酒": ["600000.SH"],
            "GICS3 白酒": ["000001.SZ"],
        },
    )
    collision = _snapshot()
    assert collision.eligible_for_entry is False
    assert collision.reason_codes == ("parent_mapping_conflict",)
    assert collision.parent_mapping_conflicts == (
        "GICS3 白酒",
        "GICS3白酒",
    )
    assert all(item.name != "白酒" for item in collision.sectors)


def test_qmt_gics_catalog_quarantines_normalized_collision_when_one_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["GICS1父级", "GICS3白酒", "GICS3 白酒"]
    _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1父级": ["600000.SH", "000001.SZ"],
            "GICS3白酒": ["600000.SH"],
            "GICS3 白酒": RuntimeError("native membership failure"),
        },
    )

    snapshot = _snapshot()

    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == (
        "catalog_coverage_mismatch",
        "parent_mapping_conflict",
        "sector_membership_query_failed",
        "sector_membership_query_failed:GICS3 白酒",
    )
    assert snapshot.parent_mapping_conflicts == (
        "GICS3 白酒",
        "GICS3白酒",
    )
    assert all(item.normalized_name != "白酒" for item in snapshot.sectors)


def test_qmt_gics_catalog_applies_nfkc_whitespace_case_code_and_dedup_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent_key = "  ＧＩＣＳ１　消费　 服务  "
    child_key = "ＧＩＣＳ３　白酒   行业"
    ignored_lowercase_key = "gics3忽略"
    ignored_source_keys = (
        ignored_lowercase_key,
        "普通板块",
        "  ＧＩＣＳ１　 ",
        "GICS3   ",
    )
    names = [parent_key, child_key, *ignored_source_keys]
    _, fake = _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            parent_key: ["６００５１９．ｓｈ", " 000858.sz ", "600519.SH"],
            child_key: [
                "６００５１９．ｓｈ",
                "600519.SH",
                " 000858.sz ",
                "SH.600002",
                "　",
            ],
            **{
                key: AssertionError("invalid or non-GICS source key must be ignored")
                for key in ignored_source_keys
            },
        },
    )

    snapshot = _snapshot()

    blank_token = "invalid-code:" + sha256_json("").removeprefix("sha256:")
    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == ("invalid_security_code",)
    assert snapshot.invalid_codes == ("SH.600002", blank_token)
    assert len(snapshot.sectors) == 1
    sector = snapshot.sectors[0]
    assert sector.name == "白酒 行业"
    assert sector.normalized_name == "白酒 行业"
    assert sector.parent_gics1_id == "GICS1 消费 服务"
    assert sector.parent_gics1_name == "消费 服务"
    assert sector.members == ("SH.600519", "SZ.000858")
    assert sector.eligible_for_entry is False
    assert sector.reason_codes == ("invalid_security_code",)
    assert not any(call[1] in set(ignored_source_keys) for call in fake.calls)


def test_qmt_gics_catalog_rejects_invalid_gics1_codes_without_local_contamination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["GICS1父级", "GICS3子级"]
    _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1父级": ["600000.SH", "BAD"],
            "GICS3子级": ["600000.SH"],
        },
    )

    snapshot = _snapshot()

    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == ("invalid_security_code",)
    assert snapshot.invalid_codes == ("BAD",)
    assert len(snapshot.sectors) == 1
    sector = snapshot.sectors[0]
    assert sector.members == ("SH.600000",)
    assert sector.eligible_for_entry is True
    assert sector.reason_codes == ()


@pytest.mark.parametrize(
    "failed_value",
    (
        RuntimeError("native membership failure"),
        None,
        ("600000.SH",),
        ["600000.SH", object()],
    ),
)
def test_qmt_gics_catalog_records_gics1_query_failures(
    monkeypatch: pytest.MonkeyPatch,
    failed_value: object,
) -> None:
    names = ["GICS1故障", "GICS1父级", "GICS3子级"]
    _, fake = _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1故障": failed_value,
            "GICS1父级": ["600000.SH"],
            "GICS3子级": ["600000.SH"],
        },
    )

    snapshot = _snapshot()

    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == (
        "sector_membership_query_failed",
        "sector_membership_query_failed:GICS1故障",
    )
    assert snapshot.sectors == ()
    assert snapshot.invalid_codes == ()
    assert snapshot.empty_sector_names == ()
    assert snapshot.parent_mapping_conflicts == ()
    assert [call[:2] for call in fake.calls] == [
        ("get_sector_list", None),
        ("get_sector_info", ""),
        ("get_stock_list_in_sector", "GICS1故障"),
        ("get_stock_list_in_sector", "GICS1父级"),
        ("get_stock_list_in_sector", "GICS3子级"),
    ]


def test_qmt_gics_catalog_records_alias_collision_even_when_gics1_query_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["GICS1故障", "GICS1父级", "GICS3白酒", "GICS3 白酒"]
    _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1故障": RuntimeError("native membership failure"),
            "GICS1父级": ["600000.SH", "000001.SZ"],
            "GICS3白酒": ["600000.SH"],
            "GICS3 白酒": ["000001.SZ"],
        },
    )

    snapshot = _snapshot()

    assert snapshot.sectors == ()
    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == (
        "parent_mapping_conflict",
        "sector_membership_query_failed",
        "sector_membership_query_failed:GICS1故障",
    )
    assert snapshot.parent_mapping_conflicts == (
        "GICS3 白酒",
        "GICS3白酒",
    )


@pytest.mark.parametrize(
    "failed_value",
    (
        RuntimeError("native membership failure"),
        None,
        ("600000.SH",),
        ["600000.SH", object()],
    ),
)
def test_qmt_gics_catalog_records_empty_and_query_failed_sectors(
    monkeypatch: pytest.MonkeyPatch,
    failed_value: object,
) -> None:
    names = ["GICS1父级", "GICS3故障", "GICS3空"]
    _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1父级": ["600000.SH"],
            "GICS3故障": failed_value,
            "GICS3空": [],
        },
    )

    snapshot = _snapshot()

    assert snapshot.eligible_for_entry is False
    assert snapshot.reason_codes == (
        "catalog_coverage_mismatch",
        "empty_gics3_sector",
        "parent_mapping_conflict",
        "sector_membership_query_failed",
        "sector_membership_query_failed:GICS3故障",
    )
    assert snapshot.invalid_codes == ()
    assert snapshot.empty_sector_names == ("GICS3空",)
    assert snapshot.parent_mapping_conflicts == ("GICS3空",)
    assert snapshot.sectors == ()

    quarantine_names = ["GICS1父级", "GICS3甲", "GICS3乙"]
    _install_native(
        monkeypatch,
        sector_list=quarantine_names,
        sector_info=_info(quarantine_names),
        members={name: ["600000.SH"] for name in quarantine_names},
    )
    quarantine = _snapshot()
    assert quarantine.eligible_for_entry is False
    assert quarantine.reason_codes == (
        "ambiguous_gics3_membership",
        "empty_gics3_sector",
    )
    assert quarantine.empty_sector_names == ("GICS3乙", "GICS3甲")
    assert {item.members for item in quarantine.sectors} == {()}
    assert all(item.eligible_for_entry is False for item in quarantine.sectors)
    assert {
        item.reason_codes for item in quarantine.sectors
    } == {("ambiguous_gics3_membership", "empty_gics3_sector")}


def test_catalog_membership_fingerprint_is_stable_across_order_capture_and_service_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = ["GICS1父级", "GICS3甲", "GICS3乙"]
    base_members = {
        "GICS1父级": ["600000.SH", "000001.SZ"],
        "GICS3甲": ["600000.SH"],
        "GICS3乙": ["000001.SZ"],
    }

    def capture(
        capture_names: list[str],
        members: dict[str, object],
        *,
        category: str = "行业",
        at: datetime = _at(),
    ):
        _install_native(
            monkeypatch,
            sector_list=capture_names,
            sector_info=_info(capture_names, category=category),
            members=members,
        )
        return _snapshot(at)

    base = capture(names, base_members)
    order_only = capture(
        list(reversed(names)),
        {
            name: list(reversed(values))
            for name, values in reversed(tuple(base_members.items()))
        },
        category="行业",
        at=_at(11, 0),
    )
    assert base.source_service_id == order_only.source_service_id
    assert base.membership_fingerprint == order_only.membership_fingerprint

    service_changed = capture(
        names,
        base_members,
        category="行业-v2",
        at=_at(14, 59),
    )
    assert base.source_service_id != service_changed.source_service_id
    assert base.captured_at != service_changed.captured_at
    assert base.membership_fingerprint == service_changed.membership_fingerprint

    changed_members = capture(
        names,
        {
            "GICS1父级": ["600000.SH", "000001.SZ", "600001.SH"],
            "GICS3甲": ["600000.SH", "600001.SH"],
            "GICS3乙": ["000001.SZ"],
        },
    )
    invalid = capture(
        names,
        {**base_members, "GICS3甲": ["600000.SH", "BAD"]},
    )
    ambiguous = capture(
        names,
        {**base_members, "GICS3乙": ["000001.SZ", "600000.SH"]},
    )
    empty_names = [*names, "GICS3空"]
    empty = capture(
        empty_names,
        {**base_members, "GICS3空": []},
    )
    conflict_names = [
        "GICS1父级A",
        "GICS1父级B",
        "GICS3甲",
        "GICS3乙",
        "GICS3冲突",
    ]
    conflict = capture(
        conflict_names,
        {
            "GICS1父级A": ["600000.SH"],
            "GICS1父级B": ["000001.SZ"],
            "GICS3甲": ["600000.SH"],
            "GICS3乙": ["000001.SZ"],
            "GICS3冲突": ["600000.SH", "000001.SZ"],
        },
    )
    fingerprints = {
        base.membership_fingerprint,
        changed_members.membership_fingerprint,
        invalid.membership_fingerprint,
        ambiguous.membership_fingerprint,
        empty.membership_fingerprint,
        conflict.membership_fingerprint,
    }
    assert len(fingerprints) == 6


def test_qmt_catalog_never_uses_stocks_bkgn_all_ticks_or_second_module_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert subject._XTDATA_NATIVE_LOCK is exchange_qmt._XTDATA_NATIVE_LOCK
    names = ["GICS1父级", "GICS3子级", "StocksBKGN"]
    _, fake = _install_native(
        monkeypatch,
        sector_list=names,
        sector_info=_info(names),
        members={
            "GICS1父级": ["600000.SH"],
            "GICS3子级": ["600000.SH"],
            "StocksBKGN": AssertionError("forbidden generic sector queried"),
        },
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden generic ExchangeQMT seam called")

    monkeypatch.setattr(exchange_qmt.ExchangeQMT, "all_stocks", forbidden)
    monkeypatch.setattr(exchange_qmt.ExchangeQMT, "all_ticks", forbidden)
    monkeypatch.setattr(exchange_qmt.ExchangeQMT, "stock_info", forbidden)
    monkeypatch.setattr(exchange_qmt.ExchangeQMT, "ticks", forbidden)
    monkeypatch.setattr(exchange_qmt.ExchangeQMT, "stock_owner_plate", forbidden)
    monkeypatch.setattr(exchange_qmt.ExchangeQMT, "plate_stocks", forbidden)

    snapshot = _snapshot()

    assert snapshot.eligible_for_entry is True
    assert not any(call[1] == "StocksBKGN" for call in fake.calls)
    source = Path(inspect.getsourcefile(subject) or subject.__file__).read_text(
        encoding="utf-8"
    )
    assert (
        "from chanlun.exchange.exchange_qmt import _XTDATA_NATIVE_LOCK" in source
    )
    for forbidden_token in (
        "src.chanlun",
        "StocksBKGN",
        "download_sector_data(",
        "get_full_tick(",
        "get_instrument_detail(",
        "all_stocks(",
        "all_ticks(",
        "stock_owner_plate(",
        "plate_stocks(",
        "import threading",
        "from threading import",
        "threading.Lock",
        "threading.RLock",
        "Lock(",
        "RLock(",
    ):
        assert forbidden_token not in source
