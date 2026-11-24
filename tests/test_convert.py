"""Conversions: Word (flowing + exact layout), Excel, PowerPoint, OCR, images, Office."""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path
from xml.dom import minidom

import pymupdf
import pytest

from conftest import ROOT, TR_TEXT, needs_ocr, needs_soffice, noop
from riffle import tools
from riffle.actions import images_to_pdf, office_to_pdf, pdf_to_word

sys.path.insert(0, str(ROOT / "bench"))
from bench import word_risks  # noqa: E402


def docx_xml(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        x = z.read("word/document.xml").decode("utf-8")
        for part in z.namelist():
            if part.endswith(".xml") or part.endswith(".rels"):
                minidom.parseString(z.read(part))   # every XML part must be well-formed
    return x


def docx_text(x: str) -> str:
    return " ".join(re.findall(r"<w:t(?: [^>]*)?>(.*?)</w:t>", x, re.S))


# ----------------------------------------------------------------- Word

def test_pdf_to_word_flow(pdf):
    out = pdf_to_word([pdf], noop)[0]
    x = docx_xml(out)
    t = docx_text(x)
    for w in ("Başlık", "Paragraf", "Görev", "Kişi"):
        assert w in t
    assert "<w:tbl>" in x, "çizgili tablo Word tablosu olmalı"
    assert word_risks(x) == [], word_risks(x)
    grids = re.findall(r'<w:gridCol w:w="(\d+)"', x)
    assert grids, "tablo ızgarası yok"


def test_pdf_to_word_ink_kept_as_picture_text_intact(inked):
    """The pen tick doesn't become a layout-breaking shape: the text stays whole and the
    tick comes back as a floating picture (no 'not transferred' notice)."""
    msgs = []
    out = pdf_to_word([inked], msgs.append)[0]
    x = docx_xml(out)
    assert "Başlık" in docx_text(x)
    assert 'name="Pen 1"' in x and "<wp:wrapNone/>" in x
    assert not any("el yazısı" in m for m in msgs)


def test_pdf_to_word_cropped_page_keeps_text(cropped):
    out = pdf_to_word([cropped], noop)[0]
    t = docx_text(docx_xml(out))
    assert "Paragraf" in t and "Görev" in t


def test_pdf_to_word_exact(pdf):
    out = tools.pdf_to_word_exact([pdf], noop)[0]
    x = docx_xml(out)
    assert "mc:AlternateContent" in x and "<wps:wsp>" in x
    assert "Başlık" in docx_text(x)
    assert x.count("<w:sectPr") == 3, "her PDF sayfası bir Word sayfası olmalı"
    assert word_risks(x) == []


def test_slides_auto_detected(work):
    doc = pymupdf.open()
    for i in range(3):
        p = doc.new_page(width=960, height=540)
        p.draw_rect(pymupdf.Rect(0, 0, 960, 80), color=None, fill=(0.2, 0.2, 0.6))
        p.insert_text((40, 60), f"Slayt {i + 1}", fontsize=30, color=(1, 1, 1))
        p.insert_text((60, 200), "Madde bir", fontsize=20)
    f = work / "sunum.pdf"
    doc.save(f)
    msgs = []
    out = pdf_to_word([f], msgs.append)[0]
    assert any("sunum algılandı" in m for m in msgs)
    x = docx_xml(out)
    assert 'w:orient="landscape"' in x and "Slayt 2" in docx_text(x)


# ----------------------------------------------------------------- Excel / PowerPoint

def test_pdf_to_excel(pdf):
    openpyxl = pytest.importorskip("openpyxl")
    out = tools.pdf_to_excel([pdf], noop)[0]
    wb = openpyxl.load_workbook(out)
    assert len(wb.worksheets) == 3          # one table per page
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    assert rows[0] == ("Ad", "Görev", "Tarih")
    assert rows[1][0] == "Kişi 1"
    assert hasattr(rows[1][2], "year"), "tarih hücresi gerçek tarih olmalı"


def test_pdf_to_excel_no_table(work):
    from conftest import make_text_pdf
    f = make_text_pdf(work / "tablosuz.pdf", pages=1, table=False)
    with pytest.raises(Exception, match="tablo bulunamadı"):
        tools.pdf_to_excel([f], noop)


def test_pdf_to_pptx(pdf):
    pptx = pytest.importorskip("pptx")
    out = tools.pdf_to_pptx([pdf], noop)[0]
    prs = pptx.Presentation(out)
    assert len(prs.slides) == 3
    texts = [sh.text_frame.text for sh in prs.slides[0].shapes if sh.has_text_frame]
    assert any("Başlık 1" in t for t in texts)


# ----------------------------------------------------------------- OCR

@needs_ocr
def test_ocr_searchable(scanned):
    with pymupdf.open(scanned) as d:
        assert len(d[0].get_text().strip()) == 0
    out = tools.make_searchable([scanned], noop)[0]
    with pymupdf.open(out) as d:
        text = d[0].get_text()
        assert "Paragraf" in text and "sayfa" in text
        assert d[0].search_for("Başlık"), "Türkçe kelime aranabilir olmalı"


@needs_ocr
def test_ocr_skips_digital(pdf):
    msgs = []
    assert tools.make_searchable([pdf], msgs.append) == []
    assert any("OCR gerekmedi" in m for m in msgs)


@needs_ocr
def test_scanned_to_word_is_editable_text_with_table(scanned):
    msgs = []
    out = pdf_to_word([scanned], msgs.append)[0]
    assert any("OCR ile metne" in m for m in msgs)
    x = docx_xml(out)
    t = docx_text(x)
    words = ["Paragraf", "satırı", "Görev", "Kişi", "Tarih"]
    hit = sum(1 for w in words if w in t)
    assert hit >= 4, f"OCR metni eksik: {t[:300]}"
    assert "<w:tbl>" in x, "taranmış tablo Word tablosuna dönmeli"


# ----------------------------------------------------------------- images

def test_images_to_pdf_natural_order(images):
    out = images_to_pdf(images, noop)[0]
    with pymupdf.open(out) as d:
        assert len(d) == 3
        widths = [round(p.rect.width) for p in d]
    # order foto 1 (1400), foto 2 (1300), foto 10 (1200)
    assert widths == sorted(widths, reverse=True)


def test_heic_supported(heic):
    jpg = tools.images_to_jpg([heic], noop)[0]
    assert jpg.suffix == ".jpg"
    out = images_to_pdf([heic], noop)[0]
    with pymupdf.open(out) as d:
        assert len(d) == 1


def test_png_alpha_to_jpg_white_background(images):
    from PIL import Image
    out = tools.images_to_jpg([images[0]], noop)[0]
    with Image.open(out) as im:
        assert im.mode == "RGB"
        r, g, b = im.getpixel((5, 5))
        assert r > 200 and g > 100, "şeffaf alan beyazla karışmalı"


def test_shrink_images(images):
    from PIL import Image
    out = tools.shrink_images([images[1]], noop, max_side=800)[0]
    with Image.open(out) as im:
        assert max(im.size) == 800


# ----------------------------------------------------------------- Office

@needs_soffice
def test_office_txt_to_pdf(work):
    f = work / "not defteri.txt"
    f.write_text(TR_TEXT + "\nikinci satır", encoding="utf-8")
    out = office_to_pdf([f], noop)[0]
    with pymupdf.open(out) as d:
        assert "ikinci" in d[0].get_text()
    again = office_to_pdf([f], noop)[0]
    assert again != out and again.exists(), "ikinci çeviri ilkini ezmemeli"


# ----------------------------------------------------------------- OCR decision rules

def _words(confs):
    from riffle.ocr import Word
    return [Word("kelime", pymupdf.Rect(0, 0, 10, 10), c, (1, 1, i)) for i, c in enumerate(confs)]


def test_text_page_rule_printed_vs_handwriting():
    from riffle.ocr import is_text_page
    printed = _words([96] * 180 + [40] * 20)            # printed scan: high confidence
    handwriting = _words([45] * 60 + [70] * 40 + [10] * 50)   # handwriting: scattered, low
    assert is_text_page(printed)
    assert not is_text_page(handwriting)
    assert not is_text_page(_words([99] * 10)), "çok az kelime → resim kalsın"


def test_scanned_without_tesseract_falls_back_to_image(scanned, monkeypatch):
    from riffle import ocr
    monkeypatch.setattr(ocr, "find_tesseract", lambda: None)
    msgs = []
    out = pdf_to_word([scanned], msgs.append)[0]
    assert any("OCR yapılamadı" in m for m in msgs)
    x = docx_xml(out)
    assert "<a:blip " in x, "OCR yoksa sayfa resim olarak gelmeli"
