"""Küçük ilerleme penceresi ve şifre sorma.

tkinter yoksa (ör. Mint'te python3-tk kurulmamışsa) sistem bildirimi
ve konsola düşer, program yine çalışır.
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

try:
    import tkinter as tk
    from tkinter import ttk
    HAS_TK = True
except ImportError:
    HAS_TK = False


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
    """tkinter olmadan bildirim."""
    print(f"{title}: {body}")
    if sys.platform.startswith("linux"):
        try:
            subprocess.Popen(["notify-send", "-a", APP_NAME, title, body])
        except OSError:
            pass


def _dialog(question: str, default: str = "", secret: bool = False, parent=None) -> str | None:
    """Temalı tek satırlık soru penceresi. Tamam: metin · İptal/Esc: None.
    parent verilirse açık bir pencerenin üstünde (Toplevel) açılır."""
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

    ttk.Button(foot, text="Tamam", style="Accent.TButton", command=ok, width=10).pack(side="right")
    ttk.Button(foot, text="İptal", command=root.destroy, width=10).pack(side="right", padx=(0, 8))
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
    if not HAS_TK:
        import getpass
        return getpass.getpass("Şifre: ")
    pw = _dialog("PDF şifresi:", secret=True)
    if pw and confirm:
        pw2 = _dialog("Şifreyi tekrar gir:", secret=True)
        if pw != pw2:
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(APP_NAME, "Şifreler eşleşmedi.", parent=root)
            root.destroy()
            pw = None
    return pw or None


def ask_folder() -> str | None:
    """Çıktı klasörü sor (ayar: her seferinde sor)."""
    if not HAS_TK or os.environ.get("RIFFLE_ORDER") == "name":
        return None
    from tkinter import filedialog
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    d = filedialog.askdirectory(parent=root, title=f"{APP_NAME} — Çıktı nereye kaydedilsin?")
    root.destroy()
    return d or None


def ask_text(question: str, default: str = "") -> str | None:
    if not HAS_TK:
        try:
            return input(f"{question} [{default}] ") or default
        except EOFError:
            return default or None
    ans = _dialog(question, default)
    if ans is None:
        return None                       # İptal
    return ans if ans.strip() or default.isspace() else None   # boş cevap: yalnızca isteğe bağlı sorularda geçerli


def _describe(p: Path) -> str:
    """Listede dosyanın yanında görünen kısa bilgi: sayfa sayısı / boyut."""
    from .actions import human_size
    info = human_size(p.stat().st_size) if p.exists() else ""
    if p.suffix.lower() == ".pdf":
        try:
            from .actions import pymupdf
            with pymupdf.open(p) as d:
                info = f"{len(d)} sayfa · {info}" if not d.needs_pass else f"şifreli · {info}"
        except Exception:
            pass
    return info


def ask_order(paths: list[Path], title: str, hint: str, ok_text: str = "Tamam"):
    """Dosya sırasını seçtiren küçük pencere (sayfa açmaz, anında gelir).

    Döndürür: ("ok", sıralı liste) · ("organize", sıralı liste) = sayfa düzenleyicide aç · İptal: None

    Sürükle-bırak · ↑/↓ düğmeleri · Ctrl+↑/↓ taşı · Del çıkar · A→Z / Z→A / tarihe göre · Enter tamam.
    """
    preset = os.environ.get("RIFFLE_ORDER")          # otomatik testler: "name" = Tamam, "organize" = düzenleyici
    if preset == "organize":
        return ("organize", list(paths))
    if not HAS_TK or preset == "name":
        return ("ok", list(paths))       # pencere yok: ad sırası
    items = list(paths)
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
    ttk.Label(frm, text="Sürükleyerek ya da düğmelerle sırala · Ctrl+↑/↓ taşır · Del listeden çıkarır",
              style="Muted.TLabel").pack(anchor="w", pady=(2, 10))
    mid = ttk.Frame(frm)
    mid.pack(fill="both", expand=True)
    style = ttk.Style(root)
    style.configure("Order.Treeview", rowheight=34)
    tree = ttk.Treeview(mid, columns=("no", "name", "info"), show="headings", selectmode="browse",
                        style="Order.Treeview", height=min(max(len(items), 4), 12))
    for col, text, w, anchor, stretch in (("no", "#", 44, "center", False), ("name", "Dosya", 280, "w", True),
                                          ("info", "Bilgi", 160, "w", False)):
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

    # sürükle-bırak
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

    b("↑  Yukarı", lambda: move(-1))
    b("↓  Aşağı", lambda: move(1))
    ttk.Separator(side).pack(fill="x", pady=8)
    b("A → Z", lambda: by_name())
    b("Z → A", lambda: by_name(True))
    b("Eskiden yeniye", lambda: sort(lambda p: p.stat().st_mtime))
    b("Ters çevir", lambda: (items.reverse(), refresh(0)))
    ttk.Separator(side).pack(fill="x", pady=8)
    b("Listeden çıkar", remove)

    foot = ttk.Frame(frm, padding=(0, 14, 0, 0))
    foot.pack(fill="x")
    ttk.Button(foot, text="Sayfa sayfa düzenle…", command=lambda: done(("organize", list(items)))).pack(side="left")
    ok_btn = ttk.Button(foot, text=ok_text, style="Accent.TButton", width=12,
                        command=lambda: done(("ok", list(items))))
    ok_btn.pack(side="right")
    ttk.Button(foot, text="İptal", width=10, command=lambda: done(None)).pack(side="right", padx=8)

    def key(seq, fn):
        root.bind(seq, lambda e: (fn(), "break")[1])

    # listenin kendi bağı: Treeview sınıfının ↑/↓ seçimi (Ctrl'li hâli de) önce çalışıp seçimi kaydırmasın
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
    """job(progress) -> (çıktılar, hatalar). Pencerede ilerleme gösterir; cancel verilirse İptal düğmesi."""
    if not HAS_TK:
        outputs, errors = job(lambda m: print("·", m))
        if errors:
            notify("Hata", "\n".join(errors))
        if outputs:
            notify("Tamamlandı", ", ".join(p.name for p in outputs))
        return

    q: queue.Queue = queue.Queue()
    notes: list[str] = []  # "ℹ " ile başlayan mesajlar sonuç ekranında da kalır
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
    headline = ttk.Label(titles, text="Çalışıyor…", style="Muted.TLabel")
    headline.pack(anchor="w")
    ttk.Label(titles, text=title, style="Strong.TLabel", wraplength=400, justify="left").pack(anchor="w")
    status = ttk.Label(frame, text="Başlıyor…", style="Muted.TLabel", width=60, wraplength=460, justify="left")
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
            status.configure(text="İptal ediliyor… (bitmiş dosyalar kalır)")
            if cancel_btn is not None:
                cancel_btn.configure(state="disabled")

    if cancel is not None:
        cancel_btn = ttk.Button(buttons, text="İptal", command=do_cancel, width=10)
        cancel_btn.pack(side="right")
        root.bind("<Escape>", lambda e: do_cancel())

    def on_close():
        if running["on"] and cancel is not None:
            do_cancel()                    # çalışırken pencereyi kapatmak = iptal
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    theme.center(root)

    def worker():
        try:
            result = job(lambda m: q.put(("msg", m)))
        except Exception as e:  # beklenmeyen hata
            result = ([], [f"Beklenmeyen hata: {e}"])
        q.put(("done", result))

    def finish(outputs: list[Path], errors: list[str]):
        # konsola da yaz (komut satırından çalıştırma ve kayıt için)
        if outputs:
            print("Tamamlandı:", ", ".join(p.name for p in outputs))
        for e in errors:
            print("Hata:", e)
        sys.stdout.flush()
        running["on"] = False
        if cancel_btn is not None:
            cancel_btn.destroy()
        root.unbind("<Escape>")
        root.bind("<Escape>", lambda e: root.destroy())
        bar.stop()
        bar.pack_forget()
        status.pack_forget()
        cancelled = any("İptal edildi" in e for e in errors)
        if errors and not outputs:
            headline.configure(text="✕  İptal edildi" if cancelled else "✕  Olmadı", style="Danger.TLabel")
        elif errors:
            headline.configure(text="!  Kısmen tamamlandı", style="Danger.TLabel")
        elif outputs:
            headline.configure(text="✓  Tamamlandı", style="Success.TLabel")
        else:
            headline.configure(text="Hiçbir şey yapılmadı", style="Muted.TLabel")
        result_box.pack(fill="x", pady=(14, 0))
        for p in outputs[:8]:
            ttk.Label(result_box, text=f"•  {p.name}", wraplength=460).pack(anchor="w", pady=1)
        if len(outputs) > 8:
            ttk.Label(result_box, text=f"… ve {len(outputs) - 8} dosya daha", style="Muted.TLabel").pack(anchor="w")
        if notes:
            ttk.Label(result_box, text="\n".join(notes), style="Muted.TLabel", wraplength=460,
                      justify="left").pack(anchor="w", pady=(8, 0))
        for e in errors:
            ttk.Label(result_box, text=e, foreground=pal.danger, wraplength=460,
                      justify="left").pack(anchor="w", pady=(6, 0))
        if outputs:
            ttk.Button(buttons, text="Klasörü aç", style="Accent.TButton",
                       command=lambda: (open_folder(outputs[0]), root.destroy())).pack(side="right")
            ttk.Button(buttons, text="Kapat", command=root.destroy, width=10).pack(side="right", padx=(0, 8))
        else:
            ttk.Button(buttons, text="Kapat", command=root.destroy, width=10).pack(side="right")
        root.attributes("-topmost", False)
        if outputs and not errors:
            root.after(15000, root.destroy)  # başarıdaysa 15 sn sonra kendiliğinden kapan
        auto = os.environ.get("RIFFLE_AUTOCLOSE_MS")      # testler için
        if auto:
            root.after(int(auto), root.destroy)

    def poll():
        try:
            while True:
                kind, payload = q.get_nowait()
                if kind == "msg":
                    if payload.startswith("ℹ "):
                        notes.append(payload[2:])
                    status.configure(text=payload)
                else:
                    finish(*payload)
                    return
        except queue.Empty:
            pass
        root.after(80, poll)

    threading.Thread(target=worker, daemon=True).start()
    root.after(80, poll)
    root.mainloop()
