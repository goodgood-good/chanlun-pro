from cl_app.services.external_tick_backoff import ExternalMarketTickBackoff


class _Clock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_external_market_tick_backoff_is_per_market_and_resets_on_success() -> None:
    clock = _Clock()
    backoff = ExternalMarketTickBackoff(clock=clock)

    first = backoff.acquire("currency_spot")
    duplicate = backoff.acquire("currency_spot")
    other_market = backoff.acquire("us")

    assert first.allowed is True
    assert duplicate.allowed is False
    assert duplicate.reason_code == "PROVIDER_PROBE_IN_FLIGHT"
    assert other_market.allowed is True

    failed = backoff.record_failure("currency_spot")
    assert failed.retry_after_seconds == 15
    assert failed.failure_count == 1

    clock.advance(14)
    assert backoff.acquire("currency_spot").retry_after_seconds == 1
    clock.advance(1)
    retry = backoff.acquire("currency_spot")
    assert retry.allowed is True
    assert retry.failure_count == 1

    backoff.record_success("currency_spot")
    recovered = backoff.acquire("currency_spot")
    assert recovered.allowed is True
    assert recovered.failure_count == 0


def test_external_market_tick_backoff_uses_a_bounded_retry_ladder() -> None:
    clock = _Clock()
    backoff = ExternalMarketTickBackoff(clock=clock)
    observed = []

    for expected in (15, 30, 60, 120, 300, 300):
        assert backoff.acquire("currency_spot").allowed is True
        failure = backoff.record_failure("currency_spot")
        observed.append(failure.retry_after_seconds)
        clock.advance(expected)

    assert observed == [15, 30, 60, 120, 300, 300]


def test_external_market_tick_backoff_shares_one_completed_probe_result() -> None:
    clock = _Clock()
    backoff = ExternalMarketTickBackoff(clock=clock)
    permit = backoff.acquire("currency_spot")
    payload = {
        "ok": True,
        "market_state": "open",
        "now_trading": True,
        "ticks": [{"code": "BTC/USDT", "price": 65432.1, "rate": 1.2}],
        "error": None,
    }

    backoff.record_success(
        "currency_spot",
        requested_codes=("BTC/USDT",),
        response_payload=payload,
    )
    shared = backoff.wait_for_success(
        "currency_spot",
        ("BTC/USDT",),
        not_before=permit.probe_started_at,
        timeout_seconds=0,
        max_age_seconds=2,
    )

    assert shared is not None
    assert shared.payload == payload
    assert shared.payload is not payload
    assert shared.age_seconds == 0


def test_external_market_tick_shared_result_is_short_lived_and_code_scoped() -> None:
    clock = _Clock()
    backoff = ExternalMarketTickBackoff(clock=clock)
    backoff.acquire("currency_spot")
    backoff.record_success(
        "currency_spot",
        requested_codes=("BTC/USDT",),
        response_payload={"ticks": [{"code": "BTC/USDT", "price": 65432.1}]},
    )

    assert (
        backoff.recent_success(
            "currency_spot",
            ("ETH/USDT",),
            max_age_seconds=2,
        )
        is None
    )


def test_stale_probe_completion_cannot_clear_a_newer_inflight_probe() -> None:
    clock = _Clock()
    backoff = ExternalMarketTickBackoff(clock=clock, stale_probe_seconds=30)
    first = backoff.acquire("currency_spot")
    clock.advance(31)
    second = backoff.acquire("currency_spot")

    assert first.probe_id != second.probe_id
    assert second.allowed is True

    published = backoff.record_success(
        "currency_spot",
        probe_id=first.probe_id,
        requested_codes=("BTC/USDT",),
        response_payload={"ticks": [{"code": "BTC/USDT", "price": 1}]},
    )
    still_inflight = backoff.acquire("currency_spot")

    assert published is False
    assert still_inflight.allowed is False
    assert still_inflight.reason_code == "PROVIDER_PROBE_IN_FLIGHT"
    assert still_inflight.probe_id == second.probe_id

    assert backoff.record_success(
        "currency_spot",
        probe_id=second.probe_id,
        requested_codes=("BTC/USDT",),
        response_payload={"ticks": [{"code": "BTC/USDT", "price": 2}]},
    ) is True
    assert backoff.acquire("currency_spot").allowed is True
    clock.advance(2.01)
    assert (
        backoff.recent_success(
            "currency_spot",
            ("BTC/USDT",),
            max_age_seconds=2,
        )
        is None
    )
