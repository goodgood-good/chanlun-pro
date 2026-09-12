from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Literal
from chanlun.core.strict_structure.center_geometry import return_outside_core
from chanlun.core.strict_structure.immutable_state import frozen_dataclass_state
from chanlun.core.strict_structure.identity import build_center_id

Direction = Literal["up", "down"]


def is_return_from_geometry(
    *, direction: Direction, departure_direction: Direction
) -> bool:
    if departure_direction not in ("up", "down"):
        raise ValueError("departure direction must be up or down")
    return direction != departure_direction


class SourceKind(str, Enum):
    SEGMENT = "segment"
    STROKE_OBSERVATION = "stroke_observation"


def center_seed_size(source_kind: SourceKind) -> int:
    """返回中枢三段价格核心的宽度。

    ``initial_units`` 始终保存用于冻结 ``ZD/ZG`` 交集的三个连续、已完成同级
    单元；进入段与独立离开段不计入这个价格核心。
    """
    SourceKind(source_kind)
    return 3


class CenterState(str, Enum):
    ONGOING = "ongoing"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"


class CenterPreviewState(str, Enum):
    TOUCH_ONLY = "touch_only"
    FORMING = "forming"
    COMPLETED = "completed"


class CenterEventKind(str, Enum):
    ESTABLISHED = "center_established"
    EXTENDED = "center_extended"
    BREAKOUT_WATCH_UP = "breakout_watch_up"
    BREAKOUT_WATCH_DOWN = "breakout_watch_down"
    COMPLETED_UP = "center_completed_up"
    COMPLETED_DOWN = "center_completed_down"
    SUPERSEDED = "center_superseded"


@dataclass(frozen=True, slots=True)
class ConstituentUnit:
    __getstate__ = frozen_dataclass_state
    unit_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    direction: Direction
    start_tick: int
    end_tick: int
    low_tick: int
    high_tick: int
    market_start: datetime
    market_end: datetime
    confirmed_at: datetime | None
    available_at: datetime
    locked: bool
    child_ids: tuple[str, ...]
    forming: bool = False
    formed_at: datetime | None = None
    low_market_time: datetime | None = None
    high_market_time: datetime | None = None

    def __post_init__(self) -> None:
        if not self.unit_id:
            raise ValueError("unit_id is required")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be >= 0")
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        if self.direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        ticks = (self.start_tick, self.end_tick, self.low_tick, self.high_tick)
        if any((type(tick) is not int for tick in ticks)):
            raise TypeError("ticks must be integers")
        if self.direction == "up" and self.end_tick < self.start_tick:
            raise ValueError("up unit must not end below start")
        if self.direction == "down" and self.end_tick > self.start_tick:
            raise ValueError("down unit must not end above start")
        if self.low_tick > self.high_tick:
            raise ValueError("low_tick must be <= high_tick")
        if not self.low_tick <= self.start_tick <= self.high_tick:
            raise ValueError("start_tick must be inside the unit range")
        if not self.low_tick <= self.end_tick <= self.high_tick:
            raise ValueError("end_tick must be inside the unit range")
        if self.market_end < self.market_start:
            raise ValueError("market_end must not precede market_start")
        for field in ("low_market_time", "high_market_time"):
            moment = getattr(self, field)
            if moment is not None and (
                not self.market_start <= moment <= self.market_end
            ):
                raise ValueError("unit extreme time must belong to its market interval")
        if type(self.locked) is not bool:
            raise TypeError("locked must be a bool")
        if type(self.forming) is not bool:
            raise TypeError("forming must be a bool")
        if self.locked and self.forming:
            raise ValueError("a locked unit cannot still be forming")
        if self.locked != (self.confirmed_at is not None):
            raise ValueError("locked and confirmed_at must agree")
        if self.confirmed_at is not None and self.confirmed_at < self.market_end:
            raise ValueError("confirmed_at must not precede market_end")
        if self.available_at < self.market_end:
            raise ValueError("available_at must not precede market_end")
        if self.confirmed_at is not None and self.available_at < self.confirmed_at:
            raise ValueError("available_at must not precede confirmed_at")
        formed_at = self.formed_at
        if self.forming:
            if formed_at is not None:
                raise ValueError("a forming unit cannot carry formed_at")
        elif formed_at is not None:
            if formed_at < self.market_end:
                raise ValueError("formed_at must not precede market_end")
            if self.available_at < formed_at:
                raise ValueError("available_at must not precede formed_at")
            if self.confirmed_at is not None and self.confirmed_at < formed_at:
                raise ValueError("confirmed_at must not precede formed_at")
        child_ids = tuple(self.child_ids)
        if any(
            (not isinstance(child_id, str) or not child_id for child_id in child_ids)
        ):
            raise ValueError("child_ids must contain non-empty strings")
        object.__setattr__(self, "child_ids", child_ids)

    def extreme_market_time(self, side: str) -> datetime | None:
        if side not in ("low", "high"):
            raise ValueError("extreme side must be low or high")
        moment = getattr(self, side + "_market_time")
        tick = getattr(self, side + "_tick")
        return (
            moment
            if moment is not None
            else self.market_end
            if tick == self.end_tick
            else self.market_start
            if tick == self.start_tick
            else None
        )

    def is_return_from(self, departure_direction: Direction) -> bool:
        return is_return_from_geometry(
            direction=self.direction, departure_direction=departure_direction
        )


@dataclass(frozen=True, slots=True)
class TrendCenter:
    __getstate__ = frozen_dataclass_state
    center_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    state: CenterState
    entry_unit: ConstituentUnit | None
    establishment_leave_unit: ConstituentUnit | None
    initial_units: tuple[ConstituentUnit, ...]
    body_units: tuple[ConstituentUnit, ...]
    extension_units: tuple[ConstituentUnit, ...]
    zd_tick: int
    zg_tick: int
    dd_tick: int
    gg_tick: int
    body_start_market_time: datetime
    established_market_time: datetime
    established_at: datetime
    last_touch_market_time: datetime
    pending_leave_unit: ConstituentUnit | None
    completion_leave_unit: ConstituentUnit | None
    completion_return_unit: ConstituentUnit | None
    completed_at: datetime | None
    available_at: datetime
    body_revision: int
    failed_departure_units: tuple[ConstituentUnit, ...] = ()
    superseded_by_center_id: str | None = None
    superseded_at: datetime | None = None
    supersession_bridge_units: tuple[ConstituentUnit, ...] = ()
    formation_rule: Literal["five_role"] | None = None
    boundary_contact: Literal["oscillation", "third_class"] = "third_class"

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "state", CenterState(self.state))
        rule = self.formation_rule
        if rule is None:
            rule = "five_role"
        if rule != "five_role":
            raise ValueError("unknown center formation rule")
        object.__setattr__(self, "formation_rule", rule)
        if self.boundary_contact not in ("oscillation", "third_class"):
            raise ValueError("unknown boundary contact interpretation")
        object.__setattr__(self, "initial_units", tuple(self.initial_units))
        object.__setattr__(self, "body_units", tuple(self.body_units))
        object.__setattr__(self, "extension_units", tuple(self.extension_units))
        object.__setattr__(
            self, "failed_departure_units", tuple(self.failed_departure_units)
        )
        object.__setattr__(
            self, "supersession_bridge_units", tuple(self.supersession_bridge_units)
        )
        if not self.center_id:
            raise ValueError("center_id is required")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be >= 0")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        expected_initial_count = center_seed_size(self.source_kind)
        if len(self.initial_units) != expected_initial_count:
            raise ValueError(
                "initial_units must contain the source-specific center body"
            )
        if self.body_units != self.initial_units + self.extension_units:
            raise ValueError("body units must equal initial plus extension units")
        if any(
            (
                item.structural_level != self.structural_level
                or item.source_kind is not self.source_kind
                for item in self.body_units
            )
        ):
            raise ValueError("center body level/source mismatch")
        if any(
            (
                item.price_basis_revision != self.price_basis_revision
                for item in self.body_units
            )
        ):
            raise ValueError("center body price basis mismatch")
        if any((not item.locked for item in self.body_units)):
            raise ValueError("formal center body units must be locked")
        if self.entry_unit is not None:
            if (
                self.entry_unit.structural_level != self.structural_level
                or self.entry_unit.source_kind is not self.source_kind
            ):
                raise ValueError("center entry level/source mismatch")
            if self.entry_unit.price_basis_revision != self.price_basis_revision:
                raise ValueError("center entry price basis mismatch")
            if not self.entry_unit.locked:
                raise ValueError("formal center entry unit must be locked")
            if self.entry_unit in self.body_units:
                raise ValueError("external entry must not enter center body")
        establishment_leave = self.establishment_leave_unit
        if self.entry_unit is None:
            raise ValueError("physical center requires an external entry unit")
        if establishment_leave is None:
            raise ValueError("physical center requires an external leave unit")
        if (
            establishment_leave.structural_level != self.structural_level
            or establishment_leave.source_kind is not self.source_kind
        ):
            raise ValueError("establishment leave level/source mismatch")
        if establishment_leave.price_basis_revision != self.price_basis_revision:
            raise ValueError("establishment leave price basis mismatch")
        if not establishment_leave.locked:
            raise ValueError("formal establishment leave must be locked")
        if establishment_leave in self.body_units:
            raise ValueError("establishment leave must stay outside center body")
        expected_zd = max((item.low_tick for item in self.core_units))
        expected_zg = min((item.high_tick for item in self.core_units))
        if (self.zd_tick, self.zg_tick) != (expected_zd, expected_zg):
            raise ValueError("center core must equal its three core-unit intersection")
        envelope_units = self.body_units + ()
        expected_dd = min((item.low_tick for item in envelope_units))
        expected_gg = max((item.high_tick for item in envelope_units))
        if (self.dd_tick, self.gg_tick) != (expected_dd, expected_gg):
            raise ValueError("center envelope must equal body envelope")
        if self.zd_tick >= self.zg_tick:
            raise ValueError("line center requires zd_tick < zg_tick")
        if self.dd_tick > self.zd_tick or self.gg_tick < self.zg_tick:
            raise ValueError("envelope must contain the core")
        if any((not self._touches_core(item) for item in self.body_units)):
            raise ValueError(
                "each center body unit must overlap the frozen center core"
            )
        if self.entry_unit is None:
            raise ValueError("physical center entry is missing")
        if not self._overlaps_core(self.entry_unit):
            raise ValueError("entry unit must positively overlap center core")
        if establishment_leave is None:
            raise ValueError("physical center leave is missing")
        if not self._overlaps_core(establishment_leave):
            raise ValueError("establishment leave must positively overlap center core")
        if not self._outside_in_direction(establishment_leave):
            raise ValueError("establishment leave endpoint must be outside center core")
        establishment_ids = tuple(
            (
                item.unit_id
                for item in (self.entry_unit, *self.initial_units, establishment_leave)
            )
        )
        if len(establishment_ids) != 5 or len(set(establishment_ids)) != 5:
            raise ValueError("physical center requires five unique establishment roles")
        if len({item.unit_id for item in self.body_units}) != len(self.body_units):
            raise ValueError("center body unit ids must be unique")
        if any(
            (
                current.market_start < previous.market_end
                for (previous, current) in zip(self.body_units, self.body_units[1:])
            )
        ):
            raise ValueError("center body intervals must be time ordered")
        first = self.body_units[0]
        if self.entry_unit is not None:
            if self.entry_unit.end_tick != first.start_tick:
                raise ValueError("center entry must connect to first core unit")
            if first.market_start < self.entry_unit.market_end:
                raise ValueError("center body cannot overlap external entry")
        if self.body_start_market_time != self.body_units[0].market_start:
            raise ValueError("body start time must equal first body unit")
        maturity_unit = self.establishment_leave_unit
        if maturity_unit is None:
            raise ValueError("physical center maturity evidence is missing")
        if self.established_market_time != maturity_unit.market_end:
            raise ValueError(
                "established market time must equal final initial body unit end"
            )
        if self.established_at != maturity_unit.confirmed_at:
            raise ValueError(
                "established_at must equal final initial body unit confirmation"
            )
        if self.established_at is None:
            raise ValueError("established center requires a confirmed maturity unit")
        if self.last_touch_market_time != self.body_units[-1].market_end:
            raise ValueError("last touch time must equal final body unit end")
        if self.available_at < self.established_at:
            raise ValueError("available_at must not precede established_at")
        center_evidence = (
            (() if self.entry_unit is None else (self.entry_unit,))
            + self.body_units
            + self.failed_departure_units
            + (
                ()
                if self.establishment_leave_unit is None
                else (self.establishment_leave_unit,)
            )
        )
        if self.available_at < max((item.available_at for item in center_evidence)):
            raise ValueError("center availability must cover body evidence")
        if self.body_revision != len(self.extension_units):
            raise ValueError("body_revision must equal extension unit count")
        for terminal in (
            self.establishment_leave_unit,
            *self.failed_departure_units,
            self.pending_leave_unit,
            self.completion_leave_unit,
            self.completion_return_unit,
        ):
            if terminal is not None and (
                terminal.structural_level != self.structural_level
                or terminal.source_kind is not self.source_kind
            ):
                raise ValueError("center lifecycle unit level/source mismatch")
            if (
                terminal is not None
                and terminal.price_basis_revision != self.price_basis_revision
            ):
                raise ValueError("center lifecycle unit price basis mismatch")
        if any((not item.locked for item in self.failed_departure_units)):
            raise ValueError("failed departure history must be locked")
        if any(
            (
                not self._touches_core(item) or not self._outside_in_direction(item)
                for item in self.failed_departure_units
            )
        ):
            raise ValueError("failed departure history has invalid leave geometry")
        if set((item.unit_id for item in self.failed_departure_units)) & set(
            (item.unit_id for item in self.body_units)
        ):
            raise ValueError("failed departures must stay outside center body")
        if self.entry_unit is not None and self.entry_unit.unit_id in {
            item.unit_id for item in self.failed_departure_units
        }:
            raise ValueError("center entry cannot be failed departure history")

        def validate_pending_leave() -> None:
            pending = self.pending_leave_unit
            if pending is None:
                return
            if self.available_at < pending.available_at:
                raise ValueError(
                    "center availability must cover pending leave evidence"
                )
            if pending in self.body_units and (not self._is_seed_exit(pending)):
                raise ValueError("pending leave must stay outside center body")
            if not pending.locked:
                raise ValueError("pending leave must be locked")
            if not self._touches_core(pending):
                raise ValueError("pending leave must touch center core")
            if not self._outside_in_direction(pending):
                raise ValueError("pending leave endpoint must be outside center core")
            self._validate_external_leave(pending)

        def validate_completion() -> None:
            leave = self.completion_leave_unit
            ret = self.completion_return_unit
            if leave is None or ret is None or self.completed_at is None:
                raise ValueError(
                    "completed center requires leave, return and completed_at"
                )
            if leave in self.body_units and (not self._is_seed_exit(leave)):
                raise ValueError("completion leave must stay outside center body")
            if ret in self.body_units:
                raise ValueError("completion return must not enter center body")
            if not leave.locked or not ret.locked:
                raise ValueError("completion evidence must be locked")
            if not self._touches_core(leave) or not self._outside_in_direction(leave):
                raise ValueError("completion leave geometry is invalid")
            self._validate_external_leave(leave)
            if leave.direction == ret.direction:
                raise ValueError("completion return must alternate with leave")
            if leave.end_tick != ret.start_tick:
                raise ValueError("completion return must connect to leave")
            if ret.market_start < leave.market_end:
                raise ValueError("completion return cannot overlap leave")
            departure_direction = self.departure_direction(leave)
            valid_return = ret.is_return_from(
                departure_direction
            ) and return_outside_core(
                direction=departure_direction,
                low_tick=ret.low_tick,
                high_tick=ret.high_tick,
                zd_tick=self.zd_tick,
                zg_tick=self.zg_tick,
            )
            if not valid_return:
                raise ValueError("completion return must stay outside center core")
            if self.completed_at != ret.confirmed_at:
                raise ValueError(
                    "completed_at must equal completion return confirmation"
                )
            if self.available_at < max(
                self.completed_at, leave.available_at, ret.available_at
            ):
                raise ValueError("center availability must cover completion evidence")

        has_supersession = (
            self.superseded_by_center_id is not None
            or self.superseded_at is not None
            or bool(self.supersession_bridge_units)
        )
        if self.state not in (CenterState.SUPERSEDED,) and has_supersession:
            raise ValueError("only a superseded center may carry successor evidence")
        if self.state is CenterState.ONGOING:
            if (
                self.completion_leave_unit is not None
                or self.completion_return_unit is not None
                or self.completed_at is not None
            ):
                raise ValueError("ongoing center cannot retain completion evidence")
            validate_pending_leave()
        elif self.state is CenterState.COMPLETED:
            if self.pending_leave_unit is not None:
                raise ValueError("completed center cannot retain pending leave")
            validate_completion()
        elif self.state in (CenterState.SUPERSEDED,):
            if (
                self.pending_leave_unit is not None
                or self.completion_leave_unit is not None
                or self.completion_return_unit is not None
                or (self.completed_at is not None)
            ):
                raise ValueError(
                    "superseded center cannot fabricate third-class completion"
                )
            if (
                not self.superseded_by_center_id
                or self.superseded_by_center_id == self.center_id
                or self.superseded_at is None
            ):
                raise ValueError(
                    "superseded center requires a distinct successor and close time"
                )
            if self.superseded_at < self.established_at:
                raise ValueError("superseded_at cannot precede center establishment")
            if self.available_at < self.superseded_at:
                raise ValueError(
                    "superseded center availability must cover successor evidence"
                )
            bridge = self.supersession_bridge_units
            if any(
                (
                    item.structural_level != self.structural_level
                    or item.source_kind is not self.source_kind
                    or item.price_basis_revision != self.price_basis_revision
                    or (not item.locked)
                    for item in bridge
                )
            ):
                raise ValueError("supersession bridge context is incompatible")
            if {item.unit_id for item in bridge} & {
                item.unit_id
                for item in (*self.body_units, *self.failed_departure_units)
            }:
                raise ValueError(
                    "supersession bridge must stay outside body and failed history"
                )
            if bridge and self.available_at < max(
                (item.available_at for item in bridge)
            ):
                raise ValueError(
                    "superseded center availability must cover bridge evidence"
                )
        if establishment_leave is not None:
            lifecycle_owners = (
                *self.failed_departure_units,
                *self.supersession_bridge_units,
                *(
                    ()
                    if self.pending_leave_unit is None
                    else (self.pending_leave_unit,)
                ),
                *(
                    ()
                    if self.completion_leave_unit is None
                    else (self.completion_leave_unit,)
                ),
            )
            establishment_owners = tuple(
                (
                    item
                    for item in lifecycle_owners
                    if item.unit_id == establishment_leave.unit_id
                )
            )
            if establishment_owners != (establishment_leave,):
                raise ValueError(
                    "physical establishment leave must belong to exactly one lifecycle role"
                )
        transition_units = (
            *self.body_units,
            *self.failed_departure_units,
            *self.supersession_bridge_units,
            *(
                ()
                if self.pending_leave_unit is None
                or self._is_seed_exit(self.pending_leave_unit)
                else (self.pending_leave_unit,)
            ),
            *(
                ()
                if self.completion_leave_unit is None
                or self._is_seed_exit(self.completion_leave_unit)
                else (self.completion_leave_unit,)
            ),
            *(
                ()
                if self.completion_return_unit is None
                else (self.completion_return_unit,)
            ),
        )
        transition_ids = tuple((item.unit_id for item in transition_units))
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("center transition ownership must be unique")
        if self.entry_unit is not None and self.entry_unit.unit_id in set(
            transition_ids
        ):
            raise ValueError("center entry must stay outside transition ownership")
        ordered_transitions = tuple(
            sorted(
                transition_units,
                key=lambda item: (item.market_start, item.market_end, item.unit_id),
            )
        )
        if not ordered_transitions or ordered_transitions[0] != self.body_units[0]:
            raise ValueError(
                "center transition history must start at its first body unit"
            )
        body_ids = {item.unit_id for item in self.body_units}
        failed_ids = {item.unit_id for item in self.failed_departure_units}
        bridge_ids = {item.unit_id for item in self.supersession_bridge_units}
        if (
            tuple((item for item in ordered_transitions if item.unit_id in body_ids))
            != self.body_units
        ):
            raise ValueError("center body order conflicts with physical transitions")
        if (
            tuple((item for item in ordered_transitions if item.unit_id in failed_ids))
            != self.failed_departure_units
        ):
            raise ValueError(
                "failed departure order conflicts with physical transitions"
            )
        if (
            tuple((item for item in ordered_transitions if item.unit_id in bridge_ids))
            != self.supersession_bridge_units
        ):
            raise ValueError(
                "supersession bridge order conflicts with physical transitions"
            )
        physical_chain = (
            *((self.entry_unit,) if self.entry_unit is not None else ()),
            *ordered_transitions,
        )
        for previous, current in zip(physical_chain, physical_chain[1:]):
            pair_label = (
                "center body"
                if previous.unit_id in body_ids and current.unit_id in body_ids
                else "center transition"
            )
            if previous.direction == current.direction:
                raise ValueError(f"{pair_label} directions must alternate")
            if previous.end_tick != current.start_tick:
                raise ValueError(f"{pair_label} prices must connect")
            if current.market_start < previous.market_end:
                raise ValueError(f"{pair_label} intervals must not overlap")
        transition_offset = {
            item.unit_id: offset for (offset, item) in enumerate(ordered_transitions)
        }
        for failed in self.failed_departure_units:
            offset = transition_offset[failed.unit_id]
            if offset + 1 >= len(ordered_transitions):
                raise ValueError("failed departure requires its disproving return")
            ret = ordered_transitions[offset + 1]
            if not self._touches_core(ret):
                raise ValueError("failed departure return must re-enter center core")
        expected_center_id = build_center_id(
            price_basis_revision=self.price_basis_revision,
            structural_level=self.structural_level,
            source_kind=self.source_kind.value,
            entry_unit_id=None if self.entry_unit is None else self.entry_unit.unit_id,
            initial_unit_ids=tuple((item.unit_id for item in self.initial_units)),
            establishment_leave_unit_id=None
            if self.establishment_leave_unit is None
            else self.establishment_leave_unit.unit_id,
            zd_tick=self.zd_tick,
            zg_tick=self.zg_tick,
            formation_rule=self.formation_rule,
        )
        if self.center_id != expected_center_id:
            raise ValueError("center_id must match the immutable center seed")

    def _overlaps_core(self, item: ConstituentUnit) -> bool:
        left = max(item.low_tick, self.zd_tick)
        right = min(item.high_tick, self.zg_tick)
        return left < right

    def _touches_core(self, item: ConstituentUnit) -> bool:
        return max(item.low_tick, self.zd_tick) <= min(item.high_tick, self.zg_tick)

    def _outside_in_direction(self, item: ConstituentUnit) -> bool:
        return (
            item.end_tick > self.zg_tick
            if item.direction == "up"
            else item.end_tick < self.zd_tick
        )

    def departure_direction(self, item: ConstituentUnit) -> Direction:
        return item.direction

    def _validate_external_leave(self, leave: ConstituentUnit) -> None:
        if self._is_seed_exit(leave):
            return
        previous = max(
            (*self.body_units, *self.failed_departure_units),
            key=lambda item: (item.market_start, item.market_end, item.unit_id),
        )
        if (
            leave.end_tick == previous.end_tick
            and leave.market_end == previous.market_end
        ):
            raise ValueError("external leave must be distinct from center body")
        if previous.end_tick != leave.start_tick:
            raise ValueError("external leave must connect to center body")
        if leave.market_start < previous.market_end:
            raise ValueError("external leave cannot overlap center body in time")

    def _is_seed_exit(self, unit: ConstituentUnit) -> bool:
        """第 54 课共享末段的几何图例；正式三类点另按第 61 课校验。"""
        return False

    def has_independent_third_class_leave(self, leave: ConstituentUnit | None) -> bool:
        """Lesson 61: three core children, an independent leave, then return.

        The lesson 54 four-child drawing remains a geometric interpretation.
        It cannot supply a confirmed or approaching third-class signal under
        the later, explicit five-child convention.
        """
        return (
            leave is not None
            and leave.unit_id not in {unit.unit_id for unit in self.body_units}
            and (leave.market_start >= self.initial_units[-1].market_end)
        )

    @property
    def third_class_confirmed(self) -> bool:
        return self.physically_completed and self.has_independent_third_class_leave(
            self.completion_leave_unit
        )

    @property
    def core_units(self) -> tuple[ConstituentUnit, ConstituentUnit, ConstituentUnit]:
        return self.initial_units[:3]

    @property
    def core_body_start_market_time(self) -> datetime:
        return self.core_units[0].market_start

    @property
    def core_body_end_market_time(self) -> datetime:
        if self.completion_leave_unit is not None:
            if self._is_seed_exit(self.completion_leave_unit):
                return self.initial_units[-1].market_end
            return self.completion_leave_unit.market_start
        if self.pending_leave_unit is not None:
            if self._is_seed_exit(self.pending_leave_unit):
                return self.initial_units[-1].market_end
            return self.pending_leave_unit.market_start
        return self.body_units[-1].market_end

    @property
    def display_range_start_market_time(self) -> datetime:
        """返回可见中枢矩形的起点，仅包含中间三段核心。"""
        return self.core_body_start_market_time

    @property
    def display_range_end_market_time(self) -> datetime:
        """返回可见中枢本体的终点，不包含外部离开段。"""
        return self.core_body_end_market_time

    @property
    def maturity_unit(self) -> ConstituentUnit:
        """返回锁定后使中枢正式成立的不可变单元。"""
        if self.establishment_leave_unit is None:
            raise ValueError("physical center maturity evidence is missing")
        return self.establishment_leave_unit

    @property
    def initial_exit_unit(self) -> ConstituentUnit | None:
        """返回使物理中枢正式成立的独立离开段。"""
        return self.establishment_leave_unit

    @property
    def establishment_units(self) -> tuple[ConstituentUnit, ...]:
        """返回用于建立该中枢的精确来源窗口。"""
        if self.entry_unit is None or self.establishment_leave_unit is None:
            return ()
        return (self.entry_unit, *self.initial_units, self.establishment_leave_unit)

    @property
    def comparison_entry_unit(self) -> ConstituentUnit | None:
        """Entry comparison context; a shared seed swing adds no ownership."""
        entry = self.entry_unit
        return entry

    @property
    def lifecycle_leave_unit(self) -> ConstituentUnit | None:
        """返回当前归属于该中枢的外部离开段。"""
        return self.completion_leave_unit or self.pending_leave_unit

    @property
    def structurally_closed(self) -> bool:
        """Whether locked later structure prevents further center extension.

        A geometric leave/return and a disjoint successor center both close
        the old center structurally. A third-class signal additionally needs
        the independent departure roles checked by ``third_class_confirmed``.
        """
        return self.state in (CenterState.COMPLETED, CenterState.SUPERSEDED)

    @property
    def structural_closed_at(self) -> datetime | None:
        if self.state is CenterState.COMPLETED:
            return self.completed_at
        if self.state in (CenterState.SUPERSEDED,):
            return self.superseded_at
        return None

    @property
    def lifecycle_role_count(self) -> int:
        """统计本体、已证伪离开历史和当前外部离开单元。

        完成回返用于确认三类点，故意不计入中枢本体。已证伪离开保留为外部
        历史；真正回到核心的回返单元才进入 ``extension_units``。
        """
        roles = (
            *((self.entry_unit,) if self.entry_unit is not None else ()),
            *self.body_units,
            *self.failed_departure_units,
            *(
                ()
                if self.establishment_leave_unit is None
                else (self.establishment_leave_unit,)
            ),
            *(
                ()
                if self.lifecycle_leave_unit is None
                else (self.lifecycle_leave_unit,)
            ),
        )
        return len({item.unit_id for item in roles})

    @property
    def has_minimum_physical_roles(self) -> bool:
        """物理中枢必须具备进入、三段核心和独立离开五个角色。"""
        establishment_ids = tuple((item.unit_id for item in self.establishment_units))
        return len(establishment_ids) == 5 and len(set(establishment_ids)) == 5

    @property
    def completion_direction(self) -> Direction | None:
        if self.completion_leave_unit is None:
            return None
        return self.departure_direction(self.completion_leave_unit)

    @property
    def physically_completed(self) -> bool:
        """返回离开段及外部首次回返是否均已确认。"""
        return (
            self.completion_leave_unit is not None
            and self.completion_return_unit is not None
            and (self.completed_at is not None)
        )

    @property
    def completion_available_at(self) -> datetime | None:
        """Return the first availability of the confirmed leave and return geometry."""
        leave = self.completion_leave_unit
        ret = self.completion_return_unit
        if leave is None or ret is None or self.completed_at is None:
            return None
        evidence = (
            *(() if self.entry_unit is None else (self.entry_unit,)),
            *self.establishment_units,
            *self.body_units,
            *self.failed_departure_units,
            leave,
            ret,
        )
        return max(self.completed_at, *(item.available_at for item in evidence))

    @property
    def tradable(self) -> bool:
        return self.source_kind is not SourceKind.STROKE_OBSERVATION


@dataclass(frozen=True, slots=True)
class CenterPreview:
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    entry_unit_id: str | None
    unit_ids: tuple[str, ...]
    state: CenterPreviewState
    zd_tick: int | None
    zg_tick: int | None
    available_at: datetime
    failed_departure_unit_ids: tuple[str, ...] = ()
    pending_leave_unit_id: str | None = None
    completion_leave_unit_id: str | None = None
    completion_return_unit_id: str | None = None
    establishment_leave_unit_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "state", CenterPreviewState(self.state))
        object.__setattr__(self, "unit_ids", tuple(self.unit_ids))
        object.__setattr__(
            self, "failed_departure_unit_ids", tuple(self.failed_departure_unit_ids)
        )
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be >= 0")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        if not self.unit_ids:
            raise ValueError("preview must reference at least one body unit")
        if len(set(self.unit_ids)) != len(self.unit_ids):
            raise ValueError("preview unit ids must be unique")
        if self.entry_unit_id is not None and self.entry_unit_id in self.unit_ids:
            raise ValueError("preview entry must stay outside its body")
        if len(set(self.failed_departure_unit_ids)) != len(
            self.failed_departure_unit_ids
        ):
            raise ValueError("preview failed departure ids must be unique")
        if set(self.failed_departure_unit_ids) & set(self.unit_ids):
            raise ValueError("preview failed departures must stay outside its body")
        if self.failed_departure_unit_ids and (
            len(self.unit_ids) < 3
            or self.zd_tick is None
            or self.zg_tick is None
            or (self.state is CenterPreviewState.TOUCH_ONLY)
        ):
            raise ValueError(
                "preview failed departures require a source-valid center core"
            )
        if (self.zd_tick is None) != (self.zg_tick is None):
            raise ValueError("preview core ticks must be both present or both absent")
        if self.state is CenterPreviewState.TOUCH_ONLY and (
            self.zd_tick is None or self.zd_tick != self.zg_tick
        ):
            raise ValueError("touch-only preview requires a zero-width core")
        if (
            self.state is CenterPreviewState.FORMING
            and self.zd_tick is not None
            and (self.zd_tick >= self.zg_tick)
        ):
            raise ValueError("forming preview core violates source overlap contract")
        if self.state is CenterPreviewState.COMPLETED:
            if (
                len(self.unit_ids) < 3
                or self.zd_tick is None
                or self.zg_tick is None
                or (self.zd_tick >= self.zg_tick)
            ):
                raise ValueError(
                    "completed preview requires a source-valid center core"
                )
            if not self.completion_leave_unit_id:
                raise ValueError("completed preview requires an external leave unit")
            if not self.completion_return_unit_id:
                raise ValueError("completed preview requires a distinct return unit")
            if self.pending_leave_unit_id is not None:
                raise ValueError("completed preview cannot retain a pending leave")
        elif (
            self.completion_leave_unit_id is not None
            or self.completion_return_unit_id is not None
        ):
            raise ValueError("non-completed preview cannot retain completion evidence")
        lifecycle_ids = tuple(
            (
                value
                for value in (
                    self.pending_leave_unit_id,
                    self.completion_leave_unit_id,
                    self.completion_return_unit_id,
                )
                if value is not None
            )
        )
        if len(set(lifecycle_ids)) != len(lifecycle_ids):
            raise ValueError("preview lifecycle unit ids must be distinct")
        if set(lifecycle_ids) & set(self.unit_ids):
            raise ValueError("preview lifecycle units must stay outside its body")
        if set(lifecycle_ids) & set(self.failed_departure_unit_ids):
            raise ValueError(
                "preview current lifecycle must stay outside failed history"
            )
        external_ids = (*self.failed_departure_unit_ids, *lifecycle_ids)
        if self.entry_unit_id is not None and self.entry_unit_id in external_ids:
            raise ValueError("preview entry and lifecycle units must be distinct")
        if self.establishment_leave_unit_id is not None:
            if self.entry_unit_id is None:
                raise ValueError("physical preview leave requires an entry")
            if self.establishment_leave_unit_id not in (
                *self.failed_departure_unit_ids,
                *lifecycle_ids,
            ):
                raise ValueError(
                    "physical preview establishment leave must own leave lifecycle"
                )

    @property
    def formal_center_id(self) -> str | None:
        """返回该预览锁定后将采用的正式中枢身份。

        触碰型预览没有有效价格区间，永远不能提升为正式中枢。其余预览与
        正式中枢共用完全相同的种子身份，盘中候选因此可以保留到确认阶段的
        精确中枢身份，供图表在确认后继续复用。
        """
        if (
            self.zd_tick is None
            or self.zg_tick is None
            or self.state is CenterPreviewState.TOUCH_ONLY
        ):
            return None
        if self.zd_tick >= self.zg_tick:
            return None
        if self.entry_unit_id is None or self.establishment_leave_unit_id is None:
            return None
        return build_center_id(
            price_basis_revision=self.price_basis_revision,
            structural_level=self.structural_level,
            source_kind=self.source_kind.value,
            entry_unit_id=self.entry_unit_id,
            initial_unit_ids=self.unit_ids[: center_seed_size(self.source_kind)],
            establishment_leave_unit_id=self.establishment_leave_unit_id,
            zd_tick=self.zd_tick,
            zg_tick=self.zg_tick,
        )


@dataclass(frozen=True, slots=True)
class CenterEvent:
    event_id: str
    kind: CenterEventKind
    center_id: str
    price_basis_revision: str
    market_time: datetime
    available_at: datetime
    leave_unit_id: str | None = None
    return_unit_id: str | None = None


@dataclass(frozen=True, slots=True)
class CenterEvidence:
    schema: str
    center_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    state: CenterState
    tradable: bool
    zd_tick: int
    zg_tick: int
    dd_tick: int
    gg_tick: int
    initial_unit_ids: tuple[str, ...]
    entry_unit_id: str | None
    establishment_leave_unit_id: str | None
    core_unit_ids: tuple[str, str, str]
    body_unit_ids: tuple[str, ...]
    extension_unit_ids: tuple[str, ...]
    failed_departure_unit_ids: tuple[str, ...]
    pending_leave_unit_id: str | None
    completion_leave_unit_id: str | None
    completion_return_unit_id: str | None
    body_start_market_time: datetime
    established_market_time: datetime
    established_at: datetime
    last_touch_market_time: datetime
    completed_at: datetime | None
    available_at: datetime
    body_revision: int
    superseded_by_center_id: str | None = None
    superseded_at: datetime | None = None
    supersession_bridge_unit_ids: tuple[str, ...] = ()

    @classmethod
    def from_center(cls, center: TrendCenter) -> "CenterEvidence":
        return cls(
            schema="chanlun-center",
            center_id=center.center_id,
            structural_level=center.structural_level,
            source_kind=center.source_kind,
            price_basis_revision=center.price_basis_revision,
            state=center.state,
            tradable=center.tradable,
            zd_tick=center.zd_tick,
            zg_tick=center.zg_tick,
            dd_tick=center.dd_tick,
            gg_tick=center.gg_tick,
            initial_unit_ids=tuple((item.unit_id for item in center.initial_units)),
            entry_unit_id=None
            if center.entry_unit is None
            else center.entry_unit.unit_id,
            establishment_leave_unit_id=None
            if center.establishment_leave_unit is None
            else center.establishment_leave_unit.unit_id,
            core_unit_ids=tuple((item.unit_id for item in center.core_units)),
            body_unit_ids=tuple((item.unit_id for item in center.body_units)),
            extension_unit_ids=tuple((item.unit_id for item in center.extension_units)),
            failed_departure_unit_ids=tuple(
                (item.unit_id for item in center.failed_departure_units)
            ),
            pending_leave_unit_id=None
            if center.pending_leave_unit is None
            else center.pending_leave_unit.unit_id,
            completion_leave_unit_id=None
            if center.completion_leave_unit is None
            else center.completion_leave_unit.unit_id,
            completion_return_unit_id=None
            if center.completion_return_unit is None
            else center.completion_return_unit.unit_id,
            body_start_market_time=center.body_start_market_time,
            established_market_time=center.established_market_time,
            established_at=center.established_at,
            last_touch_market_time=center.last_touch_market_time,
            completed_at=center.completed_at,
            available_at=center.available_at,
            body_revision=center.body_revision,
            superseded_by_center_id=center.superseded_by_center_id,
            superseded_at=center.superseded_at,
            supersession_bridge_unit_ids=tuple(
                (item.unit_id for item in center.supersession_bridge_units)
            ),
        )


@dataclass(frozen=True, slots=True)
class CenterLevelResult:
    structural_level: int
    price_basis_revision: str | None
    centers: tuple[TrendCenter, ...]
    previews: tuple[CenterPreview, ...]
    events: tuple[CenterEvent, ...]
    locked_unit_count: int
    replay_from: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "centers", tuple(self.centers))
        object.__setattr__(self, "previews", tuple(self.previews))
        object.__setattr__(self, "events", tuple(self.events))
        if len({center.center_id for center in self.centers}) != len(self.centers):
            raise ValueError("center level identities must be unique")
        for index, center in enumerate(self.centers):
            if center.state not in (CenterState.SUPERSEDED,):
                continue
            if index + 1 >= len(self.centers):
                raise ValueError("superseded center requires its successor snapshot")
            successor = self.centers[index + 1]
            if center.superseded_by_center_id != successor.center_id:
                raise ValueError(
                    "superseded center must reference the immediate successor"
                )
            if center.superseded_at != successor.established_at:
                raise ValueError("supersession time must equal successor establishment")
            boundary_unit = (
                center.supersession_bridge_units[-1]
                if center.supersession_bridge_units
                else center.body_units[-1]
            )
            if successor.entry_unit != boundary_unit:
                raise ValueError(
                    "successor entry must equal the supersession boundary unit"
                )
            if boundary_unit.end_tick != successor.body_units[0].start_tick:
                raise ValueError("supersession boundary prices must connect")
            if successor.body_units[0].market_start < boundary_unit.market_end:
                raise ValueError("supersession boundary intervals must not overlap")
            cores_overlap = not (
                successor.zd_tick > center.zg_tick or successor.zg_tick < center.zd_tick
            )
            if cores_overlap and center.state is CenterState.SUPERSEDED:
                raise ValueError(
                    "successor center core must stay outside the superseded core"
                )
        ongoing = [
            center for center in self.centers if center.state is CenterState.ONGOING
        ]
        if len(ongoing) > 1 or (ongoing and self.centers[-1] is not ongoing[0]):
            raise ValueError("only the terminal center may remain ongoing")
        forming = [
            preview
            for preview in self.previews
            if preview.state is CenterPreviewState.FORMING
        ]
        if len(forming) > 1:
            raise ValueError("only one forming center preview is allowed")
        if not ongoing:
            return
        active = ongoing[0]
        seed_width = center_seed_size(active.source_kind)
        active_seed = (
            None if active.entry_unit is None else active.entry_unit.unit_id,
            *(item.unit_id for item in active.initial_units[:seed_width]),
            None
            if active.establishment_leave_unit is None
            else active.establishment_leave_unit.unit_id,
        )
        active_completion_observed = any(
            (
                preview.state is CenterPreviewState.COMPLETED
                and (
                    preview.entry_unit_id,
                    *preview.unit_ids[:seed_width],
                    preview.establishment_leave_unit_id,
                )
                == active_seed
                for preview in self.previews
            )
        )
        shifted_live = tuple(
            (
                preview
                for preview in self.previews
                if preview.state
                in (CenterPreviewState.FORMING, CenterPreviewState.COMPLETED)
                and (
                    preview.entry_unit_id,
                    *preview.unit_ids[:seed_width],
                    preview.establishment_leave_unit_id,
                )
                != active_seed
            )
        )
        if not shifted_live or active_completion_observed:
            return
        if any(
            (preview.state is CenterPreviewState.COMPLETED for preview in shifted_live)
        ):
            raise ValueError(
                "shifted completed preview cannot displace an unresolved active-center extension"
            )
        if forming:
            raise ValueError(
                "shifted forming preview cannot displace an unresolved active-center extension"
            )
