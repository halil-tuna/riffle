"""Uygulama katmanı: komut satırı, çoklu seçim toplayıcı, kurulum (Windows/Nemo), arayüz."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pymupdf
import pytest

from conftest import ROOT, make_text_pdf

RUN = ROOT / "run.pyw"


def run(*args, env=None, timeout=120):
    e = dict(os.environ, RIFFLE_AUTOCLOSE_MS="500")   # pencere açılırsa kendiliğinden kapansın
    e.update(env or {})
    return subprocess.run([sys.executable, str(RUN), *map(str, args)], capture_output=True, text=True,
                          timeout=timeout, env=e)


# ----------------------------------------------------------------- komut satırı

def test_cli_list():
    r = run("--list")
    assert r.returncode == 0 and "pdf2word" in r.stdout and "watermark" in r.stdout


def test_cli_unknown_action(pdf):
    assert run("yok_boyle", pdf).returncode == 2


def test_cli_unsupported_file_skipped(work, pdf):
    other = work / "resim.xyz"
    other.write_text("x")
    r = run("compress", pdf, other)
    assert r.returncode == 0
    assert "desteklenmeyen" in r.stdout
    assert any(work.glob("*_sikistirilmis.pdf"))


def test_cli_ask_via_stdin(pdf):
    # tkinter yokken soru stdin'den sorulur (pencereli ortamda test_ui.py dener)
    try:
        import tkinter  # noqa: F401
        if os.environ.get("DISPLAY"):
            pytest.skip("pencereli ortam: soru penceresi test_ui.py'de denenir")
    except ImportError:
        pass
    r = subprocess.run([sys.executable, str(RUN), "extract", str(pdf)], input="2-3\n",
                       capture_output=True, text=True, timeout=60)
    out = next(pdf.parent.glob("*_secilen_sayfalar.pdf"), None)
    if out is None:
        pytest.skip("tkinter var; soru pencereyle soruluyor (arayüz testinde denenir)")
    with pymupdf.open(out) as d:
        assert len(d) == 2


# ----------------------------------------------------------------- toplayıcı (Windows çoklu seçim)

def test_collector_merges_parallel_launches(work):
    files = [make_text_pdf(work / f"parça {i}.pdf", pages=1, table=False) for i in range(1, 8)]
    procs = []
    for f in files:   # Windows Gezgini gibi: her dosya için ayrı süreç, hafif zaman farkıyla
        procs.append(subprocess.Popen([sys.executable, str(RUN), "merge", str(f)],
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        time.sleep(0.03)
    for p in procs:
        p.wait(timeout=60)
    merged = list(work.glob("*_birlesik*.pdf"))
    assert len(merged) == 1, f"tek birleşik dosya beklenirdi: {merged}"
    with pymupdf.open(merged[0]) as d:
        assert len(d) == 7


def test_collector_two_batches_back_to_back(work):
    a = [make_text_pdf(work / f"a{i}.pdf", pages=1, table=False) for i in range(3)]
    for f in a:
        subprocess.Popen([sys.executable, str(RUN), "compress", str(f)])
    time.sleep(4)
    outs = list(work.glob("a*_sikistirilmis.pdf"))
    deadline = time.time() + 30
    while len(outs) < 3 and time.time() < deadline:
        time.sleep(0.5)
        outs = list(work.glob("a*_sikistirilmis.pdf"))
    assert len(outs) == 3


# ----------------------------------------------------------------- kurulum

class FakeReg:
    """winreg'in kullandığımız kısmı, bellekte."""

    def __init__(self):
        self.keys: dict[str, dict] = {}
        m = types.ModuleType("winreg")
        m.HKEY_CURRENT_USER, m.KEY_ALL_ACCESS, m.REG_SZ, m.REG_DWORD = "HKCU", 0, 1, 4

        class K:
            def __init__(s, p): s.p = p
            def __enter__(s): return s
            def __exit__(s, *a): pass

        def CreateKey(root, p):
            parts = p.split("\\")
            for i in range(1, len(parts) + 1):
                self.keys.setdefault("\\".join(parts[:i]), {})
            return K(p)

        def SetValueEx(k, n, _, t, v): self.keys[k.p][n or "(Default)"] = v

        def OpenKey(root, p, *a):
            if p not in self.keys:
                raise FileNotFoundError(p)
            return K(p)

        def EnumKey(k, i):
            kids = sorted({x[len(k.p) + 1:].split("\\")[0] for x in self.keys if x.startswith(k.p + "\\")})
            if i >= len(kids):
                raise OSError
            return kids[i]

        def DeleteKey(root, p):
            if any(x.startswith(p + "\\") for x in self.keys):
                raise PermissionError("alt anahtarı var")
            self.keys.pop(p)

        for f in (CreateKey, SetValueEx, OpenKey, EnumKey, DeleteKey):
            setattr(m, f.__name__, f)
        self.module = m


@pytest.fixture
def installer(monkeypatch):
    reg = FakeReg()
    monkeypatch.setitem(sys.modules, "winreg", reg.module)
    sys.path.insert(0, str(ROOT))
    import importlib
    import install
    importlib.reload(install)
    install.IS_WIN = True
    install._win_refresh_shell = lambda: None
    return install, reg


def test_windows_menu_groups(installer):
    install, reg = installer
    install.install_windows()
    pdf = {k: v for k, v in reg.keys.items() if "\\.pdf\\" in k}
    groups = {v["MUIVerb"] for k, v in pdf.items() if k.endswith("_grup")}
    roots = sorted((k, v["MUIVerb"]) for k, v in pdf.items() if k.split("\\")[-1].startswith("Riffle"))
    assert roots[0][1] == "Riffle" and len(roots) <= 5
    titles = " ".join(t for _, t in roots) + " " + " ".join(groups)
    for g in ("Dönüştür", "Düzenle", "Sayfalar", "Yazdır", "Küçült / Onar", "Güvenlik"):
        assert g in titles
    cmds = [v["(Default)"] for k, v in pdf.items() if k.endswith("\\command")]
    from riffle.actions import actions_for_extension
    assert len(cmds) == len(actions_for_extension("pdf"))
    assert all(str(install.RUN) in c and c.endswith('"%1"') for c in cmds)
    # Office: az işlem → alt grup yok, doğrudan liste
    docx = [k for k in reg.keys if "\\.docx\\" in k and k.endswith("_grup")]
    assert docx == []
    assert any("\\.heic\\" in k for k in reg.keys), "iPhone HEIC menüde olmalı"
    # klasör menüleri: klasörün üstü (%1) ve içi boş alan (%V)
    folder_cmds = {k: v["(Default)"] for k, v in reg.keys.items() if "\\Directory\\" in k and k.endswith("\\command")}
    assert any("Background" not in k and v.endswith('"%1"') for k, v in folder_cmds.items())
    assert any("Background" in k and v.endswith('"%V"') for k, v in folder_cmds.items())


def test_windows_cascades_fit_16(installer):
    """Gezgin bir alt menü ağacında 16 satırdan fazlasını göstermiyor (alt menü başlıkları dahil):
    her üst girdi en fazla 16 satır, her işlem tam bir kez, Ayarlar ana menüde."""
    install, reg = installer
    from riffle.actions import actions_for_extension
    for ext in ("pdf", "docx", "jpg", "heic", "pptx"):
        cascades = install.win_cascades(ext)
        seen = []
        for title, tree in cascades:
            assert sum((1 if g else 0) + len(a) for g, a in tree) <= 16, (ext, title)
            seen += [x.id for _, acts in tree for x in acts]
        assert sorted(seen) == sorted(a.id for a in actions_for_extension(ext)), ext
        main = [x.id for _, acts in cascades[0][1] for x in acts]
        assert main[-1] == "settings" and main[0] == "organize"
    # kayıt defterinde de: her kök altında en çok 16 satır
    install.install_windows()
    roots = {k for k in reg.keys if "\\.pdf\\" in k and k.split("\\")[-1].startswith("Riffle")}
    for r in roots:
        rows = [k for k in reg.keys if k.startswith(r + "\\shell\\") and not k.endswith(("\\command", "\\shell"))]
        assert 0 < len(rows) <= 16, (r, len(rows))


def test_windows_reinstall_and_uninstall(installer):
    install, reg = installer
    install.install_windows()
    n = len(reg.keys)
    install.install_windows()
    assert len(reg.keys) == n, "yeniden kurulum anahtar biriktirmemeli"
    install.uninstall_windows()
    assert not [k for k in reg.keys if "Riffle" in k]


def test_windows_removes_old_name_menu(installer):
    """0.7.0 öncesi 'Pratik Çevirici' menüsü (pdf + klasör) Riffle kurulunca silinir: iki menü kalmaz."""
    install, reg = installer
    w = reg.module
    for k in (r"Software\Classes\SystemFileAssociations\.pdf\shell\PratikCevirici\shell\00_x",
              r"Software\Classes\SystemFileAssociations\.pdf\shell\PratikCevirici_2",
              r"Software\Classes\SystemFileAssociations\.docx\shell\PratikCevirici",
              r"Software\Classes\Directory\shell\PratikCevirici",
              r"Software\Classes\Directory\Background\shell\PratikCevirici\shell\a"):
        with w.CreateKey("HKCU", k):
            pass
    install.install_windows()
    assert not [k for k in reg.keys if "PratikCevirici" in k]
    assert [k for k in reg.keys if k.endswith(r"\.pdf\shell\Riffle")]


def test_linux_removes_old_name_actions(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    import install
    monkeypatch.setattr(install, "NEMO_DIR", tmp_path)
    (tmp_path / "pratik_compress.nemo_action").write_text("x")
    install.install_linux()
    assert not list(tmp_path.glob("pratik_*")) and list(tmp_path.glob("riffle_*"))
    install.uninstall_linux()
    assert not list(tmp_path.glob("*.nemo_action"))


def test_config_moves_from_old_name(tmp_path, monkeypatch):
    from riffle import stamp_model
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    old = tmp_path / ("PratikCevirici" if sys.platform == "win32" else "pratik-cevirici")
    old.mkdir()
    (old / "ayarlar.json").write_text('{"output": "desktop"}', encoding="utf-8")
    d = stamp_model.config_dir()
    assert (d / "ayarlar.json").read_text(encoding="utf-8") == '{"output": "desktop"}'
    assert not old.exists()


def test_windows_menu_frozen_exe(installer, monkeypatch):
    """Kurulum dosyasıyla gelen .exe: komut doğrudan programı çağırır (python/run.pyw yok)."""
    install, reg = installer
    monkeypatch.setattr(install, "FROZEN", True)
    monkeypatch.setattr(install.sys, "executable", r"C:\Users\a\AppData\Local\Programs\Riffle\Riffle.exe")
    install.install_windows()
    cmds = [v["(Default)"] for k, v in reg.keys.items() if k.endswith("\\command")]
    assert cmds and all(c.startswith('"C:\\Users\\a\\AppData\\Local\\Programs\\Riffle\\Riffle.exe" ')
                        for c in cmds)
    assert not any("run.pyw" in c for c in cmds)
    sign = next(c for k, c in ((k, v["(Default)"]) for k, v in reg.keys.items() if k.endswith("\\command"))
                if "_sign\\" in k)
    assert sign.endswith(' sign "%1"')


def test_main_menu_flags(monkeypatch, tmp_path):
    sys.path.insert(0, str(ROOT))
    import importlib
    import install
    importlib.reload(install)
    monkeypatch.setattr(install, "IS_WIN", False)
    monkeypatch.setattr(install, "NEMO_DIR", tmp_path / "actions")
    from riffle.main import main
    assert main(["--install-menu"]) == 0
    assert (tmp_path / "actions" / "riffle_sign.nemo_action").exists()
    assert main(["--uninstall-menu"]) == 0
    assert not list((tmp_path / "actions").glob("*"))


def test_nemo_actions(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT))
    import importlib
    import install
    importlib.reload(install)
    monkeypatch.setattr(install, "NEMO_DIR", tmp_path / "actions")
    install.install_linux()
    files = sorted((tmp_path / "actions").glob("riffle_*.nemo_action"))
    from riffle.actions import ACTIONS
    assert len(files) == len(ACTIONS)
    folder = (tmp_path / "actions" / "riffle_folder2pdf.nemo_action").read_text(encoding="utf-8")
    assert "Extensions=dir;" in folder
    merge = (tmp_path / "actions" / "riffle_merge.nemo_action").read_text(encoding="utf-8")
    assert "Selection=m" in merge and "%F" in merge and "Quote=double" in merge
    assert "Name=Riffle › Düzenle › PDF'leri birleştir" in merge
    install.uninstall_linux()
    assert not list((tmp_path / "actions").glob("riffle_*"))


def test_installer_loads_without_libraries():
    """install.py kütüphaneler kurulmadan önce çalışır: PyMuPDF yokken de yüklenebilmeli
    (iki kez yakalanan 'tavuk-yumurta' hatasının testi)."""
    code = ("import sys; sys.modules['pymupdf'] = None; sys.modules['fitz'] = None; "
            "sys.modules['pdf2docx'] = None; sys.path.insert(0, r'%s'); "
            "import install; t = install.menu_tree('pdf'); print(len(t))" % ROOT)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "8"   # üstte (düzenleyici, imza, form) + 6 alt menü + en sonda Ayarlar


# ---------------------------------------------------------------- ayarlar

@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "cfg"))
    sys.path.insert(0, str(ROOT))
    import importlib
    import install
    importlib.reload(install)
    from riffle import settings
    return settings, install


def test_menu_hidden_and_favorites(cfg):
    settings, install = cfg
    s = settings.load()
    s["hidden"] = ["pdf2excel", "watermark", "settings"]       # Ayarlar gizlenemez
    s["pinned"] = ["compress"]
    settings.save(s)
    settings._write("kullanim.json", {"pdf2word": 9, "merge": 5, "rotate": 2, "pdf2excel": 50})
    assert settings.favorites() == ["compress", "pdf2word", "merge"]   # gizli ve az kullanılan yok
    tree = install.menu_tree("pdf")
    top = [a.id for a in tree[0][1]]
    assert top[:3] == ["compress", "pdf2word", "merge"]
    all_ids = [a.id for _, acts in tree for a in acts]
    assert "pdf2excel" not in all_ids and "watermark" not in all_ids
    assert all_ids[-1] == "settings" and all_ids.count("compress") == 1
    conv = next(acts for g, acts in tree if g == "Dönüştür")
    assert "pdf2word" not in [a.id for a in conv]                 # üste taşındı, grupta tekrar yok


def test_record_use_triggers_refresh(cfg):
    settings, _ = cfg
    assert [settings.record_use("rotate") for _ in range(3)] == [False, False, True]
    assert settings.usage()["rotate"] == 3 and "rotate" in settings.favorites()
    s = settings.load()
    s["auto_favorites"] = False
    settings.save(s)
    assert settings.favorites() == []


def test_settings_survive_garbage(cfg):
    settings, _ = cfg
    (settings._dir() / "ayarlar.json").write_text("{bozuk", encoding="utf-8")
    assert settings.load() == settings.DEFAULTS


def test_nemo_respects_hidden(cfg, tmp_path, monkeypatch):
    settings, install = cfg
    s = settings.load()
    s["hidden"] = ["merge"]
    s["pinned"] = ["compress"]
    settings.save(s)
    monkeypatch.setattr(install, "NEMO_DIR", tmp_path / "nemo")
    install.install_linux()
    assert not (tmp_path / "nemo" / "riffle_merge.nemo_action").exists()
    comp = (tmp_path / "nemo" / "riffle_compress.nemo_action").read_text(encoding="utf-8")
    assert "Name=Riffle › Sıkıştır" in comp                         # sık kullanılan: grupsuz
    st = (tmp_path / "nemo" / "riffle_settings.nemo_action").read_text(encoding="utf-8")
    assert "Extensions=dir;pdf;" in st


def test_output_to_folder_setting(cfg, tmp_path, pdf):
    settings, _ = cfg
    target = tmp_path / "Çıktılar"
    target.mkdir()
    s = settings.load()
    s["output"], s["output_dir"] = "folder", str(target)
    settings.save(s)
    r = run("compress", pdf, env=dict(os.environ))
    assert r.returncode == 0, r.stdout + r.stderr
    assert list(target.glob("*_sikistirilmis.pdf"))
    assert not list(pdf.parent.glob("*_sikistirilmis.pdf"))
