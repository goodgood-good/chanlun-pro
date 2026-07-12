"""Lazy, reverified runtime access to the certified original lesson corpus."""

from __future__ import annotations

import base64
from pathlib import Path
from threading import Lock

from .corpus_loader import (
    CertifiedLessonCorpus,
    load_certified_lesson_corpus,
    make_certified_image_loader,
)
from .corpus_retrieval import CorpusIndex
from .corpus_types import ImageEvidence
from .evidence import (
    EvidencePacket,
    ModelCapabilities,
    RuleEvidenceBinding,
    build_evidence_packet,
)
from .models import DecisionEvent
from .llm_provider import ProviderImage
from .risk import RiskDecision


class CertifiedCorpusRuntime:
    def __init__(self, root: str | Path) -> None:
        path = Path(root)
        if not path.is_absolute():
            path = Path.cwd() / path
        self.root = path
        self._lock = Lock()
        self._corpus: CertifiedLessonCorpus | None = None
        self._index: CorpusIndex | None = None
        self._image_loader = None
        self._images = None

    def _ensure_loaded(self) -> None:
        if self._corpus is not None:
            return
        with self._lock:
            if self._corpus is not None:
                return
            corpus = load_certified_lesson_corpus(self.root)
            if not corpus.semantic_units:
                raise ValueError("certified corpus has no semantic units")
            index = CorpusIndex.build(corpus.semantic_units, images=corpus.images)
            images = {image.image_id: image for image in corpus.images}
            if len(images) != len(corpus.images):
                raise ValueError("certified corpus image identity is duplicated")
            image_loader = make_certified_image_loader(corpus)
            self._corpus = corpus
            self._index = index
            self._images = images
            self._image_loader = image_loader

    def corpus(self) -> CertifiedLessonCorpus:
        self._ensure_loaded()
        if self._corpus is None:
            raise RuntimeError("certified corpus was not loaded")
        return self._corpus

    def corpus_index(self) -> CorpusIndex:
        self._ensure_loaded()
        if self._index is None:
            raise RuntimeError("certified corpus index was not loaded")
        return self._index

    def load_provider_image(self, image: ImageEvidence) -> ProviderImage:
        self._ensure_loaded()
        if self._image_loader is None:
            raise RuntimeError("certified image loader was not loaded")
        loaded = self._image_loader(image)
        if not isinstance(loaded, ProviderImage):
            raise TypeError("certified image loader returned an invalid value")
        return loaded

    def status(self) -> dict[str, object]:
        corpus = self.corpus()
        return {
            "integrity": "complete",
            "original_integrity": "complete",
            "secondary_integrity": "independent",
            "original_evidence": "available",
            "trusted_units": len(corpus.units),
            "semantic_units": len(corpus.semantic_units),
            "trusted_images": len(corpus.images),
            "manifest_fingerprint": "sha256:" + corpus.manifest_sha256,
            "source_pdf_fingerprint": "sha256:" + corpus.source_pdf_sha256,
        }

    def evidence_packet(
        self,
        event: DecisionEvent,
        risk: RiskDecision,
        capabilities: ModelCapabilities,
        *,
        max_units: int = 8,
        rule_evidence_binding: RuleEvidenceBinding | None = None,
    ) -> EvidencePacket:
        self._ensure_loaded()
        if self._index is None:
            raise RuntimeError("certified corpus index was not loaded")
        return build_evidence_packet(
            event,
            risk,
            self._index,
            capabilities,
            max_units=max_units,
            rule_evidence_binding=rule_evidence_binding,
        )

    def read(self, image_id: str) -> tuple[bytes, str]:
        self._ensure_loaded()
        if self._images is None or self._image_loader is None:
            raise RuntimeError("certified image catalog was not loaded")
        image = self._images.get(image_id)
        if image is None:
            raise KeyError("image_not_found")
        loaded = self._image_loader(image)
        prefix = f"data:{image.media_type};base64,"
        if not loaded.data_url.startswith(prefix):
            raise ValueError("certified image data URL is invalid")
        try:
            payload = base64.b64decode(
                loaded.data_url[len(prefix) :],
                validate=True,
            )
        except ValueError as exc:
            raise ValueError("certified image payload is invalid") from exc
        if not payload:
            raise ValueError("certified image payload is empty")
        return payload, image.media_type
