"""All conversion operations.

Each operation takes a list of files and returns a list of output files.
Outputs are always written next to the source file and never overwrite an
existing file (file (1).pdf, file (2).pdf ...).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from .i18n import T

try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:
    try:
        import fitz as pymupdf  # type: ignore  # older versions
    except ImportError:
        # so install.py can read the menu list before the libraries are installed.
        # Conversions run from the .venv, where it is always installed.
        pymupdf = None  # type: ignore

PDF = ["pdf"]
OFFICE = ["doc", "docx", "odt", "rtf", "txt",
          "xls", "xlsx", "ods", "csv",
          "ppt", "pptx", "odp",
          "html", "htm"]
IMAGES = ["jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "gif", "heic", "heif"]
FOLDER = "<klasör>"   # pseudo extension for right-click actions on folders
OOXML = ["docx", "pptx", "xlsx"]          # newer zip-based Office formats
SLIDES = ["ppt", "pptx", "odp"]

Progress = Callable[[str], None]


class ConversionError(Exception):
    """Readable error shown to the user."""


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

# For the 'save outputs to the Desktop / this folder' setting: {source folder: target folder} (filled by main)
OUTPUT_REDIRECT: dict = {}


def _redirect(folder: Path) -> Path:
    try:
        return OUTPUT_REDIRECT.get(Path(folder).resolve(), folder)
    except OSError:
        return folder


# Output name suffixes: written in Turkish in the source, translated in the English UI (end of the name)
_SUFFIX_EN = [
    (r"_cok_sikistirilmis$", "_compressed_max"), (r"_sikistirilmis$", "_compressed"), (r"_birlesik$", "_merged"),
    (r"_duzenlenmis$", "_edited"), (r"_kucuk$", "_small"), (r"_resimler$", "_images"),
    (r"_sayfa_silindi$", "_pages_removed"), (r"_secilen_sayfalar$", "_selected_pages"), (r"_sayfalar$", "_pages"),
    (r"_bossuz$", "_no_blank_pages"), (r"_belge$", "_document"), (r"_duzeltilmis$", "_fixed"),
    (r"_siyah_beyaz$", "_grayscale"), (r"_kitapcik$", "_booklet"), (r"_not_ciktisi$", "_handout"),
    (r"_sifresiz$", "_unlocked"), (r"_sifreli$", "_protected"), (r"_dondurulmus(_\d+)?$", r"_rotated\1"),
    (r"_kirpilmis$", "_cropped"), (r"_imzali$", "_signed"), (r"_filigranli$", "_watermarked"),
    (r"_numarali$", "_numbered"), (r"_doldurulmus$", "_filled"), (r"_onarilmis$", "_repaired"),
    (r"_karartilmis$", "_redacted"), (r"_ters$", "_reversed"), (r"_(\d)li$", r"_\1up"),
    (r"_tablolar$", "_tables"), (r"_a4$", "_a4"), (r"_parcalar$", "_parts"), (r"_parca(\d+)$", r"_part\1"),
    (r"^karsilastirma_", "comparison_"),
]


def localized_stem(stem: str) -> str:
    from .i18n import lang
    if lang() != "en":
        return stem
    for pat, rep in _SUFFIX_EN:
        new = re.sub(pat, rep, stem)
        if new != stem:
            return new
    return stem


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    """Returns folder/stem.ext if free, otherwise a path like 'stem (1).ext'."""
    folder = _redirect(folder)
    stem = localized_stem(stem)
    ext = ext if ext.startswith(".") else "." + ext
    p = folder / f"{stem}{ext}"
    i = 1
    while p.exists():
        p = folder / f"{stem} ({i}){ext}"
        i += 1
    return p


def unique_dir(folder: Path, name: str) -> Path:
    folder = _redirect(folder)
    name = localized_stem(name)
    p = folder / name
    i = 1
    while p.exists():
        p = folder / f"{name} ({i})"
        i += 1
    p.mkdir(parents=True)
    return p


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def open_pdf(path: Path, password: str | None = None):
    try:
        doc = pymupdf.open(path)
    except Exception as e:
        raise ConversionError(T("{0} açılamadı (bozuk PDF olabilir): {1}", path.name, e))
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            doc.close()
            raise ConversionError(T("{0} şifreli. Önce 'Şifreyi kaldır' kullan.", path.name))
    return doc


def sorted_by_name(paths: list[Path]) -> list[Path]:
    # Natural sort as in Windows Explorer: page2 < page10
    import re

    def key(p: Path):
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r"(\d+)", p.name)]
    return sorted(paths, key=key)


# --------------------------------------------------------------------------
# PDF operations
# --------------------------------------------------------------------------

def append_with_bookmark(out_doc, src_doc, title: str, level: int, toc: list) -> None:
    """Appends src_doc to the end of out_doc; adds a bookmark called 'title' and keeps the source's own
    table of contents by moving it one level down."""
    start = len(out_doc) + 1                    # bookmarks use 1-based page numbers
    out_doc.insert_pdf(src_doc)
    toc.append([level, title, start])
    try:
        for lvl, t, pg, *_ in src_doc.get_toc(simple=False):
            if pg >= 1:
                toc.append([level + lvl, t, start + pg - 1])
    except Exception:
        pass


def fix_tounicode(doc) -> int:
    """In fonts embedded by PyMuPDF, ToUnicode 'bfrange' lines overflow the last byte of the target
    (e.g. <0062> <00ff> <00a0>: ı should be 0x131). Standard readers (Acrobat, Ghostscript) wrap the last
    byte, so 'ı' is copied as '1' and 'ğ' as 'á'. Fixes it by splitting overflowing ranges; returns the number of fixed fonts."""
    import re as _re
    fixed = 0
    seen = set()
    for page in doc:
        for f in page.get_fonts(full=True):
            xref = f[0]
            if xref in seen:
                continue
            seen.add(xref)
            m = _re.search(r"/ToUnicode (\d+) 0 R", doc.xref_object(xref))
            if not m:
                continue
            tu = int(m.group(1))
            try:
                cmap = doc.xref_stream(tu).decode("latin-1")
            except Exception:
                continue
            bad = False
            out_ranges = []
            for blk in _re.findall(r"beginbfrange(.*?)endbfrange", cmap, _re.S):
                for lo, hi, dst in _re.findall(r"<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>", blk):
                    lo_i, hi_i, d = int(lo, 16), int(hi, 16), int(dst, 16)
                    while lo_i <= hi_i:
                        room = 0xFF - (d & 0xFF)               # how many steps before the target's last byte overflows
                        n = min(hi_i - lo_i, room, 0xFF - (lo_i & 0xFF))
                        if n < hi_i - lo_i:
                            bad = True
                        out_ranges.append((lo_i, lo_i + n, d))
                        lo_i += n + 1
                        d += n + 1
            if not bad:
                continue
            w = len(lo) if out_ranges else 4
            body = []
            for i in range(0, len(out_ranges), 100):
                part = out_ranges[i:i + 100]
                body.append(f"{len(part)} beginbfrange")
                body += [f"<{a:0{w}x}> <{b:0{w}x}> <{c:04x}>" for a, b, c in part]
                body.append("endbfrange")
            new = _re.sub(r"\d+\s+beginbfrange.*?endbfrange\s*", "", cmap, flags=_re.S)   # delete the old ranges
            new = new.replace("endcmap", "\n".join(body) + "\nendcmap", 1)
            doc.update_stream(tu, new.encode("latin-1"))
            fixed += 1
    return fixed


def save_with_toc(out_doc, toc: list, path: Path) -> None:
    if toc:
        try:
            out_doc.set_toc(toc)
        except Exception:                        # broken source table of contents → file titles only
            out_doc.set_toc([e for e in toc if e[0] == 1])
    out_doc.save(path, garbage=3, deflate=True)


def merge_pdfs(paths: list[Path], progress: Progress, ordered: bool = False, **_) -> list[Path]:
    if len(paths) < 2:
        raise ConversionError(T("Birleştirmek için en az 2 PDF seç."))
    if not ordered:                      # if it came from the order window, keep the user's order
        paths = sorted_by_name(paths)
    out_doc, toc = pymupdf.open(), []
    for p in paths:
        progress(T("Ekleniyor: {0}", p.name))
        with open_pdf(p) as src:
            append_with_bookmark(out_doc, src, p.stem, 1, toc)
    out = unique_path(paths[0].parent, f"{paths[0].stem}_birlesik", ".pdf")
    save_with_toc(out_doc, toc, out)
    out_doc.close()
    return [out]


def split_pdf(paths: list[Path], progress: Progress, **_) -> list[Path]:
    outputs = []
    for p in paths:
        with open_pdf(p) as src:
            n = len(src)
            folder = unique_dir(p.parent, f"{p.stem}_sayfalar")
            width = len(str(n))
            for i in range(n):
                progress(T("{0}: sayfa {1}/{2}", p.name, i + 1, n))
                part = pymupdf.open()
                part.insert_pdf(src, from_page=i, to_page=i)
                part.save(folder / f"{p.stem}_{T('s')}{i + 1:0{width}d}.pdf",
                          garbage=3, deflate=True)
                part.close()
            outputs.append(folder)
    return outputs


COMPRESS_LEVELS = {
    # level: (images above this dpi are downsampled, target dpi, JPEG quality)
    "normal": (160, 150, 70),   # good enough for reading on screen and printing
    "strong": (110, 96, 50),    # for e-mail / upload limits; blurry when printed
}


def compress_pdf(paths: list[Path], progress: Progress, level: str = "normal", **_) -> list[Path]:
    threshold, target, quality = COMPRESS_LEVELS[level]
    suffix = "_sikistirilmis" if level == "normal" else "_cok_sikistirilmis"
    outputs = []
    for p in paths:
        progress(T("Sıkıştırılıyor: {0}", p.name))
        before = p.stat().st_size
        with open_pdf(p) as doc:
            if hasattr(doc, "rewrite_images"):
                doc.rewrite_images(dpi_threshold=threshold, dpi_target=target, quality=quality)
            out = unique_path(p.parent, f"{p.stem}{suffix}", ".pdf")
            doc.save(out, garbage=4, deflate=True, deflate_images=True,
                     deflate_fonts=True, clean=True, use_objstms=1)
        after = out.stat().st_size
        if after >= before:
            # If there is no gain, don't leave a bigger file
            shutil.copy2(p, out)
            progress(T("ℹ {0} zaten optimize, küçültülemedi.", p.name))
        else:
            pct = 100 * (before - after) / before
            progress(T("ℹ {0}: {1} → {2} (%{3:.0f} küçüldü)", p.name, human_size(before), human_size(after), pct))
        outputs.append(out)
    return outputs


def rotate_pdf(paths: list[Path], progress: Progress, angle: int = 90, **_) -> list[Path]:
    outputs = []
    for p in paths:
        progress(T("Döndürülüyor: {0}", p.name))
        with open_pdf(p) as doc:
            for page in doc:
                page.set_rotation((page.rotation + angle) % 360)
            out = unique_path(p.parent, f"{p.stem}_dondurulmus_{angle % 360}", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        outputs.append(out)
    return outputs


def protect_pdf(paths: list[Path], progress: Progress, password: str = "", **_) -> list[Path]:
    if not password:
        raise ConversionError(T("Şifre girilmedi."))
    outputs = []
    for p in paths:
        progress(T("Şifreleniyor: {0}", p.name))
        with open_pdf(p) as doc:
            out = unique_path(p.parent, f"{p.stem}_sifreli", ".pdf")
            doc.save(out, garbage=3, deflate=True,
                     encryption=pymupdf.PDF_ENCRYPT_AES_256,
                     user_pw=password, owner_pw=password)
        outputs.append(out)
    return outputs


def unlock_pdf(paths: list[Path], progress: Progress, password: str = "", **_) -> list[Path]:
    outputs = []
    for p in paths:
        progress(T("Şifre kaldırılıyor: {0}", p.name))
        doc = pymupdf.open(p)
        try:
            if doc.needs_pass and not doc.authenticate(password):
                raise ConversionError(T("{0}: şifre yanlış.", p.name))
            out = unique_path(p.parent, f"{p.stem}_sifresiz", ".pdf")
            doc.save(out, garbage=3, deflate=True,
                     encryption=pymupdf.PDF_ENCRYPT_NONE)
        finally:
            doc.close()
        outputs.append(out)
    return outputs


def pdf_to_jpg(paths: list[Path], progress: Progress, dpi: int = 150, **_) -> list[Path]:
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            n = len(doc)
            width = len(str(n))
            folder = unique_dir(p.parent, f"{p.stem}_jpg")
            for i, page in enumerate(doc):
                progress(T("{0}: sayfa {1}/{2}", p.name, i + 1, n))
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                pix.save(folder / f"{p.stem}_{T('s')}{i + 1:0{width}d}.jpg", jpg_quality=90)
            outputs.append(folder)
    return outputs


def pdf_to_word(paths: list[Path], progress: Progress, mode: str = "auto", **_) -> list[Path]:
    """mode: "auto" (slides and two-column papers → exact layout, everything else flowing text) · "flow" (always flowing)."""
    try:
        from pdf2docx import Converter
    except ImportError:
        raise ConversionError(T("pdf2docx kurulu değil: pip install pdf2docx"))
    import logging
    logging.getLogger().setLevel(logging.ERROR)  # pdf2docx is very chatty
    outputs = []
    for p in paths:
        progress(T("Word'e çevriliyor: {0} (uzun sürebilir)", p.name))
        out = unique_path(p.parent, p.stem, ".docx")
        with open_pdf(p) as d:
            ink = _collect_ink(d)
            notes = _collect_notes(d)
        with tempfile.TemporaryDirectory(prefix="riffle_w_") as tmp:
            src = _prepare_pdf(p, Path(tmp) / "kaynak.pdf", progress)
            from . import fixed_layout, reflow
            kind = None
            if mode != "flow":
                with pymupdf.open(src) as probe:
                    if fixed_layout.looks_like_slides(probe):
                        kind = "sunum"
                    elif reflow.is_two_column(probe):
                        kind = T("iki sütunlu belge")
            if kind:
                # Slides and two-column papers: lines/pages shift in flowing text, use the exact layout
                progress(T("ℹ {0}: {1} algılandı, birebir düzen kullanıldı (yazılar yerinde düzenlenebilir). Akan metin için: 'PDF → Word (akan metin)'.", p.name, kind))
                fixed_layout.convert(src, out, progress)
                if ink:
                    progress(T("ℹ {0}: {1} el yazısı işaret (kalem/tik) Word'e aktarılmadı.", p.name, len(ink)))
                if notes:
                    progress(T("ℹ {0}: {1} PDF notu metinde yeri bulunamadığı için aktarılmadı.", p.name, len(notes)))
                outputs.append(out)
                continue
            with pymupdf.open(src) as d:
                info = reflow.analyze_pages(d)
                info.rules = [r for pg in d for r in reflow.find_rules(pg)]
                rescued = reflow.rescue_figures(d)
                if rescued:
                    src = Path(tmp) / "figurlu.pdf"
                    d.save(src)
                    progress(T("ℹ {0}: {1} figür resim olarak aktarıldı.", p.name, rescued))
            from .textfix import patched_text
            from . import parallel
            # 0.7: keeps a centred title and subtitle from being glued onto one line
            settings = {"line_break_width_ratio": 0.7, "page_margin_factor_top": 1.0}
            with pymupdf.open(src) as probe:
                n_pages = len(probe)
            if n_pages >= 30 and not parallel.in_worker() and parallel.workers_for(n_pages // 10) > 1 \
                    and _parallel_enabled():
                try:
                    fixes = parallel.word_parallel(src, out, n_pages, settings, Path(tmp), progress)
                except parallel.Cancelled:
                    raise
                except Exception:                   # process pool could not be created: fall back to sequential
                    fixes = None
            else:
                fixes = None
            if fixes is None:
                with patched_text(src) as fixes:
                    cv = Converter(str(src))
                    try:
                        cv.convert(str(out), **settings)
                    finally:
                        cv.close()
            if fixes["glyph"]:
                progress(T("ℹ {0}: {1} özel karakter (μ, α, ± …) kurtarıldı.", p.name, fixes['glyph']))
        _unclip_table_rows(out, info)
        if ink:
            try:
                placed = _add_ink(out, ink)
            except Exception:
                placed = 0
            if placed < len(ink):
                progress(T("ℹ {0}: {1} el yazısı işaret (kalem/tik) Word'e aktarılmadı.", p.name, len(ink) - placed))
        if notes:
            try:
                added = _add_comments(out, notes)
            except Exception:
                added = 0
            if added:
                progress(T("ℹ {0}: PDF'teki {1} not Word yorumu olarak eklendi.", p.name, added))
            if added < len(notes):
                progress(T("ℹ {0}: {1} PDF notu metinde yeri bulunamadığı için aktarılmadı.", p.name, len(notes) - added))
        outputs.append(out)
    return outputs


# ---------------------------------------------------------------- pen marks (Ink annotations)

def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t).lower()


def _collect_ink(doc) -> list[dict]:
    """Ink annotations (pen ticks, underlines, circles) as transparent PNGs, each with the
    text it sits on: the PDF line nearest to it and the first lines of that line's block
    (to find the Word paragraph and its top again after conversion)."""
    items = []
    for pno, page in enumerate(doc):
        inks = list(page.annots(types=[pymupdf.PDF_ANNOT_INK]))
        if not inks:
            continue
        lines = []
        for b in page.get_text("dict")["blocks"]:
            bl = [(pymupdf.Rect(l["bbox"]), "".join(s["text"] for s in l["spans"]))
                  for l in b.get("lines", [])]
            bl = [(r, t) for r, t in bl if t.strip()]
            for k, (r, t) in enumerate(bl):
                lines.append((r, t, bl[:k + 1]))
        for annot in inks:
            r = annot.rect
            try:
                png = annot.get_pixmap(dpi=200, alpha=True).tobytes("png")
            except Exception:
                continue
            cy = (r.y0 + r.y1) / 2
            near = min(lines, key=lambda L: abs((L[0].y0 + L[0].y1) / 2 - cy), default=None)
            if near and abs((near[0].y0 + near[0].y1) / 2 - cy) > 40:
                near = None
            items.append({"page": pno, "rect": pymupdf.Rect(r), "png": png,
                          "line": near[1] if near else "", "above": near[2] if near else []})
    return items


def _add_ink(docx: Path, items: list[dict]) -> int:
    """Puts the pen marks back into the Word file as pictures floating over the text (wrapNone:
    they push nothing). Vertically they hang on the paragraph that holds the marked line, so
    they move with it when the text reflows; horizontally they sit where they were on the page.
    Returns how many could be placed."""
    import zipfile
    from .fixed_layout import EMU
    if not items:
        return 0
    with zipfile.ZipFile(docx) as z:
        xml = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        types = z.read("[Content_Types].xml").decode("utf-8")
    body = xml.find("<w:body>")
    paras = [(m.start(), m.end(), m.group(0)) for m in
             re.finditer(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', xml[body:], re.S)]
    texts = [_norm("".join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', p))) for _, _, p in paras]
    inserts, media, placed, search_from = {}, [], 0, 0
    for n, it in enumerate(items):
        key = _norm(it["line"])[:40]
        if len(key) < 6:
            continue
        # paragraphs come in page order: search forward from the last hit first
        order = list(range(search_from, len(paras))) + list(range(0, search_from))
        hit = next((i for i in order if key in texts[i]), None)
        if hit is None:
            continue
        search_from = hit
        # top of that paragraph in the PDF: the earliest line of the block that starts it
        top = None
        for r, t in it["above"]:
            if len(_norm(t)) >= 4 and texts[hit].startswith(_norm(t)[:15]):
                top = r.y0
                break
        if top is None:
            continue
        # 'relative to paragraph' counts from above the paragraph's space-before
        sb = re.search(r'<w:spacing\b[^>]*w:before="(\d+)"', paras[hit][2])
        top -= int(sb.group(1)) / 20 if sb else 0
        r = it["rect"]
        rid = f"rIdInk{n + 1}"
        media.append((rid, it["png"]))
        did = 9000 + n
        run = (f'<w:r><w:drawing><wp:anchor distT="0" distB="0" distL="0" distR="0" simplePos="0" '
               f'relativeHeight="{did}" behindDoc="0" locked="0" layoutInCell="0" allowOverlap="1">'
               f'<wp:simplePos x="0" y="0"/>'
               f'<wp:positionH relativeFrom="page"><wp:posOffset>{round(r.x0 * EMU)}</wp:posOffset></wp:positionH>'
               f'<wp:positionV relativeFrom="paragraph"><wp:posOffset>{round((r.y0 - top) * EMU)}</wp:posOffset></wp:positionV>'
               f'<wp:extent cx="{round(r.width * EMU)}" cy="{round(r.height * EMU)}"/>'
               f'<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>'
               f'<wp:docPr id="{did}" name="Pen {n + 1}"/><wp:cNvGraphicFramePr/>'
               '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
               '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
               '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
               f'<pic:nvPicPr><pic:cNvPr id="{did}" name="Pen {n + 1}"/><pic:cNvPicPr/></pic:nvPicPr>'
               f'<pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
               f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{round(r.width * EMU)}" cy="{round(r.height * EMU)}"/>'
               '</a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic>'
               '</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>')
        inserts.setdefault(hit, []).append(run)
        placed += 1
    if not placed:
        return 0
    out, pos = [], body
    for i in sorted(inserts):
        s0, _, p = paras[i]
        s0 += body
        m = re.match(r'<w:p(?: [^>]*)?>(?:<w:pPr>.*?</w:pPr>)?', p, re.S)
        cut = s0 + m.end()
        out.append(xml[pos:cut])
        out.append("".join(inserts[i]))
        pos = cut
    xml = xml[:body] + "".join(out) + xml[pos:]
    if 'xmlns:r=' not in xml[:3000]:
        return 0
    rels = rels.replace("</Relationships>", "".join(
        f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
        f'relationships/image" Target="media/{rid}.png"/>' for rid, _ in media) + "</Relationships>")
    if 'Extension="png"' not in types:
        types = types.replace("</Types>", '<Default Extension="png" ContentType="image/png"/></Types>')
    tmp = docx.with_suffix(".ink")
    with zipfile.ZipFile(docx) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = xml.encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                data = rels.encode("utf-8")
            elif item.filename == "[Content_Types].xml":
                data = types.encode("utf-8")
            zout.writestr(item, data)
        for rid, png in media:
            zout.writestr(f"word/media/{rid}.png", png)
    os.replace(tmp, docx)
    return placed


# ---------------------------------------------------------------- PDF notes → Word comments

def _pdf_date(d: str) -> str:
    """'D:20261120153000+03'00'' → '2026-11-20T15:30:00Z' (Word wants ISO; time zone dropped)."""
    m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", d or "")
    if not m:
        return ""
    y, mo, da, h, mi, se = (g or "00" for g in m.groups())
    return f"{y}-{mo}-{da}T{h}:{mi}:{se}Z"


def _collect_notes(doc) -> list[dict]:
    """Notes written on the PDF (the text of highlights, underlines, sticky notes, pen marks):
    their text, author, date and the document text they refer to: the marked words for
    highlights/underlines, otherwise the nearest line. Replies are added to their parent."""
    notes, by_xref = [], {}
    markup = (pymupdf.PDF_ANNOT_HIGHLIGHT, pymupdf.PDF_ANNOT_UNDERLINE, pymupdf.PDF_ANNOT_STRIKE_OUT,
              pymupdf.PDF_ANNOT_SQUIGGLY)    # here, not at import: the installer loads this module without pymupdf
    for pno, page in enumerate(doc):
        words = page.get_text("words")
        lines = []
        for b in page.get_text("dict")["blocks"]:
            for l in b.get("lines", []):
                t = "".join(s["text"] for s in l["spans"]).strip()
                if t:
                    lines.append((pymupdf.Rect(l["bbox"]), t))
        for a in page.annots():
            info = a.info
            text = (info.get("content") or "").strip()
            if not text or a.type[0] in (pymupdf.PDF_ANNOT_POPUP, pymupdf.PDF_ANNOT_FREE_TEXT):
                continue
            author = (info.get("title") or "").strip()
            if a.irt_xref and a.irt_xref in by_xref:          # a reply: goes under its parent note
                by_xref[a.irt_xref]["replies"].append((author, text))
                continue
            target, context = "", ("", "")
            if a.type[0] in markup and a.vertices:
                v = a.vertices
                quads = [pymupdf.Quad(v[k:k + 4]).rect for k in range(0, len(v) - 3, 4)]
                picked = [w for w in words
                          if any(pymupdf.Rect(w[:4]).intersect(q).get_area() > 0.5 * pymupdf.Rect(w[:4]).get_area()
                                 for q in quads)]
                target = " ".join(w[4] for w in picked)
                if picked:
                    # neighbours on the same line: a short marked word ('Bekçi') is found where it
                    # was marked, not at its first appearance on the page
                    first, last = picked[0], picked[-1]
                    same = lambda w, ref: w[5] == ref[5] and w[6] == ref[6]
                    pre = [w[4] for w in words if same(w, first) and w[7] < first[7]][-3:]
                    post = [w[4] for w in words if same(w, last) and w[7] > last[7]][:3]
                    context = (" ".join(pre), " ".join(post))
            if len(_norm(target)) < 2 and lines:
                cy = (a.rect.y0 + a.rect.y1) / 2
                r, t = min(lines, key=lambda L: abs((L[0].y0 + L[0].y1) / 2 - cy))
                target = t if abs((r.y0 + r.y1) / 2 - cy) < 40 else ""
            note = {"page": pno, "y": a.rect.y0, "x": a.rect.x0, "text": text, "author": author,
                    "date": _pdf_date(info.get("modDate") or info.get("creationDate")),
                    "target": target, "context": context, "replies": []}
            by_xref[a.xref] = note
            notes.append(note)
    notes.sort(key=lambda n: (n["page"], round(n["y"]), n["x"]))
    return notes


def _comment_paras(text: str) -> str:
    from xml.sax.saxutils import escape
    return "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r></w:p>'
                   for line in text.splitlines() or [""])


def _add_comments(docx: Path, notes: list[dict]) -> int:
    """Turns the PDF's notes into real Word comments (balloons in the margin, can be answered
    and resolved), each spanning the text it was written on. Returns how many were placed."""
    import zipfile
    from xml.sax.saxutils import quoteattr
    if not notes:
        return 0
    with zipfile.ZipFile(docx) as z:
        names = set(z.namelist())
        if "word/comments.xml" in names:
            return 0
        xml = z.read("word/document.xml").decode("utf-8")
        rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        types = z.read("[Content_Types].xml").decode("utf-8")
    body = xml.find("<w:body>")
    runs = [(m.start(), m.end()) for m in re.finditer(r'<w:r>(?:(?!</w:r>).)*?</w:r>', xml[body:], re.S)]
    runs = [(s + body, e + body) for s, e in runs]
    # normalized text of the whole body, and for each character the run it came from
    chars, owner = [], []
    for k, (s, e) in enumerate(runs):
        t = _norm("".join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', xml[s:e])))
        t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        chars.append(t)
        owner += [k] * len(t)
    flat = "".join(chars)
    starts, ends, comments, cursor = {}, {}, [], 0
    for note in notes:
        key = _norm(note["target"])
        if len(key) < 2:
            continue
        pre, post = (_norm(t) for t in note.get("context", ("", "")))
        i = -1
        for a_, b_ in ((pre, post), (pre, ""), ("", post)):   # with its neighbours first
            if a_ or b_:
                j = flat.find(a_ + key + b_, cursor)
                j = j if j >= 0 else flat.find(a_ + key + b_)
                if j >= 0:
                    i = j + len(a_)
                    break
        if i < 0:
            i = flat.find(key, cursor)
        if i < 0:
            i = flat.find(key)
        if i < 0 and len(key) > 30:                       # long marked passage: its start is enough
            i = flat.find(key[:30], cursor)
            key = key[:30]
        if i < 0:
            continue
        cursor = i
        cid = len(comments)
        a, b = owner[i], owner[i + len(key) - 1]
        starts.setdefault(a, []).append(cid)
        ends.setdefault(b, []).append(cid)
        text = note["text"] + "".join(f"\n— {who or 'PDF'}: {t}" for who, t in note["replies"])
        author = note["author"] or "PDF"
        initials = "".join(w[0] for w in author.split()[:3]).upper() or "P"
        date = f' w:date="{note["date"]}"' if note["date"] else ""
        comments.append(f'<w:comment w:id="{cid}" w:author={quoteattr(author)} w:initials={quoteattr(initials)}{date}>'
                        f'{_comment_paras(text)}</w:comment>')
    if not comments:
        return 0
    out, pos = [], 0
    for k in sorted(set(starts) | set(ends)):
        s, e = runs[k]
        if k in starts:
            out.append(xml[pos:s])
            out.append("".join(f'<w:commentRangeStart w:id="{c}"/>' for c in starts[k]))
            pos = s
        if k in ends:
            out.append(xml[pos:e])
            out.append("".join(f'<w:commentRangeEnd w:id="{c}"/><w:r><w:commentReference w:id="{c}"/></w:r>'
                               for c in ends[k]))
            pos = e
    xml = "".join(out) + xml[pos:]
    part = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
            '<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            + "".join(comments) + '</w:comments>')
    rels = rels.replace("</Relationships>",
                        '<Relationship Id="rIdNotes" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        'relationships/comments" Target="comments.xml"/></Relationships>')
    types = types.replace("</Types>", '<Override PartName="/word/comments.xml" ContentType="application/'
                          'vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/></Types>')
    tmp = docx.with_suffix(".notes")
    with zipfile.ZipFile(docx) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = xml.encode("utf-8")
            elif item.filename == "word/_rels/document.xml.rels":
                data = rels.encode("utf-8")
            elif item.filename == "[Content_Types].xml":
                data = types.encode("utf-8")
            zout.writestr(item, data)
        zout.writestr("word/comments.xml", part.encode("utf-8"))
    os.replace(tmp, docx)
    return len(comments)


def _parallel_enabled() -> bool:
    try:
        from .settings import load
        return bool(load()["parallel"])
    except Exception:
        return True


def _run_cancellable(cmd, timeout: float, flags: int = 0):
    """Like subprocess.run, but stops the external program (LibreOffice, Ghostscript) when Cancel is pressed."""
    from .parallel import CANCEL, Cancelled
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, creationflags=flags)
    t0 = time.time()
    while True:
        try:
            out, err = p.communicate(timeout=0.25)
            return subprocess.CompletedProcess(cmd, p.returncode, out, err)
        except subprocess.TimeoutExpired:
            if CANCEL.is_set():
                p.kill()
                p.communicate()
                raise Cancelled()
            if time.time() - t0 > timeout:
                p.kill()
                out, err = p.communicate()
                return subprocess.CompletedProcess(cmd, -9, out, err)


def _prepare_pdf(src: Path, dst: Path, progress: Progress) -> Path:
    """Prepares the PDF before handing it to pdf2docx. Returns the original if nothing changes.

    - Removes handwritten (Ink) annotations: pdf2docx makes them free shapes that break the
      text layout. pdf_to_word puts them back afterwards as floating pictures (_add_ink).
      Highlights are kept.
    - Turns pages without text (scanned, handwritten, drawings) into page images:
      pdf2docx skips vector lines, otherwise the page comes out empty or incomplete.
    """
    doc = open_pdf(src)
    try:
        ink = 0
        for page in doc:
            for annot in list(page.annots()):
                if annot.type[0] == pymupdf.PDF_ANNOT_INK:
                    page.delete_annot(annot)
                    ink += 1
        textless = {i for i, page in enumerate(doc) if len(page.get_text().strip()) < 10}
        # Cropped pages (cut from a larger canvas, e.g. magazine PDFs): pdf2docx
        # ignores the crop offset, images end up 'outside the page' and get dropped.
        cropped = {i for i, page in enumerate(doc)
                   if page.cropbox != page.mediabox or page.mediabox.x0 or page.mediabox.y0}
        if not ink and not textless and not cropped:
            return src
        # Pages without text: OCR if Tesseract is available → editable text if it is mostly text
        ocr_words, ocr_used = {}, set()
        if textless:
            from . import ocr
            try:
                ocr_words = ocr.ocr_pages(doc, sorted(textless), progress)
                ocr_used = {i for i, w in ocr_words.items() if ocr.is_text_page(w)}
                # straighten a skewed printed scan and read it again: lines and table rules should
                # sit on the axes (skewed lines produced tilted tables split into thin columns in Word).
                # Text pages only: skew measurement is unreliable on handwriting.
                from .docops import _rotate_content, skew_angle
                skewed = []
                for i in sorted(ocr_used):
                    pg = doc[i]
                    if pg.rotation:
                        continue
                    ang = skew_angle(pg)
                    if 0.15 <= abs(ang) <= 8:
                        _rotate_content(doc, pg, ang)
                        skewed.append(i)
                if skewed:
                    ocr_words.update(ocr.ocr_pages(doc, skewed, progress))
            except ocr.OcrUnavailable as e:
                progress(T("ℹ OCR yapılamadı, taranmış sayfalar resim olarak aktarılacak. {0}", str(e).splitlines()[0]))
        if textless or cropped:
            out = pymupdf.open()
            for i, page in enumerate(doc):
                if i in ocr_used:
                    from . import ocr
                    ocr.text_page(out, page, ocr_words[i])
                elif i in textless:
                    pix = page.get_pixmap(dpi=150, alpha=False)
                    new = out.new_page(width=page.rect.width, height=page.rect.height)
                    new.insert_image(new.rect, stream=pix.tobytes("jpeg", jpg_quality=82))
                elif i in cropped:
                    # place the page, as cropped, on a new page with a zero origin
                    new = out.new_page(width=page.rect.width, height=page.rect.height)
                    new.show_pdf_page(new.rect, doc, i)
                else:
                    out.insert_pdf(doc, from_page=i, to_page=i, annots=True)
            out.save(dst)
            out.close()
            if ocr_used:
                progress(T("ℹ {0}: {1} taranmış sayfa OCR ile metne çevrildi (resim/imza gibi öğeler bu sayfalarda aktarılmaz).", src.name, len(ocr_used)))
            if textless - ocr_used:
                progress(T("ℹ {0}: {1} sayfada metin yok, resim olarak aktarıldı.", src.name, len(textless - ocr_used)))
        else:
            doc.save(dst)
        return dst
    finally:
        doc.close()


_SLACK = 1440  # twips = 1 inch (2.5 cm) margin
_CELL_BEFORE = 40  # twips: cap on the top spacing inside cells of unlocked rows
_MIN_CELL = 1000  # twips ≈ 1.8 cm: rows with cells narrower than this keep their fixed height


def _cap_cell_before(tc: str, row_h: int) -> str:
    import re
    if "<w:tbl>" in tc:
        return tc
    # an empty cell (merged column) takes the document default: 10 pt 'after' spacing
    # swells single-line rows by ~4 pt and pushes the table onto the next page
    tc = tc.replace("<w:p/>", '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="200" w:lineRule="exact"/>'
                              '<w:rPr><w:sz w:val="2"/></w:rPr></w:pPr></w:p>')
    content = 0
    for para in re.findall(r'<w:p[ >].*?</w:p>', tc, flags=re.S):
        line = re.search(r'w:line="(\d+)"', para)
        content += (int(line.group(1)) if line else 276) * (para.count("<w:br") + 1)
    room = max(_CELL_BEFORE, int(row_h - content * 1.15 - _CELL_BEFORE)) if row_h else _CELL_BEFORE
    first = [True]

    def cap(b):
        v = int(b.group(1))
        if first[0]:                       # only the first paragraph's top spacing is the cell padding
            first[0] = False
            return f'w:before="{min(v, room)}"'
        return f'w:before="{min(v, _CELL_BEFORE)}"'
    return re.sub(r'w:before="(\d+)"', cap, tc)


def _fix_layout_xml(xml: str, info=None) -> str:
    import re
    # 1) Table rows 'exact height' → 'at least': the last line is not cut if the text grows.
    #    ONLY in rows where all cells are wide: narrow cells on slides
    #    (letters stacked) make the page dozens of lines longer when unlocked.
    def unclip_row(m: re.Match) -> str:
        tr = m.group(0)
        if "<w:tbl>" in tr:  # nested table: leave alone
            return tr
        widths = [int(w) for w in re.findall(r'<w:tcW[^>]*w:w="(\d+)"', tr)]
        if widths and min(widths) >= _MIN_CELL and 'w:hRule="exact"' in tr:
            tr = tr.replace('<w:trHeight w:hRule="exact"', '<w:trHeight w:hRule="atLeast"')
            # pdf2docx positions text in a cell with 'space before'; once the row is
            # unlocked this spacing swells the row and pushes the page over. The row's
            # minimum height is kept anyway, so shrink the spacing.
            # But removing it entirely sticks the text to the top of the cell (the original
            # has cell padding): keep as much as the lines in the cell leave room for.
            h = int((re.search(r'<w:trHeight[^>]*w:val="(\d+)"', tr) or [0, 0])[1])
            tr = re.sub(r'<w:tc>.*?</w:tc>', lambda c: _cap_cell_before(c.group(0), h), tr, flags=re.S)
        return tr
    xml = re.sub(r'<w:tr[ >].*?</w:tr>', unclip_row, xml, flags=re.S)

    # 1b) pdf2docx writes the table grid (gridCol) with EQUAL widths for all columns; the real
    #     widths are in the cells (tcW). Word/LibreOffice use the grid in fixed-layout tables,
    #     so a wide column gets narrow and the text wraps down.
    xml = re.sub(r'<w:tbl>.*?</w:tbl>', _fix_table_grid, xml, flags=re.S)

    # 1c) Values that violate the schema: decimal measures ("6.0", "14.000000000000057")
    #     and colours with a '#' prefix. Word may report "unreadable content" for these.
    xml = re.sub(r'(w:(?:w|sz|space|val)=")(-?\d+)\.\d+"', r'\1\2"', xml)
    xml = re.sub(r'(w:(?:color|fill)=")#([0-9A-Fa-f]{6})"', r'\1\2"', xml)

    # 1c2) Let text flow on pages that end full, remove headers/footers (see reflow.py)
    if info is not None:
        from . import reflow
        xml = reflow.reflow_sections(xml, info)

    # 1d) Multi-column pages: pdf2docx makes the left and right columns separate sections
    #     with a 'next column' section break. LibreOffice treats it as a page break
    #     (every PDF page becomes 2 pages). Make it one section + a column break.
    xml = _merge_column_sections(xml)

    # 2) pdf2docx makes every PDF page a separate section and pushes the item at the end
    #    of the page (usually the page number) down with a large 'space before'.
    #    When the lines above grow, this item moves to a new page on its own.
    #    Shorten the spacing of the paragraph right before the section break by 1 inch.
    def shrink(m: re.Match) -> str:
        before = int(m.group(2))
        if before >= 2 * _SLACK:
            return f'{m.group(1)}{before - _SLACK}"'
        return m.group(0)

    #    Middle sections end with '<w:p><w:pPr><w:sectPr>', the LAST section with
    #    '</w:p><w:sectPr' at the end of the body; both must be handled.
    sect = re.compile(r'<w:p><w:pPr><w:sectPr>|(?<=</w:p>)<w:sectPr[ >]')
    out, pos = [], 0
    for m in sect.finditer(xml):
        start = max(xml.rfind("<w:p>", 0, m.start()), xml.rfind("<w:p ", 0, m.start()))
        if start != -1 and start >= pos:
            out.append(xml[pos:start])
            out.append(re.sub(r'(w:before=")(\d+)"', shrink, xml[start:m.start()], count=1))
            pos = m.start()
    out.append(xml[pos:])
    xml = "".join(out)

    # 3) The section break paragraph itself should not take up a line
    xml = xml.replace('<w:p><w:pPr><w:sectPr>',
                      '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/><w:sectPr>')

    # 3b) PDFs without spaces (old Distiller etc.): words are separate runs with no
    #     space character between them → 'Electricaldischargemachining' in Word.
    xml = _restore_spaces(xml)
    # invisible mark textfix leaves in place of a line-end hyphen: no longer needed
    xml = xml.replace("\u2060", "")
    xml = re.sub(r"<w:t( xml:space=\"preserve\")?></w:t>", "<w:t/>", xml)

    # 4) Bullet/number marks: insert a tab, split merged items
    xml = re.sub(r'<w:p(?: [^>]*)?>.*?</w:p>', lambda m: _fix_list_para(m.group(0)), xml, flags=re.S)
    # 5) Lost horizontal separator lines → paragraph borders
    if info is not None and getattr(info, "rules", None):
        xml = _apply_rules(xml, info.rules)
    # 6) Pages turned into a single image (handwriting, drawings): pin them to the page corner
    xml = _pin_page_images(xml)
    # 7) a little room on the right so lines that filled the PDF's width don't wrap early
    xml = _wrap_slack(xml)
    # 8) headings stay on the page of the paragraph they introduce
    xml = _keep_headings_with_next(xml)
    return xml


def _body_paragraphs(xml: str, fn) -> str:
    """Applies fn to every body paragraph outside tables and text boxes (fn gets the paragraph
    and the text that follows it in its segment, returns the new paragraph)."""
    body_start = xml.find("<w:body>")
    if body_start < 0:
        return xml
    out, pos, depth = [], body_start, 0
    para = re.compile(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', re.S)

    def fix(seg: str) -> str:
        return para.sub(lambda m: fn(m.group(0), seg[m.end():m.end() + 200]), seg)

    for m in _SKIP_SPANS.finditer(xml, body_start):
        if depth == 0:
            out.append(fix(xml[pos:m.start()]))
            pos = m.start()
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            out.append(xml[pos:m.end()])
            pos = m.end()
    out.append(fix(xml[pos:]) if depth == 0 else xml[pos:])
    return xml[:body_start] + "".join(out)


def _keep_headings_with_next(xml: str) -> str:
    """pdf2docx headings are plain paragraphs: without Word's 'keep with next' a heading whose
    page ran a line longer than in the PDF ends up alone at the bottom of that page.
    Heading = a short paragraph, all of it bold or clearly larger than the body text,
    followed by another paragraph or a table."""
    sizes = Counter()
    for sz, txt in re.findall(r'<w:sz w:val="(\d+)"/>.*?<w:t[^>]*>([^<]*)</w:t>', xml, re.S):
        sizes[int(sz)] += len(txt)
    if not sizes:
        return xml
    body = sizes.most_common(1)[0][0]

    def fn(p: str, after: str) -> str:
        if "<w:keepNext" in p or "<w:sectPr" in p or not re.match(r'\s*<w:(?:p[ >]|tbl>)', after):
            return p
        runs = re.findall(r'<w:r>(.*?)</w:r>', p, re.S)
        texts = [(r, "".join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', r))) for r in runs]
        texts = [(r, t) for r, t in texts if t.strip()]
        text = "".join(t for _, t in texts).strip()
        if not text or len(text) > 120 or text.endswith((".", ":", ";", ",")):
            return p
        bold = all(re.search(r'<w:b/>|<w:b w:val="(?:1|true|on)"/>', r) for r, _ in texts)
        szs = [int(m.group(1)) for r, _ in texts for m in [re.search(r'<w:sz w:val="(\d+)"/>', r)] if m]
        larger = len(szs) == len(texts) and min(szs) >= body + 2      # ≥ 1 pt larger
        if not (bold or larger):
            return p
        if "<w:pPr>" not in p:
            return re.sub(r'(<w:p(?: [^>]*)?>)', r'\1<w:pPr><w:keepNext/></w:pPr>', p, count=1)
        k = p.find("<w:pPr>") + len("<w:pPr>")
        st = re.match(r'<w:pStyle [^>]*/>', p[k:])
        if st:
            k += st.end()
        return p[:k] + "<w:keepNext/>" + p[k:]

    return _body_paragraphs(xml, fn)


_WRAP_SLACK = 40   # twips (2 pt)
_SKIP_SPANS = re.compile(r'<w:tbl>|</w:tbl>|<w:txbxContent>|</w:txbxContent>')


def _wrap_slack(xml: str) -> str:
    """pdf2docx sets the text width to the longest line exactly. Rounding in Word/LibreOffice
    (and an indent a point off) then pushes the last word of such a line down, every following
    line shifts, and a heading can end up at the bottom of the previous page. Lower the right
    indent of body paragraphs by 2 pt (a negative indent is valid; it reaches into the margin).
    Paragraphs in tables and text boxes keep their exact width (fixed cells / frames)."""
    def fn(p: str, _after: str) -> str:
        if not re.search(r'<w:jc w:val="(?:left|both)"/>', p) and "<w:jc " in p:
            return p          # centred / right-aligned: width doesn't wrap anything
        ind = re.search(r'<w:ind\b[^>]*/>', p)
        if not ind:
            return p
        tag = ind.group(0)
        r = re.search(r'w:right="(-?\d+)"', tag)
        new = (tag.replace(r.group(0), f'w:right="{int(r.group(1)) - _WRAP_SLACK}"') if r
               else tag.replace("/>", f' w:right="{-_WRAP_SLACK}"/>'))
        return p.replace(tag, new, 1)

    return _body_paragraphs(xml, fn)


def _body_paragraphs(xml: str, fn) -> str:
    """Applies fn to every body paragraph outside tables and text boxes (fn gets the paragraph
    and the text that follows it in its segment, returns the new paragraph)."""
    body_start = xml.find("<w:body>")
    if body_start < 0:
        return xml
    out, pos, depth = [], body_start, 0
    para = re.compile(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', re.S)

    def fix(seg: str) -> str:
        return para.sub(lambda m: fn(m.group(0), seg[m.end():m.end() + 200]), seg)

    for m in _SKIP_SPANS.finditer(xml, body_start):
        if depth == 0:
            out.append(fix(xml[pos:m.start()]))
            pos = m.start()
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            out.append(xml[pos:m.end()])
            pos = m.end()
    out.append(fix(xml[pos:]) if depth == 0 else xml[pos:])
    return xml[:body_start] + "".join(out)


def _keep_headings_with_next(xml: str) -> str:
    """pdf2docx headings are plain paragraphs: without Word's 'keep with next' a heading whose
    page ran a line longer than in the PDF ends up alone at the bottom of that page.
    Heading = a short paragraph, all of it bold or clearly larger than the body text,
    followed by another paragraph or a table."""
    sizes = Counter()
    for sz, txt in re.findall(r'<w:sz w:val="(\d+)"/>.*?<w:t[^>]*>([^<]*)</w:t>', xml, re.S):
        sizes[int(sz)] += len(txt)
    if not sizes:
        return xml
    body = sizes.most_common(1)[0][0]

    def fn(p: str, after: str) -> str:
        if "<w:keepNext" in p or "<w:sectPr" in p or not re.match(r'\s*<w:(?:p[ >]|tbl>)', after):
            return p
        runs = re.findall(r'<w:r>(.*?)</w:r>', p, re.S)
        texts = [(r, "".join(re.findall(r'<w:t[^>]*>([^<]*)</w:t>', r))) for r in runs]
        texts = [(r, t) for r, t in texts if t.strip()]
        text = "".join(t for _, t in texts).strip()
        if not text or len(text) > 120 or text.endswith((".", ":", ";", ",")):
            return p
        bold = all(re.search(r'<w:b/>|<w:b w:val="(?:1|true|on)"/>', r) for r, _ in texts)
        szs = [int(m.group(1)) for r, _ in texts for m in [re.search(r'<w:sz w:val="(\d+)"/>', r)] if m]
        larger = len(szs) == len(texts) and min(szs) >= body + 2      # ≥ 1 pt larger
        if not (bold or larger):
            return p
        if "<w:pPr>" not in p:
            return re.sub(r'(<w:p(?: [^>]*)?>)', r'\1<w:pPr><w:keepNext/></w:pPr>', p, count=1)
        k = p.find("<w:pPr>") + len("<w:pPr>")
        st = re.match(r'<w:pStyle [^>]*/>', p[k:])
        if st:
            k += st.end()
        return p[:k] + "<w:keepNext/>" + p[k:]

    return _body_paragraphs(xml, fn)


_WRAP_SLACK = 40   # twips (2 pt)
_SKIP_SPANS = re.compile(r'<w:tbl>|</w:tbl>|<w:txbxContent>|</w:txbxContent>')


def _wrap_slack(xml: str) -> str:
    """pdf2docx sets the text width to the longest line exactly. Rounding in Word/LibreOffice
    (and an indent a point off) then pushes the last word of such a line down, every following
    line shifts, and a heading can end up at the bottom of the previous page. Lower the right
    indent of body paragraphs by 2 pt (a negative indent is valid; it reaches into the margin).
    Paragraphs in tables and text boxes keep their exact width (fixed cells / frames)."""
    body_start = xml.find("<w:body>")
    if body_start < 0:
        return xml
    out, pos, depth = [], body_start, 0

    def fix(seg: str) -> str:
        def one(m):
            p = m.group(0)
            if not re.search(r'<w:jc w:val="(?:left|both)"/>', p) and "<w:jc " in p:
                return p          # centred / right-aligned: width doesn't wrap anything
            ind = re.search(r'<w:ind\b[^>]*/>', p)
            if ind:
                tag = ind.group(0)
                r = re.search(r'w:right="(-?\d+)"', tag)
                new = (tag.replace(r.group(0), f'w:right="{int(r.group(1)) - _WRAP_SLACK}"') if r
                       else tag.replace("/>", f' w:right="{-_WRAP_SLACK}"/>'))
                return p.replace(tag, new, 1)
            return p
        return re.sub(r'<w:p[ >](?:(?!<w:p[ >]).)*?</w:p>', one, seg, flags=re.S)

    for m in _SKIP_SPANS.finditer(xml, body_start):
        if depth == 0:
            out.append(fix(xml[pos:m.start()]))
            pos = m.start()
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            out.append(xml[pos:m.end()])
            pos = m.end()
    out.append(fix(xml[pos:]) if depth == 0 else xml[pos:])
    return xml[:body_start] + "".join(out)


_FULL_PAGE_PARA = re.compile(
    r'<w:p>(?:(?!<w:p[ >]|</w:p>).)*?<wp:inline[^>]*><wp:extent cx="(\d+)" cy="(\d+)"/>'
    r'(?:(?!<w:p[ >]|</w:p>).)*?</wp:inline>(?:(?!<w:p[ >]|</w:p>).)*?</w:p>', re.S)
_SPACER_PARA = ('<w:p><w:pPr><w:pageBreakBefore/><w:spacing w:before="0" w:after="0" w:line="20" '
                'w:lineRule="exact"/><w:rPr><w:sz w:val="2"/><w:szCs w:val="2"/></w:rPr></w:pPr></w:p>')


def _add_page_break_before(para: str) -> str:
    """Adds <w:pageBreakBefore/> to a paragraph, in schema order (after pStyle/keepNext/keepLines)."""
    ppr_end = para.find("</w:pPr>")
    if ppr_end != -1 and "<w:pageBreakBefore" in para[:ppr_end]:
        return para
    m = re.match(r'<w:p(?: [^>]*)?>', para)
    head, rest = para[:m.end()], para[m.end():]
    if not rest.startswith("<w:pPr>"):
        return f"{head}<w:pPr><w:pageBreakBefore/></w:pPr>{rest}"
    pos = len("<w:pPr>")
    for tag in ("pStyle", "keepNext", "keepLines"):
        t = re.match(rf'<w:{tag}\b[^>]*/>', rest[pos:])
        if t:
            pos += t.end()
    return f"{head}{rest[:pos]}<w:pageBreakBefore/>{rest[pos:]}"


def _pin_page_images(xml: str) -> str:
    """pdf2docx puts a page that became an image (see _prepare_pdf) inline, as large as the page,
    in a section with zero margins. LibreOffice shifts such an image right (~9 pt) and pushes its
    edge off the page. Anchor it to the page corner behind the text instead, and keep each image
    on its own page with explicit page breaks (an anchored image no longer fills the page)."""
    sizes = {(int(w), int(h)) for w, h in re.findall(r'<w:pgSz w:w="(\d+)" w:h="(\d+)"', xml)}
    if not sizes:
        return xml

    def full_page(cx: int, cy: int) -> bool:
        w, h = cx / 635, cy / 635  # EMU → twips
        return any(abs(w - pw) <= 0.015 * pw and abs(h - ph) <= 0.015 * ph for pw, ph in sizes)

    hits = [m for m in _FULL_PAGE_PARA.finditer(xml)
            if full_page(int(m[1]), int(m[2])) and "<w:t" not in m[0].replace("<w:tab", "")]
    for m in reversed(hits):
        para = m[0]
        # keep the inline element's own attributes: pdf2docx declares the a:/pic: namespaces there
        para = re.sub(r'<wp:inline((?: [^>]*)?)>(<wp:extent [^>]*/>)(<wp:effectExtent [^>]*/>)?',
                      lambda i: '<wp:anchor' + re.sub(r' dist[TBLR]="[^"]*"', '', i[1]) +
                      ' distT="0" distB="0" distL="0" distR="0" simplePos="0" relativeHeight="1" '
                      'behindDoc="1" locked="0" layoutInCell="1" allowOverlap="1"><wp:simplePos x="0" y="0"/>'
                      '<wp:positionH relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionH>'
                      '<wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV>'
                      + i[2] + '<wp:effectExtent l="0" t="0" r="0" b="0"/><wp:wrapNone/>', para, count=1)
        para = para.replace("</wp:inline>", "</wp:anchor>", 1)
        # the now empty paragraph should take (almost) no room
        para = re.sub(r'<w:spacing [^>]*/>', '<w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/>',
                      para, count=1)
        ends_section = "<w:sectPr" in para
        # page break before, unless this is the first thing on a page anyway
        before = xml[:m.start()]
        prev_start = max(before.rfind("<w:p>"), before.rfind("<w:p "))
        first = before.rstrip().endswith("<w:body>")
        after_section = before.endswith("</w:p>") and prev_start != -1 and "<w:sectPr" in before[prev_start:]
        if not first and not after_section:
            para = _add_page_break_before(para)
        # whatever follows must start on the next page
        after = xml[m.end():]
        if ends_section:
            after = _break_after_section(after, para)
        else:
            after = _break_next(after)
        xml = before + para + after
    return xml


def _break_next(after: str) -> str:
    """Makes the body element at the start of 'after' begin on a new page."""
    if after.startswith("<w:tbl>"):
        return _SPACER_PARA + after
    if not re.match(r'<w:p[ >]', after) or _FULL_PAGE_PARA.match(after):
        return after  # end of the body, or another page image (gets its own break)
    end = after.find("</w:p>") + len("</w:p>")
    para = after[:end]
    if "<w:sectPr" in para and "<w:t" not in para.replace("<w:tab", "") and "<w:drawing" not in para:
        # empty section-break paragraph: it may stay on the image page, the break goes after it
        return para + _break_after_section(after[end:], para)
    return _add_page_break_before(para) + after[end:]


def _break_after_section(after: str, section_para: str) -> str:
    """A section ended on an image page: the next section starts a new page by itself,
    unless it is 'continuous' (its type sits in the NEXT sectPr)."""
    nxt = re.search(r'<w:sectPr[ >].*?</w:sectPr>', after, re.S)
    if nxt and 'w:type w:val="continuous"' in nxt[0]:
        return _break_next(after)
    return after


_PPR_AFTER_BDR = ("shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct",
                  "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind",
                  "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
                  "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
                  "pPrChange")


def _apply_rules(xml: str, rules) -> str:
    """Adds each line as a bottom/top border to the paragraph whose text matches (in document order)."""
    from .reflow import _norm_text
    # a border on a paragraph in a table cell stops at half the cell: skip tables
    tbl = [(m.start(), m.end()) for m in re.finditer(r"<w:tbl>.*?</w:tbl>", xml, re.S)]
    paras = [m for m in re.finditer(r"<w:p(?: [^>]*)?>.*?</w:p>", xml, re.S)
             if not any(a <= m.start() < b for a, b in tbl)]
    norm = [_norm_text(re.sub(r"<[^>]+>", "", m.group(0))) for m in paras]
    edits: dict[int, list] = {}
    used: set = set()
    start = 0
    for r in rules:
        key = r.text[-30:] if r.side == "bottom" else r.text[:30]
        if len(key) < 3:
            continue
        order = list(range(start, len(paras))) + list(range(0, start))   # order may break in columns
        ok = lambda i: key in norm[i] and (i, r.side) not in used
        hit = next((i for i in order if ok(i) and
                    (norm[i].endswith(key) if r.side == "bottom" else norm[i].startswith(key))), None)
        if hit is None:
            hit = next((i for i in order if ok(i)), None)
        if hit is None:
            continue
        edits.setdefault(hit, []).append(r)
        used.add((hit, r.side))
        start = hit
    if not edits:
        return xml

    def less_before(p: str, twips: int) -> str:
        """The border adds the gap between text and line to the paragraph; the same amount is taken from
        the neighbouring spacing so the rest of the page does not move down."""
        m = re.search(r'(<w:spacing\b[^>]*?w:before=")(\d+)(")', p)
        if not m:
            return p
        return p[:m.start(2)] + str(max(0, int(m.group(2)) - twips)) + p[m.end(2):]

    targets = {m.start(): i for i, m in enumerate(paras)}
    out, pos, carry = [], 0, 0
    for m in re.finditer(r"<w:p(?: [^>]*)?>.*?</w:p>", xml, re.S):
        p = m.group(0)
        i = targets.get(m.start())
        changed = False
        if carry:
            p, carry, changed = less_before(p, carry), 0, True
        if i is not None and i in edits and "<w:pBdr" not in p:
            sides = {}
            for r in edits[i]:
                sides[r.side] = (f'<w:{r.side} w:val="single" w:sz="{max(2, min(96, round(r.width * 8)))}" '
                                 f'w:space="{min(31, round(r.gap))}" w:color="{r.color}"/>')
                extra = round((min(31, round(r.gap)) + r.width) * 20)
                if r.side == "bottom":
                    carry = extra
                else:
                    p = less_before(p, extra)
            bdr = "<w:pBdr>" + sides.get("top", "") + sides.get("bottom", "") + "</w:pBdr>"
            if '<w:jc w:val="center"/>' in p:
                # pdf2docx indents centred headings on both sides; don't let the border be that short
                p = re.sub(r'(<w:ind\b[^>]*?)w:left="\d+"', r'\1w:left="0"', p, count=1)
                p = re.sub(r'(<w:ind\b[^>]*?)w:right="\d+"', r'\1w:right="0"', p, count=1)
            if "<w:pPr>" not in p and "<w:pPr " not in p:
                p = re.sub(r"(<w:p(?: [^>]*)?>)", r"\1<w:pPr>" + bdr + "</w:pPr>", p, count=1)
            else:
                ppr = re.search(r"<w:pPr(?: [^>]*)?>(.*?)</w:pPr>", p, re.S)
                inner = ppr.group(1)
                k = min([j for j in (inner.find(f"<w:{t}") for t in _PPR_AFTER_BDR) if j >= 0],
                        default=len(inner))
                p = p[:ppr.start(1)] + inner[:k] + bdr + inner[k:] + p[ppr.end(1):]
            changed = True
        if changed:
            out.append(xml[pos:m.start()])
            out.append(p)
            pos = m.end()
    out.append(xml[pos:])
    return "".join(out)


def _fix_table_grid(m: re.Match) -> str:
    tbl = m.group(0)
    if tbl.count("<w:tbl>") > 1:  # nested table: leave alone
        return tbl
    grid = re.search(r"<w:tblGrid>(.*?)</w:tblGrid>", tbl, re.S)
    if not grid:
        return tbl
    ncol = len(re.findall(r"<w:gridCol ", grid.group(1)))
    for tr in re.findall(r"<w:tr[ >].*?</w:tr>", tbl, re.S):
        cells = re.findall(r"<w:tc>.*?</w:tc>", tr, re.S)
        if len(cells) != ncol or any("<w:gridSpan" in c for c in cells):
            continue
        widths = [re.search(r'<w:tcW[^>]*w:w="(\d+)', c) for c in cells]
        if all(widths):
            new = "".join(f'<w:gridCol w:w="{w.group(1)}"/>' for w in widths)
            return tbl.replace(grid.group(0), f"<w:tblGrid>{new}</w:tblGrid>")
    return tbl


_SECT_P = re.compile(r'<w:p><w:pPr><w:sectPr>(.*?)</w:sectPr></w:pPr></w:p>', re.S)
_COL_BREAK = '<w:r><w:br w:type="column"/></w:r>'


def _merge_column_sections(xml: str) -> str:
    out, pos = [], 0
    matches = list(_SECT_P.finditer(xml))
    i = 0
    while i < len(matches) - 1:
        left, right = matches[i], matches[i + 1]
        lt, rt = left.group(1), right.group(1)
        multi = re.search(r'<w:cols w:num="([2-9])"', lt)
        if (multi and 'w:val="nextColumn"' in rt and re.search(r'<w:cols w:num="' + multi.group(1) + '"', rt)
                and 'w:val="nextColumn"' not in lt):
            out.append(xml[pos:left.start()])           # left column content (section break is removed)
            mid = xml[left.end():right.start()]          # right column content
            if mid.startswith("<w:p>") or mid.startswith("<w:p "):
                k = mid.find("</w:pPr>")
                end_first = mid.find("</w:p>")
                if k != -1 and k < end_first:
                    mid = mid[:k + 8] + _COL_BREAK + mid[k + 8:]
                else:
                    j = mid.find(">") + 1
                    mid = mid[:j] + _COL_BREAK + mid[j:]
            else:
                mid = "<w:p>" + _COL_BREAK + "</w:p>" + mid
            out.append(mid)
            out.append(right.group(0).replace('w:val="nextColumn"', 'w:val="continuous"'))
            pos = right.end()
            i += 2
            continue
        i += 1
    out.append(xml[pos:])
    return "".join(out)


_T = re.compile(r'(<w:t(?: [^>]*)?>)(.*?)(</w:t>)', re.S)


def _restore_spaces(xml: str) -> str:
    """If the document has no spaces, inserts spaces between consecutive word runs."""
    paras = re.findall(r'<w:p[ >].*?</w:p>', xml, re.S)
    pairs = glued = 0
    for p in paras[:400]:
        texts = [m.group(2) for m in _T.finditer(p)]
        for a, b in zip(texts, texts[1:]):
            if a and b:
                pairs += 1
                if a[-1].isalnum() and b[0].isalnum():
                    glued += 1
    if pairs < 50 or glued / pairs < 0.5:
        return xml  # normal document: leave alone

    def fix_para(m: re.Match) -> str:
        p = m.group(0)
        parts = list(_T.finditer(p))
        if len(parts) < 2:
            return p
        out, pos, prev = [], 0, None
        for t in parts:
            txt = t.group(2)
            if prev is not None and txt and prev and not prev[-1].isspace() and not txt[0].isspace():
                rs = max(p.rfind("<w:r>", 0, t.start()), p.rfind("<w:r ", 0, t.start()))
                if prev[-1] in "-\u2060" or "w:vertAlign" in p[rs:t.start()]:
                    # subscript/superscript ("TiO" + "₂") stays attached
                    # line-end hyphens were resolved in textfix (U+2060 = join); a remaining '-' is a real compound word
                    out.append(p[pos:t.start()])
                elif not re.match(r'[.,;:!?)\]]', txt) and prev[-1] not in "([/":
                    out.append(p[pos:t.start()])
                    out.append(t.group(1) + " " + txt + t.group(3))
                    pos = t.end()
                    prev = txt
                    continue
                else:
                    out.append(p[pos:t.start()])
            else:
                out.append(p[pos:t.start()])
            out.append(t.group(0))
            pos = t.end()
            prev = txt
        out.append(p[pos:])
        return "".join(out)
    xml = re.sub(r'<w:p[ >].*?</w:p>', fix_para, xml, flags=re.S)
    return xml.replace('<w:t> ', '<w:t xml:space="preserve"> ')


_BULLETS = set("•●▪◦■○")
_RUN = re.compile(r'(<w:r(?: [^>]*)?>.*?</w:r>)', re.S)
_NUMBER = re.compile(r'^(\(?\d{1,2}[.)]|[a-zçğıöşü][.)])$')


def _run_text(run: str) -> str:
    return "".join(re.findall(r'<w:t(?: [^>]*)?>(.*?)</w:t>', run, re.S))


def _fix_list_para(p: str) -> str:
    """If a '•' or '1.' arrives as a separate run stuck to the text, inserts a tab;
    if there is a new '•' in the middle of a paragraph, splits the paragraph there."""
    if len(re.findall(r"<w:p[ >]", p)) > 1 or "<w:sectPr" in p:  # nested paragraph (text box) etc.: leave alone
        return p
    m = re.match(r'(<w:p(?: [^>]*)?>)(<w:pPr>.*?</w:pPr>)?(.*)</w:p>$', p, re.S)
    if not m:
        return p
    head, ppr, body = m.group(1), m.group(2) or "", m.group(3)
    tokens = [t for t in _RUN.split(body) if t]
    run_idx = [i for i, t in enumerate(tokens) if _RUN.fullmatch(t)]
    if not run_idx:
        return p

    first = run_idx[0]
    first_txt = _run_text(tokens[first]).strip()
    is_list = first_txt in _BULLETS or bool(_NUMBER.match(first_txt))
    if not is_list:
        return p

    # pdf2docx aligns wrapped lines by putting <w:tab/> at their start; with a hanging
    # indent this tab pushes the text further. Turn in-line tabs into spaces.
    tab_run = re.compile(r'<w:r(?: [^>]*)?>(?:<w:rPr>.*?</w:rPr>)?<w:tab/></w:r>', re.S)
    cleaned = []
    for t in tokens:
        if tab_run.fullmatch(t):
            prev = next((_run_text(x) for x in reversed(cleaned) if _RUN.fullmatch(x)), "")
            if prev and not prev[-1].isspace():
                cleaned.append('<w:r><w:t xml:space="preserve"> </w:t></w:r>')
            continue
        cleaned.append(t)
    tokens = cleaned
    run_idx = [i for i, t in enumerate(tokens) if _RUN.fullmatch(t)]
    first = run_idx[0]

    # Split at bullets in the middle (numbers in a numbered list)
    numbered = bool(_NUMBER.match(first_txt))
    cuts = [i for i in run_idx[1:]
            if _run_text(tokens[i]).strip() in _BULLETS
            or (numbered and re.fullmatch(r'\d{1,2}\.', _run_text(tokens[i]).strip()))]
    groups, prev = [], 0
    for c in cuts:
        groups.append(tokens[prev:c])
        prev = c
    groups.append(tokens[prev:])

    # Same hanging indent for all items: the mark stays in place, the text and
    # wrapped lines align at a fixed distance from the mark. (pdf2docx gives each item
    # a different tab position; the result looked messy.)
    ind = re.search(r'<w:ind ([^>]*?)/>', ppr)
    if ind:
        attrs = dict(re.findall(r'w:(\w+)="(-?\d+)"', ind.group(1)))
        left = int(attrs.get("left", 0))
        marker = left - int(attrs.get("hanging", 0)) + int(attrs.get("firstLine", 0))
        gap = 284 if first_txt in _BULLETS else 397  # 0.5 cm / 0.7 cm (twips)
        new_left = max(marker, 0) + gap
        # the text now starts (new_left - left) further right: give that back on the right,
        # otherwise lines that filled the original width wrap one word early
        right = int(attrs.get("right", 0)) - max(0, new_left - left)
        ppr = ppr.replace(ind.group(0),
                          f'<w:ind w:left="{new_left}" w:right="{right}" w:hanging="{gap}"/>')
        ppr = re.sub(r'<w:tabs>.*?</w:tabs>', '', ppr, flags=re.S)

    paras = []
    for g in groups:
        g = list(g)
        r = next((i for i, t in enumerate(g) if _RUN.fullmatch(t)), None)
        if r is not None and not (r + 1 < len(g) and "<w:tab/>" in g[r + 1]):
            g.insert(r + 1, "<w:r><w:tab/></w:r>")
        paras.append(head + ppr + "".join(g) + "</w:p>")
    return "".join(paras)


def _unclip_table_rows(docx: Path, info=None) -> None:
    """Fix layout problems in pdf2docx output (see _fix_layout_xml)."""
    import zipfile
    tmp = docx.with_suffix(".tmp")
    with zipfile.ZipFile(docx) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                data = _fix_layout_xml(data.decode("utf-8"), info).encode("utf-8")
            zout.writestr(item, data)
    os.replace(tmp, docx)


def pdf_to_text(paths: list[Path], progress: Progress, **_) -> list[Path]:
    outputs = []
    for p in paths:
        progress(T("Metin çıkarılıyor: {0}", p.name))
        with open_pdf(p) as doc:
            text = "\n\f".join(page.get_text() for page in doc)
        out = unique_path(p.parent, p.stem, ".txt")
        out.write_text(text, encoding="utf-8")
        if not text.strip():
            progress(T("ℹ {0}: metin yok (taranmış PDF olabilir, OCR gerekir).", p.name))
        outputs.append(out)
    return outputs


# --------------------------------------------------------------------------
# Image → PDF
# --------------------------------------------------------------------------

def images_to_pdf(paths: list[Path], progress: Progress, ordered: bool = False, **_) -> list[Path]:
    if not ordered:
        paths = sorted_by_name(paths)
    out_doc = pymupdf.open()
    for p in paths:
        progress(T("Ekleniyor: {0}", p.name))
        try:
            img = pymupdf.open(p)
            pdf_bytes = img.convert_to_pdf()
            img.close()
        except Exception:
            # Pillow for formats PyMuPDF can't open (HEIC, some webp)
            import io
            from .tools import _open_image
            buf = io.BytesIO()
            _open_image(p).save(buf, format="JPEG", quality=92)
            with pymupdf.open("jpg", buf.getvalue()) as img:
                pdf_bytes = img.convert_to_pdf()
        with pymupdf.open("pdf", pdf_bytes) as page_doc:
            out_doc.insert_pdf(page_doc)
    stem = paths[0].stem if len(paths) == 1 else f"{paths[0].stem}_ve_{len(paths) - 1}_gorsel"
    out = unique_path(paths[0].parent, stem, ".pdf")
    out_doc.save(out, garbage=3, deflate=True)
    out_doc.close()
    return [out]


# --------------------------------------------------------------------------
# Office → PDF
# --------------------------------------------------------------------------

def _find_soffice() -> str | None:
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            cand = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if cand.exists():
                return str(cand)
    return None


def _office_com_to_pdf(src: Path, out: Path) -> bool:
    """Convert with MS Office if it is installed on Windows. True on success."""
    try:
        import win32com.client  # pywin32
    except ImportError:
        return False
    ext = src.suffix.lower().lstrip(".")
    src_s, out_s = str(src.resolve()), str(out.resolve())
    if ext in ("doc", "docx", "odt", "rtf", "txt", "html", "htm"):
        app = win32com.client.DispatchEx("Word.Application")
        app.Visible = False
        try:
            d = app.Documents.Open(src_s, ReadOnly=True)
            d.ExportAsFixedFormat(out_s, 17)  # wdExportFormatPDF
            d.Close(False)
        finally:
            app.Quit()
    elif ext in ("xls", "xlsx", "ods", "csv"):
        app = win32com.client.DispatchEx("Excel.Application")
        app.Visible = False
        app.DisplayAlerts = False
        try:
            wb = app.Workbooks.Open(src_s, ReadOnly=True)
            wb.ExportAsFixedFormat(0, out_s)  # xlTypePDF
            wb.Close(False)
        finally:
            app.Quit()
    elif ext in ("ppt", "pptx", "odp"):
        app = win32com.client.DispatchEx("PowerPoint.Application")
        try:
            pres = app.Presentations.Open(src_s, ReadOnly=True, WithWindow=False)
            pres.SaveAs(out_s, 32)  # ppSaveAsPDF
            pres.Close()
        finally:
            app.Quit()
    else:
        return False
    return out.exists()


class _LoProfile:
    """LibreOffice user profile. Creating a new profile every time (the old behaviour) added a few seconds
    to each conversion, especially on Windows; a persistent profile is used now. If a second conversion
    is running at the same time (profile locked), that run falls back to a temporary profile."""

    def __enter__(self):
        self.tmp = None
        self.lock = None
        try:
            from .stamp_model import config_dir
            base = config_dir() / "libreoffice-profil"
            base.mkdir(parents=True, exist_ok=True)
            lock = base.with_suffix(".kilit")
            try:
                if lock.exists() and time.time() - lock.stat().st_mtime > 900:
                    lock.unlink()                       # stale lock left over from a crashed run
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                self.lock = lock
                return base.as_uri()
            except FileExistsError:
                pass
        except Exception:
            pass
        self.tmp = tempfile.TemporaryDirectory(prefix="riffle_lo_")
        return Path(self.tmp.name, "profil").as_uri()

    def __exit__(self, *exc):
        if self.lock:
            try:
                self.lock.unlink()
            except OSError:
                pass
        if self.tmp:
            self.tmp.cleanup()
        return False


_COL = re.compile(r"[A-Z]+")


def _col_index(ref: str) -> int:
    n = 0
    for ch in _COL.match(ref).group(0) if _COL.match(ref) else "A":
        n = n * 26 + ord(ch) - 64
    return n


_AFTER_PAGESETUP = ("headerFooter", "rowBreaks", "colBreaks", "customProperties", "cellWatches", "ignoredErrors",
                    "smartTags", "drawing", "legacyDrawing", "legacyDrawingHF", "drawingHF", "picture", "oleObjects",
                    "controls", "webPublishItems", "tableParts", "extLst")


def _fit_sheet_xml(xml: str) -> str:
    """When printing an Excel sheet, the columns fit one page width (height is free). The user's own
    scale/fit setting is left alone. For wide tables the paper is landscape."""
    if re.search(r"<pageSetup\b[^>]*\b(scale|fitToWidth|fitToHeight)=", xml) or "fitToPage=\"1\"" in xml:
        return xml
    dim = re.search(r'<dimension ref="[A-Z]+\d+:([A-Z]+)\d+"', xml)
    wide = dim is not None and _col_index(dim.group(1)) > 8
    # 1) <sheetPr><pageSetUpPr fitToPage="1"/></sheetPr> (first child of worksheet)
    m = re.search(r"<sheetPr\b([^>]*?)(/?)>", xml)
    if m is None:
        xml = re.sub(r"(<worksheet\b[^>]*>)", r'\1<sheetPr><pageSetUpPr fitToPage="1"/></sheetPr>', xml, count=1)
    elif m.group(2) == "/":
        xml = xml[:m.start()] + f'<sheetPr{m.group(1)}><pageSetUpPr fitToPage="1"/></sheetPr>' + xml[m.end():]
    elif re.search(r"<pageSetUpPr\b", xml):
        xml = re.sub(r"<pageSetUpPr\b", '<pageSetUpPr fitToPage="1"', xml, count=1)
    else:
        # must come after tabColor / outlinePr
        end = xml.index("</sheetPr>")
        xml = xml[:end] + '<pageSetUpPr fitToPage="1"/>' + xml[end:]
    # 2) <pageSetup fitToWidth="1" fitToHeight="0" .../>
    orient = ' orientation="landscape"' if wide else ""
    ps = re.search(r"<pageSetup\b([^>]*?)/?>", xml)
    if ps:
        attrs = ps.group(1)
        if "orientation=" in attrs:
            orient = ""
        xml = xml[:ps.start()] + f'<pageSetup fitToWidth="1" fitToHeight="0"{orient}{attrs}/>' + xml[ps.end():]
        return xml
    new = f'<pageSetup paperSize="9" fitToWidth="1" fitToHeight="0"{orient}/>'
    pm = re.search(r"<pageMargins\b[^>]*/>", xml)
    if pm:
        return xml[:pm.end()] + new + xml[pm.end():]
    new = '<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>' + new
    for tag in _AFTER_PAGESETUP:
        i = xml.find(f"<{tag}")
        if i >= 0:
            return xml[:i] + new + xml[i:]
    return xml.replace("</worksheet>", new + "</worksheet>")


def _excel_fit_width(src: Path, dst: Path) -> None:
    """Copy of the src .xlsx with its sheets set to 'fit to width' (the rest of the file is untouched)."""
    import zipfile
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            data = zin.read(info.filename)
            if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", info.filename):
                data = _fit_sheet_xml(data.decode("utf-8")).encode("utf-8")
            zout.writestr(info, data)


def office_to_pdf_many(paths: list[Path], outdir: Path, progress: Progress = lambda m: None) -> dict:
    """Converts several Office files to PDF with ONE LibreOffice call (restarting the program for each
    file is the slowest part). Returns {source: pdf}; files that failed are not in the dict."""
    paths = list(dict.fromkeys(Path(p) for p in paths))
    if not paths:
        return {}
    outdir.mkdir(parents=True, exist_ok=True)
    soffice = _find_soffice()
    result: dict = {}
    if soffice:
        src_dir = outdir / "_kaynak"
        src_dir.mkdir(exist_ok=True)
        staged = []
        for i, p in enumerate(paths):                  # files with the same name must not collide: sequence number
            dst = src_dir / f"{i:03d}{p.suffix.lower()}"
            if p.suffix.lower() in (".xlsx", ".xlsm"):
                try:
                    _excel_fit_width(p, dst)      # wide tables should not be split on the right onto separate pages
                except Exception:
                    shutil.copyfile(p, dst)
            else:
                shutil.copyfile(p, dst)
            staged.append(dst)
        progress(T("PDF'e çevriliyor: {0} dosya", len(paths)) if len(paths) > 1 else T("PDF'e çevriliyor: {0}", paths[0].name))
        flags = 0x08000000 if sys.platform == "win32" else 0   # CREATE_NO_WINDOW
        with _LoProfile() as profile:
            cmd = [soffice, f"-env:UserInstallation={profile}", "--headless",
                   "--convert-to", "pdf", "--outdir", str(outdir), *map(str, staged)]
            r = _run_cancellable(cmd, 120 + 120 * len(paths), flags)
        for p, st in zip(paths, staged):
            produced = outdir / (st.stem + ".pdf")
            if produced.exists():
                result[p] = produced
        shutil.rmtree(src_dir, ignore_errors=True)
        if not result and paths:
            raise ConversionError(T("{0} çevrilemedi.\n{1}", paths[0].name, r.stderr.strip()[:300]))
        return result
    if sys.platform == "win32":
        for i, p in enumerate(paths):
            out = outdir / f"{i:03d}.pdf"
            progress(T("PDF'e çevriliyor: {0}", p.name))
            if _office_com_to_pdf(p, out):
                result[p] = out
        if result:
            return result
    raise ConversionError(
        T("Office → PDF için LibreOffice gerekli (ücretsiz): "
        "https://www.libreoffice.org/download\n"
        "Windows'ta MS Office kuruluysa 'pip install pywin32' de yeterli."))


def office_to_pdf(paths: list[Path], progress: Progress, **_) -> list[Path]:
    outputs = []
    with tempfile.TemporaryDirectory(prefix="riffle_lo_") as tmp:
        done = office_to_pdf_many(paths, Path(tmp), progress)
        missing = [p.name for p in paths if p not in done]
        for p in paths:
            if p in done:
                out = unique_path(p.parent, p.stem, ".pdf")
                shutil.move(str(done[p]), out)
                outputs.append(out)
    if missing:
        if not outputs:
            raise ConversionError(T("{0} çevrilemedi.", ', '.join(missing)))
        progress(T("ℹ çevrilemedi: {0}", ', '.join(missing)))
    return outputs


# --------------------------------------------------------------------------
# Action catalogue — the menus are generated from this
# --------------------------------------------------------------------------

@dataclass
class Action:
    id: str
    label: str
    extensions: list[str]
    func: Callable[..., list[Path]]
    group: str = ""                     # right-click submenu (Windows)
    needs_password: str | None = None   # None | "new" | "existing"
    ask: tuple | None = None            # (kwarg, question, default): ask the user for text
    multi_only: bool = False            # needs at least 2 files
    batch: bool = False                 # all files in one job (merge, image → PDF)
    interactive: bool = False           # opens its own window (no progress window)
    order: str | None = None            # order window for multiple selection: description in the title
    quick: Callable[..., list[Path]] | None = None  # windowed action with ordering → this runs on "OK"
    kwargs: dict = field(default_factory=dict)


def _t(name: str, module: str = "tools"):
    """Loads the tool at call time (tool modules import this module → avoids the import cycle)."""
    def call(*args, **kwargs):
        import importlib
        return getattr(importlib.import_module(f"{__package__}.{module}"), name)(*args, **kwargs)
    call.__name__ = name
    return call


PAGES_Q = ("pages", "Sayfa aralığı (ör. 1-3, 5, 8-  •  'tek', 'çift', 'son'):", "")

G_CONV, G_EDIT, G_PAGE, G_SIZE, G_SEC = "Dönüştür", "Düzenle", "Sayfalar", "Küçült / Onar", "Güvenlik"
G_PRINT = "Yazdır"

ACTIONS: list[Action] = [
    # --- Page organizer (PDF + images + Office mixed; top of the menu)
    Action("organize",   "Sayfaları düzenle / birleştir…",   PDF + IMAGES + OFFICE, _t("organize"),
           batch=True, interactive=True, order="Birleştirme sırası (üstteki önce gelir)",
           quick=_t("merge_files")),
    # --- Folder
    Action("folder2pdf", "Klasörü tek PDF yap (yer imli)",   [FOLDER], _t("folder_to_pdf")),
    Action("search",     "Klasörde ara (dosyaların içinde)…", [FOLDER], _t("run", "search"), interactive=True),
    # --- Convert
    Action("pdf2word",   "PDF → Word",                       PDF, pdf_to_word, G_CONV),
    Action("pdf2wordx",  "PDF → Word (birebir düzen)",       PDF, _t("pdf_to_word_exact"), G_CONV),
    Action("pdf2wordf",  "PDF → Word (akan metin)",          PDF, pdf_to_word, G_CONV, kwargs={"mode": "flow"}),
    Action("pdf2excel",  "PDF → Excel (tablolar)",           PDF, _t("pdf_to_excel"), G_CONV),
    Action("pdf2pptx",   "PDF → PowerPoint",                 PDF, _t("pdf_to_pptx"), G_CONV),
    Action("pdf2jpg",    "PDF → JPG",                        PDF, pdf_to_jpg, G_CONV),
    Action("pdf2txt",    "PDF → Metin (TXT)",                PDF, pdf_to_text, G_CONV),
    Action("images",     "İçindeki resimleri çıkar",         PDF, _t("extract_images", "docops"), G_CONV),
    Action("pdf2md",     "PDF → Markdown",                   PDF, _t("pdf_to_markdown", "docops"), G_CONV),
    Action("pdfa",       "PDF/A'ya çevir (arşiv)",           PDF, _t("pdf_to_pdfa", "docops"), G_CONV),
    Action("ocr",        "Taranmış PDF'i aranabilir yap (OCR)", PDF, _t("make_searchable"), G_CONV),
    # --- Edit
    Action("merge",      "PDF'leri birleştir",               PDF, merge_pdfs, G_EDIT, multi_only=True, batch=True,
           order="Birleştirme sırası (üstteki önce gelir)"),
    Action("pagenum",    "Sayfa numarası ekle",              PDF, _t("add_page_numbers"), G_EDIT),
    Action("watermark",  "Filigran ekle",                    PDF, _t("add_watermark"), G_EDIT,
           ask=("text", "Filigran metni:", "TASLAK")),
    Action("rotate",     "Sağa döndür (90°)",                PDF, rotate_pdf, G_EDIT, kwargs={"angle": 90}),
    Action("rotatel",    "Sola döndür (90°)",                PDF, rotate_pdf, G_EDIT, kwargs={"angle": 270}),
    Action("rotate180",  "Ters çevir (180°)",                PDF, rotate_pdf, G_EDIT, kwargs={"angle": 180}),
    Action("sign",       "İmza / yazı / logo ekle…",         PDF, _t("run", "stamper"), interactive=True),
    Action("form",       "Formu doldur…",                    PDF, _t("run", "forms"), interactive=True),
    Action("crop",       "Beyaz kenarları kırp",             PDF, _t("autocrop", "docops"), G_EDIT),
    # --- Sayfalar
    Action("split",      "Her sayfayı ayrı PDF yap",         PDF, split_pdf, G_PAGE),
    Action("extract",    "Sayfa seç ve çıkar…",              PDF, _t("extract_pages"), G_PAGE, ask=PAGES_Q),
    Action("delete",     "Sayfa sil…",                       PDF, _t("delete_pages"), G_PAGE, ask=PAGES_Q),
    Action("reverse",    "Sayfa sırasını ters çevir",        PDF, _t("reverse_pages"), G_PAGE),
    Action("blank",      "Boş sayfaları sil",                PDF, _t("remove_blank_pages", "docops"), G_PAGE),
    Action("a4",         "Bütün sayfaları A4 yap",           PDF, _t("to_a4", "docops"), G_PAGE),
    Action("splitsize",  "Boyuta göre böl… (ör. 10 MB)",     PDF, _t("split_by_size", "docops"), G_PAGE,
           ask=("size", "Her parça en fazla kaç MB olsun? (ör. 10 ya da 9.5)", "10")),
    # --- Print
    Action("nup2",       "2 sayfayı 1 kâğıda",               PDF, _t("n_up", "docops"), G_PRINT, kwargs={"per": 2}),
    Action("nup4",       "4 sayfayı 1 kâğıda",               PDF, _t("n_up", "docops"), G_PRINT, kwargs={"per": 4}),
    Action("nup6",       "6 sayfayı 1 kâğıda",               PDF, _t("n_up", "docops"), G_PRINT, kwargs={"per": 6}),
    Action("booklet",    "Kitapçık yap (katla-zımbala)",     PDF, _t("booklet", "docops"), G_PRINT),
    Action("gray",       "Siyah-beyaz yap",                  PDF, _t("grayscale", "docops"), G_PRINT),
    # --- Shrink / Repair
    Action("compress",   "Sıkıştır (önerilen)",              PDF, compress_pdf, G_SIZE, kwargs={"level": "normal"}),
    Action("compress2",  "Çok sıkıştır (e-posta için)",      PDF, compress_pdf, G_SIZE, kwargs={"level": "strong"}),
    Action("target2",    "2 MB altına indir",                PDF, _t("compress_to_target"), G_SIZE,
           kwargs={"target": "2"}),
    Action("target5",    "5 MB altına indir",                PDF, _t("compress_to_target"), G_SIZE,
           kwargs={"target": "5"}),
    Action("targetx",    "Hedef boyuta indir…",              PDF, _t("compress_to_target"), G_SIZE,
           ask=("target", "Hedef boyut (MB; ör. 1.5 ya da 500kb):", "2")),
    Action("fixscan",    "Taramayı düzelt (eğiklik, boş sayfa)", PDF, _t("fix_scan", "docops"), G_SIZE),
    Action("repair",     "Bozuk PDF'i onar",                 PDF, _t("repair_pdf"), G_SIZE),
    # --- Security
    Action("protect",    "Şifre koy",                        PDF, protect_pdf, G_SEC, needs_password="new"),
    Action("unlock",     "Şifreyi kaldır",                   PDF, unlock_pdf, G_SEC, needs_password="existing"),
    Action("redact",     "Kişisel bilgileri karart…",        PDF, _t("redact", "docops"), G_SEC,
           ask=("words", "TC kimlik, telefon, IBAN, e-posta otomatik bulunur.\n"
                         "Ek karartılacak kelimeler (virgülle; boş bırakabilirsin):", " ")),
    Action("compare",    "İki PDF'i karşılaştır",            PDF, _t("compare", "docops"), G_EDIT,
           multi_only=True, batch=True,
           order="Hangisi eski? Üstteki eski sürüm, alttaki yeni sürüm sayılır"),
    # --- Office / images
    Action("office2pdf", "PDF'e çevir",                      OFFICE, office_to_pdf, batch=True),
    Action("wordmerge",  "Word belgelerini birleştir (Word olarak)", ["docx"], _t("merge_word", "officetools"),
           multi_only=True, batch=True, order="Birleştirme sırası (üstteki önce gelir)"),
    Action("csv2xlsx",   "Excel'e çevir (karakterler, sayılar, tarihler düzgün)", ["csv"], _t("csv_to_excel", "officetools")),
    Action("handout",    "Not çıktısı yap (sayfada 3 slayt + not satırları)", SLIDES,
           _t("slide_handout", "officetools"), batch=True),
    Action("officeshrink", "Küçült (içindeki resimleri sıkıştır)", OOXML, _t("compress_office", "officetools")),
    Action("officeimg",  "İçindeki resimleri çıkar",         OOXML, _t("extract_office_images", "officetools")),
    Action("img2pdf",    "PDF'e çevir (tek dosya)",          IMAGES, images_to_pdf, batch=True,
           order="Sayfa sırası (üstteki ilk sayfa olur)"),
    Action("photo2doc",  "Fotoğraftan belge (taranmış PDF)", IMAGES, _t("photos_to_document"), batch=True,
           order="Sayfa sırası (üstteki ilk sayfa olur)"),
    Action("img2jpg",    "JPG'ye çevir",                     [e for e in IMAGES if e not in ("jpg", "jpeg")],
           _t("images_to_jpg")),
    Action("imgshrink",  "Küçült (web / e-posta için)",      IMAGES, _t("shrink_images")),
    Action("imgtarget",  "Hedef boyuta indir… (ör. 200 KB)", IMAGES, _t("image_to_target", "imagetools"),
           ask=("target", "Hedef boyut (KB; ör. 200 ya da 1.5mb):", "200")),
    Action("imgocr",     "Görseldeki yazıyı çıkar (OCR → panoya)", IMAGES, _t("image_text", "imagetools"),
           batch=True),
    Action("img2png",    "PNG'ye çevir",                     [e for e in IMAGES if e != "png"],
           _t("images_to_png", "imagetools")),
    Action("imgrotr",    "Sağa döndür",                      IMAGES, _t("rotate_images", "imagetools"),
           kwargs={"angle": 90}),
    Action("imgrotl",    "Sola döndür",                      IMAGES, _t("rotate_images", "imagetools"),
           kwargs={"angle": -90}),
    # --- at the end of every menu
    Action("settings",   "Ayarlar…",                         PDF + IMAGES + OFFICE + [FOLDER],
           _t("run", "settings"), interactive=True, batch=True),
]

BY_ID = {a.id: a for a in ACTIONS}


def actions_for_extension(ext: str) -> list[Action]:
    ext = ext if ext == FOLDER else ext.lower().lstrip(".")
    return [a for a in ACTIONS if ext in a.extensions]


def all_extensions() -> list[str]:
    """File extensions (excluding the folder pseudo extension)."""
    seen: list[str] = []
    for a in ACTIONS:
        for e in a.extensions:
            if e not in seen and e != FOLDER:
                seen.append(e)
    return seen


def accepts(action: Action, p: Path) -> bool:
    if p.is_dir():
        return FOLDER in action.extensions
    return p.is_file() and p.suffix.lower().lstrip(".") in action.extensions
