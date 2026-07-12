from __future__ import annotations

from dataclasses import dataclass
import importlib
import json
import os
import re
import time
from typing import Callable, Mapping, Protocol, Sequence

import requests

from chanlun import config

from .evidence import ModelCapabilities
from .review_prompt import provider_response_format


_OPENROUTER_URL = "https://openrouter.ai/api/v1"
_SILICONFLOW_URL = "https://api.siliconflow.cn/v1/chat/completions"
_MAX_SAFE_TEXT = 2_000
_MAX_REVIEW_OUTPUT_TOKENS = 4_096
_MAX_HTTP_RESPONSE_BYTES = 1024 * 1024
_HTTP_CHUNK_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class ProviderImage:
    image_id: str
    media_type: str
    data_url: str

    def __post_init__(self) -> None:
        if not isinstance(self.image_id, str) or not self.image_id.strip():
            raise ValueError("image_id must be non-empty")
        if not isinstance(self.media_type, str) or not self.media_type.startswith(
            "image/"
        ):
            raise ValueError("media_type must be an image type")
        prefix = f"data:{self.media_type};base64,"
        if not isinstance(self.data_url, str) or not self.data_url.startswith(prefix):
            raise ValueError("data_url must match media_type")


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    ok: bool
    provider: str
    model: str
    content: str | None
    raw_response: str
    error_code: str | None
    error_message: str | None
    retryable: bool
    latency_ms: int
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if type(self.ok) is not bool or type(self.retryable) is not bool:
            raise TypeError("ok and retryable must be boolean")
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError("latency_ms must be a non-negative integer")
        if self.ok:
            if not isinstance(self.content, str) or not self.content.strip():
                raise ValueError("successful response requires content")
            if self.error_code is not None or self.error_message is not None:
                raise ValueError("successful response cannot carry an error")
            if self.retryable:
                raise ValueError("successful response cannot be retryable")
        else:
            if self.content is not None:
                raise ValueError("failed response cannot carry content")
            if not isinstance(self.error_code, str) or not self.error_code:
                raise ValueError("failed response requires error_code")


class LLMProvider(Protocol):
    provider: str
    model: str
    capabilities: ModelCapabilities

    def complete(
        self,
        messages: Sequence[Mapping[str, object]],
        images: Sequence[ProviderImage],
        timeout: tuple[float, float],
    ) -> ProviderResponse: ...


def _redacted_text(value: object, *secrets: str) -> str:
    text_value = str(value)
    for secret in secrets:
        if secret:
            text_value = text_value.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)bearer\s+[^\s,;]+",
        "Bearer [REDACTED]",
        text_value,
    )


def _safe_text(value: object, *secrets: str) -> str:
    return _redacted_text(value, *secrets)[:_MAX_SAFE_TEXT]


def _bounded_raw_response(value: object, *secrets: str) -> tuple[str, bool]:
    text_value = _redacted_text(value, *secrets)
    encoded = text_value.encode("utf-8")
    if len(encoded) <= _MAX_HTTP_RESPONSE_BYTES:
        return text_value, False
    return (
        encoded[:_MAX_HTTP_RESPONSE_BYTES].decode("utf-8", errors="ignore"),
        True,
    )


def _serialize_openai_response(response: object) -> str:
    dump_json = getattr(response, "model_dump_json", None)
    if callable(dump_json):
        raw_response = dump_json()
    else:
        dump = getattr(response, "model_dump", None)
        if not callable(dump):
            raise TypeError("provider response does not support structured serialization")
        raw_response = json.dumps(
            dump(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if not isinstance(raw_response, str):
        raise TypeError("serialized provider response must be text")
    return raw_response


def _close_http_response(response: object) -> None:
    close = getattr(response, "close", None)
    if callable(close):
        close()


def _read_bounded_http_text(response: object) -> tuple[str, bool]:
    payload = bytearray()
    try:
        iter_content = getattr(response, "iter_content", None)
        if callable(iter_content):
            for chunk in iter_content(
                chunk_size=_HTTP_CHUNK_BYTES,
                decode_unicode=False,
            ):
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                elif not isinstance(chunk, (bytes, bytearray, memoryview)):
                    raise TypeError("HTTP response chunk must be bytes")
                remaining = _MAX_HTTP_RESPONSE_BYTES - len(payload)
                if len(chunk) > remaining:
                    payload.extend(bytes(chunk)[:remaining])
                    return payload.decode("utf-8", errors="replace"), True
                payload.extend(chunk)
            return payload.decode("utf-8", errors="replace"), False

        raw_text = getattr(response, "text", "")
        if not isinstance(raw_text, str):
            raise TypeError("HTTP response text must be text")
        encoded = raw_text.encode("utf-8")
        if len(encoded) > _MAX_HTTP_RESPONSE_BYTES:
            bounded = encoded[:_MAX_HTTP_RESPONSE_BYTES].decode(
                "utf-8",
                errors="replace",
            )
            return bounded, True
        return raw_text, False
    finally:
        _close_http_response(response)


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _timeout_pair(value: object) -> tuple[float, float] | None:
    if not isinstance(value, tuple) or len(value) != 2:
        return None
    parsed: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        number = float(item)
        if number <= 0:
            return None
        parsed.append(number)
    return parsed[0], parsed[1]


def _configured_bool(value: object, default: bool = False) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


class ConfiguredProvider:
    def __init__(
        self,
        *,
        provider: str,
        api_key: str,
        model: str,
        supports_images: bool,
        supports_json_schema: bool,
        base_url: str | None = None,
        http_request: Callable[..., object] | None = None,
        openai_client_factory: Callable[..., object] | None = None,
    ) -> None:
        self.provider = provider.strip().casefold() if isinstance(provider, str) else ""
        self.model = model.strip() if isinstance(model, str) else ""
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self.capabilities = ModelCapabilities(
            supports_images=supports_images,
            supports_json_schema=supports_json_schema,
        )
        self._base_url = base_url
        self._http_request = http_request or requests.request
        self._openai_client_factory = openai_client_factory

    @classmethod
    def from_values(
        cls,
        *,
        provider: str,
        api_key: str,
        model: str,
        supports_images: bool = False,
        supports_json_schema: bool = False,
        base_url: str | None = None,
        http_request: Callable[..., object] | None = None,
        openai_client_factory: Callable[..., object] | None = None,
    ) -> ConfiguredProvider:
        return cls(
            provider=provider,
            api_key=api_key,
            model=model,
            supports_images=supports_images,
            supports_json_schema=supports_json_schema,
            base_url=base_url,
            http_request=http_request,
            openai_client_factory=openai_client_factory,
        )

    @classmethod
    def from_config(cls) -> ConfiguredProvider:
        explicit_provider = os.environ.get(
            "CHANLUN_DECISION_LLM_PROVIDER",
            str(getattr(config, "DECISION_LLM_PROVIDER", "")),
        ).strip()
        if explicit_provider:
            provider = explicit_provider.casefold()
            api_key = os.environ.get(
                "CHANLUN_DECISION_LLM_API_KEY",
                str(getattr(config, "DECISION_LLM_API_KEY", "")),
            )
            model = os.environ.get(
                "CHANLUN_DECISION_LLM_MODEL",
                str(getattr(config, "DECISION_LLM_MODEL", "")),
            )
        elif getattr(config, "OPENROUTER_AI_KEYS", ""):
            provider = "openrouter"
            api_key = config.OPENROUTER_AI_KEYS
            model = config.OPENROUTER_AI_MODEL
        else:
            provider = "siliconflow"
            api_key = getattr(config, "AI_TOKEN", "")
            model = getattr(config, "AI_MODEL", "")
        supports_images = _configured_bool(
            os.environ.get(
                "CHANLUN_DECISION_LLM_SUPPORTS_IMAGES",
                getattr(config, "DECISION_LLM_SUPPORTS_IMAGES", False),
            )
        )
        supports_json_schema = _configured_bool(
            os.environ.get(
                "CHANLUN_DECISION_LLM_SUPPORTS_JSON_SCHEMA",
                getattr(config, "DECISION_LLM_SUPPORTS_JSON_SCHEMA", False),
            )
        )
        return cls.from_values(
            provider=provider,
            api_key=api_key,
            model=model,
            supports_images=supports_images,
            supports_json_schema=supports_json_schema,
        )

    def _failure(
        self,
        started: float,
        error_code: str,
        error_message: object,
        *,
        retryable: bool = False,
        raw_response: object = "",
    ) -> ProviderResponse:
        bounded_raw_response, _ = _bounded_raw_response(
            raw_response,
            self._api_key,
        )
        return ProviderResponse(
            ok=False,
            provider=self.provider,
            model=self.model,
            content=None,
            raw_response=bounded_raw_response,
            error_code=error_code,
            error_message=_safe_text(error_message, self._api_key),
            retryable=retryable,
            latency_ms=_elapsed_ms(started),
        )

    def _success(
        self,
        started: float,
        content: str,
        *,
        raw_response: str,
        finish_reason: str | None = None,
    ) -> ProviderResponse:
        bounded_raw_response, _ = _bounded_raw_response(
            raw_response,
            self._api_key,
        )
        return ProviderResponse(
            ok=True,
            provider=self.provider,
            model=self.model,
            content=content,
            raw_response=bounded_raw_response,
            error_code=None,
            error_message=None,
            retryable=False,
            latency_ms=_elapsed_ms(started),
            finish_reason=finish_reason,
        )

    def complete(
        self,
        messages: Sequence[Mapping[str, object]],
        images: Sequence[ProviderImage],
        timeout: tuple[float, float],
    ) -> ProviderResponse:
        started = time.monotonic()
        if self.provider not in {"openrouter", "siliconflow"}:
            return self._failure(
                started,
                "unsupported_provider",
                "provider is not supported",
            )
        if not self._api_key:
            return self._failure(
                started,
                "missing_credentials",
                "provider API credentials are missing",
            )
        if not self.model:
            return self._failure(started, "missing_model", "provider model is missing")
        if not self.capabilities.supports_json_schema:
            return self._failure(
                started,
                "json_schema_unsupported",
                "provider does not declare JSON schema support",
            )
        if images and not self.capabilities.supports_images:
            return self._failure(
                started,
                "image_capability_mismatch",
                "provider does not declare image support",
            )
        timeout_pair = _timeout_pair(timeout)
        if timeout_pair is None:
            return self._failure(
                started,
                "invalid_timeout",
                "timeout must be a positive connect/read tuple",
            )
        try:
            prepared_messages = self._prepare_messages(messages, images)
        except (TypeError, ValueError) as exc:
            return self._failure(started, "invalid_request", exc)
        if self.provider == "siliconflow":
            return self._complete_siliconflow(
                started,
                prepared_messages,
                timeout_pair,
            )
        return self._complete_openrouter(
            started,
            prepared_messages,
            timeout_pair,
        )

    def _prepare_messages(
        self,
        messages: Sequence[Mapping[str, object]],
        images: Sequence[ProviderImage],
    ) -> list[dict[str, object]]:
        if not isinstance(messages, (list, tuple)):
            raise TypeError("messages must be a sequence")
        prepared: list[dict[str, object]] = []
        for message in messages:
            if not isinstance(message, Mapping):
                raise TypeError("messages must contain mappings")
            role = message.get("role")
            content = message.get("content")
            if role not in {"system", "user", "assistant"}:
                raise ValueError("message role is invalid")
            if not isinstance(content, str):
                raise TypeError("message content must be text")
            prepared.append({"role": role, "content": content})
        if not images:
            return prepared
        if not all(isinstance(image, ProviderImage) for image in images):
            raise TypeError("images must contain ProviderImage values")
        image_parts = [
            {
                "type": "image_url",
                "image_url": {"url": image.data_url},
            }
            for image in images
        ]
        for message in reversed(prepared):
            if message["role"] == "user":
                message["content"] = [
                    {"type": "text", "text": message["content"]},
                    *image_parts,
                ]
                return prepared
        prepared.append({"role": "user", "content": image_parts})
        return prepared

    def _complete_siliconflow(
        self,
        started: float,
        messages: list[dict[str, object]],
        timeout: tuple[float, float],
    ) -> ProviderResponse:
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": _MAX_REVIEW_OUTPUT_TOKENS,
            "response_format": provider_response_format(),
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            response = self._http_request(
                "POST",
                self._base_url or _SILICONFLOW_URL,
                json=payload,
                headers=headers,
                timeout=timeout,
                stream=True,
            )
        except requests.Timeout as exc:
            return self._failure(
                started,
                "network_timeout",
                exc,
                retryable=True,
            )
        except requests.RequestException as exc:
            return self._failure(
                started,
                "network_error",
                exc,
                retryable=True,
            )
        except Exception as exc:
            return self._failure(started, "client_error", exc)

        status_code = getattr(response, "status_code", None)
        if not isinstance(status_code, int):
            _close_http_response(response)
            return self._failure(
                started,
                "malformed_response",
                "response status is missing",
            )
        try:
            raw_text, response_too_large = _read_bounded_http_text(response)
        except requests.RequestException as exc:
            return self._failure(
                started,
                "network_error",
                exc,
                retryable=True,
            )
        except Exception as exc:
            return self._failure(started, "client_error", exc)
        if response_too_large:
            return self._failure(
                started,
                "response_too_large",
                "provider HTTP response exceeds the byte limit",
                raw_response=raw_text,
            )
        if status_code != 200:
            message = raw_text
            try:
                error_payload = json.loads(raw_text)
                if isinstance(error_payload, dict):
                    message = error_payload.get("message") or error_payload.get(
                        "error", raw_text
                    )
            except (TypeError, ValueError):
                pass
            return self._failure(
                started,
                "http_error",
                message,
                retryable=status_code == 429 or status_code >= 500,
                raw_response=raw_text,
            )
        try:
            response_payload = json.loads(raw_text)
        except (TypeError, ValueError):
            return self._failure(
                started,
                "malformed_response",
                "provider returned invalid JSON",
                raw_response=raw_text,
            )
        return self._mapping_response(started, response_payload, raw_text)

    def _mapping_response(
        self,
        started: float,
        payload: object,
        raw_text: str,
    ) -> ProviderResponse:
        if not isinstance(payload, dict):
            return self._failure(
                started,
                "malformed_response",
                "provider response is not an object",
                raw_response=raw_text,
            )
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return self._failure(
                started,
                "malformed_response",
                "provider response has no choices",
                raw_response=raw_text,
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        if not isinstance(message, dict):
            return self._failure(
                started,
                "malformed_response",
                "provider response has no message",
                raw_response=raw_text,
            )
        refusal = message.get("refusal")
        content = message.get("content")
        if isinstance(refusal, str) and refusal.strip():
            return self._failure(
                started,
                "refusal",
                refusal,
                raw_response=raw_text,
            )
        if not isinstance(content, str):
            return self._failure(
                started,
                "malformed_response",
                "provider content is not text",
                raw_response=raw_text,
            )
        if not content.strip():
            return self._failure(
                started,
                "empty_content",
                "provider returned empty content",
                raw_response=raw_text,
            )
        finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
        return self._success(
            started,
            content,
            raw_response=raw_text,
            finish_reason=(
                finish_reason if isinstance(finish_reason, str) else None
            ),
        )

    def _complete_openrouter(
        self,
        started: float,
        messages: list[dict[str, object]],
        timeout: tuple[float, float],
    ) -> ProviderResponse:
        factory = self._openai_client_factory
        client_timeout: object = timeout
        if factory is None:
            try:
                openai_module = importlib.import_module("openai")
                httpx_module = importlib.import_module("httpx")
            except ImportError as exc:
                return self._failure(started, "dependency_missing", exc)
            factory = openai_module.OpenAI
            client_timeout = httpx_module.Timeout(
                timeout[1],
                connect=timeout[0],
                write=timeout[0],
                pool=timeout[0],
            )
        try:
            client = factory(
                api_key=self._api_key,
                base_url=self._base_url or _OPENROUTER_URL,
                timeout=client_timeout,
            )
            response = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0,
                max_tokens=_MAX_REVIEW_OUTPUT_TOKENS,
                response_format=provider_response_format(),
            )
            try:
                raw_response = _serialize_openai_response(response)
            except (TypeError, ValueError) as exc:
                return self._failure(
                    started,
                    "malformed_response",
                    exc,
                )
            bounded_raw_response, response_too_large = _bounded_raw_response(
                raw_response,
                self._api_key,
            )
            if response_too_large:
                return self._failure(
                    started,
                    "response_too_large",
                    "provider SDK response exceeds the byte limit",
                    raw_response=bounded_raw_response,
                )
            choices = getattr(response, "choices", None)
            if not isinstance(choices, list) or not choices:
                return self._failure(
                    started,
                    "malformed_response",
                    "provider response has no choices",
                    raw_response=bounded_raw_response,
                )
            choice = choices[0]
            message = getattr(choice, "message", None)
            if message is None:
                return self._failure(
                    started,
                    "malformed_response",
                    "provider response has no message",
                    raw_response=bounded_raw_response,
                )
            refusal = getattr(message, "refusal", None)
            content = getattr(message, "content", None)
            if isinstance(refusal, str) and refusal.strip():
                return self._failure(
                    started,
                    "refusal",
                    refusal,
                    raw_response=bounded_raw_response,
                )
            if not isinstance(content, str):
                return self._failure(
                    started,
                    "malformed_response",
                    "provider content is not text",
                    raw_response=bounded_raw_response,
                )
            if not content.strip():
                return self._failure(
                    started,
                    "empty_content",
                    "provider returned empty content",
                    raw_response=bounded_raw_response,
                )
            finish_reason = getattr(choice, "finish_reason", None)
            return self._success(
                started,
                content,
                raw_response=bounded_raw_response,
                finish_reason=(
                    finish_reason if isinstance(finish_reason, str) else None
                ),
            )
        except Exception as exc:
            name = type(exc).__name__.casefold()
            status_code = getattr(exc, "status_code", None)
            if "timeout" in name:
                return self._failure(
                    started,
                    "network_timeout",
                    exc,
                    retryable=True,
                )
            if isinstance(status_code, int):
                return self._failure(
                    started,
                    "http_error",
                    exc,
                    retryable=status_code == 429 or status_code >= 500,
                )
            return self._failure(started, "client_error", exc)
