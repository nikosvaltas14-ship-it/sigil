"""The weekly Claude limit as the guides' spending budget.

The Claude Code CLI reports how full the subscription's windows are in a
`rate_limit_event` of its stream-json output:

    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
      "unifiedWindows": {"five_hour": {"utilization": 0.31, "resetsAt": 1792143000},
                         "seven_day": {"utilization": 0.82, "resetsAt": 1792519200}}}}

`utilization` is 0..1 and `resetsAt` is epoch seconds. Every stream-json call
(the guide, verify, review calls) carries one, and `read_usage` costs one
tiny Haiku call when nothing else has run yet.

The rule (`decide`): guides may use at most `share` (default 50%) of the weekly
limit, spread evenly over the 7 days of the limit's window. On day N of the
window the weekly utilization must stay at or below `share * N / 7`, counting
everything on the account, not only Sigil, so the week can never end above
`share` because of Sigil. A day that was skipped or cheap carries its allowance
over to the next one.

It fails closed. No reading means no guides: a cap that cannot see the usage
is not a cap.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..fileio import atomic_write_text, read_text_locked
from .models import WORK_DIR

log = logging.getLogger(__name__)

WEEK_SECONDS = 7 * 24 * 3600
DAY_SECONDS = 24 * 3600

DEFAULT_WEEKLY_SHARE = 0.5          # of the weekly limit, for the whole week
DEFAULT_GUIDE_COST = 0.04           # of the weekly limit, until real guides were measured
PROBE_MODEL = "haiku"
PROBE_TIMEOUT = 120
COST_HISTORY = 10                   # guides the cost estimate averages over
COST_PATH = WORK_DIR / "guide_cost.json"


@dataclass(frozen=True)
class Usage:
    week: float | None = None            # seven_day utilization, 0..1
    week_resets_at: int | None = None    # epoch seconds
    session: float | None = None         # five_hour utilization, 0..1
    rejected: bool = False               # the CLI says calls are refused right now


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    retry_later: bool = False   # True when waiting an hour can change the answer


# The most recent reading any Claude call of this process saw.
_latest: Usage | None = None


def latest() -> Usage | None:
    return _latest


def note(reading: Usage | None) -> None:
    global _latest
    if reading is not None:
        _latest = reading


def parse_usage(stdout: str) -> Usage | None:
    """The newest usage from the `rate_limit_event` lines of a stream-json run.

    An event may carry one window or both, so the events are merged in order."""
    week = week_reset = session = None
    rejected = False
    seen = False
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{") or "rate_limit_event" not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        info = obj.get("rate_limit_info") if isinstance(obj, dict) else None
        if obj.get("type") != "rate_limit_event" or not isinstance(info, dict):
            continue
        seen = True
        rejected = info.get("status") == "rejected"
        windows = dict(info.get("unifiedWindows") or {})
        if info.get("rateLimitType") and "utilization" in info:    # the older, flat shape
            windows.setdefault(info["rateLimitType"], info)
        five = windows.get("five_hour")
        seven = windows.get("seven_day")
        if isinstance(five, dict) and _fraction(five.get("utilization")) is not None:
            session = _fraction(five.get("utilization"))
        if isinstance(seven, dict) and _fraction(seven.get("utilization")) is not None:
            week = _fraction(seven.get("utilization"))
            reset = seven.get("resetsAt")
            week_reset = int(reset) if isinstance(reset, (int, float)) else week_reset
    return Usage(week, week_reset, session, rejected) if seen else None


def _fraction(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return max(0.0, float(value))


def read_usage(settings, work_dir: Path) -> Usage | None:
    """A fresh reading: one tiny Haiku call. None when the CLI gives none."""
    from . import generate     # generate imports this module, so not at the top
    global _latest
    _latest = None             # never answer with a reading older than this call
    try:
        generate.run_claude("Reply with the word ok.", model=PROBE_MODEL, cwd=work_dir,
                            timeout=PROBE_TIMEOUT, claude_cmd=settings.claude_cmd,
                            collect_text=True)
    except generate.ClaudeCLIError as exc:
        # A refused call still carries its event, and run_claude noted it.
        log.warning("usage probe: %s", exc)
    return latest()


def allowance(week_resets_at: int, now_ts: float, share: float) -> float:
    """The weekly utilization (0..1) the guides may bring the account to by now."""
    day = int((now_ts - (week_resets_at - WEEK_SECONDS)) // DAY_SECONDS) + 1
    return share * min(7, max(1, day)) / 7


def decide(reading: Usage | None, *, share: float, estimate: float,
           now_ts: float | None = None) -> Decision:
    """May one more guide, expected to cost `estimate` of the weekly limit, start?"""
    if reading is None or reading.week is None or reading.week_resets_at is None:
        return Decision(False, "the weekly usage could not be read", retry_later=True)
    if reading.rejected or (reading.session is not None and reading.session >= 1):
        return Decision(False, "the 5-hour limit is used up", retry_later=True)
    now_ts = time.time() if now_ts is None else now_ts
    allowed = allowance(reading.week_resets_at, now_ts, share)
    if reading.week + estimate > allowed:
        return Decision(False, f"weekly usage {reading.week:.0%} + ~{estimate:.0%} for a "
                               f"guide is over today's allowance of {allowed:.0%} "
                               f"(at most {share:.0%} of the week)")
    return Decision(True, f"weekly usage {reading.week:.0%}, today's allowance {allowed:.0%}")


# ---- what a guide costs, measured -------------------------------------------

def record_cost(before: Usage | None, after: Usage | None, path: Path | None = None) -> None:
    """Remember the weekly share one guide used, so the next check can predict it."""
    if (before is None or after is None or before.week is None or after.week is None
            or before.week_resets_at != after.week_resets_at):
        return                                  # unreadable, or the window rolled over
    delta = after.week - before.week
    if delta <= 0:
        return
    costs = (_read_costs(path) + [delta])[-COST_HISTORY:]
    atomic_write_text(path or COST_PATH, json.dumps(costs))


def estimate_cost(default: float = DEFAULT_GUIDE_COST, path: Path | None = None) -> float:
    """The expected weekly share of one guide: the largest of the recent ones
    (the check should be pessimistic), or `default` before any was measured."""
    costs = _read_costs(path)
    return max(costs) if costs else default


def _read_costs(path: Path | None) -> list[float]:
    try:
        data = json.loads(read_text_locked(path or COST_PATH))
    except (OSError, ValueError):
        return []
    return [float(x) for x in data if isinstance(x, (int, float)) and x > 0] \
        if isinstance(data, list) else []
