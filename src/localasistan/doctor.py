"""`localasistan doctor`: kurulum ve çalışma ortamı denetimi.

`--llm` ile ayrıca GERÇEK bir API çağrısı yapılır: sohbetin kullandığı tam istek biçimi (25 araç,
adaptive thinking, eager_input_streaming, sunucu tarafı fallback) küçük bir istemle denenir.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import sys
from dataclasses import dataclass
from typing import Any

from .client.catalog import anthropic_tools
from .client.events import Event
from .client.hub import McpHub
from .client.providers import AnthropicProvider, AssistantTurn, Provider, ProviderError
from .config import Settings
from .errors import LocalAsistanError

OK, WARN, FAIL = "ok", "warn", "fail"


@dataclass
class Check:
    status: str
    name: str
    detail: str = ""

    def line(self, color: bool) -> str:
        mark = {OK: "✓", WARN: "!", FAIL: "✗"}[self.status]
        if color:
            code = {OK: "32", WARN: "33", FAIL: "31"}[self.status]
            mark = f"\033[{code}m{mark}\033[0m"
        return f"  {mark} {self.name}" + (f" — {self.detail}" if self.detail else "")


def _version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


async def run_checks(settings: Settings, *, live_llm: bool = False, provider: Provider | None = None) -> list[Check]:
    checks: list[Check] = []
    add = checks.append

    py = sys.version_info
    add(Check(OK if py >= (3, 11) else FAIL, "Python", f"{py.major}.{py.minor}.{py.micro} (gerekli: 3.11+)"))
    for package in ("mcp", "anthropic", "starlette", "uvicorn", "jsonschema"):
        version = _version(package)
        add(Check(OK if version else FAIL, f"paket: {package}", version or "kurulu değil"))

    for label, path in (("veri klasörü", settings.home), ("çalışma alanı", settings.workspace)):
        try:
            path.mkdir(parents=True, exist_ok=True)
            probe = path / ".doctor-probe"
            probe.write_text("ok")
            probe.unlink()
            add(Check(OK, f"yazılabilir: {label}", str(path)))
        except OSError as exc:
            add(Check(FAIL, f"yazılabilir: {label}", f"{path}: {exc.strerror}"))

    add(Check(OK, "saat dilimi", settings.timezone))
    add(Check(OK, "onay politikası", settings.approval))
    add(
        Check(
            OK if settings.has_api_key else WARN,
            "ANTHROPIC_API_KEY",
            "ayarlı" if settings.has_api_key else "yok — sohbet çalışmaz; Araçlar sekmesi çalışır",
        )
    )

    # MCP sunucusunu gerçekten başlat
    from mcp import StdioServerParameters

    hub = McpHub(
        StdioServerParameters(command=sys.executable, args=["-m", "localasistan.server"], env=settings.server_env()),
        tool_timeout=settings.tool_timeout_seconds,
    )
    specs = []
    try:
        await hub.start()
        specs = await hub.list_tools()
        info = hub.status()["server"] or {}
        add(
            Check(
                OK,
                "MCP sunucusu başladı",
                f"{info.get('name')} {info.get('version')}, protokol {info.get('protocol_version')}",
            )
        )
        add(Check(OK if len(specs) == 25 else WARN, "araç sayısı", str(len(specs))))
        outcome = await hub.call_tool("calendar_now")
        add(Check(OK if not outcome.is_error else FAIL, "örnek araç çağrısı (calendar_now)", outcome.text[:80]))
    except LocalAsistanError as exc:
        add(Check(FAIL, "MCP sunucusu", str(exc)))
    finally:
        await hub.stop()

    if live_llm:
        checks.append(await _live_llm_check(settings, specs, provider))
    return checks


async def _live_llm_check(settings: Settings, specs: list[Any], provider: Provider | None = None) -> Check:
    if provider is None and not settings.has_api_key:
        return Check(FAIL, "canlı LLM çağrısı", "ANTHROPIC_API_KEY ayarlı değil")
    if not specs:
        return Check(FAIL, "canlı LLM çağrısı", "araç listesi alınamadı (MCP sunucusu başlamadı)")
    tools, _ = anthropic_tools(specs)
    provider = provider or AnthropicProvider(settings)
    try:
        turn: AssistantTurn | None = None
        async for item in provider.stream_turn(
            system="You are a connectivity check. Answer with exactly one word.",
            tools=tools,
            messages=[{"role": "user", "content": "Reply with the single word OK. Do not use any tool."}],
        ):
            if isinstance(item, AssistantTurn):
                turn = item
            elif isinstance(item, Event) and item.type == "text_delta":
                pass
    except ProviderError as exc:
        return Check(FAIL, "canlı LLM çağrısı", f"[{exc.kind}] {exc.message}")
    except Exception as exc:
        return Check(FAIL, "canlı LLM çağrısı", f"{type(exc).__name__}: {exc}")
    if turn is None:
        return Check(FAIL, "canlı LLM çağrısı", "yanıt alınamadı")
    detail = (
        f"model={turn.model}, stop={turn.stop_reason}, giriş={turn.usage.get('input_tokens')} tok, "
        f"çıkış={turn.usage.get('output_tokens')} tok, yanıt={turn.text.strip()[:30]!r}"
    )
    status = OK if turn.stop_reason in ("end_turn", "tool_use") else WARN
    return Check(status, f"canlı LLM çağrısı ({settings.model})", detail)


def print_report(checks: list[Check], *, color: bool) -> int:
    print("LocalAsistan doctor\n")
    for check in checks:
        print(check.line(color))
    failed = sum(c.status == FAIL for c in checks)
    warned = sum(c.status == WARN for c in checks)
    print()
    if failed:
        print(f"{failed} sorun var. Yukarıdaki ✗ satırlarını düzeltin.")
    elif warned:
        print(f"Hazır ({warned} uyarı).")
    else:
        print("Her şey yolunda.")
    return 1 if failed else 0


def run_doctor(settings: Settings, *, live_llm: bool = False, provider: Provider | None = None) -> int:
    checks = asyncio.run(run_checks(settings, live_llm=live_llm, provider=provider))
    return print_report(checks, color=sys.stdout.isatty())
