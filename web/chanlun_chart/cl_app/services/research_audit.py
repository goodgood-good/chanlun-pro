"""Strict read-only presentation model for the new causal backtest report."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from chanlun.decision_support.trading_system.backtest.report import (
    SCHEMA_VERSION,
    STRATEGY_ID,
    verify_report_hash,
)


_MAX_JSON_BYTES = 64 * 1024 * 1024
_ARTIFACT_DIRECTORY = Path("audit/chanlun_trading_system_backtest")
_ARTIFACT_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,120}\.json$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_GRADES = {"certified", "research_only", "invalid"}


class ResearchAuditUnavailable(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


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
        directory = (root / _ARTIFACT_DIRECTORY).resolve(strict=True)
        directory.relative_to(root)
        candidates = [
            path
            for path in directory.iterdir()
            if path.is_file() and _ARTIFACT_RE.fullmatch(path.name) is not None
        ]
        path = max(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name))
        path.relative_to(root)
        size = path.stat().st_size
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchAuditUnavailable("artifact_unavailable") from exc
    if size <= 0 or size > _MAX_JSON_BYTES:
        raise ResearchAuditUnavailable("artifact_unavailable")
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


def _validate_algorithm_hashes(value: object) -> None:
    seen: set[str] = set()
    for row in _sequence(value):
        document = _mapping(row)
        source = _text(document.get("source"), max_length=300)
        digest = _text(document.get("sha256"), max_length=80)
        if source in seen or _HASH_RE.fullmatch(digest) is None:
            raise ResearchAuditUnavailable("artifact_invalid_schema")
        seen.add(source)


def _validate_report(payload: dict[str, Any]) -> None:
    if (
        payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("strategy_id") != STRATEGY_ID
        or payload.get("active_strategy_count") != 1
        or payload.get("read_only") is not True
        or payload.get("historical") is not True
        or payload.get("no_order_execution") is not True
    ):
        raise ResearchAuditUnavailable("strategy_contract_invalid")
    if not verify_report_hash(payload):
        raise ResearchAuditUnavailable("artifact_hash_mismatch")
    _validate_algorithm_hashes(payload.get("algorithm_hashes"))
    _text(payload.get("generated_at"), max_length=64)
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
        "sector_price_source": "tdx_native_880_index",
        "sector_price_change_gate": False,
        "next_tradable_minute_fill": True,
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


def build_research_audit_snapshot(root: str | Path) -> dict[str, object]:
    root_path = _root(root)
    path = _artifact(root_path)
    payload, file_sha256 = _load(path)
    _validate_report(payload)
    return {
        "schema_version": "research-audit-page-v11",
        "strategy_id": STRATEGY_ID,
        "strategy_label": _text(payload.get("strategy_label"), max_length=120),
        "active_strategy_count": 1,
        "read_only": True,
        "historical": True,
        "no_order_execution": True,
        "generated_at": _text(payload.get("generated_at"), max_length=64),
        "data_evidence": _mapping(payload.get("data_evidence")),
        "execution_contract": _mapping(payload.get("execution_contract")),
        "walk_forward_windows": _sequence(payload.get("walk_forward_windows")),
        "aggregate_out_of_sample": _mapping(
            payload.get("aggregate_out_of_sample")
        ),
        "point_type_metrics": _mapping(payload.get("point_type_metrics")),
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


__all__ = (
    "ResearchAuditUnavailable",
    "build_research_audit_snapshot",
)
