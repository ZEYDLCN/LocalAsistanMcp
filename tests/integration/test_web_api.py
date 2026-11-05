from __future__ import annotations

import sys

import pytest
from mcp import StdioServerParameters
from starlette.testclient import TestClient

from localasistan.server.app import build_server
from localasistan.web.app import create_app
from tests.support import ScriptedProvider, Step


@pytest.fixture
def client_factory(web_settings):
    opened: list[TestClient] = []

    def make(*, hub_target=None, provider=None, raise_exceptions=True, base_url=None, settings=None) -> TestClient:
        cfg = settings or web_settings
        app = create_app(cfg, hub_target=hub_target or build_server(cfg), provider=provider or ScriptedProvider([]))
        client = TestClient(
            app, base_url=base_url or f"http://127.0.0.1:{cfg.port}", raise_server_exceptions=raise_exceptions
        )
        client.__enter__()
        opened.append(client)
        return client

    yield make
    for client in opened:
        client.__exit__(None, None, None)


@pytest.fixture
def client(client_factory) -> TestClient:
    return client_factory()


JSON = {"content-type": "application/json"}


class TestPageAndStatic:
    def test_index_and_security_headers(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "<title>LocalAsistan</title>" in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
        assert "unsafe-inline" not in r.headers["content-security-policy"]

    def test_page_has_no_inline_script_or_style(self, client):
        html = client.get("/").text
        assert "<script>" not in html and "style=" not in html and "onclick=" not in html

    def test_static_assets(self, client):
        assert "javascript" in client.get("/static/app.js").headers["content-type"]
        assert "text/css" in client.get("/static/style.css").headers["content-type"]
        assert client.get("/static/favicon.svg").status_code == 200
        assert client.get("/static/nope.js").status_code == 404

    @pytest.mark.parametrize(
        "path", ["/static/../app.py", "/static/%2e%2e/app.py", "/static/..%2fapp.py", "/static//etc/passwd"]
    )
    def test_static_path_traversal_is_blocked(self, client, path):
        assert client.get(path).status_code in (400, 404)

    def test_healthz(self, client):
        assert client.get("/healthz").json() == {"status": "ok", "version": "0.1.0"}

    def test_unknown_route(self, client):
        assert client.get("/api/nope").status_code == 404


class TestStatusAndSettings:
    def test_status_shape(self, client, web_settings):
        s = client.get("/api/status").json()
        assert s["mcp"]["connected"] is True and s["mcp"]["tool_count"] == 25
        assert s["mcp"]["server"]["name"] == "localasistan" and s["mcp"]["server"]["capabilities"]["tools"] is True
        assert s["llm"]["model"] == "claude-opus-5-5" and s["llm"]["configured"] is True  # ScriptedProvider
        assert s["approval"] == {"policy": "ask_destructive", "policies": ["ask_destructive", "ask_writes", "auto"]}
        assert s["environment"]["timezone"] == "Europe/Istanbul"
        assert s["environment"]["workspace"] == str(web_settings.workspace)
        assert "api_key" not in str(s).lower() or "sk-" not in str(s)

    def test_unconfigured_llm_is_reported(self, client_factory):
        c = client_factory(provider=ScriptedProvider([], configured=False))
        assert c.get("/api/status").json()["llm"]["configured"] is False

    def test_policy_roundtrip_affects_tool_listing(self, client):
        def approvals():
            return {t["name"]: t["needs_approval"] for t in client.get("/api/tools").json()["tools"]}

        assert approvals()["fs_delete"] is True and approvals()["fs_write"] is False and approvals()["fs_read"] is False
        assert client.post("/api/settings", json={"approval": "ask_writes"}).json() == {"approval": "ask_writes"}
        assert approvals()["fs_write"] is True and approvals()["fs_read"] is False
        client.post("/api/settings", json={"approval": "auto"})
        assert not any(approvals().values())
        assert client.get("/api/status").json()["approval"]["policy"] == "auto"

    @pytest.mark.parametrize("body", [{"approval": "yolo"}, {"approval": 5}, {"approval": None}])
    def test_invalid_policy_rejected_and_unchanged(self, client, body):
        assert client.post("/api/settings", json=body).status_code == 400
        assert client.get("/api/status").json()["approval"]["policy"] == "ask_destructive"

    def test_settings_without_known_keys_is_a_noop(self, client):
        assert client.post("/api/settings", json={"foo": 1}).json() == {"approval": "ask_destructive"}


class TestTools:
    def test_listing(self, client):
        tools = client.get("/api/tools").json()["tools"]
        assert len(tools) == 25 and [t["name"] for t in tools] == sorted(t["name"] for t in tools)
        by = {t["name"]: t for t in tools}
        assert (
            by["fs_read"]["kind"] == "read"
            and by["fs_write"]["kind"] == "write"
            and by["fs_delete"]["kind"] == "destructive"
        )
        assert by["fs_read"]["input_schema"]["required"] == ["path"]
        assert len(client.get("/api/tools?refresh=1").json()["tools"]) == 25

    def test_call_success_and_structured_data(self, client):
        r = client.post("/api/tools/calendar_now/call", json={"arguments": {}})
        out = r.json()
        assert r.status_code == 200 and out["is_error"] is False and out["data"]["timezone"] == "Europe/Istanbul"
        assert out["duration_ms"] >= 0 and out["text"].startswith("{")

    def test_call_without_body_uses_empty_arguments(self, client):
        assert (
            client.post(
                "/api/tools/calendar_now/call", content=b"", headers={"content-type": "application/json"}
            ).status_code
            == 200
        )

    def test_call_domain_error_is_200_with_is_error(self, client):
        out = client.post("/api/tools/fs_read/call", json={"arguments": {"path": "../etc/passwd"}}).json()
        assert out["is_error"] is True and "outside the workspace" in out["text"] and out["data"] is None

    def test_manual_call_roundtrip_with_unicode(self, client, workspace):
        client.post("/api/tools/fs_write/call", json={"arguments": {"path": "çay.txt", "content": "Şükrü ✓"}})
        out = client.post("/api/tools/fs_read/call", json={"arguments": {"path": "çay.txt"}}).json()
        assert out["data"]["content"] == "Şükrü ✓" and (workspace / "çay.txt").exists()

    def test_unknown_tool_404(self, client):
        r = client.post("/api/tools/rm_rf/call", json={"arguments": {}})
        assert r.status_code == 404 and "Unknown tool" in r.json()["error"]

    @pytest.mark.parametrize("body", [{"arguments": [1]}, {"arguments": "x"}, {"arguments": 5}])
    def test_arguments_must_be_an_object(self, client, body):
        assert client.post("/api/tools/fs_list/call", json=body).status_code == 400

    @pytest.mark.parametrize("raw", [b"{not json", b"[1,2]", b'"text"', b"123"])
    def test_malformed_bodies(self, client, raw):
        r = client.post("/api/tools/fs_list/call", content=raw, headers={"content-type": "application/json"})
        assert r.status_code == 400 and "error" in r.json()

    def test_oversized_body_rejected(self, client):
        r = client.post("/api/tools/fs_write/call", json={"arguments": {"path": "x", "content": "a" * 2_100_000}})
        assert r.status_code == 400 and "too large" in r.json()["error"]

    def test_invalid_server_side_arguments_are_reported(self, client):
        out = client.post("/api/tools/fs_list/call", json={"arguments": {"max_entries": "lots"}}).json()
        assert out["is_error"] is True and "max_entries" in out["text"]


class TestResourcesAndPrompts:
    def test_resources(self, client, workspace):
        (workspace / "a.txt").write_text("selam", encoding="utf-8")
        data = client.get("/api/resources").json()
        assert {r["uri"] for r in data["resources"]} >= {"calendar://today", "db://schema"}
        assert data["templates"][0]["uri_template"] == "workspace://file/{+path}"
        read = client.get("/api/resources/read", params={"uri": "workspace://file/a.txt"}).json()
        assert read["contents"][0]["text"] == "selam"
        assert (
            "No events"
            in client.get("/api/resources/read", params={"uri": "calendar://today"}).json()["contents"][0]["text"]
        )

    def test_resource_errors(self, client):
        assert client.get("/api/resources/read").status_code == 400
        missing = client.get("/api/resources/read", params={"uri": "workspace://file/nope.txt"})
        assert missing.status_code == 404
        traversal = client.get("/api/resources/read", params={"uri": "workspace://file/../../etc/passwd"})
        assert traversal.status_code == 404 and "root:" not in traversal.text

    def test_prompts(self, client):
        prompts = {p["name"] for p in client.get("/api/prompts").json()["prompts"]}
        assert prompts == {"daily_briefing", "summarize_file", "schedule_meeting"}
        got = client.post("/api/prompts/summarize_file", json={"arguments": {"path": "notlar/x.md"}}).json()
        assert "notlar/x.md" in got["messages"][0]["text"] and got["messages"][0]["role"] == "user"

    @pytest.mark.parametrize(
        ("name", "body"),
        [
            ("summarize_file", {"arguments": {}}),
            ("nope", {"arguments": {}}),
            ("summarize_file", {"arguments": {"path": 5}}),
            ("summarize_file", {"arguments": [1]}),
        ],
    )
    def test_prompt_errors(self, client, name, body):
        assert client.post(f"/api/prompts/{name}", json=body).status_code == 400


class TestChatValidation:
    @pytest.mark.parametrize(
        "body", [{}, {"message": ""}, {"message": "   "}, {"message": 5}, {"message": None}, {"message": ["a"]}]
    )
    def test_bad_messages(self, client, body):
        assert client.post("/api/chat", json=body).status_code == 400

    def test_message_too_long(self, client):
        assert client.post("/api/chat", json={"message": "a" * 100_001}).status_code == 400

    @pytest.mark.parametrize("session_id", ["nope", 5, ["x"]])
    def test_unknown_session_is_404_not_silently_new(self, client, session_id):
        r = client.post("/api/chat", json={"message": "hi", "session_id": session_id})
        assert r.status_code == 404 and "not found" in r.json()["error"]

    def test_session_endpoints_for_unknown_ids(self, client):
        assert client.get("/api/chat/nope").status_code == 404
        assert client.post("/api/chat/nope/reset", json={}).status_code == 404
        assert client.delete("/api/chat/nope", headers=JSON).status_code == 404
        assert client.post("/api/chat/nope/approvals/x", json={"approve": True}).status_code == 404
        assert client.get("/api/chats").json() == {"sessions": []}

    def test_full_turn_through_testclient_then_history_and_housekeeping(self, client_factory):
        provider = ScriptedProvider([Step(text="Merhaba!"), Step(text="Yine merhaba")])
        c = client_factory(provider=provider)
        r = c.post("/api/chat", json={"message": "selam"})
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-store"
        import json

        events = [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:")]
        assert events[0]["type"] == "session" and events[-1] == {
            "type": "turn_end",
            "stop_reason": "end_turn",
            "iterations": 1,
        }
        sid = events[0]["session_id"]
        hist = c.get(f"/api/chat/{sid}").json()
        assert hist["title"] == "selam" and hist["busy"] is False and hist["pending_approvals"] == []
        assert [e["type"] for e in hist["transcript"]][:2] == ["user_message", "turn_start"]
        c.post("/api/chat", json={"message": "tekrar", "session_id": sid})
        assert [s["id"] for s in c.get("/api/chats").json()["sessions"]] == [sid]
        assert c.post(f"/api/chat/{sid}/reset", json={}).json() == {"ok": True}
        assert c.get(f"/api/chat/{sid}").json()["transcript"] == []
        assert c.delete(f"/api/chat/{sid}", headers=JSON).json() == {"ok": True}
        assert c.get(f"/api/chat/{sid}").status_code == 404

    def test_approval_payload_validation(self, client_factory):
        c = client_factory(provider=ScriptedProvider([Step(text="x")]))
        sid = c.post("/api/chat", json={"message": "m"}).text.split('"session_id": "')[1].split('"')[0]
        assert c.post(f"/api/chat/{sid}/approvals/abc", json={"approve": "yes"}).status_code == 400
        assert c.post(f"/api/chat/{sid}/approvals/abc", json={}).status_code == 400
        assert c.post(f"/api/chat/{sid}/approvals/abc", json={"approve": True}).status_code == 404


class TestSecurity:
    @pytest.mark.parametrize(
        "host", ["evil.com", "evil.com:8765", "127.0.0.1.evil.com", "localhost.evil.com", "0.0.0.0", "192.168.1.5"]
    )
    def test_foreign_host_headers_rejected_dns_rebinding(self, client, host):
        for method, path in (("GET", "/"), ("GET", "/api/status"), ("POST", "/api/tools/fs_list/call")):
            r = client.request(method, path, headers={"host": host, "content-type": "application/json"})
            assert r.status_code == 403 and "Host" in r.json()["error"], (method, path, host)

    def test_wrong_port_in_host_rejected(self, client, web_settings):
        assert client.get("/api/status", headers={"host": f"127.0.0.1:{web_settings.port + 1}"}).status_code == 403

    @pytest.mark.parametrize("host_fmt", ["localhost:{p}", "127.0.0.1:{p}", "[::1]:{p}", "LOCALHOST:{p}"])
    def test_loopback_names_accepted(self, client, web_settings, host_fmt):
        assert client.get("/healthz", headers={"host": host_fmt.format(p=web_settings.port)}).status_code == 200

    @pytest.mark.parametrize(
        "origin",
        [
            "http://evil.com",
            "https://evil.com",
            "null",
            "http://127.0.0.1:1",
            "http://localhost.evil.com:8765",
            "ftp://127.0.0.1",
        ],
    )
    def test_cross_origin_requests_rejected(self, client, origin):
        r = client.post(
            "/api/tools/fs_write/call", json={"arguments": {"path": "x", "content": "y"}}, headers={"origin": origin}
        )
        assert r.status_code == 403 and "Cross-origin" in r.json()["error"]
        assert client.get("/api/status", headers={"origin": origin}).status_code == 403

    def test_same_origin_request_is_fine(self, client, web_settings):
        r = client.post(
            "/api/settings", json={"approval": "auto"}, headers={"origin": f"http://127.0.0.1:{web_settings.port}"}
        )
        assert r.status_code == 200

    @pytest.mark.parametrize(
        "content_type", ["text/plain", "application/x-www-form-urlencoded", "multipart/form-data; boundary=x", ""]
    )
    def test_state_changing_requests_require_json_content_type(self, client, content_type, workspace):
        headers = {"content-type": content_type} if content_type else {}
        r = client.post(
            "/api/tools/fs_write/call", content=b'{"arguments":{"path":"pwn.txt","content":"x"}}', headers=headers
        )
        assert r.status_code == 415 and not (workspace / "pwn.txt").exists()
        assert client.delete("/api/chat/x", headers=headers).status_code == 415
        assert client.post("/api/reconnect", content=b"", headers=headers).status_code == 415

    def test_json_content_type_with_charset_is_accepted(self, client):
        r = client.post(
            "/api/settings", content=b'{"approval":"auto"}', headers={"content-type": "application/json; charset=utf-8"}
        )
        assert r.status_code == 200

    def test_simple_form_csrf_attack_cannot_write_files(self, client, workspace):
        """Kötü niyetli bir sayfanın <form method=post enctype=text/plain> saldırısı."""
        r = client.post(
            "/api/tools/fs_write/call",
            content='{"arguments":{"path":"pwned.txt","content":"x"},"pad":"=1"}',
            headers={"content-type": "text/plain", "origin": "http://evil.example"},
        )
        assert r.status_code in (403, 415) and not (workspace / "pwned.txt").exists()

    def test_no_cors_headers_ever(self, client):
        for response in (
            client.get("/api/status", headers={"origin": "http://evil.com"}),
            client.options(
                "/api/status", headers={"origin": "http://evil.com", "access-control-request-method": "POST"}
            ),
            client.get("/api/status"),
        ):
            assert not any(h.startswith("access-control-") for h in response.headers)

    def test_errors_do_not_leak_internals(self, client_factory, monkeypatch):
        c = client_factory(raise_exceptions=False)
        from localasistan.client.hub import McpHub

        async def boom(self):
            raise RuntimeError("secret /Users/x/internal path")

        monkeypatch.setattr(McpHub, "list_resources", boom)
        r = c.get("/api/resources")
        assert r.status_code == 500 and r.json() == {"error": "Internal server error."}


class TestMcpAvailability:
    def test_reconnect_endpoint_restarts_the_server(self, client):
        before = client.get("/api/status").json()["mcp"]["restarts"]
        r = client.post("/api/reconnect", json={})
        assert r.status_code == 200 and r.json()["connected"] is True and r.json()["restarts"] == before + 1
        assert client.post("/api/tools/calendar_now/call", json={"arguments": {}}).json()["is_error"] is False

    @pytest.mark.stdio
    def test_app_starts_and_reports_when_the_mcp_server_cannot_start(self, client_factory):
        c = client_factory(hub_target=StdioServerParameters(command="/definitely/missing", args=[]))
        assert c.get("/healthz").status_code == 200 and c.get("/").status_code == 200
        status = c.get("/api/status").json()["mcp"]
        assert status["connected"] is False and "command not found" in status["last_error"]
        assert c.get("/api/tools").status_code == 503
        assert c.post("/api/reconnect", json={}).status_code == 503
        assert c.post("/api/tools/fs_list/call", json={"arguments": {}}).status_code == 503
        assert c.get("/api/resources").status_code == 503
        assert c.get("/api/resources/read", params={"uri": "calendar://today"}).status_code == 503
        assert c.get("/api/prompts").status_code == 503
        assert c.post("/api/prompts/daily_briefing", json={"arguments": {}}).status_code == 503

    @pytest.mark.stdio
    def test_real_stdio_server_behind_the_app(self, client_factory, web_settings):
        target = StdioServerParameters(
            command=sys.executable, args=["-m", "localasistan.server"], env=web_settings.server_env()
        )
        c = client_factory(hub_target=target)
        assert c.get("/api/status").json()["mcp"]["tool_count"] == 25
        assert (
            c.post("/api/tools/calendar_now/call", json={"arguments": {}}).json()["data"]["timezone"]
            == "Europe/Istanbul"
        )
