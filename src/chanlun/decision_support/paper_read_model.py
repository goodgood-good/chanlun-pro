"""Read-only research-paper projection for the decision-support API."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import re
from typing import Mapping, Protocol

from .exit_evaluation_store import (
    SQLiteExitEvaluationStore,
)
from .paper_adapter import (
    PaperFill,
    PaperIntent,
    PaperLedgerState,
    PaperLot,
    reconcile_paper_ledger,
)
from .paper_admission import PaperAccountSnapshot
from .strategy_run import (
    STRATEGY_RUN_MUTATION_LEASE_PROTOCOL,
    STRATEGY_RUN_SWITCH_CAPABILITY,
)


class _PaperLedgerReader(Protocol):
    def load(self) -> PaperLedgerState: ...

    def account_snapshot(self) -> PaperAccountSnapshot: ...


def _research_payload(**values: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": "research_paper",
        "read_only": True,
        "auto_order_enabled": False,
        "live_order_capability": False,
        **values,
    }


def _intent_payload(intent: PaperIntent) -> dict[str, object]:
    return {
        "event_id": intent.event_id,
        "event_data_fingerprint": intent.event_data_fingerprint,
        "review_id": intent.review_id,
        "risk_snapshot_id": intent.risk_snapshot_id,
        "admission_authorization_id": intent.admission_authorization_id,
        "admission_payload_fingerprint": (
            intent.admission_payload_fingerprint
        ),
        "admitted_at": intent.admitted_at.isoformat(),
        "risk_expires_at": intent.risk_expires_at.isoformat(),
        "entry_event_id": intent.entry_event_id,
        "code": intent.code,
        "side": intent.side,
        "risk_shares": intent.risk_shares,
        "requested_shares": intent.requested_shares,
        "remaining_shares": intent.remaining_shares,
        "signal_bar_id": intent.signal_bar_id,
        "signal_at": intent.signal_at.isoformat(),
        "limit_pct": str(intent.limit_pct),
        "status": intent.status,
        "reason": intent.reason,
        "fee_schedule_fingerprint": intent.fee_schedule_fingerprint,
        "execution_policy_fingerprint": (
            intent.execution_policy_fingerprint
        ),
    }


def _fill_payload(fill: PaperFill) -> dict[str, object]:
    return {
        "fill_id": fill.fill_id,
        "event_id": fill.event_id,
        "entry_event_id": fill.entry_event_id,
        "review_id": fill.review_id,
        "risk_snapshot_id": fill.risk_snapshot_id,
        "code": fill.code,
        "side": fill.side,
        "shares": fill.shares,
        "reference_price": str(fill.reference_price),
        "price": str(fill.price),
        "gross_value": str(fill.gross_value),
        "commission": str(fill.commission),
        "stamp_duty": str(fill.stamp_duty),
        "transfer_fee": str(fill.transfer_fee),
        "regulatory_fee": str(fill.regulatory_fee),
        "slippage_cost": str(fill.slippage_cost),
        "trade_cost": str(fill.trade_cost),
        "filled_at": fill.filled_at.isoformat(),
        "bar_id": fill.bar_id,
    }


def _position_payload(lots: list[PaperLot]) -> dict[str, object]:
    first = lots[0]
    shares = sum(lot.shares for lot in lots)
    average_price = sum(
        (lot.price * lot.shares for lot in lots),
        start=Decimal("0"),
    ) / shares
    return {
        "code": first.code,
        "shares": shares,
        "average_price": str(average_price),
        "opened_at": min(lot.opened_at for lot in lots).isoformat(),
        "entry_event_id": first.entry_event_id,
        "entry_review_id": first.entry_review_id,
        "entry_risk_snapshot_id": first.entry_risk_snapshot_id,
    }


class PaperResearchReadModel:
    """Expose paper-ledger state without any admission or execution method."""

    def __init__(
        self,
        ledger: _PaperLedgerReader,
        *,
        exit_store: SQLiteExitEvaluationStore,
        runtime: object | None = None,
        policy_provider: object | None = None,
        strategy_run: object | None = None,
    ) -> None:
        if not callable(getattr(ledger, "load", None)) or not callable(
            getattr(ledger, "account_snapshot", None)
        ):
            raise TypeError("ledger must provide read-only snapshots")
        if not isinstance(exit_store, SQLiteExitEvaluationStore):
            raise TypeError("exit_store must be SQLiteExitEvaluationStore")
        if runtime is not None and (
            not callable(getattr(runtime, "health", None))
            or not callable(
                getattr(runtime, "attest_exit_snapshots", None)
            )
        ):
            raise TypeError(
                "runtime must provide health and attest_exit_snapshots"
            )
        if strategy_run is not None and not callable(
            getattr(strategy_run, "status_payload", None)
        ):
            raise TypeError("strategy_run must provide status_payload")
        if policy_provider is not None:
            for field_name in (
                "fee_schedule_fingerprint",
                "execution_policy_fingerprint",
            ):
                value = getattr(policy_provider, field_name, None)
                if (
                    not isinstance(value, str)
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
                ):
                    raise TypeError(
                        "policy_provider must expose immutable fingerprints"
                    )
        self._ledger = ledger
        self._exit_store = exit_store
        self._runtime = runtime
        self._policy_provider = policy_provider
        self._strategy_run = strategy_run

    def status(self) -> dict[str, object]:
        reconciliation = reconcile_paper_ledger(self._ledger)
        payload = _research_payload(
            ledger_revision=reconciliation.revision,
            exit_evaluation_revision=self._exit_store.revision,
            intent_count=reconciliation.intent_count,
            pending_intent_count=reconciliation.pending_intent_count,
            fill_count=reconciliation.fill_count,
            lot_count=reconciliation.lot_count,
            position_count=reconciliation.position_count,
        )
        if self._policy_provider is not None:
            payload.update(
                fee_schedule_fingerprint=getattr(
                    self._policy_provider,
                    "fee_schedule_fingerprint",
                ),
                execution_policy_fingerprint=getattr(
                    self._policy_provider,
                    "execution_policy_fingerprint",
                ),
            )
        if self._runtime is not None:
            payload.update(self._runtime_payload(self._state()))
        return payload

    def _runtime_payload(self, state: PaperLedgerState) -> dict[str, object]:
        strategy_run, strategy_run_failure = self._strategy_run_status()
        health = self._runtime.health()
        if (
            getattr(health, "mode", None) != "research_paper"
            or getattr(health, "read_only", None) is not True
            or getattr(health, "auto_order_enabled", None) is not False
            or getattr(health, "live_order_capability", None) is not False
        ):
            raise TypeError("paper runtime health mode is invalid")
        bar_store = getattr(health, "bar_store", None)
        if bar_store is None:
            raise TypeError("paper runtime bar-store health is unavailable")

        counter_names = (
            "bar_count",
            "observed_trading_days",
        )
        bar_values: dict[str, int] = {}
        for field_name in counter_names:
            value = getattr(bar_store, field_name, None)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError("paper runtime bar-store counter is invalid")
            bar_values[field_name] = value
        degraded = getattr(bar_store, "degraded", None)
        if not isinstance(degraded, bool):
            raise TypeError("paper runtime degraded flag is invalid")
        degraded_reason = getattr(bar_store, "degraded_reason", None)
        if degraded_reason is not None and (
            not isinstance(degraded_reason, str) or not degraded_reason
        ):
            raise TypeError("paper runtime degraded reason is invalid")
        last_bar = getattr(bar_store, "last_bar_closed_at", None)
        if last_bar is not None and not isinstance(last_bar, datetime):
            raise TypeError("paper runtime last bar timestamp is invalid")
        last_attempted = getattr(
            bar_store,
            "last_attempted_bar_closed_at",
            None,
        )
        if last_attempted is not None and not isinstance(last_attempted, datetime):
            raise TypeError("paper runtime last attempted bar timestamp is invalid")
        last_attempt_complete = getattr(
            bar_store,
            "last_attempt_complete",
            None,
        )
        if last_attempt_complete is not None and not isinstance(
            last_attempt_complete,
            bool,
        ):
            raise TypeError("paper runtime last attempt flag is invalid")
        last_attempt_failure = getattr(
            bar_store,
            "last_attempt_failure",
            None,
        )
        if last_attempt_failure is not None and (
            not isinstance(last_attempt_failure, str)
            or not last_attempt_failure
        ):
            raise TypeError("paper runtime last attempt failure is invalid")
        calendar_preflight_failure_at = getattr(
            bar_store,
            "calendar_preflight_failure_at",
            None,
        )
        if (
            calendar_preflight_failure_at is not None
            and not isinstance(calendar_preflight_failure_at, datetime)
        ):
            raise TypeError(
                "paper runtime calendar preflight timestamp is invalid"
            )
        calendar_preflight_failure = getattr(
            bar_store,
            "calendar_preflight_failure",
            None,
        )
        if calendar_preflight_failure is not None and (
            not isinstance(calendar_preflight_failure, str)
            or not calendar_preflight_failure
        ):
            raise TypeError(
                "paper runtime calendar preflight failure is invalid"
            )
        if (calendar_preflight_failure_at is None) != (
            calendar_preflight_failure is None
        ):
            raise TypeError("paper runtime calendar preflight state is invalid")

        runtime_counters: dict[str, object] = {}
        for field_name in (
            "bar_cycles",
            "bar_cycle_failures",
            "admission_cycles",
            "admission_failures",
            "admitted_event_count",
        ):
            value = getattr(health, field_name, None)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError("paper runtime counter is invalid")
            runtime_counters[field_name] = value
        last_error = getattr(health, "last_error", None)
        if last_error is not None and (
            not isinstance(last_error, str) or not last_error
        ):
            raise TypeError("paper runtime last error is invalid")
        runtime_counters["last_error"] = last_error

        coverage = getattr(health, "exit_coverage", None)
        coverage_payload: dict[str, object] | None = None
        if coverage is not None:
            coverage_bar = getattr(coverage, "bar_closed_at", None)
            if not isinstance(coverage_bar, datetime):
                raise TypeError("paper exit coverage timestamp is invalid")
            coverage_counts: dict[str, int] = {}
            for field_name in (
                "open_entry_count",
                "snapshot_count",
                "failure_count",
            ):
                value = getattr(coverage, field_name, None)
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise TypeError("paper exit coverage counter is invalid")
                coverage_counts[field_name] = value
            complete = getattr(coverage, "complete", None)
            fresh = getattr(coverage, "fresh", None)
            if not isinstance(complete, bool) or not isinstance(fresh, bool):
                raise TypeError("paper exit coverage flags are invalid")
            scan_code = getattr(coverage, "scan_code", None)
            if scan_code is not None and (
                not isinstance(scan_code, str) or not scan_code
            ):
                raise TypeError("paper exit scan code is invalid")
            cycle_failure = getattr(coverage, "cycle_failure", None)
            if cycle_failure is not None and (
                not isinstance(cycle_failure, str) or not cycle_failure
            ):
                raise TypeError("paper exit cycle failure is invalid")
            coverage_failures = getattr(coverage, "failures", None)
            if not isinstance(coverage_failures, Mapping):
                raise TypeError("paper exit coverage failures are invalid")
            coverage_failures = dict(coverage_failures)
            if any(
                not isinstance(entry_event_id, str)
                or not entry_event_id
                or not isinstance(reason, str)
                or not reason
                for entry_event_id, reason in coverage_failures.items()
            ):
                raise TypeError("paper exit coverage failures are invalid")
            if (
                len(coverage_failures) != coverage_counts["failure_count"]
                or coverage_counts["snapshot_count"]
                + coverage_counts["failure_count"]
                != coverage_counts["open_entry_count"]
            ):
                raise TypeError("paper exit coverage accounting is invalid")
            coverage_payload = {
                "bar_closed_at": coverage_bar.isoformat(),
                **coverage_counts,
                "complete": complete,
                "fresh": fresh,
                "scan_code": scan_code,
                "cycle_failure": cycle_failure,
                "failures": coverage_failures,
            }

        executable_events = len(
            {
                fill.entry_event_id
                for fill in state.fills
                if fill.side == "buy" and fill.shares > 0
            }
        )
        trading_days = bar_values["observed_trading_days"]
        reasons: list[str] = []
        if strategy_run_failure is not None:
            reasons.append(strategy_run_failure)
        if strategy_run.get("mutations_drained") is not True:
            reasons.append("strategy_run_mutations_not_drained")
        if degraded:
            reasons.append("trusted_bar_store_degraded")
        if last_error is not None:
            reasons.append("paper_runtime_unhealthy")
        if last_attempted is not None and last_attempt_complete is not True:
            reasons.append("paper_bar_cycle_incomplete")
        if calendar_preflight_failure_at is not None:
            reasons.append("paper_calendar_preflight_failed")
        if coverage_payload is None or coverage_payload["scan_code"] != "scan_complete":
            reasons.append("paper_scan_not_complete")
        if coverage_payload is None:
            reasons.append("paper_exit_coverage_unavailable")
        else:
            if coverage_payload["complete"] is not True:
                reasons.append("paper_exit_coverage_incomplete")
            if coverage_payload["fresh"] is not True:
                reasons.append("paper_exit_coverage_stale")
            if coverage_payload["failure_count"]:
                reasons.append("paper_exit_coverage_failure")
            if coverage_payload["cycle_failure"] is not None:
                reasons.append("paper_exit_cycle_failure")
        if trading_days < 20:
            reasons.append("insufficient_paper_trading_days")
        if executable_events < 30:
            reasons.append("insufficient_paper_executable_events")
        return {
            "strategy_run": strategy_run,
            "switch_capability": STRATEGY_RUN_SWITCH_CAPABILITY,
            "rolling_switch_supported": False,
            "mutation_lease_protocol": strategy_run.get(
                "mutation_lease_protocol"
            ),
            "inflight_mutation_count": strategy_run.get(
                "inflight_mutation_count"
            ),
            "mutations_drained": (
                strategy_run.get("mutations_drained") is True
            ),
            "trusted_bar_store": {
                **bar_values,
                "degraded": degraded,
                "degraded_reason": degraded_reason,
                "last_bar_closed_at": (
                    None if last_bar is None else last_bar.isoformat()
                ),
                "last_attempted_bar_closed_at": (
                    None
                    if last_attempted is None
                    else last_attempted.isoformat()
                ),
                "last_attempt_complete": last_attempt_complete,
                "last_attempt_failure": last_attempt_failure,
                "calendar_preflight_failure_at": (
                    None
                    if calendar_preflight_failure_at is None
                    else calendar_preflight_failure_at.isoformat()
                ),
                "calendar_preflight_failure": calendar_preflight_failure,
            },
            "runtime_counters": runtime_counters,
            "exit_coverage": coverage_payload,
            "paper_observation_gate": {
                "passed": not reasons,
                "run_id": strategy_run.get("run_id"),
                "epoch": strategy_run.get("epoch"),
                "strategy_run_fingerprint": strategy_run.get("fingerprint"),
                "evidence_scope": strategy_run["evidence_scope"],
                "trading_days": trading_days,
                "minimum_trading_days": 20,
                "remaining_trading_days": max(0, 20 - trading_days),
                "executable_events": executable_events,
                "minimum_executable_events": 30,
                "remaining_executable_events": max(
                    0,
                    30 - executable_events,
                ),
                "reasons": reasons,
            },
            "broker_compliance_confirmation": "pending",
            "promotion_eligible": False,
        }

    def _strategy_run_status(self) -> tuple[dict[str, object], str | None]:
        unavailable = {
            "state": "unavailable",
            "evidence_scope": "none",
            "store_bindings_complete": False,
        }
        if self._strategy_run is None:
            return unavailable, "strategy_run_unavailable"
        try:
            raw = self._strategy_run.status_payload()
        except Exception:
            return {
                "state": "invalid",
                "evidence_scope": "none",
                "store_bindings_complete": False,
            }, "strategy_run_invalid"
        if not isinstance(raw, Mapping):
            return {
                "state": "invalid",
                "evidence_scope": "none",
                "store_bindings_complete": False,
            }, "strategy_run_invalid"
        payload = dict(raw)
        run_id = payload.get("run_id")
        epoch = payload.get("epoch")
        fingerprint = payload.get("fingerprint")
        started_at = payload.get("started_at")
        identity = payload.get("identity")
        inflight_mutation_count = payload.get("inflight_mutation_count")
        mutations_drained = payload.get("mutations_drained")
        try:
            parsed_started_at = (
                datetime.fromisoformat(started_at)
                if isinstance(started_at, str)
                else None
            )
        except ValueError:
            parsed_started_at = None
        valid = (
            isinstance(run_id, str)
            and bool(run_id)
            and run_id == run_id.strip()
            and run_id.isprintable()
            and not isinstance(epoch, bool)
            and isinstance(epoch, int)
            and epoch > 0
            and isinstance(fingerprint, str)
            and re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is not None
            and payload.get("state") == "active"
            and parsed_started_at is not None
            and parsed_started_at.tzinfo is not None
            and parsed_started_at.utcoffset() is not None
            and payload.get("evidence_scope") == "current_epoch_only"
            and payload.get("store_bindings_complete") is True
            and payload.get("switch_capability")
            == STRATEGY_RUN_SWITCH_CAPABILITY
            and payload.get("rolling_switch_supported") is False
            and payload.get("mutation_lease_protocol")
            == STRATEGY_RUN_MUTATION_LEASE_PROTOCOL
            and not isinstance(inflight_mutation_count, bool)
            and isinstance(inflight_mutation_count, int)
            and inflight_mutation_count >= 0
            and isinstance(mutations_drained, bool)
            and mutations_drained is (inflight_mutation_count == 0)
            and isinstance(identity, Mapping)
            and identity.get("schema_version") == 1
        )
        if not valid:
            return {
                "state": "invalid",
                "evidence_scope": "none",
                "store_bindings_complete": False,
            }, "strategy_run_invalid"
        return payload, None

    def account(self) -> dict[str, object]:
        snapshot = self._ledger.account_snapshot()
        if not isinstance(snapshot, PaperAccountSnapshot):
            raise TypeError("ledger account snapshot is invalid")
        return _research_payload(
            valuation_basis="cost_basis_not_mark_to_market",
            initial_cash=format(snapshot.initial_cash, ".2f"),
            cash_balance=format(snapshot.cash_balance, ".2f"),
            reserved_buying_power=format(
                snapshot.reserved_buying_power,
                ".2f",
            ),
            available_buying_power=format(
                snapshot.available_buying_power,
                ".2f",
            ),
            positions_cost=format(snapshot.positions_cost, ".2f"),
            cost_basis_equity=format(snapshot.cost_basis_equity, ".2f"),
        )

    def positions(self) -> dict[str, object]:
        state = self._state()
        grouped: dict[tuple[str, str, str, str], list[PaperLot]] = {}
        for lot in state.lots:
            key = (
                lot.code,
                lot.entry_event_id,
                lot.entry_review_id,
                lot.entry_risk_snapshot_id,
            )
            grouped.setdefault(key, []).append(lot)
        items = [
            _position_payload(grouped[key])
            for key in sorted(grouped)
        ]
        return self._ledger_items(state, items)

    def intents(self) -> dict[str, object]:
        state = self._state()
        items = [
            _intent_payload(intent)
            for intent in sorted(
                state.intents,
                key=lambda item: (item.admitted_at, item.event_id),
                reverse=True,
            )
        ]
        return self._ledger_items(state, items)

    def fills(self) -> dict[str, object]:
        state = self._state()
        items = [
            _fill_payload(fill)
            for fill in sorted(
                state.fills,
                key=lambda item: (item.filled_at, item.fill_id),
                reverse=True,
            )
        ]
        return self._ledger_items(state, items)

    def exits(self) -> dict[str, object]:
        snapshots = self._exit_store.list_snapshots()
        publication_basis = "exit_store_audit"
        provisional_count = 0
        if self._runtime is not None:
            publication_basis = "exact_exit_manifest_membership"
            attest = getattr(self._runtime, "attest_exit_snapshots")
            try:
                attestations = attest(snapshots)
            except Exception:
                attestations = None
            if (
                not isinstance(attestations, tuple)
                or len(attestations) != len(snapshots)
                or any(type(value) is not bool for value in attestations)
            ):
                attestations = (False,) * len(snapshots)
            published = [
                snapshot
                for snapshot, committed in zip(snapshots, attestations)
                if committed
            ]
            provisional_count = len(snapshots) - len(published)
            snapshots = tuple(published)
        return _research_payload(
            exit_evaluation_revision=self._exit_store.revision,
            count=len(snapshots),
            provisional_count=provisional_count,
            publication_basis=publication_basis,
            items=[snapshot.to_dict() for snapshot in snapshots],
        )

    def _state(self) -> PaperLedgerState:
        state = self._ledger.load()
        if not isinstance(state, PaperLedgerState):
            raise TypeError("ledger state is invalid")
        return state

    @staticmethod
    def _ledger_items(
        state: PaperLedgerState,
        items: list[dict[str, object]],
    ) -> dict[str, object]:
        return _research_payload(
            ledger_revision=state.revision,
            count=len(items),
            items=items,
        )


__all__ = ["PaperResearchReadModel"]
