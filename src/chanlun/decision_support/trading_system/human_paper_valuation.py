"""Immutable end-of-day valuation evidence for the human virtual book."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
from pathlib import Path
import re
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.market_rules import is_st_name
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    qmt_native_code,
)
from chanlun.decision_support.trading_system.human_paper_accounting import (
    HumanPaperAccountingParameters,
    audit_human_paper_portfolio_fill_decisions,
    rebuild_human_paper_accounting,
)
from chanlun.decision_support.trading_system.human_paper_ledger import (
    audit_human_paper_execution_evidence,
    human_paper_ledger_prefix_for_identity,
    load_human_paper_execution_capture,
)


VALUATION_SCHEMA = "chanlun-human-paper-valuation"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_CENT = Decimal("0.01")


def _validate_production_valuation_mark(
    raw: Mapping[str, object],
    *,
    session: date,
    expected_quantity: int | None = None,
    expected_oldest_acquired_session: str | None = None,
) -> None:
    """Recompute one production close mark from its raw QMT facts.

    A content hash protects bytes, not truth.  This verifier therefore derives
    the security flags, expiry state, company-action window, source identity,
    OHLC envelope and market value again instead of trusting the convenient
    booleans stored beside them.
    """

    required_mark_fields = {
        "symbol",
        "quantity",
        "opened_at",
        "closed_at",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "market_value",
        "complete",
        "suspended",
        "security_status_complete",
        "corporate_action_state_complete",
        "oldest_acquired_session",
        "instrument_fact",
        "qmt_transport",
        "qmt_local_cache_source_sha256",
    }
    if not required_mark_fields.issubset(raw):
        raise ValueError("paper valuation mark source facts are incomplete")
    fact = raw.get("instrument_fact")
    if not isinstance(fact, Mapping):
        raise ValueError("paper valuation instrument fact is malformed")
    symbol = str(raw.get("symbol") or "")
    try:
        quantity = int(raw["quantity"])
        opened_at = normalize_datetime(
            datetime.fromisoformat(str(raw["opened_at"])),
            "opened_at",
        )
        closed_at = normalize_datetime(
            datetime.fromisoformat(str(raw["closed_at"])),
            "closed_at",
        )
        open_price = Decimal(str(raw["open"]))
        high = Decimal(str(raw["high"]))
        low = Decimal(str(raw["low"]))
        close = Decimal(str(raw["close"]))
        volume = Decimal(str(raw["volume"]))
        market_value = Decimal(str(raw["market_value"]))
        acquired_on = date.fromisoformat(str(raw["oldest_acquired_session"]))
        instrument_status = int(fact["instrument_status"])
        pre_close = Decimal(str(fact["pre_close"]))
        limit_up = Decimal(str(fact["limit_up"]))
        limit_down = Decimal(str(fact["limit_down"]))
        price_tick = Decimal(str(fact["price_tick"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("paper valuation mark source facts are malformed") from exc
    prices = (open_price, high, low, close, pre_close, limit_up, limit_down, price_tick)
    expected_value = (Decimal(quantity) * close).quantize(
        _CENT,
        rounding=ROUND_HALF_UP,
    )
    if (
        not symbol
        or quantity <= 0
        or (expected_quantity is not None and quantity != expected_quantity)
        or opened_at.date() != session
        or opened_at.timetz().replace(tzinfo=None) != time(14, 59)
        or closed_at.date() != session
        or closed_at.timetz().replace(tzinfo=None) != time(15)
        or closed_at - opened_at != timedelta(minutes=1)
        or any(not value.is_finite() or value <= 0 for value in prices)
        or not volume.is_finite()
        or volume < 0
        or low > min(open_price, close)
        or high < max(open_price, close)
        or low > high
        or limit_down >= limit_up
        or low < limit_down
        or high > limit_up
        or market_value != expected_value
        or acquired_on > session
        or (
            expected_oldest_acquired_session is not None
            and acquired_on.isoformat() != expected_oldest_acquired_session
        )
    ):
        raise ValueError("paper valuation OHLCV or position facts are invalid")

    transport = raw.get("qmt_transport")
    cache_identity = raw.get("qmt_local_cache_source_sha256")
    if transport == "LOCAL_FIXED_RECORD_READ_ONLY":
        if not isinstance(cache_identity, str) or _SHA256.fullmatch(cache_identity) is None:
            raise ValueError("paper valuation local QMT source identity is invalid")
    elif transport == "RPC":
        if cache_identity is not None:
            raise ValueError("paper valuation RPC source cannot claim a local cache")
    else:
        raise ValueError("paper valuation QMT transport is invalid")

    expected_fact_fields = {
        "symbol",
        "native_code",
        "session",
        "trading_day",
        "instrument_name",
        "instrument_status",
        "is_trading",
        "suspended",
        "expired",
        "expiry_date",
        "is_st",
        "pre_close",
        "limit_up",
        "limit_down",
        "price_tick",
        "corporate_actions",
        "source_methods",
        "tick_data_used",
        "account_api_used",
    }
    if set(fact) != expected_fact_fields:
        raise ValueError("paper valuation instrument fact fields changed")
    boolean_fields = (
        "is_trading",
        "suspended",
        "expired",
        "is_st",
        "tick_data_used",
        "account_api_used",
    )
    if any(type(fact.get(name)) is not bool for name in boolean_fields):
        raise ValueError("paper valuation instrument fact booleans are malformed")
    if type(fact.get("instrument_status")) is not int or instrument_status < 0:
        raise ValueError("paper valuation instrument status is malformed")
    name = str(fact.get("instrument_name") or "")
    expiry_value = fact.get("expiry_date")
    if expiry_value is None:
        expected_expired = False
    elif isinstance(expiry_value, str):
        expected_expired = date.fromisoformat(expiry_value) < session
    else:
        raise ValueError("paper valuation expiry date is malformed")
    if (
        not name
        or fact.get("symbol") != symbol
        or fact.get("native_code") != qmt_native_code(symbol)
        or fact.get("session") != session.isoformat()
        or fact.get("trading_day") != session.isoformat()
        or fact.get("source_methods")
        != ["QMT_GET_INSTRUMENT_DETAIL", "QMT_GET_DIVID_FACTORS"]
        or fact.get("suspended") is not (instrument_status >= 1)
        or fact.get("expired") is not expected_expired
        or fact.get("is_st") is not is_st_name(name)
        or fact.get("tick_data_used") is not False
        or fact.get("account_api_used") is not False
        or raw.get("suspended") is not fact.get("suspended")
        or raw.get("complete") is not True
        or raw.get("security_status_complete") is not True
        or raw.get("corporate_action_state_complete") is not True
    ):
        raise ValueError("paper valuation security facts cannot be recomputed")

    actions = fact.get("corporate_actions")
    if not isinstance(actions, list):
        raise ValueError("paper valuation corporate actions are malformed")
    action_fields = {
        "effective_on",
        "interest",
        "stock_bonus",
        "stock_gift",
        "allot_num",
        "allot_price",
        "gugai",
        "raw_price_divisor",
    }
    action_sessions: list[date] = []
    for action in actions:
        if not isinstance(action, Mapping) or set(action) != action_fields:
            raise ValueError("paper valuation corporate action row is malformed")
        try:
            action_on = date.fromisoformat(str(action["effective_on"]))
            numeric = tuple(
                Decimal(str(action[name]))
                for name in action_fields
                if name != "effective_on"
            )
            divisor = Decimal(str(action["raw_price_divisor"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("paper valuation corporate action row is invalid") from exc
        if (
            action_on < acquired_on
            or action_on > session
            or any(not value.is_finite() for value in numeric)
            or divisor <= 0
        ):
            raise ValueError("paper valuation corporate action facts are invalid")
        action_sessions.append(action_on)
    if action_sessions != sorted(set(action_sessions)):
        raise ValueError("paper valuation corporate action order is invalid")
    if any(acquired_on < value <= session for value in action_sessions):
        raise ValueError("paper valuation position requires corporate action reconciliation")


def _validate_valuation_against_ledger_prefix(
    payload: Mapping[str, object],
    *,
    paper_events: Sequence[Mapping[str, object]],
    accounting_parameters: HumanPaperAccountingParameters,
    forward_root: Path,
) -> None:
    prefix = human_paper_ledger_prefix_for_identity(
        paper_events,
        content_sha256=str(payload["paper_ledger_content_sha256"]),
        observed_at=normalize_datetime(
            datetime.fromisoformat(str(payload["captured_at"])),
            "captured_at",
        ),
    )
    execution_audit = audit_human_paper_execution_evidence(
        prefix,
        forward_root=forward_root,
    )
    accounting = rebuild_human_paper_accounting(
        prefix,
        parameters=accounting_parameters,
        execution_evidence_status=str(execution_audit.get("status") or "INVALID"),
    )
    portfolio_fill_audit = audit_human_paper_portfolio_fill_decisions(
        prefix,
        parameters=accounting_parameters,
    )
    buy_fill_count = sum(
        event.get("kind") == "FILL"
        and isinstance(event.get("payload"), Mapping)
        and event["payload"].get("side") == "BUY"
        for event in prefix
    )
    if (
        portfolio_fill_audit.get("status")
        not in {"NO_APPROVED_FILLS", "COMPLETE"}
        or int(portfolio_fill_audit.get("approved_fill_count") or 0)
        != buy_fill_count
        or int(portfolio_fill_audit.get("verified_approved_fill_count") or 0)
        != buy_fill_count
    ):
        raise ValueError(
            "paper valuation contains an unattested portfolio BUY fill"
        )
    positions = accounting.get("positions")
    if not isinstance(positions, Mapping):
        raise ValueError("paper valuation reconstructed positions are invalid")
    if (
        payload.get("accounting_content_sha256") != accounting["content_sha256"]
        or payload.get("accounting_contract_id") != accounting["accounting_contract_id"]
        or Decimal(str(payload["initial_cash"]))
        != Decimal(str(accounting["initial_cash"])).quantize(
            _CENT,
            rounding=ROUND_HALF_UP,
        )
        or Decimal(str(payload["cash_balance"]))
        != Decimal(str(accounting["cash_balance"])).quantize(
            _CENT,
            rounding=ROUND_HALF_UP,
        )
        or int(payload["position_count"]) != len(positions)
    ):
        raise ValueError("paper valuation accounting provenance changed")
    marks = payload.get("marks")
    if not isinstance(marks, list):
        raise ValueError("paper valuation marks are malformed")
    marks_by_symbol = {
        str(value.get("symbol") or ""): value
        for value in marks
        if isinstance(value, Mapping)
    }
    if set(marks_by_symbol) != set(str(value) for value in positions):
        raise ValueError("paper valuation position mark coverage changed")
    session = date.fromisoformat(str(payload["session"]))
    evidence: Mapping[str, object] | None = None
    facts: Mapping[str, object] | None = None
    bars_by_symbol: Mapping[str, object] = {}
    facts_by_symbol: dict[str, Mapping[str, object]] = {}
    if positions:
        evidence, facts = load_human_paper_execution_capture(
            forward_root=forward_root,
            session=session,
        )
        evidence_captured_at = normalize_datetime(
            datetime.fromisoformat(str(evidence["captured_at"])),
            "captured_at",
        )
        valuation_captured_at = normalize_datetime(
            datetime.fromisoformat(str(payload["captured_at"])),
            "captured_at",
        )
        if evidence_captured_at > valuation_captured_at:
            raise ValueError("paper valuation predates its execution capture")
        raw_bars = evidence.get("bars_by_symbol")
        raw_facts = facts.get("symbols")
        if not isinstance(raw_bars, Mapping) or not isinstance(raw_facts, list):
            raise ValueError("paper valuation execution source is malformed")
        bars_by_symbol = raw_bars
        for raw_fact in raw_facts:
            if not isinstance(raw_fact, Mapping):
                raise ValueError("paper valuation execution fact is malformed")
            fact_symbol = str(raw_fact.get("symbol") or "")
            if not fact_symbol or fact_symbol in facts_by_symbol:
                raise ValueError("paper valuation execution fact identity changed")
            facts_by_symbol[fact_symbol] = raw_fact
    for symbol, position in positions.items():
        if not isinstance(position, Mapping):
            raise ValueError("paper valuation reconstructed position is malformed")
        symbol_text = str(symbol)
        mark = marks_by_symbol[symbol_text]
        _validate_production_valuation_mark(
            mark,
            session=session,
            expected_quantity=int(position["quantity"]),
            expected_oldest_acquired_session=str(
                position["oldest_acquired_session"]
            ),
        )
        source_bars = bars_by_symbol.get(symbol_text)
        source_fact = facts_by_symbol.get(symbol_text)
        if not isinstance(source_bars, list) or source_fact is None:
            raise ValueError("paper valuation has no frozen execution source")
        exact_close_bars = tuple(
            value
            for value in source_bars
            if isinstance(value, Mapping)
            and value.get("opened_at") == mark.get("opened_at")
            and value.get("closed_at") == mark.get("closed_at")
        )
        if len(exact_close_bars) != 1:
            raise ValueError("paper valuation does not resolve one frozen close bar")
        source_bar = exact_close_bars[0]
        for name in ("open", "high", "low", "close", "volume"):
            if Decimal(str(mark[name])) != Decimal(str(source_bar.get(name))):
                raise ValueError("paper valuation price differs from frozen 1m grid")
        for name in (
            "symbol",
            "opened_at",
            "closed_at",
            "complete",
            "suspended",
            "security_status_complete",
            "corporate_action_state_complete",
        ):
            if mark.get(name) != source_bar.get(name):
                raise ValueError("paper valuation bar facts differ from execution source")

        mark_fact = mark.get("instrument_fact")
        if not isinstance(mark_fact, Mapping):
            raise ValueError("paper valuation instrument fact is malformed")
        fact_fields = set(mark_fact) - {"corporate_actions"}
        if any(mark_fact.get(name) != source_fact.get(name) for name in fact_fields):
            raise ValueError("paper valuation instrument fact differs from execution source")
        acquired_on = date.fromisoformat(
            str(position["oldest_acquired_session"])
        )
        source_actions = source_fact.get("corporate_actions")
        mark_actions = mark_fact.get("corporate_actions")
        if not isinstance(source_actions, list) or not isinstance(mark_actions, list):
            raise ValueError("paper valuation corporate action source is malformed")
        relevant_source_actions = [
            dict(value)
            for value in source_actions
            if isinstance(value, Mapping)
            and date.fromisoformat(str(value["effective_on"])) >= acquired_on
        ]
        if mark_actions != relevant_source_actions:
            raise ValueError("paper valuation corporate actions differ from execution source")


def validate_human_paper_valuation_sources(
    payload: Mapping[str, object],
    *,
    paper_events: Sequence[Mapping[str, object]],
    accounting_parameters: HumanPaperAccountingParameters,
    forward_root: Path,
) -> dict[str, object]:
    """Validate one complete close point against external immutable sources."""

    value = validate_human_paper_valuation_document(payload)
    if value.get("all_complete") is not True:
        raise ValueError("incomplete human paper valuation has no source-backed point")
    _validate_valuation_against_ledger_prefix(
        value,
        paper_events=paper_events,
        accounting_parameters=accounting_parameters,
        forward_root=forward_root,
    )
    return value


def build_human_paper_valuation_document(
    *,
    session: date,
    captured_at: datetime,
    paper_ledger_content_sha256: str,
    accounting: Mapping[str, object],
    marks: Sequence[Mapping[str, object]],
    errors: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build one close mark; incomplete facts produce no equity value."""

    captured = normalize_datetime(captured_at, "captured_at")
    if captured.date() != session or captured.timetz().replace(tzinfo=None) < time(15):
        raise ValueError("paper valuation must be captured after the same-session close")
    if _SHA256.fullmatch(paper_ledger_content_sha256) is None:
        raise ValueError("paper valuation ledger identity is invalid")
    accounting_id = str(accounting.get("content_sha256") or "")
    if _SHA256.fullmatch(accounting_id) is None:
        raise ValueError("paper valuation accounting identity is invalid")
    accounting_stable = dict(accounting)
    accounting_stable.pop("content_sha256", None)
    if sha256_json(accounting_stable) != accounting_id:
        raise ValueError("paper valuation accounting content hash mismatch")
    if (
        accounting.get("tick_data_used") is not False
        or accounting.get("broker_transport_available") is not False
        or accounting.get("live_status") != "LIVE_DISABLED"
    ):
        raise ValueError("paper valuation accounting safety boundary changed")
    positions = accounting.get("positions")
    if not isinstance(positions, Mapping):
        raise ValueError("paper valuation accounting positions are invalid")
    try:
        cash = Decimal(str(accounting["cash_balance"]))
        initial_cash = Decimal(str(accounting["initial_cash"]))
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("paper valuation cash is invalid") from exc
    if not cash.is_finite() or not initial_cash.is_finite() or initial_cash <= 0:
        raise ValueError("paper valuation cash must be finite")

    normalized_marks: list[dict[str, object]] = []
    seen: set[str] = set()
    market_value = Decimal("0")
    for raw in marks:
        symbol = str(raw.get("symbol") or "")
        if not symbol or symbol in seen or symbol not in positions:
            raise ValueError("paper valuation mark symbol is invalid")
        position = positions[symbol]
        if not isinstance(position, Mapping):
            raise ValueError("paper valuation position is invalid")
        _validate_production_valuation_mark(
            raw,
            session=session,
            expected_quantity=int(position.get("quantity") or 0),
            expected_oldest_acquired_session=str(
                position.get("oldest_acquired_session") or ""
            ),
        )
        try:
            quantity = int(raw["quantity"])
            opened_at = normalize_datetime(
                datetime.fromisoformat(str(raw["opened_at"])),
                "opened_at",
            )
            close = Decimal(str(raw["close"]))
            closed_at = normalize_datetime(
                datetime.fromisoformat(str(raw["closed_at"])),
                "closed_at",
            )
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("paper valuation mark is malformed") from exc
        if (
            quantity != int(position.get("quantity") or 0)
            or quantity <= 0
            or close <= 0
            or not close.is_finite()
            or opened_at.date() != session
            or opened_at.timetz().replace(tzinfo=None) != time(14, 59)
            or closed_at.date() != session
            or closed_at.timetz().replace(tzinfo=None) != time(15)
            or closed_at - opened_at != timedelta(minutes=1)
            or raw.get("complete") is not True
            or raw.get("security_status_complete") is not True
            or raw.get("corporate_action_state_complete") is not True
            or raw.get("suspended") is not False
        ):
            raise ValueError("paper valuation mark facts are incomplete")
        value = (Decimal(quantity) * close).quantize(_CENT, rounding=ROUND_HALF_UP)
        if Decimal(str(raw.get("market_value"))) != value:
            raise ValueError("paper valuation market value changed")
        normalized_marks.append(dict(raw))
        market_value += value
        seen.add(symbol)

    normalized_errors = [dict(value) for value in errors]
    expected_symbols = set(str(value) for value in positions)
    all_complete = (
        bool(accounting.get("accounting_valid"))
        and not normalized_errors
        and seen == expected_symbols
    )
    equity = (
        (cash + market_value).quantize(_CENT, rounding=ROUND_HALF_UP)
        if all_complete
        else None
    )
    pnl = None if equity is None else equity - initial_cash
    stable: dict[str, object] = {
        "schema": VALUATION_SCHEMA,
        "session": session.isoformat(),
        "captured_at": captured.isoformat(),
        "paper_ledger_content_sha256": paper_ledger_content_sha256,
        "accounting_content_sha256": accounting_id,
        "accounting_contract_id": accounting.get("accounting_contract_id"),
        "valuation_model": "LAST_COMPLETED_1M_BAR_CLOSE_AT_15_00",
        "initial_cash": format(
            initial_cash.quantize(_CENT, rounding=ROUND_HALF_UP), "f"
        ),
        "cash_balance": format(cash.quantize(_CENT, rounding=ROUND_HALF_UP), "f"),
        "market_value": format(
            market_value.quantize(_CENT, rounding=ROUND_HALF_UP), "f"
        ),
        "equity": None if equity is None else format(equity, "f"),
        "pnl_from_initial_cash": None if pnl is None else format(pnl, "f"),
        "position_count": len(positions),
        "marks": normalized_marks,
        "errors": normalized_errors,
        "all_complete": all_complete,
        "equity_curve_point_available": all_complete,
        "performance_evaluable": False,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "account_api_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def validate_human_paper_valuation_document(
    payload: Mapping[str, object],
) -> dict[str, object]:
    if payload.get("schema") != VALUATION_SCHEMA:
        raise ValueError("unsupported human paper valuation schema")
    claimed = str(payload.get("content_sha256") or "")
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if _SHA256.fullmatch(claimed) is None or claimed != sha256_json(stable):
        raise ValueError("human paper valuation content hash mismatch")
    if (
        payload.get("valuation_model") != "LAST_COMPLETED_1M_BAR_CLOSE_AT_15_00"
        or payload.get("minimum_market_data_frequency") != "1m"
        or payload.get("tick_data_used") is not False
        or payload.get("account_api_used") is not False
        or payload.get("broker_transport_available") is not False
        or payload.get("live_status") != "LIVE_DISABLED"
        or payload.get("performance_evaluable") is not False
    ):
        raise ValueError("human paper valuation safety boundary changed")
    try:
        session = date.fromisoformat(str(payload["session"]))
        captured_at = normalize_datetime(
            datetime.fromisoformat(str(payload["captured_at"])),
            "captured_at",
        )
        initial_cash = Decimal(str(payload["initial_cash"]))
        cash = Decimal(str(payload["cash_balance"]))
        market_value = Decimal(str(payload["market_value"]))
        position_count = int(payload["position_count"])
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("human paper valuation totals are malformed") from exc
    if (
        captured_at.date() != session
        or captured_at.timetz().replace(tzinfo=None) < time(15)
        or not all(value.is_finite() for value in (initial_cash, cash, market_value))
        or initial_cash <= 0
        or market_value < 0
        or position_count < 0
        or _SHA256.fullmatch(str(payload.get("paper_ledger_content_sha256") or ""))
        is None
        or _SHA256.fullmatch(str(payload.get("accounting_content_sha256") or ""))
        is None
    ):
        raise ValueError("human paper valuation totals are invalid")
    marks = payload.get("marks")
    errors = payload.get("errors")
    if not isinstance(marks, list) or not isinstance(errors, list):
        raise ValueError("human paper valuation facts are malformed")
    seen: set[str] = set()
    recomputed_market_value = Decimal("0")
    for raw in marks:
        if not isinstance(raw, Mapping):
            raise ValueError("human paper valuation mark is malformed")
        try:
            symbol = str(raw["symbol"])
            quantity = int(raw["quantity"])
            opened_at = normalize_datetime(
                datetime.fromisoformat(str(raw["opened_at"])),
                "opened_at",
            )
            closed_at = normalize_datetime(
                datetime.fromisoformat(str(raw["closed_at"])),
                "closed_at",
            )
            close = Decimal(str(raw["close"]))
            claimed_value = Decimal(str(raw["market_value"]))
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise ValueError("human paper valuation mark is malformed") from exc
        expected_value = (Decimal(quantity) * close).quantize(
            _CENT, rounding=ROUND_HALF_UP
        )
        if (
            not symbol
            or symbol in seen
            or quantity <= 0
            or not close.is_finite()
            or close <= 0
            or opened_at.date() != session
            or opened_at.timetz().replace(tzinfo=None) != time(14, 59)
            or closed_at.date() != session
            or closed_at.timetz().replace(tzinfo=None) != time(15)
            or closed_at - opened_at != timedelta(minutes=1)
            or raw.get("complete") is not True
            or raw.get("security_status_complete") is not True
            or raw.get("corporate_action_state_complete") is not True
            or raw.get("suspended") is not False
            or claimed_value != expected_value
        ):
            raise ValueError("human paper valuation mark facts are invalid")
        _validate_production_valuation_mark(raw, session=session)
        seen.add(symbol)
        recomputed_market_value += expected_value
    if (
        recomputed_market_value.quantize(_CENT, rounding=ROUND_HALF_UP)
        != market_value
    ):
        raise ValueError("human paper valuation market value is inconsistent")
    if payload.get("all_complete") is True:
        try:
            equity = Decimal(str(payload["equity"]))
            pnl = Decimal(str(payload["pnl_from_initial_cash"]))
        except (InvalidOperation, KeyError, TypeError) as exc:
            raise ValueError("complete human paper valuation totals are invalid") from exc
        if (
            payload.get("equity_curve_point_available") is not True
            or errors != []
            or len(marks) != position_count
            or equity
            != (cash + market_value).quantize(_CENT, rounding=ROUND_HALF_UP)
            or pnl != equity - initial_cash
        ):
            raise ValueError("complete human paper valuation is inconsistent")
    elif (
        payload.get("all_complete") is not False
        or payload.get("equity_curve_point_available") is not False
        or payload.get("equity") is not None
        or payload.get("pnl_from_initial_cash") is not None
    ):
        raise ValueError("incomplete human paper valuation cannot expose equity")
    return dict(payload)


def _audit_forward_valuation_continuity(
    *,
    forward_events: Sequence[Mapping[str, object]] | None,
    points: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Bind promoted close points to successful daily forward decisions.

    This deliberately audits *executed forward sessions*, not guessed
    weekdays.  The forward ledger has already passed its contract/hash-chain
    validator at both production call sites.  Rechecking the relevant event
    and evidence hashes here protects direct callers and makes the valuation
    audit independently fail closed if an in-memory event is altered.

    Every successful decision must carry one matching
    ``VALUATION_COMPLETE`` anchor and every point must have one such anchor.
    """

    point_identities = {
        str(point["session"]): str(point["content_sha256"])
        for point in points
    }
    source_available = forward_events is not None
    if forward_events is None:
        return {
            "status": (
                "CONTINUITY_UNVERIFIED" if point_identities else "NOT_STARTED"
            ),
            "forward_anchor_available": False,
            "required_valuation_sessions": [],
            "missing_valuation_sessions": [],
            "unanchored_valuation_sessions": sorted(point_identities),
            "verified_forward_anchor_count": 0,
            "invalid_evidence": [],
        }

    anchors: dict[str, str] = {}
    invalid: list[dict[str, str]] = []
    for index, event in enumerate(forward_events):
        if not isinstance(event, Mapping):
            invalid.append(
                {
                    "path": f"forward_event[{index}]",
                    "reason": "TypeError: forward event is malformed",
                }
            )
            continue
        if event.get("phase") != "DECISION" or event.get("status") != "EVALUATED":
            continue
        try:
            session = date.fromisoformat(str(event["session"]))
        except (KeyError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "path": f"forward_event[{index}]",
                    "reason": f"{type(exc).__name__}: evaluated session is invalid",
                }
            )
            continue
        session_text = session.isoformat()
        evidence = event.get("evidence")
        valuation = (
            evidence.get("human_paper_valuation")
            if isinstance(evidence, Mapping)
            else None
        )
        try:
            if event.get("schema") != "chanlun-forward-paper-event":
                raise ValueError("unsupported forward event schema")
            event_identity = str(event.get("event_sha256") or "")
            event_stable = {
                key: value for key, value in event.items() if key != "event_sha256"
            }
            if (
                _SHA256.fullmatch(event_identity) is None
                or sha256_json(event_stable) != event_identity
            ):
                raise ValueError("forward event content hash changed")
            if not isinstance(evidence, Mapping):
                raise ValueError("forward event evidence is malformed")
            evidence_identity = str(event.get("evidence_sha256") or "")
            if (
                _SHA256.fullmatch(evidence_identity) is None
                or sha256_json(dict(evidence)) != evidence_identity
            ):
                raise ValueError("forward event evidence hash changed")
            if (
                event.get("real_account_accessed") is not False
                or event.get("real_order_transport_enabled") is not False
                or event.get("live_status") != "LIVE_DISABLED"
            ):
                raise ValueError("forward event safety status changed")
            if not isinstance(valuation, Mapping):
                raise ValueError("successful forward decision lacks valuation anchor")
            valuation_identity = str(
                valuation.get("valuation_content_sha256") or ""
            )
            if (
                valuation.get("status") != "VALUATION_COMPLETE"
                or valuation.get("session") != session_text
                or _SHA256.fullmatch(valuation_identity) is None
            ):
                raise ValueError("forward valuation anchor is inconsistent")
            if session_text in anchors:
                raise ValueError("duplicate successful valuation session")
            anchors[session_text] = valuation_identity
        except (TypeError, ValueError) as exc:
            invalid.append(
                {
                    "path": f"forward_event[{index}]",
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )

    required = sorted(anchors)
    missing = sorted(set(anchors) - set(point_identities))
    unanchored = sorted(set(point_identities) - set(anchors))
    verified_count = 0
    for session_text in sorted(set(anchors) & set(point_identities)):
        if anchors[session_text] != point_identities[session_text]:
            invalid.append(
                {
                    "path": f"forward_session:{session_text}",
                    "reason": "ValueError: valuation anchor and promoted point disagree",
                }
            )
        else:
            verified_count += 1

    if invalid:
        status = "INVALID"
    elif missing or unanchored:
        status = "INCOMPLETE_CURVE"
    elif point_identities:
        status = "COMPLETE"
    else:
        status = "NOT_STARTED"
    return {
        "status": status,
        "forward_anchor_available": source_available,
        "required_valuation_sessions": required,
        "missing_valuation_sessions": missing,
        "unanchored_valuation_sessions": unanchored,
        "verified_forward_anchor_count": verified_count,
        "invalid_evidence": invalid,
    }


def audit_human_paper_valuation_evidence(
    *,
    forward_root: Path,
    paper_events: Sequence[Mapping[str, object]] | None = None,
    accounting_parameters: HumanPaperAccountingParameters | None = None,
    forward_events: Sequence[Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Validate promoted session aliases and their immutable semantic objects."""

    if (paper_events is None) is not (accounting_parameters is None):
        raise ValueError(
            "paper valuation source audit requires both ledger and parameters"
        )
    source_provenance_available = (
        paper_events is not None and accounting_parameters is not None
    )

    aliases = sorted((forward_root / "sessions").glob("*/paper_valuation.json"))
    points: list[dict[str, object]] = []
    invalid: list[dict[str, str]] = []
    source_unverified: list[dict[str, str]] = []
    seen_sessions: set[str] = set()
    for alias in aliases:
        try:
            raw = json.loads(alias.read_text(encoding="utf-8"))
            if not isinstance(raw, Mapping):
                raise ValueError("valuation alias is malformed")
            value = validate_human_paper_valuation_document(raw)
            identity = str(value["content_sha256"])
            object_path = (
                alias.parent
                / "objects"
                / "paper_valuation"
                / f"{identity[7:]}.json"
            )
            if not object_path.is_file():
                raise ValueError("immutable valuation object is missing")
            immutable = json.loads(object_path.read_text(encoding="utf-8"))
            if immutable != raw:
                raise ValueError("valuation alias and immutable object disagree")
            session = str(value["session"])
            if alias.parent.name != session or session in seen_sessions:
                raise ValueError("valuation session identity is invalid")
            if value.get("all_complete") is not True:
                raise ValueError("incomplete valuation alias was promoted")
            if not source_provenance_available:
                source_unverified.append(
                    {
                        "path": str(alias),
                        "reason": "LEDGER_AND_ACCOUNTING_SOURCE_NOT_PROVIDED",
                    }
                )
                continue
            assert paper_events is not None
            assert accounting_parameters is not None
            value = validate_human_paper_valuation_sources(
                value,
                paper_events=paper_events,
                accounting_parameters=accounting_parameters,
                forward_root=forward_root,
            )
            points.append(
                {
                    "session": session,
                    "captured_at": value["captured_at"],
                    "cash_balance": value["cash_balance"],
                    "market_value": value["market_value"],
                    "equity": value["equity"],
                    "pnl_from_initial_cash": value["pnl_from_initial_cash"],
                    "position_count": value["position_count"],
                    "content_sha256": identity,
                }
            )
            seen_sessions.add(session)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            invalid.append(
                {
                    "path": str(alias),
                    "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                }
            )
    points.sort(key=lambda value: str(value["session"]))
    source_provenance_verified = (
        bool(points) and not invalid and not source_unverified
    )
    continuity = _audit_forward_valuation_continuity(
        forward_events=forward_events,
        points=points,
    )
    invalid.extend(continuity["invalid_evidence"])
    status = "COMPLETE" if aliases else "NOT_STARTED"
    if invalid:
        status = "INVALID"
    elif source_unverified:
        status = "SOURCE_UNVERIFIED"
    elif continuity["status"] in {
        "CONTINUITY_UNVERIFIED",
        "INCOMPLETE_CURVE",
    }:
        status = str(continuity["status"])
    elif aliases and continuity["status"] == "COMPLETE":
        status = "COMPLETE"
    curve_available = bool(points) and status == "COMPLETE"
    return {
        "status": status,
        "valuation_count": len(aliases),
        "complete_valuation_count": len(points),
        "equity_curve_available": curve_available,
        "performance_evaluable": False,
        "source_provenance_available": source_provenance_available,
        # 点位来源与曲线连续性是两个独立结论。前向锚点缺失或不匹配会关闭整条曲线，
        # 但不会追溯性地否定其他已经通过来源校验的收盘标记。
        "source_provenance_verified": source_provenance_verified,
        "curve_continuity_status": continuity["status"],
        "forward_anchor_available": continuity["forward_anchor_available"],
        "required_valuation_sessions": continuity[
            "required_valuation_sessions"
        ],
        "missing_valuation_sessions": continuity[
            "missing_valuation_sessions"
        ],
        "unanchored_valuation_sessions": continuity[
            "unanchored_valuation_sessions"
        ],
        "verified_forward_anchor_count": continuity[
            "verified_forward_anchor_count"
        ],
        "points": points,
        # 单独验证过的点仍可用于诊断，但后续别名断裂会使整条曲线失效；
        # 此时绝不能通过便捷的 ``latest`` 字段暴露旧点。
        "latest": points[-1] if curve_available else None,
        "invalid_evidence": invalid,
        "source_unverified_evidence": source_unverified,
        "tick_data_used": False,
        "broker_transport_available": False,
        "live_status": "LIVE_DISABLED",
    }


__all__ = (
    "VALUATION_SCHEMA",
    "audit_human_paper_valuation_evidence",
    "build_human_paper_valuation_document",
    "validate_human_paper_valuation_document",
    "validate_human_paper_valuation_sources",
)
