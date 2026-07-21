"""Strict chart option form defaults and missing-key behavior."""

from cl_app.blueprints.options import _build_cl_config


def test_missing_intermediate_key_defaults_zero_no_keyerror() -> None:
    keys = [
        "config_use_type",
        "kline_qk",
        "chart_show_strict_centers",
    ]
    form = {
        "config_use_type": "common",
        "chart_show_strict_centers": "1",
    }

    cfg, err = _build_cl_config(form, keys)

    assert err is None
    assert cfg == {
        "config_use_type": "common",
        "kline_qk": "0",
        "chart_show_strict_centers": "1",
    }


def test_unchecked_strict_display_checkbox_defaults_zero() -> None:
    cfg, err = _build_cl_config(
        {},
        [
            "chart_show_stroke_center_observations",
            "chart_show_strict_approaching_points",
        ],
    )

    assert err is None
    assert cfg == {
        "chart_show_stroke_center_observations": "0",
        "chart_show_strict_approaching_points": "0",
    }


def test_valid_scalar_config_behavior_is_stable() -> None:
    cfg, err = _build_cl_config(
        {
            "config_use_type": "common",
            "fx_qy": "",
            "idx_macd_fast": "12",
        },
        ["config_use_type", "fx_qy", "idx_macd_fast"],
    )

    assert err is None
    assert cfg == {
        "config_use_type": "common",
        "fx_qy": "0",
        "idx_macd_fast": "12",
    }
