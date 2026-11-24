"""Speed: processing many files on all CPU cores, splitting a large PDF → Word into page chunks,
and the shared cancel flag for the Cancel button.

Processes start with "spawn" (the only way on Windows; on Linux it also avoids fork problems with the
window thread). In the packaged .exe, run.pyw calls multiprocessing.freeze_support().
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import threading
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from .i18n import T

CANCEL = threading.Event()        # the Cancel button sets this


class Cancelled(BaseException):
    """Cancel: a BaseException, because it must not get caught in the 'except Exception' blocks of the tools."""


def check_cancel() -> None:
    if CANCEL.is_set():
        raise Cancelled()


def in_worker() -> bool:
    return os.environ.get("RIFFLE_IN_WORKER") == "1"


def workers_for(n_jobs: int) -> int:
    cpu = os.cpu_count() or 2
    return max(1, min(n_jobs, cpu - 1 if cpu > 2 else cpu, 6))


def _init_worker(redirect: dict):
    os.environ["RIFFLE_IN_WORKER"] = "1"
    import warnings
    warnings.filterwarnings("ignore")
    import logging
    logging.getLogger().setLevel(logging.ERROR)
    from . import actions
    actions.OUTPUT_REDIRECT.update(redirect)


def _run_one(action_id: str, path: str, kwargs: dict):
    """Processes one file in a separate process: (outputs, notes, error)."""
    from .actions import BY_ID, ConversionError
    notes: list[str] = []
    try:
        outs = BY_ID[action_id].func([Path(path)], lambda m: m.startswith("ℹ ") and notes.append(m), **kwargs)
        return [str(o) for o in outs], notes, None
    except ConversionError as e:
        return [], notes, str(e)
    except Exception as e:
        return [], notes, f"{Path(path).name}: {type(e).__name__}: {e}"


def _kill(ex: ProcessPoolExecutor) -> None:
    for p in list(getattr(ex, "_processes", {}).values()):
        try:
            p.terminate()
        except Exception:
            pass
    ex.shutdown(wait=False, cancel_futures=True)


def run_files(action_id: str, paths: list[Path], kwargs: dict, progress) -> tuple[list[Path], list[str]]:
    """Processes the files in parallel. Sends progress('ℹ …') notes and 'n/N done' messages."""
    from . import actions
    outputs: list[Path] = []
    errors: list[str] = []
    n = len(paths)
    ctx = mp.get_context("spawn")
    ex = ProcessPoolExecutor(workers_for(n), mp_context=ctx, initializer=_init_worker,
                             initargs=(dict(actions.OUTPUT_REDIRECT),))
    pending = {ex.submit(_run_one, action_id, str(p), kwargs): p for p in paths}
    done_count = 0
    fallback: list[Path] = []
    progress(T("{0} dosya, {1} çekirdekte işleniyor…", n, workers_for(n)))
    try:
        while pending:
            if CANCEL.is_set():
                _kill(ex)
                raise Cancelled()
            done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for f in done:
                p = pending.pop(f)
                done_count += 1
                try:
                    outs, notes, err = f.result()
                except BrokenProcessPool:                  # the pool could not be created / crashed: this file is done sequentially
                    fallback.append(p)
                    done_count -= 1
                    continue
                except Exception as e:                    # crashed child process
                    outs, notes, err = [], [], f"{p.name}: {type(e).__name__}: {e}"
                outputs += [Path(o) for o in outs]
                for m in notes:
                    progress(m)
                if err:
                    errors.append(err)
                progress(T("{0}/{1} tamamlandı · {2}", done_count, n, p.name))
    finally:
        if not CANCEL.is_set():
            ex.shutdown(wait=True)
    if fallback:
        from .actions import BY_ID, ConversionError
        func = BY_ID[action_id].func
        for p in fallback:
            check_cancel()
            try:
                outputs += func([p], progress, **kwargs)
            except ConversionError as e:
                errors.append(str(e))
            except Exception as e:
                errors.append(f"{p.name}: {type(e).__name__}: {e}")
            done_count += 1
            progress(T("{0}/{1} tamamlandı · {2}", done_count, n, p.name))
    return outputs, errors


# ---------------------------------------------------------------- large PDF → Word

def _parse_chunk(args):
    src, pages, json_path, settings = args
    os.environ["RIFFLE_IN_WORKER"] = "1"
    import logging
    import warnings
    warnings.filterwarnings("ignore")
    logging.getLogger().setLevel(logging.ERROR)
    from pdf2docx import Converter
    from .textfix import patched_text
    logging.getLogger().setLevel(logging.ERROR)  # pdf2docx sets its own configuration on import
    with patched_text(src) as stats:
        cv = Converter(str(src))
        try:
            cv.load_pages()
            for page in cv.pages:
                page.skip_parsing = True
            for i in pages:
                cv.pages[i].skip_parsing = False
            s = dict(cv.default_settings)
            s.update(settings)
            cv.parse_document(**s).parse_pages(**s)
            data = cv.store()
        finally:
            cv.close()
    Path(json_path).write_text(json.dumps(data), encoding="utf-8")
    return dict(stats)


def word_parallel(src: Path, out: Path, n_pages: int, settings: dict, tmpdir: Path, progress) -> Counter:
    """Splits pdf2docx's parsing step into page chunks spread over the cores, builds the Word file in one place.
    (pdf2docx's own multiprocessing mode loses our patches, writes its JSON into the working folder and
    fails to read Turkish characters on Windows; hence our own version.)"""
    from pdf2docx import Converter
    from .textfix import patched_text
    k = workers_for(max(1, n_pages // 10))
    bounds = [round(i * n_pages / k) for i in range(k + 1)]
    chunks = [list(range(bounds[i], bounds[i + 1])) for i in range(k) if bounds[i] < bounds[i + 1]]
    jobs = [(str(src), c, str(tmpdir / f"parca{i}.json"), settings) for i, c in enumerate(chunks)]
    progress(T("Word'e çevriliyor: {0} sayfa, {1} çekirdekte", n_pages, len(chunks)))
    stats: Counter = Counter()
    ex = ProcessPoolExecutor(len(chunks), mp_context=mp.get_context("spawn"))
    try:
        pending = {ex.submit(_parse_chunk, j): j for j in jobs}
        while pending:
            if CANCEL.is_set():
                _kill(ex)
                raise Cancelled()
            done, _ = wait(pending, timeout=0.2, return_when=FIRST_COMPLETED)
            for f in done:
                pending.pop(f)
                stats.update(f.result())
                progress(T("Word'e çevriliyor: {0}/{1} parça hazır", len(jobs) - len(pending), len(jobs)))
    finally:
        if not CANCEL.is_set():
            ex.shutdown(wait=True)
    progress(T("Word dosyası kuruluyor…"))
    with patched_text(src):                      # the subscript patch is also needed while building the Word file
        cv = Converter(str(src))
        try:
            for _, _, jp, _ in jobs:
                cv.restore(json.loads(Path(jp).read_text(encoding="utf-8")))
            s = dict(cv.default_settings)
            s.update(settings)
            cv.make_docx(str(out), **s)
        finally:
            cv.close()
    return stats
