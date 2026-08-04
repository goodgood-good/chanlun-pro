from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
import hashlib
import json
from typing import Literal, Mapping, Sequence

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.v3_bar_execution import (
    HistoricalMinuteExecutionBar,
)
from chanlun.decision_support.trading_system.v3_multisymbol_replay import (
    V3_ETF_REQUIRED_CANDIDATE_GATES,
    strict_v3_replay_contract,
)
from chanlun.decision_support.trading_system.v3_parameters import (
    etf_parameter_snapshot,
)
from chanlun.decision_support.trading_system.v3_structure_signal_adapter import (
    V3_REQUIRED_STRUCTURE_RULES,
)
from chanlun.decision_support.trading_system.v31_timeframe_alignment import (
    V31AlignedEntryChain,
    v31_alignment_contract,
)


PRESCREEN_SCHEMA = "chanlun-v31-cached-symbol-prescreen/v1"
FACT_LEDGER_SCHEMA = "chanlun-v3-frozen-decision-fact-ledger/v1"
CORPORATE_ACTION_SCHEMA = "chanlun-qmt-etf-corporate-actions/v1"
REPLAY_INPUT_SCHEMA = "chanlun-strict-v3-multisymbol-replay-input/v1"
PAYLOAD_BUILD_SCHEMA = "chanlun-strict-v3-replay-payload-build/v1"
_CN_OPEN = time(9, 30)


@dataclass(frozen=True, slots=True)
class PayloadDiagnostic:
    severity: Literal["INFO", "UNRESOLVED", "ERROR"]
    code: str
    detail: str
    symbol: str | None = None
    fact_id: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayPayloadBuildResult:
    payload: dict[str, object]
    diagnostics: tuple[PayloadDiagnostic, ...]
    discovered_legal_chain_count: int
    generated_entry_event_count: int
    generated_structure_event_count: int
    generated_cash_distribution_count: int
    empty_replay: bool
    runnable: bool
    return_evaluation_allowed: bool
    build_parameter_set_id: str

    def report(self) -> dict[str, object]:
        return {
            "schema": PAYLOAD_BUILD_SCHEMA,
            "build_parameter_set_id": self.build_parameter_set_id,
            "discovered_legal_chain_count": self.discovered_legal_chain_count,
            "generated_entry_event_count": self.generated_entry_event_count,
            "generated_structure_event_count": self.generated_structure_event_count,
            "generated_cash_distribution_count": (
                self.generated_cash_distribution_count
            ),
            "empty_replay": self.empty_replay,
            "runnable": self.runnable,
            "return_evaluation_allowed": self.return_evaluation_allowed,
            "highest_status": "RESEARCH_ONLY",
            "live_status": "LIVE_DISABLED",
            "diagnostics": tuple(
                {
                    "severity": item.severity,
                    "code": item.code,
                    "detail": item.detail,
                    "symbol": item.symbol,
                    "fact_id": item.fact_id,
                }
                for item in self.diagnostics
            ),
            "payload": self.payload,
        }


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) for key in value
    ):
        raise TypeError(f"{name} must be a mapping")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{name} must be a sequence")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{name} must be a non-empty string")
    return value


def _datetime(value: object, name: str) -> datetime:
    parsed = datetime.fromisoformat(_string(value, name))
    return normalize_datetime(parsed, name)


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use an exact decimal string or integer")
    if not isinstance(value, (str, int)):
        raise TypeError(f"{name} must use an exact decimal string or integer")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    return parsed


def _project_to_provider(symbol: str) -> str:
    exchange, code = symbol.split(".", 1)
    if exchange not in {"SH", "SZ", "BJ"} or len(code) != 6:
        raise ValueError(f"unsupported project symbol: {symbol}")
    return f"{code}.{exchange}"


def _chain(value: Mapping[str, object]) -> V31AlignedEntryChain:
    return V31AlignedEntryChain(
        l0_point_id=_string(value.get("l0_point_id"), "chain.l0_point_id"),
        l0_center_id=_string(value.get("l0_center_id"), "chain.l0_center_id"),
        l1_departure_evidence_id=_string(
            value.get("l1_departure_evidence_id"),
            "chain.l1_departure_evidence_id",
        ),
        l1_return_evidence_id=_string(
            value.get("l1_return_evidence_id"),
            "chain.l1_return_evidence_id",
        ),
        l1_evidence_kind=_string(  # type: ignore[arg-type]
            value.get("l1_evidence_kind"),
            "chain.l1_evidence_kind",
        ),
        l2_locator_point_id=_string(
            value.get("l2_locator_point_id"),
            "chain.l2_locator_point_id",
        ),
        decision_at=_datetime(value.get("decision_at"), "chain.decision_at"),
        return_low=_decimal(value.get("return_low"), "chain.return_low"),
        l0_zg=_decimal(value.get("l0_zg"), "chain.l0_zg"),
        l2_confirmation_bar_high=_decimal(
            value.get("l2_confirmation_bar_high"),
            "chain.l2_confirmation_bar_high",
        ),
        structural_invalidation_price=_decimal(
            value.get("structural_invalidation_price"),
            "chain.structural_invalidation_price",
        ),
    )


def _bar_document(bar: HistoricalMinuteExecutionBar) -> dict[str, object]:
    return {
        "symbol": bar.symbol,
        "opened_at": bar.opened_at.isoformat(),
        "closed_at": bar.closed_at.isoformat(),
        "sequence": bar.sequence,
        "raw_open": format(bar.raw_open, "f"),
        "raw_high": format(bar.raw_high, "f"),
        "raw_low": format(bar.raw_low, "f"),
        "raw_close": format(bar.raw_close, "f"),
        "raw_volume": format(bar.raw_volume, "f"),
        "source_id": bar.source_id,
        "complete": bar.complete,
        "phase": bar.phase,
    }


def _mark_document(
    symbol: str,
    bar: HistoricalMinuteExecutionBar,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "available_at": bar.closed_at.isoformat(),
        "raw_close": format(bar.raw_close, "f"),
        "source_id": bar.source_id,
        "complete": bar.complete,
        "price_basis": "RAW_UNADJUSTED",
    }


def _latest_bar_at(
    bars: tuple[HistoricalMinuteExecutionBar, ...],
    observed_at: datetime,
) -> HistoricalMinuteExecutionBar | None:
    eligible = tuple(
        value
        for value in bars
        if value.complete and value.closed_at <= observed_at
    )
    return max(
        eligible,
        key=lambda row: (row.closed_at, row.sequence),
        default=None,
    )


def _first_bar_after(
    bars: tuple[HistoricalMinuteExecutionBar, ...],
    observed_at: datetime,
) -> HistoricalMinuteExecutionBar | None:
    eligible = tuple(
        value
        for value in bars
        if value.complete and value.closed_at > observed_at
    )
    return min(
        eligible,
        key=lambda row: (row.closed_at, row.sequence),
        default=None,
    )


def _session_bars_after(
    bars: tuple[HistoricalMinuteExecutionBar, ...],
    observed_at: datetime,
) -> tuple[HistoricalMinuteExecutionBar, ...]:
    return tuple(
        value
        for value in bars
        if value.complete
        and value.closed_at > observed_at
        and value.opened_at.date() == observed_at.date()
    )


def _bars_after(
    bars: tuple[HistoricalMinuteExecutionBar, ...],
    observed_at: datetime,
) -> tuple[HistoricalMinuteExecutionBar, ...]:
    """Every later completed bar, across session boundaries.

    Used only by non-expiring persistent exits; expiring optional orders must
    keep using the single-bar / same-session horizons.
    """

    return tuple(
        value
        for value in bars
        if value.complete and value.closed_at > observed_at
    )


def _sentinel_fee_model(started_at: datetime) -> dict[str, object]:
    return {
        "schedule_id": "EMPTY_REPLAY_NO_ORDER_FEE_SENTINEL",
        "currency_quantum": "0.01",
        "rates": (
            {
                "effective_from": started_at.date().isoformat(),
                "commission_rate": "0",
                "minimum_commission": "0",
                "stock_sell_stamp_rate": "0",
                "transfer_rate": "0",
                "other_buy_rate": "0",
                "other_sell_rate": "0",
            },
        ),
    }


def _corporate_hash(payload: Mapping[str, object]) -> str:
    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"generated_at", "content_sha256"}
    }
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _stable_content_hash(
    payload: Mapping[str, object],
    *,
    excluded: frozenset[str],
) -> str:
    stable = {key: value for key, value in payload.items() if key not in excluded}
    encoded = json.dumps(
        stable,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _valid_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith(
        "sha256:"
    ):
        return False
    try:
        int(value[7:], 16)
    except ValueError:
        return False
    return True


def _candidate_trace_reasons(
    candidate: Mapping[str, object],
    *,
    symbol: str,
    decision_time: datetime,
    parameter_set_id: str,
) -> tuple[str, ...]:
    checks = tuple(
        _mapping(item, "candidate.checks[]")
        for item in _sequence(candidate.get("checks", ()), "candidate.checks")
    )
    passed = {
        str(item.get("gate"))
        for item in checks
        if item.get("passed") is True
    }
    gates = tuple(str(item.get("gate")) for item in checks)
    reasons: list[str] = []
    if candidate.get("accepted") is not True:
        reasons.append("UNRESOLVED_CANDIDATE_NOT_ACCEPTED")
    if candidate.get("symbol") != symbol:
        reasons.append("UNRESOLVED_CANDIDATE_SYMBOL_BINDING")
    if candidate.get("selection_path") != "ETF_PROXY":
        reasons.append("UNRESOLVED_CANDIDATE_SELECTION_PATH_BINDING")
    if candidate.get("parameter_set_id") != parameter_set_id:
        reasons.append("UNRESOLVED_CANDIDATE_PARENT_PARAMETER_BINDING")
    if len(gates) != len(set(gates)) or not (
        V3_ETF_REQUIRED_CANDIDATE_GATES.issubset(passed)
    ):
        reasons.append("UNRESOLVED_INCOMPLETE_ETF_CANDIDATE_GATE_TRACE")
    confirmation = candidate.get("confirmation_time")
    try:
        confirmed_at = _datetime(
            confirmation,
            "candidate.confirmation_time",
        )
    except (TypeError, ValueError):
        reasons.append("UNRESOLVED_CANDIDATE_CONFIRMATION_TIME")
    else:
        if confirmed_at > decision_time:
            reasons.append("UNRESOLVED_CANDIDATE_CONFIRMATION_TIME")
    return tuple(dict.fromkeys(reasons))


def build_v3_replay_payload(
    *,
    prescreen_artifacts: Sequence[Mapping[str, object]],
    fact_ledger: Mapping[str, object] | None,
    bars_by_symbol: Mapping[str, tuple[HistoricalMinuteExecutionBar, ...]],
    corporate_action_ledger: Mapping[str, object] | None,
    initial_cash: Decimal,
    started_at: datetime,
) -> ReplayPayloadBuildResult:
    started = normalize_datetime(started_at, "started_at")
    if initial_cash <= 0:
        raise ValueError("payload initial cash must be positive")
    contract = strict_v3_replay_contract()
    diagnostics: list[PayloadDiagnostic] = []
    discovered: list[tuple[str, V31AlignedEntryChain, Mapping[str, object]]] = []
    for artifact in prescreen_artifacts:
        if artifact.get("schema") != PRESCREEN_SCHEMA:
            raise ValueError("prescreen schema is not supported")
        if (
            not _valid_sha256(artifact.get("content_sha256"))
            or artifact.get("content_sha256")
            != _stable_content_hash(
                artifact,
                excluded=frozenset({"content_sha256"}),
            )
        ):
            diagnostics.append(
                PayloadDiagnostic(
                    "ERROR",
                    "PRESCREEN_CONTENT_HASH_MISMATCH",
                    "prescreen content is missing its exact stable hash or was changed",
                )
            )
            continue
        if artifact.get("mapping") != {"L0": "30m", "L1": "5m", "L2": "1m"}:
            raise ValueError("prescreen timeframe mapping changed")
        if artifact.get("frozen_core_modified") is not False:
            raise ValueError("prescreen does not certify frozen-core zero change")
        alignment_bound = (
            artifact.get("alignment_contract")
            == v31_alignment_contract().document()
            and artifact.get("alignment_parameter_set_id")
            == contract.effective_alignment_parameter_set_id
        )
        for report_value in _sequence(
            artifact.get("symbol_reports", ()),
            "prescreen.symbol_reports",
        ):
            report = _mapping(report_value, "prescreen.symbol_reports[]")
            symbol = _string(report.get("project_code"), "project_code")
            count = int(report.get("structurally_legal_chain_count", 0))
            chain_rows = tuple(
                _mapping(item, "aligned_entry_chains[]")
                for item in _sequence(
                    report.get("aligned_entry_chains", ()),
                    "aligned_entry_chains",
                )
            )
            if count != len(chain_rows):
                raise ValueError("prescreen legal-chain count is inconsistent")
            if count and not alignment_bound:
                diagnostics.append(
                    PayloadDiagnostic(
                        "UNRESOLVED",
                        "UNRESOLVED_PRESCREEN_ALIGNMENT_V2_IDENTITY",
                        "legal chains cannot be promoted without the effective V2 identity",
                        symbol,
                    )
                )
                continue
            adjustment = _mapping(
                report.get("adjustment_gate", {}),
                "adjustment_gate",
            )
            if count and adjustment.get("formal_chain_eligibility") is not True:
                diagnostics.append(
                    PayloadDiagnostic(
                        "UNRESOLVED",
                        "UNRESOLVED_CAUSAL_ADJUSTMENT_LEDGER",
                        "raw structural chains were not promoted by the adjustment gate",
                        symbol,
                    )
                )
                continue
            for row in chain_rows:
                discovered.append((symbol, _chain(row), report))

    unique = {(symbol, chain.l0_point_id) for symbol, chain, _row in discovered}
    if len(unique) != len(discovered):
        raise ValueError("duplicate prescreen chain identity")
    discovered_count = len(discovered)

    if fact_ledger is None:
        if discovered_count:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_FROZEN_DECISION_FACT_LEDGER_MISSING",
                    "chains exist but selection/risk/exit/tactical facts are absent",
                )
            )
        fee_model = _sentinel_fee_model(started)
        candidate_rows: tuple[Mapping[str, object], ...] = ()
        coverage_rows: tuple[Mapping[str, object], ...] = ()
        signal_rows: tuple[Mapping[str, object], ...] = ()
        status_rows: tuple[Mapping[str, object], ...] = ()
        latency = 0
        fact_identities_ok = False
    else:
        if fact_ledger.get("schema") != FACT_LEDGER_SCHEMA:
            raise ValueError("frozen decision fact ledger schema is unsupported")
        fact_hash_ok = (
            _valid_sha256(fact_ledger.get("content_sha256"))
            and fact_ledger.get("content_sha256")
            == _stable_content_hash(
                fact_ledger,
                excluded=frozenset({"generated_at", "content_sha256"}),
            )
        )
        if not fact_hash_ok:
            diagnostics.append(
                PayloadDiagnostic(
                    "ERROR",
                    "FACT_LEDGER_CONTENT_HASH_MISMATCH",
                    "frozen decision facts are missing their exact stable hash or were changed",
                )
            )
        fact_identities_ok = (
            fact_hash_ok
            and fact_ledger.get("strategy_parameter_set_id")
            == etf_parameter_snapshot().parameter_set_id
            and fact_ledger.get("timeframe_override_parameter_set_id")
            == contract.timeframe_override_parameter_set_id
            and fact_ledger.get("alignment_contract_id")
            == contract.effective_alignment_contract_id
            and fact_ledger.get("alignment_parameter_set_id")
            == contract.effective_alignment_parameter_set_id
        )
        if not fact_identities_ok:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_FACT_LEDGER_PARAMETER_BINDING",
                    "fact ledger is not bound to parent ETF V3 + override + V2",
                )
            )
        fee_model = dict(_mapping(fact_ledger.get("fee_model"), "fee_model"))
        candidate_rows = tuple(
            _mapping(item, "entry_facts[]")
            for item in _sequence(fact_ledger.get("entry_facts", ()), "entry_facts")
        )
        coverage_rows = tuple(
            _mapping(item, "structure_coverage[]")
            for item in _sequence(
                fact_ledger.get("structure_coverage", ()),
                "structure_coverage",
            )
        )
        signal_rows = tuple(
            _mapping(item, "structure_signal_facts[]")
            for item in _sequence(
                fact_ledger.get("structure_signal_facts", ()),
                "structure_signal_facts",
            )
        )
        status_rows = tuple(
            _mapping(item, "execution_status_facts[]")
            for item in _sequence(
                fact_ledger.get("execution_status_facts", ()),
                "execution_status_facts",
            )
        )
        policy = _mapping(fact_ledger.get("execution_policy"), "execution_policy")
        latency = int(policy.get("broker_latency_seconds", -1))
        if latency < 0 or policy.get("optional_ttl_l2_completed_bars") != 1:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_EXECUTION_POLICY",
                    "explicit nonnegative latency and one completed L2 bar TTL are required",
                )
            )
            latency = 0

    candidates = {
        (str(row.get("symbol")), str(row.get("l0_point_id"))): row
        for row in candidate_rows
    }
    if len(candidates) != len(candidate_rows):
        raise ValueError("duplicate entry fact identity")
    coverages = {str(row.get("symbol")): row for row in coverage_rows}
    if len(coverages) != len(coverage_rows):
        raise ValueError("duplicate structure coverage symbol")
    statuses = {
        (str(row.get("symbol")), str(row.get("effective_session"))): row
        for row in status_rows
    }
    if len(statuses) != len(status_rows):
        raise ValueError("duplicate point-in-time execution status")

    grouped: dict[datetime, dict[str, list[dict[str, object]]]] = {}

    def group(at: datetime) -> dict[str, list[dict[str, object]]]:
        return grouped.setdefault(
            at,
            {"events": [], "cash_distributions": []},
        )

    generated_symbols: dict[str, datetime] = {}
    generated_ends: dict[str, datetime] = {}
    entry_count = 0
    structure_count = 0

    def status_for(symbol: str, observed: datetime) -> Mapping[str, object] | None:
        row = statuses.get((symbol, observed.date().isoformat()))
        if row is None:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_EXECUTION_STATUS_FACT_MISSING",
                    f"no status for {observed.date().isoformat()}",
                    symbol,
                )
            )
        return row

    for symbol, aligned, report in sorted(
        discovered,
        key=lambda value: (value[1].decision_at, value[0], value[1].l0_point_id),
    ):
        entry = candidates.get((symbol, aligned.l0_point_id))
        if not fact_identities_ok:
            continue
        if entry is None:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_ENTRY_SELECTION_RISK_FACTS_MISSING",
                    "aligned chain has no matching evaluated ETF candidate",
                    symbol,
                    aligned.l0_point_id,
                )
            )
            continue
        entry_decision = _datetime(entry.get("decision_time"), "entry.decision_time")
        if entry_decision != aligned.decision_at:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_ENTRY_DECISION_TIME_MISMATCH",
                    "candidate decision time differs from aligned-chain availability",
                    symbol,
                    aligned.l0_point_id,
                )
            )
            continue
        candidate = _mapping(entry.get("candidate"), "entry.candidate")
        candidate_reasons = _candidate_trace_reasons(
            candidate,
            symbol=symbol,
            decision_time=entry_decision,
            parameter_set_id=contract.strategy_parameter_set_id,
        )
        if candidate_reasons:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    candidate_reasons[0],
                    "candidate is not bound to the accepted parent V3 ETF trace: "
                    + ",".join(candidate_reasons),
                    symbol,
                    aligned.l0_point_id,
                )
            )
            continue
        q_plan = entry.get("q_plan")
        if isinstance(q_plan, bool) or not isinstance(q_plan, int) or q_plan <= 0:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_ENTRY_Q_PLAN_NOT_POSITIVE_INTEGER",
                    "an accepted entry fact requires an explicit positive integer Q_PLAN",
                    symbol,
                    aligned.l0_point_id,
                )
            )
            continue
        coverage = coverages.get(symbol)
        source_end = datetime.fromisoformat(str(report.get("source_end"))).date()
        rule_coverage_value = (
            None if coverage is None else coverage.get("rule_coverage")
        )
        rule_coverage_ok = (
            isinstance(rule_coverage_value, Mapping)
            and all(
                rule_coverage_value.get(rule) == "COMPLETE"
                for rule in V3_REQUIRED_STRUCTURE_RULES
            )
        )
        coverage_ok = (
            coverage is not None
            and coverage.get("complete") is True
            and coverage.get("recursive_level") == 0
            and tuple(coverage.get("frequencies", ())) == ("30m", "5m", "1m")
            and datetime.fromisoformat(str(coverage.get("start_at"))).date()
            <= aligned.decision_at.date()
            and datetime.fromisoformat(str(coverage.get("end_at"))).date()
            >= source_end
            and _valid_sha256(coverage.get("source_ledger_sha256"))
            and coverage.get("missing_data_was_inferred") is False
            and rule_coverage_ok
        )
        if not coverage_ok:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_POST_ENTRY_STRUCTURE_COVERAGE",
                    "complete 30m/5m/1m level-zero coverage, every V3 strategic/tactical rule, and an exact source hash are required through source end",
                    symbol,
                    aligned.l0_point_id,
                )
            )
            continue
        execution_status = status_for(symbol, aligned.decision_at)
        bars = bars_by_symbol.get(symbol, ())
        decision_bar = _latest_bar_at(bars, aligned.decision_at)
        next_bar = _first_bar_after(bars, aligned.decision_at)
        if execution_status is None or decision_bar is None or next_bar is None:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_ENTRY_RAW_1M_EXECUTION_WINDOW",
                    "decision mark and next completed raw 1m bar are required",
                    symbol,
                    aligned.l0_point_id,
                )
            )
            continue
        structure_snapshot = _string(
            entry.get("structure_snapshot_id"),
            "entry.structure_snapshot_id",
        )
        fact_ids = tuple(
            dict.fromkeys(
                (
                    structure_snapshot,
                    aligned.l0_point_id,
                    aligned.l0_center_id,
                    aligned.l1_departure_evidence_id,
                    aligned.l1_return_evidence_id,
                    aligned.l2_locator_point_id,
                    *tuple(entry.get("frozen_structure_fact_ids", ())),
                )
            )
        )
        confirmed = aligned.decision_at + timedelta(seconds=latency)
        if confirmed > next_bar.closed_at:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_BROKER_LATENCY_EXCEEDS_ENTRY_TTL",
                    "broker confirmation occurs after the only admissible L2 bar",
                    symbol,
                    aligned.l0_point_id,
                )
            )
            continue
        event = {
            "event_id": f"entry:{symbol}:{aligned.l0_point_id}",
            "facts": {
                "symbol": symbol,
                "decision_time": aligned.decision_at.isoformat(),
                "confirmation_time": aligned.decision_at.isoformat(),
                "structure_snapshot_id": structure_snapshot,
                "selection_snapshot_id": _string(
                    entry.get("selection_snapshot_id"),
                    "entry.selection_snapshot_id",
                ),
                "account_snapshot_id": _string(
                    entry.get("account_snapshot_id"),
                    "entry.account_snapshot_id",
                ),
                "strategic_state": "S_ENTRY_READY",
                "health": dict(_mapping(entry.get("health"), "entry.health")),
                "strategic": {},
                "tactical": {},
                "cycle_ledger": None,
                "candidate": dict(candidate),
                "q_plan": q_plan,
                "price_cap_or_floor": format(
                    aligned.l2_confirmation_bar_high,
                    "f",
                ),
                "active_order_id": None,
                "all_structure_inputs_completed": True,
            },
            "bindings": {
                "timeframe_override_parameter_set_id": (
                    contract.timeframe_override_parameter_set_id
                ),
                "alignment_contract_id": contract.effective_alignment_contract_id,
                "alignment_parameter_set_id": (
                    contract.effective_alignment_parameter_set_id
                ),
                "frozen_structure_fact_ids": fact_ids,
                "selection_fact_ids": tuple(entry.get("selection_fact_ids", ())),
                "risk_fact_ids": tuple(entry.get("risk_fact_ids", ())),
                "aligned_entry_chain": {
                    "l0_point_id": aligned.l0_point_id,
                    "l0_center_id": aligned.l0_center_id,
                    "l1_departure_evidence_id": aligned.l1_departure_evidence_id,
                    "l1_return_evidence_id": aligned.l1_return_evidence_id,
                    "l1_evidence_kind": aligned.l1_evidence_kind,
                    "l2_locator_point_id": aligned.l2_locator_point_id,
                    "decision_at": aligned.decision_at.isoformat(),
                    "return_low": format(aligned.return_low, "f"),
                    "l0_zg": format(aligned.l0_zg, "f"),
                    "l2_confirmation_bar_high": format(
                        aligned.l2_confirmation_bar_high,
                        "f",
                    ),
                    "structural_invalidation_price": format(
                        aligned.structural_invalidation_price,
                        "f",
                    ),
                },
                "all_required_facts_resolved": True,
                "unresolved_reason_codes": (),
            },
            "created_at": aligned.decision_at.isoformat(),
            "broker_confirmed_at": confirmed.isoformat(),
            "expires_at": next_bar.closed_at.isoformat(),
            "execution_status": dict(execution_status),
            "broker_position_quantity": None,
            "bars": tuple(
                _bar_document(value)
                for value in bars
                if value.closed_at >= aligned.decision_at
                and value.closed_at <= next_bar.closed_at
            ),
            "account_position_source": "EVENT_SOURCED_REPLAY",
            "sellable_quantity_source": "EVENT_SOURCED_REPLAY",
        }
        group(aligned.decision_at)["events"].append(event)
        generated_symbols[symbol] = min(
            generated_symbols.get(symbol, aligned.decision_at),
            aligned.decision_at,
        )
        generated_ends[symbol] = datetime.combine(
            source_end,
            time(15, 0),
            tzinfo=aligned.decision_at.tzinfo,
        )
        entry_count += 1

    for row in sorted(
        signal_rows,
        key=lambda item: (
            str(item.get("decision_time")),
            str(item.get("symbol")),
            str(item.get("event_id")),
        ),
    ):
        symbol = str(row.get("symbol"))
        if symbol not in generated_symbols:
            continue
        event_id = str(row.get("event_id"))
        if row.get("emit_to_replay") is False:
            reasons = tuple(str(value) for value in row.get("unresolved_reason_codes", ()))
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED" if reasons else "INFO",
                    (
                        reasons[0]
                        if reasons
                        else "STRUCTURE_SIGNAL_NOT_ELIGIBLE_UNDER_V3"
                    ),
                    "frozen structural observation was audited but cannot become a replay event",
                    symbol,
                    event_id,
                )
            )
            continue
        if row.get("all_required_facts_resolved", True) is not True:
            reasons = tuple(
                str(value)
                for value in row.get("unresolved_reason_codes", ())
            )
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    (
                        reasons[0]
                        if reasons
                        else "UNRESOLVED_STRUCTURE_SIGNAL_FACTS"
                    ),
                    "structure signal explicitly carries unresolved required facts",
                    symbol,
                    event_id,
                )
            )
            continue
        coverage = coverages.get(symbol)
        if (
            coverage is None
            or row.get("source_ledger_sha256")
            != coverage.get("source_ledger_sha256")
        ):
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_STRUCTURE_SIGNAL_SOURCE_LEDGER_BINDING",
                    "signal does not bind the certified full-coverage structure ledger",
                    symbol,
                    event_id,
                )
            )
            continue
        decision = _datetime(row.get("decision_time"), "signal.decision_time")
        if decision < generated_symbols[symbol]:
            continue
        if decision > generated_ends[symbol]:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_STRUCTURE_SIGNAL_AFTER_CERTIFIED_COVERAGE",
                    "signal lies after the certified source end",
                    symbol,
                    str(row.get("event_id")),
                )
            )
            continue
        completed = (
            row.get("completed") is True
            and row.get("recursive_level") == 0
            and tuple(row.get("source_frequencies", ()))
            in {
                ("30m",),
                ("5m",),
                ("1m",),
                ("5m", "1m"),
                ("30m", "5m"),
                ("30m", "5m", "1m"),
            }
        )
        if not completed:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_NONCOMPLETED_STRUCTURE_SIGNAL_FACT",
                    "only frozen completed independent level-zero changes are admissible",
                    symbol,
                    event_id,
                )
            )
            continue
        execution_status = status_for(symbol, decision)
        bars = bars_by_symbol.get(symbol, ())
        decision_bar = _latest_bar_at(bars, decision)
        persistence = str(row.get("execution_persistence"))
        if persistence == "OPTIONAL":
            next_bar = _first_bar_after(bars, decision)
            end = None if next_bar is None else next_bar.closed_at
        elif persistence == "PERSISTENT_EXIT":
            # R-04：持久战略退出不设到期（引擎要求 expires_at is None）。若只喂
            # 决策当日的剩余分钟柱，被 T+1、停牌或跌停无成交阻断的退出就再也拿不
            # 到可成交柱，等于静默放弃——而 §11.3/§10 要求它在每个可执行机会继续，
            # 直到真实仓位归零。因此供给延伸到可得数据尽头，由引擎自身的可卖量、
            # 涨跌停与严格穿价约束决定何时真正成交。
            continuation_bars = _bars_after(bars, decision)
            end = None if not continuation_bars else continuation_bars[-1].closed_at
        elif persistence == "NONE":
            end = decision
        else:
            end = None
        if execution_status is None or decision_bar is None or end is None:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_STRUCTURE_EVENT_EXECUTION_WINDOW",
                    "status, decision mark and explicit persistence horizon are required",
                    symbol,
                    event_id,
                )
            )
            continue
        confirmation = _datetime(
            row.get("confirmation_time"),
            "signal.confirmation_time",
        )
        broker_confirmed = decision + timedelta(seconds=latency)
        event = {
            "event_id": _string(row.get("event_id"), "signal.event_id"),
            "facts": {
                "symbol": symbol,
                "decision_time": decision.isoformat(),
                "confirmation_time": confirmation.isoformat(),
                "structure_snapshot_id": _string(
                    row.get("structure_snapshot_id"),
                    "signal.structure_snapshot_id",
                ),
                "selection_snapshot_id": None,
                "account_snapshot_id": _string(
                    row.get("account_snapshot_id"),
                    "signal.account_snapshot_id",
                ),
                "strategic_state": "S_ACTIVE_FULL",
                "health": dict(_mapping(row.get("health"), "signal.health")),
                "strategic": dict(
                    _mapping(row.get("strategic", {}), "signal.strategic")
                ),
                "tactical": dict(
                    _mapping(row.get("tactical", {}), "signal.tactical")
                ),
                "cycle_ledger": None,
                "candidate": None,
                "q_plan": 0,
                "price_cap_or_floor": row.get("price_cap_or_floor"),
                "active_order_id": None,
                "all_structure_inputs_completed": True,
            },
            "bindings": {
                "timeframe_override_parameter_set_id": (
                    contract.timeframe_override_parameter_set_id
                ),
                "alignment_contract_id": None,
                "alignment_parameter_set_id": None,
                "frozen_structure_fact_ids": tuple(
                    dict.fromkeys(
                        (
                            str(row.get("structure_snapshot_id")),
                            *tuple(row.get("frozen_structure_fact_ids", ())),
                        )
                    )
                ),
                "selection_fact_ids": (),
                "risk_fact_ids": tuple(row.get("risk_fact_ids", ())),
                "aligned_entry_chain": None,
                "all_required_facts_resolved": True,
                "unresolved_reason_codes": (),
            },
            "created_at": decision.isoformat(),
            "broker_confirmed_at": broker_confirmed.isoformat(),
            "expires_at": end.isoformat() if persistence == "OPTIONAL" else None,
            "execution_status": dict(execution_status),
            "broker_position_quantity": None,
            "bars": tuple(
                _bar_document(value)
                for value in bars
                if value.closed_at >= confirmation and value.closed_at <= end
            ),
            "account_position_source": "EVENT_SOURCED_REPLAY",
            "sellable_quantity_source": "EVENT_SOURCED_REPLAY",
        }
        group(decision)["events"].append(event)
        structure_count += 1

    cash_count = 0
    if generated_symbols:
        if corporate_action_ledger is None:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_CORPORATE_ACTION_LEDGER_MISSING",
                    "post-entry cash/share actions cannot be assumed absent",
                )
            )
        elif corporate_action_ledger.get("schema") != CORPORATE_ACTION_SCHEMA:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_CORPORATE_ACTION_LEDGER_SCHEMA",
                    "unsupported corporate-action ledger",
                )
            )
        elif corporate_action_ledger.get("content_sha256") != _corporate_hash(
            corporate_action_ledger
        ):
            diagnostics.append(
                PayloadDiagnostic(
                    "ERROR",
                    "CORPORATE_ACTION_LEDGER_HASH_MISMATCH",
                    "corporate-action content hash failed",
                )
            )
        else:
            ledger_hash = str(corporate_action_ledger.get("content_sha256"))
            instruments = {
                str(item.get("code")): item
                for value in _sequence(
                    corporate_action_ledger.get("instruments", ()),
                    "corporate.instruments",
                )
                for item in (_mapping(value, "corporate.instruments[]"),)
            }
            for symbol, entry_at in sorted(generated_symbols.items()):
                instrument = instruments.get(_project_to_provider(symbol))
                if (
                    instrument is None
                    or instrument.get("status")
                    != "EFFECTIVE_DATED_EVENTS_AVAILABLE"
                ):
                    diagnostics.append(
                        PayloadDiagnostic(
                            "UNRESOLVED",
                            "UNRESOLVED_SYMBOL_CORPORATE_ACTION_COVERAGE",
                            "symbol has no certified effective-dated event ledger",
                            symbol,
                        )
                    )
                    continue
                for value in _sequence(instrument.get("events", ()), "events"):
                    action = _mapping(value, "corporate.events[]")
                    effective_date = datetime.fromisoformat(
                        str(action.get("effective_on"))
                    ).date()
                    effective = datetime.combine(
                        effective_date,
                        _CN_OPEN,
                        tzinfo=entry_at.tzinfo,
                    )
                    if effective < entry_at:
                        continue
                    if effective > generated_ends[symbol]:
                        continue
                    raw = _mapping(action.get("raw"), "corporate.raw")
                    unsupported = any(
                        Decimal(str(raw.get(field, 0))) != 0
                        for field in (
                            "stockBonus",
                            "stockGift",
                            "allotNum",
                            "gugai",
                        )
                    )
                    if unsupported:
                        diagnostics.append(
                            PayloadDiagnostic(
                                "UNRESOLVED",
                                "UNRESOLVED_UNSUPPORTED_SHARE_ACTION",
                                "non-cash quantity action requires broker quantity facts",
                                symbol,
                                str(action.get("effective_on")),
                            )
                        )
                        continue
                    cash = Decimal(str(raw.get("interest", 0)))
                    if cash < 0:
                        diagnostics.append(
                            PayloadDiagnostic(
                                "ERROR",
                                "NEGATIVE_CORPORATE_CASH_DISTRIBUTION",
                                "cash distribution cannot be negative",
                                symbol,
                            )
                        )
                        continue
                    action_id = sha256_json(
                        {
                            "ledger": ledger_hash,
                            "symbol": symbol,
                            "effective": effective,
                            "cash": cash,
                        }
                    )
                    group(effective)["cash_distributions"].append(
                        {
                            "action_id": action_id,
                            "symbol": symbol,
                            "effective_at": effective.isoformat(),
                            "known_at": effective.isoformat(),
                            "cash_per_share": format(cash, "f"),
                            "source_id": (
                                f"QMT:{_project_to_provider(symbol)}:"
                                f"{effective_date.isoformat()}"
                            ),
                            "source_ledger_sha256": ledger_hash,
                            "point_in_time_complete": True,
                        }
                    )
                    cash_count += 1

    decision_times = sorted(grouped)
    batches: list[dict[str, object]] = []
    previous_valuation = started
    for index, decision in enumerate(decision_times):
        values = grouped[decision]
        events = values["events"]
        actions = values["cash_distributions"]
        desired = decision
        for event in events:
            event_bars = event.get("bars", ())
            for value in event_bars if isinstance(event_bars, Sequence) else ():
                desired = max(desired, _datetime(value["closed_at"], "bar.closed_at"))
        if actions and desired == decision:
            action_symbols = tuple(str(value["symbol"]) for value in actions)
            action_bars = tuple(
                _first_bar_after(bars_by_symbol.get(symbol, ()), decision)
                for symbol in action_symbols
            )
            if any(value is None for value in action_bars):
                diagnostics.append(
                    PayloadDiagnostic(
                        "UNRESOLVED",
                        "UNRESOLVED_CORPORATE_ACTION_VALUATION_BAR",
                        "cash-action batch lacks a completed raw 1m valuation bar",
                    )
                )
            else:
                desired = max(value.closed_at for value in action_bars if value)
        next_decision = (
            decision_times[index + 1] if index + 1 < len(decision_times) else None
        )
        if next_decision is not None and desired > next_decision:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_OVERLAPPING_EVENT_HORIZONS",
                    f"{decision.isoformat()} horizon crosses {next_decision.isoformat()}",
                )
            )
            continue
        if decision < previous_valuation:
            diagnostics.append(
                PayloadDiagnostic(
                    "UNRESOLVED",
                    "UNRESOLVED_NONCAUSAL_BATCH_ORDER",
                    decision.isoformat(),
                )
            )
            continue
        symbols = tuple(
            dict.fromkeys(
                tuple(str(event["facts"]["symbol"]) for event in events)
                + tuple(str(action["symbol"]) for action in actions)
            )
        )
        decision_marks: list[dict[str, object]] = []
        valuation_marks: list[dict[str, object]] = []
        for symbol in symbols:
            current = _latest_bar_at(bars_by_symbol.get(symbol, ()), decision)
            ending = _latest_bar_at(bars_by_symbol.get(symbol, ()), desired)
            if current is not None:
                decision_marks.append(_mark_document(symbol, current))
            if ending is not None:
                valuation_marks.append(_mark_document(symbol, ending))
        batches.append(
            {
                "batch_id": f"v3-batch:{decision.isoformat()}",
                "decision_at": decision.isoformat(),
                "valuation_at": desired.isoformat(),
                "events": tuple(events),
                "decision_marks": tuple(decision_marks),
                "valuation_marks": tuple(valuation_marks),
                "cash_distributions": tuple(actions),
            }
        )
        previous_valuation = desired

    if discovered_count == 0:
        diagnostics.append(
            PayloadDiagnostic(
                "INFO",
                "STRUCTURALLY_LEGAL_CHAIN_ZERO_EMPTY_REPLAY",
                "no entry right exists; a legal zero-order replay is emitted",
            )
        )
    elif entry_count == 0:
        diagnostics.append(
            PayloadDiagnostic(
                "UNRESOLVED",
                "NO_ENTRY_EVENT_SURVIVED_FACT_GATES",
                "structural chains exist but complete entry/coverage facts do not",
            )
        )
    if fact_ledger is None:
        diagnostics.append(
            PayloadDiagnostic(
                "INFO",
                "EMPTY_REPLAY_FEE_SENTINEL_NOT_USED",
                "the sentinel fee model cannot be used by an order",
            )
        )
    payload: dict[str, object] = {
        "schema": REPLAY_INPUT_SCHEMA,
        "initial_cash": format(initial_cash, "f"),
        "started_at": started.isoformat(),
        "fee_model": fee_model,
        "batches": tuple(batches),
        "builder_contract": {
            "strategy_parameter_set_id": contract.strategy_parameter_set_id,
            "timeframe_override_parameter_set_id": (
                contract.timeframe_override_parameter_set_id
            ),
            "alignment_contract_id": contract.effective_alignment_contract_id,
            "alignment_parameter_set_id": (
                contract.effective_alignment_parameter_set_id
            ),
            "execution_parameter_set_id": contract.execution_parameter_set_id,
            "live_status": "LIVE_DISABLED",
        },
    }
    unresolved = any(item.severity in {"UNRESOLVED", "ERROR"} for item in diagnostics)
    empty = entry_count == 0
    runnable = not any(item.severity == "ERROR" for item in diagnostics)
    return_allowed = entry_count > 0 and not unresolved
    build_identity = {
        "contract": payload["builder_contract"],
        "prescreen_content_ids": tuple(
            str(item.get("content_sha256")) for item in prescreen_artifacts
        ),
        "fact_ledger_content_id": (
            None if fact_ledger is None else fact_ledger.get("content_sha256")
        ),
        "corporate_content_id": (
            None
            if corporate_action_ledger is None
            else corporate_action_ledger.get("content_sha256")
        ),
    }
    return ReplayPayloadBuildResult(
        payload=payload,
        diagnostics=tuple(diagnostics),
        discovered_legal_chain_count=discovered_count,
        generated_entry_event_count=entry_count,
        generated_structure_event_count=structure_count,
        generated_cash_distribution_count=cash_count,
        empty_replay=empty,
        runnable=runnable,
        return_evaluation_allowed=return_allowed,
        build_parameter_set_id=sha256_json(build_identity),
    )


__all__ = [
    "CORPORATE_ACTION_SCHEMA",
    "FACT_LEDGER_SCHEMA",
    "PAYLOAD_BUILD_SCHEMA",
    "PRESCREEN_SCHEMA",
    "PayloadDiagnostic",
    "REPLAY_INPUT_SCHEMA",
    "ReplayPayloadBuildResult",
    "build_v3_replay_payload",
]
