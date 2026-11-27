#!/usr/bin/env python3
"""Riffle install / uninstall.

    python install.py              # install (Windows or Linux Mint/Nemo)
    python install.py --uninstall  # remove the right-click menu
    python install.py --menu-only  # skip the libraries, only rewrite the menu

NO admin rights needed: HKEY_CURRENT_USER on Windows, ~/.local on Linux.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
RUN = ROOT / "run.pyw"
sys.path.insert(0, str(ROOT))

from riffle import APP_NAME  # noqa: E402
from riffle.i18n import T  # noqa: E402
from riffle.actions import ACTIONS, FOLDER, all_extensions, actions_for_extension  # noqa: E402

IS_WIN = sys.platform == "win32"
FROZEN = bool(getattr(sys, "frozen", False))      # .exe packaged with PyInstaller


def command_prefix(gui: bool = True) -> str:
    """Start of the right-click command: the program itself for an .exe install, python + run.pyw for a source install."""
    if FROZEN:
        return f'"{sys.executable}"'
    return f'"{venv_python(gui)}" "{RUN}"'


# --------------------------------------------------------------------------
# Virtual environment + libraries
# --------------------------------------------------------------------------

def venv_python(gui: bool = False) -> Path:
    if IS_WIN:
        return VENV / "Scripts" / ("pythonw.exe" if gui else "python.exe")
    return VENV / "bin" / "python3"


def setup_venv() -> None:
    if not venv_python().exists():
        print(T("» Sanal ortam oluşturuluyor (.venv)…"))
        args = [sys.executable, "-m", "venv", str(VENV)]
        if not IS_WIN:
            args.insert(3, "--system-site-packages")  # make the tkinter from apt visible
        subprocess.check_call(args)
    print(T("» Kütüphaneler kuruluyor (ilk seferde 1-2 dk)…"))
    py = str(venv_python())
    subprocess.check_call([py, "-m", "pip", "install", "-q", "--upgrade", "pip"])
    subprocess.check_call([py, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")])
    r = subprocess.run([py, "-c", "import tkinter"], capture_output=True)
    if r.returncode != 0:
        print(T("  ! tkinter yok → pencere yerine bildirim kullanılacak."))
        if not IS_WIN:
            print(T("    Pencere için: sudo apt install python3-tk  (sonra kurulumu tekrar çalıştır)"))


# --------------------------------------------------------------------------
# Windows: Explorer right-click → "Riffle" submenu
# --------------------------------------------------------------------------

WIN_BASE = r"Software\Classes\SystemFileAssociations\.{ext}\shell\Riffle"
# right-click on a folder (%1) and on empty space inside a folder (%V)
WIN_FOLDER_BASES = [(r"Software\Classes\Directory\shell\Riffle", "%1"),
                    (r"Software\Classes\Directory\Background\shell\Riffle", "%V")]


def _win_delete_tree(root, path: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(root, path, 0, winreg.KEY_ALL_ACCESS) as k:
            children, i = [], 0
            while True:  # list first, then delete (so indexes don't shift while deleting)
                try:
                    children.append(winreg.EnumKey(k, i))
                    i += 1
                except OSError:
                    break
        for child in children:
            _win_delete_tree(root, path + "\\" + child)
        winreg.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def _win_refresh_shell() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED
    except Exception:
        pass


def menu_tree(ext: str) -> list:
    """Right-click menu of an extension: [(group_name | None, [actions])].

    With many actions it is split into groups (nested submenus on Windows);
    with few actions (Office, images) a single flat list.
    """
    hidden, favs = _menu_prefs()
    acts = [a for a in actions_for_extension(ext) if a.id not in hidden]
    tail = [a for a in acts if a.id == "settings"]               # "Settings…" always last
    acts = [a for a in acts if a.id != "settings"]
    top = sorted((a for a in acts if a.id in favs), key=lambda a: favs.index(a.id))
    rest = [a for a in acts if a.id not in favs]
    if len(acts) <= 6 or not any(a.group for a in acts):
        return [(None, top + rest + tail)]
    # Grouped menu: favourites at the top AND still in their own group (nothing disappears from where it
    # was learnt, e.g. PDF → Word stays under "Convert" too)
    tree: list = [(None, list(top))]
    for a in acts:
        key = a.group or None
        for node in tree:
            if node[0] == key:
                node[1].append(a)
                break
        else:
            tree.append((key, [a]))
    tree.append((None, tail))
    return [n for n in tree if n[1]]


def _menu_prefs() -> tuple[set, list]:
    """Settings: hidden actions and favourites that go to the top. Empty if the settings can't be read."""
    try:
        from riffle import settings
        s = settings.load()
        return set(s["hidden"]) - settings.NEVER_HIDE, settings.favorites(s)
    except Exception:
        return set(), []


def _win_item(winreg, HK, key: str, action, prefix, arg: str = "%1") -> None:
    with winreg.CreateKey(HK, key) as k:
        winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, T(action.label))
        # show the menu even when more than 15 files are selected
        winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
        if action.id == "settings":                     # separator line before "Settings…"
            winreg.SetValueEx(k, "CommandFlags", 0, winreg.REG_DWORD, 0x20)
    with winreg.CreateKey(HK, key + "\\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f'{prefix} {action.id} "{arg}"')


def _win_icon(winreg, key) -> None:
    """Icon next to the 'Riffle' entry in the right-click menu."""
    from riffle.theme import ICON_ICO
    if ICON_ICO.exists():
        winreg.SetValueEx(key, "Icon", 0, winreg.REG_SZ, str(ICON_ICO))


def _install_folder_menus(winreg, HK, prefix) -> None:
    acts = [a for node in menu_tree(FOLDER) for a in node[1]]
    for base, arg in WIN_FOLDER_BASES:
        _win_delete_tree(HK, base)
        if not acts:
            continue
        with winreg.CreateKey(HK, base) as k:
            winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, APP_NAME)
            winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
            _win_icon(winreg, k)
        for i, action in enumerate(acts):
            _win_item(winreg, HK, f"{base}\\shell\\{i:02d}_{action.id}", action, prefix, arg)


# Windows Explorer shows at most 16 rows in a "static" submenu tree in the registry:
# submenu headings count too, and what doesn't fit is silently left empty (in the PDF menu "Edit" was empty,
# "Settings" was missing). So crowded menus like PDF are split into several top-level entries that each fit 16 rows.
WIN_MAX_ITEMS = 16
WIN_EXTRA = 8                                  # Riffle_2 … _9


def win_cascades(ext: str) -> list:
    """[(title, [(group | None, [actions])])] — each title is a separate right-click entry, ≤16 rows."""
    tree = menu_tree(ext)
    flat_top = [a for g, acts in tree if g is None for a in acts]
    groups = [(g, acts) for g, acts in tree if g is not None]
    if not groups:                                          # few actions: one flat menu
        return [(APP_NAME, [(None, flat_top[:WIN_MAX_ITEMS])])]
    tail = [a for a in flat_top if a.id == "settings"]
    head = [a for a in flat_top if a.id != "settings"][:WIN_MAX_ITEMS - len(tail)]
    out = [(APP_NAME, [(None, head + tail)])]
    # split big groups into chunks of 16
    chunks = []
    for g, acts in groups:
        for k in range(0, len(acts), WIN_MAX_ITEMS):
            part = acts[k:k + WIN_MAX_ITEMS]
            chunks.append((g if k == 0 else f"{g} (devam)", part))
    cur, cost = [], 0
    for g, acts in chunks:
        c = 1 + len(acts)                                    # group heading + contents
        if cur and cost + c > WIN_MAX_ITEMS:
            out.append(_cascade(cur))
            cur, cost = [], 0
        cur.append((g, acts))
        cost += c
    if cur:
        out.append(_cascade(cur))
    # if there is room in the main menu, move small groups there (fewer rows in the right-click menu)
    main_cost = len(head) + len(tail)
    for k in range(len(out) - 1, 0, -1):
        title, nodes = out[k]
        if nodes[0][0] is None:                            # single group written flat: put its heading back
            nodes = [(title.split(" · ", 1)[1], nodes[0][1])]
        cost = sum(1 + len(a) for _, a in nodes)
        if main_cost + cost <= WIN_MAX_ITEMS:
            main_nodes = out[0][1]
            out[0] = (APP_NAME, [(None, head)] + nodes + main_nodes[1:-1] + [(None, tail)])
            main_cost += cost
            del out[k]
    out[0] = (APP_NAME, [n for n in out[0][1] if n[1]])
    return out[:1 + WIN_EXTRA]


def _cascade(groups: list) -> tuple:
    """Single group → flat menu ('Riffle · Convert'); several groups → nested."""
    title = f"{APP_NAME} · " + ", ".join(T(g) for g, _ in groups)
    if len(groups) == 1:
        return title, [(None, groups[0][1])]
    return title, groups


def _win_cascade_keys(base: str) -> list[str]:
    return [base] + [f"{base}_{k}" for k in range(2, 2 + WIN_EXTRA)]


def install_windows() -> None:
    import winreg
    pyw = command_prefix(gui=True)
    HK = winreg.HKEY_CURRENT_USER
    for ext in all_extensions():
        keys = _win_cascade_keys(WIN_BASE.format(ext=ext))
        for k in keys:
            _win_delete_tree(HK, k)  # clean up the old installation
        for base, (title, tree) in zip(keys, win_cascades(ext)):
            with winreg.CreateKey(HK, base) as k:
                winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, title)
                _win_icon(winreg, k)
                winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
                winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
            for gi, (group, acts) in enumerate(tree):
                if group is None:
                    for i, action in enumerate(acts):
                        _win_item(winreg, HK, f"{base}\\shell\\{gi:02d}{i:02d}_{action.id}", action, pyw)
                    continue
                gkey = f"{base}\\shell\\{gi:02d}_grup"
                with winreg.CreateKey(HK, gkey) as k:
                    winreg.SetValueEx(k, "MUIVerb", 0, winreg.REG_SZ, T(group))
                    winreg.SetValueEx(k, "SubCommands", 0, winreg.REG_SZ, "")
                    winreg.SetValueEx(k, "MultiSelectModel", 0, winreg.REG_SZ, "Player")
                for i, action in enumerate(acts):
                    _win_item(winreg, HK, f"{gkey}\\shell\\{i:02d}_{action.id}", action, pyw)
    _install_folder_menus(winreg, HK, pyw)
    _win_refresh_shell()
    print(T("✔ Windows sağ tık menüsü eklendi ({0} dosya türü + klasörler).", len(all_extensions())))
    print(T("  Windows 11: dosyaya sağ tık → 'Daha fazla seçenek göster' (veya Shift+F10)"))
    print(T("  → 'Riffle' alt menüleri."))


def uninstall_windows() -> None:
    import winreg
    for ext in all_extensions():
        for k in _win_cascade_keys(WIN_BASE.format(ext=ext)):
            _win_delete_tree(winreg.HKEY_CURRENT_USER, k)
    for base, _ in WIN_FOLDER_BASES:
        _win_delete_tree(winreg.HKEY_CURRENT_USER, base)
    _win_refresh_shell()
    print(T("✔ Windows sağ tık menüsü kaldırıldı."))


# --------------------------------------------------------------------------
# Linux Mint (Cinnamon / Nemo): ~/.local/share/nemo/actions/*.nemo_action
# --------------------------------------------------------------------------

NEMO_DIR = Path.home() / ".local" / "share" / "nemo" / "actions"


def install_linux() -> None:
    NEMO_DIR.mkdir(parents=True, exist_ok=True)
    prefix = command_prefix(gui=False)
    for old in NEMO_DIR.glob("riffle_*.nemo_action"):
        old.unlink()
    hidden, favs = _menu_prefs()
    for action in ACTIONS:
        if action.id in hidden:
            continue
        exts = [e for e in action.extensions if e != FOLDER]
        ext_line = ";".join(exts + [e.upper() for e in exts]) + ";"
        if FOLDER in action.extensions:
            ext_line = ("dir;" + ext_line) if exts else "dir;"
        selection = "m" if action.multi_only else "notnone"
        group = T(action.group) + " › " if action.group else ""
        # favourites: an extra entry right under "Riffle ›", the usual one stays in its group
        names = [("", f"riffle_{action.id}")] if action.id not in favs or not group else \
            [("", f"riffle_{action.id}_fav"), (group, f"riffle_{action.id}")]
        for grp, fname in names:
            content = (
                "[Nemo Action]\n"
                "Active=true\n"
                f"Name=Riffle › {grp if action.id in favs else group}{T(action.label)}\n"
                f"Comment={APP_NAME}: {T(action.label)}\n"
                f"Exec={prefix} {action.id} %F\n"
                "Icon-Name=document-export\n"
                f"Selection={selection}\n"
                f"Extensions={ext_line}\n"
                "Quote=double\n"
            )
            (NEMO_DIR / f"{fname}.nemo_action").write_text(content, encoding="utf-8")
    print(T("✔ Nemo eylemleri yazıldı: {0}", NEMO_DIR))
    print(T("  Masaüstünde/Nemo'da dosyaya sağ tık → 'Riffle › …' satırları."))
    print(T("  Görünmezse: nemo -q  (Nemo'yu yeniden başlatır)"))


def uninstall_linux() -> None:
    for f in NEMO_DIR.glob("riffle_*.nemo_action"):
        f.unlink()
    print(T("✔ Nemo eylemleri kaldırıldı."))


# --------------------------------------------------------------------------

def check_optional() -> None:
    """Optional external programs: if missing, say what is lost and how to install them."""
    print(T("» Ek bileşenler:"))
    from riffle.actions import _find_soffice
    if _find_soffice():
        print(T("  ✔ LibreOffice: Word/Excel/PowerPoint → PDF hazır"))
    else:
        print(T("  ✖ LibreOffice yok → Office → PDF çalışmaz (MS Office kuruluysa Windows'ta yine çalışır)"))
        print("    " + ("https://www.libreoffice.org/download" if IS_WIN else "sudo apt install libreoffice-core"))
    from riffle import ocr
    try:
        exe, langs = ocr.require()
        if "tur" in langs:
            print(T("  ✔ Tesseract OCR ({0}): taranmış PDF → metin hazır", langs))
        else:
            print(T("  ! Tesseract var ama Türkçe dil paketi yok → Türkçe harfler hatalı okunur"))
            print("    " + (T("Tesseract kurulumunu tekrar çalıştır, 'Additional language data' → Turkish işaretle")
                          if IS_WIN else "sudo apt install tesseract-ocr-tur"))
    except ocr.OcrUnavailable:
        print(T("  ✖ Tesseract yok → taranmış PDF'ler Word'e resim olarak geçer, OCR aracı çalışmaz"))
        print("    " + (T("https://github.com/UB-Mannheim/tesseract/wiki (kurarken Turkish dilini işaretle)")
                      if IS_WIN else "sudo apt install tesseract-ocr tesseract-ocr-tur"))


def main() -> None:
    for stream in (sys.stdout, sys.stderr):   # cp1252 when writing to a pipe on Windows: keep ✔ and Turkish letters from crashing
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    args = set(sys.argv[1:])
    if "--uninstall" in args:
        uninstall_windows() if IS_WIN else uninstall_linux()
        return
    if "--check" in args:
        check_optional()
        return
    if "--menu-only" not in args:
        setup_venv()
    install_windows() if IS_WIN else install_linux()
    # The component check needs PyMuPDF: run it in the virtual environment where the libraries are installed
    py = venv_python()
    if py.exists():
        subprocess.run([str(py), str(Path(__file__).resolve()), "--check"])
    else:
        try:
            check_optional()
        except ImportError:
            print(T("  (ek bileşen kontrolü atlandı: önce kütüphaneler kurulmalı)"))


if __name__ == "__main__":
    main()
