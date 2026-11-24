"""Tüm dönüştürme işlemleri.

Her işlem bir dosya listesi alır, çıktı dosyalarının listesini döndürür.
Çıktılar her zaman kaynak dosyanın yanına yazılır, var olan dosyanın
üstüne asla yazılmaz (dosya (1).pdf, dosya (2).pdf ...).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

try:
    import pymupdf  # PyMuPDF >= 1.24
except ImportError:
    try:
        import fitz as pymupdf  # type: ignore  # eski sürümler
    except ImportError:
        # install.py, kütüphaneler henüz kurulmadan menü listesini okuyabilsin diye.
        # Dönüşüm sırasında .venv içinden çalıştığımız için orada her zaman kurulu olur.
        pymupdf = None  # type: ignore

PDF = ["pdf"]
OFFICE = ["doc", "docx", "odt", "rtf", "txt",
          "xls", "xlsx", "ods", "csv",
          "ppt", "pptx", "odp",
          "html", "htm"]
IMAGES = ["jpg", "jpeg", "png", "bmp", "webp", "tif", "tiff", "gif", "heic", "heif"]
FOLDER = "<klasör>"   # klasöre sağ tık işlemleri için sözde uzantı
OOXML = ["docx", "pptx", "xlsx"]          # içi zip olan yeni Office biçimleri
SLIDES = ["ppt", "pptx", "odp"]

Progress = Callable[[str], None]


class ConversionError(Exception):
    """Kullanıcıya gösterilecek anlaşılır hata."""


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------

# Ayarlardaki "çıktıları Masaüstüne/şu klasöre kaydet" için: {kaynak klasör: hedef klasör} (main doldurur)
OUTPUT_REDIRECT: dict = {}


def _redirect(folder: Path) -> Path:
    try:
        return OUTPUT_REDIRECT.get(Path(folder).resolve(), folder)
    except OSError:
        return folder


def unique_path(folder: Path, stem: str, ext: str) -> Path:
    """folder/stem.ext boşsa onu, doluysa 'stem (1).ext' gibi bir yol verir."""
    folder = _redirect(folder)
    ext = ext if ext.startswith(".") else "." + ext
    p = folder / f"{stem}{ext}"
    i = 1
    while p.exists():
        p = folder / f"{stem} ({i}){ext}"
        i += 1
    return p


def unique_dir(folder: Path, name: str) -> Path:
    folder = _redirect(folder)
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
        raise ConversionError(f"{path.name} açılamadı (bozuk PDF olabilir): {e}")
    if doc.needs_pass:
        if not password or not doc.authenticate(password):
            doc.close()
            raise ConversionError(f"{path.name} şifreli. Önce 'Şifreyi kaldır' kullan.")
    return doc


def sorted_by_name(paths: list[Path]) -> list[Path]:
    # Windows Gezgini'ndeki gibi doğal sıralama: sayfa2 < sayfa10
    import re

    def key(p: Path):
        return [int(t) if t.isdigit() else t.lower()
                for t in re.split(r"(\d+)", p.name)]
    return sorted(paths, key=key)


# --------------------------------------------------------------------------
# PDF işlemleri
# --------------------------------------------------------------------------

def append_with_bookmark(out_doc, src_doc, title: str, level: int, toc: list) -> None:
    """src_doc'u out_doc'un sonuna ekler; 'title' adlı yer imi ekler ve kaynağın kendi
    içindekiler tablosunu bir alt seviyeye kaydırarak korur."""
    start = len(out_doc) + 1                    # yer imleri 1 tabanlı sayfa numarası kullanır
    out_doc.insert_pdf(src_doc)
    toc.append([level, title, start])
    try:
        for lvl, t, pg, *_ in src_doc.get_toc(simple=False):
            if pg >= 1:
                toc.append([level + lvl, t, start + pg - 1])
    except Exception:
        pass


def fix_tounicode(doc) -> int:
    """PyMuPDF'nin gömdüğü fontlarda ToUnicode 'bfrange' satırları hedefin son baytını taşırıyor
    (ör. <0062> <00ff> <00a0>: ı = 0x131 olmalı). Standart okuyucular (Acrobat, Ghostscript) son baytı
    sardığı için 'ı' → '1', 'ğ' → 'á' kopyalanır. Taşan aralıkları bölerek düzeltir; düzeltilen font sayısı."""
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
                        room = 0xFF - (d & 0xFF)               # hedefin son baytı taşmadan kaç adım
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
            new = _re.sub(r"\d+\s+beginbfrange.*?endbfrange\s*", "", cmap, flags=_re.S)   # eski aralıkları sil
            new = new.replace("endcmap", "\n".join(body) + "\nendcmap", 1)
            doc.update_stream(tu, new.encode("latin-1"))
            fixed += 1
    return fixed


def save_with_toc(out_doc, toc: list, path: Path) -> None:
    if toc:
        try:
            out_doc.set_toc(toc)
        except Exception:                        # bozuk kaynak içindekiler → yalnızca dosya başlıkları
            out_doc.set_toc([e for e in toc if e[0] == 1])
    out_doc.save(path, garbage=3, deflate=True)


def merge_pdfs(paths: list[Path], progress: Progress, ordered: bool = False, **_) -> list[Path]:
    if len(paths) < 2:
        raise ConversionError("Birleştirmek için en az 2 PDF seç.")
    if not ordered:                      # sıralama penceresinden geldiyse kullanıcının sırası korunur
        paths = sorted_by_name(paths)
    out_doc, toc = pymupdf.open(), []
    for p in paths:
        progress(f"Ekleniyor: {p.name}")
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
                progress(f"{p.name}: sayfa {i + 1}/{n}")
                part = pymupdf.open()
                part.insert_pdf(src, from_page=i, to_page=i)
                part.save(folder / f"{p.stem}_s{i + 1:0{width}d}.pdf",
                          garbage=3, deflate=True)
                part.close()
            outputs.append(folder)
    return outputs


COMPRESS_LEVELS = {
    # seviye: (bu dpi'nin üstündekiler küçültülür, hedef dpi, JPEG kalitesi)
    "normal": (160, 150, 70),   # ekranda okumak ve yazdırmak için yeterli
    "strong": (110, 96, 50),    # e-posta / sisteme yükleme sınırları için; yazdırmada bulanık
}


def compress_pdf(paths: list[Path], progress: Progress, level: str = "normal", **_) -> list[Path]:
    threshold, target, quality = COMPRESS_LEVELS[level]
    suffix = "_sikistirilmis" if level == "normal" else "_cok_sikistirilmis"
    outputs = []
    for p in paths:
        progress(f"Sıkıştırılıyor: {p.name}")
        before = p.stat().st_size
        with open_pdf(p) as doc:
            if hasattr(doc, "rewrite_images"):
                doc.rewrite_images(dpi_threshold=threshold, dpi_target=target, quality=quality)
            out = unique_path(p.parent, f"{p.stem}{suffix}", ".pdf")
            doc.save(out, garbage=4, deflate=True, deflate_images=True,
                     deflate_fonts=True, clean=True, use_objstms=1)
        after = out.stat().st_size
        if after >= before:
            # Kazanç yoksa daha büyük dosya bırakma
            shutil.copy2(p, out)
            progress(f"ℹ {p.name} zaten optimize, küçültülemedi.")
        else:
            pct = 100 * (before - after) / before
            progress(f"ℹ {p.name}: {human_size(before)} → {human_size(after)} (%{pct:.0f} küçüldü)")
        outputs.append(out)
    return outputs


def rotate_pdf(paths: list[Path], progress: Progress, angle: int = 90, **_) -> list[Path]:
    outputs = []
    for p in paths:
        progress(f"Döndürülüyor: {p.name}")
        with open_pdf(p) as doc:
            for page in doc:
                page.set_rotation((page.rotation + angle) % 360)
            out = unique_path(p.parent, f"{p.stem}_dondurulmus_{angle % 360}", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        outputs.append(out)
    return outputs


def protect_pdf(paths: list[Path], progress: Progress, password: str = "", **_) -> list[Path]:
    if not password:
        raise ConversionError("Şifre girilmedi.")
    outputs = []
    for p in paths:
        progress(f"Şifreleniyor: {p.name}")
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
        progress(f"Şifre kaldırılıyor: {p.name}")
        doc = pymupdf.open(p)
        try:
            if doc.needs_pass and not doc.authenticate(password):
                raise ConversionError(f"{p.name}: şifre yanlış.")
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
                progress(f"{p.name}: sayfa {i + 1}/{n}")
                pix = page.get_pixmap(dpi=dpi, alpha=False)
                pix.save(folder / f"{p.stem}_s{i + 1:0{width}d}.jpg", jpg_quality=90)
            outputs.append(folder)
    return outputs


def pdf_to_word(paths: list[Path], progress: Progress, mode: str = "auto", **_) -> list[Path]:
    """mode: "auto" (slayt ve iki sütunlu makale → birebir düzen, gerisi akan metin) · "flow" (hep akan)."""
    try:
        from pdf2docx import Converter
    except ImportError:
        raise ConversionError("pdf2docx kurulu değil: pip install pdf2docx")
    import logging
    logging.getLogger().setLevel(logging.ERROR)  # pdf2docx çok konuşkan
    outputs = []
    for p in paths:
        progress(f"Word'e çevriliyor: {p.name} (uzun sürebilir)")
        out = unique_path(p.parent, p.stem, ".docx")
        with tempfile.TemporaryDirectory(prefix="riffle_w_") as tmp:
            src = _prepare_pdf(p, Path(tmp) / "kaynak.pdf", progress)
            from . import fixed_layout, reflow
            kind = None
            if mode != "flow":
                with pymupdf.open(src) as probe:
                    if fixed_layout.looks_like_slides(probe):
                        kind = "sunum"
                    elif reflow.is_two_column(probe):
                        kind = "iki sütunlu belge"
            if kind:
                # Slayt ve iki sütunlu makale: akan metinde satırlar/sayfalar kayıyor, birebir düzen kullan
                progress(f"ℹ {p.name}: {kind} algılandı, birebir düzen kullanıldı (yazılar yerinde "
                         "düzenlenebilir). Akan metin için: 'PDF → Word (akan metin)'.")
                fixed_layout.convert(src, out, progress)
                outputs.append(out)
                continue
            with pymupdf.open(src) as d:
                info = reflow.analyze_pages(d)
                info.rules = [r for pg in d for r in reflow.find_rules(pg)]
                rescued = reflow.rescue_figures(d)
                if rescued:
                    src = Path(tmp) / "figurlu.pdf"
                    d.save(src)
                    progress(f"ℹ {p.name}: {rescued} figür resim olarak aktarıldı.")
            from .textfix import patched_text
            from . import parallel
            # 0.7: ortalanmış başlık + alt başlığın tek satıra yapışmasını önler
            settings = {"line_break_width_ratio": 0.7}
            with pymupdf.open(src) as probe:
                n_pages = len(probe)
            if n_pages >= 30 and not parallel.in_worker() and parallel.workers_for(n_pages // 10) > 1 \
                    and _parallel_enabled():
                try:
                    fixes = parallel.word_parallel(src, out, n_pages, settings, Path(tmp), progress)
                except parallel.Cancelled:
                    raise
                except Exception:                   # süreç havuzu kurulamadı: sıradan yola düş
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
                progress(f"ℹ {p.name}: {fixes['glyph']} özel karakter (μ, α, ± …) kurtarıldı.")
        _unclip_table_rows(out, info)
        outputs.append(out)
    return outputs


def _parallel_enabled() -> bool:
    try:
        from .settings import load
        return bool(load()["parallel"])
    except Exception:
        return True


def _run_cancellable(cmd, timeout: float, flags: int = 0):
    """subprocess.run gibi, ama İptal'e basılınca dış programı (LibreOffice, Ghostscript) durdurur."""
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
    """pdf2docx'e vermeden önce PDF'i hazırlar. Değişiklik yoksa orijinali döndürür.

    - El yazısı (Ink) notlarını kaldırır: kalemle atılan tikler Word'de serbest
      şekil olup metnin yerleşimini bozuyor. Vurgulamalar (Highlight) korunur.
    - Metni olmayan sayfaları (taranmış, el yazısı, çizim) sayfa resmine çevirir:
      pdf2docx vektör çizgileri atlıyor; aksi hâlde sayfa boş ya da kayıp çıkıyor.
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
        # Kırpılmış sayfalar (büyük tuvalden kesilmiş, ör. dergi PDF'leri): pdf2docx
        # kırpma ofsetini hesaba katmıyor, resimler 'sayfa dışında' kalıp düşüyor.
        cropped = {i for i, page in enumerate(doc)
                   if page.cropbox != page.mediabox or page.mediabox.x0 or page.mediabox.y0}
        if not ink and not textless and not cropped:
            return src
        if ink:
            progress(f"ℹ {src.name}: {ink} el yazısı işaret (kalem/tik) Word'e aktarılmadı.")
        # Metinsiz sayfalar: Tesseract varsa OCR → metin ağırlıklıysa düzenlenebilir metin
        ocr_words, ocr_used = {}, set()
        if textless:
            from . import ocr
            try:
                ocr_words = ocr.ocr_pages(doc, sorted(textless), progress)
                ocr_used = {i for i, w in ocr_words.items() if ocr.is_text_page(w)}
                # eğik basılı taramayı düzelt ve yeniden oku: satırlar ve tablo çizgileri eksenlere
                # otursun (eğik çizgiler Word'de kayık, ince ince bölünmüş sütunlu tablolar üretiyordu).
                # Yalnızca metin sayfalarında: el yazısında eğiklik ölçümü güvenilmez.
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
                progress(f"ℹ OCR yapılamadı, taranmış sayfalar resim olarak aktarılacak. {str(e).splitlines()[0]}")
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
                    # sayfayı kırpılmış hâliyle sıfır kökenli yeni sayfaya yerleştir
                    new = out.new_page(width=page.rect.width, height=page.rect.height)
                    new.show_pdf_page(new.rect, doc, i)
                else:
                    out.insert_pdf(doc, from_page=i, to_page=i, annots=True)
            out.save(dst)
            out.close()
            if ocr_used:
                progress(f"ℹ {src.name}: {len(ocr_used)} taranmış sayfa OCR ile metne çevrildi "
                         "(resim/imza gibi öğeler bu sayfalarda aktarılmaz).")
            if textless - ocr_used:
                progress(f"ℹ {src.name}: {len(textless - ocr_used)} sayfada metin yok, resim olarak aktarıldı.")
        else:
            doc.save(dst)
        return dst
    finally:
        doc.close()


_SLACK = 1440  # twip = 1 inç (2,5 cm) pay
_CELL_BEFORE = 40  # twip: serbest satırlarda hücre içi üst boşluk sınırı
_MIN_CELL = 1000  # twip ≈ 1,8 cm: bundan dar hücreli satırlar sabit kalır


def _fix_layout_xml(xml: str, info=None) -> str:
    import re
    # 1) Tablo satırları 'tam yükseklik' → 'en az': yazı uzarsa son satır kesilmesin.
    #    YALNIZCA tüm hücreleri geniş satırlarda: slaytlardaki dar hücreler
    #    (harfler alt alta) serbest kalırsa sayfa onlarca satır uzuyor.
    def unclip_row(m: re.Match) -> str:
        tr = m.group(0)
        if "<w:tbl>" in tr:  # iç içe tablo: dokunma
            return tr
        widths = [int(w) for w in re.findall(r'<w:tcW[^>]*w:w="(\d+)"', tr)]
        if widths and min(widths) >= _MIN_CELL and 'w:hRule="exact"' in tr:
            tr = tr.replace('<w:trHeight w:hRule="exact"', '<w:trHeight w:hRule="atLeast"')
            # pdf2docx metni hücre içinde 'önce boşluk' ile konumluyor; satır serbest
            # kalınca bu boşluk satırı şişirip sayfayı taşırıyor. Satırın en az
            # yüksekliği zaten korunduğu için boşluğu küçült.
            tr = re.sub(r'w:before="(\d+)"',
                        lambda b: f'w:before="{min(int(b.group(1)), _CELL_BEFORE)}"', tr)
        return tr
    xml = re.sub(r'<w:tr[ >].*?</w:tr>', unclip_row, xml, flags=re.S)

    # 1b) pdf2docx tablo ızgarasını (gridCol) tüm sütunlara EŞİT yazıyor, gerçek
    #     genişlikler hücrelerde (tcW). Sabit düzenli tabloda Word/LibreOffice
    #     ızgarayı kullandığı için geniş sütun daralıyor, metin alt alta kayıyor.
    xml = re.sub(r'<w:tbl>.*?</w:tbl>', _fix_table_grid, xml, flags=re.S)

    # 1c) Şemaya aykırı değerler: ondalıklı ölçüler ("6.0", "14.000000000000057")
    #     ve '#' önekli renkler. Word bunlarda "okunamayan içerik" uyarısı verebiliyor.
    xml = re.sub(r'(w:(?:w|sz|space|val)=")(-?\d+)\.\d+"', r'\1\2"', xml)
    xml = re.sub(r'(w:(?:color|fill)=")#([0-9A-Fa-f]{6})"', r'\1\2"', xml)

    # 1c2) Dolu biten sayfalarda metni akıt, üst/alt bilgileri sil (bkz. reflow.py)
    if info is not None:
        from . import reflow
        xml = reflow.reflow_sections(xml, info)

    # 1d) Çok sütunlu sayfalar: pdf2docx sol ve sağ sütunu ayrı bölüm yapıp
    #     'sonraki sütun' bölüm sonu kullanıyor. LibreOffice bunu sayfa sonu
    #     sayıyor (her PDF sayfası 2 sayfa oluyor). Tek bölüm + sütun sonu yap.
    xml = _merge_column_sections(xml)

    # 2) pdf2docx her PDF sayfasını ayrı bölüm yapıyor ve sayfa sonundaki öğeyi
    #    (genelde sayfa numarası) büyük bir 'önce boşluk' ile aşağı itiyor.
    #    Yukarıdaki satırlar uzayınca bu öğe tek başına yeni sayfaya taşıyor.
    #    Bölüm sonundan hemen önceki paragrafın boşluğunu 1 inç kısalt.
    def shrink(m: re.Match) -> str:
        before = int(m.group(2))
        if before >= 2 * _SLACK:
            return f'{m.group(1)}{before - _SLACK}"'
        return m.group(0)

    #    Ara bölümler '<w:p><w:pPr><w:sectPr>' ile, SON bölüm ise gövdenin
    #    sonunda '</w:p><w:sectPr' ile biter; ikisi de ele alınmalı.
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

    # 3) Bölüm sonu paragrafının kendisi bir satır yer kaplamasın
    xml = xml.replace('<w:p><w:pPr><w:sectPr>',
                      '<w:p><w:pPr><w:spacing w:before="0" w:after="0" w:line="20" w:lineRule="exact"/><w:sectPr>')

    # 3b) Boşluksuz PDF'ler (eski Distiller vb.): kelimeler ayrı koşu, aralarında
    #     boşluk karakteri yok → Word'de 'Electricaldischargemachining'.
    xml = _restore_spaces(xml)
    # textfix'in satır sonu tiresi yerine bıraktığı görünmez işaret: artık gerek yok
    xml = xml.replace("\u2060", "")
    xml = re.sub(r"<w:t( xml:space=\"preserve\")?></w:t>", "<w:t/>", xml)

    # 4) Madde/numara işaretleri: araya sekme koy, birleşmiş maddeleri ayır
    xml = re.sub(r'<w:p(?: [^>]*)?>.*?</w:p>', lambda m: _fix_list_para(m.group(0)), xml, flags=re.S)
    # 5) Kaybolan yatay ayırıcı çizgiler → paragraf kenarlığı
    if info is not None and getattr(info, "rules", None):
        xml = _apply_rules(xml, info.rules)
    return xml


_PPR_AFTER_BDR = ("shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap", "overflowPunct", "topLinePunct",
                  "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind",
                  "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
                  "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr", "sectPr",
                  "pPrChange")


def _apply_rules(xml: str, rules) -> str:
    """Her çizgiyi metni eşleşen paragrafa alt/üst kenarlık olarak ekler (belge sırasıyla)."""
    from .reflow import _norm_text
    # tablo hücresindeki paragrafa kenarlık hücrenin yarısında kalır: tablolar hariç
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
        order = list(range(start, len(paras))) + list(range(0, start))   # sütunlarda sıra bozulabilir
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
        """Kenarlık metinle çizgi arasını paragrafa ekler; aynı miktar komşu boşluktan düşülür ki
        sayfanın geri kalanı aşağı kaymasın."""
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
                # ortalı başlıkta pdf2docx iki yandan girinti veriyor; kenarlık o kadar kısa kalmasın
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
    if tbl.count("<w:tbl>") > 1:  # iç içe tablo: dokunma
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
            out.append(xml[pos:left.start()])           # sol sütun içeriği (bölüm sonu silinir)
            mid = xml[left.end():right.start()]          # sağ sütun içeriği
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
    """Belge 'boşluksuz' ise ardışık kelime koşularının arasına boşluk ekler."""
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
        return xml  # normal belge: dokunma

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
                    # alt/üst simge ("TiO" + "₂") bitişik kalır
                    # satır sonu tiresi textfix'te çözüldü (U+2060 = birleştir); kalan '-' gerçek birleşik kelime
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
    """'•' ya da '1.' ayrı bir koşu (run) olarak gelip metne yapışıyorsa sekme ekler;
    paragrafın ortasında yeni bir '•' varsa orada paragrafı ikiye böler."""
    if len(re.findall(r"<w:p[ >]", p)) > 1 or "<w:sectPr" in p:  # iç içe paragraf (metin kutusu) vb.: dokunma
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

    # pdf2docx alt satırları başa <w:tab/> koyarak hizalıyor; asılı girintide bu
    # sekme metni fazladan itiyor. Satır içi sekmeleri boşluğa çevir.
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

    # Ortadaki madde işaretlerinden (numaralı listede numaralardan) böl
    numbered = bool(_NUMBER.match(first_txt))
    cuts = [i for i in run_idx[1:]
            if _run_text(tokens[i]).strip() in _BULLETS
            or (numbered and re.fullmatch(r'\d{1,2}\.', _run_text(tokens[i]).strip()))]
    groups, prev = [], 0
    for c in cuts:
        groups.append(tokens[prev:c])
        prev = c
    groups.append(tokens[prev:])

    # Tüm maddelere aynı asılı girinti: işaret kendi yerinde kalır, metin ve
    # alt satırlar işaretten sabit uzaklıkta hizalanır. (pdf2docx her maddeye
    # farklı sekme konumu veriyor; sonuç dağınık görünüyordu.)
    ind = re.search(r'<w:ind ([^>]*?)/>', ppr)
    if ind:
        attrs = dict(re.findall(r'w:(\w+)="(-?\d+)"', ind.group(1)))
        left = int(attrs.get("left", 0))
        marker = left - int(attrs.get("hanging", 0)) + int(attrs.get("firstLine", 0))
        gap = 284 if first_txt in _BULLETS else 397  # 0,5 cm / 0,7 cm (twip)
        right = attrs.get("right", "0")
        ppr = ppr.replace(ind.group(0),
                          f'<w:ind w:left="{max(marker, 0) + gap}" w:right="{right}" w:hanging="{gap}"/>')
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
    """pdf2docx çıktısındaki yerleşim sorunlarını düzelt (bkz. _fix_layout_xml)."""
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
        progress(f"Metin çıkarılıyor: {p.name}")
        with open_pdf(p) as doc:
            text = "\n\f".join(page.get_text() for page in doc)
        out = unique_path(p.parent, p.stem, ".txt")
        out.write_text(text, encoding="utf-8")
        if not text.strip():
            progress(f"ℹ {p.name}: metin yok (taranmış PDF olabilir, OCR gerekir).")
        outputs.append(out)
    return outputs


# --------------------------------------------------------------------------
# Görsel → PDF
# --------------------------------------------------------------------------

def images_to_pdf(paths: list[Path], progress: Progress, ordered: bool = False, **_) -> list[Path]:
    if not ordered:
        paths = sorted_by_name(paths)
    out_doc = pymupdf.open()
    for p in paths:
        progress(f"Ekleniyor: {p.name}")
        try:
            img = pymupdf.open(p)
            pdf_bytes = img.convert_to_pdf()
            img.close()
        except Exception:
            # PyMuPDF'in açamadığı formatlar (HEIC, bazı webp) için Pillow
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
    """Windows'ta MS Office kuruluysa onunla çevir. Başarılıysa True."""
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
    """LibreOffice kullanıcı profili. Her seferinde yeni profil açmak (eski davranış) özellikle
    Windows'ta her dönüşüme birkaç saniye ekliyordu; artık kalıcı profil kullanılır. Aynı anda
    ikinci bir dönüşüm çalışıyorsa (profil kilitli) o işlem geçici profile düşer."""

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
                    lock.unlink()                       # çöken işlemden kalmış eski kilit
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


def office_to_pdf_many(paths: list[Path], outdir: Path, progress: Progress = lambda m: None) -> dict:
    """Birden çok Office dosyasını TEK LibreOffice çağrısıyla PDF'e çevirir (her dosya için programı
    yeniden başlatmak en yavaş kısım). {kaynak: pdf} döndürür; çevrilemeyenler sözlükte olmaz."""
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
        for i, p in enumerate(paths):                  # aynı adlı dosyalar çakışmasın: sıra numarası
            dst = src_dir / f"{i:03d}{p.suffix.lower()}"
            shutil.copyfile(p, dst)
            staged.append(dst)
        progress(f"PDF'e çevriliyor: {len(paths)} dosya" if len(paths) > 1 else f"PDF'e çevriliyor: {paths[0].name}")
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
            raise ConversionError(f"{paths[0].name} çevrilemedi.\n{r.stderr.strip()[:300]}")
        return result
    if sys.platform == "win32":
        for i, p in enumerate(paths):
            out = outdir / f"{i:03d}.pdf"
            progress(f"PDF'e çevriliyor: {p.name}")
            if _office_com_to_pdf(p, out):
                result[p] = out
        if result:
            return result
    raise ConversionError(
        "Office → PDF için LibreOffice gerekli (ücretsiz): "
        "https://www.libreoffice.org/download\n"
        "Windows'ta MS Office kuruluysa 'pip install pywin32' de yeterli.")


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
            raise ConversionError(f"{', '.join(missing)} çevrilemedi.")
        progress(f"ℹ çevrilemedi: {', '.join(missing)}")
    return outputs


# --------------------------------------------------------------------------
# İşlem kataloğu — menüler buradan üretilir
# --------------------------------------------------------------------------

@dataclass
class Action:
    id: str
    label: str
    extensions: list[str]
    func: Callable[..., list[Path]]
    group: str = ""                     # sağ tık alt menüsü (Windows)
    needs_password: str | None = None   # None | "new" | "existing"
    ask: tuple | None = None            # (kwarg, soru, varsayılan): kullanıcıdan metin iste
    multi_only: bool = False            # en az 2 dosya ister
    batch: bool = False                 # tüm dosyalar tek işte (birleştir, görsel→PDF)
    interactive: bool = False           # kendi penceresini açar (ilerleme penceresi yok)
    order: str | None = None            # çoklu seçimde sıralama penceresi: başlıktaki açıklama
    quick: Callable[..., list[Path]] | None = None  # pencereli işlemde sıralama → "Tamam" ile bu çalışır
    kwargs: dict = field(default_factory=dict)


def _t(name: str, module: str = "tools"):
    """Aracı çağrı anında yükler (araç modülleri bu modülü içe aktarır → döngüyü önler)."""
    def call(*args, **kwargs):
        import importlib
        return getattr(importlib.import_module(f"{__package__}.{module}"), name)(*args, **kwargs)
    call.__name__ = name
    return call


PAGES_Q = ("pages", "Sayfa aralığı (ör. 1-3, 5, 8-  •  'tek', 'çift', 'son'):", "")

G_CONV, G_EDIT, G_PAGE, G_SIZE, G_SEC = "Dönüştür", "Düzenle", "Sayfalar", "Küçült / Onar", "Güvenlik"
G_PRINT = "Yazdır"

ACTIONS: list[Action] = [
    # --- Sayfa düzenleyici (PDF + görsel + Office karışık; menüde en üstte)
    Action("organize",   "Sayfaları düzenle / birleştir…",   PDF + IMAGES + OFFICE, _t("organize"),
           batch=True, interactive=True, order="Birleştirme sırası (üstteki önce gelir)",
           quick=_t("merge_files")),
    # --- Klasör
    Action("folder2pdf", "Klasörü tek PDF yap (yer imli)",   [FOLDER], _t("folder_to_pdf")),
    Action("search",     "Klasörde ara (dosyaların içinde)…", [FOLDER], _t("run", "search"), interactive=True),
    # --- Dönüştür
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
    # --- Düzenle
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
    # --- Yazdır
    Action("nup2",       "2 sayfayı 1 kâğıda",               PDF, _t("n_up", "docops"), G_PRINT, kwargs={"per": 2}),
    Action("nup4",       "4 sayfayı 1 kâğıda",               PDF, _t("n_up", "docops"), G_PRINT, kwargs={"per": 4}),
    Action("nup6",       "6 sayfayı 1 kâğıda",               PDF, _t("n_up", "docops"), G_PRINT, kwargs={"per": 6}),
    Action("booklet",    "Kitapçık yap (katla-zımbala)",     PDF, _t("booklet", "docops"), G_PRINT),
    Action("gray",       "Siyah-beyaz yap",                  PDF, _t("grayscale", "docops"), G_PRINT),
    # --- Küçült / Onar
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
    # --- Güvenlik
    Action("protect",    "Şifre koy",                        PDF, protect_pdf, G_SEC, needs_password="new"),
    Action("unlock",     "Şifreyi kaldır",                   PDF, unlock_pdf, G_SEC, needs_password="existing"),
    Action("redact",     "Kişisel bilgileri karart…",        PDF, _t("redact", "docops"), G_SEC,
           ask=("words", "TC kimlik, telefon, IBAN, e-posta otomatik bulunur.\n"
                         "Ek karartılacak kelimeler (virgülle; boş bırakabilirsin):", " ")),
    Action("compare",    "İki PDF'i karşılaştır",            PDF, _t("compare", "docops"), G_EDIT,
           multi_only=True, batch=True,
           order="Hangisi eski? Üstteki eski sürüm, alttaki yeni sürüm sayılır"),
    # --- Office / görseller
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
    # --- her menünün sonunda
    Action("settings",   "Ayarlar…",                         PDF + IMAGES + OFFICE + [FOLDER],
           _t("run", "settings"), interactive=True, batch=True),
]

BY_ID = {a.id: a for a in ACTIONS}


def actions_for_extension(ext: str) -> list[Action]:
    ext = ext if ext == FOLDER else ext.lower().lstrip(".")
    return [a for a in ACTIONS if ext in a.extensions]


def all_extensions() -> list[str]:
    """Dosya uzantıları (klasör sözde uzantısı hariç)."""
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
