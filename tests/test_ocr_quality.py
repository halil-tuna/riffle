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
