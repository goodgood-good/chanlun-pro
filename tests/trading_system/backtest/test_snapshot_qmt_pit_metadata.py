from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
import sys
from types import SimpleNamespace
import pytest

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    CN,
    PITMetadataSnapshot,
    SecurityMasterRecord,
    SectorMembershipChange,
    sha256_json,
    snapshot_payload,
)
from chanlun.decision_support.trading_system.backtest.pit_scope import (
    PIT_SCOPE_SCHEMA,
    SCOPED_SECTOR_CLOSURE_MODE,
    scope_source_hashes,
    sector_closure_codes,
    validate_scope_proof,
)
from tools import finalize_qmt_pit_fixed_year as finalize_subject
from tools import backtest_qmt_fixed_year as extract_subject
from tools import snapshot_qmt_pit_metadata as subject


START = date(2025, 5, 1)
END = date(2026, 7, 24)


def _membership(code: str, sector: str, changed: date) -> SectorMembershipChange:
    return SectorMembershipChange(
        code=code,
        sector_id=f"qmt-sw1:{sector[:3]}",
        sector_name={"S11": "农林牧渔", "S22": "基础化工"}[sector[:3]],
        industry_code=sector,
        source_changed_on=changed,
        known_at=datetime(
            changed.year,
            changed.month,
            changed.day,
            tzinfo=CN,
        ).replace(hour=0)
        + timedelta(days=1),
    )


def _scope_fixture() -> tuple[PITMetadataSnapshot, dict[str, object]]:
    memberships = (
        _membership("SH.600001", "S1101", date(2020, 1, 1)),
        _membership("SH.600002", "S1101", date(2021, 1, 1)),
    )
    closure = ("SH.600001", "SH.600002")
    excluded_identity = "SH.600003"
    outside_intervals = [
        {
            "code": excluded_identity,
            "native_code": "600003.SH",
            "listed_from": None,
            "listed_through": "2024-12-31",
            "created_on": None,
            "relation": "BEFORE_REPLAY_RANGE",
            "proof_basis": "EXPIRE_DATE_BEFORE_REPLAY",
            "raw_date_fields": {
                "OpenDate": "0",
                "ExpireDate": "20241231",
                "CreateDate": "",
            },
        }
    ]
    inventory = (*closure, excluded_identity)
    scope: dict[str, object] = {
        "schema": PIT_SCOPE_SCHEMA,
        "mode": SCOPED_SECTOR_CLOSURE_MODE,
        "requested_codes": ["SH.600001"],
        "requested_code_count": 1,
        "selected_sector_ids": ["qmt-sw1:S11"],
        "selected_sector_count": 1,
        "closure_codes": list(closure),
        "closure_code_count": 2,
        "closure_candidate_codes": list(closure),
        "closure_candidate_code_count": 2,
        "excluded_closure_candidate_codes": [],
        "sector_closure_complete": True,
        "enumerated_contract_code_count": 3,
        "enumerated_contract_codes_sha256": sha256_json(list(inventory)),
        "membership_checkpoint_count": 2,
        "membership_checkpoint_tree_sha256": "sha256:" + "7" * 64,
        "missing_checkpoint_codes": [],
        "checkpoint_absent_identity_codes": [excluded_identity],
        "uncertified_checkpoint_absent_identity_codes": [],
        "excluded_identity_codes": [excluded_identity],
        "excluded_identity_count": 1,
        "certified_outside_range_identity_count": 1,
        "certified_outside_range_intervals": outside_intervals,
        "certified_outside_range_intervals_sha256": sha256_json(
            outside_intervals
        ),
        "detail_read_codes": list(inventory),
        "detail_read_code_count": 3,
        "factor_read_code_count": 2,
        "large_scope_confirmed": False,
    }
    snapshot = PITMetadataSnapshot(
        source_start=START,
        source_end=END,
        captured_at=datetime(2026, 8, 24, tzinfo=CN),
        securities=tuple(
            SecurityMasterRecord(
                code=code,
                name=f"证券{code[-1]}",
                listed_from=date(2010, 1, 1),
                listed_through=None,
            )
            for code in closure
        ),
        memberships=memberships,
        factors=(),
        qmt_sw1_sector_names=(("qmt-sw1:S11", "农林牧渔"),),
        source_hashes=tuple(
            sorted(
                (
                    *scope_source_hashes(scope),
                    (
                        "qmt_a_share_contract_inventory",
                        str(scope["enumerated_contract_codes_sha256"]),
                    ),
                    (
                        "cninfo_membership_universe_checkpoint_tree",
                        str(scope["membership_checkpoint_tree_sha256"]),
                    ),
                )
            )
        ),
    )
    return snapshot, scope


def test_snapshot_scope_is_required_before_qmt_is_imported() -> None:
    with pytest.raises(ValueError, match="bounded PIT scope required"):
        subject.main(["--output", "unused.json"])
    with pytest.raises(ValueError, match="requires --confirm-large-scope"):
        subject.main(["--full-market", "--output", "unused.json"])


def test_codes_file_is_normalized_and_default_worker_count_is_small(tmp_path) -> None:
    path = tmp_path / "codes.txt"
    path.write_text("# smoke\nsh.600001\nSZ.000002\n", encoding="utf-8")
    args = subject.parser().parse_args(
        ["--codes-file", str(path), "--output", str(tmp_path / "pit.json")]
    )

    assert args.workers == 2
    assert subject._normalized_requested_codes(args) == (
        "SH.600001",
        "SZ.000002",
    )


def test_historical_sector_closure_excludes_unrelated_sector() -> None:
    memberships = (
        _membership("SH.600001", "S1101", date(2020, 1, 1)),
        _membership("SH.600002", "S1102", date(2020, 1, 1)),
        _membership("SH.600003", "S2201", date(2020, 1, 1)),
    )

    closure, sectors = sector_closure_codes(
        memberships,
        requested_codes=("SH.600001",),
        start=START,
        end=END,
    )

    assert sectors == ("qmt-sw1:S11",)
    assert closure == ("SH.600001", "SH.600002")


def test_scoped_checkpoint_inventory_fails_closed_without_network(tmp_path) -> None:
    code = "SH.600001"
    records: list[object] = []
    (tmp_path / "SH_600001.json").write_text(
        json.dumps(
            {
                "schema": "cninfo-p-stock2110-checkpoint",
                "code": code,
                "not_after": END.isoformat(),
                "records": records,
                "records_sha256": sha256_json(records),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="invalid CNInfo checkpoints"):
        subject._load_complete_checkpoint_inventory(
            inventory_codes=(code, "SH.600002"),
            checkpoint_dir=tmp_path,
            end=END,
        )


def test_checkpoint_absent_identity_requires_strict_outside_range_detail() -> None:
    before = subject._strict_outside_range_identity_proof(
        code="SH.600001",
        native_code="600001.SH",
        detail={"OpenDate": "0", "ExpireDate": "20241231"},
        start=START,
        end=END,
    )
    after = subject._strict_outside_range_identity_proof(
        code="SH.600002",
        native_code="600002.SH",
        detail={
            "OpenDate": "19700101",
            "ExpireDate": "99999999",
            "CreateDate": "20260820",
        },
        start=START,
        end=END,
    )

    assert before["relation"] == "BEFORE_REPLAY_RANGE"
    assert before["proof_basis"] == "EXPIRE_DATE_BEFORE_REPLAY"
    assert after["relation"] == "AFTER_REPLAY_RANGE"
    assert after["proof_basis"] == (
        "CREATE_DATE_AFTER_REPLAY_WITH_OPEN_PLACEHOLDER"
    )
    with pytest.raises(RuntimeError, match="intersects the replay range"):
        subject._strict_outside_range_identity_proof(
            code="SH.600003",
            native_code="600003.SH",
            detail={"OpenDate": "20200101", "ExpireDate": "99999999"},
            start=START,
            end=END,
        )
    with pytest.raises(RuntimeError, match="detail is unreadable"):
        subject._strict_outside_range_identity_proof(
            code="SH.600004",
            native_code="600004.SH",
            detail=None,
            start=START,
            end=END,
        )
    with pytest.raises(RuntimeError, match="OpenDate is invalid"):
        subject._strict_outside_range_identity_proof(
            code="SH.600005",
            native_code="600005.SH",
            detail={"OpenDate": "0", "ExpireDate": "99999999"},
            start=START,
            end=END,
        )


@pytest.mark.parametrize(
    ("code", "detail", "proof_basis"),
    (
        (
            "SH.600849",
            {"OpenDate": "0", "ExpireDate": "20050629"},
            "EXPIRE_DATE_BEFORE_REPLAY",
        ),
        (
            "SZ.002525",
            {"OpenDate": "0", "ExpireDate": "20101208"},
            "EXPIRE_DATE_BEFORE_REPLAY",
        ),
        (
            "SZ.300060",
            {"OpenDate": "0", "ExpireDate": "20100309"},
            "EXPIRE_DATE_BEFORE_REPLAY",
        ),
        (
            "SZ.301688",
            {
                "OpenDate": "19700101",
                "ExpireDate": "99999999",
                "CreateDate": "20260820",
            },
            "CREATE_DATE_AFTER_REPLAY_WITH_OPEN_PLACEHOLDER",
        ),
        (
            "SZ.301697",
            {
                "OpenDate": "19700101",
                "ExpireDate": "99999999",
                "CreateDate": "20260819",
            },
            "CREATE_DATE_AFTER_REPLAY_WITH_OPEN_PLACEHOLDER",
        ),
        (
            "SH.688825",
            {"OpenDate": "20260727", "ExpireDate": "10011011"},
            "OPEN_DATE_AFTER_REPLAY",
        ),
        (
            "SH.688826",
            {"OpenDate": "20260818", "ExpireDate": "10011011"},
            "OPEN_DATE_AFTER_REPLAY",
        ),
        (
            "SH.688828",
            {"OpenDate": "20260811", "ExpireDate": "10001011"},
            "OPEN_DATE_AFTER_REPLAY",
        ),
        (
            "SH.688836",
            {"OpenDate": "20260819", "ExpireDate": "10011111"},
            "OPEN_DATE_AFTER_REPLAY",
        ),
    ),
)
def test_known_qmt_outside_range_placeholders_are_strictly_certified(
    code,
    detail,
    proof_basis,
) -> None:
    market, digits = code.split(".", 1)
    proof = subject._strict_outside_range_identity_proof(
        code=code,
        native_code=f"{digits}.{market}",
        detail=detail,
        start=START,
        end=END,
    )

    assert proof["proof_basis"] == proof_basis
    assert proof["raw_date_fields"] == {
        "OpenDate": str(detail.get("OpenDate") or ""),
        "ExpireDate": str(detail.get("ExpireDate") or ""),
        "CreateDate": str(detail.get("CreateDate") or ""),
    }


@pytest.mark.parametrize(
    ("detail", "message"),
    (
        (None, "detail is unreadable"),
        (
            {"OpenDate": "20200101", "ExpireDate": "99999999"},
            "intersects the replay range",
        ),
    ),
)
def test_checkpoint_absent_identity_mock_loader_fails_closed(detail, message) -> None:
    calls: list[tuple[str, bool]] = []

    def load(native: str, *, iscomplete: bool):
        calls.append((native, iscomplete))
        return detail

    with pytest.raises(RuntimeError, match=message):
        subject._certify_checkpoint_absent_identities(
            codes=("SH.600001",),
            native_by_code={"SH.600001": "600001.SH"},
            start=START,
            end=END,
            detail_loader=load,
        )
    assert calls == [("600001.SH", False)]


def test_scope_proof_binds_requested_codes_and_complete_sector_closure(
    tmp_path,
) -> None:
    snapshot, scope = _scope_fixture()
    assert (
        validate_scope_proof(
            snapshot=snapshot,
            scope=scope,
            replay_codes=("SH.600001",),
        )
        == ()
    )

    path = tmp_path / "pit_metadata.json"
    path.write_text(
        json.dumps(snapshot_payload(snapshot, audit={"scope": scope})),
        encoding="utf-8",
    )
    assert (
        finalize_subject._pit_scope_failures(
            path=path,
            snapshot=snapshot,
            replay_codes=("SH.600001",),
        )
        == ()
    )
    assert (
        extract_subject._pit_scope_failures(
            path=path,
            snapshot=snapshot,
            replay_codes=("SH.600001",),
        )
        == ()
    )

    tampered = {**scope, "requested_codes": ["SH.600002"]}
    failures = validate_scope_proof(
        snapshot=snapshot,
        scope=tampered,
        replay_codes=("SH.600001",),
    )
    assert "pit_scope_contract_mismatch" in failures
    assert "pit_requested_replay_scope_mismatch" in failures

    tampered_intervals = [
        {
            **dict(scope["certified_outside_range_intervals"][0]),
            "listed_through": "2025-05-01",
        }
    ]
    tampered = {**scope, "certified_outside_range_intervals": tampered_intervals}
    failures = validate_scope_proof(snapshot=snapshot, scope=tampered)
    assert "pit_scope_contract_mismatch" in failures
    assert "pit_scope_certified_outside_range_intervals_mismatch" in failures
    assert "pit_outside_range_interval_hash_mismatch" in failures
    assert "pit_outside_range_interval_invalid" in failures


def test_create_date_raw_proof_tampering_is_rejected() -> None:
    snapshot, scope = _scope_fixture()
    create_proof = subject._strict_outside_range_identity_proof(
        code="SH.600003",
        native_code="600003.SH",
        detail={
            "OpenDate": "19700101",
            "ExpireDate": "99999999",
            "CreateDate": "20260820",
        },
        start=START,
        end=END,
    )
    create_scope = {
        **scope,
        "certified_outside_range_intervals": [create_proof],
        "certified_outside_range_intervals_sha256": sha256_json([create_proof]),
    }
    non_scope_hashes = tuple(
        (name, digest)
        for name, digest in snapshot.source_hashes
        if not name.startswith("pit_scope_")
    )
    create_snapshot = replace(
        snapshot,
        source_hashes=tuple(
            sorted((*non_scope_hashes, *scope_source_hashes(create_scope)))
        ),
    )
    assert validate_scope_proof(snapshot=create_snapshot, scope=create_scope) == ()

    tampered_proof = {
        **create_proof,
        "raw_date_fields": {
            **dict(create_proof["raw_date_fields"]),
            "CreateDate": "20260724",
        },
    }
    tampered_scope = {
        **create_scope,
        "certified_outside_range_intervals": [tampered_proof],
    }
    failures = validate_scope_proof(
        snapshot=create_snapshot,
        scope=tampered_scope,
    )
    assert "pit_scope_contract_mismatch" in failures
    assert "pit_scope_certified_outside_range_intervals_mismatch" in failures
    assert "pit_outside_range_interval_hash_mismatch" in failures
    assert "pit_outside_range_interval_invalid" in failures


def test_open_date_after_replay_binds_but_does_not_parse_weird_expiry() -> None:
    snapshot, scope = _scope_fixture()
    open_proof = subject._strict_outside_range_identity_proof(
        code="SH.600003",
        native_code="600003.SH",
        detail={"OpenDate": "20260727", "ExpireDate": "10011011"},
        start=START,
        end=END,
    )
    open_scope = {
        **scope,
        "certified_outside_range_intervals": [open_proof],
        "certified_outside_range_intervals_sha256": sha256_json([open_proof]),
    }
    non_scope_hashes = tuple(
        (name, digest)
        for name, digest in snapshot.source_hashes
        if not name.startswith("pit_scope_")
    )
    open_snapshot = replace(
        snapshot,
        source_hashes=tuple(
            sorted((*non_scope_hashes, *scope_source_hashes(open_scope)))
        ),
    )
    assert open_proof["proof_basis"] == "OPEN_DATE_AFTER_REPLAY"
    assert validate_scope_proof(snapshot=open_snapshot, scope=open_scope) == ()

    tampered_proof = {
        **open_proof,
        "raw_date_fields": {
            **dict(open_proof["raw_date_fields"]),
            "ExpireDate": "10001011",
        },
    }
    tampered_scope = {
        **open_scope,
        "certified_outside_range_intervals": [tampered_proof],
    }
    failures = validate_scope_proof(snapshot=open_snapshot, scope=tampered_scope)
    assert "pit_scope_contract_mismatch" in failures
    assert "pit_scope_certified_outside_range_intervals_mismatch" in failures
    assert "pit_outside_range_interval_hash_mismatch" in failures
    assert "pit_outside_range_interval_invalid" not in failures


def test_scoped_capture_reads_details_and_factors_only_for_sector_closure(
    monkeypatch,
    tmp_path,
) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir()
    memberships = {
        "SH.600001": ("S1101", "农林牧渔"),
        "SH.600002": ("S1102", "农林牧渔"),
        "SH.600003": ("S2201", "基础化工"),
    }
    for code, (industry, name) in memberships.items():
        records = [
            {
                "F001V": "008003",
                "VARYDATE": "2020-01-01",
                "F003V": industry,
                "F004V": name,
            }
        ]
        (checkpoint_dir / f"{code.replace('.', '_')}.json").write_text(
            json.dumps(
                {
                    "schema": "cninfo-p_stock2110-checkpoint",
                    "code": code,
                    "not_after": END.isoformat(),
                    "records": records,
                    "records_sha256": sha256_json(records),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    inventory_codes = tuple(sorted(memberships))
    indexed_memberships, checkpoint_paths = (
        subject._load_complete_checkpoint_inventory(
            inventory_codes=inventory_codes,
            checkpoint_dir=checkpoint_dir,
            end=END,
        )
    )
    membership_index = tmp_path / "membership_index.json"
    membership_index.write_text(
        json.dumps(
            subject._membership_index_payload(
                memberships=indexed_memberships,
                checkpoint_paths=checkpoint_paths,
                checkpoint_root=checkpoint_dir,
                end=END,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    detail_calls: list[tuple[str, ...]] = []
    factor_calls: list[tuple[str, ...]] = []

    def security_master(_start, _end, *, native_codes):
        detail_calls.append(tuple(native_codes))
        rows = tuple(
            SecurityMasterRecord(
                code=f"{native[-2:]}.{native[:6]}",
                name=native,
                listed_from=date(2010, 1, 1),
                listed_through=None,
            )
            for native in sorted(native_codes)
        )
        return rows, {"detail_read_code_count": len(rows)}

    def capture_factors(*, securities, start, end):
        del start, end
        factor_calls.append(tuple(row.code for row in securities))
        return (), []

    monkeypatch.delitem(sys.modules, "xtquant", raising=False)
    monkeypatch.setattr(
        subject,
        "_qmt_a_share_inventory",
        lambda: pytest.fail("scoped PIT must never enumerate QMT full inventory"),
    )
    monkeypatch.setattr(subject, "_security_master", security_master)
    monkeypatch.setattr(subject, "_capture_factors", capture_factors)
    monkeypatch.setattr(
        subject,
        "_cninfo_headers",
        lambda: pytest.fail("scoped capture must not initialize CNInfo network auth"),
    )
    monkeypatch.setattr(
        subject,
        "_taxonomy",
        lambda _headers: pytest.fail("scoped capture must not fetch taxonomy"),
    )
    output = tmp_path / "smoke2" / "pit_metadata.json"

    assert (
        subject.main(
            [
                "--codes",
                "SH.600001",
                "--membership-checkpoint-dir",
                str(checkpoint_dir),
                "--membership-index",
                str(membership_index),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    assert detail_calls == [("600001.SH", "600002.SH")]
    assert "xtquant" not in sys.modules
    assert factor_calls == [("SH.600001", "SH.600002")]
    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["audit"]["scope"]["requested_codes"] == ["SH.600001"]
    assert raw["audit"]["scope"]["closure_codes"] == [
        "SH.600001",
        "SH.600002",
    ]
    assert raw["audit"]["scope"]["excluded_identity_codes"] == []
    assert raw["audit"]["scope"]["membership_checkpoint_count"] == 3
    assert raw["audit"]["scope"]["detail_read_codes"] == [
        "SH.600001",
        "SH.600002",
    ]


def test_full_capture_reuses_an_exact_immutable_membership_index(
    monkeypatch,
    tmp_path,
) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir()
    codes = ("SH.600001", "SH.600002")
    for code in codes:
        records = [
            {
                "F001V": "008003",
                "VARYDATE": "2020-01-01",
                "F003V": "S1101",
                "F004V": "农林牧渔",
            }
        ]
        (checkpoint_dir / f"{code.replace('.', '_')}.json").write_text(
            json.dumps(
                {
                    "schema": "cninfo-p_stock2110-checkpoint",
                    "code": code,
                    "not_after": END.isoformat(),
                    "records": records,
                    "records_sha256": sha256_json(records),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    indexed_memberships, checkpoint_paths = (
        subject._load_complete_checkpoint_inventory(
            inventory_codes=codes,
            checkpoint_dir=checkpoint_dir,
            end=END,
        )
    )
    membership_index = tmp_path / "membership_index.json"
    membership_index.write_text(
        json.dumps(
            subject._membership_index_payload(
                memberships=indexed_memberships,
                checkpoint_paths=checkpoint_paths,
                checkpoint_root=checkpoint_dir,
                end=END,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setitem(
        sys.modules,
        "xtquant",
        SimpleNamespace(xtdata=SimpleNamespace(enable_hello=True)),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_a_share_inventory",
        lambda: tuple((code, subject.qmt_native_code(code)) for code in codes),
    )
    monkeypatch.setattr(
        subject,
        "_security_master",
        lambda _start, _end, *, native_codes: (
            tuple(
                SecurityMasterRecord(
                    code=f"{native[-2:]}.{native[:6]}",
                    name=native,
                    listed_from=date(2010, 1, 1),
                    listed_through=None,
                )
                for native in sorted(native_codes)
            ),
            {"detail_read_code_count": len(native_codes)},
        ),
    )
    monkeypatch.setattr(subject, "_cninfo_headers", lambda: {})
    monkeypatch.setattr(
        subject,
        "_taxonomy",
        lambda _headers: ({"qmt-sw1:S11": "农林牧渔"}, {"rows": ["S11"]}),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_current_sw1",
        lambda _taxonomy: {"qmt-sw1:S11": codes},
    )
    monkeypatch.setattr(
        subject,
        "_capture_memberships",
        lambda **_kwargs: pytest.fail(
            "an exact immutable full-market index must not be downloaded again"
        ),
    )
    monkeypatch.setattr(
        subject,
        "_capture_factors",
        lambda **_kwargs: ((), []),
    )
    output = tmp_path / "full" / "pit.json"

    assert (
        subject.main(
            [
                "--full-market",
                "--confirm-large-scope",
                "--membership-checkpoint-dir",
                str(checkpoint_dir),
                "--membership-index",
                str(membership_index),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    raw = json.loads(output.read_text(encoding="utf-8"))
    assert raw["audit"]["scope"]["closure_codes"] == list(codes)
    assert raw["audit"]["scope"]["membership_checkpoint_count"] == 2
    assert raw["audit"]["membership_checkpoint_source"] == {
        "mode": "IMMUTABLE_INDEX_REUSE",
        "membership_index": str(membership_index.resolve()),
        "checkpoint_directory": str(checkpoint_dir.resolve()),
    }
    assert not (output.parent / "pit_sources").exists()


def test_full_capture_requires_a_complete_checkpoint_reuse_pair(tmp_path) -> None:
    with pytest.raises(ValueError, match="requires both"):
        subject.main(
            [
                "--full-market",
                "--confirm-large-scope",
                "--membership-index",
                str(tmp_path / "membership_index.json"),
                "--output",
                str(tmp_path / "pit.json"),
            ]
        )


def test_scoped_capture_missing_membership_index_fails_before_xtquant(
    monkeypatch,
    tmp_path,
) -> None:
    checkpoint_dir = tmp_path / "checkpoints"
    checkpoint_dir.mkdir()
    monkeypatch.delitem(sys.modules, "xtquant", raising=False)

    with pytest.raises(ValueError, match="immutable --membership-index"):
        subject.main(
            [
                "--codes",
                "SH.600001",
                "--membership-checkpoint-dir",
                str(checkpoint_dir),
                "--output",
                str(tmp_path / "pit.json"),
            ]
        )
    assert "xtquant" not in sys.modules
