"""Task4: SSE 鉴权 is_request_authenticated 复用 Flask-Login。"""
import pytest

from cl_app import create_app
from cl_app.services.sse_auth import is_request_authenticated


@pytest.fixture(scope="module")
def app():
    return create_app()


def test_pwd_set_no_cookie_denies(app, monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "LOGIN_PWD", "x", raising=False)
    assert is_request_authenticated(app, None) is False


def test_pwd_empty_no_cookie_allows(app, monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "LOGIN_PWD", "", raising=False)
    assert is_request_authenticated(app, None) is True


def test_valid_cookie_allows(app, monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "LOGIN_PWD", "", raising=False)
    with app.test_client() as c:
        r = c.get("/login", follow_redirects=False)
        set_cookie = r.headers.get("Set-Cookie") or ""
    cookie = set_cookie.split(";")[0] if set_cookie else ""
    assert is_request_authenticated(app, cookie) is True
