"""PDF araçları: birleştir, böl, sıkıştır, döndür, şifre, sayfa işlemleri, numara, filigran, onar."""

from __future__ import annotations

import pymupdf
import pytest

from conftest import TR_TEXT, make_text_pdf, needs_soffice, noop
from riffle import tools
from riffle.actions import (BY_ID, ACTIONS, ConversionError, compress_pdf, merge_pdfs, pdf_to_jpg,
                            pdf_to_text, protect_pdf, rotate_pdf, split_pdf, unique_path, unlock_pdf)


# ----------------------------------------------------------------- katalog

def test_catalog_is_consistent():
    ids = [a.id for a in ACTIONS]
    assert len(ids) == len(set(ids)), "tekrarlanan işlem kimliği"
    for a in ACTIONS:
        assert a.label and a.extensions and callable(a.func)
        assert all(e == e.lower() and not e.startswith(".") for e in a.extensions)
        if a.ask:
            key, question, default = a.ask
            assert key and question
        if a.multi_only:
            assert a.batch, f"{a.id}: en az 2 dosya isteyen işlem toplu çalışmalı"


def test_unique_path_never_overwrites(work):
    (work / "a.pdf").write_text("x")
    (work / "a (1).pdf").write_text("x")
    assert unique_path(work, "a", ".pdf").name == "a (2).pdf"


# ----------------------------------------------------------------- temel araçlar

def test_merge_natural_order(work):
    files = [make_text_pdf(work / n, pages=1, table=False) for n in ("ek 10.pdf", "ek 2.pdf", "ek 1.pdf")]
    out = merge_pdfs(files, noop)[0]
    with pymupdf.open(out) as d:
        assert len(d) == 3
    assert out.name.startswith("ek 1_birlesik")


def test_merge_needs_two(pdf):
    with pytest.raises(ConversionError):
        merge_pdfs([pdf], noop)


def test_split_and_jpg_and_text(pdf):
    folder = split_pdf([pdf], noop)[0]
    assert sorted(f.name for f in folder.iterdir()) == [f"Ödev Raporu_s{i}.pdf" for i in (1, 2, 3)]
    jpgs = pdf_to_jpg([pdf], noop)[0]
    assert len(list(jpgs.glob("*.jpg"))) == 3
    txt = pdf_to_text([pdf], noop)[0]
    assert TR_TEXT.split()[0] in txt.read_text(encoding="utf-8")


def test_compress_levels(pdf_img):
    size = pdf_img.stat().st_size
    normal = compress_pdf([pdf_img], noop, level="normal")[0]
    strong = compress_pdf([pdf_img], noop, level="strong")[0]
    assert normal.stat().st_size < size
    assert strong.stat().st_size <= normal.stat().st_size
    assert "cok_sikistirilmis" in strong.name


def test_compress_never_grows(pdf):
    out = compress_pdf([pdf], noop)[0]
    assert out.stat().st_size <= pdf.stat().st_size


@pytest.mark.parametrize("angle", [90, 180, 270])
def test_rotate(pdf, angle):
    out = rotate_pdf([pdf], noop, angle=angle)[0]
    with pymupdf.open(out) as d:
        assert all(p.rotation == angle for p in d)
    assert out.name.endswith(f"_{angle}.pdf")


def test_password_roundtrip(pdf):
    enc = protect_pdf([pdf], noop, password="Şifre-123")[0]
    with pymupdf.open(enc) as d:
        assert d.needs_pass
    with pytest.raises(ConversionError):
        unlock_pdf([enc], noop, password="yanlış")
    plain = unlock_pdf([enc], noop, password="Şifre-123")[0]
    with pymupdf.open(plain) as d:
        assert not d.needs_pass and len(d) == 3


def test_protect_requires_password(pdf):
    with pytest.raises(ConversionError):
        protect_pdf([pdf], noop, password="")


@pytest.mark.parametrize("aid", ["compress", "pagenum", "split", "pdf2word", "extract"])
def test_encrypted_input_gives_clear_error(encrypted, aid):
    a = BY_ID[aid]
    kw = dict(a.kwargs)
    if a.ask:
        kw[a.ask[0]] = "1"
    with pytest.raises(ConversionError, match="şifreli"):
        a.func([encrypted], noop, **kw)


def test_garbage_file_gives_clear_error(garbage):
    with pytest.raises(ConversionError, match="açılamadı"):
        compress_pdf([garbage], noop)


# ----------------------------------------------------------------- sayfa aralığı

@pytest.mark.parametrize("spec,n,expected", [
    ("1-3,5", 6, [0, 1, 2, 4]),
    ("  2 - 4 ; 1 ", 5, [0, 1, 2, 3]),
    ("8-", 10, [7, 8, 9]),
    ("-2", 10, [0, 1]),
    ("son", 4, [3]),
    ("tek", 5, [0, 2, 4]),
    ("çift", 5, [1, 3]),
    ("3,3,1", 3, [0, 2]),
])
def test_parse_pages(spec, n, expected):
    assert tools.parse_pages(spec, n) == expected


@pytest.mark.parametrize("spec", ["", "0", "7", "3-1", "abc", "1-x"])
def test_parse_pages_errors(spec):
    with pytest.raises(ConversionError):
        tools.parse_pages(spec, 6)


def test_extract_delete_reverse(pdf):
    ext = tools.extract_pages([pdf], noop, pages="1,3")[0]
    with pymupdf.open(ext) as d:
        assert len(d) == 2 and "sayfa 3" in d[1].get_text()
    dele = tools.delete_pages([pdf], noop, pages="2")[0]
    with pymupdf.open(dele) as d:
        assert len(d) == 2 and "sayfa 3" in d[1].get_text()
    with pytest.raises(ConversionError):
        tools.delete_pages([pdf], noop, pages="1-3")
    rev = tools.reverse_pages([pdf], noop)[0]
    with pymupdf.open(rev) as d:
        assert "Başlık 3" in d[0].get_text()


# ----------------------------------------------------------------- numara / filigran

def _visual_bbox(page, needle):
    for b in page.get_text("dict")["blocks"]:
        for ln in b.get("lines", []):
            if needle in "".join(s["text"] for s in ln["spans"]):
                return pymupdf.Rect(ln["bbox"]) * page.rotation_matrix, ln["dir"]
    return None, None


def test_page_numbers_at_visual_bottom(rotated):
    out = tools.add_page_numbers([rotated], noop)[0]
    with pymupdf.open(out) as d:
        for i, page in enumerate(d):
            box, _ = _visual_bbox(page, f"{i + 1} / {len(d)}")
            assert box is not None, f"sayfa {i + 1}: numara yok"
            W, H = page.rect.width, page.rect.height
            assert box.y0 > 0.85 * H, f"sayfa {i + 1} (dönüş {page.rotation}): numara altta değil"
            assert abs((box.x0 + box.x1) / 2 - W / 2) < 0.1 * W, "numara ortalı değil"


def test_watermark_direction_on_all_rotations(rotated):
    out = tools.add_watermark([rotated], noop, text="GİZLİ Şube")[0]
    with pymupdf.open(out) as d:
        for page in d:
            box, direction = _visual_bbox(page, "GİZLİ")
            assert box is not None
            o = pymupdf.Point(0, 0) * page.rotation_matrix
            v = pymupdf.Point(direction) * page.rotation_matrix - o
            assert v.x > 0.5 and v.y < -0.5, f"dönüş {page.rotation}: filigran sol-alttan sağ-üste değil"
            c = (box.tl + box.br) / 2
            assert abs(c.x - page.rect.width / 2) < 0.15 * page.rect.width


def test_watermark_requires_text(pdf):
    with pytest.raises(ConversionError):
        tools.add_watermark([pdf], noop, text="  ")


# ----------------------------------------------------------------- onar

def test_repair_truncated(corrupted):
    out = tools.repair_pdf([corrupted], noop)[0]
    with pymupdf.open(out) as d:
        assert len(d) >= 1


def test_repair_garbage_fails_cleanly(garbage):
    with pytest.raises(ConversionError):
        tools.repair_pdf([garbage], noop)


def test_merge_keeps_chosen_order(tmp_path):
    from conftest import make_text_pdf
    from riffle.actions import merge_pdfs
    a, b, c = (make_text_pdf(tmp_path / f"{n}.pdf", pages=1, table=False) for n in ("s1", "s2", "s10"))
    out = merge_pdfs([c, a, b], lambda m: None)[0]                  # sırasız: doğal ad sırası
    assert [t[1] for t in pymupdf.open(out).get_toc()] == ["s1", "s2", "s10"]
    out = merge_pdfs([c, a, b], lambda m: None, ordered=True)[0]    # pencereden gelen sıra korunur
    assert [t[1] for t in pymupdf.open(out).get_toc()] == ["s10", "s1", "s2"]


def test_order_actions_registered():
    from riffle.actions import ACTIONS
    assert {a.id for a in ACTIONS if a.order} == {"merge", "img2pdf", "photo2doc", "compare", "organize"}
    assert next(a for a in ACTIONS if a.id == "organize").quick is not None


def test_cli_order_skipped_without_window(tmp_path, monkeypatch):
    """Pencere yoksa (ya da RIFFLE_ORDER=name) ad sırası kullanılır, soru sorulmaz."""
    from riffle import ui
    monkeypatch.setenv("RIFFLE_ORDER", "name")
    ps = [tmp_path / "b.pdf", tmp_path / "a.pdf"]
    assert ui.ask_order(ps, "t", "h") == ("ok", ps)
    monkeypatch.setenv("RIFFLE_ORDER", "organize")
    assert ui.ask_order(ps, "t", "h") == ("organize", ps)


def test_merge_files_mixed(tmp_path, images):
    """PDF + görsel karışık, verilen sırayla tek PDF; her dosya bir yer imi."""
    from conftest import make_text_pdf
    from riffle.tools import merge_files
    a = make_text_pdf(tmp_path / "rapor.pdf", pages=2, table=False)
    img = images[0]
    out = merge_files([img, a], lambda m: None, ordered=True)[0]
    d = pymupdf.open(out)
    assert len(d) == 3
    assert [t[1] for t in d.get_toc()] == [img.stem, "rapor"]
    assert out.name == f"{img.stem}_birlesik.pdf"


@needs_soffice
def test_merge_files_with_office_single_libreoffice_call(tmp_path, monkeypatch):
    """İki Word + bir PDF: Office dosyaları TEK LibreOffice çağrısında çevrilir."""
    from conftest import make_text_pdf
    from riffle import actions
    from riffle.tools import merge_files
    docs = []
    for n in ("ozet", "ek"):
        txt = tmp_path / f"{n}.txt"
        txt.write_text(f"{n} belgesi Türkçe ğüşıöç", encoding="utf-8")
        docs.append(txt)
    calls = []
    real = actions._run_cancellable

    def spy(cmd, *a, **k):
        if "--convert-to" in cmd:
            calls.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(actions, "_run_cancellable", spy)
    pdf = make_text_pdf(tmp_path / "ana.pdf", pages=1, table=False)
    out = merge_files([docs[0], pdf, docs[1]], lambda m: None, ordered=True)[0]
    assert len(calls) == 1
    d = pymupdf.open(out)
    assert [t[1] for t in d.get_toc()] == ["ozet", "ana", "ek"]
    assert "ozet belgesi" in d[0].get_text() and "ek belgesi" in d[-1].get_text()


def test_lo_profile_lock(tmp_path, monkeypatch):
    """Kalıcı LibreOffice profili kilitliyse ikinci işlem geçici profile düşer, kilit bırakılır."""
    from riffle import actions
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    with actions._LoProfile() as p1:
        with actions._LoProfile() as p2:
            assert p1 != p2 and "libreoffice-profil" in p1 and "libreoffice-profil" not in p2
    with actions._LoProfile() as p3:
        assert p3 == p1                                   # kilit kalkınca kalıcı profil yine kullanılır
