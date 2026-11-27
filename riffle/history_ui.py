"""The History tab of the Riffle window: what was converted when, from which files to which.

Built by settings.run() inside a guard: if anything here fails, the window opens without this tab.
Reading the history (history.query) takes milliseconds, so it is done on the window's own thread.
"""

from __future__ import annotations

import math
import os
import queue
import subprocess
import sys
import threading
from collections import OrderedDict
from datetime import date, datetime, timedelta
from pathlib import Path

from .i18n import T

STATUS_ICON = {"ok": "✓", "warn": "⚠", "error": "✗", "cancelled": "⊘"}
NOTE_ICON = {"info": "ℹ", "warn": "⚠", "error": "✗"}
LIMIT = 1000


def _months() -> tuple:
    return (T("Ocak"), T("Şubat"), T("Mart"), T("Nisan"), T("Mayıs"), T("Haziran"), T("Temmuz"), T("Ağustos"),
            T("Eylül"), T("Ekim"), T("Kasım"), T("Aralık"))


def _status_names() -> dict:
    return {"ok": T("Başarılı"), "warn": T("Uyarılı"), "error": T("Hata"), "cancelled": T("İptal edildi")}


# ------------------------------------------------------------------ small helpers (tested without a window)

def open_path(path) -> None:
    """Opens a file with its default program (tests replace this)."""
    p = str(path)
    if sys.platform == "win32":
        os.startfile(p)                                    # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def riffle_command() -> list[str]:
    """How to start Riffle again: the .exe itself, or python + run.pyw for a source install."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [sys.executable, str(Path(__file__).resolve().parent.parent / "run.pyw")]


def launch(*args: str) -> None:
    """Starts Riffle in a new process (tests replace this)."""
    kw = {"creationflags": getattr(subprocess, "DETACHED_PROCESS", 0)} if sys.platform == "win32" else \
        {"start_new_session": True}
    subprocess.Popen(riffle_command() + list(args), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, **kw)


def known_action(action_id: str) -> bool:
    try:
        from .actions import BY_ID
        return action_id in BY_ID
    except Exception:
        return False


def is_local(path: str) -> bool:
    """Network drives are not checked for existence (a slow or offline share would freeze the list)."""
    if sys.platform != "win32":
        return True
    if path.startswith(("\\\\", "//")):
        return False
    try:
        import ctypes
        drive = os.path.splitdrive(path)[0]
        return not drive or ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") != 4     # DRIVE_REMOTE
    except Exception:
        return True


def exists(path: str) -> bool:
    if not is_local(path):
        return True
    try:
        return os.path.exists(path)
    except (OSError, ValueError):
        return False


def local_time(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp).astimezone()
    except (TypeError, ValueError):
        return None


def day_label(d: date, today: date | None = None) -> str:
    today = today or date.today()
    if d == today:
        return T("Bugün")
    if d == today - timedelta(days=1):
        return T("Dün")
    return f"{d.day} {_months()[d.month - 1]} {d.year}"


def duration_text(ms) -> str:
    if ms is None:
        return ""
    s = int(round(ms / 1000))
    if ms < 1000:
        return T("<1 sn")
    if s < 60:
        return T("{0} sn", s)
    return T("{0} dk {1} sn", s // 60, s % 60)


def action_label(action_id: str) -> str:
    try:
        from .actions import BY_ID
        a = BY_ID.get(action_id)
        return T(a.label).rstrip("…") if a else action_id
    except Exception:
        return action_id


def files_text(job: dict, present=exists) -> str:
    ins, outs = job.get("inputs", []), job.get("outputs", [])
    left = Path(ins[0]).name if len(ins) == 1 else T("{0} dosya", len(ins)) if ins else "—"
    if not outs:
        right = "—"
    elif len(outs) == 1:
        right = Path(outs[0]).name if present(outs[0]) else T("(bulunamadı)") + " " + Path(outs[0]).name
    else:
        right = T("{0} dosya", len(outs))
    return f"{left} → {right}"


def first_output(job: dict, present=exists) -> str | None:
    return next((o for o in job.get("outputs", []) if present(o)), None)


# ------------------------------------------------------------------ the tab

class HistoryTab:
    def __init__(self, nb, root, pal):
        import tkinter as tk
        from tkinter import font, ttk
        from . import history
        self.tk, self.ttk = tk, ttk
        self.history = history
        self.root, self.pal = root, pal
        self.jobs: dict[int, dict] = {}
        self._present: dict[str, bool] = {}
        self._after = None
        self._focus_after = None
        try:
            history.prune()                                # old records go when the window opens too
        except Exception:
            pass

        f = self.frame = ttk.Frame(nb, padding=(16, 12))
        bar = ttk.Frame(f)
        bar.pack(fill="x")
        ttk.Label(bar, text=T("Ara:")).pack(side="left")
        self.search = tk.StringVar()
        self.search_entry = ttk.Entry(bar, textvariable=self.search, width=16)
        self.search_entry.pack(side="left", padx=(6, 14))
        self.search.trace_add("write", lambda *_: self._later())
        ttk.Label(bar, text=T("İşlem:")).pack(side="left")
        self.action = tk.StringVar(value=T("Tümü"))
        self.action_box = ttk.Combobox(bar, textvariable=self.action, state="readonly", width=17)
        self.action_box.pack(side="left", padx=(6, 14))
        self.action_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(bar, text=T("Durum:")).pack(side="left")
        self.status = tk.StringVar(value=T("Tümü"))
        self.status_box = ttk.Combobox(bar, textvariable=self.status, state="readonly", width=10,
                                       values=[T("Tümü")] + list(_status_names().values()))
        self.status_box.pack(side="left", padx=(6, 0))
        self.status_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.view = tk.StringVar(value=_saved_view())
        for val, text in (("icons", T("Simgeler")), ("list", T("Liste"))):     # (packed right to left)
            try:                                                                # selected = accent (sv-ttk)
                rb = ttk.Radiobutton(bar, text=text, value=val, variable=self.view, style="Toggle.TButton",
                                     command=self._switch_view)
            except tk.TclError:
                rb = ttk.Radiobutton(bar, text=text, value=val, variable=self.view, style="Toolbutton",
                                     command=self._switch_view)
            rb.pack(side="right", padx=(4, 0))

        self.banner = ttk.Label(f, style="Muted.TLabel")       # shown only when there is something to say

        mid = self.mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, pady=(10, 8))
        cols = ("time", "action", "files", "status", "duration")
        self.list_frame = ttk.Frame(mid)
        self.tree = ttk.Treeview(self.list_frame, columns=cols, show="tree headings", selectmode="browse")
        self.tree.column("#0", width=16, minwidth=16, stretch=False)
        for c, title, w, anchor, stretch in (("time", T("Saat"), 60, "w", False),
                                             ("action", T("İşlem"), 240, "w", False),
                                             ("files", T("Dosyalar"), 330, "w", True),
                                             ("status", "", 36, "center", False),
                                             ("duration", T("Süre"), 92, "e", False)):
            self.tree.heading(c, text=title, anchor=anchor)
            self.tree.column(c, width=w, minwidth=30, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(self.list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        try:
            base = ttk.Style(root).lookup("Treeview", "font") or "TkDefaultFont"
            bold = font.nametofont(base).copy() if isinstance(base, str) and base in font.names(root) \
                else font.nametofont("TkDefaultFont").copy()
            bold.configure(weight="bold")
            self._bold = bold
            self.tree.tag_configure("group", font=bold)
        except Exception:
            pass
        self.tree.tag_configure("missing", foreground=pal.muted)
        self.icons = IconView(self, mid)
        self._show_view()
        self.empty = ttk.Label(mid, style="Muted.TLabel", justify="center", wraplength=460)

        self.detail = tk.Text(f, height=7, wrap="word", cursor="arrow")
        from . import theme
        theme.style_text(self.detail, pal)
        try:
            body = "SunValleyBodyFont" if "SunValleyBodyFont" in font.names(root) else "TkDefaultFont"
            self.detail.configure(font=body)
            f_ = font.nametofont(body)
            self.detail.configure(tabs=(max(f_.measure(T("Kaynak:")), f_.measure(T("Çıktı:"))) + 8,))
        except Exception:
            pass
        self.detail.pack(fill="x")
        self.detail.tag_configure("muted", foreground=pal.muted)
        self.detail.tag_configure("link", foreground=pal.accent, underline=True)
        self.detail.tag_configure("bad", foreground=pal.danger)
        self.detail.configure(state="disabled")

        btns = ttk.Frame(f)
        btns.pack(fill="x", pady=(8, 0))
        self.b_open = ttk.Button(btns, text=T("Aç"), command=self.open_selected)
        self.b_show = ttk.Button(btns, text=T("Klasörde göster"), command=self.show_selected)
        self.b_again = ttk.Button(btns, text=T("Tekrar dönüştür"), command=self.again_selected)
        self.b_remove = ttk.Button(btns, text=T("Listeden kaldır"), command=self.remove_selected)
        for b in (self.b_open, self.b_show, self.b_again, self.b_remove):
            b.pack(side="left", padx=(0, 8))
        ttk.Button(btns, text=T("Geçmişi temizle"), command=self.clear_all).pack(side="right")

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label=T("Aç"), command=self.open_selected)
        self.menu.add_command(label=T("Klasörde göster"), command=self.show_selected)
        self.menu.add_command(label=T("Tekrar dönüştür"), command=self.again_selected)
        self.menu.add_separator()
        self.menu.add_command(label=T("Listeden kaldır"), command=self.remove_selected)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._double)
        self.tree.bind("<Return>", lambda e: self.open_selected())
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        self.tree.bind("<Button-3>", self._context)
        root.bind("<F5>", lambda e: self.refresh(), add="+")
        self._away = False
        root.bind("<FocusIn>", self._focus, add="+")
        root.bind("<FocusOut>", self._blur, add="+")
        self.refresh()

    # ---------------------------------------------------------- reading

    def _later(self) -> None:
        if self._after:
            self.root.after_cancel(self._after)
        self._after = self.root.after(250, self.refresh)

    def _focus(self, e) -> None:
        """Another process may have written meanwhile: refresh when the window gets the focus back from
        another program (not when the focus moves between the window's own widgets)."""
        if not self._away or self._focus_after:
            return
        self._away = False
        self._focus_after = self.root.after(150, self._focus_refresh)

    def _blur(self, e) -> None:
        def check():
            try:
                if self.root.focus_get() is None:
                    self._away = True
            except Exception:                     # (a menu/combobox popup has no focus widget name)
                pass
        self.root.after(80, check)

    def _focus_refresh(self) -> None:
        self._focus_after = None
        self.refresh()

    def _present_now(self, path: str) -> bool:
        if path not in self._present:
            self._present[path] = exists(path)
        return self._present[path]

    def _filters(self) -> tuple[str | None, str | None]:
        action = self._action_ids.get(self.action.get()) if hasattr(self, "_action_ids") else None
        status = {v: k for k, v in _status_names().items()}.get(self.status.get())
        return action, status

    def refresh(self) -> None:
        self._after = None
        h = self.history
        on = h.enabled()
        self._present = {}
        try:
            action, status = self._filters()
            jobs = h.query(search=self.search.get().strip(), action=action, status=status, limit=LIMIT)
            everything = h.query(limit=LIMIT) if (action or status or self.search.get().strip()) else jobs
            error = None
        except Exception as e:                                  # unreadable database: say so, don't crash
            jobs, everything, error = [], [], e
        seen = sorted({j["action_id"] for j in everything}, key=action_label)
        self._action_ids = {T("Tümü"): None, **{action_label(a): a for a in seen}}
        self.action_box.configure(values=list(self._action_ids))
        if self.action.get() not in self._action_ids:
            self.action.set(T("Tümü"))

        keep = self.selected_id()
        self.tree.delete(*self.tree.get_children())
        self.jobs = {j["id"]: j for j in jobs}
        groups: dict[str, str] = {}
        today = date.today()
        for j in jobs:
            t = local_time(j["started_at"])
            label = day_label(t.date(), today) if t else "?"
            if label not in groups:
                groups[label] = self.tree.insert("", "end", iid=f"g{len(groups)}", text="", open=True,
                                                 values=("", label, "", "", ""), tags=("group",))
            out_ok = not j["outputs"] or any(self._present_now(o) for o in j["outputs"])
            tags = () if out_ok else ("missing",)
            self.tree.insert(groups[label], "end", iid=f"j{j['id']}", text="", tags=tags,
                             values=(t.strftime("%H:%M") if t else "", action_label(j["action_id"]),
                                     files_text(j, self._present_now), STATUS_ICON.get(j["status"], ""),
                                     duration_text(j["duration_ms"])))
        note = (T("Geçmiş kapalı. Açmak için: Genel → Gizlilik.") if not on else
                T("Geçmiş okunamadı: {0}", error) if error is not None else "")
        self.banner.configure(text=note)
        if note and not self.banner.winfo_manager():
            self.banner.pack(anchor="w", pady=(10, 0), before=self.mid)
        elif not note:
            self.banner.pack_forget()
        if jobs:
            self.empty.place_forget()
        else:
            filtered = bool(self.search.get().strip() or action or status)
            self.empty.configure(text=T("Eşleşen işlem yok.") if filtered else
                                 T("Henüz bir işlem yok. Bir dosyaya sağ tıklayıp Riffle ile dönüştürdüğünde "
                                   "burada görünür.") if on else "")
            self.empty.place(relx=0.5, rely=0.45, anchor="center")
        if keep is not None and self.tree.exists(f"j{keep}"):
            self.tree.selection_set(f"j{keep}")
            self.tree.see(f"j{keep}")
        self.icons.set_jobs([(self.tree.item(g, "values")[1], [self.jobs[int(c[1:])] for c in self.tree.get_children(g)])
                             for g in self.tree.get_children()])
        self.empty.lift()
        self.show_details()

    # ---------------------------------------------------------- selection

    def selected_id(self) -> int | None:
        sel = self.tree.selection()
        if sel and sel[0].startswith("j"):
            return int(sel[0][1:])
        return None

    def selected(self) -> dict | None:
        jid = self.selected_id()
        return self.jobs.get(jid) if jid is not None else None

    def _on_select(self, _e=None) -> None:
        sel = self.tree.selection()
        if sel and sel[0].startswith("g"):                  # day headings are not selectable
            self.tree.selection_remove(sel[0])
            return
        self.show_details()

    def select(self, job_id: int) -> None:
        self.tree.selection_set(f"j{job_id}")
        self.tree.focus(f"j{job_id}")
        self.show_details()

    def _show_view(self) -> None:
        if self.view.get() == "icons":
            self.list_frame.pack_forget()
            self.icons.frame.pack(fill="both", expand=True)
        else:
            self.icons.frame.pack_forget()
            self.list_frame.pack(fill="both", expand=True)

    def _switch_view(self) -> None:
        self._show_view()
        self.icons.layout()
        jid = self.selected_id()
        if jid is not None:
            self.icons.see(jid)
            if self.view.get() == "list":
                self.tree.see(f"j{jid}")
        _save_view(self.view.get())

    def show_details(self) -> None:
        j = self.selected()
        try:
            self.icons.highlight(j["id"] if j else None)
        except Exception:
            pass
        d = self.detail
        d.configure(state="normal")
        d.delete("1.0", "end")
        for b in (self.b_open, self.b_show, self.b_again, self.b_remove):
            b.state(["!disabled"] if j else ["disabled"])
        if j:
            t = local_time(j["started_at"])
            when = f"{day_label(t.date())} {t.strftime('%H:%M')}" if t else ""
            parts = [action_label(j["action_id"]), when, f"Riffle {j['version']}", duration_text(j["duration_ms"])]
            d.insert("end", " · ".join(p for p in parts if p) + "\n", "muted")
            for level, text in j["notes"]:
                d.insert("end", f"{NOTE_ICON.get(level, 'ℹ')} {text}\n", "bad" if level == "error" else ())
            for role, paths in ((T("Kaynak:"), j["inputs"]), (T("Çıktı:"), j["outputs"])):
                for i, p in enumerate(paths):
                    d.insert("end", (role if i == 0 else "") + "\t", "muted")
                    if self._present_now(p):
                        tag = f"file{abs(hash((role, p)))}"
                        d.insert("end", p, ("link", tag))
                        d.tag_bind(tag, "<Button-1>", lambda e, p=p: self._open(p))
                        d.tag_bind(tag, "<Enter>", lambda e: d.configure(cursor="hand2"))
                        d.tag_bind(tag, "<Leave>", lambda e: d.configure(cursor="arrow"))
                    else:
                        d.insert("end", f"{p}  {T('(bulunamadı)')}", "muted")
                    d.insert("end", "\n")
        if not j:
            d.insert("end", T("Bir işlem seç: ayrıntılar burada görünür. Çift tık çıktı dosyasını açar; listeden "
                              "kaldırmak dosyaya dokunmaz."), "muted")
        d.configure(state="disabled")
        if j:
            self.b_open.state(["!disabled"] if first_output(j, self._present_now) else ["disabled"])
            self.b_again.state(["!disabled"] if j["inputs"] and known_action(j["action_id"]) else ["disabled"])

    # ---------------------------------------------------------- actions

    def _open(self, path: str) -> None:
        try:
            open_path(path)
        except Exception as e:
            from tkinter import messagebox
            messagebox.showerror("Riffle", T("Açılamadı: {0}", e), parent=self.root)

    def _double(self, e):
        row = self.tree.identify_row(e.y)
        if row.startswith("j"):
            self.tree.selection_set(row)
            self.open_selected()
            return "break"

    def _context(self, e):
        row = self.tree.identify_row(e.y)
        if row.startswith("j"):
            self.tree.selection_set(row)
            self.tree.focus(row)
            try:
                self.menu.tk_popup(e.x_root, e.y_root)
            finally:
                self.menu.grab_release()

    def open_selected(self) -> None:
        j = self.selected()
        if not j:
            return
        out = first_output(j)
        if out is None:
            from tkinter import messagebox
            messagebox.showinfo("Riffle", T("Çıktı dosyası bulunamadı (taşınmış ya da silinmiş olabilir)."),
                                parent=self.root)
            return
        self._open(out)

    def show_selected(self) -> None:
        j = self.selected()
        if not j:
            return
        target = first_output(j) or next((p for p in j["outputs"] + j["inputs"] if exists(p)), None)
        if target is None:
            target = next(iter(j["outputs"] + j["inputs"]), None)
            target = str(Path(target).parent) if target else None
        if target:
            from . import ui
            ui.open_folder(Path(target))

    def again_selected(self) -> None:
        """Same action on the same files, in a new Riffle process (it asks again for anything it needs:
        passwords always, earlier answers such as a page range are offered as the default)."""
        from tkinter import messagebox
        j = self.selected()
        if not j or not j["inputs"]:
            return
        if not known_action(j["action_id"]):
            messagebox.showinfo("Riffle", T("Bu işlem bu Riffle sürümünde yok."), parent=self.root)
            return
        missing = [p for p in j["inputs"] if not exists(p)]
        if missing:
            names = "\n".join(Path(p).name for p in missing[:10]) + ("\n…" if len(missing) > 10 else "")
            messagebox.showinfo("Riffle", T("Kaynak dosya bulunamadı (taşınmış ya da silinmiş olabilir):\n{0}",
                                            names), parent=self.root)
            return
        try:
            launch("--again", str(j["id"]))
        except Exception as e:
            messagebox.showerror("Riffle", T("Açılamadı: {0}", e), parent=self.root)

    def remove_selected(self) -> None:
        jid = self.selected_id()
        if jid is None:
            return
        nxt = self.tree.next(f"j{jid}") or self.tree.prev(f"j{jid}")
        try:
            self.history.remove(jid)
        except Exception:
            pass
        self.refresh()
        if nxt and self.tree.exists(nxt):
            self.tree.selection_set(nxt)
            self.tree.focus(nxt)

    def clear_all(self, confirm=None) -> None:
        from tkinter import messagebox
        ask = confirm or (lambda: messagebox.askyesno(
            "Riffle", T("Bütün geçmiş kayıtları silinsin mi? Dosyaların kendisine dokunulmaz."), parent=self.root))
        if not ask():
            return
        try:
            self.history.clear()
        except Exception:
            pass
        self.refresh()


def _saved_view() -> str:
    try:
        from . import settings
        v = settings.load().get("history_view", "list")
        return v if v in ("list", "icons") else "list"
    except Exception:
        return "list"


def _save_view(view: str) -> None:
    try:
        from . import settings
        s = settings.load()
        s["history_view"] = view
        settings.save(s)
    except Exception:
        pass


# ------------------------------------------------------------------ icon view

CARD_W, CARD_H, THUMB, HEAD_H, PAD = 152, 204, 120, 34, 12
CACHE = 400                      # previews kept in memory (never on disk)


class IconView:
    """Cards with a preview of each job's output, grouped by day, like File Explorer's large icons.

    The selection lives in the (hidden) list's tree, so every button, the details box and the right-click menu
    work the same in both views. Previews are made on a background thread, only for the cards on screen,
    and are kept in memory only (thumbs.py)."""

    def __init__(self, tab: HistoryTab, parent):
        tk, ttk = tab.tk, tab.ttk
        self.tab, self.pal = tab, tab.pal
        self.frame = ttk.Frame(parent)
        self.canvas = tk.Canvas(self.frame, highlightthickness=0, background=self.pal.surface, takefocus=1,
                                yscrollincrement=CARD_H // 4)
        self.sb = ttk.Scrollbar(self.frame, orient="vertical", command=self._yview)
        self.canvas.configure(yscrollcommand=self._yset)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.sb.pack(side="left", fill="y")
        self.groups: list = []
        self.order: list[int] = []
        self.cards: dict[int, dict] = {}
        self.cols = 1
        self.sel: int | None = None
        self.images: OrderedDict = OrderedDict()   # key → PhotoImage (memory only)
        self.icons: dict = {}                      # (extension, present) → PhotoImage of the drawn type icon
        self.waiting: list = []                    # [(key, path)] still to make, the visible ones
        self.busy: set = set()
        self.lock = threading.Lock()
        self.done: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self._layout_after = None
        self._poll_after = None
        try:
            from tkinter import font
            base = font.nametofont("SunValleyBodyFont") if "SunValleyBodyFont" in font.names(tab.root) \
                else font.nametofont("TkDefaultFont")
            self.f_name = base.copy()
            self.f_meta = base.copy()
            size = int(base.cget("size"))                   # negative = pixels, positive = points
            self.f_meta.configure(size=size + 1 if size < 0 else max(7, size - 1))
            self.f_head = base.copy()
            self.f_head.configure(weight="bold")
        except Exception:
            self.f_name = self.f_meta = self.f_head = None
        c = self.canvas
        c.bind("<Configure>", lambda e: self._later_layout())
        c.bind("<Button-1>", self._click)
        c.bind("<Double-1>", self._double)
        c.bind("<Button-3>", self._context)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Button-4>", self._wheel)
        c.bind("<Button-5>", self._wheel)
        c.bind("<Return>", lambda e: self.tab.open_selected())
        c.bind("<Delete>", lambda e: self.tab.remove_selected())
        for key, step in (("<Left>", -1), ("<Right>", 1), ("<Up>", "up"), ("<Down>", "down")):
            c.bind(key, lambda e, s=step: (self._move(s), "break")[1])

    # ---------------------------------------------------------- data and drawing

    def set_jobs(self, groups) -> None:
        self.groups = groups
        self.order = [j["id"] for _, jobs in groups for j in jobs]
        if self.frame.winfo_manager():
            self.layout()

    def _later_layout(self) -> None:
        if self._layout_after:
            self.canvas.after_cancel(self._layout_after)
        self._layout_after = self.canvas.after(60, self.layout)

    def layout(self) -> None:
        self._layout_after = None
        c = self.canvas
        if not self.frame.winfo_manager():
            return
        width = max(c.winfo_width(), CARD_W + 2 * PAD)
        self.cols = max(1, (width - 2 * PAD) // CARD_W)
        x0 = PAD
        c.delete("all")
        self.cards = {}
        y = PAD
        for label, jobs in self.groups:
            c.create_text(x0 + 4, y + HEAD_H // 2, text=label, anchor="w", font=self.f_head, fill=self.pal.fg)
            c.create_line(x0 + 4, y + HEAD_H - 4, width - PAD, y + HEAD_H - 4, fill=self.pal.border)
            y += HEAD_H
            for i, j in enumerate(jobs):
                self._card(j, x0 + (i % self.cols) * CARD_W, y + (i // self.cols) * CARD_H)
            y += math.ceil(len(jobs) / self.cols) * CARD_H + 6
        c.configure(scrollregion=(0, 0, width, y + PAD))
        self.highlight(self.sel)
        self._want_visible()

    def _card(self, job: dict, x: int, y: int) -> None:
        c, pal, tab = self.canvas, self.pal, self.tab
        from . import thumbs
        path, present = thumbs.card_file(job, tab._present_now)
        tag = f"c{job['id']}"
        rect = c.create_rectangle(x + 4, y + 4, x + CARD_W - 4, y + CARD_H - 4, fill="", outline="",
                                  width=2, tags=(tag, "card"))
        img = c.create_image(x + CARD_W // 2, y + 10 + THUMB // 2, image=self._image_now(path, present),
                             tags=(tag,))
        name = Path(path).name if path else "—"
        if len(name) > 40:
            name = name[:24] + "…" + name[-14:]
        c.create_text(x + CARD_W // 2, y + THUMB + 18, text=name, width=CARD_W - 16, anchor="n", justify="center",
                      font=self.f_name, fill=pal.fg if present else pal.muted, tags=(tag,))
        t = local_time(job["started_at"])
        head = (t.strftime("%H:%M") + " · ") if t else ""
        tail = "  " + STATUS_ICON.get(job["status"], "")
        meta = head + self._fit(action_label(job["action_id"]), CARD_W - 14, head + tail) + tail
        c.create_text(x + CARD_W // 2, y + CARD_H - 18, text=meta, anchor="center", font=self.f_meta,
                      fill=pal.muted, tags=(tag,))
        self.cards[job["id"]] = {"rect": rect, "img": img, "path": path, "present": present,
                                 "box": (x, y, x + CARD_W, y + CARD_H)}

    def _fit(self, text: str, width: int, extra: str = "") -> str:
        """Shortens text with … so that extra + text fits in width pixels (one line)."""
        f = self.f_meta
        if f is None:
            return text if len(text) <= 16 else text[:15] + "…"
        room = width - f.measure(extra)
        if f.measure(text) <= room:
            return text
        while text and f.measure(text + "…") > room:
            text = text[:-1]
        return text.rstrip() + "…"

    def _key(self, path, present):
        from . import thumbs
        return (path, thumbs.stamp(path) if present else None, present, self.pal.dark)

    def _image_now(self, path, present):
        """The finished preview if it is ready, else the file-type icon (drawn at once)."""
        if path is None:
            return self._icon("x", False)
        key = self._key(path, present)
        if key in self.images:
            self.images.move_to_end(key)
            return self.images[key]
        return self._icon(path, present)

    def _icon(self, path, present):
        from . import thumbs
        ext = Path(path).suffix.lower() if not Path(path).is_dir() else "/"
        k = (ext, present, self.pal.dark)
        if k not in self.icons:
            im = thumbs.card_image(path, present, THUMB, self.pal.dark, self.pal.surface)
            self.icons[k] = self.tab.tk.PhotoImage(master=self.canvas, data=thumbs.to_png(im))
        return self.icons[k]

    # ---------------------------------------------------------- previews in the background

    def _visible_ids(self) -> list[int]:
        c = self.canvas
        top, bottom = c.canvasy(0) - CARD_H, c.canvasy(c.winfo_height()) + CARD_H
        return [jid for jid in self.order if jid in self.cards
                and self.cards[jid]["box"][3] >= top and self.cards[jid]["box"][1] <= bottom]

    def _want_visible(self) -> None:
        from . import thumbs
        want = []
        for jid in self._visible_ids():
            card = self.cards[jid]
            path, present = card["path"], card["present"]
            if not path or not present or thumbs.kind(path) == "folder":
                continue
            ext = Path(path).suffix.lower().lstrip(".")
            if sys.platform != "win32" and ext != "pdf" and ext not in thumbs._IMAGE_EXT:
                continue                                    # nothing better than the type icon here
            key = self._key(path, present)
            if key not in self.images and key not in self.busy:
                want.append((key, path))
        with self.lock:
            self.waiting = list(want)                      # only what is on screen now (a copy: the worker pops it)
        if want and (self.worker is None or not self.worker.is_alive()):
            self.worker = threading.Thread(target=self._work, daemon=True)
            self.worker.start()
        if want and self._poll_after is None:
            self._poll_after = self.canvas.after(60, self._poll)

    def _work(self) -> None:
        from . import thumbs
        thumbs.com_init()
        while True:
            with self.lock:
                if not self.waiting:
                    return
                key, path = self.waiting.pop(0)
                self.busy.add(key)
            try:
                content = thumbs.preview(path, THUMB)
                im = thumbs.card_image(path, True, THUMB, key[3], self.pal.surface, content=content)
                png = thumbs.to_png(im)
            except Exception:
                png = None
            self.done.put((key, png))

    def _poll(self) -> None:
        self._poll_after = None
        try:
            changed = False
            while True:
                try:
                    key, png = self.done.get_nowait()
                except queue.Empty:
                    break
                self.busy.discard(key)
                if png is None:
                    continue
                self.images[key] = self.tab.tk.PhotoImage(master=self.canvas, data=png)
                while len(self.images) > CACHE:
                    self.images.popitem(last=False)
                changed = True
            if changed:
                for jid, card in self.cards.items():
                    k = self._key(card["path"], card["present"]) if card["path"] else None
                    if k in self.images:
                        self.canvas.itemconfigure(card["img"], image=self.images[k])
            with self.lock:
                more = bool(self.waiting) or bool(self.busy)
            if more or (self.worker and self.worker.is_alive()):
                self._poll_after = self.canvas.after(60, self._poll)
        except Exception:                                   # the window is closing
            pass

    # ---------------------------------------------------------- selection and input

    def highlight(self, job_id) -> None:
        c = self.canvas
        if self.sel in self.cards:
            c.itemconfigure(self.cards[self.sel]["rect"], fill="", outline="")
        self.sel = job_id
        if job_id in self.cards:
            c.itemconfigure(self.cards[job_id]["rect"], fill=_mix(self.pal.surface, self.pal.select_bg, 0.18),
                            outline=self.pal.select_bg)

    def see(self, job_id) -> None:
        if job_id not in self.cards:
            return
        c = self.canvas
        x0, y0, x1, y1 = self.cards[job_id]["box"]
        region = c.cget("scrollregion").split()
        total = float(region[3]) if len(region) == 4 else 1
        top, bottom = c.canvasy(0), c.canvasy(c.winfo_height())
        if y0 < top:
            c.yview_moveto(max(0, y0 - HEAD_H) / total)
        elif y1 > bottom:
            c.yview_moveto((y1 - c.winfo_height() + PAD) / total)
        self._want_visible()

    def _at(self, e) -> int | None:
        c = self.canvas
        x, y = c.canvasx(e.x), c.canvasy(e.y)
        for jid, card in self.cards.items():
            x0, y0, x1, y1 = card["box"]
            if x0 + 4 <= x <= x1 - 4 and y0 + 4 <= y <= y1 - 4:
                return jid
        return None

    def choose(self, job_id) -> None:
        self.tab.select(job_id)                             # the tree holds the selection
        self.see(job_id)

    def _click(self, e) -> None:
        self.canvas.focus_set()
        jid = self._at(e)
        if jid is not None:
            self.choose(jid)

    def _double(self, e):
        jid = self._at(e)
        if jid is not None:
            self.choose(jid)
            self.tab.open_selected()
            return "break"

    def _context(self, e) -> None:
        jid = self._at(e)
        if jid is None:
            return
        self.choose(jid)
        try:
            self.tab.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.tab.menu.grab_release()

    def _wheel(self, e) -> None:
        if getattr(e, "num", 0) == 4 or getattr(e, "delta", 0) > 0:
            self.canvas.yview_scroll(-1, "units")
        else:
            self.canvas.yview_scroll(1, "units")
        self._want_visible()

    def _move(self, step) -> None:
        if not self.order:
            return
        if self.sel not in self.order:
            self.choose(self.order[0])
            return
        i = self.order.index(self.sel)
        if step in ("up", "down"):                         # same column in the row above / below (within the day)
            x0, y0 = self.cards[self.sel]["box"][:2]
            same_col = [jid for jid in self.order if self.cards[jid]["box"][0] == x0]
            k = same_col.index(self.sel) + (-1 if step == "up" else 1)
            if 0 <= k < len(same_col):
                self.choose(same_col[k])
            return
        k = i + step
        if 0 <= k < len(self.order):
            self.choose(self.order[k])

    def _yview(self, *args) -> None:
        self.canvas.yview(*args)
        self._want_visible()

    def _yset(self, first, last) -> None:
        self.sb.set(first, last)


def _mix(a: str, b: str, t: float) -> str:
    """Colour between a and b ('#rrggbb'), t = share of b."""
    try:
        ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
        cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))
    except (ValueError, IndexError):
        return b


def build_tab(nb, root, pal) -> HistoryTab:
    tab = HistoryTab(nb, root, pal)
    nb.add(tab.frame, text=T("Geçmiş"))
    return tab
