"""Gerçek `python -m localasistan.server` alt süreci üzerinden protokol testleri."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

import pytest
from mcp import Client

from tests.conftest import call, text_of

pytestmark = pytest.mark.stdio


def data(result):
    assert not result.is_error, text_of(result)
    return json.loads(text_of(result))


async def test_handshake_and_basic_calls_over_real_stdio(stdio_params, settings):
    async with Client(stdio_params) as c:
        assert c.server_info.name == "localasistan"
        assert len((await c.list_tools()).tools) == 25
        data(await call(c, "fs_write", {"path": "merhaba.txt", "content": "Şükrü çay içti ✓"}))
        assert data(await call(c, "fs_read", {"path": "merhaba.txt"}))["content"] == "Şükrü çay içti ✓"
        assert data(await call(c, "calendar_now"))["timezone"] == "Europe/Istanbul"
    assert (settings.workspace / "merhaba.txt").read_text(encoding="utf-8") == "Şükrü çay içti ✓"


async def test_state_persists_across_server_restarts(stdio_params):
    async with Client(stdio_params) as c:
        data(await call(c, "calendar_create_event", {"title": "Kalıcı", "start": "2026-11-20T10:00"}))
        data(await call(c, "db_execute", {"sql": "CREATE TABLE k (v TEXT)"}))
        data(await call(c, "db_insert", {"table": "k", "rows": [{"v": "kalıcı değer"}]}))
    async with Client(stdio_params) as c:
        events = data(await call(c, "calendar_list_events", {"start": "2026-11-20"}))["events"]
        rows = data(await call(c, "db_query", {"sql": "SELECT v FROM k"}))["rows"]
    assert [e["title"] for e in events] == ["Kalıcı"] and rows == [["kalıcı değer"]]


async def test_large_payloads_cross_the_pipe(stdio_params, workspace):
    big = ("satır çok uzun " * 7000)[:100_000]
    (workspace / "big.txt").write_text(big, encoding="utf-8")
    async with Client(stdio_params) as c:
        got = data(await call(c, "fs_read", {"path": "big.txt", "max_chars": 100_000}))
        written = data(await call(c, "fs_write", {"path": "copy.txt", "content": big}))
    assert got["content"] == big and written["bytes_written"] == len(big.encode())
    assert (workspace / "copy.txt").read_text(encoding="utf-8") == big


async def test_parallel_calls_over_one_stdio_connection(stdio_params):
    async with Client(stdio_params) as c:
        results = await asyncio.gather(
            *[call(c, "fs_write", {"path": f"p{i}.txt", "content": str(i)}) for i in range(30)]
        )
        listing = data(await call(c, "fs_list"))
    assert all(not r.is_error for r in results) and listing["count"] == 30


async def test_two_server_processes_cannot_double_book_the_same_slot(stdio_params):
    """Süreçler arası yarış: iki ayrı sunucu süreci aynı takvim dosyasına yazıyor."""

    async def book(label: str):
        async with Client(stdio_params) as c:
            return await call(
                c, "calendar_create_event", {"title": label, "start": "2026-11-25T10:00", "end": "2026-11-25T11:00"}
            )

    outcomes = await asyncio.gather(*[book(f"R{i}") for i in range(4)])
    assert sum(not r.is_error for r in outcomes) == 1
    assert all("Conflicts with" in text_of(r) for r in outcomes if r.is_error)


async def test_server_does_not_receive_the_api_key(monkeypatch, settings, stdio_params):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-never-reach-the-server")
    assert "ANTHROPIC_API_KEY" not in (stdio_params.env or {})
    async with Client(stdio_params) as c:
        # sunucuda env'i okuyan bir araç yoktur; ek olarak alt sürecin ortamını başlatma düzeyinde doğruluyoruz
        assert (await c.list_tools()).tools


def _run_server(env: dict[str, str], stdin: bytes = b"", timeout: float = 20) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if not k.startswith("LOCALASISTAN_")}
    return subprocess.run(
        [sys.executable, "-m", "localasistan.server"],
        input=stdin,
        capture_output=True,
        env={**base, **env},
        timeout=timeout,
        check=False,
    )


def test_invalid_configuration_exits_2_with_message_on_stderr_only(settings):
    proc = _run_server({**settings.server_env(), "LOCALASISTAN_TZ": "Nowhere/Land"})
    assert proc.returncode == 2
    assert b"Unknown timezone" in proc.stderr
    assert proc.stdout == b""  # protokol kanalına hiçbir şey sızmamalı


def test_clean_exit_when_stdin_closes(settings):
    proc = _run_server(settings.server_env(), stdin=b"")
    assert proc.returncode == 0
    assert proc.stdout == b""  # hiçbir şey çağrılmadı: stdout boş kalmalı


async def test_server_process_is_gone_after_client_exit(settings, stdio_params):
    pids_before = _server_pids()
    async with Client(stdio_params) as c:
        await c.list_tools()
        running = _server_pids() - pids_before
        assert len(running) == 1
    await asyncio.sleep(0.5)
    assert not (_server_pids() & running)


def _server_pids() -> set[int]:
    out = subprocess.run(["pgrep", "-f", "localasistan.server"], capture_output=True, text=True, check=False).stdout
    return {int(p) for p in out.split()} - {os.getpid()}


async def test_older_clients_using_the_legacy_handshake_work_too(stdio_params):
    """Claude Desktop gibi mevcut istemciler eski (initialize) el sıkışmasını kullanır."""
    async with Client(stdio_params, mode="legacy") as c:
        assert c.protocol_version != "2026-07-28"
        assert len((await c.list_tools()).tools) == 25
        assert data(await call(c, "calendar_now"))["timezone"] == "Europe/Istanbul"
        assert {str(r.uri) for r in (await c.list_resources()).resources} >= {"calendar://today"}
        assert (await c.get_prompt("daily_briefing", {})).messages
