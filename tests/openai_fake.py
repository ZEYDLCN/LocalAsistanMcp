"""OpenAI uyumlu (Chat Completions) sunucuyu `httpx2.MockTransport` ile taklit eder: SSE üretici + istek kaydedici."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx2


def sse(*chunks: dict | str, done: bool = True) -> bytes:
    lines = [f"data: {c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    if done:
        lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


def chunk(
    content: str | None = None,
    *,
    tool_calls: list[dict] | None = None,
    reasoning: str | None = None,
    finish: str | None = None,
    model: str | None = "z-ai/glm-test",
    usage: tuple[int, int] | None = None,
    index: int = 0,
    role: bool = False,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if role:
        delta["role"] = "assistant"
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning_content"] = reasoning
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "choices": [{"index": index, "delta": delta, "finish_reason": finish}],
    }
    if model:
        body["model"] = model
    if usage:
        body["usage"] = {"prompt_tokens": usage[0], "completion_tokens": usage[1], "total_tokens": sum(usage)}
    return body


def usage_chunk(prompt: int, completion: int, model: str | None = "z-ai/glm-test") -> dict[str, Any]:
    """`stream_options.include_usage` isteğinde son parça: `choices` boş, `usage` dolu."""
    body: dict[str, Any] = {
        "id": "chatcmpl-test",
        "choices": [],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }
    if model:
        body["model"] = model
    return body


def text_stream(text: str, *, size: int = 4, finish: str = "stop", usage: tuple[int, int] = (120, 40)) -> bytes:
    parts = [chunk(role=True, content="")]
    parts += [chunk(text[i : i + size]) for i in range(0, len(text), size)]
    parts.append(chunk(finish=finish))
    parts.append(usage_chunk(*usage))
    return sse(*parts)


def tool_stream(
    calls: list[tuple[str | None, str, str]],
    *,
    text: str = "",
    size: int = 6,
    finish: str = "tool_calls",
    usage: tuple[int, int] = (200, 30),
    repeat_name: bool = False,
) -> bytes:
    """calls: (id, ad, argüman_json_metni). Argümanlar `size` karakterlik parçalara bölünür (gerçek akış gibi)."""
    parts = [chunk(role=True, content="")]
    if text:
        parts.append(chunk(text))
    for index, (call_id, name, arguments) in enumerate(calls):
        first: dict[str, Any] = {"index": index, "type": "function", "function": {"name": name, "arguments": ""}}
        if call_id is not None:
            first["id"] = call_id
        parts.append(chunk(tool_calls=[first]))
        for i in range(0, len(arguments), size):
            fn: dict[str, Any] = {"arguments": arguments[i : i + size]}
            if repeat_name:
                fn["name"] = name
            parts.append(chunk(tool_calls=[{"index": index, "function": fn}]))
    parts.append(chunk(finish=finish))
    parts.append(usage_chunk(*usage))
    return sse(*parts)


class FakeOpenAI:
    """İstekleri kaydeden ve sıradaki hazır yanıtı döndüren sahte sunucu."""

    def __init__(
        self,
        responses: list[bytes | tuple[int, Any] | tuple[int, Any, dict] | Exception | Callable[[httpx2.Request], Any]],
    ) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeOpenAI: no more responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        if isinstance(item, tuple):
            status, body, *rest = item
            headers = rest[0] if rest else {}
            if isinstance(body, (dict, list)):
                return httpx2.Response(status, json=body, headers=headers)
            return httpx2.Response(status, content=str(body).encode(), headers=headers)
        return httpx2.Response(200, content=item, headers={"content-type": "text/event-stream"})

    def client(self) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self.handler))

    def sync_client(self) -> httpx2.Client:
        return httpx2.Client(transport=httpx2.MockTransport(self.handler))

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def header(self, name: str, index: int = -1) -> str | None:
        return self.requests[index].headers.get(name)


def error_body(message: str, *, status: int = 400) -> tuple[int, dict]:
    return status, {"error": {"message": message, "type": "invalid_request_error"}}
