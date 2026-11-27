"""The History tab of the Riffle window: what was converted when, from which files to which.

Built by settings.run() inside a guard: if anything here fails, the window opens without this tab.
Reading the history (history.query) takes milliseconds, so it is done on the window's own thread.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
        self.search_entry = ttk.Entry(bar, textvariable=self.search, width=22)
        self.search_entry.pack(side="left", padx=(6, 14))
        self.search.trace_add("write", lambda *_: self._later())
        ttk.Label(bar, text=T("İşlem:")).pack(side="left")
        self.action = tk.StringVar(value=T("Tümü"))
        self.action_box = ttk.Combobox(bar, textvariable=self.action, state="readonly", width=20)
        self.action_box.pack(side="left", padx=(6, 14))
        self.action_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(bar, text=T("Durum:")).pack(side="left")
        self.status = tk.StringVar(value=T("Tümü"))
        self.status_box = ttk.Combobox(bar, textvariable=self.status, state="readonly", width=12,
                                       values=[T("Tümü")] + list(_status_names().values()))
        self.status_box.pack(side="left", padx=(6, 0))
        self.status_box.bind("<<ComboboxSelected>>", lambda e: self.refresh())

        self.banner = ttk.Label(f, style="Muted.TLabel")       # shown only when there is something to say

        mid = self.mid = ttk.Frame(f)
        mid.pack(fill="both", expand=True, pady=(10, 8))
        cols = ("time", "action", "files", "status", "duration")
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings", selectmode="browse")
        self.tree.column("#0", width=16, minwidth=16, stretch=False)
        for c, title, w, anchor, stretch in (("time", T("Saat"), 60, "w", False),
                                             ("action", T("İşlem"), 240, "w", False),
                                             ("files", T("Dosyalar"), 330, "w", True),
                                             ("status", "", 36, "center", False),
                                             ("duration", T("Süre"), 92, "e", False)):
            self.tree.heading(c, text=title, anchor=anchor)
            self.tree.column(c, width=w, minwidth=30, anchor=anchor, stretch=stretch)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
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
        self.b_remove = ttk.Button(btns, text=T("Listeden kaldır"), command=self.remove_selected)
        for b in (self.b_open, self.b_show, self.b_remove):
            b.pack(side="left", padx=(0, 8))
        ttk.Button(btns, text=T("Geçmişi temizle"), command=self.clear_all).pack(side="right")

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label=T("Aç"), command=self.open_selected)
        self.menu.add_command(label=T("Klasörde göster"), command=self.show_selected)
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

    def show_details(self) -> None:
        j = self.selected()
        d = self.detail
        d.configure(state="normal")
        d.delete("1.0", "end")
        for b in (self.b_open, self.b_show, self.b_remove):
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


def build_tab(nb, root, pal) -> HistoryTab:
    tab = HistoryTab(nb, root, pal)
    nb.add(tab.frame, text=T("Geçmiş"))
    return tab
