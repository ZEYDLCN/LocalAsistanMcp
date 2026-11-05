"""Örnek veri: dosyalar, takvim ve veritabanı (ilk denemeyi kolaylaştırmak için).

Güvenlik: mevcut hiçbir dosyanın üzerine yazmaz; bir alan (çalışma alanı / takvim / veritabanı) dolu ise
`force=False` iken o alanı atlar.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from .config import Settings
from .errors import AlreadyExistsError
from .sandbox import Sandbox
from .server.calendar import CalendarService
from .server.database import DatabaseService
from .server.files import FileService

FILES = {
    "notlar/hosgeldin.md": (
        "# LocalAsistan'a hoş geldiniz\n\n"
        "Bu klasör, asistanın dosya araçlarının **çalışma alanı**dır; dışına çıkamaz.\n\n"
        'Deneyebileceğiniz şeyler:\n- "Bugünkü takvimimi özetle"\n- "alisveris.txt dosyasında ne var?"\n'
        '- "Bu ay en çok hangi kategoride harcama yaptım?"\n'
    ),
    "notlar/alisveris.txt": "süt\nekmek\nzeytin\npeynir\nçay\nlimon\n",
    "projeler/yol-haritasi.md": (
        "# Yol haritası\n\n## Bu hafta\n- [ ] MCP sunucusunu gözden geçir\n- [ ] Arayüz denemeleri\n\n"
        "## Sonraki\n- [ ] Takvim .ics içe aktarımını dene\n"
    ),
    "projeler/toplanti-notlari.md": (
        "# Toplantı notları — Haftalık planlama\n\nKatılanlar: Ayşe, Çağlar, Şule\n\n"
        "Kararlar:\n1. Teslim tarihi cumaya alındı.\n2. Şule test planını hazırlayacak.\n"
        "3. Bir sonraki toplantı salı 10:00.\n"
    ),
}

EXPENSES = [
    ("2026-10-02", "market", "Haftalık alışveriş", 842.50),
    ("2026-10-05", "ulaşım", "Akbil yükleme", 300.00),
    ("2026-10-09", "yemek", "Öğle yemeği", 185.00),
    ("2026-10-12", "market", "Meyve sebze", 214.75),
    ("2026-10-18", "fatura", "İnternet", 399.90),
    ("2026-10-21", "yemek", "Akşam yemeği", 620.00),
    ("2026-10-27", "ulaşım", "Taksi", 275.00),
    ("2026-11-01", "market", "Haftalık alışveriş", 910.20),
]
PEOPLE = [
    ("Ayşe Yılmaz", "ayse@example.com", "İstanbul"),
    ("Çağlar Demir", "caglar@example.com", "Ankara"),
    ("Şule Kaya", "sule@example.com", "İzmir"),
    ("Öznur Aydın", "oznur@example.com", "Bursa"),
]


def seed(settings: Settings, *, force: bool = False) -> dict[str, Any]:
    settings.ensure_dirs()
    files = FileService(Sandbox(settings.workspace), max_file_bytes=settings.max_file_bytes, timezone=settings.timezone)
    calendar = CalendarService(settings.calendar_db, timezone=settings.timezone)
    database = DatabaseService(settings.user_db, timeout_seconds=settings.db_timeout_seconds)
    report: dict[str, Any] = {"files": 0, "events": 0, "tables": 0, "skipped": []}

    if force or not any(settings.workspace.iterdir()):
        for path, content in FILES.items():
            try:
                files.write_new(path, content)
                report["files"] += 1
            except AlreadyExistsError:
                pass
    else:
        report["skipped"].append("workspace (not empty)")

    today = calendar._local(calendar.now()).date()
    existing = calendar.list_events(today.isoformat(), (today + timedelta(days=365)).isoformat(), limit=1)
    if force or existing["count"] == 0:
        plan = [
            ("Haftalık planlama", 0, "10:00", 60, "Ofis", ""),
            ("Öğle yemeği — Ayşe", 0, "13:00", 60, "Kadıköy", ""),
            ("Diş randevusu", 1, "09:30", 45, "Moda Diş Kliniği", "Kontrol"),
            ("Müşteri görüşmesi", 3, "11:00", 90, "Levent", "Sunum hazırlanacak"),
        ]
        for title, day, clock, minutes, place, note in plan:
            start = f"{(today + timedelta(days=day)).isoformat()}T{clock}"
            end_h, end_m = divmod(int(clock[:2]) * 60 + int(clock[3:]) + minutes, 60)
            end = f"{(today + timedelta(days=day)).isoformat()}T{end_h:02d}:{end_m:02d}"
            calendar.create_event(title, start, end, location=place, description=note, allow_conflicts=True)
            report["events"] += 1
        calendar.create_event("Proje teslimi", (today + timedelta(days=2)).isoformat(), all_day=True)
        report["events"] += 1
    else:
        report["skipped"].append("calendar (has events)")

    if force or database.list_tables()["count"] == 0:
        database.execute(
            "CREATE TABLE IF NOT EXISTS giderler (id INTEGER PRIMARY KEY AUTOINCREMENT, tarih TEXT NOT NULL, "
            "kategori TEXT NOT NULL, aciklama TEXT, tutar REAL NOT NULL)"
        )
        database.execute(
            "CREATE TABLE IF NOT EXISTS kisiler (id INTEGER PRIMARY KEY AUTOINCREMENT, ad TEXT NOT NULL, "
            "eposta TEXT UNIQUE, sehir TEXT)"
        )
        if database.query("SELECT COUNT(*) FROM giderler")["rows"][0][0] == 0:
            database.insert(
                "giderler",
                [{"tarih": t, "kategori": k, "aciklama": a, "tutar": v} for t, k, a, v in EXPENSES],
            )
        if database.query("SELECT COUNT(*) FROM kisiler")["rows"][0][0] == 0:
            database.insert("kisiler", [{"ad": n, "eposta": e, "sehir": c} for n, e, c in PEOPLE])
        report["tables"] = 2
    else:
        report["skipped"].append("database (has tables)")
    return report
