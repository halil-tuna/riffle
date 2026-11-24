"""New version notice: checks the latest release on GitHub at most once a week.

It only asks for the version number (api.github.com) and sends no files or information.
Can be turned off in Settings; with RIFFLE_NO_UPDATE_CHECK=1 (tests) it never goes online.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import __version__

REPO = "halil-tuna/riffle"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES = f"https://github.com/{REPO}/releases/latest"
INTERVAL = 7 * 24 * 3600


def _ver(s: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3]) or (0,)


def _state_file():
    from .stamp_model import config_dir
    return config_dir() / "guncelleme.json"


def _load() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def enabled() -> bool:
    if os.environ.get("RIFFLE_NO_UPDATE_CHECK"):
        return False
    try:
        from .settings import load
        return bool(load().get("check_updates", True))
    except Exception:
        return True


def fetch(timeout: float = 4.0) -> str | None:
    """Number of the latest release on GitHub (e.g. '0.9.1'); None without network."""
    import urllib.request
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"Riffle/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            tag = json.load(r).get("tag_name", "")
    except Exception:
        return None
    return tag.lstrip("vV") or None


def check(force: bool = False) -> str | None:
    """Returns the newer version number if there is one. Goes online only if the last check is over a week old."""
    if not force and not enabled():
        return None
    state = _load()
    latest = state.get("latest")
    if force or time.time() - state.get("checked", 0) > INTERVAL:
        got = fetch()
        if got:
            latest = got
            state = {"checked": time.time(), "latest": got}
            try:
                _state_file().write_text(json.dumps(state), encoding="utf-8")
            except OSError:
                pass
    if latest and _ver(latest) > _ver(__version__):
        return latest
    return None


def open_download() -> None:
    import webbrowser
    webbrowser.open(RELEASES)
