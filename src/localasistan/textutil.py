"""Metin yardımcıları."""

from __future__ import annotations

import unicodedata


def fold_text(value: str) -> str:
    """Büyük/küçük harf ve Türkçe i/ı/İ/I farkını yok sayan arama anahtarı.

    Python'un `casefold()` işlevi `I`'yı `i`'ye çevirir; Türkçe'de ise `I` ↔ `ı`.
    Aramalarda "ÇALIŞMA" ile "çalışma" (ve "INFO" ile "info") eşleşsin diye dört
    varyantın tümü `i` olarak katlanır (arama için kabul edilebilir bir gevşeklik).
    """
    text = unicodedata.normalize("NFC", value)
    # "i̇" (i + birleştirici nokta) Python'un lower()/casefold() ile İ'den ürettiği biçimdir
    return text.replace("i\u0307", "i").replace("İ", "i").replace("ı", "i").casefold()
