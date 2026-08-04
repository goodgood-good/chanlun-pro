from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd

from tools import finalize_qmt_fixed_year as subject


CN = ZoneInfo("Asia/Shanghai")


def test_fixed_year_sector_facts_use_five_minute_first_composite(
    monkeypatch,
    tmp_path,
) -> None:
    observed = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    calls: list[dict[str, object]] = []
    derives: list[tuple[pd.DataFrame, int | None]] = []

    class Source:
        def frame(self, **kwargs):
            calls.append(dict(kwargs))
            return pd.DataFrame(
                {
                    "date": [observed],
                    "open": [1.0],
                    "high": [1.1],
                    "low": [0.9],
                    "close": [1.05],
                    "volume": [8.0],
                }
            )

    def derive(frame, *, request_bars=None):
        derives.append((frame, request_bars))
        result = frame.copy(deep=True)
        result.attrs = {
            "source_base_frequency": "5m",
            "derived_frequency": "30m",
            "sector_thirty_minute_derivation_contract": (
                "SIX_CONTIGUOUS_COMPLETED_5M_COMPOSITE_BARS_V1"
            ),
        }
        return result

    sentinel = SimpleNamespace(error=None, assessments=(), row_count=1)
    monkeypatch.setattr(subject, "QmtSectorCompositeSource", Source)
    monkeypatch.setattr(
        subject,
        "derive_qmt_sector_thirty_minute_frame",
        derive,
    )
    monkeypatch.setattr(
        subject,
        "sector_facts_from_frame",
        lambda **_kwargs: sentinel,
    )

    result = subject._sector_facts(
        directory=tmp_path,
        symbols=(
            SimpleNamespace(
                sector_id="qmt-gics3:test",
                evaluations=(SimpleNamespace(observed_at=observed),),
            ),
        ),
        catalog={
            "qmt-gics3:test": {
                "name": "测试行业",
                "members": tuple(f"SH.6000{index:02d}" for index in range(8)),
            }
        },
        requested_end=date(2026, 7, 20),
        force=True,
        algorithm_revision="sha256:" + "1" * 64,
    )

    assert result == {"qmt-gics3:test": sentinel}
    assert len(calls) == 1
    assert calls[0]["frequency"] == "5m"
    assert calls[0]["request_bars"] == 4000 * 6 + 47
    assert len(derives) == 1
    assert derives[0][0] is not None
    assert derives[0][1] == 4000
