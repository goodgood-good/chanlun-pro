from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence
import xml.etree.ElementTree as ET
import zipfile
import zlib

from .corpus_types import CorpusFile, IntegrityIssue, IntegrityReport

try:
    from PIL import Image
except ImportError:  # 未安装 Pillow 时仍保留结构校验能力。
    Image = None


_TEXT_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".htm": "text/html",
    ".html": "text/html",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".txt": "text/plain",
}
_IMAGE_MEDIA_TYPES = {
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}
_EBOOK_MEDIA_TYPES = {
    ".azw3": "application/vnd.amazon.ebook",
    ".epub": "application/epub+zip",
    ".mobi": "application/x-mobipocket-ebook",
}
_JSON_MEDIA_TYPE = "application/json"
_JPEG_SOF_MARKERS = frozenset(
    {
        0xC0,
        0xC1,
        0xC2,
        0xC3,
        0xC5,
        0xC6,
        0xC7,
        0xC9,
        0xCA,
        0xCB,
        0xCD,
        0xCE,
        0xCF,
    }
)
_PNG_BIT_DEPTHS = {
    0: frozenset({1, 2, 4, 8, 16}),
    2: frozenset({8, 16}),
    3: frozenset({1, 2, 4, 8}),
    4: frozenset({8, 16}),
    6: frozenset({8, 16}),
}


def _declared_media_type(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix in _TEXT_MEDIA_TYPES:
        return _TEXT_MEDIA_TYPES[suffix]
    if suffix in _IMAGE_MEDIA_TYPES:
        return _IMAGE_MEDIA_TYPES[suffix]
    if suffix in _EBOOK_MEDIA_TYPES:
        return _EBOOK_MEDIA_TYPES[suffix]
    if suffix == ".json":
        return _JSON_MEDIA_TYPE
    return "application/octet-stream"


def _decoder_accepts(
    data: bytes,
    expected_format: str,
    dimensions: tuple[int, int],
) -> bool:
    if Image is None:
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != expected_format or image.size != dimensions:
                return False
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except (OSError, SyntaxError, ValueError):
        return False
    return True


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 6 or not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        return None

    offset = 2
    dimensions: tuple[int, int] | None = None
    saw_scan = False
    saw_eoi = False
    while offset < len(data):
        if data[offset] != 0xFF:
            return None
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            return None

        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            saw_eoi = offset == len(data)
            break
        if marker in (0x00, 0xD8):
            return None
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if offset + 2 > len(data):
            return None

        segment_length = int.from_bytes(data[offset : offset + 2], "big")
        segment_end = offset + segment_length
        if segment_length < 2 or segment_end > len(data):
            return None

        if marker in _JPEG_SOF_MARKERS:
            if segment_length < 8:
                return None
            component_count = data[offset + 7]
            if component_count == 0 or segment_length != 8 + 3 * component_count:
                return None
            height = int.from_bytes(data[offset + 3 : offset + 5], "big")
            width = int.from_bytes(data[offset + 5 : offset + 7], "big")
            if width <= 0 or height <= 0:
                return None
            dimensions = (width, height)

        if marker != 0xDA:
            offset = segment_end
            continue

        component_count = data[offset + 2] if segment_length >= 3 else 0
        if dimensions is None or component_count == 0 or segment_length != 6 + 2 * component_count:
            return None
        saw_scan = True
        offset = segment_end
        entropy_start = offset
        while offset < len(data):
            if data[offset] != 0xFF:
                offset += 1
                continue
            marker_start = offset
            while offset < len(data) and data[offset] == 0xFF:
                offset += 1
            if offset >= len(data):
                return None
            scan_marker = data[offset]
            if scan_marker == 0x00 or 0xD0 <= scan_marker <= 0xD7:
                offset += 1
                continue
            if marker_start == entropy_start:
                return None
            offset = marker_start
            break
        else:
            return None

    if dimensions is None or not saw_scan or not saw_eoi:
        return None
    if not _decoder_accepts(data, "JPEG", dimensions):
        return None
    return dimensions


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 45 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None

    offset = 8
    dimensions: tuple[int, int] | None = None
    layout: tuple[int, int, int, int, int] | None = None
    saw_idat = False
    idat_finished = False
    idat_payloads: list[bytes] = []
    saw_iend = False
    while offset < len(data):
        if offset + 12 > len(data):
            return None
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunk_end = offset + 12 + length
        if chunk_end > len(data):
            return None
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = int.from_bytes(data[offset + 8 + length : chunk_end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != expected_crc:
            return None

        if dimensions is None:
            if kind != b"IHDR" or length != 13:
                return None
            width, height, bit_depth, color_type, compression, filtering, interlace = struct_unpack_ihdr(payload)
            allowed_depths = _PNG_BIT_DEPTHS.get(color_type)
            if (
                width <= 0
                or height <= 0
                or allowed_depths is None
                or bit_depth not in allowed_depths
                or compression != 0
                or filtering != 0
                or interlace not in (0, 1)
            ):
                return None
            dimensions = (width, height)
            layout = (width, height, bit_depth, color_type, interlace)
        elif kind == b"IHDR":
            return None

        if kind == b"IDAT":
            if idat_finished:
                return None
            saw_idat = True
            idat_payloads.append(payload)
        elif saw_idat and kind != b"IEND":
            idat_finished = True

        if kind == b"IEND":
            if length != 0 or not saw_idat or chunk_end != len(data):
                return None
            saw_iend = True
            offset = chunk_end
            break
        if kind and 65 <= kind[0] <= 90 and kind not in (b"IHDR", b"PLTE", b"IDAT"):
            return None
        offset = chunk_end

    if dimensions is None or layout is None or not saw_iend:
        return None
    try:
        raw = zlib.decompress(b"".join(idat_payloads))
    except zlib.error:
        return None
    if not raw:
        return None

    width, height, bit_depth, color_type, interlace = layout
    if interlace == 0:
        channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
        row_bytes = (width * channels * bit_depth + 7) // 8
        stride = row_bytes + 1
        if len(raw) != height * stride:
            return None
        if any(raw[row * stride] > 4 for row in range(height)):
            return None
    if not _decoder_accepts(data, "PNG", dimensions):
        return None
    return dimensions

def struct_unpack_ihdr(payload: bytes) -> tuple[int, int, int, int, int, int, int]:
    width = int.from_bytes(payload[0:4], "big")
    height = int.from_bytes(payload[4:8], "big")
    return width, height, payload[8], payload[9], payload[10], payload[11], payload[12]


def _safe_zip_name(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in name


def _valid_epub(data: bytes) -> bool:
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if not infos or infos[0].filename != "mimetype":
                return False
            if infos[0].compress_type != zipfile.ZIP_STORED:
                return False
            if any(info.flag_bits & 0x1 for info in infos):
                return False
            names = [info.filename for info in infos]
            if len(names) != len(set(names)) or not all(_safe_zip_name(name) for name in names):
                return False
            if archive.testzip() is not None:
                return False
            if archive.read("mimetype") != b"application/epub+zip":
                return False
            if "META-INF/container.xml" not in names:
                return False

            container = ET.fromstring(archive.read("META-INF/container.xml"))
            container_ns = "urn:oasis:names:tc:opendocument:xmlns:container"
            if container.tag != f"{{{container_ns}}}container" or container.attrib.get("version") != "1.0":
                return False
            rootfiles = container.findall(f".//{{{container_ns}}}rootfile")
            if not rootfiles:
                return False
            package_path = rootfiles[0].attrib.get("full-path", "")
            if (
                not package_path
                or rootfiles[0].attrib.get("media-type") != "application/oebps-package+xml"
                or not _safe_zip_name(package_path)
                or package_path not in names
            ):
                return False

            package = ET.fromstring(archive.read(package_path))
            opf_ns = "http://www.idpf.org/2007/opf"
            dc_ns = "http://purl.org/dc/elements/1.1/"
            prefix = f"{{{opf_ns}}}"
            if package.tag != f"{prefix}package":
                return False
            if package.attrib.get("version") not in ("2.0", "3.0", "3.1"):
                return False
            identifier_id = package.attrib.get("unique-identifier", "")
            metadata = package.find(f"{prefix}metadata")
            manifest = package.find(f"{prefix}manifest")
            spine = package.find(f"{prefix}spine")
            if not identifier_id or metadata is None or manifest is None or spine is None:
                return False
            identifiers = metadata.findall(f"{{{dc_ns}}}identifier")
            if not any(item.attrib.get("id") == identifier_id and (item.text or "").strip() for item in identifiers):
                return False
            if metadata.find(f"{{{dc_ns}}}title") is None or metadata.find(f"{{{dc_ns}}}language") is None:
                return False

            package_dir = PurePosixPath(package_path).parent
            manifest_ids: set[str] = set()
            for item in manifest.findall(f"{prefix}item"):
                item_id = item.attrib.get("id", "")
                href = item.attrib.get("href", "").split("#", 1)[0]
                media_type = item.attrib.get("media-type", "")
                if not item_id or item_id in manifest_ids or not href or not media_type:
                    return False
                target = package_dir / PurePosixPath(href)
                target_name = target.as_posix()
                if not _safe_zip_name(target_name) or target_name not in names:
                    return False
                if media_type in ("application/xhtml+xml", "image/svg+xml"):
                    ET.fromstring(archive.read(target_name))
                manifest_ids.add(item_id)
            if not manifest_ids:
                return False

            itemrefs = spine.findall(f"{prefix}itemref")
            if not itemrefs or any(item.attrib.get("idref") not in manifest_ids for item in itemrefs):
                return False
    except (ET.ParseError, KeyError, OSError, RuntimeError, zipfile.BadZipFile):
        return False
    return True

def _valid_mobi(data: bytes) -> bool:
    if len(data) < 110 or data[60:68] != b"BOOKMOBI":
        return False
    record_count = int.from_bytes(data[76:78], "big")
    if record_count < 1 or record_count > 65535:
        return False
    table_end = 78 + 8 * record_count
    if table_end > len(data):
        return False
    offsets = [
        int.from_bytes(data[78 + 8 * index : 82 + 8 * index], "big")
        for index in range(record_count)
    ]
    if offsets != sorted(set(offsets)) or any(offset < table_end or offset >= len(data) for offset in offsets):
        return False

    first_record = offsets[0]
    record_end = offsets[1] if len(offsets) > 1 else len(data)
    if first_record + 24 > record_end:
        return False
    compression = int.from_bytes(data[first_record : first_record + 2], "big")
    if compression not in (1, 2, 17480):
        return False
    if data[first_record + 16 : first_record + 20] != b"MOBI":
        return False
    mobi_length = int.from_bytes(data[first_record + 20 : first_record + 24], "big")
    return 116 <= mobi_length <= record_end - (first_record + 16)


def _invalid(
    path: Path,
    data: bytes,
    media_type: str,
    code: str,
) -> CorpusFile:
    return CorpusFile(
        path=path,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        media_type=media_type,
        valid=False,
        error_code=code,
    )


def probe_file(path: Path) -> CorpusFile:
    resolved = Path(path).resolve()
    media_type = _declared_media_type(resolved)
    try:
        data = resolved.read_bytes()
    except OSError:
        return CorpusFile(resolved, 0, "", media_type, False, "unreadable")

    if not data:
        return _invalid(resolved, data, media_type, "zero_byte")

    suffix = resolved.suffix.casefold()
    dimensions: tuple[int, int] | None = None
    error_code = ""
    if suffix in (".jpg", ".jpeg"):
        if Image is None:
            error_code = "decoder_unavailable"
        else:
            dimensions = _jpeg_dimensions(data)
            if dimensions is None:
                error_code = "signature_mismatch"
    elif suffix == ".png":
        if Image is None:
            error_code = "decoder_unavailable"
        else:
            dimensions = _png_dimensions(data)
            if dimensions is None:
                error_code = "signature_mismatch"
    elif suffix == ".json":
        try:
            json.loads(data.decode("utf-8"))
        except UnicodeDecodeError:
            error_code = "invalid_utf8"
        except json.JSONDecodeError:
            error_code = "invalid_json"
    elif suffix in _TEXT_MEDIA_TYPES:
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            error_code = "invalid_utf8"
    elif suffix == ".epub":
        if not _valid_epub(data):
            error_code = "signature_mismatch"
    elif suffix in (".azw3", ".mobi"):
        if not _valid_mobi(data):
            error_code = "signature_mismatch"
    else:
        error_code = "unsupported_type"

    if error_code:
        return _invalid(resolved, data, media_type, error_code)

    width, height = dimensions or (None, None)
    return CorpusFile(
        path=resolved,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        media_type=media_type,
        valid=True,
        width=width,
        height=height,
    )


def _iter_paths(roots: Sequence[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for root in roots:
        resolved = Path(root).resolve()
        candidates = [resolved] if resolved.is_file() else resolved.rglob("*") if resolved.is_dir() else []
        for candidate in candidates:
            if not candidate.is_file() or candidate.is_symlink():
                continue
            item = candidate.resolve()
            if item not in seen:
                seen.add(item)
                yield item


def scan_corpus(roots: Sequence[Path]) -> IntegrityReport:
    files = tuple(
        probe_file(path)
        for path in sorted(
            _iter_paths(roots),
            key=lambda value: (str(value).casefold(), str(value)),
        )
    )
    issues = tuple(
        IntegrityIssue(str(item.path), item.error_code, item.error_code)
        for item in files
        if not item.valid
    )
    return IntegrityReport(files=files, issues=issues)
