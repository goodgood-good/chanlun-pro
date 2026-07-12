from cl_app import create_app
from cl_app.blueprints import tv as tv_module


def test_internal_history_failure_is_not_reported_as_no_data(monkeypatch):
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    monkeypatch.setattr(
        tv_module,
        "query_cl_chart_config",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    try:
        response = app.test_client().get(
            "/tv/history?symbol=a:SZ.000001&resolution=1&from=1&to=2"
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 503
    assert response.get_json() == {
        "s": "error",
        "errmsg": "History service is temporarily unavailable.",
    }
