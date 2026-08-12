"""Research-only QMT current-membership sector composites.

The caller explicitly accepts current GICS3 constituents being backfilled over
the recent-year replay.  This module makes that bias mechanical and visible;
it never presents the resulting bars as point-in-time membership evidence.
"""

from __future__ import annotations

from bisect import bisect_right
from datetime import datetime
from decimal import Decimal
import math
from pathlib import Path
from typing import Mapping, Sequence

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.backtest.pit_metadata import QmtFactorAt
from chanlun.decision_support.trading_system.backtest.fixed_year import (
    SECTOR_FACT_SCHEMA,
    SectorResearchFacts,
)
from chanlun.decision_support.trading_system.backtest.qmt_local_cache import (
    QMTLocalKlineAudit,
    read_qmt_local_kline,
)
from chanlun.decision_support.trading_system.qmt_sector_same_base import (
    derive_qmt_sector_thirty_minute_frame,
)
from chanlun.decision_support.trading_system.qmt_causal_factor_adjustment import (
    QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
    QmtCausalFactorEvent,
    apply_qmt_causal_factor_adjustment,
    build_causal_sector_price_basis_metadata,
    qmt_causal_factor_events_from_objects,
    qmt_causal_factor_revision,
)
from chanlun.decision_support.trading_system.sector_policy import assess_sector
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)


# 历史当前成员代理有意保留下方带偏差的成员模式，但其 K 线构造必须与页面/前向高周期
# 闸门消费的规范契约一致。此前保留第二套提供器、复权和方法身份，会让数值相同的两条
# 5m 中位收益链看似不同决策事实，并阻止回放使用实时板块月/周/日核心。
CURRENT_GICS3_COMPOSITE_PROVIDER = "qmt-gics3-composite"
CURRENT_GICS3_COMPOSITE_ADJUSTMENT = (
    "causal-factor-stable-24-member-median"
)
CURRENT_GICS3_COMPOSITE_MEMBER_LIMIT = 24
CURRENT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT = (
    "BIT_I_IS_SECTOR_COMPOSITE_MEMBERS_I"
)
CURRENT_GICS3_COMPOSITE_METHOD = (
    "DETERMINISTIC_HASH_SAMPLE_CAUSAL_FACTOR_MEDIAN_RETURN_CHAIN"
)
CURRENT_GICS3_PHYSICAL_5M_COVERAGE_SCHEMA = (
    "chanlun-qmt-current-sector-physical-5m-coverage"
)
_FIELDS = ("time", "open", "high", "low", "close", "volume")
_PRICES = ("open", "high", "low", "close")
_QUANTUM = Decimal("0.000001")


def deterministic_current_sector_composite_members(
    sector_id: str,
    members: Sequence[str],
) -> tuple[str, ...]:
    """Reproduce the frozen live deterministic representative sample."""

    if not sector_id:
        raise ValueError("current-sector identity is required")
    normalized = tuple(sorted(set(members)))
    if len(normalized) <= CURRENT_GICS3_COMPOSITE_MEMBER_LIMIT:
        return normalized
    ranked = sorted(
        normalized,
        key=lambda code: sha256_json(
            {
                "schema": "chanlun-qmt-gics3-sample",
                "sector_id": sector_id,
                "code": code,
            }
        ),
    )
    return tuple(
        sorted(ranked[:CURRENT_GICS3_COMPOSITE_MEMBER_LIMIT])
    )


def _member_path_revision(frame: pd.DataFrame) -> str | None:
    if frame.empty:
        return None
    return sha256_json(
        {
            "schema": "chanlun-qmt-sector-composite-member-path",
            "rows": tuple(
                {
                    "date": normalize_datetime(
                        pd.Timestamp(row.date).to_pydatetime(),
                        "current-sector member path date",
                    ),
                    "member_mask": int(row.member_mask),
                }
                for row in frame.itertuples(index=False)
            ),
        }
    )


def _attach_current_composite_provenance(
    frame: pd.DataFrame,
    *,
    sector_id: str,
    sector_members: tuple[str, ...],
    composite_members: tuple[str, ...],
    membership_revision: str,
    minimum_member_count: int,
    minimum_bar_coverage: Decimal,
    required_member_count: int,
    factor_revision: str | None,
) -> pd.DataFrame:
    frame.attrs.update(
        sector_id=sector_id,
        sector_membership_revision=membership_revision,
        # 范围说明不可变成员元组的提供方；下方独立成员模式继续披露该元组在研究年度内回填。
        sector_membership_scope="CALLER_SUPPLIED",
        sector_members=sector_members,
        sector_composite_members=composite_members,
        sector_composite_member_limit=CURRENT_GICS3_COMPOSITE_MEMBER_LIMIT,
        sector_composite_minimum_member_count=minimum_member_count,
        sector_composite_minimum_bar_coverage=str(minimum_bar_coverage),
        sector_composite_required_member_count=required_member_count,
        sector_composite_member_mask_contract=(
            CURRENT_GICS3_COMPOSITE_MEMBER_MASK_CONTRACT
        ),
        sector_composite_member_path_revision=_member_path_revision(frame),
        sector_composite_method=CURRENT_GICS3_COMPOSITE_METHOD,
        sector_membership_mode="CURRENT_CAPTURE_BACKFILLED_USER_AUTHORIZED",
        data_grade="RESEARCH_ONLY",
        live_status="LIVE_DISABLED",
        eligible_member_count=len(sector_members),
        required_member_count=required_member_count,
        sector_factor_adjustment_contract_id=(
            QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID
        ),
        sector_factor_revision=factor_revision,
    )
    return frame


def _empty(
    sector_id: str,
    *,
    sector_members: tuple[str, ...] = (),
    composite_members: tuple[str, ...] = (),
    minimum_member_count: int = 8,
    minimum_bar_coverage: Decimal = Decimal("0.60"),
    required_member_count: int = 8,
    factor_revision: str | None = None,
) -> pd.DataFrame:
    frame = pd.DataFrame(
        columns=(
            "code",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "member_mask",
        )
    )
    membership_revision = sha256_json(
        {
            "schema": "chanlun-qmt-gics3-members",
            "sector_id": sector_id,
            "members": sector_members,
            "composite_members": composite_members,
        }
    )
    metadata = (
        build_provider_price_basis_metadata(
            provider=CURRENT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=f"{sector_id}:{membership_revision.removeprefix('sha256:')}",
            adjustment=CURRENT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=_QUANTUM,
        )
        if factor_revision is None
        else build_causal_sector_price_basis_metadata(
            provider=CURRENT_GICS3_COMPOSITE_PROVIDER,
            market="a",
            code=f"{sector_id}:{membership_revision.removeprefix('sha256:')}",
            adjustment=CURRENT_GICS3_COMPOSITE_ADJUSTMENT,
            structure_price_quantum=_QUANTUM,
            factor_revision=factor_revision,
        )
    )
    result = attach_price_basis_metadata(frame, metadata)
    return _attach_current_composite_provenance(
        result,
        sector_id=sector_id,
        sector_members=sector_members,
        composite_members=composite_members,
        membership_revision=membership_revision,
        minimum_member_count=minimum_member_count,
        minimum_bar_coverage=minimum_bar_coverage,
        required_member_count=required_member_count,
        factor_revision=factor_revision,
    )


def _member_ratios(
    *,
    code: str,
    frame: pd.DataFrame,
    factor_events: Sequence[QmtCausalFactorEvent],
    start_at: datetime,
    end_at: datetime,
) -> pd.DataFrame | None:
    if frame.empty:
        return None
    work = frame.copy()
    for field in _FIELDS:
        work[field] = pd.to_numeric(work[field], errors="coerce")
    work = work.dropna(subset=list(_FIELDS))
    work = work[
        (work["time"] > 0)
        & (work["open"] > 0)
        & (work["high"] > 0)
        & (work["low"] > 0)
        & (work["close"] > 0)
        & (work["volume"] >= 0)
    ].copy()
    if work.empty:
        return None
    work["date"] = pd.to_datetime(
        work.pop("time"), unit="ms", utc=True
    ).dt.tz_convert("Asia/Shanghai")
    work = work.sort_values("date", kind="stable").drop_duplicates(
        "date", keep="last"
    )
    work = apply_qmt_causal_factor_adjustment(
        work,
        code=code,
        events=factor_events,
    )
    work["previous_close"] = work["close"].shift(1)
    left = pd.Timestamp(normalize_datetime(start_at, "start_at"))
    right = pd.Timestamp(normalize_datetime(end_at, "end_at"))
    work = work[
        (work["date"] >= left)
        & (work["date"] <= right)
        & work["previous_close"].notna()
    ].copy()
    if work.empty:
        return None
    output = pd.DataFrame({"date": work["date"]})
    for field in _PRICES:
        output[f"{field}_ratio"] = work[field] / work["previous_close"]
    ratios = output[[f"{field}_ratio" for field in _PRICES]]
    finite = ratios.map(lambda value: math.isfinite(float(value))).all(axis=1)
    output = output[finite & (ratios > 0).all(axis=1)]
    if output.empty:
        return None
    output.insert(0, "member", code)
    return output


def current_composite_from_member_frames(
    *,
    sector_id: str,
    member_frames: Mapping[str, pd.DataFrame],
    factors_by_code: Mapping[str, Sequence[QmtFactorAt]],
    eligible_member_count: int,
    start_at: datetime,
    end_at: datetime,
    minimum_member_count: int = 8,
    minimum_bar_coverage: Decimal = Decimal("0.60"),
    sector_members: Sequence[str] | None = None,
    composite_members: Sequence[str] | None = None,
) -> pd.DataFrame:
    """Build a deterministic median composite with constant current members."""

    if not sector_id:
        raise ValueError("current-sector identity is required")
    if eligible_member_count < 0:
        raise ValueError("eligible member count cannot be negative")
    if minimum_member_count <= 0:
        raise ValueError("minimum member count must be positive")
    if not Decimal("0") < minimum_bar_coverage <= Decimal("1"):
        raise ValueError("minimum bar coverage must be in (0, 1]")
    if set(factors_by_code) - set(member_frames):
        raise ValueError("factor map contains an unknown current-sector member")
    full_members = tuple(
        sorted(set(member_frames) if sector_members is None else set(sector_members))
    )
    representatives = tuple(
        sorted(
            set(member_frames)
            if composite_members is None
            else set(composite_members)
        )
    )
    if not set(member_frames).issubset(representatives):
        raise ValueError("member frame is outside the frozen representative sample")
    if not set(representatives).issubset(full_members):
        raise ValueError("representative sample is outside current-sector members")
    if len(representatives) > CURRENT_GICS3_COMPOSITE_MEMBER_LIMIT:
        raise ValueError("representative sample exceeds the frozen member limit")
    if (
        composite_members is not None
        and eligible_member_count != len(representatives)
    ):
        raise ValueError(
            "coverage denominator must equal the frozen representative sample"
        )
    factor_events_by_code = {
        code: qmt_causal_factor_events_from_objects(
            code=code,
            values=factors_by_code.get(code, ()),
            not_after=normalize_datetime(end_at, "end_at").date(),
        )
        for code in representatives
    }
    factor_revision = qmt_causal_factor_revision(
        members=representatives,
        events_by_code=factor_events_by_code,
        known_through=normalize_datetime(end_at, "end_at").date(),
    )
    required = max(
        minimum_member_count,
        math.ceil(eligible_member_count * float(minimum_bar_coverage)),
    )
    facts = tuple(
        value
        for code, frame in sorted(member_frames.items())
        if (
            value := _member_ratios(
                code=code,
                frame=frame,
                factor_events=factor_events_by_code[code],
                start_at=start_at,
                end_at=end_at,
            )
        )
        is not None
    )
    if not facts:
        return _empty(
            sector_id,
            sector_members=full_members,
            composite_members=representatives,
            minimum_member_count=minimum_member_count,
            minimum_bar_coverage=minimum_bar_coverage,
            required_member_count=required,
            factor_revision=factor_revision,
        )
    joined = pd.concat(facts, ignore_index=True)
    bit_by_member = {
        code: 1 << index for index, code in enumerate(representatives)
    }
    joined["member_bit"] = joined["member"].map(bit_by_member)
    if joined["member_bit"].isna().any():
        raise ValueError("composite contributor is outside its member-mask contract")
    grouped = joined.groupby("date", sort=True).agg(
        member_count=("member", "nunique"),
        member_mask=("member_bit", lambda values: sum(set(values))),
        open_ratio=("open_ratio", "median"),
        high_ratio=("high_ratio", "median"),
        low_ratio=("low_ratio", "median"),
        close_ratio=("close_ratio", "median"),
    )
    grouped = grouped[grouped["member_count"] >= required]
    rows: list[dict[str, object]] = []
    previous_close = 1000.0
    for observed_at, item in grouped.iterrows():
        opened = previous_close * float(item["open_ratio"])
        closed = previous_close * float(item["close_ratio"])
        high = max(previous_close * float(item["high_ratio"]), opened, closed)
        low = min(previous_close * float(item["low_ratio"]), opened, closed)
        rows.append(
            {
                "code": sector_id,
                "date": observed_at,
                "open": opened,
                "high": high,
                "low": low,
                "close": closed,
                "volume": float(item["member_count"]),
                "member_mask": int(item["member_mask"]),
            }
        )
        previous_close = closed
    if not rows:
        return _empty(
            sector_id,
            sector_members=full_members,
            composite_members=representatives,
            minimum_member_count=minimum_member_count,
            minimum_bar_coverage=minimum_bar_coverage,
            required_member_count=required,
            factor_revision=factor_revision,
        )
    membership_revision = sha256_json(
        {
            "schema": "chanlun-qmt-gics3-members",
            "sector_id": sector_id,
            "members": full_members,
            "composite_members": representatives,
        }
    )
    metadata = build_causal_sector_price_basis_metadata(
        provider=CURRENT_GICS3_COMPOSITE_PROVIDER,
        market="a",
        code=f"{sector_id}:{membership_revision.removeprefix('sha256:')}",
        adjustment=CURRENT_GICS3_COMPOSITE_ADJUSTMENT,
        structure_price_quantum=_QUANTUM,
        factor_revision=factor_revision,
    )
    result = attach_price_basis_metadata(pd.DataFrame(rows), metadata)
    return _attach_current_composite_provenance(
        result,
        sector_id=sector_id,
        sector_members=full_members,
        composite_members=representatives,
        membership_revision=membership_revision,
        minimum_member_count=minimum_member_count,
        minimum_bar_coverage=minimum_bar_coverage,
        required_member_count=required,
        factor_revision=factor_revision,
    )


class CurrentQmtGics3CompositeReplaySource:
    """Cache raw member bars and rebuild an exact point-in-time 5m prefix.

    A full-year composite cannot merely be sliced for an earlier decision:
    its factor revision and member-path revision would still bind evidence
    known after that decision.  Raw bars may be cached safely; each requested
    prefix is recomposed with only factor events effective by ``observed_at``.
    """

    def __init__(
        self,
        *,
        data_dir: Path,
        start_at: datetime,
        end_at: datetime,
        factors_by_code: Mapping[str, Sequence[QmtFactorAt]],
    ) -> None:
        self._data_dir = Path(data_dir).resolve()
        self._start_at = normalize_datetime(start_at, "start_at")
        self._end_at = normalize_datetime(end_at, "end_at")
        if self._start_at > self._end_at:
            raise ValueError("current-sector replay range is inverted")
        self._factors_by_code = {
            str(code): tuple(values) for code, values in factors_by_code.items()
        }
        self._member_frames: dict[tuple[str, str], pd.DataFrame] = {}
        self._member_audits: dict[
            tuple[str, str], QMTLocalKlineAudit | object
        ] = {}

    def _available_member_frames(
        self,
        *,
        representatives: tuple[str, ...],
        frequency: str,
    ) -> dict[str, pd.DataFrame]:
        available: dict[str, pd.DataFrame] = {}
        for code in representatives:
            key = (code, frequency)
            frame = self._member_frames.get(key)
            if frame is None:
                frame, audit = read_qmt_local_kline(
                    data_dir=self._data_dir,
                    code=code,
                    frequency=frequency,
                    start_at=self._start_at,
                    end_at=self._end_at,
                )
                frame = frame.loc[:, list(_FIELDS)].copy()
                self._member_frames[key] = frame
                self._member_audits[key] = audit
            if not frame.empty:
                available[code] = frame
        return available

    def five_minute_physical_source_coverage(
        self,
        *,
        sector_id: str,
        member_codes: Sequence[str],
        observed_at: datetime,
    ) -> dict[str, object]:
        """Explain whether the strict 5m left boundary is physical or requested.

        ``QMTLocalKlineAudit.first_at`` is the first *selected* record.  That
        alone cannot distinguish a replay window clipped at its caller-supplied
        start from a vendor cache whose file itself begins later.  This
        diagnostic uses the physical first record bound by each source file's
        SHA256.  It is never consumed by the decision core and cannot relax the
        480-session warmup gate.
        """

        observed = normalize_datetime(observed_at, "observed_at")
        if not self._start_at <= observed <= self._end_at:
            raise ValueError("sector decision lies outside the cached replay range")
        members = tuple(sorted(set(member_codes)))
        representatives = deterministic_current_sector_composite_members(
            sector_id,
            members,
        )
        # 同时填充不可变原始数据帧和审计缓存；重复前缀请求不会再次读取 QMT。
        self._available_member_frames(
            representatives=representatives,
            frequency="5m",
        )
        required = max(
            8,
            math.ceil(len(representatives) * float(Decimal("0.60"))),
        )
        source_rows: list[dict[str, object]] = []
        selected_firsts: list[datetime] = []
        physical_firsts: list[datetime] = []
        available_file_count = 0
        for code in representatives:
            audit = self._member_audits.get((code, "5m"))
            source_record_count = getattr(audit, "source_record_count", None)
            source_sha256 = getattr(audit, "source_sha256", None)
            source_first = getattr(audit, "source_first_at", None)
            source_last = getattr(audit, "source_last_at", None)
            selected_first = getattr(audit, "first_at", None)
            if isinstance(source_first, datetime):
                source_first = normalize_datetime(
                    source_first,
                    "QMT physical source first_at",
                )
                physical_firsts.append(source_first)
            else:
                source_first = None
            if isinstance(source_last, datetime):
                source_last = normalize_datetime(
                    source_last,
                    "QMT physical source last_at",
                )
            else:
                source_last = None
            if isinstance(selected_first, datetime):
                selected_first = normalize_datetime(
                    selected_first,
                    "QMT selected source first_at",
                )
                selected_firsts.append(selected_first)
            else:
                selected_first = None
            if type(source_record_count) is int and source_record_count > 0:
                available_file_count += 1
            source_rows.append(
                {
                    "code": code,
                    "source_sha256": (
                        source_sha256
                        if isinstance(source_sha256, str)
                        else "UNAVAILABLE"
                    ),
                    "source_record_count": (
                        source_record_count
                        if type(source_record_count) is int
                        else None
                    ),
                    "source_first_at": source_first,
                    "source_last_at": source_last,
                    "selected_first_at": selected_first,
                }
            )
        ordered_physical = sorted(physical_firsts)
        required_start = (
            ordered_physical[required - 1]
            if len(ordered_physical) >= required
            else None
        )
        if available_file_count < required:
            boundary_status = "INSUFFICIENT_PHYSICAL_QMT_MEMBER_FILES"
        elif required_start is None:
            boundary_status = "PHYSICAL_QMT_SOURCE_BOUNDARY_UNAVAILABLE"
        elif required_start > self._start_at:
            boundary_status = (
                "PHYSICAL_QMT_CACHE_LEFT_BOUNDARY_AFTER_REQUESTED_WARMUP"
            )
        else:
            boundary_status = (
                "REQUESTED_REPLAY_LEFT_BOUNDARY_CLIPS_EARLIER_QMT_HISTORY"
            )
        inventory_revision = sha256_json(
            {
                "schema": "chanlun-qmt-current-sector-5m-source-inventory",
                "sector_id": sector_id,
                "representatives": representatives,
                "sources": tuple(source_rows),
            }
        )
        stable: dict[str, object] = {
            "schema": CURRENT_GICS3_PHYSICAL_5M_COVERAGE_SCHEMA,
            "sector_id": sector_id,
            # 该映射附着于 ``DataFrame.attrs``，可能跨越图表证据归档的 parquet 边界；
            # 应保持真实 JSON 文档，不能把 Python datetime 对象泄漏进 pandas 元数据。
            "observed_at": observed.isoformat(),
            "requested_start_at": self._start_at.isoformat(),
            "representative_member_count": len(representatives),
            "available_member_file_count": available_file_count,
            "physical_boundary_member_count": len(physical_firsts),
            "missing_member_file_count": (
                len(representatives) - available_file_count
            ),
            "required_contributor_count": required,
            "physical_source_first_at_minimum": (
                None
                if not physical_firsts
                else min(physical_firsts).isoformat()
            ),
            "physical_source_first_at_maximum": (
                None
                if not physical_firsts
                else max(physical_firsts).isoformat()
            ),
            "required_contributor_physical_start_at": (
                None if required_start is None else required_start.isoformat()
            ),
            "selected_window_first_at_minimum": (
                None
                if not selected_firsts
                else min(selected_firsts).isoformat()
            ),
            "selected_window_first_at_maximum": (
                None
                if not selected_firsts
                else max(selected_firsts).isoformat()
            ),
            "boundary_status": boundary_status,
            "source_inventory_revision": inventory_revision,
            "diagnostic_only": True,
            "decision_core_input": False,
            "warmup_requirement_unchanged": True,
            "data_grade": "RESEARCH_ONLY",
            "live_status": "LIVE_DISABLED",
        }
        return {**stable, "audit_sha256": sha256_json(stable)}

    def five_minute_prefix(
        self,
        *,
        sector_id: str,
        member_codes: Sequence[str],
        observed_at: datetime,
    ) -> pd.DataFrame:
        observed = normalize_datetime(observed_at, "observed_at")
        if not self._start_at <= observed <= self._end_at:
            raise ValueError("sector decision lies outside the cached replay range")
        members = tuple(sorted(set(member_codes)))
        representatives = deterministic_current_sector_composite_members(
            sector_id,
            members,
        )
        available = self._available_member_frames(
            representatives=representatives,
            frequency="5m",
        )
        result = current_composite_from_member_frames(
            sector_id=sector_id,
            member_frames=available,
            factors_by_code={
                code: self._factors_by_code.get(code, ()) for code in available
            },
            eligible_member_count=len(representatives),
            start_at=self._start_at,
            end_at=observed,
            sector_members=members,
            composite_members=representatives,
        )
        result.attrs["qmt_physical_five_minute_source_coverage"] = (
            self.five_minute_physical_source_coverage(
                sector_id=sector_id,
                member_codes=members,
                observed_at=observed,
            )
        )
        return result

    def native_daily_prefix(
        self,
        *,
        sector_id: str,
        member_codes: Sequence[str],
        observed_at: datetime,
    ) -> pd.DataFrame:
        """Build a completed native-daily advisory prefix for M/W/D research.

        It is intentionally not relabelled as the 5m-derived daily object:
        cross-sectional median aggregation is nonlinear, so the two overlap
        paths cannot be reconciled.  Callers must retain the explicit
        unreconciled-research blocker when pairing this frame with 5m-derived
        30m bars.
        """

        observed = normalize_datetime(observed_at, "observed_at")
        if not self._start_at <= observed <= self._end_at:
            raise ValueError("sector decision lies outside the cached replay range")
        members = tuple(sorted(set(member_codes)))
        representatives = deterministic_current_sector_composite_members(
            sector_id,
            members,
        )
        available = self._available_member_frames(
            representatives=representatives,
            frequency="1d",
        )
        result = current_composite_from_member_frames(
            sector_id=sector_id,
            member_frames=available,
            factors_by_code={
                code: self._factors_by_code.get(code, ()) for code in available
            },
            eligible_member_count=len(representatives),
            start_at=self._start_at,
            # 此处保留决策日期，使除权日因子身份与 5m 侧一致；当前未完成日线记录只在赋予
            # 15:00 发布时间戳后才移除。
            end_at=observed,
            sector_members=members,
            composite_members=representatives,
        )
        attrs = dict(result.attrs)
        if not result.empty:
            result = result.copy()
            result["date"] = (
                pd.to_datetime(result["date"], errors="raise")
                .dt.tz_convert("Asia/Shanghai")
                .dt.normalize()
                + pd.Timedelta(hours=15)
            )
            result = result[
                result["date"] <= pd.Timestamp(observed)
            ].reset_index(drop=True)
        result.attrs = attrs
        result.attrs["sector_composite_member_path_revision"] = (
            _member_path_revision(result)
        )
        base_revision = sha256_json(
            {
                "schema": "chanlun-qmt-current-sector-native-daily-base",
                "sector_id": sector_id,
                "observed_at": observed,
                "price_basis_revision": result.attrs.get(
                    "price_basis_revision"
                ),
                "sector_membership_revision": result.attrs.get(
                    "sector_membership_revision"
                ),
                "sector_factor_revision": result.attrs.get(
                    "sector_factor_revision"
                ),
                "sector_composite_member_path_revision": result.attrs.get(
                    "sector_composite_member_path_revision"
                ),
                "rows": tuple(
                    {
                        "date": pd.Timestamp(row.date).to_pydatetime(),
                        "open": float(row.open),
                        "high": float(row.high),
                        "low": float(row.low),
                        "close": float(row.close),
                        "volume": float(row.volume),
                        "member_mask": int(row.member_mask),
                    }
                    for row in result.itertuples(index=False)
                ),
            }
        )
        result.attrs.update(
            source_base_frequency="native-d",
            source_base_stream_revision=base_revision,
            derived_frequency="d",
            sector_native_daily_role=(
                "UNRECONCILED_RESEARCH_MWD_ADVISORY_ONLY"
            ),
        )
        return result


def build_current_qmt_gics3_five_minute_composite(
    *,
    data_dir: Path,
    sector_id: str,
    member_codes: Sequence[str],
    factors_by_code: Mapping[str, Sequence[QmtFactorAt]],
    start_at: datetime,
    end_at: datetime,
) -> pd.DataFrame:
    """Build an exact current-membership 5m proxy through ``end_at``."""

    source = CurrentQmtGics3CompositeReplaySource(
        data_dir=data_dir,
        start_at=start_at,
        end_at=end_at,
        factors_by_code=factors_by_code,
    )
    return source.five_minute_prefix(
        sector_id=sector_id,
        member_codes=member_codes,
        observed_at=end_at,
    )


def build_current_qmt_gics3_composite(
    *,
    data_dir: Path,
    sector_id: str,
    member_codes: Sequence[str],
    factors_by_code: Mapping[str, Sequence[QmtFactorAt]],
    start_at: datetime,
    end_at: datetime,
) -> pd.DataFrame:
    """Build the historical 30m proxy from the shared 5m-first semantics.

    The previous research adapter aggregated every member to 30m first and
    only then took the cross-sectional median.  Median is nonlinear, so that
    object could not match the live page's 5m median-return chain.  Freeze the
    same deterministic 24-member sample, compose it at 5m, and only then form
    completed 30m buckets.
    """

    five_minute = build_current_qmt_gics3_five_minute_composite(
        data_dir=data_dir,
        sector_id=sector_id,
        member_codes=member_codes,
        factors_by_code=factors_by_code,
        start_at=start_at,
        end_at=end_at,
    )
    return derive_qmt_sector_thirty_minute_frame(
        five_minute,
    )


def reclassify_current_sector_facts(
    *,
    facts: SectorResearchFacts,
    frame: pd.DataFrame,
    expected_closes: Sequence[datetime],
    algorithm_revision: str,
    source_revision: str,
) -> SectorResearchFacts:
    """Reuse frozen causal contexts after registering a sector price source.

    The expensive prefix replay already embedded one ``TimeframeContext`` per
    assessment.  Re-running structure merely because a provider identifier was
    missing from the allowlist would add no evidence.  This function verifies
    the exact source revision and completed-bar grid, then applies the shared
    production sector policy to those immutable contexts.
    """

    if facts.schema != SECTOR_FACT_SCHEMA:
        raise ValueError("unsupported sector facts for reclassification")
    if facts.source_revision != source_revision:
        raise ValueError("sector source changed before reclassification")
    if len(frame) != facts.row_count:
        raise ValueError("sector row count changed before reclassification")
    if not algorithm_revision.startswith("sha256:"):
        raise ValueError("algorithm revision is required")
    frame_closes = tuple(
        pd.Timestamp(value).to_pydatetime() for value in frame["date"]
    )
    market_closes = tuple(sorted(set(expected_closes)))
    assessments = []
    for observed_at, previous in facts.assessments:
        expected_position = bisect_right(market_closes, observed_at)
        actual_position = bisect_right(frame_closes, observed_at)
        data_complete = (
            not market_closes
            or expected_position > 0
            and actual_position > 0
            and market_closes[expected_position - 1]
            == frame_closes[actual_position - 1]
        )
        assessments.append(
            (
                observed_at,
                assess_sector(
                    sector_id=facts.sector_id,
                    sector_name=facts.sector_name,
                    market_data_source=CURRENT_GICS3_COMPOSITE_PROVIDER,
                    thirty=previous.thirty_context,
                    five=previous.five_context,
                    one=previous.one_context,
                    data_complete=data_complete,
                ),
            )
        )
    return SectorResearchFacts(
        schema=SECTOR_FACT_SCHEMA,
        algorithm_revision=algorithm_revision,
        source_revision=source_revision,
        sector_id=facts.sector_id,
        sector_name=facts.sector_name,
        member_count=facts.member_count,
        row_count=facts.row_count,
        thirty_points=facts.thirty_points,
        assessments=tuple(assessments),
        direction_unavailable_count=facts.direction_unavailable_count,
        error=facts.error,
    )


__all__ = (
    "CURRENT_GICS3_COMPOSITE_ADJUSTMENT",
    "CURRENT_GICS3_COMPOSITE_MEMBER_LIMIT",
    "CURRENT_GICS3_COMPOSITE_PROVIDER",
    "CurrentQmtGics3CompositeReplaySource",
    "build_current_qmt_gics3_composite",
    "build_current_qmt_gics3_five_minute_composite",
    "current_composite_from_member_frames",
    "deterministic_current_sector_composite_members",
    "reclassify_current_sector_facts",
)
