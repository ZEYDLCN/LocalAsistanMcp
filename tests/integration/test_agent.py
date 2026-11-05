from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from localasistan.client.agent import ChatManager, ChatSession, SessionBusyError
from localasistan.client.hub import McpHub
from localasistan.client.providers import ProviderError
from localasistan.errors import LocalAsistanError
from localasistan.server.app import build_server
from tests.support import ScriptedProvider, Step, assert_valid_history, drive, types_of

FLAKY = str(Path(__file__).parent.parent / "fixtures" / "flaky_server.py")


@pytest.fixture
async def hub(settings):
    hub = McpHub(build_server(settings))
    await hub.start()
    yield hub
    await hub.stop()


def make_session(hub, settings, provider, **overrides) -> ChatSession:
    cfg = settings.with_overrides(**overrides) if overrides else settings
    return ChatManager(hub, provider, cfg).create()


def results_of(session, index=-1):
    """Belirli user mesajındaki tool_result blokları."""
    return [b for b in session.messages[index]["content"] if b["type"] == "tool_result"]


class TestBasicFlow:
    async def test_plain_answer(self, hub, settings):
        provider = ScriptedProvider([Step(text="Merhaba! Nasıl yardımcı olabilirim?")])
        session = make_session(hub, settings, provider)
        events = await drive(session, "selam")
        assert types_of(events)[0] == "turn_start" and types_of(events)[-1] == "turn_end"
        assert (
            "".join(e.data["text"] for e in events if e.type == "text_delta") == "Merhaba! Nasıl yardımcı olabilirim?"
        )
        assert events[-1].data == {"stop_reason": "end_turn", "iterations": 1}
        assert next(e for e in events if e.type == "usage").data["input_tokens"] == 10
        assert [m["role"] for m in session.messages] == ["user", "assistant"]
        assert_valid_history(session.messages)
        assert session.busy is False and session.title == "selam"

    async def test_tools_and_system_prompt_sent_to_the_model(self, hub, settings):
        provider = ScriptedProvider([Step(text="ok")])
        await drive(make_session(hub, settings, provider), "x")
        call = provider.calls[0]
        names = [t["name"] for t in call["tools"]]
        assert names == sorted(names) and len(names) == 25
        assert all(t["eager_input_streaming"] is True for t in call["tools"])
        assert all("title" not in t["input_schema"] for t in call["tools"])
        calendar_create = next(t for t in call["tools"] if t["name"] == "calendar_create_event")
        assert "title" in calendar_create["input_schema"]["properties"]  # "title" ÖZELLİĞİ silinmemeli
        assert "untrusted" in call["system"] and "Notes from the connected MCP server" in call["system"]
        assert "Europe/Istanbul" in call["system"]

    async def test_system_prompt_and_tools_are_stable_across_turns_for_prompt_caching(self, hub, settings):
        provider = ScriptedProvider([Step(text="bir"), Step(text="iki")])
        session = make_session(hub, settings, provider)
        await drive(session, "a")
        await drive(session, "b")
        assert provider.calls[0]["system"] == provider.calls[1]["system"]
        assert provider.calls[0]["tools"] == provider.calls[1]["tools"]
        assert [m["role"] for m in provider.calls[1]["messages"]] == ["user", "assistant", "user"]

    async def test_read_only_tool_then_answer(self, hub, settings, workspace):
        (workspace / "notlar.txt").write_text("süt, ekmek", encoding="utf-8")
        provider = ScriptedProvider(
            [
                Step(text="Bakıyorum.", tools=[("fs_read", {"path": "notlar.txt"})]),
                Step(text="Listede süt ve ekmek var."),
            ]
        )
        session = make_session(hub, settings, provider)
        events = await drive(session, "notlarımda ne var?")
        assert types_of(events) == [
            "turn_start", "text_delta", "usage", "tool_call", "tool_result", "text_delta", "usage", "turn_end",
        ]  # fmt: skip
        result = next(e for e in events if e.type == "tool_result")
        assert not result.data["is_error"] and "süt, ekmek" in result.data["text"]
        assert next(e for e in events if e.type == "tool_call").data["kind"] == "read"
        assert [m["role"] for m in session.messages] == ["user", "assistant", "user", "assistant"]
        assert "süt, ekmek" in results_of(session, 2)[0]["content"]
        assert_valid_history(session.messages)

    async def test_transcript_is_recorded_for_ui_reload(self, hub, settings):
        provider = ScriptedProvider([Step(text="Merhaba dünya!")])
        session = make_session(hub, settings, provider)
        await drive(session, "selam")
        kinds = [e["type"] for e in session.transcript]
        assert kinds[0] == "user_message" and kinds[-1] == "turn_end"
        deltas = [e for e in session.transcript if e["type"] == "text_delta"]
        assert len(deltas) == 1 and deltas[0]["text"] == "Merhaba dünya!"  # parçalar birleştirildi

    async def test_multi_step_task_with_several_tools(self, hub, settings, workspace):
        provider = ScriptedProvider(
            [
                Step(tools=[("calendar_now", {})]),
                Step(tools=[("calendar_create_event", {"title": "Toplantı", "start": "2026-11-06T10:00"})]),
                Step(tools=[("calendar_list_events", {"start": "2026-11-06"})]),
                Step(text="Yarın 10:00'a toplantı ekledim."),
            ]
        )
        session = make_session(hub, settings, provider)
        events = await drive(session, "yarın 10'a toplantı ekle")
        assert events[-1].data["iterations"] == 4
        assert [e.data["name"] for e in events if e.type == "tool_result"] == [
            "calendar_now", "calendar_create_event", "calendar_list_events",
        ]  # fmt: skip
        assert_valid_history(session.messages)


class TestApprovals:
    async def test_additive_write_runs_without_approval_by_default(self, hub, settings, workspace):
        provider = ScriptedProvider([Step(tools=[("fs_write", {"path": "a.txt", "content": "x"})]), Step(text="oldu")])
        events = await drive(make_session(hub, settings, provider), "yaz")
        assert "approval_required" not in types_of(events)
        assert (workspace / "a.txt").read_text() == "x"

    async def test_destructive_requires_approval_and_runs_when_granted(self, hub, settings, workspace):
        (workspace / "eski.txt").write_text("bye")
        provider = ScriptedProvider([Step(tools=[("fs_delete", {"path": "eski.txt"})]), Step(text="silindi")])
        session = make_session(hub, settings, provider)
        asked: list = []
        events = await drive(session, "sil", approve=lambda e: (asked.append(e) or True, ""))
        order = types_of(events)
        assert (
            order.index("tool_call")
            < order.index("approval_required")
            < order.index("approval_resolved")
            < order.index("tool_result")
        )
        assert asked[0].data["name"] == "fs_delete" and asked[0].data["input"] == {"path": "eski.txt"}
        assert asked[0].data["kind"] == "destructive"
        assert not (workspace / "eski.txt").exists()
        assert next(e for e in events if e.type == "approval_resolved").data["approved"] is True

    async def test_denied_request_is_not_executed_and_model_is_told(self, hub, settings, workspace):
        (workspace / "keep.txt").write_text("precious")
        provider = ScriptedProvider([Step(tools=[("fs_delete", {"path": "keep.txt"})]), Step(text="Tamam, silmedim.")])
        session = make_session(hub, settings, provider)
        events = await drive(session, "sil", approve=lambda e: (False, "yanlışlıkla"))
        assert (workspace / "keep.txt").read_text() == "precious"
        result = next(e for e in events if e.type == "tool_result")
        assert result.data["is_error"] and "declined" in result.data["text"] and "yanlışlıkla" in result.data["text"]
        fed_back = provider.calls[1]["messages"][-1]["content"][0]
        assert fed_back["is_error"] is True and "Do not retry" in fed_back["content"]
        assert_valid_history(session.messages)

    async def test_approval_timeout_counts_as_denial(self, hub, settings, workspace):
        (workspace / "x.txt").write_text("1")
        provider = ScriptedProvider([Step(tools=[("fs_delete", {"path": "x.txt"})]), Step(text="vazgeçtim")])
        session = make_session(hub, settings, provider, approval_timeout_seconds=0.2)
        events = await drive(session, "sil")  # kimse yanıtlamıyor
        resolved = next(e for e in events if e.type == "approval_resolved")
        assert resolved.data["approved"] is False and "time limit" in resolved.data["reason"]
        assert (workspace / "x.txt").exists() and session.pending_approvals() == []

    async def test_policy_ask_writes_also_gates_additive_tools(self, hub, settings, workspace):
        provider = ScriptedProvider([Step(tools=[("fs_write", {"path": "n.txt", "content": "1"})]), Step(text="tmm")])
        session = make_session(hub, settings, provider, approval="ask_writes")
        events = await drive(session, "yaz", approve=lambda e: (False, ""))
        assert "approval_required" in types_of(events) and not (workspace / "n.txt").exists()

    async def test_policy_ask_writes_never_gates_read_only_tools(self, hub, settings):
        provider = ScriptedProvider([Step(tools=[("fs_list", {})]), Step(text="tmm")])
        session = make_session(hub, settings, provider, approval="ask_writes")
        assert "approval_required" not in types_of(await drive(session, "listele"))

    async def test_policy_auto_skips_all_prompts(self, hub, settings, workspace):
        (workspace / "x.txt").write_text("1")
        provider = ScriptedProvider([Step(tools=[("fs_delete", {"path": "x.txt"})]), Step(text="tmm")])
        session = make_session(hub, settings, provider, approval="auto")
        assert "approval_required" not in types_of(await drive(session, "sil"))
        assert not (workspace / "x.txt").exists()

    async def test_policy_can_change_at_runtime(self, hub, settings, workspace):
        provider = ScriptedProvider(
            [Step(tools=[("fs_write", {"path": "a.txt", "content": "1"})]), Step(text="1"),
             Step(tools=[("fs_write", {"path": "b.txt", "content": "1"})]), Step(text="2")]
        )  # fmt: skip
        manager = ChatManager(hub, provider, settings)
        session = manager.create()
        assert "approval_required" not in types_of(await drive(session, "bir"))
        manager.policy = "ask_writes"
        assert "approval_required" in types_of(await drive(session, "iki", approve=lambda e: (True, "")))

    async def test_mixed_decisions_within_one_assistant_turn(self, hub, settings, workspace):
        (workspace / "a.txt").write_text("a")
        (workspace / "b.txt").write_text("b")
        provider = ScriptedProvider(
            [Step(tools=[("fs_delete", {"path": "a.txt"}), ("fs_read", {"path": "b.txt"}),
                         ("fs_delete", {"path": "b.txt"})]),
             Step(text="bitti")]
        )  # fmt: skip
        decisions = iter([True, False])
        session = make_session(hub, settings, provider)
        await drive(session, "temizle", approve=lambda e: (next(decisions), ""))
        assert not (workspace / "a.txt").exists() and (workspace / "b.txt").exists()
        results = results_of(session, 2)
        assert [r["is_error"] for r in results] == [False, False, True]
        assert_valid_history(session.messages)

    async def test_unannotated_foreign_tools_default_to_asking(self, settings, tmp_path):
        hub = McpHub(StdioServerParameters(command=sys.executable, args=[FLAKY]))
        try:
            provider = ScriptedProvider([Step(tools=[("unannotated", {})]), Step(text="ok")])
            session = make_session(hub, settings, provider)
            events = await drive(session, "çalıştır", approve=lambda e: (True, ""))
            assert "approval_required" in types_of(events)
        finally:
            await hub.stop()


class TestToolInputHandling:
    async def test_unknown_tool_does_not_crash(self, hub, settings):
        provider = ScriptedProvider([Step(tools=[("rm_rf", {"path": "/"})]), Step(text="hata aldım")])
        session = make_session(hub, settings, provider)
        events = await drive(session, "x")
        err = next(e for e in events if e.type == "tool_result")
        assert err.data["is_error"] and "Unknown tool 'rm_rf'" in err.data["text"]
        assert_valid_history(session.messages)

    @pytest.mark.parametrize(
        ("arguments", "fragment"),
        [
            ({}, "path"),
            ({"path": 5}, "path"),
            ({"path": "a.txt", "max_chars": "many"}, "max_chars"),
            ("not an object", "expected a JSON object"),
            (None, "expected a JSON object"),
            (["a"], "expected a JSON object"),
        ],
    )
    async def test_schema_violations_never_reach_the_server(self, hub, settings, arguments, fragment):
        provider = ScriptedProvider([Step(tools=[("fs_read", arguments)]), Step(text="düzelttim")])
        session = make_session(hub, settings, provider)
        events = await drive(session, "oku")
        result = next(e for e in events if e.type == "tool_result")
        assert result.data["is_error"] and "Invalid arguments for fs_read" in result.data["text"]
        assert fragment in result.data["text"] and result.data["duration_ms"] == 0
        assert_valid_history(session.messages)

    async def test_server_side_error_is_fed_back_to_the_model(self, hub, settings):
        provider = ScriptedProvider([Step(tools=[("fs_read", {"path": "../../etc/passwd"})]), Step(text="erişemem")])
        session = make_session(hub, settings, provider)
        events = await drive(session, "x")
        assert next(e for e in events if e.type == "tool_result").data["is_error"] is True
        assert "outside the workspace" in provider.calls[1]["messages"][-1]["content"][0]["content"]

    async def test_large_tool_output_is_truncated_for_the_model_not_ui_events_beyond_cap(
        self, hub, settings, workspace
    ):
        (workspace / "big.txt").write_text("a" * 50_000)
        provider = ScriptedProvider(
            [Step(tools=[("fs_read", {"path": "big.txt", "max_chars": 50_000})]), Step(text="ok")]
        )
        session = make_session(hub, settings, provider, max_tool_output_chars=1000)
        events = await drive(session, "oku")
        fed = provider.calls[1]["messages"][-1]["content"][0]["content"]
        assert len(fed) < 1200 and "output truncated" in fed
        ui = next(e for e in events if e.type == "tool_result").data
        assert ui["truncated"] is True and len(ui["text"]) <= 6001


class TestLimitsAndStopReasons:
    async def test_iteration_limit_stops_runaway_loops_with_valid_history(self, hub, settings):
        provider = ScriptedProvider([Step(tools=[("calendar_now", {})]) for _ in range(10)])
        session = make_session(hub, settings, provider, max_iterations=3)
        events = await drive(session, "döngü")
        assert events[-1].data == {"stop_reason": "max_iterations", "iterations": 4}
        assert any(e.type == "notice" and "Stopped after 3" in e.data["message"] for e in events)
        assert len(provider.calls) == 3
        assert_valid_history(session.messages)

    async def test_max_tokens_with_tool_use_does_not_run_the_tool(self, hub, settings, workspace):
        provider = ScriptedProvider(
            [
                Step(tools=[("fs_write", {"path": "yarım.txt", "content": "kesilmiş"})], stop_reason="max_tokens"),
                Step(tools=[("fs_write", {"path": "tam.txt", "content": "kısa"})]),
                Step(text="küçük yazdım"),
            ]
        )
        session = make_session(hub, settings, provider)
        events = await drive(session, "büyük dosya yaz")
        assert not (workspace / "yarım.txt").exists() and (workspace / "tam.txt").read_text() == "kısa"
        assert "NOT run" in provider.calls[1]["messages"][-1]["content"][0]["content"]
        assert any(e.type == "notice" and "cut off" in e.data["message"] for e in events)
        assert_valid_history(session.messages)

    async def test_max_tokens_text_only_is_reported(self, hub, settings):
        provider = ScriptedProvider([Step(text="uzun yanıt...", stop_reason="max_tokens")])
        events = await drive(make_session(hub, settings, provider), "yaz")
        assert events[-1].data["stop_reason"] == "max_tokens"
        assert any(e.type == "notice" for e in events)

    async def test_refusal_discards_partial_and_rolls_back_the_question(self, hub, settings, workspace):
        provider = ScriptedProvider(
            [Step(text="kısmi", tools=[("fs_write", {"path": "z.txt", "content": "x"})], stop_reason="refusal",
                  refusal={"category": "cyber", "explanation": "policy"})]
        )  # fmt: skip
        session = make_session(hub, settings, provider)
        events = await drive(session, "kötü şey")
        assert events[-1].data["stop_reason"] == "refusal"
        assert any(e.type == "notice" and "declined" in e.data["message"] for e in events)
        assert not (workspace / "z.txt").exists()  # araç çalıştırılmadı
        assert session.messages == []  # ne kullanıcı mesajı ne kısmi yanıt kaldı

    async def test_pause_turn_continues(self, hub, settings):
        provider = ScriptedProvider([Step(text="bir ", stop_reason="pause_turn"), Step(text="iki")])
        session = make_session(hub, settings, provider)
        events = await drive(session, "x")
        assert events[-1].data["iterations"] == 2 and [m["role"] for m in session.messages] == [
            "user",
            "assistant",
            "assistant",
        ]

    async def test_context_window_exceeded(self, hub, settings):
        provider = ScriptedProvider([Step(text="", stop_reason="model_context_window_exceeded")])
        events = await drive(make_session(hub, settings, provider), "x")
        assert any(e.type == "error" and e.data["kind"] == "context" for e in events)

    async def test_fallback_notice(self, hub, settings):
        provider = ScriptedProvider([Step(text="ok", served_by_fallback=True)])
        events = await drive(make_session(hub, settings, provider), "x")
        assert any(e.type == "notice" and "fallback" in e.data["message"] for e in events)


class TestErrorHandling:
    async def test_provider_error_before_any_progress_rolls_back_user_message(self, hub, settings):
        provider = ScriptedProvider(
            [Step(error=ProviderError("rate_limit", "Rate limit reached.")), Step(text="geldi")]
        )
        session = make_session(hub, settings, provider)
        first = await drive(session, "soru")
        assert [e.type for e in first][-2:] == ["error", "turn_end"] and first[-2].data["kind"] == "rate_limit"
        assert session.messages == []
        second = await drive(session, "soru")  # aynı soru yeniden gönderilebilir, çift mesaj yok
        assert second[-1].data["stop_reason"] == "end_turn"
        assert [m["role"] for m in session.messages] == ["user", "assistant"]

    async def test_provider_error_after_tool_results_keeps_progress(self, hub, settings, workspace):
        provider = ScriptedProvider(
            [Step(tools=[("fs_write", {"path": "a.txt", "content": "1"})]), Step(error=ProviderError("server", "down")),
             Step(text="devam")]
        )  # fmt: skip
        session = make_session(hub, settings, provider)
        events = await drive(session, "yaz")
        assert events[-2].type == "error" and (workspace / "a.txt").exists()
        assert_valid_history(session.messages)
        await drive(session, "devam et")
        assert_valid_history(session.messages)

    async def test_unreadable_tool_json_is_retried_then_succeeds(self, hub, settings):
        provider = ScriptedProvider([Step(partial_json=True), Step(partial_json=True), Step(text="üçüncüde oldu")])
        events = await drive(make_session(hub, settings, provider), "x")
        assert events[-1].data["stop_reason"] == "end_turn"
        assert sum(e.type == "notice" and "Retrying" in e.data["message"] for e in events) == 2

    async def test_unreadable_tool_json_gives_up_after_retries(self, hub, settings):
        provider = ScriptedProvider([Step(partial_json=True) for _ in range(5)])
        session = make_session(hub, settings, provider)
        events = await drive(session, "x")
        assert any(e.type == "error" and e.data["kind"] == "tool_json" for e in events)
        assert len(provider.calls) == 3 and session.messages == []

    async def test_missing_api_key_is_reported_without_touching_history(self, hub, settings):
        provider = ScriptedProvider([], configured=False)
        session = make_session(hub, settings, provider)
        events = await drive(session, "merhaba")
        assert [e.type for e in events] == ["turn_start", "error", "turn_end"]
        assert events[1].data["kind"] == "no_api_key" and "ANTHROPIC_API_KEY" in events[1].data["message"]
        assert session.messages == [] and provider.calls == []

    async def test_mcp_server_unavailable_is_reported(self, settings):
        dead = McpHub(StdioServerParameters(command="/no/such/binary", args=[]), start_timeout=3)
        provider = ScriptedProvider([Step(text="x")])
        session = make_session(dead, settings, provider)
        events = await drive(session, "merhaba")
        assert any(e.type == "error" and e.data["kind"] == "mcp" for e in events)
        assert session.messages == [] and provider.calls == []

    async def test_unexpected_exception_becomes_error_event_not_a_crash(self, hub, settings):
        def boom(messages):
            raise RuntimeError("provider bug")

        session = make_session(hub, settings, ScriptedProvider([boom]))
        events = await drive(session, "x")
        assert [e.type for e in events][-2:] == ["error", "turn_end"] and events[-2].data["kind"] == "internal"
        assert "provider bug" not in events[-2].data["message"]
        assert session.busy is False and session.messages == []

    async def test_empty_message_and_busy_session(self, hub, settings):
        provider = ScriptedProvider([Step(text="yavaş", delay=0.3)])
        session = make_session(hub, settings, provider)
        with pytest.raises(LocalAsistanError, match="must not be empty"):
            await drive(session, "   ")
        first = asyncio.create_task(drive(session, "ilk"))
        await asyncio.sleep(0.05)
        assert session.busy
        with pytest.raises(SessionBusyError):
            await drive(session, "ikinci")
        with pytest.raises(SessionBusyError):
            session.reset()
        await first
        assert not session.busy


class TestCancellation:
    async def test_closing_the_stream_while_waiting_for_approval_repairs_history(self, hub, settings, workspace):
        (workspace / "x.txt").write_text("1")
        provider = ScriptedProvider(
            [Step(text="siliyorum", tools=[("fs_delete", {"path": "x.txt"})]), Step(text="sonra")]
        )
        session = make_session(hub, settings, provider)
        gen = session.run_turn("sil")
        async for event in gen:
            if event.type == "approval_required":
                break
        assert session.pending_approvals()
        await gen.aclose()  # tarayıcı bağlantısı koptu
        assert (workspace / "x.txt").exists()  # çalıştırılmadı
        assert session.busy is False and session.pending_approvals() == []
        assert_valid_history(session.messages)
        assert "Cancelled" in results_of(session)[0]["content"]
        # oturum sağlam: yeni tur sorunsuz
        provider.steps.append(Step(text="devam"))
        assert (await drive(session, "yeni"))[-1].data["stop_reason"] == "end_turn"
        assert_valid_history(session.messages)

    async def test_task_cancellation_during_a_slow_tool_leaves_valid_history(self, settings):
        hub = McpHub(StdioServerParameters(command=sys.executable, args=[FLAKY]))
        try:
            provider = ScriptedProvider([Step(tools=[("slow", {"seconds": 5})]), Step(text="sonra")])
            session = make_session(hub, settings, provider)
            task = asyncio.create_task(drive(session, "yavaş iş"))
            await asyncio.sleep(1.5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert session.busy is False
            assert_valid_history(session.messages)
            assert "Cancelled" in results_of(session)[0]["content"]
            provider.steps.append(Step(text="tamam"))
            assert (await drive(session, "yeniden"))[-1].data["stop_reason"] == "end_turn"
        finally:
            await hub.stop()

    async def test_cancellation_during_model_streaming_drops_the_question(self, hub, settings):
        provider = ScriptedProvider([Step(text="yavaş", delay=5)])
        session = make_session(hub, settings, provider)
        task = asyncio.create_task(drive(session, "soru"))
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.messages == [] and session.busy is False

    async def test_pending_approval_futures_do_not_leak(self, hub, settings, workspace):
        (workspace / "x.txt").write_text("1")
        provider = ScriptedProvider([Step(tools=[("fs_delete", {"path": "x.txt"})])])
        session = make_session(hub, settings, provider)
        gen = session.run_turn("sil")
        async for event in gen:
            if event.type == "approval_required":
                approval_id = event.data["approval_id"]
                break
        await gen.aclose()
        assert session.resolve_approval(approval_id, True) is False  # artık geçersiz


class TestManager:
    async def test_create_get_delete_and_lru(self, hub, settings):
        manager = ChatManager(hub, ScriptedProvider([]), settings, max_sessions=2)
        a, b = manager.create(), manager.create()
        assert manager.get(a.id) is a and manager.get("nope") is None
        c = manager.create()  # a en son kullanılmıştı; b atılır
        assert manager.get(b.id) is None and manager.get(a.id) is a and manager.get(c.id) is c
        assert manager.get_or_create(a.id) is a and manager.get_or_create("zzz") is not a
        assert manager.delete(a.id) is True and manager.delete(a.id) is False

    async def test_busy_sessions_are_not_evicted_or_deleted(self, hub, settings):
        provider = ScriptedProvider([Step(text="x", delay=0.3)])
        manager = ChatManager(hub, provider, settings, max_sessions=1)
        busy = manager.create()
        task = asyncio.create_task(drive(busy, "uzun"))
        await asyncio.sleep(0.05)
        other = manager.create()
        assert manager.get(busy.id) is busy and manager.get(other.id) is other  # sınır esnetildi
        assert manager.delete(busy.id) is False
        await task

    async def test_summaries_and_reset(self, hub, settings):
        manager = ChatManager(hub, ScriptedProvider([Step(text="a"), Step(text="b")]), settings)
        s1, s2 = manager.create(), manager.create()
        await drive(s1, "birinci sohbet")
        await drive(s2, "ikinci sohbet")
        assert [s["title"] for s in manager.summaries()] == ["ikinci sohbet", "birinci sohbet"]
        s1.reset()
        assert s1.messages == [] and s1.transcript == [] and s1.title == ""


class TestManagerShutdown:
    async def test_aclose_denies_pending_approvals_so_shutdown_never_hangs(self, hub, settings, workspace):
        (workspace / "x.txt").write_text("1")
        provider = ScriptedProvider([Step(tools=[("fs_delete", {"path": "x.txt"})]), Step(text="bitti")])
        manager = ChatManager(hub, provider, settings)
        session = manager.create()
        events = []

        async def consume():
            async for event in session.run_turn("sil"):
                events.append(event)

        task = asyncio.create_task(consume())
        for _ in range(100):
            if session.pending_approvals():
                break
            await asyncio.sleep(0.02)
        assert session.pending_approvals()
        await manager.aclose()  # kapanış: bekleyen onay "reddedildi" sayılır
        await asyncio.wait_for(task, 5)
        resolved = next(e for e in events if e.type == "approval_resolved")
        assert resolved.data["approved"] is False and "Shutting down" in resolved.data["reason"]
        assert (workspace / "x.txt").exists() and not session.busy
