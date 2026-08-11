from __future__ import annotations

import copy
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.higher_timeframe_execution_attribution import (
    higher_timeframe_execution_attribution,
)


CN = ZoneInfo("Asia/Shanghai")
START = datetime(2026, 4, 1, 10, 0, tzinfo=CN)


def _candidate(
    index: int,
    *,
    accepted: bool,
    gates: tuple[str, str, str],
) -> dict[str, object]:
    character = f"{index:x}"
    return {
        "symbol": f"SZ.{index:06d}",
        "decision_at": (START + timedelta(days=index)).isoformat(),
        "structure_snapshot_id": "sha256:" + character * 64,
        "l0_point_id": "sha256:" + character * 64,
        "accepted": accepted,
        "exact_green": all(value == "GREEN" for value in gates),
        "market_risk_gate": gates[0],
        "sector_risk_gate": gates[1],
        "symbol_risk_gate": gates[2],
        "market_risk_blocker_codes": (
            [] if gates[0] == "GREEN" else ["MARKET_MAPPING_UNRESOLVED"]
        ),
        "sector_risk_blocker_codes": (
            [] if gates[1] == "GREEN" else ["SECTOR_MAPPING_UNRESOLVED"]
        ),
        "symbol_risk_blocker_codes": (
            [] if gates[2] == "GREEN" else ["SYMBOL_MAPPING_UNRESOLVED"]
        ),
    }


def _execution_id(index: int) -> str:
    return f"replay-order:{index:064x}:bar:{index + 1}"


def _order(
    candidate: dict[str, object],
    *,
    fill_quantity: int,
    index: int,
) -> dict[str, object]:
    fills = []
    if fill_quantity:
        fills.append(
            {
                "execution_id": _execution_id(index),
                "exchange_time": (
                    datetime.fromisoformat(str(candidate["decision_at"]))
                    + timedelta(minutes=2)
                ).isoformat(),
                "quantity": fill_quantity,
            }
        )
    return {
        "event_id": f"event:entry:{index}",
        "intent_action": "ENTRY_INTENT",
        "order": {
            "symbol": candidate["symbol"],
            "created_at": candidate["decision_at"],
            "structure_snapshot_id": candidate["structure_snapshot_id"],
        },
        "match": {
            "order_id": f"order:{index}",
            "state": "O_FILLED" if fills else "O_IDLE",
            "filled_quantity": fill_quantity,
            "fills": fills,
            "rejection_and_unfilled_reasons": (
                []
                if fills
                else [
                    "NO_WHOLE_BAR_STRICT_CROSS",
                    "ORDER_EXPIRED_WITH_UNFILLED_QUANTITY",
                ]
            ),
        },
    }


def _cycle_id(candidate: dict[str, object], index: int) -> str:
    return f"cycle:{candidate['symbol']}:{_execution_id(index)}"


def _fixture() -> tuple[
    list[dict[str, object]],
    dict[str, object],
    dict[str, object],
]:
    amber_closed = _candidate(
        0,
        accepted=True,
        gates=("AMBER", "AMBER", "AMBER"),
    )
    strict_unfilled = _candidate(
        1,
        accepted=True,
        gates=("GREEN", "GREEN", "GREEN"),
    )
    amber_open = _candidate(
        2,
        accepted=True,
        gates=("AMBER", "GREEN", "AMBER"),
    )
    rejected = _candidate(
        3,
        accepted=False,
        gates=("AMBER", "AMBER", "UNRESOLVED"),
    )
    candidates = [amber_closed, strict_unfilled, amber_open, rejected]
    replay = {
        "orders": [
            _order(amber_closed, fill_quantity=100, index=0),
            _order(strict_unfilled, fill_quantity=0, index=1),
            _order(amber_open, fill_quantity=200, index=2),
            {
                "event_id": "event:exit:0",
                "intent_action": "STRATEGIC_EXIT_INTENT",
                "order": {},
                "match": {},
            },
        ]
    }
    terminal = {
        "status": "EVALUATED",
        "reason_codes": [],
        "terminal": {"total_net_pnl": "50"},
        "pnl_decomposition": {
            "closed_cycle_realized_net_pnl": "-10",
            "open_cycle_marked_net_pnl": "60",
            "identity_difference": "0",
        },
        "closed_cycles": [
            {
                "cycle_id": _cycle_id(amber_closed, 0),
                "symbol": amber_closed["symbol"],
                "opened_at": (START + timedelta(minutes=2)).isoformat(),
                "closed_at": (START + timedelta(days=10)).isoformat(),
                "realized_net_pnl": "-10",
            }
        ],
        "open_positions": [
            {
                "cycle_id": _cycle_id(amber_open, 2),
                "symbol": amber_open["symbol"],
                "opened_at": (
                    START + timedelta(days=2, minutes=2)
                ).isoformat(),
                "marked_at": (START + timedelta(days=30)).isoformat(),
                "marked_net_pnl": "60",
            }
        ],
    }
    return candidates, replay, terminal


def test_execution_attribution_closes_candidate_order_fill_and_pnl_chain() -> None:
    candidates, replay, terminal = _fixture()

    value = higher_timeframe_execution_attribution(
        candidates,
        replay,
        terminal,
    )

    assert value["status"] == (
        "STRICT_GREEN_EXECUTION_EMPTY_RESEARCH_AMBER_ONLY"
    )
    assert value["causal_identity_status"] == "EXACT"
    assert value["risk_evidenced_candidate_count"] == 4
    assert value["accepted_candidate_count"] == 3
    assert value["hard_rejected_candidate_count"] == 1
    assert value["entry_order_count"] == 3
    assert value["entry_filled_candidate_count"] == 2
    assert value["entry_unfilled_candidate_count"] == 1
    assert value["terminal_closed_cycle_count"] == 1
    assert value["terminal_open_cycle_count"] == 1
    assert value["all_filled_entries_are_research_amber_only"] is True
    assert value["terminal_total_net_pnl"] == "50"

    strict = value["cohorts"]["STRICT_GREEN"]
    assert strict["accepted_candidate_count"] == 1
    assert strict["entry_filled_candidate_count"] == 0
    assert strict["entry_unfilled_candidate_count"] == 1
    assert strict["total_attributed_net_pnl"] == "0"

    amber = value["cohorts"]["RESEARCH_AMBER_ONLY"]
    assert amber["accepted_candidate_count"] == 2
    assert amber["entry_filled_candidate_count"] == 2
    assert amber["closed_cycle_count"] == 1
    assert amber["open_cycle_count"] == 1
    assert amber["closed_realized_net_pnl"] == "-10"
    assert amber["open_marked_net_pnl"] == "60"
    assert amber["total_attributed_net_pnl"] == "50"
    assert amber["positive_total_depends_on_open_cycle_marks"] is True
    assert value["unfilled_reason_counts"] == {
        "NO_WHOLE_BAR_STRICT_CROSS": 1,
        "ORDER_EXPIRED_WITH_UNFILLED_QUANTITY": 1,
    }
    assert [row["cycle_status"] for row in value["entries"]] == [
        "CLOSED",
        "NO_FILL",
        "OPEN_MARKED",
    ]
    assert str(value["audit_sha256"]).startswith("sha256:")
    assert value["diagnostic_only"] is True
    assert value["decisions_unchanged"] is True
    assert value["live_status"] == "LIVE_DISABLED"

    # Persisted JSON must reproduce the exact same primitive document.
    persisted_inputs = json.loads(json.dumps([candidates, replay, terminal]))
    persisted_value = higher_timeframe_execution_attribution(
        persisted_inputs[0],
        persisted_inputs[1],
        persisted_inputs[2],
    )
    assert persisted_value == value


def test_execution_attribution_rejects_missing_or_duplicate_entry_identity() -> None:
    candidates, replay, terminal = _fixture()
    replay["orders"] = replay["orders"][:-2] + replay["orders"][-1:]
    with pytest.raises(ValueError, match="identities do not match"):
        higher_timeframe_execution_attribution(candidates, replay, terminal)

    candidates, replay, terminal = _fixture()
    candidates.append(copy.deepcopy(candidates[0]))
    with pytest.raises(ValueError, match="candidate identity is duplicated"):
        higher_timeframe_execution_attribution(candidates, replay, terminal)

    candidates, replay, terminal = _fixture()
    replay["orders"].insert(1, copy.deepcopy(replay["orders"][0]))
    with pytest.raises(ValueError, match="order candidate identity is duplicated"):
        higher_timeframe_execution_attribution(candidates, replay, terminal)


def test_execution_attribution_rejects_orphan_terminal_cycle() -> None:
    candidates, replay, terminal = _fixture()
    terminal["open_positions"].append(
        {
            "cycle_id": "cycle:SZ.999999:orphan",
            "symbol": "SZ.999999",
            "opened_at": (START + timedelta(days=20)).isoformat(),
            "marked_at": (START + timedelta(days=30)).isoformat(),
            "marked_net_pnl": "1",
        }
    )
    terminal["pnl_decomposition"]["open_cycle_marked_net_pnl"] = "61"
    terminal["terminal"]["total_net_pnl"] = "51"

    with pytest.raises(ValueError, match="terminal cycles and filled entries"):
        higher_timeframe_execution_attribution(candidates, replay, terminal)


def test_execution_attribution_rejects_tampered_terminal_pnl_identity() -> None:
    candidates, replay, terminal = _fixture()
    terminal["pnl_decomposition"]["closed_cycle_realized_net_pnl"] = "-9"

    with pytest.raises(ValueError, match="P&L identity is inconsistent"):
        higher_timeframe_execution_attribution(candidates, replay, terminal)


def test_execution_attribution_rejects_fill_quantity_mismatch() -> None:
    candidates, replay, terminal = _fixture()
    replay["orders"][0]["match"]["filled_quantity"] = 99

    with pytest.raises(ValueError, match="does not equal fill ledger"):
        higher_timeframe_execution_attribution(candidates, replay, terminal)
