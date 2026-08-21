"""QMT adapters for strict strategy M/W/D risk with the explicit D -> 30m mapping.

Daily and 30-minute frames may both originate from QMT yet still be separate
vendor series.  The adapter records that distinction: stock/index evidence
requires one shared, hash-identified 1m base; a component-derived sector proxy
may explicitly require one shared 5m composite base.  Both paths reject two
independently supplied vendor series even when their provider labels match.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, Mapping, Sequence

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_WARMUP_REQUIRED_BARS,
    expected_screening_warmup_suffix_bar_count,
)
from chanlun.decision_support.trading_system.warmup_convergence import (
    WarmupConvergenceEnvelope,
    WarmupMappingSupplySnapshot,
    WarmupPeriodSemanticFacts,
    WarmupPrefixObservation,
    WarmupSemanticSnapshot,
    bind_warmup_convergence_diagnostic,
    bind_warmup_mapping_supply_diagnostic,
    classify_warmup_convergence_envelope,
)
from chanlun.decision_support.trading_system.warmup_structure_lineage import (
    WarmupStructureLineageSnapshot,
    WarmupStructureLineageSnapshotSet,
    bind_warmup_structure_lineage_diagnostic,
)
from chanlun.decision_support.trading_system.qmt_native_daily_bridge import (
    QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID,
    QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
    QmtNativeDailyCalendarCoverageEvidence,
    QmtNativeDailyReconciliationEvidence,
)
from chanlun.decision_support.trading_system.etf_proxy_facts import (
    BenchmarkStructureRiskFacts,
    DailyMarketBar,
    FactBlocker,
    FrozenStructureBar,
    HigherTimeframeRiskFacts,
    build_benchmark_structure_risk_facts,
    build_higher_timeframe_risk_facts,
)


QmtRiskGrade = Literal["FULL_SYSTEM_ELIGIBLE", "RESEARCH_ONLY", "UNRESOLVED"]
_REQUIRED = ("date", "open", "high", "low", "close", "volume")
QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS = (
    SCREENING_WARMUP_REQUIRED_BARS["d"]
)
# 收敛闸门会删除最老三分之一后再计算一次。完整输入至少需要 720 根，才能让
# 被比较的短前缀仍保有冻结的 480 根策略最低历史；这只扩大物理证据，不改变阈值。
QMT_HIGHER_TIMEFRAME_WARMUP_PHYSICAL_DAILY_BARS = (
    QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS * 3 // 2
)
QMT_SECTOR_NATIVE_DAILY_RESEARCH_BASE_FREQUENCY = (
    "5m+native-d-unreconciled-research"
)
QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID = (
    "chanlun-qmt-mwd-warmup-evidence"
)
QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PREFIX_RATIOS = (
    (1, 2),
    (2, 3),
    (5, 6),
    (1, 1),
)
QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID = sha256_json(
    {
        "contract": "chanlun-qmt-mwd-multi-prefix-convergence",
        "frequency": "d",
        "prefix_ratios": QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PREFIX_RATIOS,
        "minimum_prefix_bars": QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS,
        "minimum_full_prefix_bars": (
            QMT_HIGHER_TIMEFRAME_WARMUP_PHYSICAL_DAILY_BARS
        ),
        "semantic_signature": "chanlun-qmt-mwd-warmup-semantic-tail",
        "diagnostic_only": True,
        "active_pairwise_gate_unchanged": True,
    }
)


@dataclass(frozen=True, slots=True)
class QmtHigherTimeframeWarmupEvidence:
    """Fail-closed M/W/D prefix-convergence evidence.

    The physical-frequency scanner already freezes a 480-daily-bar warmup
    budget.  Reuse that same budget here instead of inventing a second
    strategy parameter: the M/W/D state machine is evaluated once on the
    complete available prefix and once after dropping its oldest third.  A
    favourable ``NONE`` state is usable only when both semantic tails agree.
    """

    required_daily_bar_count: int
    full_daily_bar_count: int
    suffix_daily_bar_count: int
    converged: bool
    reason_code: Literal[
        "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT",
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED",
        "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE",
    ]
    full_signature: str
    suffix_signature: str | None

    def __post_init__(self) -> None:
        if self.required_daily_bar_count != (
            QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
        ):
            raise ValueError("higher-timeframe warmup budget is immutable")
        if self.full_daily_bar_count < 0 or self.suffix_daily_bar_count < 0:
            raise ValueError("higher-timeframe warmup counts cannot be negative")
        if not self.full_signature.startswith("sha256:"):
            raise ValueError("higher-timeframe full signature is required")
        if self.reason_code.endswith("HISTORY_INSUFFICIENT"):
            if (
                self.converged
                or self.full_daily_bar_count >= self.required_daily_bar_count
                or self.suffix_daily_bar_count != 0
                or self.suffix_signature is not None
            ):
                raise ValueError("inconsistent insufficient warmup evidence")
            return
        expected = expected_screening_warmup_suffix_bar_count(
            self.full_daily_bar_count
        )
        if (
            self.full_daily_bar_count < self.required_daily_bar_count
            or self.suffix_daily_bar_count != expected
            or self.suffix_signature is None
            or not self.suffix_signature.startswith("sha256:")
        ):
            raise ValueError("inconsistent pairwise warmup evidence")
        if self.converged != self.reason_code.endswith("TAIL_STABLE"):
            raise ValueError("warmup verdict contradicts its reason code")

    def document(self) -> dict[str, object]:
        return {
            "contract_id": QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID,
            "required_daily_bar_count": self.required_daily_bar_count,
            "full_daily_bar_count": self.full_daily_bar_count,
            "suffix_daily_bar_count": self.suffix_daily_bar_count,
            "converged": self.converged,
            "reason_code": self.reason_code,
            "full_signature": self.full_signature,
            "suffix_signature": self.suffix_signature,
            "entry_disposition": (
                "NO_WARMUP_BLOCKER" if self.converged else "FAIL_CLOSED"
            ),
        }


@dataclass(frozen=True, slots=True)
class QmtHigherTimeframeInputs:
    symbol: str
    observed_at: datetime
    daily_bars: tuple[DailyMarketBar, ...]
    completed_30m_bars: tuple[FrozenStructureBar, ...]
    price_basis_revision: str | None
    source_base_stream_revision: str | None
    source_revision: str
    blockers: tuple[FactBlocker, ...]
    source_base_frequency: str | None = None
    native_daily_reconciliation_evidence: (
        QmtNativeDailyReconciliationEvidence | None
    ) = None
    native_daily_calendar_coverage_evidence: (
        QmtNativeDailyCalendarCoverageEvidence | None
    ) = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "observed_at",
            normalize_datetime(self.observed_at, "observed_at"),
        )
        if not self.symbol or not self.source_revision.startswith("sha256:"):
            raise ValueError("QMT higher-timeframe source identity is required")
        evidence = self.native_daily_reconciliation_evidence
        if self.source_base_frequency == QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY:
            if (
                evidence is None
                or evidence.symbol != self.symbol
                or evidence.observed_at != self.observed_at
                or evidence.reconciled_source_revision
                != self.source_base_stream_revision
                or evidence.price_basis_revision != self.price_basis_revision
            ):
                raise ValueError(
                    "reconciled native-daily source requires matching evidence"
                )
        elif evidence is not None:
            raise ValueError(
                "native-daily reconciliation evidence requires its source frequency"
            )
        coverage = self.native_daily_calendar_coverage_evidence
        if coverage is not None and (
            self.source_base_frequency != QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY
            or coverage.symbol != self.symbol
            or coverage.observed_at != self.observed_at
            or coverage.status != "EXACT"
            or evidence is None
            or coverage.trading_calendar_revision
            != evidence.trading_calendar_revision
        ):
            raise ValueError("native-daily calendar coverage evidence is inconsistent")

    @property
    def same_base_stream(self) -> bool:
        return (
            self.source_base_stream_revision is not None
            and self.source_base_frequency
            in {"1m", "5m", QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY}
            and not any("NOT_FROM_SAME" in value.code for value in self.blockers)
        )


@dataclass(frozen=True, slots=True)
class QmtHigherTimeframeRiskEnvelope:
    inputs: QmtHigherTimeframeInputs
    structure: BenchmarkStructureRiskFacts
    risk: HigherTimeframeRiskFacts
    warmup: QmtHigherTimeframeWarmupEvidence
    grade: QmtRiskGrade
    blockers: tuple[FactBlocker, ...]
    warmup_convergence: WarmupConvergenceEnvelope | None = None

    @property
    def full_system_eligible(self) -> bool:
        return self.grade == "FULL_SYSTEM_ELIGIBLE" and self.risk.snapshot is not None


def _visible_frame(
    frame: pd.DataFrame,
    *,
    decision_time: datetime,
    label: str,
) -> pd.DataFrame:
    missing = set(_REQUIRED).difference(frame.columns)
    if missing:
        raise ValueError(f"{label} frame is missing columns: {sorted(missing)!r}")
    work = frame.loc[:, list(_REQUIRED)].copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    if work["date"].dt.tz is None:
        raise ValueError(f"{label} completion times must be timezone-aware")
    work["date"] = work["date"].dt.tz_convert("Asia/Shanghai")
    work = work[work["date"] <= pd.Timestamp(decision_time)].copy()
    if work["date"].duplicated().any() or not work["date"].is_monotonic_increasing:
        raise ValueError(f"{label} completion times must be unique and chronological")
    for field in ("open", "high", "low", "close", "volume"):
        work[field] = pd.to_numeric(work[field], errors="raise")
    if not work.empty:
        prices = work[["open", "high", "low", "close"]]
        invalid = (
            (prices <= 0).any(axis=1)
            | (work["volume"] < 0)
            | (work["high"] < prices.max(axis=1))
            | (work["low"] > prices.min(axis=1))
        )
        if invalid.any():
            raise ValueError(f"{label} contains invalid OHLCV")
    work.attrs = dict(frame.attrs)
    return work


def qmt_higher_timeframe_inputs(
    *,
    symbol: str,
    daily_frame: pd.DataFrame,
    thirty_minute_frame: pd.DataFrame,
    decision_time: datetime,
    required_base_frequency: Literal[
        "1m",
        "5m",
        "1m+native-d",
        "5m+native-d-unreconciled-research",
    ] = "1m",
    native_daily_reconciliation_evidence: (
        QmtNativeDailyReconciliationEvidence | None
    ) = None,
    native_daily_calendar_coverage_evidence: (
        QmtNativeDailyCalendarCoverageEvidence | None
    ) = None,
) -> QmtHigherTimeframeInputs:
    """Freeze completed rows and audit their declared shared base stream."""

    decision = normalize_datetime(decision_time, "decision_time")
    if required_base_frequency not in {
        "1m",
        "5m",
        QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY,
        QMT_SECTOR_NATIVE_DAILY_RESEARCH_BASE_FREQUENCY,
    }:
        raise ValueError(
            "required_base_frequency must be 1m, 5m, 1m+native-d or the "
            "explicit unreconciled sector research bridge"
        )
    daily = _visible_frame(daily_frame, decision_time=decision, label="QMT daily")
    thirty = _visible_frame(
        thirty_minute_frame,
        decision_time=decision,
        label="QMT 30m",
    )
    blockers: list[FactBlocker] = []
    for timestamp in daily["date"]:
        if timestamp.time() != time(15, 0):
            blockers.append(
                FactBlocker(
                    "daily_completion_time",
                    "QMT_DAILY_BAR_COMPLETION_TIME_UNRESOLVED",
                    timestamp.isoformat(),
                )
            )
            break
    daily_provider = daily_frame.attrs.get("price_basis_provider")
    thirty_provider = thirty_minute_frame.attrs.get("price_basis_provider")
    daily_adjustment = daily_frame.attrs.get("price_basis_adjustment")
    thirty_adjustment = thirty_minute_frame.attrs.get("price_basis_adjustment")
    daily_basis = daily_frame.attrs.get("price_basis_revision")
    thirty_basis = thirty_minute_frame.attrs.get("price_basis_revision")
    price_basis_revision = (
        str(daily_basis)
        if daily_basis
        and daily_basis == thirty_basis
        and daily_provider == thirty_provider
        and daily_adjustment == thirty_adjustment
        else None
    )
    if price_basis_revision is None:
        blockers.append(
            FactBlocker(
                "price_basis_revision",
                "QMT_DAILY_AND_30M_PRICE_BASIS_MISMATCH",
                (
                    f"daily={daily_provider}/{daily_adjustment}/{daily_basis}; "
                    f"30m={thirty_provider}/{thirty_adjustment}/{thirty_basis}"
                ),
            )
        )
    daily_base = daily_frame.attrs.get("source_base_stream_revision")
    thirty_base = thirty_minute_frame.attrs.get("source_base_stream_revision")
    daily_base_frequency = daily_frame.attrs.get("source_base_frequency")
    thirty_base_frequency = thirty_minute_frame.attrs.get(
        "source_base_frequency"
    )
    native_evidence_valid = True
    if required_base_frequency == QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY:
        evidence = native_daily_reconciliation_evidence
        lineage_fields = {
            "qmt_native_daily_reconciliation_contract_id": (
                QMT_NATIVE_DAILY_RECONCILIATION_CONTRACT_ID
            ),
            "qmt_native_daily_content_revision": (
                None if evidence is None else evidence.native_daily_content_revision
            ),
            "qmt_intraday_one_minute_base_revision": (
                None if evidence is None else evidence.one_minute_base_revision
            ),
            "qmt_native_daily_trading_calendar_revision": (
                None if evidence is None else evidence.trading_calendar_revision
            ),
            "qmt_native_daily_price_tolerance_quanta": (
                None if evidence is None else evidence.price_tolerance_quanta
            ),
            "qmt_native_daily_price_difference_count": (
                None
                if evidence is None
                else len(evidence.price_difference_identities)
            ),
        }
        native_evidence_valid = (
            evidence is not None
            and evidence.symbol == symbol
            and evidence.observed_at == decision
            and evidence.reconciled_source_revision == daily_base
            and evidence.reconciled_source_revision == thirty_base
            and evidence.price_basis_revision == price_basis_revision
            and all(
                daily_frame.attrs.get(field) == expected
                and thirty_minute_frame.attrs.get(field) == expected
                for field, expected in lineage_fields.items()
            )
        )
    source_base_stream_revision = (
        str(daily_base)
        if daily_base
        and daily_base == thirty_base
        and str(daily_base).startswith("sha256:")
        and daily_base_frequency == required_base_frequency
        and thirty_base_frequency == required_base_frequency
        and native_evidence_valid
        else None
    )
    source_base_frequency = (
        required_base_frequency
        if source_base_stream_revision is not None
        else None
    )
    if source_base_stream_revision is None:
        blocker_code = {
            "1m": "QMT_DAILY_AND_30M_NOT_FROM_SAME_1M_BASE",
            "5m": "QMT_SECTOR_DAILY_AND_30M_NOT_FROM_SAME_5M_BASE",
            QMT_NATIVE_DAILY_RECONCILED_BASE_FREQUENCY: (
                "QMT_NATIVE_DAILY_RECONCILIATION_EVIDENCE_UNRESOLVED"
            ),
            QMT_SECTOR_NATIVE_DAILY_RESEARCH_BASE_FREQUENCY: (
                "QMT_SECTOR_NATIVE_DAILY_AND_5M_UNRECONCILED_RESEARCH_BRIDGE"
            ),
        }[required_base_frequency]
        blockers.append(
            FactBlocker(
                "source_base_stream_revision",
                blocker_code,
                (
                    "same provider is insufficient; a shared "
                    f"{required_base_frequency} derivation hash is required"
                ),
            )
        )
    daily_bars = tuple(
        DailyMarketBar(
            session=pd.Timestamp(row.date).date(),
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=Decimal(str(row.volume)),
            known_at=pd.Timestamp(row.date).to_pydatetime(),
        )
        for row in daily.itertuples(index=False)
    )
    thirty_bars = tuple(
        FrozenStructureBar(
            end_at=pd.Timestamp(row.date).to_pydatetime(),
            open=Decimal(str(row.open)),
            high=Decimal(str(row.high)),
            low=Decimal(str(row.low)),
            close=Decimal(str(row.close)),
            volume=Decimal(str(row.volume)),
        )
        for row in thirty.itertuples(index=False)
    )
    source_revision = sha256_json(
        {
            "schema": "chanlun-qmt-higher-timeframe-input",
            "symbol": symbol,
            "decision_time": decision,
            "daily": tuple(
                {
                    "session": value.session.isoformat(),
                    "open": value.open,
                    "high": value.high,
                    "low": value.low,
                    "close": value.close,
                    "volume": value.volume,
                    "known_at": value.known_at,
                    "completed": value.completed,
                }
                for value in daily_bars
            ),
            "thirty": thirty_bars,
            "price_basis_revision": price_basis_revision,
            "source_base_stream_revision": source_base_stream_revision,
            "source_base_frequency": source_base_frequency,
            "native_daily_reconciliation_evidence": (
                None
                if native_daily_reconciliation_evidence is None
                else native_daily_reconciliation_evidence.document()
            ),
            "native_daily_calendar_coverage_evidence": (
                None
                if native_daily_calendar_coverage_evidence is None
                else native_daily_calendar_coverage_evidence.document()
            ),
        }
    )
    return QmtHigherTimeframeInputs(
        symbol=symbol,
        observed_at=decision,
        daily_bars=daily_bars,
        completed_30m_bars=thirty_bars,
        price_basis_revision=price_basis_revision,
        source_base_stream_revision=source_base_stream_revision,
        source_revision=source_revision,
        blockers=tuple(blockers),
        source_base_frequency=source_base_frequency,
        native_daily_reconciliation_evidence=(
            native_daily_reconciliation_evidence
            if source_base_stream_revision is not None
            else None
        ),
        native_daily_calendar_coverage_evidence=(
            native_daily_calendar_coverage_evidence
            if source_base_stream_revision is not None
            else None
        ),
    )


def _mwd_warmup_semantic_signature(
    candidate_structure: BenchmarkStructureRiskFacts,
    candidate_risk: HigherTimeframeRiskFacts,
) -> str:
    return _mwd_warmup_semantic_snapshot(
        candidate_structure,
        candidate_risk,
    ).signature_sha256


def _mwd_warmup_semantic_snapshot(
    candidate_structure: BenchmarkStructureRiskFacts,
    candidate_risk: HigherTimeframeRiskFacts,
) -> WarmupSemanticSnapshot:
    """Retain the exact facts already hashed by the frozen signature."""

    return WarmupSemanticSnapshot(
        periods=tuple(
            WarmupPeriodSemanticFacts(
                period=value.fact.period,
                state=value.fact.state,
                evidence_bar_end=value.fact.evidence_bar_end,
                active_top_interval=value.active_top_interval,
                mapping_unique=value.fact.mapping_unique,
            # 中枢标识同时绑定几何与事件身份；即使粗粒度颜色不变，标识不匹配
            # 仍然属于结构分歧。
                mapped_center_id=value.mapped_center_id,
                mapping_candidate_ids=value.mapping_candidate_ids,
                blocker_codes=tuple(
                    blocker.code for blocker in value.blockers
                ),
                warning_codes=tuple(
                    warning.code for warning in value.warnings
                ),
            )
            for value in candidate_structure.states
        ),
        ma5=tuple(
            (str(period), value) for period, value in candidate_risk.ma5
        ),
    )


def _build_mwd_warmup_convergence(
    *,
    inputs: QmtHigherTimeframeInputs,
    trading_sessions: Sequence[date],
    calendar_coverage_end: date,
    snapshot_id: str,
    full_structure: BenchmarkStructureRiskFacts,
    full_risk: HigherTimeframeRiskFacts,
    full_structure_lineage: Mapping[str, WarmupStructureLineageSnapshot],
) -> WarmupConvergenceEnvelope:
    """Measure several left-history lengths without changing the active gate."""

    full_count = len(inputs.daily_bars)
    required = QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
    bar_counts = tuple(
        sorted(
            {
                full_count * numerator // denominator
                for numerator, denominator in (
                    QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PREFIX_RATIOS
                )
                if full_count * numerator // denominator >= required
            }
        )
    )
    observations: list[WarmupPrefixObservation] = []
    semantic_snapshots: list[WarmupSemanticSnapshot] = []
    mapping_supply_snapshots: list[WarmupMappingSupplySnapshot] = []
    structure_lineage_snapshots: list[WarmupStructureLineageSnapshotSet] = []
    for bar_count in bar_counts:
        suffix_daily = inputs.daily_bars[-bar_count:]
        suffix_start = suffix_daily[0].session
        structure_lineage: dict[str, object]
        if bar_count == full_count:
            structure = full_structure
            risk = full_risk
            structure_lineage = dict(full_structure_lineage)
        else:
            suffix_thirty = tuple(
                value
                for value in inputs.completed_30m_bars
                if value.end_at.date() >= suffix_start
            )
            structure_lineage = {}
            structure = build_benchmark_structure_risk_facts(
                suffix_daily,
                trading_sessions=trading_sessions,
                calendar_coverage_end=calendar_coverage_end,
                decision_time=inputs.observed_at,
                completed_30m_bars=suffix_thirty,
                symbol=inputs.symbol,
                structure_lineage_sink=structure_lineage,
            )
            risk = build_higher_timeframe_risk_facts(
                suffix_daily,
                trading_sessions=trading_sessions,
                calendar_coverage_end=calendar_coverage_end,
                decision_time=inputs.observed_at,
                structure_states=tuple(value.fact for value in structure.states),
                snapshot_id=sha256_json(
                    {
                        "schema": "chanlun-qmt-mwd-warmup-prefix",
                        "full_snapshot_id": snapshot_id,
                        "prefix_daily_bar_count": bar_count,
                        "prefix_start": suffix_start.isoformat(),
                    }
                ),
            )
        semantic_snapshot = _mwd_warmup_semantic_snapshot(structure, risk)
        observations.append(
            WarmupPrefixObservation(
                bar_count=bar_count,
                starts_at=suffix_daily[0].known_at,
                signature_sha256=semantic_snapshot.signature_sha256,
            )
        )
        semantic_snapshots.append(semantic_snapshot)
        mapping_supply_snapshots.append(
            WarmupMappingSupplySnapshot(
                periods=tuple(
                    (value.fact.period, value.mapping_supply)
                    for value in structure.states
                )
            )
        )
        structure_lineage_snapshots.append(
            WarmupStructureLineageSnapshotSet(
                periods=tuple(
                    (
                        period,
                        (
                            value
                            if isinstance(value, WarmupStructureLineageSnapshot)
                            else None
                        ),
                    )
                    for period in ("M", "W", "D")
                    for value in (structure_lineage.get(period),)
                )
            )
        )
    envelope = classify_warmup_convergence_envelope(
        frequency="d",
        as_of=inputs.observed_at,
        parameter_set_id=(
            QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID
        ),
        observations=tuple(observations),
    )
    envelope = bind_warmup_convergence_diagnostic(
        envelope,
        snapshots=tuple(semantic_snapshots),
    )
    envelope = bind_warmup_mapping_supply_diagnostic(
        envelope,
        snapshots=tuple(mapping_supply_snapshots),
    )
    return bind_warmup_structure_lineage_diagnostic(
        envelope,
        snapshots=tuple(structure_lineage_snapshots),
    )


def build_qmt_higher_timeframe_risk(
    *,
    inputs: QmtHigherTimeframeInputs,
    trading_sessions: Sequence[date],
    calendar_coverage_end: date,
    snapshot_id: str,
) -> QmtHigherTimeframeRiskEnvelope:
    """Run the frozen M/W/D state machine for market, sector, or stock data."""

    full_structure_lineage: dict[str, object] = {}
    structure = build_benchmark_structure_risk_facts(
        inputs.daily_bars,
        trading_sessions=trading_sessions,
        calendar_coverage_end=calendar_coverage_end,
        decision_time=inputs.observed_at,
        completed_30m_bars=inputs.completed_30m_bars,
        symbol=inputs.symbol,
        structure_lineage_sink=full_structure_lineage,
    )
    risk = build_higher_timeframe_risk_facts(
        inputs.daily_bars,
        trading_sessions=trading_sessions,
        calendar_coverage_end=calendar_coverage_end,
        decision_time=inputs.observed_at,
        structure_states=tuple(value.fact for value in structure.states),
        snapshot_id=snapshot_id,
    )

    required = QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS
    full_count = len(inputs.daily_bars)
    full_signature = _mwd_warmup_semantic_signature(structure, risk)
    warmup_convergence = _build_mwd_warmup_convergence(
        inputs=inputs,
        trading_sessions=trading_sessions,
        calendar_coverage_end=calendar_coverage_end,
        snapshot_id=snapshot_id,
        full_structure=structure,
        full_risk=risk,
        full_structure_lineage={
            period: value
            for period, value in full_structure_lineage.items()
            if isinstance(value, WarmupStructureLineageSnapshot)
        },
    )
    warmup_blocker: FactBlocker | None = None
    if full_count < required:
        warmup = QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=required,
            full_daily_bar_count=full_count,
            suffix_daily_bar_count=0,
            converged=False,
            reason_code=(
                "QMT_HIGHER_TIMEFRAME_WARMUP_HISTORY_INSUFFICIENT"
            ),
            full_signature=full_signature,
            suffix_signature=None,
        )
        warmup_blocker = FactBlocker(
            field="higher_timeframe_warmup",
            code=warmup.reason_code,
            detail=f"completed_daily_bars={full_count}; required={required}",
        )
    else:
        trim = full_count // 3
        suffix_daily = inputs.daily_bars[trim:]
        suffix_start = suffix_daily[0].session
        suffix_thirty = tuple(
            value
            for value in inputs.completed_30m_bars
            if value.end_at.date() >= suffix_start
        )
        suffix_structure = build_benchmark_structure_risk_facts(
            suffix_daily,
            trading_sessions=trading_sessions,
            calendar_coverage_end=calendar_coverage_end,
            decision_time=inputs.observed_at,
            completed_30m_bars=suffix_thirty,
            symbol=inputs.symbol,
        )
        suffix_risk = build_higher_timeframe_risk_facts(
            suffix_daily,
            trading_sessions=trading_sessions,
            calendar_coverage_end=calendar_coverage_end,
            decision_time=inputs.observed_at,
            structure_states=tuple(
                value.fact for value in suffix_structure.states
            ),
            snapshot_id=sha256_json(
                {
                    "schema": "chanlun-qmt-mwd-warmup-suffix",
                    "full_snapshot_id": snapshot_id,
                    "suffix_start": suffix_start.isoformat(),
                }
            ),
        )
        suffix_signature = _mwd_warmup_semantic_signature(
            suffix_structure,
            suffix_risk,
        )
        converged = full_signature == suffix_signature
        warmup = QmtHigherTimeframeWarmupEvidence(
            required_daily_bar_count=required,
            full_daily_bar_count=full_count,
            suffix_daily_bar_count=len(suffix_daily),
            converged=converged,
            reason_code=(
                "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_STABLE"
                if converged
                else "QMT_HIGHER_TIMEFRAME_WARMUP_TAIL_DIVERGED"
            ),
            full_signature=full_signature,
            suffix_signature=suffix_signature,
        )
        if not converged:
            warmup_blocker = FactBlocker(
                field="higher_timeframe_warmup",
                code=warmup.reason_code,
                detail=(
                    f"full={full_signature}; suffix={suffix_signature}; "
                    f"full_daily_bars={full_count}; "
                    f"suffix_daily_bars={len(suffix_daily)}"
                ),
            )
    if warmup_blocker is not None:
    # 截断的历史前缀绝不能显示为有利的绿色快照。保留完整诊断状态与五日均线，
    # 只移除决策级快照，使所有消费者都对新开仓安全关闭。
        risk = replace(
            risk,
            snapshot=None,
            blockers=(*risk.blockers, warmup_blocker),
        )
    blockers = (*inputs.blockers, *structure.blockers, *risk.blockers)
    if risk.snapshot is None:
        grade: QmtRiskGrade = "UNRESOLVED"
    elif not blockers and inputs.same_base_stream:
        grade = "FULL_SYSTEM_ELIGIBLE"
    else:
        grade = "RESEARCH_ONLY"
    return QmtHigherTimeframeRiskEnvelope(
        inputs=inputs,
        structure=structure,
        risk=risk,
        warmup=warmup,
        grade=grade,
        blockers=tuple(blockers),
        warmup_convergence=warmup_convergence,
    )


__all__ = (
    "QmtHigherTimeframeInputs",
    "QmtHigherTimeframeRiskEnvelope",
    "QmtHigherTimeframeWarmupEvidence",
    "QMT_HIGHER_TIMEFRAME_WARMUP_EVIDENCE_CONTRACT_ID",
    "QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PARAMETER_SET_ID",
    "QMT_HIGHER_TIMEFRAME_WARMUP_CONVERGENCE_PREFIX_RATIOS",
    "QMT_HIGHER_TIMEFRAME_WARMUP_REQUIRED_DAILY_BARS",
    "QMT_HIGHER_TIMEFRAME_WARMUP_PHYSICAL_DAILY_BARS",
    "QMT_SECTOR_NATIVE_DAILY_RESEARCH_BASE_FREQUENCY",
    "build_qmt_higher_timeframe_risk",
    "qmt_higher_timeframe_inputs",
)
