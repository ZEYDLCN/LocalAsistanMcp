"""Starlette uygulaması: JSON API + SSE sohbet + statik arayüz."""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Any

from mcp import StdioServerParameters
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from ..client.agent import ChatManager, SessionBusyError
from ..client.catalog import requires_approval
from ..client.events import Event
from ..client.hub import HubError, McpHub
from ..client.providers import Provider, create_provider
from ..config import APPROVAL_POLICIES, Settings
from ..errors import LocalAsistanError
from ..logging_setup import get_logger
from .security import LocalOnlyMiddleware
from .sse import SSEResponse

log = get_logger("web")
STATIC_DIR = Path(__file__).parent / "static"
_NO_STORE = {"Cache-Control": "no-store"}
_MAX_BODY_BYTES = 2_000_000


def error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=_NO_STORE)


def ok(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(data, status_code=status, headers=_NO_STORE)


async def read_json(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > _MAX_BODY_BYTES:
        raise LocalAsistanError("Request body is too large.")
    if not raw:
        return {}
    try:
        body = await request.json()
    except ValueError:
        raise LocalAsistanError("Request body must be valid JSON.") from None
    if not isinstance(body, dict):
        raise LocalAsistanError("Request body must be a JSON object.")
    return body


def create_app(
    settings: Settings,
    *,
    provider: Provider | None = None,
    hub_target: Any = None,
    extra_hosts: frozenset[str] = frozenset(),
) -> Starlette:
    """`provider`/`hub_target` yalnızca bağımlılık enjeksiyonu içindir (testler); üretimde varsayılanlar kullanılır."""

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        settings.ensure_dirs()
        target = hub_target or StdioServerParameters(
            command=sys.executable, args=["-m", "localasistan.server"], env=settings.server_env()
        )
        hub = McpHub(target, tool_timeout=settings.tool_timeout_seconds)
        try:
            await hub.start()
        except HubError as exc:
            log.error("MCP server could not be started: %s", exc)
        app.state.settings = settings
        app.state.hub = hub
        app.state.provider = provider or create_provider(settings)
        app.state.manager = ChatManager(hub, app.state.provider, settings)
        try:
            yield
        finally:
            await app.state.manager.aclose()
            await hub.stop()

    def hub_of(request: Request) -> McpHub:
        return request.app.state.hub

    def manager_of(request: Request) -> ChatManager:
        return request.app.state.manager

    # ------------------------------------------------------------------------- sayfa
    async def index(request: Request) -> Response:
        return FileResponse(STATIC_DIR / "index.html", headers=_NO_STORE)

    async def healthz(request: Request) -> Response:
        return ok({"status": "ok", "version": __version__})

    # ------------------------------------------------------------------------ durum
    async def status(request: Request) -> Response:
        hub, manager = hub_of(request), manager_of(request)
        s: Settings = request.app.state.settings
        info = hub.status()
        tool_count = None
        if info["connected"]:
            with contextlib.suppress(LocalAsistanError):
                tool_count = len(await hub.list_tools())
        return ok(
            {
                "version": __version__,
                "mcp": {**info, "tool_count": tool_count},
                "llm": {
                    "provider": manager.provider.name,
                    "configured": manager.provider.is_configured,
                    "missing": getattr(manager.provider, "missing_setting", None)
                    or (None if manager.provider.is_configured else "ANTHROPIC_API_KEY"),
                    "model": s.model,
                    "effort": s.effort,
                    "thinking": s.thinking,
                    "refusal_fallback": s.refusal_fallback,
                    "max_iterations": s.max_iterations,
                },
                "approval": {"policy": manager.policy, "policies": list(APPROVAL_POLICIES)},
                "environment": {"workspace": str(s.workspace), "home": str(s.home), "timezone": s.timezone},
                "sessions": manager.summaries(),
            }
        )

    async def reconnect(request: Request) -> Response:
        hub = hub_of(request)
        try:
            await hub.restart()
        except HubError as exc:
            return error(503, str(exc))
        return ok(hub.status())

    async def settings_update(request: Request) -> Response:
        body = await read_json(request)
        manager = manager_of(request)
        if "approval" in body:
            if body["approval"] not in APPROVAL_POLICIES:
                return error(400, f"approval must be one of: {', '.join(APPROVAL_POLICIES)}")
            manager.policy = body["approval"]
        return ok({"approval": manager.policy})

    # ------------------------------------------------------------------------ araçlar
    async def tools_list(request: Request) -> Response:
        manager = manager_of(request)
        specs = await hub_of(request).list_tools(refresh=request.query_params.get("refresh") == "1")
        return ok(
            {
                "tools": [
                    {**s.to_dict(), "needs_approval": requires_approval(s, manager.policy)}
                    for s in sorted(specs, key=lambda t: t.name)
                ]
            }
        )

    async def tool_call(request: Request) -> Response:
        name = request.path_params["name"]
        body = await read_json(request)
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict):
            return error(400, "arguments must be a JSON object.")
        hub = hub_of(request)
        if name not in {s.name for s in await hub.list_tools()}:
            return error(404, f"Unknown tool '{name}'.")
        outcome = await hub.call_tool(name, arguments)
        return ok(outcome.to_dict())

    # ------------------------------------------------------- kaynaklar ve promptlar
    async def resources_list(request: Request) -> Response:
        return ok(await hub_of(request).list_resources())

    async def resource_read(request: Request) -> Response:
        uri = request.query_params.get("uri", "")
        if not uri:
            return error(400, "uri query parameter is required.")
        try:
            return ok(await hub_of(request).read_resource(uri))
        except HubError:
            raise
        except LocalAsistanError as exc:
            return error(404, str(exc))

    async def prompts_list(request: Request) -> Response:
        return ok({"prompts": await hub_of(request).list_prompts()})

    async def prompt_get(request: Request) -> Response:
        body = await read_json(request)
        arguments = body.get("arguments", {})
        if not isinstance(arguments, dict) or not all(isinstance(v, str) for v in arguments.values()):
            return error(400, "arguments must be an object of string values.")
        try:
            return ok(await hub_of(request).get_prompt(request.path_params["name"], arguments))
        except HubError:
            raise
        except LocalAsistanError as exc:
            return error(400, str(exc))

    # ---------------------------------------------------------------------- sohbet
    async def chat(request: Request) -> Response:
        body = await read_json(request)
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            return error(400, "message must be a non-empty string.")
        if len(message) > 100_000:
            return error(400, "message is too long.")
        manager = manager_of(request)
        session_id = body.get("session_id")
        if session_id is not None:
            session = manager.get(session_id) if isinstance(session_id, str) else None
            if session is None:
                return error(404, "Chat session not found (it may have expired). Start a new chat.")
        else:
            session = manager.create()
        if session.busy:
            return error(409, "A reply is still being generated in this chat.")

        async def events():
            yield Event("session", {"session_id": session.id}).to_sse()
            try:
                async with contextlib.aclosing(session.run_turn(message)) as turn:
                    async for event in turn:
                        yield event.to_sse()
            except SessionBusyError as exc:
                yield Event("error", {"kind": "busy", "message": str(exc)}).to_sse()
            except LocalAsistanError as exc:
                yield Event("error", {"kind": "input", "message": str(exc)}).to_sse()

        return SSEResponse(events())

    async def chat_get(request: Request) -> Response:
        session = manager_of(request).get(request.path_params["sid"])
        if session is None:
            return error(404, "Chat session not found.")
        return ok(
            {
                "id": session.id,
                "title": session.title,
                "busy": session.busy,
                "transcript": session.transcript,
                "pending_approvals": session.pending_approvals(),
            }
        )

    async def chat_approval(request: Request) -> Response:
        session = manager_of(request).get(request.path_params["sid"])
        if session is None:
            return error(404, "Chat session not found.")
        body = await read_json(request)
        approve = body.get("approve")
        if not isinstance(approve, bool):
            return error(400, "approve must be true or false.")
        reason = str(body.get("reason", ""))[:200]
        if not session.resolve_approval(request.path_params["aid"], approve, reason):
            return error(404, "Approval not found or already answered.")
        return ok({"ok": True})

    async def chat_reset(request: Request) -> Response:
        session = manager_of(request).get(request.path_params["sid"])
        if session is None:
            return error(404, "Chat session not found.")
        try:
            session.reset()
        except SessionBusyError as exc:
            return error(409, str(exc))
        return ok({"ok": True})

    async def chat_delete(request: Request) -> Response:
        if not manager_of(request).delete(request.path_params["sid"]):
            return error(404, "Chat session not found or still running.")
        return ok({"ok": True})

    async def chats_list(request: Request) -> Response:
        return ok({"sessions": manager_of(request).summaries()})

    # ------------------------------------------------------------------ hata işleme
    async def on_local_error(request: Request, exc: Exception) -> Response:
        return error(400, str(exc))

    async def on_hub_error(request: Request, exc: Exception) -> Response:
        return error(503, str(exc))

    async def on_unexpected(request: Request, exc: Exception) -> Response:
        log.exception("Unhandled error on %s %s", request.method, request.url.path)
        return error(500, "Internal server error.")

    routes = [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/api/status", status),
        Route("/api/reconnect", reconnect, methods=["POST"]),
        Route("/api/settings", settings_update, methods=["POST"]),
        Route("/api/tools", tools_list),
        Route("/api/tools/{name}/call", tool_call, methods=["POST"]),
        Route("/api/resources", resources_list),
        Route("/api/resources/read", resource_read),
        Route("/api/prompts", prompts_list),
        Route("/api/prompts/{name}", prompt_get, methods=["POST"]),
        Route("/api/chat", chat, methods=["POST"]),
        Route("/api/chats", chats_list),
        Route("/api/chat/{sid}", chat_get),
        Route("/api/chat/{sid}", chat_delete, methods=["DELETE"]),
        Route("/api/chat/{sid}/reset", chat_reset, methods=["POST"]),
        Route("/api/chat/{sid}/approvals/{aid}", chat_approval, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    return Starlette(
        routes=routes,
        lifespan=lifespan,
        middleware=[Middleware(LocalOnlyMiddleware, port=settings.port, extra_hosts=extra_hosts)],
        exception_handlers={
            HubError: on_hub_error,
            LocalAsistanError: on_local_error,
            Exception: on_unexpected,
        },
    )
