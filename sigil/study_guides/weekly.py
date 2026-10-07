"""The Saturday university review: one Greek PDF summarising the week.

Flow (`run_weekly`): take the weekly lock, `gather` what the week left behind
(timetable, study guides, Moodle deadlines, University vault changes, Claude
session minutes, the University part of the Study log), have Claude write a
XeLaTeX document from those facts alone, build it with the guides' own
build-and-repair loop, file it under `<University>/Weekly Reviews/`, and
record the outcome in weekly.json.

Design rules:

* **Facts in, never invention.** The model gets a JSON block of facts and is
  told to use nothing else. The timetable says what was *scheduled*, not what
  was attended, and the facts are labelled that way.
* **An empty week costs nothing.** No guides, no deadlines, no University
  vault changes, no Claude minutes and no University Study-log paragraph ->
  `skipped` with "nothing to summarise", before any CLI call. The caller turns
  that into a one-line Telegram.
* **Only University work.** "02 Areas/Study log.md" is a general log; only
  paragraphs that name a configured course or the University survive. Vault changes are limited to the University folder.
* **Its own state and lock.** Outcomes go to weekly.json (keyed by the week's
  Monday), not runs.json, so no runs.json reader needs a skip rule; the lock is
  weekly.lock, so a Saturday review never blocks a deferred Friday guide run
  (the lock also covers a hand-started CLI run).
* **Read-only git.** The vault-changes query goes through `_git_log`, which
  runs `git log` and nothing else.
* **The document data is untrusted.** Excerpts and the Study log were written
  by models and by hand; the CLI gets no tools, and the system prompt says the
  facts are data, not instructions.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import unicodedata
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from operator import itemgetter
from pathlib import Path, PurePosixPath
from typing import Callable

from .. import session_logs
from ..config import child_env
from ..fileio import read_text_locked
from .filing import file_pdf, safe_filename
from .models import BUILD_DIR, FAILED, OK, SKIPPED, WORK_DIR
from .settings import Settings, course_by_key
from .state import JsonCache, LockBusy, RunState, run_lock
from .studymap import DONE_STATUSES, INTERNAL_KEY_PREFIX, course_name, snapshot_items
from .timetable import no_class_reason, todays_sessions

log = logging.getLogger(__name__)

# ---- paths and names ----------------------------------------------------------

WEEKLY_DIR = WORK_DIR / "weekly"             # build dirs: weekly/<monday>/guide.tex
WEEKLY_STATE = WORK_DIR / "weekly.json"      # {"<monday iso>": {...outcome...}}
WEEKLY_LOCK = WORK_DIR / "weekly.lock"
WEEKLY_FOLDER = "Weekly Reviews"             # under settings.vault_root
HUB_NOTE_NAME = "Weekly Reviews.md"          # the folder's hub note (written by app via tools)
FILENAME_LABEL = "Weekly Review"
STUDY_LOG_NOTE = "02 Areas/Study log.md"

# ---- limits -------------------------------------------------------------------

DEFAULT_WEEKLY_MODEL = "opus"                # config study_weekly_model
EXCERPT_MAX_CHARS = 1500                     # per guide «Τι μάθαμε σήμερα» excerpt
STUDY_LOG_MAX_CHARS = 4000
MAX_VAULT_CHANGES = 80                       # lines of "added: X" in the facts
GIT_TIMEOUT_SEC = 60
NEXT_WEEK_DAYS = 7                           # "next week" = Sunday..next Saturday
MAX_REPAIRS = 2
SATURDAY_OFFSET = 5                          # Monday + 5 = Saturday
ALLOWED_GIT = frozenset({"log"})
TEX_NAME = "guide.tex"                       # build.compile_tex always writes this name

# ---- recognising text ---------------------------------------------------------

_DATE_HEADING_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\b.*$", re.M)
_EXCERPT_HEADING_RE = re.compile(r"τι\s+μ[αά]θαμε\s+σ[ηή]μερα", re.I)
_EXCERPT_STOP_RE = re.compile(
    r"\\(?:section|subsection|chapter|part)\*?\s*[\[{]|\\(?:begin|end)\{tcolorbox\}"
    r"|\\newtcolorbox|\\end\{document\}")
_TEX_COMMENT_RE = re.compile(r"(?<!\\)%.*$", re.M)
_TEX_NOISE_RE = re.compile(r"\\(?:label|index)\{[^{}]*\}|\\(?:noindent|par|medskip|smallskip"
                           r"|bigskip|vspace\*?\{[^{}]*\}|hfill)\b")
_SPACES_RE = re.compile(r"[ \t]*\n[ \t]*\n\s*|[ \t]+")
# Words that tie a Study-log paragraph to the University (course names are added
# from settings). "AUTH" is matched case-sensitively, so "auth token" does not count.
_UNI_WORDS_RE = re.compile(r"\bUniversity\b|Πανεπιστ|\bΑΠΘ\b|\bΤΗΜΜΥ\b|elearning\.auth"
                           r"|03 Resources/University", re.I)
_UNI_UPPER_RE = re.compile(r"\bAUTH\b")
_BULLET_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
# LaTeX specials that make a filename unsafe inside \href{run:...}.
_LATEX_SPECIALS_RE = re.compile(r"[\\{}%#&_^~$]")


# ---- values ---------------------------------------------------------------------

@dataclass(frozen=True)
class WeekFacts:
    week_start: date                       # Monday
    week_end: date                         # Saturday
    week_number: int | None                # semester week, 1-based; None outside it
    planned: tuple[dict, ...] = ()         # scheduled sessions (and holiday rows)
    guides: tuple[dict, ...] = ()          # runs.json records Mon..Sat, latest per course-day
    guide_excerpts: dict = field(default_factory=dict)   # "date|course_key" -> text
    deadlines_past: tuple[dict, ...] = ()  # due Mon..Sat
    deadlines_next: tuple[dict, ...] = ()  # due Sun..next Sat
    completed_this_week: tuple[dict, ...] = ()   # done (inferred or checked), due this week
    deadlines_note: str = ""               # "" | "no_token" | "offline: ..." | "as of ..."
    vault_changes: tuple[str, ...] = ()    # "added: Electronics I/Diodes.md"
    claude_minutes: int | None = None      # None = no session logs to read
    claude_sessions: int = 0
    study_log_uni: str = ""
    exams_next: tuple[dict, ...] = ()      # tests/finals Sun..Sat+14 (exams.json)

    def has_activity(self) -> bool:
        """Anything beyond the timetable worth a PDF?"""
        return bool(self.guides or self.deadlines_past or self.deadlines_next
                    or self.completed_this_week or self.vault_changes or self.exams_next
                    or self.claude_minutes or self.study_log_uni.strip())

    def counts(self) -> dict:
        return {
            "planned": sum(1 for p in self.planned if p.get("course_key")),
            "guides_ok": sum(1 for g in self.guides if g.get("status") == OK),
            "guides_failed": sum(1 for g in self.guides if g.get("status") == FAILED),
            "deadlines_past": len(self.deadlines_past),
            "deadlines_next": len(self.deadlines_next),
            "completed": len(self.completed_this_week),
            "vault_changes": len(self.vault_changes),
            "claude_minutes": self.claude_minutes or 0,
        }

    def to_prompt_dict(self) -> dict:
        data = asdict(self)
        data["week_start"] = self.week_start.isoformat()
        data["week_end"] = self.week_end.isoformat()
        data["claude_minutes_in_university_notes"] = data.pop("claude_minutes")
        return data


@dataclass(frozen=True)
class WeeklyResult:
    week_start: str                        # Monday, ISO
    status: str                            # OK | SKIPPED | FAILED
    reason: str = ""
    week_number: int | None = None
    pdf_path: str = ""                     # filed PDF (or the build PDF when writes are off)
    built_pdf: str = ""                    # the build dir's PDF, for Telegram
    cost_usd: float | None = None
    counts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == OK

    def summary(self) -> str:
        """One line for Telegram / the chat."""
        label = (f"Week {self.week_number:02d}" if self.week_number
                 else f"week of {self.week_start}")
        if self.status == OK:
            c = self.counts
            where = Path(self.pdf_path).name if self.pdf_path else ""
            return (f"Weekly university review ready: {label} ({c.get('guides_ok', 0)} guides, "
                    f"{c.get('deadlines_next', 0)} deadlines next week). {where}").strip()
        if self.status == SKIPPED:
            return f"Weekly university review skipped ({label}): {self.reason}"
        return f"Weekly university review failed ({label}): {self.reason}"


# ---- weeks ----------------------------------------------------------------------

def week_bounds(today: date) -> tuple[date, date]:
    """(Monday, Saturday) of the week `today` is in; Sunday maps back to it."""
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=SATURDAY_OFFSET)


def last_saturday(today: date) -> date:
    """The most recent Saturday on or before `today` (the scheduled slot's day).

    The Saturday task may catch up or be deferred into Monday; its week is
    still the one that ended on that Saturday, never the one just begun.
    """
    return today - timedelta(days=(today.weekday() - SATURDAY_OFFSET) % 7)


def semester_week(settings: Settings, monday: date) -> int | None:
    """1-based semester week of `monday` (week 1 holds semester_start), or None."""
    if settings.semester_start is None:
        return None
    first_monday, _ = week_bounds(settings.semester_start)
    number = (monday - first_monday).days // 7 + 1
    return number if number >= 1 else None


def outside_semester(settings: Settings, monday: date) -> str:
    """Why the week Monday..Saturday is outside the semester, or ""."""
    saturday = monday + timedelta(days=SATURDAY_OFFSET)
    if settings.semester_start and saturday < settings.semester_start:
        return f"before the semester starts ({settings.semester_start.isoformat()})"
    if settings.semester_end and monday > settings.semester_end:
        return f"after the semester ended ({settings.semester_end.isoformat()})"
    return ""


def review_filename(settings: Settings, monday: date) -> str:
    """"YYYY-MM-DD - Weekly Review - Week NN.pdf" (Monday's date)."""
    number = semester_week(settings, monday)
    return safe_filename(monday.isoformat(), FILENAME_LABEL,
                         f"Week {number:02d}" if number else "")


# ---- state ----------------------------------------------------------------------

def already_done(monday: date, *, path: Path = WEEKLY_STATE) -> bool:
    record = JsonCache(path).get(monday.isoformat())
    return isinstance(record, dict) and record.get("status") == OK


def record_for(monday: date, *, path: Path = WEEKLY_STATE) -> dict | None:
    record = JsonCache(path).get(monday.isoformat())
    return record if isinstance(record, dict) else None


def all_records(*, path: Path = WEEKLY_STATE) -> dict[str, dict]:
    """Every recorded week, {monday iso: record}; for listings."""
    try:
        raw = json.loads(read_text_locked(path))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, dict)}


def _record(result: WeeklyResult, path: Path) -> None:
    """Store the outcome, unless it would hide an earlier OK build of the week
    (a forced re-run that fails must not make the week look never built)."""
    previous = JsonCache(path).get(result.week_start)
    if not result.ok and isinstance(previous, dict) and previous.get("status") == OK:
        log.info("weekly review %s: keeping the earlier OK record over %s",
                 result.week_start, result.status)
        return
    entry = {**asdict(result), "recorded_at": datetime.now().isoformat(timespec="seconds")}
    try:
        JsonCache(path).put(result.week_start, entry)
    except OSError as exc:
        log.warning("weekly review: could not record the outcome in %s: %s", path.name, exc)


# ---- gathering ------------------------------------------------------------------

def gather(settings: Settings, cfg, week_start: date, *, snapshot=None,
           refresh_deadlines: bool = True, run_state: RunState | None = None,
           build_dir: Path = BUILD_DIR, today: date | None = None,
           exams=None) -> WeekFacts:
    """Everything the week left behind. No network except the (cache-first,
    rate-floored, never-raising) deadline refresh; pass `snapshot` to skip it."""
    monday, saturday = week_bounds(week_start)
    vault = str(cfg.get("vault_path", "") or "").strip()
    guides, excerpts = _guides(settings, run_state or RunState(), monday, saturday,
                               vault, build_dir)
    from . import cowork
    guides = [*guides, *cowork.weekly_rows(cfg, monday, saturday)]
    if snapshot is None:
        snapshot = _deadline_snapshot(cfg, refresh_deadlines)
    past, upcoming, done, note = _deadlines(settings, snapshot, monday, saturday)
    minutes, sessions = _claude_minutes(settings, vault, monday, saturday,
                                        today or date.today())
    return WeekFacts(
        week_start=monday, week_end=saturday,
        week_number=semester_week(settings, monday),
        planned=tuple(_planned(settings, monday, saturday)),
        guides=tuple(guides),
        guide_excerpts=excerpts,
        deadlines_past=past, deadlines_next=upcoming, completed_this_week=done,
        deadlines_note=note,
        vault_changes=tuple(_vault_changes(settings, vault, monday, saturday)),
        claude_minutes=minutes, claude_sessions=sessions,
        study_log_uni=_study_log_uni(settings, vault, monday, saturday),
        exams_next=_exams_next(settings, saturday, exams),
    )


EXAMS_AHEAD_DAYS = 14


def _exams_next(settings: Settings, saturday: date, exams=None) -> tuple[dict, ...]:
    """Tests and finals dated Sunday..Saturday+14, from exams.json (disk only).

    `exams` (a list of exams.Exam) skips the disk read. Never raises: a broken
    store costs the review one section, not the PDF.
    """
    try:
        if exams is None:
            from .exams import load_exams
            exams = load_exams()
        lo, hi = saturday + timedelta(days=1), saturday + timedelta(days=EXAMS_AHEAD_DAYS)
        rows = []
        for e in exams:
            if not lo <= e.day <= hi:
                continue
            course = (course_name(settings, e.course_key) if e.course_key
                      else e.course_name)
            rows.append({"date": e.date, "time": e.time or "", "kind": e.kind,
                         "course": course, "title": e.title,
                         "confirmed": bool(e.confirmed), "source": e.source})
        return tuple(sorted(rows, key=lambda r: (r["date"], r["time"])))
    except Exception as exc:  # noqa: BLE001 — one input of many
        log.warning("weekly review: exams unreadable (%s)", exc)
        return ()


def _days(monday: date, saturday: date):
    return (monday + timedelta(days=n) for n in range((saturday - monday).days + 1))


def _planned(settings: Settings, monday: date, saturday: date) -> list[dict]:
    """Scheduled sessions (not attendance), plus one row per holiday."""
    rows: list[dict] = []
    for day in _days(monday, saturday):
        sessions = todays_sessions(settings, day)
        for s in sessions:
            course = _course(settings, s.course_key)
            rows.append({"date": day.isoformat(), "weekday": day.strftime("%A"),
                         "course_key": s.course_key, "course": course[0],
                         "course_gr": course[1], "type": s.type,
                         "start": s.start, "end": s.end})
        if not sessions and day.isoformat() in settings.no_class_dates:
            rows.append({"date": day.isoformat(), "weekday": day.strftime("%A"),
                         "no_class_reason": no_class_reason(settings, day)})
    return rows


def _course(settings: Settings, key: str) -> tuple[str, str]:
    """(English name, Greek name) of a course key; the key itself if unknown."""
    course = course_by_key(settings, key)
    if course is None:
        return key, key
    return course.name_en or course.name_gr, course.name_gr or course.name_en


def _guides(settings: Settings, state: RunState, monday: date, saturday: date,
            vault: str, build_dir: Path) -> tuple[list[dict], dict[str, str]]:
    """(guide rows, {"date|course_key": excerpt}) for the week's runs.json records."""
    rows: list[dict] = []
    excerpts: dict[str, str] = {}
    for rec in state.between(monday.isoformat(), saturday.isoformat()):
        key = str(rec.get("course_key") or "")
        if not key or key.startswith(INTERNAL_KEY_PREFIX):
            continue
        name_en, name_gr = _course(settings, key)
        pdf = str(rec.get("pdf_path") or "")
        row = {"date": str(rec.get("date")), "course_key": key, "course": name_en,
               "course_gr": name_gr, "status": str(rec.get("status") or ""),
               "topic": str(rec.get("topic") or ""),
               "topic_inferred": bool(rec.get("topic_inferred")),
               "reason": str(rec.get("reason") or "")}
        if pdf:
            row.update(_pdf_refs(settings, vault, Path(pdf)))
        rows.append(row)
        if row["status"] == OK:
            excerpt = _excerpt_for(rec, build_dir)
            if excerpt:
                excerpts[f"{row['date']}|{key}"] = excerpt
    return rows, excerpts


def _pdf_refs(settings: Settings, vault: str, pdf: Path) -> dict:
    """File name, vault-relative path and a relative run: link from the review."""
    refs = {"pdf_name": pdf.name, "pdf_exists": pdf.is_file()}
    try:
        refs["vault_path"] = pdf.resolve().relative_to(Path(vault).resolve()).as_posix()
    except (ValueError, OSError):
        return refs
    try:
        rel = pdf.resolve().relative_to(Path(settings.vault_root).resolve())
    except (ValueError, OSError):
        return refs
    link = (PurePosixPath("..") / PurePosixPath(rel.as_posix())).as_posix()
    if not _LATEX_SPECIALS_RE.search(link):
        refs["link"] = link
    return refs


def _deadline_snapshot(cfg, refresh: bool):
    """assignments.refresh (cache first, never raises) or cached(); None if absent."""
    try:
        from . import assignments
    except ImportError as exc:
        log.warning("weekly review: assignments module unavailable (%s)", exc)
        return None
    try:
        return assignments.refresh(cfg) if refresh else assignments.cached()
    except Exception as exc:  # noqa: BLE001 — deadlines are one input of many
        log.warning("weekly review: deadline refresh failed (%s); using the cache", exc)
    try:
        return assignments.cached()
    except Exception as exc:  # noqa: BLE001
        log.warning("weekly review: no deadline cache either (%s)", exc)
        return None


def _deadlines(settings: Settings, snapshot, monday: date, saturday: date):
    """(past, next, completed, note) from a Snapshot, split around Saturday."""
    if snapshot is None:
        return (), (), (), "unavailable"
    week_lo = datetime.combine(monday, time.min)
    week_hi = datetime.combine(saturday, time.max)
    next_hi = datetime.combine(saturday + timedelta(days=NEXT_WEEK_DAYS), time.max)
    past, upcoming, done = [], [], []
    for item in snapshot_items(snapshot):
        row = _deadline_row(settings, item)
        if row is None:
            continue
        due = datetime.fromisoformat(row["due"])
        if week_lo <= due <= week_hi:
            (done if row["status"] in DONE_STATUSES else past).append(row)
        elif week_hi < due <= next_hi:
            upcoming.append(row)
    note = str(getattr(snapshot, "error", "") or "")
    fetched = getattr(snapshot, "fetched_at_iso", None)
    if fetched and not note:
        note = f"as of {fetched}"
    by_due = itemgetter("due")
    return (tuple(sorted(past, key=by_due)), tuple(sorted(upcoming, key=by_due)),
            tuple(sorted(done, key=by_due)), note)


def _deadline_row(settings: Settings, item) -> dict | None:
    ts = getattr(item, "due_ts", None)
    try:
        due = datetime.fromtimestamp(int(ts))
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    key = getattr(item, "course_key", None)
    course = (course_name(settings, str(key)) if key
              else str(getattr(item, "course_short", "") or ""))
    return {"title": str(getattr(item, "title", "") or ""), "course": course,
            "kind": str(getattr(item, "kind", "") or ""),
            "due": due.isoformat(timespec="minutes"),
            "overdue": bool(getattr(item, "overdue", False)),
            "status": str(getattr(item, "status", "") or "open")}


# ---- excerpts -------------------------------------------------------------------

def _excerpt_for(rec: dict, build_dir: Path) -> str:
    """«Τι μάθαμε σήμερα» of one ok guide, from its build dir's guide.tex, or ""."""
    extra = rec.get("extra") if isinstance(rec.get("extra"), dict) else {}
    candidates = [Path(str(extra["workdir"])) / TEX_NAME] if extra.get("workdir") else []
    candidates.append(Path(build_dir) / f"{rec.get('date')}_{rec.get('course_key')}" / TEX_NAME)
    for tex in candidates:
        try:
            return extract_excerpt(tex.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return ""


def extract_excerpt(latex: str, limit: int = EXCERPT_MAX_CHARS) -> str:
    """The «Τι μάθαμε σήμερα» section of a guide, LaTeX lightly cleaned, or ""."""
    text = unicodedata.normalize("NFC", latex or "")
    body_at = text.find(r"\begin{document}")
    match = _EXCERPT_HEADING_RE.search(text, max(body_at, 0))
    if not match:
        return ""
    line_end = text.find("\n", match.end())
    start = match.end() if line_end < 0 else line_end + 1
    stop = _EXCERPT_STOP_RE.search(text, start)
    chunk = text[start:stop.start() if stop else len(text)]
    chunk = _TEX_NOISE_RE.sub(" ", _TEX_COMMENT_RE.sub("", chunk))
    chunk = _SPACES_RE.sub(lambda m: "\n" if "\n" in m.group(0) else " ", chunk).strip()
    return _trim(chunk, limit)


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip() + " …"


# ---- vault signals --------------------------------------------------------------

def _uni_rel(settings: Settings, vault: str) -> str:
    """The University folder, vault-relative ("03 Resources/University"), or ""."""
    if not vault or not str(settings.vault_root) or str(settings.vault_root) == ".":
        return ""
    try:
        return Path(settings.vault_root).resolve().relative_to(
            Path(vault).resolve()).as_posix()
    except (ValueError, OSError):
        return ""


def _git_log(cwd: Path, *args: str) -> tuple[bool, str]:
    """`git log ...` in `cwd`, read-only. Refuses every other subcommand."""
    if not args or args[0] not in ALLOWED_GIT:
        raise ValueError(f"git {args[0] if args else '(nothing)'} is not allowed here")
    try:
        proc = subprocess.run(
            # quotePath=false: Greek filenames come back as UTF-8, not "\316\224".
            ["git", "-c", "core.quotePath=false", *args], cwd=str(cwd),
            capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=GIT_TIMEOUT_SEC, env=child_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("weekly review: git log failed: %s", exc)
        return False, ""
    return proc.returncode == 0, proc.stdout or ""


def _vault_changes(settings: Settings, vault: str, monday: date,
                   saturday: date) -> list[str]:
    """University files added/changed/deleted this week, from the vault's git."""
    uni = _uni_rel(settings, vault)
    if not uni or not (Path(vault) / ".git").exists():
        return []
    until = saturday + timedelta(days=1)
    ok, out = _git_log(Path(vault), "log", f"--since={monday.isoformat()} 00:00",
                       f"--until={until.isoformat()} 00:00", "--name-status",
                       "--format=", "--no-renames", "--", uni)
    if not ok:
        return []
    return parse_name_status(out, uni)


_STATUS_WORDS = {"A": "added", "M": "edited", "D": "deleted"}


def parse_name_status(text: str, uni_rel: str) -> list[str]:
    """`git log --name-status` lines -> "added: <path under University>", unique,
    newest first, own Weekly Reviews folder excluded, capped."""
    prefix = uni_rel.rstrip("/") + "/"
    seen: dict[str, str] = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or not parts[0]:
            continue
        path = parts[-1].strip().strip('"')
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if not rel or rel.startswith(f"{WEEKLY_FOLDER}/"):
            continue
        # git log is newest first: the first status seen for a path is its latest.
        seen.setdefault(rel, _STATUS_WORDS.get(parts[0][:1], "changed"))
    return [f"{word}: {rel}" for rel, word in list(seen.items())[:MAX_VAULT_CHANGES]]


def _claude_minutes(settings: Settings, vault: str, monday: date, saturday: date,
                    today: date) -> tuple[int | None, int]:
    """(minutes, sessions) of Claude sessions this week that touched University
    notes; (None, 0) when there are no session logs to read."""
    uni = _uni_rel(settings, vault)
    if not uni or not (Path(vault) / session_logs.SESSION_LOGS_FOLDER).is_dir():
        return None, 0
    window = max(1, (today - monday).days + 1)
    needle = uni.lower().rstrip("/") + "/"
    total = 0.0
    count = 0
    for s in session_logs.read_sessions(vault, window):
        if not monday <= s.day <= saturday:
            continue
        if any(needle in (p.rstrip("/") + "/") for p in s.paths):
            total += s.minutes
            count += 1
    return int(round(total)), count


def _study_log_uni(settings: Settings, vault: str, monday: date, saturday: date) -> str:
    """This week's Study-log paragraphs that name a course or the University."""
    if not vault:
        return ""
    try:
        text = (Path(vault) / STUDY_LOG_NOTE).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return filter_study_log(text, _course_patterns(settings), monday, saturday)


def _course_patterns(settings: Settings) -> list[re.Pattern]:
    words: set[str] = set()
    for c in settings.courses:
        words.update(w for w in (c.name_en, c.name_gr, c.vault_folder, c.key) if w)
    return [re.compile(rf"(?<!\w){re.escape(w)}(?!\w)", re.I) for w in sorted(words)]


def filter_study_log(text: str, course_res: list[re.Pattern], monday: date,
                     saturday: date, limit: int = STUDY_LOG_MAX_CHARS) -> str:
    """Dated `## YYYY-MM-DD` sections in [monday, saturday], keeping only the
    paragraphs that are about University work; oldest first, capped."""
    heads = list(_DATE_HEADING_RE.finditer(text or ""))
    kept: list[tuple[str, list[str]]] = []
    for n, head in enumerate(heads):
        try:
            day = date.fromisoformat(head.group(1))
        except ValueError:
            continue
        if not monday <= day <= saturday:
            continue
        end = heads[n + 1].start() if n + 1 < len(heads) else len(text)
        paras = re.split(r"\n\s*\n", text[head.end():end])
        uni = []
        for raw in paras:
            # Sub-headings ("### What I changed") often sit on the line right
            # above a bullet list, in the same paragraph: drop just the heading.
            para = "\n".join(ln for ln in raw.splitlines()
                             if not ln.lstrip().startswith("#")).strip()
            if not para:
                continue
            units = [u for u in _units(para) if _is_uni(u, course_res)]
            if units:
                uni.append("\n".join(units))
        if uni:
            kept.append((head.group(1), uni))
    kept.sort(key=lambda k: k[0])
    return _trim("\n\n".join(f"## {day}\n" + "\n\n".join(paras) for day, paras in kept), limit)


def _units(paragraph: str) -> list[str]:
    """A bullet list splits into its bullets (an unrelated bullet must not ride
    along with a University one); any other paragraph is one unit."""
    lines = [ln for ln in paragraph.splitlines() if ln.strip()]
    if len(lines) > 1 and all(_BULLET_RE.match(ln) for ln in lines):
        return lines
    return [paragraph]


def _is_uni(text: str, course_res: list[re.Pattern]) -> bool:
    if _UNI_WORDS_RE.search(text) or _UNI_UPPER_RE.search(text):
        return True
    return any(r.search(text) for r in course_res)


# ---- prompt ---------------------------------------------------------------------

def render_prompt(facts: WeekFacts) -> str:
    """The user message: the weekly_user template with the facts as JSON."""
    from .prompt import load_prompt
    return load_prompt("weekly_user").format(
        week_start=facts.week_start.isoformat(),
        week_end=facts.week_end.isoformat(),
        week_label=f"Εβδομάδα {facts.week_number}" if facts.week_number else "Εβδομάδα",
        facts_json=json.dumps(facts.to_prompt_dict(), ensure_ascii=False, indent=1),
    )


def system_prompt() -> str:
    from .prompt import load_prompt
    return load_prompt("weekly_system")


# ---- the run --------------------------------------------------------------------

GenerateFn = Callable[..., object]          # run_claude-compatible
BuildFn = Callable[..., tuple]              # build_with_repair-compatible


def run_weekly(settings: Settings, cfg, week_start: date, *, force: bool = False,
               state_path: Path = WEEKLY_STATE, lock_path: Path = WEEKLY_LOCK,
               work_dir: Path = WEEKLY_DIR, gather_fn: Callable[..., WeekFacts] | None = None,
               generate_fn: GenerateFn | None = None,
               build_fn: BuildFn | None = None) -> WeeklyResult:
    """Build, file and record the review for the week holding `week_start`.

    Never raises: every failure is a FAILED WeeklyResult with a short reason.
    The *_fn hooks exist for the tests (no CLI, no xelatex).
    """
    monday, _ = week_bounds(week_start)
    base = WeeklyResult(week_start=monday.isoformat(), status=SKIPPED,
                        week_number=semester_week(settings, monday))
    if not force and already_done(monday, path=state_path):
        return replace(base, reason="already built")
    try:
        with run_lock(lock_path):
            result = _run_locked(settings, cfg, monday, base, work_dir,
                                 gather_fn or gather, generate_fn, build_fn)
    except LockBusy as exc:   # not recorded: the running one records itself
        return replace(base, reason=f"another weekly review is running ({exc})")
    except Exception as exc:  # noqa: BLE001 — a scheduled job must report, not crash
        log.exception("weekly review: unexpected failure")
        result = replace(base, status=FAILED, reason=f"{type(exc).__name__}: {exc}")
    _record(result, state_path)
    log.info("weekly review %s: %s %s", monday, result.status, result.reason)
    return result


def _run_locked(settings: Settings, cfg, monday: date, base: WeeklyResult,
                work_dir: Path, gather_fn, generate_fn, build_fn) -> WeeklyResult:
    facts = gather_fn(settings, cfg, monday)
    base = replace(base, counts=facts.counts())
    if not facts.has_activity():
        return replace(base, reason="nothing to summarise this week")
    workdir = Path(work_dir) / monday.isoformat()
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        latex, cost = _generate(settings, cfg, facts, workdir, generate_fn)
    except Exception as exc:  # noqa: BLE001 — ClaudeCLIError and friends
        return replace(base, status=FAILED, reason=f"generation failed: {exc}")
    built = _build(settings, latex, workdir, build_fn)
    if not built.ok or built.pdf_path is None:
        tail = [ln for ln in (built.log_tail or "").splitlines() if ln.strip()]
        last = next((ln for ln in tail if ln.startswith("!")), tail[-1] if tail else "no log")
        return replace(base, status=FAILED, cost_usd=cost,
                       reason=f"LaTeX build failed: {last[:200]} (see {workdir})")
    pdf = Path(built.pdf_path)
    base = replace(base, built_pdf=str(pdf), cost_usd=cost)
    if not settings.allow_vault_writes:
        return replace(base, status=OK, pdf_path=str(pdf),
                       reason=f"vault writes are off — PDF left at {pdf}")
    try:
        dest = file_pdf(pdf, settings.vault_root, WEEKLY_FOLDER,
                        review_filename(settings, monday))
    except (OSError, ValueError) as exc:
        return replace(base, status=FAILED, reason=f"built but could not file: {exc}")
    return replace(base, status=OK, pdf_path=str(dest))


def _generate(settings: Settings, cfg, facts: WeekFacts, workdir: Path,
              generate_fn) -> tuple[str, float | None]:
    from .generate import GUIDE_MAX_OUTPUT_TOKENS, run_claude, strip_to_document
    call = generate_fn or run_claude
    model = str(cfg.get("study_weekly_model", DEFAULT_WEEKLY_MODEL) or DEFAULT_WEEKLY_MODEL)
    result = call(render_prompt(facts), model=model, cwd=workdir,
                  system_prompt=system_prompt(), tools="",
                  timeout=settings.timeout_sec, claude_cmd=settings.claude_cmd,
                  max_output_tokens=GUIDE_MAX_OUTPUT_TOKENS, collect_text=True)
    return strip_to_document(result.text), getattr(result, "cost_usd", None)


def _build(settings: Settings, latex: str, workdir: Path, build_fn):
    from .build import build_with_repair
    from .generate import repair_guide
    call = build_fn or build_with_repair
    built, _ = call(latex, workdir, settings.xelatex,
                    lambda tex, tail: repair_guide(tex, tail, workdir=workdir, settings=settings),
                    max_repairs=MAX_REPAIRS)
    return built


# ---- hub note helpers (the app writes the note through its vault tools) ---------

def hub_note_rel(settings: Settings, vault: str) -> str:
    """Vault-relative path of the Weekly Reviews hub note, or "" without a vault."""
    uni = _uni_rel(settings, vault)
    return f"{uni}/{WEEKLY_FOLDER}/{HUB_NOTE_NAME}" if uni else ""


def hub_note_header() -> str:
    return ("# Weekly Reviews\n\n"
            "The Saturday university review PDFs, one line per week, newest last.\n\n"
            "*Part of* [[University - AUTH]] · *see also* [[Study log]]\n\n")


def hub_line(result: WeeklyResult) -> str:
    """"- [[<pdf name>]] · 5 guides, 2 deadlines next week" for an OK result."""
    name = Path(result.pdf_path).name if result.pdf_path else ""
    c = result.counts or {}
    return (f"- [[{name}]] · {c.get('guides_ok', 0)} guides, "
            f"{c.get('deadlines_next', 0)} deadlines next week")
