from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cl_app import create_app
from cl_app.services.alert_chart_images import (
    AlertChartImageService,
    SignedAlertChartStore,
)


PNG = b"\x89PNG\r\n\x1a\n" + b"test-png-body"


def test_signed_store_is_immutable_and_rejects_bad_or_expired_url(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 5, 13, 0, tzinfo=timezone.utc)
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
        ttl_seconds=60,
        clock=lambda: now,
    )
    url = store.publish(PNG, artifact_key="event:stable")
    parsed = urlsplit(url)
    artifact_id = Path(parsed.path).stem
    query = parse_qs(parsed.query)

    resolved = store.resolve(
        artifact_id,
        expires=query["expires"][0],
        signature=query["signature"][0],
    )
    assert resolved is not None and resolved.read_bytes() == PNG
    assert (
        store.resolve(
            artifact_id,
            expires=query["expires"][0],
            signature="0" * 64,
        )
        is None
    )

    expired = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
        ttl_seconds=60,
        clock=lambda: now + timedelta(seconds=61),
    )
    assert (
        expired.resolve(
            artifact_id,
            expires=query["expires"][0],
            signature=query["signature"][0],
        )
        is None
    )


def test_public_route_requires_signature_and_returns_png(tmp_path: Path) -> None:
    app = create_app(
        {
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "ALERT_CHART_PUBLIC_BASE_URL": "http://47.96.40.233:8890",
            "ALERT_CHART_ROOT": tmp_path,
            "TRADING_SCREENING_DINGTALK_WEBHOOK": "",
        }
    )
    store = app.extensions["alert_chart_image_store"]
    url = store.publish(PNG, artifact_key="route:test")
    parsed = urlsplit(url)

    response = app.test_client().get(parsed.path + "?" + parsed.query)
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert response.data == PNG
    assert response.headers["X-Robots-Tag"] == "noindex, noarchive"
    assert app.test_client().get(parsed.path).status_code == 404


def test_service_uses_strict_state_for_30m_5m_1m_and_publishes_once(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 5, 13, 0, tzinfo=timezone.utc)
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
        clock=lambda: now,
    )
    created = []

    class State:
        warmup_ready = False

        def __init__(self, code, exchange, **levels):
            created.append((code, exchange, levels))

        def refresh(self):
            self.warmup_ready = True

        def refresh_chart_levels(self):
            self.warmup_ready = True
            return True

        @staticmethod
        def chart_data(frequency):
            return f"chart:{frequency}"

    rendered = []

    def renderer(charts):
        rendered.append(tuple(charts))
        return PNG

    service = AlertChartImageService(
        store,
        exchange_provider=lambda market: f"exchange:{market.value}",
        state_factory=State,
        renderer=renderer,
    )
    images = service(
        {
            "charts": [
                {
                    "market": "a",
                    "code": "SZ.000001",
                    "name": "平安银行",
                    "artifact_key": "signal:1",
                },
                {
                    "market": "a",
                    "code": "SZ.000001",
                    "name": "平安银行",
                    "artifact_key": "signal:2",
                },
            ]
        }
    )

    assert len(created) == 1
    assert len(rendered) == 1
    assert len(images) == 1
    assert [value[1] for value in rendered[0]] == [
        "chart:30m",
        "chart:5m",
        "chart:1m",
    ]
    assert images[0]["url"].startswith(
        "http://47.96.40.233:8890/public/alert-chart/"
    )


def test_service_prefers_three_page_native_tradingview_images(
    tmp_path: Path,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )
    calls = []

    def browser_renderer(**context):
        calls.append(context)
        return tuple(
            {
                "frequency": frequency,
                "label": label,
                "lookback_days": days,
                "png": PNG,
            }
            for frequency, label, days in (
                ("30m", "30分钟", 180),
                ("5m", "5分钟", 45),
                ("1m", "1分钟", 10),
            )
        )

    service = AlertChartImageService(
        store,
        browser_renderer=browser_renderer,
        state_factory=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("strict fallback must not run")
        ),
    )
    images = service(
        {
            "charts": [
                {
                    "market": "a",
                    "code": "SH.513100",
                    "name": "纳指ETF",
                    "artifact_key": "signal:front-end",
                }
            ]
        }
    )

    assert calls == [{"market": "a", "code": "SH.513100", "name": "纳指ETF"}]
    assert len(images) == 3
    assert len({value["url"] for value in images}) == 3
    assert [value["alt"] for value in images] == [
        "纳指ETF SH.513100 30分钟结构图（含MACD_HTF，至少180天）",
        "纳指ETF SH.513100 5分钟结构图（含MACD_HTF，至少45天）",
        "纳指ETF SH.513100 1分钟结构图（含MACD_HTF，至少10天）",
    ]


def test_evidence_bound_alert_uses_verified_strict_snapshot_not_browser(
    tmp_path: Path,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )
    browser_calls = []
    resolutions = []

    class State:
        warmup_ready = False

        def __init__(self, *_args, **_kwargs):
            pass

        def refresh_chart_levels(self):
            self.warmup_ready = True
            return True

        def confirmed_point_occurrence(self, point_type, signal_time, *, frequency):
            resolutions.append((point_type, signal_time, frequency))
            return object()

        @staticmethod
        def chart_data(frequency):
            return f"strict:{frequency}"

    rendered = []
    service = AlertChartImageService(
        store,
        state_factory=State,
        exchange_provider=lambda market: market.value,
        browser_renderer=lambda **kwargs: browser_calls.append(kwargs),
        renderer=lambda charts: rendered.append(tuple(charts)) or PNG,
    )

    images = service(
        {
            "charts": [
                {
                    "market": "us",
                    "code": "TSLA.US",
                    "name": "Tesla",
                    "artifact_key": "strict-event",
                    "point_type": "3buy",
                    "signal_time": "2026-08-05T10:00:00-04:00",
                    "evidence_required": True,
                }
            ]
        }
    )

    assert browser_calls == []
    assert resolutions == [("3buy", "2026-08-05T10:00:00-04:00", "1m")]
    assert [value[1] for value in rendered[0]] == [
        "strict:30m",
        "strict:5m",
        "strict:1m",
    ]
    assert len(images) == 1
    assert "已核验1分钟3buy" in images[0]["alt"]


def test_evidence_bound_alert_rejects_chart_without_claimed_point(
    tmp_path: Path,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )

    class State:
        warmup_ready = True

        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def refresh_chart_levels():
            return True

        @staticmethod
        def confirmed_point_occurrence(*_args, **_kwargs):
            return None

    service = AlertChartImageService(
        store,
        state_factory=State,
        exchange_provider=lambda market: market.value,
    )

    try:
        service(
            {
                "charts": [
                    {
                        "market": "us",
                        "code": "TSLA.US",
                        "artifact_key": "missing-event",
                        "point_type": "3buy",
                        "signal_time": "2026-08-05T10:00:00-04:00",
                        "evidence_required": True,
                    }
                ]
            }
        )
    except RuntimeError as exc:
        assert "absent from chart evidence" in str(exc)
    else:
        raise AssertionError("unverified alert chart must be rejected")
