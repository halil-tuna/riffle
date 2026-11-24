"""Small progress window and password prompt.

Without tkinter (e.g. python3-tk not installed on Mint) it falls back to system
notifications and the console; the program still works.
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from . import APP_NAME
from .i18n import T

try:
    import tkinter as tk
    from tkinter import ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False


def _gui() -> bool:
    """Can a window be opened? On Linux without a display (SSH, server) fall back to the console."""
    if not HAS_TK:
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


def open_folder(path: Path) -> None:
    folder = path if path.is_dir() else path.parent
    try:
        if sys.platform == "win32":
            if path.exists() and not path.is_dir():
                subprocess.Popen(["explorer", "/select,", str(path)])
            else:
                os.startfile(folder)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError:
        pass


def notify(title: str, body: str) -> None:
    """Notification without tkinter."""
    print(f"{title}: {body}")
    if sys.platform.startswith("linux"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, title, body])
        except OSError:
            pass


def _dialog(question: str, default: str = "", secret: bool = False, parent=None) -> str | None:
    """Themed single-line question window. OK: text · Cancel/Esc: None.
    If parent is given it opens on top of an open window (Toplevel)."""
    from . import theme
    root = tk.Toplevel(parent) if parent is not None else tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    if parent is None:
        theme.setup(root)
    else:
        root.transient(parent)
        theme.window(root)
    root.attributes("-topmost", True)
    result = {"v": None}
    frm = ttk.Frame(root, padding=(22, 18, 22, 16))
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=question, style="Strong.TLabel", wraplength=400, justify="left").pack(anchor="w")
    var = tk.StringVar(master=root, value=default)
    ent = ttk.Entry(frm, textvariable=var, width=44, show="•" if secret else "")
    ent.pack(fill="x", pady=(12, 16))
    foot = ttk.Frame(frm)
    foot.pack(fill="x")

    def ok(_=None):
        result["v"] = var.get()
        root.destroy()

    ttk.Button(foot, text=T("Tamam"), style="Accent.TButton", command=ok, width=10).pack(side="right")
    ttk.Button(foot, text=T("İptal"), command=root.destroy, width=10).pack(side="right", padx=(0, 8))
    root.bind("<Return>", ok)
    root.bind("<KP_Enter>", ok)
    root.bind("<Escape>", lambda e: root.destroy())
    theme.center(root)
    ent.select_range(0, "end")
    ent.icursor("end")
    root.after(50, lambda: (ent.focus_force(), root.attributes("-topmost", False)))
    if parent is not None:
        root.grab_set()
        parent.wait_window(root)
    else:
        root.mainloop()
    return result["v"]


def ask_password(confirm: bool) -> str | None:
    if not _gui():
        import getpass
        return getpass.getpass(T("Şifre: "))
    pw = _dialog(T("PDF şifresi:"), secret=True)
    if pw and confirm:
        pw2 = _dialog(T("Şifreyi tekrar gir:"), secret=True)
        if pw != pw2:
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, T("Şifreler eşleşmedi."), parent=root)
            root.destroy()
            pw = None
    return pw or None


def ask_folder() -> str | None:
    """Ask for the output folder (setting: ask every time)."""
    if not _gui() or os.environ.get("RIFFLE_ORDER") == "name":
        return None
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    d = filedialog.askdirectory(parent=root, title=T("{0} — Çıktı nereye kaydedilsin?", APP_NAME))
    root.destroy()
    return d or None


def ask_text(question: str, default: str = "") -> str | None:
    if not _gui():
        try:
            return input(f"{question} [{default}] ") or default
        except EOFError:
            return default or None
    ans = _dialog(question, default)
    if ans is None:
        return None                       # Cancel
    return ans if ans.strip() or default.isspace() else None   # empty answer: valid only for optional questions


def _describe(p: Path) -> str:
    """Short info shown next to the file in the list: page count / size."""
    from .actions import human_size
    info = human_size(p.stat().st_size) if p.exists() else ""
    if p.suffix.lower() == ".pdf":
        try:
            from .actions import pymupdf
            with pymupdf.open(p) as d:
                info = T("{0} sayfa · {1}", len(d), info) if not d.needs_pass else T("şifreli · {0}", info)
        except Exception:
            pass
    return info


def ask_order(paths: list[Path], title: str, hint: str, ok_text: str | None = None, allow_pages: bool = True):
    """Small window to choose the file order (doesn't open pages, appears instantly).

    Returns: ("ok", ordered list) · ("organize", ordered list) = open in the page organizer · Cancel: None

    Drag and drop · ↑/↓ buttons · Ctrl+↑/↓ move · Del remove · A→Z / Z→A / by date · Enter OK.
    """
    preset = os.environ.get("RIFFLE_ORDER")          # automated tests: "name" = OK, "organize" = organizer
    if preset == "organize":
        return ("organize", list(paths))
    if not _gui() or preset == "name":
        return ("ok", list(paths))       # no window: name order
    items = list(paths)
    ok_text = ok_text or T("Tamam")
    result: dict = {"value": None}
    from . import theme

    root = tk.Tk()
    root.title(f"{APP_NAME} — {title}")
    pal = theme.setup(root)
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.minsize(600, 360)
    frm = ttk.Frame(root, padding=(18, 16, 18, 14))
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=hint, style="Strong.TLabel").pack(anchor="w")
    ttk.Label(frm, text=T("Sürükleyerek ya da düğmelerle sırala · Ctrl+↑/↓ taşır · Del listeden çıkarır"),
              style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
    mid = ttk.Frame(frm)
    mid.pack(fill="both", expand=True)
    style = ttk.Style(root)
    style.configure("Order.Treeview", rowheight=34)
    tree = ttk.Treeview(mid, columns=("no", "name", "info"), show="headings", selectmode="browse",
                        style="Order.Treeview", height=min(max(len(items), 4), 12))
    for col, text, w, anchor, stretch in (("no", "#", 44, "center", False), ("name", T("Dosya"), 280, "w", True),
                                          ("info", T("Bilgi"), 160, "w", False)):
        tree.heading(col, text=text, anchor=anchor)
        tree.column(col, width=w, anchor=anchor, stretch=stretch)
    tree.tag_configure("muted", foreground=pal.muted)
    sb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y", padx=(2, 0))
    side = ttk.Frame(mid, padding=(12, 0, 0, 0))
    side.pack(side="left", fill="y")
    desc = {p: _describe(p) for p in items}

    def refresh(sel=None):
        tree.delete(*tree.get_children())
        for i, p in enumerate(items):
            tree.insert("", "end", iid=str(i), values=(i + 1, p.name, desc[p]))
        if sel is not None and items:
            sel = str(max(0, min(sel, len(items) - 1)))
            tree.selection_set(sel)
            tree.focus(sel)
            tree.see(sel)
        ok_btn.configure(state="normal" if len(items) >= 2 else "disabled")

    def cur():
        s = tree.selection()
        return int(s[0]) if s else None

    def move(d):
        i = cur()
        if i is None or not 0 <= i + d < len(items):
            return
        items[i], items[i + d] = items[i + d], items[i]
        refresh(i + d)

    def remove():
        i = cur()
        if i is not None and len(items) > 2:
            del items[i]
            refresh(i)

    def sort(key, reverse=False):
        i = cur()
        keep = items[i] if i is not None else None
        items.sort(key=key, reverse=reverse)
        refresh(items.index(keep) if keep in items else 0)

    from .actions import sorted_by_name

    def by_name(rev=False):
        order = sorted_by_name(items)
        items[:] = order[::-1] if rev else order
        refresh(0)

    def done(value):
        result["value"] = value
        root.destroy()

    # drag and drop
    drag = {"i": None}

    def press(e):
        row = tree.identify_row(e.y)
        drag["i"] = int(row) if row else None

    def motion(e):
        row = tree.identify_row(e.y)
        i, j = drag["i"], (int(row) if row else None)
        if i is None or j is None or j == i:
            return
        items.insert(j, items.pop(i))
        drag["i"] = j
        refresh(j)

    tree.bind("<ButtonPress-1>", press, add="+")
    tree.bind("<B1-Motion>", motion)
    tree.bind("<ButtonRelease-1>", lambda e: drag.update(i=None))

    def b(text, cmd):
        ttk.Button(side, text=text, command=cmd, width=14).pack(fill="x", pady=2)

    b(T("↑  Yukarı"), lambda: move(-1))
    b(T("↓  Aşağı"), lambda: move(1))
    ttk.Separator(side).pack(fill="x", pady=8)
    b("A → Z", lambda: by_name())
    b("Z → A", lambda: by_name(True))
    b(T("Eskiden yeniye"), lambda: sort(lambda p: p.stat().st_mtime))
    b(T("Ters çevir"), lambda: (items.reverse(), refresh(0)))
    ttk.Separator(side).pack(fill="x", pady=8)
    b(T("Listeden çıkar"), remove)

    foot = ttk.Frame(frm, padding=(0, 14, 0, 0))
    foot.pack(fill="x")
    if allow_pages:
        ttk.Button(foot, text=T("Sayfa sayfa düzenle…"),
                   command=lambda: done(("organize", list(items)))).pack(side="left")
    ok_btn = ttk.Button(foot, text=ok_text, style="Accent.TButton", width=12,
                        command=lambda: done(("ok", list(items))))
    ok_btn.pack(side="right")
    ttk.Button(foot, text=T("İptal"), width=10, command=lambda: done(None)).pack(side="right", padx=8)

    def key(seq, fn):
        root.bind(seq, lambda e: (fn(), "break")[1])

    # the list's own binding: the Treeview class's ↑/↓ selection (with Ctrl too) must not run first and move the selection
    for seq, fn in (("<Control-Up>", lambda: move(-1)), ("<Control-Down>", lambda: move(1)),
                    ("<Delete>", remove)):
        tree.bind(seq, lambda e, fn=fn: (fn(), "break")[1])
        key(seq, fn)
    key("<Return>", lambda: done(("ok", list(items))) if len(items) >= 2 else None)
    key("<Escape>", lambda: done(None))
    root.protocol("WM_DELETE_WINDOW", lambda: done(None))
    refresh(0)
    theme.center(root)
    tree.focus_set()
    auto = os.environ.get("RIFFLE_AUTOCLOSE_MS")
    if auto:
        root.after(int(auto), lambda: done(None))
    root.mainloop()
    return result["value"]


def run_with_window(title: str, job: Callable[[Callable[[str], None]], tuple[list[Path], list[str]]],
                    cancel: threading.Event | None = None) -> None:
    """job(progress) -> (outputs, errors). Shows progress in a window; with cancel, a Cancel button."""
    clip_prefix = "\x00clip:"
    if not _gui():
        def cli_progress(m):
            if m.startswith(clip_prefix):
                print(m[len(clip_prefix):])         # instead of the clipboard: print the text
            else:
                print("·", m)
        outputs, errors = job(cli_progress)
        if errors:
            notify(T("Hata"), "\n".join(errors))
        if outputs:
            notify(T("Tamamlandı"), ", ".join(p.name for p in outputs))
        return

    q: queue.Queue = queue.Queue()
    clip: dict = {"text": None}          # text from an image etc.: to the clipboard when the job is done
    newer: dict = {"v": None}            # newer version on GitHub (checked in the background)
    notes: list[str] = []  # messages starting with "ℹ " also stay on the result screen
    from . import theme
    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    pal = theme.setup(root)
    root.attributes("-topmost", True)

    frame = ttk.Frame(root, padding=(22, 18, 22, 16))
    frame.pack(fill="both", expand=True)
    head = ttk.Frame(frame)
    head.pack(fill="x")
    try:
        logo = tk.PhotoImage(master=root, file=str(theme.ICON_PNG)).subsample(2, 2)
        ttk.Label(head, image=logo).pack(side="left", padx=(0, 12))
        root._logo = logo
    except Exception:
        pass
    titles = ttk.Frame(head)
    titles.pack(side="left", fill="x", expand=True)
    headline = ttk.Label(titles, text=T("Çalışıyor…"), style="Muted.TLabel")
    headline.pack(anchor="w")
    ttk.Label(titles, text=title, style="Strong.TLabel", wraplength=400, justify="left").pack(anchor="w")
    status = ttk.Label(frame, text=T("Başlıyor…"), style="Muted.TLabel", width=60, wraplength=460, justify="left")
    status.pack(anchor="w", pady=(14, 10))
    bar = ttk.Progressbar(frame, mode="indeterminate", length=460)
    bar.pack(fill="x")
    bar.start(12)
    result_box = ttk.Frame(frame)
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", side="bottom", pady=(16, 0))
    cancel_btn = None
    running = {"on": True}

    def do_cancel():
        if cancel is not None and running["on"]:
            cancel.set()
            status.configure(text=T("İptal ediliyor… (bitmiş dosyalar kalır)"))
            if cancel_btn is not None:
                cancel_btn.configure(state="disabled")

    if cancel is not None:
        cancel_btn = ttk.Button(buttons, text=T("İptal"), command=do_cancel, width=10)
        cancel_btn.pack(side="right")
        root.bind("<Escape>", lambda e: do_cancel())

    def on_close():
        if running["on"] and cancel is not None:
            do_cancel()                    # closing the window while running = cancel
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    theme.center(root)

    def worker():
        try:
            result = job(lambda m: q.put(("msg", m)))
        except Exception as e:  # unexpected error
            result = ([], [T("Beklenmeyen hata: {0}", e)])
        q.put(("done", result))

    def finish(outputs: list[Path], errors: list[str]):
        # also write to the console (for command-line runs and logging)
        if outputs:
            print(T("Tamamlandı:"), ", ".join(p.name for p in outputs))
        for e in errors:
            print(T("Hata:"), e)
        sys.stdout.flush()
        running["on"] = False
        if cancel_btn is not None:
            cancel_btn.destroy()
        root.unbind("<Escape>")
        root.bind("<Escape>", lambda e: root.destroy())
        bar.stop()
        bar.pack_forget()
        status.pack_forget()
        cancelled = any(T("İptal edildi") in e for e in errors)
        if errors and not outputs:
            headline.configure(text=T("✕  İptal edildi") if cancelled else T("✕  Olmadı"), style="Danger.TLabel")
        elif errors:
            headline.configure(text=T("!  Kısmen tamamlandı"), style="Danger.TLabel")
        elif outputs:
            headline.configure(text=T("✓  Tamamlandı"), style="Success.TLabel")
        else:
            headline.configure(text=T("Hiçbir şey yapılmadı"), style="Muted.TLabel")
        result_box.pack(fill="x", pady=(14, 0))
        for p in outputs[:8]:
            ttk.Label(result_box, text=f"•  {p.name}", wraplength=460).pack(anchor="w", pady=1)
        if len(outputs) > 8:
            ttk.Label(result_box, text=T("… ve {0} dosya daha", len(outputs) - 8), style="Muted.TLabel").pack(anchor="w")
        if clip["text"]:
            try:
                root.clipboard_clear()
                root.clipboard_append(clip["text"])
                root.update()
                notes.insert(0, T("Metin panoya kopyalandı: istediğin yere Ctrl+V ile yapıştır."))
            except tk.TclError:
                pass
        if notes:
            ttk.Label(result_box, text="\n".join(notes), style="Muted.TLabel", wraplength=460,
                      justify="left").pack(anchor="w", pady=(8, 0))
        for e in errors:
            ttk.Label(result_box, text=e, foreground=pal.danger, wraplength=460,
                      justify="left").pack(anchor="w", pady=(6, 0))
        if newer["v"]:
            from . import update
            link = ttk.Label(result_box, text=T("↑  Yeni sürüm var: Riffle {0} — indirmek için tıkla", newer['v']),
                             foreground=pal.accent, cursor="hand2")
            link.pack(anchor="w", pady=(10, 0))
            link.bind("<Button-1>", lambda e: update.open_download())
        if outputs:
            ttk.Button(buttons, text=T("Klasörü aç"), style="Accent.TButton",
                       command=lambda: (open_folder(outputs[0]), root.destroy())).pack(side="right")
            ttk.Button(buttons, text=T("Kapat"), command=root.destroy, width=10).pack(side="right", padx=(0, 8))
        else:
            ttk.Button(buttons, text=T("Kapat"), command=root.destroy, width=10).pack(side="right")
        root.attributes("-topmost", False)
        if outputs and not errors:
            root.after(15000, root.destroy)  # close by itself after 15 s on success
        auto = os.environ.get("RIFFLE_AUTOCLOSE_MS")      # for tests
        if auto:
            root.after(int(auto), root.destroy)

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "msg":
                    if payload.startswith(clip_prefix):
                        clip["text"] = payload[len(clip_prefix):]
                        continue
                    if payload.startswith("ℹ "):
                        notes.append(payload[2:])
                    status.configure(text=payload)
                else:
                    finish(*payload)
                    return
        except queue.Empty:
            pass
        root.after(80, poll)

    def look_for_update():
        try:
            from . import update
            newer["v"] = update.check()
        except Exception:
            pass

    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=look_for_update, daemon=True).start()
    root.after(80, poll)
    root.mainloop()
