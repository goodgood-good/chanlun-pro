from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import pickle
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    PITMetadataSnapshot,
    SecurityMasterRecord,
)
from chanlun.decision_support.trading_system.recent_year_research import (
    RECENT_YEAR_SELECTION_PATH,
)
from chanlun.decision_support.trading_system.recent_year_provenance import (
    RECENT_YEAR_RESEARCH_ALGORITHM_PATHS,
    RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
    recent_year_research_algorithm_hashes,
    recent_year_research_algorithm_revision,
)
from chanlun.decision_support.trading_system.sector_first_trigger_plan import (
    SectorFirstTriggerEvent,
    SectorFirstTriggerLedger,
    SectorTriggerRankFact,
    sector_trigger_windows_for_current_member,
)
from tools.build_recent_year_current_sector_triggers import (
    _catalog_scope,
    _pit_snapshot_available_at,
)
from tools.extract_sector_first_direct_facts import _load_query_plan
from tools.prescreen_sector_first_research_candidates import (
    _checkpoint_path,
    _checkpoint_payload,
    _conservative_superset_negative_rows,
    _current_sector_interval_index,
    _listed_current_sector_windows,
    _load_row_checkpoint,
)


CN = ZoneInfo("Asia/Shanghai")


def test_current_catalog_scope_filters_stale_and_tiny_qmt_sectors() -> None:
    active = tuple(
        SecurityMasterRecord(f"SH.6000{index:02d}", str(index), date(2000, 1, 1), None)
        for index in range(8)
    )
    stale = SecurityMasterRecord(
        "SH.601000", "stale", date(2000, 1, 1), date(2020, 1, 1)
    )
    snapshot = PITMetadataSnapshot(
        source_start=date(2025, 5, 1),
        source_end=date(2026, 7, 24),
        captured_at=datetime(2026, 7, 27, tzinfo=CN),
        securities=tuple(sorted((*active, stale), key=lambda row: row.code)),
        memberships=(),
        factors=(),
        qmt_sw1_sector_names=(),
        source_hashes=(("test", "sha256:" + "1" * 64),),
    )
    entry = {
        "sectors": (
            {
                "sector_id": "qmt-gics3:large",
                "name": "large",
                "member_codes": tuple(row.code for row in active) + (stale.code,),
            },
            {
                "sector_id": "qmt-gics3:tiny",
                "name": "tiny",
                "member_codes": (active[0].code,),
            },
        )
    }

    members, names, excluded = _catalog_scope(
        catalog_entry=entry,
        snapshot=snapshot,
    )

    assert members == {
        "qmt-gics3:large": tuple(row.code for row in active),
    }
    assert names == {"qmt-gics3:large": "large"}
    assert stale.code in excluded
    assert active[0].code not in excluded  # retained through the large sector


def test_historical_pit_availability_uses_signed_capture_time() -> None:
    captured = datetime(2026, 7, 27, 16, 9, tzinfo=CN)
    snapshot = PITMetadataSnapshot(
        source_start=date(2025, 5, 1),
        source_end=date(2026, 7, 24),
        captured_at=captured,
        securities=(),
        memberships=(),
        factors=(),
        qmt_sw1_sector_names=(),
        source_hashes=(("test", "sha256:" + "1" * 64),),
    )
    historical, historical_basis = _pit_snapshot_available_at(
        snapshot=snapshot,
    )

    assert historical == captured
    assert historical_basis == "CONTENT_CAPTURED_AT_HISTORICAL"


def test_terminal_query_plan_hash_is_required_for_causal_rescan(
    tmp_path: Path,
) -> None:
    stable = {
        "schema": "chanlun-sector-first-terminal-query-plan",
        "potential_symbols": ["SH.600000"],
    }
    payload = {
        **stable,
        "content_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
    }
    path = tmp_path / "query.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert _load_query_plan(path)["potential_symbols"] == ["SH.600000"]

    payload["potential_symbols"] = ["SH.600001"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="content hash changed"):
        _load_query_plan(path)


def test_indexed_current_sector_windows_are_semantically_identical() -> None:
    sector_a = "qmt-gics3:a"
    sector_b = "qmt-gics3:b"
    times = (
        datetime(2025, 8, 1, 10, 0, tzinfo=CN),
        datetime(2025, 8, 1, 10, 30, tzinfo=CN),
        datetime(2025, 8, 2, 10, 0, tzinfo=CN),
        datetime(2025, 8, 2, 10, 30, tzinfo=CN),
    )

    def event(observed_at: datetime, sector_id: str) -> SectorFirstTriggerEvent:
        return SectorFirstTriggerEvent(
            observed_at=observed_at,
            ranked_sectors=(
                SectorTriggerRankFact(
                    sector_id=sector_id,
                    sector_name=sector_id,
                    ordinal=1,
                    rank_score=1,
                    regime="neutral",
                    rank_components=(("structure", 1),),
                    reason_codes=("test",),
                ),
            ),
            hard_blocked_sector_ids=(),
            missing_sector_ids=(),
            candidate_symbol_count=1,
            candidate_count_by_sector=((sector_id, 1),),
            candidate_symbols_sha256="sha256:" + "1" * 64,
        )

    ledger = SectorFirstTriggerLedger(
        algorithm_revision="sha256:" + "2" * 64,
        sector_scope_sha256="sha256:" + "3" * 64,
        pit_snapshot_sha256="sha256:" + "4" * 64,
        events=(
            event(times[0], sector_a),
            event(times[1], sector_a),
            event(times[2], sector_a),
            event(times[3], sector_b),
        ),
        sector_source_revisions=((sector_a, "sha256:" + "5" * 64),),
        selection_path=RECENT_YEAR_SELECTION_PATH,
        taxonomy="QMT_GICS3",
        source="QMT_CURRENT_GICS3_COMPOSITE_5M_CAUSAL_TO_30M",
        selection_order=(
            "QMT_CURRENT_SECTOR_TRIGGER",
            "QMT_CURRENT_MEMBERS_BACKFILLED_USER_AUTHORIZED",
        ),
    )
    index = _current_sector_interval_index(ledger)

    for security in (
        SecurityMasterRecord("SH.600000", "all", date(2000, 1, 1), None),
        SecurityMasterRecord("SH.600001", "later", date(2025, 8, 2), None),
        SecurityMasterRecord(
            "SH.600002", "first-day-only", date(2025, 8, 1), date(2025, 8, 1)
        ),
    ):
        expected = sector_trigger_windows_for_current_member(
            ledger=ledger,
            security=security,
            sector_id=sector_a,
        )
        actual = _listed_current_sector_windows(
            intervals=index[sector_a],
            security=security,
        )
        assert actual == expected


def test_terminal_query_row_checkpoint_is_idempotent_and_tamper_evident(
    tmp_path: Path,
) -> None:
    identity = {
        "algorithm_revision": "sha256:" + "1" * 64,
        "trigger_ledger_sha256": "sha256:" + "2" * 64,
        "effective_start": "2025-08-01",
    }
    code = "SH.600000"
    row = {"code": code, "potential": False, "rows_1m": 100}
    path = _checkpoint_path(tmp_path, code)
    path.write_text(
        json.dumps(
            _checkpoint_payload(identity=identity, code=code, row=row),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert _load_row_checkpoint(path, identity=identity, code=code) == row
    assert (
        _load_row_checkpoint(
            path,
            identity={**identity, "effective_start": "2025-08-02"},
            code=code,
        )
        is None
    )

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["row"]["potential"] = True
    path.write_text(json.dumps(tampered), encoding="utf-8")
    assert _load_row_checkpoint(path, identity=identity, code=code) is None


def test_conservative_superset_reuses_only_prior_negative_rows(
    tmp_path: Path,
) -> None:
    observed_at = datetime(2025, 8, 1, 10, 0, tzinfo=CN)
    sector_a = "qmt-gics3:a"
    sector_b = "qmt-gics3:b"
    sector_c = "qmt-gics3:c"
    digest = lambda value: "sha256:" + value * 64
    current_planner_source = digest("b")

    def rank(sector_id: str, ordinal: int) -> SectorTriggerRankFact:
        return SectorTriggerRankFact(
            sector_id=sector_id,
            sector_name=sector_id,
            ordinal=ordinal,
            rank_score=1,
            regime="neutral",
            rank_components=(("structure", 1),),
            reason_codes=("test",),
        )

    def event(sector_ids: tuple[str, ...]) -> SectorFirstTriggerEvent:
        return SectorFirstTriggerEvent(
            observed_at=observed_at,
            ranked_sectors=tuple(
                rank(sector_id, ordinal)
                for ordinal, sector_id in enumerate(sector_ids, start=1)
            ),
            hard_blocked_sector_ids=(),
            missing_sector_ids=(),
            candidate_symbol_count=len(sector_ids),
            candidate_count_by_sector=tuple((sector_id, 1) for sector_id in sector_ids),
            candidate_symbols_sha256=digest("1"),
        )

    def ledger(sector_ids: tuple[str, ...]) -> SectorFirstTriggerLedger:
        return SectorFirstTriggerLedger(
            algorithm_revision=digest("2"),
            sector_scope_sha256=digest("3"),
            pit_snapshot_sha256=digest("4"),
            events=(event(sector_ids),),
            sector_source_revisions=(
                (sector_a, digest("5")),
                (sector_b, digest("6")),
                (sector_c, digest("7")),
            ),
            selection_path=RECENT_YEAR_SELECTION_PATH,
            taxonomy="QMT_GICS3",
            source="QMT_CURRENT_GICS3_COMPOSITE_5M_CAUSAL_TO_30M",
            selection_order=(
                "QMT_CURRENT_SECTOR_TRIGGER",
                "QMT_CURRENT_MEMBERS_BACKFILLED_USER_AUTHORIZED",
            ),
        )

    prior_trigger_path = tmp_path / "prior.pkl"
    prior_trigger_path.write_bytes(pickle.dumps(ledger((sector_a, sector_b))))
    trigger_sha256 = "sha256:" + hashlib.sha256(
        prior_trigger_path.read_bytes()
    ).hexdigest()
    algorithm_path = "src/chanlun/core/example.py"
    algorithm_digest = digest("7")
    stable = {
        "schema": "chanlun-sector-first-terminal-query-plan",
        "authority": "QUERY_PLANNER_ONLY_CAUSAL_RESCAN_REQUIRED",
        "selection_path": RECENT_YEAR_SELECTION_PATH,
        "three_program_mode": "DISABLED_USER_AUTHORIZED",
        "live_status": "LIVE_DISABLED",
        "failed_symbol_count": 0,
        "failures": {},
        "producer_source_sha256": current_planner_source,
        "trigger_ledger_sha256": trigger_sha256,
        "current_catalog_entry_sha256": digest("8"),
        "current_catalog_ledger_sha256": digest("9"),
        "research_parameter_set_id": digest("a"),
        "observation_range": {
            "warmup_start": "2023-05-01",
            "effective_start": "2025-08-01",
            "end": "2026-07-24",
        },
        "algorithm_hashes": [
            {"path": algorithm_path, "sha256": algorithm_digest}
        ],
        "requested_symbol_count": 2,
        "completed_symbol_count": 2,
        "potential_symbol_count": 1,
        "potential_symbols": ["SH.600000"],
        "rows": [
            {"code": "SH.600000", "potential": True},
            {"code": "SH.600001", "potential": False},
        ],
    }
    plan_path = tmp_path / "prior.json"
    plan_path.write_text(
        json.dumps(
            {
                **stable,
                "content_sha256": "sha256:"
                + hashlib.sha256(
                    json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    negatives, rescans, proof = _conservative_superset_negative_rows(
        prior_plan_path=plan_path,
        prior_trigger_path=prior_trigger_path,
        current_trigger=ledger((sector_a,)),
        current_codes=("SH.600000", "SH.600001"),
        current_sector_by_code={
            "SH.600000": sector_a,
            "SH.600001": sector_b,
        },
        current_hashes=((algorithm_path, algorithm_digest),),
        current_planner_source_sha256=current_planner_source,
        catalog_entry_sha256=digest("8"),
        catalog_ledger_sha256=digest("9"),
        research_parameter_set_id=digest("a"),
        warmup_start=date(2023, 5, 1),
        effective_start=date(2025, 8, 1),
        end=date(2026, 7, 24),
    )

    assert tuple(negatives) == ("SH.600001",)
    assert negatives["SH.600001"]["reuse_basis"] == (
        "PRIOR_WIDER_SECTOR_WINDOW_NEGATIVE"
    )
    assert rescans == ("SH.600000",)
    assert proof["status"] == "PROVEN_CONSERVATIVE_RESCAN_COVERAGE"
    assert proof["narrowed_event_count"] == 1

    expanded_negatives, expanded_rescans, expanded_proof = (
        _conservative_superset_negative_rows(
            prior_plan_path=plan_path,
            prior_trigger_path=prior_trigger_path,
            current_trigger=ledger((sector_a, sector_c)),
            current_codes=("SH.600000", "SH.600001"),
            current_sector_by_code={
                "SH.600000": sector_a,
                "SH.600001": sector_c,
            },
            current_hashes=((algorithm_path, algorithm_digest),),
            current_planner_source_sha256=current_planner_source,
            catalog_entry_sha256=digest("8"),
            catalog_ledger_sha256=digest("9"),
            research_parameter_set_id=digest("a"),
            warmup_start=date(2023, 5, 1),
            effective_start=date(2025, 8, 1),
            end=date(2026, 7, 24),
        )
    )
    assert expanded_negatives == {}
    assert expanded_rescans == ("SH.600000", "SH.600001")
    assert expanded_proof["added_sector_ids"] == (sector_c,)
    assert expanded_proof["added_sector_member_rescan_count"] == 1


def test_recent_year_algorithm_scope_is_relevant_and_deterministic() -> None:
    paths = set(RECENT_YEAR_RESEARCH_ALGORITHM_PATHS)
    assert (
        RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE
        == "chanlun-recent-year-sector-technical-research"
    )
    assert (
        "src/chanlun/decision_support/trading_system/backtest/current_sector.py"
        in paths
    )
    assert (
        "src/chanlun/decision_support/trading_system/qmt_causal_factor_adjustment.py"
        in paths
    )
    assert (
        "src/chanlun/decision_support/trading_system/direct_recursive_structure.py"
        in paths
    )
    assert not any("human_review" in value for value in paths)
    assert not any("forward_review" in value for value in paths)

    first = recent_year_research_algorithm_hashes()
    second = recent_year_research_algorithm_hashes()
    assert first == second
    assert first == tuple(sorted(first))
    assert all(value.startswith("sha256:") for _path, value in first)
    assert recent_year_research_algorithm_revision(first).startswith("sha256:")
