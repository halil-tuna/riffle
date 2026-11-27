"""Test files: all synthetic, generated at run time (no personal data)."""

from __future__ import annotations

import io
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pymupdf  # noqa: E402

from riffle.ocr import _font_file  # noqa: E402

# the order window should not open in automated tests (test_ui.py opens it itself)
os.environ.setdefault("RIFFLE_ORDER", "name")
# settings/usage files and menu refreshes must not touch the real user folder
import tempfile as _tf  # noqa: E402
os.environ.setdefault("XDG_CONFIG_HOME", _tf.mkdtemp(prefix="riffle_test_cfg_"))
os.environ.setdefault("RIFFLE_NO_MENU_REFRESH", "1")
os.environ.setdefault("RIFFLE_NO_UPDATE_CHECK", "1")   # tests must not go online
os.environ.setdefault("RIFFLE_NO_WELCOME", "1")        # the first-run window is tested on its own (test_welcome.py)
os.environ.setdefault("RIFFLE_LANG", "tr")             # expected strings are Turkish (English in separate tests)

FONT = _font_file()
TR_TEXT = "Çalışma güçlü şişe ığdır ÖĞÜŞİ"


def _text(page, xy, text, size=11):
    if FONT:
        page.insert_text(xy, text, fontsize=size, fontname="tr", fontfile=FONT)
    else:
        page.insert_text(xy, text, fontsize=size)


def make_text_pdf(path: Path, pages: int = 3, table: bool = True, image: bool = False) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        _text(p, (72, 80), f"Başlık {i + 1}: {TR_TEXT}", 16)
        for k in range(8):
            _text(p, (72, 120 + k * 16), f"Paragraf satırı {k + 1} sayfa {i + 1} — denetim metni öğrenci.")
        if table:
            x0, y0, cw, rh = 72, 300, 150, 24
            for r in range(5):
                for c in range(3):
                    p.draw_rect(pymupdf.Rect(x0 + c * cw, y0 + r * rh, x0 + (c + 1) * cw, y0 + (r + 1) * rh),
                                color=(0, 0, 0), width=0.7)
                    txt = ["Ad", "Görev", "Tarih"][c] if r == 0 else [f"Kişi {r}", f"İş {r}", f"0{r}.10.2026"][c]
                    _text(p, (x0 + c * cw + 4, y0 + r * rh + 16), txt, 10)
        if image:
            from PIL import Image
            import numpy as np
            arr = (np.random.default_rng(i).random((900, 900, 3)) * 255).astype("uint8")
            b = io.BytesIO()
            Image.fromarray(arr).save(b, "PNG")
            p.insert_image(pymupdf.Rect(300, 500, 520, 720), stream=b.getvalue())
    doc.save(path)
    return path


def make_scanned(src: Path, dst: Path) -> Path:
    from PIL import Image
    s = pymupdf.open(src)
    out = pymupdf.open()
    for p in s:
        pix = p.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
        im = Image.frombytes("L", (pix.width, pix.height), pix.samples).rotate(0.5, fillcolor=250)
        b = io.BytesIO()
        im.save(b, "JPEG", quality=70)
        n = out.new_page(width=p.rect.width, height=p.rect.height)
        n.insert_image(n.rect, stream=b.getvalue())
    out.save(dst)
    return dst


@pytest.fixture
def work(tmp_path) -> Path:
    """Every test in its own folder; a Turkish folder name with spaces is chosen on purpose."""
    d = tmp_path / "Çalışma Klasörü"
    d.mkdir()
    return d


@pytest.fixture
def pdf(work) -> Path:
    return make_text_pdf(work / "Ödev Raporu.pdf")


@pytest.fixture
def pdf_img(work) -> Path:
    return make_text_pdf(work / "resimli.pdf", pages=2, image=True)


@pytest.fixture
def encrypted(work, pdf) -> Path:
    out = work / "şifreli.pdf"
    with pymupdf.open(pdf) as d:
        d.save(out, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="1234", owner_pw="1234")
    return out


@pytest.fixture
def corrupted(work, pdf) -> Path:
    out = work / "bozuk.pdf"
    data = pdf.read_bytes()
    out.write_bytes(data[: int(len(data) * 0.85)])   # end (xref) truncated
    return out


@pytest.fixture
def garbage(work) -> Path:
    out = work / "sahte.pdf"
    out.write_bytes(b"bu bir pdf degil" * 10)
    return out


@pytest.fixture
def cropped(work, pdf) -> Path:
    """Page cropped from a large canvas (as in magazine PDFs)."""
    out = work / "kirpilmis.pdf"
    d = pymupdf.open()
    big = d.new_page(width=842, height=1191)
    big.show_pdf_page(pymupdf.Rect(100, 300, 695, 1142), pymupdf.open(pdf), 0)
    big.set_cropbox(pymupdf.Rect(100, 300, 695, 1142))
    d.save(out)
    return out


@pytest.fixture
def rotated(work, pdf) -> Path:
    out = work / "dönük.pdf"
    with pymupdf.open(pdf) as d:
        d[0].set_rotation(90)
        d[1].set_rotation(270)
        d.save(out)
    return out


@pytest.fixture
def inked(work, pdf) -> Path:
    out = work / "notlu.pdf"
    with pymupdf.open(pdf) as d:
        d[0].add_ink_annot([[(100, 100), (120, 130), (160, 90)]])
        d[0].add_highlight_annot(pymupdf.Rect(72, 110, 300, 125))
        d.save(out)
    return out


@pytest.fixture
def scanned(work, pdf) -> Path:
    return make_scanned(pdf, work / "taranmış.pdf")


@pytest.fixture
def images(work) -> list[Path]:
    from PIL import Image
    paths = []
    for i, (name, mode, color) in enumerate([("foto 10.png", "RGBA", (255, 0, 0, 128)),
                                             ("foto 2.jpg", "RGB", (0, 128, 255)),
                                             ("foto 1.webp", "RGB", (0, 200, 0))]):
        p = work / name
        Image.new(mode, (1200 + i * 100, 800), color).save(p)
        paths.append(p)
    return paths


@pytest.fixture
def heic(work) -> Path:
    pillow_heif = pytest.importorskip("pillow_heif")
    from PIL import Image
    pillow_heif.register_heif_opener()
    p = work / "IMG_0001.HEIC"
    Image.new("RGB", (640, 480), (200, 100, 50)).save(p, format="HEIF")
    return p


def has_tesseract() -> bool:
    from riffle import ocr
    try:
        ocr.require()
        return True
    except ocr.OcrUnavailable:
        return False


needs_ocr = pytest.mark.skipif(not has_tesseract(), reason="Tesseract yok")
needs_soffice = pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")),
                                   reason="LibreOffice yok")


def noop(_msg):
    pass


def camera_corners(W=3200, H=2400, tilt_x=28, tilt_y=12, rot_z=6, dist=520.0, f=2900.0):
    """Real pinhole camera: the corners of an A4 sheet (210x297 mm) on a desk in the photo."""
    import numpy as np
    w, h = 210.0, 297.0
    pts = np.array([[-w / 2, -h / 2, 0], [w / 2, -h / 2, 0], [w / 2, h / 2, 0], [-w / 2, h / 2, 0]])
    ax, ay, az = np.radians([tilt_x, tilt_y, rot_z])
    rx = np.array([[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]])
    ry = np.array([[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]])
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    p = pts @ (rz @ ry @ rx).T + [0, 0, dist]
    return [[float(f * x / z + W / 2), float(f * y / z + H / 2)] for x, y, z in p]


TRUE_CORNERS = camera_corners()


def make_photo(path: Path, desk=(40, 70, 110), corners=None, light=True, size=(3200, 2400)) -> Path:
    corners = corners or TRUE_CORNERS
    """Masada eğik çekilmiş belge fotoğrafı (perspektif + ışık eğimi + gürültü)."""
    import cv2
    import numpy as np
    src_pdf = make_text_pdf(path.with_suffix(".kaynak.pdf"), pages=1)
    pix = pymupdf.open(src_pdf)[0].get_pixmap(dpi=150)
    page = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)[:, :, ::-1].copy()
    W, H = size
    rng = np.random.default_rng(3)
    photo = np.zeros((H, W, 3), np.uint8)
    photo[:] = desk
    noise = rng.normal(0, 10, (H, W, 3))
    photo = np.clip(photo.astype(np.float32) + noise, 0, 255).astype(np.uint8)
    ph, pw = page.shape[:2]
    m = cv2.getPerspectiveTransform(np.float32([[0, 0], [pw, 0], [pw, ph], [0, ph]]), np.float32(corners))
    warped = cv2.warpPerspective(page, m, (W, H))
    mask = cv2.warpPerspective(np.full((ph, pw), 255, np.uint8), m, (W, H))
    photo[mask > 0] = warped[mask > 0]
    if light:
        yy, xx = np.mgrid[0:H, 0:W]
        g = 0.55 + 0.45 * (xx / W) * (1 - yy / H * 0.6)
        photo = np.clip(photo * g[..., None], 0, 255).astype(np.uint8)
    # cv2.imwrite can't open a path with Turkish letters on Windows: encode, let numpy write the file
    ok, buf = cv2.imencode(".jpg", cv2.GaussianBlur(photo, (3, 3), 0), [cv2.IMWRITE_JPEG_QUALITY, 85])
    buf.tofile(str(path))
    src_pdf.unlink()
    return path
