"""Klasörde ara: PDF, Word, PowerPoint, Excel ve metin dosyalarının İÇİNDE kelime arar.

Türkçe büyük/küçük harf ve şapka/nokta farkı gözetmez: "ogrenci" yazınca "ÖĞRENCİ" de bulunur.
Taranmış (metni olmayan) PDF'ler ayrıca sayılır: önce "aranabilir yap (OCR)" gerekir.
"""

from __future__ import annotations

import os
import re
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from . import APP_NAME

EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".txt", ".md", ".csv"}
MAX_FILES = 5000

# her karakter tek karaktere: konumlar korunur, bağlam orijinal metinden kesilir
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
    """(konum, metin) parçaları. Metni çıkarılamayan dosyada boş döner."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        from .actions import pymupdf
        with pymupdf.open(path) as d:
            if d.needs_pass:
                return
            for i, pg in enumerate(d):
                yield f"s. {i + 1}", pg.get_text()
    elif ext == ".docx":
        import docx
        doc = docx.Document(str(path))
        for i, para in enumerate(doc.paragraphs, 1):
            if para.text.strip():
                yield f"paragraf {i}", para.text
        for t, table in enumerate(doc.tables, 1):
            for row in table.rows:
                yield f"tablo {t}", " | ".join(c.text for c in row.cells)
    elif ext == ".pptx":
        from pptx import Presentation
        prs = Presentation(str(path))
        for i, slide in enumerate(prs.slides, 1):
            parts = [sh.text_frame.text for sh in slide.shapes if getattr(sh, "has_text_frame", False)]
            yield f"slayt {i}", "\n".join(parts)
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
            yield f"satır {i}", line


def files_in(folder: Path) -> list[Path]:
    out = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in EXTS and not p.name.startswith(("~$", ".")):
            out.append(p)
            if len(out) >= MAX_FILES:
                break
    return out


def search(folder: Path, query: str, progress=lambda m: None, stop=lambda: False, limit_per_file: int = 50):
    """(sonuçlar, taranmış_pdf_sayısı, okunamayan dosyalar)"""
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
    root.title(f"{APP_NAME} — Klasörde ara — {folder.name}")
    root.geometry("980x600")
    root.minsize(640, 360)
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    top = ttk.Frame(root, padding=10)
    top.pack(fill="x")
    ttk.Label(top, text="Aranacak:").pack(side="left")
    qv = tk.StringVar(value=query)
    ent = ttk.Entry(top, textvariable=qv, width=40, font=("Segoe UI", 11))
    ent.pack(side="left", padx=6, fill="x", expand=True)
    btn = ttk.Button(top, text="Ara")
    btn.pack(side="left")
    status = ttk.Label(root, padding=(10, 0), foreground="#5b6270")
    status.pack(fill="x")
    frame = ttk.Frame(root, padding=10)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=("file", "where", "ctx"), show="headings")
    for col, text, w in (("file", "Dosya", 220), ("where", "Yer", 90), ("ctx", "Geçtiği yer", 600)):
        tree.heading(col, text=text)
        tree.column(col, width=w, anchor="w", stretch=(col == "ctx"))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")
    ttk.Label(root, text="Çift tık: dosyayı aç", padding=(10, 0, 10, 8), foreground="#5b6270").pack(anchor="w")

    state = {"gen": 0, "paths": {}}

    def go(*_):
        state["gen"] += 1
        gen = state["gen"]
        q = qv.get()
        tree.delete(*tree.get_children())
        state["paths"].clear()
        if len(q.strip()) < 2:
            status.configure(text="En az 2 harf yaz.")
            return
        status.configure(text="Aranıyor…")

        def work():
            res = search(folder, q, lambda m: root.after(0, lambda: gen == state["gen"] and
                                                         status.configure(text=f"Aranıyor… {m}")),
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
        msg = f"{len(hits)} sonuç, {files} dosyada." if hits else "Bulunamadı."
        if scanned:
            msg += f"  ·  {scanned} taranmış PDF'te metin yok (önce 'aranabilir yap')."
        if failed:
            msg += f"  ·  {len(failed)} dosya okunamadı."
        status.configure(text=msg)
        print(f"Arama: {msg}")
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
