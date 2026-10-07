"""Which (day, course) guides are due now — the hourly study-guides check.

The `catchup` job runs every hour, so a guide follows its class within the
hour instead of waiting for one fixed evening run. Each tick asks `due()` for the
courses whose classes have all finished and that still have no guide, over
the last few days, so a machine that was off or a usage limit that blocked a
build is caught up. Oldest first, so lecture numbering and "previous topics"
see the earlier guide before the later one.

A course gets one guide per day (run.py picks the session), so it is due only
once ALL of that day's sessions of it have ended — the same choice a run after
the last class would make.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .settings import Settings
from .state import RunState
from .timetable import todays_sessions

# Days before today the hourly check still looks at. Three covers a Monday
# catching up on Friday.
DEFAULT_LOOKBACK_DAYS = 3

# A (day, course) whose generation already failed this many times is left
# alone: each attempt is an Opus call, and the same material tends to fail the
# same way. `python -m sigil.study_guides run --date ... --course ...` still
# rebuilds it by hand.
MAX_GENERATION_ATTEMPTS = 3

# Minutes after a course's last session ends before it is due, so slides the
# professor uploads right after class make it into the guide.
GRACE_MINUTES = 30


@dataclass(frozen=True)
class Due:
    day: date
    course_key: str
    last_reason: str        # the latest recorded attempt's reason; "" if none


def due(settings: Settings, now: datetime,
        lookback_days: int = DEFAULT_LOOKBACK_DAYS,
        state: RunState | None = None) -> list[Due]:
    """Every (day, course) from `lookback_days` ago through `now` that needs a run."""
    state = state or RunState()
    today = now.date()
    first = today - timedelta(days=max(0, lookback_days))
    latest = {(r.get("date"), r.get("course_key")): r
              for r in state.between(first.isoformat(), today.isoformat())}
    out: list[Due] = []
    day = first
    while day <= today:
        for key in _finished_courses(settings, day, now):
            day_str = day.isoformat()
            if state.already_succeeded(day_str, key):
                continue
            if state.generation_failures(day_str, key) >= MAX_GENERATION_ATTEMPTS:
                continue
            last = latest.get((day_str, key)) or {}
            out.append(Due(day, key, str(last.get("reason") or "")))
        day += timedelta(days=1)
    return out


def _finished_courses(settings: Settings, day: date, now: datetime) -> list[str]:
    """Courses with sessions on `day`, all of them over by `now`, in class order."""
    last_end: dict[str, str] = {}
    for s in todays_sessions(settings, day):
        last_end[s.course_key] = max(last_end.get(s.course_key, ""), s.end)
    if day < now.date():
        return list(last_end)
    clock = (now - timedelta(minutes=GRACE_MINUTES)).strftime("%H:%M")
    if (now - timedelta(minutes=GRACE_MINUTES)).date() < day:
        return []
    return [key for key, end in last_end.items() if end and end <= clock]
