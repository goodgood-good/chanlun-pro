from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pandas as pd

from cl_app import create_app
from cl_app.services.alert_chart_images import (
    _CanonicalScreeningAlertState,
    AlertChartImageService,
    SignedAlertChartStore,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_CANONICAL_REQUEST_BARS,
)
from chanlun.decision_support.trading_system.strict_realtime_monitor import (
    StrictPhysicalMonitorState,
)
from chanlun.exchange.price_basis import QMT_STRUCTURE_DIVIDEND_TYPE
import chanlun.decision_support.trading_system.strict_realtime_monitor as monitor_module
from chanlun.notifications import DingTalkWebhookNotifier


PNG = b"\x89PNG\r\n\x1a\n" + b"test-png-body"


def test_a_share_evidence_state_uses_exact_screening_prefix() -> None:
    calls = []

    class Exchange:
        market = "a"
        kline_time_label = "start"

        @staticmethod
        def klines(code, frequency, *, args):
            calls.append((code, frequency, args))
            return "canonical-frame"

    observed_at = datetime.fromisoformat("2026-08-26T10:15:00+08:00")
    state = _CanonicalScreeningAlertState(
        "SH.600250",
        Exchange(),
        clock=lambda: observed_at,
    )

    assert state._fetch_klines("1m", None, as_of=observed_at) == "canonical-frame"
    assert calls == [
        (
            "SH.600250",
            "1m",
            {
                "req_counts": SCREENING_CANONICAL_REQUEST_BARS["1m"],
                "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
            },
        )
    ]


def test_a_share_evidence_state_normalizes_qmt_opening_minute(monkeypatch) -> None:
    observed_at = datetime.fromisoformat("2026-08-26T10:15:00+08:00")
    state = _CanonicalScreeningAlertState(
        "SH.600250",
        SimpleNamespace(market="a", kline_time_label="start"),
        clock=lambda: observed_at,
    )
    frame = pd.DataFrame(
        {
            "code": ["SH.600250", "SH.600250"],
            "date": pd.to_datetime(
                ["2026-08-26T09:30:00+08:00", "2026-08-26T09:31:00+08:00"]
            ),
            "open": [9.0, 9.1],
            "high": [9.2, 9.3],
            "low": [8.9, 9.0],
            "close": [9.1, 9.2],
            "volume": [100.0, 200.0],
        }
    )
    monkeypatch.setattr(
        StrictPhysicalMonitorState,
        "_closed_frame",
        lambda *_args, **_kwargs: frame,
    )

    normalized = state._closed_frame(object(), "1m", as_of=observed_at)

    assert normalized["date"].dt.strftime("%H:%M").tolist() == ["09:31"]
    assert normalized.iloc[0]["volume"] == 300.0


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
    assert store.existing_url(artifact_key="event:stable") == url
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
    assert expired.existing_url(artifact_key="event:stable") is None


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
                "studies": ("MACD", "MACD_HTF"),
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
        "纳指ETF SH.513100 30分钟结构图（含MACD与MACD_HTF，至少180天）",
        "纳指ETF SH.513100 5分钟结构图（含MACD与MACD_HTF，至少45天）",
        "纳指ETF SH.513100 1分钟结构图（含MACD与MACD_HTF，至少10天）",
    ]


def test_browser_image_missing_standard_macd_falls_back_to_strict_renderer(
    tmp_path: Path,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )

    class State:
        warmup_ready = False

        def __init__(self, *_args, **_kwargs):
            pass

        def refresh_chart_levels(self):
            self.warmup_ready = True
            return True

        @staticmethod
        def chart_data(frequency):
            return f"strict:{frequency}"

    def incomplete_browser_renderer(**_context):
        return tuple(
            {
                "frequency": frequency,
                "label": frequency,
                "lookback_days": 10,
                "studies": ("MACD_HTF",),
                "png": PNG,
            }
            for frequency in ("30m", "5m", "1m")
        )

    rendered = []
    service = AlertChartImageService(
        store,
        state_factory=State,
        exchange_provider=lambda market: market.value,
        browser_renderer=incomplete_browser_renderer,
        renderer=lambda charts: rendered.append(tuple(charts)) or PNG,
    )

    images = service(
        {
            "charts": [
                {
                    "market": "a",
                    "code": "SH.513100",
                    "name": "纳指ETF",
                    "artifact_key": "signal:missing-standard-macd",
                }
            ]
        }
    )

    assert len(rendered) == 1
    assert [value[1] for value in rendered[0]] == [
        "strict:30m",
        "strict:5m",
        "strict:1m",
    ]
    assert len(images) == 1
    assert "含MACD与MACD_HTF" in images[0]["alt"]


def test_browser_timeout_skips_expensive_optional_static_fallback(
    tmp_path: Path,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )
    state_calls = []

    def timed_out_browser(**_context):
        raise TimeoutError("capture budget exhausted")

    service = AlertChartImageService(
        store,
        browser_renderer=timed_out_browser,
        state_factory=lambda *args, **kwargs: state_calls.append((args, kwargs)),
    )

    images = service(
        {
            "charts": [
                {
                    "market": "a",
                    "code": "SZ.002905",
                    "name": "金逸影视",
                    "artifact_key": "signal:optional-timeout",
                }
            ]
        }
    )

    assert images == []
    assert state_calls == []


def test_required_auxiliary_chart_bypasses_browser_and_uses_static_renderer(
    tmp_path: Path,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )
    browser_calls = []
    rendered = []

    class State:
        warmup_ready = False

        def __init__(self, *_args, **_kwargs):
            pass

        def refresh_chart_levels(self):
            self.warmup_ready = True

        @staticmethod
        def chart_data(frequency):
            return f"strict:{frequency}"

    service = AlertChartImageService(
        store,
        browser_renderer=lambda **kwargs: browser_calls.append(kwargs),
        state_factory=State,
        exchange_provider=lambda market: market.value,
        renderer=lambda charts: rendered.append(tuple(charts)) or PNG,
    )

    images = service(
        {
            "require_chart": True,
            "charts": [
                {
                    "market": "a",
                    "code": "SZ.002905",
                    "name": "金逸影视",
                    "artifact_key": "signal:required-chart",
                }
            ],
        }
    )

    assert browser_calls == []
    assert len(rendered) == 1
    assert [value[1] for value in rendered[0]] == [
        "strict:30m",
        "strict:5m",
        "strict:1m",
    ]
    assert len(images) == 1


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

        def confirmed_point_occurrence(
            self,
            point_type,
            signal_time,
            *,
            frequency,
            evidence_id,
            recursive_level,
            anchor_time,
        ):
            resolutions.append(
                (
                    point_type,
                    signal_time,
                    frequency,
                    evidence_id,
                    recursive_level,
                    anchor_time,
                )
            )
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

    context = {
        "charts": [
            {
                "market": "us",
                "code": "TSLA.US",
                "name": "Tesla",
                "artifact_key": "strict-event",
                "observed_at": "2026-08-05T10:01:00-04:00",
                "point_type": "3buy",
                "signal_time": "2026-08-05T10:00:00-04:00",
                "evidence_id": "strict-point-id",
                "recursive_level": 1,
                "anchor_time": "2026-08-05T09:55:00-04:00",
                "evidence_required": True,
            }
        ]
    }
    images = service(context)
    retried_images = service(context)

    assert browser_calls == []
    assert resolutions == [
        (
            "3buy",
            "2026-08-05T10:00:00-04:00",
            "5m",
            "strict-point-id",
            1,
            "2026-08-05T09:55:00-04:00",
        )
    ]
    assert [value[1] for value in rendered[0]] == [
        "strict:30m",
        "strict:5m",
        "strict:1m",
    ]
    assert len(images) == 1
    assert retried_images == images
    assert len(rendered) == 1
    assert len(resolutions) == 1
    assert "含MACD与MACD_HTF" in images[0]["alt"]
    assert "已核验5m3buy" in images[0]["alt"]


def test_evidence_bound_alert_replays_producer_clock_and_warmup_prefix(
    tmp_path: Path,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )
    created = []

    class State:
        warmup_ready = False

        def __init__(self, code, exchange, **kwargs):
            created.append((code, exchange, kwargs))

        def refresh_chart_levels(self):
            self.warmup_ready = True
            return True

        @staticmethod
        def confirmed_point_occurrence(*_args, **_kwargs):
            return object()

        @staticmethod
        def chart_data(frequency):
            return f"strict:{frequency}"

    observed_at = "2026-08-24T22:58:42+08:00"
    starts = {
        "30m": "2025-08-24T22:27:30+08:00",
        "5m": "2026-04-26T22:27:30+08:00",
        "1m": "2026-07-25T22:27:30+08:00",
    }
    service = AlertChartImageService(
        store,
        state_factory=State,
        exchange_provider=lambda market: market.value,
        renderer=lambda _charts: PNG,
    )

    images = service(
        {
            "charts": [
                {
                    "market": "us",
                    "code": "QQQ.US",
                    "name": "纳指ETF",
                    "artifact_key": "qqq-event",
                    "observed_at": observed_at,
                    "warmup_start_by_frequency": starts,
                    "point_type": "3sell",
                    "signal_time": "2026-08-24T22:55:00+08:00",
                    "evidence_id": "strict-point-id",
                    "recursive_level": 0,
                    "anchor_time": "2026-08-21T23:50:00+08:00",
                    "evidence_required": True,
                }
            ]
        }
    )

    assert len(images) == 1
    assert len(created) == 1
    assert created[0][0:2] == ("QQQ.US", "us")
    kwargs = created[0][2]
    assert kwargs["warmup_start_by_frequency"] == starts
    assert kwargs["clock"]().isoformat() == observed_at


def test_qcom_evidence_bound_chart_survives_internal_evidence_id_rebuild(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SignedAlertChartStore(
        root=tmp_path,
        public_base_url="http://47.96.40.233:8890",
        secret=b"0123456789abcdef0123456789abcdef",
    )
    signal_time = datetime.fromisoformat("2026-08-13T22:38:00+08:00")
    anchor_time = datetime.fromisoformat("2026-08-13T22:30:00+08:00")
    rebuilt_point = SimpleNamespace(
        point_type="3buy",
        point_id="sha256:rebuilt-qcom-evidence",
        recursive_level=0,
        anchor_at=anchor_time,
        available_at=signal_time,
    )
    state = StrictPhysicalMonitorState(
        "QCOM.US",
        SimpleNamespace(market="us", kline_time_label="start"),
    )
    monkeypatch.setattr(state, "evidence", lambda _frequency: object())
    monkeypatch.setattr(
        monitor_module,
        "extract_current_confirmed_points",
        lambda *_args, **_kwargs: (rebuilt_point,),
    )

    def refresh_chart_levels():
        state.warmup_ready = True
        return True

    monkeypatch.setattr(state, "refresh_chart_levels", refresh_chart_levels)
    monkeypatch.setattr(state, "chart_data", lambda frequency: f"qcom:{frequency}")
    service = AlertChartImageService(
        store,
        state_factory=lambda *_args, **_kwargs: state,
        exchange_provider=lambda market: market.value,
        renderer=lambda _charts: PNG,
    )

    context = {
        "require_evidence_match": True,
        "charts": [
                {
                    "market": "us",
                    "code": "QCOM.US",
                    "name": "高通",
                    "artifact_key": "qcom-old-event",
                    "point_type": "3buy",
                    "signal_time": signal_time.isoformat(),
                    "evidence_id": "sha256:old-qcom-evidence",
                    "recursive_level": 0,
                    "anchor_time": anchor_time.isoformat(),
                    "evidence_required": True,
                }
            ],
    }
    images = service(context)

    assert len(images) == 1
    assert "QCOM.US" in images[0]["alt"]
    assert "已核验5m3buy" in images[0]["alt"]

    requests = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"errcode": 0}'

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: requests.append((request, timeout)) or _Response(),
    )
    notifier = DingTalkWebhookNotifier(
        "https://example.invalid/send",
        keyword="买卖通知",
        rich_content_provider=service,
    )
    assert notifier.send_rich(
        "买卖通知｜关注股｜QCOM.US｜1分钟三类买点",
        ["高通 QCOM.US · 三类买点"],
        context,
    ) is True
    assert len(requests) == 1
    payload = json.loads(requests[0][0].data.decode("utf-8"))
    assert payload["msgtype"] == "markdown"
    assert "QCOM.US" in payload["markdown"]["text"]
    assert "![高通 QCOM.US" in payload["markdown"]["text"]


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
                        "evidence_id": "missing-point-id",
                        "recursive_level": 0,
                        "anchor_time": "2026-08-05T09:55:00-04:00",
                        "evidence_required": True,
                    }
                ]
            }
        )
    except RuntimeError as exc:
        assert "absent from chart evidence" in str(exc)
    else:
        raise AssertionError("unverified alert chart must be rejected")
