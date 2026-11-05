"""Gerçek Anthropic SDK'sını sahte HTTP taşıması üzerinden çalıştırmak için SSE üretici ve istemci fabrikası."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import anthropic
import httpx2


def _sse(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def message_stream(
    blocks: list[tuple],
    *,
    stop_reason: str = "end_turn",
    model: str = "claude-opus-5-5",
    usage: tuple[int, int] = (120, 40),
    stop_details: dict | None = None,
    chunk: int = 5,
) -> bytes:
    """blocks: ("text", s) | ("thinking", s, sig) | ("tool_use", id, name, dict | raw_json_str) | ("raw", dict)"""
    out = [
        _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": usage[0], "output_tokens": 1, "cache_read_input_tokens": 7},
                },
            },
        )
    ]
    for index, block in enumerate(blocks):
        kind = block[0]
        if kind == "text":
            out.append(
                _sse(
                    "content_block_start",
                    {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                )
            )
            for i in range(0, len(block[1]), chunk):
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "text_delta", "text": block[1][i : i + chunk]},
                        },
                    )
                )
        elif kind == "thinking":
            out.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                    },
                )
            )
            if block[1]:
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "thinking_delta", "thinking": block[1]},
                        },
                    )
                )
            out.append(
                _sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "signature_delta", "signature": block[2]},
                    },
                )
            )
        elif kind == "tool_use":
            _, tool_id, name, arguments = block
            raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
            out.append(
                _sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "tool_use", "id": tool_id, "name": name, "input": {}},
                    },
                )
            )
            for i in range(0, len(raw), chunk):
                out.append(
                    _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": raw[i : i + chunk]},
                        },
                    )
                )
        elif kind == "raw":
            out.append(
                _sse("content_block_start", {"type": "content_block_start", "index": index, "content_block": block[1]})
            )
        out.append(_sse("content_block_stop", {"type": "content_block_stop", "index": index}))
    delta: dict[str, Any] = {"stop_reason": stop_reason, "stop_sequence": None}
    if stop_details is not None:
        delta["stop_details"] = stop_details
    out.append(_sse("message_delta", {"type": "message_delta", "delta": delta, "usage": {"output_tokens": usage[1]}}))
    out.append(_sse("message_stop", {"type": "message_stop"}))
    return "".join(out).encode()


class FakeAnthropic:
    """İstekleri kaydeden ve sıradaki hazır yanıtı döndüren sahte API sunucusu."""

    def __init__(
        self, responses: list[bytes | tuple[int, dict] | Exception | Callable[[httpx2.Request], httpx2.Response]]
    ) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeAnthropic: no more responses")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(request)
        if isinstance(item, tuple):
            status, body = item
            return httpx2.Response(status, json=body, headers={"request-id": "req_test"})
        return httpx2.Response(
            200, content=item, headers={"content-type": "text/event-stream", "request-id": "req_test"}
        )

    def client(self) -> anthropic.AsyncAnthropic:
        return anthropic.AsyncAnthropic(
            api_key="sk-ant-test",
            max_retries=0,
            http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(self.handler)),
        )

    def body(self, index: int = -1) -> dict[str, Any]:
        return json.loads(self.requests[index].content)

    def beta_header(self, index: int = -1) -> str:
        return self.requests[index].headers.get("anthropic-beta", "")


def api_error(status: int, error_type: str, message: str) -> tuple[int, dict]:
    return status, {"type": "error", "error": {"type": error_type, "message": message}}
