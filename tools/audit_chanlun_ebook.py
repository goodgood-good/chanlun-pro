# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import posixpath
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from pypdf import PdfReader


MAX_SPLIT_BYTES = 200_000_000


@dataclass
class Finding:
    severity: str
    area: str
    file: str
    detail: str


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if local_name(child.tag) == name]


def element_text(node: ET.Element) -> str:
    return "".join(node.itertext()).strip()


def load_builder(script_path: Path):
    spec = importlib.util.spec_from_file_location("chanlun_builder_for_audit", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def normalize_text(text: str) -> str:
    text = re.sub(r"[*=]{2,}", "", text)
    return re.sub(r"\s+", "", text)


def parse_opf_spine(zf: zipfile.ZipFile) -> list[str]:
    root = ET.fromstring(zf.read("OEBPS/content.opf"))
    ns = {"opf": "http://www.idpf.org/2007/opf"}
    manifest = {item.attrib["id"]: item.attrib["href"] for item in root.findall(".//opf:item", ns)}
    return [
        manifest[item.attrib["idref"]]
        for item in root.findall(".//opf:spine/opf:itemref", ns)
        if item.attrib.get("linear", "yes") != "no"
    ]


def collect_ids(zf: zipfile.ZipFile, text_files: list[str]) -> dict[str, set[str]]:
    ids: dict[str, set[str]] = {}
    for href in text_files:
        root = ET.fromstring(zf.read("OEBPS/" + href))
        values = {node.attrib["id"] for node in root.iter() if "id" in node.attrib}
        ids[href] = values
    return ids


def parse_xhtml(zf: zipfile.ZipFile, href: str) -> ET.Element:
    return ET.fromstring(zf.read("OEBPS/" + href))


def iter_links(root: ET.Element) -> list[str]:
    links: list[str] = []
    for node in root.iter():
        if local_name(node.tag) == "a" and "href" in node.attrib:
            links.append(node.attrib["href"])
    return links


def ebook_visible_text(root: ET.Element) -> str:
    texts: list[str] = []
    for node in root.iter():
        if local_name(node.tag) in {"h1", "p", "h2"}:
            texts.append(element_text(node))
    return "".join(texts)


def resolve_href(base_href: str, href: str) -> tuple[str, str | None]:
    if href.startswith(("http://", "https://", "mailto:")):
        return href, None
    path, _, anchor = href.partition("#")
    if not path:
        target = base_href
    elif path.startswith("Text/"):
        target = path
    else:
        base_dir = posixpath.dirname(base_href)
        target = posixpath.normpath(posixpath.join(base_dir, path))
    return target, anchor or None


def nav_entries(zf: zipfile.ZipFile) -> tuple[int, int, list[str], list[str]]:
    root = ET.fromstring(zf.read("OEBPS/nav.xhtml"))
    toc_nav = None
    for node in root.iter():
        if local_name(node.tag) == "nav" and any(key.endswith("type") and value == "toc" for key, value in node.attrib.items()):
            toc_nav = node
            break
    if toc_nav is None:
        return 0, 0, [], []
    top_ol = children(toc_nav, "ol")[0]
    top_labels: list[str] = []
    nested_labels: list[str] = []
    for li in children(top_ol, "li"):
        top_link = children(li, "a")
        if top_link:
            top_labels.append(element_text(top_link[0]))
        for nested_ol in children(li, "ol"):
            for sub_li in children(nested_ol, "li"):
                sub_link = children(sub_li, "a")
                if sub_link:
                    nested_labels.append(element_text(sub_link[0]))
    return len(top_labels), len(nested_labels), top_labels, nested_labels


def malformed_date_label(label: str) -> bool:
    if not re.search(r"\d{4}-\d{2}-\d{2}", label):
        return False
    return not bool(re.search(r"[\(（]\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{2}[\)）]", label))


def audit_main_epub(
    builder,
    pdf_path: Path,
    epub_path: Path,
    output_dir: Path,
    findings: list[Finding],
) -> dict[str, int | bool]:
    tmp = Path(tempfile.mkdtemp(prefix="chanlun_audit_", dir=output_dir))
    try:
        reader = PdfReader(str(pdf_path))
        pages = builder.extract_layout_pages(pdf_path, tmp, builder.find_default_pdftotext())
        entries = builder.extract_toc_entries(reader)
        notes = builder.correct_toc_pages(entries, builder.locate_lesson_starts(pages))
        entries = sorted(entries, key=lambda entry: entry.page)
        lesson_entries = [entry for entry in entries if entry.num is not None]
        if len(reader.pages) != 2533:
            findings.append(Finding("error", "pdf", str(pdf_path), f"PDF 页数异常: {len(reader.pages)}"))
        if len(pages) != len(reader.pages):
            findings.append(Finding("error", "pdf", str(pdf_path), f"pdftotext 页数 {len(pages)} != PDF 页数 {len(reader.pages)}"))
        if len(entries) != 111:
            findings.append(Finding("error", "toc", "entries", f"目录项应为 111，实际 {len(entries)}"))
        expected_pages = list(range(7, len(reader.pages) + 1))
        actual_pages: list[int] = []
        for idx, entry in enumerate(lesson_entries):
            next_page = lesson_entries[idx + 1].page if idx + 1 < len(lesson_entries) else len(reader.pages) + 1
            actual_pages.extend(range(entry.page, next_page))
        if actual_pages != expected_pages:
            findings.append(Finding("error", "coverage", "lessons", "第 0-108 课未连续覆盖 PDF 第 7-2533 页"))

        with zipfile.ZipFile(epub_path) as zf:
            names = set(zf.namelist())
            if "OEBPS/Text/map.xhtml" in names:
                findings.append(Finding("error", "map", "OEBPS/Text/map.xhtml", "导图章节仍在 EPUB 内"))
            if any(name.startswith("OEBPS/Images/p0006_") for name in names):
                findings.append(Finding("error", "map", "OEBPS/Images/p0006_*", "PDF 第 6 页导图图片仍在 EPUB 内"))
            spine = parse_opf_spine(zf)
            text_files = [href for href in spine if href.startswith("Text/")]
            expected_spine = ["Text/cover.xhtml", "Text/original_toc.xhtml"] + [f"Text/lesson_{i:03d}.xhtml" for i in range(109)]
            if text_files != expected_spine:
                findings.append(Finding("error", "spine", "OEBPS/content.opf", "spine 与 cover/目录/lesson_000-108 顺序不一致"))

            ids = collect_ids(zf, text_files)
            resource_files = {name.removeprefix("OEBPS/") for name in zf.namelist() if name.startswith("OEBPS/")}
            image_page_files = sorted(
                name.removeprefix("OEBPS/")
                for name in zf.namelist()
                if name.startswith("OEBPS/ImagePages/") and name.endswith(".xhtml")
            )
            link_rows: list[dict[str, str]] = []
            for href in ["nav.xhtml", *text_files, *image_page_files]:
                root = ET.fromstring(zf.read("OEBPS/" + href))
                base = href if href.startswith("Text/") else ""
                if href.startswith("ImagePages/"):
                    base = href
                for link in iter_links(root):
                    target, anchor = resolve_href(base, link)
                    ok = target in ids or target in {"nav.xhtml"} or target in resource_files
                    anchor_ok = True
                    if ok and anchor and target in ids:
                        anchor_ok = anchor in ids[target]
                    if not ok or not anchor_ok:
                        findings.append(Finding("error", "links", href, f"断链: {link} -> {target}#{anchor or ''}"))
                    link_rows.append({"source": href, "href": link, "target": target, "anchor": anchor or "", "ok": str(ok and anchor_ok)})
            write_csv(output_dir / "global_link_audit.csv", link_rows, ["source", "href", "target", "anchor", "ok"])

            top_count, nested_count, top_labels, nested_labels = nav_entries(zf)
            bad_nested = [
                label
                for label in nested_labels
                if "[匿名]" in label
                or "匿名]" in label
                or re.match(r"^[\(（]?\d{4}-\d{2}-\d{2}", label)
                or re.match(r"^\s*(?:\d+|[０-９]+|[一二三四五六七八九十百]+)[、.．]", label)
                or label.startswith("教你炒股票")
                or label.startswith("缠中说禅：")
                or len(label) > 64
                or re.search(r"[，,；;：:]$", label)
                or "？" in label
                or "?" in label
            ]
            for label in bad_nested[:50]:
                findings.append(Finding("error", "nav", "OEBPS/nav.xhtml", f"二级目录不应出现: {label}"))
            for label in top_labels:
                if malformed_date_label(label) and label not in {"封面", "目录"}:
                    findings.append(Finding("warning", "nav-title", "OEBPS/nav.xhtml", f"一级目录日期疑似异常: {label}"))

            chapter_rows: list[dict[str, str | int | bool]] = []
            layout_rows: list[dict[str, str | int]] = []
            for idx, entry in enumerate(entries):
                chapter = expected_spine[idx]
                next_page = entries[idx + 1].page if idx + 1 < len(entries) else len(pages) + 1
                root = parse_xhtml(zf, chapter)
                h1s = [element_text(node) for node in root.iter() if local_name(node.tag) == "h1"]
                h2s = [element_text(node) for node in root.iter() if local_name(node.tag) == "h2"]
                ps = [element_text(node) for node in root.iter() if local_name(node.tag) == "p"]
                dates = [element_text(node) for node in root.iter() if local_name(node.tag) == "p" and node.attrib.get("class") == "date"]
                categories = [element_text(node) for node in root.iter() if local_name(node.tag) == "p" and node.attrib.get("class") == "category"]
                if entry.num is not None:
                    if len(h1s) != 1:
                        findings.append(Finding("error", "layout", chapter, f"课文章节 h1 数量异常: {len(h1s)}"))
                    if dates and not re.fullmatch(r"[\(（]\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}:\d{1,2}[\)）]", dates[0]):
                        findings.append(Finding("warning", "layout", chapter, f"日期格式疑似异常: {dates[0]}"))
                    if h1s and ("在流程图" in h1s[0] or re.search(r"\d{4}-\d{2}-\d{2}", h1s[0])):
                        findings.append(Finding("warning", "layout", chapter, f"h1 混入日期或分类: {h1s[0]}"))
                    if categories and "在流程图" not in categories[0]:
                        findings.append(Finding("warning", "layout", chapter, f"分类说明疑似异常: {categories[0]}"))

                source_norm = "" if entry.id in {"cover", "original_toc"} else normalize_text(builder.source_chapter_text(entry, next_page, pages))
                ebook_norm = "" if entry.id in {"cover", "original_toc"} else normalize_text(builder.ebook_chapter_text(Path(tmp / "dummy"))) if False else ""
                if entry.id not in {"cover", "original_toc"}:
                    ebook_text = ebook_visible_text(root)
                    ebook_norm = normalize_text(ebook_text)
                    if source_norm != ebook_norm:
                        findings.append(Finding("error", "text", chapter, "正文归一化后与 PDF 来源不一致"))

                for node in root.iter():
                    tag = local_name(node.tag)
                    if tag not in {"p", "h2"}:
                        continue
                    text = element_text(node)
                    if not text:
                        continue
                    if "\ufffd" in text:
                        layout_rows.append({"file": chapter, "kind": tag, "severity": "error", "detail": "包含替换字符 U+FFFD", "text": text[:240]})
                    if tag == "p" and len(text) > 1200:
                        layout_rows.append({"file": chapter, "kind": tag, "severity": "warning", "detail": f"段落过长 {len(text)} 字", "text": text[:240]})
                    if tag == "p" and re.search(r"[A-Za-z0-9][\u4e00-\u9fff]", text):
                        layout_rows.append({"file": chapter, "kind": tag, "severity": "info", "detail": "ASCII 与中文无空格相邻", "text": text[:240]})
                    if tag == "h2" and len(text) > 90:
                        layout_rows.append({"file": chapter, "kind": tag, "severity": "warning", "detail": f"h2 过长 {len(text)} 字", "text": text[:240]})
                    if tag == "h2" and re.match(r"^\d+[、.．]\d", text) and not re.search(r"[\(（]\d{4}-\d{2}-\d{2}", text):
                        layout_rows.append({"file": chapter, "kind": tag, "severity": "warning", "detail": "疑似数字正文误判为 h2", "text": text[:240]})
                    if tag == "h2" and re.search(r"[，,]$", text):
                        layout_rows.append({"file": chapter, "kind": tag, "severity": "warning", "detail": "h2 以逗号结尾，疑似断行误判", "text": text[:240]})

                chapter_rows.append(
                    {
                        "file": chapter,
                        "title": entry.title,
                        "page_start": entry.page,
                        "page_end": next_page - 1,
                        "h1_count": len(h1s),
                        "h2_count": len(h2s),
                        "p_count": len(ps),
                        "date_count": len(dates),
                        "category_count": len(categories),
                        "source_chars": len(source_norm),
                        "ebook_chars": len(ebook_norm),
                        "text_match": str(source_norm == ebook_norm),
                    }
                )
            write_csv(
                output_dir / "global_chapter_audit.csv",
                chapter_rows,
                ["file", "title", "page_start", "page_end", "h1_count", "h2_count", "p_count", "date_count", "category_count", "source_chars", "ebook_chars", "text_match"],
            )
            write_csv(output_dir / "global_layout_findings.csv", layout_rows, ["file", "kind", "severity", "detail", "text"])

            image_infos = [info for info in zf.infolist() if info.filename.startswith("OEBPS/Images/")]
            if any(info.compress_type != zipfile.ZIP_STORED for info in image_infos):
                findings.append(Finding("error", "images", "OEBPS/Images", "存在压缩存储的图片"))

            return {
                "pdf_pages": len(reader.pages),
                "layout_pages": len(pages),
                "entries": len(entries),
                "lessons": len(lesson_entries),
                "spine_items": len(text_files),
                "nav_top": top_count,
                "nav_nested": nested_count,
                "bad_nested": len(bad_nested),
                "images": len(image_infos),
                "notes": len(notes),
                "layout_findings": len(layout_rows),
            }
    finally:
        safe_remove_tmp(tmp, output_dir)


def audit_images_from_csv(epub_path: Path, image_csv: Path, findings: list[Finding]) -> dict[str, int]:
    expected: dict[str, str] = {}
    refs = 0
    with image_csv.open("r", encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            refs += 1
            expected.setdefault(row["href"], row["sha256"])
    missing = 0
    mismatches = 0
    with zipfile.ZipFile(epub_path) as zf:
        for href, sha in expected.items():
            zip_name = "OEBPS/" + href
            try:
                data = zf.read(zip_name)
            except KeyError:
                missing += 1
                continue
            if hashlib.sha256(data).hexdigest() != sha:
                mismatches += 1
    if missing:
        findings.append(Finding("error", "images", str(image_csv), f"EPUB 缺少 {missing} 个图片文件"))
    if mismatches:
        findings.append(Finding("error", "images", str(image_csv), f"图片哈希不一致 {mismatches} 个"))
    return {"image_refs": refs, "unique_images": len(expected), "missing_images": missing, "image_hash_mismatches": mismatches}


def audit_split(source_epub: Path, split_dir: Path, output_dir: Path, findings: list[Finding]) -> dict[str, int | bool]:
    epub_files = sorted(split_dir.glob("*.epub"))
    azw3_files = sorted(split_dir.glob("*.azw3"))
    rows: list[dict[str, str | int | bool]] = []
    with zipfile.ZipFile(source_epub) as source:
        generated_hrefs = {"Text/cover.xhtml", "Text/original_toc.xhtml"}
        source_spine = [href for href in parse_opf_spine(source) if href not in generated_hrefs]
        actual_spine: list[str] = []
        text_bad = 0
        image_bad = 0
        compressed = 0
        map_files = 0
        nav_nested = 0
        for epub in epub_files:
            with zipfile.ZipFile(epub) as zf:
                names = set(zf.namelist())
                if "OEBPS/Text/map.xhtml" in names or any(name.startswith("OEBPS/Images/p0006_") for name in names):
                    map_files += 1
                spine = parse_opf_spine(zf)
                actual_spine.extend(href for href in spine if href not in generated_hrefs)
                for href in spine:
                    if href in generated_hrefs:
                        continue
                    zip_name = "OEBPS/" + href
                    if hashlib.sha256(zf.read(zip_name)).hexdigest() != hashlib.sha256(source.read(zip_name)).hexdigest():
                        text_bad += 1
                image_infos = [info for info in zf.infolist() if info.filename.startswith("OEBPS/Images/")]
                for info in image_infos:
                    if info.compress_type != zipfile.ZIP_STORED:
                        compressed += 1
                    if info.filename == "OEBPS/Images/cover.jpg":
                        continue
                    if hashlib.sha256(zf.read(info.filename)).hexdigest() != hashlib.sha256(source.read(info.filename)).hexdigest():
                        image_bad += 1
                top_count, nested_count, _, _ = nav_entries(zf)
                nav_nested += nested_count
                rows.append(
                    {
                        "file": epub.name,
                        "type": "epub",
                        "bytes": epub.stat().st_size,
                        "under_200m": epub.stat().st_size < MAX_SPLIT_BYTES,
                        "spine_items": len(spine),
                        "nav_top": top_count,
                        "nav_nested": nested_count,
                    }
                )
        sequence_ok = actual_spine == source_spine
        if not sequence_ok:
            findings.append(Finding("error", "split", str(split_dir), "分卷章节顺序与完整 EPUB 不一致"))
        if text_bad:
            findings.append(Finding("error", "split", str(split_dir), f"分卷 XHTML 与完整 EPUB 不一致: {text_bad}"))
        if image_bad:
            findings.append(Finding("error", "split", str(split_dir), f"分卷图片哈希与完整 EPUB 不一致: {image_bad}"))
        if compressed:
            findings.append(Finding("error", "split", str(split_dir), f"分卷存在压缩图片: {compressed}"))
        if map_files:
            findings.append(Finding("error", "split", str(split_dir), f"分卷仍包含导图文件: {map_files}"))
    for azw3 in azw3_files:
        rows.append(
            {
                "file": azw3.name,
                "type": "azw3",
                "bytes": azw3.stat().st_size,
                "under_200m": azw3.stat().st_size < MAX_SPLIT_BYTES,
                "spine_items": "",
                "nav_top": "",
                "nav_nested": "",
            }
        )
    too_large = [row["file"] for row in rows if not row["under_200m"]]
    for file in too_large:
        findings.append(Finding("error", "split-size", file, "分卷文件超过 200M"))
    write_csv(output_dir / "global_split_audit.csv", rows, ["file", "type", "bytes", "under_200m", "spine_items", "nav_top", "nav_nested"])
    return {
        "split_epubs": len(epub_files),
        "split_azw3": len(azw3_files),
        "split_sequence_ok": sequence_ok,
        "split_text_bad": text_bad,
        "split_image_bad": image_bad,
        "split_compressed_images": compressed,
        "split_map_files": map_files,
        "split_too_large": len(too_large),
        "split_nested": nav_nested,
    }


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def safe_remove_tmp(tmp: Path, output_dir: Path) -> None:
    import shutil

    resolved = tmp.resolve()
    root = output_dir.resolve()
    if root in resolved.parents and resolved.name.startswith("chanlun_audit_"):
        shutil.rmtree(resolved)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, default=Path(r"C:\Users\lc\Desktop\教你炒股票108课天使版.pdf"))
    parser.add_argument("--epub", type=Path, default=Path(r"D:\project\chanlun-pro\output\ebook\chanlun_108_lessons_angel_fullres.epub"))
    parser.add_argument("--image-csv", type=Path, default=Path(r"D:\project\chanlun-pro\output\ebook\chanlun_108_lessons_angel_fullres_image_hashes.csv"))
    parser.add_argument("--split-dir", type=Path, default=Path(r"D:\project\chanlun-pro\output\ebook\split_200m"))
    parser.add_argument("--output-dir", type=Path, default=Path(r"D:\project\chanlun-pro\output\ebook\global_audit"))
    parser.add_argument("--builder", type=Path, default=Path(r"D:\project\chanlun-pro\tools\build_chanlun_ebook.py"))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    findings: list[Finding] = []
    builder = load_builder(args.builder)
    summary: dict[str, int | bool] = {}
    summary.update(audit_main_epub(builder, args.pdf, args.epub, args.output_dir, findings))
    summary.update(audit_images_from_csv(args.epub, args.image_csv, findings))
    summary.update(audit_split(args.epub, args.split_dir, args.output_dir, findings))

    finding_rows = [{"severity": item.severity, "area": item.area, "file": item.file, "detail": item.detail} for item in findings]
    write_csv(args.output_dir / "global_findings.csv", finding_rows, ["severity", "area", "file", "detail"])

    error_count = sum(1 for item in findings if item.severity == "error")
    warning_count = sum(1 for item in findings if item.severity == "warning")
    info_count = sum(1 for item in findings if item.severity == "info")
    summary_lines = [
        f"PDF: {args.pdf}",
        f"EPUB: {args.epub}",
        f"输出目录: {args.output_dir}",
        f"错误: {error_count}",
        f"警告: {warning_count}",
        f"提示: {info_count}",
        "",
        "审查指标:",
        *[f"{key}: {value}" for key, value in summary.items()],
        "",
        "明细文件:",
        f"global_findings.csv: {args.output_dir / 'global_findings.csv'}",
        f"global_chapter_audit.csv: {args.output_dir / 'global_chapter_audit.csv'}",
        f"global_layout_findings.csv: {args.output_dir / 'global_layout_findings.csv'}",
        f"global_link_audit.csv: {args.output_dir / 'global_link_audit.csv'}",
        f"global_split_audit.csv: {args.output_dir / 'global_split_audit.csv'}",
    ]
    (args.output_dir / "global_audit_summary.txt").write_text("\n".join(summary_lines), encoding="utf-8")
    print("\n".join(summary_lines))
    return 1 if error_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
