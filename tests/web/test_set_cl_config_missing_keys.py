"""Strict chart option form defaults and missing-key behavior."""

from cl_app.blueprints.options import _build_cl_config


def test_missing_intermediate_key_defaults_zero_no_keyerror() -> None:
    keys = [
        "config_use_type",
        "chart_show_fx",
        "chart_show_bi",
    ]
    form = {
        "config_use_type": "common",
        "chart_show_bi": "1",
    }

    cfg, err = _build_cl_config(form, keys)

    assert err is None
    assert cfg == {
        "config_use_type": "common",
        "chart_show_fx": "0",
        "chart_show_bi": "1",
    }


def test_unchecked_display_checkbox_defaults_zero() -> None:
    cfg, err = _build_cl_config(
        {},
        [
            "chart_show_bi",
            "chart_show_xd",
        ],
    )

    assert err is None
    assert cfg == {
        "chart_show_bi": "0",
        "chart_show_xd": "0",
    }


def test_valid_scalar_config_behavior_is_stable() -> None:
    cfg, err = _build_cl_config(
        {
            "config_use_type": "common",
            "chart_show_fx": "",
            "chart_show_xd": "1",
        },
        ["config_use_type", "chart_show_fx", "chart_show_xd"],
    )

    assert err is None
    assert cfg == {
        "config_use_type": "common",
        "chart_show_fx": "0",
        "chart_show_xd": "1",
    }
