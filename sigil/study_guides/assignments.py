"""University deadlines (assignments and quizzes) from ΑΠΘ e-learning, cached.

The source is `core_calendar_get_action_events_by_timesort`: the same list
the Moodle timeline block shows, with assignments and quizzes together.
One `mod_assign_get_assignments` call adds the assignment id and cut-off.

Design rules:

* **`refresh` never raises.** It is called from the reminders job, the
  weekly review and the exam refresh; each of them wants a
  `Snapshot` back, whatever happened. Failures become `Snapshot.error`:
  "no_token" (never logged in), "invalid_token" (run login again) or
  "offline: ..." (anything else) — and the last good items are kept.
* **One refresh at a time, and callers never wait for it.** A module lock
  covers the whole check-then-fetch-then-write. A caller that finds it held
  gets the cache immediately instead of a second, racing fetch.
* **Polite to the site.** A refresh inside `MIN_REFRESH_SEC` of the last
  attempt (success or failure) returns the cache; a forced refresh has its own, shorter `FORCED_REFRESH_SEC` floor. How often
  to refresh at all is the caller's timer; these floors only stop bursts.
* **The cache is plain JSON.** Times are ISO strings and epoch seconds, never
  datetime objects, so `json.dumps` cannot fail and other tools can read the
  file without importing this package. Shape of `deadlines.json`::

      {"version": 1, "fetched_at": iso|null, "attempted_at": iso|null,
       "error": str|null, "items": [Deadline.to_json()...],
       "done": [Deadline.to_json()...], "seen": {"<cmid>": due_ts},
       "checked": {"<assign_id>": {"status": str, "at": iso}}}

* **"Submitted?" is an inference, and a cautious one.** Moodle drops an item
  from the timeline once its action is complete — but it also drops it once
  it falls behind `timesortfrom`. So an item seen before that has vanished
  counts as "submitted?" only while its due time is still inside the lookback
  window; older never-submitted items (the probe found April ones lingering
  for months) are simply forgotten.
* **`check_submission` is on demand only.** It calls
  `mod_assign_get_submission_status`, which a probe suggested may create an
  empty "new" submission record server-side (unverified: "new" is also what
  Moodle reports when nothing exists). Never call it from a poll.
* **Quizzes count as deadlines.** Every timeline item is kept; `kind` is the
  Moodle module name ("assign", "quiz", ...).

The client and settings modules are imported inside the functions that
fetch, so reading the cache (`cached`, `Deadline.from_json`) touches no
network code. Importing this module still runs the package `__init__`
(PyMuPDF and friends), so Qt modules import it lazily, inside workers.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

from ..fileio import atomic_write_text, file_lock, read_text_locked
from . import hidden
from .models import WORK_DIR

log =logging.getLogger(__name__)

CACHE_PATH = WORK_DIR / "deadlines.json"
CACHE_VERSION = 1

MIN_REFRESH_SEC = 15 * 60          # floor between automatic refreshes
FORCED_REFRESH_SEC = 60            # floor for a forced refresh
CLIENT_TIMEOUT_SEC = 20.0
CLIENT_RETRIES = 1

PAGE_SIZE = 50                     # Moodle's maximum limitnum for this call
MAX_PAGES = 5                      # 250 timeline items is far past a semester
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_HORIZON_DAYS = 21
MAX_LOOKBACK_DAYS = 60
MAX_HORIZON_DAYS = 180
SECONDS_PER_DAY = 86400
MAX_ERROR_CHARS = 200

# mod_assign_get_assignments warns "No access rights in module context" (code
# "1") for every hidden module in every course; they are noise.
IGNORED_WARNING_CODES = frozenset({"1"})

# Deadline.status values.
OPEN = "open"
SUBMITTED_INFERRED = "submitted?"
SUBMITTED = "submitted"
NOT_SUBMITTED = "not submitted"
STATUSES = frozenset({OPEN, SUBMITTED_INFERRED, SUBMITTED, NOT_SUBMITTED})

# Snapshot.error values (besides "offline: <why>").
NO_TOKEN = "no_token"
INVALID_TOKEN = "invalid_token"
OFFLINE_PREFIX = "offline: "

# mod_assign_get_submission_status -> Deadline.status. "new" is what Moodle
# reports when nothing was submitted; a draft is not a submission either.
_SUBMISSION_STATUS = {"submitted": SUBMITTED, "new": NOT_SUBMITTED,
                      "draft": NOT_SUBMITTED, "reopened": NOT_SUBMITTED}

# Only one refresh at a time in this process (see the module docstring).
_LOCK = threading.Lock()


# ---- value types --------------------------------------------------------------

@dataclass(frozen=True)
class Deadline:
    event_id: int
    cmid: int | None            # ?id= in the event url (== event.instance on ΑΠΘ)
    kind: str                   # Moodle modulename: "assign", "quiz", ...
    eventtype: str              # "due" (assign), "close" (quiz), ...
    course_id: int
    course_short: str
    course_key: str | None      # the settings course whose moodle_course_id matches
    title: str                  # activityname, else the event name
    due_ts: int                 # epoch seconds (event.timesort)
    overdue: bool
    actionable: bool
    url: str
    assign_id: int | None = None   # mod_assign id (not the cmid), for assign rows
    cutoff_ts: int | None = None   # 0/unset in Moodle -> None
    status: str = OPEN
    course_full: str = ""          # Moodle's full course name (the shortname can be a number)

    @property
    def due(self) -> datetime:
        """The due time as an aware local datetime (never stored)."""
        return datetime.fromtimestamp(self.due_ts).astimezone()

    def to_json(self) -> dict:
        return {"event_id": self.event_id, "cmid": self.cmid, "kind": self.kind,
                "eventtype": self.eventtype, "course_id": self.course_id,
                "course_short": self.course_short, "course_key": self.course_key,
                "title": self.title, "due_ts": self.due_ts, "overdue": self.overdue,
                "actionable": self.actionable, "url": self.url,
                "assign_id": self.assign_id, "cutoff_ts": self.cutoff_ts,
                "status": self.status, "course_full": self.course_full}

    @classmethod
    def from_json(cls, raw: Any) -> "Deadline | None":
        """Rebuild from `to_json` output; None for anything malformed."""
        if not isinstance(raw, dict):
            return None
        event_id, due_ts = _int(raw.get("event_id")), _int(raw.get("due_ts"))
        if event_id is None or due_ts is None:
            return None
        status = raw.get("status")
        return cls(event_id=event_id, cmid=_int(raw.get("cmid")),
                   kind=str(raw.get("kind") or ""),
                   eventtype=str(raw.get("eventtype") or ""),
                   course_id=_int(raw.get("course_id")) or 0,
                   course_short=str(raw.get("course_short") or ""),
                   course_key=_str_or_none(raw.get("course_key")),
                   title=str(raw.get("title") or ""), due_ts=due_ts,
                   overdue=bool(raw.get("overdue")),
                   actionable=bool(raw.get("actionable")),
                   url=str(raw.get("url") or ""),
                   assign_id=_int(raw.get("assign_id")),
                   cutoff_ts=_int(raw.get("cutoff_ts")),
                   status=status if status in STATUSES else OPEN,
                   course_full=str(raw.get("course_full") or ""))


@dataclass(frozen=True)
class Snapshot:
    """What the reminders and the weekly review read.

    `items` are the live timeline (still to do, or overdue), by due time;
    `done` are the "submitted?" inferences (RECENTLY DONE).
    """
    items: tuple[Deadline, ...] = ()
    fetched_at_iso: str | None = None   # last successful fetch
    error: str | None = None            # None | "no_token" | "invalid_token" | "offline: ..."
    done: tuple[Deadline, ...] = ()

    @property
    def fetched_at(self) -> datetime | None:
        return _parse_iso(self.fetched_at_iso)

    @property
    def is_offline(self) -> bool:
        return bool(self.error and self.error.startswith(OFFLINE_PREFIX))

    def due_within(self, days: float, now: datetime | None = None) -> list[Deadline]:
        """Open items due from now up to `days` ahead, soonest first."""
        start = _epoch(now)
        end = start + days * SECONDS_PER_DAY
        return [d for d in self.items if start <= d.due_ts <= end]

    def overdue(self, now: datetime | None = None) -> list[Deadline]:
        """Items whose due time has passed but that are still on the timeline."""
        cutoff = _epoch(now)
        return [d for d in self.items if d.due_ts < cutoff]

    def age_sec(self, now: datetime | None = None) -> float | None:
        fetched = self.fetched_at
        return None if fetched is None else _epoch(now) - fetched.timestamp()


# ---- fetching -----------------------------------------------------------------

def fetch_deadlines(client, now: datetime, lookback_days: int, horizon_days: int,
                    *, course_keys: dict[int, str] | None = None,
                    page_size: int = PAGE_SIZE) -> list[Deadline]:
    """Every timeline item due in [now - lookback, now + horizon], by due time.

    `client` is a `MoodleClient` (anything with `.call`). Raises whatever the
    client raises (`MoodleError`, `InvalidToken`); `refresh` sorts those out.
    `course_keys` maps a Moodle course id to a settings course key.
    """
    now_ts = int(_epoch(now))
    events = _timeline_events(client, now_ts - lookback_days * SECONDS_PER_DAY,
                              now_ts + horizon_days * SECONDS_PER_DAY, page_size)
    keys = course_keys or {}
    deadlines = [d for d in (_deadline_from_event(e, keys) for e in events) if d]
    assign_courses = sorted({d.course_id for d in deadlines if d.kind == "assign"})
    if assign_courses:
        details = _assign_details(client, assign_courses)
        deadlines = [_with_assign_details(d, details) for d in deadlines]
    unique = {d.event_id: d for d in deadlines}          # paging overlap guard
    return sorted(unique.values(), key=lambda d: (d.due_ts, d.event_id))


def _timeline_events(client, time_from: int, time_to: int, page_size: int) -> list[dict]:
    events: list[dict] = []
    after_id = 0
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"timesortfrom": time_from, "timesortto": time_to,
                                  "limitnum": page_size,
                                  "limittononsuspendedevents": 1}
        if after_id:
            params["aftereventid"] = after_id
        reply = client.call("core_calendar_get_action_events_by_timesort", **params)
        page = reply.get("events") if isinstance(reply, dict) else None
        if not isinstance(page, list):
            from .moodle import MoodleError
            raise MoodleError("core_calendar_get_action_events_by_timesort: "
                              "no events list in the reply")
        events.extend(e for e in page if isinstance(e, dict))
        last_id = _int(reply.get("lastid"))
        if len(page) < page_size or not last_id or last_id == after_id:
            return events
        after_id = last_id
    log.warning("study deadlines: stopped paging after %d pages", MAX_PAGES)
    return events


def _deadline_from_event(event: dict, course_keys: dict[int, str]) -> Deadline | None:
    event_id, due_ts = _int(event.get("id")), _int(event.get("timesort"))
    if event_id is None or due_ts is None:
        return None
    course = event.get("course") if isinstance(event.get("course"), dict) else {}
    course_id = _int(course.get("id")) or _int(event.get("courseid")) or 0
    action = event.get("action") if isinstance(event.get("action"), dict) else {}
    url = str(event.get("url") or action.get("url") or "")
    return Deadline(
        event_id=event_id,
        cmid=_cmid_from_url(url) or _int(event.get("instance")),
        kind=str(event.get("modulename") or ""),
        eventtype=str(event.get("eventtype") or ""),
        course_id=course_id,
        course_short=str(course.get("shortname") or ""),
        course_full=str(course.get("fullname") or "").strip(),
        course_key=course_keys.get(course_id),
        title=str(event.get("activityname") or event.get("name") or "").strip(),
        due_ts=due_ts,
        overdue=bool(event.get("overdue")),
        actionable=bool(action.get("actionable", True)),
        url=url,
    )


def _cmid_from_url(url: str) -> int | None:
    values = parse_qs(urlsplit(url).query).get("id") if url else None
    return _int(values[0]) if values else None


def _assign_details(client, course_ids: list[int]) -> dict[int, tuple[int, int | None]]:
    """{cmid: (assign_id, cutoff_ts|None)} from one mod_assign_get_assignments call.

    A failure here (other than a bad token) only costs the enrichment: the
    deadlines themselves are already in hand.
    """
    from .moodle import InvalidToken, MoodleError
    try:
        reply = client.call("mod_assign_get_assignments", courseids=course_ids)
    except InvalidToken:
        raise
    except MoodleError as exc:
        log.warning("study deadlines: assignment details unavailable (%s)", exc)
        return {}
    if not isinstance(reply, dict):
        return {}
    _log_warnings(reply.get("warnings"))
    details: dict[int, tuple[int, int | None]] = {}
    for course in reply.get("courses") or []:
        for assign in (course.get("assignments") or []) if isinstance(course, dict) else []:
            if not isinstance(assign, dict):
                continue
            cmid, assign_id = _int(assign.get("cmid")), _int(assign.get("id"))
            if cmid and assign_id:
                details[cmid] = (assign_id, _int(assign.get("cutoffdate")) or None)
    return details


def _log_warnings(warnings: Any) -> None:
    for warning in warnings if isinstance(warnings, list) else []:
        if isinstance(warning, dict) and str(warning.get("warningcode")) not in IGNORED_WARNING_CODES:
            log.warning("study deadlines: mod_assign_get_assignments warning %s: %s",
                        warning.get("warningcode"), warning.get("message"))


def _with_assign_details(deadline: Deadline,
                         details: dict[int, tuple[int, int | None]]) -> Deadline:
    found = details.get(deadline.cmid or 0)
    if deadline.kind != "assign" or not found:
        return deadline
    return replace(deadline, assign_id=found[0], cutoff_ts=found[1])


# ---- refresh and cache ------------------------------------------------------

def cached(path: Path = CACHE_PATH) -> Snapshot:
    """The last saved snapshot, from disk only (never the network)."""
    return _snapshot_from_doc(_read_cache(path))


def refresh(cfg, *, force: bool = False, now: datetime | None = None,
            path: Path = CACHE_PATH, transport=None) -> Snapshot:
    """Fetch deadlines from Moodle if the floors allow, save and return them.

    Never raises. Returns the cache untouched when another refresh is running
    or the last attempt is too recent. `transport` is an httpx transport for
    tests; `path` the cache file.
    """
    if not _LOCK.acquire(blocking=False):
        log.debug("study deadlines: a refresh is already running; using the cache")
        return cached(path)
    try:
        return _refresh_locked(cfg, force, _aware(now), Path(path), transport)
    except Exception:   # the contract is "never raises"; keep the old data
        log.exception("study deadlines: refresh failed unexpectedly")
        return _record_failure(Path(path), _aware(now), OFFLINE_PREFIX + "internal error")
    finally:
        _LOCK.release()


def _refresh_locked(cfg, force: bool, now: datetime, path: Path, transport) -> Snapshot:
    doc = _read_cache(path)
    floor = FORCED_REFRESH_SEC if force else MIN_REFRESH_SEC
    last = _latest(_parse_iso(doc.get("attempted_at")), _parse_iso(doc.get("fetched_at")))
    if last is not None and 0 <= now.timestamp() - last.timestamp() < floor:
        return _snapshot_from_doc(doc)

    import httpx
    from .moodle import InvalidToken, MoodleClient, MoodleError, load_token
    from .settings import load_settings

    settings = load_settings(cfg)
    token = load_token()
    if not token:
        # No request was made, so this does not count as an attempt: a login
        # followed by Refresh must not wait out the floor.
        return _record_failure(path, now, NO_TOKEN, attempted=False)
    lookback = _bounded(cfg.get("study_deadlines_lookback_days"),
                        DEFAULT_LOOKBACK_DAYS, MAX_LOOKBACK_DAYS)
    horizon = _bounded(cfg.get("study_deadlines_horizon_days"),
                       DEFAULT_HORIZON_DAYS, MAX_HORIZON_DAYS)
    course_keys = {c.moodle_course_id: c.key for c in settings.courses if c.moodle_course_id}
    try:
        with MoodleClient(settings.moodle_url, token, timeout=CLIENT_TIMEOUT_SEC,
                          retries=CLIENT_RETRIES, transport=transport) as client:
            items = fetch_deadlines(client, now, lookback, horizon, course_keys=course_keys)
    except InvalidToken as exc:
        log.warning("study deadlines: %s", exc)
        return _record_failure(path, now, INVALID_TOKEN)
    except (MoodleError, httpx.HTTPError, OSError) as exc:
        log.warning("study deadlines: Moodle unreachable (%s)", exc)
        return _record_failure(path, now, OFFLINE_PREFIX + _short(exc))
    return _record_success(path, now, items, lookback, hidden.needles(cfg))


def _drop_hidden(items: Iterable[Deadline], skip: tuple[str, ...]) -> list[Deadline]:
    """`items` without the courses no longer being taken (study_mode_hidden_courses)."""
    return [d for d in items if not hidden.is_hidden(skip, d.course_full, d.course_short)]


def purge_hidden(cfg, path: Path = CACHE_PATH) -> int:
    """Drop hidden courses from the saved cache now, without asking Moodle.

    The refresh floor can keep a stale cache for hours, and other readers
    read the file directly. Returns how many rows were removed.
    """
    skip = hidden.needles(cfg)
    if not skip:
        return 0
    with file_lock(path):
        doc = _read_cache(path)
        removed = 0
        for field in ("items", "done"):
            rows = doc.get(field)
            if not isinstance(rows, list):
                continue
            kept = [r for r in rows if not (isinstance(r, dict) and hidden.is_hidden(
                skip, r.get("course_full"), r.get("course_short")))]
            removed += len(rows) - len(kept)
            doc[field] = kept
        if removed:
            _write_cache(path, doc)
    return removed


def _record_success(path: Path, now: datetime, items: list[Deadline],
                    lookback_days: int, skip: tuple[str, ...] = ()) -> Snapshot:
    stamp = now.isoformat(timespec="seconds")
    items = _drop_hidden(items, skip)
    with file_lock(path):
        prev = _read_cache(path)
        seen, done = infer_done(prev, items, now, lookback_days)
        done = _drop_hidden(done, skip)
        checked = _prune_checked(prev.get("checked"), [*items, *done])
        doc = {"version": CACHE_VERSION, "fetched_at": stamp, "attempted_at": stamp,
               "error": None, "items": [d.to_json() for d in items],
               "done": [d.to_json() for d in done], "seen": seen, "checked": checked}
        _write_cache(path, doc)
    log.info("study deadlines: %d open, %d recently done", len(items), len(done))
    return _snapshot_from_doc(doc)


def _record_failure(path: Path, now: datetime, error: str, *,
                    attempted: bool = True) -> Snapshot:
    """Keep the last good items; note the error (and when Moodle was tried)."""
    with file_lock(path):
        doc = {**_read_cache(path), "version": CACHE_VERSION, "error": error}
        if attempted:
            doc["attempted_at"] = now.isoformat(timespec="seconds")
        try:
            _write_cache(path, doc)
        except OSError as exc:
            log.warning("study deadlines: could not save %s (%s)", path.name, exc)
    return _snapshot_from_doc(doc)


def infer_done(prev: dict, items: list[Deadline], now: datetime,
               lookback_days: int) -> tuple[dict[str, int], list[Deadline]]:
    """(new seen map, "submitted?" items) after a successful fetch.

    `prev` is the previous cache document. An item seen before (by cmid) that
    is not in `items` any more may be inferred done — but only while its due
    time is still inside the lookback window; outside it, disappearing is just
    the window moving on, and the entry is forgotten.

    Leaving the timeline only means "done" when nothing else explains it (see
    `_vanished_as_done`): Moodle drops a quiz's event once it closes, attempted
    or not, and an assignment's once its cut-off passes. Those are forgotten,
    never shown as "submitted?". An item already inferred done stays done.
    """
    window_start = _epoch(now) - lookback_days * SECONDS_PER_DAY
    now_ts = _epoch(now)
    current = {d.cmid for d in items if d.cmid}
    open_before = {d.cmid: d for d in _deadlines(prev.get("items")) if d.cmid}
    done_before = {d.cmid: d for d in _deadlines(prev.get("done")) if d.cmid}
    old_seen = prev.get("seen") if isinstance(prev.get("seen"), dict) else {}
    seen = {str(d.cmid): d.due_ts for d in items if d.cmid}
    done: list[Deadline] = []
    for key, due_ts in old_seen.items():
        cmid, due = _int(key), _int(due_ts)
        if cmid is None or due is None or cmid in current or due < window_start:
            continue
        if cmid in done_before:
            seen[str(cmid)] = due
            done.append(done_before[cmid])
        elif cmid in open_before and _vanished_as_done(open_before[cmid], now_ts):
            seen[str(cmid)] = due
            done.append(replace(open_before[cmid], status=SUBMITTED_INFERRED,
                                overdue=False, actionable=False))
    done.sort(key=lambda d: (d.due_ts, d.event_id))
    return seen, done


def _vanished_as_done(item: Deadline, now_ts: float) -> bool:
    """Did `item` leave the timeline because it was done, not because it closed?

    An assignment stays listed (overdue) after its due date until it is
    submitted or its cut-off passes; anything else (a quiz: timeclose) leaves
    the moment it closes. So: an assignment before its cut-off, anything else
    before its due time.
    """
    if item.kind == "assign":
        return item.cutoff_ts is None or now_ts < item.cutoff_ts
    return now_ts < item.due_ts


def _snapshot_from_doc(doc: dict) -> Snapshot:
    checked = doc.get("checked") if isinstance(doc.get("checked"), dict) else {}
    items = tuple(_apply_checked(d, checked) for d in _deadlines(doc.get("items")))
    error = doc.get("error")
    return Snapshot(items=items, fetched_at_iso=_str_or_none(doc.get("fetched_at")),
                    error=str(error) if error else None,
                    done=tuple(_deadlines(doc.get("done"))))


def _apply_checked(deadline: Deadline, checked: dict) -> Deadline:
    entry = checked.get(str(deadline.assign_id)) if deadline.assign_id else None
    status = entry.get("status") if isinstance(entry, dict) else None
    return replace(deadline, status=status) if status in STATUSES else deadline


def _prune_checked(checked: Any, keep: Iterable[Deadline]) -> dict:
    ids = {str(d.assign_id) for d in keep if d.assign_id}
    return {k: v for k, v in checked.items() if k in ids} if isinstance(checked, dict) else {}


def _deadlines(raw: Any) -> list[Deadline]:
    return [d for d in (Deadline.from_json(r) for r in raw or []) if d] \
        if isinstance(raw, list) else []


def _read_cache(path: Path) -> dict:
    try:
        raw = json.loads(read_text_locked(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("study deadlines: %s is unreadable (%s); treating it as empty",
                    Path(path).name, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_cache(path: Path, doc: dict) -> None:
    atomic_write_text(path, json.dumps(doc, indent=2, ensure_ascii=False))


# ---- on-demand submission check ---------------------------------------------

def check_submission(cfg, assign_id: int, *, path: Path = CACHE_PATH,
                     transport=None) -> str:
    """Ask Moodle whether assignment `assign_id` (the mod_assign id) is submitted.

    On demand only (a context-menu action, on a worker): the call may create an
    empty "new" submission record server-side (see the module docstring). Returns
    a `Deadline.status` value, or "unknown: <why>" when Moodle cannot be asked.
    The answer is saved in the cache so the next `cached()` shows it. Never raises.
    """
    import httpx
    from .moodle import InvalidToken, MoodleClient, MoodleError, load_token
    from .settings import load_settings
    try:
        token = load_token()
        if not token:
            return f"unknown: {NO_TOKEN}"
        with MoodleClient(load_settings(cfg).moodle_url, token, timeout=CLIENT_TIMEOUT_SEC,
                          retries=CLIENT_RETRIES, transport=transport) as client:
            reply = client.call("mod_assign_get_submission_status", assignid=int(assign_id))
    except InvalidToken:
        return f"unknown: {INVALID_TOKEN}"
    except (MoodleError, httpx.HTTPError, OSError, ValueError) as exc:
        log.warning("study deadlines: submission check failed (%s)", exc)
        return f"unknown: {_short(exc)}"
    except Exception:
        log.exception("study deadlines: submission check failed unexpectedly")
        return "unknown: internal error"
    status = _submission_status(reply)
    _save_checked(path, int(assign_id), status)
    return status


def _submission_status(reply: Any) -> str:
    attempt = reply.get("lastattempt") if isinstance(reply, dict) else None
    submission = attempt.get("submission") if isinstance(attempt, dict) else None
    if not isinstance(submission, dict):
        # Team assignments report under teamsubmission; nothing at all = none.
        submission = attempt.get("teamsubmission") if isinstance(attempt, dict) else None
    raw = submission.get("status") if isinstance(submission, dict) else "new"
    return _SUBMISSION_STATUS.get(str(raw), NOT_SUBMITTED)


def _save_checked(path: Path, assign_id: int, status: str) -> None:
    try:
        with file_lock(path):
            doc = _read_cache(path)
            checked = doc.get("checked") if isinstance(doc.get("checked"), dict) else {}
            entry = {"status": status, "at": datetime.now().isoformat(timespec="seconds")}
            _write_cache(path, {**doc, "checked": {**checked, str(assign_id): entry}})
    except OSError as exc:
        log.warning("study deadlines: could not save the submission check (%s)", exc)


# ---- small helpers ----------------------------------------------------------

def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _latest(*stamps: datetime | None) -> datetime | None:
    present = [s for s in stamps if s is not None]
    return max(present, key=lambda s: s.timestamp()) if present else None


def _aware(now: datetime | None) -> datetime:
    return (now or datetime.now()).astimezone()


def _epoch(now: datetime | None) -> float:
    return _aware(now).timestamp()


def _bounded(value: Any, default: int, maximum: int) -> int:
    number = _int(value)
    return default if number is None else max(1, min(maximum, number))


def _short(exc: BaseException) -> str:
    text = str(exc) or type(exc).__name__
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS - 1] + "…"


__all__ = ["CACHE_PATH", "Deadline", "Snapshot", "fetch_deadlines", "refresh",
           "cached", "check_submission", "infer_done", "MIN_REFRESH_SEC",
           "FORCED_REFRESH_SEC", "OPEN", "SUBMITTED_INFERRED", "SUBMITTED",
           "NOT_SUBMITTED", "NO_TOKEN", "INVALID_TOKEN", "OFFLINE_PREFIX"]
