from __future__ import annotations

from dataclasses import dataclass
import re

from .lesson_corpus import ImagePlacement, LessonTextBlock, SourceRole


_CAPTION_RE = re.compile(r"^图\s*(?:\d+|[一二三四五六七八九十百]+)(?:$|[.:：、])")
_AUTHORITATIVE_TEXT_ROLES = frozenset(
    {SourceRole.LESSON_BODY, SourceRole.CHAN_REPLY, SourceRole.CHAN_EXCERPT}
)


@dataclass(frozen=True)
class ImageEvidenceDecision:
    source_role: SourceRole
    reason_codes: tuple[str, ...]
    caption_source_position: tuple[int, int] | None


def _vertical_gap(
    image_bbox: tuple[float, float, float, float],
    text_bbox: tuple[float, float, float, float],
) -> float:
    _, image_top, _, image_bottom = image_bbox
    _, text_top, _, text_bottom = text_bbox
    if text_top >= image_bottom:
        return text_top - image_bottom
    if text_bottom <= image_top:
        return image_top - text_bottom
    return 0.0


def _horizontal_center_inside(
    image_bbox: tuple[float, float, float, float],
    text_bbox: tuple[float, float, float, float],
) -> bool:
    image_x0, _, image_x1, _ = image_bbox
    text_x0, _, text_x1, _ = text_bbox
    center = (text_x0 + text_x1) / 2.0
    return image_x0 <= center <= image_x1


def _is_editor_context(block: LessonTextBlock) -> bool:
    compact = re.sub(r"\s+", "", block.normalized_text)
    return (
        "流程图" in compact
        or "红色斜体" in compact
        or "绿色斜体" in compact
        or "笔者理解备注" in compact
    )


def _is_black(block: LessonTextBlock) -> bool:
    color = block.color_rgb
    return color is not None and max(color) <= 48 and max(color) - min(color) <= 12


def classify_image_evidence(
    placement: ImagePlacement,
    page_text_blocks: tuple[LessonTextBlock, ...] | list[LessonTextBlock],
) -> ImageEvidenceDecision:
    if not isinstance(placement, ImagePlacement):
        raise TypeError("placement must be ImagePlacement")
    blocks = tuple(page_text_blocks)
    if any(not isinstance(block, LessonTextBlock) for block in blocks):
        raise TypeError("page_text_blocks must contain LessonTextBlock values")
    if any(block.page_number != placement.page_number for block in blocks):
        raise ValueError("all text blocks must be on the image page")

    _, _, _, image_bottom = placement.bbox
    captions = tuple(
        block
        for block in blocks
        if block.source_role in _AUTHORITATIVE_TEXT_ROLES
        and _is_black(block)
        and _CAPTION_RE.match(block.normalized_text) is not None
        and 0.0 <= block.bbox[1] - image_bottom <= 36.0
        and _horizontal_center_inside(placement.bbox, block.bbox)
    )
    if len(captions) == 1:
        caption = captions[0]
        return ImageEvidenceDecision(
            source_role=SourceRole.LESSON_CHART,
            reason_codes=("verified_black_caption_below",),
            caption_source_position=(caption.page_number, caption.source_sequence_index),
        )
    nearby_editor = tuple(
        block
        for block in blocks
        if _vertical_gap(placement.bbox, block.bbox) <= 80.0
        and _horizontal_center_inside(placement.bbox, block.bbox)
        and _is_editor_context(block)
    )
    if nearby_editor:
        return ImageEvidenceDecision(
            source_role=SourceRole.EDITOR_IMAGE,
            reason_codes=("editor_flowchart_context",),
            caption_source_position=None,
        )
    if len(captions) > 1:
        return ImageEvidenceDecision(
            source_role=SourceRole.UNKNOWN_IMAGE,
            reason_codes=("ambiguous_caption",),
            caption_source_position=None,
        )
    return ImageEvidenceDecision(
        source_role=SourceRole.UNKNOWN_IMAGE,
        reason_codes=("no_verified_caption",),
        caption_source_position=None,
    )
