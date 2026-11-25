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


def test_headings_keep_with_next_and_body_gets_wrap_room(tmp_path, monkeypatch):
    """A larger heading is kept with the paragraph after it (it must not end up alone at the
    bottom of a page), and body paragraphs get 2 pt more room on the right so lines that filled
    the PDF's width don't wrap one word early."""
    src = tmp_path / "baslik.pdf"
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    _text(p, (72, 90), "3.5 Güvenlik zinciri ve kapı düğmeleri", 16)
    y = 120
    for k in range(6):
        _text(p, (72, y), "Acil stop, kapak anahtarı, su seviye şamandırası ve bekçi rölesi sıra halinde bağlı.")
        y += 15
    d.save(src)
    from riffle import actions

    def convert():
        out = pdf_to_word([src], noop)[0]
        xml = zipfile.ZipFile(out).read("word/document.xml").decode()
        out.unlink()
        return re.findall(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', xml, re.S)

    def right(q):
        return int(re.search(r'<w:ind\b[^>]*w:right="(-?\d+)"', q).group(1))

    paras = convert()
    head = next(q for q in paras if "Güvenlik zinciri" in q)
    body = next(q for q in paras if "Acil stop" in q)
    assert "<w:keepNext/>" in head and "<w:keepNext/>" not in body
    monkeypatch.setattr(actions, "_WRAP_SLACK", 0)
    exact = next(q for q in convert() if "Acil stop" in q)
    assert right(body) == right(exact) - 40


def _pen_pdf(path):
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    _text(p, (72, 100), "1. Bölüm başlığı", 16)
    y = 130
    for k in range(5):
        _text(p, (72, y), f"Satır {k + 1}: su seviye şamandırası ve sıcaklık anahtarı seri bağlı.")
        y += 16
    # pen underline under line 3, a tick in the margin of line 5
    a = p.add_ink_annot([[(110, 165), (200, 166), (290, 164)]])
    a.set_colors(stroke=(0, 0.3, 0.9))
    a.set_border(width=1.5)
    a.update()
    t = p.add_ink_annot([[(52, 190), (56, 196), (66, 182)]])
    t.set_border(width=1.5)
    t.update()
    d.save(path)


def test_pen_marks_come_back_as_floating_pictures(tmp_path):
    """Ink annotations are taken out before pdf2docx (it turned them into layout-breaking shapes)
    and put back as pictures that float over the text, hung on the marked paragraph."""
    src = tmp_path / "kalem.pdf"
    _pen_pdf(src)
    out = pdf_to_word([src], noop)[0]
    z = zipfile.ZipFile(out)
    xml = z.read("word/document.xml").decode()
    pens = re.findall(r'<wp:anchor[^>]*>.*?</wp:anchor>', xml, re.S)
    pens = [a for a in pens if 'name="Pen ' in a]
    assert len(pens) == 2
    for a in pens:
        assert "<wp:wrapNone/>" in a and 'positionV relativeFrom="paragraph"' in a
    assert sum(n.startswith("word/media/rIdInk") for n in z.namelist()) == 2
    assert 'Extension="png"' in z.read("[Content_Types].xml").decode()
    from lxml import etree
    etree.fromstring(xml.encode())


@needs_soffice
def test_pen_marks_render_where_they_were(tmp_path):
    import subprocess
    src = tmp_path / "kalem.pdf"
    _pen_pdf(src)
    out = pdf_to_word([src], noop)[0]
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path / "r"), str(out)],
                   check=True, capture_output=True, timeout=180)
    with pymupdf.open(src) as a, pymupdf.open(tmp_path / "r" / (out.stem + ".pdf")) as r:
        want = sorted((x.rect for x in a[0].annots()), key=lambda q: q.y0)
        got = sorted((pymupdf.Rect(i["bbox"]) for i in r[0].get_image_info()), key=lambda q: q.y0)
        assert len(got) == 2
        for w, g in zip(want, got):
            assert abs(w.x0 - g.x0) < 2 and abs(w.y0 - g.y0) < 3


def test_pdf_notes_become_word_comments(tmp_path):
    """Notes written on a PDF (a highlight's text, a sticky note, a reply) become Word comments
    on the text they were written on - also when the marked word appears earlier on the page."""
    src = tmp_path / "notlu.pdf"
    d = pymupdf.open()
    p = d.new_page(width=595, height=842)
    _text(p, (72, 100), "3.4 Koruma: bekçi ve nöbetçi", 14)
    _text(p, (72, 130), "Bekçi (kum saati): her darbede kum saati dolmaya başlar.")
    _text(p, (72, 146), "Nöbetçi: STM32 her milisaniyede buradayım der.")
    d.save(src)
    d = pymupdf.open(src)
    p = d[0]
    hit = p.search_for("Bekçi (kum")[0]
    hit = pymupdf.Rect(hit.x0, hit.y0, hit.x0 + (hit.width * 5 / 10), hit.y1)   # just 'Bekçi'
    h = p.add_highlight_annot(hit)
    h.set_info(content="Bekçi mikrodenetleyicinin içinde mi?", title="Ömer")
    h.update()
    n = p.add_text_annot((60, 150), "Bunu sonra konuşalım.")
    n.set_info(title="Yiğit")
    n.update()
    d.save(tmp_path / "notlu2.pdf")
    src = tmp_path / "notlu2.pdf"
    msgs = []
    out = pdf_to_word([src], msgs.append)[0]
    z = zipfile.ZipFile(out)
    xml = z.read("word/document.xml").decode()
    com = z.read("word/comments.xml").decode()
    from lxml import etree
    etree.fromstring(xml.encode()), etree.fromstring(com.encode())
    assert 'relationships/comments"' in z.read("word/_rels/document.xml.rels").decode()
    assert "comments+xml" in z.read("[Content_Types].xml").decode()
    assert com.count("<w:comment ") == 2
    assert 'w:author="Ömer"' in com and "mikrodenetleyicinin" in com
    assert 'w:author="Yiğit"' in com and "sonra konuşalım" in com
    ranges = dict(re.findall(r'<w:commentRangeStart w:id="(\d+)"/>(.*?)<w:commentRangeEnd w:id="\1"/>', xml, re.S))
    covered = {k: "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", v)) for k, v in ranges.items()}
    omer = re.search(r'<w:comment w:id="(\d+)" w:author="Ömer"', com).group(1)
    assert "kum saati" in covered[omer] or covered[omer].startswith("Bekçi")   # not the heading's 'bekçi'
    assert "Koruma" not in covered[omer]
    assert any("Word yorumu" in m or "Word comments" in m for m in msgs)


def _annotated_slides(path):
    d = pymupdf.open()
    for k in range(2):
        p = d.new_page(width=842, height=595)                 # landscape → slides → exact layout
        _text(p, (60, 80), f"Slayt {k + 1}: Darbe üreteci", 28)
        _text(p, (60, 140), "MOSFET sürücüsü bootstrap kondansatörüyle beslenir.", 16)
    d.save(path)
    d = pymupdf.open(path)
    p = d[1]
    h = p.add_highlight_annot(p.search_for("bootstrap")[0])
    h.set_info(content="Bootstrap nedir?", title="Ömer")
    h.update()
    a = p.add_ink_annot([[(60, 160), (200, 162), (330, 159)]])
    a.set_border(width=2)
    a.update()
    d.save(path, incremental=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)


def test_exact_layout_carries_pen_marks_and_notes(tmp_path):
    """Slides (exact layout): the pen mark is pinned on its page, the note becomes a comment on
    that page quoting the marked text - no 'not carried over' notices."""
    src = tmp_path / "slayt.pdf"
    _annotated_slides(src)
    msgs = []
    out = pdf_to_word([src], msgs.append)[0]
    assert any("birebir" in m or "exact layout" in m for m in msgs)
    assert not any("aktarılmadı" in m or "not carried" in m for m in msgs)
    z = zipfile.ZipFile(out)
    xml = z.read("word/document.xml").decode()
    com = z.read("word/comments.xml").decode()
    from lxml import etree
    etree.fromstring(xml.encode()), etree.fromstring(com.encode())
    pen = re.search(r'<wp:anchor[^>]*>(?:(?!</wp:anchor>).)*name="Pen 1".*?</wp:anchor>', xml, re.S).group(0)
    assert 'positionV relativeFrom="page"' in pen
    assert "bootstrap" in com and "Bootstrap nedir?" in com and 'w:author="Ömer"' in com
    # both on the second page: after the first page's section break
    first_page_end = xml.find("</w:sectPr>")
    assert xml.find('name="Pen 1"') > first_page_end and xml.find("<w:commentReference") > first_page_end


@needs_soffice
def test_exact_layout_pen_mark_renders_in_place(tmp_path):
    import subprocess
    src = tmp_path / "slayt.pdf"
    _annotated_slides(src)
    out = pdf_to_word([src], noop)[0]
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir", str(tmp_path / "r"), str(out)],
                   check=True, capture_output=True, timeout=180)
    with pymupdf.open(src) as a, pymupdf.open(tmp_path / "r" / (out.stem + ".pdf")) as r:
        want = next(x.rect for x in a[1].annots() if x.type[1] == "Ink")
        boxes = [pymupdf.Rect(i["bbox"]) for i in r[1].get_image_info()]
        assert any(abs(b.x0 - want.x0) < 2 and abs(b.y0 - want.y0) < 2 and b.width < 400 for b in boxes)


def test_exact_layout_turned_text_becomes_vertical_text_box(tmp_path):
    """Side labels turned by 90°/270° on slides ('Third Edition') used to stay in the background
    picture only: now they are editable vertical text boxes, in the PDF's reading order."""
    src = tmp_path / "yan.pdf"
    d = pymupdf.open()
    p = d.new_page(width=720, height=540)
    _text(p, (60, 40), "MECHANICS OF MATERIALS", 28)
    p.insert_text((20, 20), "Third Edition", fontsize=12, rotate=-90)   # reads top to bottom
    p.insert_text((700, 500), "Sayfa kenarı", fontsize=10, rotate=90)    # reads bottom to top
    _text(p, (60, 120), "Pure bending of symmetric members.", 18)
    d.save(src)
    with pymupdf.open(src) as chk:
        dirs = {round(l["dir"][1]) for b in chk[0].get_text("dict")["blocks"] for l in b.get("lines", [])}
    assert {1, -1} <= dirs
    out = pdf_to_word([src], noop)[0]
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    boxes = re.findall(r'<wps:wsp>.*?</wps:wsp>', xml, re.S)
    def box_of(text):
        return next(b for b in boxes if text in "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", b)))
    assert 'vert="vert"' in box_of("Third Edition")
    assert 'vert="vert270"' in box_of("Sayfa kenar")
    assert 'vert="horz"' in box_of("MECHANICS")


def test_rotated_pages_are_turned_upright(tmp_path):
    """A slide saved as a portrait page shown turned by 90° (/Rotate 90): the exact layout used to
    lose its pictures and place text wrongly. Pages are made upright first, look unchanged."""
    from riffle.actions import unrotate
    src = tmp_path / "donuk.pdf"
    d = pymupdf.open()
    for k in range(2):
        p = d.new_page(width=595, height=842)
        p.set_rotation(90)                                   # shown as a 842×595 landscape slide
        p.insert_text((60, 80), f"Slayt {k + 1}: Basinc ve yogunluk", fontsize=24)
        p.draw_rect(pymupdf.Rect(60, 200, 300, 400), color=(1, 0, 0), fill=(1, 0.8, 0.8))
    d.save(src)
    with pymupdf.open(src) as chk:
        before = chk[0].get_pixmap(dpi=30).samples
        assert unrotate(chk) == 2 and chk[0].rotation == 0
        assert chk[0].rect.width > chk[0].rect.height
        assert chk[0].get_pixmap(dpi=30).samples == before        # looks the same
    out = pdf_to_word([src], noop)[0]
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    w, h = re.search(r'<w:pgSz w:w="(\d+)" w:h="(\d+)"', xml).groups()
    assert int(w) > int(h)                                        # landscape page in Word
    assert "Basinc" in xml


def test_pen_heavy_pages_go_exact_and_hidden_text_is_dropped(tmp_path):
    """Pages written over with a pen (hundreds of curved strokes, like an iPad export) go to the
    exact layout; an invisible handwriting-recognition layer doesn't become visible text."""
    import math
    src = tmp_path / "ipad.pdf"
    d = pymupdf.open()
    for k in range(2):
        p = d.new_page(width=595, height=842)
        _text(p, (60, 80), "Question 1) Analysis of a buck converter.", 14)
        sh = p.new_shape()
        for i in range(320):                                 # handwriting: one curved path per stroke
            x, y = 60 + (i % 20) * 24, 140 + (i // 20) * 38
            sh.draw_bezier((x, y), (x + 6, y - 10), (x + 12, y + 10), (x + 18 + math.sin(i), y))
            sh.finish(color=(0, 0, 0.6), width=1)
        sh.commit()
        p.insert_text((80, 200), "음 ㄻ 断 D = 若", fontsize=12, render_mode=3, fontname="china-s")
    d.save(src)
    with pymupdf.open(src) as chk:
        from riffle import fixed_layout
        assert fixed_layout.looks_handwritten(chk)
    msgs = []
    out = pdf_to_word([src], msgs.append)[0]
    assert any("el yazılı" in m or "handwritten notes" in m for m in msgs)
    xml = zipfile.ZipFile(out).read("word/document.xml").decode()
    assert "buck converter" in xml and "断" not in xml and "若" not in xml
