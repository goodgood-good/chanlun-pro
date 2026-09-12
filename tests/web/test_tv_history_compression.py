import gzip
import json

from flask import Flask
import pytest

from web.chanlun_chart.cl_app.blueprints import tv


def _large_response(app: Flask):
    return app.json.response({"s": "ok", "payload": "x" * 100_000})


def test_large_history_json_is_gzipped_and_cached_for_identical_payloads():
    app = Flask(__name__)
    with tv._tv_history_gzip_cache_lock:
        tv._tv_history_gzip_cache.clear()

    with app.test_request_context(
        "/tv/history",
        headers={"Accept-Encoding": "gzip, deflate"},
    ):
        first = tv._gzip_tv_history_response(_large_response(app))
        assert first.headers["Content-Encoding"] == "gzip"
        assert first.headers["X-Chart-Compression"] == "miss"
        assert "Accept-Encoding" in first.headers["Vary"]
        decoded = json.loads(gzip.decompress(first.get_data()))
        assert len(decoded["payload"]) == 100_000

        second = tv._gzip_tv_history_response(_large_response(app))
        assert second.headers["X-Chart-Compression"] == "hit"
        assert second.get_data() == first.get_data()


def test_history_compression_respects_identity_and_small_responses():
    app = Flask(__name__)
    with app.test_request_context(
        "/tv/history",
        headers={"Accept-Encoding": "identity"},
    ):
        response = tv._gzip_tv_history_response(_large_response(app))
        assert response.headers.get("Content-Encoding") is None
        assert len(response.get_data()) > tv._TV_HISTORY_GZIP_MIN_BYTES

    with app.test_request_context(
        "/tv/history",
        headers={"Accept-Encoding": "gzip"},
    ):
        response = tv._gzip_tv_history_response(app.json.response({"s": "ok"}))
        assert response.headers.get("Content-Encoding") is None
        assert response.get_json() == {"s": "ok"}


@pytest.mark.skipif(tv._brotli is None, reason="optional brotli codec unavailable")
def test_history_compression_prefers_brotli_and_caches_by_encoding():
    app = Flask(__name__)
    with tv._tv_history_gzip_cache_lock:
        tv._tv_history_gzip_cache.clear()

    with app.test_request_context(
        "/tv/history",
        headers={"Accept-Encoding": "br, gzip"},
    ):
        first = tv._gzip_tv_history_response(_large_response(app))
        assert first.headers["Content-Encoding"] == "br"
        assert first.headers["X-Chart-Compression"] == "miss"
        assert json.loads(tv._brotli.decompress(first.get_data()))["s"] == "ok"

        second = tv._gzip_tv_history_response(_large_response(app))
        assert second.headers["Content-Encoding"] == "br"
        assert second.headers["X-Chart-Compression"] == "hit"
        assert second.get_data() == first.get_data()


def test_structure_patch_indicator_projection_uses_lossless_delta_transport():
    source = {
        "macd_dif": [1.0],
        "macd_dea": [2.0],
        "macd_hist": [3.0],
        "macd_area": [4.0],
        "higher_macd_dif": [5.0],
        "higher_macd_dea": [6.0],
        "higher_macd_hist": [7.0],
    }

    patch = tv._history_indicator_payload(
        source,
        delta_encoded=True,
    )
    standalone = tv._history_indicator_payload(source)

    assert "macd_area" not in patch
    assert patch["macd_delta_scale"] == 1_000_000
    assert patch["macd_dif"] == [1_000_000]
    assert patch["higher_macd_hist"] == [7_000_000]
    assert "macd_delta_scale" not in standalone
    assert standalone["macd_area"] == [4.0]
    assert set(patch) == (set(source) - {"macd_area"}) | {
        "macd_delta_scale"
    }


def test_numeric_delta_round_trips_nulls_and_negative_changes():
    values = [None, 0.123456, 0.12, -0.1, -0.1, None, 1.0]

    encoded = tv._delta_encode_numeric_column(values, scale=1_000_000)
    assert encoded is not None

    previous = 0
    decoded = []
    for delta in encoded:
        if delta is None:
            decoded.append(None)
            continue
        previous += delta
        decoded.append(previous / 1_000_000)

    assert decoded == values
    assert tv._history_time_payload(
        [1_700_000_000, 1_700_000_060, 1_700_000_120],
        delta_encoded=True,
    ) == {
        "t": [1_700_000_000, 60, 60],
        "time_delta": True,
    }


def test_history_floor_is_only_published_for_complete_atomic_snapshots():
    times = [1_700_000_000, 1_700_000_060]

    assert tv._history_floor_payload(
        times,
        atomic_initial=True,
        first_data_request=True,
        complete_snapshot=True,
        countback=0,
    ) == {"history_floor": times[0]}

    for overrides in (
        {"atomic_initial": False},
        {"first_data_request": False},
        {"complete_snapshot": False},
        {"countback": 329},
    ):
        options = {
            "atomic_initial": True,
            "first_data_request": True,
            "complete_snapshot": True,
            "countback": 0,
            **overrides,
        }
        assert tv._history_floor_payload(times, **options) == {}

    assert tv._history_floor_payload(
        [],
        atomic_initial=True,
        first_data_request=True,
        complete_snapshot=True,
        countback=0,
    ) == {}
