"""Where tests & finals come from: Moodle calendar, schedules, news, the exam PDF.

Each source is a function that returns `Exam` rows and raises on failure;
`exams.refresh_exams` isolates the failures and merges the rows. Everything
that touches the outside world is injected through `SourceContext` (or the
`client` argument), so the tests run every source against fakes.

Rules:

* **Moodle is read-only here**: `core_calendar_get_calendar_events`,
  `mod_forum_get_forums_by_courses`, `mod_forum_get_forum_discussions`.
* **Module events are skipped.** An event with a `modulename` (assign, quiz)
  is already a deadline in `assignments.py`; listing it again would double
  every reminder.
* **The model is the fallback, never the first try.** An announcement's date
  is read by regex; the helper model is asked only when the post has no single
  clear date, at most `MAX_AI_CALLS` times per refresh, with no tools (the
  post is untrusted text). The exam timetable PDF is read with PyMuPDF and the
  model only picks the configured courses out of the text.
* **Downloads only from auth.gr over https**, capped at `MAX_DOWNLOAD_BYTES`.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlsplit

from ..fileio import read_text_locked
from .exam_text import (exam_kind, extract_dates, is_exam_text, is_explicit, is_quiz_only,
                        normalize, single_date)
from .exams import Exam, clean_date, clean_time
from .models import WORK_DIR

log = logging.getLogger(__name__)

PROMPT_FILE = Path(__file__).with_name("prompts") / "exam_extract.txt"

DEPARTMENT_URL = "https://ece.auth.gr/"
EXAM_TIMETABLE_PAGE = "https://ece.auth.gr/programma-exetastikis/"
ANNOUNCEMENTS_URL = "https://ece.auth.gr/anakoinoseis/"
SITE_TIMEOUT_SEC = 15.0
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_PAGE_BYTES = 5 * 1024 * 1024        # an HTML page; anything bigger is hostile
MAX_PDF_PAGES = 200
MAX_FETCH_SEC = 120          # a whole page or file, not one read
MAX_FOLLOW_PAGES = 3                 # announcement pages opened to find the PDF
MAX_MODEL_TEXT_CHARS = 80_000

CALENDAR_BACK_SEC = 86400
CALENDAR_AHEAD_DAYS = 180
DISCUSSIONS_PER_FORUM = 20
MAX_AI_CALLS = 3                     # per refresh, for announcements
OLD_POST_DAYS = 120                  # posts older than this are marked seen unread
MIN_COURSE_TOKEN = 6                 # course-name words this long filter PDF pages

AskJson = Callable[[str, dict, str], Any]

EXTRACT_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "course_key": {"type": ["string", "null"]},
            "date": {"type": "string"},
            "time": {"type": ["string", "null"]},
            "title": {"type": "string"},
            "kind": {"type": "string", "enum": ["test", "final"]},
        },
        "required": ["date", "kind"],
    },
}


@dataclass(frozen=True)
class SourceContext:
    settings: Any                    # study_guides.settings.Settings
    now: datetime
    low: date                        # earliest plausible exam date
    high: date                       # latest plausible exam date
    ask: AskJson | None = None       # ask(prompt, schema, model) -> JSON
    fetch: Callable[[str], str] | None = None
    download: Callable[[str], tuple[bytes, str]] | None = None

    @property
    def academic_year(self) -> str | None:
        start = self.settings.semester_start
        return f"{start.year}-{(start.year + 1) % 100:02d}" if start else None

    def ask_json(self, prompt: str) -> Any:
        if self.ask is not None:
            return self.ask(prompt, EXTRACT_SCHEMA, self.settings.helper_model)
        from . import generate
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        return generate.ask_json(prompt, EXTRACT_SCHEMA, self.settings.helper_model,
                                 cwd=WORK_DIR, claude_cmd=self.settings.claude_cmd)


# ---- a: Moodle course calendar ----------------------------------------------------

def calendar_exams(ctx: SourceContext, client, courses) -> list[Exam]:
    """Non-module course calendar events that read like a test (confirmed)."""
    by_id = {c.moodle_course_id: c for c in courses}
    now_ts = int(ctx.now.timestamp())
    reply = client.call(
        "core_calendar_get_calendar_events",
        events={"courseids": sorted(by_id)},
        options={"userevents": 0, "siteevents": 0,
                 "timestart": now_ts - CALENDAR_BACK_SEC,
                 "timeend": now_ts + CALENDAR_AHEAD_DAYS * 86400})
    events = reply.get("events") if isinstance(reply, dict) else None
    if not isinstance(events, list):
        raise ValueError("core_calendar_get_calendar_events: no events list in the reply")
    rows = [_calendar_row(ctx, e, by_id) for e in events if isinstance(e, dict)]
    return [r for r in rows if r]


def _calendar_row(ctx: SourceContext, event: dict, by_id: dict) -> Exam | None:
    if event.get("modulename"):
        return None
    from .moodle import html_to_text
    name = str(event.get("name") or "").strip()
    text = f"{name}\n{html_to_text(event.get('description'))}"
    course = by_id.get(_int(event.get("courseid")))
    start = _int(event.get("timestart"))
    if course is None or start is None or not is_exam_text(text):
        return None
    when = datetime.fromtimestamp(start).astimezone()
    clock = when.strftime("%H:%M")
    base = ctx.settings.moodle_url
    return Exam(id=f"cal:{event.get('id')}", kind=exam_kind(text), course_key=course.key,
                course_name=course.name_gr, title=name or "Test",
                date=when.date().isoformat(), time=None if clock == "00:00" else clock,
                source="moodle-calendar", confirmed=True,
                url=f"{base}/calendar/view.php?view=day&course={course.moodle_course_id}"
                    f"&time={start}")


# ---- b: parsed course schedules (on-disk cache only) ------------------------------

def schedule_exams(ctx: SourceContext, cache_path: Path) -> list[Exam]:
    """Schedule entries whose topic reads like a test. Never calls Moodle or a model."""
    try:
        data = json.loads(read_text_locked(cache_path))
    except FileNotFoundError:
        return []
    if not isinstance(data, dict):
        return []
    rows: list[Exam] = []
    for course in ctx.settings.courses:
        record = data.get(course.key)
        entries = record.get("entries") if isinstance(record, dict) else None
        for entry in entries if isinstance(entries, list) else []:
            row = _schedule_row(ctx, course, entry)
            if row:
                rows.append(row)
    return rows


def _schedule_row(ctx: SourceContext, course, entry: Any) -> Exam | None:
    if not isinstance(entry, dict):
        return None
    topic, day = str(entry.get("topic") or "").strip(), clean_date(entry.get("date"))
    if not day or not is_exam_text(topic) or is_quiz_only(topic):
        return None
    url = (f"{ctx.settings.moodle_url}/course/view.php?id={course.moodle_course_id}"
           if course.moodle_course_id else "")
    return Exam(id=f"sched:{course.key}:{day}", kind=exam_kind(topic), course_key=course.key,
                course_name=course.name_gr, title=topic, date=day,
                time=_class_start(ctx.settings, course.key, day, entry.get("session_type")),
                source="schedule", url=url, confirmed=is_explicit(topic))


_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def _class_start(settings, course_key: str, day: str, session_type: Any) -> str | None:
    """The timetable start time of that course on that weekday, if there is one."""
    weekday = _WEEKDAYS[date.fromisoformat(day).weekday()]
    slots = [s for s in settings.timetable if s.course_key == course_key and s.weekday == weekday]
    typed = [s for s in slots if s.type == session_type] or slots
    return min(s.start for s in typed) if typed else None


# ---- c: course announcements ------------------------------------------------------

def announcement_exams(ctx: SourceContext, client, courses, seen: dict,
                       moves: dict | None = None) -> tuple[list[Exam], dict[str, int]]:
    """(rows, {discussion id: timemodified} examined) from each course's news forum.

    A post already in `seen` with the same timemodified is skipped. A post that
    needs the model but finds the budget spent (or the model failing) is left
    out of the processed map, so the next refresh tries it again. A post that
    postpones a test ("μεταφέρεται", "postponed") is also recorded in `moves`
    (see `move_record`), so `exams.supersede` can drop the old date's row.
    """
    by_id = {c.moodle_course_id: c for c in courses}
    forums = client.call("mod_forum_get_forums_by_courses", courseids=sorted(by_id))
    if not isinstance(forums, list):
        raise ValueError("mod_forum_get_forums_by_courses: no forum list in the reply")
    budget = [MAX_AI_CALLS]
    rows: list[Exam] = []
    processed: dict[str, int] = {}
    for forum in forums:
        if not isinstance(forum, dict) or forum.get("type") != "news":
            continue
        course = by_id.get(_int(forum.get("course")))
        if course is None:
            continue
        reply = client.call("mod_forum_get_forum_discussions", forumid=_int(forum.get("id")),
                            page=0, perpage=DISCUSSIONS_PER_FORUM)
        discussions = reply.get("discussions") if isinstance(reply, dict) else None
        for disc in discussions if isinstance(discussions, list) else []:
            _examine_post(ctx, course, disc, seen, budget, rows, processed, moves)
    return rows, processed


def _examine_post(ctx, course, disc: Any, seen: dict, budget: list[int],
                  rows: list[Exam], processed: dict[str, int],
                  moves: dict | None = None) -> None:
    if not isinstance(disc, dict):
        return
    did = _int(disc.get("discussion")) or _int(disc.get("id"))
    modified = _int(disc.get("timemodified")) or _int(disc.get("created")) or 0
    if did is None or _int(seen.get(str(did))) == modified:
        return
    from .moodle import html_to_text
    subject = str(disc.get("subject") or disc.get("name") or "").strip()
    text = f"{subject}\n{html_to_text(disc.get('message'))}"
    too_old = modified and modified < ctx.now.timestamp() - OLD_POST_DAYS * 86400
    if too_old or not is_exam_text(text) or is_quiz_only(text):
        processed[str(did)] = modified
        return
    found = _post_dates(ctx, course, text, budget)
    if found is None:
        return                         # retry next refresh
    processed[str(did)] = modified
    url = f"{ctx.settings.moodle_url}/mod/forum/discuss.php?d={did}"
    if moves is not None and found:
        record = move_record(ctx, course.key, text, found)
        if record:
            moves[str(did)] = record
    for day, clock, title, kind in found:
        rows.append(Exam(id=f"ann:{did}:{day}", kind=kind, course_key=course.key,
                         course_name=course.name_gr, title=(title or subject)[:120],
                         date=day, time=clock, source="announcement", url=url,
                         confirmed=True))


# "Η πρόοδος μεταφέρεται / αναβάλλεται / μετατίθεται", "νέα ημερομηνία", "postponed".
_MOVED_RE = re.compile(
    r"\bμεταφερ\w*|\bμεταφορ\w*|\bμετατιθ\w*|\bμετατεθ\w*|\bαναβαλ\w*|\bαναβολ\w*"
    r"|\bνεα ημερομηνια|\bαλλαγη ημερομηνιασ|\bpostpon\w*|\breschedul\w*|\bmoved\b")


def move_record(ctx, course_key: str, text: str, found) -> dict | None:
    """What a postponement post supersedes, or None for an ordinary post.

    {"course_key", "kinds": [...], "new_dates": [...], "mentioned": [...]}:
    `mentioned` is every date written in the post (usually old and new).
    """
    if not _MOVED_RE.search(normalize(text)):
        return None
    mentioned = [d.isoformat() for d, _ in extract_dates(text, ctx.low, ctx.high)]
    return {"course_key": course_key, "kinds": sorted({f[3] for f in found}),
            "new_dates": sorted({f[0] for f in found}), "mentioned": mentioned}


def _post_dates(ctx, course, text: str, budget: list[int]):
    """[(date, time, title, kind)] for one post; None = ask again later."""
    hit = single_date(text, ctx.low, ctx.high)
    if hit is not None:
        return [(hit[0].isoformat(), hit[1], "", exam_kind(text))]
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    prompt = build_prompt("course announcement", ctx, text, course=course)
    try:
        raw = ctx.ask_json(prompt)
    except Exception as exc:  # noqa: BLE001 — a model hiccup: retry next time
        log.warning("study exams: the helper model couldn't read a post (%s)", exc)
        return None
    items = validate_items(raw, ctx.low, ctx.high, known_keys=None)
    return [(i["date"], i["time"], i["title"], i["kind"]) for i in items]


# ---- d: the department's exam timetable ------------------------------------------

_YEAR_PAIR = re.compile(r"(?<!\d)(20\d\d)\s*[-_/–]\s*((?:20)?\d\d)(?!\d)")
_WINTER = ("χειμεριν", "cheimerin", "xeimerin", "ιανουαρ", "ianouar", "φεβρουαρ", "fevrouar")
_OTHER_PERIOD = ("εαριν", "earin", "σεπτεμβρ", "septemvr", "ιουνι", "iouni",
                 "επαναληπτ", "epanalipt")
_NOT_TIMETABLE = ("ξενων γλωσσων", "xenon-glosson", "ορκωμοσ", "orkomos", "ωραριου", "orariou")


def is_exam_timetable_link(label: str, academic_year: str | None = None) -> bool:
    """Does a link (its decoded href + text) name the winter exam timetable?"""
    norm = normalize(unquote(label))
    if not (("προγραμμα" in norm or "programma" in norm) and ("εξετα" in norm or "exeta" in norm)):
        return False
    if any(w in norm for w in _NOT_TIMETABLE) or any(w in norm for w in _OTHER_PERIOD):
        return False
    if not any(w in norm for w in _WINTER):
        return False
    return academic_year is None or _year_ok(norm, academic_year)


def _year_ok(norm: str, academic_year: str) -> bool:
    """False only when the link names a different academic year."""
    first = int(academic_year[:4])
    pairs = [(int(a), int(b) % 100) for a, b in _YEAR_PAIR.findall(norm)]
    return not pairs or any(a == first and b == (first + 1) % 100 for a, b in pairs)


def find_exam_timetable_urls(fetch=None, *, academic_year: str | None = None) -> list[str]:
    """Winter exam timetable links on ece.auth.gr (PDFs first), or [].

    Scans the timetable page, the front page and the announcements page. An
    announcement link (HTML) is opened once to find the PDF inside it. Every
    failure is logged and skipped.
    """
    fetch = fetch or _fetch
    found: list[str] = []
    for page in (EXAM_TIMETABLE_PAGE, DEPARTMENT_URL, ANNOUNCEMENTS_URL):
        for url, label in _page_links(fetch, page):
            if is_exam_timetable_link(label, academic_year) and url not in found:
                found.append(url)
    pdfs = [u for u in found if _is_pdf_url(u)]
    for page in [u for u in found if not _is_pdf_url(u)][:MAX_FOLLOW_PAGES]:
        inner = [u for u, lbl in _page_links(fetch, page)
                 if _is_pdf_url(u) and ("εξετα" in normalize(unquote(lbl)) or
                                        "exeta" in normalize(unquote(lbl)))]
        pdfs.extend(u for u in inner if u not in pdfs)
        if not inner:
            pdfs.append(page)
    return list(dict.fromkeys(pdfs))


def find_exam_timetable_url(fetch=None, *, academic_year: str | None = None) -> str:
    """The first winter exam timetable link, or ""."""
    urls = find_exam_timetable_urls(fetch, academic_year=academic_year)
    return urls[0] if urls else ""


def _page_links(fetch, page: str) -> list[tuple[str, str]]:
    from .semester import _LinkParser
    try:
        parser = _LinkParser()
        from .moodle import _defuse_html
        parser.feed(_defuse_html(fetch(page)))
    except Exception as exc:  # noqa: BLE001 — best effort by design
        log.info("study exams: couldn't read %s (%s)", page, exc)
        return []
    out = []
    for href, text in parser.links:
        url = urljoin(page, href or "")
        if allowed_host(url):
            out.append((url, f"{url} {text}"))
    return out


def allowed_host(url: str) -> bool:
    try:
        parts = urlsplit(url)
        if "\\" in url or "@" in parts.netloc or parts.port not in (None, 443):
            return False
        host = (parts.hostname or "").lower()
    except ValueError:            # malformed netloc ("[::1].auth.gr", a bad port)
        return False
    return parts.scheme == "https" and (host == "auth.gr" or host.endswith(".auth.gr"))


def guarded_client(timeout: float):
    """An httpx client that follows redirects only within https auth.gr: the
    host check runs on every hop, before the request is sent."""
    import httpx

    def check(request) -> None:
        if not allowed_host(str(request.url)):
            raise ValueError("refusing a request outside https auth.gr")

    return httpx.Client(timeout=timeout, follow_redirects=True, max_redirects=5,
                        event_hooks={"request": [check]})


def _is_pdf_url(url: str) -> bool:
    return urlsplit(url).path.lower().endswith(".pdf")


def _fetch(url: str) -> str:
    return fetch_capped(url, SITE_TIMEOUT_SEC)


def fetch_capped(url: str, timeout: float, limit: int = MAX_PAGE_BYTES) -> str:
    """Text of an https auth.gr page, refusing more than `limit` bytes."""
    with guarded_client(timeout) as client, client.stream("GET", url) as resp:
        resp.raise_for_status()
        data = _read_capped(resp, limit, "the page")
        return data.decode(resp.encoding or "utf-8", errors="replace")


def _read_capped(resp, limit: int, what: str) -> bytes:
    """The body, refusing more than `limit` bytes or more than MAX_FETCH_SEC in
    all (the client timeout is per read, so a slow drip would never trip it)."""
    import time
    deadline = time.monotonic() + MAX_FETCH_SEC
    chunks, size = [], 0
    for chunk in resp.iter_bytes():
        size += len(chunk)
        if size > limit:
            raise ValueError(f"{what} is too large")
        if time.monotonic() > deadline:
            raise ValueError(f"{what} took longer than {MAX_FETCH_SEC}s")
        chunks.append(chunk)
    return b"".join(chunks)


def finals_from_timetable(ctx: SourceContext, url: str) -> list[Exam]:
    """Final exams for the configured courses, read from the timetable at `url`."""
    if not allowed_host(url):
        raise ValueError("not an auth.gr https link")
    data, content_type = (ctx.download or download)(url)
    text = document_text(data, content_type, url, ctx.settings.courses)
    if not text.strip():
        raise ValueError("the timetable has no readable text")
    known = {c.key: c for c in ctx.settings.courses}
    items = validate_items(ctx.ask_json(build_prompt("exam timetable", ctx, text)),
                           ctx.low, ctx.high, known_keys=set(known))
    return [Exam(id=f"final:{i['course_key']}:{i['date']}", kind="final",
                 course_key=i["course_key"], course_name=known[i["course_key"]].name_gr,
                 title=i["title"] or "Final exam", date=i["date"], time=i["time"],
                 source="exam-timetable", url=url, confirmed=True)
            for i in items if i["course_key"] in known]


def download(url: str) -> tuple[bytes, str]:
    """(bytes, content type) of an auth.gr file, capped at MAX_DOWNLOAD_BYTES."""
    with guarded_client(SITE_TIMEOUT_SEC) as client, client.stream("GET", url) as resp:
        resp.raise_for_status()
        if not allowed_host(str(resp.url)):
            raise ValueError("redirected away from auth.gr")
        return (_read_capped(resp, MAX_DOWNLOAD_BYTES, "the timetable file"),
                resp.headers.get("content-type", ""))


def document_text(data: bytes, content_type: str, url: str, courses) -> str:
    """Plain text of a PDF (pages naming a course first) or an HTML page."""
    if b"%PDF" in data[:1024] or "pdf" in content_type.lower() or _is_pdf_url(url):
        return _pdf_text(data, courses)[:MAX_MODEL_TEXT_CHARS]
    if "html" in content_type.lower() or data.lstrip()[:1] == b"<":
        from .moodle import html_to_text
        return html_to_text(data.decode("utf-8", errors="replace"))[:MAX_MODEL_TEXT_CHARS]
    raise ValueError(f"unsupported file type {content_type or '?'}")


def _pdf_text(data: bytes, courses) -> str:
    import pymupdf  # already a requirement for the study guides
    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        pages = [page.get_text() for _, page in zip(range(MAX_PDF_PAGES), pdf)]
    tokens = {w for c in courses for w in normalize(c.name_gr).split()
              if len(w) >= MIN_COURSE_TOKEN}
    relevant = [p for p in pages if any(t in normalize(p) for t in tokens)]
    return "\n\f\n".join(relevant or pages)


# ---- the model prompt and its answer ---------------------------------------------

_FALLBACK_PROMPT = (
    "Extract test/exam dates from this {source_kind} of a Greek university "
    "(course: {course}). Courses: {courses}. Today is {today}; only dates from "
    "{window_start} to {window_end}. Return JSON only: "
    '{"items": [{"course_key": null, "date": "YYYY-MM-DD", "time": "HH:MM or null", '
    '"title": "...", "kind": "test|final"}]}\n\n<document>\n{text}\n</document>\n')


def build_prompt(source_kind: str, ctx: SourceContext, text: str, *, course=None) -> str:
    try:
        template = PROMPT_FILE.read_text(encoding="utf-8")
    except OSError:
        template = _FALLBACK_PROMPT
    courses = "\n".join(f"- {c.key}: {c.name_gr} / {c.name_en}" for c in ctx.settings.courses)
    fields = {"source_kind": source_kind, "today": ctx.now.date().isoformat(),
              "course": f"{course.name_gr} ({course.key})" if course else "(several)",
              "courses": courses, "window_start": ctx.low.isoformat(),
              "window_end": ctx.high.isoformat()}
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out.replace("{text}", text[:MAX_MODEL_TEXT_CHARS])   # last: text is untrusted


def validate_items(raw: Any, low: date, high: date, *, known_keys: set | None) -> list[dict]:
    """Clean model output into [{course_key, date, time, title, kind}] inside [low, high]."""
    if isinstance(raw, dict):
        raw = raw.get("items", raw.get("exams", []))
    out, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        day = clean_date(item.get("date"))
        if not day or not (low <= date.fromisoformat(day) <= high):
            continue
        key = item.get("course_key") if isinstance(item.get("course_key"), str) else None
        if known_keys is not None and key not in known_keys:
            continue
        kind = item.get("kind") if item.get("kind") in ("test", "final") else "test"
        if (key, day, kind) in seen:
            continue
        seen.add((key, day, kind))
        out.append({"course_key": key, "date": day, "time": clean_time(item.get("time")),
                    "title": str(item.get("title") or "").strip()[:120], "kind": kind})
    return out


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


__all__ = ["SourceContext", "calendar_exams", "schedule_exams", "announcement_exams",
           "find_exam_timetable_urls", "find_exam_timetable_url", "finals_from_timetable",
           "is_exam_timetable_link", "document_text", "download", "build_prompt",
           "validate_items", "allowed_host", "EXTRACT_SCHEMA"]
