"""选股系统读取唯一严格结构账本的适配器。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chanlun.core.strict_structure.models import StrictEvidenceResult


SCREENING_STRUCTURE_SCOPE = "physical-timeframe-recursive"
SCREENING_STRUCTURE_FREQUENCIES = ("d", "30m", "5m", "1m")


def build_screening_evidence(
    cd,
    *,
    source_closed_at: datetime,
    structure_price_quantum: Decimal,
    price_basis_revision: str,
    strict_config_revision: str,
) -> StrictEvidenceResult:
    """返回 ``CL`` 已生成的唯一递归严格证据快照。"""

    if not isinstance(source_closed_at, datetime):
        raise TypeError("source_closed_at 必须是 datetime")
    if (
        not isinstance(structure_price_quantum, Decimal)
        or not structure_price_quantum.is_finite()
        or structure_price_quantum <= 0
    ):
        raise ValueError("structure_price_quantum 必须是正数")
    if not price_basis_revision or not strict_config_revision:
        raise ValueError("选股结构身份不能为空")
    getter = getattr(cd, "get_strict_evidence", None)
    if not callable(getter):
        raise TypeError("选股状态必须提供唯一严格证据接口")
    evidence = getter()
    if not isinstance(evidence, StrictEvidenceResult):
        raise TypeError("严格证据接口返回了无效对象")
    if (
        evidence.symbol != cd.get_code()
        or evidence.source_frequency != cd.get_frequency()
        or evidence.price_basis_revision != price_basis_revision
        or evidence.structure_price_quantum != structure_price_quantum
        or evidence.strict_config_revision != strict_config_revision
        or evidence.source_closed_at > source_closed_at
    ):
        raise ValueError("严格证据与选股上下文不一致")
    return evidence


__all__ = (
    "SCREENING_STRUCTURE_FREQUENCIES",
    "SCREENING_STRUCTURE_SCOPE",
    "build_screening_evidence",
)
