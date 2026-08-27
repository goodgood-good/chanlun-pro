"""人机协同实时选股与因果回放共用的唯一决策核心。

本模块同时负责评估与决策序列化。网页可以增加仅用于展示的字段（例如图表
链接），但不能重新解释信号。历史回放接收完全相同的
``SymbolStructureBundle``，并调用同一个核心。
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime
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
    _TechnicalSignalEvaluator,
)
from chanlun.decision_support.trading_system.context_evidence import (
    signal_context_risk_scale,
)
from chanlun.decision_support.trading_system.execution_policy import (
    SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,
)
from chanlun.decision_support.trading_system.models import (
    ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES,
    SectorAssessment,
    StructuralPoint,
    TimeframeContext,
    TradingPolicy,
)
from chanlun.decision_support.trading_system.operation_level import (
    is_five_minute_trade_level,
)
from chanlun.decision_support.trading_system.five_minute_setup_state import (
    FIVE_MINUTE_SETUP_STATE_CONTRACT,
    GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE,
    WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION,
    execution_recommendation_label,
    setup_state_for_point,
    unconfirmed_setup_recommendation,
    validate_setup_state_document,
)
from chanlun.decision_support.trading_system.position_recommendation import (
    BUY_SIGNAL_PROTECTION_REASON_CODES,
    build_position_recommendation,
)
from chanlun.decision_support.trading_system.provisional import ProvisionalCandidate
from chanlun.decision_support.trading_system.screening_structure import (
    SCREENING_STRUCTURE_SCOPE,
)
from chanlun.decision_support.trading_system.signal_alignment import (
    unified_signal_alignment_contract,
)
from chanlun.decision_support.trading_system.selection import (
    evaluate_formal_selection_gate,
    selection_research_snapshot_from_document,
)


DECISION_CORE_SCHEMA = "chanlun-human-assisted-decision-core"
FIVE_MINUTE_SETUP_SELECTION_REVISION = (
    "terminal-two-segment-authoritative-state-v9-geometric-candidate"
)
SIGNAL_DECISION_DOCUMENT_SCHEMA = (
    "chanlun-human-assisted-signal-decision-v8-geometric-candidate"
)
FORMAL_SELECTION_REQUIRED_REASON_CODE = "SIGNED_SELECTION_RESEARCH_REQUIRED"

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
    "segment_difference_1m",
    "entry_execution_boundary",
    "sector",
    "structural_stop",
    "risk_multiplier",
    "position_recommendation",
    "technical_entry_allowed",
    "entry_allowed",
    "exit_allowed",
    "exit_action",
    "decision_reasons",
    "conflict",
    "warmup",
    "selection_path",
    "selection_research",
    "formal_selection",
    "formal_selection_required",
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
    "data_integrity_hard_block_reason_codes",
    "market_period_diagnostics",
    "sector_period_diagnostics",
    "symbol_period_diagnostics",
    "new_entry_requires_all_green",
)


def _trading_policy_document(policy: TradingPolicy) -> dict[str, object]:
    """无精度损失地序列化所有影响决策的策略字段。"""

    return {
        "require_confirmed_five_minute": policy.require_confirmed_five_minute,
        "require_one_minute_segment_difference_for_precise_execution": (
            policy.require_one_minute_segment_difference_for_precise_execution
        ),
        "require_sector_eligibility": policy.require_sector_eligibility,
        "require_thirty_minute_context": policy.require_thirty_minute_context,
        "minimum_tick": format(policy.minimum_tick, "f"),
        "first_buy_risk_multiplier": format(policy.first_buy_risk_multiplier, "f"),
        "second_buy_risk_multiplier": format(policy.second_buy_risk_multiplier, "f"),
        "third_buy_risk_multiplier": format(policy.third_buy_risk_multiplier, "f"),
        "max_five_minute_setup_age_seconds": (policy.max_five_minute_setup_age_seconds),
    }


@dataclass(frozen=True, slots=True)
class HumanAssistedDecisionContract:
    """选股、盘中监听与回放共同使用的冻结决策逻辑身份。"""

    policy: TradingPolicy
    schema: str = DECISION_CORE_SCHEMA
    higher_context_frequency: str = "d"
    context_frequency: str = "30m"
    trade_frequency: str = "5m"
    segment_difference_frequency: str = "1m"
    segment_difference_point_types: tuple[str, ...] = tuple(
        sorted(ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES)
    )
    # 以下两个字段仅保留高/交易周期业务标签。
    strategic_frequency: str = "30m"
    tactical_frequency: str = "5m"
    physical_structure_frequencies: tuple[str, ...] = ("d", "30m", "5m", "1m")
    stroke_mode: str = STRICT_STROKE_MODE
    strict_base_profile_id: str = STRICT_BASE_PROFILE_ID
    strict_base_profile_revision: str = strict_base_config_revision()
    signal_alignment_parameter_set_id: str = (
        unified_signal_alignment_contract().parameter_set_id
    )
    structure_scope: str = SCREENING_STRUCTURE_SCOPE
    recursive_structure_allowed: bool = True
    five_minute_setup_selection_revision: str = FIVE_MINUTE_SETUP_SELECTION_REVISION
    five_minute_setup_state_contract: str = FIVE_MINUTE_SETUP_STATE_CONTRACT
    formal_selection_required: bool = True
    human_confirmation_required: bool = True
    automated_order_authorized: bool = False
    live_status: str = "LIVE_DISABLED"

    def __post_init__(self) -> None:
        if self.schema != DECISION_CORE_SCHEMA:
            raise ValueError("human-assisted decision core schema changed")
        if (
            self.policy.require_confirmed_five_minute is not True
            or self.policy.require_one_minute_segment_difference_for_precise_execution
            is not True
        ):
            raise ValueError(
                "human-assisted production policy requires independent 5m trade "
                "signals and confirmed 1m segment evidence for precise execution"
            )
        if (
            self.higher_context_frequency,
            self.context_frequency,
            self.trade_frequency,
            self.segment_difference_frequency,
        ) != ("d", "30m", "5m", "1m"):
            raise ValueError("human-assisted canonical timeframe contract changed")
        if (
            self.strategic_frequency,
            self.tactical_frequency,
        ) != ("30m", "5m"):
            raise ValueError("human-assisted timeframe contract changed")
        if self.segment_difference_point_types != tuple(
            sorted(ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES)
        ):
            raise ValueError("human-assisted one-minute segment contract changed")
        if (
            self.physical_structure_frequencies != ("d", "30m", "5m", "1m")
            or self.stroke_mode != STRICT_STROKE_MODE
            or self.strict_base_profile_id != STRICT_BASE_PROFILE_ID
            or self.strict_base_profile_revision != strict_base_config_revision()
            or self.signal_alignment_parameter_set_id
            != unified_signal_alignment_contract().parameter_set_id
            or self.structure_scope != SCREENING_STRUCTURE_SCOPE
            or not self.recursive_structure_allowed
            or self.five_minute_setup_selection_revision
            != FIVE_MINUTE_SETUP_SELECTION_REVISION
            or self.five_minute_setup_state_contract != FIVE_MINUTE_SETUP_STATE_CONTRACT
            or type(self.formal_selection_required) is not bool
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
            "context_frequency": self.context_frequency,
            "trade_frequency": self.trade_frequency,
            "segment_difference_frequency": self.segment_difference_frequency,
            "segment_difference_point_types": list(self.segment_difference_point_types),
            "strategic_frequency": self.strategic_frequency,
            "tactical_frequency": self.tactical_frequency,
            "physical_structure_frequencies": list(self.physical_structure_frequencies),
            "stroke_mode": self.stroke_mode,
            "strict_base_profile_id": self.strict_base_profile_id,
            "strict_base_profile_revision": self.strict_base_profile_revision,
            "signal_alignment_parameter_set_id": (
                self.signal_alignment_parameter_set_id
            ),
            "structure_scope": self.structure_scope,
            "recursive_structure_allowed": self.recursive_structure_allowed,
            "five_minute_setup_selection_revision": (
                self.five_minute_setup_selection_revision
            ),
            "five_minute_setup_state_contract": (self.five_minute_setup_state_contract),
            "formal_selection_required": self.formal_selection_required,
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
        "require_one_minute_segment_difference_for_precise_execution",
        "require_sector_eligibility",
        "require_thirty_minute_context",
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
        raise ValueError("human-assisted decision policy decimals are invalid") from exc
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
    segment_difference_point_types = document.get("segment_difference_point_types")
    if not isinstance(segment_difference_point_types, list) or any(
        not isinstance(value, str) for value in segment_difference_point_types
    ):
        raise ValueError("human-assisted one-minute segment types are invalid")
    string_fields = (
        "schema",
        "higher_context_frequency",
        "context_frequency",
        "trade_frequency",
        "segment_difference_frequency",
        "strategic_frequency",
        "tactical_frequency",
        "stroke_mode",
        "strict_base_profile_id",
        "strict_base_profile_revision",
        "signal_alignment_parameter_set_id",
        "structure_scope",
        "five_minute_setup_selection_revision",
        "five_minute_setup_state_contract",
        "live_status",
    )
    if any(not isinstance(document.get(name), str) for name in string_fields):
        raise ValueError("human-assisted decision contract strings are invalid")
    contract_bool_fields = (
        "recursive_structure_allowed",
        "formal_selection_required",
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
        context_frequency=str(document["context_frequency"]),
        trade_frequency=str(document["trade_frequency"]),
        segment_difference_frequency=str(document["segment_difference_frequency"]),
        segment_difference_point_types=tuple(segment_difference_point_types),
        strategic_frequency=str(document["strategic_frequency"]),
        tactical_frequency=str(document["tactical_frequency"]),
        physical_structure_frequencies=tuple(physical_frequencies),
        stroke_mode=str(document["stroke_mode"]),
        strict_base_profile_id=str(document["strict_base_profile_id"]),
        strict_base_profile_revision=str(document["strict_base_profile_revision"]),
        signal_alignment_parameter_set_id=str(
            document["signal_alignment_parameter_set_id"]
        ),
        structure_scope=str(document["structure_scope"]),
        recursive_structure_allowed=bool(document["recursive_structure_allowed"]),
        five_minute_setup_selection_revision=str(
            document["five_minute_setup_selection_revision"]
        ),
        five_minute_setup_state_contract=str(
            document["five_minute_setup_state_contract"]
        ),
        formal_selection_required=bool(document["formal_selection_required"]),
        human_confirmation_required=bool(document["human_confirmation_required"]),
        automated_order_authorized=bool(document["automated_order_authorized"]),
        live_status=str(document["live_status"]),
    )
    if document.get("contract_id") != contract.contract_id:
        raise ValueError("human-assisted decision contract identity changed")
    return contract.contract_id


def _context_document(
    context: TimeframeContext | None,
    *,
    technical_evidence: object | None = None,
    signal_assessment: object | None = None,
) -> dict[str, object] | None:
    if context is None:
        return None
    document = {
        "frequency": context.frequency,
        "direction": context.direction,
        "disposition": context.disposition,
        "hard_block": context.hard_block,
        "dominant_point_id": context.dominant_point_id,
        "dominant_point_type": context.dominant_point_type,
        "reason_codes": list(context.reason_codes),
        "observed_at": context.observed_at.isoformat(),
    }
    if technical_evidence is not None:
        document["same_period_technical_evidence"] = technical_evidence.document()
    if signal_assessment is not None:
        document["signal_context_assessment"] = signal_assessment.document()
    return document


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
        "strength_reason_codes": list(getattr(assessment, "strength_reason_codes", ())),
        "context_30m": _context_document(assessment.thirty_context),
        "context_5m": _context_document(assessment.five_context),
        "context_1m": _context_document(assessment.one_context),
    }


def _point_identity(point: StructuralPoint | ProvisionalCandidate) -> str:
    return point.point_id if isinstance(point, StructuralPoint) else point.candidate_id


def _terminal_segment_document(
    point: StructuralPoint | ProvisionalCandidate,
) -> dict[str, object]:
    reference = point.terminal_segment
    if reference is None:
        return {}
    return {
        "terminal_segment_role": reference.role,
        "terminal_segment_level": reference.structural_level,
        "terminal_segment_id": reference.unit_id,
        "terminal_segment_source_kind": reference.source_kind.value,
        "terminal_segment_direction": reference.direction,
        "terminal_segment_state": reference.state,
        "terminal_segment_start_at": reference.market_start.isoformat(),
        "terminal_segment_end_at": reference.market_end.isoformat(),
        "terminal_segment_available_at": reference.available_at.isoformat(),
    }


def point_decision_document(
    point: StructuralPoint | ProvisionalCandidate,
) -> dict[str, object]:
    setup_state = setup_state_for_point(point).document()
    if isinstance(point, ProvisionalCandidate):
        return {
            "point_id": point.candidate_id,
            "point_type": point.point_type,
            "side": point.side,
            "status": point.status,
            "source_frequency": point.source_frequency,
            "tower": point.tower,
            "recursive_level": point.recursive_level,
            "anchor_at": point.anchor_at.isoformat(),
            "confirmed_at": None,
            "available_at": point.available_at.isoformat(),
            "price_basis_revision": point.price_basis_revision,
            "anchor_price": point.anchor_price,
            "invalidation_price": point.invalidation_price,
            "center_id": point.center_id,
            "center_zd": point.center_zd,
            "center_zg": point.center_zg,
            "center_ordinal": point.center_ordinal,
            "variant": point.variant,
            "divergence_kind": point.divergence_kind,
            "parent_point_id": point.parent_point_id,
            "related_point_ids": list(point.related_point_ids),
            "small_to_large_carrier_unit_ids": list(
                point.small_to_large_carrier_unit_ids
            ),
            "missing_conditions": list(point.missing_conditions),
            "evidence_codes": list(point.evidence_codes),
            **_terminal_segment_document(point),
            **setup_state,
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
        "parent_point_id": point.parent_point_id,
        "related_point_ids": list(point.related_point_ids),
        "small_to_large_carrier_unit_ids": list(point.small_to_large_carrier_unit_ids),
        "missing_conditions": [],
        "evidence_codes": list(point.evidence_codes),
        **_terminal_segment_document(point),
        **setup_state,
    }


def apply_formal_selection_scope(
    document: dict[str, object],
    selection_sources: Sequence[str],
    *,
    formal_selection_required: bool = True,
) -> None:
    """把板块扫描来源和正式研究证据共同绑定到决策身份。"""

    if type(formal_selection_required) is not bool:
        raise ValueError("formal_selection_required must be a boolean")
    sources = tuple(dict.fromkeys(selection_sources))
    if not sources:
        sources = ("INCREMENTAL_SCAN_SCOPE",)
    sector_triggered = "QMT_SECTOR_TRIGGER" in sources
    document["selection_sources"] = list(sources)
    document["sector_triggered"] = sector_triggered
    previous_gate = document.get("formal_selection")
    previous_gate_reasons = tuple(
        str(value)
        for value in (
            previous_gate.get("reason_codes")
            if isinstance(previous_gate, Mapping)
            else ()
        )
        if isinstance(value, str) and value
    )
    raw_research = document.get("selection_research")
    research = (
        None
        if raw_research is None
        else selection_research_snapshot_from_document(raw_research)
    )
    try:
        decision_time = datetime.fromisoformat(str(document["observed_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("决策时间无效") from exc
    gate = evaluate_formal_selection_gate(
        research,
        symbol=str(document.get("code") or ""),
        decision_time=decision_time,
        selection_path=document.get("selection_path"),  # type: ignore[arg-type]
        sector_triggered=sector_triggered,
    )
    document["formal_selection"] = gate.document()
    document["formal_selection_required"] = formal_selection_required
    document["monitor_only"] = formal_selection_required and not gate.accepted
    if document.get("side") == "buy":
        raw_reasons = document.get("decision_reasons")
        if not isinstance(raw_reasons, list) or any(
            not isinstance(value, str) for value in raw_reasons
        ):
            raise ValueError("signal decision_reasons must be a string list")
        document["decision_reasons"] = list(
            dict.fromkeys(
                (
                    *(
                        value
                        for value in raw_reasons
                        if value not in previous_gate_reasons
                    ),
                    *(
                        gate.reason_codes
                        if formal_selection_required and not gate.accepted
                        else ()
                    ),
                )
            )
        )
        profile = document.get("execution_profile")
        if isinstance(profile, dict):
            raw_advisories = profile.get("advisory_reason_codes")
            advisories = (
                tuple(
                    value
                    for value in raw_advisories
                    if isinstance(value, str) and value not in previous_gate_reasons
                )
                if isinstance(raw_advisories, list)
                else ()
            )
            advisories = tuple(
                dict.fromkeys(
                    (
                        *advisories,
                        *(
                            gate.reason_codes
                            if formal_selection_required and not gate.accepted
                            else ()
                        ),
                    )
                )
            )
            profile["advisory_reason_codes"] = list(advisories)
            if (
                profile.get("structure_signal_confirmed") is True
                and profile.get("hard_blocked") is not True
            ):
                profile["recommendation"] = (
                    WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION
                    if profile.get("one_minute_required_for_precise_execution") is True
                    and profile.get("one_minute_segment_difference_present") is not True
                    else "CAUTION"
                    if advisories
                    else "READY"
                )
                profile["recommendation_label"] = execution_recommendation_label(
                    profile["recommendation"]
                )
    if document.get("decision_document_schema") is not None and all(
        name in document for name in _SIGNAL_DECISION_FIELDS
    ):
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
        raise ValueError(f"higher-timeframe decision fields missing: {missing_risk}")
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

    setup = document.get("setup_5m")
    recursive_level = (
        setup.get("recursive_level") if isinstance(setup, Mapping) else None
    )
    if (
        not isinstance(setup, Mapping)
        or setup.get("source_frequency") != "5m"
        or type(recursive_level) is not int
        or not is_five_minute_trade_level("5m", recursive_level)
        or document.get("recursive_level") != recursive_level
    ):
        raise ValueError("human-assisted signal must use physical 5m/L0 trade evidence")
    validate_setup_state_document(setup)
    profile = document.get("execution_profile")
    if isinstance(profile, Mapping):
        recommendation = profile.get("recommendation")
        if profile.get("recommendation_label") != execution_recommendation_label(
            recommendation
        ):
            raise ValueError("human-assisted execution recommendation label changed")
        segment_difference_status = profile.get("segment_difference_status")
        segment_difference_ready = profile.get("segment_difference_ready")
        precise_execution_ready = profile.get("precise_execution_ready")
        if (
            profile.get("one_minute_required_for_trade_signal") is not False
            or profile.get("one_minute_required_for_precise_execution") is not True
            or segment_difference_status
            not in {
                "STRUCTURE_PENDING",
                "WAITING_ONE_MINUTE",
                "BOUNDARY_MISSING",
                "BOUNDARY_EXPIRED",
                "READY",
            }
            or type(segment_difference_ready) is not bool
            or segment_difference_ready is not (segment_difference_status == "READY")
            or type(precise_execution_ready) is not bool
            or precise_execution_ready
            is not bool(
                segment_difference_ready
                and (document.get("entry_allowed") or document.get("exit_allowed"))
            )
        ):
            raise ValueError("human-assisted 1m precise-execution contract changed")
    expected = signal_decision_document_id(document)
    if document.get("decision_document_id") != expected:
        raise ValueError("human-assisted signal decision identity changed")
    return expected


def serialize_evaluated_signal(
    item: EvaluatedSignal,
    *,
    previous_stage: str | None = None,
    name: str | None = None,
    current_price: float | None = None,
    decision_core_id: str,
    selection_sources: Sequence[str] = (),
    formal_selection_required: bool = True,
) -> dict[str, object]:
    """返回实时选股与回放完全一致使用的标准决策字段。"""

    point = item.setup.point
    if not is_five_minute_trade_level(
        point.source_frequency,
        point.recursive_level,
    ):
        raise ValueError("human-assisted signal must use physical 5m/L0 trade evidence")
    trigger = item.trigger
    boundary = item.entry_execution_boundary
    if boundary is not None and (
        trigger is None
        or boundary.point_id != trigger.point_id
        or boundary.symbol != point.code
    ):
        raise ValueError(
            "entry execution boundary does not match the 1m segment evidence"
        )
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
                *(item.entry.reason_codes if item.entry is not None else ()),
                *(item.exit.reason_codes if item.exit is not None else ()),
                *item.advisory_reason_codes,
                *item.conflict.reason_codes,
                *(
                    item.lifecycle.reason_codes
                    if item.lifecycle.stage == "invalidated"
                    else ()
                ),
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
        # 最新已完成行情柱的价格只用于通知和页面展示。生命周期已经使用同一
        # 价格完成失效判断；这里不把易变报价加入不可变决策身份。
        "current_price": current_price,
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
        "context_d": _context_document(
            item.daily_context,
            technical_evidence=item.daily_technical_context,
            signal_assessment=item.context_assessment,
        ),
        "context_30m": _context_document(
            item.setup.context,
            technical_evidence=item.thirty_technical_context,
            signal_assessment=item.context_assessment,
        ),
        "setup_5m": point_decision_document(point),
        "segment_difference_1m": (
            None if trigger is None else point_decision_document(trigger)
        ),
        "entry_execution_boundary": (None if boundary is None else boundary.document()),
        "sector": sector_decision_document(item.setup.sector, ordinal=None),
        "structural_stop": (
            None
            if item.entry is None or item.entry.structural_stop is None
            else str(item.entry.structural_stop)
        ),
        "risk_multiplier": "0"
        if item.entry is None
        else str(item.entry.risk_multiplier),
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
            "market_reason_codes": list(item.market_higher_timeframe_reason_codes),
            "symbol_reason_codes": list(item.symbol_higher_timeframe_reason_codes),
            "sector_reason_codes": list(item.sector_higher_timeframe_reason_codes),
            "reason_codes": list(item.higher_timeframe_reason_codes),
            "data_integrity_hard_block_reason_codes": list(
                item.higher_timeframe_data_integrity_reason_codes
            ),
            "market_period_diagnostics": [
                value.document() for value in item.market_higher_timeframe_diagnostics
            ],
            "symbol_period_diagnostics": [
                value.document() for value in item.symbol_higher_timeframe_diagnostics
            ],
            "sector_period_diagnostics": [
                value.document() for value in item.sector_higher_timeframe_diagnostics
            ],
            "new_entry_requires_all_green": False,
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
        "selection_path": (
            item.formal_selection.selection_path
            if item.formal_selection is not None
            else "INDIVIDUAL_THREE_PROGRAM"
        ),
        "selection_research": (
            None
            if item.selection_research is None
            else item.selection_research.document()
        ),
        "formal_selection": (
            None if item.formal_selection is None else item.formal_selection.document()
        ),
        "human_confirmation_required": True,
        "automated_order_authorized": False,
        "live_status": "LIVE_DISABLED",
    }
    apply_formal_selection_scope(
        document,
        selection_sources,
        formal_selection_required=formal_selection_required,
    )
    structure_confirmed = bool(isinstance(point, StructuralPoint) and point.confirmed)
    setup_state = setup_state_for_point(point)
    segment_difference_present = bool(
        trigger is not None
        and trigger.confirmed
        and trigger.source_frequency == "1m"
        and item.lifecycle.stage in {"triggered", "executable", "active"}
    )
    formal_advisories = (
        ()
        if not formal_selection_required
        or item.formal_selection is None
        or item.formal_selection.accepted
        else item.formal_selection.reason_codes
    )
    structure_relation_advisories = (
        (SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,)
        if item.exit is not None
        and SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE in item.exit.reason_codes
        else ()
    )
    advisory_reasons = tuple(
        dict.fromkeys(
            (
                *item.advisory_reason_codes,
                *(
                    item.conflict.reason_codes
                    if item.conflict.risk_only_point_ids
                    and not item.conflict.hard_block
                    else ()
                ),
                *formal_advisories,
                *structure_relation_advisories,
            )
        )
    )
    hard_reasons = tuple(
        reason
        for reason in (
            *(item.entry.reason_codes if item.entry is not None else ()),
            *(item.exit.reason_codes if item.exit is not None else ()),
            *(item.conflict.reason_codes if item.conflict.hard_block else ()),
            *(
                item.lifecycle.reason_codes
                if item.lifecycle.stage == "invalidated"
                else ()
            ),
        )
        if reason
        not in {
            "five_minute_not_confirmed",
            "lifecycle_not_actionable",
            "one_minute_not_confirmed",
            "one_minute_sell_not_confirmed",
            "sell_not_confirmed",
            "setup_not_confirmed",
            GEOMETRY_AWAITING_CONFIRMATION_REASON_CODE,
            SELL_STRUCTURE_RELATION_REQUIRED_REASON_CODE,
        }
    )
    if item.lifecycle.stage == "invalidated":
        recommendation = "BLOCKED"
    elif hard_reasons:
        recommendation = "BLOCKED"
    elif not structure_confirmed:
        recommendation = unconfirmed_setup_recommendation(setup_state.formation_state)
    elif not segment_difference_present:
        recommendation = WAITING_SEGMENT_DIFFERENCE_RECOMMENDATION
    elif advisory_reasons:
        recommendation = "CAUTION"
    else:
        recommendation = "READY"
    context_grade = (
        "UNRESOLVED"
        if item.context_assessment is None
        else item.context_assessment.grade
    )
    context_risk_scale = format(
        signal_context_risk_scale(item.context_assessment),
        ".2f",
    )
    structure_anchor_price = (
        point.structure_anchor_price
        if isinstance(point, StructuralPoint)
        else point.anchor_price
    )
    position_recommendation = build_position_recommendation(
        side=point.side,
        recommendation=recommendation,
        risk_multiplier=("0" if item.entry is None else item.entry.risk_multiplier),
        context_risk_scale=context_risk_scale,
        # 从实际可见价格量到结构防守位；锚点另行传入用于追价保护和审计。
        entry_price=(
            current_price if current_price is not None else structure_anchor_price
        ),
        structural_stop=(
            point.structure_invalidation_price
            if isinstance(point, StructuralPoint)
            else point.invalidation_price
        ),
        exit_action=("none" if item.exit is None else item.exit.action),
        structure_anchor_price=structure_anchor_price,
        five_minute_available_at=point.available_at,
        one_minute_available_at=(None if trigger is None else trigger.available_at),
    )
    position_recommendation_document = position_recommendation.document()
    operational_buy_protections = tuple(
        reason
        for reason in position_recommendation.reason_codes
        if reason in BUY_SIGNAL_PROTECTION_REASON_CODES
    )
    if point.side == "buy" and operational_buy_protections:
        # 技术结构仍可继续跟踪，但追价或跌破防守位都不得继续声明
        # 当前买入可用。具体保护原因保留在规范决策理由中供页面与审计复核。
        document["entry_allowed"] = False
        document["decision_reasons"] = list(
            dict.fromkeys((*document["decision_reasons"], *operational_buy_protections))
        )
        recommendation = "BLOCKED"
        hard_reasons = tuple(
            dict.fromkeys((*hard_reasons, *operational_buy_protections))
        )
    if not structure_confirmed:
        segment_difference_status = "STRUCTURE_PENDING"
    elif not segment_difference_present:
        segment_difference_status = "WAITING_ONE_MINUTE"
    elif (
        point.side != "buy"
        or not point.code.startswith(("SH.", "SZ.", "BJ."))
        or not item.physical_timeframe_recursive
    ):
        segment_difference_status = "READY"
    elif boundary is None:
        segment_difference_status = (
            "BOUNDARY_EXPIRED"
            if "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED" in decision_reasons
            else "BOUNDARY_MISSING"
        )
    elif (
        "ONE_MINUTE_SEGMENT_BOUNDARY_EXPIRED" in decision_reasons
        or boundary.entry_valid_until <= item.lifecycle.observed_at
    ):
        segment_difference_status = "BOUNDARY_EXPIRED"
    else:
        segment_difference_status = "READY"
    segment_difference_ready = segment_difference_status == "READY"
    document["position_recommendation"] = position_recommendation_document
    document["execution_profile"] = {
        "structure_signal_confirmed": structure_confirmed,
        "one_minute_role": "SEGMENT_DIFFERENCE_ONLY",
        "one_minute_required_for_trade_signal": False,
        "one_minute_required_for_precise_execution": True,
        "one_minute_segment_difference_present": segment_difference_present,
        "segment_difference_status": segment_difference_status,
        "segment_difference_ready": segment_difference_ready,
        "precise_execution_ready": bool(
            segment_difference_ready
            and (document["entry_allowed"] or document["exit_allowed"])
        ),
        "recommendation": recommendation,
        "recommendation_label": execution_recommendation_label(recommendation),
        "hard_blocked": recommendation == "BLOCKED",
        "hard_block_reason_codes": list(dict.fromkeys(hard_reasons)),
        "advisory_reason_codes": list(advisory_reasons),
        "context_grade": context_grade,
        "context_grade_label": (
            "待判定（证据不足）"
            if item.context_assessment is None
            else item.context_assessment.grade_label
        ),
        "context_risk_scale": context_risk_scale,
        "context_risk_scale_role": "POSITION_RISK_SIZING_ONLY",
        "position_recommendation": position_recommendation_document,
        "manual_confirmation_required": True,
        "automated_order_authorized": False,
    }
    document["decision_document_id"] = signal_decision_document_id(document)
    return document


class HumanAssistedDecisionCore:
    """页面选股与历史结构包回放的唯一评估器。"""

    def __init__(
        self,
        policy: TradingPolicy = TradingPolicy(),
        *,
        formal_selection_required: bool = True,
    ) -> None:
        self.contract = HumanAssistedDecisionContract(
            policy=policy,
            formal_selection_required=formal_selection_required,
        )
        self._technical_evaluator = _TechnicalSignalEvaluator(policy)

    @property
    def contract_id(self) -> str:
        return self.contract.contract_id

    def evaluate_symbol(
        self,
        bundle: SymbolStructureBundle,
    ) -> tuple[EvaluatedSignal, ...]:
        evaluated = self._technical_evaluator.evaluate_symbol(bundle)
        gate = evaluate_formal_selection_gate(
            bundle.selection_research,
            symbol=bundle.code,
            decision_time=bundle.as_of,
            selection_path=bundle.selection_path,
            sector_triggered="QMT_SECTOR_TRIGGER" in bundle.selection_sources,
        )
        return tuple(
            replace(
                item,
                formal_selection=gate,
                selection_research=bundle.selection_research,
                advisory_reason_codes=tuple(
                    dict.fromkeys(
                        (
                            *item.advisory_reason_codes,
                            *(
                                gate.reason_codes
                                if item.setup.point.side == "buy"
                                and self.contract.formal_selection_required
                                and not gate.accepted
                                else ()
                            ),
                        )
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
                current_price=effective_bundle.latest_price,
                decision_core_id=self.contract_id,
                selection_sources=effective_bundle.selection_sources,
                formal_selection_required=(self.contract.formal_selection_required),
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
    "FIVE_MINUTE_SETUP_SELECTION_REVISION",
    "FORMAL_SELECTION_REQUIRED_REASON_CODE",
    "SIGNAL_DECISION_DOCUMENT_SCHEMA",
    "HumanAssistedDecisionContract",
    "HumanAssistedDecisionCore",
    "apply_formal_selection_scope",
    "point_decision_document",
    "replay_human_assisted_bundles",
    "sector_decision_document",
    "serialize_evaluated_signal",
    "signal_decision_document_id",
    "signal_decision_projection",
    "validate_human_assisted_contract_document",
    "validate_signal_decision_document",
)
