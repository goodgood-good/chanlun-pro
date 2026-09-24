from __future__ import annotations


import pandas as pd



def _config() -> dict[str, object]:
    return {
        "chart_show_fx": "0",
        "chart_show_bi": "0",
        "chart_show_xd": "0",
    }




















def test_chart_serializer_drops_future_end_label_before_strict_runtime(
    monkeypatch,
) -> None:
    from cl_app.services import chart_compute

    completed_at = pd.Timestamp("2026-08-05 15:00:00", tz="Asia/Shanghai")
    future_at = pd.Timestamp("2099-01-01 09:30:00", tz="Asia/Shanghai")
    frame = pd.DataFrame(
        (
            {
                "date": completed_at,
                "open": 10.0,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000.0,
            },
            {
                "date": future_at,
                "open": 10.1,
                "high": 10.3,
                "low": 10.0,
                "close": 10.2,
                "volume": 10.0,
            },
        )
    )
    frame.attrs.update(
        structure_price_quantum="0.01",
        price_basis_revision="test-qmt",
    )
    runtime = object()
    calls: list[tuple[object, ...]] = []

    def build_strict(*, market, code, frequency, frame, last_bar_closed):
        assert last_bar_closed is True
        calls.append(("build", tuple(frame["date"]), dict(frame.attrs)))
        return runtime

    def serialize(frame, config, *, market, code, frequency, strict_runtime):
        calls.append(("serialize", tuple(frame["date"]), strict_runtime))
        return {"strict_structure_mode": "replace"}

    monkeypatch.setattr(chart_compute, "build_strict_chart_cd", build_strict)
    monkeypatch.setattr(chart_compute, "cl_data_to_tv_chart", serialize)

    result = chart_compute.serialize_chart_data_with_strict_runtime(
        market="a",
        code="SH.000001",
        display_frequency="1m",
        display_klines=frame,
        chart_config=_config(),
    )

    assert result == {
        "strict_structure_mode": "replace",
        "price_basis": {"structure_price_quantum": "0.01", "price_basis_revision": "test-qmt"},
    }
    assert calls[0] == (
        "build",
        (completed_at,),
        {
            "structure_price_quantum": "0.01",
            "price_basis_revision": "test-qmt",
        },
    )
    assert calls[1] == ("serialize", (completed_at,), runtime)
