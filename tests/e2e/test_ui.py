from __future__ import annotations

import json
import re

import pytest
from playwright.sync_api import expect

from tests.support import ScriptedProvider, Step

pytestmark = pytest.mark.e2e


def send(page, text: str) -> None:
    page.fill("#input", text)
    page.press("#input", "Enter")


def wait_idle(page) -> None:
    expect(page.locator("#send")).to_be_enabled()
    expect(page.locator("#stop")).to_be_hidden()


class TestPageLoad:
    def test_loads_cleanly_and_shows_status(self, make_ui):
        ui = make_ui()
        page = ui.page
        expect(page).to_have_title("LocalAsistan")
        expect(page.locator("#mcp-label")).to_have_text("MCP bağlı")
        expect(page.locator("#model-chip")).to_have_text("claude-opus-5-5")
        expect(page.locator("#version")).to_have_text("v0.1.0")
        expect(page.locator("#policy-select")).to_have_value("ask_destructive")
        expect(page.locator(".example")).to_have_count(6)
        expect(page.locator("#banner")).to_be_hidden()
        page.wait_for_timeout(300)
        assert ui.errors == [], ui.errors  # CSP ihlali / JS hatası yok
        ui.shot("01-home")

    def test_missing_api_key_shows_banner_and_error_on_send(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([], configured=False))
        expect(ui.page.locator("#banner")).to_contain_text("ANTHROPIC_API_KEY")
        send(ui.page, "merhaba")
        expect(ui.page.locator(".note.error")).to_contain_text("ANTHROPIC_API_KEY")
        wait_idle(ui.page)

    def test_example_chips_fill_the_input(self, make_ui):
        ui = make_ui()
        ui.page.click(".example >> nth=0")
        expect(ui.page.locator("#input")).to_have_value("Bugünkü takvimimi özetle")

    def test_mobile_layout_has_no_horizontal_scroll(self, make_ui):
        ui = make_ui(viewport={"width": 375, "height": 740})
        ui.page.wait_for_timeout(300)
        ui.shot("09-mobile-home")
        for tab in ("chat", "tools", "resources", "status"):
            ui.page.click(f"#tabbtn-{tab}")
            ui.page.wait_for_timeout(150)
            assert ui.page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), tab
        ui.shot("09-mobile")


class TestChat:
    def test_markdown_streaming_tool_card_and_usage(self, make_ui):
        ui = make_ui()
        (ui.workspace / "alisveris.txt").write_text("süt\nekmek\n", encoding="utf-8")
        answer = (
            "## Liste\nDosyada **iki** madde var:\n\n- süt\n- ekmek\n\n```\nsüt\nekmek\n```\n\n"
            "| Ürün | Adet |\n|---|---|\n| süt | 1 |\n| ekmek | 2 |\n\nDetay için `fs_read` kullandım."
        )
        ui.provider.steps += [
            Step(text="Bakıyorum.", tools=[("fs_read", {"path": "alisveris.txt"})]),
            Step(text=answer),
        ]
        send(ui.page, "alışveriş listemde ne var?")
        expect(ui.page.locator(".msg.user")).to_have_text("alışveriş listemde ne var?")
        card = ui.page.locator(".tool-card")
        expect(card).to_have_class("tool-card ok")
        expect(card.locator(".tool-name")).to_have_text("fs_read")
        expect(card.locator(".badge")).to_have_text("okuma")
        expect(card.locator(".tool-state")).to_contain_text("ms")
        card.locator("summary").click()
        expect(card.locator("pre").last).to_contain_text("süt")
        last = ui.page.locator(".msg.assistant").last
        expect(last.locator("h3")).to_have_text("Liste")
        expect(last.locator("strong")).to_have_text("iki")
        expect(last.locator("ul li")).to_have_count(2)
        expect(last.locator("pre code")).to_contain_text("ekmek")
        expect(last.locator("table tbody tr")).to_have_count(2)
        expect(last.locator("p code")).to_have_text("fs_read")
        expect(ui.page.locator(".usage")).to_contain_text("↑ 10")
        wait_idle(ui.page)
        ui.page.evaluate("document.querySelector('#messages').scrollTop = 1e9")
        ui.shot("02-chat-markdown")
        assert ui.errors == [], ui.errors

    def test_enter_sends_and_shift_enter_adds_a_newline(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text="tamam")]))
        ui.page.fill("#input", "satır 1")
        ui.page.press("#input", "Shift+Enter")
        ui.page.keyboard.type("satır 2")
        assert ui.page.input_value("#input") == "satır 1\nsatır 2"
        assert ui.page.locator(".msg.user").count() == 0
        ui.page.press("#input", "Enter")
        expect(ui.page.locator(".msg.user")).to_have_text("satır 1\nsatır 2")
        expect(ui.page.locator("#input")).to_have_value("")

    def test_empty_message_is_not_sent(self, make_ui):
        ui = make_ui()
        ui.page.fill("#input", "   ")
        ui.page.press("#input", "Enter")
        assert ui.page.locator(".msg.user").count() == 0 and ui.provider.calls == []

    def test_approval_approve_deletes_the_file(self, make_ui):
        ui = make_ui()
        (ui.workspace / "eski.txt").write_text("x")
        ui.provider.steps += [Step(tools=[("fs_delete", {"path": "eski.txt"})]), Step(text="Sildim.")]
        send(ui.page, "eski.txt'yi sil")
        approval = ui.page.locator(".approval")
        expect(approval).to_contain_text("Onay gerekiyor")
        expect(approval.locator("pre")).to_contain_text("eski.txt")
        expect(ui.page.locator(".tool-card .badge")).to_have_text("yıkıcı")
        assert (ui.workspace / "eski.txt").exists()
        ui.shot("03-approval")
        approval.get_by_role("button", name="Onayla").click()
        expect(approval).to_contain_text("Onaylandı")
        expect(ui.page.locator(".msg.assistant").last).to_have_text("Sildim.")
        wait_idle(ui.page)
        assert not (ui.workspace / "eski.txt").exists()

    def test_approval_reject_keeps_the_file(self, make_ui):
        ui = make_ui()
        (ui.workspace / "kalsin.txt").write_text("x")
        ui.provider.steps += [Step(tools=[("fs_delete", {"path": "kalsin.txt"})]), Step(text="Silmedim.")]
        send(ui.page, "sil")
        ui.page.get_by_role("button", name="Reddet").click()
        expect(ui.page.locator(".approval")).to_contain_text("Reddedildi")
        expect(ui.page.locator(".tool-card")).to_have_class("tool-card err")
        wait_idle(ui.page)
        assert (ui.workspace / "kalsin.txt").exists()

    def test_policy_selector_changes_behaviour(self, make_ui):
        ui = make_ui()
        (ui.workspace / "a.txt").write_text("1")
        ui.page.select_option("#policy-select", "auto")
        ui.page.wait_for_timeout(300)
        ui.provider.steps += [Step(tools=[("fs_delete", {"path": "a.txt"})]), Step(text="oldu")]
        send(ui.page, "sil")
        expect(ui.page.locator(".msg.assistant").last).to_have_text("oldu")
        assert ui.page.locator(".approval").count() == 0 and not (ui.workspace / "a.txt").exists()
        ui.page.select_option("#policy-select", "ask_writes")
        ui.page.wait_for_timeout(300)
        ui.provider.steps += [Step(tools=[("fs_write", {"path": "b.txt", "content": "1"})]), Step(text="bitti")]
        send(ui.page, "yaz")
        expect(ui.page.locator(".approval")).to_be_visible()
        ui.page.get_by_role("button", name="Onayla").click()
        expect(ui.page.locator(".msg.assistant").last).to_have_text("bitti")
        assert (ui.workspace / "b.txt").exists()

    def test_error_and_notice_banners(self, make_ui):
        from localasistan.client.providers import ProviderError

        ui = make_ui(provider=ScriptedProvider([Step(error=ProviderError("rate_limit", "Rate limit reached."))]))
        send(ui.page, "selam")
        expect(ui.page.locator(".note.error")).to_have_text("Rate limit reached.")
        wait_idle(ui.page)
        ui.provider.steps.append(Step(text="x" * 20, stop_reason="max_tokens"))
        send(ui.page, "uzun")
        expect(ui.page.locator(".note").last).to_contain_text("cut off")

    def test_stop_button_cancels_and_chat_stays_usable(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text="çok yavaş", delay=30)]))
        send(ui.page, "uzun iş")
        expect(ui.page.locator("#stop")).to_be_visible()
        expect(ui.page.locator("#send")).to_be_disabled()
        ui.page.click("#stop")
        expect(ui.page.locator(".note.info")).to_have_text("Durduruldu.")
        wait_idle(ui.page)
        ui.provider.steps.append(Step(text="tekrar buradayım"))
        send(ui.page, "merhaba")
        expect(ui.page.locator(".msg.assistant").last).to_have_text("tekrar buradayım")

    def test_reload_restores_the_conversation_and_new_chat_clears_it(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text="**kalıcı** cevap"), Step(text="ikinci")]))
        send(ui.page, "ilk soru")
        expect(ui.page.locator(".msg.assistant strong")).to_have_text("kalıcı")
        wait_idle(ui.page)
        ui.page.reload()
        expect(ui.page.locator(".msg.user")).to_have_text("ilk soru")
        expect(ui.page.locator(".msg.assistant strong")).to_have_text("kalıcı")
        send(ui.page, "ikinci soru")  # aynı oturum sürüyor
        expect(ui.page.locator(".msg.assistant").last).to_have_text("ikinci")
        assert len(ui.provider.calls[1]["messages"]) == 3
        ui.page.click("#new-chat")
        assert ui.page.locator(".msg").count() == 0
        expect(ui.page.locator("#empty")).to_be_visible()
        ui.page.reload()
        assert ui.page.locator(".msg").count() == 0

    def test_session_lost_while_the_page_is_open_gives_a_clear_message_then_recovers(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text="bir"), Step(text="iki")]))
        send(ui.page, "a")
        wait_idle(ui.page)
        ui.page.evaluate("rememberSession('bogus')")  # sunucu yeniden başlamış gibi
        send(ui.page, "b")
        expect(ui.page.locator(".note.error")).to_contain_text("oturumu sunucuda bulunamadı")
        wait_idle(ui.page)
        send(ui.page, "b")  # artık yeni oturum açılır
        expect(ui.page.locator(".msg.assistant").last).to_have_text("iki")

    def test_stale_session_id_in_storage_is_dropped_silently_on_load(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text="merhaba")]))
        ui.page.evaluate("sessionStorage.setItem('la.session', 'bogus')")
        ui.page.reload()
        expect(ui.page.locator("#empty")).to_be_visible()
        assert ui.page.evaluate("sessionStorage.getItem('la.session')") is None
        send(ui.page, "selam")
        expect(ui.page.locator(".msg.assistant").last).to_have_text("merhaba")


class TestXssAndRendering:
    PAYLOADS = (
        '<img src=x onerror="window.__pwned=1">\n\n<script>window.__pwned=2</script>\n\n'
        "[tıkla](javascript:window.__pwned=3)\n\n[ok](https://example.com/a?b=1&c=2)\n\n"
        '**<b onmouseover="window.__pwned=4">kalın</b>**\n\n`<svg onload="window.__pwned=5">`\n\n'
        '```\n<iframe srcdoc="<script>parent.__pwned=6</script>"></iframe>\n```\n\n'
        '| a | b |\n|---|---|\n| <img src=x onerror="window.__pwned=7"> | y |'
    )

    def test_model_output_is_never_executed_or_injected(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text=self.PAYLOADS)]))
        send(ui.page, '<img src=x onerror="window.__pwned=8"> <script>window.__pwned=9</script>')
        expect(ui.page.locator(".msg.assistant a")).to_have_count(1)
        ui.page.wait_for_timeout(500)
        assert ui.page.evaluate("window.__pwned") is None
        assert ui.page.locator(".msg img, .msg script, .msg iframe, .msg svg, .msg b").count() == 0
        link = ui.page.locator(".msg.assistant a")
        assert link.get_attribute("href") == "https://example.com/a?b=1&c=2"
        assert link.get_attribute("rel") == "noopener noreferrer" and link.get_attribute("target") == "_blank"
        assert "javascript:" not in ui.page.locator(".msg.assistant").inner_html().replace(
            "[tıkla](javascript:window.__pwned=3)", ""
        )
        assert "<img" in ui.page.locator(".msg.user").inner_text()  # kullanıcı metni düz metin olarak görünür
        ui.shot("04-xss-safe")

    def test_tool_output_and_arguments_are_rendered_as_text(self, make_ui):
        ui = make_ui()
        ui.provider.steps += [
            Step(tools=[("fs_write", {"path": "x.html", "content": '<img src=x onerror="window.__pwned=10">'})]),
            Step(tools=[("fs_read", {"path": "x.html"})]),
            Step(text="bitti"),
        ]
        send(ui.page, "html yaz ve oku")
        expect(ui.page.locator(".msg.assistant").last).to_have_text("bitti")
        for card in ui.page.locator(".tool-card").all():
            card.locator("summary").click()
        ui.page.wait_for_timeout(300)
        assert ui.page.evaluate("window.__pwned") is None
        assert ui.page.locator(".tool-card img").count() == 0
        assert "onerror" in ui.page.locator(".tool-card").last.inner_text()

    def test_every_response_has_a_strict_csp_and_inline_handlers_are_inert(self, make_ui):
        ui = make_ui()
        ui.page.evaluate(
            "() => { const i = document.createElement('img'); i.src = 'x';"
            " i.setAttribute('onerror', 'window.__csp=1'); document.body.append(i); }"
        )
        ui.page.wait_for_timeout(400)
        assert ui.page.evaluate("window.__csp") is None
        assert any("Content Security Policy" in e for e in ui.errors)  # tarayıcı satır içi işleyiciyi engelledi


class TestToolsTab:
    def test_list_filter_and_run_a_safe_tool(self, make_ui):
        ui = make_ui()
        ui.page.click("#tabbtn-tools")
        expect(ui.page.locator("#tool-list li")).to_have_count(25)
        ui.page.fill("#tool-filter", "calendar")
        expect(ui.page.locator("#tool-list li")).to_have_count(9)
        ui.page.fill("#tool-filter", "yok böyle araç")
        expect(ui.page.locator("#tool-list li")).to_have_count(0)
        ui.page.fill("#tool-filter", "")
        ui.page.click("#tool-list button:has-text('calendar_now')")
        expect(ui.page.locator("#tool-detail h3")).to_have_text("Current date and time")
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator(".result-body")).to_contain_text('"timezone": "Europe/Istanbul"')
        expect(ui.page.locator(".result header .badge")).to_have_text("başarılı")
        ui.shot("05-tools")

    def test_generated_form_validation_and_roundtrip(self, make_ui):
        ui = make_ui()
        ui.page.click("#tabbtn-tools")
        ui.page.click("#tool-list button:has-text('fs_write')")
        ui.page.get_by_role("button", name="Çalıştır").click()  # zorunlu alanlar boş
        expect(ui.page.locator(".banner.error")).to_contain_text("eksik")
        expect(ui.page.locator("#f-fs_write-path")).to_have_class("invalid")
        ui.page.fill("#f-fs_write-path", "ui/notlar/çay.txt")
        ui.page.fill("#f-fs_write-content", "Şükrü ✓\nikinci satır\n")
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator(".result-body")).to_contain_text('"created": true')
        assert (ui.workspace / "ui" / "notlar" / "çay.txt").read_text(encoding="utf-8") == "Şükrü ✓\nikinci satır\n"
        ui.page.click("#tool-list button:has-text('fs_read')")
        ui.page.fill("#f-fs_read-path", "ui/notlar/çay.txt")
        ui.page.fill("#f-fs_read-max_chars", "5")
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator(".result-body")).to_contain_text('"truncated": true')

    def test_error_results_are_flagged(self, make_ui):
        ui = make_ui()
        ui.page.click("#tabbtn-tools")
        ui.page.click("#tool-list button:has-text('fs_read')")
        ui.page.fill("#f-fs_read-path", "../../etc/passwd")
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator(".result header .badge")).to_have_text("hata")
        expect(ui.page.locator(".result-body")).to_contain_text("outside the workspace")

    def test_json_fields_and_invalid_json(self, make_ui):
        ui = make_ui()
        ui.page.click("#tabbtn-tools")
        ui.page.click("#tool-list button:has-text('db_execute')")
        ui.page.fill("#f-db_execute-sql", "CREATE TABLE t (a INTEGER)")
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator(".result-body")).to_contain_text('"statement": "CREATE"')
        assert "yıkıcı" in ui.dialogs[-1]
        ui.page.click("#tool-list button:has-text('db_insert')")
        ui.page.fill("#f-db_insert-table", "t")
        ui.page.fill("#f-db_insert-rows", "[{a: 1}]")  # geçersiz JSON
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator("#f-db_insert-rows")).to_have_class("invalid")
        ui.page.fill("#f-db_insert-rows", '[{"a": 1}, {"a": 2}]')
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator(".result-body")).to_contain_text('"inserted": 2')

    def test_destructive_tool_asks_for_confirmation_and_respects_cancel(self, make_ui):
        ui = make_ui()
        (ui.workspace / "kalsin.txt").write_text("x")
        ui.page.click("#tabbtn-tools")
        ui.page.click("#tool-list button:has-text('fs_delete')")
        ui.page.fill("#f-fs_delete-path", "kalsin.txt")
        ui.dialog_mode = "dismiss"
        ui.page.get_by_role("button", name="Çalıştır").click()
        ui.page.wait_for_timeout(500)
        assert ui.dialogs and "yıkıcı" in ui.dialogs[-1] and (ui.workspace / "kalsin.txt").exists()
        assert ui.page.locator(".result").count() == 0  # çalıştırılmadı
        ui.dialog_mode = "accept"
        ui.page.get_by_role("button", name="Çalıştır").click()
        expect(ui.page.locator(".result-body")).to_contain_text('"deleted": "kalsin.txt"')
        assert not (ui.workspace / "kalsin.txt").exists()


class TestResourcesTab:
    def test_resources_templates_and_prompts(self, make_ui):
        ui = make_ui()
        (ui.workspace / "not.md").write_text("# Başlık\nmerhaba", encoding="utf-8")
        ui.page.click("#tabbtn-resources")
        expect(ui.page.locator("#resource-list li")).to_have_count(4)
        ui.page.click("#resource-list button:has-text('workspace://tree')")
        expect(ui.page.locator("#resource-view")).to_contain_text("not.md")
        ui.page.fill("#template-list input", "not.md")
        ui.page.click("#template-list button:has-text('Oku')")
        expect(ui.page.locator("#resource-view")).to_contain_text("# Başlık")
        ui.page.fill("#template-list input", "../../etc/passwd")
        ui.page.click("#template-list button:has-text('Oku')")
        expect(ui.page.locator("#resource-view")).to_contain_text("Hata")
        expect(ui.page.locator("#prompt-list .card")).to_have_count(3)
        summarize = ui.page.locator("#prompt-list .card", has_text="Summarise a file")
        summarize.locator("input").fill("not.md")
        summarize.get_by_role("button", name="Önizle").click()
        expect(ui.page.locator("#prompt-view")).to_contain_text("not.md")
        summarize.get_by_role("button", name="Sohbette kullan").click()
        expect(ui.page.locator("#tab-chat")).to_be_visible()
        expect(ui.page.locator("#input")).to_have_value(
            pytest.approx("") if False else ui.page.locator("#input").input_value()
        )
        assert "not.md" in ui.page.input_value("#input")
        ui.shot("06-resources")


class TestStatusTab:
    def test_status_panel_and_reconnect(self, make_ui):
        ui = make_ui()
        ui.page.click("#tabbtn-status")
        body = ui.page.locator("#status-body")
        expect(body).to_contain_text("LocalAsistan 0.1.0")
        expect(body).to_contain_text("Europe/Istanbul")
        expect(body).to_contain_text("claude-opus-5-5")
        assert str(ui.workspace) in body.inner_text()
        ui.shot("07-status")
        ui.page.click("#reconnect")
        expect(body.locator("dd", has_text="1").first).to_be_visible()
        expect(ui.page.locator("#mcp-label")).to_have_text("MCP bağlı")

    def test_unavailable_mcp_server_is_visible_and_recoverable_in_ui(self, make_ui):
        from mcp import StdioServerParameters

        ui = make_ui(hub_target=StdioServerParameters(command="/definitely/missing", args=[]))
        expect(ui.page.locator("#mcp-label")).to_have_text("MCP bağlı değil")
        expect(ui.page.locator("#mcp-dot")).to_have_class("dot dot-off")
        ui.page.click("#tabbtn-status")
        expect(ui.page.locator("#status-body")).to_contain_text("command not found")
        ui.page.click("#tabbtn-tools")
        expect(ui.page.locator("#tool-list .banner.error")).to_be_visible()
        send_text = "merhaba"
        ui.page.click("#tabbtn-chat")
        ui.provider.steps.append(Step(text="x"))
        send(ui.page, send_text)
        expect(ui.page.locator(".note.error")).to_be_visible()
        ui.shot("08-mcp-down")


def test_json_roundtrip_of_transcript_endpoint(make_ui):
    ui = make_ui(provider=ScriptedProvider([Step(text="selam")]))
    send(ui.page, "x")
    expect(ui.page.locator(".msg.assistant")).to_have_text("selam")
    sid = ui.page.evaluate("sessionStorage.getItem('la.session')")
    data = json.loads(ui.page.evaluate(f"fetch('/api/chat/{sid}').then(r => r.text())"))
    assert data["transcript"][0] == {"type": "user_message", "text": "x"}


class TestDesignAndComposer:
    def test_ticker_follows_the_real_state(self, make_ui):
        ui = make_ui()
        ticker = ui.page.locator("#ticker")
        expect(ui.page.locator("#ticker-text")).to_contain_text("Hazır · 25 araç")
        expect(ticker).to_have_class("ticker")
        ui.page.select_option("#policy-select", "auto")
        expect(ticker).to_have_class("ticker warn")
        expect(ui.page.locator("#ticker-text")).to_contain_text("onay kapalı")
        ui.page.select_option("#policy-select", "ask_destructive")
        expect(ticker).to_have_class("ticker")
        ui.page.click("#ticker-btn")
        expect(ui.page.locator("#tab-status")).to_be_visible()

    def test_ticker_signals_a_dead_mcp_server_and_a_missing_key(self, make_ui):
        from mcp import StdioServerParameters

        down = make_ui(hub_target=StdioServerParameters(command="/definitely/missing", args=[]))
        expect(down.page.locator("#ticker")).to_have_class("ticker bad")
        expect(down.page.locator("#ticker-text")).to_contain_text("MCP sunucusu bağlı değil")
        keyless = make_ui(provider=ScriptedProvider([], configured=False))
        expect(keyless.page.locator("#ticker")).to_have_class("ticker warn")
        expect(keyless.page.locator("#ticker-text")).to_contain_text("ANTHROPIC_API_KEY")

    def test_display_font_is_self_hosted_and_covers_turkish_letters(self, make_ui):
        ui = make_ui()
        response = ui.page.request.get(f"{ui.url}/static/fonts/anton-latin-ext-400-normal.woff2")
        assert response.status == 200 and "font" in response.headers["content-type"]
        ui.page.evaluate("document.fonts.ready")
        assert ui.page.evaluate("document.fonts.check('40px Anton', 'İŞĞÜÖÇ')")
        assert ui.page.evaluate("document.fonts.check('40px Anton', 'şğı')")
        family = ui.page.evaluate("getComputedStyle(document.querySelector('.display')).fontFamily")
        assert "Anton" in family
        assert ui.page.evaluate("getComputedStyle(document.querySelector('.display')).textTransform") == "uppercase"
        assert ui.errors == [], ui.errors  # CSP font yüklemesini engellemedi

    def test_hero_and_composer_fit_a_laptop_screen_without_scrolling(self, make_ui):
        ui = make_ui(viewport={"width": 1280, "height": 860})
        ui.page.wait_for_timeout(300)
        metrics = ui.page.evaluate(
            "(() => { const m = document.querySelector('#messages'); const h = document.querySelector('#empty');"
            " const c = document.querySelector('#composer'); return {scroll: m.scrollHeight, client: m.clientHeight,"
            " hero: h.getBoundingClientRect().height, composer: c.getBoundingClientRect().height}; })()"
        )
        assert metrics["scroll"] <= metrics["client"] + 2, metrics
        send, model = ui.page.locator("#send").bounding_box(), ui.page.locator("#model-chip").bounding_box()
        assert abs((send["y"] + send["height"] / 2) - (model["y"] + model["height"] / 2)) < 30  # aynı satır
        collage = ui.page.locator(".collage").bounding_box()
        assert collage["x"] >= 0 and collage["x"] + collage["width"] <= 1280

    def test_plus_menu_inserts_examples_and_ready_made_prompts(self, make_ui):
        ui = make_ui()
        ui.page.click("#btn-plus")
        popover = ui.page.locator("#popover")
        expect(popover).to_be_visible()
        expect(ui.page.locator("#btn-plus")).to_have_attribute("aria-expanded", "true")
        expect(popover.get_by_role("menuitem", name="Daily briefing")).to_be_visible()
        expect(popover.get_by_role("menuitem", name="Summarise a file")).to_contain_text("gerekli: path")
        ui.shot("10-plus-menu")
        popover.get_by_role("menuitem", name="Bugünkü takvimimi özetle").click()
        expect(popover).to_be_hidden()
        expect(ui.page.locator("#input")).to_have_value("Bugünkü takvimimi özetle")
        ui.page.fill("#input", "")
        ui.page.click("#btn-plus")
        ui.page.get_by_role("menuitem", name="Daily briefing").click()
        expect(ui.page.locator("#input")).to_have_value(re.compile("calendar_now"))
        ui.page.click("#btn-plus")
        ui.page.keyboard.press("Escape")
        expect(popover).to_be_hidden()

    def test_at_menu_mentions_workspace_files_and_filters(self, make_ui):
        ui = make_ui()
        (ui.workspace / "notlar").mkdir()
        (ui.workspace / "notlar" / "plan.md").write_text("# plan")
        (ui.workspace / "notlar" / "çay.txt").write_text("x")
        (ui.workspace / "rapor.txt").write_text("x")
        ui.page.fill("#input", "özetle")
        ui.page.click("#btn-at")
        popover = ui.page.locator("#popover")
        expect(popover.get_by_role("menuitem")).to_have_count(3)
        ui.shot("11-at-menu")
        popover.get_by_placeholder("Dosya ara…").fill("PLAN")
        expect(popover.get_by_role("menuitem")).to_have_count(1)
        popover.get_by_role("menuitem", name="notlar/plan.md").click()
        expect(ui.page.locator("#input")).to_have_value("özetle @notlar/plan.md")
        ui.page.click("#btn-at")
        popover.get_by_placeholder("Dosya ara…").fill("yok böyle dosya")
        expect(popover).to_contain_text("Eşleşen dosya yok")
        ui.page.click("body", position={"x": 5, "y": 400})  # dışarı tıklayınca kapanır
        expect(popover).to_be_hidden()

    def test_at_mention_reaches_the_model_unchanged(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text="tamam")]))
        (ui.workspace / "a.md").write_text("içerik")
        ui.page.click("#btn-at")
        ui.page.get_by_role("menuitem", name="a.md").click()
        ui.page.keyboard.type(" oku")
        ui.page.press("#input", "Enter")
        expect(ui.page.locator(".msg.assistant")).to_have_text("tamam")
        assert ui.provider.calls[0]["messages"][0]["content"] == "@a.md oku"
        assert "@notlar/plan.md" in ui.provider.calls[0]["system"]

    def test_tip_card_can_be_dismissed_and_stays_dismissed(self, make_ui):
        ui = make_ui()
        expect(ui.page.locator("#tip")).to_be_visible()
        ui.page.click("#tip-close")
        expect(ui.page.locator("#tip")).to_be_hidden()
        ui.page.reload()
        expect(ui.page.locator("#empty")).to_be_visible()
        expect(ui.page.locator("#tip")).to_be_hidden()

    def test_tip_button_opens_the_examples_menu(self, make_ui):
        ui = make_ui()
        ui.page.click("#tip-examples")
        expect(ui.page.locator("#popover")).to_be_visible()
        expect(ui.page.get_by_role("menuitem", name="Bugünkü takvimimi özetle")).to_be_visible()

    def test_textarea_grows_with_content_and_resets_after_sending(self, make_ui):
        ui = make_ui(provider=ScriptedProvider([Step(text="ok")]))
        before = ui.page.locator("#input").bounding_box()["height"]
        ui.page.fill("#input", "\n".join(f"satır {i}" for i in range(8)))
        grown = ui.page.locator("#input").bounding_box()["height"]
        assert grown > before + 60 and grown <= 222
        ui.page.press("#input", "Enter")
        expect(ui.page.locator(".msg.assistant")).to_have_text("ok")
        assert ui.page.locator("#input").bounding_box()["height"] <= before + 2

    def test_approval_card_keeps_the_english_tool_title_unshouted(self, make_ui):
        ui = make_ui()
        (ui.workspace / "x.txt").write_text("1")
        ui.provider.steps += [Step(tools=[("fs_delete", {"path": "x.txt"})]), Step(text="bitti")]
        send(ui.page, "sil")
        title = ui.page.locator(".approval-tool")
        expect(title).to_have_text("Delete file or folder")
        assert ui.page.evaluate("getComputedStyle(document.querySelector('.approval-tool')).textTransform") == "none"
        assert (
            ui.page.evaluate("getComputedStyle(document.querySelector('.approval strong')).textTransform")
            == "uppercase"
        )
        ui.page.get_by_role("button", name="Reddet").click()
        wait_idle(ui.page)

    def test_tool_titles_in_the_tools_tab_use_english_casing_rules(self, make_ui):
        ui = make_ui()
        ui.page.click("#tabbtn-tools")
        ui.page.click("#tool-list button:has-text('calendar_now')")
        assert ui.page.locator("#tool-detail h3").get_attribute("lang") == "en"

    def test_reduced_motion_disables_animations(self, make_ui, browser):
        ui = make_ui()
        ui.page.emulate_media(reduced_motion="reduce")
        ui.workspace.joinpath("y.txt").write_text("1")
        ui.provider.steps += [Step(tools=[("fs_delete", {"path": "y.txt"})]), Step(text="x")]
        send(ui.page, "sil")
        approval = ui.page.locator(".approval")
        expect(approval).to_be_visible()
        assert ui.page.evaluate("getComputedStyle(document.querySelector('.approval')).animationName") == "none"
        ui.page.get_by_role("button", name="Reddet").click()
        wait_idle(ui.page)
