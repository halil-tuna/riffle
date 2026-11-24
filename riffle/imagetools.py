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

def _text_from_words(words) -> str:
    """OCR kelimelerini okuma sırasıyla satır ve paragraflara diz."""
    from .ocr import _clean
    lines: dict[tuple, list] = {}
    for w in _clean(words):
        if w.conf < 30:
            continue
        lines.setdefault(w.line_key, []).append(w)
    ordered = sorted(lines.items(), key=lambda kv: (min(w.rect.y0 for w in kv[1]), min(w.rect.x0 for w in kv[1])))
    out, prev_par = [], None
    for key, ws in ordered:
        ws.sort(key=lambda w: w.rect.x0)
        par = key[:2]
        if prev_par is not None and par != prev_par:
            out.append("")
        out.append(" ".join(w.text for w in ws))
        prev_par = par
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
        text = _text_from_words(words)
        if not text:
            progress(f"ℹ {p.name}: okunabilir yazı bulunamadı")
            continue
        out = unique_path(p.parent, p.stem, ".txt")
        out.write_text(text + "\n", encoding="utf-8")
        outputs.append(out)
        texts.append(text if len(paths) == 1 else f"--- {p.name} ---\n{text}")
        n = len(text.split())
        progress(f"ℹ {p.name}: {n} kelime okundu")
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
