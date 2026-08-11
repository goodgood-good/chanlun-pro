"""Point-in-time QMT sector trigger facts for the strict strategy individual-stock path.

The trigger is deliberately an outer scheduling/selection fact.  It does not
invent a sector buy signal and it never creates an order.  A completed 30m
sector context schedules the point-in-time members; the unchanged stock
30m/5m/1m structure remains the only technical entry authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
from datetime import datetime, time
from typing import Literal

import pandas as pd

from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.models import SectorAssessment


SectorTriggerSource = Literal["QMT_GICS3_CURRENT", "QMT_SW1_PIT"]
_QMT_MARKET_DATA_SOURCES = {
    "QMT_GICS3_CURRENT": "qmt_gics3_component_composite",
    "QMT_SW1_PIT": "qmt-sw1-pit-composite",
}


def _sha256(value: str, label: str) -> None:
    if not (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    ):
        raise ValueError(f"{label} must be a sha256 identity")


@dataclass(frozen=True, slots=True)
class SectorTriggerSnapshot:
    """Proof that a stock was reached through its QMT sector at this prefix."""

    snapshot_id: str
    symbol: str
    sector_id: str
    sector_name: str
    observed_at: datetime
    source: SectorTriggerSource
    source_frequency: str
    catalog_revision: str
    membership_revision: str
    market_data_membership_revision: str
    catalog_captured_at: datetime
    membership_known_at: datetime
    membership_valid_until: datetime
    latest_completed_bar_at: datetime
    expected_latest_bar_at: datetime
    member_count: int
    symbol_is_member: bool
    data_complete: bool
    sector_eligible: bool
    sector_hard_block: bool
    sector_regime: str
    reason_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "observed_at",
            "catalog_captured_at",
            "membership_known_at",
            "membership_valid_until",
            "latest_completed_bar_at",
            "expected_latest_bar_at",
        ):
            object.__setattr__(self, field, normalize_datetime(getattr(self, field), field))
        if not all(
            isinstance(value, str) and value.strip()
            for value in (self.snapshot_id, self.symbol, self.sector_id, self.sector_name)
        ):
            raise ValueError("sector trigger identity is required")
        if self.source not in _QMT_MARKET_DATA_SOURCES:
            raise ValueError("sector trigger must come from an approved QMT source")
        if self.source_frequency != "30m":
            raise ValueError("strict strategy sector trigger frequency is frozen at 30m")
        _sha256(self.catalog_revision, "catalog_revision")
        _sha256(self.membership_revision, "membership_revision")
        _sha256(
            self.market_data_membership_revision,
            "market_data_membership_revision",
        )
        if self.member_count < 0:
            raise ValueError("sector member count cannot be negative")
        if self.membership_known_at > self.membership_valid_until:
            raise ValueError("sector membership validity interval is inverted")
        if self.latest_completed_bar_at > self.observed_at:
            raise ValueError("sector trigger cannot consume a future bar")
        if self.catalog_captured_at > self.observed_at:
            raise ValueError("sector catalog cannot be captured in the future")
        if self.sector_hard_block and self.sector_eligible:
            raise ValueError("hard-blocked sector cannot be eligible")
        if not self.reason_codes:
            raise ValueError("sector trigger reasons are required")

    @property
    def market_data_source(self) -> str:
        return _QMT_MARKET_DATA_SOURCES[self.source]

    def visible_at(self, decision_time: datetime) -> bool:
        decision = normalize_datetime(decision_time, "decision_time")
        membership_visible = (
            self.membership_known_at <= decision <= self.membership_valid_until
        )
        # QMT's current catalog is a capture, not an effective-dated history.
        # It is therefore usable only for decisions later on that same session.
        current_scope_valid = self.source != "QMT_GICS3_CURRENT" or (
            self.catalog_captured_at.date() == decision.date()
            and self.catalog_captured_at <= decision
        )
        return (
            self.observed_at <= decision
            and membership_visible
            and current_scope_valid
        )

    def passes(self, decision_time: datetime) -> bool:
        return all(
            (
                self.visible_at(decision_time),
                self.latest_completed_bar_at == self.expected_latest_bar_at,
                self.data_complete,
                self.member_count > 0,
                self.symbol_is_member,
                self.sector_eligible,
                not self.sector_hard_block,
            )
        )


def build_sector_trigger_snapshot(
    *,
    symbol: str,
    assessment: SectorAssessment,
    observed_at: datetime,
    source: SectorTriggerSource,
    catalog_revision: str,
    catalog_captured_at: datetime,
    membership_known_at: datetime,
    membership_valid_until: datetime,
    members: tuple[str, ...],
    latest_completed_bar_at: datetime,
    expected_latest_bar_at: datetime,
    data_complete: bool,
    market_data_membership_revision: str | None = None,
) -> SectorTriggerSnapshot:
    """Freeze one sector-first scheduling fact without changing signal logic."""

    if len(members) != len(set(members)) or members != tuple(sorted(members)):
        raise ValueError("sector members must be unique and sorted")
    membership_revision = sha256_json(
        {
            "schema": "chanlun-qmt-sector-membership",
            "source": source,
            "sector_id": assessment.sector_id,
            "members": members,
            "known_at": normalize_datetime(
                membership_known_at, "membership_known_at"
            ),
            "valid_until": normalize_datetime(
                membership_valid_until, "membership_valid_until"
            ),
        }
    )
    document = {
        "schema": "chanlun-sector-trigger",
        "symbol": symbol,
        "sector_id": assessment.sector_id,
        "observed_at": normalize_datetime(observed_at, "observed_at"),
        "source": source,
        "catalog_revision": catalog_revision,
        "membership_revision": membership_revision,
        "market_data_membership_revision": (
            market_data_membership_revision or membership_revision
        ),
        "latest_completed_bar_at": normalize_datetime(
            latest_completed_bar_at, "latest_completed_bar_at"
        ),
        "expected_latest_bar_at": normalize_datetime(
            expected_latest_bar_at, "expected_latest_bar_at"
        ),
        "data_complete": data_complete,
        "eligible": assessment.eligible,
        "hard_block": assessment.hard_block,
    }
    return SectorTriggerSnapshot(
        snapshot_id=sha256_json(document),
        symbol=symbol,
        sector_id=assessment.sector_id,
        sector_name=assessment.sector_name,
        observed_at=observed_at,
        source=source,
        source_frequency="30m",
        catalog_revision=catalog_revision,
        membership_revision=membership_revision,
        market_data_membership_revision=(
            market_data_membership_revision or membership_revision
        ),
        catalog_captured_at=catalog_captured_at,
        membership_known_at=membership_known_at,
        membership_valid_until=membership_valid_until,
        latest_completed_bar_at=latest_completed_bar_at,
        expected_latest_bar_at=expected_latest_bar_at,
        member_count=len(members),
        symbol_is_member=symbol in members,
        data_complete=data_complete,
        sector_eligible=assessment.eligible,
        sector_hard_block=assessment.hard_block,
        sector_regime=assessment.regime,
        reason_codes=assessment.reason_codes or ("SECTOR_ASSESSMENT_NO_REASON",),
    )


def build_current_qmt_sector_trigger(
    *,
    symbol: str,
    assessment: SectorAssessment,
    catalog: Mapping[str, object],
    sector_frame: pd.DataFrame,
    decision_time: datetime,
    expected_latest_bar_at: datetime,
) -> SectorTriggerSnapshot:
    """Bind a live QMT catalog capture and its completed 30m composite."""

    decision = normalize_datetime(decision_time, "decision_time")
    if catalog.get("source") != "qmt_gics3_components":
        raise ValueError("current sector trigger requires the QMT GICS3 catalog")
    if catalog.get("point_in_time_scope") != "CURRENT_CAPTURE_ONLY":
        raise ValueError("QMT catalog point-in-time scope is missing")
    try:
        captured_at = normalize_datetime(
            datetime.fromisoformat(str(catalog["captured_at"])),
            "catalog.captured_at",
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("QMT catalog capture time is invalid") from exc
    catalog_revision = str(catalog.get("catalog_revision") or "")
    _sha256(catalog_revision, "catalog_revision")
    rows = catalog.get("sectors")
    if not isinstance(rows, list):
        raise ValueError("QMT catalog sectors are unavailable")
    matches = tuple(
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("sector_id") == assessment.sector_id
    )
    if len(matches) != 1:
        raise ValueError("candidate sector must resolve exactly once in QMT catalog")
    [row] = matches
    raw_members = row.get("member_codes")
    if not isinstance(raw_members, list) or any(
        not isinstance(value, str) for value in raw_members
    ):
        raise ValueError("QMT catalog member list is invalid")
    members = tuple(sorted(set(raw_members)))
    if len(members) != len(raw_members):
        raise ValueError("QMT catalog member list is not unique")
    if sector_frame.empty or "date" not in sector_frame:
        raise ValueError("QMT 30m sector frame is unavailable")
    completions = pd.to_datetime(sector_frame["date"], errors="raise")
    if completions.dt.tz is None:
        raise ValueError("QMT sector completion times must be timezone-aware")
    completions = completions.dt.tz_convert("Asia/Shanghai")
    if completions.duplicated().any() or not completions.is_monotonic_increasing:
        raise ValueError("QMT sector completion times must be unique and ordered")
    latest = completions.iloc[-1].to_pydatetime()
    frame_membership = str(
        sector_frame.attrs.get("sector_membership_revision") or ""
    )
    _sha256(frame_membership, "sector frame membership revision")
    membership_valid_until = datetime.combine(
        captured_at.date(),
        time(23, 59, 59),
        tzinfo=captured_at.tzinfo,
    )
    return build_sector_trigger_snapshot(
        symbol=symbol,
        assessment=assessment,
        observed_at=decision,
        source="QMT_GICS3_CURRENT",
        catalog_revision=catalog_revision,
        catalog_captured_at=captured_at,
        membership_known_at=captured_at,
        membership_valid_until=membership_valid_until,
        members=members,
        latest_completed_bar_at=latest,
        expected_latest_bar_at=expected_latest_bar_at,
        data_complete=(
            latest == normalize_datetime(
                expected_latest_bar_at,
                "expected_latest_bar_at",
            )
        ),
        market_data_membership_revision=frame_membership,
    )


__all__ = (
    "SectorTriggerSnapshot",
    "SectorTriggerSource",
    "build_current_qmt_sector_trigger",
    "build_sector_trigger_snapshot",
)
