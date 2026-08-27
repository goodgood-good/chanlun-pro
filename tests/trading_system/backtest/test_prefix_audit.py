from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path

import pytest

from tools import audit_qmt_prefix_invariance as audit


def test_frozen_calendar_worker_slices_without_native_qmt_calls(monkeypatch) -> None:
    from chanlun.exchange import qmt_screening_sector_source as source

    original = source.qmt_trading_sessions
    monkeypatch.setattr(source, "qmt_trading_sessions", original)
    sessions = (date(2026, 7, 20), date(2026, 7, 21), date(2026, 7, 22))

    audit._install_frozen_trading_calendar(
        date(2026, 7, 1),
        date(2026, 7, 31),
        sessions,
    )

    assert source.qmt_trading_sessions(
        start=date(2026, 7, 21),
        end=date(2026, 7, 22),
        observed_at=datetime.fromisoformat("2026-07-22T15:00:00+08:00"),
    ) == sessions[1:]
    with pytest.raises(RuntimeError, match="escaped frozen coverage"):
        source.qmt_trading_sessions(
            start=date(2026, 6, 30),
            end=date(2026, 7, 22),
            observed_at=datetime.fromisoformat("2026-07-22T15:00:00+08:00"),
        )


def test_existing_prefix_checkpoint_is_bound_to_stage_revision(
    tmp_path: Path,
) -> None:
    fact = tmp_path / "fact.pkl"
    fact.write_bytes(b"fact")
    target = tmp_path / "audit.json"
    request = audit.Request(
        fact_path=str(fact),
        target=str(target),
        warmup_start=date(2025, 11, 1),
        algorithm_revision="sha256:" + "a" * 64,
        prefix_algorithm_revision="sha256:" + "b" * 64,
    )
    payload = {
        "schema": audit.AUDIT_SCHEMA,
        "code": "SH.600000",
        "algorithm_revision": request.algorithm_revision,
        "full_fact_sha256": audit._sha256(fact),
        "status": "passed",
    }
    target.write_text(json.dumps(payload), encoding="utf-8")

    assert audit._valid_existing(target, request) is None

    payload["prefix_algorithm_revision"] = request.prefix_algorithm_revision
    target.write_text(json.dumps(payload), encoding="utf-8")
    assert audit._valid_existing(target, request) == payload
