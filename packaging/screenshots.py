"""Window screenshots for the README and visual checks (Linux, on a virtual display).

    xvfb-run -a -s "-screen 0 1600x1000x24" python packaging/screenshots.py docs/screenshots [--dark] [--en]

Requires: tkinter, xdotool, ImageMagick (import). Every window is opened by the real program,
found with xdotool and only that window is captured.
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


_EN_PARA = ("This section summarizes the aim, scope and method of the project. Measurements were repeated three "
            "times and results are given as mean and standard deviation. The project schedule has four phases: "
            "literature review, design, prototype and testing. ")

L = {
    "tr": dict(report="Rapor.pdf", article="Makale.pdf", notes="Ders notu.pdf", folder="Ödevler",
               form="Staj başvuru formu.pdf", form_title="STAJ BAŞVURU FORMU", para=_PARA, query="proje",
               fields=(("Ad Soyad", "text"), ("Öğrenci No", "text"), ("Bölüm", "combo"), ("Staj türü", "check"),
                       ("Açıklama", "text")), choices=["Mekatronik", "Makine", "Elektrik"],
               h=("Proje Raporu", "Konu", "Örnek proje", "Tarih", "1. Giriş", "2. İş planı",
                  ("Aşama", "Görev", "Sorumlu", "Bitiş"), ("Faz", "Görev", "Ekip"), "3. Yöntem", "4. Sonuçlar",
                  "5. Kaynaklar", "Hafta {}: Proje yönetimi")),
    "en": dict(report="Report.pdf", article="Article.pdf", notes="Lecture notes.pdf", folder="Assignments",
               form="Internship application.pdf", form_title="INTERNSHIP APPLICATION FORM", para=_EN_PARA,
               query="project",
               fields=(("Full name", "text"), ("Student ID", "text"), ("Department", "combo"),
                       ("Summer internship", "check"), ("Notes", "text")),
               choices=["Mechatronics", "Mechanical", "Electrical"],
               h=("Project Report", "Subject", "Sample project", "Date", "1. Introduction", "2. Work plan",
                  ("Phase", "Task", "Owner", "Due"), ("Phase", "Task", "Team"), "3. Method", "4. Results",
                  "5. References", "Week {}: Project management")),
}


def _sample_pdfs(work: Path, lang: str = "tr") -> dict:
    """Synthetic sample documents (README images are public: no real people or institutions)."""
    import pymupdf
    c = L[lang]
    h, para = c["h"], c["para"]
    out = {}
    rows = "".join(f"<tr><td>{h[7][0]} {i}</td><td>{h[7][1]} {i}.{k}</td><td>{h[7][2]} {k}</td>"
                   f"<td>{10 + i}.03.2026</td></tr>" for i in range(1, 4) for k in range(1, 4))
    out["Rapor.pdf"] = _html_pdf(work / c["report"], [
        f"<h1>{h[0]}</h1><p><b>{h[1]}:</b> {h[2]} · <b>{h[3]}:</b> 12.03.2026</p><h2>{h[4]}</h2>"
        f"<p>{para * 3}</p><h2>{h[5]}</h2><table><tr>" + "".join(f"<th>{x}</th>" for x in h[6]) +
        f"</tr>{rows}</table>",
        f"<h2>{h[8]}</h2><p>{para * 6}</p>", f"<h2>{h[9]}</h2><p>{para * 5}</p>",
        f"<h2>{h[10]}</h2><p>{para * 2}</p>"])
    out["Makale.pdf"] = _html_pdf(work / c["article"], [
        f"<h1>Adaptive gap control for a benchtop machine</h1><p><i>Example article</i></p><h2>Abstract</h2>"
        f"<p>{_EN * 4}</p><h2>1. Introduction</h2><p>{_EN * 8}</p>"] + [f"<h2>{i}. Section</h2><p>{_EN * 14}</p>"
                                                                        for i in range(2, 7)])
    out["Ders notu.pdf"] = _html_pdf(work / c["notes"], [
        f"<h1>{h[11].format(i)}</h1><p>{para * 7}</p>" for i in range(1, 9)])
    # fillable form
    form = work / c["form"]
    d = pymupdf.open()
    pg = d.new_page()
    from riffle.ocr import _font_file
    pg.insert_text((60, 70), c["form_title"], fontsize=16, fontfile=_font_file(), fontname="F0")
    y = 110
    for label, kind in c["fields"]:
        pg.insert_text((60, y + 14), label, fontsize=11, fontfile=_font_file(), fontname="F0")
        w = pymupdf.Widget()
        w.field_name = label
        w.field_label = label
        w.rect = pymupdf.Rect(180, y, 480, y + 20)
        if kind == "text":
            w.field_type = pymupdf.PDF_WIDGET_TYPE_TEXT
        elif kind == "combo":
            w.field_type = pymupdf.PDF_WIDGET_TYPE_COMBOBOX
            w.choice_values = c["choices"]
        else:
            w.field_type = pymupdf.PDF_WIDGET_TYPE_CHECKBOX
            w.rect = pymupdf.Rect(180, y, 196, y + 16)
        pg.add_widget(w)
        y += 40
    d.save(form)
    out["form"] = form
    return out


def _sample_history(work: Path, cfg: Path, s: dict, lang: str) -> None:
    """A made-up history (sample file names only) for the History tab screenshot."""
    from datetime import datetime, timedelta
    old = os.environ.get("XDG_CONFIG_HOME")
    os.environ["XDG_CONFIG_HOME"] = str(cfg)
    try:
        from riffle import history
        en = lang == "en"
        now = datetime.now().astimezone()

        def at(h, m, days=0):
            return (now.replace(hour=h, minute=m, second=0) - timedelta(days=days)).isoformat(timespec="seconds")

        def out(name):
            p = work / name
            if name.endswith(".pdf"):                     # a real PDF, so the icon view shows a preview
                import shutil
                shutil.copy(s["Rapor.pdf"], p)
            else:
                p.write_bytes(b"sample")
            return p
        rep, art, notes = s["Rapor.pdf"], s["Makale.pdf"], s["Ders notu.pdf"]
        history.record("compress", [art], [out(("Article" if en else "Makale") + " (1).pdf")],
                       notes=[("info", f"{art.name}: 2.4 MB → 0.9 MB (%62)")], started_at=at(9, 12, 2),
                       duration_ms=2100)
        history.record("ocr", [notes], [work / ("Lecture notes_ocr.pdf" if en else "Ders notu_ocr.pdf")],
                       started_at=at(21, 40, 1), duration_ms=48000)
        history.record("merge", [rep, art, notes], [out("Merged.pdf" if en else "Birlesik.pdf")],
                       options={"ordered": True}, started_at=at(13, 5), duration_ms=900)
        history.record("pdf2word", [rep], [out(("Report" if en else "Rapor") + ".docx")],
                       notes=[("info", f"{rep.name}: " + ("3 notes from the PDF were added as Word comments."
                                                         if en else "PDF'teki 3 not Word yorumu olarak eklendi."))],
                       started_at=at(14, 20), duration_ms=3400)
    finally:
        if old is None:
            os.environ.pop("XDG_CONFIG_HOME", None)
        else:
            os.environ["XDG_CONFIG_HOME"] = old


def _select_row(win: str) -> None:
    """Selects the first job so the details show (the first row under the day heading)."""
    subprocess.run(["xdotool", "windowfocus", "--sync", win], capture_output=True)
    subprocess.run(["xdotool", "mousemove", "--window", win, "400", "178", "click", "1"], capture_output=True)
    time.sleep(0.8)


def _set_view(cfg: Path, view: str) -> None:
    import json
    f = cfg / "riffle" / "ayarlar.json"
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    data["history_view"] = view
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data), encoding="utf-8")


def _select_card(win: str) -> None:
    """Selects the first card of the icon view."""
    subprocess.run(["xdotool", "windowfocus", "--sync", win], capture_output=True)
    subprocess.run(["xdotool", "mousemove", "--window", win, "110", "230", "click", "1"], capture_output=True)
    time.sleep(1.0)


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


def capture(outdir: Path, dark: bool, lang: str = "tr") -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="riffle_shots_")) / L[lang]["folder"]
    work.mkdir()
    s = _sample_pdfs(work, lang)
    en = lang == "en"
    suffix = "-dark" if dark else ""
    env = dict(os.environ, RIFFLE_AUTOCLOSE_MS="60000", RIFFLE_NO_MENU_REFRESH="1", RIFFLE_NO_WELCOME="1",
               RIFFLE_THEME="dark" if dark else "light", RIFFLE_LANG=lang,
               XDG_CONFIG_HOME=str(work.parent / "cfg"))
    _sample_history(work, Path(env["XDG_CONFIG_HOME"]), s, lang)
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
    scene("order", ["merge", *pdfs], "Merge" if en else "birle")
    scene("organizer", ["organize", pdfs[0], pdfs[1]], "Page org" if en else "Sayfa d", wait=4,
          RIFFLE_ORDER="organize")
    scene("stamp", ["sign", pdfs[0]], "Add sign" if en else " ekle ", wait=2.5)
    scene("form", ["form", str(s["form"])], "Fill in" if en else "Form doldur")
    scene("settings", ["--settings"], "^Riffle$")
    scene("history", ["--app"], "^Riffle$", wait=1.5, before=_select_row)
    _set_view(Path(env["XDG_CONFIG_HOME"]), "icons")
    scene("history-icons", ["--app"], "^Riffle$", wait=2.5, before=_select_card)
    _set_view(Path(env["XDG_CONFIG_HOME"]), "list")
    fresh = work.parent / "cfg-yeni"                       # a first start: the welcome window
    scene("welcome", ["--app"], "^Riffle$", XDG_CONFIG_HOME=str(fresh), RIFFLE_NO_WELCOME="")

    def type_query(win):
        subprocess.run(["xdotool", "windowfocus", "--sync", win], capture_output=True)
        subprocess.run(["xdotool", "type", "--delay", "40", L[lang]["query"]], capture_output=True)
        subprocess.run(["xdotool", "key", "Return"], capture_output=True)
        time.sleep(3)
    scene("search", ["search", str(work)], "Search" if en else "Klas", before=type_query)
    scene("progress", ["pdf2word", pdfs[1]], "Riffle", wait=1.0)
    scene("done", ["compress", pdfs[0]], "Riffle", wait=4)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 and not sys.argv[1].startswith("-") else ROOT / "docs" / "screenshots"
    capture(out, dark="--dark" in sys.argv, lang="en" if "--en" in sys.argv else "tr")
