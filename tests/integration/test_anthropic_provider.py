"""AnthropicProvider: GERÇEK SDK + sahte HTTP taşıması. İstek biçimi, SSE ayrıştırma, hata eşleme, geçmiş yansıtma."""

from __future__ import annotations

import httpx2
import pytest

from localasistan.client.agent import ChatManager
from localasistan.client.catalog import anthropic_tools
from localasistan.client.events import Event
from localasistan.client.hub import McpHub
from localasistan.client.providers import (
    FALLBACK_BETA,
    AnthropicProvider,
    AssistantTurn,
    PartialToolJsonError,
    ProviderError,
    echo_content,
    extract_tool_uses,
)
from localasistan.server.app import build_server
from tests.anthropic_fake import FakeAnthropic, api_error, message_stream
from tests.support import assert_valid_history, drive, types_of

SYSTEM = "You are a test."
TOOLS = [
    {
        "name": "fs_list",
        "description": "List",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
        "eager_input_streaming": True,
    }
]
MESSAGES = [{"role": "user", "content": "merhaba"}]


async def run(provider: AnthropicProvider, messages=MESSAGES) -> tuple[list[Event], AssistantTurn]:
    events: list[Event] = []
    turn = None
    async for item in provider.stream_turn(system=SYSTEM, tools=TOOLS, messages=messages):
        if isinstance(item, AssistantTurn):
            turn = item
        else:
            events.append(item)
    assert turn is not None
    return events, turn


@pytest.fixture
def provider_for(settings):
    def make(fake: FakeAnthropic, **overrides) -> AnthropicProvider:
        return AnthropicProvider(settings.with_overrides(**overrides), client=fake.client())

    return make


class TestRequestShape:
    async def test_default_request_matches_opus_5_5_requirements(self, provider_for):
        fake = FakeAnthropic([message_stream([("text", "selam")])])
        await run(provider_for(fake))
        request = fake.requests[0]
        assert request.url.path == "/v1/messages" and request.method == "POST"
        assert FALLBACK_BETA in fake.beta_header() and FALLBACK_BETA == "server-side-fallback-2026-07-01"
        body = fake.body()
        assert body["model"] == "claude-opus-5-5"
        assert body["stream"] is True and body["max_tokens"] == 64000
        assert body["fallbacks"] == "default"
        assert body["thinking"] == {"type": "adaptive"}
        assert body["output_config"] == {"effort": "medium"}
        assert body["system"] == SYSTEM and body["messages"] == MESSAGES
        assert body["tools"][0]["eager_input_streaming"] is True
        # Opus 5.5'te 400 üreten parametreler ASLA gönderilmemeli
        for forbidden in ("temperature", "top_p", "top_k", "tool_choice", "budget_tokens"):
            assert forbidden not in body
        assert "budget_tokens" not in str(body["thinking"])
        assert request.headers["authorization"] == "Bearer sk-ant-test"  # SDK 2.x kimlik doğrulamayı kendisi ekler
        assert request.headers["anthropic-version"]

    async def test_fallback_disabled_sends_no_beta_header_or_fallbacks(self, provider_for):
        fake = FakeAnthropic([message_stream([("text", "x")])])
        await run(provider_for(fake, refusal_fallback=False))
        assert fake.requests[0].url.path == "/v1/messages" and fake.beta_header() == ""
        assert FALLBACK_BETA not in fake.beta_header()
        assert "fallbacks" not in fake.body() and fake.body()["thinking"] == {"type": "adaptive"}

    async def test_thinking_off_and_custom_model_effort(self, provider_for):
        fake = FakeAnthropic([message_stream([("text", "x")])])
        await run(provider_for(fake, thinking=False, model="claude-sonnet-5-5", effort="high", max_tokens=2000))
        body = fake.body()
        assert "thinking" not in body
        assert (body["model"], body["output_config"], body["max_tokens"]) == (
            "claude-sonnet-5-5",
            {"effort": "high"},
            2000,
        )

    async def test_catalog_tools_are_accepted_as_request_tools(self, provider_for, settings):
        hub = McpHub(build_server(settings))
        try:
            tools, _ = anthropic_tools(await hub.list_tools())
        finally:
            await hub.stop()
        fake = FakeAnthropic([message_stream([("text", "x")])])
        async for _ in provider_for(fake).stream_turn(system=SYSTEM, tools=tools, messages=MESSAGES):
            pass
        sent = fake.body()["tools"]
        assert [t["name"] for t in sent] == sorted(t["name"] for t in sent) and len(sent) == 25
        assert all(set(t) == {"name", "description", "input_schema", "eager_input_streaming"} for t in sent)


class TestStreamParsing:
    async def test_text_turn(self, provider_for):
        fake = FakeAnthropic([message_stream([("text", "Merhaba, nasıl yardımcı olabilirim?")], usage=(321, 17))])
        events, turn = await run(provider_for(fake))
        assert (
            "".join(e.data["text"] for e in events if e.type == "text_delta") == "Merhaba, nasıl yardımcı olabilirim?"
        )
        assert turn.text == "Merhaba, nasıl yardımcı olabilirim?" and turn.stop_reason == "end_turn"
        assert turn.usage == {
            "input_tokens": 321,
            "output_tokens": 17,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 0,
        }
        assert turn.model == "claude-opus-5-5" and turn.tool_uses == [] and turn.refusal is None

    async def test_thinking_then_tool_use_with_streamed_json(self, provider_for):
        fake = FakeAnthropic(
            [
                message_stream(
                    [
                        ("thinking", "", "sig-abc"),
                        ("text", "Bakıyorum."),
                        ("tool_use", "toolu_1", "fs_list", {"path": "notlar/çay", "recursive": True}),
                    ],
                    stop_reason="tool_use",
                )
            ]
        )
        events, turn = await run(provider_for(fake))
        statuses = [e.data for e in events if e.type == "status"]
        assert {"state": "thinking"} in statuses and {"state": "preparing_tool", "name": "fs_list"} in statuses
        assert turn.stop_reason == "tool_use"
        assert [(t.id, t.name, t.input) for t in turn.tool_uses] == [
            ("toolu_1", "fs_list", {"path": "notlar/çay", "recursive": True})
        ]
        assert [b.type for b in turn.content] == ["thinking", "text", "tool_use"]

    async def test_multiple_parallel_tool_calls(self, provider_for):
        fake = FakeAnthropic(
            [
                message_stream(
                    [("tool_use", "t1", "fs_list", {"path": "a"}), ("tool_use", "t2", "fs_list", {"path": "b"})],
                    stop_reason="tool_use",
                )
            ]
        )
        _, turn = await run(provider_for(fake))
        assert [t.id for t in turn.tool_uses] == ["t1", "t2"]

    async def test_refusal_details(self, provider_for):
        fake = FakeAnthropic(
            [
                message_stream(
                    [("text", "I can't")],
                    stop_reason="refusal",
                    stop_details={"type": "refusal", "category": "cyber", "explanation": "nope"},
                )
            ]
        )
        _, turn = await run(provider_for(fake))
        assert turn.stop_reason == "refusal" and turn.refusal == {"category": "cyber", "explanation": "nope"}

    async def test_max_tokens_stop_reason_passes_through(self, provider_for):
        fake = FakeAnthropic([message_stream([("text", "uzun")], stop_reason="max_tokens")])
        assert (await run(provider_for(fake)))[1].stop_reason == "max_tokens"

    async def test_truncated_tool_json_yields_an_incomplete_input_so_stop_reason_must_guard(self, provider_for):
        """eager_input_streaming'in bilinen riski: kesik JSON istisna fırlatmaz, EKSİK bir girdi üretir
        (burada alan tamamen düşüyor). Bu yüzden ajan `max_tokens` turundaki araçları çalıştırmaz."""
        fake = FakeAnthropic(
            [message_stream([("tool_use", "t1", "fs_list", '{"path": "yarım')], stop_reason="max_tokens")]
        )
        _, turn = await run(provider_for(fake))
        assert turn.stop_reason == "max_tokens"
        assert isinstance(turn.tool_uses[0].input, dict) and turn.tool_uses[0].input.get("path") != "yarım yol"

    async def test_unparseable_tool_json_becomes_partial_json_error(self, provider_for):
        fake = FakeAnthropic(
            [message_stream([("tool_use", "t1", "fs_list", '{"path": ]]]}}')], stop_reason="tool_use")]
        )
        with pytest.raises(PartialToolJsonError):
            await run(provider_for(fake))

    async def test_fallback_block_marks_served_by_fallback(self, provider_for):
        fallback = {"type": "fallback", "from": {"model": "claude-opus-5-5"}, "to": {"model": "claude-opus-4-8"}}
        fake = FakeAnthropic([message_stream([("raw", fallback), ("text", "yedek modelden")])])
        _, turn = await run(provider_for(fake))
        assert turn.served_by_fallback is True and turn.text == "yedek modelden"


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("response", "kind", "fragment"),
        [
            (api_error(401, "authentication_error", "invalid x-api-key"), "auth", "API key was rejected"),
            (api_error(403, "permission_error", "no access"), "permission", "no access"),
            (api_error(404, "not_found_error", "model: nope"), "model", "claude-opus-5-5"),
            (api_error(429, "rate_limit_error", "slow down"), "rate_limit", "Rate limit"),
            (api_error(400, "invalid_request_error", "prompt is too long"), "bad_request", "prompt is too long"),
            (api_error(500, "api_error", "boom"), "server", "HTTP 500"),
            (api_error(529, "overloaded_error", "busy"), "server", "HTTP 529"),
            (api_error(422, "invalid_request_error", "weird"), "api", "weird"),
        ],
    )
    async def test_http_errors(self, provider_for, response, kind, fragment):
        provider = provider_for(FakeAnthropic([response]))
        with pytest.raises(ProviderError) as info:
            await run(provider)
        assert info.value.kind == kind and fragment in info.value.message

    async def test_connection_error(self, provider_for):
        provider = provider_for(FakeAnthropic([httpx2.ConnectError("no route")]))
        with pytest.raises(ProviderError) as info:
            await run(provider)
        assert info.value.kind == "connection" and "network" in info.value.message

    async def test_bad_request_mentioning_fallbacks_hints_at_the_switch(self, provider_for):
        provider = provider_for(
            FakeAnthropic([api_error(400, "invalid_request_error", "fallbacks: not supported for this model")])
        )
        with pytest.raises(ProviderError) as info:
            await run(provider)
        assert "LOCALASISTAN_REFUSAL_FALLBACK=0" in info.value.message

    async def test_error_body_secrets_are_not_echoed_beyond_message(self, provider_for):
        provider = provider_for(
            FakeAnthropic([api_error(401, "authentication_error", "invalid x-api-key sk-ant-REAL")])
        )
        with pytest.raises(ProviderError) as info:
            await run(provider)
        assert "sk-ant-REAL" not in info.value.message


class TestConfiguration:
    def test_is_configured(self, settings, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
        assert AnthropicProvider(settings).is_configured is False
        assert AnthropicProvider(settings.with_overrides(api_key="sk-x")).is_configured is True
        monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
        assert AnthropicProvider(settings).is_configured is True
        assert AnthropicProvider(settings, client=FakeAnthropic([]).client()).is_configured is True


class TestHistoryEcho:
    """Düşünme blokları ve araç çağrıları, bir sonraki istekte AYNEN geri gönderilmeli (Opus 5.5 gereği)."""

    async def test_full_tool_round_trip_through_the_agent_with_the_real_sdk(self, settings, workspace, provider_for):
        (workspace / "not.txt").write_text("içerik", encoding="utf-8")
        fake = FakeAnthropic(
            [
                message_stream(
                    [
                        ("thinking", "plan yapıyorum", "SIG-123"),
                        ("text", "Okuyorum."),
                        ("tool_use", "toolu_A", "fs_read", {"path": "not.txt"}),
                    ],
                    stop_reason="tool_use",
                ),
                message_stream([("text", "Dosyada: içerik")]),
            ]
        )
        hub = McpHub(build_server(settings))
        try:
            session = ChatManager(hub, provider_for(fake), settings).create()
            events = await drive(session, "not.txt ne diyor?")
        finally:
            await hub.stop()

        assert types_of(events)[-1] == "turn_end" and events[-1].data["stop_reason"] == "end_turn"
        assert next(e.data["text"] for e in events if e.type == "tool_result").count("içerik") == 1
        # 2. istek: [user, assistant(thinking+text+tool_use), user(tool_result)]
        second = fake.body(1)["messages"]
        assert [m["role"] for m in second] == ["user", "assistant", "user"]
        assistant_blocks = second[1]["content"]
        assert [b["type"] for b in assistant_blocks] == ["thinking", "text", "tool_use"]
        thinking = assistant_blocks[0]
        assert thinking["thinking"] == "plan yapıyorum" and thinking["signature"] == "SIG-123"  # değiştirilmeden
        assert assistant_blocks[2] == {
            "type": "tool_use",
            "id": "toolu_A",
            "name": "fs_read",
            "input": {"path": "not.txt"},
        }
        tool_result = second[2]["content"][0]
        assert tool_result["type"] == "tool_result" and tool_result["tool_use_id"] == "toolu_A"
        assert tool_result["is_error"] is False and "içerik" in tool_result["content"]
        assert_valid_history(session.messages)
        # yeniden yansıtılan bloklarda SDK'nın eklediği boş/None alan sızıntısı olmamalı
        assert all(v is not None for block in assistant_blocks for v in block.values())

    async def test_error_results_and_denials_serialise_with_is_error(self, settings, workspace, provider_for):
        (workspace / "k.txt").write_text("x")
        fake = FakeAnthropic(
            [
                message_stream([("tool_use", "toolu_D", "fs_delete", {"path": "k.txt"})], stop_reason="tool_use"),
                message_stream([("text", "silmedim")]),
            ]
        )
        hub = McpHub(build_server(settings))
        try:
            session = ChatManager(hub, provider_for(fake), settings).create()
            await drive(session, "sil", approve=lambda e: (False, ""))
        finally:
            await hub.stop()
        result = fake.body(1)["messages"][-1]["content"][0]
        assert result["is_error"] is True and result["tool_use_id"] == "toolu_D" and (workspace / "k.txt").exists()

    def test_echo_content_without_fallback_is_unchanged(self):
        content = [{"type": "thinking", "thinking": "t", "signature": "s"}, {"type": "text", "text": "x"}]
        assert echo_content(content) == content

    def test_echo_content_drops_unsafe_blocks_before_the_last_fallback(self):
        content = [
            {"type": "thinking", "thinking": "t", "signature": "s"},
            {"type": "text", "text": "kısmi"},
            {"type": "tool_use", "id": "old", "name": "x", "input": {}},
            {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
            {"type": "text", "text": "devam"},
            {"type": "tool_use", "id": "new", "name": "y", "input": {}},
        ]
        kept = echo_content(content)
        assert [b["type"] for b in kept] == ["text", "fallback", "text", "tool_use"]
        assert [t.id for t in extract_tool_uses(content)] == ["new"]  # sınırdan önceki tool_use çalıştırılmaz
