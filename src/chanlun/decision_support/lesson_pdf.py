from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import re
from typing import Iterable
import unicodedata

from .lesson_corpus import (
    LessonBoundary,
    LessonTextBlock,
    PageSpan,
    SourceRole,
    is_running_header_or_footer,
)


_LESSON_HEADING_RE = re.compile(
    r"^教你炒股票\s*(?P<number>(?:\d\s*){1,3}):(?P<subject>.+)$"
)
_STAR_SEPARATOR_RE = re.compile(r"^\*{8,}\d*$")
_EQUALS_SEPARATOR_RE = re.compile(r"^=+$")
_TERMINAL_DATE_RE = re.compile(
    r"^\(\s*\d{4}-\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2}:\d{2}\s*\)$"
)
_PAGE_SUFFIXED_TERMINAL_DATE_RE = re.compile(
    r"^\(\s*\d{4}-\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2}:\d{2}\s*\)\d{1,4}$"
)
_REPLY_HEADER_DATE_RE = re.compile(
    r"^\d{4}-\d{1,2}-\d{1,2}\s*\d{1,2}:\d{2}:\d{2}$"
)


@dataclass(frozen=True)
class ReplyRecordAudit:
    record_index: int
    source_positions: tuple[tuple[int, int], ...]
    resolution: str
    reason_codes: tuple[str, ...]
    reader_source_positions: tuple[tuple[int, int], ...] = ()
    author_source_positions: tuple[tuple[int, int], ...] = ()
    quarantined_source_positions: tuple[tuple[int, int], ...] = ()
    separator_source_positions: tuple[tuple[int, int], ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.record_index, bool)
            or not isinstance(self.record_index, int)
            or self.record_index < 0
        ):
            raise ValueError("record_index must be a non-negative integer")

        def validated_positions(
            value: tuple[tuple[int, int], ...], field_name: str
        ) -> tuple[tuple[int, int], ...]:
            if not isinstance(value, tuple):
                raise TypeError(f"{field_name} must be a tuple")
            positions = tuple(tuple(position) for position in value)
            if any(
                len(position) != 2
                or isinstance(position[0], bool)
                or not isinstance(position[0], int)
                or position[0] <= 0
                or isinstance(position[1], bool)
                or not isinstance(position[1], int)
                or position[1] < 0
                for position in positions
            ):
                raise ValueError(f"{field_name} contains an invalid source position")
            if len(set(positions)) != len(positions):
                raise ValueError(f"{field_name} contains duplicate source positions")
            return positions

        source_positions = validated_positions(
            self.source_positions, "source_positions"
        )
        if not source_positions:
            raise ValueError("source_positions must not be empty")
        groups = (
            validated_positions(
                self.reader_source_positions, "reader_source_positions"
            ),
            validated_positions(
                self.author_source_positions, "author_source_positions"
            ),
            validated_positions(
                self.quarantined_source_positions,
                "quarantined_source_positions",
            ),
            validated_positions(
                self.separator_source_positions,
                "separator_source_positions",
            ),
        )
        source_set = set(source_positions)
        if any(not set(group).issubset(source_set) for group in groups):
            raise ValueError("reply provenance must be inside source_positions")
        if sum(len(group) for group in groups) != len(set().union(*groups)):
            raise ValueError("reply provenance groups must be disjoint")
        if not isinstance(self.resolution, str) or not self.resolution.strip():
            raise ValueError("resolution must be a non-empty string")
        if not isinstance(self.reason_codes, tuple) or any(
            not isinstance(reason, str) or not reason.strip()
            for reason in self.reason_codes
        ):
            raise ValueError("reason_codes must contain non-empty strings")
        object.__setattr__(self, "source_positions", source_positions)
        object.__setattr__(self, "reader_source_positions", groups[0])
        object.__setattr__(self, "author_source_positions", groups[1])
        object.__setattr__(self, "quarantined_source_positions", groups[2])
        object.__setattr__(self, "separator_source_positions", groups[3])
        object.__setattr__(self, "resolution", self.resolution.strip())
        object.__setattr__(
            self,
            "reason_codes",
            tuple(reason.strip() for reason in self.reason_codes),
        )


@dataclass(frozen=True)
class ReplyFsmAudit:
    single_equals_separator_count: int = 0
    split_terminal_date_count: int = 0
    page_suffixed_terminal_date_count: int = 0
    signed_terminal_date_count: int = 0
    reader_reentry_count: int = 0
    unresolved_candidate_count: int = 0
    reason_counts: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "page_suffixed_terminal_date_count": self.page_suffixed_terminal_date_count,
            "reader_reentry_count": self.reader_reentry_count,
            "reason_counts": dict(self.reason_counts),
            "signed_terminal_date_count": self.signed_terminal_date_count,
            "single_equals_separator_count": self.single_equals_separator_count,
            "split_terminal_date_count": self.split_terminal_date_count,
            "unresolved_candidate_count": self.unresolved_candidate_count,
        }


@dataclass(frozen=True)
class LessonTextExtraction:
    blocks: tuple[LessonTextBlock, ...]
    reply_records: tuple[ReplyRecordAudit, ...]
    reply_record_count: int
    closed_reply_record_count: int
    ambiguous_reply_record_count: int
    skipped_running_matter_count: int
    fsm_audit: ReplyFsmAudit = field(default_factory=ReplyFsmAudit)


def parse_lesson_heading(text: str) -> tuple[int, str] | None:
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    normalized = unicodedata.normalize("NFKC", text).strip()
    matched = _LESSON_HEADING_RE.fullmatch(normalized)
    if matched is None:
        return None
    number = int(re.sub(r"\s+", "", matched.group("number")))
    if not 0 <= number <= 108:
        return None
    return number, normalized


def _validated_expected_numbers(values: Iterable[int]) -> tuple[int, ...]:
    numbers = tuple(values)
    if not numbers:
        raise ValueError("expected_lesson_numbers must not be empty")
    if any(
        isinstance(number, bool)
        or not isinstance(number, int)
        or not 0 <= number <= 108
        for number in numbers
    ):
        raise ValueError("expected lesson numbers must be integers between 0 and 108")
    if numbers != tuple(range(numbers[0], numbers[-1] + 1)):
        raise ValueError("expected lesson numbers must be continuous and ordered")
    return numbers


def detect_lesson_boundaries(
    spans: tuple[PageSpan, ...] | list[PageSpan],
    *,
    expected_lesson_numbers: Iterable[int] = range(109),
    expected_first_page: int = 7,
    expected_last_page: int = 2533,
) -> tuple[LessonBoundary, ...]:
    values = tuple(spans)
    if any(not isinstance(span, PageSpan) for span in values):
        raise TypeError("spans must contain PageSpan values")
    numbers = _validated_expected_numbers(expected_lesson_numbers)
    if (
        isinstance(expected_first_page, bool)
        or not isinstance(expected_first_page, int)
        or isinstance(expected_last_page, bool)
        or not isinstance(expected_last_page, int)
        or expected_first_page <= 0
        or expected_first_page > expected_last_page
    ):
        raise ValueError("expected page range must be positive and ordered")

    by_page: dict[int, list[tuple[int, PageSpan]]] = defaultdict(list)
    for input_index, span in enumerate(values):
        if expected_first_page <= span.page_number <= expected_last_page:
            by_page[span.page_number].append((input_index, span))

    starts: list[tuple[int, int, str]] = []
    next_index = 0
    for page_number in range(expected_first_page, expected_last_page + 1):
        candidates = tuple(
            span
            for _, span in sorted(
                by_page.get(page_number, ()),
                key=lambda item: (item[1].source_sequence_index, item[0]),
            )
            if not is_running_header_or_footer(span)
        )
        if not candidates:
            continue
        parsed = parse_lesson_heading(candidates[0].normalized_text)
        if parsed is None:
            continue
        lesson_number, title = parsed
        if lesson_number != numbers[next_index]:
            continue
        starts.append((lesson_number, page_number, title))
        next_index += 1
        if next_index == len(numbers):
            break

    if tuple(item[0] for item in starts) != numbers:
        missing = numbers[len(starts) :]
        raise ValueError(f"missing verified lesson start pages: {missing}")
    if starts[0][1] != expected_first_page:
        raise ValueError("first verified lesson must begin on expected_first_page")

    return tuple(
        LessonBoundary(
            lesson_number=number,
            title=title,
            page_start=page_start,
            page_end=(starts[index + 1][1] - 1 if index + 1 < len(starts) else expected_last_page),
        )
        for index, (number, page_start, title) in enumerate(starts)
    )


def _is_star_separator(span: PageSpan) -> bool:
    return _STAR_SEPARATOR_RE.fullmatch(span.normalized_text) is not None


def _is_equals_separator(span: PageSpan) -> bool:
    return _EQUALS_SEPARATOR_RE.fullmatch(span.normalized_text) is not None


def _is_terminal_date(span: PageSpan) -> bool:
    return (
        _TERMINAL_DATE_RE.fullmatch(span.normalized_text) is not None
        or _PAGE_SUFFIXED_TERMINAL_DATE_RE.fullmatch(span.normalized_text) is not None
    )


def _is_editor_flowchart(span: PageSpan) -> bool:
    return "流程图" in span.normalized_text


def _is_orphan_punctuation(span: PageSpan) -> bool:
    return not any(character.isalnum() for character in span.normalized_text)


def _color_kind(span: PageSpan) -> str:
    color = span.color_rgb
    if color is None:
        return "unknown"
    red, green, blue = color
    if red >= 180 and red >= green + 80 and red >= blue + 60:
        return "editor"
    if green >= 120 and green >= red + 70 and green >= blue + 70:
        return "editor"
    if max(color) <= 48 and max(color) - min(color) <= 12:
        return "black"
    return "other"


def _role_with_medium_guards(span: PageSpan, structural_role: SourceRole) -> SourceRole:
    if _is_star_separator(span) or _is_equals_separator(span):
        return SourceRole.EDITOR_NOTE
    if _is_editor_flowchart(span) or _color_kind(span) == "editor":
        return SourceRole.EDITOR_NOTE
    if _color_kind(span) == "unknown" or _is_orphan_punctuation(span):
        return SourceRole.UNKNOWN_TEXT
    return structural_role


def _is_explicit_author_header(span: PageSpan) -> bool:
    return span.normalized_text in {"缠中说禅", "缠中说禅:", "缠中说禅："}


def _is_reader_header_at(
    record: tuple[tuple[int, PageSpan], ...],
    index: int,
) -> bool:
    span = record[index][1]
    first = span.normalized_text
    if (
        _is_equals_separator(span)
        or _is_explicit_author_header(span)
        or _is_orphan_punctuation(span)
    ):
        return False
    if first.startswith("[匿名]"):
        return True
    if index + 1 >= len(record):
        return False
    second = record[index + 1][1].normalized_text
    if _REPLY_HEADER_DATE_RE.fullmatch(second) is None:
        return False
    if len(first) > 32 or re.search(r"[。！？!?；;，,、（）()《》“”\d]", first):
        return False
    return True


def _looks_like_reader_header(record: tuple[tuple[int, PageSpan], ...]) -> bool:
    return bool(record) and _is_reader_header_at(record, 0)


def _terminal_date_shape(
    record: tuple[tuple[int, PageSpan], ...],
) -> tuple[int, str] | None:
    if not record:
        return None
    last_text = record[-1][1].normalized_text
    if _TERMINAL_DATE_RE.fullmatch(last_text) is not None:
        return len(record) - 1, "standard"
    if _PAGE_SUFFIXED_TERMINAL_DATE_RE.fullmatch(last_text) is not None:
        return len(record) - 1, "page_suffixed"
    stripped_colon = last_text.lstrip(":：").strip()
    if (
        stripped_colon != last_text
        and len(record) >= 2
        and _is_explicit_author_header(record[-2][1])
        and (
            _TERMINAL_DATE_RE.fullmatch(stripped_colon) is not None
            or _PAGE_SUFFIXED_TERMINAL_DATE_RE.fullmatch(stripped_colon) is not None
        )
    ):
        return len(record) - 1, "signed_parenthesized"
    if _REPLY_HEADER_DATE_RE.fullmatch(last_text) is not None:
        prior = tuple(item[1].normalized_text for item in record[-3:-1])
        signed = (
            bool(prior)
            and _is_explicit_author_header(record[-2][1])
        ) or (
            len(prior) == 2
            and prior[-1] in {":", "："}
            and _is_explicit_author_header(record[-3][1])
        )
        if signed:
            return len(record) - 1, "signed_plain"
    if _is_explicit_author_header(record[-1][1]) and len(record) >= 2:
        if _is_terminal_date(record[-2][1]):
            return len(record) - 2, "signed_after_parenthesized"
    if last_text in {":", "："} and len(record) >= 3:
        if _is_explicit_author_header(record[-2][1]) and _is_terminal_date(
            record[-3][1]
        ):
            return len(record) - 3, "signed_after_parenthesized"
    if len(record) < 2:
        return None
    combined = record[-2][1].normalized_text + last_text
    if _TERMINAL_DATE_RE.fullmatch(combined) is not None:
        return len(record) - 2, "split"
    if _PAGE_SUFFIXED_TERMINAL_DATE_RE.fullmatch(combined) is not None:
        return len(record) - 2, "split_page_suffixed"
    return None


@dataclass(frozen=True)
class _ReplyRecordClassification:
    roles: tuple[SourceRole, ...]
    resolution: str
    reason_codes: tuple[str, ...]
    closed: bool
    ambiguous: bool
    reason_counts: tuple[tuple[str, int], ...]
    reader_indexes: tuple[int, ...]
    author_indexes: tuple[int, ...]
    quarantined_indexes: tuple[int, ...]
    separator_indexes: tuple[int, ...]


def _classify_reply_record(
    record: tuple[tuple[int, PageSpan], ...],
) -> _ReplyRecordClassification:
    roles: list[SourceRole | None] = [None] * len(record)
    reason_counts: Counter[str] = Counter()
    terminal_shape = _terminal_date_shape(record)
    terminal_start = terminal_shape[0] if terminal_shape is not None else len(record)
    terminal_kind = terminal_shape[1] if terminal_shape is not None else None
    state = "start"
    candidate: list[int] = []
    reader_seen = False
    equals_seen = False
    reader_indexes: set[int] = set()
    author_indexes: set[int] = set()
    quarantined_indexes: set[int] = set()
    separator_indexes: set[int] = set()

    def assign(indexes: Iterable[int], structural_role: SourceRole) -> None:
        for item_index in indexes:
            reader_indexes.discard(item_index)
            author_indexes.discard(item_index)
            quarantined_indexes.discard(item_index)
            separator_indexes.discard(item_index)
            if structural_role is SourceRole.READER_COMMENT:
                reader_indexes.add(item_index)
            elif structural_role is SourceRole.CHAN_REPLY:
                author_indexes.add(item_index)
            elif structural_role is SourceRole.UNKNOWN_TEXT:
                quarantined_indexes.add(item_index)
            roles[item_index] = _role_with_medium_guards(
                record[item_index][1],
                structural_role,
            )

    def quarantine_candidate() -> None:
        if not candidate:
            return
        assign(tuple(candidate), SourceRole.UNKNOWN_TEXT)
        candidate.clear()
        reason_counts["unresolved_author_candidate"] += 1

    def close_author_candidate_if_terminal() -> bool:
        shape = _terminal_date_shape(tuple(record[index] for index in candidate))
        if shape is None:
            return False
        assign(tuple(candidate), SourceRole.CHAN_REPLY)
        candidate.clear()
        terminal_kind = shape[1]
        reason_counts["intermediate_author_terminal"] += 1
        if terminal_kind in {"split", "split_page_suffixed"}:
            reason_counts["split_terminal_datetime"] += 1
        if terminal_kind in {"page_suffixed", "split_page_suffixed"}:
            reason_counts["page_suffixed_terminal_datetime"] += 1
        if terminal_kind.startswith("signed_"):
            reason_counts["signed_terminal_datetime"] += 1
        return True

    for index in range(terminal_start):
        span = record[index][1]
        if _is_reader_header_at(record, index):
            if state in {"author", "standalone"} and candidate:
                if state != "author" or not close_author_candidate_if_terminal():
                    quarantine_candidate()
                reason_counts["reader_reentry"] += 1
            elif state == "reader" and reader_seen:
                reason_counts["reader_reentry"] += 1
            assign((index,), SourceRole.READER_COMMENT)
            state = "reader"
            reader_seen = True
            continue

        if _is_equals_separator(span):
            if len(span.normalized_text) == 1:
                reason_counts["single_equals_separator"] += 1
            if state == "standalone":
                assign(tuple(candidate), SourceRole.READER_COMMENT)
                candidate.clear()
                reader_seen = True
            elif state == "author":
                assign(tuple(candidate), SourceRole.READER_COMMENT)
                candidate.clear()
                reader_seen = True
            reader_indexes.discard(index)
            author_indexes.discard(index)
            quarantined_indexes.discard(index)
            separator_indexes.add(index)
            roles[index] = SourceRole.EDITOR_NOTE
            state = "author"
            equals_seen = True
            continue

        if _is_explicit_author_header(span):
            if state == "standalone" and candidate:
                quarantine_candidate()
            elif state == "author":
                candidate.append(index)
                continue
            candidate = [index]
            state = "author"
            continue

        if state == "reader":
            assign((index,), SourceRole.READER_COMMENT)
        elif state == "author":
            candidate.append(index)
        else:
            if state == "start":
                state = "standalone"
            candidate.append(index)

    record_reason_codes: list[str] = []
    closed = False
    ambiguous = False
    if terminal_shape is None:
        quarantine_candidate()
        for index, role in enumerate(roles):
            if role is None:
                assign((index,), SourceRole.UNKNOWN_TEXT)
        resolution = "ambiguous"
        record_reason_codes.append("missing_terminal_parenthesized_datetime")
        ambiguous = True
    elif state == "author":
        assign((*candidate, *range(terminal_start, len(record))), SourceRole.CHAN_REPLY)
        candidate.clear()
        resolution = "reader_then_author" if reader_seen or equals_seen else "standalone_author"
        record_reason_codes.append(
            "last_pure_equals_split"
            if equals_seen
            else "explicit_author_header_terminal_datetime"
        )
        closed = True
    elif state == "reader":
        assign(range(terminal_start, len(record)), SourceRole.READER_COMMENT)
        resolution = "reader_only"
        record_reason_codes.append("reader_header_without_equals")
        closed = True
    else:
        quarantine_candidate()
        assign(range(terminal_start, len(record)), SourceRole.UNKNOWN_TEXT)
        resolution = "ambiguous"
        record_reason_codes.append("unproven_standalone_author")
        ambiguous = True

    if reason_counts["single_equals_separator"]:
        record_reason_codes.append("single_equals_separator")
    if reason_counts["intermediate_author_terminal"]:
        record_reason_codes.append("intermediate_author_terminal")
    if terminal_kind in {"split", "split_page_suffixed"}:
        reason_counts["split_terminal_datetime"] += 1
        record_reason_codes.append("split_terminal_datetime")
    if terminal_kind in {"page_suffixed", "split_page_suffixed"}:
        reason_counts["page_suffixed_terminal_datetime"] += 1
        record_reason_codes.append("page_suffixed_terminal_datetime")
    if terminal_kind is not None and terminal_kind.startswith("signed_"):
        reason_counts["signed_terminal_datetime"] += 1
        record_reason_codes.append("signed_terminal_datetime")
    finalized_roles = tuple(
        role
        if role is not None
        else _role_with_medium_guards(record[index][1], SourceRole.UNKNOWN_TEXT)
        for index, role in enumerate(roles)
    )
    return _ReplyRecordClassification(
        roles=finalized_roles,
        resolution=resolution,
        reason_codes=tuple(record_reason_codes),
        closed=closed,
        ambiguous=ambiguous,
        reason_counts=tuple(sorted(reason_counts.items())),
        reader_indexes=tuple(sorted(reader_indexes)),
        author_indexes=tuple(sorted(author_indexes)),
        quarantined_indexes=tuple(sorted(quarantined_indexes)),
        separator_indexes=tuple(sorted(separator_indexes)),
    )


def _make_block(
    lesson_number: int,
    source_sequence_index: int,
    span: PageSpan,
    role: SourceRole,
) -> LessonTextBlock:
    return LessonTextBlock(
        lesson_number=lesson_number,
        page_number=span.page_number,
        bbox=span.bbox,
        page_size=span.page_size,
        page_rotation=span.page_rotation,
        source_sequence_index=source_sequence_index,
        color_rgb=span.color_rgb,
        source_role=role,
        text=span.raw_text,
        cropbox_pdf=span.cropbox_pdf,
        mediabox_pdf=span.mediabox_pdf,
    )


def classify_lesson_text(
    lesson_number: int,
    page_start: int,
    page_end: int,
    spans: tuple[PageSpan, ...] | list[PageSpan],
) -> LessonTextExtraction:
    if (
        isinstance(lesson_number, bool)
        or not isinstance(lesson_number, int)
        or not 0 <= lesson_number <= 108
    ):
        raise ValueError("lesson_number must be between 0 and 108")
    if (
        isinstance(page_start, bool)
        or not isinstance(page_start, int)
        or isinstance(page_end, bool)
        or not isinstance(page_end, int)
        or page_start <= 0
        or page_start > page_end
    ):
        raise ValueError("lesson pages must be positive and ordered")
    values = tuple(spans)
    if any(not isinstance(span, PageSpan) for span in values):
        raise TypeError("spans must contain PageSpan values")
    if any(not page_start <= span.page_number <= page_end for span in values):
        raise ValueError("all spans must be inside the lesson page range")
    source_positions = tuple(
        (span.page_number, span.source_sequence_index) for span in values
    )
    if len(set(source_positions)) != len(source_positions):
        raise ValueError("source_sequence_index must be unique within each page")

    ordered = tuple(
        sorted(
            enumerate(values),
            key=lambda item: (
                item[1].page_number,
                item[1].source_sequence_index,
                item[0],
            ),
        )
    )
    running_indexes = {
        input_index
        for input_index, span in ordered
        if is_running_header_or_footer(span)
    }
    content = tuple(
        (span.source_sequence_index, span)
        for input_index, span in ordered
        if input_index not in running_indexes
    )

    blocks: list[LessonTextBlock] = []
    first_star = next(
        (index for index, (_, span) in enumerate(content) if _is_star_separator(span)),
        len(content),
    )
    for source_sequence_index, span in content[:first_star]:
        if _is_equals_separator(span):
            role = SourceRole.EDITOR_NOTE
        elif _is_editor_flowchart(span) or _color_kind(span) == "editor":
            role = SourceRole.EDITOR_NOTE
        elif _is_orphan_punctuation(span):
            role = SourceRole.UNKNOWN_TEXT
        elif _color_kind(span) == "black":
            role = SourceRole.LESSON_BODY
        else:
            role = SourceRole.UNKNOWN_TEXT
        blocks.append(_make_block(lesson_number, source_sequence_index, span, role))

    records: list[tuple[tuple[int, PageSpan], ...]] = []
    record_audits: list[ReplyRecordAudit] = []
    current: list[tuple[int, PageSpan]] | None = None
    for source_sequence_index, span in content[first_star:]:
        if _is_star_separator(span):
            if current:
                records.append(tuple(current))
            current = []
            blocks.append(
                _make_block(
                    lesson_number,
                    source_sequence_index,
                    span,
                    SourceRole.EDITOR_NOTE,
                )
            )
            continue
        if current is None:
            current = []
        current.append((source_sequence_index, span))
    if current:
        records.append(tuple(current))

    closed_count = 0
    ambiguous_count = 0
    fsm_reason_counts: Counter[str] = Counter()
    for record_index, record in enumerate(records):
        source_positions = tuple(
            (span.page_number, source_sequence_index)
            for source_sequence_index, span in record
        )
        classified = _classify_reply_record(record)
        if classified.closed:
            closed_count += 1
        if classified.ambiguous:
            ambiguous_count += 1
        fsm_reason_counts.update(dict(classified.reason_counts))
        for (source_sequence_index, span), role in zip(record, classified.roles):
            blocks.append(_make_block(lesson_number, source_sequence_index, span, role))
        record_audits.append(
            ReplyRecordAudit(
                record_index=record_index,
                source_positions=source_positions,
                resolution=classified.resolution,
                reason_codes=classified.reason_codes,
                reader_source_positions=tuple(
                    (
                        record[index][1].page_number,
                        record[index][0],
                    )
                    for index in classified.reader_indexes
                ),
                author_source_positions=tuple(
                    (
                        record[index][1].page_number,
                        record[index][0],
                    )
                    for index in classified.author_indexes
                ),
                quarantined_source_positions=tuple(
                    (
                        record[index][1].page_number,
                        record[index][0],
                    )
                    for index in classified.quarantined_indexes
                ),
                separator_source_positions=tuple(
                    (
                        record[index][1].page_number,
                        record[index][0],
                    )
                    for index in classified.separator_indexes
                ),
            )
        )

    blocks.sort(key=lambda block: (block.page_number, block.source_sequence_index))
    return LessonTextExtraction(
        blocks=tuple(blocks),
        reply_records=tuple(record_audits),
        reply_record_count=len(records),
        closed_reply_record_count=closed_count,
        ambiguous_reply_record_count=ambiguous_count,
        skipped_running_matter_count=len(running_indexes),
        fsm_audit=ReplyFsmAudit(
            single_equals_separator_count=fsm_reason_counts[
                "single_equals_separator"
            ],
            split_terminal_date_count=fsm_reason_counts[
                "split_terminal_datetime"
            ],
            page_suffixed_terminal_date_count=fsm_reason_counts[
                "page_suffixed_terminal_datetime"
            ],
            signed_terminal_date_count=fsm_reason_counts[
                "signed_terminal_datetime"
            ],
            reader_reentry_count=fsm_reason_counts["reader_reentry"],
            unresolved_candidate_count=fsm_reason_counts[
                "unresolved_author_candidate"
            ],
            reason_counts=tuple(
                (reason, fsm_reason_counts[reason])
                for reason in (
                    "single_equals_separator",
                    "split_terminal_datetime",
                    "page_suffixed_terminal_datetime",
                    "signed_terminal_datetime",
                    "reader_reentry",
                    "intermediate_author_terminal",
                    "unresolved_author_candidate",
                )
            ),
        ),
    )
