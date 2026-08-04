from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
import pickle
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tools import run_v3_forward_paper as subject
from chanlun.decision_support.trading_system.human_assisted_decision import (
    HumanAssistedDecisionCore,
    SIGNAL_DECISION_DOCUMENT_SCHEMA,
    signal_decision_document_id,
)
from chanlun.decision_support.trading_system.decision_source_provenance import (
    decision_source_snapshot_id,
    decision_source_snapshot_matches_current,
)
from chanlun.decision_support.trading_system.a_share_minute_grid import (
    a_share_completed_one_minute_closes,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    HumanPaperEntrySelectionEvidence,
    HumanPaperIntent,
    HumanPaperMinuteBar,
    audit_human_paper_capital_rejection_evidence,
    audit_human_paper_execution_evidence,
    audit_human_paper_execution_rejection_evidence,
    audit_human_paper_operations_cancellation_evidence,
    append_human_paper_intent,
    human_paper_terminal_intent_ids,
    load_human_paper_ledger,
    settle_human_paper_intents,
)
from chanlun.decision_support.trading_system.v3_qmt_sector_ledger import (
    append_sector_catalog,
    catalog_capture_entry,
)
from chanlun.decision_support.trading_system.sector_strength import (
    build_horizontal_sector_strength_batch,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    QmtCausalFactorEvent,
)
from chanlun.decision_support.trading_system.v3_selection import (
    SectorMemberHistory,
)
from chanlun.decision_support.trading_system.v3_multisymbol_replay import ReplayBatch
from chanlun.decision_support.trading_system.v3_forward_paper import (
    _selection_paper_ledger_prefix_archive_file,
    _selection_source_report_archive_files_proven,
)
from chanlun.decision_support.trading_system.v3_human_review_screening import (
    HumanReviewAlert,
    SectorRankingReviewEvidence,
    human_review_alert_document,
    human_review_screening_parameters,
)
from chanlun.decision_support.trading_system.v3_live_human_review import (
    COVERAGE_MANIFEST_SCHEMA,
    COVERAGE_STATE_CONTRACT_ID,
    MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID,
    SECTOR_COVERAGE_CONTRACT_ID,
    SIGNAL_DOCUMENT_CONTRACT_ID,
    screening_coverage_epoch_id,
)
from chanlun.decision_support.trading_system.v3_trading_session import (
    build_trading_session_evidence,
)
from chanlun.decision_support.trading_system.v3_technical_approximation import (
    technical_approximation_parameters,
)


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 28)
SECTOR_CATALOG_REVISION = "sha256:" + "b" * 64
PAPER_SECTOR_ID = "qmt-gics3:test"
PAPER_SECTOR_NAME = "测试板块"
PAPER_SECTOR_SOURCE_KEY = "GICS3测试板块"
PAPER_SECTOR_MEMBERS = ("SH.600000", "SH.600001")
PARAMETER_SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "audit"
    / "chanlun_trading_system_backtest"
    / "recent_year_current_sector_no3p"
    / "parameter_snapshot_human_review.json"
)


def _expected_sector_catalog(
    *,
    revision: str = SECTOR_CATALOG_REVISION,
    members: tuple[str, ...] = ("SH.600000",),
) -> dict[str, object]:
    return {
        "catalog_revision": revision,
        "sectors": (
            {
                "sector_id": "qmt-gics3:test",
                "member_codes": members,
            },
        ),
    }


def _ready_sector_capture() -> dict[str, object]:
    return {
        "ready": True,
        "reason_code": "READY",
        "catalog": {
            "ledger_entry_sha256": "sha256:" + "a" * 64,
            "catalog_revision": SECTOR_CATALOG_REVISION,
            "captured_at": "2026-07-28T09:10:00+08:00",
            "sectors": ("bank",),
        },
        "receipt_audit": {"status": "COMPLETE"},
    }


def _ready_implementation_continuity() -> dict[str, object]:
    return {
        "schema": "chanlun-v3-forward-implementation-continuity/v1",
        "ready": True,
        "status": "ready",
        "reason_code": "READY",
        "market_data_read_authorized": True,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }


def _paper_sector_catalog_source(session: date) -> dict[str, object]:
    sectors = (
        {
            "sector_id": PAPER_SECTOR_ID,
            "name": PAPER_SECTOR_NAME,
            "source_key": PAPER_SECTOR_SOURCE_KEY,
            "member_codes": PAPER_SECTOR_MEMBERS,
        },
    )
    revision = subject.sha256_json(
        {
            "schema": "chanlun-qmt-gics3-catalog/v1",
            "sectors": sectors,
        }
    )
    return {
        "source": "qmt_gics3_components",
        "captured_at": datetime.combine(session, time(9, 10), tzinfo=CN).isoformat(),
        "point_in_time_scope": "CURRENT_CAPTURE_ONLY",
        "catalog_revision": revision,
        "sectors": sectors,
    }


def _paper_sector_catalog_entries() -> tuple[dict[str, object], ...]:
    entries: list[dict[str, object]] = []
    previous: str | None = None
    for offset in range(3, -1, -1):
        entry = catalog_capture_entry(
            _paper_sector_catalog_source(SESSION - timedelta(days=offset)),
            previous_entry_sha256=previous,
        )
        entries.append(entry)
        previous = str(entry["entry_sha256"])
    return tuple(entries)


_PAPER_SOURCE_REPORTS: dict[str, dict[str, object]] = {}


def _paper_review_source(
    *,
    symbol: str,
    created_at: datetime,
    entry_confirmation_bar_closed_at: datetime,
    entry_price_cap: Decimal,
    entry_valid_until: datetime,
    entry_boundary_evidence_id: str,
) -> tuple[HumanReviewAlert, dict[str, object]]:
    entry = next(
        value
        for value in _paper_sector_catalog_entries()
        if datetime.fromisoformat(str(value["captured_at"])).date()
        == created_at.date()
    )
    ranking_at = datetime.combine(created_at.date(), time(9, 20), tzinfo=CN)
    ranking = SectorRankingReviewEvidence(
        source_profile="LIVE_FULL_RANKING",
        sector_id=PAPER_SECTOR_ID,
        sector_name=PAPER_SECTOR_NAME,
        observed_at=ranking_at,
        eligible=True,
        hard_block=False,
        regime="supportive",
        ordinal=1,
        rank_score=1,
        rank_components=(("structure", 1),),
        reason_codes=("TEST_LIVE_FULL_RANKING",),
        horizontal_strength=Decimal("1"),
        horizontal_rank=1,
        strength_observed_at=ranking_at,
        strength_anchor_session=created_at.date(),
        strength_member_count=len(PAPER_SECTOR_MEMBERS),
        strength_source_revision="sha256:" + "5" * 64,
        strength_evidence_revision="sha256:" + "6" * 64,
        sector_catalog_revision=str(entry["catalog_revision"]),
    )
    alert = HumanReviewAlert(
        symbol=symbol,
        alert_type="POSSIBLE_30M_BUY",
        signal_at=datetime.combine(created_at.date(), time(9, 15), tzinfo=CN),
        review_available_at=created_at,
        source_point_id=subject.sha256_json(
            {"schema": "test-source-point/v1", "symbol": symbol}
        ),
        structure_snapshot_id=subject.sha256_json(
            {
                "schema": "test-structure-snapshot/v1",
                "symbol": symbol,
                "session": created_at.date().isoformat(),
            }
        ),
        sector_id=PAPER_SECTOR_ID,
        confidence="HIGH",
        review_priority=90,
        reference_price=Decimal("10"),
        structural_invalidation_price=Decimal("9"),
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
        symbol_risk_gate="GREEN",
        warning_codes=(),
        source_fact_ids=(ranking.evidence_id,),
        screening_parameter_set_id=(
            human_review_screening_parameters().parameter_set_id
        ),
        technical_approximation_parameter_set_id=(
            technical_approximation_parameters().parameter_set_id
        ),
        sector_ranking_evidence=ranking,
        entry_confirmation_bar_closed_at=entry_confirmation_bar_closed_at,
        entry_price_cap=entry_price_cap,
        entry_valid_until=entry_valid_until,
        entry_boundary_evidence_id=entry_boundary_evidence_id,
    )
    row = {
        **subject._jsonable(human_review_alert_document(alert)),
        "candidate_id": alert.candidate_id,
        "signal_lifecycle_id": alert.signal_lifecycle_id,
    }
    stable = subject._jsonable({
        "schema": "chanlun-v3-human-review-screen/v1",
        "data_grade": "HUMAN_REVIEW_SCREENING",
        "highest_status": "REVIEW_REQUIRED",
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "portfolio_backtest_performed": False,
        "portfolio_performance_evaluable": False,
        "orders_created": 0,
        "fills_created": 0,
        "positions_created": 0,
        "review_queue": [row],
        "live_status": "LIVE_DISABLED",
    })
    assert isinstance(stable, dict)
    report = {**stable, "content_sha256": subject.sha256_json(stable)}
    _PAPER_SOURCE_REPORTS[str(report["content_sha256"])] = report
    return alert, report


def _paper_sector_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "sector_ledger.json"
    if not path.is_file():
        for offset in range(3, -1, -1):
            append_sector_catalog(
                path,
                _paper_sector_catalog_source(SESSION - timedelta(days=offset)),
            )
    archive = tmp_path / "live_screens" / "test"
    archive.mkdir(parents=True, exist_ok=True)
    for source_hash, report in _PAPER_SOURCE_REPORTS.items():
        (archive / f"{source_hash[7:]}.json").write_text(
            json.dumps(
                subject._jsonable(report),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    return path


def _paper_entry_selection_evidence(
    *,
    feedback_id: str,
    alert: HumanReviewAlert,
    report: dict[str, object],
    created_at: datetime,
) -> HumanPaperEntrySelectionEvidence:
    entry = next(
        value
        for value in _paper_sector_catalog_entries()
        if datetime.fromisoformat(str(value["captured_at"])).date()
        == created_at.date()
    )
    ranking = alert.sector_ranking_evidence
    assert ranking is not None
    return HumanPaperEntrySelectionEvidence(
        feedback_id=feedback_id,
        candidate_id=alert.candidate_id,
        source_screen_content_sha256=str(report["content_sha256"]),
        symbol=alert.symbol,
        sector_id=PAPER_SECTOR_ID,
        sector_name=PAPER_SECTOR_NAME,
        sector_ranking_evidence_id=ranking.evidence_id,
        sector_ranking_observed_at=ranking.observed_at,
        sector_catalog_revision=str(entry["catalog_revision"]),
        sector_catalog_entry_sha256=str(entry["entry_sha256"]),
        sector_catalog_captured_at=datetime.fromisoformat(
            str(entry["captured_at"])
        ),
        attested_at=created_at,
    )


def _pending_paper_intent() -> HumanPaperIntent:
    created = datetime.combine(SESSION, time(10), tzinfo=CN)
    feedback_id = "sha256:" + "1" * 64
    symbol = "SH.600000"
    entry_valid_until = datetime.combine(SESSION, time(15), tzinfo=CN)
    boundary_id = "sha256:" + "4" * 64
    alert, report = _paper_review_source(
        symbol=symbol,
        created_at=created,
        entry_confirmation_bar_closed_at=created,
        entry_price_cap=Decimal("20000"),
        entry_valid_until=entry_valid_until,
        entry_boundary_evidence_id=boundary_id,
    )
    return HumanPaperIntent(
        feedback_id=feedback_id,
        candidate_id=alert.candidate_id,
        source_screen_content_sha256=str(report["content_sha256"]),
        symbol=symbol,
        side="BUY",
        created_at=created,
        earliest_fill_at=created,
        quantity=100,
        reference_price=Decimal("10"),
        structural_invalidation_price=Decimal("9"),
        market_risk_gate="GREEN",
        sector_risk_gate="GREEN",
        symbol_risk_gate="GREEN",
        status="PENDING",
        reason_codes=("HUMAN_CONFIRMED_PAPER_OBSERVATION",),
        entry_confirmation_bar_closed_at=created,
        entry_price_cap=Decimal("20000"),
        entry_valid_until=entry_valid_until,
        entry_boundary_evidence_id=boundary_id,
        entry_selection_evidence=_paper_entry_selection_evidence(
            feedback_id=feedback_id,
            alert=alert,
            report=report,
            created_at=created,
        ),
    )


def _replace_paper_intent(
    intent: HumanPaperIntent,
    **changes: object,
) -> HumanPaperIntent:
    side = str(changes.get("side", intent.side))
    created_at = changes.get("created_at", intent.created_at)
    if not isinstance(created_at, datetime):
        raise TypeError("created_at must remain a datetime")
    if side == "BUY":
        confirmation = changes.get(
            "entry_confirmation_bar_closed_at",
            intent.entry_confirmation_bar_closed_at,
        )
        price_cap = changes.get("entry_price_cap", intent.entry_price_cap)
        valid_until = changes.get("entry_valid_until", intent.entry_valid_until)
        boundary_id = changes.get(
            "entry_boundary_evidence_id",
            intent.entry_boundary_evidence_id,
        )
        if (
            not isinstance(confirmation, datetime)
            or not isinstance(price_cap, Decimal)
            or not isinstance(valid_until, datetime)
            or not isinstance(boundary_id, str)
        ):
            raise TypeError("BUY test intent boundary must remain complete")
        alert, report = _paper_review_source(
            symbol=str(changes.get("symbol", intent.symbol)),
            created_at=created_at,
            entry_confirmation_bar_closed_at=confirmation,
            entry_price_cap=price_cap,
            entry_valid_until=valid_until,
            entry_boundary_evidence_id=boundary_id,
        )
        changes["candidate_id"] = alert.candidate_id
        changes["source_screen_content_sha256"] = str(
            report["content_sha256"]
        )
        if "entry_selection_evidence" not in changes:
            changes["entry_selection_evidence"] = (
                _paper_entry_selection_evidence(
                    feedback_id=str(
                        changes.get("feedback_id", intent.feedback_id)
                    ),
                    alert=alert,
                    report=report,
                    created_at=created_at,
                )
            )
    elif "entry_selection_evidence" not in changes:
        changes["entry_selection_evidence"] = None
    return replace(intent, **changes)


def _frame(frequency: str, *, complete: bool) -> pd.DataFrame:
    if frequency == "1m":
        rows = 241 if complete else 120
        values = [
            datetime.combine(SESSION, time(9, 30), tzinfo=CN)
            + timedelta(minutes=index)
            for index in range(rows - 1)
        ]
        values.append(datetime.combine(SESSION, time(15), tzinfo=CN))
    else:
        rows = 48 if complete else 20
        values = [
            datetime.combine(SESSION, time(9, 35), tzinfo=CN)
            + timedelta(minutes=5 * index)
            for index in range(rows - 1)
        ]
        values.append(datetime.combine(SESSION, time(15), tzinfo=CN))
    return pd.DataFrame({"date": values})


def _complete_execution_frame(
    *,
    target_closed_at: datetime,
    open_price: Decimal,
    high: Decimal,
    low: Decimal,
    close: Decimal,
) -> pd.DataFrame:
    """Full native QMT grid with only the target bar carrying liquidity."""

    times = (
        datetime.combine(SESSION, time(9, 30), tzinfo=CN),
        *pd.date_range(
            datetime.combine(SESSION, time(9, 31), tzinfo=CN),
            periods=120,
            freq="1min",
        ),
        *pd.date_range(
            datetime.combine(SESSION, time(13, 1), tzinfo=CN),
            periods=120,
            freq="1min",
        ),
    )
    volumes = [Decimal("0") for _ in times]
    # One fixed lot requires at least 2,000 shares of whole-bar volume under
    # the frozen five-percent conservative participation rule.
    volumes[times.index(target_closed_at)] = Decimal("2000")
    frame = pd.DataFrame(
        {
            "date": times,
            "open": [open_price for _ in times],
            "high": [high for _ in times],
            "low": [low for _ in times],
            "close": [close for _ in times],
            "volume": volumes,
        }
    )
    frame.attrs.update(
        {
            "structure_price_quantum": "0.01",
            "price_basis_provider": "qmt",
            "price_basis_adjustment": "causal-forward-ex-date-v1",
            "price_basis_revision": "sha256:" + "e" * 64,
        }
    )
    return frame


def _execution_fact(
    symbol: str,
    *,
    suspended: bool = False,
    expired: bool = False,
    is_st: bool = False,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "native_code": symbol,
        "session": SESSION.isoformat(),
        "trading_day": SESSION.isoformat(),
        "instrument_name": "*ST test" if is_st else "test",
        "instrument_status": 1 if suspended else 0,
        "is_trading": False,
        "suspended": suspended,
        "expired": expired,
        "expiry_date": (
            (SESSION - timedelta(days=1)).isoformat() if expired else None
        ),
        "is_st": is_st,
        "pre_close": "10",
        "limit_up": "11",
        "limit_down": "9",
        "price_tick": "0.01",
        "corporate_actions": [],
        "source_methods": (
            "QMT_GET_INSTRUMENT_DETAIL",
            "QMT_GET_DIVID_FACTORS",
        ),
        "tick_data_used": False,
        "account_api_used": False,
    }


def test_market_data_gate_requires_completed_1m_and_5m_close(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda _code, frequency, **_kwargs: _frame(frequency, complete=True),
    )
    ready = subject._market_data_gate(session=SESSION, qmt_data_dir=tmp_path)
    assert ready["complete"] is True
    assert ready["reason_codes"] == ()

    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda _code, frequency, **_kwargs: _frame(
            frequency,
            complete=frequency != "1m",
        ),
    )
    blocked = subject._market_data_gate(session=SESSION, qmt_data_dir=tmp_path)
    assert blocked["complete"] is False
    assert "1M_SESSION_ROWS_INCOMPLETE" in blocked["reason_codes"]
    assert blocked["symbol"] == "SH.600000"


def test_paper_execution_snapshot_is_same_session_hashed_and_read_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": "600000.SH",
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "浦发银行",
            "instrument_status": 0,
            "is_trading": True,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10",
            "limit_up": "11",
            "limit_down": "9",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    intent = _pending_paper_intent()
    pending = ({**subject._jsonable(intent), "intent_id": intent.intent_id},)

    document, facts = subject._human_paper_execution_snapshot(
        args=SimpleNamespace(root=tmp_path),
        session=SESSION,
        pending=pending,
        ledger_events=(),
    )

    assert document["all_complete"] is True
    assert document["tick_data_used"] is False
    assert document["account_api_used"] is False
    assert document["content_sha256"].startswith("sha256:")
    assert facts["SH.600000"]["buy_eligible"] is True
    written = json.loads(
        (
            tmp_path
            / "sessions"
            / SESSION.isoformat()
            / "paper_execution_facts.json"
        ).read_text(encoding="utf-8")
    )
    assert written == document
    immutable = (
        tmp_path
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_facts"
        / f"{document['content_sha256'][7:]}.json"
    )
    assert json.loads(immutable.read_text(encoding="utf-8")) == document


def test_execution_snapshot_company_action_window_resets_after_closed_cycle(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Only the oldest remaining FIFO lot defines the action window."""

    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    old_session = SESSION - timedelta(days=5)
    closed_session = SESSION - timedelta(days=4)
    action_session = SESSION - timedelta(days=3)
    reopened_session = SESSION - timedelta(days=1)
    captured_factor_starts: list[date] = []

    def execution_fact(**kwargs) -> dict[str, object]:
        captured_factor_starts.append(kwargs["factor_start"])
        fact = _execution_fact(str(kwargs["symbol"]))
        # This action belongs to the already closed first cycle.  Returning it
        # deliberately exercises the holding-window check even though the
        # production QMT reader also filters from factor_start.
        fact["corporate_actions"] = [
            {"effective_on": action_session.isoformat()}
        ]
        return fact

    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        execution_fact,
    )
    ledger_events = (
        {
            "kind": "FILL",
            "payload": {
                "symbol": "SH.600000",
                "side": "BUY",
                "quantity": 100,
                "filled_at": datetime.combine(
                    old_session, time(10), tzinfo=CN
                ).isoformat(),
            },
        },
        {
            "kind": "FILL",
            "payload": {
                "symbol": "SH.600000",
                "side": "SELL",
                "quantity": 100,
                "filled_at": datetime.combine(
                    closed_session, time(10), tzinfo=CN
                ).isoformat(),
            },
        },
        {
            "kind": "FILL",
            "payload": {
                "symbol": "SH.600000",
                "side": "BUY",
                "quantity": 100,
                "filled_at": datetime.combine(
                    reopened_session, time(10), tzinfo=CN
                ).isoformat(),
            },
        },
    )

    document, facts = subject._human_paper_execution_snapshot(
        args=SimpleNamespace(root=tmp_path),
        session=SESSION,
        pending=(),
        ledger_events=ledger_events,
    )

    assert captured_factor_starts == [reopened_session]
    row = facts["SH.600000"]
    assert row["virtual_position_quantity"] == 100
    assert row["oldest_virtual_acquired_session"] == (
        reopened_session.isoformat()
    )
    assert row["position_corporate_action_conflict"] is False
    assert row["corporate_action_state_complete"] is True
    assert document["errors"] == []
    assert document["all_complete"] is True


def test_paper_execution_snapshot_retry_preserves_every_referenced_object(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A later retry may move ``captured_at`` but must not erase old evidence."""

    captured = [
        datetime.combine(SESSION, time(15, 20), tzinfo=CN),
        datetime.combine(SESSION, time(15, 21), tzinfo=CN),
    ]
    monkeypatch.setattr(subject, "_now", lambda: captured.pop(0))
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": "600000.SH",
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "浦发银行",
            "instrument_status": 0,
            "is_trading": False,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10",
            "limit_up": "11",
            "limit_down": "9",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    intent = _pending_paper_intent()
    pending = ({**subject._jsonable(intent), "intent_id": intent.intent_id},)
    args = SimpleNamespace(root=tmp_path)

    first, _ = subject._human_paper_execution_snapshot(
        args=args,
        session=SESSION,
        pending=pending,
        ledger_events=(),
    )
    first_path = (
        tmp_path
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_facts"
        / f"{first['content_sha256'][7:]}.json"
    )
    first_bytes = first_path.read_bytes()

    second, _ = subject._human_paper_execution_snapshot(
        args=args,
        session=SESSION,
        pending=pending,
        ledger_events=(),
    )
    second_path = (
        tmp_path
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_facts"
        / f"{second['content_sha256'][7:]}.json"
    )

    assert first["content_sha256"] != second["content_sha256"]
    assert first_path != second_path
    assert first_path.read_bytes() == first_bytes
    assert json.loads(first_path.read_text(encoding="utf-8")) == first
    assert json.loads(second_path.read_text(encoding="utf-8")) == second
    latest = json.loads(
        (
            tmp_path
            / "sessions"
            / SESSION.isoformat()
            / "paper_execution_facts.json"
        ).read_text(encoding="utf-8")
    )
    assert latest == second


def test_qmt_after_close_is_trading_false_is_not_misread_as_suspension(
    monkeypatch,
) -> None:
    from xtquant import xtdata

    detail = {
        "TradingDay": "20260728",
        "InstrumentName": "浦发银行",
        "InstrumentStatus": 0,
        # At the 15:20 evaluator this wall-clock field is normally false.
        "IsTrading": False,
        "PreClose": 10,
        "UpStopPrice": 11,
        "DownStopPrice": 9,
        "PriceTick": 0.01,
        "ExpireDate": "99999999",
    }
    monkeypatch.setattr(xtdata, "get_instrument_detail", lambda *_a, **_k: detail)
    monkeypatch.setattr(
        xtdata,
        "get_divid_factors",
        lambda *_a, **_k: pd.DataFrame(),
    )

    normal = subject._qmt_human_paper_execution_fact(
        symbol="SH.600000",
        session=SESSION,
        factor_start=SESSION,
    )
    assert normal["is_trading"] is False
    assert normal["suspended"] is False
    assert normal["expiry_date"] is None
    assert normal["expired"] is False

    detail["InstrumentStatus"] = 1
    suspended = subject._qmt_human_paper_execution_fact(
        symbol="SH.600000",
        session=SESSION,
        factor_start=SESSION,
    )
    assert suspended["suspended"] is True

    detail["InstrumentStatus"] = 0
    detail["ExpireDate"] = "20260727"
    expired = subject._qmt_human_paper_execution_fact(
        symbol="SH.600000",
        session=SESSION,
        factor_start=SESSION,
    )
    assert expired["expiry_date"] == "2026-07-27"
    assert expired["expired"] is True


def test_virtual_settlement_does_not_read_bars_when_execution_fact_failed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "paper.json"
    append_human_paper_intent(ledger, _pending_paper_intent())
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )
    snapshot = {
        "schema": "chanlun-human-paper-execution-facts/v1",
        "session": SESSION.isoformat(),
        "all_complete": False,
        "complete_symbol_count": 0,
        "requested_symbol_count": 1,
        "captured_at": datetime.combine(
            SESSION,
            time(15, 20),
            tzinfo=CN,
        ).isoformat(),
        "errors": [
            {
                "symbol": "SH.600000",
                "reason": "QMT_EXECUTION_FACT_CAPTURE_FAILED",
            }
        ],
        "symbols": [],
        "source": "QMT_READ_ONLY_INSTRUMENT_DETAIL_AND_DIVID_FACTORS",
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    snapshot["content_sha256"] = subject.sha256_json(snapshot)
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_facts",
        payload=snapshot,
    )
    monkeypatch.setattr(
        subject,
        "_human_paper_execution_snapshot",
        lambda **_kwargs: (snapshot, {}),
    )
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("unknown execution facts must not read or fill bars")
        ),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["status"] == "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED"
    assert result["new_virtual_fill_count"] == 0
    assert result["new_operations_cancellation_count"] == 1
    assert result["operations_cancellation_evidence"]["status"] == "COMPLETE"
    assert result["execution_fact_error_count"] == 1
    assert [event["kind"] for event in load_human_paper_ledger(ledger)["events"]] == [
        "INTENT",
        "OPERATIONS_CANCEL",
    ]


def test_virtual_settlement_never_jumps_over_missing_prior_session_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    ledger = tmp_path / "paper.json"
    original = _pending_paper_intent()
    prior = datetime.combine(SESSION - timedelta(days=2), time(10), tzinfo=CN)
    intent = _replace_paper_intent(
        original,
        created_at=prior,
        earliest_fill_at=prior,
    )
    append_human_paper_intent(ledger, intent)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_trading_sessions",
        lambda **_kwargs: (SESSION - timedelta(days=1),),
    )
    monkeypatch.setattr(
        subject,
        "_human_paper_execution_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("causal-gap intent must not reach current execution facts")
        ),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["status"] == "VIRTUAL_SETTLEMENT_BLOCKED_BY_CAUSAL_GAP"
    assert result["causally_eligible_intent_count"] == 0
    assert result["causal_gap_blocked_intent_count"] == 1
    assert result["pending_continuity"]["status"] == "CAUSAL_GAPS"
    assert result["pending_continuity"]["gaps"] == [
        {
            "intent_id": intent.intent_id,
            "symbol": intent.symbol,
            "session": (SESSION - timedelta(days=1)).isoformat(),
            "reason": "FULL_SESSION_EXECUTION_EVIDENCE_MISSING",
        }
    ]
    assert [
        event["kind"] for event in load_human_paper_ledger(ledger)["events"]
    ] == ["INTENT"]


@pytest.mark.parametrize(
    "failure_mode",
    ("MISSING_ATTESTATION", "MISSING_CATALOG", "MISSING_SOURCE_REPORT"),
)
def test_virtual_buy_selection_provenance_blocks_before_market_data(
    monkeypatch,
    tmp_path: Path,
    failure_mode: str,
) -> None:
    """An unproved QMT sector admission never reaches the 1m fill path."""

    ledger = (
        tmp_path / "isolated" / "paper.json"
        if failure_mode == "MISSING_SOURCE_REPORT"
        else tmp_path / "paper.json"
    )
    intent = _pending_paper_intent()
    sector_ledger = _paper_sector_ledger(tmp_path)
    if failure_mode == "MISSING_ATTESTATION":
        intent = replace(intent, entry_selection_evidence=None)
    elif failure_mode == "MISSING_CATALOG":
        sector_ledger = tmp_path / "missing-sector-ledger.json"
    append_human_paper_intent(ledger, intent)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=sector_ledger,
    )
    monkeypatch.setattr(
        subject,
        "_human_paper_execution_snapshot",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("blocked BUY must not request execution facts or bars")
        ),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["status"] == (
        "VIRTUAL_SETTLEMENT_BLOCKED_BY_ENTRY_SELECTION_EVIDENCE"
    )
    assert result["entry_selection_blocked_buy_intent_ids"] == [
        intent.intent_id
    ]
    assert result["entry_selection_blocked_buy_remains_pending"] is True
    gate = result["entry_selection_settlement_gate"]
    assert gate["status"] == "BLOCKED"
    assert gate["verified_pending_buy_intent_count"] == 0
    assert gate["blocked_pending_buy_intent_count"] == 1
    assert gate["sector_catalog_ledger_status"] == (
        "MISSING" if failure_mode == "MISSING_CATALOG" else "VALID"
    )
    assert gate["source_binding_audit"]["status"] == {
        "MISSING_ATTESTATION": "LEGACY_UNATTESTED_LIVE_RANKED_BUY",
        "MISSING_CATALOG": "COMPLETE",
        "MISSING_SOURCE_REPORT": "INCOMPLETE_SOURCE_ARCHIVE",
    }[failure_mode]
    assert [
        event["kind"] for event in load_human_paper_ledger(ledger)["events"]
    ] == ["INTENT"]
    if failure_mode == "MISSING_CATALOG":
        for offset in range(3, -1, -1):
            append_sector_catalog(
                sector_ledger,
                _paper_sector_catalog_source(
                    SESSION - timedelta(days=offset)
                ),
            )
        current = load_human_paper_ledger(ledger)
        repaired = subject._human_paper_entry_selection_settlement_gate(
            args=args,
            events=tuple(current["events"]),
            pending=(current["events"][0]["payload"],),
        )
        assert repaired["status"] == "READY"
        assert repaired["verified_pending_buy_intent_ids"] == [
            intent.intent_id
        ]


def test_virtual_fill_hash_resolves_exact_fact_and_one_minute_bar_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A fill identity must resolve the exact completed bar used to price it."""

    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": "600000.SH",
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "浦发银行",
            "instrument_status": 0,
            "is_trading": False,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10",
            "limit_up": "11",
            "limit_down": "9",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    closed_at = datetime.combine(SESSION, time(10, 2), tzinfo=CN)
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: _complete_execution_frame(
            target_closed_at=closed_at,
            open_price=Decimal("10.10"),
            high=Decimal("10.20"),
            low=Decimal("10.00"),
            close=Decimal("10.15"),
        ),
    )
    ledger = tmp_path / "paper.json"
    intent = _pending_paper_intent()
    append_human_paper_intent(ledger, intent)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["new_virtual_fill_count"] == 1
    assert result["entry_selection_settlement_gate"]["status"] == "READY"
    source_archive = result["entry_selection_settlement_gate"][
        "source_report_archive"
    ]
    assert source_archive["status"] == "COMPLETE"
    assert source_archive["archive_performed"] is True
    assert source_archive["all_required_source_reports_archived"] is True
    assert source_archive["archived_source_report_count"] == 1
    source_receipt = source_archive["objects"][0]
    source_object = (
        args.root / "sessions" / SESSION.isoformat() / source_receipt["path"]
    )
    assert source_object.is_file()
    source_report = json.loads(source_object.read_text(encoding="utf-8"))
    assert source_report["content_sha256"] == source_receipt[
        "source_content_sha256"
    ]
    assert subject.sha256_file(source_object) == source_receipt["file_sha256"]
    assert source_receipt["candidate_ids"] == [intent.candidate_id]
    session_root = args.root / "sessions" / SESSION.isoformat()
    ledger_archive = result["entry_selection_settlement_gate"][
        "paper_ledger_prefix_archive"
    ]
    assert ledger_archive["status"] == "COMPLETE"
    assert ledger_archive["archive_performed"] is True
    assert ledger_archive["paper_ledger_content_sha256"] == result[
        "content_sha256"
    ]
    archived_ledger = _selection_paper_ledger_prefix_archive_file(
        ledger_archive,
        session_root=session_root,
    )
    assert archived_ledger is not None
    assert archived_ledger["content_sha256"] == result["content_sha256"]
    assert ledger_archive["event_count"] == len(archived_ledger["events"])
    assert ledger_archive["last_event_id"] == archived_ledger["events"][-1][
        "event_id"
    ]
    assert _selection_source_report_archive_files_proven(
        source_archive,
        session_root=session_root,
        paper_ledger_events=archived_ledger["events"],
    )
    forged_ledger_archive = dict(ledger_archive)
    forged_ledger_archive["event_count"] = ledger_archive["event_count"] + 1
    assert (
        _selection_paper_ledger_prefix_archive_file(
            forged_ledger_archive,
            session_root=session_root,
        )
        is None
    )
    assert not _selection_source_report_archive_files_proven(
        source_archive,
        session_root=tmp_path / "source-object-not-present",
    )
    assert result["entry_selection_blocked_buy_intent_count"] == 0
    assert result["cash_and_slot_pretrade_enforced"] is True
    assert result["capital_evaluation_count"] == 1
    assert result["capital_rejection_count"] == 0
    assert result["capital_evaluations"][0]["result"] == "FILL_ALLOWED"
    assert result["slot_fraction_notional_gate_evaluable"] is True
    assert result["account_exposure_notional_gate_evaluable"] is True
    assert result["synchronous_open_position_one_minute_marks_required"] is True
    assert result["portfolio_fill_decision_audit"]["status"] == "COMPLETE"
    assert result["portfolio_fill_decision_audit"]["approved_fill_count"] == 1
    assert result[
        "portfolio_approved_fill_ledger_prefix_recomputed"
    ] is True
    assert result["terminal_signal_lifecycle_one_shot_enforced"] is True
    paper = load_human_paper_ledger(ledger)
    fill = next(
        event["payload"]
        for event in paper["events"]
        if event["kind"] == "FILL"
    )
    evidence_path = (
        args.root
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_evidence"
        / f"{fill['execution_snapshot_sha256'][7:]}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["content_sha256"] == fill["execution_snapshot_sha256"]
    assert evidence["execution_fact_snapshot_sha256"] == result[
        "execution_fact_snapshot_sha256"
    ]
    bar = next(
        value
        for value in evidence["bars_by_symbol"]["SH.600000"]
        if value["closed_at"] == closed_at.isoformat()
    )
    assert bar == {
        "buy_eligible": True,
        "close": "10.15",
        "closed_at": closed_at.isoformat(),
        "complete": True,
        "corporate_action_state_complete": True,
        "high": "10.2",
        "limit_down_locked": False,
        "limit_up_locked": False,
        "low": "10.0",
        "open": "10.1",
        "opened_at": (closed_at - timedelta(minutes=1)).isoformat(),
        "security_status_complete": True,
        "sell_eligible": True,
        "suspended": False,
        "symbol": "SH.600000",
        "volume": "2000.0",
    }
    assert fill["price"] == bar["high"]
    assert fill["filled_at"] == bar["closed_at"]
    assert fill["source_bar_closed_at"] == bar["closed_at"]
    assert fill["portfolio_decision_sha256"] == result[
        "capital_evaluations"
    ][0]["content_sha256"]
    assert fill["position_marks"] == []
    audit = audit_human_paper_execution_evidence(
        paper["events"],
        forward_root=args.root,
    )
    assert audit["status"] == "COMPLETE"
    assert audit["fill_count"] == audit["verified_fill_count"] == 1

    open_price_events = json.loads(json.dumps(paper["events"]))
    open_price_fill = next(
        event["payload"]
        for event in open_price_events
        if event["kind"] == "FILL"
    )
    open_price_fill["price"] = bar["open"]
    open_price_audit = audit_human_paper_execution_evidence(
        open_price_events,
        forward_root=args.root,
    )
    assert open_price_audit["status"] == "INVALID"
    assert "exact execution bar facts disagree" in (
        open_price_audit["invalid_evidence"][0]["reason"]
    )

    backdated_events = json.loads(json.dumps(paper["events"]))
    backdated_fill = next(
        event["payload"]
        for event in backdated_events
        if event["kind"] == "FILL"
    )
    backdated_fill["filled_at"] = bar["opened_at"]
    backdated_audit = audit_human_paper_execution_evidence(
        backdated_events,
        forward_root=args.root,
    )
    assert backdated_audit["status"] == "INVALID"
    assert "backdated before its source bar completed" in (
        backdated_audit["invalid_evidence"][0]["reason"]
    )

    touch_only_events = json.loads(json.dumps(paper["events"]))
    touch_only_intent = next(
        event["payload"]
        for event in touch_only_events
        if event["kind"] == "INTENT"
    )
    # The saved bar reaches 10.20. Reframing that value as the limit still
    # leaves the old open-price check apparently in-cap, but strict OHLC
    # evidence cannot assign any crossed volume to an exact/mixed bar.
    touch_only_intent["entry_price_cap"] = "10.20"
    touch_only_audit = audit_human_paper_execution_evidence(
        touch_only_events,
        forward_root=args.root,
    )
    assert touch_only_audit["status"] == "INVALID"
    assert "first eligible in-cap TTL 1m bar" in (
        touch_only_audit["invalid_evidence"][0]["reason"]
    )

    original_facts = json.loads(
        Path(result["execution_fact_object"]).read_text(encoding="utf-8")
    )

    def audit_with_forged_facts(
        mutate,
    ) -> dict[str, object]:
        forged_facts = json.loads(json.dumps(original_facts))
        forged_facts.pop("content_sha256")
        mutate(forged_facts)
        forged_facts["content_sha256"] = subject.sha256_json(forged_facts)
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_facts",
            payload=forged_facts,
        )
        forged_evidence = json.loads(json.dumps(evidence))
        forged_evidence.pop("content_sha256")
        forged_evidence["execution_fact_snapshot_sha256"] = forged_facts[
            "content_sha256"
        ]
        forged_evidence["content_sha256"] = subject.sha256_json(
            forged_evidence
        )
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_evidence",
            payload=forged_evidence,
        )
        forged_events = json.loads(json.dumps(paper["events"]))
        forged_event_fill = next(
            event["payload"]
            for event in forged_events
            if event["kind"] == "FILL"
        )
        forged_event_fill["execution_snapshot_sha256"] = forged_evidence[
            "content_sha256"
        ]
        return audit_human_paper_execution_evidence(
            forged_events,
            forward_root=args.root,
        )

    forged_position_audit = audit_with_forged_facts(
        lambda facts: facts["symbols"][0].__setitem__(
            "virtual_position_quantity", 100
        )
    )
    assert forged_position_audit["status"] == "INVALID"
    assert "virtual position provenance changed" in (
        forged_position_audit["invalid_evidence"][0]["reason"]
    )

    forged_status_audit = audit_with_forged_facts(
        lambda facts: facts["symbols"][0].__setitem__("instrument_status", 1)
    )
    assert forged_status_audit["status"] == "INVALID"
    assert "raw security status cannot be recomputed" in (
        forged_status_audit["invalid_evidence"][0]["reason"]
    )

    # A new content hash does not make a fact captured at a different time
    # part of the same evidence snapshot.
    forged_capture_audit = audit_with_forged_facts(
        lambda facts: facts.__setitem__(
            "captured_at",
            (captured - timedelta(minutes=1)).isoformat(),
        )
    )
    assert forged_capture_audit["status"] == "INVALID"
    assert "document envelope changed" in (
        forged_capture_audit["invalid_evidence"][0]["reason"]
    )

    forged_coverage_audit = audit_with_forged_facts(
        lambda facts: facts.__setitem__("requested_symbol_count", 2)
    )
    assert forged_coverage_audit["status"] == "INVALID"
    assert "aggregate coverage changed" in (
        forged_coverage_audit["invalid_evidence"][0]["reason"]
    )

    forged_lock_evidence = json.loads(json.dumps(evidence))
    forged_lock_evidence.pop("content_sha256")
    forged_locked_bar = next(
        value
        for value in forged_lock_evidence["bars_by_symbol"]["SH.600000"]
        if value["closed_at"] == closed_at.isoformat()
    )
    forged_locked_bar.update(
        {
            "open": "11",
            "high": "11",
            "low": "11",
            "close": "11",
            # Deliberately forged: QMT limit_up is also 11, so this one-price
            # bar must be locked and cannot be a fill opportunity.
            "limit_up_locked": False,
        }
    )
    forged_lock_evidence["content_sha256"] = subject.sha256_json(
        forged_lock_evidence
    )
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_evidence",
        payload=forged_lock_evidence,
    )
    forged_lock_events = json.loads(json.dumps(paper["events"]))
    forged_lock_fill = next(
        event["payload"]
        for event in forged_lock_events
        if event["kind"] == "FILL"
    )
    forged_lock_fill["price"] = "11"
    forged_lock_fill["execution_snapshot_sha256"] = forged_lock_evidence[
        "content_sha256"
    ]
    forged_lock_audit = audit_human_paper_execution_evidence(
        forged_lock_events,
        forward_root=args.root,
    )
    assert forged_lock_audit["status"] == "INVALID"
    assert "limit-lock state cannot be recomputed" in (
        forged_lock_audit["invalid_evidence"][0]["reason"]
    )

    forged_stable = json.loads(json.dumps(evidence))
    forged_stable.pop("content_sha256")
    malformed = dict(bar)
    malformed_opened = datetime.combine(SESSION, time(10, 3), tzinfo=CN)
    malformed["opened_at"] = malformed_opened.isoformat()
    malformed["closed_at"] = (
        malformed_opened + timedelta(seconds=30)
    ).isoformat()
    malformed_index = forged_stable["bars_by_symbol"]["SH.600000"].index(
        next(
            value
            for value in forged_stable["bars_by_symbol"]["SH.600000"]
            if value["closed_at"] == closed_at.isoformat()
        )
    )
    forged_stable["bars_by_symbol"]["SH.600000"][malformed_index] = malformed
    forged_id = subject.sha256_json(forged_stable)
    evidence_path.with_name(f"{forged_id[7:]}.json").write_text(
        json.dumps({**forged_stable, "content_sha256": forged_id}),
        encoding="utf-8",
    )
    forged_events = json.loads(json.dumps(paper["events"]))
    forged_fill = next(
        event["payload"]
        for event in forged_events
        if event["kind"] == "FILL"
    )
    forged_fill["execution_snapshot_sha256"] = forged_id
    malformed_audit = audit_human_paper_execution_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert malformed_audit["status"] == "INVALID"
    assert "exactly one minute" in malformed_audit["invalid_evidence"][0][
        "reason"
    ]

    evidence_path.unlink()
    missing = audit_human_paper_execution_evidence(
        paper["events"],
        forward_root=args.root,
    )
    assert missing["status"] == "MISSING"
    assert missing["verified_fill_count"] == 0
    assert missing["missing_evidence"][0]["fill_id"] == fill["fill_id"]
    with pytest.raises(
        RuntimeError,
        match="existing virtual fills have incomplete immutable",
    ):
        subject._settle_human_paper(args=args, session=SESSION)


def test_virtual_settlement_fails_closed_on_internal_one_minute_grid_gap(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": "600000.SH",
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "test",
            "instrument_status": 0,
            "is_trading": False,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10",
            "limit_up": "11",
            "limit_down": "9",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    target = datetime.combine(SESSION, time(10, 2), tzinfo=CN)
    complete = _complete_execution_frame(
        target_closed_at=target,
        open_price=Decimal("10.10"),
        high=Decimal("10.20"),
        low=Decimal("10.00"),
        close=Decimal("10.15"),
    )
    incomplete = complete[complete["date"] != pd.Timestamp(target)].copy()
    incomplete.attrs = dict(complete.attrs)
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: incomplete,
    )
    ledger = tmp_path / "paper.json"
    append_human_paper_intent(ledger, _pending_paper_intent())
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["status"] == "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_1M_GRID"
    assert result["new_virtual_fill_count"] == 0
    assert result["execution_bar_grid_error_count"] == 1
    assert result["full_session_one_minute_grid_proven"] is False
    assert result["execution_bar_grid_audits"][0]["status"] == (
        "INCOMPLETE_FAIL_CLOSED"
    )
    assert result["new_operations_cancellation_count"] == 1
    assert result["operations_cancellation_evidence"]["status"] == "COMPLETE"
    assert [
        event["kind"] for event in load_human_paper_ledger(ledger)["events"]
    ] == ["INTENT", "OPERATIONS_CANCEL"]


def test_one_symbol_grid_failure_cancels_buy_without_blocking_other_symbol(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: _execution_fact(str(kwargs["symbol"])),
    )
    target = datetime.combine(SESSION, time(10, 2), tzinfo=CN)

    def minute_frame(symbol: str, *_args, **_kwargs) -> pd.DataFrame:
        if symbol == "SH.600000":
            raise RuntimeError("isolated QMT 1m read failure")
        return _complete_execution_frame(
            target_closed_at=target,
            open_price=Decimal("10.10"),
            high=Decimal("10.20"),
            low=Decimal("10.00"),
            close=Decimal("10.15"),
        )

    monkeypatch.setattr(subject, "load_qmt_frame", minute_frame)
    ledger = tmp_path / "paper.json"
    failed = _pending_paper_intent()
    eligible = _replace_paper_intent(
        failed,
        feedback_id="sha256:" + "7" * 64,
        symbol="SH.600001",
    )
    append_human_paper_intent(ledger, failed)
    append_human_paper_intent(ledger, eligible)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["status"] == "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_1M_GRID"
    assert result["new_virtual_fill_count"] == 1
    assert result["execution_bar_grid_error_count"] == 1
    assert result["execution_fact_complete_symbol_count"] == 2
    assert result["optional_buy_data_fault_cancelled"] is True
    assert result["persistent_exit_independent_symbol_continues"] is True
    assert result["new_operations_cancellation_count"] == 1
    assert result["unresolved_persistent_exit_intent_count"] == 0
    assert result["operations_cancellation_evidence"]["status"] == "COMPLETE"
    assert {
        value["symbol"]: value["status"]
        for value in result["execution_bar_grid_audits"]
    } == {
        "SH.600000": "INVALID_FAIL_CLOSED",
        "SH.600001": "COMPLETE",
    }
    document = load_human_paper_ledger(ledger)
    fills = [
        event["payload"]
        for event in document["events"]
        if event["kind"] == "FILL"
    ]
    assert [value["symbol"] for value in fills] == [eligible.symbol]
    assert [event["kind"] for event in document["events"]] == [
        "INTENT",
        "INTENT",
        "FILL",
        "OPERATIONS_CANCEL",
    ]

    # Even a fully re-hashed evidence object cannot turn the failed grid into
    # a valid cancellation proof.
    forged_evidence = json.loads(
        Path(result["execution_evidence_object"]).read_text(encoding="utf-8")
    )
    forged_evidence.pop("content_sha256")
    failed_audit = next(
        value
        for value in forged_evidence["bar_grid_audits"]
        if value["symbol"] == failed.symbol
    )
    failed_audit["status"] = "COMPLETE"
    forged_evidence["content_sha256"] = subject.sha256_json(forged_evidence)
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_evidence",
        payload=forged_evidence,
    )
    forged_events = json.loads(json.dumps(document["events"]))
    cancellation = forged_events[-1]["payload"]
    cancellation["execution_evidence_snapshot_sha256"] = forged_evidence[
        "content_sha256"
    ]
    cancellation_identity = dict(cancellation)
    cancellation_identity.pop("cancellation_id")
    cancellation_identity["cancelled_at"] = datetime.fromisoformat(
        str(cancellation_identity["cancelled_at"])
    )
    cancellation["cancellation_id"] = subject.HumanPaperOperationsCancellation(
        **cancellation_identity
    ).cancellation_id
    forged_audit = audit_human_paper_operations_cancellation_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert forged_audit["status"] == "INVALID"
    assert forged_audit["verified_cancellation_count"] == 0

    repeated = subject._settle_human_paper(args=args, session=SESSION)
    assert repeated["status"] == "NO_PENDING_VIRTUAL_INTENTS"
    assert repeated["new_operations_cancellation_count"] == 0
    assert repeated["total_operations_cancellation_count"] == 1
    assert repeated["operations_cancellation_evidence"]["status"] == "COMPLETE"
    assert load_human_paper_ledger(ledger) == document


def test_failed_optional_buy_does_not_block_other_symbol_persistent_exit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A data-fault BUY is cancelled while an independent exit stays live."""

    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: _execution_fact(str(kwargs["symbol"])),
    )
    target = datetime.combine(SESSION, time(10, 2), tzinfo=CN)

    def minute_frame(symbol: str, *_args, **_kwargs) -> pd.DataFrame:
        if symbol == "SH.600000":
            raise RuntimeError("optional BUY QMT 1m read failure")
        return _complete_execution_frame(
            target_closed_at=target,
            open_price=Decimal("10.10"),
            high=Decimal("10.20"),
            low=Decimal("10.00"),
            close=Decimal("10.15"),
        )

    monkeypatch.setattr(subject, "load_qmt_frame", minute_frame)
    ledger = tmp_path / "paper.json"
    prior_day = SESSION - timedelta(days=1)
    prior_open = datetime.combine(prior_day, time(10), tzinfo=CN)
    prior_buy = _replace_paper_intent(
        _pending_paper_intent(),
        feedback_id="sha256:" + "5" * 64,
        candidate_id="sha256:" + "6" * 64,
        symbol="SH.600001",
        created_at=prior_open,
        earliest_fill_at=prior_open,
        entry_confirmation_bar_closed_at=prior_open,
        entry_valid_until=datetime.combine(prior_day, time(15), tzinfo=CN),
    )
    append_human_paper_intent(ledger, prior_buy)
    settle_human_paper_intents(
        ledger,
        bars_by_symbol={
            prior_buy.symbol: (
                HumanPaperMinuteBar(
                    symbol=prior_buy.symbol,
                    opened_at=prior_open,
                    closed_at=prior_open + timedelta(minutes=1),
                    open=Decimal("10"),
                    high=Decimal("10.1"),
                    low=Decimal("9.9"),
                    close=Decimal("10"),
                    volume=Decimal("2000"),
                    buy_eligible=True,
                    sell_eligible=True,
                    security_status_complete=True,
                    corporate_action_state_complete=True,
                    execution_snapshot_sha256="sha256:" + "8" * 64,
                ),
            )
        },
    )
    monkeypatch.setattr(
        subject,
        "audit_human_paper_execution_evidence",
        lambda *_args, **_kwargs: {"status": "COMPLETE"},
    )
    failed_buy = _pending_paper_intent()
    persistent_exit = _replace_paper_intent(
        _pending_paper_intent(),
        feedback_id="sha256:" + "7" * 64,
        candidate_id="sha256:" + "9" * 64,
        symbol="SH.600001",
        side="SELL",
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )
    append_human_paper_intent(ledger, failed_buy)
    append_human_paper_intent(ledger, persistent_exit)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["status"] == "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_1M_GRID"
    assert result["new_virtual_fill_count"] == 1
    assert result["new_operations_cancellation_count"] == 1
    assert result["unresolved_persistent_exit_intent_count"] == 0
    assert result["operations_cancellation_evidence"]["status"] == "COMPLETE"
    document = load_human_paper_ledger(ledger)
    fills = [
        event["payload"]
        for event in document["events"]
        if event["kind"] == "FILL"
    ]
    assert [(value["symbol"], value["side"]) for value in fills] == [
        (prior_buy.symbol, "BUY"),
        (persistent_exit.symbol, "SELL"),
    ]
    assert [event["kind"] for event in document["events"]][-2:] == [
        "FILL",
        "OPERATIONS_CANCEL",
    ]

    forged_down_evidence = json.loads(
        Path(result["execution_evidence_object"]).read_text(encoding="utf-8")
    )
    forged_down_evidence.pop("content_sha256")
    forged_down_bar = next(
        value
        for value in forged_down_evidence["bars_by_symbol"][
            persistent_exit.symbol
        ]
        if value["closed_at"] == target.isoformat()
    )
    forged_down_bar.update(
        {
            "open": "9",
            "high": "9",
            "low": "9",
            "close": "9",
            "limit_down_locked": False,
        }
    )
    forged_down_evidence["content_sha256"] = subject.sha256_json(
        forged_down_evidence
    )
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_evidence",
        payload=forged_down_evidence,
    )
    forged_down_events = json.loads(json.dumps(document["events"]))
    forged_down_sell = next(
        event["payload"]
        for event in forged_down_events
        if event["kind"] == "FILL" and event["payload"]["side"] == "SELL"
    )
    forged_down_sell["price"] = "9"
    forged_down_sell["execution_snapshot_sha256"] = forged_down_evidence[
        "content_sha256"
    ]
    forged_down_audit = audit_human_paper_execution_evidence(
        forged_down_events,
        forward_root=args.root,
    )
    assert forged_down_audit["status"] == "INVALID"
    assert "limit-lock state cannot be recomputed" in (
        forged_down_audit["invalid_evidence"][0]["reason"]
    )

    # Re-hashing the ledger/evidence cannot turn a same-session lot into a
    # sellable T+1 lot.  The old BUY deliberately keeps a missing legacy
    # evidence identity; INVALID from the forged SELL must take precedence.
    forged_events = json.loads(json.dumps(document["events"]))
    forged_prior_buy = next(
        event["payload"]
        for event in forged_events
        if event["kind"] == "FILL" and event["payload"]["side"] == "BUY"
    )
    forged_same_session_close = datetime.combine(
        SESSION,
        time(9, 45),
        tzinfo=CN,
    ).isoformat()
    forged_prior_buy["filled_at"] = forged_same_session_close
    forged_prior_buy["source_bar_closed_at"] = forged_same_session_close
    forged_facts = json.loads(
        Path(result["execution_fact_object"]).read_text(encoding="utf-8")
    )
    forged_facts.pop("content_sha256")
    exit_fact = next(
        value
        for value in forged_facts["symbols"]
        if value["symbol"] == persistent_exit.symbol
    )
    exit_fact["oldest_virtual_acquired_session"] = SESSION.isoformat()
    forged_facts["content_sha256"] = subject.sha256_json(forged_facts)
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_facts",
        payload=forged_facts,
    )
    forged_evidence = json.loads(
        Path(result["execution_evidence_object"]).read_text(encoding="utf-8")
    )
    forged_evidence.pop("content_sha256")
    forged_evidence["execution_fact_snapshot_sha256"] = forged_facts[
        "content_sha256"
    ]
    forged_evidence["content_sha256"] = subject.sha256_json(forged_evidence)
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_evidence",
        payload=forged_evidence,
    )
    forged_sell = next(
        event["payload"]
        for event in forged_events
        if event["kind"] == "FILL" and event["payload"]["side"] == "SELL"
    )
    forged_sell["execution_snapshot_sha256"] = forged_evidence[
        "content_sha256"
    ]
    forged_fill_audit = audit_human_paper_execution_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert forged_fill_audit["status"] == "INVALID"
    assert "capture-time T+1 lots" in forged_fill_audit["invalid_evidence"][
        0
    ]["reason"]


@pytest.mark.parametrize("unavailable", ("suspended", "expired", "st"))
def test_ineligible_instrument_does_not_require_or_read_minute_grid(
    monkeypatch,
    tmp_path: Path,
    unavailable: str,
) -> None:
    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: _execution_fact(
            str(kwargs["symbol"]),
            suspended=unavailable == "suspended",
            expired=unavailable == "expired",
            is_st=unavailable == "st",
        ),
    )
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ineligible instrument must not read a minute grid")
        ),
    )
    ledger = tmp_path / "paper.json"
    intent = _pending_paper_intent()
    append_human_paper_intent(ledger, intent)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["status"] == "VIRTUAL_SETTLEMENT_READY"
    assert result["new_virtual_fill_count"] == 0
    assert result["new_operations_cancellation_count"] == 1
    assert result["execution_bar_grid_error_count"] == 0
    assert result["full_session_one_minute_grid_proven"] is True
    assert result["execution_bar_grid_audits"] == [
        {
            "symbol": intent.symbol,
            "status": "NOT_REQUIRED_INSTRUMENT_INELIGIBLE",
            "native_row_count": 0,
            "normalized_row_count": 0,
            "complete_sessions": [],
            "session_issues": [],
            "source_base_stream_revision": None,
        }
    ]
    assert result["operations_cancellation_evidence"]["status"] == "COMPLETE"
    assert result["operations_cancellation_evidence"][
        "security_gate_cancellation_count"
    ] == 1
    reason = {
        "suspended": "SUSPENDED",
        "expired": "EXPIRED",
        "st": "ST_BUY_PROHIBITED",
    }[unavailable]
    assert result["operations_cancellation_evidence"][
        "security_gate_reason_counts"
    ][reason] == 1
    document = load_human_paper_ledger(ledger)
    cancellation = document["events"][-1]["payload"]
    assert cancellation["reason_code"] == (
        "OPTIONAL_BUY_CANCELLED_BY_SECURITY_GATE"
    )
    assert cancellation["operations_state"] == "SECURITY_GATE_CLOSED"
    if unavailable == "expired":
        forged_facts = json.loads(
            Path(result["execution_fact_object"]).read_text(encoding="utf-8")
        )
        forged_facts.pop("content_sha256")
        forged_fact = forged_facts["symbols"][0]
        forged_fact["expired"] = False
        forged_fact["buy_eligible"] = True
        forged_fact["sell_eligible"] = True
        forged_facts["content_sha256"] = subject.sha256_json(forged_facts)
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_facts",
            payload=forged_facts,
        )
        forged_evidence = json.loads(
            Path(result["execution_evidence_object"]).read_text(
                encoding="utf-8"
            )
        )
        forged_evidence.pop("content_sha256")
        forged_evidence["execution_fact_snapshot_sha256"] = forged_facts[
            "content_sha256"
        ]
        forged_evidence["content_sha256"] = subject.sha256_json(
            forged_evidence
        )
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_evidence",
            payload=forged_evidence,
        )
        forged_events = json.loads(json.dumps(document["events"]))
        forged_cancel = forged_events[-1]["payload"]
        forged_cancel["execution_fact_snapshot_sha256"] = forged_facts[
            "content_sha256"
        ]
        forged_cancel["execution_evidence_snapshot_sha256"] = (
            forged_evidence["content_sha256"]
        )
        cancellation_identity = dict(forged_cancel)
        cancellation_identity.pop("cancellation_id")
        cancellation_identity["cancelled_at"] = datetime.fromisoformat(
            str(cancellation_identity["cancelled_at"])
        )
        forged_cancel["cancellation_id"] = (
            subject.HumanPaperOperationsCancellation(
                **cancellation_identity
            ).cancellation_id
        )
        forged_expiry_audit = (
            audit_human_paper_operations_cancellation_evidence(
                forged_events,
                forward_root=args.root,
            )
        )
        assert forged_expiry_audit["status"] == "INVALID"
        assert "raw security status cannot be recomputed" in (
            forged_expiry_audit["invalid_evidence"][0]["reason"]
        )
    repeated = subject._settle_human_paper(args=args, session=SESSION)
    assert repeated["status"] == "NO_PENDING_VIRTUAL_INTENTS"
    assert repeated["new_operations_cancellation_count"] == 0
    assert load_human_paper_ledger(ledger) == document


@pytest.mark.parametrize(
    "security_state",
    ("st", "suspended", "corporate_action"),
)
def test_security_gate_cancels_buy_without_consuming_same_symbol_exit(
    monkeypatch,
    tmp_path: Path,
    security_state: str,
) -> None:
    """A closed BUY gate never consumes the persistent strategic exit."""

    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    def execution_fact(**kwargs) -> dict[str, object]:
        fact = _execution_fact(
            str(kwargs["symbol"]),
            is_st=security_state == "st",
            suspended=security_state == "suspended",
        )
        if security_state == "corporate_action":
            fact["corporate_actions"] = [
                {"effective_on": SESSION.isoformat()}
            ]
        return fact

    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        execution_fact,
    )
    target = datetime.combine(SESSION, time(10, 2), tzinfo=CN)
    def minute_frame(*_args, **_kwargs) -> pd.DataFrame:
        if security_state == "suspended":
            raise AssertionError("suspended security must not read minute data")
        return _complete_execution_frame(
            target_closed_at=target,
            open_price=Decimal("10.10"),
            high=Decimal("10.20"),
            low=Decimal("10.00"),
            close=Decimal("10.15"),
        )

    monkeypatch.setattr(subject, "load_qmt_frame", minute_frame)
    ledger = tmp_path / "paper.json"
    prior_day = SESSION - timedelta(days=1)
    prior_open = datetime.combine(prior_day, time(10), tzinfo=CN)
    prior_buy = _replace_paper_intent(
        _pending_paper_intent(),
        feedback_id="sha256:" + "5" * 64,
        candidate_id="sha256:" + "6" * 64,
        created_at=prior_open,
        earliest_fill_at=prior_open,
        entry_confirmation_bar_closed_at=prior_open,
        entry_valid_until=datetime.combine(prior_day, time(15), tzinfo=CN),
    )
    append_human_paper_intent(ledger, prior_buy)
    settle_human_paper_intents(
        ledger,
        bars_by_symbol={
            prior_buy.symbol: (
                HumanPaperMinuteBar(
                    symbol=prior_buy.symbol,
                    opened_at=prior_open,
                    closed_at=prior_open + timedelta(minutes=1),
                    open=Decimal("10"),
                    high=Decimal("10.1"),
                    low=Decimal("9.9"),
                    close=Decimal("10"),
                    volume=Decimal("2000"),
                    buy_eligible=True,
                    sell_eligible=True,
                    security_status_complete=True,
                    corporate_action_state_complete=True,
                    execution_snapshot_sha256="sha256:" + "8" * 64,
                ),
            )
        },
    )
    monkeypatch.setattr(
        subject,
        "audit_human_paper_execution_evidence",
        lambda *_args, **_kwargs: {"status": "COMPLETE"},
    )
    optional_buy = _replace_paper_intent(
        _pending_paper_intent(),
        feedback_id="sha256:" + "7" * 64,
        candidate_id="sha256:" + "9" * 64,
    )
    persistent_exit = _replace_paper_intent(
        _pending_paper_intent(),
        feedback_id="sha256:" + "a" * 64,
        candidate_id="sha256:" + "b" * 64,
        side="SELL",
        entry_confirmation_bar_closed_at=None,
        entry_price_cap=None,
        entry_valid_until=None,
        entry_boundary_evidence_id=None,
    )
    append_human_paper_intent(ledger, optional_buy)
    append_human_paper_intent(ledger, persistent_exit)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["new_operations_cancellation_count"] == 1
    document = load_human_paper_ledger(ledger)
    assert document["events"][-1]["payload"]["intent_id"] == (
        optional_buy.intent_id
    )
    if security_state == "st":
        assert result["status"] == "VIRTUAL_SETTLEMENT_READY"
        assert result["new_virtual_fill_count"] == 1
        assert result["execution_bar_grid_audits"][0]["status"] == "COMPLETE"
        assert result["operations_cancellation_evidence"][
            "security_gate_reason_counts"
        ]["ST_BUY_PROHIBITED"] == 1
        assert [event["kind"] for event in document["events"]][-2:] == [
            "FILL",
            "OPERATIONS_CANCEL",
        ]
        assert document["events"][-2]["payload"]["side"] == "SELL"
        assert result["unresolved_persistent_exit_intent_count"] == 0
        assert result["security_blocked_persistent_exit_intent_count"] == 0
    elif security_state == "suspended":
        assert result["status"] == (
            "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_SECURITY_GATE"
        )
        assert result["new_virtual_fill_count"] == 0
        assert result["execution_bar_grid_audits"][0]["status"] == (
            "NOT_REQUIRED_INSTRUMENT_INELIGIBLE"
        )
        assert result["unresolved_persistent_exit_intent_count"] == 1
        assert result["security_blocked_persistent_exit_intent_count"] == 1
        assert document["events"][-1]["kind"] == "OPERATIONS_CANCEL"
        assert persistent_exit.intent_id not in human_paper_terminal_intent_ids(
            document["events"]
        )
    else:
        assert result["status"] == (
            "VIRTUAL_SETTLEMENT_PARTIALLY_BLOCKED_BY_EXECUTION_FACTS"
        )
        assert result["new_virtual_fill_count"] == 0
        assert result["execution_bar_grid_audits"][0]["status"] == "COMPLETE"
        audit = result["operations_cancellation_evidence"]
        assert audit["data_fault_cancellation_count"] == 1
        assert audit["execution_fact_incomplete_cancellation_count"] == 1
        assert audit["execution_fact_incomplete_reason_counts"][
            "CORPORATE_ACTION_RECONCILIATION_REQUIRED"
        ] == 1
        assert result["unresolved_persistent_exit_intent_count"] == 1
        assert result["fact_incomplete_persistent_exit_intent_count"] == 1
        assert document["events"][-1]["payload"]["reason_code"] == (
            "OPTIONAL_BUY_CANCELLED_BY_EXECUTION_DATA_HALT"
        )
        assert persistent_exit.intent_id not in human_paper_terminal_intent_ids(
            document["events"]
        )
        forged_facts = json.loads(
            Path(result["execution_fact_object"]).read_text(encoding="utf-8")
        )
        forged_facts.pop("content_sha256")
        forged_facts["symbols"][0]["position_corporate_action_conflict"] = False
        forged_facts["symbols"][0]["corporate_action_state_complete"] = True
        forged_facts["errors"] = []
        forged_facts["all_complete"] = True
        forged_facts["content_sha256"] = subject.sha256_json(forged_facts)
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_facts",
            payload=forged_facts,
        )
        forged_evidence = json.loads(
            Path(result["execution_evidence_object"]).read_text(encoding="utf-8")
        )
        forged_evidence.pop("content_sha256")
        forged_evidence["execution_fact_snapshot_sha256"] = forged_facts[
            "content_sha256"
        ]
        forged_evidence["content_sha256"] = subject.sha256_json(
            forged_evidence
        )
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_evidence",
            payload=forged_evidence,
        )
        forged_events = json.loads(json.dumps(document["events"]))
        forged_cancel = forged_events[-1]["payload"]
        forged_cancel["execution_fact_snapshot_sha256"] = forged_facts[
            "content_sha256"
        ]
        forged_cancel["execution_evidence_snapshot_sha256"] = forged_evidence[
            "content_sha256"
        ]
        cancellation_identity = dict(forged_cancel)
        cancellation_identity.pop("cancellation_id")
        cancellation_identity["cancelled_at"] = datetime.fromisoformat(
            str(cancellation_identity["cancelled_at"])
        )
        forged_cancel["cancellation_id"] = (
            subject.HumanPaperOperationsCancellation(
                **cancellation_identity
            ).cancellation_id
        )
        forged_audit = audit_human_paper_operations_cancellation_evidence(
            forged_events,
            forward_root=args.root,
        )
        assert forged_audit["status"] == "INVALID"
        assert (
            "corporate-action completeness cannot be recomputed"
            in forged_audit["invalid_evidence"][0]["reason"]
        )

        # The opposite forgery is equally invalid: merely setting the
        # incompleteness fields (and re-hashing every linked object) cannot
        # manufacture a company-action data halt when the raw action list has
        # no event in the oldest remaining FIFO lot's holding interval.
        false_incomplete_facts = json.loads(
            Path(result["execution_fact_object"]).read_text(encoding="utf-8")
        )
        false_incomplete_facts.pop("content_sha256")
        false_incomplete_facts["symbols"][0]["corporate_actions"] = []
        false_incomplete_facts["content_sha256"] = subject.sha256_json(
            false_incomplete_facts
        )
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_facts",
            payload=false_incomplete_facts,
        )
        false_incomplete_evidence = json.loads(
            Path(result["execution_evidence_object"]).read_text(
                encoding="utf-8"
            )
        )
        false_incomplete_evidence.pop("content_sha256")
        false_incomplete_evidence["execution_fact_snapshot_sha256"] = (
            false_incomplete_facts["content_sha256"]
        )
        false_incomplete_evidence["content_sha256"] = subject.sha256_json(
            false_incomplete_evidence
        )
        subject._immutable_semantic_json_object(
            args.root / "sessions" / SESSION.isoformat(),
            kind="paper_execution_evidence",
            payload=false_incomplete_evidence,
        )
        false_incomplete_events = json.loads(json.dumps(document["events"]))
        false_incomplete_cancel = false_incomplete_events[-1]["payload"]
        false_incomplete_cancel["execution_fact_snapshot_sha256"] = (
            false_incomplete_facts["content_sha256"]
        )
        false_incomplete_cancel["execution_evidence_snapshot_sha256"] = (
            false_incomplete_evidence["content_sha256"]
        )
        cancellation_identity = dict(false_incomplete_cancel)
        cancellation_identity.pop("cancellation_id")
        cancellation_identity["cancelled_at"] = datetime.fromisoformat(
            str(cancellation_identity["cancelled_at"])
        )
        false_incomplete_cancel["cancellation_id"] = (
            subject.HumanPaperOperationsCancellation(
                **cancellation_identity
            ).cancellation_id
        )
        false_incomplete_audit = (
            audit_human_paper_operations_cancellation_evidence(
                false_incomplete_events,
                forward_root=args.root,
            )
        )
        assert false_incomplete_audit["status"] == "INVALID"
        assert (
            "corporate-action completeness cannot be recomputed"
            in false_incomplete_audit["invalid_evidence"][0]["reason"]
        )


def test_security_gate_cancellation_audit_recomputes_raw_status_flags(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Re-hashing a suspended fact as eligible cannot forge cancellation proof."""

    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: _execution_fact(str(kwargs["symbol"]), suspended=True),
    )
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("suspended BUY must not read one-minute data")
        ),
    )
    ledger = tmp_path / "paper.json"
    append_human_paper_intent(ledger, _pending_paper_intent())
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )
    result = subject._settle_human_paper(args=args, session=SESSION)
    document = load_human_paper_ledger(ledger)

    forged_facts = json.loads(
        Path(result["execution_fact_object"]).read_text(encoding="utf-8")
    )
    forged_facts.pop("content_sha256")
    fact = forged_facts["symbols"][0]
    fact["instrument_status"] = 0
    fact["suspended"] = False
    fact["buy_eligible"] = True
    fact["sell_eligible"] = True
    forged_facts["content_sha256"] = subject.sha256_json(forged_facts)
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_facts",
        payload=forged_facts,
    )

    forged_evidence = json.loads(
        Path(result["execution_evidence_object"]).read_text(encoding="utf-8")
    )
    forged_evidence.pop("content_sha256")
    forged_evidence["execution_fact_snapshot_sha256"] = forged_facts[
        "content_sha256"
    ]
    forged_evidence["content_sha256"] = subject.sha256_json(forged_evidence)
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_evidence",
        payload=forged_evidence,
    )

    forged_events = json.loads(json.dumps(document["events"]))
    cancellation = forged_events[-1]["payload"]
    cancellation["execution_fact_snapshot_sha256"] = forged_facts[
        "content_sha256"
    ]
    cancellation["execution_evidence_snapshot_sha256"] = forged_evidence[
        "content_sha256"
    ]
    cancellation_identity = dict(cancellation)
    cancellation_identity.pop("cancellation_id")
    cancellation_identity["cancelled_at"] = datetime.fromisoformat(
        str(cancellation_identity["cancelled_at"])
    )
    cancellation["cancellation_id"] = subject.HumanPaperOperationsCancellation(
        **cancellation_identity
    ).cancellation_id
    audit = audit_human_paper_operations_cancellation_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert audit["status"] == "INVALID"
    assert audit["verified_cancellation_count"] == 0
    assert "eligible BUY fact" in audit["invalid_evidence"][0]["reason"]


def test_buy_price_cap_rejection_resolves_exact_first_one_minute_bar_evidence(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A no-chase rejection must be independently reproducible from raw 1m facts."""

    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": "600000.SH",
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "test",
            "instrument_status": 0,
            "is_trading": False,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10",
            "limit_up": "11",
            "limit_down": "9",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    closed_at = datetime.combine(SESSION, time(10, 2), tzinfo=CN)
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: _complete_execution_frame(
            target_closed_at=closed_at,
            open_price=Decimal("10.10"),
            high=Decimal("10.20"),
            low=Decimal("10.06"),
            close=Decimal("10.15"),
        ),
    )
    intent = _replace_paper_intent(
        _pending_paper_intent(),
        entry_price_cap=Decimal("10.05"),
        entry_valid_until=closed_at,
    )
    ledger = tmp_path / "paper.json"
    append_human_paper_intent(ledger, intent)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["new_virtual_fill_count"] == 0
    assert result["new_execution_rejection_count"] == 1
    assert result["execution_rejection_evidence"]["status"] == "COMPLETE"
    paper = load_human_paper_ledger(ledger)
    rejection = next(
        event["payload"]
        for event in paper["events"]
        if event["kind"] == "EXECUTION_REJECT"
    )
    assert rejection["reason_code"] == (
        "BUY_PRICE_CAP_EXCEEDED_AT_FIRST_EXECUTABLE_BAR"
    )
    audit = audit_human_paper_execution_rejection_evidence(
        paper["events"],
        forward_root=args.root,
    )
    assert audit["verified_rejection_count"] == audit["rejection_count"] == 1
    assert audit["first_eligible_bar_verified"] is True
    assert audit["price_cap_and_ttl_verified"] is True

    forged_facts = json.loads(
        Path(result["execution_fact_object"]).read_text(encoding="utf-8")
    )
    forged_facts.pop("content_sha256")
    forged_facts["symbols"][0]["instrument_status"] = 1
    forged_facts["content_sha256"] = subject.sha256_json(forged_facts)
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_facts",
        payload=forged_facts,
    )
    original_evidence = json.loads(
        Path(result["execution_evidence_object"]).read_text(encoding="utf-8")
    )
    forged_fact_evidence = json.loads(json.dumps(original_evidence))
    forged_fact_evidence.pop("content_sha256")
    forged_fact_evidence["execution_fact_snapshot_sha256"] = forged_facts[
        "content_sha256"
    ]
    forged_fact_evidence["content_sha256"] = subject.sha256_json(
        forged_fact_evidence
    )
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_evidence",
        payload=forged_fact_evidence,
    )
    forged_fact_events = json.loads(json.dumps(paper["events"]))
    forged_fact_events[-1]["payload"]["execution_snapshot_sha256"] = (
        forged_fact_evidence["content_sha256"]
    )
    forged_fact_audit = audit_human_paper_execution_rejection_evidence(
        forged_fact_events,
        forward_root=args.root,
    )
    assert forged_fact_audit["status"] == "INVALID"
    assert "raw security status cannot be recomputed" in (
        forged_fact_audit["invalid_evidence"][0]["reason"]
    )

    forged_grid_evidence = json.loads(json.dumps(original_evidence))
    forged_grid_evidence.pop("content_sha256")
    forged_grid_evidence["all_required_bar_grids_complete"] = False
    forged_grid_evidence["content_sha256"] = subject.sha256_json(
        forged_grid_evidence
    )
    subject._immutable_semantic_json_object(
        args.root / "sessions" / SESSION.isoformat(),
        kind="paper_execution_evidence",
        payload=forged_grid_evidence,
    )
    forged_grid_events = json.loads(json.dumps(paper["events"]))
    forged_grid_events[-1]["payload"]["execution_snapshot_sha256"] = (
        forged_grid_evidence["content_sha256"]
    )
    forged_grid_audit = audit_human_paper_execution_rejection_evidence(
        forged_grid_events,
        forward_root=args.root,
    )
    assert forged_grid_audit["status"] == "INVALID"
    assert "aggregate grid status changed" in (
        forged_grid_audit["invalid_evidence"][0]["reason"]
    )

    evidence_path = (
        args.root
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_evidence"
        / f"{rejection['execution_snapshot_sha256'][7:]}.json"
    )
    forged_stable = json.loads(evidence_path.read_text(encoding="utf-8"))
    forged_stable.pop("content_sha256")
    forged_candidate = next(
        value
        for value in forged_stable["bars_by_symbol"]["SH.600000"]
        if value["closed_at"] == closed_at.isoformat()
    )
    forged_candidate["open"] = "10.00"
    forged_id = subject.sha256_json(forged_stable)
    evidence_path.with_name(f"{forged_id[7:]}.json").write_text(
        json.dumps({**forged_stable, "content_sha256": forged_id}),
        encoding="utf-8",
    )
    forged_events = json.loads(json.dumps(paper["events"]))
    forged_events[-1]["payload"]["execution_snapshot_sha256"] = forged_id
    forged_audit = audit_human_paper_execution_rejection_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert forged_audit["status"] == "INVALID"
    assert forged_audit["verified_rejection_count"] == 0


def test_virtual_buy_uses_same_minute_marks_for_every_open_position(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": kwargs["symbol"],
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "test",
            "instrument_status": 0,
            "is_trading": False,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10",
            "limit_up": "11",
            "limit_down": "9",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    second_pass = False

    def minute_frame(code: str, *_args, **_kwargs) -> pd.DataFrame:
        closed = datetime.combine(
            SESSION,
            time(10, 6) if second_pass else time(10, 2),
            tzinfo=CN,
        )
        price = Decimal("10.20") if code == "SH.600001" else Decimal("10.10")
        return _complete_execution_frame(
            target_closed_at=closed,
            open_price=price,
            high=price + Decimal("0.05"),
            low=price - Decimal("0.05"),
            close=price,
        )

    monkeypatch.setattr(subject, "load_qmt_frame", minute_frame)
    ledger = tmp_path / "paper.json"
    first = _pending_paper_intent()
    append_human_paper_intent(ledger, first)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )
    first_result = subject._settle_human_paper(args=args, session=SESSION)
    assert first_result["new_virtual_fill_count"] == 1

    second_created = datetime.combine(SESSION, time(10, 3), tzinfo=CN)
    second = _replace_paper_intent(
        first,
        feedback_id="sha256:" + "7" * 64,
        symbol="SH.600001",
        created_at=second_created,
        earliest_fill_at=second_created,
    )
    append_human_paper_intent(ledger, second)
    _paper_sector_ledger(tmp_path)
    second_pass = True

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["new_virtual_fill_count"] == 1
    assert result["portfolio_mark_unresolved_count"] == 0
    assert result["portfolio_fill_decision_audit"]["status"] == "COMPLETE"
    assert result["portfolio_fill_decision_audit"]["approved_fill_count"] == 2
    assert result["capital_evaluations"][0]["position_marks"] == [
            {
                "symbol": "SH.600000",
                "quantity": 100,
                "price": "10.1",
                "market_value": "1010.00",
            }
    ]
    evidence = json.loads(
        Path(result["execution_evidence_snapshot"]).read_text(encoding="utf-8")
    )
    assert set(evidence["bars_by_symbol"]) == {"SH.600000", "SH.600001"}
    assert result["execution_fact_complete_symbol_count"] == 2
    paper = load_human_paper_ledger(ledger)
    second_fill = tuple(
        event["payload"] for event in paper["events"] if event["kind"] == "FILL"
    )[-1]
    assert second_fill["position_marks"] == result["capital_evaluations"][0][
        "position_marks"
    ]

    evidence_path = (
        args.root
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_evidence"
        / f"{second_fill['execution_snapshot_sha256'][7:]}.json"
    )
    forged_stable = json.loads(evidence_path.read_text(encoding="utf-8"))
    forged_stable.pop("content_sha256")
    forged_mark_bar = next(
        value
        for value in forged_stable["bars_by_symbol"]["SH.600000"]
        if value["closed_at"] == second_fill["source_bar_closed_at"]
    )
    forged_mark_bar["close"] = "10.09"
    forged_id = subject.sha256_json(forged_stable)
    evidence_path.with_name(f"{forged_id[7:]}.json").write_text(
        json.dumps({**forged_stable, "content_sha256": forged_id}),
        encoding="utf-8",
    )
    forged_events = json.loads(json.dumps(paper["events"]))
    forged_events[-1]["payload"]["execution_snapshot_sha256"] = forged_id
    forged_audit = audit_human_paper_execution_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert forged_audit["status"] == "INVALID"
    assert "synchronous 1m bar facts disagree" in forged_audit[
        "invalid_evidence"
    ][0]["reason"]


def test_portfolio_rejection_proves_and_audits_synchronous_position_marks(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": kwargs["symbol"],
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "test",
            "instrument_status": 0,
            "is_trading": False,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10",
            "limit_up": "11",
            "limit_down": "9",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    later = False

    def minute_frame(code: str, *_args, **_kwargs) -> pd.DataFrame:
        closed = datetime.combine(
            SESSION,
            time(10, 6) if later else time(10, 2),
            tzinfo=CN,
        )
        price = Decimal("2000") if code == "SH.600001" else Decimal("10.10")
        return _complete_execution_frame(
            target_closed_at=closed,
            open_price=price,
            high=price + Decimal("1"),
            low=price - Decimal("1"),
            close=price,
        )

    monkeypatch.setattr(subject, "load_qmt_frame", minute_frame)
    ledger = tmp_path / "paper.json"
    first = _pending_paper_intent()
    append_human_paper_intent(ledger, first)
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )
    assert subject._settle_human_paper(args=args, session=SESSION)[
        "new_virtual_fill_count"
    ] == 1

    second_created = datetime.combine(SESSION, time(10, 3), tzinfo=CN)
    second = _replace_paper_intent(
        first,
        feedback_id="sha256:" + "8" * 64,
        symbol="SH.600001",
        created_at=second_created,
        earliest_fill_at=second_created,
    )
    append_human_paper_intent(ledger, second)
    _paper_sector_ledger(tmp_path)
    later = True
    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["portfolio_rejection_count"] == 1
    assert result["portfolio_decision_audit"]["status"] == "COMPLETE"
    assert result["capital_rejection_evidence"]["status"] == "COMPLETE"
    assert result["capital_rejection_evidence"][
        "synchronous_position_marks_verified"
    ] is True
    paper = load_human_paper_ledger(ledger)
    rejection = paper["events"][-1]["payload"]
    assert paper["events"][-1]["kind"] == "PORTFOLIO_REJECT"
    assert rejection["position_marks"] == [
            {
                "symbol": "SH.600000",
                "quantity": 100,
                "price": "10.1",
                "market_value": "1010.00",
            }
    ]

    evidence_path = (
        args.root
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_evidence"
        / f"{rejection['execution_snapshot_sha256'][7:]}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    forged_stable = json.loads(json.dumps(evidence))
    forged_stable.pop("content_sha256")
    forged_mark_bar = next(
        value
        for value in forged_stable["bars_by_symbol"]["SH.600000"]
        if value["closed_at"] == rejection["candidate_bar_closed_at"]
    )
    forged_mark_bar["close"] = "10.09"
    forged_id = subject.sha256_json(forged_stable)
    evidence_path.with_name(f"{forged_id[7:]}.json").write_text(
        json.dumps({**forged_stable, "content_sha256": forged_id}),
        encoding="utf-8",
    )
    forged_events = json.loads(json.dumps(paper["events"]))
    forged_events[-1]["payload"]["execution_snapshot_sha256"] = forged_id
    audit = audit_human_paper_capital_rejection_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert audit["status"] == "INVALID"
    assert "synchronous 1m bar facts disagree" in audit["invalid_evidence"][0][
        "reason"
    ]

    forged_quantity_events = json.loads(json.dumps(paper["events"]))
    forged_quantity_mark = forged_quantity_events[-1]["payload"][
        "position_marks"
    ][0]
    forged_quantity_mark["quantity"] = 200
    forged_quantity_mark["market_value"] = "2020.00"
    forged_quantity_audit = audit_human_paper_capital_rejection_evidence(
        forged_quantity_events,
        forward_root=args.root,
    )
    assert forged_quantity_audit["status"] == "INVALID"
    assert "instrument facts and bar disagree" in (
        forged_quantity_audit["invalid_evidence"][0]["reason"]
    )

    forged_coverage_events = json.loads(json.dumps(paper["events"]))
    forged_coverage_events[-1]["payload"]["position_marks"] = []
    forged_coverage_audit = audit_human_paper_capital_rejection_evidence(
        forged_coverage_events,
        forward_root=args.root,
    )
    assert forged_coverage_audit["status"] == "INVALID"
    assert "mark coverage changed" in (
        forged_coverage_audit["invalid_evidence"][0]["reason"]
    )


def test_virtual_capital_rejection_resolves_exact_first_one_minute_bar(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A terminal cash rejection must be as auditable as a virtual fill."""

    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "native_code": "600000.SH",
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "instrument_name": "test",
            "instrument_status": 0,
            "is_trading": False,
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "is_st": False,
            "pre_close": "10000",
            "limit_up": "11000",
            "limit_down": "9000",
            "price_tick": "0.01",
            "corporate_actions": [],
            "source_methods": (
                "QMT_GET_INSTRUMENT_DETAIL",
                "QMT_GET_DIVID_FACTORS",
            ),
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    closed_at = datetime.combine(SESSION, time(10, 2), tzinfo=CN)
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: _complete_execution_frame(
            target_closed_at=closed_at,
            open_price=Decimal("10000.00"),
            high=Decimal("10001.00"),
            low=Decimal("9999.00"),
            close=Decimal("10000.00"),
        ),
    )
    ledger = tmp_path / "paper.json"
    append_human_paper_intent(ledger, _pending_paper_intent())
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=ledger,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=_paper_sector_ledger(tmp_path),
    )

    result = subject._settle_human_paper(args=args, session=SESSION)

    assert result["new_virtual_fill_count"] == 0
    assert result["capital_rejection_count"] == 1
    assert result["capital_rejection_evidence"]["status"] == "COMPLETE"
    assert result["capital_decision_audit"]["status"] == "NO_REJECTIONS"
    assert result["portfolio_decision_audit"]["status"] == "COMPLETE"
    paper = load_human_paper_ledger(ledger)
    rejection = next(
        event["payload"]
        for event in paper["events"]
        if event["kind"] == "PORTFOLIO_REJECT"
    )
    audit = audit_human_paper_capital_rejection_evidence(
        paper["events"],
        forward_root=args.root,
    )
    assert audit["verified_rejection_count"] == audit["rejection_count"] == 1
    assert audit["first_eligible_bar_verified"] is True

    evidence_path = (
        args.root
        / "sessions"
        / SESSION.isoformat()
        / "objects"
        / "paper_execution_evidence"
        / f"{rejection['execution_snapshot_sha256'][7:]}.json"
    )
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    forged_stable = json.loads(json.dumps(evidence))
    forged_stable.pop("content_sha256")
    earlier_closed_at = datetime.combine(SESSION, time(10, 1), tzinfo=CN)
    earlier = next(
        value
        for value in forged_stable["bars_by_symbol"]["SH.600000"]
        if value["closed_at"] == earlier_closed_at.isoformat()
    )
    earlier["volume"] = "2000.0"
    forged_id = subject.sha256_json(forged_stable)
    forged_path = evidence_path.with_name(f"{forged_id[7:]}.json")
    forged_path.write_text(
        json.dumps({**forged_stable, "content_sha256": forged_id}),
        encoding="utf-8",
    )
    forged_events = json.loads(json.dumps(paper["events"]))
    forged_events[-1]["payload"]["execution_snapshot_sha256"] = forged_id
    not_first = audit_human_paper_capital_rejection_evidence(
        forged_events,
        forward_root=args.root,
    )
    assert not_first["status"] == "INVALID"
    assert "first eligible" in not_first["invalid_evidence"][0]["reason"]

    evidence_path.unlink()
    missing = audit_human_paper_capital_rejection_evidence(
        paper["events"],
        forward_root=args.root,
    )
    assert missing["status"] == "MISSING"
    assert missing["missing_evidence"][0]["rejection_id"] == rejection[
        "rejection_id"
    ]
    with pytest.raises(
        RuntimeError,
        match="existing virtual capital rejections have incomplete",
    ):
        subject._settle_human_paper(args=args, session=SESSION)


def test_market_data_gate_prefers_completed_read_only_qmt_rpc(
    monkeypatch,
    tmp_path: Path,
) -> None:
    local_calls = []

    def local(*_args, **_kwargs):
        local_calls.append(True)
        raise AssertionError("complete native evidence must not read fallback")

    monkeypatch.setattr(subject, "load_qmt_frame", local)
    native_calls = []

    def native(**kwargs):
        native_calls.append(kwargs)
        return _frame(kwargs["frequency"], complete=True)

    result = subject._market_data_gate(
        session=SESSION,
        qmt_data_dir=tmp_path,
        native_frame_provider=native,
    )

    assert result["complete"] is True
    assert local_calls == []
    assert [call["frequency"] for call in native_calls] == ["1m", "5m"]
    assert {row["source"] for row in result["frequencies"].values()} == {
        "QMT_RPC"
    }
    assert result["native_rpc_market_data_attempted"] is True
    assert result["native_rpc_skip_download"] is True
    assert result["real_account_accessed"] is False
    assert result["real_order_transport_enabled"] is False


def test_market_data_gate_falls_back_to_local_when_rpc_is_incomplete(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda _code, frequency, **_kwargs: _frame(frequency, complete=True),
    )

    result = subject._market_data_gate(
        session=SESSION,
        qmt_data_dir=tmp_path,
        native_frame_provider=lambda **kwargs: _frame(
            kwargs["frequency"],
            complete=False,
        ),
    )

    assert result["complete"] is True
    for audit in result["frequencies"].values():
        assert audit["source"] == "QMT_LOCAL_CACHE"
        assert audit["fallback_used"] is True
        assert [row["source"] for row in audit["attempts"]] == [
            "QMT_RPC",
            "QMT_LOCAL_CACHE",
        ]


def test_market_data_gate_uses_bounded_refresh_only_after_read_sources_fail(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda _code, frequency, **_kwargs: _frame(frequency, complete=False),
    )
    refresh_calls = []

    def refresh(**kwargs):
        refresh_calls.append(kwargs)
        return _frame(kwargs["frequency"], complete=True)

    result = subject._market_data_gate(
        session=SESSION,
        qmt_data_dir=tmp_path,
        native_frame_provider=lambda **kwargs: _frame(
            kwargs["frequency"],
            complete=False,
        ),
        refresh_frame_provider=refresh,
    )

    assert result["complete"] is True
    assert [call["frequency"] for call in refresh_calls] == ["1m", "5m"]
    assert result["bounded_incremental_refresh_enabled"] is True
    assert result["market_data_was_synthesized"] is False
    for frequency, audit in result["frequencies"].items():
        assert audit["source"] == "QMT_RPC_INCREMENTAL_REFRESH"
        assert audit["incremental_refresh_used"] is True
        expected_sources = [
            "QMT_RPC",
            "QMT_LOCAL_CACHE",
        ]
        if frequency == "5m":
            expected_sources.append("QMT_COMPLETED_1M_RESAMPLED_5M")
        expected_sources.append("QMT_RPC_INCREMENTAL_REFRESH")
        assert [row["source"] for row in audit["attempts"]] == expected_sources


def test_market_data_gate_builds_complete_five_minute_from_completed_one_minute(
    monkeypatch,
    tmp_path: Path,
) -> None:
    dates = (
        *pd.date_range(
            datetime.combine(SESSION, time(9, 31), tzinfo=CN),
            periods=120,
            freq="1min",
        ),
        *pd.date_range(
            datetime.combine(SESSION, time(13, 1), tzinfo=CN),
            periods=120,
            freq="1min",
        ),
    )
    one = pd.DataFrame(
        {
            "code": "SH.600000",
            "date": dates,
            "open": 10.0,
            "high": 10.2,
            "low": 9.8,
            "close": 10.1,
            "volume": 1000.0,
        }
    )
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda _code, frequency, **_kwargs: _frame(frequency, complete=False),
    )

    result = subject._market_data_gate(
        session=SESSION,
        qmt_data_dir=tmp_path,
        native_frame_provider=lambda **kwargs: (
            one if kwargs["frequency"] == "1m" else _frame("5m", complete=False)
        ),
    )

    assert result["complete"] is True
    assert result["reason_codes"] == ()
    assert result["frequencies"]["1m"]["row_count"] == 240
    assert result["frequencies"]["1m"]["last_at"].endswith("15:00:00+08:00")
    five = result["frequencies"]["5m"]
    assert five["source"] == "QMT_COMPLETED_1M_RESAMPLED_5M"
    assert five["row_count"] == 48
    assert five["last_at"].endswith("15:00:00+08:00")
    assert five["resampled_from_completed_one_minute"] is True
    assert result["tick_data_used"] is False
    assert result["minimum_market_data_frequency"] == "1m"


def test_qmt_rpc_market_frame_forbids_download_and_uses_exact_close(
    monkeypatch,
) -> None:
    import chanlun.exchange as exchange_module

    class Exchange:
        def __init__(self) -> None:
            self.calls = []

        def klines(self, code, frequency, **kwargs):
            self.calls.append((code, frequency, kwargs))
            return _frame(frequency, complete=True)

    exchange = Exchange()
    monkeypatch.setattr(exchange_module, "get_exchange", lambda _market: exchange)

    kwargs = {
        "code": "SH.600000",
        "frequency": "1m",
        "start_at": datetime.combine(SESSION, time(9, 30), tzinfo=CN),
        "end_at": datetime.combine(SESSION, time(15), tzinfo=CN),
        "minimum_rows": 240,
    }
    result = subject._qmt_rpc_market_frame(**kwargs)
    refreshed = subject._qmt_incremental_market_frame(**kwargs)

    assert len(result) == 241
    assert len(refreshed) == 241
    assert exchange.calls[0] == (
        "SH.600000",
        "1m",
        {
            "start_date": "20260728093000",
            "end_date": "20260728150100",
            "args": {
                "req_counts": 240,
                "skip_download": True,
                "research_exact_end": True,
                "dividend_type": "front",
            },
        },
    )
    assert exchange.calls[1][2]["args"]["skip_download"] is False


def test_cumulative_paper_replay_carries_all_daily_batches(
    monkeypatch,
    tmp_path: Path,
) -> None:
    for offset in range(2):
        session = SESSION + timedelta(days=offset)
        directory = tmp_path / "sessions" / session.isoformat()
        directory.mkdir(parents=True)
        decision = datetime.combine(session, time(10), tzinfo=CN)
        batches = (
            ReplayBatch(
                batch_id=f"paper:{session}",
                decision_at=decision,
                valuation_at=decision + timedelta(minutes=30),
                events=(),
            ),
        )
        (directory / "forward_replay_batches.pkl").write_bytes(
            pickle.dumps(batches, protocol=pickle.HIGHEST_PROTOCOL)
        )
        (directory / "forward_active_signals.pkl").write_bytes(
            pickle.dumps(
                {"strategic": {}, "tactical": {}},
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        )

    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: pd.DataFrame(
            {
                "date": [
                    datetime.combine(SESSION, time(15), tzinfo=CN),
                    datetime.combine(
                        SESSION + timedelta(days=1),
                        time(15),
                        tzinfo=CN,
                    ),
                ]
            }
        ),
    )

    state = subject._cumulative_replay(
        root=tmp_path,
        through_session=SESSION + timedelta(days=1),
    )

    assert state["through_session"] == "2026-07-29"
    assert len(state["batch_files"]) == 2
    assert state["positions"] == []
    assert state["metrics"]["empty_replay"] is True
    assert state["technical_mode"] == "RESEARCH_APPROXIMATION"
    assert str(state["technical_approximation_parameter_set_id"]).startswith(
        "sha256:"
    )
    assert state["active_strategic_signal_count"] == 0
    assert (tmp_path / "cumulative_state.json").is_file()
    assert (tmp_path / "active_signal_state.pkl").is_file()


def test_live_screening_snapshot_is_archived_without_order_authority(
    monkeypatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "live.json"
    decision_core = HumanAssistedDecisionCore()
    screening_policy = {
        "selection_universe_source": "qmt_gics3_current_components",
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
    }
    neutral_contexts = {
        frequency: {
            "frequency": frequency,
            "direction": "up" if frequency == "30m" else "neutral",
            "disposition": (
                "supportive" if frequency == "30m" else "neutral"
            ),
            "hard_block": False,
            "dominant_point_id": (
                "sha256:" + "a" * 64 if frequency == "30m" else None
            ),
            "dominant_point_type": "3buy" if frequency == "30m" else None,
                "reason_codes": [
                    (
                        "confirmed_buy_structure"
                        if frequency == "30m"
                        else "stock_one_minute_trigger_only"
                        if frequency == "1m"
                        else "no_active_directional_point"
                    )
                ],
            "observed_at": "2026-07-28T15:00:00+08:00",
        }
        for frequency in ("30m", "5m", "1m")
    }
    sector_document = {
        "sector_id": "qmt-gics3:test",
        "sector_name": "测试行业",
        "eligible": True,
        "hard_block": False,
        "regime": "supportive",
        "rank": 1,
        "rank_score": 45,
        "rank_components": {
            "thirty_support": 40,
            "five_support": 0,
            "one_support": 0,
            "neutral_access": 5,
        },
        "reason_codes": ["structural_ranking_only"],
        "horizontal_strength": None,
        "horizontal_rank": None,
        "strength_anchor_session": None,
        "strength_member_count": 0,
        "strength_source_revision": None,
        "strength_reason_codes": [],
        "context_30m": neutral_contexts["30m"],
        "context_5m": neutral_contexts["5m"],
        "context_1m": neutral_contexts["1m"],
    }
    strength_batch = build_horizontal_sector_strength_batch(
        decision_time=datetime(2026, 7, 28, 15, 1, tzinfo=CN),
        benchmark_symbol="SH.000300",
        benchmark_daily=(),
        members_by_sector={
            "qmt-gics3:test": (
                SectorMemberHistory(
                    "SH.600000",
                    SESSION,
                    "UNEXPLAINED_GAP",
                    (),
                ),
            )
        },
        membership_revision=SECTOR_CATALOG_REVISION,
    )
    strength = strength_batch["qmt-gics3:test"]
    sector_document.update(
        {
            "strength_member_count": strength.member_count,
            "strength_source_revision": strength.source_revision,
            "strength_reason_codes": list(strength.reason_codes),
        }
    )
    signal_sector_document = {**sector_document, "rank": None}
    screening_policy_id = subject.sha256_json(screening_policy)
    universe_revision = subject.sha256_json(
        {
            "schema": "test-screening-universe/v1",
            "codes": ["SH.600000"],
        }
    )
    coverage_epoch_id = screening_coverage_epoch_id(
        market_data_as_of=datetime(2026, 7, 28, 15, tzinfo=CN),
        universe_revision=universe_revision,
        sector_catalog_revision=SECTOR_CATALOG_REVISION,
        sector_strength_evidence_revision=strength_batch.evidence_revision,
        decision_core_id=decision_core.contract_id,
        screening_policy_id=screening_policy_id,
        structure_version="test-structure/v1",
        parameter_version="test-parameter/v1",
    )
    point_id = "sha256:" + "b" * 64
    setup_id = subject.sha256_json(
        {
            "schema": "chanlun-trade-setup/v1",
            "point_id": point_id,
            "sector_id": "qmt-gics3:test",
        }
    )
    signal_id = subject.sha256_json(
        {
            "schema": "chanlun-signal-lifecycle/v1",
            "setup_id": setup_id,
            "side": "buy",
        }
    )
    source.write_text(
        json.dumps(
            {
                "schema_version": "chanlun-trading-screening/v3",
                "structure_version": "test-structure/v1",
                "parameter_version": "test-parameter/v1",
                "signal_document_contract_id": SIGNAL_DOCUMENT_CONTRACT_ID,
                "monitor_instrument_exclusion_contract_id": (
                    MONITOR_INSTRUMENT_EXCLUSION_CONTRACT_ID
                ),
                "available": True,
                "scan_state": "complete",
                "scanned_at": "2026-07-28T15:01:00+08:00",
                "as_of": "2026-07-28T15:01:00+08:00",
                "market_data_as_of": "2026-07-28T15:00:00+08:00",
                "coverage_epoch_id": coverage_epoch_id,
                "sector_first": True,
                "read_only": True,
                "research_only": True,
                "no_order_execution": True,
                "screening_policy": screening_policy,
                "screening_policy_id": screening_policy_id,
                "decision_core_id": decision_core.contract_id,
                "decision_core": decision_core.contract.document(),
                "counts_by_stage": {"armed": 1},
                "counts_by_point_type": {"3buy": 1},
                "sectors": [sector_document],
                "sector_strength_evidence": strength_batch.evidence_document(),
                "sector_strength_evidence_revision": (
                    strength_batch.evidence_revision
                ),
                "monitor_instrument_exclusions": [],
                "signals": [
                    {
                        "signal_id": signal_id,
                        "point_id": point_id,
                        "setup_id": setup_id,
                        "decision_core_id": decision_core.contract_id,
                        "code": "SH.600000",
                        "point_type": "3buy",
                        "observed_at": "2026-07-28T15:00:00+08:00",
                        "side": "buy",
                        "tower": "formal",
                        "recursive_level": 0,
                        "structure_scope": "physical_timeframe_level_zero",
                        "structure_frequencies": ["d", "30m", "5m", "1m"],
                        "stroke_mode": "old",
                        "recursive_structure_used": False,
                        "physical_timeframe_level_zero": True,
                        "lifecycle_stage": "armed",
                        "technical_entry_allowed": False,
                        "entry_allowed": False,
                        "exit_allowed": False,
                        "exit_action": "none",
                        "human_confirmation_required": True,
                        "automated_order_authorized": False,
                        "live_status": "LIVE_DISABLED",
                        "structural_stop": "9.50",
                        "risk_multiplier": "0.75",
                        "decision_reasons": [
                            "one_minute_not_confirmed",
                            "three_buy_lacks_tick_clearance",
                        ],
                        "conflict": {
                            "hard_block": False,
                            "blocking_point_ids": [],
                            "risk_only_point_ids": [],
                        },
                        "sector": signal_sector_document,
                        "selection_sources": ["QMT_SECTOR_TRIGGER"],
                        "sector_triggered": True,
                        "monitor_only": False,
                        "context_30m": {
                            "direction": "neutral",
                            "disposition": "neutral",
                            "hard_block": False,
                            "dominant_point_id": None,
                            "dominant_point_type": None,
                            "reason_codes": [
                                "no_active_directional_point"
                            ],
                        },
                        "context_d": {
                            "frequency": "d",
                            "direction": "neutral",
                            "disposition": "neutral",
                            "hard_block": False,
                            "dominant_point_id": None,
                            "dominant_point_type": None,
                            "reason_codes": ["no_active_directional_point"],
                            "observed_at": "2026-07-28T15:00:00+08:00",
                        },
                        "setup_5m": {
                            "point_id": point_id,
                            "point_type": "3buy",
                            "side": "buy",
                            "status": "confirmed",
                            "source_frequency": "5m",
                            "tower": "formal",
                            "recursive_level": 0,
                            "anchor_at": "2026-07-28T14:55:00+08:00",
                            "confirmed_at": "2026-07-28T14:55:00+08:00",
                            "available_at": "2026-07-28T14:55:00+08:00",
                            "price_basis_revision": "qmt-none-test-v1",
                            "anchor_price": "10.00",
                            "invalidation_price": "9.50",
                            "center_id": "center-test",
                            "center_zd": "9.80",
                            "center_zg": "10.10",
                            "center_ordinal": 1,
                            "variant": "standard",
                            "divergence_kind": None,
                            "missing_conditions": [],
                            "evidence_codes": [],
                            "contains_unfinished_segment": False,
                            "actionable": True,
                        },
                        "trigger_1m": None,
                        "higher_timeframe_risk": {
                            "market_gate": "UNRESOLVED",
                            "sector_gate": "UNRESOLVED",
                            "symbol_gate": "UNRESOLVED",
                            "market_states": {
                                "M": "UNRESOLVED",
                                "W": "UNRESOLVED",
                                "D": "UNRESOLVED",
                            },
                            "sector_states": {
                                "M": "UNRESOLVED",
                                "W": "UNRESOLVED",
                                "D": "UNRESOLVED",
                            },
                            "symbol_states": {
                                "M": "UNRESOLVED",
                                "W": "UNRESOLVED",
                                "D": "UNRESOLVED",
                            },
                            "market_reason_codes": [
                                "QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE"
                            ],
                            "sector_reason_codes": [
                                "QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE"
                            ],
                            "symbol_reason_codes": [
                                "QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE"
                            ],
                            "reason_codes": [
                                "QMT_HIGHER_TIMEFRAME_PROVIDER_UNAVAILABLE"
                            ],
                            "market_period_diagnostics": [],
                            "sector_period_diagnostics": [],
                            "symbol_period_diagnostics": [],
                            "new_entry_requires_all_green": True,
                        },
                        "warmup": {
                            "converged": False,
                            "by_frequency": [
                                {
                                    "frequency": "d",
                                    "converged": True,
                                    "full_bar_count": 480,
                                    "suffix_bar_count": 320,
                                },
                                {
                                    "frequency": "30m",
                                    "converged": False,
                                    "full_bar_count": 480,
                                    "suffix_bar_count": 320,
                                },
                                {
                                    "frequency": "5m",
                                    "converged": True,
                                    "full_bar_count": 960,
                                    "suffix_bar_count": 640,
                                },
                            ],
                            "reason_codes": [
                                "D:WARMUP_TAIL_STABLE",
                                "30M:WARMUP_TAIL_DIVERGED",
                                "5M:WARMUP_TAIL_STABLE",
                            ],
                            "required_for_new_entry": True,
                        },
                    }
                ],
                "errors": [],
                "sector_exclusions": [],
                "sector_coverage_contract_id": SECTOR_COVERAGE_CONTRACT_ID,
                "coverage_manifest": {
                    "schema": COVERAGE_MANIFEST_SCHEMA,
                    "coverage_state_contract_id": COVERAGE_STATE_CONTRACT_ID,
                    "complete": True,
                    "coverage_epoch_id": coverage_epoch_id,
                    "signal_document_contract_id": (
                        SIGNAL_DOCUMENT_CONTRACT_ID
                    ),
                    "screening_policy_id": screening_policy_id,
                    "market_data_as_of": "2026-07-28T15:00:00+08:00",
                    "universe_revision": universe_revision,
                    "sector_catalog_revision": SECTOR_CATALOG_REVISION,
                    "sector_strength_evidence_revision": (
                        strength_batch.evidence_revision
                    ),
                    "discovered_codes": ["SH.600000"],
                    "completed_codes": ["SH.600000"],
                    "excluded_codes": [],
                    "failed_codes": [],
                    "exclusions": [],
                    "discarded_out_of_scope_retry_codes": [],
                    "pending_frequencies": {},
                    "backoff_frequencies": {},
                    "deferred_frequencies": {},
                },
                "snapshot_content_sha256": "sha256:" + "e" * 64,
                "scan_audit": {
                    "coverage_cycle_complete": True,
                    "coverage_cycle_completion_ratio": "1",
                    "coverage_cycle_resolution_ratio": "1",
                    "sector_discovered_count": 1,
                    "sector_completed_count": 1,
                    "sector_excluded_count": 0,
                    "sector_failed_count": 0,
                    "sector_resolved_count": 1,
                    "sector_completion_ratio": "1",
                    "sector_resolution_ratio": "1",
                    "sector_failure_counts": {},
                    "sector_exclusion_counts": {},
                    "selected_sector_count": 1,
                    "discovered_symbol_count": 1,
                    "coverage_cycle_attempted_symbol_count": 1,
                    "immediate_pending_symbol_count": 0,
                    "pending_symbol_count": 0,
                    "retry_symbol_count": 0,
                    "backoff_retry_symbol_count": 0,
                    "next_epoch_retry_symbol_count": 0,
                    "stock_failure_counts": {},
                    "stock_exclusion_counts": {},
                    "monitor_instrument_exclusion_count": 0,
                    "full_market_history_scan": False,
                    "coverage_cycle_completed_symbol_count": 1,
                    "coverage_cycle_excluded_symbol_count": 0,
                    "coverage_cycle_failed_symbol_count": 0,
                    "coverage_cycle_resolved_symbol_count": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    source_payload = json.loads(source.read_text(encoding="utf-8"))
    for signal in source_payload["signals"]:
        signal["decision_document_schema"] = (
            SIGNAL_DECISION_DOCUMENT_SCHEMA
        )
        signal["entry_execution_boundary"] = None
        signal["decision_document_id"] = signal_decision_document_id(signal)
    source_payload["snapshot_content_sha256"] = (
        subject.live_screening_snapshot_content_sha256(source_payload)
    )
    source.write_text(json.dumps(source_payload), encoding="utf-8")
    parameters = (
        Path(__file__).resolve().parents[2]
        / "audit"
        / "chanlun_trading_system_backtest"
        / "recent_year_current_sector_no3p"
        / "parameter_snapshot_human_review.json"
    )
    args = subject.parser().parse_args(
        (
            "--root",
            str(tmp_path / "forward"),
            "--parameter-snapshot",
            str(parameters),
            "--live-screening-snapshot",
            str(source),
            "--human-feedback-ledger",
            str(tmp_path / "feedback.json"),
            "evaluate",
        )
    )

    document, evidence = subject._archive_live_screening_snapshot(
        args=args,
        session=SESSION,
        expected_sector_catalog=_expected_sector_catalog(),
    )

    assert document["candidate_count"] == 1
    assert document["orders_created"] == 0
    assert document["fills_created"] == 0
    assert document["live_status"] == "LIVE_DISABLED"
    assert document["market_data_as_of"] == "2026-07-28T15:00:00+08:00"
    assert document["screening_policy_id"] == subject.sha256_json(
        screening_policy
    )
    assert document["content_sha256"].startswith("sha256:")
    assert evidence["candidate_count"] == 1
    assert Path(str(evidence["result"])).is_file()
    review_path = Path(str(evidence["human_review_result"]))
    assert review_path.is_file()
    assert review_path.parent.name == "forward_human_review_screen"
    assert Path(str(evidence["latest_result"])).name == (
        "forward_live_screening_snapshot.json"
    )
    assert Path(str(evidence["latest_human_review_result"])).name == (
        "forward_human_review_screen.json"
    )
    review = json.loads(review_path.read_text(encoding="utf-8"))
    assert not hasattr(subject, "_live_signal_human_review_alert")
    assert not hasattr(subject, "_forward_human_review_document")
    assert document["source_content_sha256"] == source_payload[
        "snapshot_content_sha256"
    ]
    assert document["source_payload_sha256"] == subject.sha256_json(
        source_payload
    )
    assert review["input_hashes"]["live_screening_snapshot"] == (
        source_payload["snapshot_content_sha256"]
    )
    expected_review = subject.live_human_review_document(
        live_snapshot=source_payload,
        source_snapshot_sha256=source_payload["snapshot_content_sha256"],
        session=SESSION,
        result_label="FORWARD_STAGED_LIVE_HUMAN_REVIEW_QUEUE",
        decision_source_snapshot=subject.current_decision_source_snapshot(
            subject.PROJECT_ROOT
        ),
    )
    assert review == expected_review
    assert review["candidate_funnel"]["review_candidate_count"] == 1
    assert review["review_queue"][0]["alert_type"] == "POSSIBLE_30M_BUY"
    assert review["review_queue"][0]["live_status"] == "LIVE_DISABLED"
    assert review["input_hashes"]["screening_policy_id"] == document[
        "screening_policy_id"
    ]
    assert decision_source_snapshot_matches_current(
        review["decision_source_snapshot"]
    )
    assert review["input_hashes"]["decision_source_snapshot_id"] == (
        decision_source_snapshot_id(review["decision_source_snapshot"])
    )
    assert review["orders_created"] == review["fills_created"] == 0
    assert evidence["human_review_candidate_count"] == 1
    assert evidence["human_review_content_sha256"] == review["content_sha256"]
    assert evidence["screening_policy_id"] == document["screening_policy_id"]
    assert evidence["promoted_screening_policy_id"] == document[
        "screening_policy_id"
    ]
    assert evidence["decision_core_id"] == document["decision_core_id"]
    assert evidence["promoted_decision_core_id"] == document[
        "decision_core_id"
    ]
    assert document["sector_catalog_revision"] == SECTOR_CATALOG_REVISION
    assert evidence["sector_catalog_revision"] == SECTOR_CATALOG_REVISION
    assert document["sector_strength_evidence_revision"] == (
        strength_batch.evidence_revision
    )
    assert evidence["sector_strength_evidence_revision"] == (
        strength_batch.evidence_revision
    )

    with pytest.raises(
        RuntimeError,
        match="does not match same-session QMT capture",
    ):
        subject._archive_live_screening_snapshot(
            args=args,
            session=SESSION,
            expected_sector_catalog=_expected_sector_catalog(
                revision="sha256:" + "f" * 64
            ),
        )

    with pytest.raises(RuntimeError, match="strength members do not match"):
        subject._archive_live_screening_snapshot(
            args=args,
            session=SESSION,
            expected_sector_catalog=_expected_sector_catalog(
                members=("SZ.000001",)
            ),
        )

    unsigned = json.loads(source.read_text(encoding="utf-8"))
    unsigned["signals"][0]["decision_reasons"].append("UNSIGNED_MUTATION")
    source.write_text(json.dumps(unsigned), encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="live screening snapshot review boundary is incomplete",
    ):
        subject._archive_live_screening_snapshot(
            args=args,
            session=SESSION,
            expected_sector_catalog=_expected_sector_catalog(),
        )
    source.write_text(json.dumps(document["snapshot"]), encoding="utf-8")

    low_coverage = json.loads(source.read_text(encoding="utf-8"))
    low_coverage["scan_audit"]["coverage_cycle_completion_ratio"] = "0.74"
    low_coverage["snapshot_content_sha256"] = (
        subject.live_screening_snapshot_content_sha256(low_coverage)
    )
    source.write_text(json.dumps(low_coverage), encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="live screening snapshot review boundary is incomplete",
    ):
        subject._archive_live_screening_snapshot(
            args=args,
            session=SESSION,
            expected_sector_catalog=_expected_sector_catalog(),
        )
    source.write_text(json.dumps(document["snapshot"]), encoding="utf-8")

    foreign_policy = json.loads(source.read_text(encoding="utf-8"))
    foreign_policy["screening_policy_id"] = "sha256:" + "0" * 64
    source.write_text(json.dumps(foreign_policy), encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="live screening snapshot review boundary is incomplete",
    ):
        subject._archive_live_screening_snapshot(
            args=args,
            session=SESSION,
            expected_sector_catalog=_expected_sector_catalog(),
        )
    source.write_text(json.dumps(document["snapshot"]), encoding="utf-8")

    # A full coverage manifest is not enough for the 15:20 daily sample when
    # that coverage epoch froze before the 15:00 close.
    preclose = json.loads(source.read_text(encoding="utf-8"))
    preclose["as_of"] = "2026-07-28T14:35:00+08:00"
    preclose["market_data_as_of"] = "2026-07-28T14:35:00+08:00"
    preclose["snapshot_content_sha256"] = (
        subject.live_screening_snapshot_content_sha256(preclose)
    )
    source.write_text(json.dumps(preclose), encoding="utf-8")
    with pytest.raises(
        RuntimeError,
        match="live screening snapshot review boundary is incomplete",
    ):
        subject._archive_live_screening_snapshot(
            args=args,
            session=SESSION,
            expected_sector_catalog=_expected_sector_catalog(),
        )
    source.write_text(json.dumps(document["snapshot"]), encoding="utf-8")

    # Upgrade a real v1 manifest in place without turning the same immutable
    # evaluation into a duplicate attempt.  The policy can be attested from the
    # already content-addressed live object.
    legacy_manifest_path = (
        tmp_path
        / "forward"
        / "sessions"
        / SESSION.isoformat()
        / "forward_session_manifest.json"
    )
    legacy_manifest = json.loads(legacy_manifest_path.read_text(encoding="utf-8"))
    legacy_attempt_id = legacy_manifest["promoted_attempt_id"]
    legacy_manifest["attempts"][0].pop("screening_policy_id")
    legacy_manifest["attempts"][0].pop("decision_core_id")
    legacy_manifest.pop("promoted_screening_policy_id")
    legacy_manifest.pop("promoted_decision_core_id")
    legacy_stable = {
        key: value
        for key, value in legacy_manifest.items()
        if key != "content_sha256"
    }
    legacy_manifest["content_sha256"] = subject.sha256_json(legacy_stable)
    legacy_manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")

    first_archive = Path(str(evidence["result"])).read_bytes()
    first_review = review_path.read_bytes()
    repeated_document, repeated_evidence = subject._archive_live_screening_snapshot(
        args=args,
        session=SESSION,
        expected_sector_catalog=_expected_sector_catalog(),
    )
    assert repeated_document == document
    assert Path(str(evidence["result"])).read_bytes() == first_archive
    assert review_path.read_bytes() == first_review
    assert repeated_evidence["result_sha256"] == evidence["result_sha256"]
    assert (
        repeated_evidence["human_review_result_sha256"]
        == evidence["human_review_result_sha256"]
    )
    assert repeated_evidence["attempt_id"] == evidence["attempt_id"]
    assert repeated_evidence["promoted_sample"] is True
    migrated_manifest = json.loads(
        legacy_manifest_path.read_text(encoding="utf-8")
    )
    assert migrated_manifest["attempt_count"] == 1
    assert migrated_manifest["promoted_attempt_id"] == legacy_attempt_id
    assert migrated_manifest["attempts"][0]["screening_policy_id"] == document[
        "screening_policy_id"
    ]
    assert migrated_manifest["promoted_screening_policy_id"] == document[
        "screening_policy_id"
    ]
    assert migrated_manifest["attempts"][0]["decision_core_id"] == document[
        "decision_core_id"
    ]
    assert migrated_manifest["promoted_decision_core_id"] == document[
        "decision_core_id"
    ]

    # Rehashing the outer manifest cannot change an existing attempt while
    # retaining its old attempt_id.
    tampered_manifest = json.loads(
        legacy_manifest_path.read_text(encoding="utf-8")
    )
    tampered_manifest["attempts"][0]["candidate_count"] += 1
    tampered_stable = {
        key: value
        for key, value in tampered_manifest.items()
        if key != "content_sha256"
    }
    tampered_manifest["content_sha256"] = subject.sha256_json(
        tampered_stable
    )
    legacy_manifest_path.write_text(
        json.dumps(tampered_manifest),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="attempt identity changed"):
        subject._load_session_manifest(
            legacy_manifest_path,
            session=SESSION,
            contract_id=str(document["contract_id"]),
            strategy_parameter_set_id=str(
                document["strategy_parameter_set_id"]
            ),
        )
    legacy_manifest_path.write_text(
        json.dumps(migrated_manifest),
        encoding="utf-8",
    )

    # Even a completely rehashed manifest/attempt cannot bless a review
    # object whose inner content hash was left stale after a semantic edit.
    forged_manifest = json.loads(
        legacy_manifest_path.read_text(encoding="utf-8")
    )
    forged_attempt = forged_manifest["attempts"][0]
    session_root = legacy_manifest_path.parent
    original_review = session_root / forged_attempt["human_review_object"][
        "path"
    ]
    forged_review = json.loads(original_review.read_text(encoding="utf-8"))
    forged_review["event_study"]["summary"]["forged"] = True
    forged_path = session_root / "objects" / "forward_human_review_screen" / (
        "forged-stale-content-hash.json"
    )
    forged_path.write_text(json.dumps(forged_review), encoding="utf-8")
    forged_attempt["human_review_object"]["path"] = str(
        forged_path.relative_to(session_root)
    )
    forged_attempt["human_review_object"]["file_sha256"] = (
        subject.sha256_file(forged_path)
    )
    forged_attempt["attempt_id"] = subject.sha256_json(
        subject._forward_attempt_identity_document(forged_attempt)
    )
    forged_manifest["promoted_attempt_id"] = forged_attempt["attempt_id"]
    forged_stable = {
        key: value
        for key, value in forged_manifest.items()
        if key != "content_sha256"
    }
    forged_manifest["content_sha256"] = subject.sha256_json(forged_stable)
    legacy_manifest_path.write_text(
        json.dumps(forged_manifest),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="content identity changed"):
        subject._promoted_forward_review_reports(
            args=args,
            through_session=SESSION,
        )
    legacy_manifest_path.write_text(
        json.dumps(migrated_manifest),
        encoding="utf-8",
    )

    # A later same-session evaluation is an immutable attempt, not a second
    # daily sample and not an overwrite of the first event-addressed object.
    changed = json.loads(source.read_text(encoding="utf-8"))
    # Decision reasons are now derived evidence and cannot be edited merely to
    # manufacture a second signed attempt.  A symbol-name metadata refresh is
    # a valid semantic change that leaves the trading decision unchanged.
    changed["signals"][0]["name"] = "第二次同日评估"
    changed["snapshot_content_sha256"] = (
        subject.live_screening_snapshot_content_sha256(changed)
    )
    source.write_text(json.dumps(changed), encoding="utf-8")
    _changed_document, changed_evidence = subject._archive_live_screening_snapshot(
        args=args,
        session=SESSION,
        expected_sector_catalog=_expected_sector_catalog(),
    )
    changed_result = Path(str(changed_evidence["result"]))
    changed_review = Path(str(changed_evidence["human_review_result"]))
    assert changed_result != Path(str(evidence["result"]))
    assert changed_review != review_path
    assert Path(str(evidence["result"])).read_bytes() == first_archive
    assert review_path.read_bytes() == first_review
    assert changed_evidence["attempt_id"] != evidence["attempt_id"]
    assert changed_evidence["promoted_sample"] is False

    manifest_path = Path(str(changed_evidence["session_manifest"]))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["attempt_count"] == 2
    assert manifest["promoted_sample_count"] == 1
    assert manifest["promoted_attempt_id"] == evidence["attempt_id"]
    assert [value["attempt_id"] for value in manifest["attempts"]] == [
        evidence["attempt_id"],
        changed_evidence["attempt_id"],
    ]
    assert {value["screening_policy_id"] for value in manifest["attempts"]} == {
        document["screening_policy_id"]
    }
    assert manifest["promoted_screening_policy_id"] == document[
        "screening_policy_id"
    ]
    assert {value["decision_core_id"] for value in manifest["attempts"]} == {
        document["decision_core_id"]
    }
    assert manifest["promoted_decision_core_id"] == document[
        "decision_core_id"
    ]

    # A genuinely different screening policy is a separate immutable cohort,
    # but it still cannot replace the first valid daily sample.
    different_policy = json.loads(source.read_text(encoding="utf-8"))
    different_policy["screening_policy"]["adapter_revision"] = "v2"
    different_policy["screening_policy_id"] = subject.sha256_json(
        different_policy["screening_policy"]
    )
    different_policy["coverage_manifest"]["screening_policy_id"] = (
        different_policy["screening_policy_id"]
    )
    different_policy_epoch = screening_coverage_epoch_id(
        market_data_as_of=datetime.fromisoformat(
            str(different_policy["market_data_as_of"])
        ),
        universe_revision=str(
            different_policy["coverage_manifest"]["universe_revision"]
        ),
        sector_catalog_revision=str(
            different_policy["coverage_manifest"]["sector_catalog_revision"]
        ),
        sector_strength_evidence_revision=str(
            different_policy["sector_strength_evidence_revision"]
        ),
        decision_core_id=str(different_policy["decision_core_id"]),
        screening_policy_id=str(different_policy["screening_policy_id"]),
        structure_version=str(different_policy["structure_version"]),
        parameter_version=str(different_policy["parameter_version"]),
    )
    different_policy["coverage_epoch_id"] = different_policy_epoch
    different_policy["coverage_manifest"]["coverage_epoch_id"] = (
        different_policy_epoch
    )
    different_policy["snapshot_content_sha256"] = (
        subject.live_screening_snapshot_content_sha256(different_policy)
    )
    source.write_text(json.dumps(different_policy), encoding="utf-8")
    _policy_document, policy_evidence = subject._archive_live_screening_snapshot(
        args=args,
        session=SESSION,
        expected_sector_catalog=_expected_sector_catalog(),
    )
    assert policy_evidence["promoted_sample"] is False
    assert policy_evidence["screening_policy_id"] == different_policy[
        "screening_policy_id"
    ]
    assert policy_evidence["promoted_screening_policy_id"] == document[
        "screening_policy_id"
    ]
    policy_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert policy_manifest["attempt_count"] == 3
    assert policy_manifest["attempts"][-1]["screening_policy_id"] == (
        different_policy["screening_policy_id"]
    )
    assert policy_manifest["promoted_attempt_id"] == evidence["attempt_id"]
    assert policy_manifest["promoted_screening_policy_id"] == document[
        "screening_policy_id"
    ]
    assert policy_manifest["promoted_decision_core_id"] == document[
        "decision_core_id"
    ]

    reference_at = datetime.combine(SESSION, time(15), tzinfo=CN)
    frame = pd.DataFrame(
        {
            "date": [reference_at],
            "open": [10],
            "high": [10.1],
            "low": [9.9],
            "close": [10],
            "volume": [1000],
        }
    )
    frame.attrs["qmt_transport"] = "LOCAL_FIXED_RECORD_READ_ONLY"
    frame.attrs["qmt_local_cache_source_sha256"] = "sha256:" + "9" * 64
    monkeypatch.setattr(subject, "load_qmt_frame", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(
        subject,
        "_qmt_forward_review_factor_events",
        lambda **_kwargs: (),
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_trading_sessions",
        lambda **_kwargs: (SESSION,),
    )
    markout_session = SESSION + timedelta(days=1)

    def qualification(*, eligible: tuple[date, ...]) -> dict[str, object]:
        observed_at = datetime.combine(
            markout_session,
            time(15, 20),
            tzinfo=CN,
        )
        implementation_id = "sha256:" + "a" * 64
        capture_id = "sha256:" + "b" * 64
        data_ready_id = "sha256:" + "c" * 64
        evaluated_id = "sha256:" + "d" * 64
        qualified_evidence = []
        for value in eligible:
            delivery = {
                "schema": "chanlun-v3-forward-paper-session-delivery/v2",
                "session": value.isoformat(),
                "observed_at": observed_at.isoformat(),
                "required": True,
                "requirement_resolved": True,
                "trading_session_evidence_proven": True,
                "ready": True,
                "status": "ready",
                "reason_code": "READY",
                "capture_event_present": True,
                "data_ready_event_present": True,
                "evaluation_event_present": True,
                "capture_ready": True,
                "evaluation_ready": True,
                "capture_evidence_proven": True,
                "data_ready_evidence_proven": True,
                "evaluation_artifacts_proven": True,
                "implementation_provenance_present": True,
                "implementation_provenance_proven": True,
                "capture_implementation_provenance_id": implementation_id,
                "data_ready_implementation_provenance_id": implementation_id,
                "evaluation_implementation_provenance_id": implementation_id,
                "capture_event_sha256": capture_id,
                "data_ready_event_sha256": data_ready_id,
                "evaluation_event_sha256": evaluated_id,
                "latest_terminal_event_status": "EVALUATED",
                "latest_terminal_event_sha256": evaluated_id,
                "real_account_accessed": False,
                "real_order_transport_enabled": False,
                "paper_status": "REVIEW_REQUIRED",
                "live_status": "LIVE_DISABLED",
            }
            qualified_evidence.append(
                {
                    "session": value.isoformat(),
                    "delivery_audit": delivery,
                    "delivery_audit_content_sha256": subject.sha256_json(
                        delivery
                    ),
                }
            )
        stable: dict[str, object] = {
            "schema": "chanlun-v3-forward-review-session-qualification/v2",
            "through_session": markout_session.isoformat(),
            "observed_at": observed_at.isoformat(),
            "qualified_sessions": [value.isoformat() for value in eligible],
            "qualified_session_evidence": qualified_evidence,
            "excluded_sessions": [],
            "qualified_session_count": len(eligible),
            "excluded_session_count": 0,
            "current_session_excluded_until_terminal_event": True,
            "forward_ledger_content_sha256": "sha256:" + "e" * 64,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "live_status": "LIVE_DISABLED",
        }
        return {**stable, "content_sha256": subject.sha256_json(stable)}

    monkeypatch.setattr(
        subject,
        "_forward_review_session_qualification",
        lambda **_kwargs: qualification(eligible=(SESSION,)),
        raising=False,
    )
    markout = subject._forward_review_markout(args=args, session=markout_session)
    markout_report = json.loads(
        Path(str(markout["result"])).read_text(encoding="utf-8")
    )
    # The second immutable attempt remains auditable but cannot silently
    # replace the first promoted daily sample.
    assert markout["unique_lifecycle_count"] == 1
    assert markout_report["observations"][0]["source_report_content_sha256"] == (
        review["content_sha256"]
    )
    assert markout_report["portfolio_performance_evaluable"] is False
    assert markout_report["orders_created"] == markout_report["fills_created"] == 0
    assert markout_report["source_provenance_status"] == "COMPLETE"
    assert markout["source_session_qualification"]["qualified_sessions"] == [
        SESSION.isoformat()
    ]
    assert markout_report["source_session_qualification"] == markout[
        "source_session_qualification"
    ]
    lineage = markout["warmup_structure_lineage_rollup"]
    assert lineage["status"] == "NOT_RECORDED_LEGACY"
    assert lineage["qualified_session_count"] == 1
    assert lineage["recorded_session_count"] == 0
    assert lineage["legacy_session_count"] == 1
    assert lineage["structure_event_count"] == 0
    lineage_report = json.loads(
        Path(str(lineage["result"])).read_text(encoding="utf-8")
    )
    assert lineage_report["content_sha256"] == lineage["content_sha256"]
    assert lineage_report["sessions"][0]["recording_status"] == (
        "NOT_RECORDED_LEGACY"
    )

    # A content-valid promoted manifest is not a qualified forward sample when
    # its Capture/Data/Evaluate delivery chain failed or remains unattested.
    monkeypatch.setattr(
        subject,
        "_forward_review_session_qualification",
        lambda **_kwargs: qualification(eligible=()),
    )
    excluded = subject._forward_review_markout(
        args=args,
        session=markout_session,
    )
    excluded_report = json.loads(
        Path(str(excluded["result"])).read_text(encoding="utf-8")
    )
    assert excluded["unique_lifecycle_count"] == 0
    assert excluded_report["sample"]["unique_lifecycle_count"] == 0
    assert excluded_report["source_session_qualification"] == excluded[
        "source_session_qualification"
    ]
    excluded_lineage = excluded["warmup_structure_lineage_rollup"]
    assert excluded_lineage["status"] == "NO_QUALIFIED_SESSIONS"
    assert excluded_lineage["qualified_session_count"] == 0


def test_forward_markout_qualification_requires_prior_ready_delivery(
    tmp_path: Path,
    monkeypatch,
) -> None:
    through_session = SESSION + timedelta(days=1)
    root = tmp_path / "forward"
    for value in (SESSION, through_session):
        session_root = root / "sessions" / value.isoformat()
        session_root.mkdir(parents=True)
        (session_root / "forward_session_manifest.json").write_text(
            "{}",
            encoding="utf-8",
        )
    (root / "forward_paper_ledger.json").write_text("{}", encoding="utf-8")
    args = SimpleNamespace(
        root=root,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        sector_ledger=tmp_path / "sector_ledger.json",
        trading_calendar=tmp_path / "calendar.json",
    )
    monkeypatch.setattr(
        subject,
        "load_forward_paper_ledger",
        lambda *_args, **_kwargs: {
            "events": ({"event": "test"},),
            "content_sha256": "sha256:" + "1" * 64,
        },
    )
    monkeypatch.setattr(
        subject,
        "authoritative_trading_session_evidence",
        lambda **_kwargs: {"classification": "TRADING_SESSION"},
    )
    monkeypatch.setattr(
        subject,
        "audit_forward_sector_capture_readiness",
        lambda **_kwargs: {"ready": True},
    )
    def delivery(*, ready: bool) -> dict[str, object]:
        implementation_id = "sha256:" + "2" * 64
        capture_id = "sha256:" + "3" * 64
        data_ready_id = "sha256:" + "4" * 64
        evaluated_id = "sha256:" + "5" * 64
        return {
            "schema": "chanlun-v3-forward-paper-session-delivery/v2",
            "session": SESSION.isoformat(),
            "observed_at": datetime.combine(
                through_session,
                time(15, 20),
                tzinfo=CN,
            ).isoformat(),
            "required": True,
            "requirement_resolved": True,
            "trading_session_evidence_proven": True,
            "ready": ready,
            "status": "ready" if ready else "not_ready",
            "reason_code": (
                "READY" if ready else "IMPLEMENTATION_PROVENANCE_UNATTESTED"
            ),
            "capture_event_present": True,
            "data_ready_event_present": True,
            "evaluation_event_present": True,
            "capture_ready": ready,
            "evaluation_ready": ready,
            "capture_evidence_proven": ready,
            "data_ready_evidence_proven": ready,
            "evaluation_artifacts_proven": ready,
            "implementation_provenance_present": ready,
            "implementation_provenance_proven": ready,
            "capture_implementation_provenance_id": (
                implementation_id if ready else None
            ),
            "data_ready_implementation_provenance_id": (
                implementation_id if ready else None
            ),
            "evaluation_implementation_provenance_id": (
                implementation_id if ready else None
            ),
            "capture_event_sha256": capture_id,
            "data_ready_event_sha256": data_ready_id,
            "evaluation_event_sha256": evaluated_id,
            "latest_terminal_event_status": "EVALUATED",
            "latest_terminal_event_sha256": evaluated_id,
            "real_account_accessed": False,
            "real_order_transport_enabled": False,
            "paper_status": "REVIEW_REQUIRED",
            "live_status": "LIVE_DISABLED",
        }

    monkeypatch.setattr(
        subject,
        "audit_forward_paper_session_delivery",
        lambda *_args, **_kwargs: delivery(ready=False),
    )

    rejected = subject._forward_review_session_qualification(
        args=args,
        through_session=through_session,
    )
    assert rejected["qualified_sessions"] == []
    assert [value["reason_code"] for value in rejected["excluded_sessions"]] == [
        "IMPLEMENTATION_PROVENANCE_UNATTESTED",
        "CURRENT_SESSION_TERMINAL_EVENT_PENDING",
    ]
    assert rejected["excluded_sessions"][0]["delivery_audit"] == delivery(
        ready=False
    )
    assert subject._qualified_forward_review_session_dates(
        rejected,
        through_session=through_session,
    ) == frozenset()

    monkeypatch.setattr(
        subject,
        "audit_forward_paper_session_delivery",
        lambda *_args, **_kwargs: delivery(ready=True),
    )
    accepted = subject._forward_review_session_qualification(
        args=args,
        through_session=through_session,
    )
    assert accepted["qualified_sessions"] == [SESSION.isoformat()]
    assert accepted["qualified_session_count"] == 1
    assert accepted["qualified_session_evidence"][0]["delivery_audit"] == (
        delivery(ready=True)
    )
    assert accepted["excluded_sessions"] == [
        {
            "session": through_session.isoformat(),
            "reason_code": "CURRENT_SESSION_TERMINAL_EVENT_PENDING",
        }
    ]
    assert subject._qualified_forward_review_session_dates(
        accepted,
        through_session=through_session,
    ) == frozenset((SESSION,))
    assert subject._forward_review_session_qualification(
        args=args,
        through_session=through_session,
    ) == accepted

    forged = dict(accepted)
    forged["qualified_session_count"] = 2
    with pytest.raises(RuntimeError, match="session qualification is invalid"):
        subject._qualified_forward_review_session_dates(
            forged,
            through_session=through_session,
        )


def test_forward_markout_prices_neutralize_known_ex_date_jump(
    monkeypatch,
) -> None:
    first_review = datetime.combine(
        SESSION - timedelta(days=1),
        time(10),
        tzinfo=CN,
    )
    frame = pd.DataFrame(
        {
            "date": (
                datetime.combine(
                    SESSION - timedelta(days=1), time(15), tzinfo=CN
                ),
                datetime.combine(SESSION, time(15), tzinfo=CN),
            ),
            "open": (10, 5),
            "high": (10, 5),
            "low": (10, 5),
            "close": (10, 5),
            "volume": (1000, 1000),
        }
    )
    frame.attrs["qmt_transport"] = "LOCAL_FIXED_RECORD_READ_ONLY"
    frame.attrs["qmt_local_cache_source_sha256"] = "sha256:" + "d" * 64
    event = QmtCausalFactorEvent(
        code="SH.600000",
        effective_on=SESSION,
        interest=Decimal("0"),
        stock_bonus=Decimal("0"),
        stock_gift=Decimal("1"),
        allot_num=Decimal("0"),
        allot_price=Decimal("0"),
        gugai=Decimal("0"),
        raw_price_divisor=Decimal("2"),
    )
    monkeypatch.setattr(subject, "load_qmt_frame", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(
        subject,
        "_qmt_forward_review_factor_events",
        lambda **_kwargs: (event,),
    )
    sample = SimpleNamespace(
        alert=SimpleNamespace(
            symbol="SH.600000",
            review_available_at=first_review,
        )
    )

    bars_by_symbol, audits = subject._forward_review_price_bars(
        samples=(sample,),
        through_session=SESSION,
    )

    assert tuple(bar.close for bar in bars_by_symbol["SH.600000"]) == (
        Decimal("10.0"),
        Decimal("10.0"),
    )
    audit = audits["SH.600000"]
    assert audit["status"] == "AVAILABLE"
    assert audit["factor_contract_id"] == (
        QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
    )
    assert audit["factor_event_count"] == 1
    assert str(audit["factor_revision"]).startswith("sha256:")
    assert audit["factor_events"][0]["effective_on"] == SESSION.isoformat()
    assert str(audit["raw_bar_revision"]).startswith("sha256:")
    assert str(audit["adjusted_bar_revision"]).startswith("sha256:")
    assert audit["raw_bar_revision"] != audit["adjusted_bar_revision"]


def test_forward_markout_normalizes_qmt_opening_event_to_exact_240_grid(
    monkeypatch,
) -> None:
    closes = a_share_completed_one_minute_closes(SESSION)
    opening = datetime.combine(SESSION, time(9, 30), tzinfo=CN)
    frame = pd.DataFrame(
        {
            "date": (opening, *closes),
            "open": [10.0] * 241,
            "high": [10.2] * 241,
            "low": [9.8] * 241,
            "close": [10.1] * 241,
            "volume": [100.0] * 241,
        }
    )
    frame.loc[0, ["open", "high", "low", "close", "volume"]] = (
        9.9,
        10.4,
        9.7,
        10.0,
        50.0,
    )
    frame.attrs["qmt_transport"] = "LOCAL_FIXED_RECORD_READ_ONLY"
    frame.attrs["qmt_local_cache_source_sha256"] = "sha256:" + "f" * 64
    monkeypatch.setattr(subject, "load_qmt_frame", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(
        subject,
        "_qmt_forward_review_factor_events",
        lambda **_kwargs: (),
    )
    sample = SimpleNamespace(
        alert=SimpleNamespace(
            symbol="SH.600000",
            review_available_at=datetime.combine(SESSION, time(10), tzinfo=CN),
        )
    )

    bars_by_symbol, audits = subject._forward_review_price_bars(
        samples=(sample,),
        through_session=SESSION,
    )

    bars = bars_by_symbol["SH.600000"]
    assert len(bars) == 240
    assert bars[0].observed_at == closes[0]
    assert all(bar.observed_at.time() != time(9, 30) for bar in bars)
    assert bars[0].high == Decimal("10.4")
    assert bars[0].low == Decimal("9.7")
    audit = audits["SH.600000"]
    assert audit["raw_row_count"] == 241
    assert audit["normalized_row_count"] == 240
    assert audit["opening_event_normalization"] == (
        subject.QMT_COMPLETED_ONE_MINUTE_GRID_REVISION
    )
    assert audit["source_audit_contract_id"] == (
        subject.FORWARD_REVIEW_SOURCE_AUDIT_CONTRACT_ID
    )


def test_forward_markout_factor_failure_never_falls_back_to_raw_prices(
    monkeypatch,
) -> None:
    first_review = datetime.combine(SESSION, time(10), tzinfo=CN)
    frame = pd.DataFrame(
        {
            "date": [first_review],
            "open": [10],
            "high": [10],
            "low": [10],
            "close": [10],
            "volume": [1000],
        }
    )
    monkeypatch.setattr(subject, "load_qmt_frame", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(
        subject,
        "_qmt_forward_review_factor_events",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("factor unavailable")),
    )
    sample = SimpleNamespace(
        alert=SimpleNamespace(
            symbol="SH.600000",
            review_available_at=first_review,
        )
    )

    bars_by_symbol, audits = subject._forward_review_price_bars(
        samples=(sample,),
        through_session=SESSION,
    )

    assert bars_by_symbol["SH.600000"] == ()
    assert audits["SH.600000"]["status"] == "UNAVAILABLE"
    assert "factor unavailable" in str(audits["SH.600000"]["reason"])


def test_daily_valuation_cash_only_is_promoted_once_and_reused(
    monkeypatch,
    tmp_path: Path,
) -> None:
    captured = datetime.combine(SESSION, time(15, 20), tzinfo=CN)
    monkeypatch.setattr(subject, "_now", lambda: captured)
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **_kwargs: pytest.fail("cash-only valuation must not read a symbol"),
    )
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=tmp_path / "paper.json",
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )

    first = subject._capture_human_paper_valuation(args=args, session=SESSION)
    second = subject._capture_human_paper_valuation(args=args, session=SESSION)

    assert first["status"] == "VALUATION_COMPLETE"
    assert first["equity"] == "1000000.00"
    assert first["performance_evaluable"] is False
    assert first["reused"] is False
    assert second["reused"] is True
    assert second["valuation_content_sha256"] == first["valuation_content_sha256"]
    alias = (
        tmp_path
        / "forward"
        / "sessions"
        / SESSION.isoformat()
        / "paper_valuation.json"
    )
    assert alias.is_file()
    objects = tuple((alias.parent / "objects" / "paper_valuation").glob("*.json"))
    assert len(objects) == 1


def test_daily_valuation_marks_open_position_from_exact_1500_bar(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fill = {
        "kind": "FILL",
        "payload": {
            "symbol": "SH.600000",
            "side": "BUY",
            "quantity": 100,
            "price": "10.00",
            "filled_at": "2026-07-28T10:01:00+08:00",
        },
    }
    paper = {
        "events": (fill,),
        "content_sha256": "sha256:" + "7" * 64,
    }
    monkeypatch.setattr(subject, "load_human_paper_ledger", lambda _path: paper)
    monkeypatch.setattr(
        subject,
        "audit_human_paper_execution_evidence",
        lambda *_args, **_kwargs: {"status": "COMPLETE"},
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            **_execution_fact(kwargs["symbol"]),
            "native_code": subject.qmt_native_code(kwargs["symbol"]),
            "limit_up": "12",
        },
    )
    frame = pd.DataFrame(
        {
            "date": [datetime.combine(SESSION, time(15), tzinfo=CN)],
            "open": [10.9],
            "high": [11.1],
            "low": [10.8],
            "close": [11.0],
            "volume": [8000],
        }
    )
    frame.attrs["qmt_transport"] = "LOCAL_FIXED_RECORD_READ_ONLY"
    frame.attrs["qmt_local_cache_source_sha256"] = "sha256:" + "8" * 64
    monkeypatch.setattr(subject, "load_qmt_frame", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(
        subject,
        "validate_human_paper_valuation_sources",
        lambda payload, **_kwargs: dict(payload),
    )
    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=tmp_path / "paper.json",
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )

    result = subject._capture_human_paper_valuation(args=args, session=SESSION)

    assert result["status"] == "VALUATION_COMPLETE"
    assert result["complete_mark_count"] == 1
    assert result["cash_balance"] == "998994.99"
    assert result["market_value"] == "1100.00"
    assert result["equity"] == "1000094.99"
    document = json.loads(Path(str(result["valuation_snapshot"])).read_text())
    assert document["marks"][0]["closed_at"] == "2026-07-28T15:00:00+08:00"
    assert document["marks"][0]["qmt_transport"] == "LOCAL_FIXED_RECORD_READ_ONLY"
    assert document["minimum_market_data_frequency"] == "1m"
    assert document["tick_data_used"] is False


def test_incomplete_daily_valuation_is_immutable_but_not_promoted(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fill = {
        "kind": "FILL",
        "payload": {
            "symbol": "SH.600000",
            "side": "BUY",
            "quantity": 100,
            "price": "10.00",
            "filled_at": "2026-07-28T10:01:00+08:00",
        },
    }
    monkeypatch.setattr(
        subject,
        "load_human_paper_ledger",
        lambda _path: {
            "events": (fill,),
            "content_sha256": "sha256:" + "6" * 64,
        },
    )
    monkeypatch.setattr(
        subject,
        "audit_human_paper_execution_evidence",
        lambda *_args, **_kwargs: {"status": "COMPLETE"},
    )
    monkeypatch.setattr(
        subject,
        "_qmt_human_paper_execution_fact",
        lambda **kwargs: {
            "symbol": kwargs["symbol"],
            "session": SESSION.isoformat(),
            "trading_day": SESSION.isoformat(),
            "suspended": False,
            "expired": False,
            "expiry_date": None,
            "corporate_actions": [],
            "tick_data_used": False,
            "account_api_used": False,
        },
    )
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: pd.DataFrame(
            columns=("date", "open", "high", "low", "close", "volume")
        ),
    )
    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(15, 20), tzinfo=CN),
    )
    args = SimpleNamespace(
        root=tmp_path / "forward",
        human_paper_ledger=tmp_path / "paper.json",
        parameter_snapshot=PARAMETER_SNAPSHOT,
    )

    result = subject._capture_human_paper_valuation(args=args, session=SESSION)

    assert result["status"] == "VALUATION_INCOMPLETE"
    assert result["equity"] is None
    assert result["error_count"] == 1
    assert Path(str(result["valuation_object"])).is_file()
    assert result["valuation_snapshot"] is None
    assert not (
        tmp_path
        / "forward"
        / "sessions"
        / SESSION.isoformat()
        / "paper_valuation.json"
    ).exists()


def test_evaluate_blocks_before_market_reads_when_implementation_changed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    args = subject.parser().parse_args(
        (
            "--root",
            str(tmp_path / "forward"),
            "--parameter-snapshot",
            str(tmp_path / "parameters.json"),
            "--sector-ledger",
            str(tmp_path / "sector.json"),
            "--qmt-local-data-dir",
            str(tmp_path / "qmt"),
            "--session",
            SESSION.isoformat(),
            "evaluate",
        )
    )
    monkeypatch.setattr(
        subject,
        "load_frozen_forward_contract",
        lambda _path: SimpleNamespace(technical_mode="HUMAN_REVIEW_SCREENING"),
    )
    continuity = {
        **_ready_implementation_continuity(),
        "ready": False,
        "status": "not_ready",
        "reason_code": "IMPLEMENTATION_CHANGED_SINCE_CAPTURE",
        "market_data_read_authorized": False,
    }
    monkeypatch.setattr(
        subject,
        "_forward_implementation_continuity",
        lambda **_kwargs: continuity,
    )
    monkeypatch.setattr(
        subject,
        "_market_data_gate",
        lambda **_kwargs: pytest.fail("market data must not be read"),
    )
    appended: list[tuple[str, str, dict[str, object]]] = []

    def fake_append(*, phase, status, evidence, **_kwargs):
        appended.append((phase, status, dict(evidence)))
        return {}, {"phase": phase, "status": status}, False

    monkeypatch.setattr(subject, "_append", fake_append)
    monkeypatch.setattr(subject, "_print", lambda _value: None)

    assert subject._evaluate(args) == 7
    assert [(phase, status) for phase, status, _ in appended] == [
        ("DECISION", "EVALUATION_BLOCKED")
    ]
    evidence = appended[0][2]
    assert evidence["failed_step"] == "implementation_continuity_preflight"
    assert evidence["implementation_continuity"] == continuity
    assert evidence["market_data_read"] is False
    assert evidence["pipeline_started"] is False


def test_human_review_evaluate_uses_frozen_evaluated_status(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """A live-screen archive is evidence for EVALUATED, not a new ledger state."""

    args = subject.parser().parse_args(
        (
            "--root",
            str(tmp_path / "forward"),
            "--parameter-snapshot",
            str(tmp_path / "parameters.json"),
            "--sector-ledger",
            str(tmp_path / "sector.json"),
            "--pit-snapshot",
            str(tmp_path / "pit.json"),
            "--qmt-local-data-dir",
            str(tmp_path / "qmt"),
            "--session",
            SESSION.isoformat(),
            "evaluate",
        )
    )
    monkeypatch.setattr(
        subject,
        "_forward_implementation_continuity",
        lambda **_kwargs: _ready_implementation_continuity(),
    )
    monkeypatch.setattr(
        subject,
        "load_frozen_forward_contract",
        lambda _path: SimpleNamespace(technical_mode="HUMAN_REVIEW_SCREENING"),
    )
    accounting_parameters = subject.load_human_paper_accounting_parameters(
        PARAMETER_SNAPSHOT
    )
    monkeypatch.setattr(
        subject,
        "load_human_paper_accounting_parameters",
        lambda _path: accounting_parameters,
    )
    monkeypatch.setattr(
        subject,
        "audit_forward_sector_capture_readiness",
        lambda **_kwargs: _ready_sector_capture(),
    )
    monkeypatch.setattr(subject, "sha256_file", lambda _path: "sha256:" + "c" * 64)
    monkeypatch.setattr(
        subject,
        "_market_data_gate",
        lambda **_kwargs: {"complete": True, "reason_codes": ()},
    )
    monkeypatch.setattr(
        subject,
        "_session_pit_snapshot",
        lambda **_kwargs: (tmp_path / "pit.json", {}, None),
    )
    archive_calls: list[dict[str, object]] = []

    def fake_archive(**kwargs):
        archive_calls.append(dict(kwargs))
        return (
            {
                "schema": "chanlun-v3-forward-live-screening-snapshot/v1",
                "candidate_count": 2,
                "scanner_error_count": 0,
            },
            {"source_mode": "STAGED_LIVE_SCREENING_ARCHIVE"},
        )

    monkeypatch.setattr(subject, "_archive_live_screening_snapshot", fake_archive)
    status_capture_calls: list[dict[str, object]] = []

    def fake_status_capture(**kwargs):
        status_capture_calls.append(dict(kwargs))
        return {
            "schema": "chanlun-qmt-instrument-status-snapshot/v1",
            "all_complete": True,
            "future_consumer_connected": False,
            "same_session_decision_adjudication_allowed": False,
            "historical_backfill_allowed": False,
            "live_status": "LIVE_DISABLED",
        }

    monkeypatch.setattr(
        subject,
        "_capture_forward_screening_instrument_status",
        fake_status_capture,
    )
    monkeypatch.setattr(
        subject,
        "_capture_human_paper_valuation",
        lambda **_kwargs: {
            "status": "VALUATION_COMPLETE",
            "equity_curve_point_available": True,
            "performance_evaluable": False,
            "live_status": "LIVE_DISABLED",
        },
    )
    calls: list[tuple[str, str]] = []
    decision_evidence: list[dict[str, object]] = []

    def fake_append(*, phase, status, evidence, **_kwargs):
        calls.append((phase, status))
        if phase == "DECISION":
            decision_evidence.append(dict(evidence))
        return {}, {"phase": phase, "status": status}, False

    monkeypatch.setattr(subject, "_append", fake_append)
    monkeypatch.setattr(subject, "_print", lambda _value: None)

    assert subject._evaluate(args) == 0
    assert calls == [("DATA_GATE", "DATA_READY"), ("DECISION", "EVALUATED")]
    assert archive_calls[0]["expected_sector_catalog"][
        "catalog_revision"
    ] == SECTOR_CATALOG_REVISION
    assert status_capture_calls[0]["sector_catalog"][
        "ledger_entry_sha256"
    ] == "sha256:" + "a" * 64
    assert decision_evidence[0]["qmt_instrument_status_snapshot"][
        "same_session_decision_adjudication_allowed"
    ] is False


def test_forward_status_snapshot_is_limited_to_screening_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    class Result:
        @staticmethod
        def evidence() -> dict[str, object]:
            return {
                "schema": "chanlun-qmt-instrument-status-snapshot/v1",
                "all_complete": True,
            }

    def fake_capture(**kwargs):
        calls.append(dict(kwargs))
        return Result()

    monkeypatch.setattr(
        subject,
        "capture_qmt_instrument_status_snapshot",
        fake_capture,
    )
    args = SimpleNamespace(root=tmp_path / "forward")
    evidence = subject._capture_forward_screening_instrument_status(
        args=args,
        session=SESSION,
        archived_screen={
            "source_content_sha256": "sha256:" + "2" * 64,
            "snapshot": {
                "signals": [
                    {"code": "SZ.000001"},
                    {"code": "SH.600000"},
                    {"code": "SZ.000001"},
                ]
            },
        },
        sector_catalog={"ledger_entry_sha256": "sha256:" + "1" * 64},
    )

    assert calls[0]["symbols"] == (
        "SZ.000001",
        "SH.600000",
        "SZ.000001",
    )
    assert calls[0]["output"] == (
        (tmp_path / "forward").resolve()
        / "sessions"
        / SESSION.isoformat()
        / "qmt_instrument_status_snapshot.json"
    )
    assert evidence["coverage_scope"] == "SCREENING_SIGNAL_SYMBOLS_ONLY"
    assert evidence["can_explain_same_session_decision"] is False
    assert evidence["can_explain_prior_historical_session"] is False
    assert evidence["future_consumer_connected"] is False


def test_human_review_evaluate_rejects_capture_without_immutable_receipt(
    monkeypatch,
    tmp_path: Path,
) -> None:
    args = subject.parser().parse_args(
        (
            "--root",
            str(tmp_path / "forward"),
            "--parameter-snapshot",
            str(tmp_path / "parameters.json"),
            "--sector-ledger",
            str(tmp_path / "sector.json"),
            "--pit-snapshot",
            str(tmp_path / "pit.json"),
            "--qmt-local-data-dir",
            str(tmp_path / "qmt"),
            "--session",
            SESSION.isoformat(),
            "evaluate",
        )
    )
    monkeypatch.setattr(
        subject,
        "_forward_implementation_continuity",
        lambda **_kwargs: _ready_implementation_continuity(),
    )
    monkeypatch.setattr(
        subject,
        "load_frozen_forward_contract",
        lambda _path: SimpleNamespace(technical_mode="HUMAN_REVIEW_SCREENING"),
    )
    monkeypatch.setattr(
        subject,
        "audit_forward_sector_capture_readiness",
        lambda **_kwargs: {
            "ready": False,
            "reason_code": "SAME_SESSION_SECTOR_CAPTURE_RECEIPT_UNPROVEN",
            "catalog": None,
            "receipt_audit": {"status": "REQUIRED_RECEIPT_GAPS"},
        },
    )
    monkeypatch.setattr(
        subject,
        "_market_data_gate",
        lambda **_kwargs: {"complete": True, "reason_codes": ()},
    )
    monkeypatch.setattr(
        subject,
        "_session_pit_snapshot",
        lambda **_kwargs: (tmp_path / "pit.json", {}, None),
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_append(*, phase, status, evidence, **_kwargs):
        calls.append((phase, status, evidence))
        return {}, {"phase": phase, "status": status}, False

    monkeypatch.setattr(subject, "_append", fake_append)
    monkeypatch.setattr(subject, "_print", lambda _value: None)

    assert subject._evaluate(args) == 3
    assert [(phase, status) for phase, status, _evidence in calls] == [
        ("DATA_GATE", "DATA_BLOCKED"),
    ]
    assert calls[0][2]["reason_codes"] == (
        "SAME_SESSION_SECTOR_CAPTURE_RECEIPT_UNPROVEN",
    )


def test_human_review_evaluate_blocks_and_retries_virtual_settlement_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    args = subject.parser().parse_args(
        (
            "--root",
            str(tmp_path / "forward"),
            "--parameter-snapshot",
            str(tmp_path / "parameters.json"),
            "--sector-ledger",
            str(tmp_path / "sector.json"),
            "--pit-snapshot",
            str(tmp_path / "pit.json"),
            "--qmt-local-data-dir",
            str(tmp_path / "qmt"),
            "--session",
            SESSION.isoformat(),
            "evaluate",
        )
    )
    monkeypatch.setattr(
        subject,
        "_forward_implementation_continuity",
        lambda **_kwargs: _ready_implementation_continuity(),
    )
    monkeypatch.setattr(
        subject,
        "load_frozen_forward_contract",
        lambda _path: SimpleNamespace(technical_mode="HUMAN_REVIEW_SCREENING"),
    )
    monkeypatch.setattr(
        subject,
        "audit_forward_sector_capture_readiness",
        lambda **_kwargs: _ready_sector_capture(),
    )
    monkeypatch.setattr(subject, "sha256_file", lambda _path: "sha256:" + "c" * 64)
    monkeypatch.setattr(
        subject,
        "_market_data_gate",
        lambda **_kwargs: {"complete": True, "reason_codes": ()},
    )
    monkeypatch.setattr(
        subject,
        "_session_pit_snapshot",
        lambda **_kwargs: (tmp_path / "pit.json", {}, None),
    )
    monkeypatch.setattr(
        subject,
        "_archive_live_screening_snapshot",
        lambda **_kwargs: (
            {
                "schema": "chanlun-v3-forward-live-screening-snapshot/v1",
                "candidate_count": 2,
                "scanner_error_count": 0,
            },
            {"source_mode": "STAGED_LIVE_SCREENING_ARCHIVE"},
        ),
    )
    monkeypatch.setattr(
        subject,
        "_settle_human_paper",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("QMT read failed")),
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_append(*, phase, status, evidence, **_kwargs):
        calls.append((phase, status, evidence))
        return {}, {"phase": phase, "status": status}, False

    printed: list[dict[str, object]] = []
    monkeypatch.setattr(subject, "_append", fake_append)
    monkeypatch.setattr(subject, "_print", printed.append)

    assert subject._evaluate(args) == 5
    assert [(phase, status) for phase, status, _evidence in calls] == [
        ("DATA_GATE", "DATA_READY"),
        ("DECISION", "EVALUATION_BLOCKED"),
    ]
    failure = calls[-1][2]["human_paper_settlement"]
    assert failure["status"] == "VIRTUAL_SETTLEMENT_FAILED"
    assert failure["broker_transport_available"] is False
    assert printed[-1]["virtual_settlement_complete"] is False


def test_human_review_evaluate_blocks_and_retries_daily_valuation_failure(
    monkeypatch,
    tmp_path: Path,
) -> None:
    args = subject.parser().parse_args(
        (
            "--root",
            str(tmp_path / "forward"),
            "--parameter-snapshot",
            str(tmp_path / "parameters.json"),
            "--sector-ledger",
            str(tmp_path / "sector.json"),
            "--pit-snapshot",
            str(tmp_path / "pit.json"),
            "--qmt-local-data-dir",
            str(tmp_path / "qmt"),
            "--session",
            SESSION.isoformat(),
            "evaluate",
        )
    )
    monkeypatch.setattr(
        subject,
        "_forward_implementation_continuity",
        lambda **_kwargs: _ready_implementation_continuity(),
    )
    monkeypatch.setattr(
        subject,
        "load_frozen_forward_contract",
        lambda _path: SimpleNamespace(technical_mode="HUMAN_REVIEW_SCREENING"),
    )
    monkeypatch.setattr(
        subject,
        "audit_forward_sector_capture_readiness",
        lambda **_kwargs: _ready_sector_capture(),
    )
    monkeypatch.setattr(subject, "sha256_file", lambda _path: "sha256:" + "c" * 64)
    monkeypatch.setattr(
        subject,
        "_market_data_gate",
        lambda **_kwargs: {"complete": True, "reason_codes": ()},
    )
    monkeypatch.setattr(
        subject,
        "_session_pit_snapshot",
        lambda **_kwargs: (tmp_path / "pit.json", {}, None),
    )
    monkeypatch.setattr(
        subject,
        "_archive_live_screening_snapshot",
        lambda **_kwargs: (
            {
                "schema": "chanlun-v3-forward-live-screening-snapshot/v1",
                "candidate_count": 2,
                "scanner_error_count": 0,
            },
            {"source_mode": "STAGED_LIVE_SCREENING_ARCHIVE"},
        ),
    )
    monkeypatch.setattr(
        subject,
        "_settle_human_paper",
        lambda **_kwargs: {
            "status": "NO_PENDING_VIRTUAL_INTENTS",
            "live_status": "LIVE_DISABLED",
        },
    )
    monkeypatch.setattr(
        subject,
        "_capture_human_paper_valuation",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("15:00 bar missing")),
    )
    calls: list[tuple[str, str, dict[str, object]]] = []

    def fake_append(*, phase, status, evidence, **_kwargs):
        calls.append((phase, status, evidence))
        return {}, {"phase": phase, "status": status}, False

    printed: list[dict[str, object]] = []
    monkeypatch.setattr(subject, "_append", fake_append)
    monkeypatch.setattr(subject, "_print", printed.append)

    assert subject._evaluate(args) == 6
    assert [(phase, status) for phase, status, _evidence in calls] == [
        ("DATA_GATE", "DATA_READY"),
        ("DECISION", "EVALUATION_BLOCKED"),
    ]
    failure = calls[-1][2]["human_paper_valuation"]
    assert failure["status"] == "VALUATION_FAILED"
    assert failure["equity_curve_point_available"] is False
    assert failure["performance_evaluable"] is False
    assert failure["broker_transport_available"] is False
    assert printed[-1]["virtual_settlement_complete"] is True
    assert printed[-1]["daily_valuation_complete"] is False


def test_legacy_forward_attempt_without_declared_policy_stays_unattested(
    tmp_path: Path,
) -> None:
    session_root = tmp_path / "session"
    live_object = session_root / "objects" / "legacy.json"
    snapshot = {
        "screening_policy": {
            "selection_universe_source": "qmt_gics3_current_components"
        }
    }
    legacy_source_identity = subject.sha256_json(snapshot)
    stable = {
        "schema": "chanlun-v3-forward-live-screening-snapshot/v1",
        "source_content_sha256": legacy_source_identity,
        "snapshot": snapshot,
    }
    document = {**stable, "content_sha256": subject.sha256_json(stable)}
    subject._atomic_json(live_object, document)
    attempt = {
        "source_content_sha256": legacy_source_identity,
        "live_object": {
            "path": str(live_object.relative_to(session_root)),
            "file_sha256": subject.sha256_file(live_object),
            "content_sha256": document["content_sha256"],
        }
    }

    assert subject._attempt_screening_policy_id(session_root, attempt) == (
        subject.LEGACY_UNATTESTED_SCREENING_POLICY_ID
    )


def test_current_forward_attempt_binds_semantic_and_exact_source_identities(
    tmp_path: Path,
) -> None:
    session_root = tmp_path / "session"
    snapshot = {
        "generated_at": "2026-07-28T15:20:00+08:00",
        "signals": [],
    }
    semantic_identity = subject.live_screening_snapshot_content_sha256(
        snapshot
    )
    payload_identity = subject.sha256_json(snapshot)

    def write_object(
        name: str,
        *,
        declared_payload_identity: str,
    ) -> tuple[Path, dict[str, object]]:
        path = session_root / "objects" / f"{name}.json"
        stable = {
            "schema": "chanlun-v3-forward-live-screening-snapshot/v1",
            "source_content_sha256": semantic_identity,
            "source_payload_sha256": declared_payload_identity,
            "snapshot": snapshot,
        }
        document = {
            **stable,
            "content_sha256": subject.sha256_json(stable),
        }
        subject._atomic_json(path, document)
        return path, document

    live_object, document = write_object(
        "valid",
        declared_payload_identity=payload_identity,
    )
    attempt = {
        "source_content_sha256": semantic_identity,
        "live_object": {
            "path": str(live_object.relative_to(session_root)),
            "file_sha256": subject.sha256_file(live_object),
            "content_sha256": document["content_sha256"],
        },
    }
    loaded, loaded_snapshot = subject._attempt_live_snapshot(
        session_root,
        attempt,
    )
    assert loaded == document
    assert loaded_snapshot == snapshot

    forged_object, forged = write_object(
        "forged",
        declared_payload_identity="sha256:" + "f" * 64,
    )
    forged_attempt = {
        **attempt,
        "live_object": {
            "path": str(forged_object.relative_to(session_root)),
            "file_sha256": subject.sha256_file(forged_object),
            "content_sha256": forged["content_sha256"],
        },
    }
    with pytest.raises(
        RuntimeError,
        match="screening source identity changed",
    ):
        subject._attempt_live_snapshot(session_root, forged_attempt)


def test_human_review_contract_keeps_pit_gap_as_warning_policy() -> None:
    source = Path(subject.__file__).read_text(encoding="utf-8")

    assert (
        'refresh_allowed=contract.technical_mode != "HUMAN_REVIEW_SCREENING"'
        in source
    )
    assert "WARNING_ONLY_FOR_CURRENT_QMT_HUMAN_REVIEW_SCREENING" in source
    assert "else:\n            reasons.append(pit_reason)" in source


def test_cumulative_paper_replay_rejects_a_missing_market_session(
    monkeypatch,
    tmp_path: Path,
) -> None:
    for offset in (0, 2):
        session = SESSION + timedelta(days=offset)
        directory = tmp_path / "sessions" / session.isoformat()
        directory.mkdir(parents=True)
        decision = datetime.combine(session, time(10), tzinfo=CN)
        (directory / "forward_replay_batches.pkl").write_bytes(
            pickle.dumps(
                (
                    ReplayBatch(
                        batch_id=f"paper:{session}",
                        decision_at=decision,
                        valuation_at=decision + timedelta(minutes=30),
                        events=(),
                    ),
                ),
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        )
        (directory / "forward_active_signals.pkl").write_bytes(
            pickle.dumps(
                {"strategic": {}, "tactical": {}},
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        )
    monkeypatch.setattr(
        subject,
        "load_qmt_frame",
        lambda *_args, **_kwargs: pd.DataFrame(
            {
                "date": [
                    datetime.combine(
                        SESSION + timedelta(days=offset),
                        time(15),
                        tzinfo=CN,
                    )
                    for offset in range(3)
                ]
            }
        ),
    )

    with pytest.raises(RuntimeError, match="unobserved market sessions"):
        subject._cumulative_replay(
            root=tmp_path,
            through_session=SESSION + timedelta(days=2),
        )


def test_default_forward_pipeline_emits_review_queue_without_replay_artifacts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    qmt = tmp_path / "qmt"
    qmt.mkdir()
    commands: list[tuple[str, ...]] = []

    def fake_run_logged(*, command, log_path):
        command = tuple(command)
        commands.append(command)
        log_path.write_text("ok\n", encoding="utf-8")
        if "backtest_v3_sector_first_full_market.py" in " ".join(command):
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                json.dumps(
                    {
                        "schema": "chanlun-v3-human-review-screen/v1",
                        "forward_paper_session": SESSION.isoformat(),
                        "result_label": "FORWARD_HUMAN_REVIEW_QUEUE",
                        "data_grade": "HUMAN_REVIEW_SCREENING",
                        "highest_status": "REVIEW_REQUIRED",
                        "portfolio_backtest_performed": False,
                        "orders_created": 0,
                        "fills_created": 0,
                        "automated_order_authorized": False,
                        "human_confirmation_required": True,
                        "review_queue": [],
                        "candidate_funnel": {"review_candidate_count": 0},
                        "signal_counts": {},
                        "event_study": {"summary": {}},
                        "content_sha256": "sha256:" + "1" * 64,
                        "live_status": "LIVE_DISABLED",
                    }
                ),
                encoding="utf-8",
            )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subject, "_run_logged", fake_run_logged)
    monkeypatch.setattr(
        subject,
        "_cumulative_replay",
        lambda **_kwargs: pytest.fail("human review must not run cumulative replay"),
    )
    parameters = (
        Path(__file__).resolve().parents[2]
        / "audit"
        / "chanlun_trading_system_backtest"
        / "recent_year_current_sector_no3p"
        / "parameter_snapshot_human_review.json"
    )
    args = subject.parser().parse_args(
        (
            "--root",
            str(tmp_path / "forward"),
            "--parameter-snapshot",
            str(parameters),
            "--qmt-local-data-dir",
            str(qmt),
            "evaluate",
        )
    )

    report, evidence = subject._forward_pipeline(
        args=args,
        session=SESSION,
        pit_path=tmp_path / "pit.json",
    )

    decision = next(
        command
        for command in commands
        if "backtest_v3_sector_first_full_market.py" in " ".join(command)
    )
    assert report is not None
    assert "--human-review-only" in decision
    assert "--batch-output" not in decision
    assert "--holding-state" not in decision
    assert "--active-signal-state" not in decision
    executed_tools = {
        Path(command[1]).resolve().relative_to(subject.PROJECT_ROOT).as_posix()
        for command in commands
    }
    assert executed_tools == {
        "tools/backtest_v3_sector_first_full_market.py",
        "tools/build_v3_recent_year_current_sector_triggers.py",
        "tools/extract_v3_sector_first_direct_facts.py",
        "tools/prescreen_v3_sector_first_research_candidates.py",
    }
    assert executed_tools <= set(subject.FORWARD_PIPELINE_TOOL_PATHS)
    assert evidence["orders_created"] == 0
    assert evidence["highest_status"] == "REVIEW_REQUIRED"
    assert not (tmp_path / "forward" / "cumulative_state.json").exists()


def test_status_exposes_sector_capture_receipt_audit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    forward_ledger = tmp_path / "forward_paper_ledger.json"
    forward_ledger.write_text("{}", encoding="utf-8")
    sector_ledger = tmp_path / "sector_ledger.json"
    sector_ledger.write_text("{}", encoding="utf-8")
    contract = SimpleNamespace(
        document=lambda: {"contract": "frozen"},
        operational_status="PAPER_OBSERVATION",
        contract_id="sha256:" + "1" * 64,
        strategy_parameter_set_id="sha256:" + "2" * 64,
    )
    expected_audit = {
        "schema": "chanlun-v3-qmt-sector-receipt-audit/v1",
        "status": "LEGACY_RECEIPT_GAPS",
        "entry_count": 3,
        "valid_receipt_count": 2,
        "missing_capture_sessions": ("2026-07-27",),
        "historical_receipts_synthesized": False,
        "live_status": "LIVE_DISABLED",
    }
    expected_readiness = {
        "schema": "chanlun-v3-forward-sector-capture-readiness/v1",
        "ready": False,
        "status": "not_ready",
        "reason_code": "SAME_SESSION_SECTOR_CAPTURE_RECEIPT_UNPROVEN",
        "session": SESSION.isoformat(),
        "receipt_proven": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }
    captured: list[dict[str, object]] = []
    audit_calls: list[tuple[Path, date | None]] = []
    monkeypatch.setattr(subject, "_paths", lambda _args: (tmp_path, forward_ledger))
    monkeypatch.setattr(subject, "load_frozen_forward_contract", lambda _path: contract)
    monkeypatch.setattr(
        subject,
        "load_forward_paper_ledger",
        lambda _path, *, contract: {
            "events": (),
            "content_sha256": "sha256:" + "e" * 64,
        },
    )
    def audit_receipts(*, output: Path, required_capture_session: date | None):
        audit_calls.append((output, required_capture_session))
        return expected_audit

    monkeypatch.setattr(subject, "audit_sector_capture_receipts", audit_receipts)
    monkeypatch.setattr(
        subject,
        "audit_forward_sector_capture_readiness",
        lambda **_kwargs: expected_readiness,
    )
    monkeypatch.setattr(
        subject,
        "_now",
        lambda: datetime.combine(SESSION, time(9, 11), tzinfo=CN),
    )
    calendar_evidence = build_trading_session_evidence(
        session=SESSION,
        observed_at=datetime.combine(SESSION, time(9, 11), tzinfo=CN),
        returned_sessions=(SESSION,),
        published_through=SESSION,
        query_attempted=True,
        query_succeeded=True,
    )
    monkeypatch.setattr(
        subject,
        "authoritative_trading_session_evidence",
        lambda **_kwargs: calendar_evidence,
    )
    monkeypatch.setattr(subject, "_print", captured.append)
    args = SimpleNamespace(
        parameter_snapshot=tmp_path / "parameters.json",
        sector_ledger=sector_ledger,
        human_paper_ledger=tmp_path / "paper.json",
        session=SESSION,
    )

    assert subject._status(args) == 0

    assert len(captured) == 1
    assert captured[0]["sector_capture_receipts"] == expected_audit
    assert captured[0]["sector_capture_readiness"] == expected_readiness
    assert captured[0]["trading_session_evidence"] == calendar_evidence
    assert captured[0]["session_delivery"]["ready"] is False
    assert captured[0]["session_delivery"]["reason_code"] == (
        "CAPTURE_MISSING_AFTER_DUE"
    )
    assert captured[0]["forward_warmup_structure_lineage"]["status"] == (
        "NO_QUALIFIED_SESSIONS"
    )
    assert (
        captured[0]["forward_warmup_structure_lineage"][
            "qualified_session_count"
        ]
        == 0
    )
    assert captured[0]["paper_execution_evidence"]["status"] == "NO_FILLS"
    assert captured[0]["paper_execution_rejection_evidence"] == {
        "schema": "chanlun-human-paper-execution-rejection-evidence-audit/v1",
        "status": "NO_REJECTIONS",
        "rejection_count": 0,
        "verified_rejection_count": 0,
        "unique_execution_evidence_count": 0,
        "missing_evidence": [],
        "invalid_evidence": [],
        "first_eligible_bar_verified": True,
        "price_cap_and_ttl_verified": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert captured[0]["paper_operations_cancellation_evidence"] == {
        "schema": (
            "chanlun-human-paper-operations-cancellation-"
            "evidence-audit/v1"
        ),
        "status": "NO_CANCELLATIONS",
        "cancellation_count": 0,
        "verified_cancellation_count": 0,
        "unique_execution_evidence_count": 0,
        "missing_evidence": [],
        "invalid_evidence": [],
        "data_fault_cancellation_count": 0,
        "execution_fact_incomplete_cancellation_count": 0,
        "execution_fact_incomplete_reason_counts": {
            "SECURITY_STATUS_INCOMPLETE": 0,
            "CORPORATE_ACTION_RECONCILIATION_REQUIRED": 0,
        },
        "security_gate_cancellation_count": 0,
        "security_gate_reason_counts": {
            "SUSPENDED": 0,
            "EXPIRED": 0,
            "ST_BUY_PROHIBITED": 0,
        },
        "optional_buy_operations_cancellation_verified": True,
        "optional_buy_data_fault_cancellation_verified": True,
        "optional_buy_security_gate_cancellation_verified": True,
        "persistent_exit_untouched": True,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    assert captured[0]["paper_capital_controls"][
        "execution_rejection_exact_1m_evidence_audited"
    ] is True
    assert captured[0]["paper_capital_controls"][
        "optional_buy_data_fault_cancelled"
    ] is True
    assert captured[0]["paper_capital_controls"][
        "optional_buy_security_gate_cancelled"
    ] is True
    assert captured[0]["paper_capital_controls"][
        "execution_fact_incomplete_optional_buy_cancelled"
    ] is True
    assert captured[0]["paper_capital_controls"][
        "operations_cancellation_exact_evidence_audited"
    ] is True
    assert captured[0]["paper_capital_controls"][
        "persistent_exit_independent_symbol_continues"
    ] is True
    assert captured[0]["paper_capital_controls"][
        "persistent_exit_security_blocked_remains_pending"
    ] is True
    assert captured[0]["paper_capital_controls"][
        "persistent_exit_fact_incomplete_remains_pending"
    ] is True
    assert audit_calls == [(sector_ledger.resolve(), SESSION)]
    assert captured[0]["real_account_access"] is False
    assert captured[0]["real_order_transport"] is False
    assert captured[0]["live_status"] == "LIVE_DISABLED"


def test_required_sector_capture_session_never_guesses_an_unpublished_weekday(
) -> None:
    session = date(2026, 7, 31)
    observed = datetime.combine(session, time(9, 11), tzinfo=CN)
    args = SimpleNamespace(session=session)
    unresolved = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(),
        published_through=date(2026, 7, 30),
        query_attempted=True,
        query_succeeded=True,
    )
    confirmed = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(session,),
        published_through=session,
        query_attempted=True,
        query_succeeded=True,
    )

    assert subject._required_sector_capture_session(
        args,
        observed_at=observed,
        trading_session_evidence=unresolved,
    ) is None
    assert subject._required_sector_capture_session(
        args,
        observed_at=observed,
        trading_session_evidence=confirmed,
    ) == session


def test_application_source_revision_changes_with_tracked_and_untracked_source(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "source"
    (repo / "src").mkdir(parents=True)
    source = repo / "src" / "application.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "tools").mkdir()
    pipeline_tool = repo / "tools" / "backtest_v3_sector_first_full_market.py"
    pipeline_tool.write_text("PIPELINE_VERSION = 1\n", encoding="utf-8")

    def git(*arguments: str) -> str:
        completed = subject.subprocess.run(
            ["git", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return completed.stdout.strip()

    git("init", "--quiet")
    git("config", "user.email", "forward-provenance@example.invalid")
    git("config", "user.name", "Forward Provenance Test")
    git("add", "src/application.py", "tools/backtest_v3_sector_first_full_market.py")
    git("commit", "--quiet", "-m", "fixture")
    head = git("rev-parse", "HEAD")

    subject._application_source_revision.cache_clear()
    original = subject._application_source_revision(repo)
    assert original.startswith(f"{head}.tree.")
    assert len(original.rsplit(".tree.", maxsplit=1)[1]) == 24

    source.write_text("VALUE = 2\n", encoding="utf-8")
    subject._application_source_revision.cache_clear()
    tracked_change = subject._application_source_revision(repo)
    assert tracked_change != original

    pipeline_tool.write_text("PIPELINE_VERSION = 2\n", encoding="utf-8")
    subject._application_source_revision.cache_clear()
    pipeline_change = subject._application_source_revision(repo)
    assert pipeline_change != tracked_change

    (repo / "ops").mkdir()
    (repo / "ops" / "untracked.ps1").write_text(
        "Write-Output 'new'\n",
        encoding="utf-8",
    )
    subject._application_source_revision.cache_clear()
    untracked_change = subject._application_source_revision(repo)
    assert untracked_change != pipeline_change


def test_implementation_provenance_is_content_addressed_and_safe() -> None:
    provenance = subject._implementation_provenance()
    stable = dict(provenance)
    content_sha256 = stable.pop("content_sha256")

    assert provenance["schema"] == subject.IMPLEMENTATION_PROVENANCE_SCHEMA
    assert content_sha256 == subject.sha256_json(stable)
    assert provenance["forward_runner_script_sha256"] == subject.sha256_file(
        subject.PROJECT_ROOT / "ops" / "run_v3_forward_paper_daily.ps1"
    )
    assert provenance["forward_python_tool_sha256"] == subject.sha256_file(
        Path(subject.__file__).resolve()
    )
    assert provenance["sector_capture_tool_sha256"] == subject.sha256_file(
        subject.PROJECT_ROOT / "tools" / "snapshot_qmt_gics3_sector_ledger.py"
    )
    assert set(subject.FORWARD_PIPELINE_TOOL_PATHS) == {
        "tools/audit_qmt_warmup_convergence.py",
        "tools/backtest_v3_sector_first_full_market.py",
        "tools/build_v3_recent_year_current_sector_triggers.py",
        "tools/extract_v3_sector_first_direct_facts.py",
        "tools/prescreen_v3_sector_first_research_candidates.py",
        "tools/run_v3_forward_paper.py",
        "tools/snapshot_qmt_gics3_sector_ledger.py",
        "tools/snapshot_qmt_pit_metadata.py",
    }
    assert all(
        (subject.PROJECT_ROOT / path).is_file()
        for path in subject.FORWARD_PIPELINE_TOOL_PATHS
    )
    assert provenance["real_account_accessed"] is False
    assert provenance["real_order_transport_enabled"] is False
    assert provenance["live_status"] == "LIVE_DISABLED"


def test_forward_append_injects_provenance_and_remains_idempotent(
    tmp_path: Path,
) -> None:
    args = SimpleNamespace(
        root=tmp_path,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        session=SESSION,
    )
    evidence = {"reason": "IMPLEMENTATION_PROVENANCE_TEST"}

    first, first_event, first_reused = subject._append(
        args=args,
        phase="CONTROL",
        status="PAPER_STARTED",
        evidence=evidence,
    )
    second, second_event, second_reused = subject._append(
        args=args,
        phase="CONTROL",
        status="PAPER_STARTED",
        evidence=evidence,
    )

    assert first_reused is False
    assert second_reused is True
    assert second == first
    assert second_event == first_event
    assert first_event["evidence"]["implementation_provenance"] == (
        subject._implementation_provenance()
    )
    assert first_event["real_order_transport_enabled"] is False
    assert first_event["live_status"] == "LIVE_DISABLED"

    with pytest.raises(ValueError, match="implementation_provenance is reserved"):
        subject._append(
            args=args,
            phase="CONTROL",
            status="PAPER_STARTED",
            evidence={"implementation_provenance": {}},
        )


def test_forward_append_rejects_mid_run_implementation_change(
    tmp_path: Path,
    monkeypatch,
) -> None:
    args = SimpleNamespace(
        root=tmp_path,
        parameter_snapshot=PARAMETER_SNAPSHOT,
        session=SESSION,
    )
    frozen = subject._implementation_provenance()
    changed_stable = {
        key: value for key, value in frozen.items() if key != "content_sha256"
    }
    changed_stable["application_source_revision"] = (
        "0" * 40 + ".tree." + "1" * 24
    )
    changed = {
        **changed_stable,
        "content_sha256": subject.sha256_json(changed_stable),
    }
    monkeypatch.setattr(subject, "_implementation_provenance", lambda: frozen)
    monkeypatch.setattr(
        subject,
        "_current_implementation_provenance",
        lambda: changed,
        raising=False,
    )

    with pytest.raises(
        RuntimeError,
        match="forward implementation changed while the command was running",
    ):
        subject._append(
            args=args,
            phase="CONTROL",
            status="PAPER_STARTED",
            evidence={"reason": "MID_RUN_SOURCE_CHANGE_TEST"},
        )

    assert not (tmp_path / "forward_paper_ledger.json").exists()


def test_mutating_commands_freeze_provenance_before_the_handler(
    monkeypatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        subject,
        "_implementation_provenance",
        lambda: calls.append("provenance") or {},
    )
    monkeypatch.setattr(
        subject,
        "_capture",
        lambda _args: calls.append("capture") or 7,
    )

    assert subject.main(["capture"]) == 7
    assert calls == ["provenance", "capture"]

    calls.clear()
    monkeypatch.setattr(
        subject,
        "_status",
        lambda _args: calls.append("status") or 0,
    )
    assert subject.main(["status"]) == 0
    assert calls == ["status"]


def test_candidate_warmup_diagnostic_reuses_hash_bound_alias(
    tmp_path: Path,
) -> None:
    from chanlun.decision_support.trading_system.candidate_warmup_diagnostics import (
        build_candidate_warmup_diagnostic_document,
        candidate_warmup_diagnostic_path,
        candidate_warmup_parameter_document,
    )
    from chanlun.decision_support.trading_system.warmup_convergence import (
        WarmupPrefixObservation,
        classify_warmup_convergence_envelope,
    )

    source_identity = "sha256:" + "d" * 64
    as_of = datetime.combine(SESSION, time(15), tzinfo=CN)
    parameters = candidate_warmup_parameter_document()
    selected = {
        "rank": 1,
        "code": "SH.600000",
        "source_position": 0,
        "lifecycle_stage": "approaching",
        "sector_horizontal_rank": 1,
        "point_type": "1buy",
        "selection_profile": "MODERN_BUY_REVIEW_ORDER",
    }
    rows = []
    for frequency in parameters["frequencies"]:
        envelope = classify_warmup_convergence_envelope(
            frequency=str(frequency),
            as_of=as_of,
            parameter_set_id="sha256:" + "e" * 64,
            observations=tuple(
                WarmupPrefixObservation(
                    bar_count=count,
                    starts_at=as_of - timedelta(days=count),
                    signature_sha256="sha256:" + "f" * 64,
                )
                for count in (480, 960, 1440)
            ),
        )
        rows.append(
            {
                "code": selected["code"],
                "frequency": frequency,
                "source": "qmt_local_completed_kline",
                "available_bar_count": 1600,
                "market_data_as_of": as_of.isoformat(),
                "envelope": envelope.document(),
                "semantic_diagnostic": None,
                "mapping_supply_diagnostic": None,
                "structure_lineage_diagnostic": None,
            }
        )
    document = build_candidate_warmup_diagnostic_document(
        source_content_sha256=source_identity,
        source_wrapper_content_sha256=None,
        requested_as_of=as_of,
        selected_candidates=(selected,),
        rows=rows,
        errors=(),
        parameter_document=parameters,
    )
    alias = candidate_warmup_diagnostic_path(
        tmp_path,
        source_content_sha256=source_identity,
        parameter_set_id=str(document["diagnostic_parameter_set_id"]),
    )
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.write_text(json.dumps(document), encoding="utf-8")

    result = subject._candidate_warmup_diagnostic(
        args=SimpleNamespace(root=tmp_path, qmt_local_data_dir=None),
        archived_screen_path=tmp_path / "unused-wrapper.json",
        source_content_sha256=source_identity,
    )

    assert result["status"] == "COMPLETE"
    assert result["reused"] is True
    assert result["content_sha256"] == document["content_sha256"]
    assert Path(str(result["result"])).is_file()
    assert result["active_gate_unchanged"] is True
    assert result["automated_order_authorized"] is False
