from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

from localasistan.errors import InvalidInputError, SandboxError
from localasistan.sandbox import Sandbox


@pytest.fixture
def sb(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path / "ws")


def test_root_is_created_and_resolved(tmp_path: Path):
    box = Sandbox(tmp_path / "new" / "ws")
    assert box.root.is_dir()
    assert box.root == (tmp_path / "new" / "ws").resolve()


def test_root_through_symlink(tmp_path: Path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    box = Sandbox(link)
    assert box.root == real.resolve()
    assert box.resolve("a.txt") == real.resolve() / "a.txt"


@pytest.mark.parametrize("value", [".", "", "./", "a/..", "./a/../."])
def test_root_aliases(sb: Sandbox, value: str):
    assert sb.resolve(value) == sb.root


def test_nested_nonexistent_ok(sb: Sandbox):
    assert sb.resolve("a/b/c.txt") == sb.root / "a" / "b" / "c.txt"


@pytest.mark.parametrize(
    "value",
    ["..", "../x", "a/../../x", "a/b/../../../x", "/etc/passwd", "/", "~/../../etc", "a/../..//etc"],
)
def test_escape_attempts_rejected(sb: Sandbox, value: str):
    # "~" literal bir klasör adıdır (genişletilmez); ".." ile kaçış yine reddedilmeli
    with pytest.raises(SandboxError, match="outside the workspace"):
        sb.resolve(value)


def test_sibling_directory_with_same_prefix_rejected(tmp_path: Path):
    box = Sandbox(tmp_path / "ws")
    evil = tmp_path / "ws-evil"
    evil.mkdir()
    with pytest.raises(SandboxError):
        box.resolve(str(evil / "x.txt"))
    with pytest.raises(SandboxError):
        box.resolve("../ws-evil/x.txt")


def test_absolute_inside_allowed(sb: Sandbox):
    target = sb.root / "notes" / "a.txt"
    assert sb.resolve(str(target)) == target


def test_symlink_dir_escape_rejected(tmp_path: Path):
    box = Sandbox(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("s")
    (box.root / "door").symlink_to(outside)
    with pytest.raises(SandboxError):
        box.resolve("door")
    with pytest.raises(SandboxError):
        box.resolve("door/secret.txt")
    with pytest.raises(SandboxError):
        box.resolve("door/new.txt")


def test_symlink_file_escape_rejected_when_followed(tmp_path: Path):
    box = Sandbox(tmp_path / "ws")
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    (box.root / "peek.txt").symlink_to(secret)
    with pytest.raises(SandboxError):
        box.resolve("peek.txt")


def test_follow_final_false_targets_the_link_itself(tmp_path: Path):
    box = Sandbox(tmp_path / "ws")
    secret = tmp_path / "secret.txt"
    secret.write_text("s")
    link = box.root / "peek.txt"
    link.symlink_to(secret)
    resolved = box.resolve("peek.txt", follow_final=False)
    assert resolved == link.parent.resolve() / "peek.txt"
    assert resolved.is_symlink()
    assert box.relative(resolved) == "peek.txt"


def test_follow_final_false_still_checks_parent(tmp_path: Path):
    box = Sandbox(tmp_path / "ws")
    outside = tmp_path / "outside"
    outside.mkdir()
    (box.root / "door").symlink_to(outside)
    with pytest.raises(SandboxError):
        box.resolve("door/x.txt", follow_final=False)


def test_symlink_inside_to_inside_allowed(sb: Sandbox):
    (sb.root / "real").mkdir()
    (sb.root / "alias").symlink_to(sb.root / "real")
    assert sb.resolve("alias/x.txt") == sb.root / "real" / "x.txt"


def test_nul_and_type_errors(sb: Sandbox):
    with pytest.raises(SandboxError, match="NUL"):
        sb.resolve("a\x00b")
    with pytest.raises(InvalidInputError):
        sb.resolve(123)  # type: ignore[arg-type]


def test_error_does_not_leak_absolute_root(sb: Sandbox):
    with pytest.raises(SandboxError) as info:
        sb.resolve("../../etc/passwd")
    assert str(sb.root) not in str(info.value)


def test_long_input_truncated_in_message(sb: Sandbox):
    with pytest.raises(SandboxError) as info:
        sb.resolve("../" + "x" * 500)
    assert len(str(info.value)) < 300


def test_relative_posix_and_root(sb: Sandbox):
    assert sb.relative(sb.root) == "."
    assert sb.relative(sb.root / "a" / "b.txt") == "a/b.txt"


def test_relative_outside_raises(sb: Sandbox, tmp_path: Path):
    with pytest.raises(SandboxError):
        sb.relative(tmp_path / "elsewhere")


def test_turkish_names_and_nfc_normalisation(sb: Sandbox):
    nfd = unicodedata.normalize("NFD", "şükür-çalışma-ğüzel.txt")
    nfc = unicodedata.normalize("NFC", nfd)
    assert nfd != nfc
    resolved = sb.resolve(nfd)
    assert sb.relative(resolved) == nfc
    assert sb.relative(sb.resolve("İstanbul/Üsküdar.md")) == "İstanbul/Üsküdar.md"


def test_is_root(sb: Sandbox):
    assert sb.is_root(sb.resolve("."))
    assert not sb.is_root(sb.resolve("a"))
