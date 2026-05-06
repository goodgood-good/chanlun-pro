"""CachedStaticFileHandler（B1）。

给 charting_library / datafeeds 这种 hash-named 静态资源发 immutable cache
头，并按 Accept-Encoding 透明分发预压缩好的 .gz 文件。

  - Cache-Control: public, max-age=31536000, immutable
      文件名都带 hash（library.668013b6b41ce2feaa5c.js），永久 cache 安全
  - Content-Encoding: gzip + Vary: Accept-Encoding
      仅当客户端支持 gzip 且兄弟 .gz 存在时启用
  - Content-Type 修正
      Tornado 父类对 .js.gz 会返回 application/x-gzip；我们透明发的是 .js，
      要把 mime 还回去
"""
import os

from tornado.web import StaticFileHandler


_MIME_BY_EXT = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".map": "application/json; charset=utf-8",
}


class CachedStaticFileHandler(StaticFileHandler):
    def get_absolute_path(self, root, path):
        full = super().get_absolute_path(root, path)
        accept = self.request.headers.get("Accept-Encoding", "")
        if "gzip" in accept and os.path.isfile(full + ".gz"):
            self._serving_gz = True
            return full + ".gz"
        self._serving_gz = False
        return full

    def set_extra_headers(self, path):
        self.set_header(
            "Cache-Control", "public, max-age=31536000, immutable"
        )
        if getattr(self, "_serving_gz", False):
            self.set_header("Content-Encoding", "gzip")
            self.set_header("Vary", "Accept-Encoding")

    def get_content_type(self):
        if getattr(self, "_serving_gz", False):
            base = self.absolute_path[:-3]  # 去掉 .gz
            ext = os.path.splitext(base)[1].lower()
            return _MIME_BY_EXT.get(ext, "application/octet-stream")
        return super().get_content_type()
