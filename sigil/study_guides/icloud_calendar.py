"""Read-only view of an iCloud (Apple) calendar, over CalDAV.

Why it exists: the weekly timetable in config lists every lab slot from the
first day of the semester, but labs start later and per group. The calendar is
where the real lab dates live, so the study guides ask it which labs happened.

Design rules:

* **Read only.** Only searches for events in a date window; nothing is created,
  changed or deleted.
* **An app-specific password, never the Apple ID password.** `interactive_login`
  reads it with getpass (generate it at account.apple.com > Sign-In and
  Security > App-Specific Passwords) and stores it in the OS credential store
  (`secrets_store`), never in a file.
* **The cache is the working copy.** `refresh` downloads a window of events into
  `CACHE_PATH`; everything else reads that file, so an offline hour or an iCloud
  hiccup never stops a guide — the last good copy is used and the failure logged.
* **The server expands repeating events** (one instance per date), so no
  recurrence rules are interpreted here.
"""
from __future__ import annotations

import getpass
import json
import logging
import unicodedata
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta

from ..fileio import atomic_write_text, read_text_locked
from .models import LAB, WORK_DIR, Course, Session

log = logging.getLogger(__name__)

CALDAV_URL = "https://caldav.icloud.com/"
APPLE_ID_NAME = "ICLOUD_APPLE_ID"          # names in the credential store
APP_PASSWORD_NAME = "ICLOUD_APP_PASSWORD"
CACHE_PATH = WORK_DIR / "calendar_cache.json"

WINDOW_BACK_DAYS = 14      # catch-up looks a few days back
WINDOW_AHEAD_DAYS = 60     # the planner and reminders look ahead
REQUEST_TIMEOUT_SEC = 20
STALE_AFTER_MIN = 45       # the hourly job re-downloads when the cache is older

WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
# Words in an event title that make it a lab, folded (see `_fold`): "(Εργαστήριο)",
# "(Spice)", or a title that starts with "Εργαστήριο".
LAB_WORDS = ("εργαστηριο", "spice")


@dataclass(frozen=True)
class CalEvent:
    title: str
    location: str
    start: str          # ISO local datetime, or date for an all-day event
    end: str
    calendar: str
    all_day: bool

    @property
    def day(self) -> date:
        return date.fromisoformat(self.start[:10])


class CalendarUnavailable(RuntimeError):
    """No credentials, or iCloud refused / could not be reached."""


def _stored_creds() -> tuple[str, str] | None:
    from ..secrets_store import get_secret
    apple_id, password = get_secret(APPLE_ID_NAME), get_secret(APP_PASSWORD_NAME)
    return (apple_id, password) if apple_id and password else None


def has_credentials() -> bool:
    return _stored_creds() is not None


def _load_creds() -> tuple[str, str]:
    creds = _stored_creds()
    if creds is None:
        raise CalendarUnavailable(
            "no iCloud calendar login; run: python -m sigil.study_guides calendar-login")
    return creds


def _connect(apple_id: str, app_password: str):
    import caldav
    return caldav.DAVClient(url=CALDAV_URL, username=apple_id, password=app_password,
                            timeout=REQUEST_TIMEOUT_SEC)


def interactive_login() -> int:
    apple_id = input("Apple ID (email): ").strip()
    password = getpass.getpass("App-specific password (input hidden): ").strip()
    try:
        calendars = _connect(apple_id, password).principal().calendars()
    except Exception as exc:  # noqa: BLE001 — any CalDAV/network failure means "not saved"
        print(f"Login failed ({type(exc).__name__}): {exc}\nNothing was saved.")
        return 1
    from ..secrets_store import set_secret
    set_secret(APPLE_ID_NAME, apple_id)
    set_secret(APP_PASSWORD_NAME, password)
    print(f"Saved. {len(calendars)} calendars found:")
    for cal in calendars:
        print(f"  - {cal.get_display_name()}")
    return 0


def _as_local(value) -> tuple[str, bool]:
    """(ISO string, all_day) for an icalendar DTSTART/DTEND value."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone().replace(tzinfo=None)
        return value.isoformat(timespec="minutes"), False
    return value.isoformat(), True


def is_invitation(comp) -> bool:
    """An event with an organizer or attendees came from someone's invitation.

    Anyone can send an invite to an Apple ID, and it lands in the calendar
    unanswered; a stranger's "Εργαστήριο ..." invite must not create a lab (and
    a guide build). Only events the owner created themselves count."""
    return "ORGANIZER" in comp or "ATTENDEE" in comp


def _events_of(calendar, start: datetime, end: datetime) -> list[CalEvent]:
    found = calendar.search(start=start, end=end, event=True, expand=True)
    events: list[CalEvent] = []
    for item in found:
        for comp in item.icalendar_instance.walk("VEVENT"):
            if "DTSTART" not in comp or is_invitation(comp):
                continue
            begin, all_day = _as_local(comp.decoded("DTSTART"))
            finish = _as_local(comp.decoded("DTEND"))[0] if "DTEND" in comp else begin
            events.append(CalEvent(str(comp.get("SUMMARY", "")).strip(),
                                   str(comp.get("LOCATION", "")).strip(),
                                   begin, finish, calendar.get_display_name() or "", all_day))
    return events


def refresh(today: date | None = None) -> list[CalEvent]:
    """Download the window of events, write the cache, return them."""
    today = today or date.today()
    start = datetime.combine(today - timedelta(days=WINDOW_BACK_DAYS), time.min)
    end = datetime.combine(today + timedelta(days=WINDOW_AHEAD_DAYS), time.min)
    try:
        client = _connect(*_load_creds())
        events: list[CalEvent] = []
        for calendar in client.principal().calendars():
            try:
                events += _events_of(calendar, start, end)
            except Exception as exc:  # noqa: BLE001 — one odd calendar must not hide the rest
                log.warning("calendar %r skipped: %s: %s", calendar.get_display_name(), type(exc).__name__, exc)
    except CalendarUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise CalendarUnavailable(f"iCloud calendar unreachable: {type(exc).__name__}: {exc}") from exc
    events.sort(key=lambda e: e.start)
    atomic_write_text(CACHE_PATH, json.dumps(
        {"fetched": datetime.now().isoformat(timespec="seconds"),
         "from": start.date().isoformat(), "to": end.date().isoformat(),
         "events": [asdict(e) for e in events]}, ensure_ascii=False))
    return events


_memo: dict = {"mtime": None, "data": None}


def _cache() -> dict | None:
    """The parsed cache, re-read only when the file changed (`todays_sessions`
    is called for many days in a row)."""
    try:
        mtime = CACHE_PATH.stat().st_mtime_ns
    except OSError:
        return None
    if _memo["mtime"] != mtime:
        try:
            data = json.loads(read_text_locked(CACHE_PATH))
            data["events"] = [CalEvent(**e) for e in data["events"]]
            date.fromisoformat(data["from"]), date.fromisoformat(data["to"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("calendar cache unreadable (%s: %s); ignoring it", type(exc).__name__, exc)
            data = None
        _memo.update(mtime=mtime, data=data)
    return _memo["data"]


def cached_events() -> list[CalEvent]:
    """The last downloaded events; [] when there is no cache yet."""
    data = _cache()
    return list(data["events"]) if data else []


def refresh_if_stale(now: datetime, max_age_min: int = STALE_AFTER_MIN) -> bool:
    """Re-download when the cache is older than `max_age_min`. Never raises:
    a failure is logged and the last good copy keeps serving. True = refreshed."""
    if not has_credentials():
        return False
    data = _cache()
    if data and now - datetime.fromisoformat(data["fetched"]) < timedelta(minutes=max_age_min):
        return False
    try:
        refresh(now.date())
    except CalendarUnavailable as exc:
        log.warning("calendar refresh failed, using the cached copy: %s", exc)
        return False
    return True


def events_on(day: date) -> list[CalEvent]:
    return [e for e in cached_events() if e.day == day]


def _fold(text: str) -> str:
    """Lower-case, accent-free, so "Ηλεκτρονική" matches "ΗΛΕΚΤΡΟΝΙΚΉ"."""
    decomposed = unicodedata.normalize("NFD", text.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).replace("ς", "σ")


def _course_for(title: str, courses: tuple[Course, ...],
                aliases: tuple[tuple[str, str], ...]) -> str:
    """The course key whose name (or alias) is in `title`, longest name wins; "" = none."""
    folded = _fold(title)
    names = [(_fold(a), key) for a, key in aliases]
    for c in courses:
        names += [(_fold(c.name_gr), c.key), (_fold(c.name_en), c.key)]
    hits = [(len(name), key) for name, key in names if name and name in folded]
    return max(hits)[1] if hits else ""


def _is_lab(title: str) -> bool:
    folded = _fold(title)
    return any(word in folded for word in LAB_WORDS)


def lab_sessions(day: date, courses: tuple[Course, ...],
                 aliases: tuple[tuple[str, str], ...]) -> list[Session] | None:
    """The labs the calendar lists on `day`; None when the calendar cannot say
    (no cache, or `day` outside the downloaded window) so the caller falls back
    to the weekly timetable. An empty list means "the calendar says no lab"."""
    data = _cache()
    if not data or not (date.fromisoformat(data["from"]) <= day < date.fromisoformat(data["to"])):
        return None
    found: dict[tuple, Session] = {}
    for event in data["events"]:
        if event.all_day or event.day != day or not _is_lab(event.title):
            continue
        key = _course_for(event.title, courses, aliases)
        if not key:
            continue
        same_day = event.end[:10] == event.start[:10]
        session = Session(key, WEEKDAYS[day.weekday()], event.start[11:16],
                          event.end[11:16] if same_day else "23:59", LAB)
        found[(key, session.start)] = session
    return sorted(found.values(), key=lambda s: s.start)
