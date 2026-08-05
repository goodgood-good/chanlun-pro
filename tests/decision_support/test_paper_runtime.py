from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import Event, Thread
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import chanlun.decision_support.paper_runtime as paper_runtime_module

from chanlun.decision_support.paper_adapter import PaperBar
from chanlun.decision_support.paper_admission import (
    TrustedPaperAdmission,
    TrustedPaperAdmissionError,
)
from chanlun.decision_support.paper_runtime import (
    make_paper_pinned_codes_provider,
    PaperExitAnalysisCycle,
    PaperExitCoverage,
    PaperExitCycleResult,
    PaperResearchRuntime,
    register_paper_research_jobs,
    SQLitePaperRiskState,
    SQLiteTrustedPaperBarStore,
    TrustedPaperBarIntegrityError,
)
from chanlun.decision_support.exit_evaluation_store import (
    ExitEvaluationCommitment,
    ExitEvaluationSnapshot,
    ExitEvaluationService,
    SQLiteExitEvaluationStore,
)
from chanlun.decision_support.corpus_loader import load_certified_lesson_corpus
from chanlun.decision_support.exit_evidence_policy import (
    load_exit_evidence_policy_file,
)
from chanlun.decision_support.exit_runtime import (
    AuthoritativeEntryLink,
    TrackedPosition,
)
from chanlun.decision_support.models import EventState
from chanlun.decision_support.risk import QuoteSnapshot, RiskPolicy
from chanlun.decision_support.scanner import SymbolStructureSnapshot


CN = ZoneInfo("Asia/Shanghai")
_CALENDAR_FINGERPRINT = "sha256:" + "e" * 64


class _LeaseProbe:
    def __init__(self) -> None:
        self.active: list[str] = []
        self.entered: list[str] = []
        self.exited: list[str] = []

    def status_payload(self) -> dict[str, object]:
        return {
            "state": "active",
            "evidence_scope": "current_epoch_only",
            "store_bindings_complete": True,
        }

    def mutation_lease(self, operation: str):
        probe = self

        class LeaseContext:
            def __enter__(self):
                probe.active.append(operation)
                probe.entered.append(operation)
                return object()

            def __exit__(self, *_args):
                assert probe.active.pop() == operation
                probe.exited.append(operation)

        return LeaseContext()


class _Calendar:
    def __init__(
        self,
        trading_days: tuple[date, ...],
        *,
        fingerprint: str = _CALENDAR_FINGERPRINT,
    ) -> None:
        self.fingerprint = fingerprint
        self._trading_days = tuple(trading_days)

    @staticmethod
    def _closes(trading_day: date) -> tuple[datetime, ...]:
        values: list[datetime] = []
        closed_at = datetime.combine(trading_day, time(9, 35), CN)
        while closed_at.time() <= time(11, 30):
            values.append(closed_at)
            closed_at += timedelta(minutes=5)
        closed_at = datetime.combine(trading_day, time(13, 5), CN)
        while closed_at.time() <= time(15, 0):
            values.append(closed_at)
            closed_at += timedelta(minutes=5)
        return tuple(values)

    def session_for(self, trading_day: date):
        if trading_day not in self._trading_days:
            return None
        position = self._trading_days.index(trading_day)
        previous = None if position == 0 else self._trading_days[position - 1]
        return SimpleNamespace(
            trading_day=trading_day,
            previous_trading_day=previous,
            expected_bar_closes=self._closes(trading_day),
            calendar_fingerprint=self.fingerprint,
        )


def _bar(closed_at: datetime, *, code: str = "SH.600001") -> PaperBar:
    return PaperBar(
        code=code,
        opened_at=closed_at - timedelta(minutes=5),
        closed_at=closed_at,
        open_price=Decimal("10.25"),
        close_price=Decimal("10.40"),
        previous_close=Decimal("10.00"),
        max_fill_shares=1000,
    )


def _exit_snapshot(
    closed_at: datetime,
    *,
    entry_event_id: str = "entry-event-1",
    cycle_digit: str = "4",
) -> ExitEvaluationSnapshot:
    return ExitEvaluationSnapshot(
        entry_event_id=entry_event_id,
        evaluation_cycle_id="sha256:" + cycle_digit * 64,
        entry_provenance_fingerprint="sha256:" + "5" * 64,
        exit_evidence_policy_fingerprint="sha256:" + "6" * 64,
        certified_corpus_manifest_fingerprint="sha256:" + "7" * 64,
        source_pdf_fingerprint="sha256:" + "8" * 64,
        bar_structure_payload_fingerprint="sha256:" + "9" * 64,
        risk_context_payload_fingerprint="sha256:" + "a" * 64,
        quote_payload_fingerprint="sha256:" + "b" * 64,
        algorithm_version="chanlun-exit-runtime-v2",
        evaluation_version=2,
        recommendation_payload={"action": "observe", "urgency": "none"},
        evaluated_at=closed_at,
    )


def test_complete_cycle_uses_incremental_exit_anchor_validation(
    tmp_path,
    monkeypatch,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    full_validation_calls = 0
    scanned_anchor_rows = 0
    original_validate_log = SQLiteTrustedPaperBarStore._validate_exit_manifest_log
    original_validate_anchors = (
        SQLiteTrustedPaperBarStore._validate_exit_manifest_anchors
    )

    def count_full_validation(self, connection):
        nonlocal full_validation_calls
        full_validation_calls += 1
        return original_validate_log(self, connection)

    def count_anchor_history(self, rows):
        nonlocal scanned_anchor_rows
        scanned_anchor_rows += len(rows)
        return original_validate_anchors(self, rows)

    monkeypatch.setattr(
        SQLiteTrustedPaperBarStore,
        "_validate_exit_manifest_log",
        count_full_validation,
    )
    monkeypatch.setattr(
        SQLiteTrustedPaperBarStore,
        "_validate_exit_manifest_anchors",
        count_anchor_history,
    )
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "incremental-exit-anchor.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )

    closes = _Calendar._closes(trading_day)[:12]
    for closed_at in closes:
        store.record_cycle(
            session=calendar.session_for(trading_day),
            bar_closed_at=closed_at,
            required_codes=(),
            optional_codes=("SH.600001",),
            bars={"SH.600001": _bar(closed_at)},
            optional_failures={},
        )
        store.complete_cycle(closed_at, exit_commitments=())

    assert full_validation_calls == 1
    assert scanned_anchor_rows == 0
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*), SUM(completed) FROM trusted_paper_bar_cycle"
        ).fetchone() == (12, 12)
        assert connection.execute(
            """
            SELECT event_count, max_sequence
            FROM trusted_paper_exit_manifest_log_state
            WHERE singleton_id = 1
            """
        ).fetchone() == (12, 12)


def test_incremental_exit_manifest_cache_rebases_after_external_append(
    tmp_path,
    monkeypatch,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    closes = _Calendar._closes(trading_day)[:3]
    path = tmp_path / "incremental-exit-external-append.sqlite3"
    first = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )

    def append(store, closed_at):
        store.record_cycle(
            session=calendar.session_for(trading_day),
            bar_closed_at=closed_at,
            required_codes=(),
            optional_codes=("SH.600001",),
            bars={"SH.600001": _bar(closed_at)},
            optional_failures={},
        )
        store.complete_cycle(closed_at, exit_commitments=())

    append(first, closes[0])
    full_rebases = 0
    original_validate = first._validate_exit_manifest_log

    def count_full_rebase(connection):
        nonlocal full_rebases
        full_rebases += 1
        return original_validate(connection)

    monkeypatch.setattr(
        first,
        "_validate_exit_manifest_log",
        count_full_rebase,
    )
    second = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    append(second, closes[1])
    append(first, closes[2])

    assert full_rebases == 1
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT event_count, max_sequence "
            "FROM trusted_paper_exit_manifest_log_state"
        ).fetchone() == (3, 3)


def test_incremental_exit_manifest_rejects_tampered_tail_anchor(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    closes = _Calendar._closes(trading_day)[:2]
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "incremental-exit-tail-tamper.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    for closed_at in closes:
        store.record_cycle(
            session=calendar.session_for(trading_day),
            bar_closed_at=closed_at,
            required_codes=(),
            optional_codes=("SH.600001",),
            bars={"SH.600001": _bar(closed_at)},
            optional_failures={},
        )
        if closed_at == closes[0]:
            store.complete_cycle(closed_at, exit_commitments=())

    prefix = store._exit_manifest_validated_prefix
    assert prefix is not None
    store._exit_manifest_anchor_path(
        prefix.max_sequence,
        prefix.history_head_sha256,
    ).unlink()

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_exit_manifest_anchor_mismatch",
    ):
        store.complete_cycle(closes[1], exit_commitments=())

    assert store._exit_manifest_validated_prefix is None
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_paper_exit_manifest"
        ).fetchone() == (1,)
        assert connection.execute(
            """
            SELECT completed FROM trusted_paper_bar_cycle
            WHERE closed_at = ?
            """,
            (closes[1].isoformat(),),
        ).fetchone() == (0,)


def test_incremental_exit_manifest_cache_advances_only_after_commit(
    tmp_path,
    monkeypatch,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    closes = _Calendar._closes(trading_day)[:2]
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "incremental-exit-commit-failure.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    for closed_at in closes:
        store.record_cycle(
            session=calendar.session_for(trading_day),
            bar_closed_at=closed_at,
            required_codes=(),
            optional_codes=("SH.600001",),
            bars={"SH.600001": _bar(closed_at)},
            optional_failures={},
        )
    store.complete_cycle(closes[0], exit_commitments=())
    committed_prefix = store._exit_manifest_validated_prefix
    original_write_anchor = store._write_exit_manifest_anchor

    def fail_anchor_write(**_kwargs):
        raise OSError("forced exit anchor write failure")

    monkeypatch.setattr(
        store,
        "_write_exit_manifest_anchor",
        fail_anchor_write,
    )
    with pytest.raises(OSError, match="forced exit anchor write failure"):
        store.complete_cycle(closes[1], exit_commitments=())

    assert store._exit_manifest_validated_prefix == committed_prefix
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT event_count, max_sequence "
            "FROM trusted_paper_exit_manifest_log_state"
        ).fetchone() == (1, 1)
    monkeypatch.setattr(
        store,
        "_write_exit_manifest_anchor",
        original_write_anchor,
    )
    store.complete_cycle(closes[1], exit_commitments=())
    assert store._exit_manifest_validated_prefix != committed_prefix


def test_paper_bar_hot_path_indexes_avoid_full_scans(tmp_path) -> None:
    store = SQLiteTrustedPaperBarStore(tmp_path / "paper-hot-indexes.sqlite3")

    with sqlite3.connect(store.path) as connection:
        segment_plan = "\n".join(
            row[3]
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT c.trading_day, c.slot_index
                FROM trusted_paper_bar_segment_member AS m
                JOIN trusted_paper_bar_cycle AS c
                  ON c.closed_at = m.closed_at
                WHERE m.segment_id = ?
                ORDER BY m.closed_at DESC LIMIT 1
                """,
                ("segment:test",),
            )
        )
        observation_plan = "\n".join(
            row[3]
            for row in connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT payload_json, payload_sha256
                FROM trusted_signal_observation_cycle
                WHERE closed_at = ?
                """,
                ("2026-07-14T09:35:00+08:00",),
            )
        )

    assert "ix_trusted_bar_segment_member_tail" in segment_plan
    assert "SCAN m" not in segment_plan
    assert "USE TEMP B-TREE" not in segment_plan
    assert "ix_trusted_signal_observation_closed" in observation_plan
    assert "SCAN trusted_signal_observation_cycle" not in observation_plan


def test_exit_manifest_publishes_only_exact_committed_snapshot(tmp_path) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "exit-manifest.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )
    committed = _exit_snapshot(closed_at)
    raw_after_completion = _exit_snapshot(
        closed_at,
        entry_event_id="entry-event-raw",
        cycle_digit="d",
    )

    store.complete_cycle(
        closed_at,
        exit_commitments=(ExitEvaluationCommitment.from_snapshot(committed),),
    )

    assert store.is_exit_snapshot_committed(committed) is True
    assert store.is_exit_snapshot_committed(raw_after_completion) is False


def test_strategy_bind_and_required_exit_manifest_publish_atomically(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "exit-manifest-bind-race.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )

    class ActiveRun:
        run_id = "run:exit-manifest-bind-race"
        epoch = 1
        strategy_run_fingerprint = "sha256:" + "1" * 64

        def __init__(self) -> None:
            self.held = False
            self.store_paths = {"bar": store.path}
            self.store_bindings = {
                "bar": SimpleNamespace(
                    run_id=self.run_id,
                    epoch=self.epoch,
                    strategy_run_fingerprint=self.strategy_run_fingerprint,
                    identity_sha256="sha256:" + "2" * 64,
                    store_role="bar",
                    store_instance_id="store:exit-manifest-bind-race",
                )
            }

        def mutation_lease(self, _operation: str):
            active = self

            class Lease:
                def __enter__(self):
                    active.held = True

                def __exit__(self, *_args):
                    active.held = False

            return Lease()

        def require_current_mutation_lease(self) -> None:
            if not self.held:
                raise RuntimeError("strategy_run_mutation_lease_required")

    active = ActiveRun()
    guard_bound = Event()
    release_bind = Event()
    complete_started = Event()
    original_bind = store._mutation_fence.bind

    def pausing_bind(*args, **kwargs):
        original_bind(*args, **kwargs)
        guard_bound.set()
        assert release_bind.wait(5)

    store._mutation_fence.bind = pausing_bind  # type: ignore[method-assign]
    bind_thread = Thread(target=store.bind_strategy_run, args=(active,))
    bind_thread.start()
    assert guard_bound.wait(5)
    errors: list[Exception] = []

    def complete_without_manifest() -> None:
        complete_started.set()
        try:
            store.complete_cycle(closed_at)
        except Exception as exc:  # pragma: no branch - asserted below
            errors.append(exc)

    complete_thread = Thread(target=complete_without_manifest)
    complete_thread.start()
    assert complete_started.wait(5)
    release_bind.set()
    bind_thread.join(5)
    complete_thread.join(5)

    assert not bind_thread.is_alive()
    assert not complete_thread.is_alive()
    assert len(errors) == 1
    assert "paper_exit_manifest_required" in str(errors[0])
    assert store.is_cycle_complete(closed_at) is False
    store.complete_cycle(closed_at, exit_commitments=())
    with sqlite3.connect(store.path) as connection:
        manifest_json = connection.execute(
            """
            SELECT payload_json FROM trusted_paper_exit_manifest
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()[0]
    manifest = json.loads(manifest_json)
    assert manifest["commitments"] == []
    assert manifest["strategy_run"] == {
        "run_id": active.run_id,
        "epoch": active.epoch,
        "strategy_run_fingerprint": active.strategy_run_fingerprint,
        "identity_sha256": "sha256:" + "2" * 64,
        "store_instance_id": "store:exit-manifest-bind-race",
    }


@pytest.mark.parametrize(
    ("column", "tampered_value"),
    (
        ("closed_at", "2026-07-14T09:40:00+08:00"),
        ("payload_json", "{}"),
        ("payload_sha256", "sha256:" + "f" * 64),
    ),
)
def test_exit_manifest_tamper_fails_closed(
    tmp_path,
    column: str,
    tampered_value: str,
) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    path = tmp_path / f"exit-manifest-tamper-{column}.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )
    snapshot = _exit_snapshot(closed_at)
    commitment = ExitEvaluationCommitment.from_snapshot(snapshot)
    store.complete_cycle(closed_at, exit_commitments=(commitment,))
    with sqlite3.connect(path) as connection:
        connection.execute(
            f"UPDATE trusted_paper_exit_manifest SET {column} = ?",
            (tampered_value,),
        )

    assert store.is_exit_snapshot_committed(snapshot) is False
    assert store.health().degraded is True


def test_completed_cycle_replay_rejects_different_exit_commitment(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "exit-manifest-replay.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )
    committed = ExitEvaluationCommitment.from_snapshot(
        _exit_snapshot(closed_at)
    )
    different = ExitEvaluationCommitment.from_snapshot(
        _exit_snapshot(
            closed_at,
            entry_event_id="entry-event-different",
            cycle_digit="d",
        )
    )
    store.complete_cycle(closed_at, exit_commitments=(committed,))
    store.complete_cycle(closed_at, exit_commitments=(committed,))

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_exit_manifest_replay_mismatch",
    ):
        store.complete_cycle(closed_at, exit_commitments=(different,))

    assert store.is_exit_snapshot_committed(
        _exit_snapshot(closed_at)
    ) is True


def test_exit_manifest_rejects_commitment_from_different_evaluated_at(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "exit-manifest-stale-cycle.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )
    stale = ExitEvaluationCommitment.from_snapshot(
        _exit_snapshot(closed_at + timedelta(minutes=5))
    )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_exit_manifest_cycle_mismatch",
    ):
        store.complete_cycle(closed_at, exit_commitments=(stale,))

    assert store.is_cycle_complete(closed_at) is False


def test_exit_manifest_consistent_sql_rewrite_is_rejected_by_external_anchor(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    path = tmp_path / "exit-manifest-consistent-rewrite.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )
    committed_snapshot = _exit_snapshot(closed_at)
    committed = ExitEvaluationCommitment.from_snapshot(committed_snapshot)
    store.complete_cycle(closed_at, exit_commitments=(committed,))
    forged_snapshot = _exit_snapshot(
        closed_at,
        entry_event_id="entry-event-forged",
        cycle_digit="d",
    )
    forged = ExitEvaluationCommitment.from_snapshot(forged_snapshot)
    with sqlite3.connect(path) as connection:
        manifest_json = connection.execute(
            """
            SELECT payload_json FROM trusted_paper_exit_manifest
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()[0]
        payload = json.loads(manifest_json)
        payload["commitments"] = [
            item.to_dict() for item in sorted((committed, forged))
        ]
        rewritten_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        rewritten_sha = paper_runtime_module._payload_sha256(rewritten_json)
        connection.execute(
            """
            UPDATE trusted_paper_exit_manifest
            SET payload_json = ?, payload_sha256 = ?
            WHERE closed_at = ?
            """,
            (rewritten_json, rewritten_sha, closed_at.isoformat()),
        )
        log_state = {
            "schema_version": 1,
            "event_count": 1,
            "max_sequence": 1,
            "history_head_sha256": rewritten_sha,
        }
        connection.execute(
            """
            UPDATE trusted_paper_exit_manifest_log_state
            SET event_count = 1, max_sequence = 1,
                history_head_sha256 = ?, payload_sha256 = ?
            WHERE singleton_id = 1
            """,
            (rewritten_sha, paper_runtime_module.sha256_json(log_state)),
        )

    assert store.is_exit_snapshot_committed(forged_snapshot) is False
    assert store.health().degraded is True


def test_exit_manifest_physical_schema_requires_close_primary_key_and_sequence_unique(
    tmp_path,
) -> None:
    path = tmp_path / "exit-manifest-invalid-schema.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE trusted_paper_exit_manifest (
                closed_at TEXT NOT NULL,
                manifest_sequence INTEGER NOT NULL,
                previous_manifest_sha256 TEXT,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL
            )
            """
        )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_exit_manifest_schema_invalid",
    ):
        SQLiteTrustedPaperBarStore(path)


def test_strategy_bind_rejects_historical_unbound_exit_manifest_immediately(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "exit-manifest-bind-history.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )
    store.complete_cycle(closed_at, exit_commitments=())
    active = _signal_observation_strategy_run(store)
    active.mutation_lease = lambda _operation: nullcontext()
    active.require_current_mutation_lease = lambda: None

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_exit_manifest_strategy_mismatch",
    ):
        store.bind_strategy_run(active)


def test_completed_cycle_missing_exit_manifest_fails_closed(tmp_path) -> None:
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    calendar = _Calendar((closed_at.date(),))
    path = tmp_path / "exit-manifest-missing.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(),
        optional_codes=("SH.600001",),
        bars={"SH.600001": _bar(closed_at)},
        optional_failures={},
    )
    snapshot = _exit_snapshot(closed_at)
    store.complete_cycle(
        closed_at,
        exit_commitments=(ExitEvaluationCommitment.from_snapshot(snapshot),),
    )
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM trusted_paper_exit_manifest")

    assert store.is_cycle_complete(closed_at) is False
    assert store.is_exit_snapshot_committed(snapshot) is False
    assert store.health().degraded is True


def test_orphan_exit_manifest_fails_closed(tmp_path) -> None:
    path = tmp_path / "exit-manifest-orphan.sqlite3"
    store = SQLiteTrustedPaperBarStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO trusted_paper_exit_manifest (
                closed_at, manifest_sequence, previous_manifest_sha256,
                payload_json, payload_sha256
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime(2026, 7, 14, 9, 35, tzinfo=CN).isoformat(),
                1,
                None,
                "{}",
                "sha256:" + "f" * 64,
            ),
        )

    assert store.health().degraded is True


def _signal_observation_strategy_run(
    store: SQLiteTrustedPaperBarStore,
) -> SimpleNamespace:
    run_id = "run:signal-observation-fixture"
    strategy_fingerprint = "sha256:" + "1" * 64
    identity_sha256 = "sha256:" + "2" * 64
    store_instance_id = "store:signal-observation-fixture"
    binding = SimpleNamespace(
        run_id=run_id,
        epoch=1,
        strategy_run_fingerprint=strategy_fingerprint,
        identity_sha256=identity_sha256,
        store_role="bar",
        store_instance_id=store_instance_id,
    )
    return SimpleNamespace(
        run_id=run_id,
        epoch=1,
        strategy_run_fingerprint=strategy_fingerprint,
        store_bindings={"bar": binding},
        store_paths={"bar": store.path},
        status_payload=lambda: {
            "run_id": run_id,
            "epoch": 1,
            "fingerprint": strategy_fingerprint,
            "state": "active",
            "evidence_scope": "current_epoch_only",
            "store_bindings_complete": True,
        },
    )


def _record_signal_observation_cycle(
    store: SQLiteTrustedPaperBarStore,
    calendar: _Calendar,
    closed_at: datetime,
    signal_fingerprints: tuple[str, ...],
    *,
    required: bool = True,
):
    code = "SH.600001"
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(code,) if required else (),
        optional_codes=() if required else (code,),
        bars={code: _bar(closed_at, code=code)},
        optional_failures={},
    )
    return store.prepare_signal_observation_batch(
        closed_at,
        {code: signal_fingerprints} if required else {},
    )


def test_signal_observation_restart_retains_baseline_and_true_first_seen(
    tmp_path,
) -> None:
    path = tmp_path / "signal-observation-restart.sqlite3"
    calendar = _Calendar((date(2026, 7, 14),))
    code = "SH.600001"
    baseline_signal = paper_runtime_module.sha256_json({"signal": "baseline"})
    new_signal = paper_runtime_module.sha256_json({"signal": "new"})
    first_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    second_at = first_at + timedelta(minutes=5)
    third_at = second_at + timedelta(minutes=5)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )

    baseline = _record_signal_observation_cycle(
        store,
        calendar,
        first_at,
        (baseline_signal,),
    )
    assert baseline.states == {
        code: {baseline_signal: "baseline_not_fresh"}
    }
    assert baseline.first_observed_at == {
        code: {baseline_signal: first_at}
    }
    store.complete_cycle(first_at, signal_observation_batch=baseline)

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(restarted)
    )
    newly_observed = _record_signal_observation_cycle(
        restarted,
        calendar,
        second_at,
        (baseline_signal, new_signal),
    )
    assert newly_observed.states == {
        code: {
            baseline_signal: "baseline_not_fresh",
            new_signal: "trusted_first_seen",
        }
    }
    assert newly_observed.first_observed_at == {
        code: {
            baseline_signal: first_at,
            new_signal: second_at,
        }
    }
    restarted.complete_cycle(
        second_at,
        signal_observation_batch=newly_observed,
    )

    twice_restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    twice_restarted.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(twice_restarted)
    )
    retained = _record_signal_observation_cycle(
        twice_restarted,
        calendar,
        third_at,
        (baseline_signal, new_signal),
    )
    assert retained.states[code][new_signal] == "trusted_first_seen"
    assert retained.first_observed_at[code][new_signal] == second_at


def test_signal_observation_incomplete_gap_quarantines_unknown_signal(
    tmp_path,
) -> None:
    path = tmp_path / "signal-observation-gap.sqlite3"
    calendar = _Calendar((date(2026, 7, 14),))
    code = "SH.600001"
    signal = paper_runtime_module.sha256_json({"signal": "after-gap"})
    first_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    failed_at = first_at + timedelta(minutes=5)
    recovered_at = failed_at + timedelta(minutes=5)
    next_at = recovered_at + timedelta(minutes=5)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    first = _record_signal_observation_cycle(store, calendar, first_at, ())
    store.complete_cycle(first_at, signal_observation_batch=first)

    _record_signal_observation_cycle(store, calendar, failed_at, (signal,))
    store.fail_cycle_attempt(failed_at, "injected_failure")
    recovered = _record_signal_observation_cycle(
        store,
        calendar,
        recovered_at,
        (signal,),
    )

    assert recovered.states == {
        code: {signal: "quarantined_unknown"}
    }
    assert recovered.first_observed_at == {code: {}}
    store.complete_cycle(recovered_at, signal_observation_batch=recovered)
    with sqlite3.connect(path) as connection:
        recovered_payload = json.loads(
            connection.execute(
                """
                SELECT payload_json FROM trusted_signal_observation_cycle
                WHERE run_id = ? AND closed_at = ?
                """,
                (recovered.run_id, recovered_at.isoformat()),
            ).fetchone()[0]
        )
    recovered_manifest = recovered_payload["manifests"][code]
    assert recovered_manifest["states"] == {
        signal: "quarantined_unknown"
    }
    assert recovered_manifest["segment_first_observed_at"] == {
        signal: None
    }

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(restarted)
    )
    retained = _record_signal_observation_cycle(
        restarted,
        calendar,
        next_at,
        (signal,),
    )
    assert retained.segment_ids == recovered.segment_ids
    assert retained.states == {
        code: {signal: "quarantined_unknown"}
    }
    assert retained.first_observed_at == {code: {}}

    with sqlite3.connect(path) as connection:
        segment_row = connection.execute(
            """
            SELECT observation_state, first_observed_at
            FROM trusted_signal_segment_observation
            WHERE run_id = ? AND segment_id = ? AND code = ?
              AND signal_fingerprint = ?
            """,
            (
                retained.run_id,
                retained.segment_ids[code],
                code,
                signal,
            ),
        ).fetchone()
    assert segment_row == ("quarantined_unknown", None)


def test_signal_observation_failed_gap_reappearance_stays_quarantined(
    tmp_path,
) -> None:
    path = tmp_path / "signal-observation-gap-reappearance.sqlite3"
    calendar = _Calendar((date(2026, 7, 14),))
    code = "SH.600001"
    signal = paper_runtime_module.sha256_json({"signal": "gap-reappearance"})
    baseline_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    failed_at = baseline_at + timedelta(minutes=5)
    empty_recovery_at = failed_at + timedelta(minutes=5)
    reappeared_at = empty_recovery_at + timedelta(minutes=5)
    retained_at = reappeared_at + timedelta(minutes=5)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )

    baseline = _record_signal_observation_cycle(
        store,
        calendar,
        baseline_at,
        (),
    )
    store.complete_cycle(baseline_at, signal_observation_batch=baseline)
    failed = _record_signal_observation_cycle(
        store,
        calendar,
        failed_at,
        (signal,),
    )
    assert failed.states == {code: {signal: "trusted_first_seen"}}
    store.fail_cycle_attempt(failed_at, "injected_failure")

    empty_recovery = _record_signal_observation_cycle(
        store,
        calendar,
        empty_recovery_at,
        (),
    )
    store.complete_cycle(
        empty_recovery_at,
        signal_observation_batch=empty_recovery,
    )
    reappeared = _record_signal_observation_cycle(
        store,
        calendar,
        reappeared_at,
        (signal,),
    )

    assert reappeared.states == {
        code: {signal: "quarantined_unknown"}
    }
    assert reappeared.first_observed_at == {code: {}}
    store.complete_cycle(reappeared_at, signal_observation_batch=reappeared)

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(restarted)
    )
    retained = _record_signal_observation_cycle(
        restarted,
        calendar,
        retained_at,
        (signal,),
    )
    assert retained.segment_ids == reappeared.segment_ids
    assert retained.states == {
        code: {signal: "quarantined_unknown"}
    }
    assert retained.first_observed_at == {code: {}}
    restarted.complete_cycle(retained_at, signal_observation_batch=retained)
    assert restarted.is_cycle_complete(retained_at) is True


def test_signal_observation_same_cycle_failed_retry_cannot_erase_prepared_signal(
    tmp_path,
) -> None:
    path = tmp_path / "signal-observation-same-cycle-retry.sqlite3"
    calendar = _Calendar((date(2026, 7, 14),))
    code = "SH.600001"
    signal = paper_runtime_module.sha256_json({"signal": "failed-retry"})
    baseline_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    retried_at = baseline_at + timedelta(minutes=5)
    reappeared_at = retried_at + timedelta(minutes=5)
    retained_at = reappeared_at + timedelta(minutes=5)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )

    baseline = _record_signal_observation_cycle(
        store,
        calendar,
        baseline_at,
        (),
    )
    store.complete_cycle(baseline_at, signal_observation_batch=baseline)
    failed = _record_signal_observation_cycle(
        store,
        calendar,
        retried_at,
        (signal,),
    )
    assert failed.states == {code: {signal: "trusted_first_seen"}}
    store.fail_cycle_attempt(retried_at, "injected_failure")

    empty_retry = _record_signal_observation_cycle(
        store,
        calendar,
        retried_at,
        (),
    )
    store.complete_cycle(retried_at, signal_observation_batch=empty_retry)
    reappeared = _record_signal_observation_cycle(
        store,
        calendar,
        reappeared_at,
        (signal,),
    )

    assert reappeared.states == {
        code: {signal: "quarantined_unknown"}
    }
    assert reappeared.first_observed_at == {code: {}}
    store.complete_cycle(reappeared_at, signal_observation_batch=reappeared)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT attempt_generation FROM trusted_paper_bar_attempt
            WHERE closed_at = ?
            """,
            (retried_at.isoformat(),),
        ).fetchone() == (2,)

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(restarted)
    )
    retained = _record_signal_observation_cycle(
        restarted,
        calendar,
        retained_at,
        (signal,),
    )
    assert retained.states == {
        code: {signal: "quarantined_unknown"}
    }
    assert retained.first_observed_at == {code: {}}


def test_explicit_cycle_attempt_start_increments_generation_monotonically(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "explicit-attempt-generation.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    code = "SH.600001"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )

    def attempt_state() -> tuple[str, int]:
        with sqlite3.connect(path) as connection:
            return connection.execute(
                """
                SELECT status, attempt_generation
                FROM trusted_paper_bar_attempt WHERE closed_at = ?
                """,
                (closed_at.isoformat(),),
            ).fetchone()

    store.start_cycle_attempt(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
    )
    assert attempt_state() == ("started", 1)
    store.start_cycle_attempt(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
    )
    assert attempt_state() == ("started", 2)
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(closed_at, code=code)},
        optional_failures={},
    )
    assert attempt_state() == ("started", 2)
    store.fail_cycle_attempt(closed_at, "injected_failure")
    store.start_cycle_attempt(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
    )
    assert attempt_state() == ("started", 3)


def test_signal_observation_new_required_segment_is_baseline_but_keeps_history(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-segment.sqlite3"
    code = "SH.600001"
    old_signal = paper_runtime_module.sha256_json({"signal": "old"})
    new_signal = paper_runtime_module.sha256_json({"signal": "new-segment"})
    first_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    optional_at = first_at + timedelta(minutes=5)
    returned_at = optional_at + timedelta(minutes=5)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    first = _record_signal_observation_cycle(
        store,
        calendar,
        first_at,
        (old_signal,),
    )
    store.complete_cycle(first_at, signal_observation_batch=first)
    optional = _record_signal_observation_cycle(
        store,
        calendar,
        optional_at,
        (),
        required=False,
    )
    store.complete_cycle(optional_at, signal_observation_batch=optional)

    returned = _record_signal_observation_cycle(
        store,
        calendar,
        returned_at,
        (old_signal, new_signal),
    )

    assert returned.states == {
        code: {
            old_signal: "baseline_not_fresh",
            new_signal: "baseline_not_fresh",
        }
    }
    assert returned.first_observed_at == {
        code: {
            old_signal: returned_at,
            new_signal: returned_at,
        }
    }


def test_signal_observation_prior_trusted_signal_rebaselines_in_new_required_segment(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-trusted-segment.sqlite3"
    code = "SH.600001"
    signal = paper_runtime_module.sha256_json({"signal": "prior-trusted"})
    baseline_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    trusted_at = baseline_at + timedelta(minutes=5)
    optional_at = trusted_at + timedelta(minutes=5)
    returned_at = optional_at + timedelta(minutes=5)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )

    baseline = _record_signal_observation_cycle(
        store,
        calendar,
        baseline_at,
        (),
    )
    store.complete_cycle(baseline_at, signal_observation_batch=baseline)
    trusted = _record_signal_observation_cycle(
        store,
        calendar,
        trusted_at,
        (signal,),
    )
    assert trusted.states == {code: {signal: "trusted_first_seen"}}
    assert trusted.first_observed_at == {code: {signal: trusted_at}}
    store.complete_cycle(trusted_at, signal_observation_batch=trusted)
    optional = _record_signal_observation_cycle(
        store,
        calendar,
        optional_at,
        (),
        required=False,
    )
    store.complete_cycle(optional_at, signal_observation_batch=optional)

    returned = _record_signal_observation_cycle(
        store,
        calendar,
        returned_at,
        (signal,),
    )

    assert returned.segment_ids[code] != trusted.segment_ids[code]
    assert returned.states == {code: {signal: "baseline_not_fresh"}}
    assert returned.first_observed_at == {code: {signal: returned_at}}
    store.complete_cycle(returned_at, signal_observation_batch=returned)
    assert store.is_cycle_complete(returned_at) is True

    with sqlite3.connect(path) as connection:
        global_row = connection.execute(
            """
            SELECT first_observed_at, first_segment_id, observation_state
            FROM trusted_signal_first_observation
            WHERE run_id = ? AND code = ? AND signal_fingerprint = ?
            """,
            (returned.run_id, code, signal),
        ).fetchone()
        segment_row = connection.execute(
            """
            SELECT first_observed_at, observation_state
            FROM trusted_signal_segment_observation
            WHERE run_id = ? AND segment_id = ? AND code = ?
              AND signal_fingerprint = ?
            """,
            (
                returned.run_id,
                returned.segment_ids[code],
                code,
                signal,
            ),
        ).fetchone()
    assert global_row == (
        trusted_at.isoformat(),
        trusted.segment_ids[code],
        "trusted_first_seen",
    )
    assert segment_row == (returned_at.isoformat(), "baseline_not_fresh")


def test_signal_observation_cycle_v3_binds_segment_authority_payload(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-cycle-v3.sqlite3"
    code = "SH.600001"
    signal = paper_runtime_module.sha256_json({"signal": "v3-authority"})
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (signal,),
    )
    store.complete_cycle(closed_at, signal_observation_batch=prepared)

    with sqlite3.connect(path) as connection:
        payload_json = connection.execute(
            """
            SELECT payload_json FROM trusted_signal_observation_cycle
            WHERE run_id = ? AND closed_at = ?
            """,
            (prepared.run_id, closed_at.isoformat()),
        ).fetchone()[0]
        segment_payload_sha256 = connection.execute(
            """
            SELECT payload_sha256
            FROM trusted_signal_segment_observation
            WHERE run_id = ? AND segment_id = ? AND code = ?
              AND signal_fingerprint = ?
            """,
            (
                prepared.run_id,
                prepared.segment_ids[code],
                code,
                signal,
            ),
        ).fetchone()[0]
    payload = json.loads(payload_json)
    manifest = payload["manifests"][code]
    assert payload["schema_version"] == 4
    assert payload["attempt_generation"] == 1
    assert payload["prior_attempt_ambiguous"] is False
    assert payload["resolution_sha256"] == prepared.resolution_sha256
    assert manifest["states"] == {signal: "baseline_not_fresh"}
    assert manifest["segment_first_observed_at"] == {
        signal: closed_at.isoformat()
    }
    assert manifest["segment_payload_sha256"] == {
        signal: segment_payload_sha256
    }


@pytest.mark.parametrize("tamper_mode", ("state", "missing"))
def test_signal_segment_authority_payload_tamper_fails_closed(
    tmp_path,
    tamper_mode,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-segment-tamper.sqlite3"
    code = "SH.600001"
    signal = paper_runtime_module.sha256_json({"signal": "segment-tamper"})
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (signal,),
    )
    store.complete_cycle(closed_at, signal_observation_batch=prepared)
    with sqlite3.connect(path) as connection:
        arguments = (
            prepared.run_id,
            prepared.segment_ids[code],
            code,
            signal,
        )
        if tamper_mode == "state":
            connection.execute(
                """
                UPDATE trusted_signal_segment_observation
                SET observation_state = 'trusted_first_seen'
                WHERE run_id = ? AND segment_id = ? AND code = ?
                  AND signal_fingerprint = ?
                """,
                arguments,
            )
        else:
            connection.execute(
                """
                DELETE FROM trusted_signal_segment_observation
                WHERE run_id = ? AND segment_id = ? AND code = ?
                  AND signal_fingerprint = ?
                """,
                arguments,
            )

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    health = restarted.health()
    assert health.degraded is True
    assert health.degraded_reason == (
        "signal_segment_observation_integrity_failure"
    )


def test_signal_observation_complete_cycle_is_atomic_and_retryable(tmp_path) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-atomic.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    signal = paper_runtime_module.sha256_json({"signal": "atomic"})
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (signal,),
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER inject_signal_observation_crash
            BEFORE INSERT ON trusted_signal_observation_cycle
            BEGIN
                SELECT RAISE(FAIL, 'injected_signal_observation_crash');
            END
            """
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected_signal"):
        store.complete_cycle(
            closed_at,
            signal_observation_batch=prepared,
        )

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT completed FROM trusted_paper_bar_cycle WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM trusted_paper_bar_attempt WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == ("started",)
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_observation_cycle"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_first_observation"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_segment_observation"
        ).fetchone() == (0,)
        connection.execute("DROP TRIGGER inject_signal_observation_crash")

    store.complete_cycle(closed_at, signal_observation_batch=prepared)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT completed FROM trusted_paper_bar_cycle WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM trusted_paper_bar_attempt WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == ("complete",)
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_observation_cycle"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_first_observation"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_segment_observation"
        ).fetchone() == (1,)


def test_signal_observation_committed_batch_replay_is_idempotent_after_restart(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-idempotent-replay.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    signal = paper_runtime_module.sha256_json({"signal": "idempotent-replay"})
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (signal,),
    )
    store.complete_cycle(closed_at, signal_observation_batch=prepared)

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(restarted)
    )
    restarted.complete_cycle(closed_at, signal_observation_batch=prepared)

    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_observation_cycle"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM trusted_signal_first_observation"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT completed FROM trusted_paper_bar_cycle WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM trusted_paper_bar_attempt WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == ("complete",)
    anchor_dir = path.with_name(path.name + ".signal-observation-anchor")
    assert tuple(anchor_dir.glob("*.json")) != ()
    assert len(tuple(anchor_dir.glob("*.json"))) == 1

    forged_generation = replace(
        prepared,
        attempt_generation=2,
        prior_attempt_ambiguous=True,
    )
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_attempt_generation_mismatch",
    ):
        restarted.complete_cycle(
            closed_at,
            signal_observation_batch=forged_generation,
        )
    forged = replace(prepared, resolution_sha256="sha256:" + "9" * 64)
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="signal_observation_batch_resolution_changed",
    ):
        restarted.complete_cycle(closed_at, signal_observation_batch=forged)


def test_signal_observation_completed_replay_resolves_later_preflight_failure(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-preflight-resolution.sqlite3"
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    committed_observed_at = closed_at + timedelta(seconds=30)
    failed_at = closed_at + timedelta(seconds=40)
    replay_observed_at = closed_at + timedelta(seconds=50)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (),
    )
    store.complete_cycle(
        closed_at,
        calendar_observed_at=committed_observed_at,
        signal_observation_batch=prepared,
    )
    store.record_calendar_preflight_failure(
        failed_at=failed_at,
        reason="calendar_backend_unavailable",
    )

    store.complete_cycle(
        closed_at,
        calendar_observed_at=replay_observed_at,
        signal_observation_batch=prepared,
    )

    assert store.health().calendar_preflight_failure_at is None
    with sqlite3.connect(path) as connection:
        failure_row = connection.execute(
            """
            SELECT failed_at, resolved_at, resolved_by_bar_closed_at,
                   payload_sha256
            FROM trusted_paper_bar_calendar_preflight
            """
        ).fetchone()
        resolution_row = connection.execute(
            """
            SELECT resolved_at, resolved_by_bar_closed_at, payload_sha256
            FROM trusted_paper_bar_calendar_preflight_resolution
            """
        ).fetchone()
    assert failure_row[:3] == (failed_at.isoformat(), None, None)
    assert failure_row[3].startswith("sha256:")
    assert resolution_row[:2] == (
        replay_observed_at.isoformat(),
        closed_at.isoformat(),
    )
    assert resolution_row[2].startswith("sha256:")


def test_signal_observation_completed_replay_clears_only_committed_preflight_watermark(
    tmp_path,
    monkeypatch,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-preflight-replay.sqlite3"
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    failed_at = closed_at + timedelta(seconds=10)
    committed_observed_at = closed_at + timedelta(seconds=30)
    newer_failed_at = closed_at + timedelta(seconds=50)
    replay_observed_at = closed_at + timedelta(seconds=70)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_calendar_preflight_insert
            BEFORE INSERT ON trusted_paper_bar_calendar_preflight
            BEGIN
                SELECT RAISE(FAIL, 'injected_preflight_write_failure');
            END
            """
        )
    with pytest.raises(sqlite3.DatabaseError, match="injected_preflight"):
        store.record_calendar_preflight_failure(
            failed_at=failed_at,
            reason="calendar_backend_unavailable",
        )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_calendar_preflight_insert")

    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (),
    )

    def crash_before_sidecar_clear(*, observed_at):
        assert observed_at == committed_observed_at
        raise SystemExit("injected_after_complete_commit")

    monkeypatch.setattr(
        store,
        "_clear_preflight_fail_stop",
        crash_before_sidecar_clear,
    )
    with pytest.raises(SystemExit, match="injected_after_complete_commit"):
        store.complete_cycle(
            closed_at,
            calendar_observed_at=committed_observed_at,
            signal_observation_batch=prepared,
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT completed FROM trusted_paper_bar_cycle WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM trusted_paper_bar_attempt WHERE closed_at = ?",
            (closed_at.isoformat(),),
        ).fetchone() == ("complete",)
        watermark = connection.execute(
            """
            SELECT observed_at, cycle_payload_sha256, payload_sha256
            FROM trusted_paper_bar_calendar_preflight_watermark
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        assert watermark is not None
        assert watermark[0] == committed_observed_at.isoformat()
        assert watermark[1].startswith("sha256:")
        assert watermark[2].startswith("sha256:")

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(restarted)
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_calendar_preflight_insert
            BEFORE INSERT ON trusted_paper_bar_calendar_preflight
            BEGIN
                SELECT RAISE(FAIL, 'injected_preflight_write_failure');
            END
            """
        )
    with pytest.raises(sqlite3.DatabaseError, match="injected_preflight"):
        restarted.record_calendar_preflight_failure(
            failed_at=newer_failed_at,
            reason="calendar_backend_unavailable",
        )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_calendar_preflight_insert")

    restarted.complete_cycle(
        closed_at,
        calendar_observed_at=replay_observed_at,
        signal_observation_batch=prepared,
    )

    remaining_fail_stops = tuple(
        sorted(
            restarted._decode_preflight_fail_stop(sidecar)
            for sidecar in restarted._preflight_fail_stop_paths()
        )
    )
    assert remaining_fail_stops == (newer_failed_at,)
    health = restarted.health()
    assert health.degraded is True
    assert health.degraded_reason == "paper_calendar_preflight_persistence_failed"
    assert health.calendar_preflight_failure_at == newer_failed_at


def test_signal_observation_completed_replay_rejects_tampered_preflight_watermark(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-preflight-watermark-tamper.sqlite3"
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    committed_observed_at = closed_at + timedelta(seconds=30)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (),
    )
    store.complete_cycle(
        closed_at,
        calendar_observed_at=committed_observed_at,
        signal_observation_batch=prepared,
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE trusted_paper_bar_calendar_preflight_watermark
            SET observed_at = ? WHERE closed_at = ?
            """,
            (
                (committed_observed_at + timedelta(seconds=30)).isoformat(),
                closed_at.isoformat(),
            ),
        )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_calendar_preflight_watermark_invalid",
    ):
        store.complete_cycle(
            closed_at,
            calendar_observed_at=committed_observed_at,
            signal_observation_batch=prepared,
        )


def test_signal_observation_external_anchor_detects_consistent_sqlite_rollback(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-rollback.sqlite3"
    first_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    second_at = first_at + timedelta(minutes=5)
    signal = paper_runtime_module.sha256_json({"signal": "rolled-back"})
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    first = _record_signal_observation_cycle(store, calendar, first_at, ())
    store.complete_cycle(first_at, signal_observation_batch=first)
    second = _record_signal_observation_cycle(
        store,
        calendar,
        second_at,
        (signal,),
    )
    store.complete_cycle(second_at, signal_observation_batch=second)

    with sqlite3.connect(path) as connection:
        first_head = connection.execute(
            """
            SELECT payload_sha256 FROM trusted_signal_observation_cycle
            WHERE observation_sequence = 1
            """
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM trusted_signal_first_observation "
            "WHERE observation_sequence = 2"
        )
        connection.execute(
            "DELETE FROM trusted_signal_segment_observation "
            "WHERE observation_sequence = 2"
        )
        connection.execute(
            "DELETE FROM trusted_signal_observation_cycle "
            "WHERE observation_sequence = 2"
        )
        rolled_back_state = {
            "schema_version": 1,
            "event_count": 1,
            "max_sequence": 1,
            "history_head_sha256": first_head,
        }
        connection.execute(
            """
            UPDATE trusted_signal_observation_log_state
            SET event_count = 1, max_sequence = 1,
                history_head_sha256 = ?, payload_sha256 = ?
            WHERE singleton_id = 1
            """,
            (
                first_head,
                paper_runtime_module.sha256_json(rolled_back_state),
            ),
        )
        connection.execute(
            "UPDATE sqlite_sequence SET seq = 1 "
            "WHERE name = 'trusted_signal_observation_cycle'"
        )

    health = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    ).health()
    assert health.degraded is True
    assert health.degraded_reason == "signal_observation_anchor_mismatch"


def test_signal_observation_attempt_generation_rewrite_hits_external_anchor(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-generation-rewrite.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(
        store,
        calendar,
        closed_at,
        (),
    )
    store.complete_cycle(closed_at, signal_observation_batch=prepared)

    with sqlite3.connect(path) as connection:
        payload = json.loads(
            connection.execute(
                """
                SELECT payload_json FROM trusted_signal_observation_cycle
                WHERE closed_at = ?
                """,
                (closed_at.isoformat(),),
            ).fetchone()[0]
        )
        forged_resolution = store._prepared_signal_observation_payload(
            binding=(
                prepared.run_id,
                prepared.epoch,
                prepared.strategy_run_fingerprint,
                prepared.identity_sha256,
                prepared.store_instance_id,
            ),
            closed_at=closed_at,
            manifests=prepared.manifests,
            segment_ids=prepared.segment_ids,
            states=prepared.states,
            first_observed_at=prepared.first_observed_at,
            attempt_generation=2,
            prior_attempt_ambiguous=True,
        )
        payload["attempt_generation"] = 2
        payload["prior_attempt_ambiguous"] = True
        payload["resolution_sha256"] = paper_runtime_module.sha256_json(
            forged_resolution
        )
        forged_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        forged_checksum = paper_runtime_module.sha256_json(payload)
        connection.execute(
            """
            UPDATE trusted_paper_bar_attempt SET attempt_generation = 2
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        )
        connection.execute(
            """
            UPDATE trusted_signal_observation_cycle
            SET payload_json = ?, payload_sha256 = ?
            WHERE closed_at = ?
            """,
            (forged_json, forged_checksum, closed_at.isoformat()),
        )
        state_payload = store._signal_observation_log_state_payload(
            event_count=1,
            max_sequence=1,
            history_head_sha256=forged_checksum,
        )
        connection.execute(
            """
            UPDATE trusted_signal_observation_log_state
            SET history_head_sha256 = ?, payload_sha256 = ?
            WHERE singleton_id = 1
            """,
            (
                forged_checksum,
                paper_runtime_module.sha256_json(state_payload),
            ),
        )

    health = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    ).health()
    assert health.degraded is True
    assert health.degraded_reason == "signal_observation_anchor_mismatch"


def test_signal_observation_payload_update_is_detected_and_fails_closed(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "signal-observation-update.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    prepared = _record_signal_observation_cycle(store, calendar, closed_at, ())
    store.complete_cycle(closed_at, signal_observation_batch=prepared)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE trusted_signal_observation_cycle
            SET payload_json = '{}'
            WHERE observation_sequence = 1
            """
        )

    health = store.health()
    assert health.degraded is True
    assert health.degraded_reason == (
        "signal_observation_manifest_integrity_failure"
    )


def test_trusted_bar_store_is_immutable_restart_safe_and_detects_tampering(
    tmp_path,
) -> None:
    path = tmp_path / "trusted-bars.sqlite3"
    calendar = _Calendar((date(2026, 7, 14),))
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    bar = _bar(datetime(2026, 7, 14, 10, 35, tzinfo=CN))
    cycle = {
        "session": calendar.session_for(bar.closed_at.date()),
        "bar_closed_at": bar.closed_at,
        "required_codes": (bar.code,),
        "optional_codes": (),
        "bars": {bar.code: bar},
        "optional_failures": {},
    }

    assert store.record_cycle(**cycle) == (bar,)
    assert store.record_cycle(**cycle) == (bar,)
    store.complete_cycle(bar.closed_at)
    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    assert restarted.get_bar(bar.bar_id) == bar
    assert restarted.get_bar(bar.bar_id).close_price == Decimal("10.40")
    assert restarted.get_for_code_at(bar.code, bar.closed_at) == bar
    assert restarted.health().bar_count == 1
    assert restarted.health().observed_trading_days == 0

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE trusted_paper_bar SET payload_json = '{}' WHERE bar_id = ?",
            (bar.bar_id,),
        )

    health = restarted.health()
    assert health.degraded is True
    assert health.degraded_reason == "paper_bar_integrity_failure"
    with pytest.raises(TrustedPaperBarIntegrityError, match="checksum"):
        restarted.get_bar(bar.bar_id)


def test_trusted_bar_store_rejects_untracked_gap_without_poisoning_integrity(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "gap.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    morning = _bar(datetime(2026, 7, 14, 11, 30, tzinfo=CN))
    afternoon = _bar(datetime(2026, 7, 14, 13, 5, tzinfo=CN))
    for bar in (morning, afternoon):
        store.record_cycle(
            session=calendar.session_for(bar.closed_at.date()),
            bar_closed_at=bar.closed_at,
            required_codes=(bar.code,),
            optional_codes=(),
            bars={bar.code: bar},
            optional_failures={},
        )
        store.complete_cycle(bar.closed_at)

    assert store.health().degraded is False

    with pytest.raises(TrustedPaperBarIntegrityError, match="paper_bar_gap_detected"):
        store.put(_bar(datetime(2026, 7, 14, 13, 15, tzinfo=CN)))

    health = SQLiteTrustedPaperBarStore(
        store.path,
        calendar_fingerprint=calendar.fingerprint,
    ).health()
    assert health.degraded is False
    assert health.degraded_reason is None
    assert health.bar_count == 2


def test_trusted_bar_store_rejects_conflicting_payload_for_same_code_and_close(
    tmp_path,
) -> None:
    store = SQLiteTrustedPaperBarStore(tmp_path / "conflict.sqlite3")
    bar = _bar(datetime(2026, 7, 14, 10, 35, tzinfo=CN))
    store.put(bar)

    with pytest.raises(TrustedPaperBarIntegrityError, match="payload_conflict"):
        store.put(replace(bar, open_price=Decimal("10.26")))

    assert store.health().degraded is True
    assert store.get_bar(bar.bar_id) == bar


def test_trusted_bar_identity_and_restart_payload_bind_close_price(tmp_path) -> None:
    path = tmp_path / "close-price.sqlite3"
    store = SQLiteTrustedPaperBarStore(path)
    bar = _bar(datetime(2026, 7, 14, 10, 35, tzinfo=CN))
    changed = replace(bar, close_price=Decimal("10.41"))

    assert changed.bar_id != bar.bar_id
    store.put(bar)
    restarted = SQLiteTrustedPaperBarStore(path)

    assert restarted.get_bar(bar.bar_id) == bar
    assert restarted.get_bar(bar.bar_id).close_price == Decimal("10.40")


def test_raw_bar_rows_never_count_as_observed_trading_day(tmp_path) -> None:
    store = SQLiteTrustedPaperBarStore(tmp_path / "complete-day.sqlite3")
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    closes: list[datetime] = []
    while closed_at.time() <= datetime(2026, 7, 14, 11, 30, tzinfo=CN).time():
        closes.append(closed_at)
        closed_at += timedelta(minutes=5)
    closed_at = datetime(2026, 7, 14, 13, 5, tzinfo=CN)
    while closed_at.time() <= datetime(2026, 7, 14, 15, 0, tzinfo=CN).time():
        closes.append(closed_at)
        closed_at += timedelta(minutes=5)

    for close in closes:
        store.put(_bar(close))
        assert store.health().observed_trading_days == 0

    assert store.health().bar_count == 48
    assert store.health().degraded is True


def test_bar_only_legacy_restart_is_degraded_and_cannot_cross_paper_gates(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    session = calendar.session_for(trading_day)
    code = "SH.600001"
    path = tmp_path / "bar-only-legacy.sqlite3"
    legacy = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    for closed_at in session.expected_bar_closes:
        legacy.put(_bar(closed_at, code=code))

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    health = restarted.health()
    assert health.bar_count == 48
    assert health.observed_trading_days == 0
    assert health.degraded is True
    assert health.degraded_reason == "paper_bar_unbound_from_v2_cycle"

    direct_gateway = object.__new__(TrustedPaperAdmission)
    direct_gateway._bar_source = restarted
    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_source_degraded",
    ):
        direct_gateway._validate_bar_source_health()

    signal_at = session.expected_bar_closes[0]
    event = SimpleNamespace(
        event_id="event-from-bar-only-legacy",
        code=code,
        bar_closed_at=signal_at,
    )
    admit_calls: list[str] = []
    runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=restarted,
        paper_gateway=SimpleNamespace(
            admit=lambda event_id, *_args, **_kwargs: admit_calls.append(
                event_id
            )
        ),
        event_store=SimpleNamespace(
            list_events=lambda: (event,),
            get_snapshot=lambda _event_id: SimpleNamespace(
                state=EventState.CONFIRMED
            ),
            list_risk_snapshots=lambda _event_id: (
                SimpleNamespace(snapshot_id="risk-bar-only-legacy"),
            ),
        ),
        trading_calendar=calendar,
    )
    assert runtime.admission_cycle(signal_at).failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert admit_calls == []


def test_runtime_injected_unattested_completed_cycles_do_not_count_observation_day(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    path = tmp_path / "runtime-injected-cycles.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    with sqlite3.connect(path) as connection:
        for slot_index, closed_at in enumerate(calendar._closes(trading_day)):
            connection.execute(
                """
                INSERT INTO trusted_paper_bar_cycle (
                    closed_at, trading_day, slot_index,
                    calendar_fingerprint, required_codes_json,
                    optional_codes_json, persisted_codes_json,
                    optional_failures_json, payload_sha256,
                    completed, completed_at
                ) VALUES (?, ?, ?, ?, '[]', '["SH.600001"]',
                          '["SH.600001"]', '{}', ?, 1, ?)
                """,
                (
                    closed_at.isoformat(),
                    trading_day.isoformat(),
                    slot_index,
                    calendar.fingerprint,
                    "sha256:" + "0" * 64,
                    closed_at.isoformat(),
                ),
            )

    health = store.health()
    assert health.observed_trading_days == 0
    assert health.degraded is True
    assert health.degraded_reason == "paper_bar_cycle_integrity_failure"


def test_cross_day_restart_requires_previous_session_close(tmp_path) -> None:
    calendar = _Calendar((date(2026, 7, 14), date(2026, 7, 15)))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "cross-day-gap.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    prior = _bar(datetime(2026, 7, 14, 14, 55, tzinfo=CN))
    store.record_cycle(
        session=calendar.session_for(prior.closed_at.date()),
        bar_closed_at=prior.closed_at,
        required_codes=(prior.code,),
        optional_codes=(),
        bars={prior.code: prior},
        optional_failures={},
    )
    store.complete_cycle(prior.closed_at)

    with pytest.raises(TrustedPaperBarIntegrityError, match="paper_bar_gap_detected"):
        store.put(_bar(datetime(2026, 7, 15, 9, 35, tzinfo=CN)))

    assert store.health().degraded is False


def test_cross_day_restart_accepts_consecutive_session_boundary(tmp_path) -> None:
    calendar = _Calendar((date(2026, 7, 14), date(2026, 7, 15)))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "cross-day-valid.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    code = "SH.600001"
    prior_close = datetime(2026, 7, 14, 15, 0, tzinfo=CN)
    current_open = datetime(2026, 7, 15, 9, 35, tzinfo=CN)
    store.record_cycle(
        session=calendar.session_for(prior_close.date()),
        bar_closed_at=prior_close,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(prior_close, code=code)},
        optional_failures={},
    )
    opening = _bar(current_open, code=code)

    assert store.record_cycle(
        session=calendar.session_for(current_open.date()),
        bar_closed_at=current_open,
        required_codes=(code,),
        optional_codes=(),
        bars={code: opening},
        optional_failures={},
    ) == (opening,)
    assert store.health().degraded is False


def test_record_cycle_retry_binds_exact_bar_payload_and_degrades_on_conflict(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "cycle-retry-payload.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    code = "SH.600001"
    original = _bar(closed_at, code=code)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    arguments = {
        "session": calendar.session_for(closed_at.date()),
        "bar_closed_at": closed_at,
        "required_codes": (code,),
        "optional_codes": (),
        "optional_failures": {},
    }

    assert store.record_cycle(bars={code: original}, **arguments) == (original,)
    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    assert restarted.record_cycle(bars={code: original}, **arguments) == (original,)

    changed = replace(original, close_price=Decimal("10.41"))
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_cycle_payload_conflict",
    ):
        restarted.record_cycle(bars={code: changed}, **arguments)

    assert restarted.health().degraded is True
    assert restarted.get_for_code_at(code, closed_at) == original


def test_segment_ledger_rejects_reassigned_member(tmp_path) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "segment-ledger-reassigned-member.sqlite3"
    first_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    second_at = first_at + timedelta(minutes=5)
    attempted_at = second_at + timedelta(minutes=5)
    code = "SH.600001"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(first_at.date()),
        bar_closed_at=first_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(first_at, code=code)},
        optional_failures={},
    )
    store.record_cycle(
        session=calendar.session_for(second_at.date()),
        bar_closed_at=second_at,
        required_codes=(),
        optional_codes=(code,),
        bars={code: _bar(second_at, code=code)},
        optional_failures={},
    )
    with sqlite3.connect(path) as connection:
        first_segment_id = connection.execute(
            """
            SELECT segment_id FROM trusted_paper_bar_segment_member
            WHERE closed_at = ?
            """,
            (first_at.isoformat(),),
        ).fetchone()[0]
        second_segment_id = connection.execute(
            """
            SELECT segment_id FROM trusted_paper_bar_segment_member
            WHERE closed_at = ?
            """,
            (second_at.isoformat(),),
        ).fetchone()[0]
        assert first_segment_id != second_segment_id
        connection.execute(
            """
            UPDATE trusted_paper_bar_segment_member
            SET segment_id = ? WHERE closed_at = ?
            """,
            (second_segment_id, first_at.isoformat()),
        )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_segment_integrity_failure",
    ):
        store.start_cycle_attempt(
            session=calendar.session_for(attempted_at.date()),
            bar_closed_at=attempted_at,
        )


@pytest.mark.parametrize(
    "tamper_mode",
    (
        "segment_id_preimage",
        "started_at_first_member",
        "ended_at_last_member",
        "active_end_reason",
    ),
)
def test_segment_ledger_metadata_tamper_fails_closed(
    tmp_path,
    tamper_mode,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / f"segment-ledger-{tamper_mode}.sqlite3"
    first_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    second_at = first_at + timedelta(minutes=5)
    code = "SH.600001"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(first_at.date()),
        bar_closed_at=first_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(first_at, code=code)},
        optional_failures={},
    )
    store.record_cycle(
        session=calendar.session_for(second_at.date()),
        bar_closed_at=second_at,
        required_codes=(),
        optional_codes=(code,),
        bars={code: _bar(second_at, code=code)},
        optional_failures={},
    )
    with sqlite3.connect(path) as connection:
        closed_segment_id = connection.execute(
            """
            SELECT segment_id FROM trusted_paper_bar_segment_member
            WHERE closed_at = ?
            """,
            (first_at.isoformat(),),
        ).fetchone()[0]
        active_segment_id = connection.execute(
            """
            SELECT segment_id FROM trusted_paper_bar_segment_member
            WHERE closed_at = ?
            """,
            (second_at.isoformat(),),
        ).fetchone()[0]
        if tamper_mode == "segment_id_preimage":
            forged_segment_id = "sha256:" + "f" * 64
            connection.execute(
                """
                UPDATE trusted_paper_bar_segment_member SET segment_id = ?
                WHERE segment_id = ?
                """,
                (forged_segment_id, active_segment_id),
            )
            connection.execute(
                """
                UPDATE trusted_paper_bar_segment SET segment_id = ?
                WHERE segment_id = ?
                """,
                (forged_segment_id, active_segment_id),
            )
        elif tamper_mode == "started_at_first_member":
            forged_started_at = second_at + timedelta(minutes=1)
            forged_segment_id = paper_runtime_module.sha256_json(
                {
                    "schema_version": 1,
                    "code": code,
                    "required": False,
                    "started_at": forged_started_at,
                    "calendar_fingerprint": calendar.fingerprint,
                }
            )
            connection.execute(
                """
                UPDATE trusted_paper_bar_segment_member SET segment_id = ?
                WHERE segment_id = ?
                """,
                (forged_segment_id, active_segment_id),
            )
            connection.execute(
                """
                UPDATE trusted_paper_bar_segment
                SET segment_id = ?, started_at = ?
                WHERE segment_id = ?
                """,
                (
                    forged_segment_id,
                    forged_started_at.isoformat(),
                    active_segment_id,
                ),
            )
        elif tamper_mode == "ended_at_last_member":
            connection.execute(
                """
                UPDATE trusted_paper_bar_segment SET ended_at = ?
                WHERE segment_id = ?
                """,
                (second_at.isoformat(), closed_segment_id),
            )
        else:
            connection.execute(
                """
                UPDATE trusted_paper_bar_segment
                SET end_reason = 'membership_removed'
                WHERE segment_id = ?
                """,
                (active_segment_id,),
            )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_segment_integrity_failure",
    ):
        SQLiteTrustedPaperBarStore(
            path,
            calendar_fingerprint=calendar.fingerprint,
        )


def test_segment_ledger_tamper_is_rejected_at_strategy_bind_boundary(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "segment-ledger-bind-boundary.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    code = "SH.600001"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(closed_at, code=code)},
        optional_failures={},
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE trusted_paper_bar_segment
            SET end_reason = 'membership_removed'
            WHERE ended_at IS NULL
            """
        )

    binding = SimpleNamespace(
        run_id="run:segment-ledger-bind",
        epoch=1,
        strategy_run_fingerprint="sha256:" + "1" * 64,
        identity_sha256="sha256:" + "2" * 64,
        store_role="bar",
        store_instance_id="store:segment-ledger-bind",
    )
    active = SimpleNamespace(
        run_id=binding.run_id,
        epoch=binding.epoch,
        strategy_run_fingerprint=binding.strategy_run_fingerprint,
        store_bindings={"bar": binding},
        store_paths={"bar": store.path},
        mutation_lease=lambda _operation: nullcontext(),
        require_current_mutation_lease=lambda: None,
    )
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_segment_integrity_failure",
    ):
        store.bind_strategy_run(active)


def test_incomplete_v3_cycle_cannot_downgrade_to_v2_and_merge_required_segments(
    tmp_path,
) -> None:
    first_day = date(2026, 7, 14)
    removed_day = date(2026, 7, 15)
    reentered_day = date(2026, 7, 16)
    calendar = _Calendar((first_day, removed_day, reentered_day))
    path = tmp_path / "incomplete-v3-downgrade-segment-merge.sqlite3"
    code = "SH.600001"
    first_at = datetime.combine(first_day, time(15, 0), CN)
    removed_at = datetime.combine(removed_day, time(9, 35), CN)
    reentered_at = datetime.combine(reentered_day, time(9, 35), CN)
    reentered_bar = _bar(reentered_at, code=code)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(first_day),
        bar_closed_at=first_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(first_at, code=code)},
        optional_failures={},
    )
    store.complete_cycle(first_at)
    store.record_cycle(
        session=calendar.session_for(removed_day),
        bar_closed_at=removed_at,
        required_codes=(),
        optional_codes=(),
        bars={},
        optional_failures={},
    )
    store.complete_cycle(removed_at)
    store.record_cycle(
        session=calendar.session_for(reentered_day),
        bar_closed_at=reentered_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: reentered_bar},
        optional_failures={},
    )
    with sqlite3.connect(path) as connection:
        old_segment_id = connection.execute(
            """
            SELECT segment_id FROM trusted_paper_bar_segment_member
            WHERE closed_at = ? AND bar_id != ?
            """,
            (first_at.isoformat(), reentered_bar.bar_id),
        ).fetchone()[0]
        new_segment_id = connection.execute(
            """
            SELECT segment_id FROM trusted_paper_bar_segment_member
            WHERE closed_at = ? AND bar_id = ?
            """,
            (reentered_at.isoformat(), reentered_bar.bar_id),
        ).fetchone()[0]
        v2_payload = store._cycle_payload(
            closed_at=reentered_at.isoformat(),
            trading_day=reentered_day.isoformat(),
            slot_index=0,
            calendar_fingerprint=calendar.fingerprint,
            required_codes=(code,),
            optional_codes=(),
            persisted_codes=(code,),
            optional_failures={},
            bar_bindings={
                code: {
                    "bar_id": reentered_bar.bar_id,
                    "payload_sha256": paper_runtime_module._payload_sha256(
                        paper_runtime_module._bar_json(reentered_bar)
                    ),
                }
            },
        )
        v2_checksum = paper_runtime_module._payload_sha256(
            json.dumps(
                v2_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        connection.execute(
            """
            UPDATE trusted_paper_bar_cycle SET payload_sha256 = ?
            WHERE closed_at = ?
            """,
            (v2_checksum, reentered_at.isoformat()),
        )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_cycle_v2_unattested",
    ):
        store.complete_cycle(reentered_at)
    store.bind_signal_observation_strategy_run(
        _signal_observation_strategy_run(store)
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE trusted_paper_bar_segment_member SET segment_id = ?
            WHERE closed_at = ? AND bar_id = ?
            """,
            (old_segment_id, reentered_at.isoformat(), reentered_bar.bar_id),
        )
        connection.execute(
            "DELETE FROM trusted_paper_bar_segment WHERE segment_id = ?",
            (new_segment_id,),
        )
        connection.execute(
            """
            UPDATE trusted_paper_bar_segment
            SET ended_at = NULL, end_reason = NULL
            WHERE segment_id = ?
            """,
            (old_segment_id,),
        )

    with pytest.raises(TrustedPaperBarIntegrityError):
        store.attest_cycle_bar(
            reentered_bar.bar_id,
            allow_current_started=True,
        )
    with pytest.raises(TrustedPaperBarIntegrityError):
        store.prepare_signal_observation_batch(reentered_at, {code: ()})
    with pytest.raises(TrustedPaperBarIntegrityError):
        SQLiteTrustedPaperBarStore(
            path,
            calendar_fingerprint=calendar.fingerprint,
        )


def test_cycle_v2_exact_replay_remains_compatible_after_segment_binding_upgrade(
    tmp_path,
    monkeypatch,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "cycle-v2-exact-replay.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    code = "SH.600001"
    bar = _bar(closed_at, code=code)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    arguments = {
        "session": calendar.session_for(closed_at.date()),
        "bar_closed_at": closed_at,
        "required_codes": (code,),
        "optional_codes": (),
        "optional_failures": {},
    }
    store.record_cycle(bars={code: bar}, **arguments)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            """
            SELECT closed_at, trading_day, slot_index,
                   calendar_fingerprint, required_codes_json,
                   optional_codes_json, persisted_codes_json,
                   optional_failures_json, payload_sha256
            FROM trusted_paper_bar_cycle WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        payload, _bars = store._decode_cycle_row(connection, row)
        assert payload["schema_version"] == 3
        segment_id = payload["bar_bindings"][code]["segment_id"]
        assert segment_id == connection.execute(
            """
            SELECT segment_id FROM trusted_paper_bar_segment_member
            WHERE bar_id = ?
            """,
            (bar.bar_id,),
        ).fetchone()[0]
        v2_payload = store._cycle_payload(
            closed_at=closed_at.isoformat(),
            trading_day=closed_at.date().isoformat(),
            slot_index=0,
            calendar_fingerprint=calendar.fingerprint,
            required_codes=(code,),
            optional_codes=(),
            persisted_codes=(code,),
            optional_failures={},
            bar_bindings={
                code: {
                    "bar_id": bar.bar_id,
                    "payload_sha256": paper_runtime_module._payload_sha256(
                        paper_runtime_module._bar_json(bar)
                    ),
                }
            },
        )
        assert v2_payload["schema_version"] == 2
        v2_json = json.dumps(
            v2_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        v2_checksum = paper_runtime_module._payload_sha256(v2_json)

    store.complete_cycle(closed_at)
    with sqlite3.connect(path) as connection:
        manifest_row = connection.execute(
            """
            SELECT manifest_sequence, payload_json, payload_sha256
            FROM trusted_paper_exit_manifest WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        manifest_sequence, manifest_json, original_manifest_checksum = (
            manifest_row
        )
        manifest_payload = json.loads(manifest_json)
        manifest_payload["bar_cycle_payload_sha256"] = v2_checksum
        v2_manifest_json = json.dumps(
            manifest_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        v2_manifest_checksum = paper_runtime_module._payload_sha256(
            v2_manifest_json
        )
        connection.execute(
            """
            UPDATE trusted_paper_bar_cycle SET payload_sha256 = ?
            WHERE closed_at = ?
            """,
            (v2_checksum, closed_at.isoformat()),
        )
        connection.execute(
            """
            UPDATE trusted_paper_exit_manifest
            SET payload_json = ?, payload_sha256 = ?
            WHERE closed_at = ?
            """,
            (
                v2_manifest_json,
                v2_manifest_checksum,
                closed_at.isoformat(),
            ),
        )
        state_payload = store._exit_manifest_log_state_payload(
            event_count=1,
            max_sequence=manifest_sequence,
            history_head_sha256=v2_manifest_checksum,
        )
        connection.execute(
            """
            UPDATE trusted_paper_exit_manifest_log_state
            SET event_count = 1, max_sequence = ?,
                history_head_sha256 = ?, payload_sha256 = ?
            WHERE singleton_id = 1
            """,
            (
                manifest_sequence,
                v2_manifest_checksum,
                paper_runtime_module.sha256_json(state_payload),
            ),
        )

    store._exit_manifest_anchor_path(
        manifest_sequence,
        original_manifest_checksum,
    ).unlink()
    store._write_exit_manifest_anchor(
        manifest_sequence=manifest_sequence,
        closed_at=closed_at.isoformat(),
        history_head_sha256=v2_manifest_checksum,
    )

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    assert restarted.record_cycle(bars={code: bar}, **arguments) == (bar,)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT payload_sha256 FROM trusted_paper_bar_cycle
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone() == (v2_checksum,)
    restarted.complete_cycle(closed_at)
    with sqlite3.connect(path) as connection:
        exit_manifest = json.loads(
            connection.execute(
                """
                SELECT payload_json FROM trusted_paper_exit_manifest
                WHERE closed_at = ?
                """,
                (closed_at.isoformat(),),
            ).fetchone()[0]
        )
    assert exit_manifest["bar_cycle_payload_sha256"] == v2_checksum
    twice_restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    assert twice_restarted.is_cycle_complete(closed_at) is True
    with sqlite3.connect(path) as connection:
        attested_checksums = twice_restarted._validate_exit_manifest_log(
            connection
        )
        v2_cycle_row = connection.execute(
            """
            SELECT closed_at, trading_day, slot_index,
                   calendar_fingerprint, required_codes_json,
                   optional_codes_json, persisted_codes_json,
                   optional_failures_json, payload_sha256
            FROM trusted_paper_bar_cycle WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone()
        validation_calls = 0

        def count_unexpected_validation(_connection):
            nonlocal validation_calls
            validation_calls += 1
            return attested_checksums

        monkeypatch.setattr(
            twice_restarted,
            "_validate_exit_manifest_log",
            count_unexpected_validation,
        )
        for _ in range(2):
            twice_restarted._decode_cycle_row(
                connection,
                v2_cycle_row,
                attested_v2_cycle_checksums=attested_checksums,
            )
        assert validation_calls == 0
    twice_restarted._exit_manifest_anchor_path(
        manifest_sequence,
        v2_manifest_checksum,
    ).unlink()
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_exit_manifest_anchor_mismatch",
    ):
        SQLiteTrustedPaperBarStore(
            path,
            calendar_fingerprint=calendar.fingerprint,
        )


def test_bound_health_read_cannot_persist_degradation_without_current_lease(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "bound-health-read.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    code = "SH.600001"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(closed_at, code=code)},
        optional_failures={},
    )

    class ActiveRun:
        run_id = "paper-run-bound-health"
        epoch = 1
        strategy_run_fingerprint = "sha256:" + "7" * 64

        def __init__(self) -> None:
            self._held = False
            self.store_paths = {"bar": store.path}
            self.store_bindings = {
                "bar": SimpleNamespace(
                    store_role="bar",
                    store_instance_id="bar-store-bound-health",
                    run_id=self.run_id,
                    epoch=self.epoch,
                    strategy_run_fingerprint=self.strategy_run_fingerprint,
                    identity_sha256="sha256:" + "8" * 64,
                )
            }

        def mutation_lease(self, _operation: str):
            active = self

            class Lease:
                def __enter__(self):
                    active._held = True
                    return object()

                def __exit__(self, *_args):
                    active._held = False

            return Lease()

        def require_current_mutation_lease(self) -> None:
            if not self._held:
                raise RuntimeError("strategy_run_mutation_lease_required")

    active = ActiveRun()
    store.bind_strategy_run(active)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE trusted_paper_bar_cycle SET payload_sha256 = ?
            WHERE closed_at = ?
            """,
            ("sha256:" + "f" * 64, closed_at.isoformat()),
        )

    assert store.health().degraded is True
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT degraded FROM trusted_paper_bar_health
            WHERE singleton_id = 1
            """
        ).fetchone() == (0,)

    with active.mutation_lease("test.persist_fail_stop"):
        assert store.health().degraded is True
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            """
            SELECT degraded, degraded_reason
            FROM trusted_paper_bar_health WHERE singleton_id = 1
            """
        ).fetchone() == (1, "paper_bar_cycle_integrity_failure")


def test_cycle_completion_attestation_fails_closed_on_attempt_tamper(
    tmp_path,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "cycle-completion-attestation.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    code = "SH.600001"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(closed_at, code=code)},
        optional_failures={},
    )

    assert store.is_cycle_complete(closed_at) is False
    store.complete_cycle(closed_at)
    assert store.is_cycle_complete(closed_at) is True

    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE trusted_paper_bar_attempt
            SET status = 'failed', failure_reason = 'tampered'
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_attempt_replay_invalid",
    ):
        store.complete_cycle(closed_at)
    assert store.is_cycle_complete(closed_at) is False
    health = store.health()
    assert health.degraded is True
    assert health.degraded_reason == "paper_bar_cycle_attestation_invalid"


@pytest.mark.parametrize("entrypoint", ("record", "startup"))
def test_record_cycle_rejects_unattested_legacy_membership_only_checksum(
    tmp_path,
    entrypoint,
) -> None:
    calendar = _Calendar((date(2026, 7, 14),))
    path = tmp_path / "cycle-legacy-checksum.sqlite3"
    closed_at = datetime(2026, 7, 14, 9, 35, tzinfo=CN)
    code = "SH.600001"
    bar = _bar(closed_at, code=code)
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    arguments = {
        "session": calendar.session_for(closed_at.date()),
        "bar_closed_at": closed_at,
        "required_codes": (code,),
        "optional_codes": (),
        "optional_failures": {},
    }
    store.record_cycle(bars={code: bar}, **arguments)
    legacy_payload = store._cycle_payload(
        closed_at=closed_at.isoformat(),
        trading_day=closed_at.date().isoformat(),
        slot_index=0,
        calendar_fingerprint=calendar.fingerprint,
        required_codes=(code,),
        optional_codes=(),
        persisted_codes=(code,),
        optional_failures={},
    )
    legacy_encoded = json.dumps(
        legacy_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    legacy_checksum = paper_runtime_module._payload_sha256(legacy_encoded)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            UPDATE trusted_paper_bar_cycle SET payload_sha256 = ?
            WHERE closed_at = ?
            """,
            (legacy_checksum, closed_at.isoformat()),
        )

    if entrypoint == "record":
        with pytest.raises(
            TrustedPaperBarIntegrityError,
            match="paper_bar_cycle_legacy_checksum_unattested",
        ):
            store.record_cycle(bars={code: bar}, **arguments)
    else:
        with pytest.raises(
            TrustedPaperBarIntegrityError,
            match="paper_bar_cycle_legacy_checksum_unattested",
        ):
            SQLiteTrustedPaperBarStore(
                path,
                calendar_fingerprint=calendar.fingerprint,
            )
    with sqlite3.connect(path) as connection:
        degraded, reason = connection.execute(
            """
            SELECT degraded, degraded_reason
            FROM trusted_paper_bar_health WHERE singleton_id = 1
            """
        ).fetchone()
    assert degraded == 1
    assert reason == "paper_bar_cycle_legacy_checksum_unattested"
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_bar_cycle_legacy_checksum_unattested",
    ):
        SQLiteTrustedPaperBarStore(
            path,
            calendar_fingerprint=calendar.fingerprint,
        )


def test_calendar_fingerprint_is_bound_and_restart_change_is_rejected(
    tmp_path,
) -> None:
    path = tmp_path / "calendar-bound.sqlite3"
    SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=_CALENDAR_FINGERPRINT,
    )

    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_calendar_fingerprint_mismatch",
    ):
        SQLiteTrustedPaperBarStore(
            path,
            calendar_fingerprint="sha256:" + "f" * 64,
        )


def test_explicit_calendar_has_stable_audited_identity_and_no_holiday_guessing() -> None:
    calendar_type = getattr(
        paper_runtime_module,
        "ExplicitPaperTradingCalendar",
        None,
    )
    assert calendar_type is not None
    trading_days = (date(2026, 7, 1), date(2026, 7, 3))
    calendar = calendar_type(
        trading_days,
        source_id="fixture-sse-calendar",
        source_fingerprint="sha256:" + "1" * 64,
    )
    same = calendar_type(
        trading_days,
        source_id="fixture-sse-calendar",
        source_fingerprint="sha256:" + "1" * 64,
    )

    assert calendar.fingerprint == same.fingerprint
    assert calendar.session_for(date(2026, 7, 2)) is None
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_calendar_date_out_of_coverage",
    ):
        calendar.session_for(date(2026, 7, 4))
    resumed = calendar.session_for(date(2026, 7, 3))
    assert resumed.previous_trading_day == date(2026, 7, 1)
    assert len(resumed.expected_bar_closes) == 48
    assert resumed.expected_bar_closes[0].time() == time(9, 35)
    assert resumed.expected_bar_closes[-1].time() == time(15, 0)


def test_explicit_calendar_loads_strict_audited_json_and_rejects_tampering(
    tmp_path,
) -> None:
    calendar_type = getattr(
        paper_runtime_module,
        "ExplicitPaperTradingCalendar",
        None,
    )
    assert calendar_type is not None
    calendar = calendar_type(
        (date(2026, 7, 1), date(2026, 7, 3)),
        source_id="fixture-sse-calendar",
        source_fingerprint="sha256:" + "2" * 64,
    )
    path = tmp_path / "paper-calendar.json"
    path.write_text(
        json.dumps(calendar.to_payload(), ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )

    loaded = calendar_type.from_json_file(path)

    assert loaded.fingerprint == calendar.fingerprint
    payload = calendar.to_payload()
    payload["trading_days"] = ["2026-07-01", "2026-07-02", "2026-07-03"]
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="paper_calendar_declared_fingerprint_mismatch",
    ):
        calendar_type.from_json_file(path)


def test_explicit_calendar_accepts_weekday_holiday_and_rejects_hidden_trade_day_gap(
    tmp_path,
) -> None:
    first_day = date(2026, 7, 1)
    holiday = date(2026, 7, 2)
    resumed_day = date(2026, 7, 3)
    code = "SH.600001"
    holiday_calendar = _Calendar((first_day, resumed_day))
    store = SQLiteTrustedPaperBarStore(
        tmp_path / "audited-holiday.sqlite3",
        calendar_fingerprint=holiday_calendar.fingerprint,
    )
    first_session = holiday_calendar.session_for(first_day)
    resumed_session = holiday_calendar.session_for(resumed_day)
    first_close = first_session.expected_bar_closes[-1]
    resumed_open = resumed_session.expected_bar_closes[0]

    store.record_cycle(
        session=first_session,
        bar_closed_at=first_close,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(first_close, code=code)},
        optional_failures={},
    )
    store.complete_cycle(first_close)
    store.record_cycle(
        session=resumed_session,
        bar_closed_at=resumed_open,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(resumed_open, code=code)},
        optional_failures={},
    )
    assert store.health().degraded is False

    complete_calendar = _Calendar((first_day, holiday, resumed_day))
    strict = SQLiteTrustedPaperBarStore(
        tmp_path / "hidden-gap.sqlite3",
        calendar_fingerprint=complete_calendar.fingerprint,
    )
    strict.record_cycle(
        session=complete_calendar.session_for(first_day),
        bar_closed_at=first_close,
        required_codes=(code,),
        optional_codes=(),
        bars={code: _bar(first_close, code=code)},
        optional_failures={},
    )
    with pytest.raises(
        TrustedPaperBarIntegrityError,
        match="required_paper_bar_gap",
    ):
        strict.record_cycle(
            session=complete_calendar.session_for(resumed_day),
            bar_closed_at=resumed_open,
            required_codes=(code,),
            optional_codes=(),
            bars={code: _bar(resumed_open, code=code)},
            optional_failures={},
        )


def test_observed_day_requires_all_exact_calendar_slots(tmp_path) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    session = calendar.session_for(trading_day)
    code = "SH.600001"
    incomplete = SQLiteTrustedPaperBarStore(
        tmp_path / "incomplete-slots.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    for closed_at in (
        session.expected_bar_closes[0],
        session.expected_bar_closes[-1],
    ):
        incomplete.record_cycle(
            session=session,
            bar_closed_at=closed_at,
            required_codes=(),
            optional_codes=(code,),
            bars={code: _bar(closed_at, code=code)},
            optional_failures={},
        )
        incomplete.complete_cycle(closed_at)
    incomplete_health = incomplete.health()
    assert incomplete_health.observed_trading_days == 0
    assert incomplete_health.verified_trading_dates == ()

    complete = SQLiteTrustedPaperBarStore(
        tmp_path / "complete-slots.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    for closed_at in session.expected_bar_closes:
        complete.record_cycle(
            session=session,
            bar_closed_at=closed_at,
            required_codes=(),
            optional_codes=(code,),
            bars={code: _bar(closed_at, code=code)},
            optional_failures={},
        )
        complete.complete_cycle(closed_at)
    complete_health = complete.health()
    assert complete_health.observed_trading_days == 1
    assert complete_health.verified_trading_dates == (trading_day,)


def test_paper_runtime_ignores_non_session_boundaries_without_failure(
    tmp_path,
) -> None:
    calls: list[str] = []

    class Provider:
        def universe_provider(self, _requested):
            calls.append("freeze")
            raise AssertionError("non-session boundary must not freeze data")

    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "session-gate.sqlite3"),
        paper_gateway=SimpleNamespace(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )
    boundaries = (
        datetime(2026, 7, 14, 9, 30, tzinfo=CN),
        datetime(2026, 7, 14, 11, 35, tzinfo=CN),
        datetime(2026, 7, 14, 13, 0, tzinfo=CN),
        datetime(2026, 7, 14, 15, 5, tzinfo=CN),
        datetime(2026, 7, 18, 10, 35, tzinfo=CN),
    )

    results = tuple(runtime.bar_cycle(value) for value in boundaries)

    assert {result.code for result in results} == {"bar_not_closed"}
    assert runtime.health().bar_cycle_failures == 0
    assert runtime.health().bar_store.calendar_preflight_failure_at is None
    assert runtime.health().bar_store.calendar_preflight_failure is None
    assert calls == []


def test_optional_candidate_churn_and_failure_start_new_persistent_segments(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    closes = calendar.session_for(trading_day).expected_bar_closes[12:16]
    optional = "SZ.000001"
    anchor = "SH.600001"

    class Provider:
        def universe_provider(self, requested):
            codes = (anchor, optional) if requested != closes[1] else (anchor,)
            return SimpleNamespace(
                securities=tuple(SimpleNamespace(code=code) for code in codes)
            )

        def required_codes(self, _requested):
            return ()

        def failures(self, _requested):
            return {}

        def paper_bar(self, code, requested):
            return _bar(requested, code=code)

    store = SQLiteTrustedPaperBarStore(
        tmp_path / "optional-churn.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=SimpleNamespace(
            scan_cycle=lambda _at: SimpleNamespace(
                code="scan_complete",
                bar_closed_at=_at,
            )
        ),
        bar_store=store,
        paper_gateway=SimpleNamespace(process_bar=lambda _bar: ()),
        event_store=SimpleNamespace(),
        exit_cycle=lambda at: PaperExitCycleResult(at, 0, {}),
        trading_calendar=calendar,
    )

    results = tuple(runtime.bar_cycle(value) for value in closes[:3])

    assert tuple(result.code for result in results) == (
        "bar_cycle_complete",
        "bar_cycle_complete",
        "bar_cycle_complete",
    )
    assert store.health().degraded is False
    restarted_store = SQLiteTrustedPaperBarStore(
        store.path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted_runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=SimpleNamespace(
            scan_cycle=lambda at: SimpleNamespace(
                code="scan_complete",
                bar_closed_at=at,
            )
        ),
        bar_store=restarted_store,
        paper_gateway=SimpleNamespace(process_bar=lambda _bar: ()),
        event_store=SimpleNamespace(),
        exit_cycle=lambda at: PaperExitCycleResult(at, 0, {}),
        trading_calendar=calendar,
    )
    assert restarted_runtime.bar_cycle(closes[3]).code == "bar_cycle_complete"
    with sqlite3.connect(restarted_store.path) as connection:
        segments = connection.execute(
            """
            SELECT started_at, ended_at, end_reason
            FROM trusted_paper_bar_segment
            WHERE code = ? ORDER BY started_at
            """,
            (optional,),
        ).fetchall()
    assert len(segments) == 2
    assert segments[0][1] is not None
    assert segments[0][2] == "membership_removed"
    assert segments[1][1] is None


def test_optional_provider_failure_does_not_block_other_codes_or_poison_reentry(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    closes = calendar.session_for(trading_day).expected_bar_closes[12:15]
    optional = "SZ.000001"
    anchor = "SH.600001"

    class Provider:
        def universe_provider(self, requested):
            return SimpleNamespace(
                securities=(
                    SimpleNamespace(code=anchor),
                    SimpleNamespace(code=optional),
                )
            )

        def required_codes(self, _requested):
            return ()

        def failures(self, requested):
            return {optional: "RuntimeError"} if requested == closes[1] else {}

        def paper_bar(self, code, requested):
            if code == optional and requested == closes[1]:
                raise RuntimeError("single optional feed failure")
            return _bar(requested, code=code)

    store = SQLiteTrustedPaperBarStore(
        tmp_path / "optional-failure.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=SimpleNamespace(
            scan_cycle=lambda _at: SimpleNamespace(
                code="scan_complete",
                bar_closed_at=_at,
            )
        ),
        bar_store=store,
        paper_gateway=SimpleNamespace(process_bar=lambda _bar: ()),
        event_store=SimpleNamespace(),
        exit_cycle=lambda at: PaperExitCycleResult(at, 0, {}),
        trading_calendar=calendar,
    )

    results = tuple(runtime.bar_cycle(value) for value in closes)

    assert tuple(result.persisted_bar_count for result in results) == (2, 1, 2)
    assert {result.code for result in results} == {"bar_cycle_complete"}
    assert store.health().degraded is False


def test_required_pinned_failure_is_atomic_and_blocks_all_downstream_actions(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    closed_at = calendar.session_for(trading_day).expected_bar_closes[12]
    required = "SZ.000001"
    anchor = "SH.600001"
    calls: list[str] = []

    class Provider:
        def universe_provider(self, _requested):
            return SimpleNamespace(
                securities=(
                    SimpleNamespace(code=anchor),
                    SimpleNamespace(code=required),
                )
            )

        def required_codes(self, _requested):
            return (required,)

        def failures(self, _requested):
            return {required: "KeyError"}

        def paper_bar(self, code, requested):
            if code == required:
                raise KeyError("required bar missing")
            return _bar(requested, code=code)

    store = SQLiteTrustedPaperBarStore(
        tmp_path / "required-failure.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=SimpleNamespace(
            scan_cycle=lambda _at: calls.append("scan")
        ),
        bar_store=store,
        paper_gateway=SimpleNamespace(
            process_bar=lambda _bar: calls.append("fill")
        ),
        event_store=SimpleNamespace(),
        exit_cycle=lambda _at: calls.append("exit"),
        trading_calendar=calendar,
    )

    result = runtime.bar_cycle(closed_at)

    assert result.code == "bar_cycle_failed"
    assert result.detail == "TrustedPaperBarIntegrityError"
    assert store.health().bar_count == 0
    assert store.health().degraded is False
    assert calls == []


def test_early_required_failure_is_durable_and_cannot_be_cleared_by_admission(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    first, failed = calendar.session_for(trading_day).expected_bar_closes[12:14]
    required = "SZ.000001"
    anchor = "SH.600001"

    class Provider:
        def universe_provider(self, requested):
            return SimpleNamespace(
                securities=(
                    SimpleNamespace(code=anchor),
                    SimpleNamespace(code=required),
                )
            )

        def required_codes(self, _requested):
            return (required,)

        def failures(self, requested):
            return {required: "KeyError"} if requested == failed else {}

        def paper_bar(self, code, requested):
            return _bar(requested, code=code)

    class ExitCycle:
        coverage = None

        def __call__(self, requested):
            self.coverage = PaperExitCoverage(requested, (), (), {})
            return PaperExitCycleResult(requested, 0, {})

        def latest_coverage(self):
            return self.coverage

        def record_scan_outcome(self, requested, scan_code):
            assert self.coverage is not None
            assert self.coverage.bar_closed_at == requested
            self.coverage = replace(self.coverage, scan_code=scan_code)

    path = tmp_path / "durable-required-failure.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    exit_cycle = ExitCycle()
    event = SimpleNamespace(
        event_id="event-after-required-failure",
        code=anchor,
        bar_closed_at=first,
    )
    admit_calls: list[str] = []
    event_store = SimpleNamespace(
        list_events=lambda: (event,),
        get_snapshot=lambda _event_id: SimpleNamespace(
            state=EventState.CONFIRMED
        ),
        list_risk_snapshots=lambda _event_id: (
            SimpleNamespace(snapshot_id="risk-after-required-failure"),
        ),
    )
    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=SimpleNamespace(
            scan_cycle=lambda at: SimpleNamespace(
                code="scan_complete",
                bar_closed_at=at,
            )
        ),
        bar_store=store,
        paper_gateway=SimpleNamespace(
            process_bar=lambda _bar: (),
            admit=lambda event_id, *_args, **_kwargs: admit_calls.append(
                event_id
            ),
        ),
        event_store=event_store,
        exit_cycle=exit_cycle,
        trading_calendar=calendar,
    )

    assert runtime.bar_cycle(first).code == "bar_cycle_complete"
    assert runtime.health().exit_coverage.fresh is True
    assert runtime.bar_cycle(failed).code == "bar_cycle_failed"
    admission = runtime.admission_cycle(failed + timedelta(seconds=1))

    health = runtime.health()
    assert health.last_error == "TrustedPaperBarIntegrityError"
    assert admission.failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert admit_calls == []
    assert health.exit_coverage.fresh is False
    assert health.bar_store.last_attempted_bar_closed_at == failed
    assert health.bar_store.last_attempt_complete is False
    assert health.bar_store.last_attempt_failure == "TrustedPaperBarIntegrityError"
    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    ).health()
    assert restarted.last_attempted_bar_closed_at == failed
    assert restarted.last_attempt_complete is False
    assert restarted.last_attempt_failure == "TrustedPaperBarIntegrityError"


@pytest.mark.parametrize(
    ("fault", "expected_reason"),
    (
        (
            "out_of_coverage",
            "paper_calendar_date_out_of_coverage",
        ),
        ("exception", "calendar_backend_unavailable"),
        (
            "bad_fingerprint",
            "paper_calendar_fingerprint_mismatch",
        ),
        (
            "wrong_trading_day",
            "paper_calendar_trading_day_mismatch",
        ),
    ),
)
def test_calendar_preflight_failure_is_durable_and_blocks_until_full_recovery(
    tmp_path,
    fault,
    expected_reason,
) -> None:
    trading_day = date(2026, 7, 14)

    class FaultingCalendar(_Calendar):
        fault = None

        def session_for(self, requested_day):
            session = super().session_for(requested_day)
            if self.fault is None or session is None:
                return session
            if self.fault == "out_of_coverage":
                raise TrustedPaperBarIntegrityError(
                    "paper_calendar_date_out_of_coverage"
                )
            if self.fault == "exception":
                raise RuntimeError("calendar_backend_unavailable")
            if self.fault == "bad_fingerprint":
                return SimpleNamespace(
                    trading_day=session.trading_day,
                    previous_trading_day=session.previous_trading_day,
                    expected_bar_closes=session.expected_bar_closes,
                    calendar_fingerprint="sha256:" + "f" * 64,
                )
            wrong_day = requested_day + timedelta(days=1)
            return SimpleNamespace(
                trading_day=wrong_day,
                previous_trading_day=None,
                expected_bar_closes=self._closes(wrong_day),
                calendar_fingerprint=self.fingerprint,
            )

    calendar = FaultingCalendar((trading_day,))
    first, failed = calendar.session_for(
        trading_day
    ).expected_bar_closes[12:14]
    code = "SH.600001"
    class Provider:
        def universe_provider(self, _requested):
            return SimpleNamespace(
                securities=(SimpleNamespace(code=code),)
            )

        def required_codes(self, _requested):
            return ()

        def failures(self, _requested):
            return {}

        def paper_bar(self, requested_code, requested):
            return _bar(requested, code=requested_code)

    class ExitCycle:
        coverage = None

        def __call__(self, requested):
            self.coverage = PaperExitCoverage(requested, (), (), {})
            return PaperExitCycleResult(requested, 0, {})

        def latest_coverage(self):
            return self.coverage

        def record_scan_outcome(self, requested, scan_code):
            assert self.coverage is not None
            assert self.coverage.bar_closed_at == requested
            self.coverage = replace(self.coverage, scan_code=scan_code)

    path = tmp_path / f"calendar-preflight-{fault}.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    exit_cycle = ExitCycle()
    event = SimpleNamespace(
        event_id=f"event-after-calendar-{fault}",
        code=code,
        bar_closed_at=first,
    )
    event_store = SimpleNamespace(
        list_events=lambda: (event,),
        get_snapshot=lambda _event_id: SimpleNamespace(
            state=EventState.CONFIRMED
        ),
        list_risk_snapshots=lambda _event_id: (
            SimpleNamespace(snapshot_id=f"risk-after-calendar-{fault}"),
        ),
    )
    admit_calls: list[str] = []
    gateway = SimpleNamespace(
        process_bar=lambda _bar: (),
        admit=lambda event_id, *_args, **_kwargs: admit_calls.append(
            event_id
        ),
    )

    def build_runtime(bar_store):
        return PaperResearchRuntime(
            data_provider=Provider(),
            analysis_runtime=SimpleNamespace(
                scan_cycle=lambda at: SimpleNamespace(
                    code="scan_complete",
                    bar_closed_at=at.replace(second=0, microsecond=0),
                )
            ),
            bar_store=bar_store,
            paper_gateway=gateway,
            event_store=event_store,
            exit_cycle=exit_cycle,
            trading_calendar=calendar,
        )

    runtime = build_runtime(store)
    assert runtime.bar_cycle(first).code == "bar_cycle_complete"
    assert runtime.health().exit_coverage.fresh is True

    calendar.fault = fault
    failure_asof = failed + timedelta(seconds=45)
    early_recovery_asof = failed + timedelta(seconds=15)
    late_recovery_asof = failed + timedelta(seconds=50)
    failure = runtime.bar_cycle(failure_asof)
    assert failure.code == "bar_cycle_failed"
    assert failure.bar_closed_at is None
    failed_health = runtime.health()
    assert failed_health.bar_store.last_attempted_bar_closed_at == first
    assert failed_health.bar_store.last_attempt_complete is True
    assert (
        failed_health.bar_store.calendar_preflight_failure_at
        == failure_asof
    )
    assert (
        failed_health.bar_store.calendar_preflight_failure
        == expected_reason
    )
    assert failed_health.exit_coverage.fresh is False
    assert runtime.admission_cycle(failure_asof).failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert admit_calls == []

    restarted_store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted_runtime = build_runtime(restarted_store)
    restarted_health = restarted_runtime.health()
    assert (
        restarted_health.bar_store.calendar_preflight_failure_at
        == failure_asof
    )
    assert restarted_health.exit_coverage.fresh is False
    assert restarted_runtime.admission_cycle(failure_asof).failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert admit_calls == []

    calendar.fault = None
    assert (
        restarted_runtime.bar_cycle(early_recovery_asof).code
        == "bar_cycle_complete"
    )
    assert (
        restarted_runtime.health().bar_store.calendar_preflight_failure_at
        == failure_asof
    )
    assert restarted_runtime.admission_cycle(early_recovery_asof).failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert (
        restarted_runtime.bar_cycle(late_recovery_asof).code
        == "bar_cycle_complete"
    )
    recovered = restarted_runtime.health()
    assert recovered.bar_store.calendar_preflight_failure_at is None
    assert recovered.bar_store.calendar_preflight_failure is None
    assert recovered.exit_coverage.fresh is True
    assert restarted_runtime.admission_cycle(late_recovery_asof).admitted_count == 1
    assert admit_calls == [event.event_id]
    assert SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    ).health().calendar_preflight_failure_at is None
    with sqlite3.connect(path) as connection:
        failure_row = connection.execute(
            """
            SELECT failed_at, resolved_at, resolved_by_bar_closed_at,
                   payload_sha256
            FROM trusted_paper_bar_calendar_preflight
            """
        ).fetchone()
        resolution_row = connection.execute(
            """
            SELECT resolved_at, resolved_by_bar_closed_at, payload_sha256
            FROM trusted_paper_bar_calendar_preflight_resolution
            """
        ).fetchone()
    assert failure_row[:3] == (failure_asof.isoformat(), None, None)
    assert failure_row[3].startswith("sha256:")
    assert resolution_row[:2] == (
        late_recovery_asof.isoformat(),
        failed.isoformat(),
    )
    assert resolution_row[2].startswith("sha256:")


def test_calendar_preflight_write_failure_latches_process_until_restart(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)

    class FaultingCalendar(_Calendar):
        fail = False

        def session_for(self, requested_day):
            if self.fail:
                raise RuntimeError("calendar_backend_unavailable")
            return super().session_for(requested_day)

    calendar = FaultingCalendar((trading_day,))
    first, failed = calendar.session_for(
        trading_day
    ).expected_bar_closes[12:14]
    code = "SH.600001"

    class Provider:
        def universe_provider(self, _requested):
            return SimpleNamespace(
                securities=(SimpleNamespace(code=code),)
            )

        def required_codes(self, _requested):
            return ()

        def failures(self, _requested):
            return {}

        def paper_bar(self, requested_code, requested):
            return _bar(requested, code=requested_code)

    path = tmp_path / "calendar-preflight-write-failure.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    event = SimpleNamespace(
        event_id="event-after-preflight-write-failure",
        code=code,
        bar_closed_at=first,
    )
    admit_calls: list[str] = []
    gateway = SimpleNamespace(
        process_bar=lambda _bar: (),
        admit=lambda event_id, *_args, **_kwargs: admit_calls.append(
            event_id
        ),
    )
    event_store = SimpleNamespace(
        list_events=lambda: (event,),
        get_snapshot=lambda _event_id: SimpleNamespace(
            state=EventState.CONFIRMED
        ),
        list_risk_snapshots=lambda _event_id: (
            SimpleNamespace(snapshot_id="risk-preflight-write-failure"),
        ),
    )

    def build_runtime(bar_store):
        return PaperResearchRuntime(
            data_provider=Provider(),
            analysis_runtime=SimpleNamespace(
                scan_cycle=lambda at: SimpleNamespace(
                    code="scan_complete",
                    bar_closed_at=at.replace(second=0, microsecond=0),
                )
            ),
            bar_store=bar_store,
            paper_gateway=gateway,
            event_store=event_store,
            exit_cycle=lambda at: PaperExitCycleResult(at, 0, {}),
            trading_calendar=calendar,
        )

    runtime = build_runtime(store)
    assert runtime.bar_cycle(first).code == "bar_cycle_complete"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_calendar_preflight_insert
            BEFORE INSERT ON trusted_paper_bar_calendar_preflight
            BEGIN
                SELECT RAISE(FAIL, 'injected_preflight_write_failure');
            END
            """
        )

    calendar.fail = True
    assert runtime.bar_cycle(failed).code == "bar_cycle_failed"
    health = runtime.health().bar_store
    assert health.degraded is True
    assert health.degraded_reason == "paper_calendar_preflight_persistence_failed"
    assert runtime.admission_cycle(failed).failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert admit_calls == []

    restarted_store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    restarted = restarted_store.health()
    assert restarted.degraded is True
    assert restarted.degraded_reason == "paper_calendar_preflight_persistence_failed"
    assert restarted.last_attempted_bar_closed_at == first
    assert restarted.last_attempt_complete is True
    restarted_runtime = build_runtime(restarted_store)
    assert restarted_runtime.admission_cycle(failed).failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert admit_calls == []

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER reject_calendar_preflight_insert")
    calendar.fail = False
    recovery_asof = failed + timedelta(seconds=1)
    assert restarted_runtime.bar_cycle(recovery_asof).code == "bar_cycle_complete"
    assert restarted_runtime.health().bar_store.degraded is False
    assert restarted_runtime.admission_cycle(recovery_asof).admitted_count == 1
    assert admit_calls == [event.event_id]


def test_calendar_preflight_db_and_primary_sidecar_failure_survives_restart(
    tmp_path,
    monkeypatch,
) -> None:
    path = tmp_path / "calendar-preflight-dual-write-failure.sqlite3"
    store = SQLiteTrustedPaperBarStore(path)
    failed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_calendar_preflight_insert
            BEFORE INSERT ON trusted_paper_bar_calendar_preflight
            BEGIN
                SELECT RAISE(FAIL, 'injected_preflight_write_failure');
            END
            """
        )

    primary_dir = store._preflight_fail_stop_dir
    original_mkdir = Path.mkdir

    def reject_primary_sidecar_dir(directory, *args, **kwargs):
        if directory == primary_dir:
            raise OSError("injected_primary_sidecar_failure")
        return original_mkdir(directory, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", reject_primary_sidecar_dir)
    with pytest.raises(sqlite3.IntegrityError, match="injected_preflight"):
        store.record_calendar_preflight_failure(
            failed_at=failed_at,
            reason="calendar_backend_unavailable",
        )
    assert store.health().degraded_reason == (
        "paper_calendar_preflight_persistence_failed"
    )

    monkeypatch.setattr(Path, "mkdir", original_mkdir)
    restarted = SQLiteTrustedPaperBarStore(path).health()
    assert restarted.degraded is True
    assert restarted.degraded_reason == (
        "paper_calendar_preflight_persistence_failed"
    )
    assert restarted.calendar_preflight_failure_at == failed_at


def test_calendar_preflight_fail_stop_propagates_to_already_open_store(
    tmp_path,
) -> None:
    path = tmp_path / "calendar-preflight-multi-instance.sqlite3"
    writer = SQLiteTrustedPaperBarStore(path)
    already_open = SQLiteTrustedPaperBarStore(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_calendar_preflight_insert
            BEFORE INSERT ON trusted_paper_bar_calendar_preflight
            BEGIN
                SELECT RAISE(FAIL, 'injected_preflight_write_failure');
            END
            """
        )

    failed_at = datetime(2026, 7, 14, 10, 35, 30, tzinfo=CN)
    with pytest.raises(sqlite3.DatabaseError):
        writer.record_calendar_preflight_failure(
            failed_at=failed_at,
            reason="calendar_backend_unavailable",
        )

    health = already_open.health()
    assert health.degraded is True
    assert health.degraded_reason == "paper_calendar_preflight_persistence_failed"
    assert health.calendar_preflight_failure_at == failed_at


def test_calendar_preflight_recovery_cannot_delete_newer_concurrent_fail_stop(
    tmp_path,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    path = tmp_path / "calendar-preflight-concurrent-fail-stop.sqlite3"
    stores = tuple(
        SQLiteTrustedPaperBarStore(
            path,
            calendar_fingerprint=calendar.fingerprint,
        )
        for _ in range(3)
    )
    older_writer, newer_writer, observer = stores
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_calendar_preflight_insert
            BEFORE INSERT ON trusted_paper_bar_calendar_preflight
            BEGIN
                SELECT RAISE(FAIL, 'injected_preflight_write_failure');
            END
            """
        )

    bar_closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    older_failure = bar_closed_at + timedelta(seconds=10)
    recovery_observed_at = bar_closed_at + timedelta(seconds=30)
    newer_failure = bar_closed_at + timedelta(seconds=50)
    for writer, failed_at in (
        (older_writer, older_failure),
        (newer_writer, newer_failure),
    ):
        with pytest.raises(sqlite3.DatabaseError):
            writer.record_calendar_preflight_failure(
                failed_at=failed_at,
                reason="calendar_backend_unavailable",
            )

    recovery_bar = _bar(bar_closed_at)
    older_writer.record_cycle(
        session=calendar.session_for(trading_day),
        bar_closed_at=bar_closed_at,
        required_codes=(recovery_bar.code,),
        optional_codes=(),
        bars={recovery_bar.code: recovery_bar},
        optional_failures={},
    )
    older_writer.complete_cycle(
        bar_closed_at,
        calendar_observed_at=recovery_observed_at,
    )

    health = observer.health()
    assert health.degraded is True
    assert health.degraded_reason == "paper_calendar_preflight_persistence_failed"
    assert health.calendar_preflight_failure_at == newer_failure


@pytest.mark.parametrize("resolved", (False, True))
def test_calendar_preflight_log_row_deletion_is_detected_and_fails_closed(
    tmp_path,
    resolved,
) -> None:
    trading_day = date(2026, 7, 14)
    calendar = _Calendar((trading_day,))
    path = tmp_path / f"preflight-delete-{resolved}.sqlite3"
    store = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    failure_at = datetime(2026, 7, 14, 10, 35, 30, tzinfo=CN)
    store.record_calendar_preflight_failure(
        failed_at=failure_at,
        reason="calendar_backend_unavailable",
    )
    if resolved:
        recovery_close = datetime(2026, 7, 14, 10, 40, tzinfo=CN)
        recovery_bar = _bar(recovery_close)
        store.record_cycle(
            session=calendar.session_for(trading_day),
            bar_closed_at=recovery_close,
            required_codes=(recovery_bar.code,),
            optional_codes=(),
            bars={recovery_bar.code: recovery_bar},
            optional_failures={},
        )
        store.complete_cycle(
            recovery_close,
            calendar_observed_at=recovery_close,
        )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM trusted_paper_bar_calendar_preflight_resolution"
        )
        connection.execute(
            "DELETE FROM trusted_paper_bar_calendar_preflight"
        )

    restarted = SQLiteTrustedPaperBarStore(
        path,
        calendar_fingerprint=calendar.fingerprint,
    )
    health = restarted.health()
    assert health.degraded is True
    assert health.degraded_reason == "paper_calendar_preflight_log_sequence_invalid"
    gateway = object.__new__(TrustedPaperAdmission)
    gateway._bar_source = restarted
    with pytest.raises(
        TrustedPaperAdmissionError,
        match="trusted_paper_bar_source_degraded",
    ):
        gateway._validate_bar_source_health()


def test_degraded_trusted_bar_store_blocks_future_fill_and_scan(tmp_path) -> None:
    store = SQLiteTrustedPaperBarStore(tmp_path / "degraded-cycle.sqlite3")
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    bar = _bar(closed_at)
    store.put(bar)
    with pytest.raises(TrustedPaperBarIntegrityError):
        store.put(replace(bar, open_price=Decimal("10.26")))
    calls: list[str] = []

    class Provider:
        def universe_provider(self, _requested):
            calls.append("freeze")
            return SimpleNamespace(securities=())

        def paper_bar(self, *_args):
            calls.append("bar")
            raise AssertionError("degraded store must block bar creation")

    class Gateway:
        def process_bar(self, _bar):
            calls.append("fill")
            return ()

    class Analysis:
        def scan_cycle(self, _asof):
            calls.append("scan")

    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=Analysis(),
        bar_store=store,
        paper_gateway=Gateway(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    result = runtime.bar_cycle(closed_at + timedelta(minutes=5))

    assert result.code == "bar_cycle_failed"
    assert result.detail == "TrustedPaperBarIntegrityError"
    assert calls == []


def test_degraded_trusted_bar_store_blocks_new_admission(tmp_path) -> None:
    store = SQLiteTrustedPaperBarStore(tmp_path / "degraded-admit.sqlite3")
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    bar = _bar(closed_at)
    store.put(bar)
    with pytest.raises(TrustedPaperBarIntegrityError):
        store.put(replace(bar, open_price=Decimal("10.26")))
    event = SimpleNamespace(
        event_id="a:degraded-admission",
        code=bar.code,
        bar_closed_at=bar.closed_at,
    )
    admit_calls: list[str] = []

    class Gateway:
        def admit(self, event_id, *_args, **_kwargs):
            admit_calls.append(event_id)

    runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=store,
        paper_gateway=Gateway(),
        event_store=SimpleNamespace(
            list_events=lambda: (event,),
            get_snapshot=lambda _event_id: SimpleNamespace(
                state=EventState.CONFIRMED
            ),
            list_risk_snapshots=lambda _event_id: (
                SimpleNamespace(snapshot_id="risk:degraded"),
            ),
        ),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    result = runtime.admission_cycle(closed_at)

    assert result.admitted_count == 0
    assert result.failures == {
        event.event_id: "TrustedPaperBarIntegrityError"
    }
    assert admit_calls == []


def test_paper_research_bar_cycle_orders_attest_fill_exit_scan_on_one_cycle(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    bar = _bar(closed_at)
    calls: list[tuple[str, object]] = []

    class Provider:
        freeze_count = 0

        def universe_provider(self, requested):
            self.freeze_count += 1
            calls.append(("freeze", requested))
            return SimpleNamespace(
                observed_at=requested,
                securities=(SimpleNamespace(code=bar.code),),
            )

        def paper_bar(self, code, requested):
            calls.append(("bar", requested))
            assert self.freeze_count == 1
            assert code == bar.code
            return bar

        def required_codes(self, _requested):
            return ()

    class Gateway:
        def process_bar(self, trusted):
            calls.append(("fill", trusted.closed_at))
            assert runtime.bar_store.get_bar(trusted.bar_id) == trusted
            return ()

        def admit(self, *_args, **_kwargs):
            raise AssertionError("admit is not part of bar_cycle")

    class Analysis:
        def scan_cycle(self, asof):
            calls.append(("scan", asof.replace(second=0, microsecond=0)))
            assert provider.freeze_count == 1
            return SimpleNamespace(
                code="scan_complete",
                bar_closed_at=asof.replace(second=0, microsecond=0),
            )

    provider = Provider()
    runtime = PaperResearchRuntime(
        data_provider=provider,
        analysis_runtime=Analysis(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "cycle.sqlite3"),
        paper_gateway=Gateway(),
        event_store=SimpleNamespace(list_events=lambda: ()),
        exit_cycle=lambda at: (
            calls.append(("exit", at))
            or PaperExitCycleResult(at, 0, {})
        ),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    result = runtime.bar_cycle(closed_at + timedelta(seconds=30))

    assert result.code == "bar_cycle_complete"
    assert [name for name, _value in calls] == [
        "freeze",
        "bar",
        "fill",
        "exit",
        "scan",
    ]
    assert len({value for _name, value in calls}) == 1


def test_paper_research_cycle_prepares_and_atomically_completes_observation_batch(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    bar = _bar(closed_at)
    prepared = object()
    calls: list[str] = []

    class Provider:
        def universe_provider(self, requested):
            calls.append("freeze")
            return SimpleNamespace(
                observed_at=requested,
                securities=(SimpleNamespace(code=bar.code),),
            )

        def paper_bar(self, _code, _requested):
            calls.append("bar")
            return bar

        def required_codes(self, _requested):
            return ()

        def prepare_signal_observation_cycle(self, requested):
            assert requested == closed_at
            calls.append("prepare_observation")
            return prepared

        def signal_observation_batch(self, requested):
            assert requested == closed_at
            calls.append("observation_batch")
            return prepared

    store = SQLiteTrustedPaperBarStore(tmp_path / "observation-cycle.sqlite3")
    original_complete_cycle = store.complete_cycle

    def complete_cycle(requested, **kwargs):
        calls.append("complete")
        assert kwargs.pop("signal_observation_batch") is prepared
        original_complete_cycle(requested, **kwargs)

    store.complete_cycle = complete_cycle
    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=SimpleNamespace(
            scan_cycle=lambda asof: (
                calls.append("scan")
                or SimpleNamespace(
                    code="scan_complete",
                    bar_closed_at=asof.replace(second=0, microsecond=0),
                )
            )
        ),
        bar_store=store,
        paper_gateway=SimpleNamespace(
            process_bar=lambda _bar: calls.append("fill") or ()
        ),
        event_store=SimpleNamespace(list_events=lambda: ()),
        exit_cycle=lambda at: (
            calls.append("exit") or PaperExitCycleResult(at, 0, {})
        ),
        trading_calendar=_Calendar((closed_at.date(),)),
    )

    result = runtime.bar_cycle(closed_at + timedelta(seconds=30))

    assert result.code == "bar_cycle_complete"
    assert calls == [
        "freeze",
        "bar",
        "prepare_observation",
        "fill",
        "exit",
        "scan",
        "observation_batch",
        "complete",
    ]
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            """
            SELECT attempt_generation FROM trusted_paper_bar_attempt
            WHERE closed_at = ?
            """,
            (closed_at.isoformat(),),
        ).fetchone() == (1,)


@pytest.mark.parametrize(
    (
        "exit_failures",
        "exit_cycle_failure",
        "scan_code",
        "expected_scan_calls",
        "scan_bar_offset",
        "scan_queue_overflow",
    ),
    (
        ({"entry-1": "resolver_failed"}, None, "scan_complete", 0, timedelta(0), 0),
        ({}, "evaluator_unavailable", "scan_complete", 0, timedelta(0), 0),
        ({}, None, "scan_failed", 1, timedelta(0), 0),
        ({}, None, "scan_complete", 1, timedelta(minutes=-5), 0),
        ({}, None, "scan_complete", 1, timedelta(0), 1),
    ),
)
def test_bar_cycle_requires_complete_exit_coverage_and_successful_scan(
    tmp_path,
    exit_failures,
    exit_cycle_failure,
    scan_code,
    expected_scan_calls,
    scan_bar_offset,
    scan_queue_overflow,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    bar = _bar(closed_at)
    scan_calls: list[datetime] = []

    class Provider:
        def universe_provider(self, requested):
            return SimpleNamespace(
                observed_at=requested,
                securities=(SimpleNamespace(code=bar.code),),
            )

        def paper_bar(self, code, requested):
            assert code == bar.code
            assert requested == closed_at
            return bar

        def required_codes(self, _requested):
            return ()

    class Analysis:
        def scan_cycle(self, asof):
            scan_calls.append(asof)
            return SimpleNamespace(
                code=scan_code,
                bar_closed_at=(
                    asof.replace(second=0, microsecond=0) + scan_bar_offset
                ),
                queue_overflow=scan_queue_overflow,
            )

    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=Analysis(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "strict-cycle.sqlite3"),
        paper_gateway=SimpleNamespace(process_bar=lambda _bar: ()),
        event_store=SimpleNamespace(list_events=lambda: ()),
        exit_cycle=lambda at: PaperExitCycleResult(
            at,
            0,
            exit_failures,
            exit_cycle_failure,
        ),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    result = runtime.bar_cycle(closed_at + timedelta(seconds=30))

    assert result.code == "bar_cycle_failed"
    assert runtime.health().bar_cycle_failures == 1
    assert len(scan_calls) == expected_scan_calls


def test_bar_cycle_rejects_stale_exit_commitment_before_scan(tmp_path) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    bar = _bar(closed_at)
    scan_calls: list[datetime] = []

    class Provider:
        def universe_provider(self, requested):
            return SimpleNamespace(
                observed_at=requested,
                securities=(SimpleNamespace(code=bar.code),),
            )

        def paper_bar(self, _code, _requested):
            return bar

        def required_codes(self, _requested):
            return ()

    class Analysis:
        def scan_cycle(self, asof):
            scan_calls.append(asof)
            return SimpleNamespace(
                code="scan_complete",
                bar_closed_at=closed_at,
                queue_overflow=0,
            )

    stale = ExitEvaluationCommitment.from_snapshot(
        _exit_snapshot(closed_at - timedelta(minutes=5))
    )
    runtime = PaperResearchRuntime(
        data_provider=Provider(),
        analysis_runtime=Analysis(),
        bar_store=SQLiteTrustedPaperBarStore(
            tmp_path / "stale-exit-before-scan.sqlite3"
        ),
        paper_gateway=SimpleNamespace(process_bar=lambda _bar: ()),
        event_store=SimpleNamespace(list_events=lambda: ()),
        exit_cycle=lambda at: PaperExitCycleResult(
            at,
            1,
            {},
            commitments=(stale,),
        ),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    result = runtime.bar_cycle(closed_at + timedelta(seconds=30))

    assert result.code == "bar_cycle_failed"
    assert result.detail == "TrustedPaperBarIntegrityError"
    assert scan_calls == []


def test_old_exit_snapshot_is_an_explicit_current_cycle_failure() -> None:
    snapshot_ids, failures, cycle_failure = (
        PaperExitAnalysisCycle._validate_batch_outcomes(
            {"entry-1": "sha256:" + "1" * 64},
            SimpleNamespace(
                snapshots=(
                    SimpleNamespace(
                        entry_event_id="entry-1",
                        evaluation_cycle_id="sha256:" + "0" * 64,
                    ),
                ),
                failures=(),
            ),
        )
    )

    assert snapshot_ids == ()
    assert failures == {"entry-1": "exit_snapshot_cycle_mismatch"}
    assert cycle_failure is None


def test_evaluator_cycle_failure_without_open_entries_is_durable(tmp_path) -> None:
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    ledger_state = SimpleNamespace(lots=(), intents=())
    ledger = SimpleNamespace(
        load=lambda: ledger_state,
        account_snapshot=lambda: SimpleNamespace(
            cash_balance=Decimal("100000"),
            available_buying_power=Decimal("100000"),
        ),
    )
    exit_service = object.__new__(ExitEvaluationService)

    def fail_evaluation(_requests):
        raise RuntimeError("evaluator_unavailable")

    exit_service.evaluate_and_persist_many = fail_evaluation
    risk_path = tmp_path / "empty-position-evaluator-failure.sqlite3"
    cycle = PaperExitAnalysisCycle(
        ledger=ledger,
        data_provider=SimpleNamespace(
            structure_for_code=lambda *_args: None,
            quote_for_code=lambda *_args: None,
        ),
        entry_resolver=SimpleNamespace(resolve=lambda *_args: None),
        risk_state=SQLitePaperRiskState(
            risk_path,
            policy=RiskPolicy.conservative(),
        ),
        exit_service=exit_service,
    )

    result = cycle(at)
    restarted = SQLitePaperRiskState(
        risk_path,
        policy=RiskPolicy.conservative(),
    ).latest_exit_coverage()

    assert result.evaluated_count == 0
    assert result.failures == {}
    assert result.cycle_failure == "evaluator_unavailable"
    assert restarted is not None
    assert restarted.cycle_failure == "evaluator_unavailable"


def test_exit_coverage_failure_and_scan_outcome_survive_restart(tmp_path) -> None:
    path = tmp_path / "durable-exit-coverage.sqlite3"
    policy = RiskPolicy.conservative()
    state = SQLitePaperRiskState(path, policy=policy)
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)

    assert hasattr(state, "record_exit_coverage")
    state.record_exit_coverage(
        bar_closed_at=at,
        open_entry_ids=("entry-1", "entry-2"),
        snapshot_entry_ids=("entry-1",),
        failures={"entry-2": "resolver_failed"},
    )
    state.record_exit_scan_outcome(at, "scan_failed")

    restarted = SQLitePaperRiskState(path, policy=policy)
    coverage = restarted.latest_exit_coverage()

    assert coverage is not None
    assert coverage.bar_closed_at == at
    assert coverage.open_entry_ids == ("entry-1", "entry-2")
    assert coverage.snapshot_entry_ids == ("entry-1",)
    assert coverage.failures == {"entry-2": "resolver_failed"}
    assert coverage.scan_code == "scan_failed"
    assert coverage.complete is True


def test_paper_research_admission_cycle_replays_failure_and_is_idempotent(
    tmp_path,
) -> None:
    closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    calendar = _Calendar((date(2026, 7, 14),))
    bar_store = SQLiteTrustedPaperBarStore(
        tmp_path / "admission.sqlite3",
        calendar_fingerprint=calendar.fingerprint,
    )
    signal_bar = _bar(closed_at)
    bar_store.record_cycle(
        session=calendar.session_for(closed_at.date()),
        bar_closed_at=closed_at,
        required_codes=(signal_bar.code,),
        optional_codes=(),
        bars={signal_bar.code: signal_bar},
        optional_failures={},
    )
    bar_store.complete_cycle(closed_at)
    event = SimpleNamespace(
        event_id="a:paper-replay",
        code="SH.600001",
        bar_closed_at=closed_at,
    )
    risk = SimpleNamespace(snapshot_id="risk:paper-replay")

    class Store:
        def list_events(self):
            return (event,)

        def get_snapshot(self, event_id):
            assert event_id == event.event_id
            return SimpleNamespace(state=EventState.CONFIRMED, event=event)

        def list_risk_snapshots(self, event_id):
            assert event_id == event.event_id
            return (risk,)

    class Gateway:
        calls = 0
        committed = False

        def process_bar(self, _bar):
            return ()

        def admit(self, event_id, signal_bar, *, risk_snapshot_id):
            self.calls += 1
            assert signal_bar == bar_store.get_bar(signal_bar.bar_id)
            if self.calls == 1:
                raise RuntimeError("crash_after_event_outbox")
            self.committed = True
            return SimpleNamespace(event_id=event_id, risk_snapshot_id=risk_snapshot_id)

    gateway = Gateway()
    runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=bar_store,
        paper_gateway=gateway,
        event_store=Store(),
        trading_calendar=calendar,
    )

    first = runtime.admission_cycle(closed_at)
    second = runtime.admission_cycle(closed_at + timedelta(seconds=1))
    third = runtime.admission_cycle(closed_at + timedelta(seconds=2))

    assert first.admitted_count == 0
    assert first.failures == {event.event_id: "RuntimeError"}
    assert second.admitted_count == 1
    assert second.failures == {}
    assert third.admitted_count == 0
    assert third.failures == {}
    assert gateway.calls == 2


def test_paper_admission_never_consumes_event_outside_current_strategy_run(
    tmp_path,
) -> None:
    event = SimpleNamespace(
        event_id="a:previous-strategy-run",
        code="SH.600001",
        bar_closed_at=datetime(2026, 7, 14, 10, 35, tzinfo=CN),
    )
    admitted: list[str] = []
    runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "epoch-event-gate.sqlite3"),
        paper_gateway=SimpleNamespace(
            admit=lambda event_id, *_args, **_kwargs: admitted.append(event_id)
        ),
        event_store=SimpleNamespace(
            list_events=lambda: (event,),
            get_snapshot=lambda _event_id: SimpleNamespace(
                state=EventState.CONFIRMED
            ),
            list_risk_snapshots=lambda _event_id: (),
        ),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
        event_eligibility_provider=lambda candidate: candidate is not event,
    )

    result = runtime.admission_cycle(event.bar_closed_at)

    assert result.admitted_count == 0
    assert result.skipped_count == 1
    assert result.failures == {}
    assert admitted == []


def test_paper_pinned_codes_include_lots_pending_intents_and_unconsumed_events() -> None:
    ledger = SimpleNamespace(
        load=lambda: SimpleNamespace(
            lots=(SimpleNamespace(code="SH.600001"),),
            intents=(
                SimpleNamespace(
                    event_id="a:pending",
                    code="SZ.000001",
                    remaining_shares=100,
                    status="pending_next_bar",
                ),
                SimpleNamespace(
                    event_id="a:done",
                    code="SH.600002",
                    remaining_shares=0,
                    status="filled",
                ),
            ),
        )
    )
    events = (
        SimpleNamespace(event_id="a:confirmed", code="SH.600003"),
        SimpleNamespace(event_id="a:old-run", code="SH.600005"),
        SimpleNamespace(event_id="a:invalid", code="SH.600004"),
    )
    states = {
        "a:confirmed": EventState.CONFIRMED,
        "a:old-run": EventState.CONFIRMED,
        "a:invalid": EventState.INVALIDATED,
    }
    store = SimpleNamespace(
        list_events=lambda: events,
        get_snapshot=lambda event_id: SimpleNamespace(state=states[event_id]),
    )

    provider = make_paper_pinned_codes_provider(
        ledger,
        store,
        event_eligibility_provider=lambda event: event.event_id != "a:old-run",
    )

    assert provider() == ("SH.600001", "SH.600003", "SZ.000001")


def test_paper_risk_state_persists_day_start_high_water_and_sticky_latches(
    tmp_path,
) -> None:
    path = tmp_path / "paper-risk.sqlite3"
    policy = RiskPolicy.conservative()
    tracker = SQLitePaperRiskState(path, policy=policy)
    at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)

    first = tracker.mark(Decimal("100000"), at)
    peak = tracker.mark(Decimal("110000"), at + timedelta(minutes=5))
    loss = tracker.mark(Decimal("98000"), at + timedelta(minutes=10))

    assert first.day_start_equity == Decimal("100000")
    assert peak.high_water_equity == Decimal("110000")
    assert loss.day_pnl == Decimal("-2000")
    assert loss.daily_loss_locked is True
    assert loss.drawdown_locked is True
    assert SQLitePaperRiskState(path, policy=policy).mark(
        Decimal("100000"),
        at + timedelta(minutes=15),
    ).daily_loss_locked is True

    next_day = tracker.mark(
        Decimal("101000"),
        datetime(2026, 7, 15, 9, 35, tzinfo=CN),
    )
    assert next_day.day_start_equity == Decimal("101000")
    assert next_day.daily_loss_locked is False
    assert next_day.drawdown_locked is True

    changed = replace(policy, daily_loss_fraction=Decimal("0.02"))
    with pytest.raises(TrustedPaperBarIntegrityError, match="risk_policy_mismatch"):
        SQLitePaperRiskState(path, policy=changed)


@pytest.mark.skipif(
    not Path("audit/chanlun_lesson_corpus_v3").is_dir(),
    reason="optional certified legacy corpus package is not versioned",
)
def test_all_open_positions_are_evaluated_when_removed_from_entry_universe(
    tmp_path,
    make_decision_event,
) -> None:
    bar_closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    events = {
        event.event_id: event
        for event in (
            make_decision_event(code="SH.600001", name="position-one"),
            make_decision_event(code="SZ.000001", name="position-two"),
        )
    }
    lots = tuple(
        SimpleNamespace(
            entry_event_id=event.event_id,
            code=event.code,
            shares=100,
            price=Decimal("10"),
            opened_at=event.observed_at + timedelta(minutes=5),
        )
        for event in events.values()
    )
    ledger_state = SimpleNamespace(lots=lots, intents=())

    class Ledger:
        def load(self):
            return ledger_state

        def account_snapshot(self):
            return SimpleNamespace(
                cash_balance=Decimal("100000"),
                available_buying_power=Decimal("100000"),
            )

    class Levels:
        frequency = "5m"

        def get_recursive_branch_levels(self):
            return (
                SimpleNamespace(
                    level=1,
                    frequency="5m",
                    direction="up",
                    completed=True,
                    start=None,
                    end=None,
                    zss=(),
                    mmds=(),
                    divergences=(),
                ),
                SimpleNamespace(
                    level=2,
                    frequency="30m",
                    direction="up",
                    completed=True,
                    start=None,
                    end=None,
                    zss=(),
                    mmds=(),
                    divergences=(),
                ),
            )

    direct_codes: list[str] = []

    class Provider:
        entry_universe = ()

        def quote_for_code(self, code, requested):
            direct_codes.append(code)
            assert requested == bar_closed_at
            return QuoteSnapshot(
                code=code,
                price=Decimal("10"),
                quote_time=requested,
                entry_tradable=False,
                exit_tradable=True,
                limit_up_locked=False,
                limit_down_locked=False,
            )

        def structure_for_code(self, code, requested):
            assert code in {event.code for event in events.values()}
            assert requested == bar_closed_at
            return SymbolStructureSnapshot(
                frequency="5m",
                cd=Levels(),
                signals=(),
                first_visible_bar=1,
                completed_bars=(
                    {
                        "closed_at": requested,
                        "open": 10.0,
                        "high": 10.2,
                        "low": 9.8,
                        "close": 10.0,
                        "volume": 1000.0,
                    },
                ),
                config={"source": "position-direct"},
                operation_bar_closed=True,
                fund_ok=True,
                comparison_ok=True,
                current_cycle_id="sha256:" + "c" * 64,
            )

    class Resolver:
        def __init__(self):
            self.links: dict[str, AuthoritativeEntryLink] = {}

        def resolve(self, entry_event_id, holding):
            event = events[entry_event_id]
            tracked = TrackedPosition(
                entry_event_id=entry_event_id,
                entry_data_fingerprint=event.data_fingerprint,
                entry_review_id="review:" + entry_event_id,
                entry_risk_snapshot_id="risk:" + entry_event_id,
                entry_paper_admission_id="sha256:" + "a" * 64,
                paper_fill_ids=("fill:" + entry_event_id,),
                paper_ledger_revision=1,
                lot_provenance_fingerprint="sha256:" + "b" * 64,
                strategy_track=event.strategy_track,
                holding=holding,
            )
            link = AuthoritativeEntryLink(
                position=tracked,
                entry_event=event,
                entry_review_id=tracked.entry_review_id,
                entry_risk_snapshot_id=tracked.entry_risk_snapshot_id,
                entry_paper_admission_id=tracked.entry_paper_admission_id,
                paper_fill_ids=tracked.paper_fill_ids,
                paper_ledger_revision=tracked.paper_ledger_revision,
                lot_provenance_fingerprint=tracked.lot_provenance_fingerprint,
            )
            self.links[entry_event_id] = link
            return link

    resolver = Resolver()
    project_root = Path(__file__).resolve().parents[2]
    exit_service = ExitEvaluationService(
        SQLiteExitEvaluationStore(tmp_path / "exit-evaluations.sqlite3"),
        evidence_policy=load_exit_evidence_policy_file(
            project_root
            / "config"
            / "decision_support"
            / "exit_evidence_policy.json",
            corpus=load_certified_lesson_corpus(
                project_root / "audit" / "chanlun_lesson_corpus_v3"
            ),
        ),
        entry_ledger_resolver=lambda candidate: resolver.links.get(
            candidate.entry_event_id
        ),
    )
    cycle = PaperExitAnalysisCycle(
        ledger=Ledger(),
        data_provider=Provider(),
        entry_resolver=resolver,
        risk_state=SQLitePaperRiskState(
            tmp_path / "risk.sqlite3",
            policy=RiskPolicy.conservative(),
        ),
        exit_service=exit_service,
    )

    result = cycle(bar_closed_at)

    assert result.evaluated_count == 2
    assert result.failures == {}
    assert set(direct_codes) == {event.code for event in events.values()}
    assert Provider.entry_universe == ()
    assert exit_service.store.revision == 2
    coverage = cycle.latest_coverage()
    assert coverage is not None
    assert coverage.bar_closed_at == bar_closed_at
    assert set(coverage.snapshot_entry_ids) == set(events)
    assert coverage.failures == {}
    assert coverage.cycle_failure is None


def test_paper_scheduler_registers_single_bar_review_and_admission_jobs(
    tmp_path,
) -> None:
    jobs: list[dict[str, object]] = []

    class Scheduler:
        def add_job(self, func, **kwargs):
            row = {"func": func, **kwargs}
            jobs.append(row)
            return row

    paper_runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "jobs.sqlite3"),
        paper_gateway=SimpleNamespace(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )
    analysis_runtime = SimpleNamespace(
        config=SimpleNamespace(enabled=True, scan_interval_seconds=30),
        review_cycle=lambda: None,
    )

    registered = register_paper_research_jobs(
        Scheduler(),
        paper_runtime,
        analysis_runtime,
        strategy_run=SimpleNamespace(
            status_payload=lambda: {
                "state": "active",
                "evidence_scope": "current_epoch_only",
                "store_bindings_complete": True,
            },
            mutation_lease=lambda _operation: nullcontext(),
        ),
    )

    assert set(registered) == {
        "decision_support_bar_cycle",
        "decision_support_review",
        "decision_support_paper_admission",
    }
    assert {job["id"] for job in jobs} == {
        "decision_support_bar_cycle",
        "decision_support_review",
        "decision_support_paper_admission",
    }
    assert all(job["max_instances"] == 1 for job in jobs)
    assert all(job["executor"] == "default" for job in jobs)
    assert "decision_support_scan" not in {job["id"] for job in jobs}


def test_paper_scheduler_bar_job_orders_observer_paper_and_reconcile(
    tmp_path,
    monkeypatch,
) -> None:
    jobs: list[dict[str, object]] = []
    calls: list[tuple[str, datetime]] = []
    paper_result = object()

    class Scheduler:
        def add_job(self, func, **kwargs):
            row = {"func": func, **kwargs}
            jobs.append(row)
            return row

    paper_runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "ordered-jobs.sqlite3"),
        paper_gateway=SimpleNamespace(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    def observe(as_of):
        calls.append(("observe", as_of))

    def bar_cycle(as_of):
        calls.append(("paper", as_of))
        return paper_result

    def reconcile(as_of):
        calls.append(("reconcile", as_of))
        return ()

    monkeypatch.setattr(paper_runtime, "bar_cycle", bar_cycle)
    registered = register_paper_research_jobs(
        Scheduler(),
        paper_runtime,
        SimpleNamespace(
            config=SimpleNamespace(enabled=True, scan_interval_seconds=30),
            review_cycle=lambda: None,
        ),
        strategy_run=SimpleNamespace(
            status_payload=lambda: {
                "state": "active",
                "evidence_scope": "current_epoch_only",
                "store_bindings_complete": True,
            },
            mutation_lease=lambda _operation: nullcontext(),
        ),
        opportunity_runtime=SimpleNamespace(observe=observe),
        research_trigger_coordinator=SimpleNamespace(reconcile=reconcile),
    )

    result = registered["decision_support_bar_cycle"]["func"]()

    assert result is paper_result
    assert tuple(name for name, _ in calls) == (
        "observe",
        "paper",
        "reconcile",
    )
    assert len({as_of for _, as_of in calls}) == 1


def test_paper_scheduler_records_all_bar_phase_failures_without_skipping(
    tmp_path,
    monkeypatch,
) -> None:
    jobs: list[dict[str, object]] = []
    calls: list[str] = []

    class Scheduler:
        def add_job(self, func, **kwargs):
            row = {"func": func, **kwargs}
            jobs.append(row)
            return row

    paper_runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "failed-jobs.sqlite3"),
        paper_gateway=SimpleNamespace(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    def fail(phase):
        def invoke(_as_of):
            calls.append(phase)
            raise RuntimeError(f"{phase}-failed")

        return invoke

    monkeypatch.setattr(paper_runtime, "bar_cycle", fail("paper"))
    registered = register_paper_research_jobs(
        Scheduler(),
        paper_runtime,
        SimpleNamespace(
            config=SimpleNamespace(enabled=True, scan_interval_seconds=30),
            review_cycle=lambda: None,
        ),
        strategy_run=SimpleNamespace(
            status_payload=lambda: {
                "state": "active",
                "evidence_scope": "current_epoch_only",
                "store_bindings_complete": True,
            },
            mutation_lease=lambda _operation: nullcontext(),
        ),
        opportunity_runtime=SimpleNamespace(observe=fail("observer")),
        research_trigger_coordinator=SimpleNamespace(
            reconcile=fail("coordinator")
        ),
    )

    with pytest.raises(
        paper_runtime_module.PaperResearchBarJobError
    ) as captured:
        registered["decision_support_bar_cycle"]["func"]()

    assert calls == ["observer", "paper", "coordinator"]
    assert tuple(captured.value.failures) == (
        "observer",
        "paper",
        "coordinator",
    )
    assert {
        phase: str(error)
        for phase, error in captured.value.failures.items()
    } == {
        "observer": "observer-failed",
        "paper": "paper-failed",
        "coordinator": "coordinator-failed",
    }


def test_paper_health_rejects_non_read_only_before_job_registration(
    tmp_path,
    monkeypatch,
) -> None:
    jobs: list[dict[str, object]] = []

    class Scheduler:
        def add_job(self, func, **kwargs):
            row = {"func": func, **kwargs}
            jobs.append(row)
            return row

    paper_runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "unsafe-health.sqlite3"),
        paper_gateway=SimpleNamespace(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )
    monkeypatch.setattr(
        paper_runtime,
        "health",
        lambda: SimpleNamespace(
            mode="research_paper",
            read_only=False,
            auto_order_enabled=False,
            live_order_capability=False,
        ),
    )

    with pytest.raises(RuntimeError, match="read-only"):
        register_paper_research_jobs(
            Scheduler(),
            paper_runtime,
            SimpleNamespace(
                config=SimpleNamespace(enabled=True, scan_interval_seconds=30),
                review_cycle=lambda: None,
            ),
            strategy_run=SimpleNamespace(
                status_payload=lambda: {
                    "state": "active",
                    "evidence_scope": "current_epoch_only",
                    "store_bindings_complete": True,
                },
                mutation_lease=lambda _operation: nullcontext(),
            ),
        )

    assert jobs == []


def test_paper_scheduler_revalidates_strategy_run_before_every_job(
    tmp_path,
    monkeypatch,
) -> None:
    jobs: list[dict[str, object]] = []
    calls = {"bar": 0, "review": 0, "admission": 0}

    class Scheduler:
        def add_job(self, func, **kwargs):
            row = {"func": func, **kwargs}
            jobs.append(row)
            return row

    paper_runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "guarded-jobs.sqlite3"),
        paper_gateway=SimpleNamespace(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )
    monkeypatch.setattr(
        paper_runtime,
        "bar_cycle",
        lambda *_: calls.__setitem__("bar", calls["bar"] + 1),
    )
    monkeypatch.setattr(
        paper_runtime,
        "admission_cycle",
        lambda *_: calls.__setitem__("admission", calls["admission"] + 1),
    )
    analysis_runtime = SimpleNamespace(
        config=SimpleNamespace(enabled=True, scan_interval_seconds=30),
        review_cycle=lambda: calls.__setitem__("review", calls["review"] + 1),
    )

    register_paper_research_jobs(
        Scheduler(),
        paper_runtime,
        analysis_runtime,
        strategy_run=SimpleNamespace(
            status_payload=lambda: (_ for _ in ()).throw(
                RuntimeError("strategy_run_store_file_replaced")
            ),
            mutation_lease=lambda _operation: nullcontext(),
        ),
    )

    for job in jobs:
        with pytest.raises(RuntimeError, match="store_file_replaced"):
            job["func"]()
    assert calls == {"bar": 0, "review": 0, "admission": 0}


def test_paper_scheduler_holds_mutation_lease_around_each_job_body(
    tmp_path,
    monkeypatch,
) -> None:
    jobs: list[dict[str, object]] = []
    probe = _LeaseProbe()

    class Scheduler:
        def add_job(self, func, **kwargs):
            row = {"func": func, **kwargs}
            jobs.append(row)
            return row

    paper_runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(tmp_path / "leased-jobs.sqlite3"),
        paper_gateway=SimpleNamespace(),
        event_store=SimpleNamespace(),
        trading_calendar=_Calendar((date(2026, 7, 14),)),
    )

    def bar_cycle(*_args):
        assert probe.active == ["paper_scheduler.bar_job"]

    def admission_cycle(*_args):
        assert probe.active == ["paper_scheduler.admission_job"]

    def review_cycle():
        assert probe.active == ["paper_scheduler.review_job"]

    monkeypatch.setattr(paper_runtime, "bar_cycle", bar_cycle)
    monkeypatch.setattr(paper_runtime, "admission_cycle", admission_cycle)
    register_paper_research_jobs(
        Scheduler(),
        paper_runtime,
        SimpleNamespace(
            config=SimpleNamespace(enabled=True, scan_interval_seconds=30),
            review_cycle=review_cycle,
        ),
        strategy_run=probe,
    )

    for job in jobs:
        job["func"]()

    assert probe.active == []
    assert probe.entered == [
        "paper_scheduler.bar_job",
        "paper_scheduler.review_job",
        "paper_scheduler.admission_job",
    ]
    assert probe.exited == probe.entered


def test_paper_runtime_direct_cycles_hold_mutation_lease(tmp_path) -> None:
    probe = _LeaseProbe()
    event_store = SimpleNamespace(
        list_events=lambda: (),
        get_snapshot=lambda _event_id: None,
        list_risk_snapshots=lambda _event_id: (),
    )
    runtime = PaperResearchRuntime(
        data_provider=SimpleNamespace(),
        analysis_runtime=SimpleNamespace(),
        bar_store=SQLiteTrustedPaperBarStore(
            tmp_path / "direct-runtime-lease.sqlite3",
            calendar_fingerprint=_CALENDAR_FINGERPRINT,
        ),
        paper_gateway=SimpleNamespace(admit=lambda *_args, **_kwargs: None),
        event_store=event_store,
        trading_calendar=_Calendar((date(2026, 7, 14),)),
        strategy_run=probe,
    )
    asof = datetime(2026, 7, 14, 10, 36, tzinfo=CN)

    runtime.bar_cycle(asof)
    runtime.admission_cycle(asof)

    assert probe.active == []
    assert probe.entered == [
        "paper_runtime.bar_cycle",
        "paper_runtime.admission_cycle",
    ]
    assert probe.exited == probe.entered


def test_exit_cycle_direct_mutations_hold_mutation_lease() -> None:
    probe = _LeaseProbe()
    bar_closed_at = datetime(2026, 7, 14, 10, 35, tzinfo=CN)
    cycle = object.__new__(PaperExitAnalysisCycle)
    cycle._strategy_run = probe

    def record_scan_outcome(bar_closed_at, scan_code):
        assert probe.active == ["paper_exit.record_scan_outcome"]
        return (bar_closed_at, scan_code)

    cycle.risk_state = SimpleNamespace(
        record_exit_scan_outcome=record_scan_outcome,
    )
    assert cycle.record_scan_outcome(bar_closed_at, "scan_complete") == (
        bar_closed_at,
        "scan_complete",
    )

    class StopCycle(Exception):
        pass

    def load():
        assert probe.active == ["paper_exit.analysis_cycle"]
        raise StopCycle

    cycle.ledger = SimpleNamespace(load=load)
    with pytest.raises(StopCycle):
        cycle(bar_closed_at)

    assert probe.active == []
    assert probe.entered == [
        "paper_exit.record_scan_outcome",
        "paper_exit.analysis_cycle",
    ]
    assert probe.exited == probe.entered
