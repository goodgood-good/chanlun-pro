"""人机协同实时选股与因果回放共用的唯一决策核心。

本模块同时负责评估与决策序列化。网页可以增加仅用于展示的字段（例如图表
链接），但不能重新解释信号。历史回放接收完全相同的
``SymbolStructureBundle``，并调用同一个核心。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from chanlun.core.strict_structure.base_profile import (
    STRICT_BASE_PROFILE_ID,
    STRICT_STROKE_MODE,
    strict_base_config_revision,
)
from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.engine import (
    EvaluatedSignal,
    SymbolStructureBundle,
    TradingEngine,
)
from chanlun.decision_support.trading_system.direct_recursive_structure import (
    direct_recursive_alignment_contract,
)
from chanlun.decision_support.trading_system.models import (
    SectorAssessment,
    StructuralPoint,
    TimeframeContext,
    TradingPolicy,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate
from chanlun.decision_support.trading_system.screening_structure import (
    SCREENING_STRUCTURE_SCOPE,
)


DECISION_CORE_SCHEMA = "chanlun-human-assisted-decision-core"
SIGNAL_DECISION_DOCUMENT_SCHEMA = "chanlun-human-assisted-signal-decision"
MONITOR_ONLY_BUY_REASON_CODE = "current_qmt_sector_trigger_required"

_SIGNAL_DECISION_FIELDS = (
    "decision_core_id",
    "signal_id",
    "setup_id",
    "point_id",
    "code",
    "point_type",
    "side",
    "tower",
    "recursive_level",
    "lifecycle_stage",
    "observed_at",
    "structure_scope",
    "structure_frequencies",
    "stroke_mode",
    "recursive_structure_used",
    "physical_timeframe_recursive",
    "context_d",
    "context_30m",
    "setup_5m",
    "trigger_1m",
    "entry_execution_boundary",
    "sector",
    "structural_stop",
    "risk_multiplier",
    "technical_entry_allowed",
    "entry_allowed",
    "exit_allowed",
    "exit_action",
    "decision_reasons",
    "conflict",
    "warmup",
    "selection_sources",
    "sector_triggered",
    "monitor_only",
    "human_confirmation_required",
    "automated_order_authorized",
    "live_status",
)
_HIGHER_TIMEFRAME_DECISION_FIELDS = (
    "market_gate",
    "sector_gate",
    "symbol_gate",
    "market_states",
    "sector_states",
    "symbol_states",
    "market_reason_codes",
    "sector_reason_codes",
    "symbol_reason_codes",
    "reason_codes",
    "market_period_diagnostics",
    "sector_period_diagnostics",
    "symbol_period_diagnostics",
    "new_entry_requires_all_green",
)


def _trading_policy_document(policy: TradingPolicy) -> dict[str, object]:
    """无精度损失地序列化所有影响决策的策略字段。"""

    return {
        "require_confirmed_five_minute": policy.require_confirmed_five_minute,
        "require_confirmed_one_minute": policy.require_confirmed_one_minute,
        "require_sector_eligibility": policy.require_sector_eligibility,
        "require_thirty_minute_context": policy.require_thirty_minute_context,
        "first_center_three_buy_only": policy.first_center_three_buy_only,
        "minimum_tick": format(policy.minimum_tick, "f"),
        "first_buy_risk_multiplier": format(
            policy.first_buy_risk_multiplier, "f"
        ),
        "second_buy_risk_multiplier": format(
            policy.second_buy_risk_multiplier, "f"
        ),
        "third_buy_risk_multiplier": format(
            policy.third_buy_risk_multiplier, "f"
        ),
        "max_five_minute_setup_age_seconds": (
            policy.max_five_minute_setup_age_seconds
        ),
    }


@dataclass(frozen=True, slots=True)
class HumanAssistedDecisionContract:
    """选股、盘中监听与回放共同使用的冻结决策逻辑身份。"""

    policy: TradingPolicy
    schema: str = DECISION_CORE_SCHEMA
    higher_context_frequency: str = "d"
    strategic_frequency: str = "30m"
    tactical_frequency: str = "5m"
    locator_frequency: str = "1m"
    physical_structure_frequencies: tuple[str, ...] = ("d", "30m", "5m", "1m")
    stroke_mode: str = STRICT_STROKE_MODE
    strict_base_profile_id: str = STRICT_BASE_PROFILE_ID
    strict_base_profile_revision: str = strict_base_config_revision()
    direct_recursive_alignment_parameter_set_id: str = (
        direct_recursive_alignment_contract().parameter_set_id
    )
    structure_scope: str = SCREENING_STRUCTURE_SCOPE
    recursive_structure_allowed: bool = True
    unfinished_segment_candidates: bool = True
    human_confirmation_required: bool = True
    automated_order_authorized: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if self.schema != DECISION_CORE_SCHEMA:
            raise ValueError("human-assisted decision core schema changed")
        if (
            self.higher_context_frequency,
            self.strategic_frequency,
            self.tactical_frequency,
            self.locator_frequency,
        ) != ("d", "30m", "5m", "1m"):
            raise ValueError("human-assisted timeframe contract changed")
        if (
            self.physical_structure_frequencies != ("d", "30m", "5m", "1m")
            or self.stroke_mode != STRICT_STROKE_MODE
            or self.strict_base_profile_id != STRICT_BASE_PROFILE_ID
            or self.strict_base_profile_revision != strict_base_config_revision()
            or self.direct_recursive_alignment_parameter_set_id
            != direct_recursive_alignment_contract().parameter_set_id
            or self.structure_scope != SCREENING_STRUCTURE_SCOPE
            or not self.recursive_structure_allowed
            or not self.unfinished_segment_candidates
        ):
            raise ValueError("human-assisted physical structure contract changed")
        if (
            not self.human_confirmation_required
            or self.automated_order_authorized
            or self.live_status != "LIVE_DISABLED"
        ):
            raise ValueError("human-assisted decision core cannot enable live trading")

    @property
    def contract_id(self) -> str:
        return sha256_json(self)

    def document(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "contract_id": self.contract_id,
            # 合同身份始终绑定 ``policy``；可移植文档也必须保存它，使归档能够
            # 独立重建身份，而不是信任不透明的自我声明。
            "policy": _trading_policy_document(self.policy),
            "higher_context_frequency": self.higher_context_frequency,
            "strategic_frequency": self.strategic_frequency,
            "tactical_frequency": self.tactical_frequency,
            "locator_frequency": self.locator_frequency,
            "physical_structure_frequencies": list(
                self.physical_structure_frequencies
            ),
            "stroke_mode": self.stroke_mode,
            "strict_base_profile_id": self.strict_base_profile_id,
            "strict_base_profile_revision": self.strict_base_profile_revision,
            "direct_recursive_alignment_parameter_set_id": (
                self.direct_recursive_alignment_parameter_set_id
            ),
            "structure_scope": self.structure_scope,
            "recursive_structure_allowed": self.recursive_structure_allowed,
            "unfinished_segment_candidates": self.unfinished_segment_candidates,
            "human_confirmation_required": self.human_confirmation_required,
            "automated_order_authorized": self.automated_order_authorized,
            "live_status": self.live_status,
        }


def validate_human_assisted_contract_document(
    document: Mapping[str, object],
) -> str:
    """重建并验证可移植的决策核心合同文档。

    外层选股快照哈希证明文档未被意外修改；本验证器进一步证明其声明的
    ``contract_id`` 是全部决策参数（包括内嵌 ``TradingPolicy``）的标准哈希。
    """

    expected_contract_fields = {
        field.name for field in fields(HumanAssistedDecisionContract)
    } | {"contract_id"}
    if set(document) != expected_contract_fields:
        raise ValueError("human-assisted decision contract fields changed")
    policy_document = document.get("policy")
    if not isinstance(policy_document, Mapping):
        raise ValueError("human-assisted decision policy is unavailable")
    expected_policy_fields = {field.name for field in fields(TradingPolicy)}
    if set(policy_document) != expected_policy_fields:
        raise ValueError("human-assisted decision policy fields changed")

    bool_fields = (
        "require_confirmed_five_minute",
        "require_confirmed_one_minute",
        "require_sector_eligibility",
        "require_thirty_minute_context",
        "first_center_three_buy_only",
    )
    if any(type(policy_document.get(name)) is not bool for name in bool_fields):
        raise ValueError("human-assisted decision policy booleans are invalid")
    decimal_fields = (
        "minimum_tick",
        "first_buy_risk_multiplier",
        "second_buy_risk_multiplier",
        "third_buy_risk_multiplier",
    )
    if any(not isinstance(policy_document.get(name), str) for name in decimal_fields):
        raise ValueError("human-assisted decision policy decimals are invalid")
    try:
        decimals = {
            name: Decimal(str(policy_document[name])) for name in decimal_fields
        }
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(
            "human-assisted decision policy decimals are invalid"
        ) from exc
    if any(not value.is_finite() for value in decimals.values()):
        raise ValueError("human-assisted decision policy decimals are invalid")
    maximum_age = policy_document.get("max_five_minute_setup_age_seconds")
    if type(maximum_age) is not int:
        raise ValueError("human-assisted decision policy age is invalid")

    physical_frequencies = document.get("physical_structure_frequencies")
    if not isinstance(physical_frequencies, list) or any(
        not isinstance(value, str) for value in physical_frequencies
    ):
        raise ValueError("human-assisted physical frequencies are invalid")
    string_fields = (
        "schema",
        "higher_context_frequency",
        "strategic_frequency",
        "tactical_frequency",
        "locator_frequency",
        "stroke_mode",
        "strict_base_profile_id",
        "strict_base_profile_revision",
        "direct_recursive_alignment_parameter_set_id",
        "structure_scope",
        "live_status",
    )
    if any(not isinstance(document.get(name), str) for name in string_fields):
        raise ValueError("human-assisted decision contract strings are invalid")
    contract_bool_fields = (
        "recursive_structure_allowed",
        "unfinished_segment_candidates",
        "human_confirmation_required",
        "automated_order_authorized",
    )
    if any(type(document.get(name)) is not bool for name in contract_bool_fields):
        raise ValueError("human-assisted decision contract booleans are invalid")

    policy = TradingPolicy(
        **{name: bool(policy_document[name]) for name in bool_fields},
        **decimals,
        max_five_minute_setup_age_seconds=maximum_age,
    )
    contract = HumanAssistedDecisionContract(
        policy=policy,
        schema=str(document["schema"]),
        higher_context_frequency=str(document["higher_context_frequency"]),
        strategic_frequency=str(document["strategic_frequency"]),
        tactical_frequency=str(document["tactical_frequency"]),
        locator_frequency=str(document["locator_frequency"]),
        physical_structure_frequencies=tuple(physical_frequencies),
        stroke_mode=str(document["stroke_mode"]),
        strict_base_profile_id=str(document["strict_base_profile_id"]),
        strict_base_profile_revision=str(document["strict_base_profile_revision"]),
        direct_recursive_alignment_parameter_set_id=str(
            document["direct_recursive_alignment_parameter_set_id"]
        ),
        structure_scope=str(document["structure_scope"]),
        recursive_structure_allowed=bool(document["recursive_structure_allowed"]),
        unfinished_segment_candidates=bool(document["unfinished_segment_candidates"]),
        human_confirmation_required=bool(document["human_confirmation_required"]),
        automated_order_authorized=bool(document["automated_order_authorized"]),
        live_status=str(document["live_status"]),
    )
    if document.get("contract_id") != contract.contract_id:
        raise ValueError("human-assisted decision contract identity changed")
    return contract.contract_id


def _context_document(context: TimeframeContext | None) -> dict[str, object] | None:
    if context is None:
        return None
    return {
        "frequency": context.frequency,
        "direction": context.direction,
        "disposition": context.disposition,
        "hard_block": context.hard_block,
        "dominant_point_id": context.dominant_point_id,
        "dominant_point_type": context.dominant_point_type,
        "reason_codes": list(context.reason_codes),
        "observed_at": context.observed_at.isoformat(),
    }


def sector_decision_document(
    assessment: SectorAssessment,
    *,
    ordinal: int | None,
) -> dict[str, object]:
    """序列化板块排序使用的全部证据，但不改变其入选资格。"""

    strength = getattr(assessment, "horizontal_strength", None)
    anchor = getattr(assessment, "strength_anchor_session", None)
    return {
        "sector_id": assessment.sector_id,
        "sector_name": assessment.sector_name,
        "eligible": assessment.eligible,
        "hard_block": assessment.hard_block,
        "regime": assessment.regime,
        "rank": ordinal,
        "rank_score": assessment.rank_score,
        "rank_components": dict(assessment.rank_components),
        "reason_codes": list(assessment.reason_codes),
        "horizontal_strength": None if strength is None else str(strength),
        "horizontal_rank": getattr(assessment, "horizontal_rank", None),
        "strength_anchor_session": None if anchor is None else anchor.isoformat(),
        "strength_member_count": getattr(assessment, "strength_member_count", 0),
        "strength_source_revision": getattr(
            assessment, "strength_source_revision", None
        ),
        "strength_reason_codes": list(
            getattr(assessment, "strength_reason_codes", ())
        ),
        "context_30m": _context_document(assessment.thirty_context),
        "context_5m": _context_document(assessment.five_context),
        "context_1m": _context_document(assessment.one_context),
    }


def _point_identity(point: StructuralPoint | ProvisionalCandidate) -> str:
    return point.point_id if isinstance(point, StructuralPoint) else point.candidate_id


def point_decision_document(
    point: StructuralPoint | ProvisionalCandidate,
) -> dict[str, object]:
    if isinstance(point, ProvisionalCandidate):
        unfinished_segment = (
            "unfinished_segment_participates" in point.evidence_codes
            or "unfinished_segment_lock" in point.missing_conditions
        )
        return {
            "point_id": point.candidate_id,
            "point_type": point.point_type,
            "side": point.side,
            "status": point.status,
            "source_frequency": point.source_frequency,
            "tower": point.tower,
            "recursive_level": point.recursive_level,
            "anchor_at": point.observed_at.isoformat(),
            "confirmed_at": None,
            "available_at": point.observed_at.isoformat(),
            "price_basis_revision": None,
            "anchor_price": point.anchor_price,
            "invalidation_price": None,
            "center_id": None,
            "center_zd": None,
            "center_zg": None,
            "center_ordinal": None,
            "variant": None,
            "divergence_kind": None,
            "missing_conditions": list(point.missing_conditions),
            "evidence_codes": list(point.evidence_codes),
            "contains_unfinished_segment": unfinished_segment,
            "actionable": False,
        }
    return {
        "point_id": point.point_id,
        "point_type": point.point_type,
        "side": point.side,
        "status": point.status,
        "source_frequency": point.source_frequency,
        "tower": point.tower,
        "recursive_level": point.recursive_level,
        "anchor_at": point.anchor_at.isoformat(),
        "confirmed_at": (
            None if point.confirmed_at is None else point.confirmed_at.isoformat()
        ),
        "available_at": point.available_at.isoformat(),
        "price_basis_revision": point.price_basis_revision,
        "anchor_price": point.structure_anchor_price,
        "invalidation_price": point.structure_invalidation_price,
        "center_id": point.center_id,
        "center_zd": point.center_zd,
        "center_zg": point.center_zg,
        "center_ordinal": point.center_ordinal,
        "variant": point.variant,
        "divergence_kind": point.divergence_kind,
        "missing_conditions": [],
        "evidence_codes": list(point.evidence_codes),
        "contains_unfinished_segment": False,
        "actionable": True,
    }


def apply_sector_selection_scope(
    document: dict[str, object],
    selection_sources: Sequence[str],
) -> None:
    """在共享决策合同内应用板块优先入场规则。

    过去网页选股会在公共评估器返回后再修改结果，导致历史回放可能允许入场，
    而页面却正确地把同一买点保留为仅监控。将规则放在这里，使选股来源成为
    可移植决策身份的一部分。
    """

    sources = tuple(dict.fromkeys(selection_sources))
    if not sources:
        sources = ("INCREMENTAL_SCAN_SCOPE",)
    sector_triggered = "QMT_SECTOR_TRIGGER" in sources
    document["selection_sources"] = list(sources)
    document["sector_triggered"] = sector_triggered
    document["monitor_only"] = not sector_triggered
    if document.get("side") == "buy" and not sector_triggered:
        raw_reasons = document.get("decision_reasons")
        if not isinstance(raw_reasons, list) or any(
            not isinstance(value, str) for value in raw_reasons
        ):
            raise ValueError("signal decision_reasons must be a string list")
        document["entry_allowed"] = False
        document["risk_multiplier"] = "0"
        document["decision_reasons"] = list(
            dict.fromkeys((*raw_reasons, MONITOR_ONLY_BUY_REASON_CODE))
        )
    if document.get("decision_document_schema") is not None:
        document["decision_document_id"] = signal_decision_document_id(document)


def signal_decision_projection(
    document: Mapping[str, object],
) -> dict[str, object]:
    """返回页面与回放共用的精确纯决策投影。

    扁平传输文档可以附加页面专用图表链接和大体积 QMT 解释证据。它们只能解释
    决策而不能改变决策，因此在此明确排除；紧凑投影仍可从任一传输文档独立重算。
    """

    if document.get("decision_document_schema") != SIGNAL_DECISION_DOCUMENT_SCHEMA:
        raise ValueError("human-assisted signal decision schema changed")
    missing = tuple(name for name in _SIGNAL_DECISION_FIELDS if name not in document)
    if missing:
        raise ValueError(f"human-assisted signal decision fields missing: {missing}")
    risk = document.get("higher_timeframe_risk")
    if not isinstance(risk, Mapping):
        raise ValueError("higher-timeframe decision document is unavailable")
    missing_risk = tuple(
        name for name in _HIGHER_TIMEFRAME_DECISION_FIELDS if name not in risk
    )
    if missing_risk:
        raise ValueError(
            f"higher-timeframe decision fields missing: {missing_risk}"
        )
    return {
        "schema": SIGNAL_DECISION_DOCUMENT_SCHEMA,
        **{name: document[name] for name in _SIGNAL_DECISION_FIELDS},
        "higher_timeframe_risk": {
            name: risk[name] for name in _HIGHER_TIMEFRAME_DECISION_FIELDS
        },
    }


def signal_decision_document_id(document: Mapping[str, object]) -> str:
    return sha256_json(signal_decision_projection(document))


def validate_signal_decision_document(document: Mapping[str, object]) -> str:
    """验证可移植的页面/回放决策身份并返回该身份。"""

    expected = signal_decision_document_id(document)
    if document.get("decision_document_id") != expected:
        raise ValueError("human-assisted signal decision identity changed")
    return expected


def serialize_evaluated_signal(
    item: EvaluatedSignal,
    *,
    previous_stage: str | None = None,
    name: str | None = None,
    decision_core_id: str,
    selection_sources: Sequence[str] = (),
) -> dict[str, object]:
    """返回实时选股与回放完全一致使用的标准决策字段。"""

    point = item.setup.point
    trigger = item.trigger
    boundary = item.entry_execution_boundary
    if boundary is not None and (
        trigger is None
        or boundary.point_id != trigger.point_id
        or boundary.symbol != point.code
    ):
        raise ValueError("entry execution boundary does not match the 1m trigger")
    entry_allowed = item.entry is not None and item.entry.allowed
    exit_allowed = item.exit is not None and item.exit.allowed
    lifecycle_stage = item.lifecycle.stage
    if (entry_allowed or exit_allowed) and previous_stage in {
        "triggered",
        "executable",
        "active",
    }:
        lifecycle_stage = "executable"
    decision_reasons = tuple(
        dict.fromkeys(
            (
                *((item.entry.reason_codes if item.entry is not None else ())),
                *((item.exit.reason_codes if item.exit is not None else ())),
                *item.conflict.reason_codes,
            )
        )
    )
    document = {
        "decision_document_schema": SIGNAL_DECISION_DOCUMENT_SCHEMA,
        "decision_core_id": decision_core_id,
        "signal_id": item.lifecycle.signal_id,
        "setup_id": item.setup.setup_id,
        "point_id": _point_identity(point),
        "code": point.code,
        "name": name,
        "point_type": point.point_type,
        "side": point.side,
        "tower": point.tower,
        "recursive_level": point.recursive_level,
        "lifecycle_stage": lifecycle_stage,
        "observed_at": item.lifecycle.observed_at.isoformat(),
        "structure_scope": SCREENING_STRUCTURE_SCOPE,
        "structure_frequencies": ["d", "30m", "5m", "1m"],
        "stroke_mode": STRICT_STROKE_MODE,
        "recursive_structure_used": item.physical_timeframe_recursive,
        "physical_timeframe_recursive": item.physical_timeframe_recursive,
        "context_d": _context_document(item.daily_context),
        "context_30m": {
            "direction": item.setup.context.direction,
            "disposition": item.setup.context.disposition,
            "hard_block": item.setup.context.hard_block,
            "dominant_point_id": item.setup.context.dominant_point_id,
            "dominant_point_type": item.setup.context.dominant_point_type,
            "reason_codes": list(item.setup.context.reason_codes),
        },
        "setup_5m": point_decision_document(point),
        "trigger_1m": None if trigger is None else point_decision_document(trigger),
        "entry_execution_boundary": (
            None if boundary is None else boundary.document()
        ),
        "sector": sector_decision_document(item.setup.sector, ordinal=None),
        "structural_stop": (
            None
            if item.entry is None or item.entry.structural_stop is None
            else str(item.entry.structural_stop)
        ),
        "risk_multiplier": "0" if item.entry is None else str(item.entry.risk_multiplier),
        "technical_entry_allowed": item.technical_entry_allowed,
        "entry_allowed": entry_allowed,
        "exit_allowed": exit_allowed,
        "exit_action": "none" if item.exit is None else item.exit.action,
        "decision_reasons": list(decision_reasons),
        "conflict": {
            "hard_block": item.conflict.hard_block,
            "blocking_point_ids": list(item.conflict.blocking_point_ids),
            "risk_only_point_ids": list(item.conflict.risk_only_point_ids),
        },
        "higher_timeframe_risk": {
            "market_gate": item.market_risk_gate,
            "sector_gate": item.sector_risk_gate,
            "symbol_gate": item.symbol_risk_gate,
            "market_states": dict(item.market_higher_timeframe_states),
            "sector_states": dict(item.sector_higher_timeframe_states),
            "symbol_states": dict(item.symbol_higher_timeframe_states),
            "market_reason_codes": list(
                item.market_higher_timeframe_reason_codes
            ),
            "symbol_reason_codes": list(
                item.symbol_higher_timeframe_reason_codes
            ),
            "sector_reason_codes": list(
                item.sector_higher_timeframe_reason_codes
            ),
            "reason_codes": list(item.higher_timeframe_reason_codes),
            "market_period_diagnostics": [
                value.document()
                for value in item.market_higher_timeframe_diagnostics
            ],
            "symbol_period_diagnostics": [
                value.document()
                for value in item.symbol_higher_timeframe_diagnostics
            ],
            "sector_period_diagnostics": [
                value.document()
                for value in item.sector_higher_timeframe_diagnostics
            ],
            "new_entry_requires_all_green": True,
        },
        "warmup": {
            "converged": item.warmup_converged,
            "by_frequency": [
                {
                    "frequency": frequency,
                    "converged": converged,
                    "full_bar_count": full_count,
                    "suffix_bar_count": suffix_count,
                }
                for frequency, converged, full_count, suffix_count in item.warmup_by_frequency
            ],
            "reason_codes": list(item.warmup_reason_codes),
            "difference_codes_by_frequency": [
                {
                    "frequency": frequency,
                    "difference_codes": list(difference_codes),
                }
                for frequency, difference_codes in (
                    item.warmup_difference_codes_by_frequency
                )
            ],
            "required_for_new_entry": True,
        },
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    apply_sector_selection_scope(document, selection_sources)
    document["decision_document_id"] = signal_decision_document_id(document)
    return document


class HumanAssistedDecisionCore:
    """页面选股与历史结构包回放的唯一评估器。"""

    def __init__(self, policy: TradingPolicy = TradingPolicy()) -> None:
        self.contract = HumanAssistedDecisionContract(policy=policy)
        self._engine = TradingEngine(policy)

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    def evaluate_symbol(
        self,
        bundle: SymbolStructureBundle,
    ) -> tuple[EvaluatedSignal, ...]:
        evaluated = self._engine.evaluate_symbol(bundle)
        if "QMT_SECTOR_TRIGGER" in bundle.selection_sources:
            return evaluated
        return tuple(
            replace(
                item,
                entry=(
                    item.entry
                    if item.entry is None or item.setup.point.side != "buy"
                    else replace(
                        item.entry,
                        allowed=False,
                        risk_multiplier=Decimal("0"),
                        reason_codes=tuple(
                            dict.fromkeys(
                                (
                                    *item.entry.reason_codes,
                                    MONITOR_ONLY_BUY_REASON_CODE,
                                )
                            )
                        ),
                    )
                ),
            )
            for item in evaluated
        )

    def decision_documents(
        self,
        bundle: SymbolStructureBundle,
        *,
        previous_stages: Mapping[str, str] | None = None,
        name: str | None = None,
        selection_sources: Sequence[str] | None = None,
    ) -> tuple[dict[str, object], ...]:
        effective_bundle = (
            bundle
            if selection_sources is None
            else replace(bundle, selection_sources=tuple(selection_sources))
        )
        stages = previous_stages or {}
        return tuple(
            serialize_evaluated_signal(
                item,
                previous_stage=stages.get(item.lifecycle.signal_id),
                name=name,
                decision_core_id=self.contract_id,
                selection_sources=effective_bundle.selection_sources,
            )
            for item in self.evaluate_symbol(effective_bundle)
        )


def replay_human_assisted_bundles(
    bundles: Sequence[SymbolStructureBundle],
    *,
    core: HumanAssistedDecisionCore,
    selection_sources_by_code: Mapping[str, Sequence[str]] | None = None,
) -> tuple[tuple[str, tuple[dict[str, object], ...]], ...]:
    """因果回放适配器；调用方只能传入当时可见的时点结构包。"""

    ordered = tuple(sorted(bundles, key=lambda value: (value.as_of, value.code)))
    sources = selection_sources_by_code or {}
    return tuple(
        (
            bundle.code,
            core.decision_documents(
                bundle,
                selection_sources=(
                    sources[bundle.code] if bundle.code in sources else None
                ),
            ),
        )
        for bundle in ordered
    )


__all__ = (
    "DECISION_CORE_SCHEMA",
    "MONITOR_ONLY_BUY_REASON_CODE",
    "SIGNAL_DECISION_DOCUMENT_SCHEMA",
    "HumanAssistedDecisionContract",
    "HumanAssistedDecisionCore",
    "apply_sector_selection_scope",
    "point_decision_document",
    "replay_human_assisted_bundles",
    "sector_decision_document",
    "serialize_evaluated_signal",
    "signal_decision_document_id",
    "signal_decision_projection",
    "validate_human_assisted_contract_document",
    "validate_signal_decision_document",
)
