from __future__ import annotations

from pathlib import Path

import pytest

from tools import cleanup_v3_historical_backtests as subject


def _minimal_plan(root: Path) -> dict[str, object]:
    source = root / subject.BACKTEST_ROOT / "formal/source.bin"
    target = root / subject.BACKTEST_ROOT / "operational/target.bin"
    old = root / subject.BACKTEST_ROOT / "old/history.bin"
    return {
        "schema": subject.SCHEMA,
        "mode": "DRY_RUN",
        "status": "READY_TO_EXECUTE",
        "promotions": [
            {
                "source": source.relative_to(root).as_posix(),
                "target": target.relative_to(root).as_posix(),
                "file_sha256": subject.sha256_file(source),
                "size_bytes": source.stat().st_size,
                "action": "COPY_REPLACE",
            }
        ],
        "planned_deletion_paths": [old.relative_to(root).as_posix()],
        "preserved_paths": [
            source.relative_to(root).as_posix(),
            target.relative_to(root).as_posix(),
        ],
    }


def test_execute_requires_both_exact_root_and_confirmation_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backtest = tmp_path / subject.BACKTEST_ROOT
    backtest.mkdir(parents=True)
    monkeypatch.setattr(
        subject, "_validate_project_root", lambda value: (tmp_path, backtest)
    )
    monkeypatch.setattr(subject, "_build_plan", lambda root, scope: {})
    monkeypatch.setattr(
        subject,
        "_execute",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("destructive execution must remain unreachable")
        ),
    )

    with pytest.raises(subject.CleanupSafetyError, match="--confirm-root"):
        subject.main(["--root", str(tmp_path), "--execute"])
    with pytest.raises(subject.CleanupSafetyError, match="confirmation-token"):
        subject.main(
            [
                "--root",
                str(tmp_path),
                "--execute",
                "--confirm-root",
                str(backtest),
            ]
        )


def test_execute_promotes_current_bytes_then_deletes_only_planned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backtest = tmp_path / subject.BACKTEST_ROOT
    source = backtest / "formal/source.bin"
    target = backtest / "operational/target.bin"
    old = backtest / "old/history.bin"
    source.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    old.parent.mkdir(parents=True)
    source.write_bytes(b"immutable current input")
    target.write_bytes(b"stale default-path input")
    old.write_bytes(b"obsolete history")
    plan = _minimal_plan(tmp_path)
    monkeypatch.setattr(
        subject,
        "_release_closure",
        lambda root: ({source}, {}, {"verified": True}),
    )
    report = tmp_path / "audit/cleanup.json"

    result = subject._execute(
        root=tmp_path,
        backtest=backtest,
        plan=plan,
        report_path=report,
    )

    assert result["status"] == "COMPLETED"
    assert result["deleted_file_count"] == 1
    assert target.read_bytes() == source.read_bytes()
    assert not old.exists()
    assert report.is_file()
    assert set(subject._relative(tmp_path, item) for item in backtest.rglob("*") if item.is_file()) == {
        "audit/chanlun_trading_system_backtest/formal/source.bin",
        "audit/chanlun_trading_system_backtest/operational/target.bin",
    }


def test_path_list_identity_is_order_sensitive_and_stable() -> None:
    first = subject._path_sha256(("a", "b"))
    assert first == subject._path_sha256(("a", "b"))
    assert first != subject._path_sha256(("b", "a"))
