"""Tesseract ile Türkçe OCR.

İki kullanım:
  1) make_searchable(): taranmış sayfaların üstüne GÖRÜNMEZ metin katmanı ekler.
     Görüntü aynen kalır; metin seçilebilir, kopyalanabilir, aranabilir olur.
  2) text_page(): taranmış sayfayı, OCR metni orijinal konumlarında GÖRÜNÜR olarak
     yazılmış yeni bir sayfaya çevirir. PDF → Word bunu düzenlenebilir metne çevirir.

Tesseract ayrı bir programdır (Apache-2.0):
  Windows: https://github.com/UB-Mannheim/tesseract/wiki  (kurarken "Turkish" dilini seç)
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
    except ImportError:          # install.py kütüphaneler kurulmadan da yükleyebilsin
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


# ------------------------------------------------------------------ OCR çekirdeği

@dataclass
class Word:
    text: str
    rect: "pymupdf.Rect"   # PDF koordinatları (pt)
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
    """Sonuç puanı: güvenilir (≥60) kelimelerin güven toplamı. Daha çok doğru kelime = daha yüksek."""
    return sum(w.conf for w in words if w.conf >= 60 and len(w.text) > 1)


def _mean_conf(words: list[Word]) -> float:
    ws = [w.conf for w in words if len(w.text) > 1]
    return sum(ws) / len(ws) if ws else 0.0


def clean_background(img, strong: bool = False):
    """Gölge/ışık farkını ve kağıt rengini temizler: arka plan tahmini (büyük maksimum + bulanıklık)
    ile bölüp kontrastı açar; gürültü için önce medyan. Telefon fotoğrafı ve soluk fotokopide işe yarar.
    strong: karlı/gürültülü soluk taramalar için daha güçlü yumuşatma."""
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
    """Temizlenmiş görüntüyü Otsu eşiğiyle siyah-beyaz yapar (çok soluk taramalar için)."""
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
    """Gölge/eşit olmayan ışık ya da düşük kontrast var mı? (Tesseract gölgedeki satırları güvenle
    'yok' sayıyor: ortalama güven yüksek kalırken kelimeler eksik çıkıyor, o yüzden ayrıca bakılır.)"""
    import numpy as np
    from PIL import ImageFilter
    small = img.resize((max(1, img.width // 4), max(1, img.height // 4)))
    bg = np.asarray(small.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(8)), dtype=np.float32)
    # kontrast küçültülmüş görüntüde değil seyreltilmiş tam görüntüde: ince yazı tipleri (Arial)
    # küçültmede griye ortalanıp temiz sayfayı 'soluk' gösteriyordu
    a = np.asarray(img, dtype=np.float32)[::3, ::3]
    uneven = np.percentile(bg, 95) - np.percentile(bg, 5)
    contrast = np.percentile(a, 99) - np.percentile(a, 1)
    return uneven > 30 or contrast < 120


def _ocr_words(exe: str, langs: str, page) -> list[Word]:
    """Önce ham görüntü; sonuç zayıfsa (gölgeli, soluk, telefonla çekilmiş) temizlenmiş görüntüler de
    denenir ve en iyi sonuç alınır. Temiz taramalarda ek maliyet yok."""
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

    # MuPDF belgeleri iş parçacıkları arasında paylaşılamaz → sayfaları önce resme çevirip
    # sırayla OCR'lamak yerine her iş parçacığına ayrı belge kopyası veriyoruz.
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
    """Sayfada çıkarılabilir metin yok ama görsel içerik var mı?"""
    if len(page.get_text().strip()) >= 10:
        return False
    return bool(page.get_images()) or bool(page.get_drawings())


# ------------------------------------------------------------------ yazı tipi

# Programla gelen Liberation Sans önce: Arial ile birebir aynı genişlikte (taramadan kurulan metin
# orijinal satır genişliğine oturur) ve her bilgisayarda aynı sonuç. Windows'un Arial'ı ikinci sırada
# değil son çarelerden: Arial'da boşluk ile bölünmez boşluk (U+00A0), tire ile yumuşak tire (U+00AD)
# aynı harf şekli; PDF'e gömülünce metin kopyalanınca/aranınca 'Başlık 1' yerine 'Başlık\xa01' çıkıyordu.
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


# ------------------------------------------------------------------ 1) aranabilir PDF

HANDWRITING_MIN_CONF = 80


def make_searchable(doc, words_by_page: dict[int, list[Word]], report: dict | None = None) -> int:
    """Görünmez metin katmanı ekler. Eklenen kelime sayısını döndürür.

    El yazısı gibi görünen sayfalarda (is_text_page değil) Tesseract çoğunlukla anlamsız kelimeler
    üretir; aramada yanlış sonuç ve kopyalamada çöp metin olmasın diye orada yalnızca çok güvenilir
    kelimeler eklenir. report verilirse {"handwritten": sayfa sayısı, "dropped": atılan kelime} doldurulur.
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
            size = max(1.0, w.rect.width / length)  # kelime genişliğine sığdır → seçim doğru hizalanır
            try:
                tw.append((w.rect.x0, w.rect.y1 - 0.18 * w.rect.height), w.text, font=font, fontsize=size)
                n += 1
            except Exception:
                pass
        tw.write_text(page, render_mode=3)  # 3 = görünmez
    from .actions import fix_tounicode
    fix_tounicode(doc)                      # Acrobat'ta arama/kopyalamada 'ı' → '1' olmasın
    if report is not None:
        report.update(handwritten=hand, dropped=dropped)
    return n


# ------------------------------------------------------------------ 2) metin sayfası

_JUNK = set("|¦_—–-=~·•.,'`\"")


def _clean(words: list[Word]) -> list[Word]:
    """Tablo kenarlığı artıkları ('|', '_' vb.) ve çok düşük güvenli tek karakterleri at."""
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
    """OCR satırlarını, aradaki büyük boşluklardan ve dikey tablo çizgilerinden
    (hücre sınırları) parçalara böler."""
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
            if w.rect.x0 - prev.rect.x1 > 1.6 * h or crosses:   # sütun boşluğu / hücre sınırı
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
    """Basılı metin ağırlıklı ve güvenilir mi? Değilse sayfa resim olarak kalmalı.

    Ölçümle belirlendi (bench): basılı taramada iyi kelime oranı 0.85-0.96, ortalama
    güven 94-96; el yazısında oran 0.24-0.41, güven 38-55. Arada kalan → resim (güvenli).
    """
    if len(words) < 20:
        return False
    multi = [w for w in words if len(w.text) > 1]        # tek karakterli simgeler ('—', '1') sayılmaz
    if len(multi) < 15:
        return False
    ratio = sum(1 for w in multi if w.conf >= 50) / len(multi)
    mean = sum(max(w.conf, 0) for w in words) / len(words)
    return ratio >= 0.7 and mean >= 80


RULE_DPI = 100


def detect_rules(page, words: list[Word] | None = None) -> list[tuple]:
    """Taranmış sayfadaki uzun yatay/dikey çizgileri (tablo kenarlıkları) bulur.

    Döndürür: [(x0, y0, x1, y1), ...] PDF koordinatlarında. numpy yoksa boş liste.
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
    # Kelime kutularının içini sil: yoğun metin satırları çizgi sanılmasın
    for w in words or []:
        x0 = int((w.rect.x0 - ox) / scale) + 1
        x1 = int((w.rect.x1 - ox) / scale) - 1
        y0 = int((w.rect.y0 - oy) / scale) + 1
        y1 = int((w.rect.y1 - oy) / scale) - 1
        if x1 > x0 and y1 > y0:
            dark[max(y0, 0):y1, max(x0, 0):x1] = False
    # eğik/soluk taramalara tolerans: çizgiye dik yönde ±2 px genişlet
    def widen(m, axis):
        out = m.copy()
        for k in (1, 2):
            out |= np.roll(m, k, axis=axis) | np.roll(m, -k, axis=axis)
        return out
    dark_h = widen(dark, 0).astype(np.int32)   # yatay çizgiler için dikeyde genişlet
    dark_v = widen(dark, 1).astype(np.int32)   # dikey çizgiler için yatayda genişlet
    rules = []

    def runs(mask_2d, L, horizontal):
        # her satırda (yatayda) L piksel boyunca kesintisiz koyu olan konumlar
        m = mask_2d if horizontal else mask_2d.T
        c = np.cumsum(np.pad(m, ((0, 0), (1, 0))), axis=1)
        full = (c[:, L:] - c[:, :-L]) >= 0.8 * L      # pencerenin %80'i koyu: kesik çizgiye tolerans
        rows = np.where(full.any(axis=1))[0]
        if not len(rows):
            return []
        out, start = [], rows[0]
        for prev, r in zip(rows, list(rows[1:]) + [None]):
            if r is None or r - prev > 1:
                band = full[start:prev + 1]
                xs = np.where(band.any(axis=0))[0]
                pos = (start + prev) / 2.0
                if prev - start <= 12:                # kalın blok değil, ince (genişletilmiş) çizgi
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
    """Eğik taramada tek çizginin parça parça algılanmasını birleştirir."""
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
    """Taramadaki mürekkep / aynı metnin normal kalınlıkta çizilmiş mürekkebi. Kalın yazıda belirgin
    büyük. (Kutu yoğunluğu tek başına yanıltıyor: alt uzantısız '2026' kutusu sıkı, oran yüksek çıkıyor.)"""
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
    """OCR metnini orijinal konumlarında görünür yazan yeni bir sayfa ekler.

    Punto: satır genişliğinden (Arial ölçülü yazı tipiyle) ve yüksekliğinden birlikte bulunur.
    Kalın: taramadaki mürekkep yoğunluğu sayfanın olağan yazısından belirgin fazlaysa."""
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
        # Genişlik (Arial ölçülü yazı tipiyle) puntoyu yükseklikten çok daha tutarlı veriyor: OCR kutusunun
        # yüksekliği harfe göre oynuyor ('2026' alt uzantısız, 'İ' noktalı). Kısa parçalarda yükseklik.
        size = r.width / length1 if len(text) >= 4 and length1 > 0 else h_size
        size = max(5.0, min(size, 40.0))
        sized.append([text, r, size, _ink_ratio(gray, r, scale, text, font, size) if gray is not None else 0.0])
    # sayfanın gövde puntosu: ona yakın (±%15) satırlar aynı puntoya oturur, tablo/metin tek tip görünür
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
        if length > r.width * 1.08 and length > 0:      # taşmasın
            size *= r.width * 1.08 / length
        try:
            tw.append((r.x0, r.y1 - 0.2 * r.height), text, font=f, fontsize=size)
        except Exception:
            pass
    tw.write_text(page, color=(0, 0, 0))
    # tablo kenarlıkları: pdf2docx bu çizgilerden gerçek Word tablosu kurar
    for x0, y0, x1, y1 in rules:
        page.draw_line((x0, y0), (x1, y1), color=(0, 0, 0), width=0.6)
