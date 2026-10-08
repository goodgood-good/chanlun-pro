"""Worker progress distinguishes partial-period success from symbol errors."""

import json
import os

import pytest

from chanlun.screening import runner
from cl_app.services.screening import all_periods_failed, validate_settings


@pytest.mark.parametrize(
    "case, successful, errors",
    [
        ("partial", 2, 2),
        ("all_failed", 0, 2),
        ("symbol_failed", 0, 2),
        ("no_candidates", 4, 0),
    ],
)
def test_committed_worker_progress_preserves_usable_empty_periods(
    tmp_path, monkeypatch, case, successful, errors
):
    settings = validate_settings({"scope": "watchlist", "frequencies": ["1m", "5m"]})
    runner.write_json(
        tmp_path / "request.json",
        {"settings": settings, "observed_at": "2026-09-17T23:14:00+08:00"},
    )
    stocks = [
        {"market": "us", "code": code, "name": code} for code in ("TSLA.US", "AMD.US")
    ]
    monkeypatch.setattr(
        runner,
        "bounded_catalog",
        lambda *_: iter([{"stocks": stocks, "excluded_requested_codes": []}]),
    )
    records = []
    for stock in stocks:
        rows = []
        for frequency in settings["frequencies"]:
            failed = case == "all_failed" or (case == "partial" and frequency == "1m")
            rows.append(
                {
                    **stock,
                    "frequency": frequency,
                    "source_closed_at": None if failed else 1000,
                    "selected": [],
                    "data_errors": ["DATA_GAPS"] if failed else [],
                }
            )
        records.append(
            {
                **stock,
                "rows": rows,
                **({"error": "worker failed"} if case == "symbol_failed" else {}),
            }
        )
    monkeypatch.setattr(
        runner, "bounded_scan", lambda *_: iter((record, []) for record in records)
    )
    runner.run_screening(tmp_path)
    state = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert state["status"] == "completed"
    if os.name == 'nt':
        from chanlun.screening.worker_lifetime import process_identity
        assert state['worker_process_birth'] == process_identity(state['worker_pid'])['birth']
    assert state["completed"] == state["total"] == 2
    assert state["selected_count"] == 0
    assert state["error_count"] == errors
    assert state["successful_period_count"] == successful
    assert all_periods_failed(state) is (successful == 0)
