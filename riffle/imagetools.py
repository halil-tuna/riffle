"""Görsel araçları: görseldeki yazıyı çıkar (OCR), hedef boyuta indir, PNG'ye çevir, döndür."""

from __future__ import annotations

import io
import re
from pathlib import Path

from .actions import ConversionError, Progress, human_size, unique_path

CLIPBOARD = "\x00clip:"    # ilerleme penceresine: bu metni panoya kopyala (ekranda gösterilmez)


def _open(p: Path):
    from .tools import _open_image
    return _open_image(p)


# ============================================================ görseldeki yazı

MARK = "[denklem / şekil — okunamadı]"


def _ink_bands(gray, min_ink: int = 3) -> list[tuple[int, int]]:
    """Görüntüde mürekkep olan yatay şeritler (piksel satır aralıkları)."""
    import numpy as np
    a = np.asarray(gray, dtype=np.uint8)
    ink = (a < 140).sum(axis=1)
    rows = ink >= min_ink
    bands, start = [], None
    for y, on in enumerate(rows):
        if on and start is None:
            start = y
        elif not on and start is not None:
            bands.append((start, y))
            start = None
    if start is not None:
        bands.append((start, len(rows)))
    # aynı satırın kopuk parçaları (i noktası, alt simge) birleşsin
    merged = []
    for b in bands:
        if merged and b[0] - merged[-1][1] <= 3:
            merged[-1] = (merged[-1][0], b[1])
        else:
            merged.append(b)
    return merged


def _fix_symbols(text: str) -> str:
    # matematikte ε'yi Tesseract € okuyor; para birimi (€5, € 10) dokunulmaz
    return re.sub(r"€(?=[A-Za-z])", "ε", text)


def _text_from_words(words, gray=None, scale: float = 1.0) -> str:
    """OCR kelimelerini okuma sırasıyla satır ve paragraflara diz.

    Denklemler, kesirler ve şekiller Tesseract'ın işi değil: ya hiç okunmuyor ya çöp çıkıyor. Görüntü
    (gray) verilirse bunlar sessizce kaybolmasın diye yerlerine işaret konur: mürekkebi olup kelimesi
    olmayan şeritler ve güveni düşük kısa satırlar."""
    from .ocr import _clean
    lines: dict[tuple, list] = {}
    for w in _clean(words):
        if w.conf < 30:
            continue
        lines.setdefault(w.line_key, []).append(w)
    items = []      # (y_px, tür, içerik, paragraf anahtarı)
    for key, ws in lines.items():
        ws.sort(key=lambda w: w.rect.x0)
        y0 = min(w.rect.y0 for w in ws) / scale
        y1 = max(w.rect.y1 for w in ws) / scale
        conf = sum(w.conf for w in ws) / len(ws)
        text = " ".join(w.text for w in ws)
        weak = gray is not None and conf < 80 and len(ws) <= 4
        items.append([(y0 + y1) / 2, "mark" if weak else "text", text, key[:2], (y0, y1)])
    heights = sorted(it[4][1] - it[4][0] for it in items if it[1] == "text") or [20]
    line_h = heights[len(heights) // 2]
    if gray is not None:
        for b0, b1 in _ink_bands(gray):
            if b1 - b0 < 0.5 * line_h:
                continue                                   # çizgi, gürültü
            covered = any(it[4][0] - 3 <= (b0 + b1) / 2 <= it[4][1] + 3 or b0 <= (it[4][0] + it[4][1]) / 2 <= b1
                          for it in items)
            if not covered:
                items.append([(b0 + b1) / 2, "mark", "", None, (b0, b1)])
    items.sort(key=lambda it: it[0])
    out, prev_par, prev_kind, prev_box = [], None, None, None
    for _, kind, text, par, box in items:
        if kind == "mark":
            if prev_kind != "mark":
                out.append(MARK)
            prev_kind, prev_par, prev_box = "mark", None, box
            continue
        # paragraf arası: satırlar arasında boşluk varsa (Tesseract her satırı ayrı paragraf sayabiliyor)
        if prev_kind == "text" and (box[0] - prev_box[1] > 0.9 * line_h if gray is not None else par != prev_par):
            out.append("")
        out.append(_fix_symbols(text))
        prev_par, prev_kind, prev_box = par, "text", box
    text = "\n".join(out).strip()
    return re.sub(r"\n{3,}", "\n\n", text)


def image_text(paths: list[Path], progress: Progress, **_) -> list[Path]:
    """Ekran görüntüsü, slayt, tahta fotoğrafı → metin (.txt) + panoya kopya."""
    import pymupdf
    from . import ocr
    ocr.require()
    outputs, texts = [], []
    for p in paths:
        progress(f"Yazı okunuyor: {p.name}")
        im = _open(p)
        # küçük ekran görüntülerinde Tesseract harfleri kaçırıyor: kısa kenarı en az ~1500 px
        if min(im.size) < 1200:
            k = 1500 / min(im.size)
            im = im.resize((int(im.width * k), int(im.height * k)))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        with pymupdf.open() as doc:
            # sayfa boyu = piksel × 72 / OCR_DPI: OCR görüntüyü kendi çözünürlüğünde okur (büyütmeden)
            k = 72 / ocr.OCR_DPI
            page = doc.new_page(width=im.width * k, height=im.height * k)
            page.insert_image(page.rect, stream=buf.getvalue())
            words = ocr.ocr_pages(doc, [0])[0]
        text = _text_from_words(words, im.convert("L"), scale=72 / ocr.OCR_DPI)
        if not text:
            progress(f"ℹ {p.name}: okunabilir yazı bulunamadı")
            continue
        out = unique_path(p.parent, p.stem, ".txt")
        out.write_text(text + "\n", encoding="utf-8")
        outputs.append(out)
        texts.append(text if len(paths) == 1 else f"--- {p.name} ---\n{text}")
        n = len(text.replace(MARK, "").split())
        marks = text.count(MARK)
        progress(f"ℹ {p.name}: {n} kelime okundu" + (
            f" · {marks} yerde denklem/şekil var, metinde [ ] ile işaretli (yazı tanıma bunları okuyamaz)"
            if marks else ""))
    if not outputs:
        raise ConversionError("Görsellerde okunabilir yazı bulunamadı.")
    progress(CLIPBOARD + "\n\n".join(texts))
    return outputs


# ============================================================ hedef boyuta indir

def _parse_kb(target: str) -> int:
    s = str(target).strip().lower().replace(",", ".").replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)(kb|k|mb|m)?$", s)
    if not m:
        raise ConversionError(f"Hedef boyut anlaşılamadı: {target!r} (ör. 200 ya da 1.5mb)")
    v = float(m[1])
    unit = m[2] or "kb"
    kb = v * 1024 if unit.startswith("m") else v
    if kb < 5:
        raise ConversionError("Hedef boyut en az 5 KB olmalı.")
    return int(kb * 1024)


def _jpeg(im, q: int) -> bytes:
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q, optimize=True, progressive=True)
    return b.getvalue()


def image_to_target(paths: list[Path], progress: Progress, target: str = "200", **_) -> list[Path]:
    """En yüksek kalitede, hedefin altında JPEG: önce kalite, gerekirse boyut küçültülür."""
    limit = _parse_kb(target)
    outputs = []
    for p in paths:
        progress(f"Hedef boyuta indiriliyor: {p.name}")
        im = _open(p).convert("RGB")
        before = p.stat().st_size
        best = None
        for _round in range(14):
            lo, hi, found = 40, 92, None
            while lo <= hi:                       # bu boyutta hedefin altındaki en yüksek kalite
                q = (lo + hi) // 2
                data = _jpeg(im, q)
                if len(data) <= limit:
                    found, lo = data, q + 1
                else:
                    hi = q - 1
            if found:
                best = found
                break
            im = im.resize((max(1, int(im.width * 0.85)), max(1, int(im.height * 0.85))))
        if best is None:
            raise ConversionError(f"{p.name}: {human_size(limit)} altına inmiyor.")
        label = f"{round(limit / 1024)}kb"
        out = unique_path(p.parent, f"{p.stem}_{label}", ".jpg")
        out.write_bytes(best)
        progress(f"ℹ {p.name}: {human_size(before)} → {human_size(len(best))} · {im.width}×{im.height} px")
        outputs.append(out)
    return outputs


# ============================================================ PNG / döndür

def images_to_png(paths: list[Path], progress: Progress, **_) -> list[Path]:
    from PIL import Image, ImageOps
    outputs = []
    for p in paths:
        progress(f"PNG'ye çevriliyor: {p.name}")
        if p.suffix.lower() in (".heic", ".heif"):
            im = _open(p)
        else:
            im = ImageOps.exif_transpose(Image.open(p))   # saydamlık korunsun
        out = unique_path(p.parent, p.stem, ".png")
        im.save(out, "PNG", optimize=True)
        outputs.append(out)
    return outputs


def rotate_images(paths: list[Path], progress: Progress, angle: int = 90, **_) -> list[Path]:
    """angle: saat yönünde derece. Biçim korunur (HEIC → JPG)."""
    from PIL import Image, ImageOps
    outputs = []
    for p in paths:
        progress(f"Döndürülüyor: {p.name}")
        ext = p.suffix.lower()
        if ext in (".heic", ".heif"):
            im, ext = _open(p), ".jpg"
        else:
            src = Image.open(p)
            im = ImageOps.exif_transpose(src)
        im = im.rotate(-angle, expand=True)
        out = unique_path(p.parent, f"{p.stem}_dondurulmus", ext)
        if ext in (".jpg", ".jpeg"):
            im.convert("RGB").save(out, "JPEG", quality=95, subsampling=0, optimize=True)
        else:
            im.save(out)
        outputs.append(out)
    return outputs
