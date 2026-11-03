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
from uuid import uuid4

from chanlun.exchange.errors import USmartAPIError
from chanlun.persistence.file_lock import interprocess_file_lock, InterprocessLockTimeout

SCHEMA = "usmart-quote-rate-v2"
MAX_WAITERS = 128
INTERACTIVE_BURST = 3


class USmartRateLimitTimeout(USmartAPIError):
    retryable = False

    def __init__(self, retry_after=1, *, wait_seconds=0.0, queue_depth=0):
        super().__init__("盈立行情请求排队超时，请稍后重试",
                         code="usmart_quote_rate_limit_timeout",
                         retry_after=max(1, math.ceil(retry_after)))
        self.wait_seconds, self.queue_depth = wait_seconds, queue_depth

    def __reduce__(self):
        return type(self), (self.retry_after,), self.__dict__


class USmartRateLimitCancelled(USmartAPIError):
    retryable = False

    def __init__(self):
        super().__init__("盈立行情请求排队已取消", code="usmart_quote_rate_limit_cancelled")

    def __reduce__(self):
        return type(self), ()


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


def _process_alive(pid):
    if pid == os.getpid():
        return True
    if os.name == "nt":
        from chanlun.screening.worker_lifetime import process_identity
        return process_identity(pid)["alive"] is not False
    try:
        os.kill(pid, 0)  # POSIX liveness check only; never used on Windows.
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _winner(queue, streak):
    foreground = next((item for item in queue if item["priority"] == "interactive"), None)
    background = next((item for item in queue if item["priority"] == "background"), None)
    return foreground if foreground and (background is None or streak < INTERACTIVE_BURST) else background


class USmartRateLimiter:
    """Gate immediately before each signed request, including every retry.

    Requests are FIFO within each priority. A waiting background request gets
    a turn after at most three foreground admissions. Dead/expired callers are
    removed; no lock covers sleeping or network I/O, and permits are not booked
    into the future. Successful calls return non-sensitive waiting metrics.
    Inject root/clocks/sleep for offline tests; a client can inject a no-op limiter
    when its HTTP session is a fixture rather than a live provider transport.
    """
    def __init__(self, root=None, *, clock=time.time, monotonic=time.monotonic,
                 sleep=time.sleep, is_alive=_process_alive, bucket=None):
        self.root = None if root is None else Path(root)
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.is_alive = is_alive
        self.bucket = bucket or _bucket

    def _root(self):
        if self.root is not None:
            return self.root
        from chanlun import config
        return config.get_data_path() / "quote_budget" / "usmart"

    @staticmethod
    def _read(path, interval):
        try:
            state = json.loads(path.read_text(encoding="utf8"))
        except FileNotFoundError:
            return {"schema": SCHEMA, "next_at": 0.0, "last_now": 0.0, "streak": 0, "queue": []}
        if isinstance(state, dict) and state.get("schema") == "usmart-quote-rate-v1":
            if not all(_finite(state.get(k)) for k in ("next_at", "interactive_until")):
                raise USmartRateLimitStateError()
            # Preserve the old admission clock; migration must not create a burst.
            state = {"schema": SCHEMA, "next_at": state["next_at"],
                     "last_now": max(0.0, state["next_at"] - interval), "streak": 0, "queue": []}
        if (not isinstance(state, dict) or state.get("schema") != SCHEMA
                or not all(_finite(state.get(k)) for k in ("next_at", "last_now"))
                or type(state.get("streak")) is not int or not 0 <= state["streak"] <= INTERACTIVE_BURST
                or not isinstance(state.get("queue"), list) or len(state["queue"]) > MAX_WAITERS):
            raise USmartRateLimitStateError()
        for item in state["queue"]:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str)
                    or not re.fullmatch(r"[a-f0-9]{32}", item["id"])
                    or type(item.get("pid")) is not int or not 0 < item["pid"] <= 0x7fffffff
                    or item.get("priority") not in {"interactive", "background"}
                    or not _finite(item.get("expires_at"))):
                raise USmartRateLimitStateError()
        if len({item["id"] for item in state["queue"]}) != len(state["queue"]):
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

    def _remove(self, path, interval, ticket):
        try:
            with interprocess_file_lock(path.with_suffix(".lock"), timeout_seconds=.05, poll_seconds=.005):
                state = self._read(path, interval)
                remaining = [item for item in state["queue"] if item["id"] != ticket]
                if remaining != state["queue"]:
                    self._write(path, {**state, "queue": remaining})
        except (OSError, ValueError, TypeError, KeyError, USmartAPIError):
            pass  # A failed cleanup expires by its original deadline; never reset the bucket.

    def wait(self, *, host, channel, endpoint, timeout=10.0, priority="interactive", cancelled=None):
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("uSMART rate-limit timeout must be positive")
        if priority not in {"interactive", "background"}:
            raise ValueError("uSMART quote priority must be interactive or background")
        identity, interval = self.bucket(host, channel, endpoint)
        path = self._root() / (identity + ".json")
        started = self.monotonic()
        deadline, ticket = started + timeout, uuid4().hex
        registered = admitted = False
        enqueued_at = None
        depth = 0

        def timed_out():
            return USmartRateLimitTimeout(interval, wait_seconds=max(0.0, self.monotonic() - started),
                                          queue_depth=depth)

        try:
            while True:
                if cancelled is not None and cancelled():
                    raise USmartRateLimitCancelled()
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise timed_out()
                try:
                    with interprocess_file_lock(path.with_suffix(".lock"),
                                                timeout_seconds=min(.05, remaining), poll_seconds=.005):
                        remaining, now = deadline - self.monotonic(), self.clock()
                        if remaining <= 0:
                            raise timed_out()
                        state = self._read(path, interval)
                        if not _finite(now) or now < state["last_now"]:
                            raise USmartRateLimitStateError()
                        queue = [item for item in state["queue"]
                                 if item["expires_at"] > now]
                        # Only an eligible head can block admission. Inspect it,
                        # rather than opening every queued PID on every poll.
                        while queue:
                            head = _winner(queue, state["streak"])
                            if self.is_alive(head["pid"]):
                                break
                            queue = [item for item in queue if item["id"] != head["id"]]
                        depth = max(depth, len(queue))
                        if not registered:
                            if len(queue) >= MAX_WAITERS:
                                raise timed_out()
                            queue.append({"id": ticket, "pid": os.getpid(), "priority": priority,
                                          "expires_at": now + remaining})
                            registered = True
                            enqueued_at = now
                        elif not any(item["id"] == ticket for item in queue):
                            raise timed_out()
                        depth = max(depth, len(queue))
                        winner = _winner(queue, state["streak"])
                        due = state["next_at"] - now
                        if winner["id"] == ticket and due <= 0:
                            self._write(path, {"schema": SCHEMA, "next_at": now + interval, "last_now": now,
                                              "streak": min(INTERACTIVE_BURST, state["streak"] + 1)
                                              if priority == "interactive" else 0,
                                              "queue": [item for item in queue if item["id"] != ticket]})
                            admitted = True
                            return {"wait_seconds": max(0.0, self.monotonic() - started), "queue_depth": depth,
                                    "priority": priority, "endpoint": str(endpoint).rstrip("/").rsplit("/", 1)[-1],
                                    "enqueued_at": enqueued_at, "admitted_at": now}
                        if queue != state["queue"]:
                            self._write(path, {**state, "last_now": now, "queue": queue})
                        delay = max(.005, due) if winner["id"] == ticket else .05
                except InterprocessLockTimeout:
                    continue
                except USmartAPIError:
                    raise
                except (OSError, ValueError, TypeError, KeyError) as exc:
                    raise USmartRateLimitStateError() from exc
                self.sleep(min(delay, max(0.0, deadline - self.monotonic()), .05 if cancelled else delay))
        finally:
            if registered and not admitted:
                self._remove(path, interval, ticket)
