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
