"""Job history (riffle/history.py) and its guarded connection to main: the history must never break a
conversion, must not create anything when switched off, and must survive several writers at once."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pymupdf
import pytest

from conftest import ROOT, make_text_pdf

import riffle
from riffle import history, settings


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A settings folder of its own for every test (the history database lives there)."""
    d = tmp_path / "cfg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(d))
    monkeypatch.setenv("APPDATA", str(d))
    return d


def _set(**kw):
    s = settings.load()
    s.update(kw)
    settings.save(s)


def _fake_window(monkeypatch, seen):
    from riffle import ui

    def run(title, job, cancel=None):
        seen["result"] = job(lambda m: seen.setdefault("msgs", []).append(m))
    monkeypatch.setattr(ui, "run_with_window", run)


# ---------------------------------------------------------------- recording and reading

def test_job_roundtrip(cfg, tmp_path):
    src = make_text_pdf(tmp_path / "rapor.pdf", pages=1, table=False)
    out = tmp_path / "rapor.docx"
    out.write_bytes(b"x" * 10)
    with history.Job("pdf2word", [src], {"mode": "flow", "password": "gizli", "words": "12345678901"}) as h:
        h.note("info", "13 not Word yorumu olarak eklendi.")
        h.outputs([out])
    jobs = history.query()
    assert len(jobs) == 1
    j = jobs[0]
    assert j["action_id"] == "pdf2word" and j["status"] == "ok" and j["version"] == riffle.__version__
    assert j["inputs"] == [str(src)] and j["outputs"] == [str(out)]
    assert j["options"] == {"mode": "flow"}                      # password and redaction words never stored
    assert j["notes"] == [("info", "13 not Word yorumu olarak eklendi.")]
    assert j["duration_ms"] >= 0 and j["started_at"] <= j["finished_at"]
    assert datetime.fromisoformat(j["started_at"]).tzinfo is not None
    assert [f["size"] for f in j["files"]] == [src.stat().st_size, 10]
    raw = history.db_path().read_bytes()
    assert b"gizli" not in raw and b"12345678901" not in raw


def test_statuses(cfg):
    with history.Job("a", []) as h:
        h.note("error", "bozuk")
    with history.Job("b", []) as h:
        h.note("error", "biri bozuk")
        h.outputs(["/tmp/biri.pdf"])
    with history.Job("c", []) as h:
        h.note("warn", "atlandı")
    with pytest.raises(ValueError):
        with history.Job("d", []):
            raise ValueError("patladı")
    from riffle.parallel import Cancelled
    with pytest.raises(Cancelled):
        with history.Job("e", []):
            raise Cancelled()
    with history.Job("f", []) as h:
        h.note("info", "bilgi")
    by = {j["action_id"]: j for j in history.query()}
    assert {k: v["status"] for k, v in by.items()} == {"a": "error", "b": "warn", "c": "warn", "d": "error",
                                                        "e": "cancelled", "f": "ok"}
    assert by["d"]["notes"] == [("error", "ValueError: patladı")]


def test_record_and_filters(cfg, tmp_path):
    history.record("merge", ["/x/a.pdf", "/x/b.pdf"], ["/x/Birlesik.pdf"])
    history.record("ocr", ["/x/100%_tarama.pdf"], [], status="error", notes=[("error", "okunamadı")])
    history.record("word", ["/x/ders_1.pdf"], ["/x/ders_1.docx"])
    assert [j["action_id"] for j in history.query()] == ["word", "ocr", "merge"]       # newest first
    assert [j["action_id"] for j in history.query(search="birlesik")] == ["merge"]    # case-insensitive
    assert [j["action_id"] for j in history.query(search="100%")] == ["ocr"]          # % is literal
    assert [j["action_id"] for j in history.query(search="s_1")] == ["word"]          # _ is literal
    assert history.query(search="s_") and not history.query(search="a_")
    assert [j["action_id"] for j in history.query(status="error")] == ["ocr"]
    assert [j["action_id"] for j in history.query(action="merge")] == ["merge"]
    assert len(history.query(limit=2)) == 2 and len(history.query(limit=2, offset=2)) == 1


def test_remove_and_clear_keep_files(cfg, tmp_path):
    f = tmp_path / "cikti.pdf"
    f.write_bytes(b"%PDF")
    a = history.record("rotate", [f], [f])
    history.record("rotate", [f], [f])
    assert history.remove(a) and not history.remove(a)
    assert history.count() == 1
    assert history.clear() == 1 and history.count() == 0
    assert f.read_bytes() == b"%PDF"
    con = sqlite3.connect(history.db_path())
    assert con.execute("SELECT COUNT(*) FROM job_files").fetchone()[0] == 0        # cascaded
    con.close()


def test_prune_by_age_and_count(cfg, monkeypatch):
    old = (datetime.now().astimezone() - timedelta(days=100)).isoformat(timespec="seconds")
    history.record("eski", ["/x/eski.pdf"], started_at=old)
    history.record("yeni", ["/x/yeni.pdf"])
    assert history.prune() == 1
    assert [j["action_id"] for j in history.query()] == ["yeni"]
    monkeypatch.setattr(history, "MAX_JOBS", 5)
    for i in range(8):
        history.record(f"j{i}", [])
    history.prune()
    assert [j["action_id"] for j in history.query()] == ["j7", "j6", "j5", "j4", "j3"]


def test_prune_runs_by_itself(cfg, monkeypatch):
    monkeypatch.setattr(history, "PRUNE_EVERY", 3)
    old = (datetime.now().astimezone() - timedelta(days=400)).isoformat(timespec="seconds")
    history.record("eski", [], started_at=old)
    history.record("b", [])
    assert history.count() == 2
    history.record("c", [])                                                     # 3rd write prunes
    assert [j["action_id"] for j in history.query()] == ["c", "b"]


def test_setting_days(cfg):
    _set(history_days=30)
    old = (datetime.now().astimezone() - timedelta(days=45)).isoformat(timespec="seconds")
    history.record("eski", [], started_at=old)
    assert history.prune() == 1


def test_no_wal(cfg):
    history.record("x", [])
    con = sqlite3.connect(history.db_path())
    assert con.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert con.execute("PRAGMA user_version").fetchone()[0] == history.SCHEMA_VERSION
    con.close()
    assert not Path(str(history.db_path()) + "-wal").exists()


# ---------------------------------------------------------------- several writers at once

_WRITER = r"""
import sys
sys.path.insert(0, sys.argv[1])
from riffle import history
ok = 0
for i in range(int(sys.argv[3])):
    with history.Job("w" + sys.argv[2], ["/x/%s_%d.pdf" % (sys.argv[2], i)], {"mode": "flow"}) as h:
        h.outputs(["/x/%s_%d.docx" % (sys.argv[2], i)])
        h.note("info", "not %d" % i)
    ok += h.job_id is not None
print(ok)
"""


def test_concurrent_writers(cfg):
    env = dict(os.environ, XDG_CONFIG_HOME=str(cfg), APPDATA=str(cfg))
    procs = [subprocess.Popen([sys.executable, "-c", _WRITER, str(ROOT), str(k), "50"], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for k in range(8)]
    outs = [p.communicate(timeout=300) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-500:] for o in outs]
    assert [o[0].strip() for o in outs] == ["50"] * 8
    con = sqlite3.connect(history.db_path())
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 400
    assert con.execute("SELECT COUNT(*) FROM job_files").fetchone()[0] == 800
    assert con.execute("SELECT COUNT(*) FROM job_notes").fetchone()[0] == 400
    assert con.execute("SELECT COUNT(DISTINCT action_id) FROM jobs").fetchone()[0] == 8
    con.close()


# ---------------------------------------------------------------- damaged / unusable storage

def test_broken_database_is_set_aside(cfg):
    history.record("ilk", [])
    db = history.db_path()
    db.write_bytes(b"bu bir veritabani degil" * 500)
    assert history.record("sonra", []) is not None
    assert [j["action_id"] for j in history.query()] == ["sonra"]
    aside = list(db.parent.glob("gecmis.bozuk-*.sqlite3"))
    assert len(aside) == 1 and aside[0].read_bytes().startswith(b"bu bir")


def test_broken_databases_kept_at_most_three(cfg):
    db = history.db_path()
    for i in range(5):
        db.write_bytes(b"cop" * 300 + bytes([i]))
        history.record(f"r{i}", [])
    assert len(list(db.parent.glob("gecmis.bozuk-*.sqlite3"))) == history.KEEP_BROKEN


def test_damaged_pages_are_set_aside(cfg):
    for i in range(30):
        history.record(f"r{i}", [f"/x/{'uzun_ad_' * 20}{i}.pdf"])
    db = history.db_path()
    data = bytearray(db.read_bytes())
    for off in range(4096, len(data), 4096):          # trash every page after the first
        data[off:off + 200] = b"\xff" * 200
    db.write_bytes(bytes(data))
    assert history.record("yeni", []) is not None
    assert history.query()[0]["action_id"] == "yeni"


def test_newer_schema_left_alone(cfg):
    db = history.db_path()
    con = sqlite3.connect(db)
    con.execute("PRAGMA user_version = 99")
    con.execute("CREATE TABLE gelecek(x)")
    con.commit()
    con.close()
    before = db.read_bytes()
    assert history.record("x", []) is None
    assert db.read_bytes() == before


def test_unwritable_place_is_silent(cfg, monkeypatch, tmp_path):
    folder = tmp_path / "klasor"
    folder.mkdir()
    monkeypatch.setattr(history, "db_path", lambda: folder)          # a folder, can't be opened as a database
    assert history.record("x", []) is None
    h = history.safe_job("x", [])
    with h:
        h.note("info", "a")
        h.outputs([tmp_path])
    assert h.job_id is None


def test_off_creates_nothing(cfg):
    _set(history=False)
    assert not history.enabled()
    with history.safe_job("rotate", ["/x/a.pdf"]) as h:
        h.note("info", "x")
    assert history.record("rotate", ["/x/a.pdf"]) is None
    assert not history.db_path().exists()
    assert history.query() == [] and history.count() == 0


# ---------------------------------------------------------------- connection to main

def test_main_records_one_job(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    _set(parallel=False)
    files = [make_text_pdf(tmp_path / f"m{i}.pdf", pages=1, table=False) for i in range(2)]
    seen = {}
    _fake_window(monkeypatch, seen)
    assert main_mod.main(["rotate", *map(str, files)]) == 0
    outs, errs = seen["result"]
    jobs = history.query()
    assert len(jobs) == 1 and not errs
    j = jobs[0]
    assert j["action_id"] == "rotate" and j["status"] == "ok"
    assert j["options"] == {"angle": 90}
    assert sorted(j["inputs"]) == sorted(os.path.abspath(f) for f in files)
    assert sorted(j["outputs"]) == sorted(os.path.abspath(o) for o in outs) and len(outs) == 2


def test_main_records_info_notes(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    src = make_text_pdf(tmp_path / "kucult.pdf", pages=2, image=True)
    seen = {}
    _fake_window(monkeypatch, seen)
    main_mod.main(["compress", str(src)])
    infos = [m[2:] for m in seen["msgs"] if m.startswith("ℹ ")]
    j = history.query()[0]
    assert infos and j["notes"] == [("info", t) for t in infos] and j["status"] == "ok"


def test_main_records_errors_and_skips(cfg, tmp_path, monkeypatch, garbage):
    from riffle import main as main_mod
    _set(parallel=False)
    good = make_text_pdf(tmp_path / "iyi.pdf", pages=1, table=False)
    txt = tmp_path / "not.txt"
    txt.write_text("x")
    seen = {}
    _fake_window(monkeypatch, seen)
    main_mod.main(["rotate", str(garbage), str(good), str(txt)])
    j = history.query()[0]
    levels = [lv for lv, _ in j["notes"]]
    assert j["status"] == "warn" and "error" in levels and "warn" in levels      # 1 failed, 1 skipped, 1 done
    assert len(j["outputs"]) == 1
    main_mod.main(["rotate", str(garbage)])
    j = history.query()[0]
    assert j["status"] == "error" and not j["outputs"]


def test_main_records_cancel(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    from riffle import ui
    _set(parallel=False)
    files = [make_text_pdf(tmp_path / f"m{i}.pdf", pages=1, table=False) for i in range(3)]

    def run(title, job, cancel=None):
        def progress(m):
            if "m1" in m:
                cancel.set()
        job(progress)
    monkeypatch.setattr(ui, "run_with_window", run)
    try:
        main_mod.main(["rotate", *map(str, files)])
    finally:
        from riffle import parallel
        parallel.CANCEL.clear()
    j = history.query()[0]
    names = [Path(o).name for o in j["outputs"]]
    assert j["status"] == "cancelled" and "m0_dondurulmus_90.pdf" in names and "m2_dondurulmus_90.pdf" not in names


def test_main_parallel_records_worker_notes(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    from riffle import parallel
    _set(parallel=True)
    monkeypatch.setattr(parallel, "workers_for", lambda n: 2)
    files = [make_text_pdf(tmp_path / f"p{i}.pdf", pages=2, image=True) for i in range(2)]
    seen = {}
    _fake_window(monkeypatch, seen)
    main_mod.main(["compress", *map(str, files)])
    j = history.query()[0]
    assert len(history.query()) == 1 and len(j["outputs"]) == 2 and len(j["inputs"]) == 2
    assert len([n for n in j["notes"] if n[0] == "info"]) == 2


def test_main_off_creates_nothing(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    _set(history=False)
    src = make_text_pdf(tmp_path / "a.pdf", pages=1, table=False)
    seen = {}
    _fake_window(monkeypatch, seen)
    main_mod.main(["rotate", str(src)])
    assert seen["result"][0] and not history.db_path().exists()


# ---------------------------------------------------------------- guarantee: broken history, same conversions

def _break_import(monkeypatch):
    monkeypatch.delattr(riffle, "history", raising=False)
    monkeypatch.setitem(sys.modules, "riffle.history", None)          # "from . import history" → ImportError


def _boom(*a, **k):
    raise RuntimeError("geçmiş bozuk")


class _BadJob:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def note(self, *a):
        raise RuntimeError("not yazılamadı")

    outputs = status = note

    def __exit__(self, *exc):
        raise sqlite3.OperationalError("disk is full")


BREAKAGES = {
    "import": _break_import,
    "safe_job": lambda mp: mp.setattr(history, "safe_job", _boom),
    "enabled": lambda mp: mp.setattr(history, "enabled", _boom),
    "Job": lambda mp: mp.setattr(history, "Job", _BadJob),
    "write": lambda mp: mp.setattr(history, "_write", _boom),
    "connect": lambda mp: mp.setattr(history, "connect", _boom),
}

# a representative set of jobs that go through main: one file, several files, a batch, parallel, Word
CASES = [
    ("rotate", 1, False),
    ("merge", 3, False),
    ("compress", 2, True),
    ("pdf2word", 1, False),
]


def _run_case(tmp_path, monkeypatch, action, n, parallel_on):
    from riffle import main as main_mod
    from riffle import parallel
    _set(parallel=parallel_on)
    if parallel_on:
        monkeypatch.setattr(parallel, "workers_for", lambda k: 2)
    tmp_path.mkdir(parents=True, exist_ok=True)
    files = [make_text_pdf(tmp_path / f"belge {i}.pdf", pages=2, table=True, image=action == "compress")
             for i in range(n)]
    seen = {}
    _fake_window(monkeypatch, seen)
    rc = main_mod.main([action, *map(str, files)])
    outs, errs = seen["result"]
    pages = []
    for o in outs:
        assert o.exists() and o.stat().st_size > 0
        if o.suffix == ".pdf":
            with pymupdf.open(o) as d:
                pages.append(len(d))
    return rc, sorted(o.name for o in outs), errs, pages


@pytest.mark.parametrize("breakage", list(BREAKAGES))
@pytest.mark.parametrize("action,n,parallel_on", CASES)
def test_broken_history_does_not_change_conversions(cfg, tmp_path, monkeypatch, breakage, action, n,
                                                    parallel_on):
    reference = _run_case(tmp_path / "saglam", monkeypatch, action, n, parallel_on)
    assert reference[0] == 0 and reference[1] and not reference[2]
    recorded = history.count()
    assert recorded == 1
    with monkeypatch.context() as mp:
        BREAKAGES[breakage](mp)
        broken = _run_case(tmp_path / "bozuk", mp, action, n, parallel_on)
    assert broken == reference
    assert history.count() == recorded                     # the broken history really did not record


# ---------------------------------------------------------------- stage 3: windows that save by themselves, again

def test_get_one(cfg):
    a = history.record("rotate", ["/x/a.pdf"], ["/x/b.pdf"], options={"angle": 90})
    history.record("merge", ["/x/c.pdf"], [])
    j = history.get(a)
    assert j["action_id"] == "rotate" and j["inputs"] == ["/x/a.pdf"] and j["options"] == {"angle": 90}
    assert history.get(999) is None


def test_interactive_windows_record(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    from riffle.actions import BY_ID
    src = make_text_pdf(tmp_path / "imza.pdf", pages=1, table=False)
    out = tmp_path / "imza_imzali.pdf"
    monkeypatch.setattr(BY_ID["sign"], "func", lambda paths, progress, **k: [out])
    main_mod.main(["sign", str(src)])
    j = history.query()[0]
    assert j["action_id"] == "sign" and j["outputs"] == [str(out)] and j["status"] == "ok"
    monkeypatch.setattr(BY_ID["form"], "func", lambda paths, progress, **k: [])     # closed without saving
    main_mod.main(["form", str(src)])
    monkeypatch.setattr(BY_ID["search"], "func", lambda paths, progress, **k: [tmp_path])
    main_mod.main(["search", str(tmp_path)])
    assert history.count() == 1


def test_organizer_from_order_window_records(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    from riffle import tools, ui
    files = [make_text_pdf(tmp_path / f"s{i}.pdf", pages=1, table=False) for i in range(2)]
    out = tmp_path / "Birlesik.pdf"
    monkeypatch.setattr(ui, "ask_order", lambda paths, *a, **k: ("organize", list(reversed(paths))))
    monkeypatch.setattr(tools, "organize", lambda paths, progress, **k: [out])
    main_mod.main(["organize", *map(str, files)])
    j = history.query()[0]
    assert j["action_id"] == "organize" and j["inputs"] == [str(files[1]), str(files[0])]
    assert j["outputs"] == [str(out)]


def test_again_runs_the_same_job(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    _set(parallel=False)
    src = make_text_pdf(tmp_path / "tekrar.pdf", pages=1, table=False)
    seen = {}
    _fake_window(monkeypatch, seen)
    main_mod.main(["rotatel", str(src)])
    first = history.query()[0]
    assert main_mod.main(["--again", str(first["id"])]) == 0
    jobs = history.query()
    assert len(jobs) == 2 and jobs[0]["action_id"] == "rotatel" and jobs[0]["inputs"] == first["inputs"]
    assert jobs[0]["options"] == {"angle": 270}
    assert Path(jobs[0]["outputs"][0]).name != Path(first["outputs"][0]).name        # a new file, not overwritten
    assert main_mod.main(["--again", "12345"]) == 1


def test_again_offers_previous_answers(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    from riffle import ui
    src = make_text_pdf(tmp_path / "filigran.pdf", pages=1, table=False)
    jid = history.record("watermark", [src], [], options={"text": "GİZLİ"})
    asked = []
    monkeypatch.setattr(ui, "ask_text", lambda q, default="": (asked.append(default), None)[1])   # then Cancel
    assert main_mod.main(["--again", str(jid)]) == 1
    assert asked == ["GİZLİ"]
    main_mod.main(["watermark", str(src)])                        # a normal run still offers the default
    assert asked[-1] == "TASLAK"


def test_again_never_has_the_password(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    from riffle import ui
    src = make_text_pdf(tmp_path / "sifre.pdf", pages=1, table=False)
    with history.Job("protect", [src], {"password": "çok-gizli"}):
        pass
    asked = []
    monkeypatch.setattr(ui, "ask_password", lambda confirm=False: (asked.append(confirm), None)[1])
    assert main_mod.main(["--again", str(history.query()[0]["id"])]) == 1
    assert asked == [True]


def test_again_keeps_the_order(cfg, tmp_path, monkeypatch):
    from riffle import main as main_mod
    from riffle import ui
    files = [make_text_pdf(tmp_path / f"{n}.pdf", pages=1, table=False) for n in ("a", "b", "c")]
    jid = history.record("merge", [files[2], files[0], files[1]], [], options={"ordered": True})
    shown = []
    monkeypatch.setattr(ui, "ask_order", lambda paths, *a, **k: (shown.append([p.name for p in paths]), None)[1])
    main_mod.main(["--again", str(jid)])
    main_mod.main(["merge", *map(str, files)])
    assert shown == [["c.pdf", "a.pdf", "b.pdf"], ["a.pdf", "b.pdf", "c.pdf"]]
