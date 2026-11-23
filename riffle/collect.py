"""Çoklu seçim toplayıcı.

Windows Gezgini sağ tık menüsünde 5 dosya seçip bir komuta tıklayınca
programı 5 kez, her seferinde TEK dosyayla çalıştırır. Birleştirme gibi
işlemler için bunları tek bir işte toplamamız gerekiyor:

  1. Her süreç kendi dosya yolunu geçici klasöre bir 'bilet' olarak yazar.
  2. Kilidi ilk alan süreç 'lider' olur, yeni bilet gelmeyene kadar bekler,
     hepsini toplar ve işi tek başına yapar.
  3. Diğerleri biletlerinin lider tarafından alındığını görünce sessizce çıkar.

Linux'ta (Nemo %F) tüm dosyalar zaten tek seferde gelir; o durumda
toplayıcı devreye girmez.
"""

from __future__ import annotations

import os
import tempfile
import time
import uuid
from pathlib import Path

SETTLE = 0.6       # bu kadar süre yeni bilet gelmezse toplama biter (sn)
MAX_WAIT = 8.0     # lider en fazla bu kadar bekler
STALE_LOCK = 60.0  # çökmüş liderin kilidi bu kadar sonra geçersiz


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
    """Toplanan tüm dosyaları döndürür; bu süreç takipçiyse None döner."""
    if len(paths) != 1:
        return paths  # zaten toplu geldi (Linux / komut satırı)

    spool = _spool(action_id)
    lock = spool / "leader.lock"
    ticket = spool / f"{time.time_ns()}_{uuid.uuid4().hex}.ticket"
    ticket.write_text(os.path.abspath(paths[0]), encoding="utf-8")

    deadline = time.time() + MAX_WAIT + 5
    while time.time() < deadline:
        if not ticket.exists():
            return None  # lider biletimizi aldı
        if _try_lock(lock):
            return _lead(spool, lock)
        time.sleep(0.1)
    # Bir şeyler ters gitti: en azından kendi dosyamızı işleyelim
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
    # aynı dosya iki kez gelirse tekrar etme
    return list(dict.fromkeys(result))
