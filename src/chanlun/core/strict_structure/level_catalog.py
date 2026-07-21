from __future__ import annotations


_LABELS = {
    "1m": ("1m", "5m", "30m", "日线"),
    "5m": ("5m", "30m", "日线"),
    "30m": ("30m", "日线"),
    "d": ("日线",),
}


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
    return _LABELS.get(key, (key,))
