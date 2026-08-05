from __future__ import annotations

from pathlib import Path

import pytest

from chanlun.decision_support.corpus_loader import load_certified_lesson_corpus
from chanlun.decision_support.corpus_types import SourceTier
from chanlun.decision_support.exit_evidence_policy import (
    load_exit_evidence_policy_file,
)
from chanlun.decision_support.exits import ExitTrigger


_EXPECTED = {
    ExitTrigger.HARD_RISK: (
        "evidence:9aa2f3cd87aaab49c481e371482876b4d7f4710b7a811cdb0c20feacbc8c6111",
        "evidence:89abab495fa799bf95ce78d059cfd814ca90a99d1f0361296e2ba55c5d465ce6",
    ),
    ExitTrigger.STRUCTURAL_INVALIDATION: (
        "evidence:bd431175ca56751c5cac6548cda9fc8e8cf8757a4e9e51fc50060bad7a0150bb",
    ),
    ExitTrigger.CONTROL_LEVEL_DOWN: (
        "evidence:6e8ff461fc6a949316f4629c688a058778305cb6a6cb65dfc1d3f1ea40fe2761",
    ),
    ExitTrigger.CONTROL_LEVEL_SELL: (
        "evidence:30e2d634264e696059ba2e4b2f80e4cb672a32091fb8e2cb5849f1a7ef2107c1",
    ),
    ExitTrigger.OPERATION_LEVEL_SELL: (
        "evidence:66303428ca684bbb24ea1f8432501c8b79dd2cd6d2a3150bafcf787e7dc64a72",
    ),
}

_EXPECTED_COORDINATES = {
    _EXPECTED[ExitTrigger.HARD_RISK][0]: (12, 161, "lesson_body"),
    _EXPECTED[ExitTrigger.HARD_RISK][1]: (22, 524, "chan_reply"),
    _EXPECTED[ExitTrigger.STRUCTURAL_INVALIDATION][0]: (
        33,
        1074,
        "lesson_body",
    ),
    _EXPECTED[ExitTrigger.CONTROL_LEVEL_DOWN][0]: (35, 1126, "lesson_body"),
    _EXPECTED[ExitTrigger.CONTROL_LEVEL_SELL][0]: (45, 1333, "lesson_body"),
    _EXPECTED[ExitTrigger.OPERATION_LEVEL_SELL][0]: (
        14,
        184,
        "lesson_body",
    ),
}

_EXPECTED_PHRASES = {
    _EXPECTED[ExitTrigger.HARD_RISK][0]: "一旦出现相应的情况，采取相应的操作",
    _EXPECTED[ExitTrigger.HARD_RISK][1]: "“止损”还是需要的",
    _EXPECTED[ExitTrigger.STRUCTURAL_INVALIDATION][0]: (
        "第三类卖点出现，必须走"
    ),
    _EXPECTED[ExitTrigger.CONTROL_LEVEL_DOWN][0]: (
        "不参与任何调整或下跌走势类型"
    ),
    _EXPECTED[ExitTrigger.CONTROL_LEVEL_SELL][0]: (
        "当这个30分钟的卖点出现时，卖出"
    ),
    _EXPECTED[ExitTrigger.OPERATION_LEVEL_SELL][0]: (
        "在次级别第一类卖点出现时，可以先减仓"
    ),
}


_CERTIFIED_CORPUS_ROOT = Path("audit/chanlun_lesson_corpus_v3")


@pytest.mark.skipif(
    not _CERTIFIED_CORPUS_ROOT.is_dir(),
    reason="optional certified legacy corpus package is not versioned",
)
def test_real_exit_policy_resolves_only_certified_original_semantic_units() -> None:
    corpus_root = _CERTIFIED_CORPUS_ROOT
    policy_path = Path("config/decision_support/exit_evidence_policy.json")
    assert policy_path.is_file(), "production exit evidence policy is missing"

    corpus = load_certified_lesson_corpus(corpus_root)
    policy = load_exit_evidence_policy_file(policy_path, corpus=corpus)

    assert policy.corpus_manifest_sha256 == corpus.manifest_sha256
    assert policy.source_pdf_sha256 == corpus.source_pdf_sha256
    assert {binding.trigger for binding in policy.bindings} == set(ExitTrigger)
    assert policy.binding(ExitTrigger.HARD_RISK).boundary_tags == (
        "project_risk_latch",
    )
    units_by_id = {unit.evidence_id: unit for unit in corpus.semantic_units}
    for trigger, evidence_ids in _EXPECTED.items():
        assert policy(trigger) == evidence_ids
        for evidence_id in evidence_ids:
            unit = units_by_id[evidence_id]
            assert unit.source_tier is SourceTier.LESSON_ORIGINAL
            assert (
                unit.lesson,
                unit.page_number,
                unit.source_role,
            ) == _EXPECTED_COORDINATES[evidence_id]
            assert unit.source_pdf_sha256 == corpus.source_pdf_sha256
            assert _EXPECTED_PHRASES[evidence_id] in "".join(unit.text.split())
