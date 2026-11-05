from __future__ import annotations

import json
import re

import pytest
from jsonschema import Draft202012Validator
from mcp.shared.exceptions import MCPError

from localasistan.server.calendar import CalendarService
from tests.conftest import call, text_of

READ_ONLY = {
    "fs_list", "fs_read", "fs_info", "fs_search", "fs_grep",
    "db_list_tables", "db_describe_table", "db_query",
    "calendar_now", "calendar_list_events", "calendar_get_event", "calendar_find_free_slots",
}  # fmt: skip
DESTRUCTIVE = {"fs_overwrite", "fs_delete", "db_execute", "calendar_update_event", "calendar_delete_event"}
ADDITIVE = {
    "fs_write", "fs_append", "fs_mkdir", "fs_move", "db_insert",
    "calendar_create_event", "calendar_export_ics", "calendar_import_ics",
}  # fmt: skip
ALL_TOOLS = READ_ONLY | DESTRUCTIVE | ADDITIVE


def data(result):
    assert not result.is_error, text_of(result)
    return json.loads(text_of(result))


class TestHandshakeAndCatalog:
    async def test_server_info_and_capabilities(self, connect):
        async with connect() as c:
            assert c.server_info.name == "localasistan"
            assert c.server_info.version
            caps = c.server_capabilities
            assert caps.tools is not None and caps.resources is not None and caps.prompts is not None
            assert "Europe/Istanbul" in c.instructions
            assert "untrusted" in c.instructions

    async def test_exact_tool_set(self, connect):
        async with connect() as c:
            names = {t.name for t in (await c.list_tools()).tools}
        assert names == ALL_TOOLS
        assert len(names) == 25

    async def test_every_tool_is_well_formed(self, connect):
        async with connect() as c:
            tools = (await c.list_tools()).tools
        for tool in tools:
            assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tool.name)  # Anthropic araç adı kuralı
            assert tool.description and len(tool.description) > 20, tool.name
            assert tool.input_schema["type"] == "object"
            Draft202012Validator.check_schema(tool.input_schema)
            for prop, spec in tool.input_schema.get("properties", {}).items():
                if prop in tool.input_schema.get("required", []):
                    assert spec.get("description"), f"{tool.name}.{prop} needs a description"
            assert tool.annotations is not None and tool.annotations.open_world_hint is False

    async def test_safety_classification_is_exact(self, connect):
        """İstemcinin onay politikası buna dayanır: sınıflandırma yanlışsa güvenlik açığıdır."""
        async with connect() as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
        for name in READ_ONLY:
            assert tools[name].annotations.read_only_hint is True, name
        for name in DESTRUCTIVE:
            a = tools[name].annotations
            assert a.read_only_hint is False and a.destructive_hint is True, name
        for name in ADDITIVE:
            a = tools[name].annotations
            assert a.read_only_hint is False and a.destructive_hint is False, name

    async def test_destructive_tool_descriptions_warn(self, connect):
        async with connect() as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
        for name in DESTRUCTIVE:
            assert "DESTRUCTIVE" in tools[name].description, name


class TestFileTools:
    async def test_full_lifecycle(self, connect, workspace):
        async with connect() as c:
            assert data(await call(c, "fs_mkdir", {"path": "notlar"}))["created"] is True
            w = data(await call(c, "fs_write", {"path": "notlar/gün.md", "content": "# Başlık\nçay şeker\n"}))
            assert w["path"] == "notlar/gün.md"
            assert data(await call(c, "fs_append", {"path": "notlar/gün.md", "content": "- süt\n"}))["created"] is False
            read = data(await call(c, "fs_read", {"path": "notlar/gün.md"}))
            assert read["content"] == "# Başlık\nçay şeker\n- süt\n"
            assert data(await call(c, "fs_info", {"path": "notlar/gün.md"}))["mime"] == "text/markdown"
            assert [e["path"] for e in data(await call(c, "fs_list", {"path": "notlar"}))["entries"]] == [
                "notlar/gün.md"
            ]
            assert data(await call(c, "fs_search", {"pattern": "*.md"}))["count"] == 1
            assert data(await call(c, "fs_grep", {"query": "ÇAY"}))["matches"][0]["line"] == 2
            data(await call(c, "fs_move", {"source": "notlar/gün.md", "destination": "arşiv/gün.md"}))
            data(await call(c, "fs_overwrite", {"path": "arşiv/gün.md", "content": "yeni"}))
            assert (workspace / "arşiv" / "gün.md").read_text(encoding="utf-8") == "yeni"
            assert data(await call(c, "fs_delete", {"path": "arşiv", "recursive": True}))["items_removed"] == 2
            assert not (workspace / "arşiv").exists()

    async def test_paging_through_a_large_file(self, connect, workspace):
        (workspace / "big.txt").write_text("0123456789" * 3000, encoding="utf-8")
        async with connect() as c:
            offset, chunks = 0, []
            while offset is not None:
                part = data(await call(c, "fs_read", {"path": "big.txt", "offset": offset, "max_chars": 8000}))
                chunks.append(part["content"])
                offset = part["next_offset"]
        assert len(chunks) == 4 and "".join(chunks) == "0123456789" * 3000

    @pytest.mark.parametrize(
        ("tool", "args", "fragment"),
        [
            ("fs_read", {"path": "../../etc/passwd"}, "outside the workspace"),
            ("fs_read", {"path": "/etc/passwd"}, "outside the workspace"),
            ("fs_write", {"path": "../x.txt", "content": "x"}, "outside the workspace"),
            ("fs_list", {"path": "nope"}, "does not exist"),
            ("fs_delete", {"path": "."}, "workspace root"),
            ("fs_write", {"path": "a\u0000b", "content": "x"}, "NUL"),
        ],
    )
    async def test_domain_errors_are_readable_and_flagged(self, connect, tool, args, fragment):
        async with connect() as c:
            r = await call(c, tool, args)
        assert r.is_error is True
        assert fragment in text_of(r)
        assert "Traceback" not in text_of(r)

    async def test_symlink_escape_blocked_through_protocol(self, connect, workspace, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")
        (workspace / "peek").symlink_to(secret)
        async with connect() as c:
            r = await call(c, "fs_read", {"path": "peek"})
            g = data(await call(c, "fs_grep", {"query": "SECRET"}))
        assert r.is_error and "outside the workspace" in text_of(r)
        assert g["count"] == 0

    async def test_existing_file_protection(self, connect):
        async with connect() as c:
            data(await call(c, "fs_write", {"path": "a.txt", "content": "1"}))
            r = await call(c, "fs_write", {"path": "a.txt", "content": "2"})
            assert r.is_error and "fs_overwrite" in text_of(r)
            assert data(await call(c, "fs_read", {"path": "a.txt"}))["content"] == "1"


class TestDatabaseTools:
    async def test_full_lifecycle(self, connect):
        async with connect() as c:
            assert data(await call(c, "db_list_tables"))["count"] == 0
            data(
                await call(
                    c, "db_execute", {"sql": "CREATE TABLE gider (id INTEGER PRIMARY KEY, kalem TEXT, tutar REAL)"}
                )
            )
            ins = data(
                await call(
                    c,
                    "db_insert",
                    {"table": "gider", "rows": [{"kalem": "Çay", "tutar": 12.5}, {"kalem": "Simit", "tutar": 8}]},
                )
            )
            assert ins["inserted"] == 2
            q = data(
                await call(
                    c,
                    "db_query",
                    {"sql": "SELECT kalem, tutar FROM gider WHERE tutar > ? ORDER BY tutar", "params": [10]},
                )
            )
            assert q["rows"] == [["Çay", 12.5]]
            d = data(await call(c, "db_describe_table", {"table": "gider"}))
            assert d["row_count"] == 2 and [col["name"] for col in d["columns"]] == ["id", "kalem", "tutar"]
            upd = data(
                await call(
                    c, "db_execute", {"sql": "UPDATE gider SET tutar = tutar * 2 WHERE kalem = ?", "params": ["Çay"]}
                )
            )
            assert upd["rows_affected"] == 1
            assert data(await call(c, "db_query", {"sql": "SELECT SUM(tutar) FROM gider"}))["rows"] == [[33.0]]

    @pytest.mark.parametrize(
        ("tool", "args", "fragment"),
        [
            ("db_query", {"sql": "DROP TABLE x"}, "only accepts SELECT"),
            ("db_query", {"sql": "SELECT 1; SELECT 2"}, "one SQL statement"),
            ("db_query", {"sql": "ATTACH DATABASE '/tmp/x.db' AS x"}, "only accepts SELECT"),
            ("db_execute", {"sql": "PRAGMA writable_schema=1"}, "does not allow"),
            ("db_execute", {"sql": "VACUUM INTO '/tmp/leak-proto.db'"}, "does not allow"),
            ("db_insert", {"table": "ghost", "rows": [{"a": 1}]}, "does not exist"),
            ("db_query", {"sql": "SELECT * FROM ghost"}, "no such table"),
        ],
    )
    async def test_guarded_statements(self, connect, tool, args, fragment):
        async with connect() as c:
            r = await call(c, tool, args)
        assert r.is_error and fragment in text_of(r)

    async def test_user_db_is_separate_from_calendar_db(self, connect, settings):
        async with connect() as c:
            data(await call(c, "calendar_create_event", {"title": "Gizli", "start": "2026-11-06T10:00"}))
            tables = [t["name"] for t in data(await call(c, "db_list_tables"))["tables"]]
            r = await call(c, "db_query", {"sql": "SELECT * FROM events"})
        assert tables == [] and r.is_error and "no such table" in text_of(r)
        assert settings.calendar_db != settings.user_db


class TestCalendarTools:
    async def test_full_lifecycle(self, connect):
        async with connect() as c:
            now = data(await call(c, "calendar_now"))
            assert now["date"] == "2026-11-05" and now["weekday"] == "Thursday"
            e = data(
                await call(
                    c,
                    "calendar_create_event",
                    {"title": "Diş randevusu", "start": "2026-11-06T10:00", "location": "Kadıköy"},
                )
            )
            assert e["start"] == "2026-11-06T10:00:00+03:00"
            clash = await call(c, "calendar_create_event", {"title": "Çakışan", "start": "2026-11-06T10:30"})
            assert clash.is_error and "allow_conflicts" in text_of(clash)
            moved = data(await call(c, "calendar_update_event", {"event_id": e["id"], "start": "2026-11-06T15:00"}))
            assert moved["start"] == "2026-11-06T15:00:00+03:00" and moved["duration_minutes"] == 60
            listing = data(await call(c, "calendar_list_events", {"start": "2026-11-06", "query": "kadıköy"}))
            assert [x["title"] for x in listing["events"]] == ["Diş randevusu"]
            slots = data(
                await call(
                    c, "calendar_find_free_slots", {"start": "2026-11-06", "end": "2026-11-06", "duration_minutes": 120}
                )
            )
            assert [(s["start"][11:16], s["end"][11:16]) for s in slots["slots"]] == [
                ("09:00", "15:00"),
                ("16:00", "18:00"),
            ]
            assert data(await call(c, "calendar_get_event", {"event_id": e["id"]}))["title"] == "Diş randevusu"
            assert data(await call(c, "calendar_delete_event", {"event_id": e["id"]}))["deleted"]["id"] == e["id"]
            gone = await call(c, "calendar_get_event", {"event_id": e["id"]})
            assert gone.is_error and "does not exist" in text_of(gone)

    async def test_ics_export_and_import_through_the_workspace(self, connect, workspace):
        async with connect() as c:
            data(await call(c, "calendar_create_event", {"title": "Toplantı, önemli", "start": "2026-11-06T10:00"}))
            data(
                await call(
                    c,
                    "calendar_create_event",
                    {"title": "Tatil", "start": "2026-11-09", "end": "2026-11-10", "all_day": True},
                )
            )
            exported = data(await call(c, "calendar_export_ics", {"path": "dışa/takvim.ics"}))
            assert exported["events_exported"] == 2
            assert (workspace / "dışa" / "takvim.ics").read_text(encoding="utf-8").count("BEGIN:VEVENT") == 2
            again = await call(c, "calendar_export_ics", {"path": "dışa/takvim.ics"})
            assert again.is_error and "already exists" in text_of(again)
            wrong = await call(c, "calendar_export_ics", {"path": "takvim.txt"})
            assert wrong.is_error and ".ics" in text_of(wrong)
            report = data(await call(c, "calendar_import_ics", {"path": "dışa/takvim.ics"}))
            assert report["imported"] == 0 and report["skipped_duplicates"] == 2  # aynı UID'ler

    async def test_import_foreign_ics_into_fresh_calendar(self, connect, workspace):
        (workspace / "dış.ics").write_text(
            "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:x1\r\nDTSTART;TZID=Europe/Istanbul:20261110T090000\r\n"
            "DTEND;TZID=Europe/Istanbul:20261110T100000\r\nSUMMARY:Dış\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
            encoding="utf-8",
        )
        async with connect() as c:
            assert data(await call(c, "calendar_import_ics", {"path": "dış.ics"}))["imported"] == 1
            ev = data(await call(c, "calendar_list_events", {"start": "2026-11-10"}))["events"][0]
        assert ev["title"] == "Dış" and ev["start"] == "2026-11-10T09:00:00+03:00"

    @pytest.mark.parametrize(
        ("tool", "args", "fragment"),
        [
            ("calendar_create_event", {"title": "x", "start": "yarın"}, "could not understand"),
            ("calendar_create_event", {"title": "x", "start": "2026-11-06"}, "needs a time"),
            ("calendar_get_event", {"event_id": 999}, "does not exist"),
            ("calendar_update_event", {"event_id": 1, "title": "x"}, "does not exist"),
            ("calendar_update_event", {"event_id": 1}, "Nothing to update"),
            ("calendar_list_events", {"start": "2026-11-06T10:00"}, "end is required"),
            (
                "calendar_find_free_slots",
                {"start": "2026-11-06", "end": "2026-11-06", "duration_minutes": 1},
                "duration_minutes",
            ),
        ],
    )
    async def test_errors(self, connect, tool, args, fragment):
        async with connect() as c:
            r = await call(c, tool, args)
        assert r.is_error and fragment in text_of(r)


class TestProtocolErrors:
    async def test_wrong_argument_types_and_missing_required(self, connect):
        async with connect() as c:
            bad_type = await call(c, "fs_list", {"max_entries": "lots"})
            missing = await call(c, "fs_read", {})
            unknown = await call(c, "no_such_tool", {})
        assert bad_type.is_error and "max_entries" in text_of(bad_type)
        assert missing.is_error and "path" in text_of(missing)
        assert unknown.is_error and "no_such_tool" in text_of(unknown)

    async def test_unexpected_exception_does_not_leak_details(self, connect, monkeypatch):
        def boom(self, *a, **k):
            raise RuntimeError("secret-internal-detail /Users/someone/path")

        monkeypatch.setattr(CalendarService, "now_info", boom)
        async with connect() as c:
            r = await call(c, "calendar_now")
        assert r.is_error
        assert "secret-internal-detail" not in text_of(r) and "/Users/" not in text_of(r)

    async def test_results_are_compact_json_text(self, connect):
        async with connect() as c:
            r = await call(c, "calendar_now")
        raw = text_of(r)
        assert "\n" not in raw and ": " not in raw.replace('"now":"', "")  # kompakt ayırıcılar

    async def test_concurrent_calls_on_one_connection(self, connect):
        import asyncio

        async with connect() as c:
            results = await asyncio.gather(
                *[call(c, "fs_write", {"path": f"c{i}.txt", "content": str(i)}) for i in range(20)]
            )
            listing = data(await call(c, "fs_list"))
        assert all(not r.is_error for r in results) and listing["count"] == 20


class TestResources:
    async def test_listing(self, connect):
        async with connect() as c:
            uris = {str(r.uri) for r in (await c.list_resources()).resources}
            templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
        assert uris == {"calendar://today", "calendar://week", "db://schema", "workspace://tree"}
        assert templates == {"workspace://file/{+path}"}

    async def test_contents(self, connect, workspace):
        (workspace / "docs").mkdir()
        (workspace / "docs" / "not.txt").write_text("içerik ✓", encoding="utf-8")
        async with connect() as c:
            data(await call(c, "calendar_create_event", {"title": "Bugünkü iş", "start": "2026-11-05T15:00"}))
            data(await call(c, "db_execute", {"sql": "CREATE TABLE t (a INTEGER)"}))
            today = (await c.read_resource("calendar://today")).contents[0].text
            week = (await c.read_resource("calendar://week")).contents[0].text
            schema = (await c.read_resource("db://schema")).contents[0].text
            tree = (await c.read_resource("workspace://tree")).contents[0].text
            file = (await c.read_resource("workspace://file/docs/not.txt")).contents[0].text
        assert "15:00 - 16:00: Bugünkü iş" in today and "Bugünkü iş" in week
        assert "CREATE TABLE t" in schema
        assert "docs/" in tree and "docs/not.txt" in tree
        assert file == "içerik ✓"

    @pytest.mark.parametrize(
        "uri",
        [
            "workspace://file/../../etc/passwd",
            "workspace://file//etc/passwd",
            "workspace://file/%2e%2e/%2e%2e/etc/passwd",
            "workspace://file/docs/../../../etc/passwd",
            "workspace://file/missing.txt",
        ],
    )
    async def test_file_resource_cannot_escape_or_find_missing(self, connect, uri):
        async with connect() as c:
            with pytest.raises(MCPError):
                await c.read_resource(uri)

    async def test_symlink_escape_via_resource(self, connect, workspace, tmp_path):
        (tmp_path / "secret.txt").write_text("TOP SECRET")
        (workspace / "peek.txt").symlink_to(tmp_path / "secret.txt")
        async with connect() as c:
            with pytest.raises(MCPError) as info:
                await c.read_resource("workspace://file/peek.txt")
        assert "TOP SECRET" not in str(info.value)

    async def test_empty_states(self, connect):
        async with connect() as c:
            assert "(workspace is empty)" in (await c.read_resource("workspace://tree")).contents[0].text
            assert "no tables" in (await c.read_resource("db://schema")).contents[0].text
            assert "No events" in (await c.read_resource("calendar://today")).contents[0].text


class TestPrompts:
    async def test_listing_and_rendering(self, connect):
        async with connect() as c:
            prompts = {p.name: p for p in (await c.list_prompts()).prompts}
            assert set(prompts) == {"daily_briefing", "summarize_file", "schedule_meeting"}
            assert [a.name for a in prompts["summarize_file"].arguments or []] == ["path"]
            brief = await c.get_prompt("daily_briefing", {})
            dated = await c.get_prompt("daily_briefing", {"date": "2026-11-06"})
            summary = await c.get_prompt("summarize_file", {"path": "notlar/toplantı.md"})
            meeting = await c.get_prompt("schedule_meeting", {"title": "Sprint planlama", "duration_minutes": "45"})
        assert "calendar_now" in brief.messages[0].content.text
        assert "2026-11-06" in dated.messages[0].content.text
        assert "notlar/toplantı.md" in summary.messages[0].content.text
        assert (
            "45 minutes" in meeting.messages[0].content.text and "Sprint planlama" in meeting.messages[0].content.text
        )
        assert brief.messages[0].role == "user"

    async def test_missing_required_argument(self, connect):
        async with connect() as c:
            with pytest.raises(MCPError):
                await c.get_prompt("summarize_file", {})
            with pytest.raises(MCPError):
                await c.get_prompt("nope", {})
