from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Sequence
from urllib.parse import unquote, urlsplit

from .corpus_types import (
    CorpusFile,
    EvidenceUnit,
    ImageEvidence,
    IntegrityReport,
    SourceTier,
)


_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)]+)\)")
_LESSON_FILE_RE = re.compile(r"L(?P<lesson>\d+)_.*\.md$", re.IGNORECASE)
_METADATA_RE = re.compile(r"^-\s*(?P<key>[A-Za-z]+):\s*(?P<value>.*)$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_ID_RE = re.compile(r"^evidence:[0-9a-f]{64}$")
_TRUSTED_IMAGE_SOURCE_TIERS = frozenset(
    {
        SourceTier.LESSON_CHART,
        SourceTier.SECONDARY_ANNOTATION,
        SourceTier.PROJECT_IMPLEMENTATION,
    }
)


class CorpusChangedError(RuntimeError):
    def __init__(self, path: Path):
        self.path = Path(path)
        super().__init__(f"{self.path} changed after integrity scan")

def _relative_source_path(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _valid_file_map(report: IntegrityReport) -> dict[Path, CorpusFile]:
    return {item.path.resolve(): item for item in report.valid_files}


def _read_verified_bytes(item: CorpusFile) -> bytes:
    try:
        data = item.path.read_bytes()
    except OSError as exc:
        raise CorpusChangedError(item.path) from exc
    if len(data) != item.size or hashlib.sha256(data).hexdigest() != item.sha256:
        raise CorpusChangedError(item.path)
    return data


def _image_id(item: CorpusFile) -> str:
    return f"sha256:{item.sha256}"


def _safe_source_path(value: str) -> bool:
    if not value or "\\" in value or re.match(r"^[A-Za-z]:", value):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value


def _stable_evidence_id(
    source_tier: SourceTier,
    source_path: str,
    paragraph: int,
    text: str,
) -> str:
    payload = json.dumps(
        {
            "paragraph": paragraph,
            "source_path": source_path,
            "source_tier": source_tier.value,
            "text": text,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"evidence:{hashlib.sha256(payload).hexdigest()}"


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _resolve_image(
    article_dir: Path,
    target: str,
    source_root: Path,
    valid_files: dict[Path, CorpusFile],
) -> CorpusFile | None:
    raw = target.strip().strip("<>")
    parsed = urlsplit(raw)
    if parsed.scheme or parsed.netloc or not parsed.path or "\\" in parsed.path:
        return None
    decoded = unquote(parsed.path)
    candidate = (article_dir / decoded).resolve()
    if not _is_within(candidate, source_root):
        return None
    item = valid_files.get(candidate)
    if item is None or not item.media_type.startswith("image/"):
        return None
    if item.width is None or item.height is None:
        return None
    _read_verified_bytes(item)
    return item


def _metadata_and_title(text: str, fallback_title: str) -> tuple[str, str | None, str | None]:
    title = fallback_title
    author: str | None = None
    source_url: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and title == fallback_title:
            title = stripped[2:].strip() or fallback_title
        match = _METADATA_RE.match(stripped)
        if not match:
            continue
        key = match.group("key").casefold()
        value = match.group("value").strip()
        if key == "author" and value:
            author = value
        elif key == "source" and value:
            source_url = value
    return title, author, source_url


def _content_blocks(
    text: str,
    article_path: Path,
    source_root: Path,
    valid_files: dict[Path, CorpusFile],
) -> list[tuple[str, tuple[str, ...]]]:
    blocks: list[tuple[str, tuple[str, ...]]] = []
    for raw_block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        lines = [line for line in raw_block.strip().splitlines() if line.strip()]
        if not lines:
            continue
        non_metadata = [
            line
            for line in lines
            if not line.lstrip().startswith("# ") and not _METADATA_RE.match(line.strip())
        ]
        if not non_metadata:
            continue
        block = "\n".join(non_metadata).strip()
        image_ids: list[str] = []
        for match in _IMAGE_RE.finditer(block):
            item = _resolve_image(article_path.parent, match.group("target"), source_root, valid_files)
            if item is not None:
                image_ids.append(_image_id(item))
        clean_text = _IMAGE_RE.sub("", block)
        clean_text = re.sub(r"[ \t]+", " ", clean_text)
        clean_text = re.sub(r"\n{3,}", "\n\n", clean_text).strip()
        blocks.append((clean_text, tuple(dict.fromkeys(image_ids))))
    return blocks


def _units_from_blocks(
    blocks: list[tuple[str, tuple[str, ...]]],
    *,
    source_tier: SourceTier,
    source_path: str,
    sha256: str,
    title: str,
    lesson: int | None,
    source_url: str | None,
    author: str | None,
) -> tuple[EvidenceUnit, ...]:
    consumed_text: set[int] = set()
    pending: list[tuple[int, str, tuple[str, ...]]] = []
    index = 0
    while index < len(blocks):
        text, image_ids = blocks[index]
        if text or not image_ids:
            index += 1
            continue
        run_start = index
        run_images: list[str] = []
        while index < len(blocks) and not blocks[index][0]:
            run_images.extend(blocks[index][1])
            index += 1
        unique_images = tuple(dict.fromkeys(run_images))
        if not unique_images:
            continue
        text_parts: list[str] = []
        previous = run_start - 1
        following = index
        if previous >= 0 and blocks[previous][0]:
            text_parts.append(blocks[previous][0])
            consumed_text.add(previous)
        if following < len(blocks) and blocks[following][0]:
            text_parts.append(blocks[following][0])
            consumed_text.add(following)
        pending.append((run_start, "\n\n".join(text_parts).strip() or title, unique_images))

    for paragraph, (text, image_ids) in enumerate(blocks):
        if not text or paragraph in consumed_text:
            continue
        pending.append((paragraph, text, image_ids))

    units = [
        EvidenceUnit(
            evidence_id=_stable_evidence_id(source_tier, source_path, paragraph, text),
            source_tier=source_tier,
            source_path=source_path,
            sha256=sha256,
            title=title,
            text=text,
            lesson=lesson,
            source_url=source_url,
            author=author,
            image_ids=image_ids,
        )
        for paragraph, text, image_ids in sorted(pending, key=lambda item: item[0])
    ]
    return tuple(units)


def _parse_markdown(
    path: Path,
    root: Path,
    valid_files: dict[Path, CorpusFile],
    source_tier: SourceTier,
    lesson: int | None = None,
) -> tuple[EvidenceUnit, ...]:
    item = valid_files.get(path.resolve())
    if item is None:
        return ()
    text = _read_verified_bytes(item).decode("utf-8")
    source_path = _relative_source_path(path, root)
    title, author, source_url = _metadata_and_title(text, path.stem)
    blocks = _content_blocks(text, path, root, valid_files)
    if not blocks:
        blocks = [(title, ())]
    return _units_from_blocks(
        blocks,
        source_tier=source_tier,
        source_path=source_path,
        sha256=item.sha256,
        title=title,
        lesson=lesson,
        source_url=source_url,
        author=author,
    )


def parse_lesson_index(root: Path, report: IntegrityReport) -> tuple[EvidenceUnit, ...]:
    resolved_root = Path(root).resolve()
    valid_files = _valid_file_map(report)
    index_path = (resolved_root / "_index.md").resolve()
    if index_path not in valid_files:
        return ()

    indexed: list[tuple[int, Path]] = []
    seen: set[Path] = set()
    for line in _read_verified_bytes(valid_files[index_path]).decode("utf-8").splitlines():
        columns = [column.strip() for column in line.strip().strip("|").split("|")]
        filename = next((column for column in columns if _LESSON_FILE_RE.fullmatch(Path(column).name)), "")
        match = _LESSON_FILE_RE.fullmatch(Path(filename).name) if filename else None
        if match is None:
            continue
        candidate = (resolved_root / filename).resolve()
        if not _is_within(candidate, resolved_root) or candidate not in valid_files or candidate in seen:
            continue
        seen.add(candidate)
        indexed.append((int(match.group("lesson")), candidate))

    units: list[EvidenceUnit] = []
    for lesson, path in indexed:
        units.extend(
            _parse_markdown(
                path,
                resolved_root,
                valid_files,
                SourceTier.LESSON_ORIGINAL,
                lesson,
            )
        )
    return tuple(units)


def parse_illustrated_archive(root: Path, report: IntegrityReport) -> tuple[EvidenceUnit, ...]:
    resolved_root = Path(root).resolve()
    articles_root = (resolved_root / "articles").resolve()
    valid_files = _valid_file_map(report)
    article_paths = sorted(
        (
            path
            for path, item in valid_files.items()
            if item.media_type == "text/markdown"
            and _is_within(path, articles_root)
            and path.name != "_index.md"
        ),
        key=lambda path: (str(path).casefold(), str(path)),
    )
    units: list[EvidenceUnit] = []
    for path in article_paths:
        units.extend(
            _parse_markdown(
                path,
                resolved_root,
                valid_files,
                SourceTier.SECONDARY_ANNOTATION,
            )
        )
    return tuple(units)


def collect_image_evidence(
    root: Path,
    report: IntegrityReport,
    source_tier: SourceTier,
) -> tuple[ImageEvidence, ...]:
    resolved_root = Path(root).resolve()
    source_tier = SourceTier(source_tier)
    if source_tier not in _TRUSTED_IMAGE_SOURCE_TIERS:
        raise ValueError("untrusted image source tier")
    by_id: dict[str, ImageEvidence] = {}
    for item in sorted(
        report.valid_files,
        key=lambda value: (str(value.path).casefold(), str(value.path)),
    ):
        if not item.media_type.startswith("image/") or not _is_within(item.path, resolved_root):
            continue
        if item.width is None or item.height is None:
            continue
        _read_verified_bytes(item)
        image = ImageEvidence(
            image_id=_image_id(item),
            source_tier=source_tier,
            source_path=_relative_source_path(item.path, resolved_root),
            sha256=item.sha256,
            media_type=item.media_type,
            width=item.width,
            height=item.height,
        )
        by_id.setdefault(image.image_id, image)
    return tuple(sorted(by_id.values(), key=lambda image: (image.image_id, image.source_path)))


def _unit_dict(unit: EvidenceUnit) -> dict[str, object]:
    body = asdict(unit)
    body["source_tier"] = unit.source_tier.value
    body["image_ids"] = list(unit.image_ids)
    body["concepts"] = list(unit.concepts)
    return body


def _image_dict(image: ImageEvidence) -> dict[str, object]:
    body = asdict(image)
    body["source_tier"] = image.source_tier.value
    return body


def write_trusted_manifest(
    path: Path,
    units: Sequence[EvidenceUnit],
    images: Sequence[ImageEvidence],
    *,
    integrity_status: str = "complete",
) -> Path:
    if integrity_status not in ("complete", "incomplete"):
        raise ValueError("invalid corpus integrity status")
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    sorted_units = tuple(sorted(units, key=lambda unit: unit.evidence_id))
    sorted_images = tuple(sorted(images, key=lambda image: image.image_id))
    source_paths = [unit.source_path for unit in sorted_units] + [
        image.source_path for image in sorted_images
    ]
    if not all(_safe_source_path(source_path) for source_path in source_paths):
        raise ValueError("unsafe source_path")
    if len({unit.evidence_id for unit in sorted_units}) != len(sorted_units):
        raise ValueError("duplicate evidence_id")
    if len({image.image_id for image in sorted_images}) != len(sorted_images):
        raise ValueError("duplicate image_id")
    if any(_EVIDENCE_ID_RE.fullmatch(unit.evidence_id) is None for unit in sorted_units):
        raise ValueError("invalid evidence_id")
    if any(_SHA256_RE.fullmatch(unit.sha256) is None for unit in sorted_units):
        raise ValueError("invalid unit sha256")
    if any(_SHA256_RE.fullmatch(image.sha256) is None for image in sorted_images):
        raise ValueError("invalid image sha256")
    if any(image.image_id != f"sha256:{image.sha256}" for image in sorted_images):
        raise ValueError("image id and sha256 mismatch")
    registered_images = {image.image_id for image in sorted_images}
    referenced_images = {image_id for unit in sorted_units for image_id in unit.image_ids}
    if not referenced_images.issubset(registered_images):
        raise ValueError("unregistered image evidence")
    if any(unit.source_tier is SourceTier.MODEL_INFERENCE for unit in sorted_units) or any(
        image.source_tier is SourceTier.MODEL_INFERENCE for image in sorted_images
    ):
        raise ValueError("model inference cannot enter trusted corpus")
    if any(image.source_tier not in _TRUSTED_IMAGE_SOURCE_TIERS for image in sorted_images):
        raise ValueError("untrusted image source tier")

    tier_counts: dict[str, int] = {}
    for unit in sorted_units:
        tier_counts[unit.source_tier.value] = tier_counts.get(unit.source_tier.value, 0) + 1
    original_status = (
        "available"
        if any(unit.source_tier is SourceTier.LESSON_ORIGINAL for unit in sorted_units)
        else "missing_original"
    )
    payload = {
        "schema_version": 1,
        "corpus_status": {
            "integrity": integrity_status,
            "original_evidence": original_status,
            "trusted_images": len(sorted_images),
            "trusted_units": len(sorted_units),
            "units_by_source_tier": dict(sorted(tier_counts.items())),
        },
        "images": [_image_dict(image) for image in sorted_images],
        "units": [_unit_dict(unit) for unit in sorted_units],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
        separators=(",", ": "),
    ) + "\n"

    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=target.name + ".",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as stream:
            stream.write(serialized)
            stream.flush()
            os.fsync(stream.fileno())
            temporary = stream.name
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return target