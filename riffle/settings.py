"""Ayarlar: menüde görünen araçlar, sık kullanılanlar, çıktı klasörü, paralel çalışma.

ayarlar.json ve kullanim.json kullanıcı ayar klasöründe durur (Windows: %APPDATA%\\Riffle,
Linux: ~/.config/riffle). Dosya bozuk ya da yoksa varsayılanlar kullanılır.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import APP_NAME

DEFAULTS = {
    "hidden": [],             # menüde gösterilmeyen işlem kimlikleri
    "pinned": [],             # her zaman menünün en üstünde
    "auto_favorites": True,   # en çok kullanılan 3 işlem kendiliğinden üste çıkar
    "output": "beside",       # beside | desktop | folder | ask
    "output_dir": "",
    "parallel": True,         # çok dosyada işlemciyi tam kullan
    "theme": "system",        # system | light | dark
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
    """Menünün en üstüne çıkacak işlemler: sabitlenenler + (otomatikse) en çok kullanılanlar."""
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
    """Kullanım sayacını artırır. Sık kullanılanlar listesi değiştiyse True (menü yenilenmeli)."""
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
    """Menüyü ayarlara göre yeniden kurar (sessiz)."""
    if os.environ.get("RIFFLE_NO_MENU_REFRESH"):       # otomatik testler
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


# ------------------------------------------------------------------ çıktı klasörü

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
    """Ayarlara göre çıktı klasörü; None = kaynağın yanına. ask: klasör soran fonksiyon."""
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
    root.title(f"{APP_NAME} — Ayarlar")
    root.geometry(f"720x{min(780, root.winfo_screenheight() - 80)}")
    from . import theme
    pal = theme.setup(root)
    ttk.Label(root, text="Ayarlar", style="Heading.TLabel", padding=(20, 16, 20, 4)).pack(anchor="w")
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=16, pady=(6, 10))
    gen = ttk.Frame(nb, padding=(16, 14))
    nb.add(gen, text="Genel")

    # --- Menü sekmesi
    menu_tab = ttk.Frame(nb, padding=(16, 12))
    nb.add(menu_tab, text="Sağ tık menüsü")
    ttk.Label(menu_tab, text="Göster: menüde görünsün · Üstte: menünün en başına sabitle",
              style="Muted.TLabel").pack(anchor="w")
    auto = tk.BooleanVar(value=s["auto_favorites"])
    ttk.Checkbutton(menu_tab, text=f"En çok kullandığım {FAV_AUTO} işlemi kendiliğinden en üste al",
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
    ttk.Label(inner, text="Göster", style="Strong.TLabel").grid(row=0, column=0, padx=4)
    ttk.Label(inner, text="Üstte", style="Strong.TLabel").grid(row=0, column=1, padx=4)
    ttk.Label(inner, text="İşlem", style="Strong.TLabel").grid(row=0, column=2, sticky="w", padx=(6, 0))
    ttk.Label(inner, text="Kullanım", style="Strong.TLabel").grid(row=0, column=3, padx=12)
    groups: dict = {}
    for a in ACTIONS:                        # menüdeki gruplar sırasıyla, her grup bir kez
        if a.id not in NEVER_HIDE:
            g = a.group or ("Klasör" if "<klasör>" in a.extensions and len(a.extensions) == 1 else
                            "Görsel / Office" if "pdf" not in a.extensions else "Genel")
            groups.setdefault(g, []).append(a)
    row, last_group = 1, None
    for g, a in ((g, a) for g, acts in groups.items() for a in acts):
        if g != last_group:
            ttk.Label(inner, text=g, style="Section.TLabel").grid(row=row, column=0, columnspan=4, sticky="w",
                                                                    pady=(12, 2))
            row += 1
            last_group = g
        sv = tk.BooleanVar(value=a.id not in s["hidden"])
        pv = tk.BooleanVar(value=a.id in s["pinned"])
        ttk.Checkbutton(inner, variable=sv).grid(row=row, column=0)
        ttk.Checkbutton(inner, variable=pv).grid(row=row, column=1)
        ttk.Label(inner, text=a.label).grid(row=row, column=2, sticky="w", padx=(6, 0))
        ttk.Label(inner, text=str(use.get(a.id, "")), style="Muted.TLabel").grid(row=row, column=3)
        show_vars[a.id], pin_vars[a.id] = sv, pv
        row += 1

    # --- Genel sekmesi
    ttk.Label(gen, text="Görünüm", style="Strong.TLabel").pack(anchor="w")
    look = tk.StringVar(value=s.get("theme", "system"))

    def preview(*_):
        """Seçilen görünümü bu pencerede hemen dene (kaydedince bütün pencerelere geçer)."""
        dark = theme.system_is_dark() if look.get() == "system" else look.get() == "dark"
        cv.configure(background=theme.switch(root, dark).bg)
    lrow = ttk.Frame(gen)
    lrow.pack(anchor="w", pady=(4, 0))
    for val, text in (("system", "Sistemle aynı"), ("light", "Açık"), ("dark", "Koyu")):
        ttk.Radiobutton(lrow, text=text, value=val, variable=look, command=preview).pack(side="left", padx=(0, 16))
    ttk.Separator(gen).pack(fill="x", pady=14)
    ttk.Label(gen, text="Çıkan dosyalar nereye kaydedilsin?", style="Strong.TLabel").pack(anchor="w")
    out = tk.StringVar(value=s["output"])
    out_dir = tk.StringVar(value=s["output_dir"])
    for val, text in (("beside", "Kaynak dosyanın yanına"), ("desktop", f"Masaüstüne ({desktop()})"),
                      ("ask", "Her seferinde sor")):
        ttk.Radiobutton(gen, text=text, value=val, variable=out).pack(anchor="w", pady=2)
    fr = ttk.Frame(gen)
    fr.pack(anchor="w", fill="x", pady=1)
    ttk.Radiobutton(fr, text="Şu klasöre:", value="folder", variable=out).pack(side="left")
    ttk.Entry(fr, textvariable=out_dir, width=40).pack(side="left", padx=4, fill="x", expand=True)

    def pick():
        d = filedialog.askdirectory(parent=root)
        if d:
            out_dir.set(d)
            out.set("folder")
    ttk.Button(fr, text="Seç…", command=pick).pack(side="left")
    par = tk.BooleanVar(value=s["parallel"])
    ttk.Separator(gen).pack(fill="x", pady=14)
    ttk.Label(gen, text="Hız", style="Strong.TLabel").pack(anchor="w", pady=(0, 4))
    ttk.Checkbutton(gen, text="Çok dosya seçince işlemcinin bütün çekirdeklerini kullan (daha hızlı)",
                    variable=par).pack(anchor="w")
    ttk.Separator(gen).pack(fill="x", pady=14)

    def reset_use():
        _write("kullanim.json", {})
        messagebox.showinfo(APP_NAME, "Kullanım sayaçları sıfırlandı.", parent=root)
    ttk.Button(gen, text="Kullanım sayaçlarını sıfırla", command=reset_use).pack(anchor="w")

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
        s["theme"] = look.get()
        if s["output"] == "folder" and not Path(s["output_dir"]).is_dir():
            messagebox.showerror(APP_NAME, "Seçilen çıktı klasörü bulunamadı.", parent=root)
            return
        save(s)
        try:
            refresh_menu()
            status.configure(text="✔ Kaydedildi, menü güncellendi.")
        except Exception as e:
            status.configure(text=f"Kaydedildi; menü güncellenemedi: {e}")
        print("Ayarlar kaydedildi")
        sys.stdout.flush()

    ttk.Button(foot, text="Kaydet", style="Accent.TButton", width=12, command=do_save).pack(side="right")
    root.bind("<Control-s>", lambda e: (do_save(), "break")[1])
    autoclose = os.environ.get("RIFFLE_AUTOCLOSE_MS")
    if autoclose:
        root.after(int(autoclose), root.destroy)
    root.mainloop()
    return []
