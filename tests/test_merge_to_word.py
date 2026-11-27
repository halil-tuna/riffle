"""Merging mixed files (PDF, Word, PowerPoint, Excel, images) into ONE Word document (officetools.merge_to_word),
the "Result: PDF | Word" choice in the order window and its connection to main / history."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pymupdf
import pytest

from conftest import TR_TEXT, make_text_pdf, needs_soffice, noop

from riffle import officetools, settings
from riffle.actions import ConversionError


def _slides(path: Path, n: int = 2) -> Path:
    d = pymupdf.open()
    for i in range(n):
        p = d.new_page(width=960, height=540)
        p.insert_text((60, 90), f"Slayt {i + 1}", fontsize=40)
        p.insert_text((60, 170), "Birinci madde", fontsize=26)
    d.save(path)
    return path


def _docx(path: Path, title: str = "Word bölümü") -> Path:
    import docx
    w = docx.Document()
    w.add_heading(title, 1)
    w.add_paragraph("Bu paragraf Word belgesinden geliyor. " * 10)
    w.save(path)
    return path


def _sections(path: Path) -> list[tuple[float, float]]:
    import docx
    return [(round(s.page_width.cm, 1), round(s.page_height.cm, 1)) for s in docx.Document(str(path)).sections]


def _texts(path: Path) -> list[str]:
    """All text in document order (exact-layout pages keep their text in text boxes)."""
    import docx
    d = docx.Document(str(path))
    return [el.text for el in d.element.body.iter() if el.tag.endswith("}t") and el.text and el.text.strip()]


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    monkeypatch.setenv("APPDATA", str(d))
    return d


def test_pdf_word_image_into_one_docx(work):
    from PIL import Image
    a = make_text_pdf(work / "1 rapor.pdf", pages=2)
    s = _slides(work / "2 sunum.pdf")
    w = _docx(work / "3 notlar.docx")
    Image.new("RGB", (1600, 900), (30, 120, 200)).save(work / "4 foto.jpg")
    before = sorted(p.name for p in work.iterdir())
    msgs = []
    out = officetools.merge_to_word([a, s, w, work / "4 foto.jpg"], msgs.append, ordered=True)[0]
    assert out.name == "1 rapor_birlesik.docx" and out.parent == work
    assert sorted(p.name for p in work.iterdir()) == sorted(before + [out.name])    # nothing else left behind
    secs = _sections(out)
    assert secs[0] == (21.0, 29.7)                                    # the A4 report first …
    assert any(w_ > h for w_, h in secs[1:-1])                        # … the slides stay landscape …
    assert secs[-1] == (29.7, 21.0)                                   # … and the landscape photo ends it
    text = " ".join(_texts(out))
    assert text.index("Başlık 1") < text.index("Slayt 1") < text.index("Word bölümü")
    assert TR_TEXT.split()[0] in text
    import docx
    assert len(docx.Document(str(out)).inline_shapes) >= 1             # the photo
    assert any("birebir düzen" in m and "2 sunum.pdf" in m for m in msgs)    # exact layout; the real file name
    assert not any("OCR" in m or "metin tanıma" in m.lower() for m in msgs)     # never OCR here
    assert any(m.startswith("ℹ 4 dosya tek Word belgesinde") for m in msgs)


def test_pdfs_never_go_through_ocr(work, scanned, monkeypatch):
    """Merging as Word uses the exact layout for PDFs: a scanned page stays a picture, OCR is never started
    (the automatic PDF → Word would OCR this scan: see the control below)."""
    from riffle import actions, ocr
    calls = []

    def spy(*a, **k):
        calls.append(1)
        raise AssertionError("OCR started")
    monkeypatch.setattr(ocr, "ocr_pages", spy)
    out = officetools.merge_to_word([scanned, _docx(work / "b.docx")], noop, ordered=True)[0]
    assert calls == [] and out.exists()
    import docx
    assert len(docx.Document(str(out)).inline_shapes) + len(
        [e for e in docx.Document(str(out)).element.body.iter() if e.tag.endswith("}anchor")]) >= 1  # the scan
    with pytest.raises(Exception):                     # control: the automatic path does go to OCR here
        actions.pdf_to_word([scanned], noop)
    assert calls == [1]


def test_order_is_kept(work):
    a = _docx(work / "a.docx", "Birinci belge")
    b = _docx(work / "b.docx", "İkinci belge")
    out = officetools.merge_to_word([b, a], noop, ordered=True)[0]
    t = " ".join(_texts(out))
    assert t.index("İkinci belge") < t.index("Birinci belge") and out.name == "b_birlesik.docx"
    out = officetools.merge_to_word([b, a], noop)[0]                   # not ordered: by name
    t = " ".join(_texts(out))
    assert t.index("Birinci belge") < t.index("İkinci belge")


def test_bad_file_is_skipped(work, garbage):
    a = _docx(work / "a.docx")
    msgs = []
    out = officetools.merge_to_word([a, garbage, _docx(work / "c.docx", "Son")], msgs.append, ordered=True)[0]
    assert out.exists() and any("atlandı" in m and "sahte.pdf" in m for m in msgs)
    with pytest.raises(ConversionError):
        officetools.merge_to_word([garbage, work / "yok.txt.bin"], noop, ordered=True)
    with pytest.raises(ConversionError):
        officetools.merge_to_word([a], noop)


def test_no_blank_page_after_a_trailing_page_break(work):
    import docx
    a = docx.Document()
    a.add_paragraph("Birinci sayfa")
    a.add_page_break()                                      # ends with a page break (like exact-layout pages)
    a.save(work / "a.docx")
    out = officetools.merge_to_word([work / "a.docx", _docx(work / "b.docx")], noop, ordered=True)[0]
    body = docx.Document(str(out)).element.body
    brs = [br for br in body.iter() if br.tag.endswith("}br") and br.get(
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}type") == "page"]
    assert not brs                                          # the break became the section break, not an extra page


@needs_soffice
def test_full_mix_renders_page_by_page(work):
    """PowerPoint and Excel go through LibreOffice; the merged document, rendered again, has every part on its
    own pages with its own size and no empty pages in between."""
    import openpyxl
    from PIL import Image
    from pptx import Presentation
    a = make_text_pdf(work / "1 rapor.pdf", pages=2)
    _slides(work / "2 sunum.pdf")
    _docx(work / "3 notlar.docx")
    pr = Presentation()
    sl = pr.slides.add_slide(pr.slide_layouts[1])
    sl.shapes.title.text = "PPTX slaytı"
    pr.save(work / "4 ders.pptx")
    wb = openpyxl.Workbook()
    for r in range(1, 12):
        wb.active.append([f"Satır {r}", r, r * 2.5])
    wb.save(work / "5 tablo.xlsx")
    Image.new("RGB", (1600, 900), (30, 120, 200)).save(work / "6 foto.jpg")
    files = sorted(p for p in work.iterdir())
    out = officetools.merge_to_word(files, noop, ordered=True)[0]
    outdir = work.parent / "render"
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(out)],
                   capture_output=True, timeout=240)
    pdf = pymupdf.open(outdir / f"{out.stem}.pdf")
    sizes = [(round(p.rect.width), round(p.rect.height)) for p in pdf]
    texts = [p.get_text() for p in pdf]
    assert len(pdf) == 8, sizes                              # 2 + 2 + 1 + 1 + 1 + 1: no empty pages
    assert sizes[0] == (595, 842) and sizes[2] == (960, 540) and sizes[3] == (960, 540)
    assert sizes[5][0] > sizes[5][1] and sizes[7] == (842, 595)
    assert "PPTX slaytı" in texts[5] and "Satır 1" in texts[6]
    assert all(t.strip() for t in texts[:7])                 # (the photo page has no text)
    assert sorted(p.name for p in work.iterdir()) == sorted([f.name for f in files] + [out.name])


def _indented_docx(path: Path) -> Path:
    """A homework-like Word file: Normal = justified with a 1.25 cm first-line indent."""
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Cm
    w = docx.Document()
    pf = w.styles["Normal"].paragraph_format
    pf.alignment, pf.first_line_indent = WD_ALIGN_PARAGRAPH.JUSTIFY, Cm(1.25)
    w.add_paragraph("Ödev metni, iki yana yaslı ve ilk satırı girintili. " * 12)
    w.save(path)
    return path


def test_pdf_part_ignores_the_first_documents_normal_style(work):
    """Halil: merging a homework .docx with PDFs, the PDF part of a worked solution came out broken. The exact
    layout's paragraphs had no indent/alignment of their own, so they took the FIRST document's Normal style
    (1.25 cm first-line indent): every line moved right and small boxes wrapped. Now they state both."""
    import zipfile
    import docx
    first = _indented_docx(work / "1 odev.docx")
    assert docx.Document(str(first)).styles["Normal"].paragraph_format.first_line_indent      # control: it IS there
    make_text_pdf(work / "2 cozum.pdf", pages=1, table=False)
    out = officetools.merge_to_word([first, work / "2 cozum.pdf"], noop, ordered=True)[0]
    xml = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    import re
    paras = [p for box in re.findall(r"<w:txbxContent>(.*?)</w:txbxContent>", xml, re.S)
             for p in re.findall(r"<w:p>.*?</w:p>", box, re.S)]
    assert paras
    assert all('w:firstLine="0"' in p and '<w:jc w:val="left"/>' in p for p in paras)


@needs_soffice
def test_pdf_part_keeps_its_place_after_an_indented_document(work):
    """Rendered: the PDF's first line starts where it starts on its own (before the fix: ~35 pt to the right)."""
    first = _indented_docx(work / "1 odev.docx")
    pdf = make_text_pdf(work / "2 cozum.pdf", pages=1, table=False)
    word = pymupdf.open(pdf)[0].get_text("words")
    x0 = min(w[0] for w in word if w[4].startswith("Paragraf"))
    out = officetools.merge_to_word([first, pdf], noop, ordered=True)[0]
    outdir = work.parent / "render"
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(out)],
                   capture_output=True, timeout=240)
    pages = list(pymupdf.open(outdir / f"{out.stem}.pdf"))
    words = [w for p in pages if "Paragraf" in p.get_text() for w in p.get_text("words")]
    assert abs(min(w[0] for w in words if w[4].startswith("Paragraf")) - x0) < 3


def _styled_docx(path: Path, font: str, size: float, text: str, header: str | None = None,
                 logo: Path | None = None, indent: bool = False) -> Path:
    """Like Word's own PDF import: a 'Body Text' style whose font/size is this document's, text in that style,
    a plain paragraph in Normal, optionally a header (text + picture)."""
    import docx
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Cm, Pt
    w = docx.Document()
    try:
        st = w.styles["Body Text"]
    except KeyError:
        st = w.styles.add_style("Body Text", WD_STYLE_TYPE.PARAGRAPH)
    st.font.name, st.font.size = font, Pt(size)
    if indent:
        w.styles["Normal"].paragraph_format.first_line_indent = Cm(1.25)
    w.add_paragraph(text, style="Body Text")
    w.add_paragraph(f"{text} (Normal)")
    if header:
        h = w.sections[0].header
        h.paragraphs[0].text = header
        if logo:
            h.add_paragraph().add_run().add_picture(str(logo), width=Cm(2))
    w.save(path)
    return path


def _style_of(out: Path, text: str):
    """The style element of the paragraph holding text in the merged file."""
    import docx
    from docx.oxml.ns import qn
    d = docx.Document(str(out))
    p = next(p for p in d.paragraphs if p.text == text)
    ps = p._p.pPr.find(qn("w:pStyle")) if p._p.pPr is not None else None
    if ps is None:                                            # no pStyle: the document's default paragraph style
        return next(s for s in d.styles.element.findall(qn("w:style"))
                    if s.get(qn("w:type")) == "paragraph" and s.get(qn("w:default")) in ("1", "true"))
    return next(s for s in d.styles.element.findall(qn("w:style")) if s.get(qn("w:styleId")) == ps.get(qn("w:val")))


def test_same_named_styles_keep_each_documents_look(work):
    """Halil's midterm: two Word files from Word's PDF import both have 'Body Text' — 19.5 pt Arial in the first,
    Times in the second. The second came out in big Arial (docxcompose keeps the first definition by name).
    Now each part's styles are its own, and its plain paragraphs don't take the first file's Normal either."""
    from docx.oxml.ns import qn
    a = _styled_docx(work / "1 bolum.docx", "Arial", 19.5, "Birinci belge", indent=True)
    b = _styled_docx(work / "2 cozum.docx", "Times New Roman", 11, "İkinci belge")
    out = officetools.merge_to_word([a, b], noop, ordered=True)[0]
    first, second = _style_of(out, "Birinci belge"), _style_of(out, "İkinci belge")
    font = lambda s: s.find(f"{qn('w:rPr')}/{qn('w:rFonts')}").get(qn("w:ascii"))           # noqa: E731
    size = lambda s: s.find(f"{qn('w:rPr')}/{qn('w:sz')}").get(qn("w:val"))                  # noqa: E731
    assert (font(first), size(first)) == ("Arial", "39")             # control: the first file is untouched
    assert (font(second), size(second)) == ("Times New Roman", "22")
    # the second file's plain paragraph: its own Normal, not the first file's 1.25 cm first-line indent
    plain = _style_of(out, "İkinci belge (Normal)")
    assert plain.get(qn("w:styleId")) != _style_of(out, "Birinci belge (Normal)").get(qn("w:styleId"))
    assert all(el.get(qn("w:firstLine")) in (None, "0") for el in plain.iter(qn("w:ind")))
    import docx
    names = [s.name for s in docx.Document(str(out)).styles]
    assert len(names) == len(set(names))                             # Word wants unique style names


def test_each_document_keeps_its_own_header(work):
    """The midterm's university logo was in its header; merged after a file without a header it was gone (and a
    header of the first file would have run on over every other file). Now: each its own; none → none."""
    from PIL import Image
    from docx.oxml.ns import qn
    import docx
    logo = work.parent / "logo.png"
    Image.new("RGB", (120, 120), (200, 30, 30)).save(logo)
    a = _styled_docx(work / "1 a.docx", "Arial", 12, "Başlıksız belge")
    b = _styled_docx(work / "2 b.docx", "Times New Roman", 11, "Logolu belge", header="SINAV BAŞLIĞI", logo=logo)
    c = _styled_docx(work / "3 c.docx", "Arial", 11, "Yine başlıksız")
    out = officetools.merge_to_word([a, b, c], noop, ordered=True)[0]
    d = docx.Document(str(out))
    headers = [(s.header.is_linked_to_previous, " ".join(p.text for p in s.header.paragraphs).strip(),
                len(s.header._element.findall(f".//{qn('a:blip')}"))) for s in d.sections]
    assert len(headers) == 3, headers
    assert headers[1] == (False, "SINAV BAŞLIĞI", 1)
    assert headers[2][0] is False and headers[2][1] == ""             # c doesn't inherit b's header
    blip = d.sections[1].header._element.find(f".//{qn('a:blip')}")
    assert d.sections[1].header.part.related_parts[blip.get(qn("r:embed"))].blob[:4] == b"\x89PNG"


@needs_soffice
def test_header_shows_only_on_its_own_pages(work):
    a = _styled_docx(work / "1 a.docx", "Arial", 12, "Başlıksız belge")
    b = _styled_docx(work / "2 b.docx", "Times New Roman", 11, "Logolu belge", header="SINAV BAŞLIĞI")
    c = _styled_docx(work / "3 c.docx", "Arial", 11, "Yine başlıksız")
    out = officetools.merge_to_word([a, b, c], noop, ordered=True)[0]
    outdir = work.parent / "render"
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(out)],
                   capture_output=True, timeout=240)
    texts = [p.get_text() for p in pymupdf.open(outdir / f"{out.stem}.pdf")]
    assert len(texts) == 3
    assert ["SINAV BAŞLIĞI" in t for t in texts] == [False, True, False]


def _two_section_docx(path: Path, text: str) -> Path:
    """Like Word's PDF import on a page with a multi-column figure: a 2-column section, then a CONTINUOUS
    section with the text below it — all on one page."""
    import docx
    from docx.enum.section import WD_SECTION
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls
    w = docx.Document()
    w.add_paragraph(f"{text}: şekil sütunu")
    w.sections[0]._sectPr.append(parse_xml(f'<w:cols {nsdecls("w")} w:num="2"/>'))
    w.add_section(WD_SECTION.CONTINUOUS)
    w.add_paragraph(f"{text}: şeklin altındaki soru (b)")
    w.save(path)
    return path


def test_a_files_continuous_section_stays_continuous(work):
    """Halil's midterm, last page: the figure in a 4-column section, question (b) below it in a 'continuous'
    section. Merging removed the last section's type (= next page) and (b) went to a page of its own. Only a
    file's FIRST section must start a new page; the others keep their type."""
    import docx
    from docx.enum.section import WD_SECTION
    a = _docx(work / "1 a.docx", "Önceki dosya")
    b = _two_section_docx(work / "2 b.docx", "Sınav")
    c = _docx(work / "3 c.docx", "Sonraki dosya")
    out = officetools.merge_to_word([a, b, c], noop, ordered=True)[0]
    types = [s.start_type for s in docx.Document(str(out)).sections]
    assert types[1:] == [WD_SECTION.NEW_PAGE, WD_SECTION.CONTINUOUS, WD_SECTION.NEW_PAGE], types


@needs_soffice
def test_continuous_section_renders_on_the_same_page(work):
    a = _docx(work / "1 a.docx", "Önceki dosya")
    b = _two_section_docx(work / "2 b.docx", "Sınav")
    c = _docx(work / "3 c.docx", "Sonraki dosya")
    out = officetools.merge_to_word([a, b, c], noop, ordered=True)[0]
    outdir = work.parent / "render"
    subprocess.run(["soffice", "--headless", "--convert-to", "pdf", "--outdir", str(outdir), str(out)],
                   capture_output=True, timeout=240)
    texts = [p.get_text() for p in pymupdf.open(outdir / f"{out.stem}.pdf")]
    assert len(texts) == 3, texts                                    # a | b (both sections) | c
    assert "şekil sütunu" in texts[1] and "soru (b)" in texts[1] and "Sonraki" in texts[2]


# ---------------------------------------------------------------- order window + main + history

def test_main_word_choice(cfg, work, monkeypatch):
    from riffle import history, ui
    from riffle import main as main_mod
    a = make_text_pdf(work / "a.pdf", pages=1, table=False)
    b = _docx(work / "b.docx")
    seen, asked = {}, []

    def window(title, job, cancel=None):
        seen["result"] = job(lambda m: None)
    monkeypatch.setattr(ui, "run_with_window", window)
    monkeypatch.setattr(ui, "ask_order", lambda paths, *a_, **k: (asked.append(k), ("word", list(paths)))[1])
    assert main_mod.main(["organize", str(a), str(b)]) == 0
    outs, errs = seen["result"]
    assert not errs and [o.suffix for o in outs] == [".docx"] and outs[0].name == "a_birlesik.docx"
    assert asked[0]["formats"] is True
    job = history.query()[0]
    assert job["action_id"] == "organize" and job["options"] == {"ordered": True, "format": "word"}
    main_mod.main(["--again", str(job["id"])])
    assert asked[-1]["fmt"] == "word"                     # "Convert again" opens with Word chosen


def test_pdf_only_merge_offers_word_too(cfg, work, monkeypatch):
    from riffle import ui
    from riffle import main as main_mod
    files = [make_text_pdf(work / f"p{i}.pdf", pages=1, table=False) for i in range(2)]
    asked = []
    monkeypatch.setattr(ui, "ask_order", lambda paths, *a_, **k: (asked.append(k), None)[1])
    main_mod.main(["merge", *map(str, files)])
    main_mod.main(["wordmerge", str(_docx(work / "x.docx")), str(_docx(work / "y.docx"))])
    assert asked[0]["formats"] is True and "formats" not in asked[1]     # Word-only merging needs no choice


def test_env_preset_word(work, monkeypatch):
    from riffle import ui
    monkeypatch.setenv("RIFFLE_ORDER", "word")
    assert ui.ask_order([work], "t", "h", formats=True)[0] == "word"
    assert ui.ask_order([work], "t", "h")[0] != "word"


try:
    import tkinter as tk
except ImportError:
    tk = None
needs_display = pytest.mark.skipif(tk is None or not os.environ.get("DISPLAY"), reason="tkinter / sanal ekran yok")


def _order_window(monkeypatch, act):
    from riffle import ui
    real = tk.Tk

    class Probe(real):
        def mainloop(self, n=0):
            self.after(300, lambda: act(self))
            super().mainloop(n)
    monkeypatch.setattr(tk, "Tk", Probe)
    monkeypatch.delenv("RIFFLE_ORDER", raising=False)
    monkeypatch.setattr(ui, "_gui", lambda: True)


def _all(root):
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        out.append(w)
    return out


@needs_display
def test_order_window_word_choice_is_remembered(cfg, work, monkeypatch):
    from tkinter import ttk
    from riffle import ui
    files = [_docx(work / "a.docx"), make_text_pdf(work / "b.pdf", pages=1, table=False)]
    state = {}

    def pick_word(root):
        w = _all(root)
        word = next(x for x in w if isinstance(x, ttk.Radiobutton) and x.cget("text") == "Word")
        pages = next(x for x in w if isinstance(x, ttk.Button) and str(x.cget("text")).startswith("Sayfa sayfa"))
        word.invoke()
        state["pages_disabled"] = "disabled" in pages.state()
        next(x for x in w if isinstance(x, ttk.Button) and x.cget("text") == "Birleştir").invoke()
    _order_window(monkeypatch, pick_word)
    got = ui.ask_order(files, "Birleştir", "Sıra", "Birleştir", formats=True)
    assert got[0] == "word" and got[1] == files and state["pages_disabled"]
    assert settings.load()["merge_format"] == "word"

    def check(root):
        word = next(x for x in _all(root) if isinstance(x, ttk.Radiobutton) and x.cget("text") == "Word")
        state["preselected"] = word.instate(["selected"])
        next(x for x in _all(root) if isinstance(x, ttk.Button) and x.cget("text") == "İptal").invoke()
    _order_window(monkeypatch, check)
    assert ui.ask_order(files, "Birleştir", "Sıra", "Birleştir", formats=True) is None
    assert state["preselected"]


@needs_display
def test_order_window_without_choice(cfg, work, monkeypatch):
    from tkinter import ttk
    from riffle import ui
    files = [_docx(work / "a.docx"), _docx(work / "b.docx")]
    seen = {}

    def look(root):
        seen["radios"] = [x for x in _all(root) if isinstance(x, ttk.Radiobutton)]
        next(x for x in _all(root) if isinstance(x, ttk.Button) and x.cget("text") == "Birleştir").invoke()
    _order_window(monkeypatch, look)
    assert ui.ask_order(files, "t", "h", "Birleştir", allow_pages=False)[0] == "ok"
    assert seen["radios"] == []
