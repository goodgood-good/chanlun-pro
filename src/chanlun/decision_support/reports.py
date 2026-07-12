"""Fail-closed rollout reporting for decision-support validation.

Reports are evidence artifacts only.  They never authorize live trading or
automatic execution.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Iterable, Mapping

from .fingerprints import normalize_datetime, sha256_json
from .models import StrategyTrack
from .promotion import PromotionDecision, evaluate_promotion


TRACKS = tuple(track.value for track in StrategyTrack)
VERSION_KEYS = (
    "data",
    "algorithm",
    "rule_set",
    "corpus_manifest",
    "model_version",
)
_FINGERPRINT_RE = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class RolloutBundle:
    output_dir: Path
    report_path: Path
    attribution_path: Path
    markdown_path: Path
    manifest_path: Path
    report_fingerprint: str


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("report values must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("report decimal values must be finite")
        return str(value)
    if isinstance(value, datetime):
        return normalize_datetime(value, "report datetime").isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _json_safe(value.to_dict())
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("report mappings require string keys")
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    raise TypeError(f"unsupported report value: {type(value).__name__}")


def _attribution_snapshot(rows: Iterable[object]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for row in rows:
        value = row.to_dict() if hasattr(row, "to_dict") else row
        if not isinstance(value, Mapping):
            raise TypeError("attribution rows must be mappings or expose to_dict")
        normalized = _json_safe(value)
        if not isinstance(normalized, dict):
            raise TypeError("attribution rows must serialize to mappings")
        result.append(normalized)
    track_order = {track: index for index, track in enumerate(TRACKS)}
    result.sort(
        key=lambda row: (
            track_order.get(str(row.get("strategy_track")), len(TRACKS)),
            str(row.get("trade_id", "")),
        )
    )
    return result


def _version_snapshot(
    values: Mapping[str, object] | object,
) -> tuple[dict[str, object], tuple[str, ...]]:
    source = values if isinstance(values, Mapping) else {}
    result: dict[str, object] = {}
    reasons: list[str] = []
    for key in VERSION_KEYS:
        value = source.get(key)
        result[key] = value if isinstance(value, str) or value is None else None
        if value is None:
            reasons.append(f"{key}_fingerprint_unknown")
        elif not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
            reasons.append(f"{key}_fingerprint_invalid")
    return result, tuple(reasons)


def _review_snapshot(
    value: object,
) -> tuple[dict[str, object], tuple[str, ...]]:
    source = value if isinstance(value, Mapping) else {}
    reasons: list[str] = []
    result: dict[str, object] = {}
    for name in ("confirmed", "rejected", "abstained"):
        count = source.get(name)
        result[name] = count
        if count is None:
            reasons.append(f"{name}_review_count_unknown")
        elif isinstance(count, bool) or not isinstance(count, int) or count < 0:
            reasons.append(f"{name}_review_count_invalid")
    for name in ("rejection_reasons", "abstain_reasons"):
        raw_reasons = source.get(name)
        if not isinstance(raw_reasons, Mapping):
            result[name] = {}
            reasons.append(f"{name}_unknown")
            continue
        normalized: dict[str, int] = {}
        for reason, count in raw_reasons.items():
            if (
                not isinstance(reason, str)
                or not reason
                or isinstance(count, bool)
                or not isinstance(count, int)
                or count < 0
            ):
                reasons.append(f"{name}_invalid")
                continue
            normalized[reason] = count
        result[name] = dict(sorted(normalized.items()))
    return result, tuple(dict.fromkeys(reasons))


def _missing_promotion(track: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "track": track,
        "state": "research",
        "promoted": False,
        "paper_gate_pending": True,
        "reasons": ["promotion_decision_missing"],
        "evaluated_at": None,
        "metrics_fingerprint": None,
        "metrics": {},
    }


def _metric_version_reasons(
    metrics: Mapping[str, object],
    versions: Mapping[str, object],
) -> tuple[str, ...]:
    fields = (
        ("data_fingerprints", "data"),
        ("algorithm_fingerprints", "algorithm"),
        ("rule_set_fingerprints", "rule_set"),
        ("corpus_manifest_fingerprints", "corpus_manifest"),
    )
    reasons: list[str] = []
    for metric_field, version_key in fields:
        observed = metrics.get(metric_field)
        expected = versions.get(version_key)
        if (
            isinstance(observed, list)
            and observed
            and isinstance(expected, str)
            and any(item != expected for item in observed)
        ):
            reasons.append(f"{version_key}_fingerprint_mismatch")
    return tuple(reasons)


def build_rollout_report(
    *,
    generated_at: datetime,
    version_fingerprints: Mapping[str, object],
    promotion_by_track: Mapping[str, PromotionDecision],
    attribution_rows: Iterable[object] = (),
    review_outcomes_by_track: Mapping[str, Mapping[str, object]] | None = None,
    corpus_integrity: Mapping[str, object] | None = None,
    monitoring_active: bool = True,
) -> dict[str, object]:
    """Build a JSON-safe two-track rollout evidence report."""

    normalized_time = normalize_datetime(generated_at, "generated_at")
    versions, version_reasons = _version_snapshot(version_fingerprints)
    report_input_reasons: list[str] = []
    if monitoring_active is None:
        monitoring_state = False
        report_input_reasons.append("monitoring_active_unknown")
    elif not isinstance(monitoring_active, bool):
        monitoring_state = False
        report_input_reasons.append("monitoring_active_invalid")
    else:
        monitoring_state = monitoring_active
        if not monitoring_state:
            report_input_reasons.append("monitoring_inactive")
    attribution = _attribution_snapshot(attribution_rows)
    if any(row.get("strategy_track") not in TRACKS for row in attribution):
        report_input_reasons.append("attribution_strategy_track_invalid")
    reviews = review_outcomes_by_track or {}
    corpus = dict(corpus_integrity or {})
    corpus_reasons: list[str] = []
    if not corpus:
        corpus_reasons.append("corpus_integrity_unknown")
    elif corpus.get("status") != "complete":
        corpus_reasons.append("corpus_integrity_incomplete")
    if (
        corpus.get("manifest_fingerprint") is not None
        and versions["corpus_manifest"] is not None
        and corpus.get("manifest_fingerprint") != versions["corpus_manifest"]
    ):
        corpus_reasons.append("corpus_manifest_fingerprint_mismatch")
    oos_by_track: dict[str, object] = {}
    paper_by_track: dict[str, object] = {}
    event_parity: dict[str, object] = {}
    risk_violations: dict[str, object] = {}
    review_citations: dict[str, object] = {}
    restart_recovery: dict[str, object] = {}
    promotion: dict[str, object] = {}
    normalized_reviews: dict[str, object] = {}
    rejection_reasons: dict[str, object] = {}
    abstain_reasons: dict[str, object] = {}
    gate_reasons: dict[str, object] = {}
    gate_passed: dict[str, bool] = {}
    paper_pending: dict[str, bool] = {}

    for track in TRACKS:
        raw_decision = promotion_by_track.get(track)
        track_reasons: list[str] = [
            *report_input_reasons,
            *version_reasons,
            *corpus_reasons,
        ]
        if isinstance(raw_decision, PromotionDecision):
            if raw_decision.track != track:
                track_reasons.append("promotion_track_mismatch")
            decision = evaluate_promotion(track, raw_decision.metrics)
            promotion_payload = decision.to_dict()
            metrics = decision.metrics.to_dict()
            track_reasons.extend(decision.reasons)
            track_reasons.extend(_metric_version_reasons(metrics, versions))
        else:
            decision = None
            promotion_payload = _missing_promotion(track)
            metrics = promotion_payload["metrics"]
            track_reasons.append("promotion_decision_missing")
        oos_by_track[track] = {
            "completed_trades": metrics.get("oos_trades"),
            "net_expectancy": metrics.get("net_expectancy"),
            "profit_factor": metrics.get("profit_factor"),
            "max_drawdown": metrics.get("max_drawdown"),
            "event_parity": metrics.get("event_parity"),
            "risk_violations": metrics.get("risk_violations"),
            "lookahead_events": metrics.get("lookahead_events"),
            "zero_fill_fake_positions": metrics.get(
                "zero_fill_fake_positions"
            ),
        }
        paper_dates = metrics.get("paper_trading_dates")
        day_count = len(paper_dates) if isinstance(paper_dates, list) else None
        executable_events = metrics.get("paper_executable_events")
        paper_gate_reasons: list[str] = []
        if day_count is None:
            paper_gate_reasons.append("paper_trading_dates_unknown")
        elif day_count < 20:
            paper_gate_reasons.append("insufficient_paper_trading_days")
        if executable_events is None:
            paper_gate_reasons.append("paper_executable_events_unknown")
        elif (
            isinstance(executable_events, bool)
            or not isinstance(executable_events, int)
            or executable_events < 0
        ):
            paper_gate_reasons.append("paper_executable_events_invalid")
        elif executable_events < 30:
            paper_gate_reasons.append("insufficient_paper_executable_events")
        if decision is not None:
            paper_prefixes = (
                "paper_",
                "exchange_calendar_",
                "insufficient_paper_",
                "duplicate_paper_",
                "critical_ledger_",
                "uncited_executable_review",
                "restart_recovery",
            )
            paper_gate_reasons.extend(
                reason
                for reason in decision.reasons
                if reason.startswith(paper_prefixes)
            )
        paper_gate_reasons = list(dict.fromkeys(paper_gate_reasons))
        paper_by_track[track] = {
            "trading_dates": paper_dates,
            "trading_day_count": day_count,
            "exchange_calendar_verified": metrics.get(
                "exchange_calendar_verified"
            ),
            "exchange_calendar_fingerprint": metrics.get(
                "exchange_calendar_fingerprint"
            ),
            "executable_events": executable_events,
            "critical_ledger_mismatches": metrics.get(
                "critical_ledger_mismatches"
            ),
            "uncited_executable_reviews": metrics.get(
                "uncited_executable_reviews"
            ),
            "restart_recovery": metrics.get("restart_recovery"),
            "gate_reasons": paper_gate_reasons,
        }
        event_parity[track] = metrics.get("event_parity")
        risk_violations[track] = metrics.get("risk_violations")
        review_citations[track] = {
            "uncited_executable_reviews": metrics.get(
                "uncited_executable_reviews"
            )
        }
        restart_recovery[track] = metrics.get("restart_recovery")
        promotion[track] = promotion_payload
        review_payload, review_reasons = _review_snapshot(reviews.get(track))
        normalized_reviews[track] = review_payload
        rejection_reasons[track] = review_payload["rejection_reasons"]
        abstain_reasons[track] = review_payload["abstain_reasons"]
        track_reasons.extend(review_reasons)
        distinct_reasons = tuple(dict.fromkeys(track_reasons))
        gate_reasons[track] = list(distinct_reasons)
        gate_passed[track] = not distinct_reasons and bool(
            decision is not None and decision.promoted
        )
        paper_pending[track] = (
            not isinstance(day_count, int)
            or day_count < 20
            or isinstance(executable_events, bool)
            or not isinstance(executable_events, int)
            or executable_events < 30
        )

    continue_paper = any(paper_pending.values())
    if report_input_reasons:
        status = "validation_blocked"
    elif continue_paper:
        status = "continue_paper"
    elif all(gate_passed.values()):
        status = "eligible_for_small_cap_manual_review"
    else:
        status = "validation_blocked"

    report: dict[str, object] = {
        "schema_version": 1,
        "generated_at": normalized_time.isoformat(),
        "version_fingerprints": versions,
        "corpus_integrity": corpus,
        "event_parity": event_parity,
        "oos_by_track": oos_by_track,
        "paper_by_track": paper_by_track,
        "risk_violations": risk_violations,
        "review_citations": review_citations,
        "restart_recovery": restart_recovery,
        "promotion": promotion,
        "review_outcomes_by_track": normalized_reviews,
        "rejection_reasons_by_track": rejection_reasons,
        "abstain_reasons_by_track": abstain_reasons,
        "gate_reasons_by_track": gate_reasons,
        "gate_passed_by_track": gate_passed,
        "report_input_reasons": list(report_input_reasons),
        "attribution_rows": attribution,
        "monitoring_active": monitoring_state,
        "continue_paper": continue_paper,
        "operational_gate": {
            "minimum_trading_days": 20,
            "minimum_executable_events": 30,
            "continue_paper": continue_paper,
            "monitoring_active": monitoring_state,
        },
        "status": status,
        "live_trading_approved": False,
        "automatic_execution_enabled": False,
    }
    report["report_fingerprint"] = sha256_json(report)
    return report


def _sha256_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _csv_bytes(rows: list[dict[str, object]]) -> bytes:
    preferred = (
        "trade_id",
        "strategy_track",
        "status",
        "rejection_reasons",
        "model_verdict",
        "return_net",
    )
    fields = set().union(*(row.keys() for row in rows)) if rows else set()
    fieldnames = list(preferred)
    fieldnames.extend(sorted(fields.difference(fieldnames)))
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                key: (
                    json.dumps(value, ensure_ascii=False, sort_keys=True)
                    if isinstance(value, (dict, list))
                    else value
                )
                for key, value in row.items()
            }
        )
    return stream.getvalue().encode("utf-8")


def _markdown_bytes(report: Mapping[str, object]) -> bytes:
    lines = [
        "# 缠论决策支持 rollout 验证报告",
        "",
        "> 本报告仅记录研究与纸面验证证据，不构成实盘交易批准，也不启用自动执行。",
        "",
        f"- 报告状态：`{report.get('status')}`",
        f"- 继续纸面验证：`{str(report.get('continue_paper')).lower()}`",
        f"- 监控保持运行：`{str(report.get('monitoring_active')).lower()}`",
        "",
        "| 策略轨 | 状态 | 纸面交易日 | 可执行事件 | 门禁原因 |",
        "|---|---|---:|---:|---|",
    ]
    paper = report.get("paper_by_track")
    promotions = report.get("promotion")
    reasons = report.get("gate_reasons_by_track")
    paper_map = paper if isinstance(paper, Mapping) else {}
    promotion_map = promotions if isinstance(promotions, Mapping) else {}
    reason_map = reasons if isinstance(reasons, Mapping) else {}
    for track in TRACKS:
        track_paper = paper_map.get(track)
        track_promotion = promotion_map.get(track)
        values = track_paper if isinstance(track_paper, Mapping) else {}
        decision = track_promotion if isinstance(track_promotion, Mapping) else {}
        raw_reasons = reason_map.get(track)
        rendered_reasons = ", ".join(raw_reasons) if isinstance(raw_reasons, list) else "unknown"
        lines.append(
            f"| {track} | {decision.get('state', 'research')} | "
            f"{values.get('trading_day_count')} | "
            f"{values.get('executable_events')} | {rendered_reasons} |"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


def write_rollout_bundle(
    output_dir: str | Path,
    report: Mapping[str, object],
) -> RolloutBundle:
    """Write authoritative JSON plus CSV/Markdown views with atomic files."""

    normalized = _json_safe(report)
    if not isinstance(normalized, dict):
        raise TypeError("report must be a mapping")
    fingerprint = normalized.get("report_fingerprint")
    unsigned = dict(normalized)
    unsigned.pop("report_fingerprint", None)
    if (
        not isinstance(fingerprint, str)
        or fingerprint != sha256_json(unsigned)
    ):
        raise ValueError("report_fingerprint_mismatch")
    rows = normalized.get("attribution_rows")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("attribution_rows_invalid")

    report_bytes = _json_bytes(normalized)
    attribution_bytes = _csv_bytes(rows)
    markdown_bytes = _markdown_bytes(normalized)
    files = {
        "rollout-report.json": report_bytes,
        "attribution.csv": attribution_bytes,
        "rollout-report.md": markdown_bytes,
    }
    manifest = {
        "schema_version": 1,
        "report_fingerprint": fingerprint,
        "live_trading_approved": False,
        "files": {
            name: _sha256_bytes(payload)
            for name, payload in files.items()
        },
    }
    files["bundle-manifest.json"] = _json_bytes(manifest)

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".rollout-stage-", dir=destination))
    backups: dict[str, Path] = {}
    committed: set[str] = set()
    try:
        for name, payload in files.items():
            (stage / name).write_bytes(payload)
        for name in files:
            target = destination / name
            if target.exists():
                backup = stage / f".backup-{name}"
                os.replace(target, backup)
                backups[name] = backup
            os.replace(stage / name, target)
            committed.add(name)
    except BaseException:
        rollback_errors: list[BaseException] = []
        for name in reversed(tuple(files)):
            target = destination / name
            backup = backups.get(name)
            try:
                if name in committed and target.exists():
                    target.unlink()
                if backup is not None and backup.exists():
                    os.replace(backup, target)
            except BaseException as exc:
                rollback_errors.append(exc)
        if rollback_errors:
            raise RuntimeError("rollout_bundle_rollback_failed") from rollback_errors[0]
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)

    return RolloutBundle(
        output_dir=destination,
        report_path=destination / "rollout-report.json",
        attribution_path=destination / "attribution.csv",
        markdown_path=destination / "rollout-report.md",
        manifest_path=destination / "bundle-manifest.json",
        report_fingerprint=fingerprint,
    )


def main(argv: list[str] | None = None) -> int:
    """Verify an authoritative report JSON and materialize its bundle."""

    parser = argparse.ArgumentParser(
        description="Verify and write a decision-support rollout bundle."
    )
    parser.add_argument("--input-report", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    payload = json.loads(arguments.input_report.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("input report must be a JSON object")
    bundle = write_rollout_bundle(arguments.output_dir, payload)
    print(
        json.dumps(
            {
                "output_dir": str(bundle.output_dir),
                "report_fingerprint": bundle.report_fingerprint,
                "live_trading_approved": False,
            },
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
    )
    return 0


__all__ = [
    "RolloutBundle",
    "TRACKS",
    "VERSION_KEYS",
    "build_rollout_report",
    "main",
    "write_rollout_bundle",
]


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())
