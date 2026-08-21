from datetime import date, datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from tools import finalize_qmt_fixed_year as subject
from tools import finalize_qmt_pit_fixed_year as pit_subject
from tools import qmt_research_contract

from chanlun.decision_support.trading_system.selection import (
    SelectionResearchSnapshot,
    selection_research_ledger_document,
)


CN = ZoneInfo("Asia/Shanghai")


def test_legacy_formal_selection_research_ledger_loader_remains_valid(
    tmp_path,
) -> None:
    observed = datetime(2026, 7, 20, 10, 30, tzinfo=CN)
    snapshot = SelectionResearchSnapshot(
        snapshot_id="research:SZ.000001:20260720",
        symbol="SZ.000001",
        path="INDIVIDUAL_THREE_PROGRAM",
        effective_at=observed,
        known_at=observed,
        valid_until=observed + timedelta(days=30),
        reviewer="研究员",
        signature="signed:research:SZ.000001:20260720",
        official_evidence_ids=("official:SZ.000001:20260720",),
        industry_opportunity_status="PASS",
        fundamental_role="LEADER",
        relative_value_status="UNDERVALUED",
        point_in_time_total_market_cap=Decimal("100000000000"),
        peer_set_id="peer:bank:20260720",
    )
    path = tmp_path / "selection_research.json"
    path.write_text(
        json.dumps(
            selection_research_ledger_document((snapshot,)),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    snapshots, by_symbol = qmt_research_contract.load_selection_research_ledger(
        path,
        replay_symbols={"SZ.000001"},
    )

    assert snapshots == (snapshot,)
    assert by_symbol == {"SZ.000001": (snapshot,)}


def test_current_pit_finalizer_does_not_require_legacy_research_ledger() -> None:
    option_destinations = {action.dest for action in pit_subject.parser()._actions}

    assert "selection_research" not in option_destinations


def test_formal_selection_research_ledger_rejects_empty(
    tmp_path,
) -> None:
    path = tmp_path / "selection_research.json"
    path.write_text(
        json.dumps({"schema": "chanlun-selection-research-ledger", "snapshots": []}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="不能为空"):
        qmt_research_contract.load_selection_research_ledger(
            path,
            replay_symbols={"SZ.000001"},
        )


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
                "SIX_CONTIGUOUS_COMPLETED_5M_COMPOSITE_BARS"
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
