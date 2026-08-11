from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from math import ceil
import random
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from chanlun.decision_support.trading_system import higher_timeframe_gate as subject
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.qmt_higher_timeframe import (
    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID,
    QmtHigherTimeframeInputs,
    QmtHigherTimeframeWarmupEvidence,
    build_qmt_higher_timeframe_risk,
    qmt_higher_timeframe_inputs,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    DailyMarketBar,
)
from chanlun.decision_support.trading_system.higher_timeframe_gate import (
    HigherTimeframeDataUnavailable,
    QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID,
    QmtHigherTimeframeGateSource,
    QmtSectorSameBaseCoverageEvidence,
    _sector_same_base_frames,
    build_sector_higher_timeframe_gate_from_five_minute,
    build_sector_higher_timeframe_research_gate_from_native_daily,
    sector_native_daily_research_bridge_contract,
    unresolved_higher_timeframe_gates,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    build_causal_sector_price_basis_metadata,
    qmt_causal_factor_revision,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    QmtMinuteSessionIssue,
    build_qmt_same_base_stream_frames,
)
from chanlun.exchange.qmt_screening_sector_source import (
    QMT_GICS3_COMPOSITE_ADJUSTMENT,
    QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE,
    QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT,
    QMT_GICS3_COMPOSITE_MEMBER_LIMIT,
    QMT_GICS3_COMPOSITE_METHOD,
    QMT_GICS3_COMPOSITE_PROVIDER,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
)
from tests.trading_system.test_qmt_same_base_stream import _native_session


CN = ZoneInfo("Asia/Shanghai")
DECISION = datetime(2026, 7, 27, 14, 1, tzinfo=CN)
REVISION = "sha256:" + "a" * 64
BASE = "sha256:" + "b" * 64


def _coverage(
    warmup: QmtHigherTimeframeWarmupEvidence,
    *,
    observed_at: datetime = DECISION,
) -> QmtSectorSameBaseCoverageEvidence:
    status = (
        "REQUIRED_HISTORY_CONVERGED"
        if warmup.converged
        else (
            "REQUIRED_HISTORY_PRESENT_BUT_TAIL_DIVERGED"
            if warmup.full_daily_bar_count >= warmup.required_daily_bar_count
            else "VISIBLE_PREFIX_INSUFFICIENT_WITHOUT_LEADING_GAP"
        )
    )
    return QmtSectorSameBaseCoverageEvidence(
        observed_at=observed_at,
        calendar_first_session=observed_at.date(),
        first_visible_bar_at=observed_at.replace(hour=9, minute=35),
        last_visible_bar_at=observed_at,
        first_completed_session=(
            observed_at.date() if warmup.full_daily_bar_count else None
        ),
        last_completed_session=(
            observed_at.date() if warmup.full_daily_bar_count else None
        ),
        visible_five_minute_bar_count=max(1, warmup.full_daily_bar_count * 48),
        completed_daily_bar_count=warmup.full_daily_bar_count,
        required_daily_bar_count=warmup.required_daily_bar_count,
        remaining_daily_bar_count=max(
            0,
            warmup.required_daily_bar_count - warmup.full_daily_bar_count,
        ),
        missing_leading_calendar_session_count=0,
        warmup_converged=warmup.converged,
        warmup_reason_code=warmup.reason_code,
        boundary_status=status,
        physical_source_boundary_status=(
            "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
        ),
        physical_source_requested_start_at=observed_at.replace(
            hour=9, minute=35
        ),
        physical_source_required_contributor_start_at=observed_at.replace(
            hour=9, minute=35
        ),
        physical_source_representative_member_count=24,
        physical_source_available_member_count=23,
        physical_source_required_contributor_count=15,
        physical_source_inventory_revision="sha256:" + "9" * 64,
    )


def test_sector_native_daily_bridge_contract_can_never_enable_live() -> None:
    contract = sector_native_daily_research_bridge_contract()
    stable = dict(contract)
    identity = stable.pop("parameter_set_id")

    assert identity == sha256_json(stable)
    assert contract["cross_frequency_reconciliation"] == (
        "UNRECONCILED_NONLINEAR_MEDIAN"
    )
    assert contract["green_cap"] == "GREEN_TO_AMBER"
    assert contract["data_grade"] == "RESEARCH_ONLY"
    assert contract["live_status"] == "LIVE_DISABLED"


def test_sector_coverage_proves_physical_qmt_cache_left_boundary() -> None:
    session = date(2026, 7, 24)
    observed_at = datetime(2026, 7, 24, 15, 0, tzinfo=CN)
    frame = pd.DataFrame(
        {
            "date": tuple(subject._sector_five_minute_closes(session)),
        }
    )
    frame.attrs["sector_id"] = "qmt-gics3:test"
    physical_stable = {
        "schema": "chanlun-qmt-current-sector-physical-5m-coverage",
        "sector_id": "qmt-gics3:test",
        "observed_at": observed_at,
        "requested_start_at": datetime(2023, 5, 1, 9, 30, tzinfo=CN),
        "representative_member_count": 24,
        "available_member_file_count": 23,
        "physical_boundary_member_count": 23,
        "missing_member_file_count": 1,
        "required_contributor_count": 15,
        "physical_source_first_at_minimum": datetime(
            2025, 4, 29, 11, 5, tzinfo=CN
        ),
        "physical_source_first_at_maximum": datetime(
            2026, 4, 1, 9, 35, tzinfo=CN
        ),
        "required_contributor_physical_start_at": datetime(
            2025, 4, 30, 10, 50, tzinfo=CN
        ),
        "selected_window_first_at_minimum": datetime(
            2025, 4, 29, 11, 5, tzinfo=CN
        ),
        "selected_window_first_at_maximum": datetime(
            2026, 4, 1, 9, 35, tzinfo=CN
        ),
        "boundary_status": (
            "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
        ),
        "source_inventory_revision": "sha256:" + "7" * 64,
        "diagnostic_only": True,
        "decision_core_input": False,
        "warmup_requirement_unchanged": True,
        "data_grade": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    frame.attrs["qmt_physical_five_minute_source_coverage"] = {
        **physical_stable,
        "audit_sha256": sha256_json(physical_stable),
    }
    warmup = QmtHigherTimeframeWarmupEvidence(
        required_daily_bar_count=480,
        full_daily_bar_count=1,
        suffix_daily_bar_count=0,
        converged=False,
        reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        full_signature="sha256:" + "8" * 64,
        suffix_signature=None,
    )

    coverage = subject.build_qmt_sector_same_base_coverage_evidence(
        five_minute_frame=frame,
        observed_at=observed_at,
        trading_sessions=(session,),
        warmup_evidence=warmup,
    )
    document = coverage.document()

    assert (
        document["contract_id"]
        == QMT_SECTOR_SAME_BASE_COVERAGE_EVIDENCE_CONTRACT_ID
    )
    assert document["physical_source_boundary_status"] == (
        "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
    )
    assert document["physical_source_available_member_count"] == 23
    assert document["physical_source_required_contributor_count"] == 15
    assert document["physical_source_requested_start_at"] == (
        "2023-05-01T09:30:00+08:00"
    )
    assert document["physical_source_required_contributor_start_at"] == (
        "2025-04-30T10:50:00+08:00"
    )
    assert QmtSectorSameBaseCoverageEvidence.from_document(document) == coverage

    for changed in (
        {
            "boundary_status": (
                "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
            )
        },
        {"required_contributor_count": 14},
        {"physical_boundary_member_count": 14},
        {
            "requested_start_at": datetime(
                2026, 1, 1, 9, 30, tzinfo=CN
            )
        },
    ):
        contradictory = {**physical_stable, **changed}
        frame.attrs["qmt_physical_five_minute_source_coverage"] = {
            **contradictory,
            "audit_sha256": sha256_json(contradictory),
        }
        with pytest.raises(ValueError, match="physical 5m source"):
            subject.build_qmt_sector_same_base_coverage_evidence(
                five_minute_frame=frame,
                observed_at=observed_at,
                trading_sessions=(session,),
                warmup_evidence=warmup,
            )


def test_sector_resolver_keeps_strict_gate_when_optional_daily_source_fails(
    monkeypatch,
) -> None:
    warmup = QmtHigherTimeframeWarmupEvidence(
        required_daily_bar_count=480,
        full_daily_bar_count=120,
        suffix_daily_bar_count=0,
        converged=False,
        reason_code="QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        full_signature="sha256:" + "4" * 64,
        suffix_signature=None,
    )
    bundle = unresolved_higher_timeframe_gates(
        symbol="SH.600000",
        observed_at=DECISION,
        reason_code=warmup.reason_code,
        sector_subject="qmt-gics3:test",
    )
    assert bundle.sector is not None
    strict = replace(bundle.sector, warmup_evidence=warmup)
    monkeypatch.setattr(
        subject,
        "build_sector_higher_timeframe_gate_from_five_minute",
        lambda **_kwargs: strict,
    )
    monkeypatch.setattr(
        subject,
        "build_qmt_sector_same_base_coverage_evidence",
        lambda **_kwargs: _coverage(warmup),
    )
    calls = 0

    def unavailable_daily() -> pd.DataFrame:
        nonlocal calls
        calls += 1
        raise RuntimeError("QMT transport unavailable")

    resolution = subject.resolve_sector_higher_timeframe_gate(
        sector_id="qmt-gics3:test",
        sector_members=("SH.600000",),
        five_minute_frame=pd.DataFrame(),
        observed_at=DECISION,
        trading_sessions=(DECISION.date(),),
        native_daily_loader=unavailable_daily,
    )

    assert calls == 1
    assert resolution.source_mode == subject.QMT_SECTOR_SAME_BASE_SOURCE_MODE
    assert resolution.evidence.gate == "UNRESOLVED"
    assert resolution.evidence.snapshot_id != strict.snapshot_id
    assert resolution.evidence.sector_strict_same_base_warmup_evidence == warmup
    assert resolution.fallback_unavailable_reason_codes == (
        "QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_UNAVAILABLE",
    )
    assert resolution.evidence.reason_codes == (
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        "QMT_SECTOR_NATIVE_DAILY_RESEARCH_SOURCE_UNAVAILABLE",
    )

    no_fallback = unresolved_higher_timeframe_gates(
        symbol="SH.600000",
        observed_at=DECISION,
        reason_code="STRICT_SECTOR_GATE_UNRESOLVED",
        sector_subject="qmt-gics3:test",
    )
    assert no_fallback.sector is not None
    no_fallback_sector = replace(
        no_fallback.sector,
        warmup_evidence=warmup,
    )
    monkeypatch.setattr(
        subject,
        "build_sector_higher_timeframe_gate_from_five_minute",
        lambda **_kwargs: no_fallback_sector,
    )
    lazy = subject.resolve_sector_higher_timeframe_gate(
        sector_id="qmt-gics3:test",
        sector_members=("SH.600000",),
        five_minute_frame=pd.DataFrame(),
        observed_at=DECISION,
        trading_sessions=(DECISION.date(),),
        native_daily_loader=lambda: (_ for _ in ()).throw(
            AssertionError("daily fallback must remain lazy")
        ),
    )
    assert lazy.source_mode == subject.QMT_SECTOR_SAME_BASE_SOURCE_MODE
    assert lazy.fallback_unavailable_reason_codes == ()


def frame(times: tuple[datetime, ...]) -> pd.DataFrame:
    value = pd.DataFrame(
        {
            "date": times,
            "open": [10 + index for index in range(len(times))],
            "high": [11 + index for index in range(len(times))],
            "low": [9 + index for index in range(len(times))],
            "close": [10.5 + index for index in range(len(times))],
            "volume": [1000 for _ in times],
        }
    )
    value.attrs.update(
        {
            "price_basis_provider": "qmt",
            "price_basis_adjustment": "front",
            "price_basis_revision": REVISION,
            "source_base_stream_revision": BASE,
            "source_base_frequency": "1m",
        }
    )
    return value


def _sector_member_path_revision(value: pd.DataFrame) -> str:
    return sha256_json(
        {
            "schema": "chanlun-qmt-sector-composite-member-path",
            "rows": tuple(
                {
                    "date": pd.Timestamp(row.date).to_pydatetime(),
                    "member_mask": int(row.member_mask),
                }
                for row in value.itertuples(index=False)
            ),
        }
    )


def _attach_physical_sector_coverage(
    frame: pd.DataFrame,
    *,
    sector_id: str,
    members: tuple[str, ...],
    observed_at: datetime,
    requested_start_at: datetime,
) -> None:
    first = pd.Timestamp(frame.iloc[0]["date"]).to_pydatetime()
    required = max(
        QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT,
        ceil(
            Decimal(len(members))
            * QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE
        ),
    )
    stable = {
        "schema": "chanlun-qmt-current-sector-physical-5m-coverage",
        "sector_id": sector_id,
        "observed_at": observed_at,
        "requested_start_at": requested_start_at,
        "representative_member_count": len(members),
        "available_member_file_count": len(members),
        "physical_boundary_member_count": len(members),
        "missing_member_file_count": 0,
        "required_contributor_count": required,
        "physical_source_first_at_minimum": first,
        "physical_source_first_at_maximum": first,
        "required_contributor_physical_start_at": first,
        "selected_window_first_at_minimum": first,
        "selected_window_first_at_maximum": first,
        "boundary_status": (
            "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
            if first > requested_start_at
            else "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
        ),
        "source_inventory_revision": "sha256:" + "6" * 64,
        "diagnostic_only": True,
        "decision_core_input": False,
        "warmup_requirement_unchanged": True,
        "data_grade": "RESEARCH_ONLY",
        "live_status": "LIVE_DISABLED",
    }
    frame.attrs["qmt_physical_five_minute_source_coverage"] = {
        **stable,
        "audit_sha256": sha256_json(stable),
    }


def _daily_from_minute(
    minute: pd.DataFrame,
    sessions: tuple[date, ...] | list[date],
    observed_at: datetime,
) -> pd.DataFrame:
    return build_qmt_same_base_stream_frames(
        symbol="SH.600000",
        one_minute_frame=minute,
        decision_time=observed_at,
        expected_sessions=sessions,
    ).daily


def _calendar_provider(sessions: tuple[date, ...] | list[date]):
    frozen = tuple(sessions)

    def provider(*, start: date, end: date, observed_at: datetime):
        assert end <= observed_at.date()
        return tuple(value for value in frozen if start <= value <= end)

    return provider


def test_qmt_higher_timeframe_adapter_consumes_completed_prefix_only() -> None:
    daily = frame(
        (
            datetime(2026, 7, 24, 15, tzinfo=CN),
            datetime(2026, 7, 27, 15, tzinfo=CN),
        )
    )
    thirty = frame(
        (
            datetime(2026, 7, 27, 10, 0, tzinfo=CN),
            datetime(2026, 7, 27, 14, 0, tzinfo=CN),
            datetime(2026, 7, 27, 14, 30, tzinfo=CN),
        )
    )

    result = qmt_higher_timeframe_inputs(
        symbol="SH.600000",
        daily_frame=daily,
        thirty_minute_frame=thirty,
        decision_time=DECISION,
    )

    assert tuple(row.session for row in result.daily_bars) == (date(2026, 7, 24),)
    assert tuple(row.end_at.time() for row in result.completed_30m_bars) == (
        time(10, 0),
        time(14, 0),
    )
    assert result.same_base_stream is True
    assert result.blockers == ()


def test_same_qmt_provider_without_shared_one_minute_hash_is_research_only_input() -> None:
    daily = frame((datetime(2026, 7, 24, 15, tzinfo=CN),))
    thirty = frame((datetime(2026, 7, 24, 15, tzinfo=CN),))
    daily.attrs.pop("source_base_stream_revision")
    thirty.attrs.pop("source_base_stream_revision")

    result = qmt_higher_timeframe_inputs(
        symbol="SH.600000",
        daily_frame=daily,
        thirty_minute_frame=thirty,
        decision_time=DECISION,
    )

    assert result.same_base_stream is False
    assert "QMT_DAILY_AND_30M_NOT_FROM_SAME_1M_BASE" in {
        blocker.code for blocker in result.blockers
    }


def test_price_basis_mismatch_fails_closed() -> None:
    daily = frame((datetime(2026, 7, 24, 15, tzinfo=CN),))
    thirty = frame((datetime(2026, 7, 24, 15, tzinfo=CN),))
    thirty.attrs["price_basis_revision"] = "sha256:" + "c" * 64

    result = qmt_higher_timeframe_inputs(
        symbol="SH.600000",
        daily_frame=daily,
        thirty_minute_frame=thirty,
        decision_time=DECISION + timedelta(days=1),
    )

    assert result.price_basis_revision is None
    assert "QMT_DAILY_AND_30M_PRICE_BASIS_MISMATCH" in {
        blocker.code for blocker in result.blockers
    }


def test_sector_base_cannot_relabel_a_one_minute_lineage_as_five_minute() -> None:
    daily = frame((datetime(2026, 7, 24, 15, tzinfo=CN),))
    thirty = frame((datetime(2026, 7, 24, 15, tzinfo=CN),))

    result = qmt_higher_timeframe_inputs(
        symbol="QMT:GICS3:bank",
        daily_frame=daily,
        thirty_minute_frame=thirty,
        decision_time=DECISION,
        required_base_frequency="5m",
    )

    assert result.same_base_stream is False
    assert result.source_base_frequency is None
    assert "QMT_SECTOR_DAILY_AND_30M_NOT_FROM_SAME_5M_BASE" in {
        blocker.code for blocker in result.blockers
    }


def test_live_gate_derives_daily_and_30m_from_one_completed_1m_prefix() -> None:
    sessions = (date(2026, 7, 23), date(2026, 7, 24))
    first = _native_session(sessions[0])
    second = _native_session(sessions[1], base=20.0)
    minute = pd.concat((first, second), ignore_index=True)
    minute.attrs = dict(first.attrs)
    observed_at = datetime(2026, 7, 24, 15, 1, tzinfo=CN)
    native_daily = _daily_from_minute(minute, sessions, observed_at)

    class Exchange:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict[str, object]]] = []

        def klines(self, symbol, frequency, *, start_date, end_date, args):
            assert "+" not in start_date and "+" not in end_date
            self.calls.append((symbol, frequency, dict(args)))
            if frequency == "1m":
                return minute
            if frequency == "d":
                return native_daily
            raise AssertionError("native 30m must not enter the live risk gate")

    exchange = Exchange()
    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: exchange,
        daily_bars=60,
        thirty_minute_bars=240,
        trading_calendar_provider=_calendar_provider(sessions),
    )
    daily, thirty, reconciliation = source._frames(
        "SH.600000",
        observed_at,
        expected_sessions=sessions,
    )

    assert len(daily) == 2
    assert len(thirty) == 16
    assert daily.attrs["source_base_stream_revision"] == thirty.attrs[
        "source_base_stream_revision"
    ]
    assert reconciliation.overlap_session_count == 2
    assert exchange.calls[0][1] == "1m"
    assert exchange.calls[1][1] == "d"
    assert exchange.calls[0][2]["dividend_type"] == "front"
    assert exchange.calls[0][2]["research_exact_end"] is True


def test_live_gate_refreshes_one_minute_once_when_native_daily_is_ahead() -> None:
    sessions = (date(2026, 7, 23), date(2026, 7, 24))
    first = _native_session(sessions[0])
    second = _native_session(sessions[1], base=20.0)
    complete_minute = pd.concat((first, second), ignore_index=True)
    complete_minute.attrs = dict(first.attrs)
    partial_minute = pd.concat((first, second.iloc[:100]), ignore_index=True)
    partial_minute.attrs = dict(first.attrs)
    observed_at = datetime(2026, 7, 24, 15, 1, tzinfo=CN)
    native_daily = _daily_from_minute(complete_minute, sessions, observed_at)

    class Exchange:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, object]]] = []

        def klines(self, _symbol, frequency, **kwargs):
            args = dict(kwargs["args"])
            self.calls.append((frequency, args))
            if frequency == "d":
                return native_daily
            assert frequency == "1m"
            return partial_minute if args["skip_download"] else complete_minute

    exchange = Exchange()
    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: exchange,
        daily_bars=60,
        thirty_minute_bars=240,
        trading_calendar_provider=_calendar_provider(sessions),
    )

    daily, thirty, reconciliation = source._frames(
        "SH.600000",
        observed_at,
        expected_sessions=sessions,
    )

    assert len(daily) == 2
    assert len(thirty) == 16
    assert reconciliation.last_overlap_session == "2026-07-24"
    minute_calls = [args for frequency, args in exchange.calls if frequency == "1m"]
    assert [args["skip_download"] for args in minute_calls] == [True, False]
    assert [frequency for frequency, _args in exchange.calls].count("d") == 1


def test_latest_closed_expected_session_survives_weekend_and_holiday() -> None:
    sessions = (date(2026, 7, 30), date(2026, 7, 31))

    assert subject._latest_closed_expected_session(
        datetime(2026, 8, 1, 9, 0, tzinfo=CN),
        sessions,
    ) == date(2026, 7, 31)
    assert subject._latest_closed_expected_session(
        datetime(2026, 8, 3, 10, 0, tzinfo=CN),
        sessions,
    ) == date(2026, 7, 31)


def test_latest_closed_expected_session_rejects_intraday_current_session() -> None:
    sessions = (date(2026, 7, 23), date(2026, 7, 24))

    assert subject._latest_closed_expected_session(
        datetime(2026, 7, 24, 14, 59, tzinfo=CN),
        sessions,
    ) is None
    assert subject._latest_closed_expected_session(
        datetime(2026, 7, 24, 15, 0, tzinfo=CN),
        sessions,
    ) == date(2026, 7, 24)


def test_live_gate_stays_fail_closed_when_refreshed_one_minute_is_still_partial(
) -> None:
    sessions = (date(2026, 7, 23), date(2026, 7, 24))
    first = _native_session(sessions[0])
    second = _native_session(sessions[1], base=20.0)
    complete_minute = pd.concat((first, second), ignore_index=True)
    complete_minute.attrs = dict(first.attrs)
    partial_minute = pd.concat((first, second.iloc[:100]), ignore_index=True)
    partial_minute.attrs = dict(first.attrs)
    observed_at = datetime(2026, 7, 24, 15, 1, tzinfo=CN)
    native_daily = _daily_from_minute(complete_minute, sessions, observed_at)

    class Exchange:
        def __init__(self) -> None:
            self.minute_calls = 0

        def klines(self, _symbol, frequency, **_kwargs):
            if frequency == "d":
                return native_daily
            self.minute_calls += 1
            return partial_minute

    exchange = Exchange()
    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: exchange,
        daily_bars=60,
        thirty_minute_bars=240,
        trading_calendar_provider=_calendar_provider(sessions),
    )

    with pytest.raises(HigherTimeframeDataUnavailable) as caught:
        source._frames(
            "SH.600000",
            observed_at,
            expected_sessions=sessions,
        )

    assert caught.value.reason_codes == (
        "QMT_NATIVE_DAILY_AHEAD_OF_ONE_MINUTE_BASE",
    )
    assert exchange.minute_calls == 2


def test_live_gate_drops_only_a_requested_oldest_session_fragment() -> None:
    fragment = _native_session(date(2026, 7, 23)).iloc[-20:].copy()
    complete = _native_session(date(2026, 7, 24), base=20.0)
    frame = pd.concat((fragment, complete), ignore_index=True)
    frame.attrs = dict(complete.attrs)

    trimmed = QmtHigherTimeframeGateSource._drop_requested_leading_fragment(
        frame,
        decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
        requested_rows=len(frame),
    )

    assert set(trimmed["date"].dt.date) == {date(2026, 7, 24)}
    assert trimmed.attrs["qmt_leading_fragment_dropped"] == "2026-07-23"
    assert trimmed.attrs["qmt_leading_fragment_reason"] == "REQUEST_COUNT_BOUNDARY"

    shorter_local_history = (
        QmtHigherTimeframeGateSource._drop_requested_leading_fragment(
            frame,
            decision_time=datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            requested_rows=len(frame) + 1000,
        )
    )
    assert set(shorter_local_history["date"].dt.date) == {date(2026, 7, 24)}
    assert shorter_local_history.attrs["qmt_leading_fragment_reason"] == (
        "LOCAL_HISTORY_COVERAGE_BOUNDARY"
    )


def test_live_gate_preserves_same_base_session_blocker_codes() -> None:
    sessions = (date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24))
    first = _native_session(sessions[0])
    last = _native_session(sessions[-1], base=20.0)
    minute = pd.concat((first, last), ignore_index=True)
    minute.attrs = dict(first.attrs)

    class Exchange:
        def klines(self, *_args, **_kwargs):
            return minute

    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: Exchange(),
        daily_bars=60,
        thirty_minute_bars=240,
    )

    with pytest.raises(HigherTimeframeDataUnavailable) as caught:
        source._frames(
            "SH.600000",
            datetime(2026, 7, 24, 15, 1, tzinfo=CN),
            expected_sessions=sessions,
        )

    assert "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING" in caught.value.reason_codes
    assert len(caught.value.reason_codes) == len(set(caught.value.reason_codes))
    assert [value.document() for value in caught.value.session_issues] == [
        {
            "session": "2026-07-23",
            "code": "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
            "observed_rows": 0,
            "classification": "UNCLASSIFIED_EXPECTED_SESSION_ABSENCE",
            "detail": (
                "trading-calendar session is absent from the QMT 1m prefix"
            ),
            "historical_trade_status_proven": False,
            "entry_disposition": "FAIL_CLOSED",
        }
    ]


def test_live_gate_preserves_native_daily_calendar_gap_without_inferencing_suspension(
) -> None:
    sessions = (date(2026, 7, 22), date(2026, 7, 23), date(2026, 7, 24))
    parts = tuple(
        _native_session(session, base=10.0 + index)
        for index, session in enumerate(sessions)
    )
    minute = pd.concat(parts, ignore_index=True)
    minute.attrs = dict(parts[0].attrs)
    observed_at = datetime(2026, 7, 24, 15, 1, tzinfo=CN)
    native_daily = _daily_from_minute(minute, sessions, observed_at)
    native_daily = native_daily[
        native_daily["date"].dt.date != sessions[1]
    ].copy()
    native_daily.attrs = dict(minute.attrs)

    class Exchange:
        def klines(self, _symbol, frequency, **_kwargs):
            return minute if frequency == "1m" else native_daily

    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: Exchange(),
        daily_bars=60,
        thirty_minute_bars=240,
    )
    with pytest.raises(HigherTimeframeDataUnavailable) as caught:
        source._frames(
            "SH.600000",
            observed_at,
            expected_sessions=sessions,
        )

    assert caught.value.reason_codes == (
        "QMT_NATIVE_DAILY_TRADING_CALENDAR_MISMATCH",
    )
    coverage = caught.value.native_daily_calendar_coverage_evidence
    assert coverage is not None
    assert coverage.status == "UNEXPLAINED_CALENDAR_SESSION_MISSING"
    assert coverage.unexplained_calendar_only_sessions == (sessions[1],)
    assert coverage.document()["point_in_time_status_evidence_present"] is False

    unresolved = unresolved_higher_timeframe_gates(
        symbol="SH.600000",
        observed_at=observed_at,
        reason_codes=caught.value.reason_codes,
        symbol_native_daily_calendar_coverage_evidence=coverage,
    )
    document = unresolved.symbol.document()
    assert document["native_daily_calendar_coverage_evidence"] == (
        coverage.document()
    )
    assert document["allows_new_entry"] is False


def test_live_same_base_mwd_gate_fails_closed_before_warmup_budget() -> None:
    sessions: list[date] = []
    current = date(2026, 1, 2)
    while len(sessions) < 130:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    parts = tuple(
        _native_session(session, base=10.0 + index / 100)
        for index, session in enumerate(sessions)
    )
    minute = pd.concat(parts, ignore_index=True)
    minute.attrs = dict(parts[0].attrs)
    observed_at = datetime.combine(
        sessions[-1],
        time(15, 1),
        tzinfo=CN,
    )
    native_daily = _daily_from_minute(minute, sessions, observed_at)

    class Exchange:
        def klines(self, _symbol, frequency, **_kwargs):
            return minute if frequency == "1m" else native_daily

    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: Exchange(),
        daily_bars=120,
        thirty_minute_bars=960,
        trading_calendar_provider=_calendar_provider(sessions),
    )

    gates = source.gates(symbol="SH.600000", as_of=observed_at)

    assert gates.market.gate == gates.symbol.gate == "UNRESOLVED"
    assert gates.market.grade == gates.symbol.grade == "UNRESOLVED"
    assert gates.market.reason_codes == gates.symbol.reason_codes == (
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
    )
    assert gates.market.warmup_evidence is not None
    assert gates.market.warmup_evidence.document() == {
        "contract_id": "chanlun-qmt-mwd-warmup-evidence",
        "required_daily_bar_count": 480,
        "full_daily_bar_count": 120,
        "suffix_daily_bar_count": 0,
        "converged": False,
        "reason_code": (
            "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
        ),
        "full_signature": gates.market.warmup_evidence.full_signature,
        "suffix_signature": None,
        "entry_disposition": "FAIL_CLOSED",
    }
    assert tuple(row.period for row in gates.market.period_diagnostics) == (
        "M",
        "W",
        "D",
    )
    counts = {
        row.period: row.completed_bar_count
        for row in gates.market.period_diagnostics
    }
    assert counts["D"] == 120
    assert counts["M"] > 0 and counts["W"] > 0
    assert all(row.state == "NONE" for row in gates.market.period_diagnostics)
    document = gates.market.document()
    assert [row["period"] for row in document["period_diagnostics"]] == [
        "M",
        "W",
        "D",
    ]

    # A caller that supplies only an opaque sector ID cannot fabricate the
    # point-in-time member set needed by the same-base composite adapter.
    sector_gates = source.gates(
        symbol="SH.600000",
        sector_id="QMT:GICS3:bank",
        as_of=observed_at,
    )
    assert sector_gates.market.gate == "UNRESOLVED"
    assert sector_gates.symbol.gate == "UNRESOLVED"
    assert sector_gates.sector is not None
    assert sector_gates.sector.gate == "UNRESOLVED"
    assert sector_gates.sector.reason_codes == (
        "QMT_SECTOR_HIGHER_TIMEFRAME_INPUT_UNAVAILABLE",
    )
    assert sector_gates.allows_new_entry is False

    stale_session = sessions[-1] + timedelta(days=1)
    while stale_session.weekday() >= 5:
        stale_session += timedelta(days=1)
    stale = source.gates(
        symbol="SH.600000",
        as_of=datetime.combine(stale_session, time(15, 1), tzinfo=CN),
    )
    assert stale.market.gate == stale.symbol.gate == "UNRESOLVED"
    assert stale.market.reason_codes == (
        "QMT_BENCHMARK_ONE_MINUTE_PREFIX_STALE",
    )
    assert stale.market.period_diagnostics == ()


def test_mwd_gate_becomes_evaluable_only_after_pairwise_warmup_converges() -> None:
    sessions: list[date] = []
    current = date(2024, 1, 2)
    while len(sessions) < 480:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    parts = tuple(
        _native_session(session, base=10.0 + index / 100)
        for index, session in enumerate(sessions)
    )
    minute = pd.concat(parts, ignore_index=True)
    minute.attrs = dict(parts[0].attrs)
    observed_at = datetime.combine(sessions[-1], time(15, 1), tzinfo=CN)
    native_daily = _daily_from_minute(minute, sessions, observed_at)

    class Exchange:
        def klines(self, _symbol, frequency, **_kwargs):
            return minute if frequency == "1m" else native_daily

    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: Exchange(),
        trading_calendar_provider=_calendar_provider(sessions),
    )
    daily, thirty, reconciliation = source._frames(
        "SH.600000",
        observed_at,
        expected_sessions=tuple(sessions),
    )
    inputs = qmt_higher_timeframe_inputs(
        symbol="SH.600000",
        daily_frame=daily,
        thirty_minute_frame=thirty,
        decision_time=observed_at,
        required_base_frequency="1m+native-d",
        native_daily_reconciliation_evidence=reconciliation,
    )
    envelope = build_qmt_higher_timeframe_risk(
        inputs=inputs,
        trading_sessions=tuple(sessions),
        calendar_coverage_end=sessions[-1],
        snapshot_id="risk:warmup-stable",
    )

    assert envelope.grade == "FULL_SYSTEM_ELIGIBLE"
    assert envelope.risk.gate == "GREEN"
    assert envelope.warmup.converged is True
    assert envelope.warmup.full_daily_bar_count == 480
    assert envelope.warmup.suffix_daily_bar_count == 320
    assert envelope.warmup.full_signature == envelope.warmup.suffix_signature
    assert envelope.warmup_convergence is not None
    assert envelope.warmup_convergence.status == "INSUFFICIENT_PREFIXES"
    assert envelope.warmup_convergence.active_gate_unchanged is True


def test_mwd_multi_prefix_diagnostic_uses_all_qualified_history_lengths() -> None:
    sessions: list[date] = []
    current = date(2022, 1, 3)
    while len(sessions) < 960:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    daily = tuple(
        DailyMarketBar(
            session=session,
            open=Decimal("10") + Decimal(index) / Decimal("1000"),
            high=Decimal("10.2") + Decimal(index) / Decimal("1000"),
            low=Decimal("9.8") + Decimal(index) / Decimal("1000"),
            close=Decimal("10.1") + Decimal(index) / Decimal("1000"),
            volume=Decimal("1000"),
            known_at=datetime.combine(session, time(15), tzinfo=CN),
        )
        for index, session in enumerate(sessions)
    )
    inputs = QmtHigherTimeframeInputs(
        symbol="SH.600000",
        observed_at=daily[-1].known_at,
        daily_bars=daily,
        completed_30m_bars=(),
        price_basis_revision="sha256:" + "1" * 64,
        source_base_stream_revision="sha256:" + "2" * 64,
        source_revision="sha256:" + "3" * 64,
        blockers=(),
        source_base_frequency="1m",
    )

    envelope = build_qmt_higher_timeframe_risk(
        inputs=inputs,
        trading_sessions=tuple(sessions),
        calendar_coverage_end=sessions[-1],
        snapshot_id="risk:warmup-multi-prefix",
    )

    convergence = envelope.warmup_convergence
    assert convergence is not None
    assert convergence.parameter_set_id == (
        QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
    )
    assert tuple(value.bar_count for value in convergence.observations) == (
        480,
        640,
        800,
        960,
    )
    assert convergence.as_of == inputs.observed_at
    assert convergence.frequency == "d"
    assert convergence.diagnostic_only is True
    assert convergence.active_gate_unchanged is True
    # The active entry gate remains the frozen full-vs-oldest-third result.
    assert envelope.warmup.suffix_daily_bar_count == 640
    assert all(
        not code.startswith("WARMUP_ENVELOPE_")
        for code in (blocker.code for blocker in envelope.blockers)
    )


def test_mwd_gate_rejects_equal_length_but_semantically_divergent_warmup() -> None:
    sessions: list[date] = []
    current = date(2022, 1, 3)
    while len(sessions) < 480:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    generator = random.Random(2)
    price = 100.0
    daily: list[DailyMarketBar] = []
    for index, session in enumerate(sessions):
        drift = 0.18 if (index // 45) % 4 in (0, 3) else -0.16
        price = max(10.0, price + drift + generator.gauss(0, 1.6))
        spread = abs(generator.gauss(1.4, 0.5)) + 0.2
        opening = price + generator.gauss(0, 0.4)
        daily.append(
            DailyMarketBar(
                session=session,
                open=Decimal(str(opening)),
                high=Decimal(str(max(opening, price) + spread)),
                low=Decimal(str(min(opening, price) - spread)),
                close=Decimal(str(price)),
                volume=Decimal("1000"),
                known_at=datetime.combine(session, time(15), tzinfo=CN),
            )
        )
    inputs = QmtHigherTimeframeInputs(
        symbol="SH.600000",
        observed_at=daily[-1].known_at,
        daily_bars=tuple(daily),
        completed_30m_bars=(),
        price_basis_revision="sha256:" + "1" * 64,
        source_base_stream_revision="sha256:" + "2" * 64,
        source_revision="sha256:" + "3" * 64,
        blockers=(),
        source_base_frequency="1m",
    )

    envelope = build_qmt_higher_timeframe_risk(
        inputs=inputs,
        trading_sessions=tuple(sessions),
        calendar_coverage_end=sessions[-1],
        snapshot_id="risk:warmup-diverged",
    )

    assert envelope.warmup.full_daily_bar_count == 480
    assert envelope.warmup.suffix_daily_bar_count == 320
    assert envelope.warmup.converged is False
    assert envelope.warmup.reason_code == (
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED"
    )
    assert envelope.warmup.full_signature != envelope.warmup.suffix_signature
    assert envelope.risk.snapshot is None
    assert envelope.grade == "UNRESOLVED"
    assert envelope.warmup.reason_code in {
        blocker.code for blocker in envelope.blockers
    }


def test_symbol_data_gap_preserves_market_gate_and_is_cached() -> None:
    observed_at = datetime(2026, 7, 24, 15, 1, tzinfo=CN)
    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: object(),
        daily_bars=60,
        thirty_minute_bars=240,
    )
    source._benchmark_prefix_is_current = lambda _observed: True  # type: ignore[method-assign]
    bucket = observed_at.isoformat(timespec="minutes")
    source._calendar_cache[bucket] = (observed_at.date(),)
    market = unresolved_higher_timeframe_gates(
        symbol="SH.000300",
        observed_at=observed_at,
        reason_code="MARKET_GATE_PROVEN_FOR_TEST",
    ).market
    calls: list[str] = []

    def one(symbol: str, **_kwargs):
        calls.append(symbol)
        if symbol == "SH.000300":
            return market
        raise HigherTimeframeDataUnavailable(
            ("QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",),
            session_issues=(
                QmtMinuteSessionIssue(
                    session=observed_at.date(),
                    code="QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
                    observed_rows=0,
                    detail=(
                        "trading-calendar session is absent from the QMT 1m prefix"
                    ),
                ),
            ),
        )

    source._one = one  # type: ignore[method-assign]

    first = source.gates(symbol="SH.600000", as_of=observed_at)
    second = source.gates(symbol="SH.600000", as_of=observed_at)

    assert first == second
    assert first.market is market
    assert first.market.reason_codes == ("MARKET_GATE_PROVEN_FOR_TEST",)
    assert first.symbol.gate == "UNRESOLVED"
    assert first.symbol.reason_codes == (
        "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
    )
    assert first.symbol.session_evidence is not None
    assert first.symbol.session_evidence.document()["issues"] == [
        {
            "session": "2026-07-24",
            "code": "QMT_ONE_MINUTE_EXPECTED_SESSION_MISSING",
            "observed_rows": 0,
            "classification": "UNCLASSIFIED_EXPECTED_SESSION_ABSENCE",
            "detail": (
                "trading-calendar session is absent from the QMT 1m prefix"
            ),
            "historical_trade_status_proven": False,
            "entry_disposition": "FAIL_CLOSED",
        }
    ]
    # First call evaluates the market and symbol once; the repeated monitoring
    # read consumes the exact same fail-closed bundle without recomputation.
    assert calls == ["SH.000300", "SH.600000"]


def test_sector_gate_uses_one_qmt_five_minute_base_and_fails_short_warmup() -> None:
    sessions: list[date] = []
    current = date(2026, 1, 2)
    while len(sessions) < 130:
        if current.weekday() < 5:
            sessions.append(current)
        current += timedelta(days=1)
    parts = tuple(
        _native_session(session, base=10.0 + index / 100)
        for index, session in enumerate(sessions)
    )
    minute = pd.concat(parts, ignore_index=True)
    minute.attrs = dict(parts[0].attrs)
    observed_at = datetime.combine(sessions[-1], time(15, 1), tzinfo=CN)
    native_daily = _daily_from_minute(minute, sessions, observed_at)
    members = tuple(f"SH.6000{index:02d}" for index in range(8))
    sector_five = build_qmt_same_base_stream_frames(
        symbol="QMT:GICS3:bank",
        one_minute_frame=minute,
        decision_time=observed_at,
        expected_sessions=sessions,
    ).five_minute
    sector_five["volume"] = len(members)
    sector_five["member_mask"] = (1 << len(members)) - 1
    membership_revision = sha256_json(
        {
            "schema": "chanlun-qmt-gics3-members",
            "sector_id": "QMT:GICS3:bank",
            "members": members,
            "composite_members": members,
        }
    )
    factor_revision = qmt_causal_factor_revision(
        members=members,
        events_by_code={},
        known_through=observed_at.date(),
    )
    attach_price_basis_metadata(
        sector_five,
        build_causal_sector_price_basis_metadata(
            provider=QMT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=(
                "QMT:GICS3:bank:"
                + membership_revision.removeprefix("sha256:")
            ),
            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=Decimal("0.000001"),
            factor_revision=factor_revision,
        ),
    )
    sector_five.attrs.update(
        {
            "sector_id": "QMT:GICS3:bank",
            "sector_membership_revision": membership_revision,
            "sector_membership_scope": "CALLER_SUPPLIED",
            "sector_members": members,
            "sector_composite_members": members,
            "sector_composite_member_limit": (
                QMT_GICS3_COMPOSITE_MEMBER_LIMIT
            ),
            "sector_composite_minimum_member_count": (
                QMT_GICS3_COMPOSITE_MINIMUM_MEMBER_COUNT
            ),
            "sector_composite_minimum_bar_coverage": str(
                QMT_GICS3_COMPOSITE_MINIMUM_BAR_COVERAGE
            ),
            "sector_composite_required_member_count": len(members),
            "sector_composite_member_mask_contract": (
                "BIT_I_IS_SECTOR_COMPOSITE_MEMBERS_I"
            ),
            "sector_composite_member_path_revision": (
                _sector_member_path_revision(sector_five)
            ),
            "sector_composite_method": QMT_GICS3_COMPOSITE_METHOD,
            "sector_factor_adjustment_contract_id": (
                QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
            ),
            "sector_factor_revision": factor_revision,
        }
    )
    extended_sessions = list(sessions)
    cursor = sessions[0] - timedelta(days=1)
    while len(extended_sessions) < 520:
        if cursor.weekday() < 5:
            extended_sessions.append(cursor)
        cursor -= timedelta(days=1)
    extended_sessions = sorted(extended_sessions)
    _attach_physical_sector_coverage(
        sector_five,
        sector_id="QMT:GICS3:bank",
        members=members,
        observed_at=observed_at,
        requested_start_at=datetime.combine(
            extended_sessions[0], time(9, 35), tzinfo=CN
        ),
    )
    native_sector = pd.DataFrame(
        {
            "date": [
                datetime.combine(value, time(15), tzinfo=CN)
                for value in extended_sessions
            ],
            "open": [1000 + index for index in range(len(extended_sessions))],
            "high": [1001.5 + index for index in range(len(extended_sessions))],
            "low": [999.5 + index for index in range(len(extended_sessions))],
            "close": [1001 + index for index in range(len(extended_sessions))],
            "volume": [len(members)] * len(extended_sessions),
            "member_mask": [(1 << len(members)) - 1] * len(extended_sessions),
        }
    )
    native_sector.attrs = dict(sector_five.attrs)
    native_sector.attrs.update(
        {
            "source_base_frequency": "native-d",
            "source_base_stream_revision": "sha256:" + "7" * 64,
            "derived_frequency": "d",
            "sector_native_daily_role": (
                "UNRECONCILED_RESEARCH_MWD_ADVISORY_ONLY"
            ),
            "sector_composite_member_path_revision": (
                _sector_member_path_revision(native_sector)
            ),
        }
    )
    provider_calls: list[dict[str, object]] = []

    def sector_frame_provider(**kwargs):
        provider_calls.append(kwargs)
        return native_sector if kwargs["frequency"] == "1d" else sector_five

    class Exchange:
        def klines(self, _symbol, frequency, **_kwargs):
            return minute if frequency == "1m" else native_daily

    source = QmtHigherTimeframeGateSource(
        exchange_provider=lambda: Exchange(),
        daily_bars=120,
        thirty_minute_bars=960,
        sector_frame_provider=sector_frame_provider,
        sector_daily_bars=120,
        sector_thirty_minute_bars=960,
        trading_calendar_provider=_calendar_provider(extended_sessions),
    )
    first = source.gates(
        symbol="SH.600000",
        sector_id="QMT:GICS3:bank",
        sector_name="银行",
        sector_members=members,
        as_of=observed_at,
    )
    direct_sector = build_sector_higher_timeframe_gate_from_five_minute(
        sector_id="QMT:GICS3:bank",
        sector_members=members,
        five_minute_frame=sector_five,
        observed_at=observed_at,
        trading_sessions=sessions,
        calendar_coverage_end=sessions[-1],
        daily_bars=120,
        thirty_minute_bars=960,
    )
    hybrid_sector = (
        build_sector_higher_timeframe_research_gate_from_native_daily(
            sector_id="QMT:GICS3:bank",
            sector_members=members,
            native_daily_frame=native_sector,
            five_minute_frame=sector_five,
            observed_at=observed_at,
            trading_sessions=extended_sessions,
            calendar_coverage_end=extended_sessions[-1],
            daily_bars=480,
            thirty_minute_bars=960,
        )
    )
    second = source.gates(
        symbol="SH.600000",
        sector_id="QMT:GICS3:bank",
        sector_name="银行",
        sector_members=members,
        as_of=observed_at,
    )
    same_sector_other_symbol = source.gates(
        symbol="SH.600001",
        sector_id="QMT:GICS3:bank",
        sector_name="银行",
        sector_members=members,
        as_of=observed_at,
    )

    assert first == second
    assert same_sector_other_symbol.sector == first.sector
    assert direct_sector.gate == "UNRESOLVED"
    assert hybrid_sector.gate == "AMBER"
    assert hybrid_sector.grade == "RESEARCH_ONLY"
    assert hybrid_sector.warmup_evidence is not None
    assert hybrid_sector.warmup_evidence.converged is True
    assert (
        "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
        in hybrid_sector.reason_codes
    )
    assert first.sector is not None
    assert first.sector.gate == "AMBER", first.sector
    assert first.sector.grade == "RESEARCH_ONLY"
    assert (
        "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
        in first.sector.reason_codes
    )
    assert first.sector.warmup_evidence is not None
    assert first.sector.warmup_evidence.full_daily_bar_count == 520
    assert first.sector.warmup_evidence.converged is True
    assert first.sector.sector_source_mode == (
        "NATIVE_DAILY_MWD_PLUS_5M_30M_UNRECONCILED_RESEARCH"
    )
    assert first.sector.sector_strict_same_base_warmup_evidence is not None
    assert (
        first.sector.sector_strict_same_base_warmup_evidence.full_daily_bar_count
        == 120
    )
    assert first.sector.sector_strict_same_base_source_coverage_evidence is not None
    assert (
        first.sector.sector_strict_same_base_source_coverage_evidence.document()[
            "boundary_status"
        ]
        == "VISIBLE_PREFIX_STARTS_AFTER_REQUESTED_WARMUP"
    )
    assert (
        first.sector.sector_strict_same_base_source_coverage_evidence.document()[
            "remaining_daily_bar_count"
        ]
            == 350
        )
    assert first.sector.sector_research_bridge_parameter_set_id == (
        sector_native_daily_research_bridge_contract()["parameter_set_id"]
    )
    assert tuple(row.period for row in first.sector.period_diagnostics) == (
        "M",
        "W",
        "D",
    )
    assert first.allows_new_entry is False
    assert len(provider_calls) == 2
    assert provider_calls[0]["frequency"] == "5m"
    assert provider_calls[1]["frequency"] == "1d"
    assert provider_calls[1]["request_bars"] == 720
    assert provider_calls[0]["members"] == members

    broken = sector_five.drop(index=100).reset_index(drop=True)
    broken.attrs = dict(sector_five.attrs)
    with pytest.raises(HigherTimeframeDataUnavailable) as caught:
        _sector_same_base_frames(
            sector_id="QMT:GICS3:bank",
            sector_members=members,
            five_minute_frame=broken,
            decision_time=observed_at,
            expected_sessions=sessions,
        )
    assert caught.value.reason_codes == (
        "QMT_SECTOR_FIVE_MINUTE_SESSION_GRID_INVALID",
    )

    for field, bad in (
        ("sector_id", "QMT:GICS3:other"),
        ("sector_members", tuple(reversed(members))),
        ("sector_membership_revision", None),
        ("sector_membership_revision", "sha256:" + "9" * 64),
        ("sector_composite_members", members[:-1]),
        ("sector_composite_member_limit", 25),
        ("sector_composite_minimum_member_count", 7),
        ("sector_composite_minimum_bar_coverage", "0.50"),
        ("sector_composite_required_member_count", 7),
    ):
        relabelled = sector_five.copy(deep=True)
        relabelled.attrs = {**sector_five.attrs, field: bad}
        with pytest.raises(HigherTimeframeDataUnavailable) as provenance:
            _sector_same_base_frames(
                sector_id="QMT:GICS3:bank",
                sector_members=members,
                five_minute_frame=relabelled,
                decision_time=observed_at,
                expected_sessions=sessions,
            )
        assert provenance.value.reason_codes == (
            "QMT_SECTOR_MEMBERSHIP_PROVENANCE_MISMATCH",
        )

    for field, bad in (
        ("structure_price_quantum", "0.01"),
        ("price_basis_provider", "relabelled-provider"),
        ("price_basis_adjustment", "front"),
        ("price_basis_revision", "sha256:" + "8" * 64),
    ):
        relabelled = sector_five.copy(deep=True)
        relabelled.attrs = {**sector_five.attrs, field: bad}
        with pytest.raises(HigherTimeframeDataUnavailable) as price_basis:
            _sector_same_base_frames(
                sector_id="QMT:GICS3:bank",
                sector_members=members,
                five_minute_frame=relabelled,
                decision_time=observed_at,
                expected_sessions=sessions,
            )
        assert price_basis.value.reason_codes == (
            "QMT_SECTOR_FIVE_MINUTE_PRICE_BASIS_UNRESOLVED",
        )

    large_members = tuple(f"SH.{601000 + index:06d}" for index in range(30))
    ranked = sorted(
        large_members,
        key=lambda code: sha256_json(
            {
                "schema": "chanlun-qmt-gics3-sample",
                "sector_id": "QMT:GICS3:bank",
                "code": code,
            }
        ),
    )
    expected_sample = tuple(
        sorted(ranked[:QMT_GICS3_COMPOSITE_MEMBER_LIMIT])
    )
    large_membership_revision = sha256_json(
        {
            "schema": "chanlun-qmt-gics3-members",
            "sector_id": "QMT:GICS3:bank",
            "members": large_members,
            "composite_members": expected_sample,
        }
    )
    large = sector_five.copy(deep=True)
    large["volume"] = len(expected_sample)
    large["member_mask"] = (1 << len(expected_sample)) - 1
    large.attrs = {
        **sector_five.attrs,
        "sector_members": large_members,
        "sector_composite_members": expected_sample,
        "sector_composite_required_member_count": 15,
        "sector_composite_member_path_revision": (
            _sector_member_path_revision(large)
        ),
        "sector_membership_revision": large_membership_revision,
    }
    large_factor_revision = qmt_causal_factor_revision(
        members=expected_sample,
        events_by_code={},
        known_through=observed_at.date(),
    )
    large.attrs["sector_factor_revision"] = large_factor_revision
    attach_price_basis_metadata(
        large,
        build_causal_sector_price_basis_metadata(
            provider=QMT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=(
                "QMT:GICS3:bank:"
                + large_membership_revision.removeprefix("sha256:")
            ),
            adjustment=QMT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=Decimal("0.000001"),
            factor_revision=large_factor_revision,
        ),
    )
    _sector_same_base_frames(
        sector_id="QMT:GICS3:bank",
        sector_members=large_members,
        five_minute_frame=large,
        decision_time=observed_at,
        expected_sessions=sessions,
    )

    replacement = next(value for value in large_members if value not in expected_sample)
    forged_sample = tuple(sorted((*expected_sample[:-1], replacement)))
    forged = large.copy(deep=True)
    forged.attrs = {
        **large.attrs,
        "sector_composite_members": forged_sample,
        # Even a self-consistent forged membership hash cannot replace the
        # independently reproduced deterministic sample.
        "sector_membership_revision": sha256_json(
            {
                "schema": "chanlun-qmt-gics3-members",
                "sector_id": "QMT:GICS3:bank",
                "members": large_members,
                "composite_members": forged_sample,
            }
        ),
    }
    with pytest.raises(HigherTimeframeDataUnavailable) as forged_provenance:
        _sector_same_base_frames(
            sector_id="QMT:GICS3:bank",
            sector_members=large_members,
            five_minute_frame=forged,
            decision_time=observed_at,
            expected_sessions=sessions,
        )
    assert forged_provenance.value.reason_codes == (
        "QMT_SECTOR_MEMBERSHIP_PROVENANCE_MISMATCH",
    )

    for invalid_contributor_count in (14, 14.5, 25):
        invalid_coverage = large.astype({"volume": "float64"}, copy=True)
        invalid_coverage.attrs = dict(large.attrs)
        invalid_coverage.loc[
            invalid_coverage.index[0], "volume"
        ] = invalid_contributor_count
        with pytest.raises(HigherTimeframeDataUnavailable) as member_coverage:
            _sector_same_base_frames(
                sector_id="QMT:GICS3:bank",
                sector_members=large_members,
                five_minute_frame=invalid_coverage,
                decision_time=observed_at,
                expected_sessions=sessions,
            )
        assert member_coverage.value.reason_codes == (
            "QMT_SECTOR_COMPOSITE_MEMBER_COVERAGE_MISMATCH",
        )

    invalid_member_path = large.copy(deep=True)
    invalid_member_path.attrs = dict(large.attrs)
    invalid_member_path.loc[
        invalid_member_path.index[0], "member_mask"
    ] = (1 << 23) - 1
    invalid_member_path.attrs["sector_composite_member_path_revision"] = (
        _sector_member_path_revision(invalid_member_path)
    )
    with pytest.raises(HigherTimeframeDataUnavailable) as member_path:
        _sector_same_base_frames(
            sector_id="QMT:GICS3:bank",
            sector_members=large_members,
            five_minute_frame=invalid_member_path,
            decision_time=observed_at,
            expected_sessions=sessions,
        )
    assert member_path.value.reason_codes == (
        "QMT_SECTOR_COMPOSITE_MEMBER_PATH_PROVENANCE_MISMATCH",
    )

    # Equal contributor counts do not prove an equal point-in-time sample.
    # Rotating which 15 of the frozen 24 members contributed must remain
    # visible in both the member-path identity and the derived 5m lineage.
    first_path = large.copy(deep=True)
    first_path["volume"] = 15
    first_path["member_mask"] = (1 << 15) - 1
    first_path.attrs = dict(large.attrs)
    first_path.attrs["sector_composite_member_path_revision"] = (
        _sector_member_path_revision(first_path)
    )
    second_path = large.copy(deep=True)
    second_path["volume"] = 15
    second_path["member_mask"] = ((1 << 15) - 1) << 9
    second_path.attrs = dict(large.attrs)
    second_path.attrs["sector_composite_member_path_revision"] = (
        _sector_member_path_revision(second_path)
    )

    first_daily, first_thirty = _sector_same_base_frames(
        sector_id="QMT:GICS3:bank",
        sector_members=large_members,
        five_minute_frame=first_path,
        decision_time=observed_at,
        expected_sessions=sessions,
    )
    second_daily, second_thirty = _sector_same_base_frames(
        sector_id="QMT:GICS3:bank",
        sector_members=large_members,
        five_minute_frame=second_path,
        decision_time=observed_at,
        expected_sessions=sessions,
    )

    assert first_path.attrs[
        "sector_composite_member_path_revision"
    ] != second_path.attrs["sector_composite_member_path_revision"]
    assert first_daily.attrs[
        "source_base_stream_revision"
    ] != second_daily.attrs["source_base_stream_revision"]
    assert first_thirty.attrs[
        "source_base_stream_revision"
    ] != second_thirty.attrs["source_base_stream_revision"]
