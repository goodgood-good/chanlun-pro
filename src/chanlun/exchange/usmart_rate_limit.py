"""Process-safe pacing for uSMART quote endpoints, independent of login tokens.

Official quote contract: https://api-doc.usmart.sg/zh-cn/quote-base.html
Each quote endpoint permits 120 calls/minute; basicinfo permits 20/minute.
Only hashed public host/channel/endpoint identity and admission clocks persist.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit

from chanlun.exchange.errors import USmartAPIError
from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout

SCHEMA = "usmart-quote-rate-v1"


class USmartRateLimitTimeout(USmartAPIError):
    retryable = False

    def __init__(self, retry_after=1):
        super().__init__("盈立行情请求排队超时，请稍后重试",
                         code="usmart_quote_rate_limit_timeout",
                         retry_after=max(1, math.ceil(retry_after)))

    def __reduce__(self):
        return type(self), (self.retry_after,)


class USmartRateLimitStateError(USmartAPIError):
    retryable = False

    def __init__(self):
        super().__init__("盈立行情请求限速状态不可用，请稍后重试",
                         code="usmart_quote_rate_limit_state_unavailable")

    def __reduce__(self):
        return type(self), ()


def _bucket(host, channel, endpoint):
    parsed = urlsplit(str(host).strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("uSMART rate-limit host must be an HTTP origin")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("uSMART quote origin cannot contain credentials")
    channel = str(channel).strip()
    endpoint = str(endpoint).strip().rstrip("/").rsplit("/", 1)[-1].lower()
    if not channel or not re.fullmatch(r"[a-z][a-z0-9_-]*", endpoint):
        raise ValueError("uSMART rate-limit channel and endpoint are required")
    origin = (parsed.scheme.lower(), parsed.hostname.lower(),
              parsed.port or (443 if parsed.scheme.lower() == "https" else 80))
    raw = json.dumps((origin, channel, endpoint), separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf8")).hexdigest(), (3.0 if endpoint == "basicinfo" else .5)


class USmartRateLimiter:
    """Gate immediately before each signed request, including every retry.

    A pending foreground request gets the next available slot ahead of bulk
    callers through a short-lived marker. No lock covers waiting or network I/O.
    Inject root/clocks/sleep for offline tests; a client can inject a no-op limiter
    when its HTTP session is a fixture rather than a live provider transport.
    """
    def __init__(self, root=None, *, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.root = None if root is None else Path(root)
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep

    def _root(self):
        if self.root is not None:
            return self.root
        from chanlun import config
        return config.get_data_path() / "quote_budget" / "usmart"

    @staticmethod
    def _read(path):
        try:
            state = json.loads(path.read_text(encoding="utf8"))
        except FileNotFoundError:
            return {"schema": SCHEMA, "next_at": 0.0, "interactive_until": 0.0}
        if (not isinstance(state, dict) or state.get("schema") != SCHEMA
                or any(type(state.get(k)) not in (int, float) or not math.isfinite(state[k]) or state[k] < 0
                       for k in ("next_at", "interactive_until"))):
            raise USmartRateLimitStateError()
        return state

    @staticmethod
    def _write(path, state):
        temporary = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(state, allow_nan=False), encoding="utf8")
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def wait(self, *, host, channel, endpoint, timeout=10.0, priority="interactive"):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("uSMART rate-limit timeout must be positive")
        if priority not in {"interactive", "background"}:
            raise ValueError("uSMART quote priority must be interactive or background")
        identity, interval = _bucket(host, channel, endpoint)
        path = self._root() / (identity + ".json")
        deadline = self.monotonic() + timeout
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise USmartRateLimitTimeout(interval)
            try:
                with interprocess_file_lock(path.with_suffix(".lock"),
                                            timeout_seconds=min(.05, remaining), poll_seconds=.005):
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        raise USmartRateLimitTimeout(interval)
                    now = self.clock()
                    if not math.isfinite(now) or now < 0:
                        raise USmartRateLimitStateError()
                    state = self._read(path)
                    due = max(state["next_at"], state["interactive_until"] if priority == "background" else 0)
                    delay = due - now
                    if delay <= 0:
                        self._write(path, {"schema": SCHEMA, "next_at": now + interval, "interactive_until": 0.0})
                        return
                    if priority == "interactive" and delay < remaining:
                        reserve = min(now + remaining, due + .05)
                        if reserve > state["interactive_until"]:
                            self._write(path, {**state, "interactive_until": reserve})
            except InterprocessLockTimeout:
                continue
            except USmartRateLimitStateError:
                raise
            except (OSError, ValueError, TypeError, KeyError) as exc:
                raise USmartRateLimitStateError() from exc
            remaining = deadline - self.monotonic()
            if delay >= remaining:
                raise USmartRateLimitTimeout(delay)
            self.sleep(delay)
