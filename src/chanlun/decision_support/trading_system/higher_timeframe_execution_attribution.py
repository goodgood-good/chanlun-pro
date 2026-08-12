"""Causal attribution from higher-timeframe gates to entry-cycle P&L.

The higher-timeframe effectiveness audit proves what the market, sector and
symbol gates knew at each candidate timestamp.  It does not, by itself, prove
which accepted candidate produced an order, a fill, or a terminal strategic
cycle.  This module closes that read-only evidence chain without changing a
candidate, order, fill, position or parameter.

The output deliberately uses JSON-stable primitive values.  The historical
builder and the web audit service therefore recompute the exact same document
from native dataclasses and from the persisted JSON artifact respectively.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Mapping, Sequence

from chanlun.decision_support.fingerprints import sha256_json


HIGHER_TIMEFRAME_EXECUTION_ATTRIBUTION_SCHEMA = (
    "chanlun-higher-timeframe-execution-attribution"
)
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_ZERO = Decimal("0")
_COHORTS = ("STRICT_GREEN", "RESEARCH_AMBER_ONLY")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a mapping")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{label} must be a sequence")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _decimal(value: object, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite decimal")
    try:
        result = Decimal(value)  # type: ignore[arg-type]
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{label} must be a finite decimal")
    return result


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _sha256(value: object, label: str) -> str:
    result = _text(value, label)
    if not _SHA256_ID.fullmatch(result):
        raise ValueError(f"{label} must be a sha256 identity")
    return result


def _timestamp(value: object, label: str) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{label} must be an ISO datetime") from exc
    else:
        raise ValueError(f"{label} must be an ISO datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return parsed.isoformat()


def _reason_codes(value: object, label: str) -> list[str]:
    output = [_text(item, label) for item in _sequence(value, label)]
    if len(output) != len(set(output)):
        raise ValueError(f"{label} must not contain duplicates")
    return output


def _candidate_key(row: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        _text(row.get("symbol"), "candidate symbol"),
        _timestamp(row.get("decision_at"), "candidate decision_at"),
        _sha256(
            row.get("structure_snapshot_id"),
            "candidate structure_snapshot_id",
        ),
    )


def _order_key(order: Mapping[str, object]) -> tuple[str, str, str]:
    return (
        _text(order.get("symbol"), "entry order symbol"),
        _timestamp(order.get("created_at"), "entry order created_at"),
        _sha256(
            order.get("structure_snapshot_id"),
            "entry order structure_snapshot_id",
        ),
    )


def _cohort(row: Mapping[str, object]) -> str:
    gates = tuple(
        _text(row.get(f"{subject}_risk_gate"), f"{subject} risk gate")
        for subject in ("market", "sector", "symbol")
    )
    exact_green = row.get("exact_green")
    if not isinstance(exact_green, bool):
        raise ValueError("candidate exact_green must be boolean")
    computed_exact_green = all(value == "GREEN" for value in gates)
    if exact_green is not computed_exact_green:
        raise ValueError("candidate exact_green contradicts risk gates")
    if computed_exact_green:
        return "STRICT_GREEN"
    if all(value in {"GREEN", "AMBER"} for value in gates) and any(
        value == "AMBER" for value in gates
    ):
        return "RESEARCH_AMBER_ONLY"
    raise ValueError("accepted candidate has a blocking risk gate")


def _terminal_cycles(
    terminal_accounting: Mapping[str, object],
) -> tuple[dict[str, dict[str, object]], Decimal, Decimal, Decimal]:
    if terminal_accounting.get("status") != "EVALUATED":
        raise ValueError("terminal accounting must be evaluated")
    if _reason_codes(
        terminal_accounting.get("reason_codes"),
        "terminal accounting reason_codes",
    ):
        raise ValueError("evaluated terminal accounting has reason codes")

    cycles: dict[str, dict[str, object]] = {}
    closed_total = _ZERO
    for raw in _sequence(
        terminal_accounting.get("closed_cycles"), "closed cycles"
    ):
        row = _mapping(raw, "closed cycle")
        cycle_id = _text(row.get("cycle_id"), "closed cycle_id")
        if cycle_id in cycles:
            raise ValueError("terminal cycle identity is duplicated")
        pnl = _decimal(row.get("realized_net_pnl"), "closed realized P&L")
        opened_at = _timestamp(row.get("opened_at"), "closed opened_at")
        closed_at = _timestamp(row.get("closed_at"), "closed closed_at")
        if closed_at < opened_at:
            raise ValueError("closed cycle ends before it opens")
        closed_total += pnl
        cycles[cycle_id] = {
            "cycle_id": cycle_id,
            "symbol": _text(row.get("symbol"), "closed cycle symbol"),
            "cycle_status": "CLOSED",
            "opened_at": opened_at,
            "terminal_observed_at": closed_at,
            "terminal_net_pnl": str(pnl),
        }

    open_total = _ZERO
    for raw in _sequence(
        terminal_accounting.get("open_positions"), "open positions"
    ):
        row = _mapping(raw, "open position")
        cycle_id = _text(row.get("cycle_id"), "open cycle_id")
        if cycle_id in cycles:
            raise ValueError("terminal cycle identity is duplicated")
        pnl = _decimal(row.get("marked_net_pnl"), "open marked P&L")
        opened_at = _timestamp(row.get("opened_at"), "open opened_at")
        marked_at = _timestamp(row.get("marked_at"), "open marked_at")
        if marked_at < opened_at:
            raise ValueError("open cycle is marked before it opens")
        open_total += pnl
        cycles[cycle_id] = {
            "cycle_id": cycle_id,
            "symbol": _text(row.get("symbol"), "open cycle symbol"),
            "cycle_status": "OPEN_MARKED",
            "opened_at": opened_at,
            "terminal_observed_at": marked_at,
            "terminal_net_pnl": str(pnl),
        }

    decomposition = _mapping(
        terminal_accounting.get("pnl_decomposition"), "P&L decomposition"
    )
    terminal = _mapping(terminal_accounting.get("terminal"), "terminal")
    reported_closed = _decimal(
        decomposition.get("closed_cycle_realized_net_pnl"),
        "reported closed P&L",
    )
    reported_open = _decimal(
        decomposition.get("open_cycle_marked_net_pnl"),
        "reported open P&L",
    )
    identity_difference = _decimal(
        decomposition.get("identity_difference"), "P&L identity difference"
    )
    total = _decimal(terminal.get("total_net_pnl"), "terminal total P&L")
    if (
        identity_difference != 0
        or closed_total != reported_closed
        or open_total != reported_open
        or total != closed_total + open_total
    ):
        raise ValueError("terminal accounting P&L identity is inconsistent")
    return cycles, closed_total, open_total, total


def higher_timeframe_execution_attribution(
    candidate_audit: Sequence[Mapping[str, object]],
    replay: Mapping[str, object],
    terminal_accounting: Mapping[str, object],
) -> dict[str, object]:
    """构建候选、订单、成交与持仓周期之间的精确归因链。

    只有包含布尔字段 ``accepted`` 的记录才已进入月、周、日风险裁决。
    在此之前被拒绝的候选不属于本审计范围，仍由既有候选漏斗记录。
    """

    risk_rows: list[Mapping[str, object]] = []
    accepted_by_key: dict[tuple[str, str, str], Mapping[str, object]] = {}
    cohort_by_key: dict[tuple[str, str, str], str] = {}
    for raw in candidate_audit:
        row = _mapping(raw, "candidate audit row")
        accepted = row.get("accepted")
        if not isinstance(accepted, bool):
            continue
        risk_rows.append(row)
        key = _candidate_key(row)
        if key in accepted_by_key or any(
            _candidate_key(previous) == key for previous in risk_rows[:-1]
        ):
            raise ValueError("risk candidate identity is duplicated")
        if accepted:
            accepted_by_key[key] = row
            cohort_by_key[key] = _cohort(row)

    if not risk_rows:
        raise ValueError("risk execution attribution has no candidate evidence")

    terminal_cycles, closed_total, open_total, total_pnl = _terminal_cycles(
        terminal_accounting
    )
    entry_orders: dict[tuple[str, str, str], Mapping[str, object]] = {}
    order_envelopes: dict[tuple[str, str, str], Mapping[str, object]] = {}
    for raw in _sequence(replay.get("orders"), "replay orders"):
        envelope = _mapping(raw, "replay order")
        if envelope.get("intent_action") != "ENTRY_INTENT":
            continue
        order = _mapping(envelope.get("order"), "entry order")
        key = _order_key(order)
        if key in entry_orders:
            raise ValueError("entry order candidate identity is duplicated")
        entry_orders[key] = order
        order_envelopes[key] = envelope

    accepted_keys = set(accepted_by_key)
    order_keys = set(entry_orders)
    if accepted_keys != order_keys:
        missing = len(accepted_keys - order_keys)
        orphan = len(order_keys - accepted_keys)
        raise ValueError(
            "accepted candidate and entry order identities do not match "
            f"(missing={missing}, orphan={orphan})"
        )

    cohort_totals: dict[str, dict[str, object]] = {
        cohort: {
            "accepted_candidate_count": 0,
            "entry_order_count": 0,
            "entry_filled_candidate_count": 0,
            "entry_unfilled_candidate_count": 0,
            "entry_fill_count": 0,
            "entry_filled_quantity": 0,
            "closed_cycle_count": 0,
            "open_cycle_count": 0,
            "closed_realized_net_pnl": _ZERO,
            "open_marked_net_pnl": _ZERO,
            "total_attributed_net_pnl": _ZERO,
        }
        for cohort in _COHORTS
    }
    entries: list[dict[str, object]] = []
    attributed_cycle_ids: set[str] = set()
    unfilled_reasons: Counter[str] = Counter()

    for key in sorted(accepted_keys, key=lambda value: (value[1], value[0])):
        candidate = accepted_by_key[key]
        cohort = cohort_by_key[key]
        envelope = order_envelopes[key]
        order = entry_orders[key]
        match = _mapping(envelope.get("match"), "entry order match")
        fills = [
            _mapping(value, "entry fill")
            for value in _sequence(match.get("fills"), "entry fills")
        ]
        filled_quantity = _integer(
            match.get("filled_quantity"), "entry filled quantity"
        )
        summed_fill_quantity = sum(
            _integer(fill.get("quantity"), "entry fill quantity")
            for fill in fills
        )
        if filled_quantity != summed_fill_quantity:
            raise ValueError("entry filled quantity does not equal fill ledger")
        reasons = _reason_codes(
            match.get("rejection_and_unfilled_reasons"),
            "entry order unfilled reasons",
        )
        if filled_quantity == 0 and fills:
            raise ValueError("zero-quantity entry order contains fills")
        if filled_quantity > 0 and not fills:
            raise ValueError("filled entry order has no fill evidence")

        totals = cohort_totals[cohort]
        totals["accepted_candidate_count"] = int(
            totals["accepted_candidate_count"]
        ) + 1
        totals["entry_order_count"] = int(totals["entry_order_count"]) + 1
        totals["entry_fill_count"] = int(totals["entry_fill_count"]) + len(
            fills
        )
        totals["entry_filled_quantity"] = int(
            totals["entry_filled_quantity"]
        ) + filled_quantity

        cycle_id: str | None = None
        cycle_status = "NO_FILL"
        terminal_net_pnl: str | None = None
        entry_execution_id: str | None = None
        if fills:
            first_fill = min(
                fills,
                key=lambda value: _timestamp(
                    value.get("exchange_time"), "entry fill exchange_time"
                ),
            )
            entry_execution_id = _text(
                first_fill.get("execution_id"), "entry execution_id"
            )
            cycle_id = f"cycle:{key[0]}:{entry_execution_id}"
            if cycle_id in attributed_cycle_ids:
                raise ValueError("entry cycle identity is duplicated")
            attributed_cycle_ids.add(cycle_id)
            terminal_cycle = terminal_cycles.get(cycle_id)
            if terminal_cycle is None:
                raise ValueError("filled entry has no terminal cycle")
            if terminal_cycle["symbol"] != key[0]:
                raise ValueError("terminal cycle symbol does not match entry")
            first_fill_at = _timestamp(
                first_fill.get("exchange_time"), "entry fill exchange_time"
            )
            if terminal_cycle["opened_at"] != first_fill_at:
                raise ValueError(
                    "terminal cycle opening time does not match first entry fill"
                )
            cycle_status = str(terminal_cycle["cycle_status"])
            terminal_net_pnl = str(terminal_cycle["terminal_net_pnl"])
            pnl = _decimal(terminal_net_pnl, "terminal cycle P&L")
            totals["entry_filled_candidate_count"] = int(
                totals["entry_filled_candidate_count"]
            ) + 1
            if cycle_status == "CLOSED":
                totals["closed_cycle_count"] = int(
                    totals["closed_cycle_count"]
                ) + 1
                totals["closed_realized_net_pnl"] = _decimal(
                    totals["closed_realized_net_pnl"], "cohort closed P&L"
                ) + pnl
            elif cycle_status == "OPEN_MARKED":
                totals["open_cycle_count"] = int(
                    totals["open_cycle_count"]
                ) + 1
                totals["open_marked_net_pnl"] = _decimal(
                    totals["open_marked_net_pnl"], "cohort open P&L"
                ) + pnl
            else:  # pragma: no cover - constructed terminal cycles are closed/open
                raise ValueError("terminal cycle status is unsupported")
            totals["total_attributed_net_pnl"] = _decimal(
                totals["total_attributed_net_pnl"], "cohort total P&L"
            ) + pnl
        else:
            totals["entry_unfilled_candidate_count"] = int(
                totals["entry_unfilled_candidate_count"]
            ) + 1
            unfilled_reasons.update(reasons)

        entries.append(
            {
                "symbol": key[0],
                "decision_at": key[1],
                "l0_point_id": _sha256(
                    candidate.get("l0_point_id"), "candidate l0_point_id"
                ),
                "structure_snapshot_id": key[2],
                "risk_cohort": cohort,
                "risk_gates": {
                    subject: _text(
                        candidate.get(f"{subject}_risk_gate"),
                        f"{subject} risk gate",
                    )
                    for subject in ("market", "sector", "symbol")
                },
                "risk_blocker_codes": {
                    subject: _reason_codes(
                        candidate.get(f"{subject}_risk_blocker_codes"),
                        f"{subject} risk blocker codes",
                    )
                    for subject in ("market", "sector", "symbol")
                },
                "order_event_id": _text(
                    envelope.get("event_id"), "entry order event_id"
                ),
                "order_id": _text(match.get("order_id"), "entry order_id"),
                "order_state": _text(
                    match.get("state"), "entry order state"
                ),
                "filled_quantity": filled_quantity,
                "fill_count": len(fills),
                "unfilled_reason_codes": reasons,
                "entry_execution_id": entry_execution_id,
                "cycle_id": cycle_id,
                "cycle_status": cycle_status,
                "terminal_net_pnl": terminal_net_pnl,
            }
        )

    if attributed_cycle_ids != set(terminal_cycles):
        missing = len(set(terminal_cycles) - attributed_cycle_ids)
        orphan = len(attributed_cycle_ids - set(terminal_cycles))
        raise ValueError(
            "terminal cycles and filled entries do not match "
            f"(unattributed={missing}, orphan={orphan})"
        )

    attributed_closed = sum(
        _decimal(value["closed_realized_net_pnl"], "cohort closed P&L")
        for value in cohort_totals.values()
    )
    attributed_open = sum(
        _decimal(value["open_marked_net_pnl"], "cohort open P&L")
        for value in cohort_totals.values()
    )
    if (
        attributed_closed != closed_total
        or attributed_open != open_total
        or attributed_closed + attributed_open != total_pnl
    ):
        raise ValueError("execution cohort P&L does not close to terminal P&L")

    cohorts: dict[str, dict[str, object]] = {}
    for cohort in _COHORTS:
        raw = cohort_totals[cohort]
        closed_pnl = _decimal(
            raw["closed_realized_net_pnl"], "cohort closed P&L"
        )
        open_pnl = _decimal(raw["open_marked_net_pnl"], "cohort open P&L")
        total = _decimal(raw["total_attributed_net_pnl"], "cohort total P&L")
        cohorts[cohort] = {
            **{
                key: int(raw[key])
                for key in (
                    "accepted_candidate_count",
                    "entry_order_count",
                    "entry_filled_candidate_count",
                    "entry_unfilled_candidate_count",
                    "entry_fill_count",
                    "entry_filled_quantity",
                    "closed_cycle_count",
                    "open_cycle_count",
                )
            },
            "closed_realized_net_pnl": str(closed_pnl),
            "open_marked_net_pnl": str(open_pnl),
            "total_attributed_net_pnl": str(total),
            "positive_total_depends_on_open_cycle_marks": (
                total > 0 and closed_pnl <= 0 and open_pnl > 0
            ),
        }

    strict_filled = int(
        cohorts["STRICT_GREEN"]["entry_filled_candidate_count"]
    )
    amber_filled = int(
        cohorts["RESEARCH_AMBER_ONLY"]["entry_filled_candidate_count"]
    )
    if strict_filled == 0 and amber_filled > 0:
        status = "STRICT_GREEN_EXECUTION_EMPTY_RESEARCH_AMBER_ONLY"
    elif strict_filled > 0 and amber_filled > 0:
        status = "MIXED_STRICT_AND_RESEARCH_EXECUTION"
    elif strict_filled > 0:
        status = "STRICT_GREEN_EXECUTION_PRESENT"
    else:
        status = "NO_ENTRY_FILL"

    stable: dict[str, object] = {
        "schema": HIGHER_TIMEFRAME_EXECUTION_ATTRIBUTION_SCHEMA,
        "status": status,
        "causal_identity_status": "EXACT",
        "risk_evidenced_candidate_count": len(risk_rows),
        "accepted_candidate_count": len(accepted_by_key),
        "hard_rejected_candidate_count": len(risk_rows) - len(accepted_by_key),
        "entry_order_count": len(entry_orders),
        "entry_filled_candidate_count": strict_filled + amber_filled,
        "entry_unfilled_candidate_count": (
            len(entry_orders) - strict_filled - amber_filled
        ),
        "terminal_closed_cycle_count": sum(
            int(value["closed_cycle_count"]) for value in cohorts.values()
        ),
        "terminal_open_cycle_count": sum(
            int(value["open_cycle_count"]) for value in cohorts.values()
        ),
        "all_filled_entries_are_research_amber_only": (
            strict_filled == 0 and amber_filled > 0
        ),
        "terminal_total_net_pnl": str(total_pnl),
        "cohorts": cohorts,
        "unfilled_reason_counts": {
            key: unfilled_reasons[key] for key in sorted(unfilled_reasons)
        },
        "entries": entries,
        "disclosures": [
            "candidate and entry order identities match on symbol, decision time and structure snapshot",
            "a strategic cycle identity is derived from the earliest causal entry fill",
            "closed and open cycle P&L is consumed from the validated terminal accounting attribution",
            "this audit is diagnostic only and cannot change a gate, order, fill or parameter",
        ],
        "diagnostic_only": True,
        "decisions_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }
    return {**stable, "audit_sha256": sha256_json(stable)}


__all__ = [
    "HIGHER_TIMEFRAME_EXECUTION_ATTRIBUTION_SCHEMA",
    "higher_timeframe_execution_attribution",
]
