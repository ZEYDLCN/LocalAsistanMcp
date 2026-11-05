from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from localasistan.errors import ConflictError, InvalidInputError, LimitError, NotFoundError
from localasistan.server.calendar import CalendarService

# 2026-11-05 Perşembe 12:00 İstanbul (UTC+3)
NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def cal(tmp_path: Path, clock: Clock) -> CalendarService:
    return CalendarService(tmp_path / "cal.db", timezone="Europe/Istanbul", now=clock)


def titles(result: dict) -> list[str]:
    return [e["title"] for e in result["events"]]


class TestNow:
    def test_now_info(self, cal):
        assert cal.now_info() == {
            "now": "2026-11-05T12:00:00+03:00",
            "date": "2026-11-05",
            "time": "12:00",
            "weekday": "Thursday",
            "iso_week": 45,
            "timezone": "Europe/Istanbul",
            "utc_offset": "+03:00",
        }

    def test_negative_offset_zone(self, tmp_path):
        ny = CalendarService(tmp_path / "ny.db", timezone="America/New_York", now=Clock())
        assert ny.now_info()["utc_offset"] == "-05:00" and ny.now_info()["time"] == "04:00"


class TestCreate:
    def test_defaults_to_one_hour_and_local_time(self, cal):
        e = cal.create_event("Toplantı", "2026-11-06T14:00")
        assert e["id"] == 1
        assert (e["start"], e["end"]) == ("2026-11-06T14:00:00+03:00", "2026-11-06T15:00:00+03:00")
        assert e["duration_minutes"] == 60 and e["weekday"] == "Friday" and e["all_day"] is False

    def test_input_zones_are_converted(self, cal):
        utc = cal.create_event("A", "2026-11-06T11:00:00Z", "2026-11-06T11:30:00Z")
        off = cal.create_event(
            "B", "2026-11-06T13:00:00+02:00", "2026-11-06T13:30:00+02:00", allow_conflicts=True
        )
        assert utc["start"] == off["start"] == "2026-11-06T14:00:00+03:00"
        assert utc["duration_minutes"] == 30

    def test_space_separator_and_seconds(self, cal):
        assert cal.create_event("A", "2026-11-06 09:30:15")["start"] == "2026-11-06T09:30:15+03:00"

    def test_text_fields_stripped_and_unicode(self, cal):
        e = cal.create_event(
            "  Şükrü'yle çay  ", "2026-11-06T09:00", description=" not ", location=" Kadıköy "
        )
        assert (e["title"], e["description"], e["location"]) == ("Şükrü'yle çay", "not", "Kadıköy")

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"title": "  "}, "title must not be empty"),
            ({"title": "x" * 201}, "too long"),
            ({"description": "x" * 5001}, "too long"),
            ({"location": "x" * 201}, "too long"),
            ({"start": ""}, "start is required"),
            ({"start": "yarın 3'te"}, "could not understand"),
            ({"start": "2026-13-01T10:00"}, "could not understand"),
            ({"start": "2026-02-30T10:00"}, "could not understand"),
            ({"start": "1850-01-01T10:00"}, "year must be"),
            ({"start": "2500-01-01T10:00"}, "year must be"),
            ({"start": "2026-11-06"}, "needs a time"),
            ({"end": "2026-11-06"}, "needs a time"),
            ({"end": "2026-11-06T13:59"}, "end must be after start"),
            ({"end": "2026-11-06T14:00"}, "end must be after start"),
            ({"end": "2028-11-06T14:00"}, "at most 366 days"),
        ],
    )
    def test_validation(self, cal, kwargs, message):
        args = {"title": "T", "start": "2026-11-06T14:00", **kwargs}
        with pytest.raises(InvalidInputError, match=message):
            cal.create_event(**args)
        assert cal.list_events("2026-01-01", "2026-12-31")["count"] == 0

    def test_non_string_start(self, cal):
        with pytest.raises(InvalidInputError):
            cal.create_event("T", None)  # type: ignore[arg-type]


class TestAllDay:
    def test_single_day_defaults(self, cal):
        e = cal.create_event("Bayram", "2026-11-06", all_day=True)
        assert (e["start"], e["end"], e["days"], e["all_day"]) == ("2026-11-06", "2026-11-06", 1, True)
        assert e["weekday"] == "Friday" and "duration_minutes" not in e

    def test_multi_day_end_is_inclusive(self, cal):
        e = cal.create_event("Tatil", "2026-11-06", "2026-11-08", all_day=True)
        assert (e["start"], e["end"], e["days"]) == ("2026-11-06", "2026-11-08", 3)

    def test_time_part_is_ignored(self, cal):
        e = cal.create_event("X", "2026-11-06T18:30", "2026-11-07T01:00", all_day=True)
        assert (e["start"], e["end"]) == ("2026-11-06", "2026-11-07")

    def test_end_before_start(self, cal):
        with pytest.raises(InvalidInputError, match="before the start"):
            cal.create_event("X", "2026-11-08", "2026-11-06", all_day=True)

    def test_appears_only_on_its_days(self, cal):
        cal.create_event("Tatil", "2026-11-06", "2026-11-07", all_day=True)
        assert titles(cal.list_events("2026-11-05")) == []
        assert titles(cal.list_events("2026-11-06")) == ["Tatil"]
        assert titles(cal.list_events("2026-11-07")) == ["Tatil"]
        assert titles(cal.list_events("2026-11-08")) == []

    def test_does_not_conflict_with_timed_events(self, cal):
        cal.create_event("Doğum günü", "2026-11-06", all_day=True)
        cal.create_event("Toplantı", "2026-11-06T10:00")  # tüm-gün olay engel olmaz
        cal.create_event("Tatil", "2026-11-06", all_day=True)  # tüm-gün olaylar birbiriyle de çakışmaz
        assert cal.list_events("2026-11-06")["count"] == 3


class TestConflicts:
    @pytest.fixture(autouse=True)
    def _base(self, cal):
        cal.create_event("Standup", "2026-11-06T10:00", "2026-11-06T11:00")

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("2026-11-06T10:30", "2026-11-06T11:30"),  # sonuna taşan
            ("2026-11-06T09:30", "2026-11-06T10:30"),  # başına taşan
            ("2026-11-06T10:15", "2026-11-06T10:45"),  # içinde
            ("2026-11-06T09:00", "2026-11-06T12:00"),  # kapsayan
            ("2026-11-06T10:00", "2026-11-06T11:00"),  # birebir aynı
        ],
    )
    def test_overlaps_rejected_with_actionable_message(self, cal, start, end):
        with pytest.raises(ConflictError, match=r"#1 'Standup'.*allow_conflicts=true"):
            cal.create_event("Yeni", start, end)
        assert cal.list_events("2026-11-06")["count"] == 1

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("2026-11-06T11:00", "2026-11-06T12:00"),  # bitişik (sonra)
            ("2026-11-06T09:00", "2026-11-06T10:00"),  # bitişik (önce)
            ("2026-11-07T10:00", "2026-11-07T11:00"),  # ertesi gün
        ],
    )
    def test_adjacent_and_other_days_allowed(self, cal, start, end):
        assert cal.create_event("Yeni", start, end)["id"] == 2

    def test_allow_conflicts(self, cal):
        assert cal.create_event("Çift", "2026-11-06T10:00", allow_conflicts=True)["id"] == 2

    def test_many_conflicts_are_summarised(self, cal):
        for i in range(6):
            cal.create_event(f"E{i}", "2026-11-06T10:00", allow_conflicts=True)
        with pytest.raises(ConflictError, match=r"7 existing event.*\+2 more"):
            cal.create_event("Yeni", "2026-11-06T10:00")

    def test_failed_create_does_not_consume_ids_or_rows(self, cal):
        with pytest.raises(ConflictError):
            cal.create_event("Yeni", "2026-11-06T10:30")
        assert cal.create_event("Sonraki", "2026-11-06T12:00")["id"] == 2

    def test_parallel_booking_of_same_slot_has_one_winner(self, tmp_path, clock):
        shared = CalendarService(tmp_path / "race.db", timezone="Europe/Istanbul", now=clock)
        outcomes: list[str] = []
        barrier = threading.Barrier(10)

        def book(n: int):
            barrier.wait()
            try:
                shared.create_event(f"Racer {n}", "2026-11-06T15:00", "2026-11-06T16:00")
                outcomes.append("ok")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=book, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert outcomes.count("ok") == 1 and outcomes.count("conflict") == 9
        assert shared.list_events("2026-11-06")["count"] == 1


class TestGetUpdateDelete:
    @pytest.fixture(autouse=True)
    def _base(self, cal):
        cal.create_event(
            "Standup", "2026-11-06T10:00", "2026-11-06T10:30", description="günlük", location="Ofis"
        )
        cal.create_event("Öğle", "2026-11-06T12:00", "2026-11-06T13:00")
        cal.create_event("Tatil", "2026-11-09", "2026-11-10", all_day=True)

    def test_get(self, cal):
        assert cal.get_event(1)["location"] == "Ofis"
        with pytest.raises(NotFoundError, match="#99"):
            cal.get_event(99)

    def test_update_title_only_keeps_everything_else(self, cal):
        before = cal.get_event(1)
        after = cal.update_event(1, title="Yeni başlık")
        assert after["title"] == "Yeni başlık"
        assert {k: v for k, v in after.items() if k != "title"} == {
            k: v for k, v in before.items() if k != "title"
        }

    def test_moving_start_preserves_duration(self, cal):
        e = cal.update_event(1, start="2026-11-06T14:00")
        assert (e["start"], e["end"], e["duration_minutes"]) == (
            "2026-11-06T14:00:00+03:00",
            "2026-11-06T14:30:00+03:00",
            30,
        )

    def test_end_only_changes_duration(self, cal):
        e = cal.update_event(1, end="2026-11-06T11:15")
        assert (e["start"], e["duration_minutes"]) == ("2026-11-06T10:00:00+03:00", 75)

    def test_start_and_end_together(self, cal):
        e = cal.update_event(1, start="2026-11-07T09:00", end="2026-11-07T09:45")
        assert (e["start"], e["duration_minutes"]) == ("2026-11-07T09:00:00+03:00", 45)

    def test_clear_description_and_location(self, cal):
        e = cal.update_event(1, description="", location="")
        assert (e["description"], e["location"]) == ("", "")

    def test_update_conflict_and_self_overlap(self, cal):
        with pytest.raises(ConflictError, match="#2 'Öğle'"):
            cal.update_event(1, start="2026-11-06T12:15")
        assert cal.get_event(1)["start"].startswith("2026-11-06T10:00")  # değişmedi
        # kendi aralığı içinde kaydırma çakışma sayılmaz
        assert cal.update_event(1, start="2026-11-06T10:10")["start"].startswith("2026-11-06T10:10")
        assert cal.update_event(1, start="2026-11-06T12:15", allow_conflicts=True)["id"] == 1

    def test_title_update_never_triggers_conflict_check(self, cal):
        cal.create_event("Çift", "2026-11-06T10:00", allow_conflicts=True)
        assert cal.update_event(1, title="Standup v2")["title"] == "Standup v2"

    def test_all_day_move_keeps_length_and_end_update(self, cal):
        moved = cal.update_event(3, start="2026-11-16")
        assert (moved["start"], moved["end"], moved["days"]) == ("2026-11-16", "2026-11-17", 2)
        longer = cal.update_event(3, end="2026-11-20")
        assert (longer["start"], longer["end"], longer["days"]) == ("2026-11-16", "2026-11-20", 5)
        with pytest.raises(InvalidInputError, match="before the start"):
            cal.update_event(3, end="2026-11-01")

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({}, "Nothing to update"),
            ({"title": " "}, "must not be empty"),
            ({"start": "2026-11-06"}, "needs a time"),
            ({"end": "2026-11-06"}, "needs a time"),
            ({"end": "2026-11-06T09:00"}, "end must be after start"),
            ({"start": "nonsense"}, "could not understand"),
        ],
    )
    def test_update_validation(self, cal, kwargs, message):
        with pytest.raises(InvalidInputError, match=message):
            cal.update_event(1, **kwargs)

    def test_update_not_found(self, cal):
        with pytest.raises(NotFoundError):
            cal.update_event(42, title="x")

    def test_delete(self, cal):
        removed = cal.delete_event(2)
        assert removed["deleted"]["title"] == "Öğle"
        with pytest.raises(NotFoundError):
            cal.get_event(2)
        with pytest.raises(NotFoundError):
            cal.delete_event(2)
        assert cal.list_events("2026-11-06")["count"] == 1

    def test_ids_are_not_reused_after_delete(self, cal):
        cal.delete_event(3)
        assert cal.create_event("Yeni", "2026-11-20T10:00")["id"] == 4


class TestList:
    @pytest.fixture(autouse=True)
    def _base(self, cal):
        cal.create_event(
            "Sabah", "2026-11-06T09:00", "2026-11-06T10:00", description="Kahve içilecek", location="Beşiktaş"
        )
        cal.create_event("Gece nöbeti", "2026-11-06T22:00", "2026-11-07T02:00")
        cal.create_event("Öğleden sonra", "2026-11-07T14:00", "2026-11-07T15:00")
        cal.create_event("Tatil", "2026-11-09", all_day=True)

    def test_single_day_shortcut_and_ordering(self, cal):
        r = cal.list_events("2026-11-06")
        assert titles(r) == ["Sabah", "Gece nöbeti"]
        assert r["range"] == {"start": "2026-11-06T00:00:00+03:00", "end": "2026-11-07T00:00:00+03:00"}

    def test_midnight_spanning_event_shows_on_both_days(self, cal):
        assert "Gece nöbeti" in titles(cal.list_events("2026-11-07"))

    def test_date_range_end_is_inclusive_day(self, cal):
        assert titles(cal.list_events("2026-11-06", "2026-11-07")) == [
            "Sabah",
            "Gece nöbeti",
            "Öğleden sonra",
        ]

    def test_datetime_range_boundaries_are_half_open(self, cal):
        # Sabah 09:00-10:00; 10:00'da biten aralık ... 10:00'da başlayan aralıkta görünmez
        assert titles(cal.list_events("2026-11-06T10:00", "2026-11-06T11:00")) == []
        assert titles(cal.list_events("2026-11-06T08:00", "2026-11-06T09:00")) == []
        assert titles(cal.list_events("2026-11-06T09:59", "2026-11-06T10:01")) == ["Sabah"]

    def test_query_is_turkish_and_case_insensitive_across_fields(self, cal):
        assert titles(cal.list_events("2026-11-06", "2026-11-09", query="GECE")) == ["Gece nöbeti"]
        assert titles(cal.list_events("2026-11-06", "2026-11-09", query="kahve")) == ["Sabah"]
        assert titles(cal.list_events("2026-11-06", "2026-11-09", query="BEŞİKTAŞ")) == ["Sabah"]
        assert (
            titles(cal.list_events("2026-11-06", "2026-11-09", query="  ")) != []
        )  # boş sorgu filtre değildir
        assert titles(cal.list_events("2026-11-06", "2026-11-09", query="yok böyle")) == []

    def test_limit_and_truncation(self, cal):
        r = cal.list_events("2026-11-06", "2026-11-09", limit=2)
        assert r["count"] == 2 and r["truncated"] is True

    @pytest.mark.parametrize(
        ("kwargs", "exc", "message"),
        [
            ({"start": "2026-11-06T09:00"}, InvalidInputError, "end is required"),
            ({"start": "2026-11-06", "end": "2026-11-05"}, InvalidInputError, "after start"),
            ({"start": "2026-11-06T10:00", "end": "2026-11-06T10:00"}, InvalidInputError, "after start"),
            ({"start": "2026-01-01", "end": "2028-01-01"}, LimitError, "at most 366"),
            ({"start": "2026-11-06", "limit": 0}, InvalidInputError, "limit"),
            ({"start": "2026-11-06", "limit": 501}, InvalidInputError, "limit"),
        ],
    )
    def test_validation(self, cal, kwargs, exc, message):
        with pytest.raises(exc, match=message):
            cal.list_events(**kwargs)


class TestTimezones:
    @pytest.fixture
    def berlin(self, tmp_path, clock) -> CalendarService:
        return CalendarService(tmp_path / "b.db", timezone="Europe/Berlin", now=clock)

    def test_spring_forward_gap_is_normalised(self, berlin):
        # 2026-03-29 02:30 Berlin'de yok (02:00 -> 03:00)
        e = berlin.create_event("Gap", "2026-03-29T02:30")
        assert e["start"] == "2026-03-29T03:30:00+02:00"

    def test_fall_back_ambiguous_time_uses_first_occurrence(self, berlin):
        e = berlin.create_event("Amb", "2026-10-25T02:30")
        assert e["start"] == "2026-10-25T02:30:00+02:00"

    def test_offset_changes_across_dst_for_same_wall_clock(self, berlin):
        winter = berlin.create_event("W", "2026-01-15T09:00")
        summer = berlin.create_event("S", "2026-07-15T09:00")
        assert winter["start"].endswith("+01:00") and summer["start"].endswith("+02:00")

    def test_duration_across_dst_gap_is_real_elapsed_time(self, berlin):
        # 01:30 CET -> 03:30 CEST arası gerçek süre 1 saattir
        e = berlin.create_event("X", "2026-03-29T01:30", "2026-03-29T03:30")
        assert e["duration_minutes"] == 60

    def test_all_day_on_dst_day_covers_the_local_day(self, berlin):
        e = berlin.create_event("DST", "2026-03-29", all_day=True)
        assert (e["start"], e["end"], e["days"]) == ("2026-03-29", "2026-03-29", 1)
        assert titles(berlin.list_events("2026-03-29")) == ["DST"]
        assert titles(berlin.list_events("2026-03-28")) == []
        assert titles(berlin.list_events("2026-03-30")) == []

    def test_viewing_in_another_zone_does_not_move_the_instant(self, tmp_path, clock):
        path = tmp_path / "shared.db"
        istanbul = CalendarService(path, timezone="Europe/Istanbul", now=clock)
        istanbul.create_event("Global", "2026-11-06T14:00")
        tokyo = CalendarService(path, timezone="Asia/Tokyo", now=clock)
        assert tokyo.get_event(1)["start"] == "2026-11-06T20:00:00+09:00"


class TestFreeSlots:
    @pytest.fixture(autouse=True)
    def _base(self, cal):
        # Cuma 2026-11-06: 10:00-11:00 ve 14:00-15:30 dolu
        cal.create_event("A", "2026-11-06T10:00", "2026-11-06T11:00")
        cal.create_event("B", "2026-11-06T14:00", "2026-11-06T15:30")

    def slots(self, result):
        return [(s["start"][11:16], s["end"][11:16]) for s in result["slots"]]

    def test_gaps_between_busy_blocks(self, cal):
        r = cal.find_free_slots("2026-11-06", "2026-11-06", 30)
        assert self.slots(r) == [("09:00", "10:00"), ("11:00", "14:00"), ("15:30", "18:00")]
        assert r["slots"][1]["free_minutes"] == 180 and r["slots"][0]["weekday"] == "Friday"

    def test_duration_filters_short_gaps(self, cal):
        r = cal.find_free_slots("2026-11-06", "2026-11-06", 150)
        assert self.slots(r) == [("11:00", "14:00"), ("15:30", "18:00")]
        assert self.slots(cal.find_free_slots("2026-11-06", "2026-11-06", 181)) == []

    def test_adjacent_and_overlapping_busy_blocks_merge(self, cal):
        cal.create_event("C", "2026-11-06T11:00", "2026-11-06T12:00")  # A'ya bitişik
        cal.create_event("D", "2026-11-06T11:30", "2026-11-06T13:00", allow_conflicts=True)  # C ile çakışan
        assert self.slots(cal.find_free_slots("2026-11-06", "2026-11-06", 30)) == [
            ("09:00", "10:00"),
            ("13:00", "14:00"),
            ("15:30", "18:00"),
        ]

    def test_events_outside_window_are_clipped(self, cal):
        cal.create_event("Erken", "2026-11-06T07:00", "2026-11-06T09:30")
        cal.create_event("Geç", "2026-11-06T17:00", "2026-11-06T20:00")
        assert self.slots(cal.find_free_slots("2026-11-06", "2026-11-06", 30)) == [
            ("09:30", "10:00"),
            ("11:00", "14:00"),
            ("15:30", "17:00"),
        ]

    def test_custom_working_hours(self, cal):
        r = cal.find_free_slots("2026-11-06", "2026-11-06", 30, work_start="10:30", work_end="16:00")
        assert self.slots(r) == [("11:00", "14:00"), ("15:30", "16:00")]

    def test_weekends_skipped_unless_included(self, cal):
        weekday_only = cal.find_free_slots("2026-11-06", "2026-11-09", 60)
        days = {s["start"][:10] for s in weekday_only["slots"]}
        assert days == {"2026-11-06", "2026-11-09"}
        with_weekend = cal.find_free_slots("2026-11-06", "2026-11-09", 60, include_weekends=True)
        assert {s["start"][:10] for s in with_weekend["slots"]} == {
            "2026-11-06",
            "2026-11-07",
            "2026-11-08",
            "2026-11-09",
        }

    def test_today_is_clipped_to_now_rounded_up(self, cal, clock):
        clock.value = datetime(2026, 11, 5, 9, 3, 20, tzinfo=UTC)  # 12:03:20 yerel
        r = cal.find_free_slots("2026-11-05", "2026-11-05", 30)
        assert self.slots(r) == [("12:05", "18:00")]

    def test_past_days_have_no_slots(self, cal):
        assert cal.find_free_slots("2026-11-02", "2026-11-04", 30)["slots"] == []

    def test_after_hours_today_has_no_slots(self, cal, clock):
        clock.value = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)  # 19:00 yerel
        assert cal.find_free_slots("2026-11-05", "2026-11-05", 30)["slots"] == []

    def test_all_day_events_block_only_when_requested(self, cal):
        cal.create_event("İzin", "2026-11-09", all_day=True)
        assert any(
            s["start"].startswith("2026-11-09")
            for s in cal.find_free_slots("2026-11-09", "2026-11-09", 60)["slots"]
        )
        blocked = cal.find_free_slots("2026-11-09", "2026-11-09", 60, all_day_blocks=True)
        assert blocked["slots"] == []

    def test_max_slots_truncation(self, cal):
        r = cal.find_free_slots("2026-11-06", "2026-11-20", 30, max_slots=2)
        assert r["count"] == 2 and r["truncated"] is True

    @pytest.mark.parametrize(
        ("kwargs", "exc", "message"),
        [
            ({"duration_minutes": 4}, InvalidInputError, "duration_minutes"),
            ({"duration_minutes": 1441}, InvalidInputError, "duration_minutes"),
            ({"max_slots": 0}, InvalidInputError, "max_slots"),
            ({"work_start": "18:00", "work_end": "09:00"}, InvalidInputError, "later than"),
            ({"work_start": "9am"}, InvalidInputError, "work_start"),
            ({"work_end": "17:00:30"}, InvalidInputError, "HH:MM"),
            ({"work_end": "17:00+03:00"}, InvalidInputError, "HH:MM"),
            ({"end": "2026-11-01"}, InvalidInputError, "must not be before"),
            ({"end": "2026-12-31"}, LimitError, "31 days"),
        ],
    )
    def test_validation(self, cal, kwargs, exc, message):
        args = {"start": "2026-11-06", "end": "2026-11-06", "duration_minutes": 30, **kwargs}
        with pytest.raises(exc, match=message):
            cal.find_free_slots(**args)

    def test_dst_day_window_is_computed_in_local_time(self, tmp_path, clock):
        clock.value = datetime(2026, 3, 20, 9, 0, tzinfo=UTC)
        berlin = CalendarService(tmp_path / "b.db", timezone="Europe/Berlin", now=clock)
        r = berlin.find_free_slots("2026-03-30", "2026-03-30", 30)  # DST'den sonraki gün
        assert r["slots"][0]["start"] == "2026-03-30T09:00:00+02:00"
        gap_day = berlin.find_free_slots("2026-03-29", "2026-03-29", 30, include_weekends=True)
        assert gap_day["slots"][0]["start"] == "2026-03-29T09:00:00+02:00"


class TestIcs:
    def test_export_empty(self, cal):
        text, count = cal.export_ics()
        assert count == 0 and "BEGIN:VEVENT" not in text

    def test_roundtrip_between_two_calendars(self, cal, tmp_path, clock):
        cal.create_event(
            "Çay, kahve; muhabbet",
            "2026-11-06T14:00",
            "2026-11-06T15:15",
            description="satır1\nsatır2",
            location="Moda, İstanbul",
        )
        cal.create_event("Tatil", "2026-11-09", "2026-11-11", all_day=True)
        text, count = cal.export_ics()
        assert count == 2

        other = CalendarService(tmp_path / "other.db", timezone="Europe/Istanbul", now=clock)
        report = other.import_ics(text)
        assert report["imported"] == 2 and report["skipped_duplicates"] == 0
        a, b = other.list_events("2026-11-06", "2026-11-12")["events"]
        assert (
            a["title"] == "Çay, kahve; muhabbet"
            and a["description"] == "satır1\nsatır2"
            and a["location"] == "Moda, İstanbul"
        )
        assert (a["start"], a["end"]) == ("2026-11-06T14:00:00+03:00", "2026-11-06T15:15:00+03:00")
        assert (b["start"], b["end"], b["days"], b["all_day"]) == ("2026-11-09", "2026-11-11", 3, True)

    def test_reimport_is_idempotent(self, cal, tmp_path, clock):
        cal.create_event("X", "2026-11-06T14:00")
        text, _ = cal.export_ics()
        other = CalendarService(tmp_path / "o.db", timezone="Europe/Istanbul", now=clock)
        other.import_ics(text)
        again = other.import_ics(text)
        assert again["imported"] == 0 and again["skipped_duplicates"] == 1
        assert other.list_events("2026-11-06")["count"] == 1

    def test_export_range_filter(self, cal):
        cal.create_event("A", "2026-11-06T10:00")
        cal.create_event("B", "2026-11-20T10:00")
        text, count = cal.export_ics("2026-11-01", "2026-11-10")
        assert count == 1 and "SUMMARY:A" in text and "SUMMARY:B" not in text

    def test_import_external_calendar_with_mixed_content(self, cal):
        text = "\r\n".join(
            [
                "BEGIN:VCALENDAR",
                "VERSION:2.0",
                "BEGIN:VEVENT",
                "UID:ext-1",
                "DTSTART;TZID=Europe/Istanbul:20261110T090000",
                "DTEND;TZID=Europe/Istanbul:20261110T100000",
                "SUMMARY:Dış toplantı",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "UID:ext-2",
                "DTSTART:20261111T090000Z",
                "RRULE:FREQ=DAILY",
                "SUMMARY:Tekrarlı",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "UID:ext-3",
                "DTSTART;VALUE=DATE:20261112",
                "SUMMARY:Gün",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "UID:ext-4",
                "DTSTART:20261113T090000Z",
                "STATUS:CANCELLED",
                "SUMMARY:İptal",
                "END:VEVENT",
                "BEGIN:VEVENT",
                "UID:ext-5",
                "SUMMARY:Başlangıçsız",
                "END:VEVENT",
                "END:VCALENDAR",
                "",
            ]
        )
        report = cal.import_ics(text)
        assert report == {
            "imported": 2,
            "skipped_duplicates": 0,
            "skipped_recurring": 1,
            "skipped_cancelled": 1,
            "skipped_invalid": 1,
            "warnings": [],
        }
        assert titles(cal.list_events("2026-11-10", "2026-11-12")) == ["Dış toplantı", "Gün"]

    def test_import_garbage_is_harmless(self, cal):
        assert cal.import_ics("not a calendar")["imported"] == 0
        assert cal.import_ics("")["imported"] == 0

    def test_import_out_of_range_dates_are_invalid_not_fatal(self, cal):
        text = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:old\r\nDTSTART:18000101T000000Z\r\nDTEND:18000101T010000Z\r\nSUMMARY:eski\r\nEND:VEVENT\r\n"
            "BEGIN:VEVENT\r\nUID:ok\r\nDTSTART:20261110T000000Z\r\nDTEND:20261110T010000Z\r\nSUMMARY:ok\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        report = cal.import_ics(text)
        assert report["imported"] == 1 and report["skipped_invalid"] == 1

    def test_import_truncates_oversized_fields(self, cal):
        text = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:big\r\nDTSTART:20261110T000000Z\r\nDTEND:20261110T010000Z\r\n"
            f"SUMMARY:{'t' * 500}\r\nDESCRIPTION:{'d' * 9000}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        cal.import_ics(text)
        e = cal.get_event(1)
        assert len(e["title"]) == 200 and len(e["description"]) == 5000


class TestAgenda:
    def test_empty_and_filled(self, cal):
        assert cal.agenda_text() == "No events (2026-11-05)."
        cal.create_event("Planlama", "2026-11-05T15:00", "2026-11-05T16:00", location="Ofis")
        cal.create_event("Bayram", "2026-11-05", all_day=True)
        text = cal.agenda_text()
        assert "Agenda 2026-11-05 (Europe/Istanbul):" in text
        assert "#1 2026-11-05 15:00 - 16:00: Planlama @ Ofis" in text
        assert "#2 2026-11-05 (all day): Bayram" in text

    def test_multi_day_label(self, cal):
        cal.create_event("Sonra", "2026-11-08T10:00")
        assert "2026-11-05 .. 2026-11-11" in cal.agenda_text(7)
