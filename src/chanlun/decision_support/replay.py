from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
import json
from typing import Callable, Mapping

from .event_service import EventView
from .fingerprints import normalize_datetime
from .models import DecisionEvent
from .scanner import DecisionScanner, ScanResult


@dataclass(frozen=True, slots=True)
class ReplayBar:
    frequency: str
    closed_at: datetime
    available_at: datetime
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.frequency, str) or not self.frequency:
            raise ValueError("frequency must be a non-empty string")
        object.__setattr__(
            self,
            "closed_at",
            normalize_datetime(self.closed_at, "closed_at"),
        )
        object.__setattr__(
            self,
            "available_at",
            normalize_datetime(self.available_at, "available_at"),
        )
        if self.available_at < self.closed_at:
            raise ValueError("bar cannot be available before it closes")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", dict(self.payload))


class ReplayFeed:
    def __init__(self) -> None:
        self._visible: dict[str, list[ReplayBar]] = {}

    def append(self, bar: ReplayBar) -> None:
        if not isinstance(bar, ReplayBar):
            raise TypeError("bar must be ReplayBar")
        values = self._visible.setdefault(bar.frequency, [])
        if values and bar.available_at < values[-1].available_at:
            raise ValueError("replay feed cannot move backwards")
        values.append(bar)

    def bars(self, frequency: str) -> tuple[Mapping[str, object], ...]:
        if not isinstance(frequency, str) or not frequency:
            raise ValueError("frequency must be a non-empty string")
        return tuple(bar.payload for bar in self._visible.get(frequency, ()))

    def records(self, frequency: str) -> tuple[ReplayBar, ...]:
        if not isinstance(frequency, str) or not frequency:
            raise ValueError("frequency must be a non-empty string")
        return tuple(self._visible.get(frequency, ()))


@dataclass(frozen=True, slots=True)
class ReplayInput:
    bars: tuple[ReplayBar, ...]
    scanner_factory: Callable[[ReplayFeed], DecisionScanner]
    operation_frequency: str = "5m"

    def __post_init__(self) -> None:
        object.__setattr__(self, "bars", tuple(self.bars))
        if not self.bars:
            raise ValueError("replay bars cannot be empty")
        if not all(isinstance(bar, ReplayBar) for bar in self.bars):
            raise TypeError("bars must contain ReplayBar values")
        if not callable(self.scanner_factory):
            raise TypeError("scanner_factory must be callable")
        if not isinstance(self.operation_frequency, str) or not self.operation_frequency:
            raise ValueError("operation_frequency must be a non-empty string")


@dataclass(frozen=True, slots=True)
class ReplayResult:
    events: tuple[DecisionEvent, ...]
    views: tuple[EventView, ...]
    scans: tuple[ScanResult, ...]
    operation_bars_processed: int


@dataclass(frozen=True, slots=True)
class EventStreamComparison:
    matches: bool
    missing_event_ids: tuple[str, ...]
    unexpected_event_ids: tuple[str, ...]
    differing_event_ids: tuple[str, ...]


def replay_symbol(
    replay_input: ReplayInput,
    *,
    until_bar: int | None = None,
) -> ReplayResult:
    if not isinstance(replay_input, ReplayInput):
        raise TypeError("replay_input must be ReplayInput")
    if until_bar is not None and (
        isinstance(until_bar, bool)
        or not isinstance(until_bar, int)
        or until_bar < 0
    ):
        raise ValueError("until_bar must be a non-negative integer")

    feed = ReplayFeed()
    scanner = replay_input.scanner_factory(feed)
    if not isinstance(scanner, DecisionScanner):
        raise TypeError("scanner_factory must return DecisionScanner")
    ordered = sorted(
        replay_input.bars,
        key=lambda item: (
            item.available_at,
            item.frequency,
            item.closed_at,
        ),
    )
    scans: list[ScanResult] = []
    processed = 0
    stop = until_bar == 0
    for _available_at, grouped in groupby(
        ordered,
        key=lambda item: item.available_at,
    ):
        if stop:
            break
        group = tuple(grouped)
        for bar in group:
            feed.append(bar)
        operation_bars = sorted(
            (
                bar
                for bar in group
                if bar.frequency == replay_input.operation_frequency
            ),
            key=lambda item: item.closed_at,
        )
        for bar in operation_bars:
            if until_bar is not None and processed >= until_bar:
                stop = True
                break
            scans.append(scanner.scan_closed_bar(bar.closed_at))
            processed += 1

    events = scanner.event_service.store.list_events()
    views = tuple(
        scanner.event_service.get(event.event_id) for event in events
    )
    return ReplayResult(events, views, tuple(scans), processed)


def _canonical_event_bytes(event: DecisionEvent) -> bytes:
    if not isinstance(event, DecisionEvent):
        raise TypeError("event stream must contain DecisionEvent values")
    return json.dumps(
        event.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _event_index(
    events: tuple[DecisionEvent, ...],
) -> tuple[tuple[str, ...], dict[str, bytes]]:
    identifiers: list[str] = []
    payloads: dict[str, bytes] = {}
    for event in events:
        if event.event_id in payloads:
            raise ValueError(f"duplicate event_id in stream: {event.event_id}")
        identifiers.append(event.event_id)
        payloads[event.event_id] = _canonical_event_bytes(event)
    return tuple(identifiers), payloads


def compare_event_streams(
    expected: tuple[DecisionEvent, ...],
    actual: tuple[DecisionEvent, ...],
) -> EventStreamComparison:
    expected_ids, expected_payloads = _event_index(tuple(expected))
    actual_ids, actual_payloads = _event_index(tuple(actual))
    missing = tuple(
        event_id for event_id in expected_ids if event_id not in actual_payloads
    )
    unexpected = tuple(
        event_id for event_id in actual_ids if event_id not in expected_payloads
    )
    differing = tuple(
        event_id
        for event_id in expected_ids
        if event_id in actual_payloads
        and expected_payloads[event_id] != actual_payloads[event_id]
    )
    matches = (
        not missing
        and not unexpected
        and not differing
        and expected_ids == actual_ids
    )
    return EventStreamComparison(matches, missing, unexpected, differing)
