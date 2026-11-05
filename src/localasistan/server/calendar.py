"""Takvim servisi — SQLite tabanlı yerel takvim (`calendar.db`).

Zaman modeli
------------
* Zamanlı olaylar **UTC** (`YYYY-MM-DDTHH:MM:SSZ`) saklanır; çıktıda kullanıcının IANA
  diliminde ofsetli ISO 8601 olarak gösterilir.
* Tüm-gün olaylar, olayın kendi diliminde yerel gece yarısından ertesi gece yarısına kadar
  saklanır; arayüzde `start`/`end` **dahil** tarihlerdir (ör. 6–8 Kasım => `end` = 8 Kasım).
* Çakışma denetimi yalnızca *zamanlı* olaylar arasındadır; tüm-gün olaylar bilgilendirici
  kabul edilir (isteğe bağlı olarak boş-slot aramasında meşgul sayılabilir).
* Çakışma denetimi ve ekleme tek `BEGIN IMMEDIATE` işleminde yapılır (çift rezervasyon yarışı yok).
"""

from __future__ import annotations

import contextlib
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ..errors import ConflictError, InvalidInputError, LimitError, NotFoundError
from ..textutil import fold_text
from . import ics

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_STORE_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_MIN_YEAR, _MAX_YEAR = 1970, 2100
_MAX_RANGE_DAYS = 366
_MAX_EVENT_DAYS = 366
_DEFAULT_DURATION = timedelta(minutes=60)

_INSERT_SQL = (
    "INSERT INTO events (uid, title, description, location, start_utc, end_utc, all_day, tz, "
    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT    NOT NULL UNIQUE,
    title       TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    location    TEXT    NOT NULL DEFAULT '',
    start_utc   TEXT    NOT NULL,
    end_utc     TEXT    NOT NULL,
    all_day     INTEGER NOT NULL DEFAULT 0,
    tz          TEXT    NOT NULL,
    created_at  TEXT    NOT NULL,
    updated_at  TEXT    NOT NULL,
    CHECK (end_utc > start_utc)
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_utc);
CREATE INDEX IF NOT EXISTS idx_events_end ON events(end_utc);
"""


@dataclass(frozen=True)
class Moment:
    value: datetime  # her zaman tz-aware
    date_only: bool


def _store(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime(_STORE_FORMAT)


def _load(text: str) -> datetime:
    return datetime.strptime(text, _STORE_FORMAT).replace(tzinfo=UTC)


class CalendarService:
    def __init__(
        self, path: Path, *, timezone: str = "UTC", now: Callable[[], datetime] | None = None
    ) -> None:
        self.path = Path(path)
        self.tz = ZoneInfo(timezone)
        self.tz_name = timezone
        self._now = now or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    # ------------------------------------------------------------------ altyapı
    @contextlib.contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10.0)
        try:
            conn.isolation_level = None
            conn.row_factory = sqlite3.Row
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")

    def now(self) -> datetime:
        return self._now().astimezone(UTC)

    def _local(self, dt: datetime, tz: ZoneInfo | None = None) -> datetime:
        return dt.astimezone(tz or self.tz)

    def _iso(self, dt: datetime, tz: ZoneInfo | None = None) -> str:
        return self._local(dt, tz).isoformat(timespec="seconds")

    # ------------------------------------------------------------ giriş ayrıştırma
    def parse_moment(self, value: str, field: str) -> Moment:
        if not isinstance(value, str) or not value.strip():
            raise InvalidInputError(
                f"{field} is required (ISO 8601, e.g. '2026-11-06T14:30' or '2026-11-06')."
            )
        text = value.strip()
        try:
            date_only = len(text) == 10 and text[4] == "-" and text[7] == "-"
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise InvalidInputError(
                f"{field}: could not understand '{text[:60]}'. "
                "Use ISO 8601, e.g. '2026-11-06T14:30' or '2026-11-06'."
            ) from None
        if not _MIN_YEAR <= parsed.year <= _MAX_YEAR:
            raise InvalidInputError(f"{field}: year must be between {_MIN_YEAR} and {_MAX_YEAR}.")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.tz)
        # DST boşluğundaki (olmayan) yerel saatleri normalleştirir (02:30 -> 03:30 gibi)
        normalized = parsed.astimezone(UTC).astimezone(parsed.tzinfo)
        return Moment(normalized, date_only)

    def _local_date(self, moment: Moment, tz: ZoneInfo | None = None) -> date:
        return self._local(moment.value, tz).date()

    def _midnight(self, day: date, tz: ZoneInfo | None = None) -> datetime:
        zone = tz or self.tz
        return datetime.combine(day, time(0, 0), tzinfo=zone).astimezone(UTC)

    # ----------------------------------------------------------------- dönüşümler
    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        tz = ZoneInfo(row["tz"])
        start, end = _load(row["start_utc"]), _load(row["end_utc"])
        all_day = bool(row["all_day"])
        local_start = self._local(start, tz)
        event: dict = {"id": row["id"], "title": row["title"], "all_day": all_day}
        if all_day:
            last_day = self._local(end - timedelta(seconds=1), tz).date()
            event["start"] = local_start.date().isoformat()
            event["end"] = last_day.isoformat()
            event["days"] = (last_day - local_start.date()).days + 1
            event["weekday"] = _WEEKDAYS[local_start.weekday()]
        else:
            display = self.tz
            ls = self._local(start, display)
            event["start"] = ls.isoformat(timespec="seconds")
            event["end"] = self._iso(end, display)
            event["duration_minutes"] = int((end - start).total_seconds() // 60)
            event["weekday"] = _WEEKDAYS[ls.weekday()]
        event["location"] = row["location"]
        event["description"] = row["description"]
        return event

    @staticmethod
    def _clean_text(value: str | None, field: str, limit: int, *, required: bool = False) -> str:
        text = (value or "").strip()
        if required and not text:
            raise InvalidInputError(f"{field} must not be empty.")
        if len(text) > limit:
            raise InvalidInputError(f"{field} is too long ({len(text)} > {limit} characters).")
        return text

    def _span(
        self, start: str, end: str | None, all_day: bool, *, tz: ZoneInfo | None = None
    ) -> tuple[datetime, datetime]:
        """(başlangıç, bitiş) UTC; hata verirse InvalidInputError."""
        first = self.parse_moment(start, "start")
        if all_day:
            first_day = self._local_date(first, tz)
            last_day = self._local_date(self.parse_moment(end, "end"), tz) if end else first_day
            if last_day < first_day:
                raise InvalidInputError("end date must not be before the start date.")
            start_utc, end_utc = (
                self._midnight(first_day, tz),
                self._midnight(last_day + timedelta(days=1), tz),
            )
        else:
            if first.date_only:
                raise InvalidInputError(
                    "start needs a time (e.g. '2026-11-06T14:30'); set all_day=true for whole-day events."
                )
            start_utc = first.value.astimezone(UTC)
            if end:
                last = self.parse_moment(end, "end")
                if last.date_only:
                    raise InvalidInputError("end needs a time (e.g. '2026-11-06T15:30').")
                end_utc = last.value.astimezone(UTC)
            else:
                end_utc = start_utc + _DEFAULT_DURATION
            if end_utc <= start_utc:
                raise InvalidInputError("end must be after start.")
        if end_utc - start_utc > timedelta(days=_MAX_EVENT_DAYS):
            raise InvalidInputError(f"An event can span at most {_MAX_EVENT_DAYS} days.")
        return start_utc, end_utc

    def _conflicts(
        self, conn: sqlite3.Connection, start: datetime, end: datetime, *, exclude_id: int | None = None
    ) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM events WHERE all_day = 0 AND start_utc < ? AND end_utc > ? AND id != ? "
            "ORDER BY start_utc",
            (_store(end), _store(start), exclude_id or -1),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _conflict_error(conflicts: list[dict]) -> ConflictError:
        listing = "; ".join(f"#{c['id']} '{c['title']}' ({c['start']} to {c['end']})" for c in conflicts[:5])
        extra = f" (+{len(conflicts) - 5} more)" if len(conflicts) > 5 else ""
        return ConflictError(
            f"Conflicts with {len(conflicts)} existing event(s): {listing}{extra}. "
            "Ask the user, then retry with allow_conflicts=true to double-book, or choose another time "
            "(see calendar_find_free_slots)."
        )

    # -------------------------------------------------------------------- CRUD
    def now_info(self) -> dict:
        current = self._local(self.now())
        offset = current.strftime("%z")
        return {
            "now": current.isoformat(timespec="seconds"),
            "date": current.date().isoformat(),
            "time": current.strftime("%H:%M"),
            "weekday": _WEEKDAYS[current.weekday()],
            "iso_week": current.isocalendar().week,
            "timezone": self.tz_name,
            "utc_offset": f"{offset[:3]}:{offset[3:]}",
        }

    def create_event(
        self,
        title: str,
        start: str,
        end: str | None = None,
        *,
        all_day: bool = False,
        description: str = "",
        location: str = "",
        allow_conflicts: bool = False,
    ) -> dict:
        title = self._clean_text(title, "title", 200, required=True)
        description = self._clean_text(description, "description", 5000)
        location = self._clean_text(location, "location", 200)
        start_utc, end_utc = self._span(start, end, all_day)
        stamp = _store(self.now())
        with self._write() as conn:
            if not all_day and not allow_conflicts:
                conflicts = self._conflicts(conn, start_utc, end_utc)
                if conflicts:
                    raise self._conflict_error(conflicts)
            cursor = conn.execute(
                _INSERT_SQL,
                (
                    f"{uuid.uuid4()}@localasistan",
                    title,
                    description,
                    location,
                    _store(start_utc),
                    _store(end_utc),
                    int(all_day),
                    self.tz_name,
                    stamp,
                    stamp,
                ),
            )
            row = conn.execute("SELECT * FROM events WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return self._row_to_dict(row)

    def get_event(self, event_id: int) -> dict:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"Event #{event_id} does not exist.")
        return self._row_to_dict(row)

    def update_event(
        self,
        event_id: int,
        *,
        title: str | None = None,
        start: str | None = None,
        end: str | None = None,
        description: str | None = None,
        location: str | None = None,
        allow_conflicts: bool = False,
    ) -> dict:
        if all(v is None for v in (title, start, end, description, location)):
            raise InvalidInputError(
                "Nothing to update: give at least one of title, start, end, description, location."
            )
        with self._write() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Event #{event_id} does not exist.")
            all_day = bool(row["all_day"])
            event_tz = ZoneInfo(row["tz"])
            old_start, old_end = _load(row["start_utc"]), _load(row["end_utc"])
            new_title = (
                self._clean_text(title, "title", 200, required=True) if title is not None else row["title"]
            )
            new_desc = (
                self._clean_text(description, "description", 5000)
                if description is not None
                else row["description"]
            )
            new_loc = self._clean_text(location, "location", 200) if location is not None else row["location"]

            new_start, new_end = old_start, old_end
            if start is not None or end is not None:
                if all_day:
                    first_day = self._local(old_start, event_tz).date()
                    last_day = self._local(old_end - timedelta(seconds=1), event_tz).date()
                    if start is not None:
                        shifted = self._local_date(self.parse_moment(start, "start"), event_tz)
                        if end is None:  # tüm-gün olayı taşırken gün sayısını koru
                            last_day = shifted + (last_day - first_day)
                        first_day = shifted
                    if end is not None:
                        last_day = self._local_date(self.parse_moment(end, "end"), event_tz)
                    if last_day < first_day:
                        raise InvalidInputError("end date must not be before the start date.")
                    new_start = self._midnight(first_day, event_tz)
                    new_end = self._midnight(last_day + timedelta(days=1), event_tz)
                else:
                    if start is not None:
                        first = self.parse_moment(start, "start")
                        if first.date_only:
                            raise InvalidInputError("start needs a time (e.g. '2026-11-06T14:30').")
                        new_start = first.value.astimezone(UTC)
                        if end is None:  # süreyi koruyarak taşı
                            new_end = new_start + (old_end - old_start)
                    if end is not None:
                        last = self.parse_moment(end, "end")
                        if last.date_only:
                            raise InvalidInputError("end needs a time (e.g. '2026-11-06T15:30').")
                        new_end = last.value.astimezone(UTC)
                    if new_end <= new_start:
                        raise InvalidInputError("end must be after start.")
                if new_end - new_start > timedelta(days=_MAX_EVENT_DAYS):
                    raise InvalidInputError(f"An event can span at most {_MAX_EVENT_DAYS} days.")
                if not all_day and not allow_conflicts and (new_start, new_end) != (old_start, old_end):
                    conflicts = self._conflicts(conn, new_start, new_end, exclude_id=event_id)
                    if conflicts:
                        raise self._conflict_error(conflicts)
            conn.execute(
                "UPDATE events SET title=?, description=?, location=?, start_utc=?, end_utc=?, "
                "updated_at=? WHERE id=?",
                (
                    new_title,
                    new_desc,
                    new_loc,
                    _store(new_start),
                    _store(new_end),
                    _store(self.now()),
                    event_id,
                ),
            )
            updated = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return self._row_to_dict(updated)

    def delete_event(self, event_id: int) -> dict:
        with self._write() as conn:
            row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"Event #{event_id} does not exist.")
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        return {"deleted": self._row_to_dict(row)}

    # -------------------------------------------------------------------- listeleme
    def _range(self, start: str, end: str | None) -> tuple[datetime, datetime]:
        first = self.parse_moment(start, "start")
        lo = self._midnight(self._local_date(first)) if first.date_only else first.value.astimezone(UTC)
        if end is None:
            if not first.date_only:
                raise InvalidInputError("end is required when start has a time of day.")
            hi = self._midnight(self._local_date(first) + timedelta(days=1))
        else:
            last = self.parse_moment(end, "end")
            hi = (
                self._midnight(self._local_date(last) + timedelta(days=1))
                if last.date_only
                else last.value.astimezone(UTC)
            )
        if hi <= lo:
            raise InvalidInputError("end must be after start.")
        if hi - lo > timedelta(days=_MAX_RANGE_DAYS):
            raise LimitError(f"The range can span at most {_MAX_RANGE_DAYS} days.")
        return lo, hi

    def list_events(
        self, start: str, end: str | None = None, *, query: str | None = None, limit: int = 100
    ) -> dict:
        if not 1 <= limit <= 500:
            raise InvalidInputError("limit must be between 1 and 500.")
        lo, hi = self._range(start, end)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE start_utc < ? AND end_utc > ? ORDER BY start_utc, id",
                (_store(hi), _store(lo)),
            ).fetchall()
        needle = fold_text(query) if query and query.strip() else None
        events: list[dict] = []
        truncated = False
        for row in rows:
            if needle and needle not in fold_text(f"{row['title']} {row['description']} {row['location']}"):
                continue
            if len(events) >= limit:
                truncated = True
                break
            events.append(self._row_to_dict(row))
        return {
            "range": {"start": self._iso(lo), "end": self._iso(hi)},
            "events": events,
            "count": len(events),
            "truncated": truncated,
        }

    # ---------------------------------------------------------------- boş zaman
    def find_free_slots(
        self,
        start: str,
        end: str,
        duration_minutes: int,
        *,
        work_start: str = "09:00",
        work_end: str = "18:00",
        include_weekends: bool = False,
        all_day_blocks: bool = False,
        max_slots: int = 20,
    ) -> dict:
        if not 5 <= duration_minutes <= 24 * 60:
            raise InvalidInputError("duration_minutes must be between 5 and 1440.")
        if not 1 <= max_slots <= 200:
            raise InvalidInputError("max_slots must be between 1 and 200.")
        day_from = self._local_date(self.parse_moment(start, "start"))
        day_to = self._local_date(self.parse_moment(end, "end"))
        if day_to < day_from:
            raise InvalidInputError("end must not be before start.")
        if (day_to - day_from).days + 1 > 31:
            raise LimitError("Search at most 31 days at a time.")
        t_start, t_end = self._parse_clock(work_start, "work_start"), self._parse_clock(work_end, "work_end")
        if t_end <= t_start:
            raise InvalidInputError("work_end must be later than work_start.")
        needed = timedelta(minutes=duration_minutes)

        floor = self.now().replace(second=0, microsecond=0)
        if floor < self.now():
            floor += timedelta(minutes=1)  # dakikaya yukarı yuvarla
        floor += timedelta(minutes=(-floor.minute) % 5)  # 5 dakikaya yukarı yuvarla
        slots: list[dict] = []
        truncated = False
        with self._connect() as conn:
            day = day_from
            while day <= day_to and not truncated:
                weekend = day.weekday() >= 5
                if weekend and not include_weekends:
                    day += timedelta(days=1)
                    continue
                window_start = datetime.combine(day, t_start, tzinfo=self.tz).astimezone(UTC)
                window_end = datetime.combine(day, t_end, tzinfo=self.tz).astimezone(UTC)
                window_start = max(window_start, floor)
                if window_end - window_start >= needed:
                    sql = "SELECT start_utc, end_utc FROM events WHERE start_utc < ? AND end_utc > ?"
                    if not all_day_blocks:
                        sql += " AND all_day = 0"
                    busy = sorted(
                        (max(_load(r["start_utc"]), window_start), min(_load(r["end_utc"]), window_end))
                        for r in conn.execute(sql, (_store(window_end), _store(window_start)))
                    )
                    cursor_time = window_start
                    gaps: list[tuple[datetime, datetime]] = []
                    for b_start, b_end in busy:
                        if b_start > cursor_time:
                            gaps.append((cursor_time, b_start))
                        cursor_time = max(cursor_time, b_end)
                    if cursor_time < window_end:
                        gaps.append((cursor_time, window_end))
                    for g_start, g_end in gaps:
                        if g_end - g_start >= needed:
                            if len(slots) >= max_slots:
                                truncated = True
                                break
                            slots.append(
                                {
                                    "start": self._iso(g_start),
                                    "end": self._iso(g_end),
                                    "free_minutes": int((g_end - g_start).total_seconds() // 60),
                                    "weekday": _WEEKDAYS[self._local(g_start).weekday()],
                                }
                            )
                day += timedelta(days=1)
        return {
            "duration_minutes": duration_minutes,
            "working_hours": f"{work_start}-{work_end}",
            "slots": slots,
            "count": len(slots),
            "truncated": truncated,
        }

    @staticmethod
    def _parse_clock(value: str, field: str) -> time:
        try:
            parsed = time.fromisoformat(value.strip())
        except (ValueError, AttributeError):
            raise InvalidInputError(f"{field} must be a time like '09:00'.") from None
        if parsed.tzinfo is not None or parsed.second or parsed.microsecond:
            raise InvalidInputError(f"{field} must be a plain HH:MM time like '09:00'.")
        return parsed

    # ------------------------------------------------------------------------- ICS
    def export_ics(self, start: str | None = None, end: str | None = None) -> tuple[str, int]:
        """(ICS metni, olay sayısı). `start`/`end` verilirse yalnızca o aralık."""
        with self._connect() as conn:
            if start:
                lo, hi = self._range(start, end)
                rows = conn.execute(
                    "SELECT * FROM events WHERE start_utc < ? AND end_utc > ? ORDER BY start_utc, id",
                    (_store(hi), _store(lo)),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM events ORDER BY start_utc, id").fetchall()
        events: list[ics.IcsEvent] = []
        for row in rows:
            tz = ZoneInfo(row["tz"])
            s, e = _load(row["start_utc"]), _load(row["end_utc"])
            if row["all_day"]:
                start_value: datetime | date = self._local(s, tz).date()
                end_value: datetime | date = self._local(e, tz).date()
            else:
                start_value, end_value = s, e
            events.append(
                ics.IcsEvent(
                    row["uid"], row["title"], start_value, end_value, row["description"], row["location"]
                )
            )
        return ics.serialize_calendar(events, self.now()), len(events)

    def import_ics(self, text: str) -> dict:
        parsed = ics.parse_calendar(text, self.tz)
        imported = duplicates = invalid = 0
        stamp = _store(self.now())
        with self._write() as conn:
            for item in parsed.events:
                uid = item.uid or f"{uuid.uuid4()}@import"
                if conn.execute("SELECT 1 FROM events WHERE uid = ?", (uid,)).fetchone():
                    duplicates += 1
                    continue
                try:
                    if isinstance(item.start, datetime):
                        start_utc, end_utc = item.start.astimezone(UTC), item.end.astimezone(UTC)  # type: ignore[union-attr]
                        all_day = 0
                    else:
                        start_utc = self._midnight(item.start)
                        end_utc = self._midnight(item.end)  # type: ignore[arg-type]
                        all_day = 1
                    if not _MIN_YEAR <= start_utc.year <= _MAX_YEAR or end_utc <= start_utc:
                        raise ValueError("out of range")
                    title = (item.title or "(no title)").strip()[:200]
                    conn.execute(
                        _INSERT_SQL,
                        (
                            uid,
                            title,
                            item.description[:5000],
                            item.location[:200],
                            _store(start_utc),
                            _store(end_utc),
                            all_day,
                            self.tz_name,
                            stamp,
                            stamp,
                        ),
                    )
                    imported += 1
                except (ValueError, OverflowError, sqlite3.IntegrityError):
                    invalid += 1
        return {
            "imported": imported,
            "skipped_duplicates": duplicates,
            "skipped_recurring": parsed.skipped_recurring,
            "skipped_cancelled": parsed.skipped_cancelled,
            "skipped_invalid": parsed.skipped_invalid + invalid,
            "warnings": parsed.warnings,
        }

    # ------------------------------------------------------------------ kaynak metni
    def agenda_text(self, days: int = 1) -> str:
        today = self._local(self.now()).date()
        last = today + timedelta(days=days - 1)
        result = self.list_events(today.isoformat(), last.isoformat(), limit=200)
        label = today.isoformat() if days == 1 else f"{today.isoformat()} .. {last.isoformat()}"
        if not result["events"]:
            return f"No events ({label})."
        lines = [f"Agenda {label} ({self.tz_name}):"]
        for event in result["events"]:
            if event["all_day"]:
                span = (
                    event["start"] if event["start"] == event["end"] else f"{event['start']}..{event['end']}"
                )
                when = f"{span} (all day)"
            else:
                when = f"{event['start'][:16].replace('T', ' ')} - {event['end'][11:16]}"
            place = f" @ {event['location']}" if event["location"] else ""
            lines.append(f"- #{event['id']} {when}: {event['title']}{place}")
        return "\n".join(lines)
