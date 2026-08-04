from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import build_v3_sector_release_manifest as subject


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64


def _manifest(*, catalog_path: str, catalog_sha: str = SHA_A) -> dict[str, object]:
    return {
        "schema": "chanlun-v3-sector-release-manifest/v1",
        "artifact": {
            "path": "published/result.json",
            "schema": "artifact/v1",
            "file_sha256": SHA_B,
            "content_sha256": SHA_A,
        },
        "upstream": {
            "current_catalog_ledger": {
                "path": catalog_path,
                "file_sha256": catalog_sha,
            }
        },
        "current_catalog_entry_sha256": SHA_A,
        "algorithm": {"scope": "scope", "revision": SHA_B, "hash_count": 1},
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "immutable_inputs_only": True,
        "content_sha256": SHA_A,
    }


def _args(root: Path, *, replace: bool) -> SimpleNamespace:
    return SimpleNamespace(
        root=root,
        artifact=Path("published/result.json"),
        trigger_ledger=Path("inputs/trigger.pkl"),
        terminal_query_plan=Path("inputs/query.json"),
        direct_manifest=Path("inputs/direct.json"),
        pit_snapshot=Path("inputs/pit.json"),
        catalog_ledger=Path("mutable/catalog.json"),
        catalog_archive_dir=Path("published/release_inputs"),
        bundle_inputs=False,
        output=Path("published/v3_release_manifest.json"),
        replace_existing=replace,
    )


def test_catalog_copy_is_content_addressed_and_survives_source_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "mutable/catalog.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"first immutable ledger")

    first = subject._freeze_catalog_copy(
        root=tmp_path,
        source=source,
        directory=tmp_path / "published/release_inputs",
    )
    source.write_bytes(b"later forward ledger")
    second = subject._freeze_catalog_copy(
        root=tmp_path,
        source=source,
        directory=tmp_path / "published/release_inputs",
    )

    assert first != second
    assert first.read_bytes() == b"first immutable ledger"
    assert second.read_bytes() == b"later forward ledger"
    assert subject.sha256_file(first).removeprefix("sha256:") in first.name
    assert subject.sha256_file(second).removeprefix("sha256:") in second.name


def test_full_release_bundle_preserves_direct_checkpoint_relative_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "candidate"
    source.mkdir()
    trigger = source / "sector_first_trigger_ledger.pkl"
    query = source / "terminal_query_plan.json"
    direct = source / "direct_extract_manifest.json"
    checkpoint = source / "direct_symbols/SH_TEST.pkl"
    pit = tmp_path / "fixed/pit_metadata.json"
    catalog = tmp_path / "mutable/catalog.json"
    checkpoint.parent.mkdir()
    pit.parent.mkdir()
    catalog.parent.mkdir()
    trigger.write_bytes(b"trigger")
    query.write_bytes(b"query")
    checkpoint.write_bytes(b"opaque checkpoint")
    pit.write_bytes(b"pit")
    catalog.write_bytes(b"catalog")
    direct.write_text(
        json.dumps(
            {
                "symbols": {
                    "SH.TEST": {
                        "checkpoint_path": "direct_symbols/SH_TEST.pkl",
                        "checkpoint_sha256": subject.sha256_file(checkpoint),
                        "checkpoint_size_bytes": checkpoint.stat().st_size,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    bundle, targets, identity = subject._freeze_release_bundle(
        root=tmp_path,
        directory=tmp_path / "published/release_inputs",
        trigger=trigger,
        query=query,
        direct_manifest=direct,
        pit=pit,
        catalog=catalog,
    )

    assert bundle.name == "bundle." + identity.removeprefix("sha256:")
    assert all(path.is_relative_to(bundle) for path in targets.values())
    assert targets["trigger_ledger"].read_bytes() == b"trigger"
    assert targets["pit_snapshot"].read_bytes() == b"pit"
    assert (
        targets["direct_manifest"].parent / "direct_symbols/SH_TEST.pkl"
    ).read_bytes() == b"opaque checkpoint"


def test_manifest_replacement_archives_old_bytes_and_allows_paths_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, replace=True)
    output = tmp_path / args.output
    output.parent.mkdir(parents=True)
    old = _manifest(catalog_path="mutable/catalog.json")
    old_bytes = (json.dumps(old, sort_keys=True) + "\n").encode()
    output.write_bytes(old_bytes)
    source = tmp_path / args.catalog_ledger
    source.parent.mkdir(parents=True)
    source.write_bytes(b"catalog bytes")
    new = _manifest(
        catalog_path="published/release_inputs/content-addressed.json"
    )
    monkeypatch.setattr(
        subject, "build_sector_release_manifest", lambda **kwargs: new
    )
    monkeypatch.setattr(
        subject,
        "verify_sector_release_manifest",
        lambda **kwargs: {"all_bound_files_verified": True},
    )

    receipt = subject.run(args)

    assert receipt["disposition"] == "REPLACED_EQUIVALENT_PATH_BINDING"
    archive = tmp_path / str(receipt["archived_previous_manifest"])
    assert archive.read_bytes() == old_bytes
    assert json.loads(output.read_text(encoding="utf-8")) == new


def test_manifest_replacement_rejects_any_hash_change_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, replace=True)
    output = tmp_path / args.output
    output.parent.mkdir(parents=True)
    old = _manifest(catalog_path="mutable/catalog.json")
    old_bytes = (json.dumps(old, sort_keys=True) + "\n").encode()
    output.write_bytes(old_bytes)
    source = tmp_path / args.catalog_ledger
    source.parent.mkdir(parents=True)
    source.write_bytes(b"catalog bytes")
    changed = _manifest(
        catalog_path="published/release_inputs/content-addressed.json",
        catalog_sha=SHA_B,
    )
    monkeypatch.setattr(
        subject, "build_sector_release_manifest", lambda **kwargs: changed
    )
    monkeypatch.setattr(
        subject,
        "verify_sector_release_manifest",
        lambda **kwargs: {"all_bound_files_verified": True},
    )

    with pytest.raises(subject.SectorReleaseManifestError, match="beyond bound paths"):
        subject.run(args)

    assert output.read_bytes() == old_bytes
    assert not (output.parent / "release_manifest_history").exists()


def test_existing_release_verification_never_reads_mutable_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _args(tmp_path, replace=False)
    output = tmp_path / args.output
    output.parent.mkdir(parents=True)
    output.write_text("{}", encoding="utf-8")
    artifact = tmp_path / args.artifact
    artifact.write_text("{}", encoding="utf-8")
    frozen = tmp_path / "published/release_inputs/catalog.json"
    frozen.parent.mkdir(parents=True)
    frozen.write_bytes(b"frozen")
    monkeypatch.setattr(
        subject,
        "verify_sector_release_manifest",
        lambda **kwargs: {
            "all_bound_files_verified": True,
            "bound_files": {
                "current_catalog_ledger": {
                    "path": "published/release_inputs/catalog.json",
                    "file_sha256": subject.sha256_file(frozen),
                }
            },
        },
    )
    monkeypatch.setattr(
        subject,
        "build_sector_release_manifest",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("existing verification must not rebuild")
        ),
    )

    receipt = subject.run(args)

    assert receipt["disposition"] == "VERIFIED_EXISTING_IMMUTABLE_RELEASE"
    assert receipt["catalog_source_path"] is None
    assert not (tmp_path / args.catalog_ledger).exists()
