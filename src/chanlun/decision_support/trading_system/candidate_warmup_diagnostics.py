"""Presentation-only deep warmup diagnostics for human-review candidates.

The active screen remains the sole source of ranking and trading decisions.
This module only selects a bounded, deterministic subset of already-ranked buy
candidates and binds multi-prefix QMT diagnostics to that exact immutable
screen.  A missing or invalid diagnostic must therefore degrade to
``NOT_AVAILABLE`` in the page; it may never change a gate, candidate identity,
paper eligibility, or an order.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import re

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.lifecycle import (
    lifecycle_stage_from_signal,
)
from chanlun.decision_support.trading_system.v3_live_human_review import (
    live_screening_snapshot_content_sha256,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupConvergenceDiagnosticEnvelope,
    WarmupConvergenceEnvelope,
    WarmupMappingSupplyDiagnosticEnvelope,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WarmupStructureLineageDiagnosticEnvelope,
)


CANDIDATE_WARMUP_DIAGNOSTIC_SCHEMA = (
    "chanlun-v3-candidate-warmup-diagnostic/v1"
)
CANDIDATE_WARMUP_PARAMETER_SCHEMA = (
    "chanlun-v3-candidate-warmup-diagnostic-parameters/v1"
)
IMMUTABLE_LIVE_SCREENING_SCHEMA = (
    "chanlun-v3-forward-live-screening-snapshot/v1"
)
DEFAULT_CANDIDATE_LIMIT = 16
DEFAULT_FREQUENCIES = ("d", "30m", "5m", "1m")
DEFAULT_BAR_BUDGETS = {"d": 1600, "30m": 2400, "5m": 9600, "1m": 14400}
DEFAULT_MINIMUM_PREFIX_BARS = {"d": 480, "30m": 480, "5m": 960, "1m": 1440}
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
_A_STOCK_CODE = re.compile(r"^(?:SH|SZ|BJ)\.\d{6}$")
_STAGE_ORDER = {
    "executable": 0,
    "triggered": 1,
    "armed": 2,
    "formed": 3,
    "approaching": 4,
    "observed": 5,
    "active": 6,
}
_POINT_ORDER = {"1buy": 0, "2buy": 1, "3buy": 2}
_CLOSED_STAGES = frozenset({"closed", "invalidated"})


def _require_sha256(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256_ID.fullmatch(value) is None:
        raise ValueError(f"{name} must be a sha256 identity")
    return value


def _sequence(value: object, name: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a sequence")
    return value


def candidate_warmup_parameter_document(
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    frequencies: Sequence[str] = DEFAULT_FREQUENCIES,
    bar_budgets: Mapping[str, int] = DEFAULT_BAR_BUDGETS,
) -> dict[str, object]:
    """Return the frozen, hashable diagnostic parameters.

    The order is part of the identity.  In particular, daily/30m context is
    audited before 5m/1m precision evidence, matching the human workflow.
    """

    values = tuple(str(value) for value in frequencies)
    if candidate_limit <= 0:
        raise ValueError("candidate_limit must be positive")
    if values != tuple(dict.fromkeys(values)) or any(
        value not in DEFAULT_FREQUENCIES for value in values
    ):
        raise ValueError("diagnostic frequencies are invalid")
    budgets = []
    minimums = []
    for frequency in values:
        budget = bar_budgets.get(frequency)
        if type(budget) is not int or budget <= 0:
            raise ValueError("diagnostic bar budget is invalid")
        budgets.append([frequency, budget])
        minimums.append([frequency, DEFAULT_MINIMUM_PREFIX_BARS[frequency]])
    return {
        "schema": CANDIDATE_WARMUP_PARAMETER_SCHEMA,
        "candidate_limit": candidate_limit,
        "selection_scope": "BOUNDED_BUY_CANDIDATES_FROM_EXISTING_SCREEN",
        "selection_order": [
            "lifecycle_stage",
            "sector_horizontal_rank",
            "point_type",
            "code",
            "source_position",
        ],
        "lifecycle_stage_order": [
            "executable",
            "triggered",
            "armed",
            "formed",
            "approaching",
            "observed",
            "active",
        ],
        "point_type_order": ["1buy", "2buy", "3buy"],
        "frequencies": list(values),
        "bar_budgets": budgets,
        "minimum_prefix_bars": minimums,
        "completed_bars_only": True,
        "qmt_skip_download": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "active_gate_unchanged": True,
        "ranking_parameters_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }


def candidate_warmup_parameter_set_id(
    *,
    candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
    frequencies: Sequence[str] = DEFAULT_FREQUENCIES,
    bar_budgets: Mapping[str, int] = DEFAULT_BAR_BUDGETS,
) -> str:
    return sha256_json(
        candidate_warmup_parameter_document(
            candidate_limit=candidate_limit,
            frequencies=frequencies,
            bar_budgets=bar_budgets,
        )
    )


def unwrap_live_screening_snapshot(
    document: Mapping[str, object],
) -> tuple[dict[str, object], str, str | None]:
    """Validate a raw screen or immutable forward wrapper.

    Returns ``(raw_snapshot, source_identity, wrapper_identity)``.  Legacy
    wrappers without ``source_payload_sha256`` used the exact payload hash as
    their source identity; current wrappers use the page-parity semantic hash
    and attest the exact payload independently.
    """

    if document.get("schema") != IMMUTABLE_LIVE_SCREENING_SCHEMA:
        snapshot = dict(document)
        semantic = live_screening_snapshot_content_sha256(snapshot)
        declared = snapshot.get("snapshot_content_sha256")
        if declared is not None and declared != semantic:
            raise ValueError("live screening snapshot content identity changed")
        return snapshot, semantic, None

    stable = dict(document)
    content_identity = _require_sha256(
        stable.pop("content_sha256", None),
        "immutable screening wrapper content_sha256",
    )
    if content_identity != sha256_json(stable):
        raise ValueError("immutable screening wrapper content hash mismatch")
    raw_snapshot = document.get("snapshot")
    if not isinstance(raw_snapshot, Mapping):
        raise ValueError("immutable screening wrapper has no snapshot")
    snapshot = dict(raw_snapshot)
    exact_identity = sha256_json(snapshot)
    semantic_identity = live_screening_snapshot_content_sha256(snapshot)
    source_identity = _require_sha256(
        document.get("source_content_sha256"),
        "immutable screening source_content_sha256",
    )
    payload_identity = document.get("source_payload_sha256")
    if payload_identity is None:
        if source_identity != exact_identity:
            raise ValueError("legacy immutable screening source identity changed")
    elif (
        _require_sha256(payload_identity, "source_payload_sha256")
        != exact_identity
        or source_identity != semantic_identity
    ):
        raise ValueError("immutable screening source identity changed")
    return snapshot, source_identity, content_identity


def select_candidate_warmup_rows(
    snapshot: Mapping[str, object],
    *,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
    explicit_codes: Sequence[str] | None = None,
) -> tuple[dict[str, object], ...]:
    """Select a bounded candidate subset without changing source ranking.

    Modern screens are sorted deterministically by existing presentation facts.
    Very old fixtures that contain no side or lifecycle metadata keep their
    input order so the diagnostic tool remains usable for historical audits.
    """

    if limit <= 0:
        raise ValueError("candidate diagnostic limit must be positive")
    signals = _sequence(snapshot.get("signals"), "screening signals")
    if explicit_codes is not None:
        result = []
        seen = set()
        for position, raw_code in enumerate(explicit_codes):
            code = str(raw_code).upper()
            if _A_STOCK_CODE.fullmatch(code) is None:
                raise ValueError("explicit diagnostic code is invalid")
            if code in seen:
                continue
            seen.add(code)
            result.append(
                {
                    "rank": len(result) + 1,
                    "code": code,
                    "source_position": position,
                    "lifecycle_stage": None,
                    "sector_horizontal_rank": None,
                    "point_type": None,
                    "selection_profile": "EXPLICIT_CODES",
                }
            )
            if len(result) == limit:
                break
        if not result:
            raise ValueError("explicit diagnostic codes are empty")
        return tuple(result)

    modern = any(
        isinstance(value, Mapping)
        and ("side" in value or "lifecycle_stage" in value)
        for value in signals
    )
    candidates: list[tuple[tuple[object, ...], dict[str, object]]] = []
    for position, value in enumerate(signals):
        if not isinstance(value, Mapping):
            continue
        code = value.get("code")
        if not isinstance(code, str) or _A_STOCK_CODE.fullmatch(code) is None:
            continue
        side = str(value.get("side", "")).lower()
        stage = str(lifecycle_stage_from_signal(value) or "").lower()
        if modern and (side != "buy" or stage in _CLOSED_STAGES):
            continue
        point_type = str(value.get("point_type", "")).lower()
        sector = value.get("sector")
        raw_horizontal_rank = (
            sector.get("horizontal_rank")
            if isinstance(sector, Mapping)
            else None
        )
        horizontal_rank = (
            raw_horizontal_rank
            if type(raw_horizontal_rank) is int and raw_horizontal_rank > 0
            else None
        )
        selection_profile = (
            "MODERN_BUY_REVIEW_ORDER" if modern else "LEGACY_INPUT_ORDER"
        )
        row = {
            "code": code,
            "source_position": position,
            "lifecycle_stage": stage or None,
            "sector_horizontal_rank": horizontal_rank,
            "point_type": point_type or None,
            "selection_profile": selection_profile,
        }
        sort_key: tuple[object, ...]
        if modern:
            sort_key = (
                _STAGE_ORDER.get(stage, 10**6),
                horizontal_rank if horizontal_rank is not None else 10**9,
                _POINT_ORDER.get(point_type, 10**6),
                code,
                position,
            )
        else:
            sort_key = (position,)
        candidates.append((sort_key, row))

    candidates.sort(key=lambda value: value[0])
    selected: list[dict[str, object]] = []
    seen_codes: set[str] = set()
    for _sort_key, row in candidates:
        code = str(row["code"])
        if code in seen_codes:
            continue
        seen_codes.add(code)
        selected.append({"rank": len(selected) + 1, **row})
        if len(selected) == limit:
            break
    if not selected:
        raise ValueError("screening snapshot has no auditable buy candidates")
    return tuple(selected)


def candidate_warmup_diagnostic_path(
    forward_root: Path,
    *,
    source_content_sha256: str,
    parameter_set_id: str | None = None,
) -> Path:
    source = _require_sha256(source_content_sha256, "source_content_sha256")
    parameters = _require_sha256(
        parameter_set_id or candidate_warmup_parameter_set_id(),
        "diagnostic_parameter_set_id",
    )
    # One content-addressed binding filename keeps the Windows path comfortably
    # below MAX_PATH even when a test/runtime root is already long.  The full
    # source and parameter identities remain inside the independently hashed
    # document and are revalidated on every read.
    binding = sha256_json(
        {
            "source_content_sha256": source,
            "diagnostic_parameter_set_id": parameters,
        }
    )
    return (
        forward_root.resolve()
        / "candidate_warmup_diagnostics"
        / f"{binding[7:]}.json"
    )


def _canonical_error(value: Mapping[str, object]) -> dict[str, object]:
    return {
        "code": str(value["code"]),
        "frequency": str(value["frequency"]),
        "error_type": str(value["error_type"]),
        "reason": str(value["reason"])[:240],
    }


def build_candidate_warmup_diagnostic_document(
    *,
    source_content_sha256: str,
    source_wrapper_content_sha256: str | None,
    requested_as_of: datetime,
    selected_candidates: Sequence[Mapping[str, object]],
    rows: Sequence[Mapping[str, object]],
    errors: Sequence[Mapping[str, object]],
    parameter_document: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a deterministic, content-addressed diagnostic report."""

    source_identity = _require_sha256(
        source_content_sha256, "source_content_sha256"
    )
    wrapper_identity = (
        None
        if source_wrapper_content_sha256 is None
        else _require_sha256(
            source_wrapper_content_sha256,
            "source_wrapper_content_sha256",
        )
    )
    if requested_as_of.tzinfo is None or requested_as_of.utcoffset() is None:
        raise ValueError("requested_as_of must be timezone-aware")
    parameters = dict(
        parameter_document or candidate_warmup_parameter_document()
    )
    parameter_set_id = sha256_json(parameters)
    selected = [dict(value) for value in selected_candidates]
    frequencies = [str(value) for value in parameters["frequencies"]]
    canonical_rows = [dict(value) for value in rows]
    canonical_errors = [_canonical_error(value) for value in errors]
    counts = Counter(
        str(value["envelope"]["status"])
        for value in canonical_rows
        if isinstance(value.get("envelope"), Mapping)
    )
    stable: dict[str, object] = {
        "schema": CANDIDATE_WARMUP_DIAGNOSTIC_SCHEMA,
        "source_content_sha256": source_identity,
        "source_wrapper_content_sha256": wrapper_identity,
        "requested_as_of": requested_as_of.isoformat(),
        "diagnostic_parameter_set_id": parameter_set_id,
        "diagnostic_parameters": parameters,
        "selected_candidates": selected,
        "codes": [str(value["code"]) for value in selected],
        "frequencies": frequencies,
        "status": "COMPLETE" if not canonical_errors else "PARTIAL",
        "classification_counts": [
            {"status": status, "count": count}
            for status, count in sorted(counts.items())
        ],
        "rows": canonical_rows,
        "errors": canonical_errors,
        "diagnostic_only": True,
        "active_gate_unchanged": True,
        "ranking_parameters_unchanged": True,
        "candidate_identity_unchanged": True,
        "paper_observation_eligibility_unchanged": True,
        "portfolio_performance_evaluable": False,
        "completed_bars_only": True,
        "cutoff_enforced": True,
        "qmt_skip_download": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    document = {**stable, "content_sha256": sha256_json(stable)}
    return validate_candidate_warmup_diagnostic_document(document)


def _validate_envelope_row(value: Mapping[str, object]) -> None:
    envelope_raw = value.get("envelope")
    if not isinstance(envelope_raw, Mapping):
        raise ValueError("candidate warmup row envelope is unavailable")
    envelope = WarmupConvergenceEnvelope.from_document(envelope_raw)
    if envelope.frequency != value.get("frequency"):
        raise ValueError("candidate warmup row frequency changed")
    semantic_raw = value.get("semantic_diagnostic")
    supply_raw = value.get("mapping_supply_diagnostic")
    lineage_raw = value.get("structure_lineage_diagnostic")
    if semantic_raw is not None:
        if not isinstance(semantic_raw, Mapping):
            raise ValueError("semantic warmup diagnostic is malformed")
        semantic = WarmupConvergenceDiagnosticEnvelope.from_document(semantic_raw)
        semantic.validate_against(envelope)
        envelope = replace(envelope, diagnostic=semantic)
    if supply_raw is not None:
        if not isinstance(supply_raw, Mapping):
            raise ValueError("mapping warmup diagnostic is malformed")
        supply = WarmupMappingSupplyDiagnosticEnvelope.from_document(supply_raw)
        supply.validate_against(envelope)
        envelope = replace(envelope, mapping_supply_diagnostic=supply)
    if lineage_raw is not None:
        if not isinstance(lineage_raw, Mapping):
            raise ValueError("lineage warmup diagnostic is malformed")
        lineage = WarmupStructureLineageDiagnosticEnvelope.from_document(lineage_raw)
        envelope = replace(envelope, structure_lineage_diagnostic=lineage)


def validate_candidate_warmup_diagnostic_document(
    raw: Mapping[str, object],
    *,
    expected_source_content_sha256: str | None = None,
    expected_parameter_set_id: str | None = None,
) -> dict[str, object]:
    """Validate report identity, complete pair coverage and safety contract."""

    value = dict(raw)
    content_identity = _require_sha256(
        value.pop("content_sha256", None), "candidate diagnostic content_sha256"
    )
    if content_identity != sha256_json(value):
        raise ValueError("candidate warmup diagnostic content hash mismatch")
    if raw.get("schema") != CANDIDATE_WARMUP_DIAGNOSTIC_SCHEMA:
        raise ValueError("candidate warmup diagnostic schema changed")
    source_identity = _require_sha256(
        raw.get("source_content_sha256"), "source_content_sha256"
    )
    if (
        expected_source_content_sha256 is not None
        and source_identity != expected_source_content_sha256
    ):
        raise ValueError("candidate warmup diagnostic source changed")
    wrapper_identity = raw.get("source_wrapper_content_sha256")
    if wrapper_identity is not None:
        _require_sha256(wrapper_identity, "source_wrapper_content_sha256")
    try:
        requested_as_of = datetime.fromisoformat(str(raw["requested_as_of"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("candidate warmup diagnostic cutoff is invalid") from exc
    if requested_as_of.tzinfo is None or requested_as_of.utcoffset() is None:
        raise ValueError("candidate warmup diagnostic cutoff is naive")
    parameters = raw.get("diagnostic_parameters")
    if not isinstance(parameters, Mapping):
        raise ValueError("candidate warmup parameters are unavailable")
    expected_parameters = candidate_warmup_parameter_document(
        candidate_limit=int(parameters["candidate_limit"]),
        frequencies=tuple(str(value) for value in parameters["frequencies"]),
        bar_budgets={str(key): int(amount) for key, amount in parameters["bar_budgets"]},
    )
    if dict(parameters) != expected_parameters:
        raise ValueError("candidate warmup parameters are non-canonical")
    parameter_set_id = _require_sha256(
        raw.get("diagnostic_parameter_set_id"),
        "diagnostic_parameter_set_id",
    )
    if parameter_set_id != sha256_json(expected_parameters):
        raise ValueError("candidate warmup parameter identity changed")
    if (
        expected_parameter_set_id is not None
        and parameter_set_id != expected_parameter_set_id
    ):
        raise ValueError("candidate warmup parameter set changed")

    selected_raw = _sequence(raw.get("selected_candidates"), "selected_candidates")
    selected = [dict(item) for item in selected_raw if isinstance(item, Mapping)]
    if len(selected) != len(selected_raw) or not selected:
        raise ValueError("candidate warmup selection is invalid")
    codes = [str(value["code"]) for value in selected]
    if raw.get("codes") != codes or len(codes) != len(set(codes)):
        raise ValueError("candidate warmup code selection changed")
    if [value.get("rank") for value in selected] != list(range(1, len(codes) + 1)):
        raise ValueError("candidate warmup selection rank changed")
    if len(codes) > int(parameters["candidate_limit"]) or any(
        _A_STOCK_CODE.fullmatch(code) is None for code in codes
    ):
        raise ValueError("candidate warmup selection exceeds its contract")
    frequencies = list(parameters["frequencies"])
    if raw.get("frequencies") != frequencies:
        raise ValueError("candidate warmup frequencies changed")

    rows_raw = _sequence(raw.get("rows"), "candidate warmup rows")
    errors_raw = _sequence(raw.get("errors"), "candidate warmup errors")
    rows = [dict(item) for item in rows_raw if isinstance(item, Mapping)]
    errors = [dict(item) for item in errors_raw if isinstance(item, Mapping)]
    if len(rows) != len(rows_raw) or len(errors) != len(errors_raw):
        raise ValueError("candidate warmup row coverage is malformed")
    expected_pairs = {(code, frequency) for code in codes for frequency in frequencies}
    actual_pairs: list[tuple[str, str]] = []
    for row in rows:
        pair = (str(row.get("code")), str(row.get("frequency")))
        actual_pairs.append(pair)
        _validate_envelope_row(row)
    for error in errors:
        if error != _canonical_error(error):
            raise ValueError("candidate warmup error is non-canonical")
        actual_pairs.append((str(error["code"]), str(error["frequency"])))
    if set(actual_pairs) != expected_pairs or len(actual_pairs) != len(expected_pairs):
        raise ValueError("candidate warmup pair coverage changed")
    expected_status = "COMPLETE" if not errors else "PARTIAL"
    if raw.get("status") != expected_status:
        raise ValueError("candidate warmup status changed")
    counts = Counter(str(row["envelope"]["status"]) for row in rows)
    expected_counts = [
        {"status": status, "count": count}
        for status, count in sorted(counts.items())
    ]
    if raw.get("classification_counts") != expected_counts:
        raise ValueError("candidate warmup classification counts changed")
    for name, expected in {
        "diagnostic_only": True,
        "active_gate_unchanged": True,
        "ranking_parameters_unchanged": True,
        "candidate_identity_unchanged": True,
        "paper_observation_eligibility_unchanged": True,
        "portfolio_performance_evaluable": False,
        "completed_bars_only": True,
        "cutoff_enforced": True,
        "qmt_skip_download": True,
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }.items():
        if raw.get(name) != expected:
            raise ValueError(f"candidate warmup safety field {name} changed")
    return dict(raw)


def candidate_warmup_presentation(
    raw: Mapping[str, object],
) -> dict[str, object]:
    """Return a compact page model while keeping bulky evidence on disk."""

    document = validate_candidate_warmup_diagnostic_document(raw)
    by_code: dict[str, list[dict[str, object]]] = {
        str(code): [] for code in document["codes"]
    }
    for row in document["rows"]:
        envelope = row["envelope"]
        observations = envelope["observations"]
        by_code[str(row["code"])].append(
            {
                "frequency": row["frequency"],
                "status": envelope["status"],
                "reason_codes": list(envelope["reason_codes"]),
                "observation_count": envelope["observation_count"],
                "prefix_bar_counts": [
                    value["bar_count"] for value in observations
                ],
                "available_bar_count": row["available_bar_count"],
                "market_data_as_of": row["market_data_as_of"],
                "envelope_content_sha256": envelope["content_sha256"],
            }
        )
    errors_by_code: dict[str, list[dict[str, object]]] = {
        str(code): [] for code in document["codes"]
    }
    for error in document["errors"]:
        errors_by_code[str(error["code"])].append(
            {
                "frequency": error["frequency"],
                "status": "UNAVAILABLE",
                "reason_codes": [error["error_type"]],
                "reason": error["reason"],
            }
        )
    candidates = {}
    for selected in document["selected_candidates"]:
        code = str(selected["code"])
        frequencies = [*by_code[code], *errors_by_code[code]]
        candidates[code] = {
            "selected": True,
            "rank": selected["rank"],
            "status": (
                "PARTIAL"
                if errors_by_code[code]
                else (
                    "NON_MONOTONIC"
                    if any(value["status"] == "NON_MONOTONIC" for value in frequencies)
                    else "AVAILABLE"
                )
            ),
            "frequencies": frequencies,
            "diagnostic_only": True,
            "active_gate_unchanged": True,
            "live_status": "LIVE_DISABLED",
        }
    return {
        "status": document["status"],
        "source_content_sha256": document["source_content_sha256"],
        "diagnostic_parameter_set_id": document[
            "diagnostic_parameter_set_id"
        ],
        "content_sha256": document["content_sha256"],
        "selected_candidate_count": len(document["codes"]),
        "classification_counts": document["classification_counts"],
        "candidates": candidates,
        "diagnostic_only": True,
        "active_gate_unchanged": True,
        "ranking_parameters_unchanged": True,
        "candidate_identity_unchanged": True,
        "paper_observation_eligibility_unchanged": True,
        "live_status": "LIVE_DISABLED",
    }


__all__ = [
    "CANDIDATE_WARMUP_DIAGNOSTIC_SCHEMA",
    "DEFAULT_BAR_BUDGETS",
    "DEFAULT_CANDIDATE_LIMIT",
    "DEFAULT_FREQUENCIES",
    "DEFAULT_MINIMUM_PREFIX_BARS",
    "build_candidate_warmup_diagnostic_document",
    "candidate_warmup_diagnostic_path",
    "candidate_warmup_parameter_document",
    "candidate_warmup_parameter_set_id",
    "candidate_warmup_presentation",
    "select_candidate_warmup_rows",
    "unwrap_live_screening_snapshot",
    "validate_candidate_warmup_diagnostic_document",
]
