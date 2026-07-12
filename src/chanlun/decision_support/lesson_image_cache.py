from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .lesson_corpus import PdfIdentity, SourceRole
from .lesson_images import ImageOccurrence, PdfImageAssetDescriptor


_ARCHIVE_HASH_CHUNK_BYTES = 1024 * 1024
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400
_LEGACY_FIXTURE_MAX_TOTAL_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class LessonImageInventory:
    assets: tuple[PdfImageAssetDescriptor, ...]
    occurrences: tuple[ImageOccurrence, ...]
    materialized_paths: dict[str, Path]
    archived_primary_paths: dict[str, Path] = field(default_factory=dict)
    archived_smask_paths: dict[str, Path] = field(default_factory=dict)


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_link_or_reparse(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
    )


def _safe_lstat(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unreadable") from exc
    if _is_link_or_reparse(info):
        raise ValueError(f"{label} must not be a symbolic link or reparse point")
    return info


def _safe_existing_path(path: Path, label: str) -> os.stat_result:
    absolute = _absolute_without_resolving(path)
    current = Path(absolute.anchor)
    parts = absolute.parts[1:]
    if not parts:
        return _safe_lstat(current, label)
    for index, part in enumerate(parts):
        current /= part
        info = _safe_lstat(current, label)
        if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{label} directory component is invalid")
    return info


def _safe_directory(path: Path, label: str) -> os.stat_result:
    info = _safe_existing_path(path, label)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{label} is not a directory")
    return info


def _safe_regular_file(path: Path, label: str) -> os.stat_result:
    info = _safe_existing_path(path, label)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} is not a regular file")
    return info


def _stable_stat_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _stream_sha256_and_size(path: Path, label: str) -> tuple[str, int]:
    before = _safe_regular_file(path, label)
    before_signature = _stable_stat_signature(before)
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with path.open("rb") as stream:
            opened_before = os.fstat(stream.fileno())
            if _stable_stat_signature(opened_before) != before_signature:
                raise ValueError(f"{label} changed before verification")
            while chunk := stream.read(_ARCHIVE_HASH_CHUNK_BYTES):
                digest.update(chunk)
                size_bytes += len(chunk)
            opened_after = os.fstat(stream.fileno())
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(f"{label} is unreadable") from exc
    after = _safe_regular_file(path, label)
    if (
        size_bytes != before.st_size
        or _stable_stat_signature(opened_after) != before_signature
        or _stable_stat_signature(after) != before_signature
    ):
        raise ValueError(f"{label} changed during verification")
    return digest.hexdigest(), size_bytes


def _asset_dict(asset: PdfImageAssetDescriptor) -> dict[str, object]:
    return {
        "asset_id": asset.asset_id,
        "bits_per_component": asset.bits_per_component,
        "color_space": asset.color_space,
        "filter_name": asset.filter_name,
        "pixel_size": list(asset.pixel_size),
        "raw_sha256": asset.raw_sha256,
        "raw_size_bytes": asset.raw_size_bytes,
        "smask_sha256": asset.smask_sha256,
        "smask_size_bytes": asset.smask_size_bytes,
        "smask_xref": asset.smask_xref,
        "source_pdf_sha256": asset.source_pdf_sha256,
        "xref": asset.xref,
    }


def _occurrence_dict(occurrence: ImageOccurrence) -> dict[str, object]:
    return {
        "asset_sha256": occurrence.asset_sha256,
        "bbox_pdf_bottom_left": list(occurrence.bbox_pdf_bottom_left),
        "bbox_top_left": list(occurrence.bbox_top_left),
        "caption_page_number": occurrence.caption_page_number,
        "caption_source_sequence_index": occurrence.caption_source_sequence_index,
        "classification_id": occurrence.classification_id,
        "classifier_version": occurrence.classifier_version,
        "cropbox_pdf": list(occurrence.cropbox_pdf) if occurrence.cropbox_pdf is not None else None,
        "draw_index": occurrence.draw_index,
        "draw_bbox_pdf_bottom_left": list(occurrence.draw_bbox_pdf_bottom_left),
        "draw_bbox_top_left": list(occurrence.draw_bbox_top_left),
        "lesson_number": occurrence.lesson_number,
        "mediabox_pdf": list(occurrence.mediabox_pdf) if occurrence.mediabox_pdf is not None else None,
        "occurrence_id": occurrence.occurrence_id,
        "page_number": occurrence.page_number,
        "page_rotation": occurrence.page_rotation,
        "page_size": list(occurrence.page_size),
        "reason_codes": list(occurrence.reason_codes),
        "source_pdf_sha256": occurrence.source_pdf_sha256,
        "source_role": occurrence.source_role.value,
        "xobject_name": occurrence.xobject_name,
        "xref": occurrence.xref,
    }


def _dedupe_assets(
    assets: tuple[PdfImageAssetDescriptor, ...] | list[PdfImageAssetDescriptor],
) -> tuple[PdfImageAssetDescriptor, ...]:
    by_xref: dict[int, PdfImageAssetDescriptor] = {}
    for asset in assets:
        if not isinstance(asset, PdfImageAssetDescriptor):
            raise TypeError("assets must contain PdfImageAssetDescriptor values")
        previous = by_xref.get(asset.xref)
        if previous is not None and previous != asset:
            raise ValueError("one image xref cannot have inconsistent asset metadata")
        by_xref[asset.xref] = asset
    return tuple(sorted(by_xref.values(), key=lambda asset: asset.xref))


def _ordered_occurrences(
    occurrences: tuple[ImageOccurrence, ...] | list[ImageOccurrence],
) -> tuple[ImageOccurrence, ...]:
    values = tuple(occurrences)
    if any(not isinstance(item, ImageOccurrence) for item in values):
        raise TypeError("occurrences must contain ImageOccurrence values")
    positions = tuple((item.page_number, item.draw_index) for item in values)
    if len(set(positions)) != len(positions):
        raise ValueError("draw_index must be unique within each page")
    return tuple(sorted(values, key=lambda item: (item.page_number, item.draw_index)))


@dataclass(frozen=True)
class IncrementalArchiveProgress:
    asset_descriptor_count: int
    primary_raw_stream_bytes: int
    primary_asset_count: int
    smask_asset_count: int
    occurrence_count: int


class IncrementalLessonImageCacheBuilder:
    """Spool image payloads per extraction batch and publish one atomic cache."""

    def __init__(
        self,
        target: Path,
        *,
        identity: PdfIdentity,
        extractor_version: str,
    ) -> None:
        if not isinstance(identity, PdfIdentity):
            raise TypeError("identity must be PdfIdentity")
        if not isinstance(extractor_version, str):
            raise TypeError("extractor_version must be a string")
        version = extractor_version.strip()
        if not version or len(version) > 128:
            raise ValueError("extractor_version must be present and bounded")
        target_path = Path(target).absolute()
        if target_path.is_symlink():
            raise ValueError("target must not be a symbolic link")
        if target_path.exists():
            raise FileExistsError(f"target already exists: {target_path}")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        self._target = target_path
        self._identity = identity
        self._extractor_version = version
        self._staging = Path(
            tempfile.mkdtemp(
                prefix=f".{target_path.name}.staging-", dir=target_path.parent
            )
        )
        self._assets_by_xref: dict[int, PdfImageAssetDescriptor] = {}
        self._occurrences: list[ImageOccurrence] = []
        self._occurrence_positions: set[tuple[int, int]] = set()
        self._primary_sizes: dict[str, int] = {}
        self._smask_sizes: dict[str, int] = {}
        self._published = False

    def __enter__(self) -> IncrementalLessonImageCacheBuilder:
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if not self._published and self._staging.exists():
            shutil.rmtree(self._staging)
        return False

    @property
    def retained_raw_bytes(self) -> int:
        return 0

    @property
    def progress(self) -> IncrementalArchiveProgress:
        return IncrementalArchiveProgress(
            asset_descriptor_count=len(self._assets_by_xref),
            primary_raw_stream_bytes=sum(
                asset.raw_size_bytes for asset in self._assets_by_xref.values()
            ),
            primary_asset_count=len(self._primary_sizes),
            smask_asset_count=len(self._smask_sizes),
            occurrence_count=len(self._occurrences),
        )

    def _ensure_open(self) -> None:
        if self._published or not self._staging.exists():
            raise RuntimeError("incremental image cache builder is closed")

    def _spool_payload(
        self,
        *,
        sha256: str,
        raw: bytes,
        directory: str,
        suffix: str,
        sizes: dict[str, int],
        label: str,
    ) -> None:
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != sha256:
            raise ValueError(f"{label} bytes do not match their SHA-256")
        previous_size = sizes.get(sha256)
        if previous_size is not None:
            if previous_size != len(raw):
                raise ValueError(f"repeated {label} hash has inconsistent size")
            return
        path = self._staging / directory / f"{sha256}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        sizes[sha256] = len(raw)

    def add_batch(
        self,
        *,
        assets: tuple[PdfImageAssetDescriptor, ...] | list[PdfImageAssetDescriptor],
        occurrences: tuple[ImageOccurrence, ...] | list[ImageOccurrence],
        primary_raw_by_sha256: dict[str, bytes],
        smask_raw_by_sha256: dict[str, bytes],
    ) -> IncrementalArchiveProgress:
        self._ensure_open()
        batch_assets = _dedupe_assets(assets)
        batch_occurrences = _ordered_occurrences(occurrences)
        if any(asset.source_pdf_sha256 != self._identity.sha256 for asset in batch_assets) or any(
            item.source_pdf_sha256 != self._identity.sha256 for item in batch_occurrences
        ):
            raise ValueError("image inventory source PDF identity mismatch")
        expected_primary = {asset.raw_sha256 for asset in batch_assets}
        expected_smasks = {
            asset.smask_sha256 for asset in batch_assets if asset.smask_sha256 is not None
        }
        if set(primary_raw_by_sha256) != expected_primary:
            raise ValueError("batch primary bytes do not close over batch assets")
        if set(smask_raw_by_sha256) != expected_smasks:
            raise ValueError("batch SMask bytes do not close over batch assets")
        for asset in batch_assets:
            previous = self._assets_by_xref.get(asset.xref)
            if previous is not None and previous != asset:
                raise ValueError("one image xref cannot have inconsistent asset metadata")
            raw = primary_raw_by_sha256[asset.raw_sha256]
            if len(raw) != asset.raw_size_bytes:
                raise ValueError("primary byte size does not match asset metadata")
            self._spool_payload(
                sha256=asset.raw_sha256,
                raw=raw,
                directory="primary_assets",
                suffix=".jpg",
                sizes=self._primary_sizes,
                label="primary image",
            )
            if asset.smask_sha256 is not None:
                smask = smask_raw_by_sha256[asset.smask_sha256]
                if len(smask) != asset.smask_size_bytes:
                    raise ValueError("SMask byte size does not match asset metadata")
                self._spool_payload(
                    sha256=asset.smask_sha256,
                    raw=smask,
                    directory="smasks",
                    suffix=".bin",
                    sizes=self._smask_sizes,
                    label="SMask",
                )
            self._assets_by_xref[asset.xref] = asset
        for occurrence in batch_occurrences:
            linked = self._assets_by_xref.get(occurrence.xref)
            if linked is None or linked.raw_sha256 != occurrence.asset_sha256:
                raise ValueError("image occurrence does not link to its xref asset")
            position = occurrence.page_number, occurrence.draw_index
            if position in self._occurrence_positions:
                raise ValueError("draw_index must be unique within each page")
            self._occurrence_positions.add(position)
            self._occurrences.append(occurrence)
        return self.progress

    def publish(self) -> Path:
        self._ensure_open()
        ordered_assets = _dedupe_assets(tuple(self._assets_by_xref.values()))
        ordered_occurrences = _ordered_occurrences(tuple(self._occurrences))
        expected_primary = {asset.raw_sha256 for asset in ordered_assets}
        expected_smasks = {
            asset.smask_sha256 for asset in ordered_assets if asset.smask_sha256 is not None
        }
        if set(self._primary_sizes) != expected_primary or set(self._smask_sizes) != expected_smasks:
            raise ValueError("spooled image payloads do not close over asset metadata")
        asset_bytes = b"".join(_json_bytes(_asset_dict(asset)) for asset in ordered_assets)
        occurrence_bytes = b"".join(
            _json_bytes(_occurrence_dict(item)) for item in ordered_occurrences
        )
        (self._staging / "image_assets.jsonl").write_bytes(asset_bytes)
        (self._staging / "image_occurrences.jsonl").write_bytes(occurrence_bytes)
        materialized_primary = [
            {
                "path": f"primary_assets/{sha256}.jpg",
                "sha256": sha256,
                "size_bytes": self._primary_sizes[sha256],
            }
            for sha256 in sorted(expected_primary)
        ]
        materialized_smasks = [
            {
                "path": f"smasks/{sha256}.bin",
                "sha256": sha256,
                "size_bytes": self._smask_sizes[sha256],
            }
            for sha256 in sorted(expected_smasks)
        ]
        selected_hashes = {
            item.asset_sha256
            for item in ordered_occurrences
            if item.source_role is SourceRole.LESSON_CHART
        }
        primary_entry_by_sha = {
            entry["sha256"]: entry for entry in materialized_primary
        }
        manifest = {
            "archived_primary_asset_count": len(expected_primary),
            "archived_smask_asset_count": len(expected_smasks),
            "asset_count": len(ordered_assets),
            "extractor_version": self._extractor_version,
            "files": {
                "assets": {
                    "path": "image_assets.jsonl",
                    "sha256": hashlib.sha256(asset_bytes).hexdigest(),
                    "size_bytes": len(asset_bytes),
                },
                "occurrences": {
                    "path": "image_occurrences.jsonl",
                    "sha256": hashlib.sha256(occurrence_bytes).hexdigest(),
                    "size_bytes": len(occurrence_bytes),
                },
            },
            "materialized_lesson_charts": [
                primary_entry_by_sha[sha256] for sha256 in sorted(selected_hashes)
            ],
            "materialized_primary_assets": materialized_primary,
            "materialized_smasks": materialized_smasks,
            "occurrence_count": len(ordered_occurrences),
            "package_kind": "chanlun_pdf_image_inventory",
            "role_counts": dict(
                sorted(Counter(item.source_role.value for item in ordered_occurrences).items())
            ),
            "schema_version": 2,
            "source_pdf": asdict(self._identity),
            "total_primary_raw_stream_bytes": sum(
                asset.raw_size_bytes for asset in ordered_assets
            ),
            "total_unique_primary_raw_stream_bytes": sum(self._primary_sizes.values()),
            "total_unique_smask_raw_stream_bytes": sum(self._smask_sizes.values()),
        }
        (self._staging / "manifest.json").write_bytes(_json_bytes(manifest))
        load_lesson_image_cache(self._staging, expected_identity=self._identity)
        os.replace(self._staging, self._target)
        self._published = True
        return _absolute_without_resolving(self._target)


def build_lesson_image_cache(
    target: Path,
    *,
    identity: PdfIdentity,
    assets: tuple[PdfImageAssetDescriptor, ...] | list[PdfImageAssetDescriptor],
    occurrences: tuple[ImageOccurrence, ...] | list[ImageOccurrence],
    materialized_raw_by_sha256: dict[str, bytes],
    materialized_smask_raw_by_sha256: dict[str, bytes] | None = None,
    extractor_version: str,
) -> Path:
    """Build a cache from one small in-memory test fixture.

    Production extraction must use ``IncrementalLessonImageCacheBuilder`` so image
    payloads are released after each batch instead of retaining the full PDF archive.
    """
    smask_payloads = dict(materialized_smask_raw_by_sha256 or {})
    payloads = tuple(materialized_raw_by_sha256.values()) + tuple(
        smask_payloads.values()
    )
    if any(not isinstance(payload, bytes) for payload in payloads):
        raise TypeError("legacy image cache fixture payloads must be bytes")
    total_bytes = sum(len(payload) for payload in payloads)
    if total_bytes > _LEGACY_FIXTURE_MAX_TOTAL_BYTES:
        raise ValueError(
            "build_lesson_image_cache is limited to small fixtures; "
            "use IncrementalLessonImageCacheBuilder for production archives"
        )
    with IncrementalLessonImageCacheBuilder(
        target,
        identity=identity,
        extractor_version=extractor_version,
    ) as builder:
        builder.add_batch(
            assets=assets,
            occurrences=occurrences,
            primary_raw_by_sha256=materialized_raw_by_sha256,
            smask_raw_by_sha256=smask_payloads,
        )
        return builder.publish()


def _identity(value: object) -> PdfIdentity:
    if not isinstance(value, dict):
        raise ValueError("image cache source_pdf is invalid")
    try:
        return PdfIdentity(
            value["filename"], value["size_bytes"], value["page_count"], value["sha256"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("image cache source_pdf is invalid") from exc


def _asset_from_dict(value: object) -> PdfImageAssetDescriptor:
    if not isinstance(value, dict):
        raise ValueError("image asset row must be an object")
    try:
        return PdfImageAssetDescriptor(
            source_pdf_sha256=value["source_pdf_sha256"],
            xref=value["xref"],
            pixel_size=tuple(value["pixel_size"]),
            filter_name=value["filter_name"],
            color_space=value["color_space"],
            bits_per_component=value["bits_per_component"],
            raw_sha256=value["raw_sha256"],
            raw_size_bytes=value["raw_size_bytes"],
            smask_xref=value["smask_xref"],
            smask_sha256=value["smask_sha256"],
            smask_size_bytes=value["smask_size_bytes"],
            asset_id=value["asset_id"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("image asset row is invalid") from exc


def _occurrence_from_dict(value: object) -> ImageOccurrence:
    if not isinstance(value, dict):
        raise ValueError("image occurrence row must be an object")
    try:
        occurrence = ImageOccurrence.create(
            source_pdf_sha256=value["source_pdf_sha256"],
            asset_sha256=value["asset_sha256"],
            lesson_number=value["lesson_number"],
            page_number=value["page_number"],
            draw_index=value["draw_index"],
            xref=value["xref"],
            xobject_name=value["xobject_name"],
            bbox_top_left=tuple(value["bbox_top_left"]),
            page_size=tuple(value["page_size"]),
            page_rotation=value["page_rotation"],
            source_role=SourceRole(value["source_role"]),
            reason_codes=tuple(value["reason_codes"]),
            classifier_version=value["classifier_version"],
            caption_page_number=value["caption_page_number"],
            caption_source_sequence_index=value["caption_source_sequence_index"],
            cropbox_pdf=(tuple(value["cropbox_pdf"]) if value["cropbox_pdf"] is not None else None),
            mediabox_pdf=(tuple(value["mediabox_pdf"]) if value["mediabox_pdf"] is not None else None),
            draw_bbox_top_left=tuple(value["draw_bbox_top_left"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("image occurrence row is invalid") from exc
    if (
        occurrence.occurrence_id != value.get("occurrence_id")
        or occurrence.classification_id != value.get("classification_id")
        or list(occurrence.bbox_pdf_bottom_left) != value.get("bbox_pdf_bottom_left")
        or list(occurrence.draw_bbox_pdf_bottom_left)
        != value.get("draw_bbox_pdf_bottom_left")
    ):
        raise ValueError("image occurrence derived fields are inconsistent")
    return occurrence


def _load_jsonl(path: Path, descriptor: object, label: str) -> tuple[object, ...]:
    if not isinstance(descriptor, dict) or descriptor.get("path") != path.name:
        raise ValueError(f"image cache {label} descriptor is invalid")
    _safe_regular_file(path, f"image cache {label} file")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"image cache {label} file is unreadable") from exc
    if (
        descriptor.get("size_bytes") != len(data)
        or descriptor.get("sha256") != hashlib.sha256(data).hexdigest()
    ):
        raise ValueError(f"image cache {label} hash or size mismatch")
    try:
        return tuple(json.loads(line) for line in data.decode("utf-8").splitlines() if line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"image cache {label} JSONL is invalid") from exc


def _load_materialized_entries(
    root: Path,
    entries: object,
    *,
    directory: str,
    suffix: str,
    label: str,
) -> dict[str, Path]:
    if not isinstance(entries, list):
        raise ValueError(f"image cache {label} list is invalid")
    paths: dict[str, Path] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f"image cache {label} entry is invalid")
        sha256 = entry.get("sha256")
        expected_relative = f"{directory}/{sha256}{suffix}"
        if entry.get("path") != expected_relative or not isinstance(sha256, str):
            raise ValueError(f"image cache {label} path is invalid")
        path = root / expected_relative
        actual_sha256, actual_size = _stream_sha256_and_size(
            path, f"image cache {label}"
        )
        if entry.get("size_bytes") != actual_size or actual_sha256 != sha256:
            raise ValueError(f"image cache {label} hash or size mismatch")
        if sha256 in paths:
            raise ValueError(f"image cache {label} entry is duplicated")
        paths[sha256] = path
    archive_directory = root / directory
    actual_files: set[Path] = set()
    try:
        archive_info = archive_directory.lstat()
    except FileNotFoundError:
        archive_info = None
    except OSError as exc:
        raise ValueError(f"image cache {label} directory is unreadable") from exc
    if archive_info is not None:
        _safe_directory(archive_directory, f"image cache {label} directory")

        def _walk_error(error: OSError) -> None:
            raise ValueError(f"image cache {label} directory is unreadable") from error

        for current_name, directory_names, file_names in os.walk(
            archive_directory,
            topdown=True,
            onerror=_walk_error,
            followlinks=False,
        ):
            current = Path(current_name)
            _safe_directory(current, f"image cache {label} directory")
            for name in directory_names:
                _safe_directory(
                    current / name, f"image cache {label} directory component"
                )
            for name in file_names:
                candidate = current / name
                _safe_regular_file(candidate, f"image cache {label}")
                actual_files.add(_absolute_without_resolving(candidate))
    if actual_files != {
        _absolute_without_resolving(path) for path in paths.values()
    }:
        raise ValueError(f"image cache {label} files do not close over the manifest")
    return paths


def _verify_archived_jpeg(
    path: Path,
    expected_size: tuple[int, int],
) -> None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
        with Image.open(path) as image:
            decoded_size = image.size
    except Exception as exc:
        raise ValueError("image cache primary asset does not decode as JPEG") from exc
    if decoded_size != expected_size:
        raise ValueError("image cache primary asset dimensions mismatch")


def load_lesson_image_cache(
    root: Path,
    *,
    expected_identity: PdfIdentity,
) -> LessonImageInventory:
    if not isinstance(expected_identity, PdfIdentity):
        raise TypeError("expected_identity must be PdfIdentity")
    root_path = _absolute_without_resolving(Path(root))
    _safe_directory(root_path, "image cache root")
    manifest_path = root_path / "manifest.json"
    _safe_regular_file(manifest_path, "image cache manifest")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("image cache manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("package_kind") != "chanlun_pdf_image_inventory"
        or manifest.get("schema_version") != 2
    ):
        raise ValueError("image cache manifest kind is invalid")
    if _identity(manifest.get("source_pdf")) != expected_identity:
        raise ValueError("image cache source PDF identity mismatch")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise ValueError("image cache file descriptors are invalid")
    asset_rows = _load_jsonl(root_path / "image_assets.jsonl", files.get("assets"), "assets")
    occurrence_rows = _load_jsonl(
        root_path / "image_occurrences.jsonl", files.get("occurrences"), "occurrences"
    )
    assets = _dedupe_assets(tuple(_asset_from_dict(row) for row in asset_rows))
    occurrences = _ordered_occurrences(
        tuple(_occurrence_from_dict(row) for row in occurrence_rows)
    )
    if any(asset.source_pdf_sha256 != expected_identity.sha256 for asset in assets):
        raise ValueError("image cache asset source PDF identity mismatch")
    if any(
        occurrence.source_pdf_sha256 != expected_identity.sha256
        for occurrence in occurrences
    ):
        raise ValueError("image cache occurrence source PDF identity mismatch")
    if manifest.get("asset_count") != len(assets) or manifest.get("occurrence_count") != len(occurrences):
        raise ValueError("image cache inventory count mismatch")
    archived_primary_paths = _load_materialized_entries(
        root_path,
        manifest.get("materialized_primary_assets"),
        directory="primary_assets",
        suffix=".jpg",
        label="materialized primary asset",
    )
    archived_smask_paths = _load_materialized_entries(
        root_path,
        manifest.get("materialized_smasks"),
        directory="smasks",
        suffix=".bin",
        label="materialized SMask",
    )
    expected_primary = {asset.raw_sha256 for asset in assets}
    expected_smasks = {asset.smask_sha256 for asset in assets if asset.smask_sha256 is not None}
    if set(archived_primary_paths) != expected_primary:
        raise ValueError("image cache primary assets do not close over asset metadata")
    if set(archived_smask_paths) != expected_smasks:
        raise ValueError("image cache SMasks do not close over asset metadata")
    for asset in assets:
        primary_path = archived_primary_paths[asset.raw_sha256]
        if primary_path.stat().st_size != asset.raw_size_bytes:
            raise ValueError("image cache primary asset size mismatches metadata")
        _verify_archived_jpeg(primary_path, asset.pixel_size)
        if asset.smask_sha256 is not None and (
            archived_smask_paths[asset.smask_sha256].stat().st_size
            != asset.smask_size_bytes
        ):
            raise ValueError("image cache SMask size mismatches metadata")
    assets_by_xref = {asset.xref: asset for asset in assets}
    for occurrence in occurrences:
        linked = assets_by_xref.get(occurrence.xref)
        if linked is None or linked.raw_sha256 != occurrence.asset_sha256:
            raise ValueError("image cache occurrence does not link to its xref asset")
    expected_selected = {
        item.asset_sha256
        for item in occurrences
        if item.source_role is SourceRole.LESSON_CHART
    }
    chart_entries = manifest.get("materialized_lesson_charts")
    expected_chart_entries = [
        {
            "path": f"primary_assets/{sha256}.jpg",
            "sha256": sha256,
            "size_bytes": archived_primary_paths[sha256].stat().st_size,
        }
        for sha256 in sorted(expected_selected)
    ]
    if chart_entries != expected_chart_entries:
        raise ValueError("image cache chart view path or metadata is invalid")
    materialized_paths = {
        sha256: archived_primary_paths[sha256] for sha256 in expected_selected
    }
    expected_role_counts = dict(
        sorted(Counter(item.source_role.value for item in occurrences).items())
    )
    if manifest.get("role_counts") != expected_role_counts:
        raise ValueError("image cache role counts mismatch")
    if (
        manifest.get("archived_primary_asset_count") != len(expected_primary)
        or manifest.get("archived_smask_asset_count") != len(expected_smasks)
        or manifest.get("total_primary_raw_stream_bytes")
        != sum(asset.raw_size_bytes for asset in assets)
        or manifest.get("total_unique_primary_raw_stream_bytes")
        != sum(path.stat().st_size for path in archived_primary_paths.values())
        or manifest.get("total_unique_smask_raw_stream_bytes")
        != sum(path.stat().st_size for path in archived_smask_paths.values())
    ):
        raise ValueError("image cache materialized archive counts mismatch")
    return LessonImageInventory(
        assets=assets,
        occurrences=occurrences,
        materialized_paths=materialized_paths,
        archived_primary_paths=archived_primary_paths,
        archived_smask_paths=archived_smask_paths,
    )
