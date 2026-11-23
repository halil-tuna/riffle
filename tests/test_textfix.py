"""PDF → Word metin düzeltmeleri: kayıp semboller (μ), satır sonu tireleri, alt simge, bağlı harfler."""

import re
import zipfile
from collections import Counter

import pymupdf
import pytest

from conftest import _text, noop
from riffle import textfix
from riffle.actions import pdf_to_word


def _docx_text(path):
    x = zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")
    paras = re.findall(r"<w:p[ >].*?</w:p>", x, re.S)
    return "\n".join(re.sub(r"<[^>]+>", "", p) for p in paras), x


def _mu_pdf(path):
    """ToUnicode'u olmayan Elsevier MathematicalPi fontu: μ, PDF'ten \\x01 olarak okunur."""
    content = (b"BT /F1 11 Tf 72 700 Td (Pulses of 500 ) Tj /F2 11 Tf (\\001) Tj /F1 11 Tf (s were applied.) Tj "
               b"0 -14 Td (Roughness was 40 ) Tj /F2 11 Tf (\\001) Tj /F1 11 Tf (m at most.) Tj ET")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 7 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Times-Roman /Encoding /WinAnsiEncoding >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /MathematicalPi-One /FirstChar 1 /LastChar 1 "
        b"/Widths [667] /Encoding 6 0 R >>",
        b"<< /Type /Encoding /Differences [ 1 /H9262 ] >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = b"%PDF-1.4\n"
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    path.write_bytes(out)
    return path


def test_char_map_mathpi(tmp_path):
    d = pymupdf.open(_mu_pdf(tmp_path / "mu.pdf"))
    assert "\x01" in d[0].get_text()                         # hata gerçekten var
    assert textfix.build_char_map(d) == {"MathematicalPi-One": {"\x01": "μ"}}


@pytest.mark.parametrize("name,ch", [("H9251", "α"), ("H9262", "μ"), ("H9274", "ω"), ("mu", "μ"),
                                     ("uni03A9", "Ω"), ("plusminus", "±"), (".notdef", None)])
def test_glyph_names(name, ch):
    assert textfix._unicode_for(name) == ch


def test_word_keeps_mu(tmp_path):
    out = pdf_to_word([_mu_pdf(tmp_path / "mu.pdf")], noop)[0]
    text, _ = _docx_text(out)
    assert "500 μs were" in text and "40 μm at" in text
    assert "\x01" not in text


@pytest.mark.parametrize("left,right,keep", [
    ("devel", "oped", False),          # belgede 'developed' geçiyor
    ("water", "based", True),          # belgede 'water-based' geçiyor
    ("hydro", "gen", False),           # hiç geçmiyor, ek değil → birleştir
    ("self", "contained", True),       # bilinen önek
    ("oil", "based", True),            # bilinen ek
    ("Ti", "Al", True),                # büyük harfle başlıyor
])
def test_keep_hyphen(left, right, keep):
    words = Counter({"developed": 3, "water-based": 2})
    assert textfix.keep_hyphen(left, right, words) is keep


def _hyphen_pdf(path):
    d = pymupdf.open()
    p = d.new_page()
    lines = [
        "Electrical discharge machining is a highly devel-",
        "oped technology. The water-based dielectric and",
        "the second water-                                 ",
        "based fluid were compared with hydrocar-",
        "bon oil, a well developed and self-",
        "contained process for the work-",
        "piece in a tank.",
    ]
    for i, t in enumerate(lines):
        _text(p, (72, 100 + i * 14), t)
    d.save(path)
    return path


def test_word_dehyphenation(tmp_path):
    out = pdf_to_word([_hyphen_pdf(tmp_path / "h.pdf")], noop)[0]
    text, _ = _docx_text(out)
    flat = re.sub(r"\s+", " ", text)
    assert "highly developed technology" in flat
    assert "hydrocarbon oil" in flat
    assert "workpiece in" in flat
    assert "second water-based fluid" in flat           # gerçek birleşik kelime korunur
    assert "self-contained" in flat
    assert "devel-" not in flat and "devel oped" not in flat
    assert "⁠" not in text


def test_subscript_and_ligature(tmp_path):
    from riffle.ocr import _font_file
    ff = _font_file()
    d = pymupdf.open()
    p = d.new_page()
    kw = dict(fontname="dv", fontfile=ff) if ff else {}
    p.insert_text((72, 100), "Titanium oxide (TiO", fontsize=11, **kw)
    x = 72 + pymupdf.Font(fontfile=ff).text_length("Titanium oxide (TiO", 11) if ff else 170
    p.insert_text((x, 102.5), "2", fontsize=7.5, **kw)
    p.insert_text((x + 4.5, 100), ") was formed in the ﬁrst dielectric ﬂuid.", fontsize=11, **kw)
    d.save(tmp_path / "s.pdf")
    out = pdf_to_word([tmp_path / "s.pdf"], noop)[0]
    text, xml = _docx_text(out)
    assert "(TiO2)" in text.replace(" ", "") and "TiO 2" not in text
    assert re.search(r'<w:vertAlign w:val="subscript"/></w:rPr><w:t[^>]*>2<', xml)
    assert "first dielectric fluid" in text and "ﬁ" not in text


def test_patch_restored(tmp_path):
    """Dönüşüm hata verse bile pymupdf ve pdf2docx yamaları geri alınır."""
    from pdf2docx.text.TextSpan import TextSpan
    orig, orig_fmt = pymupdf.Page.get_text, TextSpan._set_text_format
    src = _mu_pdf(tmp_path / "mu.pdf")
    with pytest.raises(RuntimeError):
        with textfix.patched_text(src):
            assert pymupdf.Page.get_text is not orig
            raise RuntimeError
    assert pymupdf.Page.get_text is orig and TextSpan._set_text_format is orig_fmt


# ---------------------------------------------------------------- birebir düzen

def test_exact_layout_symbols(tmp_path):
    from riffle import fixed_layout
    out = tmp_path / "mu.docx"
    fixed_layout.convert(_mu_pdf(tmp_path / "mu.pdf"), out)
    xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    assert xml.count("μ") == 2 and "\x01" not in xml
    # μ, Word'de olmayan "Mathematical Pi" fontuyla değil, yanındaki metin fontuyla yazılır
    assert "Mathematical" not in xml


def test_exact_layout_line_pitch(tmp_path):
    """Satır kutusu satır aralığından büyükse (μ'lü satırlar) satırlar birikerek aşağı kaymamalı:
    her satırın yüksekliği PDF'teki satır aralığı olmalı."""
    from riffle import fixed_layout
    d = pymupdf.open()
    p = d.new_page()
    for i in range(6):
        _text(p, (72, 100 + i * 11.0), f"Satır {i} ÇĞÜŞİÖ çalışma güçlü yazı", )
    d.save(tmp_path / "pitch.pdf")
    lines = [ln for b in fixed_layout._lines(pymupdf.open(tmp_path / "pitch.pdf")[0]) for ln in b]
    assert lines[0][0].height > 11.0                    # kutu aralıktan büyük: hatayı tetikleyen durum
    out = tmp_path / "pitch.docx"
    fixed_layout.convert(tmp_path / "pitch.pdf", out)
    xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    heights = [int(v) for v in re.findall(r'w:line="(\d+)" w:lineRule="exact"', xml) if v != "20"]
    assert heights[:5] == [220] * 5                     # 11 pt × 20


# ---------------------------------------------------------------- OCR: el yazısı

def test_ocr_layer_skips_unreliable_handwriting():
    from riffle import ocr
    d = pymupdf.open()
    d.new_page()
    d.new_page()
    R = pymupdf.Rect(50, 50, 120, 70)
    printed = [ocr.Word(f"kelime{i}", R + (0, i * 20, 0, i * 20), 95, (0, i)) for i in range(30)]
    hand = [ocr.Word(t, R + (0, i * 20, 0, i * 20), c, (0, i))
            for i, (t, c) in enumerate([("Aldan", 40), ("Sn", 35), ("aku", 30), ("Viskozite", 88), ("€,", 60)] * 6)]
    rep = {}
    n = ocr.make_searchable(d, {0: printed, 1: hand}, report=rep)
    assert rep == {"handwritten": 1, "dropped": 24}
    assert n == 30 + 6
    assert "Aldan" not in d[1].get_text() and "Viskozite" in d[1].get_text()
