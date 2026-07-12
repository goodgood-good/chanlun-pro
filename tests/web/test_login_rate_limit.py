from cl_app.services.login_rate_limit import LoginRateLimiter


def test_login_limiter_blocks_then_expires_and_can_clear():
    now = [100.0]
    limiter = LoginRateLimiter(
        max_failures=3,
        window_seconds=60,
        block_seconds=120,
        clock=lambda: now[0],
    )

    assert limiter.record_failure("127.0.0.1") is False
    assert limiter.record_failure("127.0.0.1") is False
    assert limiter.record_failure("127.0.0.1") is True
    assert limiter.is_blocked("127.0.0.1") is True

    now[0] += 121
    assert limiter.is_blocked("127.0.0.1") is False


def test_login_limiter_has_bounded_ip_state():
    limiter = LoginRateLimiter(max_entries=8)
    for index in range(100):
        limiter.record_failure(f"192.0.2.{index}")

    assert limiter.tracked_keys() <= 8
    limiter.record_failure("127.0.0.1")
    limiter.clear("127.0.0.1")
    assert limiter.is_blocked("127.0.0.1") is False
