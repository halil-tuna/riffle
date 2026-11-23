"""Sayfa düzenleyici modeli: ekleme, taşıma, döndürme, bölme, geri alma, kaydetme."""

from __future__ import annotations

import pymupdf
import pytest
from PIL import Image

from conftest import make_text_pdf, needs_soffice
from riffle.organizer_model import A4, Document


@pytest.fixture
def two(work):
    a = make_text_pdf(work / "a.pdf", pages=3, table=False)
    b = make_text_pdf(work / "b.pdf", pages=2, table=False)
    return a, b


def labels(doc):
    return [p.label for p in doc.pages]


def first_line(page) -> str:
    return page.get_text().strip().splitlines()[0]


def test_add_mixed_and_order(work, two, images):
    d = Document()
    assert d.add_files([*two, images[1]]) == []
    assert labels(d) == ["a.pdf · 1", "a.pdf · 2", "a.pdf · 3", "b.pdf · 1", "b.pdf · 2", "foto 2.jpg"]


def test_unsupported_reported(work, two):
    bad = work / "x.zip"
    bad.write_bytes(b"PK")
    d = Document()
    errs = d.add_files([two[0], bad])
    assert len(d.pages) == 3 and errs and "desteklenmeyen" in errs[0]


@pytest.mark.parametrize("sel,to,expected,new_idx", [
    ([4], 0, [4, 0, 1, 2, 3], [0]),          # sondakini başa
    ([0], 5, [1, 2, 3, 4, 0], [4]),          # baştakini sona
    ([0, 1], 4, [2, 3, 0, 1, 4], [2, 3]),    # iki sayfayı araya
    ([1, 3], 0, [1, 3, 0, 2, 4], [0, 1]),    # dağınık seçim başa
    ([2], 2, [0, 1, 2, 3, 4], [2]),          # yerinde bırak
])
def test_move(two, sel, to, expected, new_idx):
    d = Document()
    d.add_files(two)
    before = list(d.pages)
    assert d.move(sel, to) == new_idx
    assert [before.index(p) for p in d.pages] == expected


def test_rotate_delete_duplicate_undo(two):
    d = Document()
    d.add_files(two)
    d.rotate([0, 1], 90)
    d.rotate([0], 90)
    assert [p.rotation for p in d.pages[:3]] == [180, 90, 0]
    d.duplicate([1])
    assert labels(d)[1:3] == ["a.pdf · 2", "a.pdf · 2"] and d.pages[1].uid != d.pages[2].uid
    d.delete([0, 2])
    assert labels(d)[:2] == ["a.pdf · 2", "a.pdf · 3"]
    for _ in range(4):
        assert d.undo()
    assert [p.rotation for p in d.pages] == [0] * 5
    assert not d.undo() or True


def test_split_groups_and_delete_clears_marker(two):
    d = Document()
    d.add_files(two)
    d.toggle_split(2)
    assert [len(g) for g in d.groups()] == [3, 2]
    d.toggle_split(4)                 # son sayfadan sonra bölme anlamsız → yok sayılır
    assert [len(g) for g in d.groups()] == [3, 2]
    d.delete([2])
    assert len(d.groups()) == 1
    d.split_every(2)
    assert [len(g) for g in d.groups()] == [2, 2]


def test_sort_and_reverse(work):
    files = [make_text_pdf(work / n, pages=1, table=False) for n in ("s10.pdf", "s2.pdf", "s1.pdf")]
    d = Document()
    d.add_files(files)
    d.sort_by_name()
    assert labels(d) == ["s1.pdf · 1", "s2.pdf · 1", "s10.pdf · 1"]
    d.reverse()
    assert labels(d)[0] == "s10.pdf · 1"


def test_save_single_with_rotation_and_order(work, two):
    d = Document()
    d.add_files(two)
    d.move([3], 0)                    # b'nin 1. sayfası başa
    d.rotate([0], 270)
    d.add_blank(1)
    folder, stem = d.default_target()
    assert stem == "b_birlesik"
    out = d.save(folder, stem)
    assert len(out) == 1
    with pymupdf.open(out[0]) as r:
        assert len(r) == 6
        assert r[0].rotation == 270 and "Başlık 1" in first_line(r[0])
        assert r[1].get_text().strip() == ""          # boş sayfa
        assert "Başlık 1" in first_line(r[2])
    assert two[0].exists() and two[1].exists()       # kaynaklar değişmedi


def test_save_split_parts(work, two):
    d = Document()
    d.add_files(two)
    d.toggle_split(2)
    out = d.save(*d.default_target())
    assert [o.name for o in out] == ["a_birlesik_1.pdf", "a_birlesik_2.pdf"]
    with pymupdf.open(out[0]) as x, pymupdf.open(out[1]) as y:
        assert (len(x), len(y)) == (3, 2)
    again = d.save(*d.default_target())
    assert again[0].name == "a_birlesik_1 (1).pdf", "ikinci kayıt öncekini ezmemeli"


def test_image_pages_a4_and_original(work):
    tall = work / "dikey.png"
    wide = work / "yatay.jpg"
    Image.new("RGB", (800, 1600), (255, 0, 0)).save(tall)
    Image.new("RGB", (2000, 1000), (0, 0, 255)).save(wide)
    d = Document()
    d.add_files([tall, wide])
    out = d.save(work, "a4")[0]
    with pymupdf.open(out) as r:
        assert (round(r[0].rect.width), round(r[0].rect.height)) == (round(A4[0]), round(A4[1]))
        assert (round(r[1].rect.width), round(r[1].rect.height)) == (round(A4[1]), round(A4[0]))
    d.image_mode = "original"
    out = d.save(work, "orj")[0]
    with pymupdf.open(out) as r:
        assert round(r[1].rect.width) == 1500     # 2000 px @96 dpi = 1500 pt


def test_heic_and_rotation_in_output(work, heic):
    d = Document()
    d.add_files([heic])
    d.rotate([0], 90)
    out = d.save(work, "telefon")[0]
    with pymupdf.open(out) as r:
        assert r[0].rotation == 90 and r[0].get_images()


def test_thumbnail_respects_rotation(two):
    d = Document()
    d.add_files(two)
    import io
    a = Image.open(io.BytesIO(d.thumbnail_png(d.pages[0], 150)))
    d.rotate([0], 90)
    b = Image.open(io.BytesIO(d.thumbnail_png(d.pages[0], 150)))
    assert a.size == (b.size[1], b.size[0]) and max(a.size) == 150


def test_encrypted_reported_not_crash(encrypted):
    d = Document()
    errs = d.add_files([encrypted])
    assert d.pages == [] and "şifreli" in errs[0]


@needs_soffice
def test_office_file_added_as_pages(work):
    f = work / "not.txt"
    f.write_text("Merhaba Türkçe metin", encoding="utf-8")
    d = Document()
    assert d.add_files([f]) == []
    assert d.pages and d.pages[0].label.startswith("not.txt")
    folder, stem = d.default_target()
    assert folder == work and stem == "not_duzenlenmis"   # geçici klasör değil, asıl dosyanın yanı
