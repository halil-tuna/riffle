"""Dil desteği: Türkçe (kaynak metinler) ve İngilizce.

Kaynak koddaki bütün kullanıcıya görünen metinler Türkçe yazılır ve T(...) ile sarılır:

    T("Sıkıştırılıyor: {}", p.name)

İngilizcede çeviri `i18n_en.CATALOG`'dan gelir; çeviri yoksa Türkçe metin gösterilir (testler
eksik çeviriyi yakalar). Biçim yer tutucuları ({} {0} {:.1f}) çeviride de aynen bulunmalı.

Dil seçimi: RIFFLE_LANG (testler/ekran görüntüleri) > Ayarlar ("auto" | "tr" | "en") > sistem dili.
Sistem dili Türkçe değilse İngilizce.
"""

from __future__ import annotations

import os
import sys

LANGS = ("tr", "en")
_lang: str | None = None


def system_lang() -> str:
    if sys.platform == "win32":
        try:
            import ctypes
            lid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return "tr" if (lid & 0x3FF) == 0x1F else "en"
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        v = os.environ.get(var, "")
        if v:
            return "tr" if v.lower().startswith("tr") else "en"
    try:
        import locale
        loc = locale.getlocale()[0] or ""
        return "tr" if loc.lower().startswith(("tr", "turkish")) else "en"
    except Exception:
        return "en"


def preference() -> str:
    """Ayardaki değer: 'auto' | 'tr' | 'en'."""
    try:
        from .settings import load
        v = str(load().get("language", "auto")).lower()
        return v if v in LANGS else "auto"
    except Exception:
        return "auto"


def lang() -> str:
    global _lang
    if _lang is None:
        env = os.environ.get("RIFFLE_LANG", "").lower()
        if env in LANGS:
            _lang = env
        else:
            pref = preference()
            _lang = pref if pref in LANGS else system_lang()
    return _lang


def set_lang(code: str | None) -> None:
    """Dili değiştir (None: ayar/sistemden yeniden belirle)."""
    global _lang
    _lang = code if code in LANGS else None


_NOUNS = ("page|file|word|image|picture|row|column|field|table|slide|sheet|core|result|item|document|part|"
          "figure|place|icon|copy|drawing|decoration|mark|character|line")


def _singular(text: str) -> str:
    """İngilizcede '1 pages' → '1 page' (Türkçede sayıdan sonra çoğul eki yok, kaynak metin tek biçimli)."""
    import re
    text = re.sub(rf"(?<![\d.,])1 ({_NOUNS})s\b", r"1 \1", text)
    return text.replace("1 copies", "1 copy").replace("1 duplicate copies", "1 duplicate copy")


def T(msg: str, *args, **kwargs) -> str:
    en = lang() == "en"
    if en:
        from .i18n_en import CATALOG
        msg = CATALOG.get(msg, msg)
    if args or kwargs:
        try:
            msg = msg.format(*args, **kwargs)
        except (IndexError, KeyError, ValueError):
            msg = msg + " " + " ".join(map(str, args))
        if en:
            msg = _singular(msg)
    return msg


def suffix(tr_suffix: str) -> str:
    """Çıktı dosya adı eki ('_birlesik' → '_merged')."""
    return T(tr_suffix)
