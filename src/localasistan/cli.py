"""`localasistan` komut satırı.

localasistan ui [--port 8765] [--no-browser]   web arayüzü
localasistan server                             MCP sunucusu (stdio) — Claude Desktop vb. için
localasistan tools | call | chat | seed | doctor [--llm] | config | desktop-config
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import socket
import sys
import threading
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import __version__
from .client.agent import ChatManager, ChatSession
from .client.events import Event
from .client.hub import HubError, McpHub
from .client.providers import AnthropicProvider, Provider
from .config import Settings
from .errors import ConfigError, LocalAsistanError
from .logging_setup import configure_logging

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def find_dotenv() -> Path | None:
    for candidate in (Path.cwd() / ".env", Path.home() / ".localasistan" / ".env"):
        if candidate.is_file():
            return candidate
    return None


def server_params(settings: Settings):
    from mcp import StdioServerParameters

    return StdioServerParameters(command=sys.executable, args=["-m", "localasistan.server"], env=settings.server_env())


def paint(text: str, code: str, *, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


# ----------------------------------------------------------------------------------- ui
def _quiet_server_class():
    """uvicorn kapanıştan sonra aldığı sinyali yeniden fırlatır (çıkış kodu 130 / -15). Bu sınıf SIGINT/SIGTERM'i
    kendisi yönetir: ilkinde sakin kapanış (çıkış 0), ikincisinde zorla çıkış."""
    import uvicorn

    class QuietServer(uvicorn.Server):
        def install_signal_handlers(self) -> None:  # eski uvicorn sürümleri
            pass

        @contextlib.contextmanager
        def capture_signals(self):  # yeni uvicorn sürümleri
            def handler(signum, frame) -> None:
                if self.should_exit:
                    self.force_exit = True
                self.should_exit = True

            previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGINT, signal.SIGTERM)}
            try:
                yield
            finally:
                for sig, old in previous.items():
                    signal.signal(sig, old)

    return QuietServer


def cmd_ui(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    from .web.app import create_app

    host = args.host or settings.host
    port = args.port or settings.port
    settings = settings.with_overrides(host=host, port=port)
    remote = host not in LOOPBACK
    if remote and os.environ.get("LOCALASISTAN_ALLOW_REMOTE") != "1":
        print(
            f"Hata: {host} yerel olmayan bir adres. Arayüzde kimlik doğrulama yok; "
            "dosya/veritabanı araçlarını ağa açmak tehlikelidir.\n"
            "Yine de istiyorsanız LOCALASISTAN_ALLOW_REMOTE=1 ayarlayın.",
            file=sys.stderr,
        )
        return 2
    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            print(
                f"Hata: {host}:{port} kullanımda. Başka bir port deneyin: localasistan ui --port {port + 1}",
                file=sys.stderr,
            )
            return 1
    configure_logging(settings.log_level)
    app = create_app(settings, extra_hosts=frozenset({host}) if remote else frozenset())
    server = _quiet_server_class()(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on"))
    url = f"http://{'[' + host + ']' if ':' in host else host}:{port}"

    def announce() -> None:
        while not server.started and not server.should_exit:
            threading.Event().wait(0.1)
        if server.started:
            print(f"LocalAsistan hazır → {url}   (durdurmak için Ctrl+C)")
            if not settings.has_api_key:
                print(
                    "Uyarı: ANTHROPIC_API_KEY ayarlı değil — sohbet kapalı, 'Araçlar' sekmesi çalışır.", file=sys.stderr
                )
            if not args.no_browser:
                webbrowser.open(url)

    threading.Thread(target=announce, daemon=True).start()
    print(f"LocalAsistan v{__version__} başlatılıyor… çalışma alanı: {settings.workspace}")
    if threading.current_thread() is threading.main_thread() and not hasattr(server, "capture_signals"):
        for sig in (signal.SIGINT, signal.SIGTERM):  # eski uvicorn: install_signal_handlers() devre dışı
            signal.signal(sig, lambda *_: setattr(server, "should_exit", True))
    server.run()
    print("LocalAsistan durduruldu.")
    return 0


# -------------------------------------------------------------------------------- server
def cmd_server(args: argparse.Namespace, settings: Settings) -> int:
    from .server.app import build_server

    configure_logging(settings.log_level)
    build_server(settings).run("stdio")
    return 0


# ------------------------------------------------------------------------- tools / call
def cmd_tools(args: argparse.Namespace, settings: Settings) -> int:
    async def run() -> int:
        hub = McpHub(server_params(settings))
        try:
            specs = await hub.list_tools()
        except HubError as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1
        finally:
            await hub.stop()
        width = max(len(s.name) for s in specs)
        for spec in sorted(specs, key=lambda s: s.name):
            print(f"{spec.name:<{width}}  [{spec.kind:<11}]  {spec.title}")
        print(f"\n{len(specs)} araç")
        return 0

    return asyncio.run(run())


def parse_call_arguments(raw_json: str | None, pairs: list[str]) -> dict[str, Any]:
    arguments: dict[str, Any] = {}
    if raw_json:
        try:
            parsed = json.loads(raw_json)
        except ValueError as exc:
            raise LocalAsistanError(f"--json geçerli JSON değil: {exc}") from None
        if not isinstance(parsed, dict):
            raise LocalAsistanError("--json bir JSON nesnesi olmalı.")
        arguments.update(parsed)
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise LocalAsistanError(f"'{pair}' geçersiz: anahtar=değer biçiminde olmalı.")
        try:
            arguments[key] = json.loads(value)
        except ValueError:
            arguments[key] = value
    return arguments


def cmd_call(args: argparse.Namespace, settings: Settings) -> int:
    arguments = parse_call_arguments(args.json, args.pairs)

    async def run() -> int:
        hub = McpHub(server_params(settings), tool_timeout=settings.tool_timeout_seconds)
        try:
            outcome = await hub.call_tool(args.tool, arguments)
        finally:
            await hub.stop()
        text = json.dumps(outcome.data, ensure_ascii=False, indent=2) if outcome.data is not None else outcome.text
        print(text, file=sys.stderr if outcome.is_error else sys.stdout)
        return 1 if outcome.is_error else 0

    return asyncio.run(run())


# ------------------------------------------------------------------------------- chat
async def chat_loop(
    session: ChatSession,
    read_line: Callable[[str], Awaitable[str]],
    write: Callable[[str], None],
    *,
    color: bool = False,
) -> None:
    """Terminal sohbeti. `read_line` EOFError fırlatırsa çıkar."""
    write(
        paint("LocalAsistan sohbeti", "1", enabled=color)
        + "  (/yeni: yeni sohbet · /araclar: araçlar · /cikis: çık · yıkıcı araçlar onay ister)\n"
    )
    while True:
        try:
            line = (await read_line("› ")).strip()
        except (EOFError, KeyboardInterrupt):
            write("\n")
            return
        if not line:
            continue
        if line in ("/cikis", "/exit", "/quit"):
            return
        if line in ("/yeni", "/new"):
            session.reset()
            write("Yeni sohbet başlatıldı.\n")
            continue
        if line == "/araclar":
            specs = await session.hub.list_tools()
            write(", ".join(sorted(s.name for s in specs)) + "\n")
            continue
        streaming_text = False
        try:
            async for event in session.run_turn(line):
                streaming_text = await _render(event, session, read_line, write, color, streaming_text)
        except LocalAsistanError as exc:
            write(paint(f"\nHata: {exc}\n", "31", enabled=color))
        write("\n")


async def _render(
    event: Event,
    session: ChatSession,
    read_line: Callable[[str], Awaitable[str]],
    write: Callable[[str], None],
    color: bool,
    streaming_text: bool,
) -> bool:
    data = event.data
    if event.type == "text_delta":
        write(data["text"])
        return True
    if streaming_text and event.type in ("tool_call", "notice", "error", "turn_end", "approval_required"):
        write("\n")
    if event.type == "tool_call":
        args = json.dumps(data["input"], ensure_ascii=False)
        write(paint(f"⚙ {data['name']}({args[:160]})\n", "36", enabled=color))
    elif event.type == "approval_required":
        summary = json.dumps(data["input"], ensure_ascii=False)[:300]
        write(paint(f"  Onay gerekiyor: {data['title'] or data['name']} {summary}\n", "33", enabled=color))
        try:
            answer = (await read_line("  Onaylıyor musunuz? [e/H] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        session.resolve_approval(data["approval_id"], answer in ("e", "evet", "y", "yes"), "")
    elif event.type == "tool_result":
        mark = paint("✗", "31", enabled=color) if data["is_error"] else paint("✓", "32", enabled=color)
        snippet = data["text"].replace("\n", " ")[:140]
        write(f"  {mark} {data['duration_ms']} ms {snippet if data['is_error'] else ''}\n")
    elif event.type in ("notice", "error"):
        code = "31" if event.type == "error" else "33"
        write(paint(f"{'Hata' if event.type == 'error' else 'Not'}: {data['message']}\n", code, enabled=color))
    return False


def cmd_chat(args: argparse.Namespace, settings: Settings, provider: Provider | None = None) -> int:
    async def run() -> int:
        hub = McpHub(server_params(settings), tool_timeout=settings.tool_timeout_seconds)
        manager = ChatManager(hub, provider or AnthropicProvider(settings), settings)
        try:
            await hub.start()
        except HubError as exc:
            print(f"Hata: {exc}", file=sys.stderr)
            return 1
        if not manager.provider.is_configured:
            print("Hata: ANTHROPIC_API_KEY ayarlı değil (.env dosyasına ekleyin).", file=sys.stderr)
            await hub.stop()
            return 2

        async def read_line(prompt: str) -> str:
            return await asyncio.to_thread(input, prompt)

        try:
            await chat_loop(
                manager.create(),
                read_line,
                lambda s: (sys.stdout.write(s), sys.stdout.flush())[0],
                color=sys.stdout.isatty(),
            )
        finally:
            await hub.stop()
        return 0

    return asyncio.run(run())


# ------------------------------------------------------------------ seed/doctor/config
def cmd_seed(args: argparse.Namespace, settings: Settings) -> int:
    from .seed import seed

    report = seed(settings, force=args.force)
    print(f"Örnek veri: {report['files']} dosya, {report['events']} takvim olayı, {report['tables']} tablo eklendi.")
    for note in report["skipped"]:
        print(f"  atlandı: {note} — zorlamak için --force")
    print(f"Çalışma alanı: {settings.workspace}")
    return 0


def cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    from .doctor import run_doctor

    return run_doctor(settings, live_llm=args.llm)


def cmd_config(args: argparse.Namespace, settings: Settings) -> int:
    for key, value in settings.public_dict().items():
        print(f"{key:<18} {value}")
    return 0


def cmd_desktop_config(args: argparse.Namespace, settings: Settings) -> int:
    snippet = {
        "mcpServers": {
            "localasistan": {
                "command": sys.executable,
                "args": ["-m", "localasistan.server"],
                "env": {
                    k: v
                    for k, v in settings.server_env().items()
                    if k in ("LOCALASISTAN_HOME", "LOCALASISTAN_WORKSPACE", "LOCALASISTAN_TZ")
                },
            }
        }
    }
    print(json.dumps(snippet, indent=2, ensure_ascii=False))
    print(
        "\n↑ Bunu Claude Desktop yapılandırmasına ekleyin:\n"
        "  macOS: ~/Library/Application Support/Claude/claude_desktop_config.json\n"
        "  Ardından Claude Desktop'u yeniden başlatın.",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localasistan", description="Yerel dosya, takvim ve veritabanı için MCP sunucusu + istemcisi + arayüz"
    )
    parser.add_argument("--version", action="version", version=f"localasistan {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="komut")

    ui = sub.add_parser("ui", help="web arayüzünü başlat")
    ui.add_argument("--host", help="bağlanılacak adres (varsayılan 127.0.0.1)")
    ui.add_argument("--port", type=int, help="port (varsayılan 8765)")
    ui.add_argument("--no-browser", action="store_true", help="tarayıcıyı otomatik açma")
    ui.set_defaults(func=cmd_ui)

    sub.add_parser("server", help="MCP sunucusunu stdio üzerinden çalıştır").set_defaults(func=cmd_server)
    sub.add_parser("tools", help="sunucunun araçlarını listele").set_defaults(func=cmd_tools)

    call = sub.add_parser("call", help="bir aracı LLM olmadan çağır")
    call.add_argument("tool")
    call.add_argument("pairs", nargs="*", metavar="anahtar=değer", help="argümanlar (değer JSON ya da düz metin)")
    call.add_argument("--json", help="argümanları JSON nesnesi olarak ver")
    call.set_defaults(func=cmd_call)

    sub.add_parser("chat", help="terminalde LLM ile sohbet").set_defaults(func=cmd_chat)

    seed_p = sub.add_parser("seed", help="örnek dosya/takvim/veritabanı verisi yükle")
    seed_p.add_argument("--force", action="store_true", help="dolu alanlara da ekle (var olanın üzerine yazmaz)")
    seed_p.set_defaults(func=cmd_seed)

    doctor = sub.add_parser("doctor", help="kurulum ve ortam denetimi")
    doctor.add_argument("--llm", action="store_true", help="gerçek bir API çağrısı da dene (küçük bir ücret doğurur)")
    doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("config", help="geçerli yapılandırmayı göster").set_defaults(func=cmd_config)
    sub.add_parser("desktop-config", help="Claude Desktop için mcpServers JSON'u üret").set_defaults(
        func=cmd_desktop_config
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env(dotenv_path=find_dotenv())
    except ConfigError as exc:
        print(f"Yapılandırma hatası: {exc}", file=sys.stderr)
        return 2
    try:
        return int(args.func(args, settings))
    except LocalAsistanError as exc:
        print(f"Hata: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
