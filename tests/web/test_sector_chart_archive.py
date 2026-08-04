from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from chanlun.decision_support.fingerprints import sha256_json
from cl_app.services.sector_chart_archive import (
    SECTOR_CHART_ARCHIVE_RELATIVE_PATH,
    SECTOR_CHART_ARCHIVE_SCHEMA,
    SectorChartArchiveUnavailable,
    load_sector_chart_archive,
    load_sector_chart_frame,
    sector_chart_entry,
    sector_chart_frame_content_sha256,
    sector_chart_history_payload,
    sector_chart_symbol_info,
)


HASH = "sha256:" + "1" * 64
SECTOR = "qmt-gics3:" + "2" * 64


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _archive(root: Path) -> tuple[dict[str, str], str]:
    artifact_path = root / "audit" / "source.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text('{"source":true}\n', encoding="utf-8")
    manifest_path = root / SECTOR_CHART_ARCHIVE_RELATIVE_PATH
    frame_path = manifest_path.parent / "frames" / "point.30.parquet"
    frame_path.parent.mkdir(parents=True)
    cutoff = 1_735_891_200
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(
                [cutoff - 3600, cutoff], unit="s", utc=True
            ).tz_convert("Asia/Shanghai"),
            "open": [10.0, 10.2],
            "high": [10.3, 10.5],
            "low": [9.9, 10.1],
            "close": [10.2, 10.4],
            "volume": [1000.0, 1200.0],
        }
    )
    frame.to_parquet(frame_path, index=False)
    bindings = {
        "file_sha256": _sha256_file(artifact_path),
        "content_sha256": "sha256:" + "3" * 64,
        "risk_audit_sha256": "sha256:" + "4" * 64,
        "decision_source_aggregate_sha256": "sha256:" + "5" * 64,
    }
    stable = {
        "schema": SECTOR_CHART_ARCHIVE_SCHEMA,
        "live_status": "LIVE_DISABLED",
        "result_status": "RESEARCH_ONLY",
        "source_artifact": {
            "relative_path": artifact_path.relative_to(root).as_posix(),
            **bindings,
        },
        "input_hashes": {
            "pit_snapshot": "sha256:" + "6" * 64,
            "current_catalog_ledger": "sha256:" + "7" * 64,
            "current_catalog_entry": "sha256:" + "8" * 64,
        },
        "supported_intervals": ["30"],
        "entries": [
            {
                "entry_id": HASH,
                "sector_id": SECTOR,
                "sector_name": "测试板块",
                "review_as_of": pd.Timestamp(
                    cutoff, unit="s", tz="UTC"
                ).tz_convert("Asia/Shanghai").isoformat(),
                "review_as_of_unix": cutoff,
                "source_revision": "sha256:" + "9" * 64,
                "price_basis_revision": "sha256:" + "a" * 64,
                "pricescale": 1_000_000,
                "frames": {
                    "30": {
                        "interval": "30",
                        "path": frame_path.relative_to(
                            manifest_path.parent
                        ).as_posix(),
                        "file_sha256": _sha256_file(frame_path),
                        "content_sha256": sector_chart_frame_content_sha256(
                            frame
                        ),
                        "row_count": 2,
                        "first_at": pd.Timestamp(frame.iloc[0]["date"]).isoformat(),
                        "last_at": pd.Timestamp(frame.iloc[-1]["date"]).isoformat(),
                    }
                },
            }
        ],
    }
    document = {**stable, "content_sha256": sha256_json(stable)}
    manifest_path.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return bindings, document["content_sha256"]


def test_verified_sector_archive_serves_only_the_causal_prefix(
    tmp_path: Path,
) -> None:
    bindings, manifest_id = _archive(tmp_path)
    archive = load_sector_chart_archive(
        tmp_path,
        expected_source_artifact_file_sha256=bindings["file_sha256"],
        expected_source_artifact_content_sha256=bindings["content_sha256"],
        expected_risk_audit_sha256=bindings["risk_audit_sha256"],
        expected_decision_source_sha256=bindings[
            "decision_source_aggregate_sha256"
        ],
        expected_manifest_content_sha256=manifest_id,
    )
    entry = sector_chart_entry(
        archive,
        sector_id=SECTOR,
        review_as_of=1_735_891_200,
        interval="30",
    )
    assert entry["entry_id"] == HASH
    _entry, frame = load_sector_chart_frame(
        archive, entry_id=HASH, interval="30"
    )
    assert len(frame) == 2

    payload = sector_chart_history_payload(
        archive,
        entry_id=HASH,
        interval="30",
        from_ts=0,
        to_ts=1_900_000_000,
    )
    assert payload["s"] == "ok"
    assert max(payload["t"]) == 1_735_891_200
    info = sector_chart_symbol_info(archive, entry_id=HASH, interval="30")
    assert info["ticker"] == f"a:{SECTOR}"
    assert info["supported_resolutions"] == ["30"]


def test_sector_archive_rejects_wrong_cutoff_and_frame_tampering(
    tmp_path: Path,
) -> None:
    _bindings, _manifest_id = _archive(tmp_path)
    archive = load_sector_chart_archive(tmp_path)
    with pytest.raises(
        SectorChartArchiveUnavailable, match="no exact archive prefix"
    ):
        sector_chart_entry(
            archive,
            sector_id=SECTOR,
            review_as_of=1_735_891_201,
            interval="30",
        )

    frame_path = archive.manifest_path.parent / "frames" / "point.30.parquet"
    frame_path.write_bytes(frame_path.read_bytes() + b"tamper")
    with pytest.raises(SectorChartArchiveUnavailable, match="file hash changed"):
        load_sector_chart_frame(archive, entry_id=HASH, interval="30")
