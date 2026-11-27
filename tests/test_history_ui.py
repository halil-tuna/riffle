"""The Riffle window's History tab, the Privacy settings and the single window (--app)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import ROOT

import riffle
from riffle import history, history_ui, settings

RUN = ROOT / "run.pyw"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    monkeypatch.setenv("APPDATA", str(d))
    return d


def _set(**kw):
    s = settings.load()
    s.update(kw)
    settings.save(s)


def _ago(days=0, hours=0):
    return (datetime.now().astimezone() - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


def _fill(folder: Path) -> dict:
    """Made-up records (the tests never use real file names)."""
    folder.mkdir(parents=True, exist_ok=True)

    def f(name):
        p = folder / name
        p.write_bytes(b"x")
        return p
    ids = {
        "word": history.record("pdf2word", [f("rapor.pdf")], [f("rapor (1).docx")],
                               notes=[("info", "rapor.pdf: 3 not Word yorumu olarak eklendi.")], duration_ms=3200),
        "merge": history.record("merge", [f(f"b{i}.pdf") for i in range(5)], [f("Birlesik.pdf")], duration_ms=800),
        "gone": history.record("ocr", [f("tarama.pdf")], [folder / "tarama_ocr.pdf"], started_at=_ago(days=1),
                               duration_ms=65000),
        "fail": history.record("compress", [f("sozlesme.pdf")], [], status="error",
                               notes=[("error", "sozlesme.pdf: şifreli")], started_at=_ago(days=3)),
    }
    return ids


# ---------------------------------------------------------------- helpers (no window)

def test_day_and_duration_texts():
    today = date(2026, 11, 27)
    assert history_ui.day_label(today, today) == "Bugün"
    assert history_ui.day_label(date(2026, 11, 26), today) == "Dün"
    assert history_ui.day_label(date(2026, 11, 24), today) == "24 Kasım 2026"
    assert history_ui.day_label(date(2027, 1, 2), today) == "2 Ocak 2027"
    assert [history_ui.duration_text(ms) for ms in (None, 400, 3200, 65000)] == ["", "<1 sn", "3 sn", "1 dk 5 sn"]


def test_files_text():
    there = {"/x/a.docx", "/x/b.pdf"}.__contains__
    assert history_ui.files_text({"inputs": ["/x/a.pdf"], "outputs": ["/x/a.docx"]}, there) == "a.pdf → a.docx"
    assert history_ui.files_text({"inputs": ["/x/1", "/x/2"], "outputs": ["/x/b.pdf"]}, there) == "2 dosya → b.pdf"
    assert history_ui.files_text({"inputs": ["/x/a.pdf"], "outputs": ["/x/c.pdf"]}, there) == \
        "a.pdf → (bulunamadı) c.pdf"
    assert history_ui.files_text({"inputs": ["/x/a.pdf"], "outputs": []}, there) == "a.pdf → —"
    assert history_ui.files_text({"inputs": ["/x/a"], "outputs": ["/x/1", "/x/2", "/x/3"]}, there) == \
        "a → 3 dosya"
    assert history_ui.first_output({"outputs": ["/x/c.pdf", "/x/b.pdf"]}, there) == "/x/b.pdf"


def test_action_labels():
    assert history_ui.action_label("pdf2word") == "PDF → Word"
    assert history_ui.action_label("compress") == "Sıkıştır (önerilen)"
    assert history_ui.action_label("extract") == "Sayfa seç ve çıkar"          # trailing … dropped
    assert history_ui.action_label("yok_boyle_bir_sey") == "yok_boyle_bir_sey"


# ---------------------------------------------------------------- the tab (real Tk on the virtual display)

try:
    import tkinter as tk
except ImportError:                                    # (the helpers above are still tested)
    tk = None
needs_display = pytest.mark.skipif(tk is None or not os.environ.get("DISPLAY"), reason="tkinter / sanal ekran yok")


@pytest.fixture
def tab(cfg, tmp_path):
    from tkinter import ttk
    from riffle import theme
    ids = _fill(tmp_path / "dosyalar")
    root = tk.Tk()
    root.geometry("880x700")
    pal = theme.setup(root)
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True)
    t = history_ui.build_tab(nb, root, pal)
    root.update()
    t.ids = ids
    yield t
    root.destroy()


def _rows(t):
    return [(t.tree.item(g, "values")[1], [t.tree.item(c, "values") for c in t.tree.get_children(g)])
            for g in t.tree.get_children()]


@needs_display
def test_tab_lists_by_day(tab):
    rows = _rows(tab)
    assert [g for g, _ in rows] == ["Bugün", "Dün", history_ui.day_label((date.today() - timedelta(days=3)))]
    today = rows[0][1]
    assert [r[1] for r in today] == ["PDF'leri birleştir", "PDF → Word"]
    assert today[0][2] == "5 dosya → Birlesik.pdf" and today[0][3] == "✓"
    assert rows[1][1][0][2] == "tarama.pdf → (bulunamadı) tarama_ocr.pdf"
    assert "missing" in tab.tree.item(f"j{tab.ids['gone']}", "tags")
    assert rows[2][1][0][3] == "✗" and rows[2][1][0][4] == ""       # no duration recorded
    assert not tab.empty.winfo_manager() and not tab.banner.winfo_manager()


@needs_display
def test_tab_details(tab):
    tab.select(tab.ids["word"])
    text = tab.detail.get("1.0", "end")
    assert "PDF → Word" in text and f"Riffle {riffle.__version__}" in text and "3 sn" in text
    assert "ℹ rapor.pdf: 3 not Word yorumu olarak eklendi." in text
    assert "rapor.pdf" in text and "rapor (1).docx" in text
    assert "link" in tab.detail.tag_names("4.10")                    # the output path is a link
    assert str(tab.b_open.state()) == "()"
    tab.select(tab.ids["gone"])
    assert "(bulunamadı)" in tab.detail.get("1.0", "end")
    assert "disabled" in tab.b_open.state()                         # nothing to open


@needs_display
def test_tab_heading_not_selectable(tab):
    tab.tree.selection_set("g0")
    tab.root.update()
    assert tab.selected() is None and "Bir işlem seç" in tab.detail.get("1.0", "end")


@needs_display
def test_tab_search_and_filters(tab):
    tab.search.set("birlesik")
    tab.refresh()
    assert [r[1] for _, rs in _rows(tab) for r in rs] == ["PDF'leri birleştir"]
    tab.search.set("")
    tab.status.set("Hata")
    tab.refresh()
    assert [r[1] for _, rs in _rows(tab) for r in rs] == ["Sıkıştır (önerilen)"]
    tab.status.set("Tümü")
    assert "Taranmış PDF'i aranabilir yap (OCR)" in tab.action_box.cget("values")
    tab.action.set("PDF → Word")
    tab.refresh()
    assert [r[1] for _, rs in _rows(tab) for r in rs] == ["PDF → Word"]
    tab.action.set("Tümü")
    tab.search.set("hiçbirşey")
    tab.refresh()
    assert tab.empty.winfo_manager() and tab.empty.cget("text") == "Eşleşen işlem yok."


@needs_display
def test_tab_search_typing_refreshes(tab):
    tab.search_entry.insert(0, "rapor")
    deadline = time.time() + 3
    while time.time() < deadline and len(tab.jobs) != 1:
        tab.root.update()
        time.sleep(0.05)
    assert list(tab.jobs) == [tab.ids["word"]]


@needs_display
def test_double_click_opens_output(tab, monkeypatch):
    opened = []
    monkeypatch.setattr(history_ui, "open_path", opened.append)
    iid = f"j{tab.ids['word']}"
    tab.tree.see(iid)
    tab.root.update()
    from types import SimpleNamespace
    x, y, w, h = tab.tree.bbox(iid)
    assert tab._double(SimpleNamespace(x=x + 200, y=y + h // 2)) == "break"   # (Tk can't fake a double click)
    assert opened == [tab.jobs[tab.ids["word"]]["outputs"][0]]
    gx, gy, gw, gh = tab.tree.bbox("g0")
    assert tab._double(SimpleNamespace(x=gx + 200, y=gy + gh // 2)) is None and len(opened) == 1   # day heading
    tab.tree.focus_force()
    tab.root.update()
    tab.tree.event_generate("<Return>")
    tab.root.update()
    assert len(opened) == 2


@needs_display
def test_open_missing_says_so(tab, monkeypatch):
    from tkinter import messagebox
    said, opened = [], []
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: said.append(a))
    monkeypatch.setattr(history_ui, "open_path", opened.append)
    tab.select(tab.ids["gone"])
    tab.open_selected()
    assert not opened and "bulunamadı" in said[0][1]


@needs_display
def test_show_in_folder(tab, monkeypatch):
    from riffle import ui
    shown = []
    monkeypatch.setattr(ui, "open_folder", shown.append)
    tab.select(tab.ids["gone"])                       # output gone: the source's place is shown
    tab.show_selected()
    assert shown == [Path(tab.jobs[tab.ids["gone"]]["inputs"][0])]


@needs_display
def test_remove_and_clear(tab, tmp_path):
    out = Path(tab.jobs[tab.ids["word"]]["outputs"][0])
    tab.select(tab.ids["word"])
    tab.tree.focus_force()
    tab.root.update()
    tab.tree.event_generate("<Delete>")
    tab.root.update()
    assert tab.ids["word"] not in tab.jobs and history.count() == 3
    assert out.exists()                                # the file itself stays
    assert tab.selected() is not None                  # the selection moved on
    tab.clear_all(confirm=lambda: False)
    assert history.count() == 3
    tab.clear_all(confirm=lambda: True)
    assert history.count() == 0 and not tab.tree.get_children()
    assert "Henüz bir işlem yok" in tab.empty.cget("text")


@needs_display
def test_refresh_picks_up_other_processes(tab):
    history.record("rotate", ["/x/yeni.pdf"], ["/x/yeni_dondurulmus_90.pdf"])
    tab.root.event_generate("<F5>")
    tab.root.update()
    assert any(j["action_id"] == "rotate" for j in tab.jobs.values())


@needs_display
def test_off_note(tab):
    _set(history=False)
    tab.refresh()
    assert tab.banner.winfo_manager() and "Geçmiş kapalı" in tab.banner.cget("text")
    assert tab.jobs                                    # old records stay visible (and can be cleared)
    _set(history=True)
    tab.refresh()
    assert not tab.banner.winfo_manager()


@needs_display
def test_unreadable_database_does_not_crash(tab, monkeypatch):
    def boom(**_):
        raise RuntimeError("okunamıyor")
    monkeypatch.setattr(history, "query", boom)
    tab.refresh()
    assert "Geçmiş okunamadı" in tab.banner.cget("text") and not tab.jobs


@needs_display
def test_dark_theme_builds(cfg, tmp_path, monkeypatch):
    from tkinter import ttk
    from riffle import theme
    monkeypatch.setenv("RIFFLE_THEME", "dark")
    _fill(tmp_path / "d")
    root = tk.Tk()
    try:
        pal = theme.setup(root)
        assert pal.dark
        t = history_ui.build_tab(ttk.Notebook(root), root, pal)
        assert t.detail.cget("background") == pal.surface and len(t.jobs) == 4
    finally:
        root.destroy()


# ---------------------------------------------------------------- the window (settings.run)

def _capture_tabs(monkeypatch):
    from tkinter import ttk
    added = []
    real = ttk.Notebook.add

    def add(self, child, **kw):
        added.append(kw.get("text"))
        return real(self, child, **kw)
    monkeypatch.setattr(ttk.Notebook, "add", add)
    return added


@needs_display
def test_window_tabs_and_footer(cfg, tmp_path, monkeypatch):
    from tkinter import ttk
    _fill(tmp_path / "d")
    added = _capture_tabs(monkeypatch)
    seen = {}
    real_tk = tk.Tk

    class Probe(real_tk):
        def mainloop(self, n=0):
            def look():
                nb = next(w for w in self.winfo_children() if isinstance(w, ttk.Notebook))
                seen["selected"] = nb.tab(nb.select(), "text")
                foot = [w for w in self.winfo_children() if isinstance(w, ttk.Frame)]
                seen["footer_on_history"] = any(f.winfo_ismapped() for f in foot)
                nb.select(1)
                self.update()
                seen["footer_on_general"] = any(f.winfo_ismapped() for f in foot)
                self.destroy()
            self.after(300, look)
            super().mainloop(n)
    monkeypatch.setattr(tk, "Tk", Probe)
    settings.run(tab="history")
    assert added == ["Geçmiş", "Genel", "Sağ tık menüsü"]
    assert seen == {"selected": "Geçmiş", "footer_on_history": False, "footer_on_general": True}


@needs_display
def test_window_without_history_module(cfg, monkeypatch):
    """Guarded add-on: history_ui cannot be imported → the settings window opens as before."""
    monkeypatch.delattr(riffle, "history_ui", raising=False)
    monkeypatch.setitem(sys.modules, "riffle.history_ui", None)
    monkeypatch.setenv("RIFFLE_AUTOCLOSE_MS", "600")
    added = _capture_tabs(monkeypatch)
    assert settings.run(tab="history") == []
    assert added == ["Genel", "Sağ tık menüsü"]


@needs_display
def test_privacy_settings_saved(cfg, monkeypatch):
    from tkinter import ttk
    real_tk = tk.Tk

    class Probe(real_tk):
        def mainloop(self, n=0):
            def act():
                widgets, stack = [], [self]
                while stack:
                    w = stack.pop()
                    stack.extend(w.winfo_children())
                    widgets.append(w)
                box = next(w for w in widgets if isinstance(w, ttk.Checkbutton)
                           and str(w.cget("text")).startswith("İşlem geçmişini kaydet"))
                box.invoke()                                  # off
                days = next(w for w in widgets if isinstance(w, ttk.Combobox) and "90 gün" in w.cget("values"))
                days.set("365 gün")
                self.event_generate("<Control-s>")
                self.update()
                self.destroy()
            self.after(300, act)
            super().mainloop(n)
    monkeypatch.setattr(tk, "Tk", Probe)
    settings.run(tab="general")
    s = settings.load()
    assert s["history"] is False and s["history_days"] == 365


# ---------------------------------------------------------------- one window only (real processes)

needs_xdotool = pytest.mark.skipif(tk is None or not os.environ.get("DISPLAY") or not shutil.which("xdotool"),
                                   reason="sanal ekran / xdotool yok")


def _windows(name):
    r = subprocess.run(["xdotool", "search", "--onlyvisible", "--name", name], capture_output=True, text=True)
    return r.stdout.split()


def _wait(cond, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.2)
    return False


@needs_xdotool
def test_single_window(cfg):
    env = dict(os.environ, XDG_CONFIG_HOME=str(cfg), APPDATA=str(cfg), RIFFLE_AUTOCLOSE_MS="30000")
    first = subprocess.Popen([sys.executable, str(RUN), "--app"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _wait(lambda: len(_windows("^Riffle$")) == 1)
        request = cfg / "riffle" / "pencere.istek"
        for flag in ("--app", "--settings"):
            t0 = time.time()
            second = subprocess.run([sys.executable, str(RUN), flag], env=env, capture_output=True, text=True,
                                    timeout=30)
            assert second.returncode == 0 and time.time() - t0 < 15, second.stderr
            assert _wait(lambda: not request.exists(), 5)             # the open window took the request
            assert len(_windows("^Riffle$")) == 1 and first.poll() is None
    finally:
        first.kill()
        first.wait()
    # the lock goes with the process: a new window opens
    again = subprocess.Popen([sys.executable, str(RUN), "--app"], env=dict(env, RIFFLE_AUTOCLOSE_MS="2500"),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert _wait(lambda: len(_windows("^Riffle$")) == 1)
    finally:
        again.wait(timeout=30)


# ---------------------------------------------------------------- stage 3: convert again, result window link

@needs_display
def test_again_button(tab, monkeypatch):
    from tkinter import messagebox
    launched, said = [], []
    monkeypatch.setattr(history_ui, "launch", lambda *a: launched.append(a))
    monkeypatch.setattr(messagebox, "showinfo", lambda *a, **k: said.append(a[1]))
    tab.select(tab.ids["word"])
    assert "disabled" not in tab.b_again.state()
    tab.again_selected()
    assert launched == [("--again", str(tab.ids["word"]))]
    Path(tab.jobs[tab.ids["merge"]]["inputs"][2]).unlink()        # one source gone
    tab.select(tab.ids["merge"])
    tab.again_selected()
    assert len(launched) == 1 and "b2.pdf" in said[0]


@needs_display
def test_again_disabled_for_unknown_action(tab):
    jid = history.record("eski_bir_islem", ["/x/a.pdf"], [])
    tab.refresh()
    tab.select(jid)
    assert "disabled" in tab.b_again.state()


def test_riffle_command():
    cmd = history_ui.riffle_command()
    assert cmd[0] == sys.executable and cmd[1].endswith("run.pyw") and Path(cmd[1]).exists()


@needs_display
def test_result_window_history_link(cfg, tmp_path, monkeypatch):
    from tkinter import ttk
    from riffle import ui
    launched, seen = [], {}
    monkeypatch.setattr(history_ui, "launch", lambda *a: launched.append(a))
    out = tmp_path / "cikti.pdf"
    out.write_bytes(b"%PDF")
    real_tk = tk.Tk

    class Probe(real_tk):
        def mainloop(self, n=0):
            def look():
                links = [w for w in _all(self) if isinstance(w, ttk.Label) and w.cget("text") == "Geçmiş"]
                if not links:
                    self.after(200, look)
                    return
                seen["link"] = True
                links[0].event_generate("<Button-1>")
            self.after(200, look)
            super().mainloop(n)
    monkeypatch.setattr(tk, "Tk", Probe)
    ui.run_with_window("deneme", lambda progress: ([out], []))
    assert seen == {"link": True} and launched == [("--app",)]


@needs_display
def test_result_window_no_link_when_off(cfg, tmp_path, monkeypatch):
    from tkinter import ttk
    from riffle import ui
    _set(history=False)
    seen = {}
    real_tk = tk.Tk

    class Probe(real_tk):
        def mainloop(self, n=0):
            def look():
                labels = [w.cget("text") for w in _all(self) if isinstance(w, ttk.Label)]
                if not any("Tamamlandı" in str(t) for t in labels):
                    self.after(200, look)
                    return
                seen["labels"] = labels
                self.destroy()
            self.after(200, look)
            super().mainloop(n)
    monkeypatch.setattr(tk, "Tk", Probe)
    out = tmp_path / "cikti.pdf"
    out.write_bytes(b"%PDF")
    ui.run_with_window("deneme", lambda progress: ([out], []))
    assert "Geçmiş" not in seen["labels"]


def _all(root):
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        out.append(w)
    return out
