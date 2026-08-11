"""Safely promote one verified strict strategy sector candidate to the stable release.

The large research artifact and its small release manifest form one logical
publication.  Replacing only one of them leaves a broken release graph.  This
tool therefore verifies both the old and candidate graphs, requires exact
economic equivalence for a source-provenance-only refresh, archives the old
bytes, performs same-directory atomic replacements, and rolls both files back
if post-publication verification fails.

It never reads an account, writes QMT data, enables live trading, or sends an
order.  Dry-run is the default; execution requires the candidate's exact
content SHA256 as an explicit confirmation value.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tempfile
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.trading_system.decision_source_provenance import (  # noqa: E402
    replay_decision_source_snapshot_matches_current,
)
from chanlun.research_release.sector_release_manifest import (  # noqa: E402
    SectorReleaseManifestError,
    build_sector_release_manifest,
    verify_sector_release_manifest,
)


FORMAL_ROOT = Path(
    "audit/chanlun_trading_system_backtest/"
    "recent_year_current_sector_no3p_mwd_strength"
)
STABLE_ARTIFACT = FORMAL_ROOT / (
    "approximate_technical_backtest_"
    "sector_mwd_strength_tactical_lifecycle.json"
)
STABLE_MANIFEST = FORMAL_ROOT / "release_manifest.json"
SCHEMA = "chanlun-sector-release-promotion"


class PromotionError(RuntimeError):
    """Raised when a candidate cannot be promoted without ambiguity."""


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=PROJECT_ROOT)
    value.add_argument("--candidate-artifact", type=Path, required=True)
    value.add_argument("--candidate-manifest", type=Path, required=True)
    value.add_argument("--target-artifact", type=Path, default=STABLE_ARTIFACT)
    value.add_argument("--target-manifest", type=Path, default=STABLE_MANIFEST)
    value.add_argument(
        "--execute",
        action="store_true",
        help="archive and replace the stable publication; default is dry-run",
    )
    value.add_argument(
        "--confirm-content-sha256",
        help="exact candidate content SHA256; mandatory with --execute",
    )
    return value


def _inside(root: Path, value: Path, *, must_exist: bool) -> Path:
    candidate = value if value.is_absolute() else root / value
    try:
        resolved = candidate.resolve(strict=must_exist)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise PromotionError(f"release path is missing or escapes project root: {value}") from exc
    if must_exist and not resolved.is_file():
        raise PromotionError(f"release path is not a file: {value}")
    return resolved


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, item in pairs:
            if key in output:
                raise PromotionError(f"duplicate JSON key in {path}: {key}")
            output[key] = item
        return output

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                PromotionError(f"non-finite JSON constant in {path}: {raw}")
            ),
        )
    except PromotionError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PromotionError(f"cannot read strict JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PromotionError(f"JSON document must be an object: {path}")
    return value


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PromotionError(f"{label} must be a mapping")
    return value


def _economic_projection(value: Mapping[str, object]) -> dict[str, object]:
    """Remove only source-provenance fields from a research document."""

    output = dict(value)
    output.pop("content_sha256", None)
    output.pop("decision_source_snapshot", None)
    return output


def _bound_path(root: Path, value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise PromotionError(f"candidate {label}.path is invalid")
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "\\" in value:
        raise PromotionError(f"candidate {label}.path is not canonical")
    return _inside(root, Path(*pure.parts), must_exist=True)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _archive(path: Path, payload: bytes, directory: str) -> Path:
    digest = hashlib.sha256(payload).hexdigest()
    archive = path.parent / directory / f"{digest}.json"
    if archive.exists():
        if archive.read_bytes() != payload:
            raise PromotionError(f"release archive collision: {archive}")
    else:
        _atomic_bytes(archive, payload)
    if archive.read_bytes() != payload:
        raise PromotionError(f"release archive verification failed: {archive}")
    return archive


def _manifest_bytes(document: Mapping[str, object]) -> bytes:
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def run(args: argparse.Namespace) -> dict[str, object]:
    root = args.root.resolve(strict=True)
    candidate_artifact = _inside(root, args.candidate_artifact, must_exist=True)
    candidate_manifest = _inside(root, args.candidate_manifest, must_exist=True)
    target_artifact = _inside(root, args.target_artifact, must_exist=True)
    target_manifest = _inside(root, args.target_manifest, must_exist=True)
    if candidate_artifact in {target_artifact, target_manifest}:
        raise PromotionError("candidate artifact must be separate from stable files")
    if candidate_manifest in {target_artifact, target_manifest}:
        raise PromotionError("candidate manifest must be separate from stable files")

    candidate_receipt = verify_sector_release_manifest(
        root=root,
        manifest_path=candidate_manifest,
        expected_artifact_path=candidate_artifact,
    )
    stable_receipt = verify_sector_release_manifest(
        root=root,
        manifest_path=target_manifest,
        expected_artifact_path=target_artifact,
    )
    candidate_document = _strict_json(candidate_artifact)
    stable_document = _strict_json(target_artifact)
    if _economic_projection(candidate_document) != _economic_projection(
        stable_document
    ):
        raise PromotionError(
            "candidate changes decisions or economic results; "
            "source-only promotion refused"
        )
    snapshot = _mapping(
        candidate_document.get("decision_source_snapshot"),
        "candidate decision_source_snapshot",
    )
    if not replay_decision_source_snapshot_matches_current(snapshot, root):
        raise PromotionError("candidate decision sources are not current")
    candidate_content = candidate_receipt["artifact_content_sha256"]
    if args.execute and args.confirm_content_sha256 != candidate_content:
        raise PromotionError(
            "--confirm-content-sha256 must equal the verified candidate content hash"
        )

    receipt: dict[str, object] = {
        "schema": SCHEMA,
        "disposition": "READY_DRY_RUN",
        "candidate_content_sha256": candidate_content,
        "candidate_file_sha256": candidate_receipt["artifact_file_sha256"],
        "previous_content_sha256": stable_receipt["artifact_content_sha256"],
        "economic_projection_unchanged": True,
        "decision_source_matches_current": True,
        "parameters_changed": False,
        "old_artifact_archive": None,
        "old_manifest_archive": None,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    if not args.execute:
        return receipt

    previous_artifact = target_artifact.read_bytes()
    previous_manifest = target_manifest.read_bytes()
    artifact_archive = _archive(
        target_artifact,
        previous_artifact,
        "release_artifact_history",
    )
    manifest_archive = _archive(
        target_manifest,
        previous_manifest,
        "release_manifest_history",
    )
    candidate_manifest_document = _strict_json(candidate_manifest)
    upstream = _mapping(candidate_manifest_document.get("upstream"), "upstream")
    bound = {
        key: _bound_path(root, _mapping(upstream.get(key), key).get("path"), key)
        for key in (
            "trigger_ledger",
            "terminal_query_plan",
            "direct_manifest",
            "pit_snapshot",
            "current_catalog_ledger",
        )
    }
    try:
        _atomic_bytes(target_artifact, candidate_artifact.read_bytes())
        document = build_sector_release_manifest(
            root=root,
            artifact_path=target_artifact,
            trigger_ledger_path=bound["trigger_ledger"],
            terminal_query_plan_path=bound["terminal_query_plan"],
            direct_manifest_path=bound["direct_manifest"],
            pit_snapshot_path=bound["pit_snapshot"],
            catalog_ledger_path=bound["current_catalog_ledger"],
        )
        _atomic_bytes(target_manifest, _manifest_bytes(document))
        published = verify_sector_release_manifest(
            root=root,
            manifest_path=target_manifest,
            expected_artifact_path=target_artifact,
        )
    except Exception:
        _atomic_bytes(target_artifact, previous_artifact)
        _atomic_bytes(target_manifest, previous_manifest)
        verify_sector_release_manifest(
            root=root,
            manifest_path=target_manifest,
            expected_artifact_path=target_artifact,
        )
        raise
    return {
        **receipt,
        "disposition": "PROMOTED_AND_VERIFIED",
        "published_artifact_file_sha256": published["artifact_file_sha256"],
        "published_manifest_file_sha256": published["manifest_file_sha256"],
        "old_artifact_archive": artifact_archive.relative_to(root).as_posix(),
        "old_manifest_archive": manifest_archive.relative_to(root).as_posix(),
    }


def main() -> int:
    try:
        result = run(parser().parse_args())
    except (OSError, PromotionError, SectorReleaseManifestError, ValueError) as exc:
        print(
            json.dumps(
                {
                    "schema": SCHEMA,
                    "status": "FAILED_CLOSED",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "live_status": "LIVE_DISABLED",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
