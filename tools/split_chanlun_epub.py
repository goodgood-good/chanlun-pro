# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import html
import mimetypes
import re
import shutil
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from cover_art import cover_xhtml, create_clean_cover


TITLE = "缠论"
CREATOR = "缠中说禅"
LANGUAGE = "zh-CN"


@dataclass
class NavEntry:
    href: str
    title: str
    subheadings: list[tuple[str, str]]


@dataclass
class Volume:
    index: int
    content_hrefs: list[str]


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if local_name(child.tag) == name]


def element_text(node: ET.Element) -> str:
    return "".join(node.itertext()).strip()


def parse_content_opf(source: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(source.read("OEBPS/content.opf"))
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    manifest = {item.attrib["id"]: item.attrib["href"] for item in root.findall(".//opf:item", ns)}
    spine: list[str] = []
    for itemref in root.findall(".//opf:spine/opf:itemref", ns):
        href = manifest[itemref.attrib["idref"]]
        if href.startswith("Text/"):
            spine.append(href)
    return spine


def parse_nav(source: zipfile.ZipFile) -> dict[str, NavEntry]:
    root = ET.fromstring(source.read("OEBPS/nav.xhtml"))
    toc_nav = None
    for node in root.iter():
        if local_name(node.tag) == "nav" and any(key.endswith("type") and value == "toc" for key, value in node.attrib.items()):
            toc_nav = node
            break
    if toc_nav is None:
        raise RuntimeError("nav.xhtml 中没有找到 toc")
    top_ol = children(toc_nav, "ol")[0]
    entries: dict[str, NavEntry] = {}
    for li in children(top_ol, "li"):
        link = children(li, "a")[0]
        href = link.attrib["href"]
        title = element_text(link)
        subheadings: list[tuple[str, str]] = []
        nested = children(li, "ol")
        if nested:
            for sub_li in children(nested[0], "li"):
                sub_link = children(sub_li, "a")[0]
                subheadings.append((sub_link.attrib["href"], element_text(sub_link)))
        entries[href] = NavEntry(href=href, title=title, subheadings=subheadings)
    return entries


def parse_page_starts(content_audit: Path | None) -> dict[str, int]:
    if not content_audit or not content_audit.exists():
        return {}
    result: dict[str, int] = {}
    with content_audit.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            href = row.get("href", "")
            page = row.get("page_start", "")
            if href and page.isdigit():
                result[f"Text/{href}"] = int(page)
    return result


def image_refs(source: zipfile.ZipFile, text_href: str) -> set[str]:
    data = source.read("OEBPS/" + text_href).decode("utf-8")
    return {f"OEBPS/Images/{match}" for match in re.findall(r"\.\./Images/([^\"']+)", data)}


def image_page_refs(source: zipfile.ZipFile, text_href: str) -> set[str]:
    data = source.read("OEBPS/" + text_href).decode("utf-8")
    return {f"OEBPS/ImagePages/{match}" for match in re.findall(r"\.\./ImagePages/([^\"']+)", data)}


def estimate_text_unit_size(
    source: zipfile.ZipFile,
    text_href: str,
    already_counted_images: set[str],
    already_counted_image_pages: set[str],
) -> tuple[int, set[str], set[str]]:
    refs = image_refs(source, text_href)
    page_refs = image_page_refs(source, text_href)
    new_refs = refs - already_counted_images
    new_page_refs = page_refs - already_counted_image_pages
    size = source.getinfo("OEBPS/" + text_href).file_size
    size += sum(source.getinfo(ref).file_size for ref in new_refs)
    size += sum(source.getinfo(ref).file_size for ref in new_page_refs)
    return size, refs, page_refs


def split_volumes(source: zipfile.ZipFile, spine: list[str], target_bytes: int, max_bytes: int) -> list[Volume]:
    content_hrefs = [href for href in spine if href not in {"Text/cover.xhtml", "Text/original_toc.xhtml"}]
    volumes: list[Volume] = []
    current: list[str] = []
    current_images: set[str] = set()
    current_image_pages: set[str] = set()
    current_size = 0

    for href in content_hrefs:
        unit_size, refs, page_refs = estimate_text_unit_size(source, href, current_images, current_image_pages)
        if unit_size > max_bytes:
            raise RuntimeError(f"{href} 单章估算已超过限制: {unit_size} bytes")
        if current and current_size + unit_size > target_bytes:
            volumes.append(Volume(index=len(volumes) + 1, content_hrefs=current))
            current = []
            current_images = set()
            current_image_pages = set()
            current_size = 0
            unit_size, refs, page_refs = estimate_text_unit_size(source, href, current_images, current_image_pages)
        current.append(href)
        current_images |= refs
        current_image_pages |= page_refs
        current_size += unit_size
    if current:
        volumes.append(Volume(index=len(volumes) + 1, content_hrefs=current))
    return volumes


def lesson_number(href: str) -> int | None:
    match = re.search(r"lesson_(\d{3})\.xhtml$", href)
    return int(match.group(1)) if match else None


def volume_range_label(volume: Volume) -> str:
    lessons = [lesson_number(href) for href in volume.content_hrefs]
    lessons = [num for num in lessons if num is not None]
    if not lessons:
        return "导图总览"
    start = lessons[0]
    end = lessons[-1]
    if volume.content_hrefs[0].endswith("map.xhtml"):
        return f"导图总览-第{end}课"
    if start == end:
        return f"第{start}课"
    return f"第{start}课-第{end}课"


def filename_range_label(volume: Volume) -> str:
    label = volume_range_label(volume)
    return label.removesuffix("第108课") + "第108" if label.endswith("第108课") else label


def write_container(epub_root: Path) -> None:
    (epub_root / "META-INF").mkdir(parents=True, exist_ok=True)
    (epub_root / "META-INF" / "container.xml").write_text(
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


def volume_title(volume: Volume, total: int) -> str:
    return f"{TITLE} 第{volume.index:02d}卷 {filename_range_label(volume)}"


def relative_text_href(href: str) -> str:
    return href.removeprefix("Text/")


def write_generated_toc(
    text_dir: Path,
    volume: Volume,
    total: int,
    nav_entries: dict[str, NavEntry],
    page_starts: dict[str, int],
) -> None:
    top_items: list[str] = []
    for href in volume.content_hrefs:
        entry = nav_entries[href]
        page = page_starts.get(href)
        page_html = f'<span class="toc-page">p{page}</span>' if page else ""
        nested = ""
        if entry.subheadings:
            nested_items = []
            for sub_href, sub_title in entry.subheadings:
                nested_items.append(
                    f'        <li><a href="{html.escape(relative_text_href(sub_href))}">{html.escape(sub_title)}</a></li>'
                )
            nested = "\n      <ol class=\"toc-sub\">\n" + "\n".join(nested_items) + "\n      </ol>"
        top_items.append(
            f'    <li><a href="{html.escape(relative_text_href(href))}">{html.escape(entry.title)}</a>{page_html}{nested}</li>'
        )

    body = "\n".join(
        [
            '<span id="original_toc"></span>',
            "<h1>目录</h1>",
            f'<p class="volume-note">第{volume.index}卷 / 共{total}卷：{html.escape(volume_range_label(volume))}</p>',
            '<ol class="book-toc">',
            *top_items,
            "</ol>",
        ]
    )
    (text_dir / "original_toc.xhtml").write_text(xhtml_doc("目录", body), encoding="utf-8")


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


def write_nav_and_ncx(
    oebps: Path,
    volume: Volume,
    total: int,
    nav_entries: dict[str, NavEntry],
    text_hrefs: list[str],
) -> None:
    title = volume_title(volume, total)
    nav_items: list[str] = []
    for href in text_hrefs:
        if href == "Text/original_toc.xhtml":
            nav_items.append(f'    <li><a href="{html.escape(href)}">目录</a></li>')
            continue
        entry = nav_entries[href]
        nested = ""
        if entry.subheadings and href in volume.content_hrefs:
            nested_items = "\n".join(
                f'        <li><a href="{html.escape(sub_href)}">{html.escape(sub_title)}</a></li>'
                for sub_href, sub_title in entry.subheadings
            )
            nested = f"\n      <ol>\n{nested_items}\n      </ol>\n    "
        nav_items.append(f'    <li><a href="{html.escape(href)}">{html.escape(entry.title)}</a>{nested}</li>')

    (oebps / "nav.xhtml").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head>
  <meta charset="utf-8"/>
  <title>{html.escape(title)}</title>
  <link rel="stylesheet" type="text/css" href="Styles/style.css"/>
</head>
<body>
  <nav epub:type="toc" id="toc">
  <h1>目录</h1>
  <ol>
{chr(10).join(nav_items)}
  </ol>
  </nav>
</body>
</html>
""",
        encoding="utf-8",
    )

    play_order = 1
    nav_points: list[str] = []
    for href in text_hrefs:
        if href == "Text/original_toc.xhtml":
            label = "目录"
            children_xml = ""
        else:
            entry = nav_entries[href]
            label = entry.title
            child_points: list[str] = []
            if href in volume.content_hrefs:
                for sub_href, sub_title in entry.subheadings:
                    child_points.append(
                        f"""    <navPoint id="vol{volume.index:02d}_{play_order:04d}" playOrder="{play_order + 1}">
      <navLabel><text>{html.escape(sub_title)}</text></navLabel>
      <content src="{html.escape(sub_href)}"/>
    </navPoint>"""
                    )
                    play_order += 1
            children_xml = "\n" + "\n".join(child_points) + "\n  " if child_points else ""
        nav_points.append(
            f"""  <navPoint id="vol{volume.index:02d}_{play_order:04d}" playOrder="{play_order}">
    <navLabel><text>{html.escape(label)}</text></navLabel>
    <content src="{html.escape(href)}"/>{children_xml}
  </navPoint>"""
        )
        play_order += 1

    identifier = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"chanlun-reading-volume-v2-{volume.index}"))
    (oebps / "toc.ncx").write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{identifier}"/>
    <meta name="dtb:depth" content="2"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(title)}</text></docTitle>
  <navMap>
{chr(10).join(nav_points)}
  </navMap>
</ncx>
""",
        encoding="utf-8",
    )


def write_opf(
    oebps: Path,
    volume: Volume,
    total: int,
    text_hrefs: list[str],
    image_hrefs: list[str],
    image_page_hrefs: list[str],
) -> None:
    identifier = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"chanlun-reading-volume-v2-{volume.index}"))
    manifest_items = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="toc" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '<item id="style" href="Styles/style.css" media-type="text/css"/>',
    ]
    if "Images/cover.jpg" in image_hrefs:
        manifest_items.append('<item id="cover-image" href="Images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
    for idx, href in enumerate(text_hrefs):
        manifest_items.append(f'<item id="chap_{idx:03d}" href="{html.escape(href)}" media-type="application/xhtml+xml"/>')
    for idx, href in enumerate(image_page_hrefs):
        manifest_items.append(f'<item id="viewer_{idx:04d}" href="{html.escape(href)}" media-type="application/xhtml+xml"/>')
    for idx, href in enumerate(image_hrefs):
        if href == "Images/cover.jpg":
            continue
        media_type = mimetypes.guess_type(href)[0] or "image/jpeg"
        manifest_items.append(f'<item id="img_{idx:04d}" href="{html.escape(href)}" media-type="{media_type}"/>')
    spine_items = [f'<itemref idref="chap_{idx:03d}"/>' for idx in range(len(text_hrefs))]
    (oebps / "content.opf").write_text(
        f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">{identifier}</dc:identifier>
    <dc:title>{html.escape(volume_title(volume, total))}</dc:title>
    <dc:creator>{html.escape(CREATOR)}</dc:creator>
    <dc:language>{LANGUAGE}</dc:language>
    <meta name="cover" content="cover-image"/>
    <meta property="dcterms:modified">2026-07-04T00:00:00Z</meta>
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


def copy_source_file(source: zipfile.ZipFile, zip_name: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(source.read(zip_name))


def build_volume(
    source: zipfile.ZipFile,
    volume: Volume,
    total: int,
    nav_entries: dict[str, NavEntry],
    page_starts: dict[str, int],
    work_dir: Path,
    output_dir: Path,
    filename_prefix: str,
) -> dict[str, str | int]:
    epub_root = work_dir / f"vol{volume.index:02d}" / "epub"
    if epub_root.exists():
        shutil.rmtree(epub_root)
    oebps = epub_root / "OEBPS"
    text_dir = oebps / "Text"
    images_dir = oebps / "Images"
    image_pages_dir = oebps / "ImagePages"
    styles_dir = oebps / "Styles"
    text_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)
    image_pages_dir.mkdir(parents=True, exist_ok=True)
    styles_dir.mkdir(parents=True, exist_ok=True)
    write_container(epub_root)

    copy_source_file(source, "OEBPS/Styles/style.css", styles_dir / "style.css")

    volume_label = f"第{volume.index:02d}卷"
    range_label = filename_range_label(volume)
    create_clean_cover(
        images_dir / "cover.jpg",
        title=TITLE,
        subtitle="缠中说禅技术分析理论",
        range_label=range_label,
        volume_label=volume_label,
    )
    (text_dir / "cover.xhtml").write_text(cover_xhtml(volume_title(volume, total)), encoding="utf-8")

    text_hrefs = ["Text/cover.xhtml", "Text/original_toc.xhtml"]
    for href in volume.content_hrefs:
        text_hrefs.append(href)
        copy_source_file(source, "OEBPS/" + href, text_dir / Path(href).name)

    write_generated_toc(text_dir, volume, total, nav_entries, page_starts)

    image_zip_names: set[str] = set()
    image_page_zip_names: set[str] = set()
    for href in text_hrefs:
        if href in {"Text/cover.xhtml", "Text/original_toc.xhtml"}:
            continue
        image_zip_names |= image_refs(source, href)
        image_page_zip_names |= image_page_refs(source, href)
    for zip_name in sorted(image_zip_names):
        copy_source_file(source, zip_name, images_dir / Path(zip_name).name)
    for zip_name in sorted(image_page_zip_names):
        copy_source_file(source, zip_name, image_pages_dir / Path(zip_name).name)

    image_hrefs = ["Images/cover.jpg"] + [f"Images/{Path(name).name}" for name in sorted(image_zip_names)]
    image_page_hrefs = [f"ImagePages/{Path(name).name}" for name in sorted(image_page_zip_names)]
    write_nav_and_ncx(oebps, volume, total, nav_entries, text_hrefs)
    write_opf(oebps, volume, total, text_hrefs, image_hrefs, image_page_hrefs)

    output_file = output_dir / f"{filename_prefix}_第{volume.index:02d}卷_{filename_range_label(volume)}.epub"
    zip_epub(epub_root, output_file)
    return {
        "volume": volume.index,
        "range": volume_range_label(volume),
        "chapters": len(volume.content_hrefs),
        "images": len(image_zip_names),
        "epub": str(output_file),
        "bytes": output_file.stat().st_size,
    }


def prepare_output_dir(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for child in output_dir.iterdir():
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("output/ebook/chanlun_108_lessons_angel_fullres.epub"))
    parser.add_argument("--content-audit", type=Path, default=Path("output/ebook/chanlun_108_lessons_angel_fullres_content_audit.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/ebook/split_200m"))
    parser.add_argument("--filename-prefix", default="缠论")
    parser.add_argument("--target-bytes", type=int, default=185_000_000)
    parser.add_argument("--max-bytes", type=int, default=200_000_000)
    args = parser.parse_args()

    prepare_output_dir(args.output_dir)
    work_dir = args.output_dir / "_build"
    work_dir.mkdir(parents=True, exist_ok=True)

    page_starts = parse_page_starts(args.content_audit)
    with zipfile.ZipFile(args.source) as source:
        spine = parse_content_opf(source)
        nav_entries = parse_nav(source)
        volumes = split_volumes(source, spine, args.target_bytes, args.max_bytes)
        rows = [
            build_volume(source, volume, len(volumes), nav_entries, page_starts, work_dir, args.output_dir, args.filename_prefix)
            for volume in volumes
        ]

    report = args.output_dir / "split_report.csv"
    with report.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["volume", "range", "chapters", "images", "bytes", "epub"])
        writer.writeheader()
        writer.writerows(rows)

    root = work_dir.resolve()
    output_root = args.output_dir.resolve()
    if output_root in root.parents and root.name == "_build":
        shutil.rmtree(root)

    print(f"volumes: {len(rows)}")
    for row in rows:
        print(f"vol{row['volume']:02d}: {row['bytes']} bytes, {row['range']}, {row['epub']}")
    print(f"report: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
