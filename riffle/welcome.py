"""First-run window: asked once, before the first right-click job (or when the Riffle window first opens).

  [x] Keep a job history   (ticked)
  Where should the output files go?   (the same choices as in Settings)

Also shown once to people updating from an older version (their settings have no "first_run_done"); their
current output setting is preselected. Closing it with X keeps the current settings (for a new user: the
defaults — history on, next to the source). Guarded: if the window cannot be shown, the job goes on.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import APP_NAME
from .i18n import T


def needed() -> bool:
    if os.environ.get("RIFFLE_NO_WELCOME"):          # automated tests, silent runs
        return False
    try:
        from . import settings
        return not settings.load().get("first_run_done")
    except Exception:
        return False


def _done(**changes) -> None:
    from . import settings
    s = settings.load()
    s.update(changes)
    s["first_run_done"] = True
    settings.save(s)


def maybe_show() -> None:
    """Shows the window if it was never shown. Never raises."""
    try:
        if not needed():
            return
        from . import ui
        if not ui._gui():
            return                                   # console run: ask next time there is a window
        show()
    except Exception:
        try:
            _done()                                  # the window itself is broken: keep the defaults, don't retry
        except Exception:
            pass


def show(on_ready=None) -> dict:
    """The window (blocks until closed). Returns what was saved. on_ready(root): for tests."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from . import settings, theme

    s = settings.load()
    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    pal = theme.setup(root)
    frame = ttk.Frame(root, padding=(24, 20, 24, 18))
    frame.pack(fill="both", expand=True)

    head = ttk.Frame(frame)
    head.pack(fill="x")
    try:
        logo = tk.PhotoImage(master=root, file=str(theme.ICON_PNG)).subsample(2, 2)
        ttk.Label(head, image=logo).pack(side="left", padx=(0, 12))
        root._logo = logo
    except Exception:
        pass
    ttk.Label(head, text=T("Riffle'a hoş geldin"), style="Heading.TLabel").pack(side="left", anchor="w")

    hist = tk.BooleanVar(value=bool(s.get("history", True)))
    ttk.Checkbutton(frame, text=T("İşlem geçmişini kaydet"), variable=hist).pack(anchor="w", pady=(18, 2))
    ttk.Label(frame, text=T("Ne zaman neyi dönüştürdüğünü Riffle penceresinde görürsün. Yalnızca bu bilgisayarda "
                            "tutulur, dosyaların kopyası alınmaz, {0} gün sonra silinir. Sonradan Ayarlar'dan "
                            "değiştirilebilir.", s.get("history_days", 90)),
              style="Muted.TLabel", wraplength=470, justify="left").pack(anchor="w", padx=(28, 0))

    ttk.Label(frame, text=T("Çıkan dosyalar nereye kaydedilsin?"), style="Strong.TLabel").pack(anchor="w",
                                                                                              pady=(18, 4))
    out = tk.StringVar(value=s.get("output", "beside"))
    out_dir = tk.StringVar(value=s.get("output_dir", ""))
    for val, text in (("beside", T("Kaynak dosyanın yanına")), ("desktop", T("Masaüstüne ({0})", settings.desktop()))):
        ttk.Radiobutton(frame, text=text, value=val, variable=out).pack(anchor="w", pady=2)
    fr = ttk.Frame(frame)
    fr.pack(anchor="w", fill="x", pady=2)
    ttk.Radiobutton(fr, text=T("Şu klasöre:"), value="folder", variable=out).pack(side="left")
    ttk.Entry(fr, textvariable=out_dir, width=30).pack(side="left", padx=4, fill="x", expand=True)

    def pick():
        d = filedialog.askdirectory(parent=root)
        if d:
            out_dir.set(d)
            out.set("folder")
    ttk.Button(fr, text=T("Seç…"), command=pick).pack(side="left")
    ttk.Radiobutton(frame, text=T("Her seferinde sor"), value="ask", variable=out).pack(anchor="w", pady=2)

    result: dict = {}

    def start(*_):
        if out.get() == "folder" and not Path(out_dir.get()).is_dir():
            messagebox.showerror(APP_NAME, T("Seçilen çıktı klasörü bulunamadı."), parent=root)
            return
        result.update(history=hist.get(), output=out.get(), output_dir=out_dir.get())
        _done(**result)
        root.destroy()

    def close():                                     # X: keep the current settings, don't ask again
        _done()
        root.destroy()

    btns = ttk.Frame(frame)
    btns.pack(fill="x", pady=(20, 0))
    ttk.Button(btns, text=T("Başla"), style="Accent.TButton", width=12, command=start).pack(side="right")
    root.bind("<Return>", start)
    root.bind("<Escape>", lambda e: close())
    root.protocol("WM_DELETE_WINDOW", close)
    theme.center(root)
    root.attributes("-topmost", True)
    root.after(600, lambda: root.attributes("-topmost", False))
    root.focus_force()
    if on_ready:
        root.after(200, lambda: on_ready(root))
    autoclose = os.environ.get("RIFFLE_AUTOCLOSE_MS")
    if autoclose:
        root.after(int(autoclose), close)
    root.mainloop()
    return result
