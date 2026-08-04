import json

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from app import NativeHealthHandler


class TestNativeHealthHandler(AsyncHTTPTestCase):
    def get_app(self):
        class _FlaskApp:
            extensions = {
                "health_snapshot": staticmethod(
                    lambda kind, market, forward_session: (
                        {
                            "status": "not_ready",
                            "kind": kind,
                            "market": market,
                            "forward_session": forward_session,
                        },
                        503,
                    )
                )
            }

        return Application(
            [(r"/readyz", NativeHealthHandler, {"flask_app": _FlaskApp()})]
        )

    def test_health_response_does_not_use_wsgi(self):
        response = self.fetch("/readyz?market=a")

        assert response.code == 503
        assert json.loads(response.body) == {
            "status": "not_ready",
            "kind": "readyz",
            "market": "a",
            "forward_session": None,
        }

    def test_forward_session_reaches_the_shared_health_snapshot(self):
        response = self.fetch(
            "/readyz?market=a&forward_session=2026-07-30"
        )

        assert response.code == 503
        assert json.loads(response.body)["forward_session"] == "2026-07-30"
