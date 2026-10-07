"""Command line for the study guides, for setup, backfills and dry runs.

    python -m sigil.study_guides login                  # one-time Moodle token
    python -m sigil.study_guides discover               # course ids + schedules
    python -m sigil.study_guides run [--date YYYY-MM-DD] [--course KEY]
                                     [--force] [--dry-run]
    python -m sigil.study_guides goodnotes              # notes folders + new pages
    python -m sigil.study_guides calendar-login         # one-time iCloud calendar login
    python -m sigil.study_guides calendar [--days N]    # refresh + list upcoming events

Scheduled runs use `catchup`, `remind` and `weekly` (see README); `run` is the
same pipeline by hand. Run it with the venv's python.

Design rules:

* **Logs go to stderr and to data/study_guides/study_guides.log**, never to
  stdout, so a dry run's printed prompt is not interleaved with log lines.
  httpx is held at WARNING: at INFO it logs every request URL, and a Moodle
  file URL carries the token.
* **The exit code says what happened**: 0 all fine, 1 at least one course
  failed or the command crashed, 3 another run holds the lock. argparse's own
  usage errors exit 2.
* **The password never passes through here.** `login` hands straight to
  `moodle.interactive_login`, which reads it with getpass and keeps only the
  token.
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import date

from ..config import load_config
from .models import FAILED, WORK_DIR
from .moodle import interactive_login
from .run import discover, run
from .settings import load_settings
from .state import LockBusy

log = logging.getLogger("sigil.study_guides")

LOG_PATH = WORK_DIR / "study_guides.log"
# The CLI log rotates at this size, keeping this many old files: a nightly run
# writes a few kilobytes, so this is months of history in a bounded footprint.
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 3

# Loggers that would print request URLs (with the Moodle token) at INFO.
_QUIET_LOGGERS = ("httpx", "httpcore")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _utf8_console()
    _setup_logging(verbose=args.verbose)
    try:
        cfg = load_config()
        return args.handler(cfg, args)
    except LockBusy as exc:
        log.warning("study guides: %s", exc)
        print(f"Not started: {exc}", file=sys.stderr)
        return EXIT_LOCKED
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001 — report, log the traceback, exit non-zero
        log.exception("study guides: %s crashed", args.command)
        print(f"Failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m sigil.study_guides",
                                     description="Sigil study assistant.")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="debug-level logging on stderr")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="get a Moodle token (password is not stored)")
    login.add_argument("--browser", action="store_true",
                       help="sign in on ΑΠΘ's SSO page in a small browser window "
                            "(for when username/password is refused)")
    login.set_defaults(handler=_cmd_login)

    disc = sub.add_parser("discover", help="map Moodle courses to course keys, "
                                           "show their schedules, save the ids")
    disc.set_defaults(handler=_cmd_discover)

    runp = sub.add_parser("run", help="build the guides for a day's classes")
    runp.add_argument("--date", type=_iso_date, default=None,
                      help="the class day, YYYY-MM-DD (default: today)")
    runp.add_argument("--course", default=None, help="only this course key")
    runp.add_argument("--force", action="store_true",
                      help="rebuild even if this date already succeeded")
    runp.add_argument("--dry-run", action="store_true",
                      help="print topic, files and prompts; generate nothing")
    runp.set_defaults(handler=_cmd_run)

    notes = sub.add_parser("goodnotes", help="show the GoodNotes backup folder, which "
                                             "course each subject folder maps to, and "
                                             "how many pages no guide has used yet")
    notes.set_defaults(handler=_cmd_goodnotes)

    cal_login = sub.add_parser("calendar-login", help="save an iCloud app-specific "
                                                      "password for read-only calendar access")
    cal_login.set_defaults(handler=_cmd_calendar_login)

    cal = sub.add_parser("calendar", help="refresh the iCloud calendar cache and list "
                                          "the coming events")
    cal.add_argument("--days", type=int, default=14, help="how many days ahead to list")
    cal.set_defaults(handler=_cmd_calendar)

    from ..secrets_store import SECRET_NAMES
    sset = sub.add_parser("set-secret", help="store a secret in the OS credential store "
                                             "(typed hidden; never written to a file)")
    sset.add_argument("name", choices=SECRET_NAMES)
    sset.set_defaults(handler=_cmd_set_secret)
    sdel = sub.add_parser("delete-secret", help="remove a secret from the credential store")
    sdel.add_argument("name", choices=SECRET_NAMES)
    sdel.set_defaults(handler=_cmd_delete_secret)
    slist = sub.add_parser("secrets", help="show which secrets are set (never their values)")
    slist.set_defaults(handler=_cmd_secrets)

    catch = sub.add_parser("catchup",help="build guides for every finished class that "
                                           "has none yet (run hourly)")
    catch.set_defaults(handler=_cmd_catchup)

    remind = sub.add_parser("remind", help="send due Telegram reminders (run every 30 min)")
    remind.set_defaults(handler=_cmd_remind)

    week = sub.add_parser("weekly", help="build last week's review PDF (run on Saturdays)")
    week.add_argument("--force", action="store_true",
                      help="build the current week even if already built")
    week.set_defaults(handler=_cmd_weekly)
    return parser


def _iso_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {text!r}") from None


def _cmd_login(cfg, args) -> int:
    if args.browser:
        from .sso_login import browser_login
        browser_login(load_settings(cfg).moodle_url)   # exits non-zero on its own
        return EXIT_OK
    interactive_login(load_settings(cfg).moodle_url)   # exits non-zero on its own
    return EXIT_OK


def _cmd_discover(cfg, args) -> int:
    mapping = discover(cfg)
    return EXIT_OK if mapping else EXIT_FAILED


def _cmd_run(cfg, args) -> int:
    day = args.date or date.today()
    results = run(cfg, day=day, course=args.course, dry_run=args.dry_run,
                  force=args.force)
    return EXIT_FAILED if any(r.status == FAILED for r in results) else EXIT_OK


def _cmd_goodnotes(cfg, args) -> int:
    from .goodnotes import status_lines
    settings = load_settings(cfg)
    print("\n".join(status_lines(settings.goodnotes_dir, dict(settings.goodnotes_folders),
                                  settings.courses)))
    return EXIT_OK


def _cmd_calendar_login(cfg, args) -> int:
    from .icloud_calendar import interactive_login as calendar_login
    return calendar_login()


def _cmd_calendar(cfg, args) -> int:
    from datetime import timedelta
    from .icloud_calendar import refresh
    today = date.today()
    events = refresh(today)
    last = today + timedelta(days=args.days)
    shown = [e for e in events if today <= e.day <= last]
    print(f"{len(events)} events cached; {len(shown)} in the next {args.days} days:")
    for e in shown:
        when = e.start[:10] if e.all_day else f"{e.start[:10]} {e.start[11:16]}-{e.end[11:16]}"
        where = f" @ {e.location}" if e.location else ""
        print(f"  {when}  [{e.calendar}] {e.title}{where}")
    return EXIT_OK


def _cmd_set_secret(cfg, args) -> int:
    import getpass
    from ..secrets_store import set_secret
    value = getpass.getpass(f"{args.name} (input hidden): ").strip()
    if not value:
        print("Nothing entered; nothing saved.", file=sys.stderr)
        return EXIT_FAILED
    set_secret(args.name, value)
    print(f"{args.name} saved to the credential store.")
    return EXIT_OK


def _cmd_delete_secret(cfg, args) -> int:
    from ..secrets_store import delete_secret
    print(f"{args.name} removed." if delete_secret(args.name) else f"{args.name} was not set.")
    return EXIT_OK


def _cmd_secrets(cfg, args) -> int:
    from ..secrets_store import status
    for name, where in status().items():
        print(f"  {name:<22} {'set (' + where + ')' if where else 'not set'}")
    return EXIT_OK


def _cmd_catchup(cfg, args) -> int:
    from .jobs import run_catchup
    print(run_catchup(cfg))
    return EXIT_OK


def _cmd_remind(cfg, args) -> int:
    from .jobs import run_remind
    status = run_remind(cfg)
    print(status)
    return EXIT_FAILED if status.startswith("failed") else EXIT_OK


def _cmd_weekly(cfg, args) -> int:
    from .jobs import run_weekly_job
    print(run_weekly_job(cfg, force=args.force))
    return EXIT_OK


def _utf8_console() -> None:
    """Greek topics and prompts must print on a cp1253/cp437 console too."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue   # pythonw: no console at all
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as exc:
            print(f"(console encoding unchanged: {exc})", file=sys.stderr)


def _setup_logging(verbose: bool) -> None:
    fmt = logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    pkg = logging.getLogger("sigil")
    pkg.setLevel(logging.DEBUG if verbose else logging.INFO)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    pkg.addHandler(console)
    try:
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    except OSError as exc:
        pkg.warning("study guides: log file %s unavailable (%s); stderr only", LOG_PATH, exc)
    else:
        file_handler.setFormatter(fmt)
        pkg.addHandler(file_handler)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


if __name__ == "__main__":
    sys.exit(main())
