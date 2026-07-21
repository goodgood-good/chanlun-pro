from __future__ import annotations

import pandas as pd

from cl_app.services import chart_compute


def test_serializer_bridge_builds_strict_runtime_from_exact_display_frame(
    monkeypatch,
) -> None:
    frame = pd.DataFrame({"close": [10.0]})
    legacy_cd = object()
    strict_runtime = object()
    captured = {}

    def build(**kwargs):
        captured["build"] = kwargs
        return strict_runtime

    def serialize(cd, config, *, strict_runtime):
        captured["serialize"] = (cd, config, strict_runtime)
        return {"strict_structure_mode": "replace"}

    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", build)
    monkeypatch.setattr(chart_compute, "cl_data_to_tv_chart", serialize)

    result = chart_compute.serialize_chart_data_with_strict_runtime(
        market="a",
        code="SH.600926",
        display_frequency="30m",
        display_klines=frame,
        legacy_cd=legacy_cd,
        legacy_config={"chart_show_fx": "1"},
    )

    assert result == {"strict_structure_mode": "replace"}
    assert captured["build"] == {
        "market": "a",
        "code": "SH.600926",
        "frequency": "30m",
        "frame": frame,
    }
    assert captured["serialize"] == (
        legacy_cd,
        {"chart_show_fx": "1"},
        strict_runtime,
    )
