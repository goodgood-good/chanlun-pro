"""CachedStaticFileHandler（B1）。

给 charting_library / datafeeds 这种 hash-named 静态资源发 immutable cache
头，并按 Accept-Encoding 透明分发预压缩好的 .gz 文件。

  - Cache-Control: public, max-age=31536000, immutable
      文件名都带 hash（library.668013b6b41ce2feaa5c.js），永久 cache 安全。
      **警告**：immutable 仅适用于 hash-named 子树。仅在
      charting_library/、datafeeds/ 等内容哈希命名的目录下挂载本
      handler；切勿挂载到 /static/ 根路径——那会把 index.html 等普通
      资源锁定长达一年。
  - Content-Encoding: gzip + Vary: Accept-Encoding
      仅当客户端支持 gzip 且兄弟 .gz 存在时启用
  - Content-Type 修正
      Tornado 父类对 .js.gz 会返回 application/gzip（RFC 6713）；
      我们透明发的是 .js，要把 mime 还回去
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
    @classmethod
    def get_absolute_path(cls, root, path):
        """直通父类实现。

        保留显式重写是为了说明为何不在此处做 gzip 协商：
        get_absolute_path 是 classmethod，没有 request 上下文，不能读
        Accept-Encoding 头；gzip 协商在 validate_absolute_path 中完成。
        """
        return super().get_absolute_path(root, path)

    def validate_absolute_path(self, root, absolute_path):
        """在父类校验前做 per-request 的 gzip 协商。

        validate_absolute_path 是实例方法，可访问 self.request。
        协商结果写入 self._serving_gz 供 set_extra_headers /
        get_content_type 使用。
        """
        accept = self.request.headers.get("Accept-Encoding", "")
        if "gzip" in accept and os.path.isfile(absolute_path + ".gz"):
            self._serving_gz = True
            absolute_path = absolute_path + ".gz"
        else:
            self._serving_gz = False
        return super().validate_absolute_path(root, absolute_path)

    def set_extra_headers(self, path):
        # 对前端关键文件(charts.js / bundle.js / *.html / 其它项目自带 JS)
        # 用「短 max-age + must-revalidate」,让浏览器每 5 分钟回源验证 Etag。
        # 这是因为这些文件不带 hash filename 且可能频繁更新,immutable + 1年
        # max-age 会让用户永久卡在旧版本(即便 index.html ?v=hash bust 也可能
        # 被某些缓存路径绕过)。
        # 对 TradingView charting_library 等第三方资源(带 hash filename 或
        # bundles/* 子路径)保留 immutable,享用最佳缓存性能。
        p = path.replace("\\", "/").lower()
        is_project_frontend = (
            p.endswith("charts.js") or p.endswith("bundle.js") or
            p.endswith(".html") or
            ("/js/" in p and "tv_indicators" not in p and "charting_library" not in p)
        )
        if is_project_frontend:
            self.set_header(
                "Cache-Control", "public, max-age=300, must-revalidate"
            )
        else:
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
