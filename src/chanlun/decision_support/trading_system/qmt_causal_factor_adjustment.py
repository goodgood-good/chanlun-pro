"""实时与回放板块共用的 QMT 因果公司行动复权。

QMT 的 ``front`` 序列便于绘图，但其历史值可能因后续公司行动而变化。
因此板块筛选下载成分股柱时保留 ``dividend_type='none'``，并且只应用
除权日在决策日期及以前的因子事件。最终账本修订号会进入每个合成事实标识。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import math
import re

import pandas as pd

from chanlun.decision_support.fingerprints import sha256_json
from chanlun.exchange.price_basis import (
    PriceBasisMetadata,
    build_price_basis_revision,
)


QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID = (
    "QMT_RAW_PRICE_DIVISOR_CAUSAL_EX_DATE"
)
_NORMALIZED_A_SHARE_CODE = re.compile(r"^(SH|SZ|BJ)\.([0-9]{6})$")
_FACTOR_FIELDS = (
    "interest",
    "stockBonus",
    "stockGift",
    "allotNum",
    "allotPrice",
    "gugai",
    "dr",
)
_PRICE_FIELDS = ("open", "high", "low", "close")
_SHA256_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _decimal(value: object, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"QMT factor {field} is invalid") from exc
    if not result.is_finite():
        raise ValueError(f"QMT factor {field} must be finite")
    return result


def _effective_on(value: object) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, bool):
        raise ValueError("QMT factor effective date is invalid")
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("QMT factor effective time must be finite")
        return pd.to_datetime(number, unit="ms", utc=True).date()
    text = str(value).strip()
    if re.fullmatch(r"[0-9]{8}", text):
        return datetime.strptime(text, "%Y%m%d").date()
    try:
        return date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError("QMT factor effective date is invalid") from exc


@dataclass(frozen=True, slots=True)
class QmtCausalFactorEvent:
    code: str
    effective_on: date
    interest: Decimal
    stock_bonus: Decimal
    stock_gift: Decimal
    allot_num: Decimal
    allot_price: Decimal
    gugai: Decimal
    raw_price_divisor: Decimal

    def __post_init__(self) -> None:
        if _NORMALIZED_A_SHARE_CODE.fullmatch(self.code) is None:
            raise ValueError("QMT factor code must be normalized")
        economics = (
            self.interest,
            self.stock_bonus,
            self.stock_gift,
            self.allot_num,
            self.allot_price,
            self.gugai,
        )
        if any(not value.is_finite() or value < 0 for value in economics):
            raise ValueError("QMT factor economics must be finite and non-negative")
        if (
            not self.raw_price_divisor.is_finite()
            or self.raw_price_divisor <= 0
        ):
            raise ValueError("QMT raw price divisor must be positive")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "code": self.code,
            "effective_on": self.effective_on.isoformat(),
            "interest": str(self.interest),
            "stock_bonus": str(self.stock_bonus),
            "stock_gift": str(self.stock_gift),
            "allot_num": str(self.allot_num),
            "allot_price": str(self.allot_price),
            "gugai": str(self.gugai),
            "raw_price_divisor": str(self.raw_price_divisor),
        }


def qmt_causal_factor_events_from_frame(
    *,
    code: str,
    frame: pd.DataFrame,
    not_before: date,
    not_after: date,
) -> tuple[QmtCausalFactorEvent, ...]:
    """规范化原生 ``get_divid_factors`` 结果，并剔除未来行。"""

    if _NORMALIZED_A_SHARE_CODE.fullmatch(code) is None:
        raise ValueError("QMT factor code must be normalized")
    if not isinstance(frame, pd.DataFrame):
        raise TypeError("QMT factor response must be a DataFrame")
    if not_before > not_after:
        raise ValueError("QMT factor range is inverted")
    if frame.empty:
        return ()
    if isinstance(frame.index, pd.RangeIndex) and "time" not in frame.columns:
        raise ValueError("QMT factor response has no effective date")
    missing = tuple(field for field in _FACTOR_FIELDS if field not in frame.columns)
    if missing:
        raise ValueError("QMT factor response is missing required fields")
    output: list[QmtCausalFactorEvent] = []
    for index, row in frame.iterrows():
        raw_effective = (
            row["time"]
            if isinstance(frame.index, pd.RangeIndex) and "time" in frame.columns
            else index
        )
        effective = _effective_on(raw_effective)
        if not not_before <= effective <= not_after:
            continue
        output.append(
            QmtCausalFactorEvent(
                code=code,
                effective_on=effective,
                interest=_decimal(row["interest"], "interest"),
                stock_bonus=_decimal(row["stockBonus"], "stockBonus"),
                stock_gift=_decimal(row["stockGift"], "stockGift"),
                allot_num=_decimal(row["allotNum"], "allotNum"),
                allot_price=_decimal(row["allotPrice"], "allotPrice"),
                gugai=_decimal(row["gugai"], "gugai"),
                raw_price_divisor=_decimal(row["dr"], "dr"),
            )
        )
    keys = tuple(value.effective_on for value in output)
    if len(keys) != len(set(keys)):
        raise ValueError("QMT factor response contains duplicate ex-dates")
    return tuple(sorted(output, key=lambda value: value.effective_on))


def qmt_causal_factor_events_from_objects(
    *,
    code: str,
    values: Sequence[object],
    not_after: date,
) -> tuple[QmtCausalFactorEvent, ...]:
    """将不可变回放因子对象适配到统一因果契约。"""

    output: list[QmtCausalFactorEvent] = []
    for value in values:
        event = QmtCausalFactorEvent(
            code=str(getattr(value, "code")),
            effective_on=_effective_on(getattr(value, "effective_on")),
            interest=_decimal(getattr(value, "interest"), "interest"),
            stock_bonus=_decimal(getattr(value, "stock_bonus"), "stock_bonus"),
            stock_gift=_decimal(getattr(value, "stock_gift"), "stock_gift"),
            allot_num=_decimal(getattr(value, "allot_num"), "allot_num"),
            allot_price=_decimal(getattr(value, "allot_price"), "allot_price"),
            gugai=_decimal(getattr(value, "gugai"), "gugai"),
            raw_price_divisor=_decimal(
                getattr(value, "raw_price_divisor"),
                "raw_price_divisor",
            ),
        )
        if event.code != code:
            raise ValueError("QMT factor crossed a symbol identity")
        if event.effective_on <= not_after:
            output.append(event)
    keys = tuple(value.effective_on for value in output)
    if len(keys) != len(set(keys)):
        raise ValueError("QMT factors contain duplicate ex-dates")
    return tuple(sorted(output, key=lambda value: value.effective_on))


def qmt_causal_factor_revision(
    *,
    members: Sequence[str],
    events_by_code: Mapping[str, Sequence[QmtCausalFactorEvent]],
    known_through: date,
) -> str:
    normalized = tuple(sorted(set(members)))
    if len(normalized) != len(tuple(members)) or any(
        _NORMALIZED_A_SHARE_CODE.fullmatch(value) is None for value in normalized
    ):
        raise ValueError("factor revision members must be unique normalized codes")
    if set(events_by_code) - set(normalized):
        raise ValueError("factor revision contains an unknown member")
    rows: list[dict[str, str]] = []
    for code in normalized:
        prior: date | None = None
        for event in events_by_code.get(code, ()):
            if event.code != code or event.effective_on > known_through:
                raise ValueError("factor revision contains non-causal evidence")
            if prior is not None and event.effective_on <= prior:
                raise ValueError(
                    "factor revision events must be unique and increasing"
                )
            rows.append(event.canonical_payload())
            prior = event.effective_on
    return sha256_json(
        {
            "schema": "chanlun-qmt-causal-sector-factor-ledger",
            "contract_id": QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
            "known_through": known_through.isoformat(),
            "members": normalized,
            "events": tuple(rows),
        }
    )


def apply_qmt_causal_factor_adjustment(
    frame: pd.DataFrame,
    *,
    code: str,
    events: Sequence[QmtCausalFactorEvent],
    date_column: str = "date",
) -> pd.DataFrame:
    """返回副本，使 OHLC 价格在已知除权日之间保持连续。"""

    result = frame.copy()
    if date_column not in result.columns:
        raise ValueError("factor-adjusted frame has no date column")
    missing = tuple(field for field in _PRICE_FIELDS if field not in result.columns)
    if missing:
        raise ValueError("factor-adjusted frame has no complete OHLC")
    sessions = pd.to_datetime(result[date_column]).dt.date
    multiplier = pd.Series(1.0, index=result.index, dtype="float64")
    prior: date | None = None
    for event in events:
        if event.code != code:
            raise ValueError("QMT factor crossed a symbol identity")
        if prior is not None and event.effective_on <= prior:
            raise ValueError("QMT factor events must be unique and increasing")
        multiplier.loc[sessions >= event.effective_on] *= float(
            event.raw_price_divisor
        )
        prior = event.effective_on
    for field in _PRICE_FIELDS:
        result[field] = pd.to_numeric(result[field], errors="coerce") * multiplier
    return result


def build_causal_sector_price_basis_metadata(
    *,
    provider: str,
    market: str,
    code: str,
    adjustment: str,
    structure_price_quantum: Decimal,
    factor_revision: str,
) -> PriceBasisMetadata:
    if _SHA256_ID.fullmatch(factor_revision) is None:
        raise ValueError("sector factor revision must be a sha256 identity")
    revision = build_price_basis_revision(
        provider=provider,
        market=market,
        code=code,
        adjustment=adjustment,
        structure_price_quantum=structure_price_quantum,
        adjustment_ledger=(
            {
                "contract_id": QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID,
                "factor_revision": factor_revision,
            },
        ),
    )
    return PriceBasisMetadata(
        structure_price_quantum=structure_price_quantum,
        price_basis_revision=revision,
        provider=provider,
        adjustment=adjustment,
    )


__all__ = (
    "QMT_CAUSAL_FACTOR_ADJUSTMENT_CONTRACT_ID",
    "QmtCausalFactorEvent",
    "apply_qmt_causal_factor_adjustment",
    "build_causal_sector_price_basis_metadata",
    "qmt_causal_factor_events_from_frame",
    "qmt_causal_factor_events_from_objects",
    "qmt_causal_factor_revision",
)
