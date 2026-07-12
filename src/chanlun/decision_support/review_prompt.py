from __future__ import annotations

import json
from typing import Any

from .corpus_types import EvidenceUnit, ImageEvidence, SourceTier
from .evidence import EvidencePacket
from .review_schema import REQUIRED_TOP_LEVEL, ReviewVerdict


PROMPT_VERSION = "chanlun-review-v3"

_SOURCE_LABELS = tuple(
    tier.value
    for tier in SourceTier
    if tier is not SourceTier.MODEL_INFERENCE
)
_TOP_LEVEL_ORDER = (
    "verdict",
    "strategy_track",
    "summary",
    "structure_read",
    "bull_case",
    "bear_case",
    "invalidation_checks",
    "counter_evidence",
    "risk_acknowledged",
    "missing_evidence",
    "reviewed_event_id",
    "reviewed_data_fingerprint",
    "reviewed_packet_fingerprint",
)

if frozenset(_TOP_LEVEL_ORDER) != REQUIRED_TOP_LEVEL:
    raise RuntimeError("review prompt and parser top-level fields differ")


def review_response_schema(packet: EvidencePacket | None = None) -> dict[str, Any]:
    evidence_ids = None
    if packet is not None:
        evidence_ids = [
            unit.evidence_id
            for unit in (*packet.supporting, *packet.counter_evidence)
        ]
        evidence_ids.extend(image.image_id for image in packet.image_evidence)

    evidence_id_schema: dict[str, Any] = {"type": "string", "minLength": 1}
    if evidence_ids is not None:
        evidence_id_schema["enum"] = evidence_ids
    claim = {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "evidence_ids", "source_labels", "supports"],
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "evidence_ids": {
                "type": "array",
                "items": evidence_id_schema,
                "uniqueItems": True,
            },
            "source_labels": {
                "type": "array",
                "items": {"type": "string", "enum": list(_SOURCE_LABELS)},
            },
            "supports": {"type": "boolean"},
        },
    }
    claim_list = {"type": "array", "items": {"$ref": "#/$defs/claim"}}

    def scenario(rank: int) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["claims", "conditions", "rank"],
            "properties": {
                "claims": claim_list,
                "conditions": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                },
                "rank": {"type": "integer", "minimum": 1, "const": rank},
            },
        }

    properties: dict[str, Any] = {
        "verdict": {
            "type": "string",
            "enum": [verdict.value for verdict in ReviewVerdict],
        },
        "strategy_track": {"type": "string", "minLength": 1},
        "summary": {"$ref": "#/$defs/claim"},
        "structure_read": claim_list,
        "bull_case": scenario(1),
        "bear_case": scenario(2),
        "invalidation_checks": claim_list,
        "counter_evidence": claim_list,
        "risk_acknowledged": {"type": "boolean"},
        "missing_evidence": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "reviewed_event_id": {"type": "string", "minLength": 1},
        "reviewed_data_fingerprint": {"type": "string", "minLength": 1},
        "reviewed_packet_fingerprint": {"type": "string", "minLength": 1},
    }
    if packet is not None:
        properties["strategy_track"] = {
            "type": "string",
            "const": packet.event.strategy_track.value,
        }
        properties["reviewed_event_id"] = {
            "type": "string",
            "const": packet.event.event_id,
        }
        properties["reviewed_data_fingerprint"] = {
            "type": "string",
            "const": packet.event.data_fingerprint,
        }
        properties["reviewed_packet_fingerprint"] = {
            "type": "string",
            "const": packet.packet_fingerprint,
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": list(_TOP_LEVEL_ORDER),
        "properties": properties,
        "$defs": {"claim": claim},
    }


def provider_response_format() -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "chanlun_evidence_review",
            "strict": True,
            "schema": review_response_schema(),
        },
    }


def _risk_payload(packet: EvidencePacket) -> dict[str, object]:
    risk = packet.risk
    return {
        "allowed": risk.allowed,
        "shares": risk.shares,
        "planned_risk_cash": str(risk.planned_risk_cash),
        "target_weight": str(risk.target_weight),
        "entry_reference": str(risk.entry_reference),
        "reasons": list(risk.reasons),
        "daily_loss_locked": risk.daily_loss_locked,
        "drawdown_locked": risk.drawdown_locked,
        "evaluated_at": risk.evaluated_at.isoformat(),
    }


def _unit_payload(unit: EvidenceUnit) -> dict[str, object]:
    return {
        "evidence_id": unit.evidence_id,
        "source_tier": unit.source_tier.value,
        "source_path": unit.source_path,
        "title": unit.title,
        "text": unit.text,
        "sha256": unit.sha256,
        "lesson": unit.lesson,
        "source_url": unit.source_url,
        "author": unit.author,
        "image_ids": list(unit.image_ids),
        "concepts": list(unit.concepts),
        "source_role": unit.source_role,
        "source_record_id": unit.source_record_id,
        "source_pdf_sha256": unit.source_pdf_sha256,
        "page_number": unit.page_number,
        "bbox": list(unit.bbox) if unit.bbox is not None else None,
        "source_sequence_index": unit.source_sequence_index,
        "block_index": unit.block_index,
        "source_record_ids": list(unit.source_record_ids),
    }


def _image_payload(image: ImageEvidence) -> dict[str, object]:
    return {
        "image_id": image.image_id,
        "source_tier": image.source_tier.value,
        "source_path": image.source_path,
        "sha256": image.sha256,
        "media_type": image.media_type,
        "width": image.width,
        "height": image.height,
        "alt_text": image.alt_text,
        "source_role": image.source_role,
        "source_record_id": image.source_record_id,
        "source_pdf_sha256": image.source_pdf_sha256,
        "page_number": image.page_number,
        "bbox": list(image.bbox) if image.bbox is not None else None,
        "caption_record_id": image.caption_record_id,
        "asset_id": image.asset_id,
        "occurrence_id": image.occurrence_id,
    }


def build_messages(packet: EvidencePacket) -> tuple[dict[str, str], ...]:
    if not isinstance(packet, EvidencePacket):
        raise TypeError("packet must be EvidencePacket")
    system_message = (
        "你是缠论证据审查器，不是行情源、数据库客户端或交易执行器。"
        "lesson_original 仅表示已审计的缠论原文规则；lesson_chart 表示已审计图表；"
        "project_implementation 仅表示项目算法冻结的结构事实；"
        "secondary_annotation 只能作为辅助，模型推断不得伪装成证据。"
        "逐项核对多级别结构、正面条件、反面条件、失效边界和确定性风控。"
        "每个事实性 claim 必须引用输入中的 evidence_id，并逐项给出真实 source_label。"
        "每个 claim 必须用 supports 明确它支持还是否定当前情形；"
        "bull_case 和 bear_case 必须分别给出 conditions，rank 固定为 1 和 2。"
        "不得发明价格、级别、买卖点、原文、图表内容或指纹，不得输出下单指令。"
        "原文与项目事实冲突、证据缺失、图表不可见、指纹不一致或无法确认时，"
        "verdict 必须是 ABSTAIN。只输出符合 response_schema 的一个 JSON 对象。"
    )
    user_payload = {
        "prompt_version": PROMPT_VERSION,
        "packet_fingerprint": packet.packet_fingerprint,
        "reviewable": packet.reviewable,
        "blockers": list(packet.blockers),
        "event": packet.event.to_dict(),
        "risk_decision": _risk_payload(packet),
        "supporting_evidence": [
            _unit_payload(unit) for unit in packet.supporting
        ],
        "counter_evidence": [
            _unit_payload(unit) for unit in packet.counter_evidence
        ],
        "image_evidence": [
            _image_payload(image) for image in packet.image_evidence
        ],
        "response_schema": review_response_schema(packet),
    }
    return (
        {"role": "system", "content": system_message},
        {
            "role": "user",
            "content": json.dumps(
                user_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        },
    )
