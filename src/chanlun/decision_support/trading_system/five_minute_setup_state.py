"""5 分钟买卖点设置的唯一状态判定。

严格结构核心发布防重绘锁定点和临时候选；交易适配层还会把“最新已完成线段”的
首个因果几何见证提升为操作确认点，以免等待迟到的审计锁造成漏报。因而本合同必须
同时表达形态是否形成、是否达到操作确认，以及防重绘审计锁是否完成。操作确认可以
及时进入人工复核，但绝不能被展示成“审计锁已完成”。最新未完成线段仍是不可操作
候选。选股、实时监听、人工复核和通知都必须消费本模块生成的状态，不能各自重新
解释底层证据码。旧文档没有末端线段血缘时，才使用原有证据码兼容降级。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal


FIVE_MINUTE_SETUP_STATE_CONTRACT = (
    "chanlun-five-minute-setup-state-v4-operational-confirmation"
)
GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION = (
    "GEOMETRY_AWAITING_CONFIRMATION"
)
GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE = (
    "five_minute_geometry_candidate_awaiting_confirmation"
)
_EXECUTION_RECOMMENDATION_LABELS = {
    "WAITING_STRUCTURE": "5分钟买卖点结构仍在形成",
    GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION: (
        "5分钟买卖点仅为几何候选，尚未达到操作确认"
    ),
    "READY": "5分钟买卖点已达到操作确认，仍须人工复核",
    "CAUTION": "5分钟买卖点已达到操作确认，环境逆风或证据需人工复核",
    "BLOCKED": "当前不满足操作条件，等待结构或数据恢复",
}

GEOMETRY_READY_EVIDENCE_CODES = frozenset(
    {
        "provisional_center_completion",
        "core_boundary_held",
    }
)
_UNLOCKED_SEGMENT_EVIDENCE_CODES = frozenset(
    {
        "unfinished_segment_participates",
        "live_first_return",
        "core_boundary_currently_held",
    }
)
_SEGMENT_LOCK_MISSING_CONDITIONS = frozenset(
    {
        "unfinished_segment_lock",
        "terminal_unit_locked",
    }
)

SetupFormationState = Literal["forming", "geometry_ready", "confirmed"]
SetupLockState = Literal["pending", "locked"]


def _codes(value: object) -> frozenset[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        return frozenset()
    return frozenset(
        item for item in value if isinstance(item, str) and item
    )


def has_ready_provisional_geometry(
    point_type: object,
    evidence_codes: object,
) -> bool:
    """返回临时三类点是否已经具备完整几何形态。"""

    return bool(
        point_type in {"3buy", "3sell"}
        and GEOMETRY_READY_EVIDENCE_CODES.issubset(_codes(evidence_codes))
    )


@dataclass(frozen=True, slots=True)
class FiveMinuteSetupState:
    """把形态完成、结构锁定和可操作性拆成互不混淆的事实。"""

    formation_state: SetupFormationState
    lock_state: SetupLockState
    actionable: bool
    contains_forming_segment: bool
    contains_unlocked_segment: bool

    def __post_init__(self) -> None:
        if self.formation_state == "confirmed":
            if (
                not self.actionable
                or self.contains_forming_segment
                or self.lock_state not in {"pending", "locked"}
                or self.contains_unlocked_segment != (self.lock_state == "pending")
            ):
                raise ValueError(
                    "confirmed setup state must be actionable with a consistent audit lock"
                )
            return
        if self.lock_state != "pending" or self.actionable:
            raise ValueError("unconfirmed setup state must remain pending and non-actionable")
        if (
            self.formation_state == "geometry_ready"
            and self.contains_forming_segment
        ):
            raise ValueError("ready geometry cannot contain a forming segment")
        if self.contains_forming_segment and not self.contains_unlocked_segment:
            raise ValueError("forming segment must also be unlocked")

    def document(self) -> dict[str, object]:
        return {
            "state_contract": FIVE_MINUTE_SETUP_STATE_CONTRACT,
            "formation_state": self.formation_state,
            "lock_state": self.lock_state,
            "contains_forming_segment": self.contains_forming_segment,
            "contains_unlocked_segment": self.contains_unlocked_segment,
            "actionable": self.actionable,
            # 兼容字段表达“防重绘审计仍未锁定”，不等同于不可操作。新版消费方
            # 必须同时读取 actionable 与 lock_state，不能再由该旧字段单独推断状态。
            "contains_unfinished_segment": self.contains_unlocked_segment,
        }


def classify_five_minute_setup_state(
    *,
    point_type: object,
    status: object,
    evidence_codes: object = (),
    missing_conditions: object = (),
    terminal_segment_role: object = None,
    terminal_segment_state: object = None,
) -> FiveMinuteSetupState:
    """根据严格点事实返回唯一的 5 分钟设置状态。

    新版生产点都携带精确的末端线段血缘。此时线段角色是几何状态的唯一
    主判据：最新未完成线段只能是 ``forming``；尚未被交易适配层提升的完整几何只能
    是 ``geometry_ready``；已发布的操作确认点是 ``confirmed``，并另外用 lock_state
    区分审计锁待完成或已完成。证据码只为没有血缘的历史研究文档和测试夹具保留
    兼容，不得覆盖当前线段事实。
    """

    has_terminal_role = terminal_segment_role is not None
    has_terminal_state = terminal_segment_state is not None
    if has_terminal_role != has_terminal_state:
        raise ValueError("terminal segment role and state must be provided together")

    if status == "confirmed":
        if has_terminal_role:
            if (
                terminal_segment_role != "latest_completed"
                or terminal_segment_state not in {"formed", "locked"}
            ):
                raise ValueError(
                    "confirmed setup must belong to the formed latest completed segment"
                )
            locked = terminal_segment_state == "locked"
        else:
            locked = True
        return FiveMinuteSetupState(
            formation_state="confirmed",
            lock_state="locked" if locked else "pending",
            actionable=True,
            contains_forming_segment=False,
            contains_unlocked_segment=not locked,
        )
    if status != "provisional":
        raise ValueError("five-minute setup status must be confirmed or provisional")

    if has_terminal_role:
        if (
            terminal_segment_role == "latest_unfinished"
            and terminal_segment_state == "forming"
        ):
            return FiveMinuteSetupState(
                formation_state="forming",
                lock_state="pending",
                actionable=False,
                contains_forming_segment=True,
                contains_unlocked_segment=True,
            )
        if terminal_segment_role == "latest_completed" and terminal_segment_state in {
            "formed",
            "locked",
        }:
            return FiveMinuteSetupState(
                formation_state="geometry_ready",
                lock_state="pending",
                actionable=False,
                contains_forming_segment=False,
                contains_unlocked_segment=(terminal_segment_state == "formed"),
            )
        raise ValueError("provisional setup terminal segment lineage is contradictory")

    evidence = _codes(evidence_codes)
    missing = _codes(missing_conditions)
    geometry_ready = has_ready_provisional_geometry(point_type, evidence)
    contains_unlocked_segment = bool(
        evidence.intersection(_UNLOCKED_SEGMENT_EVIDENCE_CODES)
        or missing.intersection(_SEGMENT_LOCK_MISSING_CONDITIONS)
    )
    return FiveMinuteSetupState(
        formation_state="geometry_ready" if geometry_ready else "forming",
        lock_state="pending",
        actionable=False,
        contains_forming_segment=(contains_unlocked_segment and not geometry_ready),
        contains_unlocked_segment=contains_unlocked_segment,
    )


def setup_state_for_point(point: object) -> FiveMinuteSetupState:
    """从正式点或临时候选读取状态；调用方无需识别具体点类型。"""

    terminal_segment = getattr(point, "terminal_segment", None)

    return classify_five_minute_setup_state(
        point_type=getattr(point, "point_type", None),
        status=getattr(point, "status", None),
        evidence_codes=getattr(point, "evidence_codes", ()),
        missing_conditions=getattr(point, "missing_conditions", ()),
        terminal_segment_role=(
            None if terminal_segment is None else getattr(terminal_segment, "role", None)
        ),
        terminal_segment_state=(
            None if terminal_segment is None else getattr(terminal_segment, "state", None)
        ),
    )


def unconfirmed_setup_recommendation(formation_state: object) -> str:
    """Return the execution-profile state for one unconfirmed 5m setup.

    A provisional setup remains non-tradable until the trading adapter publishes
    an operational confirmation (or the strict engine later publishes its audit
    lock).  A caller must not infer operability from geometry alone.
    """

    if formation_state == "geometry_ready":
        return GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION
    if formation_state == "forming":
        return "WAITING_STRUCTURE"
    raise ValueError(
        "unconfirmed setup formation state must be forming or geometry_ready"
    )


def execution_recommendation_label(recommendation: object) -> str:
    """Return the only display label allowed for an execution recommendation."""

    if not isinstance(recommendation, str):
        raise ValueError("execution recommendation must be a string")
    try:
        return _EXECUTION_RECOMMENDATION_LABELS[recommendation]
    except KeyError as exc:
        raise ValueError("execution recommendation is unsupported") from exc


def unconfirmed_setup_reason_code(
    formation_state: object,
    *,
    forming_reason_code: str,
) -> str:
    """Preserve a precise reason for candidate geometry awaiting confirmation."""

    if not isinstance(forming_reason_code, str) or not forming_reason_code:
        raise ValueError("forming setup reason code must be non-empty")
    if formation_state == "geometry_ready":
        return GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE
    if formation_state == "forming":
        return forming_reason_code
    raise ValueError(
        "unconfirmed setup formation state must be forming or geometry_ready"
    )


def canonical_setup_state_document(
    setup: Mapping[str, object],
) -> dict[str, object]:
    """为新旧设置文档补齐规范状态字段，并覆盖矛盾的展示派生值。"""

    state = classify_five_minute_setup_state(
        point_type=setup.get("point_type"),
        status=setup.get("status"),
        evidence_codes=setup.get("evidence_codes", ()),
        missing_conditions=setup.get("missing_conditions", ()),
        terminal_segment_role=setup.get("terminal_segment_role"),
        terminal_segment_state=setup.get("terminal_segment_state"),
    )
    return {**dict(setup), **state.document()}


def validate_setup_state_document(setup: Mapping[str, object]) -> None:
    """拒绝与底层严格点事实矛盾的序列化状态。"""

    expected = canonical_setup_state_document(setup)
    for field in (
        "state_contract",
        "formation_state",
        "lock_state",
        "contains_forming_segment",
        "contains_unlocked_segment",
        "contains_unfinished_segment",
        "actionable",
    ):
        if setup.get(field) != expected[field]:
            raise ValueError(f"five-minute setup state field changed: {field}")


__all__ = (
    "FIVE_MINUTE_SETUP_STATE_CONTRACT",
    "GEOMETRY_AWAITING_CONFIRMATION_RECOMMENDATION",
    "GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE",
    "GEOMETRY_READY_EVIDENCE_CODES",
    "FiveMinuteSetupState",
    "canonical_setup_state_document",
    "classify_five_minute_setup_state",
    "execution_recommendation_label",
    "has_ready_provisional_geometry",
    "setup_state_for_point",
    "unconfirmed_setup_reason_code",
    "unconfirmed_setup_recommendation",
    "validate_setup_state_document",
)
