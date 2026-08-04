from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.research_release import v3_sector_release_manifest as subject


ALGORITHM_HASHES = (("src/example.py", "sha256:" + "a" * 64),)
ALGORITHM_REVISION = "sha256:" + "b" * 64
CATALOG_ENTRY = "sha256:" + "c" * 64


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def _content_document(stable: dict[str, object]) -> dict[str, object]:
    return {**stable, "content_sha256": sha256_json(stable)}


def _query_document(stable: dict[str, object]) -> dict[str, object]:
    return {
        **stable,
        "content_sha256": "sha256:"
        + hashlib.sha256(
            json.dumps(stable, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest(),
    }


def _algorithm_rows() -> list[dict[str, str]]:
    return [
        {"path": path, "sha256": digest}
        for path, digest in ALGORITHM_HASHES
    ]


def _release_graph(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    monkeypatch.setattr(
        subject,
        "_current_algorithm",
        lambda value: (ALGORITHM_HASHES, ALGORITHM_REVISION),
    )
    trigger = root / "inputs/trigger.pkl"
    pit = root / "inputs/pit.json"
    catalog = root / "inputs/catalog.json"
    query_path = root / "inputs/query.json"
    direct_path = root / "inputs/direct.json"
    checkpoint = root / "inputs/direct_symbols/SH_TEST.pkl"
    artifact_path = root / "published/result.json"
    manifest_path = root / "published/v3_release_manifest.json"
    trigger.parent.mkdir(parents=True, exist_ok=True)
    trigger.write_bytes(b"opaque trigger bytes")
    pit.write_bytes(b"opaque pit bytes")
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    # Deliberately not a pickle: verification must never deserialize it.
    checkpoint.write_bytes(b"not a pickle but immutable")

    catalog_stable: dict[str, object] = {
        "schema": subject.CATALOG_LEDGER_SCHEMA,
        "entries": [{"entry_sha256": CATALOG_ENTRY}],
    }
    _write_json(catalog, _content_document(catalog_stable))

    query_stable: dict[str, object] = {
        "schema": subject.TERMINAL_QUERY_PLAN_SCHEMA,
        "algorithm_hash_scope": subject.RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
        "algorithm_revision": ALGORITHM_REVISION,
        "algorithm_hashes": _algorithm_rows(),
        "trigger_ledger_sha256": subject.sha256_file(trigger),
        "current_catalog_ledger_sha256": subject.sha256_file(catalog),
        "current_catalog_entry_sha256": CATALOG_ENTRY,
        "requested_symbol_count": 1,
        "completed_symbol_count": 1,
        "computed_symbol_count": 1,
        "conservative_superset_pruned_symbol_count": 0,
        "potential_symbol_count": 1,
        "potential_symbols": ["SH.TEST"],
        "failed_symbol_count": 0,
        "failures": {},
        "rows": [{"symbol": "SH.TEST"}],
    }
    _write_json(query_path, _query_document(query_stable))

    direct: dict[str, object] = {
        "schema": subject.DIRECT_EXTRACT_MANIFEST_SCHEMA,
        "complete": True,
        "failures": {},
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "algorithm": {
            "scope": subject.RECENT_YEAR_RESEARCH_ALGORITHM_SCOPE,
            "revision": ALGORITHM_REVISION,
            "hashes": _algorithm_rows(),
        },
        "inputs": {
            "trigger_ledger_sha256": subject.sha256_file(trigger),
            "query_plan_sha256": subject.sha256_file(query_path),
            "pit_snapshot_sha256": subject.sha256_file(pit),
            "current_catalog_ledger_sha256": subject.sha256_file(catalog),
            "current_catalog_entry_sha256": CATALOG_ENTRY,
            "sector_scope_sha256": CATALOG_ENTRY,
        },
        "summary": {
            "selected_symbol_count": 1,
            "completed_symbol_count": 1,
            "failed_symbol_count": 0,
        },
        "symbols": {
            "SH.TEST": {
                "checkpoint_path": "direct_symbols/SH_TEST.pkl",
                "checkpoint_sha256": subject.sha256_file(checkpoint),
                "checkpoint_size_bytes": checkpoint.stat().st_size,
            }
        },
    }
    _write_json(direct_path, direct)

    artifact_stable: dict[str, object] = {
        "schema": subject.SECTOR_RESEARCH_ARTIFACT_SCHEMA,
        "highest_status": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
        "input_hashes": {
            "trigger_ledger": subject.sha256_file(trigger),
            "terminal_query_plan": subject.sha256_file(query_path),
            "direct_manifest": subject.sha256_file(direct_path),
            "pit_snapshot": subject.sha256_file(pit),
            "current_catalog_ledger": subject.sha256_file(catalog),
            "current_catalog_entry": CATALOG_ENTRY,
        },
    }
    _write_json(artifact_path, _content_document(artifact_stable))
    manifest = subject.build_sector_release_manifest(
        root=root,
        artifact_path=artifact_path,
        trigger_ledger_path=trigger,
        terminal_query_plan_path=query_path,
        direct_manifest_path=direct_path,
        pit_snapshot_path=pit,
        catalog_ledger_path=catalog,
    )
    _write_json(manifest_path, manifest)
    return manifest_path, artifact_path, checkpoint


def test_release_manifest_binds_every_input_without_unpickling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, artifact, _checkpoint = _release_graph(tmp_path, monkeypatch)

    receipt = subject.verify_sector_release_manifest(
        root=tmp_path,
        manifest_path=manifest,
        expected_artifact_path=artifact,
    )

    assert receipt["all_bound_files_verified"] is True
    assert receipt["checkpoint_payloads_unpickled"] is False
    assert receipt["terminal_symbol_count"] == 1
    assert receipt["direct_checkpoint_count"] == 1
    assert receipt["live_status"] == "LIVE_DISABLED"


def test_release_manifest_can_verify_frozen_graph_after_sources_move_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, artifact, _checkpoint = _release_graph(tmp_path, monkeypatch)
    monkeypatch.setattr(
        subject,
        "_current_algorithm",
        lambda value: (
            (("src/example.py", "sha256:" + "d" * 64),),
            "sha256:" + "e" * 64,
        ),
    )

    with pytest.raises(subject.SectorReleaseManifestError, match="not current"):
        subject.verify_sector_release_manifest(
            root=tmp_path,
            manifest_path=manifest,
            expected_artifact_path=artifact,
        )

    receipt = subject.verify_sector_release_manifest(
        root=tmp_path,
        manifest_path=manifest,
        expected_artifact_path=artifact,
        require_current_algorithm=False,
    )

    assert receipt["all_bound_files_verified"] is True
    assert receipt["algorithm_revision"] == ALGORITHM_REVISION
    assert receipt["algorithm_matches_current"] is False


def test_release_manifest_rejects_same_size_checkpoint_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, artifact, checkpoint = _release_graph(tmp_path, monkeypatch)
    checkpoint.write_bytes(b"X" * checkpoint.stat().st_size)

    with pytest.raises(subject.SectorReleaseManifestError, match="checkpoint hash"):
        subject.verify_sector_release_manifest(
            root=tmp_path,
            manifest_path=manifest,
            expected_artifact_path=artifact,
        )


def test_release_manifest_rejects_rehashed_path_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, artifact, _checkpoint = _release_graph(tmp_path, monkeypatch)
    document = json.loads(manifest.read_text(encoding="utf-8"))
    document["upstream"]["trigger_ledger"]["path"] = "../outside.pkl"
    document.pop("content_sha256")
    document["content_sha256"] = sha256_json(document)
    _write_json(manifest, document)

    with pytest.raises(subject.SectorReleaseManifestError, match="canonical"):
        subject.verify_sector_release_manifest(
            root=tmp_path,
            manifest_path=manifest,
            expected_artifact_path=artifact,
        )
