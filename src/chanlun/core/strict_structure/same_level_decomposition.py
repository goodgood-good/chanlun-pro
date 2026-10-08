from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from chanlun.core.strict_structure.center_machine import validate_unit_sequence
from chanlun.core.strict_structure.identity import stable_structure_id
from chanlun.core.strict_structure.models import ConstituentUnit, SourceKind, combined_extreme_market_time


@dataclass(frozen=True, slots=True)
class SameLevelCombination:
    """一次同向结合律合并的审计证据。"""

    combined_unit_id: str
    child_unit_ids: tuple[str, ...]
    direction: str
    protected_after_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SameLevelDecompositionResult:
    """可直接递归的确定性同级别分解结果。"""

    units: tuple[ConstituentUnit, ...]
    oscillatory_ids: frozenset[str]
    combinations: tuple[SameLevelCombination, ...]


@dataclass(frozen=True, slots=True)
class L038SixMemberSplit:
    """A bounded no-extension split, separate from center lifecycle ownership."""

    observation_id: str
    source_grade: int
    price_basis_revision: str
    source_unit_ids: tuple[str, ...]
    left_member_ids: tuple[str, str, str]
    right_member_ids: tuple[str, str, str]
    left_core_ticks: tuple[int, int]
    right_core_ticks: tuple[int, int]
    shared_six_core_ticks: tuple[int, int]
    boundary_market_at: datetime
    boundary_tick: int
    known_at: datetime


@dataclass(frozen=True, slots=True)
class FixedThreeMemberTypeCandidate:
    """One locally completed consolidation in a declared no-extension view.

    The member units are already completed lower trend types.  This candidate
    does not certify the whole fixed-level path or replace a canonical type.
    """

    candidate_id: str
    target_structural_level: int
    member_unit_ids: tuple[str, str, str]
    core_ticks: tuple[int, int]
    market_start: datetime
    market_end: datetime
    start_tick: int
    end_tick: int
    known_at: datetime
    lower_type_qualification_pending: bool


@dataclass(frozen=True, slots=True)
class FixedSameLevelThreeMemberReview:
    """Connected prefix and later local centers behind an unresolved connector."""

    view_id: str
    operating_type_label: str
    target_structural_level: int
    source_unit_ids: tuple[str, ...]
    candidates: tuple[FixedThreeMemberTypeCandidate, ...]
    post_gap_center_candidates: tuple[FixedThreeMemberTypeCandidate, ...]
    pending_source_unit_ids: tuple[str, ...]
    unavailable_future_count: int
    status: str
    as_of: datetime


def review_fixed_three_member_partition(
    units: tuple[ConstituentUnit, ...], *, view_id: str,
    operating_type_label: str, as_of: datetime,
    pending_lower_type_ids: frozenset[str] = frozenset(),
) -> FixedSameLevelThreeMemberReview:
    """Review the L038 fixed view in disjoint, connected three-type windows.

    L018:31-55 supplies the three *completed lower trend types* needed for a
    center.  L038:81-94 permits successive one-center consolidations when the
    operating level declares no extension.  Selecting consecutive triples is
    an explicit implementation view: a failed triple, unfinished tail or open
    left boundary cannot be silently promoted to a global formal partition.
    """

    values = tuple(units)
    if not view_id or not view_id.strip():
        raise ValueError("fixed same-level review requires a view id")
    if not operating_type_label or not operating_type_label.strip():
        raise ValueError("fixed same-level review requires an operating type")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("fixed same-level review requires an aware cutoff")
    if not values:
        raise ValueError("fixed same-level review requires lower type units")
    grade = values[0].structural_level
    if grade < 1:
        raise ValueError("fixed same-level lower types must be recursive units")
    validate_unit_sequence(values, grade, SourceKind.TREND_TYPE)
    if any(not unit.child_ids for unit in values):
        raise ValueError("fixed same-level review requires exact lower type lineage")
    if not pending_lower_type_ids.issubset({unit.unit_id for unit in values}):
        raise ValueError("pending lower type ids must reference source units")
    visible_end = next(
        (offset for offset, unit in enumerate(values) if unit.available_at > as_of),
        len(values),
    )
    if any(unit.available_at <= as_of for unit in values[visible_end:]):
        raise ValueError("fixed same-level source availability must form a prefix")
    visible = values[:visible_end]

    candidates: list[FixedThreeMemberTypeCandidate] = []
    cursor = 0
    status = "source_covered_by_local_three_member_candidates"

    def candidate_for(group, zd_tick, zg_tick):
        return FixedThreeMemberTypeCandidate(
            candidate_id=stable_structure_id(
                "chanlun-fixed-three-member-consolidation-v1",
                view_id, values[0].price_basis_revision, grade,
                tuple(unit.unit_id for unit in group),
            ),
            target_structural_level=grade,
            member_unit_ids=tuple(unit.unit_id for unit in group),
            core_ticks=(zd_tick, zg_tick),
            market_start=group[0].market_start,
            market_end=group[-1].market_end,
            start_tick=group[0].start_tick,
            end_tick=group[-1].end_tick,
            known_at=max(unit.available_at for unit in group),
            lower_type_qualification_pending=any(
                unit.unit_id in pending_lower_type_ids for unit in group
            ),
        )

    while cursor < len(visible):
        group = visible[cursor:cursor + 3]
        if len(group) < 3 or any(
            not unit.locked
            for unit in group
        ):
            status = "pending_completed_lower_type_tail"
            break
        if any(unit.same_level_combination for unit in group):
            # One combined unit can contain several lower trend types.  It is
            # not silently counted as exactly one of the user's three 5m types.
            status = "pending_combined_lower_type_membership_review"
            break
        zd_tick = max(unit.low_tick for unit in group)
        zg_tick = min(unit.high_tick for unit in group)
        if zd_tick > zg_tick:
            status = "pending_nonoverlap_or_connector_repartition"
            break
        candidate = candidate_for(group, zd_tick, zg_tick)
        if candidates and (
            candidates[-1].market_end != candidate.market_start
            or candidates[-1].end_tick != candidate.start_tick
        ):
            raise ValueError("fixed same-level candidates must meet at one endpoint")
        candidates.append(candidate)
        cursor += 3

    if visible_end < len(values) and status == "source_covered_by_local_three_member_candidates":
        status = "pending_completed_lower_type_tail"

    # The first failed triple ends the connected-prefix proof, not the search
    # for later local centers.  Advance across at least one unresolved lower
    # type, then record disjoint post-gap triples without calling them a
    # certified whole-path partition or formal 30m types.
    post_gap: list[FixedThreeMemberTypeCandidate] = []
    if status in {
        "pending_nonoverlap_or_connector_repartition",
        "pending_combined_lower_type_membership_review",
    }:
        scan = cursor + 1
        while scan + 3 <= len(visible):
            group = visible[scan:scan + 3]
            if any(not unit.locked or unit.same_level_combination for unit in group):
                scan += 1
                continue
            zd_tick = max(unit.low_tick for unit in group)
            zg_tick = min(unit.high_tick for unit in group)
            if zd_tick > zg_tick:
                scan += 1
                continue
            post_gap.append(candidate_for(group, zd_tick, zg_tick))
            scan += 3

    return FixedSameLevelThreeMemberReview(
        view_id=view_id,
        operating_type_label=operating_type_label,
        target_structural_level=grade,
        source_unit_ids=tuple(unit.unit_id for unit in visible),
        candidates=tuple(candidates),
        post_gap_center_candidates=tuple(post_gap),
        pending_source_unit_ids=tuple(unit.unit_id for unit in visible[cursor:]),
        unavailable_future_count=len(values) - visible_end,
        status=status,
        as_of=as_of,
    )


def fixed_three_member_partition_payload(
    review: FixedSameLevelThreeMemberReview,
) -> dict[str, object]:
    """Export source-bounded candidates without issuing formal points."""

    def candidate_payload(item, *, post_gap: bool) -> dict[str, object]:
        return {
            "candidate_id": item.candidate_id,
            "member_unit_ids": list(item.member_unit_ids),
            "core_ticks": list(item.core_ticks),
            "market_start": int(item.market_start.timestamp()),
            "market_end": int(item.market_end.timestamp()),
            "start_tick": item.start_tick,
            "end_tick": item.end_tick,
            "known_at": int(item.known_at.timestamp()),
            "candidate_kind": "single_center_consolidation",
            "local_three_member_source_proven": True,
            "lower_type_qualification_pending": item.lower_type_qualification_pending,
            "fixed_view_local_type_completed": (
                not post_gap and not item.lower_type_qualification_pending
            ),
            "fixed_view_local_type_state": (
                "post_gap_center_observation_connector_pending" if post_gap
                else "completed_local_consolidation" if not item.lower_type_qualification_pending
                else "pending_lower_type_recomposition"
            ),
            "post_gap_observation": post_gap,
            "formal_type_issued": False,
            "tradable": False,
        }

    return {
        "schema": "chanlun-fixed-three-member-partition-review-v1",
        "rule_source_refs": ["L018:31-55", "L038:81-94", "L038:175-196"],
        "policy_basis": "declared_fixed_same_level_implementation",
        "view_id": review.view_id,
        "operating_type_label": review.operating_type_label,
        "target_structural_level": review.target_structural_level,
        "lower_trend_structural_level": review.target_structural_level - 1,
        "source_unit_ids": list(review.source_unit_ids),
        "status": review.status,
        "as_of": int(review.as_of.timestamp()),
        "candidates": [candidate_payload(item, post_gap=False) for item in review.candidates],
        "post_gap_center_candidates": [
            candidate_payload(item, post_gap=True)
            for item in review.post_gap_center_candidates
        ],
        "pending_source_unit_ids": list(review.pending_source_unit_ids),
        "unavailable_future_count": review.unavailable_future_count,
        "whole_path_partition_certified": False,
        "formal_point_from_review": False,
        "tradable": False,
    }


def observe_l038_six_member_split(
    units: tuple[ConstituentUnit, ...], *, as_of: datetime,
) -> L038SixMemberSplit | None:
    """Observe L038:81-94's six-member case under one common-overlap scope.

    This verifies the fixed no-extension view's two connected three-member
    pieces. It does not replace the canonical center lifecycle or certify a
    complete path outside these six locked lower trend types.
    """

    values = tuple(units)
    if len(values) != 6:
        raise ValueError("L038 split requires exactly six lower trend types")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("L038 split requires an aware cutoff")
    _validate_source_chain(values, frozenset())
    validate_unit_sequence(
        values, values[0].structural_level, SourceKind.TREND_TYPE,
    )
    if any(not unit.child_ids for unit in values):
        raise ValueError("L038 split requires exact lower type lineage")
    if any(unit.available_at > as_of for unit in values):
        return None

    def core(group):
        return max(unit.low_tick for unit in group), min(
            unit.high_tick for unit in group
        )

    shared = core(values)
    if shared[0] > shared[1]:
        return None
    left, right = values[:3], values[3:]
    return L038SixMemberSplit(
        observation_id=stable_structure_id(
            "chanlun-l038-no-extension-six-member-v1",
            values[0].price_basis_revision,
            values[0].structural_level,
            tuple(unit.unit_id for unit in values),
        ),
        source_grade=values[0].structural_level,
        price_basis_revision=values[0].price_basis_revision,
        source_unit_ids=tuple(unit.unit_id for unit in values),
        left_member_ids=tuple(unit.unit_id for unit in left),
        right_member_ids=tuple(unit.unit_id for unit in right),
        left_core_ticks=core(left),
        right_core_ticks=core(right),
        shared_six_core_ticks=shared,
        boundary_market_at=left[-1].market_end,
        boundary_tick=left[-1].end_tick,
        known_at=max(unit.available_at for unit in values),
    )


def l038_six_member_split_payload(
    observation: L038SixMemberSplit,
) -> dict[str, object]:
    return {
        "rule_id": "L038",
        "rule_source_refs": ["L038:81-94", "L038:175-196"],
        "status": "bounded_no_extension_six_member_split",
        "observation_id": observation.observation_id,
        "source_grade": observation.source_grade,
        # A recursive unit is already stamped with the level whose center it
        # can form.  Its child trend belongs to the preceding level.
        "target_grade": observation.source_grade,
        "lower_trend_grade": observation.source_grade - 1,
        "price_basis_revision": observation.price_basis_revision,
        "source_unit_ids": list(observation.source_unit_ids),
        "left_member_ids": list(observation.left_member_ids),
        "right_member_ids": list(observation.right_member_ids),
        "left_core_ticks": list(observation.left_core_ticks),
        "right_core_ticks": list(observation.right_core_ticks),
        "shared_six_core_ticks": list(observation.shared_six_core_ticks),
        "boundary_market_at": int(observation.boundary_market_at.timestamp()),
        "boundary_tick": observation.boundary_tick,
        "known_at": int(observation.known_at.timestamp()),
        "overlap_scope": "all_six_share_one_price_interval",
        "canonical_center_lifecycle_replaced": False,
        "whole_path_partition_certified": False,
        "formal_point_from_this_observation": False,
        "tradable": False,
    }


def _validate_source_chain(
    values: tuple[ConstituentUnit, ...],
    oscillatory_ids: frozenset[str],
) -> None:
    """验证除方向交替外的所有不变量。

    相邻同向走势完成合并前不能检查方向；其余因果和身份约束仍采用封闭失败。
    """

    if not values:
        if oscillatory_ids:
            raise ValueError("empty decomposition cannot declare oscillatory units")
        return
    if len({item.unit_id for item in values}) != len(values):
        raise ValueError("same-level source unit ids must be unique")
    if not oscillatory_ids.issubset({item.unit_id for item in values}):
        raise ValueError("oscillatory ids must reference source units")

    level = values[0].structural_level
    basis = values[0].price_basis_revision
    for item in values:
        if (
            item.structural_level != level
            or item.source_kind is not SourceKind.TREND_TYPE
        ):
            raise ValueError("same-level decomposition requires one trend-type level")
        if item.price_basis_revision != basis:
            raise ValueError("same-level decomposition cannot cross price basis")
        if not item.locked:
            raise ValueError("same-level decomposition requires locked trend types")

    for previous, current in zip(values, values[1:]):
        if previous.end_tick != current.start_tick:
            raise ValueError("same-level source prices must connect")
        if current.market_start < previous.market_end:
            raise ValueError("same-level source intervals must not overlap")
        if current.market_start > previous.market_end:
            raise ValueError("same-level source market endpoints must connect")


def _combination_leaves(item: ConstituentUnit) -> tuple[str, ...]:
    return item.child_ids if item.same_level_combination else (item.unit_id,)


def _combined_unit(
    group: tuple[ConstituentUnit, ...],
    protected_after_ids: frozenset[str],
) -> ConstituentUnit:
    first = group[0]
    last = group[-1]
    direction = first.direction
    child_ids = tuple(
        leaf_id for item in group for leaf_id in _combination_leaves(item)
    )
    inherited_protected = {
        leaf_id for item in group for leaf_id in item.protected_after_ids
    }
    explicit_protected = set()
    for item in group:
        leaves = _combination_leaves(item)
        explicit_protected.update(
            leaf_id for leaf_id in leaves if leaf_id in protected_after_ids
        )
        if item.unit_id in protected_after_ids:
            explicit_protected.add(leaves[-1])
    protected_leaves = tuple(
        leaf_id
        for leaf_id in child_ids
        if leaf_id in inherited_protected or leaf_id in explicit_protected
    )
    return ConstituentUnit(
        unit_id=stable_structure_id(
            "chanlun-same-level-combination",
            first.price_basis_revision,
            first.structural_level,
            direction,
            child_ids,
        ),
        structural_level=first.structural_level,
        source_kind=SourceKind.TREND_TYPE,
        price_basis_revision=first.price_basis_revision,
        direction=direction,
        start_tick=first.start_tick,
        end_tick=last.end_tick,
        low_tick=min(item.low_tick for item in group),
        high_tick=max(item.high_tick for item in group),
        market_start=first.market_start,
        market_end=last.market_end,
        confirmed_at=max(item.confirmed_at for item in group),
        available_at=max(item.available_at for item in group),
        locked=True,
        child_ids=child_ids,
        same_level_combination=True,
        protected_after_ids=protected_leaves,
        formed_at=max(item.formed_at or item.confirmed_at for item in group),
        low_market_time=combined_extreme_market_time(group, "low"),
        high_market_time=combined_extreme_market_time(group, "high"),
    )


def combine_same_level_trends(
    units: tuple[ConstituentUnit, ...],
    oscillatory_ids: frozenset[str],
    protected_after_ids: frozenset[str] = frozenset(),
) -> SameLevelDecompositionResult:
    """在递归构建中枢前应用同级别结合律。

    按结合律，连续同向的走势类型属于同一走势。盘整同样由首尾端点确定方向，
    不能作为同向走势之间的无方向连接件。``oscillatory_ids`` 只为读取旧回放调用
    保留，不再改变方向判定。``SameLevelCombination`` 会保留受保护子边界来源。

    输出具有确定性和前缀因果性：只合并已锁定的来源走势类型，组合结果不会早于
    最晚子单元可用；其 ``child_ids`` 保留回放与审计所需的完整单层来源。
    """

    values = tuple(units)
    oscillatory = frozenset(oscillatory_ids)
    protected = frozenset(protected_after_ids)
    _validate_source_chain(values, oscillatory)
    unit_ids = {item.unit_id for item in values}
    leaf_ids = tuple(
        leaf_id for item in values for leaf_id in _combination_leaves(item)
    )
    if len(set(leaf_ids)) != len(leaf_ids):
        raise ValueError("same-level combination leaves must not overlap")
    if not protected.issubset(unit_ids | set(leaf_ids)):
        raise ValueError("protected boundary ids must reference source units")
    if not values:
        return SameLevelDecompositionResult((), frozenset(), ())

    output: list[ConstituentUnit] = []
    combinations: list[SameLevelCombination] = []
    index = 0
    while index < len(values):
        current = values[index]
        end = index + 1
        while end < len(values):
            candidate = values[end]
            if candidate.direction != current.direction:
                break
            end += 1

        group = values[index:end]
        if len(group) == 1:
            output.append(current)
        else:
            combined = _combined_unit(group, protected)
            output.append(combined)
            combinations.append(
                SameLevelCombination(
                    combined_unit_id=combined.unit_id,
                    child_unit_ids=combined.child_ids,
                    direction=combined.direction,
                    protected_after_ids=combined.protected_after_ids,
                )
            )
        index = end

    result_units = tuple(output)
    # Consolidation is a movement classification, not a directionless bridge.
    # All same-direction runs have already been combined above.
    result_oscillatory = frozenset()
    validate_unit_sequence(
        result_units,
        result_units[0].structural_level,
        SourceKind.TREND_TYPE,
        result_oscillatory,
    )
    return SameLevelDecompositionResult(
        units=result_units,
        oscillatory_ids=result_oscillatory,
        combinations=tuple(combinations),
    )
