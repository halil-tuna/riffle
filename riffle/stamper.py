"""Signature / text / image window (tkinter).

  - ✍ My signature: puts the saved signature on the page (draw it or load it from a photo the first time)
  - 🖼 Image: logo, stamp, photo · T Text · 📅 Date
  - Drag: move · bottom-right square: resize · double-click (text): edit · Del: delete
  - Every page: copies the selected item to every page (for a logo, letterhead, initials)
Shortcuts: PageUp/PageDown page · arrow keys move · Ctrl+I my signature · Ctrl+D date ·
           Ctrl+G all pages · Ctrl+S save · Ctrl+Z undo
"""

from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

from . import APP_NAME
from . import stamp_model as sm
from .actions import open_pdf, pymupdf
from .i18n import T

COLORS = {"Lacivert": (0.05, 0.1, 0.4), "Siyah": (0, 0, 0), "Mavi": (0.1, 0.3, 0.85), "Kırmızı": (0.75, 0.05, 0.05)}
HANDLE = 9


def _color(shown: str):
    """Colour from the (translated) colour name shown in the menu."""
    for name, rgb in COLORS.items():
        if T(name) == shown:
            return rgb
    return COLORS["Lacivert"]


SEL = "#2f6fdd"


def _hex(c):
    return "#%02x%02x%02x" % tuple(int(v * 255) for v in c)


def _ui_font():
    return "Arial" if sys.platform == "win32" else "DejaVu Sans"


def signature_dialog(parent):
    """Draw a signature or load it from a photo. Returns the PNG bytes (cancel → None)."""
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    W, H = 600, 220
    top = tk.Toplevel(parent)
    top.title(T("{0} — İmza", APP_NAME))
    top.transient(parent)
    top.resizable(False, False)
    from . import theme
    theme.window(top)
    pal = theme.palette()
    ttk.Label(top, text=T("İmzanı at"), style="Strong.TLabel", padding=(16, 14, 16, 0)).pack(anchor="w")
    ttk.Label(top, text=T("Fareyle ya da dokunmatik kalemle çiz; veya beyaz kâğıda attığın imzanın fotoğrafını yükle."),
              style="Muted.TLabel", padding=(16, 2, 16, 10)).pack(anchor="w")
    cv = tk.Canvas(top, width=W, height=H, background="white", highlightthickness=1,
                   highlightbackground=pal.border, cursor="pencil")
    cv.pack(padx=16)
    cv.create_line(30, H - 50, W - 30, H - 50, fill="#d6dae0", dash=(4, 3))
    strokes: list[list] = []
    result = {"png": None}

    def down(e):
        strokes.append([(e.x, e.y)])
        cv.create_oval(e.x - 2, e.y - 2, e.x + 2, e.y + 2, fill="#10205f", outline="", tags="ink")

    def move(e):
        if strokes:
            x0, y0 = strokes[-1][-1]
            strokes[-1].append((e.x, e.y))
            cv.create_line(x0, y0, e.x, e.y, fill="#10205f", width=4, capstyle="round", smooth=True, tags="ink")

    def clear():
        strokes.clear()
        cv.delete("ink")

    def load():
        f = filedialog.askopenfilename(parent=top, title=T("İmza fotoğrafı"),
                                       filetypes=[(T("Görseller"), "*.png *.jpg *.jpeg *.heic *.bmp *.webp"),
                                                  (T("Tümü"), "*.*")])
        if not f:
            return
        try:
            src = sm.image_bytes(Path(f))
            result["png"] = sm.clean_signature(io.BytesIO(src))
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=top)
            return
        top.destroy()

    def ok():
        if not strokes:
            messagebox.showinfo(APP_NAME, T("Önce imzanı çiz ya da fotoğraf yükle."), parent=top)
            return
        result["png"] = sm.strokes_to_png(strokes, (W, H))
        top.destroy()

    cv.bind("<ButtonPress-1>", down)
    cv.bind("<B1-Motion>", move)
    row = ttk.Frame(top, padding=(16, 12, 16, 14))
    row.pack(fill="x")
    ttk.Button(row, text=T("Temizle"), command=clear).pack(side="left")
    ttk.Button(row, text=T("Fotoğraftan yükle…"), command=load).pack(side="left", padx=8)
    ttk.Button(row, text=T("Tamam"), style="Accent.TButton", width=10, command=ok).pack(side="right")
    ttk.Button(row, text=T("İptal"), width=10, command=top.destroy).pack(side="right", padx=8)
    top.bind("<Return>", lambda e: ok())
    top.bind("<Escape>", lambda e: top.destroy())
    top.grab_set()
    cv.focus_set()
    parent.wait_window(top)
    return result["png"]


def run(paths: list[Path], progress=print) -> list[Path]:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
    from .ui import _dialog

    src = Path(paths[0])
    doc = open_pdf(src)

    root = tk.Tk()
    root.title(T("{0} — İmza / yazı / görsel ekle — {1}", APP_NAME, src.name))
    root.geometry(f"{min(1000, root.winfo_screenwidth() - 60)}x{min(860, root.winfo_screenheight() - 80)}")
    root.minsize(560, 480)
    from . import theme
    pal = theme.setup(root)
    style = ttk.Style(root)
    style.configure("Tool.TButton", padding=(8, 3))

    stamps: list[sm.Stamp] = []
    history: list[list] = []
    state = {"page": 0, "zoom": 1.0, "ox": 0, "oy": 0, "sel": None, "drag": None, "bg": None}
    imgs: dict = {}                   # keep tk images from the garbage collector
    saved: list[Path] = []
    sig_cache = {"png": sm.signature_path().read_bytes() if sm.signature_path().exists() else None}

    bar = ttk.Frame(root, padding=(8, 6))
    bar.pack(fill="x")
    canvas = tk.Canvas(root, background=pal.canvas, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    foot = ttk.Frame(root, padding=(10, 8))
    foot.pack(fill="x")
    status = ttk.Label(foot, anchor="w", style="Muted.TLabel")

    def btn(parent, text, cmd):
        b = ttk.Button(parent, text=text, command=cmd, style="Tool.TButton", width=len(text) + 1)
        b.pack(side="left", padx=1)
        return b

    # ------------------------------------------------------------------ coordinates
    def page_rect():
        return doc[state["page"]].rect

    def to_canvas(x, y):
        return state["ox"] + x * state["zoom"], state["oy"] + y * state["zoom"]

    def to_page(cx, cy):
        return (cx - state["ox"]) / state["zoom"], (cy - state["oy"]) / state["zoom"]

    def current():
        return [s for s in stamps if s.page == state["page"]]

    def selected():
        return next((s for s in stamps if s.uid == state["sel"]), None)

    def snapshot():
        import copy
        history.append(copy.deepcopy(stamps))
        del history[:-60]

    def undo():
        if history:
            stamps[:] = history.pop()
            state["sel"] = None
            redraw()

    # ------------------------------------------------------------------ drawing
    def render_page():
        cw, ch = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 200)
        r = page_rect()
        z = min((cw - 40) / r.width, (ch - 30) / r.height)
        state["zoom"] = max(z, 0.1)
        state["ox"] = (cw - r.width * state["zoom"]) / 2
        state["oy"] = 15
        pix = doc[state["page"]].get_pixmap(matrix=pymupdf.Matrix(state["zoom"], state["zoom"]), alpha=False)
        state["bg"] = tk.PhotoImage(data=base64.b64encode(pix.tobytes("png")))

    def item_image(s: sm.Stamp, w: int, h: int):
        key = (s.uid, w, h)
        if key not in imgs:
            from PIL import Image
            im = Image.open(io.BytesIO(s.data)).convert("RGBA").resize((max(w, 1), max(h, 1)), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "PNG")
            imgs[key] = tk.PhotoImage(data=base64.b64encode(buf.getvalue()))
        return imgs[key]

    def redraw(rerender=False):
        if rerender or state["bg"] is None:
            render_page()
        canvas.delete("all")
        r = page_rect()
        x0, y0 = to_canvas(0, 0)
        x1, y1 = to_canvas(r.width, r.height)
        canvas.create_rectangle(x0 + 3, y0 + 3, x1 + 3, y1 + 3, fill="#000000" if pal.dark else "#c4c8cf", outline="")
        canvas.create_image(x0, y0, image=state["bg"], anchor="nw")
        for s in current():
            a, b = to_canvas(*s.rect[:2])
            c, d = to_canvas(*s.rect[2:])
            if s.kind == "image":
                canvas.create_image(a, b, image=item_image(s, int(c - a), int(d - b)), anchor="nw",
                                    tags=("item", f"u{s.uid}"))
            else:
                px = s.fontsize() * state["zoom"]
                canvas.create_text(a + 1, b, text=s.text, anchor="nw", fill=_hex(s.color),
                                   font=(_ui_font(), -max(int(px), 4)), tags=("item", f"u{s.uid}"))
            if s.uid == state["sel"]:
                canvas.create_rectangle(a, b, c, d, outline=SEL, dash=(4, 2), width=1.5)
                canvas.create_rectangle(c - HANDLE, d - HANDLE, c + 2, d + 2, fill=SEL, outline="white")
        pg_lbl.configure(text=T("Sayfa {0} / {1}", state['page'] + 1, len(doc)))
        n = len(stamps)
        status.configure(text=(T("{0} öğe eklendi · sürükle: taşı · köşe: boyut · çift tık: yazıyı düzenle", n)
                               if n else T("Araç çubuğundan imza, görsel ya da yazı ekle.")))

    # ------------------------------------------------------------------ ekleme
    def add(stamp: sm.Stamp):
        snapshot()
        stamps.append(stamp)
        state["sel"] = stamp.uid
        redraw()
        return stamp

    def add_image(data: bytes, width=160):
        return add(sm.Stamp("image", state["page"], sm.place_image(data, page_rect(), width=width), data=data))

    def my_signature():
        if not sig_cache["png"]:
            png = signature_dialog(root)
            if not png:
                return
            sm.signature_path().write_bytes(png)
            sig_cache["png"] = png
        add_image(sig_cache["png"], width=150)

    def new_signature():
        png = signature_dialog(root)
        if png:
            sm.signature_path().write_bytes(png)
            sig_cache["png"] = png
            add_image(png, width=150)

    def add_picture():
        f = filedialog.askopenfilename(parent=root, title=T("Görsel / logo / kaşe seç"),
                                       filetypes=[(T("Görseller"), "*.png *.jpg *.jpeg *.heic *.heif *.bmp *.webp *.gif"),
                                                  (T("Tümü"), "*.*")])
        if f:
            try:
                add_image(sm.image_bytes(Path(f)), width=140)
            except Exception as e:
                messagebox.showerror(APP_NAME, str(e), parent=root)

    def add_text(text=None, size=14):
        if text is None:
            text = _dialog(T("Eklenecek yazı (alt satır için \\n):"), parent=root)
            if not text or not text.strip():
                return
            text = text.replace("\\n", "\n")
        r = page_rect()
        rect = sm.text_rect(text, (r.width * 0.12, r.height * 0.82), size)
        return add(sm.Stamp("text", state["page"], sm.clamp(rect, r), text=text, color=_color(color.get())))

    def add_date():
        add_text(sm.today_text(), 12)

    def delete():
        s = selected()
        if s:
            snapshot()
            stamps.remove(s)
            state["sel"] = None
            redraw()

    def all_pages():
        s = selected()
        if not s:
            messagebox.showinfo(APP_NAME, T("Önce bir öğe seç."), parent=root)
            return
        snapshot()
        others = [x for x in range(len(doc)) if x != s.page]
        for c in sm.copy_to_pages(s, others):
            c.rect = sm.clamp(c.rect, doc[c.page].rect)
            stamps.append(c)
        status.configure(text=T("Seçili öğe {0} sayfaya daha kopyalandı.", len(others)))
        redraw()

    def set_color(*_):
        s = selected()
        if s and s.kind == "text":
            snapshot()
            s.color = _color(color.get())
            redraw()

    def edit_text(s):
        t = _dialog(T("Yazı:"), s.text.replace("\n", "\\n"), parent=root)
        if t and t.strip():
            snapshot()
            t = t.replace("\\n", "\n")
            fs = s.fontsize()
            s.text = t
            s.rect = sm.clamp(sm.text_rect(t, s.rect[:2], fs), page_rect())
            redraw()

    # ------------------------------------------------------------------ page
    def go(delta):
        p = min(max(state["page"] + delta, 0), len(doc) - 1)
        if p != state["page"]:
            state["page"] = p
            state["sel"] = None
            redraw(rerender=True)

    # ------------------------------------------------------------------ kaydet
    def save():
        if not stamps:
            messagebox.showinfo(APP_NAME, T("Eklenmiş öğe yok."), parent=root)
            return
        status.configure(text=T("Kaydediliyor…"))
        root.update_idletasks()
        try:
            out = sm.apply(src, stamps)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=root)
            return
        saved.append(out)
        print(T("Tamamlandı:"), out.name)
        sys.stdout.flush()
        status.configure(text=T("✔ Kaydedildi: {0}  →  {1}", out.name, out.parent))
        open_btn.configure(state="normal")

    def open_folder():
        if saved:
            from .ui import open_folder as of
            of(saved[-1])

    # ------------------------------------------------------------------ fare
    def hit(cx, cy):
        x, y = to_page(cx, cy)
        tol = HANDLE / state["zoom"]
        for s in reversed(current()):
            x0, y0, x1, y1 = s.rect
            if s.uid == state["sel"] and abs(x - x1) <= tol and abs(y - y1) <= tol:
                return s, "resize"
            if x0 <= x <= x1 and y0 <= y <= y1:
                return s, "move"
        return None, None

    def on_press(e):
        canvas.focus_set()
        s, mode = hit(e.x, e.y)
        state["sel"] = s.uid if s else None
        if s:
            snapshot()
            state["drag"] = (mode, s, to_page(e.x, e.y), s.rect, False)
        else:
            state["drag"] = None
        redraw()

    def on_motion(e):
        if not state["drag"]:
            s, mode = hit(e.x, e.y)
            canvas.configure(cursor="sizing" if mode == "resize" else "fleur" if mode else "")
            return
        mode, s, (sx, sy), r0, _ = state["drag"]
        x, y = to_page(e.x, e.y)
        dx, dy = x - sx, y - sy
        x0, y0, x1, y1 = r0
        if mode == "move":
            s.rect = sm.clamp((x0 + dx, y0 + dy, x1 + dx, y1 + dy), page_rect())
        else:
            w = max(12.0, x1 - x0 + dx)
            h = w / max(s.aspect, 1e-6)          # the aspect ratio is kept (font size grows too for text)
            r = page_rect()
            w, h = min(w, r.width - x0), min(h, r.height - y0)
            s.rect = (x0, y0, x0 + w, y0 + h)
        state["drag"] = (mode, s, (sx, sy), r0, True)
        redraw()

    def on_release(e):
        if state["drag"] and not state["drag"][4]:
            history.pop()                       # just clicked: don't pollute the undo history
        state["drag"] = None

    def on_double(e):
        s, _ = hit(e.x, e.y)
        if s and s.kind == "text":
            edit_text(s)

    def nudge(dx, dy):
        s = selected()
        if s:
            x0, y0, x1, y1 = s.rect
            s.rect = sm.clamp((x0 + dx, y0 + dy, x1 + dx, y1 + dy), page_rect())
            redraw()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_motion)
    canvas.bind("<Motion>", on_motion)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Double-Button-1>", on_double)
    canvas.bind("<Configure>", lambda e: redraw(rerender=True))

    def key(seq, fn):
        root.bind(seq, lambda e: (fn(), "break")[1])

    key("<Delete>", delete)
    key("<BackSpace>", delete)
    key("<Control-z>", undo)
    key("<Control-s>", save)
    key("<Prior>", lambda: go(-1))
    key("<Next>", lambda: go(1))
    key("<Left>", lambda: nudge(-2, 0))
    key("<Right>", lambda: nudge(2, 0))
    key("<Up>", lambda: nudge(0, -2))
    key("<Down>", lambda: nudge(0, 2))
    key("<Escape>", lambda: (state.update(sel=None), redraw()))
    key("<Control-i>", my_signature)
    key("<Control-d>", add_date)
    key("<Control-g>", all_pages)

    # ------------------------------------------------------------------ toolbar
    btn(bar, T("✍ İmzam"), my_signature)
    btn(bar, T("Yeni imza"), new_signature)
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=5)
    btn(bar, T("🖼 Görsel"), add_picture)
    btn(bar, T("T Yazı"), add_text)
    btn(bar, T("Tarih"), add_date)
    color = tk.StringVar(value=T("Lacivert"))
    cb = ttk.Combobox(bar, textvariable=color, values=[T(c) for c in COLORS], state="readonly", width=9)
    cb.pack(side="left", padx=4)
    cb.bind("<<ComboboxSelected>>", set_color)
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=5)
    btn(bar, T("Her sayfaya"), all_pages)
    btn(bar, T("Sil"), delete)
    btn(bar, "↶", undo)
    nav = ttk.Frame(foot)                       # page navigation in the bottom bar: visible in a narrow window too
    nav.pack(side="left", padx=(0, 10))
    ttk.Button(nav, text="◀", width=3, command=lambda: go(-1), style="Tool.TButton").pack(side="left")
    pg_lbl = ttk.Label(nav, width=12, anchor="center")
    pg_lbl.pack(side="left")
    ttk.Button(nav, text="▶", width=3, command=lambda: go(1), style="Tool.TButton").pack(side="left")

    ttk.Button(foot, text=T("Kaydet"), command=save, style="Accent.TButton", width=12).pack(side="right", padx=(6, 0))
    open_btn = ttk.Button(foot, text=T("Klasörü aç"), command=open_folder, state="disabled", style="Tool.TButton")
    open_btn.pack(side="right", padx=2)
    status.pack(side="left", fill="x", expand=True)


    root.after(80, lambda: redraw(rerender=True))
    auto = os.environ.get("RIFFLE_AUTOCLOSE_MS")
    if auto:
        root.after(int(auto), root.destroy)
    root.mainloop()
    doc.close()
    return saved
