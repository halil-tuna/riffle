"""One Riffle main window at a time.

The first window holds an OS file lock (released by the OS even if Riffle crashes, so it never goes stale).
A second start (Start menu, the right-click "Settings…", the result window's "History" link) finds the lock
taken, leaves a small request file saying which tab to show and exits; the open window checks for that file
a few times a second, comes to the front and switches to that tab.

No sockets or named pipes (nothing a firewall could ask about), no extra dependency. If anything here fails,
the window simply opens as before.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

LOCK_NAME = "pencere.lock"
REQUEST_NAME = "pencere.istek"


class Instance:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._fh = None

    @property
    def request_file(self) -> Path:
        return self.dir / REQUEST_NAME

    def acquire(self) -> bool:
        """True if this process is the (only) main window now."""
        self.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.dir / LOCK_NAME, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh
        try:                                   # a request left over from before this window: not for us
            self.request_file.unlink()
        except OSError:
            pass
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None

    def request(self, tab: str) -> None:
        """Ask the open window to come to the front (and show `tab`)."""
        tmp = self.request_file.with_suffix(".tmp")
        tmp.write_text(tab or "", encoding="utf-8")
        os.replace(tmp, self.request_file)

    def poll(self) -> str | None:
        """The tab asked for by a second start, or None."""
        try:
            tab = self.request_file.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            self.request_file.unlink()
        except OSError:
            pass
        return tab
