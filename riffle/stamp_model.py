"""İmza / yazı / görsel yerleştirme — pencereden bağımsız model (test edilebilir).

Koordinatlar her zaman sayfanın *görünen* (döndürülmüş hâli) koordinatlarında tutulur;
PDF'e yazarken derotation_matrix ile sayfanın kendi koordinatına çevrilir.
"""

from __future__ import annotations

import io
import os
import sys
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from .actions import ConversionError, fix_tounicode, open_pdf, pymupdf, unique_path  # noqa: F401


def config_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        d, old = base / "Riffle", base / "PratikCevirici"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        d, old = base / "riffle", base / "pratik-cevirici"
    if not d.exists() and old.is_dir():
        # eski adla (Pratik Çevirici) kaydedilmiş imza, ayarlar ve sık kullanılanlar yeni ada taşınır
        try:
            old.rename(d)
        except OSError:
            pass
    d.mkdir(parents=True, exist_ok=True)
    return d


def signature_path() -> Path:
    return config_dir() / "imza.png"


# ------------------------------------------------------------------ imza görseli

def clean_signature(src, ink=(16, 32, 96)) -> bytes:
    """Fotoğraftaki/taramadaki imzayı arka planı şeffaf, tek renk mürekkep PNG'ye çevirir."""
    from PIL import Image, ImageFilter, ImageOps
    im = Image.open(src) if not isinstance(src, Image.Image) else src
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im)
    g = im.convert("L")
    g.thumbnail((1600, 1600))
    # aydınlatma düzeltmesi: büyük bulanıklık = kağıt; oran → mürekkep koyuluğu
    paper = g.filter(ImageFilter.GaussianBlur(max(8, min(g.size) // 12)))
    import numpy as np
    a = np.asarray(g, dtype=np.float32)
    p = np.maximum(np.asarray(paper, dtype=np.float32), 1)
    dark = np.clip(1 - a / p, 0, 1)                    # 0 = kağıt, 1 = koyu mürekkep
    thr = max(0.12, float(np.percentile(dark, 99)) * 0.25)
    alpha = np.clip((dark - thr * 0.5) / max(thr, 1e-3), 0, 1)
    alpha[dark < thr * 0.5] = 0
    if alpha.max() == 0:
        raise ConversionError("Görselde imza bulunamadı (beyaz kâğıda koyu kalemle atılmış olmalı).")
    ys, xs = np.nonzero(alpha > 0.2)
    pad = 6
    y0, y1 = max(ys.min() - pad, 0), min(ys.max() + pad + 1, alpha.shape[0])
    x0, x1 = max(xs.min() - pad, 0), min(xs.max() + pad + 1, alpha.shape[1])
    alpha = alpha[y0:y1, x0:x1]
    out = Image.new("RGBA", (x1 - x0, y1 - y0), ink + (0,))
    out.putalpha(Image.fromarray((alpha * 255).astype("uint8")))
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def strokes_to_png(strokes, size=(600, 220), width=5, ink=(16, 32, 96)) -> bytes:
    """Fareyle çizilen çizgileri ([(x,y),...] listeleri) şeffaf PNG imzaya çevirir."""
    from PIL import Image, ImageDraw
    scale = 3
    im = Image.new("RGBA", (size[0] * scale, size[1] * scale), ink + (0,))
    dr = ImageDraw.Draw(im)
    pts_all = []
    for s in strokes:
        pts = [(x * scale, y * scale) for x, y in s]
        pts_all += pts
        if len(pts) == 1:
            x, y = pts[0]
            r = width * scale / 2
            dr.ellipse((x - r, y - r, x + r, y + r), fill=ink + (255,))
        elif pts:
            dr.line(pts, fill=ink + (255,), width=width * scale, joint="curve")
            for x, y in (pts[0], pts[-1]):
                r = width * scale / 2
                dr.ellipse((x - r, y - r, x + r, y + r), fill=ink + (255,))
    if not pts_all:
        raise ConversionError("İmza boş.")
    bbox = im.getbbox()
    im = im.crop(bbox).resize((max(1, (bbox[2] - bbox[0]) // scale), max(1, (bbox[3] - bbox[1]) // scale)),
                              Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def image_bytes(path: Path) -> bytes:
    """Logo/görsel dosyasını PNG baytlarına çevirir (HEIC, EXIF yönü, şeffaflık korunur)."""
    from PIL import Image, ImageOps
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
    except Exception:
        pass
    try:
        im = ImageOps.exif_transpose(Image.open(path))
    except Exception as e:
        raise ConversionError(f"{Path(path).name} açılamadı: {e}")
    if im.mode not in ("RGB", "RGBA"):
        im = im.convert("RGBA")
    im.thumbnail((2000, 2000))
    buf = io.BytesIO()
    im.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def png_size(data: bytes) -> tuple[int, int]:
    from PIL import Image
    return Image.open(io.BytesIO(data)).size


# ------------------------------------------------------------------ öğeler

@dataclass
class Stamp:
    kind: str                         # "image" | "text"
    page: int
    rect: tuple                       # görünen koordinat (x0, y0, x1, y1), pt
    data: bytes = b""                 # PNG (image)
    text: str = ""
    color: tuple = (0.05, 0.1, 0.4)
    opacity: float = 1.0
    uid: int = field(default_factory=lambda: next(_ids))

    @property
    def aspect(self) -> float:
        x0, y0, x1, y1 = self.rect
        return (x1 - x0) / max(y1 - y0, 1e-6)

    def fontsize(self) -> float:
        x0, y0, x1, y1 = self.rect
        lines = max(1, self.text.count("\n") + 1)
        return max(4.0, (y1 - y0) / lines / 1.25)


def _counter():
    i = 0
    while True:
        i += 1
        yield i


_ids = _counter()


def text_rect(text: str, at: tuple, fontsize: float = 14) -> tuple:
    """Metni tam saracak kutu (görünen koordinat)."""
    from .ocr import _font
    font, _ = _font()
    lines = text.split("\n") or [""]
    w = max(font.text_length(ln, fontsize=fontsize) for ln in lines) + fontsize * 0.4
    h = fontsize * 1.25 * len(lines) + fontsize * 0.15
    x, y = at
    return (x, y, x + w, y + h)


def today_text(fmt: str = "%d.%m.%Y") -> str:
    return date.today().strftime(fmt)


def place_image(data: bytes, page_rect, at: tuple | None = None, width: float = 160) -> tuple:
    """Görseli en-boy oranını koruyarak yerleştirir; sayfadan taşmaz."""
    w, h = png_size(data)
    width = min(width, page_rect.width * 0.8)
    height = width * h / max(w, 1)
    if height > page_rect.height * 0.8:
        height = page_rect.height * 0.8
        width = height * w / max(h, 1)
    if at is None:
        at = (page_rect.width - width - 48, page_rect.height - height - 64)
    return clamp((at[0], at[1], at[0] + width, at[1] + height), page_rect)


def clamp(rect: tuple, page_rect) -> tuple:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    x0 = min(max(x0, 0), max(page_rect.width - w, 0))
    y0 = min(max(y0, 0), max(page_rect.height - h, 0))
    return (x0, y0, x0 + w, y0 + h)


def copy_to_pages(stamp: Stamp, pages: list[int]) -> list[Stamp]:
    return [replace(stamp, page=p, uid=next(_ids)) for p in pages if p != stamp.page]


# ------------------------------------------------------------------ PDF'e yaz

def apply(src: Path, stamps: list[Stamp], out: Path | None = None, flatten_note: bool = True) -> Path:
    from .ocr import _font_file
    ff = _font_file()
    with open_pdf(src) as doc:
        for s in stamps:
            if not 0 <= s.page < len(doc):
                continue
            page = doc[s.page]
            vis = pymupdf.Rect(s.rect)

            def unrot(v):
                r = v * page.derotation_matrix
                r.normalize()
                return r

            r = unrot(vis)
            if s.kind == "image":
                page.insert_image(r, stream=s.data, rotate=page.rotation, keep_proportion=True, overlay=True)
            else:
                fs = s.fontsize()
                kw = dict(fontsize=fs, color=s.color, rotate=page.rotation, align=pymupdf.TEXT_ALIGN_LEFT,
                          fill_opacity=s.opacity, lineheight=1.25)
                kw.update(dict(fontname="riffleimza", fontfile=ff) if ff else dict(fontname="helv"))
                # yazı tipi ölçü farkına pay (kesilmesin); pay görünen koordinatta sağa/alta verilir
                rc = page.insert_textbox(unrot(vis + (-2, -2, fs * 2, fs)), s.text, **kw)
                if rc < 0:                              # yine de sığmadıysa küçült
                    kw["fontsize"] = fs * 0.8
                    page.insert_textbox(unrot(vis + (-2, -2, fs * 6, fs * 3)), s.text, **kw)
        fix_tounicode(doc)
        out = out or unique_path(src.parent, f"{src.stem}_imzali", ".pdf")
        doc.save(out, garbage=3, deflate=True)
    return out
