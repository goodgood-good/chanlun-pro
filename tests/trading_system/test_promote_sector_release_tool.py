from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import promote_sector_release as subject


OLD_SHA = "sha256:" + "1" * 64
NEW_SHA = "sha256:" + "2" * 64


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _setup(tmp_path: Path) -> SimpleNamespace:
    stable_artifact = tmp_path / "published/result.json"
    stable_manifest = tmp_path / "published/release_manifest.json"
    candidate_artifact = tmp_path / "candidate/result.json"
    candidate_manifest = tmp_path / "candidate/release_manifest.json"
    economic = {"schema": "research", "parameters": {"slots": 5}}
    _write_json(
        stable_artifact,
        {
            **economic,
            "decision_source_snapshot": {"aggregate_sha256": OLD_SHA},
            "content_sha256": OLD_SHA,
        },
    )
    _write_json(
        candidate_artifact,
        {
            **economic,
            "decision_source_snapshot": {"aggregate_sha256": NEW_SHA},
            "content_sha256": NEW_SHA,
        },
    )
    _write_json(stable_manifest, {"identity": "old"})
    upstream: dict[str, object] = {}
    for name in (
        "trigger_ledger",
        "terminal_query_plan",
        "direct_manifest",
        "pit_snapshot",
        "current_catalog_ledger",
    ):
        path = tmp_path / f"inputs/{name}.dat"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        upstream[name] = {
            "path": path.relative_to(tmp_path).as_posix(),
            "file_sha256": OLD_SHA,
        }
    _write_json(candidate_manifest, {"upstream": upstream})
    return SimpleNamespace(
        root=tmp_path,
        candidate_artifact=candidate_artifact.relative_to(tmp_path),
        candidate_manifest=candidate_manifest.relative_to(tmp_path),
        target_artifact=stable_artifact.relative_to(tmp_path),
        target_manifest=stable_manifest.relative_to(tmp_path),
        execute=False,
        confirm_content_sha256=None,
    )


def _stub_verification(monkeypatch: pytest.MonkeyPatch) -> None:
    def verify(*, manifest_path: Path, expected_artifact_path: Path, **_: object):
        document = json.loads(expected_artifact_path.read_text(encoding="utf-8"))
        content = document["content_sha256"]
        return {
            "artifact_content_sha256": content,
            "artifact_file_sha256": "sha256:file-" + content[-1],
            "manifest_file_sha256": "sha256:manifest-" + content[-1],
        }

    monkeypatch.setattr(subject, "verify_sector_release_manifest", verify)
    monkeypatch.setattr(
        subject,
        "replay_decision_source_snapshot_matches_current",
        lambda *_: True,
    )


def test_dry_run_requires_no_write_and_proves_economic_equivalence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _setup(tmp_path)
    _stub_verification(monkeypatch)
    old_artifact = (tmp_path / args.target_artifact).read_bytes()
    old_manifest = (tmp_path / args.target_manifest).read_bytes()

    receipt = subject.run(args)

    assert receipt["disposition"] == "READY_DRY_RUN"
    assert receipt["economic_projection_unchanged"] is True
    assert (tmp_path / args.target_artifact).read_bytes() == old_artifact
    assert (tmp_path / args.target_manifest).read_bytes() == old_manifest
    assert not (tmp_path / "published/release_artifact_history").exists()


def test_promotion_archives_and_atomically_replaces_both_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _setup(tmp_path)
    _stub_verification(monkeypatch)
    previous_artifact = (tmp_path / args.target_artifact).read_bytes()
    previous_manifest = (tmp_path / args.target_manifest).read_bytes()
    candidate = (tmp_path / args.candidate_artifact).read_bytes()
    monkeypatch.setattr(
        subject,
        "build_sector_release_manifest",
        lambda **_: {"schema": "release", "content_sha256": NEW_SHA},
    )
    args.execute = True
    args.confirm_content_sha256 = NEW_SHA

    receipt = subject.run(args)

    assert receipt["disposition"] == "PROMOTED_AND_VERIFIED"
    assert (tmp_path / args.target_artifact).read_bytes() == candidate
    assert json.loads((tmp_path / args.target_manifest).read_text())["schema"] == (
        "release"
    )
    assert (tmp_path / receipt["old_artifact_archive"]).read_bytes() == (
        previous_artifact
    )
    assert (tmp_path / receipt["old_manifest_archive"]).read_bytes() == (
        previous_manifest
    )


def test_failed_publication_restores_the_previous_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _setup(tmp_path)
    _stub_verification(monkeypatch)
    previous_artifact = (tmp_path / args.target_artifact).read_bytes()
    previous_manifest = (tmp_path / args.target_manifest).read_bytes()
    monkeypatch.setattr(
        subject,
        "build_sector_release_manifest",
        lambda **_: (_ for _ in ()).throw(RuntimeError("injected failure")),
    )
    args.execute = True
    args.confirm_content_sha256 = NEW_SHA

    with pytest.raises(RuntimeError, match="injected failure"):
        subject.run(args)

    assert (tmp_path / args.target_artifact).read_bytes() == previous_artifact
    assert (tmp_path / args.target_manifest).read_bytes() == previous_manifest


def test_changed_decisions_are_refused_even_with_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _setup(tmp_path)
    _stub_verification(monkeypatch)
    candidate = tmp_path / args.candidate_artifact
    value = json.loads(candidate.read_text(encoding="utf-8"))
    value["parameters"] = {"slots": 6}
    _write_json(candidate, value)

    with pytest.raises(subject.PromotionError, match="economic results"):
        subject.run(args)
