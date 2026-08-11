from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .lesson_corpus import PageSpan, PdfIdentity


_SPAN_KEYS = frozenset(
    {
        "bbox",
        "color_rgb",
        "cropbox_pdf",
        "mediabox_pdf",
        "normalized_text",
        "normalized_text_sha256",
        "page_number",
        "page_rotation",
        "page_size",
        "raw_text",
        "raw_text_sha256",
        "source_sequence_index",
    }
)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _span_dict(span: PageSpan) -> dict[str, object]:
    return {
        "bbox": list(span.bbox),
        "color_rgb": list(span.color_rgb) if span.color_rgb is not None else None,
        "cropbox_pdf": list(span.cropbox_pdf) if span.cropbox_pdf is not None else None,
        "mediabox_pdf": list(span.mediabox_pdf) if span.mediabox_pdf is not None else None,
        "normalized_text": span.normalized_text,
        "normalized_text_sha256": span.normalized_sha256,
        "page_number": span.page_number,
        "page_rotation": span.page_rotation,
        "page_size": list(span.page_size),
        "raw_text": span.raw_text,
        "raw_text_sha256": span.raw_sha256,
        "source_sequence_index": span.source_sequence_index,
    }


def _ordered_spans(spans: tuple[PageSpan, ...] | list[PageSpan]) -> tuple[PageSpan, ...]:
    values = tuple(spans)
    if any(not isinstance(span, PageSpan) for span in values):
        raise TypeError("spans must contain PageSpan values")
    positions = tuple((span.page_number, span.source_sequence_index) for span in values)
    if len(set(positions)) != len(positions):
        raise ValueError("source_sequence_index must be unique within each page")
    return tuple(sorted(values, key=lambda span: (span.page_number, span.source_sequence_index)))


def build_lesson_span_cache(
    target: Path,
    *,
    identity: PdfIdentity,
    spans: tuple[PageSpan, ...] | list[PageSpan],
    extractor_id: str,
    first_page: int,
    last_page: int,
) -> Path:
    if not isinstance(identity, PdfIdentity):
        raise TypeError("identity must be PdfIdentity")
    if not isinstance(extractor_id, str):
        raise TypeError("extractor_id must be a string")
    extractor_identity = extractor_id.strip()
    if not extractor_identity or len(extractor_identity) > 128:
        raise ValueError("extractor_id must be present and bounded")
    if (
        isinstance(first_page, bool)
        or not isinstance(first_page, int)
        or isinstance(last_page, bool)
        or not isinstance(last_page, int)
        or first_page <= 0
        or first_page > last_page
        or last_page > identity.page_count
    ):
        raise ValueError("cache page range must be inside the verified PDF")
    ordered = _ordered_spans(spans)
    if any(not first_page <= span.page_number <= last_page for span in ordered):
        raise ValueError("all spans must be inside the cache page range")

    target_path = Path(target).absolute()
    if target_path.is_symlink():
        raise ValueError("target must not be a symbolic link")
    if target_path.exists():
        raise FileExistsError(f"target already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(
        tempfile.mkdtemp(prefix=f".{target_path.name}.staging-", dir=target_path.parent)
    )
    try:
        span_bytes = b"".join(_json_bytes(_span_dict(span)) for span in ordered)
        span_path = staging / "text_spans.jsonl"
        span_path.write_bytes(span_bytes)
        manifest = {
            "coverage": {"first_page": first_page, "last_page": last_page},
            "extractor_id": extractor_identity,
            "package_kind": "chanlun_pdf_text_span_cache",
            "schema": "current",
            "source_pdf": asdict(identity),
            "span_count": len(ordered),
            "text_spans": {
                "path": "text_spans.jsonl",
                "sha256": hashlib.sha256(span_bytes).hexdigest(),
                "size_bytes": len(span_bytes),
            },
        }
        (staging / "manifest.json").write_bytes(_json_bytes(manifest))
        load_lesson_span_cache(staging, expected_identity=identity)
        os.replace(staging, target_path)
        return target_path.resolve()
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _identity_from_manifest(value: object) -> PdfIdentity:
    if not isinstance(value, dict):
        raise ValueError("cache source_pdf must be an object")
    try:
        return PdfIdentity(
            filename=value["filename"],
            size_bytes=value["size_bytes"],
            page_count=value["page_count"],
            sha256=value["sha256"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cache source_pdf identity is invalid") from exc


def _span_from_dict(value: object) -> PageSpan:
    if not isinstance(value, dict) or frozenset(value) != _SPAN_KEYS:
        raise ValueError("cache span schema is invalid")
    try:
        span = PageSpan(
            page_number=value["page_number"],
            bbox=tuple(value["bbox"]),
            page_size=tuple(value["page_size"]),
            text=value["raw_text"],
            color_rgb=(tuple(value["color_rgb"]) if value["color_rgb"] is not None else None),
            page_rotation=value["page_rotation"],
            source_sequence_index=value["source_sequence_index"],
            cropbox_pdf=(tuple(value["cropbox_pdf"]) if value["cropbox_pdf"] is not None else None),
            mediabox_pdf=(tuple(value["mediabox_pdf"]) if value["mediabox_pdf"] is not None else None),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("cache span payload is invalid") from exc
    if (
        value["raw_text_sha256"] != span.raw_sha256
        or value["normalized_text"] != span.normalized_text
        or value["normalized_text_sha256"] != span.normalized_sha256
    ):
        raise ValueError("cache span text or hash is inconsistent")
    return span


def load_lesson_span_cache(
    root: Path,
    *,
    expected_identity: PdfIdentity,
) -> tuple[PageSpan, ...]:
    if not isinstance(expected_identity, PdfIdentity):
        raise TypeError("expected_identity must be PdfIdentity")
    root_path = Path(root).resolve()
    manifest_path = root_path / "manifest.json"
    span_path = root_path / "text_spans.jsonl"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cache manifest is unreadable") from exc
    if not isinstance(manifest, dict) or manifest.get("package_kind") != "chanlun_pdf_text_span_cache":
        raise ValueError("cache manifest kind is invalid")
    actual_identity = _identity_from_manifest(manifest.get("source_pdf"))
    if actual_identity != expected_identity:
        raise ValueError("cache source PDF identity mismatch")
    descriptor = manifest.get("text_spans")
    if not isinstance(descriptor, dict) or descriptor.get("path") != "text_spans.jsonl":
        raise ValueError("cache text span descriptor is invalid")
    try:
        span_bytes = span_path.read_bytes()
    except OSError as exc:
        raise ValueError("cache text spans are unreadable") from exc
    if (
        descriptor.get("size_bytes") != len(span_bytes)
        or descriptor.get("sha256") != hashlib.sha256(span_bytes).hexdigest()
    ):
        raise ValueError("cache text span hash or size mismatch")
    try:
        rows = tuple(
            json.loads(line)
            for line in span_bytes.decode("utf-8").splitlines()
            if line
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cache text span JSONL is invalid") from exc
    spans = _ordered_spans(tuple(_span_from_dict(row) for row in rows))
    if manifest.get("span_count") != len(spans):
        raise ValueError("cache span count mismatch")
    coverage = manifest.get("coverage")
    if not isinstance(coverage, dict):
        raise ValueError("cache coverage is invalid")
    first_page, last_page = coverage.get("first_page"), coverage.get("last_page")
    if any(not first_page <= span.page_number <= last_page for span in spans):
        raise ValueError("cache span lies outside declared coverage")
    return spans
