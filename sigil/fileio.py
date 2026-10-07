"""Small file helpers shared by the stores that keep state in JSON or notes."""
from __future__ import annotations

import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def file_lock(path) -> threading.RLock:
    """One re-entrant lock per file path, shared by every thread in this process.

    Windows will not let `os.replace` swap a file that another handle has open,
    and will not let a reader open one that is mid-swap. Every reader and writer
    of Sigil's stores is a thread of this one process, so serialising them here
    removes the collision outright rather than retrying around it. Take it for
    a whole read-modify-write to stop two threads clobbering each other's edit.
    """
    key = os.path.normcase(os.path.abspath(str(path)))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.RLock()
        return lock


def read_text_locked(path, encoding: str = "utf-8") -> str:
    """`Path.read_text` that cannot collide with an `atomic_write_text` swap."""
    with file_lock(path):
        return Path(path).read_text(encoding=encoding)


def atomic_write_text(path, text: str, encoding: str = "utf-8", *,
                      newline: str | None = None) -> None:
    """Write `text` to `path` so no reader ever sees a half-written file.

    The temp name is unique per process *and* thread: several stores used one
    shared `<name>.tmp`, so two simultaneous savers wrote into the same temp
    file and one of them renamed the other's half-finished bytes into place.

    On Windows `os.replace` fails with PermissionError (WinError 5 or 32) while
    another handle has the source or destination open. Other threads of this
    process are excluded by `file_lock`, so what is left is an outside process:
    a sync client, an editor, and above all antivirus or the search indexer,
    which open every freshly written file and can hold it for a noticeable
    fraction of a second (seen in tests, in %TEMP%). Retry for a couple
    of seconds before giving up rather than losing the save.
    """
    _sweep_stale()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline=newline) as fh:
            fh.write(text)
        with file_lock(path):
            for attempt in range(REPLACE_ATTEMPTS):
                try:
                    os.replace(tmp, path)
                    return
                except PermissionError:
                    if attempt == REPLACE_ATTEMPTS - 1:
                        raise
                    time.sleep(REPLACE_RETRY_SEC)
    finally:
        _discard(tmp)


REPLACE_ATTEMPTS = 40
REPLACE_RETRY_SEC = 0.05          # ~2 s in all

_STALE_TMP: list[Path] = []       # temp files we could not delete yet
_STALE_LOCK = threading.Lock()


def _discard(tmp: Path) -> None:
    """Best-effort delete of our own temp file; never raises.

    Called from a `finally`, so an exception here would replace the real error
    (the failed save) with a confusing one about the cleanup. A file that will
    not go (an antivirus scan still has it open) is remembered and retried on
    the next write, so nothing is left behind for good."""
    for _ in range(5):
        try:
            tmp.unlink(missing_ok=True)
            return
        except OSError:
            time.sleep(0.05)
    with _STALE_LOCK:
        _STALE_TMP.append(tmp)


def _sweep_stale() -> None:
    if not _STALE_TMP:
        return
    with _STALE_LOCK:
        pending, _STALE_TMP[:] = list(_STALE_TMP), []
    for tmp in pending:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            with _STALE_LOCK:
                _STALE_TMP.append(tmp)


def backup_copy(src, backup_dir) -> Path:
    """Copy `src` into `backup_dir` as `<stem>.<timestamp>.bak.md` and return it.

    The stamp is to the second, so two edits inside one second used to land on
    the same name and the second copy overwrote the first — leaving the
    "pre-edit" backup holding the *intermediate* state. Create exclusively and
    count up instead, so every backup survives.
    """
    src, backup_dir = Path(src), Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for n in range(1000):
        suffix = "" if n == 0 else f"-{n}"
        dest = backup_dir / f"{src.stem}.{stamp}{suffix}.bak.md"
        try:
            with open(src, "rb") as fin, open(dest, "xb") as fout:
                shutil.copyfileobj(fin, fout)
            return dest
        except FileExistsError:
            continue
    raise OSError(f"no free backup name for {src.name} at {stamp}")


def read_note_text(path) -> tuple[str, str]:
    """(text with LF newlines and no BOM, the newline style the file used).

    Read with newline="" so a CRLF file is recognised as CRLF; write back with
    `atomic_write_text(..., newline=<that style>)` so an edit does not silently
    flip a whole note's line endings."""
    with file_lock(path):
        with open(path, "r", encoding="utf-8", errors="ignore", newline="") as fh:
            text = fh.read()
    nl = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n").lstrip("﻿"), nl


def restrict_to_owner(path) -> bool:
    """Make `path` readable and writable by the current user only.

    POSIX: mode 600 (700 for a directory). Windows: drop inherited ACL entries
    and grant only the current user full control (icacls); on a directory the
    grant is inheritable, so files created inside it later start restricted.
    Returns False when it could not be done.
    """
    import subprocess
    p = Path(path)
    try:
        if os.name != "nt":
            os.chmod(p, 0o700 if p.is_dir() else 0o600)
            return True
        user = os.environ.get("USERNAME", "")
        if not user:
            return False
        domain = os.environ.get("USERDOMAIN", "")
        who = f"{domain}\\{user}" if domain else user
        grant = f"{who}:(OI)(CI)F" if p.is_dir() else f"{who}:F"
        # 1. /reset drops every explicit entry (some systems add them to new
        #    files), leaving only inherited ones; 2. /inheritance:r removes
        #    those and /grant:r adds the one entry that is left: the user.
        for args in (["/reset"], ["/inheritance:r", "/grant:r", grant]):
            done = subprocess.run(["icacls", str(p), *args], capture_output=True, timeout=15,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if done.returncode != 0:
                return False
        return True
    except (OSError, subprocess.SubprocessError):
        return False
