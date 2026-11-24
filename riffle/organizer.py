"""Sayfa düzenleyici penceresi (tkinter).

Seçilen PDF / görsel / Office dosyalarının tüm sayfaları küçük resim olarak gelir:
  - sürükle-bırak ile sırala, Ctrl/Shift ile çoklu seç
  - döndür, sil, çoğalt, boş sayfa / dosya ekle
  - ✂ böl işareti: kaydederken o noktadan ayrı PDF başlar
  - Kaydet: tek PDF (ya da böl işaretlerine göre birden çok PDF), kaynağın yanına

Kısayollar: Ctrl+A tümü · Del sil · Ctrl+R sağa · Ctrl+L sola · Ctrl+D çoğalt ·
            Ctrl+B böl · Ctrl+Z geri al · Ctrl+←/→ taşı · Ctrl+S kaydet
"""

from __future__ import annotations

import base64
import os
import queue
import sys
import threading
from pathlib import Path

from . import APP_NAME
from .organizer_model import Document
from .i18n import T

CELL_W, CELL_H, THUMB = 168, 214, 150
PAD = 14
SEL = "#2f60d8"     # seçim (açık ve koyu temada aynı)
SPLIT = "#d9480f"   # bölme işareti (simgedeki turuncu)


def run(paths: list[Path], progress=print) -> list[Path]:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    doc = Document()
    try:                                   # Gezgin'den sürükle-bırak (isteğe bağlı paket)
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
    except Exception:
        DND_FILES = None
        root = tk.Tk()
    root.title(T("{0} — Sayfa düzenleyici", APP_NAME))
    root.geometry(f"{min(1180, root.winfo_screenwidth() - 60)}x{min(760, root.winfo_screenheight() - 80)}")
    root.minsize(640, 420)
    from . import theme
    pal = theme.setup(root)
    fam = theme.ui_family(root)

    saved: list[Path] = []
    sel: set[int] = set()          # seçili sayfa dizinleri
    anchor = [None]                # Shift ile aralık seçimi için
    images: dict = {}              # (uid, rot) → PhotoImage
    pending: set = set()
    q: queue.Queue = queue.Queue()

    # ------------------------------------------------------------------ düzen
    bar = ttk.Frame(root, padding=(8, 6))
    bar.pack(fill="x")
    body = ttk.Frame(root)
    body.pack(fill="both", expand=True)
    canvas = tk.Canvas(body, background=pal.canvas, highlightthickness=0)
    vs = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=vs.set)
    vs.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    foot = ttk.Frame(root, padding=(10, 8))
    foot.pack(fill="x")
    status = ttk.Label(foot, anchor="w")
    style = ttk.Style(root)
    style.configure("Tool.TButton", padding=(8, 3))

    def btn(text, cmd, tip=None):
        b = ttk.Button(bar, text=text, command=cmd, style="Tool.TButton", width=len(text) + 1)
        b.pack(side="left", padx=1)
        return b

    # ------------------------------------------------------------------ yardımcılar
    def cols() -> int:
        return max(1, (canvas.winfo_width() - PAD) // (CELL_W + PAD))

    def cell_xy(i):
        c = cols()
        return PAD + (i % c) * (CELL_W + PAD), PAD + (i // c) * (CELL_H + PAD)

    def index_at(x, y):
        c = cols()
        col = int((x - PAD) // (CELL_W + PAD))
        row = int((y - PAD) // (CELL_H + PAD))
        if col < 0 or row < 0 or col >= c:
            return None
        i = row * c + col
        return i if i < len(doc.pages) else None

    def gap_at(x, y) -> int:
        """Bırakma konumu: iki küçük resim arasındaki boşluğun dizini (0..n)."""
        c = cols()
        row = max(0, int((y - PAD) // (CELL_H + PAD)))
        col = round((x - PAD) / (CELL_W + PAD))
        col = max(0, min(col, c))
        return max(0, min(row * c + col, len(doc.pages)))

    def request_thumb(p):
        key = (p.uid, p.rotation)
        if key in images or key in pending:
            return
        pending.add(key)

        def work():
            try:
                q.put((key, doc.thumbnail_png(p, THUMB)))
            except Exception:
                q.put((key, None))
        threading.Thread(target=work, daemon=True).start()

    def poll():
        changed = False
        try:
            while True:
                key, png = q.get_nowait()
                pending.discard(key)
                if png:
                    images[key] = tk.PhotoImage(data=base64.b64encode(png))
                    changed = True
        except queue.Empty:
            pass
        if changed:
            redraw()
        root.after(60, poll)

    # ------------------------------------------------------------------ çizim
    drag = {"start": None, "active": False, "gap": None}

    def redraw(*_):
        canvas.delete("all")
        n = len(doc.pages)
        for i, p in enumerate(doc.pages):
            x, y = cell_xy(i)
            selected = i in sel
            canvas.create_rectangle(x, y, x + CELL_W, y + CELL_H, fill=pal.surface,
                                    outline=SEL if selected else pal.border, width=3 if selected else 1)
            img = images.get((p.uid, p.rotation))
            if img is None:
                request_thumb(p)
                canvas.create_text(x + CELL_W / 2, y + THUMB / 2 + 8, text="…", fill=pal.muted)
            else:
                canvas.create_image(x + CELL_W / 2, y + 8 + THUMB / 2, image=img)
            label = p.label if len(p.label) <= 24 else p.label[:22] + "…"
            canvas.create_text(x + CELL_W / 2, y + CELL_H - 30, text=f"{i + 1}", fill=pal.fg,
                               font=(fam, 10, "bold"))
            canvas.create_text(x + CELL_W / 2, y + CELL_H - 13, text=label, fill=pal.muted, font=(fam, 8))
            if p.rotation:
                canvas.create_text(x + CELL_W - 8, y + 10, text=f"↻{p.rotation}°", anchor="ne",
                                   fill=SEL, font=(fam, 8, "bold"))
            if p.uid in doc.split_after and i < n - 1:
                sx = x + CELL_W + PAD / 2
                canvas.create_line(sx, y - 4, sx, y + CELL_H + 4, fill=SPLIT, width=3, dash=(6, 4))
                canvas.create_text(sx, y - 6, text="✂", fill=SPLIT, font=(fam, 12, "bold"))
        if drag["active"] and drag["gap"] is not None:
            g = drag["gap"]
            if g < n:
                x, y = cell_xy(g)
                gx = x - PAD / 2
            else:
                x, y = cell_xy(n - 1)
                gx = x + CELL_W + PAD / 2
            canvas.create_line(gx, y, gx, y + CELL_H, fill=SEL, width=4)
        rows = (n + cols() - 1) // cols()
        canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), PAD + rows * (CELL_H + PAD) + PAD))
        parts = len(doc.groups()) if doc.pages else 0
        status.configure(text=T("{0} sayfa · {1} seçili", n, len(sel))
                         + (T(" · {0} ayrı PDF olarak kaydedilecek", parts) if parts > 1 else "")
                         + T("  ·  sürükle → sırala, sağ tık → işlemler"))

    # ------------------------------------------------------------------ işlemler
    def selected():
        return sorted(sel) if sel else []

    def after_change(new_sel=None):
        sel.clear()
        if new_sel:
            sel.update(i for i in new_sel if 0 <= i < len(doc.pages))
        redraw()

    def rotate(deg):
        if not sel:
            return
        doc.rotate(selected(), deg)
        after_change(selected())

    def delete():
        if sel:
            doc.delete(selected())
            after_change()

    def duplicate():
        if sel:
            s = selected()
            doc.duplicate(s)
            after_change([i + k + 1 for k, i in enumerate(s)])

    def split():
        if sel:
            doc.toggle_split(max(sel))
            redraw()

    def move_by(delta):
        if not sel:
            return
        s = selected()
        to = s[0] + delta if delta < 0 else s[-1] + 1 + delta
        after_change(doc.move(s, max(0, to)))

    def add_files():
        types = [(T("Desteklenen"), "*.pdf *.jpg *.jpeg *.png *.webp *.bmp *.tif *.tiff *.gif *.heic *.heif "
                                 "*.doc *.docx *.odt *.rtf *.txt *.xls *.xlsx *.ods *.csv *.ppt *.pptx *.odp"),
                 (T("Tümü"), "*.*")]
        names = filedialog.askopenfilenames(parent=root, title=T("Dosya ekle"), filetypes=types)
        if names:
            at = max(sel) + 1 if sel else None
            add(list(names), at)

    def add(paths, at=None):
        status.configure(text=T("Dosyalar hazırlanıyor…"))
        root.update_idletasks()
        before = len(doc.pages)
        errors = doc.add_files(paths, at, lambda m: (status.configure(text=m), root.update_idletasks()))
        added = len(doc.pages) - before
        start = before if at is None else at
        after_change(range(start, start + added))
        if errors:
            messagebox.showwarning(APP_NAME, "\n".join(errors), parent=root)

    def blank():
        at = max(sel) + 1 if sel else None
        doc.add_blank(at)
        after_change([at if at is not None else len(doc.pages) - 1])

    def undo():
        if doc.undo():
            after_change()

    def save():
        if not doc.pages:
            return
        folder, stem = doc.default_target()
        doc.image_mode = "a4" if img_mode.get().startswith("A4") else "original"
        status.configure(text=T("Kaydediliyor…"))
        root.update_idletasks()
        try:
            outs = doc.save(folder, stem, lambda m: (status.configure(text=m), root.update_idletasks()))
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=root)
            return
        saved.extend(outs)
        print(T("Tamamlandı:"), ", ".join(o.name for o in outs))
        sys.stdout.flush()
        redraw()
        status.configure(text=T("✔ Kaydedildi: {0}  →  {1}", ', '.join(o.name for o in outs), folder))
        open_btn.configure(state="normal")

    def open_folder():
        if saved:
            from .ui import open_folder as of
            of(saved[-1])

    # ------------------------------------------------------------------ araç çubuğu
    btn(T("↶ Geri al"), undo)
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=5)
    btn(T("＋ Dosya"), add_files)
    btn(T("＋ Boş"), blank)
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=5)
    btn(T("⟲ Sola"), lambda: rotate(270))
    btn(T("⟳ Sağa"), lambda: rotate(90))
    btn(T("Çoğalt"), duplicate)
    btn(T("Sil"), delete)
    btn(T("✂ Böl"), split)
    ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=5)
    btn("A→Z", lambda: (doc.sort_by_name(), after_change()))
    btn(T("Ters sıra"), lambda: (doc.reverse(), after_change()))
    # alt çubuk: durum solda; kaydetme ve ayar sağda (pencere daralsa da görünür kalır)
    save_btn = ttk.Button(foot, text=T("Kaydet"), command=save, style="Accent.TButton", width=12)
    save_btn.pack(side="right", padx=(6, 0))
    open_btn = ttk.Button(foot, text=T("Klasörü aç"), command=open_folder, state="disabled", style="Tool.TButton")
    open_btn.pack(side="right", padx=2)
    img_mode = tk.StringVar(value=T("A4 sayfaya sığdır"))
    ttk.Combobox(foot, textvariable=img_mode, state="readonly", width=19,
                 values=[T("A4 sayfaya sığdır"), T("Görselin kendi boyutu")]).pack(side="right", padx=4)
    ttk.Label(foot, text=T("Görseller:")).pack(side="right")
    status.pack(side="left", fill="x", expand=True)

    # ------------------------------------------------------------------ fare
    def on_press(e):
        canvas.focus_set()
        y = canvas.canvasy(e.y)
        i = index_at(e.x, y)
        drag.update(start=(e.x, y), active=False, gap=None)
        ctrl = bool(e.state & 0x0004)
        shift = bool(e.state & 0x0001)
        if i is None:
            if not ctrl and not shift:
                sel.clear()
            redraw()
            return
        if shift and anchor[0] is not None:
            a, b = sorted((anchor[0], i))
            sel.clear()
            sel.update(range(a, b + 1))
        elif ctrl:
            sel.symmetric_difference_update({i})
            anchor[0] = i
        else:
            if i not in sel:
                sel.clear()
                sel.add(i)
            anchor[0] = i
        redraw()

    def on_motion(e):
        if not drag["start"] or not sel:
            return
        y = canvas.canvasy(e.y)
        sx, sy = drag["start"]
        if not drag["active"] and abs(e.x - sx) + abs(y - sy) > 8:
            drag["active"] = True
        if drag["active"]:
            drag["gap"] = gap_at(e.x, y)
            # kenara yaklaşınca kaydır
            if e.y < 30:
                canvas.yview_scroll(-1, "units")
            elif e.y > canvas.winfo_height() - 30:
                canvas.yview_scroll(1, "units")
            redraw()

    def on_release(e):
        if drag["active"] and drag["gap"] is not None and sel:
            after_change(doc.move(selected(), drag["gap"]))
        else:
            y = canvas.canvasy(e.y)
            i = index_at(e.x, y)
            ctrl = bool(e.state & 0x0004)
            shift = bool(e.state & 0x0001)
            if i is not None and not ctrl and not shift and not drag["active"]:
                sel.clear()
                sel.add(i)
                redraw()
        drag.update(start=None, active=False, gap=None)

    menu = tk.Menu(root, tearoff=0)
    menu.add_command(label=T("Sağa döndür"), command=lambda: rotate(90))
    menu.add_command(label=T("Sola döndür"), command=lambda: rotate(270))
    menu.add_command(label=T("Ters çevir (180°)"), command=lambda: rotate(180))
    menu.add_separator()
    menu.add_command(label=T("Bu sayfadan sonra böl / bölmeyi kaldır"), command=split)
    menu.add_command(label=T("Sonrasına boş sayfa ekle"), command=blank)
    menu.add_command(label=T("Sonrasına dosya ekle…"), command=add_files)
    menu.add_separator()
    menu.add_command(label=T("Çoğalt"), command=duplicate)
    menu.add_command(label=T("Sil"), command=delete)

    def on_right(e):
        y = canvas.canvasy(e.y)
        i = index_at(e.x, y)
        if i is not None and i not in sel:
            sel.clear()
            sel.add(i)
            redraw()
        if sel:
            menu.tk_popup(e.x_root, e.y_root)

    def on_wheel(e):
        if getattr(e, "num", None) == 4:
            canvas.yview_scroll(-3, "units")
        elif getattr(e, "num", None) == 5:
            canvas.yview_scroll(3, "units")
        else:
            canvas.yview_scroll(int(-e.delta / 40), "units")

    # ------------------------------------------------------------------ büyük önizleme
    def preview(i):
        if i is None or not 0 <= i < len(doc.pages):
            return
        top = tk.Toplevel(root)
        top.title(T("{0} — Önizleme", APP_NAME))
        top.transient(root)
        theme.window(top)
        side = int(min(root.winfo_screenheight() * 0.82, 1100))
        cv = tk.Canvas(top, background="#3a3d42", highlightthickness=0, width=int(side * 0.78), height=side)
        cv.pack(fill="both", expand=True)
        info = ttk.Label(top, anchor="center", padding=4)
        info.pack(fill="x")
        cur = {"i": i, "img": None}

        def show():
            k = cur["i"]
            pg = doc.pages[k]
            try:
                png = doc.thumbnail_png(pg, side)
            except Exception as e:
                info.configure(text=T("Önizlenemedi: {0}", e))
                return
            cur["img"] = tk.PhotoImage(data=base64.b64encode(png))
            cv.delete("all")
            cv.create_image(cv.winfo_width() // 2 or side // 2, (cv.winfo_height() or side) // 2,
                            image=cur["img"], anchor="center")
            info.configure(text=T("{0} / {1}  ·  {2}   (←/→ gez · R/L döndür · Del sil · Esc kapat)", k + 1, len(doc.pages), pg.label))
            sel.clear()
            sel.add(k)
            redraw()

        def nav(d):
            cur["i"] = min(max(cur["i"] + d, 0), len(doc.pages) - 1)
            show()

        def rot(deg):
            doc.rotate([cur["i"]], deg)
            after_change({cur["i"]})
            show()

        def remove():
            doc.delete([cur["i"]])
            after_change()
            if not doc.pages:
                top.destroy()
                return
            cur["i"] = min(cur["i"], len(doc.pages) - 1)
            show()

        top.bind("<Left>", lambda e: nav(-1))
        top.bind("<Right>", lambda e: nav(1))
        top.bind("<Prior>", lambda e: nav(-1))
        top.bind("<Next>", lambda e: nav(1))
        top.bind("<r>", lambda e: rot(90))
        top.bind("<l>", lambda e: rot(270))
        top.bind("<Delete>", lambda e: remove())
        top.bind("<Escape>", lambda e: top.destroy())
        cv.bind("<Configure>", lambda e: show())
        cv.bind("<Double-Button-1>", lambda e: top.destroy())
        top.focus_set()

    def on_double(e):
        preview(index_at(e.x, canvas.canvasy(e.y)))

    menu.insert_command(0, label=T("Büyük önizle"), command=lambda: preview(min(sel) if sel else None))
    menu.insert_separator(1)

    # ------------------------------------------------------------------ Gezgin'den bırak
    if DND_FILES:
        def on_drop(e):
            files = []
            for f in map(Path, root.tk.splitlist(e.data)):
                if f.is_dir():                 # klasör bırakılırsa içindeki dosyalar ad sırasıyla
                    from .actions import sorted_by_name
                    files += sorted_by_name([x for x in f.rglob("*") if x.is_file()
                                             and not x.name.startswith((".", "~$"))])
                else:
                    files.append(f)
            y = canvas.canvasy(e.y_root - canvas.winfo_rooty())
            gap = gap_at(e.x_root - canvas.winfo_rootx(), y) if doc.pages else 0
            add(files, at=gap)
            return e.action

        canvas.drop_target_register(DND_FILES)
        canvas.dnd_bind("<<Drop>>", on_drop)

    canvas.bind("<Double-Button-1>", on_double)
    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_motion)
    canvas.bind("<ButtonRelease-1>", on_release)
    canvas.bind("<Button-3>", on_right)
    canvas.bind("<Configure>", redraw)
    canvas.bind_all("<MouseWheel>", on_wheel)
    canvas.bind_all("<Button-4>", on_wheel)
    canvas.bind_all("<Button-5>", on_wheel)

    # ------------------------------------------------------------------ klavye
    def key(seq, fn):
        root.bind(seq, lambda e: (fn(), "break")[1])

    key("<Control-a>", lambda: (sel.update(range(len(doc.pages))), redraw()))
    key("<Delete>", delete)
    key("<Control-r>", lambda: rotate(90))
    key("<Control-l>", lambda: rotate(270))
    key("<Control-d>", duplicate)
    key("<Control-b>", split)
    key("<Control-z>", undo)
    key("<Control-s>", save)
    key("<Control-Left>", lambda: move_by(-1))
    key("<Control-Right>", lambda: move_by(1))
    key("<Escape>", lambda: (sel.clear(), redraw()))

    # ------------------------------------------------------------------ başlat
    def start():
        add(paths)
        sel.clear()
        redraw()
    root.after(50, start)
    root.after(60, poll)
    auto = os.environ.get("RIFFLE_AUTOCLOSE_MS")        # testler için
    if auto:
        root.after(int(auto), root.destroy)
    root.mainloop()
    return saved
