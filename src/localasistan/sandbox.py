"""Dosya araçları için kök-hapsi (path sandbox).

Tüm yollar `Path.resolve()` ile gerçek (symlink'ler çözülmüş) konuma çevrilir ve kökün
içinde kalıp kalmadığı **çözülmüş yol üzerinden** denetlenir; bu sayede `..`, mutlak yol ve
symlink kaçışları aynı kuralla yakalanır. Çıktıdaki yollar her zaman köke göre, POSIX
biçiminde ve NFC-normalleştirilmiş (macOS'ta NFD dosya adları için) verilir.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

from .errors import InvalidInputError, SandboxError


class Sandbox:
    def __init__(self, root: Path, *, create: bool = True) -> None:
        root = Path(root).expanduser()
        if create:
            root.mkdir(parents=True, exist_ok=True)
        self.root: Path = root.resolve()

    # ------------------------------------------------------------------ çözümleme
    def resolve(self, path: str = ".", *, follow_final: bool = True) -> Path:
        """`path`'i köke hapsedilmiş gerçek mutlak yola çevirir.

        `follow_final=False`: son bileşen symlink ise **link'in kendisi** hedeflenir
        (silme/taşıma için; link hedefini değil linki işlemek gerekir).
        """
        if not isinstance(path, str):
            raise InvalidInputError("path must be a string.")
        if "\x00" in path:
            raise SandboxError("Path contains a NUL byte.")
        raw = Path(path) if path != "" else Path(".")
        candidate = raw if raw.is_absolute() else self.root / raw

        if not follow_final and candidate.name not in ("", ".", ".."):
            resolved = candidate.parent.resolve() / candidate.name
        else:
            resolved = candidate.resolve()

        if resolved != self.root and self.root not in resolved.parents:
            raise SandboxError(
                f"Path '{self._display_input(path)}' is outside the workspace. "
                "Use a path relative to the workspace root."
            )
        return resolved

    def is_root(self, resolved: Path) -> bool:
        return resolved == self.root

    def relative(self, resolved: Path) -> str:
        """Köke göre POSIX yolu ("." = kök), NFC normalleştirilmiş."""
        try:
            rel = resolved.relative_to(self.root)
        except ValueError:
            # symlink'in kendisi için parent.resolve() ile hesaplanan yol köktedir; buraya düşmemeli
            raise SandboxError("Path is outside the workspace.") from None
        text = rel.as_posix()
        return unicodedata.normalize("NFC", text) if text != "." else "."

    @staticmethod
    def _display_input(path: str) -> str:
        cleaned = path.replace("\n", " ")
        return cleaned if len(cleaned) <= 120 else cleaned[:117] + "..."
