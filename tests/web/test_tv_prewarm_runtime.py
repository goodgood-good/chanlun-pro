from cl_app.blueprints import tv


def test_parallel_prewarm_never_waits_for_executor_shutdown(monkeypatch):
    futures = []
    shutdown_calls = []

    class _Future:
        def __init__(self):
            self.cancelled = False

        def result(self, timeout=None):
            raise TimeoutError(f"hung after {timeout}")

        def cancel(self):
            self.cancelled = True

    class _Executor:
        def __init__(self, **_kwargs):
            pass

        def submit(self, _callback, _interval):
            future = _Future()
            futures.append(future)
            return future

        def shutdown(self, **kwargs):
            shutdown_calls.append(kwargs)

    monkeypatch.setattr(tv, "DaemonExecutor", _Executor, raising=False)
    monkeypatch.setattr(tv, "_PREWARM_PARALLEL_TIMEOUT_SECONDS", 0.01, raising=False)

    tv._run_parallel_prewarm(["1", "5", "15"], lambda _interval: None, lambda: False)

    assert len(futures) == 3
    assert all(future.cancelled for future in futures)
    assert shutdown_calls == [{"wait": False, "cancel_futures": True}]
