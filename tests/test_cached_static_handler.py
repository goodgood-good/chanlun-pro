"""CachedStaticFileHandler 集成测试 (B1)。

通过 tornado.testing.AsyncHTTPTestCase 启一个 mini Application，验证：
- 不带 Accept-Encoding：返回原文件 + immutable cache header
- 带 gzip：发 .gz 文件 + Content-Encoding: gzip + Vary: Accept-Encoding
- .gz 不存在时仍能正常发原文件
- mime type：透明发 .gz 时 Content-Type 仍是 application/javascript
"""
import gzip
import os
import shutil
import tempfile

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from cl_app.handlers.cached_static import CachedStaticFileHandler


class CachedStaticHandlerTest(AsyncHTTPTestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cached_static_test_")
        self.js_text = "// hello world\n" * 200
        self.js_path = os.path.join(self.tmpdir, "lib.js")
        with open(self.js_path, "w", encoding="utf-8") as f:
            f.write(self.js_text)
        with gzip.open(self.js_path + ".gz", "wb") as f:
            f.write(self.js_text.encode("utf-8"))
        self.css_path = os.path.join(self.tmpdir, "site.css")
        with open(self.css_path, "w", encoding="utf-8") as f:
            f.write(".x{}")
        super().setUp()

    def tearDown(self):
        super().tearDown()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def get_app(self):
        return Application(
            [(r"/(.*)", CachedStaticFileHandler, {"path": self.tmpdir})]
        )

    def test_no_accept_encoding_returns_plain(self):
        resp = self.fetch("/lib.js", method="GET")
        assert resp.code == 200
        assert resp.body.decode("utf-8") == self.js_text
        assert "gzip" not in resp.headers.get("Content-Encoding", "")
        cache = resp.headers.get("Cache-Control", "")
        assert "immutable" in cache
        assert "max-age=31536000" in cache

    def test_gzip_accept_returns_gz(self):
        resp = self.fetch(
            "/lib.js",
            method="GET",
            headers={"Accept-Encoding": "gzip"},
            decompress_response=False,
        )
        assert resp.code == 200
        assert resp.headers.get("Content-Encoding") == "gzip"
        assert "Accept-Encoding" in resp.headers.get("Vary", "")
        assert gzip.decompress(resp.body).decode("utf-8") == self.js_text
        ct = resp.headers.get("Content-Type", "")
        assert "javascript" in ct.lower()

    def test_gzip_falls_back_when_gz_missing(self):
        resp = self.fetch(
            "/site.css",
            method="GET",
            headers={"Accept-Encoding": "gzip"},
            decompress_response=False,
        )
        assert resp.code == 200
        assert resp.headers.get("Content-Encoding", "") == ""
        assert resp.body == b".x{}"

    def test_404_for_missing_file(self):
        resp = self.fetch("/nope.js", method="GET")
        assert resp.code == 404
