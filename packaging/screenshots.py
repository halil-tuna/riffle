"""README ve görünüm kontrolü için pencere ekran görüntüleri (Linux, sanal ekranda).

    xvfb-run -a -s "-screen 0 1600x1000x24" python packaging/screenshots.py docs/screenshots [--dark]

Gerekenler: tkinter, xdotool, ImageMagick (import). Her pencere gerçek programla açılır,
xdotool ile bulunur ve yalnızca o pencerenin görüntüsü alınır.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


_PARA = ("Bu bölümde projenin amacı, kapsamı ve yöntemi özetlenir. Ölçümler üç tekrarlı yapılmış, "
         "sonuçlar ortalama ve standart sapma ile verilmiştir. Proje takvimi dört aşamadan oluşur: "
         "literatür taraması, tasarım, prototip ve test. ")
_EN = ("The proposed controller adjusts the feed rate according to the measured gap voltage. Experiments "
       "show a stable discharge regime and a lower electrode wear ratio compared with the fixed-feed baseline. ")


def _html_pdf(path: Path, pages: list[str], css: str = "") -> Path:
    import pymupdf
    d = pymupdf.open()
    for html in pages:
        pg = d.new_page()
        pg.insert_htmlbox(pg.rect + (56, 56, -56, -56), html,
                          css="* {font-family: sans-serif; font-size: 10.5pt; line-height: 1.35;} "
                              "h1 {font-size: 17pt;} h2 {font-size: 12.5pt;} "
                              "td, th {border: 0.6pt solid #666; padding: 3pt;} table {border-collapse: collapse;} " + css)
    d.save(path)
    return path


def _sample_pdfs(work: Path) -> dict:
    """Yapay örnek belgeler (README görüntüleri herkese açık: gerçek kişi/kurum bilgisi yok)."""
    import pymupdf
    out = {}
    rows = "".join(f"<tr><td>Faz {i}</td><td>Görev {i}.{k}</td><td>Ekip {k}</td><td>{10 + i}.03.2026</td></tr>"
                   for i in range(1, 4) for k in range(1, 4))
    out["Rapor.pdf"] = _html_pdf(work / "Rapor.pdf", [
        f"<h1>Proje Raporu</h1><p><b>Konu:</b> Örnek proje · <b>Tarih:</b> 12.03.2026</p><h2>1. Giriş</h2>"
        f"<p>{_PARA * 3}</p><h2>2. İş planı</h2><table><tr><th>Aşama</th><th>Görev</th><th>Sorumlu</th>"
        f"<th>Bitiş</th></tr>{rows}</table>",
        f"<h2>3. Yöntem</h2><p>{_PARA * 6}</p>", f"<h2>4. Sonuçlar</h2><p>{_PARA * 5}</p>",
        f"<h2>5. Kaynaklar</h2><p>{_PARA * 2}</p>"])
    out["Makale.pdf"] = _html_pdf(work / "Makale.pdf", [
        f"<h1>Adaptive gap control for a benchtop machine</h1><p><i>Example article</i></p><h2>Abstract</h2>"
        f"<p>{_EN * 4}</p><h2>1. Introduction</h2><p>{_EN * 8}</p>"] + [f"<h2>{i}. Section</h2><p>{_EN * 14}</p>"
                                                                        for i in range(2, 7)])
    out["Ders notu.pdf"] = _html_pdf(work / "Ders notu.pdf", [
        f"<h1>Hafta {i}: Proje yönetimi</h1><p>{_PARA * 7}</p>" for i in range(1, 9)])
    # doldurulabilir form
    form = work / "Staj başvuru formu.pdf"
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((60, 70), "STAJ BAŞVURU FORMU", fontsize=16)
    y = 110
    for label, kind in (("Ad Soyad", "text"), ("Öğrenci No", "text"), ("Bölüm", "combo"),
                        ("Staj türü", "check"), ("Açıklama", "text")):
        pg.insert_text((60, y + 14), label, fontsize=11)
        w = pymupdf.Widget()
        w.field_name = label
        w.field_label = label
        w.rect = pymupdf.Rect(180, y, 480, y + 20)
        if kind == "text":
            w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        elif kind == "combo":
            w.field_type = pymupdf.PDF_WIDGET_TYPE_COMBOBOX
            w.choice_values = ["Mekatronik", "Makine", "Elektrik"]
        else:
            w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            w.rect = pymupdf.Rect(180, y, 196, y + 16)
        pg.add_widget(w)
        y += 40
    d.save(form)
    out["form"] = form
    return out


def _window(part: str, timeout: float = 25) -> str | None:
    end = time.time() + timeout
    while time.time() < end:
        r = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", part], capture_output=True, text=True)
        ids = r.stdout.split()
        if ids:
            return ids[-1]
        time.sleep(0.25)
    return None


def _shot(win: str, path: Path) -> None:
    subprocess.run(["import", "-window", win, str(path)], check=False, capture_output=True)
    print("  →", path.name, "✔" if path.exists() else "✖")


def capture(outdir: Path, dark: bool) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="riffle_shots_")) / "Ödevler"
    work.mkdir()
    s = _sample_pdfs(work)
    suffix = "-dark" if dark else ""
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="60000", RIFFLE_NO_MENU_REFRESH="1",
               RIFFLE_THEME="dark" if dark else "light",
               XDG_CONFIG_HOME=str(work.parent / "cfg"))
    run = [sys.executable, str(ROOT / "run.pyw")]

    def scene(name, args, title, wait=1.2, before=None, **extra):
        print(name)
        p = subprocess.Popen(run + args, env={**env, **extra}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            win = _window(title)
            if not win:
                print("  pencere bulunamadı")
                return
            time.sleep(wait)
            if before:
                before(win)
            _shot(win, outdir / f"{name}{suffix}.png")
        finally:
            p.kill()
            p.wait()

    pdfs = [str(s["Rapor.pdf"]), str(s["Makale.pdf"]), str(s["Ders notu.pdf"])]
    scene("order", ["merge", *pdfs], "birle")
    scene("organizer", ["organize", pdfs[0], pdfs[1]], "Sayfa d", wait=4, RIFFLE_ORDER="organize")
    scene("stamp", ["sign", pdfs[0]], " ekle ", wait=2.5)
    scene("form", ["form", str(s["form"])], "Form doldur")
    scene("settings", ["--settings"], "Ayarlar")

    def type_query(win):
        subprocess.run(["xdotool", "windowfocus", "--sync", win], capture_output=True)
        subprocess.run(["xdotool", "type", "--delay", "40", "proje"], capture_output=True)
        subprocess.run(["xdotool", "key", "Return"], capture_output=True)
        time.sleep(3)
    scene("search", ["search", str(work)], "Klas", before=type_query)
    scene("progress", ["pdf2word", pdfs[1]], "Riffle", wait=1.0)
    scene("done", ["compress", pdfs[0]], "Riffle", wait=4)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ROOT / "docs" / "screenshots"
    capture(out, dark="--dark" in sys.argv)
