"""Redaction, comparison, margin trimming, Markdown, PDF/A, HTML→PDF."""

import shutil
from pathlib import Path

import pymupdf
import pytest

from conftest import _text, make_text_pdf, needs_soffice, noop
from riffle import docops
from riffle.actions import ACTIONS, ConversionError, office_to_pdf


def _form(path: Path, lines, meta=None):
    d = pymupdf.open()
    p = d.new_page()
    for i, t in enumerate(lines):
        _text(p, (60, 80 + i * 22), t)
    if meta:
        d.set_metadata(meta)
    d.save(path)
    return path


# ---------------------------------------------------------------- karartma

@pytest.mark.parametrize("s,ok", [("10000000146", True), ("12345678901", False), ("01234567890", False)])
def test_tc_checksum(s, ok):
    assert docops._tc_ok(s) is ok


@pytest.mark.parametrize("s,ok", [("TR33 0006 1005 1978 6457 8413 26", True),
                                  ("TR00 0000 0000 0000 0000 0000 00", False),
                                  ("DE89370400440532013000", True)])
def test_iban_checksum(s, ok):
    assert docops._iban_ok(s) is ok


def test_find_sensitive_kinds():
    text = ("TC 10000000146 tel 0532 123 45 67, +90 (212) 555 12 34 "
            "IBAN TR33 0006 1005 1978 6457 8413 26 mail a.b@std.yildiz.edu.tr yıl 2026 no 12345678901")
    kinds = sorted(k for k, _ in docops.find_sensitive(text))
    assert kinds == ["IBAN", "TC kimlik", "e-posta", "telefon", "telefon"]


def test_redact_removes_text_and_metadata(tmp_path):
    src = _form(tmp_path / "form.pdf", [
        "Ad: Halil Test", "TC Kimlik No: 10000000146", "Yanlış TC: 12345678901",
        "Tel: 0532 123 45 67", "IBAN: TR33 0006 1005 1978 6457 8413 26",
        "E-posta: halil.test@std.yildiz.edu.tr", "Danışman: Gizli Hoca", "Yıl 2026"],
        meta={"author": "Halil Tuna", "creator": "Word"})
    msgs = []
    out = docops.redact([src], msgs.append, words="Gizli Hoca, yok_kelime")[0]
    assert out.name == "form_karartilmis.pdf"
    doc = pymupdf.open(out)
    t = doc[0].get_text()
    for gone in ("10000000146", "0532", "TR33", "halil.test", "Gizli", "Hoca"):
        assert gone not in t
    for kept in ("Halil Test", "12345678901", "2026", "Danışman"):
        assert kept in t
    assert not doc.metadata.get("author")
    # the black box is really drawn
    pix = doc[0].get_pixmap(dpi=40, colorspace=pymupdf.csGRAY)
    assert min(pix.samples) < 30
    assert any("karartıldı" in m for m in msgs)
    assert src.read_bytes() != out.read_bytes()          # the original is untouched


def test_redact_nothing_found(tmp_path):
    src = _form(tmp_path / "temiz.pdf", ["Merhaba dünya", "Sayı 42"])
    msgs = []
    out = docops.redact([src], msgs.append)[0]
    assert "Merhaba" in pymupdf.open(out)[0].get_text()
    assert any("bulunamadı" in m for m in msgs)


def test_redact_on_image_pixels(tmp_path):
    """The image pixels under the text must be blacked out too (drawing a box on top is not enough)."""
    d = pymupdf.open()
    p = d.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 400, 60), False)
    pix.set_rect(pix.irect, (200, 230, 255))
    p.insert_image(pymupdf.Rect(50, 60, 450, 120), pixmap=pix)
    _text(p, (60, 95), "TC 10000000146")
    d.save(tmp_path / "res.pdf")
    out = docops.redact([tmp_path / "res.pdf"], noop)[0]
    doc = pymupdf.open(out)
    img = doc.extract_image(doc[0].get_images()[0][0])
    im = pymupdf.Pixmap(img["image"])
    row = {im.pixel(x, 30) for x in range(40, 90, 4)}
    assert (200, 230, 255) not in row                                  # the pixels under the text were wiped
    assert im.pixel(380, 30) == (200, 230, 255)                        # the rest is in place


def test_redact_action_registered():
    a = next(a for a in ACTIONS if a.id == "redact")
    assert a.ask and a.ask[0] == "words" and a.ask[2].isspace()


# ---------------------------------------------------------------- comparison

def test_compare_two_versions(tmp_path):
    a = make_text_pdf(tmp_path / "rapor_v1.pdf", pages=2, table=False)
    d = pymupdf.open(a)
    d[1].add_redact_annot(d[1].search_for("Paragraf satırı 3 sayfa 2")[0])
    d[1].apply_redactions()
    _text(d[1], (72, 500), "Yeni eklenen sonuç cümlesi burada.")
    d.save(tmp_path / "rapor_v2.pdf")
    msgs = []
    out = docops.compare([tmp_path / "rapor_v2.pdf", a], msgs.append)[0]   # v1 is the old one even if the order is reversed
    r = pymupdf.open(out)
    assert len(r) == 3
    summary = r[0].get_text()
    assert "Eski sürüm: rapor_v1.pdf" in summary and "Yeni sürüm: rapor_v2.pdf" in summary
    assert "+ Yeni eklenen sonuç cümlesi burada." in summary
    assert "− Paragraf satırı 3 sayfa 2" in summary
    assert [t[1] for t in r.get_toc()] == ["Özet", "Sayfa 1", "Sayfa 2"]
    assert any("eklendi" in m for m in msgs)


def test_compare_identical(tmp_path):
    a = make_text_pdf(tmp_path / "a.pdf", pages=1, table=False)
    shutil.copy(a, tmp_path / "b.pdf")
    out = docops.compare([a, tmp_path / "b.pdf"], noop)[0]
    assert "Metin farkı yok" in pymupdf.open(out)[0].get_text()


def test_compare_needs_two(tmp_path):
    a = make_text_pdf(tmp_path / "a.pdf", pages=1, table=False)
    with pytest.raises(ConversionError):
        docops.compare([a], noop)


def test_compare_different_page_counts(tmp_path):
    a = make_text_pdf(tmp_path / "a.pdf", pages=1, table=False)
    b = make_text_pdf(tmp_path / "b.pdf", pages=3, table=False)
    r = pymupdf.open(docops.compare([a, b], noop)[0])
    assert len(r) == 4


# ---------------------------------------------------------------- trimming

def test_autocrop(tmp_path):
    d = pymupdf.open()
    p = d.new_page()
    p.draw_rect(pymupdf.Rect(200, 300, 400, 500), fill=(0, 0, 0))
    d.new_page()                                                    # the blank page must stay
    d.save(tmp_path / "k.pdf")
    msgs = []
    out = docops.autocrop([tmp_path / "k.pdf"], msgs.append)[0]
    doc = pymupdf.open(out)
    r = doc[0].rect
    assert 220 <= r.width <= 230 and 220 <= r.height <= 230
    assert doc[1].rect == pymupdf.Rect(0, 0, 595, 842)
    # the content is still visible
    pix = doc[0].get_pixmap(dpi=36, colorspace=pymupdf.csGRAY)
    assert min(pix.samples) < 20
    assert any("1 sayfanın" in m for m in msgs)


def test_autocrop_rotated(tmp_path, rotated):
    out = docops.autocrop([rotated], noop)[0]
    src, doc = pymupdf.open(rotated), pymupdf.open(out)
    for a, b in zip(src, doc):
        assert b.rotation == a.rotation
        assert b.rect.width <= a.rect.width and b.rect.height <= a.rect.height
        # visible text was not lost
        assert a.get_text().split() == b.get_text().split()


# ---------------------------------------------------------------- Markdown / PDF/A / HTML

def test_markdown(tmp_path, pdf):
    out = docops.pdf_to_markdown([pdf], noop)[0]
    md = out.read_text(encoding="utf-8")
    assert out.suffix == ".md"
    assert "ÖĞÜŞ" in md or "öğrenci" in md
    assert "|" in md                                                  # the table must become a markdown table


@pytest.mark.skipif(not docops.find_ghostscript(), reason="Ghostscript yok")
def test_pdfa(tmp_path, pdf):
    out = docops.pdf_to_pdfa([pdf], noop)[0]
    doc = pymupdf.open(out)
    assert "OutputIntents" in doc.xref_object(doc.pdf_catalog())
    assert "pdfaid:part" in doc.get_xml_metadata()
    assert "Başlık 1: Çalışma güçlü şişe ığdır ÖĞÜŞİ" in doc[0].get_text()   # ToUnicode fix


def test_pdfa_without_ghostscript(tmp_path, pdf, monkeypatch):
    monkeypatch.setattr(docops, "find_ghostscript", lambda: None)
    with pytest.raises(ConversionError, match="Ghostscript"):
        docops.pdf_to_pdfa([pdf], noop)


@needs_soffice
def test_html_to_pdf(tmp_path):
    h = tmp_path / "sayfa.html"
    h.write_text('<html><meta charset="utf-8"><body><h1>Başlık Ğ</h1><p>Paragraf şöyle.</p></body></html>',
                 encoding="utf-8")
    out = office_to_pdf([h], noop)[0]
    assert "Başlık Ğ" in pymupdf.open(out)[0].get_text()


def test_fix_tounicode_ghostscript(tmp_path, pdf):
    """PyMuPDF's overflowing bfranges turned 'ı' into '1' in Acrobat/Ghostscript."""
    import subprocess
    from riffle.actions import fix_tounicode
    gs = docops.find_ghostscript()
    if not gs:
        pytest.skip("Ghostscript yok")
    doc = pymupdf.open(pdf)
    assert fix_tounicode(doc) >= 1
    doc.save(tmp_path / "f.pdf")
    assert fix_tounicode(pymupdf.open(tmp_path / "f.pdf")) == 0          # not needed a second time
    subprocess.run([gs, "-q", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                    f"-sOutputFile={tmp_path / 'g.pdf'}", str(tmp_path / "f.pdf")], check=True)
    assert "ığdır ÖĞÜŞİ" in pymupdf.open(tmp_path / "g.pdf")[0].get_text()


# ---------------------------------------------------------------- printing

def _numbered(path, n, w=595, h=842, rot=0):
    d = pymupdf.open()
    for i in range(n):
        p = d.new_page(width=w, height=h)
        _text(p, (w / 2 - 20, h / 2), f"S{i + 1}", size=40)
        if rot:
            p.set_rotation(rot)
    d.save(path)
    return path


@pytest.mark.parametrize("per,sheets", [(2, 4), (4, 2), (6, 2)])
def test_n_up_portrait(tmp_path, per, sheets):
    src = _numbered(tmp_path / "not.pdf", 7)
    out = docops.n_up([src], noop, per=per)[0]
    d = pymupdf.open(out)
    assert len(d) == sheets
    words = [w[4] for w in d[0].get_text("words")]
    assert words[:per] == [f"S{i + 1}" for i in range(per)]          # reading order: left to right, top to bottom
    assert all(pg.rect.width in (pytest.approx(595.3, abs=1), pytest.approx(841.9, abs=1)) for pg in d)


def test_n_up_slides_layout(tmp_path):
    """Landscape slides 2-up: stacked on portrait A4; 4-up: 2×2."""
    src = _numbered(tmp_path / "slayt.pdf", 4, w=720, h=540)
    two = pymupdf.open(docops.n_up([src], noop, per=2)[0])
    assert two[0].rect.height > two[0].rect.width
    ys = sorted(w[1] for w in two[0].get_text("words"))
    assert ys[1] - ys[0] > 200                                          # stacked
    four = pymupdf.open(docops.n_up([src], noop, per=4)[0])
    assert len(four) == 1 and len(four[0].get_text("words")) == 4


def test_booklet_order():
    assert docops.booklet_order(8) == [(7, 0), (1, 6), (5, 2), (3, 4)]
    assert docops.booklet_order(5) == [(None, 0), (1, None), (None, 2), (3, 4)]


def test_booklet(tmp_path):
    out = docops.booklet([_numbered(tmp_path / "k.pdf", 6)], noop)[0]
    d = pymupdf.open(out)
    assert len(d) == 4 and d[0].rect.width > d[0].rect.height
    assert [w[4] for w in d[0].get_text("words")] == ["S1"]             # front cover: back (blank) | P1
    assert [w[4] for w in d[1].get_text("words")] == ["S2"]


def test_grayscale(tmp_path, pdf):
    d = pymupdf.open()
    p = d.new_page()
    p.draw_rect(pymupdf.Rect(50, 50, 300, 300), fill=(1, 0, 0))
    _text(p, (60, 400), "Kırmızı başlık çalışma", size=14)
    d.save(tmp_path / "renk.pdf")
    out = docops.grayscale([tmp_path / "renk.pdf"], noop)[0]
    g = pymupdf.open(out)
    px = g[0].get_pixmap(dpi=30)
    r, gg, b = px.pixel(int(150 * 30 / 72), int(150 * 30 / 72))
    assert r == gg == b and r < 200                                     # red → grey
    assert "Kırmızı başlık" in g[0].get_text()                          # the text stayed selectable


def test_n_up_and_booklet_keep_rotation(tmp_path):
    """A rotated page (appearing landscape) is placed on the paper as it appears."""
    src = _numbered(tmp_path / "r.pdf", 4, rot=90)
    for out in (docops.n_up([src], noop, per=2)[0], docops.booklet([src], noop)[0]):
        d = pymupdf.open(out)
        dirs = {ln["dir"] for b in d[0].get_text("dict")["blocks"] for ln in b.get("lines", [])}
        assert dirs == {(0.0, 1.0)}                     # direction as seen in the source: downwards
