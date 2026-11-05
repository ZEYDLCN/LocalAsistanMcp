"""Dosya servisi — MCP'den bağımsız, sandbox'a hapsedilmiş dosya işlemleri.

Tüm dönüş değerleri JSON'a çevrilebilir `dict`'tir. Tüm hatalar `LocalAsistanError`
alt sınıflarıdır (mesajlar mutlak yol içermez).
"""

from __future__ import annotations

import contextlib
import mimetypes
import os
import shutil
import tempfile
import time
import unicodedata
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..errors import (
    AlreadyExistsError,
    InvalidInputError,
    LimitError,
    LocalAsistanError,
    NotFoundError,
    SandboxError,
)
from ..sandbox import Sandbox
from ..textutil import fold_text

_BINARY_PROBE = 8192
_MAX_LINE_CHARS = 300
_GREP_DEADLINE_SECONDS = 8.0


class FileService:
    def __init__(self, sandbox: Sandbox, *, max_file_bytes: int = 5_000_000, timezone: str = "UTC") -> None:
        self.sandbox = sandbox
        self.max_file_bytes = max_file_bytes
        self._tz = ZoneInfo(timezone)

    # ------------------------------------------------------------------ yardımcılar
    def _iso(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=self._tz).isoformat(timespec="seconds")

    def _rel(self, resolved: Path) -> str:
        return self.sandbox.relative(resolved)

    @staticmethod
    def _translate(exc: OSError, shown: str) -> LocalAsistanError:
        if isinstance(exc, FileNotFoundError):
            return NotFoundError(f"'{shown}' does not exist.")
        if isinstance(exc, PermissionError):
            return LocalAsistanError(f"Permission denied for '{shown}'.")
        if isinstance(exc, IsADirectoryError):
            return InvalidInputError(f"'{shown}' is a directory.")
        if isinstance(exc, NotADirectoryError):
            return InvalidInputError(f"A parent of '{shown}' is not a directory.")
        if isinstance(exc, FileExistsError):
            return AlreadyExistsError(f"'{shown}' already exists.")
        return LocalAsistanError(f"Filesystem error for '{shown}': {exc.strerror or exc.__class__.__name__}.")

    @contextlib.contextmanager
    def _io(self, shown: str) -> Iterator[None]:
        try:
            yield
        except LocalAsistanError:
            raise
        except OSError as exc:
            raise self._translate(exc, shown) from exc

    def _existing(self, path: str, *, follow_final: bool = True) -> tuple[Path, str]:
        resolved = self.sandbox.resolve(path, follow_final=follow_final)
        shown = self._rel(resolved)
        if not os.path.lexists(resolved):
            raise NotFoundError(f"'{shown}' does not exist.")
        return resolved, shown

    @staticmethod
    def _looks_binary(sample: bytes) -> bool:
        return b"\x00" in sample

    def _check_size(self, nbytes: int, shown: str) -> None:
        if nbytes > self.max_file_bytes:
            raise LimitError(f"Content for '{shown}' is {nbytes} bytes; the limit is {self.max_file_bytes} bytes.")

    def _entry(self, resolved: Path, *, with_path: bool = True) -> dict:
        st = resolved.lstat()
        if resolved.is_symlink():
            kind = "symlink"
        elif resolved.is_dir():
            kind = "dir"
        else:
            kind = "file"
        entry: dict = {"path" if with_path else "name": self._rel(resolved), "type": kind}
        if kind == "file":
            entry["size_bytes"] = st.st_size
        entry["modified"] = self._iso(st.st_mtime)
        return entry

    # ------------------------------------------------------------------------ list
    def list_dir(
        self,
        path: str = ".",
        *,
        recursive: bool = False,
        include_hidden: bool = False,
        max_entries: int = 200,
    ) -> dict:
        if not 1 <= max_entries <= 1000:
            raise InvalidInputError("max_entries must be between 1 and 1000.")
        base, shown = self._existing(path)
        if not base.is_dir():
            raise InvalidInputError(f"'{shown}' is not a directory.")
        entries: list[dict] = []
        truncated = False
        with self._io(shown):
            stack = [base]
            while stack and not truncated:
                current = stack.pop(0)
                children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold()))
                for child in children:
                    if not include_hidden and child.name.startswith("."):
                        continue
                    if len(entries) >= max_entries:
                        truncated = True
                        break
                    entries.append(self._entry(child))
                    if recursive and child.is_dir() and not child.is_symlink():
                        stack.append(child)
        return {"path": shown, "entries": entries, "count": len(entries), "truncated": truncated}

    # ------------------------------------------------------------------------ read
    def _load_text(self, path: str) -> tuple[str, int, str]:
        """(metin, bayt boyutu, gösterilen yol) — ikili/UTF-8 dışı/büyük dosyaları reddeder."""
        resolved, shown = self._existing(path)
        with self._io(shown):
            if resolved.is_dir():
                raise InvalidInputError(f"'{shown}' is a directory; use fs_list instead.")
            size = resolved.stat().st_size
            if size > self.max_file_bytes:
                raise LimitError(
                    f"'{shown}' is {size} bytes; reading is limited to {self.max_file_bytes} bytes. "
                    "Use fs_grep to find the relevant part."
                )
            data = resolved.read_bytes()
        if self._looks_binary(data[:_BINARY_PROBE]):
            raise InvalidInputError(f"'{shown}' looks like a binary file; only text files can be read.")
        try:
            return data.decode("utf-8-sig"), size, shown
        except UnicodeDecodeError:
            raise InvalidInputError(f"'{shown}' is not valid UTF-8 text.") from None

    def read_all(self, path: str) -> str:
        """Dosyanın tüm metni (boyut sınırı içinde) — sunucu içi kullanım (ör. ICS içe aktarma)."""
        return self._load_text(path)[0]

    def read_text(self, path: str, *, offset: int = 0, max_chars: int = 20_000) -> dict:
        if offset < 0:
            raise InvalidInputError("offset must be >= 0.")
        if not 1 <= max_chars <= 100_000:
            raise InvalidInputError("max_chars must be between 1 and 100000.")
        text, size, shown = self._load_text(path)
        total = len(text)
        if offset > total:
            raise InvalidInputError(f"offset {offset} is beyond the end of the file ({total} characters).")
        chunk = text[offset : offset + max_chars]
        end = offset + len(chunk)
        truncated = end < total
        return {
            "path": shown,
            "size_bytes": size,
            "total_chars": total,
            "offset": offset,
            "returned_chars": len(chunk),
            "truncated": truncated,
            "next_offset": end if truncated else None,
            "content": chunk,
        }

    def info(self, path: str) -> dict:
        resolved, shown = self._existing(path, follow_final=False)
        with self._io(shown):
            entry = self._entry(resolved)
            if entry["type"] == "file":
                sample = resolved.read_bytes()[:_BINARY_PROBE] if entry["size_bytes"] else b""
                entry["is_text"] = not self._looks_binary(sample)
                mime, _ = mimetypes.guess_type(resolved.name)
                entry["mime"] = mime
            elif entry["type"] == "symlink":
                try:
                    target = resolved.resolve()
                    entry["target_inside_workspace"] = self.sandbox.root in (target, *target.parents)
                except OSError:
                    entry["target_inside_workspace"] = False
            return entry

    # ----------------------------------------------------------------------- write
    def _prepare_parent(self, resolved: Path, shown: str, create_parents: bool) -> None:
        parent = resolved.parent
        if parent.exists():
            if not parent.is_dir():
                raise InvalidInputError(f"A parent of '{shown}' is not a directory.")
        elif create_parents:
            parent.mkdir(parents=True, exist_ok=True)
        else:
            raise NotFoundError(f"The parent folder of '{shown}' does not exist.")

    def write_new(self, path: str, content: str, *, create_parents: bool = True) -> dict:
        resolved = self.sandbox.resolve(path)
        shown = self._rel(resolved)
        if self.sandbox.is_root(resolved):
            raise InvalidInputError("Cannot write to the workspace root; give a file name.")
        data = content.encode("utf-8")
        self._check_size(len(data), shown)
        with self._io(shown):
            if os.path.lexists(resolved):
                raise AlreadyExistsError(f"'{shown}' already exists. Use fs_overwrite to replace it.")
            self._prepare_parent(resolved, shown, create_parents)
            fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        return {"path": shown, "bytes_written": len(data), "created": True}

    def overwrite(self, path: str, content: str) -> dict:
        resolved, shown = self._existing(path)
        data = content.encode("utf-8")
        self._check_size(len(data), shown)
        with self._io(shown):
            if resolved.is_dir():
                raise InvalidInputError(f"'{shown}' is a directory.")
            previous = resolved.stat().st_size
            fd, tmp_name = tempfile.mkstemp(dir=resolved.parent, prefix=".la-tmp-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                shutil.copymode(resolved, tmp_name)
                os.replace(tmp_name, resolved)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name)
                raise
        return {"path": shown, "bytes_written": len(data), "previous_size_bytes": previous}

    def append(self, path: str, content: str, *, create_if_missing: bool = True) -> dict:
        resolved = self.sandbox.resolve(path)
        shown = self._rel(resolved)
        if self.sandbox.is_root(resolved):
            raise InvalidInputError("Cannot append to the workspace root; give a file name.")
        data = content.encode("utf-8")
        with self._io(shown):
            exists = os.path.lexists(resolved)
            if exists and resolved.is_dir():
                raise InvalidInputError(f"'{shown}' is a directory.")
            if not exists:
                if not create_if_missing:
                    raise NotFoundError(f"'{shown}' does not exist.")
                self._prepare_parent(resolved, shown, True)
            current = resolved.stat().st_size if exists else 0
            self._check_size(current + len(data), shown)
            with open(resolved, "ab") as handle:
                handle.write(data)
        return {
            "path": shown,
            "bytes_appended": len(data),
            "size_bytes": current + len(data),
            "created": not exists,
        }

    def make_dir(self, path: str, *, parents: bool = True) -> dict:
        resolved = self.sandbox.resolve(path)
        shown = self._rel(resolved)
        with self._io(shown):
            if resolved.is_dir():
                return {"path": shown, "created": False}
            if os.path.lexists(resolved):
                raise AlreadyExistsError(f"'{shown}' already exists and is not a directory.")
            if not parents and not resolved.parent.is_dir():
                raise NotFoundError(f"The parent folder of '{shown}' does not exist.")
            resolved.mkdir(parents=True, exist_ok=True)
        return {"path": shown, "created": True}

    # ------------------------------------------------------------------ move/delete
    def move(self, source: str, destination: str) -> dict:
        src, src_shown = self._existing(source, follow_final=False)
        dst = self.sandbox.resolve(destination, follow_final=False)
        dst_shown = self._rel(dst)
        if self.sandbox.is_root(src):
            raise InvalidInputError("Cannot move the workspace root.")
        if self.sandbox.is_root(dst):
            raise InvalidInputError("Destination must be a path inside the workspace, not its root.")
        with self._io(src_shown):
            if src.is_dir() and not src.is_symlink() and (dst == src or src in dst.parents):
                raise InvalidInputError("Cannot move a folder into itself.")
            if os.path.lexists(dst):
                case_only = (
                    src.parent == dst.parent
                    and src.name != dst.name
                    and src.name.casefold() == dst.name.casefold()
                    and os.path.samefile(src, dst)
                )
                if not case_only:
                    raise AlreadyExistsError(f"'{dst_shown}' already exists; choose another destination.")
            self._prepare_parent(dst, dst_shown, True)
            try:
                os.rename(src, dst)
            except OSError:
                shutil.move(str(src), str(dst))
        return {"from": src_shown, "to": dst_shown}

    def delete(self, path: str, *, recursive: bool = False) -> dict:
        resolved, shown = self._existing(path, follow_final=False)
        if self.sandbox.is_root(resolved):
            raise InvalidInputError("Cannot delete the workspace root.")
        with self._io(shown):
            if resolved.is_symlink() or resolved.is_file():
                kind = "symlink" if resolved.is_symlink() else "file"
                resolved.unlink()
                return {"deleted": shown, "type": kind, "items_removed": 1}
            # dizin
            children = list(resolved.iterdir())
            if not children:
                resolved.rmdir()
                return {"deleted": shown, "type": "dir", "items_removed": 1}
            if not recursive:
                raise InvalidInputError(
                    f"'{shown}' is a non-empty folder. Set recursive=true to delete it and its contents."
                )
            removed = 1 + sum(len(dirs) + len(files) for _, dirs, files in os.walk(resolved))
            shutil.rmtree(resolved)
            return {"deleted": shown, "type": "dir", "items_removed": removed}

    # ----------------------------------------------------------------------- search
    def search(self, pattern: str, path: str = ".", *, include_hidden: bool = False, max_results: int = 100) -> dict:
        if not pattern or not pattern.strip():
            raise InvalidInputError("pattern must not be empty.")
        if not 1 <= max_results <= 1000:
            raise InvalidInputError("max_results must be between 1 and 1000.")
        if os.path.isabs(pattern) or ".." in Path(pattern).parts or "\x00" in pattern:
            raise InvalidInputError("pattern must be a relative glob without '..' (e.g. '*.md' or '**/*.py').")
        base, shown = self._existing(path)
        if not base.is_dir():
            raise InvalidInputError(f"'{shown}' is not a directory.")
        matches: list[dict] = []
        truncated = False
        recursive_pattern = pattern if ("/" in pattern or "**" in pattern) else f"**/{pattern}"
        with self._io(shown):
            for found in sorted(base.glob(recursive_pattern), key=lambda p: str(p).casefold()):
                try:
                    rel_parts = found.relative_to(base).parts
                except ValueError:  # pragma: no cover - glob base dışına çıkmaz
                    continue
                if not include_hidden and any(part.startswith(".") for part in rel_parts):
                    continue
                try:
                    self.sandbox.resolve(str(found), follow_final=False)
                except SandboxError:
                    continue
                if len(matches) >= max_results:
                    truncated = True
                    break
                matches.append(self._entry(found))
        return {
            "pattern": pattern,
            "base": shown,
            "matches": matches,
            "count": len(matches),
            "truncated": truncated,
        }

    def grep(
        self,
        query: str,
        path: str = ".",
        *,
        case_sensitive: bool = False,
        file_glob: str = "*",
        max_matches: int = 100,
    ) -> dict:
        """Düz metin (regex DEĞİL) arama — ReDoS riskini baştan ortadan kaldırır."""
        if not query:
            raise InvalidInputError("query must not be empty.")
        if "\n" in query:
            raise InvalidInputError("query must be a single line.")
        if not 1 <= max_matches <= 1000:
            raise InvalidInputError("max_matches must be between 1 and 1000.")
        base, shown = self._existing(path)
        needle = query if case_sensitive else fold_text(query)
        matches: list[dict] = []
        scanned = skipped_binary = skipped_large = 0
        truncated = timed_out = False
        deadline = time.monotonic() + _GREP_DEADLINE_SECONDS

        def candidates() -> Iterator[Path]:
            if base.is_file():
                yield base
                return
            for root, dirs, files in os.walk(base, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    if name.startswith("."):
                        continue
                    candidate = Path(root) / name
                    if candidate.is_symlink():
                        continue
                    if file_glob != "*" and not candidate.match(file_glob):
                        continue
                    yield candidate

        with self._io(shown):
            for candidate in candidates():
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                if candidate.stat().st_size > self.max_file_bytes:
                    skipped_large += 1
                    continue
                data = candidate.read_bytes()
                if self._looks_binary(data[:_BINARY_PROBE]):
                    skipped_binary += 1
                    continue
                try:
                    text = data.decode("utf-8-sig")
                except UnicodeDecodeError:
                    skipped_binary += 1
                    continue
                scanned += 1
                for number, line in enumerate(text.splitlines(), start=1):
                    haystack = line if case_sensitive else fold_text(line)
                    if needle in haystack:
                        if len(matches) >= max_matches:
                            truncated = True
                            break
                        shown_line = line if len(line) <= _MAX_LINE_CHARS else line[:_MAX_LINE_CHARS] + "…"
                        matches.append(
                            {
                                "path": self._rel(candidate.resolve()),
                                "line": number,
                                "text": unicodedata.normalize("NFC", shown_line),
                            }
                        )
                if truncated:
                    break
        return {
            "query": query,
            "matches": matches,
            "count": len(matches),
            "files_scanned": scanned,
            "skipped_binary": skipped_binary,
            "skipped_too_large": skipped_large,
            "truncated": truncated,
            "timed_out": timed_out,
        }
