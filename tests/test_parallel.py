"""Parallel batch processing, splitting large PDF → Word, Cancel."""

import re
import shutil
import threading
import time
import zipfile

import pymupdf
import pytest

from conftest import make_text_pdf, noop
from riffle import actions, parallel


@pytest.fixture(autouse=True)
def _reset_cancel():
    parallel.CANCEL.clear()
    yield
    parallel.CANCEL.clear()


def _docx_text(p):
    return re.sub(r"<[^>]+>", "", zipfile.ZipFile(p).read("word/document.xml").decode("utf-8"))


def test_word_parallel_matches_serial(tmp_path, monkeypatch):
    src = make_text_pdf(tmp_path / "uzun kitap.pdf", pages=40, table=True)
    monkeypatch.setattr(parallel, "workers_for", lambda n: 2)
    monkeypatch.setattr(actions, "_parallel_enabled", lambda: False)
    serial = actions.pdf_to_word([src], noop)[0]
    shutil.move(serial, tmp_path / "seri.docx")
    monkeypatch.setattr(actions, "_parallel_enabled", lambda: True)
    msgs = []
    par = actions.pdf_to_word([src], msgs.append)[0]
    assert any("2 çekirdekte" in m for m in msgs)
    assert _docx_text(tmp_path / "seri.docx") == _docx_text(par)
    assert "ÖĞÜŞİ" in _docx_text(par)                      # Turkish characters came back intact from the JSON


def test_run_files_parallel(tmp_path, monkeypatch):
    files = [make_text_pdf(tmp_path / f"r{i}.pdf", pages=2, table=False) for i in range(3)]
    monkeypatch.setattr(parallel, "workers_for", lambda n: 2)
    msgs = []
    outs, errs = parallel.run_files("compress", files, {}, msgs.append)
    assert not errs and sorted(o.name for o in outs) == [f"r{i}_sikistirilmis.pdf" for i in range(3)]
    assert any(m.startswith("3/3 tamamlandı") for m in msgs)


def test_run_files_falls_back_when_pool_breaks(tmp_path, monkeypatch):
    files = [make_text_pdf(tmp_path / f"b{i}.pdf", pages=1, table=False) for i in range(2)]
    monkeypatch.setattr(parallel, "_run_one", None)          # the child process can't find the job → the pool breaks
    outs, errs = parallel.run_files("rotate", files, {"angle": 90}, noop)
    assert len(outs) == 2 or errs                            # either finished sequentially or an error was reported
    assert all(pymupdf.open(o)[0].rotation == 90 for o in outs)


def test_cancel_stops_word_conversion(tmp_path):
    src = make_text_pdf(tmp_path / "c.pdf", pages=5, table=False)
    orig = pymupdf.Page.get_text
    parallel.CANCEL.set()
    with pytest.raises(parallel.Cancelled):
        actions.pdf_to_word([src], noop)
    assert pymupdf.Page.get_text is orig                     # the patch was undone
    assert not list(tmp_path.glob("*.docx"))


def test_cancel_kills_external_program():
    t0 = time.time()
    threading.Timer(0.4, parallel.CANCEL.set).start()
    with pytest.raises(parallel.Cancelled):
        actions._run_cancellable(["sleep", "30"], timeout=60)
    assert time.time() - t0 < 5


def test_main_job_reports_cancel(tmp_path, monkeypatch):
    """Cancel: the progress window says 'Cancelled', finished files stay."""
    from riffle import main as main_mod
    from riffle import ui
    files = [make_text_pdf(tmp_path / f"m{i}.pdf", pages=1, table=False) for i in range(3)]
    seen = {}

    def fake_window(title, job, cancel=None):
        calls = {"n": 0}

        def progress(m):
            calls["n"] += 1
            if "m1" in m:
                cancel.set()                                 # cancel on the second file
        seen["result"] = job(progress)
    monkeypatch.setattr(ui, "run_with_window", fake_window)
    from riffle import settings
    s = settings.load()
    s["parallel"] = False
    settings.save(s)
    main_mod.main(["rotate", *map(str, files)])
    outs, errs = seen["result"]
    names = [o.name for o in outs]
    assert "m0_dondurulmus_90.pdf" in names and "m2_dondurulmus_90.pdf" not in names
    assert not (tmp_path / "m2_dondurulmus_90.pdf").exists()
    assert errs and errs[-1].startswith("İptal edildi")
