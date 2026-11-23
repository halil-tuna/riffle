"""PDF metin düzeltmeleri — pdf2docx'e (ve diğer dönüştürücülere) gitmeden önce.

1. Unicode'u olmayan glifler: ToUnicode tablosu olmayan sembol fontlarında (Elsevier'in
   MathematicalPi'si, Symbol vb.) μ, α, ± gibi harfler kontrol karakteri (\\x01) olarak gelir ve
   Word'de kaybolur. Fontun kodlama tablosundaki glif adından gerçek karakter bulunur.
2. Bağlı harfler (ﬁ, ﬂ, ﬀ …) → ayrı harfler: Word'de arama ve yazım denetimi çalışsın.
3. Satır sonu tireleri: "devel-" + "oped" → "developed". Gerçek birleşik kelimeler
   ("water-based") belgede tireli geçiyorsa ya da bilinen bir ek/önekse korunur.
4. Alt simge: TiO₂'deki "2" gibi küçük ve satırın altına inen parçalar alt simge olur
   (pdf2docx yalnızca üst simgeyi tanıyor).
"""

from __future__ import annotations

import contextlib
import re
from collections import Counter

from .actions import pymupdf

SUBSCRIPT_FLAG = 1 << 12          # pdf2docx'in kullanmadığı bir bayrak biti: "alt simge"

LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
             "ﬅ": "st", "ﬆ": "st"}

# Elsevier / Bitstream "MathematicalPi" fontlarının glif adları (H + kod) — standart listede yok.
# Küçük Yunan harfleri sıralı (H9262 = μ gerçek bir makalede doğrulandı); emin olunmayan kodlar
# bilerek eklenmedi: yanlış harf koymak, harfi hiç koymamaktan kötü.
_GREEK = "αβγδεζηθικλμνξοπρστυφχψω"
MATHPI = {f"H{9251 + i}": c for i, c in enumerate(_GREEK)}
_EXTRA_NAMES = {"mu": "μ", "mu1": "μ", "micro": "µ", "Omega": "Ω", "degree": "°", "minus": "−",
                "multiply": "×", "plusminus": "±", "lessequal": "≤", "greaterequal": "≥",
                "approxequal": "≈", "infinity": "∞", "arrowright": "→", "partialdiff": "∂",
                "summation": "∑", "radical": "√", "periodcentered": "·", "similar": "∼"}

# satır sonunda tiresi korunan birleşik kelime parçaları
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
    """Fontun kod → glif adı tablosu (/Differences ya da gömülü CFF/Type1 kodlaması)."""
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
    try:                                   # gömülü CFF'in kendi kodlaması
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
    """{font adı: {bozuk karakter: doğru karakter}} — yalnızca Unicode'u olmayan fontlar için."""
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
                    continue                    # yalnızca bozuk gelen (kontrol/özel alan) kodları düzelt
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
    """Satır sonundaki tire gerçek bir birleşik kelimenin mi?"""
    l, r = left.lower(), right.lower()
    joined, hyph = words[l + r], words[f"{l}-{r}"]
    if hyph > joined:
        return True
    if joined:
        return False
    if not right[:1].islower():
        return True                         # "Ti-6Al", "micro-EDM"
    return r in KEEP_SUFFIX or l in KEEP_PREFIX


# ---------------------------------------------------------------- rawdict düzeltme

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
        if len(ch) > 1:                     # tek glif → birden çok harf (nadiren)
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
        if dy > body * 0.08:                  # satırın altında → alt simge
            s["flags"] = (s["flags"] | SUBSCRIPT_FLAG) & ~1
        elif dy < -body * 0.2:                # satırın üstünde → üst simge
            s["flags"] |= 1


def _dehyphenate(blocks, words, stats):
    # satırlar blok sınırını da aşar: paragrafın son satırı bir sonraki blokta (sütun/şekil arası) sürebilir
    lines = [ln for b in blocks if b.get("type", 0) == 0 for ln in b.get("lines", [])]
    for a, b in zip(lines, lines[1:]):
        if not a.get("spans") or not b.get("spans"):
            continue
        spans = a["spans"]
        # satır sonundaki yalnızca-boşluk parçaları ("devel-" + " ") tireyi gizler
        n = len(spans)
        while n > 1 and "chars" in spans[n - 1] and not _span_text(spans[n - 1]).strip():
            n -= 1
        last = spans[n - 1]
        chars = last.get("chars")
        if not chars:
            continue
        # sondaki boşlukları atla
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
        del chars[k + 1:]                     # sondaki boşluklar
        del spans[n:]
        if chars[k]["c"] == "\u00ad" or not keep_hyphen(left.group(1), right.group(1), words):
            # görünmez işaret (U+2060): pdf2docx satırları birleştirirken araya boşluk koymaz
            # ("devel" + "oped"); Word dosyasından sonra silinir (actions._fix_layout_xml)
            chars[k] = dict(chars[k], c="\u2060")
            stats["hyph"] += 1


def _merge_symbol_spans(line, fixed_ids):
    """Kurtarılan sembolü (μ) yanındaki metin parçasına kat: "500 μs" ayrı fontta kalınca pdf2docx
    araya boşluk koyuyor ("μ s"). Metin fontu μ'yü zaten çizebiliyor."""
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
    """pdf2docx çalışırken Page.get_text('rawdict'/'dict') çıktısını düzelten bağlam.
    Kullanım: with patched_text(src) as stats: Converter(src).convert(...)"""
    with pymupdf.open(doc_path) as d:
        cmap = build_char_map(d)
        words = vocabulary(d) if dehyphenate else None
    stats: Counter = Counter()
    orig = pymupdf.Page.get_text

    from .parallel import check_cancel

    def get_text(self, option="text", *a, **kw):
        check_cancel()                            # İptal: pdf2docx her sayfada buradan geçer
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
    """pdf2docx'e alt simge desteği ekler; geri alma fonksiyonunu döndürür."""
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
    """pdf2docx satırları birleştirirken araya boşluğu 'c.encode().isalnum()' ile karar veriyor: Türkçe
    harfler (ı, ş, ğ, ü, ö, ç) UTF-8'de çok baytlı olduğu için harf sayılmıyor ve 'taraması' + 'bölümleri'
    → 'taramasıbölümleri' oluyordu. Aynı mantık, Unicode harf kontrolüyle."""
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
