"""Word / Excel / PowerPoint / CSV araçları: Office dosyasını küçült, Word'leri Word olarak birleştir,
CSV'yi Excel'e düzgün çevir, sunumdan not çıktısı, Office dosyasındaki resimleri çıkar."""

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


# ============================================================ Office dosyasını küçült

def _shrink_image(data: bytes, ext: str, max_side: int, quality: int) -> tuple[bytes, str]:
    """(yeni veri, yeni uzantı). Küçülmüyorsa eskisini döndürür. Saydam PNG'ler PNG kalır;
    saydamlığı olmayan büyük PNG'ler (ekran görüntüsü, fotoğraf) JPEG olur."""
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
        if ext == ".png":                      # çizim/ekran görüntüsü: PNG çoğu zaman daha küçük ve net
            png = io.BytesIO()
            (im if im.mode in ("P", "L") else rgb).save(png, "PNG", optimize=True)
            # JPEG yalnızca belirgin kazançta (yazı kenarları bulanmasın)
            candidates.append((png, ".png"))
            if len(png.getvalue()) <= 1.6 * len(jpg.getvalue()):
                candidates = [candidates[1]]
        out, new_ext = min(candidates, key=lambda c: len(c[0].getvalue()))
    new = out.getvalue()
    if len(new) >= len(data) * 0.95:
        return data, ext
    return new, new_ext


def _rewrite_zip(src: Path, dst: Path, renames: dict[str, str], replaced: dict[str, bytes]) -> None:
    """Medya dosyalarını değiştirip yeniden adlandırarak Office paketini yeniden yaz:
    ilişki (.rels) hedefleri ve [Content_Types].xml uzantıları güncellenir."""
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
                for o, n in renames.items():           # nadir: medyaya özel Override kaydı
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
    """Çıktı gerçekten açılıyor mu (bozuk dosya bırakmayalım)."""
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


# ============================================================ Word belgelerini Word olarak birleştir

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
        master.add_page_break()            # her belge yeni sayfadan başlasın
        comp.append(doc)
    out = unique_path(paths[0].parent, f"{paths[0].stem}_birlesik", ".docx")
    comp.save(str(out))
    progress(T("ℹ {0} belge birleştirildi; biçimler, resimler ve numaralı listeler korunur", len(paths)))
    return [out]


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
    # UTF-8 değilse eski Windows kodlaması: Türkçe (1254) ile Batı Avrupa (1252) yalnızca 6 baytta ayrılır
    # (ğ ı ş İ Ğ Ş ↔ ð ý þ Ý Ð Þ). Almanca/Fransızca/İspanyolca dosyalarda bu baytlar yok: ikisi de doğru.
    # Bu baytlar varsa: Türkçede olmayan harfler (á é í ó ú ß ñ æ ø …) belirgin sayıdaysa Batı Avrupa
    # (İzlandaca vb.), değilse Türkçe (Türkçe metinde ı/ş/ğ çok sık, yabancı harf ancak tek tük).
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
    """(sayı biçimi, tarih sırası) — sütundaki kanıta göre.
    Sayı: 'tr' (1.234,56; Avrupa'nın çoğu) ya da 'en' (1,234.56; ABD/İngiltere).
    Tarih: 'dmy' (gün önce), 'mdy' (ABD, ay önce) ya da 'text' (karar verilemedi: dönüştürme)."""
    vals = [v.strip() for v in values]
    tr = sum(1 for v in vals if _TR_NUM.match(v) and not _EN_NUM.match(v))
    en = sum(1 for v in vals if _EN_NUM.match(v) and not _TR_NUM.match(v))
    dates = [m for m in (_DATE.match(v) for v in vals) if m]
    order = "dmy"
    if any(int(m[1]) > 12 for m in dates):
        order = "dmy"                                   # 25/12/2026: ilk sayı gün
    elif any(int(m[3]) > 12 for m in dates):
        order = "mdy"                                   # 12/25/2026: ikinci sayı gün (ABD)
    elif any(m[2] == "/" for m in dates):
        order = "text"                                  # yalnızca 03/11/2026 gibi: 3 Kasım mı 11 Mart mı belli değil
    return ("tr" if tr >= en else "en"), order


def _convert(v: str, style: tuple[str, str]):
    s = v.strip()
    if not s:
        return None
    if _INT.match(s):
        digits = s.lstrip("-")
        # baştaki sıfır (telefon, posta kodu) ya da 11+ hane (TC kimlik, hesap no): metin kalsın
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


# ============================================================ Sunumdan not çıktısı

def slide_handout(paths: list[Path], progress: Progress, per_page: int = 3, **_) -> list[Path]:
    """Sayfa başına 3 slayt (solda), yanlarında not çizgileri (sağda). A4 dikey."""
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


# ============================================================ Office dosyasındaki resimleri çıkar

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
