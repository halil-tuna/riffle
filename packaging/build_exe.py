#!/usr/bin/env python3
"""Tek klasörlük (.exe + kütüphaneler) paket üretir: PyInstaller.

    pip install pyinstaller
    python packaging/build_exe.py          # → dist/Riffle/Riffle(.exe)

Windows'ta çıktı Inno Setup ile kurulum dosyasına çevrilir (packaging/installer.iss).
Linux'ta da çalışır (test için).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
NAME = "Riffle"


def make_icon(path: Path) -> Path:
    """Riffle simgesi: yelpaze gibi açılmış (riffle edilen) üç sayfa."""
    from PIL import Image, ImageDraw
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sheets = [(-24, (47, 111, 221, 255), None), (-10, (217, 72, 15, 255), None),
              (6, (255, 255, 255, 255), (47, 111, 221, 255))]
    for angle, fill, outline in sheets:
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.rounded_rectangle((62, 24, 194, 214), 16, fill=fill, outline=outline, width=9 if outline else 0)
        if outline:
            for y in (70, 102, 134, 166):
                d.rounded_rectangle((88, y, 168 if y < 166 else 136, y + 12), 6, fill=(170, 182, 200, 255))
        layer = layer.rotate(angle, resample=Image.BICUBIC, center=(128, 236))
        im.alpha_composite(layer)
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    return path


def main() -> int:
    for stream in (sys.stdout, sys.stderr):   # Windows'ta boruya yazarken cp1252: ✔ ve Türkçe harfler patlamasın
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    try:
        import PyInstaller.__main__ as pyi
    except ImportError:
        print("Önce: pip install pyinstaller")
        return 1
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    icon = make_icon(build / "riffle.ico")
    sep = ";" if sys.platform == "win32" else ":"
    args = [
        str(ROOT / "run.pyw"),
        "--name", NAME,
        "--noconfirm", "--clean",
        "--windowed",                        # konsol penceresi açılmasın
        "--icon", str(icon),
        "--paths", str(ROOT),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(build / "pyinstaller"),
        "--specpath", str(build),
        "--collect-submodules", "riffle",    # araçlar çağrı anında importlib ile yükleniyor
        "--hidden-import", "install",        # --install-menu / --uninstall-menu
        "--collect-all", "pymupdf",
        "--collect-all", "pymupdf4llm",
        "--collect-submodules", "pdf2docx",
        "--collect-data", "pptx",            # python-pptx şablonu
        "--collect-all", "pillow_heif",
        "--collect-all", "tkinterdnd2",      # tkdnd ikili dosyaları
        "--add-data", f"{ROOT / 'riffle' / 'fonts'}{sep}riffle/fonts",   # gömülü yazı tipi (OFL)
        "--add-data", f"{ROOT / 'LICENSE'}{sep}.",
        "--add-data", f"{ROOT / 'README.md'}{sep}.",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
    ]
    pyi.run(args)
    out = ROOT / "dist" / NAME
    print(f"✔ Paket: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
