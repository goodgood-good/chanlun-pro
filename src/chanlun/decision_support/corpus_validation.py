from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote, urlsplit

from .corpus_integrity import scan_corpus
from .corpus_types import CorpusFile, IntegrityIssue, IntegrityReport


_IMAGE_RE = re.compile(
    re.escape("![")
    + r"[^]]*"
    + re.escape("](")
    + r"(?P<target>[^)]+)"
    + re.escape(")")
)
_LESSON_FILE_RE = re.compile(r"L[0-9]+_.*[.]md$", re.IGNORECASE)
_MEDIA_TYPES = {
    ".htm": "text/html",
    ".html": "text/html",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".json": "application/json",
    ".md": "text/markdown",
    ".png": "image/png",
    ".txt": "text/plain",
}


@dataclass(frozen=True)
class MetadataRepairSpec:
    destination: Path
    url: str = ""
    url_env: str = ""
    expected_sha256: str = ""
    expected_media_type: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "destination", Path(self.destination).resolve())


@dataclass(frozen=True)
class ValidatedCorpus:
    report: IntegrityReport
    repair_specs: tuple[MetadataRepairSpec, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "repair_specs", tuple(self.repair_specs))


def archive_roots(output_root: Path) -> tuple[Path, ...]:
    resolved_output = Path(output_root).resolve()
    if not resolved_output.is_dir():
        return ()
    roots = {
        directory.parent.resolve()
        for directory in resolved_output.rglob("articles")
        if directory.is_dir() and not directory.is_symlink()
    }
    return tuple(sorted(roots, key=_path_key))


def _path_key(path: Path) -> tuple[str, str]:
    value = str(path)
    return value.casefold(), value


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _verified_bytes(item: CorpusFile) -> bytes:
    try:
        data = item.path.read_bytes()
    except OSError as exc:
        raise RuntimeError("corpus changed during validation") from exc
    if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
        raise RuntimeError("corpus changed during validation")
    return data


def _media_type(path: Path) -> str:
    return _MEDIA_TYPES.get(path.suffix.casefold(), "application/octet-stream")


def _safe_relative_path(value: str) -> bool:
    if not value or chr(92) in value:
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and all(part not in ("", ".", "..") for part in path.parts)
        and path.as_posix() == value
    )


def _resolve_relative(root: Path, value: str) -> Path | None:
    if not _safe_relative_path(value):
        return None
    relative = PurePosixPath(value)
    candidate = (root / Path(*relative.parts)).resolve()
    if candidate == root.resolve() or not _is_within(candidate, root):
        return None
    return candidate


def _resolve_metadata_path(root: Path, value: object) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
    else:
        resolved = _resolve_relative(root, value)
        if resolved is None:
            return None
    if resolved == root.resolve() or not _is_within(resolved, root):
        return None
    return resolved


def _resolve_markdown_image(
    article_path: Path,
    source_root: Path,
    target: str,
) -> tuple[Path | None, str]:
    raw = target.strip().strip("<>")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return None, "unsafe_image_reference"
    if parsed.scheme or parsed.netloc:
        return None, "external_image_reference"
    if not parsed.path or chr(92) in parsed.path:
        return None, "unsafe_image_reference"
    candidate = (article_path.parent / unquote(parsed.path)).resolve()
    if candidate == source_root.resolve() or not _is_within(candidate, source_root):
        return None, "unsafe_image_reference"
    return candidate, ""


def _add_issue(
    issues: list[IntegrityIssue],
    path: Path,
    code: str,
    detail: str,
) -> None:
    issues.append(IntegrityIssue(str(path.resolve()), code, detail))


def _add_missing_file(
    files: dict[Path, CorpusFile],
    issues: list[IntegrityIssue],
    path: Path,
    code: str,
    detail: str,
) -> None:
    resolved = path.resolve()
    if resolved in files:
        return
    files[resolved] = CorpusFile(
        path=resolved,
        size=0,
        sha256="",
        media_type=_media_type(resolved),
        valid=False,
        error_code=code,
    )
    _add_issue(issues, resolved, code, detail)


def _validate_markdown_images(
    path: Path,
    source_root: Path,
    item: CorpusFile,
    files: dict[Path, CorpusFile],
    issues: list[IntegrityIssue],
) -> None:
    text = _verified_bytes(item).decode("utf-8")
    for match in _IMAGE_RE.finditer(text):
        target = match.group("target")
        candidate, error = _resolve_markdown_image(path, source_root, target)
        if error:
            _add_issue(issues, path, error, target)
            continue
        assert candidate is not None
        target_file = files.get(candidate)
        if target_file is None:
            _add_missing_file(
                files,
                issues,
                candidate,
                "missing_reference",
                f"{path.resolve()} -> {target}",
            )
        elif target_file.valid and not target_file.media_type.startswith("image/"):
            _add_issue(issues, path, "invalid_image_reference", target)


def _validate_lesson_index(
    lesson_root: Path,
    files: dict[Path, CorpusFile],
    issues: list[IntegrityIssue],
) -> None:
    index_path = (lesson_root.resolve() / "_index.md").resolve()
    item = files.get(index_path)
    if item is None or not item.valid:
        return
    text = _verified_bytes(item).decode("utf-8")
    for line in text.splitlines():
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        filename = next(
            (
                column
                for column in columns
                if _LESSON_FILE_RE.fullmatch(Path(column).name)
            ),
            "",
        )
        if not filename:
            continue
        candidate = (lesson_root.resolve() / filename).resolve()
        if not _is_within(candidate, lesson_root):
            _add_issue(issues, index_path, "unsafe_lesson_reference", filename)
        elif candidate not in files:
            _add_missing_file(
                files,
                issues,
                candidate,
                "missing_lesson_reference",
                f"{index_path} -> {filename}",
            )


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _count_mismatch(
    issues: list[IntegrityIssue],
    index_path: Path,
    field_name: str,
    declared: object,
    actual: int,
) -> None:
    if _non_negative_int(declared) != actual:
        _add_issue(
            issues,
            index_path,
            "archive_count_mismatch",
            f"{field_name}: declared={declared!r}, actual={actual}",
        )


def _register_spec(
    specs: dict[Path, MetadataRepairSpec],
    issues: list[IntegrityIssue],
    index_path: Path,
    spec: MetadataRepairSpec,
) -> None:
    previous = specs.get(spec.destination)
    if previous is not None and previous != spec:
        _add_issue(
            issues,
            index_path,
            "duplicate_repair_target",
            str(spec.destination),
        )
        return
    specs[spec.destination] = spec


def _repair_spec_from_mapping(
    value: object,
    *,
    archive_root: Path,
    index_path: Path,
    files: dict[Path, CorpusFile],
    issues: list[IntegrityIssue],
    fallback_path: object = None,
) -> MetadataRepairSpec | None:
    if not isinstance(value, dict):
        return None
    raw_path = value.get("path", fallback_path)
    destination = _resolve_metadata_path(archive_root, raw_path)
    if destination is None:
        _add_issue(
            issues,
            index_path,
            "unsafe_repair_target",
            repr(raw_path),
        )
        return None
    url = value.get("source_url", value.get("url", ""))
    url_env = value.get("url_env", "")
    expected_sha256 = value.get("sha256", "")
    expected_media_type = value.get("media_type", "")
    if not all(
        isinstance(item, str)
        for item in (url, url_env, expected_sha256, expected_media_type)
    ):
        _add_issue(
            issues,
            index_path,
            "invalid_repair_metadata",
            str(destination),
        )
        return None
    if url and url_env:
        _add_issue(
            issues,
            index_path,
            "invalid_repair_metadata",
            str(destination),
        )
        return None
    if destination not in files:
        _add_missing_file(
            files,
            issues,
            destination,
            "missing_metadata_target",
            str(index_path),
        )
    return MetadataRepairSpec(
        destination=destination,
        url=url,
        url_env=url_env,
        expected_sha256=expected_sha256,
        expected_media_type=expected_media_type or _media_type(destination),
    )


def _validate_archive_index(
    archive_root: Path,
    files: dict[Path, CorpusFile],
    issues: list[IntegrityIssue],
    specs: dict[Path, MetadataRepairSpec],
) -> None:
    index_path = (archive_root / "index.json").resolve()
    item = files.get(index_path)
    if item is None or not item.valid:
        return
    payload = json.loads(_verified_bytes(item).decode("utf-8"))
    if not isinstance(payload, dict):
        _add_issue(issues, index_path, "invalid_archive_index", "root must be object")
        return

    articles = payload.get("articles")
    aggregate_fields = (
        "count",
        "totalImages",
        "downloadedImages",
        "imageErrors",
    )
    if articles is None:
        if any(field_name in payload for field_name in aggregate_fields):
            _add_issue(
                issues,
                index_path,
                "invalid_archive_index",
                "aggregate counts require articles",
            )
    elif not isinstance(articles, list):
        _add_issue(
            issues,
            index_path,
            "invalid_archive_index",
            "articles must be a list",
        )
    else:
        _count_mismatch(
            issues,
            index_path,
            "count",
            payload.get("count"),
            len(articles),
        )
        for field_name, article_field in (
            ("totalImages", "imageCount"),
            ("downloadedImages", "downloadedImageCount"),
            ("imageErrors", "imageErrorCount"),
        ):
            counts = [
                _non_negative_int(article.get(article_field))
                if isinstance(article, dict)
                else None
                for article in articles
            ]
            if any(count is None for count in counts):
                _add_issue(
                    issues,
                    index_path,
                    "invalid_archive_index",
                    f"invalid {article_field}",
                )
            else:
                _count_mismatch(
                    issues,
                    index_path,
                    field_name,
                    payload.get(field_name),
                    sum(count for count in counts if count is not None),
                )

        valid_images = sum(
            1
            for corpus_file in files.values()
            if corpus_file.valid
            and corpus_file.media_type.startswith("image/")
            and _is_within(
                corpus_file.path,
                (archive_root / "articles" / "images").resolve(),
            )
        )
        if "downloadedImages" in payload:
            _count_mismatch(
                issues,
                index_path,
                "downloadedImagesOnDisk",
                payload.get("downloadedImages"),
                valid_images,
            )

        for article in articles:
            if not isinstance(article, dict):
                continue
            declared_files = article.get("files")
            if not isinstance(declared_files, dict):
                continue
            for file_value in declared_files.values():
                if isinstance(file_value, dict):
                    spec = _repair_spec_from_mapping(
                        file_value,
                        archive_root=archive_root,
                        index_path=index_path,
                        files=files,
                        issues=issues,
                    )
                    if spec is not None:
                        _register_spec(specs, issues, index_path, spec)
                    raw_path = file_value.get("path")
                else:
                    raw_path = file_value
                destination = _resolve_metadata_path(archive_root, raw_path)
                if destination is None:
                    _add_issue(
                        issues,
                        index_path,
                        "unsafe_metadata_path",
                        repr(raw_path),
                    )
                elif destination not in files:
                    _add_missing_file(
                        files,
                        issues,
                        destination,
                        "missing_metadata_file",
                        str(index_path),
                    )

    repair_targets = payload.get("repair_targets")
    if repair_targets is None:
        return
    if not isinstance(repair_targets, list):
        _add_issue(
            issues,
            index_path,
            "invalid_repair_metadata",
            "repair_targets must be a list",
        )
        return
    for target in repair_targets:
        spec = _repair_spec_from_mapping(
            target,
            archive_root=archive_root,
            index_path=index_path,
            files=files,
            issues=issues,
        )
        if spec is not None:
            _register_spec(specs, issues, index_path, spec)


def scan_validated_corpus(
    lesson_root: Path,
    output_root: Path,
) -> ValidatedCorpus:
    resolved_lesson = Path(lesson_root).resolve()
    resolved_output = Path(output_root).resolve()
    base = scan_corpus((resolved_lesson, resolved_output))
    files = {item.path.resolve(): item for item in base.files}
    issues = list(base.issues)
    specs: dict[Path, MetadataRepairSpec] = {}

    _validate_lesson_index(resolved_lesson, files, issues)
    lesson_markdown = [
        item
        for item in files.values()
        if item.valid
        and item.media_type == "text/markdown"
        and _is_within(item.path, resolved_lesson)
        and item.path.name != "_index.md"
    ]
    for item in sorted(lesson_markdown, key=lambda value: _path_key(value.path)):
        _validate_markdown_images(
            item.path,
            resolved_lesson,
            item,
            files,
            issues,
        )

    for archive_root in archive_roots(resolved_output):
        article_root = (archive_root / "articles").resolve()
        archive_markdown = [
            item
            for item in files.values()
            if item.valid
            and item.media_type == "text/markdown"
            and _is_within(item.path, article_root)
        ]
        for item in sorted(archive_markdown, key=lambda value: _path_key(value.path)):
            _validate_markdown_images(
                item.path,
                archive_root,
                item,
                files,
                issues,
            )
        _validate_archive_index(archive_root, files, issues, specs)

    unique_issues = {
        (issue.path, issue.code, issue.detail): issue for issue in issues
    }
    report = IntegrityReport(
        files=tuple(sorted(files.values(), key=lambda item: _path_key(item.path))),
        issues=tuple(
            unique_issues[key]
            for key in sorted(
                unique_issues,
                key=lambda value: (
                    value[0].casefold(),
                    value[0],
                    value[1],
                    value[2],
                ),
            )
        ),
    )
    return ValidatedCorpus(
        report=report,
        repair_specs=tuple(
            specs[path] for path in sorted(specs, key=_path_key)
        ),
    )
