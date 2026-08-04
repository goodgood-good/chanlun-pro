from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from chanlun.core.strict_structure.identity import stable_structure_id


Direction = Literal["up", "down"]
StrictPointType = Literal["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"]


class SourceKind(str, Enum):
    SEGMENT = "segment"
    TREND_TYPE = "trend_type"
    STROKE_OBSERVATION = "stroke_observation"


class CenterState(str, Enum):
    ONGOING = "ongoing"
    COMPLETED = "completed"


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


class CenterRelation(str, Enum):
    UP_TREND = "up_trend"
    DOWN_TREND = "down_trend"
    UPGRADE = "upgrade"


class TrendKind(str, Enum):
    CONSOLIDATION = "consolidation"
    TREND = "trend"


class TrendState(str, Enum):
    FORMING = "forming"
    COMPLETE = "complete"
    LOCKED = "locked"


class StrictPointStatus(str, Enum):
    APPROACHING = "approaching"
    CONFIRMED = "confirmed"


class StrictPointVariant(str, Enum):
    STANDARD = "standard"
    STRICT = "strict"
    WEAK_DIVERGENCE = "weak_divergence"
    BOUNDARY_TOUCH = "boundary_touch"


@dataclass(frozen=True, slots=True)
class ConstituentUnit:
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
        if any(type(tick) is not int for tick in ticks):
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
        if type(self.locked) is not bool:
            raise TypeError("locked must be a bool")
        if self.locked != (self.confirmed_at is not None):
            raise ValueError("locked and confirmed_at must agree")
        if self.confirmed_at is not None and self.confirmed_at < self.market_end:
            raise ValueError("confirmed_at must not precede market_end")
        if self.available_at < self.market_end:
            raise ValueError("available_at must not precede market_end")
        if self.confirmed_at is not None and self.available_at < self.confirmed_at:
            raise ValueError("available_at must not precede confirmed_at")

        child_ids = tuple(self.child_ids)
        if any(not isinstance(child_id, str) or not child_id for child_id in child_ids):
            raise ValueError("child_ids must contain non-empty strings")
        object.__setattr__(self, "child_ids", child_ids)


@dataclass(frozen=True, slots=True)
class TrendCenter:
    center_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    state: CenterState
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

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "state", CenterState(self.state))
        object.__setattr__(self, "initial_units", tuple(self.initial_units))
        object.__setattr__(self, "body_units", tuple(self.body_units))
        object.__setattr__(self, "extension_units", tuple(self.extension_units))

        if not self.center_id:
            raise ValueError("center_id is required")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be >= 0")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        expected_seed_size = (
            3 if self.source_kind is SourceKind.TREND_TYPE else 5
        )
        if len(self.initial_units) != expected_seed_size:
            label = "three" if expected_seed_size == 3 else "five"
            raise ValueError(
                f"initial_units must contain exactly {label} units"
            )
        if self.body_units != self.initial_units + self.extension_units:
            raise ValueError("body units must equal initial plus extension units")
        if any(
            item.structural_level != self.structural_level
            or item.source_kind is not self.source_kind
            for item in self.body_units
        ):
            raise ValueError("center body level/source mismatch")
        if any(
            item.price_basis_revision != self.price_basis_revision
            for item in self.body_units
        ):
            raise ValueError("center body price basis mismatch")
        if any(not item.locked for item in self.body_units):
            raise ValueError("formal center body units must be locked")

        expected_zd = max(item.low_tick for item in self.core_units)
        expected_zg = min(item.high_tick for item in self.core_units)
        if (self.zd_tick, self.zg_tick) != (expected_zd, expected_zg):
            raise ValueError("center core must equal middle-three intersection")
        expected_dd = min(item.low_tick for item in self.body_units)
        expected_gg = max(item.high_tick for item in self.body_units)
        if (self.dd_tick, self.gg_tick) != (expected_dd, expected_gg):
            raise ValueError("center envelope must equal body envelope")
        if self.zd_tick >= self.zg_tick:
            raise ValueError("zd_tick must be < zg_tick")
        if self.dd_tick > self.zd_tick or self.gg_tick < self.zg_tick:
            raise ValueError("envelope must contain the core")
        if not self._positive_overlap(self.entry_unit):
            raise ValueError("entry unit must positively overlap center core")
        if not self._positive_overlap(self.initial_exit_unit):
            raise ValueError("initial exit unit must positively overlap center core")

        if len({item.unit_id for item in self.body_units}) != len(self.body_units):
            raise ValueError("center body unit ids must be unique")
        for previous, current in zip(self.body_units, self.body_units[1:]):
            # 线段恒有方向，故线段中枢的构成段必然交替。走势类型中枢不同：盘整
            # 没有方向，「上涨—盘整—上涨」是合法构成，此处无法分辨盘整，故把
            # 交替判定留给持有 oscillatory_ids 的 center_machine。
            if (
                self.source_kind is SourceKind.SEGMENT
                and previous.direction == current.direction
            ):
                raise ValueError("center body directions must alternate")
            if previous.end_tick != current.start_tick:
                raise ValueError("center body prices must connect")
            if current.market_start < previous.market_end:
                raise ValueError("center body intervals must not overlap")

        if self.body_start_market_time != self.body_units[0].market_start:
            raise ValueError("body start time must equal first body unit")
        if self.established_market_time != self.initial_exit_unit.market_end:
            raise ValueError("established market time must equal initial exit end")
        if self.established_at != self.initial_exit_unit.confirmed_at:
            raise ValueError("established_at must equal initial exit confirmation")
        if self.established_at is None:
            raise ValueError("established center requires confirmed initial exit")
        if self.last_touch_market_time != self.body_units[-1].market_end:
            raise ValueError("last touch time must equal final body unit end")
        if self.available_at < self.established_at:
            raise ValueError("available_at must not precede established_at")
        if self.available_at < max(item.available_at for item in self.body_units):
            raise ValueError("center availability must cover body evidence")
        if self.body_revision != len(self.extension_units):
            raise ValueError("body_revision must equal extension unit count")

        for terminal in (
            self.pending_leave_unit,
            self.completion_leave_unit,
            self.completion_return_unit,
        ):
            if terminal is not None and (
                terminal.structural_level != self.structural_level
                or terminal.source_kind is not self.source_kind
            ):
                raise ValueError("center lifecycle unit level/source mismatch")
            if terminal is not None and (
                terminal.price_basis_revision != self.price_basis_revision
            ):
                raise ValueError("center lifecycle unit price basis mismatch")

        if self.state is CenterState.ONGOING:
            if (
                self.completion_leave_unit is not None
                or self.completion_return_unit is not None
                or self.completed_at is not None
            ):
                raise ValueError("ongoing center cannot retain completion evidence")
            if self.pending_leave_unit is not None:
                if self.pending_leave_unit is not self.body_units[-1]:
                    raise ValueError("pending leave must be the final body unit")
                if not self.pending_leave_unit.locked:
                    raise ValueError("pending leave must be locked")
                if not self._positive_overlap(self.pending_leave_unit):
                    raise ValueError("pending leave must positively overlap center core")
                if not self._outside_in_direction(self.pending_leave_unit):
                    raise ValueError("pending leave endpoint must be outside center core")
                # 五个严格交替单元的第0位与第4位必然同向，故该条对线段中枢是
                # 恒真的派生结论。走势类型层允许「上涨进入、盘整、下跌离开」的
                # 反转中枢，此处不再强制；真正的几何约束由上面的核心重叠与
                # 离开端点在核心之外两条保证。
                if (
                    self.source_kind is SourceKind.SEGMENT
                    and self.pending_leave_unit.direction != self.entry_unit.direction
                ):
                    raise ValueError("pending leave direction must match center entry")
        else:
            if self.pending_leave_unit is not None:
                raise ValueError("completed center cannot retain pending leave")
            leave_unit = self.completion_leave_unit
            return_unit = self.completion_return_unit
            if leave_unit is None or return_unit is None or self.completed_at is None:
                raise ValueError("completed center requires leave, return and completed_at")
            if leave_unit is not self.body_units[-1]:
                raise ValueError("completion leave must be the final body unit")
            if return_unit in self.body_units:
                raise ValueError("completion return must not enter center body")
            if not leave_unit.locked or not return_unit.locked:
                raise ValueError("completion evidence must be locked")
            if not self._positive_overlap(leave_unit) or not self._outside_in_direction(
                leave_unit
            ):
                raise ValueError("completion leave geometry is invalid")
            if (
                self.source_kind is SourceKind.SEGMENT
                and leave_unit.direction != self.entry_unit.direction
            ):
                raise ValueError("completion leave direction must match center entry")
            if (
                self.source_kind is SourceKind.SEGMENT
                and leave_unit.direction == return_unit.direction
            ):
                raise ValueError("completion return must alternate with leave")
            if leave_unit.end_tick != return_unit.start_tick:
                raise ValueError("completion return must connect to leave")
            if return_unit.market_start < leave_unit.market_end:
                raise ValueError("completion return cannot overlap leave")
            if leave_unit.direction == "up":
                valid_return = return_unit.direction == "down" and return_unit.low_tick >= self.zg_tick
            else:
                valid_return = return_unit.direction == "up" and return_unit.high_tick <= self.zd_tick
            if not valid_return:
                raise ValueError("completion return must stay outside center core")
            if self.completed_at != return_unit.confirmed_at:
                raise ValueError("completed_at must equal completion return confirmation")
            if self.available_at < max(
                self.completed_at,
                leave_unit.available_at,
                return_unit.available_at,
            ):
                raise ValueError("center availability must cover completion evidence")

    def _positive_overlap(self, item: ConstituentUnit) -> bool:
        return max(item.low_tick, self.zd_tick) < min(item.high_tick, self.zg_tick)

    def _outside_in_direction(self, item: ConstituentUnit) -> bool:
        return (
            item.end_tick > self.zg_tick
            if item.direction == "up"
            else item.end_tick < self.zd_tick
        )

    @property
    def entry_unit(self) -> ConstituentUnit:
        return self.initial_units[0]

    @property
    def core_units(
        self,
    ) -> tuple[ConstituentUnit, ConstituentUnit, ConstituentUnit]:
        # A center made from already-completed lower-level trend types is
        # established by the overlap of those three consecutive trend types
        # themselves (L33/L38).  Segment-sourced level zero keeps the existing
        # five-unit confirmation envelope and therefore uses its middle three.
        if self.source_kind is SourceKind.TREND_TYPE:
            return self.initial_units  # type: ignore[return-value]
        return self.initial_units[1:4]  # type: ignore[return-value]

    @property
    def core_body_start_market_time(self) -> datetime:
        return self.core_units[0].market_start

    @property
    def core_body_end_market_time(self) -> datetime:
        if self.completion_leave_unit is not None:
            return self.completion_leave_unit.market_start
        if self.pending_leave_unit is not None:
            return self.pending_leave_unit.market_start
        if self.extension_units:
            return self.body_units[-1].market_end
        return self.initial_exit_unit.market_start

    @property
    def initial_exit_unit(self) -> ConstituentUnit:
        return self.initial_units[-1]

    @property
    def completion_direction(self) -> Direction | None:
        if self.completion_leave_unit is None:
            return None
        return self.completion_leave_unit.direction

    @property
    def tradable(self) -> bool:
        return self.source_kind is not SourceKind.STROKE_OBSERVATION


@dataclass(frozen=True, slots=True)
class CenterPreview:
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    unit_ids: tuple[str, ...]
    state: CenterPreviewState
    zd_tick: int | None
    zg_tick: int | None
    available_at: datetime
    completion_return_unit_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "state", CenterPreviewState(self.state))
        object.__setattr__(self, "unit_ids", tuple(self.unit_ids))
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be >= 0")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        if not self.unit_ids:
            raise ValueError("preview must reference at least one body unit")
        if len(set(self.unit_ids)) != len(self.unit_ids):
            raise ValueError("preview unit ids must be unique")
        if (self.zd_tick is None) != (self.zg_tick is None):
            raise ValueError("preview core ticks must be both present or both absent")
        if self.state is CenterPreviewState.TOUCH_ONLY and (
            self.zd_tick is None or self.zd_tick != self.zg_tick
        ):
            raise ValueError("touch-only preview requires a zero-width core")
        if (
            self.state is CenterPreviewState.FORMING
            and self.zd_tick is not None
            and self.zd_tick >= self.zg_tick
        ):
            raise ValueError("forming preview core must have positive width")
        if self.state is CenterPreviewState.COMPLETED:
            required = 3 if self.source_kind is SourceKind.TREND_TYPE else 5
            if (
                len(self.unit_ids) < required
                or self.zd_tick is None
                or self.zg_tick is None
                or self.zd_tick >= self.zg_tick
            ):
                raise ValueError(
                    "completed preview requires a positive source-specific core"
                )
            if (
                not self.completion_return_unit_id
                or self.completion_return_unit_id in self.unit_ids
            ):
                raise ValueError("completed preview requires a distinct return unit")
        elif self.completion_return_unit_id is not None:
            raise ValueError("non-completed preview cannot retain a return unit")


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
    schema_version: str
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
    entry_unit_id: str
    core_unit_ids: tuple[str, str, str]
    initial_exit_unit_id: str
    body_unit_ids: tuple[str, ...]
    extension_unit_ids: tuple[str, ...]
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

    @classmethod
    def from_center(cls, center: TrendCenter) -> "CenterEvidence":
        return cls(
            schema_version="chanlun-center/v3",
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
            initial_unit_ids=tuple(item.unit_id for item in center.initial_units),
            entry_unit_id=center.entry_unit.unit_id,
            core_unit_ids=tuple(item.unit_id for item in center.core_units),
            initial_exit_unit_id=center.initial_exit_unit.unit_id,
            body_unit_ids=tuple(item.unit_id for item in center.body_units),
            extension_unit_ids=tuple(
                item.unit_id for item in center.extension_units
            ),
            pending_leave_unit_id=(
                None
                if center.pending_leave_unit is None
                else center.pending_leave_unit.unit_id
            ),
            completion_leave_unit_id=(
                None
                if center.completion_leave_unit is None
                else center.completion_leave_unit.unit_id
            ),
            completion_return_unit_id=(
                None
                if center.completion_return_unit is None
                else center.completion_return_unit.unit_id
            ),
            body_start_market_time=center.body_start_market_time,
            established_market_time=center.established_market_time,
            established_at=center.established_at,
            last_touch_market_time=center.last_touch_market_time,
            completed_at=center.completed_at,
            available_at=center.available_at,
            body_revision=center.body_revision,
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

        ongoing = [
            center
            for center in self.centers
            if center.state is CenterState.ONGOING
        ]
        if len(ongoing) > 1 or (
            ongoing and self.centers[-1] is not ongoing[0]
        ):
            raise ValueError("only the terminal center may remain ongoing")

        forming = [
            preview
            for preview in self.previews
            if preview.state is CenterPreviewState.FORMING
        ]
        if len(forming) > 1:
            raise ValueError("only one forming center preview is allowed")
        if not ongoing or not forming:
            return

        active = ongoing[0]
        seed_width = len(active.initial_units)
        active_seed = tuple(item.unit_id for item in active.initial_units)
        forming_seed = tuple(forming[0].unit_ids[:seed_width])
        if forming_seed == active_seed:
            return
        active_completion_observed = any(
            preview.state is CenterPreviewState.COMPLETED
            and tuple(preview.unit_ids[:seed_width]) == active_seed
            for preview in self.previews
        )
        if not active_completion_observed:
            raise ValueError(
                "shifted forming preview cannot displace an unresolved "
                "active-center extension"
            )


@dataclass(frozen=True, slots=True)
class TrendType:
    trend_id: str
    structural_level: int
    price_basis_revision: str
    kind: TrendKind
    direction: Direction
    state: TrendState
    centers: tuple[TrendCenter, ...]
    constituent_units: tuple[ConstituentUnit, ...]
    start_tick: int
    end_tick: int
    low_tick: int
    high_tick: int
    market_start: datetime
    market_end: datetime
    confirmed_at: datetime | None
    available_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", TrendKind(self.kind))
        object.__setattr__(self, "state", TrendState(self.state))
        object.__setattr__(self, "centers", tuple(self.centers))
        object.__setattr__(self, "constituent_units", tuple(self.constituent_units))

        if not self.trend_id:
            raise ValueError("trend_id is required")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be non-negative")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        if self.direction not in ("up", "down"):
            raise ValueError("direction must be up or down")
        if self.state is TrendState.FORMING and self.confirmed_at is not None:
            raise ValueError("forming trend cannot carry confirmed_at")
        if (
            self.state in (TrendState.COMPLETE, TrendState.LOCKED)
            and self.confirmed_at is None
        ):
            raise ValueError("completed trend requires confirmed_at")
        if not self.centers:
            raise ValueError("trend type requires at least one center")
        if not self.constituent_units:
            raise ValueError("trend type requires constituent units")

        if any(
            item.structural_level != self.structural_level
            for item in self.constituent_units
        ) or any(
            center.structural_level != self.structural_level
            for center in self.centers
        ):
            raise ValueError("trend level must match all evidence")
        source_kinds = {
            item.source_kind for item in self.constituent_units
        } | {center.source_kind for center in self.centers}
        if len(source_kinds) != 1:
            raise ValueError("trend source must match all evidence")
        if any(
            item.price_basis_revision != self.price_basis_revision
            for item in self.constituent_units
        ) or any(
            center.price_basis_revision != self.price_basis_revision
            for center in self.centers
        ):
            raise ValueError("trend cannot cross price basis")
        if len({item.unit_id for item in self.constituent_units}) != len(
            self.constituent_units
        ):
            raise ValueError("constituent units must be unique")
        if len({center.center_id for center in self.centers}) != len(self.centers):
            raise ValueError("trend centers must be unique")

        if self.kind is TrendKind.CONSOLIDATION and len(self.centers) != 1:
            raise ValueError("consolidation must contain exactly one center")
        if self.kind is TrendKind.TREND and len(self.centers) < 2:
            raise ValueError("trend must contain at least two centers")

        segment_sourced = all(
            item.source_kind is SourceKind.SEGMENT
            for item in self.constituent_units
        )
        for previous, current in zip(
            self.constituent_units,
            self.constituent_units[1:],
        ):
            # 同上：只有线段构成的走势类型才强制交替；走势类型构成的高层走势
            # 允许盘整夹在两段同向趋势之间。
            if segment_sourced and previous.direction == current.direction:
                raise ValueError("trend constituent directions must alternate")
            if previous.end_tick != current.start_tick:
                raise ValueError("trend constituent prices must connect")
            if current.market_start < previous.market_end:
                raise ValueError("trend constituent intervals must not overlap")

        if self.state in (TrendState.COMPLETE, TrendState.LOCKED) and any(
            center.state is not CenterState.COMPLETED for center in self.centers
        ):
            raise ValueError("completed trend requires completed centers")
        if self.state in (TrendState.COMPLETE, TrendState.LOCKED) and any(
            not item.locked for item in self.constituent_units
        ):
            raise ValueError("completed trend requires locked constituent units")

        constituent_ids = {item.unit_id for item in self.constituent_units}
        for center_index, center in enumerate(self.centers):
            missing = tuple(
                item for item in center.body_units
                if item.unit_id not in constituent_ids
            )
            if not missing:
                continue
            shared_entry_is_external_boundary = (
                center_index == 0
                and missing == (center.entry_unit,)
                and len(center.initial_units) >= 2
                and self.constituent_units[0] is center.initial_units[1]
                and center.entry_unit.market_end
                == self.constituent_units[0].market_start
            )
            if not shared_entry_is_external_boundary:
                raise ValueError(
                    "trend must contain every center body unit except its shared entry boundary"
                )
        for center in self.centers[:-1]:
            if (
                center.completion_return_unit is not None
                and center.completion_return_unit.unit_id not in constituent_ids
            ):
                raise ValueError("internal completion return must remain in trend")
        terminal_leave = self.centers[-1].completion_leave_unit
        terminal_return = self.centers[-1].completion_return_unit
        if terminal_return is not None and terminal_return.unit_id in constituent_ids:
            raise ValueError("terminal completion return belongs to the next trend")
        if terminal_leave is not None and self.constituent_units[-1] != terminal_leave:
            raise ValueError("terminal unit must be the final leave unit")

        ticks = (self.start_tick, self.end_tick, self.low_tick, self.high_tick)
        if any(type(tick) is not int for tick in ticks):
            raise TypeError("trend ticks must be integers")
        first = self.constituent_units[0]
        last = self.constituent_units[-1]
        if (
            self.start_tick != first.start_tick
            or self.end_tick != last.end_tick
            or self.low_tick
            != min(item.low_tick for item in self.constituent_units)
            or self.high_tick
            != max(item.high_tick for item in self.constituent_units)
            or self.market_start != first.market_start
            or self.market_end != last.market_end
        ):
            raise ValueError("trend geometry must equal constituent envelope")
        expected_direction = (
            "up"
            if self.end_tick > self.start_tick
            else "down"
            if self.end_tick < self.start_tick
            else last.direction
        )
        if self.direction != expected_direction:
            raise ValueError("trend direction must match endpoints")

        if self.confirmed_at is not None and self.confirmed_at < self.market_end:
            raise ValueError("trend confirmation must not precede market end")
        completion_times = tuple(
            center.completed_at
            for center in self.centers
            if center.completed_at is not None
        )
        if (
            self.state in (TrendState.COMPLETE, TrendState.LOCKED)
            and completion_times
            and self.confirmed_at < max(completion_times)
        ):
            raise ValueError("trend confirmation must cover center completion")
        if self.confirmed_at is not None and self.available_at < self.confirmed_at:
            raise ValueError("available_at must not precede confirmed_at")
        evidence_availability = tuple(
            item.available_at for item in self.constituent_units
        ) + tuple(center.available_at for center in self.centers)
        if self.available_at < max(evidence_availability):
            raise ValueError("trend availability must cover all evidence")

    @property
    def locked(self) -> bool:
        return self.state is TrendState.LOCKED

    @property
    def complete(self) -> bool:
        return self.state in (TrendState.COMPLETE, TrendState.LOCKED)

    @property
    def terminal_unit(self) -> ConstituentUnit:
        return self.constituent_units[-1]


@dataclass(frozen=True, slots=True)
class TrendAssemblyResult:
    current_trends: tuple[TrendType, ...]
    completed_trends: tuple[TrendType, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_trends", tuple(self.current_trends))
        object.__setattr__(self, "completed_trends", tuple(self.completed_trends))
        if any(
            trend.state is not TrendState.COMPLETE
            for trend in self.completed_trends
        ):
            raise ValueError(
                "completed_trends must contain immutable COMPLETE snapshots"
            )
        if tuple(
            sorted(
                self.completed_trends,
                key=lambda trend: (trend.available_at, trend.trend_id),
            )
        ) != self.completed_trends:
            raise ValueError("completed_trends must be deterministically ordered")
        if len({trend.trend_id for trend in self.completed_trends}) != len(
            self.completed_trends
        ):
            raise ValueError("completed trend snapshots must be unique")


@dataclass(frozen=True, slots=True)
class StrictLevelResult:
    structural_level: int
    units: tuple[ConstituentUnit, ...]
    center_result: CenterLevelResult
    trend_types: tuple[TrendType, ...]
    completed_trends: tuple[TrendType, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", tuple(self.units))
        object.__setattr__(self, "trend_types", tuple(self.trend_types))
        object.__setattr__(self, "completed_trends", tuple(self.completed_trends))
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be non-negative")
        if self.center_result.structural_level != self.structural_level:
            raise ValueError("level result center level mismatch")
        if any(item.structural_level != self.structural_level for item in self.units):
            raise ValueError("level result unit level mismatch")
        if any(
            trend.structural_level != self.structural_level
            for trend in self.trend_types + self.completed_trends
        ):
            raise ValueError("level result trend level mismatch")


@dataclass(frozen=True, slots=True)
class StrictStructureResult:
    schema_version: str
    price_basis_revision: str
    levels: tuple[StrictLevelResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "levels", tuple(self.levels))
        if self.schema_version != "chanlun-structure/v3":
            raise ValueError("unsupported strict structure schema")
        if not self.price_basis_revision:
            raise ValueError("price_basis_revision is required")
        if tuple(level.structural_level for level in self.levels) != tuple(
            range(len(self.levels))
        ):
            raise ValueError("strict structure levels must be contiguous")
        if any(
            item.price_basis_revision != self.price_basis_revision
            for level in self.levels
            for item in level.units
        ):
            raise ValueError("strict structure cannot cross price basis")


@dataclass(frozen=True, slots=True)
class StrictEvidenceResult:
    symbol: str
    source_frequency: str
    source_closed_at: datetime
    price_basis_revision: str
    structure_price_quantum: Decimal
    strict_config_revision: str
    structure_revision: str
    structure: StrictStructureResult
    stroke_center_observations: CenterLevelResult
    confirmed_points: tuple[StrictPointEvidence, ...]
    approaching_points: tuple[StrictPointEvidence, ...]
    divergences: tuple[DivergenceEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "confirmed_points", tuple(self.confirmed_points))
        object.__setattr__(self, "approaching_points", tuple(self.approaching_points))
        object.__setattr__(self, "divergences", tuple(self.divergences))
        for value, label in (
            (self.symbol, "symbol"),
            (self.source_frequency, "source_frequency"),
            (self.price_basis_revision, "price_basis_revision"),
            (self.strict_config_revision, "strict_config_revision"),
            (self.structure_revision, "structure_revision"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} is required")
        if (
            not isinstance(self.structure_price_quantum, Decimal)
            or not self.structure_price_quantum.is_finite()
            or self.structure_price_quantum <= 0
        ):
            raise ValueError("structure_price_quantum must be a positive Decimal")
        if self.structure.price_basis_revision != self.price_basis_revision:
            raise ValueError("strict evidence structure price basis mismatch")
        if self.stroke_center_observations.price_basis_revision not in {
            None,
            self.price_basis_revision,
        }:
            raise ValueError("stroke observation price basis mismatch")
        if any(
            center.source_kind is not SourceKind.STROKE_OBSERVATION
            for center in self.stroke_center_observations.centers
        ):
            raise ValueError("stroke center observations must remain non-tradable")
        if any(
            point.status is not StrictPointStatus.CONFIRMED
            for point in self.confirmed_points
        ):
            raise ValueError("confirmed_points contains a non-confirmed point")
        if any(
            point.status is not StrictPointStatus.APPROACHING
            for point in self.approaching_points
        ):
            raise ValueError("approaching_points contains a non-approaching point")
        all_points = self.confirmed_points + self.approaching_points
        if any(
            point.price_basis_revision != self.price_basis_revision
            for point in all_points
        ):
            raise ValueError("strict evidence point price basis mismatch")
        if any(point.available_at > self.source_closed_at for point in all_points):
            raise ValueError("strict evidence contains future-visible point")
        if len({point.point_id for point in self.confirmed_points}) != len(
            self.confirmed_points
        ):
            raise ValueError("confirmed point ids must be unique")
        if len({point.point_id for point in self.approaching_points}) != len(
            self.approaching_points
        ):
            raise ValueError("approaching point ids must be unique")
        if len({item.divergence_id for item in self.divergences}) != len(
            self.divergences
        ):
            raise ValueError("divergence ids must be unique")
        structure_levels = {
            level.structural_level for level in self.structure.levels
        }
        if any(
            item.price_basis_revision != self.price_basis_revision
            for item in self.divergences
        ):
            raise ValueError("strict evidence divergence price basis mismatch")
        if any(
            item.structural_level not in structure_levels
            for item in self.divergences
        ):
            raise ValueError("strict evidence divergence level is unavailable")
        if any(
            item.source_kind is SourceKind.STROKE_OBSERVATION
            for item in self.divergences
        ):
            raise ValueError("stroke observations cannot produce formal divergence")
        if any(
            item.confirmed_at > self.source_closed_at
            or item.available_at > self.source_closed_at
            for item in self.divergences
        ):
            raise ValueError("strict evidence contains future-visible divergence")

        completed_keys = [
            (
                center.structural_level,
                center.center_id,
                center.source_kind,
                center.completion_direction,
                center.completion_return_unit.unit_id,
                center.completed_at,
                center.zd_tick,
                center.zg_tick,
            )
            for level in self.structure.levels
            for center in level.center_result.centers
            if center.state is CenterState.COMPLETED
            and center.source_kind is not SourceKind.STROKE_OBSERVATION
        ]
        third_keys = [
            (
                point.structural_level,
                point.center_id,
                point.source_kind,
                "up" if point.point_type == "3buy" else "down",
                point.anchor_unit_id,
                point.confirmed_at,
                point.center_zd_tick,
                point.center_zg_tick,
            )
            for point in self.confirmed_points
            if point.point_type in ("3buy", "3sell")
        ]
        if len(third_keys) != len(set(third_keys)):
            raise ValueError(
                "each completed center must have exactly one third-class point"
            )
        if set(completed_keys) != set(third_keys):
            raise ValueError(
                "completed centers and third-class points must match"
            )

    @property
    def formal_inputs(self) -> dict:
        return {
            "symbol": self.symbol,
            "source_frequency": self.source_frequency,
            "price_basis_revision": self.price_basis_revision,
            "strict_config_revision": self.strict_config_revision,
            "structure": self.structure,
            "confirmed_points": self.confirmed_points,
            "divergences": self.divergences,
        }


def build_strict_point_id(
    *,
    price_basis_revision: str,
    point_type: StrictPointType,
    structural_level: int,
    anchor_unit_id: str,
    center_id: str | None,
    parent_point_id: str | None,
) -> str:
    """Build the stable identity of a confirmed strict structural point."""

    if not price_basis_revision or not price_basis_revision.strip():
        raise ValueError("price_basis_revision is required")
    if point_type not in {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}:
        raise ValueError("unsupported strict point type")
    if type(structural_level) is not int or structural_level < 0:
        raise ValueError("structural_level must be non-negative")
    if not anchor_unit_id:
        raise ValueError("anchor_unit_id is required")
    return stable_structure_id(
        "chanlun-strict-point/v2",
        price_basis_revision,
        point_type,
        structural_level,
        anchor_unit_id,
        center_id,
        parent_point_id,
    )


@dataclass(frozen=True, slots=True)
class DivergenceEvidence:
    divergence_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    kind: Literal["trend", "consolidation"]
    direction: Direction
    compare_unit_id: str
    signal_unit_id: str
    anchor_at: datetime
    anchor_tick: int
    confirmed_at: datetime
    available_at: datetime
    price_extreme_confirmed: bool
    histogram_area_decayed: bool
    histogram_peak_decayed: bool
    dif_extreme_decayed: bool
    strength_source: Literal["macd_htf", "macd_native"]

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if not self.divergence_id:
            raise ValueError("divergence_id is required")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be non-negative")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        if self.kind not in ("trend", "consolidation"):
            raise ValueError("divergence kind must be trend or consolidation")
        if self.direction not in ("up", "down"):
            raise ValueError("divergence direction must be up or down")
        if not self.compare_unit_id or not self.signal_unit_id:
            raise ValueError("divergence unit ids are required")
        if self.compare_unit_id == self.signal_unit_id:
            raise ValueError("divergence units must be distinct")
        if type(self.anchor_tick) is not int:
            raise TypeError("divergence anchor_tick must be an integer")
        for value in (self.anchor_at, self.confirmed_at, self.available_at):
            if (
                not isinstance(value, datetime)
                or value.tzinfo is None
                or value.utcoffset() is None
            ):
                raise ValueError("divergence timestamps must be timezone-aware")
        if not self.anchor_at <= self.confirmed_at <= self.available_at:
            raise ValueError(
                "divergence timestamps must satisfy anchor <= confirmed <= available"
            )
        flags = (
            self.price_extreme_confirmed,
            self.histogram_area_decayed,
            self.histogram_peak_decayed,
            self.dif_extreme_decayed,
        )
        if any(type(flag) is not bool for flag in flags):
            raise TypeError("divergence conditions must be booleans")
        if self.strength_source not in ("macd_htf", "macd_native"):
            raise ValueError("unsupported divergence strength source")
        expected_id = stable_structure_id(
            "chanlun-strict-divergence/v3",
            self.price_basis_revision,
            self.structural_level,
            self.source_kind.value,
            self.kind,
            self.direction,
            self.compare_unit_id,
            self.signal_unit_id,
        )
        if self.divergence_id != expected_id:
            raise ValueError("divergence_id does not match formal evidence")

    @property
    def is_divergent(self) -> bool:
        return all(
            (
                self.price_extreme_confirmed,
                self.histogram_area_decayed,
                self.histogram_peak_decayed,
                self.dif_extreme_decayed,
            )
        )


@dataclass(frozen=True, slots=True)
class StrictPointEvidence:
    point_id: str
    point_type: StrictPointType
    side: Literal["buy", "sell"]
    status: StrictPointStatus
    variant: StrictPointVariant
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    anchor_unit_id: str
    anchor_at: datetime
    confirmed_at: datetime | None
    available_at: datetime
    price_quantum: Decimal
    anchor_tick: int
    invalidation_tick: int
    center_id: str | None
    center_zd_tick: int | None
    center_zg_tick: int | None
    center_ordinal: int | None
    divergence: DivergenceEvidence | None
    parent_point_id: str | None
    evidence_codes: tuple[str, ...]
    missing_conditions: tuple[str, ...] = ()
    related_point_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", StrictPointStatus(self.status))
        object.__setattr__(self, "variant", StrictPointVariant(self.variant))
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "evidence_codes", tuple(self.evidence_codes))
        object.__setattr__(self, "missing_conditions", tuple(self.missing_conditions))
        object.__setattr__(self, "related_point_ids", tuple(self.related_point_ids))

        if not self.point_id:
            raise ValueError("point_id is required")
        if self.point_type not in {
            "1buy",
            "2buy",
            "3buy",
            "1sell",
            "2sell",
            "3sell",
        }:
            raise ValueError("unsupported strict point type")
        expected_side = "buy" if self.point_type.endswith("buy") else "sell"
        if self.side != expected_side:
            raise ValueError("point_type and side disagree")
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be non-negative")
        if self.source_kind is SourceKind.STROKE_OBSERVATION:
            raise ValueError("stroke observation is not tradable")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        if not self.anchor_unit_id:
            raise ValueError("anchor_unit_id is required")
        if not isinstance(self.price_quantum, Decimal) or self.price_quantum <= 0:
            raise ValueError("price_quantum must be a positive Decimal")
        ticks = (
            self.anchor_tick,
            self.invalidation_tick,
            self.center_zd_tick,
            self.center_zg_tick,
            self.center_ordinal,
        )
        if any(tick is not None and type(tick) is not int for tick in ticks):
            raise TypeError("strict point ticks and ordinal must be integers")
        if self.anchor_at > self.available_at:
            raise ValueError("available_at must not precede anchor_at")

        if self.status is StrictPointStatus.CONFIRMED:
            if self.confirmed_at is None:
                raise ValueError("confirmed point requires confirmed_at")
            if self.confirmed_at < self.anchor_at:
                raise ValueError("confirmed_at must not precede anchor_at")
            if self.available_at < self.confirmed_at:
                raise ValueError("available_at must not precede confirmed_at")
            if self.missing_conditions:
                raise ValueError("confirmed point cannot have missing conditions")
        else:
            if self.confirmed_at is not None:
                raise ValueError("approaching point cannot carry confirmed_at")
            if not self.missing_conditions:
                raise ValueError("approaching point requires missing conditions")

        if self.side == "buy" and self.invalidation_tick > self.anchor_tick:
            raise ValueError("buy invalidation cannot be above anchor")
        if self.side == "sell" and self.invalidation_tick < self.anchor_tick:
            raise ValueError("sell invalidation cannot be below anchor")
        if (self.center_zd_tick is None) != (self.center_zg_tick is None):
            raise ValueError("center boundaries must be provided together")
        if (
            self.center_zd_tick is not None
            and self.center_zg_tick is not None
            and self.center_zd_tick >= self.center_zg_tick
        ):
            raise ValueError("center boundaries must have positive width")

        if (
            self.variant is StrictPointVariant.BOUNDARY_TOUCH
            and self.point_type not in {"3buy", "3sell"}
        ):
            raise ValueError("boundary touch requires third class")
        if self.variant is StrictPointVariant.WEAK_DIVERGENCE:
            if self.point_type not in {"2buy", "2sell"}:
                raise ValueError("weak divergence requires second class")
            if (
                self.divergence is None
                or self.divergence.kind != "consolidation"
                or not self.divergence.is_divergent
            ):
                raise ValueError(
                    "weak divergence requires confirmed consolidation divergence"
                )

        if self.point_type in {"1buy", "1sell"} and (
            self.variant is not StrictPointVariant.STANDARD
            or self.divergence is None
            or self.divergence.kind != "trend"
            or not self.divergence.is_divergent
        ):
            raise ValueError("first class requires standard trend divergence")
        if self.point_type in {"2buy", "2sell"}:
            if self.variant not in {
                StrictPointVariant.STRICT,
                StrictPointVariant.WEAK_DIVERGENCE,
            }:
                raise ValueError("second class requires strict or weak divergence variant")
            if self.parent_point_id is None:
                raise ValueError("second class requires parent point")
        if self.point_type in {"3buy", "3sell"} and self.variant not in {
            StrictPointVariant.STANDARD,
            StrictPointVariant.BOUNDARY_TOUCH,
        }:
            raise ValueError("third class variant is invalid")
        if self.point_type in {"3buy", "3sell"} and (
            self.center_id is None or self.center_ordinal is None
        ):
            raise ValueError("third class requires center ordinal")
        if self.point_type not in {"3buy", "3sell"} and self.center_ordinal is not None:
            raise ValueError("center ordinal is reserved for third class")
        if self.divergence is not None and self.available_at < self.divergence.available_at:
            raise ValueError("point availability must cover divergence")
        if self.center_ordinal is not None and self.center_ordinal <= 0:
            raise ValueError("center ordinal must be positive")

        for values, label in (
            (self.evidence_codes, "evidence codes"),
            (self.missing_conditions, "missing conditions"),
            (self.related_point_ids, "related point ids"),
        ):
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{label} must contain non-empty strings")
            if len(values) != len(set(values)):
                raise ValueError(f"{label} must be unique")
        if self.point_id in self.related_point_ids:
            raise ValueError("point cannot reference itself")

    @property
    def structure_anchor_price(self) -> Decimal:
        return self.price_quantum * self.anchor_tick

    @property
    def structure_invalidation_price(self) -> Decimal:
        return self.price_quantum * self.invalidation_tick
