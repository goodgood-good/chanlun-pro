from __future__ import annotations

from datetime import date
import json
from types import SimpleNamespace

import pandas as pd

from tools import supplement_qmt_history as subject


def test_request_range_uses_a_share_session_boundaries() -> None:
    day = date(2026, 7, 24)

    assert subject._request_timestamp(day, closing=False) == "20260724093000"
    assert subject._request_timestamp(day, closing=True) == "20260724150000"


def test_failed_resume_record_is_retried(monkeypatch, tmp_path) -> None:
    output = tmp_path / "manifest.json"
    request = {
        "start": "2025-07-24",
        "end": "2026-07-24",
        "period": "1m",
        "codes": ["000001.SZ"],
    }
    output.write_text(
        json.dumps(
            {
                "schema": subject.SCHEMA,
                "request": request,
                "records": {
                    "000001.SZ": {
                        "rows": 0,
                        "earliest": None,
                        "latest": None,
                        "error": "RuntimeError:temporary",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class FakeXtdata:
        enable_hello = True
        downloads: list[tuple[str, ...]] = []

        @classmethod
        def download_history_data2(cls, codes, *_args, **_kwargs) -> None:
            cls.downloads.append(tuple(codes))

        @staticmethod
        def get_market_data(**_kwargs):
            return {
                "time": pd.DataFrame(
                    [[1753320600000]],
                    index=["000001.SZ"],
                )
            }

    monkeypatch.setitem(
        __import__("sys").modules,
        "xtquant",
        SimpleNamespace(xtdata=FakeXtdata),
    )

    result = subject.main(
        (
            "--start",
            "2025-07-24",
            "--end",
            "2026-07-24",
            "--codes",
            "000001.SZ",
            "--output",
            str(output),
        )
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert result == 0
    assert FakeXtdata.downloads == [("000001.SZ",)]
    assert payload["complete"] is True
    assert payload["records"]["000001.SZ"]["rows"] == 1
    assert "error" not in payload["records"]["000001.SZ"]

