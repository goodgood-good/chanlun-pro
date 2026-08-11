import pytest

from cl_app import create_app


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        pytest.param(
            "/tv/1.1/drawings?client=test&user=1",
            {"status": "error", "message": "JSON body must be an object."},
            id="drawings",
        ),
    ],
)
def test_non_object_json_returns_structured_400(path, expected):
    app = create_app(
        test_config={
            "TESTING": True,
            "PROPAGATE_EXCEPTIONS": False,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    app.logger.disabled = True

    response = app.test_client().post(path, json=[{"market": "a"}])

    assert response.status_code == 400
    assert response.get_json() == expected


@pytest.mark.parametrize("payload", [None, {}, {"state": None}, {"state": []}])
def test_drawings_requires_an_object_state(payload):
    app = create_app(
        test_config={
            "TESTING": True,
            "LOGIN_DISABLED": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )
    try:
        response = app.test_client().post(
            "/tv/1.1/drawings?client=test&user=1",
            json=payload,
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 400
    assert response.get_json() == {
        "status": "error",
        "message": "state must be a JSON object.",
    }
