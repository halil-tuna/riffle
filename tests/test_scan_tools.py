"""Tarama düzeltme, boş sayfa silme, resim çıkarma, klasörde arama."""

import io

import pymupdf
import pytest
from PIL import Image, ImageDraw

from conftest import _text, make_text_pdf, noop
from riffle import docops, search
from riffle.actions import ConversionError


def _skewed_scan(path, src, angles, blank_back=False):
    s = pymupdf.open(src)
    out = pymupdf.open()
    for p, deg in zip(s, angles):
        pix = p.get_pixmap(dpi=150, colorspace=pymupdf.csGRAY)
        im = Image.frombytes("L", (pix.width, pix.height), pix.samples).rotate(deg, fillcolor=245)
        b = io.BytesIO()
        im.save(b, "JPEG", quality=75)
        n = out.new_page(width=p.rect.width, height=p.rect.height)
        n.insert_image(n.rect, stream=b.getvalue())
        if blank_back:                                   # tarayıcıdan gelen boş arka yüz: gri + leke
            im = Image.new("L", (1240, 1754), 238)
            ImageDraw.Draw(im).ellipse((600, 900, 606, 906), fill=90)
            b = io.BytesIO()
            im.save(b, "JPEG")
            out.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842), stream=b.getvalue())
    out.save(path)
    return path


def test_skew_detect_and_fix(tmp_path):
    src = make_text_pdf(tmp_path / "s.pdf", pages=3, table=False)
    scan = _skewed_scan(tmp_path / "egik.pdf", src, (3.0, -2.0, 0.0), blank_back=True)
    d = pymupdf.open(scan)
    assert len(d) == 6
    angles = [docops.skew_angle(d[i]) for i in (0, 2, 4)]
    assert abs(angles[0] + 3.0) < 0.35 and abs(angles[1] - 2.0) < 0.35 and abs(angles[2]) < 0.3
    assert [docops.is_blank(pg) for pg in d] == [False, True] * 3
    msgs = []
    out = docops.fix_scan([scan], msgs.append)[0]
    r = pymupdf.open(out)
    assert len(r) == 3                                  # 3 arka yüz silindi
    assert all(abs(docops.skew_angle(pg)) <= 0.25 for pg in r)
    assert any("eğikliği düzeltildi" in m and "3 boş sayfa" in m for m in msgs)
    # kayıpsız: resim yeniden sıkıştırılmadı
    assert r.extract_image(r[0].get_images()[0][0])["ext"] == "jpeg"


def test_fix_scan_leaves_text_pdf(tmp_path, pdf):
    msgs = []
    assert docops.fix_scan([pdf], msgs.append) == []
    assert any("değiştirilmedi" in m for m in msgs)


def test_blank_pages(tmp_path):
    d = pymupdf.open()
    for i in range(5):
        p = d.new_page()
        if i in (0, 3):
            _text(p, (72, 100), f"Dolu sayfa {i}")
        if i == 2:
            p.draw_rect(pymupdf.Rect(100, 100, 400, 300), fill=(0.2, 0.2, 0.2))   # yazısız ama dolu
    d.save(tmp_path / "b.pdf")
    out = docops.remove_blank_pages([tmp_path / "b.pdf"], noop)[0]
    assert len(pymupdf.open(out)) == 3
    all_blank = pymupdf.open()
    all_blank.new_page()
    all_blank.save(tmp_path / "bos.pdf")
    with pytest.raises(ConversionError):
        docops.remove_blank_pages([tmp_path / "bos.pdf"], noop)


def test_extract_images(tmp_path):
    d = pymupdf.open()
    d.new_page()
    d.new_page()
    p1, p2 = d[0], d[1]
    jpg = io.BytesIO()
    Image.new("RGB", (400, 300), (10, 120, 200)).save(jpg, "JPEG", quality=90)
    png = io.BytesIO()
    Image.new("RGBA", (200, 200), (255, 0, 0, 100)).save(png, "PNG")
    p1.insert_image(pymupdf.Rect(50, 50, 450, 350), stream=jpg.getvalue())
    p2.insert_image(pymupdf.Rect(50, 50, 250, 250), stream=png.getvalue())       # saydam
    tiny = io.BytesIO()
    Image.new("RGB", (16, 16), (0, 0, 0)).save(tiny, "PNG")
    p2.insert_image(pymupdf.Rect(300, 50, 316, 66), stream=tiny.getvalue())     # ikon: atlanır
    xref = p1.get_images()[0][0]
    p2.insert_image(pymupdf.Rect(50, 400, 250, 550), xref=xref)                  # aynı resim tekrar
    d.save(tmp_path / "r.pdf")
    msgs = []
    folder = docops.extract_images([tmp_path / "r.pdf"], msgs.append)[0]
    files = sorted(f.name for f in folder.iterdir())
    assert files == ["sayfa001_1.jpg", "sayfa002_1.png"]
    assert Image.open(folder / "sayfa001_1.jpg").size == (400, 300)               # orijinal çözünürlük
    assert Image.open(folder / "sayfa002_1.png").mode in ("RGBA", "LA")          # saydamlık korundu
    assert any("1 küçük" in m for m in msgs)


@pytest.mark.parametrize("q,expect", [("ogrenci", 2), ("ÖĞRENCİ", 2), ("istanbul", 1), ("yok_boyle", 0)])
def test_search_folder(tmp_path, q, expect):
    root = tmp_path / "Dersler"
    (root / "alt").mkdir(parents=True)
    make_text_pdf(root / "ders.pdf", pages=1, table=False)       # "öğrenci" geçiyor
    (root / "alt" / "not.txt").write_text("İSTANBUL'da Öğrenci buluşması", encoding="utf-8")
    hits, scanned, failed = search.search(root, q)
    assert len({h.path for h in hits}) == expect
    if expect:
        h = hits[0]
        assert h.where.startswith(("s. ", "satır "))
        assert search.fold(q) in search.fold(h.context)


def test_search_office_and_scanned(tmp_path, scanned):
    import docx
    from openpyxl import Workbook
    from pptx import Presentation
    root = tmp_path / "k"
    root.mkdir()
    doc = docx.Document()
    doc.add_paragraph("Bitirme projesi dalma erozyon")
    doc.save(root / "rapor.docx")
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Erozyon sunumu"
    prs.save(root / "sunum.pptx")
    wb = Workbook()
    wb.active["B4"] = "erozyon bütçesi"
    wb.save(root / "butce.xlsx")
    import shutil
    shutil.copy(scanned, root / "tarama.pdf")
    hits, n_scanned, failed = search.search(root, "erozyon")
    where = {h.path.name: h.where for h in hits}
    assert where == {"rapor.docx": "paragraf 1", "sunum.pptx": "slayt 1", "butce.xlsx": "Sheet!B4"}
    assert n_scanned == 1 and not failed


def test_fold_keeps_length():
    t = "İSTANBUL Iğdır ÇAĞ şöyle\nyeni"
    assert len(search.fold(t)) == len(t)
    assert search.fold(t) == "istanbul igdir cag soyle yeni"


def test_extract_images_dedupes_copies(tmp_path):
    """Aynı resim her sayfada ayrı nesne olarak gömülü (Word çıktısı gibi): tek dosya çıkmalı."""
    jpg = io.BytesIO()
    Image.new("RGB", (400, 300), (10, 120, 200)).save(jpg, "JPEG")
    other = io.BytesIO()
    Image.new("RGB", (300, 300), (200, 30, 30)).save(other, "PNG")
    out = pymupdf.open()
    for i in range(6):
        d = pymupdf.open()
        p = d.new_page()
        p.insert_image(pymupdf.Rect(50, 50, 450, 350), stream=jpg.getvalue())
        if i == 3:
            p.insert_image(pymupdf.Rect(50, 400, 350, 700), stream=other.getvalue())
        out.insert_pdf(d)
    assert len({pg.get_images()[0][0] for pg in out}) == 6          # gerçekten 6 ayrı nesne
    out.save(tmp_path / "kopya.pdf")
    msgs = []
    folder = docops.extract_images([tmp_path / "kopya.pdf"], msgs.append)[0]
    assert sorted(f.name for f in folder.iterdir()) == ["sayfa001_1.jpg", "sayfa004_2.png"]
    assert any("5 tekrar eden kopya" in m for m in msgs)
