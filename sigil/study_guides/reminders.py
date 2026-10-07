"""Telegram reminders for tests, finals, exercises, projects and quizzes.

Two halves:

* **`plan()` is pure.** Given the time, the open Moodle deadlines, the known
  exams and the sent-state, it returns the `Reminder`s due now. No IO, so the
  tests cover every rule with plain values.
* **`run_reminders()` does the IO.** It refreshes the caches, plans, sends ONE
  grouped Telegram message and only then records what was sent.

Rules:

* **Leads.** Each kind has lead times in hours (config `study_remind_*_leads`).
  A lead fires when `due - lead <= now < due` and its key has not been sent.
  The key is `"<source id>:<due ts>:<lead>"`, so a moved due date re-arms
  every lead. When several leads have passed at once (Sigil was off), only the
  most urgent is sent and the larger ones are marked sent with it.
* **Submitted work is skipped** (status "submitted" or "submitted?", from the
  cached timeline / an on-demand check). The poll never calls
  `assignments.check_submission` on its own: that WS call may create an empty
  submission record server-side and Moodle stays read-only. A caller may pass
  `check=` explicitly (the tests do); then a failure just sends.
* **Past due is silent**, except ONE "overdue" line for an assignment that is
  still actionable and before its cut-off (at most `OVERDUE_MAX_DAYS` late).
* **New and moved items.** A deadline or exam first seen more than 24h ahead
  gets one "New:" line; one whose due time moved gets one "Moved:" line. The
  very first run (no state file) seeds the seen-set silently: no alert storm.
* **A published exam timetable** is announced once with its link, whether or
  not its dates could be read.
* **Quiet hours** (`study_remind_quiet`, "23:00-08:00"): nothing is sent or
  recorded inside them; everything goes out on the first run after.
* **Telegram's 4096-char limit.** The reminders are packed into as many
  messages as needed (`MAX_MESSAGE_CHARS` each); every message that goes out
  records its own reminders, so one oversized batch never blocks the rest.
* **Back-off.** After a failed send the next attempt waits
  `FAIL_BACKOFF_MIN` minutes, doubling per failure up to `FAIL_BACKOFF_MAX_MIN`
  (state key "failures"), so a wrong bot token is not hammered every run.
* **Marked sent only after Telegram accepts the message.** State file
  `data/study_guides/reminders_sent.json`, pruned of items >14 days past::

      {"version": 1, "sent": {"<key>": sent_ts}, "seen": {"<source id>": due_ts}}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable

from ..fileio import atomic_write_text, file_lock, read_text_locked
from .models import WORK_DIR

log = logging.getLogger(__name__)

STATE_PATH = WORK_DIR / "reminders_sent.json"
STATE_VERSION = 1

DEFAULT_LEADS = {"assign": (168, 72, 24, 3), "quiz": (72, 24, 3),
                 "test": (168, 48, 24), "final": (504, 168, 72, 24)}
_LEAD_KEYS = {"assign": "study_remind_assignment_leads", "quiz": "study_remind_quiz_leads",
              "test": "study_remind_test_leads", "final": "study_remind_final_leads"}
DEFAULT_QUIET = "23:00-08:00"
NEW_MIN_AHEAD_SEC = 24 * 3600        # "New:" only for items more than a day away
MOVED_MIN_SEC = 3600                 # a due time that moved less is not "Moved:"
OVERDUE_MAX_DAYS = 7
PRUNE_DAYS = 14
ACTIVE_BEFORE_DAYS = 14              # reminders start this long before the semester
CHECK_SUBMISSION_MAX_LEAD = 24
MAX_TIMETABLE_KEYS = 50
MAX_MESSAGE_CHARS = 3900             # Telegram rejects sendMessage text over 4096
MAX_LINE_CHARS = 1000
FAIL_BACKOFF_MIN = 30
FAIL_BACKOFF_MAX_MIN = 6 * 60
BACKOFF_SLACK_SEC = 120              # so a 30-minute tick is not missed by seconds

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct",
           "Nov", "Dec")
_DONE = frozenset({"submitted", "submitted?"})


# ---- value types --------------------------------------------------------------

@dataclass(frozen=True)
class Item:
    """One thing that can be reminded about, from a Deadline or an Exam."""
    sid: str                     # "dl:<event id>", "ex:<exam id>", "tt:<url>"
    group: str                   # "exam" | "submission"
    kind: str                    # "assign" | "quiz" | <other module> | "test" | "final" | "timetable"
    course: str
    title: str
    due_ts: int
    url: str = ""
    confirmed: bool = True
    source: str = ""
    date_only: bool = False
    assign_id: int | None = None
    cutoff_ts: int | None = None
    actionable: bool = True
    status: str = "open"


@dataclass(frozen=True)
class Reminder:
    item: Item
    reason: str                  # "lead" | "new" | "moved" | "overdue" | "timetable"
    lead_h: int | None = None
    keys: tuple[str, ...] = ()   # sent-state keys to record once delivered
    note: str = ""               # "" | "new" | "moved" (a lead that is also new/moved)


def item_key(item: Item, suffix: Any) -> str:
    return f"{item.sid}:{item.due_ts}:{suffix}"


# ---- turning deadlines and exams into items ----------------------------------------

def items_from(deadlines: Iterable, exams: Iterable,
               course_names: dict[str, str] | None = None) -> list[Item]:
    """Items for open deadlines (assignments.Deadline) and exams (exams.Exam)."""
    names = course_names or {}
    items = []
    for d in deadlines:
        items.append(Item(
            # Keyed by the timeline event, not the cmid: one activity can have
            # two events (a quiz's open and close), and a shared sid would read
            # as "Moved:" on every run.
            sid=f"dl:{d.event_id}", group="submission", kind=d.kind or "assign",
            course=names.get(d.course_key or "", "") or d.course_short, title=d.title,
            due_ts=int(d.due_ts), url=d.url, assign_id=d.assign_id, cutoff_ts=d.cutoff_ts,
            actionable=d.actionable, status=d.status))
    for e in exams:
        items.append(Item(
            sid=f"ex:{e.id}", group="exam", kind=e.kind,
            course=names.get(e.course_key or "", "") or e.course_name, title=e.title,
            due_ts=e.due_ts(), url=e.url, confirmed=e.confirmed, source=e.source,
            date_only=e.time is None))
    return items


def leads_from_cfg(cfg) -> dict[str, tuple[int, ...]]:
    """Lead hours per kind from config, falling back to DEFAULT_LEADS."""
    out = {}
    for kind, key in _LEAD_KEYS.items():
        raw = cfg.get(key) if cfg is not None else None
        values = tuple(sorted({int(v) for v in raw if isinstance(v, (int, float))
                               and not isinstance(v, bool) and 0 < v <= 24 * 90},
                              reverse=True)) if isinstance(raw, list) else ()
        out[kind] = values or DEFAULT_LEADS[kind]
    return out


def _leads_for(item: Item, leads: dict[str, tuple[int, ...]]) -> tuple[int, ...]:
    if item.group == "exam":
        return leads.get(item.kind, DEFAULT_LEADS["test"])
    return leads.get("quiz") if item.kind == "quiz" else leads.get("assign", DEFAULT_LEADS["assign"])


# ---- planning (pure) ---------------------------------------------------------------

def plan(now: datetime, deadlines: Iterable, exams: Iterable, sent_state: dict | None, *,
         leads: dict[str, tuple[int, ...]] | None = None,
         course_names: dict[str, str] | None = None,
         timetable_urls: Iterable[str] = ()) -> list[Reminder]:
    """The reminders due at `now`, exams first then submissions, each by due time.

    `sent_state` is the state document (None or without "seen" = the first run:
    no "New:"/"Moved:" lines). Deadlines already submitted are skipped.
    """
    state = sent_state or {}
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
    seen = state.get("seen") if isinstance(state.get("seen"), dict) else None
    lead_table = leads or DEFAULT_LEADS
    now_ts = int(now.timestamp())
    out: list[Reminder] = []
    for item in items_from(deadlines, exams, course_names):
        if item.group == "submission" and item.status in _DONE:
            continue
        reminder = _plan_item(item, now_ts, sent, seen, _leads_for(item, lead_table))
        if reminder is not None:
            out.append(reminder)
    for url in timetable_urls:
        key = f"tt:{url}"
        if key not in sent:
            item = Item(sid=key, group="exam", kind="timetable", course="",
                        title="Exam timetable published", due_ts=0, url=url)
            out.append(Reminder(item=item, reason="timetable", keys=(key,)))
    return sorted(out, key=_order)


def _order(r: Reminder) -> tuple:
    return (0 if r.item.group == "exam" else 1, r.item.due_ts, r.item.sid)


def _plan_item(item: Item, now_ts: int, sent: dict, seen: dict | None,
               leads: tuple[int, ...]) -> Reminder | None:
    if item.due_ts <= now_ts:
        return _overdue(item, now_ts, sent)
    note = _note(item, now_ts, seen)
    passed = sorted(lead for lead in leads if item.due_ts - lead * 3600 <= now_ts)
    if passed and item_key(item, passed[0]) not in sent:
        return Reminder(item=item, reason="lead", lead_h=passed[0],
                        keys=tuple(item_key(item, lead) for lead in passed), note=note)
    if note:
        return Reminder(item=item, reason=note)
    return None


def _note(item: Item, now_ts: int, seen: dict | None) -> str:
    if seen is None:
        return ""                                  # first run: seed silently
    before = seen.get(item.sid)
    if before is None:
        return "new" if item.due_ts - now_ts > NEW_MIN_AHEAD_SEC else ""
    if isinstance(before, int) and abs(before - item.due_ts) >= MOVED_MIN_SEC:
        return "moved"
    return ""


def _overdue(item: Item, now_ts: int, sent: dict) -> Reminder | None:
    if item.group != "submission" or item.kind != "assign" or not item.actionable:
        return None
    if item.cutoff_ts is not None and now_ts >= item.cutoff_ts:
        return None
    if now_ts - item.due_ts > OVERDUE_MAX_DAYS * 86400:
        return None
    key = item_key(item, "overdue")
    return None if key in sent else Reminder(item=item, reason="overdue", keys=(key,))


def next_state(state: dict | None, now: datetime, delivered: Iterable[Reminder],
               items: Iterable[Item]) -> dict:
    """The state after `delivered` went out: keys recorded, seen-set updated, pruned."""
    base = state or {}
    now_ts = int(now.timestamp())
    sent = {**(base.get("sent") if isinstance(base.get("sent"), dict) else {}),
            **{k: now_ts for r in delivered for k in r.keys}}
    seen = {**(base.get("seen") if isinstance(base.get("seen"), dict) else {}),
            **{i.sid: i.due_ts for i in items}}
    oldest = now_ts - PRUNE_DAYS * 86400
    return {"version": STATE_VERSION, "sent": _prune_sent(sent, oldest),
            "seen": {k: v for k, v in seen.items() if isinstance(v, int) and v >= oldest}}


def _prune_sent(sent: dict, oldest: int) -> dict:
    kept, timetable = {}, []
    for key, when in sent.items():
        if key.startswith("tt:"):
            timetable.append((key, when))
            continue
        parts = key.rsplit(":", 2)
        due = int(parts[1]) if len(parts) == 3 and parts[1].lstrip("-").isdigit() else None
        if due is not None and due >= oldest:
            kept[key] = when
    timetable.sort(key=lambda kv: -int(kv[1] or 0))
    return {**kept, **dict(timetable[:MAX_TIMETABLE_KEYS])}


# ---- quiet hours -------------------------------------------------------------------

def in_quiet_hours(now: datetime, spec: str = DEFAULT_QUIET) -> bool:
    """Is `now` inside "HH:MM-HH:MM" (may wrap midnight)? A bad spec means never."""
    try:
        start_s, end_s = str(spec).split("-")
        start, end = (_minutes(start_s), _minutes(end_s))
    except ValueError:
        return False
    t = now.hour * 60 + now.minute
    if start == end:
        return False
    return start <= t < end if start < end else (t >= start or t < end)


def _minutes(text: str) -> int:
    hh, mm = text.strip().split(":")
    value = int(hh) * 60 + int(mm)
    if not 0 <= value < 24 * 60:
        raise ValueError(text)
    return value


# ---- the message -------------------------------------------------------------------

def when_phrase(due_ts: int, now: datetime, date_only: bool = False) -> str:
    """"tomorrow 23:59", "in 3 days, Mon 12 Oct 09:00", "was due Mon 12 Oct 23:59"."""
    due = datetime.fromtimestamp(due_ts).astimezone()
    now_local = now.astimezone()
    day = _day_label(due)
    clock = "" if date_only else f" {due:%H:%M}"
    if due_ts <= now.timestamp():
        return f"was due {day}{clock}"
    days = (due.date() - now_local.date()).days
    minutes = int((due_ts - now.timestamp()) // 60)
    if not date_only and minutes < 60:
        return f"in {max(minutes, 1)} min ({due:%H:%M})"
    if days == 0:
        return f"today{clock}"
    if days == 1:
        return f"tomorrow{clock}"
    if days < 14:
        return f"in {days} days, {day}{clock}"
    return f"in {days // 7} weeks, {day}{clock}"


def _day_label(when: datetime) -> str:
    """"Mon 12 Oct", in English whatever the Windows locale is."""
    return f"{_DAYS[when.weekday()]} {when.day} {_MONTHS[when.month - 1]}"


def _line(r: Reminder, now: datetime) -> str:
    item = r.item
    if r.reason == "timetable":
        return f"• Exam timetable published: {_safe_url(item.url) or 'see ece.auth.gr'}"
    note = r.note or (r.reason if r.reason in ("new", "moved") else "")
    prefix = {"new": "New: ", "moved": "Moved: "}.get(note, "")
    if note == "new" and item.source == "announcement":
        prefix = "New test announced: "
    tail = ""
    if item.group == "exam" and item.confirmed:
        text = f"{'FINAL' if item.kind == 'final' else 'TEST'} {item.course} — {item.title}"
    elif item.group == "exam":
        text = f"{item.course} — {item.title}"
        tail = " (possible test - check the course page)"
    else:
        label = "quiz" if item.kind == "quiz" else "submit"
        text = f"{item.course} — {item.title} ({label})"
    when = when_phrase(item.due_ts, now, item.date_only)
    if r.reason == "overdue":
        cutoff = datetime.fromtimestamp(item.cutoff_ts).astimezone() if item.cutoff_ts else None
        until = f", accepted until {_day_label(cutoff)} {cutoff:%H:%M}" if cutoff else ""
        when = f"OVERDUE, {when}{until}"
    line = f"• {prefix}{text}: {when}{tail}"
    url = _safe_url(item.url)
    return f"{line}\n  {url}" if url else line


def _safe_url(url: str) -> str:
    """Only https auth.gr links reach Telegram; Moodle and page text are untrusted."""
    from .exam_sources import allowed_host
    return url if url and allowed_host(url) else ""


def format_message(reminders: Iterable[Reminder], now: datetime) -> str:
    """One Telegram message: Tests & finals first, then submissions."""
    items = list(reminders)
    exams = [r for r in items if r.item.group == "exam"]
    subs = [r for r in items if r.item.group != "exam"]
    parts = ["Study reminders"]
    if exams:
        parts.append("\nTests & finals\n" + "\n".join(_line(r, now) for r in exams))
    if subs:
        parts.append("\nSubmissions\n" + "\n".join(_line(r, now) for r in subs))
    return "\n".join(parts)


def format_messages(reminders: Iterable[Reminder], now: datetime,
                    limit: int = MAX_MESSAGE_CHARS) -> list[tuple[str, list[Reminder]]]:
    """[(text, its reminders)] with each text at most `limit` chars, same grouping."""
    chunks: list[tuple[str, list[Reminder]]] = []
    text, members, group = "", [], None
    for r in sorted(reminders, key=lambda x: 0 if x.item.group == "exam" else 1):
        line = _line(r, now)
        if len(line) > MAX_LINE_CHARS:
            line = line[:MAX_LINE_CHARS - 1] + "…"
        head = "Tests & finals" if r.item.group == "exam" else "Submissions"
        piece = ("\n" if head == group else f"\n\n{head}\n") + line
        if members and len(text) + len(piece) > limit:
            chunks.append((text, members))
            text, members = "", []
            piece = f"\n\n{head}\n{line}"
        if not members:
            text = "Study reminders (continued)" if chunks else "Study reminders"
        text += piece
        members.append(r)
        group = head
    if members:
        chunks.append((text, members))
    return chunks


# ---- state file --------------------------------------------------------------------

def load_state(path: Path = STATE_PATH) -> dict | None:
    """The sent-state, or None when there is no file yet (the first run)."""
    try:
        raw = json.loads(read_text_locked(path))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        log.warning("study reminders: %s unreadable (%s); starting over", Path(path).name, exc)
        return {"sent": {}, "seen": {}}
    return raw if isinstance(raw, dict) else {"sent": {}, "seen": {}}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    with file_lock(path):
        atomic_write_text(path, json.dumps(state, indent=2, ensure_ascii=False))


# ---- the runner ---------------------------------------------------------------------

def active_window(cfg, settings) -> tuple[date | None, date]:
    from .exams import exam_period
    _, period_end = exam_period(cfg)
    start = settings.semester_start
    return (start - timedelta(days=ACTIVE_BEFORE_DAYS) if start else None,
            period_end + timedelta(days=1))


def _default_send(cfg, text: str) -> bool:
    from ..notify import send_telegram
    return send_telegram(cfg, text)


def run_reminders(cfg, *, now: datetime | None = None,
                  send: Callable[[Any, str], bool] | None = None, refresh: bool = True,
                  state_path: Path = STATE_PATH, exams_path: Path | None = None,
                  deadlines_path: Path | None = None,
                  check: Callable[[Any, int], str] | None = None) -> str:
    """Refresh, plan, send one message, record it. Returns a short status string.

    `send(cfg, text) -> bool` defaults to notify.send_telegram. `check(cfg,
    assign_id) -> status` is off unless passed (the poll never calls
    assignments.check_submission itself, see the module docstring). Never raises.
    """
    try:
        return _run(cfg, (now or datetime.now()).astimezone(), send or _default_send,
                    refresh, Path(state_path), exams_path, deadlines_path, check)
    except Exception:  # noqa: BLE001 — a scheduled job must return a status
        log.exception("study reminders: run failed unexpectedly")
        return "failed: internal error"


def _run(cfg, now, send, refresh, state_path, exams_path, deadlines_path, check) -> str:
    from . import assignments, exams as exams_mod
    from .settings import load_settings

    if not cfg.get("study_remind_enabled", True):
        return "disabled"
    settings = load_settings(cfg)
    start, end = active_window(cfg, settings)
    if (start and now.date() < start) or now.date() > end:
        return "skipped: outside the semester"
    if in_quiet_hours(now, cfg.get("study_remind_quiet", DEFAULT_QUIET) or ""):
        return "quiet hours"
    ex_path = Path(exams_path) if exams_path else exams_mod.EXAMS_PATH
    dl_path = Path(deadlines_path) if deadlines_path else assignments.CACHE_PATH
    if refresh:
        assignments.refresh(cfg, now=now, path=dl_path)
        exams_mod.refresh_exams(cfg, now=now, path=ex_path)
    deadlines = assignments.cached(dl_path).items
    exams = exams_mod.load_exams(ex_path)
    names = {c.key: c.name_gr for c in settings.courses}
    state = load_state(state_path)
    planned = plan(now, deadlines, exams, state, leads=leads_from_cfg(cfg), course_names=names,
                   timetable_urls=list(exams_mod.timetable_links(ex_path)))
    items = items_from(deadlines, exams, names)
    if planned and backing_off(state, now):
        return "backing off after a failed send"
    to_send, silent = _drop_submitted(cfg, planned, check)
    if not to_send:
        save_state(next_state(state, now, silent, items), state_path)
        return "seeded" if state is None else "nothing due"
    delivered: list[Reminder] = []
    for text, members in format_messages(to_send, now):
        if not send(cfg, text):
            break
        delivered.extend(members)
    if not delivered:
        log.warning("study reminders: Telegram did not take the message; backing off")
        save_state(with_failure(state, now), state_path)
        return "failed: telegram"
    # A "New:"/"Moved:" line that did not go out must stay unseen for next time.
    unsent = {r.item.sid for r in to_send if r not in delivered}
    items = [i for i in items if i.sid not in unsent]
    after = next_state(state, now, [*delivered, *silent], items)
    log.info("study reminders: sent %d of %d reminder(s)", len(delivered), len(to_send))
    if len(delivered) < len(to_send):
        save_state(with_failure(after, now), state_path)
        return f"partly sent {len(delivered)}/{len(to_send)}"
    save_state(after, state_path)
    return f"sent {len(to_send)}"


def backing_off(state: dict | None, now: datetime) -> bool:
    """Is the last failed send too recent to try again (doubling back-off)?"""
    fail = (state or {}).get("failures")
    if not isinstance(fail, dict):
        return False
    try:
        count, last = int(fail.get("count") or 0), int(fail.get("last_ts") or 0)
    except (TypeError, ValueError):
        return False
    if count <= 0:
        return False
    wait_min = min(FAIL_BACKOFF_MIN * 2 ** min(count - 1, 10), FAIL_BACKOFF_MAX_MIN)
    return now.timestamp() - last < wait_min * 60 - BACKOFF_SLACK_SEC


def with_failure(state: dict | None, now: datetime) -> dict:
    """`state` plus one more failed send. "seen" stays as it was (or absent)."""
    base = dict(state or {})
    fail = base.get("failures") if isinstance(base.get("failures"), dict) else {}
    try:
        count = int(fail.get("count") or 0)
    except (TypeError, ValueError):
        count = 0
    base["failures"] = {"count": count + 1, "last_ts": int(now.timestamp())}
    return base


def _drop_submitted(cfg, planned: list[Reminder], check: Callable[[Any, int], str] | None
                    ) -> tuple[list[Reminder], list[Reminder]]:
    """(to send, submitted-so-silent). Without `check`, nothing asks Moodle."""
    if check is None:
        return list(planned), []
    keep, silent = [], []
    for r in planned:
        item = r.item
        needs_check = (r.reason == "lead" and item.kind == "assign" and item.assign_id
                       and (r.lead_h or 0) <= CHECK_SUBMISSION_MAX_LEAD)
        status = ""
        if needs_check:
            try:
                status = check(cfg, int(item.assign_id))
            except Exception as exc:  # noqa: BLE001 — a failed check just sends
                log.info("study reminders: submission check failed (%s)", exc)
        if status == "submitted":
            silent.append(replace(r, item=replace(item, status=status)))
        else:
            keep.append(r)
    return keep, silent


__all__ = ["Item", "Reminder", "plan", "next_state", "items_from", "leads_from_cfg",
           "in_quiet_hours", "when_phrase", "format_message", "format_messages",
           "backing_off", "with_failure", "load_state", "save_state",
           "run_reminders", "active_window", "STATE_PATH", "DEFAULT_LEADS"]
