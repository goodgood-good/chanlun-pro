from __future__ import annotations

from chanlun.decision_support.lesson_corpus import (
    ImagePlacement,
    LessonTextBlock,
    SourceRole,
)
from chanlun.decision_support.lesson_image_classifier import classify_image_evidence


def _text(
    sequence: int,
    text: str,
    *,
    bbox: tuple[float, float, float, float],
    color: tuple[int, int, int] = (0, 0, 0),
    role: SourceRole = SourceRole.LESSON_BODY,
) -> LessonTextBlock:
    return LessonTextBlock(
        lesson_number=16,
        page_number=263,
        bbox=bbox,
        page_size=(595.3, 841.9),
        page_rotation=0,
        source_sequence_index=sequence,
        color_rgb=color,
        source_role=role,
        text=text,
    )


def _placement(
    name: str,
    bbox: tuple[float, float, float, float],
) -> ImagePlacement:
    return ImagePlacement(
        page_number=263,
        bbox=bbox,
        page_size=(595.3, 841.9),
        xobject_name=name,
        pixel_size=(830, 412),
        sha256="a" * 64,
    )


def test_flowchart_legend_classifies_editor_image_before_caption_rules() -> None:
    decision = classify_image_evidence(
        _placement("IM874", (90.0, 114.96, 504.72, 321.12)),
        (
            _text(
                2,
                "教你炒股票16：中小资金的高效买卖法流程图中心态策略部分",
                bbox=(153.9, 336.1, 441.1, 346.7),
                role=SourceRole.EDITOR_NOTE,
            ),
            _text(
                3,
                "（红色斜体为笔者理解备注，",
                bbox=(89.5, 357.5, 248.6, 368.6),
                color=(255, 0, 0),
                role=SourceRole.EDITOR_NOTE,
            ),
        ),
    )

    assert decision.source_role is SourceRole.EDITOR_IMAGE
    assert decision.reason_codes == ("editor_flowchart_context",)
    assert decision.caption_source_position is None


def test_black_original_caption_below_promotes_only_the_linked_occurrence() -> None:
    decision = classify_image_evidence(
        _placement("IM875", (90.0, 445.8, 505.2, 553.32)),
        (
            _text(
                20,
                "图1",
                bbox=(288.0, 562.7, 307.4, 573.8),
            ),
            _text(
                3,
                "绿色斜体为原缠其他摘录协助理解",
                bbox=(246.1, 357.5, 441.3, 368.6),
                color=(0, 209, 0),
                role=SourceRole.EDITOR_NOTE,
            ),
        ),
    )

    assert decision.source_role is SourceRole.LESSON_CHART
    assert decision.reason_codes == ("verified_black_caption_below",)
    assert decision.caption_source_position == (263, 20)


def test_missing_or_ambiguous_caption_fails_closed() -> None:
    placement = _placement("IM43", (115.8, 492.0, 478.8, 733.8))
    missing = classify_image_evidence(placement, ())
    ambiguous = classify_image_evidence(
        placement,
        (
            _text(30, "图1", bbox=(200.0, 738.0, 230.0, 750.0)),
            _text(31, "图2", bbox=(260.0, 738.0, 290.0, 750.0)),
        ),
    )

    assert missing.source_role is SourceRole.UNKNOWN_IMAGE
    assert missing.reason_codes == ("no_verified_caption",)
    assert ambiguous.source_role is SourceRole.UNKNOWN_IMAGE
    assert ambiguous.reason_codes == ("ambiguous_caption",)
