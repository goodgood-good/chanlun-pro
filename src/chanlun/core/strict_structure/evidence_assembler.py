"""严格结构生成买卖点、背驰与证据快照的唯一生产装配器。"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from chanlun.core.strict_structure import signals
from chanlun.core.strict_structure.divergence import collect_formal_divergence_ledger
from chanlun.core.strict_structure.identity import build_strict_evidence_revision
from chanlun.core.strict_structure.models import (
    CenterLevelResult,
    StrictEvidenceResult,
    StrictStructureResult,
)


def empty_stroke_center_observations(
    price_basis_revision: str,
) -> CenterLevelResult:
    """返回不参与决策的空笔中枢观察账本。"""

    return CenterLevelResult(
        structural_level=0,
        price_basis_revision=price_basis_revision,
        centers=(),
        previews=(),
        events=(),
        locked_unit_count=0,
        replay_from=0,
    )


class StrictEvidenceAssembler:
    """在一个严格结构快照上统一装配六类点、背驰和证据身份。

    实时、图表、选股与历史回放可以用不同方式得到因果结构输入，但结构输入
    一旦确定，后续都必须通过本类生成确认点、接近点、背驰账本和修订号。
    """

    def __init__(
        self,
        *,
        symbol: str,
        source_frequency: str,
        source_closed_at: datetime,
        price_basis_revision: str,
        structure_price_quantum: Decimal,
        strict_config_revision: str,
        structure: StrictStructureResult,
        strength=None,
        projection_cache=None,
    ) -> None:
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("严格证据标的不能为空")
        if not isinstance(source_frequency, str) or not source_frequency.strip():
            raise ValueError("严格证据源周期不能为空")
        if not isinstance(source_closed_at, datetime):
            raise TypeError("严格证据收盘时点必须是 datetime")
        if not isinstance(structure, StrictStructureResult):
            raise TypeError("严格证据结构类型无效")
        if (
            not isinstance(price_basis_revision, str)
            or not price_basis_revision.strip()
            or structure.price_basis_revision != price_basis_revision
        ):
            raise ValueError("严格证据价格基准与结构不一致")
        if (
            not isinstance(structure_price_quantum, Decimal)
            or not structure_price_quantum.is_finite()
            or structure_price_quantum <= 0
        ):
            raise ValueError("严格证据价格量子必须为正数")
        if (
            not isinstance(strict_config_revision, str)
            or not strict_config_revision.strip()
        ):
            raise ValueError("严格配置修订号不能为空")

        self.symbol = symbol
        self.source_frequency = source_frequency
        self.source_closed_at = source_closed_at
        self.price_basis_revision = price_basis_revision
        self.structure_price_quantum = structure_price_quantum
        self.strict_config_revision = strict_config_revision
        self.structure = structure
        self._engine = signals.StrictSignalEngine(
            structure=structure,
            strength=strength,
            price_quantum=structure_price_quantum,
            projection_cache=projection_cache,
        )
        self._confirmed_points = None
        self._approaching_points = None
        self._divergences = None

    def confirmed_points(self):
        """返回本结构快照唯一的正式六类点账本。"""

        if self._confirmed_points is None:
            self._confirmed_points = self._engine.confirmed_points()
        return self._confirmed_points

    def approaching_points(self):
        """返回本结构快照唯一的盘中接近点账本。"""

        if self._approaching_points is None:
            self._approaching_points = self._engine.approaching_points(
                self.source_closed_at
            )
        return self._approaching_points

    def divergences(self):
        """从正式结构及其确认点汇总唯一背驰账本。"""

        if self._divergences is None:
            self._divergences = collect_formal_divergence_ledger(
                self.structure,
                self.confirmed_points(),
            )
        return self._divergences

    def evidence(
        self,
        *,
        stroke_center_observations: CenterLevelResult,
    ) -> StrictEvidenceResult:
        """生成身份完整的原子严格证据快照。"""

        if not isinstance(stroke_center_observations, CenterLevelResult):
            raise TypeError("笔中枢观察账本类型无效")
        confirmed_points = self.confirmed_points()
        divergences = self.divergences()
        return StrictEvidenceResult(
            symbol=self.symbol,
            source_frequency=self.source_frequency,
            source_closed_at=self.source_closed_at,
            price_basis_revision=self.price_basis_revision,
            structure_price_quantum=self.structure_price_quantum,
            strict_config_revision=self.strict_config_revision,
            structure_revision=build_strict_evidence_revision(
                symbol=self.symbol,
                source_frequency=self.source_frequency,
                price_basis_revision=self.price_basis_revision,
                strict_config_revision=self.strict_config_revision,
                structure=self.structure,
                confirmed_points=confirmed_points,
                divergences=divergences,
            ),
            structure=self.structure,
            stroke_center_observations=stroke_center_observations,
            confirmed_points=confirmed_points,
            approaching_points=self.approaching_points(),
            divergences=divergences,
        )


__all__ = ("StrictEvidenceAssembler", "empty_stroke_center_observations")
