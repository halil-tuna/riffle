"""Bütün sayfaları A4 yap, boyuta göre böl, Excel → PDF'te genişliğe sığdır."""

import io
import shutil

import pymupdf
import pytest

from riffle import docops
from riffle.actions import _fit_sheet_xml, office_to_pdf

noop = lambda m: None  # noqa: E731
A4 = pymupdf.paper_size("a4")


def test_to_a4_mixed_sizes(tmp_path):
    src = tmp_path / "karışık.pdf"
    d = pymupdf.open()
    for i, (w, h) in enumerate([pymupdf.paper_size("a5"), pymupdf.paper_size("letter"), (960, 540), A4]):
        pg = d.new_page(width=w, height=h)
        pg.insert_text((40, 60), f"Sayfa {i + 1}", fontsize=20)
    d.save(src)
    out = docops.to_a4([src], noop)[0]
    with pymupdf.open(out) as r:
        assert len(r) == 4
        sizes = [(round(p.rect.width), round(p.rect.height)) for p in r]
        assert sizes[0] == sizes[1] == sizes[3] == (595, 842)
        assert sizes[2] == (842, 595)                     # yatay slayt yatay A4
        assert all(f"Sayfa {i + 1}" in r[i].get_text() for i in range(4))   # metin vektör kalır


def test_to_a4_already_a4(tmp_path):
    src = tmp_path / "a4.pdf"
    d = pymupdf.open()
    d.new_page(width=A4[0], height=A4[1])
    d.save(src)
    assert docops.to_a4([src], noop) == []


def _heavy_pdf(path, pages=10):
    import numpy as np
    from PIL import Image
    d = pymupdf.open()
    rng = np.random.default_rng(0)
    for i in range(pages):
        pg = d.new_page()
        buf = io.BytesIO()
        Image.fromarray((rng.random((420, 420, 3)) * 255).astype("uint8")).save(buf, "JPEG", quality=92)
        pg.insert_image(pymupdf.Rect(50, 100, 500, 550), stream=buf.getvalue())
        pg.insert_text((50, 60), f"Sayfa {i + 1}", fontsize=18)
    d.save(path, deflate=True)
    return path


def test_split_by_size(tmp_path):
    src = _heavy_pdf(tmp_path / "rapor.pdf")
    assert src.stat().st_size > 1.5 * 1024 * 1024
    folder = docops.split_by_size([src], noop, size="1")[0]
    parts = sorted(folder.glob("*.pdf"), key=lambda p: int(p.stem.rsplit("parca", 1)[1]))
    assert folder.name == "rapor_parcalar" and len(parts) >= 2
    seen = []
    for p in parts:
        assert p.stat().st_size <= 1024 * 1024
        with pymupdf.open(p) as r:
            seen += [pg.get_text().strip().split("\n")[0] for pg in r]
    assert seen == [f"Sayfa {i}" for i in range(1, 11)]      # hiçbir sayfa kaybolmadı, sıra korundu


def test_split_by_size_small_file(tmp_path):
    src = tmp_path / "kucuk.pdf"
    d = pymupdf.open()
    d.new_page()
    d.save(src)
    assert docops.split_by_size([src], noop, size="10") == []


# ---------------------------------------------------------------- Excel genişliğe sığdır

def test_fit_sheet_xml_variants():
    base = ('<worksheet xmlns="x"><dimension ref="A1:P40"/><sheetData/>'
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/></worksheet>')
    out = _fit_sheet_xml(base)
    assert out.index("<sheetPr>") < out.index("<dimension")          # sheetPr ilk çocuk
    assert '<pageSetUpPr fitToPage="1"/>' in out
    assert 'fitToWidth="1" fitToHeight="0" orientation="landscape"' in out
    assert out.index("<pageMargins") < out.index("<pageSetup")
    narrow = base.replace("A1:P40", "A1:D40").replace("<sheetData/>", '<sheetData/>')
    assert "landscape" not in _fit_sheet_xml(narrow)
    selfclosing = base.replace("<dimension", '<sheetPr codeName="S1"/><dimension')
    assert '<sheetPr codeName="S1"><pageSetUpPr fitToPage="1"/></sheetPr>' in _fit_sheet_xml(selfclosing)
    user_scaled = base.replace("</worksheet>", '<pageSetup scale="70" orientation="portrait"/></worksheet>')
    assert _fit_sheet_xml(user_scaled) == user_scaled                 # kullanıcının ayarına dokunma
    with_hf = base.replace('footer="0.3"/>', 'footer="0.3"/><pageSetup orientation="portrait"/><headerFooter/>')
    out = _fit_sheet_xml(with_hf)
    assert 'fitToWidth="1"' in out and 'orientation="portrait"' in out and "landscape" not in out


@pytest.mark.skipif(not shutil.which("soffice") and not shutil.which("libreoffice"), reason="LibreOffice yok")
def test_excel_wide_sheet_fits_page_width(tmp_path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in range(1, 300):
        for c in range(1, 15):
            ws.cell(r, c, f"R{r}C{c} değer")
    p = tmp_path / "geniş.xlsx"
    wb.save(p)
    out = office_to_pdf([p], noop)[0]
    with pymupdf.open(out) as r:
        assert r[0].rect.width > r[0].rect.height                   # yatay
        assert "R1C14" in r[0].get_text() and "R1C1 " in r[0].get_text()   # bütün sütunlar ilk sayfada
        assert len(r) > 1                                            # satırlar sayfalara akar
