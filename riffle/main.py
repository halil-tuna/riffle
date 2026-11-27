"""Entry point.

Usage:
    python run.pyw <action> <file1> [file2 ...]
    python run.pyw --list            # list the actions
    python run.pyw --settings        # settings window
    python run.pyw --app             # the Riffle window, History tab (Start menu)
    Riffle.exe --install-menu / --uninstall-menu / --check   (packaged version)

The right-click menu calls this command.
"""

from __future__ import annotations

import sys
import warnings
from dataclasses import replace
from pathlib import Path
from .i18n import T

warnings.filterwarnings("ignore")


def main(argv: list[str] | None = None, _replay: dict | None = None) -> int:
    """_replay: options of a job run again from the History tab (--again): previous answers are offered again."""
    argv = list(sys.argv[1:] if argv is None else argv)

    # no stdout/stderr when running under pythonw; keep prints from crashing
    if sys.stdout is None:
        import io
        sys.stdout = sys.stderr = io.StringIO()
    # On Windows, when output goes to a pipe (command line, another program) the encoding is cp1252 and the
    # first print with Turkish letters aborted the job with UnicodeEncodeError ('Tamamlandı', 'Bilinmeyen işlem')
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
        # the .exe installer (Inno Setup) calls these flags; install.py does the same for a source install
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
    if argv[0] in ("--settings", "--app"):
        # --app: the Riffle window from the Start menu, History tab first; --settings: the General tab
        if argv[0] == "--app":
            _welcome()
        from .settings import run as settings_window
        settings_window(tab="history" if argv[0] == "--app" else "general")
        return 0
    if argv[0] == "--again":
        # "Convert again" from the History tab: the same action on the same files
        again = _again_args(argv[1:2])
        if again is None:
            from . import ui
            ui.notify(T("Hata"), T("Bu işlem geçmişte bulunamadı."))
            return 1
        return main(again[0], _replay=again[1])
    if argv[0] == "--list":
        for a in ACTIONS:
            print(f"{a.id:12} {T(a.label):28} {', '.join(T(e) if e.startswith('<') else e for e in a.extensions)}")
        return 0

    action_id, raw_paths = argv[0], argv[1:]
    action = BY_ID.get(action_id)
    if action is None:
        print(T("Bilinmeyen işlem: {0}", action_id))
        return 2

    from .collect import collect
    collected = raw_paths if _replay is not None else collect(action_id, raw_paths)
    if collected is None:
        return 0  # another process is collecting this job

    from . import ui

    # Filter out unsupported extensions (e.g. a .docx mixed in while merging)
    paths, skipped = [], []
    from .actions import accepts
    for p in map(Path, collected):
        if accepts(action, p):
            paths.append(p)
        else:
            skipped.append(p.name)
    if not paths:
        ui.notify(T("Uygun dosya yok"), ", ".join(skipped))
        return 1
    if action.id != "settings" and _replay is None:
        _welcome()                       # once: history on/off + where outputs go (before the output setting)
    replay = _replay or {}

    kwargs = dict(action.kwargs)
    if action.order and len(paths) > 1:
        # a small, fast file order window first; the pages are opened only if asked
        from .actions import sorted_by_name
        ok_text = T("Birleştir") if action.quick or action.id in ("merge", "wordmerge") else T("Tamam")
        start = paths if replay.get("ordered") else sorted_by_name(paths)     # again: the order used last time
        can_word = action.id in ("merge", "organize")                    # result: one PDF or one Word document
        answer = ui.ask_order(start, T(action.label), T(action.order), ok_text,
                              allow_pages=action.id != "wordmerge",     # Word merging should not go to PDF
                              **({"formats": True, "fmt": replay.get("format")} if can_word else {}))
        if answer is None:
            return 1                     # Cancel
        mode, paths = answer
        if mode == "word":               # everything converted to Word and joined as one .docx
            from .actions import _t
            kwargs["ordered"] = True
            kwargs["format"] = "word"
            action = replace(action, func=_t("merge_to_word", "officetools"), interactive=False, batch=True,
                             quick=None)
        if mode == "organize":           # "Edit page by page…": the organizer, in the chosen order
            from .tools import organize
            try:
                saved = organize(paths, print)
            except ConversionError as e:
                ui.notify(T("Hata"), str(e))
                return 1
            _record_window(action.id, paths, saved, {})
            return 0
        kwargs["ordered"] = True
        if action.quick:                 # merge directly instead of the organizer
            action = replace(action, func=action.quick, interactive=False, batch=True)
    if action.needs_password:
        pw = ui.ask_password(confirm=action.needs_password == "new")
        if pw is None:
            return 1
        kwargs["password"] = pw
    if action.ask:
        key, question, default = action.ask
        previous = replay.get(key)                     # again: last time's answer is offered
        answer = ui.ask_text(T(question), str(previous) if previous not in (None, "") else
                             T(default) if default else default)
        if answer is None:
            return 1
        kwargs[key] = answer

    from . import settings
    if action.id != "settings":
        _apply_output_setting(paths, settings, ui)

    if action.interactive:
        # opens its own window; shows the results itself
        try:
            outputs = action.func(paths, print, **kwargs)
        except ConversionError as e:
            ui.notify(T("Hata"), str(e))
            return 1
        if action.id not in ("search", "settings"):
            _record_window(action.id, paths, outputs, kwargs)
        _count_use(action.id, settings)
        return 0

    from . import parallel
    use_parallel = (not action.batch and len(paths) > 1 and action.id not in NO_PARALLEL
                    and settings.load()["parallel"] and parallel.workers_for(len(paths)) > 1)

    def job(progress):
        errors = [T("{0}: desteklenmeyen dosya, atlandı", n) for n in skipped]
        outputs: list[Path] = []
        hist = _history_job(action.id, paths, kwargs)      # never raises; does nothing if off/broken

        def prog(m):
            parallel.check_cancel()
            progress(m)
            if m.startswith("ℹ "):
                hist.note("info", m[2:])

        with hist:
            for e in errors:
                hist.note("warn", e)
            try:
                if use_parallel:
                    outs, errs = parallel.run_files(action.id, paths, kwargs, prog)
                    outputs += outs
                    errors += errs
                    for e in errs:
                        hist.note("error", e)
                else:
                    # Batch actions (merge, image → PDF) in one call, the rest file by file
                    batches = [paths] if action.batch else [[p] for p in paths]
                    for batch in batches:
                        parallel.check_cancel()
                        try:
                            outputs += action.func(batch, prog, **kwargs)
                        except ConversionError as e:
                            errors.append(str(e))
                            hist.note("error", str(e))
                        except Exception as e:
                            errors.append(f"{batch[0].name}: {type(e).__name__}: {e}")
                            hist.note("error", errors[-1])
            except parallel.Cancelled:
                errors.append(T("İptal edildi") + (T(" ({0} dosya tamamlanmıştı).", len(outputs)) if outputs else "."))
                hist.status("cancelled")
            hist.outputs(outputs)
        return outputs, errors

    n = len(paths)
    title = T("{0} — {1} dosya", T(action.label), n) if n > 1 else f"{T(action.label)} — {paths[0].name}"
    ui.run_with_window(title, job, cancel=parallel.CANCEL)
    if not parallel.CANCEL.is_set():
        _count_use(action.id, settings)
    return 0


# Actions that are already parallel inside (OCR splits pages over threads) or that batch everything in
# one LibreOffice call: keep them out of the process pool.
NO_PARALLEL = {"ocr", "office2pdf", "pdfa"}


def _apply_output_setting(paths, settings, ui) -> None:
    """If the setting is 'Desktop / this folder / ask', redirects the outputs there (instead of next to the source)."""
    try:
        target = settings.output_target(ask=ui.ask_folder)
    except Exception:
        target = None
    if target is None:
        return
    from . import actions
    for p in paths:
        try:                                           # folder actions also write their output to the parent folder
            actions.OUTPUT_REDIRECT[Path(p).resolve().parent] = target
        except OSError:
            pass


def _welcome() -> None:
    """First-run window (welcome.py) — guarded: whatever happens there, the job goes on."""
    try:
        from . import welcome
        welcome.maybe_show()
    except Exception:
        pass


def _again_args(arg: list[str]):
    """--again <job id> → ([action_id, *inputs], options) from the history, or None."""
    try:
        from . import history
        job = history.get(int(arg[0]))
        if job is None:
            return None
        return [job["action_id"], *job["inputs"]], job["options"]
    except Exception:
        return None


def _record_window(action_id: str, paths, outputs, kwargs) -> None:
    """The windows that save by themselves (page organizer, signature, form): one record if something was saved."""
    try:
        if outputs:
            from . import history
            history.record(action_id, paths, outputs, options=kwargs)
    except Exception:
        pass


class _NoHistory:
    """Stand-in when the history module cannot even be imported: every call does nothing."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: None


def _history_job(action_id: str, paths, kwargs):
    """Job history (history.py) is a guarded add-on: whatever goes wrong there, the conversion is not affected."""
    try:
        from . import history
        return history.safe_job(action_id, paths, kwargs)
    except Exception:
        return _NoHistory()


def _count_use(action_id: str, settings) -> None:
    """Usage counter; refresh the menu if the favourites changed (moved to the top)."""
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
