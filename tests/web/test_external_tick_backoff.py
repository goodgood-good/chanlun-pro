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
