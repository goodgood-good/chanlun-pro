from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from io import BytesIO
import json
import math
import re

from .lesson_corpus import SourceRole


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _optional_box(
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


@dataclass(frozen=True)
class PdfImageAssetDescriptor:
    source_pdf_sha256: str
    xref: int
    pixel_size: tuple[int, int]
    filter_name: str
    color_space: str
    bits_per_component: int
    raw_sha256: str
    raw_size_bytes: int
    smask_xref: int | None
    smask_sha256: str | None
    smask_size_bytes: int | None
    asset_id: str

    def __post_init__(self) -> None:
        source_pdf_sha256 = str(self.source_pdf_sha256).strip().lower()
        raw_sha256 = str(self.raw_sha256).strip().lower()
        if _SHA256_RE.fullmatch(source_pdf_sha256) is None:
            raise ValueError("source_pdf_sha256 must contain 64 hexadecimal characters")
        if _SHA256_RE.fullmatch(raw_sha256) is None:
            raise ValueError("raw_sha256 must contain 64 hexadecimal characters")
        if isinstance(self.xref, bool) or not isinstance(self.xref, int) or self.xref <= 0:
            raise ValueError("xref must be a positive integer")
        if len(self.pixel_size) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.pixel_size
        ):
            raise ValueError("pixel_size must contain positive integers")
        if self.filter_name != "/DCTDecode" or self.color_space != "/DeviceRGB":
            raise ValueError("descriptor must identify an RGB JPEG stream")
        if self.bits_per_component != 8:
            raise ValueError("bits_per_component must be 8")
        if (
            isinstance(self.raw_size_bytes, bool)
            or not isinstance(self.raw_size_bytes, int)
            or self.raw_size_bytes <= 0
        ):
            raise ValueError("raw_size_bytes must be a positive integer")
        if len(
            {
                self.smask_xref is None,
                self.smask_sha256 is None,
                self.smask_size_bytes is None,
            }
        ) != 1:
            raise ValueError("smask xref, hash, and size must be provided together")
        smask_sha256 = self.smask_sha256
        if self.smask_xref is not None:
            if isinstance(self.smask_xref, bool) or not isinstance(self.smask_xref, int) or self.smask_xref <= 0:
                raise ValueError("smask_xref must be a positive integer")
            smask_sha256 = str(smask_sha256).strip().lower()
            if _SHA256_RE.fullmatch(smask_sha256) is None:
                raise ValueError("smask_sha256 must contain 64 hexadecimal characters")
            if (
                isinstance(self.smask_size_bytes, bool)
                or not isinstance(self.smask_size_bytes, int)
                or self.smask_size_bytes <= 0
            ):
                raise ValueError("smask_size_bytes must be a positive integer")
        asset_id = f"asset:{raw_sha256}"
        if self.asset_id != asset_id:
            raise ValueError("asset_id must bind raw_sha256")
        object.__setattr__(self, "source_pdf_sha256", source_pdf_sha256)
        object.__setattr__(self, "raw_sha256", raw_sha256)
        object.__setattr__(self, "pixel_size", tuple(self.pixel_size))
        object.__setattr__(self, "smask_sha256", smask_sha256)


@dataclass(frozen=True)
class PdfImageAsset:
    source_pdf_sha256: str
    xref: int
    raw_bytes: bytes = field(repr=False)
    pixel_size: tuple[int, int]
    filter_name: str
    color_space: str
    bits_per_component: int
    smask_xref: int | None = None
    smask_sha256: str | None = None
    smask_size_bytes: int | None = None
    raw_sha256: str = field(init=False)
    asset_id: str = field(init=False)

    @classmethod
    def from_raw(cls, **values: object) -> PdfImageAsset:
        return cls(**values)

    def __post_init__(self) -> None:
        source_pdf_sha256 = str(self.source_pdf_sha256).strip().lower()
        if _SHA256_RE.fullmatch(source_pdf_sha256) is None:
            raise ValueError("source_pdf_sha256 must contain 64 hexadecimal characters")
        if isinstance(self.xref, bool) or not isinstance(self.xref, int) or self.xref <= 0:
            raise ValueError("xref must be a positive integer")
        if not isinstance(self.raw_bytes, bytes) or not self.raw_bytes:
            raise TypeError("raw_bytes must be non-empty bytes")
        if not self.raw_bytes.startswith(b"\xff\xd8") or not self.raw_bytes.endswith(b"\xff\xd9"):
            raise ValueError("raw_bytes must contain an original JPEG stream")
        if len(self.pixel_size) != 2 or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in self.pixel_size
        ):
            raise ValueError("pixel_size must contain positive integers")
        if self.filter_name != "/DCTDecode":
            raise ValueError("filter_name must be /DCTDecode for a JPEG asset")
        if self.color_space != "/DeviceRGB":
            raise ValueError("color_space must be /DeviceRGB")
        if (
            isinstance(self.bits_per_component, bool)
            or not isinstance(self.bits_per_component, int)
            or self.bits_per_component != 8
        ):
            raise ValueError("bits_per_component must be 8")
        if len(
            {
                self.smask_xref is None,
                self.smask_sha256 is None,
                self.smask_size_bytes is None,
            }
        ) != 1:
            raise ValueError("smask xref, hash, and size must be provided together")
        smask_sha256 = self.smask_sha256
        if self.smask_xref is not None:
            if isinstance(self.smask_xref, bool) or not isinstance(self.smask_xref, int) or self.smask_xref <= 0:
                raise ValueError("smask_xref must be a positive integer")
            smask_sha256 = str(smask_sha256).strip().lower()
            if _SHA256_RE.fullmatch(smask_sha256) is None:
                raise ValueError("smask_sha256 must contain 64 hexadecimal characters")
            if (
                isinstance(self.smask_size_bytes, bool)
                or not isinstance(self.smask_size_bytes, int)
                or self.smask_size_bytes <= 0
            ):
                raise ValueError("smask_size_bytes must be a positive integer")

        try:
            from PIL import Image

            with Image.open(BytesIO(self.raw_bytes)) as image:
                image.verify()
            with Image.open(BytesIO(self.raw_bytes)) as image:
                decoded_size = image.size
        except Exception as exc:
            raise ValueError("raw_bytes must decode as JPEG") from exc
        if decoded_size != tuple(self.pixel_size):
            raise ValueError("decoded JPEG dimensions do not match pixel_size")

        raw_sha256 = hashlib.sha256(self.raw_bytes).hexdigest()
        object.__setattr__(self, "source_pdf_sha256", source_pdf_sha256)
        object.__setattr__(self, "pixel_size", tuple(self.pixel_size))
        object.__setattr__(self, "smask_sha256", smask_sha256)
        object.__setattr__(self, "raw_sha256", raw_sha256)
        object.__setattr__(self, "asset_id", f"asset:{raw_sha256}")

    def descriptor(self) -> PdfImageAssetDescriptor:
        return PdfImageAssetDescriptor(
            source_pdf_sha256=self.source_pdf_sha256,
            xref=self.xref,
            pixel_size=self.pixel_size,
            filter_name=self.filter_name,
            color_space=self.color_space,
            bits_per_component=self.bits_per_component,
            raw_sha256=self.raw_sha256,
            raw_size_bytes=len(self.raw_bytes),
            smask_xref=self.smask_xref,
            smask_sha256=self.smask_sha256,
            smask_size_bytes=self.smask_size_bytes,
            asset_id=self.asset_id,
        )


@dataclass(frozen=True)
class ImageOccurrence:
    source_pdf_sha256: str
    asset_sha256: str
    lesson_number: int | None
    page_number: int
    draw_index: int
    xref: int
    xobject_name: str
    bbox_top_left: tuple[float, float, float, float]
    page_size: tuple[float, float]
    page_rotation: int
    source_role: SourceRole
    reason_codes: tuple[str, ...]
    classifier_version: str
    caption_page_number: int | None = None
    caption_source_sequence_index: int | None = None
    cropbox_pdf: tuple[float, float, float, float] | None = None
    mediabox_pdf: tuple[float, float, float, float] | None = None
    draw_bbox_top_left: tuple[float, float, float, float] | None = None
    bbox_pdf_bottom_left: tuple[float, float, float, float] = field(init=False)
    draw_bbox_pdf_bottom_left: tuple[float, float, float, float] = field(init=False)
    occurrence_id: str = field(init=False)
    classification_id: str = field(init=False)

    @classmethod
    def create(cls, **values: object) -> ImageOccurrence:
        return cls(**values)

    def __post_init__(self) -> None:
        source_pdf_sha256 = str(self.source_pdf_sha256).strip().lower()
        asset_sha256 = str(self.asset_sha256).strip().lower()
        if _SHA256_RE.fullmatch(source_pdf_sha256) is None:
            raise ValueError("source_pdf_sha256 must contain 64 hexadecimal characters")
        if _SHA256_RE.fullmatch(asset_sha256) is None:
            raise ValueError("asset_sha256 must contain 64 hexadecimal characters")
        if self.lesson_number is not None and (
            isinstance(self.lesson_number, bool)
            or not isinstance(self.lesson_number, int)
            or not 0 <= self.lesson_number <= 108
        ):
            raise ValueError("lesson_number must be between 0 and 108 or None")
        for field_name, value, allow_zero in (
            ("page_number", self.page_number, False),
            ("draw_index", self.draw_index, True),
            ("xref", self.xref, False),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or (not allow_zero and value == 0)
            ):
                raise ValueError(f"{field_name} must be a valid integer")
        if not isinstance(self.xobject_name, str):
            raise TypeError("xobject_name must be a string")
        xobject_name = self.xobject_name.strip().lstrip("/")
        if not xobject_name or any(separator in xobject_name for separator in ("/", "\\")):
            raise ValueError("xobject_name must be a simple name")
        if len(self.page_size) != 2 or not all(_finite_number(value) for value in self.page_size):
            raise ValueError("page_size must contain finite width and height")
        page_width, page_height = (float(value) for value in self.page_size)
        if page_width <= 0 or page_height <= 0:
            raise ValueError("page_size must be positive")
        if len(self.bbox_top_left) != 4 or not all(_finite_number(value) for value in self.bbox_top_left):
            raise ValueError("bbox_top_left must contain four finite coordinates")
        x0, top, x1, bottom = (float(value) for value in self.bbox_top_left)
        if not (0 <= x0 < x1 <= page_width and 0 <= top < bottom <= page_height):
            raise ValueError("bbox_top_left must be inside page_size")
        draw_bbox = self.draw_bbox_top_left
        if draw_bbox is None:
            draw_bbox = (x0, top, x1, bottom)
        if len(draw_bbox) != 4 or not all(_finite_number(value) for value in draw_bbox):
            raise ValueError("draw_bbox_top_left must contain four finite coordinates")
        draw_x0, draw_top, draw_x1, draw_bottom = (float(value) for value in draw_bbox)
        if not draw_x0 < draw_x1 or not draw_top < draw_bottom:
            raise ValueError("draw_bbox_top_left must be ordered")
        draw_bbox = (draw_x0, draw_top, draw_x1, draw_bottom)
        if (
            isinstance(self.page_rotation, bool)
            or not isinstance(self.page_rotation, int)
            or self.page_rotation not in {0, 90, 180, 270}
        ):
            raise ValueError("page_rotation must be 0, 90, 180, or 270")
        role = SourceRole(self.source_role)
        if role not in {
            SourceRole.LESSON_CHART,
            SourceRole.EDITOR_IMAGE,
            SourceRole.UNKNOWN_IMAGE,
        }:
            raise ValueError("image occurrence requires an image source role")
        if self.lesson_number is None and self.page_number >= 7:
            raise ValueError("lesson_number can be None only before lesson coverage")
        caption_values = (
            self.caption_page_number,
            self.caption_source_sequence_index,
        )
        if (caption_values[0] is None) != (caption_values[1] is None):
            raise ValueError("caption page and source sequence must be provided together")
        if caption_values[0] is not None:
            if (
                isinstance(caption_values[0], bool)
                or not isinstance(caption_values[0], int)
                or caption_values[0] != self.page_number
                or isinstance(caption_values[1], bool)
                or not isinstance(caption_values[1], int)
                or caption_values[1] < 0
            ):
                raise ValueError("caption source position must be valid and on the image page")
        if role is SourceRole.LESSON_CHART and caption_values[0] is None:
            raise ValueError("lesson chart requires a verified caption source position")
        cropbox_pdf = _optional_box(self.cropbox_pdf, "cropbox_pdf")
        mediabox_pdf = _optional_box(self.mediabox_pdf, "mediabox_pdf")
        if not isinstance(self.classifier_version, str):
            raise TypeError("classifier_version must be a string")
        classifier_version = self.classifier_version.strip()
        if not classifier_version or len(classifier_version) > 128:
            raise ValueError("classifier_version must be present and bounded")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(code, str) or not code.strip() or len(code.strip()) > 128
            for code in self.reason_codes
        ):
            raise ValueError("reason_codes must be a tuple of bounded strings")
        reason_codes = tuple(sorted(set(code.strip() for code in self.reason_codes)))
        if not reason_codes:
            raise ValueError("reason_codes must not be empty")
        bbox_top_left = (x0, top, x1, bottom)
        bbox_pdf_bottom_left = (x0, page_height - bottom, x1, page_height - top)
        draw_bbox_pdf_bottom_left = (
            draw_x0,
            page_height - draw_bottom,
            draw_x1,
            page_height - draw_top,
        )
        physical_payload = {
            "asset_sha256": asset_sha256,
            "cropbox_pdf": list(cropbox_pdf) if cropbox_pdf is not None else None,
            "draw_index": self.draw_index,
            "draw_bbox_top_left": list(draw_bbox),
            "mediabox_pdf": list(mediabox_pdf) if mediabox_pdf is not None else None,
            "page_number": self.page_number,
            "page_rotation": self.page_rotation,
            "page_size": [page_width, page_height],
            "source_pdf_sha256": source_pdf_sha256,
            "xobject_name": xobject_name,
            "xref": self.xref,
        }
        serialized_physical = json.dumps(
            physical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        occurrence_id = "occurrence:" + hashlib.sha256(serialized_physical).hexdigest()
        classification_payload = {
            "bbox_top_left": list(bbox_top_left),
            "caption_page_number": caption_values[0],
            "caption_source_sequence_index": caption_values[1],
            "classifier_version": classifier_version,
            "lesson_number": self.lesson_number,
            "occurrence_id": occurrence_id,
            "reason_codes": list(reason_codes),
            "source_role": role.value,
        }
        serialized_classification = json.dumps(
            classification_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        object.__setattr__(self, "source_pdf_sha256", source_pdf_sha256)
        object.__setattr__(self, "asset_sha256", asset_sha256)
        object.__setattr__(self, "xobject_name", xobject_name)
        object.__setattr__(self, "bbox_top_left", bbox_top_left)
        object.__setattr__(self, "draw_bbox_top_left", draw_bbox)
        object.__setattr__(self, "page_size", (page_width, page_height))
        object.__setattr__(self, "source_role", role)
        object.__setattr__(self, "reason_codes", reason_codes)
        object.__setattr__(self, "classifier_version", classifier_version)
        object.__setattr__(self, "cropbox_pdf", cropbox_pdf)
        object.__setattr__(self, "mediabox_pdf", mediabox_pdf)
        object.__setattr__(self, "bbox_pdf_bottom_left", bbox_pdf_bottom_left)
        object.__setattr__(self, "draw_bbox_pdf_bottom_left", draw_bbox_pdf_bottom_left)
        object.__setattr__(self, "occurrence_id", occurrence_id)
        object.__setattr__(
            self,
            "classification_id",
            "classification:" + hashlib.sha256(serialized_classification).hexdigest(),
        )

    @property
    def caption_source_position(self) -> tuple[int, int] | None:
        if self.caption_page_number is None:
            return None
        return self.caption_page_number, self.caption_source_sequence_index
