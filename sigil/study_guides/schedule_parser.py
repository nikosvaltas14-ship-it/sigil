"""Today's topic, read from the schedule the professor posted on the course page.

Many professors upload the whole semester's material in the first week, so
"the newest file" says nothing about what was taught today. What does say it is
the date-to-topic schedule on the course page — in section names, in section
summaries, in a label or page module, occasionally only as a PDF whose name we
can at least point at. That schedule is the source of truth (spec §4.3).

Design rules:

* **The model reads the schedule; this module checks it.** Greek dates
  («Τρίτη 7/10»), week numbers («Εβδομάδα 3») and "next Thursday" are resolved
  by one cheap helper-model call anchored on the semester start. Everything it
  returns is re-validated here, entry by entry: a bad date, a date outside the
  semester or an unknown session type drops *that entry* (logged), not the
  schedule. Only a schedule with nothing valid left is an error.
* **Parse once per change, not once per night.** The collected course text is
  hashed together with the semester dates and the prompt it is read against;
  while the hash matches the cache, the cached schedule (or the cached "this
  page has no schedule") is reused and no model call is made. A professor
  editing the page, or a change of the semester dates, changes the hash,
  which is exactly when a re-parse is wanted.
* **Stable text in, stable hash out.** `collect_schedule_text` walks sections
  and modules in the order Moodle returns them and never includes volatile
  fields (timestamps, view counts, completion state), so an unchanged page
  hashes the same every night.
* **The model is injected.** `parse_schedule` takes `ask_json` as a parameter,
  so the tests can drive it with a canned answer and never touch the CLI.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .material import is_schedule_name
from .models import LAB, LECTURE, SESSION_TYPES, TUTORIAL, Course, ScheduleEntry
from .moodle import html_to_text

if TYPE_CHECKING:
    from .settings import Settings
    from .state import JsonCache

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).with_name("prompts")
SCHEDULE_PROMPT_FILE = PROMPTS_DIR / "schedule_extract.txt"

# Entries a day or a week either side of the semester are real (a make-up
# lecture on 30 Sep, an extra tutorial in the exam run-up); a date months away
# is the model misreading "3/10" as March.
SEMESTER_SLACK = timedelta(days=7)

# A course page with more text than this is almost certainly carrying a pasted
# textbook chapter, not a schedule. Cap what goes to the helper model so one
# odd page cannot turn a cheap extraction call into an expensive one.
MAX_SCHEDULE_TEXT_CHARS = 60_000

# Module types whose description is prose a professor writes a schedule into.
_TEXT_MODULES = ("label", "page")

_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FENCE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)

# What the model may write for a session type, mapped to ours. Latin "A" is
# here on purpose: it looks identical to Greek "Α" and models swap them.
_SESSION_ALIASES = {
    "θ": LECTURE, "lecture": LECTURE, "theory": LECTURE, "θεωρια": LECTURE,
    "θεωρία": LECTURE, "διάλεξη": LECTURE, "διαλεξη": LECTURE,
    "α": TUTORIAL, "a": TUTORIAL, "tutorial": TUTORIAL, "φροντιστήριο": TUTORIAL,
    "φροντιστηριο": TUTORIAL, "ασκήσεις": TUTORIAL, "ασκησεις": TUTORIAL,
    "lab": LAB, "laboratory": LAB, "spice": LAB, "εργαστήριο": LAB,
    "εργαστηριο": LAB,
}
_NULL_TYPES = ("", "null", "none", "unknown", "-")

# The JSON shape asked of the helper model. An object at the root because the
# CLI's structured-output mode wants one; `validate_schedule` also accepts a
# bare list for a model that answers in plain fenced JSON instead.
SCHEDULE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date": {"type": "string"},
                    "session_type": {"type": ["string", "null"]},
                    "topic": {"type": "string"},
                    "section_ids": {"type": "array", "items": {"type": "integer"}},
                    "module_ids": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["date", "topic"],
            },
        },
    },
    "required": ["entries"],
}

# Used only when prompts/schedule_extract.txt is missing, so a half-installed
# package still parses rather than failing every course.
_FALLBACK_PROMPT = """\
Below is the text of a university course page on Moodle (course: {course_name}).
It contains the professor's schedule: which topic is taught on which date.
Semester: {semester_start} to {semester_end}. Resolve Greek dates such as
«Τρίτη 7/10», week numbers such as «Εβδομάδα 3» (week 1 starts on the semester
start date) and relative references, and write every date as YYYY-MM-DD.
session_type is "Θ" for a lecture, "Α" for a tutorial, "lab" for a lab or Spice
session, or null when the page does not say. section_ids and module_ids are the
[section N] and [module N] ids of the parts of the page that belong to that
date's topic. If the page has no schedule at all, return an empty list.
Return JSON only: {{"entries": [{{"date": "YYYY-MM-DD", "session_type": "Θ|Α|lab|null", "topic": "...", "section_ids": [], "module_ids": []}}]}}

<course_page>
{text}
</course_page>
"""


class ScheduleError(ValueError):
    """No usable schedule: no text on the page, unparseable JSON, or no valid entry."""


# --------------------------------------------------------------------------
# Collecting the page text

def collect_schedule_text(contents: list[dict] | None) -> str:
    """Every schedule-bearing part of a `core_course_get_contents` result, as text.

    Sections in Moodle order, each with its name and summary; inside a section,
    label/page modules with their description, every other module by name (so
    the model can link a date to the file module holding its slides), and files
    whose name looks like a schedule. Ids are written inline as
    `[section N]` / `[module N] (modname)` — the form prompts/schedule_extract.txt
    tells the model to read — so it can hand them back.
    """
    blocks: list[str] = []
    for section in contents or []:
        if not isinstance(section, dict):
            log.warning("course contents: skipping a non-dict section (%s)",
                        type(section).__name__)
            continue
        block = _section_text(section)
        if block:
            blocks.append(block)
    return "\n\n".join(blocks).strip()


def _section_text(section: dict) -> str:
    lines: list[str] = []
    name = _clean(section.get("name"))
    header = f"## [section {_as_int(section.get('id'))}] {name}".rstrip()
    summary = _html(section.get("summary"))
    if summary:
        lines.append(summary)
    for module in section.get("modules") or []:
        if isinstance(module, dict):
            lines.extend(_module_lines(module))
    if not name and not lines:
        return ""
    return "\n".join([header, *lines])


def _module_lines(module: dict) -> list[str]:
    """One line for the module itself, then any schedule-looking file names."""
    modname = _clean(module.get("modname")) or "module"
    ident = f"[module {_as_int(module.get('id'))}] ({modname})"
    name = _clean(module.get("name"))
    description = _html(module.get("description"))
    lines: list[str] = []
    if modname in _TEXT_MODULES or description:
        body = " — ".join(p for p in (name, description) if p)
        lines.append(f"{ident} {body}".rstrip())
    elif name:
        lines.append(f"{ident} {name}")
    for item in module.get("contents") or []:
        if not isinstance(item, dict):
            continue
        filename = _clean(item.get("filename"))
        if filename and is_schedule_name(filename):
            lines.append(f"{ident} αρχείο προγράμματος: {filename}")
    return lines


def _html(value: Any) -> str:
    if not value or not isinstance(value, str):
        return ""
    return html_to_text(value).strip()


def _clean(value: Any) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def text_hash(text: str) -> str:
    """sha256 hex of the collected text: the schedule cache's change detector."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Validation

def validate_schedule(raw: Any, semester_start: date | None,
                      semester_end: date | None) -> list[ScheduleEntry]:
    """The model's answer as clean `ScheduleEntry`s, sorted into schedule order.

    `raw` may be the JSON text (optionally inside ```json fences, optionally
    with prose around it), an already-decoded list, or an object carrying the
    list under "entries"/"schedule". Bad entries are dropped and logged; if
    none survive, `ScheduleError`.
    """
    items = _decode(raw)
    low = semester_start - SEMESTER_SLACK if semester_start else None
    high = semester_end + SEMESTER_SLACK if semester_end else None
    kept: list[tuple[str, int, ScheduleEntry]] = []
    seen: set[tuple] = set()
    for index, item in enumerate(items):
        entry, problem = _validate_entry(item, low, high)
        if entry is None:
            log.warning("schedule entry %d dropped: %s", index, problem)
            continue
        identity = (entry.date, entry.session_type, entry.topic)
        if identity in seen:
            log.info("schedule entry %d dropped: duplicate of an earlier entry", index)
            continue
        seen.add(identity)
        kept.append((entry.date, index, entry))
    if not kept:
        raise ScheduleError(f"no valid schedule entries (of {len(items)} returned)")
    kept.sort(key=lambda t: (t[0], t[1]))
    return [entry for _, _, entry in kept]


def _decode(raw: Any) -> list:
    if isinstance(raw, str):
        raw = _loads_lenient(raw)
    if isinstance(raw, dict):
        for key in ("entries", "schedule"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
        else:
            raise ScheduleError("schedule JSON is an object without an 'entries' list")
    if not isinstance(raw, list):
        raise ScheduleError(f"schedule JSON is a {type(raw).__name__}, not a list")
    return raw


def _loads_lenient(text: str) -> Any:
    """JSON from a model reply: fenced or bare, with or without prose around it."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        sliced = _outer_json(candidate)
        if sliced is not None:
            try:
                return json.loads(sliced)
            except json.JSONDecodeError:
                continue
    raise ScheduleError("schedule reply contains no parseable JSON")


def _outer_json(text: str) -> str | None:
    """From the first [ or { to the last matching ] or }, or None."""
    starts = [i for i in (text.find("["), text.find("{")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end = text.rfind("]" if text[start] == "[" else "}")
    return text[start:end + 1] if end > start else None


def _validate_entry(item: Any, low: date | None,
                    high: date | None) -> tuple[ScheduleEntry | None, str]:
    if not isinstance(item, dict):
        return None, f"not an object ({type(item).__name__})"
    day, problem = _check_date(item.get("date"), low, high)
    if day is None:
        return None, problem
    session_type, ok = _session_type(item.get("session_type"))
    if not ok:
        return None, f"unknown session_type {item.get('session_type')!r}"
    topic = _clean(item.get("topic")) if isinstance(item.get("topic"), str) else ""
    if not topic:
        return None, f"empty topic on {day}"
    return ScheduleEntry(
        date=day, session_type=session_type, topic=topic,
        section_ids=_int_tuple(item.get("section_ids")),
        module_ids=_int_tuple(item.get("module_ids")),
    ), ""


def _check_date(value: Any, low: date | None, high: date | None) -> tuple[str | None, str]:
    if not isinstance(value, str) or not _ISO_DATE.match(value.strip()):
        return None, f"date {value!r} is not YYYY-MM-DD"
    text = value.strip()
    try:
        day = date.fromisoformat(text)
    except ValueError:
        return None, f"date {text} does not exist"
    if (low and day < low) or (high and day > high):
        return None, f"date {text} is outside the semester"
    return text, ""


def _session_type(value: Any) -> tuple[str | None, bool]:
    """(normalised type, recognised?). None/"null" is a valid "unspecified"."""
    if value is None:
        return None, True
    if not isinstance(value, str):
        return None, False
    text = value.strip()
    if text.lower() in _NULL_TYPES:
        return None, True
    if text in SESSION_TYPES:
        return text, True
    mapped = _SESSION_ALIASES.get(text.lower())
    return (mapped, True) if mapped else (None, False)


def _int_tuple(value: Any) -> tuple[int, ...]:
    """Ids as ints; a non-list or a non-numeric id is ignored, not fatal."""
    if not isinstance(value, list):
        return ()
    out: list[int] = []
    for item in value:
        if isinstance(item, bool):
            continue
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            log.debug("schedule: ignoring non-numeric id %r", item)
    return tuple(dict.fromkeys(out))


# --------------------------------------------------------------------------
# Parsing (with cache)

AskJson = Callable[[str, dict, str], Any]


def parse_schedule(course: Course, contents: list[dict], settings: "Settings",
                   ask_json: AskJson, cache: "JsonCache") -> list[ScheduleEntry]:
    """The course's schedule, from the cache while the page is unchanged.

    The cache holds one record per course key — `{"hash": ..., "entries": [...]}`
    — so an edited page replaces its old record instead of piling up beside it.
    Raises `ScheduleError` when the page has no text or no valid schedule.
    """
    text = collect_schedule_text(contents)
    if not text:
        raise ScheduleError(f"{course.key}: the course page has no text to read a schedule from")
    prompt = _build_prompt(course, text, settings)
    digest = _cache_digest(text, prompt, settings)
    cached = _cached_entries(course, digest, settings, cache)
    if cached is not None:
        return cached
    if ask_json is None:
        raise ScheduleError(f"{course.key}: schedule changed and no model is available to parse it")
    log.info("%s: schedule text changed (hash %s), asking %s to parse it",
             course.key, digest[:12], settings.helper_model)
    # A reply that is not JSON at all is a model hiccup: not cached, so the next
    # run asks again. A readable reply with nothing valid in it means the page
    # has no schedule: cached as such, so an unchanged page costs no more calls.
    items = _decode(ask_json(prompt, SCHEDULE_SCHEMA, settings.helper_model))
    try:
        entries = validate_schedule(items, settings.semester_start, settings.semester_end)
    except ScheduleError as exc:
        cache.put(course.key, {"hash": digest, "entries": [], "error": str(exc)})
        raise
    cache.put(course.key, {"hash": digest, "entries": [asdict(e) for e in entries]})
    log.info("%s: parsed %d schedule entries", course.key, len(entries))
    return entries


def _cache_digest(text: str, prompt: str, settings: "Settings") -> str:
    """The cache key: the page text plus everything the answer was resolved
    against. Week numbers and year-less dates are read relative to the semester
    dates in the prompt, so configuring those dates (or editing the prompt)
    must re-parse even though the page itself is unchanged."""
    anchor = "|".join((
        settings.semester_start.isoformat() if settings.semester_start else "",
        settings.semester_end.isoformat() if settings.semester_end else "",
        text_hash(prompt),
    ))
    return text_hash(f"{text}\n\x00{anchor}")


def _cached_entries(course: Course, digest: str, settings: "Settings",
                    cache: "JsonCache") -> list[ScheduleEntry] | None:
    """Cached entries for this digest; None to re-parse. A cached "no schedule"
    (empty entries) raises `ScheduleError` without a model call."""
    record = cache.get(course.key)
    if not isinstance(record, dict) or record.get("hash") != digest:
        return None
    if record.get("entries") == []:
        raise ScheduleError(f"{course.key}: no schedule on the unchanged course page "
                            f"(cached: {record.get('error') or 'nothing valid'})")
    try:
        # Re-validated rather than trusted: the semester dates in config may
        # have changed since this was cached.
        return validate_schedule(record.get("entries"), settings.semester_start,
                                 settings.semester_end)
    except ScheduleError as exc:
        log.warning("%s: cached schedule no longer valid (%s); re-parsing", course.key, exc)
        return None


def _build_prompt(course: Course, text: str, settings: "Settings") -> str:
    if len(text) > MAX_SCHEDULE_TEXT_CHARS:
        log.warning("%s: course page text is %d chars; sending the first %d",
                    course.key, len(text), MAX_SCHEDULE_TEXT_CHARS)
        text = text[:MAX_SCHEDULE_TEXT_CHARS]
    template = _load_template()
    fields = {
        "course_name": course.name_gr,
        "course_name_en": course.name_en,
        "semester_start": settings.semester_start.isoformat() if settings.semester_start else "άγνωστη",
        "semester_end": settings.semester_end.isoformat() if settings.semester_end else "άγνωστη",
        "text": text,
    }
    return _fill(template, fields)


def _load_template() -> str:
    try:
        return SCHEDULE_PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("schedule prompt %s unreadable (%s); using the built-in one",
                    SCHEDULE_PROMPT_FILE.name, exc)
        return _FALLBACK_PROMPT


def _fill(template: str, fields: dict[str, str]) -> str:
    """Substitute `{name}` placeholders by plain replacement, not str.format:
    the template carries literal JSON braces that format() would choke on. The
    page text is appended when the template has no slot for it. A template
    written for str.format (doubled `{{ }}` around literal JSON) is unescaped
    first, before any page text goes in, so the page is never altered."""
    if "{{" in template or "}}" in template:
        template = template.replace("{{", "{").replace("}}", "}")
    has_text_slot = "{text}" in template or "{course_text}" in template
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    out = out.replace("{course_text}", fields["text"])
    if not has_text_slot:
        out = f"{out.rstrip()}\n\n<course_page>\n{fields['text']}\n</course_page>\n"
    return out


# --------------------------------------------------------------------------
# Lookups

def entry_for(entries: Iterable[ScheduleEntry], day_str: str,
              session_type: str | None) -> ScheduleEntry | None:
    """Today's entry: one of exactly this session type, else an untyped one.

    With nothing dated today, a week-level entry stands in: the extraction
    prompt dates «Εβδομάδα 3» to the first class day it can infer (usually the
    Monday), so a Wednesday class finds its topic on the week's single date,
    as long as that date is not after today. A week whose entries sit on
    several days is dated per class, so a missing day there stays missing.
    """
    entries = list(entries)
    exact = _typed_or_untyped([e for e in entries if e.date == day_str], session_type)
    if exact is not None:
        return exact
    return _typed_or_untyped(_week_level(entries, day_str), session_type)


def entry_on_day(entries: Iterable[ScheduleEntry], day_str: str) -> ScheduleEntry | None:
    """Any entry dated today, whatever its session type (first in schedule order).

    For a lecture or lab whose own type has no entry: a same-day entry the
    model labelled «Α» or lab is still what the schedule says today is about,
    and beats an inferred topic.
    """
    return next((e for e in entries if e.date == day_str), None)


def _typed_or_untyped(candidates: list[ScheduleEntry],
                      session_type: str | None) -> ScheduleEntry | None:
    for entry in candidates:
        if session_type is not None and entry.session_type == session_type:
            return entry
    for entry in candidates:
        if entry.session_type is None:
            return entry
    return None


def _week_level(entries: list[ScheduleEntry], day_str: str) -> list[ScheduleEntry]:
    """The entries of `day_str`'s ISO week when they are dated week-level:
    all on one date, on or before `day_str`. Otherwise []."""
    day = _as_date(day_str)
    if day is None:
        return []
    week = day.isocalendar()[:2]
    same_week = [e for e in entries
                 if (d := _as_date(e.date)) is not None and d.isocalendar()[:2] == week]
    dates = {e.date for e in same_week}
    if len(dates) != 1 or next(iter(dates)) > day_str:
        return []
    return same_week


def _as_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def infer_next(entries: Iterable[ScheduleEntry], day_str: str,
               done_topics: list[str], session_type: str | None = None
               ) -> ScheduleEntry | None:
    """The next topic to be taught, for a class the schedule lists nothing for.

    Used when the course had a class but the schedule has no entry for the day
    (spec §4.3 `schedule_missing`); the caller flags the topic as inferred.
    "Next" is anchored on the date, not only on runs.json: topics dated before
    today were taught whether or not a guide was built for them (the feature
    switched on mid-semester, a failed night, a skipped tutorial). So:

    1. the earliest entry dated today or later whose topic has no guide yet;
    2. else (the schedule is used up) the most recent undone past entry.

    Within each step an entry of `session_type` (or untyped) is preferred over
    one typed for another kind of session.
    """
    entries = list(entries)
    done = {_topic_key(t) for t in done_topics if t}
    undone = [e for e in entries if _topic_key(e.topic) not in done]
    upcoming = [e for e in undone if e.date >= day_str]
    past = [e for e in reversed(undone) if e.date < day_str]
    for pool, where in ((upcoming, "next"), (past, "latest past")):
        entry = _prefer_type(pool, session_type)
        if entry is not None:
            log.info("schedule_missing on %s: inferred %s topic %r (scheduled %s)",
                     day_str, where, entry.topic, entry.date)
            return entry
    return None


def _prefer_type(pool: list[ScheduleEntry],
                 session_type: str | None) -> ScheduleEntry | None:
    """The first entry of `session_type` or untyped, else simply the first."""
    if not pool:
        return None
    if session_type is not None:
        for entry in pool:
            if entry.session_type in (session_type, None):
                return entry
    return pool[0]


def _topic_key(topic: str) -> str:
    return " ".join(topic.casefold().split())
