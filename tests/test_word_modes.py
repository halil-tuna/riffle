"""PDF → Word: two-column detection (automatic exact layout), flowing text mode, horizontal separator lines."""

import re
import shutil
import zipfile

import pymupdf

from conftest import _text, needs_soffice, noop
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


def _drawing_page(doc):
    p = doc.new_page()
    for i in range(15):
        p.draw_line((60 + i * 25, 120), (320 + i * 12, 640), color=(0, 0, 1), width=2)
    p.draw_circle((300, 360), 90, color=(1, 0, 0), width=3)


def test_textless_pages_pinned_to_page_corner(tmp_path):
    """Pages without text become a page image; it must sit at the page corner (LibreOffice shifted
    an inline full-page image ~9 pt right) and every PDF page must stay one Word page."""
    src = tmp_path / "karisik.pdf"
    d = pymupdf.open()
    t = d.new_page()
    _text(t, (72, 100), "Page one title", size=14)
    _drawing_page(d)
    _drawing_page(d)
    t = d.new_page()
    _text(t, (72, 100), "Page four title", size=14)
    d.save(src)
    out = pdf_to_word([src], noop)[0]
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    assert "<wp:inline" not in xml or xml.count("<wp:anchor") >= 2
    anchors = re.findall(r'<wp:anchor[^>]*behindDoc="1".*?</wp:anchor>', xml, re.S)
    assert len(anchors) == 2
    for a in anchors:
        assert re.search(r'relativeFrom="page"><wp:posOffset>0<', a)
        assert 'xmlns:a=' in a or 'xmlns:a=' in xml[:2000]
    from lxml import etree
    etree.fromstring(xml.encode())  # still well-formed (namespaces carried over)
    body = xml[xml.find("<w:body>"):]
    assert body.find("Page one title") < body.find("<wp:anchor") < body.find("Page four title")


@needs_soffice
def test_textless_pages_render_in_place(tmp_path):
    """Rendered: 4 PDF pages → 4 pages, page images at (0, 0), text pages keep their text."""
    import subprocess
    src = tmp_path / "karisik.pdf"
    d = pymupdf.open()
    t = d.new_page()
    _text(t, (72, 100), "Page one title", size=14)
    _drawing_page(d)
    _drawing_page(d)
    t = d.new_page()
    _text(t, (72, 100), "Page four title", size=14)
    d.save(src)
    out = pdf_to_word([src], noop)[0]
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path / "r"), str(out)],
                   check=True, capture_output=True, timeout=180)
    with pymupdf.open(tmp_path / "r" / (out.stem + ".pdf")) as r:
        assert len(r) == 4
        assert "Page one title" in r[0].get_text() and "Page four title" in r[3].get_text()
        for i in (1, 2):
            boxes = [info["bbox"] for info in r[i].get_image_info()]
            assert len(boxes) == 1
            assert abs(boxes[0][0]) < 1 and abs(boxes[0][1]) < 1
            assert abs(boxes[0][2] - r[i].rect.width) < 1
