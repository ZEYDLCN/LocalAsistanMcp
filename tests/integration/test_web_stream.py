"""Gerçek uvicorn sunucusu + gerçek akış: SSE, onay uçları, bağlantı kopması, eşzamanlılık."""

from __future__ import annotations

import asyncio

from tests.support import LiveServer, ScriptedProvider, SSEReader, Step, http_client, read_sse

JSON = {"content-type": "application/json"}


def http(server: LiveServer):
    return http_client(server)


async def wait_until(predicate, timeout=5.0, interval=0.05):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


class TestStreaming:
    async def test_event_stream_end_to_end(self, live, workspace):
        (workspace / "notlar.txt").write_text("süt, ekmek", encoding="utf-8")
        server = live(
            ScriptedProvider(
                [Step(text="Bakıyorum.", tools=[("fs_read", {"path": "notlar.txt"})]), Step(text="Süt ve ekmek var.")]
            )
        )
        async with (
            http(server) as client,
            client.stream("POST", "/api/chat", json={"message": "listede ne var?"}) as response,
        ):
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-store" and response.headers["x-accel-buffering"] == "no"
            assert "script-src 'self'" in response.headers["content-security-policy"]
            events = await read_sse(response)
        kinds = [e["type"] for e in events]
        assert kinds[0] == "session" and kinds[-1] == "turn_end" and "tool_call" in kinds and "tool_result" in kinds
        text = "".join(e["text"] for e in events if e["type"] == "text_delta")
        assert text == "Bakıyorum.Süt ve ekmek var."
        result = next(e for e in events if e["type"] == "tool_result")
        assert result["is_error"] is False and "süt, ekmek" in result["text"]

    async def test_multi_turn_session_and_transcript_reload(self, live):
        server = live(ScriptedProvider([Step(text="bir"), Step(text="iki")]))
        async with http(server) as client:
            async with client.stream("POST", "/api/chat", json={"message": "a"}) as r1:
                sid = (await read_sse(r1))[0]["session_id"]
            async with client.stream("POST", "/api/chat", json={"message": "b", "session_id": sid}) as r2:
                again = await read_sse(r2)
            assert again[0]["session_id"] == sid
            history = (await client.get(f"/api/chat/{sid}")).json()
        assert [e["text"] for e in history["transcript"] if e["type"] == "user_message"] == ["a", "b"]
        assert [e["text"] for e in history["transcript"] if e["type"] == "text_delta"] == ["bir", "iki"]

    async def test_unicode_and_large_streams_survive_chunking(self, live):
        text = ("Şükrü'nün çayı 🍵 ✓ " * 4000).strip()
        server = live(ScriptedProvider([Step(text=text)]))
        async with http(server) as client, client.stream("POST", "/api/chat", json={"message": "x"}) as response:
            events = await read_sse(response)
        assert "".join(e["text"] for e in events if e["type"] == "text_delta") == text

    async def test_concurrent_sessions_are_isolated(self, live):
        steps = [Step(text=f"cevap-{i}", delay=0.2) for i in range(6)]
        server = live(ScriptedProvider(steps))
        async with http(server) as client:

            async def one(i: int):
                async with client.stream("POST", "/api/chat", json={"message": f"m{i}"}) as r:
                    events = await read_sse(r)
                return events[0]["session_id"], "".join(e["text"] for e in events if e["type"] == "text_delta")

            results = await asyncio.gather(*[one(i) for i in range(6)])
        assert len({sid for sid, _ in results}) == 6
        assert sorted(text for _, text in results) == [f"cevap-{i}" for i in range(6)]

    async def test_second_message_while_busy_gets_409(self, live):
        server = live(ScriptedProvider([Step(text="yavaş", delay=1.0)]))
        async with http(server) as client:
            async with client.stream("POST", "/api/chat", json={"message": "a"}) as r1:
                reader = SSEReader(r1)
                sid = (await reader.until(lambda e: e["type"] == "session"))[0]["session_id"]
                await asyncio.sleep(0.2)
                busy = await client.post("/api/chat", json={"message": "b", "session_id": sid})
                assert busy.status_code == 409
                reset = await client.post(f"/api/chat/{sid}/reset", json={})
                assert reset.status_code == 409
                assert (await client.delete(f"/api/chat/{sid}")).status_code == 404  # meşgul oturum silinemez
                await reader.rest()

    async def test_missing_api_key_streams_a_clear_error(self, live):
        server = live(ScriptedProvider([], configured=False))
        async with http(server) as client, client.stream("POST", "/api/chat", json={"message": "x"}) as response:
            events = await read_sse(response)
        assert [e["type"] for e in events] == ["session", "turn_start", "error", "turn_end"]
        assert events[2]["kind"] == "no_api_key"


class TestApprovalsOverHttp:
    async def test_approve_flow(self, live, workspace):
        (workspace / "silinecek.txt").write_text("x")
        server = live(ScriptedProvider([Step(tools=[("fs_delete", {"path": "silinecek.txt"})]), Step(text="Sildim.")]))
        async with http(server) as client, client.stream("POST", "/api/chat", json={"message": "sil"}) as response:
            reader = SSEReader(response)
            events = await reader.until(lambda e: e["type"] == "approval_required")
            approval = events[-1]
            sid = events[0]["session_id"]
            assert (workspace / "silinecek.txt").exists()  # onay gelmeden dokunulmaz
            assert approval["name"] == "fs_delete" and approval["kind"] == "destructive"
            state = (await client.get(f"/api/chat/{sid}")).json()
            assert state["busy"] is True and state["pending_approvals"] == [approval["approval_id"]]
            ok = await client.post(f"/api/chat/{sid}/approvals/{approval['approval_id']}", json={"approve": True})
            assert ok.json() == {"ok": True}
            rest = await reader.rest()
            again = await client.post(f"/api/chat/{sid}/approvals/{approval['approval_id']}", json={"approve": True})
            assert again.status_code == 404  # tek kullanımlık
        assert not (workspace / "silinecek.txt").exists()
        assert [e["type"] for e in rest if e["type"] in ("approval_resolved", "tool_result", "turn_end")] == [
            "approval_resolved",
            "tool_result",
            "turn_end",
        ]
        assert rest[-1]["stop_reason"] == "end_turn"

    async def test_deny_flow(self, live, workspace):
        (workspace / "kalsin.txt").write_text("x")
        server = live(ScriptedProvider([Step(tools=[("fs_delete", {"path": "kalsin.txt"})]), Step(text="Tamam.")]))
        async with http(server) as client, client.stream("POST", "/api/chat", json={"message": "sil"}) as response:
            reader = SSEReader(response)
            events = await reader.until(lambda e: e["type"] == "approval_required")
            await client.post(
                f"/api/chat/{events[0]['session_id']}/approvals/{events[-1]['approval_id']}",
                json={"approve": False, "reason": "vazgeçtim"},
            )
            rest = await reader.rest()
        assert (workspace / "kalsin.txt").exists()
        resolved = next(e for e in rest if e["type"] == "approval_resolved")
        assert resolved["approved"] is False and resolved["reason"] == "vazgeçtim"

    async def test_changing_policy_at_runtime_applies_to_the_next_turn(self, live, workspace):
        server = live(
            ScriptedProvider([Step(tools=[("fs_write", {"path": "a.txt", "content": "1"})]), Step(text="ok")])
        )
        async with http(server) as client:
            await client.post("/api/settings", json={"approval": "ask_writes"})
            async with client.stream("POST", "/api/chat", json={"message": "yaz"}) as response:
                reader = SSEReader(response)
                events = await reader.until(lambda e: e["type"] == "approval_required")
                assert events[-1]["name"] == "fs_write"
                await client.post(
                    f"/api/chat/{events[0]['session_id']}/approvals/{events[-1]['approval_id']}", json={"approve": True}
                )
                await reader.rest()
        assert (workspace / "a.txt").read_text() == "1"


class TestDisconnects:
    async def test_closing_the_stream_while_waiting_for_approval_cleans_up(self, live, workspace):
        (workspace / "x.txt").write_text("1")
        provider = ScriptedProvider([Step(tools=[("fs_delete", {"path": "x.txt"})]), Step(text="sonra")])
        server = live(provider)
        async with http(server) as client:
            async with client.stream("POST", "/api/chat", json={"message": "sil"}) as response:
                events = await SSEReader(response).until(lambda e: e["type"] == "approval_required")
                sid = events[0]["session_id"]

            # akış kapandı (tarayıcı sekmesi kapandı gibi) -> sunucu turu iptal etmeli
            async def settled():
                state = (await client.get(f"/api/chat/{sid}")).json()
                return state if not state["busy"] and not state["pending_approvals"] else None

            await wait_until(settled, timeout=5)
            assert (workspace / "x.txt").exists()  # onaysız silinmedi
            # oturum kullanılabilir kalmalı
            provider.steps.append(Step(text="yeni cevap"))
            async with client.stream("POST", "/api/chat", json={"message": "tekrar", "session_id": sid}) as again:
                rest = await read_sse(again)
            assert rest[-1]["type"] == "turn_end" and rest[-1]["stop_reason"] == "end_turn"

    async def test_closing_the_stream_during_model_streaming_frees_the_session(self, live):
        server = live(ScriptedProvider([Step(text="çok yavaş", delay=30)]))
        async with http(server) as client:
            async with client.stream("POST", "/api/chat", json={"message": "a"}) as response:
                sid = (await SSEReader(response).until(lambda e: e["type"] == "session"))[0]["session_id"]
                await asyncio.sleep(0.2)

            async def free():
                return not (await client.get(f"/api/chat/{sid}")).json()["busy"]

            await wait_until(free, timeout=5)

    async def test_server_shutdown_with_a_pending_approval_does_not_hang(self, live, workspace):
        (workspace / "x.txt").write_text("1")
        server = live(ScriptedProvider([Step(tools=[("fs_delete", {"path": "x.txt"})])]))
        client = http(server)
        stream = client.stream("POST", "/api/chat", json={"message": "sil"})
        response = await stream.__aenter__()
        await SSEReader(response).until(lambda e: e["type"] == "approval_required")
        # sunucuyu onay beklerken kapat: __exit__ içindeki join(20) takılırsa test başarısız olur
        server.server.should_exit = True
        await asyncio.to_thread(server.thread.join, 15)
        assert not server.thread.is_alive()
        await stream.__aexit__(None, None, None)
        await client.aclose()
