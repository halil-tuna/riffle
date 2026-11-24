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
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    im.resize((64, 64), Image.LANCZOS).save(path.with_suffix(".png"))     # pencere simgesi (tkinter)
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
    icon = ROOT / "riffle" / "assets" / "riffle.ico"
    if not icon.exists():
        make_icon(icon)
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
        "--add-data", f"{ROOT / 'riffle' / 'assets'}{sep}riffle/assets",  # simge (pencere + sağ tık menüsü)
        "--collect-data", "sv_ttk",          # Windows 11 teması (Tcl + görseller)
        "--collect-data", "docxcompose",     # Word birleştirmenin XML şablonları
        "--add-data", f"{ROOT / 'LICENSE'}{sep}.",
        "--add-data", f"{ROOT / 'README.md'}{sep}.",
        "--add-data", f"{ROOT / 'README.tr.md'}{sep}.",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
    ]
    pyi.run(args)
    out = ROOT / "dist" / NAME
    saved = prune(out)
    print(f"✔ Paket: {out}  (gereksiz dosyalar: {saved / 1e6:.0f} MB atıldı)")
    return 0


# Pakete giren ama hiç kullanılmayan dosyalar. Kullanılan sayfa düzeni modelleri, bütün test
# belgelerinde PDF → Markdown çalıştırılıp onnxruntime'ın yüklediği dosyalar izlenerek bulundu.
USED_LAYOUT_MODELS = {"feature_imf1.onnx", "layout_rf2.4.1+imf1.onnx", "table_grid_model_v4_ep.onnx"}


def _platform_tag() -> str:
    import platform
    osname = {"win32": "win", "darwin": "osx"}.get(sys.platform, "linux")
    m = platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x86" if m in ("x86", "i386", "i686") else "x64"
    return f"{osname}-{arch}"


def prune(out: Path) -> int:
    """PyInstaller çıktısından kullanılmayanları siler, kazanılan baytı döndürür."""
    internal = out / "_internal"
    doomed: list[Path] = []
    doomed += internal.glob("cv2/opencv_videoio_ffmpeg*")       # OpenCV video eklentisi (~41 MB, Windows)
    doomed += internal.glob("pymupdf/mupdf-devel")              # C başlıkları / .lib (derleme için)
    models = internal / "pymupdf" / "layout" / "resources" / "onnx"
    doomed += [f for f in models.glob("*.onnx") if f.name not in USED_LAYOUT_MODELS]
    tag = _platform_tag()
    doomed += [d for d in internal.glob("tkinterdnd2/tkdnd/*")    # başka işletim sistemlerinin tkdnd'si
               if d.is_dir() and not d.name.startswith(tag)]
    doomed += internal.rglob("*.pyi")                           # tip ipuçları
    # babel (docxcompose'un tarih biçimleri için): yalnızca Türkçe, İngilizce ve kök dil verisi kalsın
    doomed += [f for f in internal.glob("babel/locale-data/*.dat")
               if not f.name.split(".")[0].split("_")[0] in ("root", "tr", "en")]
    saved = 0
    for f in doomed:
        if not f.exists():
            continue
        if f.is_dir():
            saved += sum(x.stat().st_size for x in f.rglob("*") if x.is_file())
            shutil.rmtree(f)
        else:
            saved += f.stat().st_size
            f.unlink()
    return saved


if __name__ == "__main__":
    sys.exit(main())
