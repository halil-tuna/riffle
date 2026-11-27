"""First-run window (welcome.py) and where main shows it: once, before the first job, never in the way."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from conftest import make_text_pdf

import riffle
from riffle import settings, welcome

try:
    import tkinter as tk
except ImportError:
    tk = None
needs_display = pytest.mark.skipif(tk is None or not os.environ.get("DISPLAY"), reason="tkinter / sanal ekran yok")


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    d = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    monkeypatch.setenv("APPDATA", str(d))
    monkeypatch.delenv("RIFFLE_NO_WELCOME", raising=False)
    return d


def _widgets(root):
    out, stack = [], [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        out.append(w)
    return out


def _find(root, cls, text):
    from tkinter import ttk
    return next(w for w in _widgets(root) if isinstance(w, getattr(ttk, cls)) and str(w.cget("text")).startswith(text))


# ---------------------------------------------------------------- the window

@needs_display
def test_choices_are_saved(cfg):
    def act(root):
        _find(root, "Checkbutton", "İşlem geçmişini kaydet").invoke()          # history off
        _find(root, "Radiobutton", "Masaüstüne").invoke()
        _find(root, "Button", "Başla").invoke()
    assert welcome.needed()
    got = welcome.show(on_ready=act)
    s = settings.load()
    assert got["history"] is False and s["history"] is False and s["output"] == "desktop"
    assert s["first_run_done"] is True and not welcome.needed()


@needs_display
def test_folder_must_exist(cfg, tmp_path, monkeypatch):
    from tkinter import messagebox
    errors = []
    monkeypatch.setattr(messagebox, "showerror", lambda *a, **k: errors.append(a))
    target = tmp_path / "Çıktılar"

    def act(root):
        _find(root, "Radiobutton", "Şu klasöre").invoke()
        entry = next(w for w in _widgets(root) if w.winfo_class() == "TEntry")
        entry.insert(0, str(target))
        _find(root, "Button", "Başla").invoke()                # folder missing: stays open
        assert errors and root.winfo_exists()
        target.mkdir()
        _find(root, "Button", "Başla").invoke()
    welcome.show(on_ready=act)
    s = settings.load()
    assert s["output"] == "folder" and s["output_dir"] == str(target) and s["first_run_done"]


@needs_display
def test_close_keeps_current_settings(cfg, tmp_path):
    """Someone updating from 1.1.x with a chosen folder: X must not reset it."""
    folder = tmp_path / "klasor"
    folder.mkdir()
    s = settings.load()
    s.update(output="folder", output_dir=str(folder))
    settings.save(s)

    def act(root):
        assert _find(root, "Radiobutton", "Şu klasöre").instate(["selected"])     # current choice preselected
        root.event_generate("<Escape>")
    welcome.show(on_ready=act)
    s = settings.load()
    assert s["output"] == "folder" and s["output_dir"] == str(folder) and s["history"] is True
    assert s["first_run_done"] is True


# ---------------------------------------------------------------- maybe_show (no window needed)

def test_shown_once(cfg, monkeypatch):
    from riffle import ui
    calls = []
    monkeypatch.setattr(ui, "_gui", lambda: True)
    monkeypatch.setattr(welcome, "show", lambda: (calls.append(1), welcome._done()))
    welcome.maybe_show()
    welcome.maybe_show()
    assert calls == [1]


def test_broken_window_keeps_defaults(cfg, monkeypatch):
    from riffle import ui
    monkeypatch.setattr(ui, "_gui", lambda: True)

    def boom():
        raise RuntimeError("pencere açılamadı")
    monkeypatch.setattr(welcome, "show", boom)
    welcome.maybe_show()                                         # does not raise
    s = settings.load()
    assert s["first_run_done"] is True and s["history"] is True and s["output"] == "beside"


def test_console_run_waits_for_a_window(cfg, monkeypatch):
    from riffle import ui
    monkeypatch.setattr(ui, "_gui", lambda: False)
    monkeypatch.setattr(welcome, "show", lambda: pytest.fail("no window without a display"))
    welcome.maybe_show()
    assert welcome.needed()


def test_env_switch(cfg, monkeypatch):
    monkeypatch.setenv("RIFFLE_NO_WELCOME", "1")
    assert not welcome.needed()


# ---------------------------------------------------------------- where main shows it

@pytest.fixture
def seen(monkeypatch):
    from riffle import ui
    calls = []
    monkeypatch.setattr(ui, "_gui", lambda: True)
    monkeypatch.setattr(welcome, "show", lambda: (calls.append("welcome"), welcome._done(output="desktop")))
    return calls


def _quiet_window(monkeypatch, log):
    from riffle import ui

    def run(title, job, cancel=None):
        log.append("job")
        log.append(job(lambda m: None))
    monkeypatch.setattr(ui, "run_with_window", run)


def test_main_shows_it_before_the_first_job(cfg, tmp_path, monkeypatch, seen):
    from riffle import main as main_mod
    monkeypatch.setattr(settings, "desktop", lambda: tmp_path)
    (tmp_path / "kaynak").mkdir()
    src = make_text_pdf(tmp_path / "kaynak" / "a.pdf", pages=1, table=False)
    _quiet_window(monkeypatch, seen)
    main_mod.main(["rotate", str(src)])
    assert seen[:2] == ["welcome", "job"]
    outs, errs = seen[2]
    assert outs[0].parent == tmp_path                            # the choice made in the window already applies
    main_mod.main(["rotate", str(src)])
    assert seen.count("welcome") == 1


def test_main_not_for_settings_or_again(cfg, tmp_path, monkeypatch, seen):
    from riffle import main as main_mod
    from riffle import history
    monkeypatch.setattr(settings, "run", lambda *a, **k: [])
    src = make_text_pdf(tmp_path / "a.pdf", pages=1, table=False)
    main_mod.main(["settings", str(src)])
    main_mod.main(["--settings"])
    assert seen == []
    jid = history.record("rotate", [src], [])
    _quiet_window(monkeypatch, seen)
    main_mod.main(["--again", str(jid)])
    assert "welcome" not in seen and "job" in seen


def test_app_shows_it(cfg, monkeypatch, seen):
    from riffle import main as main_mod
    opened = []
    monkeypatch.setattr(settings, "run", lambda *a, **k: opened.append(k))
    main_mod.main(["--app"])
    assert seen == ["welcome"] and opened == [{"tab": "history"}]


def test_main_works_without_welcome_module(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    monkeypatch.delattr(riffle, "welcome", raising=False)
    monkeypatch.setitem(sys.modules, "riffle.welcome", None)
    log = []
    _quiet_window(monkeypatch, log)
    src = make_text_pdf(tmp_path / "a.pdf", pages=1, table=False)
    assert main_mod.main(["rotate", str(src)]) == 0
    assert log[0] == "job" and log[1][0]
