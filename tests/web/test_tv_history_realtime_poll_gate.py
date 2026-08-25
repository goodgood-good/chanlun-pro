from __future__ import annotations

import datetime

import pytest

from cl_app import create_app
from cl_app.blueprints import tv as subject


CN = datetime.timezone(datetime.timedelta(hours=8))


def _observed(value: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(value).astimezone(CN)


def _should_suppress(
    observed_at: datetime.datetime,
    *,
    market: str = "a",
    first_data_request: str = "false",
    countback: object = "2",
    requested_to_offset_seconds: int = 60,
    force_refresh: bool = False,
    review_locked: bool = False,
) -> bool:
    return subject._should_suppress_realtime_history_poll(
        market=market,
        first_data_request=first_data_request,
        countback=countback,
        requested_to=int(observed_at.timestamp()) + requested_to_offset_seconds,
        observed_at=observed_at,
        force_refresh=force_refresh,
        review_locked=review_locked,
    )


@pytest.mark.parametrize(
    "observed_at",
    (
        _observed("2026-08-26T00:05:00+08:00"),
        _observed("2026-08-26T11:41:00+08:00"),
        _observed("2026-08-26T15:11:00+08:00"),
        _observed("2026-08-29T10:00:00+08:00"),
    ),
)
def test_a_share_realtime_poll_is_suppressed_outside_active_windows(
    observed_at: datetime.datetime,
) -> None:
    assert _should_suppress(observed_at) is True


@pytest.mark.parametrize(
    "observed_at",
    (
        _observed("2026-08-26T10:00:00+08:00"),
        _observed("2026-08-26T11:40:00+08:00"),
        _observed("2026-08-26T15:10:00+08:00"),
    ),
)
def test_a_share_realtime_poll_including_close_grace_is_preserved(
    observed_at: datetime.datetime,
) -> None:
    assert _should_suppress(observed_at) is False


@pytest.mark.parametrize(
    "overrides",
    (
        {"market": "currency"},
        {"first_data_request": "true"},
        {"countback": "329"},
        {"countback": "invalid"},
        {"requested_to_offset_seconds": -3600},
        {"force_refresh": True},
        {"review_locked": True},
    ),
)
def test_non_polling_or_uncertain_requests_fail_open(overrides: dict[str, object]) -> None:
    assert _should_suppress(
        _observed("2026-08-26T00:05:00+08:00"),
        **overrides,
    ) is False


def test_route_returns_no_data_before_chart_or_exchange_work(monkeypatch) -> None:
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    monkeypatch.setattr(
        subject,
        "_should_suppress_realtime_history_poll",
        lambda **_kwargs: True,
        raising=False,
    )
    monkeypatch.setattr(
        subject,
        "query_cl_chart_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("suppressed polling must not load chart configuration")
        ),
    )

    try:
        response = app.test_client().get(
            "/tv/history?symbol=a:SZ.002083&resolution=5"
            "&firstDataRequest=false&countback=2&from=1787670000&to=1787673900"
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 200
    assert response.get_json() == {"s": "no_data"}
