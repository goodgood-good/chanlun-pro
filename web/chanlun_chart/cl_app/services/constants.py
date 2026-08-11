"""
Shared market and chart constants for cl_app.

This module centralizes static mappings and derived values previously
defined inline in the Flask app. Importing from here reduces duplication
and makes it easier to test and evolve supported markets and resolutions.
"""

import threading
import time

from tzlocal import get_localzone

from chanlun.market import Market
from chanlun.exchange import get_exchange
from chanlun.tools.log_util import LogUtil


# 项目中的周期与 tv 的周期对应表
frequency_maps = {
    "10s": "10S",
    "30s": "30S",
    "1m": "1",
    "2m": "2",
    "3m": "3",
    "5m": "5",
    "10m": "10",
    "15m": "15",
    "30m": "30",
    "60m": "60",
    "120m": "120",
    "3h": "180",
    "4h": "240",
    "d": "1D",
    "2d": "2D",
    "w": "1W",
    "m": "1M",
    "q": "3M",
    "y": "12M",
}

# tv 的周期与项目中的周期对应表
resolution_maps = dict(zip(frequency_maps.values(), frequency_maps.keys()))


_ALL_MARKETS = [
    ("a", Market.A),
    ("hk", Market.HK),
    ("fx", Market.FX),
    ("us", Market.US),
    ("futures", Market.FUTURES),
    ("ny_futures", Market.NY_FUTURES),
    ("currency", Market.CURRENCY),
    ("currency_spot", Market.CURRENCY_SPOT),
]

# Capability metadata must remain usable when a configured quote adapter is
# temporarily offline.  These are deliberately the smallest cross-provider
# contracts used by the chart router, not synthetic market data.  Readiness
# still reports the failed metadata load; the fallback merely prevents a
# transient connection failure from misclassifying ordinary 1m/5m requests as
# unsupported.  Quarterly bars are an explicit FX-only contract.
_STOCK_FUTURES_FREQUENCY_FALLBACK = (
    "1m",
    "5m",
    "15m",
    "30m",
    "60m",
    "d",
    "w",
    "m",
)
_MARKET_FREQUENCY_FALLBACKS = {
    "a": _STOCK_FUTURES_FREQUENCY_FALLBACK,
    "hk": _STOCK_FUTURES_FREQUENCY_FALLBACK,
    "us": _STOCK_FUTURES_FREQUENCY_FALLBACK,
    "futures": _STOCK_FUTURES_FREQUENCY_FALLBACK,
    "ny_futures": _STOCK_FUTURES_FREQUENCY_FALLBACK,
    "fx": (*_STOCK_FUTURES_FREQUENCY_FALLBACK, "q"),
    "currency": ("1m", "5m", "15m", "30m", "60m", "3h", "4h", "d", "w"),
    "currency_spot": ("1m", "5m", "15m", "30m", "60m", "4h", "d", "w"),
}


class _LazyMarketDict(dict):
    """Load and cache market metadata independently for each market."""

    def __init__(
        self,
        builder,
        *,
        markets=None,
        fallback_factory=list,
        fallback_builder=None,
        retry_seconds=30.0,
        load_wait_seconds=0.1,
        load_timeout_seconds=5.0,
        clock=time.monotonic,
    ):
        super().__init__()
        self._builder = builder
        self._markets = dict(_ALL_MARKETS if markets is None else markets)
        self._fallback_factory = fallback_factory
        self._fallback_builder = fallback_builder
        self._retry_seconds = retry_seconds
        self._load_wait_seconds = max(0.0, float(load_wait_seconds))
        self._load_timeout_seconds = max(0.0, float(load_timeout_seconds))
        self._clock = clock
        self._states = {key: "unloaded" for key in self._markets}
        self._failed_at = {}
        self._market_locks = {key: threading.Lock() for key in self._markets}
        self._state_lock = threading.Lock()
        self._attempts = {}
        self._closed = False

        # Templates address every supported market directly, so initialize all keys.
        for key in self._markets:
            dict.__setitem__(self, key, self._fallback_value(key))

    def _fallback_value(self, key):
        if self._fallback_builder is not None:
            return self._fallback_builder(key)
        return self._fallback_factory()

    def _cached_value_if_available(self, key):
        with self._state_lock:
            state = self._states[key]
            if state == "loaded":
                return True, dict.__getitem__(self, key)
            if state == "failed":
                failed_at = self._failed_at[key]
                if self._clock() - failed_at < self._retry_seconds:
                    return True, dict.__getitem__(self, key)
            return False, None

    def _record_failure(self, key, reason, attempt=None):
        fallback = self._fallback_value(key)
        failed_at = self._clock()
        with self._state_lock:
            if attempt is not None and self._attempts.get(key) is not attempt:
                return dict.__getitem__(self, key)
            dict.__setitem__(self, key, fallback)
            self._failed_at[key] = failed_at
            self._states[key] = "failed"
        LogUtil.warning(
            f"获取 {key} 市场元数据失败，将在 {self._retry_seconds:g} 秒后重试: {reason}"
        )
        return fallback

    def _run_builder_attempt(self, key, attempt):
        try:
            value = self._builder(key, self._markets[key])
            if not value:
                raise ValueError("metadata builder returned an empty value")
        except Exception as exc:
            with self._state_lock:
                active = self._attempts.get(key) is attempt and not self._closed
            if active:
                self._record_failure(key, exc)
        else:
            with self._state_lock:
                if self._attempts.get(key) is attempt and not self._closed:
                    dict.__setitem__(self, key, value)
                    self._failed_at.pop(key, None)
                    self._states[key] = "loaded"
        finally:
            with self._state_lock:
                if self._attempts.get(key) is attempt:
                    self._attempts.pop(key, None)
            attempt["done"].set()

    def _start_builder_attempt(self, key):
        done = threading.Event()
        attempt = {"done": done, "thread": None}
        thread = threading.Thread(
            target=self._run_builder_attempt,
            args=(key, attempt),
            daemon=True,
            name=f"MarketMetadata-{key}",
        )
        attempt["thread"] = thread
        with self._state_lock:
            if self._closed:
                return None
            existing = self._attempts.get(key)
            if existing is not None:
                return existing
            self._states[key] = "loading"
            self._attempts[key] = attempt
        thread.start()
        return attempt

    def _load_key(self, key):
        if key not in self._markets:
            raise KeyError(key)

        available, value = self._cached_value_if_available(key)
        if available:
            return value

        market_lock = self._market_locks[key]
        if not market_lock.acquire(timeout=self._load_wait_seconds):
            with self._state_lock:
                return dict.__getitem__(self, key)

        try:
            available, value = self._cached_value_if_available(key)
            if available:
                return value

            with self._state_lock:
                if self._closed:
                    return dict.__getitem__(self, key)
                attempt = self._attempts.get(key)

            if attempt is not None:
                with self._state_lock:
                    return dict.__getitem__(self, key)

            attempt = self._start_builder_attempt(key)
            if attempt is None:
                with self._state_lock:
                    return dict.__getitem__(self, key)

            if not attempt["done"].wait(self._load_timeout_seconds):
                return self._record_failure(
                    key,
                    f"timeout after {self._load_timeout_seconds:g}s",
                    attempt=attempt,
                )

            with self._state_lock:
                return dict.__getitem__(self, key)
        finally:
            market_lock.release()

    def _load_all(self):
        for key in self._markets:
            self._load_key(key)

    def start(self):
        """Allow loads again after a prior lifecycle shutdown."""
        with self._state_lock:
            self._closed = False
            # A shutdown followed by a new app lifecycle is an explicit retry
            # boundary.  Do not retain a pre-shutdown failure for another
            # retry_seconds window: that leaves supported resolutions and
            # default symbols stuck at their empty fallbacks in the new app.
            for key, state in self._states.items():
                if state == "failed" or (
                    state == "loading" and key not in self._attempts
                ):
                    self._states[key] = "unloaded"
                    self._failed_at.pop(key, None)

    def shutdown(self, timeout=0.0):
        """Stop accepting loads and wait only briefly for active attempts."""
        with self._state_lock:
            self._closed = True
            threads = [
                attempt["thread"]
                for attempt in self._attempts.values()
                if attempt.get("thread") is not None
            ]
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(remaining)
        return not any(thread.is_alive() for thread in threads)

    def cached_snapshot(self, keys=None):
        """Return cached or fallback values without invoking any builder."""
        selected_keys = tuple(self._markets if keys is None else keys)
        with self._state_lock:
            return {key: dict.__getitem__(self, key) for key in selected_keys}
    def snapshot(self, keys=None):
        """Load selected markets and return their values as a plain dict."""
        selected_keys = tuple(self._markets if keys is None else keys)
        for key in selected_keys:
            self._load_key(key)
        with self._state_lock:
            return {key: dict.__getitem__(self, key) for key in selected_keys}

    def status(self, key):
        """Return cache state without starting or waiting for a metadata load."""
        if key not in self._markets:
            raise KeyError(key)
        with self._state_lock:
            state = self._states[key]
            ready = state == "loaded" and bool(dict.__getitem__(self, key))
        return {"state": state, "ready": ready}

    def __getitem__(self, key):
        return self._load_key(key)

    def __contains__(self, key):
        return key in self._markets

    def get(self, key, default=None):
        if key not in self._markets:
            return default
        return self._load_key(key)

    def keys(self):
        return dict.keys(self)

    def values(self):
        self._load_all()
        return dict.values(self)

    def items(self):
        self._load_all()
        return dict.items(self)

    def __iter__(self):
        return dict.__iter__(self)

    def __len__(self):
        return dict.__len__(self)


def _build_market_frequencys(key, market):
    frequencies = list(get_exchange(market).support_frequencys().keys())
    # ``q`` was added to the shared TradingView map solely for the TDX FX
    # adapter.  Some configurable providers advertise a synthetic quarterly
    # frequency even though the corresponding routed market cannot honour the
    # same contract.  Keep the web/API capability boundary deterministic and
    # fail closed for a hand-crafted 3M request outside FX.
    if key != "fx":
        frequencies = [value for value in frequencies if value != "q"]
    return frequencies


def _build_market_default_codes(_key, market):
    return get_exchange(market).default_code()


# 懒加载：每个市场独立缓存；失败值保留 30 秒后重试
market_frequencys = _LazyMarketDict(
    _build_market_frequencys,
    fallback_factory=list,
    fallback_builder=lambda key: list(_MARKET_FREQUENCY_FALLBACKS[key]),
)
market_default_codes = _LazyMarketDict(
    _build_market_default_codes,
    fallback_factory=lambda: "",
)


def start_market_metadata_loaders():
    for loader in (market_frequencys, market_default_codes):
        loader.start()

def shutdown_market_metadata_loaders(timeout=0.0):
    """Stop global metadata loaders without waiting indefinitely."""
    loaders = (market_frequencys, market_default_codes)
    deadline = time.monotonic() + max(0.0, float(timeout))
    stopped = True
    for loader in loaders:
        remaining = max(0.0, deadline - time.monotonic())
        stopped = loader.shutdown(remaining) and stopped
    return stopped

# 各个市场的交易时间
market_session = {
    "a": "24x7",
    "hk": "24x7",
    "fx": "24x7",
    "us": "24x7",
    "futures": "24x7",
    "ny_futures": "24x7",
    "currency": "24x7",
    "currency_spot": "24x7",
}


# 各个交易所的时区 统一时区
market_timezone = {
    "a": "Asia/Shanghai",
    "hk": "Asia/Shanghai",
    "fx": "Asia/Shanghai",
    "us": "America/New_York",
    "futures": "Asia/Shanghai",
    "ny_futures": "Asia/Shanghai",
    "currency": str(get_localzone()),
    "currency_spot": str(get_localzone()),
}


# 市场类型
market_types = {
    "a": "stock",
    "hk": "stock",
    "fx": "stock",
    "us": "stock",
    "futures": "futures",
    "ny_futures": "futures",
    "currency": "crypto",
    "currency_spot": "crypto",
}


__all__ = [
    "frequency_maps",
    "resolution_maps",
    "market_frequencys",
    "market_default_codes",
    "market_session",
    "market_timezone",
    "market_types",
    "start_market_metadata_loaders",
    "shutdown_market_metadata_loaders",
]
