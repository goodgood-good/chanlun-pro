from __future__ import annotations

from datetime import date, datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


PRESCREEN = _load(
    "prescreen_direct_recursive_reporting_test",
    "tools/prescreen_direct_recursive.py",
)
BACKTEST = _load(
    "backtest_direct_recursive_reporting_test",
    "tools/backtest_direct_recursive.py",
)
DATA_AUDIT = _load(
    "audit_direct_recursive_data_reporting_test",
    "tools/audit_direct_recursive_data.py",
)


def test_missing_adjustment_ledger_keeps_structure_diagnostic_only() -> None:
    result = PRESCREEN._formal_structure_counts(
        formal_chain_eligibility=False,
        diagnostic_strategic_points=4,
        diagnostic_aligned_entries=2,
        diagnostic_replay_eligible_signals=3,
    )

    assert result == {
        "formal_signal_eligible": False,
        "formal_signal_gate_reason": "MISSING_PIT_CAUSAL_ADJUSTMENT_LEDGER",
        "diagnostic_strategic_point_count": 4,
        "strategic_point_count": 0,
        "diagnostic_aligned_entry_count": 2,
        "aligned_entry_count": 0,
        "diagnostic_replay_eligible_structure_signal_count": 3,
        "replay_eligible_structure_signal_count": 0,
    }


def test_causal_adjustment_promotes_only_the_same_observed_counts() -> None:
    result = PRESCREEN._formal_structure_counts(
        formal_chain_eligibility=True,
        diagnostic_strategic_points=2,
        diagnostic_aligned_entries=1,
        diagnostic_replay_eligible_signals=5,
    )

    assert result["strategic_point_count"] == 2
    assert result["aligned_entry_count"] == 1
    assert result["replay_eligible_structure_signal_count"] == 5
    with pytest.raises(ValueError, match="cannot be negative"):
        PRESCREEN._formal_structure_counts(
            formal_chain_eligibility=True,
            diagnostic_strategic_points=-1,
            diagnostic_aligned_entries=0,
            diagnostic_replay_eligible_signals=0,
        )


def test_strategic_points_receive_same_base_higher_timeframe_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cn = ZoneInfo("Asia/Shanghai")
    decision = datetime(2026, 7, 24, 14, 30, tzinfo=cn)
    frame = pd.DataFrame(
        {"date": [datetime(2026, 7, 23, 15, tzinfo=cn), decision]}
    )
    base = SimpleNamespace(
        daily=pd.DataFrame(),
        thirty_minute=pd.DataFrame(),
        complete_sessions=(date(2026, 7, 23), date(2026, 7, 24)),
        source_base_stream_revision="sha256:" + "a" * 64,
        grade="FULL_SYSTEM_ELIGIBLE",
    )
    seen: list[datetime] = []
    monkeypatch.setattr(
        PRESCREEN,
        "build_qmt_same_base_stream_frames",
        lambda **_kwargs: base,
    )

    def inputs(**kwargs):
        seen.append(kwargs["decision_time"])
        return SimpleNamespace()

    monkeypatch.setattr(PRESCREEN, "qmt_higher_timeframe_inputs", inputs)
    monkeypatch.setattr(
        PRESCREEN,
        "build_qmt_higher_timeframe_risk",
        lambda **_kwargs: SimpleNamespace(
            grade="FULL_SYSTEM_ELIGIBLE",
            warmup=SimpleNamespace(
                document=lambda: {
                    "contract_id": "chanlun-qmt-mwd-warmup-evidence",
                    "required_daily_bars": 480,
                    "full_daily_bar_count": 480,
                    "suffix_daily_bar_count": 320,
                    "converged": True,
                }
            ),
            risk=SimpleNamespace(
                gate="GREEN",
                snapshot=SimpleNamespace(
                    monthly="NONE",
                    weekly="NONE",
                    daily="NONE",
                ),
            ),
            blockers=(),
        ),
    )

    documents = PRESCREEN._higher_timeframe_risk_documents(
        code="SH.510300",
        one_minute_frame=frame,
        strategic_points=(
            SimpleNamespace(point_id="point:l0:3buy", available_at=decision),
        ),
    )

    assert seen == [decision]
    assert documents[0]["risk_gate"] == "GREEN"
    assert documents[0]["same_base_stream_revision"] == "sha256:" + "a" * 64
    assert documents[0]["signal_authority"] == "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH"
    assert documents[0]["warmup_evidence"]["converged"] is True


def test_invalid_higher_timeframe_input_rejects_candidate_without_deleting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cn = ZoneInfo("Asia/Shanghai")
    decision = datetime(2026, 7, 24, 14, 30, tzinfo=cn)
    frame = pd.DataFrame({"date": [decision]})

    def invalid(**_kwargs):
        raise ValueError("QMT 1m frame contains invalid OHLCV")

    monkeypatch.setattr(PRESCREEN, "build_qmt_same_base_stream_frames", invalid)
    documents = PRESCREEN._higher_timeframe_risk_documents(
        code="SH.510300",
        one_minute_frame=frame,
        strategic_points=(
            SimpleNamespace(point_id="point:l0:3buy", available_at=decision),
        ),
    )

    assert len(documents) == 1
    assert documents[0]["risk_gate"] == "UNRESOLVED"
    assert documents[0]["blocker_codes"] == (
        "HIGHER_TIMEFRAME_SAME_BASE_INPUT_INVALID",
    )
    assert "invalid OHLCV" in documents[0]["blocker_detail"]


def _prescreen(*, aligned: int = 0) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "chanlun-direct-recursive-etf-prescreen",
        "signal_authority": "DIRECT_RECURSIVE_ONE_MINUTE_GRAPH",
        "live_status": "LIVE_DISABLED",
        "logical_level_mapping": {
            "L0_30M_STRATEGIC": "1m graph raw recursive level 2",
            "L1_5M_TACTICAL": "1m graph raw recursive level 1",
            "L2_1M_LOCATOR": "1m graph raw recursive level 0",
        },
        "totals": {
            "instruments": 2,
            "adjustment_eligible_instruments": 1,
            "diagnostic_strategic_points": 6,
            "strategic_points": 2,
            "aligned_entries": aligned,
            "alignment_rejection_counts": {
                "NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN": 2,
            },
            "data_gate_rejection_counts": {
                "MISSING_PIT_CAUSAL_ADJUSTMENT_LEDGER": 1,
            },
        },
        "instrument_reports": [
            {
                "provider_symbol": "510300.SH",
                "source_start": "2020-01-02",
                "source_end": "2022-12-30",
                "source_sessions": 730,
                "rows_1m": 175200,
                "structure_levels": [{}, {}, {}],
            },
            {
                "provider_symbol": "510310.SH",
                "source_start": "2021-01-04",
                "source_end": "2022-12-30",
                "source_sessions": 480,
                "rows_1m": 115200,
                "structure_levels": [{}, {}],
            },
        ],
    }
    value["content_sha256"] = BACKTEST.content_sha256(value)
    return value


def test_empty_direct_replay_reports_formal_and_diagnostic_supply_separately() -> None:
    report = BACKTEST.build_backtest(
        _prescreen(),
        prescreen_file_sha256="sha256:" + "a" * 64,
    )

    assert report["diagnostic_strategic_candidate_count"] == 6
    assert report["strategic_candidate_count"] == 2
    assert report["adjustment_eligible_instrument_count"] == 1
    assert report["performance_evaluable"] is False
    assert report["replay"]["metrics"]["empty_replay"] is True
    assert report["return_claim_allowed"] is False


def test_nonempty_prescreen_fails_without_signed_execution_facts() -> None:
    with pytest.raises(
        RuntimeError,
        match="signed selection/risk/account/execution facts",
    ):
        BACKTEST.build_backtest(
            _prescreen(aligned=1),
            prescreen_file_sha256="sha256:" + "b" * 64,
        )


def test_data_audit_separates_component_structure_from_full_pnl_gate() -> None:
    prescreen = _prescreen()
    backtest = BACKTEST.build_backtest(
        prescreen,
        prescreen_file_sha256="sha256:" + "a" * 64,
    )

    audit = DATA_AUDIT.build_audit(
        prescreen,
        backtest,
        prescreen_file_sha256="sha256:" + "a" * 64,
        backtest_file_sha256="sha256:" + "b" * 64,
    )

    assert audit["technical_component_grade"] == "COMPONENT_ONLY"
    assert audit["full_system_data_gate"]["eligibility"] == "RESEARCH_ONLY"
    assert str(
        audit["coverage"]["point_in_time_adjustment_instrument_coverage"]
    ) == "0.5"
    assert audit["selection_paths"]["ETF_PROXY"]["formal_strategic_candidates"] == 2
    assert audit["selection_paths"]["INDIVIDUAL_THREE_PROGRAM"]["status"] == "UNRESOLVED"
    assert audit["return_evaluation"]["performance_evaluable"] is False
    assert audit["live_status"] == "LIVE_DISABLED"
