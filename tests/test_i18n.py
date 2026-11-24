"""Language support: does every string have an English version, do the placeholders match, are there
forgotten Turkish strings, does the English UI work end to end."""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))
import i18n_scan  # noqa: E402

from riffle.i18n_en import CATALOG  # noqa: E402

PH = re.compile(r"\{[^{}]*\}")


def test_every_text_has_english():
    missing = [f"{where}: {m!r}" for m, where in i18n_scan.msgids().items() if m not in CATALOG]
    assert not missing, "çevirisi olmayan metinler:\n" + "\n".join(missing)


def test_placeholders_and_markers_match():
    bad = []
    for tr, en in CATALOG.items():
        if sorted(PH.findall(tr)) != sorted(PH.findall(en)):
            bad.append((tr, en))
        for mark in ("ℹ ", "✔"):
            if tr.startswith(mark) != en.startswith(mark):
                bad.append((tr, en))
    assert not bad, bad


SINK_POS = {"progress", "prog", "ConversionError", "OcrUnavailable", "notify", "showinfo", "showerror",
            "showwarning", "askyesno", "ask_text", "_dialog", "add_command"}
SINK_KW = {"text", "label", "title", "message"}
ALLOWED = re.compile(r"^[\W\d_]*$|^(PDF|Word|Excel|PowerPoint|JPG|PNG|OCR|Markdown|Riffle|TXT|A4|Ctrl\+\w|"
                     r"Türkçe|English|[◀▶✂…]|[A-Z]→[A-Z])$")


def _literal_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    return None


def test_no_unwrapped_ui_strings():
    """Window text, progress messages and error texts must be wrapped in T(...)."""
    offenders = []
    for f in sorted((ROOT / "riffle").glob("*.py")) + [ROOT / "install.py"]:
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name = n.func.id if isinstance(n.func, ast.Name) else n.func.attr if isinstance(n.func, ast.Attribute) else ""
            cands = []
            if name in SINK_POS:
                cands += n.args[:1] if name not in ("showinfo", "showerror", "showwarning", "askyesno") else n.args[1:2]
            cands += [kw.value for kw in n.keywords if kw.arg in SINK_KW]
            for c in cands:
                t = _literal_text(c)
                if t is None or not re.search(r"[A-Za-zÇĞİÖŞÜçğıöşü]{2}", t):
                    continue
                if ALLOWED.match(t.strip()):
                    continue
                offenders.append(f"{f.name}:{n.lineno} {t!r}")
    assert not offenders, "T(...) ile sarılmamış metinler:\n" + "\n".join(offenders)


def _run(args, tmp_path, **env):
    e = {**os.environ, "RIFFLE_LANG": "en", "RIFFLE_AUTOCLOSE_MS": "300", "RIFFLE_NO_MENU_REFRESH": "1",
         "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "DISPLAY": "", **env}
    return subprocess.run([sys.executable, str(ROOT / "run.pyw"), *args], capture_output=True, text=True,
                          timeout=120, env=e, cwd=tmp_path)


def test_english_cli_list_and_output_names(tmp_path):
    import pymupdf
    r = _run(["--list"], tmp_path)
    assert r.returncode == 0
    assert "Merge PDFs" in r.stdout and "Compress (recommended)" in r.stdout
    assert not re.search(r"[çğıöşüÇĞİÖŞÜ]", r.stdout), r.stdout
    pdf = tmp_path / "report.pdf"
    d = pymupdf.open()
    for i in range(3):
        d.new_page().insert_text((72, 72), f"Page {i + 1}")
    d.save(pdf)
    r = _run(["compress", str(pdf)], tmp_path)
    assert (tmp_path / "report_compressed.pdf").exists(), r.stdout + r.stderr
    assert "Done" in r.stdout


def test_english_menus(tmp_path):
    code = ("import install, json; from riffle.i18n import T; "
            "print(json.dumps([[t, [[g and T(g), [T(a.label) for a in acts]] for g, acts in n]] "
            "for t, n in install.win_cascades('pdf')]))")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                       env={**os.environ, "RIFFLE_LANG": "en", "XDG_CONFIG_HOME": str(tmp_path)})
    assert r.returncode == 0, r.stderr
    import json
    menus = json.loads(r.stdout)
    flat = json.dumps(menus, ensure_ascii=False)
    assert "Merge PDFs" in flat and "PDF → Word" in flat and "Convert" in flat
    assert not re.search(r"[çğıöşüÇĞİÖŞÜ]", flat), flat


def test_suffix_localization(monkeypatch):
    from riffle import i18n
    from riffle.actions import localized_stem
    monkeypatch.setattr(i18n, "_lang", "en")
    assert localized_stem("Rapor_birlesik") == "Rapor_merged"
    assert localized_stem("x_dondurulmus_90") == "x_rotated_90"
    assert localized_stem("ders_4li") == "ders_4up"
    assert localized_stem("karsilastirma_a_b") == "comparison_a_b"
    monkeypatch.setattr(i18n, "_lang", "tr")
    assert localized_stem("Rapor_birlesik") == "Rapor_birlesik"


def test_language_choice(monkeypatch):
    from riffle import i18n
    monkeypatch.delenv("RIFFLE_LANG", raising=False)
    monkeypatch.setattr(i18n, "preference", lambda: "en")
    i18n.set_lang(None)
    try:
        assert i18n.lang() == "en" and i18n.T("Kaydet") == "Save"
        assert i18n.T("{0}: sayfa {1}/{2}", "a.pdf", 1, 3) == "a.pdf: page 1/3"
        monkeypatch.setattr(i18n, "preference", lambda: "auto")
        monkeypatch.setattr(i18n, "system_lang", lambda: "tr")
        i18n.set_lang(None)
        assert i18n.lang() == "tr" and i18n.T("Kaydet") == "Kaydet"
    finally:
        i18n.set_lang(None)
