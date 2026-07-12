from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from typing import Mapping

from .lesson_corpus import (
    LessonBoundary,
    LessonTextBlock,
    PdfIdentity,
    SourceRecord,
    SourceRole,
    _lesson_filename,
    _source_record_dict,
    _write_utf8,
    order_lesson_text_blocks,
    validate_lesson_boundaries,
    verify_pdf_identity,
)
from .lesson_image_cache import LessonImageInventory, load_lesson_image_cache
from .lesson_pdf import (
    LessonTextExtraction,
    classify_lesson_text,
    detect_lesson_boundaries,
)
from .lesson_span_cache import load_lesson_span_cache


TRUSTED_IDENTITY = PdfIdentity(
    filename="教你炒股票108课天使版.pdf",
    size_bytes=1_352_725_597,
    page_count=2_533,
    sha256="867b1262af2d3430b98421df4c5372748eb75a4eb7600cd967ecdc374817429e",
)
PACKAGE_VERSION = "lesson-package/3"
SEMANTIC_AUDIT_VERSION = "chanlun-semantic-audit/1"
EXPECTED_IMAGE_ASSETS = 2_783
EXPECTED_IMAGE_OCCURRENCES = 2_816
EXPECTED_IMAGE_PRIMARY_RAW_BYTES = 1_343_589_074
EXPECTED_IMAGE_ROLE_COUNTS = {
    "editor_image": 102,
    "lesson_chart": 105,
    "unknown_image": 2_609,
}
EXPECTED_MATERIALIZED_CHART_ASSETS = 103
EXPECTED_TEXT_CACHE_SHA256 = "f40f72744a44ada28f1f66e72ca9e1fdbe58f09ce0301df55c8d7f99af6e4aae"
EXPECTED_TEXT_SPAN_COUNT = 57_841
EXPECTED_LESSON_BOUNDARY_SHA256 = "b46b1915ff89700017e73526f151b6f0243ffbab4ac39257da58fff31c01cef3"
EXPECTED_TEXT_ROLE_COUNTS = {
    "chan_reply": 6_332,
    "editor_note": 5_988,
    "lesson_body": 26_524,
    "reader_comment": 9_309,
    "unknown_text": 4_634,
}
EXPECTED_REPLY_RESOLUTION_COUNTS = {
    "ambiguous": 323,
    "reader_only": 24,
    "reader_then_author": 1_283,
    "standalone_author": 25,
}
EXPECTED_SKIPPED_RUNNING_MATTER_COUNT = 5_054
EXPECTED_SEMANTIC_CLASSIFICATION_SHA256 = (
    "7982126c8e3676f4cac5f2d185d308e85b18143c22cd4f64f6874c9e39954f40"
)
_AUTHORITATIVE_TEXT_ROLES = frozenset(
    {SourceRole.LESSON_BODY, SourceRole.CHAN_REPLY, SourceRole.CHAN_EXCERPT}
)


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_count_map(value: Mapping[str, int], field: str) -> dict[str, int]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping")
    result = dict(value)
    if any(
        not isinstance(key, str)
        or not key
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
        for key, count in result.items()
    ):
        raise ValueError(f"{field} must contain non-negative integer counts")
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class SemanticAuditPolicy:
    source_pdf: PdfIdentity
    text_cache_sha256: str
    text_span_count: int
    lesson_boundary_sha256: str
    lesson_boundary_count: int
    text_role_counts: Mapping[str, int]
    image_role_counts: Mapping[str, int]
    reply_resolution_counts: Mapping[str, int]
    skipped_running_matter_count: int
    classification_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_pdf, PdfIdentity):
            raise TypeError("source_pdf must be PdfIdentity")
        for field in ("text_cache_sha256", "lesson_boundary_sha256", "classification_sha256"):
            value = getattr(self, field)
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ValueError(f"{field} must be a lowercase SHA-256")
        for field in (
            "text_span_count",
            "lesson_boundary_count",
            "skipped_running_matter_count",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field} must be a non-negative integer")
        object.__setattr__(
            self,
            "text_role_counts",
            _validated_count_map(self.text_role_counts, "text_role_counts"),
        )
        object.__setattr__(
            self,
            "image_role_counts",
            _validated_count_map(self.image_role_counts, "image_role_counts"),
        )
        object.__setattr__(
            self,
            "reply_resolution_counts",
            _validated_count_map(self.reply_resolution_counts, "reply_resolution_counts"),
        )


def trusted_semantic_audit_policy() -> SemanticAuditPolicy:
    return SemanticAuditPolicy(
        source_pdf=TRUSTED_IDENTITY,
        text_cache_sha256=EXPECTED_TEXT_CACHE_SHA256,
        text_span_count=EXPECTED_TEXT_SPAN_COUNT,
        lesson_boundary_sha256=EXPECTED_LESSON_BOUNDARY_SHA256,
        lesson_boundary_count=109,
        text_role_counts=EXPECTED_TEXT_ROLE_COUNTS,
        image_role_counts=EXPECTED_IMAGE_ROLE_COUNTS,
        reply_resolution_counts=EXPECTED_REPLY_RESOLUTION_COUNTS,
        skipped_running_matter_count=EXPECTED_SKIPPED_RUNNING_MATTER_COUNT,
        classification_sha256=EXPECTED_SEMANTIC_CLASSIFICATION_SHA256,
    )


@dataclass(frozen=True)
class SemanticCertificationResult:
    payload: dict[str, object]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]


def lesson_boundary_sha256(
    boundaries: tuple[LessonBoundary, ...] | list[LessonBoundary],
) -> str:
    values = tuple(boundaries)
    if any(not isinstance(item, LessonBoundary) for item in values):
        raise TypeError("boundaries must contain LessonBoundary values")
    return _canonical_sha256(
        [
            {
                "lesson_number": item.lesson_number,
                "page_end": item.page_end,
                "page_start": item.page_start,
                "title": item.title,
            }
            for item in values
        ]
    )


def _ordered_extractions(
    boundaries: tuple[LessonBoundary, ...],
    extractions: tuple[LessonTextExtraction, ...] | list[LessonTextExtraction],
) -> tuple[tuple[int, LessonTextExtraction], ...]:
    values = tuple(extractions)
    if len(values) != len(boundaries) or any(
        not isinstance(item, LessonTextExtraction) for item in values
    ):
        raise ValueError("semantic audit requires one text extraction per lesson boundary")
    ordered: list[tuple[int, LessonTextExtraction]] = []
    for boundary, extraction in zip(boundaries, values):
        if not extraction.blocks or any(
            block.lesson_number != boundary.lesson_number
            or not boundary.page_start <= block.page_number <= boundary.page_end
            for block in extraction.blocks
        ):
            raise ValueError("text extraction does not match its lesson boundary")
        if (
            extraction.reply_record_count != len(extraction.reply_records)
            or extraction.closed_reply_record_count
            + extraction.ambiguous_reply_record_count
            != extraction.reply_record_count
            or extraction.ambiguous_reply_record_count
            != sum(record.resolution == "ambiguous" for record in extraction.reply_records)
            or tuple(record.record_index for record in extraction.reply_records)
            != tuple(range(len(extraction.reply_records)))
        ):
            raise ValueError("text extraction reply audit counts are inconsistent")
        ordered.append((boundary.lesson_number, extraction))
    return tuple(ordered)


def semantic_classification_sha256(
    *,
    boundaries: tuple[LessonBoundary, ...] | list[LessonBoundary],
    extractions: tuple[LessonTextExtraction, ...] | list[LessonTextExtraction],
    image_inventory: LessonImageInventory,
) -> str:
    boundary_values = tuple(boundaries)
    ordered = _ordered_extractions(boundary_values, extractions)
    if not isinstance(image_inventory, LessonImageInventory):
        raise TypeError("image_inventory must be LessonImageInventory")
    text_rows = []
    reply_rows = []
    for lesson_number, extraction in ordered:
        for block in sorted(
            extraction.blocks,
            key=lambda item: (item.page_number, item.source_sequence_index),
        ):
            text_rows.append(
                {
                    "content_sha256": block.raw_sha256,
                    "lesson_number": lesson_number,
                    "page_number": block.page_number,
                    "source_role": block.source_role.value,
                    "source_sequence_index": block.source_sequence_index,
                }
            )
        for record in extraction.reply_records:
            reply_rows.append(
                {
                    "author_source_positions": [
                        list(item)
                        for item in getattr(record, "author_source_positions", ())
                    ],
                    "lesson_number": lesson_number,
                    "quarantined_source_positions": [
                        list(item)
                        for item in getattr(record, "quarantined_source_positions", ())
                    ],
                    "reader_source_positions": [
                        list(item)
                        for item in getattr(record, "reader_source_positions", ())
                    ],
                    "reason_codes": list(record.reason_codes),
                    "record_index": record.record_index,
                    "resolution": record.resolution,
                    "separator_source_positions": [
                        list(item)
                        for item in getattr(record, "separator_source_positions", ())
                    ],
                    "source_positions": [list(item) for item in record.source_positions],
                }
            )
    image_rows = [
        {
            "asset_sha256": item.asset_sha256,
            "classification_id": item.classification_id,
            "lesson_number": item.lesson_number,
            "occurrence_id": item.occurrence_id,
            "reason_codes": list(item.reason_codes),
            "source_role": item.source_role.value,
        }
        for item in sorted(
            image_inventory.occurrences,
            key=lambda item: (item.page_number, item.draw_index, item.occurrence_id),
        )
    ]
    return _canonical_sha256(
        {"images": image_rows, "replies": reply_rows, "text": text_rows}
    )


def _reply_provenance_audit(
    extractions: tuple[tuple[int, LessonTextExtraction], ...],
) -> dict[str, int]:
    reader_leak_count = 0
    quarantined_leak_count = 0
    anonymous_chan_reply_count = 0
    incomplete_count = 0
    for _, extraction in extractions:
        blocks_by_position = {
            (block.page_number, block.source_sequence_index): block
            for block in extraction.blocks
        }
        if len(blocks_by_position) != len(extraction.blocks):
            incomplete_count += len(extraction.blocks) - len(blocks_by_position)
        author_positions_for_extraction: set[tuple[int, int]] = set()
        seen_record_positions: set[tuple[int, int]] = set()
        for record in extraction.reply_records:
            source_positions = tuple(record.source_positions)
            reader_positions = tuple(
                getattr(record, "reader_source_positions", ())
            )
            author_positions = tuple(
                getattr(record, "author_source_positions", ())
            )
            quarantined_positions = tuple(
                getattr(record, "quarantined_source_positions", ())
            )
            separator_positions = tuple(
                getattr(record, "separator_source_positions", ())
            )
            source_set = set(source_positions)
            membership = Counter(
                (
                    *reader_positions,
                    *author_positions,
                    *quarantined_positions,
                    *separator_positions,
                )
            )
            incomplete_count += len(source_set.intersection(seen_record_positions))
            seen_record_positions.update(source_set)
            incomplete_count += sum(position not in blocks_by_position for position in source_set)
            incomplete_count += len(source_set.difference(membership))
            incomplete_count += sum(
                max(1, count - 1)
                for position, count in membership.items()
                if position not in source_set or count != 1
            )
            reader_leak_count += sum(
                blocks_by_position[position].source_role in _AUTHORITATIVE_TEXT_ROLES
                for position in reader_positions
                if position in blocks_by_position
            )
            quarantined_leak_count += sum(
                blocks_by_position[position].source_role in _AUTHORITATIVE_TEXT_ROLES
                for position in quarantined_positions
                if position in blocks_by_position
            )
            author_positions_for_extraction.update(author_positions)
        anonymous_chan_reply_count += sum(
            block.source_role is SourceRole.CHAN_REPLY
            and position not in author_positions_for_extraction
            for position, block in blocks_by_position.items()
        )
    return {
        "anonymous_chan_reply_count": anonymous_chan_reply_count,
        "quarantined_text_authoritative_leak_count": quarantined_leak_count,
        "reader_authoritative_leak_count": reader_leak_count,
        "reply_provenance_incomplete_count": incomplete_count,
    }


def build_semantic_certification(
    *,
    identity: PdfIdentity,
    boundaries: tuple[LessonBoundary, ...] | list[LessonBoundary],
    extractions: tuple[LessonTextExtraction, ...] | list[LessonTextExtraction],
    image_inventory: LessonImageInventory,
    text_cache_sha256: str,
    text_span_count: int,
    policy: SemanticAuditPolicy,
) -> SemanticCertificationResult:
    if not isinstance(policy, SemanticAuditPolicy):
        raise TypeError("policy must be SemanticAuditPolicy")
    if identity != policy.source_pdf:
        raise ValueError("source PDF identity does not match semantic audit policy")
    if text_cache_sha256 != policy.text_cache_sha256:
        raise ValueError("text cache fingerprint does not match semantic audit policy")
    if text_span_count != policy.text_span_count:
        raise ValueError("text span count does not match semantic audit policy")
    boundary_values = tuple(boundaries)
    if (
        len(boundary_values) != policy.lesson_boundary_count
        or lesson_boundary_sha256(boundary_values) != policy.lesson_boundary_sha256
    ):
        raise ValueError("lesson boundaries do not match semantic audit policy")
    ordered = _ordered_extractions(boundary_values, extractions)
    if not isinstance(image_inventory, LessonImageInventory):
        raise TypeError("image_inventory must be LessonImageInventory")

    blocks = tuple(block for _, extraction in ordered for block in extraction.blocks)
    reply_records = tuple(
        record for _, extraction in ordered for record in extraction.reply_records
    )
    ambiguous_count = sum(
        extraction.ambiguous_reply_record_count for _, extraction in ordered
    )
    if ambiguous_count != sum(
        record.resolution == "ambiguous" for record in reply_records
    ):
        raise ValueError("ambiguous reply audit count is inconsistent")
    text_role_counts = dict(
        sorted(Counter(block.source_role.value for block in blocks).items())
    )
    image_role_counts = dict(
        sorted(
            Counter(item.source_role.value for item in image_inventory.occurrences).items()
        )
    )
    reply_resolution_counts = dict(
        sorted(Counter(record.resolution for record in reply_records).items())
    )
    skipped_running_matter_count = sum(
        extraction.skipped_running_matter_count for _, extraction in ordered
    )
    classification_sha256 = semantic_classification_sha256(
        boundaries=boundary_values,
        extractions=tuple(extraction for _, extraction in ordered),
        image_inventory=image_inventory,
    )
    occurrence_ids = tuple(item.occurrence_id for item in image_inventory.occurrences)
    reply_provenance = _reply_provenance_audit(ordered)
    role_audit = {
        "ambiguous_reply_record_count": ambiguous_count,
        "anonymous_chan_reply_count": reply_provenance[
            "anonymous_chan_reply_count"
        ],
        "classification_sha256": classification_sha256,
        "image_role_counts": image_role_counts,
        "image_provenance_incomplete_count": len(occurrence_ids)
        - len(set(occurrence_ids)),
        "quarantined_text_authoritative_leak_count": reply_provenance[
            "quarantined_text_authoritative_leak_count"
        ],
        "quarantined_unknown_image_count": image_role_counts.get(
            SourceRole.UNKNOWN_IMAGE.value, 0
        ),
        "quarantined_unknown_text_count": text_role_counts.get(
            SourceRole.UNKNOWN_TEXT.value, 0
        ),
        "reader_authoritative_leak_count": reply_provenance[
            "reader_authoritative_leak_count"
        ],
        "reply_resolution_counts": reply_resolution_counts,
        "reply_provenance_incomplete_count": reply_provenance[
            "reply_provenance_incomplete_count"
        ],
        "skipped_running_matter_count": skipped_running_matter_count,
        "text_role_counts": text_role_counts,
        "unknown_image_authoritative_leak_count": 0,
    }
    blockers = []
    for field, blocker in (
        ("reader_authoritative_leak_count", "reader_authoritative_leak"),
        (
            "quarantined_text_authoritative_leak_count",
            "quarantined_text_authoritative_leak",
        ),
        ("anonymous_chan_reply_count", "anonymous_chan_reply"),
        ("reply_provenance_incomplete_count", "reply_provenance_incomplete"),
        ("image_provenance_incomplete_count", "image_provenance_incomplete"),
        (
            "unknown_image_authoritative_leak_count",
            "unknown_image_authoritative_leak",
        ),
    ):
        count = role_audit[field]
        if count:
            blockers.append(f"{blocker}:{count}")
    for actual, expected, blocker in (
        (text_role_counts, policy.text_role_counts, "text_role_counts_mismatch"),
        (image_role_counts, policy.image_role_counts, "image_role_counts_mismatch"),
        (
            reply_resolution_counts,
            policy.reply_resolution_counts,
            "reply_resolution_counts_mismatch",
        ),
        (
            skipped_running_matter_count,
            policy.skipped_running_matter_count,
            "skipped_running_matter_count_mismatch",
        ),
        (classification_sha256, policy.classification_sha256, "classification_mismatch"),
    ):
        if actual != expected:
            blockers.append(blocker)
    blocker_values = tuple(blockers)
    warnings = tuple(
        f"{warning}:{count}"
        for count, warning in (
            (ambiguous_count, "ambiguous_reply_records"),
            (
                role_audit["quarantined_unknown_text_count"],
                "quarantined_unknown_text",
            ),
            (
                role_audit["quarantined_unknown_image_count"],
                "quarantined_unknown_image",
            ),
        )
        if count
    )
    payload = {
        "lesson_boundary_count": len(boundary_values),
        "lesson_boundary_sha256": lesson_boundary_sha256(boundary_values),
        "role_audit": role_audit,
        "semantic_audit_version": SEMANTIC_AUDIT_VERSION,
        "semantic_gate_passed": not blocker_values,
        "semantic_warnings": list(warnings),
        "text_cache_sha256": text_cache_sha256,
        "text_span_count": text_span_count,
        "thresholds": {
            "anonymous_chan_reply_count_max": 0,
            "image_provenance_incomplete_count_max": 0,
            "quarantined_text_authoritative_leak_count_max": 0,
            "reader_authoritative_leak_count_max": 0,
            "reply_provenance_incomplete_count_max": 0,
            "unknown_image_authoritative_leak_count_max": 0,
        },
    }
    return SemanticCertificationResult(
        payload=payload,
        blockers=blocker_values,
        warnings=warnings,
    )


def _safe_inventory_path(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 for character in value)
    ):
        return False
    path = PurePosixPath(value)
    return (
        path.as_posix() == value
        and not path.is_absolute()
        and ".." not in path.parts
        and len(path.parts) > 1
        and path.parts[0] == "inventory"
    )


def _stream_file_fingerprint(
    path: Path,
    *,
    chunk_size: int = 8 * 1024 * 1024,
) -> tuple[int, str]:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    after = path.stat()
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError("package file changed while being fingerprinted")
    return after.st_size, digest.hexdigest()


def _stream_file_entry(path: Path, root: Path) -> dict[str, object]:
    size_bytes, sha256 = _stream_file_fingerprint(path)
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256,
        "size_bytes": size_bytes,
    }


def _marker(record: SourceRecord) -> str:
    payload = json.dumps(
        {"record_id": record.record_id},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"<!-- chanlun-source {payload} -->"


def _verify_rendered_package(root: Path, expected_record_count: int) -> None:
    if len(tuple(root.glob("L*.md"))) != 109:
        raise RuntimeError("certified package must contain 109 lesson files")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != {
        "blockers": [],
        "integrity": "certified",
        "original_evidence": "available",
    }:
        raise RuntimeError("certified package status is invalid")
    rows = (root / "source_map.jsonl").read_text(encoding="utf-8").splitlines()
    if len(rows) != expected_record_count:
        raise RuntimeError("certified package source record count mismatch")
    for line in rows:
        json.loads(line)
    for entry in manifest.get("files", ()): 
        path = root / entry["path"]
        size_bytes, sha256 = _stream_file_fingerprint(path)
        if size_bytes != entry["size_bytes"] or sha256 != entry["sha256"]:
            raise RuntimeError("certified package file hash mismatch")


def _render_lesson_package_tree(
    root: Path,
    *,
    identity: PdfIdentity,
    boundaries: tuple[LessonBoundary, ...] | list[LessonBoundary],
    text_blocks: tuple[LessonTextBlock, ...] | list[LessonTextBlock],
    image_inventory: LessonImageInventory,
    extractor_versions: dict[str, str],
    certification: dict[str, object],
    inventory_files: dict[str, bytes],
    expected_first_page: int = 7,
    expected_last_page: int = 2533,
) -> Path:
    if not isinstance(identity, PdfIdentity):
        raise TypeError("identity must be PdfIdentity")
    validated_boundaries = validate_lesson_boundaries(
        boundaries,
        expected_first_page=expected_first_page,
        expected_last_page=expected_last_page,
    )
    if identity.page_count != expected_last_page:
        raise ValueError("lesson coverage must end at the verified PDF page count")
    if not isinstance(image_inventory, LessonImageInventory):
        raise TypeError("image_inventory must be LessonImageInventory")
    if set(extractor_versions) != {"text", "image", "package"} or any(
        not isinstance(value, str) or not value.strip() for value in extractor_versions.values()
    ):
        raise ValueError("extractor_versions must identify text, image, and package")
    ordered_blocks = order_lesson_text_blocks(text_blocks)
    boundary_by_lesson = {item.lesson_number: item for item in validated_boundaries}
    for block in ordered_blocks:
        boundary = boundary_by_lesson[block.lesson_number]
        if not boundary.page_start <= block.page_number <= boundary.page_end:
            raise ValueError("text block is outside its lesson boundary")
    for boundary in validated_boundaries:
        if not any(
            block.lesson_number == boundary.lesson_number
            and block.source_role in _AUTHORITATIVE_TEXT_ROLES
            for block in ordered_blocks
        ):
            raise ValueError("every lesson must contain authoritative original text")

    root_path = Path(root).absolute()
    if root_path.exists():
        raise FileExistsError(f"render root already exists: {root_path}")
    root_path.mkdir(parents=True)
    try:
        lesson_filenames = {
            boundary.lesson_number: _lesson_filename(boundary)
            for boundary in validated_boundaries
        }
        text_rows_by_lesson: dict[int, list[tuple[LessonTextBlock, SourceRecord]]] = defaultdict(list)
        text_record_by_position: dict[tuple[int, int], tuple[LessonTextBlock, SourceRecord]] = {}
        records: list[SourceRecord] = []
        for boundary in validated_boundaries:
            lesson_blocks = tuple(
                block for block in ordered_blocks if block.lesson_number == boundary.lesson_number
            )
            for block_index, block in enumerate(lesson_blocks):
                record = SourceRecord.create(
                    record_type="text",
                    lesson_number=block.lesson_number,
                    page_number=block.page_number,
                    bbox=block.bbox,
                    page_size=block.page_size,
                    coordinate_system="pdf_top_left_pt",
                    page_rotation=block.page_rotation,
                    color_rgb=block.color_rgb,
                    source_role=block.source_role,
                    content_sha256=block.raw_sha256,
                    normalized_text_sha256=block.normalized_sha256,
                    source_pdf_sha256=identity.sha256,
                    output_path=lesson_filenames[block.lesson_number],
                    source_sequence_index=block.source_sequence_index,
                    block_index=block_index,
                    extractor_version=extractor_versions["text"],
                    cropbox_pdf=block.cropbox_pdf,
                    mediabox_pdf=block.mediabox_pdf,
                )
                position = (block.page_number, block.source_sequence_index)
                if position in text_record_by_position:
                    raise ValueError("text source position must be globally unique")
                text_record_by_position[position] = (block, record)
                text_rows_by_lesson[boundary.lesson_number].append((block, record))
                records.append(record)

        assets_by_xref = {asset.xref: asset for asset in image_inventory.assets}
        primary_paths = (
            image_inventory.archived_primary_paths or image_inventory.materialized_paths
        )
        expected_primary_hashes = {asset.raw_sha256 for asset in image_inventory.assets}
        if set(primary_paths) != expected_primary_hashes:
            raise ValueError("image inventory primary archive does not close over all assets")
        expected_smask_hashes = {
            asset.smask_sha256
            for asset in image_inventory.assets
            if asset.smask_sha256 is not None
        }
        if set(image_inventory.archived_smask_paths) != expected_smask_hashes:
            raise ValueError("image inventory SMask archive does not close over all masks")

        course_occurrences = tuple(
            sorted(
                (
                    item
                    for item in image_inventory.occurrences
                    if item.lesson_number is not None
                ),
                key=lambda item: (
                    item.lesson_number,
                    item.page_number,
                    item.draw_index,
                    item.occurrence_id,
                ),
            )
        )
        front_matter_occurrences = tuple(
            sorted(
                (
                    item
                    for item in image_inventory.occurrences
                    if item.lesson_number is None
                ),
                key=lambda item: (item.page_number, item.draw_index, item.occurrence_id),
            )
        )
        if any(item.page_number >= expected_first_page for item in front_matter_occurrences):
            raise ValueError("unassigned image occurrence is inside lesson coverage")
        selected_occurrences = tuple(
            item
            for item in course_occurrences
            if item.source_role is SourceRole.LESSON_CHART
        )
        image_rows_by_caption: dict[tuple[int, int], list[tuple[object, SourceRecord]]] = defaultdict(list)
        image_provenance_by_record: dict[str, dict[str, object]] = {}
        image_indexes: Counter[int] = Counter()
        for occurrence in course_occurrences:
            boundary = boundary_by_lesson[occurrence.lesson_number]
            if not boundary.page_start <= occurrence.page_number <= boundary.page_end:
                raise ValueError("image occurrence is outside its lesson boundary")
            asset = assets_by_xref.get(occurrence.xref)
            if asset is None or asset.raw_sha256 != occurrence.asset_sha256:
                raise ValueError("image occurrence asset link is invalid")
            caption_record = None
            if occurrence.source_role is SourceRole.LESSON_CHART:
                if occurrence.caption_source_position is None:
                    raise ValueError("lesson chart occurrence must link a caption")
                caption = text_record_by_position.get(occurrence.caption_source_position)
                if caption is None:
                    raise ValueError("lesson chart caption source record is missing")
                caption_block, caption_record = caption
                if (
                    caption_block.lesson_number != occurrence.lesson_number
                    or caption_block.source_role not in _AUTHORITATIVE_TEXT_ROLES
                ):
                    raise ValueError("lesson chart caption is not authoritative in the same lesson")
            elif occurrence.caption_source_position is not None:
                raise ValueError("quarantined image occurrence cannot claim a verified caption")
            output_path = f"images/assets/{asset.raw_sha256}.jpg"
            block_index = image_indexes[occurrence.lesson_number]
            image_indexes[occurrence.lesson_number] += 1
            record = SourceRecord.create(
                record_type="image",
                lesson_number=occurrence.lesson_number,
                page_number=occurrence.page_number,
                bbox=occurrence.bbox_top_left,
                page_size=occurrence.page_size,
                coordinate_system="pdf_top_left_pt",
                page_rotation=occurrence.page_rotation,
                color_rgb=None,
                source_role=occurrence.source_role,
                content_sha256=occurrence.asset_sha256,
                normalized_text_sha256=None,
                source_pdf_sha256=identity.sha256,
                output_path=output_path,
                source_sequence_index=occurrence.draw_index,
                block_index=block_index,
                extractor_version=extractor_versions["image"],
                cropbox_pdf=occurrence.cropbox_pdf,
                mediabox_pdf=occurrence.mediabox_pdf,
                caption_record_id=(
                    caption_record.record_id if caption_record is not None else None
                ),
                source_object_id=occurrence.occurrence_id,
            )
            smask_output_path = (
                f"images/smasks/{asset.smask_sha256}.bin"
                if asset.smask_sha256 is not None
                else None
            )
            image_provenance_by_record[record.record_id] = {
                "asset_id": asset.asset_id,
                "asset_sha256": asset.raw_sha256,
                "classification_id": occurrence.classification_id,
                "occurrence_id": occurrence.occurrence_id,
                "reason_codes": list(occurrence.reason_codes),
                "smask_output_path": smask_output_path,
                "smask_sha256": asset.smask_sha256,
                "xobject_name": occurrence.xobject_name,
                "xref": occurrence.xref,
            }
            if occurrence.source_role is SourceRole.LESSON_CHART:
                image_rows_by_caption[occurrence.caption_source_position].append(
                    (occurrence, record)
                )
            records.append(record)

        lesson_entries = []
        lesson_paths: list[Path] = []
        for boundary in validated_boundaries:
            filename = lesson_filenames[boundary.lesson_number]
            parts = [
                f"# {boundary.title}",
                "",
                f"- Lesson: {boundary.lesson_number}",
                f"- Pages: {boundary.page_start}-{boundary.page_end}",
                f"- Source-PDF-SHA256: {identity.sha256}",
                f"- Text-Extractor-Version: {extractor_versions['text']}",
                f"- Image-Extractor-Version: {extractor_versions['image']}",
                "",
            ]
            for block, record in text_rows_by_lesson[boundary.lesson_number]:
                parts.extend((_marker(record), block.raw_text, ""))
                for occurrence, image_record in sorted(
                    image_rows_by_caption.get(
                        (block.page_number, block.source_sequence_index), ()
                    ),
                    key=lambda item: item[0].draw_index,
                ):
                    parts.extend(
                        (
                            _marker(image_record),
                            f"![lesson_chart p{occurrence.page_number} xref{occurrence.xref}]({image_record.output_path})",
                            "",
                        )
                    )
            lesson_path = root_path / filename
            _write_utf8(lesson_path, "\n".join(parts).rstrip() + "\n")
            lesson_paths.append(lesson_path)
            lesson_entries.append(
                {
                    "filename": filename,
                    "lesson_number": boundary.lesson_number,
                    "page_end": boundary.page_end,
                    "page_start": boundary.page_start,
                    "record_count": sum(
                        1 for record in records if record.lesson_number == boundary.lesson_number
                    ),
                    "sha256": hashlib.sha256(lesson_path.read_bytes()).hexdigest(),
                    "title": boundary.title,
                }
            )

        copied_images: list[Path] = []
        course_asset_hashes = {item.asset_sha256 for item in course_occurrences}
        for sha256 in sorted(course_asset_hashes):
            source_path = primary_paths[sha256]
            source_size, source_sha256 = _stream_file_fingerprint(Path(source_path))
            if source_sha256 != sha256:
                raise ValueError("archived primary image hash mismatch")
            destination = root_path / "images" / "assets" / f"{sha256}.jpg"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination)
            if _stream_file_fingerprint(destination) != (source_size, source_sha256):
                raise RuntimeError("copied primary image fingerprint mismatch")
            copied_images.append(destination)
        course_smask_hashes = {
            assets_by_xref[item.xref].smask_sha256
            for item in course_occurrences
            if assets_by_xref[item.xref].smask_sha256 is not None
        }
        copied_smasks: list[Path] = []
        for sha256 in sorted(course_smask_hashes):
            source_path = image_inventory.archived_smask_paths[sha256]
            source_size, source_sha256 = _stream_file_fingerprint(Path(source_path))
            if source_sha256 != sha256:
                raise ValueError("archived SMask hash mismatch")
            destination = root_path / "images" / "smasks" / f"{sha256}.bin"
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_path, destination)
            if _stream_file_fingerprint(destination) != (source_size, source_sha256):
                raise RuntimeError("copied SMask fingerprint mismatch")
            copied_smasks.append(destination)

        inventory_paths: list[Path] = []
        for relative, data in sorted(inventory_files.items()):
            if not _safe_inventory_path(relative) or not isinstance(data, bytes):
                raise ValueError("inventory_files must contain safe inventory paths and bytes")
            path = (root_path / relative).resolve()
            try:
                path.relative_to(root_path.resolve())
            except ValueError as exc:
                raise ValueError("inventory path escapes package root") from exc
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            inventory_paths.append(path)

        index_lines = [
            "# 缠论第 0-108 课原文证据索引",
            "",
            "| 课号 | 页码 | 标题 | 文件 | SHA-256 |",
            "|---:|---:|---|---|---|",
        ]
        for item in lesson_entries:
            title = str(item["title"]).replace("|", "\\|")
            index_lines.append(
                f"| {item['lesson_number']} | {item['page_start']}-{item['page_end']} | "
                f"{title} | {item['filename']} | {item['sha256']} |"
            )
        index_path = root_path / "_index.md"
        _write_utf8(index_path, "\n".join(index_lines) + "\n")

        records.sort(
            key=lambda record: (
                record.lesson_number,
                record.page_number,
                record.bbox[1],
                record.bbox[0],
                0 if record.record_type == "text" else 1,
                record.source_sequence_index,
                record.record_id,
            )
        )
        source_map_path = root_path / "source_map.jsonl"
        source_map_path.write_bytes(
            b"".join(
                (
                    json.dumps(
                        {
                            **_source_record_dict(record),
                            **image_provenance_by_record.get(record.record_id, {}),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                for record in records
            )
        )

        package_files = tuple(
            sorted(
                (
                    *lesson_paths,
                    *copied_images,
                    *copied_smasks,
                    *inventory_paths,
                    index_path,
                    source_map_path,
                ),
                key=lambda path: path.relative_to(root_path).as_posix(),
            )
        )
        text_role_counts = Counter(
            record.source_role.value for record in records if record.record_type == "text"
        )
        image_role_counts = Counter(
            item.source_role.value for item in image_inventory.occurrences
        )
        course_image_role_counts = Counter(
            item.source_role.value for item in course_occurrences
        )
        manifest = {
            "certification": dict(sorted(certification.items())),
            "coverage": {
                "first_page": expected_first_page,
                "last_page": expected_last_page,
                "lesson_count": len(validated_boundaries),
            },
            "extractor_versions": dict(sorted(extractor_versions.items())),
            "files": [_stream_file_entry(path, root_path) for path in package_files],
            "inventory": {
                "image_asset_count": len(image_inventory.assets),
                "image_occurrence_count": len(image_inventory.occurrences),
                "archived_course_primary_asset_count": len(course_asset_hashes),
                "archived_course_smask_asset_count": len(course_smask_hashes),
                "course_image_occurrence_count": len(course_occurrences),
                "front_matter_occurrence_count": len(front_matter_occurrences),
                "image_role_counts": dict(sorted(image_role_counts.items())),
                "materialized_chart_asset_count": len(
                    {item.asset_sha256 for item in selected_occurrences}
                ),
                "materialized_chart_occurrence_count": len(selected_occurrences),
            },
            "lessons": lesson_entries,
            "package_kind": "chanlun_lesson_corpus",
            "quarantine_counts": {
                "editor_image": course_image_role_counts[SourceRole.EDITOR_IMAGE.value],
                "editor_note": text_role_counts[SourceRole.EDITOR_NOTE.value],
                "reader_comment": text_role_counts[SourceRole.READER_COMMENT.value],
                "unknown_image": course_image_role_counts[SourceRole.UNKNOWN_IMAGE.value],
                "unknown_text": text_role_counts[SourceRole.UNKNOWN_TEXT.value],
            },
            "role_counts": dict(
                sorted(Counter(record.source_role.value for record in records).items())
            ),
            "schema_version": 3,
            "source_pdf": {
                "filename": identity.filename,
                "page_count": identity.page_count,
                "sha256": identity.sha256,
                "size_bytes": identity.size_bytes,
            },
            "source_record_count": len(records),
            "status": {
                "blockers": [],
                "integrity": "certified",
                "original_evidence": "available",
            },
        }
        (root_path / "manifest.json").write_bytes(
            (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
        )
        _verify_rendered_package(root_path, len(records))
        return root_path.resolve()
    except Exception:
        if root_path.exists():
            shutil.rmtree(root_path)
        raise


def _tree_fingerprints(root: Path) -> dict[str, tuple[int, str]]:
    return {
        path.relative_to(root).as_posix(): _stream_file_fingerprint(path)
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _load_extractions(spans, boundaries):
    spans_by_page = defaultdict(list)
    for span in spans:
        spans_by_page[span.page_number].append(span)
    extractions = []
    for boundary in boundaries:
        lesson_spans = tuple(
            span
            for page_number in range(boundary.page_start, boundary.page_end + 1)
            for span in spans_by_page.get(page_number, ())
        )
        extractions.append(
            classify_lesson_text(
                boundary.lesson_number,
                boundary.page_start,
                boundary.page_end,
                lesson_spans,
            )
        )
    return tuple(extractions)


def certify_lesson_package(
    target: Path,
    *,
    pdf_path: Path,
    text_cache_root: Path,
    image_cache_root: Path,
) -> Path:
    target_path = Path(target).absolute()
    if target_path.is_symlink():
        raise ValueError("target must not be a symbolic link")
    if target_path.exists():
        raise FileExistsError(f"target already exists: {target_path}")
    verified = verify_pdf_identity(pdf_path, TRUSTED_IDENTITY)
    spans = load_lesson_span_cache(text_cache_root, expected_identity=verified)
    boundaries = detect_lesson_boundaries(spans)
    extractions = _load_extractions(spans, boundaries)
    blocks = tuple(block for extraction in extractions for block in extraction.blocks)
    image_inventory = load_lesson_image_cache(image_cache_root, expected_identity=verified)
    image_roles = dict(
        sorted(Counter(item.source_role.value for item in image_inventory.occurrences).items())
    )
    primary_raw_bytes = sum(asset.raw_size_bytes for asset in image_inventory.assets)
    if (
        len(image_inventory.assets) != EXPECTED_IMAGE_ASSETS
        or len(image_inventory.occurrences) != EXPECTED_IMAGE_OCCURRENCES
        or primary_raw_bytes != EXPECTED_IMAGE_PRIMARY_RAW_BYTES
        or image_roles != EXPECTED_IMAGE_ROLE_COUNTS
        or len(image_inventory.materialized_paths) != EXPECTED_MATERIALIZED_CHART_ASSETS
    ):
        raise RuntimeError("image evidence inventory does not satisfy certification policy")

    text_manifest_path = Path(text_cache_root) / "manifest.json"
    image_manifest_path = Path(image_cache_root) / "manifest.json"
    text_manifest_bytes = text_manifest_path.read_bytes()
    image_manifest_bytes = image_manifest_path.read_bytes()
    text_manifest = json.loads(text_manifest_bytes.decode("utf-8"))
    image_manifest = json.loads(image_manifest_bytes.decode("utf-8"))
    text_descriptor = text_manifest.get("text_spans")
    if not isinstance(text_descriptor, dict):
        raise RuntimeError("text evidence cache descriptor is missing")
    semantic_certification = build_semantic_certification(
        identity=verified,
        boundaries=boundaries,
        extractions=extractions,
        image_inventory=image_inventory,
        text_cache_sha256=text_descriptor.get("sha256"),
        text_span_count=text_manifest.get("span_count"),
        policy=trusted_semantic_audit_policy(),
    )
    if semantic_certification.blockers:
        raise RuntimeError(
            "semantic evidence certification gate is closed: "
            + ",".join(semantic_certification.blockers)
        )
    inventory_files = {
        "inventory/image_assets.jsonl": (Path(image_cache_root) / "image_assets.jsonl").read_bytes(),
        "inventory/image_cache_manifest.json": image_manifest_bytes,
        "inventory/image_occurrences.jsonl": (Path(image_cache_root) / "image_occurrences.jsonl").read_bytes(),
        "inventory/text_cache_manifest.json": text_manifest_bytes,
        "inventory/text_spans.jsonl": (Path(text_cache_root) / "text_spans.jsonl").read_bytes(),
    }
    extractor_versions = {
        "image": image_manifest["extractor_version"],
        "package": PACKAGE_VERSION,
        "text": text_manifest["extractor_version"],
    }
    certification = {
        "byte_identical_double_build": True,
        "determinism_builds": 2,
        "expected_image_asset_count": EXPECTED_IMAGE_ASSETS,
        "expected_image_occurrence_count": EXPECTED_IMAGE_OCCURRENCES,
        "expected_primary_raw_stream_bytes": EXPECTED_IMAGE_PRIMARY_RAW_BYTES,
        "semantic_audit": semantic_certification.payload,
        "source_identity_reverified": True,
    }

    target_path.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(
        tempfile.mkdtemp(prefix=f".{target_path.name}.certify-", dir=target_path.parent)
    )
    first = workspace / "first"
    second = workspace / "second"
    try:
        _render_lesson_package_tree(
            first,
            identity=verified,
            boundaries=boundaries,
            text_blocks=blocks,
            image_inventory=image_inventory,
            extractor_versions=extractor_versions,
            certification=certification,
            inventory_files=inventory_files,
        )
        _render_lesson_package_tree(
            second,
            identity=verified,
            boundaries=boundaries,
            text_blocks=tuple(reversed(blocks)),
            image_inventory=image_inventory,
            extractor_versions=extractor_versions,
            certification=certification,
            inventory_files=dict(reversed(tuple(inventory_files.items()))),
        )
        if _tree_fingerprints(first) != _tree_fingerprints(second):
            raise RuntimeError("certified package double build is not byte-identical")
        verify_pdf_identity(pdf_path, verified)
        load_lesson_span_cache(text_cache_root, expected_identity=verified)
        load_lesson_image_cache(image_cache_root, expected_identity=verified)
        current_inventory_files = {
            "inventory/image_assets.jsonl": (Path(image_cache_root) / "image_assets.jsonl").read_bytes(),
            "inventory/image_cache_manifest.json": image_manifest_path.read_bytes(),
            "inventory/image_occurrences.jsonl": (Path(image_cache_root) / "image_occurrences.jsonl").read_bytes(),
            "inventory/text_cache_manifest.json": text_manifest_path.read_bytes(),
            "inventory/text_spans.jsonl": (Path(text_cache_root) / "text_spans.jsonl").read_bytes(),
        }
        if current_inventory_files != inventory_files:
            raise RuntimeError("evidence cache changed during package certification")
        os.replace(first, target_path)
        _verify_rendered_package(
            target_path,
            json.loads((target_path / "manifest.json").read_text(encoding="utf-8"))[
                "source_record_count"
            ],
        )
        return target_path.resolve()
    finally:
        if workspace.exists():
            shutil.rmtree(workspace)
