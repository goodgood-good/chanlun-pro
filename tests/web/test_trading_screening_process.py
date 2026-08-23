from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import sys
from threading import Event, Thread
import time
from zoneinfo import ZoneInfo

import pytest

import cl_app.services.trading_screening_process as screening_process_subject

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
from chanlun.decision_support.trading_system.selection import (
    SectorMemberHistory,
)
from chanlun.decision_support.trading_system.trading_session import (
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
from cl_app.services.realtime_quotes import (
    AShareDisplayQuoteBatch,
    AShareInstrumentSessionStatus,
    AShareInstrumentSessionStatusBatch,
    AShareRealtimeQuote,
    AShareRealtimeQuoteBatch,
)
from cl_app.services.trading_screening_process import (
    NativeScreeningWorkerDeadlineExceeded,
    NativeScreeningWorkerProtocolError,
    NativeScreeningWorkerRemoteError,
    NativeScreeningWorkerTimeout,
    NativeScreeningWorkerUnavailable,
    NativeTradingDataGatewayProcessProxy,
    NativeWorkerProcessConfig,
    NativeWorkerProcessTransport,
    native_sector_snapshot_cache_revision,
    native_sector_snapshot_producer_revision,
    runtime_state_cache_producer_revision,
)
from cl_app import create_app


FIXTURE = Path(__file__).parent / "fixtures" / "native_screening_test_worker.py"


def test_native_worker_default_startup_budget_covers_cold_windows_handshake() -> None:
    config = NativeWorkerProcessConfig()

    assert config.startup_timeout_seconds >= 120
    assert config.native_idle_timeout_seconds == 210


def test_runtime_state_cache_cleanup_removes_only_dead_owned_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "runtime-cache"
    dead = parent / "web-123-0123456789abcdef"
    alive = parent / "web-456-fedcba9876543210"
    unrelated = parent / "manual-data"
    for path in (dead, alive, unrelated):
        path.mkdir(parents=True)
        (path / "marker.txt").write_text("keep boundary explicit", encoding="utf-8")
    monkeypatch.setattr(
        screening_process_subject,
        "_process_exists",
        lambda pid: pid == 456,
    )

    screening_process_subject._cleanup_stale_runtime_state_cache_roots(parent)

    assert not dead.exists()
    assert alive.exists()
    assert unrelated.exists()


def test_runtime_state_cache_cleanup_reclaims_only_unleased_persistent_versions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    parent = tmp_path / "runtime-cache"
    current = parent / f"runtime-{'a' * 24}"
    live = parent / f"runtime-{'b' * 24}"
    dead = parent / f"runtime-{'c' * 24}"
    stale = parent / f"runtime-{'d' * 24}"
    unrelated = parent / "runtime-manual-data"
    for path in (current, live, dead, stale, unrelated):
        path.mkdir(parents=True)
        (path / "marker.txt").write_text("cache payload", encoding="utf-8")
    live_owner = parent / f".{live.name}.owner-456-{'1' * 16}"
    dead_owner = parent / f".{dead.name}.owner-123-{'2' * 16}"
    live_owner.touch()
    dead_owner.touch()
    monkeypatch.setattr(
        screening_process_subject,
        "_process_exists",
        lambda pid: pid == 456,
    )

    screening_process_subject._cleanup_stale_runtime_state_cache_roots(
        parent,
        current_root=current,
    )

    assert current.exists()
    assert live.exists()
    assert live_owner.exists()
    assert not dead.exists()
    assert not dead_owner.exists()
    assert not stale.exists()
    assert unrelated.exists()


def test_runtime_state_cache_producer_ignores_decision_only_changes(
    tmp_path: Path,
) -> None:
    producer_paths = (
        "src/chanlun/core/cl.py",
        "src/chanlun/decision_support/fingerprints.py",
        "src/chanlun/decision_support/trading_system/runtime_config.py",
        "src/chanlun/decision_support/trading_system/screening_runtime.py",
        "src/chanlun/decision_support/trading_system/screening_structure.py",
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py",
    )
    for relative in producer_paths:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")
    decision_only = (
        tmp_path
        / "src/chanlun/decision_support/trading_system/higher_timeframe_gate.py"
    )
    decision_only.write_text("# decision v1\n", encoding="utf-8")

    initial = runtime_state_cache_producer_revision(project_root=tmp_path)
    decision_only.write_text("# decision v2\n", encoding="utf-8")
    after_decision_change = runtime_state_cache_producer_revision(
        project_root=tmp_path
    )
    runtime_source = (
        tmp_path
        / "src/chanlun/decision_support/trading_system/screening_runtime.py"
    )
    runtime_source.write_text("# runtime v2\n", encoding="utf-8")
    after_runtime_change = runtime_state_cache_producer_revision(project_root=tmp_path)

    assert initial == after_decision_change
    assert after_runtime_change != initial


def test_runtime_state_cache_is_stable_only_for_same_producer_revision(
    tmp_path: Path,
) -> None:
    first_revision = "sha256:" + "a" * 64
    second_revision = "sha256:" + "b" * 64
    secret = b"stable-production-secret-material"

    first = screening_process_subject._runtime_state_cache_settings(
        parent=tmp_path,
        expected_runtime_state_producer_revision=first_revision,
        persistent_secret=secret,
    )
    restarted = screening_process_subject._runtime_state_cache_settings(
        parent=tmp_path,
        expected_runtime_state_producer_revision=first_revision,
        persistent_secret=secret,
    )
    changed_source = screening_process_subject._runtime_state_cache_settings(
        parent=tmp_path,
        expected_runtime_state_producer_revision=second_revision,
        persistent_secret=secret,
    )

    assert first == restarted
    assert first.scope == "runtime_state_producer_revision"
    assert first.delete_on_close is False
    assert first.root.name.startswith("runtime-")
    assert len(first.key_hex) == 64
    assert changed_source.root != first.root
    assert changed_source.key_hex != first.key_hex
    assert changed_source.identity != first.identity


def test_runtime_state_cache_without_persistent_secret_remains_web_scoped(
    tmp_path: Path,
) -> None:
    revision = "sha256:" + "a" * 64

    settings = screening_process_subject._runtime_state_cache_settings(
        parent=tmp_path,
        expected_runtime_state_producer_revision=revision,
        persistent_secret=None,
    )

    assert settings.scope == "web_lifecycle"
    assert settings.delete_on_close is True
    assert settings.root.name.startswith(f"web-{os.getpid()}-")
    assert settings.identity == settings.root.name


def test_process_proxy_provisions_stable_runtime_scoped_candidate_cache(
    tmp_path: Path,
) -> None:
    revision = "a" * 40 + ".tree." + "b" * 24
    secret = b"stable-production-secret-material"
    producer_revision = runtime_state_cache_producer_revision()

    first = NativeTradingDataGatewayProcessProxy(
        log_path=tmp_path / "native-worker.log",
        structure_worker_count=3,
        expected_application_source_revision=revision,
        runtime_state_cache_secret=secret,
    )
    first_candidates = first._structure_transports[1:]  # noqa: SLF001
    first_environments = [
        dict(transport._environment or {})  # noqa: SLF001
        for transport in first_candidates
    ]
    first_root = first._runtime_state_cache_root  # noqa: SLF001
    first_owner = first._runtime_state_cache_owner_marker  # noqa: SLF001
    assert first_root is not None
    assert first_owner is not None and first_owner.exists()
    first_root.mkdir(parents=True)
    (first_root / "keep.marker").write_text("persistent", encoding="utf-8")
    first.close()
    assert not first_owner.exists()

    second = NativeTradingDataGatewayProcessProxy(
        log_path=tmp_path / "native-worker.log",
        structure_worker_count=3,
        expected_application_source_revision=revision,
        runtime_state_cache_secret=secret,
    )
    second_environments = [
        dict(transport._environment or {})  # noqa: SLF001
        for transport in second._structure_transports[1:]  # noqa: SLF001
    ]
    second_owner = second._runtime_state_cache_owner_marker  # noqa: SLF001
    try:
        assert second_owner is not None and second_owner.exists()
        assert first_root.exists()
        assert (first_root / "keep.marker").exists()
        assert second._runtime_state_cache_root == first_root  # noqa: SLF001
        assert [
            environment["CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_KEY"]
            for environment in first_environments
        ] == [
            environment["CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_KEY"]
            for environment in second_environments
        ]
        assert all(
            environment["CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_IDENTITY"]
            == producer_revision
            and environment["CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_SCOPE"]
            == "runtime_state_producer_revision"
            for environment in second_environments
        )
    finally:
        second.close()
    assert second_owner is not None and not second_owner.exists()


def test_native_qmt_fact_cache_is_official_launch_only(tmp_path: Path) -> None:
    content_revision = "a" * 40 + ".tree." + "b" * 24
    deployment_revision = content_revision + ".run." + "c" * 32

    assert _qmt_fact_cache_settings(
        build_revision="manual-head",
        data_path=tmp_path,
    ) == (None, None, None, None, None)
    assert _qmt_fact_cache_settings(
        build_revision="head.tree.abc.run.0123456789abcdef",
        data_path=tmp_path,
    ) == (None, None, None, None, None)

    composite, daily, status, composite_revision, daily_revision = (
        _qmt_fact_cache_settings(
            build_revision=content_revision,
            data_path=tmp_path,
        )
    )
    assert composite == (
        tmp_path.resolve() / "decision_support" / "trading_screening_sector_frame_facts"
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
    deployed = _qmt_fact_cache_settings(
        build_revision=deployment_revision,
        data_path=tmp_path,
    )
    from chanlun.exchange.qmt_screening_sector_source import (
        qmt_sector_composite_fact_producer_revision,
        qmt_sector_daily_fact_producer_revision,
    )

    assert deployed[:3] == (composite, daily, status)
    assert deployed[-2:] == (composite_revision, daily_revision)
    assert composite_revision == qmt_sector_composite_fact_producer_revision()
    assert daily_revision == qmt_sector_daily_fact_producer_revision()
    assert composite_revision != daily_revision


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
    (static / "screen.js").write_text("const label = 'current';\n", encoding="utf-8")
    assert native_sector_snapshot_producer_revision(project_root=tmp_path) == first
    content_revision = "a" * 40 + ".tree." + "b" * 24
    assert native_sector_snapshot_cache_revision(
        content_revision + ".run." + "c" * 32,
        project_root=tmp_path,
    ) == native_sector_snapshot_cache_revision(
        content_revision + ".run." + "d" * 32,
        project_root=tmp_path,
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
    content_revision = "a" * 40 + ".tree." + "b" * 24
    deployment_revision = content_revision + ".run." + "c" * 32
    assert (
        native_sector_snapshot_cache_revision("manual-head", project_root=tmp_path)
        is None
    )
    assert (
        native_sector_snapshot_cache_revision(
            "head.tree.source.run.official", project_root=tmp_path
        )
        is None
    )
    assert native_sector_snapshot_cache_revision(
        content_revision,
        project_root=tmp_path,
    ) == native_sector_snapshot_producer_revision(project_root=tmp_path)
    assert native_sector_snapshot_cache_revision(
        deployment_revision,
        project_root=tmp_path,
    ) == native_sector_snapshot_producer_revision(project_root=tmp_path)


def _transport(
    tmp_path: Path,
    *,
    idle: float = 1.0,
    backoff: float = 0.05,
    max_requests: int = 256,
    progress=lambda: None,
) -> NativeWorkerProcessTransport:
    return NativeWorkerProcessTransport(
        log_path=tmp_path / "native-worker.log",
        worker_command=(sys.executable, str(FIXTURE)),
        config=NativeWorkerProcessConfig(
            startup_timeout_seconds=3.0,
            native_idle_timeout_seconds=idle,
            restart_backoff_seconds=backoff,
            max_completed_requests_per_process=max_requests,
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
        assert health["market_data_probe"] == {
            "schema": "chanlun-qmt-market-data-readiness",
            "ready": True,
            "probe_code": "SH.600000",
            "provider": "QMT_XTDATA",
            "real_account_access": False,
            "real_order_transport": False,
        }
        assert health["minimum_market_data_frequency"] == "1m"
        assert health["tick_data_used"] is False
        assert health["real_account_access"] is False
        assert health["real_order_transport"] is False
        assert health["restart_count"] == 1
        assert health["completed_request_count"] == 1
        assert health["total_completed_request_count"] == 1
        assert health["max_completed_requests_per_process"] == 256
        assert health["max_worker_rss_bytes"] == 1536 * 1024 * 1024
        assert health["worker_rss_bytes"] is None or health["worker_rss_bytes"] > 0
        assert progress == ["tick"]
    finally:
        transport.shutdown()
    assert transport.health_snapshot()["worker_alive"] is False


def test_worker_recycles_at_completed_request_boundary_without_backoff(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path, max_requests=2)
    try:
        assert transport.request("echo", value=1) == {"value": 1}
        first_pid = transport.health_snapshot()["worker_pid"]
        assert transport.request("echo", value=2) == {"value": 2}

        recycled = transport.health_snapshot()
        assert recycled["worker_alive"] is False
        assert recycled["completed_request_count"] == 2
        assert recycled["total_completed_request_count"] == 2
        assert recycled["recycle_count"] == 1
        assert recycled["failure_count"] == 0
        assert recycled["restart_backoff_remaining_seconds"] == 0.0
        assert str(recycled["last_recycle_reason"]).startswith(
            "worker_request_limit_reached:"
        )

        assert transport.request("echo", value=3) == {"value": 3}
        restarted = transport.health_snapshot()
        assert restarted["worker_alive"] is True
        assert restarted["worker_pid"] != first_pid
        assert restarted["restart_count"] == 2
        assert restarted["completed_request_count"] == 1
        assert restarted["total_completed_request_count"] == 3
    finally:
        transport.shutdown()


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


def test_worker_startup_fails_closed_when_source_revision_is_missing(
    tmp_path: Path,
) -> None:
    expected = "a" * 40 + ".tree." + "b" * 24
    transport = NativeWorkerProcessTransport(
        log_path=tmp_path / "native-worker-revision.log",
        worker_command=(sys.executable, str(FIXTURE)),
        expected_application_source_revision=expected,
        config=NativeWorkerProcessConfig(
            startup_timeout_seconds=3.0,
            native_idle_timeout_seconds=1.0,
            restart_backoff_seconds=0.05,
        ),
    )
    try:
        with pytest.raises(
            NativeScreeningWorkerProtocolError,
            match="source revision mismatch",
        ):
            transport.startup()
        health = transport.health_snapshot()
        assert health["ready"] is False
        assert health["expected_application_source_revision"] == expected
        assert health["worker_application_source_revision"] is None
    finally:
        transport.shutdown()


@pytest.mark.parametrize("probe_mode", ["missing", "not_ready"])
def test_worker_startup_rejects_missing_or_failed_market_data_probe(
    tmp_path: Path,
    probe_mode: str,
) -> None:
    """原生进程只有真实行情 RPC 通过后才能进入 ready。"""

    transport = NativeWorkerProcessTransport(
        log_path=tmp_path / f"native-worker-probe-{probe_mode}.log",
        worker_command=(sys.executable, str(FIXTURE)),
        environment={"NATIVE_TEST_MARKET_DATA_PROBE": probe_mode},
        config=NativeWorkerProcessConfig(
            startup_timeout_seconds=3.0,
            native_idle_timeout_seconds=1.0,
            restart_backoff_seconds=0.05,
        ),
    )
    try:
        with pytest.raises(
            NativeScreeningWorkerProtocolError,
            match="invalid safety handshake",
        ):
            transport.startup()
        health = transport.health_snapshot()
        assert health["ready"] is False
        assert health["worker_alive"] is False
        assert health["market_data_probe"] is None
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


def test_native_absolute_deadline_kills_worker_without_restart_backoff(
    tmp_path: Path,
) -> None:
    """低频预算到期应回收分片，但不能阻止下一分钟立即恢复。"""

    transport = _transport(tmp_path, idle=2.0, backoff=5.0)
    started = time.monotonic()
    try:
        with pytest.raises(
            NativeScreeningWorkerDeadlineExceeded,
            match="request deadline",
        ):
            transport.request_until(
                "hang",
                deadline_monotonic=time.monotonic() + 0.15,
            )
        assert time.monotonic() - started < 1.0
        health = transport.health_snapshot()
        assert health["worker_alive"] is False
        assert health["failure_count"] == 0
        assert health["restart_backoff_remaining_seconds"] == 0.0

        assert transport.request("echo", value="next-minute") == {
            "value": "next-minute"
        }
        assert transport.health_snapshot()["ready"] is True
    finally:
        transport.shutdown()


def test_native_nowait_request_never_queues_behind_busy_worker(tmp_path: Path) -> None:
    transport = _transport(tmp_path)
    transport._request_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(NativeScreeningWorkerUnavailable, match="not queued"):
            transport.request_nowait("echo", value="later")
        assert time.monotonic() - started < 0.1
        assert transport.health_snapshot()["worker_alive"] is False
    finally:
        transport._request_lock.release()
        transport.shutdown()


def test_native_bounded_queue_wait_fails_without_stacking_requests(
    tmp_path: Path,
) -> None:
    transport = _transport(tmp_path)
    transport._request_lock.acquire()
    started = time.monotonic()
    try:
        with pytest.raises(NativeScreeningWorkerUnavailable, match="bounded queue"):
            transport.request_when_available(
                "echo",
                max_wait_seconds=0.05,
                value="later",
            )
        assert 0.04 <= time.monotonic() - started < 0.5
        assert transport.health_snapshot()["worker_alive"] is False
    finally:
        transport._request_lock.release()
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

    def realtime_ticks(self, codes):
        return {"codes": codes}

    def display_quote_snapshot(self, codes):
        return {"display_codes": codes}

    def current_session_instrument_statuses(self, codes, *, session):
        return {"status_codes": codes, "session": session}


def test_worker_dispatch_is_a_strict_read_only_allowlist() -> None:
    gateway = _FakeGateway()
    assert dispatch_gateway_request(gateway, method="members", kwargs={}) == {
        "sector": ("SH.600000",)
    }
    assert dispatch_gateway_request(
        gateway,
        method="realtime_ticks",
        kwargs={"codes": ("SH.600000",)},
    ) == {"codes": ("SH.600000",)}
    assert dispatch_gateway_request(
        gateway,
        method="display_quote_snapshot",
        kwargs={"codes": ("SH.600000",)},
    ) == {"display_codes": ("SH.600000",)}
    assert dispatch_gateway_request(
        gateway,
        method="current_session_instrument_statuses",
        kwargs={"codes": ("SH.600000",), "session": date(2026, 7, 20)},
    ) == {
        "status_codes": ("SH.600000",),
        "session": date(2026, 7, 20),
    }
    for forbidden in (
        "tick_probe",
        "screening_instrument_types",
        "tradable_instrument_codes",
        "order",
        "cancel_order",
        "account",
        "trader",
    ):
        with pytest.raises(ValueError, match="not allowed"):
            dispatch_gateway_request(gateway, method=forbidden, kwargs={})


class _InstrumentScopeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def set_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def request(self, method: str, **kwargs: object) -> object:
        self.calls.append((method, kwargs))
        raise AssertionError("证券类型目录不得通过原生工作进程读取")

    def health_snapshot(self):
        return {"ready": True}

    def shutdown(self) -> None:
        return None


class _InstrumentTypeCatalog:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, codes: tuple[str, ...]) -> dict[str, str]:
        self.calls.append(codes)
        return {
            code: (
                "index_cn"
                if code == "SH.000001"
                else "etf_cn"
                if code == "SH.510300"
                else "stock_cn"
            )
            for code in codes
        }


def test_process_proxy_uses_in_process_symbol_catalog_without_native_io() -> None:
    transport = _InstrumentScopeTransport()
    calls: list[tuple[str, ...]] = []

    def names(codes: tuple[str, ...]) -> dict[str, str | None]:
        calls.append(codes)
        return {code: "纳指ETF" for code in codes}

    proxy = NativeTradingDataGatewayProcessProxy(
        transport=transport,  # type: ignore[arg-type]
        symbol_name_provider=names,
    )

    assert proxy.symbol_name("SH.513100") == "纳指ETF"
    assert proxy.symbol_name("SH.513100") == "纳指ETF"
    assert calls == [("SH.513100",)]
    assert transport.calls == []


class _RealtimeTickTransport:
    def __init__(self, result: object) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, object]]] = []

    def set_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def request_nowait(self, method: str, **kwargs: object) -> object:
        self.calls.append((method, kwargs))
        return self.result

    def request_when_available(
        self,
        method: str,
        *,
        max_wait_seconds: float,
        **kwargs: object,
    ) -> object:
        assert max_wait_seconds == 2.0
        return self.request_nowait(method, **kwargs)

    def health_snapshot(self):
        return {"ready": True}

    def shutdown(self) -> None:
        return None


def test_process_proxy_validates_isolated_tick_probe() -> None:
    batch = AShareRealtimeQuoteBatch(
        requested_codes=("SH.000001",),
        market_open=False,
        quotes=(),
        tick_data_used=False,
    )
    transport = _RealtimeTickTransport(batch)
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    assert proxy.tick_probe("SH.000001") == {
        "schema": "chanlun-native-tick-probe",
        "code": "SH.000001",
        "status": "market_closed",
        "market_open": False,
        "usable": False,
        "tick_data_used": False,
        "real_account_access": False,
        "real_order_transport": False,
    }
    assert transport.calls == [
        ("realtime_ticks", {"codes": ("SH.000001",)})
    ]

    transport.result = AShareRealtimeQuoteBatch(
        requested_codes=("SZ.000001",),
        market_open=True,
        quotes=(
            AShareRealtimeQuote(
                code="SZ.000001",
                last=1.0,
                buy1=1.0,
                sell1=1.0,
                high=1.0,
                low=1.0,
                open=1.0,
                volume=1.0,
                rate=0.0,
            ),
        ),
        tick_data_used=True,
    )
    with pytest.raises(NativeScreeningWorkerProtocolError):
        proxy.tick_probe("SH.000001")


def test_process_proxy_routes_mandatory_quote_to_reserved_priority_worker() -> None:
    batch = AShareRealtimeQuoteBatch(
        requested_codes=("SH.513100",),
        market_open=True,
        quotes=(
            AShareRealtimeQuote(
                code="SH.513100",
                last=2.264,
                buy1=0.0,
                sell1=0.0,
                high=0.0,
                low=0.0,
                open=0.0,
                volume=0.0,
                rate=0.0,
            ),
        ),
        tick_data_used=True,
    )
    shared = _RealtimeTickTransport(batch)
    priority = _RealtimeTickTransport(batch)
    candidate = _RealtimeTickTransport(batch)
    proxy = NativeTradingDataGatewayProcessProxy(transport=shared)  # type: ignore[arg-type]
    proxy._structure_transports = (priority, candidate)  # type: ignore[assignment]  # noqa: SLF001

    result = proxy.priority_realtime_ticks(("SH.513100",))

    assert result == batch
    assert shared.calls == []
    assert priority.calls == [
        ("realtime_ticks", {"codes": ("SH.513100",)})
    ]
    assert candidate.calls == []

    status_batch = AShareInstrumentSessionStatusBatch(
        requested_codes=("SH.513100",),
        session=date(2026, 7, 20),
        facts=(
            AShareInstrumentSessionStatus(
                code="SH.513100",
                trading_day=date(2026, 7, 20),
                instrument_name="纳指ETF",
                instrument_status=2,
                is_trading=False,
            ),
        ),
    )
    priority.result = status_batch

    status_result = proxy.priority_current_session_instrument_statuses(
        ("SH.513100",),
        session=date(2026, 7, 20),
    )

    assert status_result == status_batch
    assert priority.calls[-1] == (
        "current_session_instrument_statuses",
        {"codes": ("SH.513100",), "session": date(2026, 7, 20)},
    )


def test_process_proxy_reads_coverage_instrument_status_from_control_worker() -> None:
    batch = AShareInstrumentSessionStatusBatch(
        requested_codes=("SZ.000001",),
        session=date(2026, 7, 20),
        facts=(
            AShareInstrumentSessionStatus(
                code="SZ.000001",
                trading_day=date(2026, 7, 20),
                instrument_name="平安银行",
                instrument_status=2,
                is_trading=False,
            ),
        ),
    )
    transport = _RealtimeTickTransport(batch)
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    result = proxy.current_session_instrument_statuses(
        ("SZ.000001",),
        session=date(2026, 7, 20),
    )

    assert result == batch
    assert transport.calls == [
        (
            "current_session_instrument_statuses",
            {"codes": ("SZ.000001",), "session": date(2026, 7, 20)},
        )
    ]


def test_process_proxy_preserves_closed_market_display_quote_snapshot() -> None:
    batch = AShareDisplayQuoteBatch(
        requested_codes=("SH.513100",),
        market_open=False,
        quotes=(
            AShareRealtimeQuote(
                code="SH.513100",
                last=1.672,
                buy1=1.671,
                sell1=1.672,
                high=1.684,
                low=1.655,
                open=1.66,
                volume=1000.0,
                rate=0.72,
            ),
        ),
        tick_data_used=True,
    )
    transport = _RealtimeTickTransport(batch)
    proxy = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]

    result = proxy.display_quote_snapshot(("SH.513100",))

    assert result.market_open is False
    assert result.ticks()["SH.513100"].last == 1.672
    assert transport.calls == [
        ("display_quote_snapshot", {"codes": ("SH.513100",)})
    ]


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

    def request_until(
        self,
        method: str,
        *,
        deadline_monotonic: float,
        **kwargs: object,
    ) -> object:
        return self.request(
            method,
            deadline_monotonic=deadline_monotonic,
            **kwargs,
        )

    def health_snapshot(self):
        return {"ready": True}

    def shutdown(self) -> None:
        return None


class _HistoryPreparationTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def set_progress_callback(self, callback) -> None:
        self.progress_callback = callback

    def request(self, method: str, **kwargs: object) -> object:
        self.calls.append((method, kwargs))
        assert method == "prepare_local_history"
        requests = kwargs["frequency_requests"]
        assert type(requests) is tuple
        return {
            "schema": "chanlun-screening-local-history-preparation",
            "as_of": kwargs["as_of"].isoformat(),
            "prepared_frequencies_by_code": dict(requests),
            "batch_download_available": True,
        }

    def health_snapshot(self):
        return {"ready": True}

    def shutdown(self) -> None:
        return None


def test_process_proxy_preserves_canonical_history_preparation_contract() -> None:
    as_of = datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai"))
    requests = (
        ("SH.600000", ("d", "30m", "5m")),
        ("SZ.000001", ("d", "30m", "5m", "1m")),
    )
    transport = _HistoryPreparationTransport()
    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport
    )

    result = proxy.prepare_local_history(
        frequency_requests=requests,
        as_of=as_of,
    )

    assert result == {
        "schema": "chanlun-screening-local-history-preparation",
        "as_of": as_of.isoformat(),
        "prepared_frequencies_by_code": dict(requests),
        "batch_download_available": True,
    }
    assert transport.calls == [
        (
            "prepare_local_history",
            {"frequency_requests": requests, "as_of": as_of},
        )
    ]
    assert proxy._prepared_local_frequencies("SH.600000", as_of) == (
        "d",
        "30m",
        "5m",
    )
    with pytest.raises(ValueError, match="canonical and unique"):
        proxy.prepare_local_history(
            frequency_requests=tuple(reversed(requests)),
            as_of=as_of,
        )


def test_process_proxy_realtime_history_preparation_never_starts_batch_download() -> None:
    as_of = datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai"))
    requests = (
        ("SH.600000", ("d", "30m", "5m", "1m")),
        ("SZ.000001", ("d", "30m", "5m")),
    )
    transport = _HistoryPreparationTransport()
    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport
    )

    priority = proxy.prepare_priority_local_history(
        frequency_requests=requests,
        as_of=as_of,
    )

    assert transport.calls == []
    assert priority["prepared_frequencies_by_code"] == {
        "SH.600000": ("d", "30m"),
        "SZ.000001": ("d", "30m"),
    }
    assert proxy._prepared_local_frequencies("SH.600000", as_of) == (  # noqa: SLF001
        "d",
        "30m",
    )

    candidate = proxy.prepare_candidate_local_history_until(
        frequency_requests=(("SH.600000", ("5m",)),),
        as_of=as_of,
        deadline_monotonic=time.monotonic() + 1,
    )

    assert transport.calls == []
    assert candidate["prepared_frequencies_by_code"] == {
        "SH.600000": ("d", "30m")
    }


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
                "instrument_type": "stock_cn",
                "frequencies": ("1m", "5m", "30m", "d"),
                "higher_timeframe_as_of": cutoff,
                "local_history_frequencies": (),
                "incremental_refresh_frequencies": (),
            },
        )
    ]


def test_process_proxy_candidate_refreshes_only_missing_intraday_frequencies() -> None:
    as_of = datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai"))
    sector = SectorAssessment(
        sector_id="unclassified",
        sector_name="未分类",
        eligible=False,
        hard_block=True,
        regime="hostile",
        rank_components=(),
        reason_codes=("test",),
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
    proxy.prepare_priority_local_history(
        frequency_requests=(("SH.600000", ("d", "30m", "5m", "1m")),),
        as_of=as_of,
    )
    deadline = time.monotonic() + 1

    result = proxy.candidate_structure_bundle_with_risk_cutoff_until(
        "SH.600000",
        as_of=as_of,
        sector=sector,
        frequencies=("1m", "5m", "30m", "d"),
        risk_evidence_cutoff=as_of,
        deadline_monotonic=deadline,
    )

    assert result is bundle
    request_kwargs = transport.calls[-1][1]
    assert request_kwargs["local_history_frequencies"] == ("d", "30m")
    assert request_kwargs["incremental_refresh_frequencies"] == ("5m", "1m")
    # The lane deadline controls admission.  Once admitted, a candidate gets a
    # bounded execution window so a cold first request does not destroy the
    # process and all of its newly restored incremental state.
    assert request_kwargs["deadline_monotonic"] >= deadline
    assert request_kwargs["deadline_monotonic"] - time.monotonic() > 70


def test_process_proxy_allows_fail_closed_unclassified_structure_without_sector_cache() -> None:
    """没有板块快照时仍可识别个股结构，但只能传递空的板块成员证据。"""

    as_of = datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai"))
    sector = SectorAssessment(
        sector_id="unclassified",
        sector_name="未匹配 QMT GICS3 行业",
        eligible=False,
        hard_block=True,
        regime="hostile",
        rank_components=(),
        reason_codes=("sector_membership_missing",),
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

    assert proxy.structure_bundle_with_risk_cutoff(
        "SH.600000",
        as_of=as_of,
        sector=sector,
        frequencies=("1m", "5m"),
        risk_evidence_cutoff=as_of,
    ) is bundle
    assert transport.calls[0][1]["sector_members"] == ()


def test_process_proxy_passes_etf_type_into_isolated_native_structure_worker() -> None:
    as_of = datetime(2026, 7, 29, 15, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    sector = SectorAssessment(
        sector_id="unclassified",
        sector_name="未匹配 QMT GICS3 行业",
        eligible=False,
        hard_block=True,
        regime="hostile",
        rank_components=(),
        reason_codes=("sector_membership_missing",),
    )
    bundle = SymbolStructureBundle(
        code="SH.513100",
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
        transport=transport,
        instrument_type_provider=lambda codes: {code: "etf_cn" for code in codes},
    )

    assert proxy.structure_bundle_with_risk_cutoff(
        "SH.513100",
        as_of=as_of,
        sector=sector,
        frequencies=("1m", "5m"),
        risk_evidence_cutoff=as_of,
    ) is bundle
    assert transport.calls[0][1]["instrument_type"] == "etf_cn"


def test_process_proxy_filters_watchlist_and_holdings_through_shared_catalog() -> None:
    transport = _InstrumentScopeTransport()
    catalog = _InstrumentTypeCatalog()
    proxy = NativeTradingDataGatewayProcessProxy(
        transport=transport,  # type: ignore[arg-type]
        instrument_type_provider=catalog,
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
    assert proxy.screening_instrument_types(("SH.000001", "SH.510300")) == {
        "SH.000001": "index_cn",
        "SH.510300": "etf_cn",
    }
    assert catalog.calls == [
        ("SH.000001", "SH.510300"),
        ("SH.600000",),
    ]
    assert transport.calls == []


def test_process_proxy_reads_and_caches_shared_instrument_types_without_ipc() -> None:
    transport = _InstrumentScopeTransport()
    catalog = _InstrumentTypeCatalog()
    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        instrument_type_provider=catalog,
    )
    codes = tuple(f"SH.{value:06d}" for value in range(600000, 600065))

    first = proxy.screening_instrument_types(codes)
    second = proxy.screening_instrument_types(codes)

    assert first == second == {code: "stock_cn" for code in codes}
    assert catalog.calls == [codes]
    assert transport.calls == []


def test_process_proxy_retries_unresolved_shared_instrument_type() -> None:
    transport = _InstrumentScopeTransport()
    responses = iter(("unresolved_cn", "stock_cn"))
    calls: list[tuple[str, ...]] = []

    def catalog(codes: tuple[str, ...]) -> dict[str, str]:
        calls.append(codes)
        return {codes[0]: next(responses)}

    proxy = NativeTradingDataGatewayProcessProxy(  # type: ignore[arg-type]
        transport=transport,
        instrument_type_provider=catalog,
    )

    assert proxy.screening_instrument_types(("SH.600000",)) == {
        "SH.600000": "unresolved_cn"
    }
    assert proxy.screening_instrument_types(("SH.600000",)) == {
        "SH.600000": "stock_cn"
    }
    assert calls == [("SH.600000",), ("SH.600000",)]
    assert transport.calls == []


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

    assert snapshot["schema"] == "chanlun-native-sector-snapshot"
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
        "schema": "chanlun-native-sector-snapshot",
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

    assert (
        proxy.trading_session_evidence(
            session=session,
            observed_at=observed,
        )["classification"]
        == "UNRESOLVED"
    )
    assert (
        proxy.trading_session_evidence(
            session=session,
            observed_at=observed,
        )["classification"]
        == "UNRESOLVED"
    )
    assert len(transport.calls) == 2

    forged = dict(unresolved)
    forged["classification"] = "NON_TRADING_SESSION"
    transport.evidence = forged
    with pytest.raises(NativeScreeningWorkerProtocolError):
        proxy.trading_session_evidence(
            session=session,
            observed_at=observed,
        )


def test_proxy_calendar_readiness_never_waits_behind_busy_native_screening() -> None:
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


def test_atomic_sector_snapshot_never_occupies_reserved_priority_worker() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    priority = _AtomicTransport(_atomic_snapshot(as_of))
    candidate = _AtomicTransport(_atomic_snapshot(as_of))
    proxy = NativeTradingDataGatewayProcessProxy(transport=priority)  # type: ignore[arg-type]
    proxy._structure_transports = (priority, candidate)  # type: ignore[assignment]  # noqa: SLF001

    batch = proxy.native_sector_assessments(as_of=as_of)

    assert batch.completed_count == 1
    assert priority.calls == []
    assert candidate.calls == [("sector_snapshot", {"as_of": as_of})]


def test_candidate_lane_uses_free_shard_during_atomic_sector_snapshot() -> None:
    as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))

    class BlockingAtomicTransport(_AtomicTransport):
        def __init__(self, snapshot: dict[str, object]) -> None:
            super().__init__(snapshot)
            self.started = Event()
            self.release = Event()

        def request(self, method: str, **kwargs: object) -> object:
            self.calls.append((method, kwargs))
            assert method == "sector_snapshot"
            self.started.set()
            assert self.release.wait(timeout=5)
            return self.snapshot

    priority = _AtomicTransport(_atomic_snapshot(as_of))
    sector = BlockingAtomicTransport(_atomic_snapshot(as_of))
    available = _AtomicTransport(_atomic_snapshot(as_of))
    proxy = NativeTradingDataGatewayProcessProxy(transport=priority)  # type: ignore[arg-type]
    proxy._structure_transports = (  # type: ignore[assignment]  # noqa: SLF001
        priority,
        sector,
        available,
    )
    result: list[SectorAssessmentBatch] = []
    thread = Thread(
        target=lambda: result.append(proxy.native_sector_assessments(as_of=as_of)),
        daemon=True,
    )

    thread.start()
    assert sector.started.wait(timeout=5)
    assert proxy._structure_transports_for_lane("candidate") == (available,)  # noqa: SLF001
    assert proxy._structure_transport(  # noqa: SLF001
        "SH.600000",
        lane="candidate",
    ) is available
    sector.release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert result[0].completed_count == 1
    assert proxy._structure_transports_for_lane("candidate") == (  # noqa: SLF001
        sector,
        available,
    )


def test_sector_batch_cache_roundtrips_hierarchy_relations() -> None:
    parent = SectorAssessment(
        sector_id="qmt-gics3:parent",
        sector_name="信息技术",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(("neutral_access", 5),),
        reason_codes=("test_eligible",),
    )
    child = SectorAssessment(
        sector_id="qmt-gics4:child",
        sector_name="信息技术 → 半导体",
        eligible=True,
        hard_block=False,
        regime="supportive",
        rank_components=(("five_support", 30),),
        reason_codes=("test_eligible",),
    )
    expected = SectorAssessmentBatch(
        assessments=(parent, child),
        discovered_count=2,
        completed_count=2,
        failure_counts=(),
        errors=(),
        catalog_revision="sha256:" + "8" * 64,
        parent_relations=((child.sector_id, parent.sector_id),),
    )

    document = screening_process_subject._batch_cache_document(expected)
    restored = screening_process_subject._batch_from_cache(document)

    assert restored == expected
    assert document["parent_relations"] == [
        [child.sector_id, parent.sector_id]
    ]


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
    assert document["schema"] == "chanlun-native-sector-snapshot-cache"
    assert document["content_sha256"] == sha256_json(document["payload"])
    assert document["payload"]["snapshot"]["assessments"]["catalog_revision"] == (
        "sha256:" + "7" * 64
    )
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


def test_priority_cache_reader_reuses_stale_snapshot_without_worker_call(
    tmp_path: Path,
) -> None:
    """盘中读取旧快照只恢复事实和路由，不得因周期变化启动原生重建。"""

    cached_as_of = datetime(2026, 7, 29, 10, 35, tzinfo=ZoneInfo("Asia/Shanghai"))
    requested_as_of = cached_as_of + timedelta(minutes=10)
    cache_path = tmp_path / "sector-snapshot.json"
    revision = "head.tree.priority-cache"
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

    restored = second.cached_sector_snapshot_for_priority(as_of=requested_as_of)

    assert restored is not None
    assert restored.batch == expected
    assert restored.requested_as_of == cached_as_of
    assert restored.current_decision_epoch is False
    assert restored.members == {"TDX.880301": ("SH.600000",)}
    assert transport.calls == []
    cache_health = second.health_snapshot()["sector_snapshot_cache"]
    assert cache_health["state"] == "priority_stale_hit"
    assert cache_health["reason"] == "CACHE_DECISION_TIME_STALE"


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
    assert cached["exclusion_counts"] == [["sector_member_coverage_insufficient", 1]]
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
    assert restored.exclusion_counts == (("sector_member_coverage_insufficient", 1),)
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

    assert transport.calls == [("sector_snapshot", {"as_of": requested_as_of})]


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
        "a" * 40 + ".tree." + "b" * 24 + ".run." + "c" * 32,
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
        "CHANLUN_TRADING_SCREENING_FULL_COVERAGE_WORKERS",
        raising=False,
    )
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_CANDIDATE_5M_MAX_SYMBOLS",
        raising=False,
    )
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_CANDIDATE_30M_MAX_SYMBOLS",
        raising=False,
    )
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH",
        raising=False,
    )
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_PRIORITY_MAX_SYMBOLS",
        raising=False,
    )
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_NATIVE_MAX_COMPLETED_REQUESTS",
        raising=False,
    )
    monkeypatch.delenv(
        "CHANLUN_TRADING_SCREENING_NATIVE_MAX_RSS_MB",
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

    expected_workers = min(3, max(1, (os.cpu_count() or 4) // 4))
    assert app.config["TRADING_SCREENING_STOCK_WORKERS"] == expected_workers
    assert app.config["TRADING_SCREENING_FULL_COVERAGE_WORKERS"] == 3
    assert app.config["TRADING_SCREENING_FULL_COVERAGE_ENABLED"] is False
    assert app.config["TRADING_SCREENING_CANDIDATE_5M_MAX_SYMBOLS"] == 512
    assert app.config["TRADING_SCREENING_CANDIDATE_30M_MAX_SYMBOLS"] == 96
    assert app.config[
        "TRADING_SCREENING_SUPPORTIVE_DISCOVERY_MAX_SECTOR_RANK"
    ] == 128
    assert app.config["TRADING_SCREENING_TOTAL_SYMBOLS_PER_REFRESH"] == 64
    assert app.config["TRADING_SCREENING_PRIORITY_MAX_SYMBOLS"] == 512
    assert app.config["TRADING_SCREENING_NATIVE_IDLE_TIMEOUT_SECONDS"] == 210.0
    assert app.config["TRADING_SCREENING_NATIVE_MAX_COMPLETED_REQUESTS"] == 4096
    assert app.config["TRADING_SCREENING_NATIVE_MAX_RSS_MB"] == 1536
    gateway = app.extensions["decision_support_trading_screening_gateway"]
    assert len(gateway._structure_transports) == expected_workers  # noqa: SLF001
    assert gateway._transport not in gateway._structure_transports  # noqa: SLF001
    expected_cache_roles = (
        ["shared"]
        if expected_workers == 1
        else ["priority"] + ["candidate"] * (expected_workers - 1)
    )
    assert [
        transport._environment["CHANLUN_SCREENING_WORKER_CACHE_ROLE"]
        for transport in gateway._structure_transports  # noqa: SLF001
    ] == expected_cache_roles
    if expected_workers > 1:
        priority_environments = [
            transport._environment
            for transport in gateway._structure_transports[:1]  # noqa: SLF001
        ]
        candidate_environments = [
            transport._environment
            for transport in gateway._structure_transports[1:]  # noqa: SLF001
        ]
        assert all(
            "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_DIR" not in environment
            and "CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_KEY" not in environment
            for environment in priority_environments
        )
        assert [
            environment["CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_DIR"].split(
                "structure-"
            )[-1]
            for environment in candidate_environments
        ] == [str(index) for index in range(2, expected_workers + 1)]
        assert all(
            len(
                bytes.fromhex(
                    environment["CHANLUN_SCREENING_WORKER_RUNTIME_CACHE_KEY"]
                )
            )
            == 32
            for environment in candidate_environments
        )
    worker_pool = gateway.health_snapshot()["structure_worker_pool"]
    assert worker_pool["priority_reserved_worker_count"] == 1
    assert worker_pool["candidate_worker_count"] == max(1, expected_workers - 1)
    assert worker_pool["candidate_released_worker_count"] == max(
        1, expected_workers - 1
    )


def test_structure_worker_pool_uses_sticky_symbol_routing_inside_each_lane() -> None:
    transport = _BundleTransport(
        SymbolStructureBundle(
            code="SH.600000",
            as_of=datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
            sector=SectorAssessment(
                sector_id="unclassified",
                sector_name="未分类",
                eligible=False,
                hard_block=True,
                regime="hostile",
                rank_components=(),
                reason_codes=("test",),
            ),
            thirty_direction="neutral",
            thirty_points=(),
            five_points=(),
            one_points=(),
            opposite_points=(),
        )
    )
    gateway = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]
    priority_worker = object()
    candidate_workers = (object(), object())
    gateway._structure_transports = (  # type: ignore[assignment]  # noqa: SLF001
        priority_worker,
        *candidate_workers,
    )

    priority_routes = tuple(
        gateway._structure_transport(  # noqa: SLF001
            "SH.600000",
            lane="priority",
        )
        for _ in range(3)
    )
    candidate_routes = tuple(
        gateway._structure_transport(  # noqa: SLF001
            "SH.600000",
            lane="candidate",
        )
        for _ in range(3)
    )
    released_routes = tuple(
        gateway._structure_transport(  # noqa: SLF001
            "SH.600000",
            lane="candidate_overflow",
        )
        for _ in range(3)
    )

    assert priority_routes == (priority_worker,) * 3
    assert len(set(map(id, candidate_routes))) == 1
    assert candidate_routes[0] in candidate_workers
    assert released_routes == candidate_routes


def test_unbounded_candidate_structure_never_uses_priority_worker() -> None:
    as_of = datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai"))
    sector = SectorAssessment(
        sector_id="qmt-gics3:software",
        sector_name="软件",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(),
        reason_codes=("test",),
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
    priority = _BundleTransport(bundle)
    candidate_one = _BundleTransport(bundle)
    candidate_two = _BundleTransport(bundle)
    gateway = NativeTradingDataGatewayProcessProxy(transport=priority)  # type: ignore[arg-type]
    gateway._structure_transports = (  # type: ignore[assignment]  # noqa: SLF001
        priority,
        candidate_one,
        candidate_two,
    )
    gateway._sector_members = {sector.sector_id: ("SH.600000",)}  # noqa: SLF001

    result = gateway.candidate_structure_bundle_with_risk_cutoff(
        "SH.600000",
        as_of=as_of,
        sector=sector,
        frequencies=("d", "30m", "5m"),
        risk_evidence_cutoff=as_of,
    )

    assert result == bundle
    assert priority.calls == []
    assert len(candidate_one.calls) + len(candidate_two.calls) == 1


def test_structure_worker_pool_co_locates_classified_sector_symbols() -> None:
    transport = _BundleTransport(
        SymbolStructureBundle(
            code="SH.600000",
            as_of=datetime(2026, 7, 29, 9, 47, tzinfo=ZoneInfo("Asia/Shanghai")),
            sector=SectorAssessment(
                sector_id="unclassified",
                sector_name="unclassified",
                eligible=False,
                hard_block=True,
                regime="hostile",
                rank_components=(),
                reason_codes=("test",),
            ),
            thirty_direction="neutral",
            thirty_points=(),
            five_points=(),
            one_points=(),
            opposite_points=(),
        )
    )
    gateway = NativeTradingDataGatewayProcessProxy(transport=transport)  # type: ignore[arg-type]
    workers = (object(), object(), object())
    gateway._structure_transports = workers  # type: ignore[assignment]  # noqa: SLF001
    sector = SectorAssessment(
        sector_id="qmt-gics3:software",
        sector_name="software",
        eligible=True,
        hard_block=False,
        regime="neutral",
        rank_components=(),
        reason_codes=("test",),
    )

    first = gateway._structure_affinity_key(  # noqa: SLF001
        "SH.600000",
        sector,
        has_sector_members=True,
    )
    second = gateway._structure_affinity_key(  # noqa: SLF001
        "SZ.000001",
        sector,
        has_sector_members=True,
    )

    assert first == second == "sector:qmt-gics3:software"
    assert gateway._structure_transport(  # noqa: SLF001
        first,
        lane="coverage",
    ) is gateway._structure_transport(second, lane="coverage")  # noqa: SLF001
    assert gateway._structure_affinity_key(  # noqa: SLF001
        "SH.600000",
        sector,
        has_sector_members=False,
    ) == "symbol:SH.600000"
    assert gateway._lane_structure_affinity_key(  # noqa: SLF001
        "SH.600000",
        first,
        work_lane="priority",
    ) == first
    candidate_keys = tuple(
        gateway._lane_structure_affinity_key(  # noqa: SLF001
            f"SH.{600000 + index:06d}",
            first,
            work_lane="candidate",
        )
        for index in range(16)
    )
    assert len(set(candidate_keys)) == len(candidate_keys)
    assert {
        id(gateway._structure_transport(key, lane="candidate"))  # noqa: SLF001
        for key in candidate_keys
    } == {id(worker) for worker in workers[1:]}
    gateway._structure_transports = (transport,)  # type: ignore[assignment]  # noqa: SLF001
    assert gateway.health_snapshot()["structure_worker_pool"][
        "affinity_contract_id"
    ] == "priority-sector_candidate-sector-symbol-striped-v2"


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
