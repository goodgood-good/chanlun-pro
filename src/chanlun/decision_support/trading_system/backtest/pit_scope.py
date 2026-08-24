"""Fail-closed scope proof for point-in-time replay metadata.

The historical replay may request only a handful of stocks while still needing
every historical member of the requested stocks' SW1 industries to build a
truthful sector composite.  This module keeps that distinction explicit:
``requested_codes`` are strategy subjects and ``closure_codes`` are metadata /
sector-composite dependencies.  A scoped snapshot is admissible only when the
closure was derived from a complete, content-addressed checkpoint inventory.
"""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Mapping, Sequence

from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    CN,
    PITMetadataSnapshot,
    SectorMembershipChange,
    sha256_json,
)


PIT_SCOPE_SCHEMA = "chanlun-qmt-pit-scope/v1"
SCOPED_SECTOR_CLOSURE_MODE = "SCOPED_SW1_SECTOR_CLOSURE"
FULL_MARKET_MODE = "FULL_MARKET_EXPLICIT"
MAX_UNCONFIRMED_REQUESTED_CODES = 20


def relevant_sector_ids(
    memberships: Sequence[SectorMembershipChange],
    *,
    codes: Sequence[str],
    start: date,
    end: date,
) -> tuple[str, ...]:
    """Return sectors effective for ``codes`` at any point in ``[start, end]``."""

    requested = set(codes)
    start_at = datetime.combine(start, time.min, tzinfo=CN)
    end_at = datetime.combine(end, time.max, tzinfo=CN)
    rows_by_code: dict[str, list[SectorMembershipChange]] = {
        code: [] for code in requested
    }
    for row in memberships:
        if row.code in requested and row.known_at <= end_at:
            rows_by_code[row.code].append(row)

    sectors: set[str] = set()
    for rows in rows_by_code.values():
        ordered = sorted(rows, key=lambda row: (row.known_at, row.sector_id))
        before = tuple(row for row in ordered if row.known_at <= start_at)
        if before:
            sectors.add(before[-1].sector_id)
        sectors.update(
            row.sector_id for row in ordered if start_at < row.known_at <= end_at
        )
    return tuple(sorted(sectors))


def sector_closure_codes(
    memberships: Sequence[SectorMembershipChange],
    *,
    requested_codes: Sequence[str],
    start: date,
    end: date,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Derive the historical SW1 member closure for a bounded stock request."""

    requested = tuple(sorted(set(requested_codes)))
    selected_sectors = relevant_sector_ids(
        memberships,
        codes=requested,
        start=start,
        end=end,
    )
    if not selected_sectors:
        raise ValueError("requested codes have no effective SW1 membership")
    rows_by_code: dict[str, list[SectorMembershipChange]] = {}
    for row in memberships:
        rows_by_code.setdefault(row.code, []).append(row)
    closure = tuple(
        code
        for code, rows in sorted(rows_by_code.items())
        if set(
            relevant_sector_ids(
                rows,
                codes=(code,),
                start=start,
                end=end,
            )
        ).intersection(selected_sectors)
    )
    missing = tuple(sorted(set(requested) - set(closure)))
    if missing:
        raise ValueError(
            "requested codes are absent from the historical sector closure: "
            + ",".join(missing)
        )
    return closure, selected_sectors


def scope_source_hashes(scope: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    """Bind the human-readable scope proof into immutable snapshot hashes."""

    return tuple(
        sorted(
            (
                ("pit_scope_contract", sha256_json(dict(scope))),
                (
                    "pit_scope_requested_codes",
                    sha256_json(list(scope.get("requested_codes") or ())),
                ),
                (
                    "pit_scope_sector_closure_codes",
                    sha256_json(list(scope.get("closure_codes") or ())),
                ),
                (
                    "pit_scope_sector_closure_candidate_codes",
                    sha256_json(list(scope.get("closure_candidate_codes") or ())),
                ),
                (
                    "pit_scope_selected_sector_ids",
                    sha256_json(list(scope.get("selected_sector_ids") or ())),
                ),
                (
                    "pit_scope_excluded_identity_codes",
                    sha256_json(list(scope.get("excluded_identity_codes") or ())),
                ),
                (
                    "pit_scope_certified_outside_range_intervals",
                    sha256_json(
                        list(scope.get("certified_outside_range_intervals") or ())
                    ),
                ),
                (
                    "pit_scope_detail_read_codes",
                    sha256_json(list(scope.get("detail_read_codes") or ())),
                ),
            )
        )
    )


def validate_scope_proof(
    *,
    snapshot: PITMetadataSnapshot,
    scope: Mapping[str, object],
    replay_codes: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Return deterministic failures; an empty tuple certifies the scope."""

    failures: list[str] = []
    mode = str(scope.get("mode") or "")
    requested = tuple(str(value) for value in scope.get("requested_codes") or ())
    closure = tuple(str(value) for value in scope.get("closure_codes") or ())
    candidates = tuple(
        str(value) for value in scope.get("closure_candidate_codes") or ()
    )
    sectors = tuple(str(value) for value in scope.get("selected_sector_ids") or ())
    excluded = tuple(
        str(value) for value in scope.get("excluded_closure_candidate_codes") or ()
    )
    excluded_identities = tuple(
        str(value) for value in scope.get("excluded_identity_codes") or ()
    )
    checkpoint_absent = tuple(
        str(value)
        for value in scope.get("checkpoint_absent_identity_codes") or ()
    )
    unresolved_checkpoint_absent = tuple(
        str(value)
        for value in scope.get("uncertified_checkpoint_absent_identity_codes")
        or ()
    )
    raw_outside_intervals = tuple(
        value
        for value in scope.get("certified_outside_range_intervals") or ()
        if isinstance(value, Mapping)
    )
    detail_read_codes = tuple(
        str(value) for value in scope.get("detail_read_codes") or ()
    )
    if scope.get("schema") != PIT_SCOPE_SCHEMA:
        failures.append("pit_scope_schema_missing")
    if mode not in {SCOPED_SECTOR_CLOSURE_MODE, FULL_MARKET_MODE}:
        failures.append("pit_scope_mode_invalid")
    if requested != tuple(sorted(set(requested))) or not requested:
        failures.append("pit_requested_codes_invalid")
    if closure != tuple(sorted(set(closure))) or not closure:
        failures.append("pit_sector_closure_codes_invalid")
    if candidates != tuple(sorted(set(candidates))) or not candidates:
        failures.append("pit_sector_closure_candidates_invalid")
    if not set(closure).issubset(candidates):
        failures.append("pit_sector_closure_outside_candidates")
    if excluded != tuple(sorted(set(candidates) - set(closure))):
        failures.append("pit_sector_closure_exclusions_invalid")
    if sectors != tuple(sorted(set(sectors))) or not sectors:
        failures.append("pit_selected_sector_ids_invalid")
    if not set(requested).issubset(closure):
        failures.append("pit_requested_codes_outside_closure")
    if int(scope.get("requested_code_count") or -1) != len(requested):
        failures.append("pit_requested_code_count_mismatch")
    if int(scope.get("closure_code_count") or -1) != len(closure):
        failures.append("pit_sector_closure_count_mismatch")
    if int(scope.get("closure_candidate_code_count") or -1) != len(candidates):
        failures.append("pit_sector_closure_candidate_count_mismatch")
    if int(scope.get("selected_sector_count") or -1) != len(sectors):
        failures.append("pit_selected_sector_count_mismatch")
    snapshot_codes = tuple(row.code for row in snapshot.securities)
    if closure != snapshot_codes:
        failures.append("pit_snapshot_security_closure_mismatch")
    if scope.get("sector_closure_complete") is not True:
        failures.append("pit_sector_closure_not_certified")
    if tuple(scope.get("missing_checkpoint_codes") or ()) or unresolved_checkpoint_absent:
        failures.append("pit_membership_checkpoint_gap")
    checkpoint_count = int(scope.get("membership_checkpoint_count") or -1)
    inventory_count = int(scope.get("enumerated_contract_code_count") or -2)
    if mode == SCOPED_SECTOR_CLOSURE_MODE:
        if checkpoint_absent != excluded_identities:
            failures.append("pit_checkpoint_absent_identity_proof_mismatch")
        if excluded_identities != tuple(sorted(set(excluded_identities))):
            failures.append("pit_excluded_identity_codes_invalid")
        if int(scope.get("excluded_identity_count", -1)) != len(
            excluded_identities
        ):
            failures.append("pit_excluded_identity_count_mismatch")
        if int(scope.get("certified_outside_range_identity_count", -1)) != len(
            excluded_identities
        ):
            failures.append("pit_outside_range_identity_count_mismatch")
        if checkpoint_count + len(excluded_identities) != inventory_count:
            failures.append("pit_membership_checkpoint_inventory_incomplete")
    elif checkpoint_count != inventory_count:
        failures.append("pit_membership_checkpoint_inventory_incomplete")
    detail_count = int(scope.get("detail_read_code_count") or -1)
    expected_detail_codes = tuple(sorted(set(candidates) | set(excluded_identities)))
    if (
        mode == SCOPED_SECTOR_CLOSURE_MODE
        and (
            detail_count != len(expected_detail_codes)
            or detail_read_codes != expected_detail_codes
        )
    ) or (
        mode == FULL_MARKET_MODE and detail_count < len(closure)
    ):
        failures.append("pit_security_detail_scope_mismatch")
    if int(scope.get("factor_read_code_count") or -1) != len(closure):
        failures.append("pit_factor_scope_mismatch")
    hashes = dict(snapshot.source_hashes)
    if hashes.get("qmt_a_share_contract_inventory") != scope.get(
        "enumerated_contract_codes_sha256"
    ):
        failures.append("pit_contract_inventory_hash_mismatch")
    checkpoint_hash_name = (
        "cninfo_membership_universe_checkpoint_tree"
        if mode == SCOPED_SECTOR_CLOSURE_MODE
        else "cninfo_membership_checkpoint_tree"
    )
    if hashes.get(checkpoint_hash_name) != scope.get(
        "membership_checkpoint_tree_sha256"
    ):
        failures.append("pit_membership_checkpoint_tree_hash_mismatch")
    for name, digest in scope_source_hashes(scope):
        if hashes.get(name) != digest:
            failures.append(f"{name}_mismatch")
    if mode == SCOPED_SECTOR_CLOSURE_MODE:
        outside_codes = tuple(
            str(row.get("code") or "") for row in raw_outside_intervals
        )
        if outside_codes != excluded_identities:
            failures.append("pit_outside_range_interval_identity_mismatch")
        expected_outside_hash = sha256_json(list(raw_outside_intervals))
        if scope.get("certified_outside_range_intervals_sha256") != (
            expected_outside_hash
        ):
            failures.append("pit_outside_range_interval_hash_mismatch")
        for row in raw_outside_intervals:
            try:
                code = str(row["code"])
                market, digits = code.split(".", 1)
                if row.get("native_code") != f"{digits}.{market}":
                    raise ValueError
                raw_dates = row["raw_date_fields"]
                if not isinstance(raw_dates, Mapping):
                    raise ValueError
                raw_open = str(raw_dates["OpenDate"])
                raw_expiry = str(raw_dates["ExpireDate"])
                raw_create = str(raw_dates["CreateDate"])
            except (KeyError, TypeError, ValueError):
                failures.append("pit_outside_range_interval_invalid")
                continue
            relation = str(row.get("relation") or "")
            proof_basis = str(row.get("proof_basis") or "")
            try:
                if proof_basis == "EXPIRE_DATE_BEFORE_REPLAY":
                    listed_through = date.fromisoformat(str(row["listed_through"]))
                    if (
                        row.get("listed_from") is not None
                        or row.get("created_on") is not None
                        or relation != "BEFORE_REPLAY_RANGE"
                        or listed_through >= snapshot.source_start
                        or raw_expiry != listed_through.strftime("%Y%m%d")
                    ):
                        raise ValueError
                elif (
                    proof_basis
                    == "CREATE_DATE_AFTER_REPLAY_WITH_OPEN_PLACEHOLDER"
                ):
                    created_on = date.fromisoformat(str(row["created_on"]))
                    if (
                        row.get("listed_from") is not None
                        or row.get("listed_through") is not None
                        or relation != "AFTER_REPLAY_RANGE"
                        or created_on <= snapshot.source_end
                        or raw_open not in {"0", "19700101"}
                        or raw_create != created_on.strftime("%Y%m%d")
                    ):
                        raise ValueError
                elif proof_basis == "OPEN_DATE_AFTER_REPLAY":
                    listed_from = date.fromisoformat(str(row["listed_from"]))
                    if (
                        row.get("listed_through") is not None
                        or row.get("created_on") is not None
                        or relation != "AFTER_REPLAY_RANGE"
                        or listed_from <= snapshot.source_end
                        or raw_open != listed_from.strftime("%Y%m%d")
                    ):
                        raise ValueError
                elif proof_basis == "STRICT_LISTING_INTERVAL_OUTSIDE_REPLAY":
                    listed_from = date.fromisoformat(str(row["listed_from"]))
                    raw_through = row.get("listed_through")
                    listed_through = (
                        None
                        if raw_through is None
                        else date.fromisoformat(str(raw_through))
                    )
                    if (
                        row.get("created_on") is not None
                        or raw_open != listed_from.strftime("%Y%m%d")
                        or (
                            listed_through is None
                            and raw_expiry not in {"", "0", "99999999"}
                        )
                        or (
                            listed_through is not None
                            and raw_expiry != listed_through.strftime("%Y%m%d")
                        )
                        or (
                            listed_through is not None
                            and listed_through < listed_from
                        )
                    ):
                        raise ValueError
                    if relation == "BEFORE_REPLAY_RANGE":
                        if (
                            listed_through is None
                            or listed_through >= snapshot.source_start
                        ):
                            raise ValueError
                    elif relation == "AFTER_REPLAY_RANGE":
                        if listed_from <= snapshot.source_end:
                            raise ValueError
                    else:
                        raise ValueError
                else:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                failures.append("pit_outside_range_interval_invalid")
    recomputed_sectors = relevant_sector_ids(
        snapshot.memberships,
        codes=requested,
        start=snapshot.source_start,
        end=snapshot.source_end,
    )
    if sectors != recomputed_sectors:
        failures.append("pit_selected_sector_history_mismatch")
    if mode == SCOPED_SECTOR_CLOSURE_MODE:
        try:
            recomputed_closure, _sector_ids = sector_closure_codes(
                snapshot.memberships,
                requested_codes=requested,
                start=snapshot.source_start,
                end=snapshot.source_end,
            )
        except ValueError:
            failures.append("pit_snapshot_sector_closure_unavailable")
        else:
            if closure != recomputed_closure:
                failures.append("pit_snapshot_sector_closure_membership_mismatch")
    if replay_codes is not None and mode == SCOPED_SECTOR_CLOSURE_MODE:
        if tuple(sorted(set(replay_codes))) != requested:
            failures.append("pit_requested_replay_scope_mismatch")
    if mode == FULL_MARKET_MODE and scope.get("large_scope_confirmed") is not True:
        failures.append("pit_full_market_confirmation_missing")
    return tuple(dict.fromkeys(failures))


__all__ = (
    "FULL_MARKET_MODE",
    "MAX_UNCONFIRMED_REQUESTED_CODES",
    "PIT_SCOPE_SCHEMA",
    "SCOPED_SECTOR_CLOSURE_MODE",
    "relevant_sector_ids",
    "scope_source_hashes",
    "sector_closure_codes",
    "validate_scope_proof",
)
