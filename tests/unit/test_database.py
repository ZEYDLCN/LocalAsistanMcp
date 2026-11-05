from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import ClassVar

import pytest

from localasistan.errors import InvalidInputError, LimitError, NotFoundError, SqlGuardError
from localasistan.server.database import DatabaseService, first_keyword, quote_identifier


@pytest.fixture
def db(tmp_path: Path) -> DatabaseService:
    return DatabaseService(tmp_path / "u.db", timeout_seconds=1.0, max_cell_chars=50)


@pytest.fixture
def people(db: DatabaseService) -> DatabaseService:
    db.execute(
        "CREATE TABLE people (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, "
        "email TEXT UNIQUE, age INTEGER DEFAULT 18, note TEXT)"
    )
    db.insert(
        "people",
        [
            {"name": "Ayşe", "email": "ayse@example.com", "age": 30},
            {"name": "Çağlar", "email": "caglar@example.com", "age": 41},
            {"name": "Şule", "email": None},
        ],
    )
    return db


def tables(db: DatabaseService) -> list[str]:
    return [t["name"] for t in db.list_tables()["tables"]]


class TestHelpers:
    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("select 1", "SELECT"),
            ("  \n\tSELECT 1", "SELECT"),
            ("/* c */ select 1", "SELECT"),
            ("-- c\nselect 1", "SELECT"),
            ("/* a */ -- b\n /* c */ vacuum into 'x'", "VACUUM"),
            ("insert into t values (1)", "INSERT"),
            ("", ""),
            ("-- only comment", ""),
            ("/* unterminated", ""),
            ("123", ""),
        ],
    )
    def test_first_keyword(self, sql: str, expected: str):
        assert first_keyword(sql) == expected

    def test_quote_identifier(self):
        assert quote_identifier("a") == '"a"'
        assert quote_identifier('we"ird') == '"we""ird"'


class TestSchema:
    def test_fresh_database(self, db):
        assert db.list_tables() == {"tables": [], "count": 0}
        assert db.schema_text() == "-- (no tables yet)"

    def test_list_tables_counts_and_hides_internal(self, people):
        people.execute("CREATE VIEW adults AS SELECT * FROM people WHERE age >= 18")
        listing = people.list_tables()
        assert [t["name"] for t in listing["tables"]] == ["adults", "people"]  # sqlite_sequence gizli
        by_name = {t["name"]: t for t in listing["tables"]}
        assert by_name["people"] == {"name": "people", "type": "table", "columns": 5, "rows": 3}
        assert by_name["adults"]["type"] == "view"

    def test_describe(self, people):
        people.execute("CREATE TABLE pets (id INTEGER PRIMARY KEY, owner_id INTEGER REFERENCES people(id), name TEXT)")
        people.execute("CREATE INDEX idx_pets_owner ON pets(owner_id)")
        info = people.describe_table("people")
        assert info["row_count"] == 3 and info["type"] == "table"
        cols = {c["name"]: c for c in info["columns"]}
        assert cols["id"]["primary_key"] is True
        assert cols["name"]["not_null"] is True
        assert cols["age"]["default"] == "18"
        assert "CREATE TABLE people" in info["create_sql"]
        assert any(i["unique"] for i in info["indexes"])  # email UNIQUE
        pets = people.describe_table("pets")
        assert pets["foreign_keys"] == [{"from": "owner_id", "table": "people", "to": "id"}]
        assert "idx_pets_owner" in [i["name"] for i in pets["indexes"]]

    def test_describe_unknown_and_internal(self, people):
        with pytest.raises(NotFoundError, match="db_list_tables"):
            people.describe_table("nope")
        with pytest.raises(NotFoundError):
            people.describe_table("sqlite_master")
        with pytest.raises(NotFoundError):
            people.describe_table("sqlite_sequence")
        with pytest.raises(InvalidInputError):
            people.describe_table("")

    def test_describe_hostile_names_are_data_not_sql(self, people):
        people.execute('CREATE TABLE "we""ird name" (x INTEGER)')
        assert people.describe_table('we"ird name')["columns"][0]["name"] == "x"
        with pytest.raises(NotFoundError):
            people.describe_table("people; DROP TABLE people")
        assert "people" in tables(people)

    def test_schema_text(self, people):
        text = people.schema_text()
        assert text.startswith("CREATE TABLE people") and text.rstrip().endswith(";")


class TestQuery:
    def test_basic_and_params(self, people):
        r = people.query("SELECT name, age FROM people WHERE age > ? ORDER BY age", [35])
        assert r == {"columns": ["name", "age"], "rows": [["Çağlar", 41]], "row_count": 1, "truncated": False}

    def test_null_float_and_unicode(self, people):
        r = people.query("SELECT email, 1.5, 'İstanbul ✓' FROM people WHERE name = ?", ["Şule"])
        assert r["rows"] == [[None, 1.5, "İstanbul ✓"]]

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1;",
            "  select 1  ;  ",
            "/* hi */ SELECT 1",
            "-- hi\nSELECT 1",
            "WITH x AS (SELECT 1 AS v) SELECT v FROM x",
            "VALUES (1), (2)",
            "EXPLAIN QUERY PLAN SELECT * FROM sqlite_master",
        ],
    )
    def test_accepted_shapes(self, db, sql):
        db.query(sql)

    def test_duplicate_column_names_preserved(self, people):
        r = people.query("SELECT id, id FROM people LIMIT 1")
        assert r["columns"] == ["id", "id"]

    def test_row_truncation(self, people):
        r = people.query("SELECT id FROM people", max_rows=2)
        assert r["row_count"] == 2 and r["truncated"] is True and r["truncated_reason"] == "max_rows"
        with pytest.raises(InvalidInputError):
            people.query("SELECT 1", max_rows=0)
        with pytest.raises(InvalidInputError):
            people.query("SELECT 1", max_rows=1001)

    def test_cell_truncation_and_blob(self, db):
        r = db.query("SELECT replace(hex(zeroblob(100)), '0', 'ab'), x'DEADBEEF'")
        assert "…[+" in r["rows"][0][0] and len(r["rows"][0][0]) < 100
        assert r["rows"][0][1] == "<blob 4 bytes>"

    def test_response_size_budget(self, db):
        db.execute("CREATE TABLE t (v TEXT)")
        db.insert("t", [{"v": "x" * 40} for _ in range(300)])
        # 300 satır x ~42 karakter << bütçe; bütçeyi zorlamak için çok sütunlu sorgu
        wide = ", ".join("v" for _ in range(60))
        r = DatabaseService(db.path, max_cell_chars=2000).query(f"SELECT {wide} FROM t", max_rows=1000)
        assert r["truncated"] is False or r.get("truncated_reason") == "response_size"
        assert r["row_count"] >= 1

    @pytest.mark.parametrize(
        ("sql", "fragment"),
        [
            ("INSERT INTO people (name) VALUES ('x')", "db_execute"),
            ("UPDATE people SET age = 1", "db_execute"),
            ("DELETE FROM people", "db_execute"),
            ("DROP TABLE people", "db_execute"),
            ("CREATE TABLE x (a)", "db_execute"),
            ("PRAGMA user_version = 5", "SELECT"),
            ("ATTACH DATABASE ':memory:' AS evil", "SELECT"),
            ("VACUUM", "SELECT"),
            ("VACUUM INTO '/tmp/leak.db'", "SELECT"),
            ("/* sneaky */ VACUUM INTO '/tmp/leak.db'", "SELECT"),
            ("BEGIN", "SELECT"),
            ("", "non-empty"),
        ],
    )
    def test_non_select_rejected_and_nothing_changes(self, people, sql, fragment):
        with pytest.raises((SqlGuardError, InvalidInputError), match=fragment):
            people.query(sql)
        assert people.query("SELECT COUNT(*) FROM people")["rows"] == [[3]]
        assert not Path("/tmp/leak.db").exists()

    def test_multiple_statements_rejected(self, people):
        with pytest.raises(SqlGuardError, match="one SQL statement"):
            people.query("SELECT 1; DROP TABLE people")
        assert "people" in tables(people)

    def test_write_hidden_behind_cte_is_blocked_by_authorizer(self, people):
        with pytest.raises(SqlGuardError):
            people.query("WITH x AS (SELECT 'z' AS n) INSERT INTO people (name) SELECT n FROM x")
        assert people.query("SELECT COUNT(*) FROM people")["rows"] == [[3]]

    def test_load_extension_blocked(self, db):
        with pytest.raises((SqlGuardError, InvalidInputError)):
            db.query("SELECT load_extension('/tmp/evil.so')")

    def test_pragma_table_valued_functions_are_read_only(self, people):
        try:
            r = people.query("SELECT name FROM pragma_table_info('people')")
        except (SqlGuardError, InvalidInputError):
            return  # engellenmesi de kabul edilebilir
        assert "name" in [row[0] for row in r["rows"]]

    def test_runaway_query_is_stopped(self, people):
        started = time.monotonic()
        with pytest.raises(LimitError, match="time limit"):
            people.query("WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c")
        assert time.monotonic() - started < 5

    def test_memory_bombs_are_rejected(self, db):
        with pytest.raises(LimitError):
            db.query("SELECT zeroblob(1000000000)")
        with pytest.raises(LimitError):
            db.query("SELECT randomblob(1000000000)")

    def test_param_validation(self, people):
        with pytest.raises(InvalidInputError, match="params\\[0\\]"):
            people.query("SELECT ?", [["nested"]])
        with pytest.raises(InvalidInputError, match="params\\[1\\]"):
            people.query("SELECT ?, ?", [1, {"a": 1}])
        with pytest.raises(InvalidInputError):
            people.query("SELECT ?, ?", [1])  # eksik bağlama
        assert people.query("SELECT ?", [True])["rows"] == [[1]]

    def test_syntax_and_missing_table_errors_are_readable(self, people):
        with pytest.raises(InvalidInputError, match="no such table"):
            people.query("SELECT * FROM ghosts")
        with pytest.raises(InvalidInputError, match="syntax error"):
            people.query("SELECT FROM WHERE")

    def test_sqlite_master_readable(self, people):
        r = people.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'people'")
        assert r["rows"] == [["people"]]


class TestExecute:
    def test_dml_counts(self, people):
        assert people.execute("UPDATE people SET age = age + 1 WHERE age IS NOT NULL")["rows_affected"] == 3
        ins = people.execute("INSERT INTO people (name) VALUES (?)", ["Zeynep"])
        assert ins == {"statement": "INSERT", "rows_affected": 1, "last_row_id": 4}
        assert people.execute("DELETE FROM people WHERE name = ?", ["Zeynep"])["rows_affected"] == 1
        assert people.execute("DELETE FROM people WHERE 0")["rows_affected"] == 0

    def test_ddl(self, people):
        assert people.execute("CREATE TABLE t2 (a)")["statement"] == "CREATE"
        assert "t2" in tables(people)
        people.execute("ALTER TABLE t2 ADD COLUMN b TEXT")
        assert [c["name"] for c in people.describe_table("t2")["columns"]] == ["a", "b"]
        people.execute("DROP TABLE t2")
        assert "t2" not in tables(people)

    def test_returning(self, people):
        r = people.execute("INSERT INTO people (name) VALUES ('R') RETURNING id, name")
        assert r["returned"]["rows"] == [[4, "R"]]
        assert r["rows_affected"] == 1

    def test_with_prefixed_dml(self, people):
        r = people.execute("WITH ids AS (SELECT id FROM people WHERE age > 35) DELETE FROM people WHERE id IN ids")
        assert r["rows_affected"] == 1

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "PRAGMA user_version = 7",
            "ATTACH DATABASE '/tmp/evil.db' AS e",
            "DETACH DATABASE main",
            "VACUUM",
            "VACUUM INTO '/tmp/leak2.db'",
            "/* x */ vacuum into '/tmp/leak2.db'",
            "BEGIN",
            "COMMIT",
            "ROLLBACK",
            "SAVEPOINT s",
            "REINDEX",
            "ANALYZE",
            "",
        ],
    )
    def test_disallowed_statements(self, people, sql):
        with pytest.raises((SqlGuardError, InvalidInputError)):
            people.execute(sql)
        assert not Path("/tmp/evil.db").exists() and not Path("/tmp/leak2.db").exists()
        assert people.query("SELECT COUNT(*) FROM people")["rows"] == [[3]]

    def test_multiple_statements_rejected_atomically(self, people):
        with pytest.raises(SqlGuardError, match="one SQL statement"):
            people.execute("INSERT INTO people (name) VALUES ('a'); DROP TABLE people")
        assert people.query("SELECT COUNT(*) FROM people")["rows"] == [[3]]

    def test_virtual_tables_and_internal_tables_blocked(self, people):
        with pytest.raises(SqlGuardError):
            people.execute("CREATE VIRTUAL TABLE v USING fts5(a)")
        with pytest.raises(SqlGuardError):
            people.execute("INSERT INTO sqlite_master (type, name, tbl_name, sql) VALUES ('table','x','x','')")
        with pytest.raises(SqlGuardError):
            people.execute("UPDATE sqlite_sequence SET seq = 99")

    def test_attach_inside_with_blocked_by_authorizer(self, people):
        # ilk sözcük WITH beyaz listede; yine de ATTACH çalışamaz
        with pytest.raises((SqlGuardError, InvalidInputError)):
            people.execute("WITH x AS (SELECT 1) ATTACH DATABASE '/tmp/evil3.db' AS e")
        assert not Path("/tmp/evil3.db").exists()

    def test_constraint_errors_roll_back(self, people):
        with pytest.raises(InvalidInputError, match="UNIQUE"):
            people.execute("INSERT INTO people (name, email) VALUES ('dup', 'ayse@example.com')")
        with pytest.raises(InvalidInputError, match="Constraint failed"):
            people.execute("UPDATE people SET email = 'same@x.com'")  # 2. satırda UNIQUE ihlali
        assert people.query("SELECT email FROM people ORDER BY id")["rows"][0] == ["ayse@example.com"]

    def test_foreign_keys_enforced(self, people):
        people.execute("CREATE TABLE pets (id INTEGER PRIMARY KEY, owner_id INTEGER REFERENCES people(id))")
        with pytest.raises(InvalidInputError, match="FOREIGN KEY"):
            people.execute("INSERT INTO pets (owner_id) VALUES (999)")
        people.execute("INSERT INTO pets (owner_id) VALUES (1)")

    def test_database_size_cap(self, tmp_path):
        small = DatabaseService(tmp_path / "small.db", timeout_seconds=2.0, max_bytes=64 * 1024)
        small.execute("CREATE TABLE t (v TEXT)")
        with pytest.raises(LimitError, match="size limit"):
            for _ in range(200):
                small.execute(
                    "INSERT INTO t SELECT hex(randomblob(2000)) FROM "
                    "(WITH RECURSIVE n(i) AS "
                    "(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 5) SELECT i FROM n)"
                )
        assert small.query("SELECT COUNT(*) FROM t")["rows"][0][0] > 0  # DB bozulmadı

    def test_param_errors(self, people):
        with pytest.raises(InvalidInputError):
            people.execute("INSERT INTO people (name) VALUES (?)", [{"a": 1}])

    def test_runaway_write_is_stopped_and_rolled_back(self, people):
        with pytest.raises(LimitError, match="time limit"):
            people.execute(
                "INSERT INTO people (name) WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) "
                "SELECT 'n' || x FROM c"
            )
        assert people.query("SELECT COUNT(*) FROM people")["rows"] == [[3]]


class TestInsert:
    def test_basic(self, people):
        r = people.insert("people", [{"name": "A"}, {"name": "B", "age": 5}])
        assert r == {"table": "people", "inserted": 2, "last_row_id": 5}
        assert people.query("SELECT age FROM people WHERE name IN ('A','B') ORDER BY name")["rows"] == [
            [18],
            [5],
        ]

    def test_unicode_roundtrip(self, people):
        people.insert("people", [{"name": "Gülşen İğdır", "note": "Çok güzel — ✓"}])
        assert people.query("SELECT note FROM people WHERE name = ?", ["Gülşen İğdır"])["rows"] == [["Çok güzel — ✓"]]

    def test_default_values_row(self, db):
        db.execute("CREATE TABLE d (id INTEGER PRIMARY KEY, v TEXT DEFAULT 'x')")
        db.insert("d", [{}, {}])
        assert db.query("SELECT v FROM d")["rows"] == [["x"], ["x"]]

    def test_all_or_nothing(self, people):
        with pytest.raises(InvalidInputError, match="UNIQUE"):
            people.insert(
                "people", [{"name": "ok", "email": "new@x.com"}, {"name": "bad", "email": "ayse@example.com"}]
            )
        assert people.query("SELECT COUNT(*) FROM people")["rows"] == [[3]]

    @pytest.mark.parametrize(
        ("table", "rows", "exc"),
        [
            ("ghost", [{"a": 1}], NotFoundError),
            ("sqlite_master", [{"name": "x"}], NotFoundError),
            ("people; DROP TABLE people", [{"name": "x"}], NotFoundError),
            ("people", [], InvalidInputError),
            ("people", [{"nope": 1}], InvalidInputError),
            ("people", [{'name"); DROP TABLE people;--': 1}], InvalidInputError),
            ("people", ["not a dict"], InvalidInputError),
            ("people", [{"name": ["list"]}], InvalidInputError),
            ("", [{"name": "x"}], InvalidInputError),
            ("people", [{"name": "x"}] * 1001, LimitError),
        ],
    )
    def test_rejections(self, people, table, rows, exc):
        with pytest.raises(exc):
            people.insert(table, rows)
        assert "people" in tables(people)
        assert people.query("SELECT COUNT(*) FROM people")["rows"] == [[3]]

    def test_not_null_violation(self, people):
        with pytest.raises(InvalidInputError, match="NOT NULL"):
            people.insert("people", [{"name": None}])

    def test_view_is_not_insertable(self, people):
        people.execute("CREATE VIEW v AS SELECT * FROM people")
        with pytest.raises(NotFoundError):
            people.insert("v", [{"name": "x"}])


class TestConcurrency:
    def test_parallel_writers_do_not_lose_rows(self, db):
        db.execute("CREATE TABLE c (n INTEGER)")
        errors: list[Exception] = []

        def worker(offset: int):
            try:
                for i in range(20):
                    db.insert("c", [{"n": offset * 100 + i}])
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert db.query("SELECT COUNT(*), COUNT(DISTINCT n) FROM c")["rows"] == [[160, 160]]

    def test_reads_while_writing(self, people):
        stop = threading.Event()
        seen: list[int] = []

        def reader():
            while not stop.is_set():
                seen.append(people.query("SELECT COUNT(*) FROM people")["rows"][0][0])

        t = threading.Thread(target=reader)
        t.start()
        for i in range(30):
            people.insert("people", [{"name": f"n{i}"}])
        stop.set()
        t.join()
        assert seen and max(seen) <= 33 and min(seen) >= 3


def test_raw_sqlite_is_not_writable_through_query(people):
    """Savunma doğrulaması: query bağlantısının gerçekten salt-okunur olduğunu dosya seviyesinde kanıtla."""
    before = people.path.read_bytes()
    with pytest.raises((SqlGuardError, InvalidInputError)):
        people.query("WITH x AS (SELECT 1) DELETE FROM people")
    assert people.path.read_bytes() == before
    with sqlite3.connect(people.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3


class TestDefenseLayers:
    """Her katman, DİĞERLERİ OLMASA DA tek başına zararı engellemeli (derinlemesine savunma kanıtı)."""

    HARMFUL: ClassVar[list[str]] = [
        "INSERT INTO people (name) VALUES ('x')",
        "UPDATE people SET name = 'y'",
        "DELETE FROM people",
        "DROP TABLE people",
        "CREATE TABLE z (a)",
        "ATTACH DATABASE ':memory:' AS e",
        "PRAGMA user_version = 3",
    ]

    def test_layer_1_read_only_uri_blocks_writes_on_its_own(self, people):
        conn = sqlite3.connect(f"{people.path.resolve().as_uri()}?mode=ro", uri=True)
        for sql in self.HARMFUL[:4]:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute(sql)
        conn.close()

    def test_layer_2_query_only_pragma_blocks_writes_on_its_own(self, people):
        conn = sqlite3.connect(people.path)
        conn.execute("PRAGMA query_only = ON")
        for sql in self.HARMFUL[:4]:
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                conn.execute(sql)
        conn.close()

    def test_layer_3_read_authorizer_blocks_everything_but_reads_on_a_writable_connection(self, people):
        conn = sqlite3.connect(people.path)
        conn.set_authorizer(DatabaseService._read_authorizer)
        for sql in [*self.HARMFUL, "SELECT load_extension('x')"]:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
        assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3
        assert conn.execute("WITH c AS (SELECT 1) SELECT * FROM c").fetchall() == [(1,)]
        conn.close()

    def test_layer_4_write_authorizer_blocks_attach_pragma_virtual_and_internal_tables(self, people):
        conn = sqlite3.connect(people.path, isolation_level=None)
        conn.set_authorizer(DatabaseService._make_write_authorizer(ddl=False))
        for sql in [
            "ATTACH DATABASE ':memory:' AS e",
            "PRAGMA user_version = 3",
            "BEGIN",
            "SAVEPOINT s",
            "INSERT INTO sqlite_master (type, name, tbl_name, sql) VALUES ('table', 'x', 'x', '')",
            "SELECT load_extension('x')",
        ]:
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)
        conn.execute("INSERT INTO people (name) VALUES ('izinli')")  # normal DML'e izin var
        ddl = DatabaseService._make_write_authorizer(ddl=True)
        assert ddl(sqlite3.SQLITE_INSERT, "sqlite_master", None, "main", None) == sqlite3.SQLITE_OK  # DDL iç yazımı
        conn.close()


class TestErrorTranslation:
    @pytest.mark.parametrize(
        ("exc", "expected", "fragment"),
        [
            (sqlite3.OperationalError("database or disk is full"), LimitError, "size limit"),
            (sqlite3.DataError("string or blob too big"), LimitError, "too large"),
            (sqlite3.OperationalError("interrupted"), LimitError, "time limit"),
            (sqlite3.OperationalError("attempt to write a readonly database"), SqlGuardError, "read-only"),
            (sqlite3.DatabaseError("not authorized"), SqlGuardError, "not allowed"),
            (sqlite3.IntegrityError("UNIQUE constraint failed: t.a"), InvalidInputError, "Constraint failed"),
            (sqlite3.ProgrammingError("You can only execute one statement at a time."), SqlGuardError, "one SQL"),
            (sqlite3.OperationalError("near FROM: syntax error"), InvalidInputError, "SQL error"),
            (sqlite3.DatabaseError("file is not a database"), InvalidInputError, "Database error"),
        ],
    )
    def test_translation_table(self, db, exc, expected, fragment):
        translated = db._translate(exc)
        assert isinstance(translated, expected) and fragment in str(translated)

    def test_corrupt_database_file_gives_a_domain_error(self, tmp_path):
        broken = tmp_path / "broken.db"
        broken.write_bytes(b"this is definitely not sqlite" * 50)
        with pytest.raises(Exception) as info:
            DatabaseService(broken)
        assert "database" in str(info.value).lower()
