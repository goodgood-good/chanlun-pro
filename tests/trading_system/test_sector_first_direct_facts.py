from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

from chanlun.decision_support.trading_system.backtest.fixed_year import (
    CausalDirectRecursiveDecisionFact,
    CausalStructureEventLedger,
)
from chanlun.decision_support.trading_system.backtest.pit_metadata import (
    SecurityMasterRecord,
    SectorMembershipChange,
)
from chanlun.decision_support.trading_system.sector_first_direct_facts import (
    build_sector_first_direct_symbol_facts,
)
from chanlun.decision_support.trading_system.sector_first_trigger_plan import (
    SectorFirstTriggerEvent,
    SectorFirstTriggerLedger,
    SectorTriggerRankFact,
)


CN = ZoneInfo("Asia/Shanghai")
AT = datetime(2026, 7, 20, 10, 0, tzinfo=CN)


def _trigger_ledger() -> SectorFirstTriggerLedger:
    return SectorFirstTriggerLedger(
        algorithm_revision="sha256:" + "a" * 64,
        sector_scope_sha256="sha256:" + "b" * 64,
        pit_snapshot_sha256="sha256:" + "c" * 64,
        events=(
            SectorFirstTriggerEvent(
                observed_at=AT,
                ranked_sectors=(
                    SectorTriggerRankFact(
                        sector_id="qmt-sw1:S11",
                        sector_name="one",
                        ordinal=1,
                        rank_score=45,
                        regime="supportive",
                        rank_components=(("neutral_access", 5), ("thirty_support", 40)),
                        reason_codes=("structural_ranking_only",),
                    ),
                ),
                hard_blocked_sector_ids=(),
                missing_sector_ids=(),
                candidate_symbol_count=1,
                candidate_count_by_sector=(("qmt-sw1:S11", 1),),
                candidate_symbols_sha256="sha256:" + "d" * 64,
            ),
        ),
        sector_source_revisions=(("qmt-sw1:S11", "sha256:" + "e" * 64),),
    )


def test_direct_facts_expand_only_a_triggered_sector_and_keep_three_program_closed(
    monkeypatch,
) -> None:
    frame = pd.DataFrame(
        {
            "code": ["SH.600001"],
            "date": [AT],
            "open": [10.0],
            "high": [10.1],
            "low": [9.9],
            "close": [10.0],
            "volume": [1000.0],
            "raw_open": [10.0],
            "raw_high": [10.1],
            "raw_low": [9.9],
            "raw_close": [10.0],
        }
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-raw",
    )
    monkeypatch.setattr(
        "chanlun.decision_support.trading_system."
        "sector_first_direct_facts.load_qmt_frame",
        lambda *_args, **_kwargs: frame,
    )
    decision = CausalDirectRecursiveDecisionFact(
        l0_point_id="point:l0",
        first_seen_at=AT,
        status="REJECT",
        reason_codes=("NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN",),
        structure_snapshot_id="sha256:" + "f" * 64,
        technical_entry=None,
    )
    seen_windows = []

    def causal(*_args, **kwargs):
        seen_windows.append(kwargs["visibility_windows"])
        return CausalStructureEventLedger(
            points=(),
            completed_trends=(),
            direct_recursive_decisions=(decision,),
        )

    monkeypatch.setattr(
        "chanlun.decision_support.trading_system."
        "sector_first_direct_facts.final_confirmed_structure_events",
        causal,
    )
    master = SecurityMasterRecord("SH.600001", "A", date(2020, 1, 1), None)
    membership = SectorMembershipChange(
        code="SH.600001",
        sector_id="qmt-sw1:S11",
        sector_name="one",
        industry_code="S11",
        source_changed_on=date(2020, 1, 1),
        known_at=datetime(2020, 1, 2, tzinfo=CN),
    )

    result = build_sector_first_direct_symbol_facts(
        code="SH.600001",
        warmup_start=date(2026, 7, 1),
        requested_start=date(2026, 7, 1),
        requested_end=date(2026, 7, 20),
        effective_start=date(2026, 7, 20),
        algorithm_revision="sha256:" + "a" * 64,
        trigger_ledger=_trigger_ledger(),
        trigger_ledger_sha256="sha256:" + "1" * 64,
        security_master=master,
        memberships=(membership,),
        qmt_factors=(),
    )

    assert seen_windows == [((AT, AT),)]
    assert result.direct_decisions == (decision,)
    assert result.technical_entry_count == 0
    assert result.full_system_entry_count == 0
    assert result.three_program_status == "UNRESOLVED"
    assert result.live_status == "LIVE_DISABLED"
