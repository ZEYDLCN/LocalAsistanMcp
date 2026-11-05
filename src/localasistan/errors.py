"""Ortak hata tipleri.

`LocalAsistanError` ve alt sınıfları "kullanıcıya/LLM'e gösterilmesi güvenli" hatalardır:
mesajları bilinçli olarak yazılmıştır, yığın izi ya da mutlak yol sızdırmaz. Bunların dışındaki
her istisna beklenmeyen kabul edilir ve sunucu tarafında loglanır.
"""

from __future__ import annotations


class LocalAsistanError(Exception):
    """Gösterilmesi güvenli, beklenen hata."""


class ConfigError(LocalAsistanError):
    """Geçersiz yapılandırma."""


class InvalidInputError(LocalAsistanError):
    """Çağıranın verdiği argüman geçersiz."""


class NotFoundError(LocalAsistanError):
    """İstenen dosya/kayıt/tablo yok."""


class AlreadyExistsError(LocalAsistanError):
    """Hedef zaten var ve üzerine yazılmasına izin verilmedi."""


class SandboxError(LocalAsistanError):
    """Yol çalışma alanının dışına çıkıyor ya da geçersiz."""


class LimitError(LocalAsistanError):
    """Boyut / süre / satır sınırı aşıldı."""


class ConflictError(LocalAsistanError):
    """Takvimde çakışma gibi iş kuralı çatışması."""


class SqlGuardError(LocalAsistanError):
    """SQL ifadesi güvenlik kurallarına takıldı."""
