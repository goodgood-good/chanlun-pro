#!/usr/bin/env python3
"""Build or idempotently verify the bounded strict strategy sector release manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.research_release.sector_release_manifest import (  # noqa: E402
    SectorReleaseManifestError,
    build_sector_release_manifest,
    sha256_file,
    verify_sector_release_manifest,
)


FORMAL_ROOT = Path(
    "audit/chanlun_trading_system_backtest/"
    "recent_year_current_sector_no3p_mwd_strength"
)
CURRENT_INPUT_ROOT = Path(
    "audit/chanlun_trading_system_backtest/"
    "recent_year_current_sector_no3p"
)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=PROJECT_ROOT)
    value.add_argument(
        "--artifact",
        type=Path,
        default=(
            FORMAL_ROOT
            / "approximate_technical_backtest_"
            "sector_mwd_strength_tactical_lifecycle.json"
        ),
    )
    value.add_argument(
        "--trigger-ledger",
        type=Path,
        default=CURRENT_INPUT_ROOT / "sector_first_trigger_ledger.pkl",
    )
    value.add_argument(
        "--terminal-query-plan",
        type=Path,
        default=CURRENT_INPUT_ROOT / "terminal_query_plan.json",
    )
    value.add_argument(
        "--direct-manifest",
        type=Path,
        default=CURRENT_INPUT_ROOT / "direct_extract_manifest.json",
    )
    value.add_argument(
        "--pit-snapshot",
        type=Path,
        default=Path(
            "audit/chanlun_trading_system_backtest/"
            "fixed_year_2025_2026/pit_metadata.json"
        ),
    )
    value.add_argument(
        "--catalog-ledger",
        type=Path,
        default=Path(
            ".cache/chanlun_qmt_sector_ledger/"
            "qmt_gics3_catalog_ledger.json"
        ),
    )
    value.add_argument(
        "--catalog-archive-dir",
        type=Path,
        default=FORMAL_ROOT / "release_inputs",
        help=(
            "content-addressed publication directory for the exact catalog "
            "ledger bytes consumed by the replay"
        ),
    )
    value.add_argument(
        "--bundle-inputs",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=(
            "freeze trigger/query/direct checkpoints/PIT/catalog into one "
            "content-addressed directory under --catalog-archive-dir"
        ),
    )
    value.add_argument(
        "--output",
        type=Path,
        default=FORMAL_ROOT / "release_manifest.json",
    )
    value.add_argument(
        "--replace-existing",
        action="store_true",
        help=(
            "replace a verified existing manifest only when every identity "
            "is unchanged and only bound file paths differ; archive old bytes"
        ),
    )
    return value


def _resolve(root: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _atomic_bytes(path: Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_bytes(value)
        if temporary.read_bytes() != value:
            raise SectorReleaseManifestError("temporary release bytes changed")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _freeze_catalog_copy(*, root: Path, source: Path, directory: Path) -> Path:
    source = source.resolve(strict=True)
    source.relative_to(root)
    payload = source.read_bytes()
    digest = _sha256_bytes(payload)
    directory = directory.resolve()
    directory.relative_to(root)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (
        "qmt_gics3_catalog_ledger."
        + digest.removeprefix("sha256:")
        + ".json"
    )
    if target.exists():
        if not target.is_file() or target.read_bytes() != payload:
            raise SectorReleaseManifestError(
                "content-addressed catalog copy has different bytes"
            )
    else:
        _atomic_bytes(target, payload)
    if sha256_file(target) != digest:
        raise SectorReleaseManifestError("frozen catalog hash changed")
    return target


def _strict_json(path: Path) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise SectorReleaseManifestError(f"duplicate JSON key: {key}")
            output[key] = value
        return output

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                SectorReleaseManifestError(f"non-finite JSON constant: {raw}")
            ),
        )
    except SectorReleaseManifestError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SectorReleaseManifestError(f"invalid JSON document: {path}") from exc
    if not isinstance(value, dict):
        raise SectorReleaseManifestError(f"JSON document must be an object: {path}")
    return value


def _freeze_named_copy(source: Path, target: Path) -> None:
    payload = source.read_bytes()
    if target.exists():
        if not target.is_file() or target.read_bytes() != payload:
            raise SectorReleaseManifestError(
                f"immutable release input has different bytes: {target}"
            )
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_bytes(target, payload)


def _direct_checkpoint_source(directory: Path, value: object, code: str) -> Path:
    if not isinstance(value, str) or not value:
        raise SectorReleaseManifestError(f"direct checkpoint path is invalid: {code}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise SectorReleaseManifestError(
            f"direct checkpoint path is not canonical: {code}"
        )
    try:
        resolved = directory.joinpath(*pure.parts).resolve(strict=True)
        canonical = resolved.relative_to(directory.resolve(strict=True)).as_posix()
    except (OSError, ValueError) as exc:
        raise SectorReleaseManifestError(
            f"direct checkpoint escapes or is missing: {code}"
        ) from exc
    if canonical != value or not resolved.is_file():
        raise SectorReleaseManifestError(
            f"direct checkpoint path is not canonical: {code}"
        )
    return resolved


def _freeze_release_bundle(
    *,
    root: Path,
    directory: Path,
    trigger: Path,
    query: Path,
    direct_manifest: Path,
    pit: Path,
    catalog: Path,
) -> tuple[Path, dict[str, Path], str]:
    sources = {
        "trigger_ledger": trigger.resolve(strict=True),
        "terminal_query_plan": query.resolve(strict=True),
        "direct_manifest": direct_manifest.resolve(strict=True),
        "pit_snapshot": pit.resolve(strict=True),
        "current_catalog_ledger": catalog.resolve(strict=True),
    }
    for path in sources.values():
        path.relative_to(root)
        if not path.is_file():
            raise SectorReleaseManifestError(f"release source is not a file: {path}")
    identities = {key: sha256_file(path) for key, path in sources.items()}
    bundle_sha256 = _sha256_bytes(
        json.dumps(
            identities,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    directory = directory.resolve()
    directory.relative_to(root)
    bundle = directory / ("bundle." + bundle_sha256.removeprefix("sha256:"))
    targets = {
        "trigger_ledger": bundle / "sector_first_trigger_ledger.pkl",
        "terminal_query_plan": bundle / "terminal_query_plan.json",
        "direct_manifest": bundle / "direct_extract_manifest.json",
        "pit_snapshot": bundle / "pit_metadata.json",
        "current_catalog_ledger": bundle / "qmt_gics3_catalog_ledger.json",
    }
    for key, source in sources.items():
        _freeze_named_copy(source, targets[key])

    direct = _strict_json(sources["direct_manifest"])
    symbols = direct.get("symbols")
    if not isinstance(symbols, dict) or not symbols:
        raise SectorReleaseManifestError("direct manifest symbols are unavailable")
    for code, raw in symbols.items():
        if not isinstance(raw, dict):
            raise SectorReleaseManifestError(f"direct symbol is invalid: {code}")
        relative = raw.get("checkpoint_path")
        source = _direct_checkpoint_source(
            sources["direct_manifest"].parent, relative, str(code)
        )
        size = raw.get("checkpoint_size_bytes")
        if type(size) is not int or size <= 0 or source.stat().st_size != size:
            raise SectorReleaseManifestError(f"direct checkpoint size changed: {code}")
        if sha256_file(source) != raw.get("checkpoint_sha256"):
            raise SectorReleaseManifestError(f"direct checkpoint hash changed: {code}")
        assert isinstance(relative, str)
        target = targets["direct_manifest"].parent.joinpath(
            *PurePosixPath(relative).parts
        )
        _freeze_named_copy(source, target)
    return bundle, targets, bundle_sha256


def _release_identity(document: dict[str, object]) -> dict[str, object]:
    artifact = document.get("artifact")
    upstream = document.get("upstream")
    if not isinstance(artifact, dict) or not isinstance(upstream, dict):
        raise SectorReleaseManifestError("release manifest identity is unavailable")
    upstream_hashes: dict[str, object] = {}
    for key, raw in upstream.items():
        if not isinstance(raw, dict):
            raise SectorReleaseManifestError("release upstream identity is invalid")
        upstream_hashes[str(key)] = raw.get("file_sha256")
    return {
        "schema": document.get("schema"),
        "artifact": {
            key: artifact.get(key)
            for key in ("schema", "file_sha256", "content_sha256")
        },
        "upstream_file_sha256s": upstream_hashes,
        "current_catalog_entry_sha256": document.get(
            "current_catalog_entry_sha256"
        ),
        "algorithm": document.get("algorithm"),
        "highest_status": document.get("highest_status"),
        "live_status": document.get("live_status"),
        "immutable_inputs_only": document.get("immutable_inputs_only"),
    }


def _archive_previous_manifest(output: Path, previous: bytes) -> Path:
    digest = _sha256_bytes(previous)
    directory = output.parent / "release_manifest_history"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / f"{digest.removeprefix('sha256:')}.json"
    if archive.exists():
        if archive.read_bytes() != previous:
            raise SectorReleaseManifestError("release history hash collision")
    else:
        _atomic_bytes(archive, previous)
    if sha256_file(archive) != digest:
        raise SectorReleaseManifestError("release history archive changed")
    return archive


def run(args: argparse.Namespace) -> dict[str, object]:
    root = args.root.resolve(strict=True)
    output = _resolve(root, args.output)
    output.parent.resolve(strict=True).relative_to(root)
    artifact = _resolve(root, args.artifact)
    if output.is_file() and not args.replace_existing:
        # Verification of an immutable publication must not consult today's
        # append-only QMT catalog.  Otherwise a normal forward capture makes
        # this supposedly idempotent command attempt to rebuild history from
        # newer bytes.  An explicit replacement is the only refresh path.
        receipt = verify_sector_release_manifest(
            root=root,
            manifest_path=output,
            expected_artifact_path=artifact,
        )
        bound_files = receipt.get("bound_files")
        if not isinstance(bound_files, dict):
            raise SectorReleaseManifestError("release bound files are unavailable")
        catalog = bound_files.get("current_catalog_ledger")
        if not isinstance(catalog, dict):
            raise SectorReleaseManifestError("release catalog binding is unavailable")
        return {
            "disposition": "VERIFIED_EXISTING_IMMUTABLE_RELEASE",
            "catalog_source_path": None,
            "catalog_release_path": catalog.get("path"),
            "catalog_release_sha256": catalog.get("file_sha256"),
            "archived_previous_manifest": None,
            **receipt,
        }
    source_catalog = _resolve(root, args.catalog_ledger)
    if args.bundle_inputs:
        bundle, frozen, bundle_sha256 = _freeze_release_bundle(
            root=root,
            directory=_resolve(root, args.catalog_archive_dir),
            trigger=_resolve(root, args.trigger_ledger),
            query=_resolve(root, args.terminal_query_plan),
            direct_manifest=_resolve(root, args.direct_manifest),
            pit=_resolve(root, args.pit_snapshot),
            catalog=source_catalog,
        )
        frozen_catalog = frozen["current_catalog_ledger"]
        trigger_path = frozen["trigger_ledger"]
        query_path = frozen["terminal_query_plan"]
        direct_path = frozen["direct_manifest"]
        pit_path = frozen["pit_snapshot"]
    else:
        bundle = None
        bundle_sha256 = None
        frozen_catalog = _freeze_catalog_copy(
            root=root,
            source=source_catalog,
            directory=_resolve(root, args.catalog_archive_dir),
        )
        trigger_path = _resolve(root, args.trigger_ledger)
        query_path = _resolve(root, args.terminal_query_plan)
        direct_path = _resolve(root, args.direct_manifest)
        pit_path = _resolve(root, args.pit_snapshot)
    document = build_sector_release_manifest(
        root=root,
        artifact_path=artifact,
        trigger_ledger_path=trigger_path,
        terminal_query_plan_path=query_path,
        direct_manifest_path=direct_path,
        pit_snapshot_path=pit_path,
        catalog_ledger_path=frozen_catalog,
    )
    encoded = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    archived_previous: Path | None = None
    if output.exists():
        previous = output.read_bytes()
        if previous != encoded:
            if not args.replace_existing:
                raise SectorReleaseManifestError(
                    "release manifest already exists with different bytes"
                )
            verify_sector_release_manifest(
                root=root,
                manifest_path=output,
                expected_artifact_path=artifact,
            )
            try:
                previous_document = json.loads(previous.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise SectorReleaseManifestError(
                    "existing release manifest is invalid"
                ) from exc
            if not isinstance(previous_document, dict) or _release_identity(
                previous_document
            ) != _release_identity(document):
                raise SectorReleaseManifestError(
                    "existing release manifest differs beyond bound paths"
                )
            archived_previous = _archive_previous_manifest(output, previous)
            _atomic_bytes(output, encoded)
            disposition = "REPLACED_EQUIVALENT_PATH_BINDING"
        else:
            disposition = "UNCHANGED_IDENTICAL"
    else:
        _atomic_bytes(output, encoded)
        disposition = "CREATED"
    receipt = verify_sector_release_manifest(
        root=root,
        manifest_path=output,
        expected_artifact_path=artifact,
    )
    return {
        "disposition": disposition,
        "catalog_source_path": source_catalog.relative_to(root).as_posix(),
        "catalog_release_path": frozen_catalog.relative_to(root).as_posix(),
        "catalog_release_sha256": sha256_file(frozen_catalog),
        "release_bundle_path": (
            None if bundle is None else bundle.relative_to(root).as_posix()
        ),
        "release_bundle_identity": bundle_sha256,
        "archived_previous_manifest": (
            None
            if archived_previous is None
            else archived_previous.relative_to(root).as_posix()
        ),
        **receipt,
    }


def main(argv: list[str] | None = None) -> int:
    try:
        receipt = run(parser().parse_args(argv))
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED_CLOSED",
                    "reason": str(exc),
                    "live_status": "LIVE_DISABLED",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
