"""The nightly study-guide pipeline: today's classes in, filed PDFs out.

One call to `run` walks spec §3 for every course the student had on a day:
timetable -> Moodle contents -> parsed schedule -> today's topic -> material
-> prompts -> Claude (guide, then a verify pass) -> xelatex with a repair loop
-> the course folder in the vault -> runs.json. Every stage lives in its own
module; this one only decides the order, what counts as "skip" versus "fail",
and what is written where.

Design rules:

* **One course never blocks another.** Each course runs inside its own
  try/except and ends as exactly one `CourseResult` — ok, skipped or failed
  with a reason. The only thing allowed to stop the whole loop early is a
  rejected Moodle token, because every later course would fail the same way;
  those still get a failed result each, so the summary names them all.
* **Cheap checks before expensive ones.** Idempotency, a missing Moodle id, a
  missing xelatex and an unusable vault are all caught before the Opus call,
  so a misconfiguration costs nothing from the weekly budget.
* **A dry run is read-only where it matters.** It fetches from Moodle, parses
  the schedule (which may call the helper model — that is "everything up to
  the prompt", spec §6) and prints the assembled prompts. It never calls
  guide generation, never touches the vault and never records state.
* **The vault is only written through `filing.file_pdf`**, and only when
  `allow_vault_writes` is on. With writes off the PDF stays in the build
  directory and the result says where.
* **Two runs never overlap.** The whole run holds `state.run_lock`; a second
  run raises `LockBusy` to its caller instead of queueing, because the
  scheduled catch-up and a manual `run` racing each other would each build
  the same guide.
* **No secrets in logs.** The Moodle token is loaded and handed to the client;
  it is never formatted into a message here.
"""
from __future__ import annotations

import difflib
import functools
import json
import logging
import shutil
import unicodedata
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..fileio import atomic_write_text
from . import build as build_mod
from . import generate as generate_mod
from . import goodnotes
from .filing import file_pdf, safe_filename
from .material import MAX_PICKED_FILES, build_material
from .models import (BUILD_DIR, COURSE_IDS_PATH, FAILED, FILE_CACHE_PATH, LAB,
                     LECTURE, OK, SCHEDULE_CACHE_PATH, SKIPPED, TUTORIAL,
                     WORK_DIR, Course, CourseResult, Material, ScheduleEntry,
                     Session)
from .moodle import InvalidToken, MoodleClient, MoodleError, iter_files, load_token
from .prompt import build_system, build_user
from .schedule_parser import (ScheduleError, entry_for, entry_on_day, infer_next,
                              parse_schedule)
from .settings import Settings, course_by_key, load_settings, save_course_ids
from .state import JsonCache, RunState, run_lock
from .syllabus import strand_for, strand_topic, syllabus_topic
from .timetable import (has_class, lecture_number, no_class_reason, session_decision,
                        todays_sessions)

log = logging.getLogger(__name__)

# Spec §4.6: at most two repair turns after a failed xelatex build, then the
# course is marked failed.
MAX_REPAIRS = 2
# Review rounds before a guide the reviewer keeps correcting is held back.
MAX_REVIEW_ROUNDS = 3

# How many earlier topics ride in the prompt's "previous lectures" list. The
# prompt uses them only to link today to what came before; a whole semester of
# titles is noise by week ten.
MAX_PREVIOUS_TOPICS = 12

# When a course has more than one session on a day (a lecture and a tutorial),
# it still gets ONE guide (spec §4.5); the session that decides its type is
# picked in this order.
_SESSION_PRIORITY = {LECTURE: 0, TUTORIAL: 1, LAB: 2}

# Timetable weekday codes by date.weekday(), for a session synthesised when
# --course names a course the timetable has no slot for.
_WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")

# `discover`: the lowest name similarity (0..1) at which a Moodle course is
# proposed as the match for a configured course. Below it the course is left
# unmapped, to be filled in by hand.
MATCH_THRESHOLD = 0.55

# Raw `core_course_get_contents` dumps written by `discover`, for review and as
# recorded fixtures for the tests (they hold no token: Moodle file URLs get
# the token appended only at download time).
DISCOVER_DUMP_DIR = WORK_DIR / "discover"

# The prefix of a reason that means the schedule had nothing for today.
SCHEDULE_MISSING = "schedule_missing"

GenerateFn = Callable[..., Any]


@dataclass(frozen=True)
class _Ctx:
    """Everything one run shares across its courses."""
    settings: Settings
    day_str: str
    dry_run: bool
    force: bool
    client: Any
    ask_json: Callable[[str, dict, str], Any]
    generate_fn: GenerateFn
    verify_fn: Callable[..., str]
    review_fn: Callable[..., tuple[bool, str]]
    repair_fn: Callable[..., str]
    build_fn: Callable[..., tuple]
    state: RunState
    schedule_cache: JsonCache
    file_cache: JsonCache
    out: Callable[[str], None]


@dataclass(frozen=True)
class _Plan:
    """What a course will get a guide about, once the schedule is read."""
    session: Session
    topic: str
    topic_inferred: bool
    entry: ScheduleEntry | None
    previous_topics: tuple[str, ...]
    counted: bool = False   # topic is "Διάλεξη N" from the timetable, not the page


class _Skip(Exception):
    """A course that ends as SKIPPED with this reason (not an error)."""


class _Fail(Exception):
    """A course that ends as FAILED with this reason, no traceback needed."""


class _GenerationFailed(Exception):
    """A failure after the guide model was called: wraps the original error so
    the result is flagged `generation_attempted` (catchup.py caps retries)."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


class _AlreadyDone(_Skip):
    """runs.json already has a success for this (date, course); not re-recorded."""

    def __init__(self) -> None:
        super().__init__("already built for this date (use --force to rebuild)")


# --------------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------------

def run(cfg, day: date | None = None, course: str | None = None,
        dry_run: bool = False, force: bool = False, *,
        client=None, ask_json=None, generate_fn=None,
        verify_fn=None, review_fn=None, repair_fn=None, build_fn=None,
        out: Callable[[str], None] = print) -> list[CourseResult]:
    """Build (or, with `dry_run`, only assemble) the guides for `day`'s classes.

    `course` limits the run to one course key; when the timetable has no slot
    for it that day, the course is treated as having had a lecture, so a
    backfill works before the timetable is known. `force` ignores runs.json's
    record of an earlier success. `client`, `ask_json`, `generate_fn`,
    `verify_fn`, `review_fn`, `repair_fn` and `build_fn` are injectable for tests; each
    defaults to the real Moodle client / claude CLI / xelatex.

    Raises `LockBusy` when another run holds the lock.
    """
    settings = load_settings(cfg)
    day = day or date.today()
    day_str = day.isoformat()
    sessions = _sessions_for(settings, day, course, out)
    if not sessions:
        return []
    _warn_if_unanchored(settings, out)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    with run_lock():
        own_client = client is None
        ctx = _make_ctx(settings, day_str, dry_run, force, client, ask_json,
                        generate_fn, verify_fn, review_fn, repair_fn, build_fn, out)
        ctx = _auto_discover(ctx)
        try:
            return _run_courses(ctx, _group_by_course(sessions))
        finally:
            if own_client:
                _close(ctx.client)


def has_class_today(cfg, day: date) -> bool:
    """True when the timetable has a session on `day` inside the semester.

    A settings failure answers False (and is logged): the nightly gate skipping
    one evening is better than it crashing the scheduled job.
    """
    try:
        return has_class(load_settings(cfg), day)
    except Exception:  # noqa: BLE001
        log.exception("study guides: could not decide whether %s had classes", day)
        return False


def summary_text(results: list[CourseResult], day_str: str) -> str:
    """One line per course (spec §4.8), ready for the log, Telegram or a print."""
    if not results:
        return f"Study guides {day_str}: no classes, nothing to do."
    counts = {s: sum(1 for r in results if r.status == s) for s in (OK, SKIPPED, FAILED)}
    head = (f"Study guides {day_str}: {counts[OK]} ok, {counts[SKIPPED]} skipped, "
            f"{counts[FAILED]} failed")
    return "\n".join([head] + [_summary_line(r) for r in results])


def discover(cfg, *, client=None, ask_json=None, confirm=input,
             out: Callable[[str], None] = print) -> dict:
    """List enrolled courses, map them to course keys, show their schedules.

    Returns the proposed {course_key: moodle_course_id} mapping; it is written
    to course_ids.json only after `confirm` gets a yes. Nothing on Moodle is
    changed and config.json is never written.
    """
    settings = load_settings(cfg)
    _warn_if_unanchored(settings, out)
    own_client = client is None
    client = client if client is not None else _client_or_none(settings, out)
    if client is None:
        return {}
    try:
        return _discover_with(settings, client, ask_json or _default_ask_json(settings),
                              confirm, out)
    finally:
        if own_client:
            _close(client)


def _warn_if_unanchored(settings: Settings, out: Callable[[str], None]) -> None:
    """Say so loudly when a semester date is unset: the schedule parser then
    has no anchor for «Εβδομάδα 3» or «Τρίτη 7/10», and misread dates are no
    longer rejected as outside the semester."""
    missing = [name for name, value in (("study_guides_semester_start", settings.semester_start),
                                        ("study_guides_semester_end", settings.semester_end))
               if value is None]
    if not missing:
        return
    msg = (f"WARNING: {' and '.join(missing)} not set in config.json — schedule dates "
           f"cannot be anchored to the semester and out-of-semester dates are not rejected.")
    log.warning("study guides: %s", msg)
    out(msg)


def _discover_with(settings: Settings, client, ask_json, confirm,
                   out: Callable[[str], None]) -> dict:
    enrolled = _enrolled_courses(client, out)
    mapping = _match_courses(settings.courses, enrolled, out)
    cache = JsonCache(SCHEDULE_CACHE_PATH)
    for course in settings.courses:
        if course.key in mapping:
            _discover_one(course, mapping[course.key], client, settings, ask_json, cache, out)
    if not mapping:
        out("No course matched; nothing to save. Edit course_ids.json by hand if needed.")
        return {}
    answer = confirm(f"\nSave these {len(mapping)} course ids to {COURSE_IDS_PATH}? [y/N] ")
    if str(answer).strip().lower() in ("y", "yes", "ν", "ναι"):
        save_course_ids(mapping)
        out(f"Saved {len(mapping)} course ids.")
    else:
        out("Not saved.")
    return mapping


# --------------------------------------------------------------------------
# Run: setup
# --------------------------------------------------------------------------

def _sessions_for(settings: Settings, day: date, course: str | None,
                  out: Callable[[str], None]) -> list[Session]:
    """Today's sessions, narrowed to `course` when one is named."""
    sessions = todays_sessions(settings, day)
    if course is None:
        if not sessions:
            _say_no_classes(settings, day, out)
        return sessions
    mine = [s for s in sessions if s.course_key == course]
    if mine or course_by_key(settings, course) is None:
        # An unknown key still gets one failed result, from _process_course.
        return mine or [Session(course, _WEEKDAYS[day.weekday()], "", "", LECTURE)]
    out(f"{course}: no timetable slot on {day.isoformat()}; treating it as a lecture "
        f"because --course named it.")
    log.info("study guides: %s has no slot on %s; assuming a lecture (explicit course)",
             course, day)
    return [Session(course, _WEEKDAYS[day.weekday()], "", "", LECTURE)]


def _say_no_classes(settings: Settings, day: date, out: Callable[[str], None]) -> None:
    msg = no_class_reason(settings, day) or "no classes"
    if not settings.timetable:
        msg += " — fill it in config.json, or name a course with --course"
    out(f"Study guides {day.isoformat()}: {msg}.")
    log.info("study guides: %s — %s", day, msg)


def _make_ctx(settings: Settings, day_str: str, dry_run: bool, force: bool,
              client, ask_json, generate_fn, verify_fn, review_fn, repair_fn,
              build_fn, out: Callable[[str], None]) -> _Ctx:
    return _Ctx(
        settings=settings, day_str=day_str, dry_run=dry_run, force=force,
        client=client if client is not None else _client_or_none(settings, out),
        ask_json=ask_json or _default_ask_json(settings),
        generate_fn=generate_fn or generate_mod.generate_guide,
        verify_fn=verify_fn or generate_mod.verify_guide,
        review_fn=review_fn or generate_mod.review_guide,
        repair_fn=repair_fn or generate_mod.repair_guide,
        build_fn=build_fn or build_mod.build_with_repair,
        state=RunState(), schedule_cache=JsonCache(SCHEDULE_CACHE_PATH),
        file_cache=JsonCache(FILE_CACHE_PATH), out=out)


def _client_or_none(settings: Settings, out: Callable[[str], None]) -> MoodleClient | None:
    token = load_token()
    if not token:
        msg = "no Moodle token saved — run: python -m sigil.study_guides login"
        out(msg)
        log.warning("study guides: %s", msg)
        return None
    return MoodleClient(settings.moodle_url, token)


def _close(client) -> None:
    """Close a client this module opened; a failed close is only worth a log line."""
    if client is None or not hasattr(client, "close"):
        return
    try:
        client.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("study guides: closing the Moodle client failed: %s", exc)


def _default_ask_json(settings: Settings) -> Callable[[str, dict, str], Any]:
    """The helper-model JSON call, bound to this feature's cwd and CLI path."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return functools.partial(generate_mod.ask_json, cwd=WORK_DIR,
                             claude_cmd=settings.claude_cmd)


def _group_by_course(sessions: list[Session]) -> dict[str, list[Session]]:
    grouped: dict[str, list[Session]] = {}
    for s in sessions:
        grouped.setdefault(s.course_key, []).append(s)
    return grouped


# --------------------------------------------------------------------------
# Run: the per-course loop
# --------------------------------------------------------------------------

def _run_courses(ctx: _Ctx, grouped: dict[str, list[Session]]) -> list[CourseResult]:
    results: list[CourseResult] = []
    token_dead = ""
    for key, sessions in grouped.items():
        if token_dead:
            result = _result(ctx, key, FAILED, token_dead)
        else:
            result, token_dead = _guarded(ctx, key, sessions)
        _finish(ctx, result)
        results.append(result)
    ctx.out(summary_text(results, ctx.day_str))
    return results


def _guarded(ctx: _Ctx, key: str, sessions: list[Session]) -> tuple[CourseResult, str]:
    """Run one course; turn every way it can end into a result.

    Returns (result, token_dead_reason); the second is non-empty only when
    Moodle rejected the token, which ends Moodle work for the whole run.
    """
    try:
        return _process_course(ctx, key, sessions), ""
    except _AlreadyDone as exc:
        return _result(ctx, key, SKIPPED, str(exc), extra={"already_done": True}), ""
    except _GenerationFailed as wrapped:
        cause = wrapped.cause
        if isinstance(cause, _Fail):
            reason = str(cause)
        else:
            log.error("study guides: %s %s crashed", ctx.day_str, key, exc_info=cause)
            reason = f"{type(cause).__name__}: {cause}"
        return _result(ctx, key, FAILED, reason,
                       extra={"generation_attempted": True}), ""
    except _Skip as exc:
        return _result(ctx, key, SKIPPED, str(exc)), ""
    except _Fail as exc:
        return _result(ctx, key, FAILED, str(exc)), ""
    except InvalidToken:
        reason = ("Moodle rejected the saved token — run: "
                  "python -m sigil.study_guides login")
        return _result(ctx, key, FAILED, reason), reason
    except MoodleError as exc:
        log.warning("study guides: %s %s Moodle error: %s", ctx.day_str, key, exc)
        return _result(ctx, key, FAILED, f"Moodle: {exc}"), ""
    except Exception as exc:  # noqa: BLE001 — one course must not stop the rest
        log.exception("study guides: %s %s crashed", ctx.day_str, key)
        return _result(ctx, key, FAILED, f"{type(exc).__name__}: {exc}"), ""


def _result(ctx: _Ctx, key: str, status: str, reason: str, **kw) -> CourseResult:
    return CourseResult(date=ctx.day_str, course_key=key, status=status,
                        reason=reason, **kw)


def _finish(ctx: _Ctx, result: CourseResult) -> None:
    """Log the one-line outcome; record it unless this was a dry run."""
    log.info("study guides: %s %s — %s: %s", result.date, result.course_key,
             result.status, result.reason or result.topic)
    if ctx.dry_run or result.extra.get("already_done"):
        return
    try:
        if result.status != OK and _same_as_last(ctx, result):
            return      # the hourly check retrying a cheap failure: nothing new
        ctx.state.record(result)
    except Exception:  # noqa: BLE001 — a lost record must not lose the summary
        log.exception("study guides: could not record %s %s in runs.json",
                      result.date, result.course_key)


def _same_as_last(ctx: _Ctx, result: CourseResult) -> bool:
    """True when (date, course)'s latest record already says exactly this."""
    last = next((r for r in ctx.state.for_date(result.date)
                 if r.get("course_key") == result.course_key), None)
    return bool(last and last.get("status") == result.status
                and last.get("reason") == result.reason
                and not result.extra.get("generation_attempted"))


def _process_course(ctx: _Ctx, key: str, sessions: list[Session]) -> CourseResult:
    course = _precheck(ctx, key, sessions)
    contents = ctx.client.course_contents(course.moodle_course_id)
    plan = _plan(ctx, course, sessions, contents)
    material = _material(ctx, course, plan, contents)
    if plan.counted:
        _check_counted_material(ctx, course, plan, material)
    # GoodNotes pages, after the Moodle checks: notes from the last class
    # never stand in for today's slides; they wait for a guide that is built.
    notes = goodnotes.collect(ctx.settings.goodnotes_dir,
                              dict(ctx.settings.goodnotes_folders), course, ctx.day_str)
    material = goodnotes.attach(material, notes)
    system = build_system(course)
    user = build_user(course, ctx.day_str, plan.topic, plan.topic_inferred,
                      plan.session.type, list(plan.previous_topics), material)
    if ctx.dry_run:
        _print_dry_run(ctx, course, plan, material, system, user)
        return _result(ctx, key, SKIPPED, "dry run — nothing generated",
                       topic=plan.topic, topic_inferred=plan.topic_inferred,
                       files_used=material.files_used)
    try:
        result = _generate_and_file(ctx, course, plan, material, system, user)
    except Exception as exc:  # noqa: BLE001 — re-raised, flagged as costly
        raise _GenerationFailed(exc) from exc
    goodnotes.mark_used(notes)
    return result


def _precheck(ctx: _Ctx, key: str, sessions: list[Session]) -> Course:
    """Everything that can end a course before any Moodle or Claude call."""
    course = course_by_key(ctx.settings, key)
    if course is None:
        raise _Fail(f"unknown course key {key!r}")
    if not ctx.dry_run and not ctx.force and ctx.state.already_succeeded(ctx.day_str, key):
        raise _AlreadyDone()
    if not any(session_decision(s, ctx.settings, True)[0] for s in sessions):
        reasons = "; ".join(session_decision(s, ctx.settings, True)[1] for s in sessions)
        raise _Skip(reasons or "no session to build a guide for")
    if not course.moodle_course_id:
        tag = semester_tag(ctx.settings)
        raise _Fail(f"no {tag or 'current'} Moodle page for this course yet "
                    "(not enrolled?) — it is picked up automatically once it appears")
    if ctx.client is None:
        raise _Fail("no Moodle client (no saved token — run login)")
    if not ctx.dry_run:
        _check_outputs(ctx)
    return course


def _check_outputs(ctx: _Ctx) -> None:
    """Fail before the expensive call when the guide could not be built or filed."""
    xelatex = ctx.settings.xelatex
    real_build = ctx.build_fn is build_mod.build_with_repair
    if real_build and not (xelatex and (Path(xelatex).is_file() or shutil.which(xelatex))):
        raise _Fail("xelatex not found — set study_guides_xelatex in config.json")
    root = ctx.settings.vault_root
    if ctx.settings.allow_vault_writes and not (root.is_absolute() and root.is_dir()):
        raise _Fail(f"vault folder {root} does not exist — check vault_path")


# --------------------------------------------------------------------------
# Run: topic and material
# --------------------------------------------------------------------------

def _plan(ctx: _Ctx, course: Course, sessions: list[Session], contents) -> _Plan:
    """Pick the session, today's schedule entry and the topic (spec §4.3 step 5)."""
    entries = _schedule(ctx, course, contents)
    session, entry, why = _choose_session(ctx.settings, sessions, entries, ctx.day_str)
    if session is None:
        raise _Skip(why)
    inferred = counted = False
    if entry is None:
        entry, counted = _infer(ctx, course, entries, session.type)
        inferred = True
    previous = _previous_topics(ctx, course.key, entries, entry.topic)
    return _Plan(session=session, topic=entry.topic, topic_inferred=inferred,
                 entry=entry, previous_topics=previous, counted=counted)


def _schedule(ctx: _Ctx, course: Course, contents) -> list[ScheduleEntry]:
    try:
        return parse_schedule(course, contents, ctx.settings, ctx.ask_json,
                              ctx.schedule_cache)
    except ScheduleError as exc:
        log.warning("study guides: %s — no usable schedule on the course page: %s",
                    course.key, exc)
        return []
    except generate_mod.ClaudeCLIError as exc:
        # A flaky helper call is as recoverable as a page without a schedule:
        # fall back to inference / the session rules instead of failing the course.
        log.warning("study guides: %s — the helper model could not parse the schedule "
                    "this run: %s", course.key, exc)
        return []


def _choose_session(settings: Settings, sessions: list[Session],
                    entries: list[ScheduleEntry], day_str: str
                    ) -> tuple[Session | None, ScheduleEntry | None, str]:
    """The session that should get a guide, and its entry.

    A session with a real schedule entry for today beats one whose topic would
    be inferred, whatever the priority order says (spec §4.3.5: today's topic
    is the entry dated today). So: (1) sessions with their own entry, by
    priority; (2) a lecture/lab with any same-day entry, even one the model
    typed for another session; (3) the plain rules, which may infer.
    """
    ordered = sorted(sessions, key=lambda s: _SESSION_PRIORITY.get(s.type, 9))
    for s in ordered:
        entry = entry_for(entries, day_str, s.type)
        if entry is not None and session_decision(s, settings, True)[0]:
            return s, entry, session_decision(s, settings, True)[1]
    same_day = entry_on_day(entries, day_str)
    if same_day is not None:
        for s in ordered:
            if s.type in (LECTURE, LAB) and session_decision(s, settings, True)[0]:
                why = f"today's schedule entry (typed {same_day.session_type!r})"
                return s, same_day, why
    reasons: list[str] = []
    for s in ordered:
        generate, why = session_decision(s, settings, False)
        if generate:
            return s, None, why
        reasons.append(why)
    return None, None, "; ".join(reasons) or "no session to build a guide for"


def _infer(ctx: _Ctx, course: Course, entries: list[ScheduleEntry],
           session_type: str) -> tuple[ScheduleEntry, bool]:
    """Spec §4.3.5: no entry for today — use the next unmatched topic, flagged.

    (entry, counted). A lecture never takes a lab's topic: when the page lists
    no lecture to infer (no schedule, or only the labs), the topic is
    "Διάλεξη N" counted from the timetable, and `counted` is True.
    """
    log.warning("study guides: %s %s — %s: the class is not in the course schedule",
                ctx.day_str, course.key, SCHEDULE_MISSING)
    if not ctx.settings.infer_topic:
        raise _Skip(f"{SCHEDULE_MISSING} (study_guides_infer_topic is off)")
    done = ctx.state.previous_topics(course.key, ctx.day_str)
    entry = infer_next(entries, ctx.day_str, done, session_type)
    if session_type == LECTURE and (entry is None
                                    or entry.session_type not in (None, LECTURE)):
        numbered = _numbered_lecture(ctx, course)
        if numbered is not None:
            return numbered, True
    if entry is None:
        raise _Fail(f"{SCHEDULE_MISSING}: no schedule entry for today and no "
                    f"unmatched topic left to infer")
    return entry, False


def _numbered_lecture(ctx: _Ctx, course: Course) -> ScheduleEntry | None:
    """ "Διάλεξη N — <syllabus topic>": N counted from the timetable, the topic
    from the official course outline (syllabus.py) at the same point of the
    semester. Without an outline for the course, just "Διάλεξη N"."""
    day = date.fromisoformat(ctx.day_str)
    weekday = _WEEKDAYS[day.weekday()]
    strand = strand_for(course.key, weekday, ctx.settings.strands)
    days = frozenset({weekday}) if strand else None   # a strand counts its own day
    n = lecture_number(ctx.settings, course.key, day, days)
    if n <= 0:
        return None
    end = ctx.settings.semester_end
    total = lecture_number(ctx.settings, course.key, end, days) if end else 0
    files: tuple[str, ...] = ()
    if strand:
        outline, files = strand_topic(strand, n, total)
        label = f"Διάλεξη {n} ({strand.label})"
    else:
        outline, label = syllabus_topic(course.key, n, total), f"Διάλεξη {n}"
    topic = f"{label} — {outline}" if outline else label
    log.info("study guides: %s %s — no lecture in the course schedule; lecture %d "
             "of %d by the timetable: %r", ctx.day_str, course.key, n, total, topic)
    return ScheduleEntry(date=ctx.day_str, session_type=LECTURE, topic=topic,
                         filenames=files)


def _check_counted_material(ctx: _Ctx, course: Course, plan: _Plan,
                            material: Material) -> None:
    """A counted lecture needs files, and new ones: "Διάλεξη 4" with nothing
    behind it would be a guide made up from the title, and files an earlier
    guide was built from mean the new slides are not on Moodle yet. More files
    than a pick may hold means nothing matched the number and the helper fell
    back to the whole page — a guide on that would be on the wrong chapter."""
    if not material.files_used:
        raise _Skip(f"{plan.topic}: no lecture material on Moodle to build it from")
    if len(material.files_used) > MAX_PICKED_FILES:
        raise _Skip(f"{plan.topic}: could not tell which of the course's files it "
                    f"is ({len(material.files_used)} matched) — needs a schedule "
                    "on the course page")
    used = ctx.state.files_used_before(course.key, ctx.day_str)
    outline = _outline(plan.topic)
    done = {_outline(t) for t in ctx.state.previous_topics(course.key, ctx.day_str)}
    # One PDF often spans several syllabus topics: same files are fine for a
    # new topic, not for a repeat of one (or for a bare "Διάλεξη N").
    if set(material.files_used) <= used and (not outline or outline in done):
        raise _Skip(f"{plan.topic}: no new lecture material on Moodle since the "
                    "last guide")


def _outline(topic: str) -> str:
    """The syllabus part of a counted topic ("Διάλεξη 3 — X" -> "X"), else ""."""
    _, sep, rest = topic.partition(" — ")
    return rest.strip() if sep else ""


def _previous_topics(ctx: _Ctx, course_key: str, entries: list[ScheduleEntry],
                     today_topic: str) -> tuple[str, ...]:
    """Earlier topics from the schedule, then from runs.json; deduped, recent last."""
    earlier = [e.topic for e in entries if e.date < ctx.day_str]
    earlier += ctx.state.previous_topics(course_key, ctx.day_str)
    seen: set[str] = {today_topic}
    ordered: list[str] = []
    for topic in earlier:
        if topic not in seen:
            seen.add(topic)
            ordered.append(topic)
    return tuple(ordered[-MAX_PREVIOUS_TOPICS:])


def _material(ctx: _Ctx, course: Course, plan: _Plan, contents) -> Material:
    """Today's material. `build_material` gets EVERY file of the course: it runs
    `select_files` itself (so the helper model is asked once, not twice) and
    needs the full list to find the past-exam papers. It logs its own notes."""
    material = build_material(course, plan.entry, plan.topic, iter_files(contents),
                              ctx.client, ctx.file_cache, ask_json=ctx.ask_json,
                              model=ctx.settings.helper_model)
    if not material.files_used:
        log.warning("study guides: %s %s — no material file for %r",
                    ctx.day_str, course.key, plan.topic)
    return material


def _print_dry_run(ctx: _Ctx, course: Course, plan: _Plan, material: Material,
                   system: str, user: str) -> None:
    s = plan.session
    when = f" {s.start}-{s.end}" if s.start else ""
    lines = [
        f"\n=== {course.name_gr} ({course.key}) — {ctx.day_str} ===",
        f"Session: {s.type}{when}",
        f"Topic: {plan.topic}"
        + (f"  [INFERRED — {SCHEDULE_MISSING}]" if plan.topic_inferred else ""),
        "Selected files:",
        *([f"  - {name}" for name in material.files_used] or ["  (none)"]),
        *[f"  attachment (scanned): {p.name}" for p in material.attachments],
        f"Past-exam text: {len(material.past_exams)} chars",
        *[f"  note: {n}" for n in material.notes],
        "--- system prompt ---", system,
        "--- user prompt ---", user,
        "--- end ---",
    ]
    ctx.out("\n".join(lines))


# --------------------------------------------------------------------------
# Run: generate, verify, build, file
# --------------------------------------------------------------------------

def _generate_and_file(ctx: _Ctx, course: Course, plan: _Plan, material: Material,
                       system: str, user: str) -> CourseResult:
    workdir = BUILD_DIR / f"{ctx.day_str}_{course.key}"
    workdir.mkdir(parents=True, exist_ok=True)
    _stage_attachments(material, workdir)
    cli = ctx.generate_fn(system, user, attachments_dir=workdir, settings=ctx.settings)
    latex = generate_mod.strip_to_document(cli.text)
    latex, verify_note = _verify(ctx, course, latex, workdir)
    built, latex = ctx.build_fn(
        latex, workdir, ctx.settings.xelatex,
        lambda tex, tail: ctx.repair_fn(tex, tail, workdir=workdir, settings=ctx.settings),
        max_repairs=MAX_REPAIRS)
    extra = {"workdir": str(workdir), "session_type": plan.session.type}
    if verify_note:
        extra["verify"] = verify_note
    _require_built(built, workdir)
    built, review_note = _review_gate(ctx, course, latex, built, workdir)
    if review_note:
        extra["review"] = review_note
    pdf, reason = _file(ctx, course, plan, Path(built.pdf_path))
    return _result(ctx, course.key, OK, reason, topic=plan.topic,
                   topic_inferred=plan.topic_inferred,
                   files_used=material.files_used, pdf_path=str(pdf),
                   cost_usd=cli.cost_usd, extra=extra)


def _require_built(built, workdir: Path) -> None:
    if not built.ok or built.pdf_path is None:
        last = _last_line(built.log_tail)
        raise _Fail(f"LaTeX build failed (up to {MAX_REPAIRS} repairs tried): {last} "
                    f"(see {workdir})")


def _review_gate(ctx: _Ctx, course: Course, latex: str, built,
                 workdir: Path) -> tuple[Any, str]:
    """Nothing is filed until the review model approves the compiled guide.

    Each round the reviewer either approves or returns a corrected document,
    which is rebuilt and reviewed again. A guide still unapproved after
    MAX_REVIEW_ROUNDS, or a review that cannot run, FAILS the course: the PDF
    stays in the build directory and never reaches the vault. Fail closed - an
    unreviewed guide is exactly what this gate exists to keep off the shelf.
    """
    if not ctx.settings.review_pass:
        return built, "off"
    for round_no in range(1, MAX_REVIEW_ROUNDS + 1):
        try:
            approved, revised = ctx.review_fn(latex, pdf_path=built.pdf_path,
                                              workdir=workdir, settings=ctx.settings)
        except Exception as exc:  # noqa: BLE001 - any review failure holds the guide back
            log.warning("study guides: %s %s review could not run: %s",
                        ctx.day_str, course.key, exc)
            raise _Fail(f"review could not run, guide held back: {exc} (see {workdir})")
        if approved:
            return built, f"approved in round {round_no}"
        log.info("study guides: %s %s review round %d corrected the guide",
                 ctx.day_str, course.key, round_no)
        built, latex = ctx.build_fn(
            revised, workdir, ctx.settings.xelatex,
            lambda tex, tail: ctx.repair_fn(tex, tail, workdir=workdir,
                                            settings=ctx.settings),
            max_repairs=MAX_REPAIRS)
        _require_built(built, workdir)
    raise _Fail(f"review did not approve the guide after {MAX_REVIEW_ROUNDS} rounds, "
                f"guide held back (see {workdir})")


def _stage_attachments(material: Material, workdir: Path) -> None:
    """Copy scanned PDFs next to the prompt: the Read tool resolves from cwd."""
    for src in material.attachments:
        dest = workdir / src.name
        if src.resolve() == dest.resolve():
            continue
        try:
            shutil.copy2(src, dest)
        except OSError as exc:
            log.warning("study guides: could not stage attachment %s: %s", src.name, exc)


def _verify(ctx: _Ctx, course: Course, latex: str, workdir: Path) -> tuple[str, str]:
    """Spec §4.5 verification pass. A failed check keeps the unverified guide."""
    if not ctx.settings.verify_pass:
        return latex, ""
    try:
        return ctx.verify_fn(latex, workdir=workdir, settings=ctx.settings), "done"
    except Exception as exc:  # noqa: BLE001 — an unverified guide beats none
        log.warning("study guides: %s %s verify pass failed, keeping the draft: %s",
                    ctx.day_str, course.key, exc)
        return latex, f"failed: {exc}"


def _file(ctx: _Ctx, course: Course, plan: _Plan, pdf: Path) -> tuple[Path, str]:
    """Copy the PDF into the course folder, or leave it put when writes are off."""
    if not ctx.settings.allow_vault_writes:
        return pdf, f"vault writes are off — PDF left at {pdf}"
    name = safe_filename(ctx.day_str, course.vault_folder, plan.topic)
    dest = file_pdf(pdf, ctx.settings.vault_root, course.vault_folder, name)
    note = "topic inferred (schedule_missing)" if plan.topic_inferred else ""
    return dest, note


def _last_line(log_tail: str) -> str:
    """The most telling line of a LaTeX log tail: the first `!` error, else the end."""
    lines = [ln.strip() for ln in (log_tail or "").splitlines() if ln.strip()]
    for ln in lines:
        if ln.startswith("!"):
            return ln[:200]
    return lines[-1][:200] if lines else "no log"


def _summary_line(r: CourseResult) -> str:
    if r.status == OK:
        inferred = " (inferred topic)" if r.topic_inferred else ""
        where = Path(r.pdf_path).name if r.pdf_path else ""
        tail = f" — {r.reason}" if r.reason else ""
        return f"- {r.course_key}: ok — {r.topic}{inferred} -> {where}{tail}"
    return f"- {r.course_key}: {r.status} — {r.reason}"


# --------------------------------------------------------------------------
# Discover
# --------------------------------------------------------------------------

def semester_tag(settings: Settings) -> str:
    """ΑΠΘ's course-name suffix for this semester: "2026/1" for the winter
    semester starting 2026-10, "2025/2" for a spring one starting 2026-02
    (the academic year is named after the September it starts in)."""
    start = settings.semester_start
    if start is None:
        return ""
    if start.month >= 8:
        return f"{start.year}/1"
    return f"{start.year - 1}/2"


def _auto_discover(ctx: _Ctx) -> _Ctx:
    """Fill missing Moodle course ids from this semester's enrolled courses.

    Only courses whose name carries this semester's tag ("... - 2026/1") are
    candidates, so an old enrolment with the same title can never be picked.
    Found ids are saved to course_ids.json (a dry run only reports them). The
    3rd-semester pages did not exist yet when the feature was built
    (2026-09-25), so this is what turns the job on once the student is enrolled.
    Never raises: a failure leaves the course to fail with its own reason.
    """
    found = discover_ids(ctx.settings, ctx.client, ctx.out, save=not ctx.dry_run)
    if not found:
        return ctx
    courses = tuple(replace(c, moodle_course_id=found.get(c.key, c.moodle_course_id))
                    for c in ctx.settings.courses)
    return replace(ctx, settings=replace(ctx.settings, courses=courses))


def discover_ids(settings: Settings, client, out: Callable[[str], None] = log.info,
                 *, save: bool = True) -> dict[str, int]:
    """Moodle ids for the courses that still have none, {key: id}, best effort.

    Shared by the nightly run and the reminders' exam refresh, so the ids are
    picked up the day of enrolment even on a day with no class. Never raises.
    """
    missing = [c for c in settings.courses if not c.moodle_course_id]
    tag = semester_tag(settings)
    if not missing or client is None or not tag:
        return {}
    try:
        info = client.site_info()
        enrolled = [m for m in client.user_courses(info["userid"])
                    if tag in f"{m.get('fullname', '')} {m.get('shortname', '')}"]
    except Exception as exc:  # noqa: BLE001 — discovery is best effort
        log.warning("study guides: auto-discover could not list courses: %s", exc)
        return {}
    if not enrolled:
        log.info("study guides: no enrolled course tagged %s yet", tag)
        return {}
    found = _match_courses(tuple(missing), enrolled, log.info)
    for key, mid in found.items():
        out(f"Found the Moodle page for {key}: course id {mid}")
    if found and save:
        save_course_ids(found)
    return found


def _enrolled_courses(client, out: Callable[[str], None]) -> list[dict]:
    info = client.site_info()
    courses = client.user_courses(info["userid"])
    out(f"Connected to {info.get('sitename', 'Moodle')}. Enrolled courses:")
    for c in courses:
        out(f"  {c.get('id'):>6}  {c.get('fullname', '')}  [{c.get('shortname', '')}]")
    return courses


def _normalize(text: str) -> str:
    """Accent-free casefolded words, with Greek look-alike numerals made Latin."""
    stripped = "".join(ch for ch in unicodedata.normalize("NFD", text)
                       if not unicodedata.combining(ch)).casefold()
    words = [_latin_numeral(w) for w in stripped.replace("-", " ").split()]
    return " ".join(words)


def _latin_numeral(word: str) -> str:
    """"ιι" (Greek iota) -> "ii", so «ΙΙ» typed either way compares equal."""
    if word and set(word) <= set("ιiνv"):
        return word.translate(str.maketrans("ιν", "iv"))
    return word


_NUMERALS = {"i", "ii", "iii", "iv", "v", "vi", "1", "2", "3", "4"}


def _numeral(words: str) -> str:
    found = [w for w in words.split() if w in _NUMERALS]
    return found[-1] if found else ""


def _similarity(course: Course, moodle: dict) -> float:
    """0..1 name match. A different course numeral (Circuits I vs II) is 0."""
    target = _normalize(f"{moodle.get('fullname', '')} {moodle.get('shortname', '')}")
    best = 0.0
    for name in (course.name_gr, course.name_en):
        mine = _normalize(name)
        num = _numeral(mine)
        if num and _numeral(_normalize(moodle.get("fullname", ""))) not in ("", num):
            continue
        best = max(best, difflib.SequenceMatcher(None, mine, target).ratio(),
                   _containment(mine, target))
    return best


def _containment(mine: str, target: str) -> float:
    """Share of the configured name's words present in the Moodle name."""
    words = mine.split()
    if not words:
        return 0.0
    hits = sum(1 for w in words if w in target.split())
    return hits / len(words)


def _match_courses(courses: tuple[Course, ...], enrolled: list[dict],
                   out: Callable[[str], None]) -> dict[str, int]:
    """Greedy best-first assignment, each Moodle course used at most once."""
    scored = sorted(((_similarity(c, m), c.key, int(m.get("id", 0)), m.get("fullname", ""))
                     for c in courses for m in enrolled), reverse=True)
    mapping: dict[str, int] = {}
    used: set[int] = set()
    out("\nProposed mapping:")
    for score, key, mid, fullname in scored:
        if score < MATCH_THRESHOLD or key in mapping or mid in used or not mid:
            continue
        mapping[key] = mid
        used.add(mid)
        out(f"  {key:<12} -> {mid:>6}  {fullname}  (match {score:.2f})")
    for c in courses:
        if c.key not in mapping:
            out(f"  {c.key:<12} -> no match (current id {c.moodle_course_id or 'none'})")
    return mapping


def _discover_one(course: Course, course_id: int, client, settings: Settings,
                  ask_json, cache: JsonCache, out: Callable[[str], None]) -> None:
    """Dump one course's sections, files and parsed schedule; never raises."""
    out(f"\n=== {course.name_gr} ({course.key}, id {course_id}) ===")
    try:
        contents = client.course_contents(course_id)
        _dump_contents(course.key, course_id, contents)
        _print_sections(contents, out)
        entries = parse_schedule(_with_id(course, course_id), contents, settings,
                                 ask_json, cache)
        out(f"Parsed schedule ({len(entries)} entries):")
        for e in entries:
            out(f"  {e.date}  {e.session_type or '-':<3}  {e.topic}")
    except ScheduleError as exc:
        out(f"No schedule parsed: {exc}")
    except Exception as exc:  # noqa: BLE001 — one course must not end discovery
        log.exception("study guides: discover failed for %s", course.key)
        out(f"Failed: {type(exc).__name__}: {exc}")


def _with_id(course: Course, course_id: int) -> Course:
    return replace(course, moodle_course_id=course_id)


def _print_sections(contents, out: Callable[[str], None]) -> None:
    files = iter_files(contents)
    for section in contents or []:
        out(f"  [section {section.get('id')}] {section.get('name', '')} "
            f"({len(section.get('modules') or [])} modules)")
        for f in (f for f in files if f.section_id == section.get("id")):
            out(f"      - {f.filename}  (module {f.module_id})")


def _dump_contents(key: str, course_id: int, contents) -> None:
    """Keep the raw contents for review and as test fixtures (no token inside)."""
    try:
        DISCOVER_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(DISCOVER_DUMP_DIR / f"{key}_{course_id}.json",
                          json.dumps(contents, ensure_ascii=False, indent=1))
    except (OSError, TypeError, ValueError) as exc:
        log.warning("study guides: could not save the %s contents dump: %s", key, exc)
