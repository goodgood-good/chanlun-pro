from __future__ import annotations

from chanlun.decision_support.trading_system.models import (
    RankedSector,
    SectorAssessment,
    TimeframeContext,
)


def assess_sector(
    *,
    sector_id: str,
    sector_name: str,
    market_data_source: str,
    thirty: TimeframeContext,
    five: TimeframeContext,
    one: TimeframeContext,
    data_complete: bool,
) -> SectorAssessment:
    if market_data_source not in {
        "qmt_gics3_component_composite",
        # 页面、前向模拟和当前成分研究回放共用的规范 QMT 收益率中位数组合。
        "qmt-gics3-composite",
        "qmt-sw1-pit-composite",
    }:
        return SectorAssessment(
            sector_id,
            sector_name,
            False,
            True,
            "hostile",
            (),
            ("non_native_sector_kline",),
            thirty,
            five,
            one,
        )
    if not data_complete:
        return SectorAssessment(
            sector_id,
            sector_name,
            False,
            True,
            "hostile",
            (),
            ("sector_data_incomplete",),
            thirty,
            five,
            one,
        )
    hard_block = thirty.hard_block
    components = (
        ("thirty_support", 40 if thirty.disposition == "supportive" else 0),
        ("five_support", 30 if five.disposition == "supportive" else 0),
        ("one_support", 10 if one.disposition == "supportive" else 0),
        ("neutral_access", 5 if not hard_block else 0),
    )
    has_structural_support = any(
        value > 0 for name, value in components if name != "neutral_access"
    )
    regime = (
        "hostile"
        if hard_block
        else "supportive"
        if has_structural_support
        else "neutral"
    )
    return SectorAssessment(
        sector_id=sector_id,
        sector_name=sector_name,
        eligible=not hard_block,
        hard_block=hard_block,
        regime=regime,
        rank_components=components,
        reason_codes=(
            ("higher_structure_sell_risk",)
            if hard_block
            else ("structural_ranking_only",)
        ),
        thirty_context=thirty,
        five_context=five,
        one_context=one,
    )


def rank_sectors(
    assessments: tuple[SectorAssessment, ...],
) -> tuple[RankedSector, ...]:
    eligible = sorted(
        (assessment for assessment in assessments if assessment.eligible),
        key=lambda assessment: (
            assessment.horizontal_strength is None,
            -(
                assessment.horizontal_strength
                if assessment.horizontal_strength is not None
                else 0
            ),
            -assessment.rank_score,
            assessment.sector_id,
        ),
    )
    return tuple(
        RankedSector(ordinal=index, assessment=assessment)
        for index, assessment in enumerate(eligible, start=1)
    )
