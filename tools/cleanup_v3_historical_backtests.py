#!/usr/bin/env python3
"""Safely prune obsolete V3 backtest artifacts while preserving current release.

The command is deliberately dry-run by default.  Destructive execution requires
both the exact resolved backtest root and a fixed confirmation token.  Before
deleting anything it verifies the immutable release manifest, the chart archive,
and the operational files that must remain available at stable default paths.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
for value in (PROJECT_ROOT, SOURCE_ROOT):
    if str(value) not in sys.path:
        sys.path.insert(0, str(value))

from chanlun.decision_support.fingerprints import sha256_json  # noqa: E402
from chanlun.research_release.v3_sector_release_manifest import (  # noqa: E402
    sha256_file,
    verify_sector_release_manifest,
)


CN = ZoneInfo("Asia/Shanghai")
SCHEMA = "chanlun-v3-historical-backtest-cleanup/v1"
CONFIRMATION_TOKEN = "DELETE_V3_HISTORICAL_BACKTESTS"
BACKTEST_ROOT = Path("audit/chanlun_trading_system_backtest")
FORMAL_ROOT = BACKTEST_ROOT / "recent_year_current_sector_no3p_mwd_strength"
FORMAL_ARTIFACT = FORMAL_ROOT / (
    "approximate_technical_backtest_"
    "sector_mwd_strength_tactical_lifecycle.json"
)
FORMAL_MANIFEST = FORMAL_ROOT / "v3_release_manifest.json"
CHART_ARCHIVE_ROOT = FORMAL_ROOT / "sector_chart_evidence_archive"
CURRENT_INPUT_ROOT = BACKTEST_ROOT / "recent_year_current_sector_no3p"
PRESCREEN_INPUT_ROOT = BACKTEST_ROOT / "sector_first_full_market_v2"
PIT_SNAPSHOT = BACKTEST_ROOT / "fixed_year_2025_2026/pit_metadata.json"
HUMAN_REVIEW_FILES = (
    CURRENT_INPUT_ROOT / "parameter_snapshot_human_review.json",
    CURRENT_INPUT_ROOT / "human_review_screen.json",
)
OPTIONAL_CURRENT_HUMAN_REVIEW_FILES = (
    FORMAL_ROOT / "parameter_snapshot_human_review.json",
    FORMAL_ROOT / "human_review_screen.json",
)


class CleanupSafetyError(RuntimeError):
    """Raised when cleanup cannot prove its exact and bounded target set."""


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--root", type=Path, default=PROJECT_ROOT)
    value.add_argument(
        "--execute",
        action="store_true",
        help="perform promotion and deletion; default is a read-only dry-run",
    )
    value.add_argument(
        "--confirm-root",
        help="exact resolved backtest directory; mandatory with --execute",
    )
    value.add_argument(
        "--confirmation-token",
        help=f"must equal {CONFIRMATION_TOKEN!r} with --execute",
    )
    value.add_argument(
        "--report",
        type=Path,
        default=Path(
            "audit/chanlun_live_integration/"
            "history_cleanup_2026-08-02.json"
        ),
        help="execution audit report; dry-run never writes it",
    )
    value.add_argument(
        "--include-paths",
        action="store_true",
        help="include every planned deletion path in dry-run stdout",
    )
    return value


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, item in pairs:
            if key in output:
                raise CleanupSafetyError(f"duplicate JSON key in {path}: {key}")
            output[key] = item
        return output

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda raw: (_ for _ in ()).throw(
                CleanupSafetyError(f"non-finite JSON constant in {path}: {raw}")
            ),
        )
    except CleanupSafetyError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CleanupSafetyError(f"cannot read strict JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CleanupSafetyError(f"JSON document must be an object: {path}")
    return value


def _resolved_inside(root: Path, relative: Path, *, file: bool = True) -> Path:
    if relative.is_absolute():
        raise CleanupSafetyError(f"expected a relative repository path: {relative}")
    try:
        value = (root / relative).resolve(strict=True)
        value.relative_to(root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise CleanupSafetyError(
            f"required repository path is missing or escapes root: {relative}"
        ) from exc
    if file and not value.is_file():
        raise CleanupSafetyError(f"required path is not a file: {relative}")
    if not file and not value.is_dir():
        raise CleanupSafetyError(f"required path is not a directory: {relative}")
    return value


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _path_sha256(values: Sequence[str]) -> str:
    payload = "".join(f"{value}\n" for value in values).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _has_reparse_attribute(path: Path) -> bool:
    try:
        attributes = int(getattr(os.lstat(path), "st_file_attributes", 0))
    except OSError as exc:
        raise CleanupSafetyError(f"cannot inspect cleanup path: {path}") from exc
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(attributes & marker)


def _all_files_without_links(root: Path) -> tuple[Path, ...]:
    output: list[Path] = []
    for path in root.rglob("*"):
        if _has_reparse_attribute(path):
            raise CleanupSafetyError(
                f"cleanup tree contains a symlink or reparse point: {path}"
            )
        if path.is_file():
            output.append(path.resolve())
    return tuple(sorted(output, key=lambda item: _relative(root, item)))


def _validate_project_root(value: Path) -> tuple[Path, Path]:
    try:
        root = value.expanduser().resolve(strict=True)
    except OSError as exc:
        raise CleanupSafetyError(f"project root is unavailable: {value}") from exc
    required = (
        Path("pyproject.toml"),
        Path("audit/chanlun_live_strategy/complete_strategy_v3.md"),
        Path("audit/chanlun_lesson_corpus"),
    )
    for relative in required:
        _resolved_inside(root, relative, file=relative.suffix != "")
    backtest = _resolved_inside(root, BACKTEST_ROOT, file=False)
    if backtest != (root / BACKTEST_ROOT).resolve():
        raise CleanupSafetyError("backtest root identity changed")
    return root, backtest


def _chart_archive_closure(
    *, root: Path, artifact: Path, artifact_document: Mapping[str, Any]
) -> tuple[set[Path], dict[str, object]]:
    archive = _resolved_inside(root, CHART_ARCHIVE_ROOT, file=False)
    manifest = _resolved_inside(
        root, CHART_ARCHIVE_ROOT / "manifest.json", file=True
    )
    document = _strict_json(manifest)
    if document.get("schema") != "chanlun-v3-sector-chart-evidence-archive/v1":
        raise CleanupSafetyError("current sector chart archive schema changed")
    stable = dict(document)
    expected_content = stable.pop("content_sha256", None)
    if expected_content != sha256_json(stable):
        raise CleanupSafetyError("current sector chart archive content hash changed")
    source = document.get("source_artifact")
    if not isinstance(source, Mapping):
        raise CleanupSafetyError("sector chart archive source binding is missing")
    expected_source = {
        "relative_path": _relative(root, artifact),
        "file_sha256": sha256_file(artifact),
        "content_sha256": artifact_document.get("content_sha256"),
    }
    for key, expected in expected_source.items():
        if source.get(key) != expected:
            raise CleanupSafetyError(f"sector chart archive source changed: {key}")

    keep = {manifest}
    seen: set[str] = set()
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CleanupSafetyError("sector chart archive entries are missing")
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise CleanupSafetyError("sector chart archive entry is invalid")
        frames = entry.get("frames")
        if not isinstance(frames, Mapping) or not frames:
            raise CleanupSafetyError("sector chart archive frame map is invalid")
        for metadata in frames.values():
            if not isinstance(metadata, Mapping):
                raise CleanupSafetyError("sector chart frame metadata is invalid")
            raw = metadata.get("path")
            if not isinstance(raw, str) or not raw:
                raise CleanupSafetyError("sector chart frame path is invalid")
            pure = PurePosixPath(raw)
            if pure.is_absolute() or ".." in pure.parts or "\\" in raw:
                raise CleanupSafetyError(f"sector chart frame escapes archive: {raw}")
            try:
                frame = archive.joinpath(*pure.parts).resolve(strict=True)
                canonical = frame.relative_to(archive).as_posix()
            except (OSError, RuntimeError, ValueError) as exc:
                raise CleanupSafetyError(
                    f"sector chart frame is missing or escapes archive: {raw}"
                ) from exc
            if canonical != raw or raw in seen or not frame.is_file():
                raise CleanupSafetyError(f"sector chart frame path is not unique: {raw}")
            seen.add(raw)
            if sha256_file(frame) != metadata.get("file_sha256"):
                raise CleanupSafetyError(f"sector chart frame hash changed: {raw}")
            keep.add(frame)
    return keep, {
        "manifest_file_sha256": sha256_file(manifest),
        "manifest_content_sha256": expected_content,
        "entry_count": len(entries),
        "frame_count": len(seen),
        "verified": True,
    }


def _direct_checkpoint_paths(direct_manifest: Path) -> tuple[Path, ...]:
    document = _strict_json(direct_manifest)
    symbols = document.get("symbols")
    if not isinstance(symbols, Mapping) or not symbols:
        raise CleanupSafetyError("release direct-symbol manifest is empty")
    directory = direct_manifest.parent
    output: list[Path] = []
    for code, raw in symbols.items():
        if not isinstance(code, str) or not isinstance(raw, Mapping):
            raise CleanupSafetyError("release direct-symbol row is invalid")
        relative = raw.get("checkpoint_path")
        expected_hash = raw.get("checkpoint_sha256")
        expected_size = raw.get("checkpoint_size_bytes")
        if not isinstance(relative, str) or not relative:
            raise CleanupSafetyError(f"direct checkpoint path is invalid: {code}")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise CleanupSafetyError(f"direct checkpoint escapes bundle: {code}")
        try:
            path = directory.joinpath(*pure.parts).resolve(strict=True)
            canonical = path.relative_to(directory).as_posix()
        except (OSError, RuntimeError, ValueError) as exc:
            raise CleanupSafetyError(f"direct checkpoint is unavailable: {code}") from exc
        if canonical != relative or not path.is_file():
            raise CleanupSafetyError(f"direct checkpoint is not canonical: {code}")
        if sha256_file(path) != expected_hash or path.stat().st_size != expected_size:
            raise CleanupSafetyError(f"direct checkpoint identity changed: {code}")
        output.append(path)
    if len(output) != len(set(output)):
        raise CleanupSafetyError("release direct checkpoints are not unique")
    return tuple(sorted(output))


def _release_closure(
    root: Path,
) -> tuple[set[Path], dict[str, Path], dict[str, object]]:
    verification = verify_sector_release_manifest(
        root=root,
        manifest_path=FORMAL_MANIFEST,
        expected_artifact_path=FORMAL_ARTIFACT,
        require_current_algorithm=True,
    )
    artifact = _resolved_inside(root, FORMAL_ARTIFACT)
    manifest = _resolved_inside(root, FORMAL_MANIFEST)
    artifact_document = _strict_json(artifact)
    keep = {artifact, manifest}
    bound: dict[str, Path] = {}
    rows = verification.get("bound_files")
    if not isinstance(rows, Mapping):
        raise CleanupSafetyError("verified release bound files are missing")
    for key, raw in rows.items():
        if not isinstance(key, str) or not isinstance(raw, Mapping):
            raise CleanupSafetyError("verified release bound-file row is invalid")
        path_text = raw.get("path")
        if not isinstance(path_text, str):
            raise CleanupSafetyError(f"verified release path is invalid: {key}")
        path = _resolved_inside(root, Path(path_text))
        if sha256_file(path) != raw.get("file_sha256"):
            raise CleanupSafetyError(f"verified release file changed: {key}")
        keep.add(path)
        bound[key] = path
    expected_keys = {
        "trigger_ledger",
        "terminal_query_plan",
        "direct_manifest",
        "pit_snapshot",
        "current_catalog_ledger",
    }
    if set(bound) != expected_keys:
        raise CleanupSafetyError("verified release bound-file set changed")
    checkpoint_paths = _direct_checkpoint_paths(bound["direct_manifest"])
    keep.update(checkpoint_paths)
    bundle_parents = {path.parent for path in bound.values()}
    if len(bundle_parents) != 1 or any(
        path.parent.parent != next(iter(bundle_parents))
        for path in checkpoint_paths
    ):
        raise CleanupSafetyError("current release inputs are not one closed bundle")
    chart_keep, chart_verification = _chart_archive_closure(
        root=root,
        artifact=artifact,
        artifact_document=artifact_document,
    )
    keep.update(chart_keep)
    details = {
        "artifact_file_sha256": verification["artifact_file_sha256"],
        "artifact_content_sha256": verification["artifact_content_sha256"],
        "manifest_file_sha256": verification["manifest_file_sha256"],
        "manifest_content_sha256": verification["manifest_content_sha256"],
        "algorithm_revision": verification["algorithm_revision"],
        "algorithm_matches_current": verification["algorithm_matches_current"],
        "direct_checkpoint_count": len(checkpoint_paths),
        "chart_archive": chart_verification,
        "verified": True,
    }
    return keep, bound, details


def _promotion_rows(
    *, root: Path, bound: Mapping[str, Path]
) -> tuple[list[dict[str, object]], set[Path]]:
    direct_sources = _direct_checkpoint_paths(bound["direct_manifest"])
    pairs: list[tuple[Path, Path]] = [
        (bound["trigger_ledger"], root / CURRENT_INPUT_ROOT / "sector_first_trigger_ledger.pkl"),
        (bound["terminal_query_plan"], root / CURRENT_INPUT_ROOT / "terminal_query_plan.json"),
        (bound["direct_manifest"], root / CURRENT_INPUT_ROOT / "direct_extract_manifest.json"),
        (bound["trigger_ledger"], root / PRESCREEN_INPUT_ROOT / "sector_first_trigger_ledger.pkl"),
        (bound["terminal_query_plan"], root / PRESCREEN_INPUT_ROOT / "terminal_query_plan.json"),
        (bound["pit_snapshot"], root / PIT_SNAPSHOT),
    ]
    for source in direct_sources:
        pairs.append(
            (
                source,
                root / CURRENT_INPUT_ROOT / "direct_symbols" / source.name,
            )
        )
    rows: list[dict[str, object]] = []
    destinations: set[Path] = set()
    for source, raw_target in pairs:
        target = raw_target.resolve()
        target.relative_to(root)
        if target in destinations:
            raise CleanupSafetyError(f"duplicate promotion target: {target}")
        destinations.add(target)
        expected_hash = sha256_file(source)
        current_hash = sha256_file(target) if target.is_file() else None
        rows.append(
            {
                "source": _relative(root, source),
                "target": _relative(root, target),
                "file_sha256": expected_hash,
                "size_bytes": source.stat().st_size,
                "action": "REUSE" if current_hash == expected_hash else "COPY_REPLACE",
            }
        )
    return rows, destinations


def _build_plan(root: Path, backtest: Path) -> dict[str, Any]:
    all_files = _all_files_without_links(backtest)
    release_keep, bound, release = _release_closure(root)
    promotions, operational_targets = _promotion_rows(root=root, bound=bound)
    keep = set(release_keep)
    keep.update(operational_targets)
    for relative in HUMAN_REVIEW_FILES:
        keep.add(_resolved_inside(root, relative))
    for relative in OPTIONAL_CURRENT_HUMAN_REVIEW_FILES:
        if (root / relative).is_file():
            keep.add(_resolved_inside(root, relative))
    for path in keep:
        try:
            path.relative_to(backtest)
        except ValueError as exc:
            raise CleanupSafetyError(f"preserved file escapes backtest root: {path}") from exc

    deletions = tuple(path for path in all_files if path not in keep)
    deletion_paths = tuple(_relative(root, path) for path in deletions)
    top_groups: Counter[str] = Counter()
    byte_groups: Counter[str] = Counter()
    for path in deletions:
        relative = path.relative_to(backtest)
        top = relative.parts[0] if relative.parts else "."
        top_groups[top] += 1
        byte_groups[top] += path.stat().st_size
    groups = [
        {
            "directory": key,
            "file_count": top_groups[key],
            "bytes": byte_groups[key],
        }
        for key in sorted(top_groups, key=lambda item: (-byte_groups[item], item))
    ]
    preserve_paths = tuple(sorted(_relative(root, path) for path in keep))
    return {
        "schema": SCHEMA,
        "mode": "DRY_RUN",
        "status": "READY_TO_EXECUTE",
        "project_root": str(root),
        "backtest_root": str(backtest),
        "live_status": "LIVE_DISABLED",
        "real_account_access": False,
        "real_order_transport": False,
        "release_verification": release,
        "promotions": promotions,
        "preserved_file_count": len(keep),
        "preserved_paths_sha256": _path_sha256(preserve_paths),
        "planned_deletion_file_count": len(deletions),
        "planned_deletion_bytes": sum(path.stat().st_size for path in deletions),
        "planned_deletion_paths_sha256": _path_sha256(deletion_paths),
        "planned_deletion_groups": groups,
        "preserved_paths": list(preserve_paths),
        "planned_deletion_paths": list(deletion_paths),
    }


def _atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.cleanup.tmp")
    try:
        shutil.copyfile(source, temporary)
        if sha256_file(temporary) != sha256_file(source):
            raise CleanupSafetyError(f"promotion copy changed bytes: {target}")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, document: Mapping[str, object]) -> None:
    payload = json.dumps(
        document, ensure_ascii=False, indent=2, sort_keys=True
    ).encode("utf-8") + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.cleanup.tmp")
    try:
        temporary.write_bytes(payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _execute(
    *, root: Path, backtest: Path, plan: dict[str, Any], report_path: Path
) -> dict[str, Any]:
    started = datetime.now(CN).isoformat()
    report = dict(plan)
    report.update(
        {
            "mode": "EXECUTE",
            "status": "IN_PROGRESS",
            "started_at": started,
            "deleted_file_count": 0,
            "deleted_bytes": 0,
        }
    )
    _atomic_json(report_path, report)
    deleted_count = 0
    deleted_bytes = 0
    try:
        for row in plan["promotions"]:
            if not isinstance(row, Mapping):
                raise CleanupSafetyError("promotion row changed after planning")
            source = _resolved_inside(root, Path(str(row["source"])))
            target = (root / Path(str(row["target"]))).resolve()
            target.relative_to(backtest)
            if sha256_file(source) != row["file_sha256"]:
                raise CleanupSafetyError(f"promotion source changed: {source}")
            if not target.is_file() or sha256_file(target) != row["file_sha256"]:
                _atomic_copy(source, target)
            if sha256_file(target) != row["file_sha256"]:
                raise CleanupSafetyError(f"promotion target verification failed: {target}")

        for relative in plan["planned_deletion_paths"]:
            path = (root / Path(str(relative))).resolve(strict=True)
            path.relative_to(backtest)
            if not path.is_file() or _has_reparse_attribute(path):
                raise CleanupSafetyError(f"deletion target changed: {path}")
            size = path.stat().st_size
            path.unlink()
            deleted_count += 1
            deleted_bytes += size

        directories = sorted(
            (path for path in backtest.rglob("*") if path.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        )
        for directory in directories:
            if _has_reparse_attribute(directory):
                raise CleanupSafetyError(f"directory changed to reparse point: {directory}")
            try:
                directory.rmdir()
            except OSError:
                pass

        final_files = _all_files_without_links(backtest)
        expected = set(plan["preserved_paths"])
        actual = {_relative(root, path) for path in final_files}
        if actual != expected:
            missing = sorted(expected - actual)[:10]
            extra = sorted(actual - expected)[:10]
            raise CleanupSafetyError(
                f"post-cleanup file closure mismatch; missing={missing}, extra={extra}"
            )
        release_keep, bound, release = _release_closure(root)
        del release_keep, bound
        completed = datetime.now(CN).isoformat()
        report.update(
            {
                "status": "COMPLETED",
                "completed_at": completed,
                "deleted_file_count": deleted_count,
                "deleted_bytes": deleted_bytes,
                "remaining_file_count": len(final_files),
                "remaining_bytes": sum(path.stat().st_size for path in final_files),
                "post_cleanup_release_verification": release,
            }
        )
        _atomic_json(report_path, report)
        return report
    except Exception as exc:
        report.update(
            {
                "status": "FAILED_PARTIAL",
                "failed_at": datetime.now(CN).isoformat(),
                "deleted_file_count": deleted_count,
                "deleted_bytes": deleted_bytes,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        _atomic_json(report_path, report)
        raise


def _summary(document: Mapping[str, Any], *, include_paths: bool) -> dict[str, Any]:
    excluded = set() if include_paths else {"preserved_paths", "planned_deletion_paths"}
    return {key: value for key, value in document.items() if key not in excluded}


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root, backtest = _validate_project_root(args.root)
    plan = _build_plan(root, backtest)
    if not args.execute:
        print(
            json.dumps(
                _summary(plan, include_paths=args.include_paths),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    expected_root = str(backtest)
    if args.confirm_root is None:
        raise CleanupSafetyError(
            f"--confirm-root is required and must equal {expected_root!r}"
        )
    try:
        confirmed = str(Path(args.confirm_root).expanduser().resolve(strict=True))
    except OSError as exc:
        raise CleanupSafetyError("--confirm-root cannot be resolved") from exc
    if confirmed != expected_root:
        raise CleanupSafetyError(
            f"--confirm-root mismatch: expected {expected_root!r}, got {confirmed!r}"
        )
    if args.confirmation_token != CONFIRMATION_TOKEN:
        raise CleanupSafetyError(
            f"--confirmation-token must equal {CONFIRMATION_TOKEN!r}"
        )
    report_path = (
        args.report.resolve()
        if args.report.is_absolute()
        else (root / args.report).resolve()
    )
    report_path.relative_to(root)
    if report_path.is_relative_to(backtest):
        raise CleanupSafetyError("cleanup report must be outside the deletion root")
    result = _execute(
        root=root,
        backtest=backtest,
        plan=plan,
        report_path=report_path,
    )
    print(json.dumps(_summary(result, include_paths=False), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
