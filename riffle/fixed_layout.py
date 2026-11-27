"""Exact layout (fixed positions) PDF → Word.

On design-heavy pages like slides, flowing text (pdf2docx) overflows and
falls apart. In this mode every PDF page becomes one Word page:

  - Background: the page WITHOUT text as an image, behind the text.
  - Text: editable text boxes at the original positions
    (font size, bold/italic and colour are kept).

The result looks very close to the original in Word; the text can be corrected but
there is no paragraph flow (edited box by box, like a presentation).
No external dependency: the DOCX is written directly as XML.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape
from .i18n import T

try:
    import pymupdf
except ImportError:  # pragma: no cover
    try:
        import fitz as pymupdf  # type: ignore
    except ImportError:          # so install.py can load it before the libraries are installed
        pymupdf = None  # type: ignore

EMU = 12700          # 1 pt = 12700 EMU
TWIP = 20            # 1 pt = 20 twips
BG_DPI = 150

_FONT_MAP = [
    (r"carlito", "Calibri"), (r"caladea", "Cambria"),
    (r"liberationsans|arimo|helvetica|arial", "Arial"),
    (r"liberationserif|tinos|times", "Times New Roman"),
    (r"liberationmono|cousine|courier", "Courier New"),
    (r"calibri", "Calibri"), (r"cambria", "Cambria"), (r"georgia", "Georgia"),
    (r"verdana", "Verdana"), (r"tahoma", "Tahoma"), (r"segoe", "Segoe UI"),
    (r"bookantiqua|book antiqua", "Book Antiqua"), (r"garamond", "Garamond"),
    (r"symbol", "Symbol"), (r"wingdings", "Wingdings"),
]


def map_font(name: str) -> str:
    base = name.split("+", 1)[-1]                       # subset prefix: ABCDEF+
    low = base.lower().replace(" ", "")
    for pat, word in _FONT_MAP:
        if re.search(pat, low):
            return word
    base = re.split(r"[-,]", base)[0]                    # -Bold, ,Italic
    base = re.sub(r"(PSMT|MT|PS)$", "", base)
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base) or "Calibri"


# ---------------------------------------------------------------- page analysis

def _text_dict(page, raw: bool = False):
    flags = (pymupdf.TEXT_PRESERVE_WHITESPACE | pymupdf.TEXT_PRESERVE_LIGATURES
             | pymupdf.TEXT_CID_FOR_UNKNOWN_UNICODE)   # keep the code: textfix resolves it from the glyph name
    blocks = page.get_text("rawdict" if raw else "dict", flags=flags)["blocks"]
    if raw:
        for b in blocks:
            for ln in b.get("lines", []):
                for sp in ln["spans"]:
                    sp["text"] = "".join(c["c"] for c in sp.get("chars", []))
    return blocks


def _space_em(font: str) -> float:
    """Width of a normal space in the Word font that stands in for this PDF font (em)."""
    f = map_font(font).lower()
    if "courier" in f or "mono" in f or "consol" in f:
        return 0.6
    if "calibri" in f or "carlito" in f:
        return 0.226
    if "cambria" in f or "times" in f or "georgia" in f or "garamond" in f or "serif" in f:
        return 0.25
    return 0.278


def _spaced(ln, spans):
    """The gaps of the PDF line, kept: a justified line's wide spaces and the tab after a bullet
    ('•' + gap + text) came out as plain spaces, so lines ended short and bullets stuck to
    their text. Returns spans split at wide gaps, each with the extra space in pt (7th item)."""
    chars = []
    for k, sp in enumerate(ln["spans"]):
        for c in sp.get("chars", []):
            chars.append((k, c))
    if len(chars) < 2 or len(spans) != len(ln["spans"]):
        return [(*s, 0.0) for s in spans]
    out = []
    pieces = {k: [] for k in range(len(spans))}      # per span: (start, end, extra after)
    for k in range(len(spans)):
        pieces[k] = []
    joins = ln.get("joins", set())
    for i, (k, c) in enumerate(chars):
        if i + 1 >= len(chars):
            break
        k2, nxt = chars[i + 1]
        size = spans[k][2]
        natural = _space_em(spans[k][1]) * size
        if c["c"] == " ":
            extra = (nxt["origin"][0] - c["origin"][0]) - natural
        elif k2 != k and nxt["c"] != " ":
            gap = nxt["bbox"][0] - c["bbox"][2]
            if k2 in joins:
                # pieces of one visual line stored apart (justified words): always a space between
                pieces[k].append(("gap", max(0.0, gap - natural) or 1e-9))
            elif gap > 1.3 * natural:
                # two spans with a gap but no space character between them (a bullet and its text)
                pieces[k].append(("gap", gap - natural))
            continue
        else:
            continue
        if extra > 0.6:
            pieces[k].append((i, extra))
    # rebuild: walk each span's characters, cutting after every widened space
    idx = 0
    for k, s in enumerate(spans):
        text, *style = s
        own = [c for kk, c in chars if kk == k]
        start = idx
        idx += len(own)
        widened = {i - start: extra for i, extra in pieces[k] if i != "gap"}
        tail_gap = sum(extra for i, extra in pieces[k] if i == "gap")
        if len(own) != len(text) or not widened:
            out.append((text, *style, 0.0))
        else:
            cut = 0
            for j in sorted(widened):
                if j > cut:
                    out.append((text[cut:j], *style, 0.0))
                out.append((" ", *style, widened[j]))
                cut = j + 1
            if cut < len(text):
                out.append((text[cut:], *style, 0.0))
        if tail_gap:
            out.append((" ", *style, tail_gap if tail_gap > 1e-6 else 0.0))
    return out


def _join_baselines(lines: list) -> list:
    """PDF writers store a justified line's words (or a label and its value) as separate lines
    on the same baseline. Join them back into one line, left to right; 'joins' marks the spans
    where a stored line began (a space goes there)."""
    out = []
    for ln in sorted(lines, key=lambda l: (round(l["bbox"][1], 0), l["bbox"][0])):
        prev = out[-1] if out else None
        if (prev and abs(prev["bbox"][1] - ln["bbox"][1]) < 1.0 and abs(prev["bbox"][3] - ln["bbox"][3]) < 1.0
                and ln["bbox"][0] >= prev["bbox"][2] - 0.5 and tuple(prev["dir"]) == tuple(ln["dir"])):
            prev.setdefault("joins", set()).add(len(prev["spans"]))
            prev["spans"] = prev["spans"] + ln["spans"]
            prev["bbox"] = (prev["bbox"][0], min(prev["bbox"][1], ln["bbox"][1]),
                            ln["bbox"][2], max(prev["bbox"][3], ln["bbox"][3]))
        else:
            out.append(dict(ln))
    return out


def _spans(ln, cmap):
    """A line's spans as (text, font, size, bold, italic, color), lost symbols repaired."""
    from .textfix import _bad, _fix_chars_raw
    spans = []
    prev_font = None
    for s in ln["spans"]:
        if s.get("alpha", 255) == 0:
            # invisible text: a handwriting-recognition layer (iPad: '음 ㄻ 断 …' over the strokes)
            # or a scan's OCR layer. As a text box it would show up; the page picture already does.
            continue
        fm = (cmap or {}).get(s["font"].split("+", 1)[-1])
        raw = s["text"]
        _fix_chars_raw(s, fm)            # lost symbols like μ, ﬁ/ﬂ → fi/fl
        if "chars" in s:                 # raw spans (spacing=True): the text comes from the fixed chars
            if not fm:                   # control characters / unknown codes, as the text path drops them
                s["chars"] = [c for c in s["chars"] if not _bad.search(c["c"])]
            s["text"] = "".join(c["c"] for c in s["chars"])
        if not s["text"]:
            continue
        font = s["font"]
        symbol = bool(fm) and raw.strip() != "" and all(ch in fm or ch.isspace() for ch in raw)
        if symbol and prev_font:         # the symbol font doesn't exist in Word: write the letter with the text font
            font = prev_font
        if not symbol:
            prev_font = font
        spans.append((s["text"], font, s["size"], bool(s["flags"] & 16) or "bold" in font.lower(),
                      bool(s["flags"] & 2) or "italic" in font.lower(), s["color"]))
    return spans


def _lines(page, cmap=None, order: list | None = None, spacing: bool = False):
    """Horizontal text lines: [(bbox, [(text, font, size, bold, italic, color)])].
    order (optional list) receives each returned block's position in the page's reading order.
    spacing=True (Word): spans carry a 7th item, extra space in pt after them (see _spaced)."""
    out_blocks = []
    for k, b in enumerate(_text_dict(page, raw=spacing)):
        if b.get("type") != 0:
            continue
        lines = []
        src_lines = b["lines"]
        if spacing:
            flat = [ln for ln in src_lines if abs(ln["dir"][1]) <= 0.01 and ln["dir"][0] > 0]
            src_lines = _join_baselines(flat)
        for ln in src_lines:
            dx, dy = ln["dir"]
            if abs(dy) > 0.01 or dx < 0:          # vertical/rotated text: see _vertical_lines
                continue
            spans = _spans(ln, cmap)
            if spacing:
                spans = _spaced(ln, spans)
            if "".join(t for t, *_ in spans).strip():
                lines.append((pymupdf.Rect(ln["bbox"]), spans))
        if lines:
            out_blocks.append(lines)
            if order is not None:
                order.append(k)
    return out_blocks


def _vertical_lines(page, cmap=None):
    """Text turned by 90° or 270° (side labels like 'Third Edition' on slides):
    [(bbox, spans, 'vert' | 'vert270', reading-order position)]. Other angles stay in the background."""
    out = []
    for k, b in enumerate(_text_dict(page)):
        if b.get("type") != 0:
            continue
        for ln in b["lines"]:
            dx, dy = ln["dir"]
            if abs(dx) > 0.01 or abs(abs(dy) - 1) > 0.01:
                continue
            spans = _spans(ln, cmap)
            if "".join(t for t, *_ in spans).strip():
                out.append((pymupdf.Rect(ln["bbox"]), spans, "vert" if dy > 0 else "vert270", k))
    return out


def _background(doc, pno: int, blocks) -> bytes:
    """Returns an image of the page with only its text removed (drawings and images stay)."""
    tmp = pymupdf.open()
    tmp.insert_pdf(doc, from_page=pno, to_page=pno)
    page = tmp[0]
    for lines in blocks:
        for rect, _ in lines:
            page.add_redact_annot(rect, fill=False)
    page.apply_redactions(images=0, graphics=0, text=0)  # remove the text only
    pix = page.get_pixmap(dpi=BG_DPI, alpha=False)
    data = pix.tobytes("jpeg", jpg_quality=85)
    tmp.close()
    return data


# ---------------------------------------------------------------- XML generation

def _run(text, font, size, bold, italic, color, extra: float = 0.0) -> str:
    """extra: points of character spacing added after each character (a widened space)."""
    f = escape(map_font(font), {'"': "&quot;"})
    rpr = (f'<w:rFonts w:ascii="{f}" w:hAnsi="{f}" w:cs="{f}" w:eastAsia="{f}"/>'
           + ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "")
           + f'<w:color w:val="{color:06X}"/>'
           + (f'<w:spacing w:val="{round(extra * 20)}"/>' if extra else "")
           + f'<w:sz w:val="{max(2, round(size * 2))}"/>'
           f'<w:szCs w:val="{max(2, round(size * 2))}"/>')
    return f'<w:r><w:rPr>{rpr}</w:rPr><w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


# Every paragraph says its indent and alignment itself: inside a merged Word document (merge as Word) a paragraph
# without them takes the FIRST document's Normal style — e.g. a homework's 1.25 cm first-line indent and justified
# text shifted every line right, and small boxes (formulas, subscripts) wrapped and piled up.
_NO_IND = '<w:ind w:left="0" w:right="0" w:firstLine="0"/>'
_LEFT = '<w:jc w:val="left"/>'

_ids = {"n": 0}


def _next_id() -> int:
    _ids["n"] += 1
    return _ids["n"]


def _anchor(x, y, w, h, graphic: str, behind: bool, name: str) -> str:
    i = _next_id()
    is_shape = "wordprocessingShape" in graphic
    # Wrap the text boxes in mc:AlternateContent, the way Word itself writes them
    pre, post = ("<mc:AlternateContent><mc:Choice Requires=\"wps\">", "</mc:Choice></mc:AlternateContent>") \
        if is_shape else ("", "")
    return (f'<w:r>{pre}<w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
            f'relativeHeight="{i}" behindDoc="{1 if behind else 0}" locked="0" layoutInCell="1" allowOverlap="1">'
            f'<wp:simplePos x="0" y="0"/>'
            f'<wp:positionH relativeFrom="page"><wp:posOffset>{round(x * EMU)}</wp:posOffset></wp:positionH>'
            f'<wp:positionV relativeFrom="page"><wp:posOffset>{round(y * EMU)}</wp:posOffset></wp:positionV>'
            f'<wp:extent cx="{round(w * EMU)}" cy="{round(h * EMU)}"/><wp:effectExtent l="0" t="0" r="0" b="0"/>'
            f'<wp:wrapNone/><wp:docPr id="{i}" name="{name} {i}"/><wp:cNvGraphicFramePr/>'
            f'{graphic}</wp:anchor></w:drawing>{post}</w:r>')


def _picture(rid: str, w, h) -> str:
    i = _next_id()
    return ('<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
            f'<pic:nvPicPr><pic:cNvPr id="{i}" name="Arka plan {i}"/><pic:cNvPicPr/></pic:nvPicPr>'
            f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
            f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{round(w * EMU)}" cy="{round(h * EMU)}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>')


def _textbox(paras: str, w, h, vert: str = "horz") -> str:
    return ('<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            '<a:graphicData uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape">'
            '<wps:wsp><wps:cNvSpPr txBox="1"/><wps:spPr>'
            f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{round(w * EMU)}" cy="{round(h * EMU)}"/></a:xfrm>'
            '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/><a:ln><a:noFill/></a:ln></wps:spPr>'
            f'<wps:txbx><w:txbxContent>{paras}</w:txbxContent></wps:txbx>'
            f'<wps:bodyPr rot="0" vert="{vert}" wrap="none" lIns="0" tIns="0" rIns="0" bIns="0" anchor="t" anchorCtr="0">'
            '<a:spAutoFit/></wps:bodyPr></wps:wsp></a:graphicData></a:graphic>')


def _stack_groups(lines):
    """Splits a block into groups of lines 'stacked on top of each other'.

    Lines side by side in a PDF block (e.g. the '1' in a box and the heading next to it)
    shift and get cut if they become stacked paragraphs in one box; they become separate boxes.
    """
    groups: list[list] = []
    for r, spans in sorted(lines, key=lambda l: (round(l[0].y0, 1), l[0].x0)):
        placed = False
        for g in groups:
            last = g[-1][0]
            gx0 = min(x[0].x0 for x in g)
            gx1 = max(x[0].x1 for x in g)
            below = r.y0 >= last.y1 - 0.35 * min(r.height, last.height)
            overlap_x = min(r.x1, gx1) - max(r.x0, gx0) > 0
            near = r.y0 - last.y1 < 1.5 * max(r.height, last.height)
            if below and overlap_x and near:
                g.append((r, spans))
                placed = True
                break
        if not placed:
            groups.append([(r, spans)])
    return groups


def _block_xml(lines) -> str:
    """One text block → a positioned text box. Each line is its own paragraph."""
    box = pymupdf.Rect(lines[0][0])
    for r, _ in lines[1:]:
        box |= r
    paras = []
    prev_bottom = box.y0
    for i, (r, spans) in enumerate(lines):
        size = max(s[2] for s in spans)
        line_h = max(r.height, size * 1.05)
        if i + 1 < len(lines):
            # line height = the line spacing in the PDF. When the line box (e.g. 13.3 pt for a line with μ) was
            # bigger than the spacing (11.9 pt), each line moved down a little and paragraphs overlapped the one below.
            pitch = lines[i + 1][0].y0 - r.y0
            if size * 0.8 <= pitch < line_h:
                line_h = pitch
        gap = max(0.0, r.y0 - prev_bottom)
        prev_bottom = r.y0 + line_h
        ppr = (f'<w:pPr><w:spacing w:before="{round(gap * TWIP)}" w:after="0" '
               f'w:line="{round(line_h * TWIP)}" w:lineRule="exact"/>'
               f'<w:ind w:left="{round((r.x0 - box.x0) * TWIP)}" w:right="0" w:firstLine="0"/>{_LEFT}</w:pPr>')
        paras.append("<w:p>" + ppr + "".join(_run(*s) for s in spans) + "</w:p>")
    height = (prev_bottom - box.y0) + 2
    width = box.width * 1.12 + 4  # don't cut the text if the Word font comes out slightly wider
    return _anchor(box.x0, box.y0, width, height, _textbox("".join(paras), width, height), False, "Metin")


def _vertical_xml(rect, spans, vert: str) -> str:
    """One turned line → a text box whose text runs top-to-bottom ('vert') or bottom-to-top
    ('vert270'). The box keeps the line's page rectangle; the line's thickness is its line height."""
    size = max(s[2] for s in spans)
    line_h = max(rect.width, size * 1.05)
    length = rect.height * 1.12 + 4      # room if the Word font comes out slightly longer
    y = rect.y0 if vert == "vert" else rect.y1 - length
    ppr = (f'<w:pPr><w:spacing w:before="0" w:after="0" w:line="{round(line_h * TWIP)}" w:lineRule="exact"/>'
           f'{_NO_IND}{_LEFT}</w:pPr>')
    para = "<w:p>" + ppr + "".join(_run(*s) for s in spans) + "</w:p>"
    return _anchor(rect.x0, y, line_h, length, _textbox(para, line_h, length, vert), False, "Metin")


NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
      'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
      'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
      'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
      'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
      'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml" mc:Ignorable="w14"')


def convert(pdf_path: Path, docx_path: Path, progress=lambda m: None, name: str | None = None) -> None:
    """name: the file name to show in progress messages (pdf_path may be a temporary copy)."""
    _ids["n"] = 0
    name = name or pdf_path.name
    doc = pymupdf.open(pdf_path)
    from .actions import unrotate
    unrotate(doc)          # a page shown turned by 90° (portrait page + /Rotate) → upright page
    from .textfix import build_char_map
    cmap = build_char_map(doc)
    media: list[tuple[str, bytes]] = []
    body: list[str] = []
    n = len(doc)
    for pno, page in enumerate(doc):
        progress(T("{0}: sayfa {1}/{2} (birebir düzen)", name, pno + 1, n))
        W, H = page.rect.width, page.rect.height
        order: list[int] = []
        blocks = _lines(page, cmap, order, spacing=True)
        turned = _vertical_lines(page, cmap)
        rid = f"rIdImg{pno + 1}"
        media.append((rid, _background(doc, pno, blocks + [[(r, sp)] for r, sp, _, _ in turned])))
        content = _anchor(0, 0, W, H, _picture(rid, W, H), True, "Arka plan")
        # text boxes in the PDF's reading order (copy / select-all / screen readers follow it)
        # (built after sorting: a box's number is also its drawing order)
        boxes = [(k, 0, lambda b=b: "".join(_block_xml(g) for g in _stack_groups(b)))
                 for k, b in zip(order, blocks)]
        boxes += [(k, 1, lambda r=r, sp=sp, v=v: _vertical_xml(r, sp, v)) for r, sp, v, k in turned]
        content += "".join(make() for _, _, make in sorted(boxes, key=lambda t: (t[0], t[1])))
        orient = ' w:orient="landscape"' if W > H else ""
        sect = (f'<w:sectPr><w:pgSz w:w="{round(W * TWIP)}" w:h="{round(H * TWIP)}"{orient}/>'
                '<w:pgMar w:top="0" w:right="0" w:bottom="0" w:left="0" w:header="0" w:footer="0" w:gutter="0"/>'
                '</w:sectPr>')
        # The paragraph itself is invisible and small; every page is a separate section
        p_props = '<w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>' + _NO_IND + _LEFT
        if pno < n - 1:
            body.append(f'<w:p><w:pPr>{p_props}{sect}</w:pPr>{content}</w:p>')
        else:
            body.append(f'<w:p><w:pPr>{p_props}</w:pPr>{content}</w:p>' + sect)
    doc.close()

    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document {NS}><w:body>{"".join(body)}</w:body></w:document>')
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            + "".join(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                      f'relationships/image" Target="media/{rid}.jpeg"/>' for rid, _ in media)
            + '<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
              'relationships/styles" Target="styles.xml"/></Relationships>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
              '<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/>'
              '<w:lang w:val="tr-TR"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="0"/>'
              '</w:pPr></w:pPrDefault></w:docDefaults></w:styles>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/>'
              '<Default Extension="jpeg" ContentType="image/jpeg"/>'
              '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
              'officedocument.wordprocessingml.document.main+xml"/>'
              '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-'
              'officedocument.wordprocessingml.styles+xml"/></Types>')
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                 'relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", rels)
        z.writestr("word/styles.xml", styles)
        for rid, data in media:
            z.writestr(f"word/media/{rid}.jpeg", data)


def looks_handwritten(doc, sample: int = 10) -> bool:
    """Notes written over a document with a pen (iPad/GoodNotes/Notability exports, a solved exam):
    the handwriting is hundreds of curved vector strokes per page. Flowing text can't hold them
    (strokes lose their place, pages pile up); the exact layout keeps them in the page picture.
    Measured: such pages 590-1300 curved paths, ordinary documents and slides 0-60."""
    n = min(len(doc), sample)
    if not n:
        return False
    heavy = 0
    for i in range(n):
        try:
            curves = sum(1 for d in doc[i].get_drawings() if any(it[0] == "c" for it in d["items"]))
        except Exception:
            continue
        heavy += curves >= 250
    return heavy / n >= 0.5


def looks_like_slides(doc) -> bool:
    """Slide/design document? Landscape pages or produced by presentation software."""
    meta = " ".join(str(v) for v in (doc.metadata or {}).values()).lower()
    if any(k in meta for k in ("powerpoint", "keynote", "impress", "google slides")):
        return True
    n = min(len(doc), 10)
    landscape = sum(1 for i in range(n) if doc[i].rect.width > doc[i].rect.height * 1.05)
    return n > 0 and landscape / n >= 0.6
