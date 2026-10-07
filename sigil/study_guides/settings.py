"""Everything the study-guides pipeline reads from Sigil's config, validated once.

Design rules:

* **Flat keys, read once.** Sigil's config.json is flat JSON, so every setting
  is a `study_guides_*` key read through `cfg.get(key, default)`. This module
  turns them into one frozen `Settings` at the start of a run; nothing else in
  the package touches `cfg` for these keys, so a typo is caught in one place.
* **A bad entry is logged and skipped, never fatal.** The file is hand-edited.
  One timetable row with `"start": "9am"` must not stop the other courses from
  getting their guide, so a malformed course, slot or date is dropped with a
  warning that names it, and the rest loads.
* **Discovery never writes config.json.** `discover` records the Moodle course
  ids in `course_ids.json` (see `save_course_ids`), and `load_settings` lays
  them over the configured courses. config.json is rewritten wholesale by
  `cfg.save()`, which is not something a background CLI should race.
* **No secrets here.** The Moodle token lives in its own file (moodle.py); the
  password is never stored anywhere.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
from dataclasses import field, dataclass, replace
from datetime import date, timedelta
from pathlib import Path, PurePath

from ..fileio import atomic_write_text, file_lock, read_text_locked
from .finished import finished_keys
from .syllabus import parse_strands
from .models import COURSE_IDS_PATH, LAB, LECTURE, TUTORIAL, Course, Session

log = logging.getLogger(__name__)

# ---- defaults ---------------------------------------------------------------

DEFAULT_RUN_TIME = "20:30"
DEFAULT_MODEL = "opus"              # the CLI takes aliases, not dated ids
DEFAULT_REVIEW_MODEL = "opus"        # the gate before a guide is published
DEFAULT_HELPER_MODEL = "sonnet"     # schedule extraction, file choice, verify
DEFAULT_TIMEOUT_SEC = 1800          # watchdog for one guide generation
MIN_TIMEOUT_SEC = 60                # below this even a short guide cannot finish
MAX_TIMEOUT_SEC = 4 * 3600          # above this a hung CLI blocks the whole night
DEFAULT_USAGE_MAX = 50              # % of the 5h window above which runs defer
DEFAULT_MOODLE_URL = "https://elearning.auth.gr"
# Under the vault root; the course folders sit directly inside it.
DEFAULT_VAULT_SUBDIR = "03 Resources/University"

# MiKTeX's per-user install is not on PATH on this machine, so it is tried
# before `shutil.which`. %LOCALAPPDATA% is used rather than a hardcoded user.
_MIKTEX_USER_XELATEX = ("Programs", "MiKTeX", "miktex", "bin", "x64", "xelatex.exe")

# A no-class range ("2026-12-23..2027-01-06") longer than this is a typo, not a
# break: expanding a decade of dates would silently switch the feature off.
MAX_NO_CLASS_RANGE_DAYS = 120

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

# Spellings accepted for a timetable slot's type. The course pages write the
# Greek letters; a Latin "A" typed by hand looks identical, so it is accepted.
_TYPE_ALIASES = {
    "θ": LECTURE, "th": LECTURE, "lecture": LECTURE, "theory": LECTURE,
    "α": TUTORIAL, "a": TUTORIAL, "tutorial": TUTORIAL, "φροντιστήριο": TUTORIAL,
    "lab": LAB, "εργαστήριο": LAB, "spice": LAB, "l": LAB,
}

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_KEY_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_ADDONS = ("circuits", "code", "math")

DEFAULT_COURSES: tuple[Course, ...] = (
    Course(key="circuits2", name_gr="Ηλεκτρικά Κυκλώματα ΙΙ",
           name_en="Electric Circuits II", vault_folder="Electrical Circuits II",
           addons=("circuits",)),
    Course(key="electronics1", name_gr="Ηλεκτρονική Ι",
           name_en="Electronics I", vault_folder="Electronics I",
           addons=("circuits",)),
    Course(key="appliedmath1", name_gr="Εφαρμοσμένα Μαθηματικά Ι",
           name_en="Applied Mathematics I", vault_folder="Applied Mathematics I",
           addons=("math",)),
    Course(key="datastructures", name_gr="Δομές Δεδομένων",
           name_en="Data Structures", vault_folder="Data Structures",
           addons=("code",)),
    Course(key="emfield1", name_gr="Ηλεκτρομαγνητικό Πεδίο Ι",
           name_en="Electromagnetic Field I", vault_folder="Electromagnetic Field I",
           addons=("math",)),
)


@dataclass(frozen=True)
class Settings:
    enabled: bool
    run_time: str
    semester_start: date | None
    semester_end: date | None
    no_class_dates: frozenset[str]
    skip_labs: bool
    infer_topic: bool
    model: str
    helper_model: str
    verify_pass: bool
    review_pass: bool       # the Opus publication gate
    review_model: str
    timeout_sec: int
    usage_max: int
    moodle_url: str
    vault_root: Path
    allow_vault_writes: bool
    xelatex: str
    claude_cmd: str
    courses: tuple[Course, ...]
    timetable: tuple[Session, ...]
    # GoodNotes backup: "auto" (find it in iCloud Drive), a path, or "" (off);
    # and course key -> subject folder overrides for names that do not match.
    goodnotes_dir: str = ""
    goodnotes_folders: tuple[tuple[str, str], ...] = ()
    # Labs come from the iCloud calendar (icloud_calendar.py) when it has the day;
    # `calendar_aliases` = (title text, course key) for titles that don't contain
    # the course's own name ("Ηλεκτρονικών Κυκλωμάτων 2" is Circuits II's lab).
    calendar_labs: bool = True
    calendar_aliases: tuple[tuple[str, str], ...] = ()
    strands: dict = field(default_factory=dict)   # syllabus.parse_strands


# ---- public API ---------------------------------------------------------------

def load_settings(cfg, *, course_ids_path: Path = COURSE_IDS_PATH) -> Settings:
    """Build `Settings` from Sigil's `Config` (anything with `.get(key, default)`)."""
    courses = _overlay_course_ids(_load_courses(cfg.get("study_guides_courses")),
                                  _read_course_ids(course_ids_path))
    passed = finished_keys(cfg)
    courses = tuple(c for c in courses if c.key not in passed)
    known = {c.key for c in courses}
    start = _parse_date(cfg.get("study_guides_semester_start"), "semester_start")
    end = _parse_date(cfg.get("study_guides_semester_end"), "semester_end")
    if start and end and end < start:
        log.warning("study guides: semester_end %s is before semester_start %s; "
                    "ignoring both", end, start)
        start = end = None
    return Settings(
        enabled=bool(cfg.get("study_guides_enabled", False)),
        run_time=_parse_run_time(cfg.get("study_guides_run_time", DEFAULT_RUN_TIME)),
        semester_start=start,
        semester_end=end,
        no_class_dates=_parse_no_class_dates(cfg.get("study_guides_no_class_dates")),
        skip_labs=bool(cfg.get("study_guides_skip_labs", False)),
        infer_topic=bool(cfg.get("study_guides_infer_topic", True)),
        model=_text(cfg.get("study_guides_model"), DEFAULT_MODEL),
        helper_model=_text(cfg.get("study_guides_helper_model"), DEFAULT_HELPER_MODEL),
        verify_pass=bool(cfg.get("study_guides_verify_pass", True)),
        review_pass=bool(cfg.get("study_guides_review_pass", True)),
        review_model=_text(cfg.get("study_guides_review_model"), DEFAULT_REVIEW_MODEL),
        timeout_sec=_bounded_int(cfg.get("study_guides_timeout_sec"), DEFAULT_TIMEOUT_SEC,
                                 MIN_TIMEOUT_SEC, MAX_TIMEOUT_SEC, "timeout_sec"),
        usage_max=_bounded_int(cfg.get("study_guides_usage_max"), DEFAULT_USAGE_MAX,
                               0, 100, "usage_max"),
        moodle_url=_text(cfg.get("study_guides_moodle_url"), DEFAULT_MOODLE_URL).rstrip("/"),
        vault_root=_vault_root(cfg),
        allow_vault_writes=bool(cfg.get("allow_vault_writes", True)),
        xelatex=_resolve_xelatex(_text(cfg.get("study_guides_xelatex"), "")),
        claude_cmd=_text(cfg.get("agent_claude_cmd"), ""),
        courses=courses,
        timetable=_load_timetable(_without_courses(cfg.get("study_guides_timetable"), passed),
                                  known),
        goodnotes_dir=_text(cfg.get("study_guides_goodnotes_dir"), ""),
        goodnotes_folders=_folder_overrides(cfg.get("study_guides_goodnotes_folders")),
        calendar_labs=bool(cfg.get("study_guides_calendar_labs", True)),
        calendar_aliases=_alias_pairs(cfg.get("study_guides_calendar_aliases")),
        strands=parse_strands(cfg.get("study_guides_strands")),
    )


def course_by_key(settings: Settings, key: str) -> Course | None:
    for course in settings.courses:
        if course.key == key:
            return course
    return None


def save_course_ids(mapping: dict[str, int], *, path: Path = COURSE_IDS_PATH) -> None:
    """Merge discovered Moodle ids into `course_ids.json` (atomic write).

    Merged, not replaced: a `discover` that could only match three courses
    must not forget the ids of the other two confirmed last time.
    """
    clean: dict[str, int] = {}
    for key, value in mapping.items():
        cid = _positive_int(value)
        if not isinstance(key, str) or cid is None:
            log.warning("study guides: not saving course id %r -> %r", key, value)
            continue
        clean[key] = cid
    with file_lock(path):
        merged = {**_read_course_ids(path), **clean}
        atomic_write_text(path, json.dumps(merged, indent=2, ensure_ascii=False))
    log.info("study guides: saved Moodle course ids for %s", ", ".join(sorted(clean)) or "none")


# ---- courses ------------------------------------------------------------------

def _load_courses(raw) -> tuple[Course, ...]:
    """DEFAULT_COURSES, with config entries laid over them by `key`.

    A config entry for a known key changes only the fields it names; an entry
    with a new key adds a course (and then needs name_gr and vault_folder).
    """
    by_key = {c.key: c for c in DEFAULT_COURSES}
    if raw in (None, "", []):
        return tuple(by_key.values())
    if not isinstance(raw, list):
        log.warning("study guides: study_guides_courses is not a list; using defaults")
        return tuple(by_key.values())
    for item in raw:
        course = _course_from_entry(item, by_key)
        if course is not None:
            by_key[course.key] = course
    return tuple(by_key.values())


def _course_from_entry(item, by_key: dict[str, Course]) -> Course | None:
    if not isinstance(item, dict):
        log.warning("study guides: skipping course entry %r (not an object)", item)
        return None
    key = str(item.get("key") or "").strip().lower()
    if not _KEY_RE.match(key):
        log.warning("study guides: skipping course entry with bad key %r", item.get("key"))
        return None
    base = by_key.get(key)
    fields: dict = {}
    for name in ("name_gr", "name_en", "vault_folder", "exam_format"):
        if name in item:
            fields[name] = str(item[name] or "").strip()
    if "moodle_course_id" in item:
        fields["moodle_course_id"] = _positive_int(item["moodle_course_id"]) or 0
    if "addons" in item:
        fields["addons"] = _parse_addons(item["addons"], key)
    if base is None:
        if not fields.get("name_gr") or not fields.get("vault_folder"):
            log.warning("study guides: new course %r needs name_gr and vault_folder; "
                        "skipped", key)
            return None
        return Course(key=key, name_gr=fields["name_gr"],
                      name_en=fields.get("name_en") or fields["name_gr"],
                      vault_folder=fields["vault_folder"],
                      moodle_course_id=fields.get("moodle_course_id", 0),
                      addons=fields.get("addons", ()),
                      exam_format=fields.get("exam_format") or "unknown")
    # An empty string in config means "not set", not "blank the default".
    fields = {k: v for k, v in fields.items() if v not in ("",)}
    return replace(base, **fields)


def _parse_addons(raw, key: str) -> tuple[str, ...]:
    items = raw if isinstance(raw, list) else [raw]
    addons = []
    for addon in items:
        name = str(addon or "").strip().lower()
        if name in _ADDONS and name not in addons:
            addons.append(name)
        elif name:
            log.warning("study guides: course %s: unknown addon %r ignored", key, addon)
    return tuple(addons)


def _read_course_ids(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(read_text_locked(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("study guides: %s unreadable (%s); ignoring discovered ids",
                    path.name, exc)
        return {}
    if not isinstance(raw, dict):
        log.warning("study guides: %s is not an object; ignoring it", path.name)
        return {}
    ids = {}
    for key, value in raw.items():
        cid = _positive_int(value)
        if cid is not None:
            ids[str(key)] = cid
    return ids


def _overlay_course_ids(courses: tuple[Course, ...], ids: dict[str, int]) -> tuple[Course, ...]:
    """Discovered ids win: they were confirmed more recently than config."""
    return tuple(replace(c, moodle_course_id=ids[c.key]) if c.key in ids else c
                 for c in courses)


# ---- timetable ----------------------------------------------------------------

def _without_courses(raw, keys: frozenset[str]):
    """Timetable entries minus those of finished courses (else each logs "bad course")."""
    if not isinstance(raw, list) or not keys:
        return raw
    return [e for e in raw
            if not (isinstance(e, dict) and str(e.get("course") or "").lower() in keys)]


def _load_timetable(raw, known_courses: set[str]) -> tuple[Session, ...]:
    if raw in (None, "", []):
        return ()
    if not isinstance(raw, list):
        log.warning("study guides: study_guides_timetable is not a list; treating as empty")
        return ()
    sessions = []
    for item in raw:
        session = _session_from_entry(item, known_courses)
        if session is not None:
            sessions.append(session)
    return tuple(sorted(sessions, key=lambda s: (WEEKDAYS.index(s.weekday), s.start)))


def _session_from_entry(item, known_courses: set[str]) -> Session | None:
    if not isinstance(item, dict):
        log.warning("study guides: skipping timetable entry %r (not an object)", item)
        return None
    weekday = str(item.get("weekday") or "").strip().upper()[:2]
    start = _norm_time(item.get("start"))
    end = _norm_time(item.get("end"))
    course = str(item.get("course") or "").strip().lower()
    kind = _TYPE_ALIASES.get(str(item.get("type") or "").strip().lower())
    problem = (
        "weekday" if weekday not in WEEKDAYS else
        "start" if start is None else
        "end" if end is None or end <= start else
        "course" if course not in known_courses else
        "type" if kind is None else ""
    )
    if problem:
        log.warning("study guides: skipping timetable entry %r (bad %s)", item, problem)
        return None
    return Session(course_key=course, weekday=weekday, start=start, end=end, type=kind)


def _norm_time(value) -> str | None:
    """"9:00" -> "09:00"; anything else that is not HH:MM -> None."""
    match = _TIME_RE.match(str(value or "").strip())
    if not match:
        return None
    return f"{int(match.group(1)):02d}:{match.group(2)}"


# ---- scalars ------------------------------------------------------------------

def _parse_run_time(value) -> str:
    norm = _norm_time(value)
    if norm is None:
        log.warning("study guides: run_time %r is not HH:MM; using %s",
                    value, DEFAULT_RUN_TIME)
        return DEFAULT_RUN_TIME
    return norm


def _parse_date(value, label: str) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        log.warning("study guides: %s %r is not YYYY-MM-DD; ignored", label, value)
        return None


def _parse_no_class_dates(raw) -> frozenset[str]:
    """Single dates, or inclusive ranges written "YYYY-MM-DD..YYYY-MM-DD"."""
    if raw in (None, "", []):
        return frozenset()
    if not isinstance(raw, list):
        log.warning("study guides: study_guides_no_class_dates is not a list; ignored")
        return frozenset()
    days: set[str] = set()
    for item in raw:
        text = str(item or "").strip()
        first, _, last = text.partition("..")
        lo = _parse_date(first, "no_class_dates entry")
        hi = _parse_date(last, "no_class_dates entry") if last else lo
        if lo is None or hi is None or hi < lo:
            if lo is not None:
                log.warning("study guides: no_class_dates range %r is backwards; ignored", text)
            continue
        if (hi - lo).days > MAX_NO_CLASS_RANGE_DAYS:
            log.warning("study guides: no_class_dates range %r spans more than %d days; "
                        "ignored", text, MAX_NO_CLASS_RANGE_DAYS)
            continue
        days.update((lo + timedelta(days=n)).isoformat() for n in range((hi - lo).days + 1))
    return frozenset(days)


def _bounded_int(value, default: int, lo: int, hi: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        log.warning("study guides: %s %r is not a number; using %d", label, value, default)
        return default
    clamped = max(lo, min(hi, number))
    if clamped != number:
        log.warning("study guides: %s %d outside %d..%d; using %d", label, number, lo, hi, clamped)
    return clamped


def _positive_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _folder_overrides(raw) -> tuple[tuple[str, str], ...]:
    if not isinstance(raw, dict):
        return ()
    return tuple((str(k), str(v)) for k, v in raw.items() if str(v).strip())


def _alias_pairs(raw) -> tuple[tuple[str, str], ...]:
    """{"title text": "course key"} -> ((text, key), ...)."""
    if not isinstance(raw, dict):
        return ()
    return tuple((str(k).strip(), str(v).strip()) for k, v in raw.items()
                 if str(k).strip() and str(v).strip())


def _text(value, default: str) -> str:
    text = str(value).strip() if value is not None else ""
    return text or default


def _vault_root(cfg) -> Path:
    """<vault>/03 Resources/University, or an empty Path when no vault is set.

    Filing refuses a relative root, so an unset `vault_path` fails loudly at
    filing time instead of scattering PDFs into Sigil's working directory.
    """
    vault = _text(cfg.get("vault_path"), "")
    if not vault:
        log.warning("study guides: vault_path is not set; guides cannot be filed")
        return Path()
    sub = _text(cfg.get("study_guides_vault_root"), DEFAULT_VAULT_SUBDIR)
    parts = PurePath(sub).parts
    if PurePath(sub).is_absolute() or any(p in ("..", ".") for p in parts):
        log.warning("study guides: study_guides_vault_root %r must be a plain path "
                    "inside the vault; using %s", sub, DEFAULT_VAULT_SUBDIR)
        sub = DEFAULT_VAULT_SUBDIR
    return Path(vault) / sub


def _resolve_xelatex(configured: str) -> str:
    """Configured path, else MiKTeX's per-user install, else PATH, else ""."""
    if configured:
        if Path(configured).is_file() or shutil.which(configured):
            return configured
        log.warning("study guides: study_guides_xelatex %r not found; looking elsewhere",
                    configured)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local).joinpath(*_MIKTEX_USER_XELATEX)
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("xelatex")
    if found:
        return found
    log.warning("study guides: no xelatex found; set study_guides_xelatex")
    return configured
