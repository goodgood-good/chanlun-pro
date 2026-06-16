"""strategy_optimizer 共享数值/路径 helper —— 从 _impl 拆出。

被 scoring + 各类 reports 共享的纯函数:数值提取(_float_first/_float_values/_avg)、
路径规范化(_path_key)。自包含,只依赖标准库。
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping


def _float_first(summary: Mapping[str, object], *keys: str) -> float:
    for key in keys:
        if key not in summary:
            continue
        try:
            value = float(summary[key] or 0.0)
        except (TypeError, ValueError):
            continue
        if value == value:
            return value
    return 0.0


def _float_values(rows: Iterable[Mapping[str, object]], key: str) -> list[float]:
    values: list[float] = []
    for row in rows:
        if key not in row:
            continue
        raw = row.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value == value:
            values.append(value)
    return values


def _avg(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _path_key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve()).replace("\\", "/").lower()
    except Exception:
        return str(path).replace("\\", "/").lower()
