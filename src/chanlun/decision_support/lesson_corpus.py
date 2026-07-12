from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
from typing import Callable
import unicodedata


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_RECORD_ID_RE = re.compile(r"^source:[0-9a-f]{64}$")
_OCCURRENCE_ID_RE = re.compile(r"^occurrence:[0-9a-f]{64}$")
_RUNNING_FOOTER_RE = re.compile(r"^第\s*\d+\s*页\s*共\s*\d+\s*页$")
_WINDOWS_RESERVED_STEMS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)


def _is_safe_relative_output_path(value: str) -> bool:
    if not value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        return False
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        return False
    for part in path.parts:
        if (
            not part
            or part.rstrip(" .") != part
            or any(ord(character) < 32 or character in '<>:"|?*' for character in part)
            or part.split(".", 1)[0].upper() in _WINDOWS_RESERVED_STEMS
        ):
            return False
    return True


class SourceRole(str, Enum):
    LESSON_BODY = "lesson_body"
    CHAN_REPLY = "chan_reply"
    CHAN_EXCERPT = "chan_excerpt"
    LESSON_CHART = "lesson_chart"
    EDITOR_NOTE = "editor_note"
    UNKNOWN_TEXT = "unknown_text"
    READER_COMMENT = "reader_comment"
    EDITOR_IMAGE = "editor_image"
    UNKNOWN_IMAGE = "unknown_image"


@dataclass(frozen=True)
class TextRoleContext:
    in_reply_section: bool = False
    reply_author: str | None = None
    verified_lesson_body: bool = False
    verified_chan_excerpt: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.in_reply_section, bool):
            raise TypeError("in_reply_section must be bool")
        if not isinstance(self.verified_chan_excerpt, bool):
            raise TypeError("verified_chan_excerpt must be bool")
        if not isinstance(self.verified_lesson_body, bool):
            raise TypeError("verified_lesson_body must be bool")
        author = None
        if self.reply_author is not None:
            if not isinstance(self.reply_author, str):
                raise TypeError("reply_author must be a string or None")
            author = unicodedata.normalize("NFKC", self.reply_author).strip()
            if not author:
                author = None
        if author is not None and not self.in_reply_section:
            raise ValueError("reply_author requires reply context")
        object.__setattr__(self, "reply_author", author)


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _optional_pdf_box(
    value: tuple[float, float, float, float] | None,
    field_name: str,
) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if len(value) != 4 or not all(_finite_number(item) for item in value):
        raise ValueError(f"{field_name} must contain four finite coordinates")
    x0, y0, x1, y1 = (float(item) for item in value)
    if not x0 < x1 or not y0 < y1:
        raise ValueError(f"{field_name} must be ordered")
    return x0, y0, x1, y1


def normalize_pdf_color(
    color: float | tuple[float, ...] | list[float] | None,
) -> tuple[int, int, int] | None:
    if color is None:
        return None
    values = (color,) if _finite_number(color) else tuple(color) if isinstance(color, (tuple, list)) else ()
    if len(values) not in (1, 3, 4) or not all(_finite_number(value) for value in values):
        raise ValueError("PDF color must be gray, RGB, or CMYK")
    channels = tuple(float(value) for value in values)
    if any(value < 0.0 or value > 1.0 for value in channels):
        raise ValueError("PDF color channels must be between zero and one")

    if len(channels) == 1:
        rgb = (channels[0],) * 3
    elif len(channels) == 3:
        rgb = channels
    else:
        cyan, magenta, yellow, black = channels
        rgb = (
            1.0 - min(1.0, cyan + black),
            1.0 - min(1.0, magenta + black),
            1.0 - min(1.0, yellow + black),
        )
    return tuple(int(round(value * 255.0)) for value in rgb)


@dataclass(frozen=True)
class PageSpan:
    page_number: int
    bbox: tuple[float, float, float, float]
    page_size: tuple[float, float]
    text: str
    color_rgb: tuple[int, int, int] | None
    page_rotation: int = 0
    source_sequence_index: int = 0
    cropbox_pdf: tuple[float, float, float, float] | None = None
    mediabox_pdf: tuple[float, float, float, float] | None = None
    raw_text: str = field(init=False)
    normalized_text: str = field(init=False)

    def __post_init__(self) -> None:
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int) or self.page_number <= 0:
            raise ValueError("page_number must be a positive integer")
        if len(self.page_size) != 2 or not all(_finite_number(value) for value in self.page_size):
            raise ValueError("page_size must contain finite width and height")
        page_width, page_height = (float(value) for value in self.page_size)
        if page_width <= 0 or page_height <= 0:
            raise ValueError("page_size must be positive")
        if len(self.bbox) != 4 or not all(_finite_number(value) for value in self.bbox):
            raise ValueError("bbox must contain four finite coordinates")
        x0, top, x1, bottom = (float(value) for value in self.bbox)
        if not (0 <= x0 < x1 <= page_width and 0 <= top < bottom <= page_height):
            raise ValueError("bbox must be inside the page")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        raw_text = self.text
        normalized_text = unicodedata.normalize("NFKC", raw_text).strip()
        if not normalized_text:
            raise ValueError("text must not be empty")
        if (
            isinstance(self.page_rotation, bool)
            or not isinstance(self.page_rotation, int)
            or self.page_rotation not in {0, 90, 180, 270}
        ):
            raise ValueError("page_rotation must be 0, 90, 180, or 270")
        if (
            isinstance(self.source_sequence_index, bool)
            or not isinstance(self.source_sequence_index, int)
            or self.source_sequence_index < 0
        ):
            raise ValueError("source_sequence_index must be a non-negative integer")
        cropbox_pdf = _optional_pdf_box(self.cropbox_pdf, "cropbox_pdf")
        mediabox_pdf = _optional_pdf_box(self.mediabox_pdf, "mediabox_pdf")
        if self.color_rgb is not None:
            if len(self.color_rgb) != 3 or any(
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
                for value in self.color_rgb
            ):
                raise ValueError("color_rgb must contain three byte values")
            object.__setattr__(self, "color_rgb", tuple(self.color_rgb))
        object.__setattr__(self, "bbox", (x0, top, x1, bottom))
        object.__setattr__(self, "page_size", (page_width, page_height))
        object.__setattr__(self, "text", normalized_text)
        object.__setattr__(self, "raw_text", raw_text)
        object.__setattr__(self, "normalized_text", normalized_text)
        object.__setattr__(self, "cropbox_pdf", cropbox_pdf)
        object.__setattr__(self, "mediabox_pdf", mediabox_pdf)

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()

    @property
    def normalized_sha256(self) -> str:
        return hashlib.sha256(self.normalized_text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ImagePlacement:
    page_number: int
    bbox: tuple[float, float, float, float]
    page_size: tuple[float, float]
    xobject_name: str
    pixel_size: tuple[int, int]
    sha256: str

    def __post_init__(self) -> None:
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int) or self.page_number <= 0:
            raise ValueError("page_number must be a positive integer")
        if len(self.page_size) != 2 or not all(_finite_number(value) for value in self.page_size):
            raise ValueError("page_size must contain finite width and height")
        page_width, page_height = (float(value) for value in self.page_size)
        if page_width <= 0 or page_height <= 0:
            raise ValueError("page_size must be positive")
        if len(self.bbox) != 4 or not all(_finite_number(value) for value in self.bbox):
            raise ValueError("bbox must contain four finite coordinates")
        x0, top, x1, bottom = (float(value) for value in self.bbox)
        if not (0 <= x0 < x1 <= page_width and 0 <= top < bottom <= page_height):
            raise ValueError("bbox must be inside the page")
        if not isinstance(self.xobject_name, str):
            raise TypeError("xobject_name must be a string")
        name = self.xobject_name.strip().lstrip("/")
        if not name or any(separator in name for separator in ("/", "\\")):
            raise ValueError("xobject_name must be a simple name")
        if len(self.pixel_size) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.pixel_size
        ):
            raise ValueError("pixel_size must contain positive integers")
        sha256 = str(self.sha256).strip().lower()
        if _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "bbox", (x0, top, x1, bottom))
        object.__setattr__(self, "page_size", (page_width, page_height))
        object.__setattr__(self, "xobject_name", name)
        object.__setattr__(self, "pixel_size", tuple(self.pixel_size))
        object.__setattr__(self, "sha256", sha256)


@dataclass(frozen=True)
class ImageRoleContext:
    lesson_number: int
    adjacent_text_roles: tuple[SourceRole, ...] = ()
    caption_record_id: str | None = None
    position_verified: bool = False
    verification_reason: str = ""
    editor_flowchart_hint: bool = False

    def __post_init__(self) -> None:
        if (
            isinstance(self.lesson_number, bool)
            or not isinstance(self.lesson_number, int)
            or not 0 <= self.lesson_number <= 108
        ):
            raise ValueError("lesson_number must be between 0 and 108")
        if not isinstance(self.editor_flowchart_hint, bool):
            raise TypeError("editor_flowchart_hint must be bool")
        if not isinstance(self.position_verified, bool):
            raise TypeError("position_verified must be bool")
        roles = tuple(SourceRole(role) for role in self.adjacent_text_roles)
        caption_record_id = self.caption_record_id
        if caption_record_id is not None:
            if not isinstance(caption_record_id, str):
                raise TypeError("caption_record_id must be a string or None")
            caption_record_id = caption_record_id.strip().lower()
            if _SOURCE_RECORD_ID_RE.fullmatch(caption_record_id) is None:
                raise ValueError("caption_record_id must be a stable source record id")
        if not isinstance(self.verification_reason, str):
            raise TypeError("verification_reason must be a string")
        reason = unicodedata.normalize("NFKC", self.verification_reason).strip()
        object.__setattr__(self, "adjacent_text_roles", roles)
        object.__setattr__(self, "caption_record_id", caption_record_id)
        object.__setattr__(self, "verification_reason", reason)


@dataclass(frozen=True)
class LessonBoundary:
    lesson_number: int
    title: str
    page_start: int
    page_end: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.lesson_number, bool)
            or not isinstance(self.lesson_number, int)
            or not 0 <= self.lesson_number <= 108
        ):
            raise ValueError("lesson_number must be between 0 and 108")
        if not isinstance(self.title, str):
            raise TypeError("title must be a string")
        title = re.sub(
            r"\s+",
            " ",
            unicodedata.normalize("NFKC", self.title).strip(),
        )
        if not title:
            raise ValueError("title must not be empty")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.page_start, self.page_end)
        ):
            raise ValueError("lesson pages must be positive integers")
        if self.page_start > self.page_end:
            raise ValueError("page_start must not exceed page_end")
        object.__setattr__(self, "title", title)


def validate_lesson_boundaries(
    boundaries: tuple[LessonBoundary, ...] | list[LessonBoundary],
    *,
    expected_first_page: int = 7,
    expected_last_page: int = 2533,
) -> tuple[LessonBoundary, ...]:
    values = tuple(boundaries)
    if any(not isinstance(boundary, LessonBoundary) for boundary in values):
        raise TypeError("boundaries must contain LessonBoundary values")
    if tuple(boundary.lesson_number for boundary in values) != tuple(range(109)):
        raise ValueError("lesson numbers must be exactly 0 through 108")
    if values[0].page_start != expected_first_page or values[-1].page_end != expected_last_page:
        raise ValueError("lesson pages must match the expected PDF coverage")
    if any(
        previous.page_end + 1 != following.page_start
        for previous, following in zip(values, values[1:])
    ):
        raise ValueError("lesson pages must be continuous without gaps or overlaps")
    return values


@dataclass(frozen=True)
class SourceRecord:
    record_type: str
    lesson_number: int
    page_number: int
    bbox: tuple[float, float, float, float]
    page_size: tuple[float, float]
    coordinate_system: str
    page_rotation: int
    color_rgb: tuple[int, int, int] | None
    source_role: SourceRole
    content_sha256: str
    source_pdf_sha256: str
    output_path: str
    source_sequence_index: int
    block_index: int
    extractor_version: str
    normalized_text_sha256: str | None = None
    cropbox_pdf: tuple[float, float, float, float] | None = None
    mediabox_pdf: tuple[float, float, float, float] | None = None
    caption_record_id: str | None = None
    source_object_id: str | None = None
    record_id: str = field(init=False)

    @classmethod
    def create(cls, **values: object) -> SourceRecord:
        return cls(**values)

    def __post_init__(self) -> None:
        record_type = str(self.record_type).strip().lower()
        if record_type not in {"text", "image"}:
            raise ValueError("record_type must be text or image")
        if (
            isinstance(self.lesson_number, bool)
            or not isinstance(self.lesson_number, int)
            or not 0 <= self.lesson_number <= 108
        ):
            raise ValueError("lesson_number must be between 0 and 108")
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int) or self.page_number <= 0:
            raise ValueError("page_number must be a positive integer")
        if len(self.bbox) != 4 or not all(_finite_number(value) for value in self.bbox):
            raise ValueError("bbox must contain four finite coordinates")
        if len(self.page_size) != 2 or not all(_finite_number(value) for value in self.page_size):
            raise ValueError("page_size must contain finite width and height")
        page_width, page_height = (float(value) for value in self.page_size)
        if page_width <= 0 or page_height <= 0:
            raise ValueError("page_size must be positive")
        x0, top, x1, bottom = (float(value) for value in self.bbox)
        if not (0 <= x0 < x1 <= page_width and 0 <= top < bottom <= page_height):
            raise ValueError("bbox must be inside page_size")
        if self.coordinate_system != "pdf_top_left_pt":
            raise ValueError("coordinate_system must be pdf_top_left_pt")
        if (
            isinstance(self.page_rotation, bool)
            or not isinstance(self.page_rotation, int)
            or self.page_rotation not in {0, 90, 180, 270}
        ):
            raise ValueError("page_rotation must be 0, 90, 180, or 270")
        color = self.color_rgb
        if color is not None and (
            len(color) != 3
            or any(
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
                for value in color
            )
        ):
            raise ValueError("color_rgb must contain three byte values")
        role = SourceRole(self.source_role)
        text_roles = {
            SourceRole.LESSON_BODY,
            SourceRole.CHAN_REPLY,
            SourceRole.CHAN_EXCERPT,
            SourceRole.EDITOR_NOTE,
            SourceRole.UNKNOWN_TEXT,
            SourceRole.READER_COMMENT,
        }
        image_roles = {
            SourceRole.LESSON_CHART,
            SourceRole.EDITOR_IMAGE,
            SourceRole.UNKNOWN_IMAGE,
        }
        if (record_type == "text" and role not in text_roles) or (
            record_type == "image" and role not in image_roles
        ):
            raise ValueError("source_role is incompatible with record_type")
        content_sha256 = str(self.content_sha256).strip().lower()
        source_pdf_sha256 = str(self.source_pdf_sha256).strip().lower()
        if _SHA256_RE.fullmatch(content_sha256) is None:
            raise ValueError("content_sha256 must contain 64 hexadecimal characters")
        normalized_text_sha256 = self.normalized_text_sha256
        if normalized_text_sha256 is not None:
            if not isinstance(normalized_text_sha256, str):
                raise TypeError("normalized_text_sha256 must be a string or None")
            normalized_text_sha256 = normalized_text_sha256.strip().lower()
        if record_type == "text" and _SHA256_RE.fullmatch(normalized_text_sha256 or "") is None:
            raise ValueError("text records require normalized_text_sha256")
        if record_type == "image" and normalized_text_sha256 is not None:
            raise ValueError("image records cannot have normalized_text_sha256")
        caption_record_id = self.caption_record_id
        source_object_id = self.source_object_id
        if caption_record_id is not None:
            if not isinstance(caption_record_id, str):
                raise TypeError("caption_record_id must be a string or None")
            caption_record_id = caption_record_id.strip().lower()
            if _SOURCE_RECORD_ID_RE.fullmatch(caption_record_id) is None:
                raise ValueError("caption_record_id must be a stable source record id")
        if source_object_id is not None:
            if not isinstance(source_object_id, str):
                raise TypeError("source_object_id must be a string or None")
            source_object_id = source_object_id.strip().lower()
            if _OCCURRENCE_ID_RE.fullmatch(source_object_id) is None:
                raise ValueError("source_object_id must be a stable occurrence id")
        if record_type == "text" and (
            caption_record_id is not None or source_object_id is not None
        ):
            raise ValueError("text records cannot link image provenance")
        if record_type == "image" and source_object_id is None:
            raise ValueError("image records require source_object_id")
        if role is SourceRole.LESSON_CHART and caption_record_id is None:
            raise ValueError("lesson chart records require caption_record_id")
        cropbox_pdf = _optional_pdf_box(self.cropbox_pdf, "cropbox_pdf")
        mediabox_pdf = _optional_pdf_box(self.mediabox_pdf, "mediabox_pdf")
        if _SHA256_RE.fullmatch(source_pdf_sha256) is None:
            raise ValueError("source_pdf_sha256 must contain 64 hexadecimal characters")
        if not isinstance(self.output_path, str):
            raise TypeError("output_path must be a string")
        output_path = self.output_path.strip()
        if not _is_safe_relative_output_path(output_path):
            raise ValueError("output_path must be a safe relative POSIX path")
        if (
            isinstance(self.source_sequence_index, bool)
            or not isinstance(self.source_sequence_index, int)
            or self.source_sequence_index < 0
        ):
            raise ValueError("source_sequence_index must be a non-negative integer")
        if isinstance(self.block_index, bool) or not isinstance(self.block_index, int) or self.block_index < 0:
            raise ValueError("block_index must be a non-negative integer")
        if not isinstance(self.extractor_version, str):
            raise TypeError("extractor_version must be a string")
        extractor_version = self.extractor_version.strip()
        if not extractor_version or len(extractor_version) > 128:
            raise ValueError("extractor_version must be present and bounded")

        bbox = (x0, top, x1, bottom)
        normalized_color = tuple(color) if color is not None else None
        payload = {
            "bbox": list(bbox),
            "block_index": self.block_index,
            "caption_record_id": caption_record_id,
            "color_rgb": list(normalized_color) if normalized_color is not None else None,
            "content_sha256": content_sha256,
            "coordinate_system": self.coordinate_system,
            "cropbox_pdf": list(cropbox_pdf) if cropbox_pdf is not None else None,
            "extractor_version": extractor_version,
            "lesson_number": self.lesson_number,
            "mediabox_pdf": list(mediabox_pdf) if mediabox_pdf is not None else None,
            "normalized_text_sha256": normalized_text_sha256,
            "output_path": output_path,
            "page_number": self.page_number,
            "page_rotation": self.page_rotation,
            "page_size": list((page_width, page_height)),
            "record_type": record_type,
            "source_pdf_sha256": source_pdf_sha256,
            "source_object_id": source_object_id,
            "source_role": role.value,
            "source_sequence_index": self.source_sequence_index,
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "record_type", record_type)
        object.__setattr__(self, "bbox", bbox)
        object.__setattr__(self, "page_size", (page_width, page_height))
        object.__setattr__(self, "color_rgb", normalized_color)
        object.__setattr__(self, "source_role", role)
        object.__setattr__(self, "content_sha256", content_sha256)
        object.__setattr__(self, "normalized_text_sha256", normalized_text_sha256)
        object.__setattr__(self, "cropbox_pdf", cropbox_pdf)
        object.__setattr__(self, "mediabox_pdf", mediabox_pdf)
        object.__setattr__(self, "caption_record_id", caption_record_id)
        object.__setattr__(self, "source_object_id", source_object_id)
        object.__setattr__(self, "source_pdf_sha256", source_pdf_sha256)
        object.__setattr__(self, "output_path", output_path)
        object.__setattr__(self, "extractor_version", extractor_version)
        object.__setattr__(self, "record_id", "source:" + hashlib.sha256(serialized).hexdigest())


def is_running_header_or_footer(span: PageSpan) -> bool:
    if not isinstance(span, PageSpan):
        raise TypeError("span must be PageSpan")
    _, top, _, bottom = span.bbox
    _, page_height = span.page_size
    compact = re.sub(r"\s+", "", span.text)
    header_text = (
        "天使版---教你炒股票108课" in compact
        or compact.startswith("公众号:天使")
        or compact.startswith("公众号：天使")
    )
    footer_text = _RUNNING_FOOTER_RE.fullmatch(span.text) is not None
    return (top <= min(76.0, page_height * 0.10) and header_text) or (
        bottom >= page_height - 55.0 and footer_text
    )


def classify_text_role(span: PageSpan, context: TextRoleContext) -> SourceRole:
    if not isinstance(span, PageSpan):
        raise TypeError("span must be PageSpan")
    if not isinstance(context, TextRoleContext):
        raise TypeError("context must be TextRoleContext")
    color = span.color_rgb
    if color is None:
        return SourceRole.UNKNOWN_TEXT
    red, green, blue = color
    is_black = max(color) <= 48 and max(color) - min(color) <= 12
    is_red = red >= 180 and red >= green + 80 and red >= blue + 60
    is_green = green >= 120 and green >= red + 70 and green >= blue + 70

    if is_red:
        return SourceRole.EDITOR_NOTE
    if is_green:
        return (
            SourceRole.CHAN_EXCERPT
            if context.verified_chan_excerpt
            else SourceRole.EDITOR_NOTE
        )
    if not is_black:
        return SourceRole.UNKNOWN_TEXT
    if not context.in_reply_section:
        return (
            SourceRole.LESSON_BODY
            if context.verified_lesson_body
            else SourceRole.UNKNOWN_TEXT
        )
    if context.reply_author == "缠中说禅":
        return SourceRole.CHAN_REPLY
    return (
        SourceRole.READER_COMMENT
        if context.reply_author is not None
        else SourceRole.UNKNOWN_TEXT
    )


def classify_image_role(
    placement: ImagePlacement,
    context: ImageRoleContext,
) -> SourceRole:
    if not isinstance(placement, ImagePlacement):
        raise TypeError("placement must be ImagePlacement")
    if not isinstance(context, ImageRoleContext):
        raise TypeError("context must be ImageRoleContext")
    authoritative_context = any(
        role in {
            SourceRole.LESSON_BODY,
            SourceRole.CHAN_REPLY,
            SourceRole.CHAN_EXCERPT,
        }
        for role in context.adjacent_text_roles
    )
    if (
        context.caption_record_id is not None
        and context.position_verified
        and authoritative_context
        and not context.editor_flowchart_hint
    ):
        return SourceRole.LESSON_CHART
    if context.editor_flowchart_hint:
        return SourceRole.EDITOR_IMAGE
    return SourceRole.UNKNOWN_IMAGE


@dataclass(frozen=True)
class PdfIdentity:
    filename: str
    size_bytes: int
    page_count: int
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str):
            raise TypeError("filename must be a string")
        filename = self.filename.strip()
        sha256 = str(self.sha256).strip().lower()
        if not filename or Path(filename).name != filename:
            raise ValueError("filename must be a basename")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes <= 0:
            raise ValueError("size_bytes must be a positive integer")
        if isinstance(self.page_count, bool) or not isinstance(self.page_count, int) or self.page_count <= 0:
            raise ValueError("page_count must be a positive integer")
        if _SHA256_RE.fullmatch(sha256) is None:
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "sha256", sha256)


@dataclass(frozen=True)
class PdfIdentityObservation:
    filename: str
    size_bytes: int
    page_count: int | None = None
    sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.filename, str):
            raise TypeError("filename must be a string")
        filename = self.filename.strip()
        if not filename or Path(filename).name != filename:
            raise ValueError("filename must be a basename")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        if self.page_count is not None and (
            isinstance(self.page_count, bool)
            or not isinstance(self.page_count, int)
            or self.page_count <= 0
        ):
            raise ValueError("page_count must be a positive integer or None")
        sha256 = self.sha256
        if sha256 is not None:
            if not isinstance(sha256, str):
                raise TypeError("sha256 must be a string or None")
            sha256 = sha256.strip().lower()
            if _SHA256_RE.fullmatch(sha256) is None:
                raise ValueError("sha256 must contain 64 hexadecimal characters")
        object.__setattr__(self, "filename", filename)
        object.__setattr__(self, "sha256", sha256)


class PdfIdentityMismatch(ValueError):
    def __init__(
        self,
        expected: PdfIdentity,
        actual: PdfIdentity | PdfIdentityObservation,
    ):
        self.expected = expected
        self.actual = actual
        differing = tuple(
            field
            for field in ("filename", "size_bytes", "page_count", "sha256")
            if getattr(actual, field) is not None
            and getattr(expected, field) != getattr(actual, field)
        )
        self.differing_fields = differing
        super().__init__("PDF identity mismatch: " + ", ".join(differing))


def _page_count(path: Path) -> int:
    from pypdf import PdfReader

    return len(PdfReader(str(path)).pages)


def _sha256_file(path: Path, chunk_size: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def verify_pdf_identity(
    path: Path,
    expected: PdfIdentity,
    *,
    page_counter: Callable[[Path], int] = _page_count,
    chunk_size: int = 8 * 1024 * 1024,
) -> PdfIdentity:
    if not isinstance(expected, PdfIdentity):
        raise TypeError("expected must be PdfIdentity")
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")

    source = Path(path).resolve()
    before = source.stat()
    if source.name != expected.filename or before.st_size != expected.size_bytes:
        preliminary = PdfIdentityObservation(
            filename=source.name,
            size_bytes=before.st_size,
        )
        raise PdfIdentityMismatch(expected, preliminary)
    sha256 = _sha256_file(source, chunk_size)
    after_hash = source.stat()
    if before.st_size != after_hash.st_size or before.st_mtime_ns != after_hash.st_mtime_ns:
        raise RuntimeError("PDF changed while identity was being verified")
    if sha256 != expected.sha256:
        preliminary = PdfIdentity(
            filename=source.name,
            size_bytes=after_hash.st_size,
            page_count=expected.page_count,
            sha256=sha256,
        )
        raise PdfIdentityMismatch(expected, preliminary)
    page_count = page_counter(source)
    after = source.stat()
    if after_hash.st_size != after.st_size or after_hash.st_mtime_ns != after.st_mtime_ns:
        raise RuntimeError("PDF changed while identity was being verified")

    actual = PdfIdentity(
        filename=source.name,
        size_bytes=after.st_size,
        page_count=page_count,
        sha256=sha256,
    )
    if actual != expected:
        raise PdfIdentityMismatch(expected, actual)
    return actual


@dataclass(frozen=True)
class LessonTextBlock:
    lesson_number: int
    page_number: int
    bbox: tuple[float, float, float, float]
    page_size: tuple[float, float]
    page_rotation: int
    source_sequence_index: int
    color_rgb: tuple[int, int, int] | None
    source_role: SourceRole
    text: str
    cropbox_pdf: tuple[float, float, float, float] | None = None
    mediabox_pdf: tuple[float, float, float, float] | None = None
    raw_text: str = field(init=False)
    normalized_text: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            isinstance(self.lesson_number, bool)
            or not isinstance(self.lesson_number, int)
            or not 0 <= self.lesson_number <= 108
        ):
            raise ValueError("lesson_number must be between 0 and 108")
        if isinstance(self.page_number, bool) or not isinstance(self.page_number, int) or self.page_number <= 0:
            raise ValueError("page_number must be a positive integer")
        if len(self.bbox) != 4 or not all(_finite_number(value) for value in self.bbox):
            raise ValueError("bbox must contain four finite coordinates")
        if len(self.page_size) != 2 or not all(_finite_number(value) for value in self.page_size):
            raise ValueError("page_size must contain finite width and height")
        page_width, page_height = (float(value) for value in self.page_size)
        if page_width <= 0 or page_height <= 0:
            raise ValueError("page_size must be positive")
        x0, top, x1, bottom = (float(value) for value in self.bbox)
        if not (0 <= x0 < x1 <= page_width and 0 <= top < bottom <= page_height):
            raise ValueError("bbox must be inside page_size")
        if (
            isinstance(self.page_rotation, bool)
            or not isinstance(self.page_rotation, int)
            or self.page_rotation not in {0, 90, 180, 270}
        ):
            raise ValueError("page_rotation must be 0, 90, 180, or 270")
        if (
            isinstance(self.source_sequence_index, bool)
            or not isinstance(self.source_sequence_index, int)
            or self.source_sequence_index < 0
        ):
            raise ValueError("source_sequence_index must be a non-negative integer")
        color = self.color_rgb
        if color is not None and (
            len(color) != 3
            or any(
                isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255
                for value in color
            )
        ):
            raise ValueError("color_rgb must contain three byte values")
        role = SourceRole(self.source_role)
        if role in {
            SourceRole.LESSON_CHART,
            SourceRole.EDITOR_IMAGE,
            SourceRole.UNKNOWN_IMAGE,
        }:
            raise ValueError("text block cannot use an image source role")
        if not isinstance(self.text, str):
            raise TypeError("text must be a string")
        raw_text = self.text
        normalized_text = unicodedata.normalize("NFKC", raw_text).replace("\r\n", "\n").replace("\r", "\n").strip()
        if not normalized_text:
            raise ValueError("text must not be empty")
        cropbox_pdf = _optional_pdf_box(self.cropbox_pdf, "cropbox_pdf")
        mediabox_pdf = _optional_pdf_box(self.mediabox_pdf, "mediabox_pdf")
        object.__setattr__(self, "bbox", (x0, top, x1, bottom))
        object.__setattr__(self, "page_size", (page_width, page_height))
        object.__setattr__(self, "color_rgb", tuple(color) if color is not None else None)
        object.__setattr__(self, "source_role", role)
        object.__setattr__(self, "text", normalized_text)
        object.__setattr__(self, "raw_text", raw_text)
        object.__setattr__(self, "normalized_text", normalized_text)
        object.__setattr__(self, "cropbox_pdf", cropbox_pdf)
        object.__setattr__(self, "mediabox_pdf", mediabox_pdf)

    @property
    def content_sha256(self) -> str:
        return self.raw_sha256

    @property
    def raw_sha256(self) -> str:
        return hashlib.sha256(self.raw_text.encode("utf-8")).hexdigest()

    @property
    def normalized_sha256(self) -> str:
        return hashlib.sha256(self.normalized_text.encode("utf-8")).hexdigest()


def order_lesson_text_blocks(
    blocks: tuple[LessonTextBlock, ...] | list[LessonTextBlock],
) -> tuple[LessonTextBlock, ...]:
    values = tuple(blocks)
    if any(not isinstance(block, LessonTextBlock) for block in values):
        raise TypeError("blocks must contain LessonTextBlock values")
    identities = tuple(
        (block.lesson_number, block.page_number, block.source_sequence_index)
        for block in values
    )
    if len(set(identities)) != len(identities):
        raise ValueError("source_sequence_index must be unique within each lesson page")
    return tuple(
        sorted(
            values,
            key=lambda block: (
                block.lesson_number,
                block.page_number,
                block.source_sequence_index,
                block.bbox[1],
                block.bbox[0],
                block.content_sha256,
            ),
        )
    )


def _lesson_filename(boundary: LessonBoundary) -> str:
    subject = re.sub(
        rf"^教你炒股票\s*{boundary.lesson_number}\s*[:：._\-—]*\s*",
        "",
        boundary.title,
    ).strip()
    subject = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "", subject)
    subject = re.sub(r"\s+", "_", subject).strip(" ._")
    if not subject:
        subject = "课程原文"
    subject = subject[:80].rstrip(" ._") or "课程原文"
    return f"L{boundary.lesson_number:03d}_{subject}.md"


def _write_utf8(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _file_entry(path: Path, root: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }


def _source_record_dict(record: SourceRecord) -> dict[str, object]:
    return {
        "bbox": list(record.bbox),
        "block_index": record.block_index,
        "caption_record_id": record.caption_record_id,
        "color_rgb": list(record.color_rgb) if record.color_rgb is not None else None,
        "content_sha256": record.content_sha256,
        "raw_content_sha256": record.content_sha256,
        "normalized_text_sha256": record.normalized_text_sha256,
        "coordinate_system": record.coordinate_system,
        "cropbox_pdf": list(record.cropbox_pdf) if record.cropbox_pdf is not None else None,
        "extractor_version": record.extractor_version,
        "lesson_number": record.lesson_number,
        "mediabox_pdf": list(record.mediabox_pdf) if record.mediabox_pdf is not None else None,
        "output_path": record.output_path,
        "page_number": record.page_number,
        "page_rotation": record.page_rotation,
        "page_size": list(record.page_size),
        "record_id": record.record_id,
        "record_type": record.record_type,
        "source_pdf_sha256": record.source_pdf_sha256,
        "source_object_id": record.source_object_id,
        "source_role": record.source_role.value,
        "source_sequence_index": record.source_sequence_index,
    }


def _lesson_markdown(
    boundary: LessonBoundary,
    identity: PdfIdentity,
    extractor_version: str,
    rows: tuple[tuple[LessonTextBlock, SourceRecord], ...],
) -> str:
    parts = [
        f"# {boundary.title}",
        "",
        f"- Lesson: {boundary.lesson_number}",
        f"- Pages: {boundary.page_start}-{boundary.page_end}",
        f"- Source-PDF-SHA256: {identity.sha256}",
        f"- Extractor-Version: {extractor_version}",
        "",
    ]
    for block, record in rows:
        marker = json.dumps(
            {"record_id": record.record_id},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        parts.extend((f"<!-- chanlun-source {marker} -->", block.raw_text, ""))
    return "\n".join(parts).rstrip() + "\n"


def _verify_staged_package(root: Path, expected_records: int) -> None:
    lesson_files = tuple(sorted(root.glob("L*.md")))
    if len(lesson_files) != 109:
        raise RuntimeError("staged package does not contain 109 lesson files")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    source_lines = (root / "source_map.jsonl").read_text(encoding="utf-8").splitlines()
    if len(source_lines) != expected_records:
        raise RuntimeError("staged package source record count mismatch")
    for line in source_lines:
        json.loads(line)
    for item in manifest.get("files", ()):
        path = root / item["path"]
        if not path.is_file():
            raise RuntimeError("staged package file is missing")
        data = path.read_bytes()
        if len(data) != item["size_bytes"] or hashlib.sha256(data).hexdigest() != item["sha256"]:
            raise RuntimeError("staged package file hash mismatch")


def _publish_directory(staging: Path, target: Path) -> None:
    if target.is_symlink():
        raise ValueError("target must not be a symbolic link")
    if target.exists():
        raise FileExistsError(f"target already exists: {target}")
    os.replace(staging, target)


def build_lesson_package(
    target: Path,
    *,
    identity: PdfIdentity,
    boundaries: tuple[LessonBoundary, ...] | list[LessonBoundary],
    text_blocks: tuple[LessonTextBlock, ...] | list[LessonTextBlock],
    extractor_version: str,
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
    if expected_last_page != identity.page_count:
        raise ValueError("lesson coverage must end at the verified PDF page_count")
    if not isinstance(extractor_version, str):
        raise TypeError("extractor_version must be a string")
    version = extractor_version.strip()
    if not version or len(version) > 128:
        raise ValueError("extractor_version must be present and bounded")
    blocks = tuple(text_blocks)
    if any(not isinstance(block, LessonTextBlock) for block in blocks):
        raise TypeError("text_blocks must contain LessonTextBlock values")
    by_lesson_boundary = {
        boundary.lesson_number: boundary for boundary in validated_boundaries
    }
    for block in blocks:
        boundary = by_lesson_boundary[block.lesson_number]
        if not boundary.page_start <= block.page_number <= boundary.page_end:
            raise ValueError("text block page is outside its lesson boundary")
    original_roles = {
        SourceRole.LESSON_BODY,
        SourceRole.CHAN_REPLY,
        SourceRole.CHAN_EXCERPT,
    }
    for boundary in validated_boundaries:
        if not any(
            block.lesson_number == boundary.lesson_number
            and block.source_role in original_roles
            for block in blocks
        ):
            raise ValueError("every lesson requires at least one original text block")

    ordered_blocks = order_lesson_text_blocks(blocks)
    target_path = Path(target).absolute()
    if target_path.is_symlink():
        raise ValueError("target must not be a symbolic link")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{target_path.name}.staging-",
            dir=target_path.parent,
        )
    )
    try:
        records: list[SourceRecord] = []
        lesson_entries: list[dict[str, object]] = []
        lesson_files: list[Path] = []
        for boundary in validated_boundaries:
            filename = _lesson_filename(boundary)
            lesson_blocks = tuple(
                block for block in ordered_blocks if block.lesson_number == boundary.lesson_number
            )
            rows: list[tuple[LessonTextBlock, SourceRecord]] = []
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
                    content_sha256=block.content_sha256,
                    normalized_text_sha256=block.normalized_sha256,
                    cropbox_pdf=block.cropbox_pdf,
                    mediabox_pdf=block.mediabox_pdf,
                    source_pdf_sha256=identity.sha256,
                    output_path=filename,
                    source_sequence_index=block.source_sequence_index,
                    block_index=block_index,
                    extractor_version=version,
                )
                records.append(record)
                rows.append((block, record))
            lesson_path = staging / filename
            _write_utf8(
                lesson_path,
                _lesson_markdown(boundary, identity, version, tuple(rows)),
            )
            lesson_files.append(lesson_path)
            lesson_entry = {
                "filename": filename,
                "lesson_number": boundary.lesson_number,
                "page_end": boundary.page_end,
                "page_start": boundary.page_start,
                "record_count": len(rows),
                "sha256": hashlib.sha256(lesson_path.read_bytes()).hexdigest(),
                "title": boundary.title,
            }
            lesson_entries.append(lesson_entry)

        index_lines = [
            "# 缠论第 0-108 课规范化索引",
            "",
            "| 课号 | 页码 | 标题 | 文件 | SHA-256 |",
            "|---:|---:|---|---|---|",
        ]
        for item in lesson_entries:
            safe_title = str(item["title"]).replace("|", "\\|")
            index_lines.append(
                f"| {item['lesson_number']} | {item['page_start']}-{item['page_end']} | "
                f"{safe_title} | {item['filename']} | {item['sha256']} |"
            )
        _write_utf8(staging / "_index.md", "\n".join(index_lines) + "\n")

        source_rows = tuple(_source_record_dict(record) for record in records)
        source_map_text = "".join(
            json.dumps(
                row,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for row in source_rows
        )
        _write_utf8(staging / "source_map.jsonl", source_map_text)

        package_files = tuple(
            sorted(
                (*lesson_files, staging / "_index.md", staging / "source_map.jsonl"),
                key=lambda path: path.relative_to(staging).as_posix(),
            )
        )
        role_counts = dict(
            sorted(Counter(record.source_role.value for record in records).items())
        )
        manifest: dict[str, object] = {
            "coverage": {
                "first_page": expected_first_page,
                "last_page": expected_last_page,
                "lesson_count": len(validated_boundaries),
            },
            "extractor_version": version,
            "files": [_file_entry(path, staging) for path in package_files],
            "lessons": lesson_entries,
            "package_kind": "chanlun_lesson_corpus",
            "role_counts": role_counts,
            "schema_version": 1,
            "source_pdf": {
                "filename": identity.filename,
                "page_count": identity.page_count,
                "sha256": identity.sha256,
                "size_bytes": identity.size_bytes,
            },
            "status": {
                "blockers": [
                    "image_inventory_unverified",
                    "source_identity_unattested",
                    "determinism_uncertified",
                ],
                "integrity": "pending",
                "original_evidence": "unavailable",
            },
        }
        fingerprint_payload = json.dumps(
            manifest,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        manifest["build_fingerprint"] = hashlib.sha256(fingerprint_payload).hexdigest()
        _write_utf8(
            staging / "manifest.json",
            json.dumps(
                manifest,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                separators=(",", ": "),
            )
            + "\n",
        )
        _verify_staged_package(staging, len(records))
        _publish_directory(staging, target_path)
        staging = Path()
        return target_path
    finally:
        if staging != Path() and staging.exists():
            shutil.rmtree(staging)
