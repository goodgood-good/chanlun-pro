import gzip
import inspect
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


def test_embedded_strict_projection_drops_only_duplicate_audit_components():
    center = {
        "center_id": "center-1",
        "overlap_component_count": 5,
        "overlap_components": [{"unit_id": "u1", "start_time": 1}],
        "establishment_segment_ids": ["u1"],
        "establishment_segments": [{"unit_id": "u1"}],
        "middle_three_component_ids": ["u1"],
        "middle_three_components": [{"unit_id": "u1"}],
        "points": [{"time": 1, "price": 10.0}],
    }
    fields = {
        "strict_structure_mode": "replace",
        "strict_structure": {"levels": [{"centers": [center]}]},
    }

    projected = tv._project_strict_history_fields_for_embedded(
        fields,
        embedded=True,
    )
    projected_center = projected["strict_structure"]["levels"][0]["centers"][0]

    assert projected is not fields
    assert projected_center["overlap_component_count"] == 5
    assert projected_center["establishment_segment_ids"] == ["u1"]
    assert projected_center["middle_three_component_ids"] == ["u1"]
    assert projected_center["points"] == [{"time": 1, "price": 10.0}]
    assert "overlap_components" not in projected_center
    assert "establishment_segments" not in projected_center
    assert "middle_three_components" not in projected_center
    # The cache-backed authoritative object remains available to standalone
    # audit views and later requests.
    assert fields["strict_structure"]["levels"][0]["centers"][0] is center
    assert "overlap_components" in center


def test_standalone_strict_projection_preserves_full_audit_contract():
    fields = {
        "strict_structure_mode": "replace",
        "strict_structure": {"overlap_components": [{"unit_id": "u1"}]},
    }

    assert tv._project_strict_history_fields_for_embedded(
        fields,
        embedded=False,
    ) is fields


def test_embedded_indicator_projection_uses_lossless_delta_transport():
    source = {
        "macd_dif": [1.0],
        "macd_dea": [2.0],
        "macd_hist": [3.0],
        "macd_area": [4.0],
        "higher_macd_dif": [5.0],
        "higher_macd_dea": [6.0],
        "higher_macd_hist": [7.0],
    }

    embedded = tv._history_indicator_payload(
        source,
        embedded=True,
        delta_encoded=True,
    )
    legacy_embedded = tv._history_indicator_payload(source, embedded=True)
    standalone = tv._history_indicator_payload(source, embedded=False)

    assert "macd_area" not in embedded
    assert embedded["macd_delta_scale"] == 1_000_000
    assert embedded["macd_dif"] == [1_000_000]
    assert embedded["higher_macd_hist"] == [7_000_000]
    assert legacy_embedded["macd_dif"] == [1.0]
    assert "macd_delta_scale" not in legacy_embedded
    assert standalone["macd_area"] == [4.0]
    assert set(embedded) == (set(source) - {"macd_area"}) | {
        "macd_delta_scale"
    }


def test_embedded_numeric_delta_round_trips_nulls_and_negative_changes():
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


def test_history_floor_is_only_published_for_complete_embedded_snapshots():
    times = [1_700_000_000, 1_700_000_060]

    assert tv._history_floor_payload(
        times,
        embedded=True,
        first_data_request=True,
        complete_snapshot=True,
        countback=0,
    ) == {"history_floor": times[0]}

    for overrides in (
        {"embedded": False},
        {"first_data_request": False},
        {"complete_snapshot": False},
        {"countback": 329},
    ):
        options = {
            "embedded": True,
            "first_data_request": True,
            "complete_snapshot": True,
            "countback": 0,
            **overrides,
        }
        assert tv._history_floor_payload(times, **options) == {}

    assert tv._history_floor_payload(
        [],
        embedded=True,
        first_data_request=True,
        complete_snapshot=True,
        countback=0,
    ) == {}


def test_empty_or_stale_ranked_candidate_uses_local_qmt_history_only():
    source = inspect.getsource(tv.tv_history)

    assert '{"cache_empty", "cache_stale_snapshot"}' in source
    assert "candidate_local_history_ready(market, code, frequency)" in source
    assert 'kline_args["args"] = {"skip_download": True}' in source
