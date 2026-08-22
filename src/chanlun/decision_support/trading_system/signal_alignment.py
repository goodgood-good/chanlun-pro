"""实时、选股、回放与人工复核共用的物理周期信号对齐合同。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.decision_support.trading_system.models import (
    CANONICAL_POINT_TYPES,
    CONTINUATION_SUPPORT_POINT_TYPES,
    ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES,
    REVERSAL_SUPPORT_POINT_TYPES,
)
from chanlun.decision_support.trading_system.operation_level import (
    FIVE_MINUTE_TRADE_RECURSIVE_LEVELS,
)


UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID = (
    "PHYSICAL_5M_L0_TRADE_SIGNAL_1M_PRECISE_EXECUTION_V5"
)


@dataclass(frozen=True, slots=True)
class UnifiedSignalAlignmentContract:
    """冻结一、二、三类买卖点进入决策层时的唯一跨周期关系。

    物理 5 分钟第 0 递归级别的正式点独立构成买卖信号；5m/L1 及以上
    是对应的高周期上下文，不是平行交易通道。1 分钟区间套不参与信号
    是否成立，也不得阻止 5 分钟首次通知，但它是升级为精确执行候选的硬门槛。
    """

    contract_id: str = UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID
    structure_authority: str = "STRICT_PHYSICAL_TIMEFRAME_ENGINE"
    context_frequencies: tuple[str, ...] = ("d", "30m")
    trade_signal_frequency: str = "5m"
    trade_signal_recursive_levels: tuple[int, ...] = (
        FIVE_MINUTE_TRADE_RECURSIVE_LEVELS
    )
    higher_recursive_trade_evidence_context_only: bool = True
    segment_difference_frequency: str = "1m"
    point_types: tuple[str, ...] = CANONICAL_POINT_TYPES
    reversal_support_point_types: tuple[str, ...] = tuple(
        point_type
        for point_type in CANONICAL_POINT_TYPES
        if point_type in REVERSAL_SUPPORT_POINT_TYPES
    )
    continuation_support_point_types: tuple[str, ...] = tuple(
        point_type
        for point_type in CANONICAL_POINT_TYPES
        if point_type in CONTINUATION_SUPPORT_POINT_TYPES
    )
    segment_difference_point_types: tuple[str, ...] = tuple(
        point_type
        for point_type in CANONICAL_POINT_TYPES
        if point_type in ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES
    )
    setup_classes_share_logic: bool = True
    point_classes_share_structure_authority: bool = True
    # 对 5 分钟信号及首次通知可选；对精确执行不可选。
    segment_difference_is_optional: bool = True
    segment_difference_required_for_precise_execution: bool = True
    segment_difference_must_match_side: bool = True
    third_class_can_confirm_reversal: bool = False
    third_class_can_confirm_continuation: bool = True
    third_class_keeps_center_geometry: bool = True
    small_to_large_second_class_allowed: bool = True
    provisional_points_actionable: bool = False

    def __post_init__(self) -> None:
        if self.contract_id != UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID:
            raise ValueError("统一信号对齐合同身份发生变化")
        if (
            self.structure_authority != "STRICT_PHYSICAL_TIMEFRAME_ENGINE"
            or self.context_frequencies != ("d", "30m")
            or (
                self.trade_signal_frequency,
                self.segment_difference_frequency,
            )
            != ("5m", "1m")
            or self.trade_signal_recursive_levels
            != FIVE_MINUTE_TRADE_RECURSIVE_LEVELS
            or not self.higher_recursive_trade_evidence_context_only
            or self.point_types != CANONICAL_POINT_TYPES
            or self.reversal_support_point_types
            != tuple(
                point_type
                for point_type in CANONICAL_POINT_TYPES
                if point_type in REVERSAL_SUPPORT_POINT_TYPES
            )
            or self.continuation_support_point_types
            != tuple(
                point_type
                for point_type in CANONICAL_POINT_TYPES
                if point_type in CONTINUATION_SUPPORT_POINT_TYPES
            )
            or self.segment_difference_point_types
            != tuple(
                point_type
                for point_type in CANONICAL_POINT_TYPES
                if point_type in ONE_MINUTE_SEGMENT_DIFFERENCE_POINT_TYPES
            )
            or not self.setup_classes_share_logic
            or not self.point_classes_share_structure_authority
            or not self.segment_difference_is_optional
            or not self.segment_difference_required_for_precise_execution
            or not self.segment_difference_must_match_side
            or self.third_class_can_confirm_reversal
            or not self.third_class_can_confirm_continuation
            or not self.third_class_keeps_center_geometry
            or not self.small_to_large_second_class_allowed
            or self.provisional_points_actionable
        ):
            raise ValueError("统一信号对齐规则发生变化")

    @property
    def parameter_set_id(self) -> str:
        return sha256_json(asdict(self))

    def document(self) -> dict[str, object]:
        document = asdict(self)
        document["parameter_set_id"] = self.parameter_set_id
        return document


def unified_signal_alignment_contract() -> UnifiedSignalAlignmentContract:
    return UnifiedSignalAlignmentContract()


__all__ = (
    "UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID",
    "UnifiedSignalAlignmentContract",
    "unified_signal_alignment_contract",
)
