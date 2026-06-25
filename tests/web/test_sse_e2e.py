"""Task12: SSE 端到端流测试 — 鉴权通过→订阅→收到首帧推送。

用 AsyncHTTPTestCase(随机端口, 不碰生产 9900) + mock 掉真实数据源重算,
验证 Tornado SSE 运行时: async get 保持长连接、首次立即推、SSE 帧格式。
"""
import unittest.mock as mock

import tornado.testing
import tornado.web

from cl_app import create_app
from cl_app.handlers import sse_stream

# 含笔的假缠论数据, 首次推送(decide_push prev=None→True)应被写出。
FAKE_CD = {
    "t": [1, 2, 3], "c": [1.0, 1.1, 1.2],
    "bis": [{"points": [{"time": 1, "price": 1.0}, {"time": 3, "price": 1.2}]}],
}


class SseFlowTest(tornado.testing.AsyncHTTPTestCase):
    def get_app(self):
        from chanlun import config
        config.LOGIN_PWD = ""  # 免密模式(本地默认即空)
        self.flask_app = create_app()
        return tornado.web.Application(
            [(r"/tv/stream", sse_stream.SseStreamHandler,
              {"flask_app": self.flask_app, "pool": None})]
        )

    def _login_cookie(self):
        with self.flask_app.test_client() as c:
            r = c.get("/login", follow_redirects=False)
        sc = r.headers.get("Set-Cookie") or ""
        return sc.split(";")[0]

    @tornado.testing.gen_test(timeout=15)
    async def test_first_frame_pushed(self):
        cookie = self._login_cookie()
        chunks = []
        with mock.patch.object(
            sse_stream, "recompute_chart_data", return_value=FAKE_CD
        ):
            try:
                await self.http_client.fetch(
                    self.get_url("/tv/stream?symbol=a:SH.000001&resolution=1"),
                    headers={"Cookie": cookie},
                    streaming_callback=lambda d: chunks.append(d),
                    request_timeout=3,
                )
            except Exception:
                pass  # 长连接: request_timeout 触发是预期, 我们只读首帧
        body = b"".join(chunks).decode("utf-8", "ignore")
        assert "event: chanlun" in body
        assert "\"bis\"" in body
        # SSE 帧必须带 full_snapshot 标记:前端据此整体替换形态列表,杜绝"多个未完成笔"等
        # 累积。缺了它前端会退回"只增不删"合并 → 累积复发,故钉死此契约。
        assert "full_snapshot" in body
