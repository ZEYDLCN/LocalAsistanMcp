from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from localasistan import cli
from localasistan.client.agent import ChatManager
from localasistan.client.hub import McpHub
from localasistan.config import Settings
from localasistan.doctor import FAIL, OK, WARN, run_checks
from localasistan.errors import LocalAsistanError
from tests.anthropic_fake import FakeAnthropic, api_error, message_stream
from tests.support import ScriptedProvider, Step, free_port


@pytest.fixture
def env(tmp_path, monkeypatch):
    """CLI'yi gerçek ortam değişkenleriyle çalıştırır; repo kökündeki .env'den bağımsız."""
    for key in [k for k in os.environ if k.startswith("LOCALASISTAN_") or k.startswith("ANTHROPIC_")]:
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("LOCALASISTAN_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALASISTAN_TZ", "Europe/Istanbul")
    return tmp_path


class TestParsing:
    def test_pairs_and_json(self):
        assert cli.parse_call_arguments(None, ["path=a.txt", "max_chars=5", "recursive=true", "q=çay kahve"]) == {
            "path": "a.txt", "max_chars": 5, "recursive": True, "q": "çay kahve",
        }  # fmt: skip
        assert cli.parse_call_arguments('{"a": 1, "b": [1,2]}', ["a=2"]) == {"a": 2, "b": [1, 2]}  # çiftler JSON'u ezer
        assert cli.parse_call_arguments(None, ["x=null", 'y="quoted"', "z=1.5"]) == {"x": None, "y": "quoted", "z": 1.5}
        assert cli.parse_call_arguments(None, ["eq=a=b"]) == {"eq": "a=b"}

    @pytest.mark.parametrize(
        ("raw", "pairs"), [("{bad", []), ("[1]", []), ('"s"', []), (None, ["noequals"]), (None, ["=v"])]
    )
    def test_invalid(self, raw, pairs):
        with pytest.raises(LocalAsistanError):
            cli.parse_call_arguments(raw, pairs)


class TestCommands:
    def test_config_hides_secrets(self, env, monkeypatch, capsys):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
        assert cli.main(["config"]) == 0
        out = capsys.readouterr().out
        assert "claude-opus-5-5" in out and "Europe/Istanbul" in out and "sk-ant-secret-value" not in out

    def test_invalid_configuration_exits_2(self, env, monkeypatch, capsys):
        monkeypatch.setenv("LOCALASISTAN_TZ", "Mars/Base")
        assert cli.main(["config"]) == 2
        assert "Yapılandırma hatası" in capsys.readouterr().err

    def test_dotenv_in_cwd_is_used_and_real_env_wins(self, env, monkeypatch, capsys):
        (env / ".env").write_text("LOCALASISTAN_MODEL=file-model\nLOCALASISTAN_EFFORT=high\n")
        monkeypatch.setenv("LOCALASISTAN_EFFORT", "low")
        cli.main(["config"])
        out = capsys.readouterr().out
        assert "file-model" in out and "low" in out

    def test_seed_is_idempotent_and_never_overwrites(self, env, capsys):
        assert cli.main(["seed"]) == 0
        first = capsys.readouterr().out
        assert "4 dosya" in first and "5 takvim" in first and "2 tablo" in first
        workspace = env / "home" / "workspace"
        (workspace / "notlar" / "hosgeldin.md").write_text("BENİM İÇERİĞİM")
        assert cli.main(["seed"]) == 0
        second = capsys.readouterr().out
        assert "0 dosya" in second and second.count("atlandı") == 3
        assert cli.main(["seed", "--force"]) == 0
        capsys.readouterr()
        assert (workspace / "notlar" / "hosgeldin.md").read_text() == "BENİM İÇERİĞİM"

    def test_seed_does_not_touch_a_non_empty_workspace(self, env, capsys):
        workspace = env / "home" / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "benim.txt").write_text("x")
        cli.main(["seed"])
        assert "workspace (not empty)" in capsys.readouterr().out
        assert [p.name for p in workspace.iterdir()] == ["benim.txt"]

    def test_tools_listing(self, env, capsys):
        assert cli.main(["tools"]) == 0
        out = capsys.readouterr().out
        assert "25 araç" in out and "fs_delete" in out and "[destructive]" in out and "[read" in out

    def test_call_success_error_and_bad_arguments(self, env, capsys):
        assert cli.main(["call", "calendar_now"]) == 0
        assert json.loads(capsys.readouterr().out)["timezone"] == "Europe/Istanbul"
        assert cli.main(["call", "fs_write", "path=a.txt", "content=Şükrü ✓"]) == 0
        capsys.readouterr()
        assert cli.main(["call", "fs_read", "path=a.txt"]) == 0
        assert json.loads(capsys.readouterr().out)["content"] == "Şükrü ✓"
        assert cli.main(["call", "fs_read", "path=../../etc/passwd"]) == 1
        captured = capsys.readouterr()
        assert "outside the workspace" in captured.err and captured.out == ""
        assert cli.main(["call", "nope_tool"]) == 1
        assert cli.main(["call", "fs_read", "--json", "{broken"]) == 1
        assert "geçerli JSON değil" in capsys.readouterr().err

    def test_desktop_config_is_valid_json_with_absolute_interpreter(self, env, capsys):
        assert cli.main(["desktop-config"]) == 0
        captured = capsys.readouterr()
        snippet = json.loads(captured.out)
        server = snippet["mcpServers"]["localasistan"]
        assert server["command"] == sys.executable and Path(server["command"]).is_absolute()
        assert server["args"] == ["-m", "localasistan.server"]
        assert set(server["env"]) == {"LOCALASISTAN_HOME", "LOCALASISTAN_WORKSPACE", "LOCALASISTAN_TZ"}
        assert "Application Support/Claude" in captured.err

    def test_ui_refuses_non_loopback_without_explicit_opt_in(self, env, capsys):
        assert cli.main(["ui", "--host", "0.0.0.0", "--no-browser"]) == 2
        assert "LOCALASISTAN_ALLOW_REMOTE" in capsys.readouterr().err

    def test_ui_reports_a_busy_port(self, env, capsys):
        import socket

        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen()
            port = sock.getsockname()[1]
            assert cli.main(["ui", "--port", str(port), "--no-browser"]) == 1
        assert "kullanımda" in capsys.readouterr().err

    def test_unknown_command_and_help(self, env, capsys):
        with pytest.raises(SystemExit) as info:
            cli.main(["bogus"])
        assert info.value.code == 2
        with pytest.raises(SystemExit) as info:
            cli.main(["--version"])
        assert info.value.code == 0 and "localasistan 0.1.0" in capsys.readouterr().out


class TestDoctor:
    def test_healthy_without_api_key_warns_only(self, env, capsys):
        assert cli.main(["doctor"]) == 0
        out = capsys.readouterr().out
        assert (
            "MCP sunucusu başladı" in out
            and "araç sayısı — 25" in out
            and "ANTHROPIC_API_KEY" in out
            and "Hazır (1 uyarı)" in out
        )

    def test_live_llm_requires_a_key(self, env, capsys):
        assert cli.main(["doctor", "--llm"]) == 1
        assert "ANTHROPIC_API_KEY ayarlı değil" in capsys.readouterr().out

    async def test_live_llm_success_exercises_the_exact_chat_request(self, settings):
        fake = FakeAnthropic([message_stream([("text", "OK")], usage=(5400, 4))])
        from localasistan.client.providers import AnthropicProvider

        checks = await run_checks(settings, live_llm=True, provider=AnthropicProvider(settings, client=fake.client()))
        live = checks[-1]
        assert live.status == OK and "model=claude-opus-5-5" in live.detail and "5400 tok" in live.detail
        body = fake.body()
        assert len(body["tools"]) == 25 and body["thinking"] == {"type": "adaptive"} and body["fallbacks"] == "default"
        assert all(c.status != FAIL for c in checks)

    async def test_live_llm_failure_is_reported_not_raised(self, settings):
        fake = FakeAnthropic([api_error(401, "authentication_error", "invalid x-api-key")])
        from localasistan.client.providers import AnthropicProvider

        checks = await run_checks(settings, live_llm=True, provider=AnthropicProvider(settings, client=fake.client()))
        live = checks[-1]
        assert live.status == FAIL and "[auth]" in live.detail

    async def test_missing_api_key_is_a_warning_in_checks(self, settings):
        checks = await run_checks(settings)
        key = next(c for c in checks if c.name == "ANTHROPIC_API_KEY")
        assert key.status == WARN


class TestTerminalChat:
    async def drive(self, hub, settings, provider, lines):
        manager = ChatManager(hub, provider, settings)
        session = manager.create()
        feed = iter(lines)
        out: list[str] = []
        prompts: list[str] = []

        async def read_line(prompt: str) -> str:
            prompts.append(prompt)
            try:
                return next(feed)
            except StopIteration:
                raise EOFError from None

        await cli.chat_loop(session, read_line, out.append)
        return "".join(out), prompts, session

    async def test_conversation_with_tool_and_commands(self, settings, workspace):
        (workspace / "n.txt").write_text("süt")
        hub = McpHub(__import__("localasistan.server.app", fromlist=["build_server"]).build_server(settings))
        try:
            provider = ScriptedProvider(
                [
                    Step(text="Bakıyorum.", tools=[("fs_read", {"path": "n.txt"})]),
                    Step(text="Süt var."),
                    Step(text="yeni"),
                ]
            )
            out, prompts, session = await self.drive(
                hub,
                settings,
                provider,
                ["", "/araclar", "listede ne var?", "/yeni", "tekrar", "/cikis", "asla okunmaz"],
            )
        finally:
            await hub.stop()
        assert "fs_read" in out and "⚙ fs_read" in out and "✓" in out and "Süt var." in out
        assert "Yeni sohbet başlatıldı." in out
        assert prompts.count("› ") == 6  # "/cikis"ten sonra okunmaz
        assert [m["role"] for m in session.messages] == ["user", "assistant"]  # /yeni sonrası yalnızca son tur

    async def test_approval_prompt_yes_and_default_no(self, settings, workspace):
        (workspace / "a.txt").write_text("1")
        (workspace / "b.txt").write_text("2")
        hub = McpHub(__import__("localasistan.server.app", fromlist=["build_server"]).build_server(settings))
        try:
            provider = ScriptedProvider(
                [Step(tools=[("fs_delete", {"path": "a.txt"})]), Step(text="a silindi"),
                 Step(tools=[("fs_delete", {"path": "b.txt"})]), Step(text="b kaldı")]
            )  # fmt: skip
            out, prompts, _ = await self.drive(hub, settings, provider, ["a'yı sil", "e", "b'yi sil", ""])
        finally:
            await hub.stop()
        assert not (workspace / "a.txt").exists() and (workspace / "b.txt").exists()
        assert "Onay gerekiyor" in out and out.count("Onaylıyor musunuz?") == 0  # istem prompt'ta, çıktıda değil
        assert prompts.count("  Onaylıyor musunuz? [e/H] ") == 2

    async def test_provider_errors_are_printed_and_chat_continues(self, settings):
        from localasistan.client.providers import ProviderError

        hub = McpHub(__import__("localasistan.server.app", fromlist=["build_server"]).build_server(settings))
        try:
            provider = ScriptedProvider(
                [Step(error=ProviderError("rate_limit", "Rate limit reached.")), Step(text="geldi")]
            )
            out, _, _ = await self.drive(hub, settings, provider, ["bir", "iki"])
        finally:
            await hub.stop()
        assert "Hata: Rate limit reached." in out and "geldi" in out


@pytest.mark.stdio
class TestRealUiProcess:
    def test_ui_process_serves_requests_and_shuts_down_cleanly_without_orphans(self, tmp_path):
        port = free_port()
        home = tmp_path / "home"
        proc_env = {k: v for k, v in os.environ.items() if not k.startswith(("LOCALASISTAN_", "ANTHROPIC_"))}
        proc_env.update({"LOCALASISTAN_HOME": str(home), "LOCALASISTAN_TZ": "Europe/Istanbul", "HOME": str(tmp_path)})
        proc = subprocess.Popen(
            [sys.executable, "-m", "localasistan", "ui", "--port", str(port), "--no-browser"],
            env=proc_env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )  # fmt: skip
        try:
            deadline = time.monotonic() + 40
            status = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    pytest.fail(f"ui exited early: {proc.stderr.read()}")
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=2) as response:
                        status = json.load(response)
                    if status["mcp"]["connected"]:
                        break
                except OSError:
                    time.sleep(0.2)
            assert status and status["mcp"]["tool_count"] == 25 and status["llm"]["configured"] is False
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/tools/fs_write/call",
                data=json.dumps({"arguments": {"path": "süreç.txt", "content": "ok"}}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                assert json.load(response)["is_error"] is False
            assert (home / "workspace" / "süreç.txt").read_text() == "ok"
            # MCP alt süreci şu an çalışıyor
            children = subprocess.run(
                ["pgrep", "-P", str(proc.pid)], capture_output=True, text=True, check=False
            ).stdout.split()
            assert children, "MCP sunucu alt süreci bulunamadı"
        finally:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
                pytest.fail("ui did not shut down on SIGINT")
        assert proc.returncode == 0
        time.sleep(0.5)
        orphans = subprocess.run(
            ["pgrep", "-f", "localasistan.server.*"], capture_output=True, text=True, check=False
        ).stdout.split()
        assert not [
            pid
            for pid in orphans
            if pid not in (str(os.getpid()),)
            and Path(f"/proc/{pid}/environ").exists()
            and str(home) in Path(f"/proc/{pid}/environ").read_text(errors="ignore")
        ]

    def test_ui_process_exits_cleanly_on_sigterm_too(self, tmp_path):
        port = free_port()
        proc_env = {k: v for k, v in os.environ.items() if not k.startswith(("LOCALASISTAN_", "ANTHROPIC_"))}
        proc_env.update({"LOCALASISTAN_HOME": str(tmp_path / "h"), "HOME": str(tmp_path)})
        proc = subprocess.Popen(
            [sys.executable, "-m", "localasistan", "ui", "--port", str(port), "--no-browser"],
            env=proc_env,
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1).read()
                    break
                except OSError:
                    time.sleep(0.2)
        finally:
            proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=20) == 0


class TestRemainingCliPaths:
    def test_python_dash_m_entrypoint(self, tmp_path):
        proc = subprocess.run(
            [sys.executable, "-m", "localasistan", "--version"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
            check=False,
        )
        assert proc.returncode == 0 and "localasistan 0.1.0" in proc.stdout

    @pytest.mark.stdio
    def test_server_command_speaks_mcp_on_stdio_and_exits_cleanly_on_eof(self, tmp_path):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("LOCALASISTAN_", "ANTHROPIC_"))}
        env.update({"LOCALASISTAN_HOME": str(tmp_path / "h"), "HOME": str(tmp_path)})
        proc = subprocess.run(
            [sys.executable, "-m", "localasistan", "server"],
            input=b"",
            capture_output=True,
            env=env,
            cwd=tmp_path,
            check=False,
        )
        assert proc.returncode == 0 and proc.stdout == b""

    def test_keyboard_interrupt_exits_130(self, env, monkeypatch, capsys):
        def interrupted(args, settings):
            raise KeyboardInterrupt

        monkeypatch.setattr(cli, "cmd_config", interrupted)
        parser_patch = cli.build_parser()
        monkeypatch.setattr(cli, "build_parser", lambda: parser_patch)
        for action in parser_patch._subparsers._group_actions:
            action.choices["config"].set_defaults(func=interrupted)
        assert cli.main(["config"]) == 130

    def test_chat_command_with_an_injected_provider_and_terminal_input(self, env, monkeypatch, capsys):
        lines = iter(["merhaba", "/cikis"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(lines))
        provider = ScriptedProvider([Step(text="Selam, ben asistan!")])
        args = cli.build_parser().parse_args(["chat"])
        assert cli.cmd_chat(args, Settings_from(env), provider) == 0
        assert "Selam, ben asistan!" in capsys.readouterr().out

    def test_chat_command_requires_an_api_key(self, env, capsys):
        args = cli.build_parser().parse_args(["chat"])
        assert cli.cmd_chat(args, Settings_from(env)) == 2
        assert "ANTHROPIC_API_KEY" in capsys.readouterr().err

    def test_chat_command_reports_an_unavailable_server(self, env, monkeypatch, capsys):
        from localasistan.client.hub import HubError

        async def fail(self):
            raise HubError("sunucu yok")

        monkeypatch.setattr(McpHub, "start", fail)
        args = cli.build_parser().parse_args(["chat"])
        assert cli.cmd_chat(args, Settings_from(env), ScriptedProvider([])) == 1
        assert "sunucu yok" in capsys.readouterr().err

    def test_tools_command_reports_an_unavailable_server(self, env, monkeypatch, capsys):
        from localasistan.client.hub import HubError

        async def fail(self, *, refresh=False):
            raise HubError("bağlanılamadı")

        monkeypatch.setattr(McpHub, "list_tools", fail)
        assert cli.main(["tools"]) == 1
        assert "bağlanılamadı" in capsys.readouterr().err

    async def test_doctor_reports_unwritable_directories_and_a_dead_server(self, settings, monkeypatch, tmp_path):
        blocker = tmp_path / "blocker"
        blocker.write_text("bir dosya")
        broken = settings.with_overrides(home=blocker / "home", workspace=blocker / "ws")
        checks = await run_checks(broken)
        assert [c.status for c in checks if c.name.startswith("yazılabilir")] == [FAIL, FAIL]
        assert next(c for c in checks if c.name == "MCP sunucusu").status == FAIL

    async def test_doctor_live_check_edge_cases(self, settings):
        from localasistan.doctor import _live_llm_check

        assert (await _live_llm_check(settings.with_overrides(api_key="sk"), [])).status == FAIL  # araç yok

        class Silent:
            name, is_configured = "silent", True

            async def stream_turn(self, **_kw):
                return
                yield

        spec = SimpleNamespace(name="t", title="t", description="d", input_schema={"type": "object"})
        result = await _live_llm_check(settings, [spec], Silent())
        assert result.status == FAIL and "yanıt alınamadı" in result.detail

        class Exploding(Silent):
            async def stream_turn(self, **_kw):
                raise RuntimeError("boom")
                yield

        assert "RuntimeError" in (await _live_llm_check(settings, [spec], Exploding())).detail


def Settings_from(_env_dir) -> Settings:
    """`env` fixture'ının ortam değişkenlerinden ayarları üretir."""
    return Settings.from_env()
