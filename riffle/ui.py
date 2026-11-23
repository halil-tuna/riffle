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
    from tkinter import ttk, simpledialog
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


def ask_password(confirm: bool) -> str | None:
    if not HAS_TK:
        import getpass
        return getpass.getpass("Şifre: ")
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    pw = simpledialog.askstring(APP_NAME, "PDF şifresi:", show="•", parent=root)
    if pw and confirm:
        pw2 = simpledialog.askstring(APP_NAME, "Şifreyi tekrar gir:", show="•", parent=root)
        if pw != pw2:
            from tkinter import messagebox
            messagebox.showerror(APP_NAME, "Şifreler eşleşmedi.", parent=root)
            pw = None
    root.destroy()
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
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    ans = simpledialog.askstring(APP_NAME, question, initialvalue=default, parent=root)
    root.destroy()
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

    root = tk.Tk()
    root.title(f"{APP_NAME} — {title}")
    root.attributes("-topmost", True)
    root.after(400, lambda: root.attributes("-topmost", False))
    root.minsize(520, 320)
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    frm = ttk.Frame(root, padding=12)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text=hint, font=("Segoe UI", 10, "bold")).pack(anchor="w")
    ttk.Label(frm, text="Sürükleyerek ya da ↑ ↓ ile sırala. Ctrl+↑/↓ taşır, Del listeden çıkarır.",
              foreground="#5b6270").pack(anchor="w", pady=(2, 8))
    mid = ttk.Frame(frm)
    mid.pack(fill="both", expand=True)
    lb = tk.Listbox(mid, activestyle="none", font=("Segoe UI", 10), height=min(max(len(items), 4), 14),
                    selectbackground="#2f6fdd", selectforeground="white", exportselection=False)
    sb = ttk.Scrollbar(mid, orient="vertical", command=lb.yview)
    lb.configure(yscrollcommand=sb.set)
    lb.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")
    side = ttk.Frame(mid, padding=(8, 0, 0, 0))
    side.pack(side="left", fill="y")
    desc = {p: _describe(p) for p in items}

    def refresh(sel=None):
        lb.delete(0, "end")
        for i, p in enumerate(items, 1):
            lb.insert("end", f"{i:>2}.  {p.name}" + (f"   ({desc[p]})" if desc[p] else ""))
        if sel is not None and items:
            sel = max(0, min(sel, len(items) - 1))
            lb.selection_clear(0, "end")
            lb.selection_set(sel)
            lb.activate(sel)
            lb.see(sel)
        ok_btn.configure(state="normal" if len(items) >= 2 else "disabled")

    def cur():
        s = lb.curselection()
        return s[0] if s else None

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
        drag["i"] = lb.nearest(e.y)

    def motion(e):
        i, j = drag["i"], lb.nearest(e.y)
        if i is None or j == i or not 0 <= j < len(items):
            return
        items.insert(j, items.pop(i))
        drag["i"] = j
        refresh(j)

    lb.bind("<ButtonPress-1>", press)
    lb.bind("<B1-Motion>", motion)
    lb.bind("<ButtonRelease-1>", lambda e: drag.update(i=None))

    def b(text, cmd):
        ttk.Button(side, text=text, command=cmd, width=13).pack(fill="x", pady=1)

    b("↑ Yukarı", lambda: move(-1))
    b("↓ Aşağı", lambda: move(1))
    ttk.Separator(side).pack(fill="x", pady=5)
    b("A → Z", lambda: by_name())
    b("Z → A", lambda: by_name(True))
    b("Eskiden yeniye", lambda: sort(lambda p: p.stat().st_mtime))
    b("Ters çevir", lambda: (items.reverse(), refresh(0)))
    ttk.Separator(side).pack(fill="x", pady=5)
    b("Listeden çıkar", remove)

    foot = ttk.Frame(frm, padding=(0, 10, 0, 0))
    foot.pack(fill="x")
    ttk.Button(foot, text="Sayfa sayfa düzenle…", command=lambda: done(("organize", list(items)))).pack(side="left")
    ok_btn = ttk.Button(foot, text=ok_text, command=lambda: done(("ok", list(items))))
    ok_btn.pack(side="right")
    ttk.Button(foot, text="İptal", command=lambda: done(None)).pack(side="right", padx=6)

    def key(seq, fn):
        root.bind(seq, lambda e: (fn(), "break")[1])

    # listenin kendi bağı: Listbox sınıfının ↑/↓ seçimi (Ctrl'li hâli de) önce çalışıp seçimi kaydırmasın
    for seq, fn in (("<Control-Up>", lambda: move(-1)), ("<Control-Down>", lambda: move(1)),
                    ("<Delete>", remove)):
        lb.bind(seq, lambda e, fn=fn: (fn(), "break")[1])
        key(seq, fn)
    key("<Return>", lambda: done(("ok", list(items))) if len(items) >= 2 else None)
    key("<Escape>", lambda: done(None))
    root.protocol("WM_DELETE_WINDOW", lambda: done(None))
    refresh(0)
    lb.focus_set()
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
    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass

    frame = ttk.Frame(root, padding=16)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=title, font=("Segoe UI", 11, "bold")).pack(anchor="w")
    status = ttk.Label(frame, text="Başlıyor…", width=58, wraplength=420, justify="left")
    status.pack(anchor="w", pady=(8, 8))
    bar = ttk.Progressbar(frame, mode="indeterminate", length=420)
    bar.pack(fill="x")
    bar.start(12)
    buttons = ttk.Frame(frame)
    buttons.pack(fill="x", pady=(12, 0))
    cancel_btn = None
    running = {"on": True}

    def do_cancel():
        if cancel is not None and running["on"]:
            cancel.set()
            status.configure(text="İptal ediliyor… (bitmiş dosyalar kalır)")
            if cancel_btn is not None:
                cancel_btn.configure(state="disabled")

    if cancel is not None:
        cancel_btn = ttk.Button(buttons, text="İptal", command=do_cancel)
        cancel_btn.pack(side="right")
        root.bind("<Escape>", lambda e: do_cancel())

    def on_close():
        if running["on"] and cancel is not None:
            do_cancel()                    # çalışırken pencereyi kapatmak = iptal
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)

    # ekranın ortasına
    root.update_idletasks()
    w, h = root.winfo_width(), root.winfo_height()
    root.geometry(f"+{(root.winfo_screenwidth() - w) // 2}+{(root.winfo_screenheight() - h) // 3}")

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
        lines = []
        if outputs:
            lines.append("✔ Tamamlandı:\n" + "\n".join(f"   {p.name}" for p in outputs[:8]))
            if len(outputs) > 8:
                lines.append(f"   … ve {len(outputs) - 8} tane daha")
        if notes:
            lines.append("\n".join(notes))
        if errors:
            lines.append("✖ Hatalar:\n" + "\n".join(f"   {e}" for e in errors))
        status.configure(text="\n".join(lines) or "Hiçbir şey yapılmadı.")
        if outputs:
            ttk.Button(buttons, text="Klasörü aç",
                       command=lambda: (open_folder(outputs[0]), root.destroy())).pack(side="left")
        ttk.Button(buttons, text="Kapat", command=root.destroy).pack(side="right")
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
