"""Shared value types for the study-guides pipeline.

Every stage (timetable -> moodle -> schedule -> material -> prompt ->
generate -> build -> filing -> state) passes these frozen dataclasses between
each other, so no module needs to know another's internals. They are frozen on
purpose: a stage that wants a changed value builds a new one with
`dataclasses.replace`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ..config import DATA_DIR

# Everything this feature keeps on disk lives here: gitignored via data/.
WORK_DIR = DATA_DIR / "study_guides"
CACHE_DIR = WORK_DIR / "cache"          # downloaded Moodle files
BUILD_DIR = WORK_DIR / "build"          # per-run .tex/.log/.pdf working dirs
COURSE_IDS_PATH = WORK_DIR / "course_ids.json"   # discover's overlay
RUNS_PATH = WORK_DIR / "runs.json"
SCHEDULE_CACHE_PATH = WORK_DIR / "schedule_cache.json"
FILE_CACHE_PATH = WORK_DIR / "file_cache.json"
LOCK_PATH = WORK_DIR / "run.lock"

# Session types, as the course pages and the timetable write them.
LECTURE = "Θ"
TUTORIAL = "Α"
LAB = "lab"
SESSION_TYPES = (LECTURE, TUTORIAL, LAB)

# Run outcome statuses recorded in runs.json.
OK = "ok"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass(frozen=True)
class Course:
    key: str                     # "circuits2"
    name_gr: str                 # "Ηλεκτρικά Κυκλώματα ΙΙ"
    name_en: str                 # "Electric Circuits II"
    vault_folder: str            # folder under <vault>/03 Resources/University/
    moodle_course_id: int = 0    # 0 = not discovered yet
    addons: tuple[str, ...] = ()  # subset of ("circuits", "code", "math")
    exam_format: str = "unknown"  # "unknown" | "problems" | "mcq" | free text


@dataclass(frozen=True)
class Session:
    course_key: str
    weekday: str                 # "MO".."SU"
    start: str                   # "09:00"
    end: str                     # "11:00"
    type: str                    # one of SESSION_TYPES


@dataclass(frozen=True)
class ScheduleEntry:
    date: str                    # "YYYY-MM-DD"
    session_type: str | None     # one of SESSION_TYPES or None
    topic: str
    section_ids: tuple[int, ...] = ()
    module_ids: tuple[int, ...] = ()
    filenames: tuple[str, ...] = ()  # exact files (syllabus strands); beats the ids


@dataclass(frozen=True)
class FileRef:
    module_id: int
    section_id: int
    section_name: str
    filename: str
    fileurl: str
    timemodified: int
    mimetype: str = ""
    filesize: int = 0


@dataclass(frozen=True)
class Material:
    """What the prompt gets for one course-day."""
    text: str                            # extracted text, already trimmed
    files_used: tuple[str, ...] = ()     # filenames, for the log/state
    attachments: tuple[Path, ...] = ()   # scanned PDFs staged for the Read tool
    past_exams: str = ""                 # relevant past-exam text, "" = none
    notes: tuple[str, ...] = ()          # trims / fallbacks worth logging


@dataclass(frozen=True)
class CourseResult:
    date: str
    course_key: str
    status: str                  # OK | SKIPPED | FAILED
    reason: str = ""
    topic: str = ""
    topic_inferred: bool = False
    files_used: tuple[str, ...] = ()
    pdf_path: str = ""
    cost_usd: float | None = None   # the CLI's reported cost, when it gives one
    extra: dict = field(default_factory=dict)
