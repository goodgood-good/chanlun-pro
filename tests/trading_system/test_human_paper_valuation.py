from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import chanlun.decision_support.trading_system.human_paper_valuation as valuation_module

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.human_paper_accounting import (
    assess_human_paper_portfolio_fill,
    load_human_paper_accounting_parameters,
    rebuild_human_paper_accounting,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    human_paper_ledger_content_sha256,
)
from chanlun.decision_support.trading_system.human_paper_valuation import (
    audit_human_paper_valuation_evidence,
    build_human_paper_valuation_document,
    validate_human_paper_valuation_document,
)


TZ = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 7, 28)
PARAMETER_SNAPSHOT = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "forward_paper"
    / "parameter_snapshot_human_review.json"
)


def _parameters():
    return load_human_paper_accounting_parameters(PARAMETER_SNAPSHOT)


def _accounting(events=(), *, evidence: str = "NO_FILLS") -> dict[str, object]:
    return rebuild_human_paper_accounting(
        events,
        parameters=_parameters(),
        execution_evidence_status=evidence,
    )


def _instrument_fact(symbol: str = "SH.600000") -> dict[str, object]:
    return {
        "symbol": symbol,
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
        "pre_close": "10.50",
        "limit_up": "11.55",
        "limit_down": "9.45",
        "price_tick": "0.01",
        "corporate_actions": [],
        "source_methods": [
            "QMT_GET_INSTRUMENT_DETAIL",
            "QMT_GET_DIVID_FACTORS",
        ],
        "tick_data_used": False,
        "account_api_used": False,
    }


def _mark(*, quantity: int = 100, close: str = "11.00") -> dict[str, object]:
    return {
        "symbol": "SH.600000",
        "quantity": quantity,
        "opened_at": "2026-07-28T14:59:00+08:00",
        "closed_at": "2026-07-28T15:00:00+08:00",
        "open": "10.90",
        "high": "11.10",
        "low": "10.80",
        "close": close,
        "volume": "8000",
        "market_value": format(Decimal(quantity) * Decimal(close), "f"),
        "complete": True,
        "suspended": False,
        "security_status_complete": True,
        "corporate_action_state_complete": True,
        "oldest_acquired_session": SESSION.isoformat(),
        "instrument_fact": _instrument_fact(),
        "qmt_transport": "LOCAL_FIXED_RECORD_READ_ONLY",
        "qmt_local_cache_source_sha256": "sha256:" + "8" * 64,
    }


def _approved_buy_fill_event() -> dict[str, object]:
    decision = assess_human_paper_portfolio_fill(
        (),
        parameters=_parameters(),
        symbol="SH.600000",
        quantity=100,
        price=Decimal("10.00"),
        session=SESSION,
        position_marks={},
    )
    payload = {
        "fill_id": "sha256:" + "a" * 64,
        "intent_id": "sha256:" + "b" * 64,
        "symbol": "SH.600000",
        "side": "BUY",
        "quantity": 100,
        "price": "10.00",
        "filled_at": "2026-07-28T10:02:00+08:00",
        "source_bar_closed_at": "2026-07-28T10:02:00+08:00",
        "execution_snapshot_sha256": "sha256:" + "c" * 64,
        "portfolio_decision_sha256": decision["content_sha256"],
        "accounting_contract_id": decision["accounting_contract_id"],
        "fill_model": "ADVERSE_OBSERVED_BAR_EXTREME_WITHIN_LIMIT",
        "buy_strict_cross_rule": "ENTIRE_BAR_RANGE_STRICTLY_THROUGH_LIMIT",
        "buy_max_bar_volume_participation": "0.05",
        "tick_data_used": False,
        "virtual_only": True,
        "live_status": "LIVE_DISABLED",
    }
    for name in (
        "available_cash",
        "current_market_value",
        "account_equity",
        "notional",
        "terminal_buy_fee",
        "required_cash",
        "occupied_slots",
        "slot_count",
        "slot_fraction",
        "slot_notional_cap",
        "account_exposure_cap",
        "account_exposure_notional_cap",
        "post_trade_gross_market_value",
        "position_marks",
    ):
        payload[name] = decision[name]
    return {"kind": "FILL", "payload": payload}


def _promote(root: Path, document: dict[str, object]) -> None:
    session_root = root / "sessions" / str(document["session"])
    identity = str(document["content_sha256"])
    object_path = (
        session_root / "objects" / "paper_valuation" / f"{identity[7:]}.json"
    )
    object_path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(document, ensure_ascii=False)
    object_path.write_text(encoded, encoding="utf-8")
    (session_root / "paper_valuation.json").write_text(encoded, encoding="utf-8")


def _forward_evaluation_event(
    document: dict[str, object] | None = None,
    *,
    session: date | None = None,
    valuation_content_sha256: str | None = None,
) -> dict[str, object]:
    """Build one internally hashed, successful forward valuation anchor."""

    session_value = session or date.fromisoformat(str(document["session"]))
    identity = valuation_content_sha256 or str(document["content_sha256"])
    evidence = {
        "human_paper_valuation": {
            "status": "VALUATION_COMPLETE",
            "session": session_value.isoformat(),
            "valuation_content_sha256": identity,
        }
    }
    stable: dict[str, object] = {
        "schema": "chanlun-forward-paper-event",
        "session": session_value.isoformat(),
        "recorded_at": datetime.combine(
            session_value,
            datetime.min.time(),
            tzinfo=TZ,
        ).replace(hour=15, minute=21).isoformat(),
        "phase": "DECISION",
        "status": "EVALUATED",
        "contract_id": "sha256:" + "d" * 64,
        "strategy_parameter_set_id": "sha256:" + "e" * 64,
        "previous_event_sha256": None,
        "evidence": evidence,
        "evidence_sha256": sha256_json(evidence),
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "paper_status": "PAPER_OBSERVATION",
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "event_sha256": sha256_json(stable)}


def _promote_execution_capture(root: Path) -> None:
    captured_at = datetime(2026, 7, 28, 15, 10, tzinfo=TZ).isoformat()
    fact = {
        **_instrument_fact(),
        "factor_start": SESSION.isoformat(),
        "virtual_position_quantity": 0,
        "oldest_virtual_acquired_session": None,
        "position_corporate_action_conflict": False,
        "security_status_complete": True,
        "corporate_action_state_complete": True,
        "buy_eligible": True,
        "sell_eligible": True,
    }
    facts_stable: dict[str, object] = {
        "schema": "chanlun-human-paper-execution-facts",
        "session": SESSION.isoformat(),
        "captured_at": captured_at,
        "symbols": [fact],
        "errors": [],
        "requested_symbol_count": 1,
        "complete_symbol_count": 1,
        "all_complete": True,
        "source": "QMT_READ_ONLY_INSTRUMENT_DETAIL_AND_DIVID_FACTORS",
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    facts = {**facts_stable, "content_sha256": sha256_json(facts_stable)}

    closes = [
        *(datetime(2026, 7, 28, 9, minute, tzinfo=TZ) for minute in range(31, 60)),
        *(datetime(2026, 7, 28, hour, minute, tzinfo=TZ) for hour in (10,) for minute in range(60)),
        *(datetime(2026, 7, 28, 11, minute, tzinfo=TZ) for minute in range(31)),
        *(datetime(2026, 7, 28, 13, minute, tzinfo=TZ) for minute in range(1, 60)),
        *(datetime(2026, 7, 28, 14, minute, tzinfo=TZ) for minute in range(60)),
        datetime(2026, 7, 28, 15, 0, tzinfo=TZ),
    ]
    assert len(closes) == 240
    bars = []
    for closed_at in closes:
        final = closed_at.hour == 15
        bars.append(
            {
                "symbol": "SH.600000",
                "opened_at": (closed_at - timedelta(minutes=1)).isoformat(),
                "closed_at": closed_at.isoformat(),
                "open": "10.90" if final else "10.50",
                "high": "11.10" if final else "10.50",
                "low": "10.80" if final else "10.50",
                "close": "11.00" if final else "10.50",
                "volume": "8000" if final else "1000",
                "complete": True,
                "suspended": False,
                "limit_up_locked": False,
                "limit_down_locked": False,
                "buy_eligible": True,
                "sell_eligible": True,
                "security_status_complete": True,
                "corporate_action_state_complete": True,
            }
        )
    evidence_stable: dict[str, object] = {
        "schema": "chanlun-human-paper-execution-evidence",
        "session": SESSION.isoformat(),
        "captured_at": captured_at,
        "execution_fact_snapshot_sha256": facts["content_sha256"],
        "pending_intent_ids": [],
        "bars_by_symbol": {"SH.600000": bars},
        "bar_grid_audits": [
            {
                "symbol": "SH.600000",
                "status": "COMPLETE",
                "native_row_count": 240,
                "normalized_row_count": 240,
                "complete_sessions": [SESSION.isoformat()],
                "session_issues": [],
                "source_base_stream_revision": "sha256:" + "6" * 64,
            }
        ],
        "all_required_bar_grids_complete": True,
        "fill_model": "ADVERSE_OBSERVED_BAR_EXTREME_WITHIN_LIMIT",
        "fill_timestamp_rule": "COMPLETED_BAR_CLOSE",
        "buy_strict_cross_rule": "ENTIRE_BAR_RANGE_STRICTLY_THROUGH_LIMIT",
        "buy_max_bar_volume_participation": "0.05",
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    evidence = {
        **evidence_stable,
        "content_sha256": sha256_json(evidence_stable),
    }
    session_root = root / "sessions" / SESSION.isoformat()
    for kind, alias_name, document in (
        ("paper_execution_facts", "paper_execution_facts.json", facts),
        ("paper_execution_evidence", "paper_execution_evidence.json", evidence),
    ):
        identity = str(document["content_sha256"])
        object_path = session_root / "objects" / kind / f"{identity[7:]}.json"
        object_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(document, ensure_ascii=False)
        object_path.write_text(encoded, encoding="utf-8")
        (session_root / alias_name).write_text(encoded, encoding="utf-8")


def test_cash_only_close_valuation_is_immutable_but_not_performance(
    tmp_path: Path,
) -> None:
    accounting = _accounting()
    events: tuple[dict[str, object], ...] = ()
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=accounting,
        marks=(),
        errors=(),
    )
    assert document["all_complete"] is True
    assert document["cash_balance"] == "1000000.00"
    assert document["market_value"] == "0.00"
    assert document["equity"] == "1000000.00"
    assert document["pnl_from_initial_cash"] == "0.00"
    assert document["performance_evaluable"] is False
    assert validate_human_paper_valuation_document(document) == document

    _promote(tmp_path, document)
    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
        forward_events=(_forward_evaluation_event(document),),
    )
    assert audit["status"] == "COMPLETE"
    assert audit["equity_curve_available"] is True
    assert audit["performance_evaluable"] is False
    assert audit["latest"]["content_sha256"] == document["content_sha256"]


def test_verified_point_without_forward_ledger_is_continuity_unverified(
    tmp_path: Path,
) -> None:
    events: tuple[dict[str, object], ...] = ()
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
    )

    assert audit["status"] == "CONTINUITY_UNVERIFIED"
    assert audit["curve_continuity_status"] == "CONTINUITY_UNVERIFIED"
    assert audit["source_provenance_verified"] is True
    assert audit["forward_anchor_available"] is False
    assert audit["unanchored_valuation_sessions"] == [SESSION.isoformat()]
    assert audit["equity_curve_available"] is False
    assert audit["latest"] is None


def test_missing_successful_forward_session_closes_the_whole_curve(
    tmp_path: Path,
) -> None:
    events: tuple[dict[str, object], ...] = ()
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)
    missing_session = SESSION + timedelta(days=1)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
        forward_events=(
            _forward_evaluation_event(document),
            _forward_evaluation_event(
                session=missing_session,
                valuation_content_sha256="sha256:" + "f" * 64,
            ),
        ),
    )

    assert audit["status"] == "INCOMPLETE_CURVE"
    assert audit["curve_continuity_status"] == "INCOMPLETE_CURVE"
    assert audit["missing_valuation_sessions"] == [missing_session.isoformat()]
    assert audit["verified_forward_anchor_count"] == 1
    assert audit["equity_curve_available"] is False
    assert audit["latest"] is None


def test_successful_forward_anchor_detects_a_deleted_only_point(
    tmp_path: Path,
) -> None:
    events: tuple[dict[str, object], ...] = ()
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
        forward_events=(_forward_evaluation_event(document),),
    )

    assert audit["status"] == "INCOMPLETE_CURVE"
    assert audit["valuation_count"] == 0
    assert audit["missing_valuation_sessions"] == [SESSION.isoformat()]
    assert audit["equity_curve_available"] is False


def test_only_successful_forward_sessions_are_required_not_guessed_dates(
    tmp_path: Path,
) -> None:
    """A calendar gap is valid when no successful forward session claims it."""

    events: tuple[dict[str, object], ...] = ()
    first = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    later_session = SESSION + timedelta(days=2)
    later = build_human_paper_valuation_document(
        session=later_session,
        captured_at=datetime(2026, 7, 30, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, first)
    _promote(tmp_path, later)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
        forward_events=(
            _forward_evaluation_event(first),
            _forward_evaluation_event(later),
        ),
    )

    assert audit["status"] == "COMPLETE"
    assert audit["required_valuation_sessions"] == [
        SESSION.isoformat(),
        later_session.isoformat(),
    ]
    assert audit["missing_valuation_sessions"] == []
    assert audit["verified_forward_anchor_count"] == 2


def test_forward_anchor_identity_mismatch_invalidates_curve(tmp_path: Path) -> None:
    events: tuple[dict[str, object], ...] = ()
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
        forward_events=(
            _forward_evaluation_event(
                document,
                valuation_content_sha256="sha256:" + "f" * 64,
            ),
        ),
    )

    assert audit["status"] == "INVALID"
    assert audit["equity_curve_available"] is False
    assert audit["latest"] is None
    assert "anchor and promoted point disagree" in audit["invalid_evidence"][0][
        "reason"
    ]


def test_not_started_distinguishes_available_sources_from_verified_evidence(
    tmp_path: Path,
) -> None:
    without_sources = audit_human_paper_valuation_evidence(forward_root=tmp_path)
    assert without_sources["status"] == "NOT_STARTED"
    assert without_sources["source_provenance_available"] is False
    assert without_sources["source_provenance_verified"] is False

    with_sources = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=(),
        accounting_parameters=_parameters(),
    )
    assert with_sources["status"] == "NOT_STARTED"
    assert with_sources["source_provenance_available"] is True
    assert with_sources["source_provenance_verified"] is False


def test_open_position_uses_exact_1500_completed_one_minute_close() -> None:
    fill = _approved_buy_fill_event()
    accounting = _accounting((fill,), evidence="COMPLETE")
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256="sha256:" + "2" * 64,
        accounting=accounting,
        marks=(_mark(),),
        errors=(),
    )
    assert document["all_complete"] is True
    assert document["cash_balance"] == "998994.99"
    assert document["market_value"] == "1100.00"
    assert document["equity"] == "1000094.99"
    assert document["pnl_from_initial_cash"] == "94.99"
    assert document["performance_evaluable"] is False

    incomplete = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256="sha256:" + "2" * 64,
        accounting=accounting,
        marks=(),
        errors=({"symbol": "SH.600000", "reason": "CLOSE_BAR_MISSING"},),
    )
    assert incomplete["all_complete"] is False
    assert incomplete["equity"] is None
    assert incomplete["equity_curve_point_available"] is False


def test_valuation_alias_tampering_is_detected(tmp_path: Path) -> None:
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256="sha256:" + "3" * 64,
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)
    alias = tmp_path / "sessions" / SESSION.isoformat() / "paper_valuation.json"
    tampered = dict(document)
    tampered["equity"] = "999999.99"
    alias.write_text(json.dumps(tampered), encoding="utf-8")

    audit = audit_human_paper_valuation_evidence(forward_root=tmp_path)
    assert audit["status"] == "INVALID"
    assert audit["complete_valuation_count"] == 0
    assert audit["equity_curve_available"] is False


def test_invalid_later_alias_cannot_expose_an_older_point_as_latest(
    tmp_path: Path,
) -> None:
    events: tuple[dict[str, object], ...] = ()
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)
    broken_alias = tmp_path / "sessions" / "2026-07-29" / "paper_valuation.json"
    broken_alias.parent.mkdir(parents=True)
    broken_alias.write_text("{}", encoding="utf-8")

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
    )

    assert audit["status"] == "INVALID"
    assert audit["complete_valuation_count"] == 1
    assert len(audit["points"]) == 1
    assert audit["equity_curve_available"] is False
    assert audit["latest"] is None


def test_valuation_rejects_forged_accounting_and_rehashed_bad_totals() -> None:
    accounting = _accounting()
    forged_accounting = dict(accounting)
    forged_accounting["cash_balance"] = "999999.99"
    with pytest.raises(ValueError, match="accounting content hash mismatch"):
        build_human_paper_valuation_document(
            session=SESSION,
            captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
            paper_ledger_content_sha256="sha256:" + "4" * 64,
            accounting=forged_accounting,
            marks=(),
            errors=(),
        )

    valid = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256="sha256:" + "5" * 64,
        accounting=accounting,
        marks=(),
        errors=(),
    )
    forged_totals = dict(valid)
    forged_totals["equity"] = "1000001.00"
    forged_totals["pnl_from_initial_cash"] = "1.00"
    stable = dict(forged_totals)
    stable.pop("content_sha256")
    forged_totals["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="valuation is inconsistent"):
        validate_human_paper_valuation_document(forged_totals)


def test_valuation_audit_without_ledger_source_is_explicitly_unverified(
    tmp_path: Path,
) -> None:
    events: tuple[dict[str, object], ...] = ()
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(forward_root=tmp_path)

    assert audit["status"] == "SOURCE_UNVERIFIED"
    assert audit["source_provenance_verified"] is False
    assert audit["equity_curve_available"] is False
    assert audit["complete_valuation_count"] == 0


@pytest.mark.parametrize(
    "forgery",
    ("quantity", "price_and_aggregate", "corporate_action", "instrument_name"),
)
def test_rehashed_mark_must_match_frozen_ledger_and_execution_sources(
    monkeypatch,
    tmp_path: Path,
    forgery: str,
) -> None:
    fill = _approved_buy_fill_event()
    events = (fill,)
    monkeypatch.setattr(
        valuation_module,
        "audit_human_paper_execution_evidence",
        lambda *_args, **_kwargs: {"status": "COMPLETE"},
    )
    accounting = _accounting(events, evidence="COMPLETE")
    valid = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=accounting,
        marks=(_mark(),),
        errors=(),
    )
    _promote_execution_capture(tmp_path)
    _promote(tmp_path, valid)
    valid_audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
        forward_events=(_forward_evaluation_event(valid),),
    )
    assert valid_audit["status"] == "COMPLETE"

    forged = deepcopy(valid)
    mark = forged["marks"][0]
    if forgery == "quantity":
        mark["quantity"] = 200
        mark["market_value"] = "2200.00"
    elif forgery == "price_and_aggregate":
        mark["close"] = "11.05"
        mark["market_value"] = "1105.00"
    elif forgery == "corporate_action":
        mark["instrument_fact"]["corporate_actions"] = [
            {
                "effective_on": SESSION.isoformat(),
                "interest": "0",
                "stock_bonus": "0",
                "stock_gift": "0",
                "allot_num": "0",
                "allot_price": "0",
                "gugai": "0",
                "raw_price_divisor": "1",
            }
        ]
    else:
        mark["instrument_fact"]["instrument_name"] = "另一家银行"
    forged_market_value = Decimal(str(mark["market_value"]))
    forged_equity = Decimal(str(forged["cash_balance"])) + forged_market_value
    forged["market_value"] = format(forged_market_value, "f")
    forged["equity"] = format(forged_equity, "f")
    forged["pnl_from_initial_cash"] = format(
        forged_equity - Decimal(str(forged["initial_cash"])),
        "f",
    )
    stable = dict(forged)
    stable.pop("content_sha256")
    forged["content_sha256"] = sha256_json(stable)
    assert validate_human_paper_valuation_document(forged) == forged
    _promote(tmp_path, forged)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
    )
    assert audit["status"] == "INVALID"
    assert audit["complete_valuation_count"] == 0
    assert audit["equity_curve_available"] is False


@pytest.mark.parametrize("forgery", ("ledger_identity", "accounting_cash"))
def test_rehashed_valuation_must_resolve_real_ledger_and_accounting_prefix(
    tmp_path: Path,
    forgery: str,
) -> None:
    events: tuple[dict[str, object], ...] = ()
    accounting = _accounting()
    ledger_identity = human_paper_ledger_content_sha256(events)
    if forgery == "ledger_identity":
        ledger_identity = "sha256:" + "f" * 64
    else:
        accounting = dict(accounting)
        accounting["cash_balance"] = "999000.00"
        stable_accounting = dict(accounting)
        stable_accounting.pop("content_sha256")
        accounting["content_sha256"] = sha256_json(stable_accounting)
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=ledger_identity,
        accounting=accounting,
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
    )

    assert audit["status"] == "INVALID"
    assert audit["complete_valuation_count"] == 0
    assert audit["source_provenance_verified"] is False


def test_unapproved_buy_cannot_back_equity(
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
    events = (fill,)
    monkeypatch.setattr(
        valuation_module,
        "audit_human_paper_execution_evidence",
        lambda *_args, **_kwargs: {"status": "COMPLETE"},
    )
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(events, evidence="COMPLETE"),
        marks=(_mark(),),
        errors=(),
    )
    _promote_execution_capture(tmp_path)
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
    )

    assert audit["status"] == "INVALID"
    assert "unattested portfolio BUY fill" in audit["invalid_evidence"][0][
        "reason"
    ]


def test_invalid_portfolio_buy_approval_cannot_back_equity(
    monkeypatch,
    tmp_path: Path,
) -> None:
    fill = deepcopy(_approved_buy_fill_event())
    fill["payload"]["available_cash"] = "999999.99"
    events = (fill,)
    monkeypatch.setattr(
        valuation_module,
        "audit_human_paper_execution_evidence",
        lambda *_args, **_kwargs: {"status": "COMPLETE"},
    )
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(events, evidence="COMPLETE"),
        marks=(_mark(),),
        errors=(),
    )
    _promote_execution_capture(tmp_path)
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
    )

    assert audit["status"] == "INVALID"
    assert "unattested portfolio BUY fill" in audit["invalid_evidence"][0][
        "reason"
    ]


def test_valuation_prefix_cannot_omit_an_event_effective_before_capture(
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
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        # This is a real historical prefix, but it dishonestly omits the fill
        # that was already effective before the valuation capture.
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(()),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=(fill,),
        accounting_parameters=_parameters(),
    )

    assert audit["status"] == "INVALID"
    assert "omits a causal event" in audit["invalid_evidence"][0]["reason"]


def test_valuation_prefix_may_precede_a_genuinely_future_event(tmp_path: Path) -> None:
    future_fill = {
        "kind": "FILL",
        "payload": {
            "symbol": "SH.600000",
            "side": "BUY",
            "quantity": 100,
            "price": "10.00",
            "filled_at": "2026-07-29T10:01:00+08:00",
        },
    }
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(()),
        accounting=_accounting(),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=(future_fill,),
        accounting_parameters=_parameters(),
        forward_events=(_forward_evaluation_event(document),),
    )

    assert audit["status"] == "COMPLETE"
    assert audit["source_provenance_verified"] is True


def test_valuation_prefix_cannot_include_a_future_event(tmp_path: Path) -> None:
    future_intent = {
        "kind": "INTENT",
        "payload": {"created_at": "2026-07-29T09:31:00+08:00"},
    }
    events = (future_intent,)
    document = build_human_paper_valuation_document(
        session=SESSION,
        captured_at=datetime(2026, 7, 28, 15, 20, tzinfo=TZ),
        paper_ledger_content_sha256=human_paper_ledger_content_sha256(events),
        accounting=_accounting(events),
        marks=(),
        errors=(),
    )
    _promote(tmp_path, document)

    audit = audit_human_paper_valuation_evidence(
        forward_root=tmp_path,
        paper_events=events,
        accounting_parameters=_parameters(),
    )

    assert audit["status"] == "INVALID"
    assert "contains a future event" in audit["invalid_evidence"][0]["reason"]
