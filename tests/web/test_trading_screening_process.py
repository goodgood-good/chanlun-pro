from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import pytest

from chanlun.decision_support.trading_system.incremental_scan import BarKey
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.engine import SymbolStructureBundle
from chanlun.decision_support.trading_system.sector_strength import (
    build_horizontal_sector_strength_batch,
)
from chanlun.decision_support.trading_system.models import (
    SectorAssessment,
    TimeframeContext,
)
from chanlun.decision_support.trading_system.v3_selection import (
    SectorMemberHistory,
)
from chanlun.decision_support.trading_system.v3_trading_session import (
    build_trading_session_evidence,
)
from cl_app.services.trading_screening_native_worker import (
    _ParentDisconnected,
    _qmt_fact_cache_settings,
    _send_to_parent,
    dispatch_gateway_request,
)
from cl_app.services.trading_screening_gateway import (
    SectorAnalysisExclusion,
    SectorAssessmentBatch,
)
from cl_app.services.trading_screening_process import (
    NativeScreeningWorkerProtocolError,
    NativeScreeningWorkerRemoteError,
    NativeScreeningWorkerTimeout,
    NativeScreeningWorkerUnavailable,
    NativeTradingDataGatewayProcessProxy,
    NativeWorkerProcessConfig,
    NativeWorkerProcessTransport,
    native_sector_snapshot_cache_revision,
    native_sector_snapshot_producer_revision,
)
from cl_app import create_app


FIXTURE = Path(__file__).parent / "fixtures" / "native_screening_test_worker.py"


def test_native_qmt_fact_cache_is_official_launch_only(tmp_path: Path) -> None:
    producer = "sha256:" + "1" * 64
    content_revision = "a" * 40 + ".tree." + "b" * 24

    assert _qmt_fact_cache_settings(
        build_revision="manual-head",
        data_path=tmp_path,
        producer_revision=producer,
    ) == (None, None, None, None, None)

    composite, daily, status, composite_revision, daily_revision = (
        _qmt_fact_cache_settings(
            build_revision="head.tree.abc.run.0123456789abcdef",
            data_path=tmp_path,
            producer_revision=producer,
        )
    )
    assert composite == (
        tmp_path.resolve()
        / "decision_support"
        / "trading_screening_sector_frame_facts"
    )
    assert daily == (
        tmp_path.resolve()
        / "decision_support"
        / "trading_screening_sector_daily_facts.json"
    )
    assert status == (
        tmp_path.resolve()
        / "decision_support"
        / "trading_screening_sector_member_status_facts"
    )
    assert composite_revision == producer
    assert daily_revision == producer

    content_addressed = _qmt_fact_cache_settings(
        build_revision=content_revision,
        data_path=tmp_path,
        producer_revision=producer,
    )
    assert content_addressed[:3] == (composite, daily, status)
    assert content_addressed[-2:] == (producer, producer)

    official = _qmt_fact_cache_settings(
        build_revision="head.tree.abc.run.official",
        data_path=tmp_path,
    )
    from chanlun.exchange.qmt_screening_sector_source import (
        qmt_sector_composite_fact_producer_revision,
        qmt_sector_daily_fact_producer_revision,
    )

    assert official[-2] == qmt_sector_composite_fact_producer_revision()
    assert official[-1] == qmt_sector_daily_fact_producer_revision()
    assert official[-2] != official[-1]


def _native_sector_revision_fixture(root: Path) -> None:
    (root / "src" / "chanlun").mkdir(parents=True)
    (root / "src" / "chanlun" / "decision.py").write_text(
        "DECISION = 1\n", encoding="utf-8"
    )
    services = root / "web" / "chanlun_chart" / "cl_app" / "services"
    services.mkdir(parents=True)
    for name in (
        "trading_screening_gateway.py",
        "trading_screening_native_worker.py",
        "trading_screening_process.py",
    ):
        (services / name).write_text(f"PRODUCER = {name!r}\n", encoding="utf-8")


def test_native_sector_snapshot_revision_ignores_ui_but_tracks_producer(
    tmp_path: Path,
) -> None:
    _native_sector_revision_fixture(tmp_path)
    first = native_sector_snapshot_producer_revision(project_root=tmp_path)
    assert first.startswith("sha256:")

    # An ordinary UI-only deploy has a different whole-application revision,
    # but it cannot alter native sector facts and must retain the same cache.
    static = tmp_path / "web" / "chanlun_chart" / "cl_app" / "static"
    static.mkdir(parents=True)
    (static / "screen.js").write_text("const label = 'v2';\n", encoding="utf-8")
    assert native_sector_snapshot_producer_revision(project_root=tmp_path) == first
    assert native_sector_snapshot_cache_revision(
        "head.tree.ui-a.run.1", project_root=tmp_path
    ) == native_sector_snapshot_cache_revision(
        "head.tree.ui-b.run.2", project_root=tmp_path
    )

    # A decision/source or native codec change must invalidate the snapshot.
    (tmp_path / "src" / "chanlun" / "decision.py").write_text(
        "DECISION = 2\n", encoding="utf-8"
    )
    second = native_sector_snapshot_producer_revision(project_root=tmp_path)
    assert second != first
    producer = (
        tmp_path
        / "web"
        / "chanlun_chart"
        / "cl_app"
        / "services"
        / "trading_screening_gateway.py"
    )
    producer.write_text("PRODUCER = 'changed'\n", encoding="utf-8")
    assert native_sector_snapshot_producer_revision(project_root=tmp_path) != second


def test_native_sector_snapshot_cache_is_official_launch_only(
    tmp_path: Path,
) -> None:
    _native_sector_revision_fixture(tmp_path)
    assert (
        native_sector_snapshot_cache_revision(
            "manual-head", project_root=tmp_path
        )
        is None
    )
    assert native_sector_snapshot_cache_revision(
        "head.tree.source.run.official", project_root=tmp_path
    ) == native_sector_snapshot_producer_revision(project_root=tmp_path)
    assert native_sector_snapshot_cache_revision(
        "a" * 40 + ".tree." + "b" * 24,
        project_root=tmp_path,
    ) == native_sector_snapshot_producer_revision(project_root=tmp_path)


def _transport(
    tmp_path: Path,
    *,
    idle: float = 1.0,
    backoff: float = 0.05,
    progress=lambda: None,
) -> NativeWorkerProcessTransport:
    return NativeWorkerProcessTransport(
        log_path=tmp_path / "native-worker.log",
        worker_command=(sys.executable, str(FIXTURE)),
        config=NativeWorkerProcessConfig(
            startup_timeout_seconds=3.0,
            native_idle_timeout_seconds=idle,
            restart_backoff_seconds=backoff,
        ),
        progress_callback=progress,
    )


def test_authenticated_child_reports_progress_and_safety_boundary(
    tmp_path: Path,
) -> None:
    progress: list[str] = []
    transport = _transport(tmp_path, progress=lambda: progress.append("tick"))
    try:
        assert transport.request("echo", value=7) == {"value": 7}
        health = transport.health_snapshot()
        assert health["ready"] is True
        assert health["worker_alive"] is True
        assert health["worker_pid"] != os.getpid()
        assert health["isolated_process"] is True
        assert health["loopback_authenticated"] is True
        assert health["minimum_market_data_frequency"] == "1m"
        assert health["tick_data_used"] is False
        assert health["real_account_access"] is False
        assert health["real_order_transport"] is False
        assert health["restart_count"] == 1
        assert progress == ["tick"]
    finally:
        transport.shutdown()
    assert transport.health_snapshot()["worker_alive"] is False


def test_explicit_startup_attests_worker_without_data_request(tmp_path: Path) -> None:
    transport = _transport(tmp_path)
    try:
        transport.startup()
        health = transport.health_snapshot()
        assert health["ready"] is True
        assert health["worker_alive"] is True
        assert health["last_method"] is None
        assert health["in_flight"] is False
        assert health["restart_count"] == 1

        # Startup is idempotent and must not replace a healthy process.
        worker_pid = health["worker_pid"]
        transport.startup()
        assert transport.health_snapshot()["worker_pid"] == worker_pid
    finally:
        transport.shutdown()


class _DisconnectedParent:
    def send(self, _value) -> None:
        raise ConnectionResetError(10054, "parent closed the IPC socket")


def test_worker_treats_parent_disconnect_as_clean_lifecycle_control() -> None:
    with pytest.raises(_ParentDisconnected):
        _send_to_parent(_DisconnectedParent(), {"type": "result"})  # type: ignore[arg-type]


def test_native_crash_is_contained_and_next_request_restarts_worker(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path)
    try:
        transport.request("echo", value="before")
        first_pid = transport.health_snapshot()["worker_pid"]
        with pytest.raises(NativeScreeningWorkerUnavailable, match="exited|transport"):
            transport.request("crash")
        failed = transport.health_snapshot()
        assert failed["ready"] is False
        assert failed["worker_alive"] is False
        assert failed["failure_count"] == 1

        time.sleep(0.08)
        assert transport.request("echo", value="after") == {"value": "after"}
        recovered = transport.health_snapshot()
        assert recovered["ready"] is True
        assert recovered["worker_pid"] != first_pid
        assert recovered["restart_count"] == 2
    finally:
        transport.shutdown()


def test_native_idle_timeout_kills_a_stuck_child(tmp_path: Path) -> None:
    transport = _transport(tmp_path, idle=0.15)
    started = time.monotonic()
    try:
        with pytest.raises(NativeScreeningWorkerTimeout, match="no progress"):
            transport.request("hang")
        assert time.monotonic() - started < 2.0
        health = transport.health_snapshot()
        assert health["ready"] is False
        assert health["worker_alive"] is False
        assert health["failure_count"] == 1
    finally:
        transport.shutdown()


def test_normal_remote_error_keeps_the_isolated_worker_alive(tmp_path: Path) -> None:
    transport = _transport(tmp_path)
    try:
        transport.request("echo", value="before")
        worker_pid = transport.health_snapshot()["worker_pid"]
        with pytest.raises(
            NativeScreeningWorkerRemoteError,
            match="ValueError",
        ) as caught:
            transport.request("remote_error")
        assert caught.value.method == "remote_error"
        assert caught.value.remote_error_type == "ValueError"
        assert caught.value.remote_message == "deterministic remote failure"
        health = transport.health_snapshot()
        assert health["ready"] is True
        assert health["worker_pid"] == worker_pid
        assert "deterministic remote failure" in str(health["last_remote_error"])
        assert transport.request("echo", value="after") == {"value": "after"}
    finally:
        transport.shutdown()


class _FakeGateway:
    def members(self):
        return {"sector": ("SH.600000",)}

    def tradable_instrument_codes(self, codes):
        return tuple(code for code in codes if code != "SH.000001")

    def screening_instrument_types(self, codes):
        return {
            code: "index_cn" if code == "SH.000001" else "stock_cn"
            for code in codes
        }


def test_worker_dispatch_is_a_strict_read_only_allowlist() -> None:
    gateway = _FakeGateway()
    assert dispatch_gateway_request(gateway, method="members", kwargs={}) == {
        "sector": ("SH.600000",)
    }
    assert dispatch_gateway_request(
        gateway,
        method="tradable_instrument_codes",
        kwargs={"codes": ("SH.000001", "SH.600000")},
    ) == ("SH.600000",)
    assert dispatch_gateway_request(
        gateway,
        method="screening_instrument_types",
        kwargs={"codes": ("SH.000001", "SH.600000")},
    ) == {"SH.000001": "index_cn", "SH.600000": "stock_cn"}
    for forbidden in ("order", "cancel_order", "account", "trader"):
        with pytest.raises(ValueError, match="not allowed"):
            dispatch_gateway_request(gateway, method=forbidden, kwargs={})


class _InstrumentScopeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def set_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def request(self, method: str, **kwargs: object) -> object:
        self.calls.append((method, kwargs))
        codes = kwargs["codes"]
        assert isinstance(codes, tuple)
        if method == "screening_instrument_types":
            return {
                code: "index_cn" if code == "SH.000001" else "stock_cn"
                for code in codes
            }
        assert method == "tradable_instrument_codes"
        return tuple(code for code in codes if code != "SH.000001")

    def health_snapshot(self):
        return {"ready": True}

    def shutdown(self) -> None:
        return None


class _BundleTransport:
    def __init__(self, bundle: SymbolStructureBundle) -> None:
        self.bundle = bundle
        self.calls: list[tuple[str, dict[str, object]]] = []

    def set_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def request(self, method: str, **kwargs: object) -> object:
        self.calls.append((method, kwargs))
        assert method == "structure_bundle"
        return self.bundle

    def health_snapshot(self):
        return {"ready": True}

    def shutdown(self) -> None:
        return None


def test_process_proxy_forwards_frozen_higher_timeframe_cutoff() -> None:
    as_of = datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai"))
    cutoff = as_of.replace(minute=45)
    sector = SectorAssessment(
        sector_id="TDX.880301",
        sector_name="鐓ょ偔",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("test_eligible",),
    )
    bundle = SymbolStructureBundle(
        code="SH.600000",
        as_of=as_of,
        sector=sector,
        thirty_direction="neutral",
        thirty_points=(),
        five_points=(),
        one_points=(),
        opposite_points=(),
    )
    transport = _BundleTransport(bundle)
    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport
    )
    proxy.restore_authenticated_sector_members(
        members={sector.sector_id: ("SH.600000",)},
        as_of=cutoff,
        catalog_revision="sha256:" + "7" * 64,
    )

    result = proxy.structure_bundle_with_risk_cutoff(
        "SH.600000",
        as_of=as_of,
        sector=sector,
        frequencies=("1m", "5m", "30m", "d"),
        risk_evidence_cutoff=cutoff,
    )

    assert result is bundle
    assert transport.calls == [
        (
            "structure_bundle",
            {
                "code": "SH.600000",
                "as_of": as_of,
                "sector": sector,
                "sector_members": ("SH.600000",),
                "frequencies": ("1m", "5m", "30m", "d"),
                "higher_timeframe_as_of": cutoff,
            },
        )
    ]


def test_process_proxy_filters_watchlist_and_holdings_through_native_qmt_type() -> None:
    transport = _InstrumentScopeTransport()
    proxy = NativeTradingDataGatewayProcessProxy(
        transport=transport,  # type: ignore[arg-type]
        watchlist_provider=lambda: (
            {"code": "SH.000001"},
            {"code": "SH.510300"},
        ),
        holdings_provider=lambda: ("SH.600000",),
    )

    assert proxy.active_watchlist() == ("SH.510300",)
    assert proxy.holdings() == ("SH.600000",)
    assert proxy.active_watchlist_scope() == (
        ("SH.510300",),
        ("SH.000001",),
    )
    assert proxy.screening_instrument_types(
        ("SH.000001", "SH.510300")
    ) == {"SH.000001": "index_cn", "SH.510300": "stock_cn"}
    assert transport.calls == [
        (
            "tradable_instrument_codes",
            {"codes": ("SH.000001", "SH.510300")},
        ),
        ("tradable_instrument_codes", {"codes": ("SH.600000",)}),
        (
            "tradable_instrument_codes",
            {"codes": ("SH.000001", "SH.510300")},
        ),
        (
            "screening_instrument_types",
            {"codes": ("SH.000001", "SH.510300")},
        ),
    ]


class _AtomicGateway:
    def __init__(self, *, as_of: datetime) -> None:
        self.as_of = as_of
        self.calls: list[str] = []
        self.assessment = SectorAssessment(
            sector_id="TDX.880301",
            sector_name="煤炭",
            eligible=True,
            hard_block=False,
            regime="neutral",
            rank_components=(("neutral_access", 5),),
            reason_codes=("test_eligible",),
        )

    def native_sector_assessments(self, *, as_of):
        assert as_of == self.as_of
        self.calls.append("assessments")
        return SectorAssessmentBatch(
            assessments=(self.assessment,),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        )

    def members(self):
        self.calls.append("members")
        return {self.assessment.sector_id: ("SH.600000",)}

    def changed_bars(self, since):
        assert since is None
        self.calls.append("changed_bars")
        return (BarKey(self.assessment.sector_id, "5m", self.as_of),)

    def symbol_name(self, code: str):
        assert code == "SH.600000"
        self.calls.append(f"symbol_name:{code}")
        return "浦发银行"
    def trading_session_evidence(self, *, session, observed_at):
        self.calls.append("trading_session_evidence")
        return build_trading_session_evidence(
            session=session,
            observed_at=observed_at,
            returned_sessions=(session,),
            published_through=session,
            query_attempted=True,
            query_succeeded=True,
        )


def test_worker_builds_one_atomic_sector_snapshot() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    gateway = _AtomicGateway(as_of=as_of)

    snapshot = dispatch_gateway_request(
        gateway,
        method="sector_snapshot",
        kwargs={"as_of": as_of},
    )

    assert snapshot["schema"] == "chanlun-native-sector-snapshot/v1"
    assert snapshot["members"] == {"TDX.880301": ("SH.600000",)}
    assert snapshot["changed_bars"] == (BarKey("TDX.880301", "5m", as_of),)
    assert snapshot["symbol_names"] == {}
    assert snapshot["minimum_market_data_frequency"] == "1m"
    assert snapshot["tick_data_used"] is False
    assert snapshot["real_account_access"] is False
    assert snapshot["real_order_transport"] is False
    assert gateway.calls == [
        "assessments",
        "members",
        "changed_bars",
    ]
    with pytest.raises(ValueError, match="requires exactly as_of"):
        dispatch_gateway_request(
            gateway,
            method="sector_snapshot",
            kwargs={"as_of": as_of, "unexpected": True},
        )

    calendar = dispatch_gateway_request(
        gateway,
        method="trading_session_evidence",
        kwargs={"session": as_of.date(), "observed_at": as_of},
    )
    assert calendar["classification"] == "TRADING_SESSION"
    assert calendar["tick_data_used"] is False
    assert gateway.calls[-1] == "trading_session_evidence"
    with pytest.raises(ValueError, match="requires session and observed_at"):
        dispatch_gateway_request(
            gateway,
            method="trading_session_evidence",
            kwargs={"session": as_of.date()},
        )


class _AtomicTransport:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.available = True

    def set_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def request(self, method: str, **kwargs: object) -> object:
        self.calls.append((method, kwargs))
        if not self.available:
            raise NativeScreeningWorkerUnavailable("simulated restarted worker")
        if method != "sector_snapshot":
            raise AssertionError(f"unexpected uncached request: {method}")
        return self.snapshot

    def health_snapshot(self):
        return {"ready": self.available}

    def shutdown(self) -> None:
        self.available = False


def _atomic_snapshot(as_of: datetime) -> dict[str, object]:
    assessment = SectorAssessment(
        sector_id="TDX.880301",
        sector_name="煤炭",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("test_eligible",),
    )
    return {
        "schema": "chanlun-native-sector-snapshot/v1",
        "assessments": SectorAssessmentBatch(
            assessments=(assessment,),
            discovered_count=1,
            completed_count=1,
            failure_counts=(),
            errors=(),
        ),
        "members": {assessment.sector_id: ("SH.600000",)},
        "changed_bars": (BarKey(assessment.sector_id, "5m", as_of),),
        "symbol_names": {},
        "minimum_market_data_frequency": "1m",
        "tick_data_used": False,
        "real_account_access": False,
        "real_order_transport": False,
    }


def _excluded_atomic_snapshot(as_of: datetime) -> dict[str, object]:
    snapshot = _atomic_snapshot(as_of)
    completed = snapshot["assessments"].assessments[0]
    exclusion = SectorAnalysisExclusion(
        sector_id="qmt-gics3:small",
        code="GICS3小板块",
        reason_code="sector_member_coverage_insufficient",
        reason="catalog_members=2; universe_members=2; required=3",
        detail_code="sector_constituent_count_below_minimum",
        catalog_member_count=2,
        universe_member_count=2,
        required_member_count=3,
    )
    excluded = SectorAssessment(
        sector_id=exclusion.sector_id,
        sector_name="小板块",
        eligible=False,
        hard_block=True,
        regime="hostile",
        rank_components=(),
        reason_codes=(exclusion.reason_code, exclusion.detail_code),
    )
    snapshot["assessments"] = SectorAssessmentBatch(
        assessments=(completed, excluded),
        discovered_count=2,
        completed_count=1,
        failure_counts=(),
        errors=(),
        exclusion_counts=(("sector_member_coverage_insufficient", 1),),
        exclusions=(exclusion,),
    )
    snapshot["members"] = {
        completed.sector_id: ("SH.600000",),
        excluded.sector_id: ("SH.600001", "SZ.000001"),
    }
    return snapshot


def _rich_atomic_snapshot(as_of: datetime) -> dict[str, object]:
    snapshot = _atomic_snapshot(as_of)
    context = TimeframeContext(
        frequency="30m",
        direction="up",
        disposition="supportive",
        hard_block=False,
        dominant_point_id="point-1",
        dominant_point_type="3buy",
        reason_codes=("higher_timeframe_supportive",),
        observed_at=as_of,
    )
    assessment = SectorAssessment(
        sector_id="TDX.880301",
        sector_name="煤炭",
        eligible=True,
        hard_block=False,
        regime="supportive",
        rank_components=(("supportive_access", 10),),
        reason_codes=("test_eligible",),
        thirty_context=context,
        horizontal_strength=Decimal("0.1250"),
        horizontal_rank=1,
        strength_anchor_session=as_of.date(),
        strength_member_count=1,
        strength_source_revision="sha256:" + "1" * 64,
        strength_reason_codes=("SECTOR_STRENGTH_RESOLVED",),
    )
    snapshot["assessments"] = SectorAssessmentBatch(
        assessments=(assessment,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
        catalog_revision="sha256:" + "7" * 64,
    )
    return snapshot


def _audited_atomic_snapshot(as_of: datetime) -> dict[str, object]:
    snapshot = _atomic_snapshot(as_of)
    original = snapshot["assessments"].assessments[0]
    evidence = build_horizontal_sector_strength_batch(
        decision_time=as_of,
        benchmark_symbol="SH.000300",
        benchmark_daily=(),
        members_by_sector={
            original.sector_id: (
                SectorMemberHistory(
                    "SH.600000",
                    as_of.date(),
                    "UNEXPLAINED_GAP",
                    (),
                ),
            )
        },
        membership_revision="sha256:" + "7" * 64,
    )
    strength = evidence[original.sector_id]
    assessment = SectorAssessment(
        sector_id=original.sector_id,
        sector_name=original.sector_name,
        eligible=original.eligible,
        hard_block=original.hard_block,
        regime=original.regime,
        rank_components=original.rank_components,
        reason_codes=original.reason_codes,
        horizontal_strength=strength.strength,
        horizontal_rank=strength.rank,
        strength_anchor_session=strength.anchor_session,
        strength_member_count=strength.member_count,
        strength_source_revision=strength.source_revision,
        strength_reason_codes=strength.reason_codes,
    )
    snapshot["assessments"] = SectorAssessmentBatch(
        assessments=(assessment,),
        discovered_count=1,
        completed_count=1,
        failure_counts=(),
        errors=(),
        catalog_revision="sha256:" + "7" * 64,
        strength_evidence=evidence,
    )
    return snapshot


class _CalendarTransport:
    def __init__(self, evidence: dict[str, object]) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.in_flight = False

    def set_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def request(self, method: str, **kwargs: object) -> object:
        self.calls.append((method, kwargs))
        assert method == "trading_session_evidence"
        return self.evidence

    def health_snapshot(self):
        return {"ready": True, "in_flight": self.in_flight}

    def shutdown(self) -> None:
        return None


def test_proxy_validates_and_caches_only_resolved_calendar_evidence() -> None:
    session = date(2026, 7, 30)
    observed = datetime(2026, 7, 30, 16, tzinfo=ZoneInfo("Asia/Shanghai"))
    evidence = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(session,),
        published_through=session,
        query_attempted=True,
        query_succeeded=True,
    )
    transport = _CalendarTransport(evidence)
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    first = proxy.trading_session_evidence(
        session=session,
        observed_at=observed,
    )
    second = proxy.trading_session_evidence(
        session=session,
        observed_at=observed,
    )

    assert first == second
    assert first["classification"] == "TRADING_SESSION"
    assert len(transport.calls) == 1


def test_proxy_requeries_unresolved_calendar_and_rejects_tampering() -> None:
    session = date(2026, 7, 31)
    observed = datetime(2026, 7, 31, 1, tzinfo=ZoneInfo("Asia/Shanghai"))
    unresolved = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(),
        published_through=date(2026, 7, 30),
        query_attempted=True,
        query_succeeded=True,
    )
    transport = _CalendarTransport(unresolved)
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    assert proxy.trading_session_evidence(
        session=session,
        observed_at=observed,
    )["classification"] == "UNRESOLVED"
    assert proxy.trading_session_evidence(
        session=session,
        observed_at=observed,
    )["classification"] == "UNRESOLVED"
    assert len(transport.calls) == 2

    forged = dict(unresolved)
    forged["classification"] = "NON_TRADING_SESSION"
    transport.evidence = forged
    with pytest.raises(NativeScreeningWorkerProtocolError):
        proxy.trading_session_evidence(
            session=session,
            observed_at=observed,
        )


def test_proxy_calendar_readiness_never_waits_behind_busy_native_screening(
) -> None:
    session = date(2026, 7, 31)
    observed = datetime(2026, 7, 31, 9, 11, tzinfo=ZoneInfo("Asia/Shanghai"))
    resolved = build_trading_session_evidence(
        session=session,
        observed_at=observed,
        returned_sessions=(session,),
        published_through=session,
        query_attempted=True,
        query_succeeded=True,
    )
    transport = _CalendarTransport(resolved)
    transport.in_flight = True
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    busy = proxy.trading_session_evidence(
        session=session,
        observed_at=observed,
    )

    assert busy["classification"] == "UNRESOLVED"
    assert busy["reason_code"] == "QMT_TRADING_CALENDAR_UNAVAILABLE"
    assert transport.calls == []

    transport.in_flight = False
    idle = proxy.trading_session_evidence(
        session=session,
        observed_at=observed,
    )
    assert idle["classification"] == "TRADING_SESSION"
    assert len(transport.calls) == 1


def test_proxy_keeps_atomic_sector_dependencies_after_worker_restart() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    transport = _AtomicTransport(_atomic_snapshot(as_of))
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    batch = proxy.native_sector_assessments(as_of=as_of)
    transport.available = False

    assert batch.completed_count == 1
    assert proxy.members() == {"TDX.880301": ("SH.600000",)}
    assert proxy.changed_bars(None) == (BarKey("TDX.880301", "5m", as_of),)
    assert proxy.changed_bars(None) == ()
    assert transport.calls == [("sector_snapshot", {"as_of": as_of})]


def test_proxy_restores_authenticated_member_routing_without_worker_call() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    transport = _AtomicTransport(_atomic_snapshot(as_of))
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    proxy.restore_authenticated_sector_members(
        members={"TDX.880301": ("SH.600000",)},
        as_of=as_of,
        catalog_revision="sha256:" + "7" * 64,
    )

    assert proxy.members() == {"TDX.880301": ("SH.600000",)}
    assert transport.calls == []
    cache = proxy.health_snapshot()["sector_snapshot_cache"]
    assert cache["state"] == "restored_from_screening_snapshot"
    assert cache["requested_as_of"] == as_of.isoformat()
    assert str(cache["content_sha256"]).startswith("sha256:")


def test_proxy_persists_and_reuses_same_revision_same_decision_snapshot(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    cache_path = tmp_path / "sector-snapshot.json"
    revision = "head.tree.abc123"
    first_transport = _AtomicTransport(_rich_atomic_snapshot(as_of))
    first = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=first_transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )

    expected = first.native_sector_assessments(as_of=as_of)
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    assert document["schema"] == "chanlun-native-sector-snapshot-cache/v2"
    assert document["content_sha256"] == sha256_json(document["payload"])
    assert document["payload"]["snapshot"]["assessments"][
        "catalog_revision"
    ] == ("sha256:" + "7" * 64)
    assert not tuple(tmp_path.glob(".sector-snapshot.json.*.tmp"))
    assert first.health_snapshot()["sector_snapshot_cache"]["state"] == "refreshed"

    second_transport = _AtomicTransport(_atomic_snapshot(as_of))
    second_transport.available = False
    second = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=second_transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    restored = second.native_sector_assessments(as_of=as_of)

    assert restored == expected
    assert restored.catalog_revision == "sha256:" + "7" * 64
    assert restored.assessments[0].horizontal_strength == Decimal("0.1250")
    assert restored.assessments[0].thirty_context is not None
    assert restored.assessments[0].thirty_context.observed_at == as_of
    assert second.members() == {"TDX.880301": ("SH.600000",)}
    assert second.changed_bars(None) == (BarKey("TDX.880301", "5m", as_of),)
    assert second_transport.calls == []
    cache_health = second.health_snapshot()["sector_snapshot_cache"]
    assert cache_health["state"] == "hit"
    assert cache_health["content_sha256"] == document["content_sha256"]


@pytest.mark.parametrize(
    ("cached_as_of", "requested_as_of"),
    (
        (
            datetime(2026, 7, 29, 15, 1, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2026, 7, 29, 21, 3, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
        (
            datetime(2026, 8, 2, 18, 38, tzinfo=ZoneInfo("Asia/Shanghai")),
            datetime(2026, 8, 2, 21, 3, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    ),
)
def test_proxy_reuses_snapshot_inside_same_causal_market_data_epoch(
    tmp_path: Path,
    cached_as_of: datetime,
    requested_as_of: datetime,
) -> None:
    """Wall-clock drift alone must not trigger another five-minute rebuild."""

    cache_path = tmp_path / "sector-snapshot.json"
    revision = "head.tree.same-market-data-epoch"
    first = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(_atomic_snapshot(cached_as_of)),
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    expected = first.native_sector_assessments(as_of=cached_as_of)

    transport = _AtomicTransport(_atomic_snapshot(requested_as_of))
    transport.available = False
    second = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )

    assert second.native_sector_assessments(as_of=requested_as_of) == expected
    assert transport.calls == []
    assert second.health_snapshot()["sector_snapshot_cache"]["state"] == "hit"


def test_proxy_cache_roundtrips_recomputable_sector_strength_evidence(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    cache_path = tmp_path / "sector-snapshot.json"
    revision = "head.tree.audited"
    first = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(_audited_atomic_snapshot(as_of)),
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    expected = first.native_sector_assessments(as_of=as_of)
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    cached = document["payload"]["snapshot"]["assessments"]
    assert cached["strength_evidence_revision"] == (
        expected.strength_evidence.evidence_revision
    )

    transport = _AtomicTransport(_atomic_snapshot(as_of))
    transport.available = False
    second = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    restored = second.native_sector_assessments(as_of=as_of)

    assert restored == expected
    assert restored.strength_evidence == expected.strength_evidence
    assert transport.calls == []


def test_proxy_cache_roundtrips_sector_eligibility_exclusions(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    cache_path = tmp_path / "sector-snapshot.json"
    revision = "head.tree.sector-exclusion"
    first = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(_excluded_atomic_snapshot(as_of)),
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    expected = first.native_sector_assessments(as_of=as_of)
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    cached = document["payload"]["snapshot"]["assessments"]
    assert cached["exclusion_counts"] == [
        ["sector_member_coverage_insufficient", 1]
    ]
    assert cached["exclusions"][0]["required_member_count"] == 3

    transport = _AtomicTransport(_atomic_snapshot(as_of))
    transport.available = False
    second = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    restored = second.native_sector_assessments(as_of=as_of)

    assert restored == expected
    assert restored.errors == ()
    assert restored.exclusion_counts == (
        ("sector_member_coverage_insufficient", 1),
    )
    assert restored.exclusions[0].detail_code == (
        "sector_constituent_count_below_minimum"
    )
    assert transport.calls == []


def test_proxy_rejects_tampered_sector_cache_and_refreshes_from_worker(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    cache_path = tmp_path / "sector-snapshot.json"
    revision = "head.tree.abc123"
    first = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(_atomic_snapshot(as_of)),
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    first.native_sector_assessments(as_of=as_of)
    tampered = json.loads(cache_path.read_text(encoding="utf-8"))
    tampered["payload"]["snapshot"]["members"]["TDX.880301"] = ["SH.600001"]
    cache_path.write_text(json.dumps(tampered), encoding="utf-8")

    transport = _AtomicTransport(_atomic_snapshot(as_of))
    second = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    second.native_sector_assessments(as_of=as_of)

    assert transport.calls == [("sector_snapshot", {"as_of": as_of})]
    repaired = json.loads(cache_path.read_text(encoding="utf-8"))
    assert repaired["content_sha256"] == sha256_json(repaired["payload"])


def test_proxy_rejects_rehashed_cache_with_future_market_fact(
    tmp_path: Path,
) -> None:
    as_of = datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    cache_path = tmp_path / "sector-snapshot.json"
    revision = "head.tree.abc123"
    first = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(_atomic_snapshot(as_of)),
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    first.native_sector_assessments(as_of=as_of)
    forged = json.loads(cache_path.read_text(encoding="utf-8"))
    forged["payload"]["snapshot"]["changed_bars"][0]["closed_at"] = (
        "2026-07-29T15:05:00+08:00"
    )
    forged["content_sha256"] = sha256_json(forged["payload"])
    cache_path.write_text(json.dumps(forged), encoding="utf-8")

    transport = _AtomicTransport(_atomic_snapshot(as_of))
    second = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    second.native_sector_assessments(as_of=as_of)

    assert transport.calls == [("sector_snapshot", {"as_of": as_of})]


@pytest.mark.parametrize(
    ("requested_as_of", "revision"),
    (
        (
            datetime(2026, 7, 29, 14, 55, tzinfo=ZoneInfo("Asia/Shanghai")),
            "head.tree.abc123",
        ),
        (
            datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            "head.tree.changed",
        ),
        (
            datetime(2026, 7, 30, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
            "head.tree.abc123",
        ),
    ),
)
def test_proxy_rejects_wrong_time_or_source_revision_cache(
    tmp_path: Path,
    requested_as_of: datetime,
    revision: str,
) -> None:
    cached_as_of = datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    cache_path = tmp_path / "sector-snapshot.json"
    first = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(_atomic_snapshot(cached_as_of)),
        sector_cache_path=cache_path,
        sector_cache_revision="head.tree.abc123",
    )
    first.native_sector_assessments(as_of=cached_as_of)

    transport = _AtomicTransport(_atomic_snapshot(requested_as_of))
    second = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        sector_cache_path=cache_path,
        sector_cache_revision=revision,
    )
    second.native_sector_assessments(as_of=requested_as_of)

    assert transport.calls == [
        ("sector_snapshot", {"as_of": requested_as_of})
    ]


def test_proxy_rejects_live_sector_snapshot_with_future_bar() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = _atomic_snapshot(as_of)
    snapshot["changed_bars"] = (
        BarKey(
            "TDX.880301",
            "5m",
            datetime(2026, 7, 29, 10, 40, tzinfo=ZoneInfo("Asia/Shanghai")),
        ),
    )
    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(snapshot)
    )

    with pytest.raises(NativeScreeningWorkerProtocolError, match="causality"):
        proxy.native_sector_assessments(as_of=as_of)


def test_proxy_rejects_atomic_snapshot_that_claims_account_access() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = _atomic_snapshot(as_of)
    snapshot["real_account_access"] = True
    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(snapshot)
    )

    with pytest.raises(NativeScreeningWorkerProtocolError, match="safety boundary"):
        proxy.native_sector_assessments(as_of=as_of)


def test_proxy_rejects_atomic_snapshot_that_claims_tick_data() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    snapshot = _atomic_snapshot(as_of)
    snapshot["tick_data_used"] = True
    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=_AtomicTransport(snapshot)
    )

    with pytest.raises(NativeScreeningWorkerProtocolError, match="safety boundary"):
        proxy.native_sector_assessments(as_of=as_of)


def test_app_factory_selects_isolated_gateway_when_explicitly_enabled() -> None:
    app = create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": False,
            "TRADING_SCREENING_NATIVE_PROCESS_ISOLATION": True,
        },
    )
    gateway = app.extensions["decision_support_trading_screening_gateway"]
    assert gateway.__class__.__name__ == "NativeTradingDataGatewayProcessProxy"
    assert gateway.health_snapshot()["worker_alive"] is False


def test_app_default_holdings_provider_reads_the_virtual_paper_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": False,
            "TRADING_SCREENING_NATIVE_PROCESS_ISOLATION": True,
        },
    )
    human_review = app.extensions["decision_support_human_review"]
    monkeypatch.setattr(
        human_review,
        "virtual_holding_codes",
        lambda: ("SH.600000", "SZ.000001"),
    )

    gateway = app.extensions["decision_support_trading_screening_gateway"]
    # This test owns only the app-to-virtual-ledger binding.  Native QMT
    # instrument classification has its own contract tests and must not turn
    # this unit test into an external-service probe on CI.
    monkeypatch.setattr(
        gateway,
        "tradable_instrument_codes",
        lambda codes: tuple(codes),
    )
    assert gateway.holdings() == ("SH.600000", "SZ.000001")


def test_app_factory_binds_sector_cache_to_semantic_producer_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CHANLUN_BUILD_REVISION",
        "head.tree.abc123.run.0123456789abcdef",
    )
    app = create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": False,
            "TRADING_SCREENING_NATIVE_PROCESS_ISOLATION": True,
        },
    )

    gateway = app.extensions["decision_support_trading_screening_gateway"]
    cache_health = gateway.health_snapshot()["sector_snapshot_cache"]
    assert cache_health["enabled"] is True
    assert cache_health["state"] == "not_checked"
    assert cache_health["source_revision"] == (
        native_sector_snapshot_producer_revision()
    )


def test_app_default_screening_parallelism_is_bounded_and_tunable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHANLUN_TRADING_SCREENING_STOCK_WORKERS", raising=False)
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_PRIORITY_MONITOR_MAX_SYMBOLS",
        raising=False,
    )
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH",
        raising=False,
    )
    app = create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": False,
            "TRADING_SCREENING_NATIVE_PROCESS_ISOLATION": True,
        },
    )

    expected_workers = min(
        10,
        max(1, (((os.cpu_count() or 4) * 5) + 7) // 8),
    )
    assert app.config["TRADING_SCREENING_STOCK_WORKERS"] == expected_workers
    assert app.config["TRADING_SCREENING_PRIORITY_MONITOR_MAX_SYMBOLS"] == 16
    assert app.config["TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH"] == 64
    gateway = app.extensions["decision_support_trading_screening_gateway"]
    assert len(gateway._structure_transports) == expected_workers  # noqa: SLF001


def test_direct_app_launch_uses_content_addressed_revision_for_worker_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CHANLUN_BUILD_REVISION", raising=False)
    app = create_app(
        start_scheduler=False,
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "WTF_CSRF_ENABLED": False,
            "TRADING_SCREENING_BACKGROUND_ENABLED": False,
            "TRADING_SCREENING_NATIVE_PROCESS_ISOLATION": True,
        },
    )

    revision = app.test_client().get("/livez").get_json()["revision"]
    assert len(revision.split(".tree.")) == 2
    assert len(revision.split(".tree.")[0]) == 40
    gateway = app.extensions["decision_support_trading_screening_gateway"]
    assert gateway.health_snapshot()["sector_snapshot_cache"]["enabled"] is True
    assert gateway._transport._environment == {  # noqa: SLF001
        "CHANLUN_BUILD_REVISION": revision
    }
