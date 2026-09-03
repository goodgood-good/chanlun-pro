"""CachedStaticFileHandler（B1）。

为 charting_library / datafeeds 静态资源分配与文件名相符的缓存策略，
并按 Accept-Encoding 透明分发预压缩好的 .gz 文件。

  - 内容哈希文件：Cache-Control: public, max-age=31536000, immutable
      例如 library.668013b6b41ce2feaa5c.js，文件名随内容变化，永久缓存安全。
  - 固定文件名入口：Cache-Control: public, max-age=300, must-revalidate
      例如 charting_library.standalone.js、sameorigin.html、bundle.js，
      避免升级后浏览器继续使用旧入口。
  - Content-Encoding: gzip + Vary: Accept-Encoding
      仅当客户端支持 gzip 且兄弟 .gz 存在时启用
  - Content-Type 修正
      Tornado 父类对 .js.gz 会返回 application/gzip（RFC 6713）；
      我们透明发的是 .js，要把 mime 还回去
"""
import os
import re

from tornado.web import HTTPError, StaticFileHandler


_MIME_BY_EXT = {
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".map": "application/json; charset=utf-8",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


_CONTENT_HASH_RE = re.compile(r"(?:^|\.)[0-9a-f]{8,}(?=\.)", re.IGNORECASE)
_QVALUE_RE = re.compile(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)\Z")


def _is_content_hashed_path(path: str) -> bool:
    filename = os.path.basename(path.replace("\\", "/"))
    return _CONTENT_HASH_RE.search(filename) is not None


def _parse_accept_encoding(value: str) -> dict[str, float]:
    """Parse content-coding quality values conservatively.

    Duplicate codings use their lowest quality so a later duplicate cannot
    override an explicit exclusion. Invalid q-values make that coding
    unacceptable instead of silently enabling compression.
    """
    qualities: dict[str, float] = {}
    for item in value.split(","):
        parts = [part.strip() for part in item.split(";")]
        coding = parts[0].lower()
        if not coding:
            continue

        quality = 1.0
        for parameter in parts[1:]:
            name, separator, parameter_value = parameter.partition("=")
            if name.strip().lower() != "q":
                continue
            raw_quality = parameter_value.strip() if separator else ""
            quality = (
                float(raw_quality) if _QVALUE_RE.fullmatch(raw_quality) else 0.0
            )
            break

        previous = qualities.get(coding)
        qualities[coding] = quality if previous is None else min(previous, quality)
    return qualities


def _select_gzip(accept_encoding: str, gzip_available: bool) -> bool:
    if not accept_encoding:
        return False

    qualities = _parse_accept_encoding(accept_encoding)
    gzip_quality = qualities.get("gzip", qualities.get("*", 0.0))

    if "identity" in qualities:
        identity_quality = qualities["identity"]
    elif qualities.get("*") == 0.0:
        identity_quality = 0.0
    else:
        identity_quality = 1.0

    if gzip_available and gzip_quality > 0 and gzip_quality >= identity_quality:
        return True
    if identity_quality > 0:
        return False
    if gzip_available and gzip_quality > 0:
        return True
    raise HTTPError(406, reason="No acceptable content encoding")


def _has_fresh_gzip_variant(absolute_path: str) -> bool:
    gzip_path = absolute_path + ".gz"
    try:
        return (
            os.path.isfile(gzip_path)
            and os.stat(gzip_path).st_mtime_ns
            >= os.stat(absolute_path).st_mtime_ns
        )
    except OSError:
        return False


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
        """校验源文件后做 per-request 的 gzip 协商。

        validate_absolute_path 是实例方法，可访问 self.request。
        协商结果写入 self._serving_gz 供 set_extra_headers /
        get_content_type 使用。
        """
        absolute_path = super().validate_absolute_path(root, absolute_path)
        if absolute_path is None:
            return None

        accept = self.request.headers.get("Accept-Encoding", "")
        gzip_available = _has_fresh_gzip_variant(absolute_path)
        try:
            serving_gzip = _select_gzip(accept, gzip_available)
        except HTTPError as error:
            if error.status_code != 406:
                raise
            self._serving_gz = False
            self.set_status(406, reason=error.reason)
            self.set_header("Cache-Control", "no-store")
            self.set_header("Vary", "Accept-Encoding")
            self._set_security_headers()
            self.finish()
            return None

        if serving_gzip:
            self._serving_gz = True
            absolute_path = absolute_path + ".gz"
        else:
            self._serving_gz = False
        return absolute_path

    def _set_security_headers(self):
        self.set_header("X-Content-Type-Options", "nosniff")
        self.set_header("X-Frame-Options", "SAMEORIGIN")
        self.set_header("Referrer-Policy", "same-origin")
        self.set_header(
            "Permissions-Policy",
            "geolocation=(), microphone=(), camera=(), payment=()",
        )

    def set_extra_headers(self, path):
        # ``path`` 是相对 mount root 的捕获路径。目录中既有内容哈希 bundle，
        # 也有 standalone.js / sameorigin.html / bundle.js 这类固定文件名入口；
        # 必须逐文件判定，不能按整个子树统一发 immutable。
        p = path.replace("\\", "/").lower()
        if _is_content_hashed_path(p):
            self.set_header(
                "Cache-Control", "public, max-age=31536000, immutable"
            )
        else:
            self.set_header(
                "Cache-Control", "public, max-age=300, must-revalidate"
            )
        self.set_header("Vary", "Accept-Encoding")
        self._set_security_headers()
        if getattr(self, "_serving_gz", False):
            self.set_header("Content-Encoding", "gzip")

    def get_content_type(self):
        base = (
            self.absolute_path[:-3]
            if getattr(self, "_serving_gz", False)
            else self.absolute_path
        )
        ext = os.path.splitext(base)[1].lower()
        if ext in _MIME_BY_EXT:
            return _MIME_BY_EXT[ext]
        if getattr(self, "_serving_gz", False):
            return "application/octet-stream"
        return super().get_content_type()
