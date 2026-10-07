"""Putting a finished guide PDF into the vault, safely.

Design rules:

* **Never overwrite, never delete.** The PDF is copied with exclusive create
  (`open(..., "xb")`); if the name is taken it becomes " (2)", " (3)", ... A
  file you have annotated must never be replaced by tonight's regeneration.
* **Only inside the course folder.** The folder and the filename both come
  from config and from a model-written topic, so both are checked: no absolute
  paths, no "..", no separators in the filename, and the resolved target must
  still sit under the vault root. A bad name is refused, not "fixed".
* **Windows-safe names.** The topic is free Greek text from a course page;
  `safe_filename` strips what Windows forbids and trims it so the full path
  stays under MAX_PATH_CHARS (classic MAX_PATH is 260, and Obsidian and sync
  clients choke earlier than the filesystem does).
* **This module does not check `allow_vault_writes`.** The caller (run.py)
  decides whether to file at all; this module only decides how.
"""
from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from datetime import date
from pathlib import Path, PurePath

log = logging.getLogger(__name__)

# Full path length cap, with headroom under Windows' 260.
MAX_PATH_CHARS = 240
# The topic part of a filename; longer topics are cut at a word boundary.
MAX_TOPIC_CHARS = 80
# How many " (n)" suffixes to try before giving up on a name.
MAX_COLLISIONS = 99
# A stem trimmed to fit the path cap never gets shorter than this.
MIN_STEM_CHARS = 20
PDF_SUFFIX = ".pdf"

# Characters Windows forbids in names. ":" and the slashes read as separators
# in a topic ("Κεφ. 3: Thévenin", "AC/DC"), so they become a dash; the rest go.
_DASHED = str.maketrans({":": " - ", "/": "-", "\\": "-", "|": "-"})
_DROPPED_RE = re.compile(r'[?*"<>\x00-\x1f\x7f]')
_SPACES_RE = re.compile(r"\s+")
_FORBIDDEN_RE = re.compile(r'[:/\\?*"<>|\x00-\x1f\x7f]')
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL",
                   *(f"COM{n}" for n in range(1, 10)), *(f"LPT{n}" for n in range(1, 10))}


def safe_filename(day: str, course_name: str, topic: str) -> str:
    """"YYYY-MM-DD - <Course> - <short topic>.pdf", safe on Windows.

    Raises ValueError when `day` is not an ISO date or nothing is left of the
    course name, since either would produce a misfiled PDF.
    """
    try:
        date.fromisoformat(day)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not a YYYY-MM-DD date: {day!r}") from exc
    course = _clean(course_name)
    if not course:
        raise ValueError(f"course name {course_name!r} has nothing filename-safe in it")
    short = _trim_words(_clean(topic), MAX_TOPIC_CHARS)
    parts = [day, course] + ([short] if short else [])
    return " - ".join(parts) + PDF_SUFFIX


def file_pdf(src: Path, vault_root: Path, folder: str, filename: str) -> Path:
    """Copy `src` to <vault_root>/<folder>/<filename>, never overwriting.

    Returns the path actually written (possibly with a " (n)" suffix).
    Raises ValueError for an unsafe folder/filename or a missing vault root,
    and OSError when no free name fits or the copy fails.
    """
    src, vault_root = Path(src), Path(vault_root)
    if not src.is_file():
        raise FileNotFoundError(f"guide PDF not found: {src}")
    if not vault_root.is_absolute() or not vault_root.is_dir():
        raise ValueError(f"vault root is not an existing absolute folder: {vault_root}")
    _check_folder(folder)
    _check_filename(filename)
    dest_dir = vault_root / folder
    _check_inside(dest_dir, vault_root)
    stem, suffix = _split(filename)
    _check_room(dest_dir, suffix)   # before mkdir: no empty folder for a guide that cannot fit
    dest_dir.mkdir(parents=True, exist_ok=True)
    for n in range(1, MAX_COLLISIONS + 1):
        dest = _fit(dest_dir, stem, "" if n == 1 else f" ({n})", suffix)
        if _copy_exclusive(src, dest):
            log.info("study guides: filed %s", dest)
            return dest
    raise OSError(f"no free name for {filename} in {dest_dir} after {MAX_COLLISIONS} tries")


# ---- name cleaning --------------------------------------------------------------

def _clean(text: str) -> str:
    text = unicodedata.normalize("NFC", str(text or ""))
    text = _DROPPED_RE.sub(" ", text.translate(_DASHED))
    text = _SPACES_RE.sub(" ", text).strip()
    return text.rstrip(". ")   # Windows drops trailing dots and spaces silently


def _trim_words(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" -.,;")


# ---- path safety ------------------------------------------------------------

def _check_folder(folder: str) -> None:
    pure = PurePath(str(folder or ""))
    if (not str(folder or "").strip() or pure.is_absolute() or pure.drive
            or any(p in ("..", ".") or _bad_component(p) for p in pure.parts)):
        raise ValueError(f"unsafe vault folder: {folder!r}")


def _check_filename(filename: str) -> None:
    name = str(filename or "")
    if (not name or name != PurePath(name).name or name in (".", "..")
            or _bad_component(name)):
        raise ValueError(f"unsafe filename: {filename!r}")


def _bad_component(part: str) -> bool:
    stem = part.split(".")[0].strip().upper()
    return (bool(_FORBIDDEN_RE.search(part)) or part != part.rstrip(". ")
            or stem in _RESERVED_NAMES)


def _check_inside(dest_dir: Path, vault_root: Path) -> None:
    try:
        dest_dir.resolve().relative_to(vault_root.resolve())
    except ValueError as exc:
        raise ValueError(f"{dest_dir} is outside the vault root {vault_root}") from exc


# ---- writing ----------------------------------------------------------------

def _split(filename: str) -> tuple[str, str]:
    path = PurePath(filename)
    return (path.stem, path.suffix) if path.suffix else (filename, "")


def _check_room(dest_dir: Path, suffix: str) -> None:
    worst_tag = f" ({MAX_COLLISIONS})"
    room = MAX_PATH_CHARS - len(str(dest_dir)) - 1 - len(worst_tag) - len(suffix)
    if room < MIN_STEM_CHARS:
        raise OSError(f"vault folder path too long to file into: {dest_dir}")


def _fit(dest_dir: Path, stem: str, tag: str, suffix: str) -> Path:
    """dest_dir/<stem><tag><suffix>, with the stem trimmed to the path cap."""
    room = MAX_PATH_CHARS - len(str(dest_dir)) - 1 - len(tag) - len(suffix)
    if len(stem) > room:
        stem = stem[:room].rstrip(" -.,;")
    return dest_dir / f"{stem}{tag}{suffix}"


def _copy_exclusive(src: Path, dest: Path) -> bool:
    """Copy into a brand-new `dest`. False if the name is taken."""
    try:
        fout = open(dest, "xb")
    except FileExistsError:
        return False
    try:
        with fout, open(src, "rb") as fin:
            shutil.copyfileobj(fin, fout)
    except OSError:
        # The half-written file is ours (we just created it), so removing it
        # cannot touch anything of yours.
        dest.unlink(missing_ok=True)
        raise
    return True
