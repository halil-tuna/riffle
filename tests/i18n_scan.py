"""Collects the strings to translate in the source code (for tests and translation upkeep).

    python tests/i18n_scan.py            # prints strings with missing translations
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = sorted((ROOT / "riffle").glob("*.py")) + [ROOT / "install.py"]


def msgids() -> dict[str, str]:
    """{msgid: 'file:line'}"""
    found: dict[str, str] = {}

    def add(s, f, n):
        if isinstance(s, str) and s.strip():
            found.setdefault(s, f"{f.name}:{n.lineno}")

    for f in FILES:
        if f.name in ("i18n.py", "i18n_en.py"):
            continue
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name = n.func.id if isinstance(n.func, ast.Name) else n.func.attr if isinstance(n.func, ast.Attribute) else ""
            if name == "T" and n.args and isinstance(n.args[0], ast.Constant):
                add(n.args[0].value, f, n)
            elif name == "Action":
                if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                    add(n.args[1].value, f, n)
                for kw in n.keywords:
                    if kw.arg == "order" and isinstance(kw.value, ast.Constant):
                        add(kw.value.value, f, n)
                    if kw.arg == "ask" and isinstance(kw.value, ast.Tuple):
                        for el in kw.value.elts[1:]:
                            if isinstance(el, ast.Constant) and el.value:
                                add(el.value, f, n)
        if f.name == "actions.py":                      # group names and the page range question
            for n in tree.body:
                if isinstance(n, ast.Assign):
                    tgt = [t.id for t in n.targets if isinstance(t, ast.Name)]
                    names = tgt if not isinstance(n.targets[0], ast.Tuple) else [e.id for e in n.targets[0].elts]
                    if any(x.startswith("G_") for x in names):
                        vals = n.value.elts if isinstance(n.value, ast.Tuple) else [n.value]
                        for v in vals:
                            if isinstance(v, ast.Constant):
                                add(v.value, f, n)
                    if "PAGES_Q" in names:
                        for v in n.value.elts[1:]:
                            if isinstance(v, ast.Constant) and v.value:
                                add(v.value, f, n)
        if f.name == "stamper.py":                      # colour names
            for n in tree.body:
                if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "COLORS" for t in n.targets):
                    for k in n.value.keys:
                        add(k.value, f, n)
    return found


if __name__ == "__main__":
    import sys
    sys.path.insert(0, str(ROOT))
    from riffle.i18n_en import CATALOG
    ids = msgids()
    missing = [m for m in ids if m not in CATALOG]
    print(f"{len(ids)} strings, {len(missing)} untranslated")
    for m in missing:
        print(f"{ids[m]:24} {m!r}")
