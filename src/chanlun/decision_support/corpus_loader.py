from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Callable
import unicodedata

from .corpus_chunking import build_semantic_units
from .corpus_types import EvidenceUnit, ImageEvidence, SourceTier
from .llm_provider import ProviderImage
from .lesson_image_cache import (
    _absolute_without_resolving,
    _safe_directory,
    _safe_existing_path,
    _safe_regular_file,
    _stream_sha256_and_size,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID_RE = re.compile(r"^source:[0-9a-f]{64}$")
_OCCURRENCE_ID_RE = re.compile(r"^occurrence:[0-9a-f]{64}$")
_CLASSIFICATION_ID_RE = re.compile(r"^classification:[0-9a-f]{64}$")
_MARKER_RE = re.compile(
    r'^<!-- chanlun-source (?P<payload>\{[^\r\n]+\}) -->\r?\n',
    re.MULTILINE,
)
_AUTHORITATIVE_TEXT_ROLES = frozenset({"lesson_body", "chan_reply", "chan_excerpt"})
_QUARANTINED_TEXT_ROLES = frozenset(
    {"editor_note", "reader_comment", "unknown_text"}
)
_MAX_CERTIFIED_IMAGE_BYTES = 20 * 1024 * 1024
_SEMANTIC_AUDIT_VERSION = "chanlun-semantic-audit/1"
_SEMANTIC_THRESHOLDS = {
    "anonymous_chan_reply_count_max": 0,
    "image_provenance_incomplete_count_max": 0,
    "quarantined_text_authoritative_leak_count_max": 0,
    "reader_authoritative_leak_count_max": 0,
    "reply_provenance_incomplete_count_max": 0,
    "unknown_image_authoritative_leak_count_max": 0,
}
_SEMANTIC_ROLE_AUDIT_FIELDS = frozenset(
    {
        "ambiguous_reply_record_count",
        "anonymous_chan_reply_count",
        "classification_sha256",
        "image_role_counts",
        "image_provenance_incomplete_count",
        "quarantined_text_authoritative_leak_count",
        "quarantined_unknown_image_count",
        "quarantined_unknown_text_count",
        "reader_authoritative_leak_count",
        "reply_resolution_counts",
        "reply_provenance_incomplete_count",
        "skipped_running_matter_count",
        "text_role_counts",
        "unknown_image_authoritative_leak_count",
    }
)
_SEMANTIC_ZERO_FIELDS = frozenset(
    {
        "anonymous_chan_reply_count",
        "image_provenance_incomplete_count",
        "quarantined_text_authoritative_leak_count",
        "reader_authoritative_leak_count",
        "reply_provenance_incomplete_count",
        "unknown_image_authoritative_leak_count",
    }
)


@dataclass(frozen=True)
class CertifiedCorpusPolicy:
    manifest_sha256: str = "b90e5d757209c0f9ed14226d405d59559ac557eecd127df38f83623be3d29a0a"
    source_pdf_sha256: str = "867b1262af2d3430b98421df4c5372748eb75a4eb7600cd967ecdc374817429e"
    source_pdf_size_bytes: int = 1_352_725_597
    source_pdf_page_count: int = 2_533
    lesson_count: int = 109
    image_asset_count: int = 2_783
    image_occurrence_count: int = 2_816
    total_primary_raw_stream_bytes: int = 1_343_589_074
    image_role_counts: Mapping[str, int] = field(
        default_factory=lambda: {
            "editor_image": 102,
            "lesson_chart": 105,
            "unknown_image": 2_609,
        }
    )
    materialized_chart_asset_count: int = 103

    def __post_init__(self) -> None:
        manifest_sha256 = str(self.manifest_sha256).strip().lower()
        sha256 = str(self.source_pdf_sha256).strip().lower()
        if _SHA256_RE.fullmatch(manifest_sha256) is None:
            raise ValueError("manifest_sha256 must be a SHA-256")
        if _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError("source_pdf_sha256 must be a SHA-256")
        for name in (
            "source_pdf_size_bytes",
            "source_pdf_page_count",
            "lesson_count",
            "image_asset_count",
            "image_occurrence_count",
            "total_primary_raw_stream_bytes",
            "materialized_chart_asset_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        roles = dict(self.image_role_counts)
        if any(
            role not in {"lesson_chart", "editor_image", "unknown_image"}
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            for role, count in roles.items()
        ):
            raise ValueError("image_role_counts are invalid")
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "source_pdf_sha256", sha256)
        object.__setattr__(self, "image_role_counts", dict(sorted(roles.items())))


@dataclass(frozen=True)
class CertifiedLessonCorpus:
    root: Path
    units: tuple[EvidenceUnit, ...]
    semantic_units: tuple[EvidenceUnit, ...]
    images: tuple[ImageEvidence, ...]
    manifest_sha256: str
    source_pdf_sha256: str


def _count_mapping(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for key, count in value.items()
    ):
        raise ValueError(f"certified corpus semantic {label} is invalid")
    return dict(sorted(value.items()))


def _semantic_certification(certification: object) -> dict[str, object]:
    if not isinstance(certification, dict):
        raise ValueError("certified corpus certification evidence is incomplete")
    semantic = certification.get("semantic_audit")
    expected_fields = {
        "lesson_boundary_count",
        "lesson_boundary_sha256",
        "role_audit",
        "semantic_audit_version",
        "semantic_gate_passed",
        "semantic_warnings",
        "text_cache_sha256",
        "text_span_count",
        "thresholds",
    }
    if not isinstance(semantic, dict) or set(semantic) != expected_fields:
        raise ValueError("certified corpus semantic certification is missing or invalid")
    if (
        semantic.get("semantic_audit_version") != _SEMANTIC_AUDIT_VERSION
        or semantic.get("semantic_gate_passed") is not True
        or semantic.get("thresholds") != _SEMANTIC_THRESHOLDS
    ):
        raise ValueError("certified corpus semantic certification gate is closed")
    for name in ("lesson_boundary_sha256", "text_cache_sha256"):
        if not isinstance(semantic.get(name), str) or _SHA256_RE.fullmatch(semantic[name]) is None:
            raise ValueError("certified corpus semantic certification fingerprint is invalid")
    for name in ("lesson_boundary_count", "text_span_count"):
        value = semantic.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("certified corpus semantic certification count is invalid")
    role_audit = semantic.get("role_audit")
    if not isinstance(role_audit, dict) or set(role_audit) != _SEMANTIC_ROLE_AUDIT_FIELDS:
        raise ValueError("certified corpus semantic role audit is invalid")
    if any(role_audit.get(name) != 0 for name in _SEMANTIC_ZERO_FIELDS):
        raise ValueError("certified corpus semantic role audit blocker is nonzero")
    for name in (
        "ambiguous_reply_record_count",
        "quarantined_unknown_image_count",
        "quarantined_unknown_text_count",
        "skipped_running_matter_count",
    ):
        value = role_audit.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("certified corpus semantic role audit count is invalid")
    classification = role_audit.get("classification_sha256")
    if not isinstance(classification, str) or _SHA256_RE.fullmatch(classification) is None:
        raise ValueError("certified corpus semantic classification fingerprint is invalid")
    _count_mapping(role_audit.get("image_role_counts"), "image role counts")
    _count_mapping(role_audit.get("reply_resolution_counts"), "reply resolution counts")
    _count_mapping(role_audit.get("text_role_counts"), "text role counts")
    warnings = semantic.get("semantic_warnings")
    expected_warnings = [
        f"{label}:{role_audit[name]}"
        for name, label in (
            ("ambiguous_reply_record_count", "ambiguous_reply_records"),
            ("quarantined_unknown_text_count", "quarantined_unknown_text"),
            ("quarantined_unknown_image_count", "quarantined_unknown_image"),
        )
        if role_audit[name]
    ]
    if warnings != expected_warnings:
        raise ValueError("certified corpus semantic warnings are inconsistent")
    return semantic


def make_certified_image_loader(
    corpus: CertifiedLessonCorpus,
    *,
    max_bytes: int = _MAX_CERTIFIED_IMAGE_BYTES,
) -> Callable[[ImageEvidence], ProviderImage]:
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    root = _absolute_without_resolving(corpus.root)
    trusted_images = {image.image_id: image for image in corpus.images}
    if len(trusted_images) != len(corpus.images):
        raise ValueError("certified corpus image id is duplicated")

    def load(image: ImageEvidence) -> ProviderImage:
        trusted = trusted_images.get(image.image_id)
        if trusted is None or trusted != image:
            raise ValueError("image is not part of the certified corpus")
        relative = _safe_relative(image.source_path)
        path = root / relative
        _safe_regular_file(path, "certified image")
        try:
            with path.open("rb") as stream:
                before = os.fstat(stream.fileno())
                if before.st_size <= 0 or before.st_size > max_bytes:
                    raise ValueError("certified image payload size is invalid")
                payload = stream.read(max_bytes + 1)
                after = os.fstat(stream.fileno())
        except OSError as exc:
            raise ValueError("certified image is unreadable") from exc
        if (
            len(payload) != before.st_size
            or len(payload) > max_bytes
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError("certified image payload size or identity changed")
        actual = "sha256:" + hashlib.sha256(payload).hexdigest()
        if image.sha256 != actual or image.asset_id != actual:
            raise ValueError("certified image fingerprint mismatch")
        encoded = base64.b64encode(payload).decode("ascii")
        return ProviderImage(
            image_id=image.image_id,
            media_type=image.media_type,
            data_url=f"data:{image.media_type};base64,{encoded}",
        )

    return load


def _safe_relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        raise ValueError("package path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("package path is unsafe")
    return value


def _read_manifest(root: Path) -> tuple[dict[str, object], bytes]:
    try:
        path = root / "manifest.json"
        _safe_regular_file(path, "certified corpus manifest")
        data = path.read_bytes()
        manifest = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("certified corpus manifest is unreadable") from exc
    if not isinstance(manifest, dict):
        raise ValueError("certified corpus manifest must be an object")
    return manifest, data


class _VerifiedFiles(Mapping[str, bytes]):
    def __init__(self, entries: dict[str, tuple[Path, str]]) -> None:
        self._entries = entries

    def __getitem__(self, relative: str) -> bytes:
        try:
            path, _ = self._entries[relative]
        except KeyError:
            raise KeyError(relative) from None
        try:
            return path.read_bytes()
        except OSError as exc:
            raise ValueError("certified corpus file is unreadable") from exc

    def __iter__(self) -> Iterator[str]:
        return iter(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, relative: object) -> bool:
        return relative in self._entries

    def sha256(self, relative: str) -> str:
        try:
            return self._entries[relative][1]
        except KeyError:
            raise KeyError(relative) from None


def _verified_files(root: Path, manifest: dict[str, object]) -> _VerifiedFiles:
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ValueError("certified corpus file list is invalid")
    files: dict[str, tuple[Path, str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("certified corpus file entry is invalid")
        relative = _safe_relative(entry.get("path"))
        if relative in files:
            raise ValueError("certified corpus file path is duplicated")
        path = root / relative
        try:
            actual_sha256, actual_size = _stream_sha256_and_size(
                path, "certified corpus file"
            )
        except OSError as exc:
            raise ValueError("certified corpus file is unreadable") from exc
        if entry.get("size_bytes") != actual_size or entry.get("sha256") != actual_sha256:
            raise ValueError("certified corpus file hash or size mismatch")
        files[relative] = (path, actual_sha256)
    actual_files = set()
    for path in root.rglob("*"):
        info = _safe_existing_path(path, "certified corpus tree entry")
        if stat.S_ISREG(info.st_mode):
            if path != root / "manifest.json":
                actual_files.add(path.relative_to(root).as_posix())
        elif not stat.S_ISDIR(info.st_mode):
            raise ValueError("certified corpus tree entry has an invalid file type")
    if actual_files != set(files):
        raise ValueError("certified corpus files do not close over the manifest")
    return _VerifiedFiles(files)


def _jsonl(data: bytes, label: str) -> tuple[dict[str, object], ...]:
    try:
        rows = tuple(json.loads(line) for line in data.decode("utf-8").splitlines() if line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} JSONL is invalid") from exc
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{label} rows must be objects")
    return rows


def _marker_segments(
    lesson_files: Mapping[str, bytes],
) -> dict[str, tuple[str, str]]:
    segments: dict[str, tuple[str, str]] = {}
    for relative, data in sorted(lesson_files.items()):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("lesson markdown must be UTF-8") from exc
        matches = tuple(_MARKER_RE.finditer(text))
        for index, match in enumerate(matches):
            try:
                payload = json.loads(match.group("payload"))
            except json.JSONDecodeError as exc:
                raise ValueError("lesson source marker JSON is invalid") from exc
            if not isinstance(payload, dict) or set(payload) != {"record_id"}:
                raise ValueError("lesson source marker schema is invalid")
            record_id = payload["record_id"]
            if not isinstance(record_id, str) or _SOURCE_ID_RE.fullmatch(record_id) is None:
                raise ValueError("lesson source marker id is invalid")
            if record_id in segments:
                raise ValueError("lesson source marker id is duplicated")
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segments[record_id] = (relative, text[match.end() : end])
    return segments


def _recover_raw_text(segment: str, expected_sha256: str) -> str:
    candidates = [segment]
    candidate = segment
    for _ in range(4):
        if not candidate.endswith("\n"):
            break
        candidate = candidate[:-1]
        candidates.append(candidate)
    matches = tuple(
        value
        for value in candidates
        if hashlib.sha256(value.encode("utf-8")).hexdigest() == expected_sha256
    )
    if len(matches) != 1:
        raise ValueError("lesson raw text cannot be recovered uniquely from its marker")
    return matches[0]


def _normalized_sha256(text: str) -> str:
    normalized = (
        unicodedata.normalize("NFKC", text)
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip()
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _evidence_id(record_id: str) -> str:
    return "evidence:" + hashlib.sha256(record_id.encode("utf-8")).hexdigest()


def _image_evidence_id(record_id: str) -> str:
    return "image:" + hashlib.sha256(record_id.encode("utf-8")).hexdigest()


def load_certified_lesson_corpus(
    root: Path,
    *,
    policy: CertifiedCorpusPolicy | None = None,
) -> CertifiedLessonCorpus:
    policy = policy or CertifiedCorpusPolicy()
    if not isinstance(policy, CertifiedCorpusPolicy):
        raise TypeError("policy must be CertifiedCorpusPolicy")
    root_path = _absolute_without_resolving(Path(root))
    _safe_directory(root_path, "certified corpus root")
    manifest, manifest_bytes = _read_manifest(root_path)
    if hashlib.sha256(manifest_bytes).hexdigest() != policy.manifest_sha256:
        raise ValueError("certified corpus manifest fingerprint mismatch")
    if manifest.get("package_kind") != "chanlun_lesson_corpus" or manifest.get("schema_version") != 3:
        raise ValueError("certified corpus kind or schema is invalid")
    if manifest.get("status") != {
        "blockers": [],
        "integrity": "certified",
        "original_evidence": "available",
    }:
        raise ValueError("certified corpus status gate is closed")
    certification = manifest.get("certification")
    if not isinstance(certification, dict) or (
        certification.get("byte_identical_double_build") is not True
        or certification.get("source_identity_reverified") is not True
        or certification.get("determinism_builds") != 2
    ):
        raise ValueError("certified corpus certification evidence is incomplete")
    semantic = _semantic_certification(certification)
    source_pdf = manifest.get("source_pdf")
    if not isinstance(source_pdf, dict) or (
        source_pdf.get("sha256") != policy.source_pdf_sha256
        or source_pdf.get("size_bytes") != policy.source_pdf_size_bytes
        or source_pdf.get("page_count") != policy.source_pdf_page_count
    ):
        raise ValueError("certified corpus source PDF policy mismatch")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("lesson_count") != policy.lesson_count:
        raise ValueError("certified corpus lesson coverage policy mismatch")
    inventory = manifest.get("inventory")
    if not isinstance(inventory, dict) or (
        inventory.get("image_asset_count") != policy.image_asset_count
        or inventory.get("image_occurrence_count") != policy.image_occurrence_count
        or inventory.get("image_role_counts") != dict(policy.image_role_counts)
        or inventory.get("materialized_chart_asset_count")
        != policy.materialized_chart_asset_count
    ):
        raise ValueError("certified corpus image inventory policy mismatch")

    files = _verified_files(root_path, manifest)
    lesson_files = {
        relative: files[relative]
        for relative in files
        if re.fullmatch(r"L\d{3}_.+\.md", PurePosixPath(relative).name)
        and len(PurePosixPath(relative).parts) == 1
    }
    if len(lesson_files) != policy.lesson_count:
        raise ValueError("certified corpus lesson file count mismatch")
    required = {
        "source_map.jsonl",
        "inventory/image_assets.jsonl",
        "inventory/image_occurrences.jsonl",
        "inventory/text_spans.jsonl",
    }
    if not required.issubset(files):
        raise ValueError("certified corpus required provenance inventory is missing")
    source_rows = _jsonl(files["source_map.jsonl"], "source map")
    if manifest.get("source_record_count") != len(source_rows):
        raise ValueError("certified corpus source record count mismatch")
    record_ids = tuple(row.get("record_id") for row in source_rows)
    if any(not isinstance(value, str) or _SOURCE_ID_RE.fullmatch(value) is None for value in record_ids):
        raise ValueError("certified corpus source record id is invalid")
    if len(set(record_ids)) != len(record_ids):
        raise ValueError("certified corpus source record id is duplicated")
    if any(row.get("source_pdf_sha256") != policy.source_pdf_sha256 for row in source_rows):
        raise ValueError("certified corpus source record PDF hash mismatch")
    role_counts = dict(sorted(Counter(str(row.get("source_role")) for row in source_rows).items()))
    if role_counts != manifest.get("role_counts"):
        raise ValueError("certified corpus source role counts mismatch")
    markers = _marker_segments(lesson_files)
    expected_marker_ids = {
        row["record_id"]
        for row in source_rows
        if row.get("record_type") == "text"
        or (
            row.get("record_type") == "image"
            and row.get("source_role") == "lesson_chart"
        )
    }
    if set(markers) != expected_marker_ids:
        raise ValueError("certified corpus source markers do not close over source_map")

    asset_rows = _jsonl(files["inventory/image_assets.jsonl"], "image assets")
    occurrence_rows = _jsonl(
        files["inventory/image_occurrences.jsonl"], "image occurrences"
    )
    if len(asset_rows) != policy.image_asset_count or len(occurrence_rows) != policy.image_occurrence_count:
        raise ValueError("certified corpus image inventory row count mismatch")
    if sum(int(row.get("raw_size_bytes", -1)) for row in asset_rows) != policy.total_primary_raw_stream_bytes:
        raise ValueError("certified corpus primary image byte count mismatch")
    occurrence_role_counts = dict(
        sorted(Counter(str(row.get("source_role")) for row in occurrence_rows).items())
    )
    if occurrence_role_counts != dict(policy.image_role_counts):
        raise ValueError("certified corpus image role policy mismatch")
    text_role_counts = dict(
        sorted(
            Counter(
                str(row.get("source_role"))
                for row in source_rows
                if row.get("record_type") == "text"
            ).items()
        )
    )
    semantic_role_audit = semantic["role_audit"]
    if (
        semantic["lesson_boundary_count"] != policy.lesson_count
        or semantic["text_cache_sha256"]
        != files.sha256("inventory/text_spans.jsonl")
        or semantic_role_audit["image_role_counts"] != occurrence_role_counts
        or semantic_role_audit["text_role_counts"] != text_role_counts
        or semantic_role_audit["quarantined_unknown_image_count"]
        != occurrence_role_counts.get("unknown_image", 0)
        or semantic_role_audit["quarantined_unknown_text_count"]
        != text_role_counts.get("unknown_text", 0)
    ):
        raise ValueError("certified corpus semantic certification does not match package evidence")
    assets_by_sha: dict[str, dict[str, object]] = {}
    assets_by_xref: dict[int, dict[str, object]] = {}
    for row in asset_rows:
        if row.get("source_pdf_sha256") != policy.source_pdf_sha256:
            raise ValueError("certified corpus image asset PDF hash mismatch")
        sha256 = row.get("raw_sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError("certified corpus image asset hash is invalid")
        if row.get("asset_id") != f"asset:{sha256}":
            raise ValueError("certified corpus image asset id is invalid")
        xref = row.get("xref")
        if isinstance(xref, bool) or not isinstance(xref, int) or xref <= 0 or xref in assets_by_xref:
            raise ValueError("certified corpus image asset xref is invalid or duplicated")
        smask_sha256 = row.get("smask_sha256")
        if smask_sha256 is not None and (
            not isinstance(smask_sha256, str)
            or _SHA256_RE.fullmatch(smask_sha256) is None
        ):
            raise ValueError("certified corpus image SMask hash is invalid")
        assets_by_xref[xref] = row
        assets_by_sha.setdefault(sha256, row)
    occurrences_by_id = {row.get("occurrence_id"): row for row in occurrence_rows}
    if len(occurrences_by_id) != len(occurrence_rows) or any(
        not isinstance(value, str) or _OCCURRENCE_ID_RE.fullmatch(value) is None
        for value in occurrences_by_id
    ):
        raise ValueError("certified corpus image occurrence id is invalid or duplicated")
    if any(
        row.get("source_pdf_sha256") != policy.source_pdf_sha256
        or _CLASSIFICATION_ID_RE.fullmatch(str(row.get("classification_id"))) is None
        or assets_by_xref.get(row.get("xref"), {}).get("raw_sha256")
        != row.get("asset_sha256")
        for row in occurrence_rows
    ):
        raise ValueError("certified corpus image occurrence provenance is invalid")
    course_occurrence_rows = tuple(
        row for row in occurrence_rows if row.get("lesson_number") is not None
    )
    front_matter_occurrence_rows = tuple(
        row for row in occurrence_rows if row.get("lesson_number") is None
    )
    inventory_metadata = manifest.get("inventory")
    course_asset_hashes = {row["asset_sha256"] for row in course_occurrence_rows}
    course_smask_hashes = {
        assets_by_xref[row["xref"]]["smask_sha256"]
        for row in course_occurrence_rows
        if assets_by_xref[row["xref"]].get("smask_sha256") is not None
    }
    if not isinstance(inventory_metadata, dict) or (
        inventory_metadata.get("course_image_occurrence_count")
        != len(course_occurrence_rows)
        or inventory_metadata.get("front_matter_occurrence_count")
        != len(front_matter_occurrence_rows)
        or inventory_metadata.get("archived_course_primary_asset_count")
        != len(course_asset_hashes)
        or inventory_metadata.get("archived_course_smask_asset_count")
        != len(course_smask_hashes)
    ):
        raise ValueError("certified corpus course image archive counts mismatch")
    expected_primary_paths = {
        f"images/assets/{sha256}.jpg" for sha256 in course_asset_hashes
    }
    expected_smask_paths = {
        f"images/smasks/{sha256}.bin" for sha256 in course_smask_hashes
    }
    actual_primary_paths = {
        relative for relative in files if relative.startswith("images/assets/")
    }
    actual_smask_paths = {
        relative for relative in files if relative.startswith("images/smasks/")
    }
    if actual_primary_paths != expected_primary_paths or actual_smask_paths != expected_smask_paths:
        raise ValueError("certified corpus course image files do not close over occurrences")
    if any(
        files.sha256(f"images/smasks/{sha256}.bin") != sha256
        for sha256 in course_smask_hashes
    ):
        raise ValueError("certified corpus SMask archive hash mismatches provenance")

    lessons = manifest.get("lessons")
    if (
        not isinstance(lessons, list)
        or len(lessons) != policy.lesson_count
        or any(not isinstance(item, dict) for item in lessons)
    ):
        raise ValueError("certified corpus lesson metadata is invalid")
    lesson_meta = {item.get("lesson_number"): item for item in lessons}
    if set(lesson_meta) != set(range(policy.lesson_count)):
        raise ValueError("certified corpus lesson metadata coverage mismatch")
    boundary_payload = [
        {
            "lesson_number": lesson_number,
            "page_end": lesson_meta[lesson_number].get("page_end"),
            "page_start": lesson_meta[lesson_number].get("page_start"),
            "title": lesson_meta[lesson_number].get("title"),
        }
        for lesson_number in range(policy.lesson_count)
    ]
    if _canonical_sha256(boundary_payload) != semantic["lesson_boundary_sha256"]:
        raise ValueError("certified corpus lesson boundary fingerprint mismatch")

    raw_text_by_record: dict[str, str] = {}
    row_by_id = {row["record_id"]: row for row in source_rows}
    for record_id, row in row_by_id.items():
        record_type = row.get("record_type")
        role = row.get("source_role")
        if record_type == "text":
            marker_file, segment = markers[record_id]
            if role not in _AUTHORITATIVE_TEXT_ROLES | _QUARANTINED_TEXT_ROLES:
                raise ValueError("certified corpus text source role is invalid")
            if marker_file != row.get("output_path"):
                raise ValueError("certified corpus text marker output path mismatch")
            expected_hash = row.get("content_sha256")
            if not isinstance(expected_hash, str) or _SHA256_RE.fullmatch(expected_hash) is None:
                raise ValueError("certified corpus text content hash is invalid")
            raw_text = _recover_raw_text(segment, expected_hash)
            if _normalized_sha256(raw_text) != row.get("normalized_text_sha256"):
                raise ValueError("certified corpus normalized text hash mismatch")
            raw_text_by_record[record_id] = raw_text
        elif record_type == "image":
            if role not in {"lesson_chart", "editor_image", "unknown_image"}:
                raise ValueError("certified corpus image source role is invalid")
            output_path = _safe_relative(row.get("output_path"))
            if output_path not in files:
                raise ValueError("certified corpus image archive path is missing")
            if files.sha256(output_path) != row.get("content_sha256"):
                raise ValueError("certified corpus image content hash mismatch")
            if role == "lesson_chart":
                _, segment = markers[record_id]
                if f"]({output_path})" not in segment:
                    raise ValueError("certified corpus lesson chart marker is invalid")
            elif record_id in markers or row.get("caption_record_id") is not None:
                raise ValueError("quarantined image leaked into lesson markdown or caption linkage")
        else:
            raise ValueError("certified corpus source record type is invalid")

    image_ids_by_caption: dict[str, list[str]] = defaultdict(list)
    images_by_id: dict[str, ImageEvidence] = {}
    image_rows = tuple(row for row in source_rows if row.get("record_type") == "image")
    if {row.get("source_object_id") for row in image_rows} != {
        row.get("occurrence_id") for row in course_occurrence_rows
    } or len(image_rows) != len(course_occurrence_rows):
        raise ValueError("certified corpus source map does not close over course image occurrences")
    for row in image_rows:
        occurrence_id = row.get("source_object_id")
        occurrence = occurrences_by_id.get(occurrence_id)
        if occurrence is None or (
            occurrence.get("source_role") != row.get("source_role")
            or occurrence.get("asset_sha256") != row.get("content_sha256")
            or occurrence.get("lesson_number") != row.get("lesson_number")
            or occurrence.get("page_number") != row.get("page_number")
            or occurrence.get("classification_id") != row.get("classification_id")
            or occurrence.get("reason_codes") != row.get("reason_codes")
            or occurrence.get("xobject_name") != row.get("xobject_name")
            or occurrence.get("xref") != row.get("xref")
            or row.get("occurrence_id") != occurrence_id
        ):
            raise ValueError("certified corpus image occurrence link is invalid")
        asset = assets_by_xref.get(occurrence.get("xref"))
        sha256 = row["content_sha256"]
        if asset is None or (
            asset.get("raw_sha256") != sha256
            or row.get("asset_id") != asset.get("asset_id")
            or row.get("asset_sha256") != sha256
            or row.get("output_path") != f"images/assets/{sha256}.jpg"
            or row.get("smask_sha256") != asset.get("smask_sha256")
            or row.get("smask_output_path")
            != (
                f"images/smasks/{asset['smask_sha256']}.bin"
                if asset.get("smask_sha256") is not None
                else None
            )
        ):
            raise ValueError("certified corpus image asset provenance is invalid")
        if row.get("source_role") != "lesson_chart":
            continue
        if occurrence.get("caption_page_number") != row.get("page_number"):
            raise ValueError("certified corpus chart occurrence page link is invalid")
        caption_record_id = row.get("caption_record_id")
        caption_row = row_by_id.get(caption_record_id)
        if caption_row is None or (
            caption_row.get("source_role") not in _AUTHORITATIVE_TEXT_ROLES
            or caption_row.get("lesson_number") != row.get("lesson_number")
            or caption_row.get("page_number") != occurrence.get("caption_page_number")
            or caption_row.get("source_sequence_index")
            != occurrence.get("caption_source_sequence_index")
        ):
            raise ValueError("certified corpus chart caption link is invalid")
        pixel_size = asset.get("pixel_size")
        if not isinstance(pixel_size, list) or len(pixel_size) != 2:
            raise ValueError("certified corpus chart pixel size is invalid")
        image_id = _image_evidence_id(row["record_id"])
        image_ids_by_caption[caption_record_id].append(image_id)
        if image_id in images_by_id:
            raise ValueError("certified corpus image evidence id is duplicated")
        images_by_id[image_id] = ImageEvidence(
                image_id=image_id,
                source_tier=SourceTier.LESSON_CHART,
                source_path=row["output_path"],
                sha256=f"sha256:{sha256}",
                media_type="image/jpeg",
                width=int(pixel_size[0]),
                height=int(pixel_size[1]),
                alt_text=f"第 {row['lesson_number']} 课 PDF 第 {row['page_number']} 页原文图",
                source_role="lesson_chart",
                source_record_id=row["record_id"],
                source_pdf_sha256=policy.source_pdf_sha256,
                page_number=row["page_number"],
                bbox=tuple(row["bbox"]),
                caption_record_id=caption_record_id,
                asset_id=f"sha256:{sha256}",
                occurrence_id=occurrence_id,
            )

    units = []
    for row in source_rows:
        if row.get("record_type") != "text" or row.get("source_role") not in _AUTHORITATIVE_TEXT_ROLES:
            continue
        lesson_number = row.get("lesson_number")
        metadata = lesson_meta.get(lesson_number)
        if metadata is None:
            raise ValueError("certified corpus authoritative text lesson metadata is missing")
        record_id = row["record_id"]
        units.append(
            EvidenceUnit(
                evidence_id=_evidence_id(record_id),
                source_tier=SourceTier.LESSON_ORIGINAL,
                source_path=row["output_path"],
                title=str(metadata["title"]),
                text=raw_text_by_record[record_id],
                sha256=row["content_sha256"],
                lesson=lesson_number,
                image_ids=tuple(dict.fromkeys(image_ids_by_caption.get(record_id, ()))),
                source_role=row["source_role"],
                source_record_id=record_id,
                source_pdf_sha256=policy.source_pdf_sha256,
                page_number=row["page_number"],
                bbox=tuple(row["bbox"]),
                source_sequence_index=row["source_sequence_index"],
                block_index=row["block_index"],
                source_record_ids=(record_id,),
            )
        )
    units.sort(
        key=lambda unit: (
            unit.lesson,
            unit.page_number,
            unit.source_sequence_index,
            unit.block_index,
        )
    )
    images = tuple(sorted(images_by_id.values(), key=lambda image: image.image_id))
    if len(images) != policy.image_role_counts.get("lesson_chart", 0):
        raise ValueError("certified corpus chart occurrence evidence count mismatch")
    if len({image.asset_id for image in images}) != policy.materialized_chart_asset_count:
        raise ValueError("certified corpus materialized chart asset count mismatch")
    return CertifiedLessonCorpus(
        root=root_path,
        units=tuple(units),
        semantic_units=build_semantic_units(tuple(units)),
        images=images,
        manifest_sha256=hashlib.sha256(manifest_bytes).hexdigest(),
        source_pdf_sha256=policy.source_pdf_sha256,
    )
