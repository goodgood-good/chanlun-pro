from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from chanlun.decision_support.corpus_integrity import scan_corpus
from chanlun.decision_support.corpus_sources import (
    parse_illustrated_archive,
    write_trusted_manifest,
)
from chanlun.decision_support.corpus_types import EvidenceUnit, SourceTier
from chanlun.decision_support.secondary_runtime import (
    TrustedSecondaryCorpusRuntime,
)


def _namespace_unit(
    unit: EvidenceUnit,
    *,
    archive_root: Path,
    source_root: Path,
) -> EvidenceUnit:
    prefix = archive_root.relative_to(source_root).as_posix()
    source_path = f"{prefix}/{unit.source_path}"
    identity = f"{unit.source_tier.value}\0{source_path}\0{unit.evidence_id}"
    evidence_id = "evidence:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return replace(unit, evidence_id=evidence_id, source_path=source_path)


def _secondary_runtime(
    tmp_path: Path,
    *,
    incomplete: bool = False,
) -> tuple[TrustedSecondaryCorpusRuntime, Path]:
    source_root = tmp_path / "output"
    archive_root = source_root / "secondary_archive"
    articles = archive_root / "articles"
    articles.mkdir(parents=True)
    article = articles / "interval-nest.md"
    article.write_text(
        """# 区间套辅助解读

- Author: 测试作者
- Source: https://example.test/interval-nest

区间套用于逐级缩小背驰定位范围，不能跳过中间级别。

三类买点还要核对离开中枢后的第一次回试是否跌回中枢。
""",
        encoding="utf-8",
    )
    if incomplete:
        (articles / "missing-body.md").write_bytes(b"")

    report = scan_corpus((archive_root,))
    units = tuple(
        _namespace_unit(
            unit,
            archive_root=archive_root,
            source_root=source_root,
        )
        for unit in parse_illustrated_archive(archive_root, report)
    )
    manifest = tmp_path / "trusted_manifest.json"
    write_trusted_manifest(
        manifest,
        units,
        (),
        integrity_status="incomplete" if incomplete else "complete",
    )
    return TrustedSecondaryCorpusRuntime(manifest, source_root), article


def test_secondary_runtime_returns_only_advisory_verified_hits(
    tmp_path: Path,
    make_decision_event,
) -> None:
    runtime, _ = _secondary_runtime(tmp_path)
    event = make_decision_event()

    status = runtime.status()
    related = runtime.related(event, limit=3)

    assert status["integrity"] == "complete"
    assert status["advisory_only"] is True
    assert status["eligible_for_rule_binding"] is False
    assert status["trusted_units"] == 2
    assert status["trusted_images"] == 0
    assert status["issue_counts"] == {}
    assert status["manifest_fingerprint"].startswith("sha256:")
    assert related.interpretations
    assert all(
        unit.source_tier.value == "secondary_annotation"
        for unit in related.interpretations
    )
    assert related.images == ()


def test_secondary_runtime_exposes_incomplete_coverage_without_promoting_it(
    tmp_path: Path,
    make_decision_event,
) -> None:
    runtime, _ = _secondary_runtime(tmp_path, incomplete=True)

    status = runtime.status()
    related = runtime.related(make_decision_event(), limit=3)

    assert status["integrity"] == "incomplete"
    assert status["coverage"] == "verified_subset_only"
    assert status["blockers"] == ["secondary_corpus_incomplete"]
    assert status["issue_counts"] == {"zero_byte": 1}
    assert status["eligible_for_rule_binding"] is False
    assert related.interpretations


def test_secondary_runtime_rejects_source_tampering(
    tmp_path: Path,
) -> None:
    runtime, article = _secondary_runtime(tmp_path)
    article.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="secondary corpus manifest mismatch"):
        runtime.status()


def test_secondary_runtime_accepts_schema_one_manifest_without_new_optional_fields(
    tmp_path: Path,
) -> None:
    runtime, _ = _secondary_runtime(tmp_path)
    manifest = runtime.manifest_path
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    for unit in payload["units"]:
        for name in (
            "bbox",
            "block_index",
            "page_number",
            "source_pdf_sha256",
            "source_record_id",
            "source_record_ids",
            "source_role",
            "source_sequence_index",
        ):
            unit.pop(name)
    manifest.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    assert TrustedSecondaryCorpusRuntime(manifest, runtime.source_root).status()[
        "trusted_units"
    ] == 2


def test_real_secondary_manifest_is_incomplete_advisory_evidence_only(
    make_decision_event,
) -> None:
    runtime = TrustedSecondaryCorpusRuntime(
        Path("audit/decision_corpus_build/trusted_manifest.json"),
        Path("output"),
    )

    status = runtime.status()
    related = runtime.related(
        make_decision_event(
            bs_type="1buy_nest",
            live_divergence=True,
        ),
        limit=3,
    )

    assert status["integrity"] == "incomplete"
    assert status["coverage"] == "verified_subset_only"
    assert status["advisory_only"] is True
    assert status["eligible_for_rule_binding"] is False
    assert status["trusted_units"] == 66
    assert status["trusted_images"] == 31
    assert status["issue_counts"]["zero_byte"] > 0
    assert status["issue_counts"]["archive_count_mismatch"] == 1
    assert related.interpretations
    assert all(
        unit.source_tier is SourceTier.SECONDARY_ANNOTATION
        for unit in related.interpretations
    )
