from __future__ import annotations

import contextlib
import sys
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from localasistan.config import Settings
from localasistan.server.app import build_server

# 2026-11-05 Perşembe 12:00 İstanbul (UTC+3)
FIXED_NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    env = {
        "LOCALASISTAN_HOME": str(tmp_path / "home"),
        "LOCALASISTAN_TZ": "Europe/Istanbul",
        "LOCALASISTAN_DB_TIMEOUT": "1.5",
        "LOCALASISTAN_MAX_FILE_BYTES": "200000",
    }
    return Settings.from_env(env)


@pytest.fixture
def workspace(settings: Settings) -> Path:
    settings.ensure_dirs()
    return settings.workspace.resolve()


@pytest.fixture
def connect(settings: Settings) -> Callable[..., contextlib.AbstractAsyncContextManager[Client]]:
    """`async with connect() as client:` — süreç-içi MCP bağlantısı (sabit saatli).

    anyio görev grubu aynı görevde girilip çıkılmalı; bu yüzden async fixture yerine fabrika.
    """

    @contextlib.asynccontextmanager
    async def factory(now=lambda: FIXED_NOW) -> AsyncIterator[Client]:
        async with Client(build_server(settings, now=now)) as client:
            yield client

    return factory


@pytest.fixture
def stdio_params(settings: Settings) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=["-m", "localasistan.server"], env=settings.server_env())


async def call(client: Client, name: str, arguments: dict | None = None):
    return await client.call_tool(name, arguments or {})


def text_of(result) -> str:
    return "".join(c.text for c in result.content if getattr(c, "type", "") == "text")


@pytest.fixture
def web_settings(settings: Settings) -> Settings:
    from tests.support import free_port

    return settings.with_overrides(port=free_port())


@pytest.fixture
def live(web_settings: Settings):
    """`live(provider, stdio=False, **overrides)` -> çalışan gerçek uvicorn sunucusu (varsayılan: süreç-içi MCP)."""
    from localasistan.web.app import create_app
    from tests.support import LiveServer

    servers: list[LiveServer] = []

    def start(provider, *, stdio: bool = False, **overrides) -> LiveServer:
        cfg = web_settings.with_overrides(**overrides) if overrides else web_settings
        target = (
            StdioServerParameters(command=sys.executable, args=["-m", "localasistan.server"], env=cfg.server_env())
            if stdio
            else build_server(cfg)
        )
        server = LiveServer(create_app(cfg, hub_target=target, provider=provider), cfg.port).__enter__()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.__exit__(None, None, None)
