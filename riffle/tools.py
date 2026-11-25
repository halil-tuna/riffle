"""Extra tools: page operations, watermark, page numbers, OCR, Excel/PowerPoint,
image conversion. All of them use the signature from actions.py:

    func(paths: list[Path], progress, **kwargs) -> list[Path]
"""

from __future__ import annotations

import re
from pathlib import Path

from .actions import ConversionError, fix_tounicode, open_pdf, pymupdf, unique_path
from .i18n import T

# ============================================================ helpers


def parse_pages(spec: str, n: int) -> list[int]:
    """'1-3, 5, 8-' / 'last' / 'odd' / 'even' (Turkish: 'son' / 'tek' / 'çift') → 0-based page indexes (sorted, unique)."""
    spec = (spec or "").strip().lower().replace(" ", "")
    if not spec:
        raise ConversionError(T("Sayfa aralığı boş."))
    out: list[int] = []
    for part in spec.replace(";", ",").split(","):
        if not part:
            continue
        if part in ("son", "last"):
            out.append(n - 1)
            continue
        if part in ("tek", "odd"):
            out += list(range(0, n, 2))
            continue
        if part in ("çift", "cift", "even"):
            out += list(range(1, n, 2))
            continue
        m = re.fullmatch(r"(\d*)-(\d*|son|last)", part)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = n if m.group(2) in ("", "son", "last") else int(m.group(2))
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ConversionError(T("Anlaşılmayan sayfa ifadesi: '{0}' (örnek: 1-3,5,8-)", part))
        if a < 1 or b > n or a > b:
            raise ConversionError(T("Sayfa aralığı {0} geçersiz (belge {1} sayfa).", part, n))
        out += list(range(a - 1, b))
    return sorted(set(out))


def _font_file() -> str | None:
    from .ocr import _font_file
    return _font_file()


def _visual_point(page, x: float, y: float):
    """Converts a point in the page's VISIBLE (rotated) coordinates to content coordinates."""
    return pymupdf.Point(x, y) * page.derotation_matrix


# ============================================================ page operations


def extract_pages(paths, progress, pages: str = "", **_):
    outputs = []
    for p in paths:
        with open_pdf(p) as src:
            idx = parse_pages(pages, len(src))
            progress(T("{0}: {1} sayfa çıkarılıyor", p.name, len(idx)))
            out_doc = pymupdf.open()
            for i in idx:
                out_doc.insert_pdf(src, from_page=i, to_page=i)
            out = unique_path(p.parent, f"{p.stem}_secilen_sayfalar", ".pdf")
            out_doc.save(out, garbage=3, deflate=True)
            out_doc.close()
        outputs.append(out)
    return outputs


def delete_pages(paths, progress, pages: str = "", **_):
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            idx = parse_pages(pages, len(doc))
            if len(idx) >= len(doc):
                raise ConversionError(T("{0}: tüm sayfalar silinemez.", p.name))
            progress(T("{0}: {1} sayfa siliniyor", p.name, len(idx)))
            doc.delete_pages(idx)
            out = unique_path(p.parent, f"{p.stem}_sayfa_silindi", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        outputs.append(out)
    return outputs


def reverse_pages(paths, progress, **_):
    outputs = []
    for p in paths:
        progress(T("Sayfa sırası ters çevriliyor: {0}", p.name))
        with open_pdf(p) as doc:
            doc.select(list(range(len(doc) - 1, -1, -1)))
            out = unique_path(p.parent, f"{p.stem}_ters", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        outputs.append(out)
    return outputs


def repair_pdf(paths, progress, **_):
    outputs = []
    for p in paths:
        progress(T("Onarılıyor: {0}", p.name))
        try:
            doc = pymupdf.open(p)
        except Exception as e:
            raise ConversionError(T("{0} açılamayacak kadar bozuk: {1}", p.name, e))
        out = unique_path(p.parent, f"{p.stem}_onarilmis", ".pdf")
        try:
            repaired = getattr(doc, "is_repaired", False)
            try:
                doc.save(out, garbage=4, deflate=True, clean=True)
                note = T("yapısal hatalar onarıldı.") if repaired else T("hata bulunmadı, yeniden düzenlendi.")
            except Exception:
                # Gradual recovery: copy pages one by one; take an image of any page that can't be copied
                out.unlink(missing_ok=True)
                new, copied, imaged, lost = pymupdf.open(), 0, 0, 0
                for i in range(len(doc)):
                    try:
                        new.insert_pdf(doc, from_page=i, to_page=i)
                        copied += 1
                        continue
                    except Exception:
                        pass
                    try:
                        pg = doc[i]
                        pix = pg.get_pixmap(dpi=150)
                        n = new.new_page(width=pg.rect.width, height=pg.rect.height)
                        n.insert_image(n.rect, stream=pix.tobytes("jpeg"))
                        imaged += 1
                    except Exception:
                        lost += 1
                if not len(new):
                    raise ConversionError(T("{0}: kurtarılabilen sayfa yok.", p.name))
                new.save(out, garbage=4, deflate=True)
                new.close()
                note = T("{0} sayfa kurtarıldı", copied) + (T(", {0} sayfa görüntü olarak", imaged) if imaged else "") + \
                       (T(", {0} sayfa kurtarılamadı", lost) if lost else "") + "."
        finally:
            doc.close()
        progress(f"ℹ {p.name}: {note}")
        outputs.append(out)
    return outputs


# ============================================================ page numbers / watermark


def add_page_numbers(paths, progress, fmt: str = "{n} / {total}", **_):
    ffile = _font_file()
    outputs = []
    for p in paths:
        progress(T("Sayfa numarası ekleniyor: {0}", p.name))
        with open_pdf(p) as doc:
            total = len(doc)
            for i, page in enumerate(doc):
                text = fmt.format(n=i + 1, total=total)
                W, H = page.rect.width, page.rect.height      # visible size
                size = max(8.0, min(11.0, W / 60))
                font = pymupdf.Font(fontfile=ffile) if ffile else pymupdf.Font("helv")
                tw = font.text_length(text, fontsize=size)
                pt = _visual_point(page, (W - tw) / 2, H - max(18.0, H * 0.03))
                kw = dict(fontsize=size, color=(0.25, 0.25, 0.25), rotate=page.rotation)
                if ffile:
                    page.insert_text(pt, text, fontname="rifflenum", fontfile=ffile, **kw)
                else:
                    page.insert_text(pt, text, fontname="helv", **kw)
            fix_tounicode(doc)
            out = unique_path(p.parent, f"{p.stem}_numarali", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        outputs.append(out)
    return outputs


def add_watermark(paths, progress, text: str = "", **_):
    text = (text or "").strip()
    if not text:
        raise ConversionError(T("Filigran metni boş."))
    ffile = _font_file()
    outputs = []
    for p in paths:
        progress(T("Filigran ekleniyor: {0}", p.name))
        with open_pdf(p) as doc:
            for page in doc:
                W, H = page.rect.width, page.rect.height
                font = pymupdf.Font(fontfile=ffile) if ffile else pymupdf.Font("helv")
                diag = (W ** 2 + H ** 2) ** 0.5
                size = min(120.0, 0.75 * diag / max(font.text_length(text, fontsize=1), 1))
                tl = font.text_length(text, fontsize=size)
                # Convert the bottom-left → top-right direction on the visible page to content coordinates
                import math
                c_v = pymupdf.Point(W / 2, H / 2)
                center = c_v * page.derotation_matrix
                d = (c_v + (1, -1)) * page.derotation_matrix - center
                angle = -math.degrees(math.atan2(d.y, d.x))
                start = pymupdf.Point(center.x - tl / 2, center.y + size * 0.35)
                mat = pymupdf.Matrix(angle)
                kw = dict(fontsize=size, color=(0.5, 0.5, 0.5), fill_opacity=0.18,
                          morph=(center, mat), overlay=True)
                if ffile:
                    page.insert_text(start, text, fontname="rifflewm", fontfile=ffile, **kw)
                else:
                    page.insert_text(start, text, fontname="helv", **kw)
            fix_tounicode(doc)
            out = unique_path(p.parent, f"{p.stem}_filigranli", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        outputs.append(out)
    return outputs


# ============================================================ OCR


def make_searchable(paths, progress, **_):
    from . import ocr
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            pages = [i for i, pg in enumerate(doc) if ocr.needs_ocr(pg)]
            if not pages:
                progress(T("ℹ {0}: zaten metin içeriyor, OCR gerekmedi.", p.name))
                continue
            try:
                words = ocr.ocr_pages(doc, pages, progress)
            except ocr.OcrUnavailable as e:
                raise ConversionError(str(e))
            rep: dict = {}
            n = ocr.make_searchable(doc, words, report=rep)
            out = unique_path(p.parent, f"{p.stem}_ocr", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        progress(T("ℹ {0}: {1} sayfada {2} kelime tanındı, metin seçilebilir ve aranabilir.", p.name, len(pages), n))
        if rep.get("handwritten"):
            progress(T("ℹ {0} sayfa el yazısı gibi görünüyor: Tesseract el yazısını güvenilir okuyamaz, yalnızca emin olduğu kelimeler eklendi ({1} şüpheli kelime atlandı).", rep['handwritten'], rep['dropped']))
        outputs.append(out)
    return outputs


# ============================================================ Excel / PowerPoint / Word exact layout


def pdf_to_excel(paths, progress, **_):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font
        from openpyxl.utils import get_column_letter
    except ImportError:
        raise ConversionError(T("openpyxl kurulu değil: pip install openpyxl"))
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            wb = Workbook()
            wb.remove(wb.active)
            count = 0
            for strategy in ("lines", "text"):
                for i, page in enumerate(doc):
                    progress(T("{0}: sayfa {1}/{2} tablolar aranıyor", p.name, i + 1, len(doc)))
                    try:
                        tabs = page.find_tables(strategy=strategy)
                    except Exception:
                        continue
                    for k, t in enumerate(tabs.tables):
                        # read the cell text with MuPDF's normal extractor (find_tables'
                        # own extract() mixes up the letter order in some PDFs)
                        rows = [[(" ".join(page.get_text("text", clip=pymupdf.Rect(c)).split()) if c else None)
                                 for c in row.cells] for row in t.rows]
                        if len(rows) < 2 or max((len(r) for r in rows), default=0) < 2:
                            continue
                        count += 1
                        ws = wb.create_sheet(f"S{i + 1}-T{k + 1}"[:31])
                        for r in rows:
                            ws.append([_cell(c) for c in r])
                        for row in ws.iter_rows():
                            for c in row:
                                if hasattr(c.value, "year"):
                                    c.number_format = "DD.MM.YYYY"
                        for c in ws[1]:
                            c.font = Font(bold=True)
                        for col in range(1, ws.max_column + 1):
                            width = max(len(str(ws.cell(r, col).value or "")) for r in range(1, ws.max_row + 1))
                            ws.column_dimensions[get_column_letter(col)].width = min(60, max(8, width + 2))
                if count:
                    break
                progress(T("ℹ {0}: çizgili tablo yok, metin hizasına göre aranıyor.", p.name))
            if not count:
                raise ConversionError(T("{0}: tablo bulunamadı.", p.name))
            out = unique_path(p.parent, p.stem, ".xlsx")
            wb.save(out)
        progress(T("ℹ {0}: {1} tablo Excel'e aktarıldı (her tablo ayrı sayfa).", p.name, count))
        outputs.append(out)
    return outputs


def _cell(v):
    if v is None:
        return None
    s = str(v).replace("\n", " ").strip()
    m = re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s)
    if m:
        import datetime
        try:
            return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            return s
    t = s.replace(".", "").replace(",", ".") if re.fullmatch(r"-?\d{1,3}(\.\d{3})*(,\d+)?", s) else s
    try:
        if re.fullmatch(r"-?\d+(\.\d+)?", t):
            return float(t) if "." in t else int(t)
    except ValueError:
        pass
    return s


def pdf_to_word_exact(paths, progress, **_):
    from . import fixed_layout
    outputs = []
    for p in paths:
        progress(T("Word'e (birebir düzen) çevriliyor: {0}", p.name))
        out = unique_path(p.parent, p.stem, ".docx")
        from .actions import _carry_marks, _collect_notes
        from .actions import unrotate
        with open_pdf(p) as d:            # also the password/corruption check
            unrotate(d)
            notes = _collect_notes(d)
        fixed_layout.convert(p, out, progress)
        # pen marks are already in the page backgrounds here; the notes' text becomes comments
        _carry_marks(out, p.name, [], notes, progress, fixed=True)
        outputs.append(out)
    return outputs


def pdf_to_pptx(paths, progress, **_):
    try:
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.util import Emu, Pt
    except ImportError:
        raise ConversionError(T("python-pptx kurulu değil: pip install python-pptx"))
    import io
    from . import fixed_layout as fl
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            prs = Presentation()
            W, H = doc[0].rect.width, doc[0].rect.height
            prs.slide_width, prs.slide_height = Emu(round(W * 12700)), Emu(round(H * 12700))
            blank = prs.slide_layouts[6]
            for pno, page in enumerate(doc):
                progress(T("{0}: slayt {1}/{2}", p.name, pno + 1, len(doc)))
                sx, sy = W / page.rect.width, H / page.rect.height
                blocks = fl._lines(page)
                slide = prs.slides.add_slide(blank)
                bg = fl._background(doc, pno, blocks)
                slide.shapes.add_picture(io.BytesIO(bg), 0, 0, prs.slide_width, prs.slide_height)
                for b in blocks:
                    for g in fl._stack_groups(b):
                        box = pymupdf.Rect(g[0][0])
                        for r, _ in g[1:]:
                            box |= r
                        tb = slide.shapes.add_textbox(Emu(round(box.x0 * sx * 12700)), Emu(round(box.y0 * sy * 12700)),
                                                      Emu(round(box.width * sx * 12700 * 1.12 + 50000)),
                                                      Emu(round(box.height * sy * 12700 + 20000)))
                        tf = tb.text_frame
                        tf.word_wrap = False
                        tf.margin_left = tf.margin_right = tf.margin_top = tf.margin_bottom = 0
                        for li, (r, spans) in enumerate(g):
                            para = tf.paragraphs[0] if li == 0 else tf.add_paragraph()
                            for text, font, size, bold, italic, color in spans:
                                run = para.add_run()
                                run.text = text
                                f = run.font
                                f.size = Pt(size * sy)
                                f.bold, f.italic = bold, italic
                                f.name = fl.map_font(font)
                                f.color.rgb = RGBColor((color >> 16) & 255, (color >> 8) & 255, color & 255)
            out = unique_path(p.parent, p.stem, ".pptx")
            prs.save(out)
        outputs.append(out)
    return outputs


# ============================================================ images


def _open_image(path: Path):
    from PIL import Image
    try:
        import pillow_heif  # iPhone HEIC
        pillow_heif.register_heif_opener()
    except ImportError:
        if path.suffix.lower() in (".heic", ".heif"):
            raise ConversionError(T("HEIC için pillow-heif gerekli: pip install pillow-heif"))
    from PIL import ImageOps
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)  # orientation of phone photos
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        return bg
    return im.convert("RGB")


def images_to_jpg(paths, progress, **_):
    outputs = []
    for p in paths:
        progress(T("JPG'ye çevriliyor: {0}", p.name))
        im = _open_image(p)
        out = unique_path(p.parent, p.stem, ".jpg")
        im.save(out, "JPEG", quality=90, optimize=True)
        outputs.append(out)
    return outputs


def shrink_images(paths, progress, max_side: int = 1920, **_):
    outputs = []
    for p in paths:
        progress(T("Küçültülüyor: {0}", p.name))
        im = _open_image(p)
        before = p.stat().st_size
        im.thumbnail((max_side, max_side))
        out = unique_path(p.parent, f"{p.stem}_kucuk", ".jpg")
        im.save(out, "JPEG", quality=80, optimize=True)
        after = out.stat().st_size
        from .actions import human_size
        progress(T("ℹ {0}: {1} → {2}, en fazla {3} px", p.name, human_size(before), human_size(after), max_side))
        outputs.append(out)
    return outputs


# ============================================================ page organizer


def merge_files(paths, progress, ordered: bool = False, image_mode: str = "a4", **_):
    """Turns PDF, image and Word/Excel/PowerPoint files into one PDF in the given order (without a window).
    Office files are converted in a single LibreOffice call; every file becomes a bookmark."""
    from .actions import sorted_by_name
    from .organizer_model import Document
    paths = list(paths) if ordered else sorted_by_name(list(paths))
    doc = Document()
    doc.image_mode = image_mode
    errors = doc.add_files(paths, progress=progress)
    if not doc.pages:
        raise ConversionError("\n".join(errors) or T("Birleştirilecek sayfa yok."))
    folder, stem = doc.default_target()
    outs = doc.save(folder, stem, progress)
    for e in errors:
        progress(T("ℹ atlandı → {0}", e))
    progress(T("ℹ {0} dosya, {1} sayfa birleştirildi; her dosya bir yer imi.", len(paths) - len(errors), len(doc.pages)))
    return outs


def organize(paths, progress, **_):
    try:
        import tkinter  # noqa: F401
    except ImportError:
        raise ConversionError(T("Sayfa düzenleyici pencere gerektirir (Linux: sudo apt install python3-tk)."))
    from .organizer import run
    return run(list(paths), progress)


# ============================================================ compress to target size

# (images above this dpi are downsampled, target dpi, JPEG quality) — from quality towards compromise
_TARGET_STEPS = [(400, 300, 88), (300, 220, 85), (220, 180, 82), (170, 150, 78), (150, 130, 70), (130, 110, 62), (110, 96, 55),
                 (96, 84, 48), (84, 72, 42), (72, 60, 36), (60, 50, 30)]


def _parse_mb(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(mb|m|kb|k)?\s*", (text or "").lower())
    if not m:
        raise ConversionError(T("Anlaşılmayan boyut: '{0}' (örnek: 2, 1.5, 500kb)", text))
    v = float(m.group(1).replace(",", "."))
    mb = v / 1024 if (m.group(2) or "").startswith("k") else v
    if mb <= 0.02:
        raise ConversionError(T("Hedef boyut çok küçük."))
    return mb


def _size_tag(text: str) -> str:
    """'2' → '2MB', '1.5' → '1,5MB', '300kb' → '300KB' (for the file name)."""
    mb = _parse_mb(text)
    if mb < 1:
        return f"{round(mb * 1024)}KB"
    return (f"{mb:g}".replace(".", ",")) + "MB"


def _try_compress(src: Path, dst: Path, step) -> int:
    threshold, target, quality = step
    with pymupdf.open(src) as doc:
        if hasattr(doc, "rewrite_images"):
            doc.rewrite_images(dpi_threshold=threshold, dpi_target=target, quality=quality)
        try:
            doc.subset_fonts()          # keep only the glyphs that are used
        except Exception:
            pass
        doc.save(dst, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                 clean=True, use_objstms=1)
    return dst.stat().st_size


def _rasterize(src: Path, dst: Path, dpi: int, quality: int) -> int:
    with pymupdf.open(src) as doc:
        out = pymupdf.open()
        for page in doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if dpi <= 100 else pymupdf.csRGB)
            n = out.new_page(width=page.rect.width, height=page.rect.height)
            n.insert_image(n.rect, stream=pix.tobytes("jpeg", jpg_quality=quality))
        out.save(dst, garbage=4, deflate=True)
        out.close()
    return dst.stat().st_size


def compress_to_target(paths, progress, target: str = "2", **_):
    import shutil
    import tempfile
    from .actions import human_size
    limit = int(_parse_mb(str(target)) * 1024 * 1024)
    outputs = []
    for p in paths:
        with open_pdf(p):
            pass                                   # password / corruption check
        size = p.stat().st_size
        label = human_size(limit)
        out = unique_path(p.parent, f"{p.stem}_{_size_tag(str(target))}_alti", ".pdf")
        if size <= limit:
            shutil.copy2(p, out)
            progress(T("ℹ {0}: zaten {1}, {2} altında; aynen kopyalandı.", p.name, human_size(size), label))
            outputs.append(out)
            continue
        with tempfile.TemporaryDirectory(prefix="riffle_hedef_") as tmp:
            best = None
            # Binary search: the HIGHEST quality step that meets the target
            lo, hi = 0, len(_TARGET_STEPS) - 1
            tried = {}
            while lo <= hi:
                mid = (lo + hi) // 2
                cand = Path(tmp) / f"s{mid}.pdf"
                progress(T("{0}: deneniyor (kalite {1}, {2} dpi)", p.name, _TARGET_STEPS[mid][2], _TARGET_STEPS[mid][1]))
                tried[mid] = _try_compress(p, cand, _TARGET_STEPS[mid])
                if tried[mid] <= limit:
                    best, hi = cand, mid - 1
                else:
                    lo = mid + 1
            note = ""
            if best is None:
                # Last resort: turn the pages into images (text is no longer selectable)
                for dpi, q in ((110, 55), (90, 45), (72, 35)):
                    cand = Path(tmp) / f"r{dpi}.pdf"
                    progress(T("{0}: sayfalar görüntüye çevriliyor ({1} dpi)", p.name, dpi))
                    if _rasterize(p, cand, dpi, q) <= limit:
                        best = cand
                        note = T(" Sayfalar görüntüye çevrildi: metin artık seçilemez.")
                        break
            if best is None:
                smallest = min(tried.values())
                raise ConversionError(T("{0}: {1} altına inmedi (en küçük {2}). Sayfa sayısı çok fazla olabilir; önce böl.", p.name, label, human_size(smallest)))
            shutil.copy2(best, out)
        progress(T("ℹ {0}: {1} → {2} (hedef {3}).{4}", p.name, human_size(size), human_size(out.stat().st_size), label, note))
        outputs.append(out)
    return outputs


# ============================================================ photo to document


def photos_to_document(paths, progress, ordered: bool = False, **_):
    from .scan import photos_to_pdf
    from .actions import sorted_by_name
    paths = list(paths) if ordered else sorted_by_name(list(paths))
    stem = paths[0].stem if len(paths) == 1 else f"{paths[0].stem}_ve_{len(paths) - 1}_foto"
    out = unique_path(paths[0].parent, f"{stem}_belge", ".pdf")
    info = photos_to_pdf(paths, out, progress)
    notes = []
    if info["corners"] < info["pages"]:
        notes.append(T("{0} fotoğrafta kâğıt kenarı bulunamadı, olduğu gibi temizlendi", info['pages'] - info['corners']))
    if info["words"] == -1:
        notes.append(T("Tesseract yok: metin seçilemez (OCR için README'ye bak)"))
    elif info["words"]:
        notes.append(T("{0} kelime tanındı, metin aranabilir", info['words']))
    progress(T("ℹ {0}: {1} sayfa", out.name, info['pages']) + (" · " + " · ".join(notes) if notes else ""))
    return [out]


# ============================================================ folder to one PDF


_SKIP = re.compile(r"^(~\$|\.|Thumbs\.db$|desktop\.ini$)", re.I)


def _natural(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def folder_to_pdf(paths, progress, **_):
    """Merges the PDF, image and Office files in the folder (including subfolders) into one PDF in
    natural order. Every file becomes a bookmark, every subfolder a parent bookmark.
    The output is written NEXT TO the folder (so the next run doesn't include it)."""
    import tempfile
    from .actions import (IMAGES, OFFICE, PDF, append_with_bookmark, office_to_pdf_many, save_with_toc)
    outputs = []
    for folder in paths:
        folder = Path(folder)
        if not folder.is_dir():
            raise ConversionError(T("{0} bir klasör değil.", folder.name))
        out_doc, toc, notes = pymupdf.open(), [], []
        counts = {"pdf": 0, "görsel": 0, "office": 0}
        with tempfile.TemporaryDirectory(prefix="riffle_klasor_") as tmp:
            # All Office files are converted up front in one LibreOffice call (reopening the program
            # for each one was the slowest part)
            office_files = [e for e in folder.rglob("*") if e.is_file()
                            and e.suffix.lower().lstrip(".") in OFFICE
                            and not any(_SKIP.match(part) for part in e.relative_to(folder).parts)]
            office_pdf: dict = {}
            office_err = ""
            if office_files:
                try:
                    office_pdf = office_to_pdf_many(office_files, Path(tmp) / "office", progress)
                except ConversionError as err:
                    office_err = str(err).splitlines()[0]

            def walk(d: Path, level: int):
                entries = sorted((e for e in d.iterdir() if not _SKIP.match(e.name)),
                                 key=lambda e: (_natural(e.name)))
                for e in entries:
                    if e.is_dir():
                        before, mark = len(out_doc), len(toc)
                        toc.append([level, e.name, len(out_doc) + 1])
                        walk(e, level + 1)
                        if len(out_doc) == before:       # empty subfolder: undo its bookmark
                            del toc[mark:]
                        continue
                    ext = e.suffix.lower().lstrip(".")
                    rel = e.relative_to(folder)
                    try:
                        if ext in PDF:
                            progress(T("Ekleniyor: {0}", rel))
                            with open_pdf(e) as src:
                                append_with_bookmark(out_doc, src, e.stem, level, toc)
                            counts["pdf"] += 1
                        elif ext in IMAGES:
                            progress(T("Ekleniyor: {0}", rel))
                            from .organizer_model import Document
                            doc = Document()
                            doc.add_files([e])
                            part = doc.save(Path(tmp), f"g{len(out_doc)}")[0]
                            with pymupdf.open(part) as src:
                                append_with_bookmark(out_doc, src, e.stem, level, toc)
                            counts["görsel"] += 1
                        elif ext in OFFICE:
                            pdf = office_pdf.get(e)
                            if pdf is None:
                                raise ConversionError(office_err or T("{0} çevrilemedi", e.name))
                            progress(T("Ekleniyor: {0}", rel))
                            with pymupdf.open(pdf) as src:
                                append_with_bookmark(out_doc, src, e.stem, level, toc)
                            counts["office"] += 1
                    except ConversionError as err:
                        notes.append(f"{rel}: {str(err).splitlines()[0]}")

            walk(folder, 1)
            if not len(out_doc):
                raise ConversionError(T("{0}: birleştirilecek PDF, görsel ya da Office dosyası yok.", folder.name))
            out = unique_path(folder.parent, folder.name, ".pdf")
            save_with_toc(out_doc, toc, out)
            out_doc.close()
        summary = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
        progress(T("ℹ {0}: {1}; her dosya bir yer imi.", out.name, summary))
        for n in notes:
            progress(T("ℹ atlandı → {0}", n))
        outputs.append(out)
    return outputs
