"""Multi-selection collector.

When you select 5 files in the Windows Explorer context menu and click a command,
Explorer runs the program 5 times, each time with ONE file. For actions like merging
we have to gather them into a single job:

  1. Every process writes its own file path to a temp folder as a 'ticket'.
  2. The first process to take the lock becomes the 'leader', waits until no new tickets
     arrive, collects them all and does the job alone.
  3. The others exit quietly once they see the leader has taken their ticket.

On Linux (Nemo %F) all files arrive at once anyway; the collector is not used
in that case.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from pathlib import Path

SETTLE = 0.6       # collection ends if no new ticket arrives for this long (s)
MAX_WAIT = 8.0     # the leader waits at most this long
STALE_LOCK = 60.0  # a crashed leader's lock becomes invalid after this long


def _spool(action_id: str) -> Path:
    d = Path(tempfile.gettempdir()) / "riffle" / action_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _try_lock(lock: Path) -> bool:
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(time.time()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > STALE_LOCK:
                lock.unlink()
        except OSError:
            pass
        return False


def _tickets(spool: Path) -> list[Path]:
    return sorted(spool.glob("*.ticket"))


def collect(action_id: str, paths: list[str]) -> list[str] | None:
    """Returns all collected files; returns None if this process is a follower."""
    if len(paths) != 1:
        return paths  # already arrived together (Linux / command line)

    spool = _spool(action_id)
    lock = spool / "leader.lock"
    ticket = spool / f"{time.time_ns()}_{uuid.uuid4().hex}.ticket"
    ticket.write_text(os.path.abspath(paths[0]), encoding="utf-8")

    deadline = time.time() + MAX_WAIT + 5
    while time.time() < deadline:
        if not ticket.exists():
            return None  # the leader took our ticket
        if _try_lock(lock):
            return _lead(spool, lock)
        time.sleep(0.1)
    # Something went wrong: at least process our own file
    ticket.unlink(missing_ok=True)
    return paths


def _lead(spool: Path, lock: Path) -> list[str]:
    start = time.time()
    seen = _tickets(spool)
    last_change = time.time()
    while time.time() - start < MAX_WAIT:
        time.sleep(0.1)
        now = _tickets(spool)
        if len(now) != len(seen):
            seen, last_change = now, time.time()
        elif time.time() - last_change >= SETTLE:
            break
    result: list[str] = []
    for t in seen:
        try:
            result.append(t.read_text(encoding="utf-8"))
            t.unlink()
        except OSError:
            pass
    lock.unlink(missing_ok=True)
    # don't repeat a file that arrives twice
    return list(dict.fromkeys(result))
