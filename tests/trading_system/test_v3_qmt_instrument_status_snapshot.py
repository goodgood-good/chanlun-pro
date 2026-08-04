from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.v3_forward_paper import (
    _qmt_instrument_status_snapshot_evidence_proven,
)
from chanlun.decision_support.trading_system.v3_qmt_instrument_status_snapshot import (
    QmtInstrumentStatusSnapshot,
    capture_qmt_instrument_status_snapshot,
)


CN = ZoneInfo("Asia/Shanghai")
SESSION = date(2026, 8, 3)
CATALOG = "sha256:" + "1" * 64
SCREEN = "sha256:" + "2" * 64


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 8, 3, hour, minute, second, tzinfo=CN)


def _detail(
    symbol: str,
    *,
    status: int = 0,
    trading_day: str = "20260803",
) -> dict[str, object]:
    return {
        "InstrumentName": "测试" + symbol,
        "TradingDay": trading_day,
        "InstrumentStatus": status,
        "IsTrading": False,
    }


def _clock(*values: datetime):
    iterator = iter(values)
    return lambda: next(iterator)


def _file_sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _delivery_evidence(result) -> dict[str, object]:
    return {
        **result.evidence(),
        "coverage_scope": "SCREENING_SIGNAL_SYMBOLS_ONLY",
        "can_explain_same_session_decision": False,
        "can_explain_prior_historical_session": False,
        "future_consumer_connected": False,
    }


def test_capture_records_normal_and_suspended_without_same_session_authority(
    tmp_path: Path,
) -> None:
    output = tmp_path / "2026-08-03" / "instrument_status_snapshot.json"
    calls: list[str] = []

    def provider(symbol: str) -> dict[str, object]:
        calls.append(symbol)
        return _detail(symbol, status=2 if symbol == "SZ.000002" else 0)

    result = capture_qmt_instrument_status_snapshot(
        output=output,
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SZ.000002", "SH.600000", "SH.600000"),
        detail_provider=provider,
        clock=_clock(_at(15, 20), _at(15, 20, 2)),
    )

    assert calls == ["SH.600000", "SZ.000002"]
    assert result.reused is False
    assert result.latest_path == output.resolve()
    assert result.object_path.is_file()
    assert result.latest_file_sha256 == result.object_file_sha256
    document = result.snapshot.document()
    assert document["all_complete"] is True
    assert document["status_counts"] == {"NORMAL": 1, "SUSPENDED": 1}
    assert document["same_session_decision_adjudication_allowed"] is False
    assert document["point_in_time_scope"] == (
        "SUBSEQUENT_SESSION_DECISIONS_ONLY"
    )
    assert document["historical_backfill_allowed"] is False
    assert document["tick_data_used"] is False
    assert document["real_account_accessed"] is False
    assert document["real_order_transport_enabled"] is False
    assert QmtInstrumentStatusSnapshot.from_document(document) == result.snapshot
    facts = {row["symbol"]: row for row in document["facts"]}
    assert facts["SH.600000"]["classification"] == "NORMAL"
    assert facts["SZ.000002"]["classification"] == "SUSPENDED"
    assert facts["SZ.000002"]["instrument_status"] == 2


def test_complete_capture_is_reused_verbatim_after_restart(tmp_path: Path) -> None:
    output = tmp_path / "instrument_status_snapshot.json"
    first = capture_qmt_instrument_status_snapshot(
        output=output,
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000",),
        detail_provider=lambda symbol: _detail(symbol),
        clock=_clock(_at(15, 20), _at(15, 20, 1)),
    )

    second = capture_qmt_instrument_status_snapshot(
        output=output,
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000",),
        detail_provider=lambda _symbol: (_ for _ in ()).throw(
            AssertionError("a complete retry must not call QMT")
        ),
        clock=lambda: (_ for _ in ()).throw(
            AssertionError("a complete retry must not advance the clock")
        ),
    )

    assert second.reused is True
    assert second.snapshot == first.snapshot
    assert second.object_path == first.object_path
    assert second.latest_file_sha256 == first.latest_file_sha256


def test_incomplete_capture_is_retried_and_old_attempt_remains_immutable(
    tmp_path: Path,
) -> None:
    output = tmp_path / "instrument_status_snapshot.json"

    def incomplete(symbol: str) -> dict[str, object]:
        if symbol == "SZ.000002":
            raise RuntimeError("temporary QMT failure")
        return _detail(symbol)

    first = capture_qmt_instrument_status_snapshot(
        output=output,
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000", "SZ.000002"),
        detail_provider=incomplete,
        clock=_clock(_at(15, 20), _at(15, 20, 1)),
    )
    assert first.snapshot.all_complete is False
    assert first.snapshot.errors[0].reason_code == (
        "QMT_INSTRUMENT_DETAIL_UNAVAILABLE"
    )

    second = capture_qmt_instrument_status_snapshot(
        output=output,
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000", "SZ.000002"),
        detail_provider=lambda symbol: _detail(symbol),
        clock=_clock(_at(15, 25), _at(15, 25, 1)),
    )
    assert second.reused is False
    assert second.snapshot.all_complete is True
    assert second.object_path != first.object_path
    assert first.object_path.is_file()
    assert QmtInstrumentStatusSnapshot.from_document(
        json.loads(first.object_path.read_text(encoding="utf-8"))
    ) == first.snapshot
    assert QmtInstrumentStatusSnapshot.from_document(
        json.loads(output.read_text(encoding="utf-8"))
    ) == second.snapshot


def test_invalid_or_wrong_day_detail_stays_explicitly_incomplete(
    tmp_path: Path,
) -> None:
    result = capture_qmt_instrument_status_snapshot(
        output=tmp_path / "instrument_status_snapshot.json",
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000",),
        detail_provider=lambda symbol: _detail(
            symbol,
            trading_day="20260731",
        ),
        clock=_clock(_at(15, 20), _at(15, 20, 1)),
    )

    assert result.snapshot.all_complete is False
    assert result.snapshot.facts == ()
    assert result.snapshot.errors[0].reason_code == (
        "QMT_INSTRUMENT_STATUS_FACT_INVALID"
    )
    assert result.snapshot.document()["status_counts"] == {
        "NORMAL": 0,
        "SUSPENDED": 0,
    }


def test_snapshot_rejects_rehashed_scope_or_scalar_type_tampering(
    tmp_path: Path,
) -> None:
    result = capture_qmt_instrument_status_snapshot(
        output=tmp_path / "instrument_status_snapshot.json",
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000",),
        detail_provider=lambda symbol: _detail(symbol),
        clock=_clock(_at(15, 20), _at(15, 20, 1)),
    )
    document = result.snapshot.document()

    scope = json.loads(json.dumps(document))
    scope["same_session_decision_adjudication_allowed"] = True
    stable = dict(scope)
    stable.pop("content_sha256")
    scope["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="snapshot is malformed"):
        QmtInstrumentStatusSnapshot.from_document(scope)

    scalar = json.loads(json.dumps(document))
    scalar["requested_symbol_count"] = True
    stable = dict(scalar)
    stable.pop("content_sha256")
    scalar["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="snapshot is malformed"):
        QmtInstrumentStatusSnapshot.from_document(scalar)

    status_count = json.loads(json.dumps(document))
    status_count["status_counts"]["NORMAL"] = True
    stable = dict(status_count)
    stable.pop("content_sha256")
    status_count["content_sha256"] = sha256_json(stable)
    with pytest.raises(ValueError, match="snapshot is malformed"):
        QmtInstrumentStatusSnapshot.from_document(status_count)


def test_non_mapping_qmt_result_is_an_explicit_capture_error(
    tmp_path: Path,
) -> None:
    result = capture_qmt_instrument_status_snapshot(
        output=tmp_path / "instrument_status_snapshot.json",
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000",),
        detail_provider=lambda _symbol: None,  # type: ignore[return-value]
        clock=_clock(_at(15, 20), _at(15, 20, 1)),
    )

    assert result.snapshot.all_complete is False
    assert result.snapshot.errors[0].reason_code == (
        "QMT_INSTRUMENT_STATUS_FACT_INVALID"
    )


def test_capture_is_rejected_before_the_market_close(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="snapshot identity is invalid"):
        capture_qmt_instrument_status_snapshot(
            output=tmp_path / "instrument_status_snapshot.json",
            session=SESSION,
            sector_catalog_entry_sha256=CATALOG,
            source_screen_content_sha256=SCREEN,
            symbols=(),
            detail_provider=lambda _symbol: {},
            clock=_clock(_at(14, 59), _at(15, 1)),
        )


def test_forward_delivery_proves_status_object_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    session_root = tmp_path / "sessions" / SESSION.isoformat()
    result = capture_qmt_instrument_status_snapshot(
        output=session_root / "qmt_instrument_status_snapshot.json",
        session=SESSION,
        sector_catalog_entry_sha256=CATALOG,
        source_screen_content_sha256=SCREEN,
        symbols=("SH.600000", "SZ.000002"),
        detail_provider=lambda symbol: _detail(symbol),
        clock=_clock(_at(15, 20), _at(15, 20, 1)),
    )
    evidence = _delivery_evidence(result)

    def proof(value: object) -> bool:
        return _qmt_instrument_status_snapshot_evidence_proven(
            value,
            session_root=session_root,
            session=SESSION,
            expected_source_screen_content_sha256=SCREEN,
            expected_symbols=("SZ.000002", "SH.600000", "SH.600000"),
        )

    assert proof(evidence) is True
    assert proof(None) is True  # Legacy sessions remain readable.

    wrong_scope = {**evidence, "future_consumer_connected": True}
    assert proof(wrong_scope) is False
    wrong_count = {**evidence, "requested_symbol_count": True}
    assert proof(wrong_count) is False

    tampered = json.loads(result.object_path.read_text(encoding="utf-8"))
    tampered["facts"][0]["instrument_name"] = "篡改"
    result.object_path.write_text(
        json.dumps(tampered, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    rehashed_evidence = {
        **evidence,
        "object_file_sha256": _file_sha256(result.object_path),
    }
    assert proof(rehashed_evidence) is False


def test_forward_delivery_accepts_only_exact_incomplete_diagnostic() -> None:
    def proof(value: object) -> bool:
        return _qmt_instrument_status_snapshot_evidence_proven(
            value,
            session_root=Path("unused"),
            session=SESSION,
            expected_source_screen_content_sha256=SCREEN,
            expected_symbols=(),
        )

    incomplete = {
        "status": "CAPTURE_INCOMPLETE",
        "session": SESSION.isoformat(),
        "reason_code": "QMT_INSTRUMENT_STATUS_SNAPSHOT_UNAVAILABLE",
        "error": "RuntimeError: QMT unavailable",
        "coverage_scope": "SCREENING_SIGNAL_SYMBOLS_ONLY",
        "can_explain_same_session_decision": False,
        "can_explain_prior_historical_session": False,
        "future_consumer_connected": False,
        "historical_backfill_allowed": False,
        "real_account_accessed": False,
        "real_order_transport_enabled": False,
        "live_status": "LIVE_DISABLED",
    }

    assert proof(incomplete) is True
    assert proof({**incomplete, "session": "2026-08-04"}) is False
    assert proof({**incomplete, "historical_backfill_allowed": True}) is False


def test_snapshot_cannot_cross_the_capture_session(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="snapshot identity is invalid"):
        capture_qmt_instrument_status_snapshot(
            output=tmp_path / "instrument_status_snapshot.json",
            session=SESSION,
            sector_catalog_entry_sha256=CATALOG,
            source_screen_content_sha256=SCREEN,
            symbols=(),
            detail_provider=lambda _symbol: {},
            clock=_clock(_at(23, 59), _at(23, 59) + timedelta(minutes=2)),
        )
