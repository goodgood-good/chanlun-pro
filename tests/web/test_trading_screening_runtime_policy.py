from __future__ import annotations

import copy

import pytest

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.decision_source_provenance import (
    current_decision_source_snapshot as _live_decision_source_snapshot,
)
from cl_app.services.trading_screening_runtime_policy import (
    candidate_monitor_deadline_perf,
)
from cl_app.services.trading_screening_source_migrations import (
    completed_retry_residue_source_migration_allowed,
    incomplete_retry_reconciliation_source_migration_allowed,
    orchestration_source_migration_allowed,
    priority_monitor_state_source_migration_allowed,
    resumable_checkpoint_source_migration_allowed,
    sector_snapshot_source_migration_allowed,
)


_PRE_CLEANUP_SOURCE_ID = (
    "sha256:e3f2bb6ffc56a16901582a22b4759a1e3df720670b3fb01126191043248e51d3"
)
_CLEANUP_SOURCE_ID = (
    "sha256:7343aa38677a66b48f5777404c8698c3f92c8318701241cffa4f4742ee18418b"
)
_CLEANUP_SOURCE_ROWS = {
    "src/chanlun/core/strict_structure/strength.py": (
        "sha256:b6eea83b5b04013e07daf15b8e561c579f071dfa5421309d78729ad6e2b0e53f",
        "sha256:6dd15451597fe36c0d13ebef26d2256b6688794f5bc08e1d7cf703b3bfcf5d9a",
    ),
    "src/chanlun/decision_support/trading_system/human_review_screening.py": (
        "sha256:2aa7f811c55128422e789497c3aa9d550d1cb7d3c37574b9dfa1dc1668b5f77a",
        "sha256:a3b2ae6413df5b286f41b9e8c0b32c1dcbe060e16f0a3962e63e8a6b93da053c",
    ),
    "src/chanlun/decision_support/trading_system/qmt_same_base_stream.py": (
        "sha256:d1f656f3c58498aef87a0adfb7a2bc0c8bddf7c3702034e8a782ca0c9136b13c",
        "sha256:952400669e3d4c8bde6a796e1fa363cb95c24be48ffc9ea4ab4dc5d5ea52186d",
    ),
    "src/chanlun/exchange/qmt_screening_sector_source.py": (
        "sha256:94f16f459cbf9b1f5cd2a587729726a8ee8ad77b531b93507a7aa1f01a010cac",
        "sha256:2516025c59ae3b676d31e26b30a8bf1647217ff867c0f8a4f4bbae3736348365",
    ),
}


def pre_cleanup_decision_source_snapshot() -> dict[str, object]:
    """Reconstruct the authenticated release immediately before dead-code cleanup."""

    snapshot = copy.deepcopy(_live_decision_source_snapshot())
    assert snapshot["aggregate_sha256"] == _CLEANUP_SOURCE_ID
    rows = {row["path"]: row for row in snapshot["files"]}
    for path, (previous_digest, current_digest) in _CLEANUP_SOURCE_ROWS.items():
        assert rows[path]["sha256"] == current_digest
        rows[path]["sha256"] = previous_digest
    snapshot["aggregate_sha256"] = sha256_json(
        {"schema": snapshot["schema"], "files": snapshot["files"]}
    )
    assert snapshot["aggregate_sha256"] == _PRE_CLEANUP_SOURCE_ID
    return snapshot


def deployed_decision_source_snapshot() -> dict[str, object]:
    """Reconstruct the release running immediately before this scheduler fix."""

    snapshot = pre_cleanup_decision_source_snapshot()
    rows = {row["path"]: row for row in snapshot["files"]}
    assert rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] == (
        "sha256:8dae5e9e3172bac95e10a6d6581b6842185bfaa0983516c4267f4fa02a472679"
    )
    assert rows[
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    ]["sha256"] == (
        "sha256:092fb536a4cfcc0e6a3fe4d493082e9494680d07628ee859c9bb0017e0eacc8f"
    )
    assert rows[
        "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py"
    ]["sha256"] == (
        "sha256:e074b522c6f7dd02a4091737f0397880f848bac5df3d77618e26174c6777484b"
    )
    assert rows[
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    ]["sha256"] == (
        "sha256:9953acc53171ab81e2c32533bc243627548bba1ff450081b8bd7b8e0715235ba"
    )
    rows[
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    ]["sha256"] = (
        "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74"
    )
    rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:befa02220a62df34a24eeb76e41f7f56132ed9d6eb2a2f1b87dea082d474c7c8"
    )
    rows[
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    ]["sha256"] = (
        "sha256:e55b855f2553e5f358462add507cc16d1150fa8576e0400b3061ad1e3ade5bbe"
    )
    rows[
        "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py"
    ]["sha256"] = (
        "sha256:fb0386a7a36802d11eb8f80de82d4917fed6f01e73ab3259b1ade498f5bdd688"
    )
    snapshot["aggregate_sha256"] = sha256_json(
        {"schema": snapshot["schema"], "files": snapshot["files"]}
    )
    assert snapshot["aggregate_sha256"] == (
        "sha256:0ec384b6f28c96b77d4a5ca9901c10420c31a63a7fde8c44bd383d953d5d9cf5"
    )
    return snapshot


def current_decision_source_snapshot() -> dict[str, object]:
    """Reconstruct the pre-admission release used by historical edge tests."""

    snapshot = deployed_decision_source_snapshot()
    rows = {row["path"]: row for row in snapshot["files"]}
    rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658"
    )
    rows[
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    ]["sha256"] = (
        "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5"
    )
    snapshot["aggregate_sha256"] = sha256_json(
        {"schema": snapshot["schema"], "files": snapshot["files"]}
    )
    assert snapshot["aggregate_sha256"] == (
        "sha256:b676f7b4ff538652c2920d826f28c6b997460db9b0370a6e8f4ac41e34a0394d"
    )
    return snapshot


def test_live_candidate_lane_keeps_shared_priority_deadline() -> None:
    assert candidate_monitor_deadline_perf(
        priority_deadline_perf=55.0,
        candidate_budget_deadline_perf=90.0,
        minute_codes_present=True,
        force_startup_bootstrap=False,
        compute_window_open=True,
    ) == pytest.approx(55.0)


def test_live_candidate_lane_reserves_time_for_atomic_finalization() -> None:
    assert candidate_monitor_deadline_perf(
        priority_deadline_perf=55.0,
        candidate_budget_deadline_perf=90.0,
        minute_codes_present=True,
        force_startup_bootstrap=False,
        compute_window_open=True,
        priority_finalization_reserve_seconds=5.0,
    ) == pytest.approx(50.0)


def test_live_candidate_without_minute_codes_still_protects_bar_boundary() -> None:
    assert candidate_monitor_deadline_perf(
        priority_deadline_perf=55.0,
        candidate_budget_deadline_perf=90.0,
        minute_codes_present=False,
        force_startup_bootstrap=False,
        compute_window_open=True,
        priority_finalization_reserve_seconds=5.0,
    ) == pytest.approx(50.0)


def test_closed_startup_candidate_lane_uses_independent_budget() -> None:
    assert candidate_monitor_deadline_perf(
        priority_deadline_perf=55.0,
        candidate_budget_deadline_perf=90.0,
        minute_codes_present=True,
        force_startup_bootstrap=True,
        compute_window_open=False,
    ) == pytest.approx(90.0)


def test_latest_scheduler_transition_requires_all_coordinated_source_rows() -> None:
    current = pre_cleanup_decision_source_snapshot()
    cached = deployed_decision_source_snapshot()
    assert current["aggregate_sha256"] == (
        "sha256:e3f2bb6ffc56a16901582a22b4759a1e3df720670b3fb01126191043248e51d3"
    )
    kwargs = {
        "cached_decision_source_snapshot_id": cached["aggregate_sha256"],
        "current_decision_source_snapshot_id": current["aggregate_sha256"],
        "cached_decision_source_snapshot": cached,
        "current_decision_source_snapshot": current,
    }
    assert orchestration_source_migration_allowed(**kwargs)
    assert resumable_checkpoint_source_migration_allowed(**kwargs)

    deployed_continuity = copy.deepcopy(current)
    deployed_rows = {row["path"]: row for row in deployed_continuity["files"]}
    deployed_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:6769cc39632997717a5569a52ce5020949c8766afe8ea550ebbb0109a8a31c8f"
    )
    deployed_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    ]["sha256"] = (
        "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74"
    )
    deployed_continuity["aggregate_sha256"] = sha256_json(
        {
            "schema": deployed_continuity["schema"],
            "files": deployed_continuity["files"],
        }
    )
    assert deployed_continuity["aggregate_sha256"] == (
        "sha256:8b6b4c66a6628d081d44529b24412deb3ead24b9b040e1acfca1cb79f6847ca8"
    )
    deployed_kwargs = {
        "cached_decision_source_snapshot_id": deployed_continuity[
            "aggregate_sha256"
        ],
        "current_decision_source_snapshot_id": current["aggregate_sha256"],
        "cached_decision_source_snapshot": deployed_continuity,
        "current_decision_source_snapshot": current,
    }
    assert orchestration_source_migration_allowed(**deployed_kwargs)
    assert resumable_checkpoint_source_migration_allowed(**deployed_kwargs)

    partial = copy.deepcopy(current)
    partial_rows = {row["path"]: row for row in partial["files"]}
    partial_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:befa02220a62df34a24eeb76e41f7f56132ed9d6eb2a2f1b87dea082d474c7c8"
    )
    partial["aggregate_sha256"] = sha256_json(
        {"schema": partial["schema"], "files": partial["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=partial["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=partial,
        current_decision_source_snapshot=current,
    )


def test_dead_code_cleanup_requires_all_authenticated_source_rows() -> None:
    current = _live_decision_source_snapshot()
    cached = pre_cleanup_decision_source_snapshot()
    kwargs = {
        "cached_decision_source_snapshot_id": cached["aggregate_sha256"],
        "current_decision_source_snapshot_id": current["aggregate_sha256"],
        "cached_decision_source_snapshot": cached,
        "current_decision_source_snapshot": current,
    }
    assert orchestration_source_migration_allowed(**kwargs)
    assert resumable_checkpoint_source_migration_allowed(**kwargs)
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=_PRE_CLEANUP_SOURCE_ID,
        current_decision_source_snapshot_id=_CLEANUP_SOURCE_ID,
    )

    partial = copy.deepcopy(cached)
    first_path = next(iter(_CLEANUP_SOURCE_ROWS))
    partial_rows = {row["path"]: row for row in partial["files"]}
    partial_rows[first_path]["sha256"] = _CLEANUP_SOURCE_ROWS[first_path][1]
    partial["aggregate_sha256"] = sha256_json(
        {"schema": partial["schema"], "files": partial["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=partial["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=partial,
        current_decision_source_snapshot=current,
    )

    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=(
            "sha256:2fbb5a59c874c65e51d82be910ac1d34b9fc18ae7b602324a3aa769392c614cf"
        ),
        current_source_revision=(
            "sha256:c7f38b622c9f5d88402ff8621a513cdd606a5d8d74c9d141810c7144600a041e"
        ),
    )


def test_operational_source_migration_requires_authenticated_manifests() -> None:
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id="sha256:" + "9" * 64,
        current_decision_source_snapshot_id="sha256:" + "8" * 64,
    )


def test_priority_monitor_state_migration_allows_only_reviewed_forward_paths() -> None:
    opening_release = (
        "sha256:01eddbef608cfef200aba7a7f4d1ed90f70fd37f794efd4f83f8afdeeb7234cc"
    )
    pre_admission_release = (
        "sha256:b676f7b4ff538652c2920d826f28c6b997460db9b0370a6e8f4ac41e34a0394d"
    )
    admission_release = (
        "sha256:0e242b05dee654f8d99e97e4c2eb91dd2304811c971575f4900175e0bad70c3f"
    )
    intraday_release = (
        "sha256:abcac87b2319d28a1d57cd29e4c24cba63085e69db1621b7898324a41670b03a"
    )
    streaming_release = (
        "sha256:d3712db30fdc0ec2ff920d1a171f33df9ea322776ab8a2dccb975b8242320624"
    )
    scheduling_release = (
        "sha256:9fc76471f0fe57e5fd6b3e83907a87500d1d5c021dd377136d83b588eaee0034"
    )
    close_handoff_release = (
        "sha256:504d37341745d9a201d07d1e89975431ee2218fb41a042641696aa3a4df523d9"
    )
    current = (
        "sha256:0ec384b6f28c96b77d4a5ca9901c10420c31a63a7fde8c44bd383d953d5d9cf5"
    )
    scheduler_release = (
        "sha256:2d70261f1ab2c3160cbd74161f54d85f114cfbbbdfdfea7653ca3495656c08c3"
    )
    deployed_continuity_release = (
        "sha256:8b6b4c66a6628d081d44529b24412deb3ead24b9b040e1acfca1cb79f6847ca8"
    )
    continuity_release = (
        "sha256:7f5060a02826eaff56c713b06229c026e7650cb3435566c056006e22d790f342"
    )
    affinity_stream_release = (
        "sha256:978b540e92386151a2db3d1430a30b744e6677f26dcc0f88358979dc0dd41869"
    )
    runtime_capacity_release = (
        "sha256:71a798322866a5d67d698b49a162e1e30e122de01f608584128efb4fda06acd1"
    )
    monitor_capacity_release = (
        "sha256:5d94e0e5134f3b8f293b391aee0f0d53c45687e666034e07aeee9b99f2f7e792"
    )
    fairness_release = (
        "sha256:5405cf3d282e051a50bf30212f593de2d44ab9265958faad86d8ddfff6974e8d"
    )
    final_hot_symbol_release = (
        "sha256:e3f2bb6ffc56a16901582a22b4759a1e3df720670b3fb01126191043248e51d3"
    )
    cleanup_release = (
        "sha256:7343aa38677a66b48f5777404c8698c3f92c8318701241cffa4f4742ee18418b"
    )

    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=opening_release,
        current_decision_source_snapshot_id=pre_admission_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=pre_admission_release,
        current_decision_source_snapshot_id=admission_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=admission_release,
        current_decision_source_snapshot_id=intraday_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=intraday_release,
        current_decision_source_snapshot_id=streaming_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=streaming_release,
        current_decision_source_snapshot_id=scheduling_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=scheduling_release,
        current_decision_source_snapshot_id=close_handoff_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=close_handoff_release,
        current_decision_source_snapshot_id=current,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=current,
        current_decision_source_snapshot_id=scheduler_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=scheduler_release,
        current_decision_source_snapshot_id=deployed_continuity_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=deployed_continuity_release,
        current_decision_source_snapshot_id=continuity_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=opening_release,
        current_decision_source_snapshot_id=continuity_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=continuity_release,
        current_decision_source_snapshot_id=affinity_stream_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=affinity_stream_release,
        current_decision_source_snapshot_id=runtime_capacity_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=runtime_capacity_release,
        current_decision_source_snapshot_id=monitor_capacity_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=monitor_capacity_release,
        current_decision_source_snapshot_id=fairness_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=fairness_release,
        current_decision_source_snapshot_id=final_hot_symbol_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=final_hot_symbol_release,
        current_decision_source_snapshot_id=cleanup_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=opening_release,
        current_decision_source_snapshot_id=cleanup_release,
    )
    assert priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=opening_release,
        current_decision_source_snapshot_id=affinity_stream_release,
    )
    assert not priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=continuity_release,
        current_decision_source_snapshot_id=intraday_release,
    )
    assert not priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id=opening_release,
        current_decision_source_snapshot_id="sha256:" + "0" * 64,
    )
    assert not priority_monitor_state_source_migration_allowed(
        cached_decision_source_snapshot_id="not-a-source-id",
        current_decision_source_snapshot_id=current,
    )


def test_intraday_projection_upgrade_preserves_authenticated_runtime_state() -> None:
    current = deployed_decision_source_snapshot()
    cached = copy.deepcopy(current)
    screening_row = next(
        row
        for row in cached["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    screening_row["sha256"] = (
        "sha256:f68e7eab264e0d818a2ddaf4fd4a5bee003f943c9dc6e5110bd20d13f52c6824"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert cached["aggregate_sha256"] == (
        "sha256:504d37341745d9a201d07d1e89975431ee2218fb41a042641696aa3a4df523d9"
    )
    assert current["aggregate_sha256"] == (
        "sha256:0ec384b6f28c96b77d4a5ca9901c10420c31a63a7fde8c44bd383d953d5d9cf5"
    )
    kwargs = {
        "cached_decision_source_snapshot_id": cached["aggregate_sha256"],
        "current_decision_source_snapshot_id": current["aggregate_sha256"],
        "cached_decision_source_snapshot": cached,
        "current_decision_source_snapshot": current,
    }
    assert orchestration_source_migration_allowed(**kwargs)
    assert resumable_checkpoint_source_migration_allowed(**kwargs)
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_live_admission_gate_requires_exact_coordinated_source_transition() -> None:
    current = deployed_decision_source_snapshot()
    cached = copy.deepcopy(current)
    cached_rows = {row["path"]: row for row in cached["files"]}
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658"
    )
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    ]["sha256"] = (
        "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )
    assert cached["aggregate_sha256"] == (
        "sha256:b676f7b4ff538652c2920d826f28c6b997460db9b0370a6e8f4ac41e34a0394d"
    )
    assert current["aggregate_sha256"] == (
        "sha256:0ec384b6f28c96b77d4a5ca9901c10420c31a63a7fde8c44bd383d953d5d9cf5"
    )

    kwargs = {
        "cached_decision_source_snapshot_id": cached["aggregate_sha256"],
        "current_decision_source_snapshot_id": current["aggregate_sha256"],
        "cached_decision_source_snapshot": cached,
        "current_decision_source_snapshot": current,
    }
    assert orchestration_source_migration_allowed(**kwargs)
    assert resumable_checkpoint_source_migration_allowed(**kwargs)

    partial = copy.deepcopy(current)
    partial_row = next(
        row
        for row in partial["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    partial_row["sha256"] = (
        "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658"
    )
    partial["aggregate_sha256"] = sha256_json(
        {"schema": partial["schema"], "files": partial["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=partial["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=partial,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_completed_epoch_capacity_transition() -> (
    None
):
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    transitions = {
        "web/chanlun_chart/cl_app/services/trading_screening.py": (
            "sha256:6d976d5f3dddc3c1f950b818c15140a6b2a97aeffb7142eb120da9328cb4eb81",
            "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py": (
            "sha256:781bdae926d461b657def096fc9994f013eaa29c6b97e74d31c06b07b8749637",
            "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74",
        ),
    }
    current_rows = {row["path"]: row for row in current["files"]}
    cached_rows = {row["path"]: row for row in cached["files"]}
    for path, (old_digest, new_digest) in transitions.items():
        assert current_rows[path]["sha256"] == new_digest
        cached_rows[path]["sha256"] = old_digest
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )

    intermediate = copy.deepcopy(current)
    intermediate_rows = {row["path"]: row for row in intermediate["files"]}
    intermediate_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    ]["sha256"] = (
        "sha256:5a42f1c112c80a1138f1d1d2182aa620dcb2ba993741e1a0bf32beec99731387"
    )
    intermediate["aggregate_sha256"] = sha256_json(
        {"schema": intermediate["schema"], "files": intermediate["files"]}
    )
    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=intermediate["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=intermediate,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_exact_parallel_sector_build_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    cached_rows = {row["path"]: row for row in cached["files"]}
    transitions = {
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py": (
            "sha256:5b3fa542f24b3e7ca17262ed1aeea4ad8aa61a9f65231bd12997c5ee02d38799",
            "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py": (
            "sha256:4ef9af14e4500ed7dbf55af575e510b8b02178dbfc25101ac21f547518778fcf",
            "sha256:52e60874bf524a58c53dbc3b549e78bd5766112b3c56d7c81e764bd335c268f4",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_process.py": (
            "sha256:fbdbea6840ef1ed782eb8274c009785f1ef11b1373f9ad977e20fc4dd7e2e358",
            "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5",
        ),
    }
    current_rows = {row["path"]: row for row in current["files"]}
    for path, (old_digest, new_digest) in transitions.items():
        assert current_rows[path]["sha256"] == new_digest
        cached_rows[path]["sha256"] = old_digest
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    forged = copy.deepcopy(current)
    forged["files"][0]["sha256"] = "sha256:" + "0" * 64
    forged["aggregate_sha256"] = sha256_json(
        {"schema": forged["schema"], "files": forged["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=forged["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=forged,
    )


def test_manifest_migration_allows_exact_bounded_realtime_batch_transition() -> (
    None
):
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    transitions = {
        "web/chanlun_chart/cl_app/services/trading_screening.py": (
            "sha256:9407da9bd22a530b3573f28fb8e97b5ebf756b2202bfd7b81d969ab0a67458b6",
            "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py": (
            "sha256:7e83375a8b8170913dc1f171c971c7a2cff1c220d4840549098562159347d654",
            "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_native_worker.py": (
            "sha256:8901b2dcfb32978a17bc6636f50eff6b03dca59ece299d145421610910a56e47",
            "sha256:52e60874bf524a58c53dbc3b549e78bd5766112b3c56d7c81e764bd335c268f4",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_process.py": (
            "sha256:381270d91c7e24ba176cfa4095aba941f779f1d5d44c9f2eaa2c8ee609287737",
            "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5",
        ),
    }
    current_rows = {row["path"]: row for row in current["files"]}
    cached_rows = {row["path"]: row for row in cached["files"]}
    for path, (old_digest, new_digest) in transitions.items():
        assert current_rows[path]["sha256"] == new_digest
        cached_rows[path]["sha256"] = old_digest
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_worker_queue_stripe_allows_only_the_exact_resumable_checkpoint_edge() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    current_row = next(
        row
        for row in current["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    cached_row = next(
        row
        for row in cached["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    assert current_row["sha256"] == (
        "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658"
    )
    current_row["sha256"] = (
        "sha256:0930623c45a7e58ac3635fe70432eb0756c18e9ecafbc74ce931b7637508d56d"
    )
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    cached_row["sha256"] = (
        "sha256:b5d16f6080ef2557c6edb5aa43c726aba62a5d3b489a7c0c2b4880fff43448ce"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert resumable_checkpoint_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not resumable_checkpoint_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_validated_local_first_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    current_rows = {row["path"]: row for row in current["files"]}
    cached_rows = {row["path"]: row for row in cached["files"]}
    gateway_path = (
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    )
    process_path = (
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    )
    exchange_path = "src/chanlun/exchange/exchange_qmt.py"
    assert current_rows[exchange_path]["sha256"] == (
        "sha256:006cf995b1ecde54355cbb9df63de044130967a96d200f60fe3bdd0a82b8a857"
    )
    assert current_rows[gateway_path]["sha256"] == (
        "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74"
    )
    assert current_rows[process_path]["sha256"] == (
        "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5"
    )
    cached_rows[gateway_path]["sha256"] = (
        "sha256:abc71aed21fa9eab2e8be21edbc01fc828e4e2157f2e263646efb9d065fff7d2"
    )
    cached_rows[process_path]["sha256"] = (
        "sha256:5851ef90c2f970c5748bfa6dacc87646284dc61819743944ca42b77c6f283821"
    )
    cached_rows[exchange_path]["sha256"] = (
        "sha256:31d023aab9d8f5ef951a8ea16866fffdb9c01f50a35e0063a438bd32ea25d617"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_subscription_boundary_refresh() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    cached_rows = {row["path"]: row for row in cached["files"]}
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:e45c252d40dab5845b80532c1ee7a0dd99919d0bb23c594be8fd66c02a3b069c"
    )
    cached_rows["src/chanlun/exchange/exchange_qmt.py"]["sha256"] = (
        "sha256:aab8af80fad0e489271e4762f28fc731e6fcacef07e73e1fbb023f33de7452ea"
    )
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    ]["sha256"] = (
        "sha256:d4944b72ac12081713638aa5964c1d7423b58ab0ea30cec6d3bb2f600e152445"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )
    assert cached["aggregate_sha256"] == (
        "sha256:ca47dd6db879c5f936344985287f848d2daa59ddd884f2e66b23c5df03b0973b"
    )
    assert current["aggregate_sha256"] == (
        "sha256:b676f7b4ff538652c2920d826f28c6b997460db9b0370a6e8f4ac41e34a0394d"
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_exact_realtime_tail_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    cached_rows = {row["path"]: row for row in cached["files"]}
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:abe4dfabb07cdf94826979c3c06f5518a9cf682697e1e3427a6f70cc0907c721"
    )
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    ]["sha256"] = (
        "sha256:6acf4ebc19f4027253616b38d215075354306104f3c65506c1a63a9a0bfce4c0"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_exact_installed_affinity_order_transition() -> None:
    current = current_decision_source_snapshot()
    current_rows = {row["path"]: row for row in current["files"]}
    assert current_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] == (
        "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658"
    )
    assert current_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    ]["sha256"] == (
        "sha256:c3b5270b4f64f5c9323b2da9a05bd4e1bc608a187a6226dee7d030086ddd7de5"
    )
    cached = copy.deepcopy(current)
    cached_rows = {row["path"]: row for row in cached["files"]}
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98"
    )
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    ]["sha256"] = (
        "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_realtime_capacity_release() -> None:
    current = current_decision_source_snapshot()
    current_rows = {row["path"]: row for row in current["files"]}
    assert current_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] == (
        "sha256:16455fb03ead040eaa8b23e1aec1a5bea381b3c078b8dc9d4bad7c577e6fd658"
    )
    assert current_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    ]["sha256"] == (
        "sha256:174180f907cd01064c7fb0d0a268b3b38a4bdd4e6d5968f8ab98ee3614dc4e74"
    )

    cached = copy.deepcopy(current)
    cached_rows = {row["path"]: row for row in cached["files"]}
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening.py"
    ]["sha256"] = (
        "sha256:2fbae30ff11f70342d2b06c08558bf6c85d0c070d3b0e829a2c1fb41a96083f0"
    )
    cached_rows[
        "web/chanlun_chart/cl_app/services/trading_screening_gateway.py"
    ]["sha256"] = (
        "sha256:d2496f15ec376b68f4fb1135ced452093c1714d2e34dc516fba650ccfefa9433"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_only_runtime_policy_change() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    runtime_path = (
        "web/chanlun_chart/cl_app/services/trading_screening_runtime_policy.py"
    )
    runtime_row = next(row for row in cached["files"] if row["path"] == runtime_path)
    runtime_row["sha256"] = "sha256:" + "1" * 64
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )

    decision_row = next(
        row
        for row in cached["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    decision_row["sha256"] = "sha256:" + "2" * 64
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_reviewed_cache_pointer_recovery_change() -> None:
    current = current_decision_source_snapshot()
    current_row = next(
        row
        for row in current["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    current_row["sha256"] = (
        "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98"
    )
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    cached = copy.deepcopy(current)
    decision_row = next(
        row
        for row in cached["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    assert decision_row["sha256"] == (
        "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98"
    )
    decision_row["sha256"] = (
        "sha256:bf56c653c086fc37e495d1824e960959fe48736ad346ee8a5e4b3d8c8d384e1d"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_exact_daily_completion_dispatch_change() -> None:
    current = current_decision_source_snapshot()
    path = "web/chanlun_chart/cl_app/services/trading_screening.py"
    current_row = next(row for row in current["files"] if row["path"] == path)
    current_row["sha256"] = (
        "sha256:c5d96b6543f3d5e6e555f06d6debdffc05086bad367b0b0595367e9385ef1b98"
    )
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    cached = copy.deepcopy(current)
    cached_row = next(row for row in cached["files"] if row["path"] == path)
    cached_row["sha256"] = (
        "sha256:cbd0d7ec63c1020c17a913d5fd38d15d5c1e8a27275a7f5d46ce277234640487"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_only_exact_reviewed_shard_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    transitions = {
        "web/chanlun_chart/cl_app/services/trading_screening.py": (
            "sha256:f4d1bf3f5030621a03c590946d28b318bbb715d4c9ad187b9b463324d7f81d25",
            "sha256:743704a5116f4dfac1530ae38dd1c9f491f5d56c8e21296322f717ff4a81141b",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_process.py": (
            "sha256:34bad75e736608383eae305e0979f50e71cd884330d3d947fed2945967d678ed",
            "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
        ),
    }
    for snapshot, offset in ((cached, 0), (current, 1)):
        rows = {row["path"]: row for row in snapshot["files"]}
        for path, digests in transitions.items():
            rows[path]["sha256"] = digests[offset]
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )

    current_row = next(
        row
        for row in current["files"]
        if row["path"]
        == "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    )
    current_row["sha256"] = "sha256:" + "9" * 64
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_exact_validation_liveness_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    path = "web/chanlun_chart/cl_app/services/trading_screening.py"
    current_row = next(row for row in current["files"] if row["path"] == path)
    cached_row = next(row for row in cached["files"] if row["path"] == path)
    current_row["sha256"] = (
        "sha256:6a1d8dd8fbf3b80794fb7f8e16f721cc73faf4119430a8c07e968adf2af233fa"
    )
    cached_row["sha256"] = (
        "sha256:ec204210c310ca0ca1f87057e1b41b13648062be48910b9b116a2c607a524434"
    )
    for snapshot in (cached, current):
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_validation_archive_idle_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    path = "web/chanlun_chart/cl_app/services/trading_screening.py"
    current_row = next(row for row in current["files"] if row["path"] == path)
    cached_row = next(row for row in cached["files"] if row["path"] == path)
    # Reconstruct the reviewed historical transition explicitly.  Later
    # decision-rule edits must invalidate an old archive rather than forcing
    # this orchestration-only migration test to bless the latest source hash.
    current_row["sha256"] = (
        "sha256:3cd8d938d16a422000dd7f6ea307645bed15c4a30094bd2302c845392b23cc85"
    )
    cached_row["sha256"] = (
        "sha256:6a1d8dd8fbf3b80794fb7f8e16f721cc73faf4119430a8c07e968adf2af233fa"
    )
    for snapshot in (cached, current):
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_priority_sector_snapshot_exclusion_transition() -> None:
    current = current_decision_source_snapshot()
    path = "web/chanlun_chart/cl_app/services/trading_screening_process.py"
    current_row = next(row for row in current["files"] if row["path"] == path)
    current_row["sha256"] = (
        "sha256:d21828c65153a0d70863c8ff598cefd5908692d1517c35e24c5e32ef329e5de6"
    )
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    cached = copy.deepcopy(current)
    cached_row = next(row for row in cached["files"] if row["path"] == path)
    cached_row["sha256"] = (
        "sha256:cbfd8b3c23680a2b604bae14d0c2baf8a8dc14fb537824bcb38184b5572fb0a7"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_review_auditor_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    transitions = {
        "src/chanlun/decision_support/trading_system/live_human_review.py": (
            "sha256:b554ac6c931cea904e0660dff27fe57537adddd9cd25f2cf4cc3285464966f03",
            "sha256:4e4ace9302d304a00373e01e659bb097677f8f3c9db5dfeb6bc57836215e8b84",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening_process.py": (
            "sha256:5e8b6809f29cd7aae51142a84f6d34af2db4894f44dde8b1252bda6de9c5f356",
            "sha256:bb5077ac0b737d14494a3357f8057c20de3171049e4f722321d4c57d6d84b568",
        ),
    }
    for snapshot, offset in ((cached, 0), (current, 1)):
        rows = {row["path"]: row for row in snapshot["files"]}
        for path, digests in transitions.items():
            rows[path]["sha256"] = digests[offset]
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_manifest_migration_allows_exact_incomplete_retry_reconciliation() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    transitions = {
        "src/chanlun/decision_support/trading_system/live_human_review.py": (
            "sha256:0ac28b9de593731560b31adc01c85c54ff36d77e7f74b80725b62992fc62d59f",
            "sha256:eaa58c37fea3ab49d7bd642297c10d4119410dd67b9487b01030a115fb359f26",
        ),
        "web/chanlun_chart/cl_app/services/trading_screening.py": (
            "sha256:9468b01376dc29927f52c0289355c387167a3c45a1a1b9779d8f67ef3341b6b0",
            "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
        ),
    }
    for snapshot, offset in ((cached, 0), (current, 1)):
        rows = {row["path"]: row for row in snapshot["files"]}
        for path, digests in transitions.items():
            rows[path]["sha256"] = digests[offset]
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert incomplete_retry_reconciliation_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not incomplete_retry_reconciliation_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_completed_retry_residue_cleanup() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    for snapshot, digest in (
        (
            cached,
            "sha256:709c35a877c5ced067661e55bf16ae30d4d0a542803e9a4606e7a2c57dadf53c",
        ),
        (
            current,
            "sha256:e6846a56cd2770b68af525a9b94f2dfd0bc156c0eb1340de9a849f3266a8d1fe",
        ),
    ):
        row = next(
            row
            for row in snapshot["files"]
            if row["path"]
            == "web/chanlun_chart/cl_app/services/trading_screening.py"
        )
        row["sha256"] = digest
        snapshot["aggregate_sha256"] = sha256_json(
            {"schema": snapshot["schema"], "files": snapshot["files"]}
        )

    assert completed_retry_residue_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not completed_retry_residue_source_migration_allowed(
        cached_decision_source_snapshot_id=current["aggregate_sha256"],
        current_decision_source_snapshot_id=cached["aggregate_sha256"],
        cached_decision_source_snapshot=current,
        current_decision_source_snapshot=cached,
    )


def test_manifest_migration_allows_exact_locator_admission_transition() -> None:
    current = current_decision_source_snapshot()
    cached = copy.deepcopy(current)
    current_row = next(
        row
        for row in current["files"]
        if row["path"] == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    cached_row = next(
        row
        for row in cached["files"]
        if row["path"] == "web/chanlun_chart/cl_app/services/trading_screening.py"
    )
    current_row["sha256"] = (
        "sha256:f314a453febeb7c5eaa63f73e74384d3c3f394cb267853098ac2ed0a278f84a5"
    )
    current["aggregate_sha256"] = sha256_json(
        {"schema": current["schema"], "files": current["files"]}
    )
    cached_row["sha256"] = (
        "sha256:e6846a56cd2770b68af525a9b94f2dfd0bc156c0eb1340de9a849f3266a8d1fe"
    )
    cached["aggregate_sha256"] = sha256_json(
        {"schema": cached["schema"], "files": cached["files"]}
    )

    assert orchestration_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )
    assert not completed_retry_residue_source_migration_allowed(
        cached_decision_source_snapshot_id=cached["aggregate_sha256"],
        current_decision_source_snapshot_id=current["aggregate_sha256"],
        cached_decision_source_snapshot=cached,
        current_decision_source_snapshot=current,
    )


def test_sector_snapshot_migration_allows_only_exact_reviewed_revision_pair() -> None:
    affinity_order_cached = (
        "sha256:3fe81e67380576efdc3ada6ed2dfc8e20cf0492483b3c8a740be817f0110e511"
    )
    affinity_order_current = (
        "sha256:5452b04068be4ab56e822c2871717ff0124040b6865fb2ae3eb015fd26834467"
    )
    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=affinity_order_cached,
        current_source_revision=affinity_order_current,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=affinity_order_current,
        current_source_revision=affinity_order_cached,
    )

    locked_time_rebuild_cached = (
        "sha256:909bc520565bdae72196f32c407cb254a4db725cc56f0f57d21e89fe69dd4a9b"
    )
    locked_time_rebuild_current = (
        "sha256:3fe81e67380576efdc3ada6ed2dfc8e20cf0492483b3c8a740be817f0110e511"
    )
    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=locked_time_rebuild_cached,
        current_source_revision=locked_time_rebuild_current,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=locked_time_rebuild_current,
        current_source_revision=locked_time_rebuild_cached,
    )

    priority_router_cached = (
        "sha256:835e8fd2046f70f882bd0f611cd2f64d63fc9857875b2110d466933df07dbc8d"
    )
    priority_router_current = (
        "sha256:909bc520565bdae72196f32c407cb254a4db725cc56f0f57d21e89fe69dd4a9b"
    )
    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=priority_router_cached,
        current_source_revision=priority_router_current,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=priority_router_current,
        current_source_revision=priority_router_cached,
    )

    cached = (
        "sha256:544bc1e62b74d754771c8764114d8c754f5fd4c91b9dededaa83e036538c1ac8"
    )
    current = (
        "sha256:c6c3e04ad2fcce74127fed58ee68ff39ffa1d3206218f70f4497c3950ea0a7d4"
    )

    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=cached,
        current_source_revision=current,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=current,
        current_source_revision=cached,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=cached,
        current_source_revision="sha256:" + "0" * 64,
    )

    current_cached = (
        "sha256:c6c3e04ad2fcce74127fed58ee68ff39ffa1d3206218f70f4497c3950ea0a7d4"
    )
    log_lifecycle = (
        "sha256:2a5e1822092334582e3480e6908e909f3bf5b9625ab273fd59b137d017f818b1"
    )
    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=current_cached,
        current_source_revision=log_lifecycle,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=log_lifecycle,
        current_source_revision=current_cached,
    )

    scheduler_cached = (
        "sha256:c4a0c23c76bca1f1108f18d342b47a08c91d6270f188df73ba8a54f145ec1cc8"
    )
    scheduler_current = (
        "sha256:fce0b2ae3d9fbdbe10ecf32bd6fa80dcc994155fae51d4eab7086985e29593ab"
    )
    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=scheduler_cached,
        current_source_revision=scheduler_current,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=scheduler_current,
        current_source_revision=scheduler_cached,
    )

    maintenance_cached = (
        "sha256:bb88417a5a59aafc1891512071d40f0f0432f4a26469b26aba709146b10216ab"
    )
    maintenance_current = (
        "sha256:fcb531d1e2940880845580d169999c5be7bc7d45875147c54605b38fc613bd9a"
    )
    assert sector_snapshot_source_migration_allowed(
        cached_source_revision=maintenance_cached,
        current_source_revision=maintenance_current,
    )
    assert not sector_snapshot_source_migration_allowed(
        cached_source_revision=maintenance_current,
        current_source_revision=maintenance_cached,
    )
