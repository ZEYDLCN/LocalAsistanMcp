"""Test yardımcıları: deterministik sahte LLM sağlayıcısı ve geçmiş doğrulayıcı."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from localasistan.client.events import Event
from localasistan.client.providers import AssistantTurn, PartialToolJsonError, ProviderError, ToolUse


@dataclass
class Step:
    """Sahte modelin tek bir yanıtı."""

    text: str = ""
    tools: list[tuple[str, Any]] = field(default_factory=list)  # (araç adı, girdi)
    stop_reason: str | None = None  # varsayılan: tools varsa "tool_use", yoksa "end_turn"
    error: ProviderError | None = None
    partial_json: bool = False
    refusal: dict[str, Any] | None = None
    delay: float = 0.0
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 10, "output_tokens": 5})
    served_by_fallback: bool = False


class ScriptedProvider:
    name = "scripted"

    def __init__(self, steps: list[Step | Callable[[list[dict]], Step]], *, configured: bool = True) -> None:
        self.steps = list(steps)
        self.configured = configured
        self.calls: list[dict[str, Any]] = []
        self._tool_counter = 0

    @property
    def is_configured(self) -> bool:
        return self.configured

    async def stream_turn(
        self, *, system: str, tools: list[dict], messages: list[dict]
    ) -> AsyncIterator[Event | AssistantTurn]:
        self.calls.append({"system": system, "tools": tools, "messages": copy.deepcopy(messages)})
        if not self.steps:
            raise AssertionError("ScriptedProvider: no more scripted steps")
        step = self.steps.pop(0)
        if callable(step):
            step = step(messages)
        if step.delay:
            await asyncio.sleep(step.delay)
        if step.error:
            raise step.error
        if step.partial_json:
            raise PartialToolJsonError("unterminated string")
        for i in range(0, len(step.text), 4):
            yield Event("text_delta", {"text": step.text[i : i + 4]})
        content: list[dict[str, Any]] = []
        if step.text:
            content.append({"type": "text", "text": step.text})
        tool_uses: list[ToolUse] = []
        for name, arguments in step.tools:
            self._tool_counter += 1
            tool_id = f"toolu_{self._tool_counter:03d}"
            content.append({"type": "tool_use", "id": tool_id, "name": name, "input": arguments})
            tool_uses.append(ToolUse(tool_id, name, arguments))
        stop = step.stop_reason or ("tool_use" if tool_uses else "end_turn")
        yield AssistantTurn(
            content=content,
            text=step.text,
            tool_uses=tool_uses,
            stop_reason=stop,
            usage=dict(step.usage),
            model="scripted-1",
            refusal=step.refusal,
            served_by_fallback=step.served_by_fallback,
        )


def _field(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def assert_valid_history(messages: list[dict]) -> None:
    """Anthropic Messages API kuralları: her tool_use, hemen sonraki user mesajında yanıtlanır; yetim sonuç yok."""
    pending: set[str] = set()
    for index, message in enumerate(messages):
        content = message["content"]
        blocks = content if isinstance(content, list) else []
        if message["role"] == "assistant":
            assert not pending, f"unanswered tool_use before message {index}: {pending}"
            pending = {_field(b, "id") for b in blocks if _field(b, "type") == "tool_use"}
        else:
            results = {_field(b, "tool_use_id") for b in blocks if _field(b, "type") == "tool_result"}
            assert results == pending, f"message {index}: results {results} != pending {pending}"
            if pending and blocks:
                assert blocks[0].get("type") == "tool_result", "tool_result blocks must come first"
            pending = set()
    assert not pending, f"history ends with unanswered tool_use: {pending}"


async def drive(session, text: str, *, approve: Callable[[Event], tuple[bool, str]] | None = None) -> list[Event]:
    """Bir turu sonuna kadar sürer; `approve` verilirse onay isteklerini otomatik yanıtlar."""
    events: list[Event] = []
    async for event in session.run_turn(text):
        events.append(event)
        if event.type == "approval_required" and approve is not None:
            granted, reason = approve(event)
            assert session.resolve_approval(event.data["approval_id"], granted, reason)
    return events


def types_of(events: list[Event]) -> list[str]:
    """Olay türleri; ardışık text_delta parçaları tek öğeye indirgenir."""
    kinds: list[str] = []
    for event in events:
        if event.type == "text_delta" and kinds and kinds[-1] == "text_delta":
            continue
        kinds.append(event.type)
    return kinds


# ------------------------------------------------------------------ gerçek HTTP sunucusu
import json as _json  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import uvicorn  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class LiveServer:
    """Gerçek uvicorn sunucusunu bir iş parçacığında çalıştırır (akış/kopma testleri için)."""

    def __init__(self, app, port: int) -> None:
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> LiveServer:
        self.thread.start()
        deadline = time.monotonic() + 30
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("live server did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=20)


class SSEReader:
    """Tek bir yineleyiciyle SSE akışını parça parça okur (erken dönünce akışı KAPATMAZ)."""

    def __init__(self, response) -> None:
        self._chunks = response.aiter_text().__aiter__()
        self._buffer = ""
        self.events: list[dict] = []
        self.finished = False

    async def _next_event(self) -> dict | None:
        while True:
            if "\n\n" in self._buffer:
                frame, self._buffer = self._buffer.split("\n\n", 1)
                data = "\n".join(line[5:].lstrip() for line in frame.split("\n") if line.startswith("data:"))
                if data:
                    event = _json.loads(data)
                    self.events.append(event)
                    return event
                continue
            try:
                self._buffer += await self._chunks.__anext__()
            except StopAsyncIteration:
                self.finished = True
                return None

    async def until(self, predicate) -> list[dict]:
        """Koşulu sağlayan olaya kadar (dahil) okur; o ana dek alınan TÜM olayları döndürür."""
        while True:
            event = await self._next_event()
            if event is None or predicate(event):
                return list(self.events)

    async def rest(self) -> list[dict]:
        """Akışın sonuna kadar okur; yalnızca BU çağrıdan itibaren gelen olayları döndürür."""
        start = len(self.events)
        while await self._next_event() is not None:
            pass
        return self.events[start:]


async def read_sse(response) -> list[dict]:
    """Akışı sonuna kadar okuyup tüm olayları döndürür."""
    return await SSEReader(response).rest()
