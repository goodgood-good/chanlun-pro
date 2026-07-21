"""K 线 OHLC 精度归一化。

按标的所在市场/类型的主流精度，对 open/high/low/close 做严格四舍五入
（ROUND_HALF_UP），消除不同数据源间的精度差异与浮点噪声。

接入点：各 exchange_*.py 适配器的 klines() 在 return 前调用
normalize_kline_precision()。设计文档见
docs/superpowers/specs/2026-05-18-kline-precision-normalization-design.md
"""

from decimal import ROUND_HALF_UP, Decimal
import re
from typing import Optional

import pandas as pd

from chanlun.tools.log_util import LogUtil

# 各市场默认 OHLC 小数位数（A股 见 _a_share_decimals 按子类型区分）
_MARKET_DECIMALS = {
    "hk": 3,
    "us": 3,
    "futures": 3,
    "ny_futures": 3,
    "fx": 5,
    "currency": 8,
    "currency_spot": 8,
}

_A_STOCK_DECIMALS = 2  # A股 股票
_A_FUND_DECIMALS = 3   # A股 ETF/基金/可转债
_TDX_INDUSTRY_INDEX = re.compile(r"^SH\.880\d{3}$")


def resolve_tdx_industry_index_quantum(code: str) -> Optional[Decimal]:
    """Return the native two-decimal tick for a TDX 880 industry index."""

    return (
        Decimal("0.01")
        if _TDX_INDUSTRY_INDEX.fullmatch(str(code))
        else None
    )


def _a_share_decimals(code: str) -> int:
    """A股：代码数字部分首位 ∈ {1,5} 为基金/ETF/可转债（3 位），其余为股票（2 位）。

    沪市股票 6 开头、深市 0/3 开头、北交所 4/8 开头 → 股票；
    沪市基金/转债 5x/11x/13x、深市基金/转债 15x/16x/18x/12x → 首位 1 或 5。

    数字首位 ∈ {1,5} → 基金/转债；其余首位(0,2,3,4,6,7,8,9) 一律 → 股票（默认）。
    无数字字符（空串或纯字母代码）亦走默认分支，返回股票精度（2 位）。
    """
    digits = "".join(ch for ch in code if ch.isdigit())
    if digits and digits[0] in ("1", "5"):
        return _A_FUND_DECIMALS
    return _A_STOCK_DECIMALS


def resolve_decimals(market: Optional[str], code: str) -> Optional[int]:
    """解析某标的的目标小数位数；未知市场返回 None。"""
    if market == "a":
        return _a_share_decimals(code)
    return _MARKET_DECIMALS.get(market)


def resolve_structure_price_quantum(
    market: Optional[str], code: str
) -> Optional[Decimal]:
    """返回 OHLC 归一化规则对应的严格结构价格量子。"""

    decimals = resolve_decimals(market, code)
    if decimals is None:
        return None
    return Decimal(1).scaleb(-decimals)


def _round_half_up(value: Optional[float], decimals: int) -> Optional[float]:
    """对单个数值做严格四舍五入（ROUND_HALF_UP）。

    None / NaN / inf 等非有限值原样返回。任何转换异常也原样返回，
    保证归一不会破坏数据。
    """
    if value is None:
        return value
    try:
        d = Decimal(str(value))
    except (ValueError, ArithmeticError):
        return value
    if not d.is_finite():
        return value
    quant = Decimal(1).scaleb(-decimals)  # 10^-decimals，如 decimals=3 → 0.001
    return float(d.quantize(quant, rounding=ROUND_HALF_UP))


_OHLC_COLUMNS = ("open", "high", "low", "close")


def normalize_kline_precision(df: Optional[pd.DataFrame], market: Optional[str], code: str) -> Optional[pd.DataFrame]:
    """把 K 线 DataFrame 的 OHLC 四列按市场/标的精度严格四舍五入。

    - volume 等其它列原样保留。
    - df 为 None 或空时原样返回。
    - 未知市场记 WARNING 并返回未改动的 df。
    - 不在原地修改调用方对象（内部 copy 后处理）。
    - 归一是增强功能：任何异常都记 WARNING 并返回原 df，绝不让 klines() 失败。
    """
    if df is None or len(df) == 0:
        return df
    try:
        decimals = resolve_decimals(market, code)
        if decimals is None:
            LogUtil.warning(
                f"[kline_precision] 未知市场 {market}，跳过精度归一: {code}"
            )
            return df
        out = df.copy()
        for col in _OHLC_COLUMNS:
            if col in out.columns:
                out[col] = out[col].map(lambda v: _round_half_up(v, decimals))
        return out
    except Exception as e:
        LogUtil.warning(
            f"[kline_precision] 归一失败 market={market} code={code}: {e}"
        )
        return df
