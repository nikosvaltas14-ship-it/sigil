"""The scheduled jobs, for running headless (Task Scheduler / cron).

    catchup  hourly: build a guide for every finished class that has none yet
    remind   every 30 min: Telegram reminders for tests, finals and deadlines
    weekly   Saturdays: the weekly review PDF

Each returns a short status line and sends at most one Telegram message.
Telegram hears only what is new (a guide filed, or a reason that changed), so
hourly retries of "no material posted yet" stay in the log.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path

from .. import notify
from ..fileio import atomic_write_text, read_text_locked
from . import catchup, icloud_calendar, semester, weekly
from .models import OK, WORK_DIR
from .run import run
from .settings import load_settings
from .state import LockBusy

log = logging.getLogger(__name__)

# A guide costs several Opus calls; material crafted to fail every build or
# review must not be able to burn the whole Claude plan. Config
# `study_guides_max_guides_per_day` caps the guide attempts that reach the model.
DEFAULT_MAX_GUIDES_PER_DAY = 6
BUDGET_PATH = WORK_DIR / "daily_budget.json"


def _spent_today(today: date, path: Path | None = None) -> int:
    try:
        data = json.loads(read_text_locked(path or BUDGET_PATH))
    except (OSError, ValueError):
        return 0
    return int(data.get("attempts", 0)) if data.get("date") == today.isoformat() else 0


def _record_spent(today: date, n: int, path: Path | None = None) -> None:
    atomic_write_text(path or BUDGET_PATH, json.dumps({"date": today.isoformat(), "attempts": n}))


def _reached_model(result) -> bool:
    return result.status == OK or bool(result.extra.get("generation_attempted"))


def run_catchup(cfg, now: datetime | None = None) -> str:
    from . import summary_text
    from .timetable import no_class_reason
    settings = load_settings(cfg)
    if not settings.enabled:
        return "skipped: study_guides_enabled is off"
    now = now or datetime.now()
    day = now.date()
    if semester.notice_due(settings, day):
        semester.mark_sent(settings)     # before sending: a failed send must not repeat nightly
        try:
            text = semester.notice_text(settings, semester.find_timetable_url())
        except Exception:  # noqa: BLE001 — the notice still goes out without the link
            log.exception("semester notice: timetable lookup failed")
            text = semester.notice_text(settings)
        notify.send_telegram(cfg, text)
        return f"skipped: the semester ended on {settings.semester_end:%Y-%m-%d}"
    lookback = int(cfg.get("study_guides_catchup_days", catchup.DEFAULT_LOOKBACK_DAYS) or 0)
    icloud_calendar.refresh_if_stale(now)
    items = catchup.due(settings, now, lookback)
    if not items:
        return f"skipped: {no_class_reason(settings, day) or 'every finished class has its guide'}"
    told: list[str] = []
    cap = int(cfg.get("study_guides_max_guides_per_day", DEFAULT_MAX_GUIDES_PER_DAY) or 0)
    spent = _spent_today(day)
    for item in items:
        if cap and spent >= cap:
            log.warning("study guides: daily cap of %d guide attempts reached", cap)
            told.append(f"Study guides: daily cap of {cap} reached; the rest wait for tomorrow.")
            break
        try:
            results = run(cfg, day=item.day, course=item.course_key, out=lambda _line: None)
        except LockBusy as exc:
            told.append(f"Study guides {item.day:%Y-%m-%d}: not run ({exc})")
            break
        except Exception as exc:  # noqa: BLE001 — one course must not stop the rest
            log.exception("study guides run crashed")
            told.append(f"Study guides {item.day:%Y-%m-%d} {item.course_key}: "
                        f"crashed ({type(exc).__name__})")
            continue
        spent += sum(1 for r in results if _reached_model(r))
        _record_spent(day, spent)
        log.info("study guides: %s", summary_text(results, item.day.isoformat()).replace("\n", " "))
        new = [r for r in results
               if r.status == OK or (not r.extra.get("already_done")
                                     and r.reason != item.last_reason)]
        if new:
            told.append(summary_text(new, item.day.isoformat()))
    if told:
        notify.send_telegram(cfg, "\n".join(told))
    return f"checked {len(items)} due class day(s)"


def run_remind(cfg) -> str:
    if not cfg.get("study_remind_enabled", True):
        return "skipped: study_remind_enabled is off"
    from .reminders import run_reminders
    return run_reminders(cfg)


def run_weekly_job(cfg, now: datetime | None = None, force: bool = False) -> str:
    if not cfg.get("study_weekly_enabled", True):
        return "skipped: study_weekly_enabled is off"
    settings = load_settings(cfg)
    today = (now or datetime.now()).date()
    monday, _ = weekly.week_bounds(today if force else weekly.last_saturday(today))
    outside = weekly.outside_semester(settings, monday)
    if outside:
        return f"skipped: week of {monday:%Y-%m-%d} is {outside}"
    if not force and weekly.already_done(monday):
        return f"skipped: the week of {monday:%Y-%m-%d} is already built"
    result = weekly.run_weekly(settings, cfg, monday, force=force)
    summary = result.summary()
    if result.ok or result.status != "skipped":
        notify.send_telegram(cfg, summary)
    if result.ok and cfg.get("study_weekly_send_pdf", True):
        pdf = next((p for p in (result.pdf_path, result.built_pdf) if p and Path(p).is_file()), "")
        if pdf and not notify.send_telegram_document(cfg, pdf, Path(pdf).stem):
            log.warning("weekly review: the PDF could not be sent on Telegram")
    return summary.splitlines()[0] if summary else result.status
