from __future__ import annotations

from copy import deepcopy

from chanlun.core.macd_htf import interpolate_causal_htf_for_chart


def test_chart_interpolation_uses_only_bucket_end_anchors() -> None:
    causal = {
        "dif": [10.0, -5.0, 2.0, 100.0, -100.0, 8.0, 50.0, 4.0],
        "dea": [9.0, -4.0, 1.0, 90.0, -90.0, 4.0, 40.0, 2.0],
        "hist": [2.0, -2.0, 2.0, 20.0, -20.0, 8.0, 20.0, 4.0],
        "bucket_keys": [0, 0, 0, 1, 1, 1, 2, 2],
    }
    original = deepcopy(causal)

    chart = interpolate_causal_htf_for_chart(causal)

    assert chart is not None
    assert chart["dif"] == [2.0, 2.0, 2.0, 4.0, 6.0, 8.0, 6.0, 4.0]
    assert chart["dea"] == [1.0, 1.0, 1.0, 2.0, 3.0, 4.0, 3.0, 2.0]
    assert chart["hist"] == [2.0, 2.0, 2.0, 4.0, 6.0, 8.0, 6.0, 4.0]
    assert causal == original


def test_chart_interpolation_rejects_missing_bucket_alignment() -> None:
    assert (
        interpolate_causal_htf_for_chart(
            {
                "dif": [1.0, 2.0],
                "dea": [1.0, 2.0],
                "hist": [0.0, 0.0],
                "bucket_keys": [0],
            }
        )
        is None
    )
