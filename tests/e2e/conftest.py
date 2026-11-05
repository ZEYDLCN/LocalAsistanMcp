"""Tarayıcı E2E: gerçek uvicorn sunucusu + gerçek MCP alt süreci + Playwright Chromium.

Mac'te ilk kez: `.venv/bin/playwright install chromium`.
Chromium'u elle göstermek için: LOCALASISTAN_CHROMIUM=/yol/chrome
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from localasistan.web.app import create_app
from tests.support import LiveServer, ScriptedProvider

playwright_sync = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.e2e

SHOTS = os.environ.get("LOCALASISTAN_SHOTS")  # ekran görüntüsü klasörü (isteğe bağlı)


@pytest.fixture
def browser():
    # Test başına açılıp kapanır: oturum boyu açık kalan senkron Playwright, ana iş parçacığında bir olay döngüsü
    # bırakır ve sonraki pytest-asyncio testlerini ("Runner.run() cannot be called from a running event loop") bozar.
    with playwright_sync.sync_playwright() as p:
        options: dict = {}
        exe = os.environ.get("LOCALASISTAN_CHROMIUM") or (
            "/opt/pw-browsers/chromium" if Path("/opt/pw-browsers/chromium").exists() else None
        )
        if exe:
            options["executable_path"] = exe
        try:
            instance = p.chromium.launch(**options)
        except Exception as exc:  # tarayıcı yok
            pytest.skip(f"Chromium başlatılamadı ({exc}); `playwright install chromium` çalıştırın")
        yield instance
        instance.close()


@dataclass
class UI:
    url: str
    provider: ScriptedProvider
    page: object
    workspace: Path
    errors: list[str]
    dialogs: list[str] = field(default_factory=list)  # görülen confirm/alert metinleri
    dialog_mode: str = "accept"  # "accept" | "dismiss"

    def shot(self, name: str) -> None:
        if SHOTS:
            Path(SHOTS).mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(Path(SHOTS) / f"{name}.png"), full_page=False)


@pytest.fixture
def make_ui(browser, web_settings, workspace):
    """`make_ui(settings_overrides=..., provider=..., viewport=...)` -> UI."""
    cleanup = []

    def factory(*, provider: ScriptedProvider | None = None, hub_target=None, viewport=None, **overrides) -> UI:
        from tests.support import free_port

        cfg = web_settings.with_overrides(port=free_port(), **overrides)  # her arayüz kendi portunda
        prov = provider or ScriptedProvider([])
        target = hub_target or StdioServerParameters(
            command=sys.executable, args=["-m", "localasistan.server"], env=cfg.server_env()
        )
        server = LiveServer(create_app(cfg, hub_target=target, provider=prov), cfg.port).__enter__()
        context = browser.new_context(viewport=viewport or {"width": 1280, "height": 860}, locale="tr-TR")
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
        page.on(
            "console",
            lambda msg: errors.append(f"console.{msg.type}: {msg.text}") if msg.type in ("error", "warning") else None,
        )
        ui = UI(server.base_url, prov, page, workspace, errors)

        def on_dialog(dialog) -> None:
            ui.dialogs.append(dialog.message)
            dialog.accept() if ui.dialog_mode == "accept" else dialog.dismiss()

        page.on("dialog", on_dialog)
        page.goto(server.base_url)
        cleanup.append((server, context))
        return ui

    yield factory
    for server, context in cleanup:
        context.close()
        server.__exit__(None, None, None)
