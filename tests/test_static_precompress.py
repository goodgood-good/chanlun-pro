"""static_precompress 单元测试 (B2)。
TDD：先验证以下行为
- .js / .css > 1KB 会被压缩成 .gz
- .png 等二进制资源不被压缩
- < 1KB 文件被跳过
- 已存在且更新的 .gz 跳过
- 源文件 mtime 更新后 .gz 会被刷新
"""
import gzip
import os
import time

import pytest

from cl_app.services.static_precompress import precompress_directory


@pytest.fixture
def workdir(tmp_path):
    big_js = tmp_path / "big.js"
    big_js.write_text("// hello\n" * 200, encoding="utf-8")
    big_css = tmp_path / "site.css"
    big_css.write_text(".x{color:#000}\n" * 200, encoding="utf-8")
    small = tmp_path / "small.js"
    small.write_text("ok", encoding="utf-8")
    bin_ = tmp_path / "logo.png"
    bin_.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 2000)
    return tmp_path


def test_compresses_js_and_css(workdir):
    c, s, _ = precompress_directory(str(workdir))
    assert c == 2
    assert (workdir / "big.js.gz").exists()
    assert (workdir / "site.css.gz").exists()
    assert not (workdir / "logo.png.gz").exists()


def test_skips_small_files(workdir):
    c, s, _ = precompress_directory(str(workdir))
    assert s >= 1
    assert not (workdir / "small.js.gz").exists()


def test_idempotent(workdir):
    precompress_directory(str(workdir))
    c2, s2, _ = precompress_directory(str(workdir))
    assert c2 == 0
    assert s2 >= 2


def test_refreshes_when_source_newer(workdir):
    precompress_directory(str(workdir))
    big = workdir / "big.js"
    future = time.time() + 5
    os.utime(big, (future, future))
    c, _, _ = precompress_directory(str(workdir))
    assert c == 1


def test_gz_content_matches_source(workdir):
    precompress_directory(str(workdir))
    gz = workdir / "big.js.gz"
    with gzip.open(gz, "rb") as f:
        decompressed = f.read()
    assert decompressed == (workdir / "big.js").read_bytes()


def test_missing_directory_returns_zero():
    c, s, e = precompress_directory("/path/does/not/exist")
    assert (c, s) == (0, 0)
    assert e == 0.0
