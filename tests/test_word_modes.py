"""PDF → Word: two-column detection (automatic exact layout), flowing text mode, horizontal separator lines."""

import re
import zipfile

import pymupdf

from conftest import _text, noop
from riffle import reflow
from riffle.actions import ACTIONS, pdf_to_word

LOREM = ("Electrical discharge machining removes material by repeated sparks between the tool "
         "electrode and the workpiece immersed in a dielectric fluid during the process.").split()


def _fill(page, x0, x1, y0, y1, size=9.5):
    words, i, y = LOREM * 40, 0, y0
    while y < y1:
        line, w = [], 0
        while i < len(words) and w < (x1 - x0) - 40:
            line.append(words[i])
            w += 5.2 * (len(words[i]) + 1)
            i += 1
        _text(page, (x0, y), " ".join(line), size=size)
        y += size * 1.25


def two_column_pdf(path, pages=2):
    d = pymupdf.open()
    for _ in range(pages):
        p = d.new_page()
        _text(p, (60, 60), "A review of dielectric fluids in sinker EDM", size=14)
        _fill(p, 42, 290, 100, 780)
        _fill(p, 305, 553, 100, 780)
    d.save(path)
    return path


def one_column_pdf(path):
    d = pymupdf.open()
    p = d.new_page()
    _text(p, (72, 70), "Rapor başlığı", size=16)
    _fill(p, 72, 523, 100, 700)
    d.save(path)
    return path


def test_two_column_detection(tmp_path):
    assert reflow.is_two_column(pymupdf.open(two_column_pdf(tmp_path / "m.pdf")))
    assert not reflow.is_two_column(pymupdf.open(one_column_pdf(tmp_path / "r.pdf")))


def test_two_column_table_is_not_article(tmp_path):
    """A two-column table (label | value) must not count as a paper."""
    d = pymupdf.open()
    p = d.new_page()
    y = 80
    for i in range(22):
        p.draw_rect(pymupdf.Rect(60, y, 300, y + 24), color=(0, 0, 0))
        p.draw_rect(pymupdf.Rect(300, y, 540, y + 24), color=(0, 0, 0))
        _text(p, (66, y + 16), f"Alan {i} için açıklama metni burada", size=9)
        _text(p, (306, y + 16), f"Değer {i} ve uzun bir açıklama satırı", size=9)
        y += 24
    d.save(tmp_path / "t.pdf")
    assert not reflow.is_two_column(pymupdf.open(tmp_path / "t.pdf"))


def _xml(path):
    return zipfile.ZipFile(path).read("word/document.xml").decode("utf-8")


def test_auto_uses_exact_layout_for_two_columns(tmp_path):
    src = two_column_pdf(tmp_path / "makale.pdf")
    msgs = []
    auto = pdf_to_word([src], msgs.append)[0]
    assert "txbxContent" in _xml(auto)                       # exact layout: text boxes
    assert any("iki sütunlu" in m and "akan metin" in m for m in msgs)
    flow = pdf_to_word([src], noop, mode="flow")[0]
    assert "txbxContent" not in _xml(flow)                   # flowing text
    one = pdf_to_word([one_column_pdf(tmp_path / "rapor.pdf")], noop)[0]
    assert "txbxContent" not in _xml(one)


def test_flow_action_registered():
    a = next(a for a in ACTIONS if a.id == "pdf2wordf")
    assert a.func is pdf_to_word and a.kwargs == {"mode": "flow"}


def _rules_pdf(path):
    d = pymupdf.open()
    p = d.new_page()
    _text(p, (180, 100), "Masaüstü Dalma Erozyon Tezgâhı", size=16)
    _text(p, (220, 125), "Ekip için kolay anlatım", size=11)
    p.draw_line((72, 136), (523, 136), color=(0.18, 0.36, 0.54), width=1)          # heading underline
    _text(p, (72, 170), "Bu belge sistemin ne yaptığını ve neden böyle tasarlandığını anlatır.", size=10)
    _text(p, (72, 260), "Altı çizili kısa başlık", size=10)
    p.draw_line((72, 262), (180, 262), color=(0, 0, 0), width=0.5)                 # word underline
    p.draw_line((72, 300), (523, 300), color=(1, 1, 1), width=1)                   # white: invisible
    _text(p, (72, 296), "Beyaz çizginin üstündeki satır burada duruyor.", size=10)
    # table: its borders must stay a table, not paragraph borders
    for i in range(4):
        p.draw_rect(pymupdf.Rect(72, 400 + i * 20, 300, 420 + i * 20), color=(0, 0, 0))
        p.draw_rect(pymupdf.Rect(300, 400 + i * 20, 523, 420 + i * 20), color=(0, 0, 0))
        _text(p, (78, 414 + i * 20), f"Hücre {i}", size=9)
        _text(p, (306, 414 + i * 20), f"Değer {i}", size=9)
    d.save(path)
    return path


def test_find_rules(tmp_path):
    d = pymupdf.open(_rules_pdf(tmp_path / "c.pdf"))
    rules = reflow.find_rules(d[0])
    assert [(r.text, r.side, r.color) for r in rules] == [("ekipiçinkolayanlatım", "bottom", "2E5C8A")]


def test_rule_becomes_paragraph_border(tmp_path):
    out = pdf_to_word([_rules_pdf(tmp_path / "c.pdf")], noop, mode="flow")[0]
    xml = _xml(out)
    borders = re.findall(r"<w:pBdr>(.*?)</w:pBdr>", xml)
    assert len(borders) == 1 and 'w:color="2E5C8A"' in borders[0] and "<w:bottom " in borders[0]
    para = re.search(r"<w:p(?: [^>]*)?>(?:(?!</w:p>).)*<w:pBdr>.*?</w:p>", xml, re.S).group(0)
    assert "Ekip için kolay anlatım" in re.sub(r"<[^>]+>", "", para)
    # schema order: pBdr before spacing/ind/jc
    ppr = re.search(r"<w:pPr>(.*?)</w:pPr>", para, re.S).group(1)
    assert ppr.index("<w:pBdr>") < min([ppr.index(t) for t in ("<w:spacing", "<w:ind", "<w:jc") if t in ppr]
                                       or [len(ppr)])


def test_word_mixed_table_and_rules_untouched_in_cells(tmp_path):
    out = pdf_to_word([_rules_pdf(tmp_path / "c.pdf")], noop, mode="flow")[0]
    xml = _xml(out)
    for tbl in re.findall(r"<w:tbl>.*?</w:tbl>", xml, re.S):
        assert "<w:pBdr>" not in tbl
