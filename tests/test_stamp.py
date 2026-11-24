"""Signature / text / image placement model."""

import io

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFilter

from conftest import make_text_pdf
from riffle import stamp_model as sm


def _red_png(w=200, h=100):
    im = Image.new("RGBA", (w, h), (220, 20, 20, 255))
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def _signature_photo(path):
    """Photo of a blue ballpoint signature on grey, shadowed paper."""
    w, h = 1200, 700
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            v = int(205 + 35 * x / w - 25 * y / h)          # sloping light
            px[x, y] = (v, v, v - 6)
    d = ImageDraw.Draw(im)
    pts = [(300 + i * 12, 350 + int(60 * ((i % 7) - 3) / 3)) for i in range(45)]
    d.line(pts, fill=(30, 40, 110), width=7)
    d.line([(320, 460), (850, 430)], fill=(30, 40, 110), width=5)
    im = im.filter(ImageFilter.GaussianBlur(1.2))
    im.save(path, quality=88)
    return path


def test_clean_signature(tmp_path):
    png = sm.clean_signature(_signature_photo(tmp_path / "imza.jpg"))
    im = Image.open(io.BytesIO(png))
    assert im.mode == "RGBA"
    w, h = im.size
    assert 500 < w < 640 and 150 < h < 260                   # tightly cropped (paper dropped)
    a = im.getchannel("A")
    assert a.getpixel((2, 2)) == 0                           # transparent background
    assert max(a.tobytes()) == 255
    # share of non-transparent pixels is reasonable (shadow/paper not counted as ink)
    ink = sum(1 for v in a.tobytes() if v > 128) / (w * h)
    assert 0.02 < ink < 0.35


def test_clean_signature_blank(tmp_path):
    Image.new("RGB", (400, 300), (235, 235, 235)).save(tmp_path / "bos.png")
    with pytest.raises(sm.ConversionError):
        sm.clean_signature(tmp_path / "bos.png")


def test_strokes_to_png():
    png = sm.strokes_to_png([[(10, 10), (100, 60), (200, 20)], [(50, 80)]])
    im = Image.open(io.BytesIO(png))
    assert im.mode == "RGBA" and 190 <= im.width <= 200 and 70 <= im.height <= 80
    with pytest.raises(sm.ConversionError):
        sm.strokes_to_png([])


def test_place_image_keeps_aspect():
    page = pymupdf.Rect(0, 0, 595, 842)
    r = sm.place_image(_red_png(400, 100), page, width=200)
    assert abs((r[2] - r[0]) / (r[3] - r[1]) - 4) < 0.01
    assert 0 <= r[0] and r[2] <= 595 and r[3] <= 842
    r = sm.place_image(_red_png(100, 2000), page, width=300)          # a very tall image fits the page
    assert r[3] - r[1] <= 842 * 0.8 + 0.1


def test_clamp():
    page = pymupdf.Rect(0, 0, 100, 100)
    assert sm.clamp((90, -5, 110, 15), page) == (80, 0, 100, 20)


@pytest.mark.parametrize("rot", [0, 90, 180, 270])
def test_apply_rotated_pages(tmp_path, rot):
    src = make_text_pdf(tmp_path / "r.pdf", pages=1, table=False)
    d = pymupdf.open(src)
    d[0].set_rotation(rot)
    d.save(tmp_path / f"r{rot}.pdf")
    vis_page = pymupdf.open(tmp_path / f"r{rot}.pdf")[0].rect
    img_rect = (40, 60, 140, 110)                      # visible coordinates, top left
    txt = sm.Stamp("text", 0, sm.text_rect("İmza: Halil Ğüşıöç", (40, vis_page.height - 80), 14),
                   text="İmza: Halil Ğüşıöç")
    out = sm.apply(tmp_path / f"r{rot}.pdf",
                   [sm.Stamp("image", 0, img_rect, data=_red_png()), txt])
    assert out.name == f"r{rot}_imzali.pdf"
    page = pymupdf.open(out)[0]
    assert page.rotation == rot
    pix = page.get_pixmap(dpi=72)
    assert pix.pixel(90, 85)[:3] == (220, 20, 20)        # the image is where it appears
    assert pix.pixel(200, 85)[:3] != (220, 20, 20)
    words = [w for w in page.get_text("words") if "Halil" in w[4]]
    assert words, page.get_text()
    # the text is also at the visible position (convert to visible coordinates with rotation_matrix)
    r = pymupdf.Rect(words[0][:4]) * page.rotation_matrix
    assert abs(r.y1 - (vis_page.height - 80 + 17)) < 12 and r.x0 < 120
    assert "Ğüşıöç" in page.get_text()


def test_apply_text_kept_inside_box(tmp_path):
    src = make_text_pdf(tmp_path / "t.pdf", pages=1, table=False)
    text = "Onaylandı\n23.11.2026"
    rect = sm.text_rect(text, (300, 600), 18)
    out = sm.apply(src, [sm.Stamp("text", 0, rect, text=text)])
    t = pymupdf.open(out)[0].get_text()
    assert "Onaylandı" in t and "23.11.2026" in t                  # both lines fit, nothing cut


def test_copy_to_pages(tmp_path):
    s = sm.Stamp("image", 1, (10, 10, 50, 30), data=_red_png())
    copies = sm.copy_to_pages(s, [0, 1, 2, 3])
    assert [c.page for c in copies] == [0, 2, 3]
    assert len({c.uid for c in copies} | {s.uid}) == 4
    src = make_text_pdf(tmp_path / "c.pdf", pages=4, table=False)
    out = sm.apply(src, [s] + copies)
    assert all(len(p.get_images()) == 1 for p in pymupdf.open(out))


def test_config_dir_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert sm.signature_path().parent.exists()
    assert str(tmp_path) in str(sm.signature_path())
