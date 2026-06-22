"""Task6: SSE handler 路由构建(flag) + 鉴权拒绝。"""
import tornado.testing
import tornado.web

from cl_app import create_app
from cl_app.handlers.sse_stream import SseStreamHandler, build_routes


def test_build_routes_flag_on(monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "ENABLE_SSE_PUSH", True, raising=False)
    app = create_app()
    routes = build_routes(app)
    assert any("/tv/stream" in str(r[0]) for r in routes)


def test_build_routes_flag_off(monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "ENABLE_SSE_PUSH", False, raising=False)
    app = create_app()
    assert build_routes(app) == []


class SseAuthTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        self.flask_app = create_app()
        return tornado.web.Application(
            [(r"/tv/stream", SseStreamHandler,
              {"flask_app": self.flask_app, "pool": None})]
        )

    def test_unauthorized_when_pwd_set(self):
        from chanlun import config
        old = config.LOGIN_PWD
        config.LOGIN_PWD = "x"  # 设密码 + 无 cookie → 应 401
        try:
            resp = self.fetch("/tv/stream?symbol=a:SH.000001&resolution=1")
            self.assertEqual(resp.code, 401)
        finally:
            config.LOGIN_PWD = old
