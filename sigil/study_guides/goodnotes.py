"""Your own handwritten lecture notes from GoodNotes, added to the next guide of a course.

GoodNotes on the iPad has no API. Its Auto-Backup exports every notebook as a
PDF into Google Drive, which Google Drive for Desktop streams to this PC, and
this module reads it from there. One GoodNotes folder per subject.

Design rules:

* **Notes go into the NEXT guide.** Guides run in the afternoon and notes only
  exist after a class, so the pages a guide gets are the ones no earlier guide
  of that course used — normally the previous lecture's. They are context
  ("what was stressed in class"), not today's topic.
* **New pages are counted, not dated.** A notebook keeps growing; the ledger
  (`goodnotes_pages.json`) stores how many pages of each PDF earlier guides
  already used. Pages past that count are new; a new notebook is all new.
* **Consumed only when a guide is filed.** `collect` never writes the ledger;
  `mark_used` runs after a successful build, so a failed, skipped or dry run
  leaves the notes waiting for the next one.
* **Off unless configured.** `study_guides_goodnotes_dir` is "auto" in
  DEFAULTS (search Google Drive, OneDrive, iCloud Drive), a path to pin it, and "" (what a bare test
  config gives) turns the whole feature off.
* **Handwriting is pictures.** GoodNotes ink exports as vector strokes with no
  text layer, so the notes always go to the generator as an attachment for
  its Read tool, which sees the rendered pages.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path

from ..fileio import atomic_write_text, read_text_locked
from .models import WORK_DIR, Course, Material

log = logging.getLogger(__name__)

NOTES_DIR = WORK_DIR / "goodnotes"
LEDGER_PATH = WORK_DIR / "goodnotes_pages.json"

AUTO = "auto"
# Where "auto" looks. GoodNotes' Auto-Backup offers Dropbox, Google Drive,
# OneDrive and WebDAV — not iCloud — and Google Drive is
# searched first. Google Drive for Desktop streams it as a
# drive letter holding "My Drive" (localised); the others are fallbacks. The
# backup folder's name starts with "goodnotes" ("GoodNotes", "Goodnotes 6")
# and may sit one level down (an "Apps/" folder).
GOOGLE_MY_DRIVE_NAMES = ("My Drive", "Ο Δίσκος μου")
CLOUD_DIRS = (Path(os.environ.get("OneDrive") or Path.home() / "OneDrive"),
              Path.home() / "iCloudDrive")
BACKUP_PREFIX = "goodnotes"
MAX_ROOT_DEPTH = 2

# The name the generator sees for the notes attachment.
ATTACHMENT_NAME = "my_lecture_notes_goodnotes.pdf"

# How many note pages one guide gets at most. A lecture is 5-15 handwritten
# pages; after a long gap (holidays, skipped guides) the newest pages win.
MAX_NOTE_PAGES = 30

# How deep under the backup root a subject folder may sit.
MAX_FOLDER_DEPTH = 3

# A folder word matches a course word when one starts with the other and the
# shared part is at least this long («Ηλεκτρονικής» ~ «Ηλεκτρονική»), but
# «Ηλεκτρικά» never matches «Ηλεκτρονική».
MIN_WORD_CHARS = 4

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4", "ι": "1", "ιι": "2", "ιιι": "3"}

_INTRO = (
    "=== Οι σημειώσεις μου από την τάξη (GoodNotes) ===\n"
    "Το συνημμένο {name} έχει τις χειρόγραφες σημειώσεις του φοιτητή από τα "
    "τελευταία μαθήματα αυτού του μαθήματος ({label}). Δείχνουν τι εξήγησε και "
    "τόνισε ο διδάσκων στην τάξη. Διάβασέ το με το Read tool και χρησιμοποίησε "
    "ό,τι σχετίζεται με το σημερινό θέμα: παραδείγματα, εμφάσεις, σημεία που ο "
    "φοιτητής σημείωσε ως δύσκολα. Μπορεί να καλύπτουν το προηγούμενο μάθημα — "
    "μην βγάλεις τον οδηγό εκτός θέματος γι' αυτό. Για ορισμούς και τύπους, "
    "όπου διαφωνούν με το υλικό του e-learning, εμπιστέψου το υλικό."
)


@dataclass(frozen=True)
class NotesPick:
    """The new note pages of one course, staged as one PDF."""
    course_key: str
    pdf: Path                                  # the staged PDF of new pages
    label: str                                 # "Κυκλώματα/Notebook σελ. 5-12"
    page_count: int
    counts: tuple[tuple[str, int], ...]        # ledger update: (relpath, pages)


# --------------------------------------------------------------------------
# Finding the folders

def find_root(configured: str) -> Path | None:
    """The GoodNotes backup folder, or None (feature off or not synced yet)."""
    value = (configured or "").strip()
    if not value:
        return None
    if value.lower() != AUTO:
        path = Path(os.path.expandvars(value)).expanduser()
        if not path.is_dir():
            log.warning("goodnotes: configured folder %s does not exist", path)
            return None
        return path
    for base in (*_google_drives(), *CLOUD_DIRS):
        found = _backup_dirs(base)
        if found:
            return found[0]
    return None


def _google_drives() -> list[Path]:
    """Google Drive for Desktop's "My Drive" folders, on any drive letter."""
    found = []
    for letter in "DEFGHIJKLMNOPQRSTUVWXYZ":
        for name in GOOGLE_MY_DRIVE_NAMES:
            path = Path(f"{letter}:/") / name
            try:
                if path.is_dir():
                    found.append(path)
            except OSError:
                continue
    return found


def _backup_dirs(base: Path) -> list[Path]:
    """Folders named "goodnotes…" under `base`, shallowest first."""
    if not base.is_dir():
        return []
    found: list[Path] = []
    level = [base]
    for _depth in range(MAX_ROOT_DEPTH):
        nxt: list[Path] = []
        for folder in level:
            try:
                subs = sorted(p for p in folder.iterdir() if p.is_dir())
            except OSError:
                continue
            found.extend(p for p in subs if _fold(p.name).startswith(BACKUP_PREFIX))
            nxt.extend(subs)
        if found:
            return found
        level = nxt
    return found


def course_folders(root: Path, courses: tuple[Course, ...],
                   overrides: dict[str, str] | None = None) -> dict[str, Path]:
    """course key -> its subject folder. An override (a path relative to the
    root, or absolute) wins; otherwise the folder whose name shares the most
    words with the course's names, if exactly one does best."""
    overrides = overrides or {}
    found: dict[str, Path] = {}
    folders = _subfolders(root)
    for course in courses:
        if course.key in overrides:
            path = Path(overrides[course.key])
            path = path if path.is_absolute() else root / path
            if path.is_dir():
                found[course.key] = path
            else:
                log.warning("goodnotes: folder %s for %s does not exist", path, course.key)
            continue
        best = _best_folder(course, folders)
        if best is not None:
            found[course.key] = best
    return found


def _subfolders(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, _files in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= MAX_FOLDER_DEPTH:
            dirnames[:] = []
        out.extend(Path(dirpath) / d for d in sorted(dirnames))
    return out


def _best_folder(course: Course, folders: list[Path]) -> Path | None:
    names = " ".join((course.name_gr, course.name_en, course.vault_folder, course.key))
    course_words = _words(names)
    scored = sorted(((_match(_words(f.name), course_words), -len(f.parts), f)
                     for f in folders), key=lambda t: (t[0], t[1]), reverse=True)
    if not scored or scored[0][0] == 0:
        return None
    if len(scored) > 1 and scored[1][0] == scored[0][0] and scored[1][1] == scored[0][1]:
        log.warning("goodnotes: %s matches both %s and %s — set "
                    "study_guides_goodnotes_folders", course.key,
                    scored[0][2].name, scored[1][2].name)
        return None
    return scored[0][2]


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).replace("ς", "σ")


def _words(text: str) -> set[str]:
    words = set()
    for word in re.findall(r"[^\W_]+", _fold(text)):
        if word in _ROMAN:
            words.add(_ROMAN[word])
        elif word.isdigit() or len(word) >= MIN_WORD_CHARS:
            words.add(word)
    return words


def _match(folder_words: set[str], course_words: set[str]) -> int:
    """How many of the folder's words are course words. A number alone
    ("2") never makes a match; it only breaks ties between word matches."""
    folder_nums = {w for w in folder_words if w.isdigit()}
    course_nums = {w for w in course_words if w.isdigit()}
    if folder_nums and course_nums and not folder_nums & course_nums:
        return 0   # «Μαθηματικά ΙΙ» is never the notes of «Μαθηματικά Ι»
    hits = [w for w in folder_words
            if any(_same_word(w, c) for c in course_words)]
    if not any(not w.isdigit() for w in hits):
        return 0
    return len(hits)


def _same_word(a: str, b: str) -> bool:
    if a.isdigit() or b.isdigit():
        return a == b
    short, long_ = sorted((a, b), key=len)
    return len(short) >= MIN_WORD_CHARS and long_.startswith(short)


# --------------------------------------------------------------------------
# New pages

def collect(goodnotes_dir: str, overrides: dict[str, str], course: Course,
            day_str: str, *, ledger_path: Path | None = None,
            notes_dir: Path | None = None) -> NotesPick | None:
    """The course's note pages no guide has used yet, as one staged PDF; None
    when there are none (or the feature is off). Never raises — notes are a
    bonus, a sync hiccup must not cost the guide."""
    try:
        root = find_root(goodnotes_dir)
        if root is None:
            return None
        folder = course_folders(root, (course,), overrides).get(course.key)
        if folder is None:
            log.info("goodnotes: no folder for %s under %s", course.key, root)
            return None
        return _pick(course, root, folder, day_str, ledger_path or LEDGER_PATH,
                     notes_dir or NOTES_DIR)
    except Exception:  # noqa: BLE001
        log.exception("goodnotes: could not read the notes for %s", course.key)
        return None


def _pick(course: Course, root: Path, folder: Path, day_str: str,
          ledger_path: Path, notes_dir: Path) -> NotesPick | None:
    import pymupdf
    seen = read_ledger(ledger_path).get(course.key, {})
    pdfs = sorted(folder.rglob("*.pdf"), key=lambda p: (p.stat().st_mtime, p.name))
    ranges: list[tuple[Path, int, int]] = []   # (pdf, first, last) 0-based, inclusive
    counts: list[tuple[str, int]] = []
    for pdf in pdfs:
        rel = pdf.relative_to(root).as_posix()
        try:
            with pymupdf.open(pdf) as doc:
                total = doc.page_count
        except Exception as exc:  # noqa: BLE001 — mid-sync or not downloaded yet
            log.warning("goodnotes: could not open %s (%s); next time", rel, exc)
            continue
        counts.append((rel, total))
        start = min(int(seen.get(rel, 0)), total)
        if start < total:
            ranges.append((pdf, start, total - 1))
    ranges = _newest(ranges, MAX_NOTE_PAGES)
    if not ranges:
        return None
    out = notes_dir / f"{day_str}_{course.key}" / ATTACHMENT_NAME
    out.parent.mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as merged:
        for pdf, first, last in ranges:
            with pymupdf.open(pdf) as doc:
                merged.insert_pdf(doc, from_page=first, to_page=last)
        merged.save(out)
        pages = merged.page_count
    label = "; ".join(f"{p.relative_to(folder).with_suffix('').as_posix()} "
                      f"σελ. {a + 1}-{b + 1}" for p, a, b in ranges)
    log.info("goodnotes: %s — %d new page(s): %s", course.key, pages, label)
    return NotesPick(course.key, out, label, pages, tuple(counts))


def _newest(ranges: list[tuple[Path, int, int]], budget: int) -> list[tuple[Path, int, int]]:
    """Keep the last `budget` pages, oldest-first order preserved."""
    kept: list[tuple[Path, int, int]] = []
    for pdf, first, last in reversed(ranges):
        if budget <= 0:
            break
        first = max(first, last - budget + 1)
        kept.append((pdf, first, last))
        budget -= last - first + 1
    return list(reversed(kept))


def attach(material: Material, pick: NotesPick | None) -> Material:
    """`material` with the notes staged as an attachment and introduced in the text."""
    if pick is None:
        return material
    intro = _INTRO.format(name=ATTACHMENT_NAME, label=pick.label)
    text = f"{intro}\n\n{material.text}" if material.text.strip() else intro
    return replace(material, text=text,
                   attachments=material.attachments + (pick.pdf,),
                   files_used=material.files_used + (f"GoodNotes: {pick.label}",),
                   notes=material.notes + (f"GoodNotes: {pick.page_count} page(s) attached",))


# --------------------------------------------------------------------------
# Ledger

def read_ledger(path: Path | None = None) -> dict[str, dict[str, int]]:
    path = path or LEDGER_PATH
    try:
        data = json.loads(read_text_locked(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("goodnotes: ledger %s unreadable (%s); treating as empty", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def mark_used(pick: NotesPick | None, *, ledger_path: Path | None = None) -> None:
    """Record the pick's pages as used, so the next guide gets only newer ones."""
    if pick is None:
        return
    ledger_path = ledger_path or LEDGER_PATH
    try:
        ledger = read_ledger(ledger_path)
        course = {**ledger.get(pick.course_key, {}), **dict(pick.counts)}
        ledger = {**ledger, pick.course_key: course}
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(ledger_path, json.dumps(ledger, ensure_ascii=False, indent=2))
    except Exception:  # noqa: BLE001 — the guide is already filed; never fail it
        log.exception("goodnotes: could not record used pages for %s; they "
                      "will be offered again", pick.course_key)


# --------------------------------------------------------------------------
# CLI report

def status_lines(goodnotes_dir: str, overrides: dict[str, str],
                 courses: tuple[Course, ...], *, ledger_path: Path | None = None) -> list[str]:
    """What `python -m sigil.study_guides goodnotes` prints."""
    root = find_root(goodnotes_dir)
    if root is None:
        where = (" and ".join(str(d) for d in (*_google_drives(), *CLOUD_DIRS))
                 or "Google Drive / OneDrive"
                 if (goodnotes_dir or "").strip().lower() == AUTO else goodnotes_dir)
        return [f"No GoodNotes backup folder found (looked in {where or '— feature off'}).",
                "On the iPad: GoodNotes > Settings > Cloud & Backup > Auto-Backup > Google Drive, format PDF."]
    import pymupdf
    ledger = read_ledger(ledger_path)
    mapping = course_folders(root, courses, overrides)
    lines = [f"GoodNotes backup: {root}"]
    for course in courses:
        folder = mapping.get(course.key)
        if folder is None:
            lines.append(f"  {course.key:15} -> (no folder matched)")
            continue
        seen = ledger.get(course.key, {})
        new = total = 0
        for pdf in folder.rglob("*.pdf"):
            try:
                with pymupdf.open(pdf) as doc:
                    pages = doc.page_count
            except Exception:  # noqa: BLE001
                continue
            total += pages
            new += max(0, pages - int(seen.get(pdf.relative_to(root).as_posix(), 0)))
        lines.append(f"  {course.key:15} -> {folder.relative_to(root)}  "
                     f"({total} pages, {new} not yet in a guide)")
    matched = set(mapping.values())
    loose = [f for f in _subfolders(root) if f not in matched
             and not any(m in f.parents for m in matched)
             and not any(f in m.parents for m in matched)]
    lines.extend(f"  unmatched folder: {f.relative_to(root)}" for f in loose)
    return lines
