from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
from typing import Sequence


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_ROOT = _REPOSITORY_ROOT / "src"
_MAX_URL_MAP_BYTES = 4 * 1024 * 1024
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from chanlun.decision_support.corpus_validation import (
    MetadataRepairSpec,
    archive_roots,
    scan_validated_corpus,
)
from chanlun.decision_support.corpus_retrieval import CorpusIndex
from chanlun.decision_support.corpus_repair import (
    RepairResult,
    RepairTarget,
    repair_targets,
    write_repair_report,
)
from chanlun.decision_support.corpus_sources import (
    collect_image_evidence,
    parse_illustrated_archive,
    parse_lesson_index,
    write_trusted_manifest,
)
from chanlun.decision_support.corpus_types import (
    EvidenceUnit,
    ImageEvidence,
    IntegrityIssue,
    IntegrityReport,
    SourceTier,
)


def _atomic_write_json(path: Path, payload: object) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=target.name + ".",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return target


def _integrity_payload(report: IntegrityReport) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for item in report.files:
        body = asdict(item)
        body["path"] = str(item.path)
        files.append(body)
    return {
        "schema": "current",
        "summary": {
            "invalid": sum(1 for item in report.files if not item.valid),
            "scanned": len(report.files),
            "trusted": len(report.valid_files),
        },
        "files": files,
        "issues": [asdict(issue) for issue in report.issues],
    }


_BUILD_OUTPUT_NAMES = (
    "trusted_manifest.json",
    "corpus_units.json",
    "integrity_report.json",
    "repair_report.json",
)


def _clear_trusted_outputs(build_root: Path) -> None:
    for name in _BUILD_OUTPUT_NAMES[:2]:
        (build_root / name).unlink(missing_ok=True)


def _clear_build_outputs(build_root: Path) -> None:
    for name in _BUILD_OUTPUT_NAMES:
        (build_root / name).unlink(missing_ok=True)


def _validate_roots(
    lesson_root: Path,
    output_root: Path,
    build_root: Path,
) -> None:
    if not lesson_root.is_dir() or not output_root.is_dir():
        raise ValueError("corpus root must be an existing directory")
    _validate_root_relationships(lesson_root, output_root, build_root)


def _validate_root_relationships(
    lesson_root: Path,
    output_root: Path,
    build_root: Path,
) -> None:
    if (
        lesson_root == output_root
        or _is_within(lesson_root, output_root)
        or _is_within(output_root, lesson_root)
    ):
        raise ValueError("corpus roots must not overlap")
    if _is_within(build_root, lesson_root) or _is_within(build_root, output_root):
        raise ValueError("build root must be outside corpus roots")
    if _is_within(lesson_root, build_root) or _is_within(output_root, build_root):
        raise ValueError("build root must not contain corpus roots")

def _archive_roots(output_root: Path) -> tuple[Path, ...]:
    return archive_roots(output_root)


def _archive_source_path(archive_root: Path, output_root: Path, source_path: str) -> str:
    prefix = archive_root.relative_to(output_root).as_posix()
    return source_path if prefix == "." else f"{prefix}/{source_path}"


def _namespace_archive_unit(
    unit: EvidenceUnit,
    archive_root: Path,
    output_root: Path,
) -> EvidenceUnit:
    source_path = _archive_source_path(archive_root, output_root, unit.source_path)
    identity = f"{unit.source_tier.value}\0{source_path}\0{unit.evidence_id}"
    evidence_id = "evidence:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return replace(unit, evidence_id=evidence_id, source_path=source_path)


def _namespace_archive_image(
    image: ImageEvidence,
    archive_root: Path,
    output_root: Path,
) -> ImageEvidence:
    return replace(
        image,
        source_path=_archive_source_path(archive_root, output_root, image.source_path),
    )


def _dedupe_units(units: Sequence[EvidenceUnit]) -> tuple[EvidenceUnit, ...]:
    by_id: dict[str, EvidenceUnit] = {}
    for unit in units:
        previous = by_id.setdefault(unit.evidence_id, unit)
        if previous != unit:
            raise ValueError(f"conflicting evidence_id: {unit.evidence_id}")
    return tuple(sorted(by_id.values(), key=lambda unit: unit.evidence_id))


def _dedupe_images(images: Sequence[ImageEvidence]) -> tuple[ImageEvidence, ...]:
    by_id: dict[str, ImageEvidence] = {}
    for image in sorted(images, key=lambda item: (item.image_id, item.source_path)):
        by_id.setdefault(image.image_id, image)
    return tuple(sorted(by_id.values(), key=lambda image: image.image_id))


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _RepairSpec:
    url: str
    expected_sha256: str
    expected_media_type: str


def _load_url_map(
    path: Path | None,
    roots: dict[str, Path],
) -> dict[Path, _RepairSpec]:
    if path is None:
        return {}
    map_path = Path(path).resolve()
    try:
        with map_path.open("rb") as stream:
            map_bytes = stream.read(_MAX_URL_MAP_BYTES + 1)
    except OSError as exc:
        raise ValueError("invalid url map") from exc
    if len(map_bytes) > _MAX_URL_MAP_BYTES:
        raise ValueError("url map is too large")
    try:
        payload = json.loads(map_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid url map") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "current":
        raise ValueError("invalid url map schema")
    targets = payload.get("targets")
    if not isinstance(targets, list):
        raise ValueError("invalid url map targets")

    specs: dict[Path, _RepairSpec] = {}
    for value in targets:
        if not isinstance(value, dict):
            raise ValueError("invalid url map target")
        root_name = value.get("root")
        source_path = value.get("path")
        if root_name not in roots or not isinstance(source_path, str):
            raise ValueError("invalid url map destination")
        relative = PurePosixPath(source_path)
        if (
            not source_path
            or chr(92) in source_path
            or relative.is_absolute()
            or any(part in ("", ".", "..") for part in relative.parts)
        ):
            raise ValueError("unsafe url map path")
        root = roots[root_name].resolve()
        destination = (root / Path(*relative.parts)).resolve()
        if destination == root or not _is_within(destination, root):
            raise ValueError("unsafe url map path")
        url = value.get("url", "")
        url_env = value.get("url_env", "")
        expected_sha256 = value.get("sha256", "")
        expected_media_type = value.get("media_type", "")
        if not all(
            isinstance(item, str)
            for item in (url, url_env, expected_sha256, expected_media_type)
        ):
            raise ValueError("invalid url map target fields")
        if url and url_env:
            raise ValueError("url map target cannot set both url and url_env")
        if url_env:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", url_env) is None:
                raise ValueError("invalid url environment variable name")
            url = os.environ.get(url_env, "")
        if destination in specs:
            raise ValueError("duplicate url map destination")
        specs[destination] = _RepairSpec(
            url=url,
            expected_sha256=expected_sha256,
            expected_media_type=expected_media_type,
        )
    return specs


def _metadata_repair_specs(
    values: Sequence[MetadataRepairSpec],
) -> dict[Path, _RepairSpec]:
    specs: dict[Path, _RepairSpec] = {}
    for value in values:
        url = value.url
        if value.url_env:
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value.url_env) is None:
                continue
            url = os.environ.get(value.url_env, "")
        specs[value.destination] = _RepairSpec(
            url=url,
            expected_sha256=value.expected_sha256,
            expected_media_type=value.expected_media_type,
        )
    return specs


def _repair_invalid_files(
    report: IntegrityReport,
    roots: dict[str, Path],
    specs: dict[Path, _RepairSpec],
) -> tuple[RepairResult, ...]:
    unresolved = {
        item.path.resolve(): item
        for item in report.files
        if not item.valid
    }
    if not set(specs).issubset(unresolved):
        raise ValueError("url map target is not an invalid corpus file")

    results: list[RepairResult] = []
    ordered_roots = sorted(
        ((name, root.resolve()) for name, root in roots.items()),
        key=lambda item: (
            -len(item[1].parts),
            str(item[1]).casefold(),
            str(item[1]),
            item[0],
        ),
    )
    for root_name, root in ordered_roots:
        paths = sorted(
            (path for path in unresolved if _is_within(path, root)),
            key=lambda path: (str(path).casefold(), str(path)),
        )
        targets: list[RepairTarget] = []
        for path in paths:
            spec = specs.get(path)
            targets.append(
                RepairTarget(
                    url=spec.url if spec else "",
                    destination=path,
                    expected_sha256=spec.expected_sha256 if spec else "",
                    expected_media_type=(
                        spec.expected_media_type
                        if spec and spec.expected_media_type
                        else unresolved[path].media_type
                    ),
                )
            )
        root_results = repair_targets(targets, fetch=None, allowed_root=root)
        results.extend(
            replace(item, destination=f"{root_name}/{item.destination}")
            for item in root_results
        )
        for path in paths:
            unresolved.pop(path)
    if unresolved:
        raise RuntimeError("integrity issue outside configured roots")
    return tuple(results)

def _integrity_snapshot(report: IntegrityReport) -> tuple[object, ...]:
    files = tuple(
        (
            str(item.path),
            item.size,
            item.sha256,
            item.media_type,
            item.valid,
            item.error_code,
            item.width,
            item.height,
        )
        for item in report.files
    )
    issues = tuple(
        (item.path, item.code, item.detail)
        for item in report.issues
    )
    return files, issues


def _assert_corpus_unchanged(
    report: IntegrityReport,
    lesson_root: Path,
    output_root: Path,
) -> None:
    current = scan_validated_corpus(lesson_root, output_root).report
    if _integrity_snapshot(current) != _integrity_snapshot(report):
        raise RuntimeError("corpus changed during build")


def _manifest_source_path(
    value: object,
    lesson_root: Path,
    output_root: Path,
) -> Path:
    if not isinstance(value, dict):
        raise RuntimeError("invalid written corpus entry")
    source_tier = value.get("source_tier")
    source_path = value.get("source_path")
    if not isinstance(source_tier, str) or not isinstance(source_path, str):
        raise RuntimeError("invalid written corpus entry")
    roots = {
        SourceTier.LESSON_ORIGINAL.value: lesson_root.resolve(),
        SourceTier.LESSON_CHART.value: lesson_root.resolve(),
        SourceTier.SECONDARY_ANNOTATION.value: output_root.resolve(),
        SourceTier.PROJECT_IMPLEMENTATION.value: _REPOSITORY_ROOT.resolve(),
    }
    root = roots.get(source_tier)
    relative = PurePosixPath(source_path)
    if (
        root is None
        or not source_path
        or chr(92) in source_path
        or relative.is_absolute()
        or any(part in ("", ".", "..") for part in relative.parts)
    ):
        raise RuntimeError("unsafe written corpus source path")
    source = (root / Path(*relative.parts)).resolve()
    if source == root or not _is_within(source, root):
        raise RuntimeError("unsafe written corpus source path")
    return source


def _verify_written_outputs(
    manifest_path: Path,
    units_path: Path,
    lesson_root: Path,
    output_root: Path,
) -> None:
    manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    units_payload = json.loads(units_path.read_bytes().decode("utf-8"))
    if not isinstance(manifest, dict) or not isinstance(units_payload, dict):
        raise RuntimeError("invalid written corpus payload")
    manifest_units = manifest.get("units")
    manifest_images = manifest.get("images")
    if not isinstance(manifest_units, list) or not isinstance(manifest_images, list):
        raise RuntimeError("invalid written corpus payload")
    if units_payload.get("units") != manifest_units:
        raise RuntimeError("written corpus units mismatch")
    if units_payload.get("indexed_units") != len(manifest_units):
        raise RuntimeError("written corpus index count mismatch")
    for item in [*manifest_units, *manifest_images]:
        source = _manifest_source_path(item, lesson_root, output_root)
        expected_hash = item.get("sha256") if isinstance(item, dict) else None
        if not isinstance(expected_hash, str):
            raise RuntimeError("invalid written corpus source hash")
        try:
            actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            raise RuntimeError("written corpus source missing") from exc
        if actual_hash != expected_hash:
            raise RuntimeError("written corpus source hash mismatch")


def _build_trusted_outputs(
    lesson_root: Path,
    output_root: Path,
    build_root: Path,
    report: IntegrityReport,
    *,
    integrity_status: str,
) -> bool:
    units: list[EvidenceUnit] = list(parse_lesson_index(lesson_root, report))
    images: list[ImageEvidence] = list(
        collect_image_evidence(lesson_root, report, SourceTier.LESSON_CHART)
    )
    for archive_root in _archive_roots(output_root):
        archive_units = parse_illustrated_archive(archive_root, report)
        archive_images = collect_image_evidence(
            archive_root,
            report,
            SourceTier.SECONDARY_ANNOTATION,
        )
        units.extend(
            _namespace_archive_unit(unit, archive_root, output_root)
            for unit in archive_units
        )
        images.extend(
            _namespace_archive_image(image, archive_root, output_root)
            for image in archive_images
        )

    trusted_units = _dedupe_units(units)
    trusted_images = _dedupe_images(images)
    if not trusted_units and not trusted_images:
        return False
    index = CorpusIndex.build(trusted_units)
    _assert_corpus_unchanged(report, lesson_root, output_root)
    manifest_path = write_trusted_manifest(
        build_root / "trusted_manifest.json",
        trusted_units,
        trusted_images,
        integrity_status=integrity_status,
    )
    manifest = json.loads(manifest_path.read_bytes().decode("utf-8"))
    units_path = _atomic_write_json(
        build_root / "corpus_units.json",
        {
            "schema": "current",
            "indexed_units": len(index),
            "units": manifest["units"],
        },
    )
    _assert_corpus_unchanged(report, lesson_root, output_root)
    _verify_written_outputs(
        manifest_path,
        units_path,
        lesson_root,
        output_root,
    )
    return True


class _CorpusArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(f"invalid arguments: {message}")


def _parser() -> argparse.ArgumentParser:
    parser = _CorpusArgumentParser(description="Build a verified Chanlun corpus")
    parser.add_argument("--lesson-root", default="audit/_docx_extract")
    parser.add_argument("--output-root", default="output")
    parser.add_argument("--build-root", required=True)
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--url-map")
    return parser


def _append_integrity_issue(
    report: IntegrityReport,
    path: Path,
    code: str,
    detail: str,
) -> IntegrityReport:
    issue = IntegrityIssue(str(path.resolve()), code, detail)
    if issue in report.issues:
        return report
    issues = tuple(
        sorted(
            (*report.issues, issue),
            key=lambda item: (
                item.path.casefold(),
                item.path,
                item.code,
                item.detail,
            ),
        )
    )
    return replace(report, issues=issues)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    lesson_root = Path(args.lesson_root).resolve()
    output_root = Path(args.output_root).resolve()
    build_root = Path(args.build_root).resolve()
    _validate_root_relationships(lesson_root, output_root, build_root)

    try:
        _clear_build_outputs(build_root)
        _validate_roots(lesson_root, output_root, build_root)
        roots = {"lesson": lesson_root, "output": output_root}
        if args.url_map and not args.repair:
            raise ValueError("--url-map requires --repair")
        validated = scan_validated_corpus(lesson_root, output_root)
        report = validated.report
        url_specs = _load_url_map(
            Path(args.url_map) if args.url_map else None,
            roots,
        )
        invalid_paths = {
            item.path.resolve() for item in report.files if not item.valid
        }
        if not set(url_specs).issubset(invalid_paths):
            raise ValueError("url map target is not an invalid corpus file")
        metadata_specs = {
            path: spec
            for path, spec in _metadata_repair_specs(
                validated.repair_specs
            ).items()
            if path in invalid_paths
        }
        repair_specs = {**metadata_specs, **url_specs}

        repair_results: tuple[RepairResult, ...] = ()
        if args.repair and report.issues:
            repair_results = _repair_invalid_files(
                report,
                roots,
                repair_specs,
            )
            report = scan_validated_corpus(
                lesson_root,
                output_root,
            ).report

        _atomic_write_json(
            build_root / "integrity_report.json",
            _integrity_payload(report),
        )
        write_repair_report(
            build_root / "repair_report.json",
            repair_results,
            build_root,
        )
        integrity_status = "incomplete" if report.issues else "complete"
        exit_code = (3 if args.repair else 2) if report.issues else 0
        build_written = _build_trusted_outputs(
            lesson_root,
            output_root,
            build_root,
            report,
            integrity_status=integrity_status,
        )
        if not build_written:
            report = _append_integrity_issue(
                report,
                lesson_root,
                "empty_trusted_corpus",
                "no verified evidence unit or image was produced",
            )
            _atomic_write_json(
                build_root / "integrity_report.json",
                _integrity_payload(report),
            )
            _clear_trusted_outputs(build_root)
            exit_code = 3 if args.repair else 2
        return exit_code
    except Exception:
        _clear_build_outputs(build_root)
        raise

if __name__ == "__main__":
    raise SystemExit(main())
