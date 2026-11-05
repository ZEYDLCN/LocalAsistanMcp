"""Performans bütçeleri: kasıtlı olarak cömert (yavaş makinede de geçer) ama O(n²) gibi gerilemeleri yakalar."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from localasistan.client.hub import McpHub
from localasistan.sandbox import Sandbox
from localasistan.server.app import build_server
from localasistan.server.calendar import CalendarService
from localasistan.server.database import DatabaseService
from localasistan.server.files import FileService


def timed(fn, *args, **kwargs):
    started = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, time.perf_counter() - started


def test_large_workspace_listing_search_and_grep(tmp_path):
    root = tmp_path / "ws"
    for d in range(30):
        (root / f"d{d:02d}").mkdir(parents=True)
        for f in range(100):
            (root / f"d{d:02d}" / f"f{f:03d}.txt").write_text(
                f"satır {f}\nçay {d} kahve\n" + "x" * 2000, encoding="utf-8"
            )
    service = FileService(Sandbox(root), max_file_bytes=1_000_000)

    listing, t_list = timed(service.list_dir, ".", recursive=True, max_entries=1000)
    assert listing["count"] == 1000 and t_list < 3
    found, t_search = timed(service.search, "*.txt", max_results=1000)
    assert found["count"] == 1000 and found["truncated"] and t_search < 5
    grep, t_grep = timed(service.grep, "ÇAY 7 KAHVE", ".", max_matches=500)
    assert grep["count"] == 100 and grep["files_scanned"] >= 100 and t_grep < 8


def test_large_calendar_listing_free_slots_and_ics(tmp_path):
    clock = lambda: datetime(2026, 1, 1, tzinfo=UTC)  # noqa: E731
    cal = CalendarService(tmp_path / "c.db", timezone="Europe/Istanbul", now=clock)
    base = datetime(2026, 3, 1, 8, 0)
    _, t_create = timed(
        lambda: [
            cal.create_event(
                f"Olay {i}",
                (base + timedelta(hours=i * 3)).strftime("%Y-%m-%dT%H:%M"),
                (base + timedelta(hours=i * 3, minutes=45)).strftime("%Y-%m-%dT%H:%M"),
                allow_conflicts=True,
            )
            for i in range(1500)
        ]
    )
    assert t_create < 30
    month, t_month = timed(cal.list_events, "2026-04-01", "2026-04-30", limit=500)
    assert month["count"] == 240 and t_month < 1
    search, t_search = timed(cal.list_events, "2026-03-01", "2026-12-31", query="olay 77", limit=500)
    assert search["count"] >= 1 and t_search < 2
    slots, t_slots = timed(cal.find_free_slots, "2026-04-01", "2026-05-01", 30, max_slots=200)
    assert slots["count"] > 0 and t_slots < 2
    (text, count), t_export = timed(cal.export_ics)
    assert count == 1500 and t_export < 2
    other = CalendarService(tmp_path / "o.db", timezone="Europe/Istanbul", now=clock)
    report, t_import = timed(other.import_ics, text)
    assert report["imported"] == 1500 and t_import < 10


def test_database_bulk_insert_and_query(tmp_path):
    db = DatabaseService(tmp_path / "u.db", timeout_seconds=5)
    db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, amount REAL)")
    rows = [{"name": f"kayıt {i}", "amount": i * 1.5} for i in range(1000)]
    _, t_insert = timed(db.insert, "t", rows)
    assert t_insert < 3
    result, t_query = timed(db.query, "SELECT * FROM t ORDER BY amount DESC", max_rows=1000)
    assert result["row_count"] == 1000 and not result["truncated"] and t_query < 1
    agg, t_agg = timed(db.query, "SELECT COUNT(*), SUM(amount), AVG(amount) FROM t")
    assert agg["rows"][0][0] == 1000 and t_agg < 1


@pytest.mark.parametrize("calls", [200])
async def test_hub_sequential_call_throughput(settings, calls):
    hub = McpHub(build_server(settings))
    try:
        await hub.start()
        started = time.perf_counter()
        for _ in range(calls):
            outcome = await hub.call_tool("calendar_now")
            assert not outcome.is_error
        elapsed = time.perf_counter() - started
    finally:
        await hub.stop()
    assert elapsed < 20, f"{calls} çağrı {elapsed:.1f}s sürdü"
