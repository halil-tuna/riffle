"""PDF form doldurma."""

import pymupdf
import pytest

from conftest import _text
from riffle import forms


def make_form(path):
    d = pymupdf.open()
    p = d.new_page()

    def add(kind, name, rect, label=None, **kw):
        w = pymupdf.Widget()
        w.field_type, w.field_name, w.rect = kind, name, pymupdf.Rect(rect)
        if label:
            w.field_label = label
        for k, v in kw.items():
            setattr(w, k, v)
        p.add_widget(w)

    _text(p, (50, 80), "Ad Soyad:")
    add(pymupdf.PDF_WIDGET_TYPE_TEXT, "ad", (150, 65, 450, 85), "Adınız ve soyadınız")
    _text(p, (50, 120), "Bölüm:")
    add(pymupdf.PDF_WIDGET_TYPE_COMBOBOX, "bolum", (150, 105, 450, 125), choice_values=["Mekatronik", "Makine"])
    _text(p, (50, 160), "Burslu mu:")
    add(pymupdf.PDF_WIDGET_TYPE_CHECKBOX, "burs", (150, 148, 164, 162))
    _text(p, (50, 200), "Açıklama:")
    add(pymupdf.PDF_WIDGET_TYPE_TEXT, "aciklama", (150, 190, 450, 260), field_flags=pymupdf.PDF_TX_FIELD_IS_MULTILINE)
    _text(p, (50, 290), "Sınıf:")
    add(pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "sinif", (150, 280, 162, 292))
    _text(p, (166, 290), "3.")
    add(pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON, "sinif", (200, 280, 212, 292))
    _text(p, (216, 290), "4.")
    _text(p, (50, 330), "Kimlik:")
    add(pymupdf.PDF_WIDGET_TYPE_TEXT, "kimlik", (150, 318, 450, 336), field_flags=pymupdf.PDF_FIELD_IS_READ_ONLY)
    # gerçek formlardaki gibi her radyo seçeneğinin kendi "açık" durumu olsun (PyMuPDF ikisine de /None veriyor)
    for k, w in enumerate(x for x in p.widgets() if x.field_type == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON):
        ap = d.xref_get_key(w.xref, "AP/N")[1]
        d.xref_set_key(w.xref, "AP/N", ap.replace("/None", f"/Secim{k + 1}"))
        d.xref_set_key(w.xref, "AS", "/Off")
        parent = int(d.xref_get_key(w.xref, "Parent")[1].split()[0])
    d.xref_set_key(parent, "V", "/Off")
    d.save(path)
    return path


def test_read_fields(tmp_path):
    fs = forms.read_fields(pymupdf.open(make_form(tmp_path / "f.pdf")))
    got = [(f.name, f.kind, f.label) for f in fs]
    assert got == [("ad", "text", "Adınız ve soyadınız"), ("bolum", "combo", "Bölüm"),
                   ("burs", "check", "Burslu mu"), ("aciklama", "text", "Açıklama"),
                   ("sinif", "radio", "Sınıf"), ("kimlik", "text", "Kimlik")]
    by = {f.name: f for f in fs}
    assert by["aciklama"].multiline and by["kimlik"].readonly
    assert by["bolum"].options == ["Mekatronik", "Makine"]
    assert [lab for _, lab in by["sinif"].options] == ["3.", "4."]


@pytest.mark.parametrize("flatten", [False, True])
def test_fill(tmp_path, flatten):
    src = make_form(tmp_path / "f.pdf")
    out = forms.fill(src, {"ad": "Halil Tuna Ğüşıöç İŞ", "bolum": "Mekatronik", "burs": True,
                           "aciklama": "Birinci satır\nİkinci satır", "sinif": "1"}, flatten=flatten)
    assert out.name == "f_doldurulmus.pdf"
    d = pymupdf.open(out)
    text = d[0].get_text()
    if flatten:
        assert not list(d[0].widgets() or [])                  # alan kalmadı
        assert "Halil Tuna Ğüşıöç İŞ" in text and "İkinci satır" in text
    else:
        vals = {}
        for w in d[0].widgets():
            vals.setdefault(w.field_name, []).append(w.field_value)
        assert vals["ad"] == ["Halil Tuna Ğüşıöç İŞ"] and vals["bolum"] == ["Mekatronik"]
        assert vals["burs"][0] not in ("Off", "", False)
        assert vals["sinif"][0] in ("Off", False) and vals["sinif"][1] not in ("Off", False)
        f = {x.name: x for x in forms.read_fields(d)}           # geri okununca aynı değerler
        assert f["sinif"].value == "1" and f["burs"].value is True


def test_no_fields_message(tmp_path, pdf):
    assert forms.read_fields(pymupdf.open(pdf)) == []
