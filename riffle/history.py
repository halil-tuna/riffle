"""Job history: which conversion was done when, from which files, to which files.

Kept only on this computer (gecmis.sqlite3 in the settings folder), never sent anywhere, no copies of the
files — only their paths, sizes and a few notes. Passwords are never stored (see SAFE_OPTIONS).

This is a guarded add-on: the history must never break a conversion. Every error of its own is swallowed
(`safe_job`, `record`), and when it is switched off in the settings it does nothing and creates no file.

Several Riffle processes can write at the same time (5 files selected in Explorer, two windows…), so the
storage is SQLite with short `BEGIN IMMEDIATE` transactions and a busy timeout. WAL is deliberately OFF:
SQLite 3.7.0–3.51.2 has the "WAL-reset" bug (concurrent writers + checkpoints can silently lose data) and
the Windows build's Python 3.12 ships SQLite 3.45.1. A rollback journal is safe there: journal_mode=TRUNCATE
(not DELETE: creating and deleting the journal file on every write is slow on Windows and wakes the virus scanner,
which kept other writers waiting). A writer that still finds the file locked tries again a few times.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import time
from datetime import datetime
from pathlib import Path

DB_NAME = "gecmis.sqlite3"
SCHEMA_VERSION = 1
DEFAULT_DAYS = 90
MAX_JOBS = 1000
PRUNE_EVERY = 20                 # prune old records on every 20th write
BUSY_MS = 5000
LOCK_RETRIES = 4                 # "database is locked" after BUSY_MS: try again (Windows, many writers at once)
JOURNAL = "TRUNCATE"
KEEP_BROKEN = 3                  # broken databases set aside (gecmis.bozuk-….sqlite3) kept at most
MAX_NOTES = 200
MAX_NOTE_LEN = 2000
STATUSES = ("ok", "warn", "error", "cancelled")
LEVELS = ("info", "warn", "error")

# Options that may be stored (and reused by "Convert again"). Anything else is dropped — above all
# "password", and "words" of the redaction tool (it may hold ID numbers, phone numbers…).
SAFE_OPTIONS = frozenset({"mode", "angle", "per", "level", "target", "size", "pages", "text", "ordered"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  action_id   TEXT NOT NULL,
  status      TEXT NOT NULL,
  duration_ms INTEGER,
  version     TEXT NOT NULL,
  options     TEXT
);
CREATE TABLE IF NOT EXISTS job_files (
  job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  role     TEXT NOT NULL,
  path     TEXT NOT NULL,
  size     INTEGER,
  mtime    REAL,
  file_id  TEXT
);
CREATE TABLE IF NOT EXISTS job_notes (
  job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  level  TEXT NOT NULL,
  text   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_time ON jobs(started_at);
CREATE INDEX IF NOT EXISTS job_files_job ON job_files(job_id);
CREATE INDEX IF NOT EXISTS job_notes_job ON job_notes(job_id);
"""


class HistoryUnavailable(Exception):
    """The history cannot be used right now (switched off, newer schema, unwritable folder…)."""


# ------------------------------------------------------------------ settings

def _settings() -> dict:
    from . import settings
    return settings.load()


def enabled() -> bool:
    try:
        return bool(_settings().get("history", True))
    except Exception:
        return False


def keep_days() -> int:
    try:
        return max(1, int(_settings().get("history_days", DEFAULT_DAYS)))
    except Exception:
        return DEFAULT_DAYS


def db_path() -> Path:
    from .stamp_model import config_dir
    return config_dir() / DB_NAME


# ------------------------------------------------------------------ connection

def _is_corrupt(e: BaseException) -> bool:
    """A damaged file — not a lock, a full disk or a read-only folder (those are OperationalError)."""
    msg = str(e).lower()
    return isinstance(e, sqlite3.DatabaseError) and any(s in msg for s in ("not a database", "malformed",
                                                                           "corrupt"))


def _open(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), timeout=BUSY_MS / 1000, isolation_level=None)
    try:
        con.execute(f"PRAGMA busy_timeout = {BUSY_MS}")
        mode = con.execute(f"PRAGMA journal_mode = {JOURNAL}").fetchone()
        if mode and str(mode[0]).lower() == "wal":       # could not leave WAL (another process holds it)
            raise HistoryUnavailable("journal_mode")
        con.execute("PRAGMA foreign_keys = ON")
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        if ver > SCHEMA_VERSION:                          # written by a newer Riffle: leave it alone
            raise HistoryUnavailable(f"schema {ver}")
        if ver < SCHEMA_VERSION:
            con.execute("BEGIN IMMEDIATE")
            try:                                          # (not executescript: it would COMMIT on its own)
                for stmt in filter(str.strip, _SCHEMA.split(";")):
                    con.execute(stmt)
                con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise
        return con
    except BaseException:
        con.close()
        raise


def _set_aside(path: Path) -> None:
    """Moves a broken database out of the way (gecmis.bozuk-<date>.sqlite3); keeps the last few."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.stem}.bozuk-{stamp}{path.suffix}")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.stem}.bozuk-{stamp}-{n}{path.suffix}")
        n += 1
    try:
        os.replace(path, target)
    except FileNotFoundError:
        pass                                              # another process already moved it
    journal = path.with_name(path.name + "-journal")
    try:
        os.replace(journal, target.with_name(target.name + "-journal"))
    except OSError:
        pass
    old = sorted(path.parent.glob(f"{path.stem}.bozuk-*{path.suffix}"))
    for p in old[:-KEEP_BROKEN]:
        for q in (p, p.with_name(p.name + "-journal")):
            try:
                q.unlink()
            except OSError:
                pass


def _still_corrupt(path: Path) -> bool:
    """Checks again on a fresh connection right before moving the file (another process may have replaced
    it with a new, healthy database in the meantime)."""
    try:
        con = sqlite3.connect(str(path), timeout=BUSY_MS / 1000)
        try:
            row = con.execute("PRAGMA quick_check").fetchone()
            return not row or row[0] != "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError as e:
        return _is_corrupt(e)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """An open, ready connection. A broken file is set aside and a new one started."""
    path = Path(path) if path else db_path()
    try:
        return _open(path)
    except sqlite3.DatabaseError as e:
        if not _is_corrupt(e) or not _still_corrupt(path):
            raise
    _set_aside(path)
    return _open(path)


def _is_locked(e: BaseException) -> bool:
    msg = str(e).lower()
    return isinstance(e, sqlite3.OperationalError) and ("locked" in msg or "busy" in msg)


def _tx(fn, path: Path | None = None):
    """Runs fn(con) in one short write transaction; if another process kept the file locked for longer than the
    busy timeout, tries again after a short random pause (so the waiting writers don't collide again)."""
    for attempt in range(LOCK_RETRIES + 1):
        try:
            return _tx_once(fn, path)
        except sqlite3.OperationalError as e:
            if not _is_locked(e) or attempt == LOCK_RETRIES:
                raise
            time.sleep(random.uniform(0.05, 0.4))


def _tx_once(fn, path: Path | None = None):
    """One write transaction. A broken file found mid-way: set aside, retried once."""
    for attempt in (0, 1):
        con = connect(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            try:
                result = fn(con)
                con.execute("COMMIT")
                return result
            except BaseException:
                try:
                    con.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        except sqlite3.DatabaseError as e:
            if attempt or not _is_corrupt(e):
                raise
            con.close()
            con = None
            p = Path(path) if path else db_path()
            if _still_corrupt(p):
                _set_aside(p)
        finally:
            if con is not None:
                con.close()


# ------------------------------------------------------------------ helpers

def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def safe_options(options: dict | None) -> dict:
    out = {}
    for k, v in (options or {}).items():
        if k in SAFE_OPTIONS and isinstance(v, (str, int, float, bool)) and not isinstance(v, bytes):
            out[k] = v
    return out


def _stat(p: Path) -> tuple[int | None, float | None]:
    try:
        st = os.stat(p)
        return (st.st_size if not Path(p).is_dir() else None), st.st_mtime
    except OSError:
        return None, None


def _abs(p) -> str:
    try:
        return os.path.abspath(os.fspath(p))
    except (TypeError, ValueError):
        return str(p)


def _status_for(notes: list[tuple[str, str]], outputs: list) -> str:
    levels = {lv for lv, _ in notes}
    if "error" in levels:
        return "warn" if outputs else "error"
    return "warn" if "warn" in levels else "ok"


# ------------------------------------------------------------------ writing

def _write(action_id: str, inputs, outputs, status: str, notes, options, started_at: str,
           finished_at: str, duration_ms: int | None, version: str | None, path: Path | None = None) -> int:
    if version is None:
        from . import __version__ as version
    status = status if status in STATUSES else "ok"
    opts = safe_options(options)
    files = [("in", _abs(p)) for p in inputs] + [("out", _abs(p)) for p in outputs]
    rows = []
    for role, p in dict.fromkeys(files):                  # same file twice → once
        size, mtime = _stat(p)
        rows.append((role, p, size, mtime))
    clean_notes = []
    for lv, text in list(notes)[:MAX_NOTES]:
        text = str(text).strip()
        if text:
            clean_notes.append((lv if lv in LEVELS else "info", text[:MAX_NOTE_LEN]))

    def fn(con):
        cur = con.execute(
            "INSERT INTO jobs(started_at, finished_at, action_id, status, duration_ms, version, options) "
            "VALUES (?,?,?,?,?,?,?)",
            (started_at, finished_at, str(action_id), status, duration_ms, str(version),
             json.dumps(opts, ensure_ascii=False) if opts else None))
        job_id = cur.lastrowid
        con.executemany("INSERT INTO job_files(job_id, role, path, size, mtime) VALUES (?,?,?,?,?)",
                        [(job_id, *r) for r in rows])
        con.executemany("INSERT INTO job_notes(job_id, level, text) VALUES (?,?,?)",
                        [(job_id, lv, t) for lv, t in clean_notes])
        if job_id % PRUNE_EVERY == 0:
            _prune(con, keep_days())
        return job_id

    return _tx(fn, path)


def record(action_id: str, inputs, outputs=(), status: str | None = None, notes=(), options=None,
           started_at: str | None = None, duration_ms: int | None = None, path: Path | None = None) -> int | None:
    """One call for the windows that save by themselves (page organizer, signature, forms).
    Never raises; returns the new record's id (None if not recorded)."""
    try:
        if not enabled():
            return None
        notes = [(lv, t) for lv, t in notes]
        outputs = list(outputs)
        now = _now()
        return _write(action_id, list(inputs), outputs, status or _status_for(notes, outputs), notes, options,
                      started_at or now, now, duration_ms, None, path)
    except Exception:
        return None


class Job:
    """Records one job when the `with` block ends:

        with history.Job(action_id, inputs, options) as h:
            …
            h.note("warn", message); h.outputs(paths); h.status("ok")

    The job's own exception passes through untouched (recorded as "error", or "cancelled" for Cancel);
    the history's errors are left to the caller — use safe_job() at the connection points.
    """

    def __init__(self, action_id: str, inputs, options: dict | None = None, path: Path | None = None):
        self.action_id = action_id
        self.inputs = list(inputs)
        self.options = safe_options(options)
        self.path = path
        self._outputs: list = []
        self._notes: list[tuple[str, str]] = []
        self._status: str | None = None
        self.started_at = _now()
        self._t0 = time.monotonic()
        self.job_id: int | None = None

    def __enter__(self):
        return self

    def note(self, level: str, text: str) -> None:
        if len(self._notes) < MAX_NOTES:
            self._notes.append((level, str(text)))

    def outputs(self, paths) -> None:
        self._outputs.extend(paths)

    def status(self, status: str) -> None:
        self._status = status

    def __exit__(self, et, ev, tb):
        status = self._status
        if et is not None and status is None:
            if et.__name__ == "Cancelled" or issubclass(et, KeyboardInterrupt):
                status = "cancelled"
            else:
                status = "error"
                self.note("error", f"{et.__name__}: {ev}")
        if status is None:
            status = _status_for(self._notes, self._outputs)
        self.job_id = _write(self.action_id, self.inputs, self._outputs, status, self._notes, self.options,
                             self.started_at, _now(), int((time.monotonic() - self._t0) * 1000), None,
                             self.path)
        return False


class _Noop:
    job_id = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __getattr__(self, name):
        return lambda *a, **k: None


class _Guarded:
    """Wraps a Job so that no history error can ever reach the conversion."""

    def __init__(self, job: Job):
        self._job = job

    @property
    def job_id(self):
        return self._job.job_id if self._job else None

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if self._job is not None:
            try:
                self._job.__exit__(et, ev, tb)
            except Exception:
                pass
        return False                                      # never hides the job's own exception

    def __getattr__(self, name):
        target = getattr(self.__dict__.get("_job"), name, None)
        if not callable(target):
            return lambda *a, **k: None

        def call(*a, **k):
            try:
                return target(*a, **k)
            except Exception:
                return None
        return call


def safe_job(action_id: str, inputs, options: dict | None = None):
    """The only entry point main uses: a recording Job, or a do-nothing object if the history is off or broken."""
    try:
        if not enabled():
            return _Noop()
        return _Guarded(Job(action_id, inputs, options))
    except Exception:
        return _Noop()


# ------------------------------------------------------------------ reading / housekeeping

def _prune(con: sqlite3.Connection, days: int) -> int:
    n = con.execute("DELETE FROM jobs WHERE julianday(started_at) < julianday('now', ?)",
                    (f"-{int(days)} days",)).rowcount
    n += con.execute("DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)",
                     (MAX_JOBS,)).rowcount
    return n


def prune(days: int | None = None, path: Path | None = None) -> int:
    """Deletes records older than `days` (setting: 90) and keeps at most MAX_JOBS. Returns how many went."""
    p = Path(path) if path else db_path()
    if not p.exists():
        return 0
    return _tx(lambda con: _prune(con, days or keep_days()), p)


def query(search: str = "", action: str | None = None, status: str | None = None, limit: int = 200,
          offset: int = 0, path: Path | None = None, job_id: int | None = None) -> list[dict]:
    """Newest first. search: part of a file name (in or out, case-insensitive)."""
    p = Path(path) if path else db_path()
    if not p.exists():
        return []
    where, args = [], []
    if job_id is not None:
        where.append("j.id = ?")
        args.append(int(job_id))
    if action:
        where.append("j.action_id = ?")
        args.append(action)
    if status:
        where.append("j.status = ?")
        args.append(status)
    if search:
        where.append("EXISTS (SELECT 1 FROM job_files f WHERE f.job_id = j.id AND f.path LIKE ? ESCAPE '\\')")
        s = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        args.append(f"%{s}%")
    sql = ("SELECT id, started_at, finished_at, action_id, status, duration_ms, version, options FROM jobs j"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY julianday(j.started_at) DESC, j.id DESC LIMIT ? OFFSET ?")
    con = connect(p)
    try:
        jobs = [dict(zip(("id", "started_at", "finished_at", "action_id", "status", "duration_ms", "version",
                          "options"), r)) for r in con.execute(sql, (*args, int(limit), int(offset)))]
        for j in jobs:
            j["options"] = json.loads(j["options"]) if j["options"] else {}
            files = con.execute("SELECT role, path, size, mtime FROM job_files WHERE job_id = ? ORDER BY rowid",
                                (j["id"],)).fetchall()
            j["inputs"] = [r[1] for r in files if r[0] == "in"]
            j["outputs"] = [r[1] for r in files if r[0] == "out"]
            j["files"] = [dict(zip(("role", "path", "size", "mtime"), r)) for r in files]
            j["notes"] = [tuple(r) for r in con.execute(
                "SELECT level, text FROM job_notes WHERE job_id = ? ORDER BY rowid", (j["id"],))]
        return jobs
    finally:
        con.close()


def get(job_id: int, path: Path | None = None) -> dict | None:
    """One record (same shape as query())."""
    found = query(job_id=job_id, limit=1, path=path)
    return found[0] if found else None


def remove(job_id: int, path: Path | None = None) -> bool:
    """Removes the record only (the files are not touched)."""
    p = Path(path) if path else db_path()
    if not p.exists():
        return False
    return _tx(lambda con: con.execute("DELETE FROM jobs WHERE id = ?", (int(job_id),)).rowcount > 0, p)


def clear(path: Path | None = None) -> int:
    """Deletes all records (the files are not touched)."""
    p = Path(path) if path else db_path()
    if not p.exists():
        return 0
    return _tx(lambda con: con.execute("DELETE FROM jobs").rowcount, p)


def count(path: Path | None = None) -> int:
    p = Path(path) if path else db_path()
    if not p.exists():
        return 0
    con = connect(p)
    try:
        return con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    finally:
        con.close()
