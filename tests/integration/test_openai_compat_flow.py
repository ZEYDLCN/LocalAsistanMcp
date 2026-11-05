"""OpenAI uyumlu sağlayıcı + ajan döngüsü + GERÇEK MCP sunucusu + CLI/doktor/web: uçtan uca akışlar (sahte HTTP)."""

from __future__ import annotations

import json
import os

import pytest
from starlette.testclient import TestClient

from localasistan import cli
from localasistan.client.agent import ChatManager
from localasistan.client.hub import McpHub
from localasistan.client.openai_compat import OpenAICompatProvider
from localasistan.config import Settings
from localasistan.doctor import FAIL, OK, WARN, _tool_call_check, run_checks
from localasistan.server.app import build_server
from localasistan.web.app import create_app
from tests.openai_fake import FakeOpenAI, error_body, text_stream, tool_stream
from tests.support import assert_valid_history, drive, types_of

BASE = "https://integrate.api.nvidia.com/v1"


@pytest.fixture
def compat(settings):
    return settings.with_overrides(
        provider="openai_compat", base_url=BASE, model="z-ai/glm-test", api_key="nvapi-secret", max_tokens=8192
    )


@pytest.fixture
async def hub(settings):
    hub = McpHub(build_server(settings))
    await hub.start()
    yield hub
    await hub.stop()


def session_for(hub, compat, fake: FakeOpenAI, **overrides):
    cfg = compat.with_overrides(**overrides)
    provider = OpenAICompatProvider(cfg, client=fake.client(), retry_delays=(0.0, 0.0))
    return ChatManager(hub, provider, cfg).create()


class TestAgentLoopWithARealServer:
    async def test_tool_round_trip_runs_the_real_tool_and_feeds_the_result_back(self, hub, compat):
        fake = FakeOpenAI(
            [
                tool_stream([("call_1", "calendar_now", "{}")], text="Saate bakıyorum."),
                text_stream("Şu an Perşembe öğleden sonra."),
            ]
        )
        session = session_for(hub, compat, fake)
        events = await drive(session, "saat kaç?")

        assert types_of(events)[0] == "turn_start" and types_of(events)[-1] == "turn_end"
        assert events[-1].data["stop_reason"] == "end_turn"
        assert "".join(e.data["text"] for e in events if e.type == "text_delta").endswith("öğleden sonra.")
        assert [e.data["name"] for e in events if e.type == "tool_call"] == ["calendar_now"]

        second = fake.body(1)["messages"]
        assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
        assert second[2]["tool_calls"][0]["id"] == "call_1"
        assert json.loads(second[3]["content"])["timezone"] == "Europe/Istanbul"  # gerçek araç sonucu
        assert second[3]["tool_call_id"] == "call_1"
        assert_valid_history(session.messages)

    async def test_destructive_tool_waits_for_approval_and_respects_a_denial(self, hub, compat, workspace):
        (workspace / "eski.txt").write_text("kalsın")
        fake = FakeOpenAI(
            [
                tool_stream([("c1", "fs_delete", '{"path": "eski.txt"}')]),
                text_stream("Tamam, silmedim."),
            ]
        )
        session = session_for(hub, compat, fake)
        events = await drive(session, "eski.txt'yi sil", approve=lambda e: (False, "Hayır"))
        assert "approval_required" in types_of(events)
        assert (workspace / "eski.txt").exists()
        tool_msg = fake.body(1)["messages"][-1]
        assert tool_msg["role"] == "tool" and "Hayır" in tool_msg["content"]

    async def test_destructive_tool_runs_after_the_user_approves(self, hub, compat, workspace):
        (workspace / "eski.txt").write_text("silinecek")
        fake = FakeOpenAI([tool_stream([("c1", "fs_delete", '{"path": "eski.txt"}')]), text_stream("Silindi.")])
        events = await drive(session_for(hub, compat, fake), "sil", approve=lambda e: (True, ""))
        assert not (workspace / "eski.txt").exists()
        assert events[-1].data["stop_reason"] == "end_turn"

    async def test_unreadable_tool_json_is_retried_transparently(self, hub, compat):
        fake = FakeOpenAI(
            [
                tool_stream([("c1", "calendar_now", '{"bozuk')]),
                tool_stream([("c2", "calendar_now", "{}")]),
                text_stream("Tamam."),
            ]
        )
        events = await drive(session_for(hub, compat, fake), "saat?")
        assert events[-1].data["stop_reason"] == "end_turn" and len(fake.requests) == 3
        assert any(e.type == "notice" and "Retrying" in e.data["message"] for e in events)

    async def test_a_cut_off_tool_call_is_not_executed(self, hub, compat, workspace):
        fake = FakeOpenAI(
            [
                tool_stream([("c1", "fs_write", '{"path": "yarim.txt", "content": "ab')], finish="length"),
                text_stream("Daha küçük parçalarla yazacağım."),
            ]
        )
        events = await drive(session_for(hub, compat, fake), "uzun dosya yaz")
        assert not (workspace / "yarim.txt").exists()
        assert "cut off" in fake.body(1)["messages"][-1]["content"]
        assert any(e.type == "notice" and "cut off" in e.data["message"] for e in events)

    async def test_provider_errors_surface_as_error_events_and_leave_history_clean(self, hub, compat):
        fake = FakeOpenAI([error_body("invalid api key", status=401)])
        session = session_for(hub, compat, fake)
        events = await drive(session, "merhaba")
        error = next(e for e in events if e.type == "error")
        assert error.data["kind"] == "auth" and "nvapi-secret" not in json.dumps(error.data)
        assert session.messages == []  # yanıtsız kullanıcı mesajı geri alındı

    async def test_missing_model_is_reported_with_a_helpful_message(self, hub, compat):
        fake = FakeOpenAI([])
        events = await drive(session_for(hub, compat, fake, model=""), "merhaba")
        error = next(e for e in events if e.type == "error")
        assert error.data["kind"] == "no_api_key"
        assert "LOCALASISTAN_MODEL" in error.data["message"] and "localasistan models" in error.data["message"]
        assert fake.requests == []

    async def test_content_filter_is_shown_as_a_refusal_notice(self, hub, compat):
        fake = FakeOpenAI([text_stream("", finish="content_filter")])
        events = await drive(session_for(hub, compat, fake), "x")
        assert events[-1].data["stop_reason"] == "refusal"
        assert any(e.type == "notice" and "declined" in e.data["message"] for e in events)

    async def test_multi_turn_history_is_resent_in_openai_shape(self, hub, compat):
        fake = FakeOpenAI([text_stream("Birinci."), text_stream("İkinci.")])
        session = session_for(hub, compat, fake)
        await drive(session, "bir")
        await drive(session, "iki")
        roles = [m["role"] for m in fake.body(1)["messages"]]
        assert roles == ["system", "user", "assistant", "user"]
        assert fake.body(1)["messages"][2]["content"] == "Birinci."


class TestCli:
    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        for key in [k for k in os.environ if k.startswith(("LOCALASISTAN_", "ANTHROPIC_"))]:
            monkeypatch.delenv(key)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
        monkeypatch.setenv("LOCALASISTAN_HOME", str(tmp_path / "home"))
        monkeypatch.setenv("LOCALASISTAN_TZ", "Europe/Istanbul")
        return tmp_path

    def test_models_requires_the_compat_provider(self, env, capsys):
        assert cli.main(["models"]) == 2
        assert "openai_compat" in capsys.readouterr().err

    def test_models_lists_filters_and_counts(self, compat, capsys):
        fake = FakeOpenAI([(200, {"data": [{"id": "z-ai/glm-5"}, {"id": "meta/llama-3"}, {"id": "z-ai/GLM-4"}]})])
        args = cli.build_parser().parse_args(["models", "--filter", "glm"])
        assert cli.cmd_models(args, compat, fake.sync_client()) == 0
        out = capsys.readouterr()
        assert out.out.split() == ["z-ai/GLM-4", "z-ai/glm-5"] and "2 / 3 model" in out.err
        assert fake.header("authorization") == "Bearer nvapi-secret"

    def test_models_with_no_match_exits_1(self, compat, capsys):
        fake = FakeOpenAI([(200, {"data": [{"id": "meta/llama-3"}]})])
        args = cli.build_parser().parse_args(["models", "--filter", "glm"])
        assert cli.cmd_models(args, compat, fake.sync_client()) == 1
        assert "0 / 1 model" in capsys.readouterr().err

    def test_models_reports_server_errors_without_leaking_the_key(self, compat, capsys):
        fake = FakeOpenAI([error_body("bad key", status=401)])
        args = cli.build_parser().parse_args(["models"])
        assert cli.cmd_models(args, compat, fake.sync_client()) == 1
        err = capsys.readouterr().err
        assert "API key" in err and "nvapi-secret" not in err

    def test_config_command_shows_provider_but_hides_the_key(self, env, monkeypatch, capsys):
        monkeypatch.setenv("LOCALASISTAN_PROVIDER", "openai_compat")
        monkeypatch.setenv("LOCALASISTAN_BASE_URL", BASE)
        monkeypatch.setenv("LOCALASISTAN_MODEL", "z-ai/glm-test")
        monkeypatch.setenv("LOCALASISTAN_API_KEY", "nvapi-top-secret")
        assert cli.main(["config"]) == 0
        out = capsys.readouterr().out
        assert "openai_compat" in out and "z-ai/glm-test" in out and "nvapi-top-secret" not in out

    def test_bad_provider_configuration_exits_2(self, env, monkeypatch, capsys):
        monkeypatch.setenv("LOCALASISTAN_PROVIDER", "openai_compat")
        monkeypatch.setenv("LOCALASISTAN_BASE_URL", "http://example.com/v1")
        monkeypatch.setenv("LOCALASISTAN_API_KEY", "k")
        assert cli.main(["config"]) == 2
        assert "plain http" in capsys.readouterr().err

    def test_chat_command_names_the_missing_setting(self, env, monkeypatch, capsys):
        monkeypatch.setenv("LOCALASISTAN_PROVIDER", "openai_compat")
        monkeypatch.setenv("LOCALASISTAN_BASE_URL", BASE)
        args = cli.build_parser().parse_args(["chat"])
        assert cli.cmd_chat(args, Settings.from_env()) == 2
        assert "LOCALASISTAN_MODEL" in capsys.readouterr().err


class TestDoctor:
    async def test_compat_configuration_rows(self, compat):
        checks = await run_checks(compat)
        row = next(c for c in checks if c.name.startswith("LLM sağlayıcısı"))
        assert row.status == OK and BASE in row.detail and "z-ai/glm-test" in row.detail
        assert next(c for c in checks if c.name == "LOCALASISTAN_API_KEY").status == OK
        assert all(c.name != "ANTHROPIC_API_KEY" for c in checks)
        assert "nvapi-secret" not in str([c.detail for c in checks])

    async def test_missing_model_is_a_warning_row_and_a_failed_live_check(self, compat):
        broken = compat.with_overrides(model="", api_key=None)
        checks = await run_checks(broken, live_llm=True)
        assert next(c for c in checks if c.name.startswith("LLM sağlayıcısı")).status == WARN
        assert next(c for c in checks if c.name == "LOCALASISTAN_API_KEY").status == WARN
        live = next(c for c in checks if c.name == "canlı LLM çağrısı")
        assert live.status == FAIL and "LOCALASISTAN_MODEL" in live.detail

    async def test_live_check_then_tool_check_pass_with_a_capable_model(self, compat):
        fake = FakeOpenAI([text_stream("OK"), tool_stream([("c1", "calendar_now", "{}")])])
        provider = OpenAICompatProvider(compat, client=fake.client())
        checks = await run_checks(compat, live_llm=True, provider=provider)
        live = next(c for c in checks if c.name.startswith("canlı LLM"))
        tool = next(c for c in checks if c.name.startswith("araç çağırma"))
        assert live.status == OK and "z-ai/glm-test" in live.detail
        assert tool.status == OK and "calendar_now" in tool.detail
        assert len(fake.body(1)["tools"]) == 25
        assert all(c.status != FAIL for c in checks)

    async def test_a_model_that_answers_in_text_instead_of_calling_a_tool_gets_a_warning(self, compat, hub_specs):
        fake = FakeOpenAI([text_stream("Bilmiyorum, tahmin edeyim: saat 12.")])
        provider = OpenAICompatProvider(compat, client=fake.client())
        check = await _tool_call_check(compat, hub_specs, provider)
        assert check.status == WARN and "araç çağırmadı" in check.detail

    async def test_invalid_arguments_and_unknown_tools_are_flagged(self, compat, hub_specs):
        bad_args = FakeOpenAI([tool_stream([("c1", "fs_read", '{"path": 5}')])])
        check = await _tool_call_check(compat, hub_specs, OpenAICompatProvider(compat, client=bad_args.client()))
        assert check.status == WARN and "geçersiz argüman" in check.detail
        unknown = FakeOpenAI([tool_stream([("c1", "yok_boyle_arac", "{}")])])
        check = await _tool_call_check(compat, hub_specs, OpenAICompatProvider(compat, client=unknown.client()))
        assert check.status == WARN and "tanımsız" in check.detail

    async def test_provider_failures_during_the_tool_check_are_warnings_not_failures(self, compat, hub_specs):
        fake = FakeOpenAI([error_body("nope", status=400)])
        check = await _tool_call_check(compat, hub_specs, OpenAICompatProvider(compat, client=fake.client()))
        assert check.status == WARN and "yapılamadı" in check.detail

        class Exploding:
            name, is_configured = "x", True

            async def stream_turn(self, **_kw):
                raise RuntimeError("kaboom")
                yield

        check = await _tool_call_check(compat, hub_specs, Exploding())
        assert check.status == WARN and "kaboom" in check.detail

    @pytest.fixture
    async def hub_specs(self, hub):
        return await hub.list_tools()


class TestWeb:
    @pytest.fixture
    def client(self, web_settings):
        cfg = web_settings.with_overrides(
            provider="openai_compat", base_url=BASE, model="z-ai/glm-test", api_key="nvapi-secret", max_tokens=8192
        )
        fake = FakeOpenAI([text_stream("web cevabı")])
        provider = OpenAICompatProvider(cfg, client=fake.client())
        app = create_app(cfg, hub_target=build_server(cfg), provider=provider)
        with TestClient(app, base_url=f"http://127.0.0.1:{cfg.port}") as c:
            c.fake = fake
            yield c

    def test_status_reports_the_compat_provider_without_secrets(self, client):
        llm = client.get("/api/status").json()["llm"]
        assert llm["provider"] == "openai_compat" and llm["model"] == "z-ai/glm-test"
        assert llm["configured"] is True and llm["missing"] is None
        assert "nvapi-secret" not in client.get("/api/status").text

    def test_status_names_what_is_missing(self, web_settings):
        cfg = web_settings.with_overrides(provider="openai_compat", base_url=BASE, model="")
        app = create_app(cfg, hub_target=build_server(cfg), provider=OpenAICompatProvider(cfg))
        with TestClient(app, base_url=f"http://127.0.0.1:{cfg.port}") as c:
            llm = c.get("/api/status").json()["llm"]
        assert llm["configured"] is False and llm["missing"] == "LOCALASISTAN_MODEL"

    def test_default_anthropic_status_still_names_the_api_key(self, web_settings):
        cfg = web_settings.with_overrides(api_key=None)
        app = create_app(cfg, hub_target=build_server(cfg))
        with TestClient(app, base_url=f"http://127.0.0.1:{cfg.port}") as c:
            llm = c.get("/api/status").json()["llm"]
        assert llm["provider"] == "anthropic" and llm["missing"] == "ANTHROPIC_API_KEY"
