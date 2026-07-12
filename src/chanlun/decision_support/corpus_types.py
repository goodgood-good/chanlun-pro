from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class SourceTier(str, Enum):
    LESSON_ORIGINAL = "lesson_original"
    LESSON_CHART = "lesson_chart"
    SECONDARY_ANNOTATION = "secondary_annotation"
    PROJECT_IMPLEMENTATION = "project_implementation"
    MODEL_INFERENCE = "model_inference"


@dataclass(frozen=True)
class ImageEvidence:
    image_id: str
    source_tier: SourceTier
    source_path: str
    sha256: str
    media_type: str
    width: int
    height: int
    alt_text: str = ""
    source_role: str | None = None
    source_record_id: str | None = None
    source_pdf_sha256: str | None = None
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    caption_record_id: str | None = None
    asset_id: str | None = None
    occurrence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_tier", SourceTier(self.source_tier))
        if self.width <= 0 or self.height <= 0:
            raise ValueError("image dimensions must be positive")
        if self.bbox is not None:
            object.__setattr__(self, "bbox", tuple(self.bbox))


@dataclass(frozen=True)
class EvidenceUnit:
    evidence_id: str
    source_tier: SourceTier
    source_path: str
    title: str
    text: str
    sha256: str = ""
    lesson: int | None = None
    source_url: str | None = None
    author: str | None = None
    image_ids: tuple[str, ...] = ()
    concepts: tuple[str, ...] = ()
    source_role: str | None = None
    source_record_id: str | None = None
    source_pdf_sha256: str | None = None
    page_number: int | None = None
    bbox: tuple[float, float, float, float] | None = None
    source_sequence_index: int | None = None
    block_index: int | None = None
    source_record_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_tier", SourceTier(self.source_tier))
        object.__setattr__(self, "image_ids", tuple(self.image_ids))
        object.__setattr__(self, "concepts", tuple(self.concepts))
        object.__setattr__(self, "source_record_ids", tuple(self.source_record_ids))
        if self.bbox is not None:
            object.__setattr__(self, "bbox", tuple(self.bbox))


@dataclass(frozen=True)
class CorpusFile:
    path: Path
    size: int
    sha256: str
    media_type: str
    valid: bool
    error_code: str = ""
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class IntegrityIssue:
    path: str
    code: str
    detail: str


@dataclass(frozen=True)
class IntegrityReport:
    files: tuple[CorpusFile, ...]
    issues: tuple[IntegrityIssue, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "files", tuple(self.files))
        object.__setattr__(self, "issues", tuple(self.issues))

    @property
    def valid_files(self) -> tuple[CorpusFile, ...]:
        return tuple(item for item in self.files if item.valid)
