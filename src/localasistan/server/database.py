"""Veritabanı servisi — LLM'e açılan SQLite (`userdata.db`).

Savunma katmanları:
1. `query` salt-okunur bağlantı (`mode=ro` + `query_only`) ve yalnızca SELECT-türü eylemlere
   izin veren `sqlite3` authorizer ile çalışır.
2. `execute` tek ifade kabul eder; ilk anahtar sözcük beyaz listede olmalıdır
   (VACUUM/ATTACH/PRAGMA/BEGIN... reddedilir); authorizer ATTACH/DETACH/PRAGMA/TRANSACTION/
   SAVEPOINT/sanal tablo ve `sqlite_*` yazımını engeller.
3. Her bağlantıda zaman aşımı (progress handler), uzunluk limitleri ve (yazma için) sayfa
   sayısı üst sınırı vardır.
4. Takvim verisi ayrı bir dosyadadır; LLM'in SQL'i ona erişemez.
"""

from __future__ import annotations

import contextlib
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from ..errors import InvalidInputError, LimitError, NotFoundError, SqlGuardError

# sqlite3 authorizer eylem kodları
_OK, _DENY = sqlite3.SQLITE_OK, sqlite3.SQLITE_DENY
_READ_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
}
_DENIED_WRITE_ACTIONS = {
    sqlite3.SQLITE_ATTACH,
    sqlite3.SQLITE_DETACH,
    sqlite3.SQLITE_PRAGMA,
    sqlite3.SQLITE_TRANSACTION,
    sqlite3.SQLITE_SAVEPOINT,
    sqlite3.SQLITE_CREATE_VTABLE,
    sqlite3.SQLITE_DROP_VTABLE,
}
_WRITE_TABLE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_QUERY_KEYWORDS = {"SELECT", "WITH", "VALUES", "EXPLAIN"}
_DDL_KEYWORDS = {"CREATE", "DROP", "ALTER"}
_EXECUTE_KEYWORDS = {"INSERT", "UPDATE", "DELETE", "REPLACE", "CREATE", "DROP", "ALTER", "WITH"}
_NOT_INTERNAL = "name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
_SCALAR = (str, int, float, bool, type(None))
_MAX_RESPONSE_CHARS = 200_000
_COMMENTS_AND_SPACE = re.compile(r"\s+|--[^\n]*(?:\n|$)|/\*.*?(?:\*/|$)", re.DOTALL)


def first_keyword(sql: str) -> str:
    """Yorum ve boşlukları atlayıp ilk anahtar sözcüğü (büyük harf) döndürür."""
    position = 0
    while position < len(sql):
        match = _COMMENTS_AND_SPACE.match(sql, position)
        if not match or match.end() == position:
            break
        position = match.end()
    word = re.match(r"[A-Za-z]+", sql[position:])
    return word.group(0).upper() if word else ""


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _validate_params(params: Sequence[Any] | None) -> list[Any]:
    values = list(params or [])
    for index, value in enumerate(values):
        if not isinstance(value, _SCALAR):
            raise InvalidInputError(
                f"params[{index}] must be a string, number, boolean or null (got {type(value).__name__})."
            )
    return values


class DatabaseService:
    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 5.0,
        max_bytes: int = 256 * 1024 * 1024,
        max_cell_chars: int = 2000,
    ) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.max_bytes = max_bytes
        self.max_cell_chars = max_cell_chars
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA user_version")  # dosyayı oluşturur / geçerli bir DB olduğunu doğrular

    # --------------------------------------------------------------------- bağlantılar
    @staticmethod
    def _apply_limits(conn: sqlite3.Connection) -> None:
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 5_000_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 200_000)
        conn.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, 200)
        conn.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, 50)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 1000)

    def _install_deadline(self, conn: sqlite3.Connection) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 1000)

    @contextlib.contextmanager
    def _read_connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=self.timeout_seconds
        )
        try:
            conn.isolation_level = None
            self._apply_limits(conn)
            conn.execute("PRAGMA query_only = ON")
            conn.set_authorizer(self._read_authorizer)
            self._install_deadline(conn)
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _internal_connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        """Authorizer'sız dahili bağlantı: yalnızca bu sınıfın kendi sabit sorguları için."""
        uri = f"{self.path.resolve().as_uri()}?mode={'rw' if write else 'ro'}"
        conn = sqlite3.connect(uri, uri=True, timeout=self.timeout_seconds)
        try:
            conn.isolation_level = None
            self._apply_limits(conn)
            self._install_deadline(conn)
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _read_authorizer(
        action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None
    ) -> int:
        if action in _READ_ACTIONS:
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
                return _DENY
            return _OK
        return _DENY

    @staticmethod
    def _make_write_authorizer(*, ddl: bool):
        """DDL (CREATE/DROP/ALTER) motorun kendi `sqlite_master` yazımlarına izin verir; DML vermez."""

        def authorize(
            action: int, arg1: str | None, arg2: str | None, dbname: str | None, source: str | None
        ) -> int:
            if action in _DENIED_WRITE_ACTIONS:
                return _DENY
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() == "load_extension":
                return _DENY
            if not ddl and action in _WRITE_TABLE_ACTIONS and (arg1 or "").lower().startswith("sqlite_"):
                return _DENY
            return _OK

        return authorize

    # ---------------------------------------------------------------------- çeviriler
    def _translate(self, exc: sqlite3.Error) -> Exception:
        message = str(exc)
        lowered = message.lower()
        if "not authorized" in lowered or "authoriz" in lowered:
            return SqlGuardError(
                "This statement is not allowed "
                "(ATTACH, PRAGMA, transactions and internal tables are blocked)."
            )
        if "interrupted" in lowered:
            return LimitError(
                f"The statement exceeded the {self.timeout_seconds:g}s time limit and was stopped."
            )
        if "database or disk is full" in lowered or "database is full" in lowered:
            return LimitError("The database size limit has been reached.")
        if "string or blob too big" in lowered or "too big" in lowered:
            return LimitError("A value or statement is too large.")
        if "attempt to write a readonly database" in lowered:
            return SqlGuardError("The database is read-only for this tool; use db_execute for changes.")
        if isinstance(exc, sqlite3.IntegrityError):
            return InvalidInputError(f"Constraint failed: {message}")
        if isinstance(exc, sqlite3.ProgrammingError) and "one statement at a time" in lowered:
            return SqlGuardError("Only one SQL statement per call is allowed.")
        if isinstance(exc, (sqlite3.OperationalError, sqlite3.ProgrammingError, sqlite3.DataError)):
            return InvalidInputError(f"SQL error: {message}")
        return InvalidInputError(f"Database error: {message}")

    def _cell(self, value: Any) -> Any:
        if isinstance(value, bytes):
            return f"<blob {len(value)} bytes>"
        if isinstance(value, str) and len(value) > self.max_cell_chars:
            return f"{value[: self.max_cell_chars]}…[+{len(value) - self.max_cell_chars} chars]"
        return value

    def _rows(self, cursor: sqlite3.Cursor, max_rows: int) -> dict:
        columns = [d[0] for d in cursor.description or []]
        rows: list[list[Any]] = []
        budget = _MAX_RESPONSE_CHARS
        truncated = False
        reason = None
        for raw in cursor:
            if len(rows) >= max_rows:
                truncated, reason = True, "max_rows"
                break
            row = [self._cell(v) for v in raw]
            budget -= sum(len(str(v)) + 2 for v in row)
            if budget < 0 and rows:
                truncated, reason = True, "response_size"
                break
            rows.append(row)
        result: dict[str, Any] = {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
        }
        if reason:
            result["truncated_reason"] = reason
        return result

    # --------------------------------------------------------------------- şema araçları
    def list_tables(self) -> dict:
        try:
            with self._internal_connection() as conn:
                found = conn.execute(
                    "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
                    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' ORDER BY name"
                ).fetchall()
                tables = []
                for name, kind in found:
                    ident = quote_identifier(name)
                    try:
                        count = conn.execute(f"SELECT COUNT(*) FROM {ident}").fetchone()[0]
                    except sqlite3.Error:
                        count = None
                    cols = len(conn.execute(f"PRAGMA table_info({ident})").fetchall())
                    tables.append({"name": name, "type": kind, "columns": cols, "rows": count})
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc
        return {"tables": tables, "count": len(tables)}

    def describe_table(self, name: str) -> dict:
        if not isinstance(name, str) or not name:
            raise InvalidInputError("table name must be a non-empty string.")
        try:
            with self._internal_connection() as conn:
                row = conn.execute(
                    "SELECT type, sql FROM sqlite_master WHERE name = ? AND type IN ('table','view') "
                    "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'",
                    (name,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(
                        f"Table or view '{name}' does not exist. Use db_list_tables to see what exists."
                    )
                kind, create_sql = row
                ident = quote_identifier(name)
                columns = [
                    {
                        "name": c[1],
                        "type": c[2],
                        "not_null": bool(c[3]),
                        "default": c[4],
                        "primary_key": bool(c[5]),
                    }
                    for c in conn.execute(f"PRAGMA table_info({ident})").fetchall()
                ]
                foreign_keys = [
                    {"from": f[3], "table": f[2], "to": f[4]}
                    for f in conn.execute(f"PRAGMA foreign_key_list({ident})").fetchall()
                ]
                indexes = [
                    {"name": i[1], "unique": bool(i[2])}
                    for i in conn.execute(f"PRAGMA index_list({ident})").fetchall()
                ]
                try:
                    count = conn.execute(f"SELECT COUNT(*) FROM {ident}").fetchone()[0]
                except sqlite3.Error:
                    count = None
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc
        return {
            "name": name,
            "type": kind,
            "create_sql": create_sql,
            "columns": columns,
            "foreign_keys": foreign_keys,
            "indexes": indexes,
            "row_count": count,
        }

    def schema_text(self) -> str:
        """Kaynak (resource) için tüm CREATE ifadeleri."""
        try:
            with self._internal_connection() as conn:
                rows = conn.execute(
                    f"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND {_NOT_INTERNAL} "
                    "ORDER BY type DESC, name"
                ).fetchall()
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc
        return "\n\n".join(f"{r[0]};" for r in rows) if rows else "-- (no tables yet)"

    # ------------------------------------------------------------------------ sorgular
    def query(self, sql: str, params: Sequence[Any] | None = None, *, max_rows: int = 100) -> dict:
        if not isinstance(sql, str) or not sql.strip():
            raise InvalidInputError("sql must be a non-empty string.")
        if not 1 <= max_rows <= 1000:
            raise InvalidInputError("max_rows must be between 1 and 1000.")
        keyword = first_keyword(sql)
        if keyword not in _QUERY_KEYWORDS:
            hint = " Use db_execute for changes." if keyword in _EXECUTE_KEYWORDS else ""
            raise SqlGuardError(
                f"db_query only accepts SELECT statements (got '{keyword or sql[:20]}').{hint}"
            )
        values = _validate_params(params)
        try:
            with self._read_connection() as conn:
                cursor = conn.execute(sql, values)
                return self._rows(cursor, max_rows)
        except sqlite3.Error as exc:
            raise self._translate(exc) from exc

    def execute(self, sql: str, params: Sequence[Any] | None = None, *, max_rows: int = 100) -> dict:
        if not isinstance(sql, str) or not sql.strip():
            raise InvalidInputError("sql must be a non-empty string.")
        keyword = first_keyword(sql)
        if keyword not in _EXECUTE_KEYWORDS:
            hint = " Use db_query for reads." if keyword in _QUERY_KEYWORDS else ""
            raise SqlGuardError(
                f"db_execute does not allow '{keyword or sql[:20]}' statements "
                f"(allowed: INSERT, UPDATE, DELETE, REPLACE, CREATE, DROP, ALTER).{hint}"
            )
        values = _validate_params(params)
        conn = self._open_write_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.set_authorizer(self._make_write_authorizer(ddl=keyword in _DDL_KEYWORDS))
            try:
                changes_before = conn.total_changes
                cursor = conn.execute(sql, values)
                returned = self._rows(cursor, max_rows) if cursor.description else None
                rowcount = cursor.rowcount
                if rowcount < 0:  # WITH ... ile başlayan DML'de Python rowcount vermez
                    rowcount = conn.total_changes - changes_before
                last_id = cursor.lastrowid
                cursor.close()  # yarım okunmuş RETURNING ifadesi COMMIT'i engellemesin
            finally:
                conn.set_authorizer(None)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.set_authorizer(None)
                conn.execute("ROLLBACK")
            raise self._translate(exc) from exc
        finally:
            conn.close()
        result: dict[str, Any] = {"statement": keyword, "rows_affected": max(rowcount, 0)}
        if keyword in ("INSERT", "REPLACE") and last_id:
            result["last_row_id"] = last_id
        if returned is not None:
            result["returned"] = returned
        return result

    def insert(self, table: str, rows: Sequence[dict[str, Any]]) -> dict:
        if not isinstance(table, str) or not table:
            raise InvalidInputError("table must be a non-empty string.")
        if not rows:
            raise InvalidInputError("rows must contain at least one object.")
        if len(rows) > 1000:
            raise LimitError("At most 1000 rows can be inserted per call.")
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise InvalidInputError(f"rows[{index}] must be an object of column: value pairs.")
            for column, value in row.items():
                if not isinstance(value, _SCALAR):
                    raise InvalidInputError(
                        f"rows[{index}].{column} must be a string, number, boolean or null."
                    )
        conn = self._open_write_connection()
        try:
            exists = conn.execute(
                f"SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? AND {_NOT_INTERNAL}",
                (table,),
            ).fetchone()
            if not exists:
                raise NotFoundError(
                    f"Table '{table}' does not exist. Use db_list_tables, or create it with db_execute."
                )
            ident = quote_identifier(table)
            known = {c[1] for c in conn.execute(f"PRAGMA table_info({ident})").fetchall()}
            for index, row in enumerate(rows):
                unknown = sorted(set(row) - known)
                if unknown:
                    raise InvalidInputError(
                        f"rows[{index}] has unknown column(s) {unknown}; table '{table}' has {sorted(known)}."
                    )
            conn.execute("BEGIN IMMEDIATE")
            conn.set_authorizer(self._make_write_authorizer(ddl=False))
            last_id = None
            try:
                for row in rows:
                    if row:
                        cols = ", ".join(quote_identifier(c) for c in row)
                        marks = ", ".join("?" for _ in row)
                        cursor = conn.execute(
                            f"INSERT INTO {ident} ({cols}) VALUES ({marks})", list(row.values())
                        )
                    else:
                        cursor = conn.execute(f"INSERT INTO {ident} DEFAULT VALUES")
                    last_id = cursor.lastrowid
            finally:
                conn.set_authorizer(None)
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.set_authorizer(None)
                conn.execute("ROLLBACK")
            raise self._translate(exc) from exc
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.set_authorizer(None)
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        return {"table": table, "inserted": len(rows), "last_row_id": last_id}

    # ------------------------------------------------------------------------ yazma bağlantısı
    def _open_write_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        try:
            conn.isolation_level = None
            self._apply_limits(conn)
            conn.execute("PRAGMA foreign_keys = ON")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            conn.execute(f"PRAGMA max_page_count = {max(self.max_bytes // page_size, 1)}")
            self._install_deadline(conn)
        except BaseException:
            conn.close()
            raise
        return conn
