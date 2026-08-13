from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from chanlun.core.strict_structure.identity import (
    build_center_id,
    build_strict_evidence_revision,
    build_trend_id,
    stable_structure_id,
)


Direction = Literal["up", "down"]
StrictPointType = Literal["1buy", "2buy", "3buy", "1sell", "2sell", "3sell"]


class SourceKind(str, Enum):
    SEGMENT = "segment"
    TREND_TYPE = "trend_type"
    STROKE_OBSERVATION = "stroke_observation"


def center_seed_size(source_kind: SourceKind) -> int:
    """返回中枢三段价格核心的宽度。

    ``initial_units`` 始终保存用于冻结 ``ZD/ZG`` 交集的三段。物理线段/笔中枢
    另有五段成立门槛（进入段 + 这三段 + 成熟段）；递归走势类型中枢则保留
    原始的三段已完成低级别走势。
    """

    SourceKind(source_kind)
    return 3


class CenterState(str, Enum):
    ONGOING = "ongoing"
    COMPLETED = "completed"
    DIVERGENCE_CLOSED = "divergence_closed"


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
    same_level_combination: bool = False
    protected_after_ids: tuple[str, ...] = ()

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
        if type(self.same_level_combination) is not bool:
            raise TypeError("same_level_combination must be a bool")
        protected_after_ids = tuple(self.protected_after_ids)
        object.__setattr__(self, "protected_after_ids", protected_after_ids)
        if any(
            not isinstance(child_id, str) or not child_id
            for child_id in protected_after_ids
        ):
            raise ValueError("protected_after_ids must contain non-empty strings")
        if len(set(protected_after_ids)) != len(protected_after_ids):
            raise ValueError("protected_after_ids must be unique")
        if self.same_level_combination:
            if self.source_kind is not SourceKind.TREND_TYPE or len(child_ids) < 2:
                raise ValueError(
                    "same-level combination requires at least two trend-type leaves"
                )
            if len(set(child_ids)) != len(child_ids):
                raise ValueError("same-level combination leaves must be unique")
            if not set(protected_after_ids).issubset(child_ids):
                raise ValueError("protected edges must reference combination leaves")
        elif protected_after_ids:
            raise ValueError("ordinary unit cannot carry same-level protected edges")


@dataclass(frozen=True, slots=True)
class TrendCenter:
    center_id: str
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    state: CenterState
    entry_unit: ConstituentUnit
    establishment_unit: ConstituentUnit | None
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
    boundary_divergence_id: str | None = None
    boundary_anchor_unit_id: str | None = None

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
        expected_initial_count = center_seed_size(self.source_kind)
        if len(self.initial_units) != expected_initial_count:
            raise ValueError(
                "initial_units must contain the source-specific center body"
            )
        if self.body_units != self.initial_units + self.extension_units:
            raise ValueError("body units must equal initial plus extension units")
        if not isinstance(self.entry_unit, ConstituentUnit):
            raise ValueError("center requires an external entry unit")
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

        establishment_unit = self.establishment_unit
        initial_leave = self.establishment_leave_unit
        if self.source_kind is SourceKind.TREND_TYPE:
            if establishment_unit is not None or initial_leave is not None:
                raise ValueError(
                    "recursive trend-type center has no physical fifth segment"
                )
        else:
            if establishment_unit is None:
                raise ValueError(
                    "physical center requires a fifth establishment segment"
                )
            if (
                establishment_unit.structural_level != self.structural_level
                or establishment_unit.source_kind is not self.source_kind
            ):
                raise ValueError("establishment unit level/source mismatch")
            if establishment_unit.price_basis_revision != self.price_basis_revision:
                raise ValueError("establishment unit price basis mismatch")
            if not establishment_unit.locked:
                raise ValueError("establishment unit must be locked")
            if establishment_unit in self.initial_units:
                raise ValueError(
                    "fifth establishment unit must follow the middle-three core"
                )
            if initial_leave is not None and initial_leave != establishment_unit:
                raise ValueError("initial leave must be the fifth establishment unit")

        expected_zd = max(item.low_tick for item in self.core_units)
        expected_zg = min(item.high_tick for item in self.core_units)
        if (self.zd_tick, self.zg_tick) != (expected_zd, expected_zg):
            raise ValueError("center core must equal its three core-unit intersection")
        expected_dd = min(item.low_tick for item in self.body_units)
        expected_gg = max(item.high_tick for item in self.body_units)
        if (self.dd_tick, self.gg_tick) != (expected_dd, expected_gg):
            raise ValueError("center envelope must equal body envelope")
        if self.source_kind is SourceKind.TREND_TYPE:
            if self.zd_tick > self.zg_tick:
                raise ValueError("trend-type center requires zd_tick <= zg_tick")
        elif self.zd_tick >= self.zg_tick:
            raise ValueError("line center requires zd_tick < zg_tick")
        if self.dd_tick > self.zd_tick or self.gg_tick < self.zg_tick:
            raise ValueError("envelope must contain the core")
        if any(not self._overlaps_core(item) for item in self.body_units):
            raise ValueError(
                "each center body unit must overlap the frozen center core"
            )
        if self.source_kind is not SourceKind.TREND_TYPE:
            if not self._overlaps_core(self.entry_unit):
                raise ValueError("entry unit must positively overlap center core")
            if establishment_unit is None or not self._overlaps_core(
                establishment_unit
            ):
                raise ValueError(
                    "fifth establishment unit must positively overlap center core"
                )
            if initial_leave is not None:
                if not self._outside_in_direction(initial_leave):
                    raise ValueError(
                        "initial leave endpoint must be outside center core"
                    )
            else:
                if (
                    not self.extension_units
                    or self.extension_units[0] != establishment_unit
                ):
                    raise ValueError(
                        "non-leaving fifth unit must be the first center extension"
                    )
                if self._outside_in_direction(establishment_unit):
                    raise ValueError(
                        "outside fifth unit must be represented as initial leave"
                    )

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

        first = self.body_units[0]
        if self.entry_unit.end_tick != first.start_tick:
            raise ValueError("center entry must connect to first core unit")
        if first.market_start < self.entry_unit.market_end:
            raise ValueError("center body cannot overlap external entry")

        if self.body_start_market_time != self.body_units[0].market_start:
            raise ValueError("body start time must equal first body unit")
        maturity_unit = (
            self.initial_units[-1]
            if self.source_kind is SourceKind.TREND_TYPE
            else self.establishment_unit
        )
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
            (self.entry_unit,)
            + self.body_units
            + (() if self.establishment_unit is None else (self.establishment_unit,))
        )
        if self.available_at < max(item.available_at for item in center_evidence):
            raise ValueError("center availability must cover body evidence")
        if self.body_revision != len(self.extension_units):
            raise ValueError("body_revision must equal extension unit count")

        for terminal in (
            self.establishment_unit,
            self.establishment_leave_unit,
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

        def validate_pending_leave() -> None:
            pending = self.pending_leave_unit
            if pending is None:
                return
            if pending in self.body_units:
                raise ValueError("pending leave must stay outside center body")
            if not pending.locked:
                raise ValueError("pending leave must be locked")
            if not self._overlaps_core(pending):
                raise ValueError("pending leave must overlap center core")
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
            if leave in self.body_units:
                raise ValueError("completion leave must stay outside center body")
            if ret in self.body_units:
                raise ValueError("completion return must not enter center body")
            if not leave.locked or not ret.locked:
                raise ValueError("completion evidence must be locked")
            if not self._overlaps_core(leave) or not self._outside_in_direction(leave):
                raise ValueError("completion leave geometry is invalid")
            self._validate_external_leave(leave)
            if (
                self.source_kind is not SourceKind.TREND_TYPE
                and leave.direction == ret.direction
            ):
                raise ValueError("completion return must alternate with leave")
            if leave.end_tick != ret.start_tick:
                raise ValueError("completion return must connect to leave")
            if ret.market_start < leave.market_end:
                raise ValueError("completion return cannot overlap leave")
            if leave.direction == "up":
                valid_return = ret.direction == "down" and ret.low_tick >= self.zg_tick
            else:
                valid_return = ret.direction == "up" and ret.high_tick <= self.zd_tick
            if not valid_return:
                raise ValueError("completion return must stay outside center core")
            if self.completed_at != ret.confirmed_at:
                raise ValueError(
                    "completed_at must equal completion return confirmation"
                )
            if self.available_at < max(
                self.completed_at,
                leave.available_at,
                ret.available_at,
            ):
                raise ValueError("center availability must cover completion evidence")

        if self.state is CenterState.ONGOING:
            if (
                self.completion_leave_unit is not None
                or self.completion_return_unit is not None
                or self.completed_at is not None
            ):
                raise ValueError("ongoing center cannot retain completion evidence")
            validate_pending_leave()
            if (
                self.boundary_divergence_id is not None
                or self.boundary_anchor_unit_id is not None
            ):
                raise ValueError("ongoing center cannot carry boundary evidence")
        elif self.state is CenterState.COMPLETED:
            if self.pending_leave_unit is not None:
                raise ValueError("completed center cannot retain pending leave")
            if (
                self.boundary_divergence_id is not None
                or self.boundary_anchor_unit_id is not None
            ):
                raise ValueError("completed center cannot carry boundary evidence")
            validate_completion()
        else:
            if not self.boundary_divergence_id or not self.boundary_anchor_unit_id:
                raise ValueError(
                    "divergence-closed center requires divergence and anchor ids"
                )
            has_pending = self.pending_leave_unit is not None
            has_completion = self.completion_leave_unit is not None
            if has_pending == has_completion:
                raise ValueError(
                    "divergence closure requires either pending or completed leave proof"
                )
            if has_pending:
                if (
                    self.completion_return_unit is not None
                    or self.completed_at is not None
                ):
                    raise ValueError(
                        "pending divergence closure cannot retain completion evidence"
                    )
                validate_pending_leave()
            else:
                validate_completion()

        expected_center_id = build_center_id(
            price_basis_revision=self.price_basis_revision,
            structural_level=self.structural_level,
            source_kind=self.source_kind.value,
            entry_unit_id=self.entry_unit.unit_id,
            initial_unit_ids=tuple(item.unit_id for item in self.initial_units),
            establishment_unit_id=(
                None
                if self.establishment_unit is None
                else self.establishment_unit.unit_id
            ),
            zd_tick=self.zd_tick,
            zg_tick=self.zg_tick,
        )
        if self.center_id != expected_center_id:
            raise ValueError("center_id must match the immutable center seed")

    def _overlaps_core(self, item: ConstituentUnit) -> bool:
        left = max(item.low_tick, self.zd_tick)
        right = min(item.high_tick, self.zg_tick)
        if self.source_kind is SourceKind.TREND_TYPE:
            return left <= right
        return left < right

    def _outside_in_direction(self, item: ConstituentUnit) -> bool:
        return (
            item.end_tick > self.zg_tick
            if item.direction == "up"
            else item.end_tick < self.zd_tick
        )

    def _validate_external_leave(self, leave: ConstituentUnit) -> None:
        previous = self.body_units[-1]
        if (
            leave.end_tick == previous.end_tick
            and leave.market_end == previous.market_end
        ):
            raise ValueError("external leave must be distinct from center body")
        if previous.end_tick != leave.start_tick:
            raise ValueError("external leave must connect to center body")
        if leave.market_start < previous.market_end:
            raise ValueError("external leave cannot overlap center body in time")

    @property
    def core_units(
        self,
    ) -> tuple[ConstituentUnit, ConstituentUnit, ConstituentUnit]:
        return self.initial_units[:3]  # type: ignore[return-value]

    @property
    def core_body_start_market_time(self) -> datetime:
        return self.core_units[0].market_start

    @property
    def core_body_end_market_time(self) -> datetime:
        if self.completion_leave_unit is not None:
            return self.completion_leave_unit.market_start
        if self.pending_leave_unit is not None:
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
    def initial_exit_unit(self) -> ConstituentUnit | None:
        """若第五段已是外部离开段，则返回该段。"""

        return self.establishment_leave_unit

    @property
    def maturity_unit(self) -> ConstituentUnit:
        """返回锁定后使中枢正式成立的不可变单元。"""

        if self.source_kind is SourceKind.TREND_TYPE:
            return self.initial_units[-1]
        if self.establishment_unit is None:  # 由 ``__post_init__`` 保护
            raise ValueError("physical center maturity evidence is missing")
        return self.establishment_unit

    @property
    def establishment_units(self) -> tuple[ConstituentUnit, ...]:
        """返回用于建立该中枢的精确来源窗口。"""

        if self.source_kind is SourceKind.TREND_TYPE:
            return self.initial_units
        maturity = self.establishment_unit
        if maturity is None:  # 由 ``__post_init__`` 保护，同时保持精确类型
            return ()
        return (self.entry_unit, *self.initial_units, maturity)

    @property
    def lifecycle_leave_unit(self) -> ConstituentUnit | None:
        """返回当前归属于该中枢的外部离开段。"""

        return self.completion_leave_unit or self.pending_leave_unit

    @property
    def lifecycle_role_count(self) -> int:
        """统计进入段、真实本体段和一段外部离开段。

        完成回返用于确认三类点，故意不计入中枢五角色。失败离开及其回返会先
        折叠进 ``body_units``，只有确认离开失败后才作为延伸段计数。
        """

        return (
            1
            + len(self.body_units)
            + (1 if self.lifecycle_leave_unit is not None else 0)
        )

    @property
    def has_minimum_physical_roles(self) -> bool:
        """返回该对象是否已具备离开内部物理状态机的最低角色数。"""

        if self.source_kind is SourceKind.TREND_TYPE:
            return True
        return (
            self.establishment_unit is not None
            and len(self.establishment_units) == 5
            and self.lifecycle_role_count >= 5
        )

    @property
    def completion_direction(self) -> Direction | None:
        if self.completion_leave_unit is None:
            return None
        return self.completion_leave_unit.direction

    @property
    def physically_completed(self) -> bool:
        """返回离开段及外部首次回返是否均已确认。"""

        return (
            self.completion_leave_unit is not None
            and self.completion_return_unit is not None
            and self.completed_at is not None
        )

    @property
    def completion_available_at(self) -> datetime | None:
        """返回三类点首次完成时的不可变可见时间。

        中枢随后可能因一类点背驰边界改为 ``DIVERGENCE_CLOSED``，此时中枢自身
        的 ``available_at`` 会覆盖新的边界证据，但不能反向改写此前三类点的
        首次可见时间。
        """

        leave = self.completion_leave_unit
        ret = self.completion_return_unit
        if leave is None or ret is None or self.completed_at is None:
            return None
        evidence = (
            self.entry_unit,
            *self.establishment_units,
            *self.body_units,
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
    entry_unit_id: str
    unit_ids: tuple[str, ...]
    state: CenterPreviewState
    zd_tick: int | None
    zg_tick: int | None
    available_at: datetime
    pending_leave_unit_id: str | None = None
    completion_leave_unit_id: str | None = None
    completion_return_unit_id: str | None = None
    establishment_unit_id: str | None = None
    establishment_leave_unit_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "state", CenterPreviewState(self.state))
        object.__setattr__(self, "unit_ids", tuple(self.unit_ids))
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be >= 0")
        if not self.price_basis_revision or not self.price_basis_revision.strip():
            raise ValueError("price_basis_revision is required")
        if not self.entry_unit_id:
            raise ValueError("preview requires an external entry unit")
        if not self.unit_ids:
            raise ValueError("preview must reference at least one body unit")
        if len(set(self.unit_ids)) != len(self.unit_ids):
            raise ValueError("preview unit ids must be unique")
        if self.entry_unit_id in self.unit_ids:
            raise ValueError("preview entry must stay outside its body")
        if (self.zd_tick is None) != (self.zg_tick is None):
            raise ValueError("preview core ticks must be both present or both absent")
        if self.state is CenterPreviewState.TOUCH_ONLY and (
            self.zd_tick is None or self.zd_tick != self.zg_tick
        ):
            raise ValueError("touch-only preview requires a zero-width core")
        if (
            self.state is CenterPreviewState.FORMING
            and self.zd_tick is not None
            and (
                self.zd_tick > self.zg_tick
                if self.source_kind is SourceKind.TREND_TYPE
                else self.zd_tick >= self.zg_tick
            )
        ):
            raise ValueError("forming preview core violates source overlap contract")
        if self.state is CenterPreviewState.COMPLETED:
            if (
                len(self.unit_ids) < 3
                or self.zd_tick is None
                or self.zg_tick is None
                or (
                    self.zd_tick > self.zg_tick
                    if self.source_kind is SourceKind.TREND_TYPE
                    else self.zd_tick >= self.zg_tick
                )
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
            value
            for value in (
                self.pending_leave_unit_id,
                self.completion_leave_unit_id,
                self.completion_return_unit_id,
            )
            if value is not None
        )
        if len(set(lifecycle_ids)) != len(lifecycle_ids):
            raise ValueError("preview lifecycle unit ids must be distinct")
        if set(lifecycle_ids) & set(self.unit_ids):
            raise ValueError("preview lifecycle units must stay outside its body")
        if self.entry_unit_id in lifecycle_ids:
            raise ValueError("preview entry and lifecycle units must be distinct")
        if (
            self.establishment_leave_unit_id is not None
            and self.establishment_leave_unit_id != self.establishment_unit_id
        ):
            raise ValueError(
                "preview initial leave must be its fifth establishment unit"
            )
        if self.source_kind is SourceKind.TREND_TYPE and (
            self.establishment_unit_id is not None
            or self.establishment_leave_unit_id is not None
        ):
            raise ValueError(
                "trend-type preview has no physical fifth establishment unit"
            )

    @property
    def formal_center_id(self) -> str | None:
        """返回该预览锁定后将采用的正式中枢身份。

        触碰型预览没有有效价格区间，永远不能提升为正式中枢。其余预览与
        正式中枢共用完全相同的种子身份，盘中候选因此可以保留到确认阶段的
        精确中枢血缘，而不需要由选股层重新拼装另一套身份。
        """

        if self.zd_tick is None or self.zg_tick is None:
            return None
        return build_center_id(
            price_basis_revision=self.price_basis_revision,
            structural_level=self.structural_level,
            source_kind=self.source_kind.value,
            entry_unit_id=self.entry_unit_id,
            initial_unit_ids=self.unit_ids[: center_seed_size(self.source_kind)],
            establishment_unit_id=self.establishment_unit_id,
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
    entry_unit_id: str
    core_unit_ids: tuple[str, str, str]
    establishment_unit_id: str | None
    initial_exit_unit_id: str | None
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
    boundary_divergence_id: str | None = None
    boundary_anchor_unit_id: str | None = None

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
            initial_unit_ids=tuple(item.unit_id for item in center.initial_units),
            entry_unit_id=center.entry_unit.unit_id,
            core_unit_ids=tuple(item.unit_id for item in center.core_units),
            establishment_unit_id=(
                None
                if center.establishment_unit is None
                else center.establishment_unit.unit_id
            ),
            initial_exit_unit_id=(
                None
                if center.initial_exit_unit is None
                else center.initial_exit_unit.unit_id
            ),
            body_unit_ids=tuple(item.unit_id for item in center.body_units),
            extension_unit_ids=tuple(item.unit_id for item in center.extension_units),
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
            boundary_divergence_id=center.boundary_divergence_id,
            boundary_anchor_unit_id=center.boundary_anchor_unit_id,
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
        if not ongoing or not forming:
            return

        active = ongoing[0]
        active_seed = tuple(
            item.unit_id
            for item in (
                active.establishment_units
                if active.source_kind is not SourceKind.TREND_TYPE
                else (active.entry_unit, *active.initial_units)
            )
        )
        seed_width = center_seed_size(active.source_kind)
        forming_seed = (
            forming[0].entry_unit_id,
            *forming[0].unit_ids[:seed_width],
            *(
                ()
                if active.source_kind is SourceKind.TREND_TYPE
                or forming[0].establishment_unit_id is None
                else (forming[0].establishment_unit_id,)
            ),
        )
        if forming_seed == active_seed:
            return
        active_completion_observed = any(
            preview.state is CenterPreviewState.COMPLETED
            and (
                preview.entry_unit_id,
                *preview.unit_ids[:seed_width],
                *(
                    ()
                    if active.source_kind is SourceKind.TREND_TYPE
                    or preview.establishment_unit_id is None
                    else (preview.establishment_unit_id,)
                ),
            )
            == active_seed
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
    # 已确认的同级别趋势背驰或盘整背驰，都可以在后续中枢关系变化出现前结束
    # 当前走势。该字段保持可选，以便表达仅由几何关系完成的走势快照。
    terminal_divergence: DivergenceEvidence | None = None

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
            center.structural_level != self.structural_level for center in self.centers
        ):
            raise ValueError("trend level must match all evidence")
        source_kinds = {item.source_kind for item in self.constituent_units} | {
            center.source_kind for center in self.centers
        }
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
            item.source_kind is SourceKind.SEGMENT for item in self.constituent_units
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

        if self.state in (TrendState.COMPLETE, TrendState.LOCKED):
            if self.terminal_divergence is None:
                if any(
                    center.state is not CenterState.COMPLETED for center in self.centers
                ):
                    raise ValueError("completed trend requires completed centers")
            elif (
                any(
                    center.state is not CenterState.COMPLETED
                    for center in self.centers[:-1]
                )
                or self.centers[-1].state is not CenterState.DIVERGENCE_CLOSED
            ):
                raise ValueError(
                    "divergence-completed trend requires a boundary-closed last center"
                )
        if self.state in (TrendState.COMPLETE, TrendState.LOCKED) and any(
            not item.locked for item in self.constituent_units
        ):
            raise ValueError("completed trend requires locked constituent units")

        constituent_ids = {item.unit_id for item in self.constituent_units}
        for center in self.centers:
            missing = tuple(
                item
                for item in center.body_units
                if item.unit_id not in constituent_ids
            )
            if missing:
                raise ValueError("trend must contain every center body unit")
        for center in self.centers[:-1]:
            if (
                center.completion_return_unit is not None
                and center.completion_return_unit.unit_id not in constituent_ids
            ):
                raise ValueError("internal completion return must remain in trend")
        terminal_leave = self.centers[-1].completion_leave_unit
        terminal_return = self.centers[-1].completion_return_unit
        if self.terminal_divergence is None:
            if (
                terminal_return is not None
                and terminal_return.unit_id in constituent_ids
            ):
                raise ValueError("terminal completion return belongs to the next trend")
            if (
                terminal_leave is not None
                and self.constituent_units[-1] != terminal_leave
            ):
                raise ValueError("terminal unit must be the final leave unit")
        else:
            divergence = self.terminal_divergence
            expected_divergence_kind = (
                "trend"
                if self.kind is TrendKind.TREND
                else "consolidation"
            )
            if (
                self.state not in (TrendState.COMPLETE, TrendState.LOCKED)
                or divergence.kind != expected_divergence_kind
                or not divergence.is_divergent
                or divergence.structural_level != self.structural_level
                or divergence.source_kind not in source_kinds
                or divergence.price_basis_revision != self.price_basis_revision
                or divergence.direction != self.direction
            ):
                raise ValueError("末端背驰必须与当前正式走势类型一致")
            terminal_center = self.centers[-1]
            leave = terminal_center.lifecycle_leave_unit
            terminal = self.constituent_units[-1]
            if (
                leave is None
                or terminal_center.boundary_divergence_id
                != divergence.divergence_id
                or terminal_center.boundary_anchor_unit_id
                != divergence.signal_unit_id
            ):
                raise ValueError(
                    "divergence-locked trend must preserve its boundary center"
                )
            unit_by_id = {item.unit_id: item for item in self.constituent_units}
            unit_offsets = {
                item.unit_id: offset
                for offset, item in enumerate(self.constituent_units)
            }
            try:
                compare_leg = tuple(
                    unit_by_id[item] for item in divergence.compare_leg_unit_ids
                )
                signal_leg = tuple(
                    unit_by_id[item] for item in divergence.signal_leg_unit_ids
                )
            except KeyError as exc:
                raise ValueError(
                    "divergence comparison leg is missing from trend units"
                ) from exc

            def contiguous(leg: tuple[ConstituentUnit, ...]) -> bool:
                offsets = tuple(unit_offsets[item.unit_id] for item in leg)
                return offsets == tuple(range(offsets[0], offsets[0] + len(offsets)))

            def directional_leg(leg: tuple[ConstituentUnit, ...]) -> bool:
                if len(leg) == 1:
                    return leg[0].direction == divergence.direction
                first, reverse, last = leg
                return (
                    first.direction == last.direction == divergence.direction
                    and reverse.direction != divergence.direction
                )

            compare_first_outside = True
            signal_terminal_extends = True
            if divergence.comparison_width == 3:
                compare_first = compare_leg[0]
                compare_first_outside = (
                    compare_first.high_tick < terminal_center.zd_tick
                    if divergence.direction == "up"
                    else compare_first.low_tick > terminal_center.zg_tick
                )
                signal_terminal_extends = (
                    signal_leg[-1].high_tick
                    > max(item.high_tick for item in signal_leg[:-1])
                    if divergence.direction == "up"
                    else signal_leg[-1].low_tick
                    < min(item.low_tick for item in signal_leg[:-1])
                )
            expected_anchor = (
                terminal.high_tick if self.direction == "up" else terminal.low_tick
            )
            makes_whole_trend_extreme = (
                terminal.high_tick
                > max(item.high_tick for item in self.constituent_units[:-1])
                if self.direction == "up"
                else terminal.low_tick
                < min(item.low_tick for item in self.constituent_units[:-1])
            )
            if (
                terminal.direction != self.direction
                or divergence.compare_unit_id != terminal_center.entry_unit.unit_id
                or divergence.signal_unit_id != terminal.unit_id
                or compare_leg[-1] != terminal_center.entry_unit
                or signal_leg[0] != leave
                or signal_leg[-1] != terminal
                or not contiguous(compare_leg)
                or not contiguous(signal_leg)
                or not directional_leg(compare_leg)
                or not directional_leg(signal_leg)
                or not compare_first_outside
                or not signal_terminal_extends
                or not makes_whole_trend_extreme
                or terminal.market_end != divergence.anchor_at
                or expected_anchor != divergence.anchor_tick
                or self.confirmed_at < divergence.confirmed_at
                or self.available_at < divergence.available_at
            ):
                raise ValueError(
                    "trend terminal comparison leg must match divergence evidence"
                )

        ticks = (self.start_tick, self.end_tick, self.low_tick, self.high_tick)
        if any(type(tick) is not int for tick in ticks):
            raise TypeError("trend ticks must be integers")
        first = self.constituent_units[0]
        last = self.constituent_units[-1]
        if (
            self.start_tick != first.start_tick
            or self.end_tick != last.end_tick
            or self.low_tick != min(item.low_tick for item in self.constituent_units)
            or self.high_tick != max(item.high_tick for item in self.constituent_units)
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

        expected_trend_id = build_trend_id(
            price_basis_revision=self.price_basis_revision,
            structural_level=self.structural_level,
            center_ids=tuple(center.center_id for center in self.centers),
            constituent_unit_ids=tuple(item.unit_id for item in self.constituent_units),
            direction=self.direction,
            terminal_divergence_id=(
                None
                if self.terminal_divergence is None
                else self.terminal_divergence.divergence_id
            ),
        )
        if self.trend_id != expected_trend_id:
            raise ValueError("trend_id must match the immutable trend evidence")

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
    decomposition_boundaries: tuple[DecompositionBoundaryEvidence, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "current_trends", tuple(self.current_trends))
        object.__setattr__(self, "completed_trends", tuple(self.completed_trends))
        object.__setattr__(
            self,
            "decomposition_boundaries",
            tuple(self.decomposition_boundaries),
        )
        if any(
            trend.state is not TrendState.COMPLETE for trend in self.completed_trends
        ):
            raise ValueError(
                "completed_trends must contain immutable COMPLETE snapshots"
            )
        if (
            tuple(
                sorted(
                    self.completed_trends,
                    key=lambda trend: (trend.available_at, trend.trend_id),
                )
            )
            != self.completed_trends
        ):
            raise ValueError("completed_trends must be deterministically ordered")
        if len({trend.trend_id for trend in self.completed_trends}) != len(
            self.completed_trends
        ):
            raise ValueError("completed trend snapshots must be unique")
        boundaries = self.decomposition_boundaries
        if (
            tuple(
                sorted(
                    boundaries, key=lambda item: (item.available_at, item.boundary_id)
                )
            )
            != boundaries
        ):
            raise ValueError(
                "decomposition boundaries must be deterministically ordered"
            )
        if len({item.boundary_id for item in boundaries}) != len(boundaries):
            raise ValueError("decomposition boundaries must be unique")
        current_ids = {trend.trend_id for trend in self.current_trends}
        if any(item.left_trend_id not in current_ids for item in boundaries):
            raise ValueError("decomposition boundary must reference a current trend")
        current_by_id = {trend.trend_id: trend for trend in self.current_trends}
        for boundary in boundaries:
            trend = current_by_id[boundary.left_trend_id]
            if (
                trend.state is not TrendState.LOCKED
                or trend.terminal_divergence != boundary.divergence
                or trend.centers[-1].center_id != boundary.terminal_center_id
                or trend.terminal_unit.unit_id != boundary.anchor_unit_id
                or trend.market_end != boundary.anchor_at
            ):
                raise ValueError(
                    "decomposition boundary must preserve its exact terminal trend"
                )


@dataclass(frozen=True, slots=True)
class StrictLevelResult:
    structural_level: int
    units: tuple[ConstituentUnit, ...]
    center_result: CenterLevelResult
    trend_types: tuple[TrendType, ...]
    completed_trends: tuple[TrendType, ...]
    decomposition_boundaries: tuple[DecompositionBoundaryEvidence, ...] = ()
    decomposition_mode: Literal["same_level"] = "same_level"

    def __post_init__(self) -> None:
        object.__setattr__(self, "units", tuple(self.units))
        object.__setattr__(self, "trend_types", tuple(self.trend_types))
        object.__setattr__(self, "completed_trends", tuple(self.completed_trends))
        object.__setattr__(
            self,
            "decomposition_boundaries",
            tuple(self.decomposition_boundaries),
        )
        if type(self.structural_level) is not int or self.structural_level < 0:
            raise ValueError("structural_level must be non-negative")
        if self.decomposition_mode != "same_level":
            raise ValueError("strict level requires fixed same-level decomposition")
        if self.center_result.structural_level != self.structural_level:
            raise ValueError("level result center level mismatch")
        if any(item.structural_level != self.structural_level for item in self.units):
            raise ValueError("level result unit level mismatch")
        expected_source = (
            SourceKind.SEGMENT if self.structural_level == 0 else SourceKind.TREND_TYPE
        )
        if any(item.source_kind is not expected_source for item in self.units):
            raise ValueError(
                "strict level units must use the canonical recursive source"
            )
        if any(
            center.source_kind is not expected_source
            for center in self.center_result.centers
        ) or any(
            preview.source_kind is not expected_source
            for preview in self.center_result.previews
        ):
            raise ValueError("strict level centers must match their recursive source")
        if any(
            trend.structural_level != self.structural_level
            for trend in self.trend_types + self.completed_trends
        ):
            raise ValueError("level result trend level mismatch")
        if any(
            boundary.structural_level != self.structural_level
            for boundary in self.decomposition_boundaries
        ):
            raise ValueError("level result boundary level mismatch")
        current_trend_ids = tuple(trend.trend_id for trend in self.trend_types)
        completed_trend_ids = tuple(trend.trend_id for trend in self.completed_trends)
        if len(set(current_trend_ids)) != len(current_trend_ids):
            raise ValueError("strict level current trend ids must be unique")
        if len(set(completed_trend_ids)) != len(completed_trend_ids):
            raise ValueError("strict level completed trend ids must be unique")
        trend_ids = set(current_trend_ids)
        if any(
            boundary.left_trend_id not in trend_ids
            for boundary in self.decomposition_boundaries
        ):
            raise ValueError("level result boundary trend is missing")
        trend_by_id = {trend.trend_id: trend for trend in self.trend_types}
        locked_trends = tuple(
            trend for trend in self.trend_types if trend.state is TrendState.LOCKED
        )
        for previous, current in zip(locked_trends, locked_trends[1:]):
            if (
                previous.end_tick != current.start_tick
                or current.market_start < previous.market_end
            ):
                raise ValueError("locked same-level trends must form one causal chain")
        unit_index = {unit.unit_id: index for index, unit in enumerate(self.units)}
        if len(unit_index) != len(self.units):
            raise ValueError("strict level unit ids must be unique")
        locked_count = self.center_result.locked_unit_count
        replay_from = self.center_result.replay_from
        if (
            type(locked_count) is not int
            or type(replay_from) is not int
            or not 0 <= replay_from <= locked_count <= len(self.units)
            or any(not unit.locked for unit in self.units[:locked_count])
            or any(unit.locked for unit in self.units[locked_count:])
        ):
            raise ValueError("center replay counts must match the locked unit prefix")
        units_by_id = {unit.unit_id: unit for unit in self.units}
        centers_by_id = {
            center.center_id: center for center in self.center_result.centers
        }
        if len(centers_by_id) != len(self.center_result.centers):
            raise ValueError("strict level center ids must be unique")
        for center in self.center_result.centers:
            evidence_units = (
                center.entry_unit,
                *center.establishment_units,
                *center.body_units,
                *center.extension_units,
                *(
                    ()
                    if center.pending_leave_unit is None
                    else (center.pending_leave_unit,)
                ),
                *(
                    ()
                    if center.completion_leave_unit is None
                    else (center.completion_leave_unit,)
                ),
                *(
                    ()
                    if center.completion_return_unit is None
                    else (center.completion_return_unit,)
                ),
            )
            if any(units_by_id.get(unit.unit_id) != unit for unit in evidence_units):
                raise ValueError("center evidence is not closed over level units")
        for preview in self.center_result.previews:
            referenced = (
                preview.entry_unit_id,
                *preview.unit_ids,
                *(
                    unit_id
                    for unit_id in (
                        preview.pending_leave_unit_id,
                        preview.completion_leave_unit_id,
                        preview.completion_return_unit_id,
                        preview.establishment_unit_id,
                        preview.establishment_leave_unit_id,
                    )
                    if unit_id is not None
                ),
            )
            if any(unit_id not in units_by_id for unit_id in referenced):
                raise ValueError("center preview references a missing level unit")
            if preview.state is CenterPreviewState.COMPLETED:
                seed_width = center_seed_size(preview.source_kind)
                if (
                    preview.formal_center_id is None
                    or len(preview.unit_ids) < seed_width
                    or (
                        preview.source_kind is SourceKind.SEGMENT
                        and preview.establishment_unit_id is None
                    )
                ):
                    raise ValueError("已完成中枢预览缺少正式成立种子")
                lifecycle_ids = (
                    preview.entry_unit_id,
                    *preview.unit_ids,
                    *(
                        ()
                        if preview.establishment_unit_id is None
                        else (preview.establishment_unit_id,)
                    ),
                    preview.completion_leave_unit_id,
                    preview.completion_return_unit_id,
                )
                lifecycle_units = tuple(
                    units_by_id[unit_id]
                    for unit_id in lifecycle_ids
                    if unit_id is not None
                )
                if all(unit.locked for unit in lifecycle_units):
                    raise ValueError("全锁定中枢必须提升为正式证据而非保留预览")
        for event in self.center_result.events:
            if event.center_id not in centers_by_id or any(
                unit_id is not None and unit_id not in units_by_id
                for unit_id in (event.leave_unit_id, event.return_unit_id)
            ):
                raise ValueError("center event references missing formal evidence")
        for trend in (*self.trend_types, *self.completed_trends):
            if any(
                units_by_id.get(unit.unit_id) != unit
                for unit in trend.constituent_units
            ) or any(
                centers_by_id.get(center.center_id) != center
                for center in trend.centers
            ):
                raise ValueError("trend evidence is not closed over its strict level")
        for boundary in self.decomposition_boundaries:
            trend = trend_by_id[boundary.left_trend_id]
            if (
                trend.state is not TrendState.LOCKED
                or trend.terminal_divergence != boundary.divergence
                or trend.centers[-1].center_id != boundary.terminal_center_id
                or centers_by_id.get(boundary.terminal_center_id) != trend.centers[-1]
                or trend.terminal_unit.unit_id != boundary.anchor_unit_id
                or trend.market_end != boundary.anchor_at
            ):
                raise ValueError("boundary must reference its exact terminal trend")
            boundary_index = unit_index.get(boundary.anchor_unit_id)
            if boundary_index is None:
                raise ValueError("boundary anchor is missing from level units")
            for center in self.center_result.centers:
                evidence = (
                    center.entry_unit,
                    *center.body_units,
                    *(
                        ()
                        if center.pending_leave_unit is None
                        else (center.pending_leave_unit,)
                    ),
                    *(
                        ()
                        if center.completion_leave_unit is None
                        else (center.completion_leave_unit,)
                    ),
                    *(
                        ()
                        if center.completion_return_unit is None
                        else (center.completion_return_unit,)
                    ),
                )
                try:
                    offsets = tuple(unit_index[item.unit_id] for item in evidence)
                except KeyError as exc:
                    raise ValueError(
                        "center evidence is missing from level units"
                    ) from exc
                if min(offsets) <= boundary_index < max(offsets):
                    raise ValueError(
                        "same-level center cannot cross divergence boundary"
                    )
        boundary_center_ids = {
            boundary.terminal_center_id for boundary in self.decomposition_boundaries
        }
        closed_center_ids = {
            center.center_id
            for center in self.center_result.centers
            if center.state is CenterState.DIVERGENCE_CLOSED
        }
        if boundary_center_ids != closed_center_ids:
            raise ValueError(
                "divergence-closed centers and decomposition boundaries must match"
            )


@dataclass(frozen=True, slots=True)
class StrictStructureResult:
    schema: str
    price_basis_revision: str
    levels: tuple[StrictLevelResult, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "levels", tuple(self.levels))
        if self.schema != "chanlun-structure":
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
        self._validate_recursive_unit_lineage()
        for level in self.structure.levels:
            for unit in level.units:
                if (
                    unit.market_end > self.source_closed_at
                    or unit.available_at > self.source_closed_at
                    or (
                        unit.confirmed_at is not None
                        and unit.confirmed_at > self.source_closed_at
                    )
                ):
                    raise ValueError("strict structure contains future unit evidence")
            for center in level.center_result.centers:
                if (
                    center.established_at > self.source_closed_at
                    or center.available_at > self.source_closed_at
                    or (
                        center.completed_at is not None
                        and center.completed_at > self.source_closed_at
                    )
                ):
                    raise ValueError("strict structure contains future center evidence")
            for preview in level.center_result.previews:
                if preview.available_at > self.source_closed_at:
                    raise ValueError("strict structure contains future center preview")
            for event in level.center_result.events:
                if (
                    event.market_time > self.source_closed_at
                    or event.available_at > self.source_closed_at
                ):
                    raise ValueError("strict structure contains future center event")
            for trend in (*level.trend_types, *level.completed_trends):
                if (
                    trend.market_end > self.source_closed_at
                    or trend.available_at > self.source_closed_at
                    or (
                        trend.confirmed_at is not None
                        and trend.confirmed_at > self.source_closed_at
                    )
                ):
                    raise ValueError("strict structure contains future trend evidence")
            for boundary in level.decomposition_boundaries:
                if (
                    boundary.anchor_at > self.source_closed_at
                    or boundary.confirmed_at > self.source_closed_at
                    or boundary.available_at > self.source_closed_at
                ):
                    raise ValueError(
                        "strict structure contains future boundary evidence"
                    )
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
            value > self.source_closed_at
            for value in (
                *(
                    center.available_at
                    for center in self.stroke_center_observations.centers
                ),
                *(
                    preview.available_at
                    for preview in self.stroke_center_observations.previews
                ),
                *(
                    event.available_at
                    for event in self.stroke_center_observations.events
                ),
            )
        ):
            raise ValueError("stroke observations contain future evidence")
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
        levels_by_number = {
            level.structural_level: level for level in self.structure.levels
        }
        for point in all_points:
            level = levels_by_number.get(point.structural_level)
            if level is None:
                raise ValueError("strict point structural level is unavailable")
            expected_source = (
                SourceKind.SEGMENT
                if point.structural_level == 0
                else SourceKind.TREND_TYPE
            )
            if point.source_kind is not expected_source:
                raise ValueError("strict point source does not match its level")
            anchors = tuple(
                unit for unit in level.units if unit.unit_id == point.anchor_unit_id
            )
            if len(anchors) != 1:
                raise ValueError("strict point anchor is missing from its level")
            anchor = anchors[0]
            expected_tick = anchor.low_tick if point.side == "buy" else anchor.high_tick
            if (
                anchor.source_kind is not point.source_kind
                or anchor.market_end != point.anchor_at
                or expected_tick != point.anchor_tick
                or anchor.available_at > point.available_at
            ):
                raise ValueError("strict point does not preserve its anchor unit")
            if point.status is StrictPointStatus.CONFIRMED and (
                not anchor.locked
                or anchor.confirmed_at is None
                or anchor.confirmed_at > point.confirmed_at
            ):
                raise ValueError("confirmed point anchor is not causally locked")
        if len({point.point_id for point in self.confirmed_points}) != len(
            self.confirmed_points
        ):
            raise ValueError("confirmed point ids must be unique")
        if len({point.point_id for point in self.approaching_points}) != len(
            self.approaching_points
        ):
            raise ValueError("approaching point ids must be unique")
        if len({point.point_id for point in all_points}) != len(all_points):
            raise ValueError("confirmed and approaching point ids must be disjoint")
        confirmed_by_id = {point.point_id: point for point in self.confirmed_points}
        # 延迟导入避免模型定义与纯规则模块形成加载环。盘中三类点必须能够从
        # 当前严格中枢/预览账本完整重放，调用方不能自行拼装第二套三类点逻辑。
        from chanlun.core.strict_structure.point_rules import (
            approaching_third_class_points,
        )

        expected_approaching_thirds = {
            point.point_id: point
            for level in self.structure.levels
            for point in approaching_third_class_points(
                level,
                price_quantum=self.structure_price_quantum,
            )
        }
        actual_approaching_thirds = {
            point.point_id: point
            for point in self.approaching_points
            if point.point_type in {"3buy", "3sell"}
        }
        if actual_approaching_thirds != expected_approaching_thirds:
            raise ValueError("盘中三类点必须精确重放严格中枢账本")
        for point in self.approaching_points:
            if point.point_type not in {"2buy", "2sell"}:
                if point.parent_point_id is not None:
                    raise ValueError("only second-class points may reference a parent")
                continue
            parent = confirmed_by_id.get(point.parent_point_id)
            expected_parent_type = "1buy" if point.side == "buy" else "1sell"
            if (
                parent is None
                or parent.point_type != expected_parent_type
                or parent.side != point.side
                or parent.available_at > point.available_at
            ):
                raise ValueError("approaching second-class parent is unresolved")
            is_small_to_large = "small_to_large_reversal" in point.evidence_codes
            if is_small_to_large:
                if (
                    parent.structural_level >= point.structural_level
                    or tuple(point.related_point_ids) != (parent.point_id,)
                ):
                    raise ValueError(
                        "approaching small-to-large evidence graph is incomplete"
                    )
                self._validate_small_to_large_second(
                    point,
                    parent,
                    levels_by_number,
                    approaching=True,
                )
            elif (
                parent.structural_level != point.structural_level
                or point.related_point_ids
            ):
                raise ValueError(
                    "approaching ordinary second-class parent must be same-level"
                )
        for point in self.confirmed_points:
            related = []
            for related_id in point.related_point_ids:
                evidence = confirmed_by_id.get(related_id)
                if evidence is None:
                    raise ValueError("confirmed point related evidence is missing")
                if evidence.available_at > point.available_at:
                    raise ValueError("related point evidence is available too late")
                if evidence.confirmed_at > point.confirmed_at:
                    raise ValueError("related point confirmation is available too late")
                related.append(evidence)
            if point.point_type not in {"2buy", "2sell"}:
                if point.parent_point_id is not None:
                    raise ValueError("only second-class points may reference a parent")
                continue
            parent = confirmed_by_id.get(point.parent_point_id)
            expected_parent_type = "1buy" if point.side == "buy" else "1sell"
            if (
                parent is None
                or parent.point_type != expected_parent_type
                or parent.side != point.side
                or parent.confirmed_at > point.confirmed_at
                or parent.available_at > point.available_at
            ):
                raise ValueError("second-class parent evidence is unresolved")
            is_small_to_large = "small_to_large_reversal" in point.evidence_codes
            if is_small_to_large:
                if (
                    parent.structural_level >= point.structural_level
                    or parent.point_id not in point.related_point_ids
                    or tuple(point.related_point_ids) != (parent.point_id,)
                ):
                    raise ValueError(
                        "small-to-large second-class evidence graph is incomplete"
                    )
                self._validate_small_to_large_second(
                    point,
                    parent,
                    levels_by_number,
                )
            elif parent.structural_level != point.structural_level:
                raise ValueError("ordinary second-class parent must be same-level")
        if len({item.divergence_id for item in self.divergences}) != len(
            self.divergences
        ):
            raise ValueError("divergence ids must be unique")
        structure_levels = {level.structural_level for level in self.structure.levels}
        if any(
            item.price_basis_revision != self.price_basis_revision
            for item in self.divergences
        ):
            raise ValueError("strict evidence divergence price basis mismatch")
        if any(
            item.structural_level not in structure_levels for item in self.divergences
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
        divergence_by_id = {item.divergence_id: item for item in self.divergences}
        embedded_divergences = tuple(
            item
            for item in (
                *(point.divergence for point in self.confirmed_points),
                *(
                    trend.terminal_divergence
                    for level in self.structure.levels
                    for trend in (*level.trend_types, *level.completed_trends)
                ),
                *(
                    boundary.divergence
                    for level in self.structure.levels
                    for boundary in level.decomposition_boundaries
                ),
            )
            if item is not None
        )
        embedded_by_id: dict[str, DivergenceEvidence] = {}
        for item in embedded_divergences:
            previous = embedded_by_id.setdefault(item.divergence_id, item)
            if previous != item:
                raise ValueError("embedded divergence id maps to conflicting evidence")
        if divergence_by_id != embedded_by_id:
            raise ValueError(
                "formal divergence ledger must exactly match embedded evidence"
            )
        boundaries = tuple(
            boundary
            for level in self.structure.levels
            for boundary in level.decomposition_boundaries
        )
        first_points_by_divergence: dict[str, list[StrictPointEvidence]] = {}
        for point in self.confirmed_points:
            if point.point_type not in {"1buy", "1sell"}:
                continue
            if point.divergence is None:
                raise ValueError("一类点缺少正式背驰证据")
            first_points_by_divergence.setdefault(
                point.divergence.divergence_id,
                [],
            ).append(point)
        boundary_divergence_ids = {
            boundary.divergence.divergence_id for boundary in boundaries
        }
        if not boundary_divergence_ids.issubset(first_points_by_divergence):
            raise ValueError("每个同级背驰边界必须唯一对应一个一类点")
        for boundary in boundaries:
            matches = first_points_by_divergence[boundary.divergence.divergence_id]
            expected_type = (
                "1buy" if boundary.divergence.direction == "down" else "1sell"
            )
            if len(matches) != 1:
                raise ValueError("每个同级背驰边界必须唯一对应一个一类点")
            point = matches[0]
            if (
                point.point_type != expected_type
                or point.structural_level != boundary.structural_level
                or point.source_kind is not boundary.source_kind
                or point.center_id != boundary.terminal_center_id
                or point.anchor_unit_id != boundary.anchor_unit_id
                or point.anchor_at != boundary.anchor_at
                or point.anchor_tick != boundary.anchor_tick
                or point.divergence != boundary.divergence
            ):
                raise ValueError("一类点必须保留其同级背驰边界的精确血缘")
        expected_revision = build_strict_evidence_revision(
            symbol=self.symbol,
            source_frequency=self.source_frequency,
            price_basis_revision=self.price_basis_revision,
            strict_config_revision=self.strict_config_revision,
            structure=self.structure,
            confirmed_points=self.confirmed_points,
            divergences=self.divergences,
        )
        if self.structure_revision != expected_revision:
            raise ValueError("structure_revision does not match formal evidence")

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
            if center.physically_completed
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
            raise ValueError("completed centers and third-class points must match")

    def _validate_small_to_large_second(
        self,
        point,
        parent,
        levels_by_number,
        *,
        approaching: bool = False,
    ) -> None:
        """从冻结的递归图中重建小转大二类点的同一套三段证明。"""

        target = levels_by_number[point.structural_level]
        carrier_ids = point.small_to_large_carrier_unit_ids
        positions = {unit.unit_id: index for index, unit in enumerate(target.units)}
        if any(unit_id not in positions for unit_id in carrier_ids):
            raise ValueError("small-to-large carrier is missing from target level")
        indexes = tuple(positions[unit_id] for unit_id in carrier_ids)
        if indexes != tuple(range(indexes[0], indexes[0] + 3)):
            raise ValueError("small-to-large carrier must be the immediate sequence")
        signal, rebound, pullback = (target.units[index] for index in indexes)
        expected_signal = "down" if point.side == "buy" else "up"
        signal_extreme = (
            signal.low_tick == parent.anchor_tick
            if point.side == "buy"
            else signal.high_tick == parent.anchor_tick
        )
        if approaching:
            source_level = levels_by_number.get(point.structural_level - 1)

            def ready(unit) -> bool:
                if unit.locked:
                    return True
                if source_level is None:
                    return False
                matches = tuple(
                    trend
                    for trend in source_level.trend_types
                    if trend.trend_id == unit.unit_id
                )
                return len(matches) == 1 and matches[0].complete

            carrier_locks_valid = (
                ready(signal) and ready(rebound) and not pullback.locked
            )
        else:
            carrier_locks_valid = (
                signal.locked and rebound.locked and pullback.locked
            )
        if (
            not carrier_locks_valid
            or signal.direction != expected_signal
            or signal.market_end != parent.anchor_at
            or signal.end_tick != parent.anchor_tick
            or not signal_extreme
            or rebound.direction == signal.direction
            or pullback.direction != signal.direction
            or signal.end_tick != rebound.start_tick
            or rebound.end_tick != pullback.start_tick
            or rebound.market_start < signal.market_end
            or pullback.market_start < rebound.market_end
            or pullback.unit_id != point.anchor_unit_id
        ):
            raise ValueError("small-to-large carrier geometry is invalid")

        children_by_id: dict[str, tuple[str, ...]] = {}

        def register(identifier: str, children) -> None:
            values = tuple(children)
            previous = children_by_id.setdefault(identifier, values)
            if previous != values:
                raise ValueError("recursive evidence identity changed")

        for level in self.structure.levels:
            for unit in level.units:
                register(unit.unit_id, unit.child_ids)
            for trend in (*level.trend_types, *level.completed_trends):
                register(
                    trend.trend_id,
                    (unit.unit_id for unit in trend.constituent_units),
                )

        def descendants(unit) -> frozenset[str]:
            output: set[str] = set()
            pending = list(unit.child_ids)
            while pending:
                identifier = pending.pop()
                if identifier in output:
                    continue
                output.add(identifier)
                pending.extend(children_by_id.get(identifier, ()))
            return frozenset(output)

        signal_descendants = descendants(signal)
        if parent.anchor_unit_id not in signal_descendants:
            raise ValueError("small-to-large parent is outside its signal carrier")

    def _validate_recursive_unit_lineage(self) -> None:
        """重放相邻级别之间的精确生产递归关系。"""

        # 延迟导入用于避免 models -> adapter/decomposition -> models 循环依赖。
        from chanlun.core.strict_structure.unit_adapter import (
            build_recursive_unit_stream,
        )

        for level_number in range(1, len(self.structure.levels)):
            previous = self.structure.levels[level_number - 1]
            current = self.structure.levels[level_number]
            protected_ids = frozenset(
                boundary.left_trend_id for boundary in previous.decomposition_boundaries
            )
            expected, _oscillatory_ids = build_recursive_unit_stream(
                previous.trend_types,
                protected_ids,
            )
            if current.units != expected:
                raise ValueError(
                    "recursive level units must exactly replay prior locked trends"
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
    """构建已确认严格结构买卖点的稳定身份。"""

    if not price_basis_revision or not price_basis_revision.strip():
        raise ValueError("price_basis_revision is required")
    if point_type not in {"1buy", "2buy", "3buy", "1sell", "2sell", "3sell"}:
        raise ValueError("unsupported strict point type")
    if type(structural_level) is not int or structural_level < 0:
        raise ValueError("structural_level must be non-negative")
    if not anchor_unit_id:
        raise ValueError("anchor_unit_id is required")
    return stable_structure_id(
        "chanlun-strict-point",
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
    strength_source: Literal["macd"]
    compare_leg_unit_ids: tuple[str, ...] = ()
    signal_leg_unit_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        compare_leg_ids = tuple(self.compare_leg_unit_ids) or (self.compare_unit_id,)
        signal_leg_ids = tuple(self.signal_leg_unit_ids) or (self.signal_unit_id,)
        object.__setattr__(self, "compare_leg_unit_ids", compare_leg_ids)
        object.__setattr__(self, "signal_leg_unit_ids", signal_leg_ids)
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
        if (
            len(compare_leg_ids) not in (1, 3)
            or len(signal_leg_ids) != len(compare_leg_ids)
            or compare_leg_ids[-1] != self.compare_unit_id
            or signal_leg_ids[-1] != self.signal_unit_id
            or len(set(compare_leg_ids)) != len(compare_leg_ids)
            or len(set(signal_leg_ids)) != len(signal_leg_ids)
        ):
            raise ValueError(
                "divergence requires equal one- or three-unit comparison legs"
            )
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
        if self.strength_source != "macd":
            raise ValueError("unsupported divergence strength source")
        expected_id = stable_structure_id(
            "chanlun-strict-divergence",
            self.price_basis_revision,
            self.structural_level,
            self.source_kind.value,
            self.kind,
            self.direction,
            compare_leg_ids,
            signal_leg_ids,
        )
        if self.divergence_id != expected_id:
            raise ValueError("divergence_id does not match formal evidence")

    @property
    def is_divergent(self) -> bool:
        # 面积、方向柱峰值和 DIF 极值是相互独立的力度证据，任意一项衰减即可
        # 确认力度转弱；同时成立的指标数量只影响置信度，不改变正式布尔结论。
        return self.price_extreme_confirmed and self.strength_decay_count > 0

    @property
    def comparison_width(self) -> int:
        return len(self.compare_leg_unit_ids)

    @property
    def strength_decay_count(self) -> int:
        return sum(
            (
                self.histogram_area_decayed,
                self.histogram_peak_decayed,
                self.dif_extreme_decayed,
            )
        )

@dataclass(frozen=True, slots=True)
class DecompositionBoundaryEvidence:
    """固定同级别划分所使用的因果已确认边界。"""

    boundary_id: str
    decomposition_mode: Literal["same_level"]
    boundary_kind: Literal["trend_divergence", "consolidation_divergence"]
    structural_level: int
    source_kind: SourceKind
    price_basis_revision: str
    left_trend_id: str
    terminal_center_id: str
    anchor_unit_id: str
    anchor_at: datetime
    anchor_tick: int
    confirmed_at: datetime
    available_at: datetime
    divergence: DivergenceEvidence

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        if self.decomposition_mode != "same_level":
            raise ValueError("unsupported decomposition mode")
        expected_kind = f"{self.divergence.kind}_divergence"
        if self.boundary_kind != expected_kind:
            raise ValueError("boundary kind must match divergence kind")
        if not self.divergence.is_divergent:
            raise ValueError("decomposition boundary requires confirmed divergence")
        if (
            self.structural_level != self.divergence.structural_level
            or self.source_kind is not self.divergence.source_kind
            or self.price_basis_revision != self.divergence.price_basis_revision
            or self.anchor_unit_id != self.divergence.signal_unit_id
            or self.anchor_at != self.divergence.anchor_at
            or self.anchor_tick != self.divergence.anchor_tick
            or self.confirmed_at != self.divergence.confirmed_at
            or self.available_at != self.divergence.available_at
        ):
            raise ValueError("boundary must preserve exact divergence evidence")
        if (
            not self.left_trend_id
            or not self.terminal_center_id
            or not self.anchor_unit_id
        ):
            raise ValueError("boundary requires its completed left trend")
        expected_id = stable_structure_id(
            "chanlun-decomposition-boundary",
            self.price_basis_revision,
            self.decomposition_mode,
            self.boundary_kind,
            self.structural_level,
            self.source_kind.value,
            self.left_trend_id,
            self.terminal_center_id,
            self.divergence.divergence_id,
        )
        if self.boundary_id != expected_id:
            raise ValueError("boundary_id does not match formal evidence")


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
    small_to_large_carrier_unit_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", StrictPointStatus(self.status))
        object.__setattr__(self, "variant", StrictPointVariant(self.variant))
        object.__setattr__(self, "source_kind", SourceKind(self.source_kind))
        object.__setattr__(self, "evidence_codes", tuple(self.evidence_codes))
        object.__setattr__(self, "missing_conditions", tuple(self.missing_conditions))
        object.__setattr__(self, "related_point_ids", tuple(self.related_point_ids))
        object.__setattr__(
            self,
            "small_to_large_carrier_unit_ids",
            tuple(self.small_to_large_carrier_unit_ids),
        )

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
            and self.center_zd_tick > self.center_zg_tick
        ):
            raise ValueError("center boundaries must form a closed interval")

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
            or self.divergence.kind not in {"trend", "consolidation"}
            or not self.divergence.is_divergent
        ):
            raise ValueError(
                "一类买卖点必须来自正式趋势背驰或盘整背驰"
            )
        if self.point_type in {"2buy", "2sell"}:
            if self.variant not in {
                StrictPointVariant.STRICT,
                StrictPointVariant.WEAK_DIVERGENCE,
            }:
                raise ValueError(
                    "second class requires strict or weak divergence variant"
                )
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
        if (
            self.divergence is not None
            and self.available_at < self.divergence.available_at
        ):
            raise ValueError("point availability must cover divergence")
        if self.divergence is not None and (
            self.point_type in {"1buy", "1sell"}
            or self.variant is StrictPointVariant.WEAK_DIVERGENCE
        ):
            expected_direction = "down" if self.side == "buy" else "up"
            if (
                self.divergence.structural_level != self.structural_level
                or self.divergence.source_kind is not self.source_kind
                or self.divergence.price_basis_revision != self.price_basis_revision
                or self.divergence.direction != expected_direction
                or self.divergence.anchor_at != self.anchor_at
                or self.divergence.anchor_tick != self.anchor_tick
            ):
                raise ValueError("point must preserve its exact divergence anchor")
            if (
                self.confirmed_at is not None
                and self.confirmed_at < self.divergence.confirmed_at
            ):
                raise ValueError(
                    "point confirmation must cover divergence confirmation"
                )
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
        is_small_to_large = "small_to_large_reversal" in self.evidence_codes
        if is_small_to_large:
            if self.point_type not in {"2buy", "2sell"}:
                raise ValueError("small-to-large evidence requires second class")
            if (
                len(self.small_to_large_carrier_unit_ids) != 3
                or len(set(self.small_to_large_carrier_unit_ids)) != 3
                or any(
                    not isinstance(unit_id, str) or not unit_id
                    for unit_id in self.small_to_large_carrier_unit_ids
                )
                or self.anchor_unit_id != self.small_to_large_carrier_unit_ids[-1]
            ):
                raise ValueError(
                    "小转大二类点必须保留完整的离开、反向、再离开三段载体"
                )
        elif self.small_to_large_carrier_unit_ids:
            raise ValueError(
                "ordinary point cannot carry small-to-large structural evidence"
            )
        if self.status is StrictPointStatus.CONFIRMED:
            expected_id = build_strict_point_id(
                price_basis_revision=self.price_basis_revision,
                point_type=self.point_type,
                structural_level=self.structural_level,
                anchor_unit_id=self.anchor_unit_id,
                center_id=self.center_id,
                parent_point_id=self.parent_point_id,
            )
            if self.point_id != expected_id:
                raise ValueError("confirmed point_id does not match formal evidence")

    @property
    def structure_anchor_price(self) -> Decimal:
        return self.price_quantum * self.anchor_tick

    @property
    def structure_invalidation_price(self) -> Decimal:
        return self.price_quantum * self.invalidation_tick
