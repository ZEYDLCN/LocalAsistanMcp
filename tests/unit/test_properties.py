"""Özellik-tabanlı (hypothesis) testler: rastgele girdilerde değişmezlerin (invariant) korunduğunu doğrular."""

from __future__ import annotations

import contextlib
import hashlib
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st

from localasistan.client.agent import truncate_for_model
from localasistan.client.catalog import strip_titles
from localasistan.config import parse_dotenv
from localasistan.errors import InvalidInputError, LocalAsistanError, SandboxError
from localasistan.sandbox import Sandbox
from localasistan.server import ics
from localasistan.server.calendar import CalendarService
from localasistan.server.database import DatabaseService, first_keyword
from localasistan.server.files import FileService
from localasistan.textutil import fold_text
from localasistan.web.security import split_host

FAST = hsettings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


# ================================================================== Sandbox / FileService
SEGMENTS = st.sampled_from(
    [
        "a",
        "b",
        "..",
        ".",
        "",
        "c d",
        "ş",
        "İ",
        "~",
        "...",
        "dir",
        "file.txt",
        "link_out",
        "link_in",
        "a\\b",
        "%2e%2e",
        "x" * 40,
    ]
)


@pytest.fixture(scope="module")
def jail():
    base = Path(tempfile.mkdtemp(prefix="la-prop-"))
    root, outside = base / "ws", base / "outside"
    (root / "dir").mkdir(parents=True)
    (root / "file.txt").write_text("inside")
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET")
    (root / "link_out").symlink_to(outside)
    (root / "link_in").symlink_to(root / "dir")
    return root, outside


def snapshot(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


@FAST
@given(parts=st.lists(SEGMENTS, max_size=8), absolute=st.booleans(), follow=st.booleans())
def test_sandbox_never_resolves_outside_the_root(jail, parts, absolute, follow):
    root, outside = jail
    box = Sandbox(root)
    path = ("/" if absolute else "") + "/".join(parts)
    try:
        resolved = box.resolve(path, follow_final=follow)
    except (SandboxError, InvalidInputError):
        return
    assert resolved == box.root or box.root in resolved.parents, (path, resolved)
    assert outside not in (resolved, *resolved.parents)


OPS = st.sampled_from(["write", "overwrite", "append", "mkdir", "move", "delete", "delete_r", "read", "grep"])


@FAST
@given(steps=st.lists(st.tuples(OPS, st.lists(SEGMENTS, max_size=4), st.lists(SEGMENTS, max_size=4)), max_size=12))
def test_no_file_operation_can_touch_anything_outside_the_workspace(jail, steps):
    root, outside = jail
    service = FileService(Sandbox(root), max_file_bytes=10_000)
    before = snapshot(outside)
    for op, p1, p2 in steps:
        a, b = "/".join(p1), "/".join(p2)
        try:
            match op:
                case "write":
                    service.write_new(a, "x")
                case "overwrite":
                    service.overwrite(a, "y")
                case "append":
                    service.append(a, "z")
                case "mkdir":
                    service.make_dir(a)
                case "move":
                    service.move(a, b)
                case "delete":
                    service.delete(a)
                case "delete_r":
                    service.delete(a, recursive=True)
                case "read":
                    service.read_text(a)
                case "grep":
                    service.grep("SECRET", a)
        except LocalAsistanError:
            pass
    assert snapshot(outside) == before and outside.exists()
    assert (root / "link_out").is_symlink() or not (
        root / "link_out"
    ).exists()  # outside'a işaret eden link bozulmamış/silinmiş olabilir
    # temizle: bir sonraki örnek için başlangıç durumunu geri kur
    for child in list(root.iterdir()):
        if child.name in ("dir", "file.txt", "link_out", "link_in"):
            continue
        if child.is_dir() and not child.is_symlink():
            import shutil

            shutil.rmtree(child)
        else:
            child.unlink()
    (root / "dir").mkdir(exist_ok=True)
    if not (root / "file.txt").exists():
        (root / "file.txt").write_text("inside")
    for name, target in (("link_out", outside), ("link_in", root / "dir")):
        link = root / name
        if not os.path.lexists(link):
            link.symlink_to(target)


# ====================================================================== SQL güvenliği
FRAGMENTS = [
    "SELECT", "INSERT INTO", "UPDATE", "DELETE FROM", "DROP TABLE", "CREATE TABLE", "ALTER TABLE", "ATTACH DATABASE",
    "PRAGMA", "VACUUM", "VACUUM INTO", "WITH", "RECURSIVE", "t", "people", "*", "1", "(", ")", ";", "--", "/*", "*/",
    "'x'",
    "VALUES", "INTO", "FROM", "WHERE", "sqlite_master", "load_extension('x')", "randomblob(10)", "BEGIN", "COMMIT",
    "'/tmp/la-fuzz-leak.db'", "AS", "e", ",", "=", "AND", "OR", "UNION", "ALL", "ORDER BY", "LIMIT", "3", "?",
]  # fmt: skip
SQL = st.lists(st.sampled_from(FRAGMENTS), min_size=1, max_size=14).map(" ".join) | st.text(max_size=60)


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@FAST
@given(sql=SQL)
def test_query_never_modifies_the_database_and_only_raises_domain_errors(sql):
    with tempfile.TemporaryDirectory() as tmp:
        db = DatabaseService(Path(tmp) / "u.db", timeout_seconds=1.0)
        db.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT)")
        db.insert("people", [{"name": "a"}, {"name": "b"}])
        before = file_digest(db.path)
        listing = sorted(os.listdir(tmp))
        try:
            result = db.query(sql)
            assert set(result) >= {"columns", "rows", "row_count", "truncated"}
        except LocalAsistanError:
            pass
        assert file_digest(db.path) == before
        assert sorted(os.listdir(tmp)) == listing  # yeni dosya (ATTACH/VACUUM INTO) oluşmadı


@FAST
@given(sql=SQL)
def test_execute_never_escapes_the_database_file_and_only_raises_domain_errors(sql):
    leak = Path("/tmp/la-fuzz-leak.db")
    leak.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        db = DatabaseService(Path(tmp) / "u.db", timeout_seconds=1.0)
        db.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT)")
        with contextlib.suppress(LocalAsistanError):
            db.execute(sql)
        extra = [n for n in os.listdir(tmp) if n != "u.db" and not n.endswith("-journal")]
        assert extra == []
        assert db.query("SELECT COUNT(*) FROM sqlite_master")["rows"]  # veritabanı hâlâ sağlam/okunabilir
    assert not leak.exists()


@FAST
@given(text=st.text(max_size=200))
def test_first_keyword_is_total(text):
    word = first_keyword(text)
    assert word == word.upper() and (word == "" or word.isalpha())


# ============================================================================= metin
@FAST
@given(text=st.text(max_size=100))
def test_fold_text_is_idempotent(text):
    assert fold_text(fold_text(text)) == fold_text(text)


@FAST
@given(text=st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=0x17F), max_size=60))
def test_fold_text_ignores_case_for_latin_and_turkish_letters(text):
    assert fold_text(text.upper()) == fold_text(text) == fold_text(text.lower())


@FAST
@given(text=st.text(max_size=300), limit=st.integers(min_value=1, max_value=100))
def test_truncate_for_model_bounds_the_output(text, limit):
    out = truncate_for_model(text, limit)
    assert out == text if len(text) <= limit else out.startswith(text[:limit]) and "truncated" in out


@FAST
@given(raw=st.text(max_size=80))
def test_split_host_and_dotenv_never_raise(raw):
    split_host(raw)
    assert isinstance(parse_dotenv(raw), dict)


@FAST
@given(
    schema=st.recursive(
        st.dictionaries(st.sampled_from(["title", "type", "x"]), st.text(max_size=5) | st.integers(), max_size=3),
        lambda inner: st.dictionaries(
            st.sampled_from(["properties", "anyOf", "items", "title"]), inner | st.lists(inner, max_size=2), max_size=3
        ),
        max_leaves=8,
    )
)
def test_strip_titles_is_idempotent_and_never_mutates(schema):
    import copy

    original = copy.deepcopy(schema)
    once = strip_titles(schema)
    assert schema == original
    assert strip_titles(once) == once


# ============================================================================== ICS
@FAST
@given(raw=st.text(max_size=400) | st.binary(max_size=200).map(lambda b: b.decode("latin-1")))
def test_ics_parser_never_raises(raw):
    from zoneinfo import ZoneInfo

    result = ics.parse_calendar(raw, ZoneInfo("Europe/Istanbul"))
    assert result.skipped_invalid >= 0


SAFE_TEXT = (
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs", "Cc"), blacklist_characters="\r\x85  "),
        min_size=1,
        max_size=120,
    )
    .map(str.strip)
    .filter(bool)
)


@FAST
@given(title=SAFE_TEXT, description=SAFE_TEXT | st.just(""), location=SAFE_TEXT | st.just(""))
def test_ics_roundtrip_preserves_text(title, description, location):
    from zoneinfo import ZoneInfo

    start = datetime(2026, 11, 6, 11, 0, tzinfo=UTC)
    event = ics.IcsEvent("u@x", title, start, datetime(2026, 11, 6, 12, 0, tzinfo=UTC), description, location)
    text = ics.serialize_calendar([event], start)
    assert all(len(line.encode()) <= 75 for line in text.split("\r\n"))
    parsed = ics.parse_calendar(text, ZoneInfo("UTC")).events
    assert len(parsed) == 1
    assert (parsed[0].title, parsed[0].description.rstrip(), parsed[0].location.rstrip()) == (
        title, description.rstrip(), location.rstrip(),
    )  # fmt: skip


# ========================================================================== Takvim
@pytest.fixture
def make_calendar():
    def make(path: Path) -> CalendarService:
        return CalendarService(path, timezone="Europe/Istanbul", now=lambda: datetime(2026, 11, 1, 9, 0, tzinfo=UTC))

    return make


@FAST
@given(
    value=st.text(max_size=40)
    | st.from_regex(
        r"20[0-9]{2}-[0-9]{2}-[0-9]{2}([T ][0-9]{2}:[0-9]{2}(:[0-9]{2})?(Z|[+-][0-9]{2}:[0-9]{2})?)?", fullmatch=True
    )
)
def test_parse_moment_returns_aware_values_or_a_domain_error(make_calendar, value):
    with tempfile.TemporaryDirectory() as tmp:
        cal = make_calendar(Path(tmp) / "c.db")
        try:
            moment = cal.parse_moment(value, "start")
        except InvalidInputError:
            return
        assert moment.value.tzinfo is not None and 1970 <= moment.value.year <= 2100


WORK_START, WORK_END = 9 * 60, 18 * 60


def clock_text(day: str, minute_of_day: int) -> str:
    """`day` gününün `minute_of_day`. dakikası (1440 ve üstü ertesi güne taşar) ISO metni olarak."""
    base = datetime.fromisoformat(day) + timedelta(minutes=minute_of_day)
    return base.strftime("%Y-%m-%dT%H:%M")


@FAST
@given(
    events=st.lists(st.tuples(st.integers(0, 1439), st.integers(5, 400)), max_size=12),
    duration=st.integers(5, 240),
)
def test_free_slots_are_exactly_the_free_gaps_of_the_working_day(make_calendar, events, duration):
    """Boş zaman: etkinliklerle ASLA kesişmez, çalışma saatlerinin içinde kalır ve tüm uygun boşlukları kapsar."""
    with tempfile.TemporaryDirectory() as tmp:
        cal = make_calendar(Path(tmp) / "c.db")
        day = "2026-11-09"  # Pazartesi
        busy = [False] * 1440
        for start_min, length in events:
            end_min = start_min + length
            cal.create_event("e", clock_text(day, start_min), clock_text(day, end_min), allow_conflicts=True)
            for minute in range(start_min, min(end_min, 1440)):
                busy[minute] = True
        result = cal.find_free_slots(day, day, duration)

        expected: list[tuple[int, int]] = []
        run_start = None
        for minute in range(WORK_START, WORK_END + 1):
            free = minute < WORK_END and not busy[minute]
            if free and run_start is None:
                run_start = minute
            if not free and run_start is not None:
                if minute - run_start >= duration:
                    expected.append((run_start, minute))
                run_start = None
        got = [
            (int(s["start"][11:13]) * 60 + int(s["start"][14:16]), int(s["end"][11:13]) * 60 + int(s["end"][14:16]))
            for s in result["slots"]
        ]
        assert got == expected
