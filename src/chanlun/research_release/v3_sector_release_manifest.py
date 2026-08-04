"""Content-addressed publication binding for the V3 sector research result.

The large replay result records input hashes but intentionally does not carry
machine-local file paths.  A publication therefore needs one small, immutable
document that says which workspace files those hashes refer to.  This module
validates that graph without unpickling a checkpoint or touching QMT/account
state.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.v3_recent_year_provenance import (
    RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
    recent_year_research_algorithm_hashes,
    recent_year_research_algorithm_revision,
)


SECTOR_RELEASE_MANIFEST_SCHEMA = "chanlun-v3-sector-release-manifest/v1"
SECTOR_RESEARCH_ARTIFACT_SCHEMA = (
    "chanlun-v3-sector-first-full-market-research-backtest/v2"
)
TERMINAL_QUERY_PLAN_SCHEMA = "chanlun-v3-sector-first-terminal-query-plan/v2"
DIRECT_EXTRACT_MANIFEST_SCHEMA = "chanlun-v3-sector-first-direct-extract/v3"
CATALOG_LEDGER_SCHEMA = "chanlun-v3-qmt-sector-capture-ledger/v1"

_SHA_KEYS = (
    "trigger_ledger",
    "terminal_query_plan",
    "direct_manifest",
    "pit_snapshot",
    "current_catalog_ledger",
)
_MAX_JSON_BYTES = 128 * 1024 * 1024


class SectorReleaseManifestError(ValueError):
    """Raised when a published artifact loses its immutable input binding."""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise SectorReleaseManifestError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _load_json(path: Path) -> dict[str, object]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > _MAX_JSON_BYTES:
            raise SectorReleaseManifestError(f"invalid JSON file size: {path}")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                SectorReleaseManifestError(
                    f"non-finite JSON constant in {path}: {raw}"
                )
            ),
        )
    except SectorReleaseManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SectorReleaseManifestError(f"invalid JSON document: {path}") from exc
    if not isinstance(value, dict):
        raise SectorReleaseManifestError(f"JSON document must be an object: {path}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SectorReleaseManifestError(f"{label} must be a mapping")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SectorReleaseManifestError(f"{label} must be a sequence")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise SectorReleaseManifestError(f"cannot hash release input: {path}") from exc
    return "sha256:" + digest.hexdigest()


def _relative_file(root: Path, path: Path) -> tuple[Path, str]:
    root = root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root).as_posix()
    except ValueError as exc:
        raise SectorReleaseManifestError(
            f"release input escapes project root: {path}"
        ) from exc
    if not resolved.is_file():
        raise SectorReleaseManifestError(f"release input is not a file: {path}")
    return resolved, relative


def _bound_file(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SectorReleaseManifestError(f"{label}.path must be a relative path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise SectorReleaseManifestError(f"{label}.path is not canonical")
    candidate = root.joinpath(*pure.parts)
    resolved, relative = _relative_file(root, candidate)
    if relative != value:
        raise SectorReleaseManifestError(f"{label}.path is not canonical")
    return resolved


def _content_bound(document: Mapping[str, object], label: str) -> str:
    stable = dict(document)
    reported = stable.pop("content_sha256", None)
    actual = sha256_json(stable)
    if reported != actual:
        raise SectorReleaseManifestError(
            f"{label}.content_sha256 does not bind its document"
        )
    return actual


def _query_content_bound(document: Mapping[str, object]) -> str:
    """Verify the terminal-query producer's intentionally frozen encoding."""

    stable = dict(document)
    reported = stable.pop("content_sha256", None)
    actual = "sha256:" + hashlib.sha256(
        json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if reported != actual:
        raise SectorReleaseManifestError(
            "terminal query plan.content_sha256 does not bind its document"
        )
    return actual


def _algorithm_rows(value: object, label: str) -> tuple[tuple[str, str], ...]:
    rows: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in _sequence(value, label):
        row = _mapping(raw, f"{label} row")
        path = row.get("path")
        digest = row.get("sha256")
        if not isinstance(path, str) or not isinstance(digest, str):
            raise SectorReleaseManifestError(f"{label} row is invalid")
        if path in seen:
            raise SectorReleaseManifestError(f"{label} repeats {path}")
        seen.add(path)
        rows.append((path, digest))
    return tuple(rows)


def _current_algorithm(root: Path) -> tuple[tuple[tuple[str, str], ...], str]:
    hashes = recent_year_research_algorithm_hashes(root)
    return hashes, recent_year_research_algorithm_revision(hashes)


def _check_algorithm(
    *,
    scope: object,
    revision: object,
    hashes: object,
    expected_hashes: tuple[tuple[str, str], ...],
    expected_revision: str,
    label: str,
) -> None:
    if scope != RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE:
        raise SectorReleaseManifestError(f"{label} algorithm scope changed")
    if revision != expected_revision:
        raise SectorReleaseManifestError(f"{label} algorithm revision changed")
    if _algorithm_rows(hashes, f"{label} algorithm hashes") != expected_hashes:
        raise SectorReleaseManifestError(f"{label} algorithm hashes changed")


def _verify_catalog(
    catalog: Mapping[str, object], expected_entry_sha256: str
) -> None:
    if catalog.get("schema") != CATALOG_LEDGER_SCHEMA:
        raise SectorReleaseManifestError("catalog ledger schema changed")
    _content_bound(catalog, "catalog ledger")
    matches = [
        row
        for row in _sequence(catalog.get("entries"), "catalog entries")
        if _mapping(row, "catalog entry").get("entry_sha256")
        == expected_entry_sha256
    ]
    if len(matches) != 1:
        raise SectorReleaseManifestError(
            "current catalog entry is not uniquely present in the bound ledger"
        )


def _verify_query_plan(
    query: Mapping[str, object],
    *,
    expected_hashes: tuple[tuple[str, str], ...],
    expected_revision: str,
    trigger_sha256: str,
    catalog_ledger_sha256: str,
    catalog_entry_sha256: str,
) -> tuple[str, ...]:
    if query.get("schema") != TERMINAL_QUERY_PLAN_SCHEMA:
        raise SectorReleaseManifestError("terminal query plan schema changed")
    _query_content_bound(query)
    _check_algorithm(
        scope=query.get("algorithm_hash_scope"),
        revision=query.get("algorithm_revision"),
        hashes=query.get("algorithm_hashes"),
        expected_hashes=expected_hashes,
        expected_revision=expected_revision,
        label="terminal query plan",
    )
    if query.get("trigger_ledger_sha256") != trigger_sha256:
        raise SectorReleaseManifestError("query plan trigger ledger changed")
    if query.get("current_catalog_ledger_sha256") != catalog_ledger_sha256:
        raise SectorReleaseManifestError("query plan catalog ledger changed")
    if query.get("current_catalog_entry_sha256") != catalog_entry_sha256:
        raise SectorReleaseManifestError("query plan catalog entry changed")
    rows = _sequence(query.get("rows"), "terminal query rows")
    requested = query.get("requested_symbol_count")
    completed = query.get("completed_symbol_count")
    failed = query.get("failed_symbol_count")
    failures = _mapping(query.get("failures"), "terminal query failures")
    if (
        type(requested) is not int
        or requested <= 0
        or completed != requested
        or len(rows) != requested
        or failed != 0
        or failures
    ):
        raise SectorReleaseManifestError("terminal query plan is incomplete")
    potential = tuple(
        str(value)
        for value in _sequence(
            query.get("potential_symbols"), "terminal potential symbols"
        )
    )
    if len(potential) != query.get("potential_symbol_count") or len(set(potential)) != len(
        potential
    ):
        raise SectorReleaseManifestError("terminal potential-symbol set is invalid")
    computed = query.get("computed_symbol_count")
    pruned = query.get("conservative_superset_pruned_symbol_count")
    if (
        type(computed) is not int
        or type(pruned) is not int
        or computed + pruned != requested
        or computed != len(potential)
    ):
        raise SectorReleaseManifestError("terminal query coverage is incomplete")
    return potential


def _checkpoint_path(directory: Path, relative: object, label: str) -> Path:
    if not isinstance(relative, str) or not relative:
        raise SectorReleaseManifestError(f"{label} checkpoint path is invalid")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
        raise SectorReleaseManifestError(f"{label} checkpoint path is not canonical")
    try:
        resolved = directory.joinpath(*pure.parts).resolve(strict=True)
        canonical = resolved.relative_to(directory.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as exc:
        raise SectorReleaseManifestError(
            f"{label} checkpoint escapes or is missing"
        ) from exc
    if canonical != relative or not resolved.is_file():
        raise SectorReleaseManifestError(f"{label} checkpoint path is not canonical")
    return resolved


def _verify_direct_manifest(
    direct: Mapping[str, object],
    *,
    directory: Path,
    expected_hashes: tuple[tuple[str, str], ...],
    expected_revision: str,
    trigger_sha256: str,
    query_sha256: str,
    pit_sha256: str,
    catalog_ledger_sha256: str,
    catalog_entry_sha256: str,
    potential_symbols: tuple[str, ...],
) -> tuple[int, int]:
    if direct.get("schema") != DIRECT_EXTRACT_MANIFEST_SCHEMA:
        raise SectorReleaseManifestError("direct manifest must be immutable v3")
    if (
        direct.get("complete") is not True
        or _mapping(direct.get("failures"), "direct failures")
        or direct.get("highest_status") != "RESEARCH_ONLY"
        or direct.get("live_status") != "LIVE_DISABLED"
    ):
        raise SectorReleaseManifestError("direct extraction is not release-safe")
    algorithm = _mapping(direct.get("algorithm"), "direct algorithm")
    _check_algorithm(
        scope=algorithm.get("scope"),
        revision=algorithm.get("revision"),
        hashes=algorithm.get("hashes"),
        expected_hashes=expected_hashes,
        expected_revision=expected_revision,
        label="direct manifest",
    )
    inputs = _mapping(direct.get("inputs"), "direct inputs")
    expected_inputs = {
        "trigger_ledger_sha256": trigger_sha256,
        "query_plan_sha256": query_sha256,
        "pit_snapshot_sha256": pit_sha256,
        "current_catalog_ledger_sha256": catalog_ledger_sha256,
        "current_catalog_entry_sha256": catalog_entry_sha256,
        "sector_scope_sha256": catalog_entry_sha256,
    }
    for key, wanted in expected_inputs.items():
        if inputs.get(key) != wanted:
            raise SectorReleaseManifestError(f"direct input {key} changed")
    symbols = _mapping(direct.get("symbols"), "direct symbols")
    if set(symbols) != set(potential_symbols):
        raise SectorReleaseManifestError(
            "direct symbols do not equal terminal potential symbols"
        )
    summary = _mapping(direct.get("summary"), "direct summary")
    if (
        summary.get("selected_symbol_count") != len(symbols)
        or summary.get("completed_symbol_count") != len(symbols)
        or summary.get("failed_symbol_count") != 0
    ):
        raise SectorReleaseManifestError("direct extraction coverage is incomplete")
    checkpoint_bytes = 0
    for code, raw in symbols.items():
        row = _mapping(raw, f"direct symbol {code}")
        checkpoint = _checkpoint_path(
            directory, row.get("checkpoint_path"), f"direct symbol {code}"
        )
        size = row.get("checkpoint_size_bytes")
        if type(size) is not int or size <= 0 or checkpoint.stat().st_size != size:
            raise SectorReleaseManifestError(f"direct checkpoint size changed: {code}")
        if sha256_file(checkpoint) != row.get("checkpoint_sha256"):
            raise SectorReleaseManifestError(f"direct checkpoint hash changed: {code}")
        checkpoint_bytes += size
    return len(symbols), checkpoint_bytes


def build_sector_release_manifest(
    *,
    root: Path,
    artifact_path: Path,
    trigger_ledger_path: Path,
    terminal_query_plan_path: Path,
    direct_manifest_path: Path,
    pit_snapshot_path: Path,
    catalog_ledger_path: Path,
) -> dict[str, object]:
    """Build an idempotent manifest; no file is written by this function."""

    root = root.resolve(strict=True)
    resolved: dict[str, tuple[Path, str]] = {}
    for key, path in {
        "artifact": artifact_path,
        "trigger_ledger": trigger_ledger_path,
        "terminal_query_plan": terminal_query_plan_path,
        "direct_manifest": direct_manifest_path,
        "pit_snapshot": pit_snapshot_path,
        "current_catalog_ledger": catalog_ledger_path,
    }.items():
        resolved[key] = _relative_file(root, path)
    artifact = _load_json(resolved["artifact"][0])
    artifact_content = _content_bound(artifact, "artifact")
    input_hashes = _mapping(artifact.get("input_hashes"), "artifact input hashes")
    algorithm_hashes, algorithm_revision = _current_algorithm(root)
    stable: dict[str, object] = {
        "schema": SECTOR_RELEASE_MANIFEST_SCHEMA,
        "artifact": {
            "path": resolved["artifact"][1],
            "schema": artifact.get("schema"),
            "file_sha256": sha256_file(resolved["artifact"][0]),
            "content_sha256": artifact_content,
        },
        "upstream": {
            key: {
                "path": resolved[key][1],
                "file_sha256": sha256_file(resolved[key][0]),
            }
            for key in _SHA_KEYS
        },
        "current_catalog_entry_sha256": input_hashes.get(
            "current_catalog_entry"
        ),
        "algorithm": {
            "scope": RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
            "revision": algorithm_revision,
            "hash_count": len(algorithm_hashes),
        },
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "immutable_inputs_only": True,
    }
    return {**stable, "content_sha256": sha256_json(stable)}


def verify_sector_release_manifest(
    *,
    root: Path,
    manifest_path: Path,
    expected_artifact_path: Path | None = None,
    require_current_algorithm: bool = True,
) -> dict[str, object]:
    """Verify the complete publication graph without unpickling any input.

    ``require_current_algorithm=False`` is only for the bounded pre-release
    workflow after decision sources changed.  It verifies the already-published
    graph against its own immutable query/direct algorithm identity so the
    bound PIT/catalog inputs remain usable for one small QMT sample; it never
    promotes that old graph to a current release.
    """

    if type(require_current_algorithm) is not bool:
        raise TypeError("require_current_algorithm must be bool")

    root = root.resolve(strict=True)
    manifest_resolved, manifest_relative = _relative_file(root, manifest_path)
    manifest = _load_json(manifest_resolved)
    if manifest.get("schema") != SECTOR_RELEASE_MANIFEST_SCHEMA:
        raise SectorReleaseManifestError("release manifest schema changed")
    manifest_content = _content_bound(manifest, "release manifest")
    if (
        manifest.get("highest_status") != "RESEARCH_ONLY"
        or manifest.get("live_status") != "LIVE_DISABLED"
        or manifest.get("immutable_inputs_only") is not True
    ):
        raise SectorReleaseManifestError("release manifest safety status changed")

    artifact_binding = _mapping(manifest.get("artifact"), "release artifact")
    artifact_path = _bound_file(root, artifact_binding.get("path"), "artifact")
    if expected_artifact_path is not None:
        expected = expected_artifact_path.resolve(strict=True)
        if artifact_path != expected:
            raise SectorReleaseManifestError("release manifest binds another artifact")
    artifact_file_sha = sha256_file(artifact_path)
    if artifact_binding.get("file_sha256") != artifact_file_sha:
        raise SectorReleaseManifestError("published artifact file hash changed")
    artifact = _load_json(artifact_path)
    artifact_content = _content_bound(artifact, "artifact")
    if (
        artifact_binding.get("schema") != SECTOR_RESEARCH_ARTIFACT_SCHEMA
        or artifact.get("schema") != SECTOR_RESEARCH_ARTIFACT_SCHEMA
        or artifact_binding.get("content_sha256") != artifact_content
    ):
        raise SectorReleaseManifestError("published artifact identity changed")

    upstream = _mapping(manifest.get("upstream"), "release upstream")
    if set(upstream) != set(_SHA_KEYS):
        raise SectorReleaseManifestError("release upstream file set changed")
    bound_paths: dict[str, Path] = {}
    bound_hashes: dict[str, str] = {}
    for key in _SHA_KEYS:
        binding = _mapping(upstream.get(key), f"release upstream {key}")
        path = _bound_file(root, binding.get("path"), f"upstream.{key}")
        digest = sha256_file(path)
        if binding.get("file_sha256") != digest:
            raise SectorReleaseManifestError(f"release upstream hash changed: {key}")
        bound_paths[key] = path
        bound_hashes[key] = digest

    artifact_inputs = _mapping(artifact.get("input_hashes"), "artifact inputs")
    for key in _SHA_KEYS:
        if artifact_inputs.get(key) != bound_hashes[key]:
            raise SectorReleaseManifestError(f"artifact input hash changed: {key}")
    catalog_entry_sha = manifest.get("current_catalog_entry_sha256")
    if (
        not isinstance(catalog_entry_sha, str)
        or artifact_inputs.get("current_catalog_entry") != catalog_entry_sha
    ):
        raise SectorReleaseManifestError("artifact catalog entry changed")

    query = _load_json(bound_paths["terminal_query_plan"])
    current_hashes, current_revision = _current_algorithm(root)
    if require_current_algorithm:
        expected_hashes, expected_revision = current_hashes, current_revision
    else:
        if query.get("algorithm_hash_scope") != RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE:
            raise SectorReleaseManifestError(
                "published terminal query algorithm scope changed"
            )
        expected_hashes = _algorithm_rows(
            query.get("algorithm_hashes"),
            "published terminal query algorithm hashes",
        )
        expected_revision = query.get("algorithm_revision")
        if not isinstance(expected_revision, str):
            raise SectorReleaseManifestError(
                "published terminal query algorithm revision is invalid"
            )
    algorithm = _mapping(manifest.get("algorithm"), "release algorithm")
    if (
        algorithm.get("scope") != RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE
        or algorithm.get("revision") != expected_revision
        or algorithm.get("hash_count") != len(expected_hashes)
    ):
        raise SectorReleaseManifestError("release algorithm is not current")

    catalog = _load_json(bound_paths["current_catalog_ledger"])
    _verify_catalog(catalog, catalog_entry_sha)
    potential = _verify_query_plan(
        query,
        expected_hashes=expected_hashes,
        expected_revision=expected_revision,
        trigger_sha256=bound_hashes["trigger_ledger"],
        catalog_ledger_sha256=bound_hashes["current_catalog_ledger"],
        catalog_entry_sha256=catalog_entry_sha,
    )
    direct = _load_json(bound_paths["direct_manifest"])
    checkpoint_count, checkpoint_bytes = _verify_direct_manifest(
        direct,
        directory=bound_paths["direct_manifest"].parent,
        expected_hashes=expected_hashes,
        expected_revision=expected_revision,
        trigger_sha256=bound_hashes["trigger_ledger"],
        query_sha256=bound_hashes["terminal_query_plan"],
        pit_sha256=bound_hashes["pit_snapshot"],
        catalog_ledger_sha256=bound_hashes["current_catalog_ledger"],
        catalog_entry_sha256=catalog_entry_sha,
        potential_symbols=potential,
    )
    return {
        "schema": SECTOR_RELEASE_MANIFEST_SCHEMA,
        "manifest_path": manifest_relative,
        "manifest_file_sha256": sha256_file(manifest_resolved),
        "manifest_content_sha256": manifest_content,
        "artifact_file_sha256": artifact_file_sha,
        "artifact_content_sha256": artifact_content,
        "algorithm_revision": expected_revision,
        "algorithm_hash_count": len(expected_hashes),
        "algorithm_matches_current": (
            expected_revision == current_revision
            and expected_hashes == current_hashes
        ),
        "terminal_symbol_count": len(_sequence(query.get("rows"), "query rows")),
        "direct_symbol_count": checkpoint_count,
        "direct_checkpoint_count": checkpoint_count,
        "direct_checkpoint_bytes": checkpoint_bytes,
        "bound_files": {
            key: {
                "path": bound_paths[key].relative_to(root).as_posix(),
                "file_sha256": bound_hashes[key],
            }
            for key in _SHA_KEYS
        },
        "all_bound_files_verified": True,
        "checkpoint_payloads_unpickled": False,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }


__all__ = (
    "DIRECT_EXTRACT_MANIFEST_SCHEMA",
    "SECTOR_RELEASE_MANIFEST_SCHEMA",
    "SectorReleaseManifestError",
    "build_sector_release_manifest",
    "sha256_file",
    "verify_sector_release_manifest",
)
