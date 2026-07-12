import base64
from dataclasses import FrozenInstanceError
import hashlib
from io import BytesIO
from pathlib import Path
import struct
import zipfile
import zlib

import pytest

from chanlun.decision_support import corpus_integrity
from chanlun.decision_support.corpus_integrity import probe_file, scan_corpus
from chanlun.decision_support.corpus_types import CorpusFile, IntegrityReport


_VALID_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAYEBQYFBAYGBQYHBwYIChAKCgkJChQODwwQFxQYGBcU"
    "FhYaHSUfGhsjHBYWICwgIyYnKSopGR8tMC0oMCUoKSj/2wBDAQcHBwoIChMKChMoGhYaKCgoKCgo"
    "KCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCgoKCj/wAARCAADAAIDASIA"
    "AhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQA"
    "AAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3"
    "ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWm"
    "p6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEA"
    "AwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSEx"
    "BhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElK"
    "U1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3"
    "uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwD6pooo"
    "oA//2Q=="
)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(payload, checksum) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)


def _valid_png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    scanlines = b"".join(b"\x00" + (b"\xff\xff\xff" * width) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(scanlines))
        + _png_chunk(b"IEND", b"")
    )


def _truncated_jpeg_with_sof(width: int, height: int) -> bytes:
    payload = (
        b"\x08"
        + height.to_bytes(2, "big")
        + width.to_bytes(2, "big")
        + b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
    )
    return b"\xff\xd8\xff\xc0" + (len(payload) + 2).to_bytes(2, "big") + payload


def _complete_mobi() -> bytes:
    payload = bytearray(350)
    payload[60:68] = b"BOOKMOBI"
    payload[76:78] = (1).to_bytes(2, "big")
    payload[78:82] = (86).to_bytes(4, "big")
    payload[86:88] = (1).to_bytes(2, "big")
    payload[102:106] = b"MOBI"
    payload[106:110] = (232).to_bytes(4, "big")
    return bytes(payload)


_VALID_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="book-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="book-id">fixture-book</dc:identifier>
    <dc:title>Fixture</dc:title>
    <dc:language>zh-CN</dc:language>
  </metadata>
  <manifest>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="chapter"/></spine>
</package>"""


def _epub(*, complete: bool, opf: str = _VALID_OPF) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            b"application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        if complete:
            archive.writestr(
                "META-INF/container.xml",
                '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0"><rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
            )
            archive.writestr("OEBPS/content.opf", opf)
            archive.writestr("OEBPS/chapter.xhtml", "<html xmlns=\"http://www.w3.org/1999/xhtml\"><body>fixture</body></html>")
    return buffer.getvalue()


def test_probe_file_rejects_zero_byte_jpeg(tmp_path: Path):
    image = tmp_path / "chart.jpg"
    image.write_bytes(b"")

    result = probe_file(image)

    assert result.valid is False
    assert result.error_code == "zero_byte"
    assert result.size == 0


def test_probe_file_rejects_extension_signature_mismatch(tmp_path: Path):
    image = tmp_path / "chart.jpg"
    image.write_text("not a jpeg", encoding="utf-8")

    result = probe_file(image)

    assert result.valid is False
    assert result.error_code == "signature_mismatch"


def test_probe_file_reads_verified_png_dimensions_and_hash(tmp_path: Path):
    payload = _valid_png(width=2, height=3)
    image = tmp_path / "chart.png"
    image.write_bytes(payload)

    result = probe_file(image)

    assert result.valid is True
    assert result.media_type == "image/png"
    assert (result.width, result.height) == (2, 3)
    assert result.sha256 == hashlib.sha256(payload).hexdigest()


def test_probe_file_rejects_png_when_decoder_is_unavailable(tmp_path: Path, monkeypatch):
    image = tmp_path / "chart.png"
    image.write_bytes(_valid_png(width=2, height=3))
    monkeypatch.setattr(corpus_integrity, "Image", None)

    result = probe_file(image)

    assert result.valid is False
    assert result.error_code == "decoder_unavailable"


def test_probe_file_rejects_truncated_png_with_valid_header(tmp_path: Path):
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (2).to_bytes(4, "big")
        + (3).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )
    image = tmp_path / "chart.png"
    image.write_bytes(payload)

    result = probe_file(image)

    assert result.valid is False
    assert result.error_code == "signature_mismatch"


def test_probe_file_reads_jpeg_dimensions_after_complete_decode(tmp_path: Path):
    image = tmp_path / "chart.jpg"
    image.write_bytes(_VALID_JPEG)

    result = probe_file(image)

    assert result.valid is True
    assert result.media_type == "image/jpeg"
    assert (result.width, result.height) == (2, 3)


def test_probe_file_rejects_jpeg_when_decoder_is_unavailable(tmp_path: Path, monkeypatch):
    image = tmp_path / "chart.jpg"
    image.write_bytes(_VALID_JPEG)
    monkeypatch.setattr(corpus_integrity, "Image", None)

    result = probe_file(image)

    assert result.valid is False
    assert result.error_code == "decoder_unavailable"


def test_probe_file_rejects_jpeg_truncated_after_sof(tmp_path: Path):
    image = tmp_path / "chart.jpg"
    image.write_bytes(_truncated_jpeg_with_sof(width=8, height=6))

    result = probe_file(image)

    assert result.valid is False
    assert result.error_code == "signature_mismatch"


def test_probe_file_rejects_malformed_json(tmp_path: Path):
    document = tmp_path / "index.json"
    document.write_text('{"downloaded": 92', encoding="utf-8")

    result = probe_file(document)

    assert result.valid is False
    assert result.error_code == "invalid_json"


def test_probe_file_rejects_bookmobi_marker_without_record_headers(tmp_path: Path):
    payload = bytearray(80)
    payload[60:68] = b"BOOKMOBI"
    ebook = tmp_path / "lesson.azw3"
    ebook.write_bytes(payload)

    result = probe_file(ebook)

    assert result.valid is False
    assert result.error_code == "signature_mismatch"


def test_probe_file_accepts_structurally_complete_mobi(tmp_path: Path):
    ebook = tmp_path / "lesson.azw3"
    ebook.write_bytes(_complete_mobi())

    result = probe_file(ebook)

    assert result.valid is True
    assert result.media_type == "application/vnd.amazon.ebook"


def test_probe_file_rejects_epub_without_container_and_package(tmp_path: Path):
    ebook = tmp_path / "lesson.epub"
    ebook.write_bytes(_epub(complete=False))

    result = probe_file(ebook)

    assert result.valid is False
    assert result.error_code == "signature_mismatch"


def test_probe_file_rejects_epub_with_empty_opf_package(tmp_path: Path):
    ebook = tmp_path / "lesson.epub"
    ebook.write_bytes(_epub(complete=True, opf="<package/>"))

    result = probe_file(ebook)

    assert result.valid is False
    assert result.error_code == "signature_mismatch"


def test_probe_file_accepts_complete_epub_container(tmp_path: Path):
    ebook = tmp_path / "lesson.epub"
    ebook.write_bytes(_epub(complete=True))

    result = probe_file(ebook)

    assert result.valid is True
    assert result.media_type == "application/epub+zip"


def test_scan_corpus_sorts_paths_and_reports_invalid_files(tmp_path: Path):
    (tmp_path / "b.md").write_text("走势终完美", encoding="utf-8")
    (tmp_path / "a.txt").write_text("区间套", encoding="utf-8")
    (tmp_path / "zero.jpg").write_bytes(b"")

    report = scan_corpus([tmp_path])

    assert [item.path.name for item in report.files] == ["a.txt", "b.md", "zero.jpg"]
    assert [item.path.name for item in report.valid_files] == ["a.txt", "b.md"]
    assert [(issue.path, issue.code) for issue in report.issues] == [
        (str((tmp_path / "zero.jpg").resolve()), "zero_byte")
    ]


def test_scan_corpus_uses_raw_path_as_casefold_tiebreaker(tmp_path: Path, monkeypatch):
    sharp_s = (tmp_path / "ß.txt").resolve()
    plain_ss = (tmp_path / "ss.txt").resolve()
    sharp_s.write_text("a", encoding="utf-8")
    plain_ss.write_text("b", encoding="utf-8")
    monkeypatch.setattr(
        corpus_integrity,
        "_iter_paths",
        lambda roots: iter((sharp_s, plain_ss)),
    )

    report = scan_corpus([tmp_path])

    expected = sorted(
        [sharp_s, plain_ss],
        key=lambda path: (str(path).casefold(), str(path)),
    )
    assert [item.path for item in report.files] == expected


def test_integrity_report_copies_mutable_inputs_to_tuples(tmp_path: Path):
    source = [CorpusFile(tmp_path / "a.md", 1, "hash", "text/markdown", True)]

    report = IntegrityReport(files=source)
    source.clear()

    assert isinstance(report.files, tuple)
    assert len(report.files) == 1
    with pytest.raises(FrozenInstanceError):
        report.files = ()