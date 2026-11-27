"""Small previews for the History tab's icon view, like the icons in File Explorer.

- PDF: the first page, drawn small (pymupdf).
- Images: scaled down (Pillow; iPhone HEIC too).
- Anything else on Windows (Word, Excel, PowerPoint, …): the preview File Explorer itself shows
  (IShellItemImageFactory — Windows' own thumbnail cache, so no new copy is made).
- No preview: a simple icon by file type (PDF red, Word blue, Excel green, PowerPoint orange…).

Privacy: nothing here is ever written to disk. The History tab keeps the previews in memory only while the
window is open ("no copies of your files are kept"). No window code here: everything returns Pillow images.
"""

from __future__ import annotations

import io
import os
import sys
from pathlib import Path

SIZE = 144
SUPERSAMPLE = 3                  # PDF pages are drawn 3× larger and scaled down: crisper text and lines

KINDS = {
    "pdf": ("pdf",),
    "word": ("doc", "docx", "odt", "rtf", "dot", "dotx"),
    "excel": ("xls", "xlsx", "xlsm", "ods", "csv"),
    "ppt": ("ppt", "pptx", "odp", "pps", "ppsx"),
    "image": ("jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "gif", "heic", "heif"),
    "text": ("txt", "md", "html", "htm", "json", "xml"),
    "archive": ("zip", "7z", "rar"),
}
COLORS = {"pdf": "#d93025", "word": "#2b579a", "excel": "#217346", "ppt": "#c43e1c", "image": "#7e57c2",
          "text": "#5f6670", "archive": "#8d6e63", "other": "#5f6670", "folder": "#e8a317"}
_IMAGE_EXT = set(KINDS["image"])


def kind(path) -> str:
    p = Path(path)
    if p.is_dir():
        return "folder"
    ext = p.suffix.lower().lstrip(".")
    return next((k for k, exts in KINDS.items() if ext in exts), "other")


# ------------------------------------------------------------------ previews of the content

def preview(path, size: int = SIZE):
    """A Pillow image of the file's content (at most size × size), or None if there is none to make."""
    p = Path(path)
    try:
        if not p.exists() or p.is_dir():
            return None
        ext = p.suffix.lower().lstrip(".")
        if ext == "pdf":
            return _pdf(p, size)
        if ext in _IMAGE_EXT:
            return _image(p, size)
        if sys.platform == "win32":
            return _shell(p, size)
    except Exception:
        return None
    return None


def _pdf(p: Path, size: int):
    import pymupdf
    from PIL import Image
    with pymupdf.open(p) as doc:
        if doc.needs_pass or doc.page_count == 0:
            return None
        page = doc[0]
        r = page.rect
        zoom = size * SUPERSAMPLE / max(r.width, r.height, 1)
        pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    w, h = im.size
    im = im.resize((max(1, round(w / SUPERSAMPLE)), max(1, round(h / SUPERSAMPLE))), Image.LANCZOS)
    return _sharpen(im)


def _image(p: Path, size: int):
    from PIL import Image, ImageOps
    if p.suffix.lower() in (".heic", ".heif"):
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            return None
    with Image.open(p) as im:
        im.draft("RGB", (size * 2, size * 2))           # JPEG: decode at a smaller scale (much faster)
        im = ImageOps.exif_transpose(im)
        im.thumbnail((size, size), Image.LANCZOS)
        im = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
        return _sharpen(im)


def _shell(p: Path, size: int):
    """Windows: File Explorer's thumbnail (or its icon) for any file. Call from a thread that called
    com_init()."""
    import ctypes
    from ctypes import POINTER, WINFUNCTYPE, byref, c_long, c_void_p, wintypes
    from PIL import Image

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                    ("Data4", ctypes.c_ubyte * 8)]

    class SIZE_(ctypes.Structure):
        _fields_ = [("cx", c_long), ("cy", c_long)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", c_long), ("bmWidth", c_long), ("bmHeight", c_long), ("bmWidthBytes", c_long),
                    ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD), ("bmBits", c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", c_long), ("biHeight", c_long),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", c_long), ("biYPelsPerMeter", c_long),
                    ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

    # IShellItemImageFactory {bcc18b79-ba16-442f-80c4-8a59c30c463b}
    iid = GUID(0xBCC18B79, 0xBA16, 0x442F, (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B))
    shell32, gdi32, user32 = ctypes.windll.shell32, ctypes.windll.gdi32, ctypes.windll.user32
    shell32.SHCreateItemFromParsingName.argtypes = [wintypes.LPCWSTR, c_void_p, POINTER(GUID), POINTER(c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.HRESULT
    factory = c_void_p()
    shell32.SHCreateItemFromParsingName(str(p), None, byref(iid), byref(factory))
    vtbl = ctypes.cast(factory, POINTER(POINTER(c_void_p))).contents
    release = WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtbl[2])
    get_image = WINFUNCTYPE(ctypes.HRESULT, c_void_p, SIZE_, ctypes.c_int, POINTER(wintypes.HBITMAP))(vtbl[3])
    hbmp = wintypes.HBITMAP()
    try:                                                # asked 2× larger, scaled down here (crisper)
        get_image(factory, SIZE_(size * 2, size * 2), 0, byref(hbmp))   # SIIGBF_RESIZETOFIT: thumbnail, else icon
    finally:
        release(factory)
    try:
        bm = BITMAP()
        gdi32.GetObjectW(hbmp, ctypes.sizeof(bm), byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        if w <= 0 or h <= 0:
            return None
        hdr = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)   # top-down BGRA
        buf = ctypes.create_string_buffer(w * h * 4)
        dc = user32.GetDC(None)
        try:
            if not gdi32.GetDIBits(dc, hbmp, 0, h, buf, byref(hdr), 0):
                return None
        finally:
            user32.ReleaseDC(None, dc)
        im = Image.frombuffer("RGBA", (w, h), buf.raw, "raw", "BGRA", 0, 1)
        if im.getchannel("A").getextrema()[1] == 0:        # thumbnails often come without alpha: opaque
            im = im.convert("RGB")
        im = im.copy()
        if max(im.size) > size:
            im.thumbnail((size, size), Image.LANCZOS)
            im = _sharpen(im)
        return im
    finally:
        gdi32.DeleteObject(hbmp)


def _sharpen(im):
    """A light unsharp mask after scaling down (Windows enlarges the window at 125-150 % display scaling,
    which softens it again)."""
    from PIL import ImageFilter
    if im.mode not in ("RGB", "RGBA", "L"):
        return im
    return im.filter(ImageFilter.UnsharpMask(radius=0.8, percent=70, threshold=1))


def com_init() -> None:
    """Once per worker thread on Windows (the shell thumbnail is a COM call)."""
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.ole32.CoInitializeEx(None, 0x2)   # COINIT_APARTMENTTHREADED
        except Exception:
            pass


# ------------------------------------------------------------------ drawn icons (no preview)

def _font(px: int, bold: bool = True):
    from PIL import ImageFont
    name = "LiberationSans-Bold.ttf" if bold else "LiberationSans-Regular.ttf"
    try:
        return ImageFont.truetype(str(Path(__file__).resolve().parent / "fonts" / name), px)
    except Exception:
        try:
            return ImageFont.load_default(px)
        except TypeError:
            return ImageFont.load_default()


def type_icon(path, size: int = SIZE, dark: bool = False):
    """A file-type icon: a page with a folded corner and the extension on a coloured band (or a folder)."""
    from PIL import Image, ImageDraw
    k = kind(path)
    s = 4                                                   # drawn 4× larger, scaled down = smooth edges
    W = size * s
    im = Image.new("RGBA", (W, W), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    color = COLORS.get(k, COLORS["other"])
    paper, line = ("#3a3a3a", "#6a6a6a") if dark else ("#ffffff", "#c8ccd2")
    if k == "folder":
        x0, y0, x1, y1 = W * 0.1, W * 0.24, W * 0.9, W * 0.82
        d.rounded_rectangle((x0, y0 - W * 0.07, x0 + W * 0.32, y0 + W * 0.05), radius=W * 0.03, fill="#d8920f")
        d.rounded_rectangle((x0, y0, x1, y1), radius=W * 0.04, fill=color)
        return im.resize((size, size), Image.LANCZOS)
    x0, y0, x1, y1 = W * 0.2, W * 0.08, W * 0.8, W * 0.92
    fold = W * 0.16
    body = [(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)]
    d.polygon(body, fill=paper, outline=line, width=s * 2)
    d.polygon([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)], fill=line)
    for i in range(4):                                       # text lines on the page
        yy = y0 + W * (0.22 + i * 0.09)
        d.rounded_rectangle((x0 + W * 0.08, yy, x1 - W * (0.08 + 0.1 * (i % 2)), yy + W * 0.025),
                            radius=W * 0.01, fill=line)
    ext = Path(path).suffix.lstrip(".").upper()[:4] or "?"
    by0, by1 = y0 + W * 0.58, y0 + W * 0.78
    d.rectangle((x0 - W * 0.06, by0, x1 - W * 0.06, by1), fill=color)
    f = _font(int(W * (0.13 if len(ext) <= 3 else 0.11)))
    tw = d.textlength(ext, font=f)
    d.text(((x0 - W * 0.06 + x1 - W * 0.06 - tw) / 2, by0 + (by1 - by0) * 0.12), ext, font=f, fill="#ffffff")
    return im.resize((size, size), Image.LANCZOS)


def faded(im, bg: str = "#ffffff", amount: float = 0.6):
    """A dimmed copy (the file was moved or deleted)."""
    from PIL import Image
    base = Image.new("RGBA", im.size, bg)
    top = im.convert("RGBA")
    out = Image.alpha_composite(base, top)
    return Image.blend(out, base, amount)


def card_image(path, present: bool, size: int = SIZE, dark: bool = False, bg: str = "#ffffff", content=None):
    """What a card shows: the content preview with a thin frame, else the type icon; dimmed if missing."""
    from PIL import Image, ImageDraw
    if content is not None:
        im = content.convert("RGBA")
        framed = Image.new("RGBA", (im.width + 2, im.height + 2), "#5a5a5a" if dark else "#c8ccd2")
        framed.paste(im, (1, 1))
        im = framed
    else:
        im = type_icon(path, size, dark)
    if not present:
        im = faded(im, bg)
        d = ImageDraw.Draw(im)
        r = max(10, size // 7)
        cx, cy = im.width - r - 2, im.height - r - 2
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="#9aa0a6")
        d.line((cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45), fill="#ffffff", width=max(2, r // 4))
        d.line((cx - r * 0.45, cy + r * 0.45, cx + r * 0.45, cy - r * 0.45), fill="#ffffff", width=max(2, r // 4))
    return im


def to_png(im) -> bytes:
    """PNG bytes for tk.PhotoImage(data=…) (no ImageTk needed)."""
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def card_file(job: dict, present) -> tuple[str | None, bool]:
    """Which file a card shows: the first output that exists, else the first output, else the first source."""
    outs = job.get("outputs") or []
    found = next((o for o in outs if present(o)), None)
    if found:
        return found, True
    if outs:
        return outs[0], False
    ins = job.get("inputs") or []
    if ins:
        return ins[0], present(ins[0])
    return None, False


def stamp(path) -> tuple:
    """Cache key part: a changed file gets a new preview."""
    try:
        st = os.stat(path)
        return (st.st_size, st.st_mtime)
    except OSError:
        return (None, None)
