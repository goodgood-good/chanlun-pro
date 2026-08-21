"""把已收盘行情帧转换为统一严格结构证据的唯一生产入口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import cast

import pandas as pd

from chanlun.core.cl import CL
from chanlun.core.strict_structure.models import StrictEvidenceResult
from chanlun.decision_support.fingerprints import normalize_datetime
from chanlun.decision_support.trading_system.runtime_config import (
    strict_cl_config,
    strict_snapshot_price_metadata,
)
from chanlun.decision_support.trading_system.screening_structure import (
    build_screening_evidence,
)


_FRAME_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def _validated_frame_context(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
) -> tuple[datetime, dict[str, object], pd.DataFrame]:
    if not isinstance(code, str) or not code:
        raise ValueError("screening code is required")
    if not isinstance(frequency, str) or not frequency:
        raise ValueError("screening frequency is required")
    if not isinstance(frame, pd.DataFrame) or frame.empty:
        raise ValueError("screening frame must contain closed bars")
    if any(column not in frame.columns for column in _FRAME_COLUMNS):
        raise ValueError("screening frame requires date and OHLCV columns")
    closed_at = normalize_datetime(as_of, "as_of")
    latest = normalize_datetime(
        pd.Timestamp(frame["date"].iloc[-1]).to_pydatetime(),
        "latest frame close",
    )
    if latest > closed_at:
        raise ValueError("screening frame contains bars after as_of")

    metadata = strict_snapshot_price_metadata(frame)
    config = strict_cl_config(
        structure_price_quantum=metadata.structure_price_quantum,
        price_basis_revision=metadata.price_basis_revision,
    )
    snapshot = frame.loc[:, list(_FRAME_COLUMNS)].copy()
    snapshot.attrs = dict(frame.attrs)
    return closed_at, config, snapshot


def validated_incremental_prefix_matches(
    previous: pd.DataFrame,
    current: pd.DataFrame,
) -> bool:
    """确认当前帧只更新旧末根并在其后追加，不接受滑窗或历史修订。"""

    previous_count = len(previous)
    if previous_count <= 0 or len(current) < previous_count:
        return False
    if pd.Timestamp(previous["date"].iloc[-1]) != pd.Timestamp(
        current["date"].iloc[previous_count - 1]
    ):
        return False
    if previous_count == 1:
        return True
    # 最后一根允许由盘中值更新为最终收盘值；再早的任意事实变化都必须完整重建。
    return previous.iloc[:-1].reset_index(drop=True).equals(
        current.iloc[: previous_count - 1].reset_index(drop=True)
    )


@dataclass(slots=True)
class ScreeningRuntimeState:
    """一只标的、一个物理周期的严格 ``CL`` 增量状态。"""

    code: str
    frequency: str
    market: str = "a"
    _state: CL | None = None
    _frame: pd.DataFrame | None = None
    update_count: int = 0
    incremental_update_count: int = 0
    rebuild_count: int = 0
    last_update_incremental: bool | None = None

    @property
    def cl_state(self) -> CL | None:
        """返回本次已验证的严格运行态，供只读证据投影复用。"""

        return self._state

    @property
    def retained_frame_start(self) -> datetime | None:
        """Return the stable left anchor used by a hot incremental generation."""

        if self._frame is None or self._frame.empty:
            return None
        value = pd.Timestamp(self._frame["date"].iloc[0])
        return value.to_pydatetime()

    @property
    def retained_frame_count(self) -> int:
        return 0 if self._frame is None else len(self._frame)

    def update_from_frame(
        self,
        *,
        frame: pd.DataFrame,
        as_of: datetime,
    ) -> "ScreeningRuntimeUpdate":
        """更新统一 CL 状态；无法证明前缀连续时自动完整重建。"""

        closed_at, config, snapshot = _validated_frame_context(
            code=self.code,
            frequency=self.frequency,
            frame=frame,
            as_of=as_of,
        )
        previous_frame = self._frame
        reusable = bool(
            self._state is not None
            and previous_frame is not None
            and self._state.get_code() == self.code
            and self._state.get_frequency() == self.frequency
            and self._state.market == self.market.strip().lower()
            and self._state.get_config() == config
            and validated_incremental_prefix_matches(previous_frame, snapshot)
        )
        if not reusable:
            self._state = CL(
                self.code,
                self.frequency,
                config,
                market=self.market,
            )
        assert self._state is not None
        if reusable:
            self._state.process_validated_incremental_klines(frame)
        else:
            self._state.process_klines(frame)
        self._frame = snapshot
        self.update_count += 1
        self.last_update_incremental = reusable
        if reusable:
            self.incremental_update_count += 1
        else:
            self.rebuild_count += 1
        metadata = strict_snapshot_price_metadata(frame)
        return ScreeningRuntimeUpdate(
            state=self._state,
            closed_at=closed_at,
            structure_price_quantum=metadata.structure_price_quantum,
            price_basis_revision=metadata.price_basis_revision,
            strict_config_revision=cast(str, config["strict_config_revision"]),
            incremental_reused=reusable,
        )

    def evidence_from_frame(
        self,
        *,
        frame: pd.DataFrame,
        as_of: datetime,
    ) -> StrictEvidenceResult:
        update = self.update_from_frame(frame=frame, as_of=as_of)
        evidence = update.evidence()
        self.release_evidence_cache()
        return evidence

    def release_evidence_cache(self) -> None:
        """投影完成后释放完整递归账本，保留下一分钟所需的增量基础状态。"""

        if self._state is not None:
            self._state.release_strict_evidence_cache()


@dataclass(frozen=True, slots=True)
class ScreeningRuntimeUpdate:
    """一次已处理行情帧及构建严格证据所需的不可变上下文。"""

    state: CL
    closed_at: datetime
    structure_price_quantum: Decimal
    price_basis_revision: str
    strict_config_revision: str
    incremental_reused: bool

    def evidence(self) -> StrictEvidenceResult:
        return build_screening_evidence(
            self.state,
            source_closed_at=self.closed_at,
            structure_price_quantum=self.structure_price_quantum,
            price_basis_revision=self.price_basis_revision,
            strict_config_revision=self.strict_config_revision,
        )


def screening_evidence_from_frame(
    *,
    code: str,
    frequency: str,
    frame: pd.DataFrame,
    as_of: datetime,
    market: str = "a",
) -> StrictEvidenceResult:
    """根据已收盘行情帧构建唯一递归严格结构证据。

    图表适配、实时筛选、历史回放和选股都必须从这里进入结构引擎。无状态调用创建
    一次性状态；需要分钟级复用的调用方保留 :class:`ScreeningRuntimeState`，两者执行
    完全相同的 ``CL`` 与证据构建逻辑。
    """

    return ScreeningRuntimeState(
        code=code,
        frequency=frequency,
        market=market,
    ).evidence_from_frame(frame=frame, as_of=as_of)


__all__ = (
    "ScreeningRuntimeState",
    "ScreeningRuntimeUpdate",
    "screening_evidence_from_frame",
    "validated_incremental_prefix_matches",
)
