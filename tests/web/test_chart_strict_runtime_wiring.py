from __future__ import annotations

import pandas as pd

from cl_app.services import chart_compute


def test_serializer_builds_strict_runtime_from_exact_display_frame(
    monkeypatch,
) -> None:
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime([1_736_402_400], unit="s", utc=True),
            "open": [10.0],
            "high": [10.0],
            "low": [10.0],
            "close": [10.0],
            "volume": [100.0],
        }
    )
    strict_runtime = object()
    captured = {}

    def build(**kwargs):
        captured["build"] = kwargs
        return strict_runtime

    def serialize(processed_frame, config, **kwargs):
        captured["serialize"] = (processed_frame, config, kwargs)
        return {"strict_structure_mode": "replace"}

    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", build)
    monkeypatch.setattr(chart_compute, "cl_data_to_tv_chart", serialize)

    config = {"chart_show_fx": "1"}
    result = chart_compute.serialize_chart_data_with_strict_runtime(
        market="a",
        code="SH.600926",
        display_frequency="30m",
        display_klines=frame,
        chart_config=config,
    )

    assert result == {"strict_structure_mode": "replace"}
    assert captured["build"] == {
        "market": "a",
        "code": "SH.600926",
        "frequency": "30m",
        "frame": frame,
    }
    processed_frame, serialized_config, kwargs = captured["serialize"]
    assert processed_frame is frame
    assert serialized_config is config
    assert kwargs == {
        "market": "a",
        "code": "SH.600926",
        "frequency": "30m",
        "strict_runtime": strict_runtime,
    }
