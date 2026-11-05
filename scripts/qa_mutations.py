#!/usr/bin/env python3
"""Mutasyon denetimi: güvenlik/doğruluk açısından kritik koda kasıtlı hata sokar ve testlerin BAŞARISIZ olduğunu doğrular.

Kaynak ağacının bir KOPYASI üzerinde çalışır (depo değişmez). Her mutasyon için ilgili test alt kümesi çalıştırılır;
test başarısız olursa mutasyon "öldürülmüş" (iyi), tüm testler geçerse "hayatta" (testler o korumayı görmüyor demektir).

    .venv/bin/python scripts/qa_mutations.py            # hepsi
    .venv/bin/python scripts/qa_mutations.py M01 M05    # seçilenler
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


@dataclass
class Mutation:
    id: str
    what: str
    file: str
    old: str
    new: str
    tests: list[str]


M = Mutation
MUTATIONS = [
    M("M01", "Sandbox: kök-içi denetimi kaldırıldı", "localasistan/sandbox.py",
      "if resolved != self.root and self.root not in resolved.parents:", "if False:",
      ["tests/unit/test_sandbox.py", "tests/unit/test_properties.py::test_sandbox_never_resolves_outside_the_root"]),
    M("M02", "Sandbox: follow_final=False yolu parent çözümlemesini atlıyor", "localasistan/sandbox.py",
      "resolved = candidate.parent.resolve() / candidate.name", "resolved = candidate.absolute()",
      ["tests/unit/test_sandbox.py", "tests/unit/test_files.py"]),
    M("M03", "FileService: fs_write mevcut dosyanın üzerine yazabiliyor", "localasistan/server/files.py",
      "            if os.path.lexists(resolved):\n                raise AlreadyExistsError(f\"'{shown}' already exists. Use fs_overwrite to replace it.\")",
      "            pass",
      ["tests/unit/test_files.py"]),
    M("M04", "FileService: silme sembolik linki değil hedefi izliyor", "localasistan/server/files.py",
      "resolved, shown = self._existing(path, follow_final=False)\n        if self.sandbox.is_root(resolved):\n            raise InvalidInputError(\"Cannot delete",
      "resolved, shown = self._existing(path)\n        if self.sandbox.is_root(resolved):\n            raise InvalidInputError(\"Cannot delete",
      ["tests/unit/test_files.py"]),
    M("M05", "SQL: salt-okunur bağlantının authorizer'ı her şeye izin veriyor", "localasistan/server/database.py",
      "            return _OK\n        return _DENY\n\n    @staticmethod\n    def _make_write_authorizer",
      "            return _OK\n        return _OK\n\n    @staticmethod\n    def _make_write_authorizer",
      ["tests/unit/test_database.py", "tests/unit/test_properties.py::test_query_never_modifies_the_database_and_only_raises_domain_errors"]),
    M("M06", "SQL: db_execute ilk-anahtar-kelime beyaz listesi kapalı (VACUUM INTO vb. geçer)", "localasistan/server/database.py",
      "        if keyword not in _EXECUTE_KEYWORDS:\n", "        if False:\n",
      ["tests/unit/test_database.py", "tests/unit/test_properties.py::test_execute_never_escapes_the_database_file_and_only_raises_domain_errors"]),
    M("M07", "SQL: yazma authorizer'ı ATTACH'e izin veriyor", "localasistan/server/database.py",
      "            if action in _DENIED_WRITE_ACTIONS:\n                return _DENY", "            if False:\n                return _DENY",
      ["tests/unit/test_database.py"]),
    M("M08", "SQL: zaman sınırı (progress handler) kaldırıldı", "localasistan/server/database.py",
      "conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)", "pass",
      ["tests/unit/test_database.py"]),
    M("M09", "Takvim: çakışma denetimi hiçbir şey bulmuyor", "localasistan/server/calendar.py",
      "        return [self._row_to_dict(r) for r in rows]\n\n    @staticmethod\n    def _conflict_error",
      "        return []\n\n    @staticmethod\n    def _conflict_error",
      ["tests/unit/test_calendar.py", "tests/integration/test_scenarios.py"]),
    M("M10", "Takvim: yerel gece yarısı UTC sanılıyor (tüm-gün olaylar kayar)", "localasistan/server/calendar.py",
      "return datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(UTC)", "return datetime.combine(day, time(0, 0), tzinfo=UTC)",
      ["tests/unit/test_calendar.py"]),
    M("M11", "Takvim: çakışma kontrolü işlem dışına alındı (BEGIN IMMEDIATE yok)", "localasistan/server/calendar.py",
      'conn.execute("BEGIN IMMEDIATE")', 'conn.execute("BEGIN")',
      ["tests/unit/test_calendar.py::TestConflicts::test_parallel_booking_of_same_slot_has_one_winner"]),
    M("M12", "Onay: hiçbir araç onay istemiyor", "localasistan/client/catalog.py",
      "    if policy == \"auto\" or spec.read_only:\n        return False", "    if True:\n        return False",
      ["tests/unit/test_catalog.py", "tests/integration/test_agent.py"]),
    M("M13", "Onay: bilinmeyen politika 'auto' gibi davranıyor", "localasistan/client/catalog.py",
      "        policy = \"ask_destructive\"  # bilinmeyen politika => güvenli taraf", "        policy = \"auto\"",
      ["tests/unit/test_catalog.py"]),
    M("M14", "Ajan: araç girdisi şemaya karşı doğrulanmıyor", "localasistan/client/agent.py",
      "problem = _validation_message(spec, tu.input)", "problem = None",
      ["tests/integration/test_agent.py"]),
    M("M15", "Ajan: max_tokens ile kesilen turdaki araçlar çalıştırılıyor", "localasistan/client/agent.py",
      "if turn.stop_reason == \"max_tokens\" and turn.tool_uses:", "if False:",
      ["tests/integration/test_agent.py"]),
    M("M16", "Ajan: iptalde eksik tool_result tamamlanmıyor", "localasistan/client/agent.py",
      "                    if tu.id not in done:", "                    if False:",
      ["tests/integration/test_agent.py"]),
    M("M17", "Hub: yan etkili araçlar da otomatik yeniden deneniyor", "localasistan/client/hub.py",
      "attempts = 2 if read_only else 1", "attempts = 2",
      ["tests/integration/test_hub.py::TestFailureModes::test_crash_during_write_call_is_never_retried"]),
    M("M18", "Web: Host denetimi her şeyi kabul ediyor (DNS rebinding)", "localasistan/web/security.py",
      "return host in self.hosts and (port is None or port == self.port)", "return True",
      ["tests/unit/test_security.py", "tests/integration/test_web_api.py::TestSecurity"]),
    M("M19", "Web: Origin denetimi kaldırıldı (CSRF)", "localasistan/web/security.py",
      "if origin == \"null\" or parsed.scheme not in (\"http\", \"https\") or origin_host != host.strip().lower():", "if False:",
      ["tests/integration/test_web_api.py::TestSecurity"]),
    M("M20", "Web: application/json zorunluluğu kaldırıldı (CSRF)", "localasistan/web/security.py",
      "if content_type != \"application/json\":", "if False:",
      ["tests/integration/test_web_api.py::TestSecurity"]),
    M("M21", "Hub/Provider: API anahtarı alt sürece aktarılıyor", "localasistan/config.py",
      '"LOCALASISTAN_LOG_LEVEL": self.log_level,', '"LOCALASISTAN_LOG_LEVEL": self.log_level, "ANTHROPIC_API_KEY": self.api_key or "",',
      ["tests/unit/test_config.py"]),
    M("M22", "Provider: thinking bloğu geçmişe geri yazılmıyor", "localasistan/client/providers.py",
      "content = list(final.content)", "content = [b for b in final.content if getattr(b, 'type', '') != 'thinking']",
      ["tests/integration/test_anthropic_provider.py"]),
    M("M23", "Provider: Opus 5.5'te 400 üreten sabit tool_choice gönderiliyor", "localasistan/client/providers.py",
      '"output_config": {"effort": s.effort},', '"output_config": {"effort": s.effort}, "tool_choice": {"type": "any"},',
      ["tests/integration/test_anthropic_provider.py"]),
    M("M24", "Arayüz: escapeHtml etkisiz (XSS)", "localasistan/web/static/app.js",
      'return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/\'/g, "&#39;");',
      "return s;",
      ["tests/e2e/test_ui.py::TestXssAndRendering::test_model_output_is_never_executed_or_injected"]),
    M("M25", "Arayüz: javascript: bağlantıları çiziliyor (XSS)", "localasistan/web/static/app.js",
      "\\((https?:\\/\\/[^\\s)<>\"']+)\\)", "\\(([^\\s)<>\"']+)\\)",
      ["tests/e2e/test_ui.py::TestXssAndRendering::test_model_output_is_never_executed_or_injected"]),
    M("M26", "SSE: kopmada üreteç kapatılmıyor", "localasistan/web/sse.py",
      "                if close is not None:\n                    await close()", "                pass",
      ["tests/unit/test_sse.py"]),
    M("M27", "ICS: metin kaçışı yapılmıyor", "localasistan/server/ics.py",
      "def escape_text(value: str) -> str:\n    return (", "def escape_text(value: str) -> str:\n    return value or (",
      ["tests/unit/test_ics.py", "tests/unit/test_properties.py::test_ics_roundtrip_preserves_text"]),
    M("M28", "Takvim: boş zaman geçmiş saatleri de döndürüyor", "localasistan/server/calendar.py",
      "window_start = max(window_start, floor)", "window_start = window_start",
      ["tests/unit/test_calendar.py"]),
    M("M29", "OpenAI uyumlu: araç sonuçları `tool` yerine `user` mesajı olarak gidiyor", "localasistan/client/openai_compat.py",
      '{"role": "tool", "tool_call_id": _field(block, "tool_use_id"), "content": body}',
      '{"role": "user", "tool_call_id": _field(block, "tool_use_id"), "content": body}',
      ["tests/integration/test_openai_compat_provider.py", "tests/integration/test_openai_compat_flow.py"]),
    M("M30", "OpenAI uyumlu: bitiş işareti olmayan (kopmuş) akış sessizce kabul ediliyor", "localasistan/client/openai_compat.py",
      "if finish is None and not saw_done:", "if False:",
      ["tests/integration/test_openai_compat_provider.py"]),
    M("M31", "OpenAI uyumlu: kesilmiş (length) araç çağrısı JSON hatası sayılıyor", "localasistan/client/openai_compat.py",
      'if finish == "length":', "if False:",
      ["tests/integration/test_openai_compat_provider.py", "tests/integration/test_openai_compat_flow.py"]),
    M("M32", "OpenAI uyumlu: 4xx istemci hataları da yeniden deneniyor", "localasistan/client/openai_compat.py",
      "if isinstance(error, _RetryableError) and attempt < len(self._retry_delays):", "if attempt < len(self._retry_delays):",
      ["tests/integration/test_openai_compat_provider.py"]),
    M("M33", "OpenAI uyumlu: Retry-After üst sınırı yok (saatlerce beklenebilir)", "localasistan/client/openai_compat.py",
      "return max(0.0, min(float(raw), _MAX_RETRY_AFTER))", "return max(0.0, float(raw))",
      ["tests/integration/test_openai_compat_provider.py"]),
    M("M34", "Config: düz http ile uzak sunucuya API anahtarı gidebiliyor", "localasistan/config.py",
      'if parts.scheme == "http" and has_key and not _is_loopback(parts.hostname):', "if False:",
      ["tests/unit/test_config.py"]),
    M("M35", "Config: openai_compat anahtarı MCP alt sürecine aktarılıyor", "localasistan/config.py",
      '"LOCALASISTAN_LOG_LEVEL": self.log_level,', '"LOCALASISTAN_LOG_LEVEL": self.log_level, "LOCALASISTAN_API_KEY": self.api_key or "",',
      ["tests/unit/test_config.py"]),
    M("M36", "OpenAI uyumlu: 401 hata mesajına API anahtarı sızıyor", "localasistan/client/openai_compat.py",
      'f"The server rejected the API key (HTTP 401). Check LOCALASISTAN_API_KEY. {detail}"',
      'f"The server rejected the API key (HTTP 401). Check LOCALASISTAN_API_KEY. {detail} {settings.api_key}"',
      ["tests/integration/test_openai_compat_provider.py"]),
]  # fmt: skip


def run_pytest(tree: Path, tests: list[str], timeout: int = 120) -> tuple[int, str]:
    """(çıkış kodu, çıktı kuyruğu). Zaman aşımı (test takıldı) 124 kodu ile döner: mutasyon testlerce görüldü sayılır."""
    env = {**os.environ, "PYTHONPATH": str(tree / "src")}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", *tests],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return 124, "zaman aşımı: testler takıldı"
    return proc.returncode, proc.stdout[-400:]


def main(selected: list[str]) -> int:
    chosen = [m for m in MUTATIONS if not selected or m.id in selected]
    survivors: list[Mutation] = []
    print(f"{'ID':<5} {'Sonuç':<10} {'Süre':>6}  Açıklama", flush=True)
    with tempfile.TemporaryDirectory(prefix="la-mut-") as tmp:
        base = Path(tmp) / "baseline"
        shutil.copytree(SRC, base / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        probe = subprocess.run(
            [sys.executable, "-c", "import localasistan; print(localasistan.__file__)"],
            env={**os.environ, "PYTHONPATH": str(base / "src")}, capture_output=True, text=True, check=False,
        )  # fmt: skip
        if not probe.stdout.strip().startswith(str(base)):
            print(f"HATA: mutasyon kopyası içe aktarılmıyor ({probe.stdout.strip()!r}); sonuçlar geçersiz olurdu.")
            return 2
        code, tail = run_pytest(base, ["tests/unit/test_sandbox.py", "tests/unit/test_textutil.py"])
        if code != 0:
            print(f"HATA: mutasyonsuz taban çizgisi başarısız:\n{tail}")
            return 2
        print("taban çizgisi: kopya içe aktarılıyor ve testler geçiyor\n")
        for mutation in chosen:
            tree = Path(tmp) / mutation.id
            shutil.copytree(SRC, tree / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            target = tree / "src" / mutation.file
            text = target.read_text(encoding="utf-8")
            if text.count(mutation.old) != 1:
                print(
                    f"{mutation.id:<5} {'GEÇERSİZ':<10} {'-':>6}  mutasyon hedefi bulunamadı/çoklu ({text.count(mutation.old)}): {mutation.what}"
                )
                survivors.append(mutation)
                continue
            target.write_text(text.replace(mutation.old, mutation.new), encoding="utf-8")
            started = time.monotonic()
            code, tail = run_pytest(tree, mutation.tests)
            elapsed = time.monotonic() - started
            killed = code != 0
            label = "ÖLDÜRÜLDÜ" if killed else "HAYATTA"
            note = " (testler takıldı)" if code == 124 else ""
            print(f"{mutation.id:<5} {label:<10} {elapsed:>5.1f}s  {mutation.what}{note}", flush=True)
            if not killed:
                survivors.append(mutation)
            shutil.rmtree(tree, ignore_errors=True)
    print(f"\n{len(chosen) - len(survivors)}/{len(chosen)} mutasyon yakalandı.")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
