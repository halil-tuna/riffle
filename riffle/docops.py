"""Belge işlemleri: karartma (redaksiyon), iki PDF karşılaştırma, beyaz kenar kırpma."""

from __future__ import annotations

import difflib
import hashlib
import re
from pathlib import Path

from .actions import ConversionError, fix_tounicode, human_size, open_pdf, pymupdf, sorted_by_name, unique_path
from .i18n import T

# ============================================================ karartma


def _tc_ok(s: str) -> bool:
    d = [int(c) for c in s]
    if len(d) != 11 or d[0] == 0:
        return False
    if ((sum(d[0:9:2]) * 7 - sum(d[1:8:2])) % 10) != d[9]:
        return False
    return sum(d[:10]) % 10 == d[10]


def _iban_ok(s: str) -> bool:
    s = re.sub(r"\s", "", s).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", s):
        return False
    if s.startswith("TR") and len(s) != 26:
        return False
    num = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(num) % 97 == 1


PATTERNS = {
    "TC kimlik": (re.compile(r"(?<!\d)[1-9]\d{10}(?!\d)"), _tc_ok),
    "IBAN": (re.compile(r"\bTR\s?\d{2}(?:\s?[0-9A-Z]{4}){5}\s?\d{2}\b", re.I), _iban_ok),
    "telefon": (re.compile(r"(?<!\d)(?:\+90[\s.-]?|0)?\(?[2-5]\d{2}\)?[\s.-]?\d{3}[\s.-]?\d{2}[\s.-]?\d{2}(?!\d)"),
                lambda s: len(re.sub(r"\D", "", s)) in (10, 11, 12)),
    "e-posta": (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), lambda s: True),
}


def find_sensitive(text: str, extra: list[str] | None = None) -> list[tuple[str, str]]:
    """[(tür, eşleşen metin)] — sayfa metninde."""
    hits: list[tuple[str, str]] = []
    taken: list[tuple[int, int]] = []
    for kind in ("IBAN", "TC kimlik", "telefon", "e-posta"):   # IBAN önce: içindeki rakamlar TC/telefon sanılmasın
        rx, ok = PATTERNS[kind]
        for m in rx.finditer(text):
            if any(a < m.end() and m.start() < b for a, b in taken):
                continue
            if ok(m.group(0)):
                hits.append((kind, m.group(0)))
                taken.append((m.start(), m.end()))
    for w in extra or []:
        w = w.strip()
        if len(w) >= 2:
            # kelime arası boşluk PDF'te bölünmez boşluk (U+00A0) ya da satır sonu olabilir
            pat = r"\s+".join(re.escape(x) for x in w.split())
            for m in re.finditer(pat, text, re.I):
                hits.append(("özel", m.group(0)))
    return hits


def _rects(page, needle: str):
    rects = page.search_for(needle)
    if rects:
        return rects
    # satır kırılmasına bölünmüş eşleşme: parçaları ayrı ara
    out = []
    for part in re.split(r"\s+", needle):
        if len(part) >= 2:
            out += page.search_for(part)
    return out


def redact(paths, progress, words: str = "", **_):
    extra = [w for w in re.split(r"[,;\n]", words or "") if w.strip()]
    outputs = []
    for p in paths:
        counts: dict[str, int] = {}
        textless = 0
        with open_pdf(p) as doc:
            for i, page in enumerate(doc):
                progress(T("{0}: sayfa {1}/{2} taranıyor", p.name, i + 1, len(doc)))
                text = page.get_text()
                if len(text.strip()) < 10 and page.get_images():
                    textless += 1
                hits = find_sensitive(text, extra)
                for kind, s in hits:
                    rs = _rects(page, s)
                    for r in rs:
                        page.add_redact_annot(r, fill=(0, 0, 0))
                    if rs:
                        counts[kind] = counts.get(kind, 0) + 1
                if hits:
                    # metni gerçekten sil, altındaki resim piksellerini de karart
                    page.apply_redactions(images=2, graphics=0, text=0)
            doc.set_metadata({})                 # yazar, program vb. bilgileri de temizle
            try:
                doc.del_xml_metadata()
            except Exception:
                pass
            out = unique_path(p.parent, f"{p.stem}_karartilmis", ".pdf")
            doc.save(out, garbage=4, deflate=True, clean=True)
        if counts:
            progress("ℹ " + p.name + ": " + ", ".join(f"{v} {T(k)}" for k, v in counts.items()) +
                     T(" karartıldı (metin dosyadan silindi, kopyalanamaz)."))
        else:
            progress(T("ℹ {0}: karartılacak bilgi bulunamadı; belge meta verisi temizlendi.", p.name))
        if textless:
            progress(T("ℹ {0} taranmış sayfada metin yok; önce 'aranabilir yap (OCR)' kullan.", textless))
        outputs.append(out)
    return outputs


# ============================================================ karşılaştırma


def _words(doc):
    out = []
    for pno, page in enumerate(doc):
        for x0, y0, x1, y1, w, *_ in page.get_text("words"):
            out.append((pno, pymupdf.Rect(x0, y0, x1, y1), w))
    return out


def _norm(w: str) -> str:
    return re.sub(r"[^\w]", "", w.lower())


def compare(paths, progress, ordered: bool = False, **_):
    if len(paths) != 2:
        raise ConversionError(T("Karşılaştırmak için tam 2 PDF seç (eski ve yeni sürüm)."))
    old_p, new_p = paths if ordered else sorted_by_name(paths)
    with open_pdf(old_p) as old, open_pdf(new_p) as new:
        progress(T("Metinler karşılaştırılıyor…"))
        ow, nw = _words(old), _words(new)
        sm = difflib.SequenceMatcher(None, [_norm(w[2]) for w in ow], [_norm(w[2]) for w in nw], autojunk=False)
        deleted = inserted = 0
        changes = []
        red, green = (1, 0.35, 0.35), (0.3, 0.85, 0.4)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                continue
            if tag in ("delete", "replace"):
                for pno, r, _ in ow[i1:i2]:
                    old[pno].draw_rect(r, color=None, fill=red, fill_opacity=0.4, overlay=True)
                deleted += i2 - i1
            if tag in ("insert", "replace"):
                for pno, r, _ in nw[j1:j2]:
                    new[pno].draw_rect(r, color=None, fill=green, fill_opacity=0.4, overlay=True)
                inserted += j2 - j1
            changes.append((" ".join(w[2] for w in ow[i1:i2]), " ".join(w[2] for w in nw[j1:j2]),
                            (ow[i1][0] if i1 < i2 else None), (nw[j1][0] if j1 < j2 else None)))

        report = pymupdf.open()
        _summary(report, old_p, new_p, deleted, inserted, changes, sm.ratio())
        progress(T("Rapor hazırlanıyor…"))
        gap = 18
        for i in range(max(len(old), len(new))):
            wo = old[i].rect.width if i < len(old) else 0
            wn = new[i].rect.width if i < len(new) else 0
            h = max(old[i].rect.height if i < len(old) else 0, new[i].rect.height if i < len(new) else 0)
            pg = report.new_page(width=wo + wn + gap * 3, height=h + 40)
            from .ocr import _font_file
            ff = _font_file()
            fk = dict(fontname="baslik", fontfile=ff) if ff else {}
            pg.insert_text((gap, 26), T("Sayfa {0} · ESKİ: {1}", i + 1, old_p.name), fontsize=10, color=(0.6, 0, 0), **fk)
            pg.insert_text((wo + 2 * gap, 26), T("YENİ: {0}", new_p.name), fontsize=10, color=(0, 0.45, 0), **fk)
            if i < len(old):
                pg.show_pdf_page(pymupdf.Rect(gap, 36, gap + wo, 36 + old[i].rect.height), old, i)
            if i < len(new):
                pg.show_pdf_page(pymupdf.Rect(wo + 2 * gap, 36, wo + 2 * gap + wn, 36 + new[i].rect.height), new, i)
        fix_tounicode(report)
        report.set_toc([[1, T("Özet"), 1]] + [[1, T("Sayfa {0}", i + 1), i + 2] for i in range(len(report) - 1)])
        out = unique_path(new_p.parent, f"karsilastirma_{old_p.stem}_{new_p.stem}", ".pdf")
        report.save(out, garbage=3, deflate=True)
    progress(T("ℹ {0} kelime silindi (kırmızı), {1} kelime eklendi (yeşil); benzerlik %{2:.0f}.", deleted, inserted, sm.ratio() * 100))
    return [out]


def _summary(report, old_p, new_p, deleted, inserted, changes, ratio):
    from .ocr import _font_file
    ff = _font_file()
    page = report.new_page(width=595, height=842)
    kw = dict(fontname="ozet", fontfile=ff) if ff else {}
    y = 60

    def line(text, size=10, color=(0, 0, 0), dy=None):
        nonlocal y
        page.insert_text((50, y), text[:110], fontsize=size, color=color, **kw)
        y += dy or size * 1.6

    line(T("Karşılaştırma raporu"), 18, dy=34)
    line(T("Eski sürüm: {0}", old_p.name), 11, (0.6, 0, 0))
    line(T("Yeni sürüm: {0}", new_p.name), 11, (0, 0.45, 0), dy=26)
    line(T("Benzerlik: %{0:.0f}   ·   silinen kelime: {1}   ·   eklenen kelime: {2}", ratio * 100, deleted, inserted), 11, dy=30)
    line(T("Değişiklikler (ilk 40):"), 12, dy=22)
    for old_t, new_t, op, np_ in changes[:40]:
        if y > 800:
            break
        where = T("s.{0}", (np_ if np_ is not None else op) + 1)
        if old_t:
            line(f"{where}  − {old_t}", 9, (0.7, 0, 0), dy=13)
        if new_t:
            line(f"{where}  + {new_t}", 9, (0, 0.5, 0), dy=13)
        y += 4
    if not changes:
        line(T("Metin farkı yok."), 11, (0, 0.45, 0))


# ============================================================ kenar kırp


def autocrop(paths, progress, margin: float = 12.0, **_):
    import numpy as np
    outputs = []
    for p in paths:
        cropped = 0
        with open_pdf(p) as doc:
            for i, page in enumerate(doc):
                progress(T("{0}: sayfa {1}/{2}", p.name, i + 1, len(doc)))
                pix = page.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
                a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
                ys, xs = np.where(a < 240)
                if not len(xs):
                    continue                                    # boş sayfa
                sx, sy = page.rect.width / pix.width, page.rect.height / pix.height
                vis = pymupdf.Rect(xs.min() * sx - margin, ys.min() * sy - margin,
                                   (xs.max() + 1) * sx + margin, (ys.max() + 1) * sy + margin) & page.rect
                if vis.get_area() > 0.97 * page.rect.get_area():
                    continue                                    # kırpılacak kenar yok
                r = vis * page.derotation_matrix                # görünen → döndürmesiz sayfa koordinatı
                r.normalize()
                cb = page.cropbox
                page.set_cropbox(pymupdf.Rect(cb.x0 + r.x0, cb.y0 + r.y0, cb.x0 + r.x1, cb.y0 + r.y1) & page.mediabox)
                cropped += 1
            out = unique_path(p.parent, f"{p.stem}_kirpilmis", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        progress(T("ℹ {0}: {1} sayfanın beyaz kenarları kırpıldı.", p.name, cropped))
        outputs.append(out)
    return outputs


# ============================================================ PDF → Markdown


def pdf_to_markdown(paths, progress, **_):
    outputs = []
    for p in paths:
        progress(T("Markdown'a çevriliyor: {0}", p.name))
        with open_pdf(p) as doc:
            try:
                import pymupdf4llm
                try:
                    md = pymupdf4llm.to_markdown(doc, show_progress=False)
                except (OSError, RuntimeError, ValueError):
                    # sayfa düzeni modeli yüklenemedi (eksik/bozuk dosya): modelsiz yola düş
                    pymupdf4llm.use_layout(False)
                    md = pymupdf4llm.to_markdown(doc, show_progress=False)
            except ImportError:
                md = "\n\n".join(page.get_text() for page in doc)   # yedek: düz metin
            empty = sum(1 for page in doc if len(page.get_text().strip()) < 10)
        out = unique_path(p.parent, p.stem, ".md")
        out.write_text(md, encoding="utf-8")
        if empty:
            progress(T("ℹ {0}: {1} sayfada metin yok (taranmış olabilir; önce OCR).", p.name, empty))
        outputs.append(out)
    return outputs


# ============================================================ PDF/A


def find_ghostscript() -> str | None:
    import os
    import shutil
    import sys
    for name in ("gswin64c", "gswin32c", "gs"):
        f = shutil.which(name)
        if f:
            return f
    if sys.platform == "win32":
        import glob
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            hits = sorted(glob.glob(os.path.join(base, "gs", "gs*", "bin", "gswin*c.exe")), reverse=True)
            if hits:
                return hits[0]
    return None


_PDFA_DEF = """%!
/ICCProfile ({icc}) def
[/_objdef {{icc_PDFA}} /type /stream /OBJ pdfmark
[{{icc_PDFA}} << /N 3 >> /PUT pdfmark
[{{icc_PDFA}} ICCProfile (r) file /PUT pdfmark
[/_objdef {{OutputIntent_PDFA}} /type /dict /OBJ pdfmark
[{{OutputIntent_PDFA}} << /Type /OutputIntent /S /GTS_PDFA1 /DestOutputProfile {{icc_PDFA}}
  /OutputConditionIdentifier (sRGB IEC61966-2.1) /Info (sRGB IEC61966-2.1) >> /PUT pdfmark
[{{Catalog}} << /OutputIntents [ {{OutputIntent_PDFA}} ] >> /PUT pdfmark
"""


def pdf_to_pdfa(paths, progress, **_):
    import subprocess
    import sys
    import tempfile
    gs = find_ghostscript()
    if not gs:
        raise ConversionError(T("PDF/A için Ghostscript gerekli (ücretsiz, AGPL).\n"
                              "Windows: ghostscript.com/releases/gsdnld.html · Linux: sudo apt install ghostscript"))
    from PIL import ImageCms
    outputs = []
    for p in paths:
        progress(T("PDF/A-2b'ye çevriliyor: {0}", p.name))
        out = unique_path(p.parent, f"{p.stem}_PDFA", ".pdf")
        with tempfile.TemporaryDirectory(prefix="riffle_pdfa_") as tmp:
            src = Path(tmp) / "girdi.pdf"
            with open_pdf(p) as doc:
                fix_tounicode(doc)          # Ghostscript bozuk ToUnicode'a güvenir → Türkçe harfler bozulmasın
                doc.save(src, garbage=1)
            icc = Path(tmp) / "srgb.icc"
            icc.write_bytes(ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes())
            icc_ps = str(icc).replace("\\", "/").replace("(", "\\(").replace(")", "\\)")
            defs = Path(tmp) / "PDFA_def.ps"
            defs.write_text(_PDFA_DEF.format(icc=icc_ps), encoding="latin-1")
            cmd = [gs, "-dPDFA=2", "-dBATCH", "-dNOPAUSE", "-dNOOUTERSAVE", "-dQUIET",
                   "-sColorConversionStrategy=RGB", "-dPDFACompatibilityPolicy=1",
                   f"--permit-file-read={tmp.replace(chr(92), '/')}/", "-sDEVICE=pdfwrite",
                   f"-sOutputFile={out}", str(defs), str(src)]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600,
                               creationflags=0x08000000 if sys.platform == "win32" else 0)
        if r.returncode != 0 or not out.exists():
            raise ConversionError(T("{0}: Ghostscript PDF/A üretemedi.\n{1}", p.name, (r.stderr or r.stdout)[-300:]))
        outputs.append(out)
    return outputs


# ============================================================ yazdırma: çoklu sayfa, kitapçık, gri

A4 = (595.276, 841.89)


def _best_grid(n: int, aspect: float):
    """n kaynak sayfayı A4'e en büyük ölçekle dizen (satır, sütun, yatay mı) seçimi."""
    best = None
    grids = {(r, n // r) for r in range(1, n + 1) if n % r == 0}
    for rows, cols in grids:
        for land in (False, True):
            W, H = (A4[1], A4[0]) if land else A4
            m, gap = 18, 8
            cw = (W - 2 * m - (cols - 1) * gap) / cols
            ch = (H - 2 * m - (rows - 1) * gap) / rows
            scale = min(cw / aspect, ch)            # kaynak: genişlik/yükseklik = aspect, yükseklik = 1
            if best is None or scale > best[0] + 1e-6:
                best = (scale, rows, cols, land)
    return best[1:]


def _unrotated(src):
    """Döndürülmüş sayfaları görünen hâliyle sabitleyen kopya (show_pdf_page /Rotate'i yok sayıyor)."""
    if not any(pg.rotation for pg in src):
        return src
    cp = pymupdf.open()
    cp.insert_pdf(src)
    for pg in cp:
        if pg.rotation:
            pg.remove_rotation()
    return cp


def n_up(paths, progress, per: int = 4, frame: bool = True, **_):
    outputs = []
    for p in paths:
        with open_pdf(p) as orig:
            src = _unrotated(orig)
            n = len(src)
            aspects = sorted(pg.rect.width / pg.rect.height for pg in src)
            aspect = aspects[len(aspects) // 2]
            rows, cols, land = _best_grid(per, aspect)
            W, H = (A4[1], A4[0]) if land else A4
            m, gap = 18, 8
            cw = (W - 2 * m - (cols - 1) * gap) / cols
            ch = (H - 2 * m - (rows - 1) * gap) / rows
            # hücreyi sayfanın gerçek oranına sıkıştır, ızgarayı kâğıtta ortala (aralar boş kalmasın)
            s = min(cw / aspect, ch)
            cw, ch = aspect * s, s
            ox = (W - cols * cw - (cols - 1) * gap) / 2
            oy = (H - rows * ch - (rows - 1) * gap) / 2
            out = pymupdf.open()
            for k in range(0, n, per):
                progress(T("{0}: kâğıt {1}/{2}", p.name, k // per + 1, -(-n // per)))
                sheet = out.new_page(width=W, height=H)
                for j in range(per):
                    if k + j >= n:
                        break
                    r, c = divmod(j, cols)
                    cell = pymupdf.Rect(ox + c * (cw + gap), oy + r * (ch + gap),
                                        ox + c * (cw + gap) + cw, oy + r * (ch + gap) + ch)
                    sheet.show_pdf_page(cell, src, k + j, keep_proportion=True)
                    if frame:
                        pr = src[k + j].rect
                        s = min(cell.width / pr.width, cell.height / pr.height)
                        w, h = pr.width * s, pr.height * s
                        fr = pymupdf.Rect(cell.x0 + (cell.width - w) / 2, cell.y0 + (cell.height - h) / 2,
                                          cell.x0 + (cell.width + w) / 2, cell.y0 + (cell.height + h) / 2)
                        sheet.draw_rect(fr, color=(0.75, 0.75, 0.75), width=0.4)
            o = unique_path(p.parent, f"{p.stem}_{per}li", ".pdf")
            out.save(o, garbage=3, deflate=True)
            sheets = len(out)
            out.close()
        progress(T("ℹ {0}: {1} sayfa → {2} kâğıt ({3}×{4}, {5} A4).", p.name, n, sheets, rows, cols, 'yatay' if land else 'dikey'))
        outputs.append(o)
    return outputs


def booklet_order(n: int) -> list[tuple]:
    """Kitapçık: her kâğıt yüzü (sol, sağ) sayfa dizinleri; None = boş. Kısa kenardan çevirerek
    çift taraflı bas, ortadan katla."""
    total = -(-n // 4) * 4
    pages = list(range(n)) + [None] * (total - n)
    sides = []
    lo, hi = 0, total - 1
    while lo < hi:
        sides.append((pages[hi], pages[lo]))          # ön yüz: son | ilk
        sides.append((pages[lo + 1], pages[hi - 1]))  # arka yüz: ikinci | sondan ikinci
        lo, hi = lo + 2, hi - 2
    return sides


def booklet(paths, progress, **_):
    outputs = []
    for p in paths:
        with open_pdf(p) as orig:
            src = _unrotated(orig)
            n = len(src)
            out = pymupdf.open()
            W, H = A4[1], A4[0]                        # yatay A4, iki A5 yarım
            m = 14
            for i, (left, right) in enumerate(booklet_order(n)):
                progress(T("{0}: kâğıt yüzü {1}", p.name, i + 1))
                sheet = out.new_page(width=W, height=H)
                for k, idx in enumerate((left, right)):
                    if idx is None:
                        continue
                    cell = pymupdf.Rect(k * W / 2 + m, m, (k + 1) * W / 2 - m, H - m)
                    sheet.show_pdf_page(cell, src, idx, keep_proportion=True)
            o = unique_path(p.parent, f"{p.stem}_kitapcik", ".pdf")
            out.save(o, garbage=3, deflate=True)
            out.close()
        blanks = -(-n // 4) * 4 - n
        progress(T("ℹ {0}: çift taraflı, KISA kenardan çevirerek bas, ortadan katla", p.name)
                 + (T(" ({0} boş sayfa eklendi).", blanks) if blanks else "."))
        outputs.append(o)
    return outputs


def grayscale(paths, progress, **_):
    outputs = []
    for p in paths:
        progress(T("Siyah-beyaz yapılıyor: {0}", p.name))
        with open_pdf(p) as doc:
            before = p.stat().st_size
            try:
                doc.recolor(1)                         # yazı, çizim ve resimler gri; metin seçilebilir kalır
            except Exception as e:
                raise ConversionError(T("{0}: gri tonlamaya çevrilemedi ({1}).", p.name, e))
            o = unique_path(p.parent, f"{p.stem}_siyah_beyaz", ".pdf")
            doc.save(o, garbage=4, deflate=True, deflate_images=True)
        progress(f"ℹ {p.name}: {human_size(before)} → {human_size(o.stat().st_size)}")
        outputs.append(o)
    return outputs


# ============================================================ tarama düzeltme

def _gray_small(page, dpi=60):
    import numpy as np
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False, annots=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)


def is_blank(page, ink_ratio: float = 0.0015) -> bool:
    """Boş sayfa mı? Metin yok ve koyu piksel oranı çok düşük (tarayıcı lekesi, arka yüz gölgesi sayılmaz)."""
    if page.get_text().strip():
        return False
    a = _gray_small(page, 50)
    h, w = a.shape
    core = a[int(h * 0.04):int(h * 0.96), int(w * 0.04):int(w * 0.96)]   # kenardaki tarayıcı gölgesi hariç
    return float((core < 128).mean()) < ink_ratio


def skew_angle(page, max_deg: float = 8.0) -> float:
    """Taranmış sayfanın eğikliği (derece): içeriği bu açıyla (saat yönünün tersine +) döndürünce düzelir.
    Satır izdüşümü yöntemi: doğru açıda satırlar yatay hizalanır, satır toplamlarının varyansı en büyük olur."""
    import numpy as np
    from PIL import Image
    a = _gray_small(page, 72)
    ink = (a < min(160, int(a.mean()) - 25)).astype(np.uint8) * 255
    if ink.mean() < 0.5:                     # neredeyse boş
        return 0.0
    im = Image.fromarray(ink)

    def score(deg):
        r = np.asarray(im.rotate(deg, resample=Image.NEAREST, fillcolor=0), dtype=np.float32)
        rows = r.sum(axis=1)
        return float(np.var(np.diff(rows)))

    best = max(np.arange(-max_deg, max_deg + 0.01, 0.5), key=score)
    fine = max(np.arange(best - 0.5, best + 0.51, 0.1), key=score)
    return round(float(fine), 2)


def _rotate_content(doc, page, deg: float) -> None:
    """Sayfa içeriğini merkez etrafında döndürür (kayıpsız: resim yeniden sıkıştırılmaz, metin kalır)."""
    import math
    page.clean_contents()
    xrefs = page.get_contents()
    if not xrefs:
        return
    mb = page.mediabox
    cx, cy = (mb.x0 + mb.x1) / 2, (mb.y0 + mb.y1) / 2
    t = math.radians(deg)
    c, s = math.cos(t), math.sin(t)
    tx, ty = cx - c * cx + s * cy, cy - s * cx - c * cy
    old = doc.xref_stream(xrefs[0])
    cm = f"q {c:.6f} {s:.6f} {-s:.6f} {c:.6f} {tx:.3f} {ty:.3f} cm\n".encode()
    doc.update_stream(xrefs[0], cm + old + b"\nQ")


def fix_scan(paths, progress, **_):
    """Taranmış PDF: eğik sayfaları düzelt, boş sayfaları (arka yüzler) sil."""
    from .ocr import needs_ocr
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            fixed, blanks = [], []
            for i, page in enumerate(doc):
                progress(T("{0}: sayfa {1}/{2}", p.name, i + 1, len(doc)))
                if is_blank(page):
                    blanks.append(i)
                    continue
                scanned = needs_ocr(page) or (page.get_images() and len(page.get_drawings()) < 5
                                              and sum(r.get_area() for im in page.get_images()
                                                      for r in page.get_image_rects(im[0]))
                                              > 0.6 * page.rect.get_area())
                if not scanned or page.rotation:
                    continue
                ang = skew_angle(page)
                if 0.25 <= abs(ang) <= 8:
                    _rotate_content(doc, page, ang)
                    fixed.append((i + 1, ang))
            if blanks and len(blanks) < len(doc):
                doc.delete_pages(blanks)
            elif blanks:
                blanks = []                      # hepsi boşsa hiçbirini silme
            if not fixed and not blanks:
                progress(T("ℹ {0}: eğik ya da boş sayfa bulunamadı, dosya değiştirilmedi.", p.name))
                continue
            out = unique_path(p.parent, f"{p.stem}_duzeltilmis", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        parts = []
        if fixed:
            parts.append(T("{0} sayfanın eğikliği düzeltildi (en çok {1:.1f}°)", len(fixed), max(abs(a) for _, a in fixed)))
        if blanks:
            parts.append(T("{0} boş sayfa silindi (s. {1})", len(blanks), ', '.join(str(b + 1) for b in blanks[:10])))
        progress(f"ℹ {p.name}: " + "; ".join(parts) + ".")
        outputs.append(out)
    return outputs


def remove_blank_pages(paths, progress, **_):
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            blanks = [i for i, pg in enumerate(doc) if is_blank(pg)]
            if not blanks:
                progress(T("ℹ {0}: boş sayfa yok.", p.name))
                continue
            if len(blanks) == len(doc):
                raise ConversionError(T("{0}: bütün sayfalar boş görünüyor, silinmedi.", p.name))
            doc.delete_pages(blanks)
            out = unique_path(p.parent, f"{p.stem}_bossuz", ".pdf")
            doc.save(out, garbage=3, deflate=True)
        progress(T("ℹ {0}: {1} boş sayfa silindi (s. {2}).", p.name, len(blanks), ', '.join(str(b + 1) for b in blanks[:15])))
        outputs.append(out)
    return outputs


# ============================================================ resimleri çıkar

def extract_images(paths, progress, min_side: int = 48, **_):
    """PDF'teki gömülü resimleri ORİJİNAL hâliyle (yeniden sıkıştırmadan) klasöre çıkarır.
    Aynı resim birden çok sayfada geçiyorsa bir kez; çok küçük süsler (ikon, çizgi) atlanır."""
    from .actions import unique_dir
    outputs = []
    for p in paths:
        with open_pdf(p) as doc:
            seen, saved, skipped, dupes = set(), 0, 0, 0
            seen_content: set = set()
            folder = None
            for pno, page in enumerate(doc):
                progress(T("{0}: sayfa {1}/{2}", p.name, pno + 1, len(doc)))
                for k, im in enumerate(page.get_images(full=True), 1):
                    xref, smask = im[0], im[1]
                    if xref in seen:
                        continue
                    seen.add(xref)
                    try:
                        info = doc.extract_image(xref)
                    except Exception:
                        continue
                    if not info or min(info["width"], info["height"]) < min_side:
                        skipped += 1
                        continue
                    # aynı resim belgede ayrı ayrı nesneler olarak tekrar tekrar gömülmüş olabilir
                    # (Word her sayfaya logo/şekil kopyası koyuyor): baytına ve piksellerine göre bir kez
                    keys = {hashlib.md5(info["image"]).hexdigest()}
                    try:
                        keys.add(pymupdf.Pixmap(doc, xref).digest.hex())
                    except Exception:
                        pass
                    if keys & seen_content:
                        dupes += 1
                        continue
                    seen_content |= keys
                    if folder is None:
                        folder = unique_dir(p.parent, f"{p.stem}_resimler")
                    name = T("sayfa{0:03d}_{1}", pno + 1, k)
                    try:
                        if smask:                       # saydamlık maskesi: PNG olarak birleştir
                            pix = pymupdf.Pixmap(doc.extract_image(xref)["image"])
                            mask = pymupdf.Pixmap(doc.extract_image(smask)["image"])
                            if pix.alpha:
                                pix = pymupdf.Pixmap(pix, 0)
                            if pix.n - pix.alpha >= 4:
                                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                            pix = pymupdf.Pixmap(pix, mask)
                            pix.save(folder / f"{name}.png")
                        elif info["ext"] in ("jpeg", "jpg", "png", "jpx", "jp2", "bmp", "tiff", "gif"):
                            ext = {"jpeg": "jpg", "jpx": "jp2"}.get(info["ext"], info["ext"])
                            (folder / f"{name}.{ext}").write_bytes(info["image"])
                        else:                           # JBIG2/CCITT gibi: PNG'ye çevir
                            pix = pymupdf.Pixmap(doc, xref)
                            if pix.n - pix.alpha >= 4:
                                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                            pix.save(folder / f"{name}.png")
                        saved += 1
                    except Exception:
                        try:                            # son çare: resmi PNG'ye render et
                            pix = pymupdf.Pixmap(doc, xref)
                            if pix.n - pix.alpha >= 4:
                                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                            pix.save(folder / f"{name}.png")
                            saved += 1
                        except Exception:
                            skipped += 1
        if not saved:
            progress(T("ℹ {0}: çıkarılacak resim yok", p.name) + (T(" ({0} küçük süs atlandı).", skipped) if skipped else "."))
            continue
        extra = [T("{0} tekrar eden kopya", dupes) if dupes else "", T("{0} küçük ikon/çizgi", skipped) if skipped else ""]
        extra = [e for e in extra if e]
        progress(T("ℹ {0}: {1} resim çıkarıldı → {2}", p.name, saved, folder.name)
                 + (T(" ({0} atlandı).", ', '.join(extra)) if extra else "."))
        outputs.append(folder)
    return outputs


# ============================================================ bütün sayfaları A4 yap

def to_a4(paths: list[Path], progress, **_) -> list[Path]:
    """Farklı boyutlu sayfalar (A5, Letter, telefon taraması, slayt) tek tip A4'e: içerik oranı korunarak
    sığdırılır ve ortalanır; yatay sayfalar yatay A4 olur. Metin ve çizimler vektör kalır."""
    from .actions import open_pdf
    W, H = pymupdf.paper_size("a4")
    outputs = []
    for p in paths:
        with open_pdf(p) as src, pymupdf.open() as out:
            changed = 0
            for i, page in enumerate(src):
                if i % 20 == 0:
                    progress(T("{0}: sayfa {1}/{2}", p.name, i + 1, len(src)))
                r = page.rect                                    # görünen (döndürülmüş) boyut
                land = r.width > r.height
                pw, ph = (H, W) if land else (W, H)
                if abs(r.width - pw) > 3 or abs(r.height - ph) > 3:
                    changed += 1
                dst = out.new_page(width=pw, height=ph)
                k = min(pw / r.width, ph / r.height)
                w, h = r.width * k, r.height * k
                box = pymupdf.Rect((pw - w) / 2, (ph - h) / 2, (pw + w) / 2, (ph + h) / 2)
                dst.show_pdf_page(box, src, i)
            if not changed:
                progress(T("ℹ {0}: bütün sayfalar zaten A4.", p.name))
                continue
            dst_path = unique_path(p.parent, f"{p.stem}_a4", ".pdf")
            out.save(dst_path, garbage=3, deflate=True)
            progress(T("ℹ {0}: {1} sayfa A4'e oturtuldu.", p.name, changed))
            outputs.append(dst_path)
    return outputs


# ============================================================ boyuta göre böl

def _pdf_bytes(src, pages: list[int]) -> bytes:
    with pymupdf.open() as d:
        for i in pages:
            d.insert_pdf(src, from_page=i, to_page=i)
        return d.tobytes(garbage=3, deflate=True)


def split_by_size(paths: list[Path], progress, size: str = "10", **_) -> list[Path]:
    """PDF'i her biri en fazla N MB olan parçalara böler (e-posta eki / yükleme sınırı). Sayfa sırası korunur."""
    from .actions import open_pdf, unique_dir
    from .tools import _parse_mb
    limit = int(_parse_mb(size) * 1024 * 1024)
    outputs = []
    for p in paths:
        if p.stat().st_size <= limit:
            progress(T("ℹ {0}: zaten {1}, bölmeye gerek yok.", p.name, human_size(p.stat().st_size)))
            continue
        with open_pdf(p) as src:
            n = len(src)
            progress(T("{0}: sayfa boyutları ölçülüyor", p.name))
            # tek tek sayfa boyutu (paylaşılan yazı tipleri her sayfada sayıldığı için fazla tahmin: güvenli)
            est = []
            for i in range(n):
                if i % 10 == 0:
                    progress(T("{0}: sayfa {1}/{2}", p.name, i + 1, n))
                est.append(len(_pdf_bytes(src, [i])))
            chunks, cur, cur_size = [], [], 0
            for i, s in enumerate(est):
                if cur and cur_size + s > limit:
                    chunks.append(cur)
                    cur, cur_size = [], 0
                cur.append(i)
                cur_size += s
            if cur:
                chunks.append(cur)
            # tahmin fazla olduğundan parçalar sınırın epey altında kalabilir: komşuları gerçek boyutla birleştir
            merged = [chunks[0]]
            for c in chunks[1:]:
                trial = merged[-1] + c
                if len(_pdf_bytes(src, trial)) <= limit:
                    merged[-1] = trial
                else:
                    merged.append(c)
            if len(merged) == 1:
                progress(T("ℹ {0}: tek sayfası bile {1} üstünde; önce sıkıştır.", p.name, human_size(limit))
                         if n == 1 else T("ℹ {0}: bölmeye gerek kalmadı.", p.name))
                continue
            folder = unique_dir(p.parent, f"{p.stem}_parcalar")
            big = 0
            for k, pages in enumerate(merged, 1):
                data = _pdf_bytes(src, pages)
                big += len(data) > limit
                out = folder / f"{localized_part(p.stem, k)}.pdf"
                out.write_bytes(data)
            progress(T("ℹ {0}: {1} parça → {2}", p.name, len(merged), folder.name) +
                     (T(" ({0} parçada tek sayfa sınırı aşıyor).", big) if big else ""))
            outputs.append(folder)
    return outputs


def localized_part(stem: str, k: int) -> str:
    from .actions import localized_stem
    return localized_stem(f"{stem}_parca{k}")
