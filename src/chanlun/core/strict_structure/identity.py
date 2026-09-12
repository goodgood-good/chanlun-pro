from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any


def _json_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def _stable_structure_id_uncached(namespace: str, parts: tuple[Any, ...]) -> str:
    payload = json.dumps(
        [namespace, *(_json_value(part) for part in parts)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@lru_cache(maxsize=32_768)
def _stable_structure_id_cached(namespace: str, parts: tuple[Any, ...]) -> str:
    return _stable_structure_id_uncached(namespace, parts)


def stable_structure_id(namespace: str, *parts: Any) -> str:
    """返回与进程无关的结构事实 SHA-256 身份。

    中枢计算会多次构造完全相同的单元、事件和中枢身份。对可哈希参数使用有界
    LRU，只消除重复 JSON 编码与 SHA-256，不改变规范载荷；包含列表或映射的罕见
    调用自动走原路径。
    """

    if not namespace:
        raise ValueError("namespace is required")
    normalized_parts = tuple(parts)
    try:
        hash(normalized_parts)
    except TypeError:
        return _stable_structure_id_uncached(namespace, normalized_parts)
    return _stable_structure_id_cached(namespace, normalized_parts)


def build_center_id(
    *,
    price_basis_revision: str,
    structural_level: int,
    source_kind: str,
    entry_unit_id: str | None,
    initial_unit_ids: tuple[str, ...],
    establishment_leave_unit_id: str | None,
    zd_tick: int,
    zg_tick: int,
    formation_rule: str | None = None,
) -> str:
    """返回正式中枢种子的不可变身份。"""

    normalized_source_kind = getattr(source_kind, "value", source_kind)
    if normalized_source_kind not in {"segment", "stroke_observation"} or formation_rule not in {None, "five_role"}:
        raise ValueError("only physical five-role centers are supported")
    if normalized_source_kind in {"segment", "stroke_observation"}:
        # A physical center is the five-role fact: entry + core A/B/C + the
        # independent establishment leave. Never hash a partial physical seed.
        if not entry_unit_id or not establishment_leave_unit_id:
            raise ValueError(
                "physical center identity requires entry and establishment leave"
            )
    if len(initial_unit_ids) != 3 or any(
        not isinstance(unit_id, str) or not unit_id
        for unit_id in initial_unit_ids
    ):
        raise ValueError("center identity requires exactly three core unit ids")

    return stable_structure_id(
        "chanlun-center",
        price_basis_revision,
        structural_level,
        normalized_source_kind,
        entry_unit_id,
        tuple(initial_unit_ids),
        establishment_leave_unit_id,
        zd_tick,
        zg_tick,
    )
