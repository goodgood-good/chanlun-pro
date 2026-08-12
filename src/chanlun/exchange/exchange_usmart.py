"""uSMART 盈立证券基础行情 Open API 适配器。

官方接口同时覆盖 A 股、港股和美股。本模块只接入行情能力；交易、持仓与账户
能力故意不在此适配器中开放。
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import threading
import time
from typing import Any, Dict, List, Mapping, Union

import pandas as pd
import pytz
import requests

try:
    from Crypto.Cipher import PKCS1_v1_5
    from Crypto.Hash import MD5
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pkcs1_15
except ImportError as _crypto_import_error:
    raise ImportError(
        "ExchangeUSmart requires extras: pip install 'chanlun-pro[usmart]' "
        "(or `poetry install --extras usmart`)"
    ) from _crypto_import_error

from chanlun import config
from chanlun.exchange.exchange import Exchange, Tick
from chanlun.exchange.kline_precision import (
    normalize_kline_precision,
    resolve_structure_price_quantum,
)
from chanlun.exchange.price_basis import (
    attach_price_basis_metadata,
    build_provider_price_basis_metadata,
)
from chanlun.tools.log_util import LogUtil


_QUOTE_API_PREFIX = "/quotes-openservice/api/v1"
_MARKET_API_CODES = {
    "a": ("sh", "sz"),
    "hk": ("hk",),
    "us": ("us",),
}
_MARKET_TIMEZONES = {
    "a": "Asia/Shanghai",
    "hk": "Asia/Shanghai",
    "us": "US/Eastern",
}
_MARKET_CLOSE_TIMES = {
    "a": (15, 0),
    "hk": (16, 0),
    "us": (16, 0),
}
_FREQUENCY_TYPES = {
    "1m": 1,
    "5m": 2,
    "10m": 3,
    "15m": 4,
    "30m": 5,
    "60m": 6,
    "d": 7,
    "w": 8,
    "m": 9,
    "q": 10,
    "6m": 11,
    "y": 12,
}
_FREQUENCY_LABELS = {
    "1m": "1m",
    "5m": "5m",
    "10m": "10m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
    "d": "Day",
    "w": "Week",
    "m": "Month",
    "q": "Quarter",
    "6m": "6 Months",
    "y": "Year",
}
_CALENDAR_FREQUENCIES = frozenset({"d", "w", "m", "q", "6m", "y"})
_US_HISTORY_SOURCES = frozenset({"usmart", "longbridge", "alpaca"})
_KLINE_COLUMNS = [
    "date",
    "frequency",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
]


class USmartAPIError(RuntimeError):
    """uSMART HTTP 或业务接口错误，不包含请求凭证及请求体。"""

    def __init__(self, message: str, *, code: Any = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _pem_bytes(raw_key: str, label: str) -> bytes:
    """兼容官网给出的单行 base64 DER 和完整 PEM 两种格式。"""
    value = (raw_key or "").strip().replace("\\n", "\n")
    if not value:
        raise ValueError(f"empty {label.lower()}")
    if "-----BEGIN " in value:
        return value.encode("ascii")
    compact = "".join(value.split())
    lines = "\n".join(compact[i : i + 64] for i in range(0, len(compact), 64))
    return f"-----BEGIN {label}-----\n{lines}\n-----END {label}-----".encode("ascii")


class USmartSigner:
    """实现官方要求的 RSA/MD5 签名与 PKCS#1 v1.5 登录字段加密。"""

    def __init__(self, private_key: str, public_key: str = ""):
        self._private_key = RSA.import_key(_pem_bytes(private_key, "PRIVATE KEY"))
        self._public_key = (
            RSA.import_key(_pem_bytes(public_key, "PUBLIC KEY"))
            if public_key
            else None
        )

    def sign(self, content: str, *, urlsafe: bool) -> str:
        digest = MD5.new(content.encode("utf-8"))
        signature = pkcs1_15.new(self._private_key).sign(digest)
        encoder = base64.urlsafe_b64encode if urlsafe else base64.b64encode
        return encoder(signature).decode("ascii")

    def encrypt_login_field(self, value: str) -> str:
        if self._public_key is None:
            raise RuntimeError("USMART_PUBLIC_KEY is required for automatic login")
        cipher = PKCS1_v1_5.new(self._public_key)
        encrypted = cipher.encrypt(str(value).encode("utf-8"))
        return base64.urlsafe_b64encode(encrypted).decode("ascii")


def _positive_number(value: Any, default: float, *, integer: bool = False):
    try:
        parsed = int(value) if integer else float(value)
        if parsed <= 0:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        return int(default) if integer else float(default)


class USmartClient:
    """带签名、登录 token 和超时控制的 uSMART HTTP 客户端。"""

    def __init__(
        self,
        *,
        channel: str | None = None,
        private_key: str | None = None,
        public_key: str | None = None,
        token: str | None = None,
        phone: str | None = None,
        login_password: str | None = None,
        area_code: str | None = None,
        language: str | None = None,
        quote_host: str | None = None,
        trade_host: str | None = None,
        timeout: float | None = None,
        session: requests.Session | None = None,
    ):
        self.channel = str(
            channel if channel is not None else getattr(config, "USMART_CHANNEL", "")
        ).strip()
        self.private_key = (
            private_key
            if private_key is not None
            else getattr(config, "USMART_PRIVATE_KEY", "")
        )
        self.public_key = (
            public_key
            if public_key is not None
            else getattr(config, "USMART_PUBLIC_KEY", "")
        )
        self.token = str(
            token if token is not None else getattr(config, "USMART_TOKEN", "")
        ).strip()
        self.phone = str(
            phone if phone is not None else getattr(config, "USMART_PHONE", "")
        ).strip()
        self.login_password = (
            login_password
            if login_password is not None
            else getattr(config, "USMART_LOGIN_PASSWORD", "")
        )
        self.area_code = str(
            area_code
            if area_code is not None
            else getattr(config, "USMART_AREA_CODE", "86")
        ).strip()
        self.language = str(
            language
            if language is not None
            else getattr(config, "USMART_LANGUAGE", "1")
        ).strip()
        self.quote_host = str(
            quote_host
            if quote_host is not None
            else getattr(
                config, "USMART_QUOTE_HOST", "https://open-hz.usmartsg.com:8443"
            )
        ).rstrip("/")
        self.trade_host = str(
            trade_host
            if trade_host is not None
            else getattr(config, "USMART_TRADE_HOST", "https://open-jy.usmartsg.com")
        ).rstrip("/")
        timeout_value = (
            timeout if timeout is not None else getattr(config, "USMART_TIMEOUT", 8)
        )
        self.timeout = _positive_number(timeout_value, 8)
        self.session = session or requests.Session()

        self._signer: USmartSigner | None = None
        self._signer_lock = threading.Lock()
        self._login_lock = threading.Lock()
        self._request_id_lock = threading.Lock()
        self._last_request_ids: Dict[int, int] = {}

    @staticmethod
    def _json_body(payload: Mapping[str, Any]) -> str:
        # X-Sign 必须基于实际发送的同一份 body 字符串，禁止用 requests 的 json=
        # 参数再次序列化。
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _request_id(self, length: int = 19) -> str:
        # 行情文档要求 19 位，登录文档要求 30 位。时间部分后补进程内序号，
        # 每种长度各自单调递增，避免一次登录把后续 19 位行情序号“撑长”。
        if length == 19:
            candidate = (time.time_ns() // 1_000) * 1_000
        elif length == 30:
            candidate = time.time_ns() * 100_000_000_000
        else:
            raise ValueError("unsupported uSMART request id length")
        with self._request_id_lock:
            candidate = max(candidate, self._last_request_ids.get(length, 0) + 1)
            self._last_request_ids[length] = candidate
        request_id = str(candidate)
        if len(request_id) != length:
            raise RuntimeError(f"cannot generate a {length}-digit uSMART request id")
        return request_id

    def _get_signer(self, *, require_public: bool = False) -> USmartSigner:
        if not self.private_key:
            raise RuntimeError("USMART_PRIVATE_KEY is required")
        if require_public and not self.public_key:
            raise RuntimeError("USMART_PUBLIC_KEY is required for automatic login")
        if self._signer is None:
            with self._signer_lock:
                if self._signer is None:
                    try:
                        self._signer = USmartSigner(self.private_key, self.public_key)
                    except (ValueError, IndexError, TypeError) as exc:
                        raise RuntimeError("invalid uSMART RSA key configuration") from exc
        return self._signer

    def _post(self, url: str, body: str, headers: Mapping[str, str]):
        try:
            return self.session.post(
                url,
                data=body.encode("utf-8"),
                headers=dict(headers),
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise USmartAPIError(
                f"uSMART request failed ({type(exc).__name__})"
            ) from exc

    @staticmethod
    def _response_data(response, api_path: str) -> Mapping[str, Any]:
        status_code = getattr(response, "status_code", None)
        if status_code is None or not 200 <= int(status_code) < 300:
            raise USmartAPIError(
                f"uSMART API {api_path} returned HTTP {status_code}",
                status_code=status_code,
            )
        try:
            result = response.json()
        except (TypeError, ValueError) as exc:
            raise USmartAPIError(
                f"uSMART API {api_path} returned invalid JSON",
                status_code=status_code,
            ) from exc
        if not isinstance(result, Mapping):
            raise USmartAPIError(f"uSMART API {api_path} returned an invalid payload")
        code = result.get("code")
        if code != 0:
            message = str(result.get("msg") or "request rejected")
            raise USmartAPIError(
                f"uSMART API {api_path} failed (code={code}): {message}",
                code=code,
                status_code=status_code,
            )
        data = result.get("data")
        return data if isinstance(data, Mapping) else {}

    def login(self) -> str:
        """使用手机号和登录密码获取行情 Authorization token。"""
        with self._login_lock:
            if self.token:
                return self.token
            missing = []
            if not self.channel:
                missing.append("USMART_CHANNEL")
            if not self.phone:
                missing.append("USMART_PHONE")
            if not self.login_password:
                missing.append("USMART_LOGIN_PASSWORD")
            if not self.public_key:
                missing.append("USMART_PUBLIC_KEY")
            if not self.private_key:
                missing.append("USMART_PRIVATE_KEY")
            if missing:
                raise RuntimeError(
                    "uSMART automatic login is not configured: " + ", ".join(missing)
                )

            signer = self._get_signer(require_public=True)
            payload = {
                "phoneNumber": signer.encrypt_login_field(self.phone),
                "password": signer.encrypt_login_field(self.login_password),
                "areaCode": self.area_code,
            }
            body = self._json_body(payload)
            api_path = "/user-server-sg/open-api/login"
            headers = {
                "Content-Type": "application/json; charset=utf-8",
                "X-Lang": self.language,
                "X-Channel": self.channel,
                "X-Request-Id": self._request_id(30),
                "X-Time": str(int(time.time())),
                # 交易/登录接口按官方 demo 使用普通 Base64；行情接口使用 URL-safe Base64。
                "X-Sign": signer.sign(body, urlsafe=False),
            }
            response = self._post(self.trade_host + api_path, body, headers)
            data = self._response_data(response, api_path)
            token = str(data.get("token") or "").strip()
            if not token:
                raise USmartAPIError("uSMART login succeeded without a token")
            self.token = token
            return token

    def quote(self, endpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """调用基础行情接口并返回响应的 data 字典。"""
        if not self.channel:
            raise RuntimeError("USMART_CHANNEL is required")
        token = self.token or self.login()
        signer = self._get_signer()
        body = self._json_body(payload)
        api_path = f"{_QUOTE_API_PREFIX}/{endpoint.lstrip('/')}"
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": token,
            "X-Channel": self.channel,
            "X-Lang": self.language,
            "X-Request-Id": self._request_id(),
            "X-Time": str(int(time.time())),
        }
        row_content = (
            headers["Authorization"]
            + headers["X-Channel"]
            + headers["X-Lang"]
            + headers["X-Request-Id"]
            + headers["X-Time"]
            + body
        )
        headers["X-Sign"] = signer.sign(row_content, urlsafe=True)
        response = self._post(self.quote_host + api_path, body, headers)
        return self._response_data(response, api_path)


def _parse_api_time(value: Any, timezone) -> pd.Timestamp | None:
    """解析 uSMART 的 yyyyMMddHHmmssSSS 行情时间。"""
    if value is None:
        return None
    try:
        digits = str(int(value)) if isinstance(value, (int, float)) else str(value).strip()
        if not digits.isdigit() or len(digits) < 14:
            return None
        base = dt.datetime.strptime(digits[:14], "%Y%m%d%H%M%S")
        fraction = digits[14:20]
        if fraction:
            base = base.replace(microsecond=int(fraction.ljust(6, "0")))
        return pd.Timestamp(timezone.localize(base))
    except (OverflowError, TypeError, ValueError):
        return None


def _timestamp_to_api_time(value: pd.Timestamp) -> int:
    local = value.to_pydatetime()
    return int(local.strftime("%Y%m%d%H%M%S") + f"{local.microsecond // 1000:03d}")


def _to_local_timestamp(value: Any, timezone, *, end_of_day: bool = False) -> pd.Timestamp:
    raw = str(value).strip()
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        if len(raw) == 10:
            # 纯日期表达的是目标市场的交易日；这样查询某个美股自然日不会因时差
            # 被切成前后两个交易日。
            timestamp = pd.Timestamp(timezone.localize(timestamp.to_pydatetime()))
        else:
            # 项目 Web 层把 Unix from/to 统一格式化为上海时间后传给 Exchange。
            # 对美股必须先按该项目契约贴 Asia/Shanghai，再转换成美东时间。
            project_tz = pytz.timezone("Asia/Shanghai")
            timestamp = pd.Timestamp(
                project_tz.localize(timestamp.to_pydatetime())
            ).tz_convert(timezone)
    else:
        timestamp = timestamp.tz_convert(timezone)
    if end_of_day and len(raw) == 10:
        timestamp = timestamp.replace(hour=23, minute=59, second=59, microsecond=999999)
    return timestamp


def _float_value(value: Any) -> float:
    try:
        number = float(value)
        return number if pd.notna(number) else 0.0
    except (TypeError, ValueError):
        return 0.0


class ExchangeUSmart(Exchange):
    """uSMART A/HK/US 三市场基础行情适配器。"""

        # ``latestTime`` 是已完成区间终点。美股常规分钟序列结束于 16:00，
        # 而不是按起点标记的 15:59；日历周期 K 线会在下方规范为当地市场收盘时刻。
    kline_time_label = "end"

    def __init__(
        self,
        market: str,
        client: USmartClient | None = None,
        history_exchange: Exchange | None = None,
    ):
        market_key = str(market).lower()
        if market_key not in _MARKET_API_CODES:
            raise ValueError(f"uSMART does not support market {market!r}")
        self.market = market_key
        self.client = client or USmartClient()
        self.tz = pytz.timezone(_MARKET_TIMEZONES[market_key])
        self._us_history_source = (
            str(
                getattr(config, "US_HISTORY_KLINE_SOURCE", "usmart")
            ).strip().lower()
            if market_key == "us"
            else "usmart"
        )
        if self._us_history_source not in _US_HISTORY_SOURCES:
            raise ValueError(
                "US_HISTORY_KLINE_SOURCE must be one of "
                f"{sorted(_US_HISTORY_SOURCES)}, got "
                f"{self._us_history_source!r}"
            )
            # 延迟创建，确保应用启动时无需打开第二条行情连接，uSMART 仍可提供标的和报价。
        self._us_history_exchange = history_exchange
        self._longbridge_fallback_reported = False
        self._all_stocks: List[Dict[str, Any]] | None = None
        self._stock_by_code: Dict[str, Dict[str, Any]] = {}
        self._all_stocks_lock = threading.Lock()

    def default_code(self) -> str:
        return {
            "a": "SH.000001",
            "hk": "KH.00700",
            "us": "AAPL.US",
        }[self.market]

    def support_frequencys(self) -> dict:
        return dict(_FREQUENCY_LABELS)

    def _configured_us_history_exchange(
        self,
        frequency: str,
        right_type: int,
    ) -> Exchange | None:
        """Return the stable configured US K-line backend for this instance.

        uSMART's intraday endpoint returns at most 300 rows per page and has a
        much shorter provider-side retention window than the chart contract.
        The project already exposes ``US_HISTORY_KLINE_SOURCE``; honoring it
        here keeps full loads and polling on one provider/price basis. Native
        uSMART remains the fallback for unsupported periods and non-forward
        adjustment requests, which the Longbridge adapter cannot represent.
        """

        if (
            self.market != "us"
            or self._us_history_source == "usmart"
            or right_type != 1
        ):
            return None
        if self._us_history_exchange is None:
            if self._us_history_source == "longbridge":
                credentials = tuple(
                    os.getenv(name)
                    for name in (
                        "LONGBRIDGE_APP_KEY",
                        "LONGBRIDGE_APP_SECRET",
                        "LONGBRIDGE_ACCESS_TOKEN",
                    )
                )
                if not all(credentials):
        # 控制台访问令牌不足以完成 API 密钥形式的软件开发工具包流程。本次请求全程使用
        # 已配置的 uSMART 路径以保持行情服务可用；其价格基准元数据可让调用方识别
        # 后续数据提供方变化并进行原子重建。
                    if not self._longbridge_fallback_reported:
                        LogUtil.warning(
                            "Longbridge history credentials are incomplete; "
                            "using uSMART history for this US adapter"
                        )
                        self._longbridge_fallback_reported = True
                    return None
                from chanlun.exchange.exchange_cq import ExchangeChangQiao

                self._us_history_exchange = ExchangeChangQiao()
            else:
                from chanlun.exchange.exchange_alpaca import ExchangeAlpaca

                self._us_history_exchange = ExchangeAlpaca()
        if frequency not in self._us_history_exchange.support_frequencys():
            return None
        return self._us_history_exchange

    def _project_code(self, api_market: str, symbol: str) -> str:
        symbol = str(symbol).strip()
        if self.market == "a":
            if api_market not in {"sh", "sz"}:
                raise ValueError(f"invalid A-share market {api_market!r}")
            return f"{api_market.upper()}.{symbol}"
        if self.market == "hk":
            return f"KH.{symbol.zfill(5)}"
        return f"{symbol.upper()}.US"

    def _to_secu_id(self, code: str) -> str:
        value = str(code or "").strip()
        if not value:
            raise ValueError("empty security code")
        upper = value.upper()

        if self.market == "a":
            parts = upper.split(".", 1)
            if len(parts) == 2 and parts[0] in {"SH", "SZ"}:
                api_market, symbol = parts[0].lower(), parts[1]
            elif len(parts) == 2 and parts[1] in {"SH", "SZ"}:
                api_market, symbol = parts[1].lower(), parts[0]
            elif "." not in upper:
                symbol = upper
                api_market = "sh" if symbol.startswith(("5", "6", "9")) else "sz"
            else:
                raise ValueError(f"unsupported A-share code {code!r}")
            if not symbol:
                raise ValueError(f"invalid A-share code {code!r}")
            return api_market + symbol

        if self.market == "hk":
            if upper.startswith(("KH.", "HK.")):
                symbol = upper.split(".", 1)[1]
            elif upper.endswith(".HK"):
                symbol = upper[:-3]
            else:
                symbol = upper
            return "hk" + symbol.zfill(5)

        if upper.startswith("US."):
            symbol = upper[3:]
        elif upper.endswith(".US"):
            symbol = upper[:-3]
        else:
            symbol = upper
        return "us" + symbol

    @staticmethod
    def _security_type(type_value: Any) -> str:
        try:
            type_id = int(type_value)
        except (TypeError, ValueError):
            return "unknown"
        return {
            1: "stock_cn",
            2: "etf_cn",
            6: "index_cn",
        }.get(type_id, "unknown")

    def all_stocks(self):
        if self._all_stocks is not None:
            return self._all_stocks
        with self._all_stocks_lock:
            if self._all_stocks is not None:
                return self._all_stocks
            stocks: List[Dict[str, Any]] = []
            for api_market in _MARKET_API_CODES[self.market]:
                data = self.client.quote("basicinfo", {"market": api_market})
                items = data.get("list") or []
                if not isinstance(items, list):
                    continue
                for item in items:
                    if not isinstance(item, Mapping):
                        continue
                    symbol = str(item.get("symbol") or "").strip()
                    if not symbol:
                        continue
                    item_market = str(item.get("market") or api_market).lower()
                    try:
                        project_code = self._project_code(item_market, symbol)
                    except ValueError:
                        continue
                    name = (
                        str(item.get("nameChs") or "").strip()
                        or str(item.get("nameCht") or "").strip()
                        or str(item.get("nameEn") or "").strip()
                        or symbol
                    )
                    stock = {
                        "code": project_code,
                        "name": name,
                        "lot_size": int(item.get("lotSize") or 0),
                    }
                    if self.market == "a":
                        stock["type"] = self._security_type(item.get("type1"))
                    stocks.append(stock)
            stocks.sort(key=lambda item: item["code"])
            self._all_stocks = stocks
            self._stock_by_code = {item["code"].upper(): item for item in stocks}
            return self._all_stocks

    @staticmethod
    def _right_type(args: Mapping[str, Any]) -> int:
        value = args.get("right", args.get("fq", args.get("fq_type", "qfq")))
        if isinstance(value, int) and value in {0, 1, 2}:
            return value
        normalized = str(value).strip().lower()
        rights = {
            "0": 0,
            "none": 0,
            "no": 0,
            "bfq": 0,
            "1": 1,
            "qfq": 1,
            "front": 1,
            "forward": 1,
            "2": 2,
            "hfq": 2,
            "back": 2,
            "backward": 2,
        }
        if normalized not in rights:
            raise ValueError(f"unsupported uSMART adjustment type {value!r}")
        return rights[normalized]

    def _align_calendar_bar(self, value: pd.Timestamp, frequency: str) -> pd.Timestamp:
        if frequency not in _CALENDAR_FREQUENCIES:
            return value
        if value.hour or value.minute or value.second or value.microsecond:
            return value
        hour, minute = _MARKET_CLOSE_TIMES[self.market]
        return value.replace(hour=hour, minute=minute)

    def klines(
        self,
        code: str,
        frequency: str,
        start_date: str = None,
        end_date: str = None,
        args=None,
    ) -> Union[pd.DataFrame, None]:
        if frequency not in _FREQUENCY_TYPES:
            raise ValueError(f"uSMART does not support frequency {frequency!r}")
        args = dict(args or {})
        right_type = self._right_type(args)
        history_exchange = self._configured_us_history_exchange(
            frequency,
            right_type,
        )
        if history_exchange is not None:
            return history_exchange.klines(
                code,
                frequency,
                start_date=start_date,
                end_date=end_date,
                args=args,
            )
        page_size = _positive_number(
            args.get("count", getattr(config, "USMART_KLINE_PAGE_SIZE", 1000)),
            1000,
            integer=True,
        )
        max_pages = _positive_number(
            args.get("pages", getattr(config, "USMART_KLINE_MAX_PAGES", 10)),
            10,
            integer=True,
        )

        end_bound = (
            _to_local_timestamp(end_date, self.tz, end_of_day=True)
            if end_date is not None
            else pd.Timestamp.now(tz=self.tz)
        )
        if start_date is not None:
            start_bound = _to_local_timestamp(start_date, self.tz)
        else:
            from chanlun.exchange._lookback import get_lookback_timedelta

            start_bound = end_bound - get_lookback_timedelta(frequency)
        if start_bound > end_bound:
            raise ValueError("start_date must not be later than end_date")

        secu_id = self._to_secu_id(code)
        cursor = 0 if end_date is None else _timestamp_to_api_time(end_bound)
        rows: List[Mapping[str, Any]] = []
        seen_cursors = set()

        for _ in range(max_pages):
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            data = self.client.quote(
                "kline",
                {
                    "secuId": secu_id,
                    "type": _FREQUENCY_TYPES[frequency],
                    "start": cursor,
                    "right": right_type,
                    "count": page_size,
                },
            )
            page = data.get("list") or []
            if not isinstance(page, list) or not page:
                break
            valid_times = []
            for item in page:
                if not isinstance(item, Mapping):
                    continue
                parsed = _parse_api_time(item.get("latestTime"), self.tz)
                if parsed is None:
                    continue
                rows.append(item)
                valid_times.append(parsed)
            if not valid_times:
                break
            oldest = min(valid_times)
            if oldest <= start_bound:
                break
            next_cursor = _timestamp_to_api_time(oldest - pd.Timedelta(milliseconds=1))
            if next_cursor == cursor:
                break
            cursor = next_cursor

        if not rows:
            return pd.DataFrame(columns=_KLINE_COLUMNS)

        records = []
        for item in rows:
            timestamp = _parse_api_time(item.get("latestTime"), self.tz)
            if timestamp is None:
                continue
            timestamp = self._align_calendar_bar(timestamp, frequency)
            if timestamp < start_bound or timestamp > end_bound:
                continue
            records.append(
                {
                    "date": timestamp,
                    "frequency": frequency,
                    "code": code,
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                    "volume": item.get("volume", 0),
                }
            )
        frame = pd.DataFrame(records, columns=_KLINE_COLUMNS)
        if frame.empty:
            return frame
        for column in ("open", "high", "low", "close", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=["date", "open", "high", "low", "close"])
        frame["volume"] = frame["volume"].fillna(0)
        frame = frame.drop_duplicates(subset=["date"], keep="last")
        frame = frame.sort_values("date").reset_index(drop=True)
        frame = normalize_kline_precision(frame, self.market, code)

        quantum = resolve_structure_price_quantum(self.market, code)
        if quantum is not None and not frame.empty:
            adjustment = {0: "none", 1: "forward", 2: "backward"}[right_type]
            metadata = build_provider_price_basis_metadata(
                provider="usmart",
                market=self.market,
                code=code,
                adjustment=adjustment,
                structure_price_quantum=quantum,
            )
            frame = attach_price_basis_metadata(frame, metadata)
        return frame

    def ticks(self, codes: List[str]) -> Dict[str, Tick]:
        if not codes:
            return {}
        secu_to_project = {self._to_secu_id(code): code for code in codes}
        data = self.client.quote("realtime", {"secuIds": list(secu_to_project)})
        items = data.get("list") or []
        ticks: Dict[str, Tick] = {}
        if not isinstance(items, list):
            return ticks
        for item in items:
            if not isinstance(item, Mapping):
                continue
            api_market = str(item.get("market") or "").lower()
            symbol = str(item.get("symbol") or "").strip()
            secu_id = api_market + symbol
            project_code = secu_to_project.get(secu_id)
            if project_code is None:
                continue
            last = _float_value(item.get("latestPrice", item.get("close")))
            previous_close = _float_value(item.get("preClose"))
            rate = (
                round((last - previous_close) / previous_close * 100, 2)
                if previous_close > 0
                else 0
            )
            ticks[project_code] = Tick(
                code=project_code,
                last=last,
                buy1=_float_value(item.get("bidPrice")),
                sell1=_float_value(item.get("askPrice")),
                high=_float_value(item.get("high")),
                low=_float_value(item.get("low")),
                open=_float_value(item.get("open")),
                volume=_float_value(item.get("volume")),
                rate=rate,
            )
        return ticks

    def stock_info(self, code: str) -> Union[Dict, None]:
        self.all_stocks()
        try:
            secu_id = self._to_secu_id(code)
            api_market = secu_id[:2]
            normalized_code = self._project_code(api_market, secu_id[2:])
        except ValueError:
            normalized_code = str(code)
        return self._stock_by_code.get(normalized_code.upper())

    def now_trading(self, market: str):
        api_market = _MARKET_API_CODES[self.market][0]
        try:
            data = self.client.quote("marketstate", {"market": api_market})
            status = int(data.get("status"))
            return status in {
                2,
                4,
                6,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
                31,
                32,
                61,
                62,
            }
        except Exception as exc:
            LogUtil.warning(
                f"uSMART market state unavailable for {self.market}: {type(exc).__name__}"
            )
            return None

    def stock_owner_plate(self, code: str):
        raise Exception("uSMART 基础行情接口不支持板块查询")

    def plate_stocks(self, code: str):
        raise Exception("uSMART 基础行情接口不支持板块查询")

    def balance(self):
        raise Exception("uSMART 行情适配器不支持账户查询")

    def positions(self, code: str = ""):
        raise Exception("uSMART 行情适配器不支持持仓查询")

    def order(self, code: str, o_type: str, amount: float, args=None):
        raise Exception("uSMART 行情适配器不支持交易")


__all__ = [
    "ExchangeUSmart",
    "USmartAPIError",
    "USmartClient",
    "USmartSigner",
]
