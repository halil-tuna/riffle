"""Previews for the History tab's icon view (riffle/thumbs.py). Nothing here may write to disk."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from PIL import Image

from conftest import make_text_pdf

from riffle import thumbs


def test_kinds(tmp_path):
    assert [thumbs.kind(n) for n in ("a.PDF", "b.docx", "c.xlsx", "d.pptx", "e.HEIC", "f.txt", "g.zip", "h.xyz")] == \
        ["pdf", "word", "excel", "ppt", "image", "text", "archive", "other"]
    assert thumbs.kind(tmp_path) == "folder"


def test_pdf_preview(tmp_path):
    pdf = make_text_pdf(tmp_path / "rapor.pdf", pages=2)
    im = thumbs.preview(pdf, 128)
    assert im.height == 128 and 80 < im.width < 100                 # A4 portrait fits the height
    wide = tmp_path / "slayt.pdf"
    import pymupdf
    d = pymupdf.open()
    d.new_page(width=960, height=540)
    d.save(wide)
    im = thumbs.preview(wide, 128)
    assert im.width == 128 and im.height == 72


def test_pdf_preview_not_possible(tmp_path, encrypted, garbage):
    assert thumbs.preview(encrypted) is None                        # needs a password: type icon instead
    assert thumbs.preview(garbage) is None
    assert thumbs.preview(tmp_path / "yok.pdf") is None
    assert thumbs.preview(tmp_path) is None


def test_image_previews(images, heic):
    for p in images + [heic]:
        im = thumbs.preview(p, 100)
        assert max(im.size) == 100, p.name


def test_exif_rotation(tmp_path):
    p = tmp_path / "dik.jpg"
    im = Image.new("RGB", (400, 200), (200, 50, 50))
    exif = Image.Exif()
    exif[0x0112] = 6                                                 # "rotate 90°" (phone photo held upright)
    im.save(p, exif=exif.tobytes())
    assert thumbs.preview(p, 100).size == (50, 100)


@pytest.mark.skipif(sys.platform == "win32", reason="Windows: Gezgin önizlemesi kullanılır")
def test_other_types_have_no_preview_off_windows(tmp_path):
    p = tmp_path / "rapor.docx"
    p.write_bytes(b"PK")
    assert thumbs.preview(p) is None


@pytest.mark.skipif(sys.platform != "win32", reason="yalnızca Windows")
def test_windows_shell_preview(tmp_path):
    """File Explorer's thumbnail / icon through IShellItemImageFactory (runs on the Windows CI)."""
    thumbs.com_init()
    for name, data in (("not.txt", b"merhaba"), ("tablo.csv", b"a;b\n1;2\n")):
        p = tmp_path / name
        p.write_bytes(data)
        im = thumbs.preview(p, 96)
        assert im is not None and 0 < max(im.size) <= 96, name


def test_type_icons():
    for name in ("a.pdf", "a.docx", "a.xlsx", "a.pptx", "a.png", "a.verylongext", "README"):
        for dark in (False, True):
            im = thumbs.type_icon(name, 120, dark)
            assert im.size == (120, 120) and im.mode == "RGBA"
            assert im.getchannel("A").getextrema() == (0, 255)        # transparent around the page


def test_card_images(tmp_path):
    pdf = make_text_pdf(tmp_path / "a.pdf", pages=1)
    real = thumbs.card_image(pdf, True, 120, content=thumbs.preview(pdf, 120))
    assert real.height == 122                                         # 1 px frame around the page
    gone = thumbs.card_image(tmp_path / "gitti.docx", False, 120, bg="#ffffff")
    icon = thumbs.type_icon("gitti.docx", 120)
    assert gone.size == icon.size
    band = (60, 85)                                                   # inside the coloured band: lighter when gone
    assert sum(gone.getpixel(band)[:3]) > sum(Image.alpha_composite(
        Image.new("RGBA", icon.size, "#ffffff"), icon).getpixel(band)[:3])
    png = thumbs.to_png(real)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_card_file():
    there = {"/x/b.pdf", "/x/kaynak.pdf"}.__contains__
    assert thumbs.card_file({"outputs": ["/x/a.pdf", "/x/b.pdf"], "inputs": []}, there) == ("/x/b.pdf", True)
    assert thumbs.card_file({"outputs": ["/x/a.pdf"], "inputs": ["/x/kaynak.pdf"]}, there) == ("/x/a.pdf", False)
    assert thumbs.card_file({"outputs": [], "inputs": ["/x/kaynak.pdf"]}, there) == ("/x/kaynak.pdf", True)
    assert thumbs.card_file({"outputs": [], "inputs": []}, there) == (None, False)


def test_nothing_written(tmp_path, monkeypatch, images):
    """Previews stay in memory: making them leaves no file behind (working folder, temp, settings)."""
    import os
    import tempfile
    src = tmp_path / "src"
    src.mkdir()
    pdf = make_text_pdf(src / "a.pdf", pages=1)
    work = tmp_path / "cwd"
    work.mkdir()
    monkeypatch.chdir(work)
    tmp_before = set(os.listdir(tempfile.gettempdir()))
    before = sorted(p.name for p in src.iterdir())
    for p in [pdf, *images]:
        thumbs.to_png(thumbs.card_image(p, True, content=thumbs.preview(p)))
    assert sorted(p.name for p in src.iterdir()) == before and not list(work.iterdir())
    assert not {n for n in set(os.listdir(tempfile.gettempdir())) - tmp_before if "riffle" in n.lower()
                or n.lower().endswith((".png", ".jpg"))}


def test_pdf_preview_is_crisper_than_a_plain_render(tmp_path):
    """Supersampled + sharpened: text lines keep more contrast than drawing the page straight at thumbnail size."""
    import pymupdf
    pdf = make_text_pdf(tmp_path / "net.pdf", pages=1)
    new = thumbs.preview(pdf, 144).convert("L")
    with pymupdf.open(pdf) as d:
        p = d[0]
        z = 144 / max(p.rect.width, p.rect.height)
        pix = p.get_pixmap(matrix=pymupdf.Matrix(z, z), alpha=False)
        plain = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("L")
    assert new.size == plain.size

    def darkest(im, share=0.02):                     # mean of the darkest 2 % (the text strokes)
        hist, need, total = im.histogram(), max(1, int(im.width * im.height * share)), 0
        got = 0
        for value, n in enumerate(hist):
            take = min(n, need - got)
            total += value * take
            got += take
            if got >= need:
                break
        return total / need
    assert darkest(new) < darkest(plain) - 3
