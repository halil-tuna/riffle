"""Search in folder: searches for words INSIDE PDF, Word, PowerPoint, Excel and text files.

Ignores Turkish case and circumflex/dot differences: typing "ogrenci" also finds "ÖĞRENCİ".
Scanned PDFs (without text) are counted separately: they need "make searchable (OCR)" first.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME
from .i18n import T

EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".csv"}
MAX_FILES = 5000

# every character maps to one character: positions are kept, context is cut from the original text
_FOLD = str.maketrans({"İ": "i", "I": "i", "ı": "i", "Ç": "c", "ç": "c", "Ğ": "g", "ğ": "g", "Ö": "o", "ö": "o",
                       "Ş": "s", "ş": "s", "Ü": "u", "ü": "u", "Â": "a", "â": "a", "Î": "i", "î": "i",
                       "Û": "u", "û": "u", "\n": " ", "\t": " ", "­": "-"})


def fold(t: str) -> str:
    t = t.translate(_FOLD)
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in t)


@dataclass
class Hit:
    path: Path
    where: str           # "s. 3", "slayt 2", "Sayfa1!B4", "paragraf 12"
    context: str


def _texts(path: Path):
    """(position, text) pieces. Empty for a file whose text can't be extracted."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        from .actions import pymupdf
        with pymupdf.open(path) as d:
            if d.needs_pass:
                return
            for i, pg in enumerate(d):
                yield T("s. {0}", i + 1), pg.get_text()
    elif ext == ".docx":
        import docx
        doc = docx.Document(str(path))
        for i, para in enumerate(doc.paragraphs, 1):
            if para.text.strip():
                yield T("paragraf {0}", i), para.text
        for t, table in enumerate(doc.tables, 1):
            for row in table.rows:
                yield T("tablo {0}", t), " | ".join(c.text for c in row.cells)
    elif ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(path))
        for i, slide in enumerate(prs.slides, 1):
            parts = [sh.text_frame.text for sh in slide.shapes if getattr(sh, "has_text_frame", False)]
            yield T("slayt {0}", i), "\n".join(parts)
    elif ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for row in ws.iter_rows():
                for c in row:
                    if c.value is not None and isinstance(c.value, str):
                        yield f"{ws.title}!{c.coordinate}", c.value
        wb.close()
    else:
        for enc in ("utf-8", "cp1254", "latin-1"):
            try:
                text = path.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            return
        for i, line in enumerate(text.splitlines(), 1):
            yield T("satır {0}", i), line


def files_in(folder: Path) -> list[Path]:
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith(("~$", ".")):
            out.append(p)
            if len(out) >= MAX_FILES:
                break
    return out


def search(folder: Path, query: str, progress=lambda m: None, stop=lambda: False, limit_per_file: int = 50):
    """(results, number of scanned PDFs, unreadable files)"""
    q = fold(query.strip())
    if not q:
        return [], 0, []
    hits: list[Hit] = []
    scanned, failed = 0, []
    files = files_in(folder)
    for n, path in enumerate(files, 1):
        if stop():
            break
        progress(f"{n}/{len(files)} · {path.name}")
        try:
            got = 0
            had_text = False
            for where, text in _texts(path):
                if text.strip():
                    had_text = True
                f = fold(text)
                start = 0
                while True:
                    i = f.find(q, start)
                    if i < 0 or got >= limit_per_file:
                        break
                    a, b = max(0, i - 45), min(len(text), i + len(q) + 45)
                    ctx = re.sub(r"\s+", " ", text[a:b]).strip()
                    hits.append(Hit(path, where, ("…" if a else "") + ctx + ("…" if b < len(text) else "")))
                    got += 1
                    start = i + len(q)
            if path.suffix.lower() == ".pdf" and not had_text:
                scanned += 1
        except Exception as e:
            failed.append(f"{path.name}: {type(e).__name__}")
    return hits, scanned, failed


def open_file(path: Path) -> None:
    import subprocess
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


def run(paths: list[Path], progress=print, query: str = "", **_) -> list[Path]:
    import tkinter as tk
    from tkinter import ttk
    folder = Path(paths[0])
    if not folder.is_dir():
        folder = folder.parent

    root = tk.Tk()
    root.title(T("{0} — Klasörde ara — {1}", APP_NAME, folder.name))
    root.geometry("980x600")
    root.minsize(640, 360)
    from . import theme
    theme.setup(root)
    head = ttk.Frame(root, padding=(20, 16, 20, 0))
    head.pack(fill="x")
    ttk.Label(head, text=T("Klasörde ara"), style="Heading.TLabel").pack(anchor="w")
    ttk.Label(head, text=T("{0} klasörü ve alt klasörleri · PDF, Word, PowerPoint, Excel, metin; Türkçe harf ve büyük/küçük farkı gözetmez", folder.name), style="Muted.TLabel").pack(anchor="w")
    top = ttk.Frame(root, padding=(20, 12, 20, 6))
    top.pack(fill="x")
    qv = tk.StringVar(value=query)
    ent = ttk.Entry(top, textvariable=qv, width=40)
    ent.pack(side="left", fill="x", expand=True)
    btn = ttk.Button(top, text=T("Ara"), style="Accent.TButton", width=10)
    btn.pack(side="left", padx=(8, 0))
    status = ttk.Label(root, padding=(20, 0), style="Muted.TLabel")
    status.pack(fill="x")
    ttk.Style(root).configure("Results.Treeview", rowheight=28)
    frame = ttk.Frame(root, padding=(20, 8, 20, 4))
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=("file", "where", "ctx"), show="headings", style="Results.Treeview")
    for col, text, w in (("file", T("Dosya"), 220), ("where", T("Yer"), 90), ("ctx", T("Geçtiği yer"), 600)):
        tree.heading(col, text=text)
        tree.column(col, width=w, anchor="w", stretch=(col == "ctx"))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")
    ttk.Label(root, text=T("Çift tık ya da Enter: dosyayı aç · Esc: kapat"), padding=(20, 0, 20, 12),
              style="Muted.TLabel").pack(anchor="w")

    state = {"gen": 0, "paths": {}}

    def go(*_):
        state["gen"] += 1
        gen = state["gen"]
        q = qv.get()
        tree.delete(*tree.get_children())
        state["paths"].clear()
        if len(q.strip()) < 2:
            status.configure(text=T("En az 2 harf yaz."))
            return
        status.configure(text=T("Aranıyor…"))

        def work():
            res = search(folder, q, lambda m: root.after(0, lambda: gen == state["gen"] and
                                                         status.configure(text=T("Aranıyor… {0}", m))),
                         stop=lambda: gen != state["gen"])
            root.after(0, lambda: show(gen, *res))

        threading.Thread(target=work, daemon=True).start()

    def show(gen, hits, scanned, failed):
        if gen != state["gen"]:
            return
        for h in hits:
            iid = tree.insert("", "end", values=(h.path.relative_to(folder), h.where, h.context))
            state["paths"][iid] = h.path
        files = len({h.path for h in hits})
        msg = T("{0} sonuç, {1} dosyada.", len(hits), files) if hits else T("Bulunamadı.")
        if scanned:
            msg += T("  ·  {0} taranmış PDF'te metin yok (önce 'aranabilir yap').", scanned)
        if failed:
            msg += T("  ·  {0} dosya okunamadı.", len(failed))
        status.configure(text=msg)
        print(T("Arama: {0}", msg))
        sys.stdout.flush()

    def open_sel(_e=None):
        for iid in tree.selection():
            open_file(state["paths"][iid])

    btn.configure(command=go)
    ent.bind("<Return>", go)
    tree.bind("<Double-Button-1>", open_sel)
    tree.bind("<Return>", open_sel)
    root.bind("<Escape>", lambda e: root.destroy())
    ent.focus_set()
    if query:
        root.after(100, go)
    auto = os.environ.get("RIFFLE_AUTOCLOSE_MS")
    if auto:
        root.after(int(auto), root.destroy)
    root.mainloop()
    return []
