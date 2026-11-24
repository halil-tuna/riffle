"""Compress to target size, photo to document, folder to bookmarked PDF, bookmarks."""

from __future__ import annotations

import subprocess
import sys

import numpy as np
import pymupdf
import pytest

from conftest import ROOT, TRUE_CORNERS, make_photo, make_text_pdf, needs_ocr, needs_soffice, noop
from riffle import tools
from riffle.actions import ConversionError, merge_pdfs

cv2 = pytest.importorskip("cv2")


# ----------------------------------------------------------------- target size

@pytest.mark.parametrize("text,mb", [("2", 2), ("1,5", 1.5), ("1.5 MB", 1.5), ("500kb", 500 / 1024), ("300 KB", 300 / 1024)])
def test_parse_size(text, mb):
    assert abs(tools._parse_mb(text) - mb) < 1e-9


@pytest.mark.parametrize("text", ["", "abc", "0", "-2", "2 GB"])
def test_parse_size_errors(text):
    with pytest.raises(ConversionError):
        tools._parse_mb(text)


def test_size_tag_names():
    assert [tools._size_tag(t) for t in ("2", "1.5", "500kb")] == ["2MB", "1,5MB", "500KB"]


@pytest.fixture
def big_pdf(work):
    return make_text_pdf(work / "büyük rapor.pdf", pages=6, image=True)


@pytest.mark.parametrize("target", ["5", "2", "1", "400kb"])
def test_compress_to_target_meets_limit(big_pdf, target):
    limit = tools._parse_mb(target) * 1024 * 1024
    assert big_pdf.stat().st_size > limit
    out = tools.compress_to_target([big_pdf], noop, target=target)[0]
    assert out.stat().st_size <= limit
    with pymupdf.open(out) as d:
        assert len(d) == 6 and "Başlık" in d[0].get_text(), "metin korunmalı"


def test_compress_to_target_prefers_quality(big_pdf):
    """A loose target must give a bigger (higher quality) result than a tight one."""
    loose = tools.compress_to_target([big_pdf], noop, target="5")[0].stat().st_size
    tight = tools.compress_to_target([big_pdf], noop, target="200kb")[0].stat().st_size
    assert loose >= tight


def test_compress_to_target_already_small(pdf):
    msgs = []
    out = tools.compress_to_target([pdf], msgs.append, target="5")[0]
    assert out.read_bytes() == pdf.read_bytes()
    assert any("zaten" in m for m in msgs)


def test_compress_to_target_impossible(big_pdf):
    with pytest.raises(ConversionError, match="altına inmedi"):
        tools.compress_to_target([big_pdf], noop, target="25kb")


# ----------------------------------------------------------------- photo to document

def _corner_error(found, truth):
    return float(np.max(np.linalg.norm(np.array(found) - np.array(truth), axis=1)))


@pytest.mark.parametrize("desk,light", [((40, 70, 110), True),      # brown desk, sloping light
                                        ((95, 95, 95), False),      # grey desk (no colour separation)
                                        ((30, 30, 30), True)])      # dark desk
def test_scan_finds_corners(work, desk, light):
    from riffle import scan
    p = make_photo(work / "foto.jpg", desk=desk, light=light)
    c = scan.find_corners(cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR))
    assert c is not None
    assert _corner_error(c, TRUE_CORNERS) < 0.02 * 3200, c.round().tolist()


def test_scan_cropped_photo_keeps_whole(work):
    """A photo that is already cropped (the paper fills the whole frame) must not be corrected."""
    from riffle import scan
    p = make_photo(work / "kirpik.jpg", corners=[[0, 0], [3199, 0], [3199, 2399], [0, 2399]], light=False)
    assert scan.find_corners(cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)) is None
    data, (w, h), ok = scan.photo_to_page_image(p)
    assert not ok and (w, h) == (2480, 1860)


def test_scan_output_is_clean_white_paper(work):
    from riffle import scan
    p = make_photo(work / "foto.jpg")
    data, (w, h), ok = scan.photo_to_page_image(p)
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    assert ok and h > w, "perspektif düzeltilmiş dikey sayfa"
    assert np.percentile(img, 50) > 235, "kâğıt beyaz olmalı (gölge temizlenmiş)"
    corner = img[int(0.9 * h):, :int(0.1 * w)]          # shadowed corner
    assert corner.mean() > 225


@needs_ocr
def test_photos_to_document_ocr_and_order(work):
    a = make_photo(work / "IMG_2.jpg")
    b = make_photo(work / "IMG_10.jpg", desk=(95, 95, 95), light=False)
    msgs = []
    out = tools.photos_to_document([b, a], msgs.append)[0]
    assert out.name == "IMG_2_ve_1_foto_belge.pdf"
    with pymupdf.open(out) as d:
        assert len(d) == 2 and d[0].rect.height > d[0].rect.width
        assert "Paragraf" in d[0].get_text() and d[0].search_for("Başlık")
    assert any("kelime tanındı" in m for m in msgs)


def test_photo_exif_rotation_respected(work):
    from PIL import Image
    from riffle import scan
    p = make_photo(work / "yan.jpg")
    im = Image.open(p).rotate(90, expand=True)           # sensor shot sideways
    exif = Image.Exif()
    exif[0x0112] = 6                                      # "rotate 90° clockwise to display"
    q = work / "yan_exif.jpg"
    im.save(q, exif=exif)
    data, (w, h), ok = scan.photo_to_page_image(q)
    assert ok and h > w, "EXIF yönü uygulanmalı"


# ----------------------------------------------------------------- bookmarks

def test_merge_adds_bookmarks_and_keeps_source_toc(work):
    a = make_text_pdf(work / "Bölüm 1.pdf", pages=2, table=False)
    b = make_text_pdf(work / "Bölüm 2.pdf", pages=3, table=False)
    with pymupdf.open(b) as d:
        d.set_toc([[1, "Giriş", 1], [1, "Sonuç", 3]])
        d.saveIncr()
    out = merge_pdfs([b, a], noop)[0]
    with pymupdf.open(out) as d:
        assert d.get_toc() == [[1, "Bölüm 1", 1], [1, "Bölüm 2", 3], [2, "Giriş", 3], [2, "Sonuç", 5]]


def test_organizer_bookmarks_per_source(work):
    from riffle.organizer_model import Document
    a = make_text_pdf(work / "a.pdf", pages=2, table=False)
    b = make_text_pdf(work / "b.pdf", pages=1, table=False)
    doc = Document()
    doc.add_files([a, b])
    doc.move([2], 1)                     # a1, b1, a2
    out = doc.save(work, "karisik")[0]
    with pymupdf.open(out) as d:
        assert d.get_toc() == [[1, "a", 1], [1, "b", 2], [1, "a", 3]]


# ----------------------------------------------------------------- folder

@pytest.fixture
def course(work):
    from PIL import Image
    r = work / "MKT3411 Dersler"
    (r / "Hafta 2").mkdir(parents=True)
    (r / "Hafta 10").mkdir()
    (r / "Boş klasör").mkdir()
    make_text_pdf(r / "00 Plan.pdf", pages=1, table=False)
    make_text_pdf(r / "Hafta 2" / "Slayt.pdf", pages=2, table=False)
    make_text_pdf(r / "Hafta 10" / "Slayt.pdf", pages=1, table=False)
    Image.new("RGB", (900, 1200), (10, 200, 10)).save(r / "Hafta 10" / "tahta.jpg")
    (r / "~$kilit.docx").write_bytes(b"x")
    (r / ".gizli.pdf").write_bytes(b"x")
    (r / "bozuk.pdf").write_bytes(b"bozuk")
    return r


def test_folder_to_pdf_structure(course):
    msgs = []
    out = tools.folder_to_pdf([course], msgs.append)[0]
    assert out.parent == course.parent and out.name == "MKT3411 Dersler.pdf"
    with pymupdf.open(out) as d:
        assert len(d) == 5
        assert d.get_toc() == [[1, "00 Plan", 1],
                               [1, "Hafta 2", 2], [2, "Slayt", 2],
                               [1, "Hafta 10", 4], [2, "Slayt", 4], [2, "tahta", 5]]
    assert any("atlandı" in m and "bozuk.pdf" in m for m in msgs)
    again = tools.folder_to_pdf([course], noop)[0]
    assert again.name == "MKT3411 Dersler (1).pdf"


def test_folder_to_pdf_empty(work):
    e = work / "boş"
    e.mkdir()
    (e / "not.xyz").write_text("x")
    with pytest.raises(ConversionError, match="birleştirilecek"):
        tools.folder_to_pdf([e], noop)


@needs_soffice
def test_folder_includes_office(work):
    r = work / "Ödevler"
    r.mkdir()
    (r / "ödev1.txt").write_text("Birinci ödev metni", encoding="utf-8")
    make_text_pdf(r / "ödev2.pdf", pages=1, table=False)
    out = tools.folder_to_pdf([r], noop)[0]
    with pymupdf.open(out) as d:
        assert [t for _, t, _ in d.get_toc()] == ["ödev1", "ödev2"]
        assert "Birinci" in d[0].get_text()


def test_cli_folder_action(course):
    r = subprocess.run([sys.executable, str(ROOT / "run.pyw"), "folder2pdf", str(course)],
                       capture_output=True, text=True, timeout=120,
                       env={**__import__("os").environ, "RIFFLE_AUTOCLOSE_MS": "500"})
    assert r.returncode == 0, r.stderr
    assert (course.parent / "MKT3411 Dersler.pdf").exists()


def test_file_actions_reject_folders(course):
    r = subprocess.run([sys.executable, str(ROOT / "run.pyw"), "compress", str(course)],
                       capture_output=True, text=True, timeout=60,
                       env={**__import__("os").environ, "RIFFLE_AUTOCLOSE_MS": "500"})
    assert r.returncode == 1


@pytest.mark.parametrize("tilt_x,tilt_y,rot_z,dist", [(28, 12, 6, 520), (10, 5, 2, 520), (40, -15, -8, 600),
                                                      (-30, 20, 15, 550), (35, 0, 90, 650)])
def test_true_aspect_recovers_a4_from_camera_views(tilt_x, tilt_y, rot_z, dist):
    """For an angled shot the real aspect of the paper (A4 = 0.707) must come from the camera geometry."""
    from conftest import camera_corners
    from riffle import scan
    c = scan._order(np.array(camera_corners(tilt_x=tilt_x, tilt_y=tilt_y, rot_z=rot_z, dist=dist)))
    r = scan.true_aspect(c, 3200, 2400)
    assert r is not None
    expected = 210 / 297 if abs(rot_z) < 45 else 297 / 210
    # with one-directional perspective the focal length is assumed → slightly wider tolerance (A4 snapping at 8%)
    tol = 0.03 if tilt_y else 0.08
    assert abs(r - expected) / expected < tol, r
