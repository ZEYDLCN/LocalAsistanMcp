# QA Raporu

Bu belge, projenin nasıl test edildiğini, **neyin otomatik doğrulandığını**, geliştirme sırasında testlerin yakaladığı **gerçek hataları**
ve **bu ortamda doğrulanamayan** (Mac'te sizin yapmanız gereken) maddeleri içerir. Rakamlar `make test`, `make cov` ve
`scripts/qa_mutations.py` çıktılarındandır.

## 1. Özet

| Ölçüt | Sonuç |
|---|---|
| Otomatik test sayısı | **827 geçti**, 1 atlandı (root kullanıcısında anlamsız olan dosya-izni testi) — tarayıcı E2E dahil tam paket |
| Satır+dal kapsaması (alt süreçler dahil) | **%97** (3 051 satır / 900 dal; takvim %99, sunucu `app.py` %100, web `security.py` %100) |
| Mutasyon denetimi (kritik korumalara kasıtlı hata) | **28/28 yakalandı** |
| Desteklenen Python | **3.13 · 3.12 · 3.11: üçünde de aynı tam paket** (her birinde 827 geçti, 1 atlandı) |
| Tarayıcı | Chromium (Playwright), **40 senaryo** (27 işlevsel + 13 tasarım/menü/erişilebilirlik) |
| MCP uyumluluğu | mcp 2.x istemcisi (yeni + eski el sıkışma) ve **mcp 1.30** istemcisi ile doğrulandı |
| Lint | `ruff check` + `ruff format --check` temiz |

> **Dürüst sınır:** Geliştirme ortamında gerçek bir Anthropic API anahtarı ve macOS yoktu. LLM katmanı, gerçek SDK + sahte HTTP ile
> (istek gövdesi, başlıklar, SSE ayrıştırma, düşünme bloklarının geri yansıtılması) doğrulandı; ilk **canlı** doğrulama için
> `localasistan doctor --llm` komutu hazırdır. macOS'a özgü davranışlar için §6'daki liste kullanılmalıdır.

## 2. Test katmanları

| Katman | Dosyalar | Ne doğrular |
|---|---|---|
| Birim — servisler | `tests/unit/test_{sandbox,files,database,calendar,ics,config,catalog,textutil}.py` | Sandbox (symlink, `..`, mutlak yol, NUL, Unicode), dosya işlemleri, SQL koruma katmanları, takvim/DST/çakışma/boş-zaman, ICS, yapılandırma, onay matrisi |
| Özellik-tabanlı | `tests/unit/test_properties.py` (hypothesis) | **Rastgele** girdilerde değişmezler: sandbox asla dışarı çıkmaz; hiçbir dosya işlem dizisi workspace dışına dokunamaz; `db_query` DB dosyasını bayt düzeyinde değiştirmez; boş-zaman = tam olarak çalışma saatlerinin boş aralıkları; ICS ayrıştırıcı asla çökmez |
| Protokol (süreç-içi) | `tests/integration/test_server_inprocess.py` | 25 aracın şeması/annotation sözleşmesi (okuma/yazma/yıkıcı sınıflaması **tam eşleşme**), uçtan uca araç akışları, hata sonuçları, kaynak/prompt, yol kaçışı denemeleri |
| Protokol (gerçek stdio) | `test_server_stdio.py`, `test_hub.py` | Gerçek alt süreç: kalıcılık, 100 KB yük, paralel çağrılar, **süreçler arası çift-rezervasyon yarışı**, geçersiz yapılandırma çıkış kodu, EOF ile kapanış, eski el sıkışma, sunucu çökmesi/zaman aşımı/toparlanma, yetim süreç yok |
| Ajan döngüsü | `test_agent.py` | Sahte LLM: araç turları, onay (evet/hayır/zaman aşımı), şema doğrulama, `max_tokens`/`refusal`/`pause_turn`, yineleme sınırı, **iptalde geçmişin API-geçerli kalması**, oturum yönetimi |
| LLM sağlayıcısı | `test_anthropic_provider.py` | **Gerçek Anthropic SDK + sahte HTTP**: istek gövdesi (Opus 5.5 kuralları: `temperature`/`tool_choice`/`budget_tokens` yok), başlıklar, SSE ayrıştırma, hata eşleme, thinking bloklarının aynen geri yazılması |
| Web API + güvenlik | `test_web_api.py`, `tests/unit/test_{security,sse}.py` | Tüm uçlar, Host/Origin/Content-Type reddi (DNS rebinding, CSRF), CORS yok, CSP, hata sızıntısı yok, MCP kapalıyken ayakta kalma |
| Gerçek HTTP akışı | `test_web_stream.py` | Gerçek uvicorn: SSE, onay uçları, **bağlantı kopması temizliği**, eşzamanlı oturumlar, sunucu kapanışı |
| Senaryolar | `test_scenarios.py` | Toplantı planlama (boş zaman → rezervasyon → çakışma → alternatif), gider analizi, dosya düzenleme, ICS yedekle-sil-geri yükle, **prompt-injection**, yarış |
| CLI / süreç | `test_cli.py` | Tüm komutlar, gerçek `ui` süreci, SIGINT/SIGTERM ile temiz kapanış, yetim süreç yok |
| Tarayıcı E2E | `tests/e2e/test_ui.py` | Sohbet akışı, markdown, onay kartları, durdurma, yeniden yükleme, araç formları, kaynaklar, durum, **XSS yükleri**, mobil taşma. Tasarım: durum şeridi durumları, yerel yazı tipinin yüklenmesi ve Türkçe harfleri kapsaması, karşılama ekranının 1280×860'a **kaydırmasız** sığması, `+` ve `@` menüleri, `@dosya` anmasının modele aynen ulaşması, ipucu kartının kalıcı kapatılması, mesaj kutusu otomatik boyutu, onay başlığı büyük/küçük harf, `prefers-reduced-motion` |
| Performans | `test_performance.py` | 3 000 dosya, 1 500 olay, 1 000 satır, 200 hub çağrısı için bütçeler |
| Doküman tutarlılığı | `tests/unit/test_docs.py` | README sayıları/bağlantıları/araç tablosu koddan sapmasın |

Çalıştırma:

```bash
make test        # tarayıcı hariç
make e2e         # Chromium (ilk kez: .venv/bin/playwright install chromium)
make cov         # kapsama (alt süreçler dahil)
.venv/bin/python scripts/qa_mutations.py   # mutasyon denetimi
```

## 3. Mutasyon denetimi

`scripts/qa_mutations.py`, kaynağın bir **kopyasında** kritik koruma/doğruluk satırlarını bozar ve ilgili testlerin başarısız olduğunu doğrular
(başarısız olmazsa o koruma fiilen test edilmiyor demektir).

| ID | Mutasyon (kasıtlı hata) | Sonuç |
|---|---|---|
| M01 | Sandbox: kök-içi denetimi kaldırıldı | ✅ yakalandı |
| M02 | Sandbox: follow_final=False yolu parent çözümlemesini atlıyor | ✅ yakalandı |
| M03 | FileService: fs_write mevcut dosyanın üzerine yazabiliyor | ✅ yakalandı |
| M04 | FileService: silme sembolik linki değil hedefi izliyor | ✅ yakalandı |
| M05 | SQL: salt-okunur bağlantının authorizer'ı her şeye izin veriyor | ✅ yakalandı |
| M06 | SQL: db_execute ilk-anahtar-kelime beyaz listesi kapalı (VACUUM INTO vb. geçer) | ✅ yakalandı |
| M07 | SQL: yazma authorizer'ı ATTACH'e izin veriyor | ✅ yakalandı |
| M08 | SQL: zaman sınırı (progress handler) kaldırıldı | ✅ yakalandı (testler takıldı → zaman aşımı) |
| M09 | Takvim: çakışma denetimi hiçbir şey bulmuyor | ✅ yakalandı |
| M10 | Takvim: yerel gece yarısı UTC sanılıyor (tüm-gün olaylar kayar) | ✅ yakalandı |
| M11 | Takvim: çakışma kontrolü işlem dışına alındı (BEGIN IMMEDIATE yok) | ✅ yakalandı |
| M12 | Onay: hiçbir araç onay istemiyor | ✅ yakalandı |
| M13 | Onay: bilinmeyen politika 'auto' gibi davranıyor | ✅ yakalandı |
| M14 | Ajan: araç girdisi şemaya karşı doğrulanmıyor | ✅ yakalandı |
| M15 | Ajan: max_tokens ile kesilen turdaki araçlar çalıştırılıyor | ✅ yakalandı |
| M16 | Ajan: iptalde eksik tool_result tamamlanmıyor | ✅ yakalandı |
| M17 | Hub: yan etkili araçlar da otomatik yeniden deneniyor | ✅ yakalandı |
| M18 | Web: Host denetimi her şeyi kabul ediyor (DNS rebinding) | ✅ yakalandı |
| M19 | Web: Origin denetimi kaldırıldı (CSRF) | ✅ yakalandı |
| M20 | Web: application/json zorunluluğu kaldırıldı (CSRF) | ✅ yakalandı |
| M21 | Hub/Provider: API anahtarı alt sürece aktarılıyor | ✅ yakalandı |
| M22 | Provider: thinking bloğu geçmişe geri yazılmıyor | ✅ yakalandı |
| M23 | Provider: Opus 5.5'te 400 üreten sabit tool_choice gönderiliyor | ✅ yakalandı |
| M24 | Arayüz: escapeHtml etkisiz (XSS) | ✅ yakalandı |
| M25 | Arayüz: javascript: bağlantıları çiziliyor (XSS) | ✅ yakalandı |
| M26 | SSE: kopmada üreteç kapatılmıyor | ✅ yakalandı |
| M27 | ICS: metin kaçışı yapılmıyor | ✅ yakalandı |
| M28 | Takvim: boş zaman geçmiş saatleri de döndürüyor | ✅ yakalandı |

İlk koşuda iki mutasyon testleri aşmıştı: **M05** gerçek bir boşluktu (authorizer katmanının tek başına testi yoktu → `TestDefenseLayers` eklendi); **M10** *eşdeğer mutant*tı (kaldırılan satır zaten gereksizdi → kod sadeleştirildi, mutasyon anlamlı olanla değiştirildi). M25'in hedef metni geçersizdi, düzeltildi.

## 4. Geliştirme sırasında testlerin yakaladığı gerçek hatalar

Aşağıdakilerin hepsi düzeltildi ve her biri için kalıcı bir test var.

| # | Bulgu | Nasıl yakalandı | Çözüm |
|---|---|---|---|
| 1 | Python `casefold()` büyük `I`'yı `i` yapar: “çalışma” araması “ÇALIŞMA” satırını **bulamıyordu** (Türkçe kullanıcı için ciddi) | `fs_grep` birim testi | `textutil.fold_text`: `i/ı/İ/I` eşleştirilir; takvim aramasında da kullanılır |
| 2 | SQLite `CREATE TABLE`, motor içinde `sqlite_master`'a yazar; “`sqlite_*` tablolarına yazma yasak” kuralı **tüm DDL'i bozuyordu** | DB testleri | Authorizer ifade türüne göre ayrıldı (DDL iç yazımlara izinli, DML değil) |
| 3 | `WITH … DELETE` için `sqlite3` `rowcount=-1` verir (0 sanılıyordu); yarım okunmuş `RETURNING` ifadesi `COMMIT`'i engelleyebilir | DB testleri | `total_changes` farkı + imleç kapatma |
| 4 | Oturum yöneticisi, başka oturumlar meşgulken **yeni oluşturulan oturumu kendi kendine tahliye ediyordu** | `test_busy_sessions_are_not_evicted_or_deleted` | Yeni oturum asla aday değil |
| 5 | İptal/hata sonrası geçmişte **yetim kullanıcı mesajı** (aynı soru tekrarında çift mesaj) | Ajan testleri | Yanıtsız kullanıcı mesajı geri alınır |
| 6 | İç içe async üreteçler iptalde deterministik kapanmazsa `finally` blokları çöp toplamada çalışıp geçmişe **çift `tool_result`** ekleyebilirdi | Tasarım incelemesi + iptal testleri | Her yerde `contextlib.aclosing` |
| 7 | Starlette, istemci kopunca gövde üretecini `aclose()` etmez; üreteç `yield`'deyken oturum “meşgul”, onay “bekliyor” kalabilirdi | `test_sse.py` (karşılaştırmalı) | `SSEResponse` |
| 8 | Araçlar sekmesinde `content`/`sql` gibi alanlar **tek satırlık input**'tu: yeni satırlar siliniyor, dosya içeriği bozuluyordu | Playwright E2E | `textarea` + kırpma yok |
| 9 | uvicorn kapanıştan sonra sinyali yeniden fırlatır: Ctrl+C ile durdurma çıkış kodu **130 / -15** | Gerçek `ui` süreç testi | Sinyaller yönetilir → kod 0 |
| 10 | Oturum boyunca açık senkron Playwright, ana iş parçacığında döngü bırakıp sonraki `pytest-asyncio` testlerini bozuyordu | Tam paket koşusu | Tarayıcı fixture'ı test başına |
| 11 | Okuma-authorizer'ı tamamen açılsa **hiçbir test başarısız olmuyordu** (diğer katmanlar yazmayı zaten engelliyor) | Mutasyon M05 | Her savunma katmanı tek başına test edildi (`TestDefenseLayers`) |
| 12 | Gerçek SDK davranışı varsayımlarımı düzeltti: SDK 2.x kimlik doğrulamayı `Authorization: Bearer` ile yapar; beta uç noktası aynı yolu kullanır; kesik tool-JSON istisna fırlatmaz, **eksik girdi** üretir | Sağlayıcı testleri | `max_tokens`/`refusal` turlarında araç asla çalıştırılmaz (ajan testleri) |
| 13 | Sohbet listesi, ayrıntı açılınca/yanıt bitince **kırpık kalıyordu**; markdown başlık seviyeleri; mobilde sekme çubuğu dikey yığılıyordu | Ekran görüntüleri + E2E | Akıllı otomatik kaydırma, `#`→`h2`, mobil CSS |
| 14 | Dosya sandbox'ı özellik testi **tüm örneklerde ortak tek bir jail** kullanıyordu: bir örnek `link_out` bağını silip aynı adla klasör açınca temizlik bunu onarmıyor, sonraki her örnek (boş dizi dahil) kırılıyordu. Sandbox'ta hata yoktu — dışarıdaki veri her durumda korundu; hata testin kendi durumundaydı | Yeniden tasarım sonrası **son tam koşu** (nadir rastgele dizi) | Her örnek taze jail + `finally` temizliği; değişmez netleştirildi; deterministik regresyon testi (bağı sil + aynı adla klasör aç, özyinelemeli ve değil) |
| 15 | Tasarımda CSS `uppercase` + `lang="tr"`, İngilizce araç başlıklarını Türkçe kurallarla büyütüyordu (“FİLE”, “TİME”) | Ekran görüntüsü + `test_approval_card_keeps_the_english_tool_title_unshouted` / `test_tool_titles_in_the_tools_tab_use_english_casing_rules` | İngilizce başlıklara `lang="en"` |
| 16 | İki arayüzü aynı testte ayağa kaldırmak sabit bağlantı noktasında çakışıyordu | Yeni E2E testleri | Her arayüz için `free_port()` |
| 17 | Karşılama ekranı 1280×860'ta taşıyor, mesaj kutusu iki satıra kırılıyor, MCP dairesi kartı kesiyordu | Ekran görüntüleri + `test_hero_and_composer_fit_a_laptop_screen_without_scrolling` | Kolaj/başlık/çipler yeniden ölçeklendi; kolaj `em` birimli |

## 5. Güvenlik doğrulama özeti

| Alan | Doğrulanan |
|---|---|
| Dosya sandbox'ı | `..`, mutlak yol, kardeş-önek, symlink (dizin/dosya/sarkık/dışa yazma), NUL, Unicode; **özellik testi**: hiçbir işlem dizisi workspace dışını değiştiremez |
| SQL | Her katman ayrı ayrı (salt-okunur URI, `query_only`, authorizer, anahtar sözcük beyaz listesi, tek ifade); `ATTACH`/`PRAGMA`/`VACUUM INTO`/`load_extension`; zaman, bellek-bombası, boyut sınırı; **özellik testi**: rastgele SQL asla DB dosyasını değiştirmez/yeni dosya yaratmaz |
| Web | Host (DNS rebinding), Origin, Content-Type (CSRF), CORS yok, katı CSP, hata sızıntısı yok |
| XSS | Model çıktısı, kullanıcı metni, araç çıktısı, markdown bağlantıları, tablo hücreleri — hiçbiri çalışmaz; satır içi işleyiciler CSP'ce engellenir |
| Onay | Matris (9 kombinasyon), bilinmeyen politika, mixed kararlar, zaman aşımı, kopma, **ele geçirilmiş model senaryosu** |
| Sır yönetimi | API anahtarı alt sürece geçmez, `repr`/durum/config çıktısında yok |

## 6. Mac'te sizin doğrulamanız gerekenler (bu ortamda yapılamadı)

1. `./scripts/setup.sh` temiz bir Mac'te tamamlanıyor mu? (Linux'ta hem `uv` hem `venv+pip` yolu çalıştırıldı.)
2. `.venv/bin/localasistan doctor --llm` — **ilk gerçek API çağrısı**. Başarısız olursa mesaj nedeni söyler; sunucu-tarafı fallback şüphesinde `LOCALASISTAN_REFUSAL_FALLBACK=0`.
3. Safari/Chrome'da arayüz (testler Chromium'da yapıldı); `scripts/LocalAsistan.command` çift tıklama.
4. `~/Documents` altı çalışma alanı için macOS “Dosyalar ve Klasörler” izin istemi.
5. Türkçe dosya adlarıyla (Finder'da oluşturulmuş, NFD) `fs_list`/`fs_read` — kod NFC normalleştirir, APFS ile gerçek denemesi sizde.
6. İsteğe bağlı: Takvim.app'ten `.ics` dışa aktarıp `calendar_import_ics` ile içe aktarma.
7. İsteğe bağlı: Claude Desktop ile `localasistan desktop-config` çıktısı (API anahtarı gerektirmez; adımlar README'de). Bu ortamda Claude Desktop çalıştırılamadı; sunucu protokol düzeyinde mcp 1.30 ve 2.x istemcileriyle stdio üzerinden doğrulandı.
8. Yeni arayüzün Safari'de görünümü (`backdrop`/`:focus-within`/`clamp()` kullanılır; testler Chromium'da). Anton başlık yazı tipi uygulamayla birlikte gelir, internet gerekmez.

## 7. Bilinen sınırlar (bilinçli)

Tekrarlayan (RRULE) olaylar `.ics` içe aktarımında atlanır ve sayılır · `fs_grep` düz metindir (regex yok → ReDoS yok) · sohbet geçmişi bellekte
(son 20 oturum) · uzun sohbetlerde otomatik bağlam sıkıştırma yok · tek MCP sunucusu · arayüzde kimlik doğrulama yok (yalnızca loopback) ·
Takvim.app canlı entegrasyonu yok · TOCTOU: sandbox denetimi ile işlem arasında yerel bir saldırganın symlink değiştirmesi (tek kullanıcılı yerel araç için kabul edildi).
