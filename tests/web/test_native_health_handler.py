import json

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from app import NativeHealthHandler


class TestNativeHealthHandler(AsyncHTTPTestCase):
    def get_app(self):
        class _FlaskApp:
            extensions = {
                "health_snapshot": staticmethod(
                    lambda kind, market: (
                        {
                            "status": "not_ready",
                            "kind": kind,
                            "market": market,
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
        }
