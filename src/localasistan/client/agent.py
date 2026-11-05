"""Ajan döngüsü: kullanıcı mesajı -> (LLM -> araç çağrıları -> sonuçlar)* -> yanıt.

Tasarım kuralları
-----------------
* **Geçmiş her zaman API için geçerli kalır:** her `tool_use` bloğunun karşılığında `tool_result` vardır.
  Tur iptal edilir/hata verirse eksik sonuçlar "iptal edildi" hatası olarak tamamlanır.
* **Yıkıcı araçlar onay ister** (`catalog.requires_approval`); onay `asyncio.Future` ile beklenir,
  süre aşımı/bağlantı kopması = ret.
* **Güvenilmeyen çıktı:** araç sonuçları boyut sınırına kırpılır; model girdileri çalıştırılmadan önce
  aracın JSON şemasına karşı doğrulanır (eager_input_streaming'in gerektirdiği istemci doğrulaması).
* `max_tokens` ile kesilen / `refusal` ile biten turdaki araçlar **asla çalıştırılmaz**.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator

from ..config import Settings
from ..errors import LocalAsistanError
from ..logging_setup import get_logger
from .catalog import ToolSpec, anthropic_tools, requires_approval
from .events import Event
from .hub import HubError, McpHub
from .prompts import build_system_prompt
from .providers import AssistantTurn, PartialToolJsonError, Provider, ProviderError

log = get_logger("agent")

_MAX_JSON_RETRIES = 2
_UI_RESULT_CHARS = 6000
_CANCELLED_TEXT = "Cancelled before this tool finished. It may or may not have run; check before repeating it."


class SessionBusyError(LocalAsistanError):
    """Oturumda zaten devam eden bir tur var."""


def truncate_for_model(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n[output truncated: {len(text) - limit} more characters omitted]"


def _validation_message(spec: ToolSpec, arguments: Any) -> str | None:
    if not isinstance(arguments, dict):
        return f"Invalid arguments for {spec.name}: expected a JSON object."
    errors = sorted(Draft202012Validator(spec.input_schema).iter_errors(arguments), key=lambda e: list(e.path))
    if not errors:
        return None
    first = errors[0]
    where = ".".join(str(p) for p in first.path) or "(root)"
    return f"Invalid arguments for {spec.name}: {where}: {first.message}"


@dataclass
class ChatSession:
    id: str
    hub: McpHub
    provider: Provider
    settings: Settings
    policy: Callable[[], str]
    messages: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    title: str = ""
    _busy: bool = field(default=False, init=False)
    _approvals: dict[str, asyncio.Future[tuple[bool, str]]] = field(default_factory=dict, init=False)

    # ---------------------------------------------------------------------------- yardımcılar
    @property
    def busy(self) -> bool:
        return self._busy

    def reset(self) -> None:
        if self._busy:
            raise SessionBusyError("A turn is still running; wait for it to finish or cancel it first.")
        self.messages.clear()
        self.transcript.clear()
        self.title = ""

    def pending_approvals(self) -> list[str]:
        return [a for a, f in self._approvals.items() if not f.done()]

    def resolve_approval(self, approval_id: str, approved: bool, reason: str = "") -> bool:
        future = self._approvals.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result((approved, reason))
        return True

    def _record(self, event: Event) -> Event:
        if event.type != "text_delta":
            self.transcript.append(event.to_dict())
        elif self.transcript and self.transcript[-1]["type"] == "text_delta":
            self.transcript[-1]["text"] += event.data["text"]
        else:
            self.transcript.append(event.to_dict())
        return event

    def _drop_unanswered_user_message(self, snapshot: int) -> None:
        """Bu turda yanıt/araç sonucu üretilmediyse kullanıcı mesajını geçmişten çıkarır (yeniden gönderilebilsin)."""
        if len(self.messages) == snapshot + 1 and self.messages[-1]["role"] == "user":
            del self.messages[snapshot:]

    def _repair_history(self) -> None:
        """Yarım kalan turda `tool_use` bloklarının eksik `tool_result`'larını tamamlar (geçmiş geçerli kalsın)."""
        if not self.messages:
            return
        last = self.messages[-1]
        if last["role"] != "assistant" or not isinstance(last["content"], list):
            return
        ids = [
            (b.get("id") if isinstance(b, dict) else getattr(b, "id", None))
            for b in last["content"]
            if (b.get("type") if isinstance(b, dict) else getattr(b, "type", "")) == "tool_use"
        ]
        if ids:
            self.messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": i, "content": _CANCELLED_TEXT, "is_error": True}
                        for i in ids
                    ],
                }
            )

    # ------------------------------------------------------------------------------- tur
    async def run_turn(self, user_text: str) -> AsyncIterator[Event]:
        if self._busy:
            raise SessionBusyError("A turn is already running in this session.")
        text = (user_text or "").strip()
        if not text:
            raise LocalAsistanError("Message must not be empty.")
        self._busy = True
        completed = False
        snapshot = len(self.messages)
        try:
            if not self.title:
                self.title = text[:60]
            self.transcript.append({"type": "user_message", "text": text})
            try:
                async with contextlib.aclosing(self._loop(text, snapshot)) as events:
                    async for event in events:
                        yield self._record(event)
                completed = True
            except Exception as exc:  # beklenmeyen hata: oturumu bozmadan arayüze bildir
                log.exception("Unexpected error in chat turn")
                yield self._record(
                    Event("error", {"kind": "internal", "message": f"Unexpected error: {type(exc).__name__}"})
                )
                yield self._record(Event("turn_end", {"stop_reason": "error", "iterations": 0}))
        finally:
            if not completed:
                self._repair_history()
                self._drop_unanswered_user_message(snapshot)
            for future in self._approvals.values():
                if not future.done():
                    future.set_result((False, "The request was cancelled."))
            self._approvals.clear()
            self._busy = False

    async def _loop(self, text: str, snapshot: int) -> AsyncIterator[Event]:
        turn_id = uuid.uuid4().hex[:8]
        yield Event("turn_start", {"turn_id": turn_id})
        if not self.provider.is_configured:
            yield Event(
                "error",
                {
                    "kind": "no_api_key",
                    "message": "ANTHROPIC_API_KEY is not set. Add it to your environment or .env file and restart; "
                    "the Tools tab still works without it.",
                },
            )
            yield Event("turn_end", {"stop_reason": "error", "iterations": 0})
            return
        try:
            specs = await self.hub.list_tools()
        except (HubError, LocalAsistanError) as exc:
            yield Event("error", {"kind": "mcp", "message": str(exc)})
            yield Event("turn_end", {"stop_reason": "error", "iterations": 0})
            return
        by_name = {s.name: s for s in specs}
        tools, skipped = anthropic_tools(specs)
        if skipped:
            yield Event("notice", {"level": "warning", "message": f"Tools with unsupported names skipped: {skipped}"})
        system = build_system_prompt(self.hub.instructions)

        self.messages.append({"role": "user", "content": text})
        iterations = 0
        json_retries = 0
        stop_reason = "end_turn"
        while True:
            iterations += 1
            if iterations > self.settings.max_iterations:
                yield Event(
                    "notice",
                    {
                        "level": "warning",
                        "message": f"Stopped after {self.settings.max_iterations} tool rounds. "
                        "Send another message to continue.",
                    },
                )
                stop_reason = "max_iterations"
                break

            turn: AssistantTurn | None = None
            try:
                async with contextlib.aclosing(
                    self.provider.stream_turn(system=system, tools=tools, messages=self.messages)
                ) as stream:
                    async for item in stream:
                        if isinstance(item, AssistantTurn):
                            turn = item
                        else:
                            yield item
            except PartialToolJsonError:
                json_retries += 1
                if json_retries > _MAX_JSON_RETRIES:
                    yield Event(
                        "error",
                        {"kind": "tool_json", "message": "The model produced unreadable tool input repeatedly."},
                    )
                    stop_reason = "error"
                    self._drop_unanswered_user_message(snapshot)
                    break
                yield Event("notice", {"level": "info", "message": "Retrying after unreadable tool input…"})
                iterations -= 1
                continue
            except ProviderError as exc:
                yield Event("error", {"kind": exc.kind, "message": exc.message})
                stop_reason = "error"
                self._drop_unanswered_user_message(snapshot)
                break
            if turn is None:  # pragma: no cover - sağlayıcı sözleşmesi ihlali
                yield Event("error", {"kind": "provider", "message": "The provider returned no response."})
                stop_reason = "error"
                break
            json_retries = 0

            yield Event("usage", {**turn.usage, "model": turn.model})
            if turn.served_by_fallback:
                yield Event("notice", {"level": "info", "message": "Part of this answer came from a fallback model."})

            if turn.stop_reason == "refusal":
                # kısmi içerik atılır; araçlar çalıştırılmaz
                info = turn.refusal or {}
                detail = info.get("explanation") or info.get("category") or "safety policy"
                yield Event("notice", {"level": "warning", "message": f"The model declined this request ({detail})."})
                stop_reason = "refusal"
                self._drop_unanswered_user_message(snapshot)
                break

            self.messages.append({"role": "assistant", "content": turn.content})

            if turn.stop_reason == "pause_turn":
                continue
            if turn.stop_reason == "model_context_window_exceeded":
                yield Event("error", {"kind": "context", "message": "The conversation is too long. Start a new chat."})
                stop_reason = "error"
                break

            if turn.stop_reason == "max_tokens" and turn.tool_uses:
                # kesilmiş araç girdisi güvenilmez: çalıştırma, modele hatayı bildir
                results = [
                    {
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": "Your response was cut off (max_tokens) so this tool call was NOT run. "
                        "Retry with smaller content (for example write a large file in several appends).",
                        "is_error": True,
                    }
                    for tu in turn.tool_uses
                ]
                yield Event("notice", {"level": "warning", "message": "Response was cut off; tool calls were not run."})
                self.messages.append({"role": "user", "content": results})
                continue
            if turn.stop_reason == "max_tokens":
                yield Event("notice", {"level": "warning", "message": "The answer was cut off (max_tokens)."})
                stop_reason = "max_tokens"
                break

            if not turn.tool_uses:
                stop_reason = turn.stop_reason or "end_turn"
                break

            results: list[dict[str, Any]] = []
            try:
                async with contextlib.aclosing(self._run_tools(turn, by_name)) as runner:
                    async for event, result in runner:
                        if event is not None:
                            yield event
                        if result is not None:
                            results.append(result)
            finally:
                # Ne olursa olsun (iptal dahil) her tool_use'a sonuç ver
                done = {r["tool_use_id"] for r in results}
                for tu in turn.tool_uses:
                    if tu.id not in done:
                        results.append(
                            {"type": "tool_result", "tool_use_id": tu.id, "content": _CANCELLED_TEXT, "is_error": True}
                        )
                self.messages.append({"role": "user", "content": results})
        yield Event("turn_end", {"stop_reason": stop_reason, "iterations": iterations})

    # ------------------------------------------------------------------------ araç yürütme
    async def _run_tools(
        self, turn: AssistantTurn, by_name: dict[str, ToolSpec]
    ) -> AsyncIterator[tuple[Event | None, dict[str, Any] | None]]:
        for tu in turn.tool_uses:
            spec = by_name.get(tu.name)
            arguments = tu.input if isinstance(tu.input, dict) else {}
            kind = spec.kind if spec else "unknown"
            yield Event("tool_call", {"call_id": tu.id, "name": tu.name, "input": tu.input, "kind": kind}), None

            def failed(message: str, *, tool_use=tu) -> tuple[Event, dict[str, Any]]:
                event = Event(
                    "tool_result",
                    {
                        "call_id": tool_use.id,
                        "name": tool_use.name,
                        "is_error": True,
                        "text": message,
                        "duration_ms": 0,
                    },
                )
                result = {"type": "tool_result", "tool_use_id": tool_use.id, "content": message, "is_error": True}
                return event, result

            if spec is None:
                event, result = failed(f"Unknown tool '{tu.name}'. Available tools: {', '.join(sorted(by_name))}.")
                yield event, result
                continue
            problem = _validation_message(spec, tu.input)
            if problem:
                event, result = failed(problem)
                yield event, result
                continue

            if requires_approval(spec, self.policy()):
                approval_id = uuid.uuid4().hex[:10]
                future: asyncio.Future[tuple[bool, str]] = asyncio.get_running_loop().create_future()
                self._approvals[approval_id] = future
                yield (
                    Event(
                        "approval_required",
                        {
                            "approval_id": approval_id,
                            "call_id": tu.id,
                            "name": tu.name,
                            "title": spec.title,
                            "input": arguments,
                            "kind": spec.kind,
                        },
                    ),
                    None,
                )
                try:
                    approved, reason = await asyncio.wait_for(future, self.settings.approval_timeout_seconds)
                except TimeoutError:
                    approved, reason = False, "No answer within the approval time limit."
                finally:
                    self._approvals.pop(approval_id, None)
                yield (
                    Event("approval_resolved", {"approval_id": approval_id, "approved": approved, "reason": reason}),
                    None,
                )
                if not approved:
                    detail = f" ({reason})" if reason else ""
                    message = (
                        f"The user declined to run {tu.name}{detail}. Do not retry it; "
                        "ask the user what they would like instead."
                    )
                    event, result = failed(message)
                    yield event, result
                    continue

            outcome = await self.hub.call_tool(tu.name, arguments)
            text = truncate_for_model(outcome.text, self.settings.max_tool_output_chars)
            ui_text = outcome.text if len(outcome.text) <= _UI_RESULT_CHARS else outcome.text[:_UI_RESULT_CHARS] + "…"
            yield (
                Event(
                    "tool_result",
                    {
                        "call_id": tu.id,
                        "name": tu.name,
                        "is_error": outcome.is_error,
                        "text": ui_text,
                        "truncated": len(outcome.text) > _UI_RESULT_CHARS,
                        "duration_ms": outcome.duration_ms,
                    },
                ),
                {"type": "tool_result", "tool_use_id": tu.id, "content": text, "is_error": outcome.is_error},
            )


class ChatManager:
    """Bellekte tutulan sohbet oturumları (en son kullanılan 20 tanesi)."""

    def __init__(self, hub: McpHub, provider: Provider, settings: Settings, *, max_sessions: int = 20) -> None:
        self.hub = hub
        self.provider = provider
        self.settings = settings
        self.policy = settings.approval
        self.max_sessions = max_sessions
        self._sessions: OrderedDict[str, ChatSession] = OrderedDict()

    def get(self, session_id: str) -> ChatSession | None:
        session = self._sessions.get(session_id)
        if session:
            self._sessions.move_to_end(session_id)
        return session

    def create(self) -> ChatSession:
        session = ChatSession(
            id=uuid.uuid4().hex[:12],
            hub=self.hub,
            provider=self.provider,
            settings=self.settings,
            policy=lambda: self.policy,
        )
        self._sessions[session.id] = session
        while len(self._sessions) > self.max_sessions:
            # en eski, meşgul olmayan oturumu at (yeni oluşturulan asla atılmaz)
            victim = next((sid for sid, old in self._sessions.items() if sid != session.id and not old.busy), None)
            if victim is None:  # hepsi meşgul: sınırı geçici olarak aşmasına izin ver
                break
            del self._sessions[victim]
        return session

    def get_or_create(self, session_id: str | None) -> ChatSession:
        return (self.get(session_id) if session_id else None) or self.create()

    def delete(self, session_id: str) -> bool:
        session = self._sessions.get(session_id)
        if session is None or session.busy:
            return False
        del self._sessions[session_id]
        return True

    def summaries(self) -> list[dict[str, Any]]:
        return [
            {"id": s.id, "title": s.title or "(new chat)", "created_at": s.created_at, "busy": s.busy}
            for s in reversed(self._sessions.values())
        ]

    async def aclose(self) -> None:
        for session in self._sessions.values():
            for future in session._approvals.values():
                if not future.done():
                    with contextlib.suppress(asyncio.InvalidStateError):
                        future.set_result((False, "Shutting down."))
