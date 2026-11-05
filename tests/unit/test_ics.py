from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from localasistan.server import ics

IST = ZoneInfo("Europe/Istanbul")


class TestEscapeAndFold:
    @pytest.mark.parametrize(
        "text",
        ["plain", "a,b;c", "back\\slash", "line1\nline2", "Çay, şeker; \\ ve\nyeni satır ✓ 🎉", ""],
    )
    def test_roundtrip(self, text: str):
        assert ics.unescape_text(ics.escape_text(text)) == text

    def test_escape_shapes(self):
        assert ics.escape_text("a,b;c\\d\r\ne") == r"a\,b\;c\\d\ne"

    def test_unescape_trailing_backslash_kept(self):
        assert ics.unescape_text("abc\\") == "abc\\"

    def test_short_line_not_folded(self):
        assert ics.fold_line("SUMMARY:short") == ["SUMMARY:short"]

    def test_fold_respects_octet_limit_and_roundtrips(self):
        line = "DESCRIPTION:" + "çalışma güzel ✓ 🎉 " * 20
        segments = ics.fold_line(line)
        assert len(segments) > 1
        assert all(len(seg.encode("utf-8")) <= 75 for seg in segments)
        assert all(seg.startswith(" ") for seg in segments[1:])
        assert "".join([segments[0], *(s[1:] for s in segments[1:])]) == line

    def test_fold_never_splits_a_multibyte_character(self):
        for text in ("ş" * 80, "🎉" * 40, "a" + "ğ" * 74):
            for seg in ics.fold_line("X:" + text):
                seg.encode("utf-8")  # geçerli UTF-8 kalmalı (bölünmüş karakter olsaydı yükselirdi)


class TestSerialize:
    NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)

    def test_structure(self):
        text = ics.serialize_calendar(
            [
                ics.IcsEvent(
                    "u1@x",
                    "Toplantı",
                    datetime(2026, 11, 6, 11, 0, tzinfo=UTC),
                    datetime(2026, 11, 6, 12, 0, tzinfo=UTC),
                    "not, önemli",
                    "Ofis; kat 2",
                ),
                ics.IcsEvent("u2@x", "Tatil", date(2026, 11, 9), date(2026, 11, 12)),
            ],
            self.NOW,
        )
        assert text.startswith("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")
        assert text.endswith("END:VCALENDAR\r\n")
        assert "\n" not in text.replace("\r\n", "")
        assert "DTSTART:20261106T110000Z" in text
        assert "DTEND:20261106T120000Z" in text
        assert "DTSTART;VALUE=DATE:20261109" in text and "DTEND;VALUE=DATE:20261112" in text
        assert "DESCRIPTION:not\\, önemli" in text
        assert r"LOCATION:Ofis\; kat 2" in text
        assert "DTSTAMP:20261105T090000Z" in text
        assert text.count("BEGIN:VEVENT") == text.count("END:VEVENT") == 2

    def test_empty_calendar_is_valid(self):
        text = ics.serialize_calendar([], self.NOW)
        assert "BEGIN:VEVENT" not in text and text.endswith("END:VCALENDAR\r\n")

    def test_optional_fields_omitted(self):
        text = ics.serialize_calendar(
            [ics.IcsEvent("u@x", "A", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))],
            self.NOW,
        )
        assert "DESCRIPTION" not in text and "LOCATION" not in text

    def test_serialize_then_parse_roundtrip(self):
        original = ics.IcsEvent(
            "rt@x",
            "Çok uzun başlık, noktalı virgül; ve ters\\bölü " * 3 + "son",
            datetime(2026, 11, 6, 11, 0, tzinfo=UTC),
            datetime(2026, 11, 6, 12, 30, tzinfo=UTC),
            "satır1\nsatır2, ✓ " * 10 + "son",
            "Kadıköy, İstanbul",
        )
        parsed = ics.parse_calendar(ics.serialize_calendar([original], self.NOW), IST)
        assert parsed.events == [original]


class TestParse:
    def wrap(self, *vevents: str) -> str:
        return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(vevents) + "END:VCALENDAR\r\n"

    def ev(self, *lines: str) -> str:
        return "BEGIN:VEVENT\r\n" + "".join(f"{line}\r\n" for line in lines) + "END:VEVENT\r\n"

    def test_utc_tzid_floating_and_date(self):
        text = self.wrap(
            self.ev("UID:a", "SUMMARY:UTC", "DTSTART:20261106T110000Z", "DTEND:20261106T120000Z"),
            self.ev(
                "UID:b",
                "SUMMARY:TZID",
                "DTSTART;TZID=Europe/Berlin:20261106T100000",
                "DTEND;TZID=Europe/Berlin:20261106T110000",
            ),
            self.ev("UID:c", "SUMMARY:Floating", "DTSTART:20261106T140000", "DTEND:20261106T150000"),
            self.ev("UID:d", "SUMMARY:Day", "DTSTART;VALUE=DATE:20261109", "DTEND;VALUE=DATE:20261111"),
        )
        r = ics.parse_calendar(text, IST)
        by = {e.uid: e for e in r.events}
        assert by["a"].start == datetime(2026, 11, 6, 11, 0, tzinfo=UTC)
        assert by["b"].start.astimezone(UTC) == datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
        assert by["c"].start.astimezone(UTC) == datetime(2026, 11, 6, 11, 0, tzinfo=UTC)  # Istanbul +03
        assert by["d"].start == date(2026, 11, 9) and by["d"].end == date(2026, 11, 11)
        assert r.warnings == []

    def test_duration_variants_and_defaults(self):
        text = self.wrap(
            self.ev("UID:a", "SUMMARY:1h30", "DTSTART:20261106T100000Z", "DURATION:PT1H30M"),
            self.ev("UID:b", "SUMMARY:1d", "DTSTART;VALUE=DATE:20261106", "DURATION:P1D"),
            self.ev("UID:c", "SUMMARY:1w", "DTSTART:20261106T100000Z", "DURATION:P1W"),
            self.ev("UID:d", "SUMMARY:no end timed", "DTSTART:20261106T100000Z"),
            self.ev("UID:e", "SUMMARY:no end date", "DTSTART;VALUE=DATE:20261106"),
        )
        by = {e.uid: e for e in ics.parse_calendar(text, IST).events}
        assert by["a"].end - by["a"].start == timedelta(minutes=90)
        assert by["b"].end == date(2026, 11, 7)
        assert by["c"].end - by["c"].start == timedelta(weeks=1)
        assert by["d"].end - by["d"].start == timedelta(hours=1)
        assert by["e"].end == date(2026, 11, 7)

    def test_skips_are_counted(self):
        text = self.wrap(
            self.ev("UID:r", "SUMMARY:recurring", "DTSTART:20261106T100000Z", "RRULE:FREQ=WEEKLY"),
            self.ev("UID:x", "SUMMARY:cancelled", "DTSTART:20261106T100000Z", "STATUS:CANCELLED"),
            self.ev("UID:n", "SUMMARY:no start"),
            self.ev("UID:bad", "SUMMARY:bad date", "DTSTART:garbage"),
            self.ev("UID:neg", "SUMMARY:end before", "DTSTART:20261106T100000Z", "DTEND:20261106T090000Z"),
            self.ev("UID:mix", "SUMMARY:mixed", "DTSTART:20261106T100000Z", "DTEND;VALUE=DATE:20261107"),
            self.ev("UID:ok", "SUMMARY:fine", "DTSTART:20261106T100000Z", "DTEND:20261106T110000Z"),
        )
        r = ics.parse_calendar(text, IST)
        assert [e.uid for e in r.events] == ["ok"]
        assert (r.skipped_recurring, r.skipped_cancelled, r.skipped_invalid) == (1, 1, 4)

    def test_unknown_timezone_warns_and_falls_back(self):
        text = self.wrap(
            self.ev(
                "UID:a",
                "SUMMARY:x",
                "DTSTART;TZID=Mars/Base:20261106T100000",
                "DTEND;TZID=Mars/Base:20261106T110000",
            )
        )
        r = ics.parse_calendar(text, IST)
        assert r.events[0].start.tzinfo == IST
        assert any("Mars/Base" in w for w in r.warnings) and len(r.warnings) == 1

    def test_windows_timezone_names(self):
        text = self.wrap(
            self.ev(
                "UID:a",
                "SUMMARY:x",
                "DTSTART;TZID=Turkey Standard Time:20261106T100000",
                "DTEND;TZID=Turkey Standard Time:20261106T110000",
            )
        )
        r = ics.parse_calendar(text, ZoneInfo("UTC"))
        assert r.events[0].start.astimezone(UTC) == datetime(2026, 11, 6, 7, 0, tzinfo=UTC)
        assert r.warnings == []

    def test_unfolding_and_escapes(self):
        text = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:f\r\nDTSTART:20261106T100000Z\r\nDTEND:20261106T110000Z\r\n"
            "SUMMARY:Uzun \r\n başlık\r\nDESCRIPTION:a\\nb\\, c\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        e = ics.parse_calendar(text, IST).events[0]
        assert e.title == "Uzun başlık" and e.description == "a\nb, c"

    def test_lf_only_and_lowercase_markers(self):
        text = (
            "BEGIN:VCALENDAR\nbegin:vevent\nUID:l\nDTSTART:20261106T100000Z\n"
            "DTEND:20261106T110000Z\nSUMMARY:x\nend:vevent\nEND:VCALENDAR\n"
        )
        assert len(ics.parse_calendar(text, IST).events) == 1

    def test_missing_summary_gets_placeholder(self):
        text = self.wrap(self.ev("UID:a", "DTSTART:20261106T100000Z", "DTEND:20261106T110000Z"))
        assert ics.parse_calendar(text, IST).events[0].title == "(no title)"

    def test_quoted_param_with_colon_and_semicolon(self):
        text = self.wrap(
            self.ev(
                "UID:q",
                'SUMMARY;LANGUAGE="tr;x:y":Merhaba',
                "DTSTART:20261106T100000Z",
                "DTEND:20261106T110000Z",
            )
        )
        assert ics.parse_calendar(text, IST).events[0].title == "Merhaba"

    def test_garbage_and_empty_input(self):
        assert ics.parse_calendar("", IST).events == []
        assert ics.parse_calendar("this is not ics", IST).events == []
        assert ics.parse_calendar("BEGIN:VEVENT\r\nUID:x\r\n", IST).events == []  # kapanmamış blok

    def test_max_events_cap(self):
        body = "".join(
            self.ev(f"UID:{i}", "SUMMARY:x", "DTSTART:20261106T100000Z", "DTEND:20261106T110000Z") for i in range(10)
        )
        r = ics.parse_calendar(self.wrap(body), IST, max_events=3)
        assert len(r.events) == 3 and any("stopped" in w for w in r.warnings)
