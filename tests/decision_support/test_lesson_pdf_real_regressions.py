from __future__ import annotations

import json

from chanlun.decision_support.lesson_corpus import PageSpan, SourceRole
from chanlun.decision_support.lesson_pdf import classify_lesson_text


PAGE_SIZE = (595.3, 841.9)


def _span(page: int, sequence: int, raw_text: str) -> PageSpan:
    top = float(80 + (sequence % 30) * 20)
    return PageSpan(
        page_number=page,
        bbox=(80.0, top, 520.0, top + 18.0),
        page_size=PAGE_SIZE,
        text=raw_text,
        color_rgb=(0, 0, 0),
        source_sequence_index=sequence,
    )


def _roles(result: object) -> dict[tuple[int, int], SourceRole]:
    return {
        (block.page_number, block.source_sequence_index): block.source_role
        for block in result.blocks
    }


def test_real_p252_anonymous_header_reenters_reader_state() -> None:
    spans = (
        _span(251, 12, "********************"),
        _span(
            251,
            13,
            "关于038004的作业，回答比较正确的是下面这位。但还是有点出入。10月23到25日",
        ),
        _span(252, 1, "[匿名]在路上"),
        _span(252, 2, "2006-12-1223:48:54"),
        _span(
            252,
            3,
            "我先来回答禅姐的038004的问题,以检验最近向禅姐学习的成绩,无论对错,望禅姐和各",
        ),
        _span(252, 23, "(2006-12-1312:17:52)"),
    )

    result = classify_lesson_text(15, 251, 252, spans)
    roles = _roles(result)

    assert roles[(251, 13)] is SourceRole.UNKNOWN_TEXT
    assert roles[(252, 1)] is SourceRole.READER_COMMENT
    assert roles[(252, 2)] is SourceRole.READER_COMMENT
    assert roles[(252, 3)] is SourceRole.READER_COMMENT
    assert roles[(252, 23)] is SourceRole.READER_COMMENT
    assert result.fsm_audit.reader_reentry_count == 1
    assert result.fsm_audit.unresolved_candidate_count == 1
    audit = result.reply_records[0]
    assert (251, 13) in audit.quarantined_source_positions
    assert (252, 1) in audit.reader_source_positions
    assert audit.author_source_positions == ()


def test_real_p599_anonymous_header_reenters_reader_state() -> None:
    spans = (
        _span(599, 15, "********************"),
        _span(
            599,
            16,
            "本ID突然发现，这里也不是完全没有对背弛有点感觉的，你看这位，本ID可以给他",
        ),
        _span(599, 17, "戴一个大红花。"),
        _span(599, 18, "[匿名]过客"),
        _span(599, 19, "2007-01-1713:06:23"),
        _span(600, 2, "楼主你好:601628现在30分钟背离了.我下午开盘就出对吗？急盼中......"),
        _span(600, 10, "(2007-01-1721:27:25)"),
    )

    result = classify_lesson_text(23, 599, 600, spans)
    roles = _roles(result)

    assert roles[(599, 16)] is SourceRole.UNKNOWN_TEXT
    assert roles[(599, 17)] is SourceRole.UNKNOWN_TEXT
    assert roles[(599, 18)] is SourceRole.READER_COMMENT
    assert roles[(599, 19)] is SourceRole.READER_COMMENT
    assert roles[(600, 2)] is SourceRole.READER_COMMENT
    assert roles[(600, 10)] is SourceRole.READER_COMMENT
    assert result.fsm_audit.reader_reentry_count == 1
    assert result.fsm_audit.unresolved_candidate_count == 1


def test_real_p296_single_equals_opens_author_reply() -> None:
    spans = (
        _span(296, 8, "********************"),
        _span(296, 9, "[匿名]ruifeng0021"),
        _span(296, 10, "2006-12-1512:55:19"),
        _span(296, 11, "问题:某一级别中盘整低点是如何形成的"),
        _span(296, 12, "答:某一级别中盘整低点是由次一级别中盘整后的下跌形成的"),
        _span(296, 13, "对否?"),
        _span(296, 14, "="),
        _span(296, 15, "似是而非"),
        _span(296, 16, "(2006-12-1512:56:16)"),
    )

    result = classify_lesson_text(16, 296, 296, spans)
    roles = _roles(result)

    assert roles[(296, 13)] is SourceRole.READER_COMMENT
    assert roles[(296, 14)] is SourceRole.EDITOR_NOTE
    assert roles[(296, 15)] is SourceRole.CHAN_REPLY
    assert roles[(296, 16)] is SourceRole.CHAN_REPLY
    assert result.fsm_audit.single_equals_separator_count == 1
    assert "single_equals_separator" in result.reply_records[0].reason_codes


def test_real_p302_single_equals_keeps_complete_author_reply() -> None:
    spans = (
        _span(302, 16, "********************"),
        _span(302, 17, "[匿名]缠"),
        _span(302, 18, "2006-12-1515:47:42"),
        _span(302, 19, "似懂非懂，就是确定不了射的那个点呀，我急禅姐！！！"),
        _span(302, 20, "="),
        _span(
            302,
            21,
            "急什么？关键要学会。真会了，市场永远有机会。不少人，大牛市还亏损累累，有些人，",
        ),
        _span(302, 22, "熊市照样能牛。关键要耐心学会。多看图。"),
        _span(302, 27, "(2006-12-1516:01:42)"),
    )

    result = classify_lesson_text(16, 302, 302, spans)
    roles = _roles(result)

    assert roles[(302, 19)] is SourceRole.READER_COMMENT
    assert roles[(302, 20)] is SourceRole.EDITOR_NOTE
    assert roles[(302, 21)] is SourceRole.CHAN_REPLY
    assert roles[(302, 22)] is SourceRole.CHAN_REPLY
    assert roles[(302, 27)] is SourceRole.CHAN_REPLY
    assert result.fsm_audit.single_equals_separator_count == 1


def test_real_p299_page_suffixed_terminal_closes_author_reply() -> None:
    spans = (
        _span(298, 15, "********************"),
        _span(299, 7, "[匿名]nn"),
        _span(299, 8, "2006-12-1513:06:15"),
        _span(
            299,
            9,
            "李泽厚：孔子说：“生来就有知识是上等，学习而后有知识是次等，-----",
        ),
        _span(299, 16, "==="),
        _span(299, 17, "都过奖了，共同学习吧。"),
        _span(299, 18, "(2006-12-1515:31:23)266"),
    )

    result = classify_lesson_text(16, 298, 299, spans)
    roles = _roles(result)

    assert roles[(299, 9)] is SourceRole.READER_COMMENT
    assert roles[(299, 16)] is SourceRole.EDITOR_NOTE
    assert roles[(299, 17)] is SourceRole.CHAN_REPLY
    assert roles[(299, 18)] is SourceRole.CHAN_REPLY
    assert result.fsm_audit.page_suffixed_terminal_date_count == 1
    assert "page_suffixed_terminal_datetime" in result.reply_records[0].reason_codes


def test_real_p402_split_terminal_closes_author_reply() -> None:
    spans = (
        _span(402, 3, "********************"),
        _span(402, 4, "[匿名]摄影之友"),
        _span(402, 5, "2007-01-0222:08:00"),
        _span(402, 6, "有一个问题想请教博主与各位同学:"),
        _span(402, 16, "==="),
        _span(
            402,
            17,
            "那是除权的问题，如果你不习惯这样看，可以把他复权来看。但习惯了其实无所谓，例",
        ),
        _span(402, 23, "(2007-01-03"),
        _span(402, 24, "21:26:40)"),
    )

    result = classify_lesson_text(19, 402, 402, spans)
    roles = _roles(result)

    assert roles[(402, 6)] is SourceRole.READER_COMMENT
    assert roles[(402, 16)] is SourceRole.EDITOR_NOTE
    assert roles[(402, 17)] is SourceRole.CHAN_REPLY
    assert roles[(402, 23)] is SourceRole.CHAN_REPLY
    assert roles[(402, 24)] is SourceRole.CHAN_REPLY
    assert result.fsm_audit.split_terminal_date_count == 1
    assert "split_terminal_datetime" in result.reply_records[0].reason_codes


def test_unproven_standalone_author_fails_closed_and_audit_is_serializable() -> None:
    spans = (
        _span(700, 1, "********************"),
        _span(700, 2, "任意文本不能仅凭括号日期证明是作者。"),
        _span(700, 3, "(2007-01-01 10:00:00)"),
        _span(700, 4, "********************"),
        _span(700, 5, "缠中说禅"),
        _span(700, 6, "明确作者头可以建立候选。"),
        _span(700, 7, "(2007-01-01 10:01:00)"),
    )

    result = classify_lesson_text(25, 700, 700, spans)
    roles = _roles(result)

    assert roles[(700, 2)] is SourceRole.UNKNOWN_TEXT
    assert roles[(700, 3)] is SourceRole.UNKNOWN_TEXT
    assert roles[(700, 5)] is SourceRole.CHAN_REPLY
    assert roles[(700, 6)] is SourceRole.CHAN_REPLY
    assert roles[(700, 7)] is SourceRole.CHAN_REPLY
    assert result.fsm_audit.unresolved_candidate_count == 1

    payload = result.fsm_audit.to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
    assert payload["reason_counts"]["unresolved_author_candidate"] == 1


def test_pure_equals_token_precedes_generic_reader_header_lookahead() -> None:
    spans = (
        _span(701, 1, "********************"),
        _span(701, 2, "[匿名]测试"),
        _span(701, 3, "2007-01-0110:00:00"),
        _span(701, 4, "读者问题。"),
        _span(701, 5, "="),
        _span(701, 6, "2007-01-0110:00:01"),
        _span(701, 7, "(2007-01-01 10:00:02)"),
    )

    result = classify_lesson_text(25, 701, 701, spans)
    roles = _roles(result)

    assert roles[(701, 5)] is SourceRole.EDITOR_NOTE
    assert roles[(701, 6)] is SourceRole.CHAN_REPLY
    assert roles[(701, 7)] is SourceRole.CHAN_REPLY
    assert result.fsm_audit.single_equals_separator_count == 1


def test_closed_author_reply_survives_later_reader_reentry_in_same_record() -> None:
    spans = (
        _span(702, 1, "********************"),
        _span(702, 2, "[匿名]第一位读者"),
        _span(702, 3, "2007-01-0110:00:00"),
        _span(702, 4, "第一个问题。"),
        _span(702, 5, "="),
        _span(702, 6, "已经由终止日期闭合的作者回答。"),
        _span(702, 7, "(2007-01-01 10:01:00)"),
        _span(702, 8, "[匿名]第二位读者"),
        _span(702, 9, "2007-01-0110:02:00"),
        _span(702, 10, "后续读者问题。"),
        _span(702, 11, "(2007-01-01 10:03:00)"),
    )

    result = classify_lesson_text(25, 702, 702, spans)
    roles = _roles(result)

    assert roles[(702, 6)] is SourceRole.CHAN_REPLY
    assert roles[(702, 7)] is SourceRole.CHAN_REPLY
    assert roles[(702, 8)] is SourceRole.READER_COMMENT
    assert roles[(702, 10)] is SourceRole.READER_COMMENT
    assert roles[(702, 11)] is SourceRole.READER_COMMENT
    assert result.fsm_audit.unresolved_candidate_count == 0
    assert result.fsm_audit.to_dict()["reason_counts"][
        "intermediate_author_terminal"
    ] == 1
    audit = result.reply_records[0]
    assert (702, 6) in audit.author_source_positions
    assert (702, 7) in audit.author_source_positions
    assert (702, 8) in audit.reader_source_positions
    assert (702, 11) in audit.reader_source_positions
    assert audit.quarantined_source_positions == ()


def test_signed_plain_datetime_closes_author_before_reader_reentry() -> None:
    spans = (
        _span(703, 1, "********************"),
        _span(703, 2, "[匿名]提问者"),
        _span(703, 3, "2007-01-2219:00:00"),
        _span(703, 4, "读者问题。"),
        _span(703, 5, "=="),
        _span(703, 6, "作者回答。"),
        _span(703, 7, "缠中说禅"),
        _span(703, 8, ":"),
        _span(703, 9, "2007-01-2220:52:17"),
        _span(703, 10, "[匿名]下一位读者"),
        _span(703, 11, "2007-01-2220:53:00"),
        _span(703, 12, "后续读者问题。"),
        _span(703, 13, "(2007-01-22 20:54:00)"),
    )

    result = classify_lesson_text(25, 703, 703, spans)
    roles = _roles(result)

    assert roles[(703, 6)] is SourceRole.CHAN_REPLY
    assert roles[(703, 7)] is SourceRole.CHAN_REPLY
    assert roles[(703, 8)] is SourceRole.UNKNOWN_TEXT
    assert roles[(703, 9)] is SourceRole.CHAN_REPLY
    assert roles[(703, 10)] is SourceRole.READER_COMMENT
    assert roles[(703, 12)] is SourceRole.READER_COMMENT
    assert result.fsm_audit.signed_terminal_date_count == 1
    assert result.fsm_audit.unresolved_candidate_count == 0
    audit = result.reply_records[0]
    assert (703, 8) in audit.author_source_positions
    assert (703, 10) in audit.reader_source_positions
    provenance = (
        set(audit.reader_source_positions)
        | set(audit.author_source_positions)
        | set(audit.quarantined_source_positions)
    )
    assert (703, 5) not in provenance


def test_signature_and_parenthesized_datetime_layout_variants_close_author() -> None:
    spans = (
        _span(704, 1, "********************"),
        _span(704, 2, "[匿名]提问者甲"),
        _span(704, 3, "2007-03-0616:00:00"),
        _span(704, 4, "问题甲。"),
        _span(704, 5, "=="),
        _span(704, 6, "回答甲。"),
        _span(704, 7, "缠中说禅"),
        _span(704, 8, ":(2007-03-0616:29:12)"),
        _span(704, 9, "********************"),
        _span(704, 10, "[匿名]提问者乙"),
        _span(704, 11, "2007-04-0315:00:00"),
        _span(704, 12, "问题乙。"),
        _span(704, 13, "=="),
        _span(704, 14, "回答乙。"),
        _span(704, 15, "(2007-04-0316:03:11)"),
        _span(704, 16, "缠中说禅"),
        _span(704, 17, ":"),
    )

    result = classify_lesson_text(25, 704, 704, spans)
    roles = _roles(result)

    for sequence in (6, 7, 8, 14, 15, 16):
        assert roles[(704, sequence)] is SourceRole.CHAN_REPLY
    assert roles[(704, 17)] is SourceRole.UNKNOWN_TEXT
    assert result.fsm_audit.signed_terminal_date_count == 2
    assert result.fsm_audit.unresolved_candidate_count == 0
