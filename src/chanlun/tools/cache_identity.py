"""Build a content identity for every source file that affects chart output.

The identity is part of chart-data and Chanlun-object cache keys.  A change to
calculation, rendering, or market-data normalization therefore invalidates the
affected cache automatically; there is no manual release number to maintain.
"""

from __future__ import annotations

import hashlib
import pathlib


_FP: str | None = None


def _fingerprint_files() -> list[pathlib.Path]:
    """Return the complete deterministic source set for chart cache identity."""

    package = pathlib.Path(__file__).resolve().parents[1]
    files = sorted((package / "core").rglob("*.py"))
    files += [
        package / "cl_utils" / "tv_chart.py",
        package / "cl_utils" / "strict_chart.py",
        package / "cl_utils" / "strict_chart_runtime.py",
        package / "cl_utils" / "chart_config.py",
    ]
    files += sorted((package / "exchange").glob("*.py"))
    return files


def source_fingerprint() -> str:
    """Return the process-cached content identity used by chart cache keys."""

    global _FP
    if _FP is not None:
        return _FP
    digest = hashlib.md5()
    for path in _fingerprint_files():
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass
    _FP = digest.hexdigest()[:8]
    return _FP
