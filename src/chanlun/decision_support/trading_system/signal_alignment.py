"""实时、选股、回放与人工复核共用的物理周期信号对齐合同。"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from chanlun.decision_support.fingerprints import sha256_json


UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID = (
    "PHYSICAL_5M_SETUP_1M_TRIGGER_UNIFIED_POINT_CLASSES"
)


@dataclass(frozen=True, slots=True)
class UnifiedSignalAlignmentContract:
    """冻结一、二、三类买卖点进入决策层时的唯一跨周期关系。"""

    contract_id: str = UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID
    structure_authority: str = "STRICT_PHYSICAL_TIMEFRAME_ENGINE"
    context_frequencies: tuple[str, ...] = ("d", "30m")
    setup_frequency: str = "5m"
    trigger_frequency: str = "1m"
    point_types: tuple[str, ...] = (
        "1buy",
        "2buy",
        "3buy",
        "1sell",
        "2sell",
        "3sell",
    )
    setup_classes_share_logic: bool = True
    trigger_classes_share_logic: bool = True
    trigger_must_match_side: bool = True
    third_class_keeps_center_geometry: bool = True
    small_to_large_second_class_allowed: bool = True
    provisional_points_actionable: bool = False

    def __post_init__(self) -> None:
        if self.contract_id != UNIFIED_SIGNAL_ALIGNMENT_CONTRACT_ID:
            raise ValueError("统一信号对齐合同身份发生变化")
        if (
            self.structure_authority != "STRICT_PHYSICAL_TIMEFRAME_ENGINE"
            or self.context_frequencies != ("d", "30m")
            or (self.setup_frequency, self.trigger_frequency) != ("5m", "1m")
            or self.point_types
            != ("1buy", "2buy", "3buy", "1sell", "2sell", "3sell")
            or not self.setup_classes_share_logic
            or not self.trigger_classes_share_logic
            or not self.trigger_must_match_side
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
