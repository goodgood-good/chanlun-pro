import gzip
import os
import pathlib
import tempfile

from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from cl_app.handlers.cached_static import CachedStaticFileHandler


ROOT = pathlib.Path(__file__).resolve().parents[2]
CHARTING_ROOT = ROOT / "web/chanlun_chart/cl_app/static/charting_library"
ENTRYPOINT = "charting_library.standalone.js"


class TestCachedStaticHttpContract(AsyncHTTPTestCase):
    def setUp(self):
        # The repository deliberately does not version generated ``.gz``
        # siblings. Build both representations inside the test so its result
        # cannot depend on whether app.py happened to precompress assets first.
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.static_root = pathlib.Path(self._temporary_directory.name)
        source = CHARTING_ROOT / ENTRYPOINT
        target = self.static_root / ENTRYPOINT
        target.write_bytes(source.read_bytes())
        with gzip.open(self.static_root / f"{ENTRYPOINT}.gz", "wb") as stream:
            stream.write(target.read_bytes())
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            self._temporary_directory.cleanup()

    def get_app(self):
        return Application(
            [
                (
                    r"/(.*)",
                    CachedStaticFileHandler,
                    {"path": str(self.static_root)},
                )
            ]
        )

    def fetch_raw(self, *, accept_encoding=None, headers=None):
        request_headers = dict(headers or {})
        if accept_encoding is not None:
            request_headers["Accept-Encoding"] = accept_encoding
        return self.fetch(
            f"/{ENTRYPOINT}",
            method="HEAD",
            headers=request_headers,
            decompress_response=False,
        )

    def test_gzip_q_zero_uses_identity_and_identity_varies_by_encoding(self):
        response = self.fetch_raw(accept_encoding="br, gzip;q=0")

        assert response.code == 200
        assert "Content-Encoding" not in response.headers
        assert response.headers["Vary"] == "Accept-Encoding"

    def test_wildcard_allows_gzip_but_explicit_zero_overrides_it(self):
        wildcard = self.fetch_raw(accept_encoding="br;q=0.2, *;q=1")
        excluded = self.fetch_raw(accept_encoding="*;q=1, gzip;q=0")

        assert wildcard.code == 200
        assert wildcard.headers["Content-Encoding"] == "gzip"
        assert excluded.code == 200
        assert "Content-Encoding" not in excluded.headers

    def test_encoding_quality_prefers_identity_when_it_has_higher_q(self):
        response = self.fetch_raw(
            accept_encoding="gzip;q=0.4, identity;q=0.8"
        )

        assert response.code == 200
        assert "Content-Encoding" not in response.headers

    def test_no_acceptable_representation_returns_406(self):
        response = self.fetch_raw(
            accept_encoding="gzip;q=0, identity;q=0, *;q=0"
        )

        assert response.code == 406
        assert response.headers["Vary"] == "Accept-Encoding"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "SAMEORIGIN"

    def test_missing_resource_remains_404_for_unacceptable_encoding(self):
        response = self.fetch(
            "/missing.js",
            method="HEAD",
            headers={
                "Accept-Encoding": "gzip;q=0, identity;q=0, *;q=0"
            },
            decompress_response=False,
        )

        assert response.code == 404

    def test_etag_and_304_are_representation_specific(self):
        identity = self.fetch_raw(accept_encoding="identity")
        compressed = self.fetch_raw(accept_encoding="gzip")

        assert identity.code == 200
        assert compressed.code == 200
        assert identity.headers["ETag"] != compressed.headers["ETag"]

        identity_not_modified = self.fetch_raw(
            accept_encoding="identity",
            headers={"If-None-Match": identity.headers["ETag"]},
        )
        compressed_not_modified = self.fetch_raw(
            accept_encoding="gzip",
            headers={"If-None-Match": compressed.headers["ETag"]},
        )

        assert identity_not_modified.code == 304
        assert compressed_not_modified.code == 304
        assert identity_not_modified.headers["ETag"] == identity.headers["ETag"]
        assert compressed_not_modified.headers["ETag"] == compressed.headers["ETag"]
        assert "Content-Encoding" not in identity_not_modified.headers
        assert compressed_not_modified.headers["Vary"] == "Accept-Encoding"

    def test_security_and_cache_headers_survive_identity_gzip_and_304(self):
        identity = self.fetch_raw(accept_encoding="identity")
        compressed = self.fetch_raw(accept_encoding="gzip")
        not_modified = self.fetch_raw(
            accept_encoding="gzip",
            headers={"If-None-Match": compressed.headers["ETag"]},
        )

        for response in (identity, compressed, not_modified):
            assert response.headers["Vary"] == "Accept-Encoding"
            assert response.headers["Cache-Control"] == (
                "public, max-age=300, must-revalidate"
            )
            assert response.headers["X-Content-Type-Options"] == "nosniff"
            assert response.headers["X-Frame-Options"] == "SAMEORIGIN"
            assert response.headers["Referrer-Policy"] == "same-origin"
            assert response.headers["Permissions-Policy"] == (
                "geolocation=(), microphone=(), camera=(), payment=()"
            )


class TestCachedStaticVariantFreshness(AsyncHTTPTestCase):
    def setUp(self):
        self._temporary_directory = tempfile.TemporaryDirectory()
        self.static_root = pathlib.Path(self._temporary_directory.name)
        self.source = self.static_root / "entry.js"
        self.compressed = self.static_root / "entry.js.gz"
        self.source.write_bytes(b"window.current = true;\n")
        with gzip.open(self.compressed, "wb") as stream:
            stream.write(b"window.current = false;\n")
        source_mtime = self.source.stat().st_mtime
        os.utime(self.compressed, (source_mtime - 10, source_mtime - 10))
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            self._temporary_directory.cleanup()

    def get_app(self):
        return Application(
            [
                (
                    r"/(.*)",
                    CachedStaticFileHandler,
                    {"path": str(self.static_root)},
                )
            ]
        )

    def test_stale_gzip_sibling_is_not_served(self):
        response = self.fetch(
            "/entry.js",
            method="HEAD",
            headers={"Accept-Encoding": "gzip"},
            decompress_response=False,
        )

        assert response.code == 200
        assert "Content-Encoding" not in response.headers
        assert int(response.headers["Content-Length"]) == self.source.stat().st_size
