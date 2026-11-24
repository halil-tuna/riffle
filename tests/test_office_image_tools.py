"""Word / Excel / PowerPoint / CSV and image tools (0.9.0)."""

import io
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from riffle import officetools, imagetools, update
from riffle.actions import ConversionError

noop = lambda m: None  # noqa: E731


def _photo(w=2400, h=1600, seed=1):
    """Noisy, photo-like image (compresses poorly)."""
    import numpy as np
    from PIL import Image
    rng = np.random.default_rng(seed)
    base = np.linspace(0, 255, w, dtype=np.float32)[None, :, None].repeat(h, 0).repeat(3, 2)
    noise = rng.normal(0, 40, (h, w, 3))
    return Image.fromarray(np.clip(base + noise, 0, 255).astype("uint8"))


def _png_bytes(im):
    b = io.BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


@pytest.fixture
def docx_with_images(tmp_path):
    import docx
    from docx.shared import Cm
    d = docx.Document()
    d.add_heading("Ödev Raporu — Giriş", 1)
    d.add_paragraph("Şu çalışmada ölçümler özetlenmiştir.")
    big = tmp_path / "foto.png"
    big.write_bytes(_png_bytes(_photo()))
    d.add_picture(str(big), width=Cm(12))
    d.add_picture(str(big), width=Cm(6))             # the same image a second time (the docx keeps one copy or two)
    small = tmp_path / "logo.png"
    _photo(300, 200, seed=2).save(small)
    d.add_picture(str(small), width=Cm(3))
    p = tmp_path / "Çalışma Raporu.docx"
    d.save(p)
    return p


# ---------------------------------------------------------------- shrink Office files

def test_compress_docx_smaller_and_valid(docx_with_images):
    import docx
    out = officetools.compress_office([docx_with_images], noop)[0]
    assert out.name == "Çalışma Raporu_kucuk.docx"
    assert out.stat().st_size < docx_with_images.stat().st_size * 0.6
    d = docx.Document(str(out))                      # opens
    assert "Ödev Raporu" in d.paragraphs[0].text
    assert len(d.inline_shapes) == 3                 # images in place
    with zipfile.ZipFile(out) as z:
        ct = z.read("[Content_Types].xml").decode()
        media = [n for n in z.namelist() if n.startswith("word/media/")]
        rels = z.read("word/_rels/document.xml.rels").decode()
    for m in media:                                  # every media file is declared in the relationships and content types
        assert m.rsplit("/", 1)[1] in rels
        assert f'Extension="{m.rsplit(".", 1)[1]}"' in ct or m.rsplit(".", 1)[1] in ("png", "jpeg", "jpg")


def test_compress_pptx_valid(tmp_path):
    import pptx
    from pptx.util import Inches
    prs = pptx.Presentation()
    img = tmp_path / "f.jpg"
    _photo().save(img, quality=98)
    for _ in range(2):
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.shapes.add_picture(str(img), Inches(0.5), Inches(0.5), width=Inches(8))
    p = tmp_path / "sunum.pptx"
    prs.save(p)
    out = officetools.compress_office([p], noop)[0]
    assert out.stat().st_size < p.stat().st_size * 0.7
    assert len(pptx.Presentation(str(out)).slides) == 2


def test_compress_nothing_to_do(tmp_path):
    import docx
    d = docx.Document()
    d.add_paragraph("sadece yazı")
    p = tmp_path / "yazi.docx"
    d.save(p)
    with pytest.raises(ConversionError):
        officetools.compress_office([p], noop)


# ---------------------------------------------------------------- merge Word files

def test_merge_word_keeps_order_images_lists(tmp_path, docx_with_images):
    import docx
    b = docx.Document()
    b.add_heading("Bölüm 2: Yöntem", 1)
    b.add_paragraph("Birinci adım", style="List Number")
    b.add_paragraph("İkinci adım", style="List Number")
    bp = tmp_path / "b.docx"
    b.save(bp)
    out = officetools.merge_word([bp, docx_with_images], noop, ordered=True)[0]
    assert out.name == "b_birlesik.docx"
    d = docx.Document(str(out))
    texts = [p.text for p in d.paragraphs if p.text]
    assert texts.index("Bölüm 2: Yöntem") < texts.index("Ödev Raporu — Giriş")
    assert len(d.inline_shapes) == 3
    xml = d.element.xml
    assert 'w:type="page"' in xml                    # the second document starts on a new page


def test_merge_word_needs_two(tmp_path, docx_with_images):
    with pytest.raises(ConversionError):
        officetools.merge_word([docx_with_images], noop)


# ---------------------------------------------------------------- CSV → Excel

def test_csv_turkish_windows_semicolon(tmp_path):
    import openpyxl
    rows = ["Ad Soyad;TC Kimlik;Telefon;Tutar;Tarih;Şehir",
            "Ayşe Yılmaz;12345678901;05321234567;1.234,56;03.11.2026;İzmir",
            "Çağrı Şen;10987654321;05551112233;12,5;15.01.2026;Muğla",
            "Gül Öztürk;11122233344;02121234567;7;29.02.2025;Iğdır"]
    p = tmp_path / "liste.csv"
    p.write_bytes(("\r\n".join(rows) + "\r\n").encode("cp1254"))
    notes = []
    out = officetools.csv_to_excel([p], notes.append)[0]
    ws = openpyxl.load_workbook(out).active
    assert [c.value for c in ws[1]] == ["Ad Soyad", "TC Kimlik", "Telefon", "Tutar", "Tarih", "Şehir"]
    assert ws["A2"].value == "Ayşe Yılmaz" and ws["F4"].value == "Iğdır"
    assert ws["B2"].value == "12345678901"            # national ID: stays text (no scientific notation)
    assert ws["C2"].value == "05321234567"            # leading zero is kept
    assert ws["D2"].value == pytest.approx(1234.56) and ws["D3"].value == pytest.approx(12.5)
    assert ws["D4"].value == 7
    assert ws["E2"].value == datetime(2026, 11, 3)
    assert ws["E4"].value == "29.02.2025"             # an invalid date stays text
    assert ws.freeze_panes == "A2"
    assert any("Windows-1254" in n and "noktalı virgül" in n for n in notes)


def test_csv_utf8_comma_english_numbers(tmp_path):
    import openpyxl
    p = tmp_path / "data.csv"
    p.write_text('name,amount,code\n"Doe, John","1,234.50",007\nŞule,12.25,42\n', encoding="utf-8")
    out = officetools.csv_to_excel([p], noop)[0]
    ws = openpyxl.load_workbook(out).active
    assert ws["A2"].value == "Doe, John" and ws["A3"].value == "Şule"
    assert ws["B2"].value == pytest.approx(1234.5) and ws["B3"].value == pytest.approx(12.25)
    assert ws["C2"].value == "007" and ws["C3"].value == 42


# ---------------------------------------------------------------- handouts / extract images

def test_handout_three_per_page(tmp_path):
    import pymupdf
    src = tmp_path / "slaytlar.pdf"
    d = pymupdf.open()
    for i in range(7):
        pg = d.new_page(width=960, height=540)
        pg.insert_text((60, 100), f"Slayt {i + 1}", fontsize=40)
    d.save(src)
    out = officetools.slide_handout([src], noop)[0]
    with pymupdf.open(out) as r:
        assert len(r) == 3
        assert r[0].rect.width < r[0].rect.height    # A4 portrait
        assert "Slayt 1" in r[0].get_text() and "Slayt 7" in r[2].get_text()


@pytest.mark.skipif(not shutil.which("soffice") and not shutil.which("libreoffice"), reason="LibreOffice yok")
def test_handout_from_pptx(tmp_path):
    import pptx
    import pymupdf
    prs = pptx.Presentation()
    for i in range(4):
        s = prs.slides.add_slide(prs.slide_layouts[1])
        s.shapes.title.text = f"Konu {i + 1}"
    p = tmp_path / "ders.pptx"
    prs.save(p)
    out = officetools.slide_handout([p], noop)[0]
    with pymupdf.open(out) as r:
        assert len(r) == 2 and "Konu 4" in r[1].get_text()


def test_extract_office_images_dedup(docx_with_images):
    folder = officetools.extract_office_images([docx_with_images], noop)[0]
    files = sorted(folder.iterdir())
    assert folder.name == "Çalışma Raporu_resimler"
    assert 2 <= len(files) <= 2                       # the same photo once + the logo


# ---------------------------------------------------------------- images

def test_image_to_target(tmp_path):
    p = tmp_path / "vesikalık.png"
    _photo(3000, 4000).save(p)
    notes = []
    out = imagetools.image_to_target([p], notes.append, target="200")[0]
    assert out.suffix == ".jpg" and out.stat().st_size <= 200 * 1024
    assert out.stat().st_size > 120 * 1024            # close to the target: no quality wasted
    with pytest.raises(ConversionError):
        imagetools._parse_kb("abc")
    assert imagetools._parse_kb("1.5mb") == int(1.5 * 1024 * 1024)


def test_png_and_rotate(tmp_path):
    from PIL import Image
    p = tmp_path / "yatay.jpg"
    _photo(400, 200).save(p)
    png = imagetools.images_to_png([p], noop)[0]
    assert png.suffix == ".png" and Image.open(png).size == (400, 200)
    r = imagetools.rotate_images([p], noop, angle=90)[0]
    assert r.name == "yatay_dondurulmus.jpg" and Image.open(r).size == (200, 400)
    left = imagetools.rotate_images([p], noop, angle=-90)[0]
    a, b = Image.open(r).convert("L"), Image.open(left).convert("L")
    assert a.getpixel((0, 0)) != b.getpixel((0, 0)) or a.getpixel((199, 0)) != b.getpixel((199, 0))


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract yok")
def test_image_text_to_txt_and_clipboard(tmp_path):
    from PIL import Image, ImageDraw, ImageFont
    from riffle.ocr import _font_file
    im = Image.new("RGB", (1400, 420), "white")
    dr = ImageDraw.Draw(im)
    f = ImageFont.truetype(_font_file(), 44)
    dr.text((60, 60), "Proje teslim tarihi: 12 Aralık", fill="black", font=f)
    dr.text((60, 160), "Öğrenciler raporu PDF olarak yükleyecek.", fill="black", font=f)
    p = tmp_path / "ekran görüntüsü.png"
    im.save(p)
    msgs = []
    out = imagetools.image_text([p], msgs.append)[0]
    text = out.read_text(encoding="utf-8")
    assert "Proje teslim tarihi" in text and "Öğrenciler" in text
    assert text.index("Proje") < text.index("Öğrenciler")
    clip = [m for m in msgs if m.startswith(imagetools.CLIPBOARD)]
    assert clip and "Aralık" in clip[-1]


# ---------------------------------------------------------------- update notice

def test_update_version_compare_and_cache(monkeypatch, tmp_path):
    monkeypatch.delenv("RIFFLE_NO_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update, "_state_file", lambda: tmp_path / "g.json")
    calls = []
    monkeypatch.setattr(update, "fetch", lambda timeout=4.0: calls.append(1) or "99.0.0")
    assert update.check() == "99.0.0"
    assert update.check() == "99.0.0" and len(calls) == 1      # once a week: doesn't go online a second time
    monkeypatch.setattr(update, "fetch", lambda timeout=4.0: "0.0.1")
    assert update.check(force=True) is None                    # older version: no notice
    assert update._ver("v0.10.0") > update._ver("0.9.9")


def test_update_disabled_by_env(monkeypatch):
    monkeypatch.setenv("RIFFLE_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(update, "fetch", lambda timeout=4.0: pytest.fail("ağa çıkmamalı"))
    assert update.check() is None


def test_new_actions_in_menus():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import install
    from riffle.actions import actions_for_extension
    ids = lambda ext: {a.id for a in actions_for_extension(ext)}  # noqa: E731
    assert {"wordmerge", "officeshrink", "officeimg"} <= ids("docx")
    assert {"handout", "officeshrink"} <= ids("pptx") and "handout" in ids("ppt")
    assert "csv2xlsx" in ids("csv") and "wordmerge" not in ids("doc")
    assert {"imgocr", "imgtarget", "imgrotr", "imgrotl", "img2png"} <= ids("jpg")
    assert "img2png" not in ids("png")
    for ext in ("docx", "pptx", "xlsx", "csv", "jpg", "png", "heic"):
        for _title, nodes in install.win_cascades(ext):
            assert sum((1 if g else 0) + len(a) for g, a in nodes) <= install.WIN_MAX_ITEMS


def test_csv_us_dates_and_western_encoding(tmp_path):
    import openpyxl
    rows = ["Name;City;Date;Amount", "Jürgen;Straße 5, Köln;12/25/2026;1.234,50",
            "Þór;Reykjavík;03/11/2026;7,25", "Anaïs;Café;01/02/2026;3"]
    p = tmp_path / "export.csv"
    p.write_bytes(("\n".join(rows) + "\n").encode("cp1252"))
    ws = openpyxl.load_workbook(officetools.csv_to_excel([p], noop)[0]).active
    assert ws["A3"].value == "Þór" and ws["B2"].value == "Straße 5, Köln" and ws["A4"].value == "Anaïs"
    assert ws["C2"].value == datetime(2026, 12, 25) and ws["C3"].value == datetime(2026, 3, 11)   # US: month first
    assert ws["D2"].value == pytest.approx(1234.5)


def test_csv_ambiguous_slash_dates_stay_text(tmp_path):
    import openpyxl
    p = tmp_path / "d.csv"
    p.write_text("tarih,not\n03/11/2026,a\n04/05/2026,b\n", encoding="utf-8")
    ws = openpyxl.load_workbook(officetools.csv_to_excel([p], noop)[0]).active
    assert ws["A2"].value == "03/11/2026"          # 3 November or 11 March is unclear: don't produce a wrong date


@pytest.mark.skipif(not shutil.which("tesseract"), reason="Tesseract yok")
def test_image_text_marks_unreadable_equation(tmp_path):
    """An unreadable equation/figure doesn't vanish silently: a marker stays in its place, text order is kept."""
    from PIL import Image, ImageDraw, ImageFont
    from riffle.ocr import _font_file
    im = Image.new("RGB", (1400, 520), "white")
    dr = ImageDraw.Draw(im)
    f = ImageFont.truetype(_font_file(), 40)
    dr.text((60, 40), "The bending stress is given by", fill="black", font=f)
    for k in range(5):                              # a drawing that is not words (in place of a formula / figure)
        dr.arc((200 + 90 * k, 150, 280 + 90 * k, 260), 200, 340, fill="black", width=5)
    dr.line((200, 205, 700, 205), fill="black", width=4)
    dr.text((60, 330), "where c is the distance from the axis.", fill="black", font=f)
    p = tmp_path / "formul.png"
    im.save(p)
    text = imagetools.image_text([p], noop)[0].read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l]
    assert lines[0].startswith("The bending stress") and imagetools.MARK in lines[1]
    assert "distance from the axis" in lines[-1]
    assert imagetools._fix_symbols("€y ve €5") == "εy ve €5"
