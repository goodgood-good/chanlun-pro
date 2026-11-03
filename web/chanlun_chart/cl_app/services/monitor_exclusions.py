"""Explicit quote-only preferences leave watchlists and chart data intact."""
from functools import lru_cache
import os

from chanlun.market import Market

ENVIRONMENT_KEY = 'CHANLUN_MONITOR_QUOTE_ONLY_SYMBOLS'


@lru_cache(maxsize=16)
def _parse(raw):
    result = set()
    markets = {market.value for market in Market}
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        market, separator, code = item.partition(':')
        if not separator or market not in markets or not code or any(c.isspace() for c in code):
            raise ValueError('仅看行情的监听配置包含无效标的')
        result.add(item)
    return frozenset(result)


def quote_only_symbols():
    return _parse(os.environ.get(ENVIRONMENT_KEY, ''))
