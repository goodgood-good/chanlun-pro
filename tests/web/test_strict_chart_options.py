from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from flask import Flask

from chanlun.cl_utils import chart_config
from chanlun.core.cl import CL
from cl_app.blueprints import options


UNSUPPORTED_STRUCTURE_KEYS = {
    "zs_bi_type",
    "zs_xd_type",
    "zs_qj",
    "zs_cd",
    "zs_wzgx",
    "chart_show_bi_zs",
    "chart_show_xd_zs",
    "chart_show_bi_mmd",
    "chart_show_xd_mmd",
    "chart_show_bi_bc",
    "chart_show_xd_bc",
    "chart_show_xd_zslx",
}

REMOVED_STRICT_DISPLAY_KEYS = {
    "chart_show_stroke_center_observations": "1",
    "chart_show_strict_centers": "1",
    "chart_show_strict_center_projections": "1",
    "chart_show_strict_trends": "0",
    "chart_show_strict_confirmed_points": "1",
    "chart_show_strict_approaching_points": "1",
}


def _app() -> Flask:
    app = Flask(__name__)
    app.config.update(TESTING=True, LOGIN_DISABLED=True, SECRET_KEY="test")
    app.register_blueprint(options.options_bp)
    return app


def test_chart_defaults_exclude_unsupported_and_browser_only_options(monkeypatch) -> None:
    chart_config._cache_invalidate("a")
    monkeypatch.setattr(chart_config.db, "cache_get", lambda _key: None)

    result = chart_config.query_cl_chart_config("a", "SH.600519")

    assert set(REMOVED_STRICT_DISPLAY_KEYS).isdisjoint(result)
    assert UNSUPPORTED_STRUCTURE_KEYS.isdisjoint(result)


def test_chart_preferences_do_not_control_the_core_calculator(monkeypatch) -> None:
    chart_config._cache_invalidate("a")
    monkeypatch.setattr(chart_config.db, "cache_get", lambda _key: None)

    result = chart_config.query_cl_chart_config("a", "SH.600519")
    cd = CL("SH.600519", "5m", market="a")

    assert "bi_type" not in result
    assert cd.get_config()["stroke_rule"] == "strict-cl-k-distance"


def test_options_and_file_cache_share_one_persisted_key_contract() -> None:
    assert tuple(options.CL_CHART_CONFIG_FORM_KEYS) == tuple(
        chart_config.CL_CHART_CONFIG_PERSIST_KEYS
    )
    assert set(REMOVED_STRICT_DISPLAY_KEYS).isdisjoint(
        chart_config.CL_CHART_CONFIG_PERSIST_KEYS
    )
    assert UNSUPPORTED_STRUCTURE_KEYS.isdisjoint(
        chart_config.CL_CHART_CONFIG_PERSIST_KEYS
    )
    assert set(REMOVED_STRICT_DISPLAY_KEYS).isdisjoint(
        chart_config.CL_COMPUTE_CACHE_CONFIG_KEYS
    )
    assert UNSUPPORTED_STRUCTURE_KEYS.isdisjoint(
        chart_config.CL_COMPUTE_CACHE_CONFIG_KEYS
    )


def test_unsupported_structure_option_submission_is_rejected_with_400(monkeypatch) -> None:
    writes: list[dict] = []
    monkeypatch.setattr(
        options,
        "set_cl_chart_config",
        lambda _market, _code, config: writes.append(config) or True,
    )
    client = _app().test_client()

    response = client.post(
        "/set_cl_config",
        data={
            "market": "a",
            "code": "SH.600519",
            "is_del": "false",
            "config_use_type": "common",
            "zs_qj": "zs_qj_dd",
        },
    )

    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "msg": "包含不再支持的配置项: zs_qj",
    }
    assert writes == []


def test_removed_strict_display_submission_is_rejected_with_400(
    monkeypatch,
) -> None:
    writes: list[dict] = []
    monkeypatch.setattr(
        options,
        "set_cl_chart_config",
        lambda _market, _code, config: writes.append(config) or True,
    )
    client = _app().test_client()

    response = client.post(
        "/set_cl_config",
        data={
            "market": "a",
            "code": "SH.600519",
            "is_del": "false",
            "config_use_type": "common",
            "chart_show_strict_centers": "1",
            "chart_show_strict_trends": "",
        },
    )

    assert response.status_code == 400
    assert "chart_show_strict_centers" in response.get_json()["msg"]
    assert "chart_show_strict_trends" in response.get_json()["msg"]
    assert writes == []


def test_options_template_excludes_browser_only_strict_structure_controls() -> None:
    source = Path(
        "web/chanlun_chart/cl_app/templates/options.html"
    ).read_text(encoding="utf-8")

    for key in REMOVED_STRICT_DISPLAY_KEYS:
        assert f'name="{key}"' not in source
    for key in UNSUPPORTED_STRUCTURE_KEYS:
        assert f'name="{key}"' not in source


def test_options_form_fields_match_the_shared_persistence_contract() -> None:
    class FormFieldParser(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.names: set[str] = set()

        def handle_starttag(
            self, tag: str, attrs: list[tuple[str, str | None]]
        ) -> None:
            if tag not in {"input", "select"}:
                return
            attributes = dict(attrs)
            if attributes.get("name"):
                self.names.add(attributes["name"])

    source = Path(
        "web/chanlun_chart/cl_app/templates/options.html"
    ).read_text(encoding="utf-8")
    parser = FormFieldParser()
    parser.feed(source)

    assert parser.names - {"is_del"} == set(chart_config.CL_CHART_CONFIG_PERSIST_KEYS) | {
        "market",
        "code",
    }
