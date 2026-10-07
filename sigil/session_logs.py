"""Read Claude session logs kept in an Obsidian vault (optional).

Each log is a note in `SESSION_LOGS_FOLDER` with flat frontmatter (`date`,
`duration_min`, `cwd`) and lines like "- edited `path/to/note.md`". The weekly
review uses them to count time spent on University notes; with no such folder
it simply reports no session time.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

SESSION_LOGS_FOLDER = "05 Session Logs"
SESSION_CAP_MIN = 90          # one forgotten-open session must not count as a day's work
WINDOW_DAYS = 28

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.S)
_TOUCH_RE = re.compile(r"^- (?:created|edited|read|deleted) `([^`]+)`", re.M)


@dataclass(frozen=True)
class Session:
    day: date
    minutes: float
    paths: tuple[str, ...]


def read_frontmatter(text: str) -> dict:
    """The flat `key: value` block at the top of a note (not a YAML parser)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line or line[0] in " \t-":
            continue
        key, _, value = line.partition(":")
        out[key.strip().lower()] = value.strip().strip('"').strip("'")
    return out


def _parse_date(raw: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


def read_sessions(vault_path: str, window_days: int = WINDOW_DAYS) -> list[Session]:
    """Recent sessions: when, how long (capped), and which notes they touched."""
    root = Path(vault_path) / SESSION_LOGS_FOLDER
    if not root.is_dir():
        return []
    cutoff = date.today() - timedelta(days=window_days)
    out = []
    for f in root.glob("*.md"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        fm = read_frontmatter(text)
        day = _parse_date(fm.get("date", ""))
        if day is None or day < cutoff:
            continue
        try:
            minutes = float(fm.get("duration_min", 0) or 0)
        except (TypeError, ValueError):
            minutes = 0.0
        paths = [p.replace("\\", "/").lower() for p in _TOUCH_RE.findall(text)]
        cwd = fm.get("cwd", "")
        if cwd:
            paths.append(cwd.replace("\\", "/").lower())
        out.append(Session(day=day, minutes=min(minutes, SESSION_CAP_MIN), paths=tuple(paths)))
    return out
