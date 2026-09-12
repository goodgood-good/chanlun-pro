"""Bounded market-state hints for chart cache policy, never trading permission."""

from collections import OrderedDict
from dataclasses import dataclass, field
import threading
import time

from chanlun.tools.daemon_executor import DaemonExecutor


MARKET_STATE_WAIT_SECONDS = 0.05
MARKET_STATE_TTL_SECONDS = 30.0


@dataclass
class _Flight:
    market: str
    started: float
    observed_at: float
    finished: threading.Event = field(default_factory=threading.Event)


class ChartMarketStateCache:
    """One unfinished call per market, sharing a fixed bounded daemon pool.

    A timed-out reader leaves the original provider call running. Later readers
    join it, so slow native calls cannot create an unbounded queue of retries.
    Expired closed-market hints are never served while that call is pending.
    """

    def __init__(self, *, clock=time.monotonic, max_workers=2, max_pending=8):
        self._clock = clock
        self._max_workers = max(1, int(max_workers))
        self._max_pending = max(1, int(max_pending))
        self._lock = threading.Lock()
        self._cache = OrderedDict()
        self._flights = {}
        self._executor = None
        self._closed = False

    def _current(self, market, observed_at):
        entry = self._cache.get(market)
        if entry is None:
            return None
        value, completed_at = entry
        if not 0 <= observed_at - completed_at < MARKET_STATE_TTL_SECONDS:
            self._cache.pop(market, None)
            return None
        self._cache.move_to_end(market)
        return value

    def _load(self, flight, loader):
        value = True
        try:
            result = loader()
            # None is an unknown state. Numeric/string/enum lookalikes cannot
            # assert that a market is closed or grant any other qualification.
            if result is True or result is False:
                value = result
        except BaseException:
            pass
        finally:
            completed_at = flight.observed_at + (self._clock() - flight.started)
            with self._lock:
                if not self._closed and self._flights.get(flight.market) is flight:
                    self._cache[flight.market] = (value, completed_at)
                    self._cache.move_to_end(flight.market)
                    while len(self._cache) > 16:
                        self._cache.popitem(last=False)
                    del self._flights[flight.market]
            flight.finished.set()

    def read(self, market, loader, *, now=None):
        deadline = time.monotonic() + MARKET_STATE_WAIT_SECONDS
        started = self._clock()
        observed_at = started if now is None else float(now)
        with self._lock:
            if self._closed:
                return True
            value = self._current(market, observed_at)
            if value is not None:
                return value
            flight = self._flights.get(market)
            if flight is None:
                if len(self._flights) >= self._max_workers + self._max_pending:
                    return True
                flight = _Flight(market, started, observed_at)
                self._flights[market] = flight
                try:
                    if self._executor is None:
                        self._executor = DaemonExecutor(
                            max_workers=self._max_workers,
                            max_pending=self._max_pending,
                            thread_name_prefix="ChartMarketState",
                        )
                    self._executor.submit(self._load, flight, loader)
                except RuntimeError:
                    del self._flights[market]
                    return True

        flight.finished.wait(min(MARKET_STATE_WAIT_SECONDS, max(0.0, deadline - time.monotonic())))
        with self._lock:
            value = self._current(market, observed_at + (self._clock() - started))
            return True if value is None else value

    def shutdown(self):
        """Release queued work without waiting for an uninterruptible provider."""
        with self._lock:
            self._closed = True
            for flight in self._flights.values():
                flight.finished.set()
            self._flights.clear()
            self._cache.clear()
            executor = self._executor
        if executor is not None:
            executor.shutdown(wait=False, cancel_futures=True)
