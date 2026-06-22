"""Task7: app.py 路由集成 — build_routes 能集成进 Tornado Application。"""
import tornado.web

from cl_app import create_app
from cl_app.handlers.sse_stream import SseStreamHandler, build_routes


def test_routes_integrate_into_application(monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "ENABLE_SSE_PUSH", True, raising=False)
    app = create_app()
    routes = [
        (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": "."}),
        *build_routes(app, pool=None),
        (r".*", tornado.web.RequestHandler),
    ]
    application = tornado.web.Application(routes)
    assert application is not None
    matched = [r for r in routes if "/tv/stream" in str(r[0])]
    assert len(matched) == 1
    assert matched[0][1] is SseStreamHandler


def test_routes_empty_when_flag_off(monkeypatch):
    from chanlun import config
    monkeypatch.setattr(config, "ENABLE_SSE_PUSH", False, raising=False)
    app = create_app()
    assert build_routes(app, pool=None) == []
