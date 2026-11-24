"""Turkish OCR with Tesseract.

Two uses:
  1) make_searchable(): adds an INVISIBLE text layer on top of scanned pages.
     The image stays as it is; the text becomes selectable, copyable and searchable.
  2) text_page(): turns a scanned page into a new page with the OCR text written VISIBLY
     at its original positions. PDF → Word turns this into editable text.

Tesseract is a separate program (Apache-2.0):
  Windows: https://github.com/UB-Mannheim/tesseract/wiki  (select the "Turkish" language when installing)
  Linux:   sudo apt install tesseract-ocr tesseract-ocr-tur
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from .i18n import T

try:
    import pymupdf
except ImportError:  # pragma: no cover
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:          # so install.py can load it before the libraries are installed
        pymupdf = None  # type: ignore

OCR_DPI = 300
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


class OcrUnavailable(Exception):
    pass


def find_tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            cand = Path(base) / "Tesseract-OCR" / "tesseract.exe"
            if cand.exists():
                return str(cand)
    return None


def languages(exe: str) -> str:
    r = subprocess.run([exe, "--list-langs"], capture_output=True, timeout=30, creationflags=_NO_WINDOW)
    have = set((r.stdout + r.stderr).decode("utf-8", "ignore").split())
    picked = [l for l in ("tur", "eng") if l in have]
    if not picked:
        raise OcrUnavailable(T("Tesseract'ta Türkçe/İngilizce dil paketi yok."))
    return "+".join(picked)


def require() -> tuple[str, str]:
    exe = find_tesseract()
    if not exe:
        raise OcrUnavailable(
            T("OCR için Tesseract gerekli (ücretsiz).\n"
            "Windows: github.com/UB-Mannheim/tesseract/wiki → kurarken 'Turkish' dilini işaretle.\n"
            "Linux: sudo apt install tesseract-ocr tesseract-ocr-tur"))
    return exe, languages(exe)


# ------------------------------------------------------------------ OCR core

@dataclass
class Word:
    text: str
    rect: "pymupdf.Rect"   # PDF coordinates (pt)
    conf: float
    line_key: tuple


def _tesseract(exe: str, langs: str, img, page) -> list[Word]:
    with tempfile.TemporaryDirectory(prefix="riffle_ocr_") as tmp:
        path = Path(tmp) / "s.png"
        img.save(path)
        r = subprocess.run([exe, str(path), "stdout", "-l", langs, "--psm", "3", "--dpi", str(OCR_DPI), "tsv"],
                           capture_output=True, timeout=300, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise OcrUnavailable(r.stderr.decode("utf-8", "ignore")[:300])
    scale = 72.0 / OCR_DPI
    ox, oy = page.rect.x0, page.rect.y0
    words = []
    for row in r.stdout.decode("utf-8", "ignore").splitlines()[1:]:
        f = row.split("\t")
        if len(f) < 12 or f[0] != "5":
            continue
        text = f[11].strip()
        conf = float(f[10])
        if not text or conf < 0:
            continue
        x, y, w, h = (int(v) for v in f[6:10])
        rect = pymupdf.Rect(ox + x * scale, oy + y * scale, ox + (x + w) * scale, oy + (y + h) * scale)
        words.append(Word(text, rect, conf, (int(f[2]), int(f[3]), int(f[4]))))
    return words


def _quality(words: list[Word]) -> float:
    """Result score: sum of the confidence of reliable (≥60) words. More correct words = higher."""
    return sum(w.conf for w in words if w.conf >= 60 and len(w.text) > 1)


def _mean_conf(words: list[Word]) -> float:
    ws = [w.conf for w in words if len(w.text) > 1]
    return sum(ws) / len(ws) if ws else 0.0


def clean_background(img, strong: bool = False):
    """Removes shadows/uneven light and paper colour: divides by a background estimate (large maximum + blur)
    and stretches the contrast; median first for noise. Helps with phone photos and faint photocopies.
    strong: stronger smoothing for grainy/noisy faint scans."""
    import numpy as np
    from PIL import ImageFilter
    im = img.filter(ImageFilter.GaussianBlur(1)).filter(ImageFilter.MedianFilter(5)) if strong \
        else img.filter(ImageFilter.MedianFilter(3))
    a = np.asarray(im, dtype=np.float32)
    bg = np.asarray(im.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.GaussianBlur(25)), dtype=np.float32)
    n = np.clip(a / np.maximum(bg, 1) * 255, 0, 255)
    lo, hi = np.percentile(n, 1), np.percentile(n, 99.5)
    n = np.clip((n - lo) / max(hi - lo, 1) * 255, 0, 255)
    from PIL import Image
    return Image.fromarray(n.astype(np.uint8))


def binarize(img):
    """Turns the cleaned image black and white with an Otsu threshold (for very faint scans)."""
    import numpy as np
    from PIL import Image
    n = np.asarray(clean_background(img, strong=True))
    p = np.bincount(n.ravel(), minlength=256).astype(float)
    p /= p.sum()
    w = np.cumsum(p)
    mu = np.cumsum(p * np.arange(256))
    sb = (mu[-1] * w - mu) ** 2 / np.maximum(w * (1 - w), 1e-9)
    t = int(np.argmax(sb))
    return Image.fromarray(((n > t) * 255).astype(np.uint8))


GOOD_CONF = 88


def _needs_cleaning(img) -> bool:
    """Is there a shadow/uneven light or low contrast? (Tesseract confidently 'ignores' lines in shadow:
    the average confidence stays high while words go missing, so this is checked separately.)"""
    import numpy as np
    from PIL import ImageFilter
    small = img.resize((max(1, img.width // 4), max(1, img.height // 4)))
    bg = np.asarray(small.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(8)), dtype=np.float32)
    # on the strided full image, not the downscaled one: thin fonts (Arial)
    # were averaged to grey when downscaling and made a clean page look 'faint'
    a = np.asarray(img, dtype=np.float32)[::3, ::3]
    uneven = np.percentile(bg, 95) - np.percentile(bg, 5)
    contrast = np.percentile(a, 99) - np.percentile(a, 1)
    return uneven > 30 or contrast < 120


def _ocr_words(exe: str, langs: str, page) -> list[Word]:
    """Raw image first; if the result is weak (shadowed, faint, taken with a phone) the cleaned images are
    tried too and the best result is used. No extra cost on clean scans."""
    from PIL import Image
    pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
    best = _tesseract(exe, langs, img, page)
    if _mean_conf(best) >= GOOD_CONF and len(best) >= 5 and not _needs_cleaning(img):
        return best
    for prep in (clean_background, binarize):
        try:
            cand = _tesseract(exe, langs, prep(img), page)
        except OcrUnavailable:
            raise
        except Exception:
            continue
        if _quality(cand) > _quality(best) * 1.02:
            best = cand
        if _mean_conf(best) >= GOOD_CONF and len(best) >= 5:
            break
    return best


def ocr_pages(doc, pages: list[int], progress=lambda m: None) -> dict[int, list[Word]]:
    exe, langs = require()
    out: dict[int, list[Word]] = {}

    # MuPDF documents can't be shared between threads → instead of rendering the pages first and
    # OCR-ing them one by one, we give each thread its own copy of the document.
    data = doc.tobytes()

    def job_isolated(i):
        with pymupdf.open("pdf", data) as d:
            return i, _ocr_words(exe, langs, d[i])

    workers = max(1, min(4, (os.cpu_count() or 2)))
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for i, words in ex.map(job_isolated, pages):
            out[i] = words
            done += 1
            progress(T("OCR: sayfa {0}/{1}", done, len(pages)))
    return out


def needs_ocr(page) -> bool:
    """Does the page have no extractable text but visual content?"""
    if len(page.get_text().strip()) >= 10:
        return False
    return bool(page.get_images()) or bool(page.get_drawings())


# ------------------------------------------------------------------ font

# The bundled Liberation Sans first: exactly the same widths as Arial (text rebuilt from a scan
# fits the original line width) and the same result on every computer. Windows' Arial comes
# only as a last resort: in Arial space and non-breaking space (U+00A0), hyphen and soft hyphen (U+00AD)
# share a glyph; once embedded in the PDF, copying/searching gave 'Başlık\xa01' instead of 'Başlık 1'.
_BUNDLED = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_FONT_CANDIDATES = [
    (os.path.join(_BUNDLED, "LiberationSans-Regular.ttf"), os.path.join(_BUNDLED, "LiberationSans-Bold.ttf")),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
]


def _font_file(bold: bool = False) -> str | None:
    for reg, bd in _FONT_CANDIDATES:
        if os.path.exists(reg):
            return bd if bold and os.path.exists(bd) else reg
    return None


def _font(bold: bool = False):
    f = _font_file(bold)
    return (pymupdf.Font(fontfile=f), f) if f else (pymupdf.Font("hebo" if bold else "helv"), None)


# ------------------------------------------------------------------ 1) searchable PDF

HANDWRITING_MIN_CONF = 80


def make_searchable(doc, words_by_page: dict[int, list[Word]], report: dict | None = None) -> int:
    """Adds an invisible text layer. Returns the number of words added.

    On pages that look handwritten (not is_text_page) Tesseract mostly produces meaningless words;
    to avoid wrong search hits and garbage when copying, only very reliable words are added there.
    If report is given, {"handwritten": page count, "dropped": dropped words} is filled in.
    """
    font, ffile = _font()
    n = 0
    hand = dropped = 0
    for pno, words in words_by_page.items():
        page = doc[pno]
        tw = pymupdf.TextWriter(page.rect)
        strict = len(words) >= 5 and not is_text_page(words)
        hand += strict
        for w in words:
            if w.conf < 20:
                continue
            if strict and (w.conf < HANDWRITING_MIN_CONF or len(w.text) < 2 or not any(c.isalnum() for c in w.text)):
                dropped += 1
                continue
            length = font.text_length(w.text, fontsize=1)
            if length <= 0:
                continue
            size = max(1.0, w.rect.width / length)  # fit to the word width → the selection lines up correctly
            try:
                tw.append((w.rect.x0, w.rect.y1 - 0.18 * w.rect.height), w.text, font=font, fontsize=size)
                n += 1
            except Exception:
                pass
        tw.write_text(page, render_mode=3)  # 3 = invisible
    from .actions import fix_tounicode
    fix_tounicode(doc)                      # so 'ı' doesn't become '1' when searching/copying in Acrobat
    if report is not None:
        report.update(handwritten=hand, dropped=dropped)
    return n


# ------------------------------------------------------------------ 2) text page

_JUNK = set("|¦_—–-=~·•.,'`\"")


def _clean(words: list[Word]) -> list[Word]:
    """Drop table border leftovers ('|', '_' etc.) and single characters with very low confidence."""
    out = []
    for w in words:
        t = w.text.strip("|¦")
        if not t or all(c in _JUNK for c in t):
            continue
        if len(t) == 1 and w.conf < 40 and not t.isalnum():
            continue
        if t != w.text:
            w = Word(t, w.rect, w.conf, w.line_key)
        out.append(w)
    return out


def _lines(words: list[Word], vrules=()) -> list[tuple[str, "pymupdf.Rect"]]:
    """Splits OCR lines into pieces at large gaps and at vertical table lines
    (cell boundaries)."""
    groups: dict[tuple, list[Word]] = {}
    for w in _clean(words):
        groups.setdefault(w.line_key, []).append(w)
    segs = []
    for ws in groups.values():
        ws.sort(key=lambda w: w.rect.x0)
        h = sorted(w.rect.height for w in ws)[len(ws) // 2]
        cur = [ws[0]]
        for prev, w in zip(ws, ws[1:]):
            cy = (w.rect.y0 + w.rect.y1) / 2
            crosses = any(prev.rect.x1 - 1 <= x <= w.rect.x0 + 1 and y0 - 2 <= cy <= y1 + 2
                          for x, y0, y1 in vrules)
            if w.rect.x0 - prev.rect.x1 > 1.6 * h or crosses:   # column gap / cell boundary
                segs.append(cur)
                cur = [w]
            else:
                cur.append(w)
        segs.append(cur)
    lines = []
    for ws in segs:
        r = pymupdf.Rect(ws[0].rect)
        for w in ws[1:]:
            r |= w.rect
        lines.append((" ".join(w.text for w in ws), r))
    lines.sort(key=lambda l: (round(l[1].y0), l[1].x0))
    return lines


def is_text_page(words: list[Word]) -> bool:
    """Mostly printed text and reliable? Otherwise the page should stay an image.

    Determined by measurement (bench): on printed scans the good-word ratio is 0.85-0.96, mean
    confidence 94-96; on handwriting the ratio is 0.24-0.41, confidence 38-55. In between → image (safe).
    """
    if len(words) < 20:
        return False
    multi = [w for w in words if len(w.text) > 1]        # single-character symbols ('—', '1') don't count
    if len(multi) < 15:
        return False
    ratio = sum(1 for w in multi if w.conf >= 50) / len(multi)
    mean = sum(max(w.conf, 0) for w in words) / len(words)
    return ratio >= 0.7 and mean >= 80


RULE_DPI = 100


def detect_rules(page, words: list[Word] | None = None) -> list[tuple]:
    """Finds long horizontal/vertical lines (table borders) on a scanned page.

    Returns: [(x0, y0, x1, y1), ...] in PDF coordinates. Empty list without numpy.
    """
    try:
        import numpy as np
    except ImportError:
        return []
    pix = page.get_pixmap(dpi=RULE_DPI, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
    dark = a < 170
    scale = 72.0 / RULE_DPI
    ox, oy = page.rect.x0, page.rect.y0
    # Blank out the word boxes: dense text lines should not be taken for rules
    for w in words or []:
        x0 = int((w.rect.x0 - ox) / scale) + 1
        x1 = int((w.rect.x1 - ox) / scale) - 1
        y0 = int((w.rect.y0 - oy) / scale) + 1
        y1 = int((w.rect.y1 - oy) / scale) - 1
        if x1 > x0 and y1 > y0:
            dark[max(y0, 0):y1, max(x0, 0):x1] = False
    # tolerance for skewed/faint scans: widen by ±2 px across the line
    def widen(m, axis):
        out = m.copy()
        for k in (1, 2):
            out |= np.roll(m, k, axis=axis) | np.roll(m, -k, axis=axis)
        return out
    dark_h = widen(dark, 0).astype(np.int32)   # widen vertically for horizontal lines
    dark_v = widen(dark, 1).astype(np.int32)   # widen horizontally for vertical lines
    rules = []

    def runs(mask_2d, L, horizontal):
        # positions that are continuously dark for L pixels along each row (horizontally)
        m = mask_2d if horizontal else mask_2d.T
        c = np.cumsum(np.pad(m, ((0, 0), (1, 0))), axis=1)
        full = (c[:, L:] - c[:, :-L]) >= 0.8 * L      # 80% of the window dark: tolerates dashed lines
        rows = np.where(full.any(axis=1))[0]
        if not len(rows):
            return []
        out, start = [], rows[0]
        for prev, r in zip(rows, list(rows[1:]) + [None]):
            if r is None or r - prev > 1:
                band = full[start:prev + 1]
                xs = np.where(band.any(axis=0))[0]
                pos = (start + prev) / 2.0
                if prev - start <= 12:                # a thin (widened) line, not a thick block
                    out.append((pos, xs.min(), xs.max() + L))
                if r is not None:
                    start = r
        return out

    L = max(30, int(0.05 * pix.width))
    for y, x0, x1 in runs(dark_h, L, True):
        rules.append((ox + x0 * scale, oy + y * scale, ox + x1 * scale, oy + y * scale))
    Lv = max(25, int(0.03 * pix.height))
    for x, y0, y1 in runs(dark_v, Lv, False):
        rules.append((ox + x * scale, oy + y0 * scale, ox + x * scale, oy + y1 * scale))
    return _merge_rules(rules)


def _merge_rules(rules, tol=4.0):
    """Joins a single line detected in pieces on a skewed scan."""
    hs = sorted([r for r in rules if r[1] == r[3]], key=lambda r: r[1])
    vs = sorted([r for r in rules if r[0] == r[2]], key=lambda r: r[0])
    out = []
    for group, horiz in ((hs, True), (vs, False)):
        merged: list[list] = []
        for x0, y0, x1, y1 in group:
            pos, a0, a1 = (y0, x0, x1) if horiz else (x0, y0, y1)
            for m in merged:
                if abs(m[0] - pos) <= tol and a0 <= m[2] + tol and a1 >= m[1] - tol:
                    m[0] = (m[0] * m[3] + pos) / (m[3] + 1)
                    m[1], m[2], m[3] = min(m[1], a0), max(m[2], a1), m[3] + 1
                    break
            else:
                merged.append([pos, a0, a1, 1])
        for pos, a0, a1, _ in merged:
            out.append((a0, pos, a1, pos) if horiz else (pos, a0, pos, a1))
    return out


def _ink_ratio(gray, rect, scale, text, font, size) -> float:
    """Ink on the scan / ink of the same text drawn at normal weight. Clearly larger for bold text.
    (Box density alone is misleading: the box of '2026', which has no descenders, is tight and scores high.)"""
    x0, y0 = max(int(rect.x0 / scale), 0), max(int(rect.y0 / scale), 0)
    x1, y1 = int(rect.x1 / scale) + 1, int(rect.y1 / scale) + 1
    box = gray[y0:y1, x0:x1]
    if not box.size:
        return 0.0
    scan_ink = int((box < 128).sum())
    try:
        import numpy as np
        d = pymupdf.open()
        p = d.new_page(width=rect.width + 4 * size, height=size * 2)
        tw = pymupdf.TextWriter(p.rect)
        tw.append((size, size * 1.4), text, font=font, fontsize=size)
        tw.write_text(p)
        pix = p.get_pixmap(matrix=pymupdf.Matrix(1 / scale, 1 / scale), colorspace=pymupdf.csGRAY)
        ref = int((np.frombuffer(pix.samples, dtype=np.uint8) < 128).sum())
        d.close()
    except Exception:
        return 0.0
    return scan_ink / ref if ref else 0.0


def text_page(out_doc, src_page, words: list[Word]) -> None:
    """Adds a new page that writes the OCR text visibly at its original positions.

    Font size: found from both the line width (with an Arial-metric font) and its height.
    Bold: if the ink density on the scan is clearly above the page's usual text."""
    font, ffile = _font()
    bold_font, _ = _font(bold=True)
    page = out_doc.new_page(width=src_page.rect.width, height=src_page.rect.height)
    tw = pymupdf.TextWriter(page.rect)
    rules = detect_rules(src_page, words)
    vrules = [(r[0], r[1], r[3]) for r in rules if r[0] == r[2]]
    lines = _lines(words, vrules)
    heights = sorted(r.height for _, r in lines) or [12]
    body_h = heights[len(heights) // 2]
    gray, scale = None, 1.0
    try:
        import numpy as np
        pix = src_page.get_pixmap(dpi=100, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
        gray = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        scale = src_page.rect.width / pix.width
    except Exception:
        pass
    sized = []
    for text, r in lines:
        length1 = font.text_length(text, fontsize=1)
        h_size = (r.height if r.height > 1.3 * body_h else body_h) / 0.95
        # The width (with an Arial-metric font) gives the size much more consistently than the height: the height
        # of the OCR box varies with the letters ('2026' has no descenders, 'İ' has a dot). Height for short pieces.
        size = r.width / length1 if len(text) >= 4 and length1 > 0 else h_size
        size = max(5.0, min(size, 40.0))
        sized.append([text, r, size, _ink_ratio(gray, r, scale, text, font, size) if gray is not None else 0.0])
    # body font size of the page: lines close to it (±15%) snap to it, tables/text look uniform
    body = sorted(sz for t, _, sz, _ in sized if len(t) >= 8)
    if body:
        b = body[len(body) // 2]
        for item in sized:
            if abs(item[2] - b) <= 0.15 * b:
                item[2] = b
    ratios = sorted(x for t, _, _, x in sized if len(t) >= 4 and x > 0) or [0.0]
    base = ratios[len(ratios) // 2]
    for text, r, size, ratio in sized:
        bold = base > 0 and len(text) >= 3 and ratio > 1.3 * base
        f = bold_font if bold else font
        length = f.text_length(text, fontsize=size)
        if length > r.width * 1.08 and length > 0:      # don't overflow
            size *= r.width * 1.08 / length
        try:
            tw.append((r.x0, r.y1 - 0.2 * r.height), text, font=f, fontsize=size)
        except Exception:
            pass
    tw.write_text(page, color=(0, 0, 0))
    # table borders: pdf2docx builds a real Word table from these lines
    for x0, y0, x1, y1 in rules:
        page.draw_line((x0, y0), (x1, y1), color=(0, 0, 0), width=0.6)
