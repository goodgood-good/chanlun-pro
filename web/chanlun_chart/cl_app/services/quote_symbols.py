"""Match supported quote-code variants within one explicit market."""
from collections.abc import Mapping

from chanlun.market import Market


def quote_code_identity(market: str, code: object) -> str:
    value = str(code or '').strip().upper()
    if market == Market.US.value:
        if value.startswith('US.'):
            value = value[3:]
        if value.endswith('.US'):
            value = value[:-3]
    elif market == Market.HK.value:
        if value.startswith(('KH.', 'HK.')):
            value = value[3:]
        if value.endswith('.HK'):
            value = value[:-3]
        value = value.zfill(5) if value.isdigit() else value
    elif market == Market.A.value:
        parts = value.split('.', 1)
        if len(parts) == 2 and parts[0] in {'SH', 'SZ', 'BJ'}:
            value = parts[0] + parts[1]
        elif len(parts) == 2 and parts[1] in {'SH', 'SZ', 'BJ'}:
            value = parts[1] + parts[0]
    return value


class QuoteLookup:
    """Prefer exact codes; accept only unambiguous equivalent quotes."""
    def __init__(self, market, quotes, *, equivalent=None):
        if not isinstance(quotes, Mapping):
            raise TypeError('quote response must be a mapping')
        self.market = market
        self.quotes = quotes
        self.equivalent = equivalent
        self.aliases = {}
        for code in quotes:
            identity = quote_code_identity(market, code)
            self.aliases.setdefault(identity, []).append(code)

    def get(self, code):
        if code in self.quotes:
            return self.quotes[code]
        matched = self.aliases.get(quote_code_identity(self.market, code), [])
        if len(matched) == 1:
            return self.quotes[matched[0]]
        if matched and self.equivalent is not None:
            first = self.quotes[matched[0]]
            if all(self.equivalent(first, self.quotes[key]) for key in matched[1:]):
                return first
        return None
