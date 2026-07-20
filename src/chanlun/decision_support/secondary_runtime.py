"""Verified, advisory-only access to independently built secondary evidence."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import MISSING, dataclass, fields, replace
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from threading import Lock

from .corpus_retrieval import (
    CorpusIndex,
    EvidenceQuery,
    concepts_for_event,
)
from .corpus_sources import (
    collect_image_evidence,
    parse_illustrated_archive,
)
from .corpus_types import EvidenceUnit, ImageEvidence, SourceTier
from .corpus_validation import archive_roots, scan_validated_corpus
from .models import DecisionEvent


_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_EVIDENCE_ID_RE = re.compile(r"evidence:[0-9a-f]{64}")
_IMAGE_ID_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TOP_LEVEL_KEYS = frozenset(
    {"schema_version", "corpus_status", "images", "units"}
)
_STATUS_KEYS = frozenset(
    {
        "integrity",
        "original_evidence",
        "trusted_images",
        "trusted_units",
        "units_by_source_tier",
    }
)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON value: {value}")


def _load_json(payload: bytes) -> Mapping[str, object]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ValueError("secondary corpus manifest is invalid") from exc
    if not isinstance(value, Mapping):
        raise ValueError("secondary corpus manifest must be an object")
    return value


def _safe_source_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("secondary corpus source_path is unsafe")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError("secondary corpus source_path is unsafe")
    return value


def _dataclass_kwargs(
    raw: object,
    target: type[EvidenceUnit] | type[ImageEvidence],
) -> dict[str, object]:
    if not isinstance(raw, Mapping):
        raise ValueError("secondary corpus manifest entry must be an object")
    definitions = {field.name: field for field in fields(target)}
    if not set(raw).issubset(definitions):
        raise ValueError("secondary corpus manifest entry has unknown fields")
    kwargs: dict[str, object] = {}
    for name, definition in definitions.items():
        if name in raw:
            kwargs[name] = raw[name]
        elif definition.default is not MISSING:
            kwargs[name] = definition.default
        elif definition.default_factory is not MISSING:
            kwargs[name] = definition.default_factory()
        else:
            raise ValueError(f"secondary corpus manifest entry misses {name}")
    for name in ("image_ids", "concepts", "source_record_ids"):
        if name in kwargs:
            value = kwargs[name]
            if not isinstance(value, (list, tuple)):
                raise ValueError(f"secondary corpus {name} must be an array")
            kwargs[name] = tuple(value)
    if "bbox" in kwargs and isinstance(kwargs["bbox"], list):
        kwargs["bbox"] = tuple(kwargs["bbox"])
    return kwargs


def _manifest_unit(raw: object) -> EvidenceUnit:
    try:
        unit = EvidenceUnit(**_dataclass_kwargs(raw, EvidenceUnit))
    except (TypeError, ValueError) as exc:
        raise ValueError("secondary corpus unit is invalid") from exc
    if (
        unit.source_tier is not SourceTier.SECONDARY_ANNOTATION
        or _EVIDENCE_ID_RE.fullmatch(unit.evidence_id) is None
        or _SHA256_RE.fullmatch(unit.sha256) is None
        or not unit.title.strip()
        or not unit.text.strip()
    ):
        raise ValueError("secondary corpus unit is invalid")
    _safe_source_path(unit.source_path)
    return unit


def _manifest_image(raw: object) -> ImageEvidence:
    try:
        image = ImageEvidence(**_dataclass_kwargs(raw, ImageEvidence))
    except (TypeError, ValueError) as exc:
        raise ValueError("secondary corpus image is invalid") from exc
    if (
        image.source_tier is not SourceTier.SECONDARY_ANNOTATION
        or _IMAGE_ID_RE.fullmatch(image.image_id) is None
        or _SHA256_RE.fullmatch(image.sha256) is None
        or image.image_id != f"sha256:{image.sha256}"
    ):
        raise ValueError("secondary corpus image is invalid")
    _safe_source_path(image.source_path)
    return image


def _namespaced_unit(
    unit: EvidenceUnit,
    *,
    archive_root: Path,
    source_root: Path,
) -> EvidenceUnit:
    prefix = archive_root.relative_to(source_root).as_posix()
    source_path = unit.source_path if prefix == "." else f"{prefix}/{unit.source_path}"
    identity = f"{unit.source_tier.value}\0{source_path}\0{unit.evidence_id}"
    evidence_id = "evidence:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return replace(unit, evidence_id=evidence_id, source_path=source_path)


def _namespaced_image(
    image: ImageEvidence,
    *,
    archive_root: Path,
    source_root: Path,
) -> ImageEvidence:
    prefix = archive_root.relative_to(source_root).as_posix()
    source_path = image.source_path if prefix == "." else f"{prefix}/{image.source_path}"
    return replace(image, source_path=source_path)


@dataclass(frozen=True, slots=True)
class SecondaryEvidenceSelection:
    interpretations: tuple[EvidenceUnit, ...]
    images: tuple[ImageEvidence, ...]


class TrustedSecondaryCorpusRuntime:
    """Rebuild and compare a secondary manifest before exposing advisory hits."""

    def __init__(
        self,
        manifest_path: str | Path,
        source_root: str | Path,
    ) -> None:
        self.manifest_path = Path(manifest_path).resolve()
        self.source_root = Path(source_root).resolve()
        self._lock = Lock()
        self._index: CorpusIndex | None = None
        self._units: tuple[EvidenceUnit, ...] | None = None
        self._images: tuple[ImageEvidence, ...] | None = None
        self._image_paths: dict[str, Path] | None = None
        self._status: dict[str, object] | None = None

    def _ensure_loaded(self) -> None:
        if self._index is not None:
            return
        with self._lock:
            if self._index is not None:
                return
            try:
                manifest_bytes = self.manifest_path.read_bytes()
            except OSError as exc:
                raise ValueError("secondary corpus manifest is unavailable") from exc
            manifest = _load_json(manifest_bytes)
            if set(manifest) != _TOP_LEVEL_KEYS or manifest.get("schema_version") != 1:
                raise ValueError("secondary corpus manifest schema is invalid")
            raw_status = manifest.get("corpus_status")
            raw_units = manifest.get("units")
            raw_images = manifest.get("images")
            if (
                not isinstance(raw_status, Mapping)
                or set(raw_status) != _STATUS_KEYS
                or not isinstance(raw_units, list)
                or not isinstance(raw_images, list)
            ):
                raise ValueError("secondary corpus manifest schema is invalid")

            units = tuple(sorted((_manifest_unit(raw) for raw in raw_units), key=lambda item: item.evidence_id))
            images = tuple(sorted((_manifest_image(raw) for raw in raw_images), key=lambda item: item.image_id))
            if len({unit.evidence_id for unit in units}) != len(units):
                raise ValueError("secondary corpus unit identity is duplicated")
            if len({image.image_id for image in images}) != len(images):
                raise ValueError("secondary corpus image identity is duplicated")
            registered_images = {image.image_id for image in images}
            if any(
                image_id not in registered_images
                for unit in units
                for image_id in unit.image_ids
            ):
                raise ValueError("secondary corpus unit references an unknown image")

            expected_units: list[EvidenceUnit] = []
            expected_images: list[ImageEvidence] = []
            issue_counts: Counter[str] = Counter()
            archives = archive_roots(self.source_root)
            for archive_root in archives:
                report = scan_validated_corpus(archive_root, archive_root).report
                issue_counts.update(issue.code for issue in report.issues)
                expected_units.extend(
                    _namespaced_unit(
                        unit,
                        archive_root=archive_root,
                        source_root=self.source_root,
                    )
                    for unit in parse_illustrated_archive(archive_root, report)
                )
                expected_images.extend(
                    _namespaced_image(
                        image,
                        archive_root=archive_root,
                        source_root=self.source_root,
                    )
                    for image in collect_image_evidence(
                        archive_root,
                        report,
                        SourceTier.SECONDARY_ANNOTATION,
                    )
                )
            expected_unit_tuple = tuple(
                sorted(expected_units, key=lambda item: item.evidence_id)
            )
            expected_image_tuple = tuple(
                sorted(expected_images, key=lambda item: item.image_id)
            )
            if units != expected_unit_tuple or images != expected_image_tuple:
                raise ValueError("secondary corpus manifest mismatch")

            expected_integrity = "incomplete" if issue_counts else "complete"
            expected_status = {
                "integrity": expected_integrity,
                "original_evidence": "missing_original",
                "trusted_images": len(images),
                "trusted_units": len(units),
                "units_by_source_tier": {
                    SourceTier.SECONDARY_ANNOTATION.value: len(units)
                },
            }
            if dict(raw_status) != expected_status:
                raise ValueError("secondary corpus manifest status mismatch")

            image_paths = {
                image.image_id: (self.source_root / Path(image.source_path)).resolve()
                for image in images
            }
            if any(
                not path.is_relative_to(self.source_root)
                for path in image_paths.values()
            ):
                raise ValueError("secondary corpus image path is unsafe")
            self._units = units
            self._images = images
            self._image_paths = image_paths
            self._index = CorpusIndex.build(units, images=images)
            self._status = {
                "integrity": expected_integrity,
                "coverage": (
                    "verified_subset_only"
                    if expected_integrity == "incomplete"
                    else "complete"
                ),
                "advisory_only": True,
                "eligible_for_rule_binding": False,
                "trusted_units": len(units),
                "trusted_images": len(images),
                "issue_counts": dict(sorted(issue_counts.items())),
                "blockers": (
                    ["secondary_corpus_incomplete"]
                    if expected_integrity == "incomplete"
                    else []
                ),
                "manifest_fingerprint": "sha256:"
                + hashlib.sha256(manifest_bytes).hexdigest(),
            }

    def status(self) -> dict[str, object]:
        self._ensure_loaded()
        if self._status is None:
            raise RuntimeError("secondary corpus status was not loaded")
        return dict(self._status)

    def related(
        self,
        event: DecisionEvent,
        *,
        limit: int = 3,
    ) -> SecondaryEvidenceSelection:
        if not isinstance(event, DecisionEvent):
            raise TypeError("event must be DecisionEvent")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
            raise ValueError("limit must be between 1 and 8")
        self._ensure_loaded()
        if self._index is None:
            raise RuntimeError("secondary corpus index was not loaded")
        concepts = list(concepts_for_event(event.signal.bs_type))
        if event.signal.divergence_kind == "qs" and "趋势背驰" not in concepts:
            concepts.append("趋势背驰")
        if event.signal.nest_operable and "区间套" not in concepts:
            concepts.append("区间套")
        hits = self._index.search(
            EvidenceQuery(
                text=" ".join((*concepts, event.signal.bs_type)),
                concepts=tuple(concepts),
                source_tiers=(SourceTier.SECONDARY_ANNOTATION,),
            ),
            limit=limit,
        )
        interpretations = tuple(hit.unit for hit in hits)
        image_ids = tuple(
            dict.fromkeys(
                image_id
                for unit in interpretations
                for image_id in unit.image_ids
            )
        )
        return SecondaryEvidenceSelection(
            interpretations=interpretations,
            images=self._index.images_for(image_ids),
        )

    def read(self, image_id: str) -> tuple[bytes, str]:
        self._ensure_loaded()
        if self._images is None or self._image_paths is None:
            raise RuntimeError("secondary image catalog was not loaded")
        images = {image.image_id: image for image in self._images}
        image = images.get(image_id)
        if image is None:
            raise KeyError("image_not_found")
        try:
            payload = self._image_paths[image_id].read_bytes()
        except OSError as exc:
            raise ValueError("secondary image is unavailable") from exc
        if not payload or hashlib.sha256(payload).hexdigest() != image.sha256:
            raise ValueError("secondary image was tampered")
        return payload, image.media_type


__all__ = ["SecondaryEvidenceSelection", "TrustedSecondaryCorpusRuntime"]
