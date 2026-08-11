from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import sqlite3
from zoneinfo import ZoneInfo

import pandas as pd

import pytest

from chanlun.decision_support.trading_system.timeframe_alignment import (
    AlignedEntryChain,
    AlignmentDecision,
)

from tools.prescreen_cached_symbols import (
    _apply_qmt_dr_adjustments,
    _candidate_alignment_decision_documents,
    _events_in_interval,
    _implementation_sha256,
    _prior_artifact_matches_inputs,
    discover_minute_symbols,
    provider_to_project_code,
)


CN = ZoneInfo("Asia/Shanghai")


@pytest.mark.parametrize(
    ("provider", "project"),
    (
        ("510300.SH", "SH.510300"),
        ("159915.SZ", "SZ.159915"),
        ("430047.BJ", "BJ.430047"),
    ),
)
def test_provider_to_project_code(provider: str, project: str) -> None:
    assert provider_to_project_code(provider) == project


def test_provider_to_project_code_rejects_unknown_identity() -> None:
    with pytest.raises(ValueError, match="unsupported cached A-share symbol"):
        provider_to_project_code("000300.CSI")


def test_discover_minute_symbols_is_read_only_and_filters_period(tmp_path) -> None:
    database = tmp_path / "bars.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE bars (
                symbol TEXT, period TEXT, adj_type TEXT, bar_time TEXT
            )
            """
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?)",
            (
                ("510300.SH", "P_Min1", "S_Unsplit", "2020-01-02 09:30:00"),
                ("510300.SH", "P_Min1", "S_Unsplit", "2020-01-02 09:31:00"),
                ("510050.SH", "P_Min1", "S_Unsplit", "2020-01-02 09:30:00"),
                ("000300.CSI", "P_Day1", "S_Unsplit", "2020-01-02 00:00:00"),
            ),
        )
    rows = discover_minute_symbols(database)
    assert [item["provider_symbol"] for item in rows] == [
        "510050.SH",
        "510300.SH",
    ]
    assert rows[1]["project_code"] == "SH.510300"
    assert rows[1]["rows"] == 2


def test_qmt_dr_adjustment_is_effective_date_causal() -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2020-01-02 10:00:00", "2020-01-03 10:00:00"]
            ),
            "open": [10.0, 10.0],
            "high": [10.0, 10.0],
            "low": [10.0, 10.0],
            "close": [10.0, 10.0],
        }
    )
    adjusted = _apply_qmt_dr_adjustments(
        frame,
        (
            {
                "effective_on": pd.Timestamp("2020-01-03").date(),
                "raw_price_divisor": "1.1",
            },
        ),
    )
    assert adjusted["close"].tolist() == pytest.approx([10.0, 11.0])


def test_qmt_events_before_and_after_source_interval_are_not_reapplied() -> None:
    events = tuple(
        {"effective_on": pd.Timestamp(value).date(), "raw_price_divisor": "1.1"}
        for value in ("2019-12-31", "2020-01-03", "2020-01-06")
    )
    selected = _events_in_interval(
        events,
        start=pd.Timestamp("2020-01-02").date(),
        end=pd.Timestamp("2020-01-03").date(),
    )
    assert [item["effective_on"].isoformat() for item in selected] == [
        "2020-01-03"
    ]


def test_candidate_alignment_decisions_are_complete_sorted_and_time_traceable() -> None:
    early = datetime(2020, 1, 2, 10, 0, tzinfo=CN)
    late = datetime(2020, 1, 3, 10, 0, tzinfo=CN)
    chain = AlignedEntryChain(
        l0_point_id="late",
        l0_center_id="center",
        l1_departure_evidence_id="departure",
        l1_return_evidence_id="return",
        l1_evidence_kind="COMPLETED_TREND",
        l2_locator_point_id="locator",
        decision_at=late,
        return_low=Decimal("3.50"),
        l0_zg=Decimal("3.40"),
        l2_confirmation_bar_high=Decimal("3.60"),
        structural_invalidation_price=Decimal("3.40"),
    )
    decisions = (
        AlignmentDecision(
            l0_point_id="late",
            window_start=late,
            window_end=late,
            status="PASS",
            reason_codes=(),
            chain=chain,
        ),
        AlignmentDecision(
            l0_point_id="early",
            window_start=early,
            window_end=early,
            status="REJECT",
            reason_codes=("NO_L2_LOCATOR_AT_FIRST_L1_RETURN_TERMINAL",),
        ),
    )

    documents = _candidate_alignment_decision_documents(
        decisions,
        l0_available_at={"late": late, "early": early},
    )

    assert [item["l0_point_id"] for item in documents] == ["early", "late"]
    assert documents[0]["l0_available_at"] == early
    assert documents[0]["alignment_decision_at"] == early
    assert documents[0]["chain"] is None
    assert documents[1]["alignment_decision_at"] == late
    assert documents[1]["chain"]["l2_locator_point_id"] == "locator"


def test_candidate_alignment_decisions_reject_incomplete_candidate_coverage() -> None:
    available_at = datetime(2020, 1, 2, 10, 0, tzinfo=CN)
    decision = AlignmentDecision(
        l0_point_id="only",
        window_start=available_at,
        window_end=available_at,
        status="REJECT",
        reason_codes=("NO_SUBSEQUENT_COMPLETED_L1_DOWN_RETURN",),
    )
    with pytest.raises(RuntimeError, match="cover every L0 candidate"):
        _candidate_alignment_decision_documents(
            (decision,),
            l0_available_at={"only": available_at, "missing": available_at},
        )


def test_prior_artifact_cache_identity_includes_benchmark_symbol() -> None:
    candidate = {
        "market_database_sha256": "market",
        "pit_database_sha256": "pit",
        "implementation_sha256": "implementation",
        "corporate_action_snapshot_sha256": "actions",
        "benchmark_symbol": "000300.CSI",
    }
    inputs = {
        "market_hash": "market",
        "pit_hash": "pit",
        "implementation_hash": "implementation",
        "corporate_action_hash": "actions",
    }
    assert _prior_artifact_matches_inputs(
        candidate, benchmark_symbol="000300.CSI", **inputs
    )
    assert not _prior_artifact_matches_inputs(
        candidate, benchmark_symbol="000300.SH", **inputs
    )
    candidate.pop("benchmark_symbol")
    assert not _prior_artifact_matches_inputs(
        candidate, benchmark_symbol="000300.CSI", **inputs
    )


def test_implementation_hash_includes_dependency_contents(tmp_path) -> None:
    dependency = tmp_path / "dependency.py"
    dependency.write_text("first\n", encoding="utf-8")
    first = _implementation_sha256((dependency,))
    dependency.write_text("second\n", encoding="utf-8")
    second = _implementation_sha256((dependency,))
    assert first != second
