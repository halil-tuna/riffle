"""Word / Excel / PowerPoint / CSV tools: shrink Office files, merge Word files as Word,
convert CSV to Excel properly, handouts from presentations, extract the images in Office files."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

from .actions import ConversionError, Progress, human_size, unique_dir, unique_path
from .i18n import T

MEDIA_DIRS = ("word/media/", "ppt/media/", "xl/media/")
RASTER = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".gif", ".webp"}


def _media(name: str) -> bool:
    return name.startswith(MEDIA_DIRS) and not name.endswith("/")


# ============================================================ shrink Office files

def _shrink_image(data: bytes, ext: str, max_side: int, quality: int) -> tuple[bytes, str]:
    """(new data, new extension). Returns the old one if it doesn't get smaller. Transparent PNGs stay PNG;
    large PNGs without transparency (screenshots, photos) become JPEG."""
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        return data, ext
    if getattr(im, "is_animated", False):
        return data, ext
    im = ImageOps.exif_transpose(im)
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    if alpha and im.mode != "RGBA":
        im = im.convert("RGBA")
    if alpha and im.getchannel("A").getextrema()[0] < 250:
        out, new_ext = io.BytesIO(), ".png"
        im.save(out, "PNG", optimize=True)
    else:
        rgb = im.convert("RGB")
        candidates = []
        jpg = io.BytesIO()
        rgb.save(jpg, "JPEG", quality=quality, optimize=True, progressive=True)
        candidates.append((jpg, ".jpeg" if ext != ".jpg" else ".jpg"))
        if ext == ".png":                      # drawing/screenshot: PNG is often smaller and sharper
            png = io.BytesIO()
            (im if im.mode in ("P", "L") else rgb).save(png, "PNG", optimize=True)
            # JPEG only for a clear gain (so text edges don't blur)
            candidates.append((png, ".png"))
            if len(png.getvalue()) <= 1.6 * len(jpg.getvalue()):
                candidates = [candidates[1]]
        out, new_ext = min(candidates, key=lambda c: len(c[0].getvalue()))
    new = out.getvalue()
    if len(new) >= len(data) * 0.95:
        return data, ext
    return new, new_ext


def _rewrite_zip(src: Path, dst: Path, renames: dict[str, str], replaced: dict[str, bytes]) -> None:
    """Rewrite the Office package with changed and renamed media files:
    relationship (.rels) targets and [Content_Types].xml extensions are updated."""
    base = {old.rsplit("/", 1)[1]: new.rsplit("/", 1)[1] for old, new in renames.items()}
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        new_exts = {n.rsplit(".", 1)[1].lower() for n in renames.values()}
        for info in zin.infolist():
            name = info.filename
            if name in renames:
                zout.writestr(renames[name], replaced[name])
                continue
            data = replaced.get(name) or zin.read(name)
            if base and name.endswith(".rels"):
                text = data.decode("utf-8")
                for o, n in base.items():
                    text = re.sub(rf'(Target="[^"]*/){re.escape(o)}"', rf'\g<1>{n}"', text)
                    text = text.replace(f'Target="{o}"', f'Target="{n}"')
                data = text.encode("utf-8")
            elif name == "[Content_Types].xml" and new_exts:
                text = data.decode("utf-8")
                for o, n in renames.items():           # rare: an Override entry specific to the media
                    text = text.replace(f'PartName="/{o}"', f'PartName="/{n}"')
                for e in sorted(new_exts):
                    if f'Extension="{e}"' not in text.replace("'", '"'):
                        mime = "image/jpeg" if e in ("jpg", "jpeg") else f"image/{e}"
                        text = text.replace("</Types>", f'<Default Extension="{e}" ContentType="{mime}"/></Types>')
                data = text.encode("utf-8")
            zout.writestr(info, data)


def compress_office(paths: list[Path], progress: Progress, max_side: int = 1920, quality: int = 80,
                    **_) -> list[Path]:
    outputs = []
    for p in paths:
        if p.suffix.lower() not in (".docx", ".pptx", ".xlsx"):
            progress(T("ℹ {0}: yalnızca .docx, .pptx ve .xlsx küçültülebilir (eski biçim: önce PDF'e ya da yeni biçime kaydet)", p.name))
            continue
        try:
            zin = zipfile.ZipFile(p)
        except zipfile.BadZipFile:
            raise ConversionError(T("{0} açılamadı (bozuk ya da şifreli Office dosyası).", p.name))
        with zin:
            media = [n for n in zin.namelist() if _media(n) and Path(n).suffix.lower() in RASTER]
            replaced, renames = {}, {}
            used = set(zin.namelist())
            for k, name in enumerate(media, 1):
                progress(T("{0}: resim {1}/{2}", p.name, k, len(media)))
                ext = Path(name).suffix.lower()
                new, new_ext = _shrink_image(zin.read(name), ext, max_side, quality)
                if new_ext != ext:
                    target = name[: -len(ext)] + new_ext
                    n = 1
                    while target in used:
                        target = f"{name[: -len(ext)]}_{n}{new_ext}"
                        n += 1
                    used.add(target)
                    renames[name] = target
                if new is not None and (new_ext != ext or len(new) < zin.getinfo(name).file_size):
                    replaced[name] = new
        before = p.stat().st_size
        if not replaced:
            progress(T("ℹ {0}: küçültülecek resim yok ({1})", p.name, human_size(before)))
            continue
        out = unique_path(p.parent, f"{p.stem}_kucuk", p.suffix)
        tmp = out.with_name(out.name + ".tmp")
        try:
            _rewrite_zip(p, tmp, renames, replaced)
            _check_office(tmp)
            tmp.replace(out)
        finally:
            tmp.unlink(missing_ok=True)
        after = out.stat().st_size
        progress(T("ℹ {0}: {1} → {2} (%{3} küçüldü, {4} resim)", p.name, human_size(before), human_size(after), max(0, round(100 * (1 - after / before))), len(replaced)))
        outputs.append(out)
    if not outputs and paths:
        raise ConversionError(T("Küçültülecek resim bulunamadı."))
    return outputs


def _check_office(path: Path) -> None:
    """Does the output really open (don't leave a broken file behind)."""
    ext = path.name.replace(".tmp", "").rsplit(".", 1)[-1].lower()
    try:
        if ext == "docx":
            import docx
            docx.Document(str(path))
        elif ext == "pptx":
            import pptx
            pptx.Presentation(str(path))
        elif ext == "xlsx":
            import openpyxl
            openpyxl.load_workbook(str(path)).close()
    except ImportError:
        with zipfile.ZipFile(path) as z:
            if z.testzip() is not None:
                raise ConversionError(T("Küçültülmüş dosya doğrulanamadı."))
    except Exception as e:
        raise ConversionError(T("Küçültülmüş dosya doğrulanamadı: {0}", e))


# ============================================================ merge Word documents as Word

def merge_word(paths: list[Path], progress: Progress, ordered: bool = False, **_) -> list[Path]:
    from .actions import sorted_by_name
    paths = [p for p in (paths if ordered else sorted_by_name(paths)) if p.suffix.lower() == ".docx"]
    if len(paths) < 2:
        raise ConversionError(T("Birleştirmek için en az iki .docx dosyası seç."))
    try:
        from docx import Document
        from docxcompose.composer import Composer
    except ImportError:
        raise ConversionError(T("Word birleştirme için gerekli: pip install docxcompose"))
    progress(T("Açılıyor: {0}", paths[0].name))
    master = Document(str(paths[0]))
    comp = Composer(master)
    for k, p in enumerate(paths[1:], 2):
        progress(T("Ekleniyor ({0}/{1}): {2}", k, len(paths), p.name))
        try:
            doc = Document(str(p))
        except Exception as e:
            raise ConversionError(T("{0} açılamadı: {1}", p.name, e))
        master.add_page_break()            # every document starts on a new page
        comp.append(doc)
    out = unique_path(paths[0].parent, f"{paths[0].stem}_birlesik", ".docx")
    comp.save(str(out))
    progress(T("ℹ {0} belge birleştirildi; biçimler, resimler ve numaralı listeler korunur", len(paths)))
    return [out]


# ============================================================ merge mixed files as one Word document

WORD_NATIVE = {".docx"}
WORD_LO = {".doc", ".odt", ".rtf", ".txt", ".html", ".htm"}          # LibreOffice → .docx
VIA_PDF = {".ppt", ".pptx", ".odp", ".xls", ".xlsx", ".ods", ".csv"}  # LibreOffice → PDF → Riffle's PDF → Word


def merge_to_word(paths: list[Path], progress: Progress, ordered: bool = False, **_) -> list[Path]:
    """PDF, Word, PowerPoint, Excel and image files → ONE Word document, in the given order.

    Every file is first made a .docx in a temporary folder (nothing is left next to the sources): PDFs with
    Riffle's PDF → Word in exact layout (no OCR — Halil: "the editable boxes are not that important"), slides
    and sheets through PDF,
    .doc/.odt/.rtf with LibreOffice, images as a page with the picture. Then they are joined; each file starts
    on a new page and keeps its own page size and orientation (a landscape slide stays landscape)."""
    from .actions import sorted_by_name
    paths = [Path(p) for p in (paths if ordered else sorted_by_name(list(paths)))]
    if len(paths) < 2:
        raise ConversionError(T("Birleştirmek için en az iki dosya seç."))
    try:
        from docx import Document  # noqa: F401
        from docxcompose.composer import Composer  # noqa: F401
    except ImportError:
        raise ConversionError(T("Word birleştirme için gerekli: pip install docxcompose"))
    with tempfile.TemporaryDirectory(prefix="riffle_wm_") as tmp:
        tmp = Path(tmp)
        parts, skipped = _to_docx_all(paths, tmp, progress)
        if not parts:
            raise ConversionError("\n".join(skipped) or T("Birleştirilecek sayfa yok."))
        first = paths[0]
        out = unique_path(first.parent, f"{first.stem}_birlesik", ".docx")
        progress(T("Word belgesi kuruluyor: {0} parça", len(parts)))
        _compose_sections([d for _, d in parts], out, progress)
    for e in skipped:
        progress(T("ℹ atlandı → {0}", e))
    progress(T("ℹ {0} dosya tek Word belgesinde birleştirildi; her dosya yeni sayfada, kendi sayfa yönüyle başlar.",
               len(parts)))
    return [out]


def _to_docx_all(paths: list[Path], tmp: Path, progress: Progress) -> tuple[list, list[str]]:
    """[(source, docx in tmp)] in order + error lines for the files that could not be converted."""
    from . import actions
    docx_of: dict[Path, Path] = {}
    errors: dict[Path, str] = {}
    by_lo_docx = [p for p in paths if p.suffix.lower() in WORD_LO]
    by_pdf = [p for p in paths if p.suffix.lower() in VIA_PDF]
    pdf_of: dict[Path, Path] = {}
    if by_lo_docx:                                     # one LibreOffice call for all of them
        try:
            docx_of.update(_lo_convert(by_lo_docx, tmp / "lo_docx", "docx", progress))
        except ConversionError as e:
            for p in by_lo_docx:
                errors[p] = f"{p.name}: {e}"
        for p in by_lo_docx:
            if p not in docx_of and p not in errors:
                by_pdf.append(p)                       # LibreOffice could not make a .docx: try through PDF
    if by_pdf:
        try:
            pdf_of.update(actions.office_to_pdf_many(by_pdf, tmp / "lo_pdf", progress))
        except ConversionError as e:
            for p in by_pdf:
                errors.setdefault(p, f"{p.name}: {e}")
    for i, p in enumerate(paths):
        if p in errors or p in docx_of:
            continue
        ext = p.suffix.lower()
        try:
            if ext in WORD_NATIVE:
                docx_of[p] = p
            elif ext == ".pdf" or p in pdf_of:
                src = pdf_of.get(p, p)
                docx_of[p] = _pdf_to_docx(src, p, tmp / f"w{i:03d}", progress)
            elif ext.lstrip(".") in actions.IMAGES:
                docx_of[p] = _image_to_docx(p, tmp / f"w{i:03d}.docx")
            elif p not in pdf_of:
                errors[p] = T("{0}: desteklenmeyen dosya, atlandı", p.name)
        except actions.ConversionError as e:
            errors[p] = str(e)
        except Exception as e:
            errors[p] = f"{p.name}: {type(e).__name__}: {e}"
    parts = [(p, docx_of[p]) for p in paths if p in docx_of]
    return parts, [errors[p] for p in paths if p in errors]


def _pdf_to_docx(src: Path, original: Path, work: Path, progress: Progress) -> Path:
    """Riffle's PDF → Word in EXACT LAYOUT (every page as it looks, never OCR: quick, and scans stay pictures)
    on a copy inside the temporary folder (so nothing appears next to the source); messages name the original."""
    from .tools import pdf_to_word_exact
    work.mkdir(parents=True, exist_ok=True)
    copy = work / (original.stem + ".pdf")
    shutil.copyfile(src, copy)

    def prog(m):
        progress(m.replace(copy.name, original.name))
    outs = pdf_to_word_exact([copy], prog)
    if not outs:
        raise ConversionError(T("{0} Word'e çevrilemedi.", original.name))
    return outs[0]


def _image_to_docx(p: Path, out: Path) -> Path:
    """One page with the picture fitted inside the margins (portrait or landscape like the picture)."""
    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.shared import Cm
    from PIL import Image, ImageOps
    if p.suffix.lower() in (".heic", ".heif"):
        import pillow_heif
        pillow_heif.register_heif_opener()
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        w, h = im.size
        png = out.with_suffix(".png")
        (im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")).save(png)
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    if w > h:
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = Cm(29.7), Cm(21.0)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Cm(1.5))
    box_w = sec.page_width - sec.left_margin - sec.right_margin
    box_h = sec.page_height - sec.top_margin - sec.bottom_margin - Cm(1)
    scale = min(box_w / w, box_h / h)
    para = doc.paragraphs[0] if doc.paragraphs else doc.add_paragraph()
    para.alignment = 1                                   # centre
    para.add_run().add_picture(str(png), width=int(w * scale), height=int(h * scale))
    doc.save(str(out))
    return out


def _lo_convert(paths: list[Path], outdir: Path, fmt: str, progress: Progress) -> dict:
    """{source: converted file} with ONE LibreOffice call (like actions.office_to_pdf_many)."""
    import sys
    from . import actions
    soffice = actions._find_soffice()
    if not soffice:
        return {}                                        # → through PDF (Office on Windows can do that part)
    outdir.mkdir(parents=True, exist_ok=True)
    staged = []
    for i, p in enumerate(paths):
        dst = outdir / "_kaynak" / f"{i:03d}{p.suffix.lower()}"
        dst.parent.mkdir(exist_ok=True)
        shutil.copyfile(p, dst)
        staged.append(dst)
    progress(T("Word'e çevriliyor: {0} dosya", len(paths)) if len(paths) > 1 else
             T("Word'e çevriliyor: {0}", paths[0].name))
    flags = 0x08000000 if sys.platform == "win32" else 0
    with actions._LoProfile() as profile:
        cmd = [soffice, f"-env:UserInstallation={profile}", "--headless", "--convert-to", fmt,
               "--outdir", str(outdir), *map(str, staged)]
        actions._run_cancellable(cmd, 120 + 120 * len(paths), flags)
    return {p: outdir / f"{st.stem}.{fmt}" for p, st in zip(paths, staged) if (outdir / f"{st.stem}.{fmt}").exists()}


def _compose_sections(parts: list[Path], out: Path, progress: Progress) -> None:
    """Joins .docx files; every part keeps ITS look: page size, margins, orientation, styles, headers/footers.

    docxcompose alone gets three things wrong for documents that were not made from one template:
    - it drops an appended document's last section properties (a landscape deck would end on A4 portrait);
    - styles with the same name take the FIRST document's definition (two Word files made by Word's own PDF
      import both have a 'Body Text', one 19.5 pt Arial, the other Times: the second came out in big Arial,
      its lines moved and the pen marks anchored to them landed in the wrong places — Halil's midterm);
    - the appended documents' headers and footers are dropped (the university logo was gone).
    So every part's styles are made its own first (_isolate_styles), before each part the current last
    section is closed with a section break carrying its own properties, and after the part each of its
    sections gets its own page setup and its own headers/footers (_carry_headers)."""
    from copy import deepcopy
    from docx import Document
    from docx.oxml.ns import qn
    from docxcompose.composer import Composer
    master = Document(str(parts[0]))
    comp = Composer(master)
    body = master.element.body
    keep = {qn("w:" + t) for t in ("pgSz", "pgMar", "paperSrc", "pgBorders", "lnNumType", "pgNumType", "cols",
                                   "vAlign", "titlePg", "textDirection", "bidi", "rtlGutter", "docGrid")}
    for k, part in enumerate(parts[1:], 2):
        progress(T("Ekleniyor ({0}/{1}): {2}", k, len(parts), part.name))
        try:
            doc = Document(str(part))
        except Exception as e:
            raise ConversionError(T("{0} açılamadı: {1}", part.name, e))
        _isolate_styles(doc, k)
        _merge_font_tables(master, doc)
        _close_last_section(master)
        inner_before = len(body.findall(f"{qn('w:p')}/{qn('w:pPr')}/{qn('w:sectPr')}"))
        comp.append(doc)
        dst = body.find(qn("w:sectPr"))
        src_sects = [s._sectPr for s in doc.sections]
        if dst is None or not src_sects:
            continue
        src = src_sects[-1]
        for old in [c for c in dst if c.tag in keep or c.tag == qn("w:type")]:
            dst.remove(old)                                     # (no w:type = next page)
        for el in src:
            if el.tag in keep:
                dst.append(deepcopy(el))
        _sort_children(dst, _SECTPR_ORDER)
        # headers/footers: the part's inner sections came in as paragraphs, its last one is the body's now
        inner = body.findall(f"{qn('w:p')}/{qn('w:pPr')}/{qn('w:sectPr')}")[inner_before:]
        if len(inner) == len(src_sects) - 1:
            for i, (s_src, s_dst) in enumerate(zip(src_sects, inner + [dst])):
                _carry_headers(master, doc, s_src, s_dst, first=i == 0)
    comp.save(str(out))


_SECTPR_ORDER = ("headerReference", "footerReference", "footnotePr", "endnotePr", "type", "pgSz", "pgMar",
                 "paperSrc", "pgBorders", "lnNumType", "pgNumType", "cols", "formProt", "vAlign", "noEndnote",
                 "titlePg", "textDirection", "bidi", "rtlGutter", "docGrid", "printerSettings", "sectPrChange")
_STYLE_ORDER = ("name", "aliases", "basedOn", "next", "link", "autoRedefine", "hidden", "uiPriority", "semiHidden",
                "unhideWhenUsed", "qFormat", "locked", "personal", "personalCompose", "personalReply", "rsid",
                "pPr", "rPr", "tblPr", "trPr", "tcPr", "tblStylePr")
_PPR_ORDER = ("pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
              "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
              "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid",
              "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
              "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr", "pPrChange")
_RPR_ORDER = ("rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
              "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing", "w",
              "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText", "vertAlign",
              "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath", "rPrChange")
# What Word assumes when neither the style chain nor the document defaults say it: written into a part's root
# style so that the FIRST document's defaults can't fill the gap instead.
_WORD_FALLBACK_PPR = '<w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/><w:ind w:left="0" w:right="0"/>'
_WORD_FALLBACK_RPR = '<w:sz w:val="20"/><w:szCs w:val="20"/>'


def _sort_children(el, order: tuple) -> None:
    """Puts the children in schema order (Word rejects some out-of-order files as 'unreadable content')."""
    rank = {t: i for i, t in enumerate(order)}
    kids = list(el)
    kids.sort(key=lambda c: rank.get(c.tag.rsplit("}", 1)[-1], len(order)))   # stable: unknown tags last
    for c in kids:
        el.append(c)


def _merge_props(dst, src) -> None:
    """Adds src's properties (a pPr/rPr) that dst doesn't set; for the same element, the missing attributes.
    dst wins everywhere: it is the more specific one (a style over the document defaults)."""
    from copy import deepcopy
    from docx.oxml.ns import qn
    for el in src:
        have = dst.find(el.tag)
        if have is None:
            dst.append(deepcopy(el))
            continue
        name = el.tag.rsplit("}", 1)[-1]
        for a, v in el.attrib.items():
            if a in have.attrib:
                continue
            local = a.rsplit("}", 1)[-1]
            if name == "rFonts":                       # 'ascii' and 'asciiTheme' are one setting: don't override
                base = _FONT_THEME.get(local, local)
                if any(have.get(qn("w:" + x)) is not None for x in (base, _THEME_OF[base])):
                    continue
            if name == "ind" and local in ("firstLine", "hanging", "firstLineChars", "hangingChars") and any(
                    have.get(qn("w:" + x)) is not None for x in ("firstLine", "hanging", "firstLineChars",
                                                                 "hangingChars")):
                continue
            if name == "ind" and local in ("left", "start") and any(
                    have.get(qn("w:" + x)) is not None for x in ("left", "start")):
                continue
            if name == "ind" and local in ("right", "end") and any(
                    have.get(qn("w:" + x)) is not None for x in ("right", "end")):
                continue
            have.set(a, v)


_FONT_THEME = {"asciiTheme": "ascii", "hAnsiTheme": "hAnsi", "eastAsiaTheme": "eastAsia", "cstheme": "cs"}
_THEME_OF = {v: k for k, v in _FONT_THEME.items()} | {"hint": "hint"}


def _isolate_styles(doc, k: int) -> None:
    """Makes a part's formatting independent of the document it is appended to.

    Every style gets a new id and name (docxcompose matches styles by NAME and keeps the first document's), the
    part's document defaults are written into its root paragraph styles, and every paragraph and table names its
    style explicitly (a paragraph without one would take the first document's default style)."""
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls, qn
    W = lambda t: qn("w:" + t)                                    # noqa: E731
    styles = doc.styles.element
    suffix = f"R{k}"
    id_map: dict[str, str] = {}
    default_of: dict[str, str] = {}                               # type → new id of the default style
    all_styles = styles.findall(W("style"))
    taken = {st.get(W("styleId")) for st in all_styles}
    for st in all_styles:
        old = st.get(W("styleId"))
        new = f"{old}{suffix}"
        while new in taken:
            new += "x"
        id_map[old] = new
    for st in all_styles:
        st.set(W("styleId"), id_map[st.get(W("styleId"))])
        name = st.find(W("name"))
        if name is not None:
            name.set(W("val"), f"{name.get(W('val'))} ({k})")
        for ref in ("basedOn", "next", "link"):
            el = st.find(W(ref))
            if el is not None and el.get(W("val")) in id_map:
                el.set(W("val"), id_map[el.get(W("val"))])
        if st.get(W("default")) in ("1", "true", "on"):
            default_of[st.get(W("type"))] = st.get(W("styleId"))
            del st.attrib[W("default")]
    # a part without a default paragraph style (Riffle's exact layout, python-docx-free files) gets one
    if "paragraph" not in default_of:
        new = f"Normal{suffix}"
        while new in taken or new in id_map.values():
            new += "x"
        styles.append(parse_xml(f'<w:style {nsdecls("w")} w:type="paragraph" w:styleId="{new}">'
                                f'<w:name w:val="Normal ({k})"/><w:qFormat/></w:style>'))
        default_of["paragraph"] = new
    # document defaults → every root paragraph style (no basedOn); then Word's own fallbacks for what's still unset
    dd = styles.find(W("docDefaults"))
    ppr_d = dd.find(f"{W('pPrDefault')}/{W('pPr')}") if dd is not None else None
    rpr_d = dd.find(f"{W('rPrDefault')}/{W('rPr')}") if dd is not None else None
    fallback_p = parse_xml(f'<w:pPr {nsdecls("w")}>{_WORD_FALLBACK_PPR}</w:pPr>')
    fallback_r = parse_xml(f'<w:rPr {nsdecls("w")}>{_WORD_FALLBACK_RPR}</w:rPr>')
    for st in styles.findall(W("style")):
        if st.get(W("type")) != "paragraph" or st.find(W("basedOn")) is not None:
            continue
        for tag, d, fb, order in (("pPr", ppr_d, fallback_p, _PPR_ORDER), ("rPr", rpr_d, fallback_r, _RPR_ORDER)):
            el = st.find(W(tag))
            if el is None:
                el = parse_xml(f"<w:{tag} {nsdecls('w')}/>")
                st.append(el)
            if d is not None:
                _merge_props(el, d)
            _merge_props(el, fb)
            _sort_children(el, order)
        _sort_children(st, _STYLE_ORDER)
    # rename the references everywhere the part's content is, and make the default style explicit
    roots = [doc.element]
    for rel in doc.part.rels.values():
        if not rel.is_external and hasattr(rel.target_part, "element"):
            roots.append(rel.target_part.element)
    num = None
    try:
        num = doc.part.numbering_part.element
        roots.append(num)
    except (KeyError, NotImplementedError, AttributeError):
        pass
    for root in roots:
        for tag in ("pStyle", "rStyle", "tblStyle", "styleLink", "numStyleLink"):
            for el in root.iter(W(tag)):
                if el.get(W("val")) in id_map:
                    el.set(W("val"), id_map[el.get(W("val"))])
    p_default, t_default = default_of["paragraph"], default_of.get("table")
    for root in roots:
        if root is num:
            continue
        for p in root.iter(W("p")):
            ppr = p.find(W("pPr"))
            if ppr is None:
                ppr = parse_xml(f"<w:pPr {nsdecls('w')}/>")
                p.insert(0, ppr)
            if ppr.find(W("pStyle")) is None:
                ps = parse_xml(f'<w:pStyle {nsdecls("w")} w:val="{p_default}"/>')
                ppr.insert(0, ps)
        if t_default:
            for tbl in root.iter(W("tbl")):
                tpr = tbl.find(W("tblPr"))
                if tpr is not None and tpr.find(W("tblStyle")) is None:
                    tpr.insert(0, parse_xml(f'<w:tblStyle {nsdecls("w")} w:val="{t_default}"/>'))


def _carry_headers(master, doc, src_sect, dst_sect, first: bool) -> None:
    """The part's section src_sect → its headers/footers onto the merged document's section dst_sect.

    docxcompose drops them (the first document's would continue over every other file). In a part's first section,
    a header type the part doesn't define gets an empty one, so the previous file's header doesn't run on."""
    import io
    from copy import deepcopy
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import parse_xml
    from docx.oxml.ns import nsdecls, qn
    from docx.parts.hdrftr import FooterPart, HeaderPart
    W = lambda t: qn("w:" + t)                                    # noqa: E731
    R_ID, R_EMBED, R_LINK = qn("r:id"), qn("r:embed"), qn("r:link")
    for old in [c for c in dst_sect if c.tag in (W("headerReference"), W("footerReference"))]:
        dst_sect.remove(old)
    refs = []
    for ref in src_sect:
        kind = {W("headerReference"): "header", W("footerReference"): "footer"}.get(ref.tag)
        if kind is None:
            continue
        try:
            src_part = doc.part.related_parts[ref.get(R_ID)]
        except KeyError:
            continue
        cls = HeaderPart if kind == "header" else FooterPart
        new = cls.new(master.part.package)
        new._element = deepcopy(src_part.element)
        for el in new._element.iter():
            for attr in (R_EMBED, R_ID, R_LINK):
                rid = el.get(attr)
                if rid is None or rid not in src_part.rels:
                    continue
                rel = src_part.rels[rid]
                try:
                    if rel.is_external:
                        el.set(attr, new.relate_to(rel.target_ref, rel.reltype, is_external=True))
                    elif rel.reltype == RT.IMAGE:
                        el.set(attr, new.get_or_add_image(io.BytesIO(rel.target_part.blob))[0])
                except Exception:
                    pass
        _ensure_styles(master, doc, new._element)
        refs.append((kind, ref.get(W("type")) or "default",
                     master.part.relate_to(new, RT.HEADER if kind == "header" else RT.FOOTER)))
    if first:
        types = ["default"] + (["first"] if src_sect.find(W("titlePg")) is not None else [])
        for kind, cls, rt in (("header", HeaderPart, RT.HEADER), ("footer", FooterPart, RT.FOOTER)):
            for t in types:
                if not any(k == kind and tt == t for k, tt, _ in refs):
                    refs.append((kind, t, master.part.relate_to(cls.new(master.part.package), rt)))
    for i, (kind, t, rid) in enumerate(refs):
        dst_sect.insert(i, parse_xml(f'<w:{kind}Reference {nsdecls("w", "r")} w:type="{t}" r:id="{rid}"/>'))


def _merge_font_tables(master, doc) -> None:
    """Font descriptions (family, panose, charset) of the part that the merged document lacks: without them a
    missing font (e.g. Cambria Math on a computer without it) is replaced by a different-looking one and the
    part's pages reflow. Embedded font files are not carried (their entries go without the embed links)."""
    from lxml import etree
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn
    try:
        ours = master.part.part_related_by(RT.FONT_TABLE)
        theirs = doc.part.part_related_by(RT.FONT_TABLE)
        root, other = etree.fromstring(ours.blob), etree.fromstring(theirs.blob)
    except Exception:
        return
    have = {f.get(qn("w:name")) for f in root.findall(qn("w:font"))}
    added = False
    for f in other.findall(qn("w:font")):
        if f.get(qn("w:name")) in have:
            continue
        for emb in [c for c in f if c.tag.rsplit("}", 1)[-1].startswith("embed")]:
            f.remove(emb)
        root.append(f)
        have.add(f.get(qn("w:name")))
        added = True
    if added:
        ours._blob = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _ensure_styles(master, doc, element) -> None:
    """Styles used in element (a copied header) that the merged document doesn't have yet: copied from the part,
    with the styles they are based on."""
    from copy import deepcopy
    from docx.oxml.ns import qn
    W = lambda t: qn("w:" + t)                                    # noqa: E731
    ours = master.styles.element
    have = {st.get(W("styleId")) for st in ours.findall(W("style"))}
    theirs = {st.get(W("styleId")): st for st in doc.styles.element.findall(W("style"))}
    todo = [el.get(W("val")) for tag in ("pStyle", "rStyle", "tblStyle") for el in element.iter(W(tag))]
    while todo:
        sid = todo.pop()
        if sid in have or sid not in theirs:
            continue
        ours.append(deepcopy(theirs[sid]))
        have.add(sid)
        for ref in ("basedOn", "link", "next"):
            el = theirs[sid].find(W(ref))
            if el is not None:
                todo.append(el.get(W("val")))


def _close_last_section(master) -> None:
    """Ends the document's current last section here, with a copy of its properties (a section break)."""
    from copy import deepcopy
    from docx.oxml.ns import qn
    body = master.element.body
    sect = body.find(qn("w:sectPr"))
    if sect is None:
        return
    last = None
    for el in reversed(list(body)):
        if el.tag == qn("w:sectPr"):
            continue
        last = el
        break
    if last is not None and last.tag == qn("w:p") and last.find(qn("w:pPr") + "/" + qn("w:sectPr")) is None:
        p = last
        run = p[-1] if len(p) else None                         # a page break at the very end of the last
        if run is not None and run.tag == qn("w:r") and len(run) and run[-1].tag == qn("w:br") \
                and run[-1].get(qn("w:type")) == "page":        # paragraph would leave an empty page
            run.remove(run[-1])
    else:
        from docx.oxml import OxmlElement
        p = OxmlElement("w:p")
        sect.addprevious(p)
    ppr = p.find(qn("w:pPr"))
    if ppr is None:
        from docx.oxml import OxmlElement
        ppr = OxmlElement("w:pPr")
        p.insert(0, ppr)
    ppr.append(deepcopy(sect))


# ============================================================ CSV → Excel

ENCODINGS = ("utf-8-sig", "utf-16", "cp1254", "iso-8859-9", "cp1252")
ENC_NAMES = {"utf-8-sig": "UTF-8", "utf-16": "UTF-16", "cp1254": T("Windows-1254 (Türkçe)"),
             "iso-8859-9": T("ISO-8859-9 (Türkçe)"), "cp1252": "Windows-1252"}


def _decode(data: bytes) -> tuple[str, str]:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16"), "utf-16"
    try:
        return data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        pass
    # If not UTF-8, an old Windows encoding: Turkish (1254) and Western European (1252) differ in only 6 bytes
    # (ğ ı ş İ Ğ Ş ↔ ð ý þ Ý Ð Þ). German/French/Spanish files don't have these bytes: both are right.
    # If these bytes exist: Western European if there is a clear number of letters Turkish doesn't use
    # (á é í ó ú ß ñ æ ø …) (Icelandic etc.), otherwise Turkish (ı/ş/ğ are very common in Turkish, foreign letters rare).
    tr = data.decode("cp1254", errors="replace")
    west = data.decode("cp1252", errors="replace")
    paired = sum(tr.count(c) for c in "ğĞıİşŞ")
    foreign = sum(west.count(c) for c in "áéíóúàèìòùãõñßæøåëïÁÉÍÓÚÀÈÌÒÙÃÕÑÆØÅ")
    if paired and foreign > paired / 2:
        return west, "cp1252"
    return tr, "cp1254"


def _delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:50])
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t|").delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ";,\t|"}
        return max(counts, key=counts.get) if max(counts.values()) else ","


_INT = re.compile(r"^-?\d+$")
_TR_NUM = re.compile(r"^-?\d{1,3}(\.\d{3})+(,\d+)?$|^-?\d+,\d+$")
_EN_NUM = re.compile(r"^-?\d{1,3}(,\d{3})+(\.\d+)?$|^-?\d+\.\d+$")
_DATE = re.compile(r"^(\d{1,2})([./-])(\d{1,2})\2(\d{4})$")
_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")


def _column_style(values: list[str]) -> tuple[str, str]:
    """(number format, date order) — based on the evidence in the column.
    Number: 'tr' (1.234,56; most of Europe) or 'en' (1,234.56; US/UK).
    Date: 'dmy' (day first), 'mdy' (US, month first) or 'text' (undecided: don't convert)."""
    vals = [v.strip() for v in values]
    tr = sum(1 for v in vals if _TR_NUM.match(v) and not _EN_NUM.match(v))
    en = sum(1 for v in vals if _EN_NUM.match(v) and not _TR_NUM.match(v))
    dates = [m for m in (_DATE.match(v) for v in vals) if m]
    order = "dmy"
    if any(int(m[1]) > 12 for m in dates):
        order = "dmy"                                   # 25/12/2026: the first number is the day
    elif any(int(m[3]) > 12 for m in dates):
        order = "mdy"                                   # 12/25/2026: the second number is the day (US)
    elif any(m[2] == "/" for m in dates):
        order = "text"                                  # only things like 03/11/2026: 3 November or 11 March is unclear
    return ("tr" if tr >= en else "en"), order


def _convert(v: str, style: tuple[str, str]):
    s = v.strip()
    if not s:
        return None
    if _INT.match(s):
        digits = s.lstrip("-")
        # leading zero (phone, postcode) or 11+ digits (national ID, account number): keep as text
        if (len(digits) > 1 and digits[0] == "0") or len(digits) >= 11:
            return s
        return int(s)
    num, order = style
    if num == "tr" and _TR_NUM.match(s):
        return float(s.replace(".", "").replace(",", "."))
    if num == "en" and _EN_NUM.match(s):
        return float(s.replace(",", ""))
    m = _DATE.match(s)
    if m and order != "text":
        a, b, y = int(m[1]), int(m[3]), int(m[4])
        day, month = (a, b) if order == "dmy" else (b, a)
        try:
            return datetime(y, month, day)
        except ValueError:
            return s
    m = _ISO_DATE.match(s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return s
    return s


def csv_to_excel(paths: list[Path], progress: Progress, **_) -> list[Path]:
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter
    outputs = []
    for p in paths:
        progress(T("Okunuyor: {0}", p.name))
        text, enc = _decode(p.read_bytes())
        delim = _delimiter(text)
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        rows = [r for r in rows if any(c.strip() for c in r)]
        if not rows:
            progress(T("ℹ {0}: boş dosya", p.name))
            continue
        width = max(len(r) for r in rows)
        cols = [[r[i] for r in rows[1:] if i < len(r)] for i in range(width)]
        styles = [_column_style(c) for c in cols]
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = p.stem[:31] or T("Sayfa1")
        for r_i, row in enumerate(rows, 1):
            for c_i, v in enumerate(row, 1):
                val = v if r_i == 1 else _convert(v, styles[c_i - 1])
                cell = ws.cell(row=r_i, column=c_i, value=val)
                if isinstance(val, datetime):
                    cell.number_format = "DD.MM.YYYY"
                elif isinstance(val, float):
                    cell.number_format = "#,##0.00"
        for c in ws[1]:
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="E8EEF8")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for i in range(1, width + 1):
            longest = max((len(str(r[i - 1])) for r in rows[:500] if i - 1 < len(r)), default=8)
            ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 8), 50)
        out = unique_path(p.parent, p.stem, ".xlsx")
        wb.save(out)
        sep = {";": T("noktalı virgül"), ",": T("virgül"), "\t": "sekme", "|": T("dikey çizgi")}[delim]
        progress(T("ℹ {0}: {1} satır, {2} sütun · kodlama {3} · ayırıcı {4}", p.name, len(rows) - 1, width, ENC_NAMES.get(enc, enc), sep))
        outputs.append(out)
    return outputs


# ============================================================ handouts from presentations

def slide_handout(paths: list[Path], progress: Progress, per_page: int = 3, **_) -> list[Path]:
    """3 slides per page (left), note lines next to them (right). A4 portrait."""
    import pymupdf
    from .actions import office_to_pdf_many
    outputs = []
    with tempfile.TemporaryDirectory(prefix="riffle_handout_") as tmp:
        pdfs = {p: p for p in paths if p.suffix.lower() == ".pdf"}
        office = [p for p in paths if p not in pdfs]
        if office:
            pdfs.update(office_to_pdf_many(office, Path(tmp), progress))
        for p in paths:
            src_pdf = pdfs.get(p)
            if not src_pdf:
                progress(T("ℹ {0} çevrilemedi", p.name))
                continue
            progress(T("Not çıktısı: {0}", p.name))
            with pymupdf.open(src_pdf) as src, pymupdf.open() as out:
                W, H = pymupdf.paper_size("a4")
                margin, gap = 36, 18
                slot_h = (H - 2 * margin - 24 - (per_page - 1) * gap) / per_page
                slide_w = (W - 2 * margin) * 0.52
                for start in range(0, len(src), per_page):
                    page = out.new_page(width=W, height=H)
                    page.insert_text((margin, margin + 6), p.stem, fontsize=9, color=(0.4, 0.4, 0.4))
                    page.insert_text((W - margin - 60, margin + 6),
                                     f"{start // per_page + 1} / {(len(src) + per_page - 1) // per_page}",
                                     fontsize=9, color=(0.4, 0.4, 0.4))
                    for k, i in enumerate(range(start, min(start + per_page, len(src)))):
                        y0 = margin + 24 + k * (slot_h + gap)
                        r = src[i].rect
                        scale = min(slide_w / r.width, slot_h / r.height)
                        box = pymupdf.Rect(margin, y0, margin + r.width * scale, y0 + r.height * scale)
                        page.show_pdf_page(box, src, i)
                        page.draw_rect(box, color=(0.6, 0.6, 0.6), width=0.5)
                        x0, x1 = box.x1 + 16, W - margin
                        y = y0 + 14
                        while y < y0 + slot_h:
                            page.draw_line((x0, y), (x1, y), color=(0.75, 0.75, 0.75), width=0.5)
                            y += 18
                dst = unique_path(p.parent, f"{p.stem}_not_ciktisi", ".pdf")
                out.save(dst, garbage=3, deflate=True)
                outputs.append(dst)
    return outputs


# ============================================================ extract the images in Office files

def extract_office_images(paths: list[Path], progress: Progress, min_bytes: int = 2048, **_) -> list[Path]:
    outputs = []
    for p in paths:
        if p.suffix.lower() not in (".docx", ".pptx", ".xlsx"):
            progress(T("ℹ {0}: yalnızca .docx, .pptx ve .xlsx", p.name))
            continue
        try:
            z = zipfile.ZipFile(p)
        except zipfile.BadZipFile:
            raise ConversionError(T("{0} açılamadı.", p.name))
        seen, saved, skipped_small, vector = set(), [], 0, 0
        with z:
            media = sorted(n for n in z.namelist() if _media(n))
            folder = None
            for name in media:
                ext = Path(name).suffix.lower()
                data = z.read(name)
                if ext in (".emf", ".wmf", ".svg"):
                    vector += 1
                if len(data) < min_bytes:
                    skipped_small += 1
                    continue
                h = hashlib.md5(data).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                if folder is None:
                    folder = unique_dir(p.parent, f"{p.stem}_resimler")
                    folder.mkdir(exist_ok=True)
                dst = folder / f"resim_{len(saved) + 1:02d}{'.jpg' if ext == '.jpeg' else ext}"
                dst.write_bytes(data)
                saved.append(dst)
        if not saved:
            progress(T("ℹ {0}: resim yok", p.name))
            continue
        note = T("ℹ {0}: {1} resim → {2}", p.name, len(saved), folder.name)
        if skipped_small:
            note += T(" · {0} küçük simge atlandı", skipped_small)
        if vector:
            note += T(" · {0} çizim (EMF/WMF/SVG) olduğu gibi kaydedildi", vector) if any(
                s.suffix in (".emf", ".wmf", ".svg") for s in saved) else ""
        progress(note)
        outputs.append(folder)
    if not outputs and paths:
        raise ConversionError(T("Resim bulunamadı."))
    return outputs
