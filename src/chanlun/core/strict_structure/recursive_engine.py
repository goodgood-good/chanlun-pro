from __future__ import annotations

from collections import OrderedDict

from chanlun.core.strict_structure.center_machine import (
    calculate_centers,
    validate_unit_sequence,
)
from chanlun.core.strict_structure.models import (
    CenterState,
    SourceKind,
    StrictLevelResult,
    StrictStructureResult,
    center_seed_size,
)
from chanlun.core.strict_structure.trend_assembler import (
    IncompatibleDecompositionBoundaryError,
    assemble_trend_types,
    normalize_trend_assembly,
)
from chanlun.core.strict_structure.unit_adapter import build_recursive_unit_stream


_CENTER_PREFIX_CACHE_CAPACITY = 512


def _calculate_centers_with_prefix_cache(
    units,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str],
    *,
    hard_boundary_after_ids: frozenset[str] = frozenset(),
    boundary_closed_centers=(),
    center_prefix_cache: OrderedDict | None = None,
):
    """复用同一 CL 生命周期内不可变的锁定单元前缀计算。"""

    values = tuple(units)
    # 未锁定观察尾部的 available_at 会随决策时点变化，绝不缓存。边界扫描传入的
    # 正式前缀全部锁定，其 unit_id 在同一 UnitLockRegistry 内已证明不可变。
    cacheable = center_prefix_cache is not None and all(item.locked for item in values)
    if not cacheable:
        return calculate_centers(
            values,
            structural_level,
            source_kind,
            oscillatory_ids,
            hard_boundary_after_ids=hard_boundary_after_ids,
            boundary_closed_centers=tuple(boundary_closed_centers),
        )
    closed = tuple(boundary_closed_centers)
    key = (
        "center",
        structural_level,
        SourceKind(source_kind).value,
        tuple(
            (
                item.unit_id,
                item.confirmed_at,
                item.available_at,
                item.formed_at,
            )
            for item in values
        ),
        oscillatory_ids,
        hard_boundary_after_ids,
        closed,
    )
    cached = center_prefix_cache.pop(key, None)
    if cached is not None:
        center_prefix_cache[key] = cached
        return cached
    result = calculate_centers(
        values,
        structural_level,
        source_kind,
        oscillatory_ids,
        hard_boundary_after_ids=hard_boundary_after_ids,
        boundary_closed_centers=closed,
    )
    center_prefix_cache[key] = result
    while len(center_prefix_cache) > _CENTER_PREFIX_CACHE_CAPACITY:
        center_prefix_cache.popitem(last=False)
    return result


def _assemble_trends_with_prefix_cache(
    centers,
    units,
    structural_level: int,
    oscillatory_ids: frozenset[str],
    *,
    strength,
    group_start_unit_id: str | None = None,
    ignored_boundary_anchor_ids: frozenset[str] = frozenset(),
    center_prefix_cache: OrderedDict | None = None,
):
    """复用锁定前缀的走势装配；尾部或无强度路径继续直接计算。"""

    center_values = tuple(centers)
    unit_values = tuple(units)
    cacheable = (
        center_prefix_cache is not None
        and strength is not None
        and all(item.locked for item in unit_values)
    )
    if not cacheable:
        return assemble_trend_types(
            center_values,
            unit_values,
            structural_level,
            oscillatory_ids,
            strength=strength,
            ignored_boundary_anchor_ids=ignored_boundary_anchor_ids,
            group_start_unit_id=group_start_unit_id,
        )
    key = (
        "trend",
        structural_level,
        tuple(
            (
                item.unit_id,
                item.confirmed_at,
                item.available_at,
                item.formed_at,
            )
            for item in unit_values
        ),
        center_values,
        oscillatory_ids,
        group_start_unit_id,
        ignored_boundary_anchor_ids,
    )
    cached = center_prefix_cache.pop(key, None)
    if cached is not None:
        center_prefix_cache[key] = cached
        return cached
    result = assemble_trend_types(
        center_values,
        unit_values,
        structural_level,
        oscillatory_ids,
        strength=strength,
        ignored_boundary_anchor_ids=ignored_boundary_anchor_ids,
        group_start_unit_id=group_start_unit_id,
    )
    center_prefix_cache[key] = result
    while len(center_prefix_cache) > _CENTER_PREFIX_CACHE_CAPACITY:
        center_prefix_cache.popitem(last=False)
    return result


def calculate_level_with_divergence_boundaries(
    units,
    structural_level: int,
    source_kind: SourceKind,
    oscillatory_ids: frozenset[str] = frozenset(),
    *,
    strength=None,
    center_prefix_cache: OrderedDict | None = None,
):
    """把唯一同级别账本计算到因果边界不再变化。"""

    values = tuple(units)
    unit_index = {item.unit_id: index for index, item in enumerate(values)}
    if strength is None:
        center_result = _calculate_centers_with_prefix_cache(
            values,
            structural_level,
            source_kind,
            oscillatory_ids,
            center_prefix_cache=center_prefix_cache,
        )
        return center_result, assemble_trend_types(
            center_result.centers,
            values,
            structural_level,
            oscillatory_ids,
        )
    accepted: frozenset[str] = frozenset()
    rejected: frozenset[str] = frozenset()
    closed_centers = {}
    frozen_current = {}
    frozen_completed = {}
    frozen_boundaries = {}
    last_boundary_index = -1

    def merge_unique(target, items, identifier, label):
        for item in items:
            item_id = getattr(item, identifier)
            previous = target.setdefault(item_id, item)
            if previous != item:
                raise ValueError(f"{label} id maps to conflicting evidence")

    locked_count = 0
    for item in values:
        if not item.locked:
            break
        locked_count += 1

    for _pass in range(len(values) + 1):
        discovered = None
        restart_after_rejection = False
        # 离开段之后可能折叠回中枢本体。通过重放锁定前缀，使首个已确认背驰在
        # 批量、增量和重启计算中始终保持为不可变边界。
        for prefix_end in range(last_boundary_index + 1, locked_count):
            prefix = values[: prefix_end + 1]
            prefix_centers = _calculate_centers_with_prefix_cache(
                prefix,
                structural_level,
                source_kind,
                oscillatory_ids,
                hard_boundary_after_ids=accepted,
                boundary_closed_centers=tuple(closed_centers.values()),
                center_prefix_cache=center_prefix_cache,
            )
            centers_for_trends = tuple(
                center
                for index, center in enumerate(prefix_centers.centers)
                if unit_index[
                    (
                        center.body_units[0].unit_id
                        if center.entry_unit is None
                        else center.entry_unit.unit_id
                    )
                ]
                > last_boundary_index
                and (
                    center.structurally_closed
                    or index == len(prefix_centers.centers) - 1
                )
            )
            try:
                suffix = _assemble_trends_with_prefix_cache(
                    centers_for_trends,
                    prefix,
                    structural_level,
                    oscillatory_ids,
                    strength=strength,
                    ignored_boundary_anchor_ids=rejected.intersection(
                        item.unit_id for item in prefix
                    ),
                    group_start_unit_id=(
                        None
                        if last_boundary_index < 0
                        else values[last_boundary_index + 1].unit_id
                    ),
                    center_prefix_cache=center_prefix_cache,
                )
            except IncompatibleDecompositionBoundaryError as exc:
                rejected = rejected | {exc.anchor_unit_id}
                restart_after_rejection = True
                break
            candidates = tuple(
                boundary
                for boundary in suffix.decomposition_boundaries
                if unit_index[boundary.anchor_unit_id] > last_boundary_index
            )
            if candidates:
                discovered = (
                    min(
                        candidates,
                        key=lambda boundary: (
                            unit_index[boundary.anchor_unit_id],
                            boundary.available_at,
                            boundary.boundary_id,
                        ),
                    ),
                    suffix,
                )
                break

        if restart_after_rejection:
            continue

        if discovered is None:
            center_result = _calculate_centers_with_prefix_cache(
                values,
                structural_level,
                source_kind,
                oscillatory_ids,
                hard_boundary_after_ids=accepted,
                boundary_closed_centers=tuple(closed_centers.values()),
                center_prefix_cache=center_prefix_cache,
            )
            centers_for_trends = tuple(
                center
                for index, center in enumerate(center_result.centers)
                if unit_index[
                    (
                        center.body_units[0].unit_id
                        if center.entry_unit is None
                        else center.entry_unit.unit_id
                    )
                ]
                > last_boundary_index
                and (
                    center.structurally_closed
                    or index == len(center_result.centers) - 1
                )
            )
            try:
                suffix = _assemble_trends_with_prefix_cache(
                    centers_for_trends,
                    values,
                    structural_level,
                    oscillatory_ids,
                    strength=strength,
                    ignored_boundary_anchor_ids=rejected,
                    group_start_unit_id=(
                        None
                        if last_boundary_index < 0
                        or last_boundary_index + 1 >= len(values)
                        else values[last_boundary_index + 1].unit_id
                    ),
                    center_prefix_cache=center_prefix_cache,
                )
            except IncompatibleDecompositionBoundaryError as exc:
                rejected = rejected | {exc.anchor_unit_id}
                continue
            merged_current = dict(frozen_current)
            merge_unique(
                merged_current,
                suffix.current_trends,
                "trend_id",
                "current trend",
            )
            merged_completed = dict(frozen_completed)
            merge_unique(
                merged_completed,
                suffix.completed_trends,
                "trend_id",
                "completed trend",
            )
            current_trends = tuple(merged_current.values())
            normalized = normalize_trend_assembly(
                current_trends=current_trends,
                completed_trends=tuple(
                    sorted(
                        merged_completed.values(),
                        key=lambda trend: (trend.available_at, trend.trend_id),
                    )
                ),
                decomposition_boundaries=tuple(
                    sorted(
                        frozen_boundaries.values(),
                        key=lambda boundary: (
                            boundary.available_at,
                            boundary.boundary_id,
                        ),
                    )
                ),
                source_units=values,
                structural_level=structural_level,
            )
            return center_result, normalized

        candidate, suffix = discovered
        current_ids = [trend.trend_id for trend in suffix.current_trends]
        try:
            boundary_trend_index = current_ids.index(candidate.left_trend_id)
        except ValueError as exc:
            raise ValueError("boundary trend is missing from suffix assembly") from exc

        # A divergence is allowed to end a movement only when the resulting
        # same-level ledger still connects up/down/up/down.  In particular, a
        # second same-direction divergence after an immutable boundary remains
        # observable evidence but cannot become another decomposition boundary.
        if frozen_current:
            try:
                normalize_trend_assembly(
                    current_trends=tuple(
                        (
                            *frozen_current.values(),
                            *suffix.current_trends[: boundary_trend_index + 1],
                        )
                    ),
                    completed_trends=tuple(
                        (*frozen_completed.values(), *suffix.completed_trends)
                    ),
                    decomposition_boundaries=tuple(
                        (*frozen_boundaries.values(), candidate)
                    ),
                    source_units=values[: unit_index[candidate.anchor_unit_id] + 1],
                    structural_level=structural_level,
                )
            except IncompatibleDecompositionBoundaryError as exc:
                rejected = rejected | {exc.anchor_unit_id}
                continue
        merge_unique(
            frozen_current,
            suffix.current_trends[: boundary_trend_index + 1],
            "trend_id",
            "current trend",
        )
        merge_unique(
            frozen_completed,
            tuple(
                trend
                for trend in suffix.completed_trends
                if trend.market_end <= candidate.anchor_at
                and (
                    candidate.terminal_center_id
                    not in {center.center_id for center in trend.centers}
                    or trend.terminal_divergence == candidate.divergence
                )
            ),
            "trend_id",
            "completed trend",
        )
        merge_unique(
            frozen_boundaries,
            (candidate,),
            "boundary_id",
            "decomposition boundary",
        )
        accepted = accepted | {candidate.anchor_unit_id}
        boundary_trend = suffix.current_trends[boundary_trend_index]
        terminal_center = boundary_trend.centers[-1]
        if (
            terminal_center.center_id != candidate.terminal_center_id
            or terminal_center.state is not CenterState.DIVERGENCE_CLOSED
        ):
            raise ValueError("boundary terminal center is not causally closed")
        previous_center = closed_centers.setdefault(
            candidate.anchor_unit_id,
            terminal_center,
        )
        if previous_center != terminal_center:
            raise ValueError("boundary center identity collision")
        last_boundary_index = unit_index[candidate.anchor_unit_id]
    raise RuntimeError("center/divergence decomposition did not converge")


class StrictRecursiveEngine:
    def __init__(
        self,
        max_levels: int = 50,
        *,
        center_prefix_cache: OrderedDict | None = None,
    ) -> None:
        if type(max_levels) is not int or max_levels < 1:
            raise ValueError("max_levels must be >= 1")
        if center_prefix_cache is not None and not isinstance(
            center_prefix_cache,
            OrderedDict,
        ):
            raise TypeError("center_prefix_cache must be an OrderedDict")
        self.max_levels = max_levels
        self.center_prefix_cache = center_prefix_cache

    def calculate(
        self,
        base_units,
        *,
        price_basis_revision: str | None = None,
        strength=None,
    ) -> StrictStructureResult:
        units = tuple(base_units)
        if units:
            inferred_basis = units[0].price_basis_revision
            if price_basis_revision is None:
                price_basis_revision = inferred_basis
            if price_basis_revision != inferred_basis or any(
                item.price_basis_revision != price_basis_revision for item in units
            ):
                raise ValueError("strict recursion cannot cross price basis")
        elif not price_basis_revision:
            raise ValueError("empty strict recursion requires price basis")

        levels = []
        # Legacy replay metadata is retained as an empty value only.  Every
        # physical and recursive level now follows the same strict alternation.
        oscillatory_ids: frozenset[str] = frozenset()
        for level in range(self.max_levels):
            source_kind = SourceKind.SEGMENT if level == 0 else SourceKind.TREND_TYPE
            validate_unit_sequence(units, level, source_kind, oscillatory_ids)
            # 物理层至少需要进入 + 中间三段核心 + 独立离开五个角色；更高层
            # 仍由三个已完成低级别走势类型递归，不能混用物理线段门槛。
            minimum_units = (
                center_seed_size(source_kind)
                if source_kind is SourceKind.TREND_TYPE
                else 5
            )
            if len(units) < minimum_units:
                break

            # 已确认背驰结束当前同级别走势。随后必须按分区重放中枢几何，避免后续
            # 中枢借用不可变边界两侧的单元。重复计算到稳定点，也允许新暴露后缀
            # 发现另一个已确认边界，而无需未来数据回填。
            center_result, assembly = calculate_level_with_divergence_boundaries(
                units,
                level,
                source_kind,
                oscillatory_ids,
                strength=strength,
                center_prefix_cache=self.center_prefix_cache,
            )
            levels.append(
                StrictLevelResult(
                    structural_level=level,
                    units=units,
                    center_result=center_result,
                    trend_types=assembly.current_trends,
                    completed_trends=assembly.completed_trends,
                    decomposition_boundaries=assembly.decomposition_boundaries,
                )
            )
            # 正式递归仍只消费锁定走势；唯一当前的完成/形成中走势作为未锁定
            # 观察尾部随账本上递归，使高级别小转大二类点能在回抽锁定前进入
            # 观察列表。该尾部不会进入正式中枢、背驰或已确认点。
            next_units, next_oscillatory_ids = build_recursive_unit_stream(
                assembly.current_trends,
                frozenset(
                    boundary.left_trend_id
                    for boundary in assembly.decomposition_boundaries
                ),
            )
            if len(next_units) < 3:
                break
            try:
                validate_unit_sequence(
                    next_units,
                    level + 1,
                    SourceKind.TREND_TYPE,
                    next_oscillatory_ids,
                )
            except ValueError:
                # 不交替、断链或跨 basis 的 LOCKED 走势不能被“挤”成
                # 高一级中枢；保留已完成的当前层并停止正式递归。
                break
            units = next_units
            oscillatory_ids = next_oscillatory_ids

        return StrictStructureResult(
            schema="chanlun-structure",
            price_basis_revision=price_basis_revision,
            levels=tuple(levels),
        )
