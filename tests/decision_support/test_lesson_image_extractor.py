from __future__ import annotations

import hashlib

from chanlun.decision_support.lesson_corpus import LessonTextBlock, SourceRole
from chanlun.decision_support.lesson_image_extractor import extract_page_image_evidence
from tests.decision_support.test_corpus_integrity import _VALID_JPEG


class _Name:
    def __init__(self, name: str) -> None:
        self.name = name


class _Stream:
    def __init__(
        self,
        objid: int,
        rawdata: bytes,
        attrs: dict[str, object],
    ) -> None:
        self.objid = objid
        self.rawdata = rawdata
        self.attrs = attrs


class _Ref:
    def __init__(self, stream: _Stream) -> None:
        self.objid = stream.objid
        self._stream = stream

    def resolve(self) -> _Stream:
        return self._stream


class _Page:
    width = 595.3
    height = 841.9
    rotation = 0
    cropbox = (0.0, 0.0, 595.3, 841.9)
    mediabox = (0.0, 0.0, 595.3, 841.9)

    def __init__(self) -> None:
        smask = _Stream(44, b"alpha-mask", {"Filter": _Name("FlateDecode")})
        stream = _Stream(
            43,
            _VALID_JPEG,
            {
                "BitsPerComponent": 8,
                "ColorSpace": _Name("DeviceRGB"),
                "Filter": _Name("DCTDecode"),
                "Height": 3,
                "SMask": _Ref(smask),
                "Width": 2,
            },
        )
        self.images = [
            {
                "name": "IM43",
                "stream": stream,
                "x0": 90.0,
                "top": 100.0,
                "x1": 505.0,
                "bottom": 300.0,
            },
            {
                "name": "IM43",
                "stream": stream,
                "x0": 90.0,
                "top": 400.0,
                "x1": 505.0,
                "bottom": 600.0,
            },
        ]


def test_extract_page_image_evidence_deduplicates_assets_but_keeps_draws() -> None:
    caption = LessonTextBlock(
        lesson_number=16,
        page_number=263,
        bbox=(288.0, 306.0, 307.0, 318.0),
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_sequence_index=20,
        color_rgb=(0, 0, 0),
        source_role=SourceRole.LESSON_BODY,
        text="图1",
    )

    result = extract_page_image_evidence(
        _Page(),
        page_number=263,
        lesson_number=16,
        source_pdf_sha256="a" * 64,
        page_text_blocks=(caption,),
        classifier_version="lesson-image/1",
    )

    digest = hashlib.sha256(_VALID_JPEG).hexdigest()
    assert len(result.assets) == 1
    assert result.assets[0].raw_sha256 == digest
    assert result.assets[0].smask_xref == 44
    assert result.assets[0].smask_sha256 == hashlib.sha256(b"alpha-mask").hexdigest()
    assert len(result.occurrences) == 2
    assert result.occurrences[0].source_role is SourceRole.LESSON_CHART
    assert result.occurrences[1].source_role is SourceRole.UNKNOWN_IMAGE
    assert result.occurrences[0].occurrence_id != result.occurrences[1].occurrence_id
    assert result.materialized_raw_by_sha256 == {digest: _VALID_JPEG}


def test_front_matter_bleed_records_raw_draw_box_and_clipped_visible_box() -> None:
    page = _Page()
    page.images = [
        {
            **page.images[0],
            "x0": -0.48,
            "top": -1.56,
            "x1": 595.68,
            "bottom": 846.48,
        }
    ]

    result = extract_page_image_evidence(
        page,
        page_number=1,
        lesson_number=None,
        source_pdf_sha256="a" * 64,
        page_text_blocks=(),
        classifier_version="lesson-image/1",
    )

    occurrence = result.occurrences[0]
    primary_sha256 = hashlib.sha256(_VALID_JPEG).hexdigest()
    smask_sha256 = hashlib.sha256(b"alpha-mask").hexdigest()
    assert occurrence.draw_bbox_top_left == (-0.48, -1.56, 595.68, 846.48)
    assert occurrence.bbox_top_left == (0.0, 0.0, 595.3, 841.9)
    assert occurrence.source_role is SourceRole.UNKNOWN_IMAGE
    assert result.primary_raw_by_sha256 == {primary_sha256: _VALID_JPEG}
    assert result.smask_raw_by_sha256 == {smask_sha256: b"alpha-mask"}
    assert result.materialized_raw_by_sha256 == result.primary_raw_by_sha256
