# Geliştirme Planı — Kendi MCP Sunucusu ve İstemcisi (#37)

> **Hedef:** Yerel dosya, takvim ve veritabanı araçlarını bir LLM'e (Claude) Model Context Protocol
> üzerinden açan, **kendi yazdığımız** bir MCP sunucusu + MCP istemcisi + basit bir web arayüzü.
> Mac'te tek komutla çalışmalı, dış servis gerektirmemeli (LLM API anahtarı hariç).

## 1. Kapsam

### Kapsam içi
| Alan | İçerik |
|---|---|
| **MCP sunucusu** (`localasistan.server`) | stdio taşıması; 25 araç (dosya 11, veritabanı 5, takvim 9); 5 kaynak/şablon; 3 prompt; araç `annotations` (readOnly / destructive / idempotent) |
| **MCP istemcisi** (`localasistan.client`) | Sunucuyu alt süreç olarak başlatma, el sıkışma, `tools/resources/prompts` keşfi, çağrı, yeniden bağlanma, hata/zaman aşımı yönetimi |
| **Ajan döngüsü** | Claude (Anthropic SDK) ile akışlı (streaming) araç-kullanım döngüsü; insan onayı (human-in-the-loop); `refusal`, `max_tokens`, iptal, yineleme sınırı |
| **Web arayüzü** | Sohbet, araç denetçisi (LLM'siz elle çağırma), kaynaklar/promptlar, durum paneli. Bağımlılıksız (build adımı yok) |
| **CLI** | `ui`, `server`, `tools`, `call`, `chat`, `seed`, `doctor`, `config` |
| **QA** | Birim + entegrasyon + stdio E2E + HTTP E2E + tarayıcı E2E + güvenlik + eşzamanlılık testleri |

### Kapsam dışı (bilinçli)
* Tekrarlayan (RRULE) takvim olayları — `.ics` içe aktarımında atlanır ve raporlanır.
* macOS Calendar.app / EventKit doğrudan entegrasyonu — bunun yerine `.ics` içe/dışa aktarım.
* Çok kullanıcılı / uzak erişim, kimlik doğrulama — arayüz yalnızca `127.0.0.1`'e bağlanır.
* Birden çok MCP sunucusu birleştirme — istemci tek sunucuya bağlanır (mimari genişletmeye açık).

## 2. Mimari

```
┌────────────────────────── Tarayıcı (vanilla JS SPA) ──────────────────────────┐
│  Sohbet · Araçlar · Kaynaklar/Promptlar · Durum                               │
└───────────────▲───────────────────────────────────────────────────────────────┘
                │ HTTP/JSON + SSE (yalnızca 127.0.0.1, Host/Origin denetimli)
┌───────────────┴──────────── localasistan ui (tek süreç) ──────────────────────┐
│  web/        Starlette uygulaması                                             │
│  client/     ChatSession (ajan döngüsü) ── AnthropicProvider ──► Claude API   │
│              McpHub (yaşam döngüsü, yeniden bağlanma) ── ToolCatalog         │
└───────────────▲───────────────────────────────────────────────────────────────┘
                │ MCP (JSON-RPC, stdio)  — alt süreç: python -m localasistan.server
┌───────────────┴──────────── localasistan.server ──────────────────────────────┐
│  FileService (sandbox)   DatabaseService (SQLite)   CalendarService (SQLite)  │
│  workspace/ klasörü      userdata.db                calendar.db  (+ ICS)      │
└───────────────────────────────────────────────────────────────────────────────┘
```

Ayrıca sunucu **bağımsız** bir MCP sunucusudur: Claude Desktop vb. başka istemcilere de
bağlanabilir (bkz. README).

### Temel teknik kararlar
1. **Servis katmanı + ince MCP sarmalayıcı.** İş mantığı (`FileService`, `DatabaseService`,
   `CalendarService`) MCP'den bağımsız saf Python; araçlar yalnızca sarmalayıcı. Böylece birim
   testler hızlı, entegrasyon testleri protokol seviyesinde.
2. **Güvenlik derinlemesine.** Dosya: kök-hapsi (symlink/`..`/mutlak yol/NUL baytı). Veritabanı:
   salt-okunur bağlantı + `sqlite3` authorizer + tek-ifade zorunluluğu + zaman/satır limiti +
   `ATTACH/PRAGMA/VACUUM` yasağı; takvim DB'si LLM SQL'inden ayrı dosyada. Web: Host/Origin
   doğrulama (DNS-rebinding/CSRF), zorunlu `application/json`, yalnızca loopback.
3. **Yıkıcı işlemler için onay.** MCP `annotations` bilgisi istemcide onay politikasına
   çevrilir (`ask_destructive` varsayılan). Yazma ve üzerine-yazma araçları bilerek ayrıdır
   (`fs_write` yalnızca yeni dosya; `fs_overwrite` yıkıcı).
4. **Prompt-injection savunması.** Dosya/DB/takvim içeriği "veri"dir; sistem istemi bunu söyler,
   yıkıcı işlemler onaya bağlıdır, araç çıktıları boyut sınırlıdır.
5. **Saat dilimi doğruluğu.** Olaylar UTC saklanır, kullanıcının IANA dilimiyle gösterilir;
   `calendar_now` aracı göreli tarihleri ("yarın") çözmek için tek doğruluk kaynağıdır
   (sistem istemi zamansız kalır → prompt cache bozulmaz).
6. **LLM sağlayıcı soyutlaması.** Ajan döngüsü sağlayıcıdan bağımsız; testlerde deterministik
   `ScriptedProvider`, üretimde `AnthropicProvider` (gerçek SDK + `MockTransport` ile ayrıca
   doğrulanır).
7. **mcp 2.x API'si.** `mcp.server.mcpserver.MCPServer` ve `mcp.Client` kullanılır
   (v1'deki `FastMCP` kaldırıldı). anyio bağlam yöneticileri aynı görevde girilip çıkılır →
   `McpHub` bağlantıyı tek bir arka plan görevinde yönetir.

## 3. Fazlar

Her faz: **çıktı → kabul kriteri → ayrı commit.**

### Faz 0 — İskelet ve plan
* `pyproject.toml`, `Makefile`, `.gitignore`, `.env.example`, bu plan.
* **Kabul:** `pip install -e ".[dev]"` ve `pytest` (boş) çalışır; ruff temiz.

### Faz 1 — Çekirdek
* `config` (ortam + `.env`, saat dilimi algılama), `errors`, stderr-only `logging`, `sandbox`.
* **Kabul:** Sandbox; `..`, mutlak yol, symlink kaçışı, NUL, Unicode (Türkçe karakter, NFC/NFD)
  durumlarında doğru davranır. Config birim testleri geçer.

### Faz 2 — MCP sunucusu
* 2a Dosya servisi + 11 araç · 2b Veritabanı servisi + 5 araç · 2c Takvim servisi + ICS + 10 araç
  · 2d Resources/Prompts + `python -m localasistan.server` + `seed`.
* **Kabul:** Servis birim testleri; `Client(server)` ile süreç-içi ve gerçek stdio üzerinden
  araç listesi/çağrı testleri; hatalar `is_error=true` ile okunur mesaj döner (yığın izi sızmaz).

### Faz 3 — MCP istemcisi
* `McpHub` (başlat/durdur/yeniden bağlan, zaman aşımı, eşzamanlı çağrılar), `ToolCatalog`
  (MCP → Anthropic araç şeması, onay politikası), sonuç normalizasyonu.
* **Kabul:** Sunucu öldürülünce hub sonraki çağrıda toparlanır; zaman aşımı/sözleşme ihlali
  anlaşılır hata verir; tüm sunucu yetenekleri (tools/resources/prompts) erişilebilir.

### Faz 4 — Ajan döngüsü
* `ChatSession`, olay akışı, `AnthropicProvider` (akış, adaptive thinking, `eager_input_streaming`,
  `refusal`/`max_tokens`/`pause_turn`, sunucu tarafı fallback), onay akışı, iptal ve geçmiş
  onarımı, yineleme sınırı.
* **Kabul:** `ScriptedProvider` ile tüm döngü senaryoları; gerçek SDK + sahte HTTP taşıması ile
  istek biçimi (başlıklar, gövde) ve SSE ayrıştırma doğrulanır.

### Faz 5 — Web arayüzü
* Starlette API (`/api/status|tools|resources|prompts|chat|…`), SSE sohbet, onay uçları,
  güvenlik katmanı, statik SPA.
* **Kabul:** API testleri (TestClient); tarayıcıda sohbet → araç çağrısı → onay → sonuç akışı.

### Faz 6 — CLI, Mac çalıştırma, dokümantasyon
* `localasistan` CLI, `scripts/setup.sh`, `scripts/run.sh`, `.command` başlatıcı, README,
  Claude Desktop yapılandırma örneği, `doctor`.
* **Kabul:** Temiz bir ortamda `scripts/setup.sh && scripts/run.sh` ile arayüz açılır.

### Faz 7 — QA
* Test matrisi (aşağıda) eksiksiz uygulanır; kapsama ölçülür; bulunan hatalar düzeltilir;
  `docs/QA.md` raporu yazılır.
* **Kabul:** Tüm testler yeşil; kritik modüllerde (sandbox, SQL guard, calendar, agent loop)
  yüksek kapsama; bilinen sınırlar belgelenmiş.

### Faz 8 — Yayın
* Commit geçmişi kontrolü (`Co-Authored-By` / otomatik imza satırı **yok**), feature branch push.

## 4. Test stratejisi ve matrisi (özet)

| Katman | Ne test edilir | Araç |
|---|---|---|
| Birim | Sandbox, FileService, SQL guard/DatabaseService, tarih ayrıştırma, çakışma, boş-slot, ICS, config | pytest |
| Protokol (süreç-içi) | `Client(MCPServer)`: tools/resources/prompts/annotations/hata sonuçları | pytest-asyncio |
| Protokol (gerçek stdio) | Alt süreç, el sıkışma, Türkçe karakter, büyük çıktı, sunucu çökmesi + toparlanma, zombi süreç yok | pytest-asyncio |
| Ajan | Metin, tek/çoklu araç, hata sonucu, onay (evet/hayır/zaman aşımı), iptal, `max_tokens`, `refusal`, `pause_turn`, geçersiz girdi, yineleme sınırı, geçmiş tutarlılığı | ScriptedProvider |
| Sağlayıcı | Gerçek Anthropic SDK + `MockTransport`: istek gövdesi/başlıklar, SSE ayrıştırma, hata eşlemesi | pytest |
| Web API | Tüm uçlar, SSE akışı, onay uçları, Host/Origin/Content-Type reddi, hatalı girdiler | Starlette TestClient |
| Tarayıcı E2E | Sohbet, onay kartı, araç formu, kaynaklar/promptlar, XSS (çıktı kaçışı), yeniden bağlanma | Playwright (Chromium) |
| Süreç E2E | Gerçek `localasistan ui` süreci, durdurmada alt sürecin temizlenmesi | subprocess |
| Güvenlik | Path traversal/symlink, SQL enjeksiyon/ATTACH/PRAGMA/VACUUM INTO, DNS-rebinding, CSRF, XSS | pytest + Playwright |
| Eşzamanlılık | Takvimde yarış (çift rezervasyon), paralel araç çağrıları, WAL/kilit | pytest |

## 5. Riskler

| Risk | Önlem |
|---|---|
| mcp 2.x API'si v1'den farklı | Kurulu sürümün kaynağı okundu, prototip doğrulandı; sürüm `>=2.3,<3` sabitlendi |
| anyio iptal kapsamı farklı görevden çıkılırsa bozulur | Hub bağlantıyı tek görevde yönetir |
| Canlı LLM çağrısı bu geliştirme ortamında yok (API anahtarı yok) | Sağlayıcı gerçek SDK ile sahte HTTP üzerinde doğrulanır; `localasistan doctor --llm` Mac'te canlı doğrulama sağlar |
| Mac'e özgü davranış (APFS Unicode, büyük/küçük harf, `/tmp` → `/private/tmp`) | Sandbox `resolve()` + NFC normalizasyonu; testler Türkçe dosya adları içerir |
| LLM'in yıkıcı işlem yapması | Onay politikası + yıkıcı/yazma araç ayrımı + sandbox + SQL guard |
