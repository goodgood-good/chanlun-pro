"""K 线 OHLC 精度归一化。

按标的所在市场/类型的主流精度，对 open/high/low/close 做严格四舍五入
（ROUND_HALF_UP），消除不同数据源间的精度差异与浮点噪声。

接入点：各 exchange_*.py 适配器的 klines() 在 return 前调用
normalize_kline_precision()。设计文档见
docs/superpowers/specs/2026-05-18-kline-precision-normalization-design.md
"""

from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

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


def _round_half_up(value, decimals: int):
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
