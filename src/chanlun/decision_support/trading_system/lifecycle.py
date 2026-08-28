from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite

from chanlun.core.strict_structure.models import SourceKind
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.models import (
    LifecycleStage,
    CONTINUATION_SUPPORT_POINT_TYPES,
    MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
    ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES,
    REVERSAL_SUPPORT_POINT_TYPES,
    SectorAssessment,
    SignalLifecycle,
    StructuralPoint,
    TimeframeContext,
    TradeSetup,
    structural_point_from_document,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
    is_one_minute_segment_level,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    setup_state_for_point,
)


STRUCTURE_INVALIDATED_REASON_CODE = "structure_invalidated"


def five_minute_setup_expires_at(
    point: StructuralPoint | ProvisionalCandidate,
    *,
    max_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
) -> datetime:
    """返回 5 分钟机会窗的结束时间。

    正式点的机会年龄必须从形态锚点计算，不能从可能晚很多才出现的防重绘
    锁定时间重新起算。否则一条早已走完、价格已经反向运行的历史买卖点，
    会在线段最终锁定时被错误地当作全新的实时机会。

    临时候选不同：它由当前未锁定结构前沿逐次重建，``available_at`` 表示这条
    几何形态在最新已完成行情柱上仍然成立。候选尚在严格证据中持续出现时，
    不能因为最初的几何锚点跨过周末或锁定等待期而从选股中消失。
    """

    if type(max_setup_age_seconds) is not int or max_setup_age_seconds <= 0:
        raise ValueError("max_setup_age_seconds must be a positive integer")
    freshness_started_at = (
        point.available_at
        if isinstance(point, ProvisionalCandidate)
        else point.anchor_at
    )
    if freshness_started_at is None:
        raise ValueError("five-minute setup freshness timestamp is required")
    return normalize_datetime(
        freshness_started_at,
        "setup freshness timestamp",
    ) + timedelta(seconds=max_setup_age_seconds)


def five_minute_setup_is_current(
    point: StructuralPoint | ProvisionalCandidate,
    *,
    as_of: datetime,
    max_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
) -> bool:
    """判断一个已可见的 5 分钟结构是否仍属于当前观察窗口。

    新数据携带严格末端线段血缘；它是否“当前”由最新未完成/最新已完成
    两线段窗口决定，不能再用任意日历天数截断。固定年龄只用于兼容没有
    末端血缘的旧文档与测试夹具。
    """

    if type(max_setup_age_seconds) is not int or max_setup_age_seconds <= 0:
        raise ValueError("max_setup_age_seconds must be a positive integer")
    observed_at = normalize_datetime(as_of, "as_of")
    available_at = normalize_datetime(point.available_at, "setup available_at")
    if available_at > observed_at:
        return False
    if point.terminal_segment is not None:
        return True
    return observed_at <= five_minute_setup_expires_at(
        point,
        max_setup_age_seconds=max_setup_age_seconds,
    )


def five_minute_setup_is_executable(
    point: StructuralPoint | ProvisionalCandidate,
    *,
    as_of: datetime,
    max_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
) -> bool:
    """Return whether a visible 5m setup is still inside its order window.

    Terminal lineage answers which completed/unfinished tail is current for
    display and monitoring.  It must not silently turn that structural state
    into a permanent trading opportunity.  Execution therefore always uses
    the fixed, anchor-based expiry contract, independently of tail display.
    """

    observed_at = normalize_datetime(as_of, "as_of")
    available_at = normalize_datetime(point.available_at, "setup available_at")
    return (
        available_at
        <= observed_at
        <= five_minute_setup_expires_at(
            point,
            max_setup_age_seconds=max_setup_age_seconds,
        )
    )


def five_minute_setup_family_lane(
    point: StructuralPoint | ProvisionalCandidate,
) -> tuple[str, str, int]:
    """返回不区分买卖方向的 5 分钟一/二/三类结构通道。"""

    if point.point_type not in {
        "1buy",
        "2buy",
        "3buy",
        "1sell",
        "2sell",
        "3sell",
    }:
        raise ValueError("five-minute setup point_type is invalid")
    return point.point_type[0], point.tower, point.recursive_level


def five_minute_setup_is_in_policy_scope(
    point: StructuralPoint | ProvisionalCandidate,
) -> bool:
    """Return whether a 5m setup belongs to the frozen production scope.

    The current screening contract admits only the first center-relative third
    point on both sides. Later-center third buys and third sells remain chart
    evidence, but cannot enter selection, notification, review or replay lanes.
    Keeping this rule symmetric prevents sell distributions from including a
    broader structural population than buy distributions.
    """

    return (
        point.point_type not in {"3buy", "3sell"}
        or point.center_ordinal == 1
    )


def _five_minute_terminal_segment_lane(
    point: StructuralPoint | ProvisionalCandidate,
) -> tuple[str, str]:
    """Keep the two live-tail segments independent during lifecycle pruning.

    Production points carry an exact terminal-segment reference.  The latest
    unfinished segment and the latest completed segment are two distinct
    observation targets, so a newer point on one must never erase the point
    state of the other.  The legacy lane is retained only for old fixtures and
    imported documents that predate terminal lineage metadata.
    """

    reference = point.terminal_segment
    if reference is None:
        return "legacy", ""
    # Strict internal unit ids may legitimately change when the same physical
    # tail is reconstructed from another converged left boundary.  A trading
    # lane is the market occurrence, not that implementation id.  Using the
    # immutable market geometry keeps current-state pruning and persisted
    # lifecycles stable across a canonical-window rebuild.
    return (
        reference.role,
        "|".join(
            (
                reference.source_kind.value,
                reference.direction,
                reference.market_start.isoformat(timespec="seconds"),
                reference.market_end.isoformat(timespec="seconds"),
            )
        ),
    )


def structural_point_occurrence_id(point: StructuralPoint) -> str:
    """Return the stable physical occurrence used by trading lifecycles.

    Center, parent and terminal-unit hashes describe one reconstruction of the
    strict graph.  They are intentionally absent here because two converged
    canonical windows can assign different internal hashes to the same point.
    The terminal unit can be refined, and its geometry-confirmation timestamp
    can consequently move, while the formal point remains the same operation
    at the same market anchor.  Internal hashes and ``available_at`` are
    therefore excluded.  Executable prices and center boundaries are included:
    a later projection that changes a stop/anchor price is a new causal setup,
    even when it shares the same timestamp and point class.
    """

    if not isinstance(point, StructuralPoint):
        raise TypeError("structural occurrence requires a confirmed point")
    return sha256_json(
        {
            "schema": "chanlun-structural-point-occurrence-v3",
            "code": point.code,
            "price_basis_revision": point.price_basis_revision,
            "source_frequency": point.source_frequency,
            "point_type": point.point_type,
            "side": point.side,
            "variant": point.variant,
            "tower": point.tower,
            "recursive_level": point.recursive_level,
            "anchor_at": point.anchor_at.isoformat(timespec="seconds"),
            "structure_anchor_price": point.structure_anchor_price,
            "structure_invalidation_price": (point.structure_invalidation_price),
            "center_zd": point.center_zd,
            "center_zg": point.center_zg,
            "center_ordinal": point.center_ordinal,
            "divergence_kind": point.divergence_kind,
        }
    )


def _five_minute_setup_identity(
    point: StructuralPoint | ProvisionalCandidate,
) -> str:
    return (
        point.candidate_id
        if isinstance(point, ProvisionalCandidate)
        else structural_point_occurrence_id(point)
    )


def _five_minute_setup_structural_rank(
    point: StructuralPoint | ProvisionalCandidate,
) -> tuple[datetime, datetime]:
    if point.anchor_at is None:
        raise ValueError("five-minute setup anchor_at is required")
    return (
        normalize_datetime(point.anchor_at, "setup anchor_at"),
        normalize_datetime(point.available_at, "setup available_at"),
    )


def _five_minute_setup_tie_rank(
    point: StructuralPoint | ProvisionalCandidate,
) -> tuple[bool, Decimal, int, str]:
    """Choose one conservative event when one tail maps to multiple centers."""

    if isinstance(point, ProvisionalCandidate):
        anchor = Decimal(str(point.anchor_price))
        invalidation = Decimal(str(point.invalidation_price))
    else:
        anchor = Decimal(str(point.structure_anchor_price))
        invalidation = Decimal(str(point.structure_invalidation_price))
    # Standard geometry outranks a boundary touch. Within the same geometry
    # class, the nearest valid structural invalidation is the conservative,
    # lower-risk representative. A lower center ordinal wins the remaining
    # ambiguity and the stable identity makes the result deterministic.
    return (
        point.variant == "standard",
        -abs(anchor - invalidation),
        -(point.center_ordinal or 0),
        _five_minute_setup_identity(point),
    )


def current_five_minute_setup_points(
    points: Sequence[StructuralPoint | ProvisionalCandidate],
    *,
    as_of: datetime,
    max_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
) -> tuple[StructuralPoint | ProvisionalCandidate, ...]:
    """返回每条一/二/三类通道当前唯一的正式点与临时候选。

    买、卖是同一类结构通道的两个方向，不是可以永久并存的两条通道。正式点
    与临时候选仍分别保留，避免一个仅在形成中的新候选覆盖正式设置；但当更新
    锚点的三类候选几何已经出现时，它已经证明旧正式方向不再是当前结构，必须
    淘汰旧方向。结构先后以 ``anchor_at`` 判定，不能被延迟锁定时间倒置。
    """

    observed_at = normalize_datetime(as_of, "as_of")
    latest: dict[
        tuple[str, str, int, str, str, str],
        tuple[
            tuple[datetime, datetime],
            tuple[bool, Decimal, int, str],
            StructuralPoint | ProvisionalCandidate,
        ],
    ] = {}
    for point in points:
        if point.source_frequency != "5m":
            raise ValueError("trade setup requires a 5m point")
        if not is_five_minute_trade_level(
            point.source_frequency,
            point.recursive_level,
        ):
            continue
        if not five_minute_setup_is_in_policy_scope(point):
            continue
        if isinstance(point, StructuralPoint) and not point.confirmed:
            continue
        available_at = normalize_datetime(
            point.available_at,
            "setup available_at",
        )
        if available_at > observed_at:
            raise ValueError("five-minute point cannot be after as_of")
        if not five_minute_setup_is_current(
            point,
            as_of=observed_at,
            max_setup_age_seconds=max_setup_age_seconds,
        ):
            continue
        certainty_lane = (
            "provisional" if isinstance(point, ProvisionalCandidate) else "confirmed"
        )
        lane = (
            *five_minute_setup_family_lane(point),
            *_five_minute_terminal_segment_lane(point),
            certainty_lane,
        )
        structural_rank = _five_minute_setup_structural_rank(point)
        tie_rank = _five_minute_setup_tie_rank(point)
        previous = latest.get(lane)
        if previous is None or (structural_rank, tie_rank) > (
            previous[0],
            previous[1],
        ):
            latest[lane] = (structural_rank, tie_rank, point)

    retained = [
        point for _rank, _tie_rank, point in latest.values()
    ]
    formed_frontier_by_lane: dict[tuple[str, str, int, str, str], datetime] = {}
    for point in retained:
        if not isinstance(point, ProvisionalCandidate):
            continue
        if setup_state_for_point(point).formation_state != "geometry_ready":
            continue
        lane = (
            *five_minute_setup_family_lane(point),
            *_five_minute_terminal_segment_lane(point),
        )
        anchor_at = _five_minute_setup_structural_rank(point)[0]
        previous = formed_frontier_by_lane.get(lane)
        if previous is None or anchor_at > previous:
            formed_frontier_by_lane[lane] = anchor_at

    retained = [
        point
        for point in retained
        if not (
            isinstance(point, StructuralPoint)
            and (
                formed_frontier := formed_frontier_by_lane.get(
                    (
                        *five_minute_setup_family_lane(point),
                        *_five_minute_terminal_segment_lane(point),
                    )
                )
            )
            is not None
            and point.anchor_at is not None
            and formed_frontier > normalize_datetime(point.anchor_at, "setup anchor_at")
        )
    ]
    return tuple(
        sorted(
            retained,
            key=lambda point: (
                normalize_datetime(point.available_at, "setup available_at"),
                *five_minute_setup_family_lane(point),
                *_five_minute_terminal_segment_lane(point),
                point.point_type,
                _five_minute_setup_identity(point),
            ),
        )
    )


def lifecycle_stage_from_signal(signal: Mapping[str, object]) -> str | None:
    """返回当前序列化信号声明的生命周期阶段。"""

    stage = signal.get("lifecycle_stage")
    if stage not in _TRANSITIONS:
        return None
    return stage


def _is_one_minute_reversal_segment_difference(point: StructuralPoint) -> bool:
    """判断正式点能否作为 1 分钟反转段差证据。

    一、二类点分别证明趋势背驰反转和回试确认；三类点只证明离开中枢后的延续，
    不能替代反转证据。实时决策与历史回放必须共同使用这一唯一判定入口。
    """

    return bool(
        point.confirmed
        and is_one_minute_segment_level(
            point.source_frequency,
            point.recursive_level,
        )
        and point.point_type in REVERSAL_SUPPORT_POINT_TYPES
    )


def _positive_minimum_tick(minimum_tick: Decimal | float | str) -> Decimal:
    try:
        value = Decimal(str(minimum_tick))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("minimum_tick must be a positive finite decimal") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("minimum_tick must be a positive finite decimal")
    return value


def third_class_boundary_clearance(point: StructuralPoint) -> Decimal | None:
    """返回三类点相对冻结中枢边界的同向价格间隔。"""

    if point.point_type == "3buy" and point.center_zg is not None:
        return Decimal(str(point.structure_anchor_price)) - Decimal(
            str(point.center_zg)
        )
    if point.point_type == "3sell" and point.center_zd is not None:
        return Decimal(str(point.center_zd)) - Decimal(
            str(point.structure_anchor_price)
        )
    return None


def _is_one_minute_continuation_segment_difference(
    point: StructuralPoint,
    *,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> bool:
    """判断正式三类点能否作为 1 分钟延续段差证据。

    严格结构对象已经冻结了中枢身份、序号以及首次回返几何；决策层在此额外
    拒绝仅触碰边界的形态，并要求回返锚点至少离开边界一个最小价格单位。
    """

    tick = _positive_minimum_tick(minimum_tick)
    clearance = third_class_boundary_clearance(point)
    return bool(
        point.confirmed
        and is_one_minute_segment_level(
            point.source_frequency,
            point.recursive_level,
        )
        and point.point_type in CONTINUATION_SUPPORT_POINT_TYPES
        and point.variant == "standard"
        and point.center_id is not None
        and point.center_zd is not None
        and point.center_zg is not None
        and point.center_ordinal is not None
        and clearance is not None
        and clearance >= tick
    )


def is_one_minute_segment_difference(
    point: StructuralPoint,
    *,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> bool:
    """统一判断可用于段差/精细定位的 1 分钟正式结构点。"""

    if point.point_type not in ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES:
        return False
    return _is_one_minute_reversal_segment_difference(
        point
    ) or _is_one_minute_continuation_segment_difference(
        point,
        minimum_tick=minimum_tick,
    )


def is_one_minute_segment_difference_document(
    point: Mapping[str, object],
    *,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
    expected_side: str | None = None,
) -> bool:
    """对序列化后的 1 分钟段差证据执行同一套封闭校验。"""

    tick = _positive_minimum_tick(minimum_tick)
    point_type = point.get("point_type")
    side = point.get("side")
    if (
        point.get("status") != "confirmed"
        or not is_one_minute_segment_level(
            str(point.get("source_frequency") or ""),
            point.get("recursive_level"),
        )
        or point.get("actionable") is not True
        or point_type not in ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES
        or side not in {"buy", "sell"}
        or side != ("buy" if str(point_type).endswith("buy") else "sell")
        or expected_side is not None
        and side != expected_side
    ):
        return False
    if point_type in REVERSAL_SUPPORT_POINT_TYPES:
        return True
    if (
        point_type not in CONTINUATION_SUPPORT_POINT_TYPES
        or point.get("variant") != "standard"
        or not isinstance(point.get("center_id"), str)
        or not str(point.get("center_id")).strip()
        or type(point.get("center_ordinal")) is not int
        or int(point["center_ordinal"]) <= 0
    ):
        return False
    try:
        anchor = Decimal(str(point["anchor_price"]))
        center_zd = Decimal(str(point["center_zd"]))
        center_zg = Decimal(str(point["center_zg"]))
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return False
    if not all(value.is_finite() for value in (anchor, center_zd, center_zg)):
        return False
    clearance = center_zd - anchor if point_type == "3sell" else anchor - center_zg
    return clearance >= tick


_TRANSITIONS: dict[LifecycleStage | None, set[LifecycleStage]] = {
    None: {"observed", "approaching", "formed", "triggered", "invalidated"},
    "observed": {"approaching", "formed", "triggered", "invalidated"},
    "approaching": {"formed", "triggered", "invalidated"},
    "formed": {"triggered", "invalidated"},
    # ``armed`` 只用于迁移旧快照：升级后无需等待 1 分钟点即可进入正式信号态。
    "armed": {"triggered", "invalidated"},
    "triggered": {"executable", "invalidated"},
    "executable": {"active", "invalidated"},
    "active": {"closed", "invalidated"},
    "closed": set(),
    "invalidated": set(),
}


def build_setup(
    point: StructuralPoint | ProvisionalCandidate,
    context: TimeframeContext,
    sector: SectorAssessment,
    *,
    sector_required: bool = True,
) -> TradeSetup:
    if not is_five_minute_trade_level(
        point.source_frequency,
        point.recursive_level,
    ):
        raise ValueError("trade setup requires a physical 5m level-0 point")
    if isinstance(point, StructuralPoint):
        started_at = point.available_at
        prices = [
            point.structure_invalidation_price,
            point.structure_anchor_price,
        ]
        boundary = point.center_zg if point.side == "buy" else point.center_zd
        if boundary is not None:
            prices.append(boundary)
        point_identity = structural_point_occurrence_id(point)
    else:
        started_at = point.available_at
        prices = [point.invalidation_price, point.anchor_price]
        boundary = point.center_zg if point.side == "buy" else point.center_zd
        if boundary is not None:
            prices.append(boundary)
        point_identity = point.candidate_id
    setup_id = sha256_json(
        {
            "schema": "chanlun-trade-setup",
            "point_id": point_identity,
            "sector_id": sector.sector_id,
            "sector_required": sector_required,
        }
    )
    return TradeSetup(
        setup_id=setup_id,
        point=point,
        context=context,
        sector=sector,
        started_at=started_at,
        price_low=float(min(prices)),
        price_high=float(max(prices)),
        sector_required=sector_required,
    )


def five_minute_segment_difference_window_start(
    setup_point: StructuralPoint,
) -> datetime:
    """Return the structural audit start of the 1m evidence window.

    A production 5m point is anchored at the *end* of its terminal segment.
    Lower-level evidence can exist while that segment is forming, so the audit
    window starts at the segment's market start.  Causal execution uses the
    first timestamp at which both this outer interval and an exact nested 1m
    interval are known; there is no second timing gate after setup formation.
    """

    if (
        not isinstance(setup_point, StructuralPoint)
        or not setup_point.confirmed
        or not is_five_minute_trade_level(
            setup_point.source_frequency,
            setup_point.recursive_level,
        )
    ):
        raise ValueError("segment difference window requires a confirmed 5m setup")
    reference = setup_point.terminal_segment
    if reference is None:
        return normalize_datetime(
            setup_point.anchor_at,
            "segment difference window start",
        )
    return completed_bar_interval_start(
        reference.market_start,
        minutes=5,
        field="segment difference window start",
    )


def completed_bar_interval_start(
    completed_at: datetime,
    *,
    minutes: int,
    field: str,
) -> datetime:
    """Return the left edge of one end-labelled completed minute bar.

    Trading-system structure timestamps are completed-bar labels.  Cross-
    frequency containment therefore cannot compare the first 5m and 1m labels
    directly: for example, the completed 5m bar labelled 09:55 covers the same
    left boundary as the completed 1m bar labelled 09:51.  Projecting both
    labels to their physical left edges preserves exact interval containment.
    """

    if type(minutes) is not int or minutes <= 0:
        raise ValueError("completed bar duration must be a positive integer")
    return normalize_datetime(completed_at, field) - timedelta(minutes=minutes)


def _nesting_witness_matches_five_minute_point(
    setup_point: StructuralPoint,
    segment_point: StructuralPoint,
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str,
) -> bool:
    """Return whether one exact 1m terminal segment nests inside the 5m one.

    Point class, price and timestamp proximity are insufficient evidence of
    interval nesting.  Both points must retain their terminal-segment lineage,
    and the complete inner market interval must be contained by the complete
    outer interval.  The inner point may have become available while the 5m
    segment was still forming; causality only requires that both facts were
    available by the decision timestamp.
    """

    closed_at = normalize_datetime(as_of, "as_of")
    setup_reference = setup_point.terminal_segment
    segment_reference = segment_point.terminal_segment
    setup_interval_start = (
        None
        if setup_reference is None
        else completed_bar_interval_start(
            setup_reference.market_start,
            minutes=5,
            field="five minute terminal interval start",
        )
    )
    segment_interval_start = (
        None
        if segment_reference is None
        else completed_bar_interval_start(
            segment_reference.market_start,
            minutes=1,
            field="one minute terminal interval start",
        )
    )
    return bool(
        setup_point.confirmed
        and is_five_minute_trade_level(
            setup_point.source_frequency,
            setup_point.recursive_level,
        )
        and is_one_minute_segment_difference(
            segment_point,
            minimum_tick=minimum_tick,
        )
        and setup_reference is not None
        and segment_reference is not None
        and setup_reference.source_kind is SourceKind.SEGMENT
        and segment_reference.source_kind is SourceKind.SEGMENT
        and segment_point.code == setup_point.code
        and segment_point.side == setup_point.side
        and segment_point.point_id != setup_point.point_id
        and segment_point.price_basis_revision == setup_point.price_basis_revision
        and segment_point.confirmed_at is not None
        and setup_point.available_at <= closed_at
        and segment_point.available_at <= closed_at
        and setup_interval_start <= segment_interval_start
        and segment_reference.market_end <= setup_reference.market_end
    )


def match_one_minute_nesting_witness(
    setup: TradeSetup,
    points: tuple[StructuralPoint, ...],
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> StructuralPoint | None:
    """Return an exact 1m-in-5m nesting witness for a trade setup."""

    if not isinstance(setup.point, StructuralPoint) or not setup.point.confirmed:
        return None
    return match_one_minute_nesting_witness_for_point(
        setup.point,
        points,
        as_of=as_of,
        minimum_tick=minimum_tick,
    )


def match_one_minute_nesting_witness_for_point(
    setup_point: StructuralPoint,
    points: tuple[StructuralPoint, ...],
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> StructuralPoint | None:
    """为已确认的 5 分钟正式点选择首个因果区间套见证。

    1m 见证可以在 5m 末端线段形成期间出现，但两者必须保留精确的末端线段
    血缘，且完整 1m 线段区间必须包含在完整 5m 线段区间内。它不参与 5m
    主信号是否成立的判断，也不构成信号确认后的第二套时序门槛。
    """

    if (
        not isinstance(setup_point, StructuralPoint)
        or not setup_point.confirmed
        or not is_five_minute_trade_level(
            setup_point.source_frequency,
            setup_point.recursive_level,
        )
    ):
        return None
    closed_at = normalize_datetime(as_of, "as_of")
    matches = tuple(
        point
        for point in points
        if _nesting_witness_matches_five_minute_point(
            setup_point,
            point,
            as_of=closed_at,
            minimum_tick=minimum_tick,
        )
    )
    return min(
        matches,
        key=lambda point: (
            max(setup_point.available_at, point.available_at),
            -point.terminal_segment.market_end.timestamp(),
            structural_point_occurrence_id(point),
            point.point_id,
        ),
        default=None,
    )


def _base_stage(setup: TradeSetup) -> LifecycleStage:
    state = setup_state_for_point(setup.point)
    return {
        "forming": "approaching",
        # ``formed`` 是旧生命周期字段名；其精确含义只是“非交易几何候选
        # 已出现”，不能对外解释成正式买卖点已经形成。
        "geometry_ready": "formed",
        # 5 分钟是正式买卖级别：结构锁定后立即成为可通知信号。1 分钟只补充
        # 段差/精细定位，不得把已存在的 5 分钟信号卡在等待态。
        "confirmed": "triggered",
    }[state.formation_state]


def _reason_codes(stage: LifecycleStage) -> tuple[str, ...]:
    return {
        "observed": ("context_or_sector_blocked",),
        "approaching": ("five_minute_provisional",),
        "formed": ("five_minute_geometric_candidate_awaiting_confirmation",),
        "armed": ("legacy_waiting_state_migrated",),
        "triggered": ("five_minute_trade_signal_confirmed",),
        "executable": ("execution_constraints_passed",),
        "active": ("position_active",),
        "closed": ("position_closed",),
        "invalidated": (STRUCTURE_INVALIDATED_REASON_CODE,),
    }[stage]


def lifecycle_state_from_signal_document(
    signal: Mapping[str, object],
) -> tuple[SignalLifecycle, StructuralPoint | None]:
    """恢复持久化生命周期及其原始定位点，并重新验证全部身份。"""

    stage = lifecycle_stage_from_signal(signal)
    if stage is None:
        raise ValueError("signal lifecycle stage is invalid")
    try:
        observed_at = datetime.fromisoformat(str(signal["observed_at"]))
        signal_id = str(signal["signal_id"])
        setup_id = str(signal["setup_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("signal lifecycle identity is invalid") from exc
    raw_trigger = signal.get("segment_difference_1m")
    if raw_trigger is None:
        trigger = None
    elif isinstance(raw_trigger, Mapping):
        trigger = structural_point_from_document(
            raw_trigger,
            code=str(signal.get("code") or ""),
        )
    else:
        raise ValueError("signal lifecycle trigger is invalid")
    lifecycle = SignalLifecycle(
        signal_id=signal_id,
        setup_id=setup_id,
        stage=stage,  # type: ignore[arg-type]
        observed_at=observed_at,
        trigger_point_id=None if trigger is None else trigger.point_id,
        reason_codes=_reason_codes(stage),  # type: ignore[arg-type]
        actionable=stage in {"triggered", "executable", "active"},
    )
    return lifecycle, trigger


def _signal_id(setup: TradeSetup) -> str:
    return sha256_json(
        {
            "schema": "chanlun-signal-lifecycle",
            "setup_id": setup.setup_id,
            "side": setup.point.side,
        }
    )


def _structure_is_invalidated(
    setup: TradeSetup,
    current_price: float | None,
) -> bool:
    """判断最新已收盘价是否越过该设置自己的结构失效价。"""

    if current_price is None:
        return False
    if (
        isinstance(current_price, bool)
        or not isinstance(current_price, (int, float))
        or not isfinite(float(current_price))
        or current_price <= 0
    ):
        raise ValueError("current_price must be a positive finite number")
    invalidation_price = (
        setup.point.structure_invalidation_price
        if isinstance(setup.point, StructuralPoint)
        else setup.point.invalidation_price
    )
    if setup.point.side == "buy":
        # “守前低”允许再次触及前低，三买也允许边界触碰；只有严格跌破才失效。
        return current_price < invalidation_price
    if setup.point.side == "sell":
        # 卖点与买点使用完全对称的严格越界规则。
        return current_price > invalidation_price
    raise ValueError("setup point side is invalid")


def _require_transition(
    previous: LifecycleStage | None,
    target: LifecycleStage,
) -> None:
    if target not in _TRANSITIONS[previous]:
        raise ValueError(f"illegal lifecycle transition: {previous} -> {target}")


def advance_lifecycle(
    previous: SignalLifecycle | None,
    setup: TradeSetup,
    trigger: StructuralPoint | None,
    *,
    as_of: datetime,
    current_price: float | None = None,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> SignalLifecycle:
    observed_at = normalize_datetime(as_of, "as_of")
    signal_id = _signal_id(setup)
    if previous is not None:
        if previous.signal_id != signal_id or previous.setup_id != setup.setup_id:
            raise ValueError("previous lifecycle belongs to another setup")
        if observed_at < previous.observed_at:
            raise ValueError("lifecycle time cannot move backwards")
        if previous.stage in {"triggered", "executable", "active"} and (
            previous.actionable is not True
        ):
            raise ValueError(
                f"illegal lifecycle transition: malformed {previous.stage} state"
            )
        if previous.reason_codes != _reason_codes(previous.stage):
            raise ValueError(
                f"illegal lifecycle transition: malformed {previous.stage} reasons"
            )
        if previous.stage in {"closed", "invalidated"}:
            return previous
    base_stage = _base_stage(setup)
    valid_trigger = (
        None
        if trigger is None
        else match_one_minute_nesting_witness(
            setup,
            (trigger,),
            as_of=observed_at,
            minimum_tick=minimum_tick,
        )
    )
    invalidated = _structure_is_invalidated(setup, current_price)
    # 5 分钟正式确认后的阶段只能向前推进或因结构破坏而失效。1 分钟段差证据
    # 可以稍后补充，但其缺失或离开当前窗口都不能让正式信号降级。
    if (
        previous is not None
        and previous.stage in {"triggered", "executable", "active"}
        and not invalidated
    ):
        # The first exact nesting witness fixes the setup's execution boundary.
        # A later witness is audit evidence only and cannot open another window.
        if previous.trigger_point_id is not None:
            return previous
        if valid_trigger is None:
            return previous
        return SignalLifecycle(
            signal_id=signal_id,
            setup_id=setup.setup_id,
            stage=previous.stage,
            observed_at=observed_at,
            trigger_point_id=valid_trigger.point_id,
            reason_codes=previous.reason_codes,
            actionable=True,
        )
    target: LifecycleStage = (
        "invalidated"
        if invalidated
        else "triggered"
        if base_stage in {"armed", "triggered"}
        else base_stage
    )
    if previous is not None and previous.stage == target:
        return previous
    if previous is None:
        if target == "invalidated":
            _require_transition(None, target)
        else:
            _require_transition(None, base_stage)
    else:
        _require_transition(previous.stage, target)
    return SignalLifecycle(
        signal_id=signal_id,
        setup_id=setup.setup_id,
        stage=target,
        observed_at=observed_at,
        trigger_point_id=(None if valid_trigger is None else valid_trigger.point_id),
        reason_codes=_reason_codes(target),
        actionable=target in {"triggered", "executable", "active"},
    )
