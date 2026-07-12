import pytest

from cl_app import create_app


def _make_app(monkeypatch, password_ref):
    monkeypatch.setattr("cl_app.get_login_password", lambda: password_ref[0])
    return create_app(
        test_config={
            "TESTING": True,
            "VALIDATE_WEB_SECURITY": False,
            "SCHEDULER_ENABLED": False,
            "WTF_CSRF_ENABLED": False,
        }
    )


def test_post_logout_revokes_the_current_session(monkeypatch):
    password = ["first-password"]
    app = _make_app(monkeypatch, password)
    client = app.test_client()

    assert client.post("/login", data={"password": password[0]}).status_code == 302
    assert client.get("/").status_code == 200

    response = client.post("/logout")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    assert client.get("/").status_code == 302
    app.extensions["shutdown_scheduler"]()


def test_password_rotation_invalidates_existing_session(monkeypatch):
    password = ["first-password"]
    app = _make_app(monkeypatch, password)
    client = app.test_client()

    assert client.post("/login", data={"password": password[0]}).status_code == 302
    assert client.get("/").status_code == 200

    password[0] = "rotated-password"
    response = client.get("/")

    assert response.status_code == 302
    assert "/login" in response.headers["Location"]
    app.extensions["shutdown_scheduler"]()


def test_login_route_blocks_repeated_password_failures(monkeypatch):
    password = ["first-password"]
    app = _make_app(monkeypatch, password)
    client = app.test_client()
    limiter = app.extensions["login_rate_limiter"]

    try:
        for _ in range(limiter.max_failures):
            response = client.post("/login", data={"password": "wrong"})
            assert response.status_code == 200

        blocked = client.post("/login", data={"password": "wrong"})
        assert blocked.status_code == 429
    finally:
        app.extensions["shutdown_scheduler"]()


def test_successful_login_clears_failed_attempt_state(monkeypatch):
    password = ["first-password"]
    app = _make_app(monkeypatch, password)
    client = app.test_client()
    limiter = app.extensions["login_rate_limiter"]

    try:
        assert client.post("/login", data={"password": "wrong"}).status_code == 200
        assert limiter.tracked_keys() == 1

        response = client.post("/login", data={"password": password[0]})
        assert response.status_code == 302
        assert limiter.tracked_keys() == 0
    finally:
        app.extensions["shutdown_scheduler"]()


def test_api_request_returns_json_401_after_authentication_expires(monkeypatch):
    password = ["first-password"]
    app = _make_app(monkeypatch, password)
    client = app.test_client()

    try:
        response = client.post(
            "/ticks",
            data={"market": "a", "codes": "[]"},
            follow_redirects=False,
        )
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 401
    assert response.get_json() == {
        "ok": False,
        "s": "error",
        "code": "authentication_required",
        "errmsg": "Authentication required.",
    }


@pytest.mark.parametrize("path", ["/alert_del/1", "/xuangu/task_add"])
def test_ajax_post_endpoints_share_json_authentication_contract(
    monkeypatch, path
):
    password = ["first-password"]
    app = _make_app(monkeypatch, password)

    try:
        response = app.test_client().post(path, follow_redirects=False)
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 401
    assert response.get_json() == {
        "ok": False,
        "s": "error",
        "code": "authentication_required",
        "errmsg": "Authentication required.",
    }


def test_authenticated_client_can_refresh_csrf_token(monkeypatch):
    password = ["first-password"]
    app = _make_app(monkeypatch, password)
    client = app.test_client()

    try:
        assert client.post("/login", data={"password": password[0]}).status_code == 302
        response = client.get("/api/session")
    finally:
        app.extensions["shutdown_scheduler"]()

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ok"] is True
    assert isinstance(payload["csrf_token"], str)
    assert payload["csrf_token"]
