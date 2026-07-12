from __future__ import annotations

from chanlun.decision_support.corpus_chunking import build_semantic_units
from chanlun.decision_support.corpus_types import EvidenceUnit, SourceTier


def _unit(
    sequence: int,
    text: str,
    *,
    page: int = 100,
    role: str = "lesson_body",
    image_ids: tuple[str, ...] = (),
) -> EvidenceUnit:
    identity = page * 1000 + sequence
    record_id = f"source:{identity:064x}"
    return EvidenceUnit(
        evidence_id=f"evidence:{identity:064x}",
        source_tier=SourceTier.LESSON_ORIGINAL,
        source_path="L020_lesson.md",
        title="教你炒股票 20",
        text=text,
        sha256=f"{identity:064x}",
        lesson=20,
        image_ids=image_ids,
        source_role=role,
        source_record_id=record_id,
        source_record_ids=(record_id,),
        source_pdf_sha256="a" * 64,
        page_number=page,
        bbox=(100.0, float(sequence * 20), 500.0, float(sequence * 20 + 15)),
        source_sequence_index=sequence,
        block_index=sequence + 1000,
    )


def test_semantic_chunks_preserve_physical_provenance_and_hard_boundaries() -> None:
    first = _unit(1, "第三类买点先离开中枢。")
    second = _unit(2, "次级别回试低点不跌破 ZG。", image_ids=("image:occurrence-1",))
    after_quarantine_gap = _unit(4, "序号三是被隔离的编者按，不能跨过去合并。")
    reply = _unit(5, "本 ID 的回答。", role="chan_reply")
    next_page = _unit(1, "下一页正文。", page=101)

    chunks = build_semantic_units(
        (reply, second, next_page, first, after_quarantine_gap),
        max_chars=200,
    )

    assert tuple(chunk.text for chunk in chunks) == (
        "第三类买点先离开中枢。\n次级别回试低点不跌破 ZG。",
        "序号三是被隔离的编者按，不能跨过去合并。",
        "本 ID 的回答。",
        "下一页正文。",
    )
    merged = chunks[0]
    assert merged.source_record_ids == (
        first.source_record_id,
        second.source_record_id,
    )
    assert merged.source_sequence_index == 1
    assert merged.block_index == 1001
    assert merged.bbox == (100.0, 20.0, 500.0, 55.0)
    assert merged.image_ids == ("image:occurrence-1",)
    assert merged.evidence_id.startswith("evidence:")
    assert len(merged.evidence_id) == len("evidence:") + 64


def test_semantic_chunks_are_deterministic_and_bound_size() -> None:
    units = (_unit(1, "甲" * 8), _unit(2, "乙" * 8), _unit(3, "丙" * 8))

    first = build_semantic_units(units, max_chars=17)
    second = build_semantic_units(tuple(reversed(units)), max_chars=17)

    assert first == second
    assert tuple(chunk.text for chunk in first) == ("甲" * 8 + "\n" + "乙" * 8, "丙" * 8)
    assert all(len(chunk.text) <= 17 for chunk in first)


def test_semantic_chunks_reject_untraceable_or_duplicate_original_records() -> None:
    missing = _unit(1, "缺少底层记录。")
    missing = EvidenceUnit(
        **{
            **missing.__dict__,
            "source_record_id": None,
            "source_record_ids": (),
        }
    )

    try:
        build_semantic_units((missing,))
    except ValueError as exc:
        assert "traceable source record" in str(exc)
    else:
        raise AssertionError("untraceable original evidence must be rejected")

    duplicated = (_unit(1, "第一条。"), _unit(1, "伪造重复条。"))
    try:
        build_semantic_units(duplicated)
    except ValueError as exc:
        assert "duplicated" in str(exc)
    else:
        raise AssertionError("duplicated source records must be rejected")
