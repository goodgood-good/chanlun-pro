from __future__ import annotations

import math
from typing import Any

from .lesson_corpus import PageSpan, normalize_pdf_color


def _finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _page_box(page: object, field_name: str) -> tuple[float, float, float, float]:
    value = getattr(page, field_name, None)
    if value is None or len(value) != 4 or not all(_finite(item) for item in value):
        raise ValueError(f"page {field_name} must contain four finite coordinates")
    x0, y0, x1, y1 = (float(item) for item in value)
    if not x0 < x1 or not y0 < y1:
        raise ValueError(f"page {field_name} must be ordered")
    return x0, y0, x1, y1


def _char_geometry(char: dict[str, Any]) -> tuple[float, float, float, float]:
    values = tuple(char.get(field) for field in ("x0", "top", "x1", "bottom"))
    if not all(_finite(value) for value in values):
        raise ValueError("PDF character geometry must contain four finite coordinates")
    x0, top, x1, bottom = (float(value) for value in values)
    if not x0 < x1 or not top < bottom:
        raise ValueError("PDF character geometry must be ordered")
    return x0, top, x1, bottom


def _bounded_bbox(
    geometry: tuple[float, float, float, float],
    *,
    page_width: float,
    page_height: float,
    tolerance: float = 0.01,
) -> tuple[float, float, float, float]:
    x0, top, x1, bottom = geometry
    if (
        x0 < -tolerance
        or top < -tolerance
        or x1 > page_width + tolerance
        or bottom > page_height + tolerance
    ):
        raise ValueError("PDF character geometry lies outside the page")
    bounded = (
        max(0.0, x0),
        max(0.0, top),
        min(page_width, x1),
        min(page_height, bottom),
    )
    if not bounded[0] < bounded[2] or not bounded[1] < bounded[3]:
        raise ValueError("bounded PDF character geometry is empty")
    return bounded


def extract_page_spans(page: object, *, page_number: int) -> tuple[PageSpan, ...]:
    if (
        isinstance(page_number, bool)
        or not isinstance(page_number, int)
        or page_number <= 0
    ):
        raise ValueError("page_number must be a positive integer")
    width = getattr(page, "width", None)
    height = getattr(page, "height", None)
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
    extractor = getattr(page, "extract_text_lines", None)
    if not callable(extractor):
        raise TypeError("page must provide extract_text_lines")
    lines = extractor(layout=False, strip=False, return_chars=True)
    if not isinstance(lines, list):
        raise TypeError("extract_text_lines must return a list")

    spans: list[PageSpan] = []
    sequence = 0
    for line in lines:
        if not isinstance(line, dict):
            raise TypeError("text lines must be dictionaries")
        chars = line.get("chars")
        if not isinstance(chars, list):
            raise TypeError("text lines must include a character list")
        runs: list[dict[str, Any]] = []
        for char in chars:
            if not isinstance(char, dict):
                raise TypeError("PDF characters must be dictionaries")
            text = char.get("text")
            if not isinstance(text, str):
                raise TypeError("PDF character text must be a string")
            if not text:
                continue
            color = normalize_pdf_color(char.get("non_stroking_color"))
            bbox = _bounded_bbox(
                _char_geometry(char),
                page_width=page_width,
                page_height=page_height,
            )
            if not runs or runs[-1]["color"] != color:
                runs.append({"color": color, "text": text, "bbox": list(bbox)})
            else:
                run = runs[-1]
                run["text"] += text
                run_bbox = run["bbox"]
                run_bbox[0] = min(run_bbox[0], bbox[0])
                run_bbox[1] = min(run_bbox[1], bbox[1])
                run_bbox[2] = max(run_bbox[2], bbox[2])
                run_bbox[3] = max(run_bbox[3], bbox[3])

        for run in runs:
            if not run["text"].strip():
                continue
            spans.append(
                PageSpan(
                    page_number=page_number,
                    bbox=tuple(run["bbox"]),
                    page_size=(page_width, page_height),
                    text=run["text"],
                    color_rgb=run["color"],
                    page_rotation=rotation,
                    source_sequence_index=sequence,
                    cropbox_pdf=cropbox_pdf,
                    mediabox_pdf=mediabox_pdf,
                )
            )
            sequence += 1
    return tuple(spans)

