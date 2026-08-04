from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.v31_parameters import (
    StrategyV31Parameters,
)


OperatingMode = Literal["RESEARCH", "PAPER", "LIVE"]


@dataclass(frozen=True, slots=True)
class ProgramTradingComplianceSnapshot:
    snapshot_id: str
    mode: OperatingMode
    observed_at: datetime
    valid_until: datetime
    strategy_id: str
    software_version: str
    program_trading_report_confirmed: bool
    broker_permission_confirmed: bool
    licensed_market_data: bool
    abnormal_trading_monitor_healthy: bool
    order_rate_limit_configured: bool
    cancellation_rate_monitor_configured: bool

    def __post_init__(self) -> None:
        observed = normalize_datetime(self.observed_at, "observed_at")
        valid_until = normalize_datetime(self.valid_until, "valid_until")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "valid_until", valid_until)
        if observed > valid_until:
            raise ValueError("compliance snapshot validity is reversed")
        if not all(
            value.strip()
            for value in (self.snapshot_id, self.strategy_id, self.software_version)
        ):
            raise ValueError("compliance identity is required")


@dataclass(frozen=True, slots=True)
class ComplianceDecision:
    allowed: bool
    highest_mode: OperatingMode
    reason_codes: tuple[str, ...]


def evaluate_program_trading_compliance(
    snapshot: ProgramTradingComplianceSnapshot,
    *,
    as_of: datetime,
    parameters: StrategyV31Parameters,
) -> ComplianceDecision:
    observed = normalize_datetime(as_of, "as_of")
    reasons: list[str] = []
    if not snapshot.observed_at <= observed <= snapshot.valid_until:
        reasons.append("COMPLIANCE_SNAPSHOT_NOT_VISIBLE_OR_EXPIRED")
    if snapshot.strategy_id != parameters.strategy_id:
        reasons.append("COMPLIANCE_STRATEGY_VERSION_MISMATCH")
    if snapshot.mode == "LIVE":
        checks = (
            (snapshot.program_trading_report_confirmed, "PROGRAM_TRADING_REPORT_MISSING"),
            (snapshot.broker_permission_confirmed, "BROKER_PROGRAM_PERMISSION_MISSING"),
            (snapshot.licensed_market_data, "LICENSED_MARKET_DATA_MISSING"),
            (
                snapshot.abnormal_trading_monitor_healthy,
                "ABNORMAL_TRADING_MONITOR_UNHEALTHY",
            ),
            (snapshot.order_rate_limit_configured, "ORDER_RATE_LIMIT_MISSING"),
            (
                snapshot.cancellation_rate_monitor_configured,
                "CANCELLATION_RATE_MONITOR_MISSING",
            ),
        )
        reasons.extend(code for passed, code in checks if not passed)
        # The codebase deliberately cannot lift this gate.  A future live
        # release requires a separately signed strategy version.
        reasons.append("V31_LIVE_STATUS_DISABLED")
        return ComplianceDecision(False, "PAPER", tuple(reasons))
    if parameters.require_licensed_market_data and not snapshot.licensed_market_data:
        reasons.append("LICENSED_MARKET_DATA_MISSING")
    return ComplianceDecision(not reasons, snapshot.mode, tuple(reasons))


__all__ = [
    "ComplianceDecision",
    "OperatingMode",
    "ProgramTradingComplianceSnapshot",
    "evaluate_program_trading_compliance",
]
