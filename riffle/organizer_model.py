"""Sayfa düzenleyici: arayüzden bağımsız model.

Belge = sıralı sayfa listesi. Her sayfa bir kaynağa işaret eder:
  - PDF dosyasının bir sayfası
  - bir görsel (JPG/PNG/HEIC…) → tek sayfa
  - boş sayfa
Office dosyaları eklenirken önce PDF'e çevrilir.

İşlemler: ekle, taşı, döndür, sil, çoğalt, böl işareti, sırala, ters çevir, geri al.
Kaydet: tek PDF ya da böl işaretlerinden ayrılmış birden çok PDF.
"""

from __future__ import annotations

import copy
import io
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .actions import IMAGES, OFFICE, PDF, ConversionError, open_pdf, pymupdf, unique_path
from .i18n import T

A4 = (595.0, 842.0)
IMAGE_MARGIN = 28.0  # pt (~1 cm): A4'e yerleştirilen görselin kenar boşluğu


@dataclass
class PageRef:
    kind: str                 # "pdf" | "image" | "blank"
    path: Path | None = None
    index: int = 0            # PDF sayfa numarası (0 tabanlı)
    rotation: int = 0         # kullanıcının eklediği döndürme (0/90/180/270)
    size: tuple = A4          # döndürmesiz görünen boyut (pt)
    label: str = ""           # arayüzde gösterilecek kısa ad
    uid: int = 0              # benzersiz kimlik (küçük resim önbelleği)
    origin: Path | None = None  # kullanıcının seçtiği asıl dosya (Office → geçici PDF olsa da)


@dataclass
class Document:
    pages: list[PageRef] = field(default_factory=list)
    split_after: set[int] = field(default_factory=set)   # bu uid'li sayfadan SONRA böl
    image_mode: str = "a4"    # "a4" | "original": görsel sayfa boyutu
    _undo: list = field(default_factory=list, repr=False)
    _next_uid: int = field(default=1, repr=False)
    _tmp: tempfile.TemporaryDirectory | None = field(default=None, repr=False)

    # ------------------------------------------------------------ geri al

    def _snapshot(self):
        self._undo.append((copy.deepcopy(self.pages), set(self.split_after)))
        if len(self._undo) > 100:
            self._undo.pop(0)

    def undo(self) -> bool:
        if not self._undo:
            return False
        self.pages, self.split_after = self._undo.pop()
        return True

    def _uid(self) -> int:
        self._next_uid += 1
        return self._next_uid

    # ------------------------------------------------------------ ekleme

    def add_files(self, paths, at: int | None = None, progress=lambda m: None) -> list[str]:
        """Dosyaları ekler. Eklenemeyenler için hata mesajlarını döndürür."""
        self._snapshot()
        new: list[PageRef] = []
        errors = []
        paths = [Path(p) for p in paths]
        office = [p for p in paths if p.suffix.lower().lstrip(".") in OFFICE]
        converted: dict = {}
        batch_failed = False
        if office:
            # hepsi tek LibreOffice çağrısında: dosya başına programı açmak en yavaş kısımdı
            from .actions import office_to_pdf_many
            if self._tmp is None:
                self._tmp = tempfile.TemporaryDirectory(prefix="riffle_duzen_")
            outdir = Path(self._tmp.name) / f"office{len(self.pages)}_{self._uid()}"
            try:
                converted = office_to_pdf_many(office, outdir, progress)
            except ConversionError as e:
                errors.append(str(e))
                batch_failed = True
        for p in paths:
            ext = p.suffix.lower().lstrip(".")
            try:
                if ext in PDF:
                    new += self._pdf_pages(p)
                elif ext in IMAGES:
                    new.append(self._image_page(p))
                elif ext in OFFICE:
                    if p not in converted:
                        if not batch_failed:
                            errors.append(T("{0}: PDF'e çevrilemedi", p.name))
                        continue
                    new += self._pdf_pages(converted[p], label=p.name, origin=p)
                else:
                    errors.append(T("{0}: desteklenmeyen dosya türü", p.name))
            except ConversionError as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"{p.name}: {e}")
        pos = len(self.pages) if at is None else max(0, min(at, len(self.pages)))
        self.pages[pos:pos] = new
        if not new:
            self._undo.pop()
        return errors

    def _pdf_pages(self, p: Path, label: str | None = None, origin: Path | None = None) -> list[PageRef]:
        with open_pdf(p) as d:
            out = []
            for i, pg in enumerate(d):
                r = pg.rect  # görünen (dosyanın kendi döndürmesi dahil) boyut
                out.append(PageRef("pdf", p, i, 0, (r.width, r.height),
                                   f"{label or p.name} · {i + 1}", self._uid(), origin or p))
            return out

    def _image_page(self, p: Path) -> PageRef:
        from .tools import _open_image
        im = _open_image(p)          # HEIC dahil; telefon EXIF yönü düzeltilir
        w, h = im.size
        return PageRef("image", p, 0, 0, (w * 0.75, h * 0.75), p.name, self._uid(), p)

    def add_blank(self, at: int | None = None) -> None:
        self._snapshot()
        size = A4
        ref = self.pages[at - 1] if at and 0 < at <= len(self.pages) else (self.pages[-1] if self.pages else None)
        if ref is not None and ref.kind == "pdf":
            size = ref.size
        pos = len(self.pages) if at is None else at
        self.pages.insert(pos, PageRef("blank", None, 0, 0, size, T("Boş sayfa"), self._uid()))

    # ------------------------------------------------------------ düzenleme

    def move(self, indices: list[int], to: int) -> list[int]:
        """Seçili sayfaları 'to' konumuna (taşımadan önceki dizine göre) taşır.
        Yeni dizinlerini döndürür."""
        indices = sorted(set(i for i in indices if 0 <= i < len(self.pages)))
        if not indices:
            return []
        self._snapshot()
        moving = [self.pages[i] for i in indices]
        before = sum(1 for i in indices if i < to)
        rest = [p for i, p in enumerate(self.pages) if i not in set(indices)]
        to = max(0, min(to - before, len(rest)))
        self.pages = rest[:to] + moving + rest[to:]
        return list(range(to, to + len(moving)))

    def rotate(self, indices, degrees: int) -> None:
        self._snapshot()
        for i in indices:
            if 0 <= i < len(self.pages):
                p = self.pages[i]
                p.rotation = (p.rotation + degrees) % 360

    def delete(self, indices) -> None:
        idx = set(indices)
        if not idx:
            return
        self._snapshot()
        gone = {self.pages[i].uid for i in idx if 0 <= i < len(self.pages)}
        self.pages = [p for i, p in enumerate(self.pages) if i not in idx]
        self.split_after -= gone

    def duplicate(self, indices) -> None:
        idx = sorted(set(indices))
        if not idx:
            return
        self._snapshot()
        for offset, i in enumerate(idx):
            c = copy.deepcopy(self.pages[i + offset])
            c.uid = self._uid()
            self.pages.insert(i + offset + 1, c)

    def toggle_split(self, index: int) -> None:
        if not (0 <= index < len(self.pages) - 1):
            return
        self._snapshot()
        uid = self.pages[index].uid
        self.split_after ^= {uid}

    def split_every(self, n: int = 1) -> None:
        self._snapshot()
        self.split_after = {p.uid for i, p in enumerate(self.pages[:-1]) if (i + 1) % n == 0}

    def sort_by_name(self) -> None:
        self._snapshot()

        def key(p: PageRef):
            name = p.path.name if p.path else "~"
            return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)] + [p.index]
        self.pages.sort(key=key)

    def reverse(self) -> None:
        self._snapshot()
        self.pages.reverse()

    # ------------------------------------------------------------ çizim

    def thumbnail_png(self, page: PageRef, max_side: int = 150) -> bytes:
        """Döndürülmüş hâliyle küçük resim (PNG)."""
        if page.kind == "blank":
            w, h = page.size
            if page.rotation in (90, 270):
                w, h = h, w
            z = max_side / max(w, h)
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, max(1, int(w * z)), max(1, int(h * z))), 0)
            pix.clear_with(255)
            return pix.tobytes("png")
        if page.kind == "image":
            from PIL import Image
            from .tools import _open_image
            im = _open_image(page.path)
            im.thumbnail((max_side, max_side))
            if page.rotation:
                im = im.rotate(-page.rotation, expand=True)
            b = io.BytesIO()
            im.save(b, "PNG")
            return b.getvalue()
        with pymupdf.open(page.path) as d:
            pg = d[page.index]
            w, h = pg.rect.width, pg.rect.height
            z = max_side / max(w, h)
            pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z).prerotate(page.rotation), alpha=False)
            return pix.tobytes("png")

    # ------------------------------------------------------------ kaydetme

    def groups(self) -> list[list[PageRef]]:
        out, cur = [], []
        for p in self.pages:
            cur.append(p)
            if p.uid in self.split_after:
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
        return out

    def _append(self, out_doc, p: PageRef, cache: dict) -> None:
        if p.kind == "pdf":
            src = cache.get(p.path)
            if src is None:
                src = cache[p.path] = pymupdf.open(p.path)
            out_doc.insert_pdf(src, from_page=p.index, to_page=p.index)
            pg = out_doc[-1]
            pg.set_rotation((pg.rotation + p.rotation) % 360)
        elif p.kind == "image":
            from .tools import _open_image
            im = _open_image(p.path)
            b = io.BytesIO()
            im.save(b, "JPEG", quality=92)
            iw, ih = im.size
            if self.image_mode == "a4":
                W, H = A4 if ih >= iw else (A4[1], A4[0])     # görsele göre dikey/yatay A4
                page = out_doc.new_page(width=W, height=H)
                box = pymupdf.Rect(IMAGE_MARGIN, IMAGE_MARGIN, W - IMAGE_MARGIN, H - IMAGE_MARGIN)
            else:
                W, H = iw * 0.75, ih * 0.75                     # 96 dpi kabulü
                page = out_doc.new_page(width=W, height=H)
                box = page.rect
            page.insert_image(box, stream=b.getvalue(), keep_proportion=True)
            page.set_rotation(p.rotation)
        else:
            w, h = p.size
            page = out_doc.new_page(width=w, height=h)
            page.set_rotation(p.rotation)

    def save(self, folder: Path, stem: str, progress=lambda m: None) -> list[Path]:
        if not self.pages:
            raise ConversionError(T("Kaydedilecek sayfa yok."))
        groups = self.groups()
        outputs, cache = [], {}
        try:
            for gi, g in enumerate(groups):
                out_doc = pymupdf.open()
                toc, prev = [], None
                for k, p in enumerate(g):
                    progress(T("Kaydediliyor: {0}/{1} · sayfa {2}/{3}", gi + 1, len(groups), k + 1, len(g)))
                    self._append(out_doc, p, cache)
                    # her dosyanın ardışık sayfa grubu bir yer imi (boş sayfa önceki gruba dahil)
                    if p.origin is not None and p.origin != prev:
                        toc.append([1, p.origin.stem, len(out_doc)])
                        prev = p.origin
                if len({p.origin for p in g if p.origin is not None}) > 1:
                    out_doc.set_toc(toc)
                name = stem if len(groups) == 1 else f"{stem}_{gi + 1}"
                out = unique_path(folder, name, ".pdf")
                out_doc.save(out, garbage=3, deflate=True)
                out_doc.close()
                outputs.append(out)
        finally:
            for d in cache.values():
                d.close()
        return outputs

    def default_target(self) -> tuple[Path, str]:
        origins = [p.origin for p in self.pages if p.origin is not None]
        if not origins:
            return Path.home(), "yeni_belge"
        first = origins[0]
        suffix = "_birlesik" if len(set(origins)) > 1 else "_duzenlenmis"
        return first.parent, f"{first.stem}{suffix}"
