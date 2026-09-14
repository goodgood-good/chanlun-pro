from __future__ import annotations


# Recursive structure is derived from one physical bar stream. The engine stops
# naturally when it can no longer assemble a higher-level unit; this catalog is
# only a defensive/display bound and must never imply a different bar frequency.
MAX_RECURSIVE_STRUCTURE_LEVELS = 50

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


def _validate_recursive_level(recursive_level: int) -> None:
    if type(recursive_level) is not int or recursive_level < 0:
        raise ValueError("recursive_level must be a non-negative integer")
    if recursive_level >= MAX_RECURSIVE_STRUCTURE_LEVELS:
        raise ValueError("recursive level is outside the bounded catalog")


def recursive_level_labels(source_frequency: str) -> tuple[str, ...]:
    """Return explicit structural labels without inventing physical periods."""

    source = _canonical_frequency(source_frequency)
    return tuple(
        f"{source}/L{level}" for level in range(MAX_RECURSIVE_STRUCTURE_LEVELS)
    )


def effective_frequency(source_frequency: str, recursive_level: int) -> str:
    """Return the physical source period for a bounded recursive level.

    ``recursive_level`` describes structure assembled from the source stream; it
    is not a resampling operation. Consequently ``5m/L2`` remains physical 5m
    evidence and must not be relabelled as a 30-minute or daily chart.
    """

    _validate_recursive_level(recursive_level)
    return _canonical_frequency(source_frequency)


def effective_frequency_rank(source_frequency: str, recursive_level: int) -> int:
    """Return physical-period rank; recursive depth is intentionally separate."""

    return _FREQUENCY_RANK[effective_frequency(source_frequency, recursive_level)]


__all__ = (
    "MAX_RECURSIVE_STRUCTURE_LEVELS",
    "effective_frequency",
    "effective_frequency_rank",
    "recursive_level_labels",
)
