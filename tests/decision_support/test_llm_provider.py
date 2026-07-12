from __future__ import annotations

from decimal import Decimal
import json

import pytest
import requests

from chanlun.decision_support.corpus_retrieval import CorpusIndex
from chanlun.decision_support.corpus_types import EvidenceUnit, SourceTier
from chanlun.decision_support.evidence import ModelCapabilities, build_evidence_packet
from chanlun.decision_support.llm_provider import ConfiguredProvider, ProviderResponse
from chanlun.decision_support.review_prompt import PROMPT_VERSION, build_messages
from chanlun.decision_support.risk import RiskDecision
from tests.decision_support.conftest import ts


@pytest.fixture
def packet(make_decision_event):
    event = make_decision_event(bs_type="3buy")
    risk = RiskDecision(
        allowed=True,
        shares=500,
        planned_risk_cash=Decimal("500"),
        target_weight=Decimal("0.05"),
        entry_reference=Decimal("10"),
        reasons=(),
        daily_loss_locked=False,
        drawdown_locked=False,
        evaluated_at=ts("2026-07-13T10:35:00+08:00"),
    )
    units = (
        EvidenceUnit(
            evidence_id="original-support",
            source_tier=SourceTier.LESSON_ORIGINAL,
            source_path="lesson.md",
            title="3buy lesson",
            text="Third-buy support rule.",
            sha256="sha256:" + "1" * 64,
            lesson=20,
            source_role="lesson_body",
            source_record_id="source:" + "a" * 64,
            source_pdf_sha256="b" * 64,
            page_number=420,
            bbox=(100.0, 200.0, 500.0, 220.0),
        ),
        EvidenceUnit(
            evidence_id="original-counter",
            source_tier=SourceTier.LESSON_ORIGINAL,
            source_path="lesson-counter.md",
            title="3buy 失效",
            text="第三类买点跌破后失效。",
            sha256="sha256:" + "2" * 64,
        ),
    )
    value = build_evidence_packet(
        event,
        risk,
        CorpusIndex.build(units),
        ModelCapabilities(True, True),
    )
    assert value.reviewable is True
    return value


class _HTTPResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self.text = (
            payload
            if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False)
        )


class _StreamingHTTPResponse:
    def __init__(self, status_code: int, chunks):
        self.status_code = status_code
        self._chunks = tuple(chunks)
        self.closed = False
        self.iterated = 0

    @property
    def text(self):
        raise AssertionError("streaming response must not materialize .text")

    def iter_content(self, chunk_size, decode_unicode=False):
        assert chunk_size > 0
        assert decode_unicode is False
        for chunk in self._chunks:
            self.iterated += 1
            yield chunk

    def close(self):
        self.closed = True


class _Message:
    def __init__(self, *, content=None, refusal=None):
        self.content = content
        self.refusal = refusal


class _Choice:
    def __init__(self, message):
        self.message = message
        self.finish_reason = "stop"


class _Completion:
    def __init__(self, message):
        self.choices = [_Choice(message)]

    def model_dump_json(self):
        choice = self.choices[0]
        return json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": choice.message.content,
                            "refusal": choice.message.refusal,
                        },
                        "finish_reason": choice.finish_reason,
                    }
                ]
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )


class _CompletionsAPI:
    def __init__(self, response, calls):
        self.response = response
        self.calls = calls

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _OpenAIClient:
    def __init__(self, response, calls):
        self.chat = type(
            "ChatAPI",
            (),
            {"completions": _CompletionsAPI(response, calls)},
        )()


def _provider(
    *,
    provider="siliconflow",
    api_key="secret-key",
    supports_images=True,
    http_request=None,
    openai_client_factory=None,
):
    return ConfiguredProvider.from_values(
        provider=provider,
        api_key=api_key,
        model="fixture-model",
        supports_images=supports_images,
        supports_json_schema=True,
        http_request=http_request,
        openai_client_factory=openai_client_factory,
    )


def test_prompt_is_versioned_event_bound_and_source_labeled(packet) -> None:
    messages = build_messages(packet)
    user_payload = json.loads(messages[1]["content"])

    assert PROMPT_VERSION == "chanlun-review-v3"
    assert [message["role"] for message in messages] == ["system", "user"]
    assert user_payload["prompt_version"] == PROMPT_VERSION
    assert user_payload["event"]["event_id"] == packet.event.event_id
    assert user_payload["packet_fingerprint"] == packet.packet_fingerprint
    assert any(
        item["source_tier"] == "lesson_original"
        for item in user_payload["supporting_evidence"]
    )
    assert any(
        item["source_tier"] == "project_implementation"
        for item in user_payload["supporting_evidence"]
    )
    original = next(
        item
        for item in user_payload["supporting_evidence"]
        if item["evidence_id"] == "original-support"
    )
    assert original["source_role"] == "lesson_body"
    assert original["source_record_id"] == "source:" + "a" * 64
    assert original["source_pdf_sha256"] == "b" * 64
    assert original["page_number"] == 420
    assert original["bbox"] == [100.0, 200.0, 500.0, 220.0]
    assert user_payload["response_schema"]["additionalProperties"] is False
    assert "model_inference" not in messages[1]["content"]


def test_missing_provider_key_returns_real_failure() -> None:
    provider = ConfiguredProvider.from_values(
        provider="openrouter",
        api_key="",
        model="x",
        supports_images=False,
        supports_json_schema=True,
    )

    response = provider.complete([], (), timeout=(10, 180))

    assert response.ok is False
    assert response.error_code == "missing_credentials"


def test_text_only_provider_declares_no_image_support() -> None:
    provider = ConfiguredProvider.from_values(
        provider="siliconflow",
        api_key="key",
        model="x",
        supports_images=False,
        supports_json_schema=True,
    )

    assert provider.capabilities.supports_images is False


def test_provider_response_rejects_fractional_latency() -> None:
    with pytest.raises((TypeError, ValueError), match="latency_ms"):
        ProviderResponse(
            ok=True,
            provider="fixture",
            model="fixture-model",
            content='{"verdict":"ABSTAIN"}',
            raw_response='{"choices":[]}',
            error_code=None,
            error_message=None,
            retryable=False,
            latency_ms=0.5,
        )


def test_siliconflow_success_uses_strict_schema_zero_temperature_and_timeout() -> None:
    calls = []
    payload = {"choices": [{"message": {"content": '{"verdict":"ABSTAIN"}'}}]}
    http_response = _HTTPResponse(200, payload)

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return http_response

    response = _provider(http_request=request).complete(
        [{"role": "user", "content": "review"}],
        (),
        timeout=(10, 180),
    )

    assert response.ok is True
    assert response.content == '{"verdict":"ABSTAIN"}'
    assert response.raw_response == http_response.text
    assert calls[0][2]["timeout"] == (10, 180)
    assert calls[0][2]["json"]["temperature"] == 0
    assert calls[0][2]["json"]["max_tokens"] == 4096
    assert calls[0][2]["json"]["response_format"]["type"] == "json_schema"


def test_siliconflow_stops_streaming_oversized_http_body() -> None:
    calls = []
    streamed = _StreamingHTTPResponse(
        200,
        (b"x" * 700_000, b"y" * 700_000, b"must-not-be-read"),
    )

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return streamed

    response = _provider(http_request=request).complete(
        [{"role": "user", "content": "review"}],
        (),
        timeout=(10, 180),
    )

    assert calls[0][2]["stream"] is True
    assert response.ok is False
    assert response.error_code == "response_too_large"
    assert streamed.iterated == 2
    assert streamed.closed is True


def test_network_timeout_is_retryable_and_redacts_credentials() -> None:
    def request(*args, **kwargs):
        raise requests.Timeout("timeout for secret-key")

    response = _provider(http_request=request).complete(
        [{"role": "user", "content": "review"}],
        (),
        timeout=(10, 180),
    )

    assert response.ok is False
    assert response.error_code == "network_timeout"
    assert response.retryable is True
    assert "secret-key" not in response.error_message


@pytest.mark.parametrize(
    ("status", "retryable"),
    ((400, False), (429, True), (503, True)),
)
def test_http_error_has_bounded_retry_semantics(status, retryable) -> None:
    def request(*args, **kwargs):
        return _HTTPResponse(status, {"message": "provider rejected secret-key"})

    response = _provider(http_request=request).complete(
        [{"role": "user", "content": "review"}],
        (),
        timeout=(10, 180),
    )

    assert response.ok is False
    assert response.error_code == "http_error"
    assert response.retryable is retryable
    assert "secret-key" not in response.error_message


def test_siliconflow_http_error_retains_full_bounded_response_envelope() -> None:
    raw_response = "x" * 3000
    provider = _provider(
        http_request=lambda *args, **kwargs: _HTTPResponse(503, raw_response)
    )

    response = provider.complete([], (), timeout=(10, 180))

    assert response.ok is False
    assert response.error_code == "http_error"
    assert response.raw_response == raw_response


@pytest.mark.parametrize(
    ("payload", "error_code"),
    (
        ({"unexpected": []}, "malformed_response"),
        ({"choices": [{"message": {"content": ""}}]}, "empty_content"),
        (
            {"choices": [{"message": {"content": "", "refusal": "no"}}]},
            "refusal",
        ),
    ),
)
def test_siliconflow_never_treats_invalid_success_as_ok(payload, error_code) -> None:
    provider = _provider(
        http_request=lambda *args, **kwargs: _HTTPResponse(200, payload)
    )

    response = provider.complete(
        [{"role": "user", "content": "review"}],
        (),
        timeout=(10, 180),
    )

    assert response.ok is False
    assert response.error_code == error_code


def test_images_are_rejected_before_call_when_capability_is_false() -> None:
    calls = []
    provider = _provider(
        supports_images=False,
        http_request=lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    response = provider.complete(
        [{"role": "user", "content": "review"}],
        (object(),),
        timeout=(10, 180),
    )

    assert response.ok is False
    assert response.error_code == "image_capability_mismatch"
    assert calls == []


def test_openrouter_uses_lazy_injected_client_and_zero_temperature() -> None:
    calls = []
    factory_calls = []
    completion = _Completion(_Message(content='{"verdict":"ABSTAIN"}'))

    def factory(**kwargs):
        factory_calls.append(kwargs)
        return _OpenAIClient(completion, calls)

    response = _provider(
        provider="openrouter",
        openai_client_factory=factory,
    ).complete(
        [{"role": "user", "content": "review"}],
        (),
        timeout=(10, 180),
    )

    assert response.ok is True
    assert response.raw_response == completion.model_dump_json()
    assert factory_calls[0]["api_key"] == "secret-key"
    assert calls[0]["temperature"] == 0
    assert calls[0]["max_tokens"] == 4096
    assert calls[0]["response_format"]["type"] == "json_schema"


def test_openrouter_refusal_is_failure() -> None:
    completion = _Completion(_Message(content="", refusal="policy refusal"))
    provider = _provider(
        provider="openrouter",
        openai_client_factory=lambda **kwargs: _OpenAIClient(completion, []),
    )

    response = provider.complete([], (), timeout=(10, 180))

    assert response.ok is False
    assert response.error_code == "refusal"


def test_unsupported_provider_is_explicit_failure() -> None:
    provider = ConfiguredProvider.from_values(
        provider="unknown",
        api_key="key",
        model="model",
        supports_images=False,
        supports_json_schema=True,
    )

    response = provider.complete([], (), timeout=(10, 180))

    assert response.ok is False
    assert response.error_code == "unsupported_provider"
