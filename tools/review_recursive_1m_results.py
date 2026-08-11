#!/usr/bin/env python3
"""Independent, read-only review of recursive-1m research artifacts.

This validator deliberately does not call the prescreener or replay engine.
It recomputes artifact identities, the cash ledger, fee totals, cycle P&L,
temporal execution constraints, parameter isolation, and research/live gates
from the serialized evidence alone.  It therefore provides a second path for
detecting report-layer mistakes without manufacturing another signal engine.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PRESCREEN = Path(
    "audit/chanlun_live_integration/recursive_1m_etf_prescreen.json"
)
DEFAULT_BACKTEST = Path(
    "audit/chanlun_live_integration/recursive_1m_component_backtest.json"
)
DEFAULT_DATA_GATE = Path(
    "audit/chanlun_live_integration/recursive_1m_data_acceptance.json"
)
DEFAULT_OUTPUT = Path(
    "audit/chanlun_live_integration/recursive_1m_independent_review.json"
)


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _content_sha256(document: Mapping[str, object]) -> str:
    unsigned = dict(document)
    unsigned.pop("content_sha256", None)
    return _canonical_sha256(unsigned)


def _universe_content_sha256(document: Mapping[str, object]) -> str:
    """Universe identity intentionally excludes its generation timestamp."""

    stable = {
        key: value
        for key, value in document.items()
        if key not in {"generated_at", "content_sha256"}
    }
    return _canonical_sha256(stable)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _as_mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return value


def _as_list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    return value


def _dt(value: object) -> datetime:
    return datetime.fromisoformat(str(value))


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _key_values(value: object, key: str) -> list[object]:
    values: list[object] = []
    if isinstance(value, Mapping):
        for name, item in value.items():
            if name == key:
                values.append(item)
            values.extend(_key_values(item, key))
    elif isinstance(value, list):
        for item in value:
            values.extend(_key_values(item, key))
    return values


class Checks:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def add(self, name: str, passed: bool, evidence: object) -> None:
        self.rows.append(
            {
                "check": name,
                "status": "PASS" if passed else "FAIL",
                "evidence": evidence,
            }
        )

    @property
    def passed(self) -> bool:
        return all(row["status"] == "PASS" for row in self.rows)

    @property
    def failed_names(self) -> list[str]:
        return [
            str(row["check"])
            for row in self.rows
            if row["status"] == "FAIL"
        ]


def _verify_artifact_identity(
    checks: Checks,
    name: str,
    document: Mapping[str, object],
) -> None:
    calculated = _content_sha256(document)
    expected = document.get("content_sha256")
    checks.add(
        f"{name}_content_sha256",
        expected == calculated,
        {"expected": expected, "calculated": calculated},
    )


def _review_source_files(
    checks: Checks,
    prescreen: Mapping[str, object],
) -> None:
    sources = _as_mapping(prescreen["source_database"], "source_database")
    for label in ("market", "pit", "corporate_actions"):
        path = Path(str(sources[f"{label}_path"]))
        calculated = _file_sha256(path) if path.is_file() else None
        expected = sources[f"{label}_sha256"]
        checks.add(
            f"{label}_source_file_unchanged",
            calculated == expected,
            {
                "path": str(path),
                "expected": expected,
                "calculated": calculated,
            },
        )

    universe_path = Path(str(prescreen["universe_artifact"]))
    universe = _load(universe_path) if universe_path.is_file() else {}
    checks.add(
        "universe_artifact_file_unchanged",
        universe_path.is_file()
        and _file_sha256(universe_path) == prescreen["universe_artifact_sha256"],
        str(universe_path),
    )
    checks.add(
        "universe_artifact_content_identity",
        bool(universe)
        and universe.get("content_sha256")
        == prescreen["universe_content_sha256"]
        == _universe_content_sha256(universe),
        prescreen["universe_content_sha256"],
    )


def review(
    *,
    prescreen_path: Path,
    backtest_path: Path,
    data_gate_path: Path,
) -> dict[str, object]:
    prescreen = _load(prescreen_path)
    backtest = _load(backtest_path)
    data_gate = _load(data_gate_path)
    checks = Checks()

    for name, document, schema in (
        (
            "prescreen",
            prescreen,
            "chanlun-recursive-1m-etf-prescreen",
        ),
        (
            "backtest",
            backtest,
            "chanlun-recursive-1m-component-backtest",
        ),
        (
            "data_gate",
            data_gate,
            "chanlun-recursive-1m-data-acceptance",
        ),
    ):
        checks.add(
            f"{name}_schema",
            document.get("schema") == schema,
            document.get("schema"),
        )
        _verify_artifact_identity(checks, name, document)

    checks.add(
        "backtest_references_exact_prescreen",
        backtest.get("prescreen_content_sha256")
        == prescreen.get("content_sha256")
        and backtest.get("prescreen_sha256") == _file_sha256(prescreen_path),
        {
            "content": backtest.get("prescreen_content_sha256"),
            "file": backtest.get("prescreen_sha256"),
        },
    )
    checks.add(
        "backtest_references_exact_data_gate",
        backtest.get("data_acceptance_content_sha256")
        == data_gate.get("content_sha256"),
        backtest.get("data_acceptance_content_sha256"),
    )
    _review_source_files(checks, prescreen)

    manifest = _as_mapping(backtest["parameter_manifest"], "parameter_manifest")
    manifest_unsigned = dict(manifest)
    manifest_id = manifest_unsigned.pop("manifest_sha256", None)
    checks.add(
        "parameter_manifest_frozen_and_identical",
        manifest == prescreen.get("parameter_manifest")
        and manifest_id == _canonical_sha256(manifest_unsigned),
        manifest_id,
    )
    snapshots = _as_mapping(manifest["snapshots"], "parameter snapshots")
    individual = _as_mapping(
        snapshots["INDIVIDUAL_THREE_PROGRAM"], "individual snapshot"
    )
    etf = _as_mapping(snapshots["ETF_PROXY"], "ETF snapshot")
    checks.add(
        "selection_path_parameter_snapshots_are_distinct",
        individual["parameter_set_id"] != etf["parameter_set_id"],
        {
            "INDIVIDUAL_THREE_PROGRAM": individual["parameter_set_id"],
            "ETF_PROXY": etf["parameter_set_id"],
        },
    )
    checks.add(
        "individual_path_fails_closed",
        _as_mapping(prescreen["individual_path"], "individual_path").get(
            "status"
        )
        == "NOT_RUN",
        prescreen["individual_path"],
    )

    live_values = (
        _key_values(prescreen, "live_status")
        + _key_values(backtest, "live_status")
        + _key_values(data_gate, "live_status")
    )
    checks.add(
        "all_live_statuses_disabled",
        bool(live_values) and set(live_values) == {"LIVE_DISABLED"},
        {"count": len(live_values), "values": sorted(set(live_values))},
    )
    performance_values = _key_values(backtest, "performance_evaluable")
    checks.add(
        "no_performance_evaluable_flag_true",
        bool(performance_values) and not any(performance_values),
        {"count": len(performance_values), "values": sorted(set(performance_values))},
    )
    checks.add(
        "component_only_claim_gate",
        backtest.get("data_grade") == "COMPONENT_ONLY"
        and backtest.get("highest_status") == "RESEARCH_ONLY"
        and backtest.get("complete_system_return_claim_allowed") is False
        and data_gate.get("full_system_return_evaluation_allowed") is False
        and data_gate.get("formal_execution_return_evaluation_allowed") is False,
        {
            "grade": backtest.get("data_grade"),
            "status": backtest.get("highest_status"),
        },
    )

    totals = _as_mapping(prescreen["totals"], "prescreen totals")
    instruments = _as_list(prescreen["instrument_reports"], "instrument reports")
    accepted = [
        candidate
        for report in instruments
        for candidate in _as_list(
            _as_mapping(report, "instrument report")["candidate_decisions"],
            "candidate decisions",
        )
        if _as_mapping(candidate, "candidate").get("component_eligible") is True
    ]
    full = [
        candidate
        for report in instruments
        for candidate in _as_list(
            _as_mapping(report, "instrument report")["candidate_decisions"],
            "candidate decisions",
        )
        if _as_mapping(candidate, "candidate").get("full_system_eligible") is True
    ]
    checks.add(
        "candidate_totals_reconcile",
        len(accepted) == totals["component_eligible"] == 4
        and len(full) == totals["full_system_eligible"] == 0,
        {"component": len(accepted), "full_system": len(full)},
    )

    parity = _as_mapping(
        backtest["prefix_and_decision_parity"], "prefix_and_decision_parity"
    )
    parity_events = _as_list(parity["events"], "parity events")
    checks.add(
        "prefix_rebuild_and_shared_decision_parity",
        parity.get("passed") is True
        and len(parity_events) == len(accepted)
        and all(
            _as_mapping(event, "parity event").get("prefix_rebuild_equal") is True
            and _as_mapping(event, "parity event").get("final_artifact_equal")
            is True
            for event in parity_events
        ),
        {"events": len(parity_events), "passed": parity.get("passed")},
    )

    formal = _as_mapping(backtest["formal_execution_lane"], "formal lane")
    expected_blockers = {
        "HISTORICAL_ETF_TRADE_STATUS_UNAVAILABLE",
        "BROKER_VINTAGE_FEE_SCHEDULE_UNAVAILABLE",
        "HISTORICAL_QUANTITY_INCREMENTS_UNAVAILABLE",
        "HISTORICAL_SETTLEMENT_RULES_UNAVAILABLE",
        "HISTORICAL_PRICE_LIMIT_RULES_UNAVAILABLE",
    }
    formal_blockers = set(
        _as_list(formal["blocked_reason_codes"], "formal blockers")
    )
    checks.add(
        "formal_lane_fails_closed_before_orders",
        formal.get("performance_evaluable") is False
        and formal.get("empty_replay") is True
        and formal.get("order_count") == 0
        and formal.get("fill_count") == 0
        and formal_blockers == expected_blockers,
        {
            "intent_count": formal.get("intent_count"),
            "blockers": sorted(formal_blockers),
        },
    )

    diagnostic = _as_mapping(
        backtest["diagnostic_assumption_lane"], "diagnostic lane"
    )
    metrics = _as_mapping(diagnostic["metrics"], "diagnostic metrics")
    orders = _as_list(diagnostic["orders"], "orders")
    flat_fills = _as_list(diagnostic["fills"], "fills")
    cycles = _as_list(diagnostic["closed_cycles"], "closed cycles")
    rejections = _as_list(diagnostic["rejections"], "rejections")
    nested_fills: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for record in orders:
        row = _as_mapping(record, "order record")
        order = _as_mapping(row["order"], "order")
        match = _as_mapping(row["match"], "match")
        for fill in _as_list(match["fills"], "match fills"):
            nested_fills.append((order, _as_mapping(fill, "match fill")))

    checks.add(
        "order_fill_rejection_counts_reconcile",
        len(orders) == metrics["order_count"] == 5
        and len(flat_fills) == len(nested_fills) == metrics["fill_count"] == 2
        and len(rejections) == metrics["rejection_count"] == 3,
        {
            "orders": len(orders),
            "fills": len(flat_fills),
            "rejections": len(rejections),
        },
    )
    temporal_evidence: list[dict[str, object]] = []
    temporal_ok = True
    strict_price_ok = True
    source_trace_ok = True
    for order, fill in nested_fills:
        signal_at = _dt(order["signal_bar_end"])
        bar_opened_at = _dt(fill["bar_opened_at"])
        exchange_at = _dt(fill["exchange_time"])
        temporal_ok &= bar_opened_at >= signal_at and exchange_at > signal_at
        price = _decimal(fill["execution_price"])
        limit = _decimal(order["limit_price"])
        strict_price_ok &= price < limit if order["side"] == "buy" else price > limit
        source_trace_ok &= (
            str(fill["bar_source_id"]).startswith("financial-data-query:sha256:")
            and bool(fill["structure_snapshot_id"])
            and bool(fill["selection_snapshot_id"])
            and bool(fill["intent_id"])
        )
        temporal_evidence.append(
            {
                "side": order["side"],
                "signal_bar_end": order["signal_bar_end"],
                "bar_opened_at": fill["bar_opened_at"],
                "exchange_time": fill["exchange_time"],
            }
        )
    checks.add(
        "fills_only_after_signal_bar",
        temporal_ok and bool(nested_fills),
        temporal_evidence,
    )
    checks.add(
        "all_fills_strictly_cross_limit",
        strict_price_ok and bool(nested_fills),
        [
            {
                "side": order["side"],
                "limit": order["limit_price"],
                "fill": fill["execution_price"],
            }
            for order, fill in nested_fills
        ],
    )
    checks.add(
        "fill_trace_chain_complete",
        source_trace_ok and bool(nested_fills),
        {"fills": len(nested_fills)},
    )
    exact_touch_orders = [
        _as_mapping(record, "order record")
        for record in orders
        if "EXACT_LIMIT_TOUCH_NOT_FILLED"
        in _as_list(
            _as_mapping(
                _as_mapping(record, "order record")["match"], "match"
            )["rejection_and_unfilled_reasons"],
            "unfilled reasons",
        )
    ]
    checks.add(
        "exact_limit_touch_never_assumed_filled",
        bool(exact_touch_orders)
        and all(
            _as_mapping(row["match"], "match").get("filled_quantity") == 0
            for row in exact_touch_orders
        ),
        {"orders": len(exact_touch_orders)},
    )

    t1_ok = bool(cycles) and all(
        _dt(_as_mapping(cycle, "cycle")["closed_at"]).date()
        > _dt(_as_mapping(cycle, "cycle")["opened_at"]).date()
        for cycle in cycles
    )
    checks.add("T_plus_1_observed", t1_ok, {"cycles": len(cycles)})
    increments_ok = all(
        int(_as_mapping(fill, "fill")["quantity"]) % 100 == 0
        for fill in flat_fills
    ) and all(
        int(
            _as_mapping(_as_mapping(row, "order record")["order"], "order")[
                "quantity"
            ]
        )
        % 100
        == 0
        for row in orders
    )
    checks.add(
        "diagnostic_quantity_increment_observed",
        increments_ok,
        "100-share diagnostic increment",
    )

    initial_cash = _decimal(diagnostic["initial_cash"])
    cash = initial_cash
    fees = Decimal("0")
    for raw_fill in flat_fills:
        fill = _as_mapping(raw_fill, "flat fill")
        notional = _decimal(fill["price"]) * int(fill["quantity"])
        fee = _decimal(fill["fee"])
        fees += fee
        cash += notional - fee if fill["side"] == "sell" else -notional - fee
    final_cash = _decimal(diagnostic["final_cash"])
    checks.add(
        "cash_ledger_reconstructs_exactly",
        cash == final_cash,
        {"reconstructed": format(cash, "f"), "reported": format(final_cash, "f")},
    )
    checks.add(
        "fee_ledger_reconstructs_exactly",
        fees == _decimal(metrics["total_fees"]),
        {"reconstructed": format(fees, "f"), "reported": metrics["total_fees"]},
    )
    cycle_pnl_ok = all(
        _decimal(_as_mapping(cycle, "cycle")["exit_cash"])
        - _decimal(_as_mapping(cycle, "cycle")["entry_cash"])
        == _decimal(_as_mapping(cycle, "cycle")["net_pnl"])
        for cycle in cycles
    )
    checks.add(
        "closed_cycle_pnl_reconstructs_exactly",
        cycle_pnl_ok
        and len(cycles) == metrics["strategic_cycle_count"] == 1,
        {"cycles": len(cycles)},
    )
    expected_return = (final_cash - initial_cash) / initial_cash
    checks.add(
        "flat_final_equity_return_reconstructs_exactly",
        not diagnostic["open_positions"]
        and expected_return == _decimal(metrics["net_return"]),
        {
            "reconstructed": format(expected_return, "f"),
            "reported": metrics["net_return"],
        },
    )
    curve = _as_list(diagnostic["daily_equity_curve"], "daily equity curve")
    last_curve = _as_mapping(curve[-1], "last equity point")
    checks.add(
        "daily_equity_curve_closes_to_ledger",
        bool(curve)
        and _decimal(last_curve["cash"]) == final_cash
        and _decimal(last_curve["equity"]) == final_cash
        and last_curve["occupied_symbols"] == 0,
        {"sessions": len(curve), "last": dict(last_curve)},
    )
    checks.add(
        "sample_insufficiency_is_explicit",
        metrics.get("strategic_sample_insufficient") is True
        and metrics.get("tactical_sample_insufficient") is True
        and metrics.get("strategic_cycle_count", 0) < 100
        and metrics.get("tactical_cycle_count", 0) < 200,
        {
            "strategic": metrics.get("strategic_cycle_count"),
            "tactical": metrics.get("tactical_cycle_count"),
        },
    )
    checks.add(
        "walk_forward_has_no_refit_or_return_claim",
        _as_mapping(backtest["walk_forward"], "walk_forward").get("policy")
        == "FROZEN_PARAMETERS_NO_REFIT"
        and _as_mapping(backtest["walk_forward"], "walk_forward").get(
            "performance_evaluable"
        )
        is False,
        backtest["walk_forward"],
    )
    checks.add(
        "benchmark_and_ablation_are_diagnostic_only",
        _as_mapping(backtest["benchmark"], "benchmark").get(
            "price_return_only"
        )
        is True
        and _as_mapping(backtest["ablations"], "ablations").get(
            "return_comparison_allowed"
        )
        is False,
        {
            "benchmark": backtest["benchmark"],
            "ablations": backtest["ablations"],
        },
    )

    sizing = _as_list(diagnostic["sizing_decisions"], "sizing decisions")
    candidate_capacities = []
    for row in sizing:
        item = _as_mapping(row, "sizing row")
        decision = _as_mapping(item["decision"], "sizing decision")
        candidate = next(
            (
                _as_mapping(value, "candidate")
                for value in accepted
                if _as_mapping(value, "candidate")["point_id"] == item["point_id"]
            ),
            None,
        )
        if candidate is not None:
            matching_order = next(
                (
                    _as_mapping(
                        _as_mapping(value, "order record")["order"], "order"
                    )
                    for value in orders
                    if _as_mapping(
                        _as_mapping(value, "order record")["order"], "order"
                    )["structure_snapshot_id"]
                    == item["point_id"]
                ),
                None,
            )
            if matching_order is not None:
                candidate_capacities.append(
                    _decimal(matching_order["limit_price"])
                    * int(decision["quantity"])
                )
    minimum_capacity = min(candidate_capacities) if candidate_capacities else None

    report: dict[str, object] = {
        "schema": "chanlun-recursive-1m-independent-review",
        "review_mode": "READ_ONLY_SERIALIZED_EVIDENCE_RECOMPUTATION",
        "passed": checks.passed,
        "failed_checks": checks.failed_names,
        "checks": checks.rows,
        "reviewed_artifacts": {
            "prescreen": {
                "path": str(prescreen_path.resolve()),
                "content_sha256": prescreen["content_sha256"],
                "file_sha256": _file_sha256(prescreen_path),
            },
            "backtest": {
                "path": str(backtest_path.resolve()),
                "content_sha256": backtest["content_sha256"],
                "file_sha256": _file_sha256(backtest_path),
            },
            "data_gate": {
                "path": str(data_gate_path.resolve()),
                "content_sha256": data_gate["content_sha256"],
                "file_sha256": _file_sha256(data_gate_path),
            },
        },
        "recomputed": {
            "initial_cash": format(initial_cash, "f"),
            "final_cash_and_equity": format(final_cash, "f"),
            "net_return": format(expected_return, "f"),
            "total_fees": format(fees, "f"),
            "strategic_cycles": len(cycles),
            "tactical_cycles": metrics["tactical_cycle_count"],
            "orders": len(orders),
            "fills": len(flat_fills),
            "rejections": len(rejections),
            "minimum_diagnostic_candidate_capacity_notional": (
                None if minimum_capacity is None else format(minimum_capacity, "f")
            ),
        },
        "limitations": [
            "COMPONENT_ONLY",
            "DIAGNOSTIC_EXECUTION_ASSUMPTIONS_NOT_BROKER_VINTAGE",
            "FORMAL_PERFORMANCE_NOT_EVALUABLE",
            "INDIVIDUAL_THREE_PROGRAM_NOT_RUN",
            "SURVIVOR_RISK_UNRESOLVED",
        ],
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    report["content_sha256"] = _canonical_sha256(report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prescreen", type=Path, default=DEFAULT_PRESCREEN)
    parser.add_argument("--backtest", type=Path, default=DEFAULT_BACKTEST)
    parser.add_argument("--data-gate", type=Path, default=DEFAULT_DATA_GATE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    report = review(
        prescreen_path=args.prescreen,
        backtest_path=args.backtest,
        data_gate_path=args.data_gate,
    )
    _write_json(args.output, report)
    print(
        f"wrote {args.output}: passed={report['passed']} "
        f"checks={len(report['checks'])}"
    )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
