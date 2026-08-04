from __future__ import annotations

from datetime import date
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools" / "fetch_financial_data_query_bars.py"
SPEC = importlib.util.spec_from_file_location("fetch_financial_data_query_bars", TOOL)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_natural_day_windows_are_complete_non_overlapping_and_bounded() -> None:
    windows = MODULE.natural_day_windows(
        symbol="510300.SH",
        period="P_Min1",
        split="S_Unsplit",
        start=date(2026, 1, 1),
        end=date(2026, 1, 6),
        maximum_days=2,
    )

    assert [(row.start_at.date(), row.end_at.date()) for row in windows] == [
        (date(2026, 1, 1), date(2026, 1, 2)),
        (date(2026, 1, 3), date(2026, 1, 4)),
        (date(2026, 1, 5), date(2026, 1, 6)),
    ]
    assert all(row.request()["url"] == MODULE.KLINE_URL for row in windows)


def test_natural_day_windows_reject_invalid_boundaries() -> None:
    try:
        MODULE.natural_day_windows(
            symbol="510300.SH",
            period="P_Min1",
            split="S_Unsplit",
            start=date(2026, 1, 2),
            end=date(2026, 1, 1),
            maximum_days=2,
        )
    except ValueError as exc:
        assert str(exc) == "start cannot follow end"
    else:
        raise AssertionError("invalid date boundaries must fail")


@pytest.mark.parametrize("encoding", ["utf-8", "gb18030"])
def test_query_batch_accepts_supported_windows_output_encodings(
    monkeypatch: pytest.MonkeyPatch,
    encoding: str,
) -> None:
    response = {
        "status": "SUCCESS",
        "results": [
            {
                "url": MODULE.KLINE_URL,
                "meta": {"fields": []},
                "data": [],
                "issues": [{"code": "EMPTY", "hint": "无可用行情"}],
            }
        ],
    }

    class Result:
        returncode = 0
        stdout = json.dumps(response, ensure_ascii=False).encode(encoding)

    monkeypatch.setenv("FINANCIAL_DATA_API_KEY", "test-only")
    monkeypatch.setattr(MODULE.subprocess, "run", lambda *args, **kwargs: Result())
    window = MODULE.natural_day_windows(
        symbol="510300.SH",
        period="P_Min1",
        split="S_Unsplit",
        start=date(2026, 1, 1),
        end=date(2026, 1, 2),
        maximum_days=2,
    )[0]

    assert MODULE._query_batch(Path("query.py"), (window,)) == response
