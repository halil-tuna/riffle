"""Giriş noktası.

Kullanım:
    python run.pyw <işlem> <dosya1> [dosya2 ...]
    python run.pyw --list            # işlemleri listele
    python run.pyw --settings        # ayarlar penceresi
    Riffle.exe --install-menu / --uninstall-menu / --check   (paketlenmiş sürüm)

Sağ tık menüsü bu komutu çağırır.
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import replace
from pathlib import Path

warnings.filterwarnings("ignore")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # pythonw ile çalışırken stdout/stderr yok; print'ler patlamasın
    if sys.stdout is None:
        import io
        sys.stdout = sys.stderr = io.StringIO()
    # Windows'ta çıktı bir boruya giderken (komut satırı, başka program) kodlama cp1252 oluyor ve Türkçe
    # harfli ilk print UnicodeEncodeError ile işi yarıda kesiyordu ('Tamamlandı', 'Bilinmeyen işlem')
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    from .actions import ACTIONS, BY_ID, ConversionError

    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv[0] in ("--install-menu", "--uninstall-menu", "--check"):
        # .exe kurulumu (Inno Setup) bu bayraklarla çağırır; kaynak kurulumda install.py aynısını yapar
        import install
        if argv[0] == "--check":
            install.check_optional()
        elif argv[0] == "--install-menu":
            install.install_windows() if install.IS_WIN else install.install_linux()
        else:
            install.uninstall_windows() if install.IS_WIN else install.uninstall_linux()
        return 0
    if argv[0] in ("--version", "-V"):
        from . import __version__
        print(__version__)
        return 0
    if argv[0] == "--settings":
        from .settings import run as settings_window
        settings_window()
        return 0
    if argv[0] == "--list":
        for a in ACTIONS:
            print(f"{a.id:12} {a.label:28} {', '.join(a.extensions)}")
        return 0

    action_id, raw_paths = argv[0], argv[1:]
    action = BY_ID.get(action_id)
    if action is None:
        print(f"Bilinmeyen işlem: {action_id}")
        return 2

    from .collect import collect
    collected = collect(action_id, raw_paths)
    if collected is None:
        return 0  # başka bir süreç bu işi topluyor

    from . import ui

    # Desteklenmeyen uzantıları ayıkla (ör. birleştirirken araya .docx karışırsa)
    paths, skipped = [], []
    from .actions import accepts
    for p in map(Path, collected):
        if accepts(action, p):
            paths.append(p)
        else:
            skipped.append(p.name)
    if not paths:
        ui.notify("Uygun dosya yok", ", ".join(skipped))
        return 1

    kwargs = dict(action.kwargs)
    if action.order and len(paths) > 1:
        # önce küçük, hızlı dosya sırası penceresi; sayfalar ancak istenirse açılır
        from .actions import sorted_by_name
        ok_text = "Birleştir" if action.quick or action.id == "merge" else "Tamam"
        answer = ui.ask_order(sorted_by_name(paths), action.label, action.order, ok_text)
        if answer is None:
            return 1                     # İptal
        mode, paths = answer
        if mode == "organize":           # "Sayfa sayfa düzenle…": düzenleyici, seçilen sırayla
            from .tools import organize
            try:
                organize(paths, print)
            except ConversionError as e:
                ui.notify("Hata", str(e))
                return 1
            return 0
        kwargs["ordered"] = True
        if action.quick:                 # düzenleyici yerine doğrudan birleştir
            action = replace(action, func=action.quick, interactive=False, batch=True)
    if action.needs_password:
        pw = ui.ask_password(confirm=action.needs_password == "new")
        if pw is None:
            return 1
        kwargs["password"] = pw
    if action.ask:
        key, question, default = action.ask
        answer = ui.ask_text(question, default)
        if answer is None:
            return 1
        kwargs[key] = answer

    from . import settings
    if action.id != "settings":
        _apply_output_setting(paths, settings, ui)

    if action.interactive:
        # kendi penceresini açar; sonuçları kendisi gösterir
        try:
            outputs = action.func(paths, print, **kwargs)
        except ConversionError as e:
            ui.notify("Hata", str(e))
            return 1
        _count_use(action.id, settings)
        return 0

    from . import parallel
    use_parallel = (not action.batch and len(paths) > 1 and action.id not in NO_PARALLEL
                    and settings.load()["parallel"] and parallel.workers_for(len(paths)) > 1)

    def job(progress):
        errors = [f"{n}: desteklenmeyen dosya, atlandı" for n in skipped]
        outputs: list[Path] = []

        def prog(m):
            parallel.check_cancel()
            progress(m)

        try:
            if use_parallel:
                outs, errs = parallel.run_files(action.id, paths, kwargs, prog)
                outputs += outs
                errors += errs
            else:
                # Toplu işlemler (birleştir, görsel→PDF) tek çağrıda, diğerleri dosya dosya
                batches = [paths] if action.batch else [[p] for p in paths]
                for batch in batches:
                    parallel.check_cancel()
                    try:
                        outputs += action.func(batch, prog, **kwargs)
                    except ConversionError as e:
                        errors.append(str(e))
                    except Exception as e:
                        errors.append(f"{batch[0].name}: {type(e).__name__}: {e}")
        except parallel.Cancelled:
            errors.append("İptal edildi" + (f" ({len(outputs)} dosya tamamlanmıştı)." if outputs else "."))
        return outputs, errors

    n = len(paths)
    title = f"{action.label} — {n} dosya" if n > 1 else f"{action.label} — {paths[0].name}"
    ui.run_with_window(title, job, cancel=parallel.CANCEL)
    if not parallel.CANCEL.is_set():
        _count_use(action.id, settings)
    return 0


# Zaten kendi içinde paralel çalışanlar (OCR sayfaları iş parçacıklarına böler) ya da tek LibreOffice
# çağrısında toplu işleyenler: süreç havuzuna sokma.
NO_PARALLEL = {"ocr", "office2pdf", "pdfa"}


def _apply_output_setting(paths, settings, ui) -> None:
    """Ayar 'Masaüstüne / şu klasöre / sor' ise çıktıları oraya yönlendirir (kaynağın yanı yerine)."""
    try:
        target = settings.output_target(ask=ui.ask_folder)
    except Exception:
        target = None
    if target is None:
        return
    from . import actions
    for p in paths:
        try:                                           # klasör işlemleri de çıktıyı üst klasöre yazar
            actions.OUTPUT_REDIRECT[Path(p).resolve().parent] = target
        except OSError:
            pass


def _count_use(action_id: str, settings) -> None:
    """Kullanım sayacı; sık kullanılanlar değiştiyse menüyü yenile (en üste çıkar)."""
    if action_id == "settings":
        return
    import os
    try:
        if settings.record_use(action_id) and not os.environ.get("RIFFLE_NO_MENU_REFRESH"):
            settings.refresh_menu()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
