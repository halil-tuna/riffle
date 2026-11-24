"""OCR preprocessing: no words may be lost on shadowed / faint / phone-photographed scans."""

import collections
import io
import re

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageFilter

from conftest import needs_ocr
from riffle import ocr

TXT = ("Mekatronik mühendisliği öğrencileri bu dönem dalma erozyon tezgâhının kontrol sistemini tasarlıyor. "
       "Darbe üreteci saniyede on bin kez açılıp kapanırken boşluk mesafesi sürekli ölçülüyor. "
       "Güvenlik için acil durdurma düğmesi ve sıvı seviyesi sensörü eklenmiştir. "
       "Çalışmanın sonunda yüzey pürüzlülüğü ile malzeme kaldırma oranı karşılaştırılacaktır. "
       "Ölçümler üç farklı akım değerinde ve iki elektrot malzemesiyle tekrarlanacak. "
       "Sonuçlar haftalık toplantılarda danışman hocayla birlikte değerlendirilecektir. "
       "Bütçe kalemleri arasında güç kaynağı, motor sürücü ve dielektrik sıvı yer alıyor.")


def _page_image():
    from riffle.ocr import _font_file
    d = pymupdf.open()
    p = d.new_page()
    p.insert_textbox(pymupdf.Rect(60, 60, 535, 800), TXT, fontsize=10.5, fontname="f", fontfile=_font_file())
    pix = p.get_pixmap(dpi=200, colorspace=pymupdf.csGRAY)
    return Image.frombytes("L", (pix.width, pix.height), pix.samples)


def _degrade(im, kind):
    rng = np.random.default_rng(1)
    a = np.asarray(im, dtype=np.float32)
    h, w = a.shape
    if kind in ("golge", "telefon"):
        yy, xx = np.mgrid[0:h, 0:w]
        a = a * (1 - 0.55 * np.clip((xx / w * 0.8 + yy / h * 0.6) - 0.3, 0, 1))
    if kind == "soluk":
        a = 180 + a * 0.25
    if kind in ("soluk", "telefon"):
        a = a + rng.normal(0, 18, a.shape)
    out = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
    if kind == "telefon":
        out = out.filter(ImageFilter.GaussianBlur(1.3))
    b = io.BytesIO()
    out.save(b, "JPEG", quality=60)
    doc = pymupdf.open()
    pg = doc.new_page()
    pg.insert_image(pg.rect, stream=b.getvalue())
    return doc, pg


_GT = {}


def _acc(words):
    if "t" not in _GT:                                   # text that really fits on the page
        d = pymupdf.open()
        p = d.new_page()
        p.insert_textbox(pymupdf.Rect(60, 60, 535, 800), TXT, fontsize=10.5, fontname="f",
                         fontfile=ocr._font_file())
        _GT["t"] = p.get_text()
    gt = collections.Counter(re.findall(r"\w+", _GT["t"].lower()))
    got = collections.Counter(t for w in words for t in re.findall(r"\w+", w.text.lower()))
    return sum((gt & got).values()) / sum(gt.values())


@needs_ocr
@pytest.mark.parametrize("kind", ["temiz", "golge", "soluk", "telefon"])
def test_ocr_degraded_scans(kind):
    exe, langs = ocr.require()
    doc, page = _degrade(_page_image(), kind)
    assert _acc(ocr._ocr_words(exe, langs, page)) >= (0.93 if kind == "soluk" else 0.95)


def test_needs_cleaning_detects_shadow():
    im = _page_image()
    assert not ocr._needs_cleaning(im)
    shadow = np.asarray(im, dtype=np.float32)
    h, w = shadow.shape
    yy, xx = np.mgrid[0:h, 0:w]
    shadow = shadow * (1 - 0.5 * xx / w)
    assert ocr._needs_cleaning(Image.fromarray(shadow.astype(np.uint8)))


def test_needs_cleaning_ignores_sparse_clean_page():
    """A few dark handwritten lines on white paper (< 1 % ink) are not a 'faint' scan;
    a page whose only ink is light grey still is."""
    a = np.full((3300, 2550), 255, np.uint8)
    for k in range(6):
        a[400 + k * 300: 404 + k * 300, 300:1500] = 20      # thin dark strokes, ~0.3 % of the page
    assert not ocr._needs_cleaning(Image.fromarray(a))
    a[a == 20] = 200                                      # same strokes, faint
    assert ocr._needs_cleaning(Image.fromarray(a))


def _grid_scan(xs, ys):
    """A 'scanned' page (image only) with a table grid drawn 2 px thick at 200 dpi."""
    k = 200 / 72
    a = np.full((int(792 * k), int(612 * k)), 250, np.uint8)
    for x in xs:
        a[int(ys[0] * k):int(ys[-1] * k) + 2, int(x * k):int(x * k) + 2] = 30
    for y in ys:
        a[int(y * k):int(y * k) + 2, int(xs[0] * k):int(xs[-1] * k) + 2] = 30
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG")
    d = pymupdf.open()
    p = d.new_page(width=612, height=792)
    p.insert_image(p.rect, stream=buf.getvalue())
    return d, p


def test_rules_end_at_table_border_and_survive_overlapping_words():
    """Rules stop at the table's outer border (a horizontal rule sticking out by 0.2·L made Word add
    narrow phantom columns), and an OCR word box overlapping a vertical border does not cut it."""
    xs, ys = [72, 142, 325, 395, 468, 540], [100, 140, 180, 220, 260]
    doc, page = _grid_scan(xs, ys)
    # a date whose OCR box runs 3 pt over the border at x=395 (as on the real scan)
    words = [ocr.Word("07.12.2026", pymupdf.Rect(392, 105, 446, 120), 95, (1, 1, 1))]
    rules = ocr.detect_rules(page, words)
    hs = [r for r in rules if r[1] == r[3]]
    vs = [r for r in rules if r[0] == r[2]]
    assert len(hs) == len(ys) and len(vs) == len(xs)
    for x0, _, x1, _ in hs:
        assert abs(x0 - xs[0]) < 2 and abs(x1 - xs[-1]) < 3
    border = min(vs, key=lambda r: abs(r[0] - 395))
    assert border[1] < ys[0] + 3 and border[3] > ys[-1] - 3          # full height, not cut at the word
