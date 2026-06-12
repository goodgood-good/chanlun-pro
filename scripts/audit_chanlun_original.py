"""Extract a compact evidence index from the Chanlun 108-lessons DOCX.

The goal is not to republish the source text.  It builds paragraph/image
anchors so implementation audits can point back to the original document.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}

KEYWORDS = {
    "fx_bi_xd": ["分型", "笔", "线段"],
    "center": ["中枢", "走势中枢"],
    "center_change": ["中枢延伸", "中枢扩展", "中枢扩张", "级别扩展", "级别扩张"],
    "trend_type": ["走势类型", "上涨", "下跌", "盘整"],
    "same_level": ["同级别分解", "走势分解", "机械化操作"],
    "bsp": ["第一类买点", "第二类买点", "第三类买点", "一类买点", "二类买点", "三类买点", "买卖点"],
    "divergence": ["背驰", "盘整背驰", "趋势背驰"],
    "cascade": ["区间套", "小级别", "次级别", "级别联立"],
}


def _read_xml(zf: zipfile.ZipFile, name: str) -> ET.Element:
    return ET.fromstring(zf.read(name))


def _rels(zf: zipfile.ZipFile) -> dict[str, str]:
    root = _read_xml(zf, "word/_rels/document.xml.rels")
    out: dict[str, str] = {}
    for rel in root.findall("rel:Relationship", NS):
        rid = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rid and target:
            out[rid] = target
    return out


def _paragraphs(zf: zipfile.ZipFile) -> list[dict]:
    rels = _rels(zf)
    root = _read_xml(zf, "word/document.xml")
    paragraphs: list[dict] = []
    for p in root.iterfind(".//w:p", NS):
        text = "".join(t.text or "" for t in p.iterfind(".//w:t", NS))
        image_rids = []
        for blip in p.iterfind(".//a:blip", NS):
            rid = blip.attrib.get(f"{{{NS['r']}}}embed")
            if rid:
                image_rids.append(rid)
        images = [rels.get(rid, rid) for rid in image_rids]
        if text.strip() or images:
            paragraphs.append(
                {
                    "idx": len(paragraphs),
                    "text": re.sub(r"\s+", " ", text).strip(),
                    "images": images,
                }
            )
    return paragraphs


def _image_stats(zf: zipfile.ZipFile) -> dict:
    media = [e for e in zf.infolist() if e.filename.startswith("word/media/")]
    by_ext: dict[str, int] = {}
    entries: list[dict] = []
    for e in media:
        ext = Path(e.filename).suffix.lower() or "(none)"
        by_ext[ext] = by_ext.get(ext, 0) + 1
        raw = zf.read(e.filename)
        entries.append(
            {
                "filename": e.filename.replace("word/", ""),
                "ext": ext,
                "bytes": int(e.file_size),
                "sha1": hashlib.sha1(raw).hexdigest(),
            }
        )
    return {
        "count": len(media),
        "by_ext": dict(sorted(by_ext.items())),
        "entries": entries,
    }


def _lesson_headers(paragraphs: list[dict]) -> list[dict]:
    pattern = re.compile(r"^教你炒股票\s*\d+")
    return [
        {
            "idx": p["idx"],
            "text": p["text"][:300],
            "images": p["images"],
        }
        for p in paragraphs
        if pattern.search(p["text"])
    ]


def _reply_like_anchors(paragraphs: list[dict]) -> list[dict]:
    markers = ("回复", "楼主", "博主", "缠中说禅：", "本ID")
    out: list[dict] = []
    for p in paragraphs:
        text = p["text"]
        if text and any(m in text for m in markers):
            out.append(
                {
                    "idx": p["idx"],
                    "text": text[:500],
                    "images": p["images"],
                }
            )
    return out


def _matches(paragraphs: list[dict], context: int, limit_per_group: int) -> dict:
    out: dict[str, list[dict]] = {}
    for group, keywords in KEYWORDS.items():
        hits: list[dict] = []
        seen: set[int] = set()
        for p in paragraphs:
            text = p["text"]
            if not text:
                continue
            if any(k in text for k in keywords):
                idx = int(p["idx"])
                if idx in seen:
                    continue
                seen.add(idx)
                lo = max(0, idx - context)
                hi = min(len(paragraphs), idx + context + 1)
                hits.append(
                    {
                        "idx": idx,
                        "keywords": [k for k in keywords if k in text],
                        "text": text[:500],
                        "images": p["images"],
                        "context": [
                            {
                                "idx": q["idx"],
                                "text": q["text"][:300],
                                "images": q["images"],
                            }
                            for q in paragraphs[lo:hi]
                        ],
                    }
                )
                if limit_per_group > 0 and len(hits) >= limit_per_group:
                    break
        out[group] = hits
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("docx")
    parser.add_argument("--out", default="D:/chanlun_pro/reports/chanlun_original_index.json")
    parser.add_argument("--context", type=int, default=2)
    parser.add_argument(
        "--limit-per-group",
        type=int,
        default=80,
        help="Maximum keyword hits per group; use 0 for all hits.",
    )
    args = parser.parse_args()

    docx = Path(args.docx)
    with zipfile.ZipFile(docx) as zf:
        paragraphs = _paragraphs(zf)
        payload = {
            "docx": str(docx.resolve()),
            "paragraph_count": len(paragraphs),
            "image_stats": _image_stats(zf),
            "keyword_groups": KEYWORDS,
            "matches": _matches(paragraphs, args.context, args.limit_per_group),
            "lesson_headers": _lesson_headers(paragraphs),
            "reply_like_anchor_count": len(_reply_like_anchors(paragraphs)),
            "reply_like_anchor_sample": _reply_like_anchors(paragraphs)[:300],
            "image_anchor_paragraphs": [p for p in paragraphs if p["images"]],
        }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote={out}")
    print(f"paragraphs={payload['paragraph_count']} images={payload['image_stats']['count']}")
    print(f"lesson_headers={len(payload['lesson_headers'])}")
    print(f"image_anchor_paragraphs={len(payload['image_anchor_paragraphs'])}")
    print(f"reply_like_anchor_count={payload['reply_like_anchor_count']}")
    for group, hits in payload["matches"].items():
        print(f"{group}={len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
