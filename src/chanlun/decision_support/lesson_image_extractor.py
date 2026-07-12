from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math

from .lesson_corpus import ImagePlacement, LessonTextBlock, SourceRole
from .lesson_image_classifier import classify_image_evidence
from .lesson_images import ImageOccurrence, PdfImageAsset, PdfImageAssetDescriptor


@dataclass(frozen=True)
class PageImageEvidence:
    assets: tuple[PdfImageAssetDescriptor, ...]
    occurrences: tuple[ImageOccurrence, ...]
    primary_raw_by_sha256: dict[str, bytes]
    smask_raw_by_sha256: dict[str, bytes]

    @property
    def materialized_raw_by_sha256(self) -> dict[str, bytes]:
        """Compatibility alias for callers written before full-asset archival."""
        return self.primary_raw_by_sha256


def _finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _page_box(page: object, name: str) -> tuple[float, float, float, float]:
    value = getattr(page, name, None)
    if value is None or len(value) != 4 or not all(_finite(item) for item in value):
        raise ValueError(f"page {name} must contain four finite coordinates")
    x0, y0, x1, y1 = (float(item) for item in value)
    if not x0 < x1 or not y0 < y1:
        raise ValueError(f"page {name} must be ordered")
    return x0, y0, x1, y1


def _attr(attrs: object, name: str) -> object | None:
    if not isinstance(attrs, dict):
        raise TypeError("PDF image stream attrs must be a dictionary")
    if name in attrs:
        return attrs[name]
    for key, value in attrs.items():
        if getattr(key, "name", None) == name or str(key).lstrip("/") == name:
            return value
    return None


def _pdf_name(value: object, field_name: str) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str):
        return "/" + name.lstrip("/")
    if isinstance(value, str) and value:
        return "/" + value.lstrip("/")
    raise ValueError(f"PDF image {field_name} must be a simple name")


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"PDF image {field_name} must be a positive integer")
    return value


def _stream_bytes(stream: object, field_name: str) -> bytes:
    raw = getattr(stream, "rawdata", None)
    if not isinstance(raw, bytes) or not raw:
        raise ValueError(f"PDF image {field_name} stream must expose non-empty rawdata")
    return raw


def extract_page_image_evidence(
    page: object,
    *,
    page_number: int,
    lesson_number: int | None,
    source_pdf_sha256: str,
    page_text_blocks: tuple[LessonTextBlock, ...] | list[LessonTextBlock],
    classifier_version: str,
) -> PageImageEvidence:
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number <= 0:
        raise ValueError("page_number must be a positive integer")
    width, height = getattr(page, "width", None), getattr(page, "height", None)
    if not _finite(width) or not _finite(height) or float(width) <= 0 or float(height) <= 0:
        raise ValueError("page width and height must be finite positive numbers")
    page_width, page_height = float(width), float(height)
    rotation = getattr(page, "rotation", 0)
    if (
        isinstance(rotation, bool)
        or not isinstance(rotation, int)
        or rotation not in {0, 90, 180, 270}
    ):
        raise ValueError("page rotation must be 0, 90, 180, or 270")
    cropbox_pdf = _page_box(page, "cropbox")
    mediabox_pdf = _page_box(page, "mediabox")
    images = getattr(page, "images", None)
    if not isinstance(images, list):
        raise TypeError("page images must be a list")
    blocks = tuple(page_text_blocks)
    if any(block.page_number != page_number for block in blocks):
        raise ValueError("page_text_blocks must belong to page_number")

    assets_by_key: dict[tuple[int, str], PdfImageAssetDescriptor] = {}
    occurrences: list[ImageOccurrence] = []
    primary_raw: dict[str, bytes] = {}
    smask_raw_by_sha256: dict[str, bytes] = {}
    for draw_index, image in enumerate(images):
        if not isinstance(image, dict):
            raise TypeError("page image entries must be dictionaries")
        stream = image.get("stream")
        attrs = getattr(stream, "attrs", None)
        xref = _positive_int(getattr(stream, "objid", None), "xref")
        raw_bytes = _stream_bytes(stream, "primary")
        width_px = _positive_int(_attr(attrs, "Width"), "width")
        height_px = _positive_int(_attr(attrs, "Height"), "height")
        smask_ref = _attr(attrs, "SMask")
        smask_xref = None
        smask_sha256 = None
        smask_size_bytes = None
        if smask_ref is not None:
            smask_xref = _positive_int(getattr(smask_ref, "objid", None), "SMask xref")
            resolver = getattr(smask_ref, "resolve", None)
            if not callable(resolver):
                raise ValueError("PDF image SMask reference must be resolvable")
            smask_raw = _stream_bytes(resolver(), "SMask")
            smask_sha256 = hashlib.sha256(smask_raw).hexdigest()
            smask_size_bytes = len(smask_raw)
            smask_raw_by_sha256[smask_sha256] = smask_raw
        asset = PdfImageAsset.from_raw(
            source_pdf_sha256=source_pdf_sha256,
            xref=xref,
            raw_bytes=raw_bytes,
            pixel_size=(width_px, height_px),
            filter_name=_pdf_name(_attr(attrs, "Filter"), "filter"),
            color_space=_pdf_name(_attr(attrs, "ColorSpace"), "color space"),
            bits_per_component=_positive_int(
                _attr(attrs, "BitsPerComponent"), "bits per component"
            ),
            smask_xref=smask_xref,
            smask_sha256=smask_sha256,
            smask_size_bytes=smask_size_bytes,
        )
        descriptor = asset.descriptor()
        assets_by_key[(descriptor.xref, descriptor.raw_sha256)] = descriptor
        primary_raw[descriptor.raw_sha256] = raw_bytes
        draw_bbox = tuple(
            float(image.get(name)) for name in ("x0", "top", "x1", "bottom")
        )
        if not draw_bbox[0] < draw_bbox[2] or not draw_bbox[1] < draw_bbox[3]:
            raise ValueError("PDF image draw bbox must be ordered")
        bbox = (
            max(0.0, draw_bbox[0]),
            max(0.0, draw_bbox[1]),
            min(page_width, draw_bbox[2]),
            min(page_height, draw_bbox[3]),
        )
        if not bbox[0] < bbox[2] or not bbox[1] < bbox[3]:
            raise ValueError("PDF image draw does not intersect the visible page")
        placement = ImagePlacement(
            page_number=page_number,
            bbox=bbox,
            page_size=(page_width, page_height),
            xobject_name=image.get("name"),
            pixel_size=descriptor.pixel_size,
            sha256=descriptor.raw_sha256,
        )
        if lesson_number is None:
            role = SourceRole.UNKNOWN_IMAGE
            reason_codes = ("outside_lesson_coverage",)
            caption_source_position = None
        else:
            decision = classify_image_evidence(placement, blocks)
            role = decision.source_role
            reason_codes = decision.reason_codes
            caption_source_position = decision.caption_source_position
        occurrence = ImageOccurrence.create(
            source_pdf_sha256=source_pdf_sha256,
            asset_sha256=descriptor.raw_sha256,
            lesson_number=lesson_number,
            page_number=page_number,
            draw_index=draw_index,
            xref=xref,
            xobject_name=placement.xobject_name,
            bbox_top_left=placement.bbox,
            page_size=placement.page_size,
            page_rotation=rotation,
            source_role=role,
            reason_codes=reason_codes,
            classifier_version=classifier_version,
            caption_page_number=(caption_source_position[0] if caption_source_position else None),
            caption_source_sequence_index=(
                caption_source_position[1] if caption_source_position else None
            ),
            cropbox_pdf=cropbox_pdf,
            mediabox_pdf=mediabox_pdf,
            draw_bbox_top_left=draw_bbox,
        )
        occurrences.append(occurrence)
    return PageImageEvidence(
        assets=tuple(sorted(assets_by_key.values(), key=lambda asset: (asset.xref, asset.raw_sha256))),
        occurrences=tuple(occurrences),
        primary_raw_by_sha256=primary_raw,
        smask_raw_by_sha256=smask_raw_by_sha256,
    )
