from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from localasistan.errors import (
    AlreadyExistsError,
    InvalidInputError,
    LimitError,
    NotFoundError,
    SandboxError,
)
from localasistan.sandbox import Sandbox
from localasistan.server.files import FileService


@pytest.fixture
def fs(tmp_path: Path) -> FileService:
    return FileService(Sandbox(tmp_path / "ws"), max_file_bytes=10_000, timezone="Europe/Istanbul")


@pytest.fixture
def root(fs: FileService) -> Path:
    return fs.sandbox.root


# ----------------------------------------------------------------------------- list
class TestList:
    def test_empty(self, fs: FileService):
        assert fs.list_dir() == {"path": ".", "entries": [], "count": 0, "truncated": False}

    def test_sorted_dirs_first_then_casefold(self, fs, root):
        (root / "b.txt").write_text("1")
        (root / "A.txt").write_text("1")
        (root / "zdir").mkdir()
        (root / "adir").mkdir()
        names = [e["path"] for e in fs.list_dir()["entries"]]
        assert names == ["adir", "zdir", "A.txt", "b.txt"]

    def test_entry_shape_and_timezone(self, fs, root):
        (root / "a.txt").write_text("hello")
        entry = fs.list_dir()["entries"][0]
        assert entry["type"] == "file" and entry["size_bytes"] == 5
        assert entry["modified"].endswith("+03:00")

    def test_hidden_excluded_unless_requested(self, fs, root):
        (root / ".secret").write_text("x")
        (root / "visible").write_text("x")
        assert [e["path"] for e in fs.list_dir()["entries"]] == ["visible"]
        assert {e["path"] for e in fs.list_dir(include_hidden=True)["entries"]} == {".secret", "visible"}

    def test_recursive(self, fs, root):
        (root / "a" / "b").mkdir(parents=True)
        (root / "a" / "b" / "deep.txt").write_text("x")
        paths = [e["path"] for e in fs.list_dir(recursive=True)["entries"]]
        assert "a" in paths and "a/b" in paths and "a/b/deep.txt" in paths

    def test_recursive_does_not_follow_symlink_dirs(self, fs, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("x")
        (root / "door").symlink_to(outside)
        result = fs.list_dir(recursive=True)
        assert [e["path"] for e in result["entries"]] == ["door"]
        assert result["entries"][0]["type"] == "symlink"

    def test_truncation(self, fs, root):
        for i in range(5):
            (root / f"f{i}.txt").write_text("x")
        result = fs.list_dir(max_entries=3)
        assert result["count"] == 3 and result["truncated"] is True

    def test_subdir_paths_are_relative_to_root(self, fs, root):
        (root / "docs").mkdir()
        (root / "docs" / "x.md").write_text("x")
        assert fs.list_dir("docs")["entries"][0]["path"] == "docs/x.md"

    def test_errors(self, fs, root):
        (root / "file.txt").write_text("x")
        with pytest.raises(NotFoundError):
            fs.list_dir("missing")
        with pytest.raises(InvalidInputError, match="not a directory"):
            fs.list_dir("file.txt")
        with pytest.raises(InvalidInputError):
            fs.list_dir(max_entries=0)
        with pytest.raises(SandboxError):
            fs.list_dir("..")

    def test_turkish_names(self, fs, root):
        (root / "Şükrü Çalışma.txt").write_text("x")
        assert fs.list_dir()["entries"][0]["path"] == "Şükrü Çalışma.txt"


# ----------------------------------------------------------------------------- read
class TestRead:
    def test_basic(self, fs, root):
        (root / "a.txt").write_text("merhaba dünya", encoding="utf-8")
        r = fs.read_text("a.txt")
        assert r["content"] == "merhaba dünya"
        assert r["truncated"] is False and r["next_offset"] is None
        assert r["total_chars"] == 13 and r["size_bytes"] == len("merhaba dünya".encode())

    def test_paging(self, fs, root):
        (root / "a.txt").write_text("0123456789")
        first = fs.read_text("a.txt", max_chars=4)
        assert first["content"] == "0123" and first["truncated"] and first["next_offset"] == 4
        second = fs.read_text("a.txt", offset=first["next_offset"], max_chars=4)
        assert second["content"] == "4567" and second["next_offset"] == 8
        third = fs.read_text("a.txt", offset=8, max_chars=4)
        assert third["content"] == "89" and third["truncated"] is False

    def test_empty_file(self, fs, root):
        (root / "e.txt").write_text("")
        r = fs.read_text("e.txt")
        assert r["content"] == "" and r["total_chars"] == 0

    def test_offset_at_end_is_empty_but_beyond_is_error(self, fs, root):
        (root / "a.txt").write_text("abc")
        assert fs.read_text("a.txt", offset=3)["content"] == ""
        with pytest.raises(InvalidInputError, match="beyond"):
            fs.read_text("a.txt", offset=4)

    def test_bom_stripped_and_crlf_preserved(self, fs, root):
        (root / "w.txt").write_bytes(b"\xef\xbb\xbfsatir1\r\nsatir2\r\n")
        assert fs.read_text("w.txt")["content"] == "satir1\r\nsatir2\r\n"

    def test_binary_rejected(self, fs, root):
        (root / "b.bin").write_bytes(b"\x89PNG\r\n\x00\x00data")
        with pytest.raises(InvalidInputError, match="binary"):
            fs.read_text("b.bin")

    def test_invalid_utf8_rejected(self, fs, root):
        (root / "latin.txt").write_bytes("çalışma".encode("cp1254"))
        with pytest.raises(InvalidInputError, match="UTF-8"):
            fs.read_text("latin.txt")

    def test_too_large(self, fs, root):
        (root / "big.txt").write_text("x" * 10_001)
        with pytest.raises(LimitError, match="fs_grep"):
            fs.read_text("big.txt")

    def test_directory_missing_and_invalid_params(self, fs, root):
        (root / "d").mkdir()
        with pytest.raises(InvalidInputError, match="directory"):
            fs.read_text("d")
        with pytest.raises(NotFoundError):
            fs.read_text("nope.txt")
        with pytest.raises(InvalidInputError):
            fs.read_text("d", offset=-1)
        with pytest.raises(InvalidInputError):
            fs.read_text("d", max_chars=0)
        with pytest.raises(InvalidInputError):
            fs.read_text("d", max_chars=100_001)

    def test_symlink_escape_blocked(self, fs, root, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET")
        (root / "peek.txt").symlink_to(secret)
        with pytest.raises(SandboxError):
            fs.read_text("peek.txt")

    def test_permission_denied_is_friendly(self, fs, root):
        target = root / "locked.txt"
        target.write_text("x")
        target.chmod(0)
        try:
            if os.access(target, os.R_OK):
                pytest.skip("root kullanıcısı izinleri aşar")
            with pytest.raises(Exception, match="Permission denied") as info:
                fs.read_text("locked.txt")
            assert str(root) not in str(info.value)
        finally:
            target.chmod(0o644)


# ----------------------------------------------------------------------------- info
class TestInfo:
    def test_file_dir_symlink(self, fs, root, tmp_path):
        (root / "a.md").write_text("# x")
        (root / "d").mkdir()
        outside = tmp_path / "o.txt"
        outside.write_text("x")
        (root / "link").symlink_to(outside)
        (root / "ilink").symlink_to(root / "a.md")
        a = fs.info("a.md")
        assert a["type"] == "file" and a["is_text"] is True and a["mime"] == "text/markdown"
        assert fs.info("d")["type"] == "dir"
        assert fs.info("link") == {**fs.info("link"), "type": "symlink", "target_inside_workspace": False}
        assert fs.info("ilink")["target_inside_workspace"] is True

    def test_binary_and_empty(self, fs, root):
        (root / "b.bin").write_bytes(b"\x00\x01")
        (root / "empty").write_text("")
        assert fs.info("b.bin")["is_text"] is False
        assert fs.info("empty")["is_text"] is True

    def test_missing(self, fs):
        with pytest.raises(NotFoundError):
            fs.info("zzz")


# ---------------------------------------------------------------------------- write
class TestWriteNew:
    def test_create(self, fs, root):
        r = fs.write_new("notes/today.md", "merhaba\n")
        assert r == {"path": "notes/today.md", "bytes_written": 8, "created": True}
        assert (root / "notes" / "today.md").read_text() == "merhaba\n"

    def test_unicode_content_bytes(self, fs, root):
        r = fs.write_new("t.txt", "şğıİöç")
        assert r["bytes_written"] == len("şğıİöç".encode())
        assert (root / "t.txt").read_text(encoding="utf-8") == "şğıİöç"

    def test_existing_rejected_and_untouched(self, fs, root):
        (root / "a.txt").write_text("old")
        with pytest.raises(AlreadyExistsError, match="fs_overwrite"):
            fs.write_new("a.txt", "new")
        assert (root / "a.txt").read_text() == "old"

    def test_no_parents(self, fs):
        with pytest.raises(NotFoundError, match="parent"):
            fs.write_new("x/y.txt", "1", create_parents=False)

    def test_parent_is_file(self, fs, root):
        (root / "f").write_text("x")
        with pytest.raises(InvalidInputError):
            fs.write_new("f/y.txt", "1")

    def test_root_and_dir_targets(self, fs, root):
        (root / "d").mkdir()
        with pytest.raises(InvalidInputError, match="root"):
            fs.write_new(".", "x")
        with pytest.raises(AlreadyExistsError):
            fs.write_new("d", "x")

    def test_size_limit(self, fs):
        with pytest.raises(LimitError):
            fs.write_new("big.txt", "x" * 10_001)
        assert fs.write_new("ok.txt", "x" * 10_000)["bytes_written"] == 10_000

    def test_escape_attempts(self, fs, root, tmp_path):
        with pytest.raises(SandboxError):
            fs.write_new("../evil.txt", "x")
        with pytest.raises(SandboxError):
            fs.write_new(str(tmp_path / "evil.txt"), "x")
        (root / "door").symlink_to(tmp_path)
        with pytest.raises(SandboxError):
            fs.write_new("door/evil.txt", "x")
        assert not (tmp_path / "evil.txt").exists()

    def test_dangling_symlink_to_outside_is_not_written_through(self, fs, root, tmp_path):
        target = tmp_path / "newfile.txt"
        (root / "evil").symlink_to(target)
        with pytest.raises(SandboxError):
            fs.write_new("evil", "pwned")
        assert not target.exists()

    def test_dangling_symlink_inside_resolves_to_its_inside_target(self, fs, root):
        (root / "dangling").symlink_to(root / "nowhere")
        r = fs.write_new("dangling", "x")
        assert r["path"] == "nowhere"  # link çözülür; hedef çalışma alanı içinde olduğu için güvenli
        assert (root / "nowhere").read_text() == "x"


class TestOverwrite:
    def test_replaces_atomically_without_leftovers(self, fs, root):
        (root / "a.txt").write_text("old content")
        r = fs.overwrite("a.txt", "new")
        assert r == {"path": "a.txt", "bytes_written": 3, "previous_size_bytes": 11}
        assert (root / "a.txt").read_text() == "new"
        assert [p.name for p in root.iterdir()] == ["a.txt"]

    def test_preserves_mode(self, fs, root):
        target = root / "run.sh"
        target.write_text("#!/bin/sh")
        target.chmod(0o755)
        fs.overwrite("run.sh", "#!/bin/sh\necho hi")
        assert stat.S_IMODE(target.stat().st_mode) == 0o755

    def test_errors(self, fs, root):
        (root / "d").mkdir()
        with pytest.raises(NotFoundError):
            fs.overwrite("missing.txt", "x")
        with pytest.raises(InvalidInputError):
            fs.overwrite("d", "x")
        (root / "a.txt").write_text("keep")
        with pytest.raises(LimitError):
            fs.overwrite("a.txt", "x" * 10_001)
        assert (root / "a.txt").read_text() == "keep"
        assert sorted(p.name for p in root.iterdir()) == ["a.txt", "d"]

    def test_symlink_escape_blocked(self, fs, root, tmp_path):
        secret = tmp_path / "secret.txt"
        secret.write_text("keep")
        (root / "peek.txt").symlink_to(secret)
        with pytest.raises(SandboxError):
            fs.overwrite("peek.txt", "pwned")
        assert secret.read_text() == "keep"


class TestAppend:
    def test_create_then_append(self, fs, root):
        a = fs.append("log/app.log", "satır 1\n")
        assert a["created"] is True and a["size_bytes"] == len("satır 1\n".encode())
        b = fs.append("log/app.log", "satır 2\n")
        assert b["created"] is False
        assert (root / "log" / "app.log").read_text() == "satır 1\nsatır 2\n"

    def test_no_create(self, fs):
        with pytest.raises(NotFoundError):
            fs.append("x.txt", "1", create_if_missing=False)

    def test_limit_counts_existing_size(self, fs, root):
        (root / "a.txt").write_text("x" * 9_990)
        with pytest.raises(LimitError):
            fs.append("a.txt", "y" * 11)
        assert fs.append("a.txt", "y" * 10)["size_bytes"] == 10_000

    def test_dir_and_root(self, fs, root):
        (root / "d").mkdir()
        with pytest.raises(InvalidInputError):
            fs.append("d", "x")
        with pytest.raises(InvalidInputError):
            fs.append(".", "x")


class TestMakeDir:
    def test_nested_and_idempotent(self, fs, root):
        assert fs.make_dir("a/b/c") == {"path": "a/b/c", "created": True}
        assert (root / "a" / "b" / "c").is_dir()
        assert fs.make_dir("a/b/c") == {"path": "a/b/c", "created": False}

    def test_file_in_the_way_and_no_parents(self, fs, root):
        (root / "f").write_text("x")
        with pytest.raises(AlreadyExistsError):
            fs.make_dir("f")
        with pytest.raises(NotFoundError):
            fs.make_dir("x/y", parents=False)


# ------------------------------------------------------------------------ move/delete
class TestMove:
    def test_file_and_auto_parents(self, fs, root):
        (root / "a.txt").write_text("x")
        assert fs.move("a.txt", "archive/2026/a.txt") == {"from": "a.txt", "to": "archive/2026/a.txt"}
        assert not (root / "a.txt").exists()
        assert (root / "archive" / "2026" / "a.txt").read_text() == "x"

    def test_rename_directory(self, fs, root):
        (root / "old").mkdir()
        (root / "old" / "f.txt").write_text("x")
        fs.move("old", "new")
        assert (root / "new" / "f.txt").exists()

    def test_destination_exists(self, fs, root):
        (root / "a.txt").write_text("a")
        (root / "b.txt").write_text("b")
        with pytest.raises(AlreadyExistsError):
            fs.move("a.txt", "b.txt")
        assert (root / "a.txt").read_text() == "a" and (root / "b.txt").read_text() == "b"

    def test_folder_into_itself(self, fs, root):
        (root / "d" / "sub").mkdir(parents=True)
        with pytest.raises(InvalidInputError, match="into itself"):
            fs.move("d", "d/sub/d")

    def test_missing_root_and_escape(self, fs, root, tmp_path):
        (root / "a.txt").write_text("x")
        with pytest.raises(NotFoundError):
            fs.move("nope", "x")
        with pytest.raises(InvalidInputError):
            fs.move(".", "x")
        with pytest.raises(InvalidInputError):
            fs.move("a.txt", ".")
        with pytest.raises(SandboxError):
            fs.move("a.txt", "../out.txt")
        with pytest.raises(SandboxError):
            fs.move("../x", "y")
        assert (root / "a.txt").exists()

    def test_moves_symlink_not_target(self, fs, root, tmp_path):
        outside = tmp_path / "o.txt"
        outside.write_text("outside")
        (root / "link").symlink_to(outside)
        fs.move("link", "moved-link")
        assert (root / "moved-link").is_symlink()
        assert outside.read_text() == "outside"


class TestDelete:
    def test_file_and_empty_dir(self, fs, root):
        (root / "a.txt").write_text("x")
        (root / "d").mkdir()
        assert fs.delete("a.txt") == {"deleted": "a.txt", "type": "file", "items_removed": 1}
        assert fs.delete("d")["type"] == "dir"
        assert list(root.iterdir()) == []

    def test_non_empty_needs_recursive(self, fs, root):
        (root / "d" / "e").mkdir(parents=True)
        (root / "d" / "e" / "f.txt").write_text("x")
        with pytest.raises(InvalidInputError, match="recursive=true"):
            fs.delete("d")
        assert (root / "d").exists()
        r = fs.delete("d", recursive=True)
        assert r["items_removed"] == 3 and not (root / "d").exists()

    def test_root_and_missing(self, fs, root):
        with pytest.raises(InvalidInputError, match="root"):
            fs.delete(".", recursive=True)
        with pytest.raises(InvalidInputError, match="root"):
            fs.delete("a/..", recursive=True)
        with pytest.raises(NotFoundError):
            fs.delete("zzz")
        assert root.exists()

    def test_symlink_deletes_link_only(self, fs, root, tmp_path):
        outside = tmp_path / "o.txt"
        outside.write_text("keep")
        (root / "link").symlink_to(outside)
        assert fs.delete("link")["type"] == "symlink"
        assert not os.path.lexists(root / "link") and outside.read_text() == "keep"

    def test_symlinked_dir_not_followed_by_recursive_delete(self, fs, root, tmp_path):
        outside = tmp_path / "outdir"
        outside.mkdir()
        (outside / "precious.txt").write_text("keep")
        (root / "d").mkdir()
        (root / "d" / "door").symlink_to(outside)
        fs.delete("d", recursive=True)
        assert (outside / "precious.txt").read_text() == "keep"

    def test_delete_outside_blocked(self, fs, tmp_path):
        victim = tmp_path / "victim.txt"
        victim.write_text("x")
        with pytest.raises(SandboxError):
            fs.delete("../victim.txt")
        with pytest.raises(SandboxError):
            fs.delete(str(victim))
        assert victim.exists()


# --------------------------------------------------------------------- search / grep
class TestSearch:
    @pytest.fixture(autouse=True)
    def _tree(self, root):
        (root / "docs").mkdir()
        (root / "docs" / "a.md").write_text("a")
        (root / "docs" / "deep").mkdir()
        (root / "docs" / "deep" / "b.md").write_text("b")
        (root / "top.md").write_text("t")
        (root / "code.py").write_text("c")
        (root / ".hidden.md").write_text("h")
        (root / ".git").mkdir()
        (root / ".git" / "x.md").write_text("g")

    def test_bare_pattern_is_recursive(self, fs):
        paths = [m["path"] for m in fs.search("*.md")["matches"]]
        assert paths == ["docs/a.md", "docs/deep/b.md", "top.md"]

    def test_explicit_subpattern(self, fs):
        assert [m["path"] for m in fs.search("docs/*.md")["matches"]] == ["docs/a.md"]
        assert [m["path"] for m in fs.search("**/*.py")["matches"]] == ["code.py"]

    def test_hidden_flag(self, fs):
        paths = [m["path"] for m in fs.search("*.md", include_hidden=True)["matches"]]
        assert ".hidden.md" in paths and ".git/x.md" in paths

    def test_search_in_subdir(self, fs):
        result = fs.search("*.md", "docs")
        assert result["base"] == "docs" and result["count"] == 2

    def test_truncation(self, fs):
        r = fs.search("*.md", max_results=2)
        assert r["count"] == 2 and r["truncated"] is True

    @pytest.mark.parametrize("pattern", ["../*", "/etc/*", "docs/../../*", "", "  "])
    def test_bad_patterns(self, fs, pattern):
        with pytest.raises(InvalidInputError):
            fs.search(pattern)

    def test_symlink_dir_results_filtered(self, fs, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.md").write_text("x")
        (root / "door").symlink_to(outside)
        assert all("leak" not in m["path"] for m in fs.search("*.md")["matches"])
        assert all("leak" not in m["path"] for m in fs.search("door/*.md")["matches"])

    def test_base_errors(self, fs):
        with pytest.raises(NotFoundError):
            fs.search("*.md", "nope")
        with pytest.raises(InvalidInputError):
            fs.search("*.md", "top.md")
        with pytest.raises(InvalidInputError):
            fs.search("*.md", max_results=0)


class TestGrep:
    @pytest.fixture(autouse=True)
    def _tree(self, root):
        (root / "a.txt").write_text("Merhaba Dünya\nİkinci satır\nÇALIŞMA notları\n", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "b.md").write_text("hello world\nHello again\nregex.*chars\n")
        (root / "bin.dat").write_bytes(b"hello\x00binary")
        (root / "latin.txt").write_bytes("hello çalışma".encode("cp1254"))
        (root / ".hidden.txt").write_text("hello hidden")
        (root / "big.txt").write_text("hello " + "x" * 10_000)

    def test_case_insensitive_default_across_files(self, fs):
        r = fs.grep("hello")
        found = {(m["path"], m["line"]) for m in r["matches"]}
        assert found == {("sub/b.md", 1), ("sub/b.md", 2)}
        assert r["skipped_binary"] == 2 and r["skipped_too_large"] == 1

    def test_case_sensitive(self, fs):
        r = fs.grep("Hello", case_sensitive=True)
        assert [(m["path"], m["line"]) for m in r["matches"]] == [("sub/b.md", 2)]

    def test_turkish_casefold(self, fs, root):
        # "ÇALIŞMA" (büyük I) araması "çalışma" (noktasız ı) ile eşleşmeli ve tersi
        assert [(m["path"], m["line"]) for m in fs.grep("çalışma")["matches"]] == [("a.txt", 3)]
        assert [(m["path"], m["line"]) for m in fs.grep("ÇALIŞMA")["matches"]] == [("a.txt", 3)]
        assert [(m["path"], m["line"]) for m in fs.grep("ikinci")["matches"]] == [("a.txt", 2)]
        (root / "en.txt").write_text("INFO: started\n")
        assert fs.grep("info", "en.txt")["count"] == 1

    def test_regex_metacharacters_are_literal(self, fs):
        assert fs.grep("regex.*chars")["count"] == 1
        assert fs.grep(".*")["count"] == 1  # yalnızca "regex.*chars" satırı
        assert fs.grep("(a+)+$")["count"] == 0  # ReDoS deseni düz metin olarak aranır

    def test_file_glob_and_single_file(self, fs):
        assert fs.grep("hello", file_glob="*.txt")["count"] == 0
        assert fs.grep("merhaba", "a.txt")["count"] == 1
        assert fs.grep("hello", "sub", file_glob="*.md")["count"] == 2

    def test_hidden_skipped(self, fs):
        assert all(".hidden" not in m["path"] for m in fs.grep("hidden")["matches"])

    def test_max_matches_truncation(self, fs):
        r = fs.grep("e", "sub", max_matches=2)
        assert r["count"] == 2 and r["truncated"] is True

    def test_long_lines_truncated(self, fs, root):
        (root / "long.txt").write_text("needle " + "y" * 1000)
        text = fs.grep("needle", "long.txt")["matches"][0]["text"]
        assert len(text) == 301 and text.endswith("…")

    @pytest.mark.parametrize("query", ["", "a\nb"])
    def test_bad_query(self, fs, query):
        with pytest.raises(InvalidInputError):
            fs.grep(query)

    def test_bad_limits_and_missing_path(self, fs):
        with pytest.raises(InvalidInputError):
            fs.grep("x", max_matches=0)
        with pytest.raises(NotFoundError):
            fs.grep("x", "missing")
        with pytest.raises(SandboxError):
            fs.grep("x", "..")

    def test_symlinks_not_followed(self, fs, root, tmp_path):
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.txt").write_text("needle leak")
        (root / "door").symlink_to(outside)
        (root / "peek.txt").symlink_to(outside / "leak.txt")
        assert fs.grep("needle")["count"] == 0
