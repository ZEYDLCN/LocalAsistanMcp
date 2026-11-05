"""iCalendar (RFC 5545) serileştirme / ayrıştırma — yalnızca bu uygulamanın ihtiyacı kadar.

* Dışa aktarım: UTC zaman damgaları, tüm-gün olaylar `VALUE=DATE` (bitiş **dışlayıcı**),
  metin kaçışları, 75 oktet satır katlama (UTF-8 karakter ortasında bölmez), CRLF.
* İçe aktarım: satır açma, parametreli özellikler (`TZID`, `VALUE=DATE`), `Z`/yüzen/TZID zamanlar,
  `DURATION`, `STATUS:CANCELLED` ve `RRULE`'lü (tekrarlayan) olayları atlayıp raporlama.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PRODID = "-//LocalAsistan//MCP Takvim//TR"
_WINDOWS_ZONES = {
    "Turkey Standard Time": "Europe/Istanbul",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Romance Standard Time": "Europe/Paris",
    "Eastern Standard Time": "America/New_York",
    "Central Standard Time": "America/Chicago",
    "Pacific Standard Time": "America/Los_Angeles",
}


@dataclass
class IcsEvent:
    uid: str
    title: str
    start: datetime | date  # date => tüm gün
    end: datetime | date  # tüm gün için DIŞLAYICI son gün
    description: str = ""
    location: str = ""


@dataclass
class ParseResult:
    events: list[IcsEvent] = field(default_factory=list)
    skipped_recurring: int = 0
    skipped_cancelled: int = 0
    skipped_invalid: int = 0
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- kaçış/katlama
def escape_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def unescape_text(value: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def fold_line(line: str, limit: int = 75) -> list[str]:
    """Satırı en fazla `limit` oktetlik parçalara böler (devam satırları boşlukla başlar)."""
    segments: list[str] = []
    current = ""
    current_bytes = 0
    budget = limit
    for char in line:
        size = len(char.encode("utf-8"))
        if current_bytes + size > budget:
            segments.append(current)
            current, current_bytes = "", 0
            budget = limit - 1  # devam satırındaki baştaki boşluk için
        current += char
        current_bytes += size
    segments.append(current)
    return [segments[0], *(" " + s for s in segments[1:])]


def _content_line(name: str, value: str) -> list[str]:
    return fold_line(f"{name}:{value}")


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------------------------- serileştirme
def serialize_calendar(events: list[IcsEvent], now: datetime) -> str:
    lines: list[str] = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN"]
    stamp = _fmt_utc(now)
    for event in events:
        lines.append("BEGIN:VEVENT")
        lines += _content_line("UID", event.uid)
        lines.append(f"DTSTAMP:{stamp}")
        if isinstance(event.start, datetime):
            assert isinstance(event.end, datetime)
            lines.append(f"DTSTART:{_fmt_utc(event.start)}")
            lines.append(f"DTEND:{_fmt_utc(event.end)}")
        else:
            lines.append(f"DTSTART;VALUE=DATE:{event.start:%Y%m%d}")
            lines.append(f"DTEND;VALUE=DATE:{event.end:%Y%m%d}")
        lines += _content_line("SUMMARY", escape_text(event.title))
        if event.description:
            lines += _content_line("DESCRIPTION", escape_text(event.description))
        if event.location:
            lines += _content_line("LOCATION", escape_text(event.location))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# -------------------------------------------------------------------------- ayrıştırma
_DURATION = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """`NAME;P1=V1;P2="V;2":value` -> (NAME, {P1: V1, P2: V;2}, value)."""
    in_quotes = False
    colon = -1
    for index, char in enumerate(line):
        if char == '"':
            in_quotes = not in_quotes
        elif char == ":" and not in_quotes:
            colon = index
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1 :]
    parts: list[str] = []
    buffer = ""
    in_quotes = False
    for char in head:
        if char == '"':
            in_quotes = not in_quotes
            buffer += char
        elif char == ";" and not in_quotes:
            parts.append(buffer)
            buffer = ""
        else:
            buffer += char
    parts.append(buffer)
    name = parts[0].upper()
    params: dict[str, str] = {}
    for part in parts[1:]:
        key, _, val = part.partition("=")
        params[key.upper()] = val.strip('"')
    return name, params, value


def _parse_duration(value: str) -> timedelta | None:
    match = _DURATION.match(value.strip())
    if not match or not any(match.groups()[1:]):
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = timedelta(
        weeks=int(weeks or 0),
        days=int(days or 0),
        hours=int(hours or 0),
        minutes=int(minutes or 0),
        seconds=int(seconds or 0),
    )
    return -delta if sign == "-" else delta


def _zone(name: str | None, default: ZoneInfo, warnings: list[str]) -> ZoneInfo:
    if not name:
        return default
    candidate = _WINDOWS_ZONES.get(name, name)
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        message = f"unknown timezone '{name}', assumed local time"
        if message not in warnings:
            warnings.append(message)
        return default


def _parse_when(value: str, params: dict[str, str], default: ZoneInfo, warnings: list[str]) -> datetime | date:
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value[:8], "%Y%m%d").date()
    if value.endswith(("Z", "z")):
        return datetime.strptime(value[:-1], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    naive = datetime.strptime(value, "%Y%m%dT%H%M%S")
    return naive.replace(tzinfo=_zone(params.get("TZID"), default, warnings))


def parse_calendar(text: str, default_tz: ZoneInfo, *, max_events: int = 5000) -> ParseResult:
    result = ParseResult()
    block: list[tuple[str, dict[str, str], str]] | None = None
    for line in unfold(text):
        stripped = line.strip()
        if not stripped:
            continue
        upper = stripped.upper()
        if upper == "BEGIN:VEVENT":
            block = []
            continue
        if upper == "END:VEVENT":
            if block is not None:
                if len(result.events) >= max_events:
                    result.warnings.append(f"stopped after {max_events} events")
                    break
                _finish_event(block, result, default_tz)
            block = None
            continue
        if block is not None:
            prop = _split_property(stripped)
            if prop:
                block.append(prop)
    return result


def _finish_event(props: list[tuple[str, dict[str, str], str]], result: ParseResult, default_tz: ZoneInfo) -> None:
    by_name: dict[str, tuple[dict[str, str], str]] = {}
    for name, params, value in props:
        by_name.setdefault(name, (params, value))
    if "RRULE" in by_name or "RDATE" in by_name:
        result.skipped_recurring += 1
        return
    if by_name.get("STATUS", ({}, ""))[1].strip().upper() == "CANCELLED":
        result.skipped_cancelled += 1
        return
    if "DTSTART" not in by_name:
        result.skipped_invalid += 1
        return
    try:
        start = _parse_when(by_name["DTSTART"][1], by_name["DTSTART"][0], default_tz, result.warnings)
        if "DTEND" in by_name:
            end = _parse_when(by_name["DTEND"][1], by_name["DTEND"][0], default_tz, result.warnings)
        elif "DURATION" in by_name:
            delta = _parse_duration(by_name["DURATION"][1])
            if delta is None:
                raise ValueError("bad duration")
            end = start + delta
        elif isinstance(start, datetime):
            end = start + timedelta(hours=1)
        else:
            end = start + timedelta(days=1)
    except (ValueError, OverflowError):
        result.skipped_invalid += 1
        return
    if isinstance(start, datetime) != isinstance(end, datetime):
        result.skipped_invalid += 1
        return
    if end <= start:
        result.skipped_invalid += 1
        return
    title = unescape_text(by_name.get("SUMMARY", ({}, ""))[1]).strip() or "(no title)"
    uid = by_name.get("UID", ({}, ""))[1].strip()
    result.events.append(
        IcsEvent(
            uid=uid,
            title=title,
            start=start,
            end=end,
            description=unescape_text(by_name.get("DESCRIPTION", ({}, ""))[1]),
            location=unescape_text(by_name.get("LOCATION", ({}, ""))[1]),
        )
    )
