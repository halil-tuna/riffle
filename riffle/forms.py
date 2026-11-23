"""PDF form doldurma: dilekçe, staj formu, başvuru formu gibi doldurulabilir PDF'ler.

Alanlar tek bir pencerede alt alta listelenir (etiket: formdaki açıklama, yoksa alanın solundaki yazı).
İsteğe bağlı "düzleştir": alanlar sayfaya yazı olarak işlenir, sonradan değiştirilemez (e-posta/basım için).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import APP_NAME
from .actions import ConversionError, open_pdf, pymupdf, unique_path

TEXT, CHECK, RADIO, COMBO, LIST = "text", "check", "radio", "combo", "list"


@dataclass
class Field:
    name: str
    kind: str
    label: str
    page: int
    value: object = ""
    options: list = field(default_factory=list)   # combo/list seçenekleri; radio: (on_state, etiket)
    multiline: bool = False
    readonly: bool = False
    editable_choice: bool = False
    y: float = 0.0
    x: float = 0.0


def _nearby_label(page, rect, words) -> str:
    """Alanın solundaki (aynı satırda) ya da hemen üstündeki yazı."""
    def same_line(w):
        cy = (w[1] + w[3]) / 2
        if abs(cy - (rect.y0 + rect.y1) / 2) < 8:
            return True
        return rect.height > 24 and rect.y0 - 4 <= cy <= rect.y0 + 16     # çok satırlı alan: ilk satır hizası

    left = [w for w in words if w[2] <= rect.x0 + 2 and same_line(w) and rect.x0 - w[2] < 220]
    if left:
        left.sort(key=lambda w: w[0])
        # alana en yakın kesintisiz kelime dizisi
        out, prev_x0 = [], None
        for w in sorted(left, key=lambda w: -w[2]):
            if prev_x0 is not None and prev_x0 - w[2] > 12:
                break
            out.insert(0, w[4])
            prev_x0 = w[0]
        return " ".join(out).rstrip(":").strip()
    above = [w for w in words if 0 <= rect.y0 - w[3] < 14 and w[0] < rect.x1 and w[2] > rect.x0 - 4]
    if above:
        above.sort(key=lambda w: w[0])
        return " ".join(w[4] for w in above).rstrip(":").strip()
    return ""


def read_fields(doc) -> list[Field]:
    out: list[Field] = []
    radios: dict = {}
    for pno, page in enumerate(doc):
        widgets = list(page.widgets() or [])
        if not widgets:
            continue
        words = page.get_text("words")
        for w in widgets:
            t = w.field_type
            if t in (pymupdf.PDF_WIDGET_TYPE_BUTTON, pymupdf.PDF_WIDGET_TYPE_SIGNATURE):
                continue
            label = (w.field_label or "").strip() or _nearby_label(page, w.rect, words) or w.field_name
            ro = bool(w.field_flags & pymupdf.PDF_FIELD_IS_READ_ONLY)
            if t == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                # radyo grubu: seçenek kimliği = gruptaki sıra numarası (bazı formlarda on_state yok)
                if w.field_name not in radios:
                    f = Field(w.field_name, RADIO, label, pno, "", readonly=ro, y=w.rect.y0, x=w.rect.x0)
                    radios[w.field_name] = f
                    out.append(f)
                f = radios[w.field_name]
                idx = str(len(f.options))
                on = w.on_state()
                right = sorted(x for x in words if x[0] >= w.rect.x1 - 1 and x[0] - w.rect.x1 < 80
                               and abs((x[1] + x[3]) / 2 - (w.rect.y0 + w.rect.y1) / 2) < 8)
                seq, end = [], w.rect.x1
                for x in right:                         # bitişik kelimeler; büyük boşlukta sonraki seçenek başlar
                    if x[0] - end > 8 or len(seq) >= 4:
                        break
                    seq.append(x[4])
                    end = x[2]
                opt_label = " ".join(seq) or (
                    on if isinstance(on, str) and on not in ("Off", "Yes", "On") else f"Seçenek {int(idx) + 1}")
                f.options.append((idx, opt_label))
                if w.field_value not in (None, "", "Off", False, "None"):
                    f.value = idx
                continue
            f = Field(w.field_name, TEXT, label, pno, w.field_value or "", readonly=ro, y=w.rect.y0, x=w.rect.x0)
            if t == pymupdf.PDF_WIDGET_TYPE_TEXT:
                f.multiline = bool(w.field_flags & pymupdf.PDF_TX_FIELD_IS_MULTILINE) or w.rect.height > 30
            elif t == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
                f.kind = CHECK
                f.value = w.field_value not in (None, "", "Off", False)
            elif t in (pymupdf.PDF_WIDGET_TYPE_COMBOBOX, pymupdf.PDF_WIDGET_TYPE_LISTBOX):
                f.kind = COMBO if t == pymupdf.PDF_WIDGET_TYPE_COMBOBOX else LIST
                f.options = [c if isinstance(c, str) else c[-1] for c in (w.choice_values or [])]
                f.editable_choice = bool(w.field_flags & pymupdf.PDF_CH_FIELD_IS_EDIT)
            out.append(f)
    out.sort(key=lambda f: (f.page, round(f.y / 6), f.x))
    return out


def fill(src: Path, values: dict, out: Path | None = None, flatten: bool = False) -> Path:
    """values: {alan adı: değer}. Metin: str · onay kutusu: bool · radyo: seçenek sıra no ("0", "1"…) · liste: str."""
    radio_idx: dict = {}
    with open_pdf(src) as doc:
        for page in doc:
            for w in list(page.widgets() or []):
                t = w.field_type
                if t == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                    idx = str(radio_idx.get(w.field_name, 0))
                    radio_idx[w.field_name] = int(idx) + 1
                if w.field_name not in values:
                    continue
                v = values[w.field_name]
                if t == pymupdf.PDF_WIDGET_TYPE_CHECKBOX:
                    w.field_value = w.on_state() if v else "Off"
                elif t == pymupdf.PDF_WIDGET_TYPE_RADIOBUTTON:
                    if str(v) == idx:
                        on = w.on_state()
                        w.field_value = on if isinstance(on, str) and on != "Off" else True
                    else:
                        w.field_value = "Off"
                elif t == pymupdf.PDF_WIDGET_TYPE_TEXT:
                    w.field_value = "" if v is None else str(v)
                else:
                    w.field_value = "" if v is None else str(v)
                w.update()
        if flatten:
            doc.bake(annots=False, widgets=True)       # alanlar sayfa içeriğine işlenir
        from .actions import fix_tounicode
        fix_tounicode(doc)
        out = out or unique_path(src.parent, f"{src.stem}_doldurulmus", ".pdf")
        doc.save(out, garbage=3, deflate=True)
    return out


def run(paths: list[Path], progress=print, **_) -> list[Path]:
    import tkinter as tk
    from tkinter import messagebox, ttk

    src = Path(paths[0])
    with open_pdf(src) as doc:
        fields = read_fields(doc)
        npages = len(doc)
    if not fields:
        r = tk.Tk()
        r.withdraw()
        messagebox.showinfo(APP_NAME, f"{src.name} içinde doldurulabilir alan yok.\n\n"
                                      "Yazı eklemek için: sağ tık → 'İmza / yazı / logo ekle…'", parent=r)
        r.destroy()
        return []

    root = tk.Tk()
    root.title(f"{APP_NAME} — Form doldur — {src.name}")
    root.geometry(f"720x{min(760, root.winfo_screenheight() - 80)}")
    root.minsize(480, 320)
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    head = ttk.Frame(root, padding=(12, 10, 12, 4))
    head.pack(fill="x")
    ttk.Label(head, text=f"{len(fields)} alan · {npages} sayfa", foreground="#5b6270").pack(side="left")

    outer = ttk.Frame(root)
    outer.pack(fill="both", expand=True)
    canvas = tk.Canvas(outer, highlightthickness=0, background=ttk.Style().lookup("TFrame", "background") or None)
    sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    inner = ttk.Frame(canvas, padding=(12, 4))
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))
    canvas.configure(yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.pack(side="left", fill="y")

    def wheel(e):
        canvas.yview_scroll(-1 if (getattr(e, "delta", 0) > 0 or getattr(e, "num", 0) == 4) else 1, "units")
    for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
        root.bind_all(seq, wheel)

    getters: dict = {}
    inner.columnconfigure(1, weight=1)
    row = 0
    last_page = None
    first_input = None
    for f in fields:
        if f.page != last_page and npages > 1:
            ttk.Label(inner, text=f"Sayfa {f.page + 1}", font=("Segoe UI", 10, "bold")).grid(
                row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
            row += 1
            last_page = f.page
        ttk.Label(inner, text=f.label, wraplength=230, justify="left").grid(
            row=row, column=0, sticky="nw", padx=(0, 10), pady=4)
        state = "disabled" if f.readonly else "normal"
        if f.kind == TEXT and f.multiline:
            t = tk.Text(inner, height=4, wrap="word", font=("Segoe UI", 10), relief="solid", borderwidth=1)
            t.insert("1.0", f.value or "")
            t.configure(state=state)
            t.grid(row=row, column=1, sticky="ew", pady=4)
            getters[f.name] = lambda t=t: t.get("1.0", "end-1c")
            widget = t
        elif f.kind == TEXT:
            v = tk.StringVar(value=f.value or "")
            e = ttk.Entry(inner, textvariable=v, state=state)
            e.grid(row=row, column=1, sticky="ew", pady=4)
            getters[f.name] = v.get
            widget = e
        elif f.kind == CHECK:
            v = tk.BooleanVar(value=bool(f.value))
            c = ttk.Checkbutton(inner, variable=v, state=state)
            c.grid(row=row, column=1, sticky="w", pady=4)
            getters[f.name] = v.get
            widget = c
        elif f.kind == RADIO:
            v = tk.StringVar(value=f.value or "")
            box = ttk.Frame(inner)
            box.grid(row=row, column=1, sticky="w", pady=4)
            for on, lab in f.options:
                ttk.Radiobutton(box, text=lab, value=on, variable=v, state=state).pack(side="left", padx=(0, 12))
            getters[f.name] = v.get
            widget = box
        else:
            v = tk.StringVar(value=f.value or "")
            cb = ttk.Combobox(inner, textvariable=v, values=f.options,
                              state=("normal" if f.editable_choice else "readonly") if not f.readonly else "disabled")
            cb.grid(row=row, column=1, sticky="ew", pady=4)
            getters[f.name] = v.get
            widget = cb
        if first_input is None and not f.readonly and f.kind in (TEXT, COMBO, LIST):
            first_input = widget
        row += 1

    foot = ttk.Frame(root, padding=10)
    foot.pack(fill="x")
    flat = tk.BooleanVar(value=False)
    ttk.Checkbutton(foot, text="Düzleştir (alanlar düz yazı olsun, sonradan değiştirilemesin)",
                    variable=flat).pack(side="left")
    saved: list[Path] = []
    status = ttk.Label(root, padding=(12, 0, 12, 8), foreground="#2c7a3f")
    status.pack(fill="x")

    def save(*_):
        values = {name: g() for name, g in getters.items()}
        try:
            out = fill(src, values, flatten=flat.get())
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e), parent=root)
            return
        saved.append(out)
        status.configure(text=f"✔ Kaydedildi: {out.name}")
        print("Tamamlandı:", out.name)
        sys.stdout.flush()

    ttk.Button(foot, text="Kaydet", command=save).pack(side="right")
    root.bind("<Control-s>", lambda e: (save(), "break")[1])
    if first_input is not None:
        first_input.focus_set()
    auto = os.environ.get("RIFFLE_AUTOCLOSE_MS")
    if auto:
        root.after(int(auto), root.destroy)
    root.mainloop()
    return saved
