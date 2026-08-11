"""SSE requests use the same authenticated Flask session as HTTP routes."""

import pytest
from werkzeug.security import generate_password_hash

from cl_app import create_app
from cl_app.services.sse_auth import is_request_authenticated


@pytest.fixture(scope="module")
def app():
    return create_app(test_config={
        "TESTING": True,
        "VALIDATE_WEB_SECURITY": False,
        "SCHEDULER_ENABLED": False,
        "WTF_CSRF_ENABLED": False,
    })


def test_no_cookie_denies(app):
    assert is_request_authenticated(app, None) is False


def test_valid_login_cookie_allows(app, monkeypatch):
    password = "sse-test-password"
    monkeypatch.setenv("CHANLUN_LOGIN_PWD", generate_password_hash(password))
    with app.test_client() as client:
        response = client.post(
            "/login",
            data={"password": password},
            follow_redirects=False,
        )
        set_cookie = response.headers.get("Set-Cookie") or ""
    cookie = set_cookie.split(";", 1)[0]

    assert response.status_code == 302
    assert cookie
    assert is_request_authenticated(app, cookie) is True
