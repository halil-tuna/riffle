"""Language support: Turkish (source strings) and English.

Every user-visible string in the source is written in Turkish and wrapped in T(...):

    T("Sıkıştırılıyor: {}", p.name)

In English the translation comes from `i18n_en.CATALOG`; if a translation is missing the Turkish text is
shown (the tests catch missing translations). Format placeholders ({} {0} {:.1f}) must appear unchanged
in the translation.

Language choice: RIFFLE_LANG (tests/screenshots) > Settings ("auto" | "tr" | "en") > system language.
If the system language is not Turkish, English is used.
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
    """Value stored in settings: 'auto' | 'tr' | 'en'."""
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
    """Change the language (None: determine it again from settings/system)."""
    global _lang
    _lang = code if code in LANGS else None


_NOUNS = ("page|file|word|image|picture|row|column|field|table|slide|sheet|core|result|item|document|part|"
          "figure|place|icon|copy|drawing|decoration|mark|character|line")


def _singular(text: str) -> str:
    """English: '1 pages' → '1 page' (Turkish has no plural after a number, so the source text has one form)."""
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
    """Output file name suffix ('_birlesik' → '_merged')."""
    return T(tr_suffix)
