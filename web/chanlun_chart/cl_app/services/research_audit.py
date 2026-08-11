"""Strict read-only presentation model for the new causal backtest report."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

from chanlun.decision_support.trading_system.backtest.report import (
    SCHEMA,
    STRATEGY_ID,
    verify_report_hash,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    replay_decision_source_snapshot_matches_current as decision_source_snapshot_matches_current,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HIGHER_TIMEFRAME_EFFECTIVENESS_AUDIT_SCHEMA,
    higher_timeframe_effectiveness_audit,
)
from chanlun.decision_support.trading_system.higher_timeframe_execution_attribution import (
    HIGHER_TIMEFRAME_EXECUTION_ATTRIBUTION_SCHEMA,
    higher_timeframe_execution_attribution,
)
from chanlun.research_release.sector_release_manifest import (
    SectorReleaseManifestError,
    verify_sector_release_manifest,
)


_MAX_JSON_BYTES = 64 * 1024 * 1024
_ARTIFACT_DIRECTORY = Path("audit/chanlun_trading_system_backtest")
_ARTIFACT_PATH = _ARTIFACT_DIRECTORY / "certified_report.json"
_CAUSALITY_GATE_PATH = _ARTIFACT_DIRECTORY / "causality_gate.json"
_CURRENT_RESEARCH_PATH = (
    _ARTIFACT_DIRECTORY
    / "recent_year_current_sector_no3p_mwd_strength"
    / "approximate_technical_backtest_sector_mwd_strength_tactical_lifecycle.json"
)
_CURRENT_RESEARCH_SCHEMA = (
    "chanlun-sector-first-full-market-research-backtest"
)
_TACTICAL_AUDIT_SCHEMA = "chanlun-tactical-execution-audit"
_SCHEDULER_CAUSALITY_AUDIT_SCHEMA = (
    "chanlun-session-checkpoint-scheduler-audit"
)
_TERMINAL_ACCOUNTING_SCHEMA = (
    "chanlun-terminal-accounting-attribution"
)
_PURE_UNREALIZED_REASON = (
    "OPEN_CYCLE_TACTICAL_AND_CORPORATE_CASH_FLOWS_REQUIRE_"
    "A_SEPARATE_COST_BASIS_LEDGER"
)
_PROJECT_ROOT = Path(__file__).resolve().parents[4]
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_GRADES = {"certified", "research_only", "invalid"}
_CAUSAL_CONTROLS = {
    "survivorship_free_effective_dated_security_master",
    "decision_time_sw1_membership",
    "ex_date_only_causal_price_basis",
    "cash_and_share_corporate_action_accounting",
    "closed_bar_strict_structure_witnesses",
    "next_complete_minute_execution",
    "observed_range_and_volume_fill_guard",
    "delisted_security_zero_recovery",
    "content_addressed_algorithm_data_and_checkpoints",
}


class ResearchAuditUnavailable(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.details = details


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResearchAuditUnavailable("artifact_invalid_json")
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ResearchAuditUnavailable("artifact_invalid_json")


def _root(root: str | Path) -> Path:
    try:
        path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ResearchAuditUnavailable("artifact_root_unavailable") from exc
    if not path.is_dir():
        raise ResearchAuditUnavailable("artifact_root_unavailable")
    return path


def _artifact(root: Path) -> Path:
    try:
        path = (root / _ARTIFACT_PATH).resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise OSError("research audit artifact is not a file")
        size = path.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchAuditUnavailable("artifact_unavailable") from exc
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise ResearchAuditUnavailable("artifact_unavailable")
    return path


def _current_research_artifact(root: Path) -> Path | None:
    """Return the canonical current research result when it exists.

    Its presence is authoritative.  A malformed or stale current result must
    fail closed instead of silently falling back to the older certified-report
    format, otherwise the page can present a different decision core from the
    one used by screening and replay.
    """

    candidate = root / _CURRENT_RESEARCH_PATH
    try:
        if not candidate.exists():
            return None
        path = candidate.resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise OSError("current research artifact is not a file")
        size = path.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchAuditUnavailable(
            "current_research_artifact_invalid"
        ) from exc
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    return path


def _causality_gate(root: Path) -> Path | None:
    candidate = root / _CAUSALITY_GATE_PATH
    try:
        if not candidate.exists():
            return None
        path = candidate.resolve(strict=True)
        path.relative_to(root)
        if not path.is_file():
            raise OSError("causality gate is not a file")
        size = path.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise ResearchAuditUnavailable("causality_gate_invalid")
    return path


def _load(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except ResearchAuditUnavailable:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchAuditUnavailable("artifact_invalid_json") from exc
    if not isinstance(value, dict):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value, "sha256:" + hashlib.sha256(raw).hexdigest()


def _mapping(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _sequence(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _text(value: object, *, max_length: int = 1000) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_length
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return value


def _decimal(value: object, *, optional: bool = False) -> Decimal | None:
    if value is None and optional:
        return None
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    try:
        converted = Decimal(str(value))
    except InvalidOperation as exc:
        raise ResearchAuditUnavailable("artifact_invalid_schema") from exc
    if not converted.is_finite():
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return converted


def _number_text(value: object, *, optional: bool = False) -> str | None:
    converted = _decimal(value, optional=optional)
    return None if converted is None else format(converted, "f")


def _datetime_text(value: object) -> str:
    text = _text(value, max_length=64)
    try:
        converted = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ResearchAuditUnavailable("artifact_invalid_schema") from exc
    if converted.tzinfo is None or converted.utcoffset() is None:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return text


def _sha256_id(value: object) -> str:
    text = _text(value, max_length=80)
    if _HASH_RE.fullmatch(text) is None:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return text


def _date_range(value: object) -> tuple[dict[str, Any], date, date]:
    document = _mapping(value)
    start_text = _text(document.get("start"), max_length=10)
    end_text = _text(document.get("end"), max_length=10)
    try:
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except ValueError as exc:
        raise ResearchAuditUnavailable("artifact_invalid_schema") from exc
    if start > end:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return document, start, end


def _validate_algorithm_hashes(
    value: object,
) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    output: list[tuple[str, str]] = []
    for row in _sequence(value):
        document = _mapping(row)
        source = _text(document.get("source"), max_length=300)
        digest = _text(document.get("sha256"), max_length=80)
        if source in seen or _HASH_RE.fullmatch(digest) is None:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        seen.add(source)
        output.append((source, digest))
    if not output:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return tuple(output)


def _algorithm_revision(hashes: tuple[tuple[str, str], ...]) -> str:
    encoded = json.dumps(
        hashes,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _gate_details(
    payload: dict[str, Any],
    path: Path,
    root: Path,
) -> dict[str, Any]:
    try:
        schema = _text(payload.get("schema"), max_length=80)
        checked_at = _text(payload.get("checked_at"), max_length=64)
        status = _text(payload.get("status"), max_length=32)
        pnl_generated = _boolean(payload.get("pnl_generated"))
        algorithm_revision = _text(
            payload.get("algorithm_revision"), max_length=80
        )
        validated_symbol_fact_count = _integer(
            payload.get("validated_symbol_fact_count")
        )
        validated_decision_count = _integer(
            payload.get("validated_decision_count")
        )
        snapshot_hash = _text(
            payload.get("pit_snapshot_sha256"), max_length=80
        )
        controls = [
            _text(value, max_length=100)
            for value in _sequence(payload.get("proven_controls"))
        ]
        failures: list[str] = []
        seen_codes: set[str] = set()
        for row in _sequence(payload.get("failures")):
            code = _text(row, max_length=100)
            if code in seen_codes:
                raise ResearchAuditUnavailable("causality_gate_invalid")
            seen_codes.add(code)
            failures.append(code)
        report_value = payload.get("report")
        report = (
            None
            if report_value is None
            else _text(report_value, max_length=1000)
        )
    except ResearchAuditUnavailable as exc:
        if exc.code == "causality_gate_invalid":
            raise
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if (
        schema != "chanlun-backtest-causality-gate"
        or status not in {"blocked", "passed"}
        or _HASH_RE.fullmatch(algorithm_revision) is None
        or _HASH_RE.fullmatch(snapshot_hash) is None
        or set(controls) != _CAUSAL_CONTROLS
        or len(controls) != len(_CAUSAL_CONTROLS)
        or (
            status == "blocked"
            and (pnl_generated is not False or not failures or report is not None)
        )
        or (
            status == "passed"
            and (pnl_generated is not True or failures or report is None)
        )
    ):
        raise ResearchAuditUnavailable("causality_gate_invalid")
    return {
        "checked_at": checked_at,
        "status": status,
        "pnl_generated": pnl_generated,
        "algorithm_revision": algorithm_revision,
        "pit_snapshot_sha256": snapshot_hash,
        "validated_symbol_fact_count": validated_symbol_fact_count,
        "validated_decision_count": validated_decision_count,
        "proven_controls": controls,
        "failures": [
            {
                "code": code,
                "evidence": "无未来函数门禁检测到不满足项。",
                "required": "修复该项后重新执行完整因果回放。",
            }
            for code in failures
        ],
        "report": report,
        "relative_path": path.relative_to(root).as_posix(),
    }


def _validated_causality_gate(root: Path) -> dict[str, Any]:
    path = _causality_gate(root)
    if path is None:
        raise ResearchAuditUnavailable("causality_gate_unavailable")
    try:
        payload, _file_sha256 = _load(path)
        details = _gate_details(payload, path, root)
    except ResearchAuditUnavailable as exc:
        if exc.code == "causality_gate_invalid":
            raise
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if details["status"] == "blocked":
        raise ResearchAuditUnavailable(
            "causality_gate_blocked",
            details=details,
        )
    return details


def _validate_report(
    payload: dict[str, Any],
    *,
    gate: dict[str, Any],
) -> None:
    if (
        payload.get("schema") != SCHEMA
        or payload.get("strategy_id") != STRATEGY_ID
        or payload.get("active_strategy_count") != 1
        or payload.get("read_only") is not True
        or payload.get("historical") is not True
        or payload.get("no_order_execution") is not True
    ):
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    if not verify_report_hash(payload):
        raise ResearchAuditUnavailable("artifact_hash_mismatch")
    algorithm_hashes = _validate_algorithm_hashes(
        payload.get("algorithm_hashes")
    )
    if _algorithm_revision(algorithm_hashes) != gate["algorithm_revision"]:
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    _text(payload.get("generated_at"), max_length=64)
    if payload.get("evaluation_mode") != "fixed_policy_one_year":
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    _requested, requested_start, requested_end = _date_range(
        payload.get("requested_range")
    )
    _effective, effective_start, effective_end = _date_range(
        payload.get("effective_range")
    )
    if not (
        requested_start <= effective_start <= effective_end == requested_end
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    universe = _mapping(payload.get("universe"))
    if (
        universe.get("catalog_source")
        != "qmt_sw1_with_cninfo_effective_dates"
    ):
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    eligible_sectors = _integer(universe.get("eligible_sector_count"))
    selected_symbols = _integer(universe.get("selected_symbol_count"))
    archived_symbols = _integer(
        universe.get("archived_intersecting_symbol_count")
    )
    unclassified_symbols = _integer(
        universe.get("unclassified_excluded_symbol_count")
    )
    corporate_actions = _integer(universe.get("corporate_action_count"))
    evaluation_count = _integer(universe.get("causal_evaluation_count"))
    if (
        eligible_sectors <= 0
        or universe.get("sector_composite_member_limit") is not None
        or selected_symbols <= 0
        or archived_symbols != selected_symbols + unclassified_symbols
        or corporate_actions < 0
        or gate["validated_symbol_fact_count"] != selected_symbols
        or gate["validated_decision_count"] != evaluation_count
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    source_hashes = _mapping(payload.get("data_source_hashes"))
    required_source_hashes = {
        "pit_metadata_snapshot",
        "qmt_extract_manifest",
        "prefix_invariance_audit",
        "symbol_fact_checkpoint_tree",
        "sector_fact_checkpoint_tree",
        "certified_portfolio_run",
    }
    if set(source_hashes) != required_source_hashes:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    for digest in source_hashes.values():
        if _HASH_RE.fullmatch(_text(digest, max_length=80)) is None:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
    if source_hashes["pit_metadata_snapshot"] != gate["pit_snapshot_sha256"]:
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    evidence = _mapping(payload.get("data_evidence"))
    grade = _text(evidence.get("grade"), max_length=32)
    if grade not in _EVIDENCE_GRADES:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    _sequence(evidence.get("failures"))
    _sequence(evidence.get("warnings"))
    coverage = _mapping(evidence.get("coverage"))
    for value in coverage.values():
        _decimal(value)

    contract = _mapping(payload.get("execution_contract"))
    expected_contract = {
        "context_frequency": "30m",
        "setup_frequency": "5m",
        "trigger_frequency": "1m",
        "point_classes_analyzed_independently": True,
        "sector_price_source": "qmt-sw1-pit-composite",
        "sector_price_change_gate": False,
        "next_tradable_minute_fill": True,
        "entry_risk_ttl_seconds": 300,
        "entry_liquidity_resize": "one_shot_to_10pct_minute_volume",
        "exit_liquidity_execution": (
            "partial_up_to_10pct_minute_volume_until_complete"
        ),
        "t_plus_one": True,
        "intraday_structural_stop": True,
    }
    if any(contract.get(key) != value for key, value in expected_contract.items()):
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    first_center_only = _boolean(contract.get("first_center_three_buy_only"))
    first_center_mode = _text(
        contract.get("first_center_three_buy_mode"),
        max_length=32,
    )
    first_center_values = _sequence(
        contract.get("first_center_three_buy_selected_values")
    )
    if (
        first_center_mode not in {"policy_default", "walk_forward_selected"}
        or any(not isinstance(value, bool) for value in first_center_values)
        or len(first_center_values) != len(set(first_center_values))
    ):
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    if first_center_mode == "policy_default":
        valid_first_center_contract = first_center_only and not first_center_values
    else:
        valid_first_center_contract = bool(first_center_values) and (
            first_center_only is all(first_center_values)
        )
    if not valid_first_center_contract:
        raise ResearchAuditUnavailable("strategy_contract_invalid")

    metrics = _mapping(payload.get("aggregate_out_of_sample"))
    net_return = _decimal(metrics.get("net_return"))
    max_drawdown = _decimal(metrics.get("max_drawdown"))
    calmar = _decimal(metrics.get("calmar"), optional=True)
    _decimal(metrics.get("annualized_return"), optional=True)
    _decimal(metrics.get("sharpe"), optional=True)
    _decimal(metrics.get("sortino"), optional=True)
    _integer(metrics.get("max_drawdown_duration_bars"))
    _sequence(metrics.get("warnings"))

    adequacy = _mapping(payload.get("sample_adequacy"))
    sample_passed = _boolean(adequacy.get("passed"))
    _integer(adequacy.get("closed_trade_count"))
    _mapping(adequacy.get("point_counts"))
    _sequence(adequacy.get("failures"))

    concentration = _mapping(payload.get("concentration"))
    concentration_passed = _boolean(concentration.get("passed"))
    _decimal(concentration.get("max_symbol_trade_fraction"))
    _decimal(concentration.get("max_sector_trade_fraction"))
    _decimal(concentration.get("limit"))

    verdict = _mapping(payload.get("verdict"))
    live_ready = _boolean(verdict.get("live_ready"))
    status = _text(verdict.get("status"), max_length=64)
    failed_conditions = _sequence(verdict.get("failed_conditions"))
    if len(failed_conditions) != len(set(failed_conditions)):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    if live_ready is not (status == "live_ready" and not failed_conditions):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    if live_ready and not (
        grade == "certified"
        and sample_passed
        and net_return is not None
        and net_return > 0
        and max_drawdown is not None
        and max_drawdown <= Decimal("0.10")
        and calmar is not None
        and calmar >= 1
        and concentration_passed
    ):
        raise ResearchAuditUnavailable("verdict_contract_invalid")
    for key in (
        "walk_forward_windows",
        "ablations",
        "benchmarks",
        "limitations",
    ):
        _sequence(payload.get(key))
    _mapping(payload.get("point_type_metrics"))
    _mapping(payload.get("sector_year_liquidity_metrics"))
    _mapping(payload.get("parameter_robustness"))
    bootstrap = payload.get("bootstrap_intervals")
    if bootstrap is not None:
        _mapping(bootstrap)


def _current_curve_metrics(value: object) -> dict[str, object]:
    document = _mapping(value)
    status = _text(document.get("status"), max_length=64)
    if status not in {"EVALUATED", "EVALUATED_SAMPLE_INSUFFICIENT"}:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    start = date.fromisoformat(_text(document.get("start"), max_length=10))
    end = date.fromisoformat(_text(document.get("end"), max_length=10))
    observations = _integer(document.get("observations"))
    if start > end or observations <= 0:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    output: dict[str, object] = {
        "status": status,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "observations": observations,
        "net_return": _number_text(document.get("net_return")),
        "annualized_return": _number_text(
            document.get("annualized_return"), optional=True
        ),
        "max_drawdown": _number_text(document.get("max_drawdown")),
        "sharpe": _number_text(document.get("sharpe"), optional=True),
    }
    reason = document.get("adjudication_reason")
    if reason is not None:
        output["adjudication_reason"] = _text(reason, max_length=160)
    return output


def _current_replay_metrics(
    value: object,
    *,
    require_evaluable: bool = True,
) -> dict[str, object]:
    document = _mapping(value)
    output: dict[str, object] = {
        key: _integer(document.get(key))
        for key in (
            "fill_count",
            "open_cycle_count",
            "order_count",
            "rejection_count",
            "strategic_cycle_count",
            "tactical_cycle_count",
        )
    }
    output.update(
        {
            key: _number_text(document.get(key), optional=key in {
                "annualized_return",
                "payoff_ratio",
                "profit_factor",
                "sharpe",
                "win_rate",
            })
            for key in (
                "annualized_return",
                "max_drawdown",
                "net_return",
                "payoff_ratio",
                "profit_factor",
                "sharpe",
                "total_fees",
                "turnover",
                "win_rate",
            )
        }
    )
    output.update(
        {
            key: _boolean(document.get(key))
            for key in (
                "empty_replay",
                "ledger_valid",
                "performance_evaluable",
                "strategic_sample_insufficient",
                "tactical_sample_insufficient",
            )
        }
    )
    output["warnings"] = [
        _text(item, max_length=160)
        for item in _sequence(document.get("warnings"))
    ]
    if output["ledger_valid"] is not True:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    if require_evaluable and (
        output["empty_replay"] is True
        or output["performance_evaluable"] is not True
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    if output["empty_replay"] is True and output[
        "performance_evaluable"
    ] is True:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return output


def _current_tactical_audit(
    value: object,
    *,
    replay_metrics: dict[str, object],
) -> dict[str, object]:
    document = _mapping(value)
    if document.get("schema") != _TACTICAL_AUDIT_SCHEMA:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    signals: list[dict[str, object]] = []
    for raw in _sequence(document.get("signals")):
        row = _mapping(raw)
        persistent_value = row.get("persistent_intent_id")
        persistent_id = (
            None
            if persistent_value is None
            else _text(persistent_value, max_length=500)
        )
        if persistent_id is not None and not persistent_id.startswith(
            "persistent:"
        ):
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        actions = [
            _text(item, max_length=64)
            for item in _sequence(row.get("decision_actions"))
        ]
        reasons = [
            _text(item, max_length=160)
            for item in _sequence(row.get("reason_codes"))
        ]
        if len(actions) != len(set(actions)) or len(reasons) != len(set(reasons)):
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        signal = {
            "symbol": _text(row.get("symbol"), max_length=32),
            "kind": _text(row.get("kind"), max_length=64),
            "observed_at": _datetime_text(row.get("observed_at")),
            "disposition": _text(row.get("disposition"), max_length=120),
            "decision_actions": actions,
            "reason_codes": reasons,
            "decision_record_count": _integer(
                row.get("decision_record_count")
            ),
            "order_count": _integer(row.get("order_count")),
            "fill_count": _integer(row.get("fill_count")),
            "suppressed_retry_count": _integer(
                row.get("suppressed_retry_count")
            ),
            "persistent_intent_id": persistent_id,
            "signal_identity": _sha256_id(row.get("signal_identity")),
            "structure_snapshot_id": _sha256_id(
                row.get("structure_snapshot_id")
            ),
        }
        signals.append(signal)

    generated = _integer(document.get("generated_signal_count"))
    dispatched = _integer(document.get("dispatched_source_signal_count"))
    decisions = _integer(document.get("decision_record_count"))
    orders = _integer(document.get("order_count"))
    fills = _integer(document.get("fill_count"))
    suppressed = _integer(document.get("suppressed_retry_count"))
    completed = _integer(document.get("completed_tactical_cycle_count"))
    dispositions = {
        _text(key, max_length=120): _integer(count)
        for key, count in _mapping(document.get("disposition_counts")).items()
    }
    actual_dispositions = Counter(str(row["disposition"]) for row in signals)
    if (
        generated != len(signals)
        or dispatched
        != sum(int(row["decision_record_count"]) > 0 for row in signals)
        or decisions
        != sum(int(row["decision_record_count"]) for row in signals)
        or orders != sum(int(row["order_count"]) for row in signals)
        or fills != sum(int(row["fill_count"]) for row in signals)
        or suppressed
        != sum(int(row["suppressed_retry_count"]) for row in signals)
        or dispositions != dict(actual_dispositions)
        or completed != replay_metrics["tactical_cycle_count"]
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return {
        "schema": _TACTICAL_AUDIT_SCHEMA,
        "adjudication": _text(document.get("adjudication"), max_length=180),
        "generated_signal_count": generated,
        "dispatched_source_signal_count": dispatched,
        "decision_record_count": decisions,
        "order_count": orders,
        "fill_count": fills,
        "suppressed_retry_count": suppressed,
        "completed_tactical_cycle_count": completed,
        "disposition_counts": dispositions,
        "signals": signals,
    }


def _current_scheduler_causality_audit(
    value: object,
    *,
    lifecycle: Mapping[str, object],
) -> dict[str, object]:
    """Validate the historical scheduler against daily forward semantics."""

    document = _mapping(value)
    if (
        document.get("schema") != _SCHEDULER_CAUSALITY_AUDIT_SCHEMA
        or document.get("mode")
        != "HISTORICAL_SESSION_CHECKPOINT_FIXED_POINT"
        or document.get("retirement_boundary")
        != "RESOLUTION_SESSION_END_EFFECTIVE_NEXT_SESSION"
        or document.get("live_status") != "LIVE_DISABLED"
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    converged = _boolean(document.get("converged"))
    forward_equivalent = _boolean(
        document.get("forward_checkpoint_equivalent")
    )
    parameters_changed = _boolean(document.get("parameters_changed"))
    build_passes = _integer(document.get("build_pass_count"))
    replay_passes = _integer(document.get("replay_pass_count"))
    maximum_build_passes = _integer(document.get("maximum_build_passes"))
    source_signal_count = _integer(document.get("source_signal_count"))
    initial_events = _integer(document.get("initial_event_count"))
    final_events = _integer(document.get("final_event_count"))
    newly_exposed = _integer(document.get("newly_exposed_event_count"))
    removed_stale = _integer(document.get("removed_stale_retry_event_count"))
    resolved_count = _integer(
        document.get("resolved_persistent_signal_count")
    )
    schedule_sha256 = _sha256_id(document.get("event_schedule_sha256"))
    if (
        converged is not True
        or forward_equivalent is not True
        or parameters_changed is not False
        or build_passes < 2
        or replay_passes != build_passes - 1
        or maximum_build_passes < build_passes
        or source_signal_count < resolved_count
        or min(
            initial_events,
            final_events,
            newly_exposed,
            removed_stale,
            resolved_count,
        )
        < 0
        or initial_events + newly_exposed - removed_stale != final_events
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    resolution_rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in _sequence(document.get("resolution_sessions")):
        row = _mapping(raw)
        identity = _text(row.get("persistent_intent_id"), max_length=500)
        if identity in seen or not identity.startswith("persistent:"):
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        seen.add(identity)
        try:
            resolved_on = date.fromisoformat(
                _text(row.get("resolved_on"), max_length=10)
            )
            effective_after = date.fromisoformat(
                _text(row.get("retirement_effective_after"), max_length=10)
            )
        except ValueError as exc:
            raise ResearchAuditUnavailable(
                "current_research_artifact_invalid"
            ) from exc
        if resolved_on != effective_after:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        resolution_rows.append(
            {
                "persistent_intent_id": identity,
                "resolved_on": resolved_on.isoformat(),
                "retirement_effective_after": effective_after.isoformat(),
            }
        )

    lifecycle_ids = set(
        _sequence(lifecycle.get("resolved_persistent_intent_ids"))
    )
    if (
        resolved_count != len(resolution_rows)
        or seen != lifecycle_ids
        or resolved_count
        != _integer(lifecycle.get("resolved_persistent_intent_count"))
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    return {
        "schema": _SCHEDULER_CAUSALITY_AUDIT_SCHEMA,
        "mode": "HISTORICAL_SESSION_CHECKPOINT_FIXED_POINT",
        "converged": True,
        "build_pass_count": build_passes,
        "replay_pass_count": replay_passes,
        "maximum_build_passes": maximum_build_passes,
        "source_signal_count": source_signal_count,
        "initial_event_count": initial_events,
        "final_event_count": final_events,
        "newly_exposed_event_count": newly_exposed,
        "removed_stale_retry_event_count": removed_stale,
        "resolved_persistent_signal_count": resolved_count,
        "resolution_sessions": resolution_rows,
        "event_schedule_sha256": schedule_sha256,
        "retirement_boundary": (
            "RESOLUTION_SESSION_END_EFFECTIVE_NEXT_SESSION"
        ),
        "forward_checkpoint_equivalent": True,
        "parameters_changed": False,
        "live_status": "LIVE_DISABLED",
    }


def _current_lifecycle(
    replay: dict[str, Any],
    *,
    replay_metrics: dict[str, object],
) -> dict[str, object]:
    intents = _sequence(replay.get("intents"))
    resolved = [
        _text(item, max_length=500)
        for item in _sequence(replay.get("resolved_persistent_intent_ids"))
    ]
    if len(resolved) != len(set(resolved)) or any(
        not item.startswith("persistent:") for item in resolved
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    suppression_rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in _sequence(replay.get("suppressed_persistent_event_counts")):
        pair = _sequence(raw)
        if len(pair) != 2:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        persistent_id = _text(pair[0], max_length=500)
        count = _integer(pair[1])
        if (
            persistent_id in seen
            or persistent_id not in resolved
            or count <= 0
        ):
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        seen.add(persistent_id)
        parts = persistent_id.split(":", 4)
        suppression_rows.append(
            {
                "persistent_intent_id": persistent_id,
                "symbol": parts[1] if len(parts) > 2 else "UNRESOLVED",
                "kind": parts[2] if len(parts) > 3 else "UNRESOLVED",
                "suppressed_retry_count": count,
            }
        )
    orders = _sequence(replay.get("orders"))
    rejections = _sequence(replay.get("rejections"))
    closed_cycles = _sequence(replay.get("closed_cycles"))
    positions = _sequence(replay.get("positions"))
    fill_count = sum(
        len(_sequence(_mapping(_mapping(order).get("match")).get("fills")))
        for order in orders
    )
    if (
        len(orders) != replay_metrics["order_count"]
        or fill_count != replay_metrics["fill_count"]
        or len(rejections) != replay_metrics["rejection_count"]
        or len(closed_cycles) != replay_metrics["strategic_cycle_count"]
        or len(positions) != replay_metrics["open_cycle_count"]
    ):
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return {
        "intent_count": len(intents),
        "resolved_persistent_intent_count": len(resolved),
        "resolved_persistent_intent_ids": resolved,
        "suppressed_persistent_intent_count": len(suppression_rows),
        "suppressed_retry_count": sum(
            int(row["suppressed_retry_count"]) for row in suppression_rows
        ),
        "suppressions": suppression_rows,
    }


def _current_causal_ablations(
    replay_values: object,
    scheduler_values: object,
) -> dict[str, dict[str, object]]:
    """Validate that each historical ablation owns an independent schedule."""

    replays = _mapping(replay_values)
    schedules = _mapping(scheduler_values)
    expected_names = {
        "NO_TACTICAL",
        "EXACT_GREEN_HIGHER_TIMEFRAME_ONLY",
    }
    if set(replays) != expected_names or set(schedules) != expected_names:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    output: dict[str, dict[str, object]] = {}
    for name in sorted(expected_names):
        replay = _mapping(replays.get(name))
        if (
            replay.get("result_status") != "RESEARCH_ONLY"
            or replay.get("live_status") != "LIVE_DISABLED"
        ):
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        metrics = _current_replay_metrics(
            replay.get("metrics"),
            require_evaluable=False,
        )
        lifecycle = _current_lifecycle(replay, replay_metrics=metrics)
        scheduler = _current_scheduler_causality_audit(
            schedules.get(name),
            lifecycle=lifecycle,
        )
        if name == "NO_TACTICAL" and metrics["tactical_cycle_count"] != 0:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        output[name] = {
            "metrics": metrics,
            "lifecycle": lifecycle,
            "scheduler_causality_audit": scheduler,
        }
    return output


def _current_higher_timeframe_effectiveness(
    value: object,
    *,
    candidate_audit: object,
) -> dict[str, object]:
    """Recompute the M/W/D effectiveness audit from raw candidate evidence."""

    document = _mapping(value)
    if (
        document.get("schema")
        != HIGHER_TIMEFRAME_EFFECTIVENESS_AUDIT_SCHEMA
        or not _HASH_RE.fullmatch(str(document.get("audit_sha256") or ""))
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    try:
        rows = tuple(_mapping(raw) for raw in _sequence(candidate_audit))
        expected = higher_timeframe_effectiveness_audit(rows)
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchAuditUnavailable(
            "current_research_artifact_invalid"
        ) from exc
    if document != expected:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    return expected


def _current_higher_timeframe_execution_attribution(
    value: object,
    *,
    candidate_audit: object,
    replay: Mapping[str, object],
    terminal_accounting: Mapping[str, object],
) -> dict[str, object]:
    """Recompute the candidate -> entry -> terminal-cycle evidence chain."""

    document = _mapping(value)
    if (
        document.get("schema")
        != HIGHER_TIMEFRAME_EXECUTION_ATTRIBUTION_SCHEMA
        or not _HASH_RE.fullmatch(str(document.get("audit_sha256") or ""))
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    try:
        rows = tuple(_mapping(raw) for raw in _sequence(candidate_audit))
        expected = higher_timeframe_execution_attribution(
            rows,
            replay,
            terminal_accounting,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ResearchAuditUnavailable(
            "current_research_artifact_invalid"
        ) from exc
    if document != expected:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    return expected


def _apply_sector_chart_archive_overlay(
    audit: dict[str, object],
    *,
    root: Path,
    artifact_file_sha256: str,
    artifact_content_sha256: str,
    decision_source_sha256: str,
    input_hashes: Mapping[str, object],
) -> dict[str, object]:
    """Add presentation-only chart support without changing risk evidence.

    ``audit_sha256`` continues to identify the exact recomputed M/W/D audit.
    The optional QMT composite archive has its own manifest identity and may
    only turn a sector point into a link after its exact cutoff/interval file
    is present and hash-valid.
    """

    from .sector_chart_archive import (
        SectorChartArchiveUnavailable,
        load_sector_chart_archive,
        sector_chart_entry,
    )

    subjects = _mapping(audit.get("subjects"))
    point_audit_keys = (
        "globally_deduplicated_point_audit",
        "globally_deduplicated_diagnostic_buy_point_audit",
        "warmup_non_monotonic_point_audit",
        "warmup_mapping_supply_point_audit",
    )
    for raw_subject in subjects.values():
        subject = _mapping(raw_subject)
        for audit_key in point_audit_keys:
            point_audit = _mapping(subject.get(audit_key))
            point_audit["artifact_chart_focus_supported_point_count"] = _integer(
                point_audit.get("chart_focus_supported_point_count")
            )
            for raw_point in _sequence(point_audit.get("points")):
                point = _mapping(raw_point)
                if point.get("chart_focus_supported") is True:
                    point["chart_source_kind"] = "A_SHARE_CAUSAL_PREFIX"

    try:
        archive = load_sector_chart_archive(
            root,
            expected_source_artifact_file_sha256=artifact_file_sha256,
            expected_source_artifact_content_sha256=artifact_content_sha256,
            expected_risk_audit_sha256=_sha256_id(audit.get("audit_sha256")),
            expected_decision_source_sha256=decision_source_sha256,
            expected_input_hashes=input_hashes,
        )
    except SectorChartArchiveUnavailable as exc:
        summary: dict[str, object] = {
            "schema": "chanlun-sector-chart-evidence-archive",
            "status": "UNAVAILABLE",
            "reason_code": type(exc).__name__,
            "detail": str(exc),
            "presentation_overlay_not_in_risk_audit_hash": True,
        }
    else:
        summary = archive.summary()
        verified_files: set[tuple[str, str]] = set()
        supported_sector_points = 0
        unavailable_sector_points = 0
        sector_subject = _mapping(subjects.get("sector"))
        for audit_key in point_audit_keys:
            point_audit = _mapping(sector_subject.get(audit_key))
            for raw_point in _sequence(point_audit.get("points")):
                point = _mapping(raw_point)
                symbol = str(point.get("source_symbol") or "")
                interval = str(point.get("chart_interval") or "")
                cutoff = point.get("review_as_of_unix")
                if not symbol.startswith("qmt-gics3:") or type(cutoff) is not int:
                    unavailable_sector_points += 1
                    continue
                key = (symbol, cutoff)
                try:
                    entry = sector_chart_entry(
                        archive,
                        sector_id=symbol,
                        review_as_of=cutoff,
                        interval=interval,
                        verify_file=(key[0], interval) not in verified_files,
                    )
                except SectorChartArchiveUnavailable:
                    point["chart_focus_supported"] = False
                    point["chart_source_kind"] = "UNAVAILABLE"
                    unavailable_sector_points += 1
                    continue
                verified_files.add((key[0], interval))
                point["chart_focus_supported"] = True
                point["chart_source_kind"] = "VERIFIED_QMT_SECTOR_ARCHIVE"
                point["sector_chart_archive_entry_id"] = entry["entry_id"]
                point["sector_chart_source_revision"] = entry["source_revision"]
                point["sector_chart_price_basis_revision"] = entry[
                    "price_basis_revision"
                ]
                supported_sector_points += 1
        summary.update(
            supported_sector_point_count=supported_sector_points,
            unavailable_sector_point_count=unavailable_sector_points,
            verified_frame_binding_count=len(verified_files),
        )

    for raw_subject in subjects.values():
        subject = _mapping(raw_subject)
        for audit_key in point_audit_keys:
            point_audit = _mapping(subject.get(audit_key))
            points = [
                _mapping(value) for value in _sequence(point_audit.get("points"))
            ]
            supported = sum(
                value.get("chart_focus_supported") is True for value in points
            )
            point_audit["chart_focus_supported_point_count"] = supported
            point_audit["chart_focus_unavailable_point_count"] = (
                len(points) - supported
            )
            point_audit["effective_chart_focus_supported_point_count"] = supported
    audit["chart_presentation_overlay"] = summary
    return summary


def _current_terminal_accounting(
    value: object,
    *,
    replay: dict[str, Any],
    replay_metrics: dict[str, object],
) -> dict[str, object]:
    """Validate and present the exact terminal account decomposition.

    This parser intentionally recomputes every material identity from the
    event-sourced replay.  A valid content hash alone is not sufficient: an
    internally inconsistent or manually rewritten attribution must fail
    closed before the page can display it.
    """

    document = _mapping(value)
    reason_codes = [
        _text(item, max_length=160)
        for item in _sequence(document.get("reason_codes"))
    ]
    if (
        document.get("schema") != _TERMINAL_ACCOUNTING_SCHEMA
        or document.get("status") != "EVALUATED"
        or reason_codes
        or document.get("sector_membership_mode")
        != "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    terminal_raw = _mapping(document.get("terminal"))
    terminal = {
        "observed_at": _datetime_text(terminal_raw.get("observed_at")),
        **{
            key: _decimal(terminal_raw.get(key))
            for key in (
                "initial_cash",
                "final_cash",
                "cash",
                "market_value",
                "equity",
                "total_net_pnl",
            )
        },
    }
    if (
        terminal["initial_cash"] <= 0
        or terminal["final_cash"] < 0
        or terminal["cash"] < 0
        or terminal["market_value"] < 0
        or terminal["equity"] <= 0
        or terminal["cash"] != terminal["final_cash"]
        or terminal["cash"] + terminal["market_value"]
        != terminal["equity"]
        or terminal["equity"] - terminal["initial_cash"]
        != terminal["total_net_pnl"]
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    replay_initial_cash = _decimal(replay.get("initial_cash"))
    replay_final_cash = _decimal(replay.get("final_cash"))
    equity_curve = _sequence(replay.get("equity_curve"))
    if not equity_curve:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    last_equity = _mapping(equity_curve[-1])
    if (
        terminal["initial_cash"] != replay_initial_cash
        or terminal["final_cash"] != replay_final_cash
        or terminal["observed_at"]
        != _datetime_text(last_equity.get("observed_at"))
        or terminal["cash"] != _decimal(last_equity.get("cash"))
        or terminal["market_value"]
        != _decimal(last_equity.get("market_value"))
        or terminal["equity"] != _decimal(last_equity.get("equity"))
        or _boolean(last_equity.get("complete")) is not True
        or _sequence(last_equity.get("reason_codes"))
        or terminal["total_net_pnl"] / terminal["initial_cash"]
        != _decimal(replay_metrics["net_return"])
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    decomposition_raw = _mapping(document.get("pnl_decomposition"))
    closed_realized = _decimal(
        decomposition_raw.get("closed_cycle_realized_net_pnl")
    )
    open_marked = _decimal(
        decomposition_raw.get("open_cycle_marked_net_pnl")
    )
    if (
        decomposition_raw.get("pure_unrealized_net_pnl") is not None
        or decomposition_raw.get("pure_unrealized_reason")
        != _PURE_UNREALIZED_REASON
        or _decimal(decomposition_raw.get("identity_difference")) != 0
        or closed_realized + open_marked != terminal["total_net_pnl"]
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    accounting_identity = _mapping(document.get("accounting_identity"))
    if any(
        _decimal(accounting_identity.get(key)) != 0
        for key in (
            "cash_market_equity_difference",
            "terminal_cash_difference",
            "position_market_value_difference",
        )
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    replay_closed: dict[str, dict[str, Any]] = {}
    for raw in _sequence(replay.get("closed_cycles")):
        cycle = _mapping(raw)
        cycle_id = _text(cycle.get("cycle_id"), max_length=300)
        if cycle_id in replay_closed:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        replay_closed[cycle_id] = cycle

    closed_rows: list[dict[str, object]] = []
    seen_closed: set[str] = set()
    for raw in _sequence(document.get("closed_cycles")):
        row = _mapping(raw)
        cycle_id = _text(row.get("cycle_id"), max_length=300)
        replay_row = replay_closed.get(cycle_id)
        if cycle_id in seen_closed or replay_row is None:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        seen_closed.add(cycle_id)
        parsed = {
            "symbol": _text(row.get("symbol"), max_length=32),
            "sector_id": _text(row.get("sector_id"), max_length=160),
            "sector_name": _text(row.get("sector_name"), max_length=160),
            "cycle_id": cycle_id,
            "slot_number": _integer(row.get("slot_number")),
            "opened_at": _datetime_text(row.get("opened_at")),
            "closed_at": _datetime_text(row.get("closed_at")),
            "entry_cash": _decimal(row.get("entry_cash")),
            "realized_net_pnl": _decimal(row.get("realized_net_pnl")),
        }
        if (
            parsed["slot_number"] <= 0
            or parsed["entry_cash"] <= 0
            or parsed["symbol"] != replay_row.get("symbol")
            or parsed["slot_number"] != _integer(replay_row.get("slot_number"))
            or parsed["opened_at"]
            != _datetime_text(replay_row.get("opened_at"))
            or parsed["closed_at"]
            != _datetime_text(replay_row.get("closed_at"))
            or parsed["entry_cash"] != _decimal(replay_row.get("entry_cash"))
            or parsed["realized_net_pnl"]
            != _decimal(replay_row.get("net_pnl"))
        ):
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        closed_rows.append(parsed)
    if (
        seen_closed != set(replay_closed)
        or len(closed_rows) != replay_metrics["strategic_cycle_count"]
        or sum(
            (Decimal(row["realized_net_pnl"]) for row in closed_rows),
            Decimal("0"),
        )
        != closed_realized
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    replay_positions: dict[str, dict[str, Any]] = {}
    for raw in _sequence(replay.get("positions")):
        position = _mapping(raw)
        cycle_id = _text(position.get("cycle_id"), max_length=300)
        if cycle_id in replay_positions:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        replay_positions[cycle_id] = position

    open_rows: list[dict[str, object]] = []
    seen_open: set[str] = set()
    for raw in _sequence(document.get("open_positions")):
        row = _mapping(raw)
        cycle_id = _text(row.get("cycle_id"), max_length=300)
        replay_row = replay_positions.get(cycle_id)
        if cycle_id in seen_open or replay_row is None:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        seen_open.add(cycle_id)
        parsed = {
            "symbol": _text(row.get("symbol"), max_length=32),
            "sector_id": _text(row.get("sector_id"), max_length=160),
            "sector_name": _text(row.get("sector_name"), max_length=160),
            "cycle_id": cycle_id,
            "slot_number": _integer(row.get("slot_number")),
            "opened_at": _datetime_text(row.get("opened_at")),
            "quantity": _integer(row.get("quantity")),
            "entry_cash": _decimal(row.get("entry_cash")),
            "cumulative_cash_flow": _decimal(row.get("cumulative_cash_flow")),
            "cumulative_fees": _decimal(row.get("cumulative_fees")),
            "turnover_notional": _decimal(row.get("turnover_notional")),
            "tactical_cycles_completed": _integer(
                row.get("tactical_cycles_completed")
            ),
            "last_price": _decimal(row.get("last_price")),
            "market_value": _decimal(row.get("market_value")),
            "marked_at": _datetime_text(row.get("marked_at")),
            "marked_net_pnl": _decimal(row.get("marked_net_pnl")),
            "account_equity_fraction": _decimal(
                row.get("account_equity_fraction")
            ),
            "invested_market_value_fraction": _decimal(
                row.get("invested_market_value_fraction")
            ),
        }
        if (
            parsed["slot_number"] <= 0
            or parsed["quantity"] <= 0
            or parsed["entry_cash"] <= 0
            or parsed["cumulative_fees"] < 0
            or parsed["turnover_notional"] < 0
            or parsed["last_price"] < 0
            or parsed["market_value"] < 0
            or not 0 <= parsed["account_equity_fraction"] <= 1
            or not 0 <= parsed["invested_market_value_fraction"] <= 1
            or parsed["marked_net_pnl"]
            != parsed["cumulative_cash_flow"] + parsed["market_value"]
            or parsed["account_equity_fraction"]
            != parsed["market_value"] / terminal["equity"]
            or parsed["invested_market_value_fraction"]
            != (
                Decimal("0")
                if terminal["market_value"] == 0
                else parsed["market_value"] / terminal["market_value"]
            )
            or parsed["symbol"] != replay_row.get("symbol")
            or parsed["slot_number"] != _integer(replay_row.get("slot_number"))
            or parsed["opened_at"]
            != _datetime_text(replay_row.get("opened_at"))
            or parsed["quantity"] != _integer(replay_row.get("quantity"))
            or parsed["entry_cash"] != _decimal(replay_row.get("entry_cash"))
            or parsed["cumulative_cash_flow"]
            != _decimal(replay_row.get("cumulative_cash_flow"))
            or parsed["cumulative_fees"]
            != _decimal(replay_row.get("cumulative_fees"))
            or parsed["turnover_notional"]
            != _decimal(replay_row.get("turnover_notional"))
            or parsed["tactical_cycles_completed"]
            != _integer(replay_row.get("tactical_cycles_completed"))
            or parsed["last_price"] != _decimal(replay_row.get("last_price"))
            or parsed["market_value"]
            != _decimal(replay_row.get("market_value"))
            or parsed["marked_at"] != _datetime_text(replay_row.get("marked_at"))
            or _boolean(replay_row.get("mark_complete")) is not True
        ):
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        open_rows.append(parsed)
    if (
        seen_open != set(replay_positions)
        or len(open_rows) != replay_metrics["open_cycle_count"]
        or sum(
            (Decimal(row["market_value"]) for row in open_rows),
            Decimal("0"),
        )
        != terminal["market_value"]
        or sum(
            (Decimal(row["marked_net_pnl"]) for row in open_rows),
            Decimal("0"),
        )
        != open_marked
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    expected_sectors: dict[str, dict[str, object]] = {}
    for row in closed_rows:
        sector = expected_sectors.setdefault(
            str(row["sector_id"]),
            {
                "sector_name": row["sector_name"],
                "closed_cycle_count": 0,
                "closed_cycle_realized_net_pnl": Decimal("0"),
                "open_position_count": 0,
                "open_market_value": Decimal("0"),
                "open_cycle_marked_net_pnl": Decimal("0"),
            },
        )
        if sector["sector_name"] != row["sector_name"]:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        sector["closed_cycle_count"] = int(sector["closed_cycle_count"]) + 1
        sector["closed_cycle_realized_net_pnl"] = Decimal(
            sector["closed_cycle_realized_net_pnl"]
        ) + Decimal(row["realized_net_pnl"])
    for row in open_rows:
        sector = expected_sectors.setdefault(
            str(row["sector_id"]),
            {
                "sector_name": row["sector_name"],
                "closed_cycle_count": 0,
                "closed_cycle_realized_net_pnl": Decimal("0"),
                "open_position_count": 0,
                "open_market_value": Decimal("0"),
                "open_cycle_marked_net_pnl": Decimal("0"),
            },
        )
        if sector["sector_name"] != row["sector_name"]:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        sector["open_position_count"] = int(sector["open_position_count"]) + 1
        sector["open_market_value"] = Decimal(sector["open_market_value"]) + Decimal(
            row["market_value"]
        )
        sector["open_cycle_marked_net_pnl"] = Decimal(
            sector["open_cycle_marked_net_pnl"]
        ) + Decimal(row["marked_net_pnl"])

    sector_rows: list[dict[str, object]] = []
    seen_sectors: set[str] = set()
    for raw in _sequence(document.get("sector_attribution")):
        row = _mapping(raw)
        sector_id = _text(row.get("sector_id"), max_length=160)
        expected = expected_sectors.get(sector_id)
        if sector_id in seen_sectors or expected is None:
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        seen_sectors.add(sector_id)
        parsed = {
            "sector_id": sector_id,
            "sector_name": _text(row.get("sector_name"), max_length=160),
            "closed_cycle_count": _integer(row.get("closed_cycle_count")),
            "closed_cycle_realized_net_pnl": _decimal(
                row.get("closed_cycle_realized_net_pnl")
            ),
            "open_position_count": _integer(row.get("open_position_count")),
            "open_market_value": _decimal(row.get("open_market_value")),
            "open_cycle_marked_net_pnl": _decimal(
                row.get("open_cycle_marked_net_pnl")
            ),
            "total_attributed_net_pnl": _decimal(
                row.get("total_attributed_net_pnl")
            ),
            "open_market_value_account_equity_fraction": _decimal(
                row.get("open_market_value_account_equity_fraction")
            ),
            "open_market_value_invested_fraction": _decimal(
                row.get("open_market_value_invested_fraction")
            ),
        }
        if (
            any(parsed[key] != expected[key] for key in expected)
            or parsed["total_attributed_net_pnl"]
            != parsed["closed_cycle_realized_net_pnl"]
            + parsed["open_cycle_marked_net_pnl"]
            or parsed["open_market_value_account_equity_fraction"]
            != parsed["open_market_value"] / terminal["equity"]
            or parsed["open_market_value_invested_fraction"]
            != (
                Decimal("0")
                if terminal["market_value"] == 0
                else parsed["open_market_value"] / terminal["market_value"]
            )
        ):
            raise ResearchAuditUnavailable("current_research_artifact_invalid")
        sector_rows.append(parsed)
    if seen_sectors != set(expected_sectors):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    concentration_raw = _mapping(document.get("concentration"))
    concentration = {
        "open_position_count": _integer(
            concentration_raw.get("open_position_count")
        ),
        "max_symbol": concentration_raw.get("max_symbol"),
        "max_symbol_equity_fraction": _decimal(
            concentration_raw.get("max_symbol_equity_fraction")
        ),
        "max_symbol_invested_fraction": _decimal(
            concentration_raw.get("max_symbol_invested_fraction")
        ),
        "symbol_invested_hhi": _decimal(
            concentration_raw.get("symbol_invested_hhi")
        ),
        "max_sector_id": concentration_raw.get("max_sector_id"),
        "max_sector_equity_fraction": _decimal(
            concentration_raw.get("max_sector_equity_fraction")
        ),
        "max_sector_invested_fraction": _decimal(
            concentration_raw.get("max_sector_invested_fraction")
        ),
        "sector_invested_hhi": _decimal(
            concentration_raw.get("sector_invested_hhi")
        ),
    }
    by_symbol = sorted(
        open_rows,
        key=lambda row: (-Decimal(row["market_value"]), str(row["symbol"])),
    )
    by_sector = sorted(
        sector_rows,
        key=lambda row: (
            -Decimal(row["open_market_value"]),
            str(row["sector_id"]),
        ),
    )
    expected_max_symbol = None if not by_symbol else by_symbol[0]["symbol"]
    expected_max_sector = None if not by_sector else by_sector[0]["sector_id"]
    if concentration["max_symbol"] is not None:
        concentration["max_symbol"] = _text(
            concentration["max_symbol"], max_length=32
        )
    if concentration["max_sector_id"] is not None:
        concentration["max_sector_id"] = _text(
            concentration["max_sector_id"], max_length=160
        )
    if (
        concentration["open_position_count"] != len(open_rows)
        or concentration["max_symbol"] != expected_max_symbol
        or concentration["max_sector_id"] != expected_max_sector
        or concentration["max_symbol_equity_fraction"]
        != (
            Decimal("0")
            if not by_symbol
            else Decimal(by_symbol[0]["account_equity_fraction"])
        )
        or concentration["max_symbol_invested_fraction"]
        != (
            Decimal("0")
            if not by_symbol
            else Decimal(by_symbol[0]["invested_market_value_fraction"])
        )
        or concentration["symbol_invested_hhi"]
        != sum(
            (
                Decimal(row["invested_market_value_fraction"]) ** 2
                for row in open_rows
            ),
            Decimal("0"),
        )
        or concentration["max_sector_equity_fraction"]
        != (
            Decimal("0")
            if not by_sector
            else Decimal(
                by_sector[0]["open_market_value_account_equity_fraction"]
            )
        )
        or concentration["max_sector_invested_fraction"]
        != (
            Decimal("0")
            if not by_sector
            else Decimal(by_sector[0]["open_market_value_invested_fraction"])
        )
        or concentration["sector_invested_hhi"]
        != sum(
            (
                Decimal(row["open_market_value_invested_fraction"]) ** 2
                for row in sector_rows
            ),
            Decimal("0"),
        )
        or any(
            not 0 <= Decimal(concentration[key]) <= 1
            for key in (
                "max_symbol_equity_fraction",
                "max_symbol_invested_fraction",
                "symbol_invested_hhi",
                "max_sector_equity_fraction",
                "max_sector_invested_fraction",
                "sector_invested_hhi",
            )
        )
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    disclosures = [
        _text(item, max_length=240)
        for item in _sequence(document.get("disclosures"))
    ]
    if len(disclosures) < 4:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    def present(row: dict[str, object]) -> dict[str, object]:
        return {
            key: format(value, "f") if isinstance(value, Decimal) else value
            for key, value in row.items()
        }

    return {
        "schema": _TERMINAL_ACCOUNTING_SCHEMA,
        "status": "EVALUATED",
        "sector_membership_mode": (
            "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
        ),
        "terminal": present(terminal),
        "pnl_decomposition": {
            "closed_cycle_realized_net_pnl": format(closed_realized, "f"),
            "open_cycle_marked_net_pnl": format(open_marked, "f"),
            "pure_unrealized_net_pnl": None,
            "pure_unrealized_reason": _PURE_UNREALIZED_REASON,
            "identity_difference": "0",
        },
        "concentration": present(concentration),
        "closed_cycles": [present(row) for row in closed_rows],
        "open_positions": [present(row) for row in open_rows],
        "sector_attribution": [present(row) for row in sector_rows],
        "disclosures": disclosures,
    }


def _current_benchmarks(value: object) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    seen: set[str] = set()
    for raw in _sequence(value):
        row = _mapping(raw)
        symbol = _text(row.get("symbol"), max_length=32)
        if symbol in seen:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        seen.add(symbol)
        output.append(
            {
                "symbol": symbol,
                "definition": _text(row.get("definition"), max_length=120),
                "metrics": _current_curve_metrics(row.get("metrics")),
            }
        )
    if not output:
        raise ResearchAuditUnavailable("artifact_invalid_schema")
    return output


def _build_current_research_snapshot(
    payload: dict[str, Any],
    *,
    path: Path,
    root: Path,
    file_sha256: str,
) -> dict[str, object]:
    if (
        payload.get("schema") != _CURRENT_RESEARCH_SCHEMA
        or payload.get("result_label")
        != "RECENT_YEAR_APPROXIMATE_CHANLUN_POINT_RESEARCH_BACKTEST"
        or "forward_paper_session" in payload
        or payload.get("data_grade") != "RESEARCH_APPROXIMATION"
        or payload.get("highest_status") != "RESEARCH_ONLY"
        or payload.get("live_status") != "LIVE_DISABLED"
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    reported_hash = _sha256_id(payload.get("content_sha256"))
    stable = dict(payload)
    stable.pop("content_sha256", None)
    if reported_hash != sha256_json(stable):
        raise ResearchAuditUnavailable("current_research_artifact_hash_mismatch")
    decision_source = _mapping(payload.get("decision_source_snapshot"))
    if not decision_source_snapshot_matches_current(
        decision_source, _PROJECT_ROOT
    ):
        raise ResearchAuditUnavailable(
            "current_research_decision_source_stale"
        )
    raw_input_hashes = _mapping(payload.get("input_hashes"))
    release_receipt: dict[str, object] | None = None
    # The current contract requires immutable direct checkpoints and a complete terminal query
    # plan.  Once both identities are present, the page must also prove where
    # those bytes live; otherwise a new result can silently read the old files
    # that happen to remain beside the canonical page artifact.
    if {"direct_manifest", "terminal_query_plan"}.issubset(raw_input_hashes):
        try:
            release_receipt = verify_sector_release_manifest(
                root=root,
                manifest_path=path.parent / "release_manifest.json",
                expected_artifact_path=path,
            )
        except (OSError, SectorReleaseManifestError) as exc:
            raise ResearchAuditUnavailable(
                "current_research_release_manifest_invalid"
            ) from exc

    strict_result = _mapping(payload.get("strict_full_system_result"))
    if strict_result.get("status") != "NOT_EVALUABLE":
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    strict_reason = _text(strict_result.get("reason"), max_length=180)
    research_result = _mapping(payload.get("research_variant_result"))
    replay = _mapping(research_result.get("replay"))
    if (
        replay.get("result_status") != "RESEARCH_ONLY"
        or replay.get("live_status") != "LIVE_DISABLED"
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    replay_metrics = _current_replay_metrics(replay.get("metrics"))
    performance_status = _text(
        research_result.get("performance_status"), max_length=80
    )
    performance_reason = _text(
        research_result.get("performance_reason"), max_length=180
    )
    if performance_status != "EVALUATED_SAMPLE_INSUFFICIENT":
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    daily_metrics = _current_curve_metrics(research_result.get("daily_metrics"))
    accounting_metrics = _current_curve_metrics(
        research_result.get("accounting_curve_metrics")
    )
    if (
        daily_metrics["status"] != performance_status
        or daily_metrics["net_return"] != replay_metrics["net_return"]
        or accounting_metrics["net_return"] != replay_metrics["net_return"]
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    lifecycle = _current_lifecycle(replay, replay_metrics=replay_metrics)
    scheduler_causality = _current_scheduler_causality_audit(
        payload.get("scheduler_causality_audit"),
        lifecycle=lifecycle,
    )
    causal_ablations = _current_causal_ablations(
        payload.get("ablations"),
        payload.get("ablation_scheduler_causality_audits"),
    )
    terminal_accounting = _current_terminal_accounting(
        research_result.get("terminal_accounting_attribution"),
        replay=replay,
        replay_metrics=replay_metrics,
    )
    tactical = _current_tactical_audit(
        payload.get("tactical_execution_audit"),
        replay_metrics=replay_metrics,
    )
    higher_timeframe_effectiveness = _current_higher_timeframe_effectiveness(
        payload.get("higher_timeframe_effectiveness_audit"),
        candidate_audit=payload.get("candidate_audit"),
    )
    higher_timeframe_execution = (
        _current_higher_timeframe_execution_attribution(
            payload.get("higher_timeframe_execution_attribution"),
            candidate_audit=payload.get("candidate_audit"),
            replay=replay,
            terminal_accounting=_mapping(
                research_result.get("terminal_accounting_attribution")
            ),
        )
    )
    sample_warnings = [
        _text(item, max_length=120)
        for item in _sequence(payload.get("sample_warnings"))
    ]
    expected_warnings = {
        "STRATEGIC_SAMPLE_BELOW_100"
        if replay_metrics["strategic_sample_insufficient"]
        else "STRATEGIC_SAMPLE_ADEQUATE",
        "TACTICAL_SAMPLE_BELOW_200"
        if replay_metrics["tactical_sample_insufficient"]
        else "TACTICAL_SAMPLE_ADEQUATE",
    }
    if set(sample_warnings) != expected_warnings:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    contract = _mapping(replay.get("contract"))
    expected_contract = {
        "l0_source_frequency": "30m",
        "l1_source_frequency": "5m",
        "l2_source_frequency": "1m",
        "selection_path": "QMT_CURRENT_SECTOR_TECHNICAL_ONLY",
        "result_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "level_relation_mode": "CAUSAL_CONFIRMED_POINT_APPROXIMATION",
    }
    if any(contract.get(key) != wanted for key, wanted in expected_contract.items()):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    parameters = _mapping(payload.get("parameter_snapshots"))
    parameter_ids = {
        key: _sha256_id(parameters.get(key))
        for key in (
            "strategy_parameter_set_id",
            "research_parameter_set_id",
            "technical_alignment_parameter_set_id",
            "replay_contract_parameter_set_id",
        )
    }
    variant = _mapping(parameters.get("research_variant"))
    if (
        variant.get("strategic_frequency") != "30m"
        or variant.get("tactical_frequency") != "5m"
        or variant.get("locator_frequency") != "1m"
        or variant.get("selection_path")
        != "QMT_CURRENT_SECTOR_TECHNICAL_ONLY"
        or variant.get("sector_membership_mode")
        != "CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED"
        or variant.get("three_program_mode")
        != "DISABLED_USER_AUTHORIZED"
        or variant.get("tick_data_used") is not False
        or variant.get("live_status") != "LIVE_DISABLED"
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    requested_start = date.fromisoformat(
        _text(variant.get("requested_start"), max_length=10)
    )
    requested_end = date.fromisoformat(
        _text(variant.get("requested_end"), max_length=10)
    )
    effective_start = date.fromisoformat(
        _text(variant.get("effective_start"), max_length=10)
    )
    warmup_start = date.fromisoformat(
        _text(variant.get("warmup_start"), max_length=10)
    )
    if not (
        warmup_start <= requested_start <= effective_start <= requested_end
        and daily_metrics["start"] == effective_start.isoformat()
        and daily_metrics["end"] == requested_end.isoformat()
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    funnel = _mapping(payload.get("candidate_funnel"))
    candidate_funnel = {
        key: _integer(funnel.get(key))
        for key in (
            "all_market_sector_classified_symbols",
            "terminal_recursive_potential_symbols",
            "causally_rescanned_symbols",
            "causal_technical_entry_count",
            "accepted_candidate_count",
            "order_count",
            "fill_count",
        )
    }
    if funnel.get("three_program_prefiltered_symbols") is not None:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")
    if (
        candidate_funnel["order_count"] != replay_metrics["order_count"]
        or candidate_funnel["fill_count"] != replay_metrics["fill_count"]
        or higher_timeframe_effectiveness["candidate_count"]
        != candidate_funnel["causal_technical_entry_count"]
        or higher_timeframe_effectiveness["accepted_candidate_count"]
        != candidate_funnel["accepted_candidate_count"]
    ):
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    scope_raw = _mapping(payload.get("scope"))
    scope = {
        key: _integer(scope_raw.get(key))
        for key in (
            "all_market_scope_symbols",
            "causal_extracted_symbols",
            "audited_direct_symbols",
            "signal_source_symbols",
            "sector_count",
        )
    }
    replay_symbols = [
        _text(item, max_length=32)
        for item in _sequence(scope_raw.get("replay_symbols"))
    ]
    scope["replay_symbols"] = replay_symbols
    scope["selection_order"] = [
        _text(item, max_length=120)
        for item in _sequence(scope_raw.get("selection_order"))
    ]
    if scope["all_market_scope_symbols"] != candidate_funnel[
        "all_market_sector_classified_symbols"
    ]:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    periods = _mapping(research_result.get("periods"))
    split_periods = _mapping(periods.get("train_validation_holdout"))
    holdout = _current_curve_metrics(split_periods.get("FINAL_HOLDOUT_20"))
    benchmarks = _current_benchmarks(payload.get("benchmarks"))
    approximation_disclosures = [
        _text(item, max_length=500)
        for item in _sequence(payload.get("approximation_disclosures"))
    ]
    causality_guards = [
        _text(item, max_length=160)
        for item in _sequence(payload.get("causality_guards"))
    ]
    if not approximation_disclosures or not causality_guards:
        raise ResearchAuditUnavailable("current_research_artifact_invalid")

    input_hashes: dict[str, str | None] = {}
    for key, value in raw_input_hashes.items():
        name = _text(key, max_length=100)
        input_hashes[name] = None if value is None else _sha256_id(value)
    decision_source_id = _sha256_id(decision_source.get("aggregate_sha256"))
    sector_chart_archive = _apply_sector_chart_archive_overlay(
        higher_timeframe_effectiveness,
        root=root,
        artifact_file_sha256=file_sha256,
        artifact_content_sha256=reported_hash,
        decision_source_sha256=decision_source_id,
        input_hashes=input_hashes,
    )
    data_failures = [
        "historical_sector_membership_backfilled_from_current_capture",
        "individual_three_program_disabled",
        "technical_points_are_explicit_approximation",
        "strategic_and_tactical_samples_below_required_thresholds",
    ]
    failed_conditions = ["STRICT_FULL_SYSTEM_NOT_EVALUABLE"]
    if higher_timeframe_effectiveness["strict_green_risk_eligible_count"] == 0:
        data_failures.append(
            "strict_green_higher_timeframe_candidate_sample_empty"
        )
        failed_conditions.append("STRICT_GREEN_HIGHER_TIMEFRAME_SAMPLE_EMPTY")
    failed_conditions.extend(sample_warnings)
    return {
        "schema": "research-audit-page",
        "source_kind": "current_research_variant",
        "strategy_id": "chanlun_current_sector_human_assisted",
        "strategy_label": "缠论统一策略 当前板块触发·30m/5m/1m 人工辅助研究",
        "active_strategy_count": 1,
        "read_only": True,
        "historical": True,
        "no_order_execution": True,
        "evaluation_mode": "fixed_parameters_recent_year",
        "generated_at": None,
        "requested_range": {
            "start": requested_start.isoformat(),
            "end": requested_end.isoformat(),
        },
        "effective_range": {
            "start": effective_start.isoformat(),
            "end": requested_end.isoformat(),
        },
        "data_evidence": {
            "grade": "research_only",
            "failures": data_failures,
            "warnings": sample_warnings,
        },
        "verdict": {
            "live_ready": False,
            "status": "sample_inadequate",
            "failed_conditions": failed_conditions,
        },
        "current_research": {
            "result_label": _text(payload.get("result_label"), max_length=100),
            "strict_full_system_status": "NOT_EVALUABLE",
            "strict_full_system_reason": strict_reason,
            "performance_status": performance_status,
            "performance_reason": performance_reason,
            "daily_metrics": daily_metrics,
            "accounting_curve_metrics": accounting_metrics,
            "holdout_metrics": holdout,
            "replay_metrics": replay_metrics,
            "lifecycle": lifecycle,
            "scheduler_causality_audit": scheduler_causality,
            "causal_ablations": causal_ablations,
            "terminal_accounting_attribution": terminal_accounting,
            "tactical_execution_audit": tactical,
            "candidate_funnel": candidate_funnel,
            "scope": scope,
            "benchmarks": benchmarks,
            "parameter_ids": parameter_ids,
            "research_variant": {
                "sector_taxonomy": _text(
                    variant.get("sector_taxonomy"), max_length=64
                ),
                "sector_membership_mode": _text(
                    variant.get("sector_membership_mode"), max_length=120
                ),
                "three_program_mode": _text(
                    variant.get("three_program_mode"), max_length=100
                ),
                "tick_data_used": False,
                "strategic_frequency": "30m",
                "tactical_frequency": "5m",
                "locator_frequency": "1m",
                "warmup_start": warmup_start.isoformat(),
            },
            "higher_timeframe_data_provenance": _mapping(
                payload.get("higher_timeframe_data_provenance")
            ),
            "higher_timeframe_gate_distribution": _mapping(
                payload.get("higher_timeframe_gate_distribution")
            ),
            "higher_timeframe_effectiveness_audit": (
                higher_timeframe_effectiveness
            ),
            "higher_timeframe_execution_attribution": (
                higher_timeframe_execution
            ),
            "sector_chart_evidence_archive": sector_chart_archive,
            "sector_higher_timeframe_source_distribution": _mapping(
                payload.get("sector_higher_timeframe_source_distribution")
            ),
            "sample_warnings": sample_warnings,
            "causality_guards": causality_guards,
            "approximation_disclosures": approximation_disclosures,
            "input_hashes": input_hashes,
            "decision_source_aggregate_sha256": decision_source_id,
        },
        "content_sha256": reported_hash,
        "artifact": {
            "relative_path": path.relative_to(root).as_posix(),
            "file_sha256": file_sha256,
            "integrity_verified": True,
            "decision_source_matches_current": True,
            "release_manifest_verified": release_receipt is not None,
            "release_manifest": release_receipt,
        },
    }


def build_research_audit_snapshot(root: str | Path) -> dict[str, object]:
    root_path = _root(root)
    current_path = _current_research_artifact(root_path)
    if current_path is not None:
        try:
            current_payload, current_file_sha256 = _load(current_path)
            return _build_current_research_snapshot(
                current_payload,
                path=current_path,
                root=root_path,
                file_sha256=current_file_sha256,
            )
        except ResearchAuditUnavailable as exc:
            if exc.code.startswith("current_research_"):
                raise
            raise ResearchAuditUnavailable(
                "current_research_artifact_invalid"
            ) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise ResearchAuditUnavailable(
                "current_research_artifact_invalid"
            ) from exc
    gate = _validated_causality_gate(root_path)
    path = _artifact(root_path)
    try:
        reported_path = Path(str(gate["report"])).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise ResearchAuditUnavailable("causality_gate_invalid") from exc
    if reported_path != path:
        raise ResearchAuditUnavailable("causality_gate_invalid")
    payload, file_sha256 = _load(path)
    _validate_report(payload, gate=gate)
    point_type_metrics = _mapping(payload.get("point_type_metrics"))
    closed_trade_net_pnl = sum(
        (
            _decimal(_mapping(row).get("net_pnl")) or Decimal("0")
            for row in point_type_metrics.values()
        ),
        Decimal("0"),
    )
    evidence = _mapping(payload.get("data_evidence"))
    evidence_warnings = _sequence(evidence.get("warnings"))
    return {
        "schema": "research-audit-page",
        "source_kind": "certified_report",
        "strategy_id": STRATEGY_ID,
        "strategy_label": _text(payload.get("strategy_label"), max_length=120),
        "active_strategy_count": 1,
        "read_only": True,
        "historical": True,
        "no_order_execution": True,
        "generated_at": _text(payload.get("generated_at"), max_length=64),
        "evaluation_mode": "fixed_policy_one_year",
        "requested_range": _mapping(payload.get("requested_range")),
        "effective_range": _mapping(payload.get("effective_range")),
        "universe": _mapping(payload.get("universe")),
        "data_evidence": evidence,
        "execution_contract": _mapping(payload.get("execution_contract")),
        "walk_forward_windows": _sequence(payload.get("walk_forward_windows")),
        "aggregate_out_of_sample": _mapping(
            payload.get("aggregate_out_of_sample")
        ),
        "point_type_metrics": point_type_metrics,
        "closed_trade_net_pnl": str(closed_trade_net_pnl),
        "terminal_positions_marked_to_market": (
            "terminal_open_positions_marked_to_market_not_same_bar_liquidated"
            in evidence_warnings
        ),
        "sector_year_liquidity_metrics": _mapping(
            payload.get("sector_year_liquidity_metrics")
        ),
        "bootstrap_intervals": payload.get("bootstrap_intervals"),
        "ablations": _sequence(payload.get("ablations")),
        "parameter_robustness": _mapping(
            payload.get("parameter_robustness")
        ),
        "benchmarks": _sequence(payload.get("benchmarks")),
        "concentration": _mapping(payload.get("concentration")),
        "sample_adequacy": _mapping(payload.get("sample_adequacy")),
        "verdict": _mapping(payload.get("verdict")),
        "limitations": _sequence(payload.get("limitations")),
        "algorithm_hashes": _sequence(payload.get("algorithm_hashes")),
        "content_sha256": _text(payload.get("content_sha256"), max_length=80),
        "artifact": {
            "relative_path": path.relative_to(root_path).as_posix(),
            "file_sha256": file_sha256,
            "integrity_verified": True,
        },
    }


def validate_risk_point_chart_lock(
    root: str | Path,
    *,
    point_id: str,
    source_sha256: str,
    review_as_of: int,
) -> dict[str, object]:
    """Bind a chart cutoff to one point in the canonical current audit.

    This is a read-only alternative to a human-review candidate lock.  The
    caller supplies the current effectiveness-audit hash, the stable point
    identity and the exact latest causal observation chosen by that audit.
    Recomputing the page snapshot before lookup prevents a stale or hand-made
    URL from exposing an unrelated symbol or a later K-line prefix.
    """

    if _HASH_RE.fullmatch(point_id) is None or _HASH_RE.fullmatch(source_sha256) is None:
        raise ValueError("risk point chart lock identity is malformed")
    if type(review_as_of) is not int or review_as_of <= 0:
        raise ValueError("risk point chart lock cutoff is malformed")
    snapshot = build_research_audit_snapshot(root)
    if snapshot.get("source_kind") != "current_research_variant":
        raise ValueError("risk point chart lock requires the current research audit")
    current = _mapping(snapshot.get("current_research"))
    audit = _mapping(current.get("higher_timeframe_effectiveness_audit"))
    if audit.get("audit_sha256") != source_sha256:
        raise ValueError("risk point chart lock references a stale audit")

    matches: list[tuple[dict[str, Any], str]] = []
    for subject in _mapping(audit.get("subjects")).values():
        subject_document = _mapping(subject)
        for audit_key, evidence_role in (
            ("globally_deduplicated_point_audit", "MAPPING_SUPPLY"),
            (
                "globally_deduplicated_diagnostic_buy_point_audit",
                "DIAGNOSTIC_BUY_ONLY",
            ),
            (
                "warmup_non_monotonic_point_audit",
                "WARMUP_NON_MONOTONIC_DIAGNOSTIC",
            ),
            (
                "warmup_mapping_supply_point_audit",
                "WARMUP_MAPPING_SUPPLY_DELTA",
            ),
        ):
            point_audit = _mapping(subject_document.get(audit_key))
            for raw in _sequence(point_audit.get("points")):
                point = _mapping(raw)
                if (
                    point.get("point_id") == point_id
                    and point.get("review_as_of_unix") == review_as_of
                ):
                    matches.append((point, evidence_role))
    if not matches:
        raise ValueError("risk point chart lock is not present in the current audit")
    signatures = {
        (
            value.get("source_symbol"),
            value.get("chart_interval"),
            value.get("point_anchor_unix"),
            value.get("point_available_unix"),
            value.get("point_type"),
            evidence_role,
        )
        for value, evidence_role in matches
    }
    if len(signatures) != 1:
        raise ValueError("risk point chart lock is ambiguous")
    point, evidence_role = matches[0]
    if point.get("chart_focus_supported") is not True:
        raise ValueError("risk point source has no verified causal chart")
    anchor = point.get("point_anchor_unix")
    available = point.get("point_available_unix")
    if (
        type(anchor) is not int
        or type(available) is not int
        or anchor > available
        or available > review_as_of
    ):
        raise ValueError("risk point chart lock violates causal time ordering")
    source_symbol = _text(point.get("source_symbol"), max_length=96)
    chart_interval = _text(point.get("chart_interval"), max_length=8)
    chart_source_kind = _text(
        point.get("chart_source_kind"), max_length=64
    )
    archive_fields: dict[str, object] = {}
    if source_symbol.startswith("qmt-gics3:"):
        if chart_source_kind != "VERIFIED_QMT_SECTOR_ARCHIVE":
            raise ValueError("sector risk point lost its archive binding")
        from .sector_chart_archive import (
            SectorChartArchiveUnavailable,
            load_sector_chart_archive,
            sector_chart_entry,
        )

        overlay = _mapping(audit.get("chart_presentation_overlay"))
        try:
            archive = load_sector_chart_archive(
                root,
                expected_manifest_content_sha256=_sha256_id(
                    overlay.get("manifest_content_sha256")
                ),
            )
            entry = sector_chart_entry(
                archive,
                sector_id=source_symbol,
                review_as_of=review_as_of,
                interval=chart_interval,
                verify_file=True,
            )
        except SectorChartArchiveUnavailable as exc:
            raise ValueError("sector risk chart archive is unavailable") from exc
        if entry.get("entry_id") != point.get("sector_chart_archive_entry_id"):
            raise ValueError("sector risk chart archive entry changed")
        archive_fields = {
            "sector_chart_archive_entry_id": entry["entry_id"],
            "sector_chart_archive_manifest_content_sha256": (
                archive.content_sha256
            ),
            "sector_chart_archive_manifest_file_sha256": archive.file_sha256,
            "sector_name": entry["sector_name"],
            "sector_chart_source_revision": entry["source_revision"],
            "sector_chart_price_basis_revision": entry[
                "price_basis_revision"
            ],
        }
    elif chart_source_kind != "A_SHARE_CAUSAL_PREFIX":
        raise ValueError("A-share risk point lost its causal chart binding")
    return {
        "candidate_id": point_id,
        "source_sha256": source_sha256,
        "review_as_of": review_as_of,
        "symbol": source_symbol,
        "review_available_at": _datetime_text(point.get("review_as_of")),
        "focus_at": anchor,
        "point_anchor_at": _datetime_text(point.get("point_anchor_at")),
        "point_available_at": _datetime_text(point.get("point_available_at")),
        "point_type": _text(point.get("point_type"), max_length=16),
        "source_frequency": _text(point.get("source_frequency"), max_length=16),
        "chart_interval": chart_interval,
        "chart_source_kind": chart_source_kind,
        "point_marker_semantics": "TIME_ONLY_NO_PRICE_ANCHOR",
        "lock_kind": "RISK_POINT_AUDIT",
        "evidence_role": evidence_role,
        **archive_fields,
    }


__all__ = (
    "ResearchAuditUnavailable",
    "build_research_audit_snapshot",
    "validate_risk_point_chart_lock",
)
