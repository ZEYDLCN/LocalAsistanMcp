from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import StdioServerParameters

from localasistan.client.catalog import ToolSpec
from localasistan.client.hub import HubError, McpHub
from localasistan.errors import LocalAsistanError
from localasistan.server.app import build_server

FLAKY = str(Path(__file__).parent.parent / "fixtures" / "flaky_server.py")


def flaky_params(**env: str) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=[FLAKY], env=env or None)


def pids(pattern: str) -> set[int]:
    out = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True, check=False).stdout
    return {int(p) for p in out.split()} - {os.getpid()}


@pytest.fixture
def hub_factory(settings):
    created: list[McpHub] = []

    def make(target=None, **kwargs) -> McpHub:
        hub = McpHub(target if target is not None else build_server(settings), **kwargs)
        created.append(hub)
        return hub

    yield make


async def stop_all(*hubs: McpHub) -> None:
    for hub in hubs:
        await hub.stop()


class TestInProcess:
    async def test_lifecycle_and_status(self, hub_factory):
        hub = hub_factory()
        assert hub.connected is False and hub.status()["server"] is None
        await hub.start()
        await hub.start()  # idempotent
        status = hub.status()
        assert hub.connected and status["server"]["name"] == "localasistan"
        assert status["server"]["capabilities"] == {"tools": True, "resources": True, "prompts": True}
        assert "untrusted" in hub.instructions
        await hub.stop()
        await hub.stop()  # idempotent
        assert hub.connected is False

    async def test_tool_catalog(self, hub_factory):
        hub = hub_factory()
        try:
            specs = await hub.list_tools()
            assert len(specs) == 25 and all(isinstance(s, ToolSpec) for s in specs)
            by = {s.name: s for s in specs}
            assert (
                by["fs_read"].kind == "read"
                and by["fs_write"].kind == "write"
                and by["fs_delete"].kind == "destructive"
            )
            assert by["calendar_create_event"].input_schema["required"] == ["title", "start"]
            assert await hub.list_tools() == specs  # önbellekten
            assert len(await hub.list_tools(refresh=True)) == 25
        finally:
            await hub.stop()

    async def test_lazy_start_on_first_use(self, hub_factory):
        hub = hub_factory()
        try:
            outcome = await hub.call_tool("calendar_now")
            assert not outcome.is_error and outcome.data["timezone"] == "Europe/Istanbul" and hub.connected
            assert outcome.duration_ms >= 0
        finally:
            await hub.stop()

    async def test_outcomes(self, hub_factory):
        hub = hub_factory()
        try:
            ok = await hub.call_tool("fs_write", {"path": "a.txt", "content": "merhaba"})
            assert not ok.is_error and ok.data["bytes_written"] == 7 and json.loads(ok.text) == ok.data
            domain = await hub.call_tool("fs_read", {"path": "../x"})
            assert domain.is_error and "outside the workspace" in domain.text and domain.data is None
            invalid = await hub.call_tool("fs_list", {"max_entries": "x"})
            assert invalid.is_error and "max_entries" in invalid.text
            unknown = await hub.call_tool("nope")
            assert unknown.is_error and "nope" in unknown.text
        finally:
            await hub.stop()

    async def test_parallel_calls(self, hub_factory):
        hub = hub_factory()
        try:
            outcomes = await asyncio.gather(*[hub.call_tool("calendar_now") for _ in range(25)])
            assert all(not o.is_error for o in outcomes)
        finally:
            await hub.stop()

    async def test_resources_and_prompts(self, hub_factory):
        hub = hub_factory()
        try:
            res = await hub.list_resources()
            assert {r["uri"] for r in res["resources"]} == {
                "calendar://today", "calendar://week", "db://schema", "workspace://tree",
            }  # fmt: skip
            assert res["templates"][0]["uri_template"] == "workspace://file/{+path}"
            tree = await hub.read_resource("workspace://tree")
            assert tree["contents"][0]["text"] == "(workspace is empty)"
            with pytest.raises(LocalAsistanError):
                await hub.read_resource("workspace://file/missing.txt")
            prompts = {p["name"]: p for p in await hub.list_prompts()}
            assert prompts["summarize_file"]["arguments"] == [{"name": "path", "description": None, "required": True}]
            got = await hub.get_prompt("summarize_file", {"path": "x.md"})
            assert got["messages"][0]["role"] == "user" and "x.md" in got["messages"][0]["text"]
            with pytest.raises(LocalAsistanError):
                await hub.get_prompt("nope")
        finally:
            await hub.stop()

    async def test_restart_keeps_state_and_counts(self, hub_factory):
        hub = hub_factory()
        try:
            await hub.call_tool("fs_write", {"path": "keep.txt", "content": "1"})
            await hub.restart()
            assert hub.restarts == 1 and hub.connected
            assert (await hub.call_tool("fs_read", {"path": "keep.txt"})).data["content"] == "1"
        finally:
            await hub.stop()


@pytest.mark.stdio
class TestFailureModes:
    async def test_command_not_found(self, hub_factory):
        hub = hub_factory(StdioServerParameters(command="/definitely/not/here", args=[]))
        with pytest.raises(HubError, match="Could not start the MCP server"):
            await hub.start()
        assert hub.connected is False and hub.last_error
        outcome = await hub.call_tool("anything")
        assert outcome.is_error and "unavailable" in outcome.text

    async def test_server_exits_during_startup(self, hub_factory):
        hub = hub_factory(flaky_params(FLAKY_EXIT_AT_START="1"))
        with pytest.raises(HubError, match="Could not start the MCP server"):
            await hub.start()
        assert hub.connected is False

    async def test_invalid_server_configuration_is_reported(self, hub_factory, settings):
        env = {**settings.server_env(), "LOCALASISTAN_TZ": "Nowhere/Land"}
        hub = hub_factory(StdioServerParameters(command=sys.executable, args=["-m", "localasistan.server"], env=env))
        with pytest.raises(HubError):
            await hub.start()

    async def test_slow_start_hits_start_timeout_and_leaves_no_process(self, hub_factory):
        before = pids("flaky_server.py")
        hub = hub_factory(flaky_params(FLAKY_START_DELAY="5"), start_timeout=0.8)
        with pytest.raises(HubError, match="did not start within"):
            await hub.start()
        await asyncio.sleep(0.5)
        assert pids("flaky_server.py") - before == set()

    async def test_tool_timeout_keeps_connection_usable(self, hub_factory):
        hub = hub_factory(flaky_params(), tool_timeout=0.5)
        try:
            slow = await hub.call_tool("slow", {"seconds": 3})
            assert slow.is_error and "timed out after 0.5s" in slow.text and "may still be running" in slow.text
            assert hub.connected
            assert (await hub.call_tool("echo", {"text": "hâlâ burada"})).text == "hâlâ burada"
        finally:
            await hub.stop()

    async def test_per_call_timeout_override(self, hub_factory):
        hub = hub_factory(flaky_params(), tool_timeout=30)
        try:
            out = await hub.call_tool("slow", {"seconds": 3}, timeout=0.4)
            assert out.is_error and "0.4s" in out.text
        finally:
            await hub.stop()

    async def test_server_killed_between_calls_read_only_recovers_transparently(self, hub_factory):
        hub = hub_factory(flaky_params())
        try:
            await hub.list_tools()
            before = pids("flaky_server.py")
            assert (await hub.call_tool("echo", {"text": "1"})).text == "1"
            victim = pids("flaky_server.py") - set()
            for pid in victim:
                os.kill(pid, 9)
            await asyncio.sleep(0.3)
            again = await hub.call_tool("echo", {"text": "2"})
            assert again.text == "2" and not again.is_error
            assert hub.restarts >= 1
            assert pids("flaky_server.py") != victim or before == set()
        finally:
            await hub.stop()

    async def test_crash_during_read_only_call_is_retried_once(self, hub_factory, tmp_path):
        hub = hub_factory(flaky_params())
        marker = tmp_path / "marker"
        try:
            await hub.list_tools()  # salt-okunur bilgisi için katalog gerekir
            out = await hub.call_tool("read_die_once", {"marker": str(marker)})
            assert out.text == "recovered" and not out.is_error and hub.restarts == 1
        finally:
            await hub.stop()

    async def test_read_only_crash_that_repeats_gives_up_with_clear_message(self, hub_factory):
        hub = hub_factory(flaky_params())
        try:
            await hub.list_tools()
            out = await hub.call_tool("read_die_always")
            assert out.is_error and "connection was lost" in out.text
            assert (await hub.call_tool("echo", {"text": "alive"})).text == "alive"
        finally:
            await hub.stop()

    async def test_crash_during_write_call_is_never_retried(self, hub_factory, tmp_path):
        hub = hub_factory(flaky_params())
        log = tmp_path / "side-effects.log"
        try:
            await hub.list_tools()
            out = await hub.call_tool("write_then_die", {"path": str(log)})
            assert out.is_error and "may or may not have completed" in out.text
            assert log.read_text() == "called\n"  # yan etki YALNIZCA bir kez gerçekleşti
            assert (await hub.call_tool("echo", {"text": "next"})).text == "next"  # sonraki çağrıda toparlanır
        finally:
            await hub.stop()

    async def test_unannotated_tool_is_treated_as_destructive(self, hub_factory):
        hub = hub_factory(flaky_params())
        try:
            spec = next(s for s in await hub.list_tools() if s.name == "unannotated")
            assert spec.read_only is False and spec.destructive is True and spec.kind == "destructive"
        finally:
            await hub.stop()

    async def test_large_and_non_text_outputs(self, hub_factory):
        hub = hub_factory(flaky_params())
        try:
            big = await hub.call_tool("big", {"n": 300_000})
            assert len(big.text) == 300_000
            img = await hub.call_tool("image")
            assert "[image content omitted]" in img.text and "caption" in img.text
        finally:
            await hub.stop()

    async def test_stop_terminates_the_subprocess(self, hub_factory):
        before = pids("flaky_server.py")
        hub = hub_factory(flaky_params())
        await hub.start()
        assert len(pids("flaky_server.py") - before) == 1
        await hub.stop()
        await asyncio.sleep(0.5)
        assert pids("flaky_server.py") - before == set()

    async def test_concurrent_calls_during_reconnect_start_only_one_server(self, hub_factory):
        hub = hub_factory(flaky_params())
        before = pids("flaky_server.py")
        try:
            outs = await asyncio.gather(*[hub.call_tool("echo", {"text": str(i)}) for i in range(12)])
            assert [o.text for o in outs] == [str(i) for i in range(12)]
            assert len(pids("flaky_server.py") - before) == 1
        finally:
            await hub.stop()
