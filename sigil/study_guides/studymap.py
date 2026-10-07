"""The study map: one week of classes, deadlines
and study guides as a flat, time-sorted list of rows.

Design rules:

* **Pure.** No network. Classes come from the configured timetable,
  deadlines from an `assignments.Snapshot` the caller already has (this
  module never fetches), guides from runs.json
  records. The only disk read is runs.json, and only when the caller did not
  pass the records in.
* **The day is always passed in.** Nothing here calls `date.today()`, so the
  tests and a run at 23:59 see exactly the same logic.
* **Internal records are not guides.** runs.json keys starting with "_" (the
  weekly review keeps its own weekly.json, but older builds or hand edits may
  leave one) are skipped, so they never show up as a course.
* **Holidays show, empty weekdays do not.** A day listed in `no_class_dates`
  that would otherwise have classes gets one "no class: ..." row; a weekend
  with no timetable slots gets nothing, since "no classes on Saturdays" every
  week is noise.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable

from .models import FAILED, OK, SKIPPED
from .settings import WEEKDAYS, Settings, course_by_key
from .state import RunState
from .timetable import no_class_reason, todays_sessions

log = logging.getLogger(__name__)

# Row kinds, in display order.
KIND_CLASS = "class"
KIND_DEADLINE = "deadline"
KIND_GUIDE = "guide"
KIND_EXAM = "exam"      # a test or final from exams.json (passed in, never read here)

# Deadline statuses (as assignments.Deadline writes them) that mean "done".
DONE_STATUSES = frozenset({"submitted", "submitted?"})
# The runs.json statuses a guide row can show.
GUIDE_STATUSES = (OK, SKIPPED, FAILED)
# How far ahead `next_class` looks before giving up (a break is at most a few
# weeks; the Christmas range in config is ~2).
NEXT_CLASS_LOOKAHEAD_DAYS = 28
# Prefix of runs.json course keys that are bookkeeping, not courses.
INTERNAL_KEY_PREFIX = "_"


@dataclass(frozen=True)
class StudyItem:
    kind: str               # KIND_CLASS | KIND_DEADLINE | KIND_GUIDE
    course: str             # display name (name_en, else key / Moodle shortname)
    when: datetime          # local, naive
    title: str
    status: str             # class: "Θ 11:00-13:00" / "no class: ..."; deadline:
                            # "in 3d" / "today" / "overdue 2d" / "submitted?";
                            # guide: "ok" / "failed" / "skipped"
    target: str | None = None   # pdf path or URL to open, if any


# ---- public API ---------------------------------------------------------------

def build(settings: Settings, snapshot, guides: Iterable[dict] | None, today: date,
          days_ahead: int = 7, days_back: int = 7, exams=()) -> list[StudyItem]:
    """Classes today..today+days_ahead-1, deadlines and guides around today.

    `snapshot` is an `assignments.Snapshot` (or None when there is none yet);
    `guides` are runs.json records, e.g. `RunState().between(...)` — None
    reads them from the default runs.json. `exams` are `exams.Exam` rows the
    caller loaded (tests and finals), shown on their dates from today on.
    """
    start = today - timedelta(days=days_back)
    end = today + timedelta(days=days_ahead)
    if guides is None:
        guides = RunState().between(start.isoformat(), today.isoformat())
    items = [
        *class_items(settings, today, days_ahead),
        *deadline_items(settings, snapshot, today, start, end),
        *guide_items(settings, guides, start, today),
        *exam_items(settings, exams, today, end),
    ]
    return sorted(items, key=lambda it: (it.when, it.kind, it.course))


def exam_items(settings: Settings, exams, today: date, end: date) -> list[StudyItem]:
    """Tests and finals dated today..end. Status: "TEST · in 3d" / "possible test · in 3d"."""
    rows: list[StudyItem] = []
    for exam in exams or ():
        try:
            day = exam.day
        except (AttributeError, ValueError):
            continue
        if not today <= day <= end:
            continue
        label = ("FINAL" if exam.kind == "final" else "TEST") if exam.confirmed \
            else "possible test"
        days = (day - today).days
        left = "today" if days == 0 else f"in {days}d"
        course = (course_name(settings, exam.course_key) if exam.course_key
                  else exam.course_name)
        rows.append(StudyItem(
            kind=KIND_EXAM, course=course,
            when=datetime.combine(day, _hhmm(exam.time) if exam.time else time.min),
            title=exam.title or label, status=f"{label} · {left}",
            target=exam.url or None))
    return rows


def class_items(settings: Settings, first: date, days: int) -> list[StudyItem]:
    """One row per timetable slot on each day, plus one row per holiday."""
    rows: list[StudyItem] = []
    for offset in range(max(0, days)):
        day = first + timedelta(days=offset)
        sessions = todays_sessions(settings, day)
        for s in sessions:
            rows.append(StudyItem(
                kind=KIND_CLASS, course=course_name(settings, s.course_key),
                when=datetime.combine(day, _hhmm(s.start)),
                title=f"{day.strftime('%a')} {s.start}-{s.end}",
                status=f"{s.type} {s.start}-{s.end}"))
        if not sessions and _is_holiday(settings, day):
            rows.append(StudyItem(
                kind=KIND_CLASS, course="", when=datetime.combine(day, time.min),
                title=day.strftime("%a %d %b"),
                status=f"no class: {no_class_reason(settings, day)}"))
    return rows


def deadline_items(settings: Settings, snapshot, today: date,
                   start: date, end: date) -> list[StudyItem]:
    """Snapshot items due in [start, end], with a human "in 3d" status."""
    rows: list[StudyItem] = []
    lo = datetime.combine(start, time.min)
    hi = datetime.combine(end, time.max)
    for item in snapshot_items(snapshot):
        when = _due(item)
        if when is None or not lo <= when <= hi:
            continue
        rows.append(StudyItem(
            kind=KIND_DEADLINE, course=deadline_course(settings, item), when=when,
            title=str(getattr(item, "title", "") or "(untitled)"),
            status=deadline_status(item, today), target=getattr(item, "url", None) or None))
    return rows


def guide_items(settings: Settings, records: Iterable[dict],
                start: date, end: date) -> list[StudyItem]:
    """runs.json records dated start..end (internal "_" keys skipped)."""
    rows: list[StudyItem] = []
    for rec in records:
        key = str(rec.get("course_key") or "")
        day = _iso_date(rec.get("date"))
        if not key or key.startswith(INTERNAL_KEY_PREFIX) or day is None:
            continue
        if not start <= day <= end:
            continue
        status = str(rec.get("status") or "")
        title = str(rec.get("topic") or "").strip() or str(rec.get("reason") or "").strip()
        rows.append(StudyItem(
            kind=KIND_GUIDE,
            course=str(rec.get("course_label") or "") or course_name(settings, key),
            when=_recorded_at(rec, day), title=title or "(no topic)",
            status=status if status in GUIDE_STATUSES else status or "?",
            target=str(rec.get("pdf_path") or "") or None))
    return rows


def next_class(settings: Settings, now: datetime,
               lookahead_days: int = NEXT_CLASS_LOOKAHEAD_DAYS) -> StudyItem | None:
    """The next timetable slot starting at or after `now`, or None."""
    for item in class_items(settings, now.date(), lookahead_days):
        if item.course and item.when >= now:
            return item
    return None


def deadline_status(item, today: date) -> str:
    """"submitted?" / "today" / "in 3d" / "overdue 2d" for one Deadline."""
    status = str(getattr(item, "status", "") or "")
    if status in DONE_STATUSES:
        return status
    when = _due(item)
    if when is None:
        return status or "?"
    days = (when.date() - today).days
    if days == 0:
        return "today"
    if days > 0:
        return f"in {days}d"
    return f"overdue {-days}d"


def course_name(settings: Settings, key: str) -> str:
    course = course_by_key(settings, key)
    if course is None:
        return key
    return course.name_en or course.name_gr or key


def deadline_course(settings: Settings, item) -> str:
    key = getattr(item, "course_key", None)
    if key:
        return course_name(settings, str(key))
    return str(getattr(item, "course_short", "") or "")


# ---- helpers ------------------------------------------------------------------

def snapshot_items(snapshot) -> tuple:
    """A Snapshot's live `items` plus its inferred-done `done` rows, once each."""
    if snapshot is None:
        return ()
    seen: set = set()
    out = []
    for item in (*(getattr(snapshot, "items", ()) or ()), *(getattr(snapshot, "done", ()) or ())):
        key = getattr(item, "event_id", id(item))
        if key not in seen:
            seen.add(key)
            out.append(item)
    return tuple(out)


def _due(item) -> datetime | None:
    ts = getattr(item, "due_ts", None)
    if ts is None or isinstance(ts, bool):
        return None
    try:
        return datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _is_holiday(settings: Settings, day: date) -> bool:
    """A no_class_dates day inside the semester whose weekday has slots."""
    if day.isoformat() not in settings.no_class_dates:
        return False
    if settings.semester_start and day < settings.semester_start:
        return False
    if settings.semester_end and day > settings.semester_end:
        return False
    weekday = WEEKDAYS[day.weekday()]
    return any(s.weekday == weekday for s in settings.timetable)


def _hhmm(text: str) -> time:
    try:
        hours, minutes = str(text).split(":")
        return time(int(hours), int(minutes))
    except (TypeError, ValueError):
        return time.min


def _iso_date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or ""))
    except ValueError:
        return None


def _recorded_at(rec: dict, day: date) -> datetime:
    """When the guide was made, if on its own day; else the day's start."""
    try:
        stamp = datetime.fromisoformat(str(rec.get("recorded_at") or ""))
    except ValueError:
        return datetime.combine(day, time.min)
    return stamp if stamp.date() == day else datetime.combine(day, time.min)
