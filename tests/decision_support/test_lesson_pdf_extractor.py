from __future__ import annotations

from chanlun.decision_support.lesson_pdf_extractor import extract_page_spans


class _FakePage:
    width = 200.0
    height = 300.0
    rotation = 90
    cropbox = (10.0, 20.0, 210.0, 320.0)
    mediabox = (0.0, 0.0, 220.0, 340.0)

    def extract_text_lines(self, **kwargs: object) -> list[dict[str, object]]:
        assert kwargs == {"layout": False, "strip": False, "return_chars": True}
        return [
            {
                "text": "原文 注释 摘录",
                "chars": [
                    _char("原", 10.0, (0.0, 0.0, 0.0)),
                    _char("文", 20.0, (0.0, 0.0, 0.0)),
                    _char("注", 45.0, (1.0, 0.298, 0.255)),
                    _char("释", 55.0, (1.0, 0.298, 0.255)),
                    _char("摘", 80.0, (0.0, 0.82, 0.0)),
                    _char("录", 90.0, (0.0, 0.82, 0.0)),
                ],
            },
            {
                "text": "  ",
                "chars": [
                    _char(" ", 10.0, (0.0, 0.0, 0.0), top=40.0),
                ],
            },
        ]


def _char(
    text: str,
    x0: float,
    color: tuple[float, float, float],
    *,
    top: float = 20.0,
) -> dict[str, object]:
    return {
        "text": text,
        "x0": x0,
        "x1": x0 + 9.0,
        "top": top,
        "bottom": top + 12.0,
        "non_stroking_color": color,
    }


def test_extract_page_spans_preserves_character_stream_and_splits_color_runs() -> None:
    spans = extract_page_spans(_FakePage(), page_number=263)

    assert tuple(span.raw_text for span in spans) == ("原文", "注释", "摘录")
    assert tuple(span.color_rgb for span in spans) == (
        (0, 0, 0),
        (255, 76, 65),
        (0, 209, 0),
    )
    assert tuple(span.source_sequence_index for span in spans) == (0, 1, 2)
    assert all(span.page_rotation == 90 for span in spans)
    assert all(span.cropbox_pdf == (10.0, 20.0, 210.0, 320.0) for span in spans)
    assert all(span.mediabox_pdf == (0.0, 0.0, 220.0, 340.0) for span in spans)
    assert "".join(span.raw_text for span in spans) == "原文注释摘录"

