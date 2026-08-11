from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping

from chanlun.decision_support.fingerprints import normalize_datetime


_FREQUENCIES = {"1m", "5m", "30m", "d"}
_FREQUENCY_ORDER = {"1m": 0, "5m": 1, "30m": 2, "d": 3}
_DEPENDENCIES = {
    "1m": ("1m",),
    "5m": ("1m", "5m"),
    "30m": ("1m", "5m", "30m"),
    "d": ("1m", "5m", "30m", "d"),
}


@dataclass(frozen=True, slots=True)
class BarKey:
    code: str
    frequency: str
    closed_at: datetime

    def __post_init__(self) -> None:
        if self.frequency not in _FREQUENCIES:
            raise ValueError(f"unsupported frequency: {self.frequency}")
        object.__setattr__(
            self,
            "closed_at",
            normalize_datetime(self.closed_at, "closed_at"),
        )


@dataclass(frozen=True, slots=True)
class ScanCursor:
    initialized: bool
    structure_contract_id: str | None
    parameter_set_id: str | None

    @classmethod
    def empty(cls) -> "ScanCursor":
        return cls(False, None, None)

    @classmethod
    def current(
        cls,
        *,
        structure_contract_id: str = "strict-structure",
        parameter_set_id: str = "frozen-parameters",
    ) -> "ScanCursor":
        return cls(True, structure_contract_id, parameter_set_id)


@dataclass(frozen=True, slots=True)
class ScanPlan:
    sectors: tuple[str, ...]
    symbols: tuple[str, ...]
    symbol_frequencies: tuple[tuple[str, tuple[str, ...]], ...]
    full_market_history_scan: bool
    background_full_refresh_required: bool

    def frequencies_for(self, code: str) -> tuple[str, ...]:
        return dict(self.symbol_frequencies).get(code, ())


def build_scan_plan(
    *,
    changed_bars: tuple[BarKey, ...],
    sector_members: Mapping[str, tuple[str, ...]],
    known_sector_ids: tuple[str, ...] = (),
    active_watchlist: tuple[str, ...],
    previous: ScanCursor,
    holdings: tuple[str, ...] = (),
    structure_contract_id: str = "strict-structure",
    parameter_set_id: str = "frozen-parameters",
) -> ScanPlan:
    sectors: set[str] = set()
    scheduled: dict[str, set[str]] = {}
    sector_ids = set(known_sector_ids).union(sector_members)

    def schedule(code: str, frequencies: tuple[str, ...]) -> None:
        scheduled.setdefault(code, set()).update(frequencies)

    for bar in sorted(
        set(changed_bars),
        key=lambda item: (item.closed_at, item.code, item.frequency),
    ):
        if bar.code in sector_ids or bar.code.startswith("TDX.88"):
            sectors.add(bar.code)
            for member in sector_members.get(bar.code, ()):
                schedule(member, ("1m", "5m", "30m", "d"))
            continue
        schedule(bar.code, _DEPENDENCIES[bar.frequency])

    for code in active_watchlist:
        schedule(code, ("1m",))
    for code in holdings:
        schedule(code, ("1m",))

    symbols = tuple(sorted(scheduled))
    frequency_rows = tuple(
        (
            code,
            tuple(sorted(scheduled[code], key=_FREQUENCY_ORDER.__getitem__)),
        )
        for code in symbols
    )
    background_refresh = (
        not previous.initialized
        or previous.structure_contract_id != structure_contract_id
        or previous.parameter_set_id != parameter_set_id
    )
    return ScanPlan(
        sectors=tuple(sorted(sectors)),
        symbols=symbols,
        symbol_frequencies=frequency_rows,
        full_market_history_scan=False,
        background_full_refresh_required=background_refresh,
    )


__all__ = [
    "BarKey",
    "ScanCursor",
    "ScanPlan",
    "build_scan_plan",
]
