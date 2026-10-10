"""Publish independent native periods without waiting for a symbol's 1m work."""
from collections import defaultdict


PERIOD_ORDER = ('5m', '30m', '1m')


def period_tasks(stocks, frequencies):
    requested = set(frequencies)
    critical = [period for period in PERIOD_ORDER[:2] if period in requested]
    remaining = [period for period in frequencies if period not in critical]
    # Keep catalog holdings/watchlist order within each wave. Both important
    # periods can start while the earlier 1m work is still unstarted.
    return [{**stock, '_monitor_periods':[period]}
            for wave in (critical, remaining) for stock in stocks for period in wave]


class PeriodProgress:
    def __init__(self, stocks, frequencies):
        self.required = {(s.get('market','a'),s['code']):set(frequencies) for s in stocks}
        self.completed = defaultdict(set)
        self.finished = 0

    def accept(self, result):
        symbol = result.get('market','a'), result['code']
        periods = result.get('requested_frequencies') or [row['frequency'] for row in result['rows']]
        if symbol not in self.required or len(periods) != 1 or not set(periods) <= self.required[symbol]:
            raise ValueError('period result does not match the screening request')
        if self.completed[symbol] & set(periods):
            raise ValueError('screening period result was committed twice')
        self.completed[symbol].update(periods)
        if self.completed[symbol] == self.required[symbol]:
            self.finished += 1
        return self.finished
