"""从唯一因果一分钟结构图生成忠于缠论原义的 30 分钟策略。

逻辑策略级别在不改变原始结构语义的前提下映射：

* L0 / 30 分钟策略 = 递归结构第 2 层；
* L1 / 5 分钟战术回返 = 递归结构第 1 层，由第 2 层中枢的已完成离开段和
  首次回返段表达；
* L2 / 1 分钟定位 = 递归结构第 0 层，且必须是该精确首次回返单元的后代。

九段推导只作为佐证或重新分类证据。自然递归得到的标准中枢是唯一信号权威；
未解决的九段歧义或相关中枢扩展会阻断该链，而不会建立第二条信号通道。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from chanlun.core.strict_structure.models import (
    CenterState,
    StrictEvidenceResult,
    StrictPointEvidence,
)
from chanlun.core.strict_structure.upgrade_evidence import (
    UpgradeEvidenceKind,
    collect_recursive_upgrade_evidence,
)
from chanlun.decision_support.fingerprints import normalize_datetime, sha256_json
from chanlun.decision_support.trading_system.models import (
    StructuralPoint,
)
from chanlun.decision_support.trading_system.structure_adapter import (
    extract_confirmed_points,
    structural_point_id_map,
)
from chanlun.decision_support.trading_system.direct_entry_snapshot_adapter import (
    build_direct_recursive_technical_entry_snapshot,
)
from chanlun.decision_support.trading_system.selection import (
    TechnicalEntrySnapshot,
)


AlignmentStatus = Literal["PASS", "REJECT"]
DIRECT_RECURSIVE_ALIGNMENT_CONTRACT_ID = (
    "DIRECT_RECURSIVE_1M_RAW_LEVELS_2_1_0"
)


@dataclass(frozen=True, slots=True)
class DirectRecursiveAlignmentContract:
    contract_id: str = DIRECT_RECURSIVE_ALIGNMENT_CONTRACT_ID
    source_frequency: str = "1m"
    l0_raw_recursive_level: int = 2
    l1_raw_recursive_level: int = 1
    l2_raw_recursive_level: int = 0
    l0_logical_frequency: str = "30m"
    l1_logical_frequency: str = "5m"
    l2_logical_frequency: str = "1m"
    locator_scope: str = "EXACT_DIRECT_FIRST_RETURN_DESCENDANTS"
    second_buy_policy: str = "CANONICAL_CONFIRMED_FIRST_OR_SECOND_POINT"
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        expected = (
            self.contract_id,
            self.source_frequency,
            self.l0_raw_recursive_level,
            self.l1_raw_recursive_level,
            self.l2_raw_recursive_level,
            self.l0_logical_frequency,
            self.l1_logical_frequency,
            self.l2_logical_frequency,
            self.locator_scope,
            self.second_buy_policy,
            self.live_status,
        )
        if expected != (
            DIRECT_RECURSIVE_ALIGNMENT_CONTRACT_ID,
            "1m",
            2,
            1,
            0,
            "30m",
            "5m",
            "1m",
            "EXACT_DIRECT_FIRST_RETURN_DESCENDANTS",
            "CANONICAL_CONFIRMED_FIRST_OR_SECOND_POINT",
            "LIVE_DISABLED",
        ):
            raise ValueError("direct recursive alignment contract changed")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))

    def document(self) -> dict[str, object]:
        value = asdict(self)
        value["parameter_set_id"] = self.parameter_set_id
        return value


def direct_recursive_alignment_contract() -> DirectRecursiveAlignmentContract:
    return DirectRecursiveAlignmentContract()


@dataclass(frozen=True, slots=True)
class DirectRecursivePointFact:
    point: StructuralPoint
    anchor_unit_id: str


@dataclass(frozen=True, slots=True)
class DirectRecursiveEntryChain:
    l0_point_id: str
    l0_center_id: str
    l1_departure_unit_id: str
    l1_return_unit_id: str
    l2_locator_point_id: str
    decision_at: datetime
    first_return_low: Decimal
    l0_zg: Decimal
    l2_confirmation_bar_high: Decimal
    structural_invalidation_price: Decimal
    provenance_unit_ids: tuple[str, ...]
    nine_segment_evidence_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "decision_at",
            normalize_datetime(self.decision_at, "decision_at"),
        )
        for field in ("provenance_unit_ids", "nine_segment_evidence_ids"):
            values = tuple(getattr(self, field))
            object.__setattr__(self, field, values)
            if not values and field == "provenance_unit_ids":
                raise ValueError("direct recursive chain requires provenance")
            if len(values) != len(set(values)):
                raise ValueError(f"{field} must be unique")
        if self.first_return_low < self.l0_zg:
            raise ValueError("direct recursive first return must hold L0 ZG")
        if (
            self.l2_confirmation_bar_high <= 0
            or self.structural_invalidation_price <= 0
        ):
            raise ValueError("direct recursive execution boundaries must be positive")


@dataclass(frozen=True, slots=True)
class DirectRecursiveAlignmentDecision:
    l0_point_id: str
    status: AlignmentStatus
    reason_codes: tuple[str, ...]
    chain: DirectRecursiveEntryChain | None
    relevant_expansion_ids: tuple[str, ...] = ()
    unresolved_nine_segment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        object.__setattr__(
            self, "relevant_expansion_ids", tuple(self.relevant_expansion_ids)
        )
        object.__setattr__(
            self,
            "unresolved_nine_segment_ids",
            tuple(self.unresolved_nine_segment_ids),
        )
        if self.status == "PASS" and (self.chain is None or self.reason_codes):
            raise ValueError("passing direct recursion requires one clean chain")
        if self.status == "REJECT" and (self.chain is not None or not self.reason_codes):
            raise ValueError("rejected direct recursion requires reasons")

    def document(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class DirectRecursiveStructurePath:
    symbol: str
    structure_revision: str
    structure_snapshot_id: str
    strategic_points: tuple[StructuralPoint, ...]
    decisions: tuple[DirectRecursiveAlignmentDecision, ...]
    technical_entries: tuple[TechnicalEntrySnapshot, ...]
    rejection_counts: tuple[tuple[str, int], ...]
    resolved_nine_segment_count: int
    unresolved_nine_segment_count: int
    relevant_expansion_count: int
    grade: Literal["RESEARCH_ONLY", "UNRESOLVED"]
    live_status: str = "LIVE_DISABLED"

    @property
    def aligned_entry_count(self) -> int:
        return len(self.technical_entries)


@dataclass(frozen=True, slots=True)
class _Trace:
    all_ids: frozenset[str]
    level_zero_leaf_ids: frozenset[str]


def _point_facts(
    evidence: StrictEvidenceResult,
    *,
    code: str,
) -> tuple[DirectRecursivePointFact, ...]:
    points = extract_confirmed_points(
        evidence,
        code=code,
        source_frequency="1m",
        as_of=evidence.source_closed_at,
    )
    converted_ids = structural_point_id_map(
        evidence.confirmed_points,
        code=code,
        source_frequency="1m",
    )
    raw_by_id: dict[str, StrictPointEvidence] = {}
    for raw in evidence.confirmed_points:
        point_id = converted_ids[raw.point_id]
        previous = raw_by_id.setdefault(point_id, raw)
        if previous != raw:
            raise ValueError("direct recursive point identity collision")
    return tuple(
        DirectRecursivePointFact(point, raw_by_id[point.point_id].anchor_unit_id)
        for point in points
    )


def _provenance_graph(structure) -> dict[str, tuple[int, tuple[str, ...]]]:
    graph: dict[str, tuple[int, tuple[str, ...]]] = {}

    def record(identity: str, level: int, children: tuple[str, ...]) -> None:
        value = (level, tuple(children))
        previous = graph.setdefault(identity, value)
        if previous != value:
            raise ValueError("recursive provenance identity changed")

    for level in structure.levels:
        for unit in level.units:
            record(unit.unit_id, unit.structural_level, unit.child_ids)
        for trend in level.trend_types + level.completed_trends:
            record(
                trend.trend_id,
                trend.structural_level + 1,
                tuple(item.unit_id for item in trend.constituent_units),
            )
    return graph


def _trace(root_id: str, graph: dict[str, tuple[int, tuple[str, ...]]]) -> _Trace:
    visited: set[str] = set()
    active: set[str] = set()
    leaves: set[str] = set()

    def visit(identity: str) -> None:
        if identity in active:
            raise ValueError("recursive provenance contains a cycle")
        if identity in visited:
            return
        try:
            level, children = graph[identity]
        except KeyError as exc:
            raise ValueError("recursive provenance references an unknown child") from exc
        active.add(identity)
        visited.add(identity)
        if not children:
            if level != 0:
                raise ValueError("recursive provenance terminated above level zero")
            leaves.add(identity)
        else:
            for child in children:
                visit(child)
        active.remove(identity)

    visit(root_id)
    if not leaves:
        raise ValueError("recursive provenance has no level-zero leaves")
    return _Trace(frozenset(visited), frozenset(leaves))


def build_direct_recursive_structure_path(
    *,
    evidence: StrictEvidenceResult,
    code: str,
) -> DirectRecursiveStructurePath:
    """构建一个最终快照中所有可见的因果 30 分钟策略链。"""

    if evidence.symbol != code or evidence.source_frequency != "1m":
        raise ValueError("direct recursive structure requires matching 1m evidence")
    if len(evidence.structure.levels) < 3:
        return DirectRecursiveStructurePath(
            symbol=code,
            structure_revision=evidence.structure_revision,
            structure_snapshot_id=sha256_json(
                {
                    "schema": "chanlun-direct-recursive-structure",
                    "structure_revision": evidence.structure_revision,
                    "status": "LESS_THAN_THREE_RECURSIVE_LEVELS",
                }
            ),
            strategic_points=(),
            decisions=(),
            technical_entries=(),
            rejection_counts=(("LESS_THAN_THREE_RECURSIVE_LEVELS", 1),),
            resolved_nine_segment_count=0,
            unresolved_nine_segment_count=0,
            relevant_expansion_count=0,
            grade="UNRESOLVED",
        )

    facts = _point_facts(evidence, code=code)
    facts_by_id = {fact.point.point_id: fact for fact in facts}
    strategic = tuple(
        fact
        for fact in facts
        if fact.point.recursive_level == 2
        and fact.point.point_type == "3buy"
        and fact.point.center_ordinal == 1
    )
    locator_facts = tuple(
        fact
        for fact in facts
        if fact.point.recursive_level == 0
        and fact.point.point_type in {"1buy", "2buy"}
    )
    level_two = evidence.structure.levels[2]
    centers = {value.center_id: value for value in level_two.center_result.centers}
    graph = _provenance_graph(evidence.structure)
    decisions: list[DirectRecursiveAlignmentDecision] = []
    resolved_nine_ids: set[str] = set()
    unresolved_nine_ids: set[str] = set()
    relevant_expansion_ids: set[str] = set()

    for fact in strategic:
        point = fact.point
        center = centers.get(point.center_id or "")
        reasons: list[str] = []
        if (
            center is None
            or center.state is not CenterState.COMPLETED
            or center.completion_leave_unit is None
            or center.completion_return_unit is None
        ):
            reasons.append("L0_30M_STANDARD_CENTER_NOT_COMPLETED")
            decisions.append(
                DirectRecursiveAlignmentDecision(
                    point.point_id, "REJECT", tuple(reasons), None
                )
            )
            continue
        leave = center.completion_leave_unit
        first_return = center.completion_return_unit
        if fact.anchor_unit_id != first_return.unit_id:
            raise ValueError("L0 third-buy anchor changed from its first return")
        leave_trace = _trace(leave.unit_id, graph)
        return_trace = _trace(first_return.unit_id, graph)
        center_ids: set[str] = set()
        for unit in center.body_units:
            center_ids.update(_trace(unit.unit_id, graph).all_ids)
        chain_ids = (
            center_ids
            | set(leave_trace.all_ids)
            | set(return_trace.all_ids)
        )

        upgrades = collect_recursive_upgrade_evidence(
            evidence.structure,
            as_of=point.available_at,
        )
        nine = tuple(
            item
            for item in upgrades
            if item.kind is UpgradeEvidenceKind.NINE_SEGMENT_DERIVATION
            and item.target_level == 2
            and bool(set(item.source_unit_ids) & chain_ids)
        )
        resolved_nine = tuple(
            item for item in nine if item.resolved_by_standard_center_id == center.center_id
        )
        unresolved_nine = tuple(
            item for item in nine if item.resolved_by_standard_center_id != center.center_id
        )
        resolved_nine_ids.update(item.evidence_id for item in resolved_nine)
        unresolved_nine_ids.update(item.evidence_id for item in unresolved_nine)
        if unresolved_nine:
            reasons.append("UNRESOLVED_NINE_SEGMENT_RECLASSIFICATION")

        expansions = tuple(
            item
            for item in upgrades
            if item.kind is UpgradeEvidenceKind.CENTER_EXPANSION
            and (
                bool(set(item.source_unit_ids) & chain_ids)
                or center.center_id in item.source_center_ids
            )
        )
        relevant_expansion_ids.update(item.evidence_id for item in expansions)
        if expansions:
            reasons.append("ACTIVE_CENTER_EXPANSION_RECLASSIFYING")

        eligible_locators = tuple(
            sorted(
                (
                    item
                    for item in locator_facts
                    if item.anchor_unit_id in return_trace.level_zero_leaf_ids
                    and first_return.market_start
                    <= item.point.anchor_at
                    <= first_return.market_end
                    and item.point.available_at <= point.available_at
                ),
                key=lambda item: (item.point.available_at, item.point.point_id),
            )
        )
        locator = eligible_locators[0] if eligible_locators else None
        if locator is None:
            reasons.append("NO_L2_1M_LOCATOR_IN_DIRECT_FIRST_RETURN")

        if reasons:
            decisions.append(
                DirectRecursiveAlignmentDecision(
                    l0_point_id=point.point_id,
                    status="REJECT",
                    reason_codes=tuple(dict.fromkeys(reasons)),
                    chain=None,
                    relevant_expansion_ids=tuple(
                        sorted(item.evidence_id for item in expansions)
                    ),
                    unresolved_nine_segment_ids=tuple(
                        sorted(item.evidence_id for item in unresolved_nine)
                    ),
                )
            )
            continue

        assert locator is not None
        chain = DirectRecursiveEntryChain(
            l0_point_id=point.point_id,
            l0_center_id=center.center_id,
            l1_departure_unit_id=leave.unit_id,
            l1_return_unit_id=first_return.unit_id,
            l2_locator_point_id=locator.point.point_id,
            decision_at=max(point.available_at, locator.point.available_at),
            first_return_low=(
                evidence.structure_price_quantum * first_return.low_tick
            ),
            l0_zg=Decimal(str(point.center_zg)),
            l2_confirmation_bar_high=Decimal(
                str(locator.point.structure_anchor_price)
            ),
            structural_invalidation_price=Decimal(
                str(point.structure_invalidation_price)
            ),
            provenance_unit_ids=tuple(sorted(chain_ids)),
            nine_segment_evidence_ids=tuple(
                sorted(item.evidence_id for item in resolved_nine)
            ),
        )
        decisions.append(
            DirectRecursiveAlignmentDecision(
                l0_point_id=point.point_id,
                status="PASS",
                reason_codes=(),
                chain=chain,
            )
        )

    snapshot_id = sha256_json(
        {
            "schema": "chanlun-direct-recursive-structure",
            "symbol": code,
            "source_frequency": "1m",
            "logical_level_mapping": {"L0": 2, "L1": 1, "L2": 0},
            "structure_revision": evidence.structure_revision,
            "decisions": tuple(value.document() for value in decisions),
        }
    )
    technical_entries = tuple(
        build_direct_recursive_technical_entry_snapshot(
            structure_snapshot_id=snapshot_id,
            observed_at=decision.chain.decision_at,
            chain=decision.chain,
            l0_three_buy=facts_by_id[decision.chain.l0_point_id].point,
            l2_locator=facts_by_id[decision.chain.l2_locator_point_id].point,
        )
        for decision in decisions
        if decision.chain is not None
    )
    rejection_counter = Counter(
        code for decision in decisions for code in decision.reason_codes
    )
    return DirectRecursiveStructurePath(
        symbol=code,
        structure_revision=evidence.structure_revision,
        structure_snapshot_id=snapshot_id,
        strategic_points=tuple(item.point for item in strategic),
        decisions=tuple(decisions),
        technical_entries=technical_entries,
        rejection_counts=tuple(sorted(rejection_counter.items())),
        resolved_nine_segment_count=len(resolved_nine_ids),
        unresolved_nine_segment_count=len(unresolved_nine_ids),
        relevant_expansion_count=len(relevant_expansion_ids),
        grade="RESEARCH_ONLY",
    )


__all__ = (
    "DIRECT_RECURSIVE_ALIGNMENT_CONTRACT_ID",
    "DirectRecursiveAlignmentContract",
    "DirectRecursiveAlignmentDecision",
    "DirectRecursiveEntryChain",
    "DirectRecursivePointFact",
    "DirectRecursiveStructurePath",
    "build_direct_recursive_structure_path",
    "direct_recursive_alignment_contract",
)
