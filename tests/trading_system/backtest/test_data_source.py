from __future__ import annotations

from datetime import date, datetime, timedelta
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from chanlun.decision_support.trading_system.backtest import data_source
from chanlun.decision_support.trading_system.backtest.data_audit import (
    audit_dataset,
)
from chanlun.decision_support.trading_system.backtest.data_source import (
    BacktestDataConfig,
    CausalStructureReplay,
    MembershipLoad,
    load_point_in_time_dataset,
)
from chanlun.decision_support.trading_system.backtest.report import (
    verify_report_hash,
)
from tests.trading_system.backtest.helpers import (
    BAR_AT,
    BAR_OPEN,
    CN,
    dataset,
    minute_bar,
)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.rows: tuple[dict[str, object], ...] = ()

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def execute(self, statement: str, parameters: object = None) -> None:
        del parameters
        normalized = " ".join(statement.strip().split())
        self.connection.statements.append(normalized)
        if normalized.startswith("SHOW TABLES"):
            self.rows = ()

    def fetchall(self) -> tuple[dict[str, object], ...]:
        return self.rows


class FakeConnection:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


def test_mysql_connection_is_forced_read_only(monkeypatch) -> None:
    connection = FakeConnection()
    monkeypatch.setattr(data_source, "_connect", lambda: connection)

    rows = data_source.load_daily_rows(
        date(2020, 1, 1),
        date(2020, 1, 31),
    )

    assert rows == ()
    assert connection.statements[0] == "SET SESSION TRANSACTION READ ONLY"
    assert all(
        not statement.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        for statement in connection.statements
    )
    assert connection.rolled_back is True
    assert connection.closed is True


def test_sector_loader_returns_only_native_tdx_880_bars(monkeypatch) -> None:
    frame = pd.DataFrame(
        {
            "date": [BAR_AT],
            "open": [1000.0],
            "high": [1010.0],
            "low": [990.0],
            "close": [1005.0],
            "volume": [12345.0],
        }
    )
    captured: dict[str, object] = {}

    def fake_reader(**kwargs):
        captured.update(kwargs)
        return {"tdx-industry:SH.880301": {"1m": frame}}

    monkeypatch.setattr(data_source, "_read_native_sector_frames", fake_reader)

    bars = data_source.load_tdx_native_sector_bars(
        sector_indices={"tdx-industry:SH.880301": "SH.880301"},
        start=BAR_OPEN.date(),
        end=BAR_OPEN.date(),
        max_pages=2,
    )

    assert captured["sector_indices"] == {
        "tdx-industry:SH.880301": "SH.880301"
    }
    assert len(bars) == 1
    assert bars[0].sector_id == "tdx-industry:SH.880301"
    assert bars[0].index_code == "SH.880301"
    assert bars[0].source == "tdx_native_880_index"


def test_current_membership_fallback_is_explicitly_non_historical() -> None:
    catalog = {
        "sectors": [
            {
                "sector_id": "tdx-industry:SH.880301",
                "name": "行业一",
                "kline_code": "SH.880301",
                "member_codes": ["000001"],
            }
        ]
    }

    loaded = data_source.load_sector_memberships(
        codes=("SZ.000001",),
        sessions=(date(2020, 1, 2), date(2020, 1, 3)),
        catalog=catalog,
    )

    assert loaded.as_of_each_session is False
    assert len(loaded.records) == 2
    assert loaded.sector_index_codes == (
        ("tdx-industry:SH.880301", "SH.880301"),
    )


def test_qmt_raw_bar_has_causal_adjustment_known_at(monkeypatch) -> None:
    frame = pd.DataFrame(
        {
            "date": [BAR_OPEN],
            "open": [10.0],
            "high": [10.2],
            "low": [9.9],
            "close": [10.1],
            "volume": [1000.0],
        }
    )
    monkeypatch.setattr(
        data_source,
        "_load_qmt_frames",
        lambda **_kwargs: {"SZ.000001": frame},
    )

    bars = data_source.load_qmt_minute_bars(
        ("SZ.000001",),
        start=BAR_OPEN.date(),
        end=BAR_OPEN.date(),
        chunk_size=20,
    )

    assert len(bars) == 1
    assert bars[0].adjustment_known_at == bars[0].closed_at
    assert bars[0].raw_close == bars[0].analysis_close


def test_empty_universe_does_not_start_qmt(monkeypatch) -> None:
    monkeypatch.setattr(
        data_source,
        "_load_qmt_frames",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("QMT must not be touched")
        ),
    )

    bars = data_source.load_qmt_minute_bars(
        (),
        start=BAR_OPEN.date(),
        end=BAR_OPEN.date(),
        chunk_size=20,
    )

    assert bars == ()


def test_tdx_midday_boundary_label_is_restored_to_1130(monkeypatch) -> None:
    frame = pd.DataFrame(
        {
            "date": [datetime(2026, 7, 20, 13, 0, tzinfo=CN)],
            "open": [1000.0],
            "high": [1010.0],
            "low": [990.0],
            "close": [1005.0],
            "volume": [12345.0],
        }
    )
    monkeypatch.setattr(
        data_source,
        "_read_native_sector_frames",
        lambda **_kwargs: {
            "tdx-industry:SH.880301": {"30m": frame}
        },
    )

    bars = data_source.load_tdx_native_sector_bars(
        sector_indices={"tdx-industry:SH.880301": "SH.880301"},
        start=BAR_OPEN.date(),
        end=BAR_OPEN.date(),
        max_pages=2,
    )

    assert bars[0].closed_at.hour == 11
    assert bars[0].closed_at.minute == 30


def test_missing_status_coverage_makes_loaded_dataset_invalid(monkeypatch) -> None:
    bar = minute_bar()
    monkeypatch.setattr(
        data_source,
        "load_qmt_minute_bars",
        lambda *_args, **_kwargs: (bar,),
    )
    monkeypatch.setattr(
        data_source,
        "load_security_statuses",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        data_source,
        "load_sector_memberships",
        lambda **_kwargs: MembershipLoad((), False, (), ()),
    )

    loaded = load_point_in_time_dataset(
        BacktestDataConfig(
            start=BAR_OPEN.date(),
            end=BAR_OPEN.date(),
            codes=("SZ.000001",),
        )
    )

    evidence = audit_dataset(loaded)
    assert evidence.grade == "invalid"
    assert "security_status_coverage_missing" in evidence.failures


def test_data_source_does_not_read_webhook_or_notification_configuration() -> None:
    source = Path(data_source.__file__).read_text(encoding="utf-8").lower()

    assert "dingtalk" not in source
    assert "webhook" not in source
    assert "chanlun.notifications" not in source


class RecordingCL:
    def __init__(self) -> None:
        self.seen: list[datetime] = []

    def process_klines(self, frame: pd.DataFrame) -> None:
        self.seen.extend(pd.Timestamp(value).to_pydatetime() for value in frame["date"])


def test_causal_replay_is_incremental_and_rejects_cursor_rewind() -> None:
    second_at = BAR_AT + timedelta(minutes=1)
    frame = pd.DataFrame(
        {
            "date": [BAR_AT, second_at],
            "open": [10.0, 10.1],
            "high": [10.2, 10.3],
            "low": [9.9, 10.0],
            "close": [10.1, 10.2],
            "volume": [1000.0, 1100.0],
        }
    )
    states: list[RecordingCL] = []

    def cl_factory(_code: str, _frequency: str) -> RecordingCL:
        state = RecordingCL()
        states.append(state)
        return state

    replay = CausalStructureReplay(
        frames={
            ("SZ.000001", frequency): frame.copy()
            for frequency in ("1m", "5m", "30m")
        },
        cl_factory=cl_factory,
        bundle_factory=lambda **kwargs: SimpleNamespace(
            code=kwargs["code"],
            as_of=kwargs["closed_at"],
        ),
    )

    replay.bundle_at(
        dataset=dataset(),
        closed_at=BAR_AT,
        code="SZ.000001",
    )
    replay.bundle_at(
        dataset=dataset(),
        closed_at=second_at,
        code="SZ.000001",
    )

    assert len(states) == 3
    assert all(state.seen == [BAR_AT, second_at] for state in states)
    with pytest.raises(ValueError, match="cursor"):
        replay.bundle_at(
            dataset=dataset(),
            closed_at=BAR_AT,
            code="SZ.000001",
        )


@pytest.mark.parametrize(
    ("grade", "expected"),
    (("certified", 0), ("research_only", 2), ("invalid", 3)),
)
def test_cli_returns_evidence_exit_codes(monkeypatch, tmp_path, grade, expected) -> None:
    from tools import backtest_chanlun_trading_system as cli

    monkeypatch.setattr(cli, "MAX_NATIVE_SECTOR_PAGES", 10_000)
    evidence = SimpleNamespace(grade=grade, failures=(), warnings=(), coverage=())
    monkeypatch.setattr(cli, "load_point_in_time_dataset", lambda _config: dataset())
    monkeypatch.setattr(cli, "audit_dataset", lambda _dataset: evidence)
    monkeypatch.setattr(cli, "_run_walk_forward", lambda *_args, **_kwargs: "run")
    monkeypatch.setattr(
        cli,
        "_build_report",
        lambda **_kwargs: {"schema_version": "test"},
    )
    written: list[object] = []
    monkeypatch.setattr(
        cli,
        "write_report_atomic",
        lambda _path, report: written.append(report),
    )

    result = cli.main(
        (
            "--start",
            "2020-01-01",
            "--end",
            "2024-01-10",
            "--output",
            str(tmp_path / "result.json"),
        )
    )

    assert result == expected
    assert len(written) == 1


def test_cli_runtime_failure_has_dedicated_exit_code(monkeypatch, tmp_path) -> None:
    from tools import backtest_chanlun_trading_system as cli

    monkeypatch.setattr(cli, "MAX_NATIVE_SECTOR_PAGES", 10_000)
    monkeypatch.setattr(
        cli,
        "load_point_in_time_dataset",
        lambda _config: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    result = cli.main(
        (
            "--start",
            "2020-01-01",
            "--end",
            "2024-01-10",
            "--output",
            str(tmp_path / "result.json"),
        )
    )

    assert result == 4


def test_short_cli_writes_formal_hashed_report(monkeypatch, tmp_path) -> None:
    from tools import backtest_chanlun_trading_system as cli

    monkeypatch.setattr(
        cli,
        "load_point_in_time_dataset",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("short calendar span must not load the market")
        ),
    )
    output = tmp_path / "formal.json"

    result = cli.main(
        (
            "--start",
            "2026-01-01",
            "--end",
            "2026-01-31",
            "--output",
            str(output),
        )
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result == 2
    assert payload["schema_version"] == "chanlun-low-drawdown-backtest/v1"
    assert payload["aggregate_out_of_sample"]["net_return"] == "0"
    assert payload["walk_forward_windows"] == []
    assert payload["data_evidence"]["grade"] == "research_only"
    assert "insufficient_calendar_span_for_walk_forward" in payload[
        "data_evidence"
    ]["failures"]
    assert len(payload["ablations"]) == 6
    assert all(row["completed"] is False for row in payload["ablations"])
    assert len(payload["benchmarks"]) == 4
    assert all(row["data_grade"] == "invalid" for row in payload["benchmarks"])
    assert payload["verdict"]["live_ready"] is False
    assert verify_report_hash(payload) is True


def test_long_cli_skips_stock_loader_when_native_sector_capacity_is_insufficient(
    monkeypatch,
    tmp_path,
) -> None:
    from tools import backtest_chanlun_trading_system as cli

    monkeypatch.setattr(
        cli,
        "load_point_in_time_dataset",
        lambda _config: (_ for _ in ()).throw(
            AssertionError("stock universe must not load before sector evidence")
        ),
    )
    output = tmp_path / "long.json"

    result = cli.main(
        (
            "--start",
            "2018-01-01",
            "--end",
            "2026-07-17",
            "--output",
            str(output),
        )
    )
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert result == 2
    assert payload["data_evidence"]["grade"] == "research_only"
    assert "native_sector_history_capacity_insufficient" in payload[
        "data_evidence"
    ]["failures"]
    assert "stock_loading_skipped_by_sector_evidence_preflight" in payload[
        "data_evidence"
    ]["warnings"]
    assert verify_report_hash(payload) is True


def test_algorithm_hash_manifest_covers_every_strategy_python_source() -> None:
    from tools import backtest_chanlun_trading_system as cli

    package_root = (
        cli.PROJECT_ROOT / "src/chanlun/decision_support/trading_system"
    )
    expected = {
        path.relative_to(cli.PROJECT_ROOT).as_posix()
        for path in package_root.rglob("*.py")
    }
    expected.update(
        {
            "src/chanlun/core/bs_branch.py",
            "src/chanlun/core/bs2_branch.py",
            "src/chanlun/core/cl.py",
            "tools/backtest_chanlun_trading_system.py",
        }
    )

    manifest = dict(cli._algorithm_hashes())

    assert set(manifest) == expected
    for relative_path, digest in manifest.items():
        payload = (cli.PROJECT_ROOT / relative_path).read_bytes()
        assert digest == "sha256:" + hashlib.sha256(payload).hexdigest()
