"""OpenAICompatProvider: sahte HTTP üzerinde istek biçimi, akış ayrıştırma, hata eşleme, yeniden deneme, modeller."""

from __future__ import annotations

import httpx2
import pytest

from localasistan.client.events import Event
from localasistan.client.openai_compat import (
    OpenAICompatProvider,
    list_models,
    simplify_schema,
    to_openai_messages,
    to_openai_tools,
)
from localasistan.client.providers import AssistantTurn, PartialToolJsonError, ProviderError, create_provider
from tests.openai_fake import (
    FakeOpenAI,
    chunk,
    error_body,
    sse,
    text_stream,
    tool_stream,
    usage_chunk,
)

BASE = "https://integrate.api.nvidia.com/v1"
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


@pytest.fixture
def compat(settings):
    return settings.with_overrides(
        provider="openai_compat", base_url=BASE, model="z-ai/glm-test", api_key="nvapi-secret", max_tokens=8192
    )


def make(compat, fake: FakeOpenAI, **kwargs) -> OpenAICompatProvider:
    return OpenAICompatProvider(compat, client=fake.client(), retry_delays=kwargs.pop("retry_delays", (0.0, 0.0)))


async def run(provider, messages=MESSAGES, tools=TOOLS) -> tuple[list[Event], AssistantTurn]:
    events: list[Event] = []
    turn = None
    async for item in provider.stream_turn(system=SYSTEM, tools=tools, messages=messages):
        if isinstance(item, AssistantTurn):
            turn = item
        else:
            events.append(item)
    assert turn is not None
    return events, turn


class TestRequestShape:
    async def test_request_goes_to_chat_completions_with_bearer_key_and_streaming(self, compat):
        fake = FakeOpenAI([text_stream("selam")])
        await run(make(compat, fake))
        request = fake.requests[0]
        assert str(request.url) == f"{BASE}/chat/completions"
        assert request.headers["authorization"] == "Bearer nvapi-secret"
        assert request.headers["accept"] == "text/event-stream"
        body = fake.body()
        assert body["model"] == "z-ai/glm-test" and body["stream"] is True and body["max_tokens"] == 8192
        assert body["stream_options"] == {"include_usage": True}
        assert body["messages"] == [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "merhaba"}]

    async def test_tools_are_sent_as_openai_functions_without_anthropic_only_fields(self, compat):
        fake = FakeOpenAI([text_stream("ok")])
        await run(make(compat, fake))
        assert fake.body()["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "fs_list",
                    "description": "List",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ]
        assert "eager_input_streaming" not in str(fake.body())

    async def test_no_key_means_no_authorization_header(self, compat):
        fake = FakeOpenAI([text_stream("ok")])
        await run(make(compat.with_overrides(api_key=None), fake))
        assert "authorization" not in fake.requests[0].headers

    async def test_no_tools_key_when_the_tool_list_is_empty(self, compat):
        fake = FakeOpenAI([text_stream("ok")])
        await run(make(compat, fake), tools=[])
        assert "tools" not in fake.body()


class TestStreaming:
    async def test_text_is_streamed_in_order_and_usage_is_mapped(self, compat):
        fake = FakeOpenAI([text_stream("Merhaba dünya, nasılsın?", usage=(321, 17))])
        events, turn = await run(make(compat, fake))
        assert "".join(e.data["text"] for e in events if e.type == "text_delta") == "Merhaba dünya, nasılsın?"
        assert turn.text == "Merhaba dünya, nasılsın?"
        assert turn.stop_reason == "end_turn" and turn.tool_uses == []
        assert turn.usage == {"input_tokens": 321, "output_tokens": 17}
        assert turn.model == "z-ai/glm-test"
        assert turn.content == [{"type": "text", "text": "Merhaba dünya, nasılsın?"}]

    async def test_tool_call_arguments_split_across_chunks_are_reassembled(self, compat):
        args = '{"path": "notlar", "ş": "İğ çalışma"}'
        fake = FakeOpenAI([tool_stream([("call_abc", "fs_list", args)], size=3)])
        events, turn = await run(make(compat, fake))
        assert turn.stop_reason == "tool_use"
        assert turn.tool_uses[0].id == "call_abc" and turn.tool_uses[0].name == "fs_list"
        assert turn.tool_uses[0].input == {"path": "notlar", "ş": "İğ çalışma"}
        assert turn.content[-1] == {
            "type": "tool_use",
            "id": "call_abc",
            "name": "fs_list",
            "input": {"path": "notlar", "ş": "İğ çalışma"},
        }
        assert [e.data for e in events if e.type == "status"] == [{"state": "preparing_tool", "name": "fs_list"}]

    async def test_parallel_tool_calls_keep_their_own_arguments(self, compat):
        fake = FakeOpenAI(
            [tool_stream([("c1", "fs_list", '{"path": "a"}'), ("c2", "fs_list", '{"path": "b"}')], text="Bakıyorum.")]
        )
        _, turn = await run(make(compat, fake))
        assert [(u.id, u.input) for u in turn.tool_uses] == [("c1", {"path": "a"}), ("c2", {"path": "b"})]
        assert turn.text == "Bakıyorum."
        assert [b["type"] for b in turn.content] == ["text", "tool_use", "tool_use"]

    async def test_servers_that_repeat_the_name_in_every_chunk_do_not_duplicate_it(self, compat):
        fake = FakeOpenAI([tool_stream([("c1", "fs_list", '{"path": "xyz"}')], repeat_name=True)])
        _, turn = await run(make(compat, fake))
        assert turn.tool_uses[0].name == "fs_list"

    async def test_missing_or_duplicate_call_ids_are_replaced_with_unique_ones(self, compat):
        fake = FakeOpenAI([tool_stream([(None, "fs_list", "{}"), ("dup", "fs_list", "{}"), ("dup", "fs_list", "{}")])])
        _, turn = await run(make(compat, fake))
        ids = [u.id for u in turn.tool_uses]
        assert len(set(ids)) == 3 and all(ids) and ids[1] == "dup"

    async def test_empty_arguments_mean_an_empty_object(self, compat):
        fake = FakeOpenAI([tool_stream([("c1", "fs_list", "")])])
        _, turn = await run(make(compat, fake))
        assert turn.tool_uses[0].input == {}

    async def test_tool_calls_with_stop_finish_reason_still_count_as_tool_use(self, compat):
        fake = FakeOpenAI([tool_stream([("c1", "fs_list", "{}")], finish="stop")])
        _, turn = await run(make(compat, fake))
        assert turn.stop_reason == "tool_use"

    async def test_unreadable_arguments_raise_partial_json_error_so_the_agent_can_retry(self, compat):
        fake = FakeOpenAI([tool_stream([("c1", "fs_list", '{"path": "oops')])])
        with pytest.raises(PartialToolJsonError):
            await run(make(compat, fake))

    async def test_non_object_arguments_are_rejected_too(self, compat):
        fake = FakeOpenAI([tool_stream([("c1", "fs_list", "[1, 2]")])])
        with pytest.raises(PartialToolJsonError):
            await run(make(compat, fake))

    async def test_truncated_tool_call_is_surfaced_as_max_tokens_not_as_a_json_error(self, compat):
        fake = FakeOpenAI([tool_stream([("c1", "fs_list", '{"path": "oops')], finish="length")])
        _, turn = await run(make(compat, fake))
        assert turn.stop_reason == "max_tokens" and turn.tool_uses[0].input == {}

    async def test_content_filter_becomes_a_refusal(self, compat):
        fake = FakeOpenAI([text_stream("", finish="content_filter")])
        _, turn = await run(make(compat, fake))
        assert turn.stop_reason == "refusal" and turn.refusal["category"] == "content_filter"

    async def test_reasoning_content_only_announces_thinking_and_is_not_kept(self, compat):
        fake = FakeOpenAI(
            [sse(chunk(reasoning="düşünüyorum…"), chunk(reasoning="hâlâ"), chunk("Cevap."), chunk(finish="stop"))]
        )
        events, turn = await run(make(compat, fake))
        assert [e.data for e in events if e.type == "status"] == [{"state": "thinking"}]
        assert turn.content == [{"type": "text", "text": "Cevap."}] and "düşün" not in str(turn.content)

    async def test_comments_blank_lines_and_garbage_are_ignored_and_second_choice_too(self, compat):
        payload = b": keep-alive\n\nevent: message\ndata: {not json}\n\n" + sse(
            chunk("A"), chunk("X", index=1), chunk("B"), chunk(finish="stop"), usage_chunk(1, 2)
        )
        _, turn = await run(make(compat, FakeOpenAI([payload])))
        assert turn.text == "AB" and turn.usage == {"input_tokens": 1, "output_tokens": 2}

    async def test_stream_without_a_final_marker_is_an_error_not_a_silent_truncation(self, compat):
        fake = FakeOpenAI([sse(chunk("yarım"), done=False)])
        with pytest.raises(ProviderError) as info:
            await run(make(compat, fake))
        assert info.value.kind == "connection"

    async def test_missing_finish_reason_is_tolerated_when_done_marker_arrived(self, compat):
        fake = FakeOpenAI([sse(chunk("tamam"))])
        _, turn = await run(make(compat, fake))
        assert turn.text == "tamam" and turn.stop_reason == "end_turn"

    async def test_error_object_inside_the_stream_is_reported(self, compat):
        fake = FakeOpenAI([sse({"error": {"message": "model overloaded"}}, done=False)])
        with pytest.raises(ProviderError, match="model overloaded"):
            await run(make(compat, fake))

    async def test_models_that_send_dict_arguments_are_accepted(self, compat):
        part = chunk(
            tool_calls=[{"index": 0, "id": "c1", "function": {"name": "fs_list", "arguments": {"path": "a"}}}],
            finish="tool_calls",
        )
        _, turn = await run(make(compat, FakeOpenAI([sse(part)])))
        assert turn.tool_uses[0].input == {"path": "a"}

    async def test_a_call_without_a_name_is_dropped(self, compat):
        part = chunk(tool_calls=[{"index": 0, "id": "c1", "function": {"arguments": "{}"}}], finish="tool_calls")
        _, turn = await run(make(compat, FakeOpenAI([sse(part)])))
        assert turn.tool_uses == []


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "kind", "needle"),
        [
            (401, "auth", "LOCALASISTAN_API_KEY"),
            (403, "permission", "denied"),
            (404, "model", "localasistan models"),
            (400, "bad_request", "rejected the request"),
            (422, "bad_request", "rejected the request"),
            (500, "server", "HTTP 500"),
            (418, "api", "HTTP 418"),
        ],
    )
    async def test_http_errors_map_to_clear_kinds(self, compat, status, kind, needle):
        fake = FakeOpenAI([error_body("boom detail", status=status)])
        with pytest.raises(ProviderError) as info:
            await run(make(compat, fake))
        assert info.value.kind == kind and needle in info.value.message
        assert "nvapi-secret" not in info.value.message

    async def test_tool_related_400_hints_at_model_capability(self, compat):
        fake = FakeOpenAI([error_body("tools are not supported by this model")])
        with pytest.raises(ProviderError, match="may not support tool calling"):
            await run(make(compat, fake))

    async def test_nvidia_style_detail_bodies_are_understood(self, compat):
        fake = FakeOpenAI([(400, {"status": 400, "title": "Bad Request", "detail": "max_tokens too large"})])
        with pytest.raises(ProviderError, match="max_tokens too large"):
            await run(make(compat, fake))

    async def test_plain_text_error_bodies_are_truncated_not_dumped(self, compat):
        fake = FakeOpenAI([(400, "y" * 5000)])
        with pytest.raises(ProviderError) as info:
            await run(make(compat, fake))
        assert len(info.value.message) < 700

    async def test_network_failures_are_connection_errors(self, compat):
        fake = FakeOpenAI([httpx2.ConnectError("refused")])
        with pytest.raises(ProviderError) as info:
            await run(make(compat, fake))
        assert info.value.kind == "connection" and "refused" not in info.value.message

    async def test_timeouts_say_so(self, compat):
        fake = FakeOpenAI([httpx2.ReadTimeout("slow")])
        with pytest.raises(ProviderError, match="did not respond in time"):
            await run(make(compat, fake))

    async def test_a_dropped_connection_mid_stream_is_reported(self, compat):
        class Dropping(httpx2.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices": [{"index": 0, "delta": {"content": "yar"}}]}\n\n'
                raise httpx2.ReadError("connection reset")

        fake = FakeOpenAI([lambda request: httpx2.Response(200, stream=Dropping())])
        with pytest.raises(ProviderError) as info:
            await run(make(compat, fake))
        assert info.value.kind == "connection"


class TestRetries:
    async def test_rate_limit_is_retried_with_a_visible_notice_then_succeeds(self, compat):
        fake = FakeOpenAI([(429, {"error": {"message": "slow down"}}, {"retry-after": "0"}), text_stream("sonunda")])
        events, turn = await run(make(compat, fake))
        assert turn.text == "sonunda" and len(fake.requests) == 2
        notices = [e for e in events if e.type == "notice"]
        assert len(notices) == 1 and "Rate limit" in notices[0].data["message"]

    async def test_gateway_errors_are_retried_up_to_the_limit_then_reported(self, compat):
        fake = FakeOpenAI([(503, "down"), (502, "down"), (503, "still down")])
        with pytest.raises(ProviderError) as info:
            await run(make(compat, fake))
        assert info.value.kind == "server" and len(fake.requests) == 3  # ilk deneme + 2 yeniden deneme

    async def test_client_errors_are_never_retried(self, compat):
        fake = FakeOpenAI([error_body("bad", status=400)])
        with pytest.raises(ProviderError):
            await run(make(compat, fake))
        assert len(fake.requests) == 1

    async def test_retry_after_is_honoured_but_capped(self, compat, monkeypatch):
        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        monkeypatch.setattr("localasistan.client.openai_compat.asyncio.sleep", fake_sleep)
        fake = FakeOpenAI(
            [(429, "x", {"retry-after": "9999"}), (429, "x", {"retry-after": "2"}), (429, "x"), text_stream("ok")]
        )
        with pytest.raises(ProviderError):
            await run(make(compat, fake, retry_delays=(7.0, 9.0)))
        assert slept == [30.0, 2.0]  # üst sınır 30 sn; başlık yoksa varsayılan gecikme (kullanılmadı: hak bitti)

    async def test_servers_that_reject_stream_options_are_retried_without_it_once(self, compat):
        fake = FakeOpenAI([error_body("Unknown parameter: stream_options"), text_stream("ok")])
        provider = make(compat, fake)
        _, turn = await run(provider)
        assert turn.text == "ok"
        assert "stream_options" in fake.body(0) and "stream_options" not in fake.body(1)
        await_second = FakeOpenAI([text_stream("again")])
        provider._client = await_second.client()
        await run(provider)
        assert "stream_options" not in await_second.body(0)  # tercih hatırlanır


class TestHistoryConversion:
    def test_assistant_tool_use_and_user_tool_result_round_trip_to_openai_shape(self):
        history = [
            {"role": "user", "content": "klasörü listele"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "text", "text": "Bakıyorum."},
                    {"type": "tool_use", "id": "c1", "name": "fs_list", "input": {"path": "çalışma"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c1", "content": '{"entries": []}'},
                ],
            },
        ]
        assert to_openai_messages(SYSTEM, history) == [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": "klasörü listele"},
            {
                "role": "assistant",
                "content": "Bakıyorum.",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "fs_list", "arguments": '{"path": "çalışma"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"entries": []}'},
        ]

    def test_error_results_are_marked_and_list_content_is_flattened(self):
        history = [
            {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "x", "input": {}}]},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "c1",
                        "content": [{"type": "text", "text": "kırık"}, {"type": "text", "text": " araç"}],
                        "is_error": True,
                    },
                    {"type": "text", "text": "devam et"},
                ],
            },
        ]
        out = to_openai_messages(SYSTEM, history)
        assert out[2] == {"role": "tool", "tool_call_id": "c1", "content": "ERROR: kırık araç"}
        assert out[3] == {"role": "user", "content": "devam et"}  # ek metin, araç mesajlarından SONRA

    def test_each_parallel_result_gets_its_own_tool_message_in_order(self):
        history = [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "a", "name": "x", "input": {}},
                    {"type": "tool_use", "id": "b", "name": "y", "input": {"k": 1}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "1"},
                    {"type": "tool_result", "tool_use_id": "b", "content": "2"},
                ],
            },
        ]
        out = to_openai_messages(SYSTEM, history)
        assert [m["role"] for m in out] == ["system", "assistant", "tool", "tool"]
        assert [m.get("tool_call_id") for m in out[2:]] == ["a", "b"]
        assert out[1]["content"] == "" and len(out[1]["tool_calls"]) == 2

    def test_empty_assistant_messages_are_dropped(self):
        out = to_openai_messages(SYSTEM, [{"role": "assistant", "content": [{"type": "thinking", "thinking": "x"}]}])
        assert out == [{"role": "system", "content": SYSTEM}]

    def test_sdk_style_objects_are_understood_like_dicts(self):
        class Block:
            def __init__(self, **kw):
                self.__dict__.update(kw)

        history = [
            {"role": "assistant", "content": [Block(type="tool_use", id="c1", name="fs_list", input={"path": "a"})]},
            {"role": "user", "content": [Block(type="tool_result", tool_use_id="c1", content="ok", is_error=False)]},
        ]
        out = to_openai_messages(SYSTEM, history)
        assert out[1]["tool_calls"][0]["function"]["arguments"] == '{"path": "a"}'
        assert out[2] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}


def _nullable_pairs(node) -> list:
    """İçinde `{"type": "null"}` bulunan iki seçenekli `anyOf` düğümleri (sadeleştirmeden sonra kalmamalı)."""
    found = []
    if isinstance(node, dict):
        options = node.get("anyOf")
        if isinstance(options, list) and len(options) == 2 and any(o.get("type") == "null" for o in options):
            found.append(node)
        for value in node.values():
            found += _nullable_pairs(value)
    elif isinstance(node, list):
        for item in node:
            found += _nullable_pairs(item)
    return found


class TestSchemaSimplification:
    def test_optional_anyof_null_collapses_to_the_real_type(self):
        node = {
            "anyOf": [{"type": "string"}, {"type": "null"}],
            "default": None,
            "description": "End time.",
        }
        assert simplify_schema(node) == {"type": "string", "description": "End time."}

    def test_nullable_object_keeps_its_nested_properties_and_real_defaults(self):
        node = {
            "type": "object",
            "properties": {
                "a": {"anyOf": [{"type": "integer"}, {"type": "null"}], "default": 5},
                "title": {"type": "string"},
            },
            "required": ["title"],
        }
        out = simplify_schema(node)
        assert out["properties"]["a"] == {"type": "integer", "default": 5}
        assert out["properties"]["title"] == {"type": "string"}  # "title" özellik adı korunur

    def test_real_unions_are_left_alone(self):
        node = {"anyOf": [{"type": "string"}, {"type": "integer"}]}
        assert simplify_schema(node) == node

    def test_every_real_tool_schema_converts_without_losing_required_fields(self, settings):
        import asyncio

        from mcp import Client

        from localasistan.client.catalog import anthropic_tools, spec_from_mcp
        from localasistan.server.app import build_server

        async def go():
            async with Client(build_server(settings)) as client:
                return [spec_from_mcp(t) for t in (await client.list_tools()).tools]

        specs = asyncio.run(go())
        tools, _ = anthropic_tools(specs)
        converted = to_openai_tools(tools)
        assert len(converted) == 25
        originals = {t["name"]: t["input_schema"] for t in tools}
        for item in converted:
            fn = item["function"]
            assert fn["parameters"].get("required", []) == originals[fn["name"]].get("required", [])
            assert not _nullable_pairs(fn["parameters"]), fn["name"]  # `X | null` ikilileri sadeleşmiş olmalı


class TestConfiguration:
    def test_is_configured_needs_base_url_and_model_but_not_a_key(self, compat):
        assert OpenAICompatProvider(compat.with_overrides(api_key=None)).is_configured
        missing_model = OpenAICompatProvider(compat.with_overrides(model=""))
        assert not missing_model.is_configured and missing_model.missing_setting == "LOCALASISTAN_MODEL"
        missing_url = OpenAICompatProvider(compat.with_overrides(base_url=None))
        assert not missing_url.is_configured and missing_url.missing_setting == "LOCALASISTAN_BASE_URL"
        assert OpenAICompatProvider(compat).missing_setting is None

    def test_factory_picks_the_provider_from_settings(self, compat, settings):
        assert create_provider(compat).name == "openai_compat"
        assert create_provider(settings).name == "anthropic"


class TestListModels:
    def test_ids_are_sorted_unique_and_authorized(self, compat):
        fake = FakeOpenAI(
            [(200, {"object": "list", "data": [{"id": "z/b"}, {"id": "a/glm-5"}, {"id": "z/b"}, {"nope": 1}]})]
        )
        assert list_models(compat, fake.sync_client()) == ["a/glm-5", "z/b"]
        assert str(fake.requests[0].url) == f"{BASE}/models"
        assert fake.header("authorization") == "Bearer nvapi-secret"

    def test_a_bare_list_is_accepted(self, compat):
        assert list_models(compat, FakeOpenAI([(200, [{"id": "x"}])]).sync_client()) == ["x"]

    def test_errors_become_provider_errors(self, compat):
        with pytest.raises(ProviderError, match="API key"):
            list_models(compat, FakeOpenAI([error_body("no", status=401)]).sync_client())
        with pytest.raises(ProviderError, match="non-JSON"):
            list_models(compat, FakeOpenAI([(200, "<html>")]).sync_client())
        with pytest.raises(ProviderError) as info:
            list_models(compat, FakeOpenAI([httpx2.ConnectError("x")]).sync_client())
        assert info.value.kind == "connection"

    def test_missing_base_url_is_a_config_error(self, compat):
        with pytest.raises(ProviderError, match="LOCALASISTAN_BASE_URL"):
            list_models(compat.with_overrides(base_url=None))
