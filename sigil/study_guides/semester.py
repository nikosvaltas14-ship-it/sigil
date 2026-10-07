"""The once-per-semester "your timetable is over" message.

Design rules:

* **Once per semester, keyed on its end date.** The nightly task runs every
  day, so the notice would repeat nightly without a marker. The marker stores
  the `semester_end` it announced; setting a new semester in config.json makes
  the next end announce itself again, with nothing to reset by hand.
* **Only after the semester, never before.** The first scheduled run on a day
  after `semester_end` sends it; a run during the semester never does.
* **The timetable link is best effort.** ece.auth.gr is fetched once, politely,
  with a short timeout, and any link that names the timetable is offered. When
  the page cannot be read or has no such link, the message still goes out and
  just asks for the new timetable instead. Sigil cannot read a timetable image
  on its own, so the new slots are still filled in through a Claude session.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

from ..fileio import atomic_write_text, read_text_locked
from .models import WORK_DIR
from .settings import Settings

log = logging.getLogger(__name__)

NOTICE_PATH = WORK_DIR / "semester_notice.json"

# The department site, and how long to wait on it before giving up.
DEPARTMENT_URL = "https://ece.auth.gr/"
DEPARTMENT_TIMEOUT_SEC = 10.0

# Link text or href that names the weekly timetable.
_TIMETABLE_LINK = re.compile(r"ωρολ[όο]γιο|orologio|timetable|πρ[όο]γραμμα\s+μαθημ[άα]των",
                             re.IGNORECASE)


def notice_due(settings: Settings, day: date, marker_path: Path = NOTICE_PATH) -> bool:
    """True the first time `day` falls after a semester_end not yet announced."""
    end = settings.semester_end
    if end is None or day <= end:
        return False
    return _announced_end(marker_path) != end.isoformat()


def mark_sent(settings: Settings, marker_path: Path = NOTICE_PATH) -> None:
    """Record that this semester's end has been announced."""
    if settings.semester_end is None:
        return
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(marker_path, json.dumps(
        {"announced_end": settings.semester_end.isoformat()}, indent=2))


def notice_text(settings: Settings, timetable_url: str = "") -> str:
    """The Telegram message; `timetable_url` empty means none was found."""
    end = settings.semester_end.isoformat() if settings.semester_end else "?"
    lines = [f"The semester ended on {end}, so the nightly study guides have "
             "stopped: the old timetable no longer applies."]
    if timetable_url:
        lines.append(f"The department's timetable page: {timetable_url}")
    else:
        lines.append(f"I couldn't find the new timetable on {DEPARTMENT_URL}.")
    lines.append("Send the new weekly timetable (and the new semester dates) to "
                 "a Claude session and it will update study_guides_timetable.")
    return "\n".join(lines)


def find_timetable_url(fetch=None) -> str:
    """The first ece.auth.gr link that names the timetable, or "".

    `fetch(url) -> str` returns the page HTML; injectable for tests. Every
    failure is logged and returns "" — the notice goes out regardless.
    """
    try:
        html = (fetch or _fetch)(DEPARTMENT_URL)
    except Exception as exc:  # noqa: BLE001 — best effort by design
        log.info("study guides: couldn't read %s for the timetable link (%s)",
                 DEPARTMENT_URL, exc)
        return ""
    parser = _LinkParser()
    try:
        from .moodle import _defuse_html
        parser.feed(_defuse_html(html))
    except Exception as exc:  # noqa: BLE001 — malformed HTML
        log.info("study guides: couldn't parse %s (%s)", DEPARTMENT_URL, exc)
        return ""
    from .exam_sources import allowed_host
    for href, text in parser.links:
        if _TIMETABLE_LINK.search(text) or _TIMETABLE_LINK.search(href):
            url = urljoin(DEPARTMENT_URL, href)
            if allowed_host(url):        # only an https auth.gr link reaches Telegram
                return url
    return ""


def _fetch(url: str) -> str:
    from .exam_sources import fetch_capped
    return fetch_capped(url, DEPARTMENT_TIMEOUT_SEC)


def _announced_end(marker_path: Path) -> str:
    try:
        return str(json.loads(read_text_locked(marker_path)).get("announced_end") or "")
    except FileNotFoundError:
        return ""
    except (OSError, ValueError, AttributeError) as exc:
        log.warning("study guides: %s unreadable (%s); treating as not sent",
                    marker_path.name, exc)
        return ""


class _LinkParser(HTMLParser):
    """Collects (href, visible text) for every <a> on the page."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
