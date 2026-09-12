"""Synchronous exchange interface over Longbridge's asynchronous quote SDK.

The SDK's blocking Python binding can retain the GIL while connecting. Moving
that binding to a worker does not protect web requests or their timeout clocks.
One private event loop runs the asynchronous binding instead; callers wait on
ordinary Python futures, and independent history pages can actually overlap.
"""

import asyncio
from concurrent.futures import TimeoutError as FuturesTimeoutError
import inspect
import threading
import time

from longbridge.openapi import AsyncQuoteContext


class QuoteContextBridge:
    """Lazy quote connection, bounded calls and explicit connection disposal."""

    def __init__(self, config, *, factory=None, request_timeout=30.0):
        self._config = config
        self._factory = factory or AsyncQuoteContext.create
        self._request_timeout = request_timeout
        self._context = None
        self._created_at = time.monotonic()
        self._connected = threading.Event()
        self._started = threading.Event()
        self._lock = threading.Lock()
        self._closed = False
        self._pending = set()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="lb-quote-async",
        )
        self._thread.start()
        if not self._started.wait(5.0):
            raise TimeoutError("Longbridge quote event loop did not start")

    @property
    def connection_wait_seconds(self):
        # Short quote/status deadlines must not repeatedly discard an otherwise
        # healthy first handshake. Callers still retain their own deadlines.
        if self._closed or self._connected.is_set():
            return 0.0
        return max(0.0, 20.0 - (time.monotonic() - self._created_at))

    @property
    def connecting(self):
        return self.connection_wait_seconds > 0

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            tasks = asyncio.all_tasks(self._loop)
            for task in tasks:
                task.cancel()
            if tasks:
                self._loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
            self._context = None
            self._loop.close()

    async def _invoke(self, name, args, kwargs):
        if self._context is None:
            self._context = self._factory(self._config)
        result = getattr(self._context, name)(*args, **kwargs)
        if inspect.isawaitable(result):
            result = await result
            self._connected.set()
        return result

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def call(*args, **kwargs):
            if threading.current_thread() is self._thread:
                raise RuntimeError("cannot block the quote event loop from its callback")
            with self._lock:
                if self._closed:
                    raise RuntimeError("Longbridge quote connection is closed")
                future = asyncio.run_coroutine_threadsafe(
                    self._invoke(name, args, kwargs), self._loop,
                )
                self._pending.add(future)
            try:
                return future.result(timeout=self._request_timeout)
            except (FuturesTimeoutError, TimeoutError):
                future.cancel()
                raise
            finally:
                with self._lock:
                    self._pending.discard(future)

        return call

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pending = tuple(self._pending)
        for future in pending:
            future.cancel()
        self._loop.call_soon_threadsafe(self._loop.stop)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=2.0)
