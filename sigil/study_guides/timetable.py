"""Which sessions the student had on a given day, and which of them get a guide.

Design rules:

* **The static timetable in config is the base.** The weekly timetable is fixed
  for the semester, so a list of weekday slots is the simplest and most
  reliable answer for lectures and tutorials. **Labs are the exception**: they
  start weeks late and per group, so when the iCloud calendar covers the day its
  labs replace the timetable's lab slots (`icloud_calendar.lab_sessions`); with
  no calendar (not set up, or not downloaded yet) the timetable's labs apply.
* **An empty timetable is a normal state, not an error.** Until it is filled
  in, every day simply has no class; `no_class_reason` says so in words, so the
  run summary reads "no timetable configured" rather than looking broken.
* **The day is always passed in.** Nothing here calls `date.today()`, so a
  backfill (`run --date`) and the tests see exactly the same logic.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from .models import LAB, LECTURE, TUTORIAL, Session
from .settings import WEEKDAYS, Settings

log = logging.getLogger(__name__)


def todays_sessions(settings: Settings, day: date) -> list[Session]:
    """The day's timetable slots, in start-time order; [] when there is no class."""
    if _outside_calendar(settings, day):
        return []
    weekday = WEEKDAYS[day.weekday()]
    slots = [s for s in settings.timetable if s.weekday == weekday]
    from_calendar = _calendar_labs(settings, day)
    if from_calendar is not None:
        slots = [s for s in slots if s.type != LAB] + from_calendar
    return sorted(slots, key=lambda s: s.start)


def _calendar_labs(settings: Settings, day: date) -> list[Session] | None:
    """The calendar's labs for `day`, or None to keep the timetable's."""
    if not settings.calendar_labs or not settings.timetable:   # no timetable = not set up
        return None
    from . import icloud_calendar
    labs = icloud_calendar.lab_sessions(day, settings.courses, settings.calendar_aliases)
    if labs is None:
        return None
    known = {c.key for c in settings.courses}
    return [s for s in labs if s.course_key in known]


def has_class(settings: Settings, day: date) -> bool:
    return bool(todays_sessions(settings, day))


def no_class_reason(settings: Settings, day: date) -> str:
    """Why `day` has no sessions, in one short phrase; "" when it has some."""
    if not settings.timetable:
        return "no timetable configured (study_guides_timetable is empty)"
    if settings.semester_start and day < settings.semester_start:
        return f"before the semester starts ({settings.semester_start.isoformat()})"
    if settings.semester_end and day > settings.semester_end:
        return f"after the semester ended ({settings.semester_end.isoformat()})"
    if day.isoformat() in settings.no_class_dates:
        return "listed in no_class_dates"
    if not todays_sessions(settings, day):
        return f"no classes on {day.strftime('%A')}s"
    return ""


def lecture_number(settings: Settings, course_key: str, day: date,
                   weekdays: frozenset[str] | None = None) -> int:
    """Which lecture of the semester `day`'s lecture of `course_key` is: lecture
    slots from semester start through `day`, holidays skipped. 0 = unknown.
    `weekdays` ("MO", ...) counts only those days' slots — a course split
    between two lecturers on different days counts each strand on its own.

    The topic of last resort when a course page has no lecture schedule (ΑΠΘ
    pages often list only the labs): "Διάλεξη 3" still lets `select_files`
    match a "Διάλεξη03.pdf" or a "03-....pdf"."""
    start = settings.semester_start
    if start is None or day < start:
        return 0
    count, current = 0, start
    while current <= day:
        count += sum(1 for s in todays_sessions(settings, current)
                     if s.course_key == course_key and s.type == LECTURE
                     and (weekdays is None or s.weekday in weekdays))
        current += timedelta(days=1)
    return count


def session_decision(session: Session, settings: Settings,
                     scheduled_entry_exists: bool) -> tuple[bool, str]:
    """(generate a guide?, why) for one session.

    * Lectures always get one; without a schedule entry the topic is inferred,
      unless inference is switched off.
    * Tutorials only when the schedule lists content for that day (spec §4.1):
      a tutorial with nothing listed is usually problem-solving on last week's
      material, and a guide on a guessed topic would be noise.
    * Labs follow `skip_labs`, then behave like lectures.
    """
    if session.type == TUTORIAL:
        if scheduled_entry_exists:
            return True, "tutorial with content listed in the schedule"
        return False, "tutorial with no content listed in the schedule for today"
    if session.type == LAB and settings.skip_labs:
        return False, "labs are switched off (study_guides_skip_labs)"
    if session.type not in (LECTURE, LAB):
        log.warning("study guides: unknown session type %r for %s", session.type,
                    session.course_key)
        return False, f"unknown session type {session.type!r}"
    label = "lecture" if session.type == LECTURE else "lab session"
    if scheduled_entry_exists:
        return True, f"{label} with a scheduled topic"
    if settings.infer_topic:
        return True, f"{label} with no schedule entry; topic will be inferred"
    return False, f"{label} with no schedule entry and topic inference is off"


def _outside_calendar(settings: Settings, day: date) -> bool:
    return bool(
        (settings.semester_start and day < settings.semester_start)
        or (settings.semester_end and day > settings.semester_end)
        or day.isoformat() in settings.no_class_dates
    )
