from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from chanlun.decision_support.lesson_corpus import PageSpan, PdfIdentity
from chanlun.decision_support.lesson_span_cache import (
    build_lesson_span_cache,
    load_lesson_span_cache,
)


def _identity() -> PdfIdentity:
    payload = b"trusted-source"
    return PdfIdentity(
        filename="source.pdf",
        size_bytes=len(payload),
        page_count=8,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _spans() -> tuple[PageSpan, ...]:
    return (
        PageSpan(
            page_number=7,
            bbox=(10.0, 20.0, 100.0, 32.0),
            page_size=(200.0, 300.0),
            text=" ① ",
            color_rgb=(0, 0, 0),
            source_sequence_index=0,
            cropbox_pdf=(0.0, 0.0, 200.0, 300.0),
            mediabox_pdf=(0.0, 0.0, 200.0, 300.0),
        ),
        PageSpan(
            page_number=8,
            bbox=(10.0, 40.0, 100.0, 52.0),
            page_size=(200.0, 300.0),
            text="正文",
            color_rgb=(0, 0, 0),
            source_sequence_index=0,
            cropbox_pdf=(0.0, 0.0, 200.0, 300.0),
            mediabox_pdf=(0.0, 0.0, 200.0, 300.0),
        ),
    )


def test_span_cache_is_deterministic_and_round_trips_raw_evidence(tmp_path: Path) -> None:
    first = build_lesson_span_cache(
        tmp_path / "first",
        identity=_identity(),
        spans=_spans(),
        extractor_version="lesson-pdf/1",
        first_page=7,
        last_page=8,
    )
    second = build_lesson_span_cache(
        tmp_path / "second",
        identity=_identity(),
        spans=tuple(reversed(_spans())),
        extractor_version="lesson-pdf/1",
        first_page=7,
        last_page=8,
    )

    loaded = load_lesson_span_cache(first, expected_identity=_identity())
    rows = [
        json.loads(line)
        for line in (first / "text_spans.jsonl").read_text(encoding="utf-8").splitlines()
    ]

    assert tuple(span.raw_text for span in loaded) == (" ① ", "正文")
    assert rows[0]["raw_text_sha256"] != rows[0]["normalized_text_sha256"]
    assert rows[0]["normalized_text"] == "1"
    assert (first / "text_spans.jsonl").read_bytes() == (second / "text_spans.jsonl").read_bytes()
    assert (first / "manifest.json").read_bytes() == (second / "manifest.json").read_bytes()


def test_span_cache_rejects_tampering_before_deserializing(tmp_path: Path) -> None:
    target = build_lesson_span_cache(
        tmp_path / "cache",
        identity=_identity(),
        spans=_spans(),
        extractor_version="lesson-pdf/1",
        first_page=7,
        last_page=8,
    )
    span_path = target / "text_spans.jsonl"
    span_path.write_bytes(span_path.read_bytes().replace("正文".encode(), "伪文".encode()))

    with pytest.raises(ValueError, match="hash"):
        load_lesson_span_cache(target, expected_identity=_identity())

