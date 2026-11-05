"""LLM sağlayıcıları.

Ajan döngüsü sağlayıcıdan bağımsızdır: `Provider.stream_turn()` asenkron bir üreteçtir; akış sırasında
`Event` (text_delta/status/notice) üretir ve **son öğe olarak** bir `AssistantTurn` döndürür.
Geçmiş (history) Anthropic Messages biçimindedir.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import anthropic

from ..config import Settings
from .events import Event

FALLBACK_BETA = "server-side-fallback-2026-07-01"
_UNSAFE_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use", "server_tool_use"}


@dataclass
class ToolUse:
    id: str
    name: str
    input: Any


@dataclass
class AssistantTurn:
    content: list[Any]  # sağlayıcının yerel blokları: geçmişe AYNEN geri eklenir
    text: str
    tool_uses: list[ToolUse]
    stop_reason: str | None
    usage: dict[str, int] = field(default_factory=dict)
    model: str | None = None
    refusal: dict[str, Any] | None = None
    served_by_fallback: bool = False


class ProviderError(Exception):
    """Kullanıcıya gösterilebilir sağlayıcı hatası."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class PartialToolJsonError(Exception):
    """Akış sırasında araç girdisi JSON'u ayrıştırılamadı (eager_input_streaming)."""


class Provider(Protocol):
    name: str

    @property
    def is_configured(self) -> bool: ...

    def stream_turn(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> AsyncIterator[Event | AssistantTurn]: ...


# --------------------------------------------------------------------------- blok yardımcıları
def _block_type(block: Any) -> str:
    return block.get("type", "") if isinstance(block, dict) else getattr(block, "type", "")


def _get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def echo_content(content: list[Any]) -> list[Any]:
    """Geçmişe geri eklenecek bloklar. Ortada fallback olduysa, son `fallback` bloğundan ÖNCEKİ
    thinking/redacted_thinking/tool_use bloklarını çıkarır (API'nin geri-yansıtma kuralı)."""
    last_fallback = max((i for i, b in enumerate(content) if _block_type(b) == "fallback"), default=-1)
    if last_fallback < 0:
        return list(content)
    return [b for i, b in enumerate(content) if i > last_fallback or _block_type(b) not in _UNSAFE_BEFORE_FALLBACK]


def extract_tool_uses(content: list[Any]) -> list[ToolUse]:
    """Yürütülecek araç çağrıları (fallback sınırından önceki tool_use'lar atılmıştır)."""
    return [
        ToolUse(_get(b, "id"), _get(b, "name"), _get(b, "input"))
        for b in echo_content(content)
        if _block_type(b) == "tool_use"
    ]


def extract_text(content: list[Any]) -> str:
    return "".join(_get(b, "text", "") for b in content if _block_type(b) == "text")


# ------------------------------------------------------------------------------- Anthropic
class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.settings = settings
        self._client = client

    @property
    def is_configured(self) -> bool:
        return self._client is not None or bool(self.settings.api_key) or bool(os.environ.get("ANTHROPIC_AUTH_TOKEN"))

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.settings.api_key)
        return self._client

    def build_params(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        s = self.settings
        params: dict[str, Any] = {
            "model": s.model,
            "max_tokens": s.max_tokens,
            "system": system,
            "tools": tools,
            "messages": messages,
            "output_config": {"effort": s.effort},
        }
        if s.thinking:
            params["thinking"] = {"type": "adaptive"}
        return params

    async def stream_turn(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> AsyncIterator[Event | AssistantTurn]:
        client = self._get_client()
        params = self.build_params(system=system, tools=tools, messages=messages)
        if self.settings.refusal_fallback:
            manager = client.beta.messages.stream(**params, betas=[FALLBACK_BETA], fallbacks="default")
        else:
            manager = client.messages.stream(**params)
        try:
            async with manager as stream:
                async for event in stream:
                    kind = event.type
                    if kind == "text":
                        yield Event("text_delta", {"text": event.text})
                    elif kind == "content_block_start":
                        block_type = event.content_block.type
                        if block_type == "thinking":
                            yield Event("status", {"state": "thinking"})
                        elif block_type == "tool_use":
                            yield Event("status", {"state": "preparing_tool", "name": event.content_block.name})
                final = await stream.get_final_message()
        except anthropic.APIError as exc:
            raise self._translate(exc) from exc
        except ValueError as exc:  # eager_input_streaming: ayrıştırılamayan kısmi JSON
            raise PartialToolJsonError(str(exc)) from exc
        yield self._to_turn(final)

    # --------------------------------------------------------------------------- dönüşümler
    def _to_turn(self, final: Any) -> AssistantTurn:
        content = list(final.content)
        usage = getattr(final, "usage", None)
        usage_dict = {
            key: int(getattr(usage, key, 0) or 0)
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
        }
        refusal = None
        details = getattr(final, "stop_details", None)
        if final.stop_reason == "refusal":
            refusal = {
                "category": getattr(details, "category", None),
                "explanation": getattr(details, "explanation", None),
            }
        return AssistantTurn(
            content=content,
            text=extract_text(content),
            tool_uses=extract_tool_uses(content),
            stop_reason=final.stop_reason,
            usage=usage_dict,
            model=getattr(final, "model", None),
            refusal=refusal,
            served_by_fallback=any(_block_type(b) == "fallback" for b in content),
        )

    def _translate(self, exc: anthropic.APIError) -> ProviderError:
        if isinstance(exc, anthropic.AuthenticationError):
            return ProviderError("auth", "The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.")
        if isinstance(exc, anthropic.PermissionDeniedError):
            return ProviderError("permission", f"Access denied by the Anthropic API: {_message(exc)}")
        if isinstance(exc, anthropic.NotFoundError):
            return ProviderError(
                "model", f"Model '{self.settings.model}' was not found for this key. Set LOCALASISTAN_MODEL."
            )
        if isinstance(exc, anthropic.RateLimitError):
            return ProviderError("rate_limit", "Rate limit reached. Wait a moment and try again.")
        if isinstance(exc, anthropic.BadRequestError):
            hint = ""
            if self.settings.refusal_fallback and ("fallback" in str(exc).lower() or "beta" in str(exc).lower()):
                hint = " (If this mentions fallbacks, set LOCALASISTAN_REFUSAL_FALLBACK=0.)"
            return ProviderError("bad_request", f"The API rejected the request: {_message(exc)}{hint}")
        if isinstance(exc, anthropic.APIConnectionError):
            return ProviderError("connection", "Could not reach the Anthropic API. Check your network connection.")
        if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
            return ProviderError("server", f"The Anthropic API is having trouble (HTTP {exc.status_code}). Try again.")
        return ProviderError("api", f"Anthropic API error: {_message(exc)}")


def _message(exc: anthropic.APIError) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:400]
    return str(exc)[:400]
