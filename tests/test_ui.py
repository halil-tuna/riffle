"""Gerçek pencereler: sanal ekranda (Xvfb) açılır, xdotool ile klavyeden yazılır.

Çalıştırma (Linux):  xvfb-run -a python -m pytest tests/test_ui.py
tkinter / DISPLAY / xdotool yoksa atlanır.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

import pymupdf
import pytest

from conftest import ROOT

tk = pytest.importorskip("tkinter")
pytestmark = pytest.mark.skipif(not os.environ.get("DISPLAY") or not shutil.which("xdotool"),
                                reason="sanal ekran / xdotool yok")
RUN = ROOT / "run.pyw"


def type_into_window(title_part: str, text: str, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["xdotool", "search", "--name", title_part], capture_output=True, text=True)
        wins = r.stdout.split()
        if wins:
            w = wins[-1]
            subprocess.run(["xdotool", "windowfocus", "--sync", w], capture_output=True)
            time.sleep(0.3)
            subprocess.run(["xdotool", "type", "--delay", "30", text])
            subprocess.run(["xdotool", "key", "Return"])
            return True
        time.sleep(0.2)
    return False


def start(*args):
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="1500")
    return subprocess.Popen([sys.executable, str(RUN), *map(str, args)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_progress_window_runs_and_closes(pdf):
    p = start("compress", pdf)
    assert p.wait(timeout=60) == 0
    assert any(pdf.parent.glob("*_sikistirilmis.pdf"))


def test_page_range_dialog(pdf):
    p = start("extract", pdf)
    time.sleep(1.0)
    # alanı temizle (varsayılan boş) ve yaz
    assert type_into_window("Riffle", "1,3"), "sayfa aralığı penceresi açılmadı"
    assert p.wait(timeout=60) == 0, p.stderr.read()
    out = next(pdf.parent.glob("*_secilen_sayfalar.pdf"))
    with pymupdf.open(out) as d:
        assert len(d) == 2


def test_password_dialogs_confirm(pdf):
    p = start("protect", pdf)
    time.sleep(1.0)
    assert type_into_window("Riffle", "Gizli42")
    time.sleep(0.8)
    assert type_into_window("Riffle", "Gizli42"), "onay penceresi açılmadı"
    assert p.wait(timeout=60) == 0, p.stderr.read()
    out = next(pdf.parent.glob("*_sifreli.pdf"))
    with pymupdf.open(out) as d:
        assert d.needs_pass and d.authenticate("Gizli42")


def test_watermark_dialog_default_text(pdf):
    p = start("watermark", pdf)
    time.sleep(1.0)
    assert type_into_window("Riffle", "")      # varsayılan 'TASLAK' kabul
    assert p.wait(timeout=60) == 0, p.stderr.read()
    out = next(pdf.parent.glob("*_filigranli.pdf"))
    with pymupdf.open(out) as d:
        assert "TASLAK" in d[0].get_text()


# ----------------------------------------------------------------- sayfa düzenleyici

def _window(name_part, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["xdotool", "search", "--name", name_part], capture_output=True, text=True)
        if r.stdout.split():
            return r.stdout.split()[0]
        time.sleep(0.3)
    return None


def _xdo(*args):
    subprocess.run(["xdotool", *map(str, args)], capture_output=True)
    time.sleep(0.25)


def _cell_center(win, index, cols):
    """Düzenleyicideki küçük resmin pencereye göre merkezi (organizer.py ölçüleriyle)."""
    from riffle.organizer import CELL_H, CELL_W, PAD
    geo = subprocess.run(["xdotool", "getwindowgeometry", win], capture_output=True, text=True).stdout
    x0 = PAD + (index % cols) * (CELL_W + PAD) + CELL_W // 2
    y0 = PAD + (index // cols) * (CELL_H + PAD) + CELL_H // 2
    return x0, y0 + 46   # 46 ≈ araç çubuğu yüksekliği


def test_organizer_drag_rotate_split_save(work):
    from conftest import make_text_pdf
    a = make_text_pdf(work / "a.pdf", pages=3, table=False)
    b = make_text_pdf(work / "b.pdf", pages=2, table=False)
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="25000")
    env = dict(env, RIFFLE_ORDER="organize")     # çoklu seçim: sıra penceresini atla, düzenleyici
    p = subprocess.Popen([sys.executable, str(RUN), "organize", str(a), str(b)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("Sayfa d")
    assert win, "düzenleyici penceresi açılmadı"
    time.sleep(2.5)                                   # küçük resimler
    _xdo("windowfocus", "--sync", win)
    cols = 6
    # 5. sayfayı (b·2) en başa sürükle
    x, y = _cell_center(win, 4, cols)
    tx, ty = _cell_center(win, 0, cols)
    _xdo("mousemove", "--window", win, x, y)
    _xdo("mousedown", "1")
    for k in range(1, 9):
        _xdo("mousemove", "--window", win, x + (tx - 70 - x) * k // 8, y)
    _xdo("mouseup", "1")
    # ilk sayfayı sağa döndür, 3. sayfadan sonra böl, kaydet
    _xdo("mousemove", "--window", win, tx, ty)
    _xdo("click", "1")
    _xdo("key", "ctrl+r")
    x3, y3 = _cell_center(win, 2, cols)
    _xdo("mousemove", "--window", win, x3, y3)
    _xdo("click", "1")
    _xdo("key", "ctrl+b")
    _xdo("key", "ctrl+s")
    deadline = time.time() + 20
    outs = []
    while time.time() < deadline and len(outs) < 2:
        outs = sorted(work.glob("b_birlesik_*.pdf"))
        time.sleep(0.5)
    p.kill()
    assert [o.name for o in outs] == ["b_birlesik_1.pdf", "b_birlesik_2.pdf"], list(work.iterdir())
    with pymupdf.open(outs[0]) as d1, pymupdf.open(outs[1]) as d2:
        assert (len(d1), len(d2)) == (3, 2)
        assert d1[0].rotation == 90
        assert "Başlık 2" in d1[0].get_text() and "Başlık 1" in d1[1].get_text()
        assert "Başlık 3" in d2[0].get_text()


def test_organizer_keyboard_delete_undo(work):
    from conftest import make_text_pdf
    a = make_text_pdf(work / "tek.pdf", pages=4, table=False)
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="20000")
    p = subprocess.Popen([sys.executable, str(RUN), "organize", str(a)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("Sayfa d")
    assert win
    time.sleep(2)
    _xdo("windowfocus", "--sync", win)
    x, y = _cell_center(win, 0, 6)
    _xdo("mousemove", "--window", win, x, y)
    _xdo("click", "1")
    _xdo("key", "Delete")                 # 1. sayfa silindi
    _xdo("key", "ctrl+a")
    _xdo("key", "Delete")                 # hepsi silindi
    _xdo("key", "ctrl+z")                 # geri al → 3 sayfa
    _xdo("key", "ctrl+s")
    deadline = time.time() + 15
    out = None
    while time.time() < deadline and out is None:
        out = next(work.glob("tek_duzenlenmis*.pdf"), None)
        time.sleep(0.5)
    p.kill()
    assert out is not None
    with pymupdf.open(out) as d:
        assert len(d) == 3 and "Başlık 2" in d[0].get_text()


# ----------------------------------------------------------------- imza / yazı penceresi

def test_stamper_signature_all_pages_date(work, tmp_path):
    """Kayıtlı imza Ctrl+I ile eklenir, Ctrl+G ile tüm sayfalara kopyalanır, Ctrl+D tarih, Ctrl+S kaydet."""
    import io
    from datetime import date
    from PIL import Image
    from conftest import make_text_pdf
    a = make_text_pdf(work / "sozlesme.pdf", pages=3, table=False)
    cfg = tmp_path / "cfg"
    (cfg / "riffle").mkdir(parents=True)
    buf = io.BytesIO()
    Image.new("RGBA", (300, 100), (16, 32, 96, 255)).save(buf, "PNG")
    (cfg / "riffle" / "imza.png").write_bytes(buf.getvalue())
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="20000", XDG_CONFIG_HOME=str(cfg))
    p = subprocess.Popen([sys.executable, str(RUN), "sign", str(a)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("mza / yaz")                 # xdotool ASCII dışı aramada eşleşmiyor
    assert win, "imza penceresi açılmadı"
    time.sleep(3)
    _xdo("windowfocus", "--sync", win)
    _xdo("key", "ctrl+i")           # imza (seçili olur)
    _xdo("key", "ctrl+g")           # tüm sayfalara
    _xdo("key", "ctrl+d")           # tarih (yalnızca 1. sayfa)
    _xdo("key", "Up")               # tarihi biraz kaydır
    _xdo("key", "ctrl+s")
    deadline = time.time() + 15
    out = None
    while time.time() < deadline and out is None:
        out = next(work.glob("sozlesme_imzali*.pdf"), None)
        time.sleep(0.5)
    p.kill()
    assert out is not None, p.stderr.read() if p.stderr else ""
    with pymupdf.open(out) as d:
        assert [len(pg.get_images()) for pg in d] == [1, 1, 1]
        today = date.today().strftime("%d.%m.%Y")
        assert today in d[0].get_text() and today not in d[1].get_text()
        # imza sağ altta
        r = d[0].get_image_rects(d[0].get_images()[0][0])[0]
        assert r.x0 > d[0].rect.width / 2 and r.y0 > d[0].rect.height / 2


def test_organizer_preview_rotate(work):
    """Çift tık → büyük önizleme; R ile döndür, → ile sonraki sayfa, Esc kapat, Ctrl+S kaydet."""
    from conftest import make_text_pdf
    a = make_text_pdf(work / "onizle.pdf", pages=3, table=False)
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="20000")
    p = subprocess.Popen([sys.executable, str(RUN), "organize", str(a)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("Sayfa d")
    assert win
    time.sleep(2)
    _xdo("windowfocus", "--sync", win)
    x, y = _cell_center(win, 0, 6)
    _xdo("mousemove", "--window", win, x, y)
    _xdo("click", "--repeat", "2", "--delay", "80", "1")
    pv = _window("nizleme", timeout=8)
    assert pv, "önizleme açılmadı"
    time.sleep(0.8)
    _xdo("windowfocus", "--sync", pv)
    _xdo("key", "Right")                  # 2. sayfa
    _xdo("key", "r")                      # sağa döndür
    _xdo("key", "Escape")
    time.sleep(0.4)
    _xdo("windowfocus", "--sync", win)
    _xdo("key", "ctrl+s")
    deadline = time.time() + 15
    out = None
    while time.time() < deadline and out is None:
        out = next(work.glob("onizle_duzenlenmis*.pdf"), None)
        time.sleep(0.5)
    p.kill()
    assert out is not None
    with pymupdf.open(out) as d:
        assert [pg.rotation for pg in d] == [0, 90, 0]


# ----------------------------------------------------------------- birleştirme sırası penceresi

def _order_env():
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="20000")
    env.pop("RIFFLE_ORDER", None)
    return env


def test_merge_order_dialog(work):
    """3 PDF seçilip birleştir: sıra penceresi açılır, Ctrl+↓ ile ilk dosya sona taşınır, Enter."""
    from conftest import make_text_pdf
    files = [make_text_pdf(work / f"{n}.pdf", pages=1, table=False) for n in ("a_giris", "b_yontem", "c_sonuc")]
    p = subprocess.Popen([sys.executable, str(RUN), "merge", *map(str, files)], env=_order_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("birle")
    assert win, "sıralama penceresi açılmadı"
    time.sleep(1.5)
    _xdo("windowfocus", "--sync", win)
    _xdo("key", "ctrl+Down")
    _xdo("key", "ctrl+Down")
    _xdo("key", "Return")
    deadline = time.time() + 20
    out = None
    while time.time() < deadline and out is None:
        out = next(work.glob("*_birlesik*.pdf"), None)
        time.sleep(0.5)
    time.sleep(0.5)
    p.kill()
    assert out is not None
    with pymupdf.open(out) as d:
        assert [t[1] for t in d.get_toc()] == ["b_yontem", "c_sonuc", "a_giris"]
    assert out.name.startswith("b_yontem")          # çıktı adı da yeni ilk dosyadan


def test_merge_order_cancel(work):
    from conftest import make_text_pdf
    files = [make_text_pdf(work / f"{n}.pdf", pages=1, table=False) for n in ("x1", "x2")]
    p = subprocess.Popen([sys.executable, str(RUN), "merge", *map(str, files)], env=_order_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("birle")
    assert win
    time.sleep(1.5)
    _xdo("windowfocus", "--sync", win)
    _xdo("key", "Escape")
    assert p.wait(timeout=15) == 1
    assert not list(work.glob("*_birlesik*.pdf"))


def test_organize_multi_opens_quick_order_then_merges(work):
    """Çoklu seçimde 'Sayfaları düzenle / birleştir': önce küçük sıra penceresi (sayfa açılmaz),
    Enter ile doğrudan birleştirir; sayfa düzenleyici hiç açılmaz."""
    from conftest import make_text_pdf
    a = make_text_pdf(work / "bolum1.pdf", pages=2, table=False)
    b = make_text_pdf(work / "bolum2.pdf", pages=1, table=False)
    p = subprocess.Popen([sys.executable, str(RUN), "organize", str(a), str(b)], env=_order_env(),
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t0 = time.time()
    win = _window("birle")
    assert win and time.time() - t0 < 8
    time.sleep(1.2)
    assert not subprocess.run(["xdotool", "search", "--name", "Sayfa d"], capture_output=True, text=True).stdout
    _xdo("windowfocus", "--sync", win)
    _xdo("key", "ctrl+Down")                     # bolum1'i sona al
    _xdo("key", "Return")
    deadline = time.time() + 20
    out = None
    while time.time() < deadline and out is None:
        out = next(work.glob("bolum2_birlesik*.pdf"), None)
        time.sleep(0.5)
    time.sleep(0.5)
    p.kill()
    assert out is not None
    with pymupdf.open(out) as d:
        assert len(d) == 3 and [t[1] for t in d.get_toc()] == ["bolum2", "bolum1"]


def test_folder_search_window(work):
    from conftest import make_text_pdf
    make_text_pdf(work / "ders notu.pdf", pages=2, table=False)
    (work / "ek.txt").write_text("Öğrenci listesi", encoding="utf-8")
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="12000")
    p = subprocess.Popen([sys.executable, str(RUN), "search", str(work)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("Klas")
    assert win
    time.sleep(1.5)
    _xdo("windowfocus", "--sync", win)
    _xdo("type", "--delay", "40", "ogrenci")
    _xdo("key", "Return")
    out, err = p.communicate(timeout=30)
    line = next((ln for ln in out.splitlines() if ln.startswith("Arama:")), "")
    assert "2 dosyada" in line, out + err


def test_form_window(work):
    from test_forms import make_form
    src = make_form(work / "başvuru formu.pdf")
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="15000")
    p = subprocess.Popen([sys.executable, str(RUN), "form", str(src)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("Form doldur")
    assert win
    time.sleep(1.5)
    _xdo("windowfocus", "--sync", win)
    _xdo("type", "--delay", "30", "Halil Tuna")       # ilk alan odakta
    _xdo("key", "ctrl+s")
    deadline = time.time() + 15
    out = None
    while time.time() < deadline and out is None:
        out = next(work.glob("*_doldurulmus*.pdf"), None)
        time.sleep(0.4)
    p.kill()
    assert out is not None
    vals = {w.field_name: w.field_value for w in pymupdf.open(out)[0].widgets()}
    assert vals["ad"] == "Halil Tuna"


def test_settings_window_saves(tmp_path):
    cfg = tmp_path / "cfg"
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="12000", XDG_CONFIG_HOME=str(cfg))
    p = subprocess.Popen([sys.executable, str(RUN), "--settings"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("Ayarlar")
    assert win
    time.sleep(1.5)
    _xdo("windowfocus", "--sync", win)
    _xdo("key", "ctrl+s")
    deadline = time.time() + 10
    f = cfg / "riffle" / "ayarlar.json"
    while time.time() < deadline and not f.exists():
        time.sleep(0.3)
    p.kill()
    import json
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["output"] == "beside" and data["auto_favorites"] is True


def test_progress_cancel_button(work):
    """Uzun PDF → Word sırasında Esc (İptal): yarım dosya kalmaz, pencere 'İptal edildi' der."""
    from conftest import make_text_pdf
    src = make_text_pdf(work / "kalin kitap.pdf", pages=250, table=True)
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="3000")
    p = subprocess.Popen([sys.executable, str(RUN), "pdf2wordf", str(src)], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    win = _window("Riffle")
    assert win
    time.sleep(1.5)
    _xdo("windowfocus", "--sync", win)
    _xdo("key", "Escape")
    out, err = p.communicate(timeout=60)
    assert "İptal edildi" in out, out + err
    assert not list(work.glob("*.docx"))
