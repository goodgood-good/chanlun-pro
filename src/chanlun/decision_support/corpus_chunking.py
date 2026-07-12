from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Sequence

from .corpus_types import EvidenceUnit, SourceTier


def _physical_key(unit: EvidenceUnit) -> tuple[int, int, int, int, str]:
    if (
        unit.lesson is None
        or unit.page_number is None
        or unit.source_sequence_index is None
        or unit.block_index is None
    ):
        raise ValueError("original evidence must have physical source coordinates")
    return (
        unit.lesson,
        unit.page_number,
        unit.source_sequence_index,
        unit.block_index,
        unit.evidence_id,
    )


def _validated_atomic_units(units: Sequence[EvidenceUnit]) -> tuple[EvidenceUnit, ...]:
    if isinstance(units, (str, bytes)) or not isinstance(units, Sequence):
        raise TypeError("units must be a sequence")
    values = tuple(units)
    if any(not isinstance(unit, EvidenceUnit) for unit in values):
        raise TypeError("units must contain EvidenceUnit values")
    seen_records: set[str] = set()
    for unit in values:
        if unit.source_tier is not SourceTier.LESSON_ORIGINAL:
            raise ValueError("semantic chunking only accepts lesson_original evidence")
        if (
            not unit.source_record_id
            or unit.source_record_ids != (unit.source_record_id,)
        ):
            raise ValueError("original evidence must have one traceable source record")
        if unit.source_record_id in seen_records:
            raise ValueError("source record is duplicated")
        seen_records.add(unit.source_record_id)
        _physical_key(unit)
        if unit.bbox is None or unit.source_role not in {"lesson_body", "chan_reply", "chan_excerpt"}:
            raise ValueError("original evidence provenance is incomplete")
    return tuple(sorted(values, key=_physical_key))


def _same_chunk(left: EvidenceUnit, right: EvidenceUnit) -> bool:
    return (
        left.source_tier is right.source_tier
        and left.lesson == right.lesson
        and left.page_number == right.page_number
        and left.source_path == right.source_path
        and left.title == right.title
        and left.source_role == right.source_role
        and left.source_pdf_sha256 == right.source_pdf_sha256
        and left.author == right.author
        and left.source_url == right.source_url
        and right.source_sequence_index == left.source_sequence_index + 1
        and right.block_index == left.block_index + 1
    )


def _chunk_id(record_ids: tuple[str, ...]) -> str:
    payload = json.dumps(
        record_ids,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "evidence:" + hashlib.sha256(payload).hexdigest()


def _merge(group: tuple[EvidenceUnit, ...]) -> EvidenceUnit:
    if len(group) == 1:
        return group[0]
    text = "\n".join(unit.text for unit in group)
    record_ids = tuple(unit.source_record_id for unit in group)
    bboxes = tuple(unit.bbox for unit in group)
    return replace(
        group[0],
        evidence_id=_chunk_id(record_ids),
        text=text,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        image_ids=tuple(
            dict.fromkeys(image_id for unit in group for image_id in unit.image_ids)
        ),
        concepts=tuple(
            dict.fromkeys(concept for unit in group for concept in unit.concepts)
        ),
        source_record_ids=record_ids,
        bbox=(
            min(bbox[0] for bbox in bboxes),
            min(bbox[1] for bbox in bboxes),
            max(bbox[2] for bbox in bboxes),
            max(bbox[3] for bbox in bboxes),
        ),
    )


def build_semantic_units(
    units: Sequence[EvidenceUnit],
    *,
    max_chars: int = 900,
) -> tuple[EvidenceUnit, ...]:
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    ordered = _validated_atomic_units(units)
    if not ordered:
        return ()

    chunks: list[EvidenceUnit] = []
    current: list[EvidenceUnit] = [ordered[0]]
    current_chars = len(ordered[0].text)
    for unit in ordered[1:]:
        proposed_chars = current_chars + 1 + len(unit.text)
        if _same_chunk(current[-1], unit) and proposed_chars <= max_chars:
            current.append(unit)
            current_chars = proposed_chars
            continue
        chunks.append(_merge(tuple(current)))
        current = [unit]
        current_chars = len(unit.text)
    chunks.append(_merge(tuple(current)))
    return tuple(chunks)
