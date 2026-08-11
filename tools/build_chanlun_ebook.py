# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import mimetypes
import re
import shutil
import subprocess
import uuid
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdf import PdfReader
from pypdf.generic import ContentStream, NameObject

from cover_art import cover_xhtml, create_clean_cover

try:
    from PIL import Image, ImageEnhance, ImageFilter
except ImportError:  # pragma: no cover - optional enhancement dependency
    Image = None
    ImageEnhance = None
    ImageFilter = None


TITLE = "缠论"
CREATOR = "缠中说禅"
LANGUAGE = "zh-CN"
IDENTIFIER = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, "chanlun-108-angel-edition"))
DATE_PATTERN = r"\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{1,2}"
KNOWN_LESSON_DATES = {
    20: "2007-01-05 15:23:22",
    92: "2007-12-27 20:31:33",
}


@dataclass
class TocEntry:
    id: str
    title: str
    page: int
    num: int | None = None


@dataclass
class ImageRef:
    href: str
    alt: str
    width_pct: float | None = None
    display_width_pt: float | None = None
    display_height_pt: float | None = None
    effective_dpi: float | None = None
    enhanced: bool = False


@dataclass
class ImageAuditEntry:
    page: int
    xobject: str
    href: str
    sha256: str
    source_sha256: str
    byte_count: int
    source_byte_count: int
    width: int
    height: int
    display_width_pt: float | None = None
    display_height_pt: float | None = None
    effective_dpi: float | None = None
    width_pct: float | None = None
    enhanced: bool = False


@dataclass
class ImagePlacement:
    width_pt: float
    height_pt: float
    page_width_pt: float
    page_height_pt: float


@dataclass
class ImageOptions:
    use_pdf_display_width: bool = False
    enhance_images: bool = False
    drop_overview_maps: bool = False


@dataclass
class ImageEnhanceStats:
    enhanced: int = 0
    untouched: int = 0
    failed: int = 0
    skipped_overview_maps: int = 0
    source_bytes: int = 0
    output_bytes: int = 0


@dataclass
class ChapterAuditEntry:
    title: str
    href: str
    page_start: int
    page_end: int
    source_chars: int
    ebook_chars: int
    content_match: bool
    h2_count: int
    image_count: int


@dataclass
class SubheadingEntry:
    id: str
    title: str
    kind: str


def find_default_pdftotext() -> Path | None:
    candidates = [
        shutil.which("pdftotext"),
        r"C:\Program Files\Calibre2\app\bin\pdftotext.exe",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def normalize_for_match(text: str) -> str:
    text = text.replace("：", ":")
    return re.sub(r"\s+", "", text)


def clean_toc_title(text: str) -> str:
    text = re.sub(r"[\.．。·•…]+$", "", text.strip())
    text = re.sub(r"\s+", " ", text)
    text = text.replace(" :", ":").replace(": ", ": ")
    text = text.replace(" ：", "：").replace("： ", "：")
    text = re.sub(r"教你炒股票\s+(\d+)\s*[:：]", r"教你炒股票 \1：", text)
    text = re.sub(r"：\s+", "：", text)
    text = normalize_lesson_title_date(text)
    return text


def normalize_lesson_title_date(text: str) -> str:
    num_match = re.match(r"教你炒股票\s*(\d+)\s*[:：]", text)
    if num_match:
        num = int(num_match.group(1))
        known_date = KNOWN_LESSON_DATES.get(num)
        if known_date:
            text = re.sub(r"[\(（]\d{4}-\d{2}-\d{2}.*$", "", text).strip()
            return f"{text}({known_date})"
    text = re.sub(r"(\d{4}-\d{2}-\d{2})(?=\d{1,2}:\d{2}:\d{1,2})", r"\1 ", text)
    if re.search(r"[\(（]\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}$", text):
        text += ")"
    return text


def extract_toc_entries(reader: PdfReader) -> list[TocEntry]:
    entries: list[TocEntry] = [
        TocEntry(id="cover", title="封面", page=1),
        TocEntry(id="original_toc", title="目录", page=2),
    ]
    pending = ""

    def consume(line: str) -> None:
        nonlocal pending
        raw = line.strip()
        if not raw or raw == "目 录" or raw.startswith("天使版") or re.match(r"第\s*\d+\s*页", raw):
            return
        if raw.startswith("教你炒股票"):
            if pending:
                parse_toc_line(pending, entries)
            pending = raw
            if re.search(r"\d{1,4}\s*$", raw) and "…" in raw:
                parse_toc_line(pending, entries)
                pending = ""
            return
        if pending:
            pending += " " + raw
            if re.search(r"\d{1,4}\s*$", raw):
                parse_toc_line(pending, entries)
                pending = ""

    for page_no in range(2, 6):
        text = reader.pages[page_no - 1].extract_text() or ""
        for line in text.splitlines():
            consume(line)
    if pending:
        parse_toc_line(pending, entries)

    seen: set[int | str] = set()
    deduped: list[TocEntry] = []
    for entry in entries:
        key: int | str = entry.num if entry.num is not None else entry.id
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
    return deduped


def parse_toc_line(line: str, entries: list[TocEntry]) -> None:
    line = re.sub(r"\s+", " ", line.strip())
    m = re.search(r"(\d{1,4})\s*$", line)
    if not m:
        return
    page = int(m.group(1))
    left = line[: m.start()].rstrip()
    left = re.sub(r"[\.．。·•…]+$", "", left).strip()
    num_match = re.match(r"教你炒股票\s*(\d+)\s*[:：]", left)
    if not num_match:
        return
    num = int(num_match.group(1))
    title = clean_toc_title(left)
    entries.append(TocEntry(id=f"lesson_{num:03d}", title=title, page=page, num=num))


def extract_layout_pages(pdf_path: Path, build_dir: Path, pdftotext: Path | None) -> list[str]:
    raw_path = build_dir / "raw_layout.txt"
    if pdftotext is None:
        raise RuntimeError("未找到 pdftotext。Calibre 通常自带 C:\\Program Files\\Calibre2\\app\\bin\\pdftotext.exe")
    subprocess.run(
        [str(pdftotext), "-layout", "-enc", "UTF-8", str(pdf_path), str(raw_path)],
        check=True,
    )
    text = raw_path.read_text(encoding="utf-8", errors="replace")
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    return pages


def page_body_lines(page_text: str, page_no: int) -> list[str]:
    lines: list[str] = []
    for raw in page_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            lines.append("")
            continue
        if "天使版---教你炒股票 108 课" in stripped:
            continue
        if re.match(r"第\s*\d+\s*页\s*共\s*\d+\s*页", stripped):
            continue
        lines.append(raw.rstrip())
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def locate_lesson_starts(pages: list[str]) -> dict[int, int]:
    starts: dict[int, int] = {}
    for idx, page_text in enumerate(pages, start=1):
        if idx < 7:
            continue
        lines = [line.strip() for line in page_body_lines(page_text, idx) if line.strip()]
        if not lines:
            continue
        compact = normalize_for_match(lines[0])
        m = re.match(r"教你炒股票(\d{1,3}):", compact)
        if m:
            num = int(m.group(1))
            starts.setdefault(num, idx)
    return starts


def correct_toc_pages(entries: list[TocEntry], starts: dict[int, int]) -> list[str]:
    notes: list[str] = []
    last_page = 0
    for entry in entries:
        if entry.num is None:
            last_page = entry.page
            continue
        actual = starts.get(entry.num)
        if actual and actual != entry.page:
            notes.append(f"修正第 {entry.num} 课页码: 目录 {entry.page} -> 正文 {actual}")
            entry.page = actual
        if entry.page < last_page:
            notes.append(f"警告: {entry.title} 的页码 {entry.page} 小于前一项 {last_page}")
        last_page = entry.page
    return notes


def is_heading_line(line: str, num: int) -> bool:
    compact = normalize_for_match(line)
    return f"教你炒股票{num}:" in compact


def next_nonblank_index(lines: list[str], start: int) -> int | None:
    for idx in range(start, len(lines)):
        if lines[idx].strip():
            return idx
    return None


def split_heading_date(text: str) -> tuple[str, str | None]:
    match = re.search(r"[\(（]?\d{4}-\d{2}-\d{2}\s*\d{1,2}:\d{2}:\d{1,2}[\)）]?", text)
    if not match:
        return text.strip(), None
    title = text[: match.start()].strip()
    date = match.group(0).strip()
    if date and not date.startswith(("(", "（")):
        date = f"({date}"
    if date and not date.endswith((")", "）")):
        date = f"{date})"
    return title, date


def extract_lesson_intro(lines: list[str], entry: TocEntry) -> tuple[list[str], list[str]]:
    if entry.num is None:
        return [], lines
    heading_idx = next_nonblank_index(lines, 0)
    if heading_idx is None:
        return [], lines
    heading = lines[heading_idx].strip()
    if not is_heading_line(heading, entry.num):
        return [], lines

    consumed_until = heading_idx + 1
    combined_heading = heading
    date_text: str | None = None
    next_idx = next_nonblank_index(lines, consumed_until)
    if next_idx is not None:
        next_line = lines[next_idx].strip()
        if re.fullmatch(r"\d{1,2}:\d{2}:\d{1,2}[\)）]?", next_line) and re.search(r"[\(（]\d{4}-\d{2}-\d{2}$", heading):
            combined_heading = f"{heading} {next_line}"
            consumed_until = next_idx + 1
        elif is_parenthesized_datetime(next_line):
            date_text = next_line
            consumed_until = next_idx + 1

    title_text, inline_date = split_heading_date(combined_heading)
    date_text = inline_date or date_text

    category_text: str | None = None
    category_idx = next_nonblank_index(lines, consumed_until)
    if category_idx is not None:
        candidate = lines[category_idx].strip()
        if "在流程图" in candidate and is_heading_line(candidate, entry.num):
            category_text = candidate
            consumed_until = category_idx + 1

    remaining = lines[consumed_until:]
    while remaining and not remaining[0].strip():
        remaining.pop(0)

    parts = [f"<h1>{html.escape(format_inline_spacing(title_text))}</h1>"]
    if date_text:
        parts.append(f'<p class="date">{html.escape(date_text)}</p>')
    if category_text:
        parts.append(f'<p class="category">{html.escape(format_inline_spacing(category_text))}</p>')
    return parts, remaining


def is_datetime_line(text: str) -> bool:
    stripped = text.strip()
    return bool(
        re.fullmatch(DATE_PATTERN, stripped)
        or re.fullmatch(rf"{DATE_PATTERN}[)）]", stripped)
        or re.fullmatch(rf"\({DATE_PATTERN}\)", stripped)
        or re.fullmatch(rf"（{DATE_PATTERN}）", stripped)
    )


def is_parenthesized_datetime(text: str) -> bool:
    return bool(re.fullmatch(rf"[\(（]{DATE_PATTERN}[\)）]", text.strip()))


def looks_like_reply_name(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped.startswith("[匿名]"):
        return True
    if stripped in {"缠中说禅", "缠中说禅："}:
        return True
    if len(stripped) > 24:
        return False
    if re.search(r"[。！？!?；;，,、（）()《》“”]", stripped):
        return False
    if re.search(r"\d", stripped):
        return False
    return True


def looks_like_numbered_body_item(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return bool(re.match(r"^(\d+|[０-９]+|[一二三四五六七八九十百]+)[、.．]\s*", stripped))


def classify_standalone(line: str) -> str | None:
    stripped = line.strip()
    if not stripped:
        return None
    if re.fullmatch(r"[*=]{5,}", stripped):
        return "separator"
    if is_datetime_line(stripped):
        return "date"
    if looks_like_numbered_body_item(stripped):
        return "p"
    if len(stripped) <= 42 and re.search(r"\(\d{4}-\d{2}-\d{2}", stripped):
        return "subtitle"
    return None


def line_ends_hard(line: str) -> bool:
    return bool(re.search(r"[。！？!?；;：:」』”）)]$", line.strip()))


def is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"


def needs_join_space(left: str, right: str) -> bool:
    if not left or not right:
        return False
    a = left[-1]
    b = right[0]
    if a.isspace() or b.isspace():
        return False
    if a.isascii() and a.isalnum() and (b.isascii() and b.isalnum() or is_cjk(b)):
        return True
    if is_cjk(a) and b.isascii() and b.isalnum():
        return True
    return False


def join_block_parts(parts: list[str]) -> str:
    result = ""
    for part in parts:
        clean = part.strip()
        if not clean:
            continue
        if result and needs_join_space(result, clean):
            result += " "
        result += clean
    return result


def format_inline_spacing(text: str) -> str:
    text = re.sub(r"([\u4e00-\u9fff])([A-Za-z0-9]+)", r"\1 \2", text)
    text = re.sub(r"([A-Za-z0-9]+)([\u4e00-\u9fff])", r"\1 \2", text)
    return re.sub(r"\s+", " ", text).strip()


def split_long_paragraph(text: str, max_len: int = 900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    sentences = re.findall(r".*?[。！？!?；;](?:[”’』」）)]*)|.+$", text)
    parts: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if current and len(current) + len(sentence) > max_len:
            parts.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        parts.append(current)
    return parts or [text]


def lines_to_blocks(lines: list[str]) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current: list[str] = []
    after_star_separator = False

    def flush(kind: str = "p") -> None:
        nonlocal current
        if not current:
            return
        text = join_block_parts(current)
        text = re.sub(r"\s+", " ", text)
        text = text.replace(" ，", "，").replace(" 。", "。")
        if text:
            text = format_inline_spacing(text)
            if kind == "p":
                blocks.extend((kind, part) for part in split_long_paragraph(text))
            else:
                blocks.append((kind, text))
        current = []

    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            flush()
            i += 1
            continue
        stripped = raw.strip()
        if re.fullmatch(r"\*{5,}", stripped):
            flush()
            blocks.append(("separator", stripped))
            after_star_separator = True
            i += 1
            continue
        if re.fullmatch(r"={2,}", stripped):
            flush()
            blocks.append(("separator", stripped))
            after_star_separator = False
            i += 1
            continue
        next_index = i + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        next_stripped = lines[next_index].strip() if next_index < len(lines) else ""
        if stripped.startswith("[匿名]"):
            flush()
            if next_stripped and is_datetime_line(next_stripped) and not is_parenthesized_datetime(next_stripped):
                blocks.append(("reply_heading", f"{stripped} {next_stripped}"))
                i = next_index + 1
            else:
                blocks.append(("reply_heading", stripped))
                i += 1
            after_star_separator = False
            continue
        if looks_like_reply_name(stripped) and next_stripped and is_datetime_line(next_stripped) and not is_parenthesized_datetime(next_stripped):
            flush()
            blocks.append(("reply_heading", f"{stripped} {next_stripped}"))
            i = next_index + 1
            after_star_separator = False
            continue
        if after_star_separator and next_stripped and is_parenthesized_datetime(next_stripped) and len(stripped) <= 80:
            flush()
            blocks.append(("reply_heading", f"{stripped} {next_stripped}"))
            i = next_index + 1
            after_star_separator = False
            continue
        special = classify_standalone(stripped)
        if special:
            flush()
            blocks.append((special, stripped))
            after_star_separator = False
            i += 1
            continue
        starts_para = raw.startswith("  ") or raw.startswith("\t")
        if starts_para and current:
            flush()
        elif current and line_ends_hard(current[-1]) and not starts_para:
            flush()
        elif current and line_ends_hard(current[-1]) and len(stripped) <= 36 and re.search(r"\(\d{4}-", stripped):
            flush()
        current.append(stripped)
        after_star_separator = False
        i += 1
    flush()
    return blocks


def image_output_name(page_no: int, index: int, xobject_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "", xobject_name.strip("/")) or f"image_{index}"
    return f"p{page_no:04d}_{index:02d}_{safe_name}.jpg"


def matrix_multiply(left: list[float], right: list[float]) -> list[float]:
    a, b, c, d, e, f = left
    g, h, i, j, k, l = right
    return [
        a * g + c * h,
        b * g + d * h,
        a * i + c * j,
        b * i + d * j,
        a * k + c * l + e,
        b * k + d * l + f,
    ]


def page_xobjects(page) -> dict:
    resources = page.get("/Resources") or {}
    resources = resources.get_object() if hasattr(resources, "get_object") else resources
    xobjects = resources.get("/XObject") or {}
    return xobjects.get_object() if hasattr(xobjects, "get_object") else xobjects


def page_image_placements(reader: PdfReader, page_no: int) -> dict[str, ImagePlacement]:
    page = reader.pages[page_no - 1]
    try:
        xobjects = page_xobjects(page)
        contents = page.get_contents()
        if contents is None:
            return {}
        content = ContentStream(contents, reader)
    except Exception:
        return {}

    placements: dict[str, ImagePlacement] = {}
    current_matrix = [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
    stack: list[list[float]] = []
    page_width = float(page.mediabox.width)
    page_height = float(page.mediabox.height)
    for operands, operator in content.operations:
        if operator == b"q":
            stack.append(current_matrix[:])
        elif operator == b"Q":
            current_matrix = stack.pop() if stack else [1.0, 0.0, 0.0, 1.0, 0.0, 0.0]
        elif operator == b"cm":
            try:
                current_matrix = matrix_multiply(current_matrix, [float(item) for item in operands])
            except Exception:
                continue
        elif operator == b"Do":
            if not operands:
                continue
            name = operands[0]
            obj = xobjects.get(name)
            if obj is None:
                obj = xobjects.get(NameObject(name))
            if obj is None:
                continue
            try:
                target = obj.get_object()
            except Exception:
                continue
            if str(target.get("/Subtype")) != "/Image":
                continue
            a, b, c, d, _, _ = current_matrix
            width_pt = (a * a + b * b) ** 0.5
            height_pt = (c * c + d * d) ** 0.5
            key = str(name)
            placements.setdefault(
                key,
                ImagePlacement(
                    width_pt=width_pt,
                    height_pt=height_pt,
                    page_width_pt=page_width,
                    page_height_pt=page_height,
                ),
            )
    return placements


def effective_image_dpi(width: int, height: int, placement: ImagePlacement | None) -> float | None:
    if not placement or not width or not height or not placement.width_pt or not placement.height_pt:
        return None
    dpi_x = width / (placement.width_pt / 72.0)
    dpi_y = height / (placement.height_pt / 72.0)
    return min(dpi_x, dpi_y)


def image_width_pct(placement: ImagePlacement | None) -> float | None:
    if not placement or not placement.width_pt or not placement.page_width_pt:
        return None
    # Body width is 88% because the generated CSS keeps 6% side margins.
    pct = placement.width_pt / (placement.page_width_pt * 0.88) * 100.0
    return max(18.0, min(100.0, pct))


def enhancement_level(page_no: int, effective_dpi: float | None) -> int:
    if page_no == 1 or effective_dpi is None:
        return 0
    if effective_dpi < 150:
        return 2
    if effective_dpi < 240:
        return 1
    return 0


def enhance_image_bytes(raw: bytes, level: int) -> bytes:
    if level <= 0 or Image is None or ImageEnhance is None or ImageFilter is None:
        return raw
    try:
        with Image.open(BytesIO(raw)) as image:
            image = image.convert("RGB")
            if level >= 2:
                image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
                image = ImageEnhance.Contrast(image).enhance(1.05)
                image = image.filter(ImageFilter.UnsharpMask(radius=0.8, percent=90, threshold=3))
                image = ImageEnhance.Sharpness(image).enhance(1.08)
            else:
                image = ImageEnhance.Contrast(image).enhance(1.03)
                image = image.filter(ImageFilter.UnsharpMask(radius=0.6, percent=60, threshold=4))
            output = BytesIO()
            image.save(output, format="JPEG", quality=95, subsampling=0, optimize=True)
            return output.getvalue()
    except Exception:
        return raw


def is_overview_map_image(raw: bytes, width: int, height: int) -> bool:
    if Image is None or width < 600 or not 300 <= height <= 760:
        return False
    if height == 0 or width / height < 1.6:
        return False
    try:
        with Image.open(BytesIO(raw)) as image:
            image = image.convert("RGB")
            sample = image.resize(
                (max(1, image.width // 8), max(1, image.height // 8)),
                Image.Resampling.BOX,
            )
            if hasattr(sample, "get_flattened_data"):
                pixels = list(sample.get_flattened_data())
            else:
                pixels = list(sample.getdata())
    except Exception:
        return False
    if not pixels:
        return False

    count = len(pixels)
    avg_r = sum(pixel[0] for pixel in pixels) / count
    avg_g = sum(pixel[1] for pixel in pixels) / count
    avg_b = sum(pixel[2] for pixel in pixels) / count
    green_ratio = (
        sum(1 for r, g, b in pixels if g > 80 and g > r * 1.25 and g > b * 1.15 and r < 80)
        / count
    )
    white_ratio = sum(1 for r, g, b in pixels if r > 235 and g > 235 and b > 235) / count
    return (
        avg_g - avg_r >= 20
        and avg_g - avg_b >= 8
        and green_ratio >= 0.12
        and white_ratio >= 0.65
    )


def extract_page_images(
    reader: PdfReader,
    page_no: int,
    image_dir: Path,
    dedupe: dict[str, tuple[str, str, int, bool]],
    image_audit: list[ImageAuditEntry],
    options: ImageOptions,
    stats: ImageEnhanceStats,
) -> list[ImageRef]:
    refs: list[ImageRef] = []
    page = reader.pages[page_no - 1]
    try:
        xobjects = page_xobjects(page)
    except Exception:
        return refs
    placements = page_image_placements(reader, page_no)
    image_index = 0
    for xobject_name, obj in xobjects.items():
        image_obj = obj.get_object()
        if str(image_obj.get("/Subtype")) != "/Image":
            continue
        image_index += 1
        raw = getattr(image_obj, "_data", b"") or image_obj.get_data()
        source_hash = hashlib.sha256(raw).hexdigest()
        width = int(image_obj.get("/Width") or 0)
        height = int(image_obj.get("/Height") or 0)
        if options.drop_overview_maps and is_overview_map_image(raw, width, height):
            stats.skipped_overview_maps += 1
            continue
        placement = placements.get(str(xobject_name))
        dpi = effective_image_dpi(width, height, placement)
        level = enhancement_level(page_no, dpi) if options.enhance_images else 0
        if source_hash in dedupe:
            href, output_hash, output_bytes, enhanced = dedupe[source_hash]
        else:
            output = enhance_image_bytes(raw, level)
            output_hash = hashlib.sha256(output).hexdigest()
            output_bytes = len(output)
            enhanced = output != raw
            name = image_output_name(page_no, image_index, str(xobject_name))
            dest = image_dir / name
            dest.write_bytes(output)
            href = f"Images/{dest.name}"
            dedupe[source_hash] = (href, output_hash, output_bytes, enhanced)
            stats.source_bytes += len(raw)
            stats.output_bytes += output_bytes
            if enhanced:
                stats.enhanced += 1
            elif level > 0:
                stats.failed += 1
            else:
                stats.untouched += 1
        pct = image_width_pct(placement) if options.use_pdf_display_width else None
        image_audit.append(
            ImageAuditEntry(
                page=page_no,
                xobject=str(xobject_name),
                href=href,
                sha256=output_hash,
                source_sha256=source_hash,
                byte_count=output_bytes,
                source_byte_count=len(raw),
                width=width,
                height=height,
                display_width_pt=placement.width_pt if placement else None,
                display_height_pt=placement.height_pt if placement else None,
                effective_dpi=dpi,
                width_pct=pct,
                enhanced=enhanced,
            )
        )
        refs.append(
            ImageRef(
                href=href,
                alt=f"第 {page_no} 页图 {image_index}",
                width_pct=pct,
                display_width_pt=placement.width_pt if placement else None,
                display_height_pt=placement.height_pt if placement else None,
                effective_dpi=dpi,
                enhanced=enhanced,
            )
        )
    return refs


def xhtml_doc(title: str, body: str) -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../Styles/style.css"/>
</head>
<body>
{body}
</body>
</html>
"""


def block_to_html(kind: str, text: str, element_id: str | None = None) -> str:
    escaped = html.escape(text)
    id_attr = f' id="{html.escape(element_id)}"' if element_id else ""
    if kind == "separator":
        return '<hr class="sep"/>'
    if kind == "date":
        return f'<p class="date">{escaped}</p>'
    if kind == "reply_heading":
        return f'<h2{id_attr} class="reply">{escaped}</h2>'
    if kind == "subtitle":
        return f'<h2{id_attr}>{escaped}</h2>'
    return f"<p>{escaped}</p>"


def image_ref_to_html(image_ref: ImageRef) -> str:
    src = "../" + image_ref.href
    viewer = "../" + image_viewer_href(image_ref.href)
    attrs = [f'src="{html.escape(src)}"', f'alt="{html.escape(image_ref.alt)}"']
    style_parts = ["max-width:100%", "height:auto"]
    if image_ref.width_pct:
        style_parts.insert(0, f"width:{image_ref.width_pct:.2f}%")
    attrs.append(f'style="{";".join(style_parts)}"')
    class_name = "image-link enhanced" if image_ref.enhanced else "image-link"
    return (
        f'<figure><a class="{class_name}" href="{html.escape(viewer)}">'
        f'<img {" ".join(attrs)}/>'
        "</a></figure>"
    )


def image_viewer_href(image_href: str) -> str:
    return f"ImagePages/{Path(image_href).stem}.xhtml"


def image_viewer_doc(title: str, image_href: str, alt: str) -> str:
    image_src = "../" + image_href
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="../Styles/style.css"/>
</head>
<body class="image-viewer-body">
  <div class="image-viewer-frame">
    <div class="image-viewer-cell">
      <img class="viewer-image" src="{html.escape(image_src)}" alt="{html.escape(alt)}"/>
    </div>
  </div>
</body>
</html>
"""


def include_subheading_in_nav(subheading: SubheadingEntry) -> bool:
    if subheading.kind != "subtitle":
        return False
    title = subheading.title.strip()
    if looks_like_numbered_body_item(title):
        return False
    if "[匿名]" in title or "匿名]" in title:
        return False
    if title.startswith("教你炒股票"):
        return False
    if title.startswith("缠中说禅："):
        return False
    if re.match(r"^[\(（]?\d{4}-\d{2}-\d{2}", title):
        return False
    if len(title) > 64:
        return False
    if re.search(r"[，,；;：:]$", title):
        return False
    if "？" in title or "?" in title:
        return False
    return True


def build_chapter_xhtml(
    entry: TocEntry,
    next_page: int,
    pages: list[str],
    reader: PdfReader,
    text_dir: Path,
    image_dir: Path,
    dedupe: dict[str, tuple[str, str, int, bool]],
    image_audit: list[ImageAuditEntry],
    options: ImageOptions,
    image_stats: ImageEnhanceStats,
) -> tuple[str, int, int, int, list[SubheadingEntry]]:
    body_parts = [f'<span id="{entry.id}"></span>']
    image_count = 0
    block_count = 0
    h2_count = 0
    subheadings: list[SubheadingEntry] = []
    for page_no in range(entry.page, next_page):
        if page_no < 1 or page_no > len(pages):
            continue
        lines = page_body_lines(pages[page_no - 1], page_no)
        intro_parts: list[str] = []
        if page_no == entry.page:
            intro_parts, lines = extract_lesson_intro(lines, entry)
        blocks = lines_to_blocks(lines)
        page_parts: list[str] = list(intro_parts)
        for kind, text in blocks:
            element_id = None
            if kind in {"reply_heading", "subtitle"}:
                h2_count += 1
                element_id = f"{entry.id}_sub_{h2_count:04d}"
                subheadings.append(SubheadingEntry(id=element_id, title=text, kind=kind))
            page_parts.append(block_to_html(kind, text, element_id))
            block_count += 1
        images = extract_page_images(reader, page_no, image_dir, dedupe, image_audit, options, image_stats)
        for image_ref in images:
            page_parts.append(image_ref_to_html(image_ref))
            image_count += 1
        if page_parts:
            body_parts.append(f'<section class="page" id="page-{page_no:04d}">')
            body_parts.extend(page_parts)
            body_parts.append("</section>")
    filename = f"{entry.id}.xhtml"
    (text_dir / filename).write_text(xhtml_doc(entry.title, "\n".join(body_parts)), encoding="utf-8")
    return filename, block_count, image_count, h2_count, subheadings


def write_generated_toc_chapter(
    text_dir: Path,
    entries: list[TocEntry],
    chapters: list[str],
    subheading_entries: list[list[SubheadingEntry]],
) -> None:
    top_items: list[str] = []
    for entry, chapter, subheadings in zip(entries, chapters, subheading_entries):
        if entry.id in {"cover", "original_toc"}:
            continue
        nav_subheadings = [sub for sub in subheadings if include_subheading_in_nav(sub)]
        nested = ""
        if nav_subheadings:
            nested_items = "\n".join(
                f'        <li><a href="{html.escape(chapter)}#{html.escape(sub.id)}">{html.escape(sub.title)}</a></li>'
                for sub in nav_subheadings
            )
            nested = f"\n      <ol class=\"toc-sub\">\n{nested_items}\n      </ol>"
        top_items.append(
            f'    <li><a href="{html.escape(chapter)}">{html.escape(entry.title)}</a>'
            f'<span class="toc-page">p{entry.page}</span>{nested}</li>'
        )
    body = "\n".join(
        [
            '<span id="original_toc"></span>',
            "<h1>目录</h1>",
            '<ol class="book-toc">',
            *top_items,
            "</ol>",
        ]
    )
    (text_dir / "original_toc.xhtml").write_text(xhtml_doc("目录", body), encoding="utf-8")


def image_viewer_title(image_file: Path) -> str:
    match = re.match(r"p(\d{4})_(\d{2})_", image_file.stem)
    if not match:
        return image_file.stem
    page_no = int(match.group(1))
    image_no = int(match.group(2))
    return f"第 {page_no} 页图 {image_no}"


def write_image_viewer_pages(oebps: Path, image_files: list[Path]) -> list[Path]:
    viewer_dir = oebps / "ImagePages"
    viewer_dir.mkdir(parents=True, exist_ok=True)
    viewer_files: list[Path] = []
    for image_file in image_files:
        image_href = image_file.relative_to(oebps).as_posix()
        title = image_viewer_title(image_file)
        viewer_file = viewer_dir / f"{image_file.stem}.xhtml"
        viewer_file.write_text(image_viewer_doc(title, image_href, title), encoding="utf-8")
        viewer_files.append(viewer_file)
    return viewer_files


def write_static_files(
    oebps: Path,
    entries: list[TocEntry],
    chapters: list[str],
    subheading_entries: list[list[SubheadingEntry]],
    image_files: list[Path],
) -> None:
    styles = oebps / "Styles"
    styles.mkdir(parents=True, exist_ok=True)
    (styles / "style.css").write_text(
        """
body {
  font-family: serif;
  line-height: 1.76;
  margin: 0 6%;
  text-align: left;
}
h1 {
  border-bottom: 1px solid #d8d8d8;
  font-size: 1.42em;
  line-height: 1.35;
  margin: 1.35em 0 0.55em;
  padding-bottom: 0.35em;
  page-break-after: avoid;
  text-align: left;
}
h2 {
  font-size: 1.08em;
  line-height: 1.45;
  margin: 1.15em 0 0.35em;
  page-break-after: avoid;
  text-align: left;
}
h2.reply {
  border-top: 1px solid #d4d4d4;
  color: #444;
  font-size: 0.98em;
  margin-top: 1.15em;
  padding-top: 0.55em;
}
p {
  margin: 0.42em 0;
  text-indent: 2em;
}
p.date {
  color: #555;
  font-size: 0.92em;
  margin: 0.1em 0 0.45em;
  text-align: right;
  text-indent: 0;
}
p.category {
  color: #555;
  font-size: 0.92em;
  line-height: 1.55;
  margin: 0.2em 0 1em;
  text-indent: 0;
}
figure {
  margin: 1.15em 0;
  page-break-inside: avoid;
  text-align: center;
}
figure a {
  text-decoration: none;
}
img {
  display: inline-block;
  max-width: 100%;
  height: auto;
}
body.cover-body {
  margin: 0;
  padding: 0;
  text-align: center;
}
.cover-page {
  margin: 0;
  padding: 0;
  text-align: center;
}
img.cover-image {
  display: block;
  height: auto;
  margin: 0 auto;
  max-height: 100vh;
  max-width: 100%;
  width: auto;
}
body.image-viewer-body {
  margin: 0;
  padding: 0;
  text-align: center;
}
.image-viewer-frame {
  display: table;
  height: 98vh;
  margin: 0;
  padding: 0;
  width: 100%;
}
.image-viewer-cell {
  display: table-cell;
  text-align: center;
  vertical-align: middle;
}
.image-original-link {
  display: inline-block;
  text-decoration: none;
}
img.viewer-image {
  display: inline-block;
  height: auto;
  margin: 0 auto;
  max-height: 98vh;
  max-width: 100%;
  width: auto;
}
.page {
  margin: 0;
}
.sep {
  border: none;
  border-top: 1px solid #bbb;
  margin: 1em 20%;
}
.title-page {
  margin-top: 18%;
  text-align: center;
}
.title-page h1 {
  font-size: 1.8em;
  text-align: center;
}
.title-page p {
  text-align: center;
  text-indent: 0;
}
.book-toc {
  margin: 0.8em 0 1.6em;
  padding-left: 1.35em;
}
.book-toc li {
  margin: 0.38em 0;
}
.book-toc a {
  text-decoration: none;
}
.toc-page {
  color: #666;
  font-size: 0.86em;
  margin-left: 0.45em;
}
.toc-sub {
  font-size: 0.92em;
  margin: 0.2em 0 0.45em;
  padding-left: 1.15em;
}
""".strip(),
        encoding="utf-8",
    )
    image_viewer_files = write_image_viewer_pages(oebps, image_files)

    nav_item_parts: list[str] = []
    for entry, chapter, subheadings in zip(entries, chapters, subheading_entries):
        nav_subheadings = [sub for sub in subheadings if include_subheading_in_nav(sub)]
        if nav_subheadings:
            nested = "\n".join(
                f'        <li><a href="Text/{chapter}#{html.escape(sub.id)}">{html.escape(sub.title)}</a></li>'
                for sub in nav_subheadings
            )
            nav_item_parts.append(
                f'    <li><a href="Text/{chapter}">{html.escape(entry.title)}</a>\n'
                f"      <ol>\n{nested}\n      </ol>\n    </li>"
            )
        else:
            nav_item_parts.append(f'    <li><a href="Text/{chapter}">{html.escape(entry.title)}</a></li>')
    nav_items = "\n".join(nav_item_parts)
    (oebps / "nav.xhtml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>目录</title>
  <link rel="stylesheet" type="text/css" href="Styles/style.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
  <h1>目录</h1>
  <ol>
{nav_items}
  </ol>
  </nav>
</body>
</html>
""",
        encoding="utf-8",
    )

    nav_points = []
    play_order = 1
    for entry, chapter, subheadings in zip(entries, chapters, subheading_entries):
        child_points: list[str] = []
        parent_order = play_order
        play_order += 1
        nav_subheadings = [sub for sub in subheadings if include_subheading_in_nav(sub)]
        for sub in nav_subheadings:
            child_points.append(
                f"""    <navPoint id="{html.escape(sub.id)}" playOrder="{play_order}">
      <navLabel><text>{html.escape(sub.title)}</text></navLabel>
      <content src="Text/{chapter}#{html.escape(sub.id)}"/>
    </navPoint>"""
            )
            play_order += 1
        children = "\n" + "\n".join(child_points) + "\n  " if child_points else ""
        nav_points.append(
            f"""  <navPoint id="{entry.id}" playOrder="{parent_order}">
    <navLabel><text>{html.escape(entry.title)}</text></navLabel>
    <content src="Text/{chapter}"/>{children}
  </navPoint>"""
        )
    (oebps / "toc.ncx").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{IDENTIFIER}"/>
    <meta name="dtb:depth" content="2"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(TITLE)}</text></docTitle>
  <navMap>
{chr(10).join(nav_points)}
  </navMap>
</ncx>
""",
        encoding="utf-8",
    )

    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="style" href="Styles/style.css" media-type="text/css"/>',
    ]
    cover_image = oebps / "Images" / "cover.jpg"
    if cover_image.exists():
        manifest_items.append('<item id="cover-image" href="Images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
    for idx, chapter in enumerate(chapters):
        manifest_items.append(
            f'<item id="chap_{idx:03d}" href="Text/{chapter}" media-type="application/xhtml+xml"/>'
        )
    for idx, viewer_path in enumerate(image_viewer_files):
        rel = viewer_path.relative_to(oebps).as_posix()
        manifest_items.append(f'<item id="viewer_{idx:04d}" href="{html.escape(rel)}" media-type="application/xhtml+xml"/>')
    for idx, image_path in enumerate(image_files):
        rel = image_path.relative_to(oebps).as_posix()
        if rel == "Images/cover.jpg":
            continue
        media_type = mimetypes.guess_type(rel)[0] or "image/jpeg"
        manifest_items.append(f'<item id="img_{idx:04d}" href="{html.escape(rel)}" media-type="{media_type}"/>')

    spine_items = [f'<itemref idref="chap_{idx:03d}"/>' for idx in range(len(chapters))]
    (oebps / "content.opf").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{IDENTIFIER}</dc:identifier>
    <dc:title>{html.escape(TITLE)}</dc:title>
    <dc:creator>{html.escape(CREATOR)}</dc:creator>
    <dc:language>{LANGUAGE}</dc:language>
    <meta name="cover" content="cover-image"/>
    <meta property="dcterms:modified">2026-07-03T00:00:00Z</meta>
  </metadata>
  <manifest>
    {chr(10).join(manifest_items)}
  </manifest>
  <spine toc="toc">
    {chr(10).join(spine_items)}
  </spine>
</package>
""",
        encoding="utf-8",
    )


def write_container(epub_root: Path) -> None:
    meta_inf = epub_root / "META-INF"
    meta_inf.mkdir(parents=True, exist_ok=True)
    (meta_inf / "container.xml").write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
""",
        encoding="utf-8",
    )
    (epub_root / "mimetype").write_text("application/epub+zip", encoding="ascii")


def zip_epub(epub_root: Path, output_file: Path) -> None:
    if output_file.exists():
        output_file.unlink()
    with zipfile.ZipFile(output_file, "w") as zf:
        zf.write(epub_root / "mimetype", "mimetype", compress_type=zipfile.ZIP_STORED)
        for path in sorted(epub_root.rglob("*")):
            if path.name == "mimetype" or path.is_dir():
                continue
            rel = path.relative_to(epub_root).as_posix()
            compression = zipfile.ZIP_STORED if rel.startswith("OEBPS/Images/") else zipfile.ZIP_DEFLATED
            zf.write(path, rel, compress_type=compression)


def normalize_audit_text(text: str) -> str:
    text = re.sub(r"[*=]{2,}", "", text)
    return re.sub(r"\s+", "", text)


def source_chapter_text(entry: TocEntry, next_page: int, pages: list[str]) -> str:
    source_lines: list[str] = []
    for page_no in range(entry.page, next_page):
        if page_no < 1 or page_no > len(pages):
            continue
        lines = page_body_lines(pages[page_no - 1], page_no)
        for line in lines:
            stripped = line.strip()
            if not stripped or re.fullmatch(r"[*=]{2,}", stripped):
                continue
            source_lines.append(stripped)
    return "".join(source_lines)


def ebook_chapter_text(xhtml_file: Path) -> str:
    root = ET.fromstring(xhtml_file.read_text(encoding="utf-8"))
    texts: list[str] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag in {"h1", "p", "h2"}:
            texts.append("".join(element.itertext()))
    return "".join(texts)


def count_chapter_h2(xhtml_file: Path) -> int:
    root = ET.fromstring(xhtml_file.read_text(encoding="utf-8"))
    return sum(1 for element in root.iter() if element.tag.rsplit("}", 1)[-1] == "h2")


def write_audit_files(
    output_dir: Path,
    output_basename: str,
    epub_file: Path,
    oebps: Path,
    entries: list[TocEntry],
    chapters: list[str],
    pages: list[str],
    image_audit: list[ImageAuditEntry],
    chapter_image_counts: list[int],
    image_stats: ImageEnhanceStats,
) -> tuple[Path, Path, Path, list[ChapterAuditEntry], bool]:
    image_hash_file = output_dir / f"{output_basename}_image_hashes.csv"
    with image_hash_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "page",
                "xobject",
                "href",
                "sha256",
                "source_sha256",
                "bytes",
                "source_bytes",
                "width",
                "height",
                "display_width_pt",
                "display_height_pt",
                "effective_dpi",
                "width_pct",
                "enhanced",
            ]
        )
        for item in image_audit:
            writer.writerow(
                [
                    item.page,
                    item.xobject,
                    item.href,
                    item.sha256,
                    item.source_sha256,
                    item.byte_count,
                    item.source_byte_count,
                    item.width,
                    item.height,
                    f"{item.display_width_pt:.2f}" if item.display_width_pt is not None else "",
                    f"{item.display_height_pt:.2f}" if item.display_height_pt is not None else "",
                    f"{item.effective_dpi:.1f}" if item.effective_dpi is not None else "",
                    f"{item.width_pct:.2f}" if item.width_pct is not None else "",
                    item.enhanced,
                ]
            )

    unique_image_hashes: dict[str, str] = {}
    for item in image_audit:
        unique_image_hashes.setdefault(item.href, item.sha256)
    epub_image_match = True
    with zipfile.ZipFile(epub_file) as zf:
        for href, expected_hash in unique_image_hashes.items():
            actual = hashlib.sha256(zf.read("OEBPS/" + href)).hexdigest()
            if actual != expected_hash:
                epub_image_match = False
                break

    chapter_audits: list[ChapterAuditEntry] = []
    for idx, (entry, chapter) in enumerate(zip(entries, chapters)):
        next_page = entries[idx + 1].page if idx + 1 < len(entries) else len(pages) + 1
        if entry.id in {"cover", "original_toc"}:
            source_norm = ""
            ebook_norm = ""
        else:
            source_norm = normalize_audit_text(source_chapter_text(entry, next_page, pages))
            ebook_norm = normalize_audit_text(ebook_chapter_text(oebps / "Text" / chapter))
        chapter_audits.append(
            ChapterAuditEntry(
                title=entry.title,
                href=chapter,
                page_start=entry.page,
                page_end=next_page - 1,
                source_chars=len(source_norm),
                ebook_chars=len(ebook_norm),
                content_match=source_norm == ebook_norm,
                h2_count=count_chapter_h2(oebps / "Text" / chapter),
                image_count=chapter_image_counts[idx],
            )
        )

    content_audit_file = output_dir / f"{output_basename}_content_audit.csv"
    with content_audit_file.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["href", "page_start", "page_end", "source_chars", "ebook_chars", "content_match", "h2_count", "image_count", "title"]
        )
        for item in chapter_audits:
            writer.writerow(
                [
                    item.href,
                    item.page_start,
                    item.page_end,
                    item.source_chars,
                    item.ebook_chars,
                    item.content_match,
                    item.h2_count,
                    item.image_count,
                    item.title,
                ]
            )

    audit_file = output_dir / f"{output_basename}_audit.txt"
    mismatches = [item for item in chapter_audits if not item.content_match]
    audit_file.write_text(
        "\n".join(
            [
                f"EPUB: {epub_file}",
                f"章节数: {len(chapter_audits)}",
                f"正文一致性: {'通过' if not mismatches else '未通过'}",
                f"正文不一致章节数: {len(mismatches)}",
                f"二级标题总数: {sum(item.h2_count for item in chapter_audits)}",
                f"有二级标题的章节数: {sum(1 for item in chapter_audits if item.h2_count > 0)}",
                f"图片引用数: {len(image_audit)}",
                f"唯一图片文件数: {len(unique_image_hashes)}",
                f"EPUB 内输出图片 hash 校验: {'通过' if epub_image_match else '未通过'}",
                f"图片增强文件数: {image_stats.enhanced}",
                f"图片未增强文件数: {image_stats.untouched}",
                f"图片增强失败/回退文件数: {image_stats.failed}",
                f"总览思维导图跳过数: {image_stats.skipped_overview_maps}",
                f"原始图片字节合计: {image_stats.source_bytes}",
                f"输出图片字节合计: {image_stats.output_bytes}",
                "",
                "目录页说明: 目录页已按最新一级/二级目录生成，不与 PDF 第 2-5 页旧目录逐字对照。",
                "导图说明: 已按阅读体验反馈移除 PDF 第 6 页导图总览图片。",
                "正文比对说明: 除生成目录页和已移除的导图总览页外，忽略 PDF 页眉/页脚、空白、换行以及仅由 * 或 = 组成的视觉分隔线；不忽略正文文字。",
                "图片比对说明: 逐一比对 EPUB 内输出图片字节与图片清单中的 SHA-256；source_sha256 保留 PDF 原始 JPEG stream 指纹。",
                "",
                "正文不一致章节:",
                *(f"{item.href}: {item.title}" for item in mismatches),
            ]
        ),
        encoding="utf-8",
    )
    return audit_file, image_hash_file, content_audit_file, chapter_audits, epub_image_match


def build(
    pdf_path: Path,
    output_dir: Path,
    pdftotext: Path | None,
    output_basename: str = "chanlun_108_lessons_angel_fullres",
    options: ImageOptions | None = None,
) -> Path:
    options = options or ImageOptions()
    output_dir.mkdir(parents=True, exist_ok=True)
    build_dir = output_dir / f"build_{output_basename}"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    epub_root = build_dir / "epub"
    oebps = epub_root / "OEBPS"
    text_dir = oebps / "Text"
    image_dir = oebps / "Images"
    text_dir.mkdir(parents=True, exist_ok=True)
    image_dir.mkdir(parents=True, exist_ok=True)

    print("读取 PDF 结构...")
    reader = PdfReader(str(pdf_path))
    print(f"PDF 页数: {len(reader.pages)}")
    entries = extract_toc_entries(reader)
    print(f"目录项初步提取: {len(entries)}")

    print("抽取版式文本...")
    pages = extract_layout_pages(pdf_path, build_dir, pdftotext)
    if len(pages) != len(reader.pages):
        print(f"警告: pdftotext 页数 {len(pages)} 与 PDF 页数 {len(reader.pages)} 不一致")

    starts = locate_lesson_starts(pages)
    notes = correct_toc_pages(entries, starts)
    if len(entries) != 111:
        print(f"警告: 预期目录项 111 个，实际 {len(entries)} 个")
    for note in notes:
        print(note)

    entries = sorted(entries, key=lambda e: e.page)
    dedupe: dict[str, tuple[str, str, int, bool]] = {}
    image_audit: list[ImageAuditEntry] = []
    image_stats = ImageEnhanceStats()
    chapters: list[str] = []
    subheading_entries: list[list[SubheadingEntry]] = []
    chapter_image_counts: list[int] = []
    chapter_h2_counts: list[int] = []
    total_blocks = 0
    total_images = 0
    print("生成 XHTML 章节并抽取图片...")
    for idx, entry in enumerate(entries):
        next_page = entries[idx + 1].page if idx + 1 < len(entries) else len(reader.pages) + 1
        if entry.id == "cover":
            filename = "cover.xhtml"
            create_clean_cover(
                image_dir / "cover.jpg",
                title=TITLE,
                subtitle="缠中说禅技术分析理论",
                range_label="全本 · 第0课 - 第108",
            )
            (text_dir / filename).write_text(cover_xhtml(TITLE), encoding="utf-8")
            block_count = 0
            image_count = 0
            h2_count = 0
            subheadings = []
        elif entry.id == "original_toc":
            filename = f"{entry.id}.xhtml"
            (text_dir / filename).write_text(
                xhtml_doc("目录", '<span id="original_toc"></span><h1>目录</h1>'),
                encoding="utf-8",
            )
            block_count = 0
            image_count = 0
            h2_count = 0
            subheadings = []
        else:
            filename, block_count, image_count, h2_count, subheadings = build_chapter_xhtml(
                entry,
                next_page,
                pages,
                reader,
                text_dir,
                image_dir,
                dedupe,
                image_audit,
                options,
                image_stats,
            )
        chapters.append(filename)
        subheading_entries.append(subheadings)
        chapter_image_counts.append(image_count)
        chapter_h2_counts.append(h2_count)
        total_blocks += block_count
        total_images += image_count
        print(
            f"{idx + 1:03d}/{len(entries)} {entry.title} "
            f"pages {entry.page}-{next_page - 1}, h2 {h2_count}, images {image_count}"
        )

    write_generated_toc_chapter(text_dir, entries, chapters, subheading_entries)
    image_files = sorted(image_dir.glob("*"))
    write_container(epub_root)
    write_static_files(oebps, entries, chapters, subheading_entries, image_files)
    epub_file = output_dir / f"{output_basename}.epub"
    zip_epub(epub_root, epub_file)
    audit_file, image_hash_file, content_audit_file, chapter_audits, epub_image_match = write_audit_files(
        output_dir,
        output_basename,
        epub_file,
        oebps,
        entries,
        chapters,
        pages,
        image_audit,
        chapter_image_counts,
        image_stats,
    )
    mismatches = [item for item in chapter_audits if not item.content_match]
    generated_toc_top_count = sum(1 for entry in entries if entry.id not in {"cover", "original_toc"})
    generated_toc_sub_count = sum(
        len([sub for sub in subheadings if include_subheading_in_nav(sub)])
        for subheadings in subheading_entries
    )

    report = output_dir / f"{output_basename}_report.txt"
    report.write_text(
        "\n".join(
            [
                f"源文件: {pdf_path}",
                f"PDF 页数: {len(reader.pages)}",
                f"目录项: {len(entries)}",
                f"生成目录一级项: {generated_toc_top_count}",
                f"生成目录二级项: {generated_toc_sub_count}",
                f"正文块: {total_blocks}",
                f"二级标题总数: {sum(chapter_h2_counts)}",
                f"图片引用: {total_images}",
                f"唯一图片文件: {len(image_files)}",
                f"EPUB: {epub_file}",
                f"审核报告: {audit_file}",
                f"图片 hash 清单: {image_hash_file}",
                f"正文比对清单: {content_audit_file}",
                f"正文一致性: {'通过' if not mismatches else '未通过'}",
                f"输出图片 hash 校验: {'通过' if epub_image_match else '未通过'}",
                f"图片增强文件数: {image_stats.enhanced}",
                f"图片增强失败/回退文件数: {image_stats.failed}",
                f"总览思维导图跳过数: {image_stats.skipped_overview_maps}",
                "目录页: 已按最新一级/二级目录重建，替换 PDF 第 2-5 页旧目录。",
                "导图总览: 已移除 PDF 第 6 页整体说明图。",
                "",
                "页码修正/提示:",
                *(notes or ["无"]),
                "",
                "目录:",
                *[f"{i + 1:03d}. p{entry.page}: {entry.title}" for i, entry in enumerate(entries)],
            ]
        ),
        encoding="utf-8",
    )
    print(f"EPUB 写入: {epub_file}")
    print(f"报告写入: {report}")
    print(f"审核写入: {audit_file}")
    return epub_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("output/ebook"))
    parser.add_argument("--output-basename", default="chanlun_108_lessons_angel_fullres")
    parser.add_argument("--pdf-image-widths", action="store_true")
    parser.add_argument("--enhance-images", action="store_true")
    parser.add_argument("--drop-overview-maps", action="store_true")
    parser.add_argument("--pdftotext", type=Path, default=None)
    args = parser.parse_args()
    pdftotext = args.pdftotext or find_default_pdftotext()
    build(
        args.pdf,
        args.output_dir,
        pdftotext,
        args.output_basename,
        ImageOptions(
            use_pdf_display_width=args.pdf_image_widths,
            enhance_images=args.enhance_images,
            drop_overview_maps=args.drop_overview_maps,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
