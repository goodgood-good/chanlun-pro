from __future__ import annotations


_EFFECTIVE_FREQUENCIES = {
    "1m": ("1m", "5m", "30m", "d"),
    "5m": ("5m", "30m", "d"),
    "30m": ("30m", "d"),
    "d": ("d",),
}
_DISPLAY_LABELS = {"1m": "1m", "5m": "5m", "30m": "30m", "d": "日线"}
_FREQUENCY_RANK = {"1m": 0, "5m": 1, "30m": 2, "d": 3}


def _canonical_frequency(value: str) -> str:
    raw = str(value).strip()
    aliases = {
        "1": "1m",
        "5": "5m",
        "30": "30m",
        "1D": "d",
        "1d": "d",
        "day": "d",
    }
    if raw in aliases:
        return aliases[raw]
    return f"{raw}m" if raw.isdigit() else raw.lower()


def recursive_level_labels(source_frequency: str) -> tuple[str, ...]:
    key = _canonical_frequency(source_frequency)
    values = _EFFECTIVE_FREQUENCIES.get(key, (key,))
    return tuple(_DISPLAY_LABELS.get(value, value) for value in values)


def effective_frequency(source_frequency: str, recursive_level: int) -> str:
    """返回物理周期与递归级别共同代表的唯一有效周期。"""

    if type(recursive_level) is not int or recursive_level < 0:
        raise ValueError("recursive_level must be a non-negative integer")
    key = _canonical_frequency(source_frequency)
    levels = _EFFECTIVE_FREQUENCIES.get(key)
    if levels is None or recursive_level >= len(levels):
        raise ValueError("unsupported physical frequency recursive level")
    return levels[recursive_level]


def effective_frequency_rank(source_frequency: str, recursive_level: int) -> int:
    """返回跨物理周期可直接比较的有效结构级别序号。"""

    return _FREQUENCY_RANK[effective_frequency(source_frequency, recursive_level)]
