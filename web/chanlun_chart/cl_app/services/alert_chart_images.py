"""为钉钉复核通知提供带签名的公开图表图片。

公开路由只暴露带有 HMAC 签名和过期时间的内容寻址 PNG 文件，不会暴露图表
目录、登录会话、行情接口、账户状态或下单通道。
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit

from chanlun.chart_image import render_multi_timeframe_png
from chanlun import fun
from chanlun.decision_support.trading_system.strict_realtime_monitor import (
    StrictPhysicalMonitorState,
)
from chanlun.decision_support.trading_system.qmt_same_base_stream import (
    normalize_qmt_opening_events_for_completed_minutes,
)
from chanlun.decision_support.trading_system.screening_warmup import (
    SCREENING_CANONICAL_REQUEST_BARS,
)
from chanlun.exchange import get_exchange
from chanlun.exchange.price_basis import QMT_STRUCTURE_DIVIDEND_TYPE
from chanlun.market import Market


_PNG_HEADER = b"\x89PNG\r\n\x1a\n"
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}$")


class SignedAlertChartStore:
    """持久化不可变 PNG，并签发有期限且不可猜测的公开地址。"""

    def __init__(
        self,
        *,
        root: Path,
        public_base_url: str,
        secret: bytes | str,
        ttl_seconds: int = 30 * 24 * 60 * 60,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        base = str(public_base_url or "").strip().rstrip("/")
        parsed = urlsplit(base)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("public_base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("public_base_url must not contain credentials or query")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int):
            raise TypeError("ttl_seconds must be an integer")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        secret_bytes = secret if isinstance(secret, bytes) else str(secret).encode()
        if len(secret_bytes) < 16:
            raise ValueError("chart signing secret must contain at least 16 bytes")
        self.root = Path(root)
        self.public_base_url = base
        self._secret = secret_bytes
        self.ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("chart store clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("chart store clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _signature(self, artifact_id: str, expires: int) -> str:
        message = f"{artifact_id}:{expires}".encode("ascii")
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def _paths(self, artifact_id: str) -> tuple[Path, Path]:
        return (
            self.root / f"{artifact_id}.png",
            self.root / f"{artifact_id}.json",
        )

    @staticmethod
    def _artifact_id(artifact_key: str) -> str:
        key = str(artifact_key or "").strip()
        if not key:
            raise ValueError("artifact_key is required")
        return hashlib.sha256(
            f"chanlun-alert-chart\n{key}".encode("utf-8")
        ).hexdigest()

    def _public_url(self, artifact_id: str, expires: int) -> str:
        signature = self._signature(artifact_id, expires)
        return (
            f"{self.public_base_url}/public/alert-chart/{artifact_id}.png"
            f"?expires={expires}&signature={signature}"
        )

    def existing_url(self, *, artifact_key: str) -> str | None:
        """Return a still-valid immutable artifact without rebuilding its chart."""

        artifact_id = self._artifact_id(artifact_key)
        now = self._now()
        with self._lock:
            image_path, metadata_path = self._paths(artifact_id)
            if not image_path.is_file() or not metadata_path.is_file():
                return None
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                expires = int(metadata.get("expires", 0))
                with image_path.open("rb") as stream:
                    header = stream.read(len(_PNG_HEADER))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return None
            if (
                metadata.get("schema") != "chanlun-alert-chart-artifact"
                or metadata.get("artifact_id") != artifact_id
                or expires <= int(now.timestamp())
                or header != _PNG_HEADER
            ):
                return None
        return self._public_url(artifact_id, expires)

    def publish(self, png: bytes, *, artifact_key: str) -> str:
        if not isinstance(png, bytes) or not png.startswith(_PNG_HEADER):
            raise ValueError("published chart must be PNG bytes")
        artifact_id = self._artifact_id(artifact_key)
        now = self._now()
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            image_path, metadata_path = self._paths(artifact_id)
            if not image_path.is_file():
                temporary = image_path.with_suffix(
                    f".png.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                try:
                    temporary.write_bytes(png)
                    os.replace(temporary, image_path)
                finally:
                    temporary.unlink(missing_ok=True)

            expires = int((now + timedelta(seconds=self.ttl_seconds)).timestamp())
            if metadata_path.is_file():
                try:
                    existing = json.loads(metadata_path.read_text(encoding="utf-8"))
                    existing_expires = int(existing.get("expires", 0))
                    if existing_expires > int(now.timestamp()):
                        expires = existing_expires
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    pass
            metadata = {
                "schema": "chanlun-alert-chart-artifact",
                "artifact_id": artifact_id,
                "expires": expires,
            }
            temporary_metadata = metadata_path.with_suffix(
                f".json.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            try:
                temporary_metadata.write_text(
                    json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                os.replace(temporary_metadata, metadata_path)
            finally:
                temporary_metadata.unlink(missing_ok=True)

        return self._public_url(artifact_id, expires)

    def resolve(
        self,
        artifact_id: str,
        *,
        expires: object,
        signature: object,
    ) -> Path | None:
        if not _ARTIFACT_RE.fullmatch(str(artifact_id or "")):
            return None
        try:
            expires_value = int(str(expires))
        except (TypeError, ValueError):
            return None
        if expires_value < int(self._now().timestamp()):
            return None
        expected = self._signature(artifact_id, expires_value)
        supplied = str(signature or "")
        if not hmac.compare_digest(supplied, expected):
            return None
        image_path, metadata_path = self._paths(artifact_id)
        if not image_path.is_file() or not metadata_path.is_file():
            return None
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if (
            metadata.get("schema") != "chanlun-alert-chart-artifact"
            or metadata.get("artifact_id") != artifact_id
            or metadata.get("expires") != expires_value
        ):
            return None
        return image_path


class _CanonicalScreeningAlertState(StrictPhysicalMonitorState):
    """Replay A-share alert charts from the screening producer's exact prefix."""

    def _fetch_klines(self, frequency, _last, *, as_of):
        # ``StrictPhysicalMonitorState`` normally uses monitor-specific day
        # windows (30/120/365 days).  A-share screening instead uses the
        # canonical QMT request contract (12k/12k/4k bars); mixing the two can
        # make a real emitted point disappear when the notification chart is
        # rebuilt.  Evidence alerts always perform one bounded, full-prefix
        # replay, so incremental tail fetches are intentionally unnecessary.
        return self.ex.klines(
            self.code,
            frequency,
            args={
                "req_counts": SCREENING_CANONICAL_REQUEST_BARS[frequency],
                "dividend_type": QMT_STRUCTURE_DIVIDEND_TYPE,
            },
        )

    def _closed_frame(self, raw, frequency, *, as_of=None):
        frame = super()._closed_frame(raw, frequency, as_of=as_of)
        if frequency == "1m":
            # The screening gateway merges QMT's 09:30 auction row into the
            # first completed minute before structure analysis.  Reapply that
            # same-base contract before validating the chart occurrence.
            frame = normalize_qmt_opening_events_for_completed_minutes(frame)
        return frame


class AlertChartImageService:
    """基于严格实时核心生成不可变的 30m、5m、1m 复核图。"""

    def __init__(
        self,
        store: SignedAlertChartStore,
        *,
        exchange_provider: Callable[[Market], object] = get_exchange,
        state_factory: Callable[..., object] = StrictPhysicalMonitorState,
        renderer: Callable[..., bytes] = render_multi_timeframe_png,
        browser_renderer: Callable[..., Sequence[Mapping[str, object]]] | None = None,
        max_cached_states: int = 32,
    ) -> None:
        if max_cached_states <= 0:
            raise ValueError("max_cached_states must be positive")
        self.store = store
        self._exchange_provider = exchange_provider
        self._state_factory = state_factory
        self._renderer = renderer
        self._browser_renderer = browser_renderer
        self._max_cached_states = max_cached_states
        self._states: OrderedDict[tuple[str, str], object] = OrderedDict()
        self._lock = threading.RLock()

    def _state(self, market: str, code: str) -> object:
        identity = (market, code)
        state = self._states.get(identity)
        if state is None:
            exchange = self._exchange_provider(Market(market))
            state = self._state_factory(
                code,
                exchange,
                op_level="5m",
                mid_level="1m",
                big_level="30m",
            )
            self._states[identity] = state
            while len(self._states) > self._max_cached_states:
                self._states.popitem(last=False)
        else:
            self._states.move_to_end(identity)
        return state

    def __call__(self, context: Mapping[str, object]) -> Sequence[Mapping[str, str]]:
        raw_items = context.get("charts")
        items = raw_items if isinstance(raw_items, (list, tuple)) else ()
        output: list[dict[str, str]] = []
        seen_symbols: set[tuple[str, str]] = set()
        with self._lock:
            for raw in items:
                if not isinstance(raw, Mapping):
                    continue
                market = str(raw.get("market") or "").strip().lower()
                code = str(raw.get("code") or "").strip()
                name = str(raw.get("name") or code).strip() or code
                artifact_key = str(raw.get("artifact_key") or "").strip()
                if not market or not code or not artifact_key:
                    continue
                identity = (market, code)
                if identity in seen_symbols:
                    continue
                seen_symbols.add(identity)
                evidence_required = raw.get("evidence_required") is True
                if evidence_required:
                    point_type = str(raw.get("point_type") or "").strip()
                    signal_time = str(raw.get("signal_time") or "").strip()
                    evidence_id = str(raw.get("evidence_id") or "").strip()
                    anchor_time = str(raw.get("anchor_time") or "").strip()
                    recursive_level = raw.get("recursive_level")
                    if (
                        not point_type
                        or not signal_time
                        or not evidence_id
                        or not anchor_time
                        or type(recursive_level) is not int
                        or recursive_level < 0
                    ):
                        raise RuntimeError(
                            f"alert evidence identity incomplete for {market}:{code}"
                        )
                    evidence_artifact_key = (
                        f"{artifact_key}:strict-evidence-bound"
                    )
                    alt = (
                        f"{name} {code} 30分钟/5分钟/1分钟结构图"
                        f"（已核验{raw.get('frequency') or '5m'}"
                        f"{point_type}：{signal_time}）"
                    )
                    existing_url = self.store.existing_url(
                        artifact_key=evidence_artifact_key,
                    )
                    if existing_url is not None:
                        output.append({"url": existing_url, "alt": alt})
                        continue
                    raw_starts = raw.get("warmup_start_by_frequency")
                    if raw_starts is not None and not isinstance(
                        raw_starts,
                        Mapping,
                    ):
                        raise RuntimeError(
                            f"alert warmup identity invalid for {market}:{code}"
                        )
                    raw_observed_at = str(raw.get("observed_at") or "").strip()
                    if raw_starts or raw_observed_at:
                        try:
                            observed_at = datetime.fromisoformat(raw_observed_at)
                        except ValueError as exc:
                            raise RuntimeError(
                                f"alert observation time invalid for {market}:{code}"
                            ) from exc
                        if (
                            observed_at.tzinfo is None
                            or observed_at.utcoffset() is None
                        ):
                            raise RuntimeError(
                                f"alert observation time invalid for {market}:{code}"
                            )
                        exchange = self._exchange_provider(Market(market))
                        state_options = {
                            "op_level": "5m",
                            "mid_level": "1m",
                            "big_level": "30m",
                            "clock": lambda fixed=observed_at: fixed,
                        }
                        if raw_starts:
                            state_options["warmup_start_by_frequency"] = dict(
                                raw_starts
                            )
                        state_factory = (
                            _CanonicalScreeningAlertState
                            if market == Market.A.value
                            and self._state_factory is StrictPhysicalMonitorState
                            else self._state_factory
                        )
                        state = state_factory(code, exchange, **state_options)
                    else:
                        # Legacy pending records did not persist their producer prefix.
                        state = self._state(market, code)
                    refresh_chart_levels = getattr(
                        state,
                        "refresh_chart_levels",
                        None,
                    )
                    if callable(refresh_chart_levels):
                        refresh_chart_levels()
                    else:
                        state.refresh()
                    if getattr(state, "warmup_ready", False) is not True:
                        raise RuntimeError(
                            f"chart warmup incomplete for {market}:{code}"
                        )
                    resolve_occurrence = getattr(
                        state,
                        "confirmed_point_occurrence",
                        None,
                    )
                    if not callable(resolve_occurrence) or resolve_occurrence(
                        point_type,
                        signal_time,
                        frequency=str(raw.get("frequency") or "5m"),
                        evidence_id=evidence_id,
                        recursive_level=recursive_level,
                        anchor_time=anchor_time,
                    ) is None:
                        raise RuntimeError(
                            f"alert point absent from chart evidence for {market}:{code}"
                        )
                    charts = []
                    for frequency, label in (
                        ("30m", "30分钟"),
                        ("5m", "5分钟"),
                        ("1m", "1分钟"),
                    ):
                        chart_data = state.chart_data(frequency)
                        if chart_data is None:
                            raise RuntimeError(
                                f"{frequency} chart data unavailable"
                            )
                        charts.append((f"{name} {code} · {label}", chart_data))
                    png = self._renderer(charts)
                    url = self.store.publish(
                        png,
                        artifact_key=evidence_artifact_key,
                    )
                    output.append(
                        {
                            "url": url,
                            "alt": alt,
                        }
                    )
                    continue
                if self._browser_renderer is not None:
                    try:
                        captures = tuple(
                            self._browser_renderer(
                                market=market,
                                code=code,
                                name=name,
                            )
                        )
                        expected = ("30m", "5m", "1m")
                        if tuple(
                            str(value.get("frequency") or "")
                            for value in captures
                            if isinstance(value, Mapping)
                        ) != expected:
                            raise RuntimeError(
                                "TradingView capture timeframe set is incomplete"
                            )
                        validated: list[tuple[str, str, int, bytes]] = []
                        for value in captures:
                            if not isinstance(value, Mapping):
                                raise RuntimeError(
                                    "TradingView capture item is invalid"
                                )
                            png = value.get("png")
                            if not isinstance(png, bytes) or not png.startswith(
                                _PNG_HEADER
                            ):
                                raise RuntimeError(
                                    "TradingView capture image is invalid"
                                )
                            validated.append(
                                (
                                    str(value["frequency"]),
                                    str(value.get("label") or value["frequency"]),
                                    int(value.get("lookback_days") or 0),
                                    png,
                                )
                            )
                    except Exception as exc:
        # 下方确定性的严格渲染器仍是完整回退方案；绝不能让一次浏览器失败
        # 导致重复通知或漏报提醒。
                        fun.get_logger().warning(
                            "[notify] TradingView chart capture failed: %s",
                            type(exc).__name__,
                        )
                    else:
                        for frequency, label, lookback_days, png in validated:
                            url = self.store.publish(
                                png,
                                artifact_key=(
                                    f"{artifact_key}:tradingview-client:"
                                    f"{frequency}:strict"
                                ),
                            )
                            output.append(
                                {
                                    "url": url,
                                    "alt": (
                                        f"{name} {code} {label}结构图"
                                        f"（含MACD_HTF，至少{lookback_days}天）"
                                    ),
                                }
                            )
                        continue
                state = self._state(market, code)
                refresh_chart_levels = getattr(state, "refresh_chart_levels", None)
                if callable(refresh_chart_levels):
                    refresh_chart_levels()
                else:
                    state.refresh()
                if getattr(state, "warmup_ready", False) is not True:
                    raise RuntimeError(f"chart warmup incomplete for {market}:{code}")
                charts = []
                for frequency, label in (("30m", "30分钟"), ("5m", "5分钟"), ("1m", "1分钟")):
                    chart_data = state.chart_data(frequency)
                    if chart_data is None:
                        raise RuntimeError(f"{frequency} chart data unavailable")
                    charts.append((f"{name} {code} · {label}", chart_data))
                png = self._renderer(charts)
                url = self.store.publish(
                    png,
                    artifact_key=f"{artifact_key}:strict-static-fallback",
                )
                output.append(
                    {
                        "url": url,
                        "alt": f"{name} {code} 30分钟/5分钟/1分钟结构图",
                    }
                )
        return output


__all__ = ["AlertChartImageService", "SignedAlertChartStore"]
