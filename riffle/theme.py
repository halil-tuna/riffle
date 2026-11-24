"""Shared look: Windows 11 style theme (Sun Valley, sv-ttk), light/dark mode that follows the system,
shared colours, fonts, the window icon and a dark title bar on Windows.

Every window calls `pal = theme.setup(root)` when it opens; classic tk widgets (Listbox, Canvas,
Text) take their colours from the returned palette. Without sv-ttk the 'clam' theme is painted with the same palette.
Theme choice: RIFFLE_THEME=light|dark (tests) > Settings > system.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / "assets"
ICON_PNG = ASSETS / "riffle.png"
ICON_ICO = ASSETS / "riffle.ico"


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str          # window background
    surface: str     # card / list / entry background
    canvas: str      # behind the page thumbnails
    fg: str
    muted: str       # secondary text
    border: str
    accent: str      # accent (selection frame, link)
    select_bg: str
    select_fg: str
    success: str
    danger: str
    warn: str        # split mark (orange, the colour from the icon)
    ink: str         # signature ink


LIGHT = Palette(False, bg="#fafafa", surface="#ffffff", canvas="#eef0f3", fg="#1c1c1c", muted="#5f6670",
                border="#d9dce1", accent="#005fb8", select_bg="#2f60d8", select_fg="#ffffff",
                success="#0f7b0f", danger="#c42b1c", warn="#d9480f", ink="#10205f")
DARK = Palette(True, bg="#1c1c1c", surface="#2b2b2b", canvas="#141414", fg="#fafafa", muted="#a3a8b0",
               border="#3d3d3d", accent="#57c8ff", select_bg="#2f60d8", select_fg="#ffffff",
               success="#6ccb5f", danger="#ff99a4", warn="#ff8a4c", ink="#10205f")

_current: Palette | None = None


def preference() -> str:
    """'light' | 'dark' | 'system'."""
    env = os.environ.get("RIFFLE_THEME", "").lower()
    if env in ("light", "dark", "system"):
        return env
    try:
        from .settings import load
        v = str(load().get("theme", "system")).lower()
        return v if v in ("light", "dark") else "system"
    except Exception:
        return "system"


def system_is_dark() -> bool:
    try:
        import darkdetect
        return bool(darkdetect.isDark())
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
        except OSError:
            return False
    return False


def is_dark() -> bool:
    pref = preference()
    return system_is_dark() if pref == "system" else pref == "dark"


def palette() -> Palette:
    return _current or (DARK if is_dark() else LIGHT)


# ------------------------------------------------------------------ fonts

def _families(root) -> set:
    from tkinter import font
    try:
        return set(font.families(root))
    except Exception:
        return set()


def ui_family(root=None) -> str:
    fams = _families(root) if root is not None else set()
    for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Ubuntu", "Cantarell", "Noto Sans",
              "DejaVu Sans", "Liberation Sans"):
        if not fams or f in fams:
            return f
    return "TkDefaultFont"


def _fix_fonts(root) -> None:
    """sv-ttk wants 'Segoe UI Variable'; on Windows 10 and Linux, where it is missing, use the closest family."""
    from tkinter import font
    fams = _families(root)
    if not fams:
        return
    base = ui_family(root)
    strong = base if "Variable" not in base else "Segoe UI Variable Text Semibold"
    for name in ("SunValleyCaptionFont", "SunValleyBodyFont", "SunValleyBodyStrongFont", "SunValleyBodyLargeFont",
                 "SunValleySubtitleFont", "SunValleyTitleFont", "SunValleyTitleLargeFont", "SunValleyDisplayFont"):
        try:
            f = font.nametofont(name, root)
        except Exception:
            continue
        if f.actual("family") not in fams or f.cget("family") not in fams:
            bold = "Strong" in name or "Subtitle" in name or "Title" in name or "Display" in name
            f.configure(family=strong if bold and strong in fams else base, weight="bold" if bold else "normal")
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            font.nametofont(name, root).configure(family=base, size=10)
        except Exception:
            pass


# ------------------------------------------------------------------ kurulum

def setup(root, *, resizable: bool | None = None) -> Palette:
    """Applies the theme, icon and title bar to the window; returns the palette."""
    global _current
    from tkinter import ttk
    pal = DARK if is_dark() else LIGHT
    _current = pal
    style = ttk.Style(root)
    loaded = False
    try:
        import sv_ttk
        _sv_apply(root, pal.dark)
        loaded = True
    except Exception:
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(".", background=pal.bg, foreground=pal.fg, fieldbackground=pal.surface,
                        bordercolor=pal.border, lightcolor=pal.bg, darkcolor=pal.bg,
                        troughcolor=pal.canvas, selectbackground=pal.select_bg, selectforeground=pal.select_fg)
        style.configure("TButton", padding=(10, 4))
        style.configure("Accent.TButton", background=pal.accent, foreground="#ffffff" if not pal.dark else "#000000")
        style.map("Accent.TButton", background=[("active", pal.select_bg), ("disabled", pal.border)])
        style.configure("Treeview", background=pal.surface, fieldbackground=pal.surface, foreground=pal.fg)
        root.configure(background=pal.bg)
    root._riffle_sv = loaded
    _fix_fonts(root)
    setup_styles(root)
    window(root)
    return pal


def setup_styles(root) -> None:
    """Riffle's own styles (heading, muted text, success/error) — called again when the theme changes."""
    from tkinter import font, ttk
    pal = palette()
    style = ttk.Style(root)
    loaded = getattr(root, "_riffle_sv", False)
    fam = ui_family(root)
    if "RiffleHeadline" not in font.names(root):
        base = font.nametofont("SunValleyBodyStrongFont", root) if loaded else None
        font.Font(root, name="RiffleHeadline", family=base.cget("family") if base else fam,
                  size=-17, weight="bold")
    head = "SunValleyBodyStrongFont" if loaded else (fam, 10, "bold")
    style.configure("Heading.TLabel", font=("SunValleySubtitleFont" if loaded else (fam, 15, "bold")))
    style.configure("Strong.TLabel", font=head)
    style.configure("Muted.TLabel", foreground=pal.muted)
    style.configure("Section.TLabel", foreground=pal.accent, font=head)
    style.configure("Success.TLabel", foreground=pal.success, font="RiffleHeadline")
    style.configure("Danger.TLabel", foreground=pal.danger, font="RiffleHeadline")
    style.configure("Big.TLabel", font=("SunValleyBodyLargeFont" if loaded else (fam, 13)))
    if sys.platform == "win32" and getattr(root, "_riffle_titlebar", False):
        _win_titlebar(root, pal.dark)           # the theme changed live (Settings preview)


def _sv_apply(root, dark: bool) -> None:
    """Apply the sv-ttk theme. sv-ttk paints the background/text colours on the <<ThemeChanged>> event (with
    tk_setPalette); when the event arrives is unpredictable: sometimes never (the window stayed light grey),
    sometimes after the widgets were built (overwriting the style colours and turning muted text white). We
    remove the event binding and paint the colours now, before the widgets are built."""
    import sv_ttk
    sv_ttk.set_theme("dark" if dark else "light", root)
    root.tk.call("bind", root.winfo_class(), "<<ThemeChanged>>", "")
    root.tk.call("configure_colors")


def switch(root, dark: bool) -> Palette:
    """Change the theme of an open window (the preview in Settings)."""
    global _current
    _current = DARK if dark else LIGHT
    try:
        _sv_apply(root, dark)
    except Exception:
        pass
    restyle(root)
    return _current


def restyle(root) -> None:
    """When the theme changes live: clear the colours tk_setPalette painted so the style colours apply."""
    from tkinter import ttk
    setup_styles(root)
    stack = [root]
    while stack:
        w = stack.pop()
        stack.extend(w.winfo_children())
        if isinstance(w, ttk.Label) and str(w.cget("style")).split(".")[0] in (
                "Muted", "Section", "Success", "Danger", "Strong", "Heading", "Big"):
            w.configure(foreground="")


def window(win) -> None:
    """Icon + (Windows) title bar colour; also called for Toplevels."""
    try:
        if ICON_PNG.exists():
            import tkinter as tk
            img = getattr(win, "_riffle_icon", None) or tk.PhotoImage(master=win, file=str(ICON_PNG))
            win._riffle_icon = img
            win.iconphoto(True, img)
    except Exception:
        pass
    if sys.platform == "win32":
        # the window's Windows frame is created on first show: set the title colour at that moment
        def on_map(_e=None):
            if not getattr(win, "_riffle_titlebar", False):
                win._riffle_titlebar = True
                _win_titlebar(win, palette().dark)
        win.bind("<Map>", on_map, add="+")


def _win_titlebar(win, dark: bool) -> None:
    """Windows 10 (1809+) / 11: make the title bar dark/light (DWMWA_USE_IMMERSIVE_DARK_MODE)."""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


# ------------------------------------------------------------------ classic tk widgets

def style_listbox(lb, pal: Palette | None = None) -> None:
    pal = pal or palette()
    lb.configure(background=pal.surface, foreground=pal.fg, selectbackground=pal.select_bg,
                 selectforeground=pal.select_fg, highlightthickness=1, highlightbackground=pal.border,
                 highlightcolor=pal.accent, borderwidth=0, relief="flat", activestyle="none")


def style_text(t, pal: Palette | None = None) -> None:
    pal = pal or palette()
    t.configure(background=pal.surface, foreground=pal.fg, insertbackground=pal.fg,
                selectbackground=pal.select_bg, selectforeground=pal.select_fg,
                highlightthickness=1, highlightbackground=pal.border, highlightcolor=pal.accent,
                borderwidth=0, relief="flat", padx=6, pady=4)


def center(win, dy: int = 3) -> None:
    win.update_idletasks()
    w, h = win.winfo_width(), win.winfo_height()
    win.geometry(f"+{max((win.winfo_screenwidth() - w) // 2, 0)}+{max((win.winfo_screenheight() - h) // dy, 0)}")
