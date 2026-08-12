"""Persist presentation preferences for the production chart.

Structure recognition is fixed by ``strict_base_config`` and is deliberately
absent from this module.  A saved chart preference can hide base geometry; it
cannot select another K-line, fractal, stroke, segment, center, divergence or
buy/sell-point algorithm.
"""

from __future__ import annotations

import copy
import time
from threading import RLock
from typing import Dict

from chanlun.persistence.db import db
from chanlun.tools.log_util import LogUtil


CL_CHART_CONFIG_PERSIST_KEYS = (
    "config_use_type",
    "chart_show_fx",
    "chart_show_bi",
    "chart_show_xd",
)

# 结构算法没有用户可配置的缓存维度。
CL_COMPUTE_CACHE_CONFIG_KEYS: tuple[str, ...] = ()

_DEFAULT_CONFIG: Dict[str, object] = {
    "config_use_type": "common",
    "chart_show_fx": "1",
    "chart_show_bi": "1",
    "chart_show_xd": "1",
}

_cl_config_cache: dict[str, dict[str, object]] = {}
_cl_config_cache_lock = RLock()
_cl_config_cache_ttl = 300
_cl_config_db_backoff_until = 0.0


def _normalize_code(market: str, code: str | None) -> str:
    value = "" if code is None else str(code)
    if market == "futures":
        value = value.upper().replace("KQ.M@", "")
        value = "".join(character for character in value if not character.isdigit())
    return value


def _cache_get(key: str) -> dict[str, object] | None:
    now = time.time()
    with _cl_config_cache_lock:
        item = _cl_config_cache.get(key)
        if item is None:
            return None
        if float(item["expire_at"]) <= now:
            _cl_config_cache.pop(key, None)
            return None
        return copy.deepcopy(item["config"])


def _cache_set(key: str, config: dict[str, object]) -> None:
    with _cl_config_cache_lock:
        _cl_config_cache[key] = {
            "expire_at": time.time() + _cl_config_cache_ttl,
            "config": copy.deepcopy(config),
        }


def _cache_invalidate(market: str) -> None:
    prefix = f"{market}:"
    with _cl_config_cache_lock:
        for key in tuple(_cl_config_cache):
            if key.startswith(prefix):
                _cl_config_cache.pop(key, None)


def _validated_preferences(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    allowed = set(CL_CHART_CONFIG_PERSIST_KEYS)
    return {key: value[key] for key in allowed if key in value}


def query_cl_chart_config(
    market: str,
    code: str | None,
    suffix: str = "",
) -> Dict[str, object]:
    """Return presentation-only preferences for one chart."""

    normalized_code = _normalize_code(market, code)
    local_key = f"{market}:{normalized_code}:{suffix}"
    cached = _cache_get(local_key)
    if cached is not None:
        return cached

    global _cl_config_db_backoff_until
    now = time.time()
    with _cl_config_cache_lock:
        skip_db = now < _cl_config_db_backoff_until

    stored = None
    db_consulted = False
    if not skip_db:
        try:
            stored = db.cache_get(
                f"cl_config_{market}_{normalized_code}{suffix}"
            )
            if stored is None:
                stored = db.cache_get(f"cl_config_{market}_common{suffix}")
            db_consulted = True
            with _cl_config_cache_lock:
                _cl_config_db_backoff_until = 0.0
        except Exception as exc:
            with _cl_config_cache_lock:
                _cl_config_db_backoff_until = time.time() + 30
            LogUtil.error(
                "[query_cl_chart_config] DB read failed "
                f"market={market} code={normalized_code} err={exc}",
                exc_info=True,
            )

    result = copy.deepcopy(_DEFAULT_CONFIG)
    result.update(_validated_preferences(stored))
    if db_consulted:
        _cache_set(local_key, result)
    return result


def set_cl_chart_config(
    market: str,
    code: str | None,
    config: Dict[str, object],
    suffix: str = "",
) -> bool:
    """Persist presentation preferences and reject algorithm fields."""

    unknown = set(config) - set(CL_CHART_CONFIG_PERSIST_KEYS)
    if unknown:
        raise ValueError(
            "unsupported chart config keys: " + ",".join(sorted(unknown))
        )
    normalized_code = _normalize_code(market, code)
    use_type = str(config.get("config_use_type") or "")
    if use_type not in {"common", "custom"}:
        raise ValueError("config_use_type must be common or custom")
    if use_type == "custom" and not normalized_code:
        return False

    result = copy.deepcopy(_DEFAULT_CONFIG)
    result.update(_validated_preferences(config))
    target = normalized_code if use_type == "custom" else "common"
    if use_type == "common" and normalized_code:
        db.cache_del(f"cl_config_{market}_{normalized_code}{suffix}")
    db.cache_set(f"cl_config_{market}_{target}{suffix}", result)
    _cache_invalidate(market)
    return True


def del_cl_chart_config(
    market: str,
    code: str | None,
    suffix: str = "",
) -> bool:
    """Delete one presentation preference record."""

    normalized_code = _normalize_code(market, code)
    db.cache_del(f"cl_config_{market}_{normalized_code}{suffix}")
    _cache_invalidate(market)
    return True


__all__ = (
    "CL_CHART_CONFIG_PERSIST_KEYS",
    "CL_COMPUTE_CACHE_CONFIG_KEYS",
    "del_cl_chart_config",
    "query_cl_chart_config",
    "set_cl_chart_config",
)
