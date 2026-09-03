"""Account-scoped chart persistence helpers.

This module creates a deterministic server-side storage namespace from the
authenticated username and stores the small pieces of workspace UI state that
TradingView itself does not persist.
"""

from __future__ import annotations

import hashlib
import json
import re
from threading import RLock
from typing import Any

from chanlun.persistence.db import db
from chanlun.security import normalize_login_username


PREFERENCE_SCHEMA = "chanlun-account-chart-preferences/v1"
PREFERENCE_CHART_TYPE = "preference"
PREFERENCE_NAME = "workspace"
PREFERENCE_CLIENT = "workspace-preferences"

_MARKETS = {
    "a",
    "hk",
    "us",
    "fx",
    "futures",
    "ny_futures",
    "currency",
    "currency_spot",
}
_LAYOUTS = {
    "single",
    "horizontal-2",
    "horizontal-7-3",
    "vertical-2",
    "three",
    "four",
}
_INTERVALS = {
    "10S",
    "30S",
    "1",
    "2",
    "3",
    "5",
    "10",
    "15",
    "30",
    "60",
    "120",
    "180",
    "240",
    "360",
    "480",
    "720",
    "1D",
    "2D",
    "3D",
    "1W",
    "1M",
    "3M",
    "12M",
    "D",
    "W",
    "M",
}
_DISPLAY_CONFIG_KEY = re.compile(r"^cl_show_config_([1-4])_([A-Za-z0-9_]{1,10})$")
_DRAWING_MODE_KEY = re.compile(r"^cl_independent_drawings_([1-4])$")
_MAX_PREFERENCE_BYTES = 128 * 1024
_PREFERENCE_SAVE_LOCK = RLock()
_SCREENING_POINT_TYPES = {
    "all", "buy", "sell", "1buy", "2buy", "3buy", "1sell", "2sell", "3sell"
}
_SCREENING_LIFECYCLES = {
    "all", "observed", "monitoring", "approaching", "triggered", "executable", "active"
}


class InvalidAccountPreferences(ValueError):
    """Raised when a client preference document violates the storage contract."""


def storage_scope_for_username(username: object) -> str:
    canonical = normalize_login_username(username)
    if not canonical:
        canonical = "disabled-test-account"
    return hashlib.sha256(
        ("chanlun-pro-account-scope\0" + canonical).encode("utf-8")
    ).hexdigest()


def storage_user_id_for_username(username: object) -> int:
    """Return a positive signed-INT id for the existing ``user_id`` column.

    The full account digest is also embedded in ``client_id`` below, so even the
    theoretical 31-bit numeric collision cannot merge two users' records.
    """

    digest = bytes.fromhex(storage_scope_for_username(username))
    value = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    if value == 0:
        value += 1
    return value


def scoped_client_id(account_scope: str, requested_client: object) -> str:
    raw = str(requested_client or "").strip()
    if not raw or len(raw) > 128:
        raise ValueError("client must be a non-empty string of at most 128 characters")
    client_digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    # 2 + 16 + 1 + 24 = 43, within the database VARCHAR(50) column.
    return f"u_{account_scope[:16]}_{client_digest[:24]}"


def storage_identity_for_user(user: object, requested_client: object) -> tuple[str, int]:
    username = getattr(user, "username", None) or "disabled-test-account"
    scope = str(getattr(user, "storage_scope", "") or "")
    if not scope:
        scope = storage_scope_for_username(username)
    user_id = getattr(user, "storage_user_id", None)
    if type(user_id) is not int or user_id <= 0:
        user_id = storage_user_id_for_username(username)
    return scoped_client_id(scope, requested_client), user_id


def chart_storage_identity(user: object, requested_client: object) -> tuple[str, int]:
    return storage_identity_for_user(user, requested_client)


def _json_string(value: object, *, key: str) -> tuple[object, str]:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 32 * 1024:
        raise InvalidAccountPreferences(f"{key} must be a bounded JSON string")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidAccountPreferences(f"{key} contains invalid JSON") from exc
    return parsed, json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _normalize_tv_chart(raw: object) -> str:
    parsed, _ = _json_string(raw, key="tv_chart")
    if not isinstance(parsed, dict):
        raise InvalidAccountPreferences("tv_chart must contain an object")

    normalized: dict[str, str] = {}
    theme = parsed.get("theme")
    if theme in {"Light", "dark"}:
        normalized["theme"] = theme
    market = parsed.get("market")
    if market in _MARKETS:
        normalized["market"] = market
    layout = parsed.get("chart_layout_type")
    if layout in _LAYOUTS:
        normalized["chart_layout_type"] = layout

    for selected_market in _MARKETS:
        code_key = f"{selected_market}_code"
        code = parsed.get(code_key)
        if isinstance(code, str) and 0 < len(code.strip()) <= 100:
            normalized[code_key] = code.strip()
        for chart_id in range(1, 5):
            interval_key = f"{selected_market}_interval_{chart_id}"
            interval = parsed.get(interval_key)
            if interval in _INTERVALS:
                normalized[interval_key] = interval
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _normalize_display_config(key: str, raw: object) -> str:
    parsed, _ = _json_string(raw, key=key)
    if (
        not isinstance(parsed, dict)
        or len(parsed) > 100
        or not isinstance(parsed.get("schema"), str)
    ):
        raise InvalidAccountPreferences(f"{key} must contain a chart config object")
    for name, value in parsed.items():
        if not isinstance(name, str) or len(name) > 80:
            raise InvalidAccountPreferences(f"{key} contains an invalid field")
        if name == "schema":
            if len(value) > 80:
                raise InvalidAccountPreferences(f"{key} schema is too long")
        elif type(value) is not bool:
            raise InvalidAccountPreferences(f"{key} values must be booleans")
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalize_trading_screening_view(raw: object) -> str:
    parsed, _ = _json_string(raw, key="trading_screening_view")
    if not isinstance(parsed, dict):
        raise InvalidAccountPreferences("trading_screening_view must contain an object")
    contract = parsed.get("contract")
    if not isinstance(contract, str) or not contract or len(contract) > 160:
        raise InvalidAccountPreferences("trading_screening_view contract is invalid")

    allowed_values = {
        "pointType": _SCREENING_POINT_TYPES,
        "lifecycle": _SCREENING_LIFECYCLES,
        "market": {"all", "a", "us"},
        "signalSource": {"all", "screening", "notification", "attention", "watchlist"},
        "reviewStage": {"all", "forming", "notified", "tracking"},
        "segmentState": {"all", "present", "current", "historical", "absent"},
        "selectionScope": {"all-qualified", "sector-trigger"},
        "layout": {"focus", "dual", "triple", "quad"},
    }
    normalized: dict[str, object] = {"contract": contract}
    for key, accepted in allowed_values.items():
        value = parsed.get(key)
        if value in accepted:
            normalized[key] = value

    signal_list_open = parsed.get("signalListOpen")
    if type(signal_list_open) is bool:
        normalized["signalListOpen"] = signal_list_open

    sizing = parsed.get("chartSizing")
    if isinstance(sizing, dict):
        heights = sizing.get("heights")
        normalized_heights: dict[str, int | None] = {}
        if isinstance(heights, dict):
            for layout in ("focus", "dual", "triple", "quad"):
                value = heights.get(layout)
                if value is None:
                    normalized_heights[layout] = None
                elif type(value) in {int, float} and 520 <= value <= 1200:
                    normalized_heights[layout] = round(value)
        normalized_sizing: dict[str, object] = {"heights": normalized_heights}
        for key, minimum, maximum in (
            ("dualRatio", 30, 70),
            ("tripleMainRatio", 55, 80),
            ("tripleSideRatio", 25, 75),
        ):
            value = sizing.get(key)
            if type(value) in {int, float} and minimum <= value <= maximum:
                normalized_sizing[key] = round(value)
        normalized["chartSizing"] = normalized_sizing
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _normalize_value(key: str, raw: object) -> str:
    if key == "tv_chart":
        return _normalize_tv_chart(raw)
    if key == "trading_screening_view":
        return _normalize_trading_screening_view(raw)
    if key == "chart_menu_width":
        try:
            width = int(str(raw))
        except (TypeError, ValueError) as exc:
            raise InvalidAccountPreferences("chart_menu_width must be an integer") from exc
        if not 240 <= width <= 900:
            raise InvalidAccountPreferences("chart_menu_width is out of range")
        return str(width)
    if key in {"chart_menu_collapsed", "chart_analysis_overview_collapsed"}:
        if str(raw) not in {"0", "1"}:
            raise InvalidAccountPreferences(f"{key} must be 0 or 1")
        return str(raw)
    if _DRAWING_MODE_KEY.fullmatch(key):
        if str(raw).lower() not in {"true", "false"}:
            raise InvalidAccountPreferences(f"{key} must be a boolean JSON value")
        return str(raw).lower()
    if _DISPLAY_CONFIG_KEY.fullmatch(key):
        return _normalize_display_config(key, raw)
    raise InvalidAccountPreferences(f"unsupported preference key: {key}")


def _is_supported_preference_key(key: object) -> bool:
    return bool(
        isinstance(key, str)
        and (
            key
            in {
                "tv_chart",
                "trading_screening_view",
                "chart_menu_width",
                "chart_menu_collapsed",
                "chart_analysis_overview_collapsed",
            }
            or _DRAWING_MODE_KEY.fullmatch(key)
            or _DISPLAY_CONFIG_KEY.fullmatch(key)
        )
    )


def normalize_preferences(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema") != PREFERENCE_SCHEMA:
        raise InvalidAccountPreferences("unsupported preference schema")
    values = payload.get("values")
    if not isinstance(values, dict) or len(values) > 100:
        raise InvalidAccountPreferences("values must be a bounded object")

    normalized_values: dict[str, str] = {}
    for key, raw in values.items():
        if not isinstance(key, str):
            raise InvalidAccountPreferences("preference keys must be strings")
        normalized_values[key] = _normalize_value(key, raw)
    normalized = {"schema": PREFERENCE_SCHEMA, "values": normalized_values}
    if len(json.dumps(normalized, ensure_ascii=False).encode("utf-8")) > _MAX_PREFERENCE_BYTES:
        raise InvalidAccountPreferences("preference document is too large")
    return normalized


def empty_preferences() -> dict[str, Any]:
    return {"schema": PREFERENCE_SCHEMA, "values": {}}


def load_preferences_for_user(user: object) -> tuple[dict[str, Any], bool, int | None]:
    client_id, user_id = storage_identity_for_user(user, PREFERENCE_CLIENT)
    record = db.tv_chart_get_by_name(
        PREFERENCE_CHART_TYPE,
        PREFERENCE_NAME,
        client_id,
        user_id,
    )
    if record is None:
        return empty_preferences(), False, None
    try:
        preferences = normalize_preferences(json.loads(record.content))
    except (TypeError, json.JSONDecodeError, InvalidAccountPreferences):
        return empty_preferences(), True, getattr(record, "timestamp", None)
    return preferences, True, getattr(record, "timestamp", None)


def save_preferences_for_user(user: object, payload: object) -> dict[str, Any]:
    normalized = normalize_preferences(payload)
    client_id, user_id = storage_identity_for_user(user, PREFERENCE_CLIENT)
    merge = isinstance(payload, dict) and payload.get("merge") is True
    changed_keys: tuple[str, ...] = ()
    if merge:
        raw_changed_keys = payload.get("changed_keys")
        if (
            not isinstance(raw_changed_keys, list)
            or len(raw_changed_keys) > 100
            or any(not _is_supported_preference_key(key) for key in raw_changed_keys)
            or len(raw_changed_keys) != len(set(raw_changed_keys))
        ):
            raise InvalidAccountPreferences("changed_keys must be unique supported keys")
        changed_keys = tuple(raw_changed_keys)

    # The existing storage API has no compare-and-swap primitive. Serialize the
    # tiny read/merge/write section so two browser tabs changing different keys
    # cannot both read the same revision and lose the first completed update.
    with _PREFERENCE_SAVE_LOCK:
        if merge:
            current, _exists, _updated_at = load_preferences_for_user(user)
            merged_values = dict(current["values"])
            incoming_values = normalized["values"]
            for key in changed_keys:
                if key in incoming_values:
                    merged_values[key] = incoming_values[key]
                else:
                    merged_values.pop(key, None)
            normalized = normalize_preferences(
                {"schema": PREFERENCE_SCHEMA, "values": merged_values}
            )
        db.tv_chart_save(
            PREFERENCE_CHART_TYPE,
            client_id,
            user_id,
            PREFERENCE_NAME,
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
            "",
            "",
        )
        return normalized
