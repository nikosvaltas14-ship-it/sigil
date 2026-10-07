"""Tests & finals: one store, several sources, merged.

Where the rows come from (`exam_sources.py` does the fetching):

* "moodle-calendar": course calendar events that are not module events (an
  assignment or quiz event is `assignments.py`'s job) and whose name or
  description reads like a test -> confirmed tests.
* "schedule": the course-page schedules `schedule_parser` already cached, whose
  topic reads like a test -> "possible test" unless the wording is explicit.
  Only the on-disk cache is read: no Moodle call, no model call.
* "announcement": new posts in each course's news forum that mention a test.
  A date is read by regex first; the helper model is asked only when the post
  has no single clear date. Each post is examined once (re-examined if edited).
* "exam-timetable": the department's winter exam timetable PDF, from
  https://ece.auth.gr/programma-exetastikis/, once it is published -> finals.
* "manual": entries added by hand with `add_exam`.

Design rules:

* **Sources replace only their own rows.** A calendar refresh swaps the
  calendar rows and nothing else; announcements and finals are upserted
  (their posts and PDFs are examined once, so older rows must survive); manual
  rows are never touched by a refresh.
* **Dedupe on read, not on write.** Two sources can report the same test (the
  calendar and an announcement). Both rows are stored; `load_exams` shows one
  per course+date+kind, preferring a confirmed row, then the more authoritative
  source. So a source that later drops its row cannot take the other with it.
* **`refresh_exams` never raises** and every source fails alone: no token, no
  course ids yet (not enrolled for the semester yet), offline, a
  model hiccup — each becomes an entry in `errors` and the rest still run.
* **Polite cadence.** Moodle (calendar + forums) at most every
  `MOODLE_EVERY_SEC`, the department site at most every `TIMETABLE_EVERY_SEC`,
  and the site only from `FINALS_LOOKAHEAD_DAYS` before the exam period until
  its end. The schedule source is local and runs every time.
* Store shape (`data/study_guides/exams.json`)::

      {"version": 1, "exams": [Exam.to_json()...],
       "posts": {"<discussion id>": timemodified},
       "timetables": {"<url>": {"seen_at": iso, "finals": n, "attempts": n,
                                "error": str|null}},
       "last": {"moodle": iso, "timetable": iso}, "errors": {source: str}}
"""
from __future__ import annotations

import json
import logging
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from ..fileio import atomic_write_text, file_lock, read_text_locked
from .exam_text import normalize
from .models import SCHEDULE_CACHE_PATH, WORK_DIR

log = logging.getLogger(__name__)

EXAMS_PATH = WORK_DIR / "exams.json"
STORE_VERSION = 1

KINDS = ("test", "final")
SOURCES = ("moodle-calendar", "schedule", "announcement", "exam-timetable", "manual")
# Which row wins a duplicate (after "confirmed"): lower index = more trusted.
_SOURCE_RANK = {"manual": 0, "exam-timetable": 1, "moodle-calendar": 2,
                "announcement": 3, "schedule": 4}

MOODLE_EVERY_SEC = 3 * 3600
TIMETABLE_EVERY_SEC = 24 * 3600
FINALS_LOOKAHEAD_DAYS = 45
WINDOW_SLACK_DAYS = 7              # announcement dates may start a week early
KEEP_PAST_DAYS = 60                # non-manual rows older than this are pruned
MAX_POSTS_REMEMBERED = 500
MAX_TIMETABLE_ATTEMPTS = 3

DEFAULT_EXAM_PERIOD_START = "2027-01-18"
DEFAULT_EXAM_PERIOD_END = "2027-02-12"

_LOCK = threading.Lock()

AskJson = Callable[[str, dict, str], Any]


# ---- value type -----------------------------------------------------------------

@dataclass(frozen=True)
class Exam:
    id: str                       # stable per source: "cal:123", "final:circuits2", ...
    kind: str                     # "test" | "final"
    course_key: str | None
    course_name: str
    title: str
    date: str                     # "YYYY-MM-DD"
    time: str | None = None       # "HH:MM"
    source: str = "manual"
    url: str = ""
    confirmed: bool = True        # False = "possible test"

    @property
    def day(self) -> date:
        return date.fromisoformat(self.date)

    def due_ts(self, default_time: str = "09:00") -> int:
        """Epoch seconds of the exam (a date-only exam counts from 09:00)."""
        hh, mm = (self.time or default_time).split(":")
        return int(datetime(self.day.year, self.day.month, self.day.day,
                            int(hh), int(mm)).astimezone().timestamp())

    def to_json(self) -> dict:
        return {"id": self.id, "kind": self.kind, "course_key": self.course_key,
                "course_name": self.course_name, "title": self.title,
                "date": self.date, "time": self.time, "source": self.source,
                "url": self.url, "confirmed": self.confirmed}

    @classmethod
    def from_json(cls, raw: Any) -> "Exam | None":
        """Rebuild from `to_json` output; None for anything malformed."""
        if not isinstance(raw, dict):
            return None
        exam_id, day = raw.get("id"), clean_date(raw.get("date"))
        if not isinstance(exam_id, str) or not exam_id or day is None:
            return None
        kind = raw.get("kind") if raw.get("kind") in KINDS else "test"
        source = raw.get("source") if raw.get("source") in SOURCES else "manual"
        key = raw.get("course_key")
        return cls(id=exam_id, kind=kind,
                   course_key=key if isinstance(key, str) and key else None,
                   course_name=str(raw.get("course_name") or ""),
                   title=str(raw.get("title") or ""), date=day,
                   time=clean_time(raw.get("time")), source=source,
                   url=str(raw.get("url") or ""),
                   confirmed=bool(raw.get("confirmed", True)))


@dataclass(frozen=True)
class RefreshResult:
    exams: tuple[Exam, ...] = ()          # merged + deduped, soonest first
    new: tuple[Exam, ...] = ()            # rows this refresh added
    errors: dict = field(default_factory=dict)   # {source: why}
    ran: tuple[str, ...] = ()             # sources that actually ran


def clean_date(value: Any) -> str | None:
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat() if value else None
    except ValueError:
        return None


def clean_time(value: Any) -> str | None:
    text = str(value or "").strip().replace(".", ":")
    parts = text.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return None
    hh, mm = int(parts[0]), int(parts[1])
    return f"{hh:02d}:{mm:02d}" if 0 <= hh <= 23 and 0 <= mm <= 59 else None


# ---- merge (pure) -------------------------------------------------------------------

def replace_source(rows: Iterable[Exam], source: str, new_rows: Iterable[Exam]) -> list[Exam]:
    """`rows` with every row of `source` swapped for `new_rows`. Never drops manual rows."""
    if source == "manual":
        raise ValueError("manual rows are never replaced by a refresh")
    return [r for r in rows if r.source != source] + list(new_rows)


def upsert(rows: Iterable[Exam], new_rows: Iterable[Exam]) -> list[Exam]:
    """`rows` with `new_rows` added, replacing any row with the same id."""
    fresh = {r.id: r for r in new_rows}
    return [r for r in rows if r.id not in fresh] + list(fresh.values())


def dedupe(rows: Iterable[Exam]) -> list[Exam]:
    """One row per course+date+kind (confirmed first, then trusted source), by date."""
    best: dict[tuple, Exam] = {}
    for row in rows:
        group = (row.course_key or normalize(row.course_name), row.date, row.kind)
        held = best.get(group)
        if held is None or _rank(row) < _rank(held):
            best[group] = row
    return sorted(best.values(), key=lambda e: (e.date, e.time or "99:99", e.id))


def _rank(row: Exam) -> tuple:
    return (0 if row.confirmed else 1, _SOURCE_RANK.get(row.source, 9), row.id)


def supersede(rows: Iterable[Exam], moves: dict, today: date) -> list[Exam]:
    """Drop the old-date announcement rows that a postponement post replaced.

    For each move (see `exam_sources.move_record`), the candidates are future
    announcement rows of the same course and kind from OTHER posts, on a date
    the post does not announce. Those whose date the post also mentions ("from
    12/11 to 20/11") are dropped; if it mentions none, a lone candidate is
    dropped. Two or more unmentioned candidates are left alone (which of them
    moved is a guess); the old one then fades after its date.
    """
    rows = list(rows)
    for did, move in moves.items():
        if not isinstance(move, dict):
            continue
        new_dates = set(move.get("new_dates") or ())
        candidates = [r for r in rows if r.source == "announcement"
                      and r.course_key == move.get("course_key")
                      and r.kind in (move.get("kinds") or ())
                      and not r.id.startswith(f"ann:{did}:")
                      and r.date not in new_dates and r.date >= today.isoformat()]
        mentioned = set(move.get("mentioned") or ())
        doomed = [r for r in candidates if r.date in mentioned]
        if not doomed and len(candidates) == 1:
            doomed = candidates
        drop = {r.id for r in doomed}
        rows = [r for r in rows if r.id not in drop]
    return rows


def prune(rows: Iterable[Exam], today: date) -> list[Exam]:
    """Drop non-manual rows more than KEEP_PAST_DAYS in the past."""
    oldest = (today - timedelta(days=KEEP_PAST_DAYS)).isoformat()
    return [r for r in rows if r.source == "manual" or r.date >= oldest]


def upcoming(exams: Iterable[Exam], now: datetime | None = None,
             days: float | None = None, kind: str | None = None) -> list[Exam]:
    """Exams from today on (within `days` if given, of `kind` if given), soonest first."""
    today = (now or datetime.now()).date()
    last = (today + timedelta(days=days)).isoformat() if days is not None else "9999-12-31"
    return [e for e in exams if today.isoformat() <= e.date <= last
            and (kind is None or e.kind == kind)]


# ---- store ------------------------------------------------------------------------

def read_store(path: Path = EXAMS_PATH) -> dict:
    try:
        raw = json.loads(read_text_locked(path))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("study exams: %s is unreadable (%s); treating it as empty",
                    Path(path).name, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_store(path: Path, doc: dict) -> None:
    atomic_write_text(path, json.dumps({**doc, "version": STORE_VERSION},
                                       indent=2, ensure_ascii=False))


def stored_rows(doc: dict) -> list[Exam]:
    raw = doc.get("exams")
    return [e for e in (Exam.from_json(r) for r in raw) if e] if isinstance(raw, list) else []


def load_exams(path: Path = EXAMS_PATH) -> list[Exam]:
    """Every known exam, deduped, soonest first. Disk only: fast."""
    return dedupe(stored_rows(read_store(path)))


def timetable_links(path: Path = EXAMS_PATH) -> dict[str, dict]:
    """{url: info} for every exam-timetable link found so far."""
    raw = read_store(path).get("timetables")
    return {k: v for k, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}


# ---- manual entries -------------------------------------------------------------

def add_manual(course: str, day: str, time: str | None = None, kind: str = "test",
               title: str = "", *, cfg=None, path: Path = EXAMS_PATH) -> Exam:
    """Save a test/final added by hand. Raises ValueError on a bad date/kind.

    `course` may be a course key, a Greek or English name or a fragment of one;
    with `cfg` it is matched against the configured courses, otherwise kept as
    typed. Returns the saved row.
    """
    iso = clean_date(day)
    if iso is None:
        raise ValueError(f"not a date: {day!r} (use YYYY-MM-DD)")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    key, name = match_course(course, cfg)
    label = title.strip() or ("Final exam" if kind == "final" else "Test")
    exam = Exam(id=f"manual:{uuid.uuid4().hex[:10]}", kind=kind, course_key=key,
                course_name=name, title=label, date=iso, time=clean_time(time),
                source="manual", confirmed=True)
    with file_lock(path):
        doc = read_store(path)
        _write_store(path, {**doc, "exams": [e.to_json() for e in
                                             [*stored_rows(doc), exam]]})
    log.info("study exams: added %s %s on %s", kind, key or name, iso)
    return exam


def remove_manual(exam_id: str, *, path: Path = EXAMS_PATH) -> bool:
    """Delete one manual row by id; False when there is no such manual row."""
    with file_lock(path):
        doc = read_store(path)
        rows = stored_rows(doc)
        keep = [r for r in rows if not (r.id == exam_id and r.source == "manual")]
        if len(keep) == len(rows):
            return False
        _write_store(path, {**doc, "exams": [e.to_json() for e in keep]})
    return True


def match_course(text: str, cfg=None) -> tuple[str | None, str]:
    """(course key, Greek name) for `text`, or (None, text) when nothing matches."""
    typed = str(text or "").strip()
    if cfg is None or not typed:
        return None, typed
    from .settings import load_settings
    wanted = normalize(typed)
    for course in load_settings(cfg).courses:
        names = (course.key, course.name_gr, course.name_en, course.vault_folder)
        if any(wanted == normalize(n) for n in names) or any(
                wanted in normalize(n) for n in names[1:] if len(wanted) >= 4):
            return course.key, course.name_gr
    return None, typed


# ---- the window exams can fall in ---------------------------------------------------

def exam_period(cfg) -> tuple[date, date]:
    """(start, end) of the winter exam period from config (approximate defaults)."""
    start = clean_date(cfg.get("study_exam_period_start")) or DEFAULT_EXAM_PERIOD_START
    end = clean_date(cfg.get("study_exam_period_end")) or DEFAULT_EXAM_PERIOD_END
    return date.fromisoformat(start), date.fromisoformat(end)


def date_window(cfg, settings, today: date) -> tuple[date, date]:
    """Dates a test/final may plausibly fall on: semester start - 7d .. exam period end."""
    _, period_end = exam_period(cfg)
    start = settings.semester_start or today
    return start - timedelta(days=WINDOW_SLACK_DAYS), max(period_end, today)


# ---- refresh ------------------------------------------------------------------------

def refresh_exams(cfg, *, now: datetime | None = None, force: bool = False,
                  path: Path = EXAMS_PATH, client=None, transport=None, fetch=None,
                  ask: AskJson | None = None, download=None,
                  schedule_cache_path: Path = SCHEDULE_CACHE_PATH) -> RefreshResult:
    """Run the sources that are due, merge, save. Never raises.

    `client` (anything with `.call`), `transport` (httpx, for the default
    client), `fetch(url) -> html`, `download(url) -> (bytes, content_type)` and
    `ask(prompt, schema, model)` are injectable for tests.
    """
    if not _LOCK.acquire(blocking=False):
        return RefreshResult(exams=tuple(load_exams(path)), errors={"busy": "refresh running"})
    try:
        return _refresh_locked(cfg, (now or datetime.now()).astimezone(), force, Path(path),
                               client, transport, fetch, ask, download,
                               Path(schedule_cache_path))
    except Exception:  # noqa: BLE001 — the contract is "never raises"
        log.exception("study exams: refresh failed unexpectedly")
        return RefreshResult(exams=tuple(load_exams(path)), errors={"internal": "error"})
    finally:
        _LOCK.release()


def _due(last_iso: Any, now: datetime, every_sec: int, force: bool) -> bool:
    if force:
        return True
    try:
        last = datetime.fromisoformat(str(last_iso))
    except ValueError:
        return True
    return not (0 <= now.timestamp() - last.timestamp() < every_sec)


def _refresh_locked(cfg, now, force, path, client, transport, fetch, ask, download,
                    schedule_cache_path) -> RefreshResult:
    from . import exam_sources as src
    from .settings import load_settings

    settings = load_settings(cfg)
    doc = read_store(path)
    last = doc.get("last") if isinstance(doc.get("last"), dict) else {}
    low, high = date_window(cfg, settings, now.date())
    ctx = src.SourceContext(settings=settings, now=now, low=low, high=high,
                            ask=ask, fetch=fetch, download=download)
    patch = _Patch()

    _run(patch, "schedule", lambda: patch.replace(
        "schedule", src.schedule_exams(ctx, schedule_cache_path)))
    if _due(last.get("moodle"), now, MOODLE_EVERY_SEC, force):
        _run_moodle(patch, ctx, doc, client, transport)
    period_start, period_end = exam_period(cfg)
    in_finals_window = (period_start - timedelta(days=FINALS_LOOKAHEAD_DAYS)
                        <= now.date() <= period_end)
    if in_finals_window and _due(last.get("timetable"), now, TIMETABLE_EVERY_SEC, force):
        patch.last["timetable"] = now.isoformat(timespec="seconds")
        _run(patch, "exam-timetable", lambda: _run_timetable(patch, ctx, doc))
    return _save(path, patch, now)


def _run(patch: "_Patch", source: str, job: Callable[[], Any]) -> None:
    try:
        job()
        patch.ran.append(source)
    except Exception as exc:  # noqa: BLE001 — one source failing must not stop the rest
        log.warning("study exams: %s source failed (%s)", source, exc)
        patch.errors[source] = str(exc)[:200] or type(exc).__name__


def _run_moodle(patch: "_Patch", ctx, doc: dict, client, transport) -> None:
    from . import exam_sources as src
    own_client = client is None
    if own_client:
        from .moodle import MoodleClient, load_token
        token = load_token()
        if not token:
            patch.errors["moodle"] = "no_token"
            return
        client = MoodleClient(ctx.settings.moodle_url, token, timeout=20.0, retries=1,
                              transport=transport)
    try:
        courses = _courses_with_ids(ctx, client)
        if not courses:
            patch.errors["moodle"] = "no course ids yet"
            return
        patch.last["moodle"] = ctx.now.isoformat(timespec="seconds")
        _run(patch, "moodle-calendar", lambda: patch.replace(
            "moodle-calendar", src.calendar_exams(ctx, client, courses)))
        _run(patch, "announcement", lambda: _run_announcements(patch, ctx, doc, client, courses))
    finally:
        if own_client:
            client.close()


def _courses_with_ids(ctx, client) -> list:
    """Courses with a Moodle id, looking the missing ones up once enrolled.

    The nightly guide run only discovers ids on a class day; this lets the
    reminders find the new semester's pages the day the student is enrolled.
    """
    courses = list(ctx.settings.courses)
    if any(not c.moodle_course_id for c in courses):
        from .run import discover_ids
        found = discover_ids(ctx.settings, client)
        if found:
            courses = [replace(c, moodle_course_id=found.get(c.key, c.moodle_course_id))
                       for c in courses]
    return [c for c in courses if c.moodle_course_id]


def _run_announcements(patch: "_Patch", ctx, doc: dict, client, courses) -> None:
    from . import exam_sources as src
    seen = doc.get("posts") if isinstance(doc.get("posts"), dict) else {}
    moves: dict = {}
    rows, processed = src.announcement_exams(ctx, client, courses, seen, moves)
    patch.posts.update(processed)
    prefixes = tuple(f"ann:{d}:" for d in processed)
    patch.drop_ids_with(prefixes)
    patch.upsert(rows)
    if moves:
        patch.supersede(moves, ctx.now.date())


def _run_timetable(patch: "_Patch", ctx, doc: dict) -> None:
    from . import exam_sources as src
    known = doc.get("timetables") if isinstance(doc.get("timetables"), dict) else {}
    for url in src.find_exam_timetable_urls(ctx.fetch, academic_year=ctx.academic_year):
        info = known.get(url) if isinstance(known.get(url), dict) else {}
        attempts = int(info.get("attempts") or 0)
        if info.get("finals") or attempts >= MAX_TIMETABLE_ATTEMPTS:
            continue
        entry = {"seen_at": info.get("seen_at") or ctx.now.isoformat(timespec="seconds"),
                 "attempts": attempts + 1, "finals": 0, "error": None}
        try:
            finals = src.finals_from_timetable(ctx, url)
            entry["finals"] = len(finals)
            # A newer timetable version replaces the older dates of its courses.
            patch.drop_ids_with(tuple(f"final:{k}:" for k in {f.course_key for f in finals}))
            patch.upsert(finals)
        except Exception as exc:  # noqa: BLE001 — the link alone is still worth sending
            log.warning("study exams: couldn't read the exam timetable (%s)", exc)
            entry["error"] = str(exc)[:200] or type(exc).__name__
        patch.timetables[url] = entry


class _Patch:
    """What one refresh wants to change, applied to a fresh read at save time."""

    def __init__(self) -> None:
        self.ops: list[tuple[str, Any]] = []
        self.posts: dict[str, int] = {}
        self.timetables: dict[str, dict] = {}
        self.last: dict[str, str] = {}
        self.errors: dict[str, str] = {}
        self.ran: list[str] = []

    def replace(self, source: str, rows: list[Exam]) -> None:
        self.ops.append(("replace", (source, rows)))

    def upsert(self, rows: list[Exam]) -> None:
        self.ops.append(("upsert", rows))

    def drop_ids_with(self, prefixes: tuple[str, ...]) -> None:
        if prefixes:
            self.ops.append(("drop", prefixes))

    def supersede(self, moves: dict, today: date) -> None:
        self.ops.append(("supersede", (moves, today)))

    def apply(self, rows: list[Exam]) -> list[Exam]:
        for op, arg in self.ops:
            if op == "replace":
                rows = replace_source(rows, *arg)
            elif op == "upsert":
                rows = upsert(rows, arg)
            elif op == "supersede":
                rows = supersede(rows, *arg)
            else:
                rows = [r for r in rows if not r.id.startswith(arg)]
        return rows


def _save(path: Path, patch: _Patch, now: datetime) -> RefreshResult:
    with file_lock(path):
        doc = read_store(path)
        before = {r.id for r in stored_rows(doc)}
        rows = prune(patch.apply(stored_rows(doc)), now.date())
        posts = {**_dict(doc.get("posts")), **patch.posts}
        if len(posts) > MAX_POSTS_REMEMBERED:
            posts = dict(sorted(posts.items(), key=lambda kv: -int(kv[1] or 0))
                         [:MAX_POSTS_REMEMBERED])
        new_doc = {**doc, "exams": [r.to_json() for r in rows], "posts": posts,
                   "timetables": {**_dict(doc.get("timetables")), **patch.timetables},
                   "last": {**_dict(doc.get("last")), **patch.last},
                   "errors": patch.errors}
        try:
            _write_store(path, new_doc)
        except OSError as exc:
            log.warning("study exams: could not save %s (%s)", path.name, exc)
    new = tuple(r for r in dedupe(rows) if r.id not in before)
    if new:
        log.info("study exams: %d new test/exam row(s)", len(new))
    return RefreshResult(exams=tuple(dedupe(rows)), new=new, errors=dict(patch.errors),
                         ran=tuple(patch.ran))


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


__all__ = ["Exam", "RefreshResult", "EXAMS_PATH", "KINDS", "SOURCES", "refresh_exams",
           "load_exams", "upcoming", "add_manual", "remove_manual", "match_course",
           "timetable_links", "exam_period", "date_window", "replace_source", "upsert",
           "dedupe", "supersede", "prune", "read_store", "stored_rows", "clean_date", "clean_time"]
