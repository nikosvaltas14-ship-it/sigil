"""What the study-guides pipeline remembers between runs: runs, caches, a lock.

Design rules:

* **JSON, not SQLite.** Every other Sigil store is a JSON file written with
  `atomic_write_text` and read with `read_text_locked` (blueprint §7.3); this
  one follows suit, so there is one way to inspect and repair state by hand.
* **A corrupt file loads as empty, and is kept.** A broken runs.json must never stop tonight's run. Before
  the first write over it, the broken file is moved aside to `*.corrupt`, so
  whatever was in it can still be read.
* **Idempotency is "a successful record exists".** Failed and skipped records
  are history, not blockers: a course that failed at 20:30 is retried by the
  next run, and one that succeeded is left alone unless the run is forced.
* **One run at a time, across processes.** `run_lock` covers a hand-started CLI run overlapping the
  scheduled one. It is an O_EXCL file, and a lock whose owner is dead or that is
  older than a night is taken over rather than blocking forever.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from ..fileio import atomic_write_text, file_lock, read_text_locked
from .models import FAILED, LOCK_PATH, OK, RUNS_PATH, CourseResult

log = logging.getLogger(__name__)

# runs.json keeps this many records, newest last. A semester is ~5 courses x
# ~75 class days, plus retries; this is several semesters of history.
MAX_RUN_RECORDS = 3000

# A lock older than this is from a run that died without cleaning up: a whole
# night's generation for five courses fits comfortably inside it.
STALE_LOCK_HOURS = 24      # a dead owner is detected sooner; this only covers a hung one


# ---- shared JSON load/save ------------------------------------------------------

def _load_json(path: Path, default):
    """(value, was_corrupt). Missing -> default; unreadable/wrong shape -> default."""
    try:
        raw = json.loads(read_text_locked(path))
    except FileNotFoundError:
        return default, False
    except (OSError, ValueError) as exc:
        log.warning("study guides: %s is unreadable (%s); starting empty", path.name, exc)
        return default, True
    if not isinstance(raw, type(default)):
        log.warning("study guides: %s has the wrong shape; starting empty", path.name)
        return default, True
    return raw, False


def _quarantine(path: Path) -> None:
    """Move a corrupt store aside before it is overwritten, so nothing is lost."""
    target = path.with_name(f"{path.name}.{datetime.now():%Y%m%d-%H%M%S}.corrupt")
    try:
        os.replace(path, target)
        log.warning("study guides: kept the corrupt %s as %s", path.name, target.name)
    except OSError as exc:
        log.warning("study guides: could not move corrupt %s aside (%s); "
                    "it will be overwritten", path.name, exc)


def _save_json(path: Path, value) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False))


# ---- runs -------------------------------------------------------------------

class RunState:
    """runs.json: one record per (date, course) attempt, newest last."""

    def __init__(self, path: Path = RUNS_PATH):
        self.path = Path(path)

    def already_succeeded(self, date_str: str, course_key: str) -> bool:
        return any(r.get("date") == date_str and r.get("course_key") == course_key
                   and r.get("status") == OK for r in self._records())

    def generation_failures(self, date_str: str, course_key: str) -> int:
        """How many attempts at (date, course) failed after the guide model was
        called — the expensive kind, which the hourly check stops retrying."""
        return sum(1 for r in self._records()
                   if r.get("date") == date_str and r.get("course_key") == course_key
                   and r.get("status") == FAILED
                   and (r.get("extra") or {}).get("generation_attempted"))

    def record(self, result: CourseResult) -> None:
        entry = _jsonable(asdict(result))
        entry["recorded_at"] = datetime.now().isoformat(timespec="seconds")
        with file_lock(self.path):
            data, corrupt = _load_json(self.path, {})
            if corrupt:
                _quarantine(self.path)
            runs = [r for r in data.get("runs", []) if isinstance(r, dict)]
            runs = [*runs, entry][-MAX_RUN_RECORDS:]
            _save_json(self.path, {"runs": runs})

    def for_date(self, date_str: str) -> list[dict]:
        """The latest record per course for `date_str`, in first-attempt order."""
        latest: dict[str, dict] = {}
        for rec in self._records():
            if rec.get("date") == date_str:
                latest[str(rec.get("course_key"))] = rec   # dict keeps first-insert order
        return list(latest.values())

    def previous_topics(self, course_key: str, before_date_str: str) -> list[str]:
        """Topics of successful guides before `before_date_str`, oldest first, unique."""
        dated = sorted(
            (str(r.get("date")), str(r.get("topic") or "").strip())
            for r in self._records()
            if r.get("course_key") == course_key and r.get("status") == OK
            and str(r.get("date") or "") < before_date_str
        )
        topics: list[str] = []
        for _, topic in dated:
            if topic and topic not in topics:
                topics.append(topic)
        return topics

    def files_used_before(self, course_key: str, before_date_str: str) -> set[str]:
        """Every file a successful guide of `course_key` was built from before
        `before_date_str`."""
        return {str(name)
                for r in self._records()
                if r.get("course_key") == course_key and r.get("status") == OK
                and str(r.get("date") or "") < before_date_str
                for name in (r.get("files_used") or [])}

    def between(self, start: str, end: str) -> list[dict]:
        """The latest record per (date, course) with start <= date <= end.

        Dates are "YYYY-MM-DD" strings, so they compare as text. Ascending by
        date; within a day, in first-attempt order (as `for_date`). Every
        status is returned — callers filter on "status" themselves.
        """
        latest: dict[tuple[str, str], dict] = {}
        for rec in self._records():
            day = str(rec.get("date") or "")
            if start <= day <= end:
                latest[(day, str(rec.get("course_key")))] = rec
        return sorted(latest.values(), key=lambda r: str(r.get("date")))

    def _records(self) -> list[dict]:
        data, _ = _load_json(self.path, {})
        return [r for r in data.get("runs", []) if isinstance(r, dict)]


def _jsonable(value):
    """Tuples -> lists and Paths -> str, recursively, for json.dumps."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    return value


# ---- caches -----------------------------------------------------------------

class JsonCache:
    """A small key -> JSON value store on disk (schedule and file caches).

    Every call reads the file, so two instances on one path never disagree;
    the files are small enough that this costs nothing next to a CLI call.
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def get(self, key: str) -> Any | None:
        data, _ = _load_json(self.path, {})
        return data.get(key)

    def put(self, key: str, value: Any) -> None:
        with file_lock(self.path):
            data, corrupt = _load_json(self.path, {})
            if corrupt:
                _quarantine(self.path)
            _save_json(self.path, {**data, key: _jsonable(value)})


# ---- run lock ---------------------------------------------------------------

class LockBusy(RuntimeError):
    """Another study-guides run is in progress."""


@contextmanager
def run_lock(path: Path = LOCK_PATH) -> Iterator[None]:
    """Hold an O_EXCL lock file for the duration of a run.

    Raises `LockBusy` when a live run holds it. A stale lock (owner dead, or
    older than STALE_LOCK_HOURS) is removed and taken over, once.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = {"pid": os.getpid(), "started": datetime.now().isoformat(timespec="seconds")}
    if not _try_create(path, stamp):
        holder = _read_lock(path)
        stale_why = _stale_reason(path, holder)
        if not stale_why:
            raise LockBusy(f"another study-guides run holds the lock "
                           f"(pid {holder.get('pid', '?')} since {holder.get('started', '?')})")
        log.warning("study guides: taking over a stale run lock (%s)", stale_why)
        _remove(path)
        if not _try_create(path, stamp):
            raise LockBusy("another study-guides run took the lock at the same moment")
    try:
        yield
    finally:
        _release(path, stamp["pid"])


def _try_create(path: Path, stamp: dict) -> bool:
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(stamp, fh)
    return True


def _read_lock(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _stale_reason(path: Path, holder: dict) -> str:
    """Why the lock can be taken over, or "" if its owner may still be running."""
    started = _lock_started(path, holder)
    if started is not None and datetime.now() - started > timedelta(hours=STALE_LOCK_HOURS):
        return f"older than {STALE_LOCK_HOURS} h"
    pid = holder.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and not _pid_alive(pid):
        return f"pid {pid} is not running"
    if not holder and started is None:
        return "lock file unreadable"
    return ""


def _lock_started(path: Path, holder: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(holder["started"]))
    except (KeyError, ValueError):
        pass
    try:   # a half-written lock file still has an mtime
        return datetime.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    """True if a process with this pid exists. Never signals it.

    On Windows `os.kill(pid, 0)` would *terminate* the process, so the check
    goes through OpenProcess/GetExitCodeProcess instead.
    """
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means it exists but belongs to someone else.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning("study guides: could not remove lock %s (%s)", path.name, exc)


def _release(path: Path, pid: int) -> None:
    """Remove the lock only if it is still ours (a takeover must not be undone)."""
    if _read_lock(path).get("pid") == pid:
        _remove(path)
    else:
        log.warning("study guides: run lock no longer ours at release; left in place")
