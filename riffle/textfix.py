"""PDF text fixes — before the text goes to pdf2docx (and the other converters).

1. Glyphs without Unicode: in symbol fonts with no ToUnicode table (Elsevier's
   MathematicalPi, Symbol etc.) letters like μ, α, ± arrive as control characters (\\x01) and
   disappear in Word. The real character is found from the glyph name in the font's encoding table.
2. Ligatures (ﬁ, ﬂ, ﬀ …) → separate letters, so search and spell check work in Word.
3. Line-end hyphens: "devel-" + "oped" → "developed". Real compound words
   ("water-based") are kept if they appear hyphenated in the document or are a known prefix/suffix.
4. Subscripts: small pieces that drop below the line, like the "2" in TiO₂, become subscripts
   (pdf2docx only recognises superscripts).
"""

from __future__ import annotations

import contextlib
import re
from collections import Counter

from .actions import pymupdf

SUBSCRIPT_FLAG = 1 << 12          # a flag bit pdf2docx does not use: "subscript"

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
             "ﬅ": "st", "ﬆ": "st",
             # A soft hyphen (U+00AD) drawn in a PDF is a visible hyphen: fonts like Arial that map hyphen and soft
             # hyphen to the same glyph put '\xad' in the text, and line-end joining
             # and compound words ('water-based') didn't see it
             "\u00ad": "-"}

# Glyph names of the Elsevier / Bitstream "MathematicalPi" fonts (H + code) — not in the standard list.
# Lowercase Greek letters are in sequence (H9262 = μ verified in a real paper); codes we were not sure of
# were left out on purpose: a wrong letter is worse than no letter.
_GREEK = "αβγδεζηθικλμνξοπρστυφχψω"
MATHPI = {f"H{9251 + i}": c for i, c in enumerate(_GREEK)}
_EXTRA_NAMES = {"mu": "μ", "mu1": "μ", "micro": "µ", "Omega": "Ω", "degree": "°", "minus": "−",
                "multiply": "×", "plusminus": "±", "lessequal": "≤", "greaterequal": "≥",
                "approxequal": "≈", "infinity": "∞", "arrowright": "→", "partialdiff": "∂",
                "summation": "∑", "radical": "√", "periodcentered": "·", "similar": "∼"}

# compound word parts that keep their hyphen at the end of a line
KEEP_SUFFIX = {"based", "free", "like", "related", "scale", "type", "specific", "dependent", "induced",
               "assisted", "controlled", "rich", "level", "wide", "known", "term", "made", "friendly",
               "rate", "layer", "phase", "state", "order", "time", "size", "point", "mode", "step",
               "sensitive", "resistant", "soluble", "oriented", "driven", "art", "up", "off", "in", "out"}
KEEP_PREFIX = {"non", "self", "semi", "multi", "anti", "quasi"}

_bad = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f-�]")


def _unicode_for(name: str) -> str | None:
    if not name or name == ".notdef":
        return None
    if name in MATHPI:
        return MATHPI[name]
    if name in _EXTRA_NAMES:
        return _EXTRA_NAMES[name]
    try:
        from fontTools import agl
        u = agl.toUnicode(name)
        if u and not _bad.search(u):
            return u
    except Exception:
        pass
    m = re.fullmatch(r"(?:uni|u)([0-9A-Fa-f]{4,6})", name)
    return chr(int(m.group(1), 16)) if m else None


def _code_names(doc, xref: int) -> dict[int, str]:
    """The font's code → glyph name table (/Differences or the embedded CFF/Type1 encoding)."""
    names: dict[int, str] = {}
    try:
        obj = doc.xref_object(xref)
    except Exception:
        return names
    enc = re.search(r"/Encoding\s+(\d+)\s+0\s+R", obj)
    diff_src = doc.xref_object(int(enc.group(1))) if enc else obj
    d = re.search(r"/Differences\s*\[(.*?)\]", diff_src, re.S)
    if d:
        code = 0
        for tok in re.findall(r"/[^\s/\[\]]+|\d+", d.group(1)):
            if tok.startswith("/"):
                names[code] = tok[1:]
                code += 1
            else:
                code = int(tok)
    if names:
        return names
    try:                                   # the embedded CFF's own encoding
        _, ext, _, buf = doc.extract_font(xref)
        if ext == "cff" and buf:
            import io
            from fontTools.cffLib import CFFFontSet
            cff = CFFFontSet()
            cff.decompile(io.BytesIO(buf), None)
            top = cff[cff.fontNames[0]]
            encv = getattr(top, "Encoding", None)
            if isinstance(encv, list):
                names = {i: n for i, n in enumerate(encv) if n and n != ".notdef"}
    except Exception:
        pass
    return names


def build_char_map(doc) -> dict[str, dict[str, str]]:
    """{font name: {broken character: correct character}} — only for fonts without Unicode."""
    out: dict[str, dict[str, str]] = {}
    seen = set()
    for page in doc:
        for xref, ext, typ, base, *_ in page.get_fonts(full=True):
            if xref in seen or not xref:
                continue
            seen.add(xref)
            names = _code_names(doc, xref)
            m = {}
            for code, name in names.items():
                if code >= 0x110000 or not _bad.match(chr(code)):
                    continue                    # fix only codes that arrive broken (control/private use area)
                u = _unicode_for(name)
                if u:
                    m[chr(code)] = u
            if m:
                short = base.split("+", 1)[-1]
                out.setdefault(short, {}).update(m)
    return out


def vocabulary(doc) -> Counter:
    words = Counter()
    for page in doc:
        for w in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", page.get_text()):
            words[w.lower()] += 1
    return words


def keep_hyphen(left: str, right: str, words: Counter) -> bool:
    """Is the hyphen at the end of the line part of a real compound word?"""
    l, r = left.lower(), right.lower()
    joined, hyph = words[l + r], words[f"{l}-{r}"]
    if hyph > joined:
        return True
    if joined:
        return False
    if not right[:1].islower():
        return True                         # "Ti-6Al", "micro-EDM"
    return r in KEEP_SUFFIX or l in KEEP_PREFIX


# ---------------------------------------------------------------- rawdict fixes

def _span_text(span) -> str:
    return "".join(c["c"] for c in span["chars"]) if "chars" in span else span.get("text", "")


def _fix_chars_raw(span, cmap):
    if "chars" not in span:
        t = span.get("text", "")
        if cmap:
            t = "".join(cmap.get(ch, ch) for ch in t)
        t = "".join(LIGATURES.get(ch, ch) for ch in t)
        span["text"] = _bad.sub("", t) if not cmap else t
        return
    new = []
    for c in span["chars"]:
        ch = c["c"]
        if cmap and ch in cmap:
            ch = cmap[ch]
        lig = LIGATURES.get(ch)
        if lig:
            x0, y0, x1, y1 = c["bbox"]
            w = (x1 - x0) / len(lig)
            for k, part in enumerate(lig):
                nc = dict(c)
                nc["c"] = part
                nc["bbox"] = (x0 + k * w, y0, x0 + (k + 1) * w, y1)
                nc["origin"] = (c["origin"][0] + k * w, c["origin"][1])
                new.append(nc)
            continue
        if len(ch) > 1:                     # one glyph → several letters (rare)
            ch = ch[0]
        c = dict(c)
        c["c"] = ch
        new.append(c)
    span["chars"] = new


def _mark_subscripts(line):
    spans = line.get("spans", [])
    if len(spans) < 2:
        return
    sizes = sorted(s["size"] for s in spans if _span_text(s).strip())
    if not sizes:
        return
    body = sizes[len(sizes) // 2]
    base_y = max((s["origin"][1] for s in spans if abs(s["size"] - body) < 0.5), default=None)
    if base_y is None:
        return
    for i, s in enumerate(spans):
        t = _span_text(s).strip()
        if not t or len(t) > 4 or s["size"] > body * 0.85:
            continue
        dy = s["origin"][1] - base_y
        if dy > body * 0.08:                  # below the line → subscript
            s["flags"] = (s["flags"] | SUBSCRIPT_FLAG) & ~1
        elif dy < -body * 0.2:                # above the line → superscript
            s["flags"] |= 1


def _dehyphenate(blocks, words, stats):
    # lines cross block boundaries too: the last line of a paragraph may continue in the next block (across a column/figure)
    lines = [ln for b in blocks if b.get("type", 0) == 0 for ln in b.get("lines", [])]
    for a, b in zip(lines, lines[1:]):
        if not a.get("spans") or not b.get("spans"):
            continue
        spans = a["spans"]
        # whitespace-only pieces at the end of a line ("devel-" + " ") hide the hyphen
        n = len(spans)
        while n > 1 and "chars" in spans[n - 1] and not _span_text(spans[n - 1]).strip():
            n -= 1
        last = spans[n - 1]
        chars = last.get("chars")
        if not chars:
            continue
        # skip the trailing spaces
        k = len(chars) - 1
        while k >= 0 and chars[k]["c"].isspace():
            k -= 1
        if k < 1 or chars[k]["c"] not in "-\u00ad\u2010":
            continue
        left = re.search(r"([^\W\d_]+)$", "".join(c["c"] for c in chars[:k]))
        first = next((s for s in b["spans"] if _span_text(s).strip()), None)
        right = re.match(r"\s*([^\W\d_]+)", _span_text(first)) if first else None
        if not left or not right:
            continue
        del chars[k + 1:]                     # trailing spaces
        del spans[n:]
        if chars[k]["c"] == "\u00ad" or not keep_hyphen(left.group(1), right.group(1), words):
            # invisible mark (U+2060): pdf2docx inserts no space when joining the lines
            # ("devel" + "oped"); removed from the Word file afterwards (actions._fix_layout_xml)
            chars[k] = dict(chars[k], c="\u2060")
            stats["hyph"] += 1


def _merge_symbol_spans(line, fixed_ids):
    """Merge the recovered symbol (μ) into the neighbouring text piece: when "500 μs" stays in a separate font,
    pdf2docx inserts a space ("μ s"). The text font can draw μ anyway."""
    spans = line.get("spans", [])
    i = 0
    while i < len(spans):
        s = spans[i]
        if id(s) in fixed_ids and "chars" in s:
            nxt = spans[i + 1] if i + 1 < len(spans) else None
            prv = spans[i - 1] if i > 0 else None
            tgt = next((t for t in (nxt, prv) if t is not None and "chars" in t
                        and id(t) not in fixed_ids and abs(t["size"] - s["size"]) < 0.6), None)
            if tgt is not None:
                if tgt is nxt:
                    tgt["chars"] = s["chars"] + tgt["chars"]
                else:
                    tgt["chars"] = tgt["chars"] + s["chars"]
                x0 = min(tgt["bbox"][0], s["bbox"][0])
                x1 = max(tgt["bbox"][2], s["bbox"][2])
                tgt["bbox"] = (x0, tgt["bbox"][1], x1, tgt["bbox"][3])
                del spans[i]
                continue
        i += 1


def fix_page_dict(raw: dict, cmap: dict, words: Counter | None, stats: Counter) -> dict:
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            fixed_ids = set()
            for s in line.get("spans", []):
                fm = cmap.get(s.get("font", "").split("+", 1)[-1])
                before = _span_text(s)
                _fix_chars_raw(s, fm)
                if fm and _span_text(s) != before:
                    stats["glyph"] += sum(1 for ch in before if ch in fm)
                    fixed_ids.add(id(s))
            if fixed_ids:
                _merge_symbol_spans(line, fixed_ids)
            _mark_subscripts(line)
    if words is not None:
        _dehyphenate(raw.get("blocks", []), words, stats)
    return raw


@contextlib.contextmanager
def patched_text(doc_path, dehyphenate: bool = True):
    """Context that fixes the output of Page.get_text('rawdict'/'dict') while pdf2docx runs.
    Usage: with patched_text(src) as stats: Converter(src).convert(...)"""
    with pymupdf.open(doc_path) as d:
        cmap = build_char_map(d)
        words = vocabulary(d) if dehyphenate else None
    stats: Counter = Counter()
    orig = pymupdf.Page.get_text

    from .parallel import check_cancel

    def get_text(self, option="text", *a, **kw):
        check_cancel()                            # Cancel: pdf2docx passes through here on every page
        res = orig(self, option, *a, **kw)
        if option in ("rawdict", "dict") and isinstance(res, dict):
            fix_page_dict(res, cmap, words, stats)
        return res

    patched_style = _patch_pdf2docx_subscript()
    patched_join = _patch_pdf2docx_line_join()
    pymupdf.Page.get_text = get_text
    try:
        yield stats
    finally:
        pymupdf.Page.get_text = orig
        if patched_style:
            patched_style()
        if patched_join:
            patched_join()


def _patch_pdf2docx_subscript():
    """Adds subscript support to pdf2docx; returns the function that undoes it."""
    try:
        from pdf2docx.text.TextSpan import TextSpan
    except Exception:
        return None
    orig = TextSpan._set_text_format

    def _set(self, docx_run):
        orig(self, docx_run)
        if self.flags & SUBSCRIPT_FLAG:
            docx_run.font.subscript = True
            docx_run.font.superscript = False

    TextSpan._set_text_format = _set

    def undo():
        TextSpan._set_text_format = orig
    return undo


def _patch_pdf2docx_line_join():
    """When joining lines, pdf2docx decides on the space with 'c.encode().isalnum()': Turkish
    letters (ı, ş, ğ, ü, ö, ç) are multi-byte in UTF-8, so they didn't count as letters and 'taraması' + 'bölümleri'
    → 'taramasıbölümleri'. Same logic, with a Unicode letter check."""
    try:
        import string
        from pdf2docx.text.Lines import Lines
        from pdf2docx.text.TextSpan import TextSpan
    except Exception:
        return None
    orig = Lines.adjust_last_word
    punc = "".join(c for c in string.punctuation if c != "-")

    def word_char(c):
        return bool(c) and (c.isalnum() or c in punc)

    def adjust_last_word(self, delete_end_line_hyphen: bool):
        for i, line in enumerate(self._instances[:-1]):
            end_span = line.spans[-1]
            if not isinstance(end_span, TextSpan) or not end_span.chars:
                continue
            end_char = end_span.chars[-1]
            start_span = self._instances[i + 1].spans[0]
            if not isinstance(start_span, TextSpan) or not start_span.chars:
                continue
            nxt = start_span.chars[0]
            if delete_end_line_hyphen and end_char.c.endswith("-") and nxt.c.islower():
                end_char.c = ""
            if word_char(end_char.c[-1:]) and word_char(nxt.c[:1]):
                end_char.c += " "

    Lines.adjust_last_word = adjust_last_word

    def undo():
        Lines.adjust_last_word = orig
    return undo
