from __future__ import annotations

import pytest

from chanlun.decision_support.lesson_corpus import PageSpan, SourceRole
from chanlun.decision_support.lesson_pdf import (
    ReplyRecordAudit,
    classify_lesson_text,
    detect_lesson_boundaries,
    parse_lesson_heading,
)


PAGE_SIZE = (595.3, 841.9)


def _span(
    page: int,
    sequence: int,
    text: str,
    *,
    color: tuple[int, int, int] | None = (0, 0, 0),
    top: float | None = None,
) -> PageSpan:
    y = float(90 + sequence * 22) if top is None else top
    return PageSpan(
        page_number=page,
        bbox=(80.0, y, 520.0, y + 18.0),
        page_size=PAGE_SIZE,
        text=text,
        color_rgb=color,
        source_sequence_index=sequence,
    )


def test_lesson_boundary_requires_first_body_line_and_expected_next_number() -> None:
    spans = (
        _span(7, 0, "天使版---教你炒股票 108 课", top=44.0),
        _span(7, 1, "教你炒股票 0：序 言"),
        _span(8, 0, "本课正文。"),
        _span(8, 1, "教你炒股票 1：正文中的引用，不是新课。"),
        _span(9, 0, "教你炒股票 2：课号超前，不能跳过第 1 课。"),
        _span(10, 0, "教你炒股票 1：不会赢钱的经济人，只会套牢一辈子"),
        _span(11, 0, "教你炒股票 2：没有庄家，有的只是赢家和输家"),
        _span(12, 0, "第 12 页 共 12 页", top=800.0),
    )

    boundaries = detect_lesson_boundaries(
        spans,
        expected_lesson_numbers=(0, 1, 2),
        expected_first_page=7,
        expected_last_page=12,
    )

    assert tuple((item.lesson_number, item.page_start, item.page_end) for item in boundaries) == (
        (0, 7, 9),
        (1, 10, 10),
        (2, 11, 12),
    )


def test_lesson_heading_accepts_spaced_digits_but_not_embedded_title() -> None:
    assert parse_lesson_heading("教你炒股票 1 0：2005 年中国股市大预言") == (
        10,
        "教你炒股票 1 0:2005 年中国股市大预言",
    )
    assert parse_lesson_heading("正文提到教你炒股票 10：这里只是引用") is None


def test_reply_state_machine_uses_last_pure_equals_and_preserves_raw_text() -> None:
    spans = (
        _span(258, 0, " 教你炒股票 16：中小资金的高效买卖法 "),
        _span(258, 1, "正文。"),
        _span(258, 2, "**********1"),
        _span(258, 3, "新浪网友"),
        _span(258, 4, "2007-01-01 10:00:00"),
        _span(258, 5, "问题里有 a=b，不是分隔线。"),
        _span(258, 6, "================"),
        _span(258, 7, "补充问题。"),
        _span(258, 8, "================"),
        _span(258, 9, "缠中说禅"),
        _span(258, 10, "结构闭合后，蓝色作者文字仍可确认。", color=(0, 0, 220)),
        _span(258, 11, "(2007-01-01 12:00:00)"),
    )

    result = classify_lesson_text(16, 258, 258, spans)
    by_text = {block.raw_text: block for block in result.blocks}

    assert by_text[" 教你炒股票 16：中小资金的高效买卖法 "].source_role is SourceRole.LESSON_BODY
    assert by_text["正文。"].source_role is SourceRole.LESSON_BODY
    assert by_text["**********1"].source_role is SourceRole.EDITOR_NOTE
    assert by_text["新浪网友"].source_role is SourceRole.READER_COMMENT
    assert by_text["问题里有 a=b，不是分隔线。"].source_role is SourceRole.READER_COMMENT
    assert by_text["补充问题。"].source_role is SourceRole.READER_COMMENT
    assert by_text["缠中说禅"].source_role is SourceRole.CHAN_REPLY
    assert by_text["结构闭合后，蓝色作者文字仍可确认。"].source_role is SourceRole.CHAN_REPLY
    assert by_text["(2007-01-01 12:00:00)"].source_role is SourceRole.CHAN_REPLY
    assert result.reply_record_count == 1
    assert result.closed_reply_record_count == 1
    assert result.ambiguous_reply_record_count == 0
    assert result.reply_records[0].resolution == "reader_then_author"
    assert result.reply_records[0].reason_codes == ("last_pure_equals_split",)
    assert result.reply_records[0].separator_source_positions == (
        (258, 6),
        (258, 8),
    )
    provenance_positions = set().union(
        result.reply_records[0].reader_source_positions,
        result.reply_records[0].author_source_positions,
        result.reply_records[0].quarantined_source_positions,
        result.reply_records[0].separator_source_positions,
    )
    assert provenance_positions == set(result.reply_records[0].source_positions)


def test_no_equals_requires_standalone_author_shape_and_terminal_date() -> None:
    spans = (
        _span(52, 0, "教你炒股票 7：给赚了指数亏了钱的一些忠告"),
        _span(52, 1, "**********"),
        _span(52, 2, "缠中说禅"),
        _span(52, 3, "这是缠师独立补充。"),
        _span(52, 4, "（2006-12-01 12:00:00）"),
        _span(52, 5, "**********"),
        _span(52, 6, "新浪网友"),
        _span(52, 7, "2006-12-0112:01:00"),
        _span(52, 8, "只有读者留言，没有等号。"),
        _span(52, 9, "(2006-12-01 12:02:00)"),
    )

    result = classify_lesson_text(7, 52, 52, spans)
    by_text = {block.raw_text: block.source_role for block in result.blocks}

    assert by_text["缠中说禅"] is SourceRole.CHAN_REPLY
    assert by_text["这是缠师独立补充。"] is SourceRole.CHAN_REPLY
    assert by_text["（2006-12-01 12:00:00）"] is SourceRole.CHAN_REPLY
    assert by_text["新浪网友"] is SourceRole.READER_COMMENT
    assert by_text["只有读者留言，没有等号。"] is SourceRole.READER_COMMENT
    assert by_text["(2006-12-01 12:02:00)"] is SourceRole.READER_COMMENT
    assert result.reply_record_count == 2
    assert result.closed_reply_record_count == 2
    assert result.ambiguous_reply_record_count == 0
    assert tuple(record.resolution for record in result.reply_records) == (
        "standalone_author",
        "reader_only",
    )


def test_unclosed_reply_and_unknown_color_fail_closed() -> None:
    spans = (
        _span(944, 0, "教你炒股票 60：图解分析示范五"),
        _span(944, 1, "**********"),
        _span(944, 2, "可能是作者，也可能是读者。", color=None),
        _span(944, 3, "没有闭合日期。"),
    )

    result = classify_lesson_text(60, 944, 944, spans)
    roles = {block.raw_text: block.source_role for block in result.blocks}

    assert roles["可能是作者，也可能是读者。"] is SourceRole.UNKNOWN_TEXT
    assert roles["没有闭合日期。"] is SourceRole.UNKNOWN_TEXT
    assert result.reply_record_count == 1
    assert result.closed_reply_record_count == 0
    assert result.ambiguous_reply_record_count == 1
    assert result.reply_records[0].resolution == "ambiguous"
    assert result.reply_records[0].reason_codes == (
        "missing_terminal_parenthesized_datetime",
    )


def test_editor_colors_and_flowchart_never_inherit_structural_author_role() -> None:
    spans = (
        _span(263, 0, "教你炒股票 16：中小资金的高效买卖法"),
        _span(263, 1, "教你炒股票16：中小资金的高效买卖法流程图中心态策略部分"),
        _span(263, 2, "“"),
        _span(263, 3, "**********"),
        _span(263, 4, "读者问题。"),
        _span(263, 5, "================"),
        _span(263, 6, "红色编者按。", color=(255, 76, 65)),
        _span(263, 7, "绿色待核摘录。", color=(0, 209, 0)),
        _span(263, 8, "在流程图中，红色文字表示编者说明。"),
        _span(263, 9, "蓝色结构内作者回复。", color=(0, 0, 220)),
        _span(263, 10, "(2007-01-01 12:00:00)"),
    )

    result = classify_lesson_text(16, 263, 263, spans)
    roles = {block.raw_text: block.source_role for block in result.blocks}

    assert roles["教你炒股票16：中小资金的高效买卖法流程图中心态策略部分"] is SourceRole.EDITOR_NOTE
    assert roles["“"] is SourceRole.UNKNOWN_TEXT
    assert roles["红色编者按。"] is SourceRole.EDITOR_NOTE
    assert roles["绿色待核摘录。"] is SourceRole.EDITOR_NOTE
    assert roles["在流程图中，红色文字表示编者说明。"] is SourceRole.EDITOR_NOTE
    assert roles["蓝色结构内作者回复。"] is SourceRole.CHAN_REPLY


def test_reply_record_audit_rejects_overlapping_or_external_provenance() -> None:
    common = {
        "record_index": 0,
        "source_positions": ((7, 1), (7, 2)),
        "resolution": "reader_then_author",
        "reason_codes": ("fixture",),
    }

    with pytest.raises(ValueError, match="disjoint"):
        ReplyRecordAudit(
            **common,
            reader_source_positions=((7, 1),),
            author_source_positions=((7, 1),),
        )
    with pytest.raises(ValueError, match="inside source_positions"):
        ReplyRecordAudit(
            **common,
            quarantined_source_positions=((8, 1),),
        )
