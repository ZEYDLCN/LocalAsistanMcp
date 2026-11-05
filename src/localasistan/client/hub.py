"""McpHub — tek bir MCP sunucusuna bağlantı yaşam döngüsü.

* `mcp.Client` bağlamı **tek bir arka plan görevinde** girilip çıkılır (anyio iptal kapsamı kuralı);
  istekler (`call_tool` vb.) herhangi bir görevden yapılabilir.
* Bağlantı koparsa (`CONNECTION_CLOSED`) hub "bozuk" işaretlenir ve **sonraki** çağrıda yeniden kurulur.
  Çağrı sırasında kopan yan etkili bir araç otomatik tekrar denenmez (çift yazma riski);
  yalnızca salt-okunur araçlar bir kez yeniden denenir.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

import anyio
import mcp_types
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError

from ..errors import LocalAsistanError
from ..logging_setup import get_logger
from .catalog import ToolSpec, spec_from_mcp

log = get_logger("hub")


class HubError(LocalAsistanError):
    """MCP sunucusuna bağlanılamadı / bağlantı kullanılamıyor."""


@dataclass
class ToolOutcome:
    text: str
    is_error: bool
    duration_ms: int = 0
    data: Any = None  # metin geçerli JSON ise ayrıştırılmış hali

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "is_error": self.is_error, "duration_ms": self.duration_ms, "data": self.data}


def _flatten_content(result: Any) -> str:
    parts: list[str] = []
    for block in result.content or []:
        kind = getattr(block, "type", "")
        if kind == "text":
            parts.append(block.text)
        elif kind in ("image", "audio"):
            parts.append(f"[{kind} content omitted]")
        elif kind == "resource":
            resource = block.resource
            parts.append(getattr(resource, "text", None) or f"[resource {resource.uri}]")
        elif kind == "resource_link":
            parts.append(f"[resource link {block.uri}]")
    text = "\n".join(parts).strip()
    if not text and getattr(result, "structured_content", None) is not None:
        text = json.dumps(result.structured_content, ensure_ascii=False)
    return text or "(no output)"


def _maybe_json(text: str) -> Any:
    if text[:1] in "{[":
        try:
            return json.loads(text)
        except ValueError:
            return None
    return None


def _describe_exception(exc: BaseException) -> str:
    """İstisnayı kullanıcıya gösterilebilir tek satıra çevirir."""
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_describe_exception(e) for e in exc.exceptions)
    if isinstance(exc, MCPError):
        return f"{exc.message} (code {exc.code})"
    if isinstance(exc, FileNotFoundError):
        return f"command not found: {exc.filename or exc}"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


class McpHub:
    def __init__(
        self,
        target: StdioServerParameters | Any,
        *,
        tool_timeout: float = 60.0,
        start_timeout: float = 30.0,
    ) -> None:
        self._target = target
        self.tool_timeout = tool_timeout
        self.start_timeout = start_timeout
        self._client: Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None
        self._lock = asyncio.Lock()
        self._broken = False
        self._tools: list[ToolSpec] | None = None
        self._info: dict[str, Any] | None = None
        self.last_error: str | None = None
        self.restarts = 0

    # ------------------------------------------------------------------ yaşam döngüsü
    @property
    def connected(self) -> bool:
        return self._client is not None and not self._broken and self._runner is not None and not self._runner.done()

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def restart(self) -> None:
        async with self._lock:
            await self._stop_locked()
            self.restarts += 1
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.connected:
            return
        await self._stop_locked()
        loop = asyncio.get_running_loop()
        ready: asyncio.Future[None] = loop.create_future()
        self._stop = asyncio.Event()
        self._broken = False
        self._tools = None
        self._runner = asyncio.create_task(self._run(ready, self._stop), name="mcp-hub-runner")
        try:
            await asyncio.wait_for(asyncio.shield(ready), self.start_timeout)
        except TimeoutError:
            self.last_error = f"The MCP server did not start within {self.start_timeout:g}s."
            await self._stop_locked()
            raise HubError(self.last_error) from None
        except HubError:
            await self._stop_locked()
            raise
        self.last_error = None

    async def _stop_locked(self) -> None:
        runner, stop = self._runner, self._stop
        self._runner = self._stop = None
        self._client = None
        self._tools = None
        if runner is None:
            return
        if stop is not None:
            stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(runner), 10)
        except (TimeoutError, asyncio.CancelledError):
            runner.cancel()
            with contextlib.suppress(BaseException):
                await runner
        except Exception:
            pass  # runner kendi hatasını zaten kaydetti

    async def _run(self, ready: asyncio.Future[None], stop: asyncio.Event) -> None:
        try:
            async with Client(self._target, read_timeout_seconds=None) as client:
                self._client = client
                self._info = {
                    "name": getattr(client.server_info, "name", None),
                    "title": getattr(client.server_info, "title", None),
                    "version": getattr(client.server_info, "version", None),
                    "protocol_version": client.protocol_version,
                    "instructions": client.instructions,
                    "capabilities": {
                        "tools": client.server_capabilities.tools is not None,
                        "resources": client.server_capabilities.resources is not None,
                        "prompts": client.server_capabilities.prompts is not None,
                    },
                }
                if not ready.done():
                    ready.set_result(None)
                await stop.wait()
        except BaseException as exc:
            message = _describe_exception(exc)
            self.last_error = message
            log.warning("MCP connection ended: %s", message)
            if not ready.done():
                ready.set_exception(HubError(f"Could not start the MCP server: {message}"))
                ready.exception()  # "never retrieved" uyarısını önle (bekleyen kalmamış olabilir)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self._client = None
            if not ready.done():  # pragma: no cover - savunma amaçlı
                ready.set_exception(HubError("The MCP server stopped unexpectedly."))
                ready.exception()

    async def _ensure(self) -> Client:
        if not self.connected:
            async with self._lock:
                if not self.connected:
                    if self._broken or self._runner is not None:
                        self.restarts += 1
                    await self._start_locked()
        client = self._client
        if client is None:  # pragma: no cover - yarış durumunda savunma
            raise HubError("The MCP server is not connected.")
        return client

    def _mark_broken(self, reason: str) -> None:
        self._broken = True
        self.last_error = reason
        log.warning("MCP connection marked broken: %s", reason)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "server": self._info,
            "restarts": self.restarts,
            "last_error": self.last_error,
        }

    @property
    def instructions(self) -> str | None:
        return (self._info or {}).get("instructions")

    # --------------------------------------------------------------------------- araçlar
    async def list_tools(self, *, refresh: bool = False) -> list[ToolSpec]:
        if self._tools is not None and not refresh and self.connected:
            return list(self._tools)
        client = await self._ensure()
        specs: list[ToolSpec] = []
        cursor: str | None = None
        for _ in range(50):  # sayfa sınırı: sonsuz döngüye karşı
            page = await self._request(client.list_tools(cursor=cursor, cache_mode="refresh"))
            specs.extend(spec_from_mcp(t) for t in page.tools)
            cursor = page.next_cursor
            if not cursor:
                break
        self._tools = specs
        return list(specs)

    async def call_tool(
        self, name: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None
    ) -> ToolOutcome:
        limit = timeout or self.tool_timeout
        started = asyncio.get_running_loop().time()
        read_only = False
        if self._tools is not None:
            read_only = any(t.name == name and t.read_only for t in self._tools)
        attempts = 2 if read_only else 1
        last_message = ""
        for _ in range(attempts):
            try:
                client = await self._ensure()
            except HubError as exc:
                return self._outcome(f"The MCP server is unavailable: {exc}", True, started)
            try:
                result = await client.call_tool(name, arguments or {}, read_timeout_seconds=limit)
            except MCPError as exc:
                if exc.code == mcp_types.REQUEST_TIMEOUT:
                    return self._outcome(
                        f"Tool '{name}' timed out after {limit:g}s. It may still be running on the server; "
                        "check the result before retrying.",
                        True,
                        started,
                    )
                if exc.code == mcp_types.CONNECTION_CLOSED:
                    self._mark_broken("connection closed by the server")
                    last_message = (
                        "The MCP server connection was lost during the call; it will be restarted on the next call. "
                        "The call may or may not have completed."
                    )
                    continue
                return self._outcome(f"MCP error {exc.code}: {exc.message}", True, started)
            except (OSError, RuntimeError, anyio.ClosedResourceError, anyio.BrokenResourceError) as exc:
                self._mark_broken(f"{type(exc).__name__}: {exc}")
                last_message = (
                    f"The MCP connection failed ({type(exc).__name__}); it will be restarted on the next call."
                )
                continue
            text = _flatten_content(result)
            return self._outcome(text, bool(result.is_error), started, data=_maybe_json(text))
        return self._outcome(last_message or "The MCP call failed.", True, started)

    @staticmethod
    def _outcome(text: str, is_error: bool, started: float, *, data: Any = None) -> ToolOutcome:
        elapsed = int((asyncio.get_running_loop().time() - started) * 1000)
        return ToolOutcome(text=text, is_error=is_error, duration_ms=elapsed, data=data)

    async def _request(self, awaitable: Any) -> Any:
        """Protokol isteği; kopan bağlantıyı işaretleyip anlaşılır HubError'a çevirir."""
        try:
            return await awaitable
        except MCPError as exc:
            if exc.code == mcp_types.CONNECTION_CLOSED:
                self._mark_broken("connection closed by the server")
                raise HubError("The MCP server connection was lost. Try again to reconnect.") from exc
            raise

    # ---------------------------------------------------------------- kaynaklar / promptlar
    async def list_resources(self) -> dict[str, list[dict[str, Any]]]:
        client = await self._ensure()
        resources = await self._request(client.list_resources())
        templates = await self._request(client.list_resource_templates())
        return {
            "resources": [
                {
                    "uri": str(r.uri),
                    "name": r.name,
                    "title": r.title,
                    "description": r.description,
                    "mime_type": r.mime_type,
                }
                for r in resources.resources
            ],
            "templates": [
                {
                    "uri_template": t.uri_template,
                    "name": t.name,
                    "title": t.title,
                    "description": t.description,
                    "mime_type": t.mime_type,
                }
                for t in templates.resource_templates
            ],
        }

    async def read_resource(self, uri: str) -> dict[str, Any]:
        client = await self._ensure()
        try:
            result = await self._request(client.read_resource(uri))
        except MCPError as exc:
            raise LocalAsistanError(exc.message) from exc
        contents = []
        for item in result.contents:
            if hasattr(item, "text"):
                contents.append({"uri": str(item.uri), "mime_type": item.mime_type, "text": item.text})
            else:
                contents.append(
                    {"uri": str(item.uri), "mime_type": item.mime_type, "blob_base64_chars": len(item.blob)}
                )
        return {"uri": uri, "contents": contents}

    async def list_prompts(self) -> list[dict[str, Any]]:
        client = await self._ensure()
        result = await self._request(client.list_prompts())
        return [
            {
                "name": p.name,
                "title": p.title,
                "description": p.description,
                "arguments": [
                    {"name": a.name, "description": a.description, "required": bool(a.required)}
                    for a in (p.arguments or [])
                ],
            }
            for p in result.prompts
        ]

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> dict[str, Any]:
        client = await self._ensure()
        try:
            result = await self._request(client.get_prompt(name, arguments or {}))
        except MCPError as exc:
            raise LocalAsistanError(exc.message) from exc
        messages = []
        for m in result.messages:
            content = m.content
            text = (
                content.text if getattr(content, "type", "") == "text" else f"[{getattr(content, 'type', 'content')}]"
            )
            messages.append({"role": m.role, "text": text})
        return {"description": result.description, "messages": messages}
