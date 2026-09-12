"""Chart-data-only producer identity, separate from global CL object caches."""

from functools import lru_cache
import hashlib
from pathlib import Path


def chart_producer_files():
    app = Path(__file__).resolve().parents[1]
    return tuple(app / name for name in (
        "services/chart_producer_identity.py",
        "services/chart_bar_time.py",
        "services/chart_compute.py",
        "services/chart_initial_build.py",
        "services/chart_market_state.py",
        "services/chart_cache.py",
        "services/kline_recompute.py",
        "services/sse_refresh.py",
        "services/sse_signature.py",
        "handlers/sse_stream.py",
        "blueprints/tv.py",
    ))


@lru_cache(maxsize=1)
def chart_producer_revision():
    digest = hashlib.sha256(b"native-center-chart-producer-v2\0")
    for path in chart_producer_files():
        digest.update(path.relative_to(Path(__file__).resolve().parents[1]).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<absent>")
        digest.update(b"\0")
    return digest.hexdigest()[:16]
