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
from .client.providers import AssistantTurn, Provider, ProviderError, create_provider
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
    for package in ("mcp", "anthropic", "httpx2", "starlette", "uvicorn", "jsonschema"):
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
    if settings.provider == "openai_compat":
        ready = bool(settings.base_url and settings.model)
        add(
            Check(
                OK if ready else WARN,
                "LLM sağlayıcısı (openai_compat)",
                f"{settings.base_url or 'LOCALASISTAN_BASE_URL yok'}, "
                f"model={settings.model or 'LOCALASISTAN_MODEL yok'}",
            )
        )
        add(
            Check(
                OK if settings.has_api_key else WARN,
                "LOCALASISTAN_API_KEY",
                "ayarlı"
                if settings.has_api_key
                else "yok — Ollama gibi yerel sunucular anahtar istemez; diğerleri ister",
            )
        )
    else:
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
        provider = provider or create_provider(settings)
        live = await _live_llm_check(settings, specs, provider)
        checks.append(live)
        if live.status == OK and specs:
            checks.append(await _tool_call_check(settings, specs, provider))
    return checks


async def _live_llm_check(settings: Settings, specs: list[Any], provider: Provider | None = None) -> Check:
    provider = provider or create_provider(settings)
    if not provider.is_configured:
        missing = getattr(provider, "missing_setting", None) or "ANTHROPIC_API_KEY"
        return Check(FAIL, "canlı LLM çağrısı", f"{missing} ayarlı değil")
    if not specs:
        return Check(FAIL, "canlı LLM çağrısı", "araç listesi alınamadı (MCP sunucusu başlamadı)")
    tools, _ = anthropic_tools(specs)
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


async def _tool_call_check(settings: Settings, specs: list[Any], provider: Provider) -> Check:
    """Modelin GERÇEK araç şemalarımızla geçerli bir araç çağrısı üretip üretmediğini dener (model seçmeye yarar)."""
    from jsonschema import Draft202012Validator

    tools, _ = anthropic_tools(specs)
    by_name = {t["name"]: t for t in tools}
    try:
        turn: AssistantTurn | None = None
        async for item in provider.stream_turn(
            system="You are a tool-use check. Use the provided tools; never answer from memory.",
            tools=tools,
            messages=[{"role": "user", "content": "What is the current date and time? Use a tool to find out."}],
        ):
            if isinstance(item, AssistantTurn):
                turn = item
    except ProviderError as exc:
        return Check(WARN, "araç çağırma denemesi", f"yapılamadı: [{exc.kind}] {exc.message}")
    except Exception as exc:
        return Check(WARN, "araç çağırma denemesi", f"yapılamadı: {type(exc).__name__}: {exc}")
    label = f"araç çağırma denemesi ({settings.model})"
    if turn is None or not turn.tool_uses:
        said = (turn.text.strip()[:60] if turn else "") or "yanıt yok"
        return Check(
            WARN,
            label,
            f"model araç çağırmadı (yanıt: {said!r}); araç kullanımında zayıf olabilir, başka bir model deneyin",
        )
    use = turn.tool_uses[0]
    spec = by_name.get(use.name)
    if spec is None:
        return Check(WARN, label, f"model tanımsız bir araç çağırdı: {use.name!r}")
    if not Draft202012Validator(spec["input_schema"]).is_valid(use.input):
        return Check(WARN, label, f"{use.name} için geçersiz argüman üretti: {str(use.input)[:80]}")
    return Check(OK, label, f"{use.name}({str(use.input)[:60]}) — geçerli çağrı")


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
