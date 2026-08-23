from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import isfinite

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

    The current screening contract deliberately trades only the first-center
    third buy.  A later-center or identity-unknown third buy may still remain
    available as chart evidence, but it must never enter selection,
    notification, review, or replay execution lanes.
    """

    return point.point_type != "3buy" or point.center_ordinal == 1


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
            "structure_invalidation_price": (
                point.structure_invalidation_price
            ),
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
            list[StructuralPoint | ProvisionalCandidate],
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
            "provisional"
            if isinstance(point, ProvisionalCandidate)
            else "confirmed"
        )
        lane = (
            *five_minute_setup_family_lane(point),
            *_five_minute_terminal_segment_lane(point),
            certainty_lane,
        )
        structural_rank = _five_minute_setup_structural_rank(point)
        previous = latest.get(lane)
        if previous is None or structural_rank > previous[0]:
            latest[lane] = (structural_rank, [point])
        elif structural_rank == previous[0]:
            previous[1].append(point)

    retained = [
        point
        for _rank, lane_points in latest.values()
        for point in lane_points
    ]
    formed_frontier_by_lane: dict[
        tuple[str, str, int, str, str], datetime
    ] = {}
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
            and formed_frontier
            > normalize_datetime(point.anchor_at, "setup anchor_at")
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


def is_one_minute_reversal_trigger(point: StructuralPoint) -> bool:
    """判断正式点能否作为 1 分钟反转触发证据。

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


def is_one_minute_continuation_trigger(
    point: StructuralPoint,
    *,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> bool:
    """判断正式三类点能否作为 1 分钟延续触发证据。

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
    return is_one_minute_reversal_trigger(
        point
    ) or is_one_minute_continuation_trigger(point, minimum_tick=minimum_tick)


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


def is_one_minute_execution_trigger(
    point: StructuralPoint,
    *,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> bool:
    """旧接口别名；1 分钟点现在只表示段差证据，不再触发 5 分钟信号。"""

    return is_one_minute_segment_difference(point, minimum_tick=minimum_tick)


def is_one_minute_execution_trigger_document(
    point: Mapping[str, object],
    *,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
    expected_side: str | None = None,
) -> bool:
    """旧文档接口别名；保留用于读取既有档案。"""

    return is_one_minute_segment_difference_document(
        point,
        minimum_tick=minimum_tick,
        expected_side=expected_side,
    )


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


def _setup_price_range(point: StructuralPoint) -> tuple[float, float]:
    prices = [
        point.structure_invalidation_price,
        point.structure_anchor_price,
    ]
    boundary = point.center_zg if point.side == "buy" else point.center_zd
    if boundary is not None:
        prices.append(boundary)
    return min(prices), max(prices)


def five_minute_segment_difference_window_start(
    setup_point: StructuralPoint,
) -> datetime:
    """Return the structural audit start of the 1m evidence window.

    A production 5m point is anchored at the *end* of its terminal segment.
    Lower-level evidence can exist while that segment is forming, so the audit
    window starts at the segment's market start.  Executable matching below has
    an additional causal gate: a 1m locator must become available no earlier
    than the formal 5m setup itself.
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
    return normalize_datetime(
        setup_point.anchor_at if reference is None else reference.market_start,
        "segment difference window start",
    )


def _segment_difference_matches_five_minute_point(
    setup_point: StructuralPoint,
    segment_point: StructuralPoint,
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str,
) -> bool:
    closed_at = normalize_datetime(as_of, "as_of")
    window_start = five_minute_segment_difference_window_start(setup_point)
    price_low, price_high = _setup_price_range(setup_point)
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
        and segment_point.code == setup_point.code
        and segment_point.side == setup_point.side
        and segment_point.point_id != setup_point.point_id
        and segment_point.price_basis_revision == setup_point.price_basis_revision
        and segment_point.confirmed_at is not None
        and setup_point.available_at <= closed_at
        and window_start <= segment_point.anchor_at
        and setup_point.available_at <= segment_point.available_at <= closed_at
        and price_low
        <= segment_point.structure_anchor_price
        <= price_high
    )


def match_five_minute_setup_point(
    trigger: StructuralPoint,
    points: tuple[StructuralPoint, ...],
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
    max_setup_age_seconds: int = MAX_FIVE_MINUTE_SETUP_AGE_SECONDS,
) -> StructuralPoint | None:
    """为一个 1 分钟执行触发选择唯一、因果有效的 5 分钟设置。

    跨市场实时监听没有 A 股板块对象，不能直接构造完整决策包；但它仍必须与
    主决策核心共享同一套触发类型、价格区间、价格基准及时序约束。若多条设置
    同时匹配，选择触发发生前最近确认的一条，避免同一触发重复通知。
    """

    if type(max_setup_age_seconds) is not int or max_setup_age_seconds <= 0:
        raise ValueError("max_setup_age_seconds must be a positive integer")
    closed_at = normalize_datetime(as_of, "as_of")
    if trigger.available_at > closed_at or not is_one_minute_segment_difference(
        trigger,
        minimum_tick=minimum_tick,
    ):
        return None
    opportunity_as_of = min(closed_at, trigger.available_at)
    candidates = tuple(
        point
        for point in points
        if five_minute_setup_is_current(
            point,
            as_of=opportunity_as_of,
            max_setup_age_seconds=max_setup_age_seconds,
        )
        and _segment_difference_matches_five_minute_point(
            point,
            trigger,
            as_of=closed_at,
            minimum_tick=minimum_tick,
        )
    )
    return max(
        candidates,
        key=lambda point: (
            point.available_at,
            point.recursive_level,
            point.tower,
            point.point_id,
        ),
        default=None,
    )


def match_one_minute_segment_difference(
    setup: TradeSetup,
    points: tuple[StructuralPoint, ...],
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> StructuralPoint | None:
    if not isinstance(setup.point, StructuralPoint) or not setup.point.confirmed:
        return None
    return match_one_minute_segment_difference_for_point(
        setup.point,
        points,
        as_of=as_of,
        minimum_tick=minimum_tick,
    )


def match_one_minute_segment_difference_for_point(
    setup_point: StructuralPoint,
    points: tuple[StructuralPoint, ...],
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> StructuralPoint | None:
    """为已确认的 5 分钟正式点选择最近的因果 1 分钟段差。

    形成期间的低级别结构仍属于底层审计事实，但不能充当事后才确认的 5m
    信号之执行定位器。这里只返回在正式 5m 点可用后出现的 1m 证据；它不
    参与 5m 主信号是否成立的判断。
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
        if _segment_difference_matches_five_minute_point(
            setup_point,
            point,
            as_of=closed_at,
            minimum_tick=minimum_tick,
        )
    )
    return max(
        matches,
        key=lambda point: (
            point.available_at,
            point.recursive_level,
            point.tower,
            point.point_id,
        ),
        default=None,
    )


def match_one_minute_trigger(
    setup: TradeSetup,
    points: tuple[StructuralPoint, ...],
    *,
    as_of: datetime,
    minimum_tick: Decimal | float | str = Decimal("0.01"),
) -> StructuralPoint | None:
    """旧接口别名；返回值现在是可选段差证据，不是正式信号触发器。"""

    return match_one_minute_segment_difference(
        setup,
        points,
        as_of=as_of,
        minimum_tick=minimum_tick,
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
    raw_trigger = signal.get("segment_difference_1m", signal.get("trigger_1m"))
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
        else match_one_minute_segment_difference(
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
        if valid_trigger is None or previous.trigger_point_id == valid_trigger.point_id:
            return previous
        # The 5m signal identity and monotonic stage remain unchanged, while a
        # later valid 1m occurrence may replace an expired locator.  Recording
        # the new point and observation time keeps the lifecycle evidence
        # consistent with the decision document and its new execution boundary.
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
        trigger_point_id=(
            None if valid_trigger is None else valid_trigger.point_id
        ),
        reason_codes=_reason_codes(target),
        actionable=target in {"triggered", "executable", "active"},
    )
