"""Settings: tools shown in the menu, favourites, output folder, parallel processing.

ayarlar.json and kullanim.json live in the user settings folder (Windows: %APPDATA%\\Riffle,
Linux: ~/.config/riffle). If a file is broken or missing, the defaults are used.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import APP_NAME
from .i18n import T

DEFAULTS = {
    "hidden": [],             # action ids not shown in the menu
    "pinned": [],             # always at the top of the menu
    "auto_favorites": True,   # the 3 most used actions move to the top by themselves
    "output": "beside",       # beside | desktop | folder | ask
    "output_dir": "",
    "parallel": True,         # use the whole CPU for many files
    "theme": "system",        # system | light | dark
    "check_updates": True,    # weekly check for a new release on GitHub
    "language": "auto",       # auto (system) | tr | en
    "history": True,          # job history (only on this computer; see history.py)
    "history_days": 90,       # history records older than this are deleted
    "first_run_done": False,  # the welcome window was shown once
}
FAV_AUTO = 3
FAV_MIN_USES = 3
NEVER_HIDE = {"settings"}


def _dir() -> Path:
    from .stamp_model import config_dir
    return config_dir()


def _read(name: str) -> dict:
    try:
        return json.loads((_dir() / name).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write(name: str, data: dict) -> None:
    p = _dir() / name
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def load() -> dict:
    s = dict(DEFAULTS)
    s.update({k: v for k, v in _read("ayarlar.json").items() if k in DEFAULTS})
    return s


def save(s: dict) -> None:
    _write("ayarlar.json", {k: s.get(k, v) for k, v in DEFAULTS.items()})


def usage() -> dict:
    return {k: int(v) for k, v in _read("kullanim.json").items() if isinstance(v, (int, float))}


def favorites(s: dict | None = None, use: dict | None = None) -> list[str]:
    """Actions that go to the top of the menu: pinned ones + (if automatic) the most used."""
    from .actions import BY_ID
    s = s or load()
    use = usage() if use is None else use
    favs = [a for a in s["pinned"] if a in BY_ID and a not in s["hidden"]]
    if s["auto_favorites"]:
        ranked = sorted((k for k, n in use.items() if n >= FAV_MIN_USES and k in BY_ID
                         and k not in s["hidden"] and k not in favs and k not in NEVER_HIDE),
                        key=lambda k: -use[k])
        favs += ranked[:FAV_AUTO]
    return favs


def record_use(action_id: str) -> bool:
    """Increments the usage counter. True if the favourites list changed (the menu should be refreshed)."""
    try:
        s = load()
        before = favorites(s)
        use = usage()
        use[action_id] = use.get(action_id, 0) + 1
        _write("kullanim.json", use)
        return favorites(s, use) != before
    except Exception:
        return False


def refresh_menu() -> None:
    """Rebuilds the menu according to the settings (silently)."""
    if os.environ.get("RIFFLE_NO_MENU_REFRESH"):       # automated tests
        return
    import io
    from contextlib import redirect_stdout
    try:
        import install
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        import install
    with redirect_stdout(io.StringIO()):
        install.install_windows() if install.IS_WIN else install.install_linux()


# ------------------------------------------------------------------ output folder

def desktop() -> Path:
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            buf = ctypes.create_unicode_buffer(wintypes.MAX_PATH)
            ctypes.windll.shell32.SHGetFolderPathW(None, 0x0010, None, 0, buf)   # CSIDL_DESKTOPDIRECTORY
            if buf.value:
                return Path(buf.value)
        except Exception:
            pass
    for name in ("Masaüstü", "Desktop"):
        p = Path.home() / name
        if p.is_dir():
            return p
    try:
        import subprocess
        r = subprocess.run(["xdg-user-dir", "DESKTOP"], capture_output=True, text=True, timeout=3)
        if r.stdout.strip():
            return Path(r.stdout.strip())
    except Exception:
        pass
    return Path.home()


def output_target(s: dict | None = None, ask=None) -> Path | None:
    """Output folder according to the settings; None = next to the source. ask: function that asks for a folder."""
    s = s or load()
    mode = s["output"]
    if mode == "desktop":
        return desktop()
    if mode == "folder" and s["output_dir"] and Path(s["output_dir"]).is_dir():
        return Path(s["output_dir"])
    if mode == "ask" and ask is not None:
        chosen = ask()
        return Path(chosen) if chosen else None
    return None


# ------------------------------------------------------------------ pencere

def run(paths=None, progress=print, **_) -> list:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from .actions import ACTIONS

    s = load()
    use = usage()
    root = tk.Tk()
    root.title(T("{0} — Ayarlar", APP_NAME))
    root.geometry(f"720x{min(780, root.winfo_screenheight() - 80)}")
    from . import theme
    pal = theme.setup(root)
    ttk.Label(root, text=T("Ayarlar"), style="Heading.TLabel", padding=(20, 16, 20, 4)).pack(anchor="w")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=16, pady=(6, 10))
    gen = ttk.Frame(nb, padding=(16, 14))
    nb.add(gen, text=T("Genel"))

    # --- Menu tab
    menu_tab = ttk.Frame(nb, padding=(16, 12))
    nb.add(menu_tab, text=T("Sağ tık menüsü"))
    ttk.Label(menu_tab, text=T("Göster: menüde görünsün · Üstte: menünün en başına sabitle"),
              style="Muted.TLabel").pack(anchor="w")
    auto = tk.BooleanVar(value=s["auto_favorites"])
    ttk.Checkbutton(menu_tab, text=T("En çok kullandığım {0} işlemi kendiliğinden en üste al", FAV_AUTO),
                    variable=auto).pack(anchor="w", pady=(4, 6))
    box = ttk.Frame(menu_tab)
    box.pack(fill="both", expand=True)
    cv = tk.Canvas(box, highlightthickness=0, background=pal.bg)
    sb = ttk.Scrollbar(box, orient="vertical", command=cv.yview)
    inner = ttk.Frame(cv)
    inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
    cv.create_window((0, 0), window=inner, anchor="nw")
    cv.configure(yscrollcommand=sb.set)
    cv.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")

    def wheel(e):
        cv.yview_scroll(-1 if (getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4) else 1, "units")
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(seq, wheel)

    show_vars, pin_vars = {}, {}
    ttk.Label(inner, text=T("Göster"), style="Strong.TLabel").grid(row=0, column=0, padx=4)
    ttk.Label(inner, text=T("Üstte"), style="Strong.TLabel").grid(row=0, column=1, padx=4)
    ttk.Label(inner, text=T("İşlem"), style="Strong.TLabel").grid(row=0, column=2, sticky="w", padx=(6, 0))
    ttk.Label(inner, text=T("Kullanım"), style="Strong.TLabel").grid(row=0, column=3, padx=12)
    groups: dict = {}
    for a in ACTIONS:                        # groups of the menu in order, each group once
        if a.id not in NEVER_HIDE:
            g = a.group or (T("Klasör") if "<klasör>" in a.extensions and len(a.extensions) == 1 else
                            T("Görsel / Office") if "pdf" not in a.extensions else T("Genel"))
            groups.setdefault(g, []).append(a)
    row, last_group = 1, None
    for g, a in ((g, a) for g, acts in groups.items() for a in acts):
        if g != last_group:
            ttk.Label(inner, text=T(g), style="Section.TLabel").grid(row=row, column=0, columnspan=4, sticky="w",
                                                                    pady=(12, 2))
            row += 1
            last_group = g
        sv = tk.BooleanVar(value=a.id not in s["hidden"])
        pv = tk.BooleanVar(value=a.id in s["pinned"])
        ttk.Checkbutton(inner, variable=sv).grid(row=row, column=0)
        ttk.Checkbutton(inner, variable=pv).grid(row=row, column=1)
        ttk.Label(inner, text=T(a.label)).grid(row=row, column=2, sticky="w", padx=(6, 0))
        ttk.Label(inner, text=str(use.get(a.id, "")), style="Muted.TLabel").grid(row=row, column=3)
        show_vars[a.id], pin_vars[a.id] = sv, pv
        row += 1

    # --- General tab
    ttk.Label(gen, text=T("Dil · Language"), style="Strong.TLabel").pack(anchor="w")
    lang_var = tk.StringVar(value=s.get("language", "auto"))
    lrow0 = ttk.Frame(gen)
    lrow0.pack(anchor="w", pady=(4, 0))
    for val, text in (("auto", T("Sistem dili")), ("tr", "Türkçe"), ("en", "English")):
        ttk.Radiobutton(lrow0, text=text, value=val, variable=lang_var).pack(side="left", padx=(0, 16))
    ttk.Label(gen, text=T("Menüler hemen, pencereler bir sonraki açılışta değişir."),
              style="Muted.TLabel").pack(anchor="w", pady=(2, 0))
    ttk.Separator(gen).pack(fill="x", pady=14)
    ttk.Label(gen, text=T("Görünüm"), style="Strong.TLabel").pack(anchor="w")
    look = tk.StringVar(value=s.get("theme", "system"))

    def preview(*_):
        """Try the chosen appearance in this window right away (applies to all windows once saved)."""
        dark = theme.system_is_dark() if look.get() == "system" else look.get() == "dark"
        cv.configure(background=theme.switch(root, dark).bg)
    lrow = ttk.Frame(gen)
    lrow.pack(anchor="w", pady=(4, 0))
    for val, text in (("system", T("Sistemle aynı")), ("light", T("Açık")), ("dark", T("Koyu"))):
        ttk.Radiobutton(lrow, text=text, value=val, variable=look, command=preview).pack(side="left", padx=(0, 16))
    ttk.Separator(gen).pack(fill="x", pady=14)
    ttk.Label(gen, text=T("Çıkan dosyalar nereye kaydedilsin?"), style="Strong.TLabel").pack(anchor="w")
    out = tk.StringVar(value=s["output"])
    out_dir = tk.StringVar(value=s["output_dir"])
    for val, text in (("beside", T("Kaynak dosyanın yanına")), ("desktop", T("Masaüstüne ({0})", desktop())),
                      ("ask", T("Her seferinde sor"))):
        ttk.Radiobutton(gen, text=text, value=val, variable=out).pack(anchor="w", pady=2)
    fr = ttk.Frame(gen)
    fr.pack(anchor="w", fill="x", pady=1)
    ttk.Radiobutton(fr, text=T("Şu klasöre:"), value="folder", variable=out).pack(side="left")
    ttk.Entry(fr, textvariable=out_dir, width=40).pack(side="left", padx=4, fill="x", expand=True)

    def pick():
        d = filedialog.askdirectory(parent=root)
        if d:
            out_dir.set(d)
            out.set("folder")
    ttk.Button(fr, text=T("Seç…"), command=pick).pack(side="left")
    par = tk.BooleanVar(value=s["parallel"])
    ttk.Separator(gen).pack(fill="x", pady=14)
    ttk.Label(gen, text=T("Hız"), style="Strong.TLabel").pack(anchor="w", pady=(0, 4))
    ttk.Checkbutton(gen, text=T("Çok dosya seçince işlemcinin bütün çekirdeklerini kullan (daha hızlı)"),
                    variable=par).pack(anchor="w")
    ttk.Separator(gen).pack(fill="x", pady=14)
    from . import __version__, update
    ttk.Label(gen, text=T("Güncellemeler · şu anki sürüm {0}", __version__), style="Strong.TLabel").pack(
        anchor="w", pady=(0, 4))
    upd = tk.BooleanVar(value=s.get("check_updates", True))
    ttk.Checkbutton(gen, text=T("Haftada bir yeni sürüm var mı diye bak (yalnızca sürüm numarası sorulur)"),
                    variable=upd).pack(anchor="w")
    urow = ttk.Frame(gen)
    urow.pack(anchor="w", pady=(6, 0))
    uinfo = ttk.Label(urow, style="Muted.TLabel")

    def check_now():
        uinfo.configure(text=T("Bakılıyor…"), foreground="")
        root.update_idletasks()
        latest = update.fetch()
        if latest is None:
            uinfo.configure(text=T("GitHub'a ulaşılamadı (internet bağlantısı?)"))
        elif update._ver(latest) > update._ver(__version__):
            uinfo.configure(text=T("Yeni sürüm var: {0} — indirmek için tıkla", latest), foreground=pal.accent,
                            cursor="hand2")
            uinfo.bind("<Button-1>", lambda e: update.open_download())
        else:
            uinfo.configure(text=T("Güncel sürümü kullanıyorsun."))
    ttk.Button(urow, text=T("Şimdi denetle"), command=check_now).pack(side="left")
    uinfo.pack(side="left", padx=10)
    ttk.Separator(gen).pack(fill="x", pady=14)

    def reset_use():
        _write("kullanim.json", {})
        messagebox.showinfo(APP_NAME, T("Kullanım sayaçları sıfırlandı."), parent=root)
    ttk.Button(gen, text=T("Kullanım sayaçlarını sıfırla"), command=reset_use).pack(anchor="w")

    foot = ttk.Frame(root, padding=(16, 0, 16, 14))
    foot.pack(fill="x")
    status = ttk.Label(foot, foreground=pal.success)
    status.pack(side="left")

    def do_save(*_):
        s["hidden"] = [k for k, v in show_vars.items() if not v.get()]
        s["pinned"] = [k for k, v in pin_vars.items() if v.get() and k not in s["hidden"]]
        s["auto_favorites"] = auto.get()
        s["output"] = out.get()
        s["output_dir"] = out_dir.get()
        s["parallel"] = par.get()
        s["check_updates"] = upd.get()
        s["theme"] = look.get()
        s["language"] = lang_var.get()
        if s["output"] == "folder" and not Path(s["output_dir"]).is_dir():
            messagebox.showerror(APP_NAME, T("Seçilen çıktı klasörü bulunamadı."), parent=root)
            return
        save(s)
        from . import i18n
        i18n.set_lang(None)                    # write the menus in the new language
        try:
            refresh_menu()
            status.configure(text=T("✔ Kaydedildi, menü güncellendi."))
        except Exception as e:
            status.configure(text=T("Kaydedildi; menü güncellenemedi: {0}", e))
        print(T("Ayarlar kaydedildi"))
        sys.stdout.flush()

    ttk.Button(foot, text=T("Kaydet"), style="Accent.TButton", width=12, command=do_save).pack(side="right")
    root.bind("<Control-s>", lambda e: (do_save(), "break")[1])
    autoclose = os.environ.get("RIFFLE_AUTOCLOSE_MS")
    if autoclose:
        root.after(int(autoclose), root.destroy)
    root.mainloop()
    return []
